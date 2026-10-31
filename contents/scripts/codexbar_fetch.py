#!/usr/bin/env python3
"""CodexBar AgentView data fetcher.

Runs `codexbar usage --json --provider <id> [--source <s>]` for each enabled
provider in parallel, merges the per-provider results into a single JSON
document on stdout. The QML widget calls this once per polling tick.

Usage:
  codexbar_fetch.py --cli-path PATH --providers codex,claude,zai,opencodego,openrouter,kilo,typesafe
  codexbar_fetch.py --cli-path PATH --providers codex,claude --cache-only
  codexbar_fetch.py --cli-path PATH --providers codex,claude --min-interval 60

With --forecast-url it also attaches a `forecast` object describing when the next
OpenAI usage-limit reset is expected (data from codex-reset.com).
Claude yields one record per login: a running Meridian's profiles with the
usage it already polls; else the CLI once per Meridian profile directory
(~/.config/meridian/profiles.json), else CLAUDE_CONFIG_DIR or ~/.claude.
Codex adds a running Meridian's ChatGPT seats to the CLI's accounts. Meridian
is optional, and a missing CLI is fatal only when Meridian served nothing.
Successful normalized usage is kept privately for 24 hours. --cache-only reads
it without executing the CLI; refresh failures retain matching accounts with
stale, cachedAt, and refreshError fields. The same file keeps a run-length
usage history per 7d and 5h window, reported as `recent` consumption over the
last 24 hours and 1 hour respectively.

Every refresh stamps a shared claim. With --min-interval N a
fetch that finds a claim younger than N seconds returns the --cache-only result
instead, so pollers in several processes do not refresh the same usage twice.
Our files live in the state directory from state_paths; the CLI's own
~/.codexbar/config.json is only read.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import state_paths

# Per-provider source flag. None = let the CLI auto-pick.
# Claude defaults to OAuth so we get extraRateWindows (Claude Design,
# Daily Routines, …) — CLI source only returns primary/secondary/tertiary.
PROVIDER_SOURCE: dict[str, str | None] = {
    "codex": "oauth",
    "claude": "oauth",
    "zai": None,
    # api = real opencode.ai quota (needs apiKey in ~/.codexbar/config.json);
    # the CLI's auto pick is the "local" estimate, which is far off.
    "opencodego": "api",
    "openrouter": None,
    "kilo": None,
    # Balance-only. Linux has no browser cookie import, so the CLI needs
    # cookieSource=manual + cookieHeader in ~/.codexbar/config.json.
    "typesafe": None,
}

# Fallback sources tried in order when the primary source errors (e.g. 429).
PROVIDER_FALLBACK_SOURCES: dict[str, list[str]] = {
    "codex": ["cli"],
    "claude": ["cli"],
    "opencodego": ["local"],
}

# omp-sub-rotate keeps one state and one operating-mode file per rotation in
# this directory: `codex` rotates the Codex accounts, `meridian` the Claude
# profiles behind Meridian.
OMP_SUB_ROTATE_DIR = "~/.omp/agent/sub-rotate"
OMP_ROTATIONS = {"codex": "codex", "claude": "meridian"}
ROTATION_DESCRIPTIONS = {
    "codex": {
        "FILL": "Pros share traffic, stop at 85%.",
        "TARGET": "One Pro drains to 100%, redeems.",
        "EXPIRY-BURN": "Burn accounts whose resets expire soon.",
        "BURN": "Global reset announced; Pros stop 95%.",
        "NO-BANK": "No banked resets; Pros stop 95%.",
        "IDLE": "No Pro accounts logged in.",
    },
    "claude": {
        "FILL": "Accounts share traffic, stop at 95%.",
        "TARGET": "One account drains to 100%, redeems.",
        "EXPIRY-BURN": "Burn accounts whose resets expire soon.",
        "BURN": "Global reset announced; accounts stop 95%.",
        "NO-BANK": "No banked resets; accounts stop 95%.",
        "IDLE": "No Claude accounts behind Meridian.",
    },
}


def _expand(path: str) -> str:
    return os.path.expanduser(os.path.expandvars(path))


def _state_file(name: str) -> str:
    return str(state_paths.state_dir() / name)


def _rotation_accounts(provider: str, state: dict, emails: dict) -> dict:
    """Email → open/blocked for every rotated account, or {} when any is ambiguous.

    Codex keys its block list (`owned`) by credential id and labels each
    account with its email; Meridian keys `closed` by profile id, whose email
    comes from Meridian's profile list.
    """
    blocked = state.get("owned" if provider == "codex" else "closed")
    accounts = state.get("accounts")
    if (
        not isinstance(blocked, dict)
        or provider == "codex" and any(not re.fullmatch(r"0|[1-9][0-9]*", key) for key in blocked)
        or not isinstance(accounts, list)
    ):
        return {}
    availability = {}
    keys = set()
    for account in accounts:
        if not isinstance(account, dict):
            return {}
        if provider == "codex":
            key, label = account.get("credentialId"), account.get("label")
            if type(key) is not int or key < 0:
                return {}
            key = str(key)
        else:
            key = account.get("id")
            if not isinstance(key, str) or not key:
                return {}
            label = emails.get(key)
        if (
            not isinstance(label, str)
            or not label.strip()
            or label in availability
            or key in keys
        ):
            return {}
        keys.add(key)
        availability[label] = "blocked" if key in blocked else "open"
    return availability


def _read_rotation(provider: str, emails: dict | None = None) -> dict | None:
    """Read omp's rotation status for `provider` without changing its state or mode.

    No state file means omp does not rotate this provider: nothing is shown.
    """
    name = OMP_ROTATIONS[provider]
    descriptions = ROTATION_DESCRIPTIONS[provider]
    directory = _expand(OMP_SUB_ROTATE_DIR)
    try:
        with open(os.path.join(directory, f"{name}-state.json"), encoding="utf-8") as stream:
            state = json.load(stream)
        if not isinstance(state, dict):
            return None
        mode = state.get("mode")
        stalled = state.get("stalled", False)
        if (
            not isinstance(mode, str)
            or mode not in descriptions
            or not isinstance(stalled, bool)
        ):
            return None
        try:
            with open(os.path.join(directory, f"{name}-mode.json"), encoding="utf-8") as stream:
                config = json.load(stream)
        except FileNotFoundError:
            config = {}
        if not isinstance(config, dict):
            return None
        operating_mode = config.get("operatingMode", "confirm")
        if operating_mode not in ("auto", "confirm"):
            return None
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    result = {
        "mode": mode,
        "description": descriptions[mode],
        "operatingMode": operating_mode,
        "stalled": stalled,
    }
    accounts = _rotation_accounts(provider, state, emails or {})
    if accounts:
        result["accountAvailability"] = accounts
    return result


def _result_error(provider: str, code: str, message: str) -> dict:
    return {"id": provider, "ok": False, "error": {"code": code, "message": message}}


# Money as the balance details rows format it, e.g. "$6.17" or "$1,234.50".
_OR_MONEY_RE = re.compile(r"^\s*\$?([\d,]+(?:\.\d+)?)\s*$")
# Identity line the CLI builds when it has a credit balance, e.g. "Balance: $6.17".
_OR_BALANCE_RE = re.compile(r"^\s*Balance:\s*\$?([\d,]+(?:\.\d+)?)\s*$")


def _balance_text(usage: dict) -> str | None:
    """Remaining credit of a balance-only provider as "$6.17 left".

    OpenRouter: CLI 0.56.3 stopped emitting `openRouterUsage`, so the remaining
    credit is only in the "Credits" / "Remaining" details row, with the identity
    line ("Balance: $6.17") as the last resort. TypeSafe only has the identity
    line. Neither carries a numeric field. Returns None when no amount parses.
    """
    details = usage.get("details")
    for section in details if isinstance(details, list) else []:
        if not isinstance(section, dict) or section.get("title") != "Credits":
            continue
        rows = section.get("rows")
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or row.get("label") != "Remaining":
                continue
            m = _OR_MONEY_RE.match(str(row.get("value") or ""))
            if m:
                return f"${float(m.group(1).replace(',', '')):.2f} left"
    m = _OR_BALANCE_RE.match(str(usage.get("loginMethod") or ""))
    if m:
        return f"${float(m.group(1).replace(',', '')):.2f} left"
    return None


def _reset_credits(usage: dict) -> dict | None:
    """Codex "saved reset" credits: how many are usable and which expires first.

    The CLI mirrors the OpenAI payload (`codexResetCredits.credits[]` with a
    `status` and `expires_at`). Only `available` credits count; the soonest
    expiry is the one the user has to spend first. None when absent.
    """
    raw = usage.get("codexResetCredits")
    if not isinstance(raw, dict):
        return None
    credits = raw.get("credits")
    soonest: _dt.datetime | None = None
    count = 0
    for credit in credits if isinstance(credits, list) else []:
        if not isinstance(credit, dict) or credit.get("status") != "available":
            continue
        count += 1
        expires = _parse_iso(credit.get("expires_at"))
        if expires is not None and (soonest is None or expires < soonest):
            soonest = expires
    return {
        "count": count,
        "soonestExpiresAt": soonest.isoformat() if soonest else None,
    }


# Claude "saved resets" ship in the `cedar_ember` block of Anthropic's OAuth
# usage endpoint. The CodexBar CLI (0.64.1) drops the block, so it is read
# directly. The server only answers for Claude Code's client identity; any
# other User-Agent gets `ineligible_reason: "surface"` and no grants.
CLAUDE_RESET_URL = "https://api.anthropic.com/api/oauth/usage?cedar_ember=1&skip_spend=1"
CLAUDE_PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
CLAUDE_RESET_USER_AGENT = "claude-cli/2.1.280 (external, cli)"
CLAUDE_RESET_TIMEOUT = 8.0

# Meridian (github.com/rynfar/meridian) serves its Claude profiles and the
# usage it polls for them on its local proxy port. `/v1/usage/quota/all` waits
# up to 5 s for ChatGPT seats, so a shorter timeout would drop to the CLI.
MERIDIAN_DEFAULT_PORT = 3456
MERIDIAN_TIMEOUT = 10.0


def _claude_default_dir() -> str:
    return _expand(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude")


def _meridian_profile_dirs() -> list[tuple[str, str]]:
    """(profile id, login directory) per Meridian profile on disk, in Meridian's order.

    Meridian keeps one refreshed Claude Code login per profile in
    `profiles.json`, ordered by `settings.json` `profileOrder`.
    """
    root = os.path.join(_expand(os.environ.get("XDG_CONFIG_HOME") or "~/.config"), "meridian")

    def load(name):
        try:
            with open(os.path.join(root, name), encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    profiles = load("profiles.json")
    settings = load("settings.json")
    order = settings.get("profileOrder") if isinstance(settings, dict) else None
    order = [name for name in order if isinstance(name, str)] if isinstance(order, list) else []
    found = []
    for profile in profiles if isinstance(profiles, list) else []:
        if not isinstance(profile, dict):
            continue
        name, directory = profile.get("id"), profile.get("claudeConfigDir")
        if isinstance(name, str) and isinstance(directory, str) and os.path.isdir(_expand(directory)):
            found.append((order.index(name) if name in order else len(order), name, _expand(directory)))
    found.sort(key=lambda item: item[0])
    return [(name, directory) for _, name, directory in found]


def _claude_config_dirs() -> list[str]:
    """Claude accounts as config directories: Meridian's profiles, else the local one.

    The CodexBar CLI reads one Claude login per run (`CLAUDE_CONFIG_DIR`); its
    token accounts hold static access tokens that are never refreshed.
    """
    dirs = list(dict.fromkeys(directory for _, directory in _meridian_profile_dirs()))
    return dirs or [_claude_default_dir()]


def _meridian_profile_emails(meridian: "_Meridian | None") -> dict[str, str]:
    """Profile id → email: a running Meridian's, else the one its login saved.

    Local files only when `meridian` is None, so a cache-only read stays offline.
    """
    emails = {}
    for name, directory in _meridian_profile_dirs():
        email = _claude_local_account(directory)[1]
        if email:
            emails[name] = email
    for profile, _ in meridian.profiles("claude") if meridian is not None else []:
        email = profile.get("email")
        if isinstance(email, str) and email.strip():
            emails[profile["id"]] = email.strip()
    return emails


def _rotations(providers: list[str], meridian: "_Meridian | None") -> dict:
    """omp's rotation status per enabled provider; None where omp writes none."""
    return {
        "claudeRotation": _read_rotation("claude", _meridian_profile_emails(meridian))
        if "claude" in providers else None,
        "codexRotation": _read_rotation("codex") if "codex" in providers else None,
    }


def _claude_local_account(config_dir: str) -> tuple[str | None, str | None]:
    """(accountUuid, email) Claude Code saved for this login, when it did."""
    paths = [os.path.join(config_dir, ".claude.json")]
    if config_dir == _expand("~/.claude"):
        paths.append(_expand("~/.claude.json"))
    for path in paths:
        try:
            with open(path, encoding="utf-8") as fh:
                account = json.load(fh).get("oauthAccount")
        except (OSError, ValueError, AttributeError):
            continue
        if not isinstance(account, dict):
            continue
        uuid, email = account.get("accountUuid"), account.get("emailAddress")
        if isinstance(email, str) and email.strip():
            return (uuid if isinstance(uuid, str) else None), email.strip()
    return None, None


def _claude_oauth(config_dir: str) -> dict:
    """The login's saved `claudeAiOauth` block, or {}."""
    try:
        with open(os.path.join(config_dir, ".credentials.json"), encoding="utf-8") as fh:
            oauth = json.load(fh).get("claudeAiOauth")
    except (OSError, ValueError, AttributeError):
        return {}
    return oauth if isinstance(oauth, dict) else {}


def _claude_access_token(config_dir: str) -> str | None:
    """The login's unexpired OAuth access token, or None."""
    oauth = _claude_oauth(config_dir)
    token = oauth.get("accessToken")
    if not isinstance(token, str) or not token:
        return None
    expires_ms = _as_float(oauth.get("expiresAt"))
    if expires_ms is not None and expires_ms <= time.time() * 1000:
        return None
    return token


def _claude_plan_weight(tier) -> int:
    """Usage allowance in Pro-sized units, from the rate-limit tier.

    `default_claude_max_5x` / `_20x` carry 5× / 20× Pro's limits, Team
    seats included; any other tier counts as one Pro allowance.
    """
    multiplier = re.search(r"max_(\d+)x", str(tier or ""))
    return int(multiplier.group(1)) if multiplier else 1


def _claude_oauth_get(url: str, token: str, timeout: float) -> dict | None:
    body = _read_http_body(url, timeout, {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": CLAUDE_RESET_USER_AGENT,
    })
    data = json.loads(body.decode("utf-8", "replace"))
    return data if isinstance(data, dict) else None


def _claude_profile_email(token: str, timeout: float) -> str | None:
    """Best effort: the login's email from Anthropic's OAuth profile endpoint."""
    try:
        account = (_claude_oauth_get(CLAUDE_PROFILE_URL, token, timeout) or {}).get("account")
    except Exception:  # noqa: BLE001 - identity is optional
        return None
    email = account.get("email") if isinstance(account, dict) else None
    return email.strip() if isinstance(email, str) and email.strip() else None


def _usable_reset_credits(grants, now: _dt.datetime) -> dict:
    """Sum (resets_left, starts, ends, paused, usable) grants in the Codex `resetCredits` shape.

    Counts grants that are not paused, not flagged unusable, and whose
    starts..ends window contains now; the soonest end is the one to spend first.
    """
    soonest: _dt.datetime | None = None
    count = 0
    for left, starts, ends, paused, usable in grants:
        if paused is True or usable is False:
            continue
        if isinstance(left, bool) or not isinstance(left, int) or left < 1:
            continue
        if (starts is not None and starts > now) or (ends is not None and ends <= now):
            continue
        count += left
        if ends is not None and (soonest is None or ends < soonest):
            soonest = ends
    return {
        "count": count,
        "soonestExpiresAt": soonest.isoformat() if soonest else None,
    }


def _claude_reset_credits(status, now: _dt.datetime) -> dict | None:
    """Usable grants of Anthropic's `cedar_ember` block. None when ineligible."""
    if not isinstance(status, dict) or status.get("eligible") is not True:
        return None
    grants = status.get("grants")
    return _usable_reset_credits((
        (g.get("resets_left"), _parse_iso(g.get("starts_at")), _parse_iso(g.get("ends_at")),
         g.get("paused"), None)
        for g in (grants if isinstance(grants, list) else []) if isinstance(g, dict)
    ), now)


def _meridian_reset_credits(banked, now: _dt.datetime) -> dict | None:
    """Usable grants of a Meridian profile's `bankedResets`. None when ineligible or unknown."""
    if not isinstance(banked, dict) or banked.get("eligible") is not True:
        return None

    def at(ms):
        value = _as_float(ms)
        return _dt.datetime.fromtimestamp(value / 1000, _dt.timezone.utc) if value else None

    grants = banked.get("grants")
    return _usable_reset_credits((
        (g.get("resetsLeft"), at(g.get("startsAt")), at(g.get("expiresAt")),
         g.get("paused"), g.get("usableNow"))
        for g in (grants if isinstance(grants, list) else []) if isinstance(g, dict)
    ), now)


def _fetch_claude_reset_credits(token: str, timeout: float) -> tuple[bool, dict | None]:
    """(answered, credits). Best effort: any failure is (False, None)."""
    try:
        data = _claude_oauth_get(CLAUDE_RESET_URL, token, timeout)
        if data is None:
            return False, None
        return True, _claude_reset_credits(
            data.get("cedar_ember"), _dt.datetime.now(_dt.timezone.utc)
        )
    except Exception:  # noqa: BLE001 - the endpoint cannot break usage
        return False, None


CLAUDE_RESET_CACHE_NAME = "claude_reset_credits.json"
# Saved resets change rarely, and their endpoint is Anthropic's usage endpoint,
# which rate-limits per access token: every extra call eats into the budget
# Meridian needs for the usage readings themselves. Meridian 1.87+ lists them
# itself; this path serves older Meridian and the CLI.
CLAUDE_RESET_TTL = 3600
_claude_reset_lock = threading.Lock()


def _claude_reset_credits_for(email: str | None, token: str | None, may_fetch: bool) -> dict | None:
    """The account's saved resets, asking Anthropic at most once per CLAUDE_RESET_TTL.

    A failed ask also starts the wait and keeps the last answer. Without an
    email there is no cache key, so the endpoint is asked every time.
    """
    key = (email or "").strip().casefold()
    path = _state_file(CLAUDE_RESET_CACHE_NAME)

    def load() -> dict:
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    entry = load().get(key) if key else None
    entry = entry if isinstance(entry, dict) else None
    cached = entry.get("credits") if entry and isinstance(entry.get("credits"), dict) else None
    at = _as_float(entry.get("at")) if entry else None
    if at is not None and 0 <= time.time() - at < CLAUDE_RESET_TTL:
        return cached
    if not (may_fetch and token):
        return cached
    answered, credits = _fetch_claude_reset_credits(token, CLAUDE_RESET_TIMEOUT)
    if not answered:
        credits = cached
    if key:
        with _claude_reset_lock:
            data = load()
            data[key] = {"at": time.time(), "credits": credits}
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=os.path.dirname(path),
                    prefix=".claude_reset-", delete=False,
                ) as fh:
                    os.fchmod(fh.fileno(), 0o600)
                    json.dump(data, fh)
                os.replace(fh.name, path)
            except OSError:
                pass
    return credits


# Providers whose weekly reset also clears the 5h session window. Codex resets
# every window at once. Claude does not: its weekly rollover leaves the running
# 5h session untouched, so clamping would show a session end that never happens.
_WEEKLY_RESET_CLEARS_SESSION = {"codex"}


def _clamp_to_account_reset(provider: str, primary, secondary):
    """End the session window at an earlier weekly reset (Codex only).

    For providers in `_WEEKLY_RESET_CLEARS_SESSION`, a 5h window whose natural
    end falls after the weekly reset actually ends at the weekly reset. The copy
    keeps the natural start in `startsAt` so pace spans the truncated window.
    """
    if provider not in _WEEKLY_RESET_CLEARS_SESSION:
        return primary
    if not isinstance(primary, dict) or not isinstance(secondary, dict):
        return primary
    end = _parse_iso(primary.get("resetsAt"))
    weekly = _parse_iso(secondary.get("resetsAt"))
    minutes = primary.get("windowMinutes")
    if end is None or weekly is None or weekly >= end:
        return primary
    if not isinstance(minutes, (int, float)) or minutes <= 0:
        return primary
    start = end - _dt.timedelta(minutes=minutes)
    if weekly <= start:
        return primary
    return {
        **primary,
        "resetsAt": secondary["resetsAt"],
        "startsAt": start.isoformat().replace("+00:00", "Z"),
    }


def _normalize_record(provider: str, record: dict) -> dict:
    raw_usage = record.get("usage")
    usage = raw_usage if isinstance(raw_usage, dict) else {}
    raw_identity = usage.get("identity")
    identity = raw_identity if isinstance(raw_identity, dict) else {}
    raw_email = usage.get("accountEmail") or record.get("account")
    account_email = raw_email.strip() if isinstance(raw_email, str) else None
    raw_login_method = usage.get("loginMethod") or identity.get("loginMethod")
    login_method = raw_login_method.strip() if isinstance(raw_login_method, str) else None

    if record.get("error"):
        raw_error = record["error"]
        error = raw_error if isinstance(raw_error, dict) else {}
        result = _result_error(
            provider,
            str(error.get("kind", "provider")),
            str(error.get("message") or (
                raw_error if isinstance(raw_error, str) else "unknown error"
            )),
        )
        result.update({
            "loginMethod": login_method,
            "accountEmail": account_email,
        })
        return result

    primary = usage.get("primary")
    or_usage = usage.get("openRouterUsage")
    balance_text: str | None = None
    if provider == "openrouter":
        # The CodexBar CLI returns primary.usedPercent=100 as a placeholder.
        # Only a real per-key allowance replaces it; otherwise no bar renders.
        primary = None
        if isinstance(or_usage, dict):
            key_limit = or_usage.get("keyLimit")
            if isinstance(key_limit, (int, float)) and key_limit > 0:
                monthly = or_usage.get("keyUsageMonthly")
                monthly = monthly if isinstance(monthly, (int, float)) else 0.0
                primary = {
                    "usedPercent": min(100.0, (monthly / key_limit) * 100.0),
                    "resetDescription": f"${monthly:.2f} / ${key_limit:.0f}",
                }
        if primary is None:
            balance_text = _balance_text(usage)
    elif provider == "typesafe":
        # Spend and credit balance only; the CLI emits no rate window.
        primary = None
        balance_text = _balance_text(usage)
    elif provider == "kilo" and isinstance(primary, dict):
        # Kilo reports used/total credits. With auto-topup off this is a
        # balance, not a recurring window, so show the amount in the header.
        m = re.match(
            r"^\s*([\d.]+)\s*/\s*([\d.]+)\s*credits?",
            primary.get("resetDescription") or "",
        )
        if m:
            try:
                used = float(m.group(1))
                total = float(m.group(2))
            except ValueError:
                used = total = None
            if used is not None and total is not None:
                balance_text = f"${max(0.0, total - used):.2f} left"
                primary = None
    raw_extras = usage.get("extraRateWindows")
    extra_rate_windows = [
        extra
        for extra in (raw_extras if isinstance(raw_extras, list) else [])
        if isinstance(extra, dict)
        and not (
            provider == "codex"
            and str(extra.get("id", "")).startswith("codex-spark")
        )
    ]
    secondary = usage.get("secondary")
    return {
        "id": provider,
        "ok": True,
        "loginMethod": login_method,
        "accountEmail": account_email,
        "primary": _clamp_to_account_reset(provider, primary, secondary),
        "secondary": secondary,
        "tertiary": usage.get("tertiary"),
        "extraRateWindows": extra_rate_windows,
        "resetCredits": _reset_credits(usage) if provider == "codex" else None,
        "openRouterUsage": or_usage,
        "balanceText": balance_text,
        "updatedAt": usage.get("updatedAt"),
        "error": None,
    }


def _run_cli(
    cli: str, provider: str, source: str | None, timeout: float, env: dict | None = None,
) -> list[dict]:
    """Invoke `codexbar usage` and normalize every returned account."""
    cmd = [cli, "usage", "--json", "--provider", provider]
    if provider == "codex":
        cmd.append("--all-accounts")
    if source:
        cmd += ["--source", source]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False, env=env,
        )
    except FileNotFoundError:
        return [_result_error(provider, "cli_missing", f"CLI not found at {cli}")]
    except subprocess.TimeoutExpired:
        return [_result_error(provider, "timeout", f"CLI timed out after {timeout}s")]
    except OSError as exc:
        return [_result_error(provider, "cli_error", str(exc))]
    except UnicodeError as exc:
        return [_result_error(provider, "parse", str(exc))]

    stdout = (proc.stdout or "").strip()
    if not stdout:
        return [
            _result_error(
                provider,
                "no_output",
                (proc.stderr or "empty stdout").strip()[:400],
            )
        ]
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return [_result_error(provider, "parse", f"{exc}: {stdout[:200]}")]

    records = payload if isinstance(payload, list) else [payload]
    if not records or any(not isinstance(record, dict) for record in records):
        return [_result_error(provider, "shape", "unexpected CLI payload")]
    results = [_normalize_record(provider, record) for record in records]
    account_count = len(results)
    for result in results:
        result["accountCount"] = account_count
    return results


def _fetch_sources(cli: str, provider: str, timeout: float, env: dict | None = None) -> list[dict]:
    results = _run_cli(cli, provider, PROVIDER_SOURCE.get(provider), timeout, env)
    if not any(result.get("ok") for result in results):
        for fallback in PROVIDER_FALLBACK_SOURCES.get(provider, []):
            retries = _run_cli(cli, provider, fallback, timeout, env)
            if any(retry.get("ok") for retry in retries):
                results = retries
                break
    return results


class _ClaudeLogin:
    """Every fact about one Claude login, from the first source that has it.

    Meridian's profile and usage entry come first, the login directory
    (`.claude.json`, `.credentials.json`) next, and Anthropic is asked only
    for what neither knows. The Meridian and the CLI path both read accounts
    through this class, so neither can skip a source or reorder them.
    """

    def __init__(self, directory, profile: dict | None = None, usage: dict | None = None) -> None:
        self.directory = _expand(directory) if isinstance(directory, str) and directory else None
        self.profile = profile or {}
        self.usage = usage or {}

    def _field(self, key: str) -> str | None:
        oauth = _claude_oauth(self.directory) if self.directory else {}
        for value in (self.profile.get(key), oauth.get(key)):
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def token(self) -> str | None:
        return _claude_access_token(self.directory) if self.directory else None

    def email(self, reported: str | None = None) -> str | None:
        """Meridian's, else the CLI-reported, else the saved, else Anthropic's."""
        for value in (self.profile.get("email"), reported):
            if isinstance(value, str) and value.strip():
                return value.strip()
        saved = _claude_local_account(self.directory)[1] if self.directory else None
        if saved:
            return saved
        token = self.token()
        return _claude_profile_email(token, CLAUDE_RESET_TIMEOUT) if token else None

    def plan(self, reported: str | None = None) -> str | None:
        """Meridian's plan, else the CLI-reported one, else the saved login's."""
        if reported and not self.profile.get("subscriptionType"):
            return reported
        return _claude_plan(self._field("subscriptionType"), self._field("rateLimitTier")) or reported

    def weight(self) -> int:
        return _claude_plan_weight(self._field("rateLimitTier"))

    def reset_credits(self, email: str | None, may_fetch: bool) -> dict | None:
        """Meridian 1.87+'s `bankedResets`, else Anthropic at most hourly."""
        if "bankedResets" in self.usage:
            return _meridian_reset_credits(
                self.usage["bankedResets"], _dt.datetime.now(_dt.timezone.utc))
        return _claude_reset_credits_for(email, self.token(), may_fetch)


def _fetch_claude_account(cli: str, config_dir: str, timeout: float) -> list[dict]:
    """One Claude login. Every record carries the login's email when it is known,
    so a failed refresh keeps that account's last-known usage."""
    env = None
    if config_dir != _claude_default_dir():
        env = {**os.environ, "CLAUDE_CONFIG_DIR": config_dir}
    results = _fetch_sources(cli, "claude", timeout, env)
    login = _ClaudeLogin(config_dir)
    email = login.email(next((r["accountEmail"] for r in results if r.get("accountEmail")), None))
    ok = any(result.get("ok") for result in results)
    credits = login.reset_credits(email, True) if ok else None
    weight = login.weight()
    for result in results:
        if email and not result.get("accountEmail"):
            result["accountEmail"] = email
        result["loginMethod"] = login.plan(result.get("loginMethod"))
        result["planWeight"] = weight
        if result.get("ok"):
            result["resetCredits"] = credits
    return results


def _claude_plan(subscription, tier) -> str | None:
    """Plan label in the CLI's form: "Claude Team", "Claude Max 5x"."""
    name = {"max": "Max", "pro": "Pro", "team": "Team", "enterprise": "Enterprise"}.get(
        str(subscription or "").lower())
    if name is None:
        return None
    multiplier = re.search(r"max_(\d+x)", str(tier or ""))
    if name == "Max" and multiplier:
        name += " " + multiplier.group(1)
    return "Claude " + name


def _meridian_get(path: str) -> dict:
    port = os.environ.get("MERIDIAN_PORT") or str(MERIDIAN_DEFAULT_PORT)
    key = os.environ.get("MERIDIAN_API_KEY")
    body = _read_http_body(
        f"http://127.0.0.1:{port}{path}", MERIDIAN_TIMEOUT, {"x-api-key": key} if key else None)
    data = json.loads(body.decode("utf-8", "replace"))
    if not isinstance(data, dict):
        raise ValueError(f"Meridian {path}: not an object")
    return data


class _Meridian:
    """One read of a running Meridian per refresh, shared by Claude and Codex.

    Meridian is optional: when it does not answer, `snapshot()` is None and
    each provider falls back to the CodexBar CLI.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._read = False
        self._snapshot: tuple[dict, dict] | None = None

    def snapshot(self) -> tuple[dict, dict] | None:
        with self._lock:
            if not self._read:
                self._read = True
                try:
                    self._snapshot = (_meridian_get("/profiles/list"),
                                      _meridian_get("/v1/usage/quota/all"))
                except Exception:  # noqa: BLE001 - Meridian is optional
                    self._snapshot = None
            return self._snapshot

    def profiles(self, provider: str) -> list[tuple[dict, dict]]:
        """(profile, usage) per Claude profile or ChatGPT seat, in Meridian's order."""
        snapshot = self.snapshot()
        if snapshot is None:
            return []
        listed, quota = snapshot
        raw_usage = quota.get("profiles")
        usage = {
            entry.get("id"): entry for entry in (raw_usage if isinstance(raw_usage, list) else [])
            if isinstance(entry, dict)
        }
        order = listed.get("profileOrder")
        order = [name for name in order if isinstance(name, str)] if isinstance(order, list) else []
        raw_profiles = listed.get("profiles")
        wanted = (lambda kind: kind.startswith("claude")) if provider == "claude" \
            else (lambda kind: kind == "chatgpt")
        profiles = [
            profile for profile in (raw_profiles if isinstance(raw_profiles, list) else [])
            if isinstance(profile, dict) and isinstance(profile.get("id"), str)
            and wanted(str(profile.get("type", "")))
        ]
        if provider == "claude" and not profiles and isinstance(usage.get("default"), dict):
            # Without configured profiles Meridian serves the local login as `default`.
            profiles = [{"id": "default", "type": "claude-max", "credentialDir": _claude_default_dir()}]
        profiles.sort(key=lambda p: order.index(p["id"]) if p["id"] in order else len(order))
        return [(profile, usage.get(profile["id"]) or {}) for profile in profiles]


def _meridian_window(window: dict, minutes: int) -> dict | None:
    used = _as_float(window.get("utilization"))
    if used is None:
        return None
    resets = _as_float(window.get("resetsAt"))
    return {
        "usedPercent": round(used * 100, 1),
        "windowMinutes": minutes,
        "resetsAt": _dt.datetime.fromtimestamp(resets / 1000, _dt.timezone.utc).isoformat()
        if resets else None,
    }


def _meridian_claude_account(profile: dict, usage: dict) -> list[dict]:
    """One Meridian profile as a CLI-shaped record, through `_normalize_record`.

    Window types follow Anthropic's OAuth usage keys, mapped like the CLI:
    Sonnet weekly is the tertiary window, Cowork is Daily Routines, other
    model weeklies are scoped extras, and Design (omelette) and app limits
    are not shown.
    """
    login = _ClaudeLogin(profile.get("credentialDir"), profile, usage)
    email = login.email()
    identity = {"accountEmail": email, "loginMethod": login.plan()}
    raw_windows = usage.get("windows")
    windows = {
        window["type"]: window for window in (raw_windows if isinstance(raw_windows, list) else [])
        if isinstance(window, dict) and isinstance(window.get("type"), str)
    }
    if not windows:
        return [_meridian_unavailable("claude", usage, identity)]
    extras = []
    for kind, window in windows.items():
        scope = kind.removeprefix("seven_day_")
        if kind in ("five_hour", "seven_day", "seven_day_sonnet") or scope == kind \
                or scope in ("omelette", "oauth_apps"):
            continue
        converted = _meridian_window(window, 10080)
        if converted is None:
            continue
        if scope == "cowork":
            extras.append({"id": "claude-routines", "title": "Daily Routines", "window": converted})
        else:
            extras.append({
                "id": f"claude-weekly-scoped-{scope}",
                "title": scope.replace("_", " ").capitalize() + " only",
                "window": converted,
            })
    fetched = _as_float(usage.get("fetchedAt"))
    record = _normalize_record("claude", {"usage": {
        **identity,
        "primary": _meridian_window(windows.get("five_hour") or {}, 300),
        "secondary": _meridian_window(windows.get("seven_day") or {}, 10080),
        "tertiary": _meridian_window(windows.get("seven_day_sonnet") or {}, 10080),
        "extraRateWindows": extras,
        "updatedAt": _dt.datetime.fromtimestamp(fetched / 1000, _dt.timezone.utc).isoformat()
        if fetched else None,
    }})
    record["planWeight"] = login.weight()
    # A rate-limited login gets no extra call; the last answer stands.
    record["resetCredits"] = login.reset_credits(email, usage.get("stale") is not True)
    return [_meridian_stale(record, usage)]


def _meridian_unavailable(provider: str, usage: dict, identity: dict) -> dict:
    failure = usage.get("failure")
    reason = (failure.get("reason") if isinstance(failure, dict) else None) or usage.get("error")
    message = f"Meridian has no usage for this account ({reason or 'not fetched yet'})"
    return _normalize_record(provider, {
        "error": {"kind": "meridian", "message": message}, "usage": identity,
    })


def _meridian_stale(record: dict, usage: dict) -> dict:
    if usage.get("stale") is not True:
        return record
    # Meridian's last poll failed; this is its earlier reading.
    failure = usage.get("failure")
    reason = failure.get("reason") if isinstance(failure, dict) else None
    record = _last_known(record, f"Meridian refresh failed ({reason or 'unknown'})")
    record["cachedAt"] = record.get("updatedAt")
    return record


_MERIDIAN_WINDOW_UNITS = {"m": 1, "h": 60, "d": 1440}


def _meridian_window_minutes(kind: str) -> int | None:
    """Width of a ChatGPT seat window: `five_hour`, `seven_day`, or a label like `30d`."""
    if kind == "five_hour":
        return 300
    if kind == "seven_day":
        return 10080
    match = re.fullmatch(r"(\d+)([mhd])", kind)
    return int(match.group(1)) * _MERIDIAN_WINDOW_UNITS[match.group(2)] if match else None


def _meridian_seat_resets(resets) -> dict | None:
    """A seat's saved resets (`{available, expiresAt: [ms|null]}`) in the `resetCredits` shape."""
    if not isinstance(resets, dict):
        return None
    count = resets.get("available")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return None
    expiries = resets.get("expiresAt")
    soonest = min((
        ms for ms in (_as_float(value) for value in (expiries if isinstance(expiries, list) else []))
        if ms
    ), default=None)
    return {
        "count": count,
        "soonestExpiresAt": _dt.datetime.fromtimestamp(soonest / 1000, _dt.timezone.utc).isoformat()
        if soonest else None,
    }


def _meridian_codex_account(profile: dict, usage: dict) -> dict:
    """One Meridian ChatGPT seat as a Codex record.

    Seat windows are named by width, not position: a 5h window is primary,
    the weekly one secondary, and a plan with only a wider one (Free's 30d)
    shows that as its secondary.
    """
    email = profile.get("email") if isinstance(profile.get("email"), str) else None
    plan = profile.get("subscriptionType")
    identity = {"accountEmail": email, "loginMethod": plan if isinstance(plan, str) else None}
    raw_windows = usage.get("windows")
    windows = sorted((
        (minutes, window) for window in (raw_windows if isinstance(raw_windows, list) else [])
        if isinstance(window, dict) and isinstance(window.get("type"), str)
        and (minutes := _meridian_window_minutes(window["type"])) is not None
    ), key=lambda item: item[0])
    if not windows:
        return _meridian_unavailable("codex", usage, identity)
    slots: dict[str, dict | None] = {"primary": None, "secondary": None}
    extras = []
    for minutes, window in windows:
        converted = _meridian_window(window, minutes)
        if converted is None:
            continue
        slot = "primary" if minutes < 1440 else "secondary"
        if slots[slot] is None:
            slots[slot] = converted
        else:
            extras.append({"id": f"codex-meridian-{window['type']}",
                           "title": window["type"], "window": converted})
    fetched = _as_float(usage.get("fetchedAt"))
    record = _normalize_record("codex", {"usage": {
        **identity,
        **slots,
        "extraRateWindows": extras,
        "updatedAt": _dt.datetime.fromtimestamp(fetched / 1000, _dt.timezone.utc).isoformat()
        if fetched else None,
    }})
    record["resetCredits"] = _meridian_seat_resets(profile.get("resets"))
    return _meridian_stale(record, usage)


def _fetch_claude(cli: str, timeout: float, meridian: _Meridian) -> list[dict]:
    """Every Claude login, one record per account, fetched in parallel.

    A running Meridian already polls each profile's usage, and Anthropic's
    usage endpoint rate-limits per access token: reading Meridian's copy keeps
    this widget from starving Meridian's routing data, and the reverse.
    Without Meridian every login directory goes through the CLI.
    """
    served = meridian.profiles("claude")
    if served:
        jobs = [lambda p=profile, u=usage: _meridian_claude_account(p, u) for profile, usage in served]
    else:
        jobs, uuids = [], set()
        for config_dir in _claude_config_dirs():
            uuid = _claude_local_account(config_dir)[0]
            if uuid and uuid in uuids:
                continue
            uuids.add(uuid)
            jobs.append(lambda d=config_dir: _fetch_claude_account(cli, d, timeout))
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        per_login = list(pool.map(lambda job: job(), jobs))
    return _one_record_per_account(record for records in per_login for record in records)


def _one_record_per_account(records) -> list[dict]:
    """The first record per email, in order; every email-less record stays."""
    results, emails = [], set()
    for record in records:
        email = (record.get("accountEmail") or "").strip().casefold()
        if email and email in emails:
            continue
        emails.add(email)
        results.append(record)
    for record in results:
        record["accountCount"] = len(results)
    return results


def _fetch_codex(cli: str, timeout: float, meridian: _Meridian) -> list[dict]:
    """Codex accounts from the CLI, plus every ChatGPT seat Meridian serves.

    The CLI's reading of an account wins: it carries Reserve and saved-reset
    detail. A seat fills in what the CLI cannot read, and with seats a missing
    CLI is no error, since Meridian is then the Codex source.
    """
    cli_records = _fetch_sources(cli, "codex", timeout)
    seats = [_meridian_codex_account(profile, usage) for profile, usage in meridian.profiles("codex")]
    if not seats:
        return cli_records
    cli_records = [record for record in cli_records if record.get("ok")
                   or (record.get("error") or {}).get("code") != "cli_missing"]
    ok, failed = (lambda r: r.get("ok")), (lambda r: not r.get("ok"))
    return _one_record_per_account(
        [*filter(ok, cli_records), *filter(ok, seats), *filter(failed, cli_records), *filter(failed, seats)])


def _fetch_provider(cli: str, provider: str, timeout: float, meridian: _Meridian | None = None) -> list[dict]:
    meridian = meridian or _Meridian()
    if provider == "claude":
        return _fetch_claude(cli, timeout, meridian)
    if provider == "codex":
        return _fetch_codex(cli, timeout, meridian)
    return _fetch_sources(cli, provider, timeout)


def _cli_version(cli: str, timeout: float = 5.0) -> str | None:
    """Return the codexbar CLI version, or None when it cannot be determined.

    `codexbar --version` prints e.g. "CodexBar 0.56.3". Any failure (missing
    binary, non-zero exit, old CLI without the flag, unexpected text) is
    silent: the widget simply hides the version label.
    """
    try:
        proc = subprocess.run(
            [cli, "--version"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None
    if proc.returncode != 0:
        return None
    match = re.search(r"\d+(?:\.\d+)+(?:[-+][0-9A-Za-z.]+)?", proc.stdout or "")
    return match.group(0) if match else None

# codex-reset.com publishes a probabilistic forecast for the next OpenAI
# usage-limit reset. It exposes no point estimate, only per-horizon
# probabilities plus the observed cadence, so the ETA below is derived:
# last confirmed reset + recent median cadence, snapped into the hour window
# resets historically land in.
FORECAST_TIMEOUT = 8.0
FORECAST_MAX_BODY_BYTES = 256 * 1024
# Resets happen every few days and the model refreshes slowly, so a short
# cache keeps a 30-second poll tick from hammering a third-party endpoint.
FORECAST_CACHE_TTL = 900.0
FORECAST_CACHE_NAME = "forecast_cache.json"
FORECAST_MAX_MEDIAN_DAYS = 365.0
# Same origin as the forecast. Open Codex incidents precede compensation
# resets, so the status feed rides along with the forecast fetch.
FORECAST_STATUS_PATH = "/api/status-history"
FORECAST_STATUS_TIMEOUT = 4.0


def _as_float(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _as_hour(value) -> int | None:
    parsed = _as_float(value)
    if parsed is None or not parsed.is_integer() or not 0 <= parsed <= 23:
        return None
    return int(parsed)


def _parse_iso(value) -> _dt.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = _dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_dt.timezone.utc)
        return parsed.astimezone(_dt.timezone.utc)
    except (ValueError, OverflowError):
        return None


USAGE_CACHE_NAME = "usage_cache.json"
# Stamped with the start time of every fetch that runs the CLI.
FETCH_CLAIM_NAME = "fetch_claim"
USAGE_CACHE_TTL = 24 * 60 * 60
# Recent-consumption lookback per window length: minutes → hours.
RECENT_LOOKBACK_HOURS = {10080: 24, 300: 1}
# Observations further apart than this leave the time between them unobserved.
HISTORY_GAP_SECONDS = 15 * 60
# A reset time moving later by more than this, or usage dropping by more than
# the percent tolerance, starts a new window cycle. Both absorb API jitter.
HISTORY_CYCLE_SHIFT_SECONDS = 10 * 60
HISTORY_RESET_DROP_PERCENT = 0.5
USAGE_CREDENTIAL_ENV = {
    # Claude and Codex accounts can come from Meridian: another instance or
    # key is another roster.
    "claude": ("MERIDIAN_PORT", "MERIDIAN_API_KEY"),
    "codex": ("MERIDIAN_PORT", "MERIDIAN_API_KEY"),
    "zai": ("ZAI_API_KEY",),
    "opencodego": ("OPENCODE_API_KEY",),
    "openrouter": ("OPENROUTER_API_KEY", "OPENROUTER_MANAGEMENT_API_KEY"),
    "kilo": ("KILO_API_KEY",),
}


def _usage_auth_input(provider: str, payload: dict, path: str) -> dict:
    if provider == "codex":
        tokens = payload.get("tokens")
        if isinstance(tokens, dict):
            account_id = tokens.get("account_id") or tokens.get("accountId")
            if isinstance(account_id, str) and account_id.strip():
                # Codex's documented account id survives OAuth token rotation.
                # Keep other auth modes/keys in the fingerprint, not rotating tokens.
                return {"credentials": {
                    **{key: value for key, value in payload.items()
                       if key not in ("tokens", "last_refresh")},
                    "tokens": {"account_id": account_id},
                }}
    elif provider == "opencodego":
        payload = payload.get("opencode-go")
    elif provider == "kilo":
        payload = payload.get("kilo")
    # Claude's local OAuth record has no reliable account id. Opaque secrets
    # must remain conservative: a refresh can also be an account replacement.
    return {"path": path, "credentials": payload}


def _usage_cache_scopes(cli: str, providers: list[str]) -> dict[str, str]:
    """Fingerprint local account inputs without storing credentials in the cache."""
    try:
        with open(_expand("~/.codexbar/config.json"), encoding="utf-8") as fh:
            config = json.load(fh)
    except FileNotFoundError:
        config = {}
    except (OSError, ValueError):
        return {}
    if not isinstance(config, dict) or not isinstance(config.get("providers", []), list):
        return {}
    scopes = {}
    for provider in providers:
        entries = [
            entry for entry in config.get("providers", [])
            if isinstance(entry, dict) and entry.get("id") == provider
        ]
        auth_paths = []
        if provider == "codex":
            homes = [os.environ.get("CODEX_HOME") or "~/.codex"]
            for entry in entries:
                paths = entry.get("codexProfileHomePaths", [])
                if isinstance(paths, list):
                    homes.extend(path for path in paths if isinstance(path, str))
            auth_paths = [os.path.join(_expand(home), "auth.json") for home in homes]
        elif provider == "claude":
            auth_paths = [
                os.path.join(directory, ".credentials.json") for directory in _claude_config_dirs()
            ]
        elif provider == "opencodego":
            auth_paths = [_expand("~/.local/share/opencode/auth.json")]
        elif provider == "kilo":
            auth_paths = [_expand("~/.local/share/kilo/auth.json")]
        inputs = {
            "cli": os.path.realpath(shutil.which(cli) or cli),
            "provider": provider,
            "config": entries,
            "environment": {
                name: os.environ[name]
                for name in USAGE_CREDENTIAL_ENV.get(provider, ())
                if name in os.environ
            },
        }
        digest = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode())
        auth_inputs = set()
        try:
            for path in sorted(set(auth_paths)):
                contribution = {"path": path, "missing": True}
                try:
                    with open(path, encoding="utf-8") as fh:
                        auth = json.load(fh)
                    if not isinstance(auth, dict):
                        raise ValueError("invalid credential object")
                    contribution = _usage_auth_input(provider, auth, path)
                except FileNotFoundError:
                    pass
                auth_inputs.add(json.dumps(contribution, sort_keys=True))
        except (OSError, ValueError):
            continue
        # --all-accounts reads a roster, not a default/profile assignment.
        # Deduplicate known identities so omp can rotate the default among
        # configured profiles; opaque and missing credentials stay path-bound.
        digest.update(json.dumps(sorted(auth_inputs)).encode())
        scopes[provider] = digest.hexdigest()
    return scopes


def _cached_usage_record(record: object, now: _dt.datetime) -> dict | None:
    """Validate persisted data and retain only normalized display fields."""
    if not isinstance(record, dict) or record.get("ok") is not True or record.get("error"):
        return None
    if not isinstance(record.get("id"), str):
        return None
    measured = _parse_iso(record.get("updatedAt"))
    if measured is None or not 0 <= (now - measured).total_seconds() <= USAGE_CACHE_TTL:
        return None
    if "cachedAt" in record and _parse_iso(record["cachedAt"]) != measured:
        return None
    result = {
        "id": record["id"], "ok": True, "error": None,
        "updatedAt": measured.isoformat(), "cachedAt": measured.isoformat(),
    }
    for key in ("accountEmail", "loginMethod", "balanceText"):
        value = record.get(key)
        if value is not None and not isinstance(value, str):
            return None
        result[key] = value
    weight = record.get("planWeight")
    if weight is not None:
        if type(weight) is not int or weight < 1:
            return None
        result["planWeight"] = weight
    # A last-known reading keeps why it is last-known, so a cache read cannot
    # pass it off as current (Tern adopts recent error-free cache as live).
    refresh_error = record.get("refreshError")
    if record.get("stale") is True and isinstance(refresh_error, str) and refresh_error:
        result["stale"] = True
        result["refreshError"] = refresh_error

    def window(value):
        if not isinstance(value, dict):
            raise ValueError("invalid cached window")
        used = value.get("usedPercent")
        if isinstance(used, bool) or not isinstance(used, (int, float)) or not math.isfinite(used):
            raise ValueError("invalid cached usage")
        normalized = {"usedPercent": used}
        for key in ("resetsAt", "startsAt", "resetDescription"):
            field = value.get(key)
            if field is not None:
                if not isinstance(field, str) or (key != "resetDescription" and _parse_iso(field) is None):
                    raise ValueError("invalid cached reset")
                normalized[key] = field
        minutes = value.get("windowMinutes")
        if minutes is not None:
            if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or not math.isfinite(minutes) or minutes <= 0:
                raise ValueError("invalid cached duration")
            normalized["windowMinutes"] = minutes
        return normalized

    try:
        for slot in ("primary", "secondary", "tertiary"):
            value = record.get(slot)
            result[slot] = window(value) if value is not None else None
        extras = record.get("extraRateWindows", [])
        if not isinstance(extras, list):
            return None
        result["extraRateWindows"] = []
        for extra in extras:
            if not isinstance(extra, dict) or not all(
                isinstance(extra.get(key, ""), str) for key in ("id", "title")
            ):
                return None
            result["extraRateWindows"].append({
                "id": extra.get("id", ""), "title": extra.get("title", ""),
                "window": window(extra.get("window")),
            })
        credits = record.get("resetCredits")
        if credits is not None:
            if not isinstance(credits, dict) or type(credits.get("count")) is not int or credits["count"] < 0:
                return None
            expires = credits.get("soonestExpiresAt")
            if expires is not None and _parse_iso(expires) is None:
                return None
            result["resetCredits"] = {"count": credits["count"], "soonestExpiresAt": expires}
        router = record.get("openRouterUsage")
        if isinstance(router, dict):
            result["openRouterUsage"] = {
                key: value for key, value in router.items()
                if key in ("balance", "keyLimit", "keyUsageMonthly")
                and type(value) in (int, float) and math.isfinite(value)
            }
    except (ValueError, OverflowError):
        return None
    return result


def _usage_cache_payload() -> dict:
    try:
        with open(_state_file(USAGE_CACHE_NAME), encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) and payload.get("version") == 1 else {}


def _usage_cache_entries(payload: dict, now: _dt.datetime) -> dict[str, dict]:
    """Prune the shared cache, retaining only safe normalized entries."""
    entries = payload.get("providers")
    if not isinstance(entries, dict):
        return {}
    cached = {}
    for provider, entry in entries.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("scope"), str):
            continue
        if re.fullmatch(r"[a-f0-9]{64}", entry["scope"]) is None:
            continue
        records = entry.get("records")
        if not isinstance(records, list):
            continue
        valid = []
        for record in records:
            normalized = _cached_usage_record(record, now)
            if normalized is not None and normalized["id"] == provider:
                valid.append(normalized)
        if valid:
            cached[provider] = {"scope": entry["scope"], "records": valid}
    return cached


def _usage_cache_read(scopes: dict[str, str], now: _dt.datetime) -> dict[str, list[dict]]:
    cached = {}
    for provider, entry in _usage_cache_entries(_usage_cache_payload(), now).items():
        if scopes.get(provider) == entry["scope"]:
            records = entry["records"]
            cached[provider] = [
                {**record, "sourceScope": entry["scope"], "accountCount": len(records)}
                for record in records
            ]
    return cached


def _usage_cache_write(scopes: dict[str, str], records: list[dict], now: _dt.datetime) -> dict:
    """Replace the scoped entries, record fresh observations, and return the history."""
    replacements = {provider: {"scope": scope, "records": []} for provider, scope in scopes.items()}
    for record in records:
        normalized = _cached_usage_record(record, now)
        if normalized is not None and normalized["id"] in replacements:
            replacements[normalized["id"]]["records"].append(normalized)
    path = _state_file(USAGE_CACHE_NAME)
    temporary = None
    history: dict[str, list[list]] = {}
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Lock the stable sidecar, not the cache inode replaced by os.replace().
        with open(path + ".lock", "a", encoding="utf-8") as lock:
            os.fchmod(lock.fileno(), 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            payload = _usage_cache_payload()
            entries = _usage_cache_entries(payload, now)
            entries.update(replacements)
            history = _usage_history_entries(payload, now)
            for record in records:
                if record.get("ok") is True and record.get("stale") is not True:
                    _usage_history_observe(history, record)
            _usage_history_prune(history, now)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=os.path.dirname(path),
                prefix=".usage_cache-", delete=False,
            ) as fh:
                temporary = fh.name
                os.fchmod(fh.fileno(), 0o600)
                json.dump({"version": 1, "providers": entries, "history": history}, fh, allow_nan=False)
            os.replace(temporary, path)
    except (OSError, ValueError):
        pass
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
    return history


def _usage_history_key(record: dict, slot: str) -> str:
    account = (record.get("accountEmail") or "").strip().casefold()
    return f"{record['id']}:{account}:{slot}"


def _usage_history_windows(record: dict):
    """Yield (key, window) for windows with a recent-consumption lookback."""
    windows = [(slot, record.get(slot)) for slot in ("primary", "secondary", "tertiary")]
    for extra in record.get("extraRateWindows") or []:
        if isinstance(extra, dict):
            windows.append(("extra:" + str(extra.get("id", "")), extra.get("window")))
    for slot, window in windows:
        if isinstance(window, dict) and window.get("windowMinutes") in RECENT_LOOKBACK_HOURS:
            yield _usage_history_key(record, slot), window


def _usage_history_entries(payload: dict, now: _dt.datetime) -> dict[str, list[list]]:
    """Validated runs per window: [firstSeen, lastSeen, usedPercent, resetsAt|None]."""
    history = payload.get("history")
    if not isinstance(history, dict):
        return {}
    limit = int(now.timestamp())
    valid = {}
    for key, runs in history.items():
        if not isinstance(runs, list):
            continue
        kept = []
        for run in runs:
            if not (
                isinstance(run, list) and len(run) == 4
                and type(run[0]) is int and type(run[1]) is int
                and (kept[-1][1] if kept else 0) <= run[0] <= run[1] <= limit
                and type(run[2]) in (int, float) and math.isfinite(run[2])
                and (run[3] is None or type(run[3]) is int)
            ):
                kept = []
                break
            kept.append(run)
        if kept:
            valid[key] = kept
    return valid


def _usage_history_new_cycle(previous: list, current: list) -> bool:
    """A usage drop or a later reset time means the window reset in between."""
    if current[2] < previous[2] - HISTORY_RESET_DROP_PERCENT:
        return True
    return (previous[3] is not None and current[3] is not None
            and current[3] - previous[3] > HISTORY_CYCLE_SHIFT_SECONDS)


def _usage_history_observe(history: dict[str, list[list]], record: dict) -> None:
    """Extend the window's current run, or start one when the reading changed."""
    measured = _parse_iso(record.get("updatedAt"))
    if measured is None:
        return
    seen = int(measured.timestamp())
    for key, window in _usage_history_windows(record):
        used = window.get("usedPercent")
        if isinstance(used, bool) or not isinstance(used, (int, float)) or not math.isfinite(used):
            continue
        reset = _parse_iso(window.get("resetsAt"))
        current = [seen, seen, used, int(reset.timestamp()) if reset else None]
        runs = history.setdefault(key, [])
        if runs:
            last = runs[-1]
            if seen < last[1]:
                continue
            same_reset = (last[3] is None) == (current[3] is None) and (
                last[3] is None or abs(current[3] - last[3]) <= HISTORY_CYCLE_SHIFT_SECONDS)
            if used == last[2] and same_reset and seen - last[1] <= HISTORY_GAP_SECONDS:
                last[1] = seen
                continue
        runs.append(current)


def _usage_history_prune(history: dict[str, list[list]], now: _dt.datetime) -> None:
    """Keep runs inside the longest lookback plus the run in effect at its start."""
    start = int(now.timestamp()) - max(RECENT_LOOKBACK_HOURS.values()) * 3600
    for key in list(history):
        runs = history[key]
        if runs[-1][1] < start:
            del history[key]
            continue
        first = 0
        while first + 1 < len(runs) and runs[first + 1][0] <= start:
            first += 1
        del runs[:first]


def _recent_consumption(runs: list[list], hours: int, now: _dt.datetime) -> dict | None:
    """Percent of the window consumed in the last `hours`, summed across resets.

    Within one cycle only increases count; after a reset the new cycle's usage
    counts in full. A change across an unobserved gap that spans the lookback
    start cannot be placed on either side, so it is left out; a change across
    an ordinary poll interval spanning it counts in full.
    """
    now_ts = int(now.timestamp())
    start = now_ts - hours * 3600
    if not runs or runs[-1][1] < start:
        return None
    base = 0
    while base + 1 < len(runs) and runs[base + 1][0] <= start:
        base += 1
    window = runs[base:]
    straddles = (window[0][1] < start and len(window) > 1
                 and window[1][0] - window[0][1] > HISTORY_GAP_SECONDS)
    consumed = 0.0
    for index, (previous, current) in enumerate(zip(window, window[1:])):
        if index == 0 and straddles:
            continue
        if _usage_history_new_cycle(previous, current):
            consumed += current[2]
        else:
            consumed += max(0.0, current[2] - previous[2])
    return {"hours": hours, "consumedPercent": round(consumed, 2)}


def _attach_recent_usage(records: list[dict], history: dict[str, list[list]], now: _dt.datetime) -> None:
    for record in records:
        if record.get("ok") is not True:
            continue
        for key, window in _usage_history_windows(record):
            recent = _recent_consumption(
                history.get(key, []), RECENT_LOOKBACK_HOURS[window["windowMinutes"]], now,
            )
            if recent is not None:
                window["recent"] = recent


def _last_known(record: dict, error: str = "") -> dict:
    return {**record, "stale": True, "refreshError": error}


def _merge_usage_results(results: list[dict], cached: list[dict], now: _dt.datetime) -> list[dict]:
    """A returned account roster replaces the old one, except matching failures."""
    def account(record):
        return (record.get("accountEmail") or "").strip().casefold()

    if len(results) == 1 and not results[0].get("ok") and not account(results[0]) and cached:
        message = results[0].get("error", {}).get("message") or "Refresh failed"
        merged = [_last_known(record, message) for record in cached]
    else:
        merged = []
        for record in results:
            if record.get("ok") and record.get("stale") is True:
                # The source itself served a last-known reading.
                merged.append(record)
                continue
            if record.get("ok"):
                fresh = {key: value for key, value in record.items()
                         if key not in ("stale", "cachedAt", "refreshError")}
                measured = _parse_iso(fresh.get("updatedAt")) or now
                fresh["updatedAt"] = measured.isoformat()
                merged.append(fresh)
                continue
            matches = [old for old in cached if account(old) == account(record)]
            if len(matches) == 1:
                message = record.get("error", {}).get("message") or "Refresh failed"
                merged.append(_last_known(matches[0], message))
            else:
                merged.append(record)
    for record in merged:
        record["accountCount"] = len(merged)
    return merged


def _forecast_eta(
    last_reset: _dt.datetime | None,
    median_days: float | None,
    start_hour: int | None,
    end_hour: int | None,
    zone: str,
    now: _dt.datetime,
) -> _dt.datetime | None:
    """Project the next reset from the last one plus the observed cadence.

    The window hours are wall-clock hours in `zone`; an unknown zone skips
    the window snap rather than misplacing it by the zone offset.
    """
    if last_reset is None or median_days is None or median_days <= 0:
        return None
    try:
        tz = ZoneInfo(zone or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        tz = None
    projected = last_reset + _dt.timedelta(days=median_days)
    eta = projected
    if tz is not None and start_hour is not None and end_hour is not None:
        local = projected.astimezone(tz)
        # Roll in the window zone so a day across a DST change keeps the hour.
        eta = local
        start_at = local.replace(hour=start_hour, minute=0, second=0, microsecond=0)
        end_at = start_at.replace(hour=end_hour)
        if end_hour <= start_hour:
            end_at += _dt.timedelta(days=1)
        if local < start_at:
            eta = start_at
        elif local >= end_at:
            eta = start_at + _dt.timedelta(days=1)
    # Keep the displayed instant in the future by rolling it forward one day
    # at a time when the projection has already elapsed.
    guard = 0
    while eta <= now and guard < 400:
        eta += _dt.timedelta(days=1)
        guard += 1
    return eta.astimezone(_dt.timezone.utc)


def _forecast_cache_read(max_age: float | None) -> dict | None:
    try:
        with open(_state_file(FORECAST_CACHE_NAME), "r", encoding="utf-8") as fh:
            cached = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(cached, dict):
        return None
    payload = cached.get("forecast")
    if not isinstance(payload, dict):
        return None
    cached_at = _as_float(cached.get("cachedAt")) or 0.0
    if max_age is not None and (time.time() - cached_at) > max_age:
        return None
    return payload


def _forecast_cache_write(payload: dict) -> None:
    path = _state_file(FORECAST_CACHE_NAME)
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"cachedAt": time.time(), "forecast": payload}, fh)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        pass


def _normalize_forecast(data: dict) -> dict:
    def _obj(key: str) -> dict:
        value = data.get(key)
        return value if isinstance(value, dict) else {}

    probs = _obj("probabilities")
    window = _obj("time_window")
    cadence = _obj("cadence")

    def _percent(rounded_key: str, raw_key: str) -> float | None:
        rounded = _as_float(probs.get(rounded_key))
        if rounded is not None:
            return rounded
        raw = _as_float(probs.get(raw_key))
        return raw * 100.0 if raw is not None else None

    now = _dt.datetime.now(_dt.timezone.utc)
    last_reset = _parse_iso(data.get("last_reset_at"))
    median_days = _as_float(cadence.get("recent_median_days"))
    if (
        last_reset is None
        or median_days is None
        or median_days <= 0
        or median_days > FORECAST_MAX_MEDIAN_DAYS
    ):
        raise ValueError("forecast missing a valid reset cadence")
    window_start_hour = _as_hour(window.get("start_hour"))
    window_end_hour = _as_hour(window.get("end_hour"))
    eta = _forecast_eta(
        last_reset, median_days, window_start_hour, window_end_hour,
        str(window.get("timezone") or ""), now,
    )
    # Announcements outlive the reset they promise when it is delivered as a
    # banked reset, which never moves `last_reset_at`. Once one lapses, its
    # post also settles every earlier alert and hint.
    cutoff = last_reset
    for key, at_key in (("official_signal", "at"), ("latest_alert", "source_at")):
        raw = data.get(key)
        if isinstance(raw, dict) and _announcement_lapsed(raw, now):
            at = _parse_iso(raw.get(at_key))
            if at is not None and at > cutoff:
                cutoff = at
    # The latest alert only explains the *next* reset when it postdates the
    # cutoff; otherwise it announced a reset already recorded or settled.
    alert = data.get("latest_alert")
    alert_summary = None
    if isinstance(alert, dict):
        alert_at = _parse_iso(alert.get("source_at"))
        summary = alert.get("summary")
        if (
            alert_at is not None
            and alert_at > cutoff
            and isinstance(summary, str)
            and summary.strip()
        ):
            alert_summary = summary.strip()
    signal = _forecast_signal(data, cutoff, now)
    hint = None
    raw_hint = data.get("latest_hint")
    if isinstance(raw_hint, dict):
        hint_at = _parse_iso(raw_hint.get("at"))
        quote = raw_hint.get("quote")
        if hint_at is not None and hint_at > cutoff and isinstance(quote, str) and quote.strip():
            hint = {"at": hint_at.isoformat(), "quote": quote.strip()}
    wait = None
    raw_wait = _obj("wait_comparison")
    wait_days = _as_float(raw_wait.get("wait_days"))
    if wait_days is not None and wait_days >= 0:
        share = _as_float(raw_wait.get("shorter_share"))
        wait = {
            "days": wait_days,
            "shorterShare": share if share is not None and 0 <= share <= 1 else None,
        }
    return {
        "ok": True,
        "stale": False,
        "expectedAt": eta.isoformat() if eta is not None else None,
        "windowStartHour": window_start_hour,
        "windowEndHour": window_end_hour,
        "signal": signal,
        "hint": hint,
        "wait": wait,
        "incident": None,
        "prob24h": _percent("rounded_24h", "raw_24h"),
        "prob48h": _percent("rounded_48h", "raw_48h"),
        "confidence": str(data.get("confidence") or ""),
        "alertSummary": alert_summary,
        "error": None,
    }


def _announcement_deadline(raw: dict) -> _dt.datetime | None:
    """The announced deadline. Sites write "end of day" as 06:59:59.999, so a
    deadline in the last second of a minute is rounded up to the minute."""
    window = raw.get("window")
    window = window if isinstance(window, dict) else {}
    deadline = _parse_iso(window.get("target_at")) or _parse_iso(window.get("end_at"))
    if deadline is not None and deadline.second == 59:
        deadline = deadline.replace(second=0, microsecond=0) + _dt.timedelta(minutes=1)
    return deadline


def _announcement_lapsed(raw: dict, now: _dt.datetime) -> bool:
    deadline = _announcement_deadline(raw)
    return raw.get("state") == "expired" or (deadline is not None and deadline <= now)


def _forecast_signal(
    data: dict, cutoff: _dt.datetime, now: _dt.datetime
) -> dict | None:
    """An announced reset (e.g. a dated commitment on X) that postdates the
    cutoff and whose window is still open. `official_signal` is the scored
    announcement; `latest_alert` carries the same window when the site has
    only an alert. None in plain model mode or once the announcement lapsed.
    """
    for key, at_key in (("official_signal", "at"), ("latest_alert", "source_at")):
        raw = data.get(key)
        if not isinstance(raw, dict) or _announcement_lapsed(raw, now):
            continue
        at = _parse_iso(raw.get(at_key))
        if at is None or at <= cutoff:
            continue
        deadline = _announcement_deadline(raw)
        score = raw.get("score")
        percent = None
        if isinstance(score, dict):
            percent = _as_float(score.get("value"))
        elif score is not None:
            percent = _as_float(score)
        if percent is None:
            probs = data.get("probabilities")
            if isinstance(probs, dict):
                percent = _as_float(probs.get("signal_percent"))
        if percent is not None and not 0 <= percent <= 100:
            percent = None
        return {
            "percent": percent,
            "deadlineAt": deadline.isoformat() if deadline is not None else None,
        }
    return None


def _http_worker() -> int:
    """Private HTTP worker: request on stdin, bounded body on stdout."""
    try:
        request_data = json.load(sys.stdin)
        headers = {"User-Agent": "codexbar-agentview", "Accept": "application/json"}
        headers.update(request_data.get("headers") or {})
        request = urllib.request.Request(request_data["url"], headers=headers)
        with urllib.request.urlopen(
            request, timeout=request_data["timeout"]
        ) as response:
            body = response.read(FORECAST_MAX_BODY_BYTES + 1)
        if len(body) > FORECAST_MAX_BODY_BYTES:
            sys.stdout.write("response exceeds the body size limit")
            return 1
        sys.stdout.buffer.write(body)
        return 0
    except Exception:  # noqa: BLE001 - never echo URLs or credentials
        sys.stdout.write("request failed")
        return 1


def _read_http_body(url: str, timeout: float, headers: dict | None = None) -> bytes:
    # Socket timeouts cannot stop a slow-drip response or bound DNS lookup.
    # Exec a disposable worker, including startup in its wall-clock budget.
    # Headers travel over stdin so credentials never reach argv.
    deadline = time.monotonic() + timeout
    with subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--http-worker"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    ) as proc:
        try:
            body, _ = proc.communicate(
                json.dumps(
                    {"url": url, "timeout": timeout, "headers": headers or {}}
                ).encode("utf-8"),
                timeout=max(0.0, deadline - time.monotonic()),
            )
        except BaseException:
            proc.kill()
            proc.communicate()
            raise
    if proc.returncode != 0:
        raise ValueError(body.decode("utf-8", "replace") or "http worker failed")
    return body


def _forecast_failure(exc: Exception) -> dict:
    stale = _forecast_cache_read(None)
    if stale is not None and stale.get("ok") is True:
        return dict(stale, stale=True)
    return {
        "ok": False,
        "stale": False,
        "expectedAt": None,
        "error": {"code": "forecast_unavailable", "message": str(exc)},
    }


def _normalize_incident(data: dict) -> dict | None:
    """Open Codex incident from codex-reset.com's status feed, else None."""
    current = data.get("current")
    if not isinstance(current, dict):
        return None
    active = current.get("active_incident")
    codex_status = str(current.get("codex") or "")
    open_incident = (
        isinstance(active, dict)
        or current.get("degraded") is True
        or (codex_status != "" and codex_status != "operational")
    )
    if not open_incident:
        return None
    surfaces = current.get("surfaces")
    degraded = [
        str(surface.get("label") or surface.get("id") or "")
        for surface in (surfaces if isinstance(surfaces, list) else [])
        if isinstance(surface, dict) and surface.get("status") not in (None, "operational")
    ]
    return {"open": True, "surfaces": [label for label in degraded if label]}


def _fetch_incident(forecast_url: str, timeout: float) -> dict | None:
    """Status feed is best effort: any failure just means no incident line."""
    try:
        parts = urllib.parse.urlsplit(forecast_url)
        url = urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, FORECAST_STATUS_PATH, "", "")
        )
        body = _read_http_body(url, timeout).decode("utf-8", "replace").strip()
        data = json.loads(body) if body else None
        return _normalize_incident(data) if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001 - the endpoint cannot break usage
        return None


def _fetch_forecast(url: str, timeout: float) -> dict:
    """Fetch and normalize the reset forecast without raising."""
    try:
        fresh = _forecast_cache_read(FORECAST_CACHE_TTL)
        if fresh is not None:
            return fresh
        body = _read_http_body(url, timeout).decode("utf-8", "replace").strip()
        if not body:
            raise ValueError("empty response body")
        data = json.loads(body)
        if not isinstance(data, dict):
            raise ValueError("unexpected response shape")
        payload = _normalize_forecast(data)
    except Exception as exc:  # noqa: BLE001 - the endpoint cannot break usage
        return _forecast_failure(exc)
    payload["incident"] = _fetch_incident(url, min(timeout, FORECAST_STATUS_TIMEOUT))
    _forecast_cache_write(payload)
    return payload


def _positive_seconds(value: str) -> float:
    seconds = _as_float(value)
    if seconds is None or seconds <= 0:
        raise argparse.ArgumentTypeError("must be finite and positive")
    return seconds


def _claim_fetch(min_interval: float | None) -> bool:
    """Stamp the shared fetch claim unless a fetch started under
    `min_interval` seconds ago. The flock makes check and stamp atomic across
    processes. An unusable claim file never blocks a fetch."""
    path = _state_file(FETCH_CLAIM_NAME)
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return True
    with os.fdopen(fd, "r+", encoding="utf-8") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            now = time.time()
            last = _as_float(fh.read().strip())
            if min_interval is not None and last is not None and 0 <= now - last < min_interval:
                return False
            fh.seek(0)
            fh.truncate()
            fh.write(f"{now}\n")
        except (OSError, ValueError):
            pass
    return True


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cli-path", required=True)
    parser.add_argument(
        "--providers",
        default="codex,claude,zai,opencodego,openrouter,kilo,typesafe",
        help="Comma-separated provider ids to query.",
    )
    parser.add_argument("--timeout", type=_positive_seconds, default=30.0)
    parser.add_argument(
        "--forecast-url",
        default=None,
        help="Fetch the Codex reset forecast from this URL.",
    )
    parser.add_argument(
        "--cache-only", action="store_true",
        help="Read last-known usage without invoking the CLI or network.",
    )
    parser.add_argument(
        "--min-interval", type=_positive_seconds, default=None,
        help="Return cached usage when another fetch started this many seconds ago.",
    )
    args = parser.parse_args(argv)
    state_paths.migrate()

    cli = _expand(args.cli_path)
    providers = list(dict.fromkeys(p.strip() for p in args.providers.split(",") if p.strip()))
    now = _dt.datetime.now(_dt.timezone.utc)
    scopes = _usage_cache_scopes(cli, providers)
    cached = _usage_cache_read(scopes, now)
    restored = [
        _last_known(record, record.get("refreshError", ""))
        for p in providers for record in cached.get(p, [])
    ]
    out = {
        "updatedAt": max((r["updatedAt"] for r in restored), default=None),
        "providers": restored,
        "fatal": None,
        "forecast": None,
        # Local files only: a cache read still drops a status omp no longer writes.
        **_rotations(providers, None),
        "cliVersion": None,
    }
    cli_missing = not (os.path.isfile(cli) and os.access(cli, os.X_OK)) and not shutil.which(cli)
    cache_only = args.cache_only or not _claim_fetch(args.min_interval)
    if cache_only:
        _attach_recent_usage(restored, _usage_history_entries(_usage_cache_payload(), now), now)
        out["cacheOnly"] = True
        json.dump(out, sys.stdout)
        sys.stdout.write("\n")
        return 0

    # Without the CLI, a running Meridian still serves Claude and ChatGPT seats.
    meridian = _Meridian()
    results: list[dict] = []
    forecast_future = None
    with ThreadPoolExecutor(
        max_workers=max(1, len(providers) + int(bool(args.forecast_url)))
    ) as pool:
        futures = {
            pool.submit(_fetch_provider, cli, p, args.timeout, meridian): p for p in providers
        }
        if args.forecast_url:
            forecast_future = pool.submit(
                _fetch_forecast, args.forecast_url, FORECAST_TIMEOUT
            )
        for fut in as_completed(futures):
            try:
                results.extend(fut.result())
            except Exception as exc:  # noqa: BLE001 - isolate provider failures
                provider = futures[fut]
                results.append(_result_error(provider, "provider", str(exc)))
    served = any(record.get("ok") for record in results)

    forecast = None
    if forecast_future is not None:
        try:
            forecast = forecast_future.result()
        except Exception as exc:  # noqa: BLE001 - keep healthy provider results
            forecast = _forecast_failure(exc)

    now = _dt.datetime.now(_dt.timezone.utc)
    # A CLI request may refresh tokens or outlive an account/config switch.
    # Never publish its results or old fallback under a different identity.
    current_scopes = _usage_cache_scopes(cli, providers)
    cached = _usage_cache_read(current_scopes, now)
    merged = []
    for provider in providers:
        if scopes.get(provider) != current_scopes.get(provider):
            records = [_result_error(
                provider, "credential_scope_changed",
                "Provider credentials or configuration changed during refresh; refresh again.",
            )]
        else:
            records = _merge_usage_results(
                [record for record in results if record["id"] == provider],
                cached.get(provider, []), now,
            )
        for record in records:
            record.pop("sourceScope", None)
            if provider in current_scopes:
                record["sourceScope"] = current_scopes[provider]
        merged.extend(records)
    results = merged
    _attach_recent_usage(results, _usage_cache_write(current_scopes, results, now), now)

    out = {
        "updatedAt": max((r["updatedAt"] for r in results if r.get("updatedAt")), default=None),
        "providers": results,
        "fatal": None,
        "forecast": forecast,
        **_rotations(providers, meridian),
        "cliVersion": _cli_version(cli),
    }
    if cli_missing and not served:
        message = f"codexbar CLI not found or not executable: {cli}"
        if {"claude", "codex"} & set(providers) and meridian.snapshot() is None:
            message += "; Meridian is not running either"
        out["fatal"] = {"code": "cli_missing", "message": message}
    json.dump(out, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--http-worker"]:
        sys.exit(_http_worker())
    sys.exit(main(sys.argv[1:]))

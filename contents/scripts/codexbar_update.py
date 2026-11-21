#!/usr/bin/env python3
"""Whether a newer CodexBar AgentView release exists, and how this copy updates to it.

    codexbar_update.py check [--max-age SECONDS]
    codexbar_update.py update [--apply]
    codexbar_update.py reload

`check` asks GitHub for the latest release, at most once a day (once an hour
after a failure), and prints

    {"current", "latest", "newer", "url", "checkedAt", "error"?,
     "update": {"possible", "reason"?, "command"}}

The request carries a generic User-Agent and nothing about the user. Its answer
is cached in the state directory (`update_check.json`), which several pollers
share under a lock, so a machine makes one request per interval however many
Tern daemons ask. CODEXBAR_UPDATE_URL replaces the GitHub URL (tests).

`update` prints the same; `update --apply` moves this checkout's branch forward
to the release tag with `git merge --ff-only` and refuses, exiting 1, on
anything else: uncommitted changes, a detached HEAD, a branch the tag doesn't
fast-forward. It never stashes, resets or discards. `reload` asks Tern to load
the plugin again from the updated files.
"""
from __future__ import annotations

import argparse
import contextlib
import http.client
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import state_paths

REPO = "materemias/codexbar-agentview"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_URL = f"https://github.com/{REPO}/releases"
CLONE_URL = f"https://github.com/{REPO}.git"
URL_ENV = "CODEXBAR_UPDATE_URL"
PLUGIN_ID = "codexbar-agentview"
# The checkout this script runs from: contents/scripts/ sits two levels down.
CHECKOUT = Path(__file__).resolve().parents[2]
CACHE_NAME = "update_check.json"
SUCCESS_TTL = 24 * 60 * 60
FAILURE_TTL = 60 * 60
HTTP_TIMEOUT = 10
GIT_TIMEOUT = 30
MAX_ANSWER = 1 << 20
_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
_MANIFEST_VERSION = re.compile(r'^version\s*=\s*"([^"]*)"', re.M)


def parse_version(text: object) -> tuple[int, int, int] | None:
    """`X.Y.Z` (or `vX.Y.Z`) as numbers; None for anything else, pre-releases included."""
    found = _VERSION.match(text.strip()) if isinstance(text, str) else None
    return (int(found[1]), int(found[2]), int(found[3])) if found else None


def installed_version(checkout: Path = CHECKOUT) -> str:
    """The `version` of tern/plugin.toml's top table, or "" when it has none."""
    try:
        text = (checkout / "tern" / "plugin.toml").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""
    top = re.split(r"^\s*\[", text, maxsplit=1, flags=re.M)[0]
    found = _MANIFEST_VERSION.search(top)
    return found[1] if found and parse_version(found[1]) else ""


def is_newer(latest: str, current: str) -> bool:
    """Whether release `latest` is past `current`; a checkout ahead of it (development) is not behind."""
    new, old = parse_version(latest), parse_version(current)
    return new is not None and old is not None and new > old


def fetch_latest(url: str) -> dict:
    """The latest release as {"latest", "url"}; raises on any failure or an answer that isn't one."""
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json", "User-Agent": "codexbar-agentview",
    })
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        body = response.read(MAX_ANSWER + 1)
    if len(body) > MAX_ANSWER:
        raise ValueError("the release answer is too large")
    data = json.loads(body)
    if not isinstance(data, dict) or data.get("draft") or data.get("prerelease"):
        raise ValueError("no published release")
    tag = data.get("tag_name")
    version = parse_version(tag)
    if version is None:
        raise ValueError(f"release tag {tag!r} isn't a version")
    page = data.get("html_url")
    return {
        "latest": ".".join(map(str, version)),
        # The page opens in a browser: only GitHub's own.
        "url": page if isinstance(page, str) and page.startswith("https://github.com/") else RELEASES_URL,
    }


def _read_cache(path: Path) -> dict:
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return cached if isinstance(cached, dict) else {}


def _fresh(cached: dict, max_age: float, now: float) -> bool:
    checked = cached.get("checkedAt")
    if not isinstance(checked, (int, float)) or isinstance(checked, bool) or checked > now:
        return False  # never checked, or a clock set back since
    # A failure waits its hour even for --max-age 0: an unreachable GitHub isn't asked again sooner.
    ttl = FAILURE_TTL if cached.get("error") else max_age
    return now - checked < ttl


@contextlib.contextmanager
def held_lock():
    """The state directory's update lock: one check or update at a time across every poller."""
    directory = state_paths.state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / "update_check.lock", "a", encoding="utf-8") as held:
        state_paths.make_private(held.fileno())
        state_paths.lock(held)
        yield


def latest_release(max_age: float = SUCCESS_TTL, now: float | None = None) -> tuple[dict, bool]:
    """The cached answer when fresh enough, else GitHub's, cached for the next caller; and
    whether this call asked GitHub.

    Callers hold `held_lock()`, so concurrent ones wait for one request and
    then read its answer. A failure keeps the last release found, so a newer
    one stays known while GitHub is unreachable."""
    path = state_paths.state_dir() / CACHE_NAME
    now = time.time() if now is None else now
    cached = _read_cache(path)
    if _fresh(cached, max_age, now):
        return cached, False
    answer: dict = {"checkedAt": int(now)}
    try:
        answer.update(fetch_latest(os.environ.get(URL_ENV) or API_URL))
    except urllib.error.HTTPError as error:
        answer["error"] = f"GitHub answered {error.code} {error.reason}"
    except (urllib.error.URLError, http.client.HTTPException, OSError) as error:
        # A refused connection reads "[Errno 111] Connection refused", not "<urlopen error …>".
        answer["error"] = f"Couldn't reach GitHub: {getattr(error, 'reason', error)}"
    except ValueError as error:
        answer["error"] = f"GitHub's answer isn't a release: {error}"
    if "error" in answer and parse_version(cached.get("latest")) and isinstance(cached.get("url"), str):
        answer["latest"], answer["url"] = cached["latest"], cached["url"]
    state_paths.write_file(path, json.dumps(answer), durable=False)
    return answer, True


def _quote(path: Path) -> str:
    return subprocess.list2cmdline([str(path)]) if os.name == "nt" else shlex.quote(str(path))


def _then(first: str, second: str) -> str:
    """`second` once `first` succeeded, in the user's shell: PowerShell 5.1 on Windows has no `&&`."""
    return f"{first}; if ($?) {{ {second} }}" if os.name == "nt" else f"{first} && {second}"


def _git(checkout: Path, *args: str) -> subprocess.CompletedProcess:
    # No prompt for credentials or a key's passphrase: nobody sees one, and it would hang until the timeout.
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    argv = ["git", "-C", str(checkout), *args]
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=GIT_TIMEOUT, check=False, env=env)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "", f"git {args[0]} took over {GIT_TIMEOUT} s")
    except OSError as error:
        return subprocess.CompletedProcess(argv, 127, "", f"git: {error}")


def _first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def update_plan(latest: str, checkout: Path = CHECKOUT, fetch: bool = True) -> dict:
    """How `checkout` gets to release `latest`: {"possible", "reason"?, "command", "branch"?}.

    Possible only for a git checkout of its own, without uncommitted changes
    to tracked files, on a branch that release tag `v<latest>` fast-forwards.
    Untracked files may stay: a merge that would overwrite one stops itself
    before changing anything. With `fetch`, origin's tags are fetched when the
    tag is missing; without, a missing tag counts as possible, for `--apply`
    to fetch and judge. `command` is what the user would run themselves."""
    tag = f"v{latest}"
    where = _quote(checkout)
    merge = _then(f"git -C {where} fetch --tags origin", f"git -C {where} merge --ff-only {tag}")
    top = _git(checkout, "rev-parse", "--show-toplevel")
    if top.returncode != 0 or not top.stdout.strip() or Path(top.stdout.strip()).resolve() != checkout.resolve():
        return {"possible": False, "reason": f"{checkout} isn't a git checkout, so it can't update itself.",
                "command": f"git clone {CLONE_URL}"}
    status = _git(checkout, "status", "--porcelain", "--untracked-files=no")
    if status.returncode != 0:
        return {"possible": False, "reason": f"git status failed: {_first_line(status.stderr)}", "command": merge}
    if status.stdout.strip():
        return {"possible": False, "reason": f"{checkout} has uncommitted changes. Commit or stash them, then update.",
                "command": merge}
    branch = _git(checkout, "symbolic-ref", "-q", "--short", "HEAD").stdout.strip()
    if not branch:
        return {"possible": False, "reason": "HEAD is detached, so there is no branch to move forward.",
                "command": _then(f"git -C {where} fetch --tags origin", f"git -C {where} checkout {tag}")}
    ref = f"refs/tags/{tag}"
    if _git(checkout, "rev-parse", "-q", "--verify", f"{ref}^{{commit}}").returncode != 0:
        if not fetch:
            return {"possible": True, "command": merge, "branch": branch}
        fetched = _git(checkout, "fetch", "--tags", "origin")
        if fetched.returncode != 0:
            return {"possible": False, "reason": f"git fetch failed: {_first_line(fetched.stderr)}", "command": merge}
        if _git(checkout, "rev-parse", "-q", "--verify", f"{ref}^{{commit}}").returncode != 0:
            return {"possible": False, "reason": f"origin has no tag {tag}.",
                    "command": _then(f"git -C {where} fetch {CLONE_URL} tag {tag}", f"git -C {where} merge --ff-only {tag}")}
    if _git(checkout, "merge-base", "--is-ancestor", "HEAD", ref).returncode == 0:
        return {"possible": True, "command": merge, "branch": branch}
    if _git(checkout, "merge-base", "--is-ancestor", ref, "HEAD").returncode == 0:
        return {"possible": False, "reason": f"Branch {branch} already contains {tag}.", "command": ""}
    return {"possible": False, "reason": f"Branch {branch} has commits {tag} doesn't, so it can't fast-forward.",
            "command": f"git -C {where} merge {tag}"}


def _status(max_age: float, checkout: Path, fetch: bool = False) -> dict:
    """`fetch`: fetch a missing tag whatever happens. Else only the call that asked GitHub
    fetches, so a missing tag costs one fetch a day, never one per check."""
    release, requested = latest_release(max_age)
    current = installed_version(checkout)
    latest = release.get("latest") if parse_version(release.get("latest")) else None
    newer = bool(latest) and is_newer(latest, current)
    answer = {
        "current": current, "latest": latest, "newer": newer,
        "url": release.get("url") or RELEASES_URL, "checkedAt": release.get("checkedAt"),
        "update": update_plan(latest, checkout, fetch or requested) if newer
        else {"possible": False, "reason": "Up to date.", "command": ""},
    }
    if release.get("error"):
        answer["error"] = release["error"]
    return answer


def status(max_age: float = SUCCESS_TTL, checkout: Path = CHECKOUT) -> dict:
    """What `check` prints. The plan's fetch runs under the lock too: concurrent fetches clash on refs."""
    with held_lock():
        return _status(max_age, checkout)


def apply_update(checkout: Path = CHECKOUT) -> tuple[int, dict]:
    """Fast-forwards `checkout` to the latest release, or refuses."""
    with held_lock():
        return _apply(checkout)


def _apply(checkout: Path) -> tuple[int, dict]:
    found = _status(SUCCESS_TTL, checkout, fetch=True)
    if not found["newer"]:
        reason = found.get("error") or f"{found['current'] or 'This copy'} is the latest release."
        return 1, {"ok": False, "error": reason, "command": ""}
    plan = found["update"]
    if not plan["possible"]:
        return 1, {"ok": False, "error": plan["reason"], "command": plan["command"]}
    tag = f"v{found['latest']}"
    # A branch moved since the plan was made fails this, never anything worse.
    merged = _git(checkout, "merge", "--ff-only", f"refs/tags/{tag}")
    if merged.returncode != 0:
        return 1, {"ok": False, "error": f"git merge failed: {_first_line(merged.stderr or merged.stdout)}",
                   "command": plan["command"]}
    return 0, {"ok": True, "updated": True, "from": found["current"], "to": found["latest"], "branch": plan["branch"]}


def reload_plugin() -> tuple[int, dict]:
    from codexbar_agents import tern_bin

    try:
        done = subprocess.run([tern_bin() or "tern", "plugin", "reload", PLUGIN_ID], capture_output=True,
                              text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        return 1, {"ok": False, "error": str(error)}
    if done.returncode != 0:
        return 1, {"ok": False, "error": _first_line(done.stderr or done.stdout) or f"exit {done.returncode}"}
    return 0, {"ok": True}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="whether a newer release exists")
    check.add_argument("--max-age", type=float, default=SUCCESS_TTL,
                       help="reuse a cached answer younger than this many seconds (default a day)")
    update = commands.add_parser("update", help="how this checkout updates; --apply does it")
    update.add_argument("--apply", action="store_true")
    commands.add_parser("reload", help="load the Tern plugin again")
    args = parser.parse_args(argv)
    state_paths.migrate()
    if args.command == "reload":
        code, answer = reload_plugin()
    elif args.command == "update" and args.apply:
        code, answer = apply_update()
    else:
        code, answer = 0, status(args.max_age if args.command == "check" else SUCCESS_TTL)
    print(json.dumps(answer))
    return code


if __name__ == "__main__":
    sys.exit(main())

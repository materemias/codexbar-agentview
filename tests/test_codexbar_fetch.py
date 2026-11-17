from __future__ import annotations

import contextlib
import datetime as dt
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from fakes import WINDOWS, fake_program


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "contents"
    / "scripts"
    / "codexbar_fetch.py"
)
sys.path.insert(0, str(SCRIPT_PATH.parent))
SPEC = importlib.util.spec_from_file_location("codexbar_fetch", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
fetch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fetch)

# Windows has no `true`: a batch file beside this one stands in.
TRUE_EXECUTABLE = shutil.which("true") or (str(Path(__file__).with_name("true.cmd")) if WINDOWS else None)
assert TRUE_EXECUTABLE is not None, "provider tests require the true executable"
try:
    fetch.ZoneInfo("America/Los_Angeles")
    HAS_TZ_DATABASE = True
except fetch.ZoneInfoNotFoundError:
    HAS_TZ_DATABASE = False


@contextlib.contextmanager
def forecast_server(body: bytes, interval: float = 0):
    stopped = threading.Event()
    requested = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requested.set()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                if interval:
                    for byte in body:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        if stopped.wait(interval):
                            break
                else:
                    self.wfile.write(body)
            except OSError:
                pass

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/forecast", requested
    finally:
        stopped.set()
        server.shutdown()
        server.server_close()
        thread.join()


def utc(*parts: int) -> dt.datetime:
    return dt.datetime(*parts, tzinfo=dt.timezone.utc)


MERIDIAN = "http://127.0.0.1:3456"  # MERIDIAN_PORT in IsolatedUsageTestCase


def serve_meridian(responses: dict, other=None):
    """Patch `_read_http_body` to answer Meridian's endpoints from `responses`
    and any other URL with `other(url, headers)`. The patch records each call."""
    def http(url, _timeout, headers=None):
        if url.startswith(MERIDIAN):
            return json.dumps(responses[url[len(MERIDIAN):]]).encode()
        if other is None:
            raise AssertionError(f"unexpected request: {url}")
        return other(url, headers)

    return mock.patch.object(fetch, "_read_http_body", side_effect=http)


def run_main(*arguments: str, cli: str = TRUE_EXECUTABLE) -> dict:
    """main's JSON answer; callers patch the sources it reads."""
    output = io.StringIO()
    with mock.patch("sys.stdout", output):
        code = fetch.main(["--cli-path", cli, *arguments])
    if code != 0:
        raise AssertionError(f"main exited {code}")
    return json.loads(output.getvalue())


class IsolatedUsageTestCase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        environment = mock.patch.dict(fetch.os.environ, {
            "HOME": directory.name,
            "USERPROFILE": directory.name,  # the home on Windows
            "XDG_STATE_HOME": str(self.home / ".local" / "state"),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "CODEX_HOME": str(self.home / ".codex"),
            "CLAUDE_CONFIG_DIR": str(self.home / ".claude"),
            "MERIDIAN_PORT": "3456",
        })
        environment.start()
        self.addCleanup(environment.stop)


class ProviderFailureTests(IsolatedUsageTestCase):
    def test_invalid_executable_emits_normalized_snapshot(self) -> None:
        cli = self.home / "invalid-cli"
        cli.write_text("not an executable format\n", encoding="utf-8")
        cli.chmod(0o700)
        payload = run_main("--providers", "kilo", cli=str(cli))
        self.assertIsNone(payload["fatal"])
        self.assertIsNone(payload["cliVersion"])
        record = payload["providers"][0]
        self.assertEqual((record["id"], record["ok"], record["error"]["code"]), ("kilo", False, "cli_error"))

    def test_malformed_provider_keeps_healthy_results(self) -> None:
        def cli_result(command, **kwargs):
            if "--version" in command:
                return subprocess.CompletedProcess(command, 0, "CodexBar 0.56.3", "")
            provider = command[command.index("--provider") + 1]
            primary = {"usedPercent": 12}
            if provider == "kilo":
                primary["resetDescription"] = 42
            return subprocess.CompletedProcess(
                command, 0, json.dumps({"usage": {"primary": primary}}), ""
            )

        with (
            mock.patch.object(fetch.subprocess, "run", side_effect=cli_result),
            # A running Meridian's ChatGPT seats would add Codex accounts.
            mock.patch.object(fetch, "_read_http_body", side_effect=OSError("Meridian offline")),
        ):
            payload = run_main("--providers", "kilo,codex")
        self.assertIsNone(payload["fatal"])
        self.assertEqual(payload["cliVersion"], "0.56.3")
        failed, healthy = payload["providers"]
        self.assertEqual(failed["id"], "kilo")
        self.assertFalse(failed["ok"])
        self.assertEqual(failed["error"]["code"], "provider")
        self.assertEqual(healthy["id"], "codex")
        self.assertTrue(healthy["ok"])
        self.assertEqual(healthy["primary"]["usedPercent"], 12)

    def test_timeout_requires_a_finite_positive_number(self) -> None:
        for value in ("0", "-1", "nan", "inf", "-inf", "invalid"):
            with self.subTest(value=value):
                with (
                    mock.patch("sys.stderr", io.StringIO()),
                    self.assertRaises(SystemExit) as raised,
                ):
                    fetch.main(
                        ["--cli-path", TRUE_EXECUTABLE, f"--timeout={value}"]
                    )
                self.assertEqual(raised.exception.code, 2)


class ClaudeAccountTests(IsolatedUsageTestCase):
    """One record per Claude login: a running Meridian's usage, else the CLI per login."""

    def setUp(self) -> None:
        super().setUp()
        self.cli = fake_program(self.home / "codexbar", (
            "import os, sys\n"
            "try:\n"
            "    with open(os.path.join(os.environ['CLAUDE_CONFIG_DIR'], 'usage.json')) as usage:\n"
            "        sys.stdout.write(usage.read())\n"
            "except (KeyError, OSError):\n"
            "    print('{\"provider\":\"claude\",\"error\":{\"message\":\"rate limited\"}}')\n"
        ))
        self.meridian = self.home / ".config" / "meridian"
        self.profile_urls: list[str] = []

    def login(self, name: str, email: str | None = None, cli_email: str | None = None,
              used: float | None = None, plan: tuple[str, str] | None = None) -> Path:
        """A Claude login directory; `plan` is (subscriptionType, rateLimitTier)."""
        directory = self.home / name
        directory.mkdir()
        oauth = {"accessToken": f"token-{name}", "expiresAt": int((time.time() + 3600) * 1000)}
        if plan:
            oauth.update(subscriptionType=plan[0], rateLimitTier=plan[1])
        (directory / ".credentials.json").write_text(json.dumps({"claudeAiOauth": oauth}))
        if email:
            (directory / ".claude.json").write_text(json.dumps(
                {"oauthAccount": {"accountUuid": f"uuid-{email}", "emailAddress": email}}))
        if used is not None:
            (directory / "usage.json").write_text(json.dumps({
                "provider": "claude", "source": "oauth",
                "usage": {"accountEmail": cli_email, "loginMethod": "Claude Max 5x",
                          "primary": {"usedPercent": used, "windowMinutes": 300}},
            }))
        return directory

    def profiles(self, entries: list[tuple[str, Path]], order: list[str]) -> None:
        self.meridian.mkdir(parents=True)
        (self.meridian / "profiles.json").write_text(json.dumps(
            [{"id": name, "claudeConfigDir": str(path)} for name, path in entries]))
        (self.meridian / "settings.json").write_text(json.dumps({"profileOrder": order}))

    def fetch_claude(self) -> list[dict]:
        def http(url, _timeout, headers):
            if url != fetch.CLAUDE_PROFILE_URL:
                raise OSError("offline")
            self.profile_urls.append(headers["Authorization"])
            return json.dumps({"account": {"email": "profile@example.com"}}).encode()

        with mock.patch.object(fetch, "_read_http_body", side_effect=http):
            return fetch._fetch_provider(str(self.cli), "claude", 10)

    def test_stopped_meridian_profiles_are_fetched_in_profile_order_once_per_account(self) -> None:
        team = self.login("team", email="team@example.com", cli_email="team@example.com", used=10)
        max_ = self.login("max", used=20)
        twin = self.login("twin", cli_email="TEAM@example.com", used=99)
        self.profiles([("team", team), ("max", max_), ("twin", twin)], ["max", "team", "twin"])
        records = self.fetch_claude()
        self.assertEqual(
            [(r["accountEmail"], r["primary"]["usedPercent"]) for r in records],
            [("profile@example.com", 20), ("team@example.com", 10)],
        )
        self.assertEqual({r["accountCount"] for r in records}, {2})
        # Only the login without a saved or reported email asks the profile endpoint.
        self.assertEqual(self.profile_urls, ["Bearer token-max"])

    def test_failed_login_still_names_its_account(self) -> None:
        team = self.login("team", email="team@example.com")
        self.profiles([("team", team)], ["team"])
        [record] = self.fetch_claude()
        self.assertFalse(record["ok"])
        self.assertEqual(record["accountEmail"], "team@example.com")

    def test_without_meridian_the_local_login_is_used(self) -> None:
        self.login(".claude", email="local@example.com", cli_email="local@example.com", used=5)
        self.login("ignored", email="other@example.com", cli_email="other@example.com", used=50)
        [record] = self.fetch_claude()
        self.assertEqual((record["accountEmail"], record["primary"]["usedPercent"]),
                         ("local@example.com", 5))

    def test_running_meridian_supplies_every_profile_without_the_cli(self) -> None:
        team = self.login("team")
        now_ms = int(time.time() * 1000)
        responses = {
            "/profiles/list": {"profileOrder": ["max", "team"], "profiles": [
                {"id": "team", "type": "claude-max", "email": "team@example.com",
                 "subscriptionType": "team", "rateLimitTier": "default_claude_max_5x",
                 "credentialDir": str(team)},
                {"id": "gpt", "type": "chatgpt", "email": "gpt@example.com"},
                {"id": "max", "type": "claude-max", "email": "max@example.com",
                 "subscriptionType": "max", "rateLimitTier": "default_claude_max_20x"},
            ]},
            "/v1/usage/quota/all": {"profiles": [
                {"id": "team", "fetchedAt": now_ms, "windows": [
                    {"type": "five_hour", "utilization": 0.07, "resetsAt": now_ms + 3_600_000},
                    {"type": "seven_day", "utilization": 0.19, "resetsAt": now_ms + 86_400_000},
                    {"type": "seven_day_sonnet", "utilization": 0.3, "resetsAt": None},
                    {"type": "seven_day_fable", "utilization": 0.01, "resetsAt": None},
                    {"type": "seven_day_cowork", "utilization": 0.02, "resetsAt": None},
                    {"type": "seven_day_omelette", "utilization": 0.5, "resetsAt": None},
                ]},
                {"id": "max", "windows": None, "failure": {"reason": "rate_limited"}},
            ]},
        }
        anthropic = []

        def ask(url, headers):
            anthropic.append((url, headers["Authorization"]))
            return b'{"cedar_ember": {"eligible": true, "grants": [{"resets_left": 2}]}}'

        with (
            serve_meridian(responses, ask),
            mock.patch.object(fetch.subprocess, "run", side_effect=AssertionError("CLI ran")),
        ):
            unavailable, team_record = fetch._fetch_provider(str(self.cli), "claude", 10)
            # Saved resets share Anthropic's per-token usage limit: the next
            # refresh within the hour reuses the answer.
            _, again = fetch._fetch_provider(str(self.cli), "claude", 10)
        self.assertEqual(
            (unavailable["ok"], unavailable["accountEmail"], unavailable["loginMethod"]),
            (False, "max@example.com", "Claude Max 20x"),
        )
        self.assertIn("rate_limited", unavailable["error"]["message"])
        self.assertEqual(team_record["loginMethod"], "Claude Team")
        # A Team seat on the Max 5x tier pools as five Pro allowances.
        self.assertEqual(team_record["planWeight"], 5)
        self.assertEqual(
            [team_record[slot]["usedPercent"] for slot in ("primary", "secondary", "tertiary")],
            [7, 19, 30],
        )
        self.assertEqual(team_record["primary"]["windowMinutes"], 300)
        self.assertEqual(
            [(extra["id"], extra["title"]) for extra in team_record["extraRateWindows"]],
            [("claude-weekly-scoped-fable", "Fable only"), ("claude-routines", "Daily Routines")],
        )
        self.assertEqual((team_record["resetCredits"]["count"], again["resetCredits"]["count"]), (2, 2))
        self.assertEqual(anthropic, [(fetch.CLAUDE_RESET_URL, "Bearer token-team")])
        self.assertEqual({team_record["accountCount"], unavailable["accountCount"]}, {2})

    def test_failed_saved_resets_ask_waits_like_an_answered_one(self) -> None:
        team = self.login("team")
        now_ms = int(time.time() * 1000)
        responses = {
            "/profiles/list": {"profiles": [
                {"id": "team", "type": "claude-max", "email": "team@example.com",
                 "subscriptionType": "team", "credentialDir": str(team)}]},
            "/v1/usage/quota/all": {"profiles": [
                {"id": "team", "fetchedAt": now_ms,
                 "windows": [{"type": "five_hour", "utilization": 0.1, "resetsAt": None}]}]},
        }
        asks = []

        def ask(url, _headers):
            asks.append(url)
            raise OSError("HTTP Error 429")

        with serve_meridian(responses, ask):
            first = fetch._fetch_provider(str(self.cli), "claude", 10)
            second = fetch._fetch_provider(str(self.cli), "claude", 10)
        self.assertEqual(asks, [fetch.CLAUDE_RESET_URL])
        self.assertEqual([r["ok"] for r in first + second], [True, True])

    def test_meridian_banked_resets_replace_the_anthropic_ask(self) -> None:
        team = self.login("team")
        now_ms = int(time.time() * 1000)
        day = 86_400_000
        banked = {"eligible": True, "ineligibleReason": None, "grants": [
            {"id": "late", "resetsLeft": 2, "expiresAt": now_ms + 9 * day, "usableNow": True},
            {"id": "soon", "resetsLeft": 1, "expiresAt": now_ms + 2 * day, "usableNow": None},
            {"id": "used", "resetsLeft": 0, "expiresAt": now_ms + day},
            {"id": "paused", "resetsLeft": 3, "expiresAt": None, "paused": True},
            {"id": "unusable", "resetsLeft": 3, "expiresAt": None, "usableNow": False},
            {"id": "future", "resetsLeft": 3, "startsAt": now_ms + day, "expiresAt": None},
            {"id": "expired", "resetsLeft": 3, "expiresAt": now_ms - day},
        ]}
        responses = {
            "/profiles/list": {"profiles": [
                {"id": id_, "type": "claude-max", "email": f"{id_}@example.com",
                 "subscriptionType": "team", "credentialDir": str(team)}
                for id_ in ("team", "spent", "unknown")]},
            "/v1/usage/quota/all": {"profiles": [
                {"id": id_, "fetchedAt": now_ms, "bankedResets": value,
                 "windows": [{"type": "five_hour", "utilization": 0.1, "resetsAt": None}]}
                for id_, value in (("team", banked), ("spent", {"eligible": True, "grants": []}),
                                   ("unknown", None))]},
        }

        with serve_meridian(responses) as http:
            team_record, spent, unknown = fetch._fetch_provider(str(self.cli), "claude", 10)
        # Meridian's banked resets answer, even "unknown": Anthropic is not asked.
        self.assertTrue(all(call.args[0].startswith(MERIDIAN) for call in http.call_args_list))
        self.assertEqual(team_record["resetCredits"]["count"], 3)
        self.assertEqual(
            fetch._parse_iso(team_record["resetCredits"]["soonestExpiresAt"]).timestamp(),
            (now_ms + 2 * day) / 1000,
        )
        self.assertEqual(spent["resetCredits"], {"count": 0, "soonestExpiresAt": None})
        self.assertIsNone(unknown["resetCredits"])

    def test_meridian_facts_come_first_and_the_login_fills_what_it_lacks(self) -> None:
        full = self.login("full", email="saved@example.com")
        bare = self.login("bare", email="bare@example.com", plan=("max", "default_claude_max_20x"))
        now_ms = int(time.time() * 1000)
        responses = {
            "/profiles/list": {"profiles": [
                {"id": "full", "type": "claude-max", "email": "meridian@example.com",
                 "subscriptionType": "team", "rateLimitTier": "default_claude_max_5x",
                 "credentialDir": str(full)},
                {"id": "bare", "type": "claude-max", "email": None, "subscriptionType": None,
                 "rateLimitTier": None, "credentialDir": str(bare)},
            ]},
            "/v1/usage/quota/all": {"profiles": [
                {"id": id_, "fetchedAt": now_ms, "bankedResets": {"eligible": True, "grants": []},
                 "windows": [{"type": "five_hour", "utilization": 0.1, "resetsAt": None}]}
                for id_ in ("full", "bare")]},
        }
        with (
            serve_meridian(responses) as http,
            mock.patch.object(fetch.subprocess, "run", side_effect=AssertionError("CLI ran")),
        ):
            records = fetch._fetch_provider(str(self.cli), "claude", 10)
        self.assertEqual(
            [(r["accountEmail"], r["loginMethod"], r["planWeight"]) for r in records],
            [("meridian@example.com", "Claude Team", 5), ("bare@example.com", "Claude Max 20x", 20)])
        # Meridian and the login directories answered everything: no Anthropic call.
        self.assertTrue(all(call.args[0].startswith(MERIDIAN) for call in http.call_args_list))

    def test_meridian_chatgpt_seats_fill_in_codex_accounts_the_cli_cannot_read(self) -> None:
        now_ms = int(time.time() * 1000)
        responses = {
            "/profiles/list": {"profiles": [
                {"id": "shared", "type": "chatgpt", "email": "a@example.com", "subscriptionType": "plus"},
                {"id": "seat", "type": "chatgpt", "email": "b@example.com", "subscriptionType": "pro",
                 "resets": {"available": 2, "expiresAt": [now_ms + 86_400_000, None]}},
                {"id": "free", "type": "chatgpt", "email": "c@example.com", "subscriptionType": "free"},
            ]},
            "/v1/usage/quota/all": {"profiles": [
                {"id": "shared", "type": "chatgpt", "fetchedAt": now_ms,
                 "windows": [{"type": "five_hour", "utilization": 0.9, "resetsAt": None}]},
                {"id": "seat", "type": "chatgpt", "fetchedAt": now_ms, "windows": [
                    {"type": "seven_day", "utilization": 0.25, "resetsAt": now_ms + 3_600_000},
                    {"type": "five_hour", "utilization": 0.5, "resetsAt": None}]},
                {"id": "free", "type": "chatgpt", "fetchedAt": now_ms,
                 "windows": [{"type": "30d", "utilization": 0.1, "resetsAt": None}]},
            ]},
        }

        cli_account = fetch._normalize_record("codex", {"usage": {
            "accountEmail": "A@example.com", "loginMethod": "plus",
            "primary": {"usedPercent": 10, "windowMinutes": 300}}})
        for cli_records, expected in (
            ([cli_account], [("A@example.com", 10), ("b@example.com", 50), ("c@example.com", None)]),
            ([fetch._result_error("codex", "cli_missing", "CLI not found")],
             [("a@example.com", 90), ("b@example.com", 50), ("c@example.com", None)]),
        ):
            with (
                serve_meridian(responses),
                mock.patch.object(fetch, "_fetch_sources", return_value=cli_records),
            ):
                records = fetch._fetch_provider(str(self.cli), "codex", 10)
            self.assertEqual(
                [(r["accountEmail"], (r["primary"] or {}).get("usedPercent")) for r in records], expected)
        _, seat, free = records
        self.assertEqual((seat["loginMethod"], seat["secondary"]["usedPercent"]), ("pro", 25))
        self.assertEqual(seat["resetCredits"]["count"], 2)
        self.assertEqual(
            fetch._parse_iso(seat["resetCredits"]["soonestExpiresAt"]).timestamp(),
            (now_ms + 86_400_000) / 1000)
        self.assertEqual((free["secondary"]["windowMinutes"], free["secondary"]["usedPercent"]),
                         (43200, 10))

    def test_meridian_without_profiles_serves_the_local_login_as_default(self) -> None:
        self.login(".claude", email="local@example.com", plan=("max", "default_claude_max_20x"))
        responses = {
            "/profiles/list": {"profiles": []},
            "/v1/usage/quota/all": {"profiles": [
                {"id": "default", "fetchedAt": int(time.time() * 1000), "bankedResets": None,
                 "windows": [{"type": "five_hour", "utilization": 0.3, "resetsAt": None}]}]},
        }
        with (
            mock.patch.dict(fetch.os.environ, {"MERIDIAN_API_KEY": "secret"}),
            serve_meridian(responses) as http,
            mock.patch.object(fetch.subprocess, "run", side_effect=AssertionError("CLI ran")),
        ):
            [record] = fetch._fetch_provider(str(self.cli), "claude", 10)
        self.assertEqual(
            (record["accountEmail"], record["loginMethod"], record["planWeight"],
             record["primary"]["usedPercent"]),
            ("local@example.com", "Claude Max 20x", 20, 30))
        # Both Meridian requests carry its API key.
        self.assertEqual([call.args[2] for call in http.call_args_list], [{"x-api-key": "secret"}] * 2)

    def test_meridian_reports_a_failed_refresh_once_it_is_news(self) -> None:
        for age_ms, failures, reported in (
            (60_000, 2, True),
            ((fetch.MERIDIAN_QUIET_STALE_SECONDS + 60) * 1000, 1, True),
            # Meridian retries after its own backoff; one 429 on a recent reading is not news.
            (60_000, 1, False),
        ):
            with self.subTest(age_ms=age_ms, failures=failures):
                shutil.rmtree(self.home / ".local" / "state", ignore_errors=True)
                measured = int(time.time() * 1000) - age_ms
                failure = {"reason": "rate_limited", "consecutiveFailures": failures}
                responses = {
                    "/profiles/list": {"profiles": [
                        {"id": "team", "type": "claude-max", "email": "team@example.com",
                         "subscriptionType": "team"}]},
                    "/v1/usage/quota/all": {"profiles": [
                        {"id": "team", "fetchedAt": measured, "stale": True, "failure": failure,
                         "windows": [{"type": "five_hour", "utilization": 0.4, "resetsAt": None}]}]},
                }
                with (
                    serve_meridian(responses),
                    mock.patch.object(fetch.subprocess, "run", side_effect=AssertionError("CLI ran")),
                    mock.patch.object(fetch, "_cli_version", return_value=None),
                ):
                    [record] = run_main("--providers", "claude", cli=str(self.cli))["providers"]
                self.assertEqual((record["ok"], record["stale"], record["primary"]["usedPercent"]),
                                 (True, True, 40))
                self.assertEqual(fetch._parse_iso(record["cachedAt"]).timestamp(), measured / 1000)
                self.assertRegex(record["refreshError"], "rate_limited" if reported else "^$")

    def test_out_of_range_meridian_time_leaves_other_profiles_readable(self) -> None:
        now_ms = int(time.time() * 1000)
        responses = {
            "/profiles/list": {"profiles": [
                {"id": id_, "type": "claude-max", "email": f"{id_}@example.com", "subscriptionType": "max"}
                for id_ in ("bad", "good")]},
            "/v1/usage/quota/all": {"profiles": [
                {"id": "bad", "fetchedAt": 1e20,
                 "bankedResets": {"eligible": True, "grants": [{"resetsLeft": 1, "expiresAt": 1e20}]},
                 "windows": [{"type": "five_hour", "utilization": 0.2, "resetsAt": 1e20}]},
                {"id": "good", "fetchedAt": now_ms, "bankedResets": None,
                 "windows": [{"type": "five_hour", "utilization": 0.3, "resetsAt": now_ms + 3_600_000}]},
            ]},
        }
        with serve_meridian(responses):
            bad, good = fetch._fetch_provider(str(self.cli), "claude", 10)
        self.assertEqual((bad["ok"], bad["primary"]["usedPercent"], bad["primary"]["resetsAt"]),
                         (True, 20, None))
        self.assertEqual(bad["resetCredits"], {"count": 1, "soonestExpiresAt": None})
        self.assertEqual((good["ok"], good["primary"]["usedPercent"]), (True, 30))
        self.assertEqual(fetch._meridian_seat_resets({"available": 1, "expiresAt": [1e20]}),
                         {"count": 1, "soonestExpiresAt": None})

    def test_expired_saved_reset_is_not_served(self) -> None:
        path = Path(fetch._state_file(fetch.CLAUDE_RESET_CACHE_NAME))
        path.parent.mkdir(parents=True, exist_ok=True)
        now = dt.datetime.now(dt.timezone.utc)
        for offset, served in ((-60, False), (3600, True)):
            with self.subTest(offset=offset):
                credits = {"count": 1, "soonestExpiresAt": (now + dt.timedelta(seconds=offset)).isoformat()}
                path.write_text(json.dumps({"a@example.com": {"at": time.time(), "credits": credits}}))
                self.assertEqual(fetch._claude_reset_credits_for("a@example.com", None, False),
                                 credits if served else None)


class UsageCacheTests(IsolatedUsageTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.now = dt.datetime.now(dt.timezone.utc)
        self.measured = self.now - dt.timedelta(minutes=5)
        self.cache_path = self.home / ".local/state/codexbar-agentview/usage_cache.json"

    def record(self, provider="codex", account="a@example.com", used=42) -> dict:
        return fetch._normalize_record(provider, {
            "source": "oauth",
            "usage": {
                "accountEmail": account,
                "loginMethod": "Plus",
                "updatedAt": self.measured.isoformat(),
                "primary": {
                    "usedPercent": used, "windowMinutes": 300,
                    "resetsAt": (self.now + dt.timedelta(hours=2)).isoformat(),
                },
            },
        })

    def snapshot(self, records=None, providers="codex", cache_only=False, cli=TRUE_EXECUTABLE,
                 min_interval=None, forecast_url=None, fetch_provider=None) -> dict:
        """main's answer, offline: `fetch_provider(name)` answers each provider
        fetched, else its `records`. `self.fetched` lists the providers fetched."""
        arguments = ["--providers", providers]
        if cache_only:
            arguments += ["--cache-only", "--forecast-url", "https://example.invalid/forecast"]
        if min_interval is not None:
            arguments += ["--min-interval", str(min_interval)]
        if forecast_url is not None:
            arguments += ["--forecast-url", forecast_url]
        self.fetched: list[str] = []

        def provider(_cli, name, *_):
            self.fetched.append(name)
            if fetch_provider is not None:
                return fetch_provider(name)
            return [record for record in records or [] if record["id"] == name]

        with (
            mock.patch.object(fetch, "_fetch_provider", side_effect=provider),
            mock.patch.object(fetch, "_cli_version", return_value=None),
            mock.patch.object(fetch, "_read_rotation", return_value=None),
            mock.patch.object(fetch.subprocess, "run", side_effect=AssertionError("unexpected subprocess")),
            mock.patch.object(fetch, "_read_http_body", side_effect=AssertionError("unexpected network")),
        ):
            return run_main(*arguments, cli=cli)

    def test_shared_cache_freshness_uses_requesting_cadence_and_grace(self) -> None:
        for age, stale in ((149, False), (150, False), (151, True)):
            with self.subTest(age=age):
                self.measured = self.now - dt.timedelta(seconds=age)
                self.snapshot([self.record(used=95)])
                before = self.cache_path.read_bytes()
                with (
                    mock.patch.object(fetch._dt, "datetime", wraps=dt.datetime) as clock,
                    mock.patch.object(fetch, "_claim_fetch", return_value=[]),
                ):
                    clock.now.return_value = self.now
                    shared = self.snapshot(min_interval=60)
                self.assertTrue(shared["cacheOnly"])
                self.assertTrue(shared["sharedCache"])
                self.assertEqual(shared["providers"][0]["stale"], stale)
                self.assertEqual(self.cache_path.read_bytes(), before)

    def test_explicit_cache_only_does_not_adopt_recent_shared_usage(self) -> None:
        self.measured = self.now
        self.snapshot([self.record(used=95)])
        with mock.patch.object(fetch, "_claim_fetch") as claim:
            restored = self.snapshot(cache_only=True, min_interval=60)
        claim.assert_not_called()
        self.assertTrue(restored["cacheOnly"])
        self.assertNotIn("sharedCache", restored)
        self.assertTrue(restored["providers"][0]["stale"])

    def test_a_claim_covers_only_the_providers_fetched(self) -> None:
        # Another poller refreshed Claude alone just now; this one shows Codex too.
        self.measured = self.now
        self.snapshot([self.record("claude", used=10)], providers="claude", min_interval=60)
        both = self.snapshot([self.record("codex", used=70), self.record("claude", used=99)],
                             providers="codex,claude", min_interval=60)
        self.assertEqual(self.fetched, ["codex"])
        self.assertNotIn("cacheOnly", both)
        readings = {r["id"]: (r["primary"]["usedPercent"], r.get("stale", False))
                    for r in both["providers"]}
        self.assertEqual(readings, {"codex": (70, False), "claude": (10, False)})
        cached = json.loads(self.cache_path.read_text())["providers"]
        self.assertEqual(cached["claude"]["records"][0]["primary"]["usedPercent"], 10)
        # Within the interval every provider comes from the shared cache.
        again = self.snapshot([self.record("codex", used=99)], providers="codex,claude", min_interval=60)
        self.assertEqual(self.fetched, [])
        self.assertTrue(again["cacheOnly"])
        self.assertTrue(again["sharedCache"])
        self.assertEqual([r["primary"]["usedPercent"] for r in again["providers"]], [70, 10])

    def test_a_claim_from_before_per_provider_claims_covers_every_provider(self) -> None:
        claim = self.cache_path.with_name("fetch_claim")
        claim.parent.mkdir(parents=True, exist_ok=True)
        for stamp, shared in ((time.time(), True), (0, False)):
            with self.subTest(shared=shared):
                claim.write_text(f"{stamp}\n")
                answer = self.snapshot([self.record(used=42)], providers="codex", min_interval=60)
                self.assertEqual(answer.get("sharedCache", False), shared)

    def test_a_refused_claim_still_refreshes_the_forecast(self) -> None:
        forecast = {"ok": True, "expectedAt": "2026-11-20T00:00:00Z"}
        with (
            mock.patch.object(fetch, "_claim_fetch", return_value=[]),
            mock.patch.object(fetch, "_fetch_forecast", return_value=forecast) as fetch_forecast,
        ):
            shared = self.snapshot(min_interval=60, forecast_url="https://example.invalid/f")
        self.assertTrue(shared["sharedCache"])
        self.assertEqual(shared["forecast"], forecast)
        fetch_forecast.assert_called_once()

    def test_cached_claude_accounts_keep_their_plan_weight(self) -> None:
        # Tern renders the combined Claude bar from the cache alone.
        heavy = {**self.record("claude", "max@example.com"), "planWeight": 5}
        light = self.record("claude", "pro@example.com")
        self.snapshot([heavy, light], providers="claude")
        restored = self.snapshot(providers="claude", cache_only=True)["providers"]
        self.assertEqual([r.get("planWeight") for r in restored], [5, None])

    def test_accounts_are_listed_by_email_on_fetch_and_cache_read(self) -> None:
        records = [self.record(account="zed@example.com"), self.record(account="Bob@example.com"),
                   self.record(account="amy@example.com")]
        expected = ["amy@example.com", "Bob@example.com", "zed@example.com"]
        fetched = self.snapshot(records)["providers"]
        self.assertEqual([r["accountEmail"] for r in fetched], expected)
        restored = self.snapshot(cache_only=True)["providers"]
        self.assertEqual([r["accountEmail"] for r in restored], expected)

    def test_cache_read_keeps_a_quiet_source_late_reading_last_known(self) -> None:
        # A one-off Meridian failure carries no error text but is still late.
        late = fetch._meridian_stale(self.record("claude"), {
            "stale": True, "fetchedAt": time.time() * 1000,
            "failure": {"reason": "rate_limited", "consecutiveFailures": 1}})
        self.snapshot([late], providers="claude")
        restored = self.snapshot(providers="claude", cache_only=True)["providers"][0]
        self.assertEqual((restored["stale"], restored["refreshError"], restored["sourceStale"]),
                         (True, "", True))

    def test_cache_read_keeps_a_source_reported_stale_reading_last_known(self) -> None:
        # Meridian can serve a recent reading whose own poll then failed.
        stale = fetch._last_known(self.record("claude"), "Meridian refresh failed (rate_limited)")
        stale["cachedAt"] = stale["updatedAt"]
        live = self.snapshot([stale], providers="claude")["providers"][0]
        self.assertTrue(live["stale"])
        restored = self.snapshot(providers="claude", cache_only=True)["providers"][0]
        self.assertTrue(restored["stale"])
        self.assertEqual(restored["refreshError"], "Meridian refresh failed (rate_limited)")

    def test_startup_restores_private_normalized_usage_without_refresh(self) -> None:
        record = self.record()
        record["identity"] = {"access_token": "secret-identity"}
        record["primary"]["rawToken"] = "secret-window"
        record["raw"] = {"credentials": "secret-payload"}
        fresh = self.snapshot([record])
        self.assertEqual(fresh["updatedAt"], self.measured.isoformat())
        contents = self.cache_path.read_text()
        self.assertNotIn("secret", contents)
        self.assertNotIn("codexRotation", contents)
        self.assertNotIn("forecast", contents)
        if not WINDOWS:  # Windows files have no mode bits; the profile's ACL keeps them private.
            self.assertEqual(self.cache_path.stat().st_mode & 0o777, 0o600)
        restored = self.snapshot(cache_only=True)
        old = restored["providers"][0]
        self.assertTrue(restored["cacheOnly"])
        self.assertIsNone(restored["forecast"])
        self.assertIsNone(restored["codexRotation"])
        self.assertEqual(restored["updatedAt"], self.measured.isoformat())
        self.assertEqual(old["primary"]["usedPercent"], 42)
        self.assertTrue(old["stale"])
        self.assertEqual(old["cachedAt"], self.measured.isoformat())
        self.assertEqual(old["refreshError"], "")

    def test_missing_cli_still_returns_last_known_usage(self) -> None:
        cli = str(self.home / "removed-cli")
        scopes = fetch._usage_cache_scopes(cli, ["codex"])
        fetch._usage_cache_write(scopes, [self.record()], self.now)
        startup = self.snapshot(cache_only=True, cli=cli)
        self.assertEqual(startup["providers"][0]["primary"]["usedPercent"], 42)
        self.assertIsNone(startup["fatal"])
        missing = fetch._result_error("codex", "cli_missing", f"CLI not found at {cli}")
        failed = self.snapshot([missing], cli=cli)
        self.assertEqual(failed["fatal"]["code"], "cli_missing")
        # What to install comes with it, for this platform.
        self.assertIn("CodexBar CLI", failed["fatal"]["setup"][0])
        self.assertIn("macOS tarball", fetch._cli_setup("darwin")[0])
        self.assertIn("codexbar-cli", fetch._cli_setup("linux")[0])
        # No Windows build to install: Meridian or nothing.
        windows = fetch._cli_setup("win32")
        self.assertIn("no Windows build", windows[0])
        self.assertIn("Meridian", windows[0])
        self.assertIn("turn usage tracking off", windows[-1])
        self.assertFalse(any("Install" in line or "brew" in line for line in windows))
        [kept] = failed["providers"]
        self.assertEqual((kept["stale"], kept["primary"]["usedPercent"]), (True, 42))
        self.assertEqual(failed["updatedAt"], self.measured.isoformat())
        # A cache answer (another poller's claim, startup) says so as well, once the reading failed.
        self.assertEqual(self.snapshot(cache_only=True, cli=cli)["fatal"]["code"], "cli_missing")

    def test_missing_cli_is_not_fatal_while_meridian_serves_a_provider(self) -> None:
        cli = str(self.home / "removed-cli")
        served = self.record("claude")
        missing = fetch._result_error("codex", "cli_missing", f"CLI not found at {cli}")
        result = self.snapshot([served, missing], providers="claude,codex", cli=cli)
        self.assertIsNone(result["fatal"])
        self.assertEqual([(r["id"], r["ok"]) for r in result["providers"]],
                         [("claude", True), ("codex", False)])
        self.assertIsNone(self.snapshot(cache_only=True, providers="claude,codex", cli=cli)["fatal"])

    def test_provider_failure_retains_only_that_provider_and_does_not_renew_age(self) -> None:
        self.snapshot([self.record(), self.record("kilo", None, 12)], providers="codex,kilo")
        failed = fetch._result_error("codex", "timeout", "Timed out")
        fresh, old = self.snapshot(
            [failed, self.record("kilo", None, 25)], providers="kilo,codex",
        )["providers"]
        self.assertEqual(fresh["primary"]["usedPercent"], 25)
        self.assertNotIn("stale", fresh)
        self.assertEqual(old["primary"]["usedPercent"], 42)
        self.assertEqual(old["refreshError"], "Timed out")
        self.assertEqual(old["cachedAt"], self.measured.isoformat())
        scopes = fetch._usage_cache_scopes(TRUE_EXECUTABLE, ["codex", "kilo"])
        expired = fetch._usage_cache_read(scopes, self.measured + dt.timedelta(hours=24, seconds=1))
        self.assertEqual(expired, {})
        recovered = self.snapshot([self.record(used=49)])["providers"][0]
        self.assertNotIn("stale", recovered)
        self.assertNotIn("refreshError", recovered)
        self.assertEqual(recovered["primary"]["usedPercent"], 49)

    def test_independent_account_failure_and_successful_roster_removal(self) -> None:
        self.snapshot([self.record(), self.record(account="b@example.com", used=81)])
        failed = fetch._normalize_record("codex", {
            "account": "B@example.com", "error": {"message": "Account expired"},
        })
        first, second = self.snapshot([self.record(used=51), failed])["providers"]
        self.assertEqual(first["primary"]["usedPercent"], 51)
        self.assertNotIn("stale", first)
        self.assertTrue(second["stale"])
        self.assertEqual(second["primary"]["usedPercent"], 81)
        self.assertEqual(second["refreshError"], "Account expired")
        self.snapshot([self.record(used=60)])
        failure = fetch._result_error("codex", "timeout", "Offline")
        remaining = self.snapshot([failure])["providers"]
        self.assertEqual([record["accountEmail"] for record in remaining], ["a@example.com"])
        self.assertEqual(remaining[0]["accountCount"], 1)
        self.assertEqual(remaining[0]["primary"]["usedPercent"], 60)

    def test_unknown_failed_account_never_borrows_another_account(self) -> None:
        self.snapshot([self.record()])
        failure = fetch._normalize_record("codex", {
            "account": "new@example.com", "error": {"message": "Cannot authenticate"},
        })
        result = self.snapshot([failure])["providers"]
        self.assertEqual(len(result), 1)
        self.assertFalse(result[0]["ok"])
        self.assertEqual(result[0]["accountEmail"], "new@example.com")
        self.assertEqual(self.snapshot(cache_only=True)["providers"], [])

    def test_cache_rejects_expired_future_and_malformed_records(self) -> None:
        self.snapshot([self.record()])
        original = json.loads(self.cache_path.read_text())
        for timestamp in (
            (self.now - dt.timedelta(hours=25)).isoformat(),
            (self.now + dt.timedelta(hours=1)).isoformat(),
            "not-a-date", "9999-12-31T23:59:59-23:59", None,
        ):
            with self.subTest(timestamp=timestamp):
                payload = json.loads(json.dumps(original))
                record = payload["providers"]["codex"]["records"][0]
                record["updatedAt"] = record["cachedAt"] = timestamp
                self.cache_path.write_text(json.dumps(payload))
                self.assertEqual(self.snapshot(cache_only=True)["providers"], [])
        for invalid in ("{", "null", "[]", '{"version":1,"providers":[]}'):
            with self.subTest(invalid=invalid):
                self.cache_path.write_text(invalid)
                self.assertEqual(self.snapshot(cache_only=True)["providers"], [])
                refreshed = self.snapshot([self.record(used=17)])
                self.assertEqual(refreshed["providers"][0]["primary"]["usedPercent"], 17)
        original["providers"]["codex"]["records"][0]["primary"]["usedPercent"] = "bad"
        self.cache_path.write_text(json.dumps(original))
        self.assertEqual(self.snapshot(cache_only=True)["providers"], [])

    def test_scope_rejects_disabled_provider_cli_auth_and_roster_changes(self) -> None:
        auth = self.home / ".codex" / "auth.json"
        auth.parent.mkdir()
        auth.write_text('{"tokens":{"account_id":"original"}}')
        self.snapshot([self.record(), self.record("kilo", None)], providers="codex,kilo")
        enabled = self.snapshot(providers="kilo", cache_only=True)["providers"]
        self.assertEqual([record["id"] for record in enabled], ["kilo"])
        self.assertEqual(self.snapshot(cache_only=True, cli="/missing/other-cli")["providers"], [])
        auth.write_text('{"tokens":{"account_id":"replacement"}}')
        self.assertEqual(self.snapshot(cache_only=True)["providers"], [])
        self.snapshot([self.record()])
        config = self.home / ".codexbar" / "config.json"
        config.parent.mkdir(exist_ok=True)
        config.write_text(json.dumps({
            "providers": [{"id": "codex", "codexProfileHomePaths": ["~/.codex-other"]}],
        }))
        self.assertEqual(self.snapshot(cache_only=True)["providers"], [])

    def test_opencode_api_key_change_invalidates_cached_account(self) -> None:
        with mock.patch.dict(fetch.os.environ, {"OPENCODE_API_KEY": "old-account-secret"}):
            self.snapshot([self.record("opencodego", None)], providers="opencodego")
            restored = self.snapshot(providers="opencodego", cache_only=True)["providers"]
            self.assertEqual(restored[0]["primary"]["usedPercent"], 42)
            self.assertNotIn("old-account-secret", self.cache_path.read_text())
            with mock.patch.dict(fetch.os.environ, {"OPENCODE_API_KEY": "new-account-secret"}):
                self.assertEqual(
                    self.snapshot(providers="opencodego", cache_only=True)["providers"], [],
                )

    def test_failed_atomic_replace_keeps_previous_good_snapshot(self) -> None:
        self.snapshot([self.record()])
        previous = self.cache_path.read_bytes()
        with mock.patch.object(fetch.os, "replace", side_effect=OSError("disk full")):
            current = self.snapshot([self.record(used=73)])
        self.assertEqual(current["providers"][0]["primary"]["usedPercent"], 73)
        self.assertEqual(self.cache_path.read_bytes(), previous)
        self.assertEqual(list(self.cache_path.parent.glob(".usage_cache.json.*")), [])

    def test_mid_fetch_account_switch_discards_success_and_failure(self) -> None:
        auth = self.home / ".codex" / "auth.json"
        auth.parent.mkdir()
        for succeeds in (False, True):
            with self.subTest(succeeds=succeeds):
                auth.write_text('{"tokens":{"account_id":"account-a"}}')
                self.snapshot([self.record()])
                old_scope = fetch._usage_cache_scopes(TRUE_EXECUTABLE, ["codex"])["codex"]

                def changed_fetch(provider):
                    if provider == "kilo":
                        return [self.record("kilo", None, 17)]
                    auth.write_text('{"tokens":{"account_id":"account-b"}}')
                    return [self.record(used=91) if succeeds
                            else fetch._result_error("codex", "timeout", "Offline")]

                failed, healthy = self.snapshot(
                    providers="codex,kilo", fetch_provider=changed_fetch)["providers"]
                self.assertFalse(failed["ok"])
                self.assertEqual(failed["error"]["code"], "credential_scope_changed")
                self.assertNotIn("primary", failed)
                new_scope = fetch._usage_cache_scopes(TRUE_EXECUTABLE, ["codex"])["codex"]
                self.assertNotEqual(new_scope, old_scope)
                self.assertEqual(failed["sourceScope"], new_scope)
                self.assertEqual(healthy["primary"]["usedPercent"], 17)
                self.assertEqual(self.snapshot(cache_only=True)["providers"], [])

    def test_switching_meridian_mid_fetch_discards_the_old_instances_accounts(self) -> None:
        self.snapshot([self.record("claude")], providers="claude")
        for name, value in (("MERIDIAN_PORT", "3457"), ("MERIDIAN_API_KEY", "other")):
            with self.subTest(name=name), mock.patch.dict(fetch.os.environ):
                def switched_fetch(_provider):
                    fetch.os.environ[name] = value
                    return [self.record("claude", used=91)]

                [record] = self.snapshot(providers="claude", fetch_provider=switched_fetch)["providers"]
                self.assertEqual(record["error"]["code"], "credential_scope_changed")
                self.assertNotIn("primary", record)

    def test_token_rotation_keeps_the_cache_and_an_account_change_drops_it(self) -> None:
        def codex(token: str, account: str) -> Path:
            auth = self.home / ".codex" / "auth.json"
            auth.parent.mkdir(exist_ok=True)
            auth.write_text(json.dumps({"tokens": {
                "account_id": account, "access_token": token, "refresh_token": token}}))
            return auth

        def claude(token: str, account: str) -> Path:
            # Claude's account is the one its login saved beside the token.
            login = self.home / ".claude"
            login.mkdir(exist_ok=True)
            (login / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": token}}))
            (login / ".claude.json").write_text(json.dumps(
                {"oauthAccount": {"accountUuid": account, "emailAddress": "a@example.com"}}))
            return login / ".claude.json"

        for provider, save in (("codex", codex), ("claude", claude)):
            with self.subTest(provider=provider):
                save("token-a", "account-a")
                fresh = self.snapshot([self.record(provider)], providers=provider)["providers"][0]
                identity = save("token-b", "account-a")
                restored = self.snapshot(providers=provider, cache_only=True)
                self.assertEqual([r["primary"]["usedPercent"] for r in restored["providers"]], [42])
                self.assertEqual(restored["providers"][0]["sourceScope"], fresh["sourceScope"])
                # Pollers reread when the file naming the account changes.
                self.assertIn(str(identity), restored["inputs"])
                save("token-b", "account-b")
                self.assertEqual(self.snapshot(providers=provider, cache_only=True)["providers"], [])

    def test_live_cached_and_failed_records_share_scope_without_persisting_it(self) -> None:
        live = self.snapshot([self.record()])["providers"][0]
        cached = self.snapshot(cache_only=True)["providers"][0]
        failed = self.snapshot([fetch._result_error("codex", "timeout", "Offline")])["providers"][0]
        self.assertEqual(live["sourceScope"], cached["sourceScope"])
        self.assertEqual(live["sourceScope"], failed["sourceScope"])
        self.assertNotIn("sourceScope", self.cache_path.read_text())
        fresh_error = self.snapshot(
            [fetch._result_error("kilo", "timeout", "Offline")], providers="kilo",
        )["providers"][0]
        self.assertEqual(
            fresh_error["sourceScope"], fetch._usage_cache_scopes(TRUE_EXECUTABLE, ["kilo"])["kilo"],
        )

    def test_partial_provider_writes_preserve_untouched_cache_and_prune_expired(self) -> None:
        self.snapshot([self.record(), self.record("kilo", None, 12)], providers="codex,kilo")
        self.snapshot([self.record(used=61)])
        restored = self.snapshot(providers="kilo", cache_only=True)["providers"]
        self.assertEqual([r["primary"]["usedPercent"] for r in restored], [12])
        scopes = fetch._usage_cache_scopes(TRUE_EXECUTABLE, ["codex"])
        fetch._usage_cache_write(scopes, [], self.now + dt.timedelta(hours=25))
        self.assertEqual(json.loads(self.cache_path.read_text())["providers"].get("kilo"), None)

    def test_empty_or_unavailable_scopes_preserve_valid_cache_without_exposing_it(self) -> None:
        self.snapshot([self.record()])
        fetch._usage_cache_write({}, [], self.now)
        self.assertEqual(
            [r["primary"]["usedPercent"] for r in self.snapshot(cache_only=True)["providers"]], [42],
        )
        config = self.home / ".codexbar" / "config.json"
        config.parent.mkdir(exist_ok=True)
        config.write_text("{")
        unscoped = self.snapshot([self.record(used=88)])["providers"][0]
        self.assertNotIn("sourceScope", unscoped)
        self.assertEqual(self.snapshot(cache_only=True)["providers"], [])
        config.unlink()
        self.assertEqual(
            [r["primary"]["usedPercent"] for r in self.snapshot(cache_only=True)["providers"]], [42],
        )

    def test_restart_restores_recent_consumption_from_history(self) -> None:
        scopes = fetch._usage_cache_scopes(TRUE_EXECUTABLE, ["codex"])
        for minutes, used in ((70, 40), (60, 40), (50, 42), (40, 43), (30, 44), (20, 45), (10, 46), (5, 46)):
            record = self.record(used=used)
            record["updatedAt"] = (self.now - dt.timedelta(minutes=minutes)).isoformat()
            fetch._usage_cache_write(scopes, [record], self.now - dt.timedelta(minutes=minutes))
        restored = self.snapshot(cache_only=True)["providers"][0]
        self.assertEqual(
            restored["primary"]["recent"], {"hours": 1, "consumedPercent": 6},
        )

    def test_opaque_oauth_credentials_remain_conservatively_scoped(self) -> None:
        for provider, relative, key in (
            ("codex", ".codex/auth.json", "access_token"),
            ("claude", ".claude/.credentials.json", "accessToken"),
        ):
            with self.subTest(provider=provider):
                auth = self.home / relative
                auth.parent.mkdir(exist_ok=True)
                section = "tokens" if provider == "codex" else "claudeAiOauth"
                auth.write_text(json.dumps({section: {key: "opaque-a"}}))
                self.snapshot([self.record(provider)], providers=provider)
                auth.write_text(json.dumps({section: {key: "opaque-b"}}))
                # Claude without a saved account (.claude.json) has no stable id either.
                self.assertEqual(self.snapshot(providers=provider, cache_only=True)["providers"], [])

    def test_an_unfinished_fetch_keeps_its_claim_past_the_interval(self) -> None:
        clock = [1000.0]
        claim = self.cache_path.with_name("fetch_claim")
        with mock.patch.object(fetch, "time", mock.Mock(wraps=time, time=lambda: clock[0])):
            self.assertEqual(fetch._claim_fetch(60, ["codex"], 1090), ["codex"])
            clock[0] = 1070
            self.assertEqual(fetch._claim_fetch(60, ["codex"], 1160), [])
            clock[0] = 1080
            fetch._release_fetch(["codex"], 1090)
            clock[0] = 1130
            self.assertEqual(fetch._claim_fetch(60, ["codex"], 1220), [])
            clock[0] = 1141
            self.assertEqual(fetch._claim_fetch(60, ["codex"], 1231), ["codex"])
            # That fetch crashed: its claim lapses once its budget ran out.
            clock[0] = 1230
            self.assertEqual(fetch._claim_fetch(60, ["codex"], 1320), [])
            clock[0] = 1232
            self.assertEqual(fetch._claim_fetch(60, ["codex"], 1322), ["codex"])
            # A per-provider time from before in-flight claims is the last fetch.
            claim.write_text(json.dumps({"codex": 1200}))
            self.assertEqual(fetch._claim_fetch(60, ["codex"], 1322), [])

    def test_failed_codex_cli_keeps_cached_accounts_beside_meridian_seats(self) -> None:
        self.snapshot([self.record(account="b@example.com", used=81)])
        now_ms = int(time.time() * 1000)
        responses = {
            "/profiles/list": {"profiles": [
                {"id": "seat", "type": "chatgpt", "email": "a@example.com", "subscriptionType": "plus"}]},
            "/v1/usage/quota/all": {"profiles": [
                {"id": "seat", "fetchedAt": now_ms,
                 "windows": [{"type": "five_hour", "utilization": 0.3, "resetsAt": None}]}]},
        }
        with (
            serve_meridian(responses),
            mock.patch.object(fetch, "_fetch_sources", return_value=[
                fetch._result_error("codex", "timeout", "CLI timed out after 30s")]),
            mock.patch.object(fetch, "_cli_version", return_value=None),
            mock.patch.object(fetch, "_read_rotation", return_value=None),
        ):
            seat, kept = run_main("--providers", "codex")["providers"]
        self.assertEqual((seat["accountEmail"], seat["primary"]["usedPercent"], seat.get("stale")),
                         ("a@example.com", 30, None))
        self.assertEqual((kept["accountEmail"], kept["primary"]["usedPercent"], kept["stale"],
                          kept["refreshError"]), ("b@example.com", 81, True, "CLI timed out after 30s"))

    def test_meridian_is_read_only_for_due_providers(self) -> None:
        claim = self.cache_path.with_name("fetch_claim")
        claim.parent.mkdir(parents=True, exist_ok=True)
        claim.write_text(json.dumps({"claude": time.time()}))

        def provider(_cli, name, *_):
            self.assertEqual(name, "zai")
            return [self.record("zai", None)]

        with (
            mock.patch.object(fetch, "_fetch_provider", side_effect=provider),
            mock.patch.object(fetch, "_read_http_body") as http,
            mock.patch.object(fetch, "_meridian_profile_emails",
                              wraps=fetch._meridian_profile_emails) as emails,
            mock.patch.object(fetch, "_cli_version", return_value=None),
        ):
            payload = run_main("--providers", "claude,zai", "--min-interval", "60")
        self.assertEqual([r["id"] for r in payload["providers"]], ["zai"])
        http.assert_not_called()
        # Without omp's rotation state there is nothing to label with emails.
        emails.assert_not_called()

    def test_stalled_meridian_cannot_outlast_the_run_deadline(self) -> None:
        def stall(_url, timeout, _headers=None):
            time.sleep(timeout)
            raise TimeoutError("stalled")

        def cli(command, timeout=None, **_):
            time.sleep(timeout)
            raise subprocess.TimeoutExpired(command, timeout)

        started = time.monotonic()
        with (
            mock.patch.object(fetch, "FETCH_BUDGET_MARGIN", 0.1),
            mock.patch.object(fetch, "_read_http_body", side_effect=stall),
            mock.patch.object(fetch.subprocess, "run", side_effect=cli),
            mock.patch.object(fetch, "_read_rotation", return_value=None),
        ):
            records = run_main("--providers", "claude,codex", "--timeout", "0.1")["providers"]
        # Budget: 2 × 0.1 s + 0.1 s; Meridian alone would wait 2 × 10 s.
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(sorted(r["id"] for r in records), ["claude", "codex"])
        self.assertFalse(any(r["ok"] for r in records))

    def test_provider_specific_auth_files_ignore_unrelated_opencode_entries(self) -> None:
        opencode = self.home / ".local/share/opencode/auth.json"
        kilo = self.home / ".local/share/kilo/auth.json"
        opencode.parent.mkdir(parents=True)
        kilo.parent.mkdir(parents=True)
        opencode.write_text(json.dumps({
            "opencode-go": {"type": "api", "key": "go-a"},
            "anthropic": {"type": "oauth", "access": "unrelated-a"},
        }))
        kilo.write_text('{"kilo":{"access":"kilo-a"}}')
        scopes = fetch._usage_cache_scopes(TRUE_EXECUTABLE, ["opencodego", "kilo"])
        opencode.write_text(json.dumps({
            "opencode-go": {"type": "api", "key": "go-a"},
            "anthropic": {"type": "oauth", "access": "unrelated-b"},
        }))
        self.assertEqual(fetch._usage_cache_scopes(TRUE_EXECUTABLE, ["opencodego", "kilo"]), scopes)
        kilo.write_text('{"kilo":{"access":"kilo-b"}}')
        updated = fetch._usage_cache_scopes(TRUE_EXECUTABLE, ["opencodego", "kilo"])
        self.assertEqual(updated["opencodego"], scopes["opencodego"])
        self.assertNotEqual(updated["kilo"], scopes["kilo"])
        opencode.write_text('{"opencode-go":{"type":"api","key":"go-b"}}')
        self.assertNotEqual(
            fetch._usage_cache_scopes(TRUE_EXECUTABLE, ["opencodego"])["opencodego"],
            scopes["opencodego"],
        )

    def test_known_account_refresh_during_fetch_keeps_fresh_usage(self) -> None:
        auth = self.home / ".codex/auth.json"
        auth.parent.mkdir()
        auth.write_text('{"tokens":{"account_id":"same","access_token":"old"}}')
        original_scope = fetch._usage_cache_scopes(TRUE_EXECUTABLE, ["codex"])["codex"]

        def refreshed_fetch(_provider):
            auth.write_text('{"tokens":{"account_id":"same","access_token":"new"}}')
            return [self.record(used=57)]

        [record] = self.snapshot(fetch_provider=refreshed_fetch)["providers"]
        self.assertTrue(record["ok"])
        self.assertEqual(record["primary"]["usedPercent"], 57)
        self.assertEqual(record["sourceScope"], original_scope)
        self.assertEqual(self.snapshot(cache_only=True)["providers"][0]["primary"]["usedPercent"], 57)

    def test_unscopable_provider_keeps_saved_entry_while_other_provider_updates(self) -> None:
        auth = self.home / ".codex/auth.json"
        auth.parent.mkdir()
        original_auth = '{"tokens":{"account_id":"same"}}'
        auth.write_text(original_auth)
        self.snapshot([self.record(), self.record("kilo", None, 10)], providers="codex,kilo")
        auth.write_text("{")
        current = self.snapshot(
            [self.record(used=88), self.record("kilo", None, 22)], providers="codex,kilo",
        )["providers"]
        self.assertNotIn("sourceScope", current[0])
        self.assertRegex(current[1]["sourceScope"], r"^[a-f0-9]{64}$")
        self.assertEqual(
            [r["id"] for r in self.snapshot(providers="codex,kilo", cache_only=True)["providers"]],
            ["kilo"],
        )
        auth.write_text(original_auth)
        restored = self.snapshot(providers="codex,kilo", cache_only=True)["providers"]
        self.assertEqual([r["primary"]["usedPercent"] for r in restored], [42, 22])

    def test_default_codex_rotation_preserves_all_accounts_scope(self) -> None:
        auth = self.home / ".codex/auth.json"
        profile_a = self.home / ".codex-a/auth.json"
        profile_b = self.home / ".codex-b/auth.json"
        for path, account in ((auth, "a"), (profile_a, "a"), (profile_b, "b")):
            path.parent.mkdir()
            path.write_text(json.dumps({"tokens": {
                "account_id": account, "access_token": str(path),
            }}))
        config = self.home / ".codexbar/config.json"
        config.parent.mkdir()
        config.write_text(json.dumps({"providers": [{
            "id": "codex", "codexProfileHomePaths": [str(profile_a.parent), str(profile_b.parent)],
        }]}))
        fresh = self.snapshot([self.record(), self.record(account="b@example.com", used=71)])
        original_scope = fresh["providers"][0]["sourceScope"]
        auth.write_text('{"tokens":{"account_id":"b","access_token":"rotated-default"}}')
        restored = self.snapshot(cache_only=True)["providers"]
        self.assertEqual([r["primary"]["usedPercent"] for r in restored], [42, 71])
        self.assertEqual(
            fetch._usage_cache_scopes(TRUE_EXECUTABLE, ["codex"])["codex"], original_scope,
        )
        auth.write_text('{"tokens":{"account_id":"c","access_token":"new-account"}}')
        self.assertNotEqual(
            fetch._usage_cache_scopes(TRUE_EXECUTABLE, ["codex"])["codex"], original_scope,
        )
        self.assertEqual(self.snapshot(cache_only=True)["providers"], [])

    def test_identityless_codex_credentials_remain_bound_to_home_path(self) -> None:
        first = self.home / ".codex"
        second = self.home / ".codex-other"
        for home in (first, second):
            home.mkdir()
            (home / "auth.json").write_text('{"tokens":{"access_token":"opaque"}}')
        original = fetch._usage_cache_scopes(TRUE_EXECUTABLE, ["codex"])["codex"]
        with mock.patch.dict(fetch.os.environ, {"CODEX_HOME": str(second)}):
            moved = fetch._usage_cache_scopes(TRUE_EXECUTABLE, ["codex"])["codex"]
        self.assertNotEqual(moved, original)


class WriteFileTests(IsolatedUsageTestCase):
    def test_a_failed_write_closes_its_temp_file_before_deleting_it(self) -> None:
        # Windows can't delete a file still open: deleting it then would hide the
        # write's own error and leave the temp file behind. Model that here.
        opened = []
        real_temp = fetch.state_paths.tempfile.NamedTemporaryFile
        real_unlink = Path.unlink

        def temp(*args, **kwargs):
            opened.append(real_temp(*args, **kwargs))
            return opened[-1]

        def unlink(path, *args, **kwargs):
            if any(not handle.closed and Path(handle.name) == path for handle in opened):
                raise PermissionError("in use")
            return real_unlink(path, *args, **kwargs)

        with mock.patch.object(fetch.state_paths.tempfile, "NamedTemporaryFile", temp), \
                mock.patch.object(Path, "unlink", unlink), \
                mock.patch.object(fetch.state_paths.os, "fsync", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                fetch.state_paths.write_file(self.home / "out.json", "{}", durable=True)
        self.assertEqual(list(self.home.glob(".out.json.*")), [])


class StateMigrationTests(IsolatedUsageTestCase):
    def test_first_run_moves_our_legacy_files_and_keeps_the_cli_files(self) -> None:
        legacy = self.home / ".codexbar"
        (legacy / "agents").mkdir(parents=True)
        (legacy / "agents" / "sentinel.json").write_text("{}")
        for name in (
            "agents.json", "agents.parsers.json", "usage_cache.json", "forecast_cache.json",
            "agents.lock", "usage_cache.json.lock", "tern_fetch_claim",
        ):
            (legacy / name).write_text(name)
        (legacy / "config.json").write_text('{"providers": []}')
        (legacy / "claude-oauth-cache.lock").write_text("")

        target = fetch.state_paths.migrate()
        self.assertEqual(target, self.home / ".local/state/codexbar-agentview")
        if not WINDOWS:  # Windows folders have no mode bits; the profile's ACL keeps them private.
            self.assertEqual(target.stat().st_mode & 0o777, 0o700)
        self.assertEqual(sorted(path.name for path in target.iterdir()), [
            "agents", "agents.json", "agents.parsers.json", "forecast_cache.json", "usage_cache.json",
        ])
        self.assertEqual((target / "usage_cache.json").read_text(), "usage_cache.json")
        self.assertEqual((target / "agents" / "sentinel.json").read_text(), "{}")
        self.assertEqual(sorted(path.name for path in legacy.iterdir()),
                         ["claude-oauth-cache.lock", "config.json"])
        self.assertEqual((legacy / "config.json").read_text(), '{"providers": []}')

        (legacy / "agents.json").write_text("written by an old helper")
        fetch.state_paths.migrate()
        self.assertEqual((target / "agents.json").read_text(), "agents.json")

    def test_retry_finishes_an_interrupted_migration_without_overwriting(self) -> None:
        legacy = self.home / ".codexbar"
        legacy.mkdir()
        (legacy / "agents.json").write_text("old history")
        (legacy / "usage_cache.json").write_text("old usage")
        target = self.home / ".local/state/codexbar-agentview"
        target.mkdir(parents=True)
        (target / "agents.json").write_text("new history")

        fetch.state_paths.migrate()
        self.assertEqual((target / "agents.json").read_text(), "new history")
        self.assertEqual((target / "usage_cache.json").read_text(), "old usage")


class RotationTestCase(IsolatedUsageTestCase):
    STATE = ""  # omp-sub-rotate's state file for this provider

    def setUp(self) -> None:
        super().setUp()
        self.directory = self.home / ".omp" / "agent" / "sub-rotate"

    def write_state(self, state: object) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / self.STATE).write_text(json.dumps(state), encoding="utf-8")


class CodexRotationTests(RotationTestCase):
    STATE = "codex-state.json"

    def test_missing_directory_or_state_hides_rotation(self) -> None:
        self.assertIsNone(fetch._read_rotation("codex"))
        self.directory.mkdir(parents=True)
        (self.directory / "codex-mode.json").write_text('{"operatingMode":"auto"}', encoding="utf-8")
        self.assertIsNone(fetch._read_rotation("codex"))

    def test_all_rules_with_missing_mode_default_to_confirm(self) -> None:
        rules = {
            "FILL": "Pros share traffic, stop at 85%.",
            "TARGET": "One Pro drains to 100%, redeems.",
            "EXPIRY-BURN": "Burn accounts whose resets expire soon.",
            "BURN": "Global reset announced; Pros stop 95%.",
            "NO-BANK": "No banked resets; Pros stop 95%.",
            "IDLE": "No Pro accounts logged in.",
        }
        for mode, description in rules.items():
            with self.subTest(mode=mode):
                self.write_state({"mode": mode})
                self.assertEqual(fetch._read_rotation("codex"), {
                    "mode": mode,
                    "description": description,
                    "operatingMode": "confirm",
                    "stalled": False,
                })

    def test_operating_modes_and_stalled_are_read_without_writes(self) -> None:
        # A mode file without the field means confirm, like a missing file.
        for config, operating_mode in (({"operatingMode": "auto"}, "auto"),
                                       ({"operatingMode": "confirm"}, "confirm"), ({}, "confirm")):
            for stalled in (False, True):
                with self.subTest(config=config, stalled=stalled):
                    self.write_state({"mode": "TARGET", "stalled": stalled})
                    mode_file = self.directory / "codex-mode.json"
                    mode_file.write_text(json.dumps(config), encoding="utf-8")
                    state_bytes = (self.directory / "codex-state.json").read_bytes()
                    mode_bytes = mode_file.read_bytes()
                    rotation = fetch._read_rotation("codex")
                    self.assertEqual(rotation["operatingMode"], operating_mode)
                    self.assertEqual(rotation["stalled"], stalled)
                    self.assertEqual((self.directory / "codex-state.json").read_bytes(), state_bytes)
                    self.assertEqual(mode_file.read_bytes(), mode_bytes)

    def test_invalid_state_hides_rotation_instead_of_inventing_idle(self) -> None:
        for state in (
            None, [], "FILL", 1, {}, {"mode": None}, {"mode": []},
            {"mode": "UNKNOWN"}, {"mode": "fill"},
            {"mode": "FILL", "stalled": "false"},
            {"mode": "FILL", "stalled": 1},
            {"mode": "FILL", "stalled": None},
        ):
            with self.subTest(state=state):
                self.write_state(state)
                self.assertIsNone(fetch._read_rotation("codex"))

    def test_invalid_operating_mode_hides_rotation(self) -> None:
        self.write_state({"mode": "FILL"})
        for config in (
            None, [], "auto", 1, {"operatingMode": None},
            {"operatingMode": []}, {"operatingMode": True},
            {"operatingMode": "AUTO"}, {"operatingMode": "unknown"},
        ):
            with self.subTest(config=config):
                (self.directory / "codex-mode.json").write_text(json.dumps(config), encoding="utf-8")
                self.assertIsNone(fetch._read_rotation("codex"))

    def test_corrupt_or_unreadable_files_hide_rotation(self) -> None:
        for filename in ("codex-state.json", "codex-mode.json"):
            for contents in (b"{", b"\xff"):
                with self.subTest(filename=filename, contents=contents):
                    self.write_state({"mode": "FILL"})
                    path = self.directory / filename
                    path.write_bytes(contents)
                    self.assertIsNone(fetch._read_rotation("codex"))
                    path.unlink()
            with self.subTest(filename=filename, contents="directory"):
                self.write_state({"mode": "FILL"})
                path = self.directory / filename
                path.unlink(missing_ok=True)
                path.mkdir()
                self.assertIsNone(fetch._read_rotation("codex"))
                path.rmdir()

    def test_account_availability_uses_owned_keys_only(self) -> None:
        state = {
            "mode": "FILL",
            "owned": {"16": {"until": "2000-01-01T00:00:00Z"}, "18": None},
            "accounts": [
                {"credentialId": 14, "label": "a@example.com"},
                {"credentialId": 16, "label": "b@example.com", "openReason": "target"},
                {"credentialId": 18, "label": "c@example.com"},
            ],
        }
        self.write_state(state)
        self.assertEqual(fetch._read_rotation("codex")["accountAvailability"], {
            "a@example.com": "open",
            "b@example.com": "blocked",
            "c@example.com": "blocked",
        })
        state["owned"] = {}
        self.write_state(state)
        self.assertEqual(fetch._read_rotation("codex")["accountAvailability"], {
            "a@example.com": "open",
            "b@example.com": "open",
            "c@example.com": "open",
        })

    def test_unknown_or_malformed_accounts_have_no_availability(self) -> None:
        account = {"credentialId": 14, "label": "a@example.com"}
        for fields in (
            {"accounts": [account]},
            {"owned": {}, "accounts": None},
            {"owned": {}, "accounts": {}},
            {"owned": {}, "accounts": []},
            *({"owned": owned, "accounts": [account]} for owned in (
                None, [], ["14"], "14", False, {"not-an-id": {}}, {"014": {}},
            )),
            *({"owned": {}, "accounts": accounts} for accounts in (
                [None],
                [{"credentialId": 14}],
                [{"credentialId": 14, "label": ""}],
                [{"credentialId": 14, "label": 123}],
                [{"credentialId": "14", "label": "a@example.com"}],
                [{"credentialId": True, "label": "a@example.com"}],
                [account, {"credentialId": 16, "label": "a@example.com"}],
                [account, {"credentialId": 14, "label": "b@example.com"}],
            )),
        ):
            with self.subTest(fields=fields):
                self.write_state({"mode": "FILL", **fields})
                rotation = fetch._read_rotation("codex")
                self.assertEqual(rotation["mode"], "FILL")
                self.assertNotIn("accountAvailability", rotation)

    def test_snapshot_refresh_reads_changes_and_removal(self) -> None:
        accounts = [{"credentialId": 14, "label": "a@example.com"}]
        self.write_state({"mode": "FILL", "owned": {"14": {}}, "accounts": accounts})

        def snapshot() -> dict:
            with (
                mock.patch.object(fetch, "_fetch_provider", return_value=[
                    {"id": "codex", "ok": True, "accountEmail": "a@example.com"},
                    {"id": "codex", "ok": True, "accountEmail": "b@example.com"},
                ]),
                mock.patch.object(fetch, "_cli_version", return_value=None),
            ):
                return run_main("--providers", "codex")

        rotation = snapshot()["codexRotation"]
        self.assertEqual(rotation["mode"], "FILL")
        self.assertEqual(rotation["accountAvailability"], {"a@example.com": "blocked"})
        self.write_state({"mode": "BURN", "stalled": True, "owned": {}, "accounts": accounts})
        rotation = snapshot()["codexRotation"]
        self.assertEqual(rotation["mode"], "BURN")
        self.assertTrue(rotation["stalled"])
        self.assertEqual(rotation["accountAvailability"], {"a@example.com": "open"})
        self.write_state({"mode": "BURN", "owned": [], "accounts": accounts})
        self.assertNotIn("accountAvailability", snapshot()["codexRotation"])
        self.write_state({"mode": "BURN"})
        self.assertNotIn("accountAvailability", snapshot()["codexRotation"])
        (self.directory / "codex-state.json").unlink()
        result = snapshot()
        self.assertIsNone(result["codexRotation"])
        self.assertEqual([record["accountEmail"] for record in result["providers"]],
                         ["a@example.com", "b@example.com"])


class ClaudeRotationTests(RotationTestCase):
    """omp-sub-rotate's Meridian rotation: `closed` keyed by Meridian profile id."""

    STATE = "meridian-state.json"

    def setUp(self) -> None:
        super().setUp()
        self.emails = {"work": "a@example.com", "home": "b@example.com"}

    def test_without_state_file_nothing_is_shown(self) -> None:
        self.assertIsNone(fetch._read_rotation("claude", self.emails))
        # The Codex rotation's files say nothing about Claude.
        self.directory.mkdir(parents=True)
        (self.directory / "codex-state.json").write_text('{"mode":"FILL"}', encoding="utf-8")
        (self.directory / "meridian-mode.json").write_text('{"operatingMode":"auto"}', encoding="utf-8")
        self.assertIsNone(fetch._read_rotation("claude", self.emails))

    def test_closed_profiles_map_to_emails(self) -> None:
        accounts = [{"id": "work"}, {"id": "home"}]
        self.write_state({"mode": "NO-BANK", "closed": {"home": {"until": 1}}, "accounts": accounts})
        self.assertEqual(fetch._read_rotation("claude", self.emails), {
            "mode": "NO-BANK",
            "description": "No banked resets; accounts stop 95%.",
            "operatingMode": "confirm",
            "stalled": False,
            "accountAvailability": {"a@example.com": "open", "b@example.com": "blocked"},
        })
        (self.directory / "meridian-mode.json").write_text('{"operatingMode":"auto"}', encoding="utf-8")
        self.write_state({"mode": "TARGET", "stalled": True, "closed": {}, "accounts": accounts})
        rotation = fetch._read_rotation("claude", self.emails)
        self.assertEqual(rotation["operatingMode"], "auto")
        self.assertTrue(rotation["stalled"])
        self.assertEqual(rotation["accountAvailability"],
                         {"a@example.com": "open", "b@example.com": "open"})

    def test_profile_without_known_email_hides_availability(self) -> None:
        for accounts, emails in (
            ([{"id": "work"}, {"id": "other"}], self.emails),
            ([{"id": "work"}, {"id": "home"}], {"work": "a@example.com", "home": "a@example.com"}),
            ([{"id": "work"}, {"id": "work"}], self.emails),
            ([{"id": 3}], self.emails),
            ([{"id": "work"}], None),
        ):
            with self.subTest(accounts=accounts, emails=emails):
                self.write_state({"mode": "FILL", "closed": {}, "accounts": accounts})
                rotation = fetch._read_rotation("claude", emails)
                self.assertEqual(rotation["mode"], "FILL")
                self.assertNotIn("accountAvailability", rotation)

    def test_snapshot_reads_meridian_profile_emails(self) -> None:
        self.write_state({"mode": "FILL", "closed": {"work": {}}, "accounts": [{"id": "work"}]})

        def snapshot(providers: str) -> dict:
            with (
                mock.patch.object(fetch, "_fetch_provider", return_value=[
                    {"id": "claude", "ok": True, "accountEmail": "a@example.com"},
                ]),
                mock.patch.object(fetch._Meridian, "profiles", return_value=[
                    ({"id": "work", "email": "a@example.com"}, {}),
                ]),
                mock.patch.object(fetch, "_cli_version", return_value=None),
            ):
                return run_main("--providers", providers)

        result = snapshot("claude")
        self.assertEqual(result["claudeRotation"]["accountAvailability"], {"a@example.com": "blocked"})
        self.assertIsNone(result["codexRotation"])
        self.assertIsNone(snapshot("codex")["claudeRotation"])
        (self.directory / "meridian-state.json").unlink()
        self.assertIsNone(snapshot("claude")["claudeRotation"])

    def test_cache_only_read_tracks_state_file_offline(self) -> None:
        login = self.home / ".config" / "meridian" / "work"
        login.mkdir(parents=True)
        (login / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"emailAddress": "a@example.com"}}), encoding="utf-8")
        (login.parent / "profiles.json").write_text(
            json.dumps([{"id": "work", "claudeConfigDir": str(login)}]), encoding="utf-8")
        self.write_state({"mode": "FILL", "closed": {"work": {}}, "accounts": [{"id": "work"}]})

        def cached() -> dict:
            with mock.patch.object(fetch._Meridian, "snapshot", side_effect=AssertionError("network")):
                return run_main("--providers", "claude", "--cache-only")

        self.assertEqual(cached()["claudeRotation"]["accountAvailability"], {"a@example.com": "blocked"})
        (self.directory / "meridian-state.json").unlink()
        self.assertIsNone(cached()["claudeRotation"])


class SessionWindowClampTests(unittest.TestCase):
    def test_only_a_codex_weekly_reset_ends_the_session_window_early(self) -> None:
        # Claude's weekly rollover does not clear the running 5h session.
        for provider, weekly_reset, expected in (
            ("codex", "2026-09-26T20:00:00Z",
             {"resetsAt": "2026-09-26T20:00:00Z", "startsAt": "2026-09-26T16:49:00Z"}),
            ("codex", "2026-09-30T20:00:00Z", {"resetsAt": "2026-09-26T21:49:00Z"}),
            ("claude", "2026-09-26T20:00:00Z", {"resetsAt": "2026-09-26T21:49:00Z"}),
        ):
            with self.subTest(provider=provider, weekly_reset=weekly_reset):
                primary = fetch._normalize_record(provider, {"usage": {
                    "primary": {"usedPercent": 64, "windowMinutes": 300,
                                "resetsAt": "2026-09-26T21:49:00Z"},
                    "secondary": {"usedPercent": 71, "windowMinutes": 10080,
                                  "resetsAt": weekly_reset},
                }})["primary"]
                self.assertEqual({key: primary[key] for key in ("resetsAt", "startsAt") if key in primary},
                                 expected)


class ResetCreditTests(unittest.TestCase):
    def test_counts_available_credits_and_soonest_expiry(self) -> None:
        record = fetch._normalize_record("codex", {"usage": {
            "primary": {"usedPercent": 1},
            "codexResetCredits": {"availableCount": 3, "credits": [
                {"status": "available", "expires_at": "2026-10-04T22:10:12Z"},
                {"status": "available", "expires_at": "2026-10-04T00:47:58Z"},
                {"status": "used", "expires_at": "2026-09-01T00:00:00Z"},
            ]},
        }})
        self.assertEqual(record["resetCredits"], {
            "count": 2,
            "soonestExpiresAt": "2026-10-04T00:47:58+00:00",
        })

    def test_missing_credits_yield_none(self) -> None:
        record = fetch._normalize_record("codex", {"usage": {"primary": {"usedPercent": 1}}})
        self.assertIsNone(record["resetCredits"])

    def test_claude_counts_only_active_unpaused_grants(self) -> None:
        now = dt.datetime(2026, 9, 23, tzinfo=dt.timezone.utc)
        credits = fetch._claude_reset_credits({"eligible": True, "grants": [
            {"resets_left": 2, "starts_at": "2026-09-22T16:00:00+00:00",
             "ends_at": "2026-10-22T16:00:00+00:00"},
            {"resets_left": 1, "ends_at": "2026-10-01T00:00:00+00:00"},
            {"resets_left": 1, "paused": True, "ends_at": "2026-09-24T00:00:00+00:00"},
            {"resets_left": 1, "starts_at": "2026-09-30T00:00:00+00:00"},
            {"resets_left": 1, "ends_at": "2026-09-22T00:00:00+00:00"},
            {"resets_left": 0, "ends_at": "2026-09-25T00:00:00+00:00"},
        ]}, now)
        self.assertEqual(credits, {
            "count": 3,
            "soonestExpiresAt": "2026-10-01T00:00:00+00:00",
        })

    def test_claude_ineligible_status_yields_none(self) -> None:
        now = dt.datetime(2026, 9, 23, tzinfo=dt.timezone.utc)
        self.assertIsNone(fetch._claude_reset_credits(
            {"eligible": False, "ineligible_reason": "surface", "grants": []}, now
        ))


class ForecastTests(IsolatedUsageTestCase):
    BODY = json.dumps({
        "last_reset_at": "2026-09-14T02:00:00Z",
        "cadence": {"recent_median_days": 2},
    }).encode()

    def test_eta_uses_cadence_and_the_next_window(self) -> None:
        for last_reset, cadence, now, expected in (
            # A projection before the window waits for its start.
            (utc(2026, 8, 31, 2, 34, 27), 2.1, utc(2026, 9, 2, 10), utc(2026, 9, 2, 23)),
            # One inside the window stays.
            (utc(2026, 9, 1, 22, 30), 25 / 24, utc(2026, 9, 2, 10), utc(2026, 9, 2, 23, 30)),
            # One inside the window but already past rolls a day.
            (utc(2026, 9, 1, 22, 30), 25 / 24, utc(2026, 9, 2, 23, 45), utc(2026, 9, 3, 23, 30)),
        ):
            with self.subTest(last_reset=last_reset, now=now):
                self.assertEqual(fetch._forecast_eta(last_reset, cadence, 23, 2, "UTC", now), expected)

    @unittest.skipUnless(HAS_TZ_DATABASE, "needs the IANA time zone database (tzdata on Windows)")
    def test_eta_window_hours_are_in_the_window_zone(self) -> None:
        def eta(last_reset, now, zone="America/Los_Angeles"):
            return fetch._forecast_eta(last_reset, 1, 23, 2, zone, now)

        # Projection 2026-11-01 12:00Z is 04:00 PST, after the DST change; the
        # 23:00 Los Angeles window start that day is 07:00Z on Nov 2.
        self.assertEqual(eta(utc(2026, 10, 31, 12), utc(2026, 10, 31, 13)), utc(2026, 11, 2, 7))
        self.assertEqual(eta(utc(2026, 10, 31, 12), utc(2026, 10, 31, 13), "Mars/Olympus"),
                         utc(2026, 11, 1, 12))
        # Projection Oct 31 23:30 PDT is inside the window but elapsed; one
        # day later is Nov 1 23:30 PST, not 22:30.
        self.assertEqual(eta(utc(2026, 10, 31, 6, 30), utc(2026, 11, 1, 6, 45)), utc(2026, 11, 2, 7, 30))

    def test_end_of_day_deadline_rounds_up_to_the_minute(self) -> None:
        deadline = fetch._announcement_deadline(
            {"window": {"target_at": "2026-10-03T06:59:59.999Z"}}
        )

        self.assertEqual(deadline, dt.datetime(2026, 10, 3, 7, tzinfo=dt.timezone.utc))

    def test_normalizes_forecast_fields(self) -> None:
        payload = fetch._normalize_forecast({
            "updated_at": "2026-09-02T10:03:10.570Z",
            "last_reset_at": "2026-08-31T02:34:27.000Z",
            "probabilities": {"raw_24h": 0.25, "raw_48h": 0.45},
            "time_window": {"start_hour": 23, "end_hour": 2, "label": "11 PM - 2 AM", "timezone": "UTC"},
            "cadence": {"recent_median_days": 2.1},
            "confidence": "low",
        })
        self.assertTrue(payload["ok"])
        self.assertEqual((payload["prob24h"], payload["prob48h"]), (25.0, 45.0))
        self.assertEqual((payload["windowStartHour"], payload["windowEndHour"]), (23, 2))
        self.assertTrue(payload["expectedAt"])
        self.assertIsNone(payload["alertSummary"])

    def test_only_an_alert_newer_than_the_last_reset_is_summarized(self) -> None:
        for alert, summary in (
            ({"source_at": "2026-09-01T18:00:00.000Z", "summary": "  Reset promised for 6pm PT.  "},
             "Reset promised for 6pm PT."),
            ({"source_at": "2026-08-31T02:34:27.000Z", "summary": "Usage reset for every paid subscription."},
             None),
            ({"source_at": "2026-08-29T12:00:00.000Z", "summary": "An earlier reset announcement."}, None),
            (None, None),
        ):
            with self.subTest(alert=alert):
                payload = fetch._normalize_forecast({
                    "last_reset_at": "2026-08-31T02:34:27.000Z",
                    "cadence": {"recent_median_days": 2.1},
                    "latest_alert": alert,
                })
                self.assertEqual(payload["alertSummary"], summary)

    def test_nonfinite_cadence_keeps_snapshot_healthy(self) -> None:
        body = json.dumps({
            "last_reset_at": "2026-08-31T02:34:27.000Z",
            "cadence": {"recent_median_days": "inf"},
        }).encode()
        with mock.patch.object(fetch, "_read_http_body", return_value=body):
            payload = run_main("--providers", "", "--forecast-url", "https://example.invalid/forecast")
        self.assertIsNone(payload["fatal"])
        self.assertEqual(payload["providers"], [])
        self.assertFalse(payload["forecast"]["ok"])

    def test_failed_refresh_serves_the_cache_as_stale(self) -> None:
        for cached in (
            {"ok": True, "stale": False, "expectedAt": "2026-09-02T23:00:00+00:00"},
            # A cached forecast without an ETA keeps it missing.
            {"ok": True, "stale": False, "expectedAt": None, "windowStartHour": 23, "windowEndHour": 2},
        ):
            with self.subTest(cached=cached):
                fetch._forecast_cache_write(cached)
                with (
                    mock.patch.object(fetch, "FORECAST_CACHE_TTL", -1),
                    mock.patch.object(fetch, "_read_http_body", side_effect=OSError("offline")),
                ):
                    result = fetch._fetch_forecast("http://127.0.0.1:9", 1)
                self.assertEqual(result, dict(cached, stale=True))

    def test_successful_response_is_cached(self) -> None:
        with forecast_server(self.BODY) as (url, _):
            result = fetch._fetch_forecast(url, 2)
        cached = fetch._fetch_forecast(url, 0.1)
        self.assertTrue(result["ok"])
        self.assertFalse(result["stale"])
        self.assertEqual(cached, result)

    def test_slow_drip_has_total_deadline_and_retains_stale_cache(self) -> None:
        cached = {"ok": True, "stale": False, "expectedAt": "2026-09-16T02:00:00+00:00"}
        fetch._forecast_cache_write(cached)
        timeout = 0.4
        with (
            mock.patch.object(fetch, "FORECAST_CACHE_TTL", -1),
            forecast_server(self.BODY, interval=0.05) as (url, requested),
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            started = time.monotonic()
            result = pool.submit(fetch._fetch_forecast, url, timeout).result(timeout=3)
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, timeout + 0.75)
        self.assertTrue(requested.is_set())
        self.assertEqual(result, dict(cached, stale=True))

    def test_oversized_valid_forecast_is_rejected(self) -> None:
        body = self.BODY + b" " * (fetch.FORECAST_MAX_BODY_BYTES + 1)
        with forecast_server(body) as (url, _):
            result = fetch._fetch_forecast(url, 2)
        self.assertFalse(Path(fetch._state_file(fetch.FORECAST_CACHE_NAME)).exists())
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "forecast_unavailable")

    def test_concurrent_cache_writes_leave_valid_json(self) -> None:
        payload = {"ok": True, "padding": "x" * 200_000}
        path = Path(fetch._state_file(fetch.FORECAST_CACHE_NAME))
        errors = []

        def write(_):
            for _ in range(30):
                fetch._forecast_cache_write(payload)
                try:
                    json.loads(path.read_text(encoding="utf-8"))
                except ValueError as error:
                    errors.append(error)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(write, range(8)))
        self.assertEqual(errors, [])
        self.assertEqual([p.name for p in path.parent.iterdir()], [path.name])


class CliVersionTests(unittest.TestCase):
    def test_version_comes_only_from_a_parsable_banner(self) -> None:
        for returncode, stdout, version in (
            (0, "CodexBar 0.56.3\n", "0.56.3"),
            (64, "Unknown flag '--version'", None),
            (0, "CodexBar\n", None),
        ):
            with self.subTest(returncode=returncode, stdout=stdout):
                completed = subprocess.CompletedProcess([], returncode, stdout, "")
                with mock.patch.object(fetch.subprocess, "run", return_value=completed):
                    self.assertEqual(fetch._cli_version("/bin/codexbar"), version)

    def test_missing_binary_yields_none(self) -> None:
        with mock.patch.object(fetch.subprocess, "run", side_effect=FileNotFoundError("no cli")):
            self.assertIsNone(fetch._cli_version("/bin/codexbar"))


class OpenRouterBalanceTests(unittest.TestCase):
    CREDITS_DETAILS = [{"title": "Credits", "rows": [
        {"label": "Remaining", "value": "$6.17"},
        {"label": "Used", "value": "$126.83"},
    ]}]

    def _normalize(self, usage: dict) -> dict:
        return fetch._normalize_record(
            "openrouter", {"provider": "openrouter", "source": "api", "usage": usage}
        )

    def test_without_a_key_limit_the_balance_replaces_the_bar(self) -> None:
        full = {"usedPercent": 100.0}
        for name, usage, balance in (
            # CLI 0.56.3 shape: no openRouterUsage at all.
            ("details row", {"primary": {**full, "resetDescription": "placeholder"},
                             "loginMethod": "Balance: $6.17", "details": self.CREDITS_DETAILS}, "$6.17 left"),
            ("login method", {"primary": full, "loginMethod": "Balance: $6.17"}, "$6.17 left"),
            ("zero key limit", {"primary": full, "openRouterUsage": {"keyLimit": 0},
                                "details": self.CREDITS_DETAILS}, "$6.17 left"),
            ("no key limit", {"primary": full, "openRouterUsage": {"balance": 6.17},
                              "details": self.CREDITS_DETAILS}, "$6.17 left"),
            ("thousands separator", {"details": [{"title": "Credits", "rows": [
                {"label": "Remaining", "value": "$1,234.50"}]}]}, "$1234.50 left"),
            ("unparseable", {"primary": full, "loginMethod": "Balance: unavailable", "details": [
                {"title": "Credits", "rows": [{"label": "Remaining", "value": "Unavailable"}]}]}, None),
            ("missing usage", {}, None),
        ):
            with self.subTest(name):
                record = self._normalize(usage)
                self.assertEqual(record["balanceText"], balance)
                self.assertIsNone(record["primary"])

    def test_key_limit_renders_bar_and_no_balance_text(self) -> None:
        record = self._normalize({
            "primary": {"usedPercent": 100.0},
            "openRouterUsage": {"keyLimit": 20, "keyUsageMonthly": 5.0},
            "details": self.CREDITS_DETAILS,
        })
        self.assertIsNone(record["balanceText"])
        self.assertEqual(record["primary"]["usedPercent"], 25.0)
        self.assertEqual(record["primary"]["resetDescription"], "$5.00 / $20")


class RecentUsageTests(unittest.TestCase):
    NOW = dt.datetime(2026, 9, 30, 12, tzinfo=dt.timezone.utc)
    KEY = "codex:a@example.com:secondary"

    def observe(self, history: dict, minutes_ago: float, used: float, reset: dt.datetime) -> None:
        fetch._usage_history_observe(history, {
            "id": "codex", "ok": True, "accountEmail": "A@example.com",
            "updatedAt": (self.NOW - dt.timedelta(minutes=minutes_ago)).isoformat(),
            "secondary": {"usedPercent": used, "windowMinutes": 10080, "resetsAt": reset.isoformat()},
        }, self.NOW)

    def observe_claude(self, history: dict, readings, reset: dt.datetime, slots=("primary",)) -> None:
        """Claude's (minutes_ago, used) readings of its 5h window, or of `slots`."""
        window_minutes = {"primary": 300, "secondary": 10080}
        for minutes, used in readings:
            fetch._usage_history_observe(history, {
                "id": "claude", "ok": True, "updatedAt": (self.NOW - dt.timedelta(minutes=minutes)).isoformat(),
                **{slot: {"usedPercent": used, "windowMinutes": window_minutes[slot],
                          "resetsAt": reset.isoformat()} for slot in slots},
            }, self.NOW)

    def consumption(self, samples) -> dict | None:
        """samples: (minutes_ago, used, reset) polled every 10 minutes between steps."""
        history: dict = {}
        for (start, used, reset), (end, _, _) in zip(samples, samples[1:] + [(0, None, None)]):
            minutes = start
            while minutes > end:
                self.observe(history, minutes, used, reset)
                minutes -= 10
        self.observe(history, 0, samples[-1][1], samples[-1][2])
        fetch._usage_history_prune(history, self.NOW)
        return fetch._recent_consumption(history[self.KEY], 24, self.NOW)

    def test_counts_increases_since_lookback_start(self) -> None:
        reset = self.NOW + dt.timedelta(days=3)
        recent = self.consumption([(30 * 60, 30, reset), (20 * 60, 35, reset), (60, 42, reset)])
        self.assertEqual(recent, {"hours": 24, "consumedPercent": 12})

    def test_new_cycle_counts_its_usage_even_above_the_previous_reading(self) -> None:
        old_reset = self.NOW - dt.timedelta(hours=6)
        new_reset = old_reset + dt.timedelta(days=7)
        recent = self.consumption([(30 * 60, 30, old_reset), (10 * 60, 40, old_reset), (6 * 60 - 5, 45, new_reset)])
        self.assertEqual(recent, {"hours": 24, "consumedPercent": 55})

    def test_small_drop_within_a_cycle_is_jitter_not_a_reset(self) -> None:
        reset = self.NOW + dt.timedelta(days=3)
        recent = self.consumption([(30 * 60, 42.3, reset), (60, 42.0, reset), (30, 43.0, reset)])
        self.assertEqual(recent["consumedPercent"], 1.0)

    def test_reset_inside_unobserved_gap_counts_new_cycle_usage(self) -> None:
        old_reset = self.NOW - dt.timedelta(hours=6)
        history: dict = {}
        for minutes in range(30 * 60, 8 * 60, -10):
            self.observe(history, minutes, 40, old_reset)
        for minutes in range(2 * 60, -1, -10):
            self.observe(history, minutes, 10, old_reset + dt.timedelta(days=7))
        recent = fetch._recent_consumption(history[self.KEY], 24, self.NOW)
        self.assertEqual(recent, {"hours": 24, "consumedPercent": 10})

    def test_five_hour_window_uses_one_hour_lookback(self) -> None:
        history: dict = {}
        self.observe_claude(history, ((120, 10), (110, 20), (100, 30), (90, 30), (80, 30), (70, 30), (60, 30),
                                      (50, 30), (40, 33), (30, 35), (20, 35), (10, 36), (0, 37)),
                            self.NOW + dt.timedelta(hours=2))
        record = {"id": "claude", "ok": True, "primary": {"usedPercent": 37, "windowMinutes": 300}}
        fetch._attach_recent_usage([record], history, self.NOW)
        self.assertEqual(record["primary"]["recent"], {"hours": 1, "consumedPercent": 7})

    def test_windows_get_runs_covering_their_span(self) -> None:
        history: dict = {}
        self.observe_claude(history, ((7 * 60, 5), (6 * 60, 8), (4 * 60, 20), (0, 37)),
                            self.NOW + dt.timedelta(hours=2), ("primary", "secondary"))
        record = {"id": "claude", "ok": True, "primary": {"usedPercent": 37, "windowMinutes": 300},
                  "secondary": {"usedPercent": 37, "windowMinutes": 10080}}
        fetch._attach_recent_usage([record], history, self.NOW)
        # The run in effect five hours ago (8%, seen 6h ago) starts it; the 7h-old one is dropped.
        self.assertEqual([run[2] for run in record["primary"]["runs"]], [8, 20, 37])
        self.assertEqual([run[2] for run in record["secondary"]["runs"]], [5, 8, 20, 37])

    def test_prune_keeps_a_week_and_thins_all_but_the_last_day(self) -> None:
        reset = self.NOW + dt.timedelta(days=1)
        history: dict = {}
        # A reading every 10 minutes for nine days, each one different.
        for step in range(9 * 24 * 6, -1, -1):
            self.observe(history, step * 10, 100 - step / 20, reset)
        fetch._usage_history_prune(history, self.NOW)
        runs = history[self.KEY]
        week = int((self.NOW - dt.timedelta(days=7)).timestamp())
        day = int((self.NOW - dt.timedelta(hours=24, minutes=30)).timestamp())
        self.assertLessEqual(runs[0][0], week)
        self.assertGreater(runs[1][0], week)
        older = [r for r in runs if r[1] < day]
        self.assertLessEqual(len(older), 6 * 24 * 2 + 2)
        self.assertEqual(len({r[0] // fetch.HISTORY_THIN_SECONDS for r in older}), len(older))
        # The last day keeps every reading, so recent consumption is unchanged.
        self.assertEqual(len([r for r in runs if r[0] >= day + fetch.HISTORY_THIN_SECONDS]), 24 * 6 + 1)
        self.assertEqual(runs[-1][2], 100)

    def test_thinning_keeps_resets_and_gaps_as_run_boundaries(self) -> None:
        thin = fetch.HISTORY_THIN_SECONDS
        base = int((self.NOW - dt.timedelta(days=3)).timestamp()) // thin * thin
        old, new = base + 3600, base + 3600 + 7 * 24 * 3600
        history = {self.KEY: [
            [base + 60, base + 300, 95, old], [base + 360, base + 600, 96, old],
            # The window resets within the same thin interval.
            [base + 900, base + 1200, 2, new], [base + 1260, base + 1500, 3, new],
            # Next interval: the same reading on both sides of an unobserved gap.
            [base + thin, base + thin + 60, 3, new], [base + thin + 1200, base + thin + 1260, 3, new],
        ]}
        fetch._usage_history_prune(history, self.NOW)
        self.assertEqual(history[self.KEY], [
            [base + 60, base + 600, 96, old], [base + 900, base + 1500, 3, new],
            [base + thin, base + thin + 60, 3, new], [base + thin + 1200, base + thin + 1260, 3, new],
        ])

    def test_gap_across_lookback_start_is_not_counted(self) -> None:
        history: dict = {}
        self.observe_claude(history, ((90, 10), (30, 30), (20, 30), (10, 32), (0, 32)),
                            self.NOW + dt.timedelta(hours=2))
        recent = fetch._recent_consumption(history["claude::primary"], 1, self.NOW)
        self.assertEqual(recent, {"hours": 1, "consumedPercent": 2})

    def test_a_future_run_drops_only_itself(self) -> None:
        now = int(self.NOW.timestamp())
        runs = [[now - 7200 + i * 60, now - 7200 + i * 60 + 30, i % 50, None] for i in range(100)]
        payload = {"history": {self.KEY: runs + [[now + 600, now + 900, 99, None]]}}
        self.assertEqual(fetch._usage_history_entries(payload, self.NOW), {self.KEY: runs})
        # A reading stamped ahead of the clock is skipped, so it can't block later ones.
        history: dict = {}
        self.observe(history, -10, 50, self.NOW + dt.timedelta(days=1))
        self.observe(history, 0, 51, self.NOW + dt.timedelta(days=1))
        self.assertEqual([run[2] for run in history[self.KEY]], [51])


if __name__ == "__main__":
    unittest.main()

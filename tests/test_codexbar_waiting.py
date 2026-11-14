from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "contents"
    / "scripts"
    / "codexbar_waiting.py"
)
sys.path.insert(0, str(SCRIPT_PATH.parent))
SPEC = importlib.util.spec_from_file_location("codexbar_waiting", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
waiting = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(waiting)
import state_paths  # noqa: E402  (after the path insert above)

WINDOWS = state_paths.WINDOWS
# A Tern daemon's `<pid>-<start time>` id read from /proc, which Windows and macOS lack.
HAS_PROC = Path("/proc/self/stat").exists()
# Whether a running daemon's id can be checked: /proc, or Windows' process times.
NAMES_DAEMONS = HAS_PROC or WINDOWS
# A daemon id of no running process, in the platform's form.
GONE_DAEMON = "999999999-20261114T073633.9672515" if WINDOWS else "999999999-1"

SID = "01a1abf1-15c8-748b-b9fa-126ebc0b88e5"
OTHER = "01a1abf1-0000-748b-b9fa-126ebc0b88e6"
T0 = datetime(2026, 11, 5, 12, 0, 0).astimezone()


def resume_line(cwd: str) -> str:
    """The command that resumes SID in `cwd`, in the platform's shell."""
    if WINDOWS:
        return f"Set-Location -LiteralPath '{cwd}' -ErrorAction Stop; omp --resume '{SID}'"
    return f"cd -- {cwd} && omp --resume {SID}"


class FakeGhError(Exception):
    def __init__(self, message: str, transient: bool) -> None:
        super().__init__(message)
        self.transient = transient


def fake_github(**overrides) -> types.SimpleNamespace:
    """waiting_github's contract, answering "nothing happened yet"."""
    defaults = dict(
        ENDED_POLL_S=3600,
        GhError=FakeGhError,
        is_transient=lambda text: "connect" in text or "HTTP 502" in text,
        FIX_PROMPT="fix {repo}#{number} at {head}",
        parse_pr=lambda text: ("o/r", 7) if "7" in text else None,
        poll=lambda cond, memo, **kwargs: {
            "memo": memo, "events": [], "fired": False, "fire_text": None,
            "confirm": False, "error": None, "transient": False, "next_poll_s": 180,
        },
        snapshot=lambda repo, number: {"head": "abc123", "threads": []},
        verify_fix=lambda repo, number, before: (True, "new head"),
        pr_status=lambda repo, number, run=None: {
            "title": f"PR {number}", "state": "open", "review": None, "checks": None,
        },
    )
    return types.SimpleNamespace(**{**defaults, **overrides})


def poll_result(memo: dict, **overrides) -> dict:
    return {
        "memo": memo, "events": [], "fired": False, "fire_text": None, "confirm": False,
        "error": None, "transient": False, "next_poll_s": 180, **overrides,
    }


class Case(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.omp = self.root / "omp"
        self.omp.mkdir()
        self.env = {
            "XDG_STATE_HOME": str(self.root / "state"),
            "HOME": str(self.root / "home"),
            # Path.home() on Windows.
            "USERPROFILE": str(self.root / "home"),
            "OMP_AGENT_DIR": str(self.omp),
            "PI_CODING_AGENT_DIR": "",
        }
        (self.root / "home").mkdir()
        patch = mock.patch.dict(os.environ, self.env)
        patch.start()
        self.addCleanup(patch.stop)
        self.state_dir = self.root / "state" / "codexbar-agentview"
        self.state_dir.mkdir(parents=True)
        self.clock = T0
        clock = mock.patch.object(waiting, "_now", lambda: self.clock)
        clock.start()
        self.addCleanup(clock.stop)
        # agents.json's bootId stands for the system's, unless a test sets one.
        self.system_boot = ""
        boot = mock.patch.object(waiting, "_system_boot_id", lambda: self.system_boot)
        boot.start()
        self.addCleanup(boot.stop)
        # When this boot began, as a wall-clock timestamp; unknown unless a test sets it.
        self.booted = None
        booted = mock.patch.object(waiting, "_booted_at", lambda: self.booted)
        booted.start()
        self.addCleanup(booted.stop)
        # The Tern daemon a command runs under; tests run outside Tern unless set.
        self.creator = None
        creator = mock.patch.object(waiting, "_ancestor_daemon", lambda: self.creator)
        creator.start()
        self.addCleanup(creator.stop)

    def agents(self, *live: str, closed: list[dict] | None = None, boot: str = "") -> None:
        for sid in live:
            (self.root / sid[:4]).mkdir(exist_ok=True)
        payload = {
            "bootId": boot,
            "agents": [
                {
                    "sessionId": sid, "provider": "omp", "cwd": str(self.root / sid[:4]),
                    "state": "idle", "ternPane": 40 + index, "ternSession": "Default",
                    "windowTitle": f"title {sid[:4]}", "recap": "live recap",
                    "resumeCommand": f"omp --resume {sid}",
                }
                for index, sid in enumerate(live)
            ],
            "history": closed or [],
        }
        (self.state_dir / "agents.json").write_text(json.dumps(payload))

    def run_cli(self, *argv: str, expect: int = 0) -> dict:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = waiting.main(list(argv))
        answer = json.loads(out.getvalue())
        self.assertEqual(code, expect, answer)
        self.assertEqual(answer["ok"], expect == 0)
        return answer

    def add(self, *argv: str, session: str = SID) -> dict:
        return self.run_cli("add", "--session", session, *argv)["wait"]

    def advance(self, **delta) -> None:
        self.clock += timedelta(**delta)

    def tick(self, *extra: str) -> dict:
        return self.run_cli("tick", "--now", self.clock.isoformat(), *extra)

    def tick_through(self, *extra: str, **delta) -> dict:
        """Ticks as the host makes them, every TICK_MAX_S at most, for `delta`:
        a longer gap is a sleep (`_note_tick`)."""
        end = self.clock + timedelta(**delta)
        while True:
            self.clock = min(self.clock + timedelta(seconds=waiting.TICK_MAX_S), end)
            answer = self.tick(*extra)
            if self.clock >= end:
                return answer

    def session(self, sid: str = SID) -> dict:
        view = self.run_cli("list")
        return next(item for item in view["sessions"] if item["session_id"] == sid)

    def wait_of(self, wait_id: str, sid: str = SID) -> dict:
        return next(wait for wait in self.session(sid)["waits"] if wait["id"] == wait_id)

    def heartbeat(self, sid: str = SID, *argv: str) -> None:
        self.run_cli("heartbeat", "--session", sid, *argv)

    def queued_prompt(self, *argv: str) -> tuple[str, str]:
        """A fired time wait whose prompt was claimed and delivered."""
        wait = self.add("--kind", "time", "--in", "1s", "--prompt", "go", *argv)["id"]
        self.advance(seconds=2)
        self.tick()
        action = self.run_cli("claim-prompts", "--session", SID)["prompts"][0]["action"]
        self.run_cli("ack", "--action", action, "--result", "delivered")
        return wait, action

    def state(self) -> dict:
        return json.loads((self.state_dir / "waiting.json").read_text(encoding="utf-8"))

    def github(self, **overrides):
        patch = mock.patch.dict(sys.modules, {"waiting_github": fake_github(**overrides)})
        patch.start()
        self.addCleanup(patch.stop)


class CliShapeTests(Case):
    def test_bad_input_is_one_json_object_with_exit_2(self) -> None:
        for argv in (
            ["add", "--session", SID, "--kind", "time"],
            ["add", "--session", SID, "--kind", "time", "--in", "soon"],
            ["add", "--session", SID, "--kind", "nope"],
            ["add", "--kind", "time", "--in", "1h"],
            ["add", "--session", SID, "--pane", "1", "--kind", "time", "--in", "1h"],
            ["add", "--session", "../etc", "--kind", "time", "--in", "1h"],
            ["add", "--session", SID, "--kind", "time", "--in", "1h", "--what", "x"],
            ["add", "--pane", "4", "--kind", "time", "--in", "1h", "--prompt", "x"],
            ["add", "--session", SID, "--kind", "command", "--argv", ""],
            ["add", "--session", SID, "--kind", "command", "--argv", "[1]"],
            *([] if WINDOWS else [["add", "--session", SID, "--kind", "command", "--argv", "ls 'open"]]),
            ["add", "--session", SID, "--kind", "command", "--argv", '["ls"]',
             "--predicate", "stdout_regex:("],
            ["add", "--session", SID, "--kind", "time", "--at", "24:00"],
            ["add", "--session", SID, "--kind", "time", "--at", "18:00", "--in", "1h"],
            ["add", "--session", SID, "--kind", "time", "--at", "18:60"],
            ["add", "--session", SID, "--kind", "something"],
            ["add", "--session", SID, "--kind", "something", "--what", "  "],
            ["stop", "--session", SID, "--wait", "w99"],
            ["stop", "--session", SID],
            ["stop", "--session", SID, "--wait", "w1", "--all"],
            ["mark-seen", "--session", SID, "--wait", "w99"],
            ["mark-seen", "--session", SID],
            ["nonsense"],
        ):
            with self.subTest(argv=argv):
                self.run_cli(*argv, expect=2)

    def test_removed_kinds_and_commands_are_gone(self) -> None:
        for argv in (
            ["add", "--session", SID, "--kind", "agent_turn"],
            ["add", "--session", SID, "--kind", "person", "--what", "x"],
            ["add", "--session", SID, "--kind", "something", "--what", "x", "--who", "y"],
            ["add", "--session", SID, "--kind", "something", "--what", "x", "--remind-in", "1d"],
            ["chain-pr", "--session", SID, "--repo", "o/r", "--number", "7"],
            ["done", "--session", SID],
        ):
            with self.subTest(argv=argv):
                self.run_cli(*argv, expect=2)

    def test_durations(self) -> None:
        self.assertEqual(waiting._parse_duration("1h30m"), 5400)
        self.assertEqual(waiting._parse_duration("1w"), 604800)
        for bad in ("", "0s", "5", "1x", "h1"):
            with self.subTest(bad=bad), self.assertRaises(waiting.BadInput):
                waiting._parse_duration(bad)

    def test_times_read_z_and_local(self) -> None:
        self.assertEqual(
            waiting._parse_time("2026-11-05T12:00:00Z").timestamp(),
            datetime.fromisoformat("2026-11-05T12:00:00+00:00").timestamp(),
        )
        self.assertEqual(waiting._parse_time(T0.replace(tzinfo=None).isoformat()), T0)

    def test_mutation_rewrites_the_view_file(self) -> None:
        answer = self.run_cli("add", "--session", SID, "--kind", "later", "--note", "x")
        self.assertEqual(answer["session"], SID)
        self.assertIsNone(answer["pane"])
        saved = json.loads((self.state_dir / "waiting_view.json").read_text())
        self.assertEqual(saved, answer["view"])

    def test_list_writes_nothing(self) -> None:
        self.assertEqual(self.run_cli("list")["sessions"], [])
        self.assertFalse((self.state_dir / "waiting.json").exists())
        self.assertFalse((self.state_dir / "waiting_view.json").exists())

    def test_unreadable_state_is_refused_not_replaced(self) -> None:
        (self.state_dir / "waiting.json").write_text("{broken")
        self.run_cli("add", "--session", SID, "--kind", "later", expect=1)
        self.assertEqual((self.state_dir / "waiting.json").read_text(), "{broken")


class ConditionTests(Case):
    def test_time_fires_at_its_time(self) -> None:
        wait = self.add("--kind", "time", "--in", "1h", "--note", "retry")["id"]
        self.advance(minutes=59)
        self.assertEqual(self.tick()["actions"], [])
        self.assertEqual(self.wait_of(wait)["state"], "waiting")
        self.advance(minutes=1)
        actions = self.tick()["actions"]
        self.assertEqual(self.wait_of(wait)["state"], "fired")
        self.assertEqual(self.wait_of(wait)["ball"], "on_me")
        self.assertEqual(actions[0]["type"], "toast")
        self.assertEqual(actions[0]["sub"], "retry")
        self.assertEqual(set(actions[0]), {"id", "type", "level", "text", "sub", "session", "pane"})
        self.assertTrue(self.session()["unseen"])

    def test_time_at_absolute(self) -> None:
        wait = self.add("--kind", "time", "--at", (T0 + timedelta(hours=1)).isoformat())["id"]
        self.tick()
        self.assertEqual(self.wait_of(wait)["state"], "waiting")
        self.advance(hours=2)
        self.tick()
        self.assertEqual(self.wait_of(wait)["state"], "fired")

    def test_no_toast(self) -> None:
        self.add("--kind", "time", "--in", "1s", "--no-toast")
        self.advance(seconds=2)
        self.assertEqual(self.tick()["actions"], [])

    def command(self, script: str, *argv: str) -> str:
        return self.add(
            "--kind", "command", "--argv", json.dumps([sys.executable, "-c", script]), *argv,
        )["id"]

    def test_command_exit0(self) -> None:
        wait = self.command("raise SystemExit(1)", "--interval", "1m")
        self.tick()
        self.assertEqual(self.wait_of(wait)["state"], "waiting")
        self.assertEqual(self.state()["sessions"][SID]["waits"][0]["errors"], 0)
        other = self.command("pass")
        self.tick()
        self.assertEqual(self.wait_of(other)["state"], "fired")

    def test_command_waits_for_its_interval(self) -> None:
        marker = self.root / "ran"
        wait = self.command(f"open({str(marker)!r}, 'a').write('x'); raise SystemExit(1)", "--interval", "1h")
        self.tick()
        self.advance(minutes=30)
        self.tick()
        self.assertEqual(marker.read_text(), "x")
        self.advance(minutes=31)
        self.tick()
        self.assertEqual(marker.read_text(), "xx")
        self.assertEqual(self.wait_of(wait)["state"], "waiting")

    def test_grep_style_miss_is_not_a_failure(self) -> None:
        wait = self.command("raise SystemExit(1)", "--predicate", "stdout_regex:FIXED", "--interval", "1m")
        for _ in range(6):
            self.tick()
            self.advance(minutes=2)
        self.assertEqual(self.wait_of(wait)["state"], "waiting")

    def test_failed_json_command_reports_stderr(self) -> None:
        wait = self.command(
            "import sys; print('boom', file=sys.stderr); raise SystemExit(2)",
            "--predicate", "json_eq:a=1", "--interval", "1m",
        )
        for _ in range(5):
            self.tick()
            self.advance(minutes=2)
        self.assertIn("exit 2: boom", self.wait_of(wait)["reason"])

    def test_json_eq_tells_true_from_one(self) -> None:
        script = "print('{\"a\": true}')"
        wait = self.command(script, "--predicate", "json_eq:a=1")
        self.tick()
        self.assertEqual(self.wait_of(wait)["state"], "waiting")

    def test_json_eq_tells_true_from_one_at_any_depth(self) -> None:
        for stdout, expected in (
            ('{"result": {"ready": 1}}', '{"ready":true}'),
            ('{"result": [1]}', "[true]"),
            ('{"result": {"ready": true, "extra": 1}}', '{"ready":true}'),
        ):
            with self.subTest(stdout=stdout, expected=expected):
                self.assertEqual(waiting._predicate(f"json_eq:result={expected}", 0, stdout), (False, ""))
        self.assertEqual(
            waiting._predicate('json_eq:result={"n":[1.0,true]}', 0, '{"result": {"n": [1, true]}}'), (True, ""),
        )

    def test_a_running_check_is_not_started_again(self) -> None:
        self.command("pass", "--interval", "30s")
        started = []

        def hang(jobs, now):
            started.extend(jobs)
            return []

        with mock.patch.object(waiting, "_run_jobs", hang):
            self.tick()
            self.advance(minutes=1)
            self.tick()
            self.assertEqual(len(started), 1)
            self.advance(minutes=5)
            self.tick()
        self.assertEqual(len(started), 2)

    def test_a_tick_claims_only_what_it_can_finish_in_time(self) -> None:
        for _ in range(3):
            self.command("raise SystemExit(1)")
        started = []

        def hang(jobs, now):
            started.append(len(jobs))
            return []

        with mock.patch.object(waiting, "MAX_CHECKS_PER_TICK", 2), \
                mock.patch.object(waiting, "_run_jobs", hang):
            self.tick()
            self.tick()
        self.assertEqual(started, [2, 1])

    def test_a_result_older_than_a_newer_claim_is_dropped(self) -> None:
        wait = self.command("pass", "--interval", "1m")
        claimed = []

        def hang(jobs, now):
            claimed.extend(jobs)
            return []

        with mock.patch.object(waiting, "_run_jobs", hang):
            self.tick()
            self.advance(minutes=6)
            self.tick()
        older, newer = ({**job, "result": {"holds": True, "error": ""}} for job in claimed)
        with waiting._transaction() as state:
            waiting._merge(state, [older], self.clock)
        self.assertEqual(self.wait_of(wait)["state"], "waiting")
        with waiting._transaction() as state:
            waiting._merge(state, [newer], self.clock)
        self.assertEqual(self.wait_of(wait)["state"], "fired")

    def test_command_stdout_regex(self) -> None:
        miss = self.command("print('red')", "--predicate", "stdout_regex:gr[e]+n")
        hit = self.command("print('green')", "--predicate", "stdout_regex:gr[e]+n")
        self.tick()
        self.assertEqual(self.wait_of(miss)["state"], "waiting")
        self.assertEqual(self.wait_of(hit)["state"], "fired")

    def test_command_json_eq(self) -> None:
        script = "print('{\"a\": {\"b\": [1, \"done\"]}}')"
        miss = self.command(script, "--predicate", 'json_eq:a.b.1="open"')
        hit = self.command(script, "--predicate", 'json_eq:a.b.1="done"')
        absent = self.command(script, "--predicate", "json_eq:a.c=1")
        self.tick()
        self.assertEqual(self.wait_of(miss)["state"], "waiting")
        self.assertEqual(self.wait_of(hit)["state"], "fired")
        self.assertEqual(self.wait_of(absent)["state"], "waiting")

    def test_changed_fires_on_new_output_only(self) -> None:
        source = self.root / "tag"
        source.write_text("v1")
        wait = self.command(
            f"print(open({str(source)!r}).read())", "--predicate", "changed", "--interval", "1m",
        )
        self.tick()
        stored = self.state()["sessions"][SID]["waits"][0]["condition"]
        self.assertEqual(stored["baseline"], "v1")
        self.assertEqual(self.wait_of(wait)["state"], "waiting")
        self.advance(minutes=2)
        self.tick()
        self.assertEqual(self.wait_of(wait)["state"], "waiting")
        source.write_text("v2")
        self.advance(minutes=2)
        self.tick()
        self.assertEqual(self.wait_of(wait)["state"], "fired")
        self.assertEqual(self.session()["history"][-1]["text"], "v1 → v2")

    def test_changed_failing_run_is_an_error_not_a_change(self) -> None:
        flag = self.root / "fail"
        script = (
            f"import os, sys\nif os.path.exists({str(flag)!r}): "
            "print('gone', file=sys.stderr); raise SystemExit(1)\nprint('v1')"
        )
        wait = self.command(script, "--predicate", "changed", "--interval", "1m")
        self.tick()
        flag.write_text("")
        self.advance(minutes=2)
        self.tick()
        self.assertEqual(self.wait_of(wait)["state"], "waiting")
        stored = self.state()["sessions"][SID]["waits"][0]
        self.assertEqual((stored["errors"], stored["condition"]["baseline"]), (1, "v1"))

    def test_a_verify_cannot_use_changed(self) -> None:
        answer = self.run_cli(
            "add", "--session", SID, "--kind", "time", "--in", "1m", "--prompt", "go",
            "--verify", '{"type": "command", "argv": ["true"], "predicate": "changed"}', expect=2,
        )
        self.assertIn("changed", answer["error"])

    def test_a_verify_cannot_wait_for_activity(self) -> None:
        answer = self.run_cli(
            "add", "--session", SID, "--kind", "time", "--in", "1m", "--prompt", "go",
            "--verify", '{"type": "pr", "repo": "o/r", "number": 7, "until": "activity"}', expect=2,
        )
        self.assertIn("activity", answer["error"])

    def test_activity_wait(self) -> None:
        wait = self.add("--kind", "pr", "--repo", "o/r", "--number", "7", "--until", "activity")
        self.assertEqual((wait["label"], wait["summary"]), ("New activity", "PR#7 new activity"))

    def test_release_wait(self) -> None:
        wait = self.add("--kind", "release", "--repo", "can1357/oh-my-pi")
        self.assertEqual(
            (wait["kind"], wait["label"], wait["summary"]),
            ("command", "next release of can1357/oh-my-pi", "next release of can1357/oh-my-pi"),
        )
        condition = self.state()["sessions"][SID]["waits"][0]["condition"]
        self.assertEqual(
            condition["argv"],
            ["gh", "api", "repos/can1357/oh-my-pi/releases/latest", "--jq", ".tag_name"],
        )
        self.assertEqual((condition["interval_s"], condition["predicate"]), (3600, "changed"))
        with mock.patch.object(waiting, "_run_argv", return_value=(0, "v1.87.0\n", "")):
            self.tick()
        self.assertEqual(self.wait_of(wait["id"])["summary"], "after v1.87.0")
        self.assertEqual(self.wait_of(wait["id"])["ball"], "on_machine")

    def test_release_options(self) -> None:
        wait = self.add("--kind", "release", "--repo", "a/b", "--interval", "10m")
        self.assertEqual(self.state()["sessions"][SID]["waits"][0]["condition"]["interval_s"], 600)
        self.assertEqual(wait["label"], "next release of a/b")
        for bad in (("--repo", "nope"), ("--repo", "a/b", "--number", "3"), ()):
            self.run_cli("add", "--session", SID, "--kind", "release", *bad, expect=2)

    def test_release_wait_on_a_repo_without_a_release(self) -> None:
        wait = self.add("--kind", "release", "--repo", "a/b", "--interval", "1m")["id"]
        missing = (1, '{"message":"Not Found"}', "gh: Not Found (HTTP 404)")
        with mock.patch.object(waiting, "_run_argv", return_value=missing):
            for _ in range(waiting.MAX_ERRORS + 1):
                self.tick()
                self.advance(minutes=2)
        seen = self.wait_of(wait)
        self.assertEqual((seen["state"], seen["ball"]), ("waiting", "on_machine"))
        self.assertEqual(self.session()["history"][0]["text"], "Baseline: no release yet")
        with mock.patch.object(waiting, "_run_argv", return_value=(0, "v1.0.0\n", "")):
            self.tick()
        self.assertEqual(self.wait_of(wait)["fired_text"], "v1.0.0 released")

    def test_a_release_that_disappears_fires_nothing(self) -> None:
        wait = self.add("--kind", "release", "--repo", "a/b", "--interval", "1m")["id"]
        with mock.patch.object(waiting, "_run_argv", return_value=(0, "v1.0.0\n", "")):
            self.tick()
        self.advance(minutes=2)
        with mock.patch.object(waiting, "_run_argv", return_value=(1, "", "gh: Not Found (HTTP 404)")):
            self.tick()
        self.assertEqual(self.wait_of(wait)["state"], "waiting")

    def test_command_runs_in_the_session_folder(self) -> None:
        folder = self.root / "project"
        folder.mkdir()
        self.agents()
        payload = json.loads((self.state_dir / "agents.json").read_text())
        payload["agents"] = [{"sessionId": SID, "cwd": str(folder)}]
        (self.state_dir / "agents.json").write_text(json.dumps(payload))
        wait = self.command(
            f"import os; print(os.getcwd() == {str(folder)!r})", "--predicate", "stdout_regex:True",
        )
        self.tick()
        self.assertEqual(self.wait_of(wait)["state"], "fired")

    def test_command_without_known_session_folder_captures_registration_directory(self) -> None:
        for session_cwd in (None, ""):
            with self.subTest(session_cwd=session_cwd):
                self.agents(closed=[{"sessionId": SID, "cwd": session_cwd}])
                with mock.patch.object(waiting.os, "getcwd", return_value=str(self.root)):
                    wait = self.add("--kind", "command", "--argv", '["true"]')["id"]
                saved = next(
                    item for item in self.state()["sessions"][SID]["waits"] if item["id"] == wait
                )
                self.assertEqual(saved["condition"]["cwd"], str(self.root))

    def test_command_preserves_known_missing_or_relative_session_folder(self) -> None:
        for session_cwd in ("relative", str(self.root / "missing")):
            with self.subTest(session_cwd=session_cwd):
                self.agents(closed=[{"sessionId": SID, "cwd": session_cwd}])
                wait = self.add("--kind", "command", "--argv", '["true"]')["id"]
                saved = next(
                    item for item in self.state()["sessions"][SID]["waits"] if item["id"] == wait
                )
                self.assertEqual(saved["condition"]["cwd"], session_cwd)

    def test_command_rejects_unsaved_or_relative_directory_before_spawning(self) -> None:
        for cwd in (None, "", "relative", 42):
            with self.subTest(cwd=cwd), mock.patch.object(waiting, "_run_argv") as run:
                condition = {"argv": ["true"], "predicate": "exit0"}
                if cwd is not None:
                    condition["cwd"] = cwd
                result = waiting._run_command(condition)
                self.assertFalse(result["holds"])
                self.assertIn("cwd", result["error"])
                self.assertIn("recreate", result["error"])
                run.assert_not_called()

    def test_five_failures_need_attention(self) -> None:
        wait = self.add(
            "--kind", "command", "--argv", '["/nonexistent/tool"]', "--interval", "1m",
        )["id"]
        for _ in range(4):
            self.tick()
            self.advance(minutes=2)
        self.assertEqual(self.wait_of(wait)["state"], "waiting")
        self.tick()
        seen = self.wait_of(wait)
        self.assertEqual(seen["state"], "needs_attention")
        self.assertIn("failed 5 times", seen["reason"])
        self.assertEqual(self.session()["next_step"], seen["reason"])

    def test_command_timeout_counts_as_failure(self) -> None:
        with mock.patch.object(waiting, "COMMAND_TIMEOUT_S", 0.2):
            code, _, error = waiting._run_argv([sys.executable, "-c", "import time; time.sleep(30)"], None)
        self.assertIsNone(code)
        self.assertIn("timed out", error)

    def test_a_timeout_also_kills_what_the_command_started(self) -> None:
        # The grandchild holds the output pipe: alive, it keeps the read waiting 5 s more.
        command = ("import subprocess, sys, time; "
                   "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], stdout=sys.stdout); "
                   "time.sleep(30)")
        started = time.monotonic()
        with mock.patch.object(waiting, "COMMAND_TIMEOUT_S", 1):
            code, _, error = waiting._run_argv([sys.executable, "-c", command], None)
        self.assertEqual((code, error), (None, "timed out after 1 s"))
        self.assertLess(time.monotonic() - started, 4, "the command's child outlived the kill")

    def test_a_timeout_kills_a_child_that_outlives_the_command(self) -> None:
        # The command exits at once; its child keeps the output pipe open, so
        # the read times out, and only the child is left to kill.
        command = ("import subprocess, sys; "
                   "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], stdout=sys.stdout)")
        started = time.monotonic()
        with mock.patch.object(waiting, "COMMAND_TIMEOUT_S", 2):
            code, _, error = waiting._run_argv([sys.executable, "-c", command], None)
        self.assertEqual((code, error), (None, "timed out after 2 s"))
        self.assertLess(time.monotonic() - started, 5, "the orphaned child outlived the kill")

    def test_events_by_pane_resolve_the_session(self) -> None:
        self.agents(SID)
        self.heartbeat()
        wait, _ = self.queued_prompt()
        self.run_cli("event", "--type", "turn_finished", "--pane", "40")
        self.assertEqual(self.wait_of(wait)["state"], "review")

    def test_a_wait_the_agent_sets_during_the_prompts_turn_supersedes_the_one_that_ran(self) -> None:
        self.agents(SID)
        self.heartbeat()
        wait, _ = self.queued_prompt("--label", "4h check")
        self.advance(minutes=1)
        after = self.add("--kind", "time", "--in", "1d", "--label", "24h check", "--by", "agent")["id"]
        self.run_cli("event", "--type", "turn_finished", "--pane", "40")
        self.assertEqual(self.state()["sessions"][SID]["waits"][0]["state"], "done")
        session = self.session()
        self.assertEqual([w["id"] for w in session["waits"]], [after])
        self.assertEqual(session["ball"], "on_timer")
        self.assertIn("4h check: ran; superseded by 24h check", [h["text"] for h in session["history"]])

    def test_a_wait_set_before_the_prompt_ran_doesnt_supersede_it(self) -> None:
        self.agents(SID)
        self.heartbeat()
        self.add("--kind", "time", "--in", "1d", "--label", "later one")
        wait, _ = self.queued_prompt()
        self.run_cli("event", "--type", "turn_finished", "--pane", "40")
        self.assertEqual(self.wait_of(wait)["state"], "review")

    def test_a_wait_i_add_during_the_prompts_turn_doesnt_supersede_it(self) -> None:
        self.agents(SID)
        self.heartbeat()
        wait, _ = self.queued_prompt()
        self.advance(minutes=1)
        self.add("--kind", "time", "--in", "1d", "--label", "mine")
        self.run_cli("event", "--type", "turn_finished", "--pane", "40")
        self.assertEqual(self.wait_of(wait)["state"], "review")

    def test_seen_ends_what_is_on_me_and_stop_ends_the_rest(self) -> None:
        self.agents(SID)
        self.heartbeat()
        ran, _ = self.queued_prompt()
        self.run_cli("event", "--type", "turn_finished", "--pane", "40")
        later = self.add("--kind", "time", "--in", "1d")["id"]
        self.run_cli("mark-seen", "--session", SID)
        self.assertEqual([w["id"] for w in self.session()["waits"]], [later])
        self.run_cli("mark-seen", "--session", SID, expect=2)
        self.run_cli("stop", "--session", SID, "--all")
        self.assertEqual({w["id"]: w["state"] for w in self.state()["sessions"][SID]["waits"]}, {ran: "done", later: "done"})

    def test_job_on_a_pane_record(self) -> None:
        answer = self.run_cli("add", "--pane", "51", "--kind", "job", "--note", "build")
        self.assertIsNone(answer["session"])
        self.assertEqual(answer["pane"], 51)
        wait = answer["wait"]["id"]
        pane = answer["view"]["panes"][0]
        self.assertEqual((pane["pane"], pane["ball"], pane["note"]), (51, "on_machine", "build"))
        self.run_cli("event", "--type", "job_finished", "--pane", "52", "--status", "0")
        self.assertEqual(self.run_cli("list")["panes"][0]["waits"][0]["state"], "waiting")
        view = self.run_cli("event", "--type", "job_finished", "--pane", "51", "--status", "0")["view"]
        self.assertEqual(view["panes"][0]["waits"][0]["id"], wait)
        self.assertEqual(view["panes"][0]["ball"], "on_me")

    def test_job_of_the_session_own_pane(self) -> None:
        self.agents(SID)
        wait = self.add("--kind", "job")
        self.assertEqual(wait["condition"]["pane"], 40)

    def test_job_needs_a_known_pane(self) -> None:
        self.run_cli("add", "--session", SID, "--kind", "job", expect=2)

    def test_pane_gone_is_no_finished_job(self) -> None:
        wait = self.add("--kind", "job", "--pane", "51")["id"]
        self.run_cli("event", "--type", "pane_closed", "--pane", "51")
        self.assertEqual(self.wait_of(wait)["state"], "needs_attention")

    def test_chained_jobs_on_one_pane_take_one_event_each(self) -> None:
        first = self.add("--kind", "job", "--pane", "51")["id"]
        second = self.add("--kind", "job", "--pane", "51", "--after", first)["id"]
        self.run_cli("event", "--type", "job_finished", "--pane", "51", "--status", "0")
        self.assertEqual(self.wait_of(first)["state"], "fired")
        self.assertEqual(self.wait_of(second)["state"], "waiting")

    def test_job_on_a_session(self) -> None:
        wait = self.add("--kind", "job", "--pane", "51")["id"]
        self.run_cli("event", "--type", "job_finished", "--pane", "51", "--status", "0")
        self.assertEqual(self.wait_of(wait)["state"], "fired")

    def test_job_close_on_success(self) -> None:
        self.run_cli("add", "--pane", "51", "--kind", "job", "--close-on-success")
        view = self.run_cli("event", "--type", "job_finished", "--pane", "51", "--status", "0")["view"]
        self.assertEqual(view["panes"], [])
        actions = self.tick()["actions"]
        self.assertEqual(
            [action for action in actions if action["type"] == "close_pane"],
            [{"id": mock.ANY, "type": "close_pane", "pane": 51}],
        )
        self.assertEqual(
            [action["level"] for action in actions if action["type"] == "toast"], ["success"],
        )

    def test_a_job_that_closed_its_pane_marks_the_session_unseen(self) -> None:
        self.add("--kind", "job", "--pane", "51", "--close-on-success")
        self.run_cli("event", "--type", "job_finished", "--pane", "51", "--status", "0")
        self.assertTrue(self.state()["sessions"][SID]["unseen"])

    def test_job_failure_keeps_the_pane_and_comes_to_me_in_red(self) -> None:
        self.run_cli("add", "--pane", "51", "--kind", "job", "--close-on-success")
        view = self.run_cli("event", "--type", "job_finished", "--pane", "51", "--status", "3")["view"]
        wait = view["panes"][0]["waits"][0]
        self.assertEqual((wait["state"], wait["ball"]), ("needs_attention", "on_me"))
        self.assertIn("3", wait["reason"])
        actions = self.tick()["actions"]
        self.assertEqual([action["type"] for action in actions], ["toast"])
        self.assertEqual(actions[0]["level"], "error")

    def test_pane_closed_drops_the_record(self) -> None:
        self.run_cli("add", "--pane", "51", "--kind", "job")
        view = self.run_cli("event", "--type", "pane_closed", "--pane", "51")["view"]
        self.assertEqual(view["panes"], [])
        self.assertEqual(self.state()["panes"], {})

    def test_reboot_drops_window_actions_on_old_pane_ids(self) -> None:
        self.agents(boot="one")
        self.run_cli("add", "--pane", "51", "--kind", "job", "--park")
        self.run_cli("add", "--pane", "52", "--kind", "job", "--close-on-success")
        self.run_cli("event", "--type", "job_finished", "--pane", "52", "--status", "0")
        self.assertEqual(
            sorted(action["type"] for action in self.tick()["actions"]),
            ["close_pane", "park", "toast"],
        )
        self.agents(boot="two")
        self.assertEqual([action["type"] for action in self.tick()["actions"]], ["toast"])

    def test_reboot_drops_pane_records(self) -> None:
        self.agents(boot="one")
        self.run_cli("add", "--pane", "51", "--kind", "job")
        self.tick()
        self.assertEqual(len(self.run_cli("list")["panes"]), 1)
        self.agents(boot="two")
        self.tick()
        self.assertEqual(self.run_cli("list")["panes"], [])

    def test_reboot_keeps_what_the_new_daemon_made_before_its_first_tick(self) -> None:
        self.agents(boot="one")
        self.creator = "a"
        self.run_cli("add", "--pane", "51", "--kind", "job")
        self.tick("--daemon", "a")
        self.agents(boot="two")
        self.advance(seconds=waiting.TICKER_LEASE_S)  # "a" ran before the reboot
        self.creator = "b"
        self.run_cli("add", "--pane", "52", "--kind", "job", "--park")
        actions = self.tick("--daemon", "b")["actions"]
        self.assertEqual([action["type"] for action in actions], ["park"])
        self.assertEqual([pane["pane"] for pane in self.run_cli("list")["panes"]], [52])

    def test_something_never_fires_and_ends_with_stop(self) -> None:
        answer = self.add("--kind", "something", "--what", "Reply from Dóra")
        wait = answer["id"]
        self.assertEqual((answer["label"], answer["summary"]), ("Reply from Dóra", "Reply from Dóra"))
        self.advance(days=2)
        self.assertEqual(self.tick()["actions"], [])
        seen = self.wait_of(wait)
        self.assertEqual((seen["state"], seen["ball"]), ("waiting", "on_someone"))
        self.assertIsNone(seen["due_at"])
        self.assertEqual(self.session()["status"], "⏳ Reply from Dóra · 2d")
        self.run_cli("stop", "--session", SID, "--wait", wait)
        self.assertEqual(self.run_cli("list")["sessions"], [])

    def test_something_past_its_deadline_comes_to_me_and_stays_open(self) -> None:
        wait = self.add("--kind", "something", "--what", "x", "--deadline-in", "1d")["id"]
        self.assertEqual(self.wait_of(wait)["ball"], "on_someone")
        self.advance(days=1)
        self.assertEqual(len(self.tick()["actions"]), 1)
        seen = self.wait_of(wait)
        self.assertEqual(
            (seen["state"], seen["ball"], seen["overdue"]), ("waiting", "on_me", True),
        )

    def test_something_takes_no_prompt(self) -> None:
        for prompt in ("go", ""):
            with self.subTest(prompt=prompt):
                self.run_cli(
                    "add", "--session", SID, "--kind", "something", "--what", "x",
                    "--prompt", prompt, expect=2,
                )
        self.assertFalse((self.state_dir / "waiting.json").exists())

    def test_something_works_on_a_pane(self) -> None:
        pane = self.run_cli("add", "--pane", "51", "--kind", "something", "--what", "x")["view"]["panes"][0]
        self.assertEqual(pane["ball"], "on_someone")

    def test_snoozed_prompt_goes_out_once_at_the_new_time(self) -> None:
        wait = self.add("--kind", "time", "--in", "1h", "--prompt", "go")["id"]
        self.advance(hours=1)
        self.tick()
        self.run_cli("snooze", "--session", SID, "--wait", wait, "--for", "1d")
        self.assertEqual(self.state()["sessions"][SID]["prompt_queue"], [])
        self.tick()
        self.assertEqual(self.wait_of(wait)["state"], "waiting")
        self.advance(days=1)
        self.tick()
        self.assertEqual(self.wait_of(wait)["state"], "acting")
        self.assertEqual(len(self.state()["sessions"][SID]["prompt_queue"]), 1)

    def test_a_bare_clock_is_the_next_time_it_shows(self) -> None:
        for clock, days, hour, minute in (
            ("18:00", 0, 18, 0), ("12:01", 0, 12, 1), ("12:00", 1, 12, 0), ("00:30", 1, 0, 30),
            ("9:30", 1, 9, 30),
        ):
            with self.subTest(clock=clock):
                wait = self.add("--kind", "time", "--at", clock)
                expected = (self.clock + timedelta(days=days)).replace(hour=hour, minute=minute)
                self.assertEqual(wait["condition"]["at"], expected.isoformat())

    def test_a_bare_clock_after_18_is_tomorrow(self) -> None:
        self.clock = self.clock.replace(hour=18, minute=0, second=30)
        wait = self.add("--kind", "time", "--at", "18:00")
        tomorrow = (self.clock + timedelta(days=1)).replace(second=0)
        self.assertEqual(wait["condition"]["at"], tomorrow.isoformat())

    @unittest.skipUnless(hasattr(time, "tzset"), "needs time.tzset to change the time zone in-process")
    def test_a_bare_clock_keeps_the_wall_clock_over_a_dst_change(self) -> None:
        self.addCleanup(time.tzset)
        with mock.patch.dict(os.environ, {"TZ": "CET-1CEST,M3.5.0,M10.5.0/3"}):
            time.tzset()
            self.clock = datetime(2026, 10, 24, 20, 0, 0).astimezone()
            wait = self.add("--kind", "time", "--at", "18:00")
        self.assertEqual(wait["condition"]["at"], "2026-10-25T18:00:00+01:00")

    @unittest.skipUnless(hasattr(time, "tzset"), "needs time.tzset to change the time zone in-process")
    def test_a_bare_clock_takes_the_second_pass_of_a_repeated_hour(self) -> None:
        self.addCleanup(time.tzset)
        with mock.patch.dict(os.environ, {"TZ": "CET-1CEST,M3.5.0,M10.5.0/3"}):
            time.tzset()
            self.clock = datetime(2026, 10, 25, 2, 15, 0, fold=1).astimezone()
            wait = self.add("--kind", "time", "--at", "02:30")
        self.assertEqual(wait["condition"]["at"], "2026-10-25T02:30:00+01:00")

    def test_a_bare_clock_in_a_chain_counts_from_the_step_start(self) -> None:
        first = self.add("--kind", "something", "--what", "x")["id"]
        second = self.add("--kind", "time", "--at", "18:00", "--after", first)
        self.assertEqual((second["state"], second["summary"]), ("pending", "at 18:00"))
        self.clock = self.clock.replace(hour=19)
        self.run_cli("what", "--session", SID, "--text", "")
        tomorrow = (self.clock + timedelta(days=1)).replace(hour=18)
        self.assertEqual(self.wait_of(second["id"])["condition"]["at"], tomorrow.isoformat())

    def test_later_never_fires(self) -> None:
        wait = self.add("--kind", "later", "--size", "M")["id"]
        self.advance(days=365)
        self.tick()
        seen = self.session()
        self.assertEqual(self.wait_of(wait)["ball"], "on_my_time")
        self.assertEqual((seen["ball"], seen["size"]), ("on_my_time", "M"))


class PrTests(Case):
    def test_pr_url_and_defaults(self) -> None:
        self.github()
        wait = self.add("--kind", "pr", "--pr", "https://example/7")
        self.assertEqual(wait["condition"], {"type": "pr", "repo": "o/r", "number": 7, "until": "ready"})
        self.assertEqual(wait["summary"], "PR#7 ready")
        self.assertEqual(
            [(pr["repo"], pr["number"], pr["source"]) for pr in self.session()["prs"]],
            [("o/r", 7, "wait")],
        )

    def test_bad_pr_reference(self) -> None:
        self.github()
        self.run_cli("add", "--session", SID, "--kind", "pr", "--pr", "nope", expect=2)
        self.run_cli("add", "--session", SID, "--kind", "pr", expect=2)

    def test_poll_memo_events_and_interval(self) -> None:
        calls = []

        def poll(cond, memo, **kwargs):
            calls.append(dict(memo))
            memo["seen"] = True
            return poll_result(
                memo, next_poll_s=600,
                events=[
                    {"key": "a", "text": "head moved", "on_me": False},
                    {"key": "b", "text": "check failed", "on_me": True},
                ] if len(calls) == 1 else [],
            )

        self.github(poll=poll)
        wait = self.add("--kind", "pr", "--repo", "o/r", "--number", "7", "--bots", "a,b")["id"]
        self.assertEqual(self.wait_of(wait)["condition"]["bots"], ["a", "b"])
        self.tick()
        seen = self.wait_of(wait)
        self.assertEqual((seen["state"], seen["ball"]), ("waiting", "on_me"))
        self.assertEqual(seen["reason"], "check failed")
        self.assertEqual(
            [item["text"] for item in self.session()["history"]], ["head moved", "check failed"],
        )
        self.advance(minutes=9)
        self.tick()
        self.assertEqual(len(calls), 1)
        self.advance(minutes=2)
        self.tick()
        self.assertEqual(calls, [{}, {"seen": True}])

    def test_fired(self) -> None:
        self.github(poll=lambda cond, memo, **kwargs: poll_result(
            memo, fired=True, fire_text="PR#7 merged",
        ))
        wait = self.add("--kind", "pr", "--repo", "o/r", "--number", "7", "--until", "merged")["id"]
        self.tick()
        self.assertEqual(self.wait_of(wait)["state"], "fired")
        self.assertEqual(self.session()["history"][-1]["text"], "PR#7 merged")

    def test_confirm_stays_watched_on_its_event(self) -> None:
        self.github(poll=lambda cond, memo, **kwargs: poll_result(
            memo, confirm=True,
            events=[{"key": "confirm:v1", "text": "v1 mentions #7", "on_me": True}],
        ))
        wait = self.add("--kind", "pr", "--repo", "o/r", "--number", "7", "--until", "released")["id"]
        self.tick()
        seen = self.wait_of(wait)
        self.assertEqual((seen["state"], seen["ball"]), ("waiting", "on_me"))
        self.assertEqual(seen["reason"], "v1 mentions #7")

    def test_five_hard_errors_but_not_transient_ones(self) -> None:
        results = iter(
            [poll_result({}, error="rate", transient=True)] * 5
            + [poll_result({}, error="404")] * 5
        )
        self.github(poll=lambda cond, memo, **kwargs: next(results))
        wait = self.add("--kind", "pr", "--repo", "o/r", "--number", "7")["id"]
        for _ in range(9):
            self.tick()
            self.advance(minutes=4)
        self.assertEqual(self.wait_of(wait)["state"], "waiting")
        self.tick()
        self.assertEqual(self.wait_of(wait)["state"], "needs_attention")

    def test_babysit_pr_shape(self) -> None:
        self.github()
        answer = self.run_cli(
            "babysit-pr", "--session", SID, "--repo", "o/r", "--number", "7",
            "--released", "--note", "ship it", "--park",
        )
        waits = answer["waits"]
        self.assertEqual(
            [wait["condition"]["until"] for wait in waits],
            ["review_on_head", "ready", "merged", "released"],
        )
        self.assertEqual([wait["state"] for wait in waits], ["waiting", "pending", "pending", "pending"])
        self.assertEqual([wait["next"] for wait in waits], [waits[1]["id"], waits[2]["id"], waits[3]["id"], None])
        self.assertTrue(waits[0]["has_prompt"])
        self.assertEqual(self.session()["ball"], "on_machine")
        self.assertEqual(self.session()["next_step"], "Ready")
        self.assertEqual([action["type"] for action in self.tick()["actions"]], ["park"])

    def test_babysit_pr_without_fix_or_release(self) -> None:
        self.github()
        waits = self.run_cli(
            "babysit-pr", "--session", SID, "--repo", "o/r", "--number", "7", "--no-fix",
        )["waits"]
        self.assertEqual(len(waits), 3)
        self.assertFalse(waits[0]["has_prompt"])

    def fix_chain(self, verify_fix) -> str:
        self.github(
            poll=lambda cond, memo, **kwargs: poll_result(
                memo, fired=cond["until"] == "review_on_head", fire_text="review landed",
            ),
            verify_fix=verify_fix,
        )
        self.agents(SID)
        self.heartbeat()
        first = self.run_cli(
            "babysit-pr", "--session", SID, "--repo", "o/r", "--number", "7",
        )["waits"][0]["id"]
        self.tick()
        return first

    def test_fix_prompt_is_filled_and_verified_against_the_snapshot(self) -> None:
        before = []

        def verify_fix(repo, number, snapshot):
            before.append(snapshot)
            return True, "new head"

        first = self.fix_chain(verify_fix)
        self.assertEqual(self.wait_of(first)["state"], "acting")
        prompt = self.run_cli("claim-prompts", "--session", SID)["prompts"][0]
        self.assertEqual(prompt["text"], "fix o/r#7 at abc123")
        self.run_cli("ack", "--action", prompt["action"], "--result", "delivered")
        self.run_cli("event", "--type", "turn_finished", "--session", SID)
        self.assertEqual(before, [])
        self.assertEqual(self.wait_of(first)["state"], "acting")
        view = self.tick()["view"]
        self.assertEqual(before, [{"head": "abc123", "threads": []}])
        self.assertEqual(
            [(wait["label"], wait["state"]) for wait in view["sessions"][0]["waits"]],
            [("Ready", "waiting"), ("Merged", "pending")],
        )

    def test_failed_fix_needs_attention_and_the_chain_stops(self) -> None:
        first = self.fix_chain(lambda repo, number, snapshot: (False, "head did not move"))
        prompt = self.run_cli("claim-prompts", "--session", SID)["prompts"][0]
        self.run_cli("ack", "--action", prompt["action"], "--result", "delivered")
        self.run_cli("event", "--type", "turn_finished", "--session", SID)
        self.tick()
        seen = self.session()
        self.assertEqual(self.wait_of(first)["state"], "needs_attention")
        self.assertEqual(self.wait_of(first)["reason"], "head did not move")
        self.assertEqual(seen["next_step"], "head did not move")
        self.assertEqual(seen["waits"][1]["state"], "pending")

    def test_a_new_head_clears_what_an_event_said_about_the_old_one(self) -> None:
        results = iter([
            poll_result({}, events=[{"key": "fail:a:ci", "text": "ci failed", "on_me": True}]),
            poll_result({}, events=[{"key": "head:b", "text": "new head b", "on_me": False}]),
        ])
        self.github(poll=lambda cond, memo, **kwargs: next(results))
        wait = self.add("--kind", "pr", "--repo", "o/r", "--number", "7")["id"]
        self.tick()
        self.assertEqual(self.wait_of(wait)["ball"], "on_me")
        self.advance(minutes=4)
        self.tick()
        seen = self.wait_of(wait)
        self.assertEqual((seen["ball"], seen["reason"]), ("on_machine", None))

    def test_fix_without_a_snapshot_goes_to_review(self) -> None:
        def snapshot(repo, number):
            raise RuntimeError("gh down")

        self.github(
            poll=lambda cond, memo, **kwargs: poll_result(memo, fired=True),
            snapshot=snapshot,
        )
        self.agents(SID)
        self.heartbeat()
        first = self.run_cli(
            "babysit-pr", "--session", SID, "--repo", "o/r", "--number", "7",
        )["waits"][0]["id"]
        self.tick()
        prompt = self.run_cli("claim-prompts", "--session", SID)["prompts"][0]
        self.assertEqual(prompt["text"], "fix o/r#7 at the current head")
        self.run_cli("ack", "--action", prompt["action"], "--result", "delivered")
        self.run_cli("event", "--type", "turn_finished", "--session", SID)
        self.assertEqual(self.wait_of(first)["state"], "review")

    def test_pr_verify_transient_errors_back_off_and_do_not_count(self) -> None:
        results = iter(
            [poll_result({}, error="flaky", transient=True)] * 6 + [poll_result({}, fired=True)]
        )
        self.github(poll=lambda cond, memo, **kwargs: next(results))
        self.agents(SID)
        self.heartbeat()
        verify = json.dumps({"type": "pr", "repo": "o/r", "number": 7, "until": "merged"})
        wait, _ = self.queued_prompt("--verify", verify)
        self.run_cli("event", "--type", "turn_finished", "--session", SID)
        self.tick()
        self.assertEqual(self.wait_of(wait)["state"], "acting")
        for _ in range(5):
            self.advance(minutes=4)
            self.tick()
        self.assertEqual(self.wait_of(wait)["state"], "acting")
        self.advance(minutes=4)
        self.tick()
        self.assertEqual(self.run_cli("list")["sessions"], [])

    def test_a_hard_verify_error_needs_attention_at_once(self) -> None:
        self.github(poll=lambda cond, memo, **kwargs: poll_result(memo, error="no such PR"))
        self.agents(SID)
        self.heartbeat()
        verify = json.dumps({"type": "pr", "repo": "o/r", "number": 7, "until": "merged"})
        wait, _ = self.queued_prompt("--verify", verify)
        self.run_cli("event", "--type", "turn_finished", "--session", SID)
        self.tick()
        self.assertEqual(self.wait_of(wait)["state"], "needs_attention")

    def test_gh_failure_during_a_fix_verify_is_retried(self) -> None:
        reads = iter(["down", "up"])

        def verify_fix(repo, number, before):
            if next(reads) == "down":
                raise FakeGhError("gh timed out", True)
            return True, "new head"

        first = self.fix_chain(verify_fix)
        action = self.run_cli("claim-prompts", "--session", SID)["prompts"][0]["action"]
        self.run_cli("ack", "--action", action, "--result", "delivered")
        self.run_cli("event", "--type", "turn_finished", "--session", SID)
        self.tick()
        self.assertEqual(self.wait_of(first)["state"], "acting")
        self.advance(minutes=4)
        self.tick()
        self.assertEqual(self.session()["waits"][0]["label"], "Ready")

    def test_an_old_verify_can_not_settle_a_new_attempt(self) -> None:
        self.agents(SID)
        self.heartbeat()
        verify = json.dumps({"type": "command", "argv": [sys.executable, "-c", "pass"]})
        wait, _ = self.queued_prompt("--verify", verify)
        self.run_cli("event", "--type", "turn_finished", "--session", SID)
        real = waiting._run_jobs

        def snooze_and_run_it_again(jobs, now):
            self.run_cli("snooze", "--session", SID, "--wait", wait, "--for", "1s")
            self.advance(seconds=2)
            self.tick()
            action = self.run_cli("claim-prompts", "--session", SID)["prompts"][0]["action"]
            self.run_cli("ack", "--action", action, "--result", "delivered")
            self.run_cli("event", "--type", "turn_finished", "--session", SID)
            return real(jobs, now)

        with mock.patch.object(waiting, "_run_jobs", snooze_and_run_it_again):
            self.tick()
        self.assertEqual(self.wait_of(wait)["state"], "acting")
        saved = self.state()["sessions"][SID]["waits"][0]
        self.assertTrue(saved["verify_pending"])
        self.assertIsNone(saved["verify_claim_until"])
        self.tick()
        self.assertEqual(self.run_cli("list")["sessions"], [])


class DeadlineTests(Case):
    def test_overdue_comes_to_me_and_stays_watched(self) -> None:
        wait = self.add("--kind", "time", "--in", "2d", "--deadline-in", "1d")["id"]
        self.advance(hours=23)
        self.tick()
        self.assertFalse(self.wait_of(wait)["overdue"])
        self.advance(hours=2)
        self.tick()
        seen = self.wait_of(wait)
        self.assertEqual((seen["overdue"], seen["ball"], seen["state"]), (True, "on_me", "waiting"))
        self.assertEqual(len(self.tick()["actions"]), 1)
        self.advance(days=2)
        self.tick()
        seen = self.wait_of(wait)
        self.assertEqual((seen["overdue"], seen["state"]), (False, "fired"))

    def test_overdue_command_keeps_running(self) -> None:
        wait = self.add(
            "--kind", "command", "--argv", json.dumps([sys.executable, "-c", "raise SystemExit(1)"]),
            "--interval", "1m", "--deadline", (T0 + timedelta(minutes=30)).isoformat(),
        )["id"]
        self.advance(hours=1)
        self.tick()
        self.assertTrue(self.wait_of(wait)["overdue"])
        self.assertLess(
            waiting._parse_time(T0.isoformat()),
            waiting._parse_time(self.state()["sessions"][SID]["waits"][0]["next_check_at"]),
        )

    def test_snooze_overdue_moves_the_deadline(self) -> None:
        wait = self.add("--kind", "job", "--pane", "51", "--deadline-in", "1h")["id"]
        self.advance(hours=2)
        self.tick()
        self.run_cli("snooze", "--session", SID, "--wait", wait, "--for", "1d")
        seen = self.wait_of(wait)
        self.assertEqual((seen["overdue"], seen["ball"]), (False, "on_machine"))
        self.assertEqual(seen["deadline"], (self.clock + timedelta(days=1)).isoformat())

    def test_snooze_time_and_something(self) -> None:
        timer = self.add("--kind", "time", "--in", "1h")["id"]
        something = self.add("--kind", "something", "--what", "x", "--deadline-in", "1h")["id"]
        self.advance(hours=2)
        self.tick()
        for wait in (timer, something):
            self.run_cli("snooze", "--session", SID, "--wait", wait, "--for", "1d")
        self.assertEqual(self.wait_of(timer)["state"], "waiting")
        self.assertEqual(self.wait_of(timer)["ball"], "on_timer")
        self.assertEqual(self.wait_of(something)["ball"], "on_someone")
        self.advance(days=1)
        self.tick()
        self.assertEqual(self.wait_of(timer)["state"], "fired")
        self.assertEqual(self.wait_of(something)["ball"], "on_me")

    def test_snooze_pushes_a_deadline_still_ahead_of_something(self) -> None:
        wait = self.add("--kind", "something", "--what", "x", "--deadline-in", "1h")["id"]
        self.run_cli("snooze", "--session", SID, "--wait", wait, "--for", "1d")
        self.assertEqual(self.wait_of(wait)["deadline"], (self.clock + timedelta(days=1)).isoformat())

    def test_snooze_gives_something_a_time_to_come_back(self) -> None:
        wait = self.add("--kind", "something", "--what", "x")["id"]
        self.run_cli("snooze", "--session", SID, "--wait", wait, "--for", "1d")
        self.assertEqual(self.wait_of(wait)["ball"], "on_someone")
        self.advance(days=1)
        self.tick()
        self.assertEqual(self.wait_of(wait)["ball"], "on_me")

    def test_snooze_brings_a_finished_job_back(self) -> None:
        wait = self.add("--kind", "job", "--pane", "51")["id"]
        self.run_cli("event", "--type", "job_finished", "--pane", "51", "--status", "0")
        self.run_cli("snooze", "--session", SID, "--wait", wait, "--for", "1h")
        self.assertEqual(self.wait_of(wait)["ball"], "on_timer")
        self.advance(hours=2)
        self.tick()
        self.assertEqual(self.wait_of(wait)["state"], "fired")


class SnoozeTests(Case):
    """Snooze 1d: a wait still waiting is watched again from the new time; one whose
    event happened is hidden until then and comes back as it was."""

    def snooze(self, wait: str, expect: int = 0) -> dict:
        return self.run_cli("snooze", "--session", SID, "--wait", wait, "--for", "1d", expect=expect)

    def states(self) -> dict:
        return {wait["id"]: wait["state"] for wait in self.state()["sessions"][SID]["waits"]}

    def ran_prompt(self, *argv: str) -> str:
        """A command wait whose prompt the agent ran: it waits for my look."""
        self.agents(SID)
        self.heartbeat()
        wait = self.add(
            "--kind", "command", "--argv", json.dumps([sys.executable, "-c", "pass"]),
            "--prompt", "go", *argv,
        )["id"]
        self.tick()
        action = self.run_cli("claim-prompts", "--session", SID)["prompts"][0]["action"]
        self.run_cli("ack", "--action", action, "--result", "delivered")
        return wait

    def test_a_snoozed_job_no_longer_watches_its_pane(self) -> None:
        wait = self.add("--kind", "job", "--pane", "51", "--label", "Build")["id"]
        self.run_cli("event", "--type", "job_finished", "--pane", "51", "--status", "0")
        self.snooze(wait)
        self.run_cli("event", "--type", "job_finished", "--pane", "51", "--status", "3")
        self.run_cli("event", "--type", "pane_closed", "--pane", "51")
        self.assertEqual(self.states()[wait], "snoozed")
        self.advance(days=1)
        self.tick()
        seen = self.wait_of(wait)
        self.assertEqual((seen["state"], seen["fired_text"]), ("fired", "Build: finished"))

    def test_a_job_whose_pane_closed_comes_back_as_it_was(self) -> None:
        wait = self.add("--kind", "job", "--pane", "51")["id"]
        self.run_cli("event", "--type", "pane_closed", "--pane", "51")
        self.snooze(wait)
        self.assertIsNone(self.wait_of(wait)["outcome"])
        self.advance(days=1)
        self.tick()
        seen = self.wait_of(wait)
        self.assertEqual(
            (seen["state"], seen["reason"]), ("needs_attention", "Pane closed before the job finished"),
        )

    def test_an_overdue_job_whose_pane_closed_is_hidden_too(self) -> None:
        wait = self.add("--kind", "job", "--pane", "51", "--deadline-in", "1h")["id"]
        self.advance(hours=2)
        self.tick()
        self.run_cli("event", "--type", "pane_closed", "--pane", "51")
        self.snooze(wait)
        seen = self.wait_of(wait)
        self.assertEqual((seen["state"], seen["ball"], seen["overdue"]), ("snoozed", "on_timer", False))
        self.advance(days=1)
        self.tick()
        seen = self.wait_of(wait)
        self.assertEqual(
            (seen["state"], seen["reason"]), ("needs_attention", "Pane closed before the job finished"),
        )

    def test_a_prompt_that_ran_does_not_run_again(self) -> None:
        wait = self.ran_prompt()
        self.run_cli("event", "--type", "turn_finished", "--session", SID)
        self.assertEqual(self.states()[wait], "review")
        self.snooze(wait)
        self.advance(days=1)
        self.tick()
        self.assertEqual(self.states()[wait], "review")
        self.assertEqual(self.state()["sessions"][SID]["prompt_queue"], [])

    def test_a_prompt_the_agent_runs_is_not_snoozed(self) -> None:
        wait = self.ran_prompt()
        self.assertIn("runs its prompt", self.snooze(wait, expect=2)["error"])
        self.assertEqual(self.states()[wait], "acting")

    def test_a_check_that_kept_failing_is_watched_again(self) -> None:
        wait = self.add(
            "--kind", "command", "--argv", json.dumps([sys.executable, "-c", "pass"]),
            "--predicate", "json_eq:a=1", "--interval", "1m",
        )["id"]
        for _ in range(waiting.MAX_ERRORS):
            self.tick()
            self.advance(minutes=2)
        self.assertEqual(self.states()[wait], "needs_attention")
        self.snooze(wait)
        saved = self.state()["sessions"][SID]["waits"][0]
        self.assertEqual((saved["state"], saved["errors"]), ("waiting", 0))
        self.assertEqual(saved["next_check_at"], (self.clock + timedelta(days=1)).isoformat())

    def test_a_fired_chain_step_keeps_one_step_armed(self) -> None:
        first = self.add("--kind", "time", "--in", "1s")["id"]
        second = self.add("--kind", "time", "--in", "1h", "--after", first)["id"]
        self.advance(seconds=2)
        self.tick()
        self.snooze(first)
        self.assertEqual(self.states(), {first: "snoozed", second: "waiting"})
        self.assertEqual(len(self.session()["threads"]), 1)
        self.advance(days=1)
        self.tick()
        self.assertEqual(self.states(), {first: "fired", second: "fired"})

    def test_a_snooze_that_changes_nothing_is_refused(self) -> None:
        later = self.add("--kind", "later")["id"]
        job = self.add("--kind", "job", "--pane", "51")["id"]
        for wait in (later, job):
            self.snooze(wait, expect=2)
        self.assertNotIn("Snoozed", " ".join(item["text"] for item in self.session()["history"]))

    def test_the_view_offers_snooze_exactly_where_snooze_takes_it(self) -> None:
        later = self.add("--kind", "later")["id"]
        job = self.add("--kind", "job", "--pane", "51")["id"]
        first = self.add("--kind", "time", "--in", "1h")["id"]
        step = self.add("--kind", "time", "--in", "1h", "--after", first)["id"]
        acting = self.ran_prompt()
        offered = {wait["id"]: wait["snoozable"] for wait in self.session()["waits"]}
        self.assertEqual(offered, {later: False, job: False, first: True, step: False, acting: False})
        for wait, snoozable in offered.items():
            self.snooze(wait, expect=0 if snoozable else 2)
        # A job that ended can be hidden; so can a pane's wait.
        self.run_cli("event", "--type", "job_finished", "--pane", "51", "--status", "0")
        self.assertTrue(self.wait_of(job)["snoozable"])
        self.run_cli("add", "--pane", "52", "--kind", "something", "--what", "deploy")
        pane = self.run_cli("list")["panes"][0]
        self.assertEqual([wait["snoozable"] for wait in pane["waits"]], [True])


class NoteTests(Case):
    def test_user_sets_note(self) -> None:
        self.add("--kind", "later", "--note", "first")
        self.add("--kind", "later", "--note", "second")
        self.assertEqual(self.session()["note"], "second")

    def test_agent_fills_an_empty_note(self) -> None:
        self.add("--kind", "later", "--note", "theirs", "--by", "agent")
        self.assertEqual(self.session()["note"], "theirs")
        self.assertIsNone(self.session()["suggested_note"])

    def test_agent_never_overwrites(self) -> None:
        self.add("--kind", "later", "--note", "mine")
        self.add("--kind", "later", "--note", "theirs", "--by", "agent")
        seen = self.session()
        self.assertEqual((seen["note"], seen["suggested_note"]), ("mine", "theirs"))
        self.run_cli("note", "--session", SID, "--accept-suggested")
        seen = self.session()
        self.assertEqual((seen["note"], seen["suggested_note"]), ("theirs", None))

    def test_note_command(self) -> None:
        self.add("--kind", "later")
        self.run_cli("note", "--session", SID, "--text", "later on")
        self.assertEqual(self.session()["note"], "later on")
        self.run_cli("note", "--session", SID, expect=2)
        self.run_cli("note", "--session", SID, "--accept-suggested", expect=2)

    def test_a_note_needs_something_set_aside(self) -> None:
        self.run_cli("note", "--session", SID, "--text", "forgotten", expect=2)
        self.run_cli("note", "--pane", "51", "--text", "forgotten", expect=2)
        self.assertFalse((self.state_dir / "waiting.json").exists())
        wait = self.add("--kind", "time", "--in", "1h", "--note", "old")["id"]
        self.run_cli("stop", "--session", SID, "--wait", wait)
        self.run_cli("note", "--session", SID, "--text", "new", expect=2)

    def test_a_pinned_session_takes_a_note_without_a_wait(self) -> None:
        (self.omp / "session-pins.json").write_text(json.dumps([SID]))
        self.run_cli("note", "--session", SID, "--text", "for later")
        seen = self.session()
        self.assertEqual((seen["note"], seen["ball"], seen["waits"]), ("for later", "on_my_time", []))

    def test_a_new_set_aside_does_not_inherit_the_old_sentence(self) -> None:
        wait = self.add("--kind", "later", "--note", "old")["id"]
        self.run_cli("stop", "--session", SID, "--wait", wait)
        self.add("--kind", "later")
        seen = self.session()
        self.assertIsNone(seen["note"])
        self.assertEqual(seen["history"], [])

    def test_note_on_a_pane_wait(self) -> None:
        self.run_cli("add", "--pane", "51", "--kind", "something", "--what", "x")
        pane = self.run_cli("note", "--pane", "51", "--text", "first")["view"]["panes"][0]
        self.assertEqual(pane["note"], "first")


class MenuRequestTests(Case):
    def request(self, sid: str = SID) -> dict:
        return self.run_cli("menu-request", "--session", sid)

    def assert_refused(self, reason: str) -> None:
        answer = self.request()
        self.assertEqual((answer["menu"], answer["reason"]), (False, reason))
        self.assertNotIn("view", answer)
        self.assertEqual(self.run_cli("list")["menu_requests"], {})
        self.assertEqual(self.state()["menu_requests"], {})

    def test_a_fresh_menu_heartbeat_stores_the_request(self) -> None:
        self.heartbeat(SID, "--features", "menu")
        answer = self.request()
        self.assertTrue(answer["menu"])
        first = answer["view"]["menu_requests"][SID]
        self.assertEqual(first["at"], self.clock.isoformat())
        written = json.loads((self.state_dir / "waiting_view.json").read_text())
        self.assertEqual(written["menu_requests"], {SID: first})
        self.advance(seconds=1)
        second = self.request()["view"]["menu_requests"][SID]
        self.assertGreater(second["seq"], first["seq"])

    def test_a_heartbeat_without_features_is_an_older_extension(self) -> None:
        self.heartbeat()
        self.assert_refused(waiting.REASON_MENU_OLD_EXTENSION)

    def test_the_latest_heartbeat_decides_the_features(self) -> None:
        self.heartbeat(SID, "--features", "menu")
        self.heartbeat()
        self.assert_refused(waiting.REASON_MENU_OLD_EXTENSION)

    def test_a_stale_heartbeat_is_no_extension(self) -> None:
        self.heartbeat(SID, "--features", "menu")
        self.advance(seconds=waiting.EXTENSION_FRESH_S + 1)
        self.assert_refused(waiting.REASON_MENU_NO_EXTENSION)

    def test_no_heartbeat_is_no_extension(self) -> None:
        self.heartbeat(OTHER, "--features", "menu")
        self.assert_refused(waiting.REASON_MENU_NO_EXTENSION)

    def test_the_view_drops_a_request_after_15_seconds(self) -> None:
        self.heartbeat(SID, "--features", "menu")
        self.request()
        self.advance(seconds=14)
        self.assertIn(SID, self.run_cli("list")["menu_requests"])
        self.advance(seconds=1)
        self.assertEqual(self.run_cli("list")["menu_requests"], {})
        self.tick()
        self.assertEqual(self.state()["menu_requests"], {})

    def test_a_request_stamped_ahead_of_the_clock_is_not_fresh(self) -> None:
        self.heartbeat(SID, "--features", "menu")
        self.request()
        self.advance(seconds=-1)
        self.assertEqual(self.run_cli("list")["menu_requests"], {})

    def test_seq_keeps_growing_past_an_expired_request(self) -> None:
        self.heartbeat(SID, "--features", "menu")
        self.heartbeat(OTHER, "--features", "menu")
        first = self.request()["view"]["menu_requests"][SID]["seq"]
        self.advance(seconds=20)
        self.tick()
        self.assertGreater(self.request(OTHER)["view"]["menu_requests"][OTHER]["seq"], first)

    def test_features_leave_with_the_extension_record(self) -> None:
        self.heartbeat(SID, "--features", "menu")
        self.advance(seconds=waiting.EXTENSION_KEPT_S + 1)
        self.tick()
        self.assertEqual(self.state()["extension_features"], {})

    def test_a_state_file_from_before_the_menu_still_loads(self) -> None:
        self.heartbeat()
        state = self.state()
        for field in ("extension_features", "extension_panes", "menu_seq", "menu_requests"):
            del state[field]
        (self.state_dir / "waiting.json").write_text(json.dumps(state))
        self.heartbeat(SID, "--features", "menu")
        self.assertTrue(self.request()["menu"])

    def test_a_heartbeat_names_the_pane_and_the_menu(self) -> None:
        self.heartbeat(SID, "--pane", "52", "--features", "menu")
        self.heartbeat(OTHER, "--pane", "53")
        self.assertEqual(self.run_cli("list")["extension_panes"], {
            "52": {"session": SID, "menu": True}, "53": {"session": OTHER, "menu": False},
        })

    def test_a_newer_session_takes_the_pane(self) -> None:
        self.heartbeat(SID, "--pane", "52", "--features", "menu")
        self.heartbeat(OTHER, "--pane", "52", "--features", "menu")
        self.assertEqual(
            self.run_cli("list")["extension_panes"], {"52": {"session": OTHER, "menu": True}},
        )
        self.assertEqual(self.state()["extension_panes"], {OTHER: 52})
        self.assertEqual(self.run_cli("menu-request", "--pane", "52")["session"], OTHER)

    def test_a_session_keeps_only_its_latest_pane(self) -> None:
        self.heartbeat(SID, "--pane", "52")
        self.heartbeat(SID, "--pane", "53")
        self.assertEqual(list(self.run_cli("list")["extension_panes"]), ["53"])

    def test_a_heartbeat_without_a_pane_keeps_the_last_one(self) -> None:
        self.heartbeat(SID, "--pane", "52")
        self.heartbeat(SID)
        self.assertEqual(list(self.run_cli("list")["extension_panes"]), ["52"])

    def test_a_stale_heartbeat_leaves_the_pane(self) -> None:
        self.heartbeat(SID, "--pane", "52", "--features", "menu")
        self.advance(seconds=waiting.EXTENSION_FRESH_S + 1)
        self.assertEqual(self.run_cli("list")["extension_panes"], {})
        answer = self.run_cli("menu-request", "--pane", "52")
        self.assertEqual((answer["menu"], answer["reason"]), (False, waiting.REASON_MENU_NO_PANE))

    def test_the_pane_leaves_with_the_extension_record(self) -> None:
        self.heartbeat(SID, "--pane", "52")
        self.advance(seconds=waiting.EXTENSION_KEPT_S + 1)
        self.tick()
        self.assertEqual(self.state()["extension_panes"], {})

    def test_menu_request_by_pane(self) -> None:
        self.heartbeat(SID, "--pane", "52", "--features", "menu")
        answer = self.run_cli("menu-request", "--pane", "52")
        self.assertTrue(answer["menu"])
        self.assertEqual(answer["session"], SID)
        self.assertIn(SID, answer["view"]["menu_requests"])

    def test_menu_request_by_pane_refuses_an_empty_pane_and_an_old_extension(self) -> None:
        self.heartbeat(SID, "--pane", "52")
        empty = self.run_cli("menu-request", "--pane", "99")
        self.assertEqual((empty["menu"], empty["reason"]), (False, "no omp extension in this pane"))
        old = self.run_cli("menu-request", "--pane", "52")
        self.assertEqual(
            (old["menu"], old["session"], old["reason"]),
            (False, SID, waiting.REASON_MENU_OLD_EXTENSION),
        )
        self.assertEqual(self.state()["menu_requests"], {})

    def test_menu_request_needs_one_target(self) -> None:
        self.run_cli("menu-request", expect=2)
        self.run_cli("menu-request", "--session", SID, "--pane", "52", expect=2)

    def test_a_session_gets_the_reported_pane_only_without_a_tracked_one(self) -> None:
        self.add("--kind", "later")
        self.heartbeat(SID, "--pane", "52")
        self.assertEqual(self.session()["pane"], 52)
        self.agents(SID)
        self.assertEqual(self.session()["pane"], 40)
        self.agents()
        self.advance(seconds=waiting.EXTENSION_FRESH_S + 1)
        self.assertIsNone(self.session()["pane"])

    def test_a_session_that_ended_gives_its_pane_back(self) -> None:
        self.heartbeat(SID, "--pane", "52", "--features", "menu")
        self.advance(seconds=5)
        self.agents(closed=[{"sessionId": SID, "cwd": "/w", "windowTitle": "x"}])
        self.assertEqual(self.run_cli("list")["extension_panes"], {})
        answer = self.run_cli("menu-request", "--pane", "52")
        self.assertEqual((answer["menu"], answer["reason"]), (False, waiting.REASON_MENU_NO_PANE))

    def swept(self, boot: str) -> None:
        """agents.json of `boot`, swept now, listing neither session."""
        self.agents(boot=boot)
        payload = json.loads((self.state_dir / "agents.json").read_text())
        payload["updatedAt"] = int(self.clock.timestamp() * 1000)
        (self.state_dir / "agents.json").write_text(json.dumps(payload))

    def test_a_reboot_drops_a_fresh_report_from_before_it(self) -> None:
        self.swept("one")
        self.tick()
        self.advance(seconds=5)
        self.heartbeat(SID, "--pane", "52", "--features", "menu")
        self.advance(seconds=5)
        self.swept("two")
        self.tick()
        self.assertEqual(self.run_cli("list")["extension_panes"], {})
        answer = self.run_cli("menu-request", "--pane", "52")
        self.assertEqual((answer["menu"], answer["reason"]), (False, waiting.REASON_MENU_NO_PANE))

    def test_a_report_after_the_reboots_sweep_stays(self) -> None:
        self.swept("one")
        self.tick()
        self.advance(seconds=5)
        self.swept("two")
        self.advance(seconds=5)
        self.heartbeat(SID, "--pane", "52", "--features", "menu")
        self.tick()
        self.assertEqual(list(self.run_cli("list")["extension_panes"]), ["52"])

    def test_a_report_since_the_boot_stays_before_the_sweep(self) -> None:
        self.swept("one")
        self.system_boot = "one"
        self.tick()
        self.heartbeat(OTHER, "--pane", "51", "--features", "menu")
        self.system_boot = "two"
        self.booted = self.clock.timestamp() + 1
        self.advance(seconds=5)
        self.heartbeat(SID, "--pane", "52", "--features", "menu")
        self.tick()  # agents.json is still the last boot's
        self.assertEqual(list(self.run_cli("list")["extension_panes"]), ["52"])

    @unittest.skipUnless(HAS_PROC, "dates a daemon by its /proc start time")
    def test_a_new_daemon_keeps_the_panes_reported_since_it_started(self) -> None:
        self.tick("--daemon", "999999999-1")  # no such process
        self.heartbeat(OTHER, "--pane", "51", "--features", "menu")
        self.advance(seconds=1)
        # The new daemon starts now, 100 s into the boot.
        self.booted = self.clock.timestamp() - 100
        self.advance(seconds=1)
        self.heartbeat(SID, "--pane", "52", "--features", "menu")
        self.tick("--daemon", f"4242-{100 * os.sysconf('SC_CLK_TCK')}")
        self.assertEqual(list(self.run_cli("list")["extension_panes"]), ["52"])

    def test_a_resumed_session_keeps_its_pane_before_the_sweep_sees_it(self) -> None:
        self.agents(closed=[{"sessionId": SID, "cwd": "/w", "windowTitle": "x"}])
        payload = json.loads((self.state_dir / "agents.json").read_text())
        payload["updatedAt"] = int(self.clock.timestamp() * 1000)
        (self.state_dir / "agents.json").write_text(json.dumps(payload))
        self.advance(seconds=5)
        self.heartbeat(SID, "--pane", "52")
        self.assertEqual(list(self.run_cli("list")["extension_panes"]), ["52"])


class ChainTests(Case):
    def test_clearing_the_status_activates_after(self) -> None:
        first = self.add("--kind", "something", "--what", "x")["id"]
        second = self.add("--kind", "time", "--in", "1h", "--after", first)
        self.assertEqual(second["state"], "pending")
        self.assertIsNone(second["due_at"])
        self.assertEqual(self.wait_of(first)["next"], second["id"])
        self.advance(hours=2)
        self.tick()
        self.assertEqual(self.wait_of(second["id"])["state"], "pending")
        self.run_cli("what", "--session", SID, "--text", "")
        seen = self.wait_of(second["id"])
        self.assertEqual(seen["state"], "waiting")
        self.assertEqual(seen["condition"]["at"], (self.clock + timedelta(hours=1)).isoformat())

    def test_stopping_a_prerequisite_stops_the_steps_behind_it(self) -> None:
        first = self.add("--kind", "command", "--argv", '["true"]', "--label", "Archive sessions")["id"]
        second = self.add("--kind", "time", "--in", "1h", "--after", first, "--label", "Forget chats")["id"]
        third = self.add("--kind", "later", "--after", second, "--label", "Tidy up")["id"]
        self.run_cli("stop", "--session", SID, "--wait", first)
        self.assertEqual(self.session()["waits"], [])
        steps = {wait["id"]: wait for wait in self.state()["sessions"][SID]["waits"]}
        self.assertEqual([steps[wait]["state"] for wait in (first, second, third)], ["done"] * 3)
        self.assertIsNone(steps[second]["armed_at"])
        history = [entry["text"] for entry in self.state()["sessions"][SID]["history"]]
        self.assertEqual(
            history[-3:], ["Stopped: Archive sessions", "Stopped: Forget chats", "Stopped: Tidy up"],
        )

    def test_firing_without_a_prompt_moves_the_chain_on(self) -> None:
        first = self.add("--kind", "time", "--in", "1h")["id"]
        second = self.add("--kind", "something", "--what", "x", "--after", first)["id"]
        self.advance(hours=1)
        self.tick()
        self.assertEqual(self.wait_of(first)["state"], "fired")
        self.assertEqual(self.wait_of(second)["state"], "waiting")
        self.run_cli("mark-seen", "--session", SID, "--wait", first)
        self.assertEqual(self.wait_of(second)["state"], "waiting")

    def test_after_a_fired_wait_starts_at_once(self) -> None:
        first = self.add("--kind", "time", "--in", "1s")["id"]
        self.advance(seconds=2)
        self.tick()
        second = self.add("--kind", "later", "--after", first)
        self.assertEqual(second["state"], "waiting")

    def test_after_appends_to_the_chain(self) -> None:
        first = self.add("--kind", "later")["id"]
        second = self.add("--kind", "later", "--after", first)["id"]
        third = self.add("--kind", "later", "--after", first)["id"]
        self.assertEqual(self.wait_of(first)["next"], second)
        self.assertEqual(self.wait_of(second)["next"], third)

    def test_stopping_a_step_mid_chain_leaves_the_steps_before_it(self) -> None:
        first = self.add("--kind", "time", "--in", "1h")["id"]
        second = self.add("--kind", "later", "--after", first)["id"]
        third = self.add("--kind", "later", "--after", second)["id"]
        self.run_cli("stop", "--session", SID, "--wait", second)
        self.assertEqual([wait["id"] for wait in self.session()["waits"]], [first])
        self.assertEqual(self.wait_of(first)["state"], "waiting")
        # The step before still runs; nothing is left behind it to start.
        self.advance(hours=1)
        self.tick()
        self.assertEqual(self.wait_of(first)["state"], "fired")
        self.assertEqual([wait["id"] for wait in self.session()["waits"]], [first])
        self.assertEqual(self.state()["sessions"][SID]["waits"][2]["id"], third)
        self.assertEqual(self.state()["sessions"][SID]["waits"][2]["state"], "done")

    def test_stopping_a_step_that_started_leaves_it_running(self) -> None:
        first = self.add("--kind", "time", "--in", "1h")["id"]
        second = self.add("--kind", "later", "--after", first)["id"]
        self.advance(hours=1)
        self.tick()
        self.assertEqual(self.wait_of(second)["state"], "waiting")
        self.run_cli("stop", "--session", SID, "--wait", first)
        self.assertEqual(self.wait_of(second)["state"], "waiting")

    def test_unknown_after(self) -> None:
        self.run_cli("add", "--session", SID, "--kind", "later", "--after", "w9", expect=2)

    def test_stop_all_leaves_the_view(self) -> None:
        first = self.add("--kind", "later")["id"]
        self.add("--kind", "later", "--after", first)
        view = self.run_cli("stop", "--session", SID, "--all")["view"]
        self.assertEqual(view["sessions"], [])

    def test_remove(self) -> None:
        self.add("--kind", "later")
        self.run_cli("remove", "--session", SID)
        self.assertEqual(self.state()["sessions"], {})


class PromptTests(Case):
    def setUp(self) -> None:
        super().setUp()
        self.agents(SID)
        self.heartbeat()

    def test_fifo_and_resend_check_after_a_crash(self) -> None:
        first = self.add("--kind", "time", "--in", "1s", "--prompt", "one")["id"]
        second = self.add("--kind", "time", "--in", "1s", "--prompt", "two")["id"]
        self.advance(seconds=2)
        view = self.tick()["view"]
        self.assertEqual(view["sessions"][0]["prompts_queued"], 2)
        self.assertEqual(self.wait_of(first)["state"], "acting")
        self.assertEqual(self.wait_of(first)["ball"], "on_machine")

        claimed = self.run_cli("claim-prompts", "--session", SID)
        prompt = claimed["prompts"][0]
        self.assertEqual(len(claimed["prompts"]), 1)
        self.assertEqual((prompt["wait"], prompt["text"], prompt["resend_check"]), (first, "one", False))
        self.assertIsNone(prompt["claimed_at"])
        claimed_at = self.clock.isoformat()
        self.assertEqual(claimed["view"]["sessions"][0]["prompts_queued"], 0)

        # The extension died between send and ack: its next run must look first.
        self.advance(minutes=3)
        self.assertEqual(self.session()["prompts_queued"], 2)
        again = self.run_cli("claim-prompts", "--session", SID)["prompts"][0]
        self.assertEqual((again["action"], again["resend_check"]), (prompt["action"], True))
        # The resend check looks only at messages since that earlier claim.
        self.assertEqual(again["claimed_at"], claimed_at)

        self.run_cli("ack", "--action", prompt["action"], "--result", "delivered")
        self.assertEqual(self.run_cli("claim-prompts", "--session", SID)["prompts"], [])
        self.run_cli("event", "--type", "turn_finished", "--session", SID)
        following = self.run_cli("claim-prompts", "--session", SID)["prompts"][0]
        self.assertEqual((following["wait"], following["text"]), (second, "two"))

    def test_ack_failed_needs_attention(self) -> None:
        wait = self.add("--kind", "time", "--in", "1s", "--prompt", "go")["id"]
        self.advance(seconds=2)
        self.tick()
        action = self.run_cli("claim-prompts", "--session", SID)["prompts"][0]["action"]
        self.run_cli("ack", "--action", action, "--result", "failed", "--error", "no route")
        seen = self.wait_of(wait)
        self.assertEqual((seen["state"], seen["reason"], seen["ball"]), ("needs_attention", "no route", "on_me"))
        self.assertEqual(self.session()["prompts_queued"], 0)

    def test_repeated_delivered_ack_logs_once(self) -> None:
        _, action = self.queued_prompt()
        before = len(self.session()["history"])
        self.assertTrue(self.run_cli("ack", "--action", action, "--result", "delivered")["ignored"])
        self.assertEqual(len(self.session()["history"]), before)

    def test_ack_is_idempotent_and_checks_the_result(self) -> None:
        wait, action = self.queued_prompt()
        self.run_cli("ack", "--action", action, "--result", "done", expect=2)
        self.run_cli("stop", "--session", SID, "--wait", wait)
        self.assertTrue(self.run_cli("ack", "--action", action, "--result", "delivered")["ignored"])

    def test_turn_without_verify_goes_to_review_then_seen_moves_on(self) -> None:
        wait, _ = self.queued_prompt()
        following = self.add("--kind", "later", "--after", wait)["id"]
        self.run_cli("event", "--type", "turn_finished", "--session", SID)
        seen = self.wait_of(wait)
        self.assertEqual((seen["state"], seen["ball"], seen["reason"]), ("review", "on_me", "ran, take a look"))
        self.assertEqual(self.wait_of(following)["state"], "pending")
        self.run_cli("mark-seen", "--session", SID, "--wait", wait)
        self.assertEqual(self.wait_of(following)["state"], "waiting")

    def test_turn_with_verify_ok_finishes_and_moves_on(self) -> None:
        verify = json.dumps({"type": "command", "argv": [sys.executable, "-c", "pass"]})
        wait, _ = self.queued_prompt("--verify", verify)
        following = self.add("--kind", "later", "--after", wait)["id"]
        self.run_cli("event", "--type", "turn_finished", "--session", SID)
        self.assertEqual(self.wait_of(wait)["state"], "acting")
        self.assertEqual(self.wait_of(following)["state"], "pending")
        self.tick()
        self.assertEqual([item["id"] for item in self.session()["waits"]], [following])
        self.assertEqual(self.wait_of(following)["state"], "waiting")

    def test_turn_with_verify_fail_needs_attention(self) -> None:
        verify = json.dumps({"type": "command", "argv": [sys.executable, "-c", "raise SystemExit(1)"]})
        wait, _ = self.queued_prompt("--verify", verify)
        following = self.add("--kind", "later", "--after", wait)["id"]
        self.run_cli("event", "--type", "turn_finished", "--session", SID)
        self.tick()
        self.assertEqual(self.wait_of(wait)["state"], "needs_attention")
        self.assertEqual(self.wait_of(following)["state"], "pending")

    def test_verify_left_behind_by_a_crash_runs_in_tick(self) -> None:
        verify = json.dumps({"type": "command", "argv": [sys.executable, "-c", "pass"]})
        wait, _ = self.queued_prompt("--verify", verify)
        self.run_cli("event", "--type", "turn_finished", "--session", SID)
        with mock.patch.object(waiting, "_run_jobs", lambda jobs, now: []):
            self.tick()
        self.assertEqual(self.wait_of(wait)["state"], "acting")
        self.tick()
        self.assertEqual(self.wait_of(wait)["state"], "acting")
        self.advance(minutes=6)
        self.tick()
        self.assertEqual(self.run_cli("list")["sessions"], [])

    def test_a_turn_before_delivery_settles_nothing(self) -> None:
        wait = self.add("--kind", "time", "--in", "1s", "--prompt", "go")["id"]
        self.advance(seconds=2)
        self.tick()
        self.run_cli("event", "--type", "turn_finished", "--session", SID)
        self.assertEqual(self.wait_of(wait)["state"], "acting")

    def test_a_question_holds_the_queued_prompt_until_the_turn_ends(self) -> None:
        wait = self.add("--kind", "time", "--in", "1s", "--prompt", "go")["id"]
        self.advance(seconds=2)
        self.tick()
        self.run_cli("event", "--type", "waiting_input", "--session", SID)
        self.assertEqual(self.wait_of(wait)["reason"], "agent asks a question")
        self.assertEqual(self.session()["prompts_queued"], 0)
        self.assertEqual(self.run_cli("claim-prompts", "--session", SID)["prompts"], [])
        self.run_cli("event", "--type", "turn_finished", "--session", SID)
        self.assertEqual(self.wait_of(wait)["ball"], "on_machine")
        self.assertEqual(self.session()["prompts_queued"], 1)
        self.assertEqual(self.run_cli("claim-prompts", "--session", SID)["prompts"][0]["wait"], wait)

    def test_waiting_input_puts_an_acting_wait_on_me(self) -> None:
        wait, _ = self.queued_prompt()
        self.run_cli("event", "--type", "waiting_input", "--session", SID)
        seen = self.wait_of(wait)
        self.assertEqual((seen["state"], seen["ball"], seen["reason"]), ("acting", "on_me", "agent asks a question"))
        self.run_cli("event", "--type", "turn_finished", "--session", SID)
        self.assertEqual(self.wait_of(wait)["state"], "review")

    def test_closed_session_keeps_the_prompt_for_after_resume(self) -> None:
        wait = self.add("--kind", "time", "--in", "1s", "--prompt", "go")["id"]
        self.agents()
        self.advance(seconds=2)
        self.tick()
        seen = self.wait_of(wait)
        self.assertEqual((seen["state"], seen["ball"]), ("acting", "on_me"))
        self.assertEqual(seen["reason"], waiting.REASON_CLOSED)
        self.assertEqual(self.session()["prompts_queued"], 1)
        self.agents(SID)
        self.heartbeat()
        seen = self.wait_of(wait)
        self.assertEqual((seen["ball"], seen["reason"]), ("on_machine", None))
        self.assertEqual(self.run_cli("claim-prompts", "--session", SID)["prompts"][0]["text"], "go")

    def test_live_without_extension(self) -> None:
        wait = self.add("--kind", "time", "--in", "1s", "--prompt", "go")["id"]
        self.tick_through(minutes=20)
        seen = self.wait_of(wait)
        self.assertEqual((seen["ball"], seen["reason"]), ("on_me", waiting.REASON_NO_EXTENSION))
        self.assertFalse(self.session()["extension"])
        self.heartbeat()
        self.tick()
        self.assertEqual(self.wait_of(wait)["ball"], "on_machine")
        self.assertTrue(self.session()["extension"])

    def test_unknown_liveness_flags_nothing(self) -> None:
        wait = self.add("--kind", "time", "--in", "1s", "--prompt", "go")["id"]
        (self.state_dir / "agents.json").unlink()
        self.advance(minutes=20)
        self.tick()
        self.assertEqual(self.wait_of(wait)["ball"], "on_machine")

    def test_extension_dying_during_a_turn_comes_to_me(self) -> None:
        wait, _ = self.queued_prompt()
        self.tick_through(minutes=20)
        seen = self.wait_of(wait)
        self.assertEqual((seen["ball"], seen["reason"]), ("on_me", waiting.REASON_NO_EXTENSION))

    def stale_agents(self, stamp: datetime) -> None:
        self.agents()
        payload = json.loads((self.state_dir / "agents.json").read_text())
        payload["updatedAt"] = int(stamp.timestamp() * 1000)
        (self.state_dir / "agents.json").write_text(json.dumps(payload))

    def test_a_resumed_session_beats_an_older_sweep(self) -> None:
        wait = self.add("--kind", "time", "--in", "1s", "--prompt", "go")["id"]
        self.agents()
        self.advance(seconds=2)
        self.tick()
        self.assertEqual(self.wait_of(wait)["reason"], waiting.REASON_CLOSED)
        self.stale_agents(self.clock)
        self.advance(seconds=2)
        action = self.run_cli("claim-prompts", "--session", SID)["prompts"][0]["action"]
        self.run_cli("ack", "--action", action, "--result", "delivered")
        self.tick()
        seen = self.wait_of(wait)
        self.assertEqual((seen["state"], seen["ball"]), ("acting", "on_machine"))
        self.run_cli("event", "--type", "turn_finished", "--session", SID)
        self.assertEqual(self.wait_of(wait)["state"], "review")

    def test_a_sweep_after_the_last_heartbeat_settles_it(self) -> None:
        wait, _ = self.queued_prompt()
        self.advance(minutes=1)
        self.stale_agents(self.clock)
        self.tick()
        # Closed during its turn: sent again, once.
        self.assertEqual(self.wait_of(wait)["state"], "acting")
        self.assertEqual(self.state()["sessions"][SID]["prompt_queue"][0]["state"], "queued")

    def test_liveness_is_read_again_after_slow_checks(self) -> None:
        wait = self.add("--kind", "time", "--in", "1s", "--prompt", "go")["id"]
        self.add("--kind", "command", "--argv", json.dumps([sys.executable, "-c", "raise SystemExit(1)"]))
        self.agents()
        self.advance(seconds=2)
        real = waiting._run_jobs

        def resume_meanwhile(jobs, now):
            self.agents(SID)
            action = self.run_cli("claim-prompts", "--session", SID)["prompts"][0]["action"]
            self.run_cli("ack", "--action", action, "--result", "delivered")
            return real(jobs, now)

        with mock.patch.object(waiting, "_run_jobs", resume_meanwhile):
            self.tick()
        self.assertEqual(self.wait_of(wait)["state"], "acting")

    def test_a_session_closed_during_the_turn_sends_the_prompt_again_once(self) -> None:
        wait, _ = self.queued_prompt()
        self.agents()
        self.tick()
        seen = self.wait_of(wait)
        self.assertEqual((seen["state"], seen["outcome"]), ("acting", "attention"))
        self.assertEqual(seen["reason"], waiting.REASON_CLOSED)  # nothing here can resume it
        self.assertIn("Sending again after the session closed: Timer", [h["text"] for h in self.session()["history"]])
        self.agents(SID)
        self.heartbeat()
        prompt = self.run_cli("claim-prompts", "--session", SID)["prompts"][0]
        self.assertEqual(prompt["text"], f"{waiting.CLOSE_NOTE}\n\ngo")
        self.assertFalse(prompt["resend_check"])
        self.run_cli("ack", "--action", prompt["action"], "--result", "delivered")
        # Cut off a second time, it comes to me.
        self.agents()
        self.tick()
        seen = self.wait_of(wait)
        self.assertEqual((seen["state"], seen["reason"]), ("needs_attention", waiting.REASON_CUT_TWICE))
        self.assertEqual(self.session()["prompts_queued"], 0)

    def test_snooze_cancels_the_queued_prompt(self) -> None:
        wait = self.add("--kind", "time", "--in", "1s", "--prompt", "go")["id"]
        self.advance(seconds=2)
        self.tick()
        self.run_cli("snooze", "--session", SID, "--wait", wait, "--for", "1h")
        self.assertEqual(self.run_cli("claim-prompts", "--session", SID)["prompts"], [])
        self.advance(hours=2)
        self.tick()
        self.assertEqual(self.run_cli("claim-prompts", "--session", SID)["prompts"][0]["text"], "go")


class OutboxTests(Case):
    def test_redelivered_until_acked(self) -> None:
        self.add("--kind", "time", "--in", "1s", "--note", "x")
        self.advance(seconds=2)
        first = self.tick()["actions"]
        self.assertEqual(self.tick()["actions"], first)
        self.run_cli("ack", "--action", first[0]["id"], "--result", "done")
        self.assertEqual(self.tick()["actions"], [])

    def test_park_resolves_the_tern_pane(self) -> None:
        self.agents(OTHER, SID)
        self.add("--kind", "later", "--park")
        action = self.tick()["actions"][0]
        self.assertEqual(
            action, {"id": mock.ANY, "type": "park", "session": SID, "pane": 41},
        )

    def test_park_of_an_unknown_pane(self) -> None:
        self.add("--kind", "later", "--park")
        self.assertIsNone(self.tick()["actions"][0]["pane"])

    def test_park_of_a_pane_record(self) -> None:
        self.run_cli("add", "--pane", "51", "--kind", "job", "--park")
        self.assertEqual(self.tick()["actions"][0]["pane"], 51)

    def test_ack_rejects_delivered_for_a_window_action(self) -> None:
        self.add("--kind", "later", "--park")
        action = self.tick()["actions"][0]["id"]
        self.run_cli("ack", "--action", action, "--result", "delivered", expect=2)

    def test_a_toast_that_failed_changes_nothing(self) -> None:
        wait = self.add("--kind", "time", "--in", "10s")["id"]
        self.advance(seconds=20)
        action = self.tick()["actions"][0]["id"]
        self.run_cli("ack", "--action", action, "--result", "failed", "--error", "gone")
        seen = self.wait_of(wait)
        self.assertEqual((seen["state"], seen["reason"]), ("fired", None))
        self.assertEqual(self.tick()["actions"], [])

    def test_a_failed_close_puts_the_open_wait_on_me(self) -> None:
        self.agents(SID)
        self.heartbeat()
        wait = self.add(
            "--kind", "job", "--pane", "51", "--close-on-success", "--prompt", "go",
        )["id"]
        self.run_cli("event", "--type", "job_finished", "--pane", "51", "--status", "0")
        actions = self.tick()["actions"]
        close = next(action for action in actions if action["type"] == "close_pane")
        self.run_cli("ack", "--action", close["id"], "--result", "failed", "--error", "stuck")
        seen = self.wait_of(wait)
        self.assertEqual((seen["state"], seen["ball"], seen["reason"]), ("acting", "on_me", "stuck"))

    def test_old_toasts_expire(self) -> None:
        self.add("--kind", "time", "--in", "1s")
        self.advance(seconds=2)
        self.assertEqual(len(self.tick()["actions"]), 1)
        self.advance(days=8)
        self.assertEqual(self.tick()["actions"], [])

    def test_unacked_actions_stop_pinning_the_tick(self) -> None:
        self.add("--kind", "later", "--park")
        self.assertEqual(self.tick()["next_tick_s"], 30)
        self.advance(minutes=6)
        self.assertEqual(self.tick()["next_tick_s"], 300)


class TickTests(Case):
    def test_next_tick_range(self) -> None:
        self.assertEqual(self.tick()["next_tick_s"], 300)
        self.add("--kind", "time", "--in", "2m")
        self.assertEqual(self.tick()["next_tick_s"], 120)
        self.run_cli("stop", "--session", SID, "--all")
        self.add("--kind", "time", "--in", "2s")
        self.assertEqual(self.tick()["next_tick_s"], 15)

    def test_unacked_actions_tick_soon(self) -> None:
        self.add("--kind", "later", "--park")
        self.assertEqual(self.tick()["next_tick_s"], 30)

    def test_verifications_count_toward_what_one_tick_takes(self) -> None:
        state = waiting._empty_state()
        record = state["sessions"][SID] = waiting._new_record(SID, None)
        for _ in range(2):
            wait = waiting._new_wait(
                state, T0, kind="time", condition={"type": "time", "at": None}, by="user", label="x",
            )
            wait.update(state="acting", verify_pending=True, verify={"type": "command"})
            record["waits"].append(wait)
        with mock.patch.object(waiting, "MAX_CHECKS_PER_TICK", 1):
            self.assertEqual(len(waiting._claim_checks(state, T0)), 1)

    def test_result_of_a_changed_wait_is_dropped(self) -> None:
        wait = self.add(
            "--kind", "command", "--argv", json.dumps([sys.executable, "-c", "pass"]),
        )["id"]
        real = waiting._run_jobs

        def run_while_i_snooze(jobs, now):
            self.run_cli("snooze", "--session", SID, "--wait", wait, "--for", "1h")
            return real(jobs, now)

        with mock.patch.object(waiting, "_run_jobs", run_while_i_snooze):
            self.tick()
        self.assertEqual(self.wait_of(wait)["state"], "waiting")

    def test_a_note_written_during_a_slow_check_survives(self) -> None:
        self.add(
            "--kind", "command", "--argv", json.dumps([sys.executable, "-c", "pass"]),
        )
        real = waiting._run_jobs

        def run_while_i_write(jobs, now):
            self.run_cli("note", "--session", SID, "--text", "typed meanwhile")
            return real(jobs, now)

        with mock.patch.object(waiting, "_run_jobs", run_while_i_write):
            self.tick()
        self.assertEqual(self.session()["note"], "typed meanwhile")
        self.assertEqual(self.session()["ball"], "on_me")

    def test_concurrent_ticks_run_a_check_once(self) -> None:
        marker = self.root / "ran"
        script = f"import time; open({str(marker)!r}, 'a').write('x'); time.sleep(0.3); raise SystemExit(1)"
        self.add("--kind", "command", "--argv", json.dumps([sys.executable, "-c", script]))
        now = self.clock.isoformat()
        script_path = str(SCRIPT_PATH)
        processes = [
            subprocess.Popen(
                [sys.executable, script_path, "tick", "--now", now],
                stdout=subprocess.PIPE, env={**os.environ},
            )
            for _ in range(3)
        ]
        for process in processes:
            out, _ = process.communicate(timeout=30)
            self.assertTrue(json.loads(out)["ok"])
        self.assertEqual(marker.read_text(), "x")


class ConcurrencyTests(Case):
    def test_two_processes_lose_no_update(self) -> None:
        count = 12
        program = (
            "import sys, subprocess\n"
            "for i in range(int(sys.argv[3])):\n"
            "    subprocess.run([sys.executable, sys.argv[1], 'add', '--session', sys.argv[2],\n"
            "        '--kind', 'later', '--label', str(i)], check=True, stdout=subprocess.DEVNULL)\n"
        )
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", program, str(SCRIPT_PATH), sid, str(count)],
                env={**os.environ},
            )
            for sid in (SID, OTHER)
        ]
        for process in processes:
            self.assertEqual(process.wait(timeout=120), 0)
        state = self.state()
        self.assertEqual(len(state["sessions"][SID]["waits"]), count)
        self.assertEqual(len(state["sessions"][OTHER]["waits"]), count)
        ids = [
            wait["id"] for record in state["sessions"].values() for wait in record["waits"]
        ]
        self.assertEqual(len(set(ids)), count * 2)

    def test_threads_of_one_process_serialize_too(self) -> None:
        # The flock is taken per open file description.
        order = []
        with waiting._transaction():
            def other() -> None:
                with waiting._transaction():
                    order.append("second")

            thread = threading.Thread(target=other)
            thread.start()
            thread.join(0.2)
            order.append("first")
        thread.join(5)
        self.assertEqual(order, ["first", "second"])


class ViewTests(Case):
    def test_shape_of_a_session(self) -> None:
        self.agents(SID)
        self.heartbeat()
        wait = self.add("--kind", "time", "--in", "2h", "--label", "Retry", "--note", "try again")
        self.assertEqual(
            set(wait),
            {
                "id", "kind", "label", "summary", "state", "ball", "overdue", "outcome",
                "fired_text", "fired_at", "progress", "mark", "reason", "created_at", "due_at", "deadline",
                "condition", "has_prompt", "next", "by", "repeat", "retryable", "daemon",
            },
        )
        seen = self.session()
        self.assertEqual(
            set(seen),
            {
                "session_id", "title", "cwd", "project", "live", "pane", "tern_session",
                "agent_state", "extension", "resume_command", "note", "suggested_note",
                "size", "set_aside_at", "pinned", "ball", "unseen", "outcome", "headline",
                "status", "line", "composer_row",
                "next_step", "threads", "still_waiting", "waiting_for", "recap", "recap_at", "recap_stale", "due_at", "prompts_queued", "waits",
                "close",
                "history", "prs", "on_you", "stoppable",
            },
        )
        self.assertEqual(seen["status"], "⏲ Retry · in 2h")
        self.assertEqual(seen["line"], '⏲ Retry · in 2h · "try again"')
        self.assertEqual(seen["pane"], 40)
        self.assertEqual(seen["tern_session"], "Default")
        self.assertEqual(seen["agent_state"], "idle")
        self.assertEqual(seen["recap"], "live recap")
        self.assertIsNone(seen["resume_command"])
        self.assertTrue(seen["extension"])
        self.assertEqual(seen["project"], SID[:4])

    def test_on_me_line(self) -> None:
        self.add("--kind", "time", "--in", "1h", "--label", "Retry", "--note", "x")
        self.advance(days=2)
        self.tick()
        seen = self.session()
        self.assertEqual(seen["status"], "✅ 1h passed · 0s")
        self.advance(hours=6)
        seen = self.session()
        self.assertEqual(seen["status"], "✅ 1h passed · 6h")
        self.assertEqual(seen["line"], '✅ 1h passed · 6h · "x"')
        self.assertEqual(seen["composer_row"], seen["line"])

    def test_pr_line(self) -> None:
        self.add(
            "--kind", "pr", "--repo", "o/r", "--number", "16476", "--until", "review_on_head",
            "--label", "Greptile review on head", "--note", "fix, merge, release",
        )
        self.advance(days=2)
        seen = self.session()
        self.assertEqual(seen["status"], "⏳ PR#16476 · 2d")
        self.assertEqual(
            seen["line"],
            '⏳ PR#16476 · Greptile review on head · 2d · "fix, merge, release"',
        )

    def test_sort_ball_then_due_then_set_aside(self) -> None:
        sids = [f"aaaa000{index}" for index in range(5)]
        timer = "aaaa0006"
        self.add("--kind", "repeat", "--every", "1h", "--prompt", "p", session=timer)
        self.add("--kind", "later", session=sids[0])
        self.add("--kind", "something", "--what", "x", session=sids[1])
        self.add("--kind", "job", "--pane", "51", session=sids[2])
        self.advance(minutes=1)
        self.add("--kind", "job", "--pane", "52", session=sids[3])
        self.add("--kind", "time", "--in", "1h", session=sids[4])
        fired = "aaaa0005"
        self.add("--kind", "time", "--in", "1s", session=fired)
        self.advance(seconds=2)
        self.tick()
        view = self.run_cli("list")
        self.assertEqual(
            [item["session_id"] for item in view["sessions"]],
            [fired, sids[2], sids[3], timer, sids[4], sids[1], sids[0]],
        )
        self.assertEqual(
            view["counts"],
            {"on_me": 1, "on_machine": 2, "on_timer": 2, "on_someone": 1, "on_my_time": 1},
        )

    def test_counts_include_panes(self) -> None:
        self.run_cli("add", "--pane", "51", "--kind", "job")
        self.assertEqual(self.run_cli("list")["counts"]["on_machine"], 1)

    def test_ball_is_the_worst_of_the_open_waits(self) -> None:
        first = self.add("--kind", "later")["id"]
        self.assertEqual(self.session()["ball"], "on_my_time")
        self.add("--kind", "something", "--what", "x")
        self.assertEqual(self.session()["ball"], "on_someone")
        self.add("--kind", "job", "--pane", "51")
        self.assertEqual(self.session()["ball"], "on_machine")
        self.add("--kind", "time", "--in", "1s", "--after", first)
        self.assertEqual(self.session()["ball"], "on_machine")
        self.add("--kind", "time", "--in", "1s")
        self.advance(seconds=2)
        self.tick()
        self.assertEqual(self.session()["ball"], "on_me")

    def test_pending_steps_do_not_hold_the_ball(self) -> None:
        first = self.add("--kind", "something", "--what", "x")["id"]
        self.add("--kind", "job", "--pane", "51", "--after", first)
        self.assertEqual(self.session()["ball"], "on_someone")

    def test_list_filters_to_one_session(self) -> None:
        self.add("--kind", "later")
        self.add("--kind", "later", session=OTHER)
        view = self.run_cli("list", "--session", SID)
        self.assertEqual([item["session_id"] for item in view["sessions"]], [SID])
        self.assertEqual(view["counts"]["on_my_time"], 2)

    def test_seen_clears_unseen(self) -> None:
        self.add("--kind", "time", "--in", "1s")
        self.advance(seconds=2)
        self.tick()
        self.assertTrue(self.session()["unseen"])
        self.run_cli("seen", "--session", SID)
        self.assertFalse(self.session()["unseen"])

    def test_history_shows_the_last_twenty(self) -> None:
        self.add("--kind", "later")
        state_path = self.state_dir / "waiting.json"
        state = json.loads(state_path.read_text())
        state["sessions"][SID]["history"] = [
            {"at": T0.isoformat(), "wait": None, "text": str(index)} for index in range(30)
        ]
        state_path.write_text(json.dumps(state))
        history = self.session()["history"]
        self.assertEqual(len(history), 20)
        self.assertEqual(history[-1]["text"], "29")

    def pin(self, *sids: str) -> None:
        (self.omp / "session-pins.json").write_text(json.dumps(list(sids)))

    def test_pinned_session_without_waits(self) -> None:
        self.pin(SID)
        seen = self.session()
        self.assertEqual((seen["pinned"], seen["ball"], seen["waits"]), (True, "on_my_time", []))
        self.assertEqual(seen["line"], "📌 pinned")
        self.assertEqual(self.run_cli("list")["counts"]["on_my_time"], 1)

    def test_pinned_keeps_its_sentence_after_stop(self) -> None:
        self.pin(SID)
        wait = self.add("--kind", "time", "--in", "1h", "--note", "mine")["id"]
        self.run_cli("stop", "--session", SID, "--wait", wait)
        self.assertEqual(self.session()["note"], "mine")
        self.add("--kind", "later")
        self.assertEqual(self.session()["note"], "mine")

    def test_bad_pins_file_is_ignored(self) -> None:
        (self.omp / "session-pins.json").write_text('{"a": 1}')
        self.assertEqual(self.run_cli("list")["sessions"], [])
        self.pin("../evil", SID)
        self.assertEqual(
            [item["session_id"] for item in self.run_cli("list")["sessions"]], [SID],
        )


class ClosedSessionTests(Case):
    def transcript(self, sid: str, title: str = "Fix the thing", cwd: str = "/w/project") -> Path:
        folder = self.omp / "sessions" / "-w-project"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"2026-11-01T10-00-00-000Z_{sid}.jsonl"
        lines = [
            {"type": "title", "v": "1", "title": title, "source": "auto"},
            {"type": "session", "version": "3", "id": sid, "cwd": cwd},
            {"type": "model_change", "model": "x"},
        ]
        path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
        return path

    def history_db(self, recaps: list[tuple[str, str, int]], titles: dict[str, str] | None = None) -> None:
        con = sqlite3.connect(self.omp / "history.db")
        con.execute(
            "CREATE TABLE session_recaps (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " session_id TEXT, cwd TEXT, recap TEXT, created_at INTEGER)"
        )
        con.executemany(
            "INSERT INTO session_recaps (session_id, cwd, recap, created_at) VALUES (?, '', ?, ?)",
            recaps,
        )
        if titles is not None:
            con.execute("CREATE TABLE session_titles (session_id TEXT PRIMARY KEY, title TEXT)")
            con.executemany("INSERT INTO session_titles VALUES (?, ?)", titles.items())
        con.commit()
        con.close()

    def pin(self, *sids: str) -> None:
        (self.omp / "session-pins.json").write_text(json.dumps(list(sids)))

    def test_title_and_resume_from_the_session_file(self) -> None:
        self.transcript(SID)
        self.history_db([(SID, "older", 1), (SID, "  newest\nrecap ", 2)])
        self.add("--kind", "later")
        seen = self.session()
        self.assertEqual(seen["title"], "Fix the thing")
        self.assertEqual(seen["cwd"], "/w/project")
        self.assertEqual(seen["project"], "project")
        self.assertEqual(seen["recap"], "newest recap")
        self.assertEqual(seen["resume_command"], resume_line("/w/project"))
        self.assertFalse(seen["live"])

    def test_a_closed_sessions_recap_has_its_time(self) -> None:
        self.transcript(SID)
        self.history_db([(SID, "older", 1), (SID, "newest", 1_793_958_276)])
        self.add("--kind", "later")
        seen = self.session()
        self.assertEqual(waiting._stamp(seen["recap_at"]), 1_793_958_276)
        self.assertFalse(seen["recap_stale"])

    def test_a_live_omp_recap_gets_its_time_from_the_history_db(self) -> None:
        self.history_db([(SID, "live recap", 1_793_958_276)])
        self.agents(SID)
        payload = json.loads((self.state_dir / "agents.json").read_text())
        payload["agents"][0]["recapStale"] = True
        (self.state_dir / "agents.json").write_text(json.dumps(payload))
        self.add("--kind", "later")
        seen = self.session()
        self.assertEqual(
            (seen["recap"], waiting._stamp(seen["recap_at"]), seen["recap_stale"]),
            ("live recap", 1_793_958_276, True),
        )

    def test_a_recap_omp_did_not_write_has_no_time(self) -> None:
        self.history_db([(SID, "an older omp recap", 1_793_958_276)])
        self.agents(SID)  # its recap, "live recap", is Claude's away summary or newer
        self.add("--kind", "later")
        self.assertIsNone(self.session()["recap_at"])

    def test_a_recap_time_out_of_range_is_no_time(self) -> None:
        self.transcript(SID)
        self.history_db([(SID, "newest", 1_793_958_276_000_000)])  # microseconds, not seconds
        self.add("--kind", "later")
        seen = self.session()
        self.assertEqual((seen["recap"], seen["recap_at"]), ("newest", None))

    def test_title_table_wins_over_the_session_file(self) -> None:
        self.transcript(SID)
        self.history_db([], {SID: "From the table"})
        self.add("--kind", "later")
        self.assertEqual(self.session()["title"], "From the table")

    def test_history_of_agents_json_wins(self) -> None:
        self.transcript(SID)
        self.history_db([(SID, "from the db", 1)])
        self.agents(closed=[{
            "sessionId": SID, "cwd": "/w/there", "windowTitle": "From agents",
            "resumeCommand": "omp --resume saved", "recap": "from agents",
        }])
        self.add("--kind", "later")
        seen = self.session()
        self.assertEqual(
            (seen["title"], seen["cwd"], seen["recap"], seen["resume_command"]),
            ("From agents", "/w/there", "from agents", "omp --resume saved"),
        )

    def test_unknown_session_still_has_a_title(self) -> None:
        self.add("--kind", "later")
        seen = self.session()
        self.assertEqual(seen["title"], SID[:8])
        self.assertIsNone(seen["resume_command"])

    def test_resume(self) -> None:
        self.agents()
        self.transcript(SID)
        answer = self.run_cli("resume", "--session", SID)
        self.assertEqual(answer["command"], resume_line("/w/project"))
        self.assertEqual(answer["cwd"], "/w/project")

    def test_resume_prefers_agents_history(self) -> None:
        self.agents(closed=[{"sessionId": SID, "cwd": "/w", "resumeCommand": "omp --resume saved"}])
        self.assertEqual(self.run_cli("resume", "--session", SID)["command"], "omp --resume saved")

    def test_resume_errors(self) -> None:
        self.agents(SID)
        self.run_cli("resume", "--session", SID, expect=2)
        self.run_cli("resume", "--session", OTHER, expect=2)

    def test_resume_refuses_while_liveness_is_unknown_or_newer_than_agents_json(self) -> None:
        self.transcript(SID)
        self.run_cli("resume", "--session", SID, expect=2)
        sweep = {"agents": [], "history": [], "updatedAt": (T0 - timedelta(minutes=1)).timestamp() * 1000}
        (self.state_dir / "agents.json").write_text(json.dumps(sweep))
        self.run_cli("resume", "--session", SID)
        self.heartbeat()
        self.assertIn("running", self.run_cli("resume", "--session", SID, expect=2)["error"])

    def test_resume_text_matches_the_one_launch_checks_against(self) -> None:
        path = SCRIPT_PATH.with_name("codexbar_agents.py")
        spec = importlib.util.spec_from_file_location("codexbar_agents_for_resume", path)
        agents = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(agents)
        for cwd in ("/w/a b", ""):
            self.assertEqual(
                waiting._resume_text(SID, cwd), agents._resume_command("omp", SID, cwd, True),
            )

    def test_duplicate_pin_lists_once(self) -> None:
        self.pin(SID, SID)
        self.assertEqual(len(self.run_cli("inventory")["items"]), 1)

    def test_pinned_closed_session_in_inventory(self) -> None:
        path = self.transcript(SID)
        os.utime(path, (T0.timestamp() - 7200, T0.timestamp() - 7200))
        self.history_db([(SID, "what happened", 5)])
        self.pin(SID)
        item = self.run_cli("inventory")["items"][0]
        self.assertEqual(
            item,
            {
                "session_id": SID, "title": "Fix the thing", "cwd": "/w/project",
                "project": "project", "note": None, "size": None, "age_s": 7200,
                "recap": "what happened", "live": False, "pane": None,
                "resume_command": resume_line("/w/project"),
            },
        )

    def test_inventory_sorts_by_size_then_age(self) -> None:
        ids = [f"bbbb000{index}" for index in range(4)]
        self.pin(*ids)
        self.add("--kind", "later", "--size", "L", session=ids[0])
        self.advance(hours=1)
        self.add("--kind", "later", "--size", "S", session=ids[1])
        self.advance(hours=1)
        self.add("--kind", "later", "--size", "S", session=ids[2])
        items = self.run_cli("inventory")["items"]
        self.assertEqual(
            [item["session_id"] for item in items], [ids[1], ids[2], ids[0], ids[3]],
        )
        self.assertEqual(items[0]["age_s"], 3600)

    def test_inventory_lists_only_pins(self) -> None:
        self.add("--kind", "time", "--in", "1h", "--note", "unpinned")
        self.assertEqual(self.run_cli("inventory")["items"], [])

    def test_live_pin_in_inventory(self) -> None:
        self.agents(SID)
        self.pin(SID)
        item = self.run_cli("inventory")["items"][0]
        self.assertEqual((item["live"], item["pane"], item["resume_command"]), (True, 40, None))

    def test_closed_record_is_pruned_after_a_week(self) -> None:
        wait = self.add("--kind", "later")["id"]
        self.run_cli("stop", "--session", SID, "--wait", wait)
        self.advance(days=6)
        self.tick()
        self.assertIn(SID, self.state()["sessions"])
        self.advance(days=2)
        self.tick()
        self.assertNotIn(SID, self.state()["sessions"])


def user(text: object) -> dict:
    return {"type": "message", "message": {"role": "user", "content": text}}


def assistant(*blocks: dict) -> dict:
    return {"type": "message", "message": {"role": "assistant", "content": list(blocks)}}


def said(text: str) -> dict:
    return assistant({"type": "text", "text": text})


def call(
    call_id: str, command: str, key: str = "command", name: str = "bash",
    extra: dict | None = None,
) -> dict:
    return assistant({
        "type": "toolCall", "id": call_id, "name": name,
        "arguments": {key: command, **(extra or {})},
    })


def result(call_id: str, text: str) -> dict:
    return {"type": "message", "message": {
        "role": "toolResult", "toolCallId": call_id, "toolName": "bash",
        "content": [{"type": "text", "text": text}],
    }}


def pr_url(number: int, repo: str = "o/r") -> str:
    return f"https://github.com/{repo}/pull/{number}"


class SessionPrCase(Case):
    def setUp(self) -> None:
        super().setUp()
        self.asked: list[int] = []
        self.status = {"title": "A PR", "state": "open", "review": "approved", "checks": "passing"}
        github = fake_github(pr_status=self.pr_status)
        patch = mock.patch.dict(sys.modules, {"waiting_github": github})
        patch.start()
        self.addCleanup(patch.stop)

    def pr_status(self, repo: str, number: int, run=None) -> dict:
        self.asked.append(number)
        return self.status

    def transcript_path(self, sid: str = SID) -> Path:
        return self.omp / "sessions" / "-w" / f"2026-11-05T10-00-00-000Z_{sid}.jsonl"

    def write(self, *records: dict, sid: str = SID) -> None:
        path = self.transcript_path(sid)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as out:
            out.writelines(json.dumps(record) + "\n" for record in records)

    def prs(self, sid: str = SID) -> list[tuple[int, str]]:
        return [(pr["number"], pr["source"]) for pr in self.session(sid)["prs"]]

    def created(self, *numbers: int) -> None:
        text = "\n".join(pr_url(number) for number in numbers)
        self.write(call("c1", "gh pr create --fill"), result("c1", text))


class PrDiscoveryTests(SessionPrCase):
    def setUp(self) -> None:
        super().setUp()
        self.add("--kind", "later")

    def test_gh_pr_create_as_a_command_counts_as_created(self) -> None:
        self.write(
            call("c1", "gh pr create --fill"), result("c1", f"{pr_url(1)}\n\nWall time: 2 s"),
            call("c2", "!gh pr create --fill", key="code", name="eval"),
            result("c2", pr_url(2, "o/other")),
            call("c3", 'cd /w && GH_HOST=x gh pr create -f'), result("c3", pr_url(3)),
            call("c4", "git push\ngh pr create -f"), result("c4", pr_url(4)),
            call("c5", "url=$(gh pr create -f)"), result("c5", pr_url(5)),
        )
        self.assertEqual([number for number, _ in self.prs()], [1, 2, 3, 4, 5])
        self.assertEqual({source for _, source in self.prs()}, {"created"})
        self.assertEqual(self.session()["prs"][1]["repo"], "o/other")

    def test_every_field_of_the_shape(self) -> None:
        self.created(1)
        self.assertEqual(self.session()["prs"], [{
            "repo": "o/r", "number": 1, "url": pr_url(1), "source": "created", "title": None,
            "state": None, "review": None, "checks": None, "checked_at": None,
        }])

    def test_github_tool_header_takes_only_the_new_pr(self) -> None:
        self.write(result(
            "any", f"# Created Pull Request #2: x\n\nURL: {pr_url(2)}\n\n## Body\nAfter {pr_url(1)}",
        ))
        self.assertEqual(self.prs(), [(2, "created")])

    def test_a_result_of_another_call_is_noise(self) -> None:
        self.write(
            call("c1", "gh pr list"), result("c1", pr_url(1)),
            call("c2", "gh pr create --fill"), result("other", pr_url(2)),
        )
        self.assertEqual(self.prs(), [])

    def test_a_search_for_the_command_is_not_the_command(self) -> None:
        self.write(
            call("c1", "grep -r 'gh pr create' ."), result("c1", pr_url(1)),
            call("c2", 'rg "gh pr create --fill" .'), result("c2", pr_url(2)),
            call("c3", "cat notes", extra={"i": "Sampling gh pr create calls"}),
            result("c3", pr_url(3)),
            call("c4", "echo see gh pr create --fill"), result("c4", pr_url(4)),
            call("c5", "gh pr created --fill"), result("c5", pr_url(5)),
        )
        self.assertEqual(self.prs(), [])

    def test_mentions_come_from_user_and_assistant_text(self) -> None:
        self.write(
            user(f"look at {pr_url(1)}"),
            user([{"type": "text", "text": f"and {pr_url(2)}"}]),
            said(f"Opened {pr_url(3)}."),
            assistant({"type": "thinking", "thinking": pr_url(4)}),
        )
        self.assertEqual(self.prs(), [(3, "mentioned"), (2, "mentioned"), (1, "mentioned")])

    def test_suffixes_are_the_same_pr(self) -> None:
        self.write(user(
            f"{pr_url(1)}#issuecomment-99 {pr_url(1)}/files {pr_url(2)}/commits/abc {pr_url(3)}x"
        ))
        self.assertEqual(self.prs(), [(2, "mentioned"), (1, "mentioned")])

    def test_created_outranks_wait_outranks_mentioned(self) -> None:
        self.add("--kind", "pr", "--repo", "o/r", "--number", "1")
        self.add("--kind", "pr", "--repo", "o/r", "--number", "2")
        self.write(user(f"{pr_url(1)} {pr_url(2)} {pr_url(3)} {pr_url(4)}"))
        self.created(1, 4)
        self.write(said(pr_url(4)))
        self.assertEqual(self.prs(), [(1, "created"), (4, "created"), (2, "wait"), (3, "mentioned")])

    def test_repo_case_does_not_split_a_pr(self) -> None:
        self.add("--kind", "pr", "--repo", "O/R", "--number", "7")
        self.write(user(pr_url(7, "o/r")))
        self.assertEqual(self.prs(), [(7, "wait")])

    def test_only_the_five_newest_mentions_stay(self) -> None:
        self.write(*[user(pr_url(number)) for number in range(1, 8)])
        self.assertEqual([number for number, _ in self.prs()], [7, 6, 5, 4, 3])
        self.write(user(pr_url(4)))
        self.assertEqual([number for number, _ in self.prs()], [4, 7, 6, 5, 3])

    def test_created_prs_are_outside_the_mention_cap(self) -> None:
        self.created(1)
        self.write(*[user(pr_url(number)) for number in range(2, 9)], user(pr_url(1)))
        self.assertEqual(self.prs()[0], (1, "created"))
        self.assertEqual(len(self.prs()), 6)

    def test_a_scan_reads_only_what_was_appended(self) -> None:
        self.write(user(pr_url(1)))
        self.assertEqual(self.prs(), [(1, "mentioned")])
        path = self.transcript_path()
        # Same length, so only a read from the start would see pull/2.
        path.write_bytes(path.read_bytes().replace(b"pull/1", b"pull/2"))
        self.write(user(pr_url(3)))
        self.assertEqual(self.prs(), [(3, "mentioned"), (1, "mentioned")])

    def test_a_truncated_transcript_is_scanned_again(self) -> None:
        self.write(user(pr_url(1)), user(pr_url(2)))
        self.assertEqual(len(self.prs()), 2)
        path = self.transcript_path()
        path.write_text(json.dumps(user(pr_url(3))) + "\n")
        self.assertEqual(self.prs(), [(3, "mentioned")])

    def test_another_path_is_scanned_again(self) -> None:
        self.write(user(pr_url(1)))
        self.assertEqual(self.prs(), [(1, "mentioned")])
        moved = self.transcript_path().parent.parent / "-v" / self.transcript_path().name
        moved.parent.mkdir()
        moved.write_text(json.dumps(user(pr_url(2))) + "\n")
        self.transcript_path().unlink()
        self.assertEqual(self.prs(), [(2, "mentioned")])

    def test_a_result_after_the_scan_still_finds_its_call(self) -> None:
        self.write(call("c1", "gh pr create --fill"))
        self.assertEqual(self.prs(), [])
        self.write(result("c1", pr_url(1)))
        self.assertEqual(self.prs(), [(1, "created")])

    def test_a_line_being_written_waits_for_its_end(self) -> None:
        line = json.dumps(user(pr_url(1))) + "\n"
        path = self.transcript_path()
        path.parent.mkdir(parents=True)
        path.write_text(line[:-10])
        self.assertEqual(self.prs(), [])
        with open(path, "a", encoding="utf-8") as out:
            out.write(line[-10:])
        self.assertEqual(self.prs(), [(1, "mentioned")])

    def test_broken_lines_and_cache_are_survivable(self) -> None:
        path = self.transcript_path()
        path.parent.mkdir(parents=True)
        path.write_bytes(b'{"broken /pull/\n[1, "/pull/"]\n\xff/pull/\n')
        self.write(user(pr_url(1)))
        self.assertEqual(self.prs(), [(1, "mentioned")])
        (self.state_dir / "pr_cache.json").write_text("{broken")
        self.assertEqual(self.prs(), [(1, "mentioned")])
        (self.state_dir / "pr_cache.json").write_text(json.dumps({
            "version": 1,
            "sessions": {SID: {"path": str(path), "offset": 0}},
            "statuses": {"o/r#1": {"asked_at": "soon"}, "o/r#2": 5},
        }))
        self.assertEqual(self.prs(), [(1, "mentioned")])

    def test_a_transcript_emptied_and_refilled_is_scanned_again(self) -> None:
        self.write(user(pr_url(1)))
        self.assertEqual(self.prs(), [(1, "mentioned")])
        path = self.transcript_path()
        size = path.stat().st_size
        path.write_text("")
        self.assertEqual(self.prs(), [])
        path.write_text((json.dumps(user(pr_url(2))) + "\n") * 2)
        self.assertGreater(path.stat().st_size, size)
        self.assertEqual(self.prs(), [(2, "mentioned")])

    def test_a_failed_create_leaves_no_call_behind(self) -> None:
        self.write(call("c1", "gh pr create --fill"), result("c1", "no commits between"))
        self.prs()
        cache = json.loads((self.state_dir / "pr_cache.json").read_text())
        self.assertEqual(cache["sessions"][SID]["calls"], [])

    def test_list_asks_github_nothing(self) -> None:
        self.created(1)
        self.run_cli("list")
        self.assertEqual(self.asked, [])


class PrStatusTickTests(SessionPrCase):
    def setUp(self) -> None:
        super().setUp()
        self.add("--kind", "later")

    def test_tick_refreshes_created_and_wait_prs_and_shows_them(self) -> None:
        self.created(1)
        self.add("--kind", "pr", "--repo", "o/r", "--number", "2")
        answer = self.tick()
        self.assertEqual(sorted(self.asked), [1, 2])
        pr = answer["view"]["sessions"][0]["prs"][0]
        self.assertEqual(
            (pr["title"], pr["state"], pr["review"], pr["checks"]),
            ("A PR", "open", "approved", "passing"),
        )
        self.assertEqual(pr["checked_at"], self.clock.isoformat(timespec="seconds"))
        saved = json.loads((self.state_dir / "waiting_view.json").read_text())
        self.assertEqual(saved, answer["view"])

    def test_tick_leaves_mentioned_prs_alone(self) -> None:
        self.write(user(pr_url(1)))
        self.tick()
        self.assertEqual(self.asked, [])

    def test_tick_asks_at_most_six_and_the_oldest_first(self) -> None:
        self.created(*range(1, 9))
        self.tick()
        self.assertEqual(len(self.asked), 6)
        first = set(self.asked)
        self.asked.clear()
        self.advance(minutes=1)
        self.tick()
        self.assertEqual(sorted(self.asked), sorted(set(range(1, 9)) - first))

    def test_one_status_lasts_ten_minutes(self) -> None:
        self.created(1)
        self.tick()
        self.advance(minutes=9)
        self.tick()
        self.assertEqual(self.asked, [1])
        self.advance(minutes=1)
        self.tick()
        self.assertEqual(self.asked, [1, 1])

    def test_a_merged_pr_lasts_six_hours(self) -> None:
        self.status = {**self.status, "state": "merged"}
        self.created(1)
        self.tick()
        self.advance(hours=5, minutes=59)
        self.tick()
        self.assertEqual(self.asked, [1])
        self.advance(minutes=1)
        self.tick()
        self.assertEqual(self.asked, [1, 1])

    def fail(self, repo: str, number: int, run=None) -> dict:
        self.asked.append(number)
        raise RuntimeError("gh is down")

    def test_a_failing_pr_keeps_its_status(self) -> None:
        self.created(1)
        self.tick()
        sys.modules["waiting_github"].pr_status = self.fail
        self.advance(minutes=20)
        row = self.tick()["view"]["sessions"][0]["prs"][0]
        self.assertEqual(self.asked, [1, 1])
        self.assertEqual(row["title"], "A PR")
        self.assertEqual(row["checked_at"], T0.isoformat(timespec="seconds"))

    def test_failing_prs_do_not_starve_the_rest(self) -> None:
        sys.modules["waiting_github"].pr_status = self.fail
        self.created(*range(1, 8))
        self.tick()
        self.assertEqual(sorted(self.asked), [1, 2, 3, 4, 5, 6])
        self.asked.clear()
        self.advance(minutes=1)
        self.tick()
        self.assertEqual(self.asked, [7])

    def test_github_runs_outside_the_state_lock(self) -> None:
        self.created(1)
        free: list[bool] = []

        def status(repo: str, number: int, run=None) -> dict:
            descriptor = os.open(self.state_dir / "waiting.lock", os.O_RDWR)
            try:
                free.append(state_paths.lock(descriptor, blocking=False))
            finally:
                os.close(descriptor)
            return self.status

        sys.modules["waiting_github"].pr_status = status
        self.tick()
        self.assertEqual(free, [True])

    def test_the_gh_call_gets_the_short_timeout(self) -> None:
        seen: dict = {}

        def status(repo: str, number: int, run=None) -> dict:
            with mock.patch.object(subprocess, "run") as run_process:
                run(["gh"], timeout=30, text=True)
            seen.update(run_process.call_args.kwargs)
            return self.status

        sys.modules["waiting_github"].pr_status = status
        self.created(1)
        self.tick()
        self.assertEqual(seen, {"timeout": 10, "text": True})

    def test_statuses_survive_a_tick_with_jobs(self) -> None:
        self.created(1)
        self.add("--kind", "command", "--argv", json.dumps([sys.executable, "-c", "pass"]))
        self.tick()
        self.assertEqual(self.session()["prs"][0]["title"], "A PR")


class CardTests(SessionPrCase):
    def card(self, sid: str = SID) -> dict:
        return self.run_cli("card", "--session", sid)["card"]

    def test_a_live_session_without_a_record(self) -> None:
        payload = {"agents": [{
            "sessionId": SID, "provider": "omp", "cwd": "/nowhere", "state": "working",
            "model": "m1", "lastPrompt": "x" * 400, "startedAt": 0,
            "stateChangedAt": T0.timestamp() * 1000, "updatedAt": 1, "windowTitle": "Title",
        }], "history": []}
        (self.state_dir / "agents.json").write_text(json.dumps(payload))
        card = self.card()
        self.assertEqual(card["title"], "Title")
        self.assertEqual((card["waits"], card["history"], card["prs"]), ([], [], []))
        self.assertEqual(
            (card["provider"], card["model"], card["agent_state_raw"]), ("omp", "m1", "working"),
        )
        self.assertIsNone(card["started_at"])
        self.assertEqual(card["active_at"], T0.isoformat())
        self.assertEqual(len(card["last_prompt"]), 300)
        self.assertIsNone(card["branch"])
        self.assertFalse(card["has_record"])
        self.assertFalse((self.state_dir / "waiting.json").exists())

    def test_every_field_of_the_view_is_there(self) -> None:
        self.add("--kind", "later", "--note", "why")
        card = self.card()
        self.assertTrue(set(self.session()) <= set(card))
        self.assertEqual(card["note"], "why")
        self.assertEqual(len(card["waits"]), 1)
        self.assertTrue(card["has_record"])

    def test_a_closed_session_from_agents_history(self) -> None:
        self.agents(closed=[{
            "sessionId": SID, "provider": "omp", "cwd": "/w/closed", "windowTitle": "Old",
            "lastPrompt": "bye", "lastSeenAt": T0.timestamp() * 1000, "model": "m2",
            "resumeCommand": f"omp --resume {SID}",
        }])
        card = self.card()
        self.assertFalse(card["live"])
        self.assertEqual((card["title"], card["model"]), ("Old", "m2"))
        self.assertIsNone(card["agent_state_raw"])
        self.assertEqual(card["active_at"], T0.isoformat())
        self.assertEqual(card["resume_command"], f"omp --resume {SID}")

    def test_a_closed_session_known_only_by_its_transcript(self) -> None:
        self.write(
            {"type": "session", "title": "From file", "cwd": "/w/file"}, user(pr_url(1)),
        )
        card = self.card()
        self.assertEqual((card["title"], card["provider"]), ("From file", "omp"))
        self.assertIn(SID, card["resume_command"])
        self.assertEqual([pr["number"] for pr in card["prs"]], [1])

    def test_a_finished_record_still_shows_its_note(self) -> None:
        wait = self.add("--kind", "later", "--note", "why")["id"]
        self.run_cli("stop", "--session", SID, "--wait", wait)
        card = self.card()
        self.assertEqual((card["note"], card["waits"]), ("why", []))
        self.assertTrue(card["has_record"])

    def test_unknown_and_invalid_ids(self) -> None:
        self.run_cli("card", "--session", OTHER, expect=2)
        self.run_cli("card", "--session", "../etc", expect=2)

    def test_card_refreshes_every_source_and_saves_it(self) -> None:
        self.write(user(pr_url(1)))
        card = self.card()
        self.assertEqual(self.asked, [1])
        self.assertEqual(card["prs"][0]["title"], "A PR")
        self.add("--kind", "later")
        self.assertEqual(self.session()["prs"][0]["title"], "A PR")
        self.assertEqual(self.asked, [1])

    def test_card_leaves_a_fresh_status_alone(self) -> None:
        self.created(1)
        self.card()
        self.advance(minutes=9)
        self.card()
        self.assertEqual(self.asked, [1])
        self.advance(minutes=1)
        self.card()
        self.assertEqual(self.asked, [1, 1])

    def test_card_counts_a_merged_pr_as_old_after_ten_minutes(self) -> None:
        self.status = {**self.status, "state": "merged"}
        self.created(1)
        self.card()
        self.advance(minutes=10)
        self.card()
        self.assertEqual(self.asked, [1, 1])

    def test_card_asks_at_most_six_in_parallel(self) -> None:
        self.created(*range(1, 9))
        barrier = threading.Barrier(6, timeout=5)

        def status(repo: str, number: int, run=None) -> dict:
            barrier.wait()
            self.asked.append(number)
            return self.status

        sys.modules["waiting_github"].pr_status = status
        self.assertEqual(len(self.card()["prs"]), 8)
        self.assertEqual(len(self.asked), 6)

    def test_card_works_before_the_state_folder_exists(self) -> None:
        self.state_dir.rmdir()
        self.write(user(pr_url(1)))
        self.assertEqual(self.card()["prs"][0]["title"], "A PR")
        self.assertEqual(self.asked, [1])

    def test_card_answers_when_github_does_not(self) -> None:
        def fail(repo: str, number: int, run=None) -> dict:
            raise RuntimeError("gh is down")

        sys.modules["waiting_github"].pr_status = fail
        self.created(1)
        self.assertIsNone(self.card()["prs"][0]["title"])

    def test_branch_of_the_working_folder(self) -> None:
        repo = self.root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "topic", str(repo)], check=True)
        self.agents(closed=[{"sessionId": SID, "cwd": str(repo), "windowTitle": "x"}])
        self.assertEqual(self.card()["branch"], "topic")

    def test_branch_is_null_without_git(self) -> None:
        with mock.patch.object(subprocess, "run", side_effect=OSError("no git")):
            self.assertIsNone(waiting._git_branch("/w"))
        with mock.patch.object(
            subprocess, "run", side_effect=subprocess.TimeoutExpired("git", 2),
        ):
            self.assertIsNone(waiting._git_branch("/w"))
        self.assertIsNone(waiting._git_branch(None))

    def cached(self, sid: str = SID) -> dict:
        return self.run_cli("card", "--session", sid, "--cached")["card"]

    def test_cached_runs_no_process(self) -> None:
        self.add("--kind", "later", "--note", "why")
        self.created(1)
        self.card()
        self.asked.clear()
        repo = self.root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "topic", str(repo)], check=True)
        self.agents(closed=[{"sessionId": SID, "cwd": str(repo), "windowTitle": "x"}])
        with (
            mock.patch.object(subprocess, "run", side_effect=AssertionError("ran a process")),
            mock.patch.object(subprocess, "Popen", side_effect=AssertionError("ran a process")),
        ):
            card = self.cached()
        self.assertEqual(self.asked, [])
        self.assertEqual((card["title"], card["note"]), ("x", "why"))
        self.assertEqual([pr["number"] for pr in card["prs"]], [1])
        self.assertIsNone(card["branch"])

    def test_cached_shows_what_the_last_refresh_saved(self) -> None:
        self.created(1)
        self.card()
        self.advance(hours=1)
        self.assertEqual(self.cached()["prs"][0]["title"], "A PR")
        self.assertEqual(self.asked, [1])

    def test_cached_has_every_field_of_the_plain_card(self) -> None:
        self.add("--kind", "later")
        self.assertEqual(set(self.cached()), set(self.card()))

    def test_cached_refuses_an_unknown_session(self) -> None:
        self.run_cli("card", "--session", OTHER, "--cached", expect=2)

    def test_cached_reads_no_transcript(self) -> None:
        self.created(1)
        self.card()
        self.write(user(pr_url(2)))
        self.advance(hours=1)
        before = (self.state_dir / "pr_cache.json").read_text()
        self.assertEqual([pr["number"] for pr in self.cached()["prs"]], [1])
        self.assertEqual((self.state_dir / "pr_cache.json").read_text(), before)
        self.assertEqual(sorted(pr["number"] for pr in self.card()["prs"]), [1, 2])


def stamped(record: dict, moment: datetime) -> dict:
    """A transcript record with omp's UTC timestamp."""
    utc = moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
    return {**record, "timestamp": f"{utc}Z"}


class SeenStopTests(SessionPrCase):
    """Seen closes what is on me; Stop ends what is still watched."""

    def fired(self, label: str = "Ping") -> str:
        wait = self.add("--kind", "time", "--in", "1s", "--label", label)["id"]
        self.advance(seconds=2)
        self.tick()
        return wait

    def history(self) -> list[str]:
        return [line["text"] for line in self.state()["sessions"][SID]["history"]]

    def states(self) -> dict[str, str]:
        return {wait["id"]: wait["state"] for wait in self.state()["sessions"][SID]["waits"]}

    def test_seen_closes_only_what_is_on_me_and_logs_it(self) -> None:
        ping = self.fired()
        watched = self.add("--kind", "time", "--in", "1d", "--label", "Watched")["id"]
        self.run_cli("mark-seen", "--session", SID)
        self.assertEqual(self.states(), {ping: "done", watched: "waiting"})
        self.assertIn("Seen: Ping", self.history())
        self.assertFalse(any(text.endswith("Watched") for text in self.history()))
        answer = self.run_cli("mark-seen", "--session", SID, expect=2)
        self.assertEqual(answer["error"], "nothing is on you")
        self.assertEqual(self.states()[watched], "waiting")

    def test_seen_with_nothing_set_aside_is_refused(self) -> None:
        self.assertEqual(self.run_cli("mark-seen", "--session", SID, expect=2)["error"], "nothing is on you")

    def test_seen_refuses_a_wait_still_watched(self) -> None:
        self.fired()
        watched = self.add("--kind", "time", "--in", "1d")["id"]
        self.run_cli("mark-seen", "--session", SID, "--wait", watched, expect=2)
        self.assertEqual(self.states()[watched], "waiting")

    def test_stop_one_ends_it_and_leaves_the_rest(self) -> None:
        ping = self.fired()
        watched = self.add("--kind", "time", "--in", "1d", "--label", "Watched")["id"]
        self.run_cli("stop", "--session", SID, "--wait", watched)
        self.assertEqual(self.states(), {ping: "fired", watched: "done"})
        self.assertIn("Stopped: Watched", self.history())

    def test_stop_all_ends_every_wait_and_its_chain(self) -> None:
        ping = self.fired()
        first = self.add("--kind", "something", "--what", "x", "--label", "First")["id"]
        second = self.add("--kind", "later", "--label", "Second", "--after", first)["id"]
        self.assertEqual(self.session()["stoppable"], 3)
        view = self.run_cli("stop", "--session", SID, "--all")["view"]
        self.assertEqual(view["sessions"], [])
        self.assertEqual(self.states(), {ping: "done", first: "done", second: "done"})
        self.assertEqual(
            [wait["next"] for wait in self.state()["sessions"][SID]["waits"]], [None, None, None],
        )
        self.assertEqual(
            [text for text in self.history() if text.startswith("Stopped: ")],
            ["Stopped: Ping", "Stopped: First", "Stopped: Second"],
        )
        self.assertFalse(any(text.startswith("Next: ") for text in self.history()))

    def test_on_you_lists_what_came_and_stoppable_counts_every_open_wait(self) -> None:
        ping = self.fired()
        self.add("--kind", "time", "--in", "1d")
        seen = self.session()
        fired = self.wait_of(ping)
        self.assertEqual(seen["on_you"], [{
            "id": ping, "label": "Ping", "mark": fired["mark"], "text": fired["fired_text"], "report": None,
        }])
        self.assertEqual(seen["stoppable"], 2)
        self.run_cli("mark-seen", "--session", SID)
        seen = self.session()
        self.assertEqual((seen["on_you"], seen["stoppable"]), ([], 1))

    def test_a_pane_has_on_you_and_stoppable_too(self) -> None:
        self.run_cli("add", "--pane", "51", "--kind", "job")
        pane = self.run_cli("list")["panes"][0]
        self.assertEqual((pane["on_you"], pane["stoppable"]), ([], 1))

    def test_seen_skips_a_status_the_chain_it_moved_on_replaced(self) -> None:
        ran = self.add("--kind", "time", "--in", "1s", "--prompt", "go", "--label", "Ran")["id"]
        status = self.add("--kind", "something", "--what", "status", "--label", "Status", "--deadline-in", "1s")["id"]
        step = self.add("--kind", "something", "--what", "after", "--after", ran)["id"]
        self.advance(seconds=2)
        self.tick()
        action = self.run_cli("claim-prompts", "--session", SID)["prompts"][0]["action"]
        self.run_cli("ack", "--action", action, "--result", "failed")
        self.assertEqual([entry["id"] for entry in self.session()["on_you"]], [ran, status])
        self.run_cli("mark-seen", "--session", SID)
        self.assertEqual(self.states(), {status: "done", ran: "done", step: "waiting"})
        self.assertIn("Done: Status", self.history())
        self.assertNotIn("Seen: Status", self.history())

    def test_a_review_wait_reports_the_agents_last_reply(self) -> None:
        self.write(stamped(said("before the prompt"), self.clock - timedelta(minutes=1)))
        wait, _ = self.queued_prompt()
        delivered = self.clock
        reply = "Done.  Merged\n the PR " + "and more " * 60
        self.write(
            stamped(user("go"), delivered),
            stamped(said("first look"), delivered + timedelta(seconds=1)),
            stamped(call("c1", "ls"), delivered + timedelta(seconds=2)),
            stamped(said(reply), delivered + timedelta(seconds=3)),
            stamped(call("c2", "ls"), delivered + timedelta(seconds=4)),
        )
        self.run_cli("event", "--type", "turn_finished", "--session", SID)
        (entry,) = self.session()["on_you"]
        self.assertEqual((entry["id"], entry["text"]), (wait, "ran, take a look"))
        self.assertEqual(len(entry["report"]), waiting.REPORT_CHARS)
        self.assertTrue(entry["report"].startswith("Done. Merged the PR and more"))
        self.assertTrue(entry["report"].endswith("…"))
        self.assertEqual(self.run_cli("card", "--session", SID, "--cached")["card"]["on_you"], [entry])

    def test_a_review_wait_with_no_reply_since_reports_nothing(self) -> None:
        self.write(stamped(said("before the prompt"), self.clock - timedelta(minutes=1)))
        self.queued_prompt()
        self.run_cli("event", "--type", "turn_finished", "--session", SID)
        self.assertIsNone(self.session()["on_you"][0]["report"])


class OutcomeTests(Case):
    """What a wait came to, and how every surface says it."""

    def test_precedence_of_one_wait(self) -> None:
        for state, on_me, overdue, outcome in (
            ("pending", False, False, None),
            ("waiting", False, False, None),
            ("fired", False, False, "fulfilled"),
            ("acting", False, False, None),
            ("review", False, False, "fulfilled"),
            ("needs_attention", False, False, "attention"),
            ("waiting", True, False, "attention"),
            ("acting", True, False, "attention"),
            ("waiting", False, True, "overdue"),
            ("waiting", True, True, "attention"),
            ("fired", False, True, "overdue"),
            ("needs_attention", False, True, "attention"),
        ):
            with self.subTest(state=state, on_me=on_me, overdue=overdue):
                wait = {"state": state, "on_me": on_me, "overdue": overdue}
                self.assertEqual(waiting._outcome(wait), outcome)

    def test_a_waiting_wait_has_none(self) -> None:
        wait = self.add("--kind", "time", "--in", "1h")
        self.assertEqual((wait["outcome"], wait["fired_text"], wait["fired_at"]), (None, None, None))
        seen = self.session()
        self.assertEqual((seen["outcome"], seen["headline"]), (None, None))
        self.assertTrue(seen["line"].startswith("⏲ "), seen["line"])

    def test_other_waiting_kinds_keep_the_hourglass(self) -> None:
        self.add("--kind", "job", "--pane", "51")
        self.assertTrue(self.session()["line"].startswith("⏳ "))

    def test_a_time_wait_says_how_long_it_ran_from_when_its_step_began(self) -> None:
        first = self.add("--kind", "something", "--what", "x")["id"]
        second = self.add("--kind", "time", "--in", "2d", "--after", first)["id"]
        self.advance(days=3)
        self.run_cli("what", "--session", SID, "--text", "")
        self.advance(days=2)
        self.tick()
        seen = self.wait_of(second)
        self.assertEqual(
            (seen["outcome"], seen["fired_text"], seen["fired_at"]),
            ("fulfilled", "2d passed", self.clock.isoformat()),
        )
        session = self.session()
        self.assertEqual((session["outcome"], session["headline"]), ("fulfilled", "✅ 2d passed"))

    def test_command_fired_text(self) -> None:
        run = [sys.executable, "-c", "print('green')"]
        exited = self.add("--kind", "command", "--argv", json.dumps(run))["id"]
        matched = self.add(
            "--kind", "command", "--argv", json.dumps(run), "--predicate", "stdout_regex:green",
        )["id"]
        self.tick()
        self.assertEqual(self.wait_of(exited)["fired_text"], "command exited 0")
        self.assertEqual(self.wait_of(matched)["fired_text"], "command output matched")

    def test_release_fired_text(self) -> None:
        wait = self.add("--kind", "release", "--repo", "a/b", "--interval", "1m")["id"]
        with mock.patch.object(waiting, "_run_argv", return_value=(0, "v1.0.0\n", "")):
            self.tick()
        self.advance(minutes=2)
        with mock.patch.object(waiting, "_run_argv", return_value=(0, "v2.0.0\n", "")):
            self.tick()
        self.assertEqual(self.wait_of(wait)["fired_text"], "v2.0.0 released")
        self.assertEqual(self.session()["history"][-1]["text"], "v1.0.0 → v2.0.0")

    def test_a_pr_fired_text_names_the_pr(self) -> None:
        self.github(poll=lambda cond, memo, **kwargs: poll_result(
            memo, fired=True, fire_text="greptile reviewed abc1234",
        ))
        wait = self.add("--kind", "pr", "--repo", "o/r", "--number", "7", "--until", "review_on_head")
        self.tick()
        self.assertEqual(self.wait_of(wait["id"])["fired_text"], "PR#7 greptile reviewed abc1234")
        self.assertEqual(self.session()["history"][-1]["text"], "greptile reviewed abc1234")

    def test_a_session_whose_prompt_runs_is_at_work_not_fulfilled(self) -> None:
        self.agents(SID)
        self.heartbeat()
        wait, _ = self.queued_prompt()
        seen = self.wait_of(wait)
        self.assertEqual(
            (seen["state"], seen["ball"], seen["outcome"], seen["progress"]),
            ("acting", "on_machine", None, "agent on it"),
        )
        session = self.session()
        self.assertEqual((session["outcome"], session["headline"]), (None, None))
        self.assertTrue(session["line"].startswith("🔧 "))

    def test_a_review_says_the_prompt_ran(self) -> None:
        self.agents(SID)
        self.heartbeat()
        self.queued_prompt("--note", "x")
        self.run_cli("event", "--type", "turn_finished", "--session", SID)
        self.advance(hours=1)
        seen = self.session()
        self.assertEqual(seen["outcome"], "fulfilled")
        self.assertEqual(seen["line"], '✅ 1s passed · prompt ran, take a look · 1h · "x"')

    def test_a_closed_session_needs_me(self) -> None:
        self.agents(SID)
        self.heartbeat()
        wait = self.add("--kind", "time", "--in", "1s", "--prompt", "go")["id"]
        self.advance(seconds=2)
        self.agents()
        self.tick()
        seen = self.session()
        self.assertIsNone(self.wait_of(wait)["fired_text"])
        self.assertEqual(
            (seen["outcome"], seen["headline"]), ("attention", f"⚠️ {waiting.REASON_CLOSED}"),
        )
        self.assertTrue(seen["line"].startswith("⚠️ "), seen["line"])
        self.assertEqual(seen["history"][-1]["kind"], "attention")

    def test_a_failed_job_needs_me(self) -> None:
        self.add("--kind", "job", "--pane", "51")
        self.run_cli("event", "--type", "job_finished", "--pane", "51", "--status", "3")
        seen = self.session()
        self.assertEqual((seen["outcome"], seen["headline"]), ("attention", "⚠️ Exited with status 3"))
        self.assertEqual(seen["history"][-1]["kind"], "attention")

    def test_overdue(self) -> None:
        self.add("--kind", "something", "--what", "Reply", "--deadline-in", "1h")
        self.advance(hours=2)
        self.tick()
        seen = self.session()
        self.assertEqual((seen["outcome"], seen["headline"]), ("overdue", "⏰ Reply overdue"))
        self.assertEqual(seen["status"], "⏰ Reply · 2h")
        self.assertEqual(seen["history"][-1]["kind"], "attention")

    def test_the_worst_wait_decides_the_session(self) -> None:
        self.add("--kind", "time", "--in", "1h", "--label", "Soon")
        self.add("--kind", "something", "--what", "Reply", "--deadline-in", "2h")
        job = self.add("--kind", "job", "--pane", "51")["id"]
        self.advance(hours=1)
        self.tick()
        self.assertEqual(self.session()["outcome"], "fulfilled")
        self.advance(hours=2)
        self.tick()
        seen = self.session()
        self.assertEqual(seen["outcome"], "overdue")
        self.assertTrue(seen["line"].startswith("⏰ Reply"), seen["line"])
        self.run_cli("event", "--type", "job_finished", "--pane", "51", "--status", "1")
        self.assertEqual(self.wait_of(job)["outcome"], "attention")
        seen = self.session()
        self.assertEqual((seen["outcome"], seen["headline"]), ("attention", "⚠️ Exited with status 1"))

    def test_a_fired_wait_outranks_a_waiting_one_on_the_machine(self) -> None:
        self.add("--kind", "job", "--pane", "51")
        self.add("--kind", "time", "--in", "1s")
        self.advance(seconds=2)
        self.tick()
        seen = self.session()
        self.assertEqual(seen["outcome"], "fulfilled")
        self.assertTrue(seen["line"].startswith("✅ "))

    def test_a_pane_has_an_outcome_too(self) -> None:
        self.run_cli("add", "--pane", "51", "--kind", "job")
        self.run_cli("event", "--type", "job_finished", "--pane", "51", "--status", "0")
        pane = self.run_cli("list")["panes"][0]
        self.assertEqual(
            (pane["outcome"], pane["headline"]), ("fulfilled", "✅ Job in pane 51: finished"),
        )
        self.assertTrue(pane["line"].startswith("✅ "))

    def test_a_time_wait_says_what_was_asked_for_however_late_the_tick(self) -> None:
        wait = self.add("--kind", "time", "--in", "90m")["id"]
        self.advance(hours=9)
        self.tick()
        self.assertEqual(self.wait_of(wait)["fired_text"], "90m passed")

    def test_a_clock_time_is_rounded(self) -> None:
        self.advance(seconds=7)
        wait = self.add("--kind", "time", "--at", (T0 + timedelta(hours=3)).isoformat())["id"]
        self.advance(hours=4)
        self.tick()
        self.assertEqual(self.wait_of(wait)["fired_text"], "2h passed")

    def test_a_snoozed_time_wait_counts_from_the_snooze(self) -> None:
        wait = self.add("--kind", "time", "--in", "2d")["id"]
        self.advance(days=3)
        self.tick()
        self.run_cli("snooze", "--session", SID, "--wait", wait, "--for", "1h")
        self.advance(hours=2)
        self.tick()
        self.assertEqual(self.wait_of(wait)["fired_text"], "1h passed")

    def test_a_snooze_ends_the_outcome(self) -> None:
        wait = self.add("--kind", "time", "--in", "1h")["id"]
        self.advance(hours=2)
        self.tick()
        self.run_cli("snooze", "--session", SID, "--wait", wait, "--for", "1h")
        seen = self.wait_of(wait)
        self.assertEqual((seen["outcome"], seen["fired_text"], seen["fired_at"]), (None, None, None))

    def test_a_refired_job_keeps_what_happened(self) -> None:
        wait = self.add("--kind", "job", "--pane", "51", "--label", "Build")["id"]
        self.run_cli("event", "--type", "job_finished", "--pane", "51", "--status", "0")
        self.run_cli("snooze", "--session", SID, "--wait", wait, "--for", "1h")
        self.advance(hours=2)
        self.tick()
        self.assertEqual(self.wait_of(wait)["fired_text"], "Build: finished")
        self.assertEqual(self.wait_of(wait)["fired_at"], self.clock.isoformat())

    def test_history_marks_the_fulfilment(self) -> None:
        self.add("--kind", "something", "--what", "x")
        self.add("--kind", "time", "--in", "1s", "--label", "Soon")
        self.run_cli("what", "--session", SID, "--text", "y")
        self.advance(seconds=2)
        self.tick()
        self.assertEqual(
            [(item["text"], item["kind"]) for item in self.session()["history"]],
            [("Waiting for: y", None), ("Soon", "fired")],
        )

    def test_a_passed_verify_is_marked_in_the_history(self) -> None:
        self.agents(SID)
        self.heartbeat()
        self.add("--kind", "later")
        verify = json.dumps({"type": "command", "argv": [sys.executable, "-c", "pass"]})
        self.queued_prompt("--verify", verify)
        self.run_cli("event", "--type", "turn_finished", "--session", SID)
        self.tick()
        last = self.session()["history"][-1]
        self.assertTrue(last["text"].startswith("Verified:"), last)
        self.assertEqual(last["kind"], "fired")

    def test_state_from_before_outcomes_still_shows(self) -> None:
        wait = self.add("--kind", "time", "--in", "1h", "--label", "Retry")["id"]
        self.advance(hours=2)
        self.tick()
        state = self.state()
        record = state["sessions"][SID]
        for field in ("armed_at", "fired_text", "fired_at"):
            del record["waits"][0][field]
        for entry in record["history"]:
            del entry["kind"]
        (self.state_dir / "waiting.json").write_text(json.dumps(state))
        seen = self.wait_of(wait)
        self.assertEqual((seen["fired_text"], seen["fired_at"]), ("Retry", T0.isoformat()))
        self.assertIsNone(self.session()["history"][-1]["kind"])

    def test_a_command_is_labelled_by_its_whole_line(self) -> None:
        wait = self.add("--kind", "command", "--argv", "bash -c 'test -f /tmp/x'")
        self.assertEqual(wait["label"], "bash -c 'test -f /tmp/x'")
        self.assertEqual(wait["summary"], "bash -c 'test -f /tmp/x'")
        long = self.add("--kind", "command", "--argv", "echo " + "x" * 100)
        self.assertEqual(len(long["label"]), 60)


class OnTimerTests(Case):
    def test_a_time_wait_is_on_a_timer_until_it_fires(self) -> None:
        wait = self.add("--kind", "time", "--in", "1h")["id"]
        self.assertEqual(self.wait_of(wait)["ball"], "on_timer")
        seen = self.run_cli("list")
        self.assertEqual(seen["sessions"][0]["ball"], "on_timer")
        self.assertEqual(seen["counts"]["on_timer"], 1)
        self.assertNotIn("scheduled", seen["counts"])
        self.advance(hours=2)
        self.tick()
        self.assertEqual(self.wait_of(wait)["ball"], "on_me")

    def test_ball_order(self) -> None:
        self.assertEqual(
            waiting.BALLS, ("on_me", "on_machine", "on_timer", "on_someone", "on_my_time"),
        )

    def test_a_machine_wait_outranks_the_timer_in_one_session(self) -> None:
        self.add("--kind", "time", "--in", "1h")
        self.add("--kind", "job", "--pane", "51")
        self.assertEqual(self.session()["ball"], "on_machine")


class MergedPrTests(Case):
    def chain(self, *argv: str, session: str = SID) -> list[str]:
        return [wait["id"] for wait in self.run_cli(
            "babysit-pr", "--session", session, "--repo", "o/r", "--number", "7", *argv,
        )["waits"]]

    def waits(self, sid: str = SID) -> dict:
        return {wait["id"]: wait for wait in self.state()["sessions"][sid]["waits"]}

    def merged(self, **overrides) -> None:
        self.github(poll=lambda cond, memo, **kwargs: poll_result(memo, merged=True, **overrides))

    def test_a_merge_jumps_the_chain_to_its_end(self) -> None:
        self.github()
        review, ready, merged, released = self.chain("--no-fix", "--released")
        self.merged()
        actions = self.tick()["actions"]
        waits = self.waits()
        self.assertEqual(
            [waits[wait]["state"] for wait in (review, ready, merged, released)],
            ["done", "done", "fired", "waiting"],
        )
        seen = self.session()
        self.assertEqual((seen["outcome"], seen["headline"]), ("fulfilled", "✅ PR#7 merged"))
        self.assertEqual(
            [(item["text"], item["kind"]) for item in seen["history"]],
            [
                ("PR#7 merged", "fired"),
                ("Next: Released", None),
                ("PR#7 merged: Ready no longer needed", None),
                ("PR#7 merged: Review on head no longer needed", None),
            ],
        )
        self.assertEqual([action["text"] for action in actions], ["PR#7 merged"])

    def test_a_chain_without_a_release_ends_on_the_merge(self) -> None:
        self.github()
        waits = self.chain("--no-fix")
        self.merged()
        self.tick()
        self.assertEqual([self.waits()[wait]["state"] for wait in waits], ["done", "done", "fired"])

    def test_a_ready_wait_on_a_merged_pr_is_done(self) -> None:
        self.merged()
        wait = self.add("--kind", "pr", "--repo", "o/r", "--number", "7", "--note", "n")["id"]
        answer = self.tick()
        self.assertEqual(self.waits()[wait]["state"], "done")
        self.assertEqual(answer["view"]["sessions"], [])
        history = self.state()["sessions"][SID]["history"]
        self.assertEqual(history[-1]["text"], "PR#7 merged: Ready no longer needed")
        self.assertEqual(
            [action["text"] for action in answer["actions"]], ["PR#7 merged: Ready no longer needed"],
        )

    def test_a_pr_its_waits_saw_merged_reads_merged_before_the_status_refresh(self) -> None:
        self.github()  # its pr_status still says open
        self.chain("--no-fix")
        self.merged()
        self.tick()
        self.assertEqual(
            [(pr["title"], pr["state"]) for pr in self.session()["prs"]], [("PR 7", "merged")],
        )

    def test_a_merge_wins_over_a_condition_that_also_holds(self) -> None:
        self.merged(fired=True, fire_text="ready on abc")
        wait = self.add("--kind", "pr", "--repo", "o/r", "--number", "7")["id"]
        self.tick()
        self.assertEqual(self.waits()[wait]["state"], "done")

    def test_a_merge_settles_waits_of_other_sessions_and_only_that_pr(self) -> None:
        # Only the lowercase spelling's poll sees it, so the others hear from the rule.
        self.github(poll=lambda cond, memo, **kwargs: poll_result(
            memo, merged=cond["repo"] == "o/r" and cond["number"] == 7,
        ))
        first = self.add("--kind", "pr", "--repo", "o/r", "--number", "7")["id"]
        same = self.add("--kind", "pr", "--repo", "O/R", "--number", "7", session=OTHER)["id"]
        other = self.add("--kind", "pr", "--repo", "o/r", "--number", "8", session=OTHER)["id"]
        self.tick()
        self.assertEqual(self.waits()[first]["state"], "done")
        self.assertEqual(self.waits(OTHER)[same]["state"], "done")
        self.assertEqual(self.waits(OTHER)[other]["state"], "waiting")

    def test_a_merged_wait_seeing_the_merge_closes_the_steps_left_open(self) -> None:
        def poll(cond, memo, **kwargs):
            if cond["until"] == "merged":
                return poll_result(memo, merged=True, fired=True, fire_text="PR#7 merged")
            return poll_result(memo, fired=True, fire_text=f"{cond['until']} held")

        self.github(poll=poll)
        review, ready, merged = self.chain("--no-fix")
        self.tick()
        self.advance(minutes=1)
        self.tick()
        self.advance(minutes=1)
        self.tick()
        waits = self.waits()
        self.assertEqual(
            [waits[wait]["state"] for wait in (review, ready, merged)], ["done", "done", "fired"],
        )

    def test_a_merge_seen_by_a_release_wait_leaves_it_watching(self) -> None:
        self.merged()
        released = self.add("--kind", "pr", "--repo", "o/r", "--number", "7", "--until", "released")["id"]
        ready = self.add("--kind", "pr", "--repo", "o/r", "--number", "7", session=OTHER)["id"]
        self.tick()
        self.assertEqual(self.waits()[released]["state"], "waiting")
        self.assertEqual(self.waits(OTHER)[ready]["state"], "done")

    def test_a_merge_seen_with_an_error_still_counts(self) -> None:
        def poll(cond, memo, **kwargs):
            released = cond["until"] == "released"
            return poll_result(memo, merged=released, error="gh failed" if released else None)

        self.github(poll=poll)
        self.add("--kind", "pr", "--repo", "o/r", "--number", "7", "--until", "released")
        ready = self.add("--kind", "pr", "--repo", "o/r", "--number", "7", session=OTHER)["id"]
        self.tick()
        self.assertEqual(self.waits(OTHER)[ready]["state"], "done")

    def test_a_merge_drops_the_fix_prompt_still_queued(self) -> None:
        self.agents(SID)
        self.heartbeat()
        self.github(poll=lambda cond, memo, **kwargs: poll_result(
            memo, fired=cond["until"] == "review_on_head", fire_text="review landed",
        ))
        review, *_ = self.chain()
        self.tick()
        self.assertEqual(self.waits()[review]["state"], "acting")
        self.merged()
        self.add("--kind", "pr", "--repo", "o/r", "--number", "7", "--until", "activity", session=OTHER)
        self.tick()
        self.assertEqual(self.waits()[review]["state"], "done")
        self.assertEqual(self.run_cli("claim-prompts", "--session", SID)["prompts"], [])

    def test_a_merge_fires_a_merged_wait_that_needs_me(self) -> None:
        self.github(poll=lambda cond, memo, **kwargs: poll_result(memo, error="404"))
        wait = self.add("--kind", "pr", "--repo", "o/r", "--number", "7", "--until", "merged")["id"]
        for _ in range(waiting.MAX_ERRORS):
            self.tick()
            self.advance(minutes=4)
        self.assertEqual(self.waits()[wait]["state"], "needs_attention")
        self.merged()
        self.add("--kind", "pr", "--repo", "o/r", "--number", "7", session=OTHER)
        self.tick()
        self.assertEqual(self.waits()[wait]["state"], "fired")

    def test_a_status_refresh_sees_the_merge_nothing_polls(self) -> None:
        self.agents(SID)
        self.heartbeat()
        self.github(
            poll=lambda cond, memo, **kwargs: poll_result(
                memo, fired=cond["until"] == "review_on_head", fire_text="review landed",
            ),
            pr_status=lambda repo, number, run=None: {
                "title": "A PR", "state": "merged", "review": None, "checks": None,
            },
        )
        review, ready, merged = self.chain()
        self.tick()
        waits = self.waits()
        self.assertEqual(
            [waits[wait]["state"] for wait in (review, ready, merged)], ["done", "done", "fired"],
        )
        self.assertEqual(self.run_cli("claim-prompts", "--session", SID)["prompts"], [])

    def test_an_open_status_settles_nothing(self) -> None:
        self.github()
        review, *_ = self.chain("--no-fix")
        self.tick()
        self.assertEqual(self.waits()[review]["state"], "waiting")

    def test_closed_without_merge_needs_me_and_stays_watched(self) -> None:
        closed = {"key": "closed:x", "text": "PR#7 closed without merging", "on_me": True}
        self.github()
        review, ready, merged = self.chain("--no-fix")
        lone = self.add("--kind", "pr", "--repo", "o/r", "--number", "7", "--until", "merged")["id"]
        self.github(poll=lambda cond, memo, **kwargs: poll_result(memo, events=[closed], next_poll_s=3600))
        self.tick()
        waits = self.waits()
        self.assertEqual(
            [waits[wait]["state"] for wait in (review, ready, merged, lone)],
            ["waiting", "pending", "pending", "waiting"],
        )
        for wait in (review, lone):
            self.assertEqual(self.wait_of(wait)["outcome"], "attention")
        seen = self.session()
        self.assertEqual(
            (seen["outcome"], seen["headline"]), ("attention", "⚠️ PR#7 closed without merging"),
        )

    def test_a_merged_wait_back_on_me_after_its_prompt_is_not_fired_again(self) -> None:
        self.agents(SID)
        self.heartbeat()
        self.github(
            poll=lambda cond, memo, **kwargs: poll_result(memo, merged=True, fired=True, fire_text="PR#7 merged"),
            pr_status=lambda repo, number, run=None: {
                "title": "A PR", "state": "merged", "review": None, "checks": None,
            },
        )
        verify = json.dumps({"type": "command", "argv": [sys.executable, "-c", "raise SystemExit(1)"]})
        wait = self.add(
            "--kind", "pr", "--repo", "o/r", "--number", "7", "--until", "merged",
            "--prompt", "go", "--verify", verify,
        )["id"]
        self.tick()
        action = self.run_cli("claim-prompts", "--session", SID)["prompts"][0]["action"]
        self.run_cli("ack", "--action", action, "--result", "delivered")
        self.run_cli("event", "--type", "turn_finished", "--session", SID)
        self.tick()
        self.assertEqual(self.waits()[wait]["state"], "needs_attention")
        self.assertEqual(self.run_cli("claim-prompts", "--session", SID)["prompts"], [])

    def test_a_snoozed_merged_wait_stays_snoozed(self) -> None:
        self.merged(fired=True, fire_text="PR#7 merged")
        wait = self.add("--kind", "pr", "--repo", "o/r", "--number", "7", "--until", "merged")["id"]
        self.tick()
        self.run_cli("snooze", "--session", SID, "--wait", wait, "--for", "1d")
        self.add("--kind", "pr", "--repo", "o/r", "--number", "7", "--until", "activity", session=OTHER)
        self.tick()
        self.assertEqual(self.waits()[wait]["state"], "snoozed")


class FiredPrTests(Case):
    """A fired `review_on_head` or `ready` is taken back when the PR moves under it."""

    def setUp(self) -> None:
        super().setUp()
        self.pr = {"head": "a" * 40, "merge_state": "CLEAN", "holds": True, "events": []}

        def poll(cond, memo, **kwargs):
            holds = self.pr["holds"] and cond["until"] in ("review_on_head", "ready")
            return poll_result(
                memo, fired=holds, fire_text=f"{cond['until']} on {self.pr['head'][:7]}" if holds else None,
                head=self.pr["head"], merge_state=self.pr["merge_state"], base="main",
                events=self.pr["events"],
            )

        self.github(poll=poll)

    def chain(self) -> list[str]:
        answer = self.run_cli("babysit-pr", "--session", SID, "--repo", "o/r", "--number", "7", "--no-fix")
        return [wait["id"] for wait in answer["waits"]]

    def states(self, ids: list[str]) -> list[str]:
        waits = {wait["id"]: wait for wait in self.state()["sessions"][SID]["waits"]}
        return [waits[wait]["state"] for wait in ids]

    def ticks(self, count: int) -> None:
        for _ in range(count):
            self.tick()
            self.advance(minutes=4)

    def ready(self) -> list[str]:
        return [self.add("--kind", "pr", "--repo", "o/r", "--number", "7", "--until", "ready")["id"]]

    def test_a_new_head_takes_ready_back_and_the_chain_waits_for_it_again(self) -> None:
        chain = self.chain()
        self.ticks(2)
        self.assertEqual(self.states(chain), ["fired", "fired", "waiting"])
        self.pr.update(head="b" * 40, holds=False)
        self.ticks(1)
        self.assertEqual(self.states(chain), ["waiting", "waiting", "pending"])
        seen = self.session()
        self.assertIn("PR#7 no longer ready: new head bbbbbbb", [item["text"] for item in seen["history"]])
        self.assertIsNone(seen["outcome"])
        self.pr["holds"] = True
        self.ticks(2)
        self.assertEqual(self.states(chain), ["fired", "fired", "waiting"])

    def test_a_conflict_takes_ready_back_on_the_same_head(self) -> None:
        ready = self.ready()
        self.ticks(1)
        self.assertEqual(self.states(ready), ["fired"])
        self.pr.update(merge_state="DIRTY", holds=False)
        self.ticks(1)
        self.assertEqual(self.states(ready), ["waiting"])
        self.assertEqual(self.session()["history"][-1]["text"], "PR#7 no longer ready: conflicts with main")

    def test_an_unchanged_pr_keeps_the_wait_fulfilled(self) -> None:
        ready = self.ready()
        self.ticks(3)
        self.assertEqual(self.states(ready), ["fired"])

    def test_a_new_babysit_replaces_the_sessions_old_chain_on_that_pr(self) -> None:
        old = self.chain()
        self.ticks(2)
        new = self.chain()
        self.assertEqual(self.states(old), ["done", "done", "done"])
        self.assertEqual(self.states(new), ["waiting", "pending", "pending"])
        self.assertIn("Replaced by a new babysit: Ready", [item["text"] for item in self.session()["history"]])

    def test_a_babysit_keeps_a_wait_of_its_own_on_that_pr(self) -> None:
        released = self.add("--kind", "pr", "--repo", "o/r", "--number", "7", "--until", "released", "--prompt", "build on it")["id"]
        self.chain()
        self.assertEqual(self.states([released]), ["waiting"])

    def test_every_chain_shows_in_the_line_and_the_next_step(self) -> None:
        self.chain()
        self.run_cli("babysit-pr", "--session", SID, "--repo", "o/r", "--number", "8", "--no-fix")
        seen = self.session()
        self.assertEqual(seen["threads"], ["⏳ PR#7 review on head", "⏳ PR#8 review on head"])
        self.assertIn("+ ⏳ PR#8 review on head", seen["line"])
        self.assertEqual(seen["next_step"], "PR#7 ready · PR#8 ready")
        self.assertTrue(seen["status"].endswith(" +1"))

    def test_a_fulfilled_session_names_what_it_still_waits_for(self) -> None:
        ready = self.ready()
        self.ticks(1)
        self.assertEqual(self.states(ready), ["fired"])
        self.assertEqual(self.session()["still_waiting"], [])
        self.add("--kind", "pr", "--repo", "o/r", "--number", "7", "--until", "released")
        seen = self.session()
        self.assertEqual(seen["outcome"], "fulfilled")
        self.assertEqual(seen["still_waiting"], ["⏳ PR#7 released"])
        self.assertEqual([w["mark"] for w in seen["waits"]], ["✅ ", "⏳ "])

    def test_a_review_holds_when_the_base_moves(self) -> None:
        review = [self.add("--kind", "pr", "--repo", "o/r", "--number", "7", "--until", "review_on_head")["id"]]
        self.ticks(1)
        self.pr["merge_state"] = "BEHIND"
        self.ticks(3)
        self.assertEqual(self.states(review), ["fired"])
        self.assertFalse([item for item in self.session()["history"] if "no longer" in item["text"]])

    def test_a_pr_closed_without_merging_brings_a_fulfilled_wait_back_to_me(self) -> None:
        ready = self.ready()
        self.ticks(1)
        self.pr.update(holds=False, events=[
            {"key": "closed:x", "text": "PR#7 closed without merging", "on_me": True},
        ])
        self.ticks(1)
        self.assertEqual(self.states(ready), ["needs_attention"])
        self.assertEqual(self.session()["headline"], "⚠️ PR#7 closed without merging")

    def test_a_fulfilled_wait_is_rechecked_on_time(self) -> None:
        self.ready()
        self.tick()
        self.advance(minutes=6)
        self.assertEqual(self.tick()["next_tick_s"], 180)

    def test_a_step_taken_back_counts_its_times_anew(self) -> None:
        ready = self.ready()[0]
        timer = self.add("--kind", "time", "--in", "1h", "--deadline-in", "2h", "--after", ready)["id"]
        self.tick()
        self.pr.update(head="b" * 40, holds=False)
        self.advance(minutes=4)
        self.tick()
        waits = {wait["id"]: wait for wait in self.state()["sessions"][SID]["waits"]}
        self.assertEqual(
            (waits[timer]["state"], waits[timer]["condition"]["at"], waits[timer]["deadline"]),
            ("pending", None, None),
        )
        self.advance(hours=3)
        self.pr["holds"] = True
        self.tick()
        waits = {wait["id"]: wait for wait in self.state()["sessions"][SID]["waits"]}
        self.assertEqual(
            (waits[timer]["state"], waits[timer]["condition"]["at"], waits[timer]["deadline"]),
            ("waiting", (self.clock + timedelta(hours=1)).isoformat(), (self.clock + timedelta(hours=2)).isoformat()),
        )

    def test_a_repeat_taken_back_counts_its_runs_anew(self) -> None:
        self.agents(SID)
        ready = self.ready()[0]
        repeat = self.add(
            "--kind", "repeat", "--every", "1h", "--times", "3", "--prompt", "ping", "--after", ready,
        )["id"]
        self.tick()
        self.advance(hours=1)
        self.heartbeat()
        self.tick()
        action = self.run_cli("claim-prompts", "--session", SID)["prompts"][0]["action"]
        self.run_cli("ack", "--action", action, "--result", "delivered")
        self.pr.update(head="b" * 40, holds=False)
        self.advance(minutes=4)
        self.tick()
        waits = {wait["id"]: wait for wait in self.state()["sessions"][SID]["waits"]}
        self.assertEqual((waits[repeat]["state"], waits[repeat]["repeat"]["runs"]), ("pending", 0))

    def test_a_new_babysit_carries_on_to_a_step_chained_after_the_old_one(self) -> None:
        old = self.chain()
        timer = self.add("--kind", "time", "--in", "1h", "--after", old[-1])["id"]
        new = self.chain()
        waits = {wait["id"]: wait for wait in self.state()["sessions"][SID]["waits"]}
        self.assertEqual(self.states(old), ["done", "done", "done"])
        self.assertEqual((waits[new[-1]]["next"], waits[timer]["state"]), (timer, "pending"))


class PinTests(Case):
    @property
    def pins_path(self) -> Path:
        return self.omp / "session-pins.json"

    def pins(self) -> list:
        return json.loads(self.pins_path.read_text())

    def seed(self, *sids: str) -> None:
        self.pins_path.write_text(json.dumps(list(sids)))

    def add_answer(self, *argv: str, sid: str = SID) -> dict:
        return self.run_cli("add", "--session", sid, *argv)

    def pin_wanted(self, sid: str = SID) -> bool | None:
        return self.state()["sessions"][sid]["pin_wanted"]

    def set_pin_wanted(self, value: bool) -> None:
        path = self.state_dir / "waiting.json"
        state = json.loads(path.read_text())
        state["sessions"][SID]["pin_wanted"] = value
        path.write_text(json.dumps(state))

    def hold_lock(self) -> socket.socket:
        holder = waiting._try_omp_lock(f"{self.pins_path}.lock")
        self.addCleanup(holder.close)
        fast = mock.patch.object(waiting, "PIN_LOCK_DELAY_S", 0)
        fast.start()
        self.addCleanup(fast.stop)
        return holder

    def test_later_appends_the_pin_once(self) -> None:
        self.seed("first", "second")
        answer = self.add_answer("--kind", "later")
        self.assertTrue(answer["pinned"])
        self.assertNotIn("pin_error", answer)
        self.add_answer("--kind", "later")
        self.assertEqual(self.pins(), ["first", "second", SID])

    def test_the_file_is_written_the_way_omp_writes_it(self) -> None:
        self.add_answer("--kind", "later")
        self.assertEqual(self.pins_path.read_bytes(), f'[\n\t"{SID}"\n]'.encode())
        if not WINDOWS:  # Windows files have no mode bits; the profile's ACL keeps them private.
            self.assertEqual(self.pins_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual([path.name for path in self.omp.iterdir()], ["session-pins.json"])

    def test_the_view_shows_the_pin_at_once(self) -> None:
        answer = self.add_answer("--kind", "later")
        self.assertTrue(answer["view"]["sessions"][0]["pinned"])
        saved = json.loads((self.state_dir / "waiting_view.json").read_text())
        self.assertTrue(saved["sessions"][0]["pinned"])

    def test_an_agent_pins_too(self) -> None:
        self.add_answer("--kind", "later", "--by", "agent")
        self.assertEqual(self.pins(), [SID])

    def test_other_kinds_leave_the_pin_alone(self) -> None:
        self.assertFalse(self.add_answer("--kind", "time", "--in", "1h")["pinned"])
        self.assertFalse(self.pins_path.exists())
        self.seed(SID)
        self.assertTrue(self.add_answer("--kind", "time", "--in", "1h")["pinned"])

    def test_a_pane_can_not_be_pinned(self) -> None:
        answer = self.run_cli("add", "--pane", "4", "--kind", "later")
        self.assertFalse(answer["pinned"])
        self.assertFalse(self.pins_path.exists())
        self.assertFalse(self.run_cli("stop", "--pane", "4", "--all")["pinned"])

    def test_stopping_the_later_wait_unpins_and_keeps_the_rest(self) -> None:
        self.seed("first", "second")
        wait = self.add_answer("--kind", "later")["wait"]["id"]
        self.seed("first", SID, "second")
        answer = self.run_cli("stop", "--session", SID, "--wait", wait)
        self.assertFalse(answer["pinned"])
        self.assertEqual(self.pins(), ["first", "second"])
        self.assertEqual(answer["view"]["sessions"][0]["session_id"], "first")
        self.assertEqual(len(answer["view"]["sessions"]), 2)

    def test_stopping_another_wait_keeps_the_pin(self) -> None:
        self.add_answer("--kind", "later")
        wait = self.add_answer("--kind", "time", "--in", "1h")["wait"]["id"]
        answer = self.run_cli("stop", "--session", SID, "--wait", wait)
        self.assertTrue(answer["pinned"])
        self.assertEqual(self.pins(), [SID])

    def test_a_second_later_wait_keeps_the_pin(self) -> None:
        first = self.add_answer("--kind", "later")["wait"]["id"]
        second = self.add_answer("--kind", "later")["wait"]["id"]
        self.assertTrue(self.run_cli("stop", "--session", SID, "--wait", first)["pinned"])
        self.assertFalse(self.run_cli("stop", "--session", SID, "--wait", second)["pinned"])
        self.assertEqual(self.pins(), [])

    def test_stop_all_unpins_a_pinned_session(self) -> None:
        self.seed("first", SID)
        self.add_answer("--kind", "time", "--in", "1h")
        self.assertFalse(self.run_cli("stop", "--session", SID, "--all")["pinned"])
        self.assertEqual(self.pins(), ["first"])

    def test_stop_all_with_no_record_unpins_too(self) -> None:
        self.seed(SID)
        self.assertFalse(self.run_cli("stop", "--session", SID, "--all")["pinned"])
        self.assertEqual(self.pins(), [])

    def test_stop_all_on_an_unpinned_session_writes_nothing(self) -> None:
        self.assertFalse(self.run_cli("stop", "--session", SID, "--all")["pinned"])
        self.assertFalse(self.pins_path.exists())

    def test_a_held_lock_leaves_the_record_and_reports_the_pin(self) -> None:
        self.seed("first")
        self.hold_lock()
        answer = self.add_answer("--kind", "later", "--note", "why")
        self.assertFalse(answer["pinned"])
        self.assertIn("locked", answer["pin_error"])
        self.assertEqual(self.pins(), ["first"])
        self.assertEqual(self.session()["note"], "why")

    def test_a_held_lock_only_postpones_the_unpin_on_stop(self) -> None:
        wait = self.add_answer("--kind", "later")["wait"]["id"]
        self.hold_lock()
        answer = self.run_cli("stop", "--session", SID, "--wait", wait)
        self.assertTrue(answer["pinned"])
        self.assertIn("pin_error", answer)
        self.assertEqual(self.state()["sessions"][SID]["waits"][0]["state"], "done")

    def test_a_held_lock_does_not_matter_when_nothing_changes(self) -> None:
        self.seed(SID)
        self.hold_lock()
        self.assertNotIn("pin_error", self.add_answer("--kind", "later"))

    def test_a_held_lock_is_no_error_when_stop_has_nothing_to_unpin(self) -> None:
        self.hold_lock()
        answer = self.run_cli("stop", "--session", SID, "--all")
        self.assertFalse(answer["pinned"])
        self.assertNotIn("pin_error", answer)

    def test_a_pin_is_written_under_the_state_lock(self) -> None:
        real = waiting._replace_pins
        held: list[bool] = []

        def write(path: Path, rows: list) -> None:
            descriptor = os.open(self.state_dir / "waiting.lock", os.O_RDWR)
            try:
                held.append(not state_paths.lock(descriptor, blocking=False))
            finally:
                os.close(descriptor)
            real(path, rows)

        with mock.patch.object(waiting, "_replace_pins", write):
            wait = self.add_answer("--kind", "later")["wait"]["id"]
            self.run_cli("stop", "--session", SID, "--wait", wait)
        self.assertEqual(held, [True, True])

    def test_the_attempt_under_the_state_lock_stays_short(self) -> None:
        self.assertLessEqual(waiting.PIN_LOCK_TRIES * waiting.PIN_LOCK_DELAY_S, 1)

    def test_stop_after_a_failed_pin_stays_unpinned_for_good(self) -> None:
        holder = self.hold_lock()
        wait = self.add_answer("--kind", "later")["wait"]["id"]
        self.assertTrue(self.pin_wanted())
        self.assertNotIn("pin_error", self.run_cli("stop", "--session", SID, "--wait", wait))
        self.assertIsNone(self.pin_wanted())
        holder.close()
        self.tick()
        self.assertFalse(self.pins_path.exists())

    def test_tick_pins_once_the_lock_frees(self) -> None:
        holder = self.hold_lock()
        self.add_answer("--kind", "later")
        self.tick()
        self.assertFalse(self.pins_path.exists())
        self.assertTrue(self.pin_wanted())
        holder.close()
        answer = self.tick()
        self.assertEqual(self.pins(), [SID])
        self.assertIsNone(self.pin_wanted())
        self.assertTrue(answer["view"]["sessions"][0]["pinned"])

    def test_tick_unpins_once_the_lock_frees(self) -> None:
        self.seed("first", SID)
        wait = self.add_answer("--kind", "later")["wait"]["id"]
        holder = self.hold_lock()
        self.run_cli("stop", "--session", SID, "--wait", wait)
        self.assertIs(self.pin_wanted(), False)
        holder.close()
        self.tick()
        self.assertEqual(self.pins(), ["first"])
        self.assertIsNone(self.pin_wanted())

    def test_tick_never_pins_a_session_whose_later_wait_is_stopped(self) -> None:
        wait = self.add_answer("--kind", "later")["wait"]["id"]
        self.run_cli("stop", "--session", SID, "--wait", wait)
        self.set_pin_wanted(True)
        self.tick()
        self.assertEqual(self.pins(), [])
        self.assertIsNone(self.pin_wanted())

    def test_tick_never_unpins_a_session_with_an_open_later_wait(self) -> None:
        self.add_answer("--kind", "later")
        self.set_pin_wanted(False)
        self.tick()
        self.assertEqual(self.pins(), [SID])
        self.assertIsNone(self.pin_wanted())

    def test_a_new_later_wait_replaces_a_failed_unpin(self) -> None:
        wait = self.add_answer("--kind", "later")["wait"]["id"]
        holder = self.hold_lock()
        self.run_cli("stop", "--session", SID, "--wait", wait)
        holder.close()
        self.add_answer("--kind", "later")
        self.assertIsNone(self.pin_wanted())
        self.tick()
        self.assertEqual(self.pins(), [SID])

    def test_stop_all_with_no_record_keeps_a_failed_unpin(self) -> None:
        self.seed(SID)
        holder = self.hold_lock()
        self.assertIn("pin_error", self.run_cli("stop", "--session", SID, "--all"))
        self.assertIs(self.pin_wanted(), False)
        self.assertIsNotNone(self.state()["sessions"][SID]["closed_at"])
        holder.close()
        self.tick()
        self.assertEqual(self.pins(), [])

    def test_a_held_lock_costs_a_tick_one_attempt(self) -> None:
        holder = self.hold_lock()
        self.add_answer("--kind", "later")
        self.add_answer("--kind", "later", sid=OTHER)
        real = waiting._try_omp_lock
        with mock.patch.object(waiting, "_try_omp_lock", side_effect=real) as attempts:
            self.tick()
        self.assertEqual(attempts.call_count, waiting.PIN_LOCK_TRIES)
        holder.close()
        self.tick()
        self.assertEqual(sorted(self.pins()), sorted([SID, OTHER]))

    def test_an_unreadable_file_is_kept_not_replaced(self) -> None:
        for text in ("{broken", '{"a": 1}'):
            with self.subTest(text=text):
                self.pins_path.write_text(text)
                answer = self.add_answer("--kind", "later")
                self.assertIn("pin_error", answer)
                self.assertEqual(self.pins_path.read_text(), text)

    def test_a_failed_write_only_costs_the_pin_and_leaves_no_temp_file(self) -> None:
        real = os.replace

        def replace(source: object, target: object) -> None:
            if Path(target) == self.pins_path:
                raise OSError("disk full")
            real(source, target)

        with mock.patch.object(waiting.os, "replace", replace):
            answer = self.add_answer("--kind", "later")
        self.assertEqual(answer["pin_error"], "disk full")
        self.assertEqual(list(self.omp.iterdir()), [])
        self.assertEqual(len(self.session()["waits"]), 1)

    def test_the_lock_is_released_after_a_write(self) -> None:
        self.add_answer("--kind", "later")
        holder = waiting._try_omp_lock(f"{self.pins_path}.lock")
        self.assertIsNotNone(holder)
        holder.close()

    def test_xxh64_vectors(self) -> None:
        self.assertEqual(waiting._xxh64(b"", 0), 0xef46db3751d8e999)
        self.assertEqual(waiting._xxh64(b"abc", 0), 0x44bc2cf5ad770999)

    def test_the_lock_name_is_omps(self) -> None:
        # Taken from a run against omp 18.16's own pi_natives FileLock.
        self.assertEqual(
            waiting._omp_lock_name("/tmp/agent/session-pins.json.lock"),
            "omp-file-lock-bd764e9a96bf2f299508f3ce85f622c4",
        )

    @unittest.skipUnless(WINDOWS, "omp's Windows lease is a named kernel mutex")
    def test_the_windows_lease_is_omps_named_mutex(self) -> None:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenMutexW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
        kernel32.OpenMutexW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        lock = f"{self.pins_path}.lock"
        # pi-natives' file_lock/windows.rs: `Global\` and the memory lock name.
        name = f"Global\\{waiting._omp_lock_name(lock)}"
        holder = waiting._try_omp_lock(lock)
        handle = kernel32.OpenMutexW(0x00100000, False, name)
        self.assertTrue(handle, ctypes.get_last_error())
        kernel32.CloseHandle(handle)
        self.assertIsNone(waiting._try_omp_lock(lock))
        holder.close()
        self.assertFalse(kernel32.OpenMutexW(0x00100000, False, name))
        self.assertFalse(Path(lock).exists(), "the lease is no file")

    def test_the_agent_dir_follows_omp(self) -> None:
        with mock.patch.dict(os.environ, {"PI_CODING_AGENT_DIR": "/a", "OMP_AGENT_DIR": "/b"}):
            self.assertEqual(waiting.Omp().root, Path("/a"))
        with mock.patch.dict(os.environ, {"OMP_AGENT_DIR": "/b"}):
            self.assertEqual(waiting.Omp().root, Path("/b"))
        with mock.patch.dict(os.environ, {"OMP_AGENT_DIR": ""}):
            self.assertEqual(waiting.Omp().root, self.root / "home" / ".omp" / "agent")


class ArgvTests(Case):
    def test_a_json_array(self) -> None:
        self.assertEqual(waiting._parse_argv('["ls", "-l a"]'), ["ls", "-l a"])

    @unittest.skipIf(WINDOWS, "Windows runs a command line in PowerShell, with no POSIX split")
    def test_a_command_line(self) -> None:
        self.assertEqual(
            waiting._parse_argv("curl -s 'http://x/y z' --retry 2"),
            ["curl", "-s", "http://x/y z", "--retry", "2"],
        )

    @unittest.skipIf(WINDOWS, "Windows runs a command line in PowerShell, with no POSIX split")
    def test_a_command_line_that_only_looks_like_an_array(self) -> None:
        self.assertEqual(waiting._parse_argv("[ -f /tmp/x ]"), ["[", "-f", "/tmp/x", "]"])

    def test_errors_name_both_forms(self) -> None:
        # PowerShell, not a split, parses a Windows command line: an open quote is its error.
        for text in ("", "[]", "[1]", *(() if WINDOWS else ("ls 'open",))):
            with self.subTest(text=text), self.assertRaises(waiting.BadInput) as caught:
                waiting._parse_argv(text)
            self.assertIn("JSON array", str(caught.exception))
            self.assertIn("command line", str(caught.exception))

    @unittest.skipIf(WINDOWS, "Windows runs a command line in PowerShell, with no POSIX split")
    def test_a_command_line_runs_without_a_shell(self) -> None:
        marker = self.root / "ran"
        script = f"import sys; open({str(marker)!r}, 'w').write(sys.argv[1])"
        argv = shlex.join([sys.executable, "-c", script, "a|b"])
        wait = self.add("--kind", "command", "--argv", argv)
        self.assertEqual(wait["condition"]["argv"][-1], "a|b")
        self.tick()
        self.assertEqual(marker.read_text(), "a|b")

    def test_a_windows_command_line_runs_in_powershell(self) -> None:
        line = "  gh pr view 7 | Select-String MERGED  "
        with mock.patch.object(waiting.state_paths, "WINDOWS", True):
            argv = waiting._parse_argv(line)
            self.assertEqual(waiting._parse_argv('["gh", "a b"]'), ["gh", "a b"])
            self.assertRaises(waiting.BadInput, waiting._parse_argv, "   ")
        self.assertEqual(argv, [*waiting.WINDOWS_SHELL, line.strip()])
        self.assertEqual(waiting._command_line({"argv": argv}), line.strip()[:waiting.COMMAND_LABEL_CHARS])

    @unittest.skipUnless(WINDOWS, "needs Windows PowerShell")
    def test_a_windows_command_line_gets_powershell_pipes(self) -> None:
        marker = self.root / "ran"
        line = f"Set-Content -LiteralPath '{marker}' -NoNewline -Value ('a', 'b' -join '|')"
        wait = self.add("--kind", "command", "--argv", line)
        self.assertEqual(wait["condition"]["argv"], [*waiting.WINDOWS_SHELL, line])
        self.tick()
        self.assertEqual(marker.read_text(), "a|b")


class MenuTests(Case):
    def test_menu_prints_the_shipped_items(self) -> None:
        items = self.run_cli("menu")["items"]
        self.assertEqual(items, json.loads(waiting.MENU_PATH.read_text())["items"])
        self.assertEqual(len({item["id"] for item in items}), len(items))
        for item in items:
            with self.subTest(item=item["id"]):
                self.assertTrue(item["label"] and item["detail"])
                self.assertLessEqual(set(item["targets"]), {"session", "pane"})

    def test_a_broken_menu_file_is_an_error(self) -> None:
        good = {"id": "a", "label": "A", "detail": "d", "targets": ["session"]}
        path = self.root / "menu.json"
        for text in (
            "{",
            json.dumps({"items": []}),
            json.dumps([good]),
            json.dumps({"items": [good, {**good, "label": "B"}]}),
            json.dumps({"items": [{**good, "label": " "}]}),
            json.dumps({"items": [{**good, "id": 3}]}),
            json.dumps({"items": [{**good, "targets": ["session", "window"]}]}),
            json.dumps({"items": [{**good, "targets": []}]}),
            json.dumps({"items": [{**good, "command": ""}]}),
            json.dumps({"items": [good, {**good, "id": " a"}]}),
            json.dumps({"items": [{**good, "command": 7}]}),
        ):
            path.write_text(text)
            with self.subTest(text=text), mock.patch.object(waiting, "MENU_PATH", path):
                self.assertIn("menu file", self.run_cli("menu", expect=2)["error"])
        missing = self.root / "missing.json"
        with mock.patch.object(waiting, "MENU_PATH", missing):
            self.run_cli("menu", expect=2)


class WhatTests(Case):
    def what(self, text: str, *argv: str, target: tuple[str, str] = ("--session", SID)) -> dict:
        return self.run_cli("what", *target, "--text", text, *argv)

    def test_what_creates_updates_and_clears(self) -> None:
        wait = self.what("Reply from Dóra")["wait"]
        self.assertEqual(
            (wait["kind"], wait["label"], wait["summary"], wait["by"], wait["state"]),
            ("something", "Reply from Dóra", "Reply from Dóra", "user", "waiting"),
        )
        self.assertEqual(self.session()["waiting_for"], "Reply from Dóra")
        changed = self.what("Reply from Béla")["wait"]
        self.assertEqual((changed["id"], changed["label"]), (wait["id"], "Reply from Béla"))
        seen = self.session()
        self.assertEqual((seen["waiting_for"], len(seen["waits"])), ("Reply from Béla", 1))
        self.assertEqual(seen["history"][-1]["text"], "Waiting for: Reply from Béla")
        answer = self.what("")
        self.assertIsNone(answer["wait"])
        self.assertEqual(answer["view"]["sessions"], [])
        self.assertEqual(self.state()["sessions"][SID]["waits"][0]["state"], "done")

    def test_clearing_leaves_other_waits(self) -> None:
        timer = self.add("--kind", "time", "--in", "1h")["id"]
        self.what("x")
        self.what("  ")
        seen = self.session()
        self.assertEqual([wait["id"] for wait in seen["waits"]], [timer])
        self.assertIsNone(seen["waiting_for"])

    def test_clearing_nothing_changes_nothing(self) -> None:
        answer = self.what("")
        self.assertIsNone(answer["wait"])
        self.assertEqual(answer["view"]["sessions"], [])

    def test_park_only_when_asked(self) -> None:
        self.agents(SID)
        self.what("x")
        self.assertEqual(self.state()["actions"], [])
        self.what("y", "--park")
        self.assertEqual([action["type"] for action in self.state()["actions"]], ["park"])
        self.assertEqual(self.state()["actions"][0]["pane"], 40)

    def test_add_something_twice_keeps_one(self) -> None:
        first = self.add("--kind", "something", "--what", "x")
        second = self.add("--kind", "something", "--what", "y", "--deadline-in", "1d")
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(second["deadline"], (self.clock + timedelta(days=1)).isoformat())
        seen = self.session()
        self.assertEqual([wait["label"] for wait in seen["waits"]], ["y"])
        self.assertEqual(seen["waiting_for"], "y")
        self.assertIn("Waiting for: y", [entry["text"] for entry in seen["history"]])

    def test_a_new_deadline_ends_overdue(self) -> None:
        wait = self.add("--kind", "something", "--what", "x", "--deadline-in", "1h")["id"]
        self.advance(hours=2)
        self.tick()
        self.assertEqual(self.wait_of(wait)["ball"], "on_me")
        self.add("--kind", "something", "--what", "x", "--deadline-in", "1d")
        self.assertEqual(self.wait_of(wait)["ball"], "on_someone")

    def test_waiting_for_on_a_pane(self) -> None:
        view = self.what("the build", target=("--pane", "51"))["view"]
        self.assertEqual(view["panes"][0]["waiting_for"], "the build")
        self.run_cli("add", "--pane", "52", "--kind", "job")
        panes = {pane["pane"]: pane for pane in self.run_cli("list")["panes"]}
        self.assertIsNone(panes[52]["waiting_for"])

    def test_a_deadline_alone_keeps_the_label(self) -> None:
        wait = self.add("--kind", "something", "--what", "x", "--label", "Dóra")["id"]
        self.add("--kind", "something", "--what", "x", "--deadline-in", "2d")
        seen = self.session()
        self.assertEqual(self.wait_of(wait)["label"], "Dóra")
        self.assertTrue(seen["history"][-1]["text"].startswith("Deadline moved to"))

    def test_the_last_writer_owns_the_text(self) -> None:
        self.what("x")
        wait = self.add("--kind", "something", "--what", "y", "--by", "agent")
        self.assertEqual((wait["label"], wait["by"]), ("y", "agent"))

    def test_a_chained_step_is_no_status_yet(self) -> None:
        timer = self.add("--kind", "time", "--in", "1h")["id"]
        step = self.add("--kind", "something", "--what", "later on", "--after", timer)
        self.assertEqual(step["state"], "pending")
        self.assertIsNone(self.session()["waiting_for"])
        now = self.what("now")["wait"]
        self.assertNotEqual(now["id"], step["id"])
        self.assertEqual(self.session()["waiting_for"], "now")
        self.assertEqual(self.wait_of(step["id"])["label"], "later on")
        self.advance(hours=1)
        self.tick()
        self.run_cli("mark-seen", "--session", SID, "--wait", timer)
        seen = self.session()
        self.assertEqual([wait["id"] for wait in seen["waits"]], [step["id"]])
        self.assertEqual(seen["waiting_for"], "later on")

    def test_a_step_after_an_ended_wait_replaces_the_status(self) -> None:
        timer = self.add("--kind", "time", "--in", "1s")["id"]
        self.advance(seconds=2)
        self.tick()
        self.what("now")
        step = self.add("--kind", "something", "--what", "next", "--after", timer)
        self.assertEqual(step["state"], "waiting")
        seen = self.session()
        self.assertEqual([wait["id"] for wait in seen["waits"] if wait["kind"] == "something"], [step["id"]])
        self.assertEqual(seen["waiting_for"], "next")

    def test_older_state_with_two_ends_with_one(self) -> None:
        self.what("x")
        state = self.state()
        first = state["sessions"][SID]["waits"][0]
        state["sessions"][SID]["waits"].append({
            **first, "id": "w99", "label": "y", "condition": {"type": "something", "what": "y"},
        })
        (self.state_dir / "waiting.json").write_text(json.dumps(state))
        self.what("z")
        seen = self.session()
        self.assertEqual(([wait["label"] for wait in seen["waits"]], seen["waiting_for"]), (["z"], "z"))
        (self.state_dir / "waiting.json").write_text(json.dumps(state))
        self.what("")
        self.assertEqual(self.run_cli("list")["sessions"], [])

    def test_needs_a_target_and_text(self) -> None:
        self.run_cli("what", "--text", "x", expect=2)
        self.run_cli("what", "--session", SID, expect=2)


class RepeatTests(Case):
    """A prompt sent every interval until it ends: by count, window, the agent, or a condition."""

    def setUp(self) -> None:
        super().setUp()
        self.agents(SID)
        self.heartbeat()

    def repeat(self, *argv: str, every: str = "1h") -> str:
        return self.add("--kind", "repeat", "--every", every, "--prompt", "check it", *argv)["id"]

    def at(self, **delta) -> dict:
        """Tick at T0 + delta, the extension still heard from."""
        self.clock = T0 + timedelta(**delta)
        self.heartbeat()
        return self.tick()

    def claim(self) -> list[dict]:
        return self.run_cli("claim-prompts", "--session", SID)["prompts"]

    def deliver(self) -> dict:
        prompt = self.claim()[0]
        self.run_cli("ack", "--action", prompt["action"], "--result", "delivered")
        return prompt

    def finish_turn(self) -> None:
        self.run_cli("event", "--type", "turn_finished", "--session", SID)

    def runs(self) -> list[str]:
        return [
            line["text"] for line in self.session()["history"]
            if line["text"].startswith(("Run ", "Skipped"))
        ]

    def test_snoozing_an_overdue_repeat_keeps_its_queued_run(self) -> None:
        wait = self.repeat("--deadline-in", "30m")
        self.at(hours=1)
        self.assertEqual(self.wait_of(wait)["outcome"], "overdue")
        self.run_cli("snooze", "--session", SID, "--wait", wait, "--for", "1d")
        self.assertEqual(self.wait_of(wait)["outcome"], None)
        self.assertEqual([prompt["action"] for prompt in self.claim()], [mock.ANY])
        self.assertEqual(self.state()["sessions"][SID]["waits"][0]["repeat"]["runs"], 1)

    def test_runs_at_the_interval_and_says_so(self) -> None:
        wait = self.repeat()
        seen = self.wait_of(wait)
        self.assertEqual((seen["label"], seen["ball"], seen["state"]), ("Every 1h", "on_timer", "waiting"))
        self.assertEqual(seen["repeat"], {
            "every": "1h", "next_at": (T0 + timedelta(hours=1)).isoformat(), "runs": 0,
            "times": None, "ends_at": None, "until": None,
        })
        self.assertEqual(seen["summary"], "↻ every 1h · next 13:00")
        self.assertTrue(seen["has_prompt"])
        session = self.session()
        self.assertEqual(session["status"], "↻ every 1h · next 13:00")
        self.assertEqual(session["line"], "↻ every 1h · next 13:00")
        self.assertEqual(session["composer_row"], session["line"])
        self.at(minutes=59)
        self.assertEqual(self.claim(), [])
        self.assertEqual(self.at(hours=1)["actions"], [])
        self.assertEqual(self.deliver()["text"], "check it")
        seen = self.session()
        self.assertFalse(seen["unseen"])
        self.assertEqual(seen["waits"][0]["state"], "waiting")
        self.finish_turn()
        self.assertEqual(self.wait_of(wait)["repeat"]["next_at"], (T0 + timedelta(hours=2)).isoformat())
        self.at(hours=2)
        self.assertEqual(self.deliver()["text"], "check it")
        self.assertEqual(self.wait_of(wait)["repeat"]["runs"], 2)
        self.assertEqual(self.runs(), ["Run 1: Every 1h", "Run 2: Every 1h"])

    def test_a_run_due_during_the_last_ones_turn_is_skipped(self) -> None:
        wait = self.repeat()
        self.at(hours=1)
        self.deliver()
        self.at(hours=2)
        self.at(hours=2, minutes=10)
        self.assertEqual(self.runs(), ["Run 1: Every 1h", "Skipped a run: the last one is still going"])
        self.assertEqual(self.wait_of(wait)["summary"], "↻ every 1h · next after this run")
        self.clock = T0 + timedelta(hours=2, minutes=30)
        self.finish_turn()
        self.assertEqual(
            self.wait_of(wait)["repeat"]["next_at"], (T0 + timedelta(hours=3, minutes=30)).isoformat(),
        )
        self.at(hours=3, minutes=29)
        self.assertEqual(self.claim(), [])
        self.at(hours=3, minutes=30)
        self.assertEqual(self.claim()[0]["text"], "check it")
        self.assertEqual(self.wait_of(wait)["repeat"]["runs"], 2)

    def test_ends_after_its_runs(self) -> None:
        wait = self.repeat("--times", "2")
        self.assertEqual(self.wait_of(wait)["summary"], "↻ every 1h · next 13:00 · 0/2")
        self.at(hours=1)
        self.deliver()
        self.finish_turn()
        self.at(hours=2)
        self.deliver()
        self.assertEqual(self.wait_of(wait)["summary"], "↻ every 1h · last run · 2/2")
        self.at(hours=3)
        self.assertEqual(self.wait_of(wait)["state"], "waiting")
        self.finish_turn()
        seen = self.wait_of(wait)
        self.assertEqual((seen["state"], seen["ball"], seen["outcome"]), ("fired", "on_me", "fulfilled"))
        self.assertEqual(seen["fired_text"], "Every 1h: ended after 2 runs")
        self.assertEqual(self.session()["history"][-1]["kind"], "fired")
        self.at(hours=4)
        self.assertEqual(self.claim(), [])

    def test_ends_with_its_window_and_drops_a_run_not_sent(self) -> None:
        wait = self.repeat("--for", "90m")
        self.assertEqual(self.wait_of(wait)["summary"], "↻ every 1h · next 13:00 · until 13:30")
        self.at(hours=1)
        self.at(minutes=90)
        self.assertEqual(self.wait_of(wait)["fired_text"], "Every 1h: ended after 0 runs")
        self.assertEqual(self.claim(), [])

    def test_the_agent_ends_it(self) -> None:
        wait = self.repeat("--until", "the deploy is green")
        self.assertEqual(self.wait_of(wait)["label"], "Every 1h until the deploy is green")
        self.run_cli("note", "--session", SID, "--text", "ship it")
        self.assertEqual(
            self.session()["line"], '↻ every 1h · next 13:00 · until the deploy is green · "ship it"',
        )
        self.at(hours=1)
        text = self.deliver()["text"]
        self.assertTrue(
            text.startswith("check it\n\n(This prompt repeats every 1h until: the deploy is green."), text,
        )
        self.assertIn(f'action "end_repeat", wait "{wait}"', text)
        answer = self.run_cli("end-repeat", "--session", SID, "--wait", wait, "--reason", "all green")
        self.assertEqual(
            answer["wait"]["fired_text"],
            "Every 1h until the deploy is green: the agent ended it: all green after 1 run",
        )
        self.finish_turn()
        self.assertEqual(self.wait_of(wait)["state"], "fired")
        self.run_cli("end-repeat", "--session", SID, "--wait", wait, expect=2)
        self.run_cli("snooze", "--session", SID, "--wait", wait, "--for", "1d")
        self.assertEqual(self.wait_of(wait)["state"], "snoozed")
        self.run_cli("end-repeat", "--session", SID, "--wait", wait, expect=2)

    def test_ending_during_a_run_keeps_its_turn_in_the_queue(self) -> None:
        wait = self.repeat("--until", "done")
        other = self.add("--kind", "time", "--in", "1h", "--prompt", "other")["id"]
        self.at(hours=1)
        self.deliver()
        self.run_cli("end-repeat", "--session", SID, "--wait", wait)
        self.assertEqual(self.claim(), [])
        self.finish_turn()
        self.assertEqual(self.wait_of(other)["state"], "acting")
        self.assertEqual(self.claim()[0]["text"], "other")

    def test_a_question_during_a_run_comes_to_me(self) -> None:
        wait = self.repeat()
        self.at(hours=1)
        self.run_cli("event", "--type", "waiting_input", "--session", SID)
        self.assertEqual(self.wait_of(wait)["reason"], None)
        self.deliver()
        self.run_cli("event", "--type", "waiting_input", "--session", SID)
        seen = self.wait_of(wait)
        self.assertEqual((seen["ball"], seen["reason"]), ("on_me", waiting.REASON_QUESTION))
        self.finish_turn()
        self.assertEqual(self.wait_of(wait)["ball"], "on_timer")

    def test_a_label_of_its_own_comes_first(self) -> None:
        self.repeat("--label", "Ping reviewers")
        session = self.session()
        self.assertEqual(session["status"], "↻ Ping reviewers · every 1h · next 13:00")
        self.assertEqual(session["line"], "↻ Ping reviewers · every 1h · next 13:00")

    def test_only_a_repeat_can_be_ended(self) -> None:
        wait = self.add("--kind", "time", "--in", "1h")["id"]
        self.run_cli("end-repeat", "--session", SID, "--wait", wait, expect=2)

    def test_another_kind_keeps_its_label_before_the_repeat(self) -> None:
        self.add(
            "--kind", "command", "--argv", "true", "--label", "Deploy green",
            "--every", "30m", "--prompt", "check it",
        )
        self.assertEqual(self.session()["line"], "⏳ Deploy green · ↻ every 30m · next 12:30")

    def test_a_command_that_holds_ends_the_repeat_and_its_queued_run(self) -> None:
        marker = self.root / "done"
        script = f"import os, sys; sys.exit(0 if os.path.exists({str(marker)!r}) else 1)"
        wait = self.add(
            "--kind", "command", "--argv", json.dumps([sys.executable, "-c", script]),
            "--interval", "90m", "--every", "1h", "--prompt", "check it",
        )["id"]
        self.at(hours=1)
        marker.touch()
        self.at(hours=2, minutes=30)
        seen = self.wait_of(wait)
        self.assertEqual((seen["state"], seen["fired_text"]), ("fired", "command exited 0"))
        self.assertTrue(self.session()["history"][-1]["text"].endswith(" · repeat ended after 0 runs"))
        self.assertEqual(self.claim(), [])

    def test_a_pr_that_fires_ends_the_repeat(self) -> None:
        merged: list[bool] = []
        self.github(poll=lambda cond, memo, **kwargs: poll_result(
            memo, fired=bool(merged), fire_text="PR#7 merged",
        ))
        wait = self.add(
            "--kind", "pr", "--repo", "o/r", "--number", "7", "--until", "merged",
            "--every", "1h", "--prompt", "nudge the reviewers",
        )["id"]
        self.assertEqual(self.wait_of(wait)["summary"], "PR#7 merged · ↻ every 1h · next 13:00")
        self.at(hours=1)
        self.assertEqual(self.claim()[0]["text"], "nudge the reviewers")
        merged.append(True)
        self.at(hours=1, minutes=4)
        seen = self.wait_of(wait)
        self.assertEqual((seen["state"], seen["fired_text"]), ("fired", "PR#7 merged"))
        self.assertEqual(self.session()["history"][-1]["text"], "PR#7 merged · repeat ended after 1 run")
        # The run the extension is sending keeps its place until its turn ends.
        self.assertEqual([entry["state"] for entry in self.state()["sessions"][SID]["prompt_queue"]], ["sending"])

    def test_one_more_day_moves_the_next_run_and_lets_a_running_one_finish(self) -> None:
        wait = self.repeat()
        self.at(hours=1)
        self.deliver()
        self.run_cli("snooze", "--session", SID, "--wait", wait, "--for", "1d")
        next_at = (T0 + timedelta(days=1, hours=1)).isoformat()
        seen = self.wait_of(wait)
        self.assertEqual((seen["state"], seen["repeat"]["next_at"]), ("waiting", next_at))
        self.finish_turn()
        self.assertEqual(self.wait_of(wait)["repeat"]["next_at"], next_at)
        self.at(hours=5)
        self.assertEqual(self.claim(), [])
        self.at(days=1, hours=1)
        self.assertEqual(self.claim()[0]["text"], "check it")

    def test_one_more_day_drops_a_run_not_sent_yet(self) -> None:
        wait = self.repeat()
        self.at(hours=1)
        self.run_cli("snooze", "--session", SID, "--wait", wait, "--for", "1d")
        self.assertEqual(self.claim(), [])
        repeat = self.state()["sessions"][SID]["waits"][0]["repeat"]
        self.assertEqual((repeat["running"], repeat["runs"]), (None, 0))

    def test_a_failed_run_stops_it_until_one_more_day(self) -> None:
        wait = self.repeat()
        self.at(hours=1)
        action = self.claim()[0]["action"]
        self.run_cli("ack", "--action", action, "--result", "failed", "--error", "no")
        self.assertEqual(self.wait_of(wait)["state"], "needs_attention")
        self.at(hours=3)
        self.assertEqual(self.claim(), [])
        self.run_cli("snooze", "--session", SID, "--wait", wait, "--for", "1h")
        self.assertEqual(self.wait_of(wait)["state"], "waiting")
        self.at(hours=4)
        self.assertEqual(self.claim()[0]["text"], "check it")

    def test_a_repeat_in_a_chain_counts_from_its_step(self) -> None:
        first = self.add("--kind", "something", "--what", "x")["id"]
        wait = self.repeat("--after", first)
        self.assertIsNone(self.wait_of(wait)["repeat"]["next_at"])
        self.at(hours=2)
        self.assertEqual(self.claim(), [])
        self.run_cli("what", "--session", SID, "--text", "")
        self.assertEqual(self.wait_of(wait)["repeat"]["next_at"], (T0 + timedelta(hours=3)).isoformat())

    def test_a_session_closed_during_a_run_sends_the_run_again_once(self) -> None:
        wait = self.repeat()
        self.at(hours=1)
        self.deliver()
        self.agents()
        self.clock = T0 + timedelta(hours=1, minutes=5)
        self.tick()
        record = self.state()["sessions"][SID]
        self.assertEqual([entry["text"] for entry in record["prompt_queue"]], [f"{waiting.CLOSE_NOTE}\n\ncheck it"])
        self.assertEqual(record["waits"][0]["repeat"]["runs"], 1)
        # Reopened, the run goes in again, and a second close would cut it twice.
        self.agents(SID)
        self.at(hours=1, minutes=6)
        self.deliver()
        self.assertEqual(self.session()["close"]["reason"], "turn")
        # Cut twice, that run is over and the repeat goes on.
        self.agents()
        self.clock = T0 + timedelta(hours=1, minutes=10)
        self.tick()
        self.assertEqual(self.wait_of(wait)["state"], "waiting")
        self.assertEqual(self.session()["history"][-1]["text"], "The session closed during a run")
        self.assertEqual(self.state()["sessions"][SID]["prompt_queue"], [])

    def test_the_last_run_cut_off_by_a_close_still_runs(self) -> None:
        wait = self.repeat("--times", "1")
        self.at(hours=1)
        self.deliver()
        self.agents()
        self.clock = T0 + timedelta(hours=1, minutes=5)
        self.tick()
        self.assertEqual(self.wait_of(wait)["state"], "waiting")
        self.agents(SID)
        self.at(hours=1, minutes=6)
        self.assertEqual(self.deliver()["text"], f"{waiting.CLOSE_NOTE}\n\ncheck it")
        self.finish_turn()
        seen = self.wait_of(wait)
        self.assertEqual((seen["state"], seen["outcome"]), ("fired", "fulfilled"))

    def test_the_next_run_sets_the_next_tick(self) -> None:
        self.repeat(every="2m")
        self.assertEqual(self.tick()["next_tick_s"], 120)

    def test_every_rules(self) -> None:
        for argv in (
            ["--kind", "repeat", "--prompt", "x"],
            ["--kind", "repeat", "--every", "30s", "--prompt", "x"],
            ["--kind", "repeat", "--every", "366d", "--prompt", "x"],
            ["--kind", "repeat", "--every", "1h"],
            ["--kind", "repeat", "--every", "1h", "--for", "30m", "--prompt", "x"],
            ["--kind", "repeat", "--every", "1h", "--for", "1h", "--prompt", "x"],
            ["--kind", "repeat", "--every", "1h", "--times", "0", "--prompt", "x"],
            ["--kind", "repeat", "--every", "1h", "--prompt", "x",
             "--verify", '{"type": "command", "argv": ["true"]}'],
            ["--kind", "time", "--in", "1h", "--every", "1h", "--prompt", "x"],
            ["--kind", "something", "--what", "x", "--every", "1h", "--prompt", "x"],
            ["--kind", "command", "--argv", "true", "--for", "2h"],
            ["--kind", "command", "--argv", "true", "--times", "2"],
            ["--kind", "time", "--in", "1h", "--until", "done"],
            ["--kind", "pr", "--repo", "o/r", "--number", "7", "--until", "soon"],
        ):
            with self.subTest(argv=argv):
                self.run_cli("add", "--session", SID, *argv, expect=2)
        self.run_cli(
            "add", "--pane", "4", "--kind", "command", "--argv", "true",
            "--every", "1h", "--prompt", "x", expect=2,
        )


class ResumeTests(Case):
    """A closed session whose wait sends a prompt comes back in Tern by itself."""

    boot = "one"

    def live(self, tern_session: str | None = "work") -> None:
        self.agents(SID, boot=self.boot)
        payload = json.loads((self.state_dir / "agents.json").read_text())
        if tern_session is None:
            del payload["agents"][0]["ternSession"]
        else:
            payload["agents"][0]["ternSession"] = tern_session
        (self.state_dir / "agents.json").write_text(json.dumps(payload))

    def closed(self, command: str | None = f"omp --resume {SID}") -> None:
        row = {"sessionId": SID, "cwd": "/w", "windowTitle": "x"}
        if command:
            row["resumeCommand"] = command
        self.agents(closed=[row], boot=self.boot)

    def resumes(self, window: bool = True) -> list[dict]:
        answer = self.tick("--window") if window else self.tick()
        return [action for action in answer["actions"] if action["type"] == "resume"]

    def resumed_and_delivered(self) -> None:
        """The resume brought the session up and its extension took the prompt."""
        self.run_cli("ack", "--action", self.resumes()[0]["id"], "--result", "done")
        self.live()
        self.heartbeat(SID)
        prompt = self.run_cli("claim-prompts", "--session", SID)["prompts"][0]
        self.run_cli("ack", "--action", prompt["action"], "--result", "delivered")

    def rebooted(self) -> None:
        self.boot = "two" if self.boot == "one" else "three"
        self.advance(minutes=1)
        self.closed()

    def test_a_window_going_away_restarts_the_countdown(self) -> None:
        wait = self.fired_prompt()
        self.assertEqual(len(self.resumes()), 1)
        self.advance(seconds=waiting.RESUME_GRACE_S - 10)
        self.assertEqual(len(self.resumes(window=False)), 1)
        self.advance(seconds=2 * waiting.RESUME_GRACE_S)
        self.assertEqual(len(self.resumes(window=False)), 1)
        self.assertEqual(len(self.resumes()), 1)  # a window again: counts from here
        self.advance(seconds=waiting.RESUME_GRACE_S - 1)
        self.assertEqual(len(self.resumes()), 1)
        self.assertEqual(self.wait_of(wait)["reason"], waiting.REASON_RESUMING)
        self.advance(seconds=1)
        self.assertEqual(self.resumes(), [])
        self.assertEqual(self.wait_of(wait)["reason"], waiting.REASON_CLOSED)

    def test_a_resume_waits_for_a_window_before_it_can_expire(self) -> None:
        wait = self.fired_prompt()
        self.assertEqual(len(self.resumes(window=False)), 1)
        self.advance(seconds=waiting.RESUME_GRACE_S - 1)
        self.assertEqual(len(self.resumes(window=False)), 1)
        self.assertEqual(self.wait_of(wait)["reason"], waiting.REASON_RESUMING)
        # Without a window it can't run: that comes to me, and the resume stays.
        self.advance(seconds=2 * waiting.RESUME_GRACE_S)
        actions = self.tick()["actions"]
        self.assertEqual([action["type"] for action in actions if action["type"] == "resume"], ["resume"])
        # Only a window could show a toast, and by then it says otherwise.
        self.assertFalse([action for action in actions if waiting.REASON_NO_WINDOW in action.get("text", "")])
        seen = self.wait_of(wait)
        self.assertEqual((seen["ball"], seen["reason"]), ("on_me", waiting.REASON_NO_WINDOW))
        self.assertEqual(len(self.resumes()), 1)  # a window is up from here
        seen = self.wait_of(wait)
        self.assertEqual((seen["ball"], seen["reason"]), ("on_machine", waiting.REASON_RESUMING))
        self.advance(seconds=waiting.RESUME_GRACE_S)
        self.assertEqual(self.resumes(), [])
        self.assertEqual(self.wait_of(wait)["reason"], waiting.REASON_CLOSED)

    def test_the_next_tick_comes_when_a_resume_expires(self) -> None:
        self.fired_prompt()
        self.assertEqual(len(self.resumes(window=False)), 1)
        # A window comes up long after the resume was queued: its outbox is old.
        self.advance(minutes=10)
        self.assertEqual(self.tick("--window")["next_tick_s"], waiting.RESUME_GRACE_S)

    def test_a_reboot_resumes_again_and_sends_the_cut_prompt_once_more(self) -> None:
        wait = self.fired_prompt()
        self.resumed_and_delivered()
        self.rebooted()
        resumes = self.resumes()
        self.assertEqual([action["session"] for action in resumes], [SID])
        seen = self.wait_of(wait)
        self.assertEqual((seen["state"], seen["reason"]), ("acting", waiting.REASON_RESUMING))
        self.assertIn("Sending again after a reboot: Retry", [h["text"] for h in self.session()["history"]])
        self.run_cli("ack", "--action", resumes[0]["id"], "--result", "done")
        self.live()
        self.heartbeat(SID)
        prompt = self.run_cli("claim-prompts", "--session", SID)["prompts"][0]
        self.assertEqual(prompt["text"], f"{waiting.REBOOT_NOTE}\n\ngo")
        self.assertFalse(prompt["resend_check"])
        # Cut off a second time, it comes to me instead.
        self.run_cli("ack", "--action", prompt["action"], "--result", "delivered")
        self.rebooted()
        self.tick("--window")
        self.assertEqual(self.wait_of(wait)["state"], "needs_attention")

    def closed_mid_turn(self) -> None:
        """The session closes while the prompt's turn runs, and its extension has gone quiet."""
        self.closed()
        self.advance(seconds=waiting.RESUME_QUIET_S)

    def back_with_the_prompt(self) -> dict:
        """Tern reopens the session and its extension takes the prompt."""
        resumes = self.resumes()
        self.assertEqual([action["session"] for action in resumes], [SID])
        self.run_cli("ack", "--action", resumes[0]["id"], "--result", "done")
        self.live()
        self.heartbeat(SID)
        prompt = self.run_cli("claim-prompts", "--session", SID)["prompts"][0]
        self.run_cli("ack", "--action", prompt["action"], "--result", "delivered")
        return prompt

    def test_a_close_mid_turn_resumes_and_sends_the_prompt_again_once(self) -> None:
        wait = self.fired_prompt()
        self.resumed_and_delivered()
        self.assertEqual(self.session()["close"]["verdict"], "safe")
        self.closed_mid_turn()
        prompt = self.back_with_the_prompt()
        self.assertEqual(prompt["text"], f"{waiting.CLOSE_NOTE}\n\ngo")
        self.assertIn("Sending again after the session closed: Retry", [h["text"] for h in self.session()["history"]])
        # A second close would cut it off again: that one comes to me.
        self.assertEqual(self.session()["close"], {
            "verdict": "park", "reason": "turn", "text": "park only: turn cut off once already",
        })
        self.closed_mid_turn()
        self.assertEqual(self.resumes(), [])
        seen = self.wait_of(wait)
        self.assertEqual((seen["state"], seen["reason"]), ("needs_attention", waiting.REASON_CUT_TWICE))

    def test_a_reboot_and_then_a_close_cut_the_turn_twice(self) -> None:
        wait = self.fired_prompt()
        self.resumed_and_delivered()
        self.rebooted()
        prompt = self.back_with_the_prompt()
        self.assertEqual(prompt["text"], f"{waiting.REBOOT_NOTE}\n\ngo")
        self.assertEqual(self.session()["close"]["reason"], "turn")
        self.closed_mid_turn()
        self.assertEqual(self.resumes(), [])
        self.assertEqual(self.wait_of(wait)["state"], "needs_attention")

    def test_a_close_and_then_a_reboot_cut_the_turn_twice(self) -> None:
        wait = self.fired_prompt()
        self.resumed_and_delivered()
        self.closed_mid_turn()
        self.back_with_the_prompt()
        self.rebooted()
        self.tick("--window")
        self.assertEqual(self.wait_of(wait)["state"], "needs_attention")

    def test_a_described_check_closed_before_its_command_is_reopened_to_write_it(self) -> None:
        self.closed()
        wait = self.add(
            "--kind", "command", "--describe", "the deploy is green", "--prompt", "tell me",
        )["id"]
        self.assertEqual(self.session()["close"]["verdict"], "safe")
        prompt = self.back_with_the_prompt()
        self.assertTrue(prompt["check"])
        self.assertEqual(prompt["text"], waiting._check_instruction("the deploy is green"))
        self.assertEqual(self.wait_of(wait)["state"], "pending")
        written = self.run_cli("write-check", "--session", SID, "--argv", '["true"]')["wait"]
        self.assertEqual((written["state"], written["condition"]["argv"]), ("waiting", ["true"]))

    def test_a_reboot_gives_a_session_that_never_came_up_a_fresh_resume(self) -> None:
        wait = self.fired_prompt()
        self.run_cli("ack", "--action", self.resumes()[0]["id"], "--result", "done")
        self.advance(seconds=waiting.RESUME_GRACE_S)
        self.assertEqual(self.resumes(), [])
        self.assertEqual(self.wait_of(wait)["reason"], waiting.REASON_CLOSED)
        self.rebooted()
        self.assertEqual(len(self.resumes()), 1)
        self.assertEqual(self.wait_of(wait)["reason"], waiting.REASON_RESUMING)

    def test_an_extension_heard_before_a_reboot_doesnt_hold_the_resume(self) -> None:
        self.fired_prompt()
        self.heartbeat(SID)
        self.assertEqual(self.resumes(), [])  # heard lately: it may still run
        self.rebooted()
        self.assertEqual(len(self.resumes()), 1)

    def fired_prompt(self) -> str:
        """A time wait whose prompt is due while its session, last seen in Tern
        session "work", is closed."""
        self.live()
        self.tick()
        wait = self.add("--kind", "time", "--in", "1s", "--prompt", "go", "--label", "Retry")["id"]
        self.closed()
        self.advance(seconds=2)
        return wait

    def test_a_closed_session_is_resumed_once_where_it_ran(self) -> None:
        wait = self.fired_prompt()
        resumes = self.resumes()
        self.assertEqual(resumes, [{
            "id": mock.ANY, "type": "resume", "session": SID, "command": f"omp --resume {SID}",
            "cwd": "/w", "tern_session": "work",
        }])
        self.assertEqual(self.resumes(), resumes)
        self.assertEqual(self.run_cli("list")["sessions"][0]["waits"][0]["id"], wait)
        seen = self.wait_of(wait)
        self.assertEqual(
            (seen["ball"], seen["outcome"], seen["reason"], seen["progress"]),
            ("on_machine", None, waiting.REASON_RESUMING, waiting.REASON_RESUMING),
        )
        self.assertEqual(self.session()["history"][-1]["text"], "Resuming the session to send: Retry")
        self.assertEqual(self.session()["prompts_queued"], 1)

    def test_after_it_opened_it_waits_then_comes_to_me(self) -> None:
        wait = self.fired_prompt()
        action = self.resumes()[0]["id"]
        self.run_cli("ack", "--action", action, "--result", "done")
        self.assertEqual(self.state()["sessions"][SID]["resume_opened_at"], self.clock.isoformat())
        self.advance(seconds=waiting.RESUME_GRACE_S - 1)
        self.assertEqual(self.resumes(), [])
        self.assertEqual(self.wait_of(wait)["reason"], waiting.REASON_RESUMING)
        self.advance(seconds=1)
        self.assertEqual(self.resumes(), [])
        seen = self.wait_of(wait)
        self.assertEqual((seen["ball"], seen["reason"]), ("on_me", waiting.REASON_CLOSED))
        self.assertIsNotNone(self.session()["resume_command"])
        self.advance(hours=1)
        self.assertEqual(self.resumes(), [])

    def test_the_next_prompt_gets_its_own_resume(self) -> None:
        self.fired_prompt()
        first = self.resumes()[0]["id"]
        self.run_cli("ack", "--action", first, "--result", "done")
        self.advance(minutes=5)
        self.assertEqual(self.resumes(), [])
        self.add("--kind", "time", "--in", "1s", "--prompt", "again")
        self.advance(seconds=2)
        second = self.resumes()
        self.assertEqual(len(second), 1)
        self.assertNotEqual(second[0]["id"], first)

    def test_a_manual_resume_waits_while_any_resume_is_queued(self) -> None:
        self.fired_prompt()
        self.assertEqual(len(self.resumes()), 1)
        # Past the grace, before a tick drops it: a window may still take it.
        self.advance(seconds=waiting.RESUME_GRACE_S + 60)
        self.run_cli("resume", "--session", SID, expect=2)
        self.assertEqual(self.resumes(), [])
        self.assertEqual(self.run_cli("resume", "--session", SID)["command"], f"omp --resume {SID}")

    def test_a_manual_resume_waits_for_the_automatic_one(self) -> None:
        self.fired_prompt()
        action = self.resumes()[0]["id"]
        refused = self.run_cli("resume", "--session", SID, expect=2)
        self.assertEqual(refused["error"], "that session is reopening already")
        self.run_cli("ack", "--action", action, "--result", "done")
        self.advance(seconds=waiting.RESUME_GRACE_S - 1)
        self.run_cli("resume", "--session", SID, expect=2)
        self.advance(seconds=1)
        self.assertEqual(self.run_cli("resume", "--session", SID)["command"], f"omp --resume {SID}")

    def test_a_failed_resume_comes_to_me_with_its_error(self) -> None:
        wait = self.fired_prompt()
        action = self.resumes()[0]["id"]
        self.run_cli("ack", "--action", action, "--result", "failed", "--error", "no Tern window")
        self.assertEqual(self.resumes(), [])
        seen = self.wait_of(wait)
        self.assertEqual((seen["ball"], seen["reason"]), ("on_me", "no Tern window"))

    def test_a_resume_no_window_takes_is_dropped(self) -> None:
        wait = self.fired_prompt()
        self.assertEqual(len(self.resumes()), 1)
        self.advance(seconds=waiting.RESUME_GRACE_S)
        self.assertEqual(self.resumes(), [])
        seen = self.wait_of(wait)
        self.assertEqual((seen["ball"], seen["reason"]), ("on_me", waiting.REASON_CLOSED))

    def test_a_session_back_by_itself_cancels_the_resume(self) -> None:
        wait = self.fired_prompt()
        self.assertEqual(len(self.resumes()), 1)
        self.live()
        self.heartbeat()
        self.assertEqual(self.resumes(), [])
        self.assertIsNone(self.wait_of(wait)["reason"])

    def test_no_resume_while_it_runs_or_without_a_command(self) -> None:
        self.live()
        wait = self.add("--kind", "time", "--in", "1s", "--prompt", "go")["id"]
        self.tick_through("--window", minutes=20)
        self.assertEqual(self.resumes(), [])
        self.assertEqual(self.wait_of(wait)["reason"], waiting.REASON_NO_EXTENSION)
        self.closed(command=None)
        self.assertEqual(self.resumes(), [])
        self.assertEqual(self.wait_of(wait)["reason"], waiting.REASON_CLOSED)

    def test_the_last_tern_session_is_kept_and_a_repeat_run_resumes_too(self) -> None:
        self.live()
        self.add("--kind", "repeat", "--every", "1h", "--prompt", "check it")
        self.live(tern_session=None)
        self.tick()
        self.closed()
        self.advance(hours=1)
        self.assertEqual([action["tern_session"] for action in self.resumes()], ["work"])

    def test_a_session_in_a_background_tab_resumes_in_its_tern_session(self) -> None:
        self.live(tern_session=None)
        payload = json.loads((self.state_dir / "agents.json").read_text())
        payload["agents"][0]["ternHome"] = "side work"
        (self.state_dir / "agents.json").write_text(json.dumps(payload))
        self.add("--kind", "repeat", "--every", "1h", "--prompt", "check it")
        self.closed()
        self.advance(hours=1)
        self.assertEqual([action["tern_session"] for action in self.resumes()], ["side work"])

    def test_an_extension_heard_lately_holds_the_resume(self) -> None:
        """agents.json may not list a running omp: a second one on its session
        file waits until its extension has been quiet for a heartbeat."""
        self.closed()
        self.heartbeat()
        wait = self.add("--kind", "time", "--in", "1s", "--prompt", "go")["id"]
        self.advance(seconds=2)
        self.assertEqual(self.resumes(), [])
        self.assertEqual(self.wait_of(wait)["reason"], waiting.REASON_RESUMING)
        self.advance(seconds=waiting.RESUME_QUIET_S)
        self.assertEqual(len(self.resumes()), 1)

    def test_removing_the_session_drops_its_resume(self) -> None:
        self.fired_prompt()
        self.assertEqual(len(self.resumes()), 1)
        self.run_cli("remove", "--session", SID)
        self.assertEqual(self.resumes(), [])

    def test_an_overdue_wait_keeps_saying_so_while_resuming(self) -> None:
        self.closed()
        wait = self.add(
            "--kind", "pr", "--repo", "o/r", "--number", "7", "--every", "1h",
            "--prompt", "nudge", "--deadline-in", "30m",
        )["id"]
        self.github()
        self.advance(hours=1)
        self.assertEqual(len(self.resumes()), 1)
        seen = self.wait_of(wait)
        self.assertEqual((seen["outcome"], seen["reason"], seen["progress"]), ("overdue", waiting.REASON_OVERDUE, None))

    def test_a_session_never_seen_in_tern_resumes_without_one(self) -> None:
        self.add("--kind", "time", "--in", "1s", "--prompt", "go")
        self.closed()
        self.advance(seconds=2)
        self.assertEqual([action["tern_session"] for action in self.resumes()], [None])


class CloseTests(Case):
    """Whether a session or pane can be closed now, and how it comes back (`close`)."""

    def setUp(self) -> None:
        super().setUp()
        self.agents(SID)
        self.heartbeat()

    def close(self) -> dict | None:
        return self.session()["close"]

    def test_a_prompt_reopens_it_by_itself(self) -> None:
        self.add("--kind", "time", "--in", "1h", "--prompt", "go")
        self.assertEqual(self.close(), {"verdict": "safe", "reason": "prompt", "text": "closable"})

    def test_a_repeat_reopens_it_for_each_run(self) -> None:
        self.add("--kind", "repeat", "--every", "1h", "--prompt", "p")
        self.assertEqual(self.close()["verdict"], "safe")

    def test_a_wait_without_a_prompt_comes_back_to_me(self) -> None:
        self.add("--kind", "time", "--in", "1h")
        self.assertEqual(
            self.close(), {"verdict": "manual", "reason": "manual", "text": "closable · you reopen"},
        )

    def test_later_and_a_pin_come_back_to_me(self) -> None:
        self.add("--kind", "later")
        self.assertEqual(self.close()["verdict"], "manual")
        self.run_cli("stop", "--session", SID, "--all")
        (self.omp / "session-pins.json").write_text(json.dumps([SID]))
        self.assertIsNone(self.close())  # pinned, nothing to wait for
        self.add("--kind", "time", "--in", "1h", "--prompt", "go")
        self.assertEqual(self.close()["verdict"], "manual")

    def test_a_wait_already_on_me_doesnt_hold_the_session(self) -> None:
        wait, _ = self.queued_prompt()
        # Its turn runs: a close sends it again.
        self.assertEqual(self.close()["verdict"], "safe")
        self.run_cli("event", "--type", "turn_finished", "--session", SID)
        self.assertEqual(self.wait_of(wait)["state"], "review")
        # "Ran, take a look" waits on the Waiting tab whether the session is open or not.
        self.assertEqual(self.close(), {"verdict": "safe", "reason": "seen", "text": "closable"})
        # Beside it, a wait still to come decides.
        self.add("--kind", "something", "--what", "Anna's answer")
        self.assertEqual(self.close()["verdict"], "manual")

    def test_the_worst_wait_wins(self) -> None:
        self.add("--kind", "time", "--in", "1h", "--prompt", "go")
        self.add("--kind", "something", "--what", "Anna's answer")
        self.assertEqual(self.close()["verdict"], "manual")
        self.add("--kind", "job")  # the job that runs in the session's own pane
        self.assertEqual(
            self.close(), {"verdict": "park", "reason": "job", "text": "park only: job runs in this pane"},
        )

    def test_a_job_in_another_pane_comes_back_by_its_prompt(self) -> None:
        self.add("--kind", "job", "--pane", "51", "--prompt", "look at the build")
        self.assertEqual(self.close()["verdict"], "safe")

    def test_without_a_transcript_or_resume_command_it_can_not_come_back(self) -> None:
        payload = json.loads((self.state_dir / "agents.json").read_text())
        del payload["agents"][0]["resumeCommand"]
        (self.state_dir / "agents.json").write_text(json.dumps(payload))
        self.add("--kind", "time", "--in", "1h", "--prompt", "go")
        self.assertEqual(self.close(), {
            "verdict": "park", "reason": "unresumable", "text": "park only: it can't be resumed",
        })
        folder = self.omp / "sessions" / "-w"
        folder.mkdir(parents=True)
        (folder / f"2026-11-05_{SID}.jsonl").write_text("")
        self.assertEqual(self.close()["verdict"], "safe")

    def test_a_panes_waits_end_with_it(self) -> None:
        self.run_cli("add", "--pane", "51", "--kind", "job")
        self.run_cli("add", "--pane", "52", "--kind", "time", "--in", "1h")
        closes = {view["pane"]: view["close"] for view in self.run_cli("list")["panes"]}
        self.assertEqual(closes, {
            51: {"verdict": "park", "reason": "job", "text": "park only: job runs in this pane"},
            52: {"verdict": "park", "reason": "shell", "text": "park only: its waits end with the pane"},
        })

    def test_the_card_says_it_too(self) -> None:
        self.add("--kind", "time", "--in", "1h")
        card = self.run_cli("card", "--session", SID, "--cached")["card"]
        self.assertEqual(card["close"]["verdict"], "manual")

    def test_every_text_is_short(self) -> None:
        for verdict, text in waiting.CLOSE_REASONS.values():
            self.assertIn(verdict, ("safe", "manual", "park"))
            self.assertLess(len(text), 40)

    def activity(self, *argv: str) -> None:
        """A heartbeat from an extension that reports each change of what runs."""
        self.heartbeat(SID, "--features", "menu,activity", *argv)

    def test_running_work_makes_it_park_only(self) -> None:
        self.add("--kind", "time", "--in", "1h", "--prompt", "go")
        self.activity("--idle", "--work")
        self.assertEqual(self.close(), {
            "verdict": "park", "reason": "work", "text": "park only: work runs in this session",
        })
        # Work goes on through a turn, and says more than it.
        self.activity("--work")
        self.assertEqual(self.close()["reason"], "work")
        self.activity("--idle")
        self.assertEqual(self.close(), {"verdict": "safe", "reason": "prompt", "text": "closable"})

    def test_a_turn_no_wait_sent_makes_it_park_only(self) -> None:
        self.add("--kind", "time", "--in", "1h")
        self.activity()
        self.assertEqual(self.close(), {
            "verdict": "park", "reason": "busy", "text": "park only: the agent is working",
        })

    def test_outside_tern_a_park_reads_keep_open(self) -> None:
        # Running in kitty, say: no Tern pane to park the session in.
        payload = json.loads((self.state_dir / "agents.json").read_text())
        del payload["agents"][0]["ternPane"]
        (self.state_dir / "agents.json").write_text(json.dumps(payload))
        self.add("--kind", "time", "--in", "1h", "--prompt", "go")
        self.activity("--idle", "--work")
        self.assertEqual(self.close(), {
            "verdict": "park", "reason": "work", "text": "keep open: work runs in this session",
        })
        self.activity("--idle")
        self.assertEqual(self.close()["text"], "closable")

    def test_idle_without_work_keeps_the_waits_verdict(self) -> None:
        self.add("--kind", "time", "--in", "1h")
        self.activity("--idle")
        self.assertEqual(self.close()["reason"], "manual")

    def test_a_delivered_prompts_turn_is_sent_again_so_it_stays_closable(self) -> None:
        self.queued_prompt()
        self.activity()
        self.assertEqual(self.close()["verdict"], "safe")
        self.activity("--work")
        self.assertEqual(self.close()["reason"], "work")

    def test_only_a_fresh_report_from_a_live_session_counts(self) -> None:
        self.add("--kind", "time", "--in", "1h")
        # An older extension heartbeats every 5 minutes: its word may be long over.
        self.heartbeat(SID, "--features", "menu", "--work")
        self.assertEqual(self.close()["reason"], "manual")
        self.activity("--work")
        self.assertEqual(self.close()["reason"], "work")
        self.advance(seconds=waiting.EXTENSION_FRESH_S + 1)
        self.assertEqual(self.close()["reason"], "manual")
        self.activity("--work")
        self.agents()  # closed: nothing of it runs
        self.assertNotIn(self.close()["reason"], ("work", "busy"))

    def test_a_moved_session_leaves_its_activity_behind(self) -> None:
        self.add("--kind", "later")
        self.activity("--work")
        self.heartbeat(OTHER, "--moved-from", SID, "--move-reason", "replaced")
        self.assertNotIn(SID, self.state()["extension_activity"])

    def test_park_alone_queues_a_park_for_the_sessions_pane(self) -> None:
        self.assertEqual(self.run_cli("park", "--session", SID)["parked"], 40)
        added = self.run_cli("add", "--session", SID, "--kind", "time", "--in", "1h", "--by", "agent", "--park")
        self.assertEqual(added["parked"], 40)
        parks = [action for action in self.state()["actions"] if action["type"] == "park"]
        self.assertEqual([(action["session"], action["pane"]) for action in parks], [(SID, 40), (SID, 40)])
        self.assertIsNone(self.run_cli("park", "--session", OTHER)["parked"])


class DescribedCheckTests(Case):
    """/wait check <words>: the session's agent writes the command, asked by a queued prompt."""

    DESCRIPTION = "the deploy is green"

    def setUp(self) -> None:
        super().setUp()
        self.agents(SID)
        self.heartbeat()

    def describe(self, *argv: str) -> str:
        return self.add("--kind", "command", "--describe", self.DESCRIPTION, "--interval", "10m", *argv)["id"]

    def asked(self) -> dict:
        """The agent got the instruction."""
        prompt = self.run_cli("claim-prompts", "--session", SID)["prompts"][0]
        self.run_cli("ack", "--action", prompt["action"], "--result", "delivered")
        return prompt

    def write(self, argv: list[str], expect: int = 0) -> dict:
        return self.run_cli("write-check", "--session", SID, "--argv", json.dumps(argv), expect=expect)

    def test_it_waits_unchecked_until_the_agent_writes_the_command(self) -> None:
        wait = self.describe("--prompt", "tell me")
        seen = self.wait_of(wait)
        self.assertEqual(
            (seen["state"], seen["label"], seen["summary"], seen["condition"]["argv"]),
            ("pending", self.DESCRIPTION, waiting.CHECK_UNWRITTEN, None),
        )
        self.assertEqual(self.session()["close"]["verdict"], "safe")
        prompt = self.asked()
        self.assertEqual(prompt["text"], waiting._check_instruction(self.DESCRIPTION))
        self.assertTrue(prompt["check"])
        self.assertIn(
            f"Asked the agent to write the check: {self.DESCRIPTION}",
            [entry["text"] for entry in self.session()["history"]],
        )
        # Nothing runs, and nothing fires, before the command is there.
        self.advance(hours=1)
        self.tick()
        self.assertEqual(self.wait_of(wait)["state"], "pending")
        written = self.write([sys.executable, "-c", "raise SystemExit(1)"])["wait"]
        self.assertEqual((written["id"], written["state"]), (wait, "waiting"))
        self.assertEqual(self.state()["sessions"][SID]["prompt_queue"], [])
        logged = self.session()["history"][-1]["text"]
        self.assertTrue(logged.startswith("Check: ") and logged.endswith(" every 10m"), logged)
        # The turn that wrote it ends; the check runs and doesn't hold.
        self.run_cli("event", "--type", "turn_finished", "--session", SID)
        self.tick()
        seen = self.wait_of(wait)
        self.assertEqual((seen["state"], seen["outcome"], seen["fired_at"]), ("waiting", None, None))
        self.assertEqual(self.run_cli("claim-prompts", "--session", SID)["prompts"], [])

    def test_a_turn_that_wrote_no_command_puts_it_on_me(self) -> None:
        wait = self.describe()
        self.asked()
        self.run_cli("event", "--type", "turn_finished", "--session", SID)
        seen = self.wait_of(wait)
        self.assertEqual((seen["state"], seen["reason"]), ("needs_attention", waiting.REASON_NO_CHECK))
        self.assertIsNone(self.state()["sessions"][SID]["waits"][0]["fired_at"])
        self.write(["true"], expect=2)

    def test_only_an_instruction_the_agent_has_takes_the_command(self) -> None:
        self.describe()
        self.write(["true"], expect=2)

    def test_one_described_check_at_a_time(self) -> None:
        self.describe()
        answer = self.run_cli(
            "add", "--session", SID, "--kind", "command", "--describe", "another", expect=2,
        )
        self.assertIn(self.DESCRIPTION, answer["error"])

    def test_stop_takes_the_instruction_back(self) -> None:
        wait = self.describe()
        self.run_cli("stop", "--session", SID, "--wait", wait)
        self.assertEqual(self.run_cli("claim-prompts", "--session", SID)["prompts"], [])

    def test_closing_before_the_command_was_written_asks_once_more(self) -> None:
        self.describe()
        self.asked()
        self.agents()
        self.tick()
        prompt = self.run_cli("claim-prompts", "--session", SID)["prompts"][0]
        self.assertEqual(prompt["text"], f"{waiting.CLOSE_NOTE}\n\n{waiting._check_instruction(self.DESCRIPTION)}")
        self.assertTrue(prompt["check"])

    def test_describe_takes_no_argv_and_needs_a_session(self) -> None:
        for argv in (
            ["add", "--session", SID, "--kind", "command", "--describe", "x", "--argv", '["true"]'],
            ["add", "--pane", "51", "--kind", "command", "--describe", "x"],
            ["add", "--session", SID, "--kind", "time", "--in", "1h", "--describe", "x"],
        ):
            with self.subTest(argv=argv):
                self.run_cli(*argv, expect=2)


class HealthTests(Case):
    def setUp(self) -> None:
        super().setUp()
        self.agents()

    def view_file(self) -> dict:
        return json.loads((self.state_dir / "waiting_view.json").read_text())

    def failing_tick(self) -> None:
        self.run_cli("tick", "--now", self.clock.isoformat(), expect=1)

    def test_a_tick_says_when_it_checked(self) -> None:
        self.assertEqual(self.tick()["view"]["health"], {
            "checked_at": self.clock.isoformat(), "error": None, "error_since": None,
            "error_kind": None, "agents_stale_since": None,
        })

    def test_a_failed_tick_leaves_its_error_in_the_view_and_only_a_tick_clears_it(self) -> None:
        self.add("--kind", "later")
        self.tick()
        checked = self.clock.isoformat()
        saved = (self.state_dir / "waiting.json").read_text()
        with mock.patch.object(waiting, "_evaluate", side_effect=RuntimeError("boom")):
            self.advance(minutes=5)
            self.failing_tick()
            first = self.clock.isoformat()
            self.advance(minutes=5)
            self.failing_tick()
        self.assertEqual((self.state_dir / "waiting.json").read_text(), saved)
        view = self.view_file()
        self.assertEqual(len(view["sessions"]), 1)
        self.assertEqual(view["health"], {
            "checked_at": checked, "error": "RuntimeError: boom", "error_since": first,
            "error_kind": "failed", "agents_stale_since": None,
        })
        # Any other command's view keeps saying so.
        self.assertEqual(self.run_cli("list")["health"]["error"], "RuntimeError: boom")
        self.add("--kind", "later")
        health = self.view_file()["health"]
        self.assertEqual((health["error_since"], health["error_kind"]), (first, "failed"))
        health = self.tick()["view"]["health"]
        self.assertEqual((health["error"], health["error_since"], health["error_kind"]), (None, None, None))

    def test_an_error_from_a_view_without_its_kind_counts_as_a_failed_tick(self) -> None:
        self.tick()
        view = self.view_file()
        view["health"].update(error="RuntimeError: boom", error_since=self.clock.isoformat())
        del view["health"]["error_kind"]
        (self.state_dir / "waiting_view.json").write_text(json.dumps(view))
        self.add("--kind", "later")
        self.assertEqual(self.view_file()["health"]["error_kind"], "failed")

    def test_a_mistyped_now_is_no_failed_tick(self) -> None:
        self.tick()
        self.run_cli("tick", "--now", "yesterday", expect=2)
        self.assertIsNone(self.view_file()["health"]["error"])

    def test_only_a_tick_sets_the_error(self) -> None:
        self.add("--kind", "later", session=OTHER)
        state = self.state()
        state["sessions"][OTHER]["prompt_queue"] = [{"bogus": 1}]
        (self.state_dir / "waiting.json").write_text(json.dumps(state))
        self.add("--kind", "later")
        self.assertIsNone(self.view_file()["health"]["error"])
        health = self.tick()["view"]["health"]
        self.assertTrue(health["error"].startswith(f"session {OTHER[:8]} can't be shown"))
        # The tick ran: only one row is missing.
        self.assertEqual((health["error_since"], health["error_kind"]), (self.clock.isoformat(), "partial"))
        self.assertEqual(health["checked_at"], self.clock.isoformat())
        since = self.clock.isoformat()
        self.advance(minutes=5)
        self.add("--kind", "later")
        health = self.view_file()["health"]
        self.assertEqual((health["error_since"], health["error_kind"]), (since, "partial"))

    def test_an_unreadable_session_scan_counts_as_stale(self) -> None:
        (self.state_dir / "agents.json").write_text("{torn")
        self.assertEqual(self.tick()["view"]["health"]["agents_stale_since"], self.clock.isoformat())

    def test_an_unreadable_state_without_a_view_writes_health_alone(self) -> None:
        (self.state_dir / "waiting.json").write_text("{broken")
        self.failing_tick()
        view = self.view_file()
        self.assertEqual(list(view), ["health"])
        self.assertIsNone(view["health"]["checked_at"])
        self.assertIn("unreadable", view["health"]["error"])
        self.assertEqual((self.state_dir / "waiting.json").read_text(), "{broken")


class IsolationTests(Case):
    def edit_state(self, change) -> None:
        state = self.state()
        change(state)
        (self.state_dir / "waiting.json").write_text(json.dumps(state))

    def test_a_bad_record_comes_to_me_once_and_the_others_go_on(self) -> None:
        bad = self.add("--kind", "later", session=OTHER)["id"]
        good = self.add("--kind", "time", "--in", "1m")["id"]
        self.edit_state(lambda state: state["sessions"][OTHER]["waits"][0].update(deadline="not a time"))
        self.advance(minutes=2)
        self.tick()
        self.assertEqual(self.wait_of(good)["state"], "fired")
        seen = self.wait_of(bad, OTHER)
        self.assertEqual(
            (seen["state"], seen["reason"]),
            ("needs_attention", "internal error: BadInput: bad time: 'not a time'"),
        )
        lines = len(self.session(OTHER)["history"])
        self.advance(minutes=5)
        self.tick()
        self.assertEqual(len(self.session(OTHER)["history"]), lines)

    def test_an_error_whose_text_changes_is_still_said_once(self) -> None:
        wait = self.add("--kind", "later")["id"]
        count = iter(range(100))

        def failing(state, record, now):
            raise RuntimeError(f"try {next(count)}")

        with mock.patch.object(waiting, "_evaluate_record", failing):
            self.tick()
            lines = len(self.session()["history"])
            self.advance(minutes=5)
            self.tick()
        self.assertEqual(len(self.session()["history"]), lines)
        self.assertEqual(self.wait_of(wait)["reason"], "internal error: RuntimeError: try 0")

    def test_retry_puts_a_quarantined_wait_back_and_a_new_failure_comes_back_once(self) -> None:
        argv = json.dumps([sys.executable, "-c", "raise SystemExit(1)"])
        wait = self.add("--kind", "command", "--argv", argv)["id"]
        failing = mock.patch.object(waiting, "_evaluate_record", side_effect=RuntimeError("boom"))
        with failing:
            self.tick()
        self.assertTrue(self.wait_of(wait)["retryable"])
        with failing:
            self.tick_through(hours=2)  # no hourly retry: only `retry` puts it back
        self.assertEqual(self.wait_of(wait)["state"], "needs_attention")
        self.run_cli("retry", "--session", SID, "--wait", wait)
        seen = self.wait_of(wait)
        self.assertEqual((seen["state"], seen["reason"], seen["retryable"]), ("waiting", None, False))
        with failing:
            self.tick()
            lines = len(self.session()["history"])
            self.tick_through(minutes=10)
        self.assertEqual(self.wait_of(wait)["reason"], "internal error: RuntimeError: boom")
        self.assertEqual(len(self.session()["history"]), lines)
        self.run_cli("retry", "--session", SID, "--wait", wait)
        self.tick()
        self.assertEqual(self.wait_of(wait)["state"], "waiting")

    def test_retry_sends_a_prompt_the_quarantine_cancelled_before_the_agent_got_it(self) -> None:
        self.agents(SID)
        self.heartbeat()
        wait = self.add("--kind", "time", "--in", "1s", "--prompt", "go")["id"]
        self.advance(seconds=2)
        self.tick()
        with mock.patch.object(waiting, "_evaluate_record", side_effect=RuntimeError("boom")):
            self.tick()
        self.assertEqual(self.session()["prompts_queued"], 0)
        self.run_cli("retry", "--session", SID, "--wait", wait)
        self.assertEqual(self.wait_of(wait)["state"], "acting")
        prompts = self.run_cli("claim-prompts", "--session", SID)["prompts"]
        self.assertEqual([prompt["text"] for prompt in prompts], ["go"])

    def test_a_record_that_keeps_failing_says_so_once(self) -> None:
        self.agents(OTHER)
        wait = self.add("--kind", "later", session=OTHER)["id"]
        self.edit_state(lambda state: state["sessions"][OTHER].update(prompt_queue=[{"bogus": 1}]))
        health = self.tick_through(minutes=10)["view"]["health"]
        # Too broken to show: the view's health says which row is missing.
        self.assertTrue(health["error"].startswith(f"session {OTHER[:8]} can't be shown: KeyError"))
        record = self.state()["sessions"][OTHER]
        self.assertEqual(
            (record["waits"][0]["state"], record["waits"][0]["reason"]),
            ("needs_attention", "internal error: KeyError: 'wait'"),
        )
        lines = len(record["history"])
        self.tick_through(minutes=10)
        self.assertEqual(len(self.state()["sessions"][OTHER]["history"]), lines)


class SettleTests(Case):
    def setUp(self) -> None:
        super().setUp()
        self.agents(SID)
        self.heartbeat()

    def test_an_idle_heartbeat_settles_a_prompt_delivered_a_while_ago(self) -> None:
        wait, _ = self.queued_prompt()
        self.assertEqual(self.state()["sessions"][SID]["prompt_queue"][0]["delivered_at"], self.clock.isoformat())
        self.advance(seconds=waiting.SETTLE_IDLE_S)
        self.heartbeat(SID, "--idle")
        self.assertEqual(self.wait_of(wait)["state"], "acting")
        self.advance(seconds=1)
        self.heartbeat(SID)  # busy: its turn may still run
        self.assertEqual(self.wait_of(wait)["state"], "acting")
        self.heartbeat(SID, "--idle")
        self.assertEqual(self.wait_of(wait)["state"], "review")


class WakeTests(Case):
    def setUp(self) -> None:
        super().setUp()
        self.agents(SID)
        self.heartbeat()

    def test_after_a_sleep_the_extension_gets_time_to_say_it_runs(self) -> None:
        wait = self.add("--kind", "time", "--in", "2h", "--prompt", "go")["id"]
        self.tick()
        self.advance(hours=3)  # asleep: no tick
        self.tick()
        self.tick_through(minutes=10)
        seen = self.wait_of(wait)
        self.assertEqual((seen["state"], seen["ball"], seen["reason"]), ("acting", "on_machine", None))
        self.tick_through(minutes=10)
        self.assertEqual(self.wait_of(wait)["reason"], waiting.REASON_NO_EXTENSION)

    def test_a_prompt_queued_just_now_waits_a_heartbeat(self) -> None:
        self.tick_through(minutes=20)
        wait = self.add("--kind", "time", "--in", "1s", "--prompt", "go")["id"]
        self.advance(seconds=2)
        self.tick()
        self.assertIsNone(self.wait_of(wait)["reason"])
        self.tick_through(minutes=7)
        self.assertEqual(self.wait_of(wait)["reason"], waiting.REASON_NO_EXTENSION)


class StaleAgentsTests(Case):
    def sweep(self, age_s: int, *live: str, closed: list[dict] | None = None, boot: str = "") -> None:
        self.agents(*live, closed=closed, boot=boot)
        payload = json.loads((self.state_dir / "agents.json").read_text())
        payload["updatedAt"] = int((self.clock.timestamp() - age_s) * 1000)
        (self.state_dir / "agents.json").write_text(json.dumps(payload))

    CLOSED = [{"sessionId": SID, "cwd": "/w", "windowTitle": "x", "resumeCommand": f"omp --resume {SID}"}]

    def resumes(self) -> list[dict]:
        return [action for action in self.tick("--window")["actions"] if action["type"] == "resume"]

    def test_nothing_is_flagged_or_resumed_on_a_stale_sweep(self) -> None:
        wait = self.add("--kind", "time", "--in", "1s", "--prompt", "go")["id"]
        self.sweep(waiting.AGENTS_STALE_S + 1, closed=self.CLOSED)
        self.advance(seconds=2)
        self.assertEqual(self.resumes(), [])
        seen = self.wait_of(wait)
        self.assertEqual((seen["ball"], seen["reason"]), ("on_machine", None))
        stale_since = self.clock.isoformat()
        self.advance(minutes=1)
        self.assertEqual(self.tick()["view"]["health"]["agents_stale_since"], stale_since)
        self.sweep(0, closed=self.CLOSED)
        self.assertEqual(len(self.resumes()), 1)
        self.assertIsNone(self.run_cli("list")["health"]["agents_stale_since"])

    def test_a_sweep_that_goes_stale_during_slow_checks_is_reported(self) -> None:
        self.sweep(0)
        self.add("--kind", "command", "--argv", json.dumps([sys.executable, "-c", "raise SystemExit(1)"]))
        real = waiting._run_jobs

        def slow(jobs, now):
            self.advance(seconds=waiting.AGENTS_STALE_S + 1)
            return real(jobs, now)

        with mock.patch.object(waiting, "_run_jobs", slow):
            health = self.run_cli("tick")["view"]["health"]
        self.assertEqual(health["agents_stale_since"], self.clock.isoformat())

    def test_a_sweep_from_before_the_reboot_runs_nothing(self) -> None:
        self.system_boot = "one"
        self.sweep(0, SID, boot="one")
        self.heartbeat()
        wait, _ = self.queued_prompt()
        self.system_boot = "two"
        self.advance(hours=9)
        answer = self.tick()
        self.assertFalse(answer["view"]["sessions"][0]["live"])
        self.assertEqual(answer["view"]["health"]["agents_stale_since"], self.clock.isoformat())
        actions = answer["actions"]
        self.assertFalse([action for action in actions if waiting.REASON_NO_EXTENSION in action.get("text", "")])
        self.assertNotEqual(self.wait_of(wait)["reason"], waiting.REASON_NO_EXTENSION)
        self.assertEqual(self.state()["sessions"][SID]["prompt_queue"][0]["state"], "queued")
        self.assertEqual(self.state()["boot_id"], "two")

    def test_resume_reopens_a_session_a_stale_sweep_lists_as_running(self) -> None:
        self.sweep(10 * 60, SID)
        self.assertEqual(self.run_cli("resume", "--session", SID)["command"], f"omp --resume {SID}")
        self.system_boot = "two"
        self.sweep(0, SID, boot="one")
        self.assertEqual(self.run_cli("resume", "--session", SID)["command"], f"omp --resume {SID}")

    def test_resume_trusts_no_stale_sweep_to_say_it_runs(self) -> None:
        self.sweep(10 * 60, closed=self.CLOSED)
        self.heartbeat()  # heard after the sweep: "running" by that sweep
        self.assertEqual(self.run_cli("resume", "--session", SID)["command"], f"omp --resume {SID}")
        self.sweep(0, closed=self.CLOSED)
        self.advance(seconds=1)
        self.heartbeat()
        self.run_cli("resume", "--session", SID, expect=2)


class TransientTests(Case):
    def test_an_outage_is_told_once_after_an_hour_and_a_success_clears_it(self) -> None:
        down = [True]
        self.github(poll=lambda cond, memo, **kwargs: poll_result(
            memo, error="error connecting to api.github.com", transient=True, next_poll_s=60,
        ) if down[0] else poll_result(memo))
        wait = self.add("--kind", "pr", "--repo", "o/r", "--number", "7")["id"]
        self.tick()
        self.tick_through(minutes=55)
        self.assertIsNone(self.wait_of(wait)["progress"])
        self.tick_through(minutes=5)
        seen = self.wait_of(wait)
        self.assertEqual((seen["state"], seen["progress"]), ("waiting", "GitHub unreachable since 12:00"))
        self.tick_through(minutes=30)
        told = [line for line in self.session()["history"] if "unreachable" in line["text"]]
        self.assertEqual(len(told), 1)
        down[0] = False
        self.tick_through(minutes=5)
        self.assertIsNone(self.wait_of(wait)["progress"])

    def test_an_outage_counts_from_when_checks_resume_after_a_snooze(self) -> None:
        self.github(poll=lambda cond, memo, **kwargs: poll_result(
            memo, error="error connecting to api.github.com", transient=True, next_poll_s=60,
        ))
        wait = self.add("--kind", "pr", "--repo", "o/r", "--number", "7")["id"]
        self.tick()
        self.tick_through(minutes=30)
        self.run_cli("snooze", "--session", SID, "--wait", wait, "--for", "1d")
        self.advance(days=1)
        self.tick()
        self.tick_through(minutes=30)
        self.assertIsNone(self.wait_of(wait)["progress"])

    def test_a_pr_verify_of_a_command_wait_says_github_is_unreachable(self) -> None:
        self.github(poll=lambda cond, memo, **kwargs: poll_result(
            memo, error="error connecting to api.github.com", transient=True,
        ))
        self.agents(SID)
        self.heartbeat()
        verify = json.dumps({"type": "pr", "repo": "o/r", "number": 7, "until": "merged"})
        wait = self.add(
            "--kind", "command", "--argv", json.dumps([sys.executable, "-c", "pass"]),
            "--prompt", "merge it", "--verify", verify,
        )["id"]
        self.tick()
        action = self.run_cli("claim-prompts", "--session", SID)["prompts"][0]["action"]
        self.run_cli("ack", "--action", action, "--result", "delivered")
        self.run_cli("event", "--type", "turn_finished", "--session", SID)
        self.tick()
        since = self.clock
        for _ in range(13):
            self.advance(minutes=5)
            self.heartbeat()
            self.tick()
        seen = self.wait_of(wait)
        self.assertEqual(
            (seen["state"], seen["progress"]), ("acting", f"GitHub unreachable since {since:%H:%M}"),
        )

    def test_a_release_check_passes_over_network_failures(self) -> None:
        self.github()
        script = "import sys; sys.stderr.write(sys.argv[1] + '\\n'); sys.exit(1)"
        condition = {"cwd": str(self.root), "predicate": "changed", "repo": "o/r"}
        offline = waiting._run_command({**condition, "argv": [sys.executable, "-c", script, "error connecting to api.github.com"]})
        refused = waiting._run_command({**condition, "argv": [sys.executable, "-c", script, "Bad credentials (HTTP 401)"]})
        self.assertEqual((offline["transient"], refused["transient"]), (True, False))

    def test_a_failing_release_check_says_since_when_instead_of_failing(self) -> None:
        self.github()
        script = "import sys; sys.stderr.write('error connecting to api.github.com\\n'); sys.exit(1)"
        wait = self.add(
            "--kind", "command", "--argv", json.dumps([sys.executable, "-c", script]),
            "--predicate", "changed", "--cwd", str(self.root),
        )["id"]
        state = self.state()
        state["sessions"][SID]["waits"][0]["condition"]["repo"] = "o/r"
        (self.state_dir / "waiting.json").write_text(json.dumps(state))
        self.tick_through(minutes=95)
        seen = self.wait_of(wait)
        self.assertEqual(
            (seen["state"], seen["progress"]),
            ("waiting", f"{Path(sys.executable).name} failing since 12:05"),
        )

    def test_a_verify_command_that_cant_run_can_be_retried(self) -> None:
        self.agents(SID)
        self.heartbeat()
        verify = json.dumps({"type": "command", "argv": [str(self.root / "missing")]})
        wait, _ = self.queued_prompt("--verify", verify)
        self.run_cli("event", "--type", "turn_finished", "--session", SID)
        self.tick()
        seen = self.wait_of(wait)
        self.assertEqual((seen["state"], seen["retryable"]), ("needs_attention", True))

    def test_a_bug_in_a_fix_check_is_no_outage(self) -> None:
        reads = iter(["bug", "ok"])

        def verify_fix(repo, number, before):
            if next(reads) == "bug":
                raise KeyError("head")
            return True, "new head"

        first = PrTests.fix_chain(self, verify_fix)
        action = self.run_cli("claim-prompts", "--session", SID)["prompts"][0]["action"]
        self.run_cli("ack", "--action", action, "--result", "delivered")
        self.run_cli("event", "--type", "turn_finished", "--session", SID)
        self.tick()
        seen = self.wait_of(first)
        self.assertEqual((seen["state"], seen["retryable"]), ("needs_attention", True))
        self.run_cli("retry", "--session", SID, "--wait", first)
        self.assertEqual(self.wait_of(first)["state"], "acting")
        self.tick()
        self.assertEqual(self.session()["waits"][0]["label"], "Ready")


class RetryTests(Case):
    def setUp(self) -> None:
        super().setUp()
        self.down = True
        self.polls = 0

        self.events = []

        def poll(cond, memo, **kwargs):
            self.polls += 1
            if self.down:
                return poll_result(memo, error="HTTP 401: Bad credentials", events=self.events)
            return poll_result(memo)

        self.github(poll=poll)
        self.wait = self.add("--kind", "pr", "--repo", "o/r", "--number", "7", "--label", "PR")["id"]
        for _ in range(waiting.MAX_ERRORS):
            self.tick()
            self.advance(minutes=4)
        self.assertEqual(self.wait_of(self.wait)["state"], "needs_attention")

    def test_a_failed_hourly_try_keeps_the_events_it_saw(self) -> None:
        self.events = [
            {"key": "c:1", "text": "comment from a reviewer", "on_me": False},
            {"key": "r:1", "text": "changes requested", "on_me": True},
        ]
        self.tick_through(minutes=65)
        lines = [(line["text"], line["kind"]) for line in self.session()["history"]]
        self.assertIn(("comment from a reviewer", None), lines)
        self.assertIn(("changes requested", "attention"), lines)
        self.assertEqual(self.wait_of(self.wait)["state"], "needs_attention")

    def test_retry_puts_it_back_to_its_checks(self) -> None:
        self.assertTrue(self.wait_of(self.wait)["retryable"])
        other = self.add("--kind", "later")["id"]
        self.run_cli("retry", "--session", SID, "--wait", other, expect=2)
        self.down = False
        self.run_cli("retry", "--session", SID, "--wait", self.wait)
        seen = self.wait_of(self.wait)
        self.assertEqual((seen["state"], seen["ball"], seen["retryable"]), ("waiting", "on_machine", False))
        self.assertEqual(self.session()["history"][-1]["text"], "Retry: PR")
        polls = self.polls
        self.tick()
        self.assertEqual((self.polls, self.wait_of(self.wait)["state"]), (polls + 1, "waiting"))

    def test_it_is_tried_again_hourly_and_comes_back_when_that_works(self) -> None:
        polls, lines = self.polls, len(self.session()["history"])
        self.tick_through(minutes=50)
        self.assertEqual(self.polls, polls)
        self.tick_through(minutes=10)
        self.assertEqual(self.polls, polls + 1)
        self.assertEqual(len(self.session()["history"]), lines)  # still down: nothing to say
        self.assertEqual(self.wait_of(self.wait)["state"], "needs_attention")
        self.down = False
        self.tick_through(minutes=60)
        seen = self.wait_of(self.wait)
        self.assertEqual((seen["state"], seen["ball"]), ("waiting", "on_machine"))
        self.assertEqual(self.session()["history"][-1]["text"], "Checks work again: PR")


class AncestorDaemonTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.proc = Path(temporary.name)

    def process(self, pid: int, parent: int, name: str, *argv: str) -> None:
        folder = self.proc / str(pid)
        folder.mkdir()
        # Field 22, the start time, is the 20th after the name.
        fields = ["S", str(parent), *["0"] * 17, f"{pid}00"]
        (folder / "stat").write_text(f"{pid} ({name}) {' '.join(fields)} 0 0\n")
        (folder / "cmdline").write_bytes(b"\0".join(part.encode() for part in argv) + b"\0")

    def test_only_the_tern_daemon_counts_not_the_window_app(self) -> None:
        self.process(80, 1, "tern", "/opt/tern/tern", "daemon", "--socket", "/run/t.sock")
        self.process(90, 80, "tern", "/opt/tern/tern")  # the window app
        self.process(100, 90, "python3", "python3", "codexbar_waiting.py")
        self.assertEqual(waiting._daemon_above(100, self.proc), "80-8000")

    def test_a_window_outside_the_daemon_names_none(self) -> None:
        self.process(90, 1, "tern", "/opt/tern/tern")
        self.process(95, 90, "tern", "/opt/tern/tern", "tray")
        self.process(100, 95, "python3", "python3")
        self.assertIsNone(waiting._daemon_above(100, self.proc))


class DaemonTests(Case):
    def test_another_daemon_stands_aside_while_one_ticks(self) -> None:
        self.add("--kind", "later")
        self.tick("--daemon", "a")
        saved = (self.state_dir / "waiting.json").read_text()
        self.advance(minutes=5)
        answer = self.tick("--daemon", "b")
        self.assertEqual((answer["skipped"], answer["actions"]), (waiting.TICK_SKIPPED, []))
        self.assertEqual(len(answer["view"]["sessions"]), 1)
        self.assertEqual((self.state_dir / "waiting.json").read_text(), saved)
        self.advance(seconds=waiting.TICKER_LEASE_S - 5 * 60)
        self.assertNotIn("skipped", self.tick("--daemon", "b"))
        self.assertEqual(self.state()["ticker"]["daemon"], "b")

    def own_daemon(self) -> str:
        """This process, named as a running Tern daemon."""
        if WINDOWS:
            import proc_windows
            created = proc_windows.start_ms(os.getpid())
            stamp = datetime.fromtimestamp(created // 1000, timezone.utc).strftime("%Y%m%dT%H%M%S")
            return f"{os.getpid()}-{stamp}.{created % 1000 * 10_000:07d}"
        stat = Path("/proc/self/stat").read_text()
        return f"{os.getpid()}-{stat[stat.rfind(')') + 2:].split()[19]}"

    @unittest.skipUnless(NAMES_DAEMONS, "names a running daemon by /proc or Windows' process times")
    def test_a_running_daemon_keeps_the_ticker_however_long_it_slept(self) -> None:
        self.tick("--daemon", self.own_daemon())
        self.advance(hours=2)
        self.assertEqual(self.tick("--daemon", "b")["skipped"], waiting.TICK_SKIPPED)

    def test_a_windows_daemon_id_is_its_pid_and_utc_creation_time(self) -> None:
        self.assertAlmostEqual(waiting._started_at("4880-20261114T073633.9672515"),
                               datetime(2026, 11, 14, 7, 36, 33, tzinfo=timezone.utc).timestamp() + 0.9672515)
        for start in ("20261114T073633.967251", "20261314T073633.9672515", "1234"):
            self.assertIsNone(waiting._windows_start_ticks(start), start)
        created = {4880: 1794641793967, 4881: 0, 4882: 0}
        fake = types.SimpleNamespace(start_ms=created.get, exited=lambda pid: pid == 4881)
        with (
            mock.patch.object(waiting.state_paths, "WINDOWS", True),
            mock.patch.dict(sys.modules, {"proc_windows": fake}),
        ):
            alive = {daemon: waiting._daemon_alive(f"{daemon}-20261114T073633.9672515")
                     for daemon in created}
            created[4880] += 1  # the pid now names a younger process
            self.assertFalse(waiting._daemon_alive("4880-20261114T073633.9672515"))
            self.assertIsNone(waiting._daemon_alive("4880-12345"))
        # Running; gone; another user's that can't be read.
        self.assertEqual(alive, {4880: True, 4881: False, 4882: None})

    @unittest.skipUnless(NAMES_DAEMONS, "names a running daemon by /proc or Windows' process times")
    def test_a_running_daemon_that_stopped_ticking_loses_the_ticker(self) -> None:
        awake = [1000.0]
        with mock.patch.object(waiting, "_awake_s", lambda: awake[0]):
            self.tick("--daemon", self.own_daemon())
            awake[0] += waiting.TICKER_LEASE_S - 1
            self.assertEqual(self.tick("--daemon", "b")["skipped"], waiting.TICK_SKIPPED)
            awake[0] += 1
            self.assertNotIn("skipped", self.tick("--daemon", "b"))
        self.assertEqual(self.state()["ticker"]["daemon"], "b")

    def test_a_tick_dated_ahead_by_a_clock_set_back_is_old(self) -> None:
        self.tick("--daemon", "a")
        self.advance(seconds=-50)
        self.assertEqual(self.tick("--daemon", "b")["skipped"], waiting.TICK_SKIPPED)
        self.advance(seconds=-20)
        self.assertNotIn("skipped", self.tick("--daemon", "b"))

    def test_the_lease_runs_from_the_end_of_the_tick(self) -> None:
        self.add("--kind", "command", "--argv", json.dumps([sys.executable, "-c", "raise SystemExit(1)"]))
        real = waiting._run_jobs

        def slow(jobs, now):
            self.advance(minutes=4)
            return real(jobs, now)

        with mock.patch.object(waiting, "_run_jobs", slow):
            self.run_cli("tick", "--daemon", "a")
        self.assertEqual(self.state()["ticker"]["at"], self.clock.isoformat())

    def test_another_daemons_pane_actions_stay_out_of_the_outbox(self) -> None:
        self.tick("--daemon", "a")
        self.creator = "b"
        self.run_cli("add", "--pane", "52", "--kind", "later", "--park")
        self.creator = None
        self.run_cli("add", "--pane", "53", "--kind", "later", "--park")
        actions = self.tick("--daemon", "a")["actions"]
        self.assertEqual([action["pane"] for action in actions if action["type"] == "park"], [53])
        self.assertEqual([action["pane"] for action in self.state()["actions"]], [52, 53])

    @unittest.skipUnless(NAMES_DAEMONS, "tells a gone daemon by /proc or Windows' process times")
    def test_a_daemon_that_is_gone_holds_no_lease(self) -> None:
        self.tick("--daemon", GONE_DAEMON)  # no such process
        self.advance(minutes=1)
        self.assertNotIn("skipped", self.tick("--daemon", "b"))

    def test_a_pane_event_with_daemon_reaches_only_that_daemons_items(self) -> None:
        self.tick("--daemon", "a")
        self.creator = "b"
        theirs = self.add("--kind", "job", "--pane", "51")["id"]
        self.run_cli("add", "--pane", "51", "--kind", "later")
        self.creator = None
        legacy = self.add("--kind", "job", "--pane", "51")["id"]  # stamped a, the ticker, until:
        state = self.state()
        unstamped = next(wait for wait in state["sessions"][SID]["waits"] if wait["id"] == legacy)
        del unstamped["daemon"]
        (self.state_dir / "waiting.json").write_text(json.dumps(state))
        self.assertEqual(self.wait_of(theirs)["daemon"], "b")
        self.assertEqual(self.run_cli("list")["panes"][0]["daemon"], "b")
        # Daemon c doesn't tick: its pane 51 is neither b's nor the unstamped one's.
        self.run_cli("event", "--type", "pane_closed", "--pane", "51", "--daemon", "c")
        self.assertEqual([self.wait_of(theirs)["state"], self.wait_of(legacy)["state"]], ["waiting", "waiting"])
        self.assertEqual(len(self.run_cli("list")["panes"]), 1)
        # The ticking daemon a: the unstamped wait is its own, b's aren't.
        self.run_cli("event", "--type", "job_finished", "--pane", "51", "--status", "1", "--daemon", "a")
        self.assertEqual([self.wait_of(theirs)["state"], self.wait_of(legacy)["state"]], ["waiting", "needs_attention"])
        self.run_cli("event", "--type", "pane_closed", "--pane", "51", "--daemon", "b")
        self.assertEqual(self.wait_of(theirs)["state"], "needs_attention")
        self.assertEqual(self.run_cli("list")["panes"], [])

    def test_a_pane_event_reaches_only_its_daemons_waits(self) -> None:
        self.creator = "b"
        job = self.add("--kind", "job", "--pane", "51")["id"]
        self.creator = "a"  # daemon a's pane 51 is another pane
        self.run_cli("event", "--type", "job_finished", "--pane", "51", "--status", "1")
        self.run_cli("event", "--type", "pane_closed", "--pane", "51")
        self.assertEqual(self.wait_of(job)["state"], "waiting")
        self.creator = "b"
        self.run_cli("event", "--type", "pane_closed", "--pane", "51")
        self.assertEqual(self.wait_of(job)["state"], "needs_attention")

    def test_a_new_daemon_ends_what_watched_the_old_ones_panes(self) -> None:
        self.tick("--daemon", "a")
        job = self.add("--kind", "job", "--pane", "51")["id"]
        self.run_cli("add", "--pane", "52", "--kind", "later", "--park")
        self.assertEqual(self.state()["sessions"][SID]["waits"][0]["daemon"], "a")
        self.advance(seconds=waiting.TICKER_LEASE_S)
        actions = self.tick("--daemon", "b")["actions"]
        self.assertEqual([action["type"] for action in actions if action["type"] != "toast"], [])
        seen = self.wait_of(job)
        self.assertEqual((seen["state"], seen["reason"]), ("needs_attention", "Pane closed before the job finished"))
        self.assertEqual(self.run_cli("list")["panes"], [])
        later = self.add("--kind", "job", "--pane", "51")["id"]
        self.advance(minutes=1)
        self.tick("--daemon", "b")
        self.assertEqual(self.wait_of(later)["state"], "waiting")

    def test_a_pane_of_the_new_daemon_made_before_it_ticked_stays(self) -> None:
        self.tick("--daemon", "a")
        self.creator = "b"  # added from a pane of daemon b, which hasn't ticked yet
        job = self.add("--kind", "job", "--pane", "51")["id"]
        self.run_cli("add", "--pane", "52", "--kind", "later")
        self.advance(seconds=waiting.TICKER_LEASE_S)
        self.tick("--daemon", "b")
        self.assertEqual(self.wait_of(job)["state"], "waiting")
        self.assertEqual([pane["pane"] for pane in self.run_cli("list")["panes"]], [52])

    def test_a_window_names_its_daemon_for_what_it_adds(self) -> None:
        # The window app runs commands outside the daemon's process tree.
        self.creator = None
        self.tick("--daemon", "a")
        self.run_cli("add", "--pane", "52", "--kind", "later", "--park", "--daemon", "b")
        job = self.add("--kind", "job", "--pane", "51", "--daemon", "b")["id"]
        self.assertEqual(self.run_cli("list")["panes"][0]["daemon"], "b")
        self.assertEqual(self.wait_of(job)["daemon"], "b")
        self.assertEqual([action.get("daemon") for action in self.state()["actions"]], ["b"])
        # Daemon a's windows don't park daemon b's pane 52.
        self.assertEqual(self.tick("--daemon", "a")["actions"], [])


class SessionMoveTests(Case):
    """omp moves a session it can't write to a new file under a new id; the
    extension's heartbeat says from which and why."""

    def setUp(self) -> None:
        super().setUp()
        self.agents(SID, OTHER)
        self.heartbeat()

    def moved(self, reason: str, sid: str = OTHER, old: str = SID) -> None:
        self.heartbeat(sid, "--moved-from", old, "--move-reason", reason)

    def test_a_replaced_session_brings_its_waits_prompts_and_history(self) -> None:
        wait = self.add("--kind", "time", "--in", "1s", "--prompt", "go", "--note", "why")["id"]
        self.advance(seconds=2)
        self.tick()
        self.moved("replaced")
        view = self.run_cli("list")
        self.assertEqual([row["session_id"] for row in view["sessions"]], [OTHER])
        row = self.session(OTHER)
        self.assertEqual(([w["id"] for w in row["waits"]], row["note"]), ([wait], "why"))
        self.assertEqual(row["history"][-1]["text"], f"Moved from session {SID[:8]}")
        self.assertEqual(self.run_cli("claim-prompts", "--session", SID)["prompts"], [])
        self.assertEqual(self.run_cli("claim-prompts", "--session", OTHER)["prompts"][0]["wait"], wait)
        # The extension says it again on its next heartbeat: nothing more moves.
        saved = self.state()["sessions"]
        self.moved("contested")
        self.assertEqual(self.state()["sessions"], saved)

    def test_a_session_open_elsewhere_keeps_its_waits(self) -> None:
        wait = self.add("--kind", "later")["id"]
        self.moved("open-elsewhere")
        self.assertEqual([w["id"] for w in self.session(SID)["waits"]], [wait])
        self.assertNotIn(OTHER, self.state()["sessions"])

    def test_a_new_id_with_waits_of_its_own_keeps_both_and_says_so_once(self) -> None:
        mine = self.add("--kind", "later")["id"]
        theirs = self.add("--kind", "something", "--what", "x", session=OTHER)["id"]
        self.moved("replaced")
        self.moved("replaced")
        self.assertEqual([w["id"] for w in self.session(SID)["waits"]], [mine])
        self.assertEqual([w["id"] for w in self.session(OTHER)["waits"]], [theirs])
        moved = [line for line in self.session(OTHER)["history"] if SID[:8] in line["text"]]
        self.assertEqual(len(moved), 1)

    def test_the_pin_moves_with_the_session(self) -> None:
        self.add("--kind", "later")
        pins = self.omp / "session-pins.json"
        self.assertEqual(json.loads(pins.read_text()), [SID])
        self.moved("replaced")
        self.assertEqual(json.loads(pins.read_text()), [OTHER])
        self.assertEqual([row["session_id"] for row in self.run_cli("list")["sessions"]], [OTHER])

    def test_a_pin_the_new_id_could_not_take_stays_on_the_old_one(self) -> None:
        self.add("--kind", "something", "--what", "x")
        pins = self.omp / "session-pins.json"
        pins.write_text(json.dumps([SID]))  # the user's own pin: no wait asks for it
        real = waiting._write_pin

        def locked(path: Path, session: str, pinned: bool) -> None:
            if pinned:
                raise waiting.PinError("session-pins.json is locked by another process")
            real(path, session, pinned)

        with mock.patch.object(waiting, "_write_pin", locked):
            self.moved("replaced")
        self.assertEqual(json.loads(pins.read_text()), [SID])
        self.assertEqual({row["session_id"] for row in self.run_cli("list")["sessions"]}, {SID, OTHER})

    def test_a_move_needs_both_flags(self) -> None:
        self.run_cli("heartbeat", "--session", OTHER, "--moved-from", SID, expect=2)
        self.run_cli("heartbeat", "--session", OTHER, "--move-reason", "replaced", expect=2)
        self.run_cli("heartbeat", "--session", SID, "--moved-from", SID, "--move-reason", "replaced", expect=2)


class ClaimPaneTests(Case):
    """Two processes run one session id: the one that holds it exactly takes its prompts."""

    def setUp(self) -> None:
        super().setUp()
        self.agents(SID)
        payload = json.loads((self.state_dir / "agents.json").read_text())
        row = payload["agents"][0]
        payload["agents"] = [
            {**row, "ternPane": 40, "identityExact": True},
            {**row, "ternPane": 41, "identityExact": False},
        ]
        (self.state_dir / "agents.json").write_text(json.dumps(payload))
        self.heartbeat()
        self.add("--kind", "time", "--in", "1s", "--prompt", "go")
        self.advance(seconds=2)
        self.tick()

    def claim(self, *argv: str) -> list[dict]:
        return self.run_cli("claim-prompts", "--session", SID, *argv)["prompts"]

    def test_a_pane_that_does_not_hold_the_session_exactly_gets_nothing(self) -> None:
        self.assertEqual(self.claim("--pane", "41"), [])
        self.assertEqual(self.state()["sessions"][SID]["prompt_queue"][0]["state"], "queued")
        self.assertEqual(self.claim("--pane", "40")[0]["text"], "go")

    def test_without_a_pane_or_an_exact_holder_the_claim_is_unchanged(self) -> None:
        payload = json.loads((self.state_dir / "agents.json").read_text())
        payload["agents"][0]["identityExact"] = False
        (self.state_dir / "agents.json").write_text(json.dumps(payload))
        self.assertEqual(self.claim("--pane", "41")[0]["text"], "go")

    def test_a_claim_without_its_pane_is_unchanged(self) -> None:
        self.assertEqual(self.claim()[0]["text"], "go")

    def test_a_stale_session_scan_refuses_no_claim(self) -> None:
        payload = json.loads((self.state_dir / "agents.json").read_text())
        payload["updatedAt"] = (self.clock.timestamp() - waiting.AGENTS_STALE_S - 1) * 1000
        (self.state_dir / "agents.json").write_text(json.dumps(payload))
        self.assertEqual(self.claim("--pane", "41")[0]["text"], "go")


class StampTests(Case):
    def stamp(self) -> str:
        return (self.state_dir / "waiting.stamp").read_text()

    def test_every_save_changes_the_stamp_and_nothing_else_does(self) -> None:
        self.add("--kind", "later")
        stamps = [self.stamp()]
        self.add("--kind", "time", "--in", "1h")
        stamps.append(self.stamp())
        self.heartbeat()
        stamps.append(self.stamp())
        self.assertEqual(len(set(stamps)), 3)
        self.assertEqual([int(stamp) for stamp in stamps], sorted(int(stamp) for stamp in stamps))
        self.run_cli("list")
        self.assertEqual(self.stamp(), stamps[-1])
        self.assertEqual(int(self.stamp()), self.state()["save_seq"])


class ToastFoldTests(Case):
    def fire(self, count: int) -> None:
        for index in range(count):
            self.add("--kind", "time", "--in", "1s", "--label", f"T{index}", session=f"{OTHER[:-1]}{index}")
        self.advance(seconds=2)
        self.tick()

    def toasts(self) -> list[str]:
        return [action["text"] for action in self.tick()["actions"] if action["type"] == "toast"]

    def test_toasts_no_window_showed_for_an_hour_fold_into_one(self) -> None:
        self.fire(3)
        self.advance(minutes=61)
        self.assertEqual(len(self.toasts()), 3)
        self.fire(1)
        self.assertEqual(len(self.toasts()), 4)
        self.advance(minutes=61)
        self.assertEqual(self.toasts(), ["4 waits changed while no Tern window was open"])

    def test_a_wait_that_toasted_again_counts_once(self) -> None:
        self.fire(1)
        state = self.state()
        toast = next(action for action in state["actions"] if action["type"] == "toast")
        state["actions"] += [{**toast, "id": f"again{index}"} for index in range(3)]
        (self.state_dir / "waiting.json").write_text(json.dumps(state))
        self.advance(minutes=61)
        self.assertEqual(self.toasts(), ["1 wait changed while no Tern window was open"])


class SharedFailureTests(Case):
    def test_a_failure_no_record_causes_is_the_ticks_error_not_a_quarantine(self) -> None:
        wait = self.add("--kind", "pr", "--repo", "o/r", "--number", "7")["id"]

        def broken():
            raise ImportError("no waiting_github")

        with mock.patch.object(waiting, "_github", broken):
            health = self.tick()["view"]["health"]
        self.assertEqual(health["error"], "PR checks: ImportError: no waiting_github")
        seen = self.wait_of(wait)
        self.assertEqual((seen["state"], seen["reason"]), ("waiting", None))


class ClampTests(Case):
    def edit_waits(self, change) -> None:
        state = self.state()
        for wait in state["sessions"][SID]["waits"]:
            change(wait)
        (self.state_dir / "waiting.json").write_text(json.dumps(state))

    def far(self) -> str:
        return (self.clock + timedelta(days=365)).isoformat()

    def test_times_a_clock_jump_left_far_ahead_come_back_within_their_interval(self) -> None:
        argv = json.dumps([sys.executable, "-c", "raise SystemExit(1)"])
        check = self.add("--kind", "command", "--argv", argv)["id"]
        runs = self.add("--kind", "repeat", "--every", "1h", "--prompt", "go")["id"]
        deadline = self.add("--kind", "later", "--deadline", self.far())["id"]
        self.tick()

        def jump(wait: dict) -> None:
            if wait["id"] == check:
                wait["next_check_at"] = self.far()
            if wait["id"] == runs:
                wait["repeat"]["next_at"] = self.far()

        self.edit_waits(jump)
        self.tick()
        waits = {wait["id"]: wait for wait in self.state()["sessions"][SID]["waits"]}
        bound = (self.clock + timedelta(seconds=waiting.DEFAULT_INTERVAL_S)).isoformat()
        self.assertEqual(waits[check]["next_check_at"], bound)
        self.assertEqual(waits[runs]["repeat"]["next_at"], (self.clock + timedelta(hours=1)).isoformat())
        self.assertEqual(waits[deadline]["deadline"], self.far())

    def test_a_snooze_from_before_snooze_s_keeps_its_length(self) -> None:
        argv = json.dumps([sys.executable, "-c", "raise SystemExit(1)"])
        check = self.add("--kind", "command", "--argv", argv)["id"]
        self.run_cli("snooze", "--session", SID, "--wait", check, "--for", "3d")
        self.edit_waits(lambda wait: wait.pop("snooze_s"))
        self.advance(minutes=1)
        self.tick()
        waits = {wait["id"]: wait for wait in self.state()["sessions"][SID]["waits"]}
        self.assertEqual(waits[check]["next_check_at"], (self.clock + timedelta(days=3, minutes=-1)).isoformat())

    def test_an_unsnoozed_wait_from_before_snooze_s_gets_its_interval(self) -> None:
        argv = json.dumps([sys.executable, "-c", "raise SystemExit(1)"])
        check = self.add("--kind", "command", "--argv", argv)["id"]
        self.tick()

        def jumped(wait: dict) -> None:
            wait.pop("snooze_s")
            wait["next_check_at"] = self.far()

        self.edit_waits(jumped)
        self.tick()
        waits = {wait["id"]: wait for wait in self.state()["sessions"][SID]["waits"]}
        bound = (self.clock + timedelta(seconds=waiting.DEFAULT_INTERVAL_S)).isoformat()
        self.assertEqual(waits[check]["next_check_at"], bound)

    def test_a_snooze_keeps_its_length(self) -> None:
        argv = json.dumps([sys.executable, "-c", "raise SystemExit(1)"])
        check = self.add("--kind", "command", "--argv", argv)["id"]
        fired = self.add("--kind", "command", "--argv", json.dumps([sys.executable, "-c", "pass"]))["id"]
        self.advance(seconds=2)
        self.tick()
        self.run_cli("snooze", "--session", SID, "--wait", check, "--for", "1d")
        self.run_cli("snooze", "--session", SID, "--wait", fired, "--for", "2d")
        self.edit_waits(lambda wait: wait.update(refire_at=self.far()) if wait["id"] == fired else None)
        self.tick()
        waits = {wait["id"]: wait for wait in self.state()["sessions"][SID]["waits"]}
        self.assertEqual(waits[check]["next_check_at"], (self.clock + timedelta(days=1)).isoformat())
        self.assertEqual(waits[fired]["refire_at"], (self.clock + timedelta(days=2)).isoformat())


class PruneTests(Case):
    def test_waits_that_ended_a_week_ago_leave_an_open_record(self) -> None:
        self.add("--kind", "later")
        ended = self.add("--kind", "time", "--in", "1s", "--label", "Old")["id"]
        self.advance(seconds=2)
        self.tick()
        self.run_cli("mark-seen", "--session", SID, "--wait", ended)
        ids = lambda: [wait["id"] for wait in self.state()["sessions"][SID]["waits"]]
        self.advance(days=7)
        self.tick()
        self.assertIn(ended, ids())
        self.advance(seconds=1)
        self.tick()
        self.assertNotIn(ended, ids())
        self.assertIn("Seen: Old", [line["text"] for line in self.state()["sessions"][SID]["history"]])

    def test_pr_cache_rows_unused_for_a_week_go(self) -> None:
        old = (self.clock - timedelta(days=8)).isoformat()
        recent = (self.clock - timedelta(days=6)).isoformat()
        entry = {"path": "/t", "offset": 0, "calls": [], "prs": []}
        (self.state_dir / "pr_cache.json").write_text(json.dumps({
            "version": waiting.PR_CACHE_VERSION,
            "sessions": {"gone": {**entry, "used_at": old}, "kept": {**entry, "used_at": recent}, "unstamped": entry},
            "statuses": {"o/r#1": {"asked_at": old}, "o/r#2": {"asked_at": recent}},
        }))
        cache = waiting.PrCache()
        self.assertEqual(sorted(cache.sessions), ["kept", "unstamped"])
        self.assertEqual(list(cache.statuses), ["o/r#2"])
        cache.save()
        saved = json.loads((self.state_dir / "pr_cache.json").read_text())
        self.assertEqual(list(saved["statuses"]), ["o/r#2"])
        # A row from before `used_at` counts from now, so it goes a week later.
        self.assertEqual(saved["sessions"]["unstamped"]["used_at"], self.clock.isoformat())
        self.advance(days=8)
        self.assertEqual(list(waiting.PrCache().sessions), [])

    def test_the_pr_cache_counts_from_tick_now(self) -> None:
        entry = {"path": "/t", "offset": 0, "calls": [], "prs": [], "used_at": (self.clock - timedelta(days=1)).isoformat()}
        (self.state_dir / "pr_cache.json").write_text(json.dumps({
            "version": waiting.PR_CACHE_VERSION, "sessions": {"old": entry}, "statuses": {},
        }))
        self.run_cli("tick", "--now", (self.clock + timedelta(days=7)).isoformat())
        self.assertEqual(json.loads((self.state_dir / "pr_cache.json").read_text())["sessions"], {})

    def test_a_chain_runs_on_past_a_pruned_step(self) -> None:
        first = self.add("--kind", "time", "--in", "9d")["id"]
        skipped = self.add("--kind", "later", "--after", first)["id"]
        last = self.add("--kind", "later", "--label", "Last", "--after", skipped)["id"]
        # A step that ended before it started and left the steps behind it, as older state has them.
        state = self.state()
        for wait in state["sessions"][SID]["waits"]:
            if wait["id"] == skipped:
                wait.update(state="done", done_at=self.clock.isoformat())
        (self.state_dir / "waiting.json").write_text(json.dumps(state))
        self.advance(days=8)
        self.tick()
        self.assertNotIn(skipped, [wait["id"] for wait in self.state()["sessions"][SID]["waits"]])
        self.advance(days=1)
        self.tick()
        self.assertEqual(self.wait_of(last)["state"], "waiting")


EXTENSION_PATH = Path(__file__).resolve().parents[1] / "omp-extension" / "waitingfor.ts"


@contextlib.contextmanager
def extension_folder():
    """A temporary folder for a driver run. The CLI calls the extension started last
    may outlive the driver; Windows can't delete a folder they still use, so wait."""
    folder = tempfile.mkdtemp()
    try:
        yield folder
    finally:
        for attempt in range(100):
            try:
                shutil.rmtree(folder)
                break
            except PermissionError:
                if attempt == 99:
                    raise
                time.sleep(0.1)


# Loads the extension into a stand-in for omp 18.19 and moves its session the way omp's
# SessionManager does when the file is open for writing elsewhere: a new id whose header continues
# the old one, then each `onPersistenceNotice` observer at once (which replays earlier notices on
# subscribe). NOTICE=1 makes two moves back to back; NOTICE=0 one with no notice, then a turn ends.
MOVE_DRIVER = r"""
import * as fs from "node:fs";
const { default: install } = await import(process.env.EXTENSION);
const [OLD, MID, NEW] = JSON.parse(process.env.IDS);
const state = () => { try { return JSON.parse(fs.readFileSync(process.env.STATE_FILE, "utf8")); } catch { return {}; } };
async function until(test) {
    for (let i = 0; i < 300; i++) { if (test(state())) return true; await Bun.sleep(50); }
    return false;
}
const chain = new Proxy(function () {}, { get: () => chain, apply: () => chain });
const handlers = {};
install({
    zod: chain, logger: { debug() {} }, sendUserMessage() {},
    pi: { Text: class { setText() {} render() { return []; } describe() { return {}; } } },
    on: (name, fn) => { handlers[name] = fn; }, registerCommand() {}, registerShortcut() {}, registerTool() {},
});
const observers = new Set();
const notices = [];
let subscriptions = 0;
const manager = {
    id: OLD, header: { type: "session", id: OLD },
    getSessionId() { return this.id; }, getHeader() { return this.header; }, getBranch: () => [],
    onPersistenceNotice(fn) {
        subscriptions += 1;
        for (const notice of notices) fn(notice);
        observers.add(fn);
        return () => observers.delete(fn);
    },
    move(id, notify) {
        this.header = { type: "session", id, parentSession: this.id, continuationOf: this.id };
        this.id = id;
        if (!notify) return;
        const notice = { reason: "open-elsewhere", from: "a", to: "b" };
        notices.push(notice);
        for (const fn of observers) fn(notice);
    },
};
let panelClears = 0; // a switch clears the panel; a move keeps it
const ctx = {
    hasUI: true, cwd: process.cwd(), isIdle: () => true, hasPendingMessages: () => false,
    ui: { setWidget(name, widget) { if (name === "waitingfor-panel" && !widget) panelClears += 1; } },
    sessionManager: manager,
    setTimeout: (fn, ms) => setTimeout(fn, ms), setInterval: (fn, ms) => setInterval(fn, ms),
    clearTimer: (timer) => clearTimeout(timer),
};
const out = {};
handlers.session_start({}, ctx);
out.started = await until((s) => s.extension_panes?.[OLD] === 52);
panelClears = 0;
if (process.env.NOTICE === "1") {
    manager.move(MID, true);
    manager.move(NEW, true);
} else {
    manager.move(NEW, false);
    handlers.agent_end({}, ctx);
}
out.moved = await until((s) => s.extension_panes?.[NEW] === 52);
out.panes = state().extension_panes ?? {};
out.panelClears = panelClears;
out.subscriptions = subscriptions;
handlers.session_shutdown();
out.unsubscribed = observers.size === 0;
console.log(JSON.stringify(out));
process.exit(0);
"""
MID = "01a1abf1-1111-748b-b9fa-126ebc0b88e7"


@unittest.skipUnless(shutil.which("bun"), "needs bun to load the omp extension")
class ExtensionSessionMoveTests(unittest.TestCase):
    """omp moves a session it can't write to a new file under a new id, with no session event."""

    def evaluate(self, code: str, **env: str) -> dict:
        return run_extension_driver(self, code, **env)

    def test_session_change_tells_a_move_from_a_switch(self) -> None:
        answer = self.evaluate(r"""
const { sessionChange } = await import(process.env.EXTENSION);
const manager = (id, continuationOf) => ({ getSessionId: () => id, getHeader: () => ({ id, continuationOf }) });
console.log(JSON.stringify({
    same: sessionChange(manager("a"), "a") ?? null,
    moved: sessionChange(manager("b", "a"), "a"),
    switched: sessionChange(manager("c", "x"), "a"),
    first: sessionChange(manager("a"), undefined),
    none: sessionChange(manager(undefined), "a") ?? null,
    headerless: sessionChange({ getSessionId: () => "b" }, "a"),
}));
""")
        self.assertEqual(answer, {
            "same": None, "moved": {"id": "b", "moved": True}, "switched": {"id": "c", "moved": False},
            "first": {"id": "a", "moved": False}, "none": None, "headerless": {"id": "b", "moved": False},
        })

    def moved(self, notice: str) -> dict:
        with extension_folder() as folder:
            root = Path(folder)
            (root / "omp").mkdir()
            return self.evaluate(
                MOVE_DRIVER, NOTICE=notice, IDS=json.dumps([SID, MID, OTHER]), TERN_PANE="52",
                XDG_STATE_HOME=str(root / "state"), OMP_AGENT_DIR=str(root / "omp"), PI_CODING_AGENT_DIR="",
                STATE_FILE=str(root / "state" / "codexbar-agentview" / "waiting.json"),
            )

    def assert_followed(self, answer: dict) -> None:
        self.assertTrue(answer["started"], answer)
        self.assertTrue(answer["moved"], answer)
        # The pane now reports the newest id, so the ids it moved from no longer claim it.
        self.assertEqual(answer["panes"], {OTHER: 52})
        # Each move is followed as one, not as a switch, on the one subscription.
        self.assertEqual((answer["panelClears"], answer["subscriptions"]), (0, 1))
        self.assertTrue(answer["unsubscribed"])

    def test_the_extension_follows_omps_move_notice(self) -> None:
        self.assert_followed(self.moved("1"))

    def test_the_extension_notices_a_move_without_the_notice(self) -> None:
        self.assert_followed(self.moved("0"))


def run_extension_driver(test: unittest.TestCase, code: str, **env: str) -> dict:
    """Runs `code` under bun with the extension's path in EXTENSION; the JSON of its last line."""
    with extension_folder() as folder:
        script = Path(folder) / "driver.mjs"
        script.write_text(code, encoding="utf-8")
        done = subprocess.run(
            ["bun", "run", str(script)], cwd=folder, capture_output=True, encoding="utf-8", timeout=60,
            env={**os.environ, "EXTENSION": str(EXTENSION_PATH), **env},
        )
    test.assertEqual(done.returncode, 0, done.stderr)
    return json.loads(done.stdout.strip().splitlines()[-1])


@unittest.skipUnless(shutil.which("bun"), "needs bun to load the omp extension")
class ExtensionWaitLineTests(unittest.TestCase):
    """/wait gh lines: a sentence's first word is read as a PR, state or repo only where the form
    takes one, a lone -- ends the arguments, and gh confirms a repo that could be a word."""

    def parse(self, *lines: str) -> dict:
        return run_extension_driver(self, r"""
const { parseWait } = await import(process.env.EXTENSION);
const read = (p) => ({ args: p.args ?? null, error: p.error ?? null, needs: p.needs ?? null, pr: p.pr ?? null,
    guessed: p.guessedRepo ?? null, without: p.withoutRepo?.args ?? null });
console.log(JSON.stringify(Object.fromEntries(JSON.parse(process.env.LINES).map((line) => [line, read(parseWait(line))]))));
""", LINES=json.dumps(lines))

    def test_a_state_word_stays_a_state_unless_a_lone_dash_dash_ends_the_arguments(self) -> None:
        user = ["--by", "user", "--park"]
        answer = self.parse(
            "gh pr review the patch", "gh pr -- review the patch", "gh pr -- review the patch -- Merge it",
            "gh pr 42 -- merged at last", "gh pr 42 ready waiting -- Merge it", "gh pr", "gh pr 42 --",
        )
        self.assertEqual(answer["gh pr review the patch"]["args"], ["--kind", "pr", "--until=review_on_head", "--note=the patch", *user])
        self.assertEqual(answer["gh pr -- review the patch"]["args"], ["--kind", "pr", "--note=review the patch", *user])
        self.assertEqual(answer["gh pr -- review the patch -- Merge it"]["args"], ["--kind", "pr", "--note=review the patch", "--prompt=Merge it", *user])
        self.assertEqual((answer["gh pr 42 -- merged at last"]["args"], answer["gh pr 42 -- merged at last"]["pr"]), (["--kind", "pr", "--note=merged at last", *user], "42"))
        self.assertEqual(answer["gh pr 42 ready waiting -- Merge it"]["args"], ["--kind", "pr", "--until=ready", "--note=waiting", "--prompt=Merge it", *user])
        self.assertEqual(answer["gh pr"]["error"], "gh pr needs a sentence.")
        self.assertEqual(answer["gh pr 42 --"]["error"], "gh pr needs a sentence.")

    def test_a_pr_reference_follows_githubs_name_rules(self) -> None:
        answer = self.parse("gh pr owner/repo#5 merged done", "gh pr -bad/repo#5 x", "gh babysit -- 42 is the answer")
        self.assertEqual(answer["gh pr owner/repo#5 merged done"]["pr"], "owner/repo#5")
        self.assertEqual((answer["gh pr -bad/repo#5 x"]["pr"], answer["gh pr -bad/repo#5 x"]["args"][2]), (None, "--note=-bad/repo#5 x"))
        self.assertEqual((answer["gh babysit -- 42 is the answer"]["pr"], answer["gh babysit -- 42 is the answer"]["args"][0]), (None, "--note=42 is the answer"))

    def test_a_word_that_can_be_a_repo_waits_for_gh_unless_dash_dash_says_it_is(self) -> None:
        answer = self.parse(
            "gh release try/catch cleanup landed", "gh release try/catch -- cleanup landed",
            "gh release -- try/catch cleanup landed", "gh release ../x hi", "every 2h until gh release try/catch -- Upgrade",
        )
        guessed = answer["gh release try/catch cleanup landed"]
        self.assertEqual((guessed["args"][2:4], guessed["guessed"], guessed["needs"]), (["--repo=try/catch", "--note=cleanup landed"], "try/catch", None))
        self.assertEqual(guessed["without"][2], "--note=try/catch cleanup landed")
        explicit = answer["gh release try/catch -- cleanup landed"]
        self.assertEqual((explicit["args"][2], explicit["guessed"]), ("--repo=try/catch", None))
        for line in ("gh release -- try/catch cleanup landed", "gh release ../x hi"):
            self.assertEqual((answer[line]["needs"], answer[line]["guessed"]), ("repo", None), line)
        repeat = answer["every 2h until gh release try/catch -- Upgrade"]
        self.assertEqual((repeat["guessed"], repeat["without"][2:4]), ("try/catch", ["--every=2h", "--note=try/catch"]))

    def test_completions_offer_the_end_of_the_arguments_where_it_works(self) -> None:
        answer = run_extension_driver(self, r"""
const { waitCompletions } = await import(process.env.EXTENSION);
const labels = (prefix) => (waitCompletions(prefix) ?? []).map((item) => item.label);
console.log(JSON.stringify(Object.fromEntries(["gh pr ", "gh pr 42 ", "gh release ", "gh babysit ", "every 2h until gh pr "].map((p) => [p, labels(p)]))));
""")
        states = ["ready", "review", "merged", "released", "activity"]
        self.assertEqual(answer, {
            "gh pr ": [*states, "--"], "gh pr 42 ": [*states, "--"], "gh release ": ["--"], "gh babysit ": ["--"],
            # There the first -- starts the prompt.
            "every 2h until gh pr ": states,
        })

    def test_the_reading_names_what_the_line_was_taken_for(self) -> None:
        answer = run_extension_driver(self, r"""
const { ghReading } = await import(process.env.EXTENSION);
console.log(JSON.stringify([
    ghReading("add", ["--repo=o/r", "--number=42", "--kind", "pr", "--until=review_on_head", "--note=x"]),
    ghReading("add", ["--repo=me/here", "--kind", "release", "--note=try/catch cleanup"], "try/catch"),
    ghReading("babysit-pr", ["--pr=https://github.com/o/r/pull/1"]),
]));
""")
        self.assertEqual(answer, [
            'Waiting on PR o/r#42 until review · "x"',
            'Waiting for the next release of me/here · "try/catch cleanup" · try/catch is no GitHub repo, so it stays in the sentence',
            "Babysitting PR https://github.com/o/r/pull/1",
        ])


@unittest.skipUnless(shutil.which("bun"), "needs bun to load the omp extension")
class ExtensionHealthWarningTests(unittest.TestCase):
    def test_a_recent_partial_error_reads_as_the_last_check(self) -> None:
        answer = run_extension_driver(self, r"""
const { healthWarning } = await import(process.env.EXTENSION);
const now = Date.parse("2026-11-12T12:00:00");
const at = (minutes) => new Date(now - minutes * 60_000).toISOString();
const error = "session abcd can't be shown: boom";
console.log(JSON.stringify([
    healthWarning({ checked_at: at(1), error, error_kind: "partial" }, now),
    healthWarning({ checked_at: at(1), error, error_kind: "partial", agents_stale_since: at(30) }, now),
    healthWarning({ checked_at: at(1), error, error_kind: "failed" }, now),
    healthWarning({ checked_at: at(1), error }, now),
    healthWarning({ checked_at: at(20), error, error_kind: "partial" }, now),
    healthWarning({ checked_at: at(1), agents_stale_since: at(30) }, now),
    healthWarning({ checked_at: at(1) }, now) ?? null,
    Array.from(healthWarning({ checked_at: at(1), error: "😀".repeat(130), error_kind: "partial" }, now)).length,
]));
""")
        self.assertEqual(answer, [
            "⚠ Last check: session abcd can't be shown: boom",
            "⚠ Last check: session abcd can't be shown: boom · session scan stale since 11:30",
            "⚠ Waits not checked since 11:59: session abcd can't be shown: boom",
            # A CLI from before error_kind: the tick may have failed.
            "⚠ Waits not checked since 11:59: session abcd can't be shown: boom",
            "⚠ Waits not checked since 11:40: session abcd can't be shown: boom",
            "⚠ Session scan stale since 11:30",
            None,
            len("⚠ Last check: ") + 120,
        ])

    def rows(self, pane: str) -> list:
        return run_extension_driver(self, r"""
const { closeVerdict, rowText } = await import(process.env.EXTENSION);
const now = Date.parse("2026-11-12T12:00:00");
const at = (minutes) => new Date(now - minutes * 60_000).toISOString();
const theme = { fg: (tone, text) => `<${tone}>${text}</>` };
const session = (close) => ({ composer_row: "⏲ Retry · in 2h", outcome: null, close });
const safe = { verdict: "safe", reason: "prompt", text: "closable" };
const park = { verdict: "park", reason: "turn", text: "park only: turn cut off once already" };
const checked = { checked_at: at(1) };
const row = (close, health) => rowText(session(close), theme, closeVerdict(session(close), health, now));
console.log(JSON.stringify([
    row(safe, checked),
    row(park, checked),
    // Nothing checks waits: whatever the session's verdict, it is park.
    row(safe, { checked_at: at(20) }),
    row(safe, { checked_at: at(1), error: "boom", error_kind: "failed" }),
    // One check failed in a recent tick: the others were checked.
    row(safe, { checked_at: at(1), error: "boom", error_kind: "partial" }),
    row(null, { checked_at: at(20) }),
]));
""", TERN_PANE=pane)

    def test_the_row_leads_with_whether_the_session_can_be_closed(self) -> None:
        self.assertEqual(self.rows("52"), [
            "<muted>closable</> │ ⏲ Retry · in 2h",
            "<warning>park only: turn cut off once already</> │ ⏲ Retry · in 2h",
            "<warning>park only: nothing checks waits</> │ ⏲ Retry · in 2h",
            "<warning>park only: nothing checks waits</> │ ⏲ Retry · in 2h",
            "<muted>closable</> │ ⏲ Retry · in 2h",
            "⏲ Retry · in 2h",
        ])

    def test_outside_tern_nothing_says_park(self) -> None:
        self.assertEqual(self.rows("")[1:4], [
            "<warning>keep open: turn cut off once already</> │ ⏲ Retry · in 2h",
            "<warning>keep open: nothing checks waits</> │ ⏲ Retry · in 2h",
            "<warning>keep open: nothing checks waits</> │ ⏲ Retry · in 2h",
        ])


# Loads the extension for SID, asks for a described check with /wait check, and lets the agent write
# it with its waitingfor tool once the queued instruction reached it.
DESCRIBED_CHECK_DRIVER = r"""
import * as fs from "node:fs";
const { default: install } = await import(process.env.EXTENSION);
const SID = process.env.SID;
const state = () => { try { return JSON.parse(fs.readFileSync(process.env.STATE_FILE, "utf8")); } catch { return {}; } };
async function until(test) {
    for (let i = 0; i < 300; i++) { if (test()) return true; await Bun.sleep(50); }
    return false;
}
const chain = new Proxy(function () {}, { get: () => chain, apply: () => chain });
const handlers = {};
const commands = {};
let tool;
const branch = [];
install({
    zod: chain, logger: { debug() {} },
    sendUserMessage(text) {
        branch.push({ type: "message", message: { role: "user", content: text, timestamp: Date.now() } });
        return Promise.resolve();
    },
    pi: { Text: class { setText() {} render() { return []; } describe() { return {}; } } },
    on: (name, fn) => { handlers[name] = fn; }, registerCommand: (name, spec) => { commands[name] = spec; },
    registerShortcut() {}, registerTool: (spec) => { tool = spec; },
});
const ctx = {
    hasUI: true, cwd: process.cwd(), isIdle: () => true, hasPendingMessages: () => false, ui: { setWidget() {} },
    sessionManager: {
        getSessionId: () => SID, getHeader: () => ({ type: "session", id: SID }), getBranch: () => branch,
        onPersistenceNotice: () => () => {},
    },
    setTimeout: (fn, ms) => setTimeout(fn, ms), setInterval: (fn, ms) => setInterval(fn, ms),
    clearTimer: (timer) => clearTimeout(timer),
};
handlers.session_start({}, ctx);
await commands.wait.handler("check every 10m the deploy is green -- tell me", ctx);
const queue = () => state().sessions?.[SID]?.prompt_queue ?? [];
await until(() => queue()[0]?.state === "delivered");
const sent = branch.map((entry) => entry.message.content);
const extra = await tool.execute("t1", { action: "add", kind: "command", argv: ["true"], label: "x" }, null, null, ctx).catch((error) => error.message);
await tool.execute("t2", { action: "add", kind: "command", argv: ["true"] }, null, null, ctx);
handlers.session_shutdown();
const waits = state().sessions[SID].waits.map((wait) => ({
    state: wait.state, argv: wait.condition.argv, label: wait.label, interval: wait.condition.interval_s, prompt: wait.prompt,
}));
console.log(JSON.stringify({ sent, extra, waits, queue: queue().length }));
process.exit(0);
"""


@unittest.skipUnless(shutil.which("bun"), "needs bun to load the omp extension")
class ExtensionDescribedCheckTests(unittest.TestCase):
    def test_the_instruction_goes_through_the_queue_and_the_tool_writes_the_check(self) -> None:
        with extension_folder() as folder:
            root = Path(folder)
            (root / "omp").mkdir()
            answer = run_extension_driver(
                self, DESCRIBED_CHECK_DRIVER, SID=SID,
                STATE_FILE=str(root / "state" / "codexbar-agentview" / "waiting.json"),
                XDG_STATE_HOME=str(root / "state"), OMP_AGENT_DIR=str(root / "omp"), PI_CODING_AGENT_DIR="",
                TERN_PANE="",
            )
        self.assertEqual(answer["sent"], [waiting._check_instruction("the deploy is green")])
        self.assertIn("call it again with action, kind and argv only, without label", answer["extra"])
        self.assertEqual(answer["waits"], [{
            "state": "waiting", "argv": ["true"], "label": "the deploy is green", "interval": 600,
            "prompt": "tell me",
        }])
        self.assertEqual(answer["queue"], 0)


# Sets a session aside from the menu with Close the session, as a user would, and reports whether
# omp was shut down and what the panel says.
MENU_CLOSE_DRIVER = r"""
const { default: install } = await import(process.env.EXTENSION);
const SID = process.env.SID;
const chain = new Proxy(function () {}, { get: () => chain, apply: () => chain });
const handlers = {};
const commands = {};
class Text { constructor(text = "") { this.text = text; } setText(text) { this.text = text; } render() { return [this.text]; } describe() { return {}; } }
install({
    zod: chain, logger: { debug() {} }, sendUserMessage() {}, pi: { Text },
    on: (name, fn) => { handlers[name] = fn; }, registerCommand: (name, spec) => { commands[name] = spec; },
    registerShortcut() {}, registerTool() {},
});
const widgets = {};
const choices = ["Retry later", "Close the session"];
const lines = ["1h", "", ""];
let shutdown = false;
const ctx = {
    hasUI: true, cwd: process.cwd(), isIdle: () => true, hasPendingMessages: () => false,
    getAsyncJobSnapshot: () => ({ running: JSON.parse(process.env.JOBS || "[]") }),
    ui: {
        setWidget(name, factory) { widgets[name] = factory; },
        select: async () => choices.shift(), editor: async () => lines.shift(),
    },
    sessionManager: {
        getSessionId: () => SID, getHeader: () => ({ type: "session", id: SID }), getBranch: () => [],
        onPersistenceNotice: () => () => {},
    },
    setTimeout: (fn, ms) => setTimeout(fn, ms), setInterval: (fn, ms) => setInterval(fn, ms),
    clearTimer: (timer) => clearTimeout(timer),
    shutdown: async () => { shutdown = true; },
};
handlers.session_start({}, ctx);
await Bun.sleep(300);
await commands.wait.handler("", ctx);
const panel = widgets["waitingfor-panel"]?.(null, { fg: (_tone, text) => text })?.render(200).join("\n") ?? null;
handlers.session_shutdown();
console.log(JSON.stringify({ shutdown, panel, left: choices.length }));
process.exit(0);
"""


@unittest.skipUnless(shutil.which("bun"), "needs bun to load the omp extension")
class ExtensionMenuCloseTests(unittest.TestCase):
    def closed(self, strip_close: bool, jobs: str = "[]") -> dict:
        with extension_folder() as folder:
            root = Path(folder)
            (root / "omp").mkdir()
            extension = EXTENSION_PATH
            if strip_close:
                # The CLI as an older or broken one would answer: views without `close`. A copy
                # of the extension finds this wrapper where it looks for the CLI, on any platform.
                package = root / "package"
                extension = package / "omp-extension" / EXTENSION_PATH.name
                scripts = package / "contents" / "scripts"
                extension.parent.mkdir(parents=True)
                scripts.mkdir(parents=True)
                shutil.copy(EXTENSION_PATH, extension)
                shutil.copy(SCRIPT_PATH.parent / "waiting_menu.json", scripts)
                (scripts / SCRIPT_PATH.name).write_text(
                    "import json, subprocess, sys\n"
                    f"done = subprocess.run([sys.executable, {str(SCRIPT_PATH)!r}, *sys.argv[1:]], capture_output=True)\n"
                    "sys.stderr.buffer.write(done.stderr)\n"
                    "answer = json.loads(done.stdout)\n"
                    "for row in (answer.get('view') or answer).get('sessions') or []:\n"
                    "    row.pop('close', None)\n"
                    "print(json.dumps(answer))\n"
                    "sys.exit(done.returncode)\n"
                )
            # A transcript makes the session resumable, as one with messages is.
            (root / "omp" / "sessions" / "-w").mkdir(parents=True)
            (root / "omp" / "sessions" / "-w" / f"2026-11-05_{SID}.jsonl").write_text("")
            return run_extension_driver(
                self, MENU_CLOSE_DRIVER, SID=SID, JOBS=jobs, EXTENSION=str(extension),
                XDG_STATE_HOME=str(root / "state"), OMP_AGENT_DIR=str(root / "omp"), PI_CODING_AGENT_DIR="",
                TERN_PANE="",
            )

    def test_a_saved_wait_with_a_verdict_closes_the_session(self) -> None:
        answer = self.closed(strip_close=False)
        self.assertEqual((answer["shutdown"], answer["left"]), (True, 0), answer)

    def test_without_a_verdict_for_the_saved_wait_it_stays_open(self) -> None:
        answer = self.closed(strip_close=True)
        self.assertEqual((answer["shutdown"], answer["left"]), (False, 0), answer)
        self.assertIn("Not closed: no close verdict came back for it", answer["panel"])

    def test_a_running_job_keeps_close_off_the_menu(self) -> None:
        answer = self.closed(strip_close=False, jobs='[{"id": "bg_1", "type": "bash", "label": "sleep 120"}]')
        # Outside Tern, with Close off the menu, nothing is left to ask: the wait is saved and the session stays.
        self.assertEqual((answer["shutdown"], answer["left"]), (False, 1), answer)


# Loads the extension for SID and changes what runs in it: a background job, then a bash call. The
# heartbeats tell the CLI each change (`extension_activity`).
ACTIVITY_DRIVER = r"""
import * as fs from "node:fs";
const { default: install } = await import(process.env.EXTENSION);
const SID = process.env.SID;
const state = () => { try { return JSON.parse(fs.readFileSync(process.env.STATE_FILE, "utf8")); } catch { return {}; } };
const activity = () => state().extension_activity?.[SID];
async function until(busy, work) {
    for (let i = 0; i < 200; i++) {
        const seen = activity();
        if (seen?.busy === busy && seen?.work === work) return true;
        await Bun.sleep(50);
    }
    return false;
}
const chain = new Proxy(function () {}, { get: () => chain, apply: () => chain });
const handlers = {};
install({
    zod: chain, logger: { debug() {} }, sendUserMessage() {},
    pi: { Text: class { setText() {} render() { return []; } describe() { return {}; } } },
    on: (name, fn) => { handlers[name] = fn; }, registerCommand() {}, registerShortcut() {}, registerTool() {},
});
let jobs = [{ id: "bg_1", type: "bash", label: "sleep 120" }];
let idle = true;
const ctx = {
    hasUI: true, cwd: process.cwd(), isIdle: () => idle, hasPendingMessages: () => false, ui: { setWidget() {} },
    getAsyncJobSnapshot: () => ({ running: jobs }),
    sessionManager: {
        getSessionId: () => SID, getHeader: () => ({ type: "session", id: SID }), getBranch: () => [],
        onPersistenceNotice: () => () => {},
    },
    setTimeout: (fn, ms) => setTimeout(fn, ms), setInterval: (fn, ms) => setInterval(fn, ms),
    clearTimer: (timer) => clearTimeout(timer),
};
const steps = [];
handlers.session_start({}, ctx);
steps.push(await until(false, true));
jobs = [];
steps.push(await until(false, false));
idle = false;
handlers.tool_execution_start({ toolName: "bash", toolCallId: "c1" }, ctx);
steps.push(await until(true, true));
handlers.tool_execution_end({ toolName: "bash", toolCallId: "c1" }, ctx);
idle = true;
steps.push(await until(false, false));
handlers.session_shutdown();
console.log(JSON.stringify(steps));
process.exit(0);
"""


@unittest.skipUnless(shutil.which("bun"), "needs bun to load the omp extension")
class ExtensionActivityTests(unittest.TestCase):
    def test_each_change_of_what_runs_reaches_the_cli(self) -> None:
        with extension_folder() as folder:
            root = Path(folder)
            (root / "omp").mkdir()
            answer = run_extension_driver(
                self, ACTIVITY_DRIVER, SID=SID,
                STATE_FILE=str(root / "state" / "codexbar-agentview" / "waiting.json"),
                XDG_STATE_HOME=str(root / "state"), OMP_AGENT_DIR=str(root / "omp"), PI_CODING_AGENT_DIR="",
                TERN_PANE="",
            )
        # A job at the start, gone, a bash call in a turn, the turn over.
        self.assertEqual(answer, [True, True, True, True])


# Calls the waitingfor tool as the agent would, asking it to park, in Tern (TERN_PANE) or not.
TOOL_PARK_DRIVER = r"""
import * as fs from "node:fs";
const { default: install } = await import(process.env.EXTENSION);
const SID = process.env.SID;
const chain = new Proxy(function () {}, { get: () => chain, apply: () => chain });
const handlers = {};
let tool;
install({
    zod: chain, logger: { debug() {} }, sendUserMessage() {},
    pi: { Text: class { setText() {} render() { return []; } describe() { return {}; } } },
    on: (name, fn) => { handlers[name] = fn; }, registerCommand() {}, registerShortcut() {}, registerTool: (spec) => { tool = spec; },
});
const ctx = {
    hasUI: true, cwd: process.cwd(), isIdle: () => true, hasPendingMessages: () => false, ui: { setWidget() {} },
    sessionManager: {
        getSessionId: () => SID, getHeader: () => ({ type: "session", id: SID }), getBranch: () => [],
        onPersistenceNotice: () => () => {},
    },
    setTimeout: (fn, ms) => setTimeout(fn, ms), setInterval: (fn, ms) => setInterval(fn, ms),
    clearTimer: (timer) => clearTimeout(timer),
};
handlers.session_start({}, ctx);
const notes = [];
for (const params of [{ action: "add", kind: "later", park: true }, { action: "park" }]) {
    const answer = await tool.execute("t", params, null, null, ctx);
    notes.push(JSON.parse(answer.content[0].text).park);
}
handlers.session_shutdown();
let actions = [];
try { actions = JSON.parse(fs.readFileSync(process.env.STATE_FILE, "utf8")).actions; } catch {}
const parks = actions.filter((action) => action.type === "park").map((action) => [action.session, action.pane]);
console.log(JSON.stringify({ notes, parks }));
process.exit(0);
"""


@unittest.skipUnless(shutil.which("bun"), "needs bun to load the omp extension")
class ExtensionToolParkTests(unittest.TestCase):
    def parked(self, pane: str, listed: int | None = 52) -> dict:
        with extension_folder() as folder:
            root = Path(folder)
            (root / "omp").mkdir()
            state = root / "state" / "codexbar-agentview"
            state.mkdir(parents=True)
            (state / "agents.json").write_text(json.dumps({"bootId": "", "history": [], "agents": [{
                "sessionId": SID, "provider": "omp", "cwd": folder, "state": "idle", "ternPane": listed,
                "resumeCommand": f"omp --resume {SID}",
            }]}))
            return run_extension_driver(
                self, TOOL_PARK_DRIVER, SID=SID, STATE_FILE=str(state / "waiting.json"),
                XDG_STATE_HOME=str(root / "state"), OMP_AGENT_DIR=str(root / "omp"), PI_CODING_AGENT_DIR="",
                TERN_PANE=pane,
            )

    def test_in_tern_the_park_reaches_the_cli(self) -> None:
        answer = self.parked("52")
        self.assertEqual(answer["parks"], [[SID, 52], [SID, 52]])
        self.assertTrue(all(note.startswith("parked:") for note in answer["notes"]), answer)

    def test_a_pane_tern_has_not_listed_yet_is_not_parked(self) -> None:
        answer = self.parked("52", listed=None)
        self.assertEqual(answer["notes"], [
            "not parked: Tern has not listed this session's pane yet; try again in a few seconds",
        ] * 2)

    def test_outside_tern_nothing_is_parked_and_the_result_says_so(self) -> None:
        answer = self.parked("")
        self.assertEqual(answer["parks"], [])
        self.assertEqual(answer["notes"], [
            "not parked: this omp runs outside Tern, so there is no pane to park",
        ] * 2)


# Loads the extension with a wait under OLD, then omp moves the session once per REASONS entry (two:
# through MID): the heartbeats tell the CLI, which re-keys the record when it was replaced.
MOVE_REASON_DRIVER = r"""
import * as fs from "node:fs";
import { execFileSync } from "node:child_process";
const { default: install } = await import(process.env.EXTENSION);
const [OLD, MID, NEW] = JSON.parse(process.env.IDS);
const reasons = JSON.parse(process.env.REASONS);
execFileSync(process.env.PYTHON, [process.env.CLI, "add", "--session", OLD, "--kind", "later", "--label", "Keep"]);
const state = () => { try { return JSON.parse(fs.readFileSync(process.env.STATE_FILE, "utf8")); } catch { return {}; } };
async function until(test) {
    for (let i = 0; i < 300; i++) { if (test(state())) return true; await Bun.sleep(50); }
    return false;
}
const chain = new Proxy(function () {}, { get: () => chain, apply: () => chain });
const handlers = {};
install({
    zod: chain, logger: { debug() {} }, sendUserMessage() {},
    pi: { Text: class { setText() {} render() { return []; } describe() { return {}; } } },
    on: (name, fn) => { handlers[name] = fn; }, registerCommand() {}, registerShortcut() {}, registerTool() {},
});
const observers = new Set();
const manager = {
    id: OLD, header: { type: "session", id: OLD },
    getSessionId() { return this.id; }, getHeader() { return this.header; }, getBranch: () => [],
    onPersistenceNotice(fn) { observers.add(fn); return () => observers.delete(fn); },
    move(id, reason) {
        this.header = { type: "session", id, continuationOf: this.id };
        this.id = id;
        for (const fn of observers) fn({ reason, from: "a", to: "b" });
    },
};
const ctx = {
    hasUI: true, cwd: process.cwd(), isIdle: () => true, hasPendingMessages: () => false, ui: { setWidget() {} },
    sessionManager: manager,
    setTimeout: (fn, ms) => setTimeout(fn, ms), setInterval: (fn, ms) => setInterval(fn, ms),
    clearTimer: (timer) => clearTimeout(timer),
};
const labels = (s, id) => (s.sessions?.[id]?.waits ?? []).map((wait) => wait.label);
handlers.session_start({}, ctx);
await until((s) => s.extension_panes?.[OLD] === 52);
if (reasons.length === 2) manager.move(MID, reasons[0]);
manager.move(NEW, reasons.at(-1));
await until((s) => s.extension_panes?.[NEW] === 52 && (reasons.includes("open-elsewhere") || labels(s, NEW).length > 0));
await Bun.sleep(300); // a second move's heartbeat
handlers.session_shutdown();
console.log(JSON.stringify({ old: labels(state(), OLD), new: labels(state(), NEW) }));
process.exit(0);
"""


@unittest.skipUnless(shutil.which("bun"), "needs bun to load the omp extension")
class ExtensionMoveReasonTests(unittest.TestCase):
    def moved(self, *reasons: str) -> dict:
        with extension_folder() as folder:
            root = Path(folder)
            (root / "omp").mkdir()
            env = {
                "XDG_STATE_HOME": str(root / "state"), "OMP_AGENT_DIR": str(root / "omp"), "PI_CODING_AGENT_DIR": "",
                "TERN_PANE": "52",
            }
            return run_extension_driver(
                self, MOVE_REASON_DRIVER, REASONS=json.dumps(reasons), IDS=json.dumps([SID, MID, OTHER]),
                CLI=str(SCRIPT_PATH), PYTHON=sys.executable,
                STATE_FILE=str(root / "state" / "codexbar-agentview" / "waiting.json"), **env,
            )

    def test_a_replaced_session_takes_its_waits_along(self) -> None:
        self.assertEqual(self.moved("replaced"), {"old": [], "new": ["Keep"]})

    def test_back_to_back_moves_reach_the_newest_id(self) -> None:
        self.assertEqual(self.moved("contested", "replaced"), {"old": [], "new": ["Keep"]})

    def test_a_session_open_elsewhere_keeps_its_waits(self) -> None:
        self.assertEqual(self.moved("open-elsewhere"), {"old": ["Keep"], "new": []})

    def test_no_move_crosses_into_a_session_open_elsewhere(self) -> None:
        # OLD's record was MID's to take, and MID is another process's now: it stays, and nothing reaches NEW.
        self.assertEqual(self.moved("replaced", "open-elsewhere"), {"old": ["Keep"], "new": []})


if __name__ == "__main__":
    unittest.main()

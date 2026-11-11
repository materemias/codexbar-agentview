from __future__ import annotations

import contextlib
import fcntl
import importlib.util
import io
import json
import os
import shlex
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from datetime import datetime, timedelta
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

SID = "01a1abf1-15c8-748b-b9fa-126ebc0b88e5"
OTHER = "01a1abf1-0000-748b-b9fa-126ebc0b88e6"
T0 = datetime(2026, 11, 5, 12, 0, 0).astimezone()


def fake_github(**overrides) -> types.SimpleNamespace:
    """waiting_github's contract, answering "nothing happened yet"."""
    defaults = dict(
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

    def tick(self) -> dict:
        return self.run_cli("tick", "--now", self.clock.isoformat())

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
        return json.loads((self.state_dir / "waiting.json").read_text())

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
            ["add", "--session", SID, "--kind", "command", "--argv", "ls 'open"],
            ["add", "--session", SID, "--kind", "command", "--argv", '["ls"]',
             "--predicate", "stdout_regex:("],
            ["add", "--session", SID, "--kind", "time", "--at", "24:00"],
            ["add", "--session", SID, "--kind", "time", "--at", "18:00", "--in", "1h"],
            ["add", "--session", SID, "--kind", "time", "--at", "18:60"],
            ["add", "--session", SID, "--kind", "something"],
            ["add", "--session", SID, "--kind", "something", "--what", "  "],
            ["done", "--session", SID, "--wait", "w99"],
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

    def test_events_by_pane_resolve_the_session(self) -> None:
        self.agents(SID)
        self.heartbeat()
        wait, _ = self.queued_prompt()
        self.run_cli("event", "--type", "turn_finished", "--pane", "40")
        self.assertEqual(self.wait_of(wait)["state"], "review")

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

    def test_something_never_fires_and_ends_with_done(self) -> None:
        answer = self.add("--kind", "something", "--what", "Reply from Dóra")
        wait = answer["id"]
        self.assertEqual((answer["label"], answer["summary"]), ("Reply from Dóra", "Reply from Dóra"))
        self.advance(days=2)
        self.assertEqual(self.tick()["actions"], [])
        seen = self.wait_of(wait)
        self.assertEqual((seen["state"], seen["ball"]), ("waiting", "on_someone"))
        self.assertIsNone(seen["due_at"])
        self.assertEqual(self.session()["status"], "⏳ Reply from Dóra · 2d")
        self.run_cli("done", "--session", SID, "--wait", wait)
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

    def test_a_bare_clock_keeps_the_wall_clock_over_a_dst_change(self) -> None:
        self.addCleanup(time.tzset)
        with mock.patch.dict(os.environ, {"TZ": "CET-1CEST,M3.5.0,M10.5.0/3"}):
            time.tzset()
            self.clock = datetime(2026, 10, 24, 20, 0, 0).astimezone()
            wait = self.add("--kind", "time", "--at", "18:00")
        self.assertEqual(wait["condition"]["at"], "2026-10-25T18:00:00+01:00")

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
        self.run_cli("done", "--session", SID, "--wait", first)
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
        class GhError(Exception):
            transient = True

        reads = iter(["down", "up"])

        def snapshot(repo, number):
            if next(reads) == "down":
                raise GhError("gh timed out")
            return {"head": "abc123", "threads": []}

        first = self.fix_chain(lambda repo, number, before: (True, "new head"))
        sys.modules["waiting_github"].snapshot = snapshot
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
        self.assertEqual(self.wait_of(wait)["ball"], "on_machine")
        self.advance(hours=2)
        self.tick()
        self.assertEqual(self.wait_of(wait)["state"], "fired")


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

    def test_a_note_after_done_starts_the_next_record(self) -> None:
        wait = self.add("--kind", "later", "--note", "old")["id"]
        self.run_cli("done", "--session", SID, "--wait", wait)
        self.run_cli("note", "--session", SID, "--text", "new")
        self.add("--kind", "later")
        seen = self.session()
        self.assertEqual(seen["note"], "new")
        self.assertEqual(seen["history"], [])
        self.assertEqual(len(seen["waits"]), 1)

    def test_a_note_without_a_wait_does_not_stay_forever(self) -> None:
        self.run_cli("note", "--session", SID, "--text", "forgotten")
        self.advance(days=8)
        self.tick()
        self.assertNotIn(SID, self.state()["sessions"])

    def test_a_new_set_aside_does_not_inherit_the_old_sentence(self) -> None:
        wait = self.add("--kind", "later", "--note", "old")["id"]
        self.run_cli("done", "--session", SID, "--wait", wait)
        self.add("--kind", "later")
        seen = self.session()
        self.assertIsNone(seen["note"])
        self.assertEqual(seen["history"], [])

    def test_note_before_the_wait_survives(self) -> None:
        self.run_cli("note", "--session", SID, "--text", "first")
        self.add("--kind", "later")
        self.assertEqual(self.session()["note"], "first")


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

    def test_a_resumed_session_keeps_its_pane_before_the_sweep_sees_it(self) -> None:
        self.agents(closed=[{"sessionId": SID, "cwd": "/w", "windowTitle": "x"}])
        payload = json.loads((self.state_dir / "agents.json").read_text())
        payload["updatedAt"] = int(self.clock.timestamp() * 1000)
        (self.state_dir / "agents.json").write_text(json.dumps(payload))
        self.advance(seconds=5)
        self.heartbeat(SID, "--pane", "52")
        self.assertEqual(list(self.run_cli("list")["extension_panes"]), ["52"])


class ChainTests(Case):
    def test_done_activates_after(self) -> None:
        first = self.add("--kind", "something", "--what", "x")["id"]
        second = self.add("--kind", "time", "--in", "1h", "--after", first)
        self.assertEqual(second["state"], "pending")
        self.assertIsNone(second["due_at"])
        self.assertEqual(self.wait_of(first)["next"], second["id"])
        self.advance(hours=2)
        self.tick()
        self.assertEqual(self.wait_of(second["id"])["state"], "pending")
        self.run_cli("done", "--session", SID, "--wait", first)
        seen = self.wait_of(second["id"])
        self.assertEqual(seen["state"], "waiting")
        self.assertEqual(seen["condition"]["at"], (self.clock + timedelta(hours=1)).isoformat())

    def test_firing_without_a_prompt_moves_the_chain_on(self) -> None:
        first = self.add("--kind", "time", "--in", "1h")["id"]
        second = self.add("--kind", "something", "--what", "x", "--after", first)["id"]
        self.advance(hours=1)
        self.tick()
        self.assertEqual(self.wait_of(first)["state"], "fired")
        self.assertEqual(self.wait_of(second)["state"], "waiting")
        self.run_cli("done", "--session", SID, "--wait", first)
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

    def test_done_on_a_pending_step_keeps_the_chain_whole(self) -> None:
        first = self.add("--kind", "later")["id"]
        second = self.add("--kind", "later", "--after", first)["id"]
        third = self.add("--kind", "later", "--after", second)["id"]
        self.run_cli("done", "--session", SID, "--wait", second)
        self.assertEqual(self.wait_of(third)["state"], "pending")
        self.run_cli("done", "--session", SID, "--wait", first)
        self.assertEqual(self.wait_of(third)["state"], "waiting")

    def test_unknown_after(self) -> None:
        self.run_cli("add", "--session", SID, "--kind", "later", "--after", "w9", expect=2)

    def test_done_all_leaves_the_view(self) -> None:
        first = self.add("--kind", "later")["id"]
        self.add("--kind", "later", "--after", first)
        view = self.run_cli("done", "--session", SID)["view"]
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
        self.assertEqual(claimed["view"]["sessions"][0]["prompts_queued"], 0)

        # The extension died between send and ack: its next run must look first.
        self.advance(minutes=3)
        self.assertEqual(self.session()["prompts_queued"], 2)
        again = self.run_cli("claim-prompts", "--session", SID)["prompts"][0]
        self.assertEqual((again["action"], again["resend_check"]), (prompt["action"], True))

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
        self.run_cli("done", "--session", SID, "--wait", wait)
        self.assertTrue(self.run_cli("ack", "--action", action, "--result", "delivered")["ignored"])

    def test_turn_without_verify_goes_to_review_then_done_moves_on(self) -> None:
        wait, _ = self.queued_prompt()
        following = self.add("--kind", "later", "--after", wait)["id"]
        self.run_cli("event", "--type", "turn_finished", "--session", SID)
        seen = self.wait_of(wait)
        self.assertEqual((seen["state"], seen["ball"], seen["reason"]), ("review", "on_me", "ran, take a look"))
        self.assertEqual(self.wait_of(following)["state"], "pending")
        self.run_cli("done", "--session", SID, "--wait", wait)
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
        self.advance(minutes=20)
        self.tick()
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
        self.advance(minutes=20)
        self.tick()
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
        self.assertEqual(self.wait_of(wait)["state"], "needs_attention")

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

    def test_session_closing_during_the_turn_needs_attention(self) -> None:
        wait, _ = self.queued_prompt()
        self.agents()
        self.tick()
        self.assertEqual(self.wait_of(wait)["state"], "needs_attention")
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
        self.run_cli("done", "--session", SID)
        self.add("--kind", "time", "--in", "2s")
        self.assertEqual(self.tick()["next_tick_s"], 15)

    def test_unacked_actions_tick_soon(self) -> None:
        self.add("--kind", "later", "--park")
        self.assertEqual(self.tick()["next_tick_s"], 30)

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
                "condition", "has_prompt", "next", "by",
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
                "schedules", "history", "prs",
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
        self.agents(timer)
        self.write_schedule(timer, [{"id": 1, "prompt": "p", "dueAt": int(T0.timestamp() * 1000)}])
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
            [fired, sids[2], sids[3], sids[4], timer, sids[1], sids[0]],
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

    def write_schedule(self, sid: str, jobs: object) -> None:
        folder = self.omp / "schedule"
        folder.mkdir(exist_ok=True)
        (folder / f"{sid}.json").write_text(json.dumps(jobs))

    def test_schedule_jobs_of_a_waiting_session(self) -> None:
        due = int((T0 + timedelta(hours=1)).timestamp() * 1000)
        self.write_schedule(SID, [
            {"id": 2, "prompt": "later", "dueAt": due + 3600000},
            {"id": 1, "prompt": "check", "everyMs": 7200000, "dueAt": due},
            {"id": 3},
        ])
        self.add("--kind", "later", "--label", "Big one")
        seen = self.session()
        self.assertEqual(
            seen["schedules"][0],
            {"id": 1, "prompt": "check", "every_s": 7200, "due_at": (T0 + timedelta(hours=1)).isoformat()},
        )
        self.assertEqual(len(seen["schedules"]), 2)
        self.assertTrue(seen["line"].endswith("↻ every 2h · next 13:00"), seen["line"])
        self.assertNotIn("↻", seen["composer_row"])

    def test_schedule_only_session_shows_while_live(self) -> None:
        due = int((T0 + timedelta(hours=1)).timestamp() * 1000)
        self.write_schedule(SID, [{"id": 1, "prompt": "check", "everyMs": 5400000, "dueAt": due}])
        self.assertEqual(self.run_cli("list")["sessions"], [])
        self.agents(SID)
        seen = self.session()
        self.assertEqual(seen["ball"], "on_timer")
        self.assertEqual(self.run_cli("list")["counts"]["on_timer"], 1)
        self.assertEqual(seen["line"], "↻ every 90m · next 13:00")
        self.assertEqual(seen["composer_row"], "")

    def test_a_wait_or_a_pin_keeps_its_ball_beside_schedules(self) -> None:
        due = int((T0 + timedelta(hours=1)).timestamp() * 1000)
        self.write_schedule(SID, [{"id": 1, "prompt": "check", "dueAt": due}])
        self.agents(SID)
        wait = self.add("--kind", "something", "--what", "x")["id"]
        seen = self.session()
        self.assertEqual((seen["ball"], len(seen["schedules"])), ("on_someone", 1))
        self.run_cli("done", "--session", SID, "--wait", wait)
        self.assertEqual(self.session()["ball"], "on_timer")
        self.pin(SID)
        self.assertEqual(self.session()["ball"], "on_my_time")

    def test_broken_schedule_file_means_no_jobs(self) -> None:
        folder = self.omp / "schedule"
        folder.mkdir()
        (folder / f"{SID}.json").write_text("{")
        self.add("--kind", "later")
        self.assertEqual(self.session()["schedules"], [])

    def pin(self, *sids: str) -> None:
        (self.omp / "session-pins.json").write_text(json.dumps(list(sids)))

    def test_pinned_session_without_waits(self) -> None:
        self.pin(SID)
        seen = self.session()
        self.assertEqual((seen["pinned"], seen["ball"], seen["waits"]), (True, "on_my_time", []))
        self.assertEqual(seen["line"], "📌 pinned")
        self.assertEqual(self.run_cli("list")["counts"]["on_my_time"], 1)

    def test_pinned_keeps_its_sentence_after_done(self) -> None:
        self.pin(SID)
        wait = self.add("--kind", "time", "--in", "1h", "--note", "mine")["id"]
        self.run_cli("done", "--session", SID, "--wait", wait)
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
        self.assertEqual(seen["resume_command"], f"cd -- /w/project && omp --resume {SID}")
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
        self.transcript(SID)
        answer = self.run_cli("resume", "--session", SID)
        self.assertEqual(answer["command"], f"cd -- /w/project && omp --resume {SID}")
        self.assertEqual(answer["cwd"], "/w/project")

    def test_resume_prefers_agents_history(self) -> None:
        self.agents(closed=[{"sessionId": SID, "cwd": "/w", "resumeCommand": "omp --resume saved"}])
        self.assertEqual(self.run_cli("resume", "--session", SID)["command"], "omp --resume saved")

    def test_resume_errors(self) -> None:
        self.run_cli("resume", "--session", SID, expect=2)
        self.agents(SID)
        self.run_cli("resume", "--session", SID, expect=2)

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
                "resume_command": f"cd -- /w/project && omp --resume {SID}",
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
        self.run_cli("done", "--session", SID, "--wait", wait)
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
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                free.append(True)
            except BlockingIOError:
                free.append(False)
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
        self.run_cli("done", "--session", SID, "--wait", wait)
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
        self.run_cli("done", "--session", SID, "--wait", first)
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


class FiredPrTests(Case):
    """A fired `review_on_head` or `ready` is taken back when the PR moves under it."""

    def setUp(self) -> None:
        super().setUp()
        self.pr = {"head": "a" * 40, "merge_state": "CLEAN", "holds": True}

        def poll(cond, memo, **kwargs):
            holds = self.pr["holds"] and cond["until"] in ("review_on_head", "ready")
            return poll_result(
                memo, fired=holds, fire_text=f"{cond['until']} on {self.pr['head'][:7]}" if holds else None,
                head=self.pr["head"], merge_state=self.pr["merge_state"], base="main",
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
        self.assertEqual(self.pins_path.read_text(), f'[\n\t"{SID}"\n]')
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
        self.assertFalse(self.run_cli("done", "--pane", "4")["pinned"])

    def test_done_on_the_later_wait_unpins_and_keeps_the_rest(self) -> None:
        self.seed("first", "second")
        wait = self.add_answer("--kind", "later")["wait"]["id"]
        self.seed("first", SID, "second")
        answer = self.run_cli("done", "--session", SID, "--wait", wait)
        self.assertFalse(answer["pinned"])
        self.assertEqual(self.pins(), ["first", "second"])
        self.assertEqual(answer["view"]["sessions"][0]["session_id"], "first")
        self.assertEqual(len(answer["view"]["sessions"]), 2)

    def test_done_on_another_wait_keeps_the_pin(self) -> None:
        self.add_answer("--kind", "later")
        wait = self.add_answer("--kind", "time", "--in", "1h")["wait"]["id"]
        answer = self.run_cli("done", "--session", SID, "--wait", wait)
        self.assertTrue(answer["pinned"])
        self.assertEqual(self.pins(), [SID])

    def test_a_second_later_wait_keeps_the_pin(self) -> None:
        first = self.add_answer("--kind", "later")["wait"]["id"]
        second = self.add_answer("--kind", "later")["wait"]["id"]
        self.assertTrue(self.run_cli("done", "--session", SID, "--wait", first)["pinned"])
        self.assertFalse(self.run_cli("done", "--session", SID, "--wait", second)["pinned"])
        self.assertEqual(self.pins(), [])

    def test_done_without_a_wait_unpins_a_pinned_session(self) -> None:
        self.seed("first", SID)
        self.add_answer("--kind", "time", "--in", "1h")
        self.assertFalse(self.run_cli("done", "--session", SID)["pinned"])
        self.assertEqual(self.pins(), ["first"])

    def test_done_with_no_record_unpins_too(self) -> None:
        self.seed(SID)
        self.assertFalse(self.run_cli("done", "--session", SID)["pinned"])
        self.assertEqual(self.pins(), [])

    def test_done_on_an_unpinned_session_writes_nothing(self) -> None:
        self.assertFalse(self.run_cli("done", "--session", SID)["pinned"])
        self.assertFalse(self.pins_path.exists())

    def test_a_held_lock_leaves_the_record_and_reports_the_pin(self) -> None:
        self.seed("first")
        self.hold_lock()
        answer = self.add_answer("--kind", "later", "--note", "why")
        self.assertFalse(answer["pinned"])
        self.assertIn("locked", answer["pin_error"])
        self.assertEqual(self.pins(), ["first"])
        self.assertEqual(self.session()["note"], "why")

    def test_a_held_lock_only_postpones_the_unpin_on_done(self) -> None:
        wait = self.add_answer("--kind", "later")["wait"]["id"]
        self.hold_lock()
        answer = self.run_cli("done", "--session", SID, "--wait", wait)
        self.assertTrue(answer["pinned"])
        self.assertIn("pin_error", answer)
        self.assertEqual(self.state()["sessions"][SID]["waits"][0]["state"], "done")

    def test_a_held_lock_does_not_matter_when_nothing_changes(self) -> None:
        self.seed(SID)
        self.hold_lock()
        self.assertNotIn("pin_error", self.add_answer("--kind", "later"))

    def test_a_held_lock_is_no_error_when_done_has_nothing_to_unpin(self) -> None:
        self.hold_lock()
        answer = self.run_cli("done", "--session", SID)
        self.assertFalse(answer["pinned"])
        self.assertNotIn("pin_error", answer)

    def test_a_pin_is_written_under_the_state_lock(self) -> None:
        real = waiting._replace_pins
        held: list[bool] = []

        def write(path: Path, rows: list) -> None:
            descriptor = os.open(self.state_dir / "waiting.lock", os.O_RDWR)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held.append(False)
            except BlockingIOError:
                held.append(True)
            finally:
                os.close(descriptor)
            real(path, rows)

        with mock.patch.object(waiting, "_replace_pins", write):
            wait = self.add_answer("--kind", "later")["wait"]["id"]
            self.run_cli("done", "--session", SID, "--wait", wait)
        self.assertEqual(held, [True, True])

    def test_the_attempt_under_the_state_lock_stays_short(self) -> None:
        self.assertLessEqual(waiting.PIN_LOCK_TRIES * waiting.PIN_LOCK_DELAY_S, 1)

    def test_done_after_a_failed_pin_stays_unpinned_for_good(self) -> None:
        holder = self.hold_lock()
        wait = self.add_answer("--kind", "later")["wait"]["id"]
        self.assertTrue(self.pin_wanted())
        self.assertNotIn("pin_error", self.run_cli("done", "--session", SID, "--wait", wait))
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
        self.run_cli("done", "--session", SID, "--wait", wait)
        self.assertIs(self.pin_wanted(), False)
        holder.close()
        self.tick()
        self.assertEqual(self.pins(), ["first"])
        self.assertIsNone(self.pin_wanted())

    def test_tick_never_pins_a_session_whose_later_wait_is_done(self) -> None:
        wait = self.add_answer("--kind", "later")["wait"]["id"]
        self.run_cli("done", "--session", SID, "--wait", wait)
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
        self.run_cli("done", "--session", SID, "--wait", wait)
        holder.close()
        self.add_answer("--kind", "later")
        self.assertIsNone(self.pin_wanted())
        self.tick()
        self.assertEqual(self.pins(), [SID])

    def test_done_with_no_record_keeps_a_failed_unpin(self) -> None:
        self.seed(SID)
        holder = self.hold_lock()
        self.assertIn("pin_error", self.run_cli("done", "--session", SID))
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

    def test_a_command_line(self) -> None:
        self.assertEqual(
            waiting._parse_argv("curl -s 'http://x/y z' --retry 2"),
            ["curl", "-s", "http://x/y z", "--retry", "2"],
        )

    def test_a_command_line_that_only_looks_like_an_array(self) -> None:
        self.assertEqual(waiting._parse_argv("[ -f /tmp/x ]"), ["[", "-f", "/tmp/x", "]"])

    def test_errors_name_both_forms(self) -> None:
        for text in ("", "[]", "[1]", "ls 'open"):
            with self.subTest(text=text), self.assertRaises(waiting.BadInput) as caught:
                waiting._parse_argv(text)
            self.assertIn("JSON array", str(caught.exception))
            self.assertIn("command line", str(caught.exception))

    def test_a_command_line_runs_without_a_shell(self) -> None:
        marker = self.root / "ran"
        script = f"import sys; open({str(marker)!r}, 'w').write(sys.argv[1])"
        argv = shlex.join([sys.executable, "-c", script, "a|b"])
        wait = self.add("--kind", "command", "--argv", argv)
        self.assertEqual(wait["condition"]["argv"][-1], "a|b")
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
        self.run_cli("done", "--session", SID, "--wait", timer)
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


if __name__ == "__main__":
    unittest.main()

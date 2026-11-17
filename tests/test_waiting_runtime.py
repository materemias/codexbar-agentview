"""codexbar_waiting.py end to end: real state files, commands, verifiers and gh.

Most CLI calls run main() in this process: each is still a separate call that
reads and writes the state on disk, without a Python start. A call spawns the
real CLI where a separate process is the point: an event caller that must
return within a deadline, a tick that owns slow work after its caller exited,
or state that must persist from one process to the next.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from fakes import WINDOWS, fake_program, kill_tree


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "contents"
    / "scripts"
    / "codexbar_waiting.py"
)
sys.path.insert(0, str(SCRIPT_PATH.parent))
import codexbar_waiting as waiting  # noqa: E402  (after the path insert above)

SID = "01a1abf1-15c8-748b-b9fa-126ebc0b88e5"
CALLER_DEADLINE_S = 5
PROCESS_DEADLINE_S = 5


class WaitingRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        self.helper = self.root / "helper project"
        self.project = self.root / "pane project"
        self.bin = self.root / "bin"
        self.state_dir = self.root / "state" / "codexbar-agentview"
        for directory in (self.home, self.helper, self.project, self.bin, self.state_dir):
            directory.mkdir(parents=True)
        self.env = {
            "HOME": str(self.home),
            "PATH": str(self.bin),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "XDG_DATA_HOME": str(self.root / "data"),
            "OMP_AGENT_DIR": str(self.root / "omp"),
            "PI_CODING_AGENT_DIR": str(self.root / "pi"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "LANG": "C.UTF-8",
        }
        if WINDOWS:
            # Path.home(), and what Python and cmd.exe need to start at all.
            self.env.update(USERPROFILE=str(self.home), SYSTEMROOT=os.environ["SYSTEMROOT"],
                            COMSPEC=os.environ["COMSPEC"])
        # In-process calls, and what they start, see only the spawned CLI's environment.
        environment = mock.patch.dict(os.environ, self.env, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        # Tick after all creation timestamps, independent of second boundaries.
        self.now = datetime.now().astimezone().replace(microsecond=0) + timedelta(minutes=1)
        self.processes: list[subprocess.Popen] = []
        self.gates: list[tuple[Path, Path, Path]] = []
        self.addCleanup(self.stop_processes)

    def stop_processes(self) -> None:
        # The real command runner puts verifiers in their own process groups.
        # Release them even when an assertion interrupts the event or tick.
        for release, _, _ in self.gates:
            release.touch()
        for process in self.processes:
            try:
                process.communicate(timeout=PROCESS_DEADLINE_S)
            except subprocess.TimeoutExpired:
                kill_tree(process.pid)
                process.communicate(timeout=PROCESS_DEADLINE_S)
        for _, started, finished in self.gates:
            completed = set(finished.read_text().splitlines()) if finished.exists() else set()
            pids = started.read_text().splitlines() if started.exists() else []
            for pid in set(pids) - completed:
                kill_tree(int(pid))

    def start_cli(self, *argv: str) -> subprocess.Popen:
        process = subprocess.Popen(
            [sys.executable, str(SCRIPT_PATH), *argv],
            cwd=self.helper, env=self.env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True,
        )
        self.processes.append(process)
        return process

    def answer(
        self, process: subprocess.Popen, *, timeout: float = PROCESS_DEADLINE_S,
    ) -> dict:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.fail(
                f"waiting CLI exceeded {timeout} s; turn_finished must queue "
                "verification instead of executing it in the event caller"
            )
        self.assertEqual(process.returncode, 0, stdout + stderr)
        answer = json.loads(stdout)
        self.assertTrue(answer["ok"], answer)
        return answer

    def spawn(self, *argv: str) -> dict:
        return self.answer(self.start_cli(*argv))

    def cli(self, *argv: str, expect: int = 0, cwd: Path | None = None) -> dict:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.chdir(cwd or self.helper), contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            code = waiting.main(list(argv))
        self.assertEqual(code, expect, stdout.getvalue() + stderr.getvalue())
        answer = json.loads(stdout.getvalue())
        self.assertEqual(answer["ok"], expect == 0, answer)
        return answer

    def tick(self, seconds: int = 0) -> dict:
        now = self.now + timedelta(seconds=seconds)
        return self.cli("tick", "--now", now.isoformat())

    def state(self) -> dict:
        return json.loads((self.state_dir / "waiting.json").read_text(encoding="utf-8"))

    def saved_wait(self, wait_id: str, *, pane: int | None = None) -> dict:
        state = self.state()
        record = state["sessions"][SID] if pane is None else state["panes"][str(pane)]
        return next(wait for wait in record["waits"] if wait["id"] == wait_id)

    def live_session(self) -> None:
        (self.state_dir / "agents.json").write_text(json.dumps({
            "agents": [{
                "sessionId": SID, "provider": "omp", "cwd": str(self.project),
                "state": "idle", "ternPane": 41, "windowTitle": "fixture session",
            }],
            "history": [],
        }))
        self.cli("heartbeat", "--session", SID, "--pane", "41")

    def delivered_prompt(self, verify: dict, *extra: str) -> tuple[str, str]:
        self.live_session()
        wait = self.cli(
            "add", "--session", SID, "--kind", "time", "--at",
            (self.now - timedelta(seconds=1)).isoformat(),
            "--prompt", "run the fixture task", "--verify", json.dumps(verify),
            *extra, "--no-toast",
        )["wait"]["id"]
        following = self.cli(
            "add", "--session", SID, "--kind", "time", "--in", "1h",
            "--after", wait, "--no-toast",
        )["wait"]["id"]
        self.tick()
        prompt = self.cli("claim-prompts", "--session", SID)["prompts"][0]
        self.assertEqual(prompt["wait"], wait)
        self.cli("ack", "--action", prompt["action"], "--result", "delivered")
        return wait, following

    def assert_verification_queued(self, wait: str, following: str) -> None:
        saved = self.saved_wait(wait)
        self.assertEqual(saved["state"], "acting")
        self.assertTrue(saved["verify_pending"])
        self.assertIsNone(saved["verify_claim_until"], "the event must not hold a tick's claim")
        self.assertEqual(self.saved_wait(following)["state"], "pending")
        self.assertEqual(self.state()["sessions"][SID]["prompt_queue"], [])
        self.assertEqual(self.cli("claim-prompts", "--session", SID)["prompts"], [])

    def blocking_verifier(self, exit_code: int) -> tuple[dict, Path, Path, Path]:
        release, started, finished = (self.root / name for name in ("release", "started", "finished"))
        self.gates.append((release, started, finished))
        script = self.root / "verifier.py"
        script.write_text(
            "import os, sys, time\n"
            "from pathlib import Path\n"
            "release, started, finished = map(Path, sys.argv[1:4])\n"
            "pid = str(os.getpid()) + '\\n'\n"
            "with started.open('a') as stream:\n"
            "    stream.write(pid)\n"
            "deadline = time.monotonic() + 60\n"
            "while not release.exists() and time.monotonic() < deadline:\n"
            "    time.sleep(0.01)\n"
            "with finished.open('a') as stream:\n"
            "    stream.write(pid)\n"
            "if not release.exists():\n"
            "    raise SystemExit(97)\n"
            "raise SystemExit(int(sys.argv[4]))\n"
        )
        verify = {
            "type": "command",
            "argv": [sys.executable, str(script), str(release), str(started), str(finished), str(exit_code)],
        }
        return verify, release, started, finished

    def wait_for_verifier(self, marker: Path, process: subprocess.Popen) -> None:
        deadline = time.monotonic() + PROCESS_DEADLINE_S
        while time.monotonic() < deadline:
            if marker.exists() and marker.read_text().strip():
                return
            if process.poll() is not None:
                break
            time.sleep(0.01)
        self.fail("tick did not start the durable pending verifier")

    def check_deferred_command_verification(self, exit_code: int) -> None:
        verify, release, started, finished = self.blocking_verifier(exit_code)
        wait, following = self.delivered_prompt(verify)
        # Spawned: an event that ran the verifier would block, and the deadline fails it at once.
        event = self.start_cli("event", "--type", "turn_finished", "--session", SID)
        answer = self.answer(event, timeout=CALLER_DEADLINE_S)
        self.assertEqual(answer["view"]["sessions"][0]["waits"][0]["state"], "acting")
        self.assertFalse(started.exists(), "the event ran the command before a tick")
        self.assertFalse(finished.exists())
        self.assert_verification_queued(wait, following)

        # A different process owns the slow work after the event caller has exited.
        tick = self.start_cli("tick", "--now", self.now.isoformat())
        self.wait_for_verifier(started, tick)
        self.assertEqual(self.saved_wait(wait)["state"], "acting")
        self.assertTrue(self.saved_wait(wait)["verify_pending"])
        self.assertIsNotNone(self.saved_wait(wait)["verify_claim_until"])
        # Spawned too: a second run of the verifier would block until the release.
        self.spawn("tick", "--now", self.now.isoformat())
        self.assertEqual(len(started.read_text().splitlines()), 1, "concurrent ticks ran the verifier twice")
        release.touch()
        self.answer(tick)
        self.assertEqual(finished.read_text().splitlines(), started.read_text().splitlines())
        saved = self.saved_wait(wait)
        self.assertFalse(saved["verify_pending"])
        if exit_code == 0:
            self.assertEqual(saved["state"], "done")
            self.assertEqual(self.saved_wait(following)["state"], "waiting")
            visible = self.cli("list")["sessions"][0]["waits"]
            self.assertEqual([item["id"] for item in visible], [following])
        else:
            self.assertEqual(saved["state"], "needs_attention")
            self.assertIn("verify command does not hold", saved["reason"])
            self.assertEqual(self.saved_wait(following)["state"], "pending")
            self.assertEqual(self.cli("list")["sessions"][0]["ball"], "on_me")
        self.cli("event", "--type", "turn_finished", "--session", SID)
        self.tick()
        self.assertEqual(len(started.read_text().splitlines()), 1)
        self.assertEqual(self.cli("claim-prompts", "--session", SID)["prompts"], [])

    def test_turn_finished_defers_command_verification_until_tick_success(self) -> None:
        self.check_deferred_command_verification(0)

    def test_turn_finished_defers_command_verification_until_tick_failure(self) -> None:
        self.check_deferred_command_verification(1)

    def test_turn_finished_does_not_query_github_before_tick(self) -> None:
        calls = self.root / "gh-calls.jsonl"
        response = {"data": {
            "viewer": {"login": "fixture-user"},
            "repository": {"pullRequest": {
                "state": "MERGED", "headRefOid": "head123", "baseRefName": "main",
                "mergeCommit": {"oid": "merge123"}, "mergedAt": self.now.isoformat(),
            }},
        }}
        fake_program(self.bin / "gh", (
            "import json, sys\n"
            "from pathlib import Path\n"
            f"with Path({str(calls)!r}).open('a') as stream:\n"
            "    stream.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "if sys.argv[1:3] == ['api', 'rate_limit']:\n"
            "    print('5000')\n"
            "elif sys.argv[1:3] == ['api', 'graphql']:\n"
            f"    print({json.dumps(response)!r})\n"
            "else:\n"
            "    raise SystemExit('unexpected fixture gh request')\n"
        ))
        wait, following = self.delivered_prompt({
            "type": "pr", "repo": "fixture/repo", "number": 7, "until": "merged",
        })
        self.cli("event", "--type", "turn_finished", "--session", SID)
        self.assertFalse(calls.exists(), "turn_finished queried GitHub instead of leaving work for tick")
        self.assert_verification_queued(wait, following)
        self.tick()
        self.assertEqual(self.saved_wait(wait)["state"], "done")
        self.assertEqual(self.saved_wait(following)["state"], "waiting")
        self.assertGreaterEqual(len(calls.read_text().splitlines()), 2)

    def status_argv(self) -> list[str]:
        # Relative files deliberately differ between the pane and helper projects.
        return [sys.executable, "-c", (
            "import json; from pathlib import Path; "
            "status = Path('status').read_text().strip(); "
            "Path('checked').write_text(str(Path.cwd())); "
            "print(json.dumps({'status': status}))"
        )]

    def project_statuses(self) -> None:
        (self.project / "status").write_text("pending")
        (self.helper / "status").write_text("ready")

    def add_status_check(self, *target: str, cwd: Path | None = None) -> str:
        """A wait on a command that fires once ./status reads ready."""
        return self.cli(
            "add", *target, "--kind", "command", "--argv", json.dumps(self.status_argv()),
            "--predicate", 'json_eq:status="ready"', "--interval", "1s", "--no-toast", cwd=cwd,
        )["wait"]["id"]

    def assert_failed_without_running(self, wait: str, first: dict, *, pane: int | None = None) -> None:
        """The first tick counted the command as failed: run nowhere, fired nothing."""
        saved = self.saved_wait(wait, pane=pane)
        self.assertEqual(saved["state"], "waiting", "the command fired on the helper project's ready file")
        self.assertEqual(saved["errors"], 1, "a missing saved cwd is a command error, not a new execution context")
        self.assertIsNone(saved["fired_at"])
        self.assertEqual(first["actions"], [])
        self.assertFalse((self.helper / "checked").exists(), "the command ran in the helper project")

    def gave_up_after_five_failures(self, wait: str, *, pane: int | None = None) -> dict:
        for seconds in range(1, 5):
            self.tick(seconds=seconds)
        saved = self.saved_wait(wait, pane=pane)
        self.assertEqual(saved["state"], "needs_attention")
        self.assertEqual(saved["errors"], 5)
        self.assertIn("Command failed 5 times", saved["reason"])
        self.assertIsNone(saved["fired_at"])
        self.assertFalse((self.helper / "checked").exists())
        return saved

    def test_pane_command_persists_explicit_cwd_across_cli_processes(self) -> None:
        self.project_statuses()
        wait = self.spawn(
            "add", "--pane", "72", "--kind", "command", "--cwd", str(self.project),
            "--argv", json.dumps(self.status_argv()), "--predicate", 'json_eq:status="ready"',
            "--interval", "1s", "--no-toast",
        )["wait"]["id"]
        first = self.tick()
        self.assertEqual(first["view"]["panes"][0]["waits"][0]["state"], "waiting")
        self.assertEqual(self.saved_wait(wait, pane=72)["condition"]["cwd"], str(self.project))
        self.assertEqual((self.project / "checked").read_text(), str(self.project))
        self.assertFalse((self.helper / "checked").exists())
        self.assertEqual(first["actions"], [])
        (self.project / "status").write_text("ready")
        self.tick(seconds=1)
        self.assertEqual(self.saved_wait(wait, pane=72)["state"], "fired")
        self.assertFalse((self.helper / "checked").exists())

    def test_explicit_cwd_applies_to_release_commands(self) -> None:
        self.project_statuses()
        fake_program(self.bin / "gh", (
            "import sys\n"
            "from pathlib import Path\n"
            "if sys.argv[1:] != ['api', 'repos/fixture/repo/releases/latest', '--jq', '.tag_name']:\n"
            "    raise SystemExit('unexpected fixture gh request')\n"
            "Path('checked').write_text(str(Path.cwd()))\n"
            "print(Path('status').read_text().strip())\n"
        ))
        wait = self.cli(
            "add", "--pane", "72", "--kind", "release", "--repo", "fixture/repo",
            "--cwd", str(self.project), "--interval", "1s", "--no-toast",
        )["wait"]["id"]
        self.tick()
        self.assertEqual(self.saved_wait(wait, pane=72)["state"], "waiting")
        self.assertEqual((self.project / "checked").read_text(), str(self.project))
        self.assertFalse((self.helper / "checked").exists())
        (self.project / "status").write_text("v2")
        self.tick(seconds=1)
        self.assertEqual(self.saved_wait(wait, pane=72)["state"], "fired")
        self.assertFalse((self.helper / "checked").exists())

    def test_explicit_cwd_overrides_session_facts_for_command_verification(self) -> None:
        self.project_statuses()
        declared = self.root / "explicit verification project"
        declared.mkdir()
        (declared / "status").write_text("ready")
        verify = {
            "type": "command", "argv": self.status_argv(),
            "predicate": 'json_eq:status="ready"',
        }
        wait, following = self.delivered_prompt(verify, "--cwd", str(declared))
        self.cli("event", "--type", "turn_finished", "--session", SID)
        self.assertFalse((declared / "checked").exists())
        self.tick()
        self.assertEqual(self.saved_wait(wait)["state"], "done")
        self.assertEqual(self.saved_wait(following)["state"], "waiting")
        self.assertEqual((declared / "checked").read_text(), str(declared))
        self.assertFalse((self.project / "checked").exists())
        self.assertFalse((self.helper / "checked").exists())

    def test_missing_saved_session_cwd_never_falls_back_to_helper_project(self) -> None:
        self.project_statuses()
        self.live_session()
        wait = self.add_status_check("--session", SID)
        self.assertEqual(self.saved_wait(wait)["condition"]["cwd"], str(self.project))
        moved = self.root / "renamed project"
        self.project.rename(moved)
        self.assert_failed_without_running(wait, self.tick())
        self.assertFalse((moved / "checked").exists())
        saved = self.gave_up_after_five_failures(wait)
        self.assertIn(str(self.project), saved["reason"])
        self.assertEqual(self.cli("list")["sessions"][0]["outcome"], "attention")

    def test_session_cwd_missing_before_registration_never_uses_helper_project(self) -> None:
        self.project_statuses()
        self.live_session()
        moved = self.root / "renamed project"
        self.project.rename(moved)
        wait = self.add_status_check("--session", SID)
        self.assertEqual(self.saved_wait(wait)["condition"]["cwd"], str(self.project))
        self.assert_failed_without_running(wait, self.tick())
        self.assertFalse((moved / "checked").exists())

    def test_explicit_cwd_rejects_empty_relative_missing_and_file_paths(self) -> None:
        regular_file = self.root / "not a directory"
        regular_file.write_text("fixture")
        (self.helper / "relative").mkdir()
        for cwd in ("", "relative", str(self.root / "missing"), str(regular_file)):
            with self.subTest(cwd=cwd):
                answer = self.cli(
                    "add", "--pane", "72", "--kind", "command", f"--cwd={cwd}",
                    "--argv", json.dumps(self.status_argv()), expect=2,
                )
                self.assertIn("cwd", answer["error"].lower())
                self.assertNotIn("unrecognized arguments", answer["error"])
                self.assertFalse((self.state_dir / "waiting.json").exists())
                self.assertFalse((self.helper / "checked").exists())

    def test_pane_command_without_cwd_captures_registration_directory(self) -> None:
        self.project_statuses()
        wait = self.add_status_check("--pane", "72", cwd=self.project)
        first = self.tick()
        saved = self.saved_wait(wait, pane=72)
        self.assertEqual(
            saved["state"], "waiting",
            "a pane check without --cwd fired on the tick helper's ready file",
        )
        self.assertEqual(saved["errors"], 0)
        self.assertEqual(saved["condition"]["cwd"], str(self.project))
        self.assertEqual((self.project / "checked").read_text(), str(self.project))
        self.assertFalse((self.helper / "checked").exists())
        self.assertEqual(first["actions"], [])
        (self.project / "status").write_text("ready")
        self.tick(seconds=1)
        self.assertEqual(self.saved_wait(wait, pane=72)["state"], "fired")
        self.assertFalse((self.helper / "checked").exists())

    def test_legacy_pane_command_without_saved_cwd_reports_error_without_running(self) -> None:
        self.project_statuses()
        wait = self.add_status_check("--pane", "72", "--cwd", str(self.project))
        # Older versions legitimately persisted pane commands without a cwd.
        # No writer is running while this isolated fixture becomes that state.
        state = self.state()
        condition = next(
            item["condition"] for item in state["panes"]["72"]["waits"] if item["id"] == wait
        )
        del condition["cwd"]
        (self.state_dir / "waiting.json").write_text(json.dumps(state))

        self.assert_failed_without_running(wait, self.tick(), pane=72)
        self.assertFalse((self.project / "checked").exists())
        saved = self.gave_up_after_five_failures(wait, pane=72)
        self.assertIn("cwd", saved["reason"].casefold())
        self.assertIn("recreate", saved["reason"].casefold())
        self.assertEqual(self.cli("list")["panes"][0]["outcome"], "attention")
        self.assertFalse((self.project / "checked").exists())

    def test_legacy_command_verification_without_saved_cwd_never_uses_helper_project(self) -> None:
        self.project_statuses()
        wait, following = self.delivered_prompt({
            "type": "command", "argv": self.status_argv(),
            "predicate": 'json_eq:status="ready"',
        })
        state = self.state()
        verify = next(
            item["verify"] for item in state["sessions"][SID]["waits"] if item["id"] == wait
        )
        del verify["cwd"]
        (self.state_dir / "waiting.json").write_text(json.dumps(state))

        self.cli("event", "--type", "turn_finished", "--session", SID)
        self.assert_verification_queued(wait, following)
        self.tick()
        saved = self.saved_wait(wait)
        self.assertEqual(
            saved["state"], "needs_attention",
            "a legacy verifier without a saved cwd completed on the helper project's ready file",
        )
        self.assertFalse(saved["verify_pending"])
        self.assertIn("cwd", saved["reason"].casefold())
        self.assertIn("recreate", saved["reason"].casefold())
        self.assertEqual(self.saved_wait(following)["state"], "pending")
        self.assertEqual(self.cli("list")["sessions"][0]["outcome"], "attention")
        self.assertFalse((self.helper / "checked").exists())
        self.assertFalse((self.project / "checked").exists())

    def test_described_check_runs_in_the_session_project_only_once_written(self) -> None:
        self.project_statuses()
        self.live_session()
        wait = self.cli(
            "add", "--session", SID, "--kind", "command", "--describe", "the status is ready",
            "--interval", "1s", "--no-toast",
        )["wait"]["id"]
        self.tick()
        self.assertEqual(self.saved_wait(wait)["state"], "pending")
        self.assertEqual(self.saved_wait(wait)["condition"]["cwd"], str(self.project))
        self.assertFalse((self.project / "checked").exists(), "the check ran before its command was written")
        prompt = self.cli("claim-prompts", "--session", SID)["prompts"][0]
        self.assertTrue(prompt["check"])
        self.cli("ack", "--action", prompt["action"], "--result", "delivered")
        argv = [sys.executable, "-c", (
            "from pathlib import Path; Path('checked').write_text(str(Path.cwd())); "
            "raise SystemExit(Path('status').read_text().strip() != 'ready')"
        )]
        self.cli("write-check", "--session", SID, "--argv", json.dumps(argv))
        self.cli("event", "--type", "turn_finished", "--session", SID)
        self.tick(seconds=1)
        self.assertEqual(self.saved_wait(wait)["state"], "waiting")
        self.assertEqual((self.project / "checked").read_text(), str(self.project))
        self.assertFalse((self.helper / "checked").exists())
        (self.project / "status").write_text("ready")
        self.tick(seconds=2)
        self.assertEqual(self.saved_wait(wait)["state"], "fired")


if __name__ == "__main__":
    unittest.main()

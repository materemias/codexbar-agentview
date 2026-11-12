from __future__ import annotations

from contextlib import ExitStack
import fcntl
import io
import json
import os
import sys
import runpy
import socket
import subprocess
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "contents" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import codexbar_focus as focus  # noqa: E402

_PGREP = mock.patch.object(focus.agents, "_pgrep", return_value=[])


def setUpModule() -> None:
    # launch() looks for processes writing the session; keep the real ones out.
    _PGREP.start()


def tearDownModule() -> None:
    _PGREP.stop()


def entry(**changes: object) -> dict:
    record = {
        "provider": "omp",
        "sessionId": "sid-1",
        "cwd": "/work/project",
        "host": "kitty",
        "desktop": "3",
        "closedBy": "exit",
        "resumeCommand": "cd -- /work/project && omp --resume sid-1",
    }
    record.update(changes)
    return record


class T3LinkTests(unittest.TestCase):
    def test_link_maps_native_sessions_to_t3_threads(self) -> None:
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "environment-id").write_text("env-1\n")

            def native(sid: str) -> str:
                return json.dumps({"nativeThreadRef": {"nativeId": sid}})

            with sqlite3.connect(root / "statev2.sqlite") as db:
                db.execute("CREATE TABLE orchestration_v2_projection_provider_threads"
                           " (thread_id TEXT, updated_at TEXT, payload_json TEXT)")
                db.executemany(
                    "INSERT INTO orchestration_v2_projection_provider_threads VALUES (?, ?, ?)", [
                        ("t-old", "1", native("claude-sid")),
                        ("t-claude", "2", native("claude-sid")),
                        ("t-codex", "1", native("codex-sid")),
                        (None, "3", native("codex-sid")),
                        ("t-pending", "1", json.dumps({"nativeThreadRef": None})),
                    ])
            db.close()
            with mock.patch.object(focus, "T3_DIR", root):
                self.assertEqual(focus._t3_thread_link("claude-sid"), "t3code://threads/env-1/t-claude")
                self.assertEqual(focus._t3_thread_link("codex-sid"), "t3code://threads/env-1/t-codex")
                self.assertEqual(focus._t3_thread_link("other"), "")
            with mock.patch.object(focus, "T3_DIR", root / "missing"):
                self.assertEqual(focus._t3_thread_link("claude-sid"), "")


class LaunchTests(unittest.TestCase):
    def launch(self, payload: dict, desktops: int = 6):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agents.json"
            path.write_text(json.dumps(payload))
            popen = mock.MagicMock()
            popen.return_value.pid = 4242
            popen.return_value.poll.return_value = None
            stderr = io.StringIO()
            with (
                mock.patch.object(focus, "AGGREGATE_PATH", path),
                mock.patch.object(focus.subprocess, "Popen", popen),
                mock.patch.object(focus, "_desktop_count", return_value=desktops),
                mock.patch.object(focus, "_kwin_place", return_value=True) as place,
                mock.patch.object(focus, "_set_current_desktop") as switch,
                mock.patch.object(focus.sys, "platform", "linux"),
                mock.patch.object(focus, "USE_KWIN", True),
                mock.patch.object(focus.agents, "tern_bin", return_value="tern"),
                mock.patch.dict(focus.os.environ, {"TERN_DAEMON_SOCKET": ""}),
                mock.patch.object(focus.sys, "stderr", stderr),
            ):
                code = focus.launch("omp", "sid-1")
        return code, popen, place, switch, stderr.getvalue()

    def test_session_a_process_outside_the_saved_list_may_write_is_refused(self) -> None:
        # A headless resume, or a host the sweep doesn't list: one holds the
        # transcript open, the other holds none in the record's folder.
        for held, cwd, expected, message in (
            ("/s/-work/2026_sid-1.jsonl", "/elsewhere", 5, "already running"),
            ("", "/work/project", 11, "may be writing this session"),
        ):
            with (
                self.subTest(message),
                mock.patch.object(focus.agents, "_pgrep", side_effect=lambda _n, services=False:
                                  [] if services else [77]),
                mock.patch.object(focus.agents, "_open_jsonl_under", return_value=(held, bool(held))),
                mock.patch.object(focus.agents, "_cwd_of", return_value=cwd),
                # Off the real /proc and ~/.omp/logs: no argv or log names a session.
                mock.patch.object(focus.agents, "_argv_of", return_value=["omp"]),
                mock.patch.object(focus.agents, "_omp_log_files", return_value={}),
            ):
                code, popen, *_rest, stderr = self.launch({"agents": [], "history": [entry()]})
            self.assertEqual(code, expected)
            popen.assert_not_called()
            self.assertIn(message, stderr)

    def test_verified_kitty_record_switches_desktop_then_launches_in_scope(self) -> None:
        code, popen, place, switch, _ = self.launch({"agents": [], "history": [entry()]})
        self.assertEqual(code, 0)
        argv = popen.call_args.args[0]
        self.assertEqual(argv[:4], ["systemd-run", "--user", "--scope", "--quiet"])
        kitty = argv.index("kitty")
        self.assertEqual(argv[kitty + 1:kitty + 3], ["--directory", "/work/project"])
        self.assertIn("omp --resume sid-1", argv[-1])
        switch.assert_called_once_with(3)
        place.assert_called_once_with(4242, "3")

    def test_refusals_never_spawn_or_switch(self) -> None:
        cases = {
            5: {"agents": [{"provider": "omp", "sessionId": "sid-1"}],
                "history": [entry()]},
            2: {"agents": [], "history": []},
            6: {"agents": [], "history": [entry(resumeCommand="rm -rf /")]},
            7: {"agents": [], "history": [entry(host="konsole")]},
        }
        for expected, payload in cases.items():
            with self.subTest(expected=expected):
                code, popen, place, switch, _ = self.launch(payload)
                self.assertEqual(code, expected)
                popen.assert_not_called()
                place.assert_not_called()
                switch.assert_not_called()

    def test_missing_desktop_opens_on_current_and_reports_it(self) -> None:
        code, popen, place, switch, err = self.launch(
            {"agents": [], "history": [entry(desktop="9")]}, desktops=6
        )
        self.assertEqual(code, 0)
        popen.assert_called_once()
        place.assert_not_called()
        switch.assert_not_called()
        self.assertIn("desktop 9 no longer exists", err)

    def tern_launch(self, daemon: bool = True, window: bool = True):
        calls = []
        window_checks = []

        def tern(args, socket=""):
            calls.append(args)
            ok = args[0] != "ls" or daemon or len(calls) > 2
            out = '{"session": 4, "tab": 8, "block": 9}' if args[0] == "new" else ""
            return focus.subprocess.CompletedProcess(args, 0 if ok else 1, out, "")

        def windows():
            window_checks.append(1)
            return [77] if window or len(window_checks) > 1 else []

        with (
            mock.patch.object(focus, "_tern", side_effect=tern),
            mock.patch.object(focus.time, "sleep"),
            mock.patch.object(focus.agents, "tern_window_pids", side_effect=windows),
            mock.patch.object(focus, "_kwin_activate", return_value=True) as activate,
        ):
            code, popen, place, switch, _ = self.launch(
                {"agents": [], "history": [entry(host="tern")]}
            )
        return code, popen, place, switch, activate, calls

    def test_tern_record_opens_a_tab_in_running_tern_and_raises_it(self) -> None:
        code, popen, place, switch, activate, calls = self.tern_launch()
        self.assertEqual(code, 0)
        popen.assert_not_called()
        switch.assert_not_called()
        place.assert_not_called()
        tab, shown = calls[-2:]
        self.assertEqual(tab[:5], ["new", "tab", "--json", "--cwd", "/work/project"])
        self.assertIn("omp --resume sid-1", tab[-1])
        self.assertEqual(shown, ["focus", "9"])
        activate.assert_called_once_with([77])

    def test_tern_record_starts_tern_first_when_none_runs(self) -> None:
        code, popen, _, _, _, calls = self.tern_launch(daemon=False)
        self.assertEqual(code, 0)
        self.assertEqual(popen.call_args.args[0][-2:], ["--", "tern"])
        self.assertEqual(calls[-2][:2], ["new", "tab"])

    def test_tern_daemon_without_window_opens_a_window_before_the_tab(self) -> None:
        code, popen, _, _, _, calls = self.tern_launch(window=False)
        self.assertEqual(code, 0)
        self.assertEqual(popen.call_args.args[0][-2:], ["--", "tern"])
        self.assertEqual(calls[-2][:2], ["new", "tab"])

    def test_launch_all_restores_reboot_rows_in_desktop_order(self) -> None:
        def row(sid: str, **changes: object) -> dict:
            record = entry(sessionId=sid, closedBy="reboot",
                           resumeCommand=f"cd -- /work/project && omp --resume {sid}")
            record.update(changes)
            return record
        payload = {
            "agents": [{"provider": "omp", "sessionId": "live"}],
            "history": [
                row("unknown", desktop=""), row("d5", desktop="5"),
                row("d2", desktop="2"), row("live", desktop="1"),
                row("konsole", desktop="1", host="konsole"),
                row("no-cmd", desktop="1", resumeCommand=""),
                entry(sessionId="exit-row", desktop="1", closedBy="exit",
                      resumeCommand="cd -- /work/project && omp --resume exit-row"),
                row("hidden", desktop="1"),
            ],
        }
        requested = {("omp", sid) for sid in
                     ("unknown", "d5", "d2", "live", "konsole", "no-cmd", "exit-row")}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agents.json"
            path.write_text(json.dumps(payload))
            stdout = io.StringIO()
            with (
                mock.patch.object(focus, "AGGREGATE_PATH", path),
                mock.patch.object(focus, "launch",
                                  side_effect=lambda _p, sid: 8 if sid == "d5" else 0) as launch,
                mock.patch.object(focus.sys, "stdout", stdout),
            ):
                code = focus.launch_all(requested)
        self.assertEqual(code, 0)
        # Unrequested rows never launch, and a failed launch is not reported.
        self.assertEqual([c.args[1] for c in launch.call_args_list],
                         ["d2", "d5", "unknown"])
        self.assertEqual(json.loads(stdout.getvalue()),
                         [["omp", "d2"], ["omp", "unknown"]])


class SessionOperationOwnershipTests(unittest.TestCase):
    # Each helper imports the production CLI afresh. Only terminal dispatch
    # and process inspection/signalling are replaced: no daemon or agent runs.
    WORKER = textwrap.dedent(r"""
        import json
        import os
        from pathlib import Path
        import socket
        import subprocess
        import sys
        import time
        from contextlib import ExitStack
        from unittest import mock

        scripts, directory, action, mode, provider, descriptor = sys.argv[1:]
        sys.path.insert(0, scripts)
        import codexbar_focus as focus

        root = Path(directory)
        channel = socket.socket(fileno=int(descriptor))
        channel.settimeout(10)

        def event(value):
            data = (json.dumps(value) + "\n").encode()
            fd = os.open(root / "events.jsonl",
                         os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)

        def terminal(args, socket=""):
            if args[:2] == ["new", "tab"]:
                if mode == "fail":
                    event({"kind": "failed-start"})
                    return subprocess.CompletedProcess(args, 1, "", "controlled startup failure")
                event({"kind": "dispatch", "action": action, "provider": provider, "argv": args})
                if mode == "crash":
                    os._exit(86)
                if mode == "accepted-timeout":
                    return None
                if mode == "hold":
                    channel.sendall(b"D")
                    if channel.recv(1) != b"G":
                        raise RuntimeError("dispatch was not released")
                return subprocess.CompletedProcess(args, 0, '{"block": 9}', "")
            return subprocess.CompletedProcess(args, 0, "", "")

        def stopped(pid, timeout):
            if mode == "writer-stuck":
                return False
            # Simulate the sweep observing the old writer's exit while the
            # resumed tab is still in startup and not discoverable.
            payload = json.loads(focus.AGGREGATE_PATH.read_text())
            payload["agents"] = []
            focus.AGGREGATE_PATH.write_text(json.dumps(payload))
            return True

        with ExitStack() as stack:
            if mode in ("expired", "unresolved-empty-cwd", "unresolved-same-cwd"):
                expired = time.monotonic() + focus.SESSION_START_RESERVATION_SECONDS + 1
                stack.enter_context(mock.patch.object(focus.time, "monotonic", return_value=expired))
            stack.enter_context(mock.patch.object(focus, "_tern_ready", return_value=True))
            stack.enter_context(mock.patch.object(focus, "_tern", side_effect=terminal))
            stack.enter_context(mock.patch.object(focus, "_user_shell", return_value="/bin/sh"))
            stack.enter_context(mock.patch.object(focus.agents, "_pgrep", return_value=[]))
            stack.enter_context(mock.patch.object(focus.agents, "tern_bin", return_value="/unused/tern"))
            # A listed writer runs unless the test says it exited after the scan.
            stack.enter_context(mock.patch.object(focus, "_exited", return_value=mode == "writer-exited"))
            if mode in ("unresolved-empty-cwd", "unresolved-same-cwd"):
                stack.enter_context(mock.patch.object(
                    focus.agents, "_pgrep",
                    side_effect=lambda _provider, services=False: [] if services else [424242]))
                stack.enter_context(mock.patch.object(focus.agents, "_resume_arg", return_value=""))
                stack.enter_context(mock.patch.object(focus.agents, "_shell_resume_arg", return_value=""))
                stack.enter_context(mock.patch.object(
                    focus.agents, "_cwd_of",
                    return_value=str(root) if mode == "unresolved-same-cwd" else ""))
                stack.enter_context(mock.patch.dict(
                    focus.agents._INFO_FN,
                    {provider: lambda pid: {"sessionId": "another-session", "identityExact": False}}))
            if action == "--teleport":
                info = {"sessionId": "same-session", "identityExact": True,
                        "state": "idle", "cwd": str(root)}
                stack.enter_context(mock.patch.dict(
                    focus.agents._INFO_FN, {provider: lambda pid: info}))
                stack.enter_context(mock.patch.object(
                    focus.agents, "_pgrep",
                    side_effect=lambda _provider, services=False: [] if services else [424242]))
                stack.enter_context(mock.patch.object(focus.agents, "_resume_arg", return_value=""))
                stack.enter_context(mock.patch.object(focus.agents, "_held_rollouts", return_value=set()))
                stack.enter_context(mock.patch.object(focus.agents, "_codex_home", return_value=str(root)))
                stack.enter_context(mock.patch.object(focus.agents, "_cwd_of", return_value=str(root)))
                stack.enter_context(mock.patch.object(
                    focus.agents, "_parent_walk_for_host", return_value=("kitty", [])))
                stack.enter_context(mock.patch.object(focus.agents, "_ppid_of", return_value=0))
                stack.enter_context(mock.patch.object(focus.agents, "_comm_of", return_value=""))
                stack.enter_context(mock.patch.object(
                    focus, "_signal", side_effect=lambda pid, sig:
                    event({"kind": "signal", "pid": pid, "signal": sig})))
                stack.enter_context(mock.patch.object(focus, "_wait_exit", side_effect=stopped))
            channel.sendall(b"R")
            if channel.recv(1) != b"G":
                raise RuntimeError("helper was not started")
            channel.sendall(b"C")
            code = focus.main(["--tern", action, provider, "same-session"])
        channel.close()
        sys.exit(code)
    """)

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.env = {
            "HOME": str(self.root / "home"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "XDG_DATA_HOME": str(self.root / "data"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
            "OMP_CONFIG_DIR": str(self.root / "omp"),
            "OMP_STATE_DIR": str(self.root / "omp-state"),
            "CODEX_HOME": str(self.root / "codex"),
            "CLAUDE_CONFIG_DIR": str(self.root / "claude"),
            "PATH": "",
        }
        for value in self.env.values():
            if value:
                Path(value).mkdir(parents=True, exist_ok=True)
        self.path = Path(self.env["XDG_STATE_HOME"]) / "codexbar-agentview" / "agents.json"
        self.path.parent.mkdir()
        self.record = entry(
            provider="codex", sessionId="same-session", cwd=str(self.root),
            host="tern", desktop="",
            resumeCommand=focus.agents._resume_command(
                "codex", "same-session", str(self.root), True),
        )
        self.snapshot()

    def snapshot(self, live: bool = False) -> None:
        agents = ([{"provider": "codex", "sessionId": "same-session",
                    "pid": 424242, "host": "kitty"}] if live else [])
        self.path.write_text(json.dumps({"agents": agents, "history": [self.record]}))

    @staticmethod
    def stop_helper(process: subprocess.Popen, channel: socket.socket) -> None:
        channel.close()
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)

    def start_helper(self, action: str = "--launch", mode: str = "normal",
                     provider: str = "codex"):
        parent, child = socket.socketpair()
        parent.settimeout(10)
        try:
            process = subprocess.Popen(
                [sys.executable, "-I", "-B", "-c", self.WORKER, str(SCRIPTS),
                 str(self.root), action, mode, provider, str(child.fileno())],
                env=self.env, pass_fds=(child.fileno(),),
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True,
            )
        except BaseException:
            parent.close()
            raise
        finally:
            child.close()
        self.addCleanup(self.stop_helper, process, parent)
        self.assertEqual(parent.recv(1), b"R", "helper failed before becoming ready")
        parent.sendall(b"G")
        self.assertEqual(parent.recv(1), b"C", "helper was not released to call the production CLI")
        return process, parent

    def finish_helper(self, process: subprocess.Popen) -> tuple[int, str]:
        _stdout, stderr = process.communicate(timeout=15)
        return process.returncode, stderr

    def events(self) -> list[dict]:
        path = self.root / "events.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def dispatches(self) -> list[dict]:
        return [event for event in self.events() if event["kind"] == "dispatch"]

    def test_competing_launch_helpers_dispatch_one_resume(self) -> None:
        first, barrier = self.start_helper(mode="hold")
        self.assertEqual(barrier.recv(1), b"D")
        second, _ = self.start_helper()
        # The second caller is released to call main while the first is at
        # the startup boundary. It may refuse immediately or wait for it.
        barrier.sendall(b"G")
        first_result = self.finish_helper(first)
        second_result = self.finish_helper(second)
        self.assertEqual(first_result[0], 0, first_result[1])
        self.assertEqual(len(self.dispatches()), 1,
                         "competing helpers dispatched two writers for one session")
        self.assertEqual(second_result[0], 5, second_result[1])

    def test_completed_helper_keeps_reservation_until_writer_discovery(self) -> None:
        first, _ = self.start_helper()
        self.assertEqual(self.finish_helper(first)[0], 0)
        # No writer has appeared in agents.json yet. A process-local lock
        # released on CLI exit cannot protect this startup gap.
        self.assertEqual(json.loads(self.path.read_text())["agents"], [])
        second, _ = self.start_helper()
        result = self.finish_helper(second)
        self.assertEqual(len(self.dispatches()), 1,
                         "helper exit released ownership before writer discovery")
        self.assertEqual(result[0], 5, result[1])

    def test_a_reserve_and_a_launch_never_both_start_the_session(self) -> None:
        reserved, _ = self.start_helper(action="--reserve")
        self.assertEqual(self.finish_helper(reserved)[0], 0)
        launched, _ = self.start_helper()
        self.assertEqual(self.finish_helper(launched)[0], 5)
        self.assertEqual(self.dispatches(), [], "a launch dispatched while a reopen owned the startup")

    def test_a_reserve_after_a_launch_is_refused(self) -> None:
        launched, _ = self.start_helper()
        self.assertEqual(self.finish_helper(launched)[0], 0)
        reserved, _ = self.start_helper(action="--reserve")
        self.assertEqual(self.finish_helper(reserved)[0], 5)

    def test_a_reserve_refuses_a_running_session(self) -> None:
        self.snapshot(live=True)
        reserved, _ = self.start_helper(action="--reserve")
        self.assertEqual(self.finish_helper(reserved)[0], 5)

    def test_a_reserve_ignores_a_listed_writer_that_exited(self) -> None:
        # A stale scan still lists the session after its writer exited.
        self.snapshot(live=True)
        reserved, _ = self.start_helper(action="--reserve", mode="writer-exited")
        result = self.finish_helper(reserved)
        self.assertEqual(result[0], 0, result[1])

    def test_first_teleport_after_successful_launch_reconciles_discovered_writer(self) -> None:
        launched, _ = self.start_helper()
        result = self.finish_helper(launched)
        self.assertEqual(result[0], 0, result[1])
        self.snapshot(live=True)
        teleport, _ = self.start_helper(action="--teleport")
        result = self.finish_helper(teleport)
        self.assertEqual(result[0], 0, result[1])
        self.assertEqual([event["action"] for event in self.dispatches()],
                         ["--launch", "--teleport"])
        kinds = [event["kind"] for event in self.events()]
        self.assertEqual(kinds, ["dispatch", "signal", "dispatch"])
        self.assertEqual(json.loads(self.path.read_text())["agents"], [],
                         "teleport must observe the old writer exit before dispatch")

    def test_teleport_after_successful_launch_still_requires_old_writer_exit(self) -> None:
        launched, _ = self.start_helper()
        result = self.finish_helper(launched)
        self.assertEqual(result[0], 0, result[1])
        self.snapshot(live=True)
        teleport, _ = self.start_helper(action="--teleport", mode="writer-stuck")
        result = self.finish_helper(teleport)
        self.assertEqual(result[0], 10, result[1])
        self.assertIn("agent did not exit", result[1])
        self.assertEqual(len(self.dispatches()), 1)
        self.assertEqual([event["kind"] for event in self.events()],
                         ["dispatch", "signal", "signal"])
        self.assertTrue(json.loads(self.path.read_text())["agents"])

    def test_accepted_terminal_timeout_keeps_reservation_for_retry(self) -> None:
        ambiguous, _ = self.start_helper(mode="accepted-timeout")
        result = self.finish_helper(ambiguous)
        self.assertIn(result[0], (0, 8), result[1])
        self.assertEqual(len(self.dispatches()), 1)
        self.assertEqual(json.loads(self.path.read_text())["agents"], [])
        retry, _ = self.start_helper()
        result = self.finish_helper(retry)
        self.assertEqual(len(self.dispatches()), 1,
                         "an ambiguous terminal reply dispatched a second writer")
        self.assertEqual(result[0], 5, result[1])

    def test_expired_reservation_ignores_unresolved_process_without_cwd(self) -> None:
        self.record.update(
            provider="pi", resumeCommand=focus.agents._resume_command(
                "pi", "same-session", str(self.root), True))
        self.snapshot()
        launched, _ = self.start_helper(provider="pi")
        result = self.finish_helper(launched)
        self.assertEqual(result[0], 0, result[1])
        retry, _ = self.start_helper(provider="pi", mode="unresolved-empty-cwd")
        result = self.finish_helper(retry)
        self.assertEqual(result[0], 0, result[1])
        self.assertEqual(len(self.dispatches()), 2,
                         "a disappeared process with no cwd blocked an unrelated session")

    def test_expired_reservation_refuses_unresolved_process_in_same_cwd(self) -> None:
        self.record.update(
            provider="pi", resumeCommand=focus.agents._resume_command(
                "pi", "same-session", str(self.root), True))
        self.snapshot()
        launched, _ = self.start_helper(provider="pi")
        result = self.finish_helper(launched)
        self.assertEqual(result[0], 0, result[1])
        retry, _ = self.start_helper(provider="pi", mode="unresolved-same-cwd")
        result = self.finish_helper(retry)
        self.assertEqual(result[0], 11, result[1])
        self.assertEqual(len(self.dispatches()), 1,
                         "an unresolved writer in the same cwd must still prevent dispatch")

    def test_failed_terminal_start_releases_reservation_for_retry(self) -> None:
        failed, _ = self.start_helper(mode="fail")
        result = self.finish_helper(failed)
        self.assertEqual(result[0], 8, result[1])
        self.assertIn("controlled startup failure", result[1])
        self.assertEqual(self.dispatches(), [])
        retry, _ = self.start_helper()
        result = self.finish_helper(retry)
        self.assertEqual(result[0], 0, result[1])
        self.assertEqual(len(self.dispatches()), 1)

    def test_same_session_id_in_different_providers_has_independent_ownership(self) -> None:
        other = {
            **self.record, "provider": "omp",
            "resumeCommand": focus.agents._resume_command(
                "omp", "same-session", str(self.root), True),
        }
        self.path.write_text(json.dumps({"agents": [], "history": [self.record, other]}))
        codex, barrier = self.start_helper(mode="hold")
        self.assertEqual(barrier.recv(1), b"D")
        omp, _ = self.start_helper(provider="omp")
        # Unrelated ownership must not serialize startup behind Codex.
        omp_result = self.finish_helper(omp)
        barrier.sendall(b"G")
        codex_result = self.finish_helper(codex)
        self.assertEqual(omp_result[0], 0, omp_result[1])
        self.assertEqual(codex_result[0], 0, codex_result[1])
        self.assertEqual([event["provider"] for event in self.dispatches()], ["codex", "omp"])

    def test_teleport_and_launch_share_session_ownership(self) -> None:
        self.snapshot(live=True)
        teleport, barrier = self.start_helper(action="--teleport", mode="hold")
        self.assertEqual(barrier.recv(1), b"D")
        self.assertEqual(json.loads(self.path.read_text())["agents"], [])
        launch, _ = self.start_helper()
        barrier.sendall(b"G")
        teleport_result = self.finish_helper(teleport)
        launch_result = self.finish_helper(launch)
        self.assertEqual(teleport_result[0], 0, teleport_result[1])
        self.assertEqual(self.events()[0]["kind"], "signal")
        self.assertEqual(len(self.dispatches()), 1,
                         "launch raced the teleport between old-writer exit and discovery")
        self.assertEqual(self.dispatches()[0]["action"], "--teleport")
        self.assertEqual(launch_result[0], 5, launch_result[1])

    def test_crashed_dispatch_stays_reserved_then_retries_after_expiry(self) -> None:
        crashed, _ = self.start_helper(mode="crash")
        result = self.finish_helper(crashed)
        self.assertEqual(result[0], 86, result[1])
        self.assertEqual(len(self.dispatches()), 1)
        blocked, _ = self.start_helper()
        result = self.finish_helper(blocked)
        self.assertEqual(result[0], 5, result[1])
        self.assertEqual(len(self.dispatches()), 1)
        retry, _ = self.start_helper(mode="expired")
        result = self.finish_helper(retry)
        self.assertEqual(result[0], 0, result[1])
        self.assertEqual(len(self.dispatches()), 2)

    def test_expired_reservations_reconcile_every_provider_and_refuse_writers(self) -> None:
        cases = (
            ("codex", False, True), ("pi", False, True),
            ("opencode", False, False), ("codex", True, True),
        )
        for provider, service, exact in cases:
            with self.subTest(provider=provider, service=service):
                directory = self.root / f"{provider}-{service}"
                directory.mkdir()
                path = directory / "agents.json"
                record = {
                    **self.record, "provider": provider,
                    "resumeCommand": focus.agents._resume_command(
                        provider, "same-session", str(self.root), True),
                }
                path.write_text(json.dumps({"agents": [], "history": [record]}))
                with (
                    mock.patch.object(focus, "AGGREGATE_PATH", path),
                    mock.patch.object(focus, "_launch_tern", return_value=0) as dispatch,
                    mock.patch.object(focus.sys, "stderr", io.StringIO()),
                    mock.patch.object(focus.agents, "_held_rollouts", return_value=set()),
                    mock.patch.object(focus.agents, "_resume_arg", return_value=""),
                    mock.patch.object(focus.agents, "_shell_resume_arg", return_value=""),
                    mock.patch.object(focus.agents, "_codex_home", return_value=str(self.root)),
                    mock.patch.object(focus.agents, "_cwd_of", return_value=str(self.root)),
                ):
                    self.assertEqual(focus.launch(provider, "same-session"), 0)
                    expired = focus.time.monotonic() + focus.SESSION_START_RESERVATION_SECONDS + 1
                    info = {"sessionId": "same-session", "cwd": str(self.root),
                            "identityExact": exact}
                    with (
                        mock.patch.object(focus.time, "monotonic", return_value=expired),
                        mock.patch.object(
                            focus.agents, "_pgrep",
                            side_effect=lambda _provider, services=False: [77] if services == service else []),
                        mock.patch.dict(focus.agents._INFO_FN, {provider: lambda _pid: info}),
                    ):
                        self.assertEqual(focus.launch(provider, "same-session"), 5)
                    dispatch.assert_called_once()
                    # Discovery releases the startup reservation, but never
                    # dispatches. Once that writer exits, retry need not wait.
                    self.assertEqual(focus.launch(provider, "same-session"), 0)
                    self.assertEqual(dispatch.call_count, 2)

    def test_expired_codex_reservation_checks_every_server_rollout(self) -> None:
        with (
            mock.patch.object(focus, "AGGREGATE_PATH", self.path),
            mock.patch.object(focus, "_launch_tern", return_value=0) as dispatch,
            mock.patch.object(focus.sys, "stderr", io.StringIO()),
        ):
            self.assertEqual(focus.launch("codex", "same-session"), 0)
            expired = focus.time.monotonic() + focus.SESSION_START_RESERVATION_SECONDS + 1
            with (
                mock.patch.object(focus.time, "monotonic", return_value=expired),
                mock.patch.object(
                    focus.agents, "_pgrep",
                    side_effect=lambda _provider, services=False: [77] if services else []),
                mock.patch.object(focus.agents, "_codex_home", return_value=str(self.root)),
                mock.patch.object(focus.agents, "_held_rollouts", return_value={
                    str(self.root / "sessions" / "other.jsonl"),
                    str(self.root / "sessions" / "rollout-same-session.jsonl"),
                }),
                mock.patch.object(focus.agents, "_resume_arg", return_value=""),
                mock.patch.dict(focus.agents._INFO_FN, {"codex": lambda _pid: {
                    "sessionId": "another-session", "identityExact": True}}),
            ):
                self.assertEqual(focus.launch("codex", "same-session"), 5)
            dispatch.assert_called_once()

    def check_expired_codex_launch(self, files_visible: bool) -> list[int]:
        """Launch codes for an ended codex session after its reservation
        expired, while a codex in its folder runs a session it can't name."""
        codes = []
        with (
            mock.patch.object(focus, "AGGREGATE_PATH", self.path),
            mock.patch.object(focus, "_launch_tern", return_value=0),
            mock.patch.object(focus.sys, "stderr", io.StringIO()),
            mock.patch.object(focus.agents, "_resume_arg", return_value=""),
            mock.patch.object(focus.agents, "_codex_home", return_value=str(self.root)),
            mock.patch.object(focus, "_fds_visible", return_value=files_visible),
        ):
            self.assertEqual(focus.launch("codex", "same-session"), 0)
            expired = focus.time.monotonic() + focus.SESSION_START_RESERVATION_SECONDS + 1
            for info in ({}, {"sessionId": "other", "identityExact": False}):
                with (
                    self.subTest(info=info),
                    mock.patch.object(focus.time, "monotonic", return_value=expired),
                    # A TUI and the app-server holding another session's rollout.
                    mock.patch.object(focus.agents, "_pgrep",
                                      side_effect=lambda _provider, services=False: [78] if services else [77]),
                    mock.patch.object(focus.agents, "_held_rollouts", side_effect=lambda pid, _needle: (
                        {str(self.root / "sessions" / "rollout-other.jsonl")} if pid == 78 else set())),
                    mock.patch.object(focus.agents, "_cwd_of", return_value=str(self.root)),
                    mock.patch.dict(focus.agents._INFO_FN, {"codex": lambda pid: (
                        {"sessionId": "other", "identityExact": True} if pid == 78 else info)}),
                ):
                    codes.append(focus.launch("codex", "same-session"))
                    # A launch leaves a fresh reservation; expire it for the next.
                    focus._reservation_path("codex", "same-session").unlink(missing_ok=True)
        return codes

    def test_expired_reservation_does_not_treat_unresolved_writer_as_absent(self) -> None:
        # Without the processes' open files nothing says which session runs.
        self.assertEqual(self.check_expired_codex_launch(files_visible=False), [11, 11])

    def test_a_codex_holding_another_rollout_does_not_block_a_launch(self) -> None:
        # Every codex writer holds its rollout open, and none holds this one.
        self.assertEqual(self.check_expired_codex_launch(files_visible=True), [0, 0])

    def test_fd_visibility_needs_every_link(self) -> None:
        def readlink(path):
            if path.endswith("/4"):
                raise PermissionError(path)
            if path.endswith("/5"):
                raise FileNotFoundError(path)  # closed while looking
            return "/dev/null"
        for entries, visible in ((["3", "5"], True), (["3", "4"], False)):
            with (
                self.subTest(entries=entries),
                mock.patch.object(focus.os, "listdir", return_value=entries),
                mock.patch.object(focus.os, "readlink", side_effect=readlink),
            ):
                self.assertEqual(focus._fds_visible(77), visible)
        with mock.patch.object(focus.os, "listdir", side_effect=PermissionError("denied")):
            self.assertFalse(focus._fds_visible(77))

    def test_expired_reservation_refuses_when_provider_inspection_is_unavailable(self) -> None:
        with (
            mock.patch.object(focus, "AGGREGATE_PATH", self.path),
            mock.patch.object(focus, "_launch_tern", return_value=0) as dispatch,
            mock.patch.object(focus.sys, "stderr", io.StringIO()),
        ):
            self.assertEqual(focus.launch("codex", "same-session"), 0)
            expired = focus.time.monotonic() + focus.SESSION_START_RESERVATION_SECONDS + 1
            with (
                mock.patch.object(focus.time, "monotonic", return_value=expired),
                mock.patch.dict(focus.agents._INFO_FN, {}, clear=True),
            ):
                self.assertEqual(focus.launch("codex", "same-session"), 11)
            dispatch.assert_called_once()

    def test_reserve_without_a_row_checks_the_folder_it_is_given(self) -> None:
        # agents.json has no row for the session; an omp that names none of
        # its sessions runs in the folder the caller resumes it in.
        self.path.write_text(json.dumps({"agents": [], "history": []}))
        with (
            mock.patch.object(focus, "AGGREGATE_PATH", self.path),
            mock.patch.object(focus.sys, "stderr", io.StringIO()) as stderr,
            mock.patch.object(focus.agents, "_pgrep",
                              side_effect=lambda _name, services=False: [] if services else [77]),
            mock.patch.object(focus.agents, "_open_jsonl_under", return_value=("", False)),
            mock.patch.object(focus.agents, "_omp_log_files", return_value={}),
            mock.patch.object(focus.agents, "_argv_of", return_value=["omp"]),
            mock.patch.object(focus.agents, "_cwd_of", return_value="/work/project"),
            mock.patch.object(focus.state_paths, "migrate"),
        ):
            self.assertEqual(focus.main(["--tern", "--reserve", "omp", "sid-1", "/work/project"]), 11)
        self.assertIn("a running omp in /work/project may be writing this session", stderr.getvalue())

    def reserve(self) -> str:
        with (
            mock.patch.object(focus.sys, "stdout", io.StringIO()) as stdout,
            mock.patch.object(focus.state_paths, "migrate"),
        ):
            self.assertEqual(focus.main(["--tern", "--reserve", "codex", "same-session"]), 0)
        return stdout.getvalue().strip()

    def release(self, token: str) -> int:
        with mock.patch.object(focus.state_paths, "migrate"):
            return focus.main(["--tern", "--release", "codex", "same-session", token])

    def test_release_by_its_owner_lets_the_session_launch(self) -> None:
        with (
            mock.patch.object(focus, "AGGREGATE_PATH", self.path),
            mock.patch.object(focus, "_launch_tern", return_value=0) as dispatch,
            mock.patch.object(focus.sys, "stderr", io.StringIO()),
        ):
            token = self.reserve()
            self.assertEqual(focus.launch("codex", "same-session"), 5)
            self.assertEqual(self.release(token), 0)
            self.assertFalse(focus._reservation_path("codex", "same-session").exists())
            self.assertEqual(focus.launch("codex", "same-session"), 0)
            dispatch.assert_called_once()
            # Nothing of the caller's is left: a second release changes nothing.
            self.assertEqual(self.release(token), 0)
            self.assertTrue(focus._reservation_path("codex", "same-session").exists())

    def test_release_leaves_a_reservation_it_does_not_own(self) -> None:
        with (
            mock.patch.object(focus, "AGGREGATE_PATH", self.path),
            mock.patch.object(focus.sys, "stderr", io.StringIO()),
        ):
            token = self.reserve()
            reservation = focus._reservation_path("codex", "same-session")
            owner = json.loads(reservation.read_text())
            later = focus.time.monotonic() + focus.SESSION_START_RESERVATION_SECONDS + 1
            cases = {
                "another launcher's": ({**owner, "expiresAt": owner["expiresAt"] + 1}, None),
                "another boot's": ({**owner, "bootId": "another-boot"}, None),
                "expired": (owner, later),
                "not a reservation": ([owner], None),
            }
            for name, (written, now) in cases.items():
                with self.subTest(name):
                    reservation.write_text(json.dumps(written))
                    with ExitStack() as stack:
                        if now is not None:
                            stack.enter_context(
                                mock.patch.object(focus.time, "monotonic", return_value=now))
                        self.assertEqual(self.release(token), 0)
                    self.assertEqual(json.loads(reservation.read_text()), written)

    def test_release_rejects_a_malformed_token(self) -> None:
        with (
            mock.patch.object(focus, "AGGREGATE_PATH", self.path),
            mock.patch.object(focus.sys, "stderr", io.StringIO()) as stderr,
        ):
            token = self.reserve()
            self.assertEqual(self.release("not-a-token"), 1)
            self.assertIn("not a reservation token", stderr.getvalue())
            self.assertTrue(focus._reservation_path("codex", "same-session").exists())
            self.assertEqual(self.release(token), 0)


class TeleportTests(unittest.TestCase):
    AGENT, SHELL, KITTY = 500, 400, 300

    def teleport(self, info: dict | None = None, tern_ready: bool = True,
                 exits: bool = True, host: str = "kitty"):
        live = {"provider": "omp", "sessionId": "sid-1", "pid": self.AGENT, "host": "kitty"}
        info = {"sessionId": "sid-1", "identityExact": True, "state": "idle",
                "cwd": "/work/project", **(info or {})}
        parents = {self.AGENT: self.SHELL, self.SHELL: self.KITTY}
        comms = {self.SHELL: "zsh", self.KITTY: "kitty"}
        signals: list[tuple[int, int]] = []
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agents.json"
            path.write_text(json.dumps({"agents": [live], "history": []}))
            with (
                mock.patch.object(focus, "AGGREGATE_PATH", path),
                mock.patch.object(focus.agents, "tern_bin", return_value="/usr/bin/tern"),
                mock.patch.dict(focus.agents._INFO_FN, {"omp": lambda _pid: info}),
                mock.patch.object(focus.agents, "_parent_walk_for_host",
                                  return_value=(host, [])),
                mock.patch.object(focus.agents, "_ppid_of", side_effect=parents.get),
                mock.patch.object(focus.agents, "_comm_of", side_effect=lambda p: comms.get(p, "")),
                mock.patch.object(focus, "_tern_ready", return_value=tern_ready),
                mock.patch.object(focus, "_start_tern", return_value=False),
                mock.patch.object(focus, "_wait_exit", return_value=exits),
                mock.patch.object(focus.os, "kill",
                                  side_effect=lambda p, s: signals.append((p, s))),
                mock.patch.object(focus, "_launch_tern", return_value=0) as launch,
                mock.patch.object(focus.sys, "stderr", io.StringIO()),
            ):
                code = focus.teleport("omp", "sid-1")
        return code, signals, launch

    def test_idle_session_hangs_up_then_closes_window_then_resumes_in_tern(self) -> None:
        code, signals, launch = self.teleport()
        self.assertEqual(code, 0)
        self.assertEqual(signals, [(self.AGENT, focus.signal.SIGHUP),
                                   (self.SHELL, focus.signal.SIGHUP)])
        cwd, _shell, command = launch.call_args.args
        self.assertEqual(cwd, "/work/project")
        self.assertEqual(command, "cd -- /work/project && omp --resume sid-1")

    def test_refusals_never_signal_or_launch(self) -> None:
        cases = {
            "busy": ({"info": {"state": "working"}}, 9),
            "pid now runs another session": ({"info": {"sessionId": "sid-2"}}, 3),
            "unproven identity": ({"info": {"identityExact": False}}, 3),
            "not in kitty": ({"host": "konsole"}, 7),
            "Tern cannot start": ({"tern_ready": False}, 8),
        }
        for name, (kwargs, expected) in cases.items():
            with self.subTest(name):
                code, signals, launch = self.teleport(**kwargs)
                self.assertEqual(code, expected)
                self.assertEqual(signals, [])
                launch.assert_not_called()

    def test_agent_that_survives_hangup_and_term_is_not_resumed_twice(self) -> None:
        code, signals, launch = self.teleport(exits=False)
        self.assertEqual(code, 10)
        self.assertEqual([s for _, s in signals],
                         [focus.signal.SIGHUP, focus.signal.SIGTERM])
        launch.assert_not_called()


class PlatformFocusTests(unittest.TestCase):
    def test_default_and_plugin_modes_disable_only_platform_specific_focus(self) -> None:
        for platform, expected in (("darwin", False), ("linux", True)):
            with mock.patch.object(focus.sys, "platform", platform):
                module = runpy.run_path(str(SCRIPTS / "codexbar_focus.py"))
                self.assertIs(module["USE_KWIN"], expected)
        with (
            mock.patch.object(focus.sys, "platform", "linux"),
            mock.patch.object(focus, "USE_KWIN", True),
            mock.patch.object(focus.state_paths, "migrate"),
            mock.patch.object(focus, "focus", return_value=0) as action,
        ):
            self.assertEqual(focus.main(["--tern", "sid-1"]), 0)
            self.assertFalse(focus.USE_KWIN)
            action.assert_called_once_with("sid-1")

    def test_linux_t3_opener_reports_handler_failure(self) -> None:
        with (
            mock.patch.object(focus.sys, "platform", "linux"),
            mock.patch.object(focus, "_t3_thread_link", return_value="t3code://threads/env/thread"),
            mock.patch.object(focus.subprocess, "run",
                              return_value=focus.subprocess.CompletedProcess([], 1)) as run,
        ):
            self.assertFalse(focus._t3_open("sid-1"))
        self.assertEqual(run.call_args.args[0], ["xdg-open", "t3code://threads/env/thread"])


class MacFocusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(focus.sys, "platform", "darwin"))
        self.stack.enter_context(mock.patch.object(focus, "USE_KWIN", False))
        self.stack.enter_context(mock.patch.object(focus.sys, "stderr", io.StringIO()))
        self.kwin = self.stack.enter_context(mock.patch.object(focus, "_kwin_activate"))
        self.place = self.stack.enter_context(mock.patch.object(focus, "_kwin_place"))
        self.switch = self.stack.enter_context(mock.patch.object(focus, "_set_current_desktop"))
        self.desktops = self.stack.enter_context(mock.patch.object(focus, "_desktop_count"))
        self.addCleanup(self.assert_no_linux_desktop_calls)

    def assert_no_linux_desktop_calls(self) -> None:
        for method in (self.kwin, self.place, self.switch, self.desktops):
            method.assert_not_called()

    def payload(self, records: list[dict], live: list[dict] | None = None) -> None:
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        path = Path(directory) / "agents.json"
        path.write_text(json.dumps({"agents": live or [], "history": records}))
        self.stack.enter_context(mock.patch.object(focus, "AGGREGATE_PATH", path))

    def test_kitty_bundle_discovery_requires_executable_file(self) -> None:
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        home = Path(directory)
        executable = home / "Applications/kitty.app/Contents/MacOS/kitty"
        executable.mkdir(parents=True)
        original = focus.Path.is_file
        with (
            mock.patch.object(focus.shutil, "which", return_value=None),
            mock.patch.object(focus.Path, "home", return_value=home),
            mock.patch.object(focus.Path, "is_file", autospec=True,
                              side_effect=lambda path: original(path) if home in path.parents else False),
        ):
            # An executable directory is not a usable app-bundle binary.
            self.assertEqual(focus._kitty_bin(), "")
            executable.rmdir()
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o700)
            self.assertEqual(focus._kitty_bin(), str(executable))
            executable.chmod(0o600)
            self.assertEqual(focus._kitty_bin(), "")

    def test_resume_shell_preserves_helper_home_across_terminal_environment(self) -> None:
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        home = Path(directory) / "helper-home"
        home.mkdir()
        terminal_home = Path(directory) / "terminal-home"
        terminal_home.mkdir()
        with mock.patch.dict(focus.os.environ, {"HOME": str(home)}):
            argv = focus._resume_shell_argv("/bin/sh", 'printf "%s\\n" "$HOME"')
        result = focus.subprocess.run(
            argv, input="exit\n", text=True, capture_output=True, timeout=5,
            env={"HOME": str(terminal_home), "PATH": "/usr/bin:/bin"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(home))

    def test_failed_kitty_start_and_missing_installation_are_failures(self) -> None:
        self.payload([entry()])
        with (
            mock.patch.object(focus, "_kitty_bin", return_value="/bad/kitty"),
            mock.patch.object(focus.subprocess, "Popen", side_effect=OSError("cannot execute")) as spawn,
        ):
            self.assertEqual(focus.launch("omp", "sid-1"), 8)
            self.assertEqual(spawn.call_count, 1)
        with (
            mock.patch.object(focus, "_kitty_bin", return_value=""),
            mock.patch.object(focus.subprocess, "Popen") as spawn,
        ):
            self.assertEqual(focus.launch("omp", "sid-1"), 8)
            spawn.assert_not_called()

    def test_restore_reports_only_selected_verified_successful_rows(self) -> None:
        rows = [entry(sessionId=sid, closedBy="reboot",
                      resumeCommand=f"cd -- /work/project && omp --resume {sid}")
                for sid in ("good", "bad", "live", "not-selected", "failed")]
        rows[1]["resumeCommand"] = "unexpected command"
        self.payload(rows, [{"provider": "omp", "sessionId": "live"}])
        stdout = io.StringIO()
        def spawn(argv, **kwargs):
            if "omp --resume failed;" in argv[-1]:
                raise OSError("cannot launch")
            return mock.Mock(pid=42)
        with (
            mock.patch.object(focus, "_kitty_bin", return_value="/bundle/kitty"),
            mock.patch.object(focus.subprocess, "Popen", side_effect=spawn) as started,
            mock.patch.object(focus.sys, "stdout", stdout),
        ):
            self.assertEqual(focus.launch_all({("omp", sid) for sid in
                                              ("good", "bad", "live", "failed")}), 0)
        self.assertEqual(json.loads(stdout.getvalue()), [["omp", "good"]])
        self.assertEqual(started.call_count, 2)
        self.assertTrue(all(call.args[0][0] == "/bundle/kitty" for call in started.call_args_list))

    def test_kitty_focus_prefers_inherited_socket_without_abstract_fallbacks(self) -> None:
        record = entry(pid=42)
        addresses = []
        def remote(argv, **kwargs):
            address = argv[argv.index("--to") + 1] if "--to" in argv else ""
            addresses.append(address)
            return focus.subprocess.CompletedProcess(argv, 0 if address == "unix:/private/custom.sock" else 1)
        with (
            mock.patch.object(focus, "_find_sentinel", return_value=record),
            mock.patch.object(focus, "_ancestor_pids", return_value=[42, 41]),
            mock.patch.object(focus.agents, "_comm_of", return_value="kitty"),
            mock.patch.object(focus.agents, "_environ_value", return_value="unix:/private/custom.sock"),
            mock.patch.object(focus, "_kitty_bin", return_value="/bundle/kitty"),
            mock.patch.object(focus.subprocess, "run", side_effect=remote),
        ):
            self.assertEqual(focus.focus("sid-1"), 0)
        self.assertEqual(addresses, ["unix:/private/custom.sock"])
        with (
            mock.patch.object(focus, "_kitty_bin", return_value="/bundle/kitty"),
            mock.patch.object(focus.agents, "_environ_value", return_value=""),
            mock.patch.object(focus.Path, "glob", return_value=[]),
            mock.patch.object(focus.subprocess, "run",
                              return_value=focus.subprocess.CompletedProcess([], 1)) as run,
        ):
            self.assertFalse(focus._kitty_focus([42]))
        self.assertTrue(all("--to" not in call.args[0] for call in run.call_args_list))

    def test_t3_focus_requires_opener_success(self) -> None:
        with (
            mock.patch.object(focus, "_find_sentinel", return_value=entry(host="t3code", pid=42)),
            mock.patch.object(focus, "_ancestor_pids", return_value=[42]),
            mock.patch.object(focus.agents, "t3_window_pids", return_value=[43]),
            mock.patch.object(focus.agents, "_comm_of", return_value="Code"),
            mock.patch.object(focus, "_t3_thread_link", return_value="t3code://threads/env/thread"),
            mock.patch.object(focus.subprocess, "run") as run,
        ):
            for result, expected in ((0, 0), (1, 4)):
                run.return_value = focus.subprocess.CompletedProcess([], result)
                self.assertEqual(focus.focus("sid-1"), expected)
                self.assertEqual(run.call_args.args[0], ["/usr/bin/open", "t3code://threads/env/thread"])
            for failure in (OSError("no handler"), focus.subprocess.TimeoutExpired("open", 5)):
                run.side_effect = failure
                self.assertEqual(focus.focus("sid-1"), 4)

    def test_other_daemon_window_does_not_make_target_ready(self) -> None:
        self.payload([entry(host="tern")])
        calls = []
        def tern(args, socket=""):
            calls.append(args)
            return focus.subprocess.CompletedProcess(args, 0, '{"block": 9}', "")
        with (
            mock.patch.dict(focus.os.environ, {"TERN_DAEMON_SOCKET": "/tmp/target.sock"}),
            mock.patch.object(focus.agents, "tern_window_pids", return_value=[77]),
            mock.patch.object(focus.agents, "_environ_value", return_value="/tmp/unrelated.sock"),
            mock.patch.object(focus, "_tern", side_effect=tern),
            mock.patch.object(focus, "_start_tern", return_value=False) as start,
        ):
            self.assertEqual(focus.launch("omp", "sid-1"), 8)
            start.assert_called_once()
        self.assertEqual(calls, [])  # No hidden tab in the answering target daemon.
        with (
            mock.patch.dict(focus.os.environ, {"TERN_DAEMON_SOCKET": "/tmp/target.sock"}),
            mock.patch.object(focus.agents, "tern_window_pids", return_value=[77]),
            mock.patch.object(focus.agents, "_environ_value", return_value="/tmp/target.sock"),
            mock.patch.object(focus, "_tern", side_effect=tern),
            mock.patch.object(focus, "_start_tern") as start,
        ):
            self.assertEqual(focus.launch("omp", "sid-1"), 0)
            start.assert_not_called()
        self.assertEqual(calls[-2][:2], ["new", "tab"])
        self.assertEqual(calls[-1], ["focus", "9"])

    def test_tern_focus_failure_does_not_claim_success(self) -> None:
        with (
            mock.patch.object(focus, "_find_sentinel", return_value=entry(host="tern", pid=42)),
            mock.patch.object(focus, "_ancestor_pids", return_value=[42]),
            mock.patch.object(focus.agents, "tern_window_pids", return_value=[43]),
            mock.patch.object(focus.agents, "_environ_value",
                              side_effect=lambda _pid, name: "9" if name == "TERN_PANE" else "/tmp/target.sock"),
            mock.patch.object(focus, "_tern") as tern,
        ):
            tern.return_value = focus.subprocess.CompletedProcess([], 1)
            self.assertEqual(focus.focus("sid-1"), 4)
            tern.return_value = focus.subprocess.CompletedProcess([], 0)
            self.assertEqual(focus.focus("sid-1"), 0)
            self.assertEqual(tern.call_args.args, (["focus", "9"], "/tmp/target.sock"))

    def test_tern_start_preserves_environment_and_reports_early_exit(self) -> None:
        isolated = {"HOME": "/tmp/test-home", "TERN_DAEMON_SOCKET": "/tmp/test.sock",
                    "TERN_CONFIG_DIR": "/tmp/test-config"}
        with (
            mock.patch.dict(focus.os.environ, isolated),
            mock.patch.object(focus.agents, "tern_bin", return_value="/bundle/tern"),
            mock.patch.object(focus.time, "sleep"),
            mock.patch.object(focus, "_tern_ready", side_effect=[False, True]),
            mock.patch.object(focus.subprocess, "Popen") as spawn,
        ):
            spawn.return_value.poll.return_value = None
            self.assertTrue(focus._start_tern())
            self.assertEqual(spawn.call_args.args[0], ["/bundle/tern"])
            # No env replacement: the direct executable inherits all three.
            self.assertNotIn("env", spawn.call_args.kwargs)
            spawn.return_value.poll.return_value = 1
            self.assertFalse(focus._start_tern())
        self.payload([entry(host="tern")])
        with (
            mock.patch.object(focus, "_tern_ready", return_value=False),
            mock.patch.object(focus, "_start_tern", return_value=False),
            mock.patch.object(focus, "_tern") as tern,
        ):
            self.assertEqual(focus.launch("omp", "sid-1"), 8)
            tern.assert_not_called()


class StartupLockTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        patch = mock.patch.object(focus, "AGGREGATE_PATH", self.root / "agents.json")
        patch.start()
        self.addCleanup(patch.stop)

    def lock_file(self, name: str, age_s: float, suffix: str = "lock") -> Path:
        path = self.root / f"agents.json.{name}.startup.{suffix}"
        path.touch()
        stamp = time.time() - age_s
        os.utime(path, (stamp, stamp))
        return path

    def test_only_unheld_locks_unused_for_a_day_go(self) -> None:
        day = focus.STARTUP_LOCK_KEPT_SECONDS
        old = self.lock_file("old", day + 60)
        fresh = self.lock_file("fresh", day - 60)
        held = self.lock_file("held", day + 60)
        mine = self.lock_file("mine", day + 60)
        holder = os.open(held, os.O_RDWR)
        self.addCleanup(os.close, holder)
        fcntl.flock(holder, fcntl.LOCK_EX)
        focus._prune_startup_locks(self.root, keep=mine)
        self.assertEqual([path.exists() for path in (old, fresh, held, mine)], [False, True, True, True])

    def test_reservations_go_with_their_locks(self) -> None:
        day = focus.STARTUP_LOCK_KEPT_SECONDS
        mine = self.lock_file("mine", 0)
        pair = [self.lock_file("pair", day + 60), self.lock_file("pair", day + 60, "json")]
        # Its lock went before reservations were pruned too.
        orphan = self.lock_file("orphan", day + 60, "json")
        fresh_orphan = self.lock_file("fresh-orphan", day - 60, "json")
        reserved = [self.lock_file("reserved", day + 60), self.lock_file("reserved", day - 60, "json")]
        focus._prune_startup_locks(self.root, keep=mine)
        self.assertEqual([path.exists() for path in pair], [False, False])
        self.assertFalse(orphan.exists())
        self.assertFalse(orphan.with_suffix(".lock").exists())
        self.assertTrue(fresh_orphan.exists())
        self.assertEqual([path.exists() for path in reserved], [True, True])

    def test_it_looks_at_most_once_an_hour(self) -> None:
        day = focus.STARTUP_LOCK_KEPT_SECONDS
        mine = self.lock_file("mine", 0)
        first = self.lock_file("first", day + 60)
        focus._prune_startup_locks(self.root, keep=mine)
        second = self.lock_file("second", day + 60)
        focus._prune_startup_locks(self.root, keep=mine)
        self.assertEqual([first.exists(), second.exists()], [False, True])
        later = time.time() + focus.STARTUP_PRUNE_EVERY_SECONDS + 1
        with mock.patch.object(focus.time, "time", return_value=later):
            focus._prune_startup_locks(self.root, keep=mine)
        self.assertFalse(second.exists())

    def test_a_waiter_on_a_removed_lock_takes_the_new_one(self) -> None:
        path = self.lock_file("s", 0)
        holder = os.open(path, os.O_RDWR)
        fcntl.flock(holder, fcntl.LOCK_EX)
        entered = threading.Event()
        leave = threading.Event()

        def waiter() -> None:
            with focus._startup_lock(path):
                entered.set()
                leave.wait(5)

        thread = threading.Thread(target=waiter)
        thread.start()
        self.assertFalse(entered.wait(0.2))
        # A prune between the waiter's open and its flock: the path is gone.
        path.unlink()
        os.close(holder)
        self.assertTrue(entered.wait(5))
        # The waiter holds the file the path names now, so nobody else gets it.
        probe = os.open(path, os.O_RDWR)
        try:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(probe)
            leave.set()
            thread.join(5)


if __name__ == "__main__":
    unittest.main()

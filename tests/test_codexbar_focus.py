from __future__ import annotations

from contextlib import ExitStack
import io
import json
import sys
import runpy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "contents" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import codexbar_focus as focus  # noqa: E402


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
    def test_link_maps_claude_and_codex_sessions_to_t3_threads(self) -> None:
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "environment-id").write_text("env-1\n")
            with sqlite3.connect(root / "state.sqlite") as db:
                db.execute("CREATE TABLE provider_session_runtime"
                           " (thread_id TEXT, last_seen_at TEXT, resume_cursor_json TEXT)")
                db.executemany("INSERT INTO provider_session_runtime VALUES (?, ?, ?)", [
                    ("t-claude", "2", json.dumps({"threadId": "t-claude", "resume": "claude-sid"})),
                    ("t-codex", "1", json.dumps({"threadId": "codex-sid"})),
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


if __name__ == "__main__":
    unittest.main()

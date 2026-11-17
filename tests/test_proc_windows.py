from __future__ import annotations

import contextlib
import ctypes
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "contents" / "scripts"
sys.path.insert(0, str(SCRIPTS))
if sys.platform == "win32":
    import proc_windows as proc

WOW64_CMD = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "SysWOW64" / "cmd.exe"


def same_path(a: str, b: str | Path) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def load(name: str, fake_dlls: bool = False) -> types.ModuleType:
    """A fresh copy of a helper module. With `fake_dlls`, Windows' DLLs are
    stand-ins, so the module's pure logic runs on any platform."""
    spec = importlib.util.spec_from_file_location(f"windows_test_{name}", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    with mock.patch.object(ctypes, "WinDLL", mock.MagicMock(), create=True) if fake_dlls else contextlib.nullcontext():
        spec.loader.exec_module(module)
    return module


class WindowsParentTests(unittest.TestCase):
    def test_a_parent_younger_than_its_child_is_not_its_parent(self) -> None:
        # Windows keeps an exited parent's pid, which a newer process may reuse.
        fake = load("proc_windows", fake_dlls=True)
        parent = types.SimpleNamespace(InheritedFromUniqueProcessId=17)
        for child_ms, parent_ms, expected in ((2000, 1000, 17), (1000, 2000, 0), (0, 2000, 17)):
            with self.subTest(child=child_ms, parent=parent_ms), \
                    mock.patch.object(fake, "_basic_info", return_value=parent), \
                    mock.patch.object(fake, "_created_ms", side_effect=[child_ms, parent_ms]):
                self.assertEqual(fake.ppid_of(42), expected)


@unittest.skipUnless(sys.platform == "win32", "reads Windows processes through kernel32 and ntdll")
class WindowsProcessTests(unittest.TestCase):
    def start(self, argv: list[str], **kwargs) -> subprocess.Popen:
        child = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
        self.addCleanup(self.stop, child)
        return child

    @staticmethod
    def stop(child: subprocess.Popen) -> None:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)

    def test_current_process(self) -> None:
        me = os.getpid()
        self.assertTrue(same_path(proc.cwd_of(me), os.getcwd()))
        self.assertTrue(same_path(proc.exe_of(me), sys.executable) or proc.comm_of(me).startswith("python"))
        self.assertEqual(proc.ppid_of(me), os.getppid())
        self.assertEqual(proc.environ_value(me, "SystemRoot"), os.environ["SystemRoot"])
        self.assertTrue(proc.argv_of(me))
        self.assertFalse(proc.exited(me))
        self.assertIn(me, proc.all_pids())
        self.assertGreater(proc.start_ms(me), 0)
        self.assertLessEqual(proc.start_ms(me), time.time() * 1000)

    def test_child_argv_folder_environment_parent_and_start(self) -> None:
        # The late variable outgrows the block the process started with.
        code = "import os, sys; os.environ['AGENTVIEW_LATE'] = 'x' * 9000; print('ready', flush=True); sys.stdin.readline()"
        args = ["-u", "-c", code, "two words", 'quote"d', "back\\slash\\", "--resume", "session-id", ""]
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, AGENTVIEW_TEST_MARKER="owned-child \u00e9", TERN_PANE="12345")
            before = int(time.time() * 1000)
            child = self.start([sys.executable, *args], cwd=directory, env=env)
            self.assertEqual(child.stdout.readline().strip(), b"ready")
            after = int(time.time() * 1000)
            argv = proc.argv_of(child.pid)
            self.assertTrue(same_path(argv[0], sys.executable))
            self.assertEqual(argv[1:], args)
            self.assertTrue(same_path(proc.cwd_of(child.pid), directory))
            self.assertFalse(proc.cwd_of(child.pid).endswith("\\"))
            self.assertEqual(proc.environ_value(child.pid, "AGENTVIEW_TEST_MARKER"), "owned-child \u00e9")
            # Windows names are case-insensitive.
            self.assertEqual(proc.environ_value(child.pid, "tern_pane"), "12345")
            self.assertEqual(proc.environ_value(child.pid, "TERN_PAN"), "")
            self.assertEqual(proc.environ_value(child.pid, "MISSING"), "")
            self.assertEqual(proc.environ_value(child.pid, "AGENTVIEW_LATE"), "x" * 9000)
            self.assertEqual(proc.ppid_of(child.pid), os.getpid())
            self.assertGreaterEqual(proc.start_ms(child.pid), before - 1000)
            self.assertLessEqual(proc.start_ms(child.pid), after)
            self.assertIn(child.pid, proc.all_pids())
            self.assertFalse(proc.exited(child.pid))
            self.assertEqual(proc.open_paths(child.pid), [])
            # The base interpreter a venv launcher starts is python too.
            self.assertRegex(proc.comm_of(child.pid), r"^python")
            child.communicate(input=b"\n", timeout=10)
            self.assertTrue(proc.exited(child.pid))
            self.assertEqual(proc.argv_of(child.pid), [])
            self.assertEqual(proc.cwd_of(child.pid), "")
            self.assertEqual(proc.environ_value(child.pid, "TERN_PANE"), "")

    @unittest.skipUnless(WOW64_CMD.is_file(), "no 32-bit cmd.exe (SysWOW64) on this Windows")
    def test_32_bit_process(self) -> None:
        # Registered first, so removed after the child is stopped.
        directory = self.enterContext(tempfile.TemporaryDirectory())
        child = self.start(
            [str(WOW64_CMD), "/d", "/q", "/k", "echo", "ready"], cwd=directory, env=dict(os.environ, TERN_PANE="7"),
        )
        # WOW64 fills in the 32-bit PEB while the process starts; its
        # output means it has.
        self.assertEqual(child.stdout.readline().strip(), b"ready")
        self.assertEqual(proc.argv_of(child.pid)[1:], ["/d", "/q", "/k", "echo", "ready"])
        self.assertEqual(proc.comm_of(child.pid), "cmd")
        self.assertTrue(same_path(proc.cwd_of(child.pid), directory))
        self.assertEqual(proc.environ_value(child.pid, "TERN_PANE"), "7")
        self.assertEqual(proc.ppid_of(child.pid), os.getpid())

    def test_missing_and_protected_processes_read_as_empty(self) -> None:
        live = set(proc.all_pids())
        missing = next(pid for pid in range(4, 1 << 20, 4) if pid not in live)
        for pid in (missing, 0, -1, 1 << 40):
            with self.subTest(pid=pid):
                self.assertEqual(proc.argv_of(pid), [])
                self.assertEqual(proc.cwd_of(pid), "")
                self.assertEqual(proc.environ_value(pid, "PATH"), "")
                self.assertEqual(proc.ppid_of(pid), 0)
                self.assertEqual(proc.start_ms(pid), 0)
                self.assertEqual(proc.exe_of(pid), "")
                self.assertEqual(proc.comm_of(pid), "")
                self.assertTrue(proc.exited(pid))
        # The System process runs but refuses a normal user's reads.
        self.assertFalse(proc.exited(4))
        # Refused access to one process says nothing of a pid out of range.
        self.assertTrue(proc.exited(1 << 40))
        self.assertEqual(proc.argv_of(4), [])

    def test_command_line_splitting(self) -> None:
        self.assertEqual(proc._split_command_line(""), [])
        self.assertEqual(proc._split_command_line("   "), [])
        self.assertEqual(
            proc._split_command_line(r'omp.exe --resume "a b" c\\"d" "e\"f"'),
            ["omp.exe", "--resume", "a b", 'c\\d', 'e"f'],
        )

    def test_boot_id_is_the_boot_count(self) -> None:
        self.assertRegex(proc.boot_id(), r"^\d+$")
        self.assertEqual(proc.boot_id(), proc.boot_id())

    def test_agents_list_a_tern_pane_process_without_a_tern_ancestor(self) -> None:
        agents = load("codexbar_agents")
        code = "import sys; print('ready', flush=True); sys.stdin.readline()"
        child = self.start([sys.executable, "-c", code], env=dict(os.environ, TERN_PANE="7"))
        self.assertEqual(child.stdout.readline().strip(), b"ready")
        plain_env = {k: v for k, v in os.environ.items() if k.upper() != "TERN_PANE"}
        plain = self.start([sys.executable, "-c", code], env=plain_env)
        self.assertEqual(plain.stdout.readline().strip(), b"ready")
        # Run from a VS Code or Tern terminal, the real walk would stop there.
        with mock.patch.object(agents, "_parent_walk_for_host", return_value=("", [])):
            host, chain = agents._session_host(child.pid, {"tern": []})
            self.assertEqual((host, chain), ("tern", [child.pid]))
            self.assertEqual(agents._session_host(plain.pid, {"tern": []}), ("", []))
        self.assertTrue(re.fullmatch(r"\d+", agents._read_boot_id()))

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_agents_name_a_node_hosted_agent_by_its_package(self) -> None:
        agents = load("codexbar_agents")
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        cli = root / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js"
        cli.parent.mkdir(parents=True)
        cli.write_text("console.log('ready'); process.stdin.on('data', () => process.exit(0));\n")
        node = shutil.which("node")
        children = {}
        for kind, args in (("interactive", ["--resume", "abc"]), ("one-shot", ["-p", "hi"]), ("other", [])):
            script = str(cli) if kind != "other" else str(root / "other.js")
            if kind == "other":
                Path(script).write_text(cli.read_text())
            child = self.start([node, "--no-warnings", script, *args], env=dict(os.environ, TERN_PANE="7"))
            self.assertEqual(child.stdout.readline().strip(), b"ready")
            children[kind] = child.pid
        self.assertEqual(agents._comm_of(children["interactive"]), "claude")
        self.assertEqual(agents._argv_of(children["interactive"]), ["claude", "--resume", "abc"])
        self.assertEqual(agents._resume_arg(children["interactive"], "--resume"), "abc")
        with agents._process_snapshot():
            self.assertIn(children["interactive"], agents._pgrep("claude"))
            self.assertNotIn(children["one-shot"], agents._pgrep("claude"))
            self.assertIn(children["one-shot"], agents._pgrep("claude", services=True))
        # A node script that is no agent keeps the runtime's name.
        self.assertEqual(agents._comm_of(children["other"]), "node")
        self.assertEqual(Path(agents._argv_of(children["other"])[0]).stem.lower(), "node")


if __name__ == "__main__":
    unittest.main()

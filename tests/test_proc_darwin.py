from __future__ import annotations

import ctypes
import importlib.util
import json
import os
import select
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "contents" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import proc_darwin as proc


def procargs(argc: int, *entries: bytes) -> bytes:
    return struct.pack("=i", argc) + b"/usr/bin/agent\0\0\0" + b"\0".join(entries) + b"\0"


def copy_bytes(buffer, payload: bytes) -> int:
    ctypes.memmove(buffer, payload, len(payload))
    return len(payload)


class DarwinArgumentTests(unittest.TestCase):
    def test_argv_and_environment_decode_padding_empty_arguments_and_exact_names(self):
        raw = procargs(4, b"omp", b"--resume", b"session-id", b"", b"TERM_OTHER=no", b"TERM=xterm-256color", b"PROMPT=bad\xff", b"")
        with mock.patch.object(proc, "_sysctl", side_effect=lambda mib, size: struct.pack("=i", 4096) if mib[-1] == proc._KERN_ARGMAX else raw):
            self.assertEqual(proc.argv_of(42), ["omp", "--resume", "session-id", ""])
            self.assertEqual(proc.environ_value(42, "TERM"), "xterm-256color")
            self.assertEqual(proc.environ_value(42, "PROMPT"), "bad\ufffd")
            self.assertEqual(proc.environ_value(42, "MISSING"), "")

    def test_malformed_argc_and_truncated_payload_do_not_claim_session_argv(self):
        for raw in (
            b"", b"abc", struct.pack("=i", 1) + b"unterminated executable",
            struct.pack("=i", 1) + b"\0omp\0", procargs(-1, b"omp", b"TOKEN=secret"),
            procargs(0, b"omp"), procargs(100, b"omp", b"TOKEN=secret"),
            struct.pack("=i", 2) + b"/bin/omp\0omp\0--resume",
        ):
            with self.subTest(raw=raw), mock.patch.object(proc, "_sysctl", side_effect=[struct.pack("=i", 4096), raw]):
                self.assertEqual(proc._procargs(42), ([], []))

    def test_missing_or_malformed_argmax_keeps_process_decoding_available(self):
        for limit in (b"", b"abc", b"abcdef", struct.pack("=i", -1), struct.pack("=i", 0)):
            with self.subTest(limit=limit), mock.patch.object(proc, "_sysctl", side_effect=[limit, procargs(1, b"omp")]):
                self.assertEqual(proc.argv_of(42), ["omp"])

    def test_sysctl_permission_and_oversized_reply_return_empty(self):
        with mock.patch.object(proc, "_libc") as libc:
            libc.sysctl.return_value = -1
            self.assertEqual(proc._sysctl([1, 49, 42], 16), b"")
            def oversized(mib, count, buffer, length, *_):
                ctypes.cast(length, ctypes.POINTER(ctypes.c_size_t))[0] = 17
                return 0
            libc.sysctl.side_effect = oversized
            self.assertEqual(proc._sysctl([1, 49, 42], 16), b"")


class DarwinProcessBoundaryTests(unittest.TestCase):
    def test_bsd_parent_start_and_zombie_state_decode_kernel_layout(self):
        short = struct.pack("=4I16s", 42, 17, 42, proc._SZOMB, b"agent") + bytes(32)
        fields = [0] * 12 + [b"agent", b"agent"] + [0] * 6 + [1_700_000_000, 123_456]
        full = proc._BSDINFO.pack(*fields)
        def info(pid, flavor, arg, buffer, size):
            return copy_bytes(buffer, short if flavor == proc._PROC_PIDT_SHORTBSDINFO else full)
        with mock.patch.object(proc, "_libc") as libc:
            libc.proc_pidinfo.side_effect = info
            self.assertEqual(proc.ppid_of(42), 17)
            self.assertEqual(proc.start_ms(42), 1_700_000_000_123)
            self.assertTrue(proc.exited(42))
            with mock.patch.object(proc, "argv_of", return_value=[]), mock.patch.object(proc, "exe_of", return_value=""):
                self.assertEqual(proc.comm_of(42), "agent")

    def test_permissions_short_reads_and_disappearing_processes_fail_closed(self):
        with mock.patch.object(proc, "_libc") as libc:
            for result in (-1, 0, 12):
                with self.subTest(result=result):
                    libc.proc_pidinfo.return_value = result
                    self.assertEqual(proc.ppid_of(42), 0)
                    self.assertEqual(proc.start_ms(42), 0)
                    self.assertEqual(proc.cwd_of(42), "")
                    self.assertTrue(proc.exited(42))
            libc.proc_pidinfo.return_value = 0
            libc.proc_pidpath.return_value = 0
            self.assertEqual(proc.exe_of(42), "")
            self.assertEqual(proc.open_paths(42), [])
            with mock.patch.object(proc, "_sysctl", return_value=b""):
                self.assertEqual(proc.argv_of(42), [])
                self.assertEqual(proc.environ_value(42, "TERN_PANE"), "")
                self.assertEqual(proc.comm_of(42), "")

    def test_cwd_and_executable_decode_null_terminated_paths(self):
        path = b"/tmp/project\xff\0ignored"
        vnode = bytes(proc._VNODE_INFO_SIZE) + path
        vnode += bytes(2 * proc._VNODE_INFO_PATH_SIZE - len(vnode))
        with mock.patch.object(proc, "_libc") as libc:
            libc.proc_pidinfo.side_effect = lambda pid, flavor, arg, buf, size: copy_bytes(buf, vnode)
            libc.proc_pidpath.side_effect = lambda pid, buf, size: copy_bytes(buf, b"/Applications/Tern.app/Contents/MacOS/tern\0")
            self.assertEqual(proc.cwd_of(42), "/tmp/project\ufffd")
            self.assertEqual(proc.exe_of(42), "/Applications/Tern.app/Contents/MacOS/tern")

    def test_open_file_scan_ignores_non_vnodes_and_an_fd_closed_during_scan(self):
        fds = struct.pack("=iIiIiI", 3, proc._PROX_FDTYPE_VNODE, 4, 2, 5, proc._PROX_FDTYPE_VNODE)
        path = b"/tmp/.omp/agent/sessions/project/session.jsonl\0"
        vnode = bytes(proc._FILEINFO_SIZE + proc._VNODE_INFO_SIZE) + path
        vnode += bytes(proc._FILEINFO_SIZE + proc._VNODE_INFO_PATH_SIZE - len(vnode))
        def info(pid, flavor, arg, buf, size):
            return len(fds) if buf is None else copy_bytes(buf, fds)
        def fdinfo(pid, fd, flavor, buf, size):
            return 0 if fd == 3 else copy_bytes(buf, vnode)
        with mock.patch.object(proc, "_libc") as libc:
            libc.proc_pidinfo.side_effect = info
            libc.proc_pidfdinfo.side_effect = fdinfo
            self.assertEqual(proc.open_paths(42), [path[:-1].decode()])
            self.assertEqual(libc.proc_pidfdinfo.call_count, 2)
            libc.proc_pidinfo.side_effect = [len(fds), 0]
            self.assertEqual(proc.open_paths(42), [])

    def test_pid_list_excludes_zero_and_handles_disappearance_between_calls(self):
        def fill(buffer, size):
            if buffer is None:
                return 2
            buffer[0], buffer[1], buffer[2] = 42, 0, 17
            return 3
        with mock.patch.object(proc, "_libc") as libc:
            libc.proc_listallpids.side_effect = fill
            self.assertEqual(proc.all_pids(), [42, 17])
            libc.proc_listallpids.side_effect = [2, -1]
            self.assertEqual(proc.all_pids(), [])
            libc.proc_listallpids.side_effect = [0]
            self.assertEqual(proc.all_pids(), [])

    def test_boot_uuid_decodes_and_permission_failure_is_empty(self):
        def boot(name, buf, size, *_):
            raw = b"11111111-2222-3333-4444-555555555555\0"
            copy_bytes(buf, raw)
            ctypes.cast(size, ctypes.POINTER(ctypes.c_size_t))[0] = len(raw)
            return 0
        with mock.patch.object(proc, "_libc") as libc:
            libc.sysctlbyname.side_effect = boot
            self.assertEqual(proc.boot_id(), "11111111-2222-3333-4444-555555555555")
            libc.sysctlbyname.side_effect = None
            libc.sysctlbyname.return_value = -1
            self.assertEqual(proc.boot_id(), "")


@unittest.skipUnless(sys.platform == "darwin", "requires macOS libproc/sysctl")
class DarwinRealProcessTests(unittest.TestCase):
    def test_real_child_introspection_preserves_rollout_ownership(self):
        spec = importlib.util.spec_from_file_location("darwin_test_agents", SCRIPTS / "codexbar_agents.py")
        agents = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(agents)
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory).resolve()
            rollout = cwd / ".omp/agent/sessions/project/2026-01-01_session-id.jsonl"
            rollout.parent.mkdir(parents=True)
            rollout.write_text(json.dumps({"type": "session", "id": "session-id", "cwd": str(cwd), "timestamp": int(time.time() * 1000)}) + "\n" + json.dumps({"type": "message", "message": {"role": "assistant", "content": [{"type": "text", "text": "ready"}], "stopReason": "stop"}}) + "\n")
            code = "import sys; f = open(sys.argv[1]); print('ready', flush=True); sys.stdin.readline()"
            env = dict(os.environ, AGENTVIEW_TEST_MARKER="owned-child", TERN_PANE="12345")
            before = int(time.time() * 1000)
            child = subprocess.Popen([sys.executable, "-u", "-c", code, str(rollout), "--resume", "session-id"], cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                self.assertTrue(select.select([child.stdout], [], [], 10)[0], "child did not open its rollout")
                self.assertEqual(child.stdout.readline(), b"ready\n")
                after = int(time.time() * 1000)
                self.assertEqual(proc.ppid_of(child.pid), os.getpid())
                self.assertEqual(Path(proc.cwd_of(child.pid)).resolve(), cwd)
                self.assertEqual(proc.argv_of(child.pid)[1:], ["-u", "-c", code, str(rollout), "--resume", "session-id"])
                self.assertEqual(proc.environ_value(child.pid, "AGENTVIEW_TEST_MARKER"), "owned-child")
                self.assertEqual(proc.environ_value(child.pid, "TERN_PANE"), "12345")
                self.assertIn(str(rollout), proc.open_paths(child.pid))
                self.assertIn(child.pid, proc.all_pids())
                self.assertGreaterEqual(proc.start_ms(child.pid), before - 1000)
                self.assertLessEqual(proc.start_ms(child.pid), after)
                self.assertFalse(proc.exited(child.pid))
                executable = subprocess.check_output(
                    ["/bin/ps", "-p", str(child.pid), "-o", "comm="], text=True,
                ).strip()
                self.assertEqual(Path(proc.exe_of(child.pid)).resolve(), Path(executable).resolve())
                self.assertEqual(agents._open_jsonl_under(child.pid, "/.omp/agent/sessions/"), (str(rollout), True))
                record = agents._pi_info(child.pid)
                self.assertEqual((record["sessionId"], record["cwd"], record["state"], record["identityExact"]), ("session-id", str(cwd), "idle", True))
                self.assertRegex(proc.boot_id(), r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
                child.communicate(input=b"\n", timeout=5)
                self.assertEqual(child.returncode, 0)
                self.assertTrue(proc.exited(child.pid))
                self.assertEqual(proc.argv_of(child.pid), [])
                self.assertEqual(proc.open_paths(child.pid), [])
            finally:
                if child.poll() is None:
                    child.terminate()
                    child.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()

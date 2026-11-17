"""Runs the Tern plugin's Luau checks with the `luau` CLI.

Each half (tern/host.luau, tern/window.luau) runs as one chunk: the fake
`tern` (tests/tern_fake.luau), then the half's source, then its checks
(tests/tern_<half>_checks.luau), which see the half's top-level locals. Each
check prints one `ok <name>` or `FAIL <name>: <error>` line.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
LUAU = shutil.which("luau")


@unittest.skipUnless(LUAU, "needs the luau CLI (Arch: pacman -S luau)")
class TernLuauTests(unittest.TestCase):
    def _run_checks(self, half: str) -> None:
        parts = [
            (TESTS / "tern_fake.luau").read_text(encoding="utf-8"),
            (ROOT / "tern" / f"{half}.luau").read_text(encoding="utf-8"),
            (TESTS / f"tern_{half}_checks.luau").read_text(encoding="utf-8"),
        ]
        # Where each part starts in the chunk, for the line numbers of an error.
        starts, line = [], 1
        for text in parts:
            starts.append(line)
            line += text.count("\n") + 1
        where = f"chunk lines: fake from {starts[0]}, {half}.luau from {starts[1]}, checks from {starts[2]}"
        with tempfile.TemporaryDirectory() as folder:
            chunk = Path(folder) / f"{half}_checks.luau"
            chunk.write_text("\n".join(parts), encoding="utf-8")
            proc = subprocess.run([LUAU, str(chunk)], capture_output=True, encoding="utf-8", timeout=60)
        self.assertEqual(proc.returncode, 0, f"{proc.stdout}{proc.stderr}\n{where}")
        results = [ln for ln in proc.stdout.splitlines() if ln.startswith(("ok ", "FAIL "))]
        self.assertTrue(results, f"no checks ran:\n{proc.stdout}{proc.stderr}\n{where}")
        for result in results:
            name = result.removeprefix("ok ").removeprefix("FAIL ").split(" :: ", 1)[0]
            with self.subTest(check=name):
                self.assertTrue(result.startswith("ok "), f"{result}\n{where}")

    def test_host_checks(self) -> None:
        self._run_checks("host")

    def test_window_checks(self) -> None:
        self._run_checks("window")


if __name__ == "__main__":
    unittest.main()

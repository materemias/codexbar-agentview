"""Stand-ins the tests start as programs, on POSIX and on Windows."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

WINDOWS = os.name == "nt"


def fake_program(path: Path, source: str) -> Path:
    """A program at `path` running Python `source`, found on PATH by its bare
    name like the real one: a script with a shebang, or on Windows `path.exe`
    (Windows starts only an .exe by bare name), made with the launcher pip
    makes console scripts with. Returns the file to run."""
    if not WINDOWS:
        path.write_text(f"#!{sys.executable}\n{source}")
        path.chmod(0o755)
        return path
    from pip._vendor.distlib.scripts import ScriptMaker
    with tempfile.TemporaryDirectory() as directory:
        Path(directory, path.name).write_text(f"#!python\n{source}")
        maker = ScriptMaker(directory, str(path.parent), add_launchers=True)
        maker.executable = sys.executable
        maker.variants = {""}
        [made] = maker.make(path.name)
    return Path(made)


def kill_tree(pid: int) -> None:
    """Kill a process started in its own session (POSIX) and what it started."""
    if WINDOWS:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, check=False)
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGKILL)

"""CodexBar AgentView's own state directory, and the file lock its helpers share.

The upstream CodexBar CLI owns ~/.codexbar/ (config.json and its caches). Our
files live in $XDG_STATE_HOME/codexbar-agentview, or by default
~/.local/state/codexbar-agentview (%LOCALAPPDATA%\\codexbar-agentview on
Windows) when XDG_STATE_HOME is unset or relative. `migrate()` moves the files
earlier versions kept in ~/.codexbar/ the first time a helper runs after the
rename.
"""
from __future__ import annotations

import contextlib
import errno
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import IO

WINDOWS = os.name == "nt"

if WINDOWS:
    import msvcrt

    # Windows byte-range locks are mandatory: a lock on a file's data would
    # block other processes reading it. Lock one byte far past any data.
    _LOCK_OFFSET = 1 << 30

    def lock(file: int | IO, blocking: bool = True) -> bool:
        """An exclusive lock on `file` (a file object or descriptor) until it
        is closed; False when `blocking` is off and another process holds it."""
        fd = file if isinstance(file, int) else file.fileno()
        position = os.lseek(fd, 0, os.SEEK_CUR)
        try:
            while True:
                os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    return True
                except OSError as error:
                    # Only a conflict means another holder; anything else is a failure.
                    if error.errno not in (errno.EACCES, errno.EDEADLOCK):
                        raise
                    if not blocking:
                        return False
                    time.sleep(0.05)
        finally:
            os.lseek(fd, position, os.SEEK_SET)

    def make_private(fd: int) -> None:
        """Owner-only permissions; on Windows the profile's ACL already is."""
else:
    import fcntl

    def lock(file: int | IO, blocking: bool = True) -> bool:
        """An exclusive lock on `file` (a file object or descriptor) until it
        is closed; False when `blocking` is off and another process holds it."""
        try:
            fcntl.flock(file, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            return False
        return True

    def make_private(fd: int) -> None:
        """Owner-only permissions (0600)."""
        os.fchmod(fd, 0o600)


def replace(source: str | os.PathLike, target: str | os.PathLike) -> None:
    """`os.replace`. Windows refuses to replace a file another process has
    open without delete sharing, as Python's readers do for a moment: retry
    for about a second before giving up."""
    for _ in range(20 if WINDOWS else 0):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            time.sleep(0.05)
    os.replace(source, target)


def fsync_dir(path: str | os.PathLike) -> None:
    """Make a rename in folder `path` durable. Windows can't open a folder to
    fsync it, and NTFS journals the rename itself: a no-op there."""
    if WINDOWS:
        return
    directory = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def write_file(path: str | os.PathLike, text: str, durable: bool) -> None:
    """Replace `path` atomically through a private, uniquely named temporary file
    beside it; `durable` also fsyncs the file and its folder."""
    path = Path(path)
    output = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.",
        suffix=".tmp", delete=False,
    )
    temporary = Path(output.name)
    try:
        # Closed before any cleanup: Windows can't delete a file still open.
        with output:
            make_private(output.fileno())
            output.write(text)
            if durable:
                output.flush()
                os.fsync(output.fileno())
        replace(temporary, path)
    except BaseException:
        # A failed cleanup must not hide the write's own error.
        with contextlib.suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise
    if durable:
        fsync_dir(path.parent)


# Earlier versions kept these in ~/.codexbar/ next to the CLI's own files.
_LEGACY_MOVED = (
    "agents.json", "agents.parsers.json", "usage_cache.json",
    "forecast_cache.json", "agents",
)
_LEGACY_DELETED = ("agents.lock", "usage_cache.json.lock", "tern_fetch_claim")


def state_dir() -> Path:
    """The state directory for the current environment; never touches disk."""
    base = os.environ.get("XDG_STATE_HOME", "")
    if os.path.isabs(base):
        root = Path(base)
    elif WINDOWS and os.path.isabs(os.environ.get("LOCALAPPDATA", "")):
        root = Path(os.environ["LOCALAPPDATA"])
    else:
        root = Path.home() / ".local" / "state"
    return root / "codexbar-agentview"


STATE_DIR = state_dir()


def migrate() -> Path:
    """Create the state directory and move legacy files out of ~/.codexbar/.

    Helpers start concurrently, so the whole step runs under a lock beside the
    directory. Each item moves only while the new location lacks it: a retry
    finishes an interrupted migration and never overwrites newer state."""
    target = state_dir()
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.parent / "codexbar-agentview.migrate.lock"
    with open(lock_path, "a") as held:
        make_private(held.fileno())
        lock(held)
        target.mkdir(mode=0o700, exist_ok=True)
        os.chmod(target, 0o700)
        legacy = Path.home() / ".codexbar"
        if not legacy.is_dir():
            return target
        for name in _LEGACY_MOVED:
            source, dest = legacy / name, target / name
            if source.exists() and not dest.exists():
                shutil.move(str(source), str(dest))
        for name in _LEGACY_DELETED:
            try:
                (legacy / name).unlink()
            except FileNotFoundError:
                pass
    return target

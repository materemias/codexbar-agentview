"""CodexBar AgentView's own state directory.

The upstream CodexBar CLI owns ~/.codexbar/ (config.json and its caches). Our
files live in $XDG_STATE_HOME/codexbar-agentview, or
~/.local/state/codexbar-agentview when XDG_STATE_HOME is unset or relative.
`migrate()` moves the files earlier versions kept in ~/.codexbar/ the first
time a helper runs after the rename.
"""
from __future__ import annotations

import fcntl
import os
import shutil
from pathlib import Path

# Earlier versions kept these in ~/.codexbar/ next to the CLI's own files.
_LEGACY_MOVED = (
    "agents.json", "agents.parsers.json", "usage_cache.json",
    "forecast_cache.json", "agents",
)
_LEGACY_DELETED = ("agents.lock", "usage_cache.json.lock", "tern_fetch_claim")


def state_dir() -> Path:
    """The state directory for the current environment; never touches disk."""
    base = os.environ.get("XDG_STATE_HOME", "")
    root = Path(base) if os.path.isabs(base) else Path.home() / ".local" / "state"
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
    with open(lock_path, "a") as lock:
        os.fchmod(lock.fileno(), 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
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

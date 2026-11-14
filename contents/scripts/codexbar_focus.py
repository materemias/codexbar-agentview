#!/usr/bin/env python3
"""Bring the terminal window hosting an agent session to the front, or
relaunch a history record in a new kitty window or Tern tab.

Invoked as a URL handler via Qt.openUrlExternally() from the plasmoid:
  codexbar-agentview://focus/<sessionId>

It looks up the matching sentinel in the state directory's agents/ folder or
the agents.json aggregate, walks the agent's process ancestry, and tries (in
order):
  1. kitty's remote control if kitty is somewhere up the tree, or Tern's
     `tern focus` for the session's pane
  2. KWin scripting to activate any window owned by an ancestor pid

The KWin step works for any window kwin manages — VS Code, plain kitty,
Konsole, Yakuake, Wezterm, etc.

  codexbar_focus.py --launch <provider> <sessionId>

starts the saved resume command of a history record. A kitty record gets a
new window: Linux uses a systemd scope and its saved virtual desktop; macOS
opens directly on the current desktop. A Tern record gets a new tab in
the running Tern window, whose session daemon owns the process.

  codexbar_focus.py --teleport <provider> <sessionId>

moves an idle live kitty session into a new Tern tab: it hangs up the agent
as closing its window would, closes that kitty window, and resumes the
session in Tern.

  codexbar_focus.py --reserve <provider> <sessionId> [<cwd>]

takes the session's startup ownership for a caller that starts the writer
itself in <cwd>, as the Tern plugin does when it reopens a session for a wait.
On success it prints the reservation's token on stdout.

  codexbar_focus.py --release <provider> <sessionId> <token>

gives that ownership back when the caller never started the writer. Only the
reservation the token names goes, while it holds: one that expired, or that
another launcher took since, stays.

A leading --tern (e.g. `--tern --launch <provider> <sessionId>`) is passed by
the Tern plugin, which may run outside KDE: the helper then never calls KWin.
Kitty and Tern focus still work, T3 Code opens its thread link, kitty
launches open on the current desktop, and other hosts fail.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal
from urllib.parse import quote

import codexbar_agents as agents
import state_paths

AGENTS_DIR = state_paths.STATE_DIR / "agents"
AGGREGATE_PATH = state_paths.STATE_DIR / "agents.json"
# Darwin and Windows have neither KWin nor systemd; --tern also disables KWin on Linux.
USE_KWIN = sys.platform not in ("darwin", "win32")
T3_DIR = Path.home() / ".t3" / "userdata"


def _find_sentinel(session_id: str) -> dict | None:
    # Per-session sentinel files (Claude with hooks installed).
    if AGENTS_DIR.is_dir():
        for entry in AGENTS_DIR.glob("*.json"):
            if entry.name.endswith(".tmp.json"):
                continue
            try:
                rec = json.loads(entry.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(rec, dict) and rec.get("sessionId") == session_id:
                return rec
    # Virtual records (Codex, untracked Claude) only live in the aggregate file.
    if AGGREGATE_PATH.is_file():
        try:
            agg = json.loads(AGGREGATE_PATH.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        for rec in (agg.get("agents") or []):
            if isinstance(rec, dict) and rec.get("sessionId") == session_id:
                return rec
    return None


def _ancestor_pids(start_pid: int, max_depth: int = 16) -> list[int]:
    """Walk /proc up from start_pid; return [start_pid, parent, grandparent, …]."""
    pids: list[int] = []
    cur = int(start_pid or 0)
    while cur > 1 and len(pids) < max_depth and cur not in pids:
        pids.append(cur)
        cur = agents._ppid_of(cur)
    return pids


def _kitty_bin() -> str:
    """Find Kitty even when a GUI-launched helper inherits a reduced PATH."""
    found = shutil.which("kitty")
    if found:
        return found
    if sys.platform == "darwin":
        for root in (Path("/Applications"), Path.home() / "Applications"):
            path = root / "kitty.app" / "Contents" / "MacOS" / "kitty"
            if path.is_file() and os.access(path, os.X_OK):
                return str(path)
    return ""


def _kitty_focus(candidate_pids: list[int]) -> bool:
    """Try every kitty socket we can find for a window matching one of the pids.

    Works only when kitty has `allow_remote_control yes` and a `listen_on`
    socket. Prefer the address inherited by the agent; legacy sockets are
    fallbacks, and abstract Unix sockets are Linux-only.
    """
    executable = _kitty_bin()
    if not executable:
        return False
    sockets = [address for pid in candidate_pids
               if (address := agents._environ_value(pid, "KITTY_LISTEN_ON"))]
    for pattern in ("mykitty-*", "kitty-*"):
        sockets.extend(f"unix:{path}" for path in Path("/tmp").glob(pattern))
    if sys.platform != "darwin":
        for pid in candidate_pids:
            sockets.append(f"unix:@kitty-{pid}")
            sockets.append(f"unix:@mykitty-{pid}")
    seen = set()
    for sock in sockets:
        if sock in seen:
            continue
        seen.add(sock)
        for pid in candidate_pids:
            try:
                proc = subprocess.run(
                    [executable, "@", "--to", sock, "focus-window", "--match", f"pid:{pid}"],
                    capture_output=True, timeout=2.0, check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                return False
            if proc.returncode == 0:
                return True
    # Last attempt: default socket (no --to).
    for pid in candidate_pids:
        try:
            proc = subprocess.run(
                [executable, "@", "focus-window", "--match", f"pid:{pid}"],
                capture_output=True, timeout=2.0, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        if proc.returncode == 0:
            return True
    return False


def _tern(args: list[str], socket: str = "") -> subprocess.CompletedProcess | None:
    """Run one `tern` scripting command; None when tern is missing or hangs."""
    env = dict(os.environ, TERN_DAEMON_SOCKET=socket) if socket else None
    try:
        return subprocess.run(
            [agents.tern_bin() or "tern", *args], capture_output=True, text=True, timeout=5.0,
            check=False, env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _tern_focus(pid: int) -> bool:
    """Show the Tern pane running pid in every Tern window. Agents inherit
    TERN_PANE and TERN_PANE_SOCKET from the pane's shell."""
    pane = agents._environ_value(pid, "TERN_PANE")
    if not pane.isdigit():
        return False
    proc = _tern(["focus", pane], agents._environ_value(pid, "TERN_PANE_SOCKET"))
    return proc is not None and proc.returncode == 0


KWIN_SCRIPT_TEMPLATE = """
const targets = new Set([%PIDS%]);
const captionHint = "%HINT%".toLowerCase();
const wins = workspace.windowList ? workspace.windowList() : workspace.clientList();
// Multi-window apps like VS Code share one main pid across all their
// windows, so a pure pid match can grab the wrong window. Prefer one whose
// caption contains the agent's cwd basename (workspace folder name); fall
// back to any pid match.
let preferred = null;
let fallback = null;
for (const w of wins) {
    if (!w || typeof w.pid === "undefined") continue;
    if (!targets.has(w.pid)) continue;
    if (!fallback) fallback = w;
    if (captionHint && w.caption &&
        w.caption.toLowerCase().indexOf(captionHint) >= 0) {
        preferred = w;
        break;
    }
}
const target = preferred || fallback;
if (target) {
    try { target.minimized = false; } catch (e) {}
    // If the window lives on another virtual desktop, switch to it first;
    // KWin won't focus a window that isn't on the current desktop. Plasma 6
    // exposes `desktops` as an array; Plasma 5 used a numeric `desktop`.
    try {
        if (target.desktops && target.desktops.length > 0) {
            workspace.currentDesktop = target.desktops[0];
        } else if (typeof target.desktop === "number" && target.desktop > 0) {
            workspace.currentDesktop = target.desktop;
        }
    } catch (e) {}
    // Same idea for Activities.
    try {
        if (target.activities && target.activities.length > 0 &&
            workspace.currentActivity &&
            target.activities.indexOf(workspace.currentActivity) < 0) {
            workspace.currentActivity = target.activities[0];
        }
    } catch (e) {}
    try { workspace.activeWindow = target; } catch (e) {}
    try { if (workspace.raiseWindow) workspace.raiseWindow(target); } catch (e) {}
}
"""


def _caption_hint(record: dict) -> str:
    """The basename of cwd, escaped for embedding in a JS string literal."""
    cwd = (record or {}).get("cwd") or ""
    base = ""
    for part in reversed(cwd.split("/")):
        if part:
            base = part
            break
    # Strip anything that could break the JS string. Cwd basenames don't
    # normally contain these but be paranoid.
    return "".join(ch for ch in base if ch.isalnum() or ch in ("-", "_", ".", " "))


def _load_kwin_script(script: str, name: str | None = None) -> str:
    """Load and start a KWin script; return its id, or "" on failure."""
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False)
    tmp.write(script)
    tmp.close()
    try:
        load = subprocess.run(
            ["qdbus6", "org.kde.KWin", "/Scripting",
             "org.kde.kwin.Scripting.loadScript", tmp.name,
             *([name] if name else [])],
            capture_output=True, text=True, timeout=4.0, check=False,
        )
        sid = (load.stdout or "").strip()
        if not sid.isdigit():
            return ""
        subprocess.run(
            ["qdbus6", "org.kde.KWin", "/Scripting",
             "org.kde.kwin.Scripting.start"],
            capture_output=True, timeout=4.0, check=False,
        )
        return sid
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def _stop_kwin_script(sid: str, name: str | None = None) -> None:
    try:
        subprocess.run(
            ["qdbus6", "org.kde.KWin", f"/Scripting/Script{sid}", "stop"],
            capture_output=True, timeout=2.0, check=False,
        )
        if name:
            subprocess.run(
                ["qdbus6", "org.kde.KWin", "/Scripting",
                 "org.kde.kwin.Scripting.unloadScript", name],
                capture_output=True, timeout=2.0, check=False,
            )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


def _kwin_activate(candidate_pids: list[int], record: dict | None = None) -> bool:
    """Use KWin scripting to activate the right window. Pid-only match isn't
    enough for Electron apps where many windows share one main pid; we also
    pass a cwd-basename caption hint and prefer matches containing it."""
    if not candidate_pids:
        return False
    script = (KWIN_SCRIPT_TEMPLATE
              .replace("%PIDS%", ",".join(str(p) for p in candidate_pids))
              .replace("%HINT%", _caption_hint(record or {})))
    sid = _load_kwin_script(script)
    if not sid:
        return False
    # Run + immediately stop so kwin doesn't keep the script registered.
    _stop_kwin_script(sid)
    return True


# Keeps the window of one new pid on a saved desktop and activates it there,
# even if the user switched away while kitty started. The script unloads
# itself once it placed the window; the launcher unloads it after
# PLACE_TIMEOUT if the window never appeared.
KWIN_PLACE_TEMPLATE = """
const pid = %PID%;
const want = "%DESKTOP%";
const name = "%NAME%";
let placed = false;
function place(w) {
    if (placed || !w || w.pid !== pid) return;
    placed = true;
    if (want === "all") {
        w.onAllDesktops = true;
    } else {
        const d = workspace.desktops[Number(want) - 1];
        if (d) {
            w.desktops = [d];
            workspace.currentDesktop = d;
        }
    }
    try { workspace.activeWindow = w; } catch (e) {}
    callDBus("org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting",
             "unloadScript", name);
}
workspace.windowAdded.connect(place);
for (const w of workspace.windowList()) place(w);
"""
PLACE_TIMEOUT = 15.0
# A Tern window opened by Launch must have its session daemon up by then.
TERN_START_TIMEOUT = 10.0


def _desktop_count() -> int:
    try:
        out = subprocess.run(
            ["qdbus6", "org.kde.KWin", "/VirtualDesktopManager",
             "org.kde.KWin.VirtualDesktopManager.count"],
            capture_output=True, text=True, timeout=2.0, check=False,
        ).stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 0
    return int(out) if out.isdigit() else 0


def _set_current_desktop(number: int) -> None:
    try:
        subprocess.run(
            ["qdbus6", "org.kde.KWin", "/KWin",
             "org.kde.KWin.setCurrentDesktop", str(number)],
            capture_output=True, timeout=2.0, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


def _kwin_place(pid: int, desktop: str) -> bool:
    name = f"codexbar-restore-{pid}"
    script = (KWIN_PLACE_TEMPLATE
              .replace("%PID%", str(pid))
              .replace("%DESKTOP%", desktop)
              .replace("%NAME%", name))
    sid = _load_kwin_script(script, name)
    if not sid:
        return False
    deadline = time.monotonic() + PLACE_TIMEOUT
    while time.monotonic() < deadline:
        time.sleep(0.2)
        try:
            loaded = subprocess.run(
                ["qdbus6", "org.kde.KWin", "/Scripting",
                 "org.kde.kwin.Scripting.isScriptLoaded", name],
                capture_output=True, text=True, timeout=2.0, check=False,
            ).stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            break
        if loaded == "false":
            return True
    _stop_kwin_script(sid, name)
    return False


# A terminal can accept a command before its agent becomes discoverable.
# Keep that startup gap owned even after the helper exits or crashes.
SESSION_START_RESERVATION_SECONDS = 60.0
# A session's startup lock file nobody has taken for this long is removed.
STARTUP_LOCK_KEPT_SECONDS = 24 * 3600.0
# How often a startup looks for those files at all.
STARTUP_PRUNE_EVERY_SECONDS = 3600.0


def _names_inode(fd: int, path: Path) -> bool:
    """Whether `path` still names the file open as `fd`."""
    try:
        current = os.stat(path)
    except FileNotFoundError:
        return False
    held = os.fstat(fd)
    return (current.st_dev, current.st_ino) == (held.st_dev, held.st_ino)


@contextlib.contextmanager
def _startup_lock(path: Path):
    """The flock serializing one session's startups. `_prune_startup_locks` may
    unlink the file meanwhile: a lock that ends up on an inode the path no
    longer names is let go and taken again on the path's file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            state_paths.make_private(fd)
            state_paths.lock(fd)
            if _names_inode(fd, path):
                # Its last use, for `_prune_startup_locks`; Windows can't utime an fd.
                os.utime(path if state_paths.WINDOWS else fd)
                yield
                return
        finally:
            os.close(fd)


def _prune_startup_locks(directory: Path, keep: Path) -> None:
    """Remove the startup lock and reservation files of other sessions unused
    for a day. A pair goes only while nobody holds its lock and the lock's
    path still names the inode locked, so a waiter on it notices
    (`_startup_lock`); a reservation whose lock is gone is locked anew first.
    At most once an hour: a marker file's age says when the last look was."""
    now = time.time()
    marker = directory / f"{AGGREGATE_PATH.name}.startup-pruned"
    try:
        if now - marker.stat().st_mtime < STARTUP_PRUNE_EVERY_SECONDS:
            return
    except OSError:
        pass
    with contextlib.suppress(OSError):
        marker.touch()
    cutoff = now - STARTUP_LOCK_KEPT_SECONDS
    locks = {
        path.with_suffix(".lock")
        for path in directory.glob(f"{AGGREGATE_PATH.name}.*.startup.*")
        if path.suffix in (".lock", ".json")
    }
    for path in locks - {keep}:
        reservation = path.with_suffix(".json")
        if _mtime(path) > cutoff or _mtime(reservation) > cutoff:
            continue
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            continue
        remove = False
        try:
            # A startup may have reserved the session since the look above;
            # one that only used the lock leaves nothing a removal loses.
            if (state_paths.lock(fd, blocking=False) and _names_inode(fd, path)
                    and _mtime(reservation) <= cutoff):
                reservation.unlink(missing_ok=True)
                remove = True
                # Windows deletes no open file, so the lock goes once closed
                # below, and stays while a startup has it open.
                if not state_paths.WINDOWS:
                    path.unlink()
        except OSError:
            pass
        finally:
            os.close(fd)
        if remove and state_paths.WINDOWS:
            with contextlib.suppress(OSError):
                path.unlink()


def _mtime(path: Path) -> float:
    """When `path` last changed; a missing file counts as never."""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


class _DispatchAckUnknown(Exception):
    """A terminal may have started the writer without acknowledging it."""


def _session_writer_status(provider: str, session_id: str, cwd: str) -> str:
    """Return running, uncertain or absent, including non-focusable writers."""
    with agents._process_snapshot():
        source = agents._BACKFILL_SOURCES.get(provider)
        if source is not None:
            held, unresolved = source.live()
        else:
            info_fn = agents._INFO_FN.get(provider)
            if info_fn is None:
                return "uncertain"
            flags = (
                ("--session", "--session-id") if provider == "pi"
                else (agents._RESUME_PREFIXES[provider][-1],)
            )
            held, unresolved = set(), set()
            pids = [
                pid for services in (False, True)
                for pid in agents._pgrep(provider, services=services)
            ]
            # Codex writes a session only through its rollout, held open by
            # the TUI or the app-server that loaded it. With every codex
            # process's files in view, the check below sees a writer of this
            # session, and one whose own session is a guess isn't writing it.
            rollouts_seen = provider == "codex" and all(map(_fds_visible, pids))
            for pid in pids:
                for flag in flags:
                    resumed = agents._resume_arg(pid, flag)
                    if provider == "pi":
                        resumed = resumed or agents._shell_resume_arg(pid, provider, flag)
                    if resumed:
                        held.add(resumed)
                # A Codex app-server can hold several sessions; its
                # selected info record alone does not name every writer.
                if provider == "codex":
                    paths = agents._held_rollouts(
                        pid, agents._codex_home(pid) + "/sessions/"
                    )
                    if any(path.endswith(f"-{session_id}.jsonl") for path in paths):
                        held.add(session_id)
                try:
                    info = info_fn(pid)
                except (OSError, ValueError, TypeError, AttributeError, OverflowError):
                    info = {}
                info = info if isinstance(info, dict) else {}
                sid = agents._session_id(info.get("sessionId"))
                if sid:
                    held.add(sid)
                if not rollouts_seen and (not sid or info.get("identityExact") is not True):
                    if directory := agents._cwd_of(pid):
                        unresolved.add(directory)
                    if directory := agents._text(info.get("cwd")):
                        unresolved.add(directory)
    if session_id in held:
        return "running"
    return "uncertain" if cwd and cwd in unresolved else "absent"


def _fds_visible(pid: int) -> bool:
    """Whether this helper can read every file `pid` holds open. An fd closed
    while looking is no loss; any other failure hides what it held."""
    fd_dir = f"/proc/{pid}/fd"
    try:
        entries = os.listdir(fd_dir)
    except OSError:
        return False
    for entry in entries:
        try:
            os.readlink(os.path.join(fd_dir, entry))
        except FileNotFoundError:
            continue
        except OSError:
            return False
    return True


def _reservation_path(provider: str, session_id: str) -> Path:
    """A session's startup reservation; its lock has the suffix `.lock`."""
    digest = hashlib.sha256(json.dumps([provider, session_id]).encode()).hexdigest()
    return AGGREGATE_PATH.with_name(f"{AGGREGATE_PATH.name}.{digest}.startup.json")


def _session_operation(
    provider: str, session_id: str, kind: Literal["launch", "teleport"],
    operation: Callable[[str, str, dict, str], int], cwd: str = "",
    print_token: bool = False,
) -> int:
    """Own validation through dispatch, then reserve the writer's startup.
    `operation` gets the session's folder: its agents.json record's, else
    `cwd`, else the last reservation's, else "". With `print_token`, a
    reservation that stays is named on stdout for `release`."""
    reservation = _reservation_path(provider, session_id)
    lock = reservation.with_suffix(".lock")
    with _startup_lock(lock):
        _prune_startup_locks(lock.parent, keep=lock)
        payload = agents._load_payload(AGGREGATE_PATH)
        record = next(
            (r for r in payload["agents"] + payload["history"]
             if agents._record_key(r) == (provider, session_id)), {}
        )
        cwd = agents._text(record.get("cwd")) or cwd
        try:
            previous = json.loads(reservation.read_text())
            if not isinstance(previous, dict):
                previous = {}
        except FileNotFoundError:
            previous = None
        except (OSError, ValueError):
            previous = {}
        boot_id = agents._read_boot_id()
        if previous is not None:
            if not cwd:
                cwd = agents._text(previous.get("cwd"))
            # Expiry is not evidence of an absent writer. Reconcile every
            # provider before allowing a crashed/abandoned startup to retry.
            status = _session_writer_status(provider, session_id, cwd)
            if status == "running":
                reservation.unlink(missing_ok=True)
                if kind == "launch":
                    return _refusal(provider, cwd, status)
            else:
                deadline = previous.get("expiresAt")
                now = time.monotonic()
                if (
                    previous.get("bootId") == boot_id
                    and isinstance(deadline, (int, float)) and not isinstance(deadline, bool)
                    and now < deadline <= now + SESSION_START_RESERVATION_SECONDS
                ):
                    sys.stderr.write("codexbar_focus: session is already starting\n")
                    return 5
                if refused := _refusal(provider, cwd, status):
                    return refused
        owner = {
            "bootId": boot_id, "cwd": cwd,
            "expiresAt": time.monotonic() + SESSION_START_RESERVATION_SECONDS,
        }
        agents._write_file(reservation, json.dumps(owner), durable=True)
        # An exception may follow dispatch; leave its bounded reservation
        # intact, just as when the helper is killed at that boundary.
        try:
            result = operation(provider, session_id, payload, cwd)
        except _DispatchAckUnknown:
            result = 8
        else:
            if result != 0:
                reservation.unlink(missing_ok=True)
                return result
        owner["expiresAt"] = time.monotonic() + SESSION_START_RESERVATION_SECONDS
        agents._write_file(reservation, json.dumps(owner), durable=True)
        if print_token and result == 0:
            print(json.dumps(owner["expiresAt"]))
        return result


def launch(provider: str, session_id: str) -> int:
    """Resume one history record in a new kitty window or Tern tab."""
    return _session_operation(provider, session_id, "launch", _launch)


def reserve(provider: str, session_id: str, cwd: str = "") -> int:
    """Take a session's startup ownership for a caller that starts the writer
    itself in `cwd`, such as the Tern plugin reopening a closed session in a
    tab of its choosing. Refused as `_refusal` says, or with 5 while another
    launcher starts it, and Launch, Teleport or another reserve are refused
    for the reservation's lifetime after it. A session agents.json has no row
    for is checked against the processes in `cwd`. Prints the token
    `release` takes."""
    return _session_operation(
        provider, session_id, "launch", _writer_refusal, cwd, print_token=True
    )


def release(provider: str, session_id: str, token: str) -> int:
    """Give back the startup ownership `reserve` took, for a caller that never
    started the writer. The reservation goes only while it is the one the
    token names: this boot's, with that deadline, not yet past. One that
    expired may have let another launcher start the session, and theirs
    carries another deadline. 0 when it went or nothing of the caller's is
    left."""
    try:
        deadline = float(token)
    except ValueError:
        sys.stderr.write(f"codexbar_focus: not a reservation token: {token}\n")
        return 1
    reservation = _reservation_path(provider, session_id)
    if not reservation.exists():
        return 0
    with _startup_lock(reservation.with_suffix(".lock")):
        try:
            owner = json.loads(reservation.read_text())
        except FileNotFoundError:
            return 0
        except ValueError:
            return 0  # not a reservation `reserve` wrote
        held = owner.get("expiresAt") if isinstance(owner, dict) else None
        if (
            isinstance(held, float) and held == deadline
            and owner.get("bootId") == agents._read_boot_id()
            and time.monotonic() < deadline
        ):
            reservation.unlink()
    return 0


def _listed_writer_runs(record: dict) -> bool:
    """Whether an agents.json row's process still runs. A stale scan lists
    writers that exited since; a row without a pid can't be checked."""
    pid = record.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        return True
    return not _exited(pid)


def _refusal(provider: str, cwd: str, status: str) -> int:
    """Refuse a new writer, saying why: 5 while the session runs, 11 while a
    running process may be writing it, which nothing here can confirm and a
    caller should not count as running. 0 for an absent writer."""
    if status == "running":
        sys.stderr.write("codexbar_focus: session is already running\n")
        return 5
    if status == "uncertain":
        folder = f" in {cwd}" if cwd else ""
        sys.stderr.write(f"codexbar_focus: a running {provider}{folder} may be writing this session\n")
        return 11
    return 0


def _writer_refusal(provider: str, session_id: str, payload: dict, cwd: str) -> int:
    """`_refusal` of the session, by agents.json, then the processes."""
    if any(
        agents._record_key(r) == (provider, session_id) and _listed_writer_runs(r)
        for r in payload["agents"]
    ):
        return _refusal(provider, cwd, "running")
    return _refusal(provider, cwd, _session_writer_status(provider, session_id, cwd))


def _launch(provider: str, session_id: str, payload: dict, cwd: str) -> int:
    if refused := _writer_refusal(provider, session_id, payload, cwd):
        return refused
    key = (provider, session_id)
    record = next(
        (r for r in payload["history"] if agents._record_key(r) == key), None
    )
    if record is None:
        sys.stderr.write("codexbar_focus: no history record for session\n")
        return 2
    # The folder its resume command was saved for.
    cwd = record.get("cwd") if isinstance(record.get("cwd"), str) else ""
    command = record.get("resumeCommand")
    if not command or command != agents._resume_command(
        provider, session_id, cwd, True, record.get("codexHome")
    ):
        sys.stderr.write("codexbar_focus: no verified resume command\n")
        return 6
    host = record.get("host")
    if host not in agents.LAUNCH_HOSTS:
        sys.stderr.write("codexbar_focus: only kitty and Tern history records launch\n")
        return 7
    shell = _user_shell()
    if host == "tern":
        return _launch_tern(cwd, shell, command)
    if state_paths.WINDOWS:
        sys.stderr.write("codexbar_focus: kitty is not available on Windows\n")
        return 8
    # An interactive shell loads the user's PATH; the trailing shell keeps
    # the window open after the agent exits, like the original terminal.
    executable = _kitty_bin() if sys.platform == "darwin" else "kitty"
    if not executable:
        sys.stderr.write("codexbar_focus: Kitty is not installed\n")
        return 8
    argv = [
        executable, "--directory", cwd or str(Path.home()), "--",
        *_resume_shell_argv(shell, command),
    ]
    if sys.platform != "darwin":
        argv = ["systemd-run", "--user", "--scope", "--quiet", "--collect", "--", *argv]
    desktop = record.get("desktop")
    if not USE_KWIN or not agents._valid_desktop(desktop):
        desktop = ""
    elif desktop != "all" and int(desktop) > _desktop_count():
        sys.stderr.write(
            f"codexbar_focus: desktop {desktop} no longer exists; "
            "opened on the current desktop\n"
        )
        desktop = ""
    elif desktop != "all":
        # Switch first, so the user watches the window open where it lands.
        _set_current_desktop(int(desktop))
    try:
        # systemd-run --scope execs kitty, so this pid is the window's pid.
        proc = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )
    except OSError as exc:
        sys.stderr.write(f"codexbar_focus: launch failed: {exc}\n")
        return 8
    if desktop and not _kwin_place(proc.pid, desktop):
        sys.stderr.write("codexbar_focus: launched, but could not move window\n")
    return 0


def _user_shell() -> str:
    if state_paths.WINDOWS:
        return "powershell"
    import pwd
    return os.environ.get("SHELL") or pwd.getpwuid(os.getuid()).pw_shell or "/bin/sh"


def _resume_shell_argv(shell: str, command: str) -> list[str]:
    if state_paths.WINDOWS:
        # A PowerShell line (agents._resume_command); -NoExit keeps the tab
        # open after the agent exits, like the trailing shell below.
        return [shell, "-NoExit", "-Command", command]
    argv = [shell, "-ic", f"{command}; exec {shlex.quote(shell)} -i"]
    if sys.platform == "darwin":
        # Mac terminals customize direct-shell startup. Keep the helper's
        # home even when a daemon supplies its own shell environment.
        argv = ["/usr/bin/env", f"HOME={Path.home()}", *argv]
    return argv


# Shells whose exit closes the kitty window they run in.
SHELL_COMMS = {"bash", "zsh", "fish", "sh", "dash", "ksh", "mksh", "tcsh", "nu"}
TELEPORT_HUP_WAIT = 3.0
TELEPORT_TERM_WAIT = 7.0


def _exited(pid: int) -> bool:
    if sys.platform == "darwin":
        import proc_darwin
        return proc_darwin.exited(pid)
    if state_paths.WINDOWS:
        import proc_windows
        return proc_windows.exited(pid)
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return True
    # The state follows the parenthesized comm; a zombie has exited.
    return stat.rsplit(")", 1)[-1].split()[:1] == ["Z"]


def _wait_exit(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while not _exited(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)
    return True


def _signal(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass


def teleport(provider: str, session_id: str) -> int:
    """Resume an idle live kitty session in a new Tern tab.

    The agent must exit first: a session file has one live writer, so Tern
    starts only after the kitty process is gone."""
    if state_paths.WINDOWS:
        sys.stderr.write("codexbar_focus: only kitty sessions teleport, and Windows has no kitty\n")
        return 7
    return _session_operation(provider, session_id, "teleport", _teleport)


def _teleport(provider: str, session_id: str, payload: dict, _cwd: str) -> int:
    if not agents.tern_bin():
        sys.stderr.write("codexbar_focus: Tern is not installed\n")
        return 7
    key = (provider, session_id)
    record = next(
        (r for r in payload["agents"] if agents._record_key(r) == key), None
    )
    if record is None:
        sys.stderr.write("codexbar_focus: session is not running\n")
        return 2
    pid = record.get("pid")
    info_fn = agents._INFO_FN.get(provider)
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1 or info_fn is None:
        sys.stderr.write("codexbar_focus: session has no process\n")
        return 3
    # Re-read the process, so a stale snapshot never signals a reused pid.
    try:
        info = info_fn(pid)
    except (OSError, ValueError, TypeError, AttributeError, OverflowError):
        info = {}
    info = info if isinstance(info, dict) else {}
    if (
        agents._session_id(info.get("sessionId")) != session_id
        or info.get("identityExact") is not True
    ):
        sys.stderr.write("codexbar_focus: process no longer runs this session\n")
        return 3
    if agents._parent_walk_for_host(pid)[0] != "kitty":
        sys.stderr.write("codexbar_focus: only kitty sessions teleport\n")
        return 7
    if (info.get("state") or "working") != "idle":
        sys.stderr.write("codexbar_focus: session is busy; teleport it once idle\n")
        return 9
    cwd = agents._text(info.get("cwd")) or agents._cwd_of(pid)
    command = agents._resume_command(
        provider, session_id, cwd, True, info.get("codexHome")
    )
    if not command:
        sys.stderr.write("codexbar_focus: no verified resume command\n")
        return 6
    # Start Tern before ending the agent, so a failure leaves it running.
    if not _tern_ready() and not _start_tern():
        sys.stderr.write("codexbar_focus: teleport failed: Tern did not start\n")
        return 8
    parent = agents._ppid_of(pid)
    window_shell = parent if (
        agents._comm_of(parent) in SHELL_COMMS and agents._comm_of(agents._ppid_of(parent)) == "kitty"
    ) else 0
    # A hangup is what closing the window sends; every agent saves on it.
    _signal(pid, signal.SIGHUP)
    if not _wait_exit(pid, TELEPORT_HUP_WAIT):
        _signal(pid, signal.SIGTERM)
        if not _wait_exit(pid, TELEPORT_TERM_WAIT):
            sys.stderr.write("codexbar_focus: teleport failed: the agent did not exit\n")
            return 10
    if window_shell:
        _signal(window_shell, signal.SIGHUP)
    return _launch_tern(cwd, _user_shell(), command)


def _tern_ready() -> bool:
    """A Tern window shows the daemon's sessions. The daemon outlives its
    windows, so an answering daemon alone would take a hidden tab."""
    windows = agents.tern_window_pids()
    socket = os.environ.get("TERN_DAEMON_SOCKET", "")
    if socket:
        windows = [pid for pid in windows
                   if agents._environ_value(pid, "TERN_DAEMON_SOCKET") == socket]
    if not windows:
        return False
    proc = _tern(["ls"])
    return proc is not None and proc.returncode == 0


def _start_tern() -> bool:
    """Open a Tern window with the helper's HOME and daemon/config environment.
    Linux uses a scope so a plasmashell stop does not kill the window."""
    executable = agents.tern_bin()
    if not executable:
        return False
    argv = [executable]
    if sys.platform not in ("darwin", "win32"):
        argv = ["systemd-run", "--user", "--scope", "--quiet", "--collect", "--", *argv]
    # Its own session or process group, apart from the helper's.
    detach = ({"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
              if state_paths.WINDOWS else {"start_new_session": True})
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, **detach,
        )
    except OSError:
        return False
    deadline = time.monotonic() + TERN_START_TIMEOUT
    while time.monotonic() < deadline:
        time.sleep(0.3)
        if proc.poll() is not None:
            return False
        if _tern_ready():
            return True
    return False


def _launch_tern(cwd: str, shell: str, command: str) -> int:
    """Open the resume command in a new tab of the running Tern window and
    raise that window. Tern's session daemon starts the program, so it
    outlives plasmashell without a scope."""
    if not _tern_ready() and not _start_tern():
        sys.stderr.write("codexbar_focus: launch failed: Tern did not start\n")
        return 8
    proc = _tern([
        "new", "tab", "--json", "--cwd", cwd or str(Path.home()), "--",
        *_resume_shell_argv(shell, command),
    ])
    if proc is None:
        sys.stderr.write("codexbar_focus: launch failed: Tern did not acknowledge tab creation\n")
        raise _DispatchAckUnknown
    if proc.returncode != 0:
        detail = proc.stderr.strip() or "tern is not available"
        sys.stderr.write(f"codexbar_focus: launch failed: {detail}\n")
        return 8
    # A tab created from the command line stays in the background.
    try:
        block = json.loads(proc.stdout).get("block")
    except (ValueError, AttributeError):
        block = None
    if isinstance(block, int):
        _tern(["focus", str(block)])
    if USE_KWIN:
        windows = agents.tern_window_pids()
        if not windows or not _kwin_activate(windows):
            sys.stderr.write("codexbar_focus: launched, but could not raise Tern\n")
    return 0


def _desktop_rank(record: dict) -> tuple[int, int]:
    """Numbered desktops first, then "all", then unknown, like the UI."""
    desktop = record.get("desktop")
    if agents._valid_desktop(desktop) and desktop != "all":
        return 0, int(desktop)
    return (1, 0) if desktop == "all" else (2, 0)


def launch_all(keys: set[tuple[str, str]]) -> int:
    """Resume the given launchable restart rows, one desktop after another.

    Each launch waits for its window to be placed, so desktop switches run
    in order and the user ends on the last desktop restored. Stdout carries
    the JSON list of [provider, sessionId] pairs that actually launched."""
    payload = agents._load_payload(AGGREGATE_PATH)
    live = {agents._record_key(r) for r in payload["agents"]}
    records = sorted(
        (r for r in payload["history"]
         if agents._record_key(r) in keys and agents._record_key(r) not in live
         and r.get("closedBy") == "reboot" and r.get("host") in agents.LAUNCH_HOSTS
         and r.get("resumeCommand")),
        key=_desktop_rank,
    )
    launched: list[list[str]] = []
    for record in records:
        provider, session_id = agents._record_key(record)
        if launch(provider, session_id) == 0:
            launched.append([provider, session_id])
    sys.stdout.write(json.dumps(launched) + "\n")
    if not launched:
        sys.stderr.write("codexbar_focus: nothing to restore\n")
        return 2
    return 0


def _t3_thread_link(session_id: str) -> str:
    """The t3code:// link of the T3 Code thread running a provider session.
    T3 Code 0.1 keeps the native Claude, Codex or ACP session id as
    `nativeThreadRef.nativeId` on each of the thread's provider threads."""
    try:
        env = (T3_DIR / "environment-id").read_text().strip()
        db = sqlite3.connect(f"file:{T3_DIR / 'statev2.sqlite'}?mode=ro", uri=True, timeout=1)
        try:
            row = db.execute(
                "SELECT thread_id FROM orchestration_v2_projection_provider_threads"
                " WHERE json_extract(payload_json, '$.nativeThreadRef.nativeId') = ?"
                " AND thread_id IS NOT NULL"
                " ORDER BY updated_at DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        finally:
            db.close()
    except (OSError, sqlite3.Error):
        return ""
    if not env or not row:
        return ""
    return f"t3code://threads/{quote(env, safe='')}/{quote(row[0], safe='')}"


def _t3_open(session_id: str) -> bool:
    """Ask T3 Code to show the thread; 0.1 opens it, earlier versions only
    raise the window (pingdotgg/t3code#9745)."""
    link = _t3_thread_link(session_id)
    if not link:
        return False
    try:
        proc = subprocess.run(
            ["/usr/bin/open" if sys.platform == "darwin" else "xdg-open", link],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=5.0, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def focus(session_id: str) -> int:
    record = _find_sentinel(session_id)
    if not record:
        sys.stderr.write(f"codexbar_focus: no sentinel for session {session_id}\n")
        return 2

    pid = int(record.get("pid") or 0)
    candidates = _ancestor_pids(pid)
    if not candidates:
        sys.stderr.write("codexbar_focus: no ancestry — claude pid already gone\n")
        return 3

    host = record.get("host")
    if host == "t3code":
        if _t3_open(session_id) and not USE_KWIN:
            return 0
        candidates += [p for p in agents.t3_window_pids() if p not in candidates]

    if host == "tern":
        windows = [p for p in agents.tern_window_pids() if p not in candidates]
        if _tern_focus(pid):
            # The pane already shows; raising its window is best effort.
            if USE_KWIN:
                _kwin_activate(candidates + windows, record)
            return 0
        candidates += windows
    # Skip kitty branch if no kitty in the tree.
    elif any(agents._comm_of(p) == "kitty" for p in candidates) and _kitty_focus(candidates):
        return 0

    if USE_KWIN and _kwin_activate(candidates, record):
        return 0

    reason = "" if USE_KWIN else " (KWin is unavailable)"
    sys.stderr.write(
        f"codexbar_focus: couldn't focus pids={candidates} host={host}{reason}\n"
    )
    return 4


def main(argv: list[str]) -> int:
    global USE_KWIN
    if argv[:1] == ["--tern"]:
        USE_KWIN = False
        argv = argv[1:]
    if not argv:
        return 1
    state_paths.migrate()
    if argv[0] == "--reserve":
        return reserve(*argv[1:]) if len(argv) in (3, 4) else 1
    if argv[0] == "--release":
        return release(*argv[1:]) if len(argv) == 4 else 1
    if argv[0] in ("--launch", "--teleport"):
        if len(argv) != 3:
            return 1
        action = {"--launch": launch, "--teleport": teleport}[argv[0]]
        return action(argv[1], argv[2])
    if argv[0] == "--launch-all":
        if len(argv) != 2:
            return 1
        return launch_all(agents._parse_session_keys(argv[1]))
    arg = argv[0]
    prefix = "codexbar-agentview://focus/"
    if arg.startswith(prefix):
        session_id = arg[len(prefix):]
    else:
        session_id = arg
    # Strip trailing slash, query, fragment.
    for sep in ("/", "?", "#"):
        if sep in session_id:
            session_id = session_id.split(sep, 1)[0]
    return focus(session_id.strip())


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as exc:
        sys.stderr.write(f"codexbar_focus: {exc}\n")
        sys.exit(99)

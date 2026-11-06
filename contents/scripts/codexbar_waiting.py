#!/usr/bin/env python3
"""waitingfor: sessions set aside, what they wait for, and what happened since.

The single writer of waiting.json. Tern, the omp extension, tern-awui and Plasma
all go through this CLI or read waiting_view.json, so no surface holds logic.
Code watches the waits; `tick` runs one pass and exits, its caller schedules it.

Every command prints one JSON object. State changes run under a flock on
waiting.lock and replace waiting.json atomically. Commands and `gh` run outside
the lock: a tick claims the due checks, runs them, and merges each result only
if the wait's `rev` is unchanged, so a Done or a snooze during a slow check wins.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import errno
import fcntl
import json
import math
import os
import re
import shlex
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import BinaryIO

import state_paths

STATE_VERSION = 1
HISTORY_KEPT = 200
HISTORY_SHOWN = 20
MAX_ERRORS = 5
COMMAND_TIMEOUT_S = 60
DEFAULT_INTERVAL_S = 15 * 60
RELEASE_INTERVAL_S = 60 * 60
PR_POLL_S = 3 * 60
# How long a claimed check stays with the process that claimed it. A tick takes
# only what its workers can finish in that time; a check that still overruns is
# claimed again, and the claim's new `rev` drops the older result.
CHECK_CLAIM_S = 5 * 60
CHECK_WORKERS = 6
MAX_CHECKS_PER_TICK = CHECK_WORKERS * CHECK_CLAIM_S // (COMMAND_TIMEOUT_S + 5)
# A `sending` prompt older than this was left by a crash: offer it again.
SENDING_FRESH_S = 2 * 60
EXTENSION_FRESH_S = 15 * 60
EXTENSION_KEPT_S = 24 * 3600
# How long a menu request stands: a key press, not a wait. Later nobody is there.
MENU_REQUEST_FRESH_S = 15
CLOSED_RECORD_KEPT_S = 7 * 24 * 3600
ACTION_KEPT_S = 7 * 24 * 3600
MAX_DURATION_S = 10 * 366 * 24 * 3600
TICK_MIN_S = 15
TICK_MAX_S = 300
# Soon enough for a window that just opened, late enough for acks to land. Only
# fresh actions get this: without a window nobody will ack them.
TICK_OUTBOX_S = 30
OUTBOX_RECENT_S = 5 * 60
RECAP_CHARS = 600
NOTE_IN_LINE_CHARS = 80
SHORT_LABEL_CHARS = 24
LAST_PROMPT_CHARS = 300
BRANCH_TIMEOUT_S = 2

PR_CACHE_VERSION = 1
PR_MENTIONED_KEPT = 5
# Asking GitHub about a PR: how long an answer lasts, how many questions one run
# asks, and how long each may take. A merged or closed PR rarely changes.
PR_STALE_S = 10 * 60
PR_SETTLED_STALE_S = 6 * 3600
PR_REFRESH_MAX = 6
PR_GH_TIMEOUT_S = 10
PR_STATUS_FIELDS = ("title", "state", "review", "checks")
PR_CREATED_HEADER = "# Created Pull Request"
# Waited for under the state lock, so about 1 s at most.
PIN_LOCK_TRIES = 10
PIN_LOCK_DELAY_S = 0.1
# XXH64's primes.
_XXH_P1 = 0x9E3779B185EBCA87
_XXH_P2 = 0xC2B2AE3D27D4EB4F
_XXH_P3 = 0x165667B19E3779F9
_XXH_P4 = 0x85EBCA77C2B2AE63
_XXH_P5 = 0x27D4EB2F165667C5
_U64 = (1 << 64) - 1

# `scheduled`: a live session listed only for its /schedule jobs.
BALLS = ("on_me", "on_machine", "scheduled", "on_someone", "on_my_time")
BALL_RANK = {ball: rank for rank, ball in enumerate(BALLS)}
SIZE_RANK = {"S": 0, "M": 1, "L": 2}
PR_UNTIL = {
    "review_on_head": "Review on head",
    "ready": "Ready",
    "merged": "Merged",
    "released": "Released",
    "activity": "New activity",
}
# `release` is only an `add` shorthand: it stores a `command` wait.
KINDS = ("time", "command", "job", "pr", "release", "something", "later")
KIND_OPTIONS = {
    "time": {"at", "in_"},
    "command": {"argv", "interval", "predicate"},
    "job": {"close_on_success"},
    "pr": {"pr", "repo", "number", "until", "bots"},
    "release": {"repo", "interval"},
    "something": {"what"},
    "later": {"size"},
}
FEATURE_MENU = "menu"
# The set-aside items both menus show (the omp extension reads it too).
MENU_PATH = Path(__file__).resolve().with_name("waiting_menu.json")
MENU_TARGETS = ("session", "pane")

REASON_CLOSED = "session closed: resume it to deliver the prompt"
REASON_NO_EXTENSION = "omp extension not loaded"
REASON_QUESTION = "agent asks a question"
REASON_REVIEW = "ran, take a look"
REASON_OVERDUE = "past its deadline"
REASON_MENU_NO_EXTENSION = "omp extension not loaded in this session"
REASON_MENU_OLD_EXTENSION = "this session runs an older waitingfor extension: /restart it"

_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}")
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
# The negative lookahead keeps `/pull/12abc` out; `/files` or `#issuecomment-1`
# after the number stay the same PR.
_PR_URL = re.compile(
    r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)(?![0-9A-Za-z_])"
)
# Where a command stands in a shell line, or after eval's `!`. A search for it
# quotes it, and prose has words before it.
_GH_PR_CREATE = re.compile(
    r"(?:^|[;&|(!])\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*gh pr create\b", re.MULTILINE,
)
_DURATION = re.compile(r"(?:\d+[smhdw])+")
_DURATION_PART = re.compile(r"(\d+)([smhdw])")
_UNIT_S = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_CLOCK_TIME = re.compile(r"(?:[01]?[0-9]|2[0-3]):[0-5][0-9]")


class BadInput(Exception):
    """Reported as {"ok": false} with exit 2."""


class PinError(Exception):
    """session-pins.json can't be changed: the answer's `pin_error`, not a failure."""


# Time ------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now().astimezone().replace(microsecond=0)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _parse_time(text: str) -> datetime:
    """ISO 8601; local time without an offset. Python 3.10 reads no `Z`."""
    value = text.strip()
    if value.endswith(("Z", "z")):
        value = value[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(value).astimezone()
    except (ValueError, OverflowError, OSError):
        raise BadInput(f"bad time: {text!r}") from None


def _stamp(text: str | None) -> float | None:
    return None if text is None else _parse_time(text).timestamp()


def _parse_duration(text: str) -> int:
    value = text.strip().lower()
    if not _DURATION.fullmatch(value):
        raise BadInput(f"bad duration: {text!r}")
    seconds = sum(int(n) * _UNIT_S[unit] for n, unit in _DURATION_PART.findall(value))
    if not 0 < seconds <= MAX_DURATION_S:
        raise BadInput(f"duration out of range: {text!r}")
    return seconds


def _compact(seconds: float) -> str:
    """The largest whole unit: `45s`, `3h`, `2d`."""
    seconds = int(max(seconds, 0))
    for unit, size in (("w", 604800), ("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def _interval_text(seconds: int) -> str:
    """An interval without rounding: 5400 s is `90m`, not `1h`."""
    for unit, size in (("w", 604800), ("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size and seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def _clock(moment: datetime, now: datetime) -> str:
    moment = moment.astimezone(now.tzinfo)
    if moment.date() == now.date():
        return f"{moment:%H:%M}"
    return f"{moment:%b} {moment.day} {moment:%H:%M}"


# Files -----------------------------------------------------------------------


def _state_path() -> Path:
    return state_paths.state_dir() / "waiting.json"


def _view_path() -> Path:
    return state_paths.state_dir() / "waiting_view.json"


def _write_file(path: Path, text: str, durable: bool) -> None:
    """Replace `path` atomically; `durable` also fsyncs it and its folder."""
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.",
        suffix=".tmp", delete=False,
    ) as output:
        temporary = Path(output.name)
        try:
            os.fchmod(output.fileno(), 0o600)
            output.write(text)
            if durable:
                output.flush()
                os.fsync(output.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    if durable:
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def _empty_state() -> dict:
    return {
        "version": STATE_VERSION, "wait_seq": 0, "action_seq": 0, "boot_id": "",
        "sessions": {}, "panes": {}, "actions": [], "extensions": {},
        "extension_features": {}, "menu_seq": 0, "menu_requests": {},
    }


def _load_state() -> dict:
    """The state, or an error: unreadable state is never replaced by an empty one."""
    path = _state_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _empty_state()
    try:
        state = json.loads(text)
    except ValueError as error:
        raise RuntimeError(f"{path} is unreadable: {error}") from None
    if not isinstance(state, dict) or state.get("version") != STATE_VERSION:
        raise RuntimeError(f"{path} has an unknown format")
    return {**_empty_state(), **state}


@contextlib.contextmanager
def _transaction():
    """The state under the writer lock, saved on a clean exit if it changed."""
    state_paths.migrate()
    lock = os.open(state_paths.state_dir() / "waiting.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = _load_state()
        before = json.dumps(state)
        yield state
        after = json.dumps(state)
        if after != before:
            _write_file(_state_path(), json.dumps(state, indent=1), durable=True)
    finally:
        os.close(lock)


def _publish(
    state: dict, now: datetime, agents: "Agents | None" = None, omp: "Omp | None" = None,
) -> dict:
    """Build the view and write waiting_view.json; callers hold the lock."""
    view = _build_view(state, now, agents or _read_agents(), omp=omp)
    _write_file(_view_path(), json.dumps(view), durable=False)
    return view


def _mutate(
    apply, now: datetime | None = None,
    agents: "Agents | None" = None, omp: "Omp | None" = None,
) -> dict:
    """Run `apply(state, now)` in a transaction; its answer gains `view`."""
    now = now or _now()
    with _transaction() as state:
        answer = apply(state, now) or {}
        answer["ok"] = True
        answer["view"] = _publish(state, now, agents, omp)
    return answer


# Outside sources -------------------------------------------------------------


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


class Agents:
    """agents.json, by session id. `known` is False without a readable file:
    liveness is then unknown, which is not the same as every session closed."""

    def __init__(self, payload: object = None):
        self.known = isinstance(payload, dict)
        data = payload if isinstance(payload, dict) else {}
        self.boot_id = _text(data.get("bootId"))
        updated = data.get("updatedAt")
        self.updated_at = updated / 1000 if type(updated) in (int, float) else None
        self.live = self._by_session(data.get("agents"))
        self.closed = self._by_session(data.get("history"))

    @staticmethod
    def _by_session(rows: object) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and isinstance(row.get("sessionId"), str):
                out[row["sessionId"]] = row
        return out

    def pane_of(self, session: str) -> int | None:
        pane = self.live.get(session, {}).get("ternPane")
        return pane if type(pane) is int else None

    def session_of(self, pane: int) -> str | None:
        for session, row in self.live.items():
            if row.get("ternPane") == pane:
                return session
        return None


def _read_agents() -> Agents:
    try:
        text = (state_paths.state_dir() / "agents.json").read_text(encoding="utf-8")
        return Agents(json.loads(text))
    except (OSError, ValueError):
        return Agents()


class Omp:
    """Read-only facts from omp's agent directory, each read once per command."""

    def __init__(self):
        # omp reads PI_CODING_AGENT_DIR; both must agree on session-pins.json.
        configured = os.environ.get("PI_CODING_AGENT_DIR") or os.environ.get("OMP_AGENT_DIR")
        self.root = Path(configured) if configured else Path.home() / ".omp" / "agent"
        self.pins_path = self.root / "session-pins.json"
        self._pins: list[str] | None = None
        self._transcripts: dict[str, Path] | None = None

    def pins(self) -> list[str]:
        if self._pins is None:
            data = self._json(self.pins_path)
            rows = data if isinstance(data, list) else []
            self._pins = list(dict.fromkeys(
                sid for sid in rows if isinstance(sid, str) and _SESSION_ID.fullmatch(sid)
            ))
        return self._pins

    def fresh_pins(self) -> list[str]:
        """The file as it is now, for code that is about to change it or just did."""
        self._pins = None
        return self.pins()

    def schedules(self, session: str) -> list[dict]:
        """The /schedule extension's jobs; a missing file means none."""
        data = self._json(self.root / "schedule" / f"{session}.json")
        jobs = []
        for job in data if isinstance(data, list) else []:
            if not isinstance(job, dict) or type(job.get("dueAt")) not in (int, float):
                continue
            every = job.get("everyMs")
            every_s = round(every / 1000) if type(every) in (int, float) and every > 0 else None
            try:
                due = datetime.fromtimestamp(job["dueAt"] / 1000).astimezone()
            except (ValueError, OverflowError, OSError):
                continue
            jobs.append({
                "id": job.get("id"), "prompt": _text(job.get("prompt")),
                "every_s": every_s, "due_at": _iso(due),
            })
        jobs.sort(key=lambda job: job["due_at"])
        return jobs

    def transcript(self, session: str) -> Path | None:
        if self._transcripts is None:
            self._transcripts = {
                path.stem.partition("_")[2]: path
                for path in (self.root / "sessions").glob("*/*_*.jsonl")
            }
        return self._transcripts.get(session)

    def header(self, session: str) -> dict:
        """Title and folder from a transcript's first records."""
        out = {"title": "", "cwd": ""}
        path = self.transcript(session)
        if path is None:
            return out
        try:
            with open(path, encoding="utf-8", errors="replace") as lines:
                for _ in range(8):
                    line = lines.readline(1 << 20)
                    if not line:
                        break
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(record, dict):
                        continue
                    if record.get("type") == "title":
                        out["title"] = _text(record.get("title")) or out["title"]
                    elif record.get("type") == "session":
                        out["title"] = out["title"] or _text(record.get("title"))
                        out["cwd"] = _text(record.get("cwd"))
        except OSError:
            pass
        return out

    def titles_and_recaps(self, ids: list[str]) -> tuple[dict[str, str], dict[str, str]]:
        """history.db's titles and newest recaps, each table optional."""
        db = self.root / "history.db"
        if not ids or not db.is_file():
            return {}, {}
        marks = ",".join("?" * len(ids))
        titles: dict[str, str] = {}
        recaps: dict[str, str] = {}
        try:
            con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.5)
        except sqlite3.Error:
            return {}, {}
        try:
            with contextlib.suppress(sqlite3.Error):
                titles = {
                    sid: _text(title) for sid, title in con.execute(
                        f"SELECT session_id, title FROM session_titles WHERE session_id IN ({marks})",
                        ids,
                    )
                }
            with contextlib.suppress(sqlite3.Error):
                # Oldest first, so the newest recap of each session wins.
                for sid, recap in con.execute(
                    f"SELECT session_id, recap FROM session_recaps WHERE session_id IN ({marks})"
                    " ORDER BY created_at, id",
                    ids,
                ):
                    recap = " ".join(_text(recap).split())
                    if recap:
                        recaps[sid] = recap[:RECAP_CHARS]
        finally:
            con.close()
        return titles, recaps

    @staticmethod
    def _json(path: Path) -> object:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None


def _resume_text(session: str, cwd: str) -> str:
    """Same form as codexbar_agents._resume_command, which Launch checks against."""
    command = shlex.join(["omp", "--resume", session])
    return f"{shlex.join(['cd', '--', cwd])} && {command}" if cwd else command


class Facts:
    """What the surfaces show about a session: agents.json, then omp's own files."""

    def __init__(self, agents: Agents, omp: Omp | None = None):
        self.agents = agents
        self.omp = omp or Omp()
        self._titles: dict[str, str] = {}
        self._recaps: dict[str, str] = {}

    def prefetch(self, ids: list[str]) -> None:
        """One history.db read for every session the command will describe."""
        self._titles, self._recaps = self.omp.titles_and_recaps(
            [sid for sid in ids if sid not in self.agents.live]
        )

    def describe(self, session: str) -> dict:
        live = self.agents.live.get(session)
        row = live or self.agents.closed.get(session) or {}
        title = _text(row.get("windowTitle"))
        cwd = _text(row.get("cwd"))
        recap = _text(row.get("recap"))
        command = _text(row.get("resumeCommand"))
        if live is None:
            title = title or self._titles.get(session, "")
            recap = recap or self._recaps.get(session, "")
        if not title or not cwd or (live is None and not command):
            header = self.omp.header(session)
            title = title or header["title"]
            cwd = cwd or header["cwd"]
            if live is None and not command and self.omp.transcript(session) is not None:
                command = _resume_text(session, cwd)
        return {
            "title": title or Path(cwd).name or session[:8],
            "cwd": cwd or None,
            "project": Path(cwd).name or None,
            "recap": recap or None,
            "live": live is not None,
            "pane": self.agents.pane_of(session),
            "resume_command": None if live is not None else command or None,
        }


# omp's pin file --------------------------------------------------------------


def _rotl64(value: int, bits: int) -> int:
    return ((value << bits) | (value >> (64 - bits))) & _U64


def _xxh64_round(total: int, lane: int) -> int:
    return _rotl64((total + lane * _XXH_P2) & _U64, 31) * _XXH_P1 & _U64


def _xxh64(data: bytes, seed: int) -> int:
    """XXH64, which omp names its locks with; the standard library has none."""
    size, index = len(data), 0
    if size >= 32:
        lanes = [
            (seed + _XXH_P1 + _XXH_P2) & _U64, (seed + _XXH_P2) & _U64,
            seed, (seed - _XXH_P1) & _U64,
        ]
        while index + 32 <= size:
            for slot in range(4):
                word = int.from_bytes(data[index:index + 8], "little")
                lanes[slot] = _xxh64_round(lanes[slot], word)
                index += 8
        total = sum(_rotl64(lane, bits) for lane, bits in zip(lanes, (1, 7, 12, 18))) & _U64
        for lane in lanes:
            total = ((total ^ _xxh64_round(0, lane)) * _XXH_P1 + _XXH_P4) & _U64
    else:
        total = (seed + _XXH_P5) & _U64
    total = (total + size) & _U64
    while index + 8 <= size:
        total ^= _xxh64_round(0, int.from_bytes(data[index:index + 8], "little"))
        total = (_rotl64(total, 27) * _XXH_P1 + _XXH_P4) & _U64
        index += 8
    if index + 4 <= size:
        total ^= int.from_bytes(data[index:index + 4], "little") * _XXH_P1 & _U64
        total = (_rotl64(total, 23) * _XXH_P2 + _XXH_P3) & _U64
        index += 4
    for byte in data[index:]:
        total ^= byte * _XXH_P5 & _U64
        total = _rotl64(total, 11) * _XXH_P1 & _U64
    total ^= total >> 33
    total = total * _XXH_P2 & _U64
    total ^= total >> 29
    total = total * _XXH_P3 & _U64
    return total ^ (total >> 32)


def _omp_lock_name(lock_path: str) -> str:
    """pi-natives' `memory_lock_name`; omp must get this exact name."""
    data = lock_path.encode()
    return f"omp-file-lock-{_xxh64(data, 0x4f4d502d4c4f434b):016x}{_xxh64(data, 0x50492d46494c454c):016x}"


def _try_omp_lock(lock_path: str) -> socket.socket | BinaryIO | None:
    """One try at the lock omp's withFileLock takes, or None while it's held.
    On Linux that is an abstract socket and no file at all; elsewhere a flock on
    a sidecar that stays behind. Either one ends with its process."""
    if sys.platform == "linux":
        holder = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            holder.bind(b"\0" + _omp_lock_name(lock_path).encode())
        except OSError as error:
            holder.close()
            if error.errno == errno.EADDRINUSE:
                return None
            raise
        return holder
    holder = os.fdopen(os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600), "r+b")
    try:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        holder.close()
        if isinstance(error, BlockingIOError):
            return None
        raise
    return holder


def _replace_pins(path: Path, rows: list) -> None:
    temporary = Path(f"{path}.{os.getpid()}.{uuid.uuid4()}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with open(descriptor, "w", encoding="utf-8") as output:
            json.dump(rows, output, indent="\t", ensure_ascii=False)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _write_pin(path: Path, session: str, pinned: bool) -> None:
    """session-pins.json, as omp's updateSessionPin writes it: under its lock,
    from a fresh read, replaced atomically. Unlike omp this refuses a file it
    can't read, because a replacement would lose every other pin."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = f"{os.path.abspath(path)}.lock"
    holder = None
    for attempt in range(PIN_LOCK_TRIES):
        holder = _try_omp_lock(lock_path)
        if holder is not None:
            break
        if attempt + 1 < PIN_LOCK_TRIES:
            time.sleep(PIN_LOCK_DELAY_S)
    if holder is None:
        raise PinError(f"{path.name} is locked by another process")
    with holder:
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            rows = []
        except ValueError:
            raise PinError(f"{path.name} is unreadable") from None
        if not isinstance(rows, list):
            raise PinError(f"{path.name} is not a JSON array")
        if (session in rows) != pinned:
            _replace_pins(
                path, [*rows, session] if pinned else [row for row in rows if row != session],
            )


def _later_open(record: dict | None) -> bool:
    return bool(record) and any(wait["kind"] == "later" for wait in _open_waits(record))


def _move_pin(
    state: dict, session: str | None, want: bool | None, omp: Omp, now: datetime,
) -> dict:
    """Move the session's pin to `want` (None leaves it) and return the answer's
    `pinned`. Callers hold the state lock, so a Done that closes the wait also
    unpins, and no pin can land after it. What can't be written stays in
    `pin_wanted` for tick; a later `want` replaces it."""
    if session is None:
        return {"pinned": False}
    pinned = session in omp.fresh_pins()
    if want is None:
        return {"pinned": pinned}
    try:
        if want != pinned:
            _write_pin(omp.pins_path, session, want)
    except (OSError, PinError) as error:
        record = _ensure(state, session, None)
        record["pin_wanted"] = want
        _settle(state, record, now)
        return {"pinned": pinned, "pin_error": str(error)}
    record = state["sessions"].get(session)
    if record:
        record["pin_wanted"] = None
    return {"pinned": session in omp.fresh_pins()}


def _pin_pass(state: dict, omp: Omp, now: datetime) -> None:
    """Retry the pins that failed. A wait may have closed or opened since, and
    one file means one failure speaks for all: the state lock waits only once."""
    for session, record in state["sessions"].items():
        want = record.get("pin_wanted")
        if want is None:
            continue
        if want != _later_open(record):
            record["pin_wanted"] = None
        elif "pin_error" in _move_pin(state, session, want, omp, now):
            return


# Session PRs -----------------------------------------------------------------


def _pr_key(pr: dict) -> str:
    """GitHub ignores the case of a repo name: one PR is one row however it's typed."""
    return f"{pr['repo'].lower()}#{pr['number']}"


def _message_text(content: object) -> str:
    """A transcript message's text: a plain string, or its text blocks."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        block["text"] for block in content if isinstance(block, dict)
        and block.get("type") == "text" and isinstance(block.get("text"), str)
    )


def _note_pr(prs: list[dict], repo: str, number: int, source: str) -> None:
    """`created` outranks `mentioned`; the newest mention sits last."""
    new = {"repo": repo, "number": number, "source": source}
    key = _pr_key(new)
    known = next((pr for pr in prs if _pr_key(pr) == key), None)
    if known is None:
        prs.append(new)
    elif source == "created":
        known["source"] = "created"
    elif known["source"] == "mentioned":
        prs.remove(known)
        prs.append(known)
    for old in [pr for pr in prs if pr["source"] == "mentioned"][:-PR_MENTIONED_KEPT]:
        prs.remove(old)


def _runs_pr_create(arguments: object) -> bool:
    """Whether a tool call's shell command or code runs `gh pr create`."""
    if not isinstance(arguments, dict):
        return False
    return any(
        isinstance(arguments.get(key), str) and _GH_PR_CREATE.search(arguments[key])
        for key in ("command", "code")
    )


def _scan_line(line: bytes, prs: list[dict], calls: set[str]) -> None:
    """One transcript line. `calls` holds the tool calls that ran `gh pr create`
    and have no result yet. Other tool results stay out: search output is noise."""
    try:
        record = json.loads(line)
    except ValueError:
        return
    if not isinstance(record, dict) or record.get("type") != "message":
        return
    message = record.get("message")
    if not isinstance(message, dict):
        return
    role, content = message.get("role"), message.get("content")
    if role == "toolResult":
        text = _message_text(content)
        call = message.get("toolCallId")
        if isinstance(call, str) and call in calls:
            calls.remove(call)
            found = _PR_URL.findall(text)
        elif text.startswith(PR_CREATED_HEADER):
            # The URL follows the title; the PR's body may link other PRs.
            found = _PR_URL.findall(text)[:1]
        else:
            return
        for repo, number in found:
            _note_pr(prs, repo, int(number), "created")
    elif role in ("user", "assistant"):
        for repo, number in _PR_URL.findall(_message_text(content)):
            _note_pr(prs, repo, int(number), "mentioned")
        for block in content if role == "assistant" and isinstance(content, list) else ():
            if (
                isinstance(block, dict) and block.get("type") == "toolCall"
                and isinstance(block.get("id"), str) and _runs_pr_create(block.get("arguments"))
            ):
                calls.add(block["id"])


def _read_new_lines(entry: dict, transcript: Path) -> None:
    """Scan the whole lines past `offset`. Most lines mention no PR: only the
    ones that can matter pay for JSON parsing. A result without a URL still ends
    its call, so a pending call makes every tool result matter."""
    prs, calls = entry["prs"], set(entry["calls"])
    offset = entry["offset"]
    with open(transcript, "rb") as lines:
        lines.seek(offset)
        for line in lines:
            if not line.endswith(b"\n"):
                break  # Still being written; the next scan gets all of it.
            offset += len(line)
            if (
                b"/pull/" in line or b"gh pr create" in line
                or (calls and b'"toolResult"' in line)
            ):
                _scan_line(line, prs, calls)
    entry.update(offset=offset, calls=sorted(calls))


def _record_prs(record: dict | None) -> list[dict]:
    if record is None:
        return []
    return [
        {"repo": pr["repo"], "number": pr["number"], "source": "wait"}
        for pr in record["prs"]
    ]


def _valid_cached_pr(pr: object) -> bool:
    return (
        isinstance(pr, dict) and isinstance(pr.get("repo"), str)
        and type(pr.get("number")) is int and pr.get("source") in ("created", "mentioned")
    )


def _valid_cached_session(entry: object) -> bool:
    return (
        isinstance(entry, dict) and isinstance(entry.get("path"), str)
        and type(entry.get("offset")) is int and entry["offset"] >= 0
        and isinstance(entry.get("calls"), list)
        and all(isinstance(call, str) for call in entry["calls"])
        and isinstance(entry.get("prs"), list)
        and all(_valid_cached_pr(pr) for pr in entry["prs"])
    )


def _valid_cached_status(status: object) -> bool:
    if not isinstance(status, dict) or not isinstance(status.get("asked_at"), str):
        return False
    try:
        _parse_time(status["asked_at"])
    except BadInput:
        return False
    return True



class PrCache:
    """pr_cache.json: what each session's transcript showed up to `offset`, and
    what GitHub last said about each PR. No lock: it can be rebuilt, so a lost
    race only costs a rescan or a question to GitHub, and a row that can't be
    read is dropped rather than failing every command."""

    def __init__(self):
        self.path = state_paths.state_dir() / "pr_cache.json"
        self.sessions: dict[str, dict] = {}
        self.statuses: dict[str, dict] = {}
        self.dirty = False
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(data, dict) and data.get("version") == PR_CACHE_VERSION:
            sessions, statuses = data.get("sessions"), data.get("statuses")
            if isinstance(sessions, dict) and isinstance(statuses, dict):
                self.sessions = {
                    session: entry for session, entry in sessions.items()
                    if _valid_cached_session(entry)
                }
                self.statuses = {
                    key: status for key, status in statuses.items()
                    if _valid_cached_status(status)
                }

    def save(self) -> None:
        if self.dirty:
            with contextlib.suppress(OSError):
                self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                _write_file(self.path, json.dumps({
                    "version": PR_CACHE_VERSION, "sessions": self.sessions,
                    "statuses": self.statuses,
                }), durable=False)

    def _scan(self, session: str, transcript: Path | None) -> list[dict]:
        if transcript is None:
            return []
        entry = self.sessions.get(session)
        try:
            size = transcript.stat().st_size
        except OSError:
            return entry["prs"] if entry else []
        if entry is None or entry["path"] != str(transcript) or size < entry["offset"]:
            entry = {"path": str(transcript), "offset": 0, "prs": [], "calls": []}
            self.sessions[session] = entry
            self.dirty = True
        if size > entry["offset"]:
            try:
                _read_new_lines(entry, transcript)
            except OSError:
                return entry["prs"]
            self.dirty = True
        return entry["prs"]

    def _row(self, pr: dict) -> dict:
        status = self.statuses.get(_pr_key(pr), {})
        return {
            "repo": pr["repo"], "number": pr["number"],
            "url": f"https://github.com/{pr['repo']}/pull/{pr['number']}",
            "source": pr["source"],
            **{field: status.get(field) for field in PR_STATUS_FIELDS},
            "checked_at": status.get("checked_at"),
        }

    def session_prs(
        self, session: str, transcript: Path | None, record: dict | None,
    ) -> list[dict]:
        """One session's PRs: created, then wait, then mentioned, newest first."""
        scanned = self._scan(session, transcript)
        rows: dict[str, dict] = {}
        for pr in (
            *(pr for pr in scanned if pr["source"] == "created"),
            *_record_prs(record),
            *(pr for pr in reversed(scanned) if pr["source"] == "mentioned"),
        ):
            rows.setdefault(_pr_key(pr), pr)
        return [self._row(pr) for pr in rows.values()]

    def stale(
        self, prs: list[dict], now: datetime, *,
        sources: tuple[str, ...] = ("created", "wait"), settled_s: int = PR_SETTLED_STALE_S,
    ) -> list[dict]:
        """Which of `prs` to ask GitHub about: least recently asked first, within
        one run's bound, so a PR that keeps failing can't starve the others."""
        due: dict[str, tuple[float, dict]] = {}
        for pr in prs:
            status = self.statuses.get(_pr_key(pr), {})
            asked = _stamp(status.get("asked_at")) or 0.0
            limit = settled_s if status.get("state") in ("merged", "closed") else PR_STALE_S
            if pr["source"] in sources and now.timestamp() - asked >= limit:
                due[_pr_key(pr)] = (asked, pr)
        ordered = sorted(due.values(), key=lambda item: item[0])
        return [pr for _, pr in ordered[:PR_REFRESH_MAX]]

    def remember(self, pr: dict, status: dict | None, now: datetime) -> None:
        """`status` is None when GitHub didn't answer: the old one stays."""
        row = self.statuses.setdefault(_pr_key(pr), {})
        row["asked_at"] = _iso(now)
        if status is not None:
            row.update({field: status.get(field) for field in PR_STATUS_FIELDS})
            row["checked_at"] = _iso(now)
        self.dirty = True


def _pr_status_run(argv: list[str], **options: object) -> subprocess.CompletedProcess:
    return subprocess.run(argv, **{**options, "timeout": PR_GH_TIMEOUT_S})


def _refresh_prs(prs: list[dict], now: datetime) -> None:
    """Ask GitHub in parallel; call this outside the state lock. A status is
    only decoration, so a failure stays out of the command's answer."""
    if not prs:
        return

    def ask(pr: dict) -> dict | None:
        try:
            return _github().pr_status(pr["repo"], pr["number"], run=_pr_status_run)
        except Exception:
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(prs)) as pool:
        answers = list(pool.map(ask, prs))
    # Loaded only now: the calls take seconds, and another process may have
    # scanned a transcript meanwhile.
    cache = PrCache()
    for pr, answer in zip(prs, answers):
        cache.remember(pr, answer, now)
    cache.save()


# Records and waits -----------------------------------------------------------


def _new_record(session: str | None, pane: int | None) -> dict:
    return {
        "session": session, "pane": pane, "note": None, "suggested_note": None,
        "size": None, "set_aside_at": None, "unseen": False, "closed_at": None,
        "prs": [], "waits": [], "prompt_queue": [], "history": [], "pin_wanted": None,
    }


def _records(state: dict) -> list[dict]:
    return [*state["sessions"].values(), *state["panes"].values()]


def _lookup(state: dict, session: str | None, pane: int | None) -> dict | None:
    return state["sessions"].get(session) if session else state["panes"].get(str(pane))


def _ensure(state: dict, session: str | None, pane: int | None) -> dict:
    record = _lookup(state, session, pane)
    if record is None:
        record = _new_record(session, pane)
        if session:
            state["sessions"][session] = record
        else:
            state["panes"][str(pane)] = record
    return record


def _open_waits(record: dict) -> list[dict]:
    return [wait for wait in record["waits"] if wait["state"] != "done"]


def _wait_in(record: dict, wait_id: str) -> dict | None:
    return next((wait for wait in record["waits"] if wait["id"] == wait_id), None)


def _find_wait(state: dict, wait_id: str) -> tuple[dict, dict] | None:
    for record in _records(state):
        wait = _wait_in(record, wait_id)
        if wait is not None:
            return record, wait
    return None


def _log(record: dict, now: datetime, wait: dict | None, text: str) -> None:
    record["history"].append({
        "at": _iso(now), "wait": wait["id"] if wait else None, "text": text,
    })
    del record["history"][:-HISTORY_KEPT]


def _enqueue(state: dict, now: datetime, action: dict) -> None:
    state["action_seq"] += 1
    state["actions"].append({
        "id": f"a{state['action_seq']}", **action, "created_at": _iso(now),
    })


def _toast(state: dict, record: dict, wait: dict, now: datetime, level: str, text: str) -> None:
    if wait["toast"]:
        _enqueue(state, now, {
            "type": "toast", "level": level, "text": text, "sub": record["note"],
            "session": record["session"], "pane": record["pane"], "wait": wait["id"],
        })


def _touch(wait: dict) -> None:
    """A change a slow check that started earlier must not overwrite."""
    wait["rev"] += 1


def _new_wait(state: dict, now: datetime, **fields: object) -> dict:
    state["wait_seq"] += 1
    wait = {
        "id": f"w{state['wait_seq']}", "state": "pending", "created_at": _iso(now),
        "deadline": None, "deadline_in_s": None, "overdue": False, "on_me": False,
        "reason": None, "prompt": None, "verify": None, "fix": False, "toast": True,
        "close_on_success": False, "next": None, "rev": 1, "next_check_at": None,
        "refire_at": None, "errors": 0, "memo": {}, "snapshot": None,
        "verify_pending": False, "verify_claim_until": None, "verify_memo": {},
        "event_flag": False,
    }
    wait.update(fields)
    return wait


def _arm(wait: dict, now: datetime) -> None:
    """Start watching: times given relative to the start become absolute here,
    so a step in a chain counts from when the chain reaches it."""
    condition = wait["condition"]
    in_s = condition.pop("in_s", None)
    if in_s is not None:
        condition["at"] = _iso(now + timedelta(seconds=in_s))
    clock = condition.pop("clock", None)
    if clock is not None:
        condition["at"] = _next_clock(clock, now)
    if wait["deadline_in_s"] is not None:
        wait["deadline"] = _iso(now + timedelta(seconds=wait["deadline_in_s"]))
        wait["deadline_in_s"] = None
    if wait["kind"] in ("command", "pr"):
        wait["next_check_at"] = _iso(now)
    wait["state"] = "waiting"
    _touch(wait)


def _activate_next(record: dict, wait: dict, now: datetime) -> None:
    following_id = wait["next"]
    while following_id:
        following = _wait_in(record, following_id)
        if following is None:
            return
        if following["state"] == "pending":
            _arm(following, now)
            _log(record, now, following, f"Next: {following['label']}")
            return
        if following["state"] != "done":
            return
        following_id = following["next"]


def _cancel_prompts(record: dict, wait: dict) -> None:
    record["prompt_queue"] = [
        entry for entry in record["prompt_queue"] if entry["wait"] != wait["id"]
    ]


def _settle(state: dict, record: dict, now: datetime) -> None:
    """A record with no open wait leaves the view; a pane record goes at once."""
    if _open_waits(record):
        return
    if record["pane"] is not None:
        state["panes"].pop(str(record["pane"]), None)
    elif record["closed_at"] is None:
        record["closed_at"] = _iso(now)


def _complete(state: dict, record: dict, wait: dict, now: datetime) -> None:
    started = wait["state"] != "pending"
    wait["state"] = "done"
    wait.update(on_me=False, overdue=False, reason=None, verify_pending=False)
    _touch(wait)
    _cancel_prompts(record, wait)
    # A pending step that ends early only drops out: the step before it still
    # decides when the chain moves on.
    if started:
        _activate_next(record, wait, now)
    _settle(state, record, now)


def _prompt_text(wait: dict) -> str:
    text = wait["prompt"]
    if wait["fix"]:
        condition = wait["condition"]
        head = (wait["snapshot"] or {}).get("head") or "the current head"
        text = (
            text.replace("{repo}", condition["repo"])
            .replace("{number}", str(condition["number"]))
            .replace("{head}", head)
        )
    return text


def _fire(
    state: dict, record: dict, wait: dict, now: datetime,
    text: str | None = None, level: str = "info",
) -> None:
    """The condition held. With a prompt the wait stays with the machine; without
    one the step is over, so the chain moves on while the wait waits for my Done."""
    text = text or wait["label"]
    wait.update(on_me=False, overdue=False, reason=None, refire_at=None)
    _touch(wait)
    _log(record, now, wait, text)
    record["unseen"] = True
    if wait["prompt"]:
        wait["state"] = "acting"
        state["action_seq"] += 1
        record["prompt_queue"].append({
            "action": f"a{state['action_seq']}", "wait": wait["id"],
            "text": _prompt_text(wait), "state": "queued", "claimed_at": None,
        })
    else:
        wait["state"] = "fired"
        _activate_next(record, wait, now)
    _toast(state, record, wait, now, level, text)


def _attention(state: dict, record: dict, wait: dict, now: datetime, reason: str) -> None:
    wait["state"] = "needs_attention"
    wait.update(reason=reason, on_me=False, verify_pending=False)
    _touch(wait)
    _cancel_prompts(record, wait)
    _log(record, now, wait, reason)
    record["unseen"] = True
    _toast(state, record, wait, now, "error", f"{wait['label']}: {reason}")


def _flag_on_me(
    state: dict, record: dict, wait: dict, now: datetime, reason: str, level: str = "info",
) -> None:
    """The ball comes to me while the wait keeps its state."""
    if wait["on_me"] and wait["reason"] == reason:
        return
    wait.update(on_me=True, reason=reason)
    _log(record, now, wait, reason)
    record["unseen"] = True
    _toast(state, record, wait, now, level, f"{wait['label']}: {reason}")


def _clear_delivery_flags(record: dict) -> None:
    for wait in _open_waits(record):
        if wait["reason"] in (REASON_CLOSED, REASON_NO_EXTENSION):
            wait.update(on_me=False, reason=None)


def _extension_fresh(state: dict, session: str, now: datetime) -> bool:
    seen = _stamp(state["extensions"].get(session))
    return seen is not None and now.timestamp() - seen <= EXTENSION_FRESH_S


def _fresh_menu_requests(state: dict, now: datetime) -> dict:
    stamp = now.timestamp()
    return {
        session: request for session, request in state["menu_requests"].items()
        if 0 <= stamp - _stamp(request["at"]) < MENU_REQUEST_FRESH_S
    }


def _running(state: dict, agents: Agents, session: str) -> bool:
    """agents.json lags a resumed session by a sweep: an extension heard from
    since that sweep proves it runs."""
    if session in agents.live:
        return True
    seen = _stamp(state["extensions"].get(session))
    return (
        seen is not None and agents.updated_at is not None
        and seen >= math.floor(agents.updated_at)
    )


def _delivery_pass(state: dict, agents: Agents, now: datetime) -> None:
    """Who can take the queued prompts: a closed session or one without the
    extension puts the wait on me; the prompt stays queued for later."""
    if not agents.known:
        return
    for session, record in state["sessions"].items():
        queue = record["prompt_queue"]
        if not queue:
            continue
        live = _running(state, agents, session)
        if not live and queue[0]["state"] == "delivered":
            entry = queue.pop(0)
            wait = _wait_in(record, entry["wait"])
            if wait is not None:
                _attention(state, record, wait, now, "session closed before the prompt's turn ended")
        reason = None
        if not live:
            reason = REASON_CLOSED
        elif not _extension_fresh(state, session, now):
            reason = REASON_NO_EXTENSION
        for entry in list(record["prompt_queue"]):
            wait = _wait_in(record, entry["wait"])
            if wait is None:
                continue
            if reason:
                _flag_on_me(state, record, wait, now, reason)
            elif wait["reason"] in (REASON_CLOSED, REASON_NO_EXTENSION):
                wait.update(on_me=False, reason=None)


# Conditions ------------------------------------------------------------------


def _github():
    import waiting_github
    return waiting_github


def _validate_predicate(text: str) -> str:
    if text in ("exit0", "changed"):
        return text
    if text.startswith("stdout_regex:"):
        try:
            re.compile(text.partition(":")[2])
        except re.error as error:
            raise BadInput(f"bad regex: {error}") from None
        return text
    if text.startswith("json_eq:"):
        path, equals, value = text.partition(":")[2].partition("=")
        if not path or not equals:
            raise BadInput(f"bad predicate: {text!r}")
        try:
            json.loads(value)
        except ValueError:
            raise BadInput(f"bad JSON in predicate: {text!r}") from None
        return text
    raise BadInput(f"bad predicate: {text!r}")


def _json_path(data: object, path: str) -> tuple[bool, object]:
    for part in path.split("."):
        if isinstance(data, dict) and part in data:
            data = data[part]
        elif isinstance(data, list) and part.isdigit() and int(part) < len(data):
            data = data[int(part)]
        else:
            return False, None
    return True, data


def _run_argv(argv: list[str], cwd: str | None) -> tuple[int | None, str, str]:
    """(exit code, stdout, detail); no shell, and a timeout kills the whole group.
    `detail` is the error without an exit code, else stderr's last line."""
    try:
        process = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, errors="replace",
            start_new_session=True, cwd=cwd if cwd and os.path.isdir(cwd) else None,
        )
    except (OSError, ValueError) as error:
        return None, "", str(error)
    try:
        stdout, stderr = process.communicate(timeout=COMMAND_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.communicate(timeout=5)
        return None, "", f"timed out after {COMMAND_TIMEOUT_S} s"
    lines = stderr.strip().splitlines()
    return process.returncode, stdout, lines[-1][:200] if lines else ""


def _predicate(predicate: str, code: int, stdout: str) -> tuple[bool, str]:
    """(holds, error). A nonzero exit is "not yet" for `exit0` and for a regex
    (`grep` exits 1 on a miss); JSON from a failed command can't be judged."""
    if predicate == "exit0":
        return code == 0, ""
    if predicate.startswith("stdout_regex:"):
        return re.search(predicate.partition(":")[2], stdout) is not None, ""
    if code != 0:
        return False, f"exit {code}"
    path, _, value = predicate.partition(":")[2].partition("=")
    try:
        data = json.loads(stdout)
    except ValueError:
        return False, "command printed no JSON"
    found, actual = _json_path(data, path)
    expected = json.loads(value)
    # Python's 1 == True is not JSON's.
    same_type = isinstance(actual, bool) == isinstance(expected, bool)
    return found and same_type and actual == expected, ""


def _run_command(condition: dict) -> dict:
    code, stdout, detail = _run_argv(condition["argv"], condition.get("cwd"))
    if code is None:
        return {"holds": False, "error": detail}
    if condition["predicate"] == "changed":
        # The baseline lives on the wait; _apply_command compares against it.
        error = "" if code == 0 else f"exit {code}"
        return {
            "holds": False, "output": stdout.strip(),
            "error": f"{error}: {detail}" if error and detail else error,
        }
    holds, error = _predicate(condition["predicate"], code, stdout)
    return {"holds": holds, "error": f"{error}: {detail}" if error and detail else error}


def _run_verify(job: dict) -> dict:
    """{"ok", "reason", "transient"}: does the prompt's postcondition hold?"""
    verify = job["verify"]
    if verify["type"] == "command":
        result = _run_command(verify)
        reason = "verify command holds" if result["holds"] else (
            result["error"] or "verify command does not hold"
        )
        return {"ok": result["holds"], "reason": reason, "transient": False}
    github = _github()
    if verify["type"] == "pr_fix":
        # verify_fix reports a gh failure as a plain "no"; reading first tells
        # a fix that failed from a check that couldn't run.
        try:
            github.snapshot(verify["repo"], verify["number"])
        except Exception as error:
            return {
                "ok": False, "reason": str(error),
                "transient": getattr(error, "transient", True),
            }
        ok, reason = github.verify_fix(verify["repo"], verify["number"], job["snapshot"])
        return {"ok": ok, "reason": reason, "transient": False}
    polled = github.poll(verify, job["memo"])
    memo = polled.get("memo", job["memo"])
    if polled.get("error"):
        return {
            "ok": False, "reason": polled["error"], "memo": memo,
            "transient": bool(polled.get("transient")),
        }
    held = bool(polled.get("fired"))
    return {
        "ok": held, "transient": False,
        "reason": f"PR#{verify['number']} {PR_UNTIL[verify['until']].lower()}"
        + ("" if held else ": not yet"),
    }


def _run_job(job: dict, now: datetime) -> dict:
    try:
        if job["type"] == "command":
            return {**job, "result": _run_command(job["condition"])}
        if job["type"] == "verify":
            return {**job, "result": _run_verify(job)}
        github = _github()
        result = github.poll(job["condition"], job["memo"], now=now.timestamp())
        if result.get("fired") and job["fix"]:
            try:
                result["snapshot"] = github.snapshot(
                    job["condition"]["repo"], job["condition"]["number"]
                )
            except Exception:
                # Without a snapshot the fix ends in `review`, not lost.
                result["snapshot"] = None
        return {**job, "result": result}
    except Exception as error:
        if job["type"] == "verify":
            result = {"ok": False, "reason": str(error), "transient": False}
        else:
            result = {"error": str(error), "transient": False, "holds": False}
        return {**job, "result": result}


def _run_jobs(jobs: list[dict], now: datetime) -> list[dict]:
    if not jobs:
        return []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(jobs), CHECK_WORKERS)) as pool:
        return list(pool.map(lambda job: _run_job(job, now), jobs))


def _verify_job(wait: dict, now: datetime) -> dict:
    _touch(wait)
    wait["verify_claim_until"] = _iso(now + timedelta(seconds=CHECK_CLAIM_S))
    return {
        "type": "verify", "wait": wait["id"], "rev": wait["rev"], "verify": wait["verify"],
        "snapshot": wait["snapshot"], "memo": wait["verify_memo"],
    }


def _claim_verifies(record: dict, now: datetime) -> list[dict]:
    return [
        _verify_job(wait, now) for wait in record["waits"]
        if wait["state"] == "acting" and wait["verify_pending"]
        and (_stamp(wait["verify_claim_until"]) or 0) <= now.timestamp()
    ]


def _claim_checks(state: dict, now: datetime) -> list[dict]:
    """Take the due checks out of the next tick's way before running them."""
    jobs = []
    stamp = now.timestamp()
    for record in _records(state):
        jobs.extend(_claim_verifies(record, now))
        for wait in record["waits"]:
            if len(jobs) >= MAX_CHECKS_PER_TICK:
                return jobs
            if wait["state"] != "waiting" or wait["kind"] not in ("command", "pr"):
                continue
            if (_stamp(wait["next_check_at"]) or 0) > stamp:
                continue
            _touch(wait)
            job = {"wait": wait["id"], "rev": wait["rev"], "condition": wait["condition"]}
            if wait["kind"] == "command":
                job["type"] = "command"
                interval = wait["condition"]["interval_s"]
            else:
                job.update(type="pr", memo=wait["memo"], fix=wait["fix"])
                interval = PR_POLL_S
            # The result sets the real next check; this only holds the claim.
            claim = max(interval, CHECK_CLAIM_S)
            wait["next_check_at"] = _iso(now + timedelta(seconds=claim))
            jobs.append(job)
    return jobs


def _apply_command(state: dict, record: dict, wait: dict, result: dict, now: datetime) -> None:
    wait["next_check_at"] = _iso(now + timedelta(seconds=wait["condition"]["interval_s"]))
    if result["error"]:
        wait["errors"] += 1
        if wait["errors"] >= MAX_ERRORS:
            _attention(state, record, wait, now, f"Command failed {MAX_ERRORS} times: {result['error']}")
        return
    wait["errors"] = 0
    condition = wait["condition"]
    if condition["predicate"] == "changed":
        baseline, output = condition.get("baseline"), result["output"]
        if baseline is None:
            condition["baseline"] = output
            _log(record, now, wait, f"Baseline: {output or '(empty)'}")
        elif output != baseline:
            _fire(state, record, wait, now, f"{baseline} → {output}")
        return
    if result["holds"]:
        _fire(state, record, wait, now)


def _apply_poll(state: dict, record: dict, wait: dict, result: dict, now: datetime) -> None:
    wait["memo"] = result.get("memo", wait["memo"])
    poll_s = result.get("next_poll_s") or PR_POLL_S
    wait["next_check_at"] = _iso(now + timedelta(seconds=poll_s))
    for event in result.get("events") or []:
        if str(event.get("key", "")).startswith("head:") and wait["event_flag"]:
            # What the event said was about the old head.
            reason = REASON_OVERDUE if wait["overdue"] else None
            wait.update(on_me=False, reason=reason, event_flag=False)
        if event.get("on_me"):
            _flag_on_me(state, record, wait, now, event["text"], "error")
            wait["event_flag"] = True
        else:
            _log(record, now, wait, event["text"])
    error = result.get("error")
    if error:
        if not result.get("transient"):
            wait["errors"] += 1
            if wait["errors"] >= MAX_ERRORS:
                _attention(state, record, wait, now, f"GitHub failed {MAX_ERRORS} times: {error}")
        return
    wait["errors"] = 0
    # `confirm` needs nothing here: its on_me event told me, and the wait stays
    # watched, so a compare that later proves the release fires it.
    if result.get("fired"):
        wait["snapshot"] = result.get("snapshot", wait["snapshot"])
        _fire(state, record, wait, now, result.get("fire_text"))


def _apply_verify(state: dict, record: dict, wait: dict, result: dict, now: datetime) -> None:
    if result["transient"]:
        # Not counted, like a transient poll error; the next try backs off.
        wait["verify_memo"] = result.get("memo", wait["verify_memo"])
        wait["verify_claim_until"] = _iso(now + timedelta(seconds=PR_POLL_S))
    elif result["ok"]:
        _log(record, now, wait, f"Verified: {result['reason']}")
        record["unseen"] = True
        _toast(state, record, wait, now, "success", wait["label"])
        _complete(state, record, wait, now)
    else:
        _attention(state, record, wait, now, result["reason"])


def _merge(state: dict, finished: list[dict], now: datetime) -> None:
    """Apply slow results, dropping each one whose wait moved on meanwhile."""
    for job in finished:
        found = _find_wait(state, job["wait"])
        if found is None:
            continue
        record, wait = found
        if job["type"] == "verify":
            if wait["rev"] == job["rev"] and wait["state"] == "acting" and wait["verify_pending"]:
                _apply_verify(state, record, wait, job["result"], now)
        elif wait["rev"] == job["rev"] and wait["state"] == "waiting":
            apply = _apply_command if job["type"] == "command" else _apply_poll
            apply(state, record, wait, job["result"], now)


def _pane_closed(state: dict, pane: int, now: datetime) -> None:
    state["panes"].pop(str(pane), None)
    for record in list(state["sessions"].values()):
        for wait in list(record["waits"]):
            if _watches_job(wait, pane):
                _attention(state, record, wait, now, "Pane closed before the job finished")


def _watches_job(wait: dict, pane: int) -> bool:
    return (
        wait["kind"] == "job" and wait["state"] == "waiting"
        and wait["condition"]["pane"] == pane
    )


def _finish_job(state: dict, record: dict, wait: dict, status: int | None, now: datetime) -> None:
    if status not in (None, 0):
        _attention(state, record, wait, now, f"Exited with status {status}")
        return
    if status == 0 and wait["close_on_success"]:
        _enqueue(state, now, {
            "type": "close_pane", "pane": wait["condition"]["pane"], "wait": wait["id"],
        })
        if not wait["prompt"]:
            _log(record, now, wait, f"{wait['label']}: finished")
            _toast(state, record, wait, now, "success", f"{wait['label']}: finished")
            _complete(state, record, wait, now)
            return
    _fire(
        state, record, wait, now, f"{wait['label']}: finished",
        "success" if status == 0 else "info",
    )


def _housekeeping(state: dict, agents: Agents, omp: Omp, now: datetime) -> None:
    stamp = now.timestamp()
    if agents.boot_id:
        if state["boot_id"] and state["boot_id"] != agents.boot_id:
            # Pane ids don't survive a reboot, and may belong to another pane now.
            panes = {int(pane) for pane in state["panes"]} | {
                wait["condition"]["pane"]
                for record in state["sessions"].values() for wait in record["waits"]
                if wait["kind"] == "job" and wait["state"] == "waiting"
            }
            for pane in panes:
                _pane_closed(state, pane, now)
            state["actions"] = [
                action for action in state["actions"] if action["type"] == "toast"
            ]
        state["boot_id"] = agents.boot_id
    state["extensions"] = {
        session: seen for session, seen in state["extensions"].items()
        if stamp - _parse_time(seen).timestamp() <= EXTENSION_KEPT_S
    }
    state["extension_features"] = {
        session: features for session, features in state["extension_features"].items()
        if session in state["extensions"]
    }
    state["menu_requests"] = _fresh_menu_requests(state, now)
    pins = set(omp.pins())

    def keep(session: str, record: dict) -> bool:
        ended = record["closed_at"] or record["set_aside_at"]
        return (
            bool(_open_waits(record)) or session in pins or ended is None
            or stamp - _parse_time(ended).timestamp() <= CLOSED_RECORD_KEPT_S
        )

    state["sessions"] = {
        session: record for session, record in state["sessions"].items()
        if keep(session, record)
    }
    state["actions"] = [
        action for action in state["actions"]
        if stamp - _parse_time(action["created_at"]).timestamp() <= ACTION_KEPT_S
    ]


def _evaluate(state: dict, now: datetime) -> None:
    """Everything that needs only the clock."""
    stamp = now.timestamp()
    for record in _records(state):
        for wait in list(record["waits"]):
            if wait["state"] != "waiting":
                continue
            condition = wait["condition"]
            refire = _stamp(wait["refire_at"])
            if wait["kind"] == "time" and _stamp(condition["at"]) <= stamp:
                _fire(state, record, wait, now)
            elif refire is not None and refire <= stamp:
                _fire(state, record, wait, now)
            else:
                deadline = _stamp(wait["deadline"])
                if deadline is not None and deadline <= stamp and not wait["overdue"]:
                    wait["overdue"] = True
                    wait["reason"] = wait["reason"] or REASON_OVERDUE
                    _log(record, now, wait, f"{wait['label']}: {REASON_OVERDUE}")
                    record["unseen"] = True
                    _toast(state, record, wait, now, "error", f"{wait['label']}: {REASON_OVERDUE}")


def _next_tick_s(state: dict, now: datetime) -> int:
    stamp = now.timestamp()
    times: list[float | None] = []
    for record in _records(state):
        for wait in record["waits"]:
            if wait["state"] == "acting" and wait["verify_pending"]:
                times.append(_stamp(wait["verify_claim_until"]) or stamp)
            if wait["state"] != "waiting":
                continue
            times.extend((_stamp(wait["next_check_at"]), _stamp(wait["refire_at"])))
            if not wait["overdue"]:
                times.append(_stamp(wait["deadline"]))
            if wait["kind"] == "time":
                times.append(_stamp(wait["condition"]["at"]))
    delay = min((moment - stamp for moment in times if moment is not None), default=TICK_MAX_S)
    delay = min(max(math.ceil(delay), TICK_MIN_S), TICK_MAX_S)
    if any(
        stamp - _parse_time(action["created_at"]).timestamp() <= OUTBOX_RECENT_S
        for action in state["actions"]
    ):
        return min(delay, TICK_OUTBOX_S)
    return delay


def _public_action(action: dict) -> dict:
    return {key: value for key, value in action.items() if key not in ("wait", "created_at")}


# View ------------------------------------------------------------------------


def _wait_due(wait: dict) -> float | None:
    if wait["state"] == "pending":
        return None
    times = [_stamp(wait["deadline"]), _stamp(wait["refire_at"])]
    if wait["kind"] == "time":
        times.append(_stamp(wait["condition"]["at"]))
    return min((moment for moment in times if moment is not None), default=None)


def _wait_ball(wait: dict) -> str:
    if wait["state"] in ("fired", "review", "needs_attention") or wait["overdue"] or wait["on_me"]:
        return "on_me"
    if wait["state"] == "acting":
        return "on_machine"
    return {"something": "on_someone", "later": "on_my_time"}.get(wait["kind"], "on_machine")


def _summary(wait: dict, now: datetime) -> str:
    condition = wait["condition"]
    kind = wait["kind"]
    if kind == "pr":
        return f"PR#{condition['number']} {PR_UNTIL[condition['until']].lower()}"
    if kind == "time":
        if condition.get("at"):
            return f"until {_clock(_parse_time(condition['at']), now)}"
        if condition.get("clock"):
            return f"at {condition['clock']}"
        return f"for {_interval_text(condition['in_s'])}"
    if kind == "command" and condition.get("repo"):
        baseline = condition.get("baseline")
        return f"after {baseline}" if baseline else f"next release of {condition['repo']}"
    if kind == "command":
        return f"command {Path(condition['argv'][0]).name}"
    if kind == "job":
        return f"job in pane {condition['pane']}"
    if kind == "something":
        return condition["what"]
    return "when I have time"


def _wait_view(wait: dict, now: datetime) -> dict:
    due = _wait_due(wait)
    return {
        "id": wait["id"], "kind": wait["kind"], "label": wait["label"],
        "summary": _summary(wait, now), "state": wait["state"],
        "ball": _wait_ball(wait), "overdue": wait["overdue"],
        "reason": wait["reason"], "created_at": wait["created_at"],
        "due_at": None if due is None else _iso(datetime.fromtimestamp(due).astimezone()),
        "deadline": wait["deadline"], "condition": wait["condition"],
        "has_prompt": bool(wait["prompt"]), "next": wait["next"], "by": wait["by"],
    }


def _active(views: list[dict]) -> list[dict]:
    """Pending chain steps say nothing about who holds the ball now."""
    return [view for view in views if view["state"] != "pending"] or views


def _primary(views: list[dict]) -> dict:
    return min(_active(views), key=lambda view: (
        BALL_RANK[view["ball"]], _sort_time(view["due_at"]), view["created_at"],
    ))


def _sort_time(text: str | None) -> float:
    return math.inf if text is None else _stamp(text)


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _when(view: dict, now: datetime) -> str:
    """Time left while something is due, else how long it has been."""
    due = _sort_time(view["due_at"])
    if due != math.inf and due > now.timestamp():
        return f"in {_compact(due - now.timestamp())}"
    return _compact(now.timestamp() - _stamp(view["created_at"]))


def _schedule_text(schedules: list[dict], now: datetime) -> str:
    if not schedules:
        return ""
    job = schedules[0]
    every = f"every {_interval_text(job['every_s'])} · " if job["every_s"] else ""
    return f"↻ {every}next {_clock(_parse_time(job['due_at']), now)}"


def _headline(
    views: list[dict], note: str | None, pinned: bool, schedule: str, now: datetime,
) -> tuple[str, str, str]:
    """(status segment, line, composer row). The row leaves the schedule out:
    omp-schedule draws its own row above the composer."""
    status = line = ""
    if views:
        primary = _primary(views)
        on_me = primary["ball"] == "on_me"
        mark = "● " if on_me else "⏳ "
        if primary["kind"] == "pr":
            short = f"PR#{primary['condition']['number']}"
        else:
            short = _clip(primary["label"], SHORT_LABEL_CHARS)
        detail = (primary["reason"] if on_me else None) or primary["label"]
        age = _when(primary, now)
        parts = [short] + ([detail] if detail != short else []) + [age]
        if note:
            parts.append(f'"{_clip(note, NOTE_IN_LINE_CHARS)}"')
        status = f"{mark}{short} · {age}"
        line = mark + " · ".join(parts)
    elif pinned:
        status = "📌 pinned"
        line = status + (f' · "{_clip(note, NOTE_IN_LINE_CHARS)}"' if note else "")
    row = line
    if schedule:
        status = status or schedule
        line = f"{line} · {schedule}" if line else schedule
    return status, line, row


def _claimable(record: dict) -> dict | None:
    """The queue's head if a claim may take it. A delivered head's turn still
    runs, and an agent asking a question must not get a prompt for an answer."""
    queue = record["prompt_queue"]
    if not queue or queue[0]["state"] == "delivered":
        return None
    wait = _wait_in(record, queue[0]["wait"])
    return None if wait is not None and wait["reason"] == REASON_QUESTION else queue[0]


def _prompts_queued(record: dict | None, now: datetime) -> int:
    """Prompts a claim would hand out now: none while the head is on its way,
    so the extension's watcher can't claim one it is sending."""
    head = _claimable(record) if record else None
    if head is None:
        return 0
    if head["state"] == "sending":
        claimed = _stamp(head["claimed_at"]) or 0
        if now.timestamp() - claimed < SENDING_FRESH_S:
            return 0
    return len(record["prompt_queue"])


def _next_step(views: list[dict]) -> str | None:
    for view in views:
        if view["state"] == "needs_attention":
            return view["reason"]
    return next((view["label"] for view in views if view["state"] == "pending"), None)


def _waiting_for(views: list[dict]) -> str | None:
    """The target's free-text status (see `_free_text_waits`)."""
    return next((
        view["condition"]["what"] for view in views
        if view["kind"] == "something" and view["state"] != "pending"
    ), None)


def _history_view(record: dict | None) -> list[dict]:
    return record["history"][-HISTORY_SHOWN:] if record else []


def _session_view(
    session: str, record: dict | None, facts: Facts, pinned: bool,
    schedules: list[dict], state: dict, now: datetime, prs: PrCache,
) -> dict:
    info = facts.describe(session)
    views = [_wait_view(wait, now) for wait in _open_waits(record)] if record else []
    live = facts.agents.live.get(session) or {}
    agent_state = live.get("state")
    note = record["note"] if record else None
    if views:
        ball = min((view["ball"] for view in _active(views)), key=BALL_RANK.get)
    else:
        # Listed only for its /schedule jobs: those run on their own.
        ball = "on_my_time" if pinned else "scheduled" if schedules else "on_machine"
    status, line, composer_row = _headline(views, note, pinned, _schedule_text(schedules, now), now)
    dues = [view["due_at"] for view in _active(views) if view["due_at"]]
    return {
        "session_id": session, "title": info["title"], "cwd": info["cwd"],
        "project": info["project"], "live": info["live"], "pane": info["pane"],
        "tern_session": _text(live.get("ternSession")) or None,
        "agent_state": agent_state if agent_state in ("working", "blocked", "idle") else None,
        "extension": _extension_fresh(state, session, now),
        "resume_command": info["resume_command"],
        "note": note,
        "suggested_note": record["suggested_note"] if record else None,
        "size": record["size"] if record else None,
        "set_aside_at": record["set_aside_at"] if record else None,
        "pinned": pinned, "ball": ball, "unseen": bool(record and record["unseen"]),
        "status": status, "line": line, "composer_row": composer_row,
        "next_step": _next_step(views),
        "waiting_for": _waiting_for(views),
        "recap": info["recap"],
        "due_at": min(dues, key=_stamp) if dues else None,
        "prompts_queued": _prompts_queued(record, now),
        "waits": views, "schedules": schedules, "history": _history_view(record),
        "prs": prs.session_prs(session, facts.omp.transcript(session), record),
    }


def _pane_view(record: dict, now: datetime) -> dict:
    views = [_wait_view(wait, now) for wait in _open_waits(record)]
    status, line, _ = _headline(views, record["note"], False, "", now)
    dues = [view["due_at"] for view in _active(views) if view["due_at"]]
    return {
        "pane": record["pane"],
        "label": _primary(views)["label"] if views else None,
        "note": record["note"],
        "ball": min((view["ball"] for view in _active(views)), key=BALL_RANK.get),
        "status": status, "line": line, "waits": views,
        "waiting_for": _waiting_for(views),
        "history": _history_view(record),
        "due_at": min(dues, key=_stamp) if dues else None,
        "set_aside_at": record["set_aside_at"],
    }


def _sort_key(view: dict) -> tuple:
    return (
        BALL_RANK[view["ball"]], _sort_time(view["due_at"]), _sort_time(view["set_aside_at"]),
    )


def _build_view(
    state: dict, now: datetime, agents: Agents,
    only: str | None = None, omp: Omp | None = None,
) -> dict:
    facts = Facts(agents, omp)
    pins = set(facts.omp.pins())
    chosen = []
    for session in sorted({*state["sessions"], *pins, *agents.live}):
        if not _SESSION_ID.fullmatch(session):
            continue
        record = state["sessions"].get(session)
        has_waits = bool(record and _open_waits(record))
        if not (has_waits or session in pins or session in agents.live):
            continue
        schedules = facts.omp.schedules(session)
        if has_waits or session in pins or schedules:
            chosen.append((session, record, schedules))
    facts.prefetch([session for session, _, _ in chosen])
    prs = PrCache()
    sessions = [
        _session_view(session, record, facts, session in pins, schedules, state, now, prs)
        for session, record, schedules in chosen
    ]
    prs.save()
    sessions.sort(key=_sort_key)
    panes = [
        _pane_view(record, now) for record in state["panes"].values() if _open_waits(record)
    ]
    panes.sort(key=_sort_key)
    counts = dict.fromkeys(BALLS, 0)
    for view in [*sessions, *panes]:
        counts[view["ball"]] += 1
    if only is not None:
        sessions = [view for view in sessions if view["session_id"] == only]
        panes = []
    return {
        "ok": True, "now": _iso(now), "counts": counts, "sessions": sessions, "panes": panes,
        "menu_requests": _fresh_menu_requests(state, now),
    }


# Commands --------------------------------------------------------------------


def _target(args: argparse.Namespace) -> tuple[str | None, int | None]:
    if (args.session is None) == (args.pane is None):
        raise BadInput("give exactly one of --session and --pane")
    return args.session, args.pane


def _time_or_in(at: str | None, in_: str | None, name: str) -> tuple[str | None, int | None]:
    """(absolute ISO time, relative seconds) from a TIME/DUR option pair."""
    if at is not None and in_ is not None:
        raise BadInput(f"give only one {name} option")
    if at is not None:
        return _iso(_parse_time(at)), None
    return None, None if in_ is None else _parse_duration(in_)


def _next_clock(clock: str, now: datetime) -> str:
    """The next time after `now` the local clock shows `H:MM`. A day is counted on
    the wall clock, and `fold=1` is the second pass through an hour that repeats."""
    hour, minute = map(int, clock.split(":"))
    today = now.replace(tzinfo=None, hour=hour, minute=minute, second=0)
    moments = (today, today.replace(fold=1), today + timedelta(days=1))
    return _iso(next(moment for moment in map(datetime.astimezone, moments) if moment > now))


def _check_argv(argv: object, forms: str = "a JSON array of strings") -> list[str]:
    if not isinstance(argv, list) or not argv or not all(
        isinstance(part, str) and part for part in argv
    ):
        raise BadInput(f"argv needs {forms}")
    return argv


def _parse_argv(text: str) -> list[str]:
    """A JSON array, or a command line split like a shell would; it still runs
    without one. `[ -f x ]` is a command line: only a parsed list is an array."""
    forms = "a JSON array of strings or a plain command line"
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, list):
        return _check_argv(parsed, forms)
    try:
        return _check_argv(shlex.split(text), forms)
    except ValueError as error:
        raise BadInput(f"argv needs {forms}: {error}") from None


def _pr_reference(args: argparse.Namespace) -> tuple[str, int]:
    if args.pr is not None:
        if args.repo is not None or args.number is not None:
            raise BadInput("give --pr or --repo with --number, not both")
        parsed = _github().parse_pr(args.pr)
        if parsed is None:
            raise BadInput(f"not a PR reference: {args.pr!r}")
        return parsed
    if args.repo is None or args.number is None:
        raise BadInput("a PR needs --pr, or --repo with --number")
    if not _REPO.fullmatch(args.repo) or args.number <= 0:
        raise BadInput("bad --repo or --number")
    return args.repo, args.number


def _parse_verify(text: str) -> dict:
    try:
        verify = json.loads(text)
    except ValueError:
        raise BadInput("--verify needs a JSON object") from None
    if not isinstance(verify, dict):
        raise BadInput("--verify needs a JSON object")
    if verify.get("type") == "command":
        predicate = _validate_predicate(str(verify.get("predicate", "exit0")))
        if predicate == "changed":
            # A verify runs once; it has no earlier output to compare with.
            raise BadInput("a verify command can't use the changed predicate")
        return {"type": "command", "argv": _check_argv(verify.get("argv")), "predicate": predicate}
    if verify.get("type") == "pr":
        repo, number, until = verify.get("repo"), verify.get("number"), verify.get("until")
        valid_repo = isinstance(repo, str) and _REPO.fullmatch(repo)
        if not valid_repo or type(number) is not int or number <= 0:
            raise BadInput("a pr verify needs repo and number")
        # A verify reads the PR once, and `activity` needs a poll before it.
        if until not in PR_UNTIL or until == "activity":
            raise BadInput(f"bad verify until: {until!r}")
        out = {"type": "pr", "repo": repo, "number": number, "until": until}
        if isinstance(verify.get("bots"), list):
            out["bots"] = [str(bot) for bot in verify["bots"]]
        return out
    raise BadInput("--verify type must be command or pr")


def _default_label(kind: str, condition: dict) -> str:
    if kind == "command" and condition.get("repo"):
        return f"next release of {condition['repo']}"
    if kind == "command":
        return f"{Path(condition['argv'][0]).name} check"
    if kind == "job":
        return f"Job in pane {condition['pane']}"
    if kind == "pr":
        return PR_UNTIL[condition["until"]]
    if kind == "something":
        return condition["what"]
    return {"time": "Timer", "later": "When I have time"}[kind]


def _comma_list(text: str) -> list[str]:
    return [part.strip() for part in text.split(",") if part.strip()]


def _condition(args: argparse.Namespace) -> dict:
    kind = args.kind
    allowed = KIND_OPTIONS[kind]
    for name in set().union(*KIND_OPTIONS.values()) - allowed:
        if getattr(args, name) not in (None, False):
            raise BadInput(f"--{name.strip('_').replace('_', '-')} does not apply to {kind}")
    if kind == "time":
        clock = _CLOCK_TIME.fullmatch((args.at or "").strip())
        if clock and args.in_ is None:
            return {"type": "time", "at": None, "clock": clock[0]}
        at, in_s = _time_or_in(args.at, args.in_, "time")
        if at is None and in_s is None:
            raise BadInput("time needs --at or --in")
        return {"type": "time", "at": at} if at else {"type": "time", "at": None, "in_s": in_s}
    if kind == "command":
        if args.argv is None:
            raise BadInput("command needs --argv")
        return {
            "type": "command", "argv": _parse_argv(args.argv),
            "interval_s": _parse_duration(args.interval) if args.interval else DEFAULT_INTERVAL_S,
            "predicate": _validate_predicate(args.predicate or "exit0"),
        }
    if kind == "release":
        if args.repo is None or not _REPO.fullmatch(args.repo):
            raise BadInput("release needs --repo owner/repo")
        return {
            "type": "command",
            "argv": ["gh", "api", f"repos/{args.repo}/releases/latest", "--jq", ".tag_name"],
            "interval_s": _parse_duration(args.interval) if args.interval else RELEASE_INTERVAL_S,
            "predicate": "changed", "repo": args.repo,
        }
    if kind == "job":
        return {"type": "job", "pane": args.pane}
    if kind == "something":
        if not _text(args.what):
            raise BadInput("something needs --what")
        return {"type": "something", "what": _text(args.what)}
    if kind == "pr":
        repo, number = _pr_reference(args)
        condition = {
            "type": "pr", "repo": repo, "number": number, "until": args.until or "ready",
        }
        if args.bots:
            condition["bots"] = _comma_list(args.bots)
        return condition
    return {"type": kind}


def _begin(record: dict, omp: Omp, now: datetime) -> None:
    """A record whose waits all ended starts over: the old sentence, history and
    finished steps belong to the last time it was set aside. A pinned session
    keeps its sentence, the inventory shows it."""
    if record["closed_at"] is not None:
        record.update(closed_at=None, waits=[], set_aside_at=None)
        if record["session"] not in omp.pins():
            record.update(note=None, suggested_note=None, size=None, unseen=False, prs=[], history=[])
    record["set_aside_at"] = record["set_aside_at"] or _iso(now)


def _apply_note(record: dict, note: str | None, by: str) -> None:
    if not note:
        return
    if by == "user" or not record["note"]:
        record["note"] = note
    else:
        record["suggested_note"] = note


def _add_pr(record: dict, repo: str, number: int) -> None:
    if not any(pr["repo"] == repo and pr["number"] == number for pr in record["prs"]):
        record["prs"].append({"repo": repo, "number": number})


def _park(state: dict, record: dict, agents: Agents, now: datetime) -> None:
    session = record["session"]
    pane = agents.pane_of(session) if session else record["pane"]
    _enqueue(state, now, {"type": "park", "session": session, "pane": pane})


def _attach_after(record: dict, wait: dict, after: str | None, now: datetime) -> None:
    """Put `wait` at the end of `after`'s chain, or arm it when that has ended."""
    if after is None:
        _arm(wait, now)
        return
    tail = _wait_in(record, after)
    if tail is None:
        raise BadInput(f"unknown wait: {after}")
    while tail["next"] and _wait_in(record, tail["next"]) is not None:
        tail = _wait_in(record, tail["next"])
    tail["next"] = wait["id"]
    if tail["state"] in ("fired", "done"):
        _arm(wait, now)


def _free_text_waits(record: dict | None) -> list[dict]:
    """The target's free-text status: its started `something` wait. A pending
    chain step isn't a status yet; state from before the one-per-target rule
    may hold several."""
    if record is None:
        return []
    return [
        wait for wait in _open_waits(record)
        if wait["kind"] == "something" and wait["state"] != "pending"
    ]


def _set_something(
    state: dict, record: dict | None, what: str, label: str, by: str, now: datetime,
) -> dict | None:
    """New text for the target's free-text status; None when it has none. Extra
    ones left from older state close, so one remains."""
    waits = _free_text_waits(record)
    if not waits:
        return None
    wait = waits[0]
    for extra in waits[1:]:
        _log(record, now, extra, f"Done: {extra['label']}")
        _complete(state, record, extra, now)
    if what != wait["condition"]["what"]:
        wait["condition"]["what"] = what
        label = label or what
        _log(record, now, wait, f"Waiting for: {what}")
    wait["label"] = label or wait["label"]
    wait["by"] = by
    _touch(wait)
    return wait


def _clear_something(state: dict, record: dict | None, now: datetime) -> None:
    for wait in _free_text_waits(record):
        _log(record, now, wait, f"Done: {wait['label']}")
        _complete(state, record, wait, now)


def _move_deadline(
    record: dict, wait: dict, deadline: str | None, deadline_in_s: int | None, now: datetime,
) -> None:
    if deadline is None:
        deadline = _iso(now + timedelta(seconds=deadline_in_s))
    wait.update(deadline=deadline, deadline_in_s=None)
    if wait["overdue"] and _stamp(deadline) > now.timestamp():
        wait["overdue"] = False
        if wait["reason"] == REASON_OVERDUE:
            wait["reason"] = None
    _log(record, now, wait, f"Deadline moved to {_clock(_parse_time(deadline), now)}")


def _start_wait(
    state: dict, record: dict, omp: Omp, now: datetime, after: str | None, **fields: object,
) -> dict:
    _begin(record, omp, now)
    wait = _new_wait(state, now, **fields)
    record["waits"].append(wait)
    _attach_after(record, wait, after, now)
    return wait


def cmd_add(args: argparse.Namespace) -> dict:
    kind = args.kind
    agents = _read_agents()
    if kind == "job":
        session, pane = args.session, None if args.session else args.pane
        # From a session's own pane, the job is the one running there.
        args.pane = args.pane if args.pane is not None else agents.pane_of(args.session or "")
        if args.pane is None:
            raise BadInput("job needs --pane")
    else:
        session, pane = _target(args)
    condition = _condition(args)
    kind = condition["type"] if kind == "release" else kind
    if args.prompt and session is None:
        raise BadInput("a prompt needs --session")
    if args.prompt is not None and kind == "something":
        raise BadInput("something takes no prompt: it never fires")
    if args.verify and not args.prompt:
        raise BadInput("--verify needs --prompt")
    verify = _parse_verify(args.verify) if args.verify else None
    deadline, deadline_in_s = _time_or_in(args.deadline, args.deadline_in, "deadline")
    facts = Facts(agents)
    cwd = facts.describe(session)["cwd"] if session else None
    for check in (condition, verify):
        if check and check["type"] == "command" and cwd:
            check["cwd"] = cwd

    def apply(state: dict, now: datetime) -> dict:
        record = _ensure(state, session, pane)
        existed = _wait_in(record, args.after) if args.after else None
        if args.after and existed is None:
            raise BadInput(f"unknown wait: {args.after}")
        # A target has one free-text status: another one replaces its text. A
        # step chained with --after is no status yet, so it gets its own wait.
        wait = None
        if kind == "something" and not args.after:
            wait = _set_something(state, record, condition["what"], _text(args.label), args.by, now)
        if wait is not None:
            if deadline is not None or deadline_in_s is not None:
                _move_deadline(record, wait, deadline, deadline_in_s, now)
        else:
            after = None if record["closed_at"] is not None else args.after
            wait = _start_wait(
                state, record, facts.omp, now, after, kind=kind, condition=condition, by=args.by,
                label=_text(args.label) or _default_label(kind, condition),
                deadline=deadline, deadline_in_s=deadline_in_s,
                prompt=args.prompt or None, verify=verify, toast=not args.no_toast,
                close_on_success=args.close_on_success,
            )
        _apply_note(record, args.note, args.by)
        if args.size:
            record["size"] = args.size
        if kind == "pr":
            _add_pr(record, condition["repo"], condition["number"])
        if args.park:
            _park(state, record, agents, now)
        pin = _move_pin(state, session, True if kind == "later" else None, facts.omp, now)
        return {"session": session, "pane": pane, "wait": _wait_view(wait, now), **pin}

    return _mutate(apply, agents=agents, omp=facts.omp)


def cmd_babysit_pr(args: argparse.Namespace) -> dict:
    repo, number = _pr_reference(args)
    prompt = None if args.no_fix else _github().FIX_PROMPT
    agents = _read_agents()
    omp = Omp()

    def apply(state: dict, now: datetime) -> dict:
        record = _ensure(state, args.session, None)
        _begin(record, omp, now)
        steps = ["review_on_head", "ready", "merged"]
        if args.released:
            steps.append("released")
        waits = []
        for until in steps:
            condition = {"type": "pr", "repo": repo, "number": number, "until": until}
            wait = _new_wait(
                state, now, kind="pr", condition=condition, by=args.by,
                label=PR_UNTIL[until],
            )
            if waits:
                waits[-1]["next"] = wait["id"]
            waits.append(wait)
        if prompt:
            waits[0].update(
                prompt=prompt, fix=True,
                verify={"type": "pr_fix", "repo": repo, "number": number},
            )
        record["waits"].extend(waits)
        _arm(waits[0], now)
        _apply_note(record, args.note, args.by)
        _add_pr(record, repo, number)
        if args.park:
            _park(state, record, agents, now)
        return {
            "session": args.session, "pane": None, "wait": _wait_view(waits[0], now),
            "waits": [_wait_view(wait, now) for wait in waits],
        }

    return _mutate(apply, agents=agents, omp=omp)


def cmd_note(args: argparse.Namespace) -> dict:
    session, pane = _target(args)
    if args.accept_suggested == (args.text is not None):
        raise BadInput("give exactly one of --text and --accept-suggested")
    omp = Omp()

    def apply(state: dict, now: datetime) -> dict:
        record = _lookup(state, session, pane)
        if args.accept_suggested:
            if not record or not record["suggested_note"]:
                raise BadInput("no suggested note")
            note = record["suggested_note"]
        else:
            record = _ensure(state, session, pane)
            note = args.text
        _begin(record, omp, now)
        record["note"] = note
        if args.accept_suggested:
            record["suggested_note"] = None
        return {}

    return _mutate(apply)


def cmd_what(args: argparse.Namespace) -> dict:
    """The target's free-text status: one `something` wait, set, changed, or
    closed by empty text."""
    session, pane = _target(args)
    what = _text(args.text)
    agents = _read_agents()
    omp = Omp()

    def apply(state: dict, now: datetime) -> dict:
        record = _lookup(state, session, pane)
        if not what:
            _clear_something(state, record, now)
            return {"wait": None}
        wait = _set_something(state, record, what, "", "user", now)
        if wait is None:
            record = _ensure(state, session, pane)
            wait = _start_wait(
                state, record, omp, now, None, kind="something",
                condition={"type": "something", "what": what}, by="user", label=what,
            )
        if args.park:
            _park(state, record, agents, now)
        return {"wait": _wait_view(wait, now)}

    return _mutate(apply, agents=agents, omp=omp)


def _target_wait(record: dict | None, wait_id: str) -> dict:
    wait = _wait_in(record, wait_id) if record else None
    if wait is None or wait["state"] == "done":
        raise BadInput(f"unknown wait: {wait_id}")
    return wait


def cmd_done(args: argparse.Namespace) -> dict:
    session, pane = _target(args)
    omp = Omp()

    def apply(state: dict, now: datetime) -> dict:
        record = _lookup(state, session, pane)
        unpin = True
        if args.wait:
            wait = _target_wait(record, args.wait)
            _log(record, now, wait, f"Done: {wait['label']}")
            _complete(state, record, wait, now)
            # Another `later` wait still belongs in the inventory.
            unpin = wait["kind"] == "later" and not _later_open(record)
        elif record:
            for wait in _open_waits(record):
                wait["next"] = None
                _complete(state, record, wait, now)
            _settle(state, record, now)
        return _move_pin(state, session, False if unpin else None, omp, now)

    return _mutate(apply, omp=omp)


def cmd_snooze(args: argparse.Namespace) -> dict:
    session, pane = _target(args)
    delay = timedelta(seconds=_parse_duration(args.for_))

    def apply(state: dict, now: datetime) -> dict:
        record = _lookup(state, session, pane)
        wait = _target_wait(record, args.wait)
        if wait["state"] == "pending":
            raise BadInput(f"{args.wait} has not started yet")
        until = _iso(now + delay)
        condition = wait["condition"]
        if wait["overdue"]:
            wait.update(deadline=until, overdue=False)
        elif wait["kind"] == "time":
            condition["at"] = until
        elif wait["kind"] == "something":
            # A deadline is the only time it has, so a snooze sets one.
            wait["deadline"] = until
        elif wait["kind"] in ("command", "pr"):
            wait["next_check_at"] = until
        elif wait["kind"] == "job" and wait["state"] != "waiting":
            # Nothing would bring it back: its event already happened.
            wait["refire_at"] = until
        if (_stamp(wait["deadline"]) or math.inf) <= now.timestamp():
            wait["deadline"] = until
        _cancel_prompts(record, wait)
        wait.update(state="waiting", on_me=False, reason=None, errors=0, verify_pending=False)
        _touch(wait)
        _log(record, now, wait, f"Snoozed until {_clock(_parse_time(until), now)}")
        return {}

    return _mutate(apply)


def cmd_seen(args: argparse.Namespace) -> dict:
    def apply(state: dict, now: datetime) -> dict:
        record = state["sessions"].get(args.session)
        if record:
            record["unseen"] = False
        return {}

    return _mutate(apply)


def cmd_remove(args: argparse.Namespace) -> dict:
    session, pane = _target(args)

    def apply(state: dict, now: datetime) -> dict:
        if session:
            state["sessions"].pop(session, None)
        else:
            state["panes"].pop(str(pane), None)
        return {}

    return _mutate(apply)


def _settle_turn(state: dict, record: dict, now: datetime) -> None:
    """The turn after a delivered prompt ended: verify it, or hand it to me."""
    queue = record["prompt_queue"]
    if not queue or queue[0]["state"] != "delivered":
        return
    entry = queue.pop(0)
    wait = _wait_in(record, entry["wait"])
    if wait is None or wait["state"] != "acting":
        return
    wait.update(on_me=False, reason=None)
    verify = wait["verify"]
    if verify is None or (verify["type"] == "pr_fix" and not wait["snapshot"]):
        wait.update(state="review", reason=REASON_REVIEW)
        _touch(wait)
        _log(record, now, wait, f"{wait['label']}: {REASON_REVIEW}")
        record["unseen"] = True
        _toast(state, record, wait, now, "info", f"{wait['label']}: {REASON_REVIEW}")
    else:
        wait.update(verify_pending=True, verify_claim_until=None)


def cmd_event(args: argparse.Namespace) -> dict:
    kind = args.type
    if kind in ("job_finished", "pane_closed") and args.pane is None:
        raise BadInput(f"{kind} needs --pane")
    agents = _read_agents()
    session = args.session
    if session is None and args.pane is not None:
        session = agents.session_of(args.pane)
    if kind in ("turn_finished", "waiting_input") and args.session is None and args.pane is None:
        raise BadInput(f"{kind} needs --session or --pane")
    jobs: list[dict] = []

    def apply(state: dict, now: datetime) -> dict:
        record = state["sessions"].get(session) if session else None
        if kind == "turn_finished" and record:
            _settle_turn(state, record, now)
            for wait in _open_waits(record):
                if wait["reason"] == REASON_QUESTION:
                    wait.update(on_me=False, reason=None)
            jobs.extend(_claim_verifies(record, now))
        elif kind == "waiting_input" and record:
            for wait in _open_waits(record):
                if wait["state"] == "acting":
                    _flag_on_me(state, record, wait, now, REASON_QUESTION)
        elif kind == "job_finished":
            finished_jobs = [
                (held, wait) for held in _records(state) for wait in held["waits"]
                if _watches_job(wait, args.pane)
            ]
            for held, wait in finished_jobs:
                _finish_job(state, held, wait, args.status, now)
        elif kind == "pane_closed":
            _pane_closed(state, args.pane, now)
        return {}

    answer = _mutate(apply, agents=agents)
    if jobs:
        now = _now()
        finished = _run_jobs(jobs, now)
        answer = _mutate(lambda state, moment: _merge(state, finished, moment), now, agents)
    return answer


def cmd_heartbeat(args: argparse.Namespace) -> dict:
    features = _comma_list(args.features or "")

    def apply(state: dict, now: datetime) -> dict:
        state["extensions"][args.session] = _iso(now)
        state["extension_features"][args.session] = features
        record = state["sessions"].get(args.session)
        if record:
            _clear_delivery_flags(record)
        return {}

    return _mutate(apply)


def cmd_menu_request(args: argparse.Namespace) -> dict:
    """Tern can't draw in omp's composer: the session's extension opens the menu
    when it sees the request in the view."""
    now = _now()
    with _transaction() as state:
        if not _extension_fresh(state, args.session, now):
            return {"ok": True, "menu": False, "reason": REASON_MENU_NO_EXTENSION}
        if FEATURE_MENU not in state["extension_features"].get(args.session, ()):
            return {"ok": True, "menu": False, "reason": REASON_MENU_OLD_EXTENSION}
        state["menu_seq"] += 1
        state["menu_requests"][args.session] = {"seq": state["menu_seq"], "at": _iso(now)}
        return {"ok": True, "menu": True, "view": _publish(state, now)}


def cmd_claim_prompts(args: argparse.Namespace) -> dict:
    def apply(state: dict, now: datetime) -> dict:
        # Only a loaded extension claims, so this is as good as a heartbeat.
        state["extensions"][args.session] = _iso(now)
        record = state["sessions"].get(args.session)
        head = _claimable(record) if record else None
        if head is None:
            return {"prompts": []}
        resend_check = head["state"] == "sending"
        head.update(state="sending", claimed_at=_iso(now))
        wait = _wait_in(record, head["wait"])
        if wait is not None:
            wait.update(on_me=False, reason=None)
        return {"prompts": [{
            "action": head["action"], "wait": head["wait"], "text": head["text"],
            "resend_check": resend_check,
        }]}

    return _mutate(apply)


def _ack_prompt(
    state: dict, record: dict, entry: dict, args: argparse.Namespace, now: datetime,
) -> dict:
    wait = _wait_in(record, entry["wait"])
    if args.result == "done":
        raise BadInput(f"{args.action} is a prompt: ack delivered or failed")
    if args.result == "failed":
        record["prompt_queue"].remove(entry)
        if wait is not None:
            _attention(state, record, wait, now, args.error or "prompt delivery failed")
        return {}
    if entry["state"] == "delivered":
        return {"ignored": True}
    entry["state"] = "delivered"
    if wait is not None:
        wait.update(on_me=False, reason=None)
        _log(record, now, wait, f"Prompt delivered: {wait['label']}")
    return {}


def cmd_ack(args: argparse.Namespace) -> dict:
    def apply(state: dict, now: datetime) -> dict:
        for action in state["actions"]:
            if action["id"] != args.action:
                continue
            if args.result == "delivered":
                raise BadInput(f"{args.action} is a window action: ack done or failed")
            state["actions"].remove(action)
            found = _find_wait(state, action["wait"]) if action.get("wait") else None
            # A toast that didn't show is no reason to stop watching.
            if args.result == "failed" and action["type"] != "toast" and found:
                if found[1]["state"] != "done":
                    reason = args.error or f"{action['type']} failed"
                    _flag_on_me(state, found[0], found[1], now, reason, "error")
            return {}
        for record in state["sessions"].values():
            for entry in record["prompt_queue"]:
                if entry["action"] == args.action:
                    return _ack_prompt(state, record, entry, args, now)
        # A repeated ack, or the wait ended meanwhile.
        return {"ignored": True}

    return _mutate(apply)


def cmd_tick(args: argparse.Namespace) -> dict:
    now = _parse_time(args.now) if args.now else _now()
    agents = _read_agents()
    omp = Omp()

    def finish(state: dict, agents: Agents) -> dict:
        return {
            "ok": True,
            "actions": [_public_action(action) for action in state["actions"]],
            "next_tick_s": _next_tick_s(state, now),
            "view": _publish(state, now, agents, omp),
        }

    with _transaction() as state:
        _housekeeping(state, agents, omp, now)
        _pin_pass(state, omp, now)
        _evaluate(state, now)
        _delivery_pass(state, agents, now)
        jobs = _claim_checks(state, now)
        answer = None if jobs else finish(state, agents)
    if jobs:
        finished = _run_jobs(jobs, now)
        # The slow checks may have taken a minute: a session can have come back.
        agents = _read_agents()
        with _transaction() as state:
            _merge(state, finished, now)
            _delivery_pass(state, agents, now)
            answer = finish(state, agents)
    stale = PrCache().stale(
        [pr for session in answer["view"]["sessions"] for pr in session["prs"]], now,
    )
    if stale:
        _refresh_prs(stale, now)
        with _transaction() as state:
            answer = finish(state, agents)
    return answer


def cmd_resume(args: argparse.Namespace) -> dict:
    facts = Facts(_read_agents())
    if args.session in facts.agents.live:
        # A session file has one live writer.
        raise BadInput("session is running")
    facts.prefetch([args.session])
    info = facts.describe(args.session)
    if not info["resume_command"]:
        raise BadInput(f"no resume command for {args.session}")
    return {"ok": True, "command": info["resume_command"], "cwd": info["cwd"]}


def _from_ms(value: object) -> str | None:
    """An agents.json time in milliseconds; 0 means it isn't known."""
    if type(value) not in (int, float) or value <= 0:
        return None
    try:
        return _iso(datetime.fromtimestamp(value / 1000).astimezone())
    except (ValueError, OverflowError, OSError):
        return None


def _git_branch(cwd: str | None) -> str | None:
    if not cwd:
        return None
    try:
        done = subprocess.run(
            ["git", "-C", cwd, "branch", "--show-current"], stdin=subprocess.DEVNULL,
            capture_output=True, text=True, errors="replace", timeout=BRANCH_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return (done.stdout.strip() or None) if done.returncode == 0 else None


def cmd_card(args: argparse.Namespace) -> dict:
    """Works for any omp session, with or without a record, live or closed."""
    session = args.session
    state = _load_state()
    now = _now()
    agents = _read_agents()
    facts = Facts(agents)
    omp = facts.omp
    record = state["sessions"].get(session)
    row = agents.live.get(session) or agents.closed.get(session) or {}
    transcript = omp.transcript(session)
    pinned = session in omp.pins()
    if not (record or row or transcript or pinned):
        raise BadInput(f"unknown session: {session}")
    cache = PrCache()
    prs = cache.session_prs(session, transcript, record)
    cache.save()
    _refresh_prs(
        cache.stale(prs, now, sources=("created", "wait", "mentioned"), settled_s=PR_STALE_S),
        now,
    )
    cache = PrCache()
    facts.prefetch([session])
    view = _session_view(
        session, record, facts, pinned, omp.schedules(session), state, now, cache,
    )
    cache.save()
    prompt = _clip(_text(row.get("lastPrompt")), LAST_PROMPT_CHARS)
    return {"ok": True, "card": {
        **view,
        "provider": _text(row.get("provider")) or ("omp" if transcript else None),
        "model": _text(row.get("model")) or None,
        "agent_state_raw": row.get("state"),
        "started_at": _from_ms(row.get("startedAt")),
        "active_at": _from_ms(
            row.get("stateChangedAt") or row.get("updatedAt") or row.get("lastSeenAt")
        ),
        "last_prompt": prompt or None,
        "branch": _git_branch(view["cwd"]),
        "has_record": record is not None,
    }}


def _load_menu(path: Path) -> list[dict]:
    """The set-aside items, checked so neither menu draws a broken one."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise BadInput(f"menu file {path}: {error}") from error
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        raise BadInput(f"menu file {path}: needs a non-empty \"items\" list")
    ids = set()
    for number, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise BadInput(f"menu file {path}: item {number} is not an object")
        problem = None
        if not _text(item.get("id")) or item["id"] != item["id"].strip():
            problem = "needs an id without surrounding spaces"
        elif item["id"] in ids:
            problem = f"repeats the id {item['id']!r}"
        elif not _text(item.get("label")) or not _text(item.get("detail")):
            problem = "needs a label and a detail"
        elif any(key in item and not _text(item[key]) for key in ("command", "pane_detail")):
            problem = "has an empty or non-text command or pane_detail"
        elif not (
            isinstance(item.get("targets"), list) and item["targets"]
            and all(target in MENU_TARGETS for target in item["targets"])
        ):
            problem = f"needs targets from {', '.join(MENU_TARGETS)}"
        if problem:
            raise BadInput(f"menu file {path}: item {number} {problem}")
        ids.add(item["id"])
    return items


def cmd_menu(args: argparse.Namespace) -> dict:
    return {"ok": True, "items": _load_menu(MENU_PATH)}


def cmd_list(args: argparse.Namespace) -> dict:
    return _build_view(_load_state(), _now(), _read_agents(), only=args.session)


def cmd_inventory(args: argparse.Namespace) -> dict:
    state = _load_state()
    now = _now().timestamp()
    facts = Facts(_read_agents())
    pins = facts.omp.pins()
    facts.prefetch(pins)
    items = []
    for session in pins:
        record = state["sessions"].get(session)
        info = facts.describe(session)
        started = _stamp(record["set_aside_at"]) if record else None
        if started is None:
            transcript = facts.omp.transcript(session)
            with contextlib.suppress(OSError):
                started = transcript.stat().st_mtime if transcript else None
        items.append({
            "session_id": session, "title": info["title"], "cwd": info["cwd"],
            "project": info["project"], "note": record["note"] if record else None,
            "size": record["size"] if record else None,
            "age_s": None if started is None else max(int(now - started), 0),
            "recap": info["recap"], "live": info["live"], "pane": info["pane"],
            "resume_command": info["resume_command"],
        })
    items.sort(key=lambda item: (
        SIZE_RANK.get(item["size"], len(SIZE_RANK)),
        item["age_s"] is None, -(item["age_s"] or 0),
    ))
    return {"ok": True, "items": items}


# CLI -------------------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):
        raise BadInput(message)


def _session_arg(text: str) -> str:
    if not _SESSION_ID.fullmatch(text):
        raise argparse.ArgumentTypeError(f"bad session id: {text!r}")
    return text


def _pane_arg(text: str) -> int:
    try:
        pane = int(text)
    except ValueError:
        pane = -1
    if pane < 0:
        raise argparse.ArgumentTypeError(f"bad pane id: {text!r}")
    return pane


def _add_target(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--session", type=_session_arg)
    parser.add_argument("--pane", type=_pane_arg)


def _add_pr_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--pr")
    parser.add_argument("--repo")
    parser.add_argument("--number", type=int)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="codexbar_waiting.py", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)

    def command(name: str, run) -> argparse.ArgumentParser:
        sub = commands.add_parser(name)
        sub.set_defaults(run=run)
        return sub

    add = command("add", cmd_add)
    _add_target(add)
    add.add_argument("--kind", required=True, choices=KINDS)
    add.add_argument("--at")
    add.add_argument("--in", dest="in_")
    add.add_argument("--argv")
    add.add_argument("--interval")
    add.add_argument("--predicate")
    add.add_argument("--what")
    _add_pr_options(add)
    add.add_argument("--until", choices=tuple(PR_UNTIL))
    add.add_argument("--bots")
    add.add_argument("--size", choices=tuple(SIZE_RANK))
    add.add_argument("--label")
    add.add_argument("--note")
    add.add_argument("--by", choices=("user", "agent"), default="user")
    add.add_argument("--deadline")
    add.add_argument("--deadline-in")
    add.add_argument("--prompt")
    add.add_argument("--verify")
    add.add_argument("--no-toast", action="store_true")
    add.add_argument("--close-on-success", action="store_true")
    add.add_argument("--after")
    add.add_argument("--park", action="store_true")

    babysit = command("babysit-pr", cmd_babysit_pr)
    babysit.add_argument("--session", type=_session_arg, required=True)
    _add_pr_options(babysit)
    babysit.add_argument("--no-fix", action="store_true")
    babysit.add_argument("--released", action="store_true")
    babysit.add_argument("--note")
    babysit.add_argument("--by", choices=("user", "agent"), default="user")
    babysit.add_argument("--park", action="store_true")

    note = command("note", cmd_note)
    _add_target(note)
    note.add_argument("--text")
    note.add_argument("--accept-suggested", action="store_true")

    what = command("what", cmd_what)
    _add_target(what)
    what.add_argument("--text", required=True)
    what.add_argument("--park", action="store_true")

    done = command("done", cmd_done)
    _add_target(done)
    done.add_argument("--wait")

    snooze = command("snooze", cmd_snooze)
    _add_target(snooze)
    snooze.add_argument("--wait", required=True)
    snooze.add_argument("--for", dest="for_", required=True)

    for name, run in (
        ("seen", cmd_seen), ("card", cmd_card), ("menu-request", cmd_menu_request),
        ("claim-prompts", cmd_claim_prompts), ("resume", cmd_resume),
    ):
        command(name, run).add_argument("--session", type=_session_arg, required=True)

    heartbeat = command("heartbeat", cmd_heartbeat)
    heartbeat.add_argument("--session", type=_session_arg, required=True)
    heartbeat.add_argument("--features")

    remove = command("remove", cmd_remove)
    _add_target(remove)

    event = command("event", cmd_event)
    event.add_argument(
        "--type", required=True,
        choices=("turn_finished", "waiting_input", "job_finished", "pane_closed"),
    )
    _add_target(event)
    event.add_argument("--status", type=int)

    ack = command("ack", cmd_ack)
    ack.add_argument("--action", required=True)
    ack.add_argument("--result", required=True, choices=("delivered", "failed", "done"))
    ack.add_argument("--error")

    tick = command("tick", cmd_tick)
    tick.add_argument("--now")

    command("list", cmd_list).add_argument("--session", type=_session_arg)
    command("inventory", cmd_inventory)
    command("menu", cmd_menu)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        answer = args.run(args)
    except BadInput as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        return 2
    except Exception as error:
        traceback.print_exc()
        print(json.dumps({"ok": False, "error": str(error)}))
        return 1
    print(json.dumps(answer))
    return 0


if __name__ == "__main__":
    sys.exit(main())

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
import copy
import errno
import fcntl
import functools
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
# A wait that check errors put on me is tried again this often, silently.
RETRY_DEAD_S = 60 * 60
# How long GitHub or a release check may stay unreachable before the wait says so.
TRANSIENT_REPORT_S = 60 * 60
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
# A delivered prompt whose session has sat idle this long ran its turn, or never will.
SETTLE_IDLE_S = 2 * 60
# The sweep writes agents.json every few seconds: older than this, nobody sweeps.
AGENTS_STALE_S = 2 * 60
PROC = Path("/proc")
# How long a menu request stands: a key press, not a wait. Later nobody is there.
MENU_REQUEST_FRESH_S = 15
CLOSED_RECORD_KEPT_S = 7 * 24 * 3600
ACTION_KEPT_S = 7 * 24 * 3600
DONE_WAIT_KEPT_S = 7 * 24 * 3600
PR_CACHE_KEPT_S = 7 * 24 * 3600
# Toasts no window showed: past this many, the ones older than TOAST_FOLD_S fold into one.
TOAST_FOLD_COUNT = 3
TOAST_FOLD_S = 60 * 60
# How far a snooze from before `snooze_s` may lie ahead.
SNOOZE_BOUND_S = 24 * 3600
MAX_DURATION_S = 10 * 366 * 24 * 3600
TICK_MIN_S = 15
TICK_MAX_S = 300
# A daemon that ticked within this still ticks: another one's tick stands aside.
TICKER_LEASE_S = 2 * TICK_MAX_S
TICK_SKIPPED = "another Tern daemon ticks this state"
# A ticker's last tick dated further ahead than this was stamped before the clock went back.
TICKER_AHEAD_S = 60
# Soon enough for a window that just opened, late enough for acks to land. Only
# fresh actions get this: without a window nobody will ack them.
TICK_OUTBOX_S = 30
OUTBOX_RECENT_S = 5 * 60
RECAP_CHARS = 600
NOTE_IN_LINE_CHARS = 80
SHORT_LABEL_CHARS = 24
COMMAND_LABEL_CHARS = 60
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

# `on_timer`: nothing to do but wait for the clock: a time wait, or a repeat.
BALLS = ("on_me", "on_machine", "on_timer", "on_someone", "on_my_time")
BALL_RANK = {ball: rank for rank, ball in enumerate(BALLS)}
# Worst first; a wait with no outcome ranks after them all.
OUTCOMES = ("attention", "overdue", "fulfilled")
OUTCOME_RANK = {outcome: rank for rank, outcome in enumerate(OUTCOMES)}
OUTCOME_MARKS = {"attention": "⚠️ ", "overdue": "⏰ ", "fulfilled": "✅ "}
# A wait whose prompt the agent runs now: no outcome yet.
WORKING_MARK = "🔧 "
WORKING_TEXT = "agent on it"
# A prompt sent every interval: a `repeat` wait's mark, and before its timing
# wherever another kind of wait repeats one.
REPEAT_MARK = "↻ "
# The session line names this many threads besides its primary one.
OTHER_TRACKS_SHOWN = 2
SIZE_RANK = {"S": 0, "M": 1, "L": 2}
PR_UNTIL = {
    "review_on_head": "Review on head",
    "ready": "Ready",
    "merged": "Merged",
    "released": "Released",
    "activity": "New activity",
}
# `release` is only an `add` shorthand: it stores a `command` wait.
KINDS = ("time", "command", "job", "pr", "release", "something", "later", "repeat")
KIND_OPTIONS = {
    "time": {"at", "in_"},
    "command": {"argv", "interval", "predicate"},
    "job": {"close_on_success"},
    "pr": {"pr", "repo", "number", "until", "bots"},
    "release": {"repo", "interval"},
    "something": {"what"},
    "later": {"size"},
    # A repeat's `until` is the agent's judgment in words, a PR's is a state.
    "repeat": {"until"},
}
# The kinds a prompt may repeat on until they fire; `repeat` only repeats.
REPEAT_KINDS = ("repeat", "command", "pr", "release")
REPEAT_MIN_S = 60
REPEAT_MAX_S = 365 * 24 * 3600
# How long a resume the Tern window opened, or still has to open, may take to
# bring the session up before the wait comes back to me.
RESUME_GRACE_S = 3 * 60
# The omp extension heartbeats every 5 min; one heard from within this may
# still run, whatever agents.json says.
RESUME_QUIET_S = 6 * 60
FEATURE_MENU = "menu"
# The set-aside items both menus show (the omp extension reads it too).
MENU_PATH = Path(__file__).resolve().with_name("waiting_menu.json")
MENU_TARGETS = ("session", "pane")

REASON_CLOSED = "session closed: resume it to deliver the prompt"
REASON_RESUMING = "resuming the session in Tern"
# A resume waits for a Tern window to carry it out.
REASON_NO_WINDOW = "open a Tern window to reopen it"
# Leads a prompt sent again because a reboot cut its turn off.
REBOOT_NOTE = "(A reboot cut this prompt's earlier turn off. Check what was already done before you go on.)"
REASON_NO_EXTENSION = "omp extension not loaded"
REASON_QUESTION = "agent asks a question"
REASON_REVIEW = "ran, take a look"
REASON_OVERDUE = "past its deadline"
REASON_MENU_NO_EXTENSION = "omp extension not loaded in this session"
REASON_MENU_OLD_EXTENSION = "this session runs an older waitingfor extension: /restart it"
REASON_MENU_NO_PANE = "no omp extension in this pane"
# Who can take a queued prompt: these go away once the session can take it.
DELIVERY_REASONS = (REASON_CLOSED, REASON_NO_EXTENSION, REASON_RESUMING, REASON_NO_WINDOW)
# What "resuming" may say over; a closed session also over the deadline note.
# Anything else on me (an error, a PR's news) says more than either.
RESUMING_REPLACES = (None, REASON_QUESTION, *DELIVERY_REASONS)
CLOSED_REPLACES = (*RESUMING_REPLACES, REASON_OVERDUE)
# Actions that name a pane, which only the daemon that ran then knows.
PANE_ACTIONS = ("park", "close_pane")

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
        "extension_features": {}, "extension_panes": {}, "menu_seq": 0, "menu_requests": {},
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
def _writer_lock():
    state_paths.migrate()
    lock = os.open(state_paths.state_dir() / "waiting.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield
    finally:
        os.close(lock)


@contextlib.contextmanager
def _transaction():
    """The state under the writer lock, saved on a clean exit if it changed."""
    with _writer_lock():
        state = _load_state()
        before = json.dumps(state)
        yield state
        after = json.dumps(state)
        if after != before:
            _write_file(_state_path(), json.dumps(state, indent=1), durable=True)


def _publish(
    state: dict, now: datetime, agents: "Agents | None" = None, omp: "Omp | None" = None,
    ticked: bool = False, problems: list[str] = (),
) -> dict:
    """Build the view and write waiting_view.json; callers hold the lock."""
    view = _build_view(state, now, agents or _read_agents(), omp=omp, ticked=ticked, problems=problems)
    _write_file(_view_path(), json.dumps(view), durable=False)
    return view


def _error_line(error: BaseException) -> str:
    text = " ".join(str(error).split())
    return _clip(f"{type(error).__name__}: {text}" if text else type(error).__name__, 300)


def _last_health() -> dict:
    """The `health` waiting_view.json holds: a failed tick leaves its error only there."""
    try:
        health = json.loads(_view_path().read_text(encoding="utf-8")).get("health")
    except (OSError, ValueError, AttributeError):
        return {}
    return health if isinstance(health, dict) else {}


def _health(state: dict, now: datetime, error: str | None, ticked: bool) -> dict:
    """The view's `health`. Only a tick sets or clears `error` and `error_since`;
    any other view carries them over as they are, rows it couldn't build
    included (the next tick reports those)."""
    last = _last_health()
    last_error = last.get("error") if isinstance(last.get("error"), str) else None
    last_since = last.get("error_since") if isinstance(last.get("error_since"), str) else None
    if not ticked:
        error, since = last_error, last_since
    elif error:
        since = (last_error and last_since) or _iso(now)
    else:
        since = None
    return {
        "checked_at": state.get("last_tick_at"),
        "error": error,
        "error_since": since,
        "agents_stale_since": state.get("agents_stale_since"),
    }


def _report_tick_failure(error: BaseException) -> None:
    """A tick that failed says so in the view it leaves; waiting.json stays as it is."""
    with contextlib.suppress(Exception):
        with _writer_lock():
            path = _view_path()
            try:
                view = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                view = None
            view = view if isinstance(view, dict) else {}
            last = view.get("health") if isinstance(view.get("health"), dict) else {}
            health = {"checked_at": None, "agents_stale_since": None, **last}
            health.update(
                error=_error_line(error),
                error_since=(last.get("error") and last.get("error_since")) or _iso(_now()),
            )
            view["health"] = health
            _write_file(path, json.dumps(view), durable=False)


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

    def __init__(self, payload: object = None, foreign: bool = False):
        """`foreign`: the file is from another boot, so every session it lists has
        ended, and when it was swept says nothing about this boot."""
        self.foreign = foreign
        self.known = isinstance(payload, dict)
        data = payload if isinstance(payload, dict) else {}
        self.boot_id = _text(data.get("bootId"))
        updated = data.get("updatedAt")
        self.updated_at = updated / 1000 if type(updated) in (int, float) and not foreign else None
        self.live = self._by_session(data.get("agents"))
        self.closed = self._by_session(data.get("history"))
        if foreign:
            self.closed.update(self.live)
            self.live = {}

    def untrusted(self) -> "Agents":
        """These rows with no session taken for running: for a stale file."""
        agents = copy.copy(self)
        agents.live, agents.closed = {}, {**self.closed, **self.live}
        return agents

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
    """agents.json; one from before the reboot lists no session that runs now."""
    try:
        text = (state_paths.state_dir() / "agents.json").read_text(encoding="utf-8")
        payload = json.loads(text)
    except (OSError, ValueError):
        return Agents()
    agents = Agents(payload)
    boot = _system_boot_id()
    return Agents(payload, foreign=True) if boot and agents.boot_id and agents.boot_id != boot else agents


def _system_boot_id() -> str:
    """This boot's id, read as codexbar_agents.py reads it for agents.json, or
    "" where there is none."""
    import codexbar_agents
    return codexbar_agents._read_boot_id()


def _boot_id(agents: Agents) -> str:
    return _system_boot_id() or agents.boot_id


def _agents_stale(agents: Agents, now: datetime) -> bool:
    """Nobody has swept lately, or not since the reboot, or the file is missing
    or unreadable: who runs is unknown, and the view says so. A file that
    doesn't say when it was written can't be judged."""
    if agents.foreign or not agents.known:
        return True
    return agents.updated_at is not None and now.timestamp() - agents.updated_at > AGENTS_STALE_S


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

    def titles_and_recaps(self, ids: list[str]) -> tuple[dict[str, str], dict[str, tuple[str, int]]]:
        """history.db's titles and newest recaps with their `created_at` (seconds), each table optional."""
        db = self.root / "history.db"
        if not ids or not db.is_file():
            return {}, {}
        marks = ",".join("?" * len(ids))
        titles: dict[str, str] = {}
        recaps: dict[str, tuple[str, int]] = {}
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
                for sid, recap, created_at in con.execute(
                    f"SELECT session_id, recap, created_at FROM session_recaps WHERE session_id IN ({marks})"
                    " ORDER BY created_at, id",
                    ids,
                ):
                    recap = " ".join(_text(recap).split())
                    if recap:
                        recaps[sid] = (recap[:RECAP_CHARS], created_at if isinstance(created_at, int) else 0)
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
        self._recaps: dict[str, tuple[str, int]] = {}

    def prefetch(self, ids: list[str]) -> None:
        """One history.db read for every session the command will describe: titles and recaps
        of closed ones, and the time of every omp recap, which agents.json doesn't keep."""
        self._titles, self._recaps = self.omp.titles_and_recaps(ids)

    def describe(self, session: str) -> dict:
        live = self.agents.live.get(session)
        row = live or self.agents.closed.get(session) or {}
        title = _text(row.get("windowTitle"))
        cwd = _text(row.get("cwd"))
        recap = _text(row.get("recap"))
        command = _text(row.get("resumeCommand"))
        if live is None:
            title = title or self._titles.get(session, "")
        stored, seconds = self._recaps.get(session, ("", 0))
        if not recap and live is None:
            recap = stored
        # Both sides clip the same normalized text, so one is a prefix of the other. A recap
        # that isn't omp's (Claude Code's away summary) has no time.
        dated = bool(recap and stored) and (stored.startswith(recap) or recap.startswith(stored))
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
            # history.db's seconds; one out of range is no time, not a failed view.
            "recap_at": _from_ms(seconds * 1000) if dated else None,
            # omp's recap of an earlier exchange: the session has had turns since it was written.
            "recap_stale": bool(recap) and row.get("recapStale") is True,
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
        and (entry.get("used_at") is None or _valid_time(entry["used_at"]))
    )


def _valid_cached_status(status: object) -> bool:
    return isinstance(status, dict) and _valid_time(status.get("asked_at"))


def _valid_time(text: object) -> bool:
    if not isinstance(text, str):
        return False
    try:
        _parse_time(text)
    except BadInput:
        return False
    return True


def _recent(text: str | None, now: float) -> bool:
    """Used within PR_CACHE_KEPT_S; a row from before `used_at` is stamped now."""
    return text is None or now - _stamp(text) <= PR_CACHE_KEPT_S



class PrCache:
    """pr_cache.json: what each session's transcript showed up to `offset`, and
    what GitHub last said about each PR. No lock: it can be rebuilt, so a lost
    race only costs a rescan or a question to GitHub, and a row that can't be
    read is dropped rather than failing every command. A session nobody showed,
    and a PR nobody asked about, for PR_CACHE_KEPT_S go too."""

    def __init__(self, scan: bool = True, now: datetime | None = None):
        """Without `scan`, sessions keep what the last scan saved: no transcript is
        read. `now` is the command's time, `tick --now` included."""
        self.scan = scan
        self.path = state_paths.state_dir() / "pr_cache.json"
        self.sessions: dict[str, dict] = {}
        self.statuses: dict[str, dict] = {}
        self.dirty = False
        self.now = now or _now()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(data, dict) and data.get("version") == PR_CACHE_VERSION:
            sessions, statuses = data.get("sessions"), data.get("statuses")
            if isinstance(sessions, dict) and isinstance(statuses, dict):
                stamp = self.now.timestamp()
                self.sessions = {
                    session: entry for session, entry in sessions.items()
                    if _valid_cached_session(entry) and _recent(entry.get("used_at"), stamp)
                }
                self.statuses = {
                    key: status for key, status in statuses.items()
                    if _valid_cached_status(status) and _recent(status["asked_at"], stamp)
                }
                unstamped = [entry for entry in self.sessions.values() if "used_at" not in entry]
                for entry in unstamped:
                    entry["used_at"] = _iso(self.now)  # from before `used_at`: counts from now
                self.dirty = bool(unstamped) or (
                    len(self.sessions) < len(sessions) or len(self.statuses) < len(statuses)
                )

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
        if not self.scan:
            return entry["prs"] if entry else []
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
        """One session's PRs: created, then wait, then mentioned, newest first.

        A PR the session's own waits saw merged says so before the status cache's next refresh,
        so its badge never contradicts the wait that fired on the merge."""
        scanned = self._scan(session, transcript)
        entry = self.sessions.get(session)
        if entry is not None and (_stamp(entry.get("used_at")) or 0) < self.now.timestamp() - 3600:
            # Kept fresh to the hour: the cache isn't rewritten for every view.
            entry["used_at"] = _iso(self.now)
            self.dirty = True
        rows: dict[str, dict] = {}
        for pr in (
            *(pr for pr in scanned if pr["source"] == "created"),
            *_record_prs(record),
            *(pr for pr in reversed(scanned) if pr["source"] == "mentioned"),
        ):
            rows.setdefault(_pr_key(pr), pr)
        merged = {
            _pr_key(wait["condition"]) for wait in (record["waits"] if record else [])
            if wait["kind"] == "pr" and (
                (wait.get("memo") or {}).get("merged")
                or (wait["condition"].get("until") in ("merged", "released") and wait.get("fired_at"))
            )
        }
        views = [self._row(pr) for pr in rows.values()]
        for view in views:
            if _pr_key(view) in merged:
                view["state"] = "merged"
        return views

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
    cache = PrCache(now=now)
    for pr, answer in zip(prs, answers):
        cache.remember(pr, answer, now)
    cache.save()


# Records and waits -----------------------------------------------------------


def _new_record(session: str | None, pane: int | None) -> dict:
    return {
        "session": session, "pane": pane, "note": None, "suggested_note": None,
        "size": None, "set_aside_at": None, "unseen": False, "closed_at": None,
        "prs": [], "waits": [], "prompt_queue": [], "history": [], "pin_wanted": None,
        # The Tern session it last ran in, and its automatic resumes (`_resume`).
        # Records from before these have none: read them with `.get`.
        "tern_session": None, "resume_opened_at": None, "resume_prompts": [],
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
            record["daemon"] = _creator_daemon(state)
            state["panes"][str(pane)] = record
    return record


def _remember_tern_session(record: dict, agents: Agents) -> None:
    """Where a closed session's automatic resume opens it: the Tern session
    whose tab held it (`ternHome`), else the one showing it. A row outside Tern
    names none, which keeps the last one."""
    row = (agents.live.get(record["session"]) if record["session"] else None) or {}
    name = _text(row.get("ternHome")) or _text(row.get("ternSession"))
    if name:
        record["tern_session"] = name


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


def _log(
    record: dict, now: datetime, wait: dict | None, text: str, kind: str | None = None,
) -> None:
    """`kind` is "fired" or "attention", for the lines a surface highlights."""
    record["history"].append({
        "at": _iso(now), "wait": wait["id"] if wait else None, "text": text, "kind": kind,
    })
    del record["history"][:-HISTORY_KEPT]


def _ticking_daemon(state: dict) -> str | None:
    return (state.get("ticker") or {}).get("daemon")


def _proc_stat(pid: int | str, proc: Path = PROC) -> tuple[str, int, str] | None:
    """(name, parent pid, start time) of a process from /proc, or None."""
    try:
        stat = (proc / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    name_end = stat.rfind(")")
    fields = stat[name_end + 2:].split()
    try:
        return stat[stat.find("(") + 1:name_end], int(fields[1]), fields[19]
    except (IndexError, ValueError):
        return None


def _daemon_above(pid: int, proc: Path = PROC) -> str | None:
    """The `tern daemon` process above `pid`, as `<pid>-<start time>`. Tern's
    window app, tray, remote server and event listener are named `tern` too, and
    a window's commands run under the app: only argv[1] `daemon` counts."""
    while pid > 1:
        stat = _proc_stat(pid, proc)
        if stat is None:
            return None
        name, parent, start = stat
        if name == "tern":
            try:
                argv = (proc / str(pid) / "cmdline").read_bytes().split(b"\0")
            except OSError:
                argv = []
            if argv[1:2] == [b"daemon"]:
                return f"{pid}-{start}"
        pid = parent
    return None


@functools.lru_cache(maxsize=None)
def _ancestor_daemon() -> str | None:
    """The Tern daemon this process runs under (a pane's command, or the host's
    own), named as the host names it for `tick --daemon`. None outside Tern's
    daemon or without /proc."""
    return _daemon_above(os.getpid())


def _creator_daemon(state: dict) -> str | None:
    """Whose pane ids a new pane-scoped item names (`_panes_gone`): the daemon
    the command runs under, else the one that ticks."""
    return _ancestor_daemon() or _ticking_daemon(state)


def _enqueue(state: dict, now: datetime, action: dict) -> None:
    state["action_seq"] += 1
    if action["type"] in PANE_ACTIONS:
        action = {**action, "daemon": _creator_daemon(state)}
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
        "event_flag": False, "armed_at": None, "fired_text": None, "fired_at": None,
        "snoozed_from": None, "repeat": None,
        # Since when checks fail for a reason that passes (`_note_transient`).
        "transient_since": None, "transient_logged": False,
        # What `retry` restores of a wait check errors put on me (`_attention`).
        "retry_as": None,
        "done_at": None, "snooze_s": None,
    }
    wait.update(fields)
    if wait.get("kind") == "job":
        wait["daemon"] = _creator_daemon(state)
    return wait


def _arm(wait: dict, now: datetime) -> None:
    """Start watching: times given relative to the start become absolute here,
    so a step in a chain counts from when the chain reaches it. The relative
    ones stay, for when the chain reaches it again (`_disarm`)."""
    condition = wait["condition"]
    if condition.get("in_s") is not None:
        condition["at"] = _iso(now + timedelta(seconds=condition["in_s"]))
    if condition.get("clock") is not None:
        condition["at"] = _next_clock(condition["clock"], now)
    if wait["deadline_in_s"] is not None:
        wait["deadline"] = _iso(now + timedelta(seconds=wait["deadline_in_s"]))
    if wait["kind"] in ("command", "pr"):
        wait["next_check_at"] = _iso(now)
    repeat = wait.get("repeat")
    if repeat:
        repeat["next_at"] = _iso(now + timedelta(seconds=repeat["every_s"]))
        if repeat["for_s"] is not None:
            repeat["ends_at"] = _iso(now + timedelta(seconds=repeat["for_s"]))
    wait.update(state="waiting", armed_at=_iso(now))
    _checks_work(wait)
    _touch(wait)


def _disarm(record: dict, wait: dict) -> None:
    """Back to a chain step not reached yet: the times `_arm` counted from that
    moment are counted again when the chain reaches it, and a repeat sends no
    run until then."""
    condition = wait["condition"]
    if condition.get("in_s") is not None or condition.get("clock") is not None:
        condition["at"] = None
    if wait["deadline_in_s"] is not None:
        wait["deadline"] = None
    if wait.get("repeat"):
        _stop_repeat(record, wait)
        wait["repeat"]["ends_at"] = None
    wait.update(state="pending", next_check_at=None, armed_at=None)
    _touch(wait)


def _activate_next(record: dict, wait: dict, now: datetime) -> None:
    following_id = wait["next"]
    while following_id:
        following = _wait_in(record, following_id)
        if following is None:
            return
        if following["state"] == "pending":
            _arm_step(record, following, now)
            _log(record, now, following, f"Next: {following['label']}")
            return
        if following["state"] != "done":
            return
        following_id = following["next"]


def _cancel_prompts(record: dict, wait: dict) -> None:
    record["prompt_queue"] = [
        entry for entry in record["prompt_queue"] if entry["wait"] != wait["id"]
    ]


def _queue_prompt(
    state: dict, record: dict, wait: dict, text: str, now: datetime, **extra: object,
) -> str:
    state["action_seq"] += 1
    action = f"a{state['action_seq']}"
    record["prompt_queue"].append({
        "action": action, "wait": wait["id"], "text": text, "state": "queued",
        "claimed_at": None, "queued_at": _iso(now), **extra,
    })
    return action


def _queued(record: dict, action: str | None) -> bool:
    return action is not None and any(entry["action"] == action for entry in record["prompt_queue"])


def _cancel_queued_runs(record: dict, wait: dict) -> None:
    """Drop the repeat's runs the agent hasn't got, which then never counted;
    one it has runs to its end."""
    repeat = wait["repeat"]
    was_queued = _queued(record, repeat["running"])
    record["prompt_queue"] = [
        entry for entry in record["prompt_queue"]
        if entry["wait"] != wait["id"] or entry["state"] != "queued"
    ]
    if not _queued(record, repeat["running"]):
        if was_queued:
            repeat["runs"] -= 1
        repeat["running"] = None


def _stop_repeat(record: dict, wait: dict) -> None:
    """No more runs: the ones not sent go and nothing is due. A run the agent
    has keeps its place, so its turn's end settles it and nothing else."""
    _cancel_queued_runs(record, wait)
    wait["repeat"]["next_at"] = None


def _runs_text(count: int) -> str:
    return f"{count} run" if count == 1 else f"{count} runs"


def _repeat_spent(repeat: dict) -> bool:
    return repeat["times"] is not None and repeat["runs"] >= repeat["times"]


def _settle(state: dict, record: dict, now: datetime) -> None:
    """A record with no open wait leaves the view; a pane record goes at once."""
    if _open_waits(record):
        return
    if record["pane"] is not None:
        state["panes"].pop(str(record["pane"]), None)
    elif record["closed_at"] is None:
        record["closed_at"] = _iso(now)


def _complete(state: dict, record: dict, wait: dict, now: datetime) -> None:
    _finish(record, wait, now)
    _settle(state, record, now)


def _finish(record: dict, wait: dict, now: datetime, activate: bool = True) -> None:
    """Close one wait without settling the record: callers that keep another
    wait open need no `state`. `activate=False` closes it without moving its
    chain on."""
    started = wait["state"] != "pending"
    wait["state"] = "done"
    wait.update(on_me=False, overdue=False, reason=None, verify_pending=False, done_at=_iso(now))
    _touch(wait)
    _cancel_prompts(record, wait)
    # A pending step that ends early only drops out: the step before it still
    # decides when the chain moves on.
    if started and activate:
        _activate_next(record, wait, now)


def _arm_step(record: dict, wait: dict, now: datetime) -> None:
    """Arm a chain step. A `something` step becomes the target's one free-text
    status, so the one it replaces closes."""
    _arm(wait, now)
    if wait["kind"] != "something":
        return
    for other in _free_text_waits(record):
        if other is not wait:
            _log(record, now, other, f"Done: {other['label']}")
            _finish(record, other, now)


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
    text: str | None = None, level: str = "info", fired: str | None = None,
    ending: bool = False,
) -> None:
    """The condition held. With a prompt the wait stays with the machine; without
    one the step is over, so the chain moves on while the wait waits for my Done.
    `text` is the history's and the toast's, `fired` the surfaces' "what happened"
    when that reads differently. A repeat stops here; `ending` says `text`
    already tells how (`_end_repeat`)."""
    text = text or wait["label"]
    repeat = wait.get("repeat")
    if repeat:
        _stop_repeat(record, wait)
        if not ending:
            fired = fired or text
            text = f"{text} · repeat ended after {_runs_text(repeat['runs'])}"
    wait.update(
        on_me=False, overdue=False, reason=None, refire_at=None,
        fired_text=fired or text, fired_at=_iso(now),
        transient_since=None, transient_logged=False,
    )
    _touch(wait)
    _log(record, now, wait, text, "fired")
    record["unseen"] = True
    if wait["prompt"]:
        wait["state"] = "acting"
        _queue_prompt(state, record, wait, _prompt_text(wait), now)
    else:
        wait["state"] = "fired"
        _activate_next(record, wait, now)
    _toast(state, record, wait, now, level, text)


def _end_repeat(state: dict, record: dict, wait: dict, now: datetime, how: str = "ended") -> None:
    """The repeat is over, the condition it served aside: the wait fires with no
    prompt. Runs not sent go first, so the count says what ran."""
    _stop_repeat(record, wait)
    text = f"{wait['label']}: {how} after {_runs_text(wait['repeat']['runs'])}"
    _fire(state, record, wait, now, text, ending=True)


def _run_over(state: dict, record: dict, entry: dict, now: datetime) -> None:
    """A repeat's run is over: a skipped next run counts from now, and the last
    one ends the repeat."""
    wait = _wait_in(record, entry["wait"])
    repeat = wait.get("repeat") if wait else None
    if not repeat:
        return
    if repeat["running"] == entry["action"]:
        repeat["running"] = None
    if wait["state"] != "waiting":
        return
    if _repeat_spent(repeat):
        _end_repeat(state, record, wait, now)
    elif repeat["next_at"] is None:
        repeat["next_at"] = _iso(now + timedelta(seconds=repeat["every_s"]))


def _attention(
    state: dict, record: dict, wait: dict, now: datetime, reason: str, retry_as: str | None = None,
) -> None:
    """`retry_as` is "check" or "verify" when check errors brought it here: then
    `retry` can put it back, and `_claim_checks` tries it again now and then.
    "internal" (`_quarantine`) only `retry` puts back."""
    wait["state"] = "needs_attention"
    wait.update(reason=reason, on_me=False, verify_pending=False, retry_as=retry_as)
    _checks_work(wait)
    if retry_as == "check":
        wait["next_check_at"] = _iso(now + timedelta(seconds=RETRY_DEAD_S))
    elif retry_as == "verify":
        wait["verify_claim_until"] = _iso(now + timedelta(seconds=RETRY_DEAD_S))
    _touch(wait)
    _cancel_prompts(record, wait)
    _log(record, now, wait, reason, "attention")
    record["unseen"] = True
    _toast(state, record, wait, now, "error", f"{wait['label']}: {reason}")


def _flag_on_me(
    state: dict, record: dict, wait: dict, now: datetime, reason: str, level: str = "info",
    toast: bool = True,
) -> None:
    """The ball comes to me while the wait keeps its state."""
    if wait["on_me"] and wait["reason"] == reason:
        return
    wait.update(on_me=True, reason=reason)
    _log(record, now, wait, reason, "attention")
    record["unseen"] = True
    if toast:
        _toast(state, record, wait, now, level, f"{wait['label']}: {reason}")


INTERNAL_ERROR = "internal error: "
# The `retry_as` kinds `_claim_checks` tries again hourly; "internal" waits for `retry`.
CHECK_RETRIES = ("check", "verify")
# The time fields `_quarantine` clears when they don't parse, so the record can
# still be shown.
_WAIT_TIMES = ("deadline", "refire_at", "next_check_at", "verify_claim_until", "armed_at", "fired_at", "done_at")


@contextlib.contextmanager
def _isolated(state: dict, record: dict, now: datetime):
    """One record's step: a record that raises is put on me, and the tick goes on."""
    try:
        yield
    except Exception as error:  # a bad record, not a reason to stop every other one
        _quarantine(state, record, now, error)


def _quarantine(state: dict, record: dict, now: datetime, error: Exception) -> None:
    """Each open wait goes on me, once: a record that keeps raising says so only
    the first time."""
    reason = f"{INTERNAL_ERROR}{_error_line(error)}"
    waits = [wait for wait in record.get("waits") or [] if isinstance(wait, dict)]
    for wait in waits:
        # One whose text changes from tick to tick is still the same failure.
        if wait.get("state") == "done" or str(wait.get("reason")).startswith(INTERNAL_ERROR):
            continue
        for key in _WAIT_TIMES:
            try:
                _stamp(wait.get(key))
            except Exception:
                wait[key] = None
        # What `retry` puts it back to (`_rearm`).
        wait["quarantined"] = {
            key: wait.get(key) for key in ("state", "reason", "on_me", "verify_pending", "retry_as")
        }
        try:
            _attention(state, record, wait, now, reason, retry_as="internal")
        except Exception:  # the record is too broken for its prompts to be cancelled
            wait.update(state="needs_attention", reason=reason, on_me=False, retry_as="internal")
            with contextlib.suppress(Exception):
                _log(record, now, wait, reason, "attention")


def _clear_delivery_flags(record: dict) -> None:
    for wait in _open_waits(record):
        if wait["reason"] in DELIVERY_REASONS:
            wait.update(on_me=False, reason=None)


def _extension_fresh(state: dict, session: str, now: datetime) -> bool:
    seen = _stamp(state["extensions"].get(session))
    return seen is not None and now.timestamp() - seen <= EXTENSION_FRESH_S


def _extension_lost(state: dict, record: dict, now: datetime) -> bool:
    """A live session's extension is missing, not just quiet: silent for
    EXTENSION_FRESH_S and the queue's head waiting longer than a heartbeat, both
    counted from the wake when the machine slept since."""
    woke = _stamp(state.get("woke_at")) or 0.0
    heard = _stamp(state["extensions"].get(record["session"])) or 0.0
    queued = _stamp(record["prompt_queue"][0].get("queued_at")) or 0.0
    stamp = now.timestamp()
    return (
        stamp - max(heard, woke) > EXTENSION_FRESH_S
        and stamp - max(queued, woke) > RESUME_QUIET_S
    )


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


def _pending_resume(state: dict, session: str) -> dict | None:
    return next((
        action for action in state["actions"]
        if action["type"] == "resume" and action["session"] == session
    ), None)


def _drop_resume(state: dict, session: str) -> None:
    """A session that came back, or has nothing to send any more, needs no
    second omp: a session file has one live writer."""
    pending = _pending_resume(state, session)
    if pending is not None:
        state["actions"].remove(pending)


def _resume_opened_lately(record: dict, now: datetime) -> bool:
    opened = _stamp(record.get("resume_opened_at"))
    return opened is not None and now.timestamp() - opened < RESUME_GRACE_S


def _mark_resuming(record: dict) -> None:
    """An overdue wait keeps saying so: it is on me either way."""
    for entry in record["prompt_queue"]:
        wait = _wait_in(record, entry["wait"])
        if wait is not None and not wait["overdue"] and wait["reason"] in RESUMING_REPLACES:
            wait.update(on_me=False, reason=REASON_RESUMING)


def _flag_no_window(state: dict, record: dict, now: datetime) -> None:
    """Like `_mark_resuming`, but on me: the resume can't run without a window."""
    for entry in record["prompt_queue"]:
        wait = _wait_in(record, entry["wait"])
        if wait is not None and not wait["overdue"] and wait["reason"] in RESUMING_REPLACES:
            # No toast: only a window could show it, and by then it says otherwise.
            _flag_on_me(state, record, wait, now, REASON_NO_WINDOW, toast=False)


def _heard_lately(state: dict, session: str, now: datetime) -> bool:
    seen = _stamp(state["extensions"].get(session))
    return seen is not None and now.timestamp() - seen < RESUME_QUIET_S


def _resume(state: dict, record: dict, facts: Facts, now: datetime, window: bool) -> bool:
    """Have the Tern window resume a closed session whose prompts wait in its
    queue. True while that is under way; False when the prompts wait for me:
    the resume opened and the session didn't come up, failed, found no window,
    or there is no command. Each prompt gets one automatic resume."""
    session = record["session"]
    pending = _pending_resume(state, session)
    if pending is not None:
        # The countdown runs only while a window is up to carry the resume out;
        # a window going away restarts it from the next one.
        if not window:
            pending["window_since"] = None
            pending["windowless_since"] = pending.get("windowless_since") or _iso(now)
        else:
            pending["windowless_since"] = None
            if pending.get("window_since") is None:
                pending["window_since"] = _iso(now)
        windowless = _stamp(pending["windowless_since"])
        if windowless is not None and now.timestamp() - windowless >= RESUME_GRACE_S:
            # It stays queued for the window that comes up; until then I'm told.
            _flag_no_window(state, record, now)
            return True
    since = _stamp(pending.get("window_since")) if pending is not None else None
    if pending is not None and since is not None and now.timestamp() - since >= RESUME_GRACE_S:
        # A window was up all that time and didn't carry it out. One that comes
        # up much later would open a session nobody expects by then; a window
        # that wasn't up yet, as at login, doesn't count.
        state["actions"].remove(pending)
        return False
    queued = [entry["action"] for entry in record["prompt_queue"]]
    covered = record.get("resume_prompts") or []
    if pending is not None or _resume_opened_lately(record, now):
        # Prompts queued meanwhile wait for this resume, not one of their own.
        record["resume_prompts"] = list(dict.fromkeys([*covered, *queued]))
        _mark_resuming(record)
        return True
    if set(queued) <= set(covered):
        return False
    info = facts.describe(session)
    if not info["resume_command"]:
        return False
    if _heard_lately(state, session, now):
        # agents.json may not list a running omp yet, or list it as untracked:
        # until its extension has been quiet for a heartbeat, a second omp could
        # open on its session file. A running one claims the prompt meanwhile.
        _mark_resuming(record)
        return True
    head = record["prompt_queue"][0]["wait"]
    _enqueue(state, now, {
        "type": "resume", "session": session, "command": info["resume_command"],
        "cwd": info["cwd"], "tern_session": record.get("tern_session"), "wait": head,
        "window_since": _iso(now) if window else None,
        "windowless_since": None if window else _iso(now),
    })
    record.update(resume_prompts=queued, resume_opened_at=None)
    wait = _wait_in(record, head)
    _log(record, now, wait, f"Resuming the session to send: {wait['label'] if wait else 'a prompt'}")
    _mark_resuming(record)
    return True


def _delivery_pass(
    state: dict, agents: Agents, now: datetime, window: bool, stale: bool = False,
) -> None:
    """Who can take the queued prompts: a closed session is resumed in Tern, and
    comes to me when that doesn't bring it up; one without the extension comes
    to me once it has been missing a while. The prompts stay queued for later.
    While agents.json is `stale` only an extension heard from says anything."""
    if not agents.known:
        return
    facts = Facts(agents)
    for session, record in state["sessions"].items():
        with _isolated(state, record, now):
            _deliver(state, record, agents, facts, now, window, stale)


def _deliver(
    state: dict, record: dict, agents: Agents, facts: Facts, now: datetime,
    window: bool, stale: bool,
) -> None:
    session = record["session"]
    queue = record["prompt_queue"]
    live = _running(state, agents, session)
    fresh = live and _extension_fresh(state, session, now)
    if stale and not fresh:
        return
    if fresh:
        # Back for good: a later close may be resumed again at once.
        record["resume_opened_at"] = None
    if not live and queue and queue[0]["state"] == "delivered":
        entry = queue.pop(0)
        wait = _wait_in(record, entry["wait"])
        if entry.get("repeat"):
            # The repeat goes on: its next run resumes the session.
            if wait is not None:
                _log(record, now, wait, "The session closed during a run")
            _run_over(state, record, entry, now)
        elif wait is not None:
            _attention(state, record, wait, now, "session closed before the prompt's turn ended")
    # Cancelling prompts replaces the list.
    queue = record["prompt_queue"]
    if live or not queue:
        _drop_resume(state, session)
    if not queue:
        return
    if fresh:
        _clear_delivery_flags(record)
        return
    if live and _resume_opened_lately(record, now):
        # Up again, its extension about to say so.
        _mark_resuming(record)
        return
    if not live and _resume(state, record, facts, now, window):
        return
    if live and not _extension_lost(state, record, now):
        # Quiet, or the machine just woke: its next heartbeat may come yet.
        return
    reason = REASON_NO_EXTENSION if live else REASON_CLOSED
    for entry in list(queue):
        wait = _wait_in(record, entry["wait"])
        if wait is not None and (live or wait["reason"] in CLOSED_REPLACES):
            _flag_on_me(state, record, wait, now, reason)


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
            start_new_session=True, cwd=cwd,
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
    return found and _json_equal(actual, json.loads(value)), ""


def _json_equal(actual: object, expected: object) -> bool:
    """JSON's equality at every depth: Python's 1 == True is not JSON's."""
    if isinstance(actual, bool) or isinstance(expected, bool):
        return type(actual) is type(expected) and actual == expected
    if isinstance(expected, dict):
        return isinstance(actual, dict) and actual.keys() == expected.keys() and all(
            _json_equal(actual[key], item) for key, item in expected.items()
        )
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(
            map(_json_equal, actual, expected)
        )
    return not isinstance(actual, (dict, list)) and actual == expected


def _run_command(condition: dict) -> dict:
    cwd = condition.get("cwd")
    if not isinstance(cwd, str) or not cwd or not os.path.isabs(cwd):
        return {"holds": False, "error": "Command cwd is not a saved absolute directory; recreate this wait"}
    code, stdout, detail = _run_argv(condition["argv"], cwd)
    # A release check asks GitHub: a network or rate-limit failure passes.
    release = condition["predicate"] == "changed" and bool(condition.get("repo"))
    if code is None:
        return {
            "holds": False, "error": detail,
            "transient": release and _github().is_transient(detail),
        }
    if condition["predicate"] == "changed":
        if release and code != 0 and detail.endswith("(HTTP 404)"):
            # A repo without a release yet: nothing to compare, not a failure.
            return {"holds": False, "output": "", "error": ""}
        # The baseline lives on the wait; _apply_command compares against it.
        error = "" if code == 0 else f"exit {code}"
        return {
            "holds": False, "output": stdout.strip(),
            "error": f"{error}: {detail}" if error and detail else error,
            "transient": release and code != 0 and _github().is_transient(f"{detail}\n{stdout}"),
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
        # An error is a command that couldn't run or be judged, not a "no".
        return {
            "ok": result["holds"], "reason": reason, "error": bool(result["error"]),
            "transient": False,
        }
    github = _github()
    if verify["type"] == "pr_fix":
        try:
            ok, reason = github.verify_fix(verify["repo"], verify["number"], job["snapshot"])
        except Exception as error:  # a check that couldn't run, not a failed fix
            # Only GitHub's own failures pass; anything else is a bug that stays.
            return {
                "ok": False, "reason": str(error) or type(error).__name__, "error": True,
                "transient": isinstance(error, github.GhError) and bool(error.transient),
            }
        return {"ok": ok, "reason": reason, "transient": False}
    polled = github.poll(verify, job["memo"])
    memo = polled.get("memo", job["memo"])
    if polled.get("error"):
        return {
            "ok": False, "reason": polled["error"], "memo": memo, "error": True,
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
        if result.get("fired") and job["fix"] and job["type"] == "pr":
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
            result = {"ok": False, "reason": _error_line(error), "transient": False, "error": True}
        else:
            result = {"error": _error_line(error), "transient": False, "holds": False}
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


def _check_job(wait: dict, now: datetime, recheck: bool = False) -> dict:
    _touch(wait)
    # A wait whose snooze ran out is checked again: the bound `_clamp` keeps falls back.
    wait["snooze_s"] = None
    job = {"wait": wait["id"], "rev": wait["rev"], "condition": wait["condition"]}
    if wait["kind"] == "command":
        job["type"] = "command"
        interval = wait["condition"]["interval_s"]
    else:
        job.update(type="recheck" if recheck else "pr", memo=wait["memo"], fix=wait["fix"])
        interval = PR_POLL_S
    # The result sets the real next check; this only holds the claim.
    claim = max(interval, CHECK_CLAIM_S)
    wait["next_check_at"] = _iso(now + timedelta(seconds=claim))
    return job


def _retry_job(wait: dict, now: datetime) -> dict:
    """The hourly try of a wait check errors put on me (`_apply_retry`)."""
    if wait["retry_as"] == "verify":
        job = _verify_job(wait, now)
        wait["verify_claim_until"] = _iso(now + timedelta(seconds=RETRY_DEAD_S))
    else:
        job = _check_job(wait, now)
        wait["next_check_at"] = _iso(now + timedelta(seconds=RETRY_DEAD_S))
    return {**job, "retry": True}


def _claim_checks(state: dict, now: datetime) -> list[dict]:
    """Take the due checks out of the next tick's way before running them."""
    jobs = []
    stamp = now.timestamp()
    for record in _records(state):
        with _isolated(state, record, now):
            for wait in record["waits"]:
                if len(jobs) >= MAX_CHECKS_PER_TICK:
                    return jobs
                if wait["state"] == "needs_attention" and wait.get("retry_as"):
                    due = wait["verify_claim_until" if wait["retry_as"] == "verify" else "next_check_at"]
                    if wait["retry_as"] in CHECK_RETRIES and (_stamp(due) or 0) <= stamp:
                        jobs.append(_retry_job(wait, now))
                    continue
                if wait["state"] == "acting" and wait["verify_pending"]:
                    if (_stamp(wait["verify_claim_until"]) or 0) <= stamp:
                        jobs.append(_verify_job(wait, now))
                    continue
                recheck = _revocable(wait)
                if not recheck and (wait["state"] != "waiting" or wait["kind"] not in ("command", "pr")):
                    continue
                if (_stamp(wait["next_check_at"]) or 0) > stamp:
                    continue
                jobs.append(_check_job(wait, now, recheck))
    return jobs


def _transient_text(wait: dict, now: datetime) -> str:
    """Says what fails: a command check's own command, else GitHub. Only a PR
    verification fails transiently, so a verifying wait waits on GitHub."""
    since = _clock(_parse_time(wait["transient_since"]), now)
    verifying = wait["state"] == "acting" and wait["verify_pending"]
    if wait["kind"] == "command" and not verifying:
        return f"{Path(wait['condition']['argv'][0]).name} failing since {since}"
    return f"GitHub unreachable since {since}"


def _transient_reported(wait: dict, now: datetime) -> bool:
    since = _stamp(wait.get("transient_since"))
    return since is not None and now.timestamp() - since >= TRANSIENT_REPORT_S


def _note_transient(record: dict, wait: dict, now: datetime) -> None:
    """A failure that passes: retried, and told once when it has lasted."""
    if wait.get("transient_since") is None:
        wait["transient_since"] = _iso(now)
    elif not wait.get("transient_logged") and _transient_reported(wait, now):
        wait["transient_logged"] = True
        _log(record, now, wait, f"{wait['label']}: {_transient_text(wait, now)}")


def _checks_work(wait: dict) -> None:
    wait.update(transient_since=None, transient_logged=False)


def _rearm(wait: dict, now: datetime) -> None:
    """Back from check errors to what failed: its checks, or its verification.
    A quarantined wait goes back to what it was; if its record still raises, it
    is quarantined again."""
    if wait["retry_as"] == "internal":
        _unquarantine(wait, now)
        _touch(wait)
        return
    if wait["retry_as"] == "verify":
        wait.update(state="acting", verify_pending=True, verify_claim_until=None)
    else:
        wait.update(state="waiting", errors=0, next_check_at=_iso(now))
    wait.update(on_me=False, reason=None, retry_as=None)
    _checks_work(wait)
    _touch(wait)


def _unquarantine(wait: dict, now: datetime) -> None:
    before = wait.pop("quarantined", None) or {"state": "waiting"}
    wait.update(
        state=before["state"], reason=before.get("reason"), on_me=bool(before.get("on_me")),
        verify_pending=bool(before.get("verify_pending")), retry_as=before.get("retry_as"),
        errors=0,
    )
    if wait["state"] == "acting" and not wait["verify_pending"]:
        # The quarantine cancelled its prompt: whatever ran is mine to look at.
        wait.update(state="review", reason=REASON_REVIEW)
    elif wait["state"] == "acting":
        wait["verify_claim_until"] = None
    elif wait["state"] == "snoozed" and wait.get("refire_at") is None:
        wait["refire_at"] = _iso(now)  # cleared as unreadable: it comes back now
    elif wait["state"] == "waiting" and wait["kind"] in ("command", "pr"):
        wait["next_check_at"] = _iso(now)


def _apply_command(state: dict, record: dict, wait: dict, result: dict, now: datetime) -> None:
    wait["next_check_at"] = _iso(now + timedelta(seconds=wait["condition"]["interval_s"]))
    if result.get("transient"):
        _note_transient(record, wait, now)
        return
    if result["error"]:
        wait["errors"] += 1
        if wait["errors"] >= MAX_ERRORS:
            _attention(
                state, record, wait, now, f"Command failed {MAX_ERRORS} times: {result['error']}",
                retry_as="check",
            )
        return
    _checks_work(wait)
    wait["errors"] = 0
    condition = wait["condition"]
    if condition["predicate"] == "changed":
        baseline, output = condition.get("baseline"), result["output"]
        # A release check sees "" while the repo has none; that never fires it.
        release = bool(condition.get("repo"))
        if baseline is None:
            condition["baseline"] = output
            _log(record, now, wait, f"Baseline: {output or ('no release yet' if release else '(empty)')}")
        elif output != baseline and (output or not release):
            _fire(
                state, record, wait, now,
                f"{baseline or ('no release' if release else '(empty)')} → {output}",
                fired=f"{output} released" if release else None,
            )
        return
    if result["holds"]:
        exited = condition["predicate"] == "exit0"
        _fire(
            state, record, wait, now,
            fired="command exited 0" if exited else "command output matched",
        )


def _merge_fires(wait: dict) -> bool:
    """A merge fires a `merged` wait that hasn't fired: one back on me after
    its prompt ran stays there, and a snoozed one stays snoozed."""
    return wait["condition"]["until"] == "merged" and (
        wait["state"] in ("pending", "waiting")
        or wait["state"] == "needs_attention" and not wait.get("fired_at")
    )


def _revocable(wait: dict) -> bool:
    """A fired `review_on_head` or `ready` that waits for my Done: it was true of
    one head, so it stays watched until then."""
    return (
        wait["state"] == "fired" and wait["kind"] == "pr" and bool(wait.get("fired_head"))
        and wait["condition"]["until"] in ("review_on_head", "ready")
    )


def _apply_recheck(state: dict, record: dict, wait: dict, result: dict, now: datetime) -> None:
    """Take a fired wait back when the PR moved under it: a new head, or, for
    `ready`, a branch that can't merge. A review stays true of its head however
    the base moves. A PR closed without merging brings the wait back to me.
    Other events are left to the poll that runs next."""
    wait["next_check_at"] = _iso(now + timedelta(seconds=result.get("next_poll_s") or PR_POLL_S))
    if result.get("merged"):
        _pr_merged(state, wait["condition"], now)
        return
    closed = next((
        event for event in result.get("events") or []
        if str(event.get("key", "")).startswith("closed:")
    ), None)
    if closed is not None:
        _attention(state, record, wait, now, closed["text"])
        return
    if result.get("error") or not result.get("head"):
        return
    head, merge_state = result["head"], result.get("merge_state")
    base = result.get("base") or "its base"
    ready = wait["condition"]["until"] == "ready"
    if head != wait["fired_head"]:
        why = f"new head {head[:7]}"
    elif ready and merge_state == "DIRTY":
        why = f"conflicts with {base}"
    elif ready and merge_state == "BEHIND":
        why = f"behind {base}"
    else:
        return
    number = wait["condition"]["number"]
    line = f"PR#{number} no longer {'ready' if ready else 'reviewed'}: {why}"
    wait.update(
        state="waiting", fired_text=None, fired_at=None, fired_head=None,
        on_me=False, reason=None, next_check_at=_iso(now),
    )
    _touch(wait)
    _log(record, now, wait, line)
    # The chain's next step waits for this one again, its times counted anew.
    following = _wait_in(record, wait["next"]) if wait["next"] else None
    if (
        following is not None and following["state"] == "waiting"
        and not following["on_me"] and not following["overdue"]
    ):
        _disarm(record, following)
    _toast(state, record, wait, now, "info", line)


def _merge_ends(wait: dict) -> bool:
    return wait["condition"]["until"] in ("review_on_head", "ready")


def _pr_merged(state: dict, pr: dict, now: datetime) -> None:
    """A merge settles every wait on the PR, in any session: nothing is left to
    review or get ready, and the `merged` steps have happened."""
    key, number = _pr_key(pr), pr["number"]
    text = f"PR#{number} merged"
    waits = [
        (record, wait) for record in _records(state) for wait in _open_waits(record)
        if wait["kind"] == "pr" and _pr_key(wait["condition"]) == key
    ]
    for record, wait in waits:
        if _merge_fires(wait):
            _fire(state, record, wait, now, text)
    needless = [item for item in waits if _merge_ends(item[1])]
    # A pending step goes first: closing the step before it would arm it.
    needless.sort(key=lambda item: item[1]["state"] != "pending")
    for record, wait in needless:
        line = f"{text}: {wait['label']} no longer needed"
        _log(record, now, wait, line)
        if not wait["next"]:  # In a chain, a later step has the word.
            _toast(state, record, wait, now, "info", line)
        _complete(state, record, wait, now)


def _merged_prs(cache: PrCache, state: dict) -> list[dict]:
    """The open PR waits a merge has yet to settle, by GitHub's last word. Nothing
    polls a PR while its chain's step runs a prompt or waits for me. `state` may
    be read without the lock: `_pr_merged` decides again under it."""
    return [
        wait["condition"]
        for record in _records(state) for wait in _open_waits(record)
        if wait["kind"] == "pr" and (_merge_fires(wait) or _merge_ends(wait))
        and cache.statuses.get(_pr_key(wait["condition"]), {}).get("state") == "merged"
    ]


def _apply_poll(state: dict, record: dict, wait: dict, result: dict, now: datetime) -> None:
    wait["memo"] = result.get("memo", wait["memo"])
    poll_s = result.get("next_poll_s") or PR_POLL_S
    wait["next_check_at"] = _iso(now + timedelta(seconds=poll_s))
    if result.get("merged"):
        _pr_merged(state, wait["condition"], now)
        if wait["state"] != "waiting":
            return
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
        if result.get("transient"):
            _note_transient(record, wait, now)
            return
        wait["errors"] += 1
        if wait["errors"] >= MAX_ERRORS:
            _attention(
                state, record, wait, now, f"GitHub failed {MAX_ERRORS} times: {error}",
                retry_as="check",
            )
        return
    _checks_work(wait)
    wait["errors"] = 0
    # `confirm` needs nothing here: its on_me event told me, and the wait stays
    # watched, so a compare that later proves the release fires it.
    if result.get("fired"):
        wait["snapshot"] = result.get("snapshot", wait["snapshot"])
        wait["fired_head"] = result.get("head")
        text = result.get("fire_text") or wait["label"]
        name = f"PR#{wait['condition']['number']} "
        _fire(state, record, wait, now, text, fired=text if text.startswith(name) else name + text)


def _apply_verify(state: dict, record: dict, wait: dict, result: dict, now: datetime) -> None:
    if result["transient"]:
        # Not counted, like a transient poll error; the next try backs off.
        wait["verify_memo"] = result.get("memo", wait["verify_memo"])
        wait["verify_claim_until"] = _iso(now + timedelta(seconds=PR_POLL_S))
        _note_transient(record, wait, now)
        return
    _checks_work(wait)
    if result["ok"]:
        _log(record, now, wait, f"Verified: {result['reason']}", "fired")
        record["unseen"] = True
        _toast(state, record, wait, now, "success", wait["label"])
        _complete(state, record, wait, now)
    else:
        # A check that couldn't run can run again; a fix that didn't hold can't.
        retry_as = "verify" if result.get("error") else None
        _attention(state, record, wait, now, result["reason"], retry_as=retry_as)


def _apply_retry(state: dict, record: dict, wait: dict, job: dict, now: datetime) -> None:
    """The hourly try of a wait check errors put on me: a failure changes nothing
    (its next try is claimed already); a success takes it back from me and
    counts like any other result."""
    result = job["result"]
    if job["type"] == "verify":
        if result["transient"] or result.get("error"):
            wait["verify_memo"] = result.get("memo", wait["verify_memo"])
            return
    elif result.get("error"):
        if job["type"] == "pr":
            # The memo now counts these events as seen: say them, or they're lost.
            wait["memo"] = result.get("memo", wait["memo"])
            for event in result.get("events") or []:
                if event.get("on_me"):
                    _log(record, now, wait, event["text"], "attention")
                    record["unseen"] = True
                    _toast(state, record, wait, now, "error", f"{wait['label']}: {event['text']}")
                else:
                    _log(record, now, wait, event["text"])
        return
    _rearm(wait, now)
    _log(record, now, wait, f"Checks work again: {wait['label']}")
    _apply_result(state, record, wait, job, now)


def _apply_result(state: dict, record: dict, wait: dict, job: dict, now: datetime) -> None:
    apply = {
        "verify": _apply_verify, "recheck": _apply_recheck, "command": _apply_command,
    }.get(job["type"], _apply_poll)
    apply(state, record, wait, job["result"], now)


def _awaits(wait: dict, job: dict) -> bool:
    """Whether the wait is still where this kind of result applies."""
    if job["type"] == "verify":
        return wait["state"] == "acting" and wait["verify_pending"]
    if job["type"] == "recheck":
        return _revocable(wait)
    return wait["state"] == "waiting"


def _merge(state: dict, finished: list[dict], now: datetime) -> None:
    """Apply slow results, dropping each one whose wait moved on meanwhile."""
    for job in finished:
        found = _find_wait(state, job["wait"])
        if found is None:
            continue
        record, wait = found
        with _isolated(state, record, now):
            if wait["rev"] != job["rev"]:
                continue
            if job.get("retry"):
                if wait["state"] == "needs_attention" and wait.get("retry_as") in CHECK_RETRIES:
                    _apply_retry(state, record, wait, job, now)
            elif _awaits(wait, job):
                _apply_result(state, record, wait, job, now)


def _pane_closed(state: dict, pane: int, now: datetime, keep=None) -> None:
    """`keep(item)` spares a pane record or job wait of another pane of that id."""
    record = state["panes"].get(str(pane))
    if record is not None and not (keep and keep(record)):
        del state["panes"][str(pane)]
    for record in list(state["sessions"].values()):
        for wait in list(record["waits"]):
            if _watches_job(wait, pane) and not (keep and keep(wait)):
                _attention(state, record, wait, now, "Pane closed before the job finished")


def _reached_by(state: dict, daemon: str | None):
    """Which pane-scoped items a pane event of `daemon` is about: those it
    stamped, and unstamped ones only when it ticks the state. Any other names
    another pane of that number. Without a daemon, every item."""
    def reached(item: dict) -> bool:
        stamp = item.get("daemon")
        return daemon is None or stamp == daemon or (stamp is None and daemon == _ticking_daemon(state))

    return reached


def _panes_gone(state: dict, now: datetime, daemon: str | None = None) -> None:
    """A pane id names a pane of the Tern daemon that ran then: after a reboot,
    or under another daemon, it may name another pane. What watched one ends as
    pane gone; what `daemon` made stays."""
    def mine(item: dict) -> bool:
        return daemon is not None and item.get("daemon") == daemon

    panes = {int(pane) for pane, record in state["panes"].items() if not mine(record)} | {
        wait["condition"]["pane"]
        for record in state["sessions"].values() for wait in record["waits"]
        if wait["kind"] == "job" and wait["state"] == "waiting" and not mine(wait)
    }
    for pane in panes:
        _pane_closed(state, pane, now, keep=mine)
    state["actions"] = [
        action for action in state["actions"]
        if action["type"] not in PANE_ACTIONS or mine(action)
    ]


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
            _log(record, now, wait, f"{wait['label']}: finished", "fired")
            record["unseen"] = True
            _toast(state, record, wait, now, "success", f"{wait['label']}: finished")
            _complete(state, record, wait, now)
            return
    _fire(
        state, record, wait, now, f"{wait['label']}: finished",
        "success" if status == 0 else "info",
    )


def _after_reboot(state: dict, agents: Agents, now: datetime) -> None:
    """Nothing that ran before the reboot runs now. Each closed session gets one
    fresh automatic resume, an extension heard from before the reboot doesn't
    hold it back, and a prompt the reboot cut off is sent again, once, saying
    so; cut off a second time it comes to me."""
    state["extensions"] = {
        session: seen for session, seen in state["extensions"].items()
        if _running(state, agents, session)
    }
    for record in state["sessions"].values():
        record.update(resume_prompts=[], resume_opened_at=None)
        for entry in record["prompt_queue"]:
            if entry["state"] == "queued" or entry.get("rebooted"):
                continue
            entry.update(
                state="queued", claimed_at=None, rebooted=True, queued_at=_iso(now),
                text=f"{REBOOT_NOTE}\n\n{entry['text']}",
            )
            wait = _wait_in(record, entry["wait"])
            if wait is not None:
                _log(record, now, wait, f"Sending again after a reboot: {wait['label']}")


def _fold_toasts(state: dict, now: datetime) -> None:
    """Toasts no window has shown for an hour fold into one, so the first window
    doesn't play a burst of them."""
    stamp = now.timestamp()
    old = [
        action for action in state["actions"]
        if action["type"] == "toast" and stamp - _stamp(action["created_at"]) > TOAST_FOLD_S
    ]
    if len(old) <= TOAST_FOLD_COUNT:
        return
    gone = {action["id"] for action in old}
    state["actions"] = [action for action in state["actions"] if action["id"] not in gone]
    count = sum(action.get("folded", 1) for action in old)
    _enqueue(state, now, {
        "type": "toast", "level": "error" if any(action["level"] == "error" for action in old) else "info",
        "text": f"{count} waits changed while no Tern window was open", "sub": None,
        "session": None, "pane": None, "folded": count,
    })


def _prune_done_waits(state: dict, now: datetime) -> None:
    """A record kept open by a `later` or a repeat drops the waits that ended a
    week ago; its history keeps what they did."""
    for record in _records(state):
        if _open_waits(record):
            with _isolated(state, record, now):
                _prune_record(record, now)


def _prune_record(record: dict, now: datetime) -> None:
    stamp = now.timestamp()
    for wait in record["waits"]:
        if wait["state"] == "done" and not wait.get("done_at"):
            wait["done_at"] = _iso(now)  # ended before `done_at` was kept
    by_id = {wait["id"]: wait for wait in record["waits"]}
    gone = {
        wait["id"] for wait in record["waits"]
        if wait["state"] == "done" and stamp - _stamp(wait["done_at"]) > DONE_WAIT_KEPT_S
    }
    if not gone:
        return
    for wait in record["waits"]:
        # A chain runs on through its ended steps (`_activate_next`): link past them.
        following = wait["next"]
        while following in gone:
            following = by_id[following]["next"]
        wait["next"] = following
    record["waits"] = [wait for wait in record["waits"] if wait["id"] not in gone]


def _housekeeping(
    state: dict, agents: Agents, omp: Omp, now: datetime, daemon: str | None = None,
) -> None:
    """`daemon` is the ticking Tern daemon's id: under a new one, pane ids name
    other panes."""
    stamp = now.timestamp()
    boot = _boot_id(agents)
    rebooted = bool(boot and state["boot_id"] and state["boot_id"] != boot)
    if boot:
        if rebooted:
            _after_reboot(state, agents, now)
            # Pane ids don't survive a reboot, and may belong to another pane now.
            _panes_gone(state, now)
            state["actions"] = [
                action for action in state["actions"] if action["type"] == "toast"
            ]
            # A session absent from agents.json can't be told dead from new by
            # its id: only a report since the sweep, or a live row, is this boot's.
            state["extension_panes"] = {
                session: pane for session, pane in state["extension_panes"].items()
                if _running(state, agents, session)
            }
        state["boot_id"] = boot
    if daemon:
        last = _ticking_daemon(state)
        if last and last != daemon and not rebooted:
            # A new daemon: its panes are new too, and so may be the ones extensions report.
            _panes_gone(state, now, daemon)
            state["extension_panes"] = {}
        _hold_ticker(state, daemon, now)
    state["extensions"] = {
        session: seen for session, seen in state["extensions"].items()
        if stamp - _parse_time(seen).timestamp() <= EXTENSION_KEPT_S
    }
    # A session's features and pane end with its extension's record.
    for known in (state["extension_features"], state["extension_panes"]):
        for session in [session for session in known if session not in state["extensions"]]:
            del known[session]
    state["menu_requests"] = _fresh_menu_requests(state, now)
    for record in state["sessions"].values():
        _remember_tern_session(record, agents)
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
    _fold_toasts(state, now)
    _prune_done_waits(state, now)


def _timer_text(wait: dict) -> str:
    """How long a time wait ran, as asked for: a late tick doesn't change it, and
    only a clock time that fell mid-minute gets rounded."""
    armed = _stamp(wait.get("armed_at") or wait["created_at"])
    seconds = max(int(_stamp(wait["condition"]["at"]) - armed), 0)
    return f"{_compact(seconds) if seconds % 60 else _interval_text(seconds)} passed"


def _unsnooze(state: dict, record: dict, wait: dict, now: datetime) -> None:
    """A snoozed wait whose event had happened comes back as it was. One whose
    prompt the snooze cancelled before the agent got it fires again."""
    back = wait["snoozed_from"]
    wait.update(state=back, snoozed_from=None, refire_at=None)
    if back == "needs_attention":
        _attention(state, record, wait, now, wait["reason"], retry_as=wait.get("retry_as"))
    elif back == "review":
        wait["reason"] = REASON_REVIEW
        _touch(wait)
        _log(record, now, wait, f"{wait['label']}: {REASON_REVIEW}", "fired")
        record["unseen"] = True
        _toast(state, record, wait, now, "info", f"{wait['label']}: {REASON_REVIEW}")
    else:
        # A repeat ended when it fired; it isn't ending again.
        _fire(state, record, wait, now, fired=wait.get("fired_text"), ending=True)


def _run_text(wait: dict) -> str:
    repeat = wait["repeat"]
    text = repeat["prompt"]
    if repeat["until"]:
        text += (
            f"\n\n(This prompt repeats every {_interval_text(repeat['every_s'])} until:"
            f" {repeat['until']}. When that holds, end it with the waitingfor tool:"
            f' action "end_repeat", wait "{wait["id"]}".)'
        )
    return text


def _repeat_step(state: dict, record: dict, wait: dict, now: datetime) -> None:
    """A waiting repeat: end it, skip a run while the last one's turn goes on,
    or queue the run that is due. Runs neither toast nor mark the target unseen."""
    repeat = wait["repeat"]
    stamp = now.timestamp()
    if not _queued(record, repeat["running"]):
        repeat["running"] = None
    ends = _stamp(repeat["ends_at"])
    if (ends is not None and ends <= stamp) or (_repeat_spent(repeat) and repeat["running"] is None):
        _end_repeat(state, record, wait, now)
        return
    if _repeat_spent(repeat):
        return  # Its last run is still going.
    due = _stamp(repeat["next_at"])
    if due is None:
        # A skipped run counts from the end of the one going on (`_run_over`);
        # this catches a run that left the queue some other way.
        if repeat["running"] is None:
            repeat["next_at"] = _iso(now + timedelta(seconds=repeat["every_s"]))
        return
    if due > stamp:
        return
    if repeat["running"] is not None:
        repeat["next_at"] = None
        _log(record, now, wait, "Skipped a run: the last one is still going")
        return
    action = _queue_prompt(state, record, wait, _run_text(wait), now, repeat=True)
    repeat.update(runs=repeat["runs"] + 1, running=action, last_run_at=_iso(now))
    wait["snooze_s"] = None
    # The last run has no next one.
    spent = _repeat_spent(repeat)
    repeat["next_at"] = None if spent else _iso(now + timedelta(seconds=repeat["every_s"]))
    _log(record, now, wait, f"Run {repeat['runs']}: {wait['label']}")


def _evaluate(state: dict, now: datetime) -> None:
    """Everything that needs only the clock."""
    for record in _records(state):
        with _isolated(state, record, now):
            _evaluate_record(state, record, now)


def _evaluate_record(state: dict, record: dict, now: datetime) -> None:
    stamp = now.timestamp()
    for wait in list(record["waits"]):
        if wait["state"] == "snoozed" and _stamp(wait["refire_at"]) <= stamp:
            _unsnooze(state, record, wait, now)
        if wait["state"] == "waiting" and wait.get("repeat"):
            _repeat_step(state, record, wait, now)
        if wait["state"] != "waiting":
            continue
        condition = wait["condition"]
        if wait["kind"] == "time" and _stamp(condition["at"]) <= stamp:
            _fire(state, record, wait, now, fired=_timer_text(wait))
        else:
            deadline = _stamp(wait["deadline"])
            if deadline is not None and deadline <= stamp and not wait["overdue"]:
                wait["overdue"] = True
                wait["reason"] = wait["reason"] or REASON_OVERDUE
                _log(record, now, wait, f"{wait['label']}: {REASON_OVERDUE}", "attention")
                record["unseen"] = True
                _toast(state, record, wait, now, "error", f"{wait['label']}: {REASON_OVERDUE}")


def _check_bound_s(wait: dict, pr_bound: int) -> int:
    """How far ahead a wait's next check can lie: its interval or claim, an
    hour for a retry, or what its snooze asked for."""
    if wait["state"] == "needs_attention":
        bound = RETRY_DEAD_S
    elif wait["kind"] == "command":
        bound = max(wait["condition"]["interval_s"], CHECK_CLAIM_S)
    else:
        bound = pr_bound
    return max(bound, wait.get("snooze_s") or 0)


def _backfill_snooze(record: dict, wait: dict, stamp: float) -> None:
    """A wait from before `snooze_s` whose last history line is its snooze may
    have been snoozed for days: what lies ahead of it counts as asked for, once.
    Any other gets its own interval's bound (a hidden snooze: 24 h)."""
    last = next((line for line in reversed(record["history"]) if line.get("wait") == wait["id"]), None)
    if last is None or not str(last.get("text")).startswith("Snoozed until "):
        wait["snooze_s"] = None
        return
    times = [wait.get("next_check_at"), wait.get("refire_at"), (wait.get("repeat") or {}).get("next_at")]
    ahead = max((_stamp(text) - stamp for text in times if text), default=0)
    wait["snooze_s"] = math.ceil(ahead) if ahead > 0 else None


def _clamp(state: dict, now: datetime, problems: list[str]) -> None:
    """A clock that jumped forward and back leaves times far ahead: none lies
    further out than its own interval. Deadlines and time waits are mine.
    What no one record causes goes to `problems`, the tick's error."""
    stamp = now.timestamp()
    pr_bound = None
    if any(
        isinstance(wait, dict) and wait.get("kind") == "pr"
        for record in _records(state) for wait in record.get("waits") or []
    ):
        try:
            # waiting_github's longest `next_poll_s`.
            pr_bound = max(_github().ENDED_POLL_S, CHECK_CLAIM_S)
        except Exception as error:  # the PR checks fail the same way; their times stay
            problems.append(f"PR checks: {_error_line(error)}")

    def bound(wait: dict, holder: dict, key: str, seconds: int) -> None:
        if (_stamp(holder.get(key)) or 0) > stamp + seconds:
            holder[key] = _iso(now + timedelta(seconds=seconds))
            _touch(wait)

    for record in _records(state):
        with _isolated(state, record, now):
            for wait in _open_waits(record):
                if "snooze_s" not in wait:
                    _backfill_snooze(record, wait, stamp)
                if wait["kind"] == "command" or wait["kind"] == "pr" and pr_bound is not None:
                    bound(wait, wait, "next_check_at", _check_bound_s(wait, pr_bound))
                bound(wait, wait, "verify_claim_until", RETRY_DEAD_S)
                repeat = wait.get("repeat")
                if repeat:
                    bound(wait, repeat, "next_at", max(repeat["every_s"], wait.get("snooze_s") or 0))
                if wait["state"] == "snoozed":
                    bound(wait, wait, "refire_at", wait.get("snooze_s") or SNOOZE_BOUND_S)


def _next_tick_s(state: dict, now: datetime) -> int:
    stamp = now.timestamp()
    times: list[float | None] = []
    for record in _records(state):
        with _isolated(state, record, now):
            times += _record_times(state, record, now)
    delay = min((moment - stamp for moment in times if moment is not None), default=TICK_MAX_S)
    delay = min(max(math.ceil(delay), TICK_MIN_S), TICK_MAX_S)
    if any(
        stamp - _parse_time(action["created_at"]).timestamp() <= OUTBOX_RECENT_S
        for action in state["actions"]
    ):
        return min(delay, TICK_OUTBOX_S)
    return delay


def _record_times(state: dict, record: dict, now: datetime) -> list[float | None]:
    """When the record next needs a tick."""
    stamp = now.timestamp()
    times: list[float | None] = []
    if _resume_opened_lately(record, now):
        times.append(_stamp(record["resume_opened_at"]) + RESUME_GRACE_S)
    pending = _pending_resume(state, record["session"]) if record["session"] else None
    if pending is not None:
        # When the resume expires, or comes to me for want of a window (`_resume`).
        for since in (_stamp(pending.get("window_since")), _stamp(pending.get("windowless_since"))):
            if since is not None and since + RESUME_GRACE_S > stamp:
                times.append(since + RESUME_GRACE_S)
    if record["prompt_queue"] and _heard_lately(state, record["session"] or "", now):
        # When a closed session's extension has been quiet long enough to resume it.
        times.append(_stamp(state["extensions"][record["session"]]) + RESUME_QUIET_S)
    for wait in record["waits"]:
        if wait["state"] == "acting" and wait["verify_pending"]:
            times.append(_stamp(wait["verify_claim_until"]) or stamp)
        if wait["state"] == "snoozed":
            times.append(_stamp(wait["refire_at"]))
        if _revocable(wait):
            times.append(_stamp(wait["next_check_at"]))
        if wait["state"] != "waiting":
            continue
        times.append(_stamp(wait["next_check_at"]))
        if wait.get("repeat"):
            times += [_stamp(wait["repeat"]["next_at"]), _stamp(wait["repeat"]["ends_at"])]
        if not wait["overdue"]:
            times.append(_stamp(wait["deadline"]))
        if wait["kind"] == "time":
            times.append(_stamp(wait["condition"]["at"]))
    return times


def _public_action(action: dict) -> dict:
    return {
        key: value for key, value in action.items()
        if key not in ("wait", "created_at", "window_since", "windowless_since", "daemon", "folded")
    }


# View ------------------------------------------------------------------------


def _wait_due(wait: dict) -> float | None:
    if wait["state"] == "pending":
        return None
    times = [_stamp(wait["deadline"]), _stamp(wait["refire_at"])]
    if wait["kind"] == "time":
        times.append(_stamp(wait["condition"]["at"]))
    repeat = wait.get("repeat")
    if repeat and wait["state"] == "waiting":
        times += [_stamp(repeat["next_at"]), _stamp(repeat["ends_at"])]
    return min((moment for moment in times if moment is not None), default=None)


def _wait_ball(wait: dict) -> str:
    if wait["state"] in ("fired", "review", "needs_attention") or wait["overdue"] or wait["on_me"]:
        return "on_me"
    if wait["state"] == "acting":
        return "on_machine"
    # Nothing watches a snoozed wait: only time brings it back.
    if wait["state"] == "snoozed":
        return "on_timer"
    return {
        "something": "on_someone", "later": "on_my_time", "time": "on_timer", "repeat": "on_timer",
    }.get(wait["kind"], "on_machine")


def _outcome(wait: dict) -> str | None:
    """How the wait ended up, if it did. Every on_me wait has one, and it decides
    the wait's prefix on every surface. A prompt the agent still runs (`acting`)
    is work in progress, not an outcome."""
    if wait["state"] == "needs_attention" or wait["on_me"]:
        return "attention"
    if wait["overdue"]:
        return "overdue"
    return "fulfilled" if wait["state"] in ("fired", "review") else None


def _repeat_view(repeat: dict | None) -> dict | None:
    if not repeat:
        return None
    return {
        "every": _interval_text(repeat["every_s"]), "next_at": repeat["next_at"],
        "runs": repeat["runs"], "times": repeat["times"], "ends_at": repeat["ends_at"],
        "until": repeat["until"],
    }


def _default_repeat_label(repeat: dict) -> str:
    """`Every 2h`, or `Every 2h until <until>`, from a wait view's `repeat`."""
    label = f"Every {repeat['every']}"
    return f"{label} until {repeat['until']}" if repeat["until"] else label


def _repeat_text(repeat: dict, state: str, now: datetime) -> str:
    """`every 2h · next 16:00 · 3/5 · until 18:00`, from a wait view's `repeat`."""
    parts = [f"every {repeat['every']}"]
    spent = _repeat_spent(repeat)
    if state == "waiting":
        if repeat["next_at"]:
            parts.append(f"next {_clock(_parse_time(repeat['next_at']), now)}")
        else:
            parts.append("last run" if spent else "next after this run")
    if repeat["times"] is not None:
        parts.append(f"{repeat['runs']}/{repeat['times']}")
    if repeat["ends_at"]:
        parts.append(f"until {_clock(_parse_time(repeat['ends_at']), now)}")
    return " · ".join(parts)


def _summary(wait: dict, repeat: dict | None, now: datetime) -> str:
    """`repeat` is the wait view's."""
    if repeat is None:
        return _condition_summary(wait, now)
    text = REPEAT_MARK + _repeat_text(repeat, wait["state"], now)
    return text if wait["kind"] == "repeat" else f"{_condition_summary(wait, now)} · {text}"


def _condition_summary(wait: dict, now: datetime) -> str:
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
        return _command_line(condition)
    if kind == "job":
        return f"job in pane {condition['pane']}"
    if kind == "something":
        return condition["what"]
    return "when I have time"


def _progress(wait: dict, outcome: str | None, now: datetime) -> str | None:
    """What the wait does right now, beyond waiting: Tern resumes the session for
    it, its checks have failed a while for a reason that passes, or the agent
    runs its prompt."""
    if wait["reason"] == REASON_RESUMING:
        return REASON_RESUMING
    checked = wait["state"] == "waiting" or wait["state"] == "acting" and wait["verify_pending"]
    if checked and _transient_reported(wait, now):
        return _transient_text(wait, now)
    return WORKING_TEXT if wait["state"] == "acting" and outcome is None else None


def _wait_view(wait: dict, now: datetime) -> dict:
    due = _wait_due(wait)
    outcome = _outcome(wait)
    fulfilled = outcome == "fulfilled"
    repeat = _repeat_view(wait.get("repeat"))
    summary = _summary(wait, repeat, now)
    view = {
        "id": wait["id"], "kind": wait["kind"], "label": wait["label"],
        "summary": summary, "state": wait["state"],
        "ball": _wait_ball(wait), "overdue": wait["overdue"],
        "outcome": outcome,
        # A wait that fired before these were kept has only its label and start;
        # a PR's summary ("PR#20 merged") says more than a chain step's label ("Merged").
        "fired_text": (wait.get("fired_text") or (summary if wait["kind"] == "pr" else wait["label"]))
        if fulfilled else None,
        "fired_at": (wait.get("fired_at") or wait["created_at"]) if fulfilled else None,
        "progress": _progress(wait, outcome, now),
        # Check errors put it on me, and `retry` can put it back.
        "retryable": wait["state"] == "needs_attention" and bool(wait.get("retry_as")),
        "reason": wait["reason"], "created_at": wait["created_at"],
        "due_at": None if due is None else _iso(datetime.fromtimestamp(due).astimezone()),
        "deadline": wait["deadline"], "condition": wait["condition"],
        "has_prompt": bool(wait["prompt"] or wait.get("repeat")), "next": wait["next"], "by": wait["by"],
        "repeat": repeat,
        # A job wait's Tern daemon, whose pane it watches (`event --daemon`).
        "daemon": wait.get("daemon"),
    }
    view["mark"] = _wait_mark(view)
    return view


def _active(views: list[dict]) -> list[dict]:
    """Pending chain steps say nothing about who holds the ball now."""
    return [view for view in views if view["state"] != "pending"] or views


def _primary(views: list[dict]) -> dict:
    """The wait a session is shown by. On_me waits all have an outcome and no other
    ball has a worse one, so this is also the wait with the worst outcome."""
    return min(_active(views), key=lambda view: (
        BALL_RANK[view["ball"]], OUTCOME_RANK.get(view["outcome"], len(OUTCOMES)),
        _sort_time(view["due_at"]), view["created_at"],
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


def _outcome_headline(views: list[dict]) -> tuple[str | None, str | None]:
    """(outcome, headline) of the target: its worst open wait's."""
    if not views:
        return None, None
    primary = _primary(views)
    outcome = primary["outcome"]
    if outcome == "fulfilled":
        return outcome, f"{OUTCOME_MARKS[outcome]}{primary['fired_text']}"
    if outcome == "attention":
        return outcome, f"{OUTCOME_MARKS[outcome]}{primary['reason'] or primary['label']}"
    if outcome == "overdue":
        return outcome, f"{OUTCOME_MARKS[outcome]}{primary['label']} overdue"
    return None, None


def _wait_mark(view: dict) -> str:
    if view["outcome"] is None and view["state"] == "acting":
        return WORKING_MARK
    if view["outcome"] is None and view["kind"] == "repeat" and view["state"] != "snoozed":
        return REPEAT_MARK
    waits_for_time = view["kind"] == "time" or view["state"] == "snoozed"
    return OUTCOME_MARKS.get(view["outcome"]) or ("⏲ " if waits_for_time else "⏳ ")


def _tracks(views: list[dict]) -> list[list[dict]]:
    """The target's waits by thread: a chain (steps linked by `next`) is one, any
    other wait its own. In the waits' order, so a chain's steps stay together."""
    by_id = {view["id"]: view for view in views}
    following = {view["next"] for view in views if view["next"] in by_id}
    tracks = []
    for view in views:
        if view["id"] in following:
            continue
        track, step = [], view
        while step is not None and step not in track:
            track.append(step)
            step = by_id.get(step["next"]) if step["next"] else None
        tracks.append(track)
    return tracks


def _track_text(view: dict) -> str:
    """One thread in a few words, by its current step."""
    if view["outcome"] == "fulfilled":
        return view["fired_text"]
    if view["outcome"] == "attention" and view["reason"]:
        name = f"PR#{view['condition']['number']}" if view["kind"] == "pr" else view["label"]
        return f"{name}: {view['reason']}"
    if _repeats_alone(view):
        # Its mark is the summary's `↻`.
        text = view["summary"].removeprefix(REPEAT_MARK)
        own = _own_repeat_label(view)
        text = f"{own} · {text}" if own else text
    else:
        text = view["summary"] if view["kind"] in ("pr", "something") else view["label"]
    return f"{text}, {view['progress']}" if view["progress"] else text


def _repeats_alone(view: dict) -> bool:
    """A `repeat` wait that its timing tells all about: a snoozed one is hidden
    until a time, like any other."""
    return view["kind"] == "repeat" and view["state"] != "snoozed"


def _own_repeat_label(view: dict) -> str | None:
    """A label given to a `repeat` wait; the default one only repeats its timing."""
    return None if view["label"] == _default_repeat_label(view["repeat"]) else view["label"]



def _track_texts(views: list[dict]) -> list[str]:
    """Every thread's current step, the target's primary one first, each with its mark."""
    if not views:
        return []
    primary = _primary(views)
    currents = [_primary(track) for track in _tracks(views)]
    currents.sort(key=lambda current: current is not primary)
    return [_wait_mark(current) + _track_text(current) for current in currents]


def _still_waiting(views: list[dict]) -> list[str]:
    """The threads nothing has come of yet, by their current step with its mark:
    a fulfilled session that still waits on one isn't done."""
    return [
        _wait_mark(current) + _track_text(current)
        for current in (_primary(track) for track in _tracks(views))
        if current["outcome"] is None
    ]


def _status_lines(
    views: list[dict], note: str | None, pinned: bool, now: datetime,
) -> tuple[str, str]:
    """(status segment, line). A repeat says when it runs next instead of how
    long it has been; a `repeat` wait is only that, so it says it once."""
    status = line = ""
    if views:
        primary = _primary(views)
        mark = _wait_mark(primary)
        if primary["outcome"] == "fulfilled":
            short = _clip(primary["fired_text"], SHORT_LABEL_CHARS)
            age = _compact(now.timestamp() - _stamp(primary["fired_at"]))
            ran = [f"prompt {REASON_REVIEW}"] if primary["state"] == "review" else []
            parts = [primary["fired_text"], *ran, age]
            glance = f"{short} · {age}"
        elif _repeats_alone(primary):
            timing = _repeat_text(primary["repeat"], primary["state"], now)
            own = _own_repeat_label(primary)
            until = primary["repeat"]["until"]
            detail = primary["reason"] if primary["outcome"] else None
            parts = [text for text in (
                own, timing, until and f"until {until}", detail, primary["progress"],
            ) if text]
            glance = f"{_clip(own, SHORT_LABEL_CHARS)} · {timing}" if own else timing
        else:
            if primary["kind"] == "pr":
                short = f"PR#{primary['condition']['number']}"
            else:
                short = _clip(primary["label"], SHORT_LABEL_CHARS)
            detail = (primary["reason"] if primary["outcome"] else None) or primary["label"]
            if primary["progress"]:
                detail = f"{detail}, {primary['progress']}"
            if primary["repeat"] and primary["state"] == "waiting":
                age = REPEAT_MARK + _repeat_text(primary["repeat"], primary["state"], now)
            else:
                age = _when(primary, now)
            parts = [short] + ([detail] if detail != short else []) + [age]
            glance = f"{short} · {age}"
        # The session's other threads, each by its current step: one line tells them all.
        others = _track_texts(views)[1:]
        parts += [f"+ {text}" for text in others[:OTHER_TRACKS_SHOWN]]
        if len(others) > OTHER_TRACKS_SHOWN:
            parts.append(f"+{len(others) - OTHER_TRACKS_SHOWN} more")
        if note:
            parts.append(f'"{_clip(note, NOTE_IN_LINE_CHARS)}"')
        status = mark + glance + (f" +{len(others)}" if others else "")
        line = mark + " · ".join(parts)
    elif pinned:
        status = "📌 pinned"
        line = status + (f' · "{_clip(note, NOTE_IN_LINE_CHARS)}"' if note else "")
    return status, line


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
    """What needs me, else each thread's next step, named by its PR when there are several."""
    for view in views:
        if view["state"] == "needs_attention":
            return view["reason"]
    tracks = _tracks(views)
    steps = []
    for track in tracks:
        step = next((view for view in track if view["state"] == "pending"), None)
        if step is not None:
            steps.append(step["summary"] if len(tracks) > 1 and step["kind"] == "pr" else step["label"])
    return " · ".join(steps) or None


def _waiting_for(views: list[dict]) -> str | None:
    """The target's free-text status (see `_free_text_waits`)."""
    return next((
        view["condition"]["what"] for view in views
        if view["kind"] == "something" and view["state"] != "pending"
    ), None)


def _history_view(record: dict | None) -> list[dict]:
    # Lines from before `kind` have none.
    return [{"kind": None, **entry} for entry in record["history"][-HISTORY_SHOWN:]] if record else []


def _reported_pane(state: dict, agents: Agents, session: str, now: datetime) -> int | None:
    """The pane the session's extension last named, while it may still run there.
    Only a session agents.json has seen end counts as gone: a new one isn't in
    agents.json by its id until it has a session file."""
    if not _extension_fresh(state, session, now):
        return None
    if session in agents.closed and not _running(state, agents, session):
        return None
    return state["extension_panes"].get(session)


def _extension_panes(state: dict, agents: Agents, now: datetime) -> dict:
    """By pane: which session's extension runs there, and whether it has the menu."""
    panes = {}
    for session in state["extension_panes"]:
        pane = _reported_pane(state, agents, session, now)
        if pane is not None:
            features = state["extension_features"].get(session, ())
            panes[str(pane)] = {"session": session, "menu": FEATURE_MENU in features}
    return panes


def _target_view(record: dict | None, now: datetime) -> dict:
    """What a session row and a pane row both say about the target's waits."""
    views = [_wait_view(wait, now) for wait in _open_waits(record)] if record else []
    outcome, headline = _outcome_headline(views)
    dues = [view["due_at"] for view in _active(views) if view["due_at"]]
    return {
        "ball": min((view["ball"] for view in _active(views)), key=BALL_RANK.get) if views else None,
        "outcome": outcome, "headline": headline,
        "threads": _track_texts(views), "still_waiting": _still_waiting(views),
        "waiting_for": _waiting_for(views),
        "due_at": min(dues, key=_stamp) if dues else None,
        "waits": views, "history": _history_view(record),
    }


def _session_view(
    session: str, record: dict | None, facts: Facts, pinned: bool,
    state: dict, now: datetime, prs: PrCache,
) -> dict:
    info = facts.describe(session)
    target = _target_view(record, now)
    views = target["waits"]
    live = facts.agents.live.get(session) or {}
    agent_state = live.get("state")
    note = record["note"] if record else None
    if not views:
        # A card may show any session; a listed one without waits is pinned.
        target["ball"] = "on_my_time" if pinned else "on_machine"
    status, line = _status_lines(views, note, pinned, now)
    pane = info["pane"]
    if pane is None:
        pane = _reported_pane(state, facts.agents, session, now)
    return {
        **target,
        "session_id": session, "title": info["title"], "cwd": info["cwd"],
        "project": info["project"], "live": info["live"], "pane": pane,
        "tern_session": _text(live.get("ternSession")) or None,
        "agent_state": agent_state if agent_state in ("working", "blocked", "idle") else None,
        "extension": _extension_fresh(state, session, now),
        "resume_command": info["resume_command"],
        "note": note,
        "suggested_note": record["suggested_note"] if record else None,
        "size": record["size"] if record else None,
        "set_aside_at": record["set_aside_at"] if record else None,
        "pinned": pinned, "unseen": bool(record and record["unseen"]),
        "status": status, "line": line, "composer_row": line,
        "next_step": _next_step(views),
        "recap": info["recap"], "recap_at": info["recap_at"], "recap_stale": info["recap_stale"],
        "prompts_queued": _prompts_queued(record, now),
        "prs": prs.session_prs(session, facts.omp.transcript(session), record),
    }


def _pane_view(record: dict, now: datetime) -> dict:
    target = _target_view(record, now)
    status, line = _status_lines(target["waits"], record["note"], False, now)
    return {
        **target,
        "pane": record["pane"],
        "label": _primary(target["waits"])["label"],
        "note": record["note"],
        "status": status, "line": line,
        "set_aside_at": record["set_aside_at"],
        # The Tern daemon whose pane this is (`event --daemon`).
        "daemon": record.get("daemon"),
    }


def _sort_key(view: dict) -> tuple:
    return (
        BALL_RANK[view["ball"]], _sort_time(view["due_at"]), _sort_time(view["set_aside_at"]),
    )


def _build_view(
    state: dict, now: datetime, agents: Agents,
    only: str | None = None, omp: Omp | None = None, ticked: bool = False,
    problems: list[str] = (),
) -> dict:
    """`ticked`: a tick's view, which clears the error a failed tick left.
    `problems`: what went wrong in the tick beyond any one record."""
    facts = Facts(agents, omp)
    pins = set(facts.omp.pins())
    chosen = []
    for session in sorted({*state["sessions"], *pins}):
        if not _SESSION_ID.fullmatch(session):
            continue
        record = state["sessions"].get(session)
        if (record and _open_waits(record)) or session in pins:
            chosen.append((session, record))
    facts.prefetch([session for session, _ in chosen])
    prs = PrCache(now=now)
    errors = list(problems)

    def shown(name: str, build) -> list[dict]:
        """`build()`'s rows; one that can't be built is left out, and the view's
        health says so."""
        try:
            return build()
        except Exception as error:
            errors.append(f"{name} can't be shown: {_error_line(error)}")
            return []

    sessions = [
        row for session, record in chosen for row in shown(
            f"session {session[:8]}",
            lambda: [_session_view(session, record, facts, session in pins, state, now, prs)],
        )
    ]
    prs.save()
    sessions.sort(key=_sort_key)
    panes = [
        row for key, record in state["panes"].items() for row in shown(
            f"pane {key}", lambda: [_pane_view(record, now)] if _open_waits(record) else [],
        )
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
        "extension_panes": _extension_panes(state, agents, now),
        "health": _health(state, now, "; ".join(errors) or None, ticked),
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


def _command_line(condition: dict) -> str:
    return _clip(shlex.join(condition["argv"]), COMMAND_LABEL_CHARS)


def _default_label(kind: str, condition: dict) -> str:
    if kind == "command" and condition.get("repo"):
        return f"next release of {condition['repo']}"
    if kind == "command":
        return _command_line(condition)
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
        if args.until is not None and args.until not in PR_UNTIL:
            raise BadInput(f"bad --until for pr: {args.until!r}; one of {', '.join(PR_UNTIL)}")
        condition = {
            "type": "pr", "repo": repo, "number": number, "until": args.until or "ready",
        }
        if args.bots:
            condition["bots"] = _comma_list(args.bots)
        return condition
    return {"type": kind}


def _repeat_options(args: argparse.Namespace) -> dict | None:
    """The `repeat` of a new wait, None without `--every`. Its prompt is the one
    that repeats, so the wait itself fires none."""
    kind = args.kind
    if args.every is None:
        if kind == "repeat":
            raise BadInput("repeat needs --every")
        for value, flag in ((args.for_, "--for"), (args.times, "--times")):
            if value is not None:
                raise BadInput(f"{flag} needs --every")
        return None
    if kind not in REPEAT_KINDS:
        raise BadInput(f"--every does not apply to {kind}")
    every = _parse_duration(args.every)
    if not REPEAT_MIN_S <= every <= REPEAT_MAX_S:
        raise BadInput("--every must be between 1m and 365d")
    for_s = None if args.for_ is None else _parse_duration(args.for_)
    # The first run is due when a window of exactly --every closes, and a closed window ends it first.
    if for_s is not None and for_s <= every:
        raise BadInput("--for must be longer than --every")
    if args.times is not None and args.times < 1:
        raise BadInput("--times must be at least 1")
    prompt = _text(args.prompt)
    if not prompt:
        raise BadInput("--every needs --prompt: the prompt to repeat")
    if args.verify:
        raise BadInput("--verify does not apply to a repeating prompt")
    return {
        "every_s": every, "prompt": prompt, "for_s": for_s, "ends_at": None,
        "times": args.times, "runs": 0, "next_at": None, "running": None,
        "last_run_at": None, "until": (_text(args.until) or None) if kind == "repeat" else None,
    }


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
        _arm_step(record, wait, now)
        return
    tail = _wait_in(record, after)
    if tail is None:
        raise BadInput(f"unknown wait: {after}")
    while tail["next"] and _wait_in(record, tail["next"]) is not None:
        tail = _wait_in(record, tail["next"])
    tail["next"] = wait["id"]
    if tail["state"] in ("fired", "done"):
        _arm_step(record, wait, now)


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
    if args.cwd is not None and (not os.path.isabs(args.cwd) or not os.path.isdir(args.cwd)):
        raise BadInput("--cwd must be an absolute existing directory")
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
    repeat = _repeat_options(args)
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
    cwd = args.cwd
    if cwd is None and session:
        cwd = facts.describe(session)["cwd"] or None
    command_checks = [check for check in (condition, verify) if check and check["type"] == "command"]
    if command_checks:
        cwd = cwd if cwd is not None else os.getcwd()
        for check in command_checks:
            check["cwd"] = cwd

    def apply(state: dict, now: datetime) -> dict:
        record = _ensure(state, session, pane)
        _remember_tern_session(record, agents)
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
                label=_text(args.label) or (
                    _default_repeat_label(_repeat_view(repeat)) if kind == "repeat"
                    else _default_label(kind, condition)
                ),
                deadline=deadline, deadline_in_s=deadline_in_s,
                prompt=None if repeat else args.prompt or None, verify=verify,
                toast=not args.no_toast, repeat=repeat,
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


def _drop_old_chain(record: dict, repo: str, number: int, now: datetime) -> list[dict]:
    """A new babysit of a PR replaces the session's earlier one: its open steps
    close at once, none of them arming the next. Only chain steps (linked by
    `next`) go; a wait of its own on that PR, such as one for its release with a
    prompt to send then, stays. Returns the steps chained after the old chain
    that it never reached (`--after`), for the new chain to carry on to."""
    key = _pr_key({"repo": repo, "number": number})
    linked = {wait["next"] for wait in record["waits"] if wait["next"]}
    dropped = [
        wait for wait in _open_waits(record)
        if wait["kind"] == "pr" and _pr_key(wait["condition"]) == key
        and (wait["next"] or wait["id"] in linked)
    ]
    for wait in dropped:
        _log(record, now, wait, f"Replaced by a new babysit: {wait['label']}")
        _finish(record, wait, now, activate=False)
    following = (_wait_in(record, wait["next"]) for wait in dropped if wait["next"])
    return [wait for wait in following if wait is not None and wait["state"] == "pending"]


def cmd_babysit_pr(args: argparse.Namespace) -> dict:
    repo, number = _pr_reference(args)
    prompt = None if args.no_fix else _github().FIX_PROMPT
    agents = _read_agents()
    omp = Omp()

    def apply(state: dict, now: datetime) -> dict:
        record = _ensure(state, args.session, None)
        _remember_tern_session(record, agents)
        _begin(record, omp, now)
        orphans = _drop_old_chain(record, repo, number, now)
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
        tail = waits[-1]
        for orphan in orphans:
            tail["next"] = orphan["id"]
            tail = orphan
            while tail["next"] and _wait_in(record, tail["next"]) is not None:
                tail = _wait_in(record, tail["next"])
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
        # Only a set-aside target, or a pinned session in the inventory, shows
        # its sentence; one anywhere else would sit unseen.
        if not (record and _open_waits(record)) and not (session and session in omp.pins()):
            raise BadInput("nothing is set aside here: add a wait first")
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
            _remember_tern_session(record, agents)
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
            # What is on me, when something is: a wait still watched for me
            # stays. With nothing on me, Done ends them all.
            mine = [wait for wait in _open_waits(record) if _wait_ball(wait) == "on_me"]
            for wait in mine or _open_waits(record):
                if not mine:
                    wait["next"] = None
                _complete(state, record, wait, now)
            _settle(state, record, now)
            unpin = not _open_waits(record)
        return _move_pin(state, session, False if unpin else None, omp, now)

    return _mutate(apply, omp=omp)


def _prompt_handed_over(record: dict, wait: dict) -> bool:
    """The agent has the wait's prompt, or ran it and its check is queued."""
    return wait["verify_pending"] or any(
        entry["wait"] == wait["id"] and entry["state"] != "queued"
        for entry in record["prompt_queue"]
    )


def _snooze_hides(wait: dict) -> bool:
    """Whether Snooze 1d only hides the wait until then. Its event already
    happened: watching for it again would wait for it twice, run a prompt that
    ran again, or arm the chain's next step a second time. A Time wait is the
    exception: it waits for the new time and its prompt runs then, unless it
    already moved its chain on."""
    if wait["kind"] == "time":
        return wait["state"] == "fired" and bool(wait["next"])
    if wait["kind"] == "job" or wait["state"] in ("fired", "review"):
        return True
    # A check that failed comes back to its checks; a prompt that went wrong
    # comes back to me.
    return wait["state"] == "needs_attention" and bool(wait.get("fired_at"))


def cmd_snooze(args: argparse.Namespace) -> dict:
    """Snooze 1d: the wait comes off me until `--for` from now. A waiting one is
    watched again from then; one whose event happened is hidden until then and
    comes back as it was (`_unsnooze`)."""
    session, pane = _target(args)
    delay = timedelta(seconds=_parse_duration(args.for_))

    def apply(state: dict, now: datetime) -> dict:
        record = _lookup(state, session, pane)
        wait = _target_wait(record, args.wait)
        if wait["state"] == "pending":
            raise BadInput(f"{args.wait} has not started yet")
        if wait["kind"] == "later":
            raise BadInput(f"{args.wait} waits for my time: nothing brings it back")
        if wait["kind"] == "job" and wait["state"] == "waiting" and not wait["overdue"]:
            raise BadInput(f"{args.wait} is a job that still runs: it comes back when it ends")
        # A repeat's run goes on whatever the wait does next.
        if wait["kind"] != "time" and not wait.get("repeat") and _prompt_handed_over(record, wait):
            raise BadInput(f"{args.wait}: the agent runs its prompt now")
        until = _iso(now + delay)
        snoozed = f"Snoozed until {_clock(_parse_time(until), now)}"
        if wait["overdue"] and wait["state"] == "waiting":
            # Only the deadline moves: its checks, and a repeat's runs, go on.
            wait.update(deadline=until, deadline_in_s=None, overdue=False, on_me=False, reason=None)
            _touch(wait)
            _log(record, now, wait, snoozed)
            return {}
        condition = wait["condition"]
        # How far ahead `_clamp` lets the times this sets lie.
        wait["snooze_s"] = int(delay.total_seconds())
        if wait["state"] == "snoozed":
            wait["refire_at"] = until
        elif _snooze_hides(wait):
            # Its reason stays for when it comes back; a deadline that passed
            # moves below.
            wait.update(
                snoozed_from=wait["state"], state="snoozed", refire_at=until,
                on_me=False, overdue=False, verify_pending=False,
            )
            _checks_work(wait)
        else:
            if wait["kind"] == "time":
                condition["at"] = until
                wait["armed_at"] = _iso(now)
            elif wait["kind"] == "something":
                # A deadline is the only time it has, so a snooze sets one.
                wait.update(deadline=until, deadline_in_s=None)
            elif wait["kind"] != "repeat":
                wait["next_check_at"] = until
            if wait.get("repeat"):
                wait["repeat"]["next_at"] = until
            wait.update(
                state="waiting", on_me=False, overdue=False, reason=None, errors=0, verify_pending=False,
            )
            _checks_work(wait)
        if (_stamp(wait["deadline"]) or math.inf) <= now.timestamp():
            wait.update(deadline=until, deadline_in_s=None)
        if wait.get("repeat"):
            _cancel_queued_runs(record, wait)
        else:
            _cancel_prompts(record, wait)
        _touch(wait)
        _log(record, now, wait, snoozed)
        return {}

    return _mutate(apply)


def cmd_retry(args: argparse.Namespace) -> dict:
    """Retry now: a wait check errors put on me goes back to its checks, or its
    verification, and the next tick runs them; a quarantined one goes back to
    what it was."""
    session, pane = _target(args)

    def apply(state: dict, now: datetime) -> dict:
        record = _lookup(state, session, pane)
        wait = _target_wait(record, args.wait)
        if wait["state"] != "needs_attention" or not wait.get("retry_as"):
            raise BadInput(f"{args.wait} did not stop on failed checks or an internal error")
        _rearm(wait, now)
        _log(record, now, wait, f"Retry: {wait['label']}")
        return {}

    return _mutate(apply)


def cmd_end_repeat(args: argparse.Namespace) -> dict:
    """The agent saw the repeat's `until` hold: the wait fires, with no prompt."""
    reason = _text(args.reason)

    def apply(state: dict, now: datetime) -> dict:
        record = state["sessions"].get(args.session)
        wait = _target_wait(record, args.wait)
        if not wait.get("repeat"):
            raise BadInput(f"{args.wait} does not repeat")
        if wait["state"] == "pending":
            raise BadInput(f"{args.wait} has not started yet")
        # A snoozed repeat had ended: only a fired one is hidden that way.
        if wait["state"] not in ("waiting", "needs_attention"):
            raise BadInput(f"{args.wait} has already ended")
        why = f": {reason}" if reason else ""
        _end_repeat(state, record, wait, now, f"the agent ended it{why}")
        return {"wait": _wait_view(wait, now)}

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
            _drop_resume(state, session)
        else:
            state["panes"].pop(str(pane), None)
        return {}

    return _mutate(apply)


def _settle_turn(state: dict, record: dict, now: datetime) -> None:
    """The delivered prompt's turn ended: queue verification, or hand it to me."""
    queue = record["prompt_queue"]
    if not queue or queue[0]["state"] != "delivered":
        return
    entry = queue.pop(0)
    if entry.get("repeat"):
        _run_over(state, record, entry, now)
        return
    wait = _wait_in(record, entry["wait"])
    if wait is None or wait["state"] != "acting":
        return
    wait.update(on_me=False, reason=None)
    verify = wait["verify"]
    if verify is None or (verify["type"] == "pr_fix" and not wait["snapshot"]):
        # A wait the agent set during the turn is the next step the run decided
        # on: the one that ran is behind it, not on me. One I added meanwhile
        # is my own business, and chain steps waiting behind it, set up with
        # it, are not that either.
        start = _stamp(entry.get("claimed_at"))
        after = next((
            other for other in _open_waits(record)
            if other is not wait and other["state"] != "pending" and other["by"] == "agent"
            and start is not None and _stamp(other["created_at"]) > start
        ), None)
        if after is not None:
            _log(record, now, wait, f"{wait['label']}: ran; superseded by {after['label']}")
            _complete(state, record, wait, now)
            return
        wait.update(state="review", reason=REASON_REVIEW)
        _touch(wait)
        _log(record, now, wait, f"{wait['label']}: {REASON_REVIEW}")
        record["unseen"] = True
        _toast(state, record, wait, now, "info", f"{wait['label']}: {REASON_REVIEW}")
    else:
        wait.update(verify_pending=True, verify_claim_until=None)
        _touch(wait)


def _turn_finished(state: dict, record: dict, now: datetime) -> None:
    _settle_turn(state, record, now)
    for wait in _open_waits(record):
        if wait["reason"] == REASON_QUESTION:
            wait.update(on_me=False, reason=None)


def _settle_idle(state: dict, record: dict, now: datetime) -> None:
    """An idle session whose delivered prompt has had time to run: its turn
    ended unseen (the event got lost), or never ran (omp died after the prompt
    landed). Either way it is over, as `turn_finished` would say."""
    queue = record["prompt_queue"]
    if not queue or queue[0]["state"] != "delivered":
        return
    delivered = _stamp(queue[0].get("delivered_at") or queue[0].get("claimed_at"))
    if delivered is not None and now.timestamp() - delivered > SETTLE_IDLE_S:
        _turn_finished(state, record, now)


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

    def apply(state: dict, now: datetime) -> dict:
        # The Tern daemon whose pane the event is about: `--daemon`, else the one
        # this command runs under.
        reached = _reached_by(state, args.daemon or _ancestor_daemon())
        record = state["sessions"].get(session) if session else None
        if kind == "turn_finished" and record:
            _turn_finished(state, record, now)
        elif kind == "waiting_input" and record:
            for wait in _open_waits(record):
                # A repeat stays `waiting` while its run is with the agent.
                running = (wait.get("repeat") or {}).get("running")
                with_agent = any(
                    entry["action"] == running and entry["state"] != "queued"
                    for entry in record["prompt_queue"]
                )
                if wait["state"] == "acting" or with_agent:
                    _flag_on_me(state, record, wait, now, REASON_QUESTION)
        elif kind == "job_finished":
            finished_jobs = [
                (held, wait) for held in _records(state) for wait in held["waits"]
                if _watches_job(wait, args.pane) and reached(wait)
            ]
            for held, wait in finished_jobs:
                _finish_job(state, held, wait, args.status, now)
        elif kind == "pane_closed":
            _pane_closed(state, args.pane, now, keep=lambda item: not reached(item))
        return {}

    return _mutate(apply, agents=agents)


def cmd_heartbeat(args: argparse.Namespace) -> dict:
    features = _comma_list(args.features or "")

    def apply(state: dict, now: datetime) -> dict:
        state["extensions"][args.session] = _iso(now)
        state["extension_features"][args.session] = features
        if args.pane is not None:
            # A pane runs one session: a newer one there replaces the old mapping.
            panes = state["extension_panes"]
            state["extension_panes"] = {
                session: pane for session, pane in panes.items() if pane != args.pane
            }
            state["extension_panes"][args.session] = args.pane
        record = state["sessions"].get(args.session)
        if record:
            _clear_delivery_flags(record)
            if args.idle:
                _settle_idle(state, record, now)
        return {}

    return _mutate(apply)


def cmd_menu_request(args: argparse.Namespace) -> dict:
    """Tern can't draw in omp's composer: the session's extension opens the menu
    when it sees the request in the view."""
    session, pane = _target(args)
    now = _now()
    agents = _read_agents()
    with _transaction() as state:
        if pane is not None:
            session = next((
                held for held in state["extension_panes"]
                if _reported_pane(state, agents, held, now) == pane
            ), None)
            if session is None:
                return {"ok": True, "menu": False, "reason": REASON_MENU_NO_PANE}
        elif not _extension_fresh(state, session, now):
            return {"ok": True, "menu": False, "session": session, "reason": REASON_MENU_NO_EXTENSION}
        if FEATURE_MENU not in state["extension_features"].get(session, ()):
            return {"ok": True, "menu": False, "session": session, "reason": REASON_MENU_OLD_EXTENSION}
        state["menu_seq"] += 1
        state["menu_requests"][session] = {"seq": state["menu_seq"], "at": _iso(now)}
        return {"ok": True, "menu": True, "session": session, "view": _publish(state, now, agents)}


def cmd_claim_prompts(args: argparse.Namespace) -> dict:
    def apply(state: dict, now: datetime) -> dict:
        # Only a loaded extension claims, so this is as good as a heartbeat.
        state["extensions"][args.session] = _iso(now)
        record = state["sessions"].get(args.session)
        head = _claimable(record) if record else None
        if head is None:
            return {"prompts": []}
        resend_check = head["state"] == "sending"
        # A resend check looks only at messages since the earlier claim.
        claimed_at = head["claimed_at"] if resend_check else None
        head.update(state="sending", claimed_at=_iso(now))
        wait = _wait_in(record, head["wait"])
        if wait is not None:
            wait.update(on_me=False, reason=None)
        return {"prompts": [{
            "action": head["action"], "wait": head["wait"], "text": head["text"],
            "resend_check": resend_check, "claimed_at": claimed_at,
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
    entry.update(state="delivered", delivered_at=_iso(now))
    if wait is not None:
        wait.update(on_me=False, reason=None)
        # A run logged itself when it was queued.
        if not entry.get("repeat"):
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
            if action["type"] == "resume" and args.result == "done":
                record = state["sessions"].get(action["session"])
                if record is not None:
                    record["resume_opened_at"] = _iso(now)
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
    try:
        return _tick(args)
    except Exception as error:
        _report_tick_failure(error)
        raise


def _for_other_panes(action: dict, daemon: str | None) -> bool:
    """A park or close_pane another daemon's pane ids name: this daemon's
    windows would act on the wrong pane. It stays queued until it expires."""
    return (
        daemon is not None and action["type"] in PANE_ACTIONS
        and action.get("daemon") not in (None, daemon)
    )


def _ticked_elsewhere(state: dict, daemon: str, now: datetime) -> bool:
    """Another Tern daemon holds the ticker while its last tick is recent. One
    that still runs counts that in time the machine was awake, so a suspend
    doesn't cost it the ticker but its plugin going quiet does; one that is gone
    holds nothing. One whose life can't be checked counts the wall clock, and a
    tick dated more than a minute ahead (a clock set back) is old."""
    ticker = state.get("ticker") or {}
    if not ticker.get("daemon") or ticker["daemon"] == daemon:
        return False
    alive = _daemon_alive(ticker["daemon"])
    if alive is False:
        return False
    awake = ticker.get("awake")
    if alive and type(awake) in (int, float):
        return 0 <= _awake_s() - awake < TICKER_LEASE_S
    at = _stamp(ticker.get("at"))
    return at is not None and -TICKER_AHEAD_S <= now.timestamp() - at < TICKER_LEASE_S


def _hold_ticker(state: dict, daemon: str, now: datetime) -> None:
    state["ticker"] = {"daemon": daemon, "at": _iso(now), "awake": _awake_s()}


def _awake_s() -> float:
    """Seconds the machine was awake, on a clock every process of this boot shares."""
    return time.monotonic()


def _daemon_alive(daemon: str) -> bool | None:
    """Whether the `<pid>-<start time>` daemon still runs: False when its process
    is gone or is another one now (a restart, a reboot). None when /proc can't
    tell, or the id is in another form."""
    pid, _, start = daemon.partition("-")
    if not (pid.isdigit() and start.isdigit()) or not (PROC / "self" / "stat").exists():
        return None
    stat = _proc_stat(pid)
    return stat is not None and stat[2] == start


def _note_tick(state: dict, now: datetime) -> None:
    """A tick long after the last one follows a sleep (or a stopped daemon):
    extensions get a heartbeat's time from then (`_extension_lost`)."""
    last = _stamp(state.get("last_tick_at"))
    if last is not None and now.timestamp() - last > 2 * TICK_MAX_S:
        state["woke_at"] = _iso(now)
    state["last_tick_at"] = _iso(now)


def _tick(args: argparse.Namespace) -> dict:
    def moment() -> datetime:
        return _parse_time(args.now) if args.now else _now()

    now = moment()
    agents = _read_agents()
    stale = _agents_stale(agents, now)
    omp = Omp()
    # What went wrong beyond any one record: the view's `health.error`.
    problems: list[str] = []

    def finish(state: dict, agents: Agents) -> dict:
        # As agents.json read last: after slow checks, not before them.
        state["agents_stale_since"] = (state.get("agents_stale_since") or _iso(now)) if stale else None
        if args.daemon:
            # The lease runs from the end of the tick: slow checks don't shorten it.
            _hold_ticker(state, args.daemon, now)
        return {
            "ok": True,
            "actions": [
                _public_action(action) for action in state["actions"]
                if not _for_other_panes(action, args.daemon)
            ],
            "next_tick_s": _next_tick_s(state, now),
            "view": _publish(state, now, agents, omp, ticked=True, problems=problems),
        }

    with _transaction() as state:
        if args.daemon and _ticked_elsewhere(state, args.daemon, now):
            return {
                "ok": True, "skipped": TICK_SKIPPED, "actions": [], "next_tick_s": TICK_MAX_S,
                "view": _build_view(state, now, agents, omp=omp),
            }
        _note_tick(state, now)
        _housekeeping(state, agents, omp, now, args.daemon)
        _clamp(state, now, problems)
        _pin_pass(state, omp, now)
        _evaluate(state, now)
        _delivery_pass(state, agents, now, args.window, stale)
        jobs = _claim_checks(state, now)
        answer = None if jobs else finish(state, agents)
    if jobs:
        finished = _run_jobs(jobs, now)
        # The slow checks may have taken minutes: a session can have come back,
        # and what they found is counted from now (`finish` reads the new `now`).
        now = moment()
        agents = _read_agents()
        stale = _agents_stale(agents, now)
        with _transaction() as state:
            _merge(state, finished, now)
            _delivery_pass(state, agents, now, args.window, stale)
            answer = finish(state, agents)
    cache = PrCache(now=now)
    due = cache.stale(
        [pr for session in answer["view"]["sessions"] for pr in session["prs"]], now,
    )
    if due:
        _refresh_prs(due, now)
        now = moment()
        cache = PrCache(now=now)
    merged = _merged_prs(cache, _load_state())
    if due or merged:
        with _transaction() as state:
            for pr in merged:
                _pr_merged(state, pr, now)
            answer = finish(state, agents)
    return answer


def cmd_resume(args: argparse.Namespace) -> dict:
    agents = _read_agents()
    if not agents.known:
        raise BadInput("agents.json is unreadable: can't tell whether the session runs")
    state = _load_state()
    if _agents_stale(agents, _now()):
        # It can't say which session runs: codexbar_focus.py --reserve, which
        # the reopen goes through, checks the processes.
        agents = agents.untrusted()
    elif _running(state, agents, args.session):
        # A session file has one live writer.
        raise BadInput("session is running")
    facts = Facts(agents)
    record = state["sessions"].get(args.session)
    if _pending_resume(state, args.session) is not None or (
        record is not None and _resume_opened_lately(record, _now())
    ):
        # Tern opens it, or just did, and agents.json doesn't list it yet. A
        # queued resume counts until a tick drops it: until then a window may
        # still take it.
        raise BadInput("that session is reopening already")
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
    """Works for any omp session, with or without a record, live or closed.
    `--cached` answers at once from local files; the plain form also asks GitHub
    and git."""
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
    cache = PrCache(scan=not args.cached)
    if not args.cached:
        prs = cache.session_prs(session, transcript, record)
        cache.save()
        _refresh_prs(
            cache.stale(prs, now, sources=("created", "wait", "mentioned"), settled_s=PR_STALE_S),
            now,
        )
        cache = PrCache()
    facts.prefetch([session])
    view = _session_view(session, record, facts, pinned, state, now, cache)
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
        "branch": None if args.cached else _git_branch(view["cwd"]),
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
    add.add_argument("--cwd")
    add.add_argument("--interval")
    add.add_argument("--predicate")
    add.add_argument("--what")
    _add_pr_options(add)
    # A PR's state for `pr`, the agent's words for `repeat`.
    add.add_argument("--until")
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
    add.add_argument("--every")
    add.add_argument("--for", dest="for_")
    add.add_argument("--times", type=int)

    retry = command("retry", cmd_retry)
    _add_target(retry)
    retry.add_argument("--wait", required=True)

    end_repeat = command("end-repeat", cmd_end_repeat)
    end_repeat.add_argument("--session", type=_session_arg, required=True)
    end_repeat.add_argument("--wait", required=True)
    end_repeat.add_argument("--reason")

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
        ("seen", cmd_seen),
        ("claim-prompts", cmd_claim_prompts), ("resume", cmd_resume),
    ):
        command(name, run).add_argument("--session", type=_session_arg, required=True)

    card = command("card", cmd_card)
    card.add_argument("--session", type=_session_arg, required=True)
    card.add_argument("--cached", action="store_true")

    heartbeat = command("heartbeat", cmd_heartbeat)
    heartbeat.add_argument("--session", type=_session_arg, required=True)
    heartbeat.add_argument("--pane", type=_pane_arg)
    heartbeat.add_argument("--features")
    # omp runs no turn and streams nothing (`_settle_idle`).
    heartbeat.add_argument("--idle", action="store_true")

    menu_request = command("menu-request", cmd_menu_request)
    _add_target(menu_request)

    remove = command("remove", cmd_remove)
    _add_target(remove)

    event = command("event", cmd_event)
    event.add_argument(
        "--type", required=True,
        choices=("turn_finished", "waiting_input", "job_finished", "pane_closed"),
    )
    _add_target(event)
    event.add_argument("--status", type=int)
    # Whose pane `--pane` is: pane events reach only that daemon's items.
    event.add_argument("--daemon")

    ack = command("ack", cmd_ack)
    ack.add_argument("--action", required=True)
    ack.add_argument("--result", required=True, choices=("delivered", "failed", "done"))
    ack.add_argument("--error")

    tick = command("tick", cmd_tick)
    tick.add_argument("--now")
    # Tern says a window is up to carry out a resume; until one is, none expires.
    tick.add_argument("--window", action="store_true")
    # The Tern daemon that ticks: one ticks at a time, and pane ids are its own.
    tick.add_argument("--daemon")

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

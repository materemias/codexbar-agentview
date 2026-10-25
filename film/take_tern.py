#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Films the Tern take for docs/tern.mp4 and docs/tern.gif: this checkout's tern/ plugin in a real
Tern window, on a private session daemon with a scratch HOME, so your windows, sessions, usage and
CodexBar AgentView state stay out of it. The window holds a made-up workspace whose panes run
stand-in agents (this script's --pane mode); agents.json and usage_cache.json are fixtures it
writes. A `python3` shim first on the daemon's PATH turns the plugin's /proc sweeps into a rewrite
of the fixture and its usage fetches into cache reads. $SHELL runs History's restores with `omp`,
`claude` and `codex` stand-ins first on PATH, so they reopen stand-ins, never a real agent. The
window is shot about ten times a second; the frames, captions, key presses and camera framings go
to WORK/timeline.json for film/compose.py.

    uv run film/take_tern.py [WORK]     (default /tmp/codexbar-agentview-film-tern)
    uv run film/compose.py tern [WORK]

Needs Tern and a Wayland session: it opens a Tern window on your desktop for about a minute.
"""

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TAKE = Path(__file__).resolve()
SCRIPTS = REPO / "contents/scripts"
PYTHON = shutil.which("python3") or "/usr/bin/python3"
SIZE = (1500, 700)
FONT = 14
TRANSITION_MS = 1400
PROVIDERS = ["claude", "codex", "zai", "opencodego", "openrouter", "kilo"]
NAMES = {"claude": "Claude Code", "codex": "Codex", "omp": "omp"}

# The made-up workspace: one tab per project, a pane per live session. `turns` are the session's
# last messages as the peek shows them; `ago` is minutes since its state changed.
SESSIONS = [
    {"id": "4f1c2a90-7d3e-4b51-9a0e-2c6d8e1f3a01", "folder": "atlas", "title": "Review cache invalidation",
     "provider": "codex", "model": "gpt-5.4", "state": "working", "ago": 3,
     "turns": [("user", "text", "Check whether a failed cache refresh keeps the previous result."),
               ("assistant", "text", "The previous value stays available while a refresh runs. Checking the failure path now."),
               ("assistant", "tools", "Read src/cache/refresh.ts · Grep \"onRefreshError\"")]},
    {"id": "8b2e6d14-3c9a-4f07-b5d1-6e0a9c4b7f02", "folder": "atlas", "title": "Polish settings layout",
     "provider": "claude", "model": "claude-sonnet-4-6", "state": "idle", "ago": 18,
     "turns": [("user", "text", "Tighten the spacing on the settings page and line up the switches."),
               ("assistant", "text", "Done. Rows share one 12 px grid and every switch sits on the right edge. The page is 40 px shorter.")]},
    {"id": "019a3e5c-6b7d-7f21-a8c4-3d5e7f9a1b03", "folder": "harbor", "title": "Trace upload retries",
     "provider": "omp", "model": "claude-opus-4-6", "state": "working", "ago": 6,
     "turns": [("user", "text", "Uploads over 50 MB retry forever on a flaky network. Find out why."),
               ("assistant", "text", "The retry loop resets its attempt counter after every partial success, so it never reaches the limit."),
               ("assistant", "tools", "Read src/upload/retry.ts · Ran the upload tests")]},
    {"id": "c3d9f2a7-1e4b-4c86-9f30-7a2b5d8e6c04", "folder": "harbor", "title": "Review query timeout",
     "provider": "codex", "model": "gpt-5.4", "state": "blocked", "ago": 9,
     "turns": [("user", "text", "Reports time out after 30 s on large accounts. Review the query."),
               ("assistant", "text", "The report joins events without an index on account_id. Adding one means a migration on a 40 GB table."),
               ("assistant", "text", "Run the migration now, or schedule it for tonight?")],
     "options": ["Run it now", "Schedule it for tonight"]},
    {"id": "5e7a1c3b-9d2f-4a68-8b14-0f6c3e9d2a05", "folder": "harbor", "title": "Check export permissions", "away": True,
     "provider": "claude", "model": "claude-sonnet-4-6", "state": "idle", "ago": 41,
     "turns": [("user", "text", "Make sure viewers can't export billing data."),
               ("assistant", "text", "Viewers were blocked in the UI but not in the API. The export endpoint now checks the role too, with a test for each role.")]},
    {"id": "019a3f0d-2c4e-7b39-9e51-8f0a2c4d6e06", "folder": "beacon", "title": "Fix runner shutdown", "away": True,
     "provider": "omp", "model": "gpt-5.4", "state": "working", "ago": 2,
     "turns": [("user", "text", "The runner hangs on SIGTERM when a job is mid-upload."),
               ("assistant", "text", "Shutdown waits on the upload, and the upload waits on a context nobody cancels. Wiring the shutdown context through."),
               ("assistant", "tools", "Edited src/runner/shutdown.go · Ran go test ./runner/...")]},
    {"id": "9a4c7e2f-5b1d-4e93-a6c8-1d3f5b7e9c07", "folder": "beacon", "title": "Review webhook errors",
     "provider": "claude", "model": "claude-sonnet-4-6", "state": "blocked", "ago": 7,
     "turns": [("user", "text", "Webhook deliveries fail with 401 since we rotated the signing secret."),
               ("assistant", "text", "The verifier still reads the old secret from a config cache that only reloads on restart."),
               ("assistant", "tools", "Read src/webhooks/verify.ts · Grep \"SIGNING_SECRET\""),
               ("assistant", "text", "Should I reload the secret when it rotates, or read it on every request?")],
     "options": ["Reload on rotation", "Read it on every request"],
     "recap": "Webhooks fail since the secret rotation: the verifier caches the old secret. Waiting for a choice between reloading on rotation and reading per request."},
    {"id": "e6b8d1f4-7a3c-4d25-b9e0-5c7a9e1b3d08", "folder": "beacon", "title": "Improve log grouping", "away": True,
     "provider": "codex", "model": "gpt-5.4", "state": "working", "ago": 4,
     "turns": [("user", "text", "Group repeated errors in the log view by their stack trace."),
               ("assistant", "text", "Grouping by a hash of the top five frames. Writing tests for traces that differ only in line numbers.")]},
]
# Sessions that exited on this boot; the take brings back the newest one. No row is interrupted by a
# restart: Tern brings its own sessions back after one, so the film shows no Restore all.
HISTORY = [
    {"id": "d2f5a8c1-4b7e-4093-8c26-9e1b4d7a0c10", "folder": "atlas", "title": "Fix keyboard navigation",
     "provider": "codex", "model": "gpt-5.4", "closedBy": "exit", "ago": 6,
     "turns": [("user", "text", "Keep the selection on the same session when the list changes."),
               ("assistant", "text", "Selection now follows the session, not the row index. Two tests cover a list that reorders under it.")]},
    {"id": "7c0e3b6a-2d9f-4e51-a4b8-6f2c8e0a4d11", "folder": "harbor", "title": "Update API docs",
     "provider": "claude", "model": "claude-sonnet-4-6", "closedBy": "exit", "ago": 190,
     "turns": [("user", "text", "Document the new pagination parameters."),
               ("assistant", "text", "Added cursor and limit to the reference, with an example for each endpoint.")]},
    {"id": "019a3d12-4f6a-7c83-b2d9-5e1f3a7c9b14", "folder": "beacon", "title": "Tune alert thresholds",
     "provider": "omp", "model": "gpt-5.4", "closedBy": "exit", "ago": 310,
     "turns": [("user", "text", "Alerts fire on every deploy. Tune the thresholds."),
               ("assistant", "text", "Error-rate alerts now wait five minutes after a deploy before they fire.")]},
]
RESUME_PREFIXES = {"claude": ("claude", "--resume"), "codex": ("codex", "resume"), "omp": ("omp", "--resume")}


def resume_command(provider: str, sid: str, cwd: str) -> str:
    """codexbar_agents._resume_command for a proven identity, which launch checks against."""
    return f"{shlex.join(['cd', '--', cwd])} && {shlex.join([*RESUME_PREFIXES[provider], sid])}"


def write_json(path: Path, data) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, path)


# --refresh: what a sweep of the film's world finds -------------------------------------------------


def refresh(work: Path) -> None:
    """Writes agents.json from WORK/world.json, measured now. The plugin sweeps only when the
    snapshot is over 15 s old; the take refreshes it every few seconds and the python3 shim answers
    any sweep with this, so no /proc scan ever writes your real sessions into the film."""
    world = json.loads((work / "world.json").read_text())
    now = int(time.time() * 1000)
    agents = [{**r, "updatedAt": now} for r in world["agents"]]
    counts = {s: sum(r["state"] == s for r in agents) for s in ("working", "blocked", "idle", "untracked")}
    counts["total"] = len(agents)
    state = Path(world["stateDir"])
    write_json(state / "agents.json", {
        "bootId": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "updatedAt": now, "counts": counts, "agents": agents, "history": world["history"], "ternInstalled": True,
    })


# --pane: a stand-in agent's screen -------------------------------------------------------------------

RESET, BOLD, DIM = "\x1b[0m", "\x1b[1m", "\x1b[2m"
TINT = {"claude": "\x1b[38;5;173m", "codex": "\x1b[38;5;111m", "omp": "\x1b[38;5;141m"}
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
# Tern keeps a pane's terminal at the size it was opened with and reflows the grid to the pane, so a
# line wider than the narrowest pane the take shows (a third of the window) would break mid-word.
TEXT_COLS = 50


def pane(work: Path, sid: str, resumed: bool) -> None:
    """Draws one session as a terminal agent would: its last messages, then a spinner while it
    works, its question while it waits for you, or a prompt while it is idle."""
    session = json.loads((work / "sessions.json").read_text())[sid]
    if resumed:
        session = {**session, "state": "idle", "options": None}
    tint = TINT.get(session["provider"], "")
    since = time.time() - session["ago"] * 60
    sys.stdout.write(f"\x1b]2;{session['title']}\x07")

    def draw():
        out = ["\x1b[?25l\x1b[H\x1b[2J\x1b[3J\n"]
        head = f"{NAMES[session['provider']]} · {session['model']} · ~/code/{session['folder']}"
        out.append(f"  {tint}{BOLD}✻{RESET} {DIM}{head}{RESET}\n")
        if resumed:
            out.append(f"  {DIM}Resumed {session['title']}{RESET}\n")
        out.append("\n")
        for role, kind, text in session["turns"]:
            lines = textwrap.wrap(text, TEXT_COLS) or [""]
            if role == "user":
                mark, style = f"{BOLD}❯{RESET} ", BOLD
            elif kind == "tools":
                mark, style = f"{DIM}⎿{RESET} ", DIM
            else:
                mark, style = f"{tint}●{RESET} ", ""
            out.append(f"  {mark}{style}{lines[0]}{RESET}\n")
            out.extend(f"    {style}{line}{RESET}\n" for line in lines[1:])
            out.append("\n")
        for n, option in enumerate(session.get("options") or [], 1):
            out.append(f"    {tint if n == 1 else DIM}{n}. {option}{RESET}\n")
        if session["state"] == "working":
            out.append("  ")
        else:
            rule = "─" * (TEXT_COLS + 2)
            out.append(f"\n  {DIM}{rule}{RESET}\n  {BOLD}❯{RESET} \x1b[?25h")
        sys.stdout.write("".join(out))
        sys.stdout.flush()

    draw()
    tick = 0
    while True:
        if session["state"] == "working":
            elapsed = int(time.time() - since)
            sys.stdout.write(f"\r  {tint}{SPIN[tick % len(SPIN)]}{RESET} {DIM}Working · {elapsed // 60}m {elapsed % 60:02d}s{RESET}\x1b[K")
            sys.stdout.flush()
            tick += 1
        time.sleep(0.1)


# The take ------------------------------------------------------------------------------------------


class Take:
    def __init__(self, work: Path):
        self.work = work
        self.home = work / "home"
        self.state = self.home / ".local/state"
        self.state_dir = self.state / "codexbar-agentview"
        self.bin = work / "bin"
        self.sock = work / "win.sock"
        self.daemon_sock = work / "d.sock"
        self.config = work / "tern-config"
        self.shots = work / "target/shots/tern/live"
        self.procs: list[subprocess.Popen] = []
        self.world_lock = threading.Lock()
        self.events: list[dict] = []
        self.frames: list[dict] = []
        self.filming = False
        self.t0 = time.monotonic()
        self.pointer = (SIZE[0] * 0.5, SIZE[1] * 0.5)

    def now(self) -> float:
        return (time.monotonic() - self.t0) * 1000

    # Setup.

    def prepare(self):
        """The scratch HOME, the shims, the fixtures and a private Tern configuration."""
        sentinel = self.work / ".take_tern"
        if self.work.exists():
            if not sentinel.exists():
                sys.exit(f"{self.work} exists and wasn't made by film/take_tern.py: pick a new folder")
            shutil.rmtree(self.work)
        self.shots.mkdir(parents=True)
        sentinel.write_text("")
        self.state_dir.mkdir(parents=True)
        self.bin.mkdir()
        for folder in {s["folder"] for s in SESSIONS + HISTORY}:
            (self.home / "code" / folder).mkdir(parents=True, exist_ok=True)
        log = self.work / "helpers.log"
        shim = self.bin / "python3"
        shim.write_text(textwrap.dedent(f"""\
            #!/bin/sh
            # film/take_tern.py: the plugin's helpers never scan /proc or fetch usage in the film.
            echo "$*" >> {shlex.quote(str(log))}
            case "$1" in
            */codexbar_agents.py)
              for a in "$@"; do [ "$a" = --once ] && exec {PYTHON} {shlex.quote(str(TAKE))} --refresh {shlex.quote(str(self.work))}; done ;;
            */codexbar_fetch.py)
              for a in "$@"; do [ "$a" = --cache-only ] && exec {PYTHON} "$@"; done
              exec {PYTHON} "$@" --cache-only ;;
            esac
            exec {PYTHON} "$@"
            """))
        agent = textwrap.dedent(f"""\
            #!/bin/sh
            # film/take_tern.py: a restored session (`<agent> --resume <id>`) runs as a stand-in.
            for a in "$@"; do id="$a"; done
            exec {PYTHON} {shlex.quote(str(TAKE))} --pane {shlex.quote(str(self.work))} "$id" --resumed
            """)
        # Only the restore shell puts these on PATH.
        agents = self.work / "agents"
        agents.mkdir()
        for name in ("omp", "claude", "codex"):
            (agents / name).write_text(agent)
        # Tern starts panes with your login HOME and PATH, whatever its daemon's are. A restore runs
        # `$SHELL -ic '<resume command>'` in a new tab, so $SHELL puts the stand-ins first and reads
        # no startup file that could find a real agent.
        shell = self.bin / "film-shell"
        shell.write_text(textwrap.dedent(f"""\
            #!/bin/sh
            export HOME={shlex.quote(str(self.home))} PATH={shlex.quote(str(agents))}:/usr/bin:/bin PS1='\\W $ '
            exec /bin/bash --norc --noprofile "$@"
            """))
        for f in [*self.bin.iterdir(), *agents.iterdir()]:
            f.chmod(0o755)

        self.env = {k: v for k, v in os.environ.items()
                    if not re.match(r"^(TERN_|PI_|OMP_|XDG_(DATA|STATE|CONFIG|CACHE)_HOME$|CODEX_HOME$|CLAUDE_CONFIG_DIR$|.*_API_KEY$)", k)}
        runtime = Path(self.env.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
        if not self.env.get("WAYLAND_DISPLAY"):
            sockets = sorted(p.name for p in runtime.glob("wayland-[0-9]"))
            if not sockets:
                sys.exit("no Wayland session: run the take from your desktop session")
            self.env["WAYLAND_DISPLAY"] = sockets[0]
        self.env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime}/bus")
        self.env.update(
            HOME=str(self.home), XDG_STATE_HOME=str(self.state), SHELL=str(shell),
            PATH=f"{self.bin}:{self.env.get('PATH', '/usr/bin:/bin')}",
            TERN_DAEMON_SOCKET=str(self.daemon_sock), TERN_CONFIG_DIR=str(self.config),
        )

        self.sessions = {}
        for s in SESSIONS + HISTORY:
            self.sessions[s["id"]] = {
                **{k: s[k] for k in ("title", "provider", "model", "folder", "turns")},
                "state": s.get("state", "idle"), "ago": s["ago"], "options": s.get("options"),
            }
        write_json(self.work / "sessions.json", self.sessions)
        self.write_usage()

        (self.config / "plugins").mkdir(parents=True)
        (self.config / "plugins/codexbar-agentview.path").write_text(str(REPO / "tern"))
        (self.config / "settings.json").write_text(json.dumps(
            {"theme": "Dark", "status_bar": True, "plugins_disabled": ["agents"], "new_tabs": "Home"}, indent=1))
        # Tern finds the stand-in `omp` through $SHELL and would offer, in a dialog on your desktop,
        # to keep it up to date: this is its record of having asked.
        (self.config / "omp").mkdir()
        (self.config / "omp/state.json").write_text(json.dumps(
            {"managed": False, "checked": 0, "no_build": False, "offered": True}, indent=1))
        # Your Stencil account's picture sits at the left of the tab bar; banners would cover panes.
        (self.work / "film.css").write_text(
            "nav#tabbar button.tn-peers { display: none !important; }\n"
            "div#banners.tn-bn-stack, div#banners.tn-bn-stack > div.tn-bn { display: none !important; }\n"
            ".tn-glow { display: none !important; }\n"
            # The working folder's path is the take's scratch folder, not a project's.
            "footer#statusline > span.sl-cwd { visibility: hidden !important; }\n")

    def write_usage(self):
        """usage_cache.json as the fetcher writes it, measured a minute ago, under the account
        scope the fetcher computes in the film's environment, with a run-length history behind each
        window's recent consumption."""
        scope = subprocess.run(
            [PYTHON, "-c", "import json, sys; sys.path.insert(0, sys.argv[1]); import codexbar_fetch as f; "
             "print(json.dumps(f._usage_cache_scopes(sys.argv[2], sys.argv[3].split(','))))",
             str(SCRIPTS), "/usr/bin/codexbar", ",".join(PROVIDERS)],
            env=self.env, capture_output=True, text=True, check=True)
        scopes = json.loads(scope.stdout)
        now = time.time()
        measured = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now - 60))
        history: dict[str, list] = {}

        def window(provider, email, slot, used, minutes, hours_left, recent=None):
            reset = int(now + hours_left * 3600)
            if recent is not None:
                start = int(now) - (1 if minutes == 300 else 24) * 3600
                history[f"{provider}:{(email or '').casefold()}:{slot}"] = [
                    [start - 1200, start + 600, used - recent, reset], [start + 900, int(now) - 60, used, reset]]
            return {"usedPercent": used, "windowMinutes": minutes,
                    "resetsAt": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(reset))}

        def record(provider, email=None, plan=None, balance=None, **slots):
            r = {"id": provider, "ok": True, "error": None, "updatedAt": measured, "cachedAt": measured,
                 "accountEmail": email, "loginMethod": plan, "balanceText": balance,
                 "primary": None, "secondary": None, "tertiary": None, "extraRateWindows": []}
            for slot, spec in slots.items():
                if slot == "extra":
                    r["extraRateWindows"] = [{"id": "claude-design", "title": "Design", "window": window(provider, email, "extra:claude-design", *spec)}]
                elif slot == "router":
                    r["openRouterUsage"] = spec
                else:
                    r[slot] = window(provider, email, slot, *spec)
            return r

        records = {
            "claude": [record("claude", plan="Max", primary=(28, 300, 2, 9), secondary=(39, 10080, 80, 13), extra=(14, 10080, 80))],
            "codex": [
                record("codex", "studio@example.com", "Pro", primary=(35, 300, 1.5, 4), secondary=(42, 10080, 72, 11)),
                record("codex", "lab@example.com", "Plus", primary=(68, 300, 2, 21), secondary=(76, 10080, 48, 18)),
                record("codex", "team@example.com", "Plus", primary=(18, 300, 3, 0), secondary=(30, 10080, 96, 2)),
            ],
            "zai": [record("zai", primary=(25, 300, 2), secondary=(46, 43200, 240))],
            "opencodego": [record("opencodego", primary=(19, 300, 2), secondary=(28, 10080, 72), tertiary=(32, 43200, 240))],
            "openrouter": [record("openrouter", router={"balance": 18.4})],
            "kilo": [record("kilo", balance="$12.60 left", primary=(37, 43200, 400))],
        }
        providers = {p: {"scope": scopes[p], "records": rs} for p, rs in records.items()}
        write_json(self.state_dir / "usage_cache.json", {"version": 1, "providers": providers, "history": history})

    def start(self):
        out = open(self.work / "tern.log", "w")
        self.procs.append(subprocess.Popen(["tern", "daemon", "--socket", str(self.daemon_sock)],
                                           env=self.env, stdout=out, stderr=out))
        time.sleep(1.5)
        self.procs.append(subprocess.Popen(["tern", "--control", str(self.sock), str(self.home / "code")],
                                           cwd=self.work, env=self.env, stdout=out, stderr=out))
        for _ in range(80):
            if self.sock.exists():
                break
            time.sleep(0.25)
        self.ctl("ready")
        self.ctl("resize", str(SIZE[0]), str(SIZE[1]))
        self.ctl("font", str(FONT))
        self.ctl("css", "codexbar-agentview-film", str(self.work / "film.css"))
        # Opaque, as the README's screenshots: glass lets your desktop through.
        self.ctl("style", "solid")

    def tern(self, *args: str) -> str:
        p = subprocess.run(["tern", *args], env=self.env, capture_output=True, text=True, timeout=20)
        if p.returncode:
            raise RuntimeError(f"tern {' '.join(args)}: {p.stderr or p.stdout}")
        return p.stdout

    def ctl(self, *args: str) -> dict:
        p = subprocess.run(["tern", "ctl", "--control", str(self.sock), *args], env=self.env,
                           capture_output=True, text=True, timeout=30)
        line = next((ln for ln in p.stdout.splitlines() if ln.startswith("{")), None)
        reply = json.loads(line) if line else {"ok": False, "error": p.stdout + p.stderr}
        if not reply.get("ok"):
            raise RuntimeError(f"ctl {' '.join(args)}: {reply.get('error')}")
        return reply

    def blocks(self) -> list[dict]:
        tree = json.loads(self.tern("ls", "--json"))
        return [b for s in tree["sessions"] for t in s["tabs"] for b in t["blocks"]]

    def pane_command(self, sid: str) -> list[str]:
        return [PYTHON, str(TAKE), "--pane", str(self.work), sid]

    def session_name(self) -> str:
        """The name of the session the window shows."""
        return next(s["name"] for s in json.loads(self.tern("ls", "--json"))["sessions"] if s.get("shown"))

    def build_workspace(self):
        """A tab per project with a pane per session, sessions marked `away` in a second Tern
        session, then the fixture pointing at those panes."""
        first = [b["id"] for b in self.blocks()]
        tabs: dict[str, int] = {}
        away = 0
        for s in SESSIONS:
            cwd = str(self.home / "code" / s["folder"])
            launch = ["--cwd", cwd, "--", *self.pane_command(s["id"])]
            if s.get("away"):
                if not away:
                    away = json.loads(self.tern("new", "session", "side work", "--json", *launch))["block"]
                else:
                    self.tern("split", str(away), "right", *launch)
            elif s["folder"] not in tabs:
                made = json.loads(self.tern("new", "tab", "--json", *launch))
                tabs[s["folder"]] = made["block"]
                self.tern("rename", str(made["block"]), s["folder"])
            else:
                self.tern("split", str(tabs[s["folder"]]), "right", *launch)
        for block in first:
            self.tern("close", str(block))
        self.tern("rename", self.session_name(), "Demo workspace")
        self.tabs = tabs
        time.sleep(1.5)
        self.panes = self.pane_processes()
        with self.world_lock:
            self.world = {"stateDir": str(self.state_dir),
                          "agents": [self.live_record(s) for s in SESSIONS],
                          "history": [self.history_record(s) for s in HISTORY]}
            self.save_world()

    def pane_processes(self) -> dict[str, tuple[int, int]]:
        """Each stand-in's pid and the Tern block it runs in (TERN_PANE in its environment)."""
        found = {}
        for proc in Path("/proc").iterdir():
            if not proc.name.isdigit():
                continue
            try:
                argv = (proc / "cmdline").read_bytes().split(b"\0")
                environ = (proc / "environ").read_bytes().split(b"\0")
            except OSError:
                continue
            args = [a.decode() for a in argv if a]
            if "--pane" not in args or str(self.work) not in args:
                continue
            env = dict(e.decode().split("=", 1) for e in environ if b"=" in e)
            if env.get("TERN_PANE_SOCKET") == str(self.daemon_sock) and env.get("TERN_PANE", "").isdigit():
                found[args[args.index("--pane") + 2]] = (int(proc.name), int(env["TERN_PANE"]))
        return found

    def turns(self, s: dict, end_ms: int) -> list[dict]:
        n = len(s["turns"])
        return [{"role": role, "kind": kind, "text": text, "ts": end_ms - (n - i) * 45000}
                for i, (role, kind, text) in enumerate(s["turns"])]

    def live_record(self, s: dict) -> dict:
        now = int(time.time() * 1000)
        changed = now - s["ago"] * 60000
        cwd = str(self.home / "code" / s["folder"])
        pid, pane = self.panes.get(s["id"], (0, 0))
        user = [t for t in s["turns"] if t[0] == "user"]
        return {
            "provider": s["provider"], "sessionId": s["id"], "cwd": cwd, "pid": pid, "ancestorPids": [],
            "host": "tern", "state": s["state"], "model": s["model"], "lastPrompt": user[-1][2] if user else "",
            "recent": self.turns(s, changed), "windowTitle": s["title"], "recap": s.get("recap", ""),
            "recapStale": False, "startedAt": changed - 1800000, "stateChangedAt": changed,
            "identityExact": True, "codexHome": "", "resumeCommand": resume_command(s["provider"], s["id"], cwd),
            "ternPane": pane,
        }

    def history_record(self, s: dict) -> dict:
        seen = int(time.time() * 1000) - s["ago"] * 60000
        cwd = str(self.home / "code" / s["folder"])
        user = [t for t in s["turns"] if t[0] == "user"]
        return {
            "provider": s["provider"], "sessionId": s["id"], "cwd": cwd, "windowTitle": s["title"],
            "lastPrompt": user[-1][2] if user else "", "host": "tern", "desktop": "", "lastState": "idle",
            "lastSeenAt": seen, "model": s["model"], "resumeCommand": resume_command(s["provider"], s["id"], cwd),
            "codexHome": "", "closedBy": s["closedBy"], "recent": self.turns(s, seen), "recap": s.get("recap", ""),
        }

    def save_world(self):
        write_json(self.work / "world.json", self.world)
        refresh(self.work)

    def keep_fresh(self):
        while self.alive:
            with self.world_lock:
                refresh(self.work)
            time.sleep(4)

    def restored(self, sids: list[str]):
        """After a restore from the History tab: those sessions run again, each in its new pane."""
        for _ in range(60):
            self.panes = self.pane_processes()
            if all(sid in self.panes for sid in sids):
                break
            time.sleep(0.25)
        with self.world_lock:
            self.world["history"] = [r for r in self.world["history"] if r["sessionId"] not in sids]
            for s in HISTORY:
                if s["id"] in sids:
                    self.world["agents"].append(self.live_record({**s, "state": "idle", "ago": 0}))
            self.save_world()

    # Filming.

    def shoot(self):
        """Shoots the window about ten times a second for the whole take."""
        while self.filming:
            t = self.now()
            name = f"desktop-{len(self.frames):05d}"
            self.ctl("shot", name)
            self.frames.append({"t": t, "path": str(self.shots / f"{name}.png")})
            time.sleep(max(0, 0.1 - (self.now() - t) / 1000))

    def event(self, kind: str, **fields):
        self.events.append({"t": self.now(), "kind": kind, **fields})

    def caption(self, eyebrow: str, headline: str):
        self.event("caption", eyebrow=eyebrow, headline=headline)

    def cut(self, rect=None, scene="desktop", fill=None, wait=True):
        """A new framing: compose.py moves the camera over TRANSITION_MS, so the action waits."""
        fields = {"scene": scene}
        if rect:
            fields["rect"] = [round(v) for v in rect]
        if fill:
            fields["fill"] = fill
        self.event("scene", **fields)
        if wait:
            time.sleep(TRANSITION_MS / 1000)

    def key(self, chord: str, *caps: str):
        """A key the dashboard takes; compose.py draws `caps` as key caps."""
        self.event("key", keys=list(caps))
        time.sleep(0.12)
        self.ctl("key", chord)

    def command(self, name: str, *caps: str):
        """One of the plugin's keyed commands. `ctl key` reaches the focused element and not
        Tern's keymap, so the command bound to `caps` runs as its key would run it."""
        self.event("key", keys=list(caps))
        time.sleep(0.12)
        self.ctl("plugins", "run", f"plugin.codexbar-agentview.{name}")

    def type(self, text: str, pause=0.11):
        for i, ch in enumerate(text):
            self.event("key", label=text[: i + 1])
            self.ctl("type", json.dumps(ch))
            time.sleep(pause)

    def glide(self, x: float, y: float, ms=500):
        x0, y0 = self.pointer
        steps = 12
        for i in range(1, steps + 1):
            k = i / steps
            e = 2 * k * k if k < 0.5 else 1 - (-2 * k + 2) ** 2 / 2
            at = (x0 + (x - x0) * e, y0 + (y - y0) * e)
            self.ctl("move", f"{at[0]:.1f}", f"{at[1]:.1f}")
            self.event("move", surface="desktop", x=round(at[0], 1), y=round(at[1], 1))
            time.sleep(ms / 1000 / steps)
        self.pointer = (x, y)

    def click(self):
        self.event("click", surface="desktop")
        self.ctl("click", f"{self.pointer[0]:.1f}", f"{self.pointer[1]:.1f}")

    def layout(self) -> list[dict]:
        """The window's elements and boxes, from a shot's layout JSON."""
        self.ctl("shot", "layout")
        return json.loads((self.shots / "layout.layout.json").read_text())["elements"]

    def find(self, pattern: str) -> list[float]:
        for e in self.layout():
            if e["visible"] and re.search(pattern, e["path"]) and e["rect"][2] > 0:
                return e["rect"]
        raise RuntimeError(f"no visible element matches {pattern}")

    def wait_open(self, kind: str, timeout=5.0):
        """Waits until the plugin reports one of its `kind` blocks open (its `open` key)."""
        kv = self.config / "plugin-data/codexbar-agentview/kv.json"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if any(e.get("kind") == kind for e in json.loads(kv.read_text()).get("open") or []):
                    return
            except (OSError, ValueError):
                pass
            time.sleep(0.1)
        raise RuntimeError(f"no {kind} block opened")

    def film(self):
        """The story: the status line, the agent list (filter, peek, jump), usage, the native
        settings tab, then one closed session brought back from History."""
        # Framings, in window pixels: the status line under the panes' lower part, the dashboard
        # beside a tab's one pane (it opens as the right half), the tab bar over the question the
        # blocked session asks, and the settings tab's search and results.
        status = (760, 410, 740, 270)
        dash = (700, 40, 800, 430)
        asked = (0, 0, 900, 370)
        settings = (20, 40, 1260, 480)
        cog = r"cb-tab\.cb-settings$"
        exited = min(HISTORY, key=lambda s: s["ago"])["id"]

        self.tern("focus", str(self.tabs["atlas"]))
        time.sleep(2.5)
        self.filming = True
        shooter = threading.Thread(target=self.shoot)
        shooter.start()
        try:
            self.cut(scene="wide", wait=False)
            self.caption("CodexBar AgentView for Tern", "Usage and agents, always in view.")
            time.sleep(2.6)

            self.caption("Status line", "Who needs you, at a glance.")
            self.cut(status)
            time.sleep(2.0)

            self.caption("Agents", "One key lists every session.")
            self.cut()
            self.command("agents", "Ctrl", "Alt", "A")
            time.sleep(1.2)
            self.caption("Agents", "Find any session by typing.")
            self.cut(dash)
            self.type("webhook")
            time.sleep(1.2)
            self.caption("Agents", "Peek at where it left off.")
            self.key("Space", "Space")
            time.sleep(2.0)
            self.caption("Agents", "Jump straight to it.")
            self.key("Enter", "Enter")
            time.sleep(0.3)
            self.cut(asked)
            time.sleep(1.1)

            self.caption("Usage", "No surprise limits.")
            self.command("usage", "Ctrl", "Alt", "U")
            self.cut(dash)
            time.sleep(2.0)

            self.caption("Settings", "Every option, one search away.")
            x, y, w, h = self.find(cog)
            self.pointer = (x + w / 2 - 180, y + h / 2 + 170)
            self.glide(x + w / 2, y + h / 2, 600)
            time.sleep(0.25)
            self.click()
            # The keyboard takes over: the pointer goes, and Tern's real one leaves the window's
            # content, whose hover shows a drag handle.
            self.event("hide", surface="desktop")
            self.ctl("move", "4", str(SIZE[1] - 4))
            self.wait_open("settings")
            self.cut(settings)
            self.type("combined")
            time.sleep(0.7)
            self.key("ArrowDown", "↓")
            time.sleep(0.5)
            self.key("Space", "Space")
            time.sleep(1.2)
            self.key("Escape", "Esc")
            time.sleep(0.3)
            self.key("Escape", "Esc")
            time.sleep(0.6)

            self.caption("History", "Closed one by mistake? Bring it back.")
            self.command("agents", "Ctrl", "Alt", "A")
            time.sleep(0.6)
            self.cut(dash)
            self.key("ArrowRight", "→")
            time.sleep(1.2)
            self.key("Enter", "Enter")
            self.restored([exited])
            self.cut()
            time.sleep(0.6)

            self.caption("CodexBar AgentView for Tern", "Never leave the keyboard.")
            self.cut(scene="wide")
            time.sleep(2.6)
        finally:
            self.filming = False
            shooter.join()
        write_json(self.work / "timeline.json", {
            "transitionMs": TRANSITION_MS,
            "devices": [{"surface": "desktop", "kind": "display", "size": list(SIZE)}],
            "frames": {"desktop": self.frames},
            "events": self.events,
        })
        seconds = (self.now() - self.events[0]["t"]) / 1000
        print(f"{len(self.frames)} frames over {seconds:.1f} s -> {self.work / 'timeline.json'}")

    def run(self):
        self.prepare()
        self.alive = True
        try:
            self.start()
            self.build_workspace()
            threading.Thread(target=self.keep_fresh, daemon=True).start()
            self.film()
        finally:
            self.alive = False
            self.filming = False
            try:
                self.ctl("quit")
            except Exception:
                pass
            self.stop()

    def stop(self):
        """Stops the window, the daemon (which detaches), everything still running in its panes and
        anything else it started, such as a dialog."""
        for p in self.procs:
            if p.poll() is None:
                p.terminate()
        mine = str(self.work)
        sockets = {f"TERN_PANE_SOCKET={self.daemon_sock}", f"TERN_DAEMON_SOCKET={self.daemon_sock}"}
        # `uv run film/take_tern.py WORK` names WORK too.
        spare, pid = set(), os.getpid()
        while pid > 1 and pid not in spare:
            spare.add(pid)
            status = Path(f"/proc/{pid}/status").read_text()
            pid = int(re.search(r"^PPid:\s*(\d+)", status, re.M).group(1))
        for proc in Path("/proc").iterdir():
            if not proc.name.isdigit() or int(proc.name) in spare:
                continue
            try:
                args = (proc / "cmdline").read_bytes().decode(errors="replace").split("\0")
                env = (proc / "environ").read_bytes().decode(errors="replace").split("\0")
            except OSError:
                continue
            if any(a == mine or a.startswith(mine + "/") for a in args) or sockets.intersection(env):
                try:
                    os.kill(int(proc.name), signal.SIGTERM)
                except OSError:
                    pass


if __name__ == "__main__":
    if sys.argv[1:2] == ["--refresh"]:
        refresh(Path(sys.argv[2]))
    elif sys.argv[1:2] == ["--pane"]:
        pane(Path(sys.argv[2]), sys.argv[3], "--resumed" in sys.argv[4:])
    else:
        Take(Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/codexbar-agentview-film-tern").resolve()).run()

#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Films the waiting take for docs/waiting.mp4 and docs/waiting.gif: one omp session set aside with
a one-line `/wait`, the Waiting tab, Snooze session, the wait coming back and the session card. It
runs take_tern.py's world (scratch HOME, private Tern daemon, stand-in agents, fixture agents.json)
plus one real omp, resumed from a transcript this script writes, with the omp extension in a
scratch agent folder. Nobody sends that omp a prompt, so no model is ever asked anything. Time
waits fire after `--in`; the take fast-forwards one by rewriting its times under the CLI's lock,
which the caption calls "20 minutes later".

    uv run film/take_waiting.py [WORK]   (default /tmp/codexbar-agentview-film-waiting)
    uv run film/compose.py waiting [WORK]

Needs Tern, omp and a Wayland session (headless-gui keeps the window off your desktop).
"""

import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import take_tern as base  # noqa: E402

OMP = shutil.which("omp")
# The session set aside on camera: a real omp in its own pane, its conversation written here.
OMP_SESSION = {
    "id": "019a4b21-6c3e-7d14-9b2a-5e8f1c3a7d20", "folder": "harbor", "title": "Ship deploy 1824",
    "provider": "omp", "model": "claude-opus-5-5", "state": "idle", "ago": 4,
    "turns": [("user", "text", "Deploy build 1824 to staging, then we smoke-test it."),
              ("assistant", "text", "The staging deploy of build 1824 is running. It takes about 20 minutes; "
                                    "the smoke tests can start once it's out.")],
}
SET_ASIDE = "/wait 20m smoke-test staging once deploy 1824 is out"
# The take_tern sessions this take shows, and the waits already set on them.
SHOWN = ["Review query timeout", "Polish settings layout", "Review cache invalidation", "Check export permissions",
         "Improve log grouping"]
WAITS = [
    ("Check export permissions", ["--kind", "command", "--argv", "test -f export.done", "--interval", "10m",
                                  "--label", "Export job finished", "--note", "Re-run the audit once the export is done"]),
    ("Review cache invalidation", ["--kind", "repeat", "--every", "1d", "--for", "1w", "--label", "Flaky e2e watch",
                                   "--note", "Run the e2e suite daily for a week",
                                   "--prompt", "Run the e2e suite three times and list the flaky tests"]),
    ("Polish settings layout", ["--kind", "something", "--what", "Mia's design review",
                                "--note", "Fix what Mia flags in the settings review"]),
    ("Improve log grouping", ["--kind", "later", "--note", "Rework the grouping UI on a quiet afternoon"]),
]
ISO = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d")


class WaitingTake(base.Take):
    def prepare(self):
        super().prepare()
        if not OMP:
            sys.exit("omp isn't on PATH")
        self.omp_home = self.home / ".omp/agent"
        (self.omp_home / "extensions").mkdir(parents=True)
        (self.omp_home / "extensions/codexbar-waitingfor.ts").symlink_to(base.REPO / "omp-extension/waitingfor.ts")
        # Past omp's first-run setup, which would ask for a provider login.
        (self.omp_home / "config.yml").write_text("setupVersion: 3\n")
        self.omp_file = self.write_transcript(OMP_SESSION)

    def write_transcript(self, s: dict) -> Path:
        """An omp session file holding `turns`, as omp writes one, so `omp --resume` shows them."""
        cwd = self.home / "code" / s["folder"]
        start = datetime.now(timezone.utc) - timedelta(minutes=s["ago"] + 2)
        stamp = lambda d: d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"  # noqa: E731
        folder = self.omp_home / "sessions" / ("-" + str(cwd.relative_to(self.home)).replace("/", "-"))
        folder.mkdir(parents=True)
        lines = [
            {"type": "title", "v": 1, "title": s["title"], "source": "auto", "updatedAt": stamp(start), "pad": ""},
            {"type": "session", "version": 3, "id": s["id"], "timestamp": stamp(start), "cwd": str(cwd)},
            {"type": "model_change", "id": "m0", "parentId": None, "timestamp": stamp(start),
             "model": f"anthropic/{s['model']}", "resolvedModelIsFallback": False},
        ]
        parent = "m0"
        for i, (role, _kind, text) in enumerate(s["turns"]):
            at = start + timedelta(seconds=40 * (i + 1))
            message = {"role": role, "content": [{"type": "text", "text": text}], "timestamp": int(at.timestamp() * 1000)}
            if role == "user":
                message["attribution"] = "user"
            else:
                message.update(api="anthropic-messages", provider="anthropic", model=s["model"], stopReason="stop",
                               usage={"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 0})
            lines.append({"type": "message", "id": f"t{i}", "parentId": parent, "timestamp": stamp(at), "message": message})
            parent = f"t{i}"
        path = folder / f"{start.strftime('%Y-%m-%dT%H-%M-%S')}-{start.microsecond // 1000:03d}Z_{s['id']}.jsonl"
        compact = lambda line: json.dumps(line, separators=(",", ":"), ensure_ascii=False)  # noqa: E731
        # omp pads the title line to 256 bytes so it can rewrite it in place; a header without it is malformed.
        title = compact(lines[0])
        lines[0]["pad"] = " " * (255 - len(title.encode()))
        path.write_text("".join(compact(line) + "\n" for line in lines))
        return path

    def build_workspace(self):
        """harbor: the omp session beside a stand-in; atlas and beacon: stand-ins. Waits on the
        stand-ins come from WAITS, through the CLI."""
        shown = [s for s in base.SESSIONS if s["title"] in SHOWN]
        first = [b["id"] for b in self.blocks()]
        cwd = str(self.home / "code" / "harbor")
        # The omp extension calls the CLI with this environment: the film's state, never yours. The
        # placeholder key lets omp restore the session's model; nobody prompts, so it's never used.
        omp = ["env", f"HOME={self.home}", f"XDG_STATE_HOME={self.state}", f"PATH={self.env['PATH']}",
               "ANTHROPIC_API_KEY=film-placeholder", OMP, "--resume", str(self.omp_file)]
        made = json.loads(self.tern("new", "tab", "--json", "--cwd", cwd, "--", *omp))
        self.omp_block = made["block"]
        self.tern("rename", str(self.omp_block), "harbor")
        tabs = {"harbor": self.omp_block}
        for s in shown:
            launch = ["--cwd", str(self.home / "code" / s["folder"]), "--", *self.pane_command(s["id"])]
            if s["folder"] in tabs:
                self.tern("split", str(tabs[s["folder"]]), "right", *launch)
            else:
                made = json.loads(self.tern("new", "tab", "--json", *launch))
                tabs[s["folder"]] = made["block"]
                self.tern("rename", str(made["block"]), s["folder"])
        for block in first:
            self.tern("close", str(block))
        self.tern("rename", self.session_name(), "Demo workspace")
        self.tabs = tabs
        time.sleep(1.5)
        for _ in range(80):
            self.panes = self.pane_processes()
            if OMP_SESSION["id"] in self.panes:
                break
            time.sleep(0.25)
        else:
            raise RuntimeError("omp didn't start in its pane")
        with self.world_lock:
            self.world = {"stateDir": str(self.state_dir),
                          "agents": [self.live_record(s) for s in [OMP_SESSION, *shown]], "history": []}
            self.save_world()
        cli = [base.PYTHON, str(base.SCRIPTS / "codexbar_waiting.py")]
        ids = {s["title"]: s for s in shown}
        for title, args in WAITS:
            s = ids[title]
            subprocess.run([*cli, "add", "--session", s["id"], "--cwd", str(self.home / "code" / s["folder"]), *args],
                           cwd=self.home / "code" / s["folder"], env=self.env, capture_output=True, text=True, check=True)
        subprocess.run([*cli, "tick"], env=self.env, capture_output=True, text=True, check=True)
        # omp loads its extension and draws its conversation.
        time.sleep(8)

    def pane_processes(self) -> dict[str, tuple[int, int]]:
        """The stand-ins, as take_tern finds them, and the omp running OMP_SESSION's file."""
        found = super().pane_processes()
        for proc in Path("/proc").iterdir():
            if not proc.name.isdigit():
                continue
            try:
                argv = [a.decode() for a in (proc / "cmdline").read_bytes().split(b"\0") if a]
                environ = (proc / "environ").read_bytes().split(b"\0")
            except OSError:
                continue
            if str(self.omp_file) not in argv or "--resume" not in argv or argv[0] == "env":
                continue
            env = dict(e.decode().split("=", 1) for e in environ if b"=" in e)
            if env.get("TERN_PANE_SOCKET") == str(self.daemon_sock) and env.get("TERN_PANE", "").isdigit():
                found[OMP_SESSION["id"]] = (int(proc.name), int(env["TERN_PANE"]))
        return found

    def fast_forward(self, session: str, minutes: int):
        """Moves the session's record `minutes` into the past, so its time wait is due, under the
        CLI's lock, then ticks as Tern would."""
        def shift(value):
            if isinstance(value, dict):
                return {k: shift(v) for k, v in value.items()}
            if isinstance(value, list):
                return [shift(v) for v in value]
            if isinstance(value, str) and ISO.match(value):
                return (datetime.fromisoformat(value) - timedelta(minutes=minutes)).isoformat(timespec="seconds")
            return value

        with open(self.state_dir / "waiting.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            path = self.state_dir / "waiting.json"
            state = json.loads(path.read_text())
            state["sessions"][session] = shift(state["sessions"][session])
            base.write_json(path, state)
        subprocess.run([base.PYTHON, str(base.SCRIPTS / "codexbar_waiting.py"), "tick"],
                       env=self.env, capture_output=True, text=True, check=True)

    def film(self):
        """The story: set the omp session aside in one line, the Waiting tab and its groups, a peek,
        Snooze session, the wait coming back 20 minutes later, and the session with its card."""
        dash = (700, 40, 800, 430)
        omp_pane = (0, 40, 760, 640)
        self.tern("focus", str(self.omp_block))
        time.sleep(2.0)
        self.filming = True
        shooter = threading.Thread(target=self.shoot)
        shooter.start()
        try:
            self.cut(scene="wide", wait=False)
            self.caption("Set sessions aside", "Never lose why you stopped.")
            time.sleep(2.6)

            self.caption("Set it aside", "One line: what it waits for, and why.")
            self.cut(omp_pane)
            self.type(SET_ASIDE, pause=0.06)
            time.sleep(0.4)
            self.key("Enter", "Enter")
            time.sleep(2.2)

            self.caption("Waiting", "Code watches the wait. No tokens spent.")
            self.command("waiting", "Ctrl", "Alt", "W")
            self.cut(dash)
            time.sleep(2.2)

            self.caption("Waiting", "Grouped by who has the next step.")
            self.key("ArrowDown", "↓")
            time.sleep(0.4)
            self.key("Space", "Space")
            time.sleep(2.4)
            self.key("Space", "Space")
            time.sleep(0.3)

            self.caption("Ask the agent", "\"Check it daily for a week\" works too.")
            self.key("ArrowDown", "↓")
            time.sleep(0.4)
            self.key("Space", "Space")
            time.sleep(2.2)
            self.key("Space", "Space")
            time.sleep(0.3)

            self.caption("Snooze", "Out of sight until you want it back.")
            self.key("F4", "F4")
            time.sleep(2.2)
            self.key("Escape", "Esc")
            time.sleep(0.6)

            self.caption("20 minutes later", "It comes back when it's your turn.")
            self.fast_forward(OMP_SESSION["id"], 20)
            time.sleep(1.2)
            for _ in range(6):
                self.key("ArrowUp", "↑")
                time.sleep(0.15)
            time.sleep(1.6)

            self.caption("Coming back", "Why you left, and what happened since.")
            self.key("Enter", "Enter")
            time.sleep(0.4)
            self.cut()
            time.sleep(3.4)

            self.caption("Set sessions aside", "Works in omp. Best in Tern.")
            self.cut(scene="wide")
            time.sleep(2.6)
        finally:
            self.filming = False
            shooter.join()
        base.write_json(self.work / "timeline.json", {
            "transitionMs": base.TRANSITION_MS,
            "devices": [{"surface": "desktop", "kind": "display", "size": list(base.SIZE)}],
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


if __name__ == "__main__":
    WaitingTake(Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/codexbar-agentview-film-waiting").resolve()).run()

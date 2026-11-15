#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Films the waiting take for docs/waiting.mp4 and docs/waiting.gif: four omp sessions set aside
with one-line `/wait`s (babysit a PR, come back in two days, wait on a person, repeat daily), the
Waiting tab, Snooze session, the two-day wait coming back and the session card. It runs
take_tern.py's world (scratch HOME, private Tern daemon, stand-in agents, fixture agents.json) plus
four real omps, each resumed from a transcript this script writes, with the omp extension in a
scratch agent folder. Nobody sends those omps a prompt, so no model is ever asked anything. A fake
`gh` first on PATH serves an open PR without a review, so the babysat PR waits without the network.
The take fast-forwards the two-day wait by rewriting its times under the CLI's lock.

    uv run film/take_waiting.py [WORK]   (default /tmp/codexbar-agentview-film-waiting)
    uv run film/compose.py waiting [WORK]

Needs Tern, omp and a Wayland session (headless-gui keeps the window off your desktop).
"""

import fcntl
import json
import re
import shutil
import subprocess
import sys
import textwrap
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import take_tern as base  # noqa: E402

OMP = shutil.which("omp")


def omp_session(sid: str, folder: str, title: str, model: str, ask: str, answer: str) -> dict:
    return {"id": sid, "folder": folder, "title": title, "provider": "omp", "model": model, "state": "idle",
            "ago": 5, "turns": [("user", "text", ask), ("assistant", "text", answer)]}


# Each montage step: a real omp session in a tab of its own, beside take_tern stand-ins that stay
# when it parks, the line typed into it, and the caption over it.
MONTAGE = [
    {"session": omp_session("019a4b21-6c3e-7d14-9b2a-5e8f1c3a7d20", "harbor", "Fix upload retry backoff",
                            "claude-opus-5-5", "Fix the upload retry backoff and open a pull request.",
                            "Opened PR #42 with the backoff fix. CI is running, and the review bot "
                            "usually comments within the hour."),
     "standins": ["Review query timeout"],
     "line": "/wait gh babysit 42 ship the backoff fix",
     "caption": ("Babysit a pull request", "Review, fixes, merge. Code watches it.")},
    {"session": omp_session("019a4b22-1d5f-7a20-8c3b-6f9e2d4b8e21", "atlas", "Migrate billing tables",
                            "claude-sonnet-5", "Run the billing table migration.",
                            "The provider's API quota ran out halfway, so nothing was written. "
                            "It resets in two days."),
     "standins": ["Review cache invalidation"],
     "line": "/wait 2d retry the migration once the quota resets",
     "caption": ("Come back later", "In two days, at 18:00, or on a date.")},
    {"session": omp_session("019a4b23-8e2a-7b31-9d4c-7a0f3e5c9f22", "beacon", "Restyle the settings page",
                            "claude-opus-5-5", "Restyle the settings page from Mia's mockups.",
                            "The layout follows the mockups. Mia wants to review it before it ships."),
     "standins": ["Improve log grouping"],
     "line": "/wait for Mia's design review",
     "caption": ("Wait on a person", "In your own words.")},
    {"session": omp_session("019a4b24-3f6b-7c42-ae5d-8b1a4f6d0a23", "orbit", "Hunt flaky e2e tests",
                            "claude-sonnet-5", "The e2e suite fails at random. Find the flaky tests.",
                            "Three runs passed. The flakes are rare, so catching them takes a week of runs."),
     "standins": ["Check export permissions"],
     "line": "/wait every 1d for 1w catch the flaky tests -- Run the e2e suite and list what failed",
     "caption": ("Repeat on a schedule", "A prompt every day, for a week.")},
]
COMES_BACK = MONTAGE[1]["session"]
# Waits already set on stand-ins, so the Waiting tab shows the other groups too.
WAITS = [
    ("Check export permissions", ["--kind", "command", "--argv", "test -f export.done", "--interval", "10m",
                                  "--label", "Export job finished", "--note", "Re-run the audit once the export is done"]),
    ("Improve log grouping", ["--kind", "later", "--note", "Rework the grouping UI on a quiet afternoon"]),
]
ISO = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d")
# acme/harbor#42 as the PR poller reads it: open, CI running, no review yet.
PR = {"data": {"viewer": {"login": "me"}, "repository": {"pullRequest": {
    "state": "OPEN", "headRefOid": "a" * 40, "baseRefName": "main", "mergeStateStatus": "BLOCKED",
    "mergedAt": None, "closedAt": None, "mergeCommit": None, "author": {"login": "me"},
    "baseRef": {"target": {"oid": "e" * 40}}, "comments": {"nodes": []}, "reviews": {"nodes": []},
    "reviewThreads": {"pageInfo": {"hasPreviousPage": False, "startCursor": None}, "nodes": []},
    "commits": {"nodes": [{"commit": {"statusCheckRollup": {"contexts": {
        "pageInfo": {"hasNextPage": False, "endCursor": None},
        "nodes": [{"name": "test", "status": "IN_PROGRESS", "conclusion": None, "isRequired": True}]}}}}]},
}}}}
# The same PR as `gh pr view --json` answers it for the session card.
PR_VIEW = {"title": "Fix upload retry backoff", "state": "OPEN", "isDraft": False, "reviewDecision": "REVIEW_REQUIRED",
           "statusCheckRollup": [{"name": "test", "status": "IN_PROGRESS", "conclusion": None}]}


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
        self.omp_files = {step["session"]["id"]: self.write_transcript(step["session"]) for step in MONTAGE}
        self.write_gh()

    def write_gh(self):
        """A `gh` that answers as acme/harbor with PR #42 open and unreviewed, and fails the rest."""
        pr = self.work / "pr.json"
        pr.write_text(json.dumps(PR))
        gh = self.bin / "gh"
        gh.write_text(textwrap.dedent(f"""\
            #!{base.PYTHON}
            # film/take_waiting.py: GitHub as the film shows it, without the network.
            import sys
            args = sys.argv[1:]
            with open({str(self.work / "gh.log")!r}, "a") as log:
                log.write(" ".join(args) + "\\n")
            if args[:2] == ["repo", "view"] and (len(args) == 2 or args[2].startswith("-")):
                print("acme/harbor"); sys.exit(0)
            if args[:3] == ["pr", "view", "42"]:
                print({json.dumps(PR_VIEW)!r}); sys.exit(0)
            if args[:1] == ["api"]:
                target = next((a for a in args[1:] if a.startswith(("repos/", "query=", "rate_limit", "user"))), "")
                if target == "rate_limit":
                    print(5000); sys.exit(0)
                if target == "user":
                    print("me"); sys.exit(0)
                if target.startswith("query="):
                    print(open({str(pr)!r}).read()); sys.exit(0)
                if "/rules/branches/" in target or "/actions/runs" in target:
                    sys.exit(0)
            sys.stderr.write("gh: Not Found (HTTP 404)\\n")
            sys.exit(1)
            """))
        gh.chmod(0o755)

    def write_transcript(self, s: dict) -> Path:
        """An omp session file holding `turns`, as omp writes one, so `omp --resume` shows them."""
        cwd = self.home / "code" / s["folder"]
        cwd.mkdir(parents=True, exist_ok=True)
        start = datetime.now(timezone.utc) - timedelta(minutes=s["ago"] + 2)
        stamp = lambda d: d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"  # noqa: E731
        folder = self.omp_home / "sessions" / ("-" + str(cwd.relative_to(self.home)).replace("/", "-"))
        folder.mkdir(parents=True, exist_ok=True)
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
        """One tab per montage step: its omp on the left, its stand-ins split to the right."""
        by_title = {s["title"]: s for s in base.SESSIONS}
        first = [b["id"] for b in self.blocks()]
        self.omp_blocks = []
        for step in MONTAGE:
            s = step["session"]
            # The omp extension calls the CLI with this environment: the film's state, never yours.
            # The placeholder key lets omp restore the session's model; nobody prompts, so it's never used.
            omp = ["env", f"HOME={self.home}", f"XDG_STATE_HOME={self.state}", f"PATH={self.env['PATH']}",
                   "ANTHROPIC_API_KEY=film-placeholder", OMP, "--resume", str(self.omp_files[s["id"]])]
            made = json.loads(self.tern("new", "tab", "--json", "--cwd", str(self.home / "code" / s["folder"]),
                                        "--", *omp))
            self.omp_blocks.append(made["block"])
            self.tern("rename", str(made["block"]), s["folder"])
            for title in step["standins"]:
                stand = by_title[title]
                self.tern("split", str(made["block"]), "right", "--cwd", str(self.home / "code" / stand["folder"]),
                          "--", *self.pane_command(stand["id"]))
        for block in first:
            self.tern("close", str(block))
        self.tern("rename", self.session_name(), "Demo workspace")
        time.sleep(1.5)
        for _ in range(120):
            self.panes = self.pane_processes()
            if all(sid in self.panes for sid in self.omp_files):
                break
            time.sleep(0.25)
        else:
            raise RuntimeError("an omp didn't start in its pane")
        shown = [by_title[t] for step in MONTAGE for t in step["standins"]]
        with self.world_lock:
            self.world = {"stateDir": str(self.state_dir), "history": [],
                          "agents": [self.live_record(s) for s in [*(m["session"] for m in MONTAGE), *shown]]}
            self.save_world()
        cli = [base.PYTHON, str(base.SCRIPTS / "codexbar_waiting.py")]
        for title, args in WAITS:
            s = by_title[title]
            subprocess.run([*cli, "add", "--session", s["id"], "--cwd", str(self.home / "code" / s["folder"]), *args],
                           cwd=self.home / "code" / s["folder"], env=self.env, capture_output=True, text=True, check=True)
        subprocess.run([*cli, "tick"], env=self.env, capture_output=True, text=True, check=True)
        # The omps load their extension and draw their conversations.
        time.sleep(8)

    def pane_processes(self) -> dict[str, tuple[int, int]]:
        """The stand-ins, as take_tern finds them, and each omp running a montage session's file."""
        found = super().pane_processes()
        files = {str(path): sid for sid, path in self.omp_files.items()}
        for proc in Path("/proc").iterdir():
            if not proc.name.isdigit():
                continue
            try:
                argv = [a.decode() for a in (proc / "cmdline").read_bytes().split(b"\0") if a]
                environ = (proc / "environ").read_bytes().split(b"\0")
            except OSError:
                continue
            sid = next((files[a] for a in argv if a in files), None)
            if sid is None or "--resume" not in argv or argv[0] == "env":
                continue
            env = dict(e.decode().split("=", 1) for e in environ if b"=" in e)
            if env.get("TERN_PANE_SOCKET") == str(self.daemon_sock) and env.get("TERN_PANE", "").isdigit():
                found[sid] = (int(proc.name), int(env["TERN_PANE"]))
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
        """The story: four sessions set aside in one line each, the Waiting tab and its groups, a
        peek, Snooze session, the two-day wait coming back, and that session with its card."""
        dash = (700, 40, 800, 430)
        omp_pane = (0, 40, 760, 640)
        self.tern("focus", str(self.omp_blocks[0]))
        time.sleep(2.0)
        self.filming = True
        shooter = threading.Thread(target=self.shoot)
        shooter.start()
        try:
            self.cut(scene="wide", wait=False)
            self.caption("Set sessions aside", "Never lose why you stopped.")
            time.sleep(2.6)

            for i, step in enumerate(MONTAGE):
                if i:
                    self.tern("focus", str(self.omp_blocks[i]))
                    time.sleep(0.8)
                self.caption(*step["caption"])
                self.cut(omp_pane)
                time.sleep(0.6)
                self.type(step["line"], pause=0.045)
                time.sleep(0.5)
                self.key("Enter", "Enter")
                time.sleep(1.8)

            self.caption("Waiting", "Code watches every wait. No tokens spent.")
            self.command("waiting", "Ctrl", "Alt", "W")
            self.cut(dash)
            time.sleep(2.4)

            self.caption("Waiting", "Grouped by who has the next step.")
            self.key("ArrowDown", "↓")
            time.sleep(0.4)
            self.key("Space", "Space")
            time.sleep(2.4)
            self.caption("Ask the agent", "\"Babysit the PR you just opened\" works too.")
            time.sleep(2.4)
            self.key("Space", "Space")
            time.sleep(0.3)

            self.caption("Snooze", "Out of sight until you want it back.")
            self.key("F4", "F4")
            time.sleep(2.2)
            self.key("Escape", "Esc")
            time.sleep(0.6)

            self.caption("Two days later", "It comes back when it's your turn.")
            self.fast_forward(COMES_BACK["id"], 2 * 24 * 60)
            time.sleep(1.2)
            for _ in range(8):
                self.key("ArrowUp", "↑")
                time.sleep(0.12)
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

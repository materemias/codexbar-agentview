#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Films the waiting take for docs/waiting.mp4 and docs/waiting.gif, from the waits people set most:
the agent babysits a pull request, checks a migration once it finishes, and repeats a check daily
after a rollout; you put a task off for when you have time. Then the Waiting tab, Snooze, a review
landing on the PR (the agent fixes it while parked, and the PR comes back ready to merge), and the
migration finishing, which reopens its closed session so the agent can report.

It runs take_tern.py's world (scratch HOME, private Tern daemon, stand-in agents, fixture
agents.json) plus four real omps with the omp extension, each resumed from a transcript this script
writes. Their model is a scripted stand-in served from this process: it answers each line the take
types, calls the `waitingfor` tool as an agent would, and answers the prompts waits send. A fake
`gh` serves the pull request and a fake `billing-migrate` the migration, so nothing reaches the
network. Time skips ("an hour later") move the sessions' records into the past under the CLI's lock.

    uv run film/take_waiting.py [WORK]   (default /tmp/codexbar-agentview-film-waiting)
    uv run film/compose.py waiting [WORK]

Needs Tern, omp and a Wayland session (headless-gui keeps the window off your desktop).
"""

import fcntl
import http.server
import json
import re
import shlex
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
CLI = [base.PYTHON, str(base.SCRIPTS / "codexbar_waiting.py")]
ISO = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d")


def omp_session(sid: str, folder: str, title: str, model: str, ask: str, answer: str) -> dict:
    return {"id": sid, "folder": folder, "title": title, "provider": "omp", "model": model, "state": "idle",
            "ago": 5, "turns": [("user", "text", ask), ("assistant", "text", answer)]}


PR_SESSION = omp_session(
    "019a4b21-6c3e-7d14-9b2a-5e8f1c3a7d20", "harbor", "Fix upload retry backoff", "claude-opus-5-5",
    "Fix the upload retry backoff and open a pull request.",
    "Opened PR #42 with the backoff fix. CI is running, and coderabbit usually reviews within the hour.")
JOB_SESSION = omp_session(
    "019a4b22-1d5f-7a20-8c3b-6f9e2d4b8e21", "harbor", "Migrate billing tables", "claude-sonnet-5",
    "Start the billing table migration on staging.",
    "Started `billing-migrate` on staging. It copies 14 tables and takes about 40 minutes.")
ROLLOUT_SESSION = omp_session(
    "019a4b23-8e2a-7b31-9d4c-7a0f3e5c9f22", "beacon", "Roll out 2.4", "claude-opus-5-5",
    "Roll 2.4 out to the whole fleet.",
    "2.4 is on all 40 hosts. Error rate and latency look normal so far.")
LATER_SESSION = omp_session(
    "019a4b24-3f6b-7c42-ae5d-8b1a4f6d0a23", "beacon", "Rework log grouping", "claude-sonnet-5",
    "How much work is it to group the logs by request instead of by host?",
    "About a day: the grouping, the filters and the tests all change. Worth doing on a quiet afternoon.")
SESSIONS = [PR_SESSION, JOB_SESSION, ROLLOUT_SESSION, LATER_SESSION]

# The strip above the focused omp's composer, and the session card floating over its pane.
STRIP = r"sf-dock[^>]*>div\.sf\.sf-col\.gap-none>div\.sf\.sf-row\.sf-act\.gap-sm\.al-center$"
# The strip's close verdict, the badge at its right end.
BADGE = STRIP[:-1] + r">.*sf-badge[^>]*$"
CARD = r"section\.tn-pane\.tn-float-card\.pip[.a-z-]*$"

# The lines typed into each session, in its own words.
PR_LINE = "babysit it until it's merged"
JOB_LINE = "it takes about 40 minutes. check it when it's done and report back, I'm closing this"
ROLLOUT_LINE = "check the fleet every day for a week"
LATER_LINE = "/wait later rework the grouping when I have a quiet afternoon"

HEAD, FIXED = "3f2a9c1" + "0" * 33, "b4e91c2" + "0" * 33

# The scripted model: the line it answers (a substring of the latest user text), then its turn: a
# tool call and the words after the call's result, or only words.
SCRIPT = [
    (PR_LINE, {"action": "babysit_pr", "number": 42, "note": "Ship the upload backoff fix", "park_after": True},
     "Babysitting PR #42. When a review lands I'll fix what's real and push. Once it's green and reviewed, "
     "it's back on you to merge. Parking this session."),
    ("A review landed on", None,
     "coderabbit flagged two things on 3f2a9c1:\n\n"
     "1. The jitter could push the delay below zero. Real: it's clamped at zero now.\n"
     "2. Nothing tested the retry cap. Added a test.\n\n"
     "Pushed b4e91c2 and resolved both threads."),
    (JOB_LINE, {"action": "add", "kind": "command", "argv": ["billing-migrate", "status"],
                "predicate": "stdout_regex:^finished", "interval": "5m", "label": "Billing migration finished",
                "note": "Report on the billing migration once it finishes",
                "prompt": "The billing migration finished. Read `billing-migrate log`, compare the row counts "
                          "with the source, and report."},
     "Set. Code runs `billing-migrate status` every 5 minutes, no tokens. When it says finished, Tern "
     "reopens this session and I'll report. Safe to close."),
    ("The billing migration finished", None,
     "The migration finished in 41 minutes: 14 tables, 2.3M rows, no errors. Row counts match the source "
     "on every table. Nothing left to do."),
    (ROLLOUT_LINE, {"action": "add", "kind": "repeat", "every": "1d", "for": "1w", "label": "Fleet check after 2.4",
                    "note": "Watch the fleet for a week after the 2.4 rollout",
                    "prompt": "Daily check after the 2.4 rollout: compare the fleet's error rate and p95 latency "
                              "with last week, and report anything off.", "park_after": True},
     "Done: I'll check the fleet once a day for a week, then stop. Parking this session."),
]

# acme/harbor#42 as the PR poller reads it, by stage: open with CI running; reviewed with two open
# threads; fixed on a new head, green, reviewed again, threads resolved; merged.
STAGES = ("open", "reviewed", "fixed", "merged")


def pr_answer(stage: str) -> dict:
    head = FIXED if stage in ("fixed", "merged") else HEAD
    reviewed = stage != "open"
    resolved = stage in ("fixed", "merged")
    threads = [{"id": f"t{i}", "isResolved": resolved, "path": "src/upload/retry.ts", "line": line,
                "comments": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [
                    {"id": f"tc{i}", "body": body, "author": {"login": "coderabbitai", "__typename": "Bot"}}]}}
               for i, (line, body) in enumerate([(18, "The jitter can make the delay negative."),
                                                 (31, "Nothing tests the retry cap.")])] if reviewed else []
    reviews = []
    if reviewed:
        reviews.append({"id": "r1", "state": "COMMENTED", "body": "2 actionable comments.",
                        "commit": {"oid": HEAD}, "author": {"login": "coderabbitai", "__typename": "Bot"}})
    if resolved:
        reviews.append({"id": "r2", "state": "APPROVED", "body": "Both comments addressed.",
                        "commit": {"oid": FIXED}, "author": {"login": "coderabbitai", "__typename": "Bot"}})
    done = stage != "open"
    return {"data": {"viewer": {"login": "me"}, "repository": {"pullRequest": {
        "state": "MERGED" if stage == "merged" else "OPEN", "headRefOid": head, "baseRefName": "main",
        "mergeStateStatus": "CLEAN" if resolved else "BLOCKED",
        "mergedAt": "2026-11-15T12:00:00Z" if stage == "merged" else None, "closedAt": None,
        "mergeCommit": {"oid": "9" * 40} if stage == "merged" else None, "author": {"login": "me"},
        "baseRef": {"target": {"oid": "e" * 40}}, "comments": {"nodes": []}, "reviews": {"nodes": reviews},
        "reviewThreads": {"pageInfo": {"hasPreviousPage": False, "startCursor": None}, "nodes": threads},
        "commits": {"nodes": [{"commit": {"statusCheckRollup": {"contexts": {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": [{"name": "test", "status": "COMPLETED" if done else "IN_PROGRESS",
                       "conclusion": "SUCCESS" if done else None, "isRequired": True,
                       "checkSuite": {"app": {"slug": "github-actions"}}}]}}}}]},
    }}}}


class Model(http.server.BaseHTTPRequestHandler):
    """The scripted stand-in for Anthropic's Messages API that the film's omps talk to."""
    take: "WaitingTake"

    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        if self.path.endswith("count_tokens"):
            return self.reply_json({"input_tokens": 100})
        blocks, stop = self.take.turn(body.get("messages") or [])
        if not body.get("stream"):
            return self.reply_json({"id": "msg_film", "type": "message", "role": "assistant", "model": body.get("model"),
                                    "content": blocks, "stop_reason": stop, "usage": {"input_tokens": 10, "output_tokens": 5}})
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()

        def event(name, data):
            self.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())
            self.wfile.flush()

        event("message_start", {"type": "message_start", "message": {
            "id": "msg_film", "type": "message", "role": "assistant", "model": body.get("model"), "content": [],
            "stop_reason": None, "usage": {"input_tokens": 10, "output_tokens": 1}}})
        time.sleep(0.8)
        for i, block in enumerate(blocks):
            if block["type"] == "text":
                event("content_block_start", {"type": "content_block_start", "index": i,
                                              "content_block": {"type": "text", "text": ""}})
                # A few words at a time, as a model writes.
                for word in re.findall(r"\S+\s*", block["text"]):
                    event("content_block_delta", {"type": "content_block_delta", "index": i,
                                                  "delta": {"type": "text_delta", "text": word}})
                    time.sleep(0.035)
            else:
                if i:
                    time.sleep(2.5)  # The words before a call stay readable a moment.
                event("content_block_start", {"type": "content_block_start", "index": i, "content_block": {
                    "type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}})
                event("content_block_delta", {"type": "content_block_delta", "index": i, "delta": {
                    "type": "input_json_delta", "partial_json": json.dumps(block["input"])}})
            event("content_block_stop", {"type": "content_block_stop", "index": i})
        event("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                                "usage": {"output_tokens": 5}})
        event("message_stop", {"type": "message_stop"})

    def reply_json(self, data):
        out = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def user_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    return "\n".join(c.get("text", "") for c in content or [] if c.get("type") == "text")


class WaitingTake(base.Take):
    # The explainer's stage is about 4:3. A window near its size is drawn almost 1:1, and the
    # larger font keeps the conversation readable in the film.
    size = (900, 690)
    font = 16

    def prepare(self):
        super().prepare()
        if not OMP:
            sys.exit("omp isn't on PATH")
        self.stage = "open"
        self.model_log = open(self.work / "model.log", "a")
        Model.take = self
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Model)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

        self.omp_home = self.home / ".omp/agent"
        (self.omp_home / "extensions").mkdir(parents=True)
        (self.omp_home / "extensions/codexbar-waitingfor.ts").symlink_to(base.REPO / "omp-extension/waitingfor.ts")
        # Past omp's first-run setup; tools run without asking, as most people set them.
        (self.omp_home / "config.yml").write_text("setupVersion: 3\ntools:\n  approvalMode: yolo\n")
        # Every Anthropic model goes to the scripted stand-in.
        (self.omp_home / "models.yml").write_text(
            f"providers:\n  anthropic:\n    baseUrl: http://127.0.0.1:{self.server.server_address[1]}\n    apiKey: film\n")
        for s in SESSIONS:
            self.write_transcript(s)
        self.write_tools()

    def write_tools(self):
        """`omp-film`, a real omp in the film's HOME; the restore shell's `omp`, which runs it for
        the film's omp sessions; and the fake `gh` and `billing-migrate`."""
        omp = self.bin / "omp-film"
        omp.write_text(textwrap.dedent(f"""\
            #!/bin/sh
            exec env HOME={shlex.quote(str(self.home))} XDG_STATE_HOME={shlex.quote(str(self.state))} \\
              PATH={shlex.quote(self.env["PATH"])} {shlex.quote(OMP)} "$@"
            """))
        ids = "|".join(s["id"] for s in SESSIONS)
        restore = self.work / "agents/omp"
        restore.write_text(textwrap.dedent(f"""\
            #!/bin/sh
            # film/take_waiting.py: the film's omp sessions reopen as real omps, the rest as stand-ins.
            for a in "$@"; do id="$a"; done
            case "$id" in {ids}) exec {shlex.quote(str(omp))} --resume "$id" ;; esac
            exec {base.PYTHON} {shlex.quote(str(base.TAKE))} --pane {shlex.quote(str(self.work))} "$id" --resumed
            """))
        self.stage_file = self.work / "pr-stage"
        self.stage_file.write_text("open")
        for stage in STAGES:
            (self.work / f"pr-{stage}.json").write_text(json.dumps(pr_answer(stage)))
        gh = self.bin / "gh"
        gh.write_text(textwrap.dedent(f"""\
            #!{base.PYTHON}
            # film/take_waiting.py: acme/harbor#42 as the film shows it, without the network.
            import json, sys
            args = sys.argv[1:]
            with open({str(self.work / "gh.log")!r}, "a") as log:
                log.write(" ".join(args)[:200] + "\\n")
            stage = open({str(self.stage_file)!r}).read().strip()
            answer = json.load(open({str(self.work)!r} + "/pr-" + stage + ".json"))
            if args[:2] == ["repo", "view"]:
                print("acme/harbor"); sys.exit(0)
            if args[:3] == ["pr", "view", "42"]:
                pr = answer["data"]["repository"]["pullRequest"]
                print(json.dumps({{"title": "Fix upload retry backoff", "state": pr["state"], "isDraft": False,
                                   "reviewDecision": "APPROVED" if stage in ("fixed", "merged") else "REVIEW_REQUIRED",
                                   "statusCheckRollup": pr["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]["nodes"]}}))
                sys.exit(0)
            if args[:1] == ["api"]:
                target = next((a for a in args[1:] if a.startswith(("repos/", "query=", "rate_limit", "user"))), "")
                if target == "rate_limit":
                    print(5000); sys.exit(0)
                if target == "user":
                    print("me"); sys.exit(0)
                if target.startswith("query="):
                    print(json.dumps(answer)); sys.exit(0)
                if "/rules/branches/" in target or "/actions/runs" in target:
                    sys.exit(0)
            sys.stderr.write("gh: Not Found (HTTP 404)\\n")
            sys.exit(1)
            """))
        self.migration_done = self.work / "migration-done"
        migrate = self.bin / "billing-migrate"
        migrate.write_text(textwrap.dedent(f"""\
            #!/bin/sh
            # film/take_waiting.py: the staging migration, finished once the take says so.
            [ -e {shlex.quote(str(self.migration_done))} ] && echo "finished: 14 tables, 2,301,442 rows" || echo "running: table 9 of 14"
            """))
        for f in (omp, restore, gh, migrate):
            f.chmod(0o755)

    def set_stage(self, stage: str):
        """Moves the PR on. The cards' PR pills come from the CLI's 10-minute cache: dropped, so
        the next card asks the fake `gh` again."""
        self.stage = stage
        self.stage_file.write_text(stage)
        (self.state_dir / "pr_cache.json").unlink(missing_ok=True)

    def turn(self, messages: list[dict]) -> tuple[list[dict], str]:
        """The scripted model's next turn for `messages`: a tool call's follow-up words once its
        result is in, else the turn for the latest line it knows."""
        last = messages[-1] if messages else {}
        content = last.get("content") if isinstance(last.get("content"), list) else []
        result = next((c for c in content if c.get("type") == "tool_result"), None)
        self.model_log.write(json.dumps({"result": bool(result), "text": user_text(last)[-160:]}) + "\n")
        self.model_log.flush()
        if result:
            # toolu_film_<step>, then toolu_film_<step>_park when the step parks after its words.
            step, *park = result["tool_use_id"].removeprefix("toolu_film_").split("_")
            call, words = SCRIPT[int(step)][1], SCRIPT[int(step)][2]
            if park:
                return [{"type": "text", "text": "Parked."}], "end_turn"
            if call.get("park_after"):
                return [{"type": "text", "text": words},
                        {"type": "tool_use", "id": f"toolu_film_{step}_park", "name": "waitingfor",
                         "input": {"action": "park"}}], "tool_use"
            return [{"type": "text", "text": words}], "end_turn"
        text = user_text(last)
        for i, (line, call, words) in enumerate(SCRIPT):
            if line in text:
                if call:
                    tool = {k: v for k, v in call.items() if k != "park_after"}
                    return [{"type": "tool_use", "id": f"toolu_film_{i}", "name": "waitingfor", "input": tool}], "tool_use"
                if line == "A review landed on":
                    # The agent pushes its fix: a new head, green, reviewed again, threads resolved.
                    threading.Timer(1.5, self.set_stage, ("fixed",)).start()
                return [{"type": "text", "text": words}], "end_turn"
        return [{"type": "text", "text": "OK."}], "end_turn"

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
        """A tab for each omp session."""
        first = [b["id"] for b in self.blocks()]
        self.blocks_of = {}
        omp = [str(self.bin / "omp-film"), "--resume"]
        for s in SESSIONS:
            cwd = str(self.home / "code" / s["folder"])
            made = json.loads(self.tern("new", "tab", "--json", "--cwd", cwd, "--", *omp, s["id"]))
            self.blocks_of[s["id"]] = made["block"]
            self.tern("rename", str(made["block"]), s["folder"])
        for block in first:
            self.tern("close", str(block))
        self.tern("rename", self.session_name(), "Demo workspace")
        time.sleep(1.5)
        self.wait_for_omps([s["id"] for s in SESSIONS])
        with self.world_lock:
            self.world = {"stateDir": str(self.state_dir), "history": [],
                          "agents": [self.live_record(s) for s in SESSIONS]}
            self.save_world()
        # The omps load their extension and draw their conversations.
        time.sleep(8)

    def wait_for_omps(self, ids: list[str], tries: int = 160):
        for _ in range(tries):
            self.panes = self.pane_processes()
            if all(sid in self.panes for sid in ids):
                return
            time.sleep(0.25)
        raise RuntimeError(f"omp didn't start for {ids}")

    def pane_processes(self) -> dict[str, tuple[int, int]]:
        """The stand-ins, as take_tern finds them, and each real omp by the session it resumed."""
        found = super().pane_processes()
        ids = {s["id"] for s in SESSIONS}
        for proc in Path("/proc").iterdir():
            if not proc.name.isdigit():
                continue
            try:
                argv = [a.decode() for a in (proc / "cmdline").read_bytes().split(b"\0") if a]
                environ = (proc / "environ").read_bytes().split(b"\0")
            except OSError:
                continue
            sid = next((a for a in argv if a in ids), None)
            if sid is None or "--resume" not in argv or "--pane" in argv or Path(argv[0]).name in ("env", "sh"):
                continue
            env = dict(e.decode().split("=", 1) for e in environ if b"=" in e)
            # A restore runs through Tern's own shell, whose environment can drop the socket path.
            if env.get("TERN_PANE_SOCKET", str(self.daemon_sock)) == str(self.daemon_sock) and env.get("TERN_PANE", "").isdigit():
                found[sid] = (int(proc.name), int(env["TERN_PANE"]))
        return found

    # The CLI's state.

    def rewrite(self, change):
        """Changes waiting.json under the CLI's lock, then ticks as Tern would."""
        with open(self.state_dir / "waiting.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            path = self.state_dir / "waiting.json"
            state = json.loads(path.read_text())
            change(state)
            base.write_json(path, state)
        self.tick()

    def tick(self):
        subprocess.run([*CLI, "tick"], env=self.env, capture_output=True, text=True)

    def age(self, session: str, minutes: int):
        """Moves the session's record and its omp extension's last word `minutes` into the past:
        what was set an hour ago reads so, a check due within the hour is due now, and a closed
        session's extension has been gone long enough to reopen it."""
        def shift(value):
            if isinstance(value, dict):
                return {k: shift(v) for k, v in value.items()}
            if isinstance(value, list):
                return [shift(v) for v in value]
            if isinstance(value, str) and ISO.match(value):
                return (datetime.fromisoformat(value) - timedelta(minutes=minutes)).isoformat(timespec="seconds")
            return value

        def change(state):
            state["sessions"][session] = shift(state["sessions"][session])
            if session in state["extensions"]:
                state["extensions"][session] = shift(state["extensions"][session])
        self.rewrite(change)

    def poke(self, session: str):
        """Makes every check the session has due now."""
        now = datetime.now().astimezone().isoformat(timespec="seconds")

        def due(state):
            for wait in state["sessions"][session]["waits"]:
                if wait.get("next_check_at") and wait["next_check_at"] > now:
                    wait["next_check_at"] = now
        self.rewrite(due)

    def poke_while(self, session: str, done, seconds: float):
        """Pokes the session every second and a half until `done()` or `seconds` pass."""
        end = time.monotonic() + seconds
        while time.monotonic() < end and not done():
            self.poke(session)
            time.sleep(1.5)

    def waits(self, session: str) -> list[dict]:
        state = json.loads((self.state_dir / "waiting.json").read_text())
        return (state["sessions"].get(session) or {}).get("waits") or []

    def wait_state(self, session: str, until: str) -> str | None:
        return next((w["state"] for w in self.waits(session) if (w.get("condition") or {}).get("until") == until), None)

    def close_session(self, s: dict):
        """Closes the session's pane with Tern's key; the fixture then lists it as closed, as the
        scanner would."""
        self.event("key", keys=["Ctrl", "Shift", "W"])
        time.sleep(0.3)
        self.tern("close", str(self.blocks_of[s["id"]]))
        with self.world_lock:
            self.world["agents"] = [r for r in self.world["agents"] if r["sessionId"] != s["id"]]
            self.world["history"].append(self.history_record({**s, "closedBy": "exit", "ago": 0}))
            self.save_world()

    def reopened(self, s: dict, seconds: float = 40) -> bool:
        """After Tern reopened the closed session in a new tab: it runs again, in that pane."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.panes = self.pane_processes()
            if s["id"] in self.panes:
                with self.world_lock:
                    self.world["history"] = [r for r in self.world["history"] if r["sessionId"] != s["id"]]
                    self.world["agents"].append(self.live_record({**s, "ago": 0}))
                    self.save_world()
                return True
            self.tick()
            time.sleep(0.5)
        return False

    # Filming.

    def chapter(self, title: str, **fields):
        """The explainer column's text from now on: step, title, body, say, then and dot."""
        self.event("chapter", title=title, **fields)

    def mark(self, rect=None):
        """Rings `rect` of the window on the stage until the next mark; none clears it."""
        self.event("mark", surface="desktop", rect=[round(v) for v in rect] if rect else None)

    def ask(self, s: dict, line: str, after: float):
        """Puts `line` into the session's omp, sends it and gives the agent `after` seconds. The
        column shows what you say, so the line goes in whole instead of being typed out."""
        self.tern("focus", str(self.blocks_of[s["id"]]))
        time.sleep(0.6)
        self.ctl("type", json.dumps(line))
        time.sleep(1.4)
        self.key("Enter", "Enter")
        time.sleep(after)

    def film(self):
        self.tern("focus", str(self.blocks_of[PR_SESSION["id"]]))
        time.sleep(2.0)
        self.filming = True
        shooter = threading.Thread(target=self.shoot)
        shooter.start()
        try:
            self.cut(scene="wide", wait=False)
            self.chapter("Sessions pile up", body="A session waits on a review, a job or a free afternoon, "
                         "so you leave it open. The tabs multiply, and every switch back starts with "
                         "working out what it was for.")
            time.sleep(6.0)
            self.chapter("Set sessions aside", body="Say why in one sentence. Code watches what the "
                         "session waits for and follows up on time, so you can close the tab.",
                         then=["You come back to the reason and what happened since.",
                               "Code does the watching, on a timer or a check, without tokens."])
            time.sleep(6.5)

            pr = {"dot": 0, "step": "Babysit a pull request", "title": "The agent just opened PR #42.",
                  "body": "Review, CI and the merge will take hours. You don't want to keep checking.",
                  "say": PR_LINE}
            self.chapter(**pr)
            self.cut()
            time.sleep(3.0)
            self.ask(PR_SESSION, PR_LINE, 1.5)
            self.chapter(**pr, then=["The agent sets a babysit on the PR and parks the session.",
                                     "Code polls GitHub. No tokens are spent while it waits."])
            time.sleep(6.0)

            job = {"dot": 1, "step": "Wait for a job", "title": "A migration runs for 40 minutes.",
                   "body": "You want its result, not the session open in front of you.", "say": JOB_LINE}
            self.chapter(**job)
            time.sleep(3.5)
            self.ask(JOB_SESSION, JOB_LINE, 5.0)
            self.chapter(**job, then=["The agent writes a check that passes when the job is done."])
            time.sleep(2.5)
            try:
                badge = self.wait_shown(BADGE, 3.0)
            except RuntimeError:
                print("no strip badge on screen; ringing the strip", file=sys.stderr)
                badge = self.wait_shown(STRIP)
            self.mark(badge)
            self.chapter(**job, then=["The agent writes a check that passes when the job is done.",
                                      "The strip's badge always says whether closing is safe."])
            time.sleep(4.0)
            self.mark()
            self.chapter(**job, then=["The agent writes a check that passes when the job is done.",
                                      "The strip's badge always says whether closing is safe.",
                                      "You close the session. It reopens when the check passes."])
            time.sleep(1.5)
            self.close_session(JOB_SESSION)
            time.sleep(3.0)

            rollout = {"dot": 2, "step": "Check on a schedule", "title": "A rollout needs a daily look.",
                       "body": "Every day for a week, and then it should stop by itself.", "say": ROLLOUT_LINE}
            self.chapter(**rollout)
            time.sleep(3.0)
            self.ask(ROLLOUT_SESSION, ROLLOUT_LINE, 5.0)
            self.chapter(**rollout, then=["A daily repeat sends the agent its prompt once a day.",
                                          "After seven runs it ends."])
            time.sleep(5.0)

            later = {"dot": 3, "step": "When you have time", "title": "A big task for a quiet afternoon.",
                     "body": "Nothing to watch, only a note on why you'll come back.",
                     "say_label": "You type", "say": LATER_LINE}
            self.chapter(**later)
            time.sleep(3.0)
            self.ask(LATER_SESSION, LATER_LINE, 2.5)
            self.command("waiting", "Ctrl", "Alt", "W")
            self.chapter(**later, then=["The Waiting tab groups every session by who has the next step."])
            time.sleep(5.0)
            self.chapter(**later, then=["The Waiting tab groups every session by who has the next step.",
                                        "Not today? Snooze session hides it until the time you pick."])
            for _ in range(4):
                self.key("ArrowDown", "↓")
                time.sleep(0.3)
            self.key("F4", "F4")
            time.sleep(1.8)
            for _ in range(4):
                self.key("ArrowDown", "↓")
                time.sleep(0.25)
            self.key("Enter", "Enter")
            time.sleep(1.4)
            self.key("Enter", "Enter")
            # Snoozed, it leaves no tab; the Waiting tab comes right back for the PR.
            time.sleep(1.0)

            review = {"dot": 0, "step": "Babysit a pull request", "title": "An hour later, a review lands.",
                      "body": "Two open threads on PR #42. The session is still parked."}
            self.chapter(**review)
            self.command("waiting", "Ctrl", "Alt", "W")
            time.sleep(2.5)
            self.set_stage("reviewed")
            self.age(PR_SESSION["id"], 60)
            fixed = ["The babysit sends its fix prompt to the parked session.",
                     "The agent pushes a fix and resolves the threads."]
            self.chapter(**review, then=fixed)
            self.poke_while(PR_SESSION["id"], lambda: self.stage == "fixed", 12)
            time.sleep(3.0)
            self.poke_while(PR_SESSION["id"], lambda: self.wait_state(PR_SESSION["id"], "ready") in ("fired", "done"), 20)
            self.chapter(**review, then=[*fixed, "Checks green, reviewed, no open threads: it's back on you."])
            for _ in range(8):
                self.key("ArrowUp", "↑")
                time.sleep(0.1)
            self.key("Space", "Space")
            time.sleep(4.5)
            self.key("Space", "Space")
            time.sleep(0.4)
            self.key("Enter", "Enter")
            time.sleep(5.0)

            # You merge on GitHub; the last step of the babysat chain sees it and closes the PR out.
            self.chapter(dot=0, step="Babysit a pull request", title="You merge it on GitHub.",
                         body="The babysit's last step sees the merge and tells you. The agent never merges.")
            self.set_stage("merged")
            self.poke_while(PR_SESSION["id"], lambda: self.wait_state(PR_SESSION["id"], "merged") in ("fired", "done"), 15)
            time.sleep(4.5)

            self.chapter(dot=1, step="Wait for a job", title="Meanwhile, the migration finished.",
                         then=["Tern reopens the closed session in a new tab.",
                               "The agent reads the log and reports."])
            reported = self.model_log.tell()
            self.migration_done.write_text("")
            self.age(JOB_SESSION["id"], 60)
            self.reopened(JOB_SESSION, 40)
            # Tern reopens it in a tab of its own without taking focus; here you click that tab.
            if JOB_SESSION["id"] in self.panes:
                time.sleep(1.0)
                try:
                    self.tern("focus", str(self.panes[JOB_SESSION["id"]][1]))
                except RuntimeError as error:
                    print(error, file=sys.stderr)
            end = time.monotonic() + 30
            while time.monotonic() < end and "billing migration finished" not in \
                    (self.work / "model.log").read_text()[reported:].lower():
                time.sleep(0.5)
            time.sleep(4.0)

            # What you see in a session you come back to: the strip, then the card, which opened
            # by itself on focusing a session with a wait back on you.
            back = {"dot": 4, "step": "Coming back"}
            strip = {**back, "title": "A strip above the composer.",
                     "body": "Every set-aside session shows it while you work in it."}
            self.chapter(**strip)
            self.mark(self.wait_shown(STRIP))
            time.sleep(2.0)
            self.chapter(**strip, then=["Your sentence first, then what it waits for and what came back.",
                                        "Green when something came back, amber when it needs you."])
            time.sleep(5.5)
            try:
                x, y, w, h = self.wait_shown(CARD, 3.0)
            except RuntimeError:
                self.command("session_card", "Ctrl", "Alt", "I")
                x, y, w, h = self.wait_shown(CARD)
            card = {**back, "title": "The session card.",
                    "body": "It opens by itself when you come back to news, or with Ctrl+Alt+I."}
            self.chapter(**card)
            # A float shows at its glance size until focused; focused, it grows to its content, as
            # it does when you click into it to read it.
            self.glide(x + w * 0.4, y + 14)
            self.click()
            tree = json.loads(self.tern("ls", "--json"))
            floats = [b for s in tree["sessions"] if s.get("shown") for t in s["tabs"] if t.get("shown")
                      for b in t["blocks"] if b.get("pip")]
            if floats:
                self.tern("focus", str(floats[0]["id"]))
            else:
                print("no floating block to focus:", self.blocks(), file=sys.stderr)
            time.sleep(0.8)
            x, y, w, h = self.wait_shown(CARD)
            self.mark((x, y, w, h))
            # The window is drawn almost 1:1, so the camera only needs to come a little closer.
            cw, ch = max(w + 100, self.size[0] * 0.7), max(h + 80, self.size[1] * 0.7)
            cx = min(max(0, x + w / 2 - cw / 2), self.size[0] - cw)
            cy = min(max(0, y + h / 2 - ch / 2), self.size[1] - ch)
            self.cut((round(cx), round(cy), round(cw), round(ch)))
            time.sleep(1.0)
            self.chapter(**card, then=["Why you left, and when.", "Every wait, and what each one saw.",
                                       "What happened since, step by step.", "Seen, Snooze 1d, Stop and Close."])
            time.sleep(7.5)
            self.mark()

            self.chapter("Set sessions aside", body="Ask the agent in your own words, or type /wait. "
                         "Works in omp, best in Tern.")
            self.cut(scene="wide")
            time.sleep(3.5)
        finally:
            self.filming = False
            shooter.join()
        base.write_json(self.work / "timeline.json", {
            "transitionMs": base.TRANSITION_MS,
            "layout": "explainer",
            "dots": 5,
            "devices": [{"surface": "desktop", "kind": "display", "size": list(self.size)}],
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
            self.server.shutdown()


if __name__ == "__main__":
    WaitingTake(Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/codexbar-agentview-film-waiting").resolve()).run()

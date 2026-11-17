"""The omp extension (omp-extension/waitingfor.ts) under bun, against a fake codexbar_waiting.py."""

import json
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_codexbar_waiting import EXTENSION_PATH, SCRIPT_PATH, SID, extension_folder, run_extension_driver  # noqa: E402

# Logs each call to CLI_LOG, then answers as the CLI would for one session SID. FAKE_SLOW: a command
# that takes 5 s; FAKE_PROMPTS: the prompts the first claim-prompts hands out (queued until then).
FAKE_CLI = r"""
import json, os, sys, time
args = sys.argv[1:]
log = os.environ["CLI_LOG"]
with open(log, "a", encoding="utf-8") as out:
    out.write(json.dumps(args) + "\n")
if args[0] == os.environ.get("FAKE_SLOW"):
    time.sleep(5)
claimed = os.path.join(os.path.dirname(log), "claimed")
prompts = json.loads(os.environ.get("FAKE_PROMPTS") or "[]")
queued = 0 if os.path.exists(claimed) else len(prompts)
row = {"session_id": os.environ["SID"], "waits": [], "prompts_queued": queued}
answer = {"ok": True, "view": {"sessions": [row]}}
if args[0] == "list":
    answer = {"ok": True, "sessions": [row]}
elif args[0] == "claim-prompts":
    open(claimed, "w").close()
    answer = {"ok": True, "prompts": prompts if queued else [], "view": {"sessions": [{**row, "prompts_queued": 0}]}}
elif args[0] == "add":
    answer = {"ok": True, "wait": {"id": "w1"}, "view": {"sessions": [row]}}
print(json.dumps(answer), flush=True)
"""

# Starts the session, then runs CALL: a /wait line (`line`), a tool call (`tool`) or nothing but
# waiting for queued prompts (`prompts`). TRANSCRIPT: user messages already in the session; DROP:
# every send rejects later with PromptDroppedError.
DRIVER = r"""
import * as fs from "node:fs";
const { default: install } = await import(process.env.EXTENSION);
const SID = process.env.SID;
const chain = new Proxy(function () {}, { get: () => chain, apply: () => chain });
const handlers = {};
const commands = {};
let tool;
const sent = [];
class Text { constructor(text = "") { this.text = text; } setText(text) { this.text = text; } render() { return [this.text]; } describe() { return {}; } }
install({
    zod: chain, logger: { debug() {} }, pi: { Text },
    sendUserMessage(text) {
        sent.push(text);
        if (process.env.DROP) return Promise.reject(Object.assign(new Error("dropped"), { name: "PromptDroppedError" }));
    },
    on: (name, fn) => { handlers[name] = fn; }, registerCommand: (name, spec) => { commands[name] = spec; },
    registerShortcut() {}, registerTool: (spec) => { tool = spec; },
});
const widgets = {};
const branch = JSON.parse(process.env.TRANSCRIPT || "[]").map((text) => ({ type: "message", message: { role: "user", content: text, timestamp: Date.now() } }));
const ctx = {
    hasUI: true, cwd: process.cwd(), isIdle: () => true, hasPendingMessages: () => false,
    ui: { setWidget(name, factory) { widgets[name] = factory; }, notify() {}, select: async () => undefined, editor: async () => undefined },
    sessionManager: {
        getSessionId: () => SID, getHeader: () => ({ type: "session", id: SID }), getBranch: () => branch,
        onPersistenceNotice: () => () => {},
    },
    setTimeout: (fn, ms) => setTimeout(fn, ms), setInterval: (fn, ms) => setInterval(fn, ms),
    clearTimer: (timer) => clearTimeout(timer), shutdown: async () => {},
};
const log = () => { try { return fs.readFileSync(process.env.CLI_LOG, "utf8").trim().split("\n").filter(Boolean).map((line) => JSON.parse(line)); } catch { return []; } };
const acks = () => log().filter((args) => args[0] === "ack").map((args) => args.find((arg, i) => args[i - 1] === "--result"));
handlers.session_start({}, ctx);
for (let i = 0; i < 300 && !log().some((args) => args[0] === "list"); i++) await Bun.sleep(50);
await Bun.sleep(100);
const call = JSON.parse(process.env.CALL);
let result = null;
if (call.tool) result = await tool.execute("t", call.tool, null, null, ctx).then(() => null, (error) => error.message);
if (call.line) await commands.wait.handler(call.line, ctx);
if (call.prompts) for (let i = 0; i < 200 && acks().length < call.prompts; i++) await Bun.sleep(50);
// A tool call's answer is final; a line or prompts wait for the panel's refresh and any late send.
if (!call.tool) await Bun.sleep(300);
const panel = widgets["waitingfor-panel"]?.(null, { fg: (_tone, text) => text })?.render(200).join("\n") ?? null;
handlers.session_shutdown();
console.log(JSON.stringify({ panel, result, sent, acks: acks() }));
process.exit(0);
"""


@unittest.skipUnless(shutil.which("bun"), "needs bun to load the omp extension")
class ExtensionTests(unittest.TestCase):
    def drive(self, call: dict, timeout_ms: int | None = None, **env: str) -> dict:
        with extension_folder() as folder:
            root = Path(folder)
            package = root / "package"
            extension = package / "omp-extension" / EXTENSION_PATH.name
            scripts = package / "contents" / "scripts"
            extension.parent.mkdir(parents=True)
            scripts.mkdir(parents=True)
            source = EXTENSION_PATH.read_text(encoding="utf-8")
            if timeout_ms is not None:
                # Waiting the real 30 s out would make the test slow.
                source = source.replace("const CLI_TIMEOUT_MS = 30_000;", f"const CLI_TIMEOUT_MS = {timeout_ms};")
            extension.write_text(source, encoding="utf-8")
            shutil.copy(SCRIPT_PATH.parent / "waiting_menu.json", scripts)
            (scripts / SCRIPT_PATH.name).write_text(FAKE_CLI, encoding="utf-8")
            return run_extension_driver(
                self, DRIVER, SID=SID, EXTENSION=str(extension), CALL=json.dumps(call), CLI_LOG=str(root / "cli.log"),
                XDG_STATE_HOME=str(root / "state"), OMP_AGENT_DIR=str(root / "omp"), PI_CODING_AGENT_DIR="", TERN_PANE="",
                **env,
            )

    def test_a_date_followed_by_an_unreadable_clock_is_refused(self) -> None:
        answer = run_extension_driver(self, r"""
const { parseWait } = await import(process.env.EXTENSION);
console.log(JSON.stringify(["2026-11-07 10:30am standup", "snooze 2026-11-07 9.30 back", "2026-11-07 5pm call", "2026-11-07 standup"].map((line) => parseWait(line).error ?? null)));
""")
        self.assertEqual(answer, ["Not a time: 10:30am", "Not a time: 9.30", "Not a time: 5pm", None])

    def test_a_literal_check_shows_what_it_saved(self) -> None:
        # The check's own -- became the prompt's: the reading shows it.
        answer = self.drive({"line": "check ! grep -q -- x f -- tell me"})
        self.assertIn("Check: grep -q · prompt: x f -- tell me", answer["panel"] or "", answer)

    def test_a_cli_timeout_says_the_change_may_have_been_saved(self) -> None:
        # The fake answers `stop` after 5 s; the other calls, a fresh Python each, finish well within 0.5 s.
        answer = self.drive({"tool": {"action": "stop", "wait": "w1"}}, timeout_ms=500, FAKE_SLOW="stop")
        self.assertEqual(
            answer["result"],
            "codexbar_waiting.py timed out after 0.5 s; the change may have been saved: call list before retrying",
        )

    def test_a_resent_prompt_already_in_the_transcript_is_acked_without_a_second_send(self) -> None:
        prompts = [{"action": "a1", "text": "hello", "resend_check": True, "claimed_at": "2000-01-01T00:00:00Z"}]
        answer = self.drive({"prompts": 1}, FAKE_PROMPTS=json.dumps(prompts), TRANSCRIPT=json.dumps(["hello"]))
        self.assertEqual((answer["sent"], answer["acks"]), ([], ["delivered"]), answer)

    def test_a_dropped_prompt_is_acked_failed(self) -> None:
        prompts = [{"action": "a1", "text": "hello"}]
        answer = self.drive({"prompts": 2}, FAKE_PROMPTS=json.dumps(prompts), DROP="1")
        self.assertEqual((answer["sent"], sorted(answer["acks"])), (["hello"], ["delivered", "failed"]), answer)


if __name__ == "__main__":
    unittest.main()

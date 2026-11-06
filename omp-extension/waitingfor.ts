// waitingfor: what this omp session waits for, kept by CodexBar AgentView's codexbar_waiting.py.
//
// /wait alone (or Ctrl+Alt+B outside Tern) opens the set-aside menu; FORMS below lists the rest.
//
// The CLI holds all the logic and state; this extension draws the row and a panel above the
// composer, gives the agent the `waitingfor` tool, delivers queued prompts and reports turns.
// omp imports extensions from their real path, so a symlinked install finds the CLI in its repo.
// @ts-nocheck

import { execFile } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

const SCRIPTS = path.join(path.dirname(fs.realpathSync(import.meta.path)), "..", "contents", "scripts");
const CLI = path.join(SCRIPTS, "codexbar_waiting.py");
/** The set-aside items, shared with Tern's picker. */
const MENU_FILE = path.join(SCRIPTS, "waiting_menu.json");
const STATE_BASE = process.env.XDG_STATE_HOME ?? "";
const STATE_DIR = path.join(path.isAbsolute(STATE_BASE) ? STATE_BASE : path.join(os.homedir(), ".local", "state"), "codexbar-agentview");
const VIEW_NAME = "waiting_view.json";
const VIEW_FILE = path.join(STATE_DIR, VIEW_NAME);

const WIDGET = "waitingfor";
const PANEL = "waitingfor-panel";
const CLI_TIMEOUT_MS = 30_000;
const GH_TIMEOUT_MS = 15_000;
/** A guess for a prefill must not hold the next dialog for long. */
const GH_GUESS_MS = 3_000;
const VIEW_DEBOUNCE_MS = 300;
const REFRESH_MS = 60_000;
const HEARTBEAT_MS = 5 * 60_000;
/** How long a menu request from Tern stays worth answering. */
const MENU_REQUEST_MS = 15_000;
/** How many of the session's latest user messages a resend check looks through. */
const RESEND_WINDOW = 10;
/** How many of the session's latest messages the menu looks through for a PR. */
const PR_WINDOW = 4;
/** How often a check runs when nothing says; the CLI's default too. */
const CHECK_EVERY = "15m";
/** How long the agent has to turn a described check into a command. */
const CHECK_TTL_MS = 10 * 60_000;

const DURATION = /^(\d+[smhdw])+$/;
const REPO = /^[\w.-]+\/[\w.-]+$/;
const PR_REF = /^(?:#?\d+|https?:\/\/\S+|[\w.-]+\/[\w.-]+#\d+)$/;
const AT = /^(?:(\d{4}-\d{2}-\d{2})(?:[T ](\d{1,2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?))?|(\d{1,2}:\d{2}))(?:\s+([\s\S]*))?$/;
// `ready` leads: it is what a PR wait does by default, and Enter takes the first choice.
const UNTILS = [
	{ label: "ready", until: "ready", description: "Reviews and checks are through (the default)" },
	{ label: "review", until: "review_on_head", description: "A review bot finished on the current head" },
	{ label: "merged", until: "merged", description: "The pull request is merged" },
	{ label: "released", until: "released", description: "A release contains it" },
	{ label: "activity", until: "activity", description: "Someone else comments, reviews, pushes, merges or closes it" },
];
const SIZES = [
	{ label: "S", description: "Small task" },
	{ label: "M", description: "Medium task" },
	{ label: "L", description: "Large task" },
];
/**
 * What /wait takes: /wait help, an error's usage and the completion list all read this.
 * `word`: what completes the form (under `gh ` with `gh`; one form per word); `alone`: nothing
 * follows the word; `featured`: /wait help shows its example.
 */
const FORMS = [
	{ usage: "/wait", what: "open the menu (Ctrl+Alt+B outside Tern)" },
	{
		usage: "/wait <duration> <sentence> [-- prompt]",
		what: "after a while: 2d, 3h, 1h30m",
		example: "2d retry the migration -- Run the migration again and report what changed",
		featured: true,
	},
	{
		usage: "/wait <date> [HH:MM] <sentence> [-- prompt]",
		what: "at a time: 2026-11-07 09:00, or just 18:00 (the next one)",
		example: "2026-11-07 09:00 send the invoice",
	},
	{
		word: "pr",
		gh: true,
		usage: "/wait gh pr [N|URL] [until] <sentence> [-- prompt]",
		what: "until ready (default), review, merged, released or activity",
		example: "gh pr 42 ready waiting for review -- Merge it if the checks pass",
		featured: true,
	},
	{
		word: "babysit",
		gh: true,
		usage: "/wait gh babysit [N|URL] [sentence]",
		what: "bot review, the agent fixes, then ready and merged",
		example: "gh babysit 42 ship the backoff fix",
	},
	{
		word: "release",
		gh: true,
		usage: "/wait gh release [owner/repo] <sentence> [-- prompt]",
		what: "a repo's next release",
		example: "gh release owner/repo try the new version -- Upgrade and rerun the failing case",
	},
	{
		word: "check",
		usage: "/wait check [every <dur>] <what to check> [-- prompt]",
		what: `the agent writes a command for it; code runs it until it exits 0 (default every ${CHECK_EVERY})`,
		example: "check every 10m the staging deploy is green -- Tell me what changed",
		featured: true,
	},
	{
		usage: "/wait check [every <dur>] ! <command line> [-- prompt]",
		what: "your own command, run until it exits 0",
		example: "check ! curl -fsS https://example.com/health -- Tell me it is back",
		featured: true,
	},
	{
		word: "for",
		usage: "/wait for [what you wait for]",
		what: "this session's free-text wait, in your words; you mark it done; alone, it clears",
		example: "for Anna's answer on the contract",
		featured: true,
	},
	{
		word: "later",
		usage: "/wait later [S|M|L] <sentence>",
		what: "pinned, no reminder",
		example: "later M untangle the config",
		featured: true,
	},
	{ word: "list", alone: true, usage: "/wait list", what: "this session's details" },
	{ word: "done", usage: "/wait done [wait-id]", what: "close one wait, or all of them", example: "done w1" },
	{ word: "note", usage: "/wait note <text>", what: "set this session's sentence", example: "note on hold until Monday" },
	{ word: "help", alone: true, usage: "/wait help", what: "this help" },
];
const choice = ({ word, alone, what, example }) => ({
	label: word,
	value: alone ? word : `${word} `,
	description: example ? `${what}: ${example}` : what,
});
const TOP_CHOICES = [
	{ word: "2d", what: "wait two days", example: "2d retry the migration -- Run the migration again and report what changed" },
	{ word: "3h", what: "wait three hours", example: "3h check the deploy" },
	{ word: "1w", what: "wait a week", example: "1w see whether the fix held" },
	{ word: "gh", what: "a GitHub PR or release", example: "gh pr 42 ready waiting for review" },
	...FORMS.filter((form) => form.word && !form.gh),
].map(choice);
const GH_CHOICES = FORMS.filter((form) => form.gh).map(choice);
const BANG = { label: "!", description: "your own command line instead: ! curl -fsS https://example.com/health" };
const CHECK_CHOICES = [{ label: "every", description: `how often to run it: every 10m (default ${CHECK_EVERY})` }, BANG];
/** Their text may hold a `--` of its own, as they never fire a prompt. */
const NO_PROMPT: Record<string, true> = { done: true, note: true, later: true, for: true };

/** Runs a program to its end without blocking the UI thread; never rejects. */
function run(file: string, args: string[], options: { timeout: number; cwd?: string }) {
	const { promise, resolve } = Promise.withResolvers();
	execFile(file, args, { ...options, maxBuffer: 16 * 1024 * 1024 }, (error, stdout, stderr) => {
		resolve({ failed: Boolean(error), stdout: String(stdout), stderr: String(stderr), message: error?.message });
	});
	return promise;
}

/** One CLI command: its JSON answer, or an Error with the CLI's own message. */
async function cli(args: string[]) {
	const { stdout, stderr, message } = await run("python3", [CLI, ...args], { timeout: CLI_TIMEOUT_MS });
	let answer;
	try {
		answer = JSON.parse(stdout);
	} catch {
		throw new Error(stderr.trim().split("\n").pop() || message || "codexbar_waiting.py gave no answer");
	}
	if (answer.ok === false) throw new Error(answer.error ?? "codexbar_waiting.py failed");
	return answer;
}

const GH_REPO = ["repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"];
const GH_PR = ["pr", "view", "--json", "url", "-q", ".url"];

/** One line from `gh` in `cwd`, empty with gh's reason when it has none: this repo, or this branch's PR. */
async function ghValue(args: string[], cwd: string, timeout: number) {
	const { failed, stdout, stderr } = await run("gh", args, { timeout, cwd });
	return { value: failed ? "" : stdout.trim(), why: stderr.trim() || `gh ${args[0]} view failed` };
}

/** The CLI's target options for a PR: a bare number takes its repo from `gh` in `cwd`. */
async function prTarget(ref: string, cwd: string): Promise<string[]> {
	const number = ref.replace(/^#/, "");
	if (!/^\d+$/.test(number)) return [`--pr=${ref}`];
	const { value: repo, why } = await ghValue(GH_REPO, cwd, GH_TIMEOUT_MS);
	if (!repo) throw new Error(`No GitHub repo here for PR ${number}: ${why}`);
	return [`--repo=${repo}`, `--number=${number}`];
}

function cut(text: string): [string, string] {
	const match = text.match(/^(\S+)\s*([\s\S]*)$/);
	return match ? [match[1], match[2]] : ["", ""];
}

/** Text before and after the first standalone `--`; no prompt at all gives `undefined`. */
function splitPrompt(text: string): [string, string | undefined] {
	const match = text.match(/^([\s\S]*?)(?:^|\s+)--(?:\s+([\s\S]*))?$/);
	return match ? [match[1], match[2] ?? ""] : [text, undefined];
}

/** The options a user's own wait ends with: the sentence, the prompt, and the park. */
function userArgs(sentence: string, prompt?: string): string[] {
	return [...(sentence ? [`--note=${sentence}`] : []), ...(prompt ? [`--prompt=${prompt}`] : []), "--by", "user", "--park"];
}

function checkOptions(argv: string, label: string, interval: string): string[] {
	return ["--kind", "command", `--argv=${argv}`, "--predicate=exit0", `--label=${label}`, `--interval=${interval}`];
}

/**
 * A check, or `{error}`: `! command` runs as given; anything else is a description, and `check`
 * asks the agent for its command.
 */
function checkRequest(what: string, interval = CHECK_EVERY, prompt?: string) {
	if (!DURATION.test(interval)) return { error: `Check every takes a duration such as 10m, not ${interval}.` };
	const literal = what.match(/^!(?:\s+([\s\S]*))?$/);
	if (!literal) return { check: { description: what, interval, prompt: prompt || undefined } };
	const command = literal[1]?.trim();
	if (!command) return { error: "A command line must follow the !." };
	return { command: "add", args: [...checkOptions(command, command, interval), ...userArgs("", prompt)] };
}

/** The free-text wait set to `text`, which parks the session; empty clears it. */
function whatRequest(text: string) {
	return { command: "what", args: [`--text=${text}`, ...(text ? ["--park"] : [])] };
}

/**
 * `{command, args}` for one /wait line, `{check}` for a check the agent is to write, `{error,
 * forms}` for a bad line. `needs: "pr"|"repo"`: a target still to resolve, `pr` being the
 * reference the line gave.
 */
export function parseWait(input: string) {
	const [first, line] = cut(input.trim());
	const [sub, subLine] = cut(line);
	const [word, text] = first === "gh" ? [`gh ${sub}`.trim(), subLine] : [first, line];
	const key = DURATION.test(word) ? "<duration>" : /^\d/.test(word) ? "<date>" : word;
	const bad = (error: string, about = key) => ({ error, forms: FORMS.filter((form) => form.usage.startsWith(`/wait ${about}`)) });
	const [rest, prompt] = NO_PROMPT[key] ? [text, undefined] : splitPrompt(text);
	if (prompt === "") return bad("Nothing follows --: put the prompt after it, or drop the --.");
	const add = (kind: string, options: string[], sentence: string) => ({
		command: "add",
		args: ["--kind", kind, ...options, ...userArgs(sentence, prompt)],
	});
	switch (key) {
		case "":
			return { command: "menu", args: [] };
		case "list":
		case "help":
			return rest ? bad(`${key} takes nothing else.`) : { command: key, args: [] };
		case "done":
			return /\s/.test(rest) ? bad("done takes one wait id.") : { command: "done", args: rest ? [`--wait=${rest}`] : [] };
		case "note":
			return rest ? { command: "note", args: [`--text=${rest}`] } : bad("note needs the sentence.");
		case "later": {
			const [size, tail] = cut(rest);
			const sized = /^[SML]$/.test(size);
			const sentence = sized ? tail : rest;
			return sentence ? add("later", sized ? [`--size=${size}`] : [], sentence) : bad("later needs a sentence.");
		}
		case "for":
			return whatRequest(rest);
		case "check": {
			// Not followed by a duration, `every` starts the description: check every backup ran.
			const every = rest.match(/^every\s+(\S+)\s*([\s\S]*)$/);
			const timed = every && DURATION.test(every[1]);
			const what = timed ? every[2] : rest;
			if (!what) return bad("check needs what to check, or ! and a command line.");
			const request = checkRequest(what, timed ? every[1] : undefined, prompt);
			return request.error ? bad(request.error) : request;
		}
		case "gh pr": {
			const [head, tail] = cut(rest);
			const ref = PR_REF.test(head) ? head : undefined;
			const [next, after] = cut(ref ? tail : rest);
			const until = UNTILS.find((item) => item.label === next)?.until;
			const sentence = until ? after : ref ? tail : rest;
			if (!sentence) return bad("gh pr needs a sentence.");
			return { ...add("pr", until ? [`--until=${until}`] : [], sentence), needs: "pr", pr: ref };
		}
		case "gh babysit": {
			if (prompt !== undefined) return bad("gh babysit takes no prompt: its review step already has the fix prompt.");
			const [head, tail] = cut(rest);
			const ref = PR_REF.test(head) ? head : undefined;
			return { command: "babysit-pr", args: userArgs(ref ? tail : rest), needs: "pr", pr: ref };
		}
		case "gh release": {
			const [head, tail] = cut(rest);
			const named = REPO.test(head);
			const sentence = named ? tail : rest;
			if (!sentence) return bad("gh release needs a sentence.");
			return { ...add("release", named ? [`--repo=${head}`] : [], sentence), needs: named ? undefined : "repo" };
		}
		case "<duration>":
			return rest ? add("time", [`--in=${word}`], rest) : bad("A sentence must follow the time.");
		case "<date>": {
			const at = `${word} ${rest}`.trimEnd().match(AT);
			if (!at) return bad(`Not a date or time: ${word}`);
			if (!at[4]) return bad("A sentence must follow the time.");
			const clock = (at[2] ?? at[3])?.replace(/^\d:/, "0$&");
			return add("time", [`--at=${at[1] ? `${at[1]}${clock ? `T${clock}` : ""}` : clock}`], at[4]);
		}
		default:
			return first === "gh" ? bad("gh takes pr, babysit or release.", "gh") : bad(`Not a /wait form: ${word}`, "");
	}
}

/** What can follow the finished words of a /wait line. */
function choicesAfter(words: string[], waits) {
	const path = words.join(" ");
	if (!path) return TOP_CHOICES;
	if (path === "gh") return GH_CHOICES;
	if (path === "gh pr" || (words.length === 3 && path.startsWith("gh pr ") && PR_REF.test(words[2]))) return UNTILS;
	if (path === "later") return SIZES;
	if (path === "check") return CHECK_CHOICES;
	if (words.length === 3 && path.startsWith("check every ") && DURATION.test(words[2])) return [BANG];
	if (path === "done") return waits.map((wait) => ({ label: wait.id, value: wait.id, description: wait.label }));
	return [];
}

/** The completions for what follows `/wait `; each `value` replaces all of `prefix`. */
export function waitCompletions(prefix: string, waits = []) {
	const text = prefix.trimStart();
	const words = text.split(/\s+/);
	const word = words.pop();
	const head = text.slice(0, text.length - word.length);
	const items = choicesAfter(words, waits)
		.filter((item) => item.label.startsWith(word))
		.map(({ label, value = `${label} `, description }) => ({ label, value: head + value, description }));
	return items.length > 0 ? items : null;
}

/** An ISO time as the panel shows it, without seconds and year. */
function when(iso: string): string {
	return iso.slice(5, 16).replace("T", " ");
}

function waitFacts(wait): string {
	return [wait.state, wait.overdue ? "overdue" : null, wait.due_at ? `due ${when(wait.due_at)}` : null, wait.reason].filter(Boolean).join(", ");
}

/** `forms` as aligned columns; a line too wide for `width` drops its text under the command. */
function columns(forms, theme, width: number): string {
	const indent = Math.max(...forms.map((form) => form.usage.length)) + 2;
	return forms
		.map(({ usage, what }) => (indent + what.length <= width ? usage.padEnd(indent) : `${usage}\n    `) + theme.fg("muted", what))
		.join("\n");
}

// A panel is a function of the theme and the width it may fill, giving the panel's text.

function helpPanel(theme, width: number): string {
	return [
		columns(FORMS, theme, width),
		"",
		"A prompt after -- goes into the session when the wait fires. A command with its own -- goes through the menu.",
		"Without a PR, /wait takes the one in the last messages or on this branch; without a repo, this directory's.",
		"",
		theme.fg("muted", "Examples"),
		...FORMS.flatMap((form) => (form.featured ? `/wait ${form.example}` : [])),
	].join("\n");
}

function errorPanel(message: string, forms = []) {
	return (theme, width: number) => [theme.fg("error", message), ...(forms.length > 0 ? [columns(forms, theme, width)] : [])].join("\n");
}

/** The session details: `card` is the CLI's answer. */
function detailsPanel(card) {
	return (theme) => {
		const muted = (text: string) => theme.fg("muted", text);
		const waits = card.waits ?? [];
		const history = (card.history ?? []).slice(-5);
		const sentence = card.note || (waits.length > 0 ? "" : "Nothing is waiting in this session.");
		const place = [card.project, card.branch].filter(Boolean).join(" · ");
		return [
			sentence && theme.bold(sentence),
			card.suggested_note && `Suggested: ${card.suggested_note}`,
			place && muted(place),
			...(card.prs ?? []).map((pr) => {
				const facts = [pr.state, pr.review?.replaceAll("_", " "), pr.checks && `checks ${pr.checks}`].filter(Boolean).join(", ");
				return [`${pr.repo}#${pr.number}`, facts, pr.title].filter(Boolean).join("  ");
			}),
			...waits.map((wait) => `${wait.id}  ${wait.summary || wait.label} (${waitFacts(wait)})`),
			history.length > 0 && muted("Since then"),
			...history.map(({ at, text }) => `${when(at)}  ${text}`),
			card.next_step && `Next: ${card.next_step}`,
		]
			.filter(Boolean)
			.join("\n");
	};
}

/** The text of a message's content, as it was sent. */
function messageText(message): string {
	if (typeof message.content === "string") return message.content;
	return (message.content ?? [])
		.filter((block) => block.type === "text")
		.map((block) => block.text)
		.join("\n");
}

/** The session's latest user messages, for a prompt whose earlier send may have landed. */
function recentUserTexts(c): string[] {
	return c.sessionManager
		.getBranch()
		.filter((entry) => entry.type === "message" && entry.message?.role === "user")
		.slice(-RESEND_WINDOW)
		.map((entry) => messageText(entry.message));
}

/** The PR in the agent's latest message that names one: a URL, else a number. */
export function lastPr(c): string | undefined {
	const messages = c.sessionManager
		.getBranch()
		.filter((entry) => entry.type === "message")
		.slice(-PR_WINDOW)
		.reverse();
	for (const { message } of messages) {
		if (message?.role !== "assistant") continue;
		const text = messageText(message);
		const pr =
			text.match(/https:\/\/github\.com\/[\w.-]+\/[\w.-]+\/pull\/\d+/)?.[0] ?? text.match(/\b[\w.-]+\/[\w.-]+#\d+/)?.[0] ?? text.match(/\bpr\s*#(\d+)/i)?.[1];
		if (pr) return pr;
	}
	return undefined;
}

/** The options for what a /wait line left out: a PR from the last messages or this branch, a repo from `cwd`. */
async function targetOptions(parsed, c): Promise<string[]> {
	if (parsed.needs === "repo") {
		const { value: repo, why } = await ghValue(GH_REPO, c.cwd, GH_TIMEOUT_MS);
		if (!repo) throw new Error(`No GitHub repo here: ${why}`);
		return [`--repo=${repo}`];
	}
	if (parsed.needs !== "pr") return [];
	const named = parsed.pr ?? lastPr(c);
	if (named) return prTarget(named, c.cwd);
	const { value: url, why } = await ghValue(GH_PR, c.cwd, GH_TIMEOUT_MS);
	if (!url) throw new Error(`No PR in the last messages or on this branch: ${why}`);
	return [`--pr=${url}`];
}

async function readViewFile() {
	try {
		return JSON.parse(await fs.promises.readFile(VIEW_FILE, "utf8"));
	} catch {
		return null;
	}
}

const TOOL_FLAGS = {
	at: "at",
	in: "in",
	interval: "interval",
	predicate: "predicate",
	what: "what",
	until: "until",
	bots: "bots",
	size: "size",
	label: "label",
	note: "note",
	deadline: "deadline",
	deadline_in: "deadline-in",
	prompt: "prompt",
	verify: "verify",
	after: "after",
};

/** The CLI options of an agent's own `add`; never `--park`, as only the user sets a session aside. */
async function toolAddArgs(params, session: string, cwd: string): Promise<string[]> {
	if (!params.kind) throw new Error("add needs a kind");
	const args = ["add", "--session", session, "--kind", params.kind, "--by", "agent"];
	for (const [name, flag] of Object.entries(TOOL_FLAGS)) {
		if (params[name] !== undefined) args.push(`--${flag}=${params[name]}`);
	}
	if (params.argv) args.push(`--argv=${JSON.stringify(params.argv)}`);
	if (params.kind === "pr") args.push(...(await toolPrTarget(params, cwd)));
	if (params.kind === "release" && params.repo) args.push(`--repo=${params.repo}`);
	return args;
}

async function toolPrTarget(params, cwd: string): Promise<string[]> {
	if (params.pr) return prTarget(params.pr, cwd);
	if (!params.number) throw new Error("A PR needs pr (URL), or number with an optional repo");
	return params.repo ? [`--repo=${params.repo}`, `--number=${params.number}`] : prTarget(String(params.number), cwd);
}

/** One asked line; undefined on Esc. omp's `input` has no prefill, its editor in prompt style does. */
async function ask(c, title: string, value = ""): Promise<string | undefined> {
	return (await c.ui.editor(title, value, undefined, { promptStyle: true }))?.trim();
}

/** The chosen one of `items`, undefined on Esc; omp answers with the label. */
async function pick(c, title: string, items) {
	const label = await c.ui.select(title, items);
	return items.find((item) => item.label === label);
}

/** `--in=2d` for a duration, `--at=TIME` for anything else; the CLI checks both. */
function timeOptions(text: string): string[] {
	const compact = text.replace(/\s+/g, "").toLowerCase();
	return [DURATION.test(compact) ? `--in=${compact}` : `--at=${text}`];
}

/** The PR step: `pr` from the last messages, else this branch's, is prefilled. */
async function askPr(c, pr: string | undefined) {
	const ref = await ask(c, "PR URL, owner/repo#N or number", pr ?? (await ghValue(GH_PR, c.cwd, GH_GUESS_MS)).value);
	return ref ? prTarget(ref, c.cwd) : undefined;
}

const PROMPT_TITLE = "Prompt to send when it fires (empty: only tell me)";

/**
 * The steps of the menu's items, keyed by the item ids of MENU_FILE, which holds their text.
 * `options` asks what the form needs and gives its CLI options; undefined ends the flow (Esc, or a
 * required line left empty). The sentence and the prompt come after it, unless the flow turns
 * them off. `request` instead asks everything and gives a request as parseWait does. Shell panes
 * get Tern's own picker from the same file.
 */
const FLOWS = {
	time: {
		kind: "time",
		async options(c) {
			const due = await ask(c, "When? 2d, 3h, 18:00 or 2026-11-07 09:00", "1d");
			return due ? timeOptions(due) : undefined;
		},
	},
	pr: {
		kind: "pr",
		async options(c, pr) {
			const target = await askPr(c, pr);
			const until = target && (await pick(c, "Until", UNTILS));
			return until ? [...target, `--until=${until.until}`] : undefined;
		},
	},
	babysit: {
		command: "babysit-pr",
		prompt: false,
		options: askPr,
	},
	release: {
		kind: "release",
		async options(c) {
			const repo = await ask(c, "Repository, owner/repo", (await ghValue(GH_REPO, c.cwd, GH_GUESS_MS)).value);
			if (!repo) return undefined;
			if (!REPO.test(repo)) throw new Error(`GitHub release takes owner/repo, not ${repo}`);
			return [`--repo=${repo}`];
		},
	},
	check: {
		async request(c) {
			const what = await ask(c, "What should be checked? Describe it, or ! and a command");
			if (!what) return undefined;
			const every = await ask(c, "Check every", CHECK_EVERY);
			if (every === undefined) return undefined;
			const prompt = await ask(c, PROMPT_TITLE);
			if (prompt === undefined) return undefined;
			const request = checkRequest(what, every.replace(/\s+/g, "").toLowerCase() || CHECK_EVERY, prompt);
			if (request.error) throw new Error(request.error);
			return request;
		},
	},
	for: {
		async request(c, record) {
			const what = await ask(c, "What do you wait for? (empty: nothing any more)", record?.waiting_for ?? "");
			return what === undefined ? undefined : whatRequest(what);
		},
	},
	later: {
		kind: "later",
		prompt: false,
		async options(c) {
			const size = await pick(c, "How big?", SIZES);
			return size ? [`--size=${size.label}`] : undefined;
		},
	},
};

/** The menu's items for an omp session, in file order: `{label, description, flow}`, or the reason there are none. */
function readMenu() {
	try {
		const { items } = JSON.parse(fs.readFileSync(MENU_FILE, "utf8"));
		return {
			items: items
				.filter((item) => item.targets?.includes("session") && Object.hasOwn(FLOWS, item.id))
				.map((item) => ({
					label: item.label,
					description: item.command ? `${item.command} · ${item.detail}` : item.detail,
					flow: FLOWS[item.id],
				})),
		};
	} catch (error) {
		return { items: [], error: `Can't read the menu ${MENU_FILE}: ${error instanceof Error ? error.message : error}` };
	}
}
const MENU = readMenu();

/** What the agent gets for a described check. */
function checkInstruction(description: string): string {
	return [
		`I want to wait until this holds: "${description}".`,
		"Write a command that exits 0 exactly when it holds and non-zero otherwise. It runs without a shell, as an argv; use sh -c '…' only when it needs pipes.",
		"Run it once now and show me its exit code and output.",
		"Then call the waitingfor tool with action add, kind command and argv, and nothing else: the label, the interval and the rest come from my /wait check.",
	].join("\n");
}

export default function (pi) {
	const z = pi.zod;
	let ctx; // the main session with a UI; subagents and headless runs only get the tool and /wait
	let sessionId: string | undefined;
	let timers = [];
	let debounce;
	let watcher;
	let shownLine: string | undefined | null = null; // null: draw next time, whatever the line is
	let waits = []; // this session's open waits, as the last applied view had them
	let menuOpen = false;
	let menuSeq = 0; // the last menu request seen
	let activatedAt = 0; // in whole seconds, as a request's time has no finer ones
	let viewsApplied = 0;
	let panel; // what the panel shows, as a function (see helpPanel); undefined: nothing
	// CLI steps of the turn lifecycle run one at a time, so a turn's end can't overtake its ack.
	let tail = Promise.resolve();
	let claiming = false;
	let claimAgain = false;
	// Prompts this process sent: a follow-up stays out of the transcript until the turn ends.
	const sent = new Set<string>();
	// Checks the user described and the agent is to write, by session, with the instruction it got.
	const checks = new Map<string, { description: string; interval: string; prompt?: string; instruction: string; at: number }>();

	const log = (error) => pi.logger?.debug("waitingfor", { error: error instanceof Error ? error.message : String(error) });
	const quietly = (task) => {
		task().catch(log);
	};
	const inOrder = (step) => (tail = tail.then(step).catch(log));

	function drawRow(line: string | undefined) {
		if (!ctx || line === shownLine) return;
		shownLine = line;
		// In Tern a click on the row opens the session's card.
		const text = line && process.env.TERN_PANE ? `\x1b]8;;codexbar-agentview://card/${sessionId}\x1b\\${line}\x1b]8;;\x1b\\` : line;
		ctx.ui.setWidget(WIDGET, text ? [text] : undefined, { placement: "aboveEditor" });
		// omp draws widgets in the order they were set, and the panel belongs under the row.
		if (panel) drawPanel(ctx);
	}

	function panelComponent(build, theme) {
		let width: number | undefined;
		let text;
		return {
			render(available: number) {
				if (available !== width) {
					width = available;
					text = new pi.pi.Text(build(theme, available - 2), 1, 0);
				}
				return text.render(available);
			},
			invalidate() {
				width = undefined;
			},
		};
	}

	function drawPanel(c) {
		const build = panel;
		c.ui.setWidget(PANEL, build && ((_tui, theme) => panelComponent(build, theme)), { placement: "aboveEditor" });
	}

	function setPanel(c, build) {
		panel = build;
		drawPanel(c);
	}

	/** Shows the session details; a newer panel, or a cleared one, wins over a slow answer. */
	async function showDetails(c) {
		const loading = (theme) => theme.fg("muted", "Loading…");
		setPanel(c, loading);
		let details;
		try {
			details = detailsPanel((await cli(["card", "--session", c.sessionManager.getSessionId()])).card);
		} catch (error) {
			const message = error instanceof Error ? error.message : String(error);
			// A session with no message yet has nothing for the card to read.
			details = message.startsWith("unknown session") ? detailsPanel({}) : errorPanel(message);
		}
		if (panel === loading) setPanel(c, details);
	}

	/** Opens the menu for a request Tern made since the last view; one from before activation is old. */
	function answerMenuRequest(view) {
		const request = view.menu_requests?.[sessionId];
		if (!request || request.seq <= menuSeq) return;
		menuSeq = request.seq;
		const at = Date.parse(request.at);
		if (at >= activatedAt && Date.now() - at < MENU_REQUEST_MS) void openMenu(ctx);
	}

	function applyView(view) {
		if (!view || !ctx) return;
		viewsApplied += 1;
		const session = view.sessions?.find((record) => record.session_id === sessionId);
		waits = session?.waits ?? [];
		drawRow(session?.composer_row || undefined);
		if (session?.prompts_queued > 0) void claimPrompts();
		answerMenuRequest(view);
	}

	async function refresh() {
		const session = sessionId;
		const applied = viewsApplied;
		const view = await cli(["list", "--session", session]);
		// A view applied meanwhile came from a later command than this answer.
		if (session === sessionId && applied === viewsApplied) applyView(view);
	}

	async function claimOne() {
		const session = sessionId;
		if (!ctx) return;
		const { prompts = [] } = await cli(["claim-prompts", "--session", session]);
		for (const prompt of prompts) {
			// Switched away: the prompt stays `sending`, and this session's next claim checks it.
			if (!ctx || session !== sessionId) return;
			if (!(prompt.resend_check && (sent.has(prompt.action) || recentUserTexts(ctx).includes(prompt.text)))) {
				try {
					pi.sendUserMessage(prompt.text, ctx.isIdle() ? undefined : { deliverAs: "followUp" });
					sent.add(prompt.action);
				} catch (error) {
					applyView((await cli(["ack", "--action", prompt.action, "--result", "failed", `--error=${error?.message ?? error}`])).view);
					continue;
				}
			}
			applyView((await cli(["ack", "--action", prompt.action, "--result", "delivered"])).view);
		}
	}

	async function claimPrompts() {
		if (claiming) {
			claimAgain = true;
			return;
		}
		claiming = true;
		try {
			do {
				claimAgain = false;
				await inOrder(claimOne);
			} while (claimAgain);
		} finally {
			claiming = false;
		}
	}

	/** Reports `type` for a main-session event when the session's waits, as `applies` reads them, can react to it. */
	function report(type: string, applies: (session) => boolean, c) {
		const session = sessionId;
		if (!ctx || c?.agent?.kind === "sub") return;
		inOrder(async () => {
			const record = (await readViewFile())?.sessions?.find((s) => s.session_id === session);
			if (record && applies(record)) applyView((await cli(["event", "--type", type, "--session", session])).view);
		});
	}
	const waitingInput = (c) => report("waiting_input", (session) => session.waits?.some((wait) => wait.state === "acting"), c);

	function watchView() {
		if (watcher) return;
		try {
			fs.mkdirSync(STATE_DIR, { recursive: true, mode: 0o700 });
			watcher = fs.watch(STATE_DIR, { persistent: false }, (_event, file) => {
				if (!ctx || (file && file !== VIEW_NAME)) return;
				try {
					if (debounce) ctx.clearTimer(debounce);
					debounce = ctx.setTimeout(async () => applyView(await readViewFile()), VIEW_DEBOUNCE_MS);
				} catch (error) {
					log(error);
				}
			});
			watcher.on("error", log);
		} catch (error) {
			log(error); // the periodic refresh still keeps the row current
		}
	}

	function clearTimers() {
		for (const timer of timers) ctx?.clearTimer(timer);
		if (debounce) ctx?.clearTimer(debounce);
		timers = [];
		debounce = undefined;
	}

	function heartbeat() {
		return cli(["heartbeat", "--session", sessionId, "--features", "menu"]);
	}

	function activate(c) {
		if (c.agent?.kind === "sub" || !c.hasUI) return;
		clearTimers();
		ctx = c;
		shownLine = null; // a switched session may start with omp's widgets reset
		waits = [];
		menuSeq = 0;
		activatedAt = Date.now() - (Date.now() % 1000);
		setPanel(c, undefined);
		sessionId = c.sessionManager.getSessionId();
		// A session switched away from took its queued instruction with it.
		for (const id of checks.keys()) if (id !== sessionId) checks.delete(id);
		watchView();
		timers = [
			c.setInterval(() => quietly(refresh), REFRESH_MS),
			c.setInterval(() => quietly(heartbeat), HEARTBEAT_MS),
			// On resume or switch omp replaces the transcript after this hook returns; a prompt
			// sent before that would be lost, so the first claim runs on the next tick.
			c.setTimeout(() => {
				inOrder(heartbeat);
				quietly(refresh);
				void claimPrompts();
			}, 0),
		];
	}

	pi.on("session_start", (_event, c) => activate(c));
	pi.on("session_switch", (_event, c) => activate(c));
	pi.on("session_branch", (_event, c) => activate(c));
	pi.on("session_shutdown", () => {
		clearTimers();
		watcher?.close();
		watcher = undefined;
		ctx = undefined;
	});

	// A non-final agent_end hands over to a queued follow-up, which may be the prompt being settled
	// or the instruction for a described check.
	pi.on("agent_end", (event, c) => {
		if (event?.willContinue) return;
		report("turn_finished", (session) => session.waits?.length > 0, c);
		dropUnwrittenCheck(c);
	});
	pi.on("input", (_event, c) => {
		if (panel) setPanel(c, undefined);
	});
	pi.on("tool_approval_requested", (_event, c) => waitingInput(c));
	pi.on("tool_execution_start", (event, c) => {
		if (event.toolName === "ask") waitingInput(c);
	});

	/** Runs a user or agent command for `c`'s session and redraws the row from its answer. */
	async function act(c, args: string[]) {
		const answer = await cli(args);
		// `list` answers with the view itself.
		if (c.sessionManager.getSessionId() === sessionId) applyView(answer.view ?? answer);
		return answer;
	}

	async function waitMenu(c, session: string, open) {
		const items = open.map((wait) => ({ label: `${wait.id}  ${wait.label}`, description: waitFacts(wait), wait }));
		const chosen = await pick(c, "This session's waits", items);
		if (!chosen) return;
		const { id, state } = chosen.wait;
		const action = await c.ui.select(chosen.label, ["Done", ...(state === "pending" ? [] : ["+1 day"]), "Close"]);
		if (action === "Done") await act(c, ["done", "--session", session, `--wait=${id}`]);
		else if (action === "+1 day") await act(c, ["snooze", "--session", session, `--wait=${id}`, "--for", "1d"]);
	}

	/** A described check whose turn ended without the tool call is dropped, and the user told. */
	function dropUnwrittenCheck(c) {
		if (!ctx || c?.agent?.kind === "sub" || !checks.delete(sessionId)) return;
		setPanel(ctx, errorPanel("The agent didn't register a check."));
	}

	/**
	 * The described check an agent call writes: an add of a command in the turn the check's
	 * instruction started, within 10 minutes of that start. A turn that was running when the user
	 * asked has an older last user message.
	 */
	function checkWritten(c, params) {
		if (params.action !== "add" || params.kind !== "command" || !params.argv) return undefined;
		const session = c.sessionManager.getSessionId();
		const check = checks.get(session);
		const last = c.sessionManager
			.getBranch()
			.findLast((entry) => entry.type === "message" && entry.message?.role === "user")?.message;
		if (!check || !last || messageText(last) !== check.instruction) return undefined;
		if (Date.now() - (Number(last.timestamp) || check.at) <= CHECK_TTL_MS) return check;
		checks.delete(session);
		if (ctx && session === sessionId) setPanel(ctx, errorPanel("The agent took over 10 minutes: its command is its own wait, not your check."));
		return undefined;
	}

	/**
	 * Leaves a described check to the agent; its waitingfor call adds it as the user's (see the tool).
	 * One at a time: a queued instruction can't be taken back, so a second one would get the first's command.
	 */
	function requestCheck(c, check) {
		const session = c.sessionManager.getSessionId();
		const pending = checks.get(session);
		if (pending) throw new Error(`The agent is still on the check "${pending.description}"; ask for the next one after its turn.`);
		const instruction = checkInstruction(check.description);
		checks.set(session, { ...check, instruction, at: Date.now() });
		try {
			pi.sendUserMessage(instruction, c.isIdle() ? undefined : { deliverAs: "followUp" });
		} catch (error) {
			checks.delete(session);
			throw error;
		}
	}

	/** Carries out a request from a /wait line or the menu for `c`'s session. */
	async function perform(c, request) {
		if (request.check) return requestCheck(c, request.check);
		const target = await targetOptions(request, c);
		await act(c, [request.command, "--session", c.sessionManager.getSessionId(), ...target, ...request.args]);
	}

	/** The steps of an `options` flow, then its command and the park. Nothing happens before the command. */
	async function setAside(c, session: string, flow, pr: string | undefined, record) {
		const options = await flow.options(c, pr);
		if (!options) return;
		const sentence = await ask(c, "Why set it aside, what next?", record?.note ?? "");
		if (sentence === undefined) return;
		const prompt = flow.prompt === false ? "" : await ask(c, PROMPT_TITLE);
		if (prompt === undefined) return;
		const kind = flow.kind ? ["--kind", flow.kind] : [];
		await act(c, [flow.command ?? "add", "--session", session, ...kind, ...options, ...userArgs(sentence, prompt)]);
	}

	async function openMenu(c) {
		if (!c.hasUI || menuOpen) return;
		menuOpen = true;
		setPanel(c, undefined);
		try {
			if (MENU.error) throw new Error(MENU.error);
			const session = c.sessionManager.getSessionId();
			const record = (await act(c, ["list", "--session", session])).sessions?.[0];
			const open = record?.waits ?? [];
			const pr = lastPr(c);
			const prItem = MENU.items.find((item) => item.flow === FLOWS.pr);
			const short = pr?.replace(/^https:\/\/github\.com\/(.+)\/pull\/(\d+)$/, "$1#$2");
			const first = pr && prItem ? [{ ...prItem, description: prItem.description.replace("gh pr", `gh pr ${short}`) }] : [];
			const items = [
				...first,
				...(open.length > 0 ? [{ label: `This session's waits (${open.length})`, description: "done or one more day" }] : []),
				...MENU.items.filter((item) => first.length === 0 || item !== prItem),
			];
			const chosen = await pick(c, "What does this wait for?", items);
			if (!chosen) return;
			if (!chosen.flow) await waitMenu(c, session, open);
			else if (!chosen.flow.request) await setAside(c, session, chosen.flow, pr, record);
			else {
				const request = await chosen.flow.request(c, record);
				if (request) await perform(c, request);
			}
		} catch (error) {
			setPanel(c, errorPanel(error instanceof Error ? error.message : String(error)));
		} finally {
			menuOpen = false;
		}
	}

	pi.registerCommand("wait", {
		description: "Set this session aside: /wait opens the menu, or /wait 2d <sentence> [-- prompt] | gh pr|babysit|release | check | for | later | list | done | help",
		getArgumentCompletions: (prefix: string) => waitCompletions(prefix, waits),
		handler: async (input: string, c) => {
			const parsed = parseWait(input);
			setPanel(c, undefined);
			if (parsed.error) setPanel(c, errorPanel(parsed.error, parsed.forms));
			else if (parsed.command === "menu") await openMenu(c);
			else if (parsed.command === "help") setPanel(c, helpPanel);
			else if (parsed.command === "list") await showDetails(c);
			else {
				try {
					await perform(c, parsed);
				} catch (error) {
					setPanel(c, errorPanel(error instanceof Error ? error.message : String(error)));
				}
			}
		},
	});

	pi.registerShortcut("ctrl+alt+b", { description: "Set this session aside", handler: (c) => openMenu(c) });
	pi.registerShortcut("ctrl+alt+i", {
		description: "Show or hide this session's details",
		handler: (c) => (panel ? setPanel(c, undefined) : showDetails(c)),
	});

	pi.registerTool({
		name: "waitingfor",
		label: "Waiting for",
		description: [
			"Wait for an event and tell the user when it happens: a PR's review, comments, merge or release, CI, a command's output or a time. Code polls; no tokens until it fires.",
			"Use this whenever the user says wait for X, notify me when, or tell me if, and right after you create a pull request (kind pr, or babysit_pr for review, ready and merged). The user is notified in Tern and on their phone: don't add your own ntfy call or polling loop.",
			"The session can be parked or closed meanwhile; a closed one comes back to the user with a Resume button.",
			"Kinds: pr for a known PR's review_on_head, ready, merged, released, or activity (the next comment, review, merge or close from someone other than the user); release for a repo's next release; command for anything else code can check; time to retry later; something for what code can't watch, such as a person's reply, named by what: it never fires and the user closes it; later for when the user has time.",
			"Not for re-prompting yourself on a timer with no event to watch: that is the schedule tool.",
			"note is your suggested sentence for the user. The user's own sentence always stands: never try to overwrite it, and don't repeat a note the session already has.",
			"prompt is sent into this session when the wait fires. list shows the open waits, done closes one by its id.",
			"This tool never parks the session on its own; setting it aside is the user's call. When the user's /wait check asked you for a command, call add with kind command and argv only: it becomes their check.",
		].join("\n"),
		parameters: z.object({
			action: z.enum(["add", "babysit_pr", "list", "done"]).describe("What to do"),
			kind: z.enum(["time", "command", "pr", "release", "something", "later"]).optional().describe("add: what to wait for"),
			label: z.string().optional().describe("add: short label; derived when omitted"),
			note: z.string().optional().describe("add, babysit_pr: suggested sentence for the user"),
			in: z.string().optional().describe("time: delay such as 30m, 2h, 1d, 1h30m"),
			at: z.string().optional().describe("time: ISO 8601, local time without an offset"),
			argv: z.array(z.string()).optional().describe("command: argv, run without a shell"),
			interval: z.string().optional().describe("command, release: how often to check (default 15m, release 1h)"),
			predicate: z
				.string()
				.optional()
				.describe("command: exit0 (default), stdout_regex:<re>, json_eq:<dotted.path>=<json> or changed (stdout differs from its first run)"),
			what: z.string().optional().describe("something: what you wait for"),
			pr: z.string().optional().describe("pr, babysit_pr: URL, O/R#N, or a number in this repo"),
			repo: z.string().optional().describe("pr, babysit_pr: O/R, with number; release: the O/R whose next release to wait for"),
			number: z.number().optional().describe("pr, babysit_pr: PR number; the repo defaults to this directory's"),
			until: z.enum(["review_on_head", "ready", "merged", "released", "activity"]).optional().describe("pr: default ready; activity for any feedback from others"),
			bots: z.string().optional().describe("pr: review bots, comma separated"),
			size: z.enum(["S", "M", "L"]).optional().describe("later: how big the task is"),
			deadline: z.string().optional().describe("add: tell the user if nothing happened by this time"),
			deadline_in: z.string().optional().describe("add: the same as a delay from now"),
			prompt: z.string().optional().describe("add: text to send into this session when the wait fires"),
			verify: z.string().optional().describe("add: JSON condition that must hold after the prompt's turn"),
			after: z.string().optional().describe("add: start only after this wait id completes"),
			no_fix: z.boolean().optional().describe("babysit_pr: skip the automatic review-fix prompt"),
			released: z.boolean().optional().describe("babysit_pr: also wait for a release"),
			wait: z.string().optional().describe("done: the wait id to close"),
		}),
		approval: "write",
		loadMode: "essential",
		async execute(_id, params, _signal, _onUpdate, c) {
			if (c.agent?.kind === "sub") throw new Error("waitingfor belongs to the main session; report back to it instead");
			const session = c.sessionManager.getSessionId();
			let args;
			// The user's described check this call writes; a failed add keeps it for the agent's retry.
			const check = checkWritten(c, params);
			switch (params.action) {
				case "add":
					args = check
						? ["add", "--session", session, ...checkOptions(JSON.stringify(params.argv), check.description, check.interval), ...userArgs("", check.prompt)]
						: await toolAddArgs(params, session, c.cwd);
					break;
				case "babysit_pr":
					args = [
						"babysit-pr",
						"--session",
						session,
						...(await toolPrTarget(params, c.cwd)),
						...(params.note ? [`--note=${params.note}`] : []),
						...(params.no_fix ? ["--no-fix"] : []),
						...(params.released ? ["--released"] : []),
						"--by",
						"agent",
					];
					break;
				case "done":
					if (!params.wait) throw new Error("done needs a wait id; list shows them");
					args = ["done", "--session", session, `--wait=${params.wait}`];
					break;
				default:
					args = ["list", "--session", session];
			}
			const { view, ...answer } = await act(c, args);
			if (check) {
				if (checks.get(session) === check) checks.delete(session);
				if (ctx && session === sessionId) setPanel(ctx, () => `Check: ${params.argv.join(" ")} every ${check.interval}`);
			}
			const result = params.action === "list" ? (answer.sessions?.[0] ?? { waits: [] }) : answer;
			return { content: [{ type: "text", text: JSON.stringify(result) }], details: {} };
		},
	});
}

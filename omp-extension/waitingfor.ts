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
const HEALTH = "waitingfor-health";
const CLI_TIMEOUT_MS = 30_000;
const GH_TIMEOUT_MS = 15_000;
/** A guess for a prefill must not hold the next dialog for long. */
const GH_GUESS_MS = 3_000;
const VIEW_DEBOUNCE_MS = 300;
/** The CLI settles a delivered prompt on an idle heartbeat once the delivery is 2 minutes old (SETTLE_IDLE_S). */
const SETTLE_IDLE_MS = 125_000;
const REFRESH_MS = 60_000;
const HEARTBEAT_MS = 5 * 60_000;
/** How long a menu request from Tern stays worth answering. */
const MENU_REQUEST_MS = 15_000;
/** How many of the session's latest user messages a resend check looks through. */
const RESEND_WINDOW = 10;
/** How many of the agent's latest messages the menu looks through for a PR. */
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
		word: "every",
		usage: "/wait every <dur> [for <dur>|N times] -- <prompt>",
		what: "repeat the prompt until Done, for a while or N times; a sentence may go before the --",
		example: "every 1d 5 times triage new issues -- Summarize the issues opened since the last run",
	},
	{
		usage: "/wait every <dur> until <end> -- <prompt>",
		what: "<end>: ! command, check <what>, gh pr [N] [state], gh release [repo], or words the agent judges",
		example: "every 2h until the migration finished -- Check the migration and report its progress",
		featured: true,
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
	{ word: "done", usage: "/wait done [wait-id]", what: "close one wait, or what's on you (every wait when nothing is)", example: "done w1" },
	{ word: "note", usage: "/wait note <text>", what: "set this session's sentence", example: "note on hold until Monday" },
	{ word: "help", alone: true, usage: "/wait help", what: "this help" },
];
const choice = ({ word, alone, what, example }) => ({
	label: word,
	value: alone ? word : `${word} `,
	description: example ? `${what}: ${example}` : what,
});
const TOP_CHOICES = [
	{ word: "2d", what: "wait two days", example: FORMS.find((form) => form.usage.startsWith("/wait <duration>")).example },
	{ word: "3h", what: "wait three hours", example: "3h check the deploy" },
	{ word: "1w", what: "wait a week", example: "1w see whether the fix held" },
	{ word: "gh", what: "a GitHub PR or release", example: "gh pr 42 ready waiting for review" },
	...FORMS.filter((form) => form.word && !form.gh),
].map(choice);
const GH_CHOICES = FORMS.filter((form) => form.gh).map(choice);
const BANG = { label: "!", description: "your own command line instead: ! curl -fsS https://example.com/health" };
const CHECK_CHOICES = [{ label: "every", description: `how often to run it: every 10m (default ${CHECK_EVERY})` }, BANG];
const EVERY_CHOICES = [
	{ label: "30m", description: "every half hour" },
	{ label: "1h", description: "every hour" },
	{ label: "2h", description: "every two hours" },
	{ label: "1d", description: "once a day" },
];
// `end`: what repeatEnds gives once the line has that end, which takes the item off.
const EVERY_ENDS = [
	{ label: "for", description: "stop after a while: for 1d", end: "span" },
	{ label: "5 times", description: "stop after this many runs, any number of them", end: "times" },
	{ label: "until", description: "stop when it holds: ! command, check <what>, gh pr …, gh release …, or words the agent judges" },
];
const FOR_CHOICES = [
	{ label: "1d", description: "for a day" },
	{ label: "1w", description: "for a week" },
];
const TIMES = { label: "times", description: "stop after this many runs" };
const UNTIL_CHOICES = [
	{ ...BANG, description: "your command exits 0: ! curl -fsS https://example.com/health" },
	{ label: "check", description: "say what holds; the agent writes the command: check the deploy is green" },
	{ label: "gh", description: "a PR state or a repo's next release: gh pr 42 merged" },
];
/** Their text may hold a `--` of its own, as they never fire a prompt; list and help take no text at all. */
const NO_PROMPT: Record<string, true> = { done: true, note: true, later: true, for: true, list: true, help: true };

const messageOf = (error) => (error instanceof Error ? error.message : String(error));

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
	const { failed, stdout, stderr, message } = await run("gh", args, { timeout, cwd });
	return { value: failed ? "" : stdout.trim(), why: stderr.trim() || message || `gh ${args[0]} view failed` };
}

/** This directory's GitHub repo; without one, gh's reason, `about` saying what wanted it. */
async function cwdRepo(cwd: string, about = ""): Promise<string> {
	const { value, why } = await ghValue(GH_REPO, cwd, GH_TIMEOUT_MS);
	if (!value) throw new Error(`No GitHub repo here${about}: ${why}`);
	return value;
}

/** The CLI's target options for a PR: a bare number takes its repo from `gh` in `cwd`. */
async function prTarget(ref: string, cwd: string): Promise<string[]> {
	const number = ref.replace(/^#/, "");
	if (!/^\d+$/.test(number)) return [`--pr=${ref}`];
	return [`--repo=${await cwdRepo(cwd, ` for PR ${number}`)}`, `--number=${number}`];
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

/** The options a user's own wait ends with: the sentence, the prompt, and the park unless the menu was told not to. */
function userArgs(sentence: string, prompt?: string, park = true): string[] {
	return [...(sentence ? [`--note=${sentence}`] : []), ...(prompt ? [`--prompt=${prompt}`] : []), "--by", "user", ...(park ? ["--park"] : [])];
}

function checkOptions(argv: string, label: string, interval: string): string[] {
	return ["--kind", "command", `--argv=${argv}`, "--predicate=exit0", `--label=${label}`, `--interval=${interval}`];
}

/** A check's own command line, `! command` (or `!command`), rather than a description for the agent. */
const LITERAL_CHECK = /^!([\s\S]*)$/;

/**
 * A check, or `{error}`: a LITERAL_CHECK runs as given; anything else is a description, and `check`
 * asks the agent for its command. `repeat`: the options of a prompt the check's wait repeats until it passes.
 */
function checkRequest(what: string, { interval = CHECK_EVERY, prompt = "", sentence = "", park = true, close = false, repeat = [] } = {}) {
	if (!DURATION.test(interval)) return { error: `Check every takes a duration such as 10m, not ${interval}.` };
	const literal = what.match(LITERAL_CHECK);
	if (!literal) return { check: { description: what, interval, prompt: prompt || undefined, sentence, park, close, repeat } };
	const command = literal[1]?.trim();
	if (!command) return { error: "A command line must follow the !." };
	return { command: "add", args: [...checkOptions(command, command, interval), ...repeat, ...userArgs(sentence, prompt, park)], close };
}

const UNIT_SECONDS = { s: 1, m: 60, h: 3600, d: 86_400, w: 604_800 };

/** A DURATION's length in seconds. */
function seconds(duration: string): number {
	let total = 0;
	for (const [, count, unit] of duration.matchAll(/(\d+)([smhdw])/g)) total += Number(count) * UNIT_SECONDS[unit];
	return total;
}

/**
 * Why the CLI would refuse a repeat every `every` for `span`, before anyone typed the rest; undefined
 * when it wouldn't. It mirrors the limits of codexbar_waiting.py's `_repeat_options`: change both.
 */
function repeatError(every: string, span?: string): string | undefined {
	if (seconds(every) < 60 || seconds(every) > 365 * 86_400) return `A repeat runs every 1m to 365d, not every ${every}.`;
	if (span && seconds(span) <= seconds(every)) return `For ${span} ends by the first run, ${every} from now.`;
	return undefined;
}

/** The PR reference, its `--until` options and the sentence of what follows `gh pr`. */
function prParts(text: string) {
	const [head, tail] = cut(text);
	const ref = PR_REF.test(head) ? head : undefined;
	const [next, after] = cut(ref ? tail : text);
	const until = UNTILS.find((item) => item.label === next)?.until;
	return { ref, options: until ? [`--until=${until}`] : [], sentence: until ? after : ref ? tail : text };
}

/** The `--repo` options and the sentence of what follows `gh release`; without a repo, `needs` it from this directory. */
function releaseParts(text: string) {
	const [head, tail] = cut(text);
	return REPO.test(head) ? { options: [`--repo=${head}`], sentence: tail } : { options: [], sentence: text, needs: "repo" };
}

/**
 * `for <dur>` and `<N> times`, each once and in either order, taken off the front of `text`; a
 * `for` or `times` not followed by its value, or given twice, stays in `rest`. Parsing and
 * completion both read the repeat's end through it.
 */
function repeatEnds(text: string) {
	let rest = text;
	let span: string | undefined;
	let times: string | undefined;
	for (;;) {
		const lasting = span === undefined && rest.match(/^for\s+(\S+)(?:\s+([\s\S]*))?$/);
		const count = times === undefined && rest.match(/^(\d+)\s+times(?:\s+([\s\S]*))?$/);
		if (lasting && DURATION.test(lasting[1])) [, span, rest = ""] = lasting;
		else if (count) [, times, rest = ""] = count;
		else break;
	}
	return { span, times, rest };
}

/**
 * What follows `every`: `<dur> [for <dur>] [<N> times] [until <end> | <sentence>]`, or `{error}`.
 * `repeat` holds the CLI options.
 */
function everyParts(text: string) {
	const [every, tail] = cut(text);
	if (!DURATION.test(every)) return { error: `every takes a duration such as 2h${every ? `, not ${every}` : ""}.` };
	const { span, times, rest } = repeatEnds(tail);
	if (times !== undefined && Number(times) < 1) return { error: "times takes 1 run or more." };
	const invalid = repeatError(every, span);
	if (invalid) return { error: invalid };
	const until = rest.match(/^until(?:\s+([\s\S]*))?$/);
	if (until && !until[1]) return { error: "until needs what ends it: ! a command, check <what>, gh pr, gh release, or words the agent judges." };
	const repeat = [`--every=${every}`, ...(span ? [`--for=${span}`] : []), ...(times ? [`--times=${times}`] : [])];
	return { every, repeat, until: until?.[1], sentence: until ? "" : rest };
}

/** The free-text wait set to `text`, which parks the session unless told not to; empty clears it. */
function whatRequest(text: string, park = true) {
	return { command: "what", args: [`--text=${text}`, ...(text && park ? ["--park"] : [])] };
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
			const request = checkRequest(what, { interval: timed ? every[1] : undefined, prompt });
			return request.error ? bad(request.error) : request;
		}
		case "every": {
			if (prompt === undefined) return bad("every needs the prompt to repeat, after --.");
			const parts = everyParts(rest);
			if (parts.error) return bad(parts.error);
			const { repeat, until } = parts;
			if (until === undefined) return add("repeat", repeat, parts.sentence);
			const [head, tail] = cut(until);
			if (LITERAL_CHECK.test(until) || head === "check") {
				const what = head === "check" ? tail : until;
				if (!what) return bad("until check needs what to check, or ! and a command line.");
				const request = checkRequest(what, { interval: parts.every, prompt, repeat });
				return request.error ? bad(request.error) : request;
			}
			if (head !== "gh") return add("repeat", [...repeat, `--until=${until}`], "");
			const [sub, ghText] = cut(tail);
			if (sub === "pr") {
				const pr = prParts(ghText);
				return { ...add("pr", [...pr.options, ...repeat], pr.sentence), needs: "pr", pr: pr.ref };
			}
			// gh is taken, as check is: words for the agent to judge start with another word.
			if (sub !== "release") return bad("until gh takes pr or release; words for the agent to judge can't start with gh.");
			const release = releaseParts(ghText);
			return { ...add("release", [...release.options, ...repeat], release.sentence), needs: release.needs };
		}
		case "gh pr": {
			const pr = prParts(rest);
			if (!pr.sentence) return bad("gh pr needs a sentence.");
			return { ...add("pr", pr.options, pr.sentence), needs: "pr", pr: pr.ref };
		}
		case "gh babysit": {
			if (prompt !== undefined) return bad("gh babysit takes no prompt: its review step already has the fix prompt.");
			const [head, tail] = cut(rest);
			const ref = PR_REF.test(head) ? head : undefined;
			return { command: "babysit-pr", args: userArgs(ref ? tail : rest), needs: "pr", pr: ref };
		}
		case "gh release": {
			const release = releaseParts(rest);
			if (!release.sentence) return bad("gh release needs a sentence.");
			return { ...add("release", release.options, release.sentence), needs: release.needs };
		}
		case "<duration>":
			return rest ? add("time", [`--in=${word}`], rest) : bad("A sentence must follow the time.");
		case "<date>": {
			const at = `${word} ${rest}`.trimEnd().match(AT);
			if (!at) return bad(`Not a date or time: ${word}`);
			if (!at[4]) return bad("A sentence must follow the time.");
			return add("time", [`--at=${atValue(at)}`], at[4]);
		}
		default:
			return first === "gh" ? bad("gh takes pr, babysit or release.", "gh") : bad(`Not a /wait form: ${word}`, "");
	}
}

/** The `--at` value of an AT match: the clock padded to HH:MM, after the date and a T when there is one. */
function atValue(at): string {
	const clock = (at[2] ?? at[3])?.replace(/^\d:/, "0$&");
	return at[1] ? `${at[1]}${clock ? `T${clock}` : ""}` : clock;
}

/** What can follow the finished words after `every`: the interval, then `for`/`times`, then `until` and its kinds. */
function everyChoices(words: string[]) {
	if (words.length === 0) return EVERY_CHOICES;
	if (!DURATION.test(words[0])) return [];
	const { span, times, rest: text } = repeatEnds(words.slice(1).join(" "));
	const rest = text ? text.split(" ") : [];
	const given = { span, times };
	if (rest.length === 0) return EVERY_ENDS.filter((item) => item.end === undefined || given[item.end] === undefined);
	// Only a span the repeat takes: after `every 1d`, `for 1d` would end it by the first run.
	if (rest.length === 1 && rest[0] === "for" && span === undefined) return FOR_CHOICES.filter((item) => !repeatError(words[0], item.label));
	if (rest.length === 1 && /^\d+$/.test(rest[0]) && times === undefined) return [TIMES];
	if (rest[0] !== "until") return [];
	if (rest.length === 1) return UNTIL_CHOICES;
	if (rest[1] !== "gh") return [];
	const gh = choicesAfter(rest.slice(1), []);
	return rest.length === 2 ? gh.filter((item) => item.label !== "babysit") : gh;
}

/** What can follow the finished words of a /wait line. */
function choicesAfter(words: string[], waits) {
	const path = words.join(" ");
	if (!path) return TOP_CHOICES;
	if (words[0] === "every") return everyChoices(words.slice(1));
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

/** The theme color of an outcome: what a session's headline and row prefix are drawn in. */
const TONES: Record<string, string> = { fulfilled: "success", attention: "warning", overdue: "warning" };

/** theme.bold can come out plain, so the bold is written out. */
const bold = (text: string) => `\x1b[1m${text}\x1b[22m`;

/** The CLI's composer row: its prefix in the outcome's color, and what happened in bold when fulfilled. */
function rowText(session, theme): string {
	const row: string = session.composer_row;
	const tone = TONES[session.outcome];
	const space = row.indexOf(" ");
	if (!tone || space < 0) return row;
	const prefix = row.slice(0, space);
	const rest = row.slice(space + 1);
	const headline = session.headline;
	const fired = session.outcome === "fulfilled" && headline && row.startsWith(headline) ? headline.slice(space + 1) : "";
	return `${theme.fg(tone, prefix)} ${fired ? `${bold(fired)}${rest.slice(fired.length)}` : rest}`;
}

/** An ISO time as the panel shows it, without seconds and year. */
function when(iso: string): string {
	return iso.slice(5, 16).replace("T", " ");
}

/** A tick older than this means the waits went unchecked; the CLI ticks every few minutes at most. */
const TICK_STALE_MS = 15 * 60_000;
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "16:00" today, else "Nov 6, 16:00", as Tern and the applet write times. */
function clockTime(ms: number, now: number): string {
	const at = new Date(ms);
	const hhmm = `${String(at.getHours()).padStart(2, "0")}:${String(at.getMinutes()).padStart(2, "0")}`;
	return at.toDateString() === new Date(now).toDateString() ? hhmm : `${MONTHS[at.getMonth()]} ${at.getDate()}, ${hhmm}`;
}

/**
 * The warning a view's `health` gives at `now`; undefined while the waits are being checked. Before
 * the first tick (`checked_at` null) it warns only once this process has seen that since
 * `uncheckedSince` for 15 minutes, so a fresh install doesn't flash a warning.
 */
export function healthWarning(health, now = Date.now(), uncheckedSince?: number): string | undefined {
	if (!health) return undefined;
	const checked = Date.parse(health.checked_at ?? "");
	const since = Number.isNaN(checked) ? "never" : clockTime(checked, now);
	// One line, capped as the applet caps it: an error with a traceback must not flood the row above
	// the composer. Capped by code points, as a cut through a surrogate pair would draw garbage.
	const chars = Array.from(String(health.error ?? "").replace(/\s+/g, " ").trim());
	const error = chars.length > 120 ? `${chars.slice(0, 119).join("")}…` : chars.join("");
	if (error) return `⚠ Waits not checked since ${since}: ${error}`;
	const last = Number.isNaN(checked) ? uncheckedSince : checked;
	if (last !== undefined && now - last > TICK_STALE_MS) return `⚠ Waits not checked since ${since}`;
	const stale = Date.parse(health.agents_stale_since ?? "");
	return Number.isNaN(stale) ? undefined : `⚠ Session scan stale since ${clockTime(stale, now)}`;
}

function waitFacts(wait): string {
	return [wait.progress ?? wait.state, wait.overdue ? "overdue" : null, wait.due_at ? `due ${when(wait.due_at)}` : null, wait.reason].filter(Boolean).join(", ");
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
		"A prompt after -- goes into the session when the wait fires; with every, at each run.",
		"A run due while the last run's turn still goes is skipped. A command with its own -- goes through the menu.",
		"Without a PR, /wait takes the one in the last messages or on this branch; without a repo, this directory's.",
		"",
		theme.fg("muted", "Examples"),
		...FORMS.flatMap((form) => (form.featured ? `/wait ${form.example}` : [])),
	].join("\n");
}

function errorPanel(message: string, forms = []) {
	return (theme, width: number) => [theme.fg("error", message), ...(forms.length > 0 ? [columns(forms, theme, width)] : [])].join("\n");
}

/** The session details from `card`, the CLI's answer; the health warning has its own line above the row. */
function detailsPanel(card) {
	return (theme) => {
		const muted = (text: string) => theme.fg("muted", text);
		const waits = card.waits ?? [];
		const history = (card.history ?? []).slice(-5);
		const sentence = card.note || (waits.length > 0 ? "" : "Nothing is waiting in this session.");
		const place = [card.project, card.branch].filter(Boolean).join(" · ");
		const source = { wait: "waiting on", created: "created" };
		const prLine = (pr) => {
			const facts = [source[pr.source], pr.state, pr.review?.replaceAll("_", " "), pr.checks && `checks ${pr.checks}`].filter(Boolean).join(", ");
			return [`${pr.repo}#${pr.number}`, facts, pr.title].filter(Boolean).join("  ");
		};
		const prs = card.prs ?? [];
		const mentioned = prs.filter((pr) => pr.source === "mentioned");
		return [
			card.headline && theme.fg(TONES[card.outcome] ?? "muted", card.headline),
			card.outcome && card.still_waiting?.length > 0 && `${muted("Still waiting for")} ${card.still_waiting.join("  ·  ")}`,
			sentence && bold(sentence),
			card.suggested_note && `Suggested: ${card.suggested_note}`,
			place && muted(place),
			...prs.filter((pr) => pr.source !== "mentioned").map(prLine),
			mentioned.length > 0 && muted("Mentioned"),
			...mentioned.map((pr) => muted(prLine(pr))),
			...waits.map((wait) => `${wait.id}  ${wait.mark ?? ""}${wait.summary || wait.label} (${waitFacts(wait)})`),
			history.length > 0 && muted("Since set aside"),
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

/** The session's latest `count` messages from `role`, newest first, walking back from the end. */
function latestMessages(c, role: string, count: number) {
	const branch = c.sessionManager.getBranch();
	const found = [];
	for (let i = branch.length - 1; i >= 0 && found.length < count; i--) {
		const entry = branch[i];
		if (entry.type === "message" && entry.message?.role === role) found.push(entry.message);
	}
	return found;
}

/** Whether `text` is among the session's latest user messages, sent at `since` (ms) or later. */
function landed(c, text: string, since: number): boolean {
	return latestMessages(c, "user", RESEND_WINDOW).some((message) => !(Number(message.timestamp) < since) && messageText(message) === text);
}

const PR_URL = /https:\/\/github\.com\/[\w.-]+\/[\w.-]+\/pull\/\d+/g;
// owner/repo#N standing alone: not the tail of a longer path, the owner shaped like a GitHub
// account (no dots), and not a document's anchor such as docs/setup.md#3.
const PR_SHORT = /(?<![\w./-])[A-Za-z0-9][A-Za-z0-9-]*\/(?![\w.-]*\.(?:md|markdown|txt|rst|html?)#)[\w.-]+#\d+(?![\w-])/g;
const PR_NUMBER = /\bpr\s*#(\d+)(?!\w)/gi;

/** The PR in the agent's latest message that names one (a URL, owner/repo#N or PR #N), the message's last. */
export function lastPr(c): string | undefined {
	for (const message of latestMessages(c, "assistant", PR_WINDOW)) {
		const text = messageText(message);
		let latest;
		for (const pattern of [PR_URL, PR_SHORT, PR_NUMBER]) {
			for (const match of text.matchAll(pattern)) {
				if (!latest || match.index > latest.index) latest = { index: match.index, pr: match[1] ?? match[0] };
			}
		}
		if (latest) return latest.pr;
	}
	return undefined;
}

/** The options for what a /wait line left out: a PR from the last messages or this branch, a repo from `cwd`. */
async function targetOptions(parsed, c): Promise<string[]> {
	if (parsed.needs === "repo") return [`--repo=${await cwdRepo(c.cwd)}`];
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
	every: "every",
	for: "for",
	times: "times",
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
	if (params.kind === "release") args.push(`--repo=${params.repo ?? (await cwdRepo(cwd))}`);
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

/** A duration as typed in a dialog: `1h 30M` → `1h30m`. */
function durationOf(text: string): string {
	return text.replace(/\s+/g, "").toLowerCase();
}

/** `--in=2d` for a duration, `--at=TIME` for anything else, a time as a /wait line has it; the CLI checks both. */
function timeOptions(text: string): string[] {
	const compact = durationOf(text);
	if (DURATION.test(compact)) return [`--in=${compact}`];
	const at = text.match(AT);
	return [`--at=${at && !at[4] ? atValue(at) : text}`];
}

/** The PR step: `pr` from the last messages, else this branch's, is prefilled. */
async function askPr(c, pr: string | undefined) {
	const ref = await ask(c, "PR URL, owner/repo#N or number", pr ?? (await ghValue(GH_PR, c.cwd, GH_GUESS_MS)).value);
	return ref ? prTarget(ref, c.cwd) : undefined;
}

const PROMPT_TITLE = "Prompt to send when it fires (empty: only tell me)";
const CHECK_TITLE = "What should be checked? Describe it, or ! and a command";
const SENTENCE_TITLE = "Why set it aside, what next?";

const PARK_CHOICES = [
	{ label: "Park it", description: "off the screen until you open it", park: true },
	{ label: "Keep it open", description: "stays where it is", park: false },
];

/** What Close the session says will bring the session back. */
const CLOSE_NOTES = {
	prompt: "it reopens in Tern when the wait fires",
	quiet: "resume it from the Waiting tab when the wait fires",
	repeat: "it reopens in Tern for each run",
	something: "resume it from the Waiting tab",
	// "When I have time" sessions stay away a long while; the pin brings them back.
	later: "it stays pinned; resume it from When I have time",
};

/**
 * The menu's last step: the chosen `{park, close?}`, undefined on Esc. With `closeNote`, what brings the
 * session back, closing is offered first, but only while `idle(c)` holds: closing mid-turn, or while omp
 * retries, compacts or holds a queued message, would cut the turn off.
 * A check the agent is to write (`what`) closes once its turn wrote it.
 */
async function askPark(c, idle: (c) => boolean, closeNote?: string, what?: string) {
	if (!closeNote || !idle(c)) return pick(c, "Park the session?", PARK_CHOICES);
	const described = what !== undefined && !LITERAL_CHECK.test(what);
	const description = described ? `${closeNote}; it closes once the agent wrote the command` : closeNote;
	return pick(c, "Close or park the session?", [{ label: "Close the session", description, park: false, close: true }, ...PARK_CHOICES]);
}

/**
 * How a repeat ends, in the menu: `ask` gives the wait's `{kind, options}`, or `{check}` with a
 * check's text; undefined ends the flow (Esc, or a required line left empty).
 */
const ENDS = [
	{ label: "Keep going", description: "until you mark it done", ask: async () => ({ kind: "repeat", options: [] }) },
	{
		label: "Until the agent says so",
		description: "you say what; each run asks the agent, and it ends the repeat once that holds",
		async ask(c) {
			const until = await ask(c, "Until what holds? The agent judges it");
			return until ? { kind: "repeat", options: [`--until=${until}`] } : undefined;
		},
	},
	{
		label: "Until a command passes",
		description: "describe it and the agent writes the command, or ! and your own",
		async ask(c) {
			const what = await ask(c, CHECK_TITLE);
			return what ? { check: what } : undefined;
		},
	},
	{
		label: "Until a PR state",
		description: "ready, review, merged, released or new activity",
		async ask(c, pr) {
			const options = await FLOWS.pr.options(c, pr);
			return options && { kind: "pr", options };
		},
	},
	{
		label: "Until a release",
		description: "a repo's next release",
		async ask(c) {
			const options = await FLOWS.release.options(c);
			return options && { kind: "release", options };
		},
	},
	{
		label: "For a while",
		description: "then it ends: 1d, 1w",
		async ask(c, _pr, every) {
			// The prefill is one the repeat takes: 1d would end a daily repeat by its first run.
			const typed = await ask(c, "For how long? 1d, 1w", FOR_CHOICES.find((item) => !repeatError(every, item.label))?.label ?? "");
			if (!typed) return undefined;
			const span = durationOf(typed);
			if (!DURATION.test(span)) throw new Error(`For a while takes a duration such as 1d, not ${typed}.`);
			const invalid = repeatError(every, span);
			if (invalid) throw new Error(invalid);
			return { kind: "repeat", options: [`--for=${span}`] };
		},
	},
	{
		label: "N times",
		description: "then it ends",
		async ask(c) {
			const times = await ask(c, "How many runs?", "5");
			if (!times) return undefined;
			if (!/^[1-9]\d*$/.test(times)) throw new Error(`N times takes a whole number from 1, not ${times}.`);
			return { kind: "repeat", options: [`--times=${times}`] };
		},
	},
];

/**
 * The steps of the menu's items, keyed by the item ids of MENU_FILE, which holds their text.
 * `options` asks what the form needs and gives its CLI options; undefined ends the flow (Esc, or a
 * required line left empty). The sentence and the prompt come after it, unless the flow turns
 * them off; `closeNote` replaces the park step's own. `request(c, record, pr, idle)` instead asks
 * everything, the park step included, and gives a request as parseWait does, with `close` when the
 * user chose to close; `idle` is the guard askPark offers Close under.
 * Shell panes get Tern's own picker from the same file.
 */
const FLOWS = {
	time: {
		kind: "time",
		async options(c) {
			const due = await ask(c, "When? 2d, 3h, 18:00 or 2026-11-07 09:00", "1d");
			return due ? timeOptions(due) : undefined;
		},
	},
	every: {
		async request(c, record, pr, idle) {
			const typed = await ask(c, "Repeat every? 30m, 2h, 1d", "1h");
			if (!typed) return undefined;
			const every = durationOf(typed);
			if (!DURATION.test(every)) throw new Error(`Repeat every takes a duration such as 2h, not ${typed}.`);
			const invalid = repeatError(every);
			if (invalid) throw new Error(invalid);
			const end = await pick(c, "When does it end?", ENDS);
			const ends = end && (await end.ask(c, pr, every));
			if (!ends) return undefined;
			const prompt = await ask(c, "Prompt to repeat");
			if (prompt === undefined) return undefined;
			if (!prompt) throw new Error("Repeat a prompt needs the prompt to repeat.");
			const sentence = await ask(c, SENTENCE_TITLE, record?.note ?? "");
			if (sentence === undefined) return undefined;
			const choice = await askPark(c, idle, CLOSE_NOTES.repeat, ends.check);
			if (!choice) return undefined;
			const repeat = [`--every=${every}`];
			if (ends.check === undefined) {
				return { command: "add", args: ["--kind", ends.kind, ...ends.options, ...repeat, ...userArgs(sentence, prompt, choice.park)], close: choice.close };
			}
			const request = checkRequest(ends.check, { interval: every, prompt, sentence, park: choice.park, close: choice.close, repeat });
			if (request.error) throw new Error(request.error);
			return request;
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
		// Its review step has the fix prompt.
		closeNote: CLOSE_NOTES.prompt,
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
		async request(c, _record, _pr, idle) {
			const what = await ask(c, CHECK_TITLE);
			if (!what) return undefined;
			const every = await ask(c, "Check every", CHECK_EVERY);
			if (every === undefined) return undefined;
			const prompt = await ask(c, PROMPT_TITLE);
			if (prompt === undefined) return undefined;
			const choice = await askPark(c, idle, prompt ? CLOSE_NOTES.prompt : CLOSE_NOTES.quiet, what);
			if (!choice) return undefined;
			const request = checkRequest(what, { interval: durationOf(every) || CHECK_EVERY, prompt, park: choice.park, close: choice.close });
			if (request.error) throw new Error(request.error);
			return request;
		},
	},
	for: {
		async request(c, record, _pr, idle) {
			const what = await ask(c, "What do you wait for? (empty: nothing any more)", record?.waiting_for ?? "");
			if (what === undefined) return undefined;
			const choice = what ? await askPark(c, idle, CLOSE_NOTES.something) : PARK_CHOICES[1];
			return choice ? { ...whatRequest(what, choice.park), close: choice.close } : undefined;
		},
	},
	later: {
		kind: "later",
		prompt: false,
		closeNote: CLOSE_NOTES.later,
		// Closing needs the pin: it is what brings the session back.
		pin: true,
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
		return { items: [], error: `Can't read the menu ${MENU_FILE}: ${messageOf(error)}` };
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
	let settle; // the heartbeat that settles a prompt delivered while idle
	let watcher;
	let shownLine: string | undefined | null = null; // what drawRow last drew; null: draw next time, whatever it is
	let ownSession; // this session's record, as the last applied view had it
	let health; // the last applied view's health
	let uncheckedSince: number | undefined; // when a view no tick had checked yet first came, while none has
	let waits = []; // this session's open waits, as the last applied view had them
	let menuOpen = false;
	let closeAfterTurn: string | undefined; // the session to close once the turn writing its described check ends
	let menuSeq = 0; // the last menu request seen
	let activatedAt = 0; // in whole seconds, as a request's time has no finer ones
	// Views applied, counted apart: one with only a failed tick's health leaves the sessions as they were.
	let sessionsApplied = 0;
	let healthApplied = 0;
	let panel; // what the panel shows, as a function (see helpPanel); undefined: nothing
	// CLI steps of the turn lifecycle run one at a time, so a turn's end can't overtake its ack.
	let tail = Promise.resolve();
	let claiming = false;
	let claimAgain = false;
	// Prompts this process sent and the CLI has yet to hear about: a follow-up stays out of the
	// transcript until the turn ends.
	const sent = new Set<string>();
	// Checks the user described and the agent is to write, by session, with the instruction it got.
	const checks = new Map<
		string,
		{
			description: string;
			interval: string;
			prompt?: string;
			sentence: string;
			park: boolean;
			close: boolean;
			repeat: string[];
			instruction: string;
			at: number;
		}
	>();

	const log = (error) => pi.logger?.debug("waitingfor", { error: messageOf(error) });
	const quietly = (task) => {
		task().catch(log);
	};
	const inOrder = (step) => (tail = tail.then(step).catch(log));

	/** Draws the health warning, when there is one, over the session's row, when it has one. */
	function drawRow() {
		const session = ownSession;
		const line = session?.composer_row || undefined;
		const warning = healthWarning(health, Date.now(), uncheckedSince);
		const shown = JSON.stringify([session?.outcome, session?.headline, line, warning]);
		if (!ctx || shown === shownLine) return;
		shownLine = shown;
		// omp draws widgets in the order they were set: the warning, the row, then the panel.
		ctx.ui.setWidget(HEALTH, warning && ((_tui, theme) => panelComponent((t) => t.fg("warning", warning), theme)), { placement: "aboveEditor" });
		// In Tern a click on the row opens the session's card.
		const href = process.env.TERN_PANE ? `codexbar-agentview://card/${sessionId}` : undefined;
		const [open, close] = href ? [`\x1b]8;;${href}\x1b\\`, "\x1b]8;;\x1b\\"] : ["", ""];
		const widget = (_tui, theme) => {
			const text = new pi.pi.Text("", 1, 0);
			let plain;
			let linked;
			return {
				...panelComponent((t) => open + rowText(session, t) + close, theme),
				// Tern's native view paints an undescribed widget as terminal cells, where the link
				// shows no pointer and no hover. Described, the row opens its card on a plain click
				// (a span link would want Ctrl), and codexbar.css underlines it on hover. The same
				// node object while the text holds lets omp skip it.
				describe(cx) {
					text.setText(rowText(session, theme));
					const node = text.describe(cx);
					if (!href) return node;
					if (node !== plain) {
						plain = node;
						linked = { ...node, p: { ...node.p, href, actions: { click: "open" } } };
					}
					return linked;
				},
			};
		};
		ctx.ui.setWidget(WIDGET, line ? widget : undefined, { placement: "aboveEditor" });
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
			const message = messageOf(error);
			// A session with no message yet has nothing for the card to read; an unreadable state
			// fails the card, and the health warning above the row says since when.
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

	/** Applies a view; one without `sessions` is a failed tick's health alone, and the row stays as it was. */
	function applyView(view) {
		if (!view || !ctx) return;
		if ("health" in view) {
			healthApplied += 1;
			health = view.health;
			uncheckedSince = !health || health.checked_at ? undefined : (uncheckedSince ?? Date.now());
		}
		if (!view.sessions) {
			drawRow();
			return;
		}
		sessionsApplied += 1;
		ownSession = view.sessions.find((session) => session.session_id === sessionId);
		waits = ownSession?.waits ?? [];
		drawRow();
		if (ownSession?.prompts_queued > 0) void claimPrompts();
		answerMenuRequest(view);
	}

	/** Applies this session's list; when the CLI fails, the view file, which holds a failed tick's error. */
	async function refresh() {
		const session = sessionId;
		const applied = sessionsApplied;
		const healthSeen = healthApplied;
		let view;
		try {
			view = await cli(["list", "--session", session]);
		} catch (error) {
			log(error);
			view = await readViewFile();
		}
		// A view applied meanwhile came from a later command than this answer: its sessions win, and
		// so does a failed tick's health that came alone.
		if (!view || session !== sessionId || applied !== sessionsApplied) return;
		if (healthSeen !== healthApplied) delete view.health;
		applyView(view);
	}

	/**
	 * A send omp drops rejects later (PromptDroppedError), after the ack said delivered; unless its
	 * text reached the transcript after all, the prompt failed.
	 */
	function failIfDropped(c, session: string, prompt, since: number, sending) {
		Promise.resolve(sending).catch((error) => {
			if (error?.name !== "PromptDroppedError" && (session !== sessionId || landed(c, prompt.text, since))) return;
			sent.delete(prompt.action);
			inOrder(async () => applyView((await cli(["ack", "--action", prompt.action, "--result", "failed", `--error=${messageOf(error)}`])).view));
		});
	}

	async function claimOne() {
		const session = sessionId;
		if (!ctx) return;
		const claimedAt = Date.now();
		const { prompts = [] } = await cli(["claim-prompts", "--session", session]);
		let delivered = false;
		for (const prompt of prompts) {
			// Switched away: the prompt stays `sending`, and this session's next claim checks it.
			if (!ctx || session !== sessionId) return;
			const c = ctx;
			// The earlier claim's send may have landed; the same words from before it don't count.
			const resent = prompt.resend_check && (sent.has(prompt.action) || landed(c, prompt.text, Date.parse(prompt.claimed_at)));
			if (!resent) {
				try {
					failIfDropped(c, session, prompt, claimedAt, pi.sendUserMessage(prompt.text, c.isIdle() ? undefined : { deliverAs: "followUp" }));
					sent.add(prompt.action);
				} catch (error) {
					applyView((await cli(["ack", "--action", prompt.action, "--result", "failed", `--error=${messageOf(error)}`])).view);
					continue;
				}
			}
			applyView((await cli(["ack", "--action", prompt.action, "--result", "delivered"])).view);
			sent.delete(prompt.action);
			delivered = true;
		}
		if (!delivered || session !== sessionId || !idle()) return;
		// Idle right after the ack, no turn runs for the prompt: it landed before (a resend after a
		// crash), or its turn ended before the ack. The CLI settles it on an idle heartbeat once the
		// delivery is 2 minutes old, so one goes after SETTLE_IDLE_MS. One timer will do: the next
		// prompt is claimed only once this one settled.
		if (settle) ctx.clearTimer(settle);
		settle = ctx.setTimeout(() => quietly(heartbeat), SETTLE_IDLE_MS);
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
	// A repeat's run leaves its wait `waiting`; the CLI tells whether one is with the agent.
	const waitingInput = (c) =>
		report("waiting_input", (session) => session.waits?.some((wait) => wait.state === "acting" || (wait.state === "waiting" && wait.repeat)), c);

	function watchView() {
		if (watcher) return;
		try {
			fs.mkdirSync(STATE_DIR, { recursive: true, mode: 0o700 });
			watcher = fs.watch(STATE_DIR, { persistent: false }, (_event, file) => {
				if (!ctx || (file && file !== VIEW_NAME)) return;
				try {
					if (debounce) ctx.clearTimer(debounce);
					debounce = ctx.setTimeout(() => quietly(async () => applyView(await readViewFile())), VIEW_DEBOUNCE_MS);
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
		for (const timer of [...timers, debounce, settle]) if (timer) ctx?.clearTimer(timer);
		timers = [];
		debounce = undefined;
		settle = undefined;
	}

	// Between attempts of a retry or around a compaction omp may stream nothing while the turn goes on.
	let retrying = false;
	let compacting = false;
	/** omp is idle: no turn running, recovering or queued to start. Closing the session needs it too. */
	const idle = (c = ctx) => !retrying && !compacting && c?.isIdle() === true && c.hasPendingMessages?.() !== true;

	function heartbeat() {
		const pane = process.env.TERN_PANE;
		return cli(["heartbeat", "--session", sessionId, ...(pane ? ["--pane", pane] : []), "--features", "menu", ...(idle() ? ["--idle"] : [])]);
	}

	function activate(c) {
		if (c.agent?.kind === "sub" || !c.hasUI) return;
		clearTimers();
		ctx = c;
		shownLine = null; // a switched session may start with omp's widgets reset
		ownSession = undefined;
		waits = [];
		menuSeq = 0;
		closeAfterTurn = undefined; // chosen for the session switched away from
		retrying = false;
		compacting = false;
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
				// Without the CLI nothing here works: the first failure shows, the rest only in the log.
				const session = sessionId;
				inOrder(() =>
					heartbeat().catch((error) => {
						if (ctx && session === sessionId) setPanel(ctx, errorPanel(`waitingfor can't reach codexbar_waiting.py: ${messageOf(error)}`));
						throw error;
					}),
				);
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

	// The main session's only: a subagent's retry or compaction leaves this one's idle as it is.
	pi.on("auto_retry_start", (_event, c) => {
		if (c?.agent?.kind !== "sub") retrying = true;
	});
	pi.on("auto_retry_end", (_event, c) => {
		if (c?.agent?.kind !== "sub") retrying = false;
	});
	pi.on("auto_compaction_start", (_event, c) => {
		if (c?.agent?.kind !== "sub") compacting = true;
	});
	pi.on("auto_compaction_end", (_event, c) => {
		if (c?.agent?.kind !== "sub") compacting = false;
	});

	// A non-final agent_end hands over to a queued follow-up, which may be the prompt being settled
	// or the instruction for a described check.
	pi.on("agent_end", (event, c) => {
		if (event?.willContinue) return;
		report("turn_finished", (session) => session.waits?.length > 0, c);
		dropUnwrittenCheck(c);
		if (c?.agent?.kind !== "sub" && closeAfterTurn !== undefined && closeAfterTurn === sessionId) closeOnceSettled(c);
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
		const { id, state, kind, retryable } = chosen.wait;
		// Retry comes first for a wait that check errors stopped: after fixing the cause it is the step to take.
		const actions = [...(retryable ? ["Retry"] : []), "Done", ...(state === "pending" || kind === "later" ? [] : ["Snooze 1d"]), "Back"];
		const action = await c.ui.select(chosen.label, actions);
		if (action === "Retry") await act(c, ["retry", "--session", session, `--wait=${id}`]);
		else if (action === "Done") await act(c, ["done", "--session", session, `--wait=${id}`]);
		else if (action === "Snooze 1d") await act(c, ["snooze", "--session", session, `--wait=${id}`, "--for", "1d"]);
	}

	/** A described check whose turn ended without the tool call is dropped, and the user told. */
	function dropUnwrittenCheck(c) {
		if (!ctx || c?.agent?.kind === "sub" || !checks.delete(sessionId)) return;
		setPanel(ctx, errorPanel("The agent didn't register a check."));
	}

	/** When `check`'s instruction reached the agent, as its user message has it; undefined while still queued. */
	function checkStarted(c, check): number | undefined {
		const entry = c.sessionManager
			.getBranch()
			.findLast(
				(item) =>
					item.type === "message" &&
					item.message?.role === "user" &&
					!(Number(item.message.timestamp) < check.at) && // not the same words from an earlier check
					messageText(item.message) === check.instruction,
			);
		return entry && (Number(entry.message.timestamp) || check.at);
	}

	/** `c`'s session's described check; 10 minutes after its instruction reached the agent it has expired. */
	function pendingCheck(c) {
		const session = c.sessionManager.getSessionId();
		const check = checks.get(session);
		const started = check && checkStarted(c, check);
		if (started === undefined || Date.now() - started <= CHECK_TTL_MS) return check;
		checks.delete(session);
		if (ctx && session === sessionId) setPanel(ctx, errorPanel("The check expired: the agent took over 10 minutes to write it."));
		return undefined;
	}

	/**
	 * The described check an agent call writes: an add of a command once the check's instruction
	 * reached the agent, steering messages after it or not. While it is still queued, the running
	 * turn's command waits are the agent's own.
	 */
	function checkWritten(c, params) {
		if (params.action !== "add" || params.kind !== "command" || !params.argv) return undefined;
		const check = pendingCheck(c);
		return check && checkStarted(c, check) !== undefined ? check : undefined;
	}

	/**
	 * Leaves a described check to the agent; its waitingfor call adds it as the user's (see the tool).
	 * One at a time: a queued instruction can't be taken back, so a second one would get the first's command.
	 */
	function requestCheck(c, check) {
		const session = c.sessionManager.getSessionId();
		const pending = pendingCheck(c);
		if (pending) throw new Error(`The agent is still on the check "${pending.description}"; ask for the next one after its turn.`);
		const instruction = checkInstruction(check.description);
		const entry = { ...check, instruction, at: Date.now() };
		checks.set(session, entry);
		let sending;
		try {
			sending = pi.sendUserMessage(instruction, c.isIdle() ? undefined : { deliverAs: "followUp" });
		} catch (error) {
			checks.delete(session);
			throw error;
		}
		setPanel(c, (theme) => theme.fg("muted", `Asked the agent for a command that checks: ${check.description}`));
		// A dropped instruction rejects later; the agent never got it.
		Promise.resolve(sending).catch((error) => {
			if (error?.name !== "PromptDroppedError" && (session !== sessionId || checkStarted(c, entry) !== undefined)) return;
			if (checks.get(session) === entry) checks.delete(session);
			if (ctx && session === sessionId) setPanel(ctx, errorPanel(`The check didn't reach the agent: ${messageOf(error)}`));
		});
	}

	/** Carries out a request from a /wait line or the menu for `session`, the one open when it was asked; the CLI's answer. */
	async function perform(c, request, session: string) {
		if (request.check) {
			// The instruction goes to whichever session is open now.
			if (c.sessionManager.getSessionId() !== session) throw new Error("The session changed: ask for the check again.");
			return requestCheck(c, request.check);
		}
		const target = await targetOptions(request, c);
		return act(c, [request.command, "--session", session, ...target, ...request.args]);
	}

	/**
	 * Closes `session`, the one whose wait the CLI saved (`answer`), and with `pinned` its pin, which
	 * is what brings a When I have time session back. Without it, with a turn running, or with
	 * another session open by then, it stays open and says why.
	 */
	async function closeSession(c, session: string, answer, pinned = false) {
		if (pinned && answer?.pinned !== true) {
			setPanel(c, errorPanel(`Not closed: the session couldn't be pinned (${answer?.pin_error ?? "no pin"}). Try /pin, then close it.`));
			return;
		}
		// The CLI steps under way (an ack, a turn's end) finish first, and so do the claims they chain
		// on: a turn's end can claim a prompt, and its send starts a turn.
		for (let seen; seen !== tail || claiming; ) {
			seen = tail;
			await seen;
		}
		// shutdown ends whichever session is open, and a switch meanwhile made it another one.
		if (session !== sessionId || c.sessionManager.getSessionId() !== session) return;
		// A prompt sent while the dialogs were open may have started a turn, a retry or compaction may
		// still be finishing one, and closing would cut it off.
		if (!idle(c)) {
			const kept = pinned ? "The session stays pinned" : "The wait is saved";
			setPanel(c, errorPanel(`Not closed: the agent is still working. ${kept}; close it once the turn ends.`));
			return;
		}
		await c.shutdown();
	}

	/**
	 * Closes the session whose described check the agent just wrote, once the turn that wrote it has
	 * settled: omp reports idle a moment after agent_end. Five seconds still busy means a new turn.
	 */
	function closeOnceSettled(c, tries = 20) {
		if (!ctx || closeAfterTurn !== sessionId) return;
		if (!idle(c) && tries > 0) {
			ctx.setTimeout(() => closeOnceSettled(c, tries - 1), 250);
			return;
		}
		const session = closeAfterTurn;
		closeAfterTurn = undefined;
		closeSession(c, session, undefined).catch((error) => setPanel(c, errorPanel(messageOf(error))));
	}

	/**
	 * The steps of an `options` flow, then its command and, if chosen, the park or closing the session
	 * once the command succeeded. Nothing happens before the command.
	 */
	async function setAside(c, session: string, flow, pr: string | undefined, record) {
		const options = await flow.options(c, pr);
		if (!options) return;
		const sentence = await ask(c, SENTENCE_TITLE, record?.note ?? "");
		if (sentence === undefined) return;
		const prompt = flow.prompt === false ? "" : await ask(c, PROMPT_TITLE);
		if (prompt === undefined) return;
		const choice = await askPark(c, idle, flow.closeNote ?? (prompt ? CLOSE_NOTES.prompt : CLOSE_NOTES.quiet));
		if (!choice) return;
		const kind = flow.kind ? ["--kind", flow.kind] : [];
		const answer = await act(c, [flow.command ?? "add", "--session", session, ...kind, ...options, ...userArgs(sentence, prompt, choice.park)]);
		if (choice.close) await closeSession(c, session, answer, flow.pin === true);
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
				...(open.length > 0
					? [{ label: `This session's waits (${open.length})`, description: open.some((wait) => wait.retryable) ? "retry, done or one more day" : "done or one more day" }]
					: []),
				...MENU.items.filter((item) => first.length === 0 || item !== prItem),
			];
			const chosen = await pick(c, "What does this wait for?", items);
			if (!chosen) return;
			if (!chosen.flow) await waitMenu(c, session, open);
			else if (!chosen.flow.request) await setAside(c, session, chosen.flow, pr, record);
			else {
				const request = await chosen.flow.request(c, record, pr, idle);
				if (!request) return;
				const answer = await perform(c, request, session);
				if (request.close) await closeSession(c, session, answer);
			}
		} catch (error) {
			setPanel(c, errorPanel(messageOf(error)));
		} finally {
			menuOpen = false;
		}
	}

	pi.registerCommand("wait", {
		description: "Set this session aside: /wait opens the menu, or /wait 2d <sentence> [-- prompt] | every 2h -- <prompt> | gh pr|babysit|release | check | for | later | list | done | help",
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
					await perform(c, parsed, c.sessionManager.getSessionId());
				} catch (error) {
					setPanel(c, errorPanel(messageOf(error)));
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
			"The session can be parked or closed meanwhile; a closed one reopens in Tern by itself when a prompt is due, else it comes back to the user with a Resume button.",
			"Kinds: pr for a known PR's review_on_head, ready, merged, released, or activity (the next comment, review, merge or close from someone other than the user); release for a repo's next release; command for anything else code can check; time to retry later; repeat to send prompt on a timer; something for what code can't watch, such as a person's reply, named by what: it never fires and the user closes it; later for when the user has time.",
			"every sends prompt again and again instead of once: kind repeat runs until the user's Done; with kind command, pr or release it ends once that holds; for or times end it after a while or a number of runs; with kind repeat, until is a condition in words that you judge at each run, ending the repeat with end_repeat once it holds. A run due while the last run's turn still goes is skipped.",
			"note is your suggested sentence for the user. The user's own sentence always stands: never try to overwrite it, and don't repeat a note the session already has.",
			"prompt is sent into this session when the wait fires. list shows the open waits, done closes one by its id.",
			"This tool never parks the session on its own; setting it aside is the user's call. When the user's /wait check asked you for a command, call add with kind command and argv only: it becomes their check.",
		].join("\n"),
		parameters: z.object({
			action: z.enum(["add", "babysit_pr", "list", "done", "end_repeat"]).describe("What to do"),
			kind: z.enum(["time", "command", "pr", "release", "repeat", "something", "later"]).optional().describe("add: what to wait for"),
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
			until: z
				.string()
				.optional()
				.describe("pr: review_on_head, ready (default), merged, released, or activity for any feedback from others; repeat: when to stop, in words you judge"),
			every: z.string().optional().describe("repeat, command, pr, release: send prompt every this long (30m, 2h, 1d; at least 1m); prompt is then required"),
			for: z.string().optional().describe("with every: end the repeat after this long, longer than every"),
			times: z.number().int().optional().describe("with every: end the repeat after this many runs"),
			bots: z.string().optional().describe("pr: review bots, comma separated"),
			size: z.enum(["S", "M", "L"]).optional().describe("later: how big the task is"),
			deadline: z.string().optional().describe("add: tell the user if nothing happened by this time"),
			deadline_in: z.string().optional().describe("add: the same as a delay from now"),
			prompt: z.string().optional().describe("add: text to send into this session when the wait fires; with every, at each run"),
			verify: z.string().optional().describe("add: JSON condition that must hold after the prompt's turn"),
			after: z.string().optional().describe("add: start only after this wait id completes"),
			no_fix: z.boolean().optional().describe("babysit_pr: skip the automatic review-fix prompt"),
			released: z.boolean().optional().describe("babysit_pr: also wait for a release"),
			wait: z.string().optional().describe("done, end_repeat: the wait id"),
			reason: z.string().optional().describe("end_repeat: why it ended"),
		}),
		approval: "write",
		loadMode: "essential",
		async execute(_id, params, _signal, _onUpdate, c) {
			if (c.agent?.kind === "sub") throw new Error("waitingfor belongs to the main session; report back to it instead");
			const session = c.sessionManager.getSessionId();
			let args;
			// The user's described check this call writes; a failed add keeps it for the agent's retry.
			const check = checkWritten(c, params);
			const extra = check ? Object.keys(params).filter((name) => params[name] !== undefined && !["action", "kind", "argv"].includes(name)) : [];
			if (extra.length > 0) {
				throw new Error(`This add writes the user's /wait check, which sets the rest: call it again with action, kind and argv only, without ${extra.join(", ")}. The command must exit 0 when it holds.`);
			}
			switch (params.action) {
				case "add":
					args = check
						? [
								"add",
								"--session",
								session,
								...checkOptions(JSON.stringify(params.argv), check.description, check.interval),
								...check.repeat,
								...userArgs(check.sentence, check.prompt, check.park),
							]
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
				case "end_repeat":
					if (!params.wait) throw new Error("end_repeat needs the repeat's wait id; its prompt names it");
					args = ["end-repeat", "--session", session, `--wait=${params.wait}`, ...(params.reason ? [`--reason=${params.reason}`] : [])];
					break;
				default:
					args = ["list", "--session", session];
			}
			const { view, ...answer } = await act(c, args);
			if (check) {
				if (checks.get(session) === check) checks.delete(session);
				if (check.close && session === sessionId) closeAfterTurn = session;
				if (ctx && session === sessionId) setPanel(ctx, () => `Check: ${params.argv.join(" ")} every ${check.interval}`);
			}
			const result = params.action === "list" ? (answer.sessions?.[0] ?? { waits: [] }) : answer;
			return { content: [{ type: "text", text: JSON.stringify(result) }], details: {} };
		},
	});
}

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
/** Why omp moved a session to a new id (its persistence notice); the CLI re-keys the old record on all but open-elsewhere. */
const MOVE_REASONS = ["open-elsewhere", "replaced", "contested"];

const DURATION = /^(\d+[smhdw])+$/;
// GitHub's name rules: an account of up to 39 letters, digits and single inner hyphens; a repo of up to
// 100 letters, digits, `.`, `-` and `_`, but not `.` or `..`.
const OWNER_REPO = String.raw`(?=[A-Za-z0-9-]{1,39}\/)[A-Za-z0-9](?:-?[A-Za-z0-9])*\/(?!\.\.?(?:#|$))[\w.-]{1,100}`;
const REPO = new RegExp(`^${OWNER_REPO}$`);
const PR_REF = new RegExp(String.raw`^(?:#?\d+|https?:\/\/\S+|${OWNER_REPO}#\d+)$`);
/** A lone `--` right after a gh form's arguments ends them: the sentence follows, whatever its first word. */
const ARGS_END = "--";
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
		what: "repeat the prompt until you stop it, for a while or N times; a sentence may go before the --",
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
		usage: "/wait gh pr [N|URL] [until] [--] <sentence> [-- prompt]",
		what: "until ready (default), review, merged, released or activity",
		example: "gh pr 42 ready waiting for review -- Merge it if the checks pass",
		featured: true,
	},
	{
		word: "babysit",
		gh: true,
		usage: "/wait gh babysit [N|URL] [--] [sentence]",
		what: "bot review, the agent fixes, then ready and merged",
		example: "gh babysit 42 ship the backoff fix",
	},
	{
		word: "release",
		gh: true,
		usage: "/wait gh release [owner/repo] [--] <sentence> [-- prompt]",
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
		what: "this session's free-text wait, in your words; you stop it when it's settled; alone, it clears",
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
	{ word: "seen", usage: "/wait seen [wait-id]", what: "you've read what came: close the waits on you, or one of them", example: "seen w1" },
	{ word: "stop", usage: "/wait stop <wait-id|all>", what: "stop waiting: end one wait, or all of them", example: "stop w1" },
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
const NO_PROMPT: Record<string, true> = { seen: true, stop: true, note: true, later: true, for: true, list: true, help: true };

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

/** A check's own command line, `! command` (or `!command`), rather than a description for the agent. */
const LITERAL_CHECK = /^!([\s\S]*)$/;

/**
 * A check's add, or `{error}`: a LITERAL_CHECK runs as given; anything else is a description, and its
 * add (`described`) queues the instruction that asks the agent for the command. `repeat`: the options
 * of a prompt the check's wait repeats until it passes.
 */
function checkRequest(what: string, { interval = CHECK_EVERY, prompt = "", sentence = "", park = true, close = false, repeat = [] } = {}) {
	if (!DURATION.test(interval)) return { error: `Check every takes a duration such as 10m, not ${interval}.` };
	const literal = what.match(LITERAL_CHECK);
	const command = literal?.[1]?.trim();
	if (literal && !command) return { error: "A command line must follow the !." };
	const check = literal ? [`--argv=${command}`] : [`--describe=${what}`];
	return {
		command: "add",
		args: ["--kind", "command", ...check, "--predicate=exit0", `--label=${command ?? what}`, `--interval=${interval}`, ...repeat, ...userArgs(sentence, prompt, park)],
		close,
		...(literal ? {} : { described: what }),
	};
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

/**
 * A gh form's arguments, read off the front of `text`: `take(read)` takes the next word when `read`
 * gives it a value, and `rest()` gives what follows them, past a lone ARGS_END (`ended`).
 */
function ghArgs(text: string) {
	let body = text;
	return {
		take<T>(read: (word: string) => T | undefined): T | undefined {
			const [head, tail] = cut(body);
			const value = head === ARGS_END ? undefined : read(head);
			if (value !== undefined) body = tail;
			return value;
		},
		rest() {
			const [head, tail] = cut(body);
			return head === ARGS_END ? { body: tail, ended: true } : { body, ended: false };
		},
	};
}

/** What follows `gh pr`, or `gh babysit` without `states`: the PR reference, the `--until` options and the rest. */
function prParts(text: string, states = true) {
	const args = ghArgs(text);
	const ref = args.take((word) => (PR_REF.test(word) ? word : undefined));
	const until = states ? args.take((word) => UNTILS.find((item) => item.label === word)?.until) : undefined;
	return { ref, options: until ? [`--until=${until}`] : [], ...args.rest() };
}

/** What follows `gh release`: the repo, when the first word can be one, and the rest. */
function releaseParts(text: string) {
	const args = ghArgs(text);
	const repo = args.take((word) => (REPO.test(word) ? word : undefined));
	return { repo, ...args.rest() };
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
 * `{command, args}` for one /wait line (`described`: a check the agent is to write), `{error,
 * forms}` for a bad line. `needs: "pr"|"repo"`: a target still to resolve, `pr` being the
 * reference the line gave. `gh`: a GitHub wait, whose reading is shown once it is saved.
 * `guessedRepo`: a first word taken for the repo that gh is to confirm, `withoutRepo` the request
 * when it is no repo after all.
 */
export function parseWait(input: string) {
	const [first, line] = cut(input.trim());
	const [sub, subLine] = cut(line);
	const [word, text] = first === "gh" ? [`gh ${sub}`.trim(), subLine] : [first, line];
	const key = DURATION.test(word) ? "<duration>" : /^\d/.test(word) ? "<date>" : word;
	const bad = (error: string, about = key) => ({ error, forms: FORMS.filter((form) => form.usage.startsWith(`/wait ${about}`)) });
	// A gh form's arguments come off first: a lone -- right after them starts the sentence, and only
	// a later one starts the prompt.
	const gh = key === "gh pr" ? prParts(text) : key === "gh babysit" ? prParts(text, false) : key === "gh release" ? releaseParts(text) : undefined;
	const [rest, prompt] = NO_PROMPT[key] ? [text, undefined] : splitPrompt(gh ? gh.body : text);
	if (prompt === "") return bad("Nothing follows --: put the prompt after it, or drop the --.");
	const add = (kind: string, options: string[], sentence: string) => ({
		command: "add",
		args: ["--kind", kind, ...options, ...userArgs(sentence, prompt)],
	});
	// A first word that can be a repo may still be the sentence's (try/catch): unless a -- after it
	// says it is the repo, gh tells before the add (confirmRepo).
	const release = (parts, repeat: string[], sentence: string) => {
		if (!parts.repo) return { ...add("release", repeat, sentence), needs: "repo", gh: true };
		const request = { ...add("release", [`--repo=${parts.repo}`, ...repeat], sentence), gh: true };
		if (parts.ended) return request;
		return { ...request, guessedRepo: parts.repo, withoutRepo: { ...add("release", repeat, `${parts.repo} ${sentence}`.trimEnd()), needs: "repo", gh: true } };
	};
	switch (key) {
		case "":
			return { command: "menu", args: [] };
		case "list":
		case "help":
			return rest ? bad(`${key} takes nothing else.`) : { command: key, args: [] };
		case "seen":
			return /\s/.test(rest) ? bad("seen takes one wait id.") : { command: "mark-seen", args: rest ? [`--wait=${rest}`] : [] };
		case "stop":
			if (!rest || /\s/.test(rest)) return bad("stop takes one wait id, or all.");
			return { command: "stop", args: rest === "all" ? ["--all"] : [`--wait=${rest}`] };
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
				return { ...add("pr", [...pr.options, ...repeat], pr.body), needs: "pr", pr: pr.ref, gh: true };
			}
			// gh is taken, as check is: words for the agent to judge start with another word.
			if (sub !== "release") return bad("until gh takes pr or release; words for the agent to judge can't start with gh.");
			const target = releaseParts(ghText);
			return release(target, repeat, target.body);
		}
		case "gh pr":
			return rest ? { ...add("pr", gh.options, rest), needs: "pr", pr: gh.ref, gh: true } : bad("gh pr needs a sentence.");
		case "gh babysit":
			if (prompt !== undefined) return bad("gh babysit takes no prompt: its review step already has the fix prompt.");
			return { command: "babysit-pr", args: userArgs(rest), needs: "pr", pr: gh.ref, gh: true };
		case "gh release":
			return rest ? release(gh, [], rest) : bad("gh release needs a sentence.");
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
	// Here the first -- starts the prompt: until's gh arguments have no end of their own.
	const gh = choicesAfter(rest.slice(1), []).filter((item) => item !== SENTENCE_START);
	return rest.length === 2 ? gh.filter((item) => item.label !== "babysit") : gh;
}

const SENTENCE_START = { label: ARGS_END, description: "the sentence starts here, even with a word that reads as a PR, state or repo" };

/** A wait on the user: what Seen closes. */
const onYou = (wait) => wait.ball === "on_me";
/** A wait something still watches, which Stop ends; a fired one is Seen's. Stop all ends every wait. */
const watched = (wait) => wait.outcome !== "fulfilled";

/** What can follow the finished words of a /wait line. */
function choicesAfter(words: string[], waits) {
	const path = words.join(" ");
	if (!path) return TOP_CHOICES;
	if (words[0] === "every") return everyChoices(words.slice(1));
	if (path === "gh") return GH_CHOICES;
	if (path === "gh pr" || (words.length === 3 && path.startsWith("gh pr ") && PR_REF.test(words[2]))) return [...UNTILS, SENTENCE_START];
	if (path === "gh babysit" || path === "gh release" || (words.length === 3 && path.startsWith("gh release ") && REPO.test(words[2]))) return [SENTENCE_START];
	if (path === "later") return SIZES;
	if (path === "check") return CHECK_CHOICES;
	if (words.length === 3 && path.startsWith("check every ") && DURATION.test(words[2])) return [BANG];
	const ids = (list) => list.map((wait) => ({ label: wait.id, value: wait.id, description: wait.label }));
	if (path === "seen") return ids(waits.filter(onYou));
	if (path === "stop") return [...ids(waits.filter(watched)), ...(waits.length > 0 ? [{ label: "all", value: "all", description: `every wait of this session (${waits.length})` }] : [])];
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

/**
 * The CLI's composer row: its prefix in the outcome's color, and what happened in bold when fulfilled;
 * led by `close`, whether the session may be closed (closeVerdict).
 */
export function rowText(session, theme, close?): string {
	const row = outcomeRow(session, theme);
	return close ? `${theme.fg(close.verdict === "park" ? "warning" : "muted", close.text)} │ ${row}` : row;
}

function outcomeRow(session, theme): string {
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
 * What a view's `health` says at `now`: its `warning`, undefined while the waits are being checked, and
 * `unchecked` when that warning is that nothing checks them. Before the first tick (`checked_at` null)
 * it warns only once this process has seen that since `uncheckedSince` for 15 minutes, so a fresh
 * install doesn't flash a warning. A recent tick whose error came from one check (`error_kind`
 * "partial") checked the rest: the waits count as checked, and only the error shows. A CLI without
 * `error_kind` means the tick failed.
 */
function healthState(health, now: number, uncheckedSince?: number): { warning?: string; unchecked: boolean } {
	if (!health) return { unchecked: false };
	const checked = Date.parse(health.checked_at ?? "");
	const since = Number.isNaN(checked) ? "never" : clockTime(checked, now);
	const stale = Date.parse(health.agents_stale_since ?? "");
	const scanSince = Number.isNaN(stale) ? undefined : clockTime(stale, now);
	// One line, capped as the applet caps it: an error with a traceback must not flood the row above
	// the composer. Capped by code points, as a cut through a surrogate pair would draw garbage.
	const chars = Array.from(String(health.error ?? "").replace(/\s+/g, " ").trim());
	const error = chars.length > 120 ? `${chars.slice(0, 119).join("")}…` : chars.join("");
	const fresh = !Number.isNaN(checked) && now - checked <= TICK_STALE_MS;
	if (error && fresh && health.error_kind === "partial") {
		return { warning: `⚠ Last check: ${error}${scanSince ? ` · session scan stale since ${scanSince}` : ""}`, unchecked: false };
	}
	if (error) return { warning: `⚠ Waits not checked since ${since}: ${error}`, unchecked: true };
	const last = Number.isNaN(checked) ? uncheckedSince : checked;
	if (last !== undefined && now - last > TICK_STALE_MS) return { warning: `⚠ Waits not checked since ${since}`, unchecked: true };
	return { warning: scanSince && `⚠ Session scan stale since ${scanSince}`, unchecked: false };
}

export function healthWarning(health, now = Date.now(), uncheckedSince?: number): string | undefined {
	return healthState(health, now, uncheckedSince).warning;
}

/** Closing loses the waits' checks while nothing runs them, whatever the session's own verdict says. */
const UNCHECKED_CLOSE = { verdict: "park", reason: "unchecked", text: "park only: nothing checks waits" };

/**
 * Whether the session may be closed: the CLI's `close` (undefined without waits), or park while the
 * view's health says nothing checks the waits.
 */
export function closeVerdict(session, health, now = Date.now(), uncheckedSince?: number) {
	const close = session?.close ?? undefined;
	return close && healthState(health, now, uncheckedSince).unchecked ? UNCHECKED_CLOSE : close;
}

/**
 * What became of `known` in `manager`, undefined while it is still the session. omp moves a session it
 * can't write to a new file under a new id ("open-elsewhere", "replaced", "contested"; it reports each
 * through `onPersistenceNotice`), and the new header's `continuationOf` names the old id: `moved` then,
 * where any other change is another session.
 */
export function sessionChange(manager, known: string | undefined): { id: string; moved: boolean } | undefined {
	const id = manager?.getSessionId?.();
	if (!id || id === known) return undefined;
	return { id, moved: known !== undefined && manager.getHeader?.()?.continuationOf === known };
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
		"In gh forms a lone -- right after the PR, state or repo starts the sentence: gh pr -- review the patch.",
		"",
		theme.fg("muted", "Examples"),
		...FORMS.flatMap((form) => (form.featured ? `/wait ${form.example}` : [])),
	].join("\n");
}

function errorPanel(message: string, forms = []) {
	return (theme, width: number) => [theme.fg("error", message), ...(forms.length > 0 ? [columns(forms, theme, width)] : [])].join("\n");
}

/**
 * The session details from `card`, the CLI's answer, and `close`, whether it may be closed
 * (closeVerdict); the health warning has its own line above the row.
 */
function detailsPanel(card, close?) {
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
			close && `${muted("Closing:")} ${theme.fg(close.verdict === "park" ? "warning" : "muted", close.text)}`,
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

/** `request` as the line meant it: a guessed repo gh can't find was the sentence's first word; any other gh failure keeps it. */
async function confirmRepo(request, cwd: string) {
	if (!request.guessedRepo) return request;
	const { failed, stderr } = await run("gh", ["repo", "view", request.guessedRepo, "--json", "nameWithOwner"], { timeout: GH_TIMEOUT_MS, cwd });
	return failed && /Could not resolve to a Repository/i.test(stderr) ? request.withoutRepo : request;
}

/**
 * What a GitHub wait's CLI `args` were read as, so a word taken for a PR, a state or a repo shows;
 * `dropped`: the first word gh found no repo for, which stayed in the sentence.
 */
export function ghReading(command: string, args: string[], dropped?: string): string {
	const value = (name: string) => args.find((arg) => arg.startsWith(`--${name}=`))?.slice(name.length + 3);
	const number = value("number");
	const pr = value("pr") ?? (number && `${value("repo")}#${number}`);
	const until = UNTILS.find((item) => item.until === value("until"))?.label ?? "ready";
	const what = command === "babysit-pr" ? `Babysitting PR ${pr}` : pr ? `Waiting on PR ${pr} until ${until}` : `Waiting for the next release of ${value("repo")}`;
	const note = value("note");
	const every = value("every");
	return [what, every && `prompt every ${every}`, note && `"${note}"`, dropped && `${dropped} is no GitHub repo, so it stays in the sentence`].filter(Boolean).join(" · ");
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
 * session back, closing is offered first, but only while `canClose(c)` holds: omp is idle (closing
 * mid-turn, or while omp retries, compacts or holds a queued message, would cut the turn off) and
 * closing would lose nothing. A check the agent is to write (`what`) closes once its turn wrote it.
 */
async function askPark(c, canClose: (c) => boolean, closeNote?: string, what?: string) {
	if (!closeNote || !canClose(c)) return pick(c, "Park the session?", PARK_CHOICES);
	const described = what !== undefined && !LITERAL_CHECK.test(what);
	const description = described ? `${closeNote}; it closes once the agent wrote the command` : closeNote;
	return pick(c, "Close or park the session?", [{ label: "Close the session", description, park: false, close: true }, ...PARK_CHOICES]);
}

/**
 * How a repeat ends, in the menu: `ask` gives the wait's `{kind, options}`, or `{check}` with a
 * check's text; undefined ends the flow (Esc, or a required line left empty).
 */
const ENDS = [
	{ label: "Keep going", description: "until you stop it", ask: async () => ({ kind: "repeat", options: [] }) },
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
 * them off; `closeNote` replaces the park step's own. `request(c, record, pr, canClose)` instead asks
 * everything, the park step included, and gives a request as parseWait does, with `close` when the
 * user chose to close; `canClose` is the guard askPark offers Close under.
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
		async request(c, record, pr, canClose) {
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
			const choice = await askPark(c, canClose, CLOSE_NOTES.repeat, ends.check);
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
		async request(c, _record, _pr, canClose) {
			const what = await ask(c, CHECK_TITLE);
			if (!what) return undefined;
			const every = await ask(c, "Check every", CHECK_EVERY);
			if (every === undefined) return undefined;
			const prompt = await ask(c, PROMPT_TITLE);
			if (prompt === undefined) return undefined;
			const choice = await askPark(c, canClose, prompt ? CLOSE_NOTES.prompt : CLOSE_NOTES.quiet, what);
			if (!choice) return undefined;
			const request = checkRequest(what, { interval: durationOf(every) || CHECK_EVERY, prompt, park: choice.park, close: choice.close });
			if (request.error) throw new Error(request.error);
			return request;
		},
	},
	for: {
		async request(c, record, _pr, canClose) {
			const what = await ask(c, "What do you wait for? (empty: nothing any more)", record?.waiting_for ?? "");
			if (what === undefined) return undefined;
			const choice = what ? await askPark(c, canClose, CLOSE_NOTES.something) : PARK_CHOICES[1];
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
	let closeAfterCheck: { session: string; wait: string } | undefined; // the described check whose writing closes the session
	let menuSeq = 0; // the last menu request seen
	let activatedAt = 0; // in whole seconds, as a request's time has no finer ones
	let unwatchMoves; // ends the subscription to omp's notices of a session moved to a new file
	let watched; // the SessionManager unwatchMoves listens to
	const formerIds = new Set<string>(); // the ids omp moved this conversation from (followSession)
	let moves: { from: string; reason: string }[] = []; // the moves to this id the CLI has yet to hear of, oldest first (followSession)
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
	// Described checks whose instruction this process sent, by session: the agent's next command add
	// writes it (checkWritten). The CLI holds the check itself.
	const asked = new Map<string, { wait: string; text: string; at: number }>();

	const log = (error) => pi.logger?.debug("waitingfor", { error: messageOf(error) });
	const quietly = (task) => {
		task().catch(log);
	};
	const inOrder = (step) => (tail = tail.then(step).catch(log));

	/** Draws the health warning, when there is one, over the session's row, when it has one, led by its close verdict. */
	function drawRow() {
		const session = ownSession;
		const line = session?.composer_row || undefined;
		const warning = healthWarning(health, Date.now(), uncheckedSince);
		const verdict = closeVerdict(session, health, Date.now(), uncheckedSince);
		const shown = JSON.stringify([session?.outcome, session?.headline, line, warning, verdict]);
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
				...panelComponent((t) => open + rowText(session, t, verdict) + close, theme),
				// Tern's native view paints an undescribed widget as terminal cells, where the link
				// shows no pointer and no hover. Described, the row opens its card on a plain click
				// (a span link would want Ctrl), and codexbar.css underlines it on hover. The same
				// node object while the text holds lets omp skip it.
				describe(cx) {
					text.setText(rowText(session, theme, verdict));
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
			const { card } = await cli(["card", "--session", c.sessionManager.getSessionId()]);
			details = detailsPanel(card, closeVerdict(card, health, Date.now(), uncheckedSince));
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
		if (followSession()) return; // the activation refreshes
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
			if (error?.name !== "PromptDroppedError" && (!ours(session) || landed(c, prompt.text, since))) return;
			sent.delete(prompt.action);
			inOrder(async () => applyView((await cli(["ack", "--action", prompt.action, "--result", "failed", `--error=${messageOf(error)}`])).view));
		});
	}

	async function claimOne() {
		// A session omp moved claims under its new id: the old one's prompts are another process's now.
		if (followSession()) return; // the activation claims
		const session = sessionId;
		if (!ctx) return;
		const claimedAt = Date.now();
		// In Tern the pane says which process may take them: a session open in two panes is the exact one's.
		const pane = process.env.TERN_PANE;
		const { prompts = [] } = await cli(["claim-prompts", "--session", session, ...(pane ? ["--pane", pane] : [])]);
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
			// The instruction for a described check: the agent's command add writes it (checkWritten).
			if (prompt.check) asked.set(session, { wait: prompt.wait, text: prompt.text, at: resent ? Date.parse(prompt.claimed_at) : claimedAt });
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
		if (!ctx || c?.agent?.kind === "sub") return;
		followSession();
		const session = sessionId;
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
		if (followSession()) return Promise.resolve(); // the activation heartbeats
		const pane = process.env.TERN_PANE;
		const move = moves[0];
		const moveArgs = move ? ["--moved-from", move.from, "--move-reason", move.reason] : [];
		const beat = cli(["heartbeat", "--session", sessionId, ...(pane ? ["--pane", pane] : []), "--features", "menu", ...(idle() ? ["--idle"] : []), ...moveArgs]);
		if (!move) return beat;
		// Heartbeats carry the moves one at a time, each until one gets through: the CLI's re-key is idempotent.
		return beat.then((answer) => {
			if (moves[0] === move) moves.shift();
			if (moves.length > 0) quietly(heartbeat);
			return answer;
		});
	}

	/**
	 * Moves on with the session omp writes now, when that is no longer `sessionId`; true when it did. omp
	 * moves a session it can't write to a new file under a new id, with no session event: the conversation
	 * goes on there, and the old id is another process's (or nobody's). A move keeps what the turn is doing,
	 * the described check it writes and the panel; anything else is a switch. `notice`, omp's report of
	 * the move, says why; a move seen without one counts as open-elsewhere, which the CLI leaves alone.
	 */
	function followSession(notice?): boolean {
		const c = ctx;
		const change = c && sessionChange(c.sessionManager, sessionId);
		if (!change) return false;
		if (change.moved) {
			formerIds.add(sessionId);
			const check = asked.get(sessionId);
			if (check) asked.set(change.id, check);
			if (closeAfterTurn === sessionId) closeAfterTurn = change.id;
			if (closeAfterCheck?.session === sessionId) closeAfterCheck = { ...closeAfterCheck, session: change.id };
			const reason = MOVE_REASONS.includes(notice?.reason) ? notice.reason : "open-elsewhere";
			// Back-to-back moves all go to the newest id, but none across an open-elsewhere: the id moved
			// from there is another process's, and so is the record an earlier move would have handed it.
			// The CLI refuses a move from the id itself.
			moves = [...(reason === "open-elsewhere" ? [] : moves), { from: sessionId, reason }].filter((move) => move.from !== change.id);
		}
		activate(c, change.moved);
		return true;
	}

	/** `session` is this conversation's: the open one, or one omp moved it from. */
	const ours = (session: string) => session === sessionId || formerIds.has(session);

	function activate(c, moved = false) {
		if (c.agent?.kind === "sub" || !c.hasUI) return;
		clearTimers();
		ctx = c;
		shownLine = null; // a switched session may start with omp's widgets reset
		ownSession = undefined;
		waits = [];
		menuSeq = 0;
		if (!moved) {
			formerIds.clear();
			moves = [];
			closeAfterTurn = undefined; // chosen for the session switched away from
			closeAfterCheck = undefined;
			retrying = false;
			compacting = false;
			setPanel(c, undefined);
		}
		activatedAt = Date.now() - (Date.now() % 1000);
		sessionId = c.sessionManager.getSessionId();
		// A session switched away from took its delivered instruction with it.
		for (const id of asked.keys()) if (id !== sessionId) asked.delete(id);
		watchView();
		// omp tells of each move here, inside the write that made it, so a tool call or claim in the same
		// tick already sees the new id; it replays the earlier moves at once, which name this id already.
		if (c.sessionManager !== watched) {
			unwatchMoves?.();
			watched = c.sessionManager;
			unwatchMoves = watched.onPersistenceNotice?.((notice) => followSession(notice));
		}
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
		unwatchMoves?.();
		unwatchMoves = undefined;
		watched = undefined;
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
		if (c?.agent?.kind === "sub") return;
		// A check its turn didn't write is the CLI's to report; a later command add is the agent's own.
		asked.delete(sessionId);
		if (closeAfterTurn !== undefined && closeAfterTurn === sessionId) closeOnceSettled(c);
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
		// One wait has its own Stop; several also end at once, after a confirmation.
		const stopAll = `Stop all ${open.length} waits`;
		if (open.length > 1) items.push({ label: stopAll, description: "end every wait of this session" });
		const chosen = await pick(c, "This session's waits", items);
		if (!chosen) return;
		if (!chosen.wait) {
			const sure = await c.ui.confirm(`${stopAll}?`, "Each one ends now, whatever it was waiting for; none comes back to you.");
			if (sure) await act(c, ["stop", "--session", session, "--all"]);
			return;
		}
		const { id, retryable, snoozable } = chosen.wait;
		// Retry comes first for a wait that check errors stopped: after fixing the cause it is the step to take.
		const actions = [
			...(retryable ? ["Retry"] : []),
			...(onYou(chosen.wait) ? ["Seen"] : []),
			...(watched(chosen.wait) ? ["Stop"] : []),
			...(snoozable ? ["Snooze 1d"] : []),
			"Back",
		];
		const action = await c.ui.select(chosen.label, actions);
		if (action === "Retry") await act(c, ["retry", "--session", session, `--wait=${id}`]);
		else if (action === "Seen") await act(c, ["mark-seen", "--session", session, `--wait=${id}`]);
		else if (action === "Stop") await act(c, ["stop", "--session", session, `--wait=${id}`]);
		else if (action === "Snooze 1d") await act(c, ["snooze", "--session", session, `--wait=${id}`, "--for", "1d"]);
	}

	/**
	 * The described check an agent call writes: an add of a command once the check's instruction
	 * reached the agent, steering messages after it or not. While it is still on its way, the running
	 * turn's command waits are the agent's own.
	 */
	function checkWritten(c, params) {
		if (params.action !== "add" || params.kind !== "command" || !params.argv) return undefined;
		const check = asked.get(c.sessionManager.getSessionId());
		const reached =
			check &&
			c.sessionManager
				.getBranch()
				.some(
					(item) =>
						item.type === "message" &&
						item.message?.role === "user" &&
						!(Number(item.message.timestamp) < check.at) && // not the same words from an earlier check
						messageText(item.message) === check.text,
				);
		return reached ? check : undefined;
	}

	/** Carries out a request from a /wait line or the menu for `session`, the one open when it was asked; the CLI's answer. */
	async function perform(c, request, session: string) {
		const resolved = await confirmRepo(request, c.cwd);
		const args = [...(await targetOptions(resolved, c)), ...resolved.args];
		const answer = await act(c, [resolved.command, "--session", session, ...args]);
		if (c.sessionManager.getSessionId() !== session) return answer;
		if (resolved.gh) {
			const reading = ghReading(resolved.command, args, resolved === request ? undefined : request.guessedRepo);
			setPanel(c, (theme) => theme.fg("muted", reading));
		} else if (resolved.described) {
			setPanel(c, (theme) => theme.fg("muted", `Asked the agent for a command that checks: ${resolved.described}`));
		}
		return answer;
	}

	/**
	 * What keeps the session from closing now: nothing checking waits, or its close verdict (closeVerdict)
	 * not saying it comes back. After a save (`saved`) the session has a wait, so a view without its
	 * verdict blocks too. Undefined when closing loses nothing; the last applied view has the wait just saved.
	 */
	function closeBlocker(saved = false) {
		if (healthState(health, Date.now(), uncheckedSince).unchecked) return UNCHECKED_CLOSE;
		const close = ownSession?.close;
		if (!close) return saved ? { text: "no close verdict came back for it" } : undefined;
		return close.verdict === "safe" || close.verdict === "manual" ? undefined : { text: close.text || `verdict ${close.verdict}` };
	}

	/** Close is offered while omp is idle and closing would lose nothing. */
	const canClose = (c) => idle(c) && closeBlocker() === undefined;

	/**
	 * Closes `session`, the one whose wait the CLI saved (`answer`), and with `pinned` its pin, which
	 * is what brings a When I have time session back. Without it, with a turn running, with closing
	 * losing something (closeBlocker), or with another session open by then, it stays open and says why.
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
		// shutdown ends whichever session is open, and a switch meanwhile made it another one; a move
		// keeps the conversation.
		followSession();
		if (!ours(session) || c.sessionManager.getSessionId() !== sessionId) return;
		// A prompt sent while the dialogs were open may have started a turn, a retry or compaction may
		// still be finishing one, and closing would cut it off.
		if (!idle(c)) {
			const kept = pinned ? "The session stays pinned" : "The wait is saved";
			setPanel(c, errorPanel(`Not closed: the agent is still working. ${kept}; close it once the turn ends.`));
			return;
		}
		const blocker = closeBlocker(true);
		if (blocker) {
			const kept = pinned ? "The session stays pinned" : "The wait is saved";
			setPanel(c, errorPanel(`Not closed: ${blocker.text}. ${kept}; park the session instead.`));
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
		const choice = await askPark(c, canClose, flow.closeNote ?? (prompt ? CLOSE_NOTES.prompt : CLOSE_NOTES.quiet));
		if (!choice) return;
		const kind = flow.kind ? ["--kind", flow.kind] : [];
		const answer = await act(c, [flow.command ?? "add", "--session", session, ...kind, ...options, ...userArgs(sentence, prompt, choice.park)]);
		if (choice.close) await closeSession(c, session, answer, flow.pin === true);
	}

	async function openMenu(c) {
		if (!c.hasUI || menuOpen) return;
		followSession();
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
					? [
							{
								label: `This session's waits (${open.length})`,
								description: `${[...(open.some(onYou) ? ["seen"] : []), "stop", ...(open.some((wait) => wait.retryable) ? ["retry"] : [])].join(", ")} or one more day`,
							},
						]
					: []),
				...MENU.items.filter((item) => first.length === 0 || item !== prItem),
			];
			const chosen = await pick(c, "What does this wait for?", items);
			if (!chosen) return;
			if (!chosen.flow) await waitMenu(c, session, open);
			else if (!chosen.flow.request) await setAside(c, session, chosen.flow, pr, record);
			else {
				const request = await chosen.flow.request(c, record, pr, canClose);
				if (!request) return;
				const answer = await perform(c, request, session);
				// A described check closes once the agent wrote its command (the tool).
				if (request.close && request.described) closeAfterCheck = { session, wait: answer.wait?.id };
				else if (request.close) await closeSession(c, session, answer);
			}
		} catch (error) {
			setPanel(c, errorPanel(messageOf(error)));
		} finally {
			menuOpen = false;
		}
	}

	pi.registerCommand("wait", {
		description: "Set this session aside: /wait opens the menu, or /wait 2d <sentence> [-- prompt] | every 2h -- <prompt> | gh pr|babysit|release | check | for | later | list | seen | stop | help",
		getArgumentCompletions: (prefix: string) => waitCompletions(prefix, waits),
		handler: async (input: string, c) => {
			followSession();
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
			"Kinds: pr for a known PR's review_on_head, ready, merged, released, or activity (the next comment, review, merge or close from someone other than the user); release for a repo's next release; command for anything else code can check; time to retry later; repeat to send prompt on a timer; something for what code can't watch, such as a person's reply, named by what: it never fires and the user stops it; later for when the user has time.",
			"every sends prompt again and again instead of once: kind repeat runs until the user stops it; with kind command, pr or release it ends once that holds; for or times end it after a while or a number of runs; with kind repeat, until is a condition in words that you judge at each run, ending the repeat with end_repeat once it holds. A run due while the last run's turn still goes is skipped.",
			"note is your suggested sentence for the user. The user's own sentence always stands: never try to overwrite it, and don't repeat a note the session already has.",
			"prompt is sent into this session when the wait fires. list shows the open waits, stop ends one by its id.",
			"This tool never parks the session on its own; setting it aside is the user's call. When the user's /wait check asked you for a command, call add with kind command and argv only: it becomes their check.",
		].join("\n"),
		parameters: z.object({
			action: z.enum(["add", "babysit_pr", "list", "stop", "end_repeat"]).describe("What to do"),
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
			wait: z.string().optional().describe("stop, end_repeat: the wait id"),
			reason: z.string().optional().describe("end_repeat: why it ended"),
		}),
		approval: "write",
		loadMode: "essential",
		async execute(_id, params, _signal, _onUpdate, c) {
			if (c.agent?.kind === "sub") throw new Error("waitingfor belongs to the main session; report back to it instead");
			followSession(); // the check the user described may still sit under the id omp moved from
			const session = c.sessionManager.getSessionId();
			let args;
			// The user's described check this call writes; a failed write keeps it for the agent's retry.
			const check = checkWritten(c, params);
			const extra = check ? Object.keys(params).filter((name) => params[name] !== undefined && !["action", "kind", "argv"].includes(name)) : [];
			if (extra.length > 0) {
				throw new Error(`This add writes the user's /wait check, which sets the rest: call it again with action, kind and argv only, without ${extra.join(", ")}. The command must exit 0 when it holds.`);
			}
			switch (params.action) {
				case "add":
					args = check ? ["write-check", "--session", session, `--argv=${JSON.stringify(params.argv)}`] : await toolAddArgs(params, session, c.cwd);
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
				case "stop":
					if (!params.wait) throw new Error("stop needs a wait id; list shows them");
					args = ["stop", "--session", session, `--wait=${params.wait}`];
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
				if (asked.get(session) === check) asked.delete(session);
				if (closeAfterCheck?.session === session && closeAfterCheck.wait === check.wait && session === sessionId) {
					closeAfterCheck = undefined;
					closeAfterTurn = session;
				}
				const seconds = answer.wait.condition.interval_s;
				const [unit, size] = Object.entries(UNIT_SECONDS).findLast(([, length]) => seconds % length === 0);
				if (ctx && session === sessionId) setPanel(ctx, () => `Check: ${params.argv.join(" ")} every ${seconds / size}${unit}`);
			}
			const result = params.action === "list" ? (answer.sessions?.[0] ?? { waits: [] }) : answer;
			return { content: [{ type: "text", text: JSON.stringify(result) }], details: {} };
		},
	});
}

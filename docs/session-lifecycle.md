# Session lifecycle: set aside, wait, come back

You leave a coding-agent session for many reasons. A pull request needs a
review, a deploy has to finish, you want to retry in two days, or the task is
big and needs a free afternoon. When you come back, you need three things
fast: why you left, what happened since, and what to do next.

CodexBar AgentView keeps that for you. Each set-aside session gets your own
sentence, a list of what it waits for, and a history of what happened. Code
watches the waits. An AI model only runs when a wait sends the session a
prompt you wrote, or when you ask the agent to write a check for you.

Most of this works on omp sessions in Tern and needs the omp extension
([install it](../README.md#omp-extension-set-aside-sessions)). Shell panes (a
build, a deploy script) get a shorter set. Plasma and tern-awui show the same
list.

## Set a session aside

Press `Ctrl+Alt+B` or type `/wait` in omp. Both open the same menu:

1. **This session's waits**, when it has some: Done or +1 day for each.
2. What the session waits for. Every item shows the `/wait` command that does
   the same in one line.
3. Your sentence: why you left it, what comes next. It starts with the
   session's current sentence.
4. Optionally, a prompt to send into the session when the wait fires.

Then the pane is parked. Parked cards stay as Tern draws them.

In Tern, `Ctrl+Alt+B` on an omp pane asks that session's omp extension to open
its menu above the composer, so there is one menu, not two. A session started
before the extension was installed or updated needs `/restart` first; until
then Tern shows a toast saying so. On a pane without omp, Tern shows its own
picker with the items that make sense there and no prompt step.

Both menus read their items from `contents/scripts/waiting_menu.json`, so the
labels, the order and the commands always match.

## The `/wait` commands

Type `/wait ` for completions. `/wait help` lists them above the composer.

| Command | Waits for |
|---|---|
| `/wait 2d <sentence> [-- prompt]` | a while: `3h`, `1h30m`, `1w` |
| `/wait 2026-11-07 09:00 <sentence> [-- prompt]` | a date and time, or just `18:00` (the next one) |
| `/wait gh pr [N\|URL] [until] <sentence> [-- prompt]` | a pull request (see below) |
| `/wait gh babysit [N\|URL] [sentence]` | a bot review; then the agent fixes what's real, and the wait follows the PR to ready and merged |
| `/wait gh release [owner/repo] <sentence> [-- prompt]` | a repo's next GitHub release |
| `/wait check [every 10m] <what to check> [-- prompt]` | a condition you describe; the agent writes the command once, code runs it |
| `/wait check [every 10m] ! <command> [-- prompt]` | a command you give, until it exits 0 |
| `/wait for <what>` | anything else, in your words; you mark it done. One per session: it replaces the previous text, and `/wait for` alone removes it |
| `/wait later [S\|M\|L] <sentence>` | the time you have for it; pins the session |
| `/wait list` | this session's details |
| `/wait done [w1]` | close one wait, or all of them |
| `/wait note <text>` | replace your sentence |

The text after a standalone `--` is the prompt sent into the session when the
wait fires. `/wait` answers in the area above the composer, under the
session's row, and the answer goes away with your next input.

Without a number, `gh pr` and `gh babysit` take the PR the agent named last,
else the current branch's PR. `gh release` without a repo takes the current
one.

## What each wait watches

| Wait | How it's checked | Fires when |
|---|---|---|
| Time | the clock | the time comes |
| GitHub PR | `gh`, every 3 minutes | the PR reaches the state you picked |
| Babysit | `gh`, every 3 minutes | each step of the chain is done (below) |
| GitHub release | `gh api …/releases/latest`, hourly | the latest tag changes |
| Command check | the command, every 15 minutes, without a shell | it exits 0 |
| Something else | nothing | you mark it done |
| When I have time | nothing | never; it's a list |
| Job (shell pane) | Tern's command and pane events | the command ends; it can close the pane on exit 0 |

None of these checks spends tokens.

**Pull request states.** `ready` (the default) means the required checks are
green, a review bot finished on the latest push, no bot's check still runs,
and no review thread is open. `review` waits only for the bot review on the
latest push. `merged` and `released` do what they say; `released` compares
the merge commit with the release tags. `activity` fires on the next comment,
review, merge or close from someone other than you, and on a push when the PR
isn't yours. Failed checks, conflicts and a PR closed without merging bring
the wait back to you at once.

The review bots are Greptile, Macroscope, CodeRabbit, Codex, Copilot, Gemini,
Cursor Bugbot, Sourcery and roboomp. Copilot and Codex don't review every
push by default, so `ready` also goes ahead 15 minutes after a push no bot
reviews. A bot check that runs for over an hour is reported once.

**Babysit** is a chain of waits: a bot review on the latest push, then a
prompt that asks the agent to fix the real findings and answer the rest, then
a code check that something happened (a new push, or every thread answered),
then `ready`, then `merged`. It never merges.

**Command check from a description.** `/wait check the nightly job passed`
sends one prompt to the session's agent: write a command that exits 0 exactly
when that holds, run it once, and register it. The extension then adds the
check with your description as its label and parks the session; the command
shows above the composer (`Check: <command> every 15m`) so you can read what
will run. From then on only the command runs. If the agent's turn ends without
registering a check, the area above the composer says so. One described check
can be pending at a time. With `!` you give the command yourself and no agent
is asked.

**Deadlines.** Any wait can have one (from the CLI and the agent's tool).
When it passes, the wait comes back to you as overdue and is still watched.

## Who has the next step

The Waiting tab, Plasma and tern-awui group sessions the same way:

| Group | What's in it |
|---|---|
| On me | a wait fired, ran its prompt, needs attention, or is overdue |
| Machine watching | code is checking something |
| Scheduled | the session has only `/schedule` jobs, which prompt it on their own |
| Waiting on something | something else waits, with your words for it |
| When I have time | sessions you pinned or set aside for later |

A session's group is its most urgent wait's. Sessions with `/schedule` jobs
show them as `↻ every 2h · next 16:00` in their status and card wherever they
are listed.

## Follow-up prompts

When a wait with a prompt fires, the omp extension sends the prompt into the
session, parked or not, after the running turn. A prompt can carry a check (a
command or a PR state) that decides whether the turn did its job. Without one,
the wait comes back to you as "ran, take a look".

A closed session never restarts by itself. Its wait comes back to you with
Resume, which reopens the session in a new Tern tab and then sends the
prompt. A live session without the extension can't take prompts; its wait
comes back to you saying so.

## Coming back

**Waiting tab** (`Ctrl+Alt+W`):

| Key | Does |
|---|---|
| `Space` | details: your sentence, what it waits for, what happened since, the next step, the recap, the session's pull requests and schedules |
| `Enter` | open the session in its own tab, or resume a closed one |
| `Shift+Enter` | the session card |
| `+` | +1 day (below) |
| `F2` | Waiting for…: edit the free-text status |
| `Delete` | Done |

A row shows what the session waits for in your words when it has a free-text
status (`/wait for`), and `F2` or the row's **Waiting for…** button edits it. A
session has one free-text status; setting it again replaces it, and an empty
one removes it.

**+1 day** pushes the row's most pressing wait: the one that's on you, else
the one due first. It always takes the wait off you, and a prompt that wait
had queued is cancelled until it fires again.

| Wait | +1 day |
|---|---|
| Time | moves the time to 24 hours from now; the prompt runs then |
| GitHub PR, release, command check | pauses the checks for 24 hours |
| Something else | reminds you in 24 hours |
| Job that already ended | brings it back in 24 hours |
| Anything overdue | moves the deadline to 24 hours from now |
| When I have time, a chain step not started yet | not offered |

**Done** closes the wait. On a session set aside for later it also unpins it.

**The session card** floats over the pane. It opens by itself the first time
you focus a session whose wait fired, and on demand with `Ctrl+Alt+I`, a click
on the row above the composer or on the status segment, or `Shift+Enter` in
the dashboard. It shows your sentence, the folder, branch and model, the
session's pull requests with their state, its waits and schedules, what
happened since, and the next step. Focus it to use its buttons and links.
Outside Tern, `Ctrl+Alt+I` shows the same details above the composer.

**On the pane** a row above the omp composer says what the session waits for
(`⏳ PR#42 · review on head · 2d · "fix, merge, release"`, starting with `●`
when it's back on you). Tern adds a status line segment and colors the tab
yellow while a session in it is on you. It only colors tabs that have no color
of their own, and only removes the yellow it set.

## When I have time

Every session pinned with omp's `/pin` is listed here, closed ones too, sorted
by the size you gave it (S, M, L). Setting a session aside for later pins it;
Done unpins it. This group never notifies; open it when you have time.

## The agent's tool

The agent can add waits through the `waitingfor` tool, for example right after
it opens a pull request. It never parks the session and never overwrites your
sentence; its suggestion waits for you to accept it.

## Pull requests of a session

The card and the details list the session's PRs: the ones it created
(`gh pr create` or omp's GitHub tool), the ones it waits for, and the last
five mentioned in the conversation, each with its title, state, review and
checks.

## Elsewhere

- **Plasma** shows the waits only with Settings → Agents → **Tern
  integration** on: an hourglass on waiting rows, `●` when one is back on you,
  and an amber count beside the panel dots.
- **tern-awui** shows the same groups on the phone, with +1 day, Done and a
  push notification when a session comes back to you.

## How it works

Everything goes through one program, `contents/scripts/codexbar_waiting.py`.
It is the only writer of `waiting.json` in the state folder and rewrites
`waiting_view.json` after every change; Tern, the omp extension, Plasma and
tern-awui read that view and act only through the CLI. The Tern plugin calls
its `tick` command, which evaluates the due checks and returns what a window
has to do (toast, park, close a pane). Prompts go through the omp extension
inside the session, so they reach parked sessions and never touch your draft.
Pull requests are read through `gh`; nothing ever writes to GitHub.

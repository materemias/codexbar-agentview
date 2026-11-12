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

1. **This session's waits**, when it has some: Done or Snooze 1d for each.
2. What the session waits for. Every item shows the `/wait` command that does
   the same in one line.
3. Your sentence: why you left it, what comes next. It starts with the
   session's current sentence.
4. Optionally, a prompt to send into the session when the wait fires; Repeat
   a prompt needs one.
5. What to do with the session: **Close the session**, **Park it** or **Keep
   it open**. Close is offered only while the agent is idle, and it ends omp
   once the wait is saved; for a check the agent writes, once that turn ends.
   A turn still going by then, retrying, compacting or with a message queued,
   keeps the session open, and the row says so.
   A wait with a prompt reopens the session in Tern when it fires, and a
   repeat for each run (below); one without comes back to the Waiting tab,
   where Resume reopens it. For **When I have time** the pin brings it back
   from that group.

A one-line `/wait` command always parks. Parked cards stay as Tern draws them.

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
| `/wait every 2h [for 1d\|3 times] [until …] [sentence] -- <prompt>` | sends the prompt every 2 hours (below) |
| `/wait 2026-11-07 09:00 <sentence> [-- prompt]` | a date and time, or just `18:00` (the next one) |
| `/wait gh pr [N\|URL] [until] [--] <sentence> [-- prompt]` | a pull request (see below) |
| `/wait gh babysit [N\|URL] [--] [sentence]` | a bot review; then the agent fixes what's real, and the wait follows the PR to ready and merged |
| `/wait gh release [owner/repo] [--] <sentence> [-- prompt]` | a repo's next GitHub release |
| `/wait check [every 10m] <what to check> [-- prompt]` | a condition you describe; the agent writes the command once, code runs it |
| `/wait check [every 10m] ! <command> [-- prompt]` | a command you give, until it exits 0 |
| `/wait for <what>` | anything else, in your words; you mark it done. One per session: it replaces the previous text, and `/wait for` alone removes it |
| `/wait later [S\|M\|L] <sentence>` | the time you have for it; pins the session |
| `/wait list` | this session's details |
| `/wait done [w1]` | close one wait, or all of them |
| `/wait note <text>` | replace your sentence (on a session with waits or pinned) |

The text after a standalone `--` is the prompt sent into the session when the
wait fires. `/wait` answers in the area above the composer, under the
session's row, and the answer goes away with your next input.

Without a number, `gh pr` and `gh babysit` take the PR the agent named last,
else the current branch's PR. `gh release` without a repo takes the current
one. A lone `--` right after the PR, state or repo starts the sentence
(`gh pr -- review the patch`). A first word that could be a repo is checked
with gh and stays in the sentence if GitHub has no such repo. After saving,
the area above the composer shows what the line was read as.

**Repeating a prompt.** `/wait every 2h -- <prompt>` sends the prompt every
2 hours, the first time 2 hours from now, until you press Done. It can stop by
itself:

| Ends | Form |
|---|---|
| after a while, or a number of runs | `for 1d`, `3 times` |
| when the agent says so | `until <in your words>`: each prompt tells the agent the condition, and it ends the repeat with its `waitingfor` tool |
| when a command passes | `until ! <command>`, or `until check <what to check>` for a command the agent writes once |
| when a pull request or release gets there | `until gh pr 42 merged`, `until gh release owner/repo` |

A run that comes due while the previous run's turn is still going is skipped,
and the next one counts from when that turn ends. When the repeat ends, the
session comes back to you with how many runs it made. Repeats replace the
omp-schedule extension; the agent asks for one with its `waitingfor` tool.

## What each wait watches

| Wait | How it's checked | Fires when |
|---|---|---|
| Time | the clock | the time comes |
| Repeat | the clock, and its `until` check if it has one | never by itself: it sends the prompt each interval and ends as above |
| GitHub PR | `gh`, every 3 minutes | the PR reaches the state you picked |
| Babysit | `gh`, every 3 minutes | each step of the chain is done (below) |
| GitHub release | `gh api …/releases/latest`, hourly | the latest tag changes; a repo with no release yet keeps waiting |
| Command check | the command, every 15 minutes, without a shell | it exits 0 |
| Manual wait | nothing | you mark it done |
| When I have time | nothing | never; it's a list |
| Job (shell pane) | Tern's command and pane events | the command ends; it can close the pane on exit 0 |

None of these checks spends tokens.

**Pull request states.** `ready` (the default) means the required checks are
green, a review bot finished on the latest push, no bot's check still runs,
and no review thread is open. CI that hasn't run doesn't count as green: on a
PR from a fork, GitHub holds the CI until a maintainer approves it, and the
wait says so and keeps waiting. `review` waits only for the bot review on the
latest push. `merged` and `released` do what they say; `released` compares
the merge commit with the release tags, and a prerelease that holds it is
only reported once: the wait keeps waiting for a release. `activity` fires on the next comment,
review, merge or close from someone other than you, and on a push when the PR
isn't yours. Failed checks, conflicts and a PR closed without merging bring
the wait back to you at once.

A fulfilled `ready` holds for one push. A new push, a conflict or a branch
behind its base takes it back; a fulfilled `review` is taken back only by a
new push. The wait is watched again, the history says why
(`PR#42 no longer ready: new head 1a2b3c4`), and in a chain the next step
waits for it again, counting its time from when the chain gets back to it.
A fulfilled one on a PR closed without merging comes back to you.

The review bots are Greptile, Macroscope, CodeRabbit, Codex, Copilot, Gemini,
Cursor Bugbot, Sourcery and roboomp. Copilot, Codex and Gemini review a PR once
and skip later pushes, so when only they have reviewed it, `ready` goes ahead
15 minutes after the poller first saw a push none of them reviews. A bot that
reviews every push and reviewed this PR before is waited for on the new push;
after an hour without its review, the wait tells you once. A bot check that
runs for over an hour is reported once.

**Babysit** is a chain of waits: a bot review on the latest push, then a
prompt that asks the agent to fix the real findings and answer the rest, then
a code check that something happened (a new push, or every thread answered),
then `ready`, then `merged`. It never merges. Babysitting a PR again replaces
the session's earlier chain on it, and steps you chained after the old one
move onto the new one; a wait of its own on that PR, such as one for its
release, stays.

**Command check from a description.** `/wait check the nightly job passed`
sends one prompt to the session's agent: write a command that exits 0 exactly
when that holds, run it once, and register it. The extension then adds the
check with your description as its label and parks the session; the command
shows above the composer (`Check: <command> every 15m`) so you can read what
will run. From then on only the command runs. If the agent's turn ends without
registering a check, the area above the composer says so. One described check
can be pending at a time. With `!` you give the command yourself and no agent
is asked.

Command checks capture their directory when the wait is added: an explicit
`add --cwd` overrides the known session project directory; otherwise, the
known session project directory is preferred over the registration directory.
Tern captures the pane's directory. `--cwd` must be an absolute, existing
directory and also applies to release checks and command-based follow-up
verification. A known session directory is retained even if already missing
when registered; the check reports failure rather than using another directory.
Only checks with no known session directory use the registration directory.
Legacy checks with no saved directory require recreation; their project
directory is never guessed.

**Deadlines.** Any wait can have one (from the CLI and the agent's tool).
When it passes, the wait comes back to you as overdue and is still watched.

## Who has the next step

The Waiting tab, Plasma and tern-awui group sessions the same way:

| Group | What's in it |
|---|---|
| On me | a wait was fulfilled, needs you, or is overdue |
| Machine watching | code is checking something: a PR, a release, a command, a job |
| On a timer | only time has to pass: a Retry later wait or a repeating prompt |
| Waiting on something | something else waits, with your words for it |
| When I have time | sessions you pinned or set aside for later |

A session's group is its most urgent wait's. A repeating prompt shows as
`↻ every 2h · next 16:00` in the session's status, row and card.

## When a wait is fulfilled

A wait that happened stays in On me until you mark it Done, and everything
shows it the same way:

| Mark | Means | Color |
|---|---|---|
| ✅ | fulfilled: what you waited for happened (`✅ PR#20 merged`) | green |
| ⚠️ | needs you: a check failed, a PR conflicts or was closed, a prompt couldn't be sent | yellow |
| ⏰ | overdue: its deadline passed, still watched | yellow |
| 🔧 | at work: the agent runs the wait's prompt, such as a babysit's review fixes | |
| ⏲ | waiting for a time | |
| ⏳ | waiting for anything else | |

A session can wait for several things at once, say two babysat PRs and a
release. Each chain of steps, and each wait of its own, is a thread. The row
above the composer, the status segment (`+2`) and the Waiting tab row show
every thread by its current step, the most urgent first:
`⏳ PR#18849 · Ready · 27m · + ⏳ PR#26 review on head`. Next step lists each
thread's next step.

The row above the composer, the Waiting tab row and the status segment start
with the mark. The session card opens with a banner such as
`✅ Fulfilled · PR#20 merged · 2h ago`. While another thread still waits, the
banner drops "Fulfilled" and names it underneath:
`✅ PR#18849 merged · 1h ago`, then `Still waiting for ⏳ PR#18849 released`.
Each wait under "Waiting for" carries its mark, and the fulfilling line in
"Since set aside" is green. In On me, the ⚠️ sessions come first, then ⏰,
then ✅. A tab holding a session that's on you turns green when all of them
are fulfilled and yellow when one needs you.

When a babysat PR is merged early, the steps it skipped (review, ready) are
closed and the merged step fires at once.

## Follow-up prompts

When a wait with a prompt fires, the omp extension sends the prompt into the
session, parked or not, after the running turn. A prompt can carry a check (a
command or a PR state) that decides whether the turn did its job. When the
turn ends, verification is queued for a background tick, not run inside the
extension's turn-finished event. Without a check, the wait comes back to you
as "ran, take a look", unless the agent set a new wait during that turn: then
the new wait is the next step, and the one that ran closes ("ran; superseded by
…" in its history).

A closed session comes back by itself when one of its waits has a prompt to
send: Tern reopens it with `omp --resume` in a new tab of the Tern session it
was last in (or Default), without switching to it, and the prompt goes in once
omp is up. With the dashboard open, Tern waits up to 30 seconds for it to
close. If a Tern window is up for 3 minutes in a row without reopening the
session, or it doesn't start, the wait comes back to you with Resume; while no
window is up, as right after login, the reopen waits for one, and the 3
minutes start again when one comes up. Meanwhile Resume says the
session is reopening. Before opening the tab, Tern takes the session's startup
ownership, the same one History's Launch and Teleport take: it doesn't reopen a
session that runs or is starting, and for a minute after it neither do they,
nor the Waiting tab's Resume. An omp running in the session's folder that no
log or transcript ties to a session may be writing this one: Tern doesn't
reopen it, and the wait comes back to you saying which folder. A session
whose extension was heard from in the last 6 minutes may still be running and
isn't reopened. A wait without a prompt only comes back to you. A live session
without the extension can't take prompts; its wait comes back to you saying
so.

After a reboot, waits pick up where they were once Tern starts at login: a time
that came due while the machine was off fires on the first tick, and
PR, release and command checks poll again. Each closed session with a prompt to
send gets a fresh reopen, even one whose reopen didn't work before the reboot,
and an extension heard from just before it doesn't hold the reopen back. A
prompt whose turn the reboot cut off is sent again once, led by a note asking
the agent to check what was already done; cut off a second time, it comes to
you. A job wait on a shell pane comes back to you, since the pane is gone.

## Coming back

**Waiting tab** (`Ctrl+Alt+W`):

| Key | Does |
|---|---|
| `Space` | details: your sentence, what it waits for, what happened since, the next step, the recap and the session's pull requests |
| `Enter` | open the session in its own tab, or resume a closed one |
| `Shift+Enter` | the session card |
| `+` | Snooze 1d (below) |
| `F2` | Manual wait…: edit the free-text wait |
| `F5` | Retry a wait its failing checks stopped (when shown) |
| `Delete` | Done |

A row shows what the session waits for in your words when it has a manual wait
(`/wait for`), and `F2` or the row's **Manual wait…** button edits it. A
session has one manual wait; setting it again replaces it, and an empty text
removes it.

**Snooze 1d** pushes the row's most pressing wait: the one that's on you, else
the one due first. It always takes the wait off you, and a prompt that wait
had queued is cancelled until it fires again.

| Wait | Snooze 1d |
|---|---|
| Time | moves the time to 24 hours from now; the prompt runs then |
| Repeat | moves the next run to 24 hours from now; a run already going finishes |
| GitHub PR, release, command check still waiting | pauses the checks for 24 hours |
| Manual wait | reminds you in 24 hours |
| Anything that already happened: a job that ended, a fulfilled PR or check, a fired chain step | hides it for 24 hours (⏲, On a timer), then brings it back as it was; its prompt doesn't run again |
| Anything overdue | moves the deadline to 24 hours from now |
| When I have time, a chain step not started yet, a job still running, a wait whose prompt the agent has | not offered |

**Done** on a row or card closes what's on you and leaves the waits still
watched; with nothing on you, it closes every wait. On a session set aside for
later, closing the last wait also unpins it.

**The session card** floats over the pane. It opens by itself the first time
you focus a session whose wait fired, and on demand with `Ctrl+Alt+I`, a click
on the row above the composer or on the status segment, or `Shift+Enter` in
the dashboard. It shows your sentence, the folder, branch and model, the
session's pull requests with their state, its waits, what happened since you
set it aside (each line with its time), the next step, and omp's latest recap
of the session with the time omp wrote it. A recap
written before the session's later turns is headed "Earlier recap" and
dimmed. Focus the card to use its buttons and links. An open card keeps itself
current: it redraws as soon as the session's waits change, rereads times, agent
state and the last exchange every minute, and its pull request states every
10 minutes.
Outside Tern, `Ctrl+Alt+I` shows the same details above the composer.

**On the pane** a row above the omp composer says what the session waits for
(`⏳ PR#42 · review on head · 2d · "fix, merge, release"`, with the marks
above). Tern adds a status line segment and colors the tab green or yellow
while a session in it is on you. It only colors tabs that have no color of
their own, and only removes the colors it set.

## When I have time

Every session pinned with omp's `/pin` is listed here, closed ones too, sorted
by the size you gave it (S, M, L). Setting a session aside for later pins it;
Done unpins it. This group never notifies; open it when you have time.

## The agent's tool

The agent can add waits through the `waitingfor` tool, for example right after
it opens a pull request. It never parks the session and never overwrites your
sentence; its suggestion waits for you to accept it.

## Pull requests of a session

The card and the details list the session's PRs under **Pull requests**: the
ones it waits on (marked "waiting on") and the ones it created (marked
"created", by `gh pr create` or omp's GitHub tool). The last five PRs mentioned
in the conversation follow under **Mentioned**. Each shows its title, state,
review and checks; statuses refresh every 10 minutes, but a PR that the
session's own wait saw merged reads merged at once.

## Elsewhere

- **Plasma** shows the waits only with Settings → Agents → **Tern
  integration** on: an hourglass on waiting rows, `●` when one is back on you,
  and an amber count beside the panel dots.
- **tern-awui** shows the same groups on the phone, with Snooze 1d, Done and a
  push notification when a session comes back to you.

## When waits can't be checked

Every surface says so when checking stops, instead of showing a list that has
quietly frozen. The Waiting tab, the row above the omp composer, `/wait list`
and the Plasma Agents section show one line:

| Line | Means |
|---|---|
| ⚠ Waits not checked since 14:20: … | the last check failed, with its error |
| ⚠ Waits not checked since 14:20 | nothing has checked the waits for 15 minutes (Tern isn't running, say) |
| ⚠ Waits not checked since never | no check has run yet, 15 minutes after this surface first showed the waits |
| ⚠ Session scan stale since 14:20 | the session list is older than 2 minutes or from before a reboot |
| ⚠ Last check: … | the last check ran, recently, but couldn't do part of its work (a session it can't show, a PR whose checks it can't read) |

Except for "Last check", the panel's back-on-you count meanwhile reads `2?`,
dimmed, or `0?`; a "Last check" line with a stale session scan says so at its
end and marks the count too. A Tern
daemon that isn't the one checking (a second daemon on the same state) says
"Another Tern daemon checks these waits" and leaves checks, actions and pane
events to the other one.

One broken session doesn't stop the rest: its waits come to you as
"internal error: …", once.

A wait whose checks fail is still watched:

- **GitHub or the network down:** the checks keep retrying. After an hour the
  wait shows "GitHub unreachable since 14:20" (or "`<command>` failing since
  14:20") and logs one line; "Checks work again" when they do.
- **Real errors** (an expired `gh` login, a command that can't run): after five
  in a row the wait comes to you. **Retry** (`F5` on the Waiting tab, on the
  card, in the omp menu, or `retry --wait`) watches it again, and it is retried
  by itself once an hour. Retry also puts back a wait stopped by an internal
  error.

Other things that used to stall:

- A prompt that reached the session without a turn running, as after an omp
  crash, settles about 2 minutes later instead of staying "agent on it".
- After sleep or a reboot, "omp extension not loaded" waits until the
  extension has been silent for 15 minutes and the prompt queued for 6,
  counted from the wake.
- While the session scan is stale nothing is flagged and no session is
  reopened; Resume still works, guarded by the session's startup lock.
- With no Tern window, a reopen comes to you after 3 minutes as "open a Tern
  window to reopen it" and runs when one opens. More than three toasts nobody
  saw for an hour fold into one.
- A job wait whose pane disappeared without an event ends as "pane closed"
  within about 10 seconds; one from a previous Tern daemon ends too.
- A check time a clock jump pushed far ahead comes back to its interval, and
  Tern schedules on a clock that doesn't jump.
- Ended waits older than a week, unused PR cache rows and old startup locks are
  cleaned up.

## How it works

Everything goes through one program, `contents/scripts/codexbar_waiting.py`.
It is the only writer of `waiting.json` in the state folder and rewrites
`waiting_view.json` after every change; Tern, the omp extension, Plasma and
tern-awui read that view and act only through the CLI. The Tern plugin calls
its `tick` command, which evaluates due checks and queued follow-up
verifications, then returns what a window has to do (toast, park, close a
pane). The extension's turn-finished event only queues verification. Prompts
go through the omp extension inside the session, so they reach parked sessions
and never touch your draft.
Pull requests are read through `gh`; nothing ever writes to GitHub.

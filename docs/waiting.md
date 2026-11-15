# Set sessions aside

You leave coding-agent sessions all the time. A pull request needs a review, a
deploy has to finish, a colleague owes you an answer, or the task is big and
deserves a free afternoon. A day later you're back at a pane with no idea why
you stopped or what was supposed to happen next.

Setting a session aside fixes that. You write one sentence about why you left,
say what the session waits for, and optionally give it a prompt to run when the
wait is over. Code watches the wait, not a model, so no tokens are spent until
something happens. When it does, the session gets your prompt (Tern reopens it
if you closed it), and a card tells you what happened while you were away.

You don't have to learn any of this to use it. The agent has the same toolkit
as a tool, so "babysit the PR you just opened" or "check every morning whether
the nightly build passed" is enough. See [Ask the agent](#ask-the-agent).

- [Use cases](#use-cases)
- [Ask the agent](#ask-the-agent)
- [Where it works](#where-it-works)
- [What a session can wait for](#what-a-session-can-wait-for)
- [When a wait fires](#when-a-wait-fires)
- [Who has the next step](#who-has-the-next-step)
- [Seen, Stop and Snooze](#seen-stop-and-snooze)
- [Closing, parking and reboots](#closing-parking-and-reboots)
- [Coming back](#coming-back)
- [Keyboard shortcuts](#keyboard-shortcuts)

## Use cases

Each of these used to mean keeping a tab open and remembering to look. Now it's
one line, or one sentence to the agent.

| Situation | Set it aside with |
|---|---|
| You opened a PR and a review bot will comment in ten minutes. You want the real findings fixed and the PR taken to merge. | `/wait gh babysit` |
| The PR is approved but not merged. Once it is, the release notes need writing. | `/wait gh pr 42 merged -- write the release notes` |
| You're blocked on a fix in a library you don't own. | `/wait gh release vercel/next.js bump once their fix ships -- upgrade and run the tests` |
| The migration fails because of an outage upstream. Try again in two days. | `/wait 2d upstream fix should be out by then -- retry the migration` |
| A deploy or a long job runs on another machine. | `/wait check every 10m ! curl -fsS https://staging.example.com/health -- run the smoke tests` |
| You don't know the exact command, only what should be true. | `/wait check the nightly backup finished -- summarise its log` |
| A flaky test needs watching for a week. | `/wait every 1d for 1w -- run the e2e suite three times and list the flaky tests` |
| Keep a PR rebased until someone merges it. | `/wait every 2h until gh pr 42 merged -- rebase on main if the PR is behind` |
| Anna owes you an answer on the schema. | `/wait for Anna's answer on the schema` |
| A big rewrite you only want to touch on a quiet afternoon. | `/wait later L rewrite the sync engine` |
| You're away for three days and don't want the session in sight. | `/wait snooze 3d after the offsite` |
| A build runs in a shell pane, and you want to know when it ends. | `Ctrl+Alt+B` on the pane, then **Job finishes** |
| CI on main is red, and you want to hear when it's green, or by Friday either way. | ask the agent (below) |

Several waits can share a session: two babysat PRs and a release, say. Any wait
can have a deadline, and the agent can chain them so that one step starts only
after the previous one fired: wait for the merge, then for the release, then
upgrade.

## Ask the agent

The agent can set all of this up through its `waitingfor` tool. Say what you
want the way you'd say it to a colleague:

> Open the PR and babysit it.

> Let me know when the CI on main is green again. If it isn't by Friday, tell
> me anyway.

> Check once a week for the next month whether the restore test still passes.
> If it fails, find out why.

> Every morning, look at yesterday's error log and summarise anything new.
> Stop at the end of the month.

> Ping me when someone comments on #88.

> Retry this in two days. If it still fails, write it up.

> Park this session, I'll come back to it next week.

It picks the kind of wait, the interval, how it ends, the deadline and the
prompt, and it can park the session for you. When it opens a pull request, it
usually sets a wait on it right away. Its waits show up everywhere yours do,
and you stop them the same way.

Some things it leaves to you on purpose:

- It never closes a session. It can only park it, which is always safe.
- It never overwrites your sentence. It can suggest one, and the suggestion
  waits for you to accept it.
- It writes to GitHub only when a prompt asks it to, as babysit's fix step
  does. The code that watches waits only reads GitHub, and babysit never
  merges.

## Where it works

The waits live in one place on your machine, and every surface shows the same
ones. Tern's session daemon runs the checks in the background, so Tern has to
be running even when you work in another terminal; with its Start at login
setting on, it always is.

| | omp in Tern | omp in another terminal | Claude Code, Codex or a shell in Tern |
|---|:---:|:---:|:---:|
| Set aside with `/wait`, `Ctrl+Alt+B` or the agent's tool | yes | yes | `Ctrl+Alt+B` only |
| Follow-up prompts | yes | yes | no |
| Row above the composer saying what it waits for | strip in the composer's style | text row | no |
| Reopen a closed session when a prompt is due | yes, in a new tab | yes, in a new Tern tab | no |
| Park the session | yes | no pane to park | yes |
| Waiting tab | yes | yes | yes |
| Session card | floats over the pane | details above the composer (`Ctrl+Alt+I`) | floats over the pane |
| Job waits on a shell command | | | yes |

omp works on its own, in kitty or any terminal, as long as Tern runs in the
background, but it works best in Tern. There the session can be parked
instead of closed, the card floats over the pane, the row above the composer
looks like part of it, and the tab changes color when something is back on
you. On a pane without omp, `Ctrl+Alt+B` offers a shorter menu with no prompt
step, and the waits belong to the pane: closing it ends them.

Without Tern running at all, you can still set waits and see them in omp, but
nothing checks them, and every surface says so.

It needs the [Tern extension](../README.md#install) and, for `/wait`, the row
and the agent's tool, the
[omp extension](../README.md#omp-extension-set-aside-sessions). Pull request
and release waits need `gh`, logged in.

## What a session can wait for

### A pull request, babysat

`/wait gh babysit` (or "babysit it" to the agent) follows a PR to the end. It
waits until a review bot has looked at the latest push, then asks the agent to
fix what's real in the findings and answer the rest. Code then checks that the
agent pushed or answered every thread, waits for the PR to be ready (checks
green, no open thread), and finally for the merge. A failed check, a conflict
or a closed PR comes straight back to you. If the PR is merged early, the steps
it skipped close and the merge step fires at once.

The review bots it knows are Greptile, Macroscope, CodeRabbit, Codex, Copilot,
Gemini, Cursor Bugbot, Sourcery and roboomp. Without a number, it takes the PR
the agent named last, else the current branch's.

### A pull request or a release

`/wait gh pr 42 merged ship the docs with it` waits for one state of a PR,
then your sentence:

| State | Fires when |
|---|---|
| `ready` (the default) | checks are green and no thread is open |
| `review` | a review bot finished on the latest push |
| `merged` | the PR is merged |
| `released` | a release contains the merge; a prerelease doesn't count |
| `activity` | someone other than you comments, reviews, pushes, merges or closes it |

`/wait gh release owner/repo blocked on their fix` waits for the repo's next
release. Give either one a prompt after `--` and the session picks the work up
by itself.

### A time

The time comes first, then your sentence: `/wait 3h quota resets by then`. It
takes a while (`2d`, `3h`, `1h30m`), a clock time (`18:00`, the next one) or a
date (`2026-11-20 09:00`). Use it for "retry in two days", "after the weekend"
or "once the quota resets". With a prompt the agent starts on its own; without
one, the session comes back to you.

### A condition

`/wait check` with a description asks the agent to write a command that tests
it, once. From then on code runs that command, every 15 minutes unless you say
`every 5m`, until it succeeds. The row above the composer shows the command
before anything relies on it. With `!` you give the command yourself:

```text
/wait check the nightly backup finished
/wait check every 5m ! test -f /srv/export/done.flag
```

Deploys, long jobs on another machine, a file appearing, an endpoint coming
back: anything a command can tell.

### A repeat

`/wait every` sends a prompt again and again, the first time one interval from
now. It stops when you stop it, or by itself:

| Ends | Form |
|---|---|
| after a while, or a number of runs | `for 1w`, `5 times` |
| when the agent says so | `until <in your words>` |
| when a command passes | `until ! <command>`, or `until check <what>` for one the agent writes |
| when a PR or release gets there | `until gh pr 42 merged`, `until gh release owner/repo` |

```text
/wait every 2h until gh pr 42 merged -- rebase on main if the PR is behind
/wait every 6h until the error rate is back under 1% -- look at the dashboard and report
```

With `until <in your words>`, each run tells the agent the condition and it ends
the repeat once that holds. When a repeat ends, the session comes back to you
with how many runs it made.

A repeat counts from when it starts, not from the calendar. Each run comes an
interval after the last one started, so `every 1w` set on a Saturday morning
runs on Saturday mornings. There is no "every Saturday at 10:00": a run due
while the machine sleeps comes when it wakes, and the later runs move with it.
A run that comes due while the last one is still going is skipped, so a slow
run never piles up.

### A person, or anything else

`/wait for Anna's answer on the schema` records what you wait for in your own
words. Code can't read your inbox, so nothing watches it. The session sits
under "Waiting on something" with your words on it, and you stop the wait when
the answer arrives. A session has one such wait; `/wait for` again replaces the
text.

### When you have time

`/wait later L rewrite the sync engine` pins the session with a size (S, M or
L) and lists it under "When I have time". It never notifies you. Open the group
when you have an afternoon and pick by size. Sessions you pinned with omp's
`/pin` show there too.

### A job in a shell pane

In a shell pane running a build, a deploy script or a long test run,
`Ctrl+Alt+B` sets a job wait. It fires when the command ends and can close the
pane when the command succeeds.

## When a wait fires

With a prompt, the omp extension sends it into the session after the turn that
is running, parked or not. If you closed the session, Tern reopens it with
`omp --resume` in a new tab, without switching to it, and the prompt goes in
once omp is up.

When the agent's turn ends, the wait comes to you as "ran, take a look", and
the card shows the start of the agent's reply so you can read the result. Two
things change that:

- A prompt can carry a check, a command or a PR state that decides whether the
  turn did its job. Then the wait closes by itself when the check passes and
  comes to you when it fails.
- If the agent set a new wait during that turn, the new wait is the next step
  and the one that ran closes.

Without a prompt, a fired wait only comes back to you.

## Who has the next step

The Waiting tab, Plasma and tern-awui group set-aside sessions the same way,
by their most urgent wait:

| Group | What's in it |
|---|---|
| On me | a wait was fulfilled, needs you, or is overdue |
| Machine watching | code checks something: a PR, a release, a command, a job |
| On a timer | only time has to pass: a retry later, a repeat, a snooze |
| Waiting on something | your own words for it, such as a person's answer |
| When I have time | sessions you pinned or set aside for later |

Every surface marks a wait the same way:

| Mark | Means |
|---|---|
| ✅ | fulfilled: what you waited for happened (`✅ PR#20 merged`) |
| ⚠️ | needs you: a check failed, a PR conflicts or was closed, a prompt couldn't be sent |
| ⏰ | overdue: its deadline passed, still watched |
| 🔧 | at work: the agent runs the wait's prompt |
| ↻ | a repeat, with its next run (`↻ every 2h · next 16:00`) |
| ⏲ | waiting for a time |
| ⏳ | waiting for anything else |

## Seen, Stop and Snooze

A wait ends by hand in one of two ways, and each button means one thing:

- **Seen** says you've read what came. It appears only when something is on
  you, and closes exactly those waits.
- **Stop** ends a wait that's still watched: `×` beside one wait, or
  **Stop N waits…** for all of them after a second press. Stopping a step also
  stops the steps chained behind it.

Two kinds of snooze, for two different things:

- **Snooze 1d** is "not now, remind me tomorrow" for a wait that's on you. It
  hides that wait for a day and brings it back as it was. An overdue wait gets
  its deadline moved a day instead. A wait the machine still watches isn't on
  you, so it has no Snooze 1d.
- **Snooze session** takes the whole session off the screen until a time and
  brings it back by itself: `/wait snooze 2d`, or **Snooze session…** in the
  `Ctrl+Alt+B` menu and on the Waiting tab, which asks for the time and whether
  to park the session or close it. Its other waits keep running meanwhile. When
  it's back you get a "Back from snooze" toast, and Snooze 1d on it sends it
  away for another day. Opening it earlier ends the snooze.

## Closing, parking and reboots

You can usually close a session after setting it aside. Every surface says
whether that's safe, with the same words:

| Says | Means |
|---|---|
| `closable` | every wait still to come has a prompt, so Tern reopens the session when one fires |
| `closable · you reopen` | something still to come has no prompt: it comes back to you, and you resume it |
| `park only: …` | closing would lose something, and the reason follows: a job runs in the pane, the agent is mid-turn, there's no transcript to resume from, or nothing is checking the waits |

Parking only takes the pane out of its tab, so it's always safe.

Waits survive restarts. After a reboot they pick up once Tern starts: a time
that passed fires, checks poll again, and a prompt whose turn the reboot cut
off is sent once more with a note to check what was already done. If nothing
has checked the waits for 15 minutes (Tern isn't running, say), every surface
shows `Waits not checked since …`.

## Coming back

- **The session card** floats over the pane. It opens by itself the first time
  you focus a session whose wait fired, and on demand with `Ctrl+Alt+I` or a
  click on the row above the composer. It shows your sentence, what the
  session waits for, what happened since you left it with times, the next
  step, the agent's latest recap and the session's pull requests with their
  state. Its buttons do Seen, Stop, Snooze and Retry.
- **The Waiting tab** (`Ctrl+Alt+W`) lists every set-aside session by group.
  Each row says whether the session is `running`, `parked`, `closed` or
  `elsewhere`, and typing filters by title, sentence, project, wait or state.
- **Above the omp composer**, a strip shows the mark, your sentence and what
  it waits for, and ends with whether it's safe to close. In Tern it's tinted
  green when something came back and amber when something needs you.
- **Tern's tab** turns green when a session in it is done and yellow when one
  needs you, and the **Back on me** widget counts them.
- **The Plasma widget's Agents list**, with Settings → Agents → **Tern
  integration** on, gives waiting sessions an hourglass, a `●` when one is back
  on you, and an amber count beside the panel dots.
- **On your phone**, [tern-awui](https://github.com/materemias/tern-awui)
  shows the same groups and sends a push notification when a session comes
  back to you. You can snooze, mark Seen or stop a wait from there.

## Keyboard shortcuts

### Anywhere in Tern

| Key | Does |
|---|---|
| `Ctrl+Alt+B` | Set the focused session aside. On an omp pane it opens omp's own menu above the composer, which starts with the session's current waits (Seen, Stop or Snooze 1d for each); on other panes, Tern's shorter menu |
| `Ctrl+Alt+W` | Open or close the Waiting tab |
| `Ctrl+Alt+I` | Open or close the focused session's card |
| `Ctrl+Alt+A` / `Ctrl+Alt+U` / `Ctrl+Alt+H` | The dashboard's Agents, Usage and History tabs |

### In omp

| Key or command | Does |
|---|---|
| `/wait ` | Completions for every form as you type; `/wait help` lists them |
| `/wait …` | Set a wait in one line (see the use cases above); in Tern it also parks the session |
| `/wait list` | This session's details above the composer |
| `/wait seen`, `/wait stop <id\|all>` | Seen, or stop one wait or all of them |
| `/wait note <text>` | Replace your sentence |
| `Ctrl+Alt+B` | The set-aside menu, outside Tern too |
| `Ctrl+Alt+I` | Outside Tern: this session's details above the composer |

A menu item always shows the `/wait` line that does the same thing, so the
menu teaches the short form.

### On the Waiting tab

| Key | Does |
|---|---|
| `↑` / `↓` | Select a session |
| Typing | Filter; `parked` or `closed` lists those |
| `Space` | Details: your sentence, the waits, what happened since, the next step, the recap |
| `Enter` | Open the session in its own tab, or resume a closed one |
| `Shift+Enter` | The session card |
| `Delete` | Seen, when something is on you |
| `+` | Snooze 1d the wait that's on you |
| `F4` | Snooze session… |
| `F2` | Edit what it waits for, in your words |
| `F5` | Retry a wait whose checks kept failing (when shown) |
| `←` / `→` | Other dashboard tabs |
| `Esc` | Clear the filter, then close |

## Reference

[Session lifecycle](session-lifecycle.md) has every command and option, how
each wait is checked, and what happens when checks fail.

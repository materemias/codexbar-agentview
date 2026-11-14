# Set sessions aside

You leave coding-agent sessions all the time. A pull request needs a review, a
deploy has to finish, a colleague owes you an answer, or the task is big and
deserves a free afternoon. A day later you're back at a pane with no idea why
you stopped or what was supposed to happen next.

Setting a session aside fixes that. You write one sentence about why you
left, say what the session waits for, and optionally give it a prompt to run
when the wait is over. Code watches the wait, not a model: no tokens are spent
until something happens. When it does, the session gets your prompt (Tern
reopens it if you closed it), and you get a card saying what happened while
you were away.

It needs the [Tern extension](../README.md#install) and, for the
`/wait` command and the agent's tool, the
[omp extension](../README.md#omp-extension-set-aside-sessions).

## Examples

Typed in omp, one line each:

```text
/wait gh babysit
/wait gh pr 42 merged wait for Péter's approval -- write the release notes
/wait 2d upstream fix should be out by then -- retry the migration
/wait check the nightly backup finished -- summarise its log
/wait check every 10m ! curl -fsS https://staging.example.com/health -- run the smoke tests
/wait every 1d for 1w -- run the e2e suite three times and list the flaky tests
/wait gh release vercel/next.js bump once their fix ships -- upgrade and run the tests
/wait for Anna's answer on the schema
/wait later L rewrite the sync engine
```

Or asked of the agent, in your own words:

> Open the PR and babysit it.

> Check once a week for the next month whether the restore test still passes.
> If it fails, find out why.

> Tell me when the deploy is green. If it isn't by 18:00, tell me anyway.

> Ping me when someone comments on #88.

> Park this session, I'll come back to it next week.

`Ctrl+Alt+B` opens the same choices as a menu, in Tern and in omp, and every
menu item shows the `/wait` line that does the same thing.

## What you can do with it

### Babysit a pull request

`/wait gh babysit` (or "babysit it" to the agent) follows a PR to the end. It
waits until a review bot has looked at the latest push, then asks the agent
to fix what's real in the findings and answer the rest. Code then checks that
the agent pushed or answered every thread, waits for the PR to be ready
(checks green, no open thread), and finally for the merge. It never merges by
itself. A failed check, a conflict or a closed PR comes straight back to you.

The review bots it knows are Greptile, Macroscope, CodeRabbit, Codex,
Copilot, Gemini, Cursor Bugbot, Sourcery and roboomp.

### Wait for a pull request or a release

`/wait gh pr 42 merged` waits for one state of a PR: `ready` (the default),
`review` (a bot finished on the latest push), `merged`, `released` (a release
contains the merge) or `activity` (the next comment, review, push, merge or
close by someone else). `/wait gh release owner/repo` waits for a repo's next
release, which is handy when you're blocked on someone else's fix.

Give it a prompt and the session picks the work up by itself: write the
release notes after the merge, bump the dependency after the release.

### Come back later

`/wait 2d`, `/wait 3h` or `/wait 2026-11-20 09:00` puts the session away for a
while. Use it for "retry in two days", "after the weekend" or "once the
quota resets". With a prompt the agent starts on its own at that time;
without one, the session comes back to you.

### Wait until something is true

`/wait check` with a description of the condition asks the agent to write a
command that tests it, once. From then on code runs that command, every 15
minutes by default, until it succeeds. You see the command above the composer
before anything relies on it. With `!` you give the command yourself:

```text
/wait check the nightly backup finished
/wait check every 5m ! test -f /srv/export/done.flag
```

Deploys, long jobs on another machine, a file appearing, an endpoint coming
back: anything a command can tell.

### Repeat something on a schedule

`/wait every` sends a prompt again and again. It can run until you stop it,
for a while (`for 1w`), a number of times (`5 times`), or until something
holds:

```text
/wait every 2h until gh pr 42 merged -- rebase on main if the PR is behind
/wait every 1d until ! test -f done.flag -- check the import and fix what broke
/wait every 6h until the error rate is back under 1% -- look at the dashboard and report
```

The last form leaves the decision to the agent: each run tells it the
condition, and it ends the repeat once that holds.

A repeat counts from when it starts, not from the calendar. The first run
comes one interval after you set it, and each next one an interval after the
last one started, so `every 1w` set on a Saturday morning runs on Saturday
mornings. There is no "every Saturday at 10:00": a run due while the machine
sleeps comes when it wakes, and the later runs move with it. A run that comes
due while the last one is still going is skipped, so a slow run never piles up.

### Wait for a person

`/wait for Anna's answer on the schema` records what you wait for in your own
words. Nothing watches it, since code can't read your inbox, but the session
sits in the Waiting tab under "Waiting on something" with your words on it,
and you stop the wait when the answer arrives.

### Park big work for when you have time

`/wait later L rewrite the sync engine` pins the session with a size (S, M or
L) and lists it under "When I have time". It never notifies you. Open the
group when you have an afternoon and pick by size.

### Watch a job in a shell pane

In a shell pane running a build, a deploy script or a long test run,
`Ctrl+Alt+B` sets a job wait: it fires when the command ends and can close
the pane when the command succeeds.

### Combine them

A session can wait for several things at once, such as two babysat PRs and a
release, and any wait can have a deadline ("tell me if nothing happened by
Friday"). The agent can also chain waits, so a step starts only after the
previous one fired: wait for the merge, then for the release, then upgrade.

## Ask the agent

The agent has the same toolkit through its `waitingfor` tool, so you rarely
need the exact syntax. Say what you want the way you'd say it to a colleague:

- "Babysit the PR you just opened."
- "Let me know when the CI on main is green again. If it isn't by Friday, tell
  me anyway."
- "Every morning at 9, check yesterday's error log and summarise anything new.
  Stop at the end of the month."
- "Retry this in two days. If it still fails, write it up."
- "Remind me about this when foo/bar publishes its next release."

It picks the kind of wait, the interval, the end condition, the deadline and
the prompt, and it can park the session for you. Some things it leaves to
you on purpose. It never closes a session and never overwrites your sentence
(its suggestion waits for you to accept it). The code that watches waits
only reads GitHub; the agent writes there only when a prompt asks it to, as
babysit's fix step does. Babysit never merges. When the agent
opens a pull request, it usually sets a wait on it right away.

## Coming back

- **The session card** is a small window that floats over the pane. It opens
  by itself the first time you focus a session whose wait fired, and with
  `Ctrl+Alt+I` any time. It shows your sentence, what the session waits for,
  what happened since you left it (each line with its time), the next step,
  the agent's latest recap and the session's pull requests with their state.
  For a prompt that ran, it shows the start of the agent's reply, so you can
  read the result and press **Seen**.
- **The Waiting tab** (`Ctrl+Alt+W`) lists every set-aside session grouped by
  who has the next step: On me, Machine watching, On a timer, Waiting on
  something, When I have time. `Space` peeks at a session without leaving the
  list, `Enter` opens or resumes it, `+` snoozes it a day.
- **Above the omp composer** a row says what the session waits for and
  whether it's safe to close. Tern colours the tab green when a session in it
  is done and yellow when one needs you, and the **Back on me** widget counts
  them.
- **In the Plasma widget's Agents list**, with Settings → Agents → **Tern
  integration** on, waiting sessions get an hourglass, a `●` when one is back
  on you, and an amber count beside the panel dots.
- **On your phone**, [tern-awui](https://github.com/materemias/tern-awui)
  shows the same groups and sends a push notification when a session comes
  back to you. You can snooze, mark Seen or stop a wait from there.

## Closing, parking and reboots

You can usually close a session after setting it aside. Every surface says
whether that's safe: `closable` means Tern reopens it when a prompt is due,
`closable · you reopen` means it comes back to you with a Resume button, and
`park only: …` says what closing would lose, such as a background job or an
agent mid-turn. Parking only takes the pane out of its tab, so it's always
safe.

Waits survive restarts. After a reboot they pick up once Tern starts: a time
that passed fires, checks poll again, and a prompt whose turn the reboot cut
off is sent once more with a note to check what was already done.

## Reference

[Session lifecycle](session-lifecycle.md) has every command and option, how
each wait is checked, the exact PR states, the marks and groups, Snooze, Seen
and Stop, and what happens when checks fail.

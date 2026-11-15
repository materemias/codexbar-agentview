# Changelog

## Unreleased

### Breaking

- Recreate command waits and command-based follow-up checks that have no saved working directory.
- The Tern extension needs Tern 0.9.18 or later. Usage, agent counts, "on me" and "to restore" left the status line for widgets of their own: place them through palette › Edit widgets….

### Features

- Windows support for the Tern extension, the omp extension and the helpers: state in `%LOCALAPPDATA%\codexbar-agentview`, helpers started with `py -3` (else `python`), Agents limited to sessions in Tern panes, resume tabs in `powershell -NoExit`, typed checks run in PowerShell, and the omp extension installed as a loader file instead of a link. Usage comes from Meridian only there, since the `codexbar` CLI has no Windows build. CI runs the tests on Windows too.
- `/wait every 2h -- <prompt>` repeats a prompt into the session, for a while (`for 1d`, `3 times`) or until the agent says it's done, a command passes or a PR or release gets there; a run due while the last one's turn is still going is skipped. It replaces the omp-schedule extension, whose jobs no longer show in the Waiting tab.
- A closed session whose wait has a prompt to send reopens by itself in a new Tern tab of the Tern session it was last in, without switching to it, and gets the prompt; every set-aside menu offers Close the session while the agent is idle.
- Waits pick up after a reboot: due times fire once Tern starts, closed sessions with a prompt to send reopen, and a prompt whose turn the reboot cut off is sent again once with a note asking the agent to check what was already done.
- Tern widget bar widgets with provider logos: Claude and Codex usage combined across all your accounts (plan-weighted) as a ring, bar or graph of the 5h window, or text, with a hover card listing each window's reset and every account's figures; Claude and Codex weekly widgets graphing the week; a usage widget for each other provider; coding agents; back on me; sessions to restore. The helpers keep a week of usage readings for the graphs, older than a day at one per 30 minutes.
- Filters take several words separated by spaces, in any order: a session matches when each word appears in its title, prompt, folder, provider, model or recent messages, and every matched word is highlighted.
- Set sessions aside: `Ctrl+Alt+B` or `/wait` sets a session aside with your own sentence and what it waits for: a while or a date (`/wait 2d`, `/wait 18:00`), a GitHub pull request's review, readiness, merge, release or next comment from someone else (`/wait gh pr`), a PR the agent looks after through review fixes to merge (`/wait gh babysit`), a repo's next release (`/wait gh release`), a check you describe and the agent turns into a command once (`/wait check`, or `/wait check ! <command>`), anything else in your words (`/wait for`), a shell job, or "when I have time" (`/wait later`). The menu asks last whether to park the pane, and for "when I have time" also offers to close the session, which stays pinned; a one-line `/wait` always parks. When the wait fires, the session gets its optional follow-up prompt, parked or not, and comes back to you. Review waits know Greptile, Macroscope, CodeRabbit, Codex, Copilot, Gemini, Cursor Bugbot, Sourcery and roboomp. A PR isn't ready while its CI waits for a maintainer's approval or a bot that reviews every push hasn't reviewed the latest one, and a ready or reviewed PR that gets a new push or a conflict is watched again. [Session lifecycle](docs/session-lifecycle.md) explains it all.
- A Waiting tab in the Tern dashboard (`Ctrl+Alt+W`) groups set-aside sessions by who has the next step (on me, machine watching, on a timer, waiting on something, when I have time), each row led by its project, with what happened since, the next step, the session's pull requests, its free-text "waiting for" status and an action to edit it, Open (in its own tab) or Resume, Snooze 1d and Done. "When I have time" pins the session in omp and lists every pinned one, closed ones included; Done unpins it. A fulfilled wait shows ✅ and green everywhere (row, card banner, status, tab), one that needs you ⚠️ and yellow, an overdue one ⏰.
- An omp extension adds `/wait` (one set-aside menu for omp sessions, opened by `Ctrl+Alt+B` in Tern and outside it and built from the same list as Tern's picker for other panes, one-line commands with completions, and `/wait help` and `/wait list` above the composer instead of in toasts), a `waitingfor` agent tool and a row above the composer showing what the session waits for; install it with the Plasma Install button, the Tern palette command `Install omp extension` or `install_integration.py --omp-extension`.
- A session card floats over any omp session in Tern (`Ctrl+Alt+I`, a click on the row above the composer or on the status segment, or the Card buttons, `Shift+Enter`) with your sentence, folder, branch, model, the pull requests it waits on or created and, apart, the ones only mentioned, waits, what happened since you set it aside with times, and omp's recap with its time ("Earlier recap" when the session moved on since); it opens by itself the first time you focus a session whose wait fired. The row above the composer underlines under the pointer. Outside Tern, `Ctrl+Alt+I` shows the same details above the composer. A status line segment and a colored tab show sessions back on you.
- The Plasma widget's new Settings → Agents → Tern integration switch marks set-aside sessions in Agents and counts the ones back on you beside the panel dots.
- `Ctrl+Alt+H` toggles the Tern dashboard on History.
- Waits say when they can't be checked: the Waiting tab, the row above the omp composer, `/wait list` and the Plasma Agents section show "⚠ Waits not checked since HH:MM" (with the error when a check failed) or "⚠ Session scan stale since HH:MM", and the panel's back-on-you count reads `N?`. A recent check that couldn't do part of its work shows "⚠ Last check: …" and leaves the count plain. A GitHub or check outage of an hour shows "GitHub unreachable since HH:MM" on the wait.
- Retry (`F5` on the Waiting tab, on the card, in the omp menu, `retry --wait`) watches a wait its failing checks or an internal error stopped; such waits are also retried every hour.
- Each Waiting tab row says whether its session is running, parked or closed in Tern (or runs elsewhere), beside its time; typing `parked` in the filter lists the parked ones.
- Every place that shows a set-aside session says whether it is safe to close: `closable` (a prompt reopens it, or every wait is already on you), `closable · you reopen` (it comes back to you) or `park only: …` with the reason (a job running in the pane, a turn already cut off once, nothing to resume from, or nothing checking the waits). It leads the row above the omp composer, marks each Waiting row (`✓`, `↺`, `⏸`) and its card, and shows in `/wait list`, the Plasma tooltip and tern-awui; the menu offers Close the session only when it isn't park only.
- Closing a session while a prompt's turn runs no longer loses the prompt: like after a reboot, Tern reopens the session and sends it again once, asking the agent to check what was already done. A repeat's run, the last one included, is sent again the same way. A check the agent is to write is sent the same way, so the session can be closed before the agent wrote it.
- The Tern dashboard's buttons no longer show their keys; the footer lists them.
- Done is split in two, so it no longer ends every wait when nothing is on you: **Seen** (`Delete`, `/wait seen`) closes only what came to you and is offered only then, and **Stop** ends watched waits, one with `×` on the card or `/wait stop w1`, all with `Stop N waits…` and a second press or `/wait stop all`. Stopping a wait never starts the step chained after it; the steps behind it stop too. The card lists what's on you under **On you**, with the start of the agent's report for "ran, take a look", and "Since set aside" records each `Seen:` and `Stopped:`. The agent tool's `done` action is now `stop`.
- A session is `park only` while closing it would kill work: a command the agent started still runs (`park only: work runs in this session`) or the agent is mid-turn on your request (`park only: the agent is working`). The agent can park its own session through the `waitingfor` tool when you ask it to set the session aside.
- The session card and `/wait list` name each wait in its own words first ("Archive sessions indexed in cass"), with the command or time that watches it after it, muted, instead of only `bash -c '…'` or `until Nov 14 08:00`.
- Usage tracking can be turned off (Plasma: **Track usage**; Tern: **Usage tracking**) for anyone without the `codexbar` CLI or who tracks usage elsewhere; off, nothing fetches usage and no meters, cards or alerts show. It stays on by default, and when the CLI is missing the usage area lists what to install instead of a bare error, with a button that turns tracking off.
- Snooze session: `/wait snooze 2d [sentence]`, or **Snooze session…** in the `Ctrl+Alt+B` menu and on the Waiting tab, parks or closes a session until a time and brings it back by itself in a tab of its own, without taking focus, with a "Back from snooze" toast; its other waits keep running meanwhile, and opening it earlier ends the snooze. Snooze 1d on a session that came back sends it away for another day.
- Snooze 1d is offered only on a wait that's on you; waits the machine still watches or that run on a timer no longer have it.
- The row above the omp composer leads with your sentence and ends with whether the session can be closed; in Tern it is a strip as wide as the composer, tinted by what came back, with the close answer as a coloured badge.

### Fixes

- `Ctrl+Alt+I` in Tern opens the same session card as a click on the row above the composer: the session the pane's omp extension reports, not the one an older agents scan still maps to the pane.
- omp: choosing one of the session's waits in the `/wait` menu works again; it failed with "watched is not a function", so Retry, Seen, Stop and Snooze 1d couldn't be reached from there.
- In the Plasma popup, each Claude and Codex account's windows sit indented under a guide line below its name, as in Tern, so they read as parts of the combined pool above them instead of its peers.
- The Tern window works with the helpers again on Tern 0.9.20: wait cards, Seen, Stop, Snooze and Retry, the `Ctrl+Alt+B` menu, pane-closed and agent events, focusing a session and Install omp extension no longer fail with "table has a protected metatable".
- An omp session running outside Tern is no longer offered parking: the `/wait` menu asks only Close the session or Keep it open, and skips the step when Close isn't possible; a one-line `/wait` doesn't queue a park, and every `park only` reason reads `keep open` for such a session.
- Usage alerts no longer pile up: a reset the provider reports a few seconds or minutes apart no longer counts as a new period and repeats the alert, alerts from one refresh arrive as one notification so Plasma doesn't refuse them as too many, a failed one is retried after 10 minutes instead of on every refresh, and a pace projection warns only once a quarter of the window has passed.
- Waits keep going unattended: one broken session no longer stops the others' checks; a prompt that landed without a turn settles after 2 minutes instead of staying "agent on it"; sleep, a reboot or a stale session scan no longer bring false "omp extension not loaded" flags or reopen a running session; a reopen with no Tern window comes to you after 3 minutes and unseen toasts fold into one; job waits whose pane vanished without an event end; a second Tern daemon on the same state stands aside; clock jumps no longer push checks far out; ended waits, PR cache rows, a PR's event memory and startup locks no longer pile up.
- A wait whose prompt ran no longer stays on you when the agent set a new wait in that turn: the new one is the next step and the old one closes. Done on a row or card closes what's on you and keeps the waits still watched; with nothing on you, it closes every wait.
- Snooze 1d on something that already happened (a finished job, a fulfilled PR or check, a fired chain step) hides it for a day and brings it back as it was, without watching its pane again or rerunning its prompt; it is refused where it would change nothing. A fulfilled `review` is taken back only by a new push, a fulfilled wait on a PR closed without merging comes back to you, a re-armed chain step counts its time again, babysitting a PR again keeps steps chained after the old babysit, and a release wait on a repo with no release yet keeps waiting. Snooze 1d on an overdue repeat moves only its deadline and keeps the run that is due, and a repeat that re-arms with its chain gets its full number of runs again.
- Waits after a reboot, an outage or a stuck daemon: a Tern daemon that holds the checks but stopped running them hands them over within 10 awake minutes; omp panes that reported in after a reboot or a daemon restart, and waits or parks set before the first check, are kept; "GitHub unreachable since …" counts from when checks resumed; Retry on a wait stopped by an internal error sends a prompt the agent never got instead of showing "ran, take a look"; a pending reopen expires on time; the folded toast counts waits, not toasts.
- Pull request waits: `ready`'s 15-minute grace counts from when the push was first seen, not the commit date; a PR that already fails a check or conflicts says so on the first poll, and a conflict that survives a push is reported again; a workflow run cancelled for a newer one no longer blocks `ready`; checks and thread replies past the first 100 count; a roboomp review no longer counts twice; reviews seen while GraphQL is rate-limited are kept; transient failures retry after a minute, while a failure naming a repo without quotes (such as "release not found in acme/eof-parser") is reported instead of retried. A prerelease no longer fulfils a release wait; it is reported once and the wait keeps waiting.
- Tern: Resume can't start the same session twice, panes titled `-zsh` (or text starting with `-`) can be set aside, a Waiting row with a headline still lists its other threads, Open says when the pane has closed instead of "in another window", and actions the omp extension queues during a tick no longer wait up to 5 minutes. An automatic reopen cut off by a plugin reload is retried instead of counted as opened, job waits end with their job on a host whose daemon id can't be read, and the usage widgets' "resets in" counts down every minute. A weekly usage graph that doesn't cover the week yet says since when it runs.
- Tern session cards keep their branch between refreshes, a shell pane's card closes when its waits end, and an auto-opened card opens once per firing.
- omp: a prompt omp drops is reported as failed instead of delivered; `/wait check` stays your check after a steering message; `/wait check !cmd` works without a space; a `later` wait no longer offers Snooze 1d; the implicit PR comes from the agent's latest messages and ignores paths like `docs/setup.md#3`; `/wait gh …` stays with the session it was typed in; `/wait every` completes `5 times` after `for`, and its `for` choices and the menu's prefill skip spans the interval would refuse; Close the session waits for a prompt a turn's end just claimed; the waits warning shows once while `/wait list` is open. In `/wait gh …` a lone `--` starts the sentence (`gh pr -- review the patch`), a first word GitHub has no repo for stays in the sentence, and the answer shows how the line was read.
- The Plasma Install button links the omp extension only where omp is set up, instead of creating `~/.omp`; uninstalling reports a link it can't remove instead of stopping with a traceback; a `mimeapps.list` it creates is readable like other config (0644, not 0600).
- A forked omp session reopened with `/restart` is listed under the session it actually continues, not its parent, and the omp extension follows omp when it moves a session to a new file, so the pane no longer serves the other copy's waits and prompts. When omp replaces a session's file, its waits, prompts and pin move to the new id; a pane that only resumed a copy of a session another pane runs doesn't take its prompts.
- Launch resumes an ended codex session while another codex runs in its folder, and isn't refused for a minute after a Tern reopen that never started.
- The Plasma widget keeps every provider current when Tern fetches fewer providers than it shows: usage fetches are claimed per provider, and the forecast refreshes even when another poller fetched.
- The Plasma widget keeps the last waits and panel count when the waits file reads empty or broken, and warns once they are 15 minutes old, instead of showing nothing waiting. A long waits error is cut at the same character as in Tern, never inside an emoji.
- In the 7-day usage graph, data older than a day shows a window reset where it happened instead of up to 30 minutes early, and an unobserved gap is no longer drawn as observed.
- "not started · resets … after first use" shows only for a window measured at 0%, not for one with no reading.
- A session card whose session got one thing but still waits for another, such as a PR merged but not yet released, no longer says ✅ Fulfilled: its banner says what came and what it still waits for, and every wait on the card shows its mark.
- Focus on an agent whose Tern pane is parked brings the pane back in a tab of its own instead of failing with "couldn't focus".
- An open session card keeps itself current: changed waits show at once, times, agent state and the last exchange every minute, pull request states every 10 minutes. It used to show what it read when it opened.
- A wait whose prompt the agent is still running shows 🔧 at work instead of ✅ Fulfilled.
- A session waiting for several things, such as two babysat PRs, shows each of them in its row, status and next step, not only the first.
- Babysitting a PR no longer closes a separate wait on the same PR, such as one for its release.
- Codex and Claude accounts are always listed in alphabetical order by email, in the widget and in Tern, instead of switching places between refreshes.
- In the Tern extension, a slow usage-cache read that finishes late no longer replaces a newer one's result, such as bringing back an account whose login was replaced.
- Focusing a session that runs in T3 Code 0.1 or later opens its thread instead of only raising the window.
- Initials and word starts in a filter match only neighbouring words, so a query no longer matches scattered letters across a long prompt.
- The Tern extension highlights the matched words in each row, and shows the matching prompt and messages under it in the conversation panel's style.
- Filters ignore tool-call summaries such as "bash, eval, read" and match only prompts and message text.
- In the Tern extension, Space types into a filter in progress instead of opening the conversation.
- In the Tern extension, clearing a filter keeps the selected session selected, as in the applet, so Esc then Space peeks at the session the filter found.
- A Claude 5h window that hasn't started since its reset says `not started · resets 5h after first use` instead of leaving its reset line empty.
- Launch and Teleport prevent duplicate starts of the same session.
- Integration installation preserves file associations when an update fails.
- Follow-up verification runs in the background after the agent's turn ends.
- Wait checks use the saved project or pane directory and report a missing directory.
- Plasma and Tern share usage refreshes within the configured interval.
- The installed Plasma package includes the omp extension.
- A session reopened while another pane still ran it no longer splits its waits: once the other pane closes, its waits move to the copy that goes on, and any the copy already set up again are stopped instead of firing twice.

### Docs

- The README leads with the Tern extension; the Plasma widget is the optional extra. A new page, [Set sessions aside](docs/waiting.md), starts with examples and walks through what waits can do: babysitting a PR, waiting for a merge or release, coming back later, waiting on a check, repeating on a schedule, asking the agent to set them up, and where you see them again.

## 0.3.0 (2026-11-02)

### Features

- History lists omp and Claude Code sessions that closed while the widget wasn't watching, or before it was installed, read from their prompt history.

### Fixes

- A single failed Meridian usage poll on a recent reading no longer shows "Refresh failed" on the Claude account; the error appears once Meridian fails again or the reading is 10 minutes old.
- The widget, its agent sweep and the Tern extension use far less CPU: the sweep takes a quarter of the time, the popup redraws only the session rows that changed, and Tern rereads usage only after it changed or once a minute.
- omp sessions that Tern resumed by file path show their own title and state again, instead of another session's from the same folder.
- Launching a closed omp session from History works again in folders where other omp sessions are running; it still refuses a session a running omp has worked in.
- The Tern extension shows omp rotation-mode changes and drops an account whose login changed within 10 seconds, instead of keeping them until its next usage fetch.

## 0.2.0 (2026-10-31)

### Breaking

- The Tern extension needs Tern 0.9.9 or later; update Tern before relinking.

### Features

- Several Claude accounts, one per Meridian profile, shown under one Claude header with per-account tray meters and combined 5h and 7d bars pooled in Pro allowances, like Codex accounts, each switchable on its own. Usage and saved resets come from Meridian when it runs (saved resets need Meridian 1.87 or later), so the widget no longer competes with it for Anthropic's usage rate limit; otherwise saved resets are asked at most once an hour per account.
- The combined Claude and Codex bar stays visible while an account shows last-known usage, greyed with its allowance count and no pace.
- Codex shows Meridian's ChatGPT seats beside the CLI's accounts.
- Claude and Codex seats keep working from Meridian when the `codexbar` CLI is missing; only when neither answers does the widget report both missing, keeping last-known usage.
- Meridian instances that need `MERIDIAN_API_KEY`, or run without profiles, are read too.
- A Claude account's email and plan come from Meridian too, falling back to the login directory when Meridian leaves them out.
- The Claude card shows omp's Meridian rotation mode and marks each account open or closed, like Codex.
- The Tern dashboard is drawn with Tern's native elements: tabs, buttons with key hints, a framed chat view of the conversation, a status bar with Settings, empty and loading states, and the old-session eye inside the filter field.

### Fixes

- The Tern dashboard opens in a window attached to a remote host instead of closing after three seconds with "Could not place CodexBar AgentView dashboard".
- The Codex rotation mode and account markers follow omp's current `sub-rotate` extension instead of a stale file from the retired `codex-rotation` one.

### Docs

- README lists which Meridian versions supply what.

## 0.1.0 (2026-10-26)

### Breaking

- The project is now CodexBar AgentView. Remove the old applet, add `io.github.materemias.codexbaragentview` to the panel, and rerun the integration installer to register the `codexbar-agentview://` handler. State moves to `~/.local/state/codexbar-agentview` on first run.

### Features

- Plasma 6 tray applet showing AI provider usage from the CodexBar CLI.
- Agents view listing running coding-agent sessions, with provider configuration, tray settings, and several backends.
- Searchable conversation previews for agent sessions, with role colors and keyboard navigation.
- Per-account Codex usage meters, grouped under one header with plan-weighted combined bars.
- Virtual desktop badges on agent rows.
- Sessions sort by recent activity, and a finished session pulses.
- Sessions survive a reboot.
- Agent rows show the active model name.
- Codex Reserve quota label.
- Codex reset forecast with alert summaries, shown in local time.
- Saved resets for Codex and Claude shown inline.
- The popup header shows the CodexBar CLI version.
- Usage bars show a pace tick with the projected usage at reset.
- Usage bars and tray rings are colored by pace instead of absolute usage.
- OpenCode Go provider.
- TypeSafe provider (balance only).
- Percent-only tray mode uses a larger font, and tray meters can appear only when over pace.
- omp Codex rotation mode shown above account usage.
- Codex account availability indicators.
- The Codex 5h window ends at an earlier weekly reset.
- History tab with one-click kitty restore, dismiss, Restore all, keyboard navigation, and filter snippets.
- History keeps recently closed sessions, 100 by default and configurable.
- Codex and pi sessions are tracked as reliably as omp ones.
- Desktop alerts for usage, which expire and name the account.
- Account-scoped usage caching.
- The popup can be resized taller.
- 7d and 5h bars show recent consumption over the last 24h and 1h.
- Claude and Codex threads run by T3 Code are listed, and focusing one opens its thread in T3 Code.
- Agent sessions running in Tern are listed, focused, and relaunched.
- A ring marks the session shown in the focused window.
- Filtering shows why each session matched.
- Agent and History rows line up in compact columns.
- The selected session shows its latest recap.
- Teleport an idle kitty session into a Tern tab.
- New card design for the Agents, Usage, and History tabs.
- Native Tern plugin with a dashboard, status line, and palette commands.
- The Tern plugin runs on macOS (alpha).
- A hide-old switch, also on Tab, hides sessions idle for over a day on Plasma and Tern.

### Fixes

- omp processes are detected with pgrep.
- omp and pi worker subprocesses no longer appear as agents.
- omp activity is read from the transcript state.
- Session titles come from the root omp rollout.
- Recent-idle feedback lasts five minutes.
- OpenRouter balance reads the CLI 0.56.3 usage shape.
- The CLI path defaults to `/usr/bin/codexbar`.
- The CLI version badge aligns in the popup header.
- Agent polling is faster and the applet handles helper failures.
- Lapsed Codex reset announcements are dropped.
- Early pace alerts stay quiet.
- Program-driven omp and pi sessions, such as Tern's Carly assistant, are hidden.
- Agents and History lists get their full height on Tern 0.9.
- The Backend, Providers and About settings pages scroll when taller than the dialog.

### Docs

- README opens each interface with an intro film, keeps screenshots folded beneath it, and covers provider authentication, requirements per interface and installation.

### Internal

- Tests run on every push.
- A `vX.Y.Z` tag that passes the tests publishes a GitHub release with that version's changelog section.

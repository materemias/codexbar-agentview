# Changelog

## Unreleased

### Breaking

- Recreate command waits and command-based follow-up checks that have no saved working directory.
- The Tern extension needs Tern 0.9.18 or later. Usage, agent counts, "on me" and "to restore" left the status line for widgets of their own: place them through palette › Edit widgets….

### Features

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

### Fixes

- A wait whose prompt ran no longer stays on you when the agent set a new wait in that turn: the new one is the next step and the old one closes. Done on a row or card closes what's on you and keeps the waits still watched; with nothing on you, it closes every wait.
- Snooze 1d on something that already happened (a finished job, a fulfilled PR or check, a fired chain step) hides it for a day and brings it back as it was, without watching its pane again or rerunning its prompt; it is refused where it would change nothing. A fulfilled `review` is taken back only by a new push, a fulfilled wait on a PR closed without merging comes back to you, a re-armed chain step counts its time again, babysitting a PR again keeps steps chained after the old babysit, and a release wait on a repo with no release yet keeps waiting.
- Pull request waits: `ready`'s 15-minute grace counts from when the push was first seen, not the commit date; a PR that already fails a check or conflicts says so on the first poll, and a conflict that survives a push is reported again; a workflow run cancelled for a newer one no longer blocks `ready`; checks and thread replies past the first 100 count; a roboomp review no longer counts twice; reviews seen while GraphQL is rate-limited are kept; transient failures retry after a minute.
- Tern: Resume can't start the same session twice, panes titled `-zsh` (or text starting with `-`) can be set aside, a Waiting row with a headline still lists its other threads, Open says when the pane has closed instead of "in another window", and actions the omp extension queues during a tick no longer wait up to 5 minutes.
- Tern session cards keep their branch between refreshes, a shell pane's card closes when its waits end, and an auto-opened card opens once per firing.
- omp: a prompt omp drops is reported as failed instead of delivered; `/wait check` stays your check after a steering message; `/wait check !cmd` works without a space; a `later` wait no longer offers Snooze 1d; the implicit PR comes from the agent's latest messages and ignores paths like `docs/setup.md#3`; `/wait gh …` stays with the session it was typed in.
- The Plasma Install button links the omp extension only where omp is set up, instead of creating `~/.omp`; uninstalling reports a link it can't remove instead of stopping with a traceback.
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

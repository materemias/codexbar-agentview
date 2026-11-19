# Changelog

## Unreleased

### Breaking

- The Tern extension needs Tern 0.9.18 or later. Usage, agent counts, "on me" and "to restore" moved from the status line to widgets: add them through palette › Edit widgets….

### Features

- Set a session aside with `Ctrl+Alt+B` or `/wait`: your sentence about why you stopped, and what the session waits for.
- Waits on a time, a pull request's review, readiness, merge or next comment, a repo's release, a command check, a shell job, a person, or "when I have time".
- `/wait gh babysit` takes a pull request through every round of review fixes to merge.
- A babysat pull request that conflicts with its base or falls behind it gets rebased by the agent.
- `/wait every 2h -- <prompt>` repeats a prompt for a while or until a condition holds; it replaces the omp-schedule extension.
- A wait can carry a prompt; when it fires, Tern sends it, reopening a closed session in a new tab.
- Waits survive reboots and closed sessions; a turn cut off by either is sent again once.
- The Waiting tab (`Ctrl+Alt+W`) groups set-aside sessions by who has the next step.
- The session card (`Ctrl+Alt+I`) shows why you left, the waits, what happened since and omp's recap.
- A strip above the omp composer shows what the session waits for and whether it can be closed.
- The omp extension adds `/wait` and the agent's `waitingfor` tool; install it from Plasma, the Tern palette or `install_integration.py --omp-extension`.
- Every set-aside session says whether it is safe to close, and if not, why.
- Seen closes what came back to you; Stop ends waits still being watched.
- Snooze session (`/wait snooze 2d`, `F4`) hides a session until a time, then brings it back.
- Snooze 1d pushes a wait that's on you back by a day.
- Retry (`F5`) restarts a wait whose checks failed; such waits also retry every hour.
- Waits warn when they aren't being checked or GitHub is unreachable.
- Waiting rows show whether the session is running, parked or closed.
- Plasma: Settings → Agents → Tern integration marks set-aside sessions and counts the ones back on you.
- Tern widget bar widgets: combined Claude and Codex usage, weekly graphs, per-provider usage, agents, back on you, and to restore.
- Filters match several words in any order and highlight each one.
- `Ctrl+Alt+H` opens the Tern dashboard on History.
- Usage tracking can be turned off; without the `codexbar` CLI, the usage area says what to install.
- Windows support for the Tern extension, the omp extension and the helpers; usage comes from Meridian only.
- Tern dashboard buttons no longer show their keys; the footer lists them.
- Tern settings open as a dialog over the dashboard, in the style of Tern's and omp's own, instead of a tab.

### Fixes

- The Tern extension works on Tern 0.9.20 again.
- Resume reopens a closed session while a fresh omp runs in the same folder.
- Resume retries for 2 minutes while another omp in the session's folder starts up.
- The session card lists waits in the order they come due.
- An outdated Plasma applet asks to be updated and leaves Tern's newer agent list alone.
- Claude and Codex account windows in the Plasma popup sit indented under their account.
- Usage alerts no longer repeat for one reset or get dropped as too many.
- A Claude 5h window that hasn't started says `not started · resets 5h after first use`.
- Accounts stay in alphabetical order instead of switching places.
- A slow usage read no longer overwrites a newer one in Tern.
- Plasma keeps every provider current when Tern fetches fewer of them.
- Plasma and Tern share usage refreshes within the configured interval.
- A forked omp session reopened with `/restart` is listed under the session it continues.
- Focusing an agent in a parked Tern pane brings the pane back.
- Launch resumes an ended codex session while another codex runs in its folder.
- Launch and Teleport no longer start the same session twice.
- Focusing a T3 Code 0.1 session opens its thread.
- Filter initials match only neighbouring words, and tool-call summaries no longer match.
- In Tern, Space types into a filter instead of opening the conversation, and clearing a filter keeps the selection.
- The integration installer keeps file associations when an update fails.
- History keeps up to 1000 closed sessions whichever of Plasma and Tern scans.
- The agent list keeps updating past odd process names and deeply nested transcript lines.
- A failed teleport leaves the kitty window open and prints the resume command.
- The installer leaves your own Claude Code hooks alone.
- Only one poller fetches a provider at a time, however long the fetch runs.
- Usage graphs survive a clock correction.
- A `ready` wait waits while the PR is a draft and ends quietly once it merged.
- A release wait finds a release whose tag GitHub hadn't compared yet.
- `/wait` refuses a date followed by an unreadable time such as `10:30am`.
- Panel and popup text shows as plain text, never as HTML.
- Two processes on one session list the exact owner, and the other as untracked.
- `codex -p <profile>` sessions are listed, and `codex exec` runs are hidden.
- Cached Claude usage survives an access-token refresh.
- One bad Meridian timestamp leaves the other accounts readable.
- Codex accounts outside Meridian stay as last-known when the Codex CLI fails.
- Untracked processes stay out of History after a reboot.
- Claude sessions in folders with spaces or accents show their details.
- omp sessions under your home folder are found without an open transcript.
- A job wait gets its result even when the first report to the CLI fails.
- Waits from another Tern daemon stay off this daemon's panes.
- Tern fetches a provider only it enables once that provider's reading is old.
- Tern retries a failing agent scan once a minute.
- A waiting session open in two panes leaves the shared view file alone.
- A logged-out `gh` shows one hint to run `gh auth login`.
- `/wait check !` shows the command and prompt it saved.
- A `STALE` check counts as cancelled, and a bot's own reply doesn't count as a fix.
- A comment made before the first GraphQL poll is reported.
- Plasma shows an error when it can't read agent state, and helpers time out.
- Launch and Focus report when kitty didn't start or no window matched.
- The installer follows `XDG_CONFIG_HOME` and `XDG_DATA_HOME` and quotes the handler path.
- An account's "only if proj > 100%" box shows the old all-accounts setting.
- Typing switches the Plasma popup to Agents after a tab click.
- Kilo and OpenRouter tray meters hide windows the account doesn't report.
- Several agents waiting for input arrive as one notification.
- Resume works for nu and tcsh users.

### Docs

- The README leads with the Tern extension; the Plasma widget is optional.
- [Set sessions aside](docs/waiting.md) is a guide to waits, with a film.
- The films show current model names.

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

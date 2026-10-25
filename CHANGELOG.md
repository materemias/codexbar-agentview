# Changelog

## Unreleased

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

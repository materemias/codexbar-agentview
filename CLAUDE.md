# CodexBar AgentView

KDE Plasma 6 applet and Tern plugin showing AI provider usage (from the upstream `codexbar` CLI) and live coding-agent sessions.

Identities: applet `io.github.materemias.codexbaragentview`, Tern plugin `codexbar-agentview`, URL scheme `codexbar-agentview://`. Our state lives in `$XDG_STATE_HOME/codexbar-agentview` (`contents/scripts/state_paths.py`). `~/.codexbar/` belongs to the CodexBar CLI; only read its `config.json`.

## Architecture

- `codexbar_fetch.py` is the only provider normalization path and `contents/config/main.xml` the only config schema; extend them rather than adding another.
- QML never writes `agents.json`; dismissal goes through `codexbar_agents.py --dismiss` under the writer lock.
- Run processes from QML through `native/` (`QProcess`): unique executable DataSource names make Plasma's metadata grow.
- Usage fetches are expensive: each is a Claude OAuth call, and a rate-limited one starts a real Claude Code session. The applet polls; the Tern plugin reads the cache and fetches only when nothing measured usage for 2 × `refreshSeconds`. The `fetch_claim` lock keeps every poller to one fetch per interval.
- The Tern plugin passes `--tern` to `codexbar_focus.py`, which skips every KWin step.

## Development

- The native plugin can't be replaced while Plasma runs it: `systemctl --user stop plasma-plasmashell.service`, `kpackagetool6 -t Plasma/Applet -u build/package`, then start. `kquitapp6` returns before plasmashell exits, and the restart leaves the panel down.
- `plasmoidviewer` is unreliable on Wayland; check changes in the real panel.
- Test `tern/` changes on a scratch daemon: `tern --control <sock> <dir>` with `TERN_DAEMON_SOCKET` set to a scratch socket, driven by `tern ctl --control <sock>`. On the user's daemon, `ctl` input reaches their focused pane.
- After visible UI changes, re-render the films in `film/`.
- Ship README.md updates in the same change as the feature.
- Commit with Conventional Commits (`feat(scope): …`, `fix`, `docs`, `ci`, `refactor`, `test`, `chore`), one change per commit. History before October 2026 uses plain sentences; don't copy it.

## Invariants

- Provider order everywhere: Claude, Codex, z.ai, OpenCode Go, OpenRouter, Kilo, TypeSafe.
- Tray meter keys are persisted config: `codex:<window>` (legacy) and `codex:<encoded-email>:<window>`.
- Codex Spark windows are filtered out on purpose.
- OpenCode Go uses the `api` source; the CLI's `local` estimate is far off and only a fallback.
- Processes whose session cannot be resolved stay listed as `untracked`.
- Not listed: Claude Code agent-view tasks (`claude daemon`), omp/pi under `--mode rpc|json`, omp `__advisor.*.jsonl` sidecars.
- Launch resumes a history row only when its saved command matches `_resume_command` and the session is not live. Teleport resumes only after the kitty agent exited: a session file has one live writer.
- Filtering toggles row `visible`; Repeater models stay unfiltered so typing does not rebuild delegates.
- Plasma clamps popup height: keep usage sections compact.
- Claude OAuth fetches take about 16 s; keep the helper timeout at 30 s or more.

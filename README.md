# herdr-agent-inbox

Treat herdr's Agents sidebar as an **inbox** — inspired by
[T3 Code's "settle" workflow](https://x.com/theo/status/2079892861689254129):

> *"Think of it more like an inbox. When you're done with a thread, click the
> 'settle' button and it slides to the bottom. This has helped me 'finish' more."*

## What it does

1. **Session titles** — every agent row gets a ChatGPT-style title derived from
   the agent's native session transcript (first real user prompt; Claude Code
   summaries when available). Works for claude, pi, codex (session-ref
   dependent); falls back to the pane label / terminal title for others
   (hermes profiles already have meaningful titles).
2. **Inbox ordering with Settle / Mark unread** — the Agents panel is sorted by
   attention tier, most recent first within each tier:

   | rank | tier |
   |------|------|
   | 0 | `blocked` (needs input) |
   | 1 | `done` (finished, unseen) or manually **marked unread** |
   | 2 | `working` |
   | 3 | `idle` (seen) |
   | 5 | **settled** (reviewed, slid to the bottom) |

   Settling auto-clears the moment the agent starts working again or blocks.
   A marked-unread flag clears when you focus the pane (after having left it).
3. **Workspace rollups** — each space row can show `$agents`
   (e.g. `!1 ●2 ▸1 ⚑3` = 1 blocked, 2 finished/unread, 1 working, 3 settled)
   and `$busy` (longest currently-working stint).
4. **Running time** — per-agent `$age` (how long this agent session has been in
   the pane) and `$since` (time in its current state), refreshed every 30s.

## Architecture

- `daemon.py` — stdlib-Python daemon started by the plugin `[[startup]]` hook.
  Subscribes to herdr socket events (`pane.updated/created/closed/focused/
  agent_detected`), diffs `agent.list`, and reports:
  - `pane.report_metadata`: `title` + tokens `title`, `rank`, `age`, `since`, `flag`
  - `workspace.report_metadata`: tokens `agents`, `busy`
  - `agent.view.set`: sort `[{token:"rank"} asc, state_change_seq desc]`
  - Control socket (`$HERDR_PLUGIN_STATE_DIR/control.sock`) for
    settle/unread/retitle commands.
  - State persists across server restarts in `state.json`, keyed by
    `terminal_id` (stable across restore).
- `actions.py` — keybindable plugin actions (settle, unread, settle-workspace,
  retitle, restart-daemon).
- `inbox_tui.py` — popup inbox (`[[panes]] id="inbox"`, placement popup):
  j/k navigate, enter focus, `s` settle, `u` unread, `S` settle all finished,
  `r` retitle, `g` group by workspace, `q` quit.

## Install

```sh
herdr plugin link ~/.config/herdr/plugins-dev/herdr-agent-inbox
sh ~/.config/herdr/plugins-dev/herdr-agent-inbox/scripts/ensure-daemon.sh   # first time; afterwards [[startup]] handles it
```

### Sidebar rows (config.toml)

The tokens only render if your sidebar rows reference them:

```toml
[ui.sidebar.agents]
rows = [
  ["state_icon", "$title"],
  [{ token = "$flag", fg = "#f1fa8c", bold = true }, "agent", "workspace", { token = "$age", dim = true }],
]

[ui.sidebar.spaces]
rows = [
  ["state_icon", "workspace", { token = "$busy", dim = true }],
  ["branch", "git_status", "$agents"],
]
# Note: styled custom tokens keep the `$` prefix ({ token = "$flag", … });
# builtins (state_icon, workspace, …) stay bare.
```

### Keybindings (config.toml)

```toml
[[keys.command]]
key = "prefix+m"          # settle focused agent
type = "shell"
command = "herdr plugin action invoke settle --plugin herdr-agent-inbox"

[[keys.command]]
key = "prefix+shift+m"    # mark focused agent unread
type = "shell"
command = "herdr plugin action invoke unread --plugin herdr-agent-inbox"

[[keys.command]]
key = "prefix+alt+m"      # settle all finished agents in current workspace
type = "shell"
command = "herdr plugin action invoke settle-workspace --plugin herdr-agent-inbox"

[[keys.command]]
key = "prefix+i"          # open the inbox popup
type = "shell"
command = "herdr plugin pane open --plugin herdr-agent-inbox --entrypoint inbox"
```

Then `herdr server reload-config`.

## Notes / limitations

- Sort is installed via `agent.view.set` (source `herdr-agent-inbox`) and
  overrides `ui.agent_panel_sort` while active. `herdr agent view` has no CLI;
  to remove it, stop the daemon and restart the herdr server, or send
  `agent.view.clear` over the socket.
- `$age` for agents that existed before the plugin was installed starts
  counting from first sight, not the real session start.
- Codex panes only get transcript titles when herdr reports a session ref for
  them; otherwise they fall back to the pane/terminal title.
- Requires `python3` on PATH (stdlib only). Logs:
  `<plugin state dir>/daemon.log`.

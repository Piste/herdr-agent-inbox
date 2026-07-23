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
  j/k navigate, enter focus, `s` settle, `u` unread, `c` clear flag,
  `S` settle all finished, `r` retitle, `g` group by workspace, `q` quit.
  Mouse: left-click selects, double-click focuses, **right-click toggles
  settle/unsettle** on the row under the cursor. (Herdr's native right-click
  context menus are hardcoded in the client — `ContextMenuKind` in
  `src/app/state.rs` — so a plugin cannot add "Settle" there; in-popup mouse
  is the extension-route answer. Native menu items would need a herdr fork.)

## Title configuration

`<herdr config dir>/plugins/config/herdr-agent-inbox/config.toml`
(auto-created with comments on first run, hot-reloaded on change):

```toml
[title]
source = "first"   # "first" opening prompt | "last" most recent prompt
summarize = true   # pipe the prompt through summarize_cmd for a real title
summarize_cmd = "claude -p --model claude-haiku-4-5-20251001 'Write a 4-8 word title for this coding-agent session request. Output ONLY the title, nothing else.'"
summarize_timeout_secs = 60
```

`source = "last"` re-derives the title whenever the agent finishes a turn.
Summaries run on an async worker, are cached per prompt content (so each
session is summarized once), and fall back to the truncated raw prompt until
the summary lands or if the command fails.

## Sidebar rollup legend ($agents)

`!2` blocked · `●1` finished-unseen/unread · `▸3` working · `·2` idle ·
`⚑1` settled. A workspace whose only content is a single idle agent shows
nothing (the space's own status icon already conveys it).

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
key = "prefix+m"
type = "shell"
description = "settle agent (slides to bottom of inbox)"
command = "herdr plugin action invoke settle --plugin herdr-agent-inbox"

[[keys.command]]
key = "prefix+shift+m"
type = "shell"
description = "mark agent unread (back to attention tier)"
command = "herdr plugin action invoke unread --plugin herdr-agent-inbox"

[[keys.command]]
key = "prefix+alt+m"
type = "shell"
description = "settle all finished agents in workspace"
command = "herdr plugin action invoke settle-workspace --plugin herdr-agent-inbox"

[[keys.command]]
key = "prefix+i"
type = "shell"
description = "open agent inbox popup"
command = "herdr plugin pane open --plugin herdr-agent-inbox --entrypoint inbox"
```

The `description` fields make the bindings show up properly in the `prefix+?`
help panel. Then `herdr server reload-config`.

### Sidebar resize (mouse + keyboard)

**Mouse (native herdr, undocumented):** drag the sidebar's **last column**
(the divider between sidebar and panes) with the left button. Herdr clamps
the drag to `[sidebar_min_width, sidebar_max_width]` and persists the result
in session state — so keep those bounds apart (e.g. 20/60). There's also a
draggable divider between the spaces and agents sections.

**Keyboard (plugin):** `sidebar-grow` / `sidebar-shrink` actions (bind to
e.g. `prefix+alt+right` / `prefix+alt+left`), or an absolute width via
`python3 actions.py sidebar 36`.

How the keyboard path works (verified on herdr 0.7.5): the live width is
session state — config `sidebar_width` only applies when still config-owned —
but the min/max clamps re-apply to it on every `reload-config`. So a resize
runs two phases: pin `width = min = max = target` + reload (forces the live
width), then relax the bounds back to `[20, 60]` + reload (restores mouse
drag range). The baseline for `±2` is read from `session.json` (live value,
where drags land) or the config file, whichever is fresher. Symlinked
configs are edited through `os.path.realpath`, so a dotfiles symlink
survives.

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

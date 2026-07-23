#!/usr/bin/env python3
"""herdr-agent-inbox popup TUI.

An inbox over all agent panes: attention first, settled at the bottom.

Keys:
  j/k, arrows   move          enter  focus agent (closes popup)
  s             settle        u      mark unread
  c             clear settle/unread flag
  S             settle every finished (done/idle) agent
  r             regenerate title
  g             toggle group-by-workspace
  q / esc       quit

Mouse:
  left click    select        double left click   focus agent
  right click   toggle settle/unsettle on the row under the cursor
"""

import curses
import json
import os
import shlex
import socket
import subprocess
import sys
import time

SOURCE = "herdr-agent-inbox"

STATUS_ICON = {
    "blocked": "!",
    "done": "●",
    "working": "▸",
    "idle": "○",
    "unknown": "?",
}


def herdr_socket_path():
    return os.environ.get("HERDR_SOCKET_PATH") or os.path.expanduser(
        "~/.config/herdr/herdr.sock"
    )


def herdr_request(method, params, timeout=6.0):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(herdr_socket_path())
        s.sendall((json.dumps({"id": "inbox-tui", "method": method,
                               "params": params}) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(1 << 16)
            if not chunk:
                break
            buf += chunk
    finally:
        s.close()
    resp = json.loads(buf)
    if "error" in resp:
        raise RuntimeError(str(resp["error"]))
    return resp.get("result") or {}


def control_send(cmd):
    # Must match daemon.py: state dir derived from the session socket.
    p = os.path.join(os.path.dirname(herdr_socket_path()), "agent-inbox-state")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(3.0)
    try:
        s.connect(os.path.join(p, "control.sock"))
        s.sendall((json.dumps(cmd) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(1 << 14)
            if not chunk:
                break
            buf += chunk
        return json.loads(buf)
    finally:
        s.close()


def load_agents():
    agents = herdr_request("agent.list", {}).get("agents", [])
    ws_labels = {}
    ws_order = {}
    try:
        for idx, ws in enumerate(herdr_request("workspace.list", {}).get("workspaces", [])):
            ws_labels[ws.get("workspace_id")] = ws.get("label") or ws.get("workspace_id")
            ws_order[ws.get("workspace_id")] = idx
    except (OSError, RuntimeError, ValueError):
        pass
    rows = []
    for rec in agents:
        tokens = rec.get("tokens") or {}
        rows.append({
            "pane_id": rec.get("pane_id"),
            "tab_id": rec.get("tab_id"),
            "terminal_id": rec.get("terminal_id"),
            "cwd": rec.get("cwd") or "",
            "workspace_id": rec.get("workspace_id"),
            "ws_order": ws_order.get(rec.get("workspace_id"), 999),
            "workspace": ws_labels.get(rec.get("workspace_id"), rec.get("workspace_id")),
            "agent": rec.get("agent") or "?",
            "status": rec.get("agent_status") or "unknown",
            "seq": rec.get("state_change_seq") or 0,
            "title": rec.get("title") or tokens.get("title")
                     or rec.get("terminal_title_stripped") or rec.get("agent") or "",
            "rank": tokens.get("rank") or "4",
            "age": tokens.get("age") or "",
            "since": tokens.get("since") or "",
            "flag": tokens.get("flag") or "",
        })
    rows.sort(key=lambda r: (r["rank"], -r["seq"]))
    return rows


_TAB_CACHE = {"ts": 0.0, "labels": {}}


def tab_labels(workspace_ids):
    now = time.time()
    if now - _TAB_CACHE["ts"] > 15:
        labels = {}
        for ws in workspace_ids:
            try:
                for t in herdr_request("tab.list", {"workspace_id": ws}).get("tabs", []):
                    labels[t.get("tab_id")] = t.get("label") or str(t.get("number", "?"))
            except (OSError, RuntimeError, ValueError):
                pass
        _TAB_CACHE["ts"] = now
        _TAB_CACHE["labels"] = labels
    return _TAB_CACHE["labels"]


def load_history():
    """terminal_id -> list of closed chats, from the daemon's state file."""
    p = os.path.join(os.path.dirname(herdr_socket_path()),
                     "agent-inbox-state", "state.json")
    try:
        with open(p) as f:
            terms = json.load(f).get("terminals", {})
        return {tid: (t.get("history") or []) for tid, t in terms.items()}
    except (OSError, ValueError):
        return {}


def load_hist_entries():
    """Archived chats from the daemon's history.jsonl, newest first."""
    p = os.path.join(os.path.dirname(herdr_socket_path()),
                     "agent-inbox-state", "history.jsonl")
    entries = []
    try:
        with open(p) as f:
            for ln in f.readlines()[-200:]:
                try:
                    entries.append(json.loads(ln))
                except ValueError:
                    pass
    except OSError:
        pass
    entries.reverse()  # newest first
    # A chat resumed and closed again re-archives under the same session
    # ref — keep only the newest entry per session.
    seen, uniq = set(), []
    for e in entries:
        key = (e.get("agent"), e.get("sess_value")) if e.get("sess_value") \
            else id(e)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(e)
    return uniq


def resume_cmd(entry):
    """Command that reopens this chat in its native CLI, or None."""
    agent = entry.get("agent")
    kind, val = entry.get("sess_kind"), entry.get("sess_value")
    if not val:
        return None
    if agent == "claude" and kind == "id":
        return "claude --resume %s" % shlex.quote(val)
    if agent == "pi":
        return "pi --session %s" % shlex.quote(val)
    if agent == "codex" and kind == "id":
        return "codex resume %s" % shlex.quote(val)
    return None


def _herdr_cli(*args):
    herdr = os.environ.get("HERDR_BIN_PATH", "herdr")
    r = subprocess.run([herdr, *args], capture_output=True, text=True)
    try:
        return json.loads(r.stdout).get("result") or {}
    except ValueError:
        return {}


def do_resume(entry):
    """Reopen an archived chat: split near where it lived (or recreate its
    workspace) and launch the agent's native resume command. Returns an
    error string, or None on success."""
    cmd = resume_cmd(entry)
    if not cmd:
        return "no resumable session ref for this chat"
    new_pane = None
    target = None
    pane = entry.get("pane_id")
    if pane and _herdr_cli("pane", "get", pane).get("pane"):
        target = pane
    if not target and entry.get("workspace_id"):
        panes = _herdr_cli("pane", "list", "--workspace",
                           entry["workspace_id"]).get("panes") or []
        if panes:
            target = panes[0].get("pane_id")
    if target:
        args = ["pane", "split", target, "--direction", "down", "--focus"]
        if entry.get("cwd"):
            args += ["--cwd", entry["cwd"]]
        new_pane = (_herdr_cli(*args).get("pane") or {}).get("pane_id")
    else:
        args = ["workspace", "create", "--focus"]
        if entry.get("cwd"):
            args += ["--cwd", entry["cwd"]]
        if entry.get("workspace"):
            args += ["--label", entry["workspace"]]
        ws = (_herdr_cli(*args).get("workspace") or {}).get("workspace_id")
        if ws:
            panes = _herdr_cli("pane", "list", "--workspace", ws).get("panes") or []
            if panes:
                new_pane = panes[0].get("pane_id")
    if not new_pane:
        return "could not open a pane to resume into"
    _herdr_cli("pane", "run", new_pane, cmd)
    return None


# herdr's palettes, extracted from src/app/state.rs. The sidebar signals
# states with (ui/status.rs): blocked=red, working=yellow, done-unseen=teal,
# idle/seen=green, unknown=overlay0; selection bg=surface0.
_THEME_PALETTES = {
    "catppuccin": {"accent": (137, 180, 250), "red": (243, 139, 168), "yellow": (249, 226, 175), "green": (166, 227, 161), "teal": (148, 226, 213), "blue": (137, 180, 250), "mauve": (203, 166, 247), "overlay0": (108, 112, 134), "surface0": (49, 50, 68)},
    "catppuccin-latte": {"accent": (30, 102, 245), "red": (210, 15, 57), "yellow": (223, 142, 29), "green": (64, 160, 43), "teal": (23, 146, 153), "blue": (30, 102, 245), "mauve": (136, 57, 239), "overlay0": (156, 160, 176), "surface0": (204, 208, 218)},
    "tokyo-night": {"accent": (122, 162, 247), "red": (247, 118, 142), "yellow": (224, 175, 104), "green": (158, 206, 106), "teal": (125, 207, 255), "blue": (122, 162, 247), "mauve": (187, 154, 247), "overlay0": (86, 95, 137), "surface0": (36, 40, 59)},
    "tokyo-night-day": {"accent": (46, 125, 233), "red": (245, 42, 101), "yellow": (140, 108, 62), "green": (88, 117, 57), "teal": (17, 140, 116), "blue": (46, 125, 233), "mauve": (120, 71, 189), "overlay0": (137, 144, 179), "surface0": (196, 200, 218)},
    "dracula": {"accent": (189, 147, 249), "red": (255, 85, 85), "yellow": (241, 250, 140), "green": (80, 250, 123), "teal": (139, 233, 253), "blue": (139, 233, 253), "mauve": (255, 121, 198), "overlay0": (98, 114, 164), "surface0": (68, 71, 90)},
    "nord": {"accent": (136, 192, 208), "red": (191, 97, 106), "yellow": (235, 203, 139), "green": (163, 190, 140), "teal": (143, 188, 187), "blue": (129, 161, 193), "mauve": (180, 142, 173), "overlay0": (76, 86, 106), "surface0": (59, 66, 82)},
    "gruvbox": {"accent": (215, 153, 33), "red": (251, 73, 52), "yellow": (250, 189, 47), "green": (184, 187, 38), "teal": (142, 192, 124), "blue": (131, 165, 152), "mauve": (211, 134, 155), "overlay0": (146, 131, 116), "surface0": (60, 56, 54)},
    "gruvbox-light": {"accent": (7, 102, 120), "red": (157, 0, 6), "yellow": (181, 118, 20), "green": (121, 116, 14), "teal": (66, 123, 88), "blue": (7, 102, 120), "mauve": (143, 63, 113), "overlay0": (146, 131, 116), "surface0": (235, 219, 178)},
    "one-dark": {"accent": (97, 175, 239), "red": (224, 108, 117), "yellow": (229, 192, 123), "green": (152, 195, 121), "teal": (86, 182, 194), "blue": (97, 175, 239), "mauve": (198, 120, 221), "overlay0": (92, 99, 112), "surface0": (44, 49, 58)},
    "one-light": {"accent": (64, 120, 242), "red": (228, 86, 73), "yellow": (193, 132, 1), "green": (80, 161, 79), "teal": (1, 132, 188), "blue": (64, 120, 242), "mauve": (166, 38, 164), "overlay0": (160, 161, 167), "surface0": (240, 240, 241)},
    "solarized": {"accent": (38, 139, 210), "red": (220, 50, 47), "yellow": (181, 137, 0), "green": (133, 153, 0), "teal": (42, 161, 152), "blue": (38, 139, 210), "mauve": (211, 54, 130), "overlay0": (88, 110, 117), "surface0": (7, 54, 66)},
    "solarized-light": {"accent": (38, 139, 210), "red": (220, 50, 47), "yellow": (181, 137, 0), "green": (133, 153, 0), "teal": (42, 161, 152), "blue": (38, 139, 210), "mauve": (211, 54, 130), "overlay0": (147, 161, 161), "surface0": (238, 232, 213)},
    "kanagawa": {"accent": (126, 156, 216), "red": (195, 64, 67), "yellow": (192, 163, 110), "green": (118, 148, 106), "teal": (127, 180, 202), "blue": (126, 156, 216), "mauve": (149, 127, 184), "overlay0": (114, 113, 105), "surface0": (42, 42, 55)},
    "kanagawa-lotus": {"accent": (77, 105, 155), "red": (200, 64, 83), "yellow": (119, 113, 63), "green": (111, 137, 78), "teal": (78, 140, 162), "blue": (77, 105, 155), "mauve": (98, 76, 131), "overlay0": (160, 156, 172), "surface0": (220, 213, 172)},
    "rose-pine": {"accent": (196, 167, 231), "red": (235, 111, 146), "yellow": (246, 193, 119), "green": (49, 116, 143), "teal": (156, 207, 216), "blue": (49, 116, 143), "mauve": (196, 167, 231), "overlay0": (110, 106, 134), "surface0": (31, 29, 46)},
    "rose-pine-dawn": {"accent": (144, 122, 169), "red": (180, 99, 122), "yellow": (234, 157, 52), "green": (40, 105, 131), "teal": (86, 148, 159), "blue": (40, 105, 131), "mauve": (144, 122, 169), "overlay0": (152, 147, 165), "surface0": (242, 233, 225)},
    "vesper": {"accent": (255, 199, 153), "red": (255, 128, 128), "yellow": (255, 199, 153), "green": (153, 255, 228), "teal": (102, 221, 204), "blue": (176, 176, 176), "mauve": (255, 209, 168), "overlay0": (92, 92, 92), "surface0": (35, 35, 35)},
}


def theme_palette():
    """The active theme's palette, honoring [theme.custom] hex overrides."""
    path = os.environ.get("HERDR_CONFIG_PATH") or os.path.expanduser(
        "~/.config/herdr/config.toml")
    name, custom = None, {}
    try:
        import tomllib
        with open(path, "rb") as f:
            theme = tomllib.load(f).get("theme") or {}
        name = theme.get("name")
        custom = theme.get("custom") or {}
    except (OSError, ValueError, ImportError):
        pass
    pal = dict(_THEME_PALETTES.get(name or "catppuccin") or {})
    for key, val in custom.items():
        if isinstance(val, str) and val.startswith("#") and len(val) == 7:
            try:
                pal[key] = tuple(int(val[i:i + 2], 16) for i in (1, 3, 5))
            except ValueError:
                pass
    return pal


def _rgb_to_256(r, g, b):
    """Nearest xterm-256 index (6x6x6 cube vs grayscale ramp)."""
    def cube(v):
        return 0 if v < 48 else 1 if v < 115 else (v - 35) // 40
    qr, qg, qb = cube(r), cube(g), cube(b)
    steps = [0, 95, 135, 175, 215, 255]
    cube_idx = 16 + 36 * qr + 6 * qg + qb
    cube_dist = (steps[qr] - r) ** 2 + (steps[qg] - g) ** 2 + (steps[qb] - b) ** 2
    gray = max(0, min(23, (sum((r, g, b)) // 3 - 8) // 10))
    gv = 8 + gray * 10
    gray_dist = (gv - r) ** 2 + (gv - g) ** 2 + (gv - b) ** 2
    return (232 + gray) if gray_dist < cube_dist else cube_idx


def chat_emoji(r):
    # Herdr's sidebar language: red=blocked, yellow=working,
    # teal/blue=finished-unseen, green=idle/clear.
    if r["rank"] == "5":
        return "🏁"
    if r["status"] == "blocked":
        return "🔴"
    if r["rank"] == "1":
        return "🔵"
    if r["status"] == "working":
        return "🟡"
    if r["status"] == "idle":
        return "🟢"
    return "⚪"


_EMOJI_ORDER = ["🔴", "🔵", "🟡", "🟢", "🏁", "⚫", "⚪"]


def _agg(emojis):
    uniq = set(emojis)
    return "".join(e for e in _EMOJI_ORDER if e in uniq)


def _short_cwd(cwd):
    home = os.path.expanduser("~")
    return ("~" + cwd[len(home):]) if cwd.startswith(home) else cwd


CLOSED_PER_DIR = 5


def build_tree(rows, archive):
    """workspace → tab → directory → chats hierarchy.

    Live chats come from agent.list; closed chats accumulate from the
    durable archive (history.jsonl), ChatGPT-style: they stay listed under
    their workspace/directory even after their pane is gone, newest first,
    capped per directory. Closed rows are selectable and resumable.
    """
    lines = []
    tabs_by_ws = {}
    ws_rank = {}
    for r in rows:
        tabs = tabs_by_ws.setdefault(r["workspace_id"], {})
        # Group by directory, not pane: three panes in the same cwd share
        # one directory line with their chats listed under it.
        tabs.setdefault(r["tab_id"], {}).setdefault(r["cwd"], []).append(r)
        ws_rank[r["workspace_id"]] = min(r["ws_order"],
                                         ws_rank.get(r["workspace_id"], 999))
    # Archived chats grouped by (workspace, cwd). Only open workspaces show
    # in the tree — the full global archive lives under 'h'.
    closed_by_dir = {}
    for e in archive:
        closed_by_dir.setdefault((e.get("workspace_id"), e.get("cwd")), []).append(e)
    for group in closed_by_dir.values():
        group.sort(key=lambda e: -(e.get("closed") or 0))
    # Same order as the sidebar's spaces list.
    ws_order = sorted(tabs_by_ws, key=lambda w: ws_rank[w])
    tlabels = tab_labels(ws_order)
    for ws in ws_order:
        ws_lines = []
        multi_tab = len(tabs_by_ws[ws]) > 1
        pane_x = 5 if multi_tab else 3  # dedent when the tab row is hidden
        seen_dirs = set()
        for tab_id, dirs in tabs_by_ws[ws].items():
            tab_emojis = []
            tab_lines = []
            for cwd, chats in dirs.items():
                seen_dirs.add(cwd)
                closed = closed_by_dir.get((ws, cwd), [])[:CLOSED_PER_DIR]
                pane_emojis = [chat_emoji(c) for c in chats] + (["⚫"] if closed else [])
                tab_lines.append(("pane", {"cwd": _short_cwd(chats[0]["cwd"]),
                                           "flags": _agg(pane_emojis), "x": pane_x}))
                for c in chats:
                    c["_x"] = pane_x + 2
                    tab_lines.append(("row", c))
                for h in closed:
                    tab_lines.append(("closed", dict(h, x=pane_x + 2)))
                tab_emojis.extend(pane_emojis)
            if multi_tab:  # a single tab adds no information
                tab_lines.insert(0, ("tab", {"label": tlabels.get(tab_id, tab_id or "?"),
                                             "flags": _agg(tab_emojis)}))
            ws_lines.extend(tab_lines)
        # Directories whose panes are gone but whose chats live on in the
        # archive — the Codex-app style accumulated list.
        for (aws, cwd), group in sorted(closed_by_dir.items(),
                                        key=lambda kv: -(kv[1][0].get("closed") or 0)):
            if aws != ws or not cwd or cwd in seen_dirs:
                continue
            ws_lines.append(("pane", {"cwd": _short_cwd(cwd), "flags": "⚫",
                                      "x": pane_x}))
            for h in group[:CLOSED_PER_DIR]:
                ws_lines.append(("closed", dict(h, x=pane_x + 2)))
        lines.append(("ws", {"workspace": rows_ws_label(rows, ws)}))
        lines.extend(ws_lines)
    return lines


def rows_ws_label(rows, ws_id):
    for r in rows:
        if r["workspace_id"] == ws_id:
            return r["workspace"]
    return ws_id


VIEW_MODES = ("tree", "compact", "grouped", "flat")


def _group_flag(group):
    """Dominant state of a workspace's agents, most urgent wins."""
    if any(r["status"] == "blocked" for r in group):
        return "!"
    if any(r["rank"] == "1" for r in group):
        return "●"
    if any(r["status"] == "working" for r in group):
        return "▸"
    if all(r["rank"] == "5" for r in group):
        return "⚑"
    return "○"


def build_lines(rows, mode):
    """Returns a list of ('header', text) / ('row', row-dict) entries."""
    lines = []
    if mode == "flat":
        for r in rows:
            lines.append(("row", r))
        return lines
    by_ws = {}
    for r in rows:
        by_ws.setdefault(r["workspace_id"], []).append(r)
    # Same order as the sidebar's spaces list.
    order = sorted(by_ws, key=lambda w: by_ws[w][0]["ws_order"])
    for ws in order:
        group = by_ws[ws]
        lines.append(("header", {
            "workspace": group[0]["workspace"],
            "flag": _group_flag(group) if mode == "compact" else None,
            "count": len(group),
        }))
        for r in group:
            lines.append(("row", r))
    return lines


def _prefs_path():
    return os.path.join(os.path.dirname(herdr_socket_path()),
                        "agent-inbox-state", "tui_prefs.json")


def load_prefs():
    try:
        with open(_prefs_path()) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_prefs(prefs):
    try:
        with open(_prefs_path(), "w") as f:
            json.dump(prefs, f)
    except OSError:
        pass


def counts_line(rows):
    c = {"attention": 0, "working": 0, "idle": 0, "settled": 0}
    for r in rows:
        if r["rank"] in ("0", "1"):
            c["attention"] += 1
        elif r["rank"] == "2":
            c["working"] += 1
        elif r["rank"] == "5":
            c["settled"] += 1
        else:
            c["idle"] += 1
    return "%(attention)d need attention · %(working)d working · %(idle)d idle · %(settled)d settled" % c


def run(stdscr):
    curses.curs_set(0)
    curses.use_default_colors()
    # Herdr's sidebar state language: blocked=red, working=yellow,
    # done-unseen=teal, idle=green, unknown=overlay0. Basic-color fallbacks
    # first, then the exact theme RGBs when the terminal has 256 colors.
    curses.init_pair(1, curses.COLOR_RED, -1)      # blocked
    curses.init_pair(2, curses.COLOR_YELLOW, -1)   # working
    curses.init_pair(3, curses.COLOR_CYAN, -1)     # done / unread (teal)
    curses.init_pair(7, curses.COLOR_GREEN, -1)    # idle / clear
    curses.init_pair(4, curses.COLOR_CYAN, -1)     # agent names (blue)
    curses.init_pair(5, curses.COLOR_BLUE, -1)     # pane dirs (accent)
    curses.init_pair(6, curses.COLOR_MAGENTA, -1)  # workspace names (mauve)
    sel_pair = curses.A_REVERSE
    pal = theme_palette()
    if pal and curses.COLORS >= 256:
        try:
            for pair, token in ((1, "red"), (2, "yellow"), (3, "teal"),
                                (7, "green"), (4, "blue"), (5, "accent"),
                                (6, "mauve")):
                if token in pal:
                    curses.init_pair(pair, _rgb_to_256(*pal[token]), -1)
            if "surface0" in pal:
                curses.init_pair(10, -1, _rgb_to_256(*pal["surface0"]))
                sel_pair = curses.color_pair(10)
        except curses.error:
            pass
    curses.mousemask(curses.ALL_MOUSE_EVENTS)
    stdscr.timeout(2000)  # refresh every 2s when idle

    prefs = load_prefs()
    mode = prefs.get("view")
    if mode not in VIEW_MODES:
        mode = "tree"
    hist_mode = False
    sel = 0
    status_msg = ""
    rows = []

    while True:
        try:
            rows = load_agents()
            err = None
        except (OSError, RuntimeError, ValueError) as e:
            err = str(e)
        if hist_mode:
            lines = [("hist", e) for e in load_hist_entries()]
        elif mode == "tree":
            lines = build_tree(rows, load_hist_entries())
        else:
            lines = build_lines(rows, mode)
        row_idx = [i for i, (kind, _) in enumerate(lines)
                   if kind in ("row", "hist", "closed")]
        if row_idx:
            sel = max(0, min(sel, len(row_idx) - 1))

        h, w = stdscr.getmaxyx()
        stdscr.erase()
        if hist_mode:
            header = " Chat history — %d archived (newest first)" % len(lines)
            help_line = " enter/dbl-click:reopen chat (↩ = resumable)  h:back  q:quit"
        else:
            header = " Agent Inbox — %s" % counts_line(rows)
            help_line = (" enter:focus  s:settle  u:unread  c:clear  S:settle-finished"
                         "  r:retitle  g:view  h:history  q:quit  |  right-click: settle")
        stdscr.addnstr(0, 0, header.ljust(w - 1), w - 1, curses.A_BOLD)
        stdscr.addnstr(h - 1, 0, (status_msg or help_line).ljust(w - 1), w - 1, curses.A_DIM)

        top = 1
        visible = h - 2
        # Scroll so selection stays on screen.
        sel_line = row_idx[sel] if row_idx else 0
        first = max(0, min(sel_line - visible // 2, len(lines) - visible))
        FLAG_ATTR = {
            "!": curses.color_pair(1) | curses.A_BOLD,
            "●": curses.color_pair(3) | curses.A_BOLD,   # finished-unseen: teal
            "▸": curses.color_pair(2),                   # working: yellow
            "⚑": curses.A_DIM,
            "○": curses.color_pair(7),                   # idle: green
        }

        def seg(yy, x, s, attr):
            if x < w - 1 and s:
                stdscr.addnstr(yy, x, s, w - 1 - x, attr)
            return x + len(s)

        def pick(base, on):
            # Selected rows use the theme highlight pair; curses pairs can't
            # be OR-combined, so keep only bold/dim from the base attr.
            if not on:
                return base
            return sel_pair | (base & (curses.A_BOLD | curses.A_DIM))

        for y, i in enumerate(range(first, min(len(lines), first + visible))):
            kind, item = lines[i]
            yy = top + y
            if kind == "ws":
                seg(yy, 1, item["workspace"], curses.color_pair(6) | curses.A_BOLD)
                continue
            if kind == "tab":
                seg(yy, 3, item["label"], curses.color_pair(4) | curses.A_BOLD)
                continue
            if kind == "pane":
                seg(yy, item.get("x", 5), item["cwd"], curses.color_pair(5))
                continue
            if kind == "hist":
                on = bool(row_idx and i == row_idx[sel])
                if on:
                    stdscr.addnstr(yy, 0, " " * (w - 1), w - 1, sel_pair)
                when = time.strftime("%d/%m %H:%M", time.localtime(item.get("closed", 0)))
                x = seg(yy, 1, when, pick(curses.A_DIM, on))
                x = seg(yy, x, "  %s" % item.get("agent", "?"),
                        pick(curses.color_pair(4), on))
                x = seg(yy, x, ": ", pick(curses.A_DIM, on))
                x = seg(yy, x, item.get("title", ""), pick(0, on))
                meta = "  %s%s" % (item.get("workspace") or item.get("workspace_id") or "",
                                   "  ↩" if resume_cmd(item) else "")
                seg(yy, x, meta, pick(curses.A_DIM, on))
                continue
            if kind == "closed":
                on = bool(row_idx and i == row_idx[sel])
                if on:
                    stdscr.addnstr(yy, 0, " " * (w - 1), w - 1, sel_pair)
                x = seg(yy, item.get("x", 7), "%s: %s" % (item.get("agent", "?"),
                                                          item.get("title", "")),
                        pick(curses.A_DIM, on))
                seg(yy, x, " — ⚫%s" % ("  ↩" if resume_cmd(item) else ""),
                    pick(curses.A_DIM, on))
                continue
            if kind == "header":
                yy = top + y
                x = seg(yy, 1, item["workspace"],
                        curses.color_pair(6) | curses.A_BOLD)
                if item.get("flag"):
                    x = seg(yy, x, " — ", curses.A_DIM)
                    seg(yy, x, item["flag"], FLAG_ATTR.get(item["flag"], 0))
                else:
                    seg(yy, x, "  (%d)" % item["count"], curses.A_DIM)
                continue
            r = item
            icon = "⚑" if r["rank"] == "5" else (
                "●" if r["flag"] == "●" else STATUS_ICON.get(r["status"], "?"))
            attr = curses.A_NORMAL
            if r["status"] == "blocked":
                attr = curses.color_pair(1) | curses.A_BOLD
            elif r["rank"] == "1":
                attr = curses.color_pair(3) | curses.A_BOLD   # done/unread: teal
            elif r["status"] == "working":
                attr = curses.color_pair(2)                   # working: yellow
            elif r["rank"] == "5":
                attr = curses.A_DIM
            selected = bool(row_idx and i == row_idx[sel])
            if mode == "tree":
                if selected:
                    stdscr.addnstr(yy, 0, " " * (w - 1), w - 1, sel_pair)
                x = seg(yy, r.get("_x", 7), r["agent"],
                        pick(curses.color_pair(4), selected))
                x = seg(yy, x, ": ", pick(curses.A_DIM, selected))
                x = seg(yy, x, r["title"][: max(10, w - x - 8)], pick(attr, selected))
                x = seg(yy, x, " — ", pick(curses.A_DIM, selected))
                seg(yy, x, chat_emoji(r), pick(0, selected))
                continue
            if mode == "compact":
                yy = top + y
                if selected:
                    stdscr.addnstr(yy, 0, " " * (w - 1), w - 1, sel_pair)
                title = r["title"][: max(10, w - len(r["agent"]) - 12)]
                x = seg(yy, 4, title, pick(attr, selected))
                x = seg(yy, x, " — ", pick(curses.A_DIM, selected))
                seg(yy, x, r["agent"], pick(curses.color_pair(4), selected))
                continue
            meta = ("%s · %s" % (r["agent"], r["age"]) if mode == "grouped"
                    else "%s · %s · %s" % (r["agent"], r["workspace"], r["age"]))
            indent = "   " if mode == "grouped" else " "
            avail = max(10, w - len(meta) - len(indent) - 7)
            text = "%s%s %-*s  %s" % (indent, icon, avail, r["title"][:avail], meta)
            attr = pick(attr, selected)
            stdscr.addnstr(top + y, 0, text.ljust(w - 1), w - 1, attr)
        stdscr.refresh()
        status_msg = ""

        def send_op(op, row):
            try:
                resp = control_send({"cmd": op, "pane_id": row["pane_id"]})
                if not resp.get("ok"):
                    return " %s failed: %s" % (op, resp.get("error"))
                time.sleep(0.6)  # let the daemon re-report before refresh
                return ""
            except (OSError, ValueError) as e:
                return " daemon not reachable: %s" % e

        ch = stdscr.getch()
        if ch in (ord("q"), 27):
            return
        if ch == -1:
            continue  # timeout -> refresh
        if ch == ord("h"):
            hist_mode = not hist_mode
            sel = 0
            continue
        if ch == curses.KEY_MOUSE:
            try:
                _, mx, my, _, bstate = curses.getmouse()
            except curses.error:
                continue
            i = first + (my - top)
            if not (0 <= my - top < visible and 0 <= i < len(lines)):
                continue
            if lines[i][0] not in ("row", "hist", "closed"):
                continue
            row = lines[i][1]
            if i in row_idx:
                sel = row_idx.index(i)
            if lines[i][0] in ("hist", "closed"):
                if bstate & curses.BUTTON1_DOUBLE_CLICKED:
                    err = do_resume(row)
                    if err:
                        status_msg = " " + err
                        continue
                    return
                continue
            if bstate & (curses.BUTTON3_CLICKED | curses.BUTTON3_PRESSED
                         | curses.BUTTON3_RELEASED):
                # Right-click: toggle settled/unsettled on the row under cursor.
                op = "clear" if (row["rank"] == "5" or row["flag"]) else "settle"
                status_msg = send_op(op, row) or (
                    " ⚑ settled: %s" % row["title"] if op == "settle"
                    else " cleared: %s" % row["title"])
            elif bstate & curses.BUTTON1_DOUBLE_CLICKED:
                try:
                    herdr_request("agent.focus", {"target": row["pane_id"]})
                except (OSError, RuntimeError) as e:
                    status_msg = " focus failed: %s" % e
                    continue
                return
            continue
        if not row_idx:
            continue
        cur_kind, cur = lines[row_idx[sel]]
        if ch in (ord("j"), curses.KEY_DOWN):
            sel = min(sel + 1, len(row_idx) - 1)
        elif ch in (ord("k"), curses.KEY_UP):
            sel = max(sel - 1, 0)
        elif ch == ord("g") and not hist_mode:
            mode = VIEW_MODES[(VIEW_MODES.index(mode) + 1) % len(VIEW_MODES)]
            prefs["view"] = mode
            save_prefs(prefs)
            status_msg = " view: %s" % mode
        elif ch == 10:  # enter
            if cur_kind in ("hist", "closed"):
                err = do_resume(cur)
                if err:
                    status_msg = " " + err
                    continue
                return
            try:
                herdr_request("agent.focus", {"target": cur["pane_id"]})
            except (OSError, RuntimeError) as e:
                status_msg = " focus failed: %s" % e
                continue
            return
        elif ch in (ord("s"), ord("u"), ord("r"), ord("c")):
            if cur_kind in ("hist", "closed"):
                status_msg = " archived chat — enter reopens it"
                continue
            op = {"s": "settle", "u": "unread", "r": "retitle", "c": "clear"}[chr(ch)]
            status_msg = send_op(op, cur)
        elif ch == ord("S") and not hist_mode:
            n = 0
            for r in rows:
                if r["status"] in ("done", "idle") and r["rank"] != "5":
                    try:
                        if control_send({"cmd": "settle", "pane_id": r["pane_id"]}).get("ok"):
                            n += 1
                    except (OSError, ValueError):
                        break
            status_msg = " settled %d agents" % n
            time.sleep(0.6)


def main():
    try:
        curses.wrapper(run)
    except KeyboardInterrupt:
        pass
    except Exception:
        import traceback
        p = os.path.join(os.path.dirname(herdr_socket_path()),
                         "agent-inbox-state", "tui-crash.log")
        with open(p, "a") as f:
            traceback.print_exc(file=f)
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""herdr-agent-inbox popup TUI.

An inbox over live agent panes, with a searchable archive.

Keys:
  j/k, arrows   move          enter  focus agent (closes popup)
  a             archive idle/done agent (durably, then close its pane)
  u / h         browse archived agents
  /             search archive
  r             regenerate title
  g             toggle group-by-workspace
  q / esc       quit

Mouse:
  left click    select        double left click   focus agent
  right click   archive the idle/done row under the cursor
"""

import curses
import json
import os
import shlex
import socket
import subprocess
import sys
import time
import unicodedata


def _cwidth(ch):
    return 2 if unicodedata.east_asian_width(ch) in "WF" else 1


def _wwidth(s):
    return sum(_cwidth(ch) for ch in s)


def _wtrunc(s, cols):
    """Truncate s to at most `cols` display columns (emoji are 2 wide)."""
    out, used = [], 0
    for ch in s:
        cw = _cwidth(ch)
        if used + cw > cols:
            break
        out.append(ch)
        used += cw
    return "".join(out)

SOURCE = "herdr-agent-inbox"

STATUS_ICON = {
    "blocked": "!",
    "done": "●",
    "working": "▸",
    "idle": "○",
    "unknown": "?",
}


DEMO = "--demo" in sys.argv


def herdr_socket_path():
    return os.environ.get("HERDR_SOCKET_PATH") or os.path.expanduser(
        "~/.config/herdr/herdr.sock"
    )


def _demo_rows():
    mk = lambda i, ws, wsl, tab, cwd, agent, status, rank, title, age, flag="": {
        "pane_id": "w%d:p1" % i, "tab_id": "%s:t1" % ws, "terminal_id": "t%d" % i,
        "cwd": cwd, "workspace_id": ws, "ws_order": i, "workspace": wsl,
        "agent": agent, "status": status, "seq": 100 - i, "title": title,
        "rank": rank, "age": age, "since": age, "flag": flag,
    }
    return [
        mk(1, "w1", "webapp", "t1", "/Users/dev/webapp", "claude", "blocked", "0",
           "Add OAuth login flow with refresh tokens", "12m"),
        mk(2, "w2", "api", "t2", "/Users/dev/api", "codex", "done", "1",
           "Fix flaky payment webhook tests", "1h04m"),
        mk(3, "w1", "webapp", "t1", "/Users/dev/webapp", "pi", "working", "2",
           "Migrate database to Postgres 17", "26m"),
        mk(4, "w3", "dotfiles", "t3", "/Users/dev/dotfiles", "codex", "working", "2",
           "Refactor zsh prompt into modules", "8m"),
        mk(5, "w4", "blog", "t4", "/Users/dev/blog", "claude", "idle", "3",
           "Write post: terminal multiplexers in 2026", "2h11m"),
        mk(6, "w2", "api", "t2", "/Users/dev/api", "hermes", "idle", "5",
           "Rate-limit the public endpoints", "3h40m", "⚑"),
    ]


def _demo_history():
    return [
        {"agent": "claude", "title": "Design the billing schema", "closed": 1784800000,
         "workspace_id": "w2", "workspace": "api", "cwd": "/Users/dev/api",
         "sess_kind": "id", "sess_value": "demo-1"},
        {"agent": "pi", "title": "Spike: switch bundler to rolldown", "closed": 1784790000,
         "workspace_id": "w1", "workspace": "webapp", "cwd": "/Users/dev/webapp",
         "sess_kind": "path", "sess_value": "/tmp/demo.jsonl"},
        {"agent": "codex", "title": "Fix dark-mode contrast issues", "closed": 1784780000,
         "workspace_id": "w1", "workspace": "webapp", "cwd": "/Users/dev/webapp"},
    ]


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
    if not buf:
        raise RuntimeError("herdr closed the connection without replying")
    try:
        resp = json.loads(buf)
    except ValueError as e:
        raise RuntimeError("bad response from herdr: %s" % e)
    if "error" in resp:
        raise RuntimeError(str(resp["error"]))
    return resp.get("result") or {}


def control_send(cmd):
    if DEMO:
        return {"ok": True, "title": "(demo)"}
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
    if DEMO:
        return _demo_rows()
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
            "sess_kind": (rec.get("agent_session") or {}).get("kind"),
            "sess_value": (rec.get("agent_session") or {}).get("value"),
        })
    rows.sort(key=lambda r: (r["rank"], -r["seq"]))
    return rows


_TAB_CACHE = {"ts": 0.0, "labels": {}}


def tab_labels(workspace_ids):
    if DEMO:
        return {"w1:t1": "feature", "w2:t1": "hotfix", "w3:t1": "1", "w4:t1": "1"}
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


def session_key(item):
    value = item.get("sess_value")
    return (item.get("agent"), item.get("sess_kind"), value) if value else None


def filter_history(entries, query):
    words = query.casefold().split()
    if not words:
        return entries
    out = []
    for entry in entries:
        haystack = " ".join(str(entry.get(k) or "") for k in (
            "title", "agent", "workspace", "workspace_id", "cwd", "sess_value"
        )).casefold()
        if all(word in haystack for word in words):
            out.append(entry)
    return out


def load_hist_entries(live_rows=None):
    """Archived chats from the daemon's history.jsonl, newest first."""
    if DEMO:
        return _demo_history()
    p = os.path.join(os.path.dirname(herdr_socket_path()),
                     "agent-inbox-state", "history.jsonl")
    entries = []
    try:
        with open(p) as f:
            for ln in f:
                try:
                    entries.append(json.loads(ln))
                except ValueError:
                    pass
    except OSError:
        pass
    entries.reverse()  # newest first
    # A chat resumed and closed again re-archives under the same session
    # ref — keep only the newest entry per session.
    live = {session_key(r) for r in (live_rows or []) if session_key(r)}
    seen, uniq = set(), []
    for e in entries:
        key = session_key(e) or ("legacy", e.get("closed"), e.get("title"))
        if key in live:
            continue
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
    if r.returncode:
        return {"_error": (r.stderr or r.stdout or "herdr command failed").strip()}
    try:
        return json.loads(r.stdout).get("result") or {}
    except ValueError:
        return {"_error": "invalid response from herdr"}


def do_resume(entry):
    """Reopen an archived chat: split near where it lived (or recreate its
    workspace) and launch the agent's native resume command. Returns an
    error string, or None on success."""
    if DEMO:
        return "demo mode — resume disabled"
    cmd = resume_cmd(entry)
    if not cmd:
        return "no resumable session ref for this chat"
    try:
        for rec in herdr_request("agent.list", {}).get("agents", []):
            sess = rec.get("agent_session") or {}
            live_key = (rec.get("agent"), sess.get("kind"), sess.get("value"))
            if live_key == session_key(entry):
                herdr_request("agent.focus", {"target": rec.get("pane_id")})
                return None
    except (OSError, RuntimeError, ValueError):
        pass
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
    result = _herdr_cli("pane", "run", new_pane, cmd)
    if result.get("_error"):
        _herdr_cli("pane", "close", new_pane)
        return "could not revive chat: %s" % result["_error"]
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


def _sel_key_of(line):
    kind, item = line
    if kind == "row":
        return ("row", item.get("pane_id"))
    return ("arch", item.get("sess_value") or item.get("closed"))


def _agent_labels_needed(lines):
    """Only spend screen space on agent kinds when they distinguish rows."""
    agents = {
        item.get("agent")
        for kind, item in lines
        if kind in ("row", "hist", "closed") and item.get("agent")
    }
    return len(agents) > 1


def chat_emoji(r):
    # Herdr's sidebar language: red=blocked, yellow=working,
    # teal/blue=finished-unseen, green=idle/clear.
    if r["status"] == "blocked":
        return "🔴"
    if r["rank"] == "1":
        return "🔵"
    if r["status"] == "working":
        return "🟡"
    if r["status"] == "idle":
        return "🟢"
    return "⚪"


_EMOJI_ORDER = ["🔴", "🔵", "🟡", "🟢", "⚫", "⚪"]


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
            tab_lines = []
            for cwd, chats in dirs.items():
                seen_dirs.add(cwd)
                closed = closed_by_dir.get((ws, cwd), [])[:CLOSED_PER_DIR]
                tab_lines.append(("pane", {"cwd": _short_cwd(chats[0]["cwd"]),
                                           "x": pane_x}))
                for c in chats:
                    c["_x"] = pane_x + 2
                    tab_lines.append(("row", c))
                for h in closed:
                    tab_lines.append(("closed", dict(h, x=pane_x + 2)))
            if multi_tab:  # a single tab adds no information
                tab_lines.insert(0, ("tab", {"label": tlabels.get(tab_id, tab_id or "?")}))
            ws_lines.extend(tab_lines)
        # Directories whose panes are gone but whose chats live on in the
        # archive — the Codex-app style accumulated list.
        for (aws, cwd), group in sorted(closed_by_dir.items(),
                                        key=lambda kv: -(kv[1][0].get("closed") or 0)):
            if aws != ws or not cwd or cwd in seen_dirs:
                continue
            ws_lines.append(("pane", {"cwd": _short_cwd(cwd), "x": pane_x}))
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
    c = {"attention": 0, "working": 0, "idle": 0}
    for r in rows:
        if r["rank"] in ("0", "1"):
            c["attention"] += 1
        elif r["rank"] == "2":
            c["working"] += 1
        else:
            c["idle"] += 1
    return "%(attention)d need attention · %(working)d working · %(idle)d idle" % c


def run(stdscr):
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    # Herdr's sidebar state language: blocked=red, working=yellow,
    # done-unseen=teal, idle=green, unknown=overlay0. Basic-color fallbacks
    # first, then the exact theme RGBs when the terminal has 256 colors.
    sel_pair = curses.A_REVERSE
    try:
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_RED, -1)      # blocked
        curses.init_pair(2, curses.COLOR_YELLOW, -1)   # working
        curses.init_pair(3, curses.COLOR_CYAN, -1)     # done / unread (teal)
        curses.init_pair(7, curses.COLOR_GREEN, -1)    # idle / clear
        curses.init_pair(4, curses.COLOR_CYAN, -1)     # agent names (blue)
        curses.init_pair(5, curses.COLOR_BLUE, -1)     # pane dirs (accent)
        curses.init_pair(6, curses.COLOR_MAGENTA, -1)  # workspace names (mauve)
        pal = theme_palette()
        if pal and curses.COLORS >= 256:
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
    stdscr.timeout(500)  # cheap live calls; keep external state feeling immediate

    prefs = load_prefs()
    mode = prefs.get("view")
    if mode not in VIEW_MODES:
        mode = "tree"
    hist_mode = False
    hist_query = ""
    search_edit = False
    sel = 0
    sel_key = None
    status_msg = ""
    rows = []

    while True:
        try:
            rows = load_agents()
            err = None
        except (OSError, RuntimeError, ValueError) as e:
            err = str(e)
        archive = load_hist_entries(rows)
        if hist_mode:
            lines = [("hist", e) for e in filter_history(archive, hist_query)]
        elif mode == "tree":
            lines = build_tree(rows, [])
        else:
            lines = build_lines(rows, mode)
        show_agent = _agent_labels_needed(lines)
        row_idx = [i for i, (kind, _) in enumerate(lines)
                   if kind in ("row", "hist", "closed")]
        if row_idx:
            sel = max(0, min(sel, len(row_idx) - 1))
            # Background refreshes re-sort the list; keep the selection on
            # the same item, not the same position.
            if sel_key is not None:
                for pos, li in enumerate(row_idx):
                    if _sel_key_of(lines[li]) == sel_key:
                        sel = pos
                        break

        h, w = stdscr.getmaxyx()
        stdscr.erase()
        if hist_mode:
            header = " Archive — %d of %d" % (len(lines), len(archive))
            if hist_query:
                header += " matching %r" % hist_query
            help_line = (" search: %s" % hist_query) if search_edit else \
                " /:search  enter/dbl-click:revive  u/h:back  q:quit"
        else:
            header = " Agent Inbox — %s" % counts_line(rows)
            help_line = (" enter:focus  a:archive idle/done  r:retitle"
                         "  g:view  u/h:archive  q:quit  |  right-click:archive")
        if err:
            header += "  (herdr unreachable — showing stale data)"
        try:
            stdscr.addnstr(0, 0, _wtrunc(header, w - 1).ljust(w - 1), 2 * w, curses.A_BOLD)
            stdscr.addnstr(h - 1, 0, _wtrunc(status_msg or help_line, w - 2), 2 * w,
                           curses.A_DIM)
        except curses.error:
            pass

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
            # Truncate and advance by DISPLAY width (emoji occupy 2 columns);
            # never write into the bottom-right cell (curses raises there).
            if x < w - 1 and s:
                cols = w - 1 - x
                s = _wtrunc(s, cols)
                try:
                    stdscr.addnstr(yy, x, s, cols * 2, attr)
                except curses.error:
                    pass
            return x + _wwidth(s)

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
                x = seg(yy, x, "  ", pick(curses.A_DIM, on))
                if show_agent:
                    x = seg(yy, x, item.get("agent", "?"),
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
                label = ("%s: %s" % (item.get("agent", "?"), item.get("title", ""))
                         if show_agent else item.get("title", ""))
                x = seg(yy, item.get("x", 7), label,
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
            icon = STATUS_ICON.get(r["status"], "?")
            attr = curses.A_NORMAL
            if r["status"] == "blocked":
                attr = curses.color_pair(1) | curses.A_BOLD
            elif r["rank"] == "1":
                attr = curses.color_pair(3) | curses.A_BOLD   # done/unread: teal
            elif r["status"] == "working":
                attr = curses.color_pair(2)                   # working: yellow
            selected = bool(row_idx and i == row_idx[sel])
            if mode == "tree":
                if selected:
                    stdscr.addnstr(yy, 0, " " * (w - 1), w - 1, sel_pair)
                x = r.get("_x", 7)
                if show_agent:
                    x = seg(yy, x, r["agent"],
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
                agent_space = len(r["agent"]) + 3 if show_agent else 0
                title = r["title"][: max(10, w - agent_space - 12)]
                x = seg(yy, 4, title, pick(attr, selected))
                if show_agent:
                    x = seg(yy, x, " — ", pick(curses.A_DIM, selected))
                    seg(yy, x, r["agent"], pick(curses.color_pair(4), selected))
                continue
            if mode == "grouped":
                meta = "%s · %s" % (r["agent"], r["age"]) if show_agent else r["age"]
            else:
                meta = ("%s · %s · %s" % (r["agent"], r["workspace"], r["age"])
                        if show_agent else "%s · %s" % (r["workspace"], r["age"]))
            indent = "   " if mode == "grouped" else " "
            avail = max(10, w - len(meta) - len(indent) - 7)
            text = "%s%s %-*s  %s" % (indent, icon, avail, r["title"][:avail], meta)
            attr = pick(attr, selected)
            stdscr.addnstr(top + y, 0, text.ljust(w - 1), w - 1, attr)
        stdscr.refresh()
        status_msg = ""
        if search_edit:
            try:
                curses.curs_set(1)
                stdscr.move(h - 1, min(w - 2, len(" search: ") + _wwidth(hist_query)))
            except curses.error:
                pass
        else:
            try:
                curses.curs_set(0)
            except curses.error:
                pass

        def send_op(op, row):
            try:
                resp = control_send({"cmd": op, "pane_id": row["pane_id"]})
                if not resp.get("ok"):
                    return " %s failed: %s" % (op, resp.get("error"))
                return ""
            except (OSError, ValueError) as e:
                return " daemon not reachable: %s" % e

        ch = stdscr.getch()
        if search_edit:
            if ch in (10, 13):
                search_edit = False
            elif ch == 27:
                search_edit = False
            elif ch in (curses.KEY_BACKSPACE, 127, 8):
                hist_query = hist_query[:-1]
            elif 32 <= ch <= 0x10ffff:
                hist_query += chr(ch)
            sel = 0
            sel_key = None
            continue
        if ch in (ord("q"), 27):
            return
        if ch == -1:
            continue  # timeout -> refresh
        if ch in (ord("h"), ord("u")):
            hist_mode = not hist_mode
            hist_query = ""
            search_edit = False
            sel = 0
            sel_key = None
            continue
        if ch == ord("/") and hist_mode:
            search_edit = True
            continue
        if ch == ord("g") and not hist_mode:
            mode = VIEW_MODES[(VIEW_MODES.index(mode) + 1) % len(VIEW_MODES)]
            prefs["view"] = mode
            save_prefs(prefs)
            status_msg = " view: %s" % mode
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
                sel_key = _sel_key_of(lines[i])
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
                status_msg = send_op("archive", row) or (" archived: %s" % row["title"])
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
            sel_key = _sel_key_of(lines[row_idx[sel]])
        elif ch in (ord("k"), curses.KEY_UP):
            sel = max(sel - 1, 0)
            sel_key = _sel_key_of(lines[row_idx[sel]])
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
        elif ch in (ord("a"), ord("r")):
            if cur_kind in ("hist", "closed"):
                status_msg = " archived chat — enter revives it"
                continue
            op = {"a": "archive", "r": "retitle"}[chr(ch)]
            status_msg = send_op(op, cur)


def main():
    os.umask(0o077)  # prefs/crash-log live in the private plugin state dir
    try:
        curses.wrapper(run)
    except KeyboardInterrupt:
        pass
    except Exception:
        import traceback
        d = os.path.join(os.path.dirname(herdr_socket_path()), "agent-inbox-state")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "tui-crash.log"), "a") as f:
            traceback.print_exc(file=f)
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())

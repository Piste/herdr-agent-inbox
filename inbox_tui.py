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
import socket
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
    try:
        for ws in herdr_request("workspace.list", {}).get("workspaces", []):
            ws_labels[ws.get("workspace_id")] = ws.get("label") or ws.get("workspace_id")
    except (OSError, RuntimeError, ValueError):
        pass
    rows = []
    for rec in agents:
        tokens = rec.get("tokens") or {}
        rows.append({
            "pane_id": rec.get("pane_id"),
            "workspace_id": rec.get("workspace_id"),
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


def build_lines(rows, grouped):
    """Returns a list of ('header', text) / ('row', row-dict) entries."""
    lines = []
    if not grouped:
        for r in rows:
            lines.append(("row", r))
        return lines
    by_ws = {}
    order = []
    for r in rows:
        if r["workspace_id"] not in by_ws:
            by_ws[r["workspace_id"]] = []
            order.append(r["workspace_id"])
        by_ws[r["workspace_id"]].append(r)
    for ws in order:
        group = by_ws[ws]
        lines.append(("header", "▾ %s" % group[0]["workspace"]))
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
    curses.init_pair(1, curses.COLOR_RED, -1)      # blocked
    curses.init_pair(2, curses.COLOR_YELLOW, -1)   # done / unread
    curses.init_pair(3, curses.COLOR_GREEN, -1)    # working
    curses.init_pair(4, curses.COLOR_CYAN, -1)     # headers
    curses.mousemask(curses.ALL_MOUSE_EVENTS)
    stdscr.timeout(2000)  # refresh every 2s when idle

    prefs = load_prefs()
    grouped = bool(prefs.get("grouped", True))  # Codex-style tree by default
    sel = 0
    status_msg = ""
    rows = []

    while True:
        try:
            rows = load_agents()
            err = None
        except (OSError, RuntimeError, ValueError) as e:
            err = str(e)
        lines = build_lines(rows, grouped)
        row_idx = [i for i, (kind, _) in enumerate(lines) if kind == "row"]
        if row_idx:
            sel = max(0, min(sel, len(row_idx) - 1))

        h, w = stdscr.getmaxyx()
        stdscr.erase()
        header = " Agent Inbox — %s" % counts_line(rows)
        stdscr.addnstr(0, 0, header.ljust(w - 1), w - 1, curses.A_BOLD)
        help_line = (" enter:focus  s:settle  u:unread  c:clear  S:settle-finished"
                     "  r:retitle  g:group  q:quit  |  right-click: settle/unsettle")
        stdscr.addnstr(h - 1, 0, (status_msg or help_line).ljust(w - 1), w - 1, curses.A_DIM)

        top = 1
        visible = h - 2
        # Scroll so selection stays on screen.
        sel_line = row_idx[sel] if row_idx else 0
        first = max(0, min(sel_line - visible // 2, len(lines) - visible))
        for y, i in enumerate(range(first, min(len(lines), first + visible))):
            kind, item = lines[i]
            if kind == "header":
                stdscr.addnstr(top + y, 0, " " + item, w - 1,
                               curses.color_pair(4) | curses.A_BOLD)
                continue
            r = item
            icon = "⚑" if r["rank"] == "5" else (
                "●" if r["flag"] == "●" else STATUS_ICON.get(r["status"], "?"))
            attr = curses.A_NORMAL
            if r["status"] == "blocked":
                attr = curses.color_pair(1) | curses.A_BOLD
            elif r["rank"] == "1":
                attr = curses.color_pair(2) | curses.A_BOLD
            elif r["status"] == "working":
                attr = curses.color_pair(3)
            elif r["rank"] == "5":
                attr = curses.A_DIM
            meta = ("%s · %s" % (r["agent"], r["age"]) if grouped
                    else "%s · %s · %s" % (r["agent"], r["workspace"], r["age"]))
            indent = "   " if grouped else " "
            avail = max(10, w - len(meta) - len(indent) - 7)
            text = "%s%s %-*s  %s" % (indent, icon, avail, r["title"][:avail], meta)
            if row_idx and i == row_idx[sel]:
                attr |= curses.A_REVERSE
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
        if ch == curses.KEY_MOUSE:
            try:
                _, mx, my, _, bstate = curses.getmouse()
            except curses.error:
                continue
            i = first + (my - top)
            if not (0 <= my - top < visible and 0 <= i < len(lines)):
                continue
            if lines[i][0] != "row":
                continue
            row = lines[i][1]
            if i in row_idx:
                sel = row_idx.index(i)
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
        cur = lines[row_idx[sel]][1]
        if ch in (ord("j"), curses.KEY_DOWN):
            sel = min(sel + 1, len(row_idx) - 1)
        elif ch in (ord("k"), curses.KEY_UP):
            sel = max(sel - 1, 0)
        elif ch == ord("g"):
            grouped = not grouped
            prefs["grouped"] = grouped
            save_prefs(prefs)
        elif ch == 10:  # enter
            try:
                herdr_request("agent.focus", {"target": cur["pane_id"]})
            except (OSError, RuntimeError) as e:
                status_msg = " focus failed: %s" % e
                continue
            return
        elif ch in (ord("s"), ord("u"), ord("r"), ord("c")):
            op = {"s": "settle", "u": "unread", "r": "retitle", "c": "clear"}[chr(ch)]
            status_msg = send_op(op, cur)
        elif ch == ord("S"):
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
    return 0


if __name__ == "__main__":
    sys.exit(main())

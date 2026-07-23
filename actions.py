#!/usr/bin/env python3
"""herdr-agent-inbox action dispatcher.

Invoked by herdr plugin actions (see herdr-plugin.toml). Reads the invocation
context from HERDR_PLUGIN_CONTEXT_JSON and forwards the command to the daemon's
control socket, starting the daemon first if it isn't running.

Usage: actions.py <settle|unread|retitle|settle-workspace> [pane_id]
       actions.py sidebar <+N|-N|N>

`sidebar` is herdr's missing runtime sidebar resize: herdr only sizes the
sidebar from config (ui.sidebar_width, clamped by min/max), so this rewrites
that one line in config.toml and asks the server to reload. Writes go through
os.path.realpath so a symlinked config (e.g. into dotfiles) is edited in
place rather than replaced by a regular file.
"""

import json
import os
import re
import socket
import subprocess
import sys
import time

SIDEBAR_FLOOR = 10
SIDEBAR_CEIL = 80


def state_dir():
    # Must match daemon.py: derived from the session socket, not plugin env.
    sock = os.environ.get("HERDR_SOCKET_PATH") or os.path.expanduser(
        "~/.config/herdr/herdr.sock"
    )
    return os.path.join(os.path.dirname(sock), "agent-inbox-state")


def control_send(cmd):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(3.0)
    try:
        s.connect(os.path.join(state_dir(), "control.sock"))
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


def ensure_daemon():
    here = os.path.dirname(os.path.abspath(__file__))
    subprocess.run(["/bin/sh", os.path.join(here, "scripts", "ensure-daemon.sh")],
                   check=False)


def notify(title, body=None, sound=None):
    herdr = os.environ.get("HERDR_BIN_PATH", "herdr")
    argv = [herdr, "notification", "show", title]
    if body:
        argv += ["--body", body]
    if sound:
        argv += ["--sound", sound]
    subprocess.run(argv, check=False, capture_output=True)


def config_path():
    p = os.environ.get("HERDR_CONFIG_PATH") or os.path.expanduser(
        "~/.config/herdr/config.toml"
    )
    return os.path.realpath(p)


def sidebar_resize(spec):
    """spec: '+2', '-2', or an absolute column count like '30'.

    Herdr computes the effective sidebar width as
    clamp(auto_scale_from_content, sidebar_min_width, sidebar_max_width);
    `sidebar_width` is only a launch-time default and changing it alone does
    nothing to a live client (verified on 0.7.5). The clamps DO re-apply on
    reload-config, so resizing pins min = max = target for an exact width.
    Restore auto-scaling by hand-editing min/max apart again.
    """
    path = config_path()
    with open(path, "r") as f:
        text = f.read()

    def get(key):
        m = re.search(r"(?m)^(\s*%s\s*=\s*)(\d+)" % key, text)
        return m, (int(m.group(2)) if m else None)

    m_w, width = get("sidebar_width")
    m_min, cur_min = get("sidebar_min_width")
    m_max, cur_max = get("sidebar_max_width")
    if not (m_w and m_min and m_max):
        return None, "sidebar_width/min/max not all present in %s" % path

    # Once pinned, min == max == current width; before that, sidebar_width
    # is the best nominal baseline we have.
    base = cur_min if cur_min == cur_max else width
    if spec.startswith(("+", "-")):
        new = base + int(spec)
    else:
        new = int(spec)
    new = max(SIDEBAR_FLOOR, min(SIDEBAR_CEIL, new))
    if new == base == cur_min == cur_max:
        return base, None

    # Replace right-to-left so earlier match offsets stay valid.
    for m in sorted((m_w, m_min, m_max), key=lambda m: -m.start(2)):
        text = text[: m.start(2)] + str(new) + text[m.end(2):]

    tmp = path + ".agent-inbox.tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)

    herdr = os.environ.get("HERDR_BIN_PATH", "herdr")
    r = subprocess.run([herdr, "server", "reload-config"],
                       capture_output=True, text=True)
    try:
        diags = json.loads(r.stdout)["result"].get("diagnostics") or []
    except (ValueError, KeyError):
        diags = ["reload-config failed: %s" % (r.stderr or r.stdout)]
    return new, ("; ".join(diags) if diags else None)


def main():
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    op = sys.argv[1]

    if op == "sidebar":
        if len(sys.argv) < 3:
            print("usage: actions.py sidebar <+N|-N|N>", file=sys.stderr)
            return 2
        try:
            new, err = sidebar_resize(sys.argv[2])
        except (OSError, ValueError) as e:
            new, err = None, str(e)
        if err:
            notify("agent-inbox: sidebar resize failed", body=err)
            return 1
        return 0
    ctx = {}
    try:
        ctx = json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON") or "{}")
    except ValueError:
        pass
    pane_id = sys.argv[2] if len(sys.argv) > 2 else ctx.get("focused_pane_id")
    workspace_id = ctx.get("workspace_id")

    cmd = {"cmd": op, "pane_id": pane_id, "workspace_id": workspace_id}
    resp = None
    for attempt in (1, 2):
        try:
            resp = control_send(cmd)
            break
        except (OSError, ValueError):
            if attempt == 1:
                ensure_daemon()
                time.sleep(0.8)
    if resp is None:
        notify("agent-inbox: daemon not reachable", body="see plugin state dir daemon.log")
        return 1
    if not resp.get("ok"):
        notify("agent-inbox: %s failed" % op, body=str(resp.get("error", "")))
        return 1

    label = resp.get("title") or ""
    if op == "settle":
        notify("⚑ settled", body=label or None)
    elif op == "unread":
        notify("● marked unread", body=label or None)
    elif op == "settle-workspace":
        notify("⚑ settled workspace", body=label or None)
    return 0


if __name__ == "__main__":
    sys.exit(main())

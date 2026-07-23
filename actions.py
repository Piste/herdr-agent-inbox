#!/usr/bin/env python3
"""herdr-agent-inbox action dispatcher.

Invoked by herdr plugin actions (see herdr-plugin.toml). Reads the invocation
context from HERDR_PLUGIN_CONTEXT_JSON and forwards the command to the daemon's
control socket, starting the daemon first if it isn't running.

Usage: actions.py <settle|unread|retitle|settle-workspace> [pane_id]
"""

import json
import os
import socket
import subprocess
import sys
import time


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


def main():
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    op = sys.argv[1]
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

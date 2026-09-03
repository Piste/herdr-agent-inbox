#!/usr/bin/env python3
"""herdr-agent-inbox action dispatcher.

Invoked by herdr plugin actions (see herdr-plugin.toml). Reads the invocation
context from HERDR_PLUGIN_CONTEXT_JSON and forwards the command to the daemon's
control socket, starting the daemon first if it isn't running.

Usage: actions.py <archive|retitle> [pane_id]
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
# Bounds left in config after a keyboard resize, so herdr's NATIVE mouse drag
# (grab the sidebar's last column) keeps a useful range to move within.
DRAG_MIN = 20
DRAG_MAX = 60


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


def _live_sidebar_width():
    """Best-known current width. session.json holds the live value (mouse
    drags land there) but herdr saves it lazily; our own config writes are
    instant. Trust whichever file is fresher."""
    sess = os.path.join(
        os.path.dirname(os.environ.get("HERDR_SOCKET_PATH")
                        or os.path.expanduser("~/.config/herdr/herdr.sock")),
        "session.json",
    )
    sess_w = cfg_w = None
    try:
        with open(sess) as f:
            w = json.load(f).get("sidebar_width")
        sess_w = int(w) if w else None
    except (OSError, ValueError, TypeError):
        pass
    try:
        with open(config_path()) as f:
            m = re.search(r"(?m)^\s*sidebar_width\s*=\s*(\d+)", f.read())
        cfg_w = int(m.group(1)) if m else None
    except OSError:
        pass
    if sess_w is not None and cfg_w is not None:
        try:
            newer_sess = os.path.getmtime(sess) > os.path.getmtime(config_path())
        except OSError:
            newer_sess = True
        return sess_w if newer_sess else cfg_w
    return sess_w if sess_w is not None else cfg_w


def _write_sidebar_bounds(width, mn, mx):
    path = config_path()
    with open(path, "r") as f:
        text = f.read()

    matches = {}
    for key in ("sidebar_width", "sidebar_min_width", "sidebar_max_width"):
        m = re.search(r"(?m)^(\s*%s\s*=\s*)(\d+)" % key, text)
        if not m:
            return "missing %s in %s" % (key, path)
        matches[key] = m
    values = {"sidebar_width": width, "sidebar_min_width": mn,
              "sidebar_max_width": mx}
    # Replace right-to-left so earlier match offsets stay valid.
    for key, m in sorted(matches.items(), key=lambda kv: -kv[1].start(2)):
        text = text[: m.start(2)] + str(values[key]) + text[m.end(2):]

    tmp = path + ".agent-inbox.tmp"
    mode = os.stat(path).st_mode & 0o7777
    with open(tmp, "w") as f:
        f.write(text)
    os.chmod(tmp, mode)  # preserve the user's own config.toml permissions
    os.replace(tmp, path)

    herdr = os.environ.get("HERDR_BIN_PATH", "herdr")
    r = subprocess.run([herdr, "server", "reload-config"],
                       capture_output=True, text=True)
    try:
        diags = json.loads(r.stdout)["result"].get("diagnostics") or []
    except (ValueError, KeyError, TypeError, AttributeError):
        diags = ["reload-config failed: %s" % (r.stderr or r.stdout)]
    return "; ".join(diags) if diags else None


def sidebar_resize(spec):
    """spec: '+2', '-2', or an absolute column count like '30'.

    Herdr resize semantics (verified on 0.7.5): the live width persists in
    session state and follows config only when config-owned, BUT the min/max
    clamps re-apply to it on every reload-config. So: phase 1 pins
    width=min=max=target (forcing the live width), phase 2 relaxes the
    bounds back to [DRAG_MIN, DRAG_MAX] so herdr's native mouse drag on the
    sidebar divider (its last column) keeps room to move.
    """
    # Serialize concurrent resize invocations (rapid keypresses) — an
    # unlocked read-modify-write of config.toml would interleave.
    import fcntl
    lock_dir = state_dir()
    os.makedirs(lock_dir, exist_ok=True)
    lock = open(os.path.join(lock_dir, "resize.lock"), "a")
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        base = _live_sidebar_width()
        if base is None:
            base = 34
        if spec.startswith(("+", "-")):
            new = base + int(spec)
        else:
            new = int(spec)
        new = max(SIDEBAR_FLOOR, min(SIDEBAR_CEIL, new))

        err = _write_sidebar_bounds(new, new, new)      # force the live width
        if err:
            # Never leave the bounds pinned — that would disable herdr's
            # native mouse drag on the sidebar divider.
            _write_sidebar_bounds(new, min(DRAG_MIN, new), max(DRAG_MAX, new))
            return None, err
        err = _write_sidebar_bounds(new, min(DRAG_MIN, new), max(DRAG_MAX, new))
        return new, err
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


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
    if op == "set-title" and len(sys.argv) > 3:
        cmd["title"] = sys.argv[3]
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
    if op == "archive":
        notify("Archived agent", body=label or None)
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Shared exact-session restore helpers for Agent Inbox.

Both the popup (manual revive, with focus) and the daemon (snooze wake, without
focus) use this module so their safety and identity behavior cannot drift.
"""

import json
import os
import shlex
import socket
import subprocess
import time


def herdr_socket_path():
    return os.environ.get("HERDR_SOCKET_PATH") or os.path.expanduser(
        "~/.config/herdr/herdr.sock"
    )


def herdr_request(method, params, timeout=6.0):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(herdr_socket_path())
        sock.sendall((json.dumps({"id": "agent-inbox-restore", "method": method,
                                  "params": params}) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = sock.recv(1 << 16)
            if not chunk:
                break
            buf += chunk
    finally:
        sock.close()
    if not buf:
        raise RuntimeError("herdr closed the connection without replying")
    try:
        response = json.loads(buf)
    except ValueError as exc:
        raise RuntimeError("bad response from herdr: %s" % exc)
    if "error" in response:
        raise RuntimeError(str(response["error"]))
    return response.get("result") or {}


def herdr_cli(*args):
    herdr = os.environ.get("HERDR_BIN_PATH", "herdr")
    result = subprocess.run([herdr, *args], capture_output=True, text=True)
    if result.returncode:
        return {"_error": (result.stderr or result.stdout
                            or "herdr command failed").strip()}
    if not result.stdout.strip():
        # Mutating helpers such as `pane run` may succeed without a JSON body.
        return {}
    try:
        return json.loads(result.stdout).get("result") or {}
    except ValueError:
        return {"_error": "invalid response from herdr"}


def session_key(item):
    value = item.get("sess_value")
    return (item.get("agent"), item.get("sess_kind"), value) if value else None


def resume_cmd(entry):
    """Command that reopens this chat in its native CLI, or None."""
    agent = entry.get("agent")
    kind, value = entry.get("sess_kind"), entry.get("sess_value")
    if not value:
        return None
    if agent == "claude" and kind == "id":
        return "claude --resume %s" % shlex.quote(value)
    if agent == "pi" and kind == "path":
        return "pi --session %s" % shlex.quote(value)
    if agent == "codex" and kind == "id":
        # A popup opened from another Codex pane can inherit that pane's id.
        # The resumed child must let its own exact identity win.
        return "env -u CODEX_THREAD_ID codex resume %s" % shlex.quote(value)
    return None


def report_resumed_session(entry, pane_id):
    """Attach the exact verified native session reference after launch."""
    agent = entry.get("agent")
    kind = entry.get("sess_kind")
    value = entry.get("sess_value")
    if agent not in ("codex", "claude", "pi") or kind not in ("id", "path") \
            or not value:
        return False
    params = {
        "pane_id": pane_id,
        "source": "herdr:%s" % agent,
        "agent": agent,
        "seq": time.time_ns(),
        "session_start_source": "resume",
    }
    params["agent_session_id" if kind == "id" else "agent_session_path"] = value
    try:
        herdr_request("pane.report_agent_session", params)
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def resume_session(entry, focus=True):
    """Resume entry once and return a structured result.

    Manual revives pass focus=True. Scheduled snooze wakes pass focus=False,
    which explicitly preserves the user's current Herdr location.
    """
    command = resume_cmd(entry)
    if not command:
        return {"ok": False, "error": "no resumable session ref for this chat"}

    # A scheduled wake may have opened its pane but not reached detectable
    # agent state yet. Never create a second copy; manual revive focuses the
    # pane so the user can see or repair it.
    waking_pane = entry.get("wake_pane_id")
    if waking_pane and herdr_cli("pane", "get", waking_pane).get("pane"):
        if focus:
            try:
                herdr_request("pane.focus", {"pane_id": waking_pane})
            except (OSError, RuntimeError, ValueError) as exc:
                return {"ok": False, "pane_id": waking_pane,
                        "error": "could not focus waking chat: %s" % exc}
        return {"ok": True, "pane_id": waking_pane, "already_live": True}

    try:
        agents = herdr_request("agent.list", {}).get("agents", [])
    except (OSError, RuntimeError, ValueError) as exc:
        # Fail closed: without a trustworthy live-session list we cannot prove
        # that another copy of this exact conversation is not already open.
        return {"ok": False, "error": "could not check live sessions: %s" % exc}
    for rec in agents:
        session = rec.get("agent_session") or {}
        live_key = (rec.get("agent"), session.get("kind"), session.get("value"))
        if live_key == session_key(entry):
            pane_id = rec.get("pane_id")
            if focus:
                try:
                    herdr_request("agent.focus", {"target": pane_id})
                except (OSError, RuntimeError, ValueError) as exc:
                    return {"ok": False, "pane_id": pane_id,
                            "error": "could not focus live chat: %s" % exc}
            return {"ok": True, "pane_id": pane_id, "already_live": True}

    target = None
    old_pane = entry.get("pane_id")
    if old_pane and herdr_cli("pane", "get", old_pane).get("pane"):
        target = old_pane
    if not target and entry.get("workspace_id"):
        panes = herdr_cli("pane", "list", "--workspace",
                          entry["workspace_id"]).get("panes") or []
        if panes:
            target = panes[0].get("pane_id")

    focus_flag = "--focus" if focus else "--no-focus"
    new_pane = None
    if target:
        args = ["pane", "split", target, "--direction", "down", focus_flag]
        if entry.get("cwd"):
            args += ["--cwd", entry["cwd"]]
        split = herdr_cli(*args)
        if split.get("_error"):
            return {"ok": False, "error": split["_error"]}
        new_pane = (split.get("pane") or {}).get("pane_id")
    else:
        args = ["workspace", "create", focus_flag]
        if entry.get("cwd"):
            args += ["--cwd", entry["cwd"]]
        if entry.get("workspace"):
            args += ["--label", entry["workspace"]]
        created = herdr_cli(*args)
        if created.get("_error"):
            return {"ok": False, "error": created["_error"]}
        new_pane = (created.get("root_pane") or {}).get("pane_id")
        if not new_pane:
            workspace = (created.get("workspace") or {}).get("workspace_id")
            if workspace:
                panes = herdr_cli("pane", "list", "--workspace", workspace).get(
                    "panes") or []
                if panes:
                    new_pane = panes[0].get("pane_id")

    if not new_pane:
        return {"ok": False, "error": "could not open a pane to resume into"}
    launched = herdr_cli("pane", "run", new_pane, command)
    if launched.get("_error"):
        herdr_cli("pane", "close", new_pane)
        return {"ok": False, "error": "could not revive chat: %s"
                % launched["_error"]}

    reported = report_resumed_session(entry, new_pane)
    if focus:
        try:
            # Reassert after launch: the popup owns focus during split/create.
            herdr_request("pane.focus", {"pane_id": new_pane})
        except (OSError, RuntimeError, ValueError) as exc:
            return {"ok": False, "pane_id": new_pane,
                    "error": "chat revived, but could not focus it: %s" % exc}
    return {"ok": True, "pane_id": new_pane, "already_live": False,
            "session_reported": reported}

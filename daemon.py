#!/usr/bin/env python3
"""herdr-agent-inbox daemon.

Watches herdr's agent panes and decorates them with inbox metadata:

- pane token/metadata `title`: session title derived from the agent's native
  session transcript (first real user prompt), like ChatGPT/Claude chat titles.
- pane token `rank`: inbox tier (blocked=0, done/unread=1, working=2, idle=3,
  unknown=4, settled=5) used by an `agent.view.set` sort so the Agents panel
  behaves like an inbox: attention on top, settled slides to the bottom.
- pane tokens `age` (session running time) and `since` (time in current state).
- pane token `flag`: "⚑" settled / "●" marked unread.
- workspace tokens `agents` (per-status counts, e.g. "!1 ▸2 ✓1 ⚑1") and
  `busy` (longest currently-working stint).

Settle / mark-unread commands arrive on a control socket (see actions.py).
State persists across herdr server restarts keyed by terminal_id.

Stdlib only. One herdr request per connection (the server closes the socket
after each response); only events.subscribe holds a long-lived connection.
"""

import fcntl
import glob
import hashlib
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time

try:
    import tomllib
except ImportError:  # < 3.11
    tomllib = None

SOURCE = "herdr-agent-inbox"
TICK_SECS = 30.0
DEBOUNCE_SECS = 0.4
TITLE_MAX = 56
STATE_KEEP_SECS = 3 * 24 * 3600  # keep state for vanished terminals 3 days

RANK_BLOCKED = "0"
RANK_ATTENTION = "1"   # done (finished, unseen) or manually marked unread
RANK_WORKING = "2"
RANK_IDLE = "3"
RANK_UNKNOWN = "4"
RANK_SETTLED = "5"

FLAG_SETTLED = "⚑"   # ⚑
FLAG_UNREAD = "●"    # ●

VIEW_SORT = [
    {"field": {"token": "rank"}, "order": "asc"},
    {"field": "state_change_seq", "order": "desc"},
]


def herdr_socket_path():
    p = os.environ.get("HERDR_SOCKET_PATH")
    if p:
        return p
    return os.path.expanduser("~/.config/herdr/herdr.sock")


def state_dir():
    # Deliberately NOT $HERDR_PLUGIN_STATE_DIR: the daemon may be started from
    # a shell (no plugin env) or from a hook (plugin env); deriving the path
    # from the session socket keeps every entry point on the same control
    # socket and state file.
    p = os.path.join(os.path.dirname(herdr_socket_path()), "agent-inbox-state")
    os.makedirs(p, exist_ok=True)
    return p


def config_file():
    d = os.environ.get("HERDR_PLUGIN_CONFIG_DIR") or os.path.join(
        os.path.dirname(herdr_socket_path()), "plugins", "config", "herdr-agent-inbox"
    )
    return os.path.join(d, "config.toml")


DEFAULT_CONFIG = {
    "title_source": "first",       # "first" | "last" user prompt
    "summarize": False,            # pipe the prompt through summarize_cmd
    "summarize_cmd": "",           # shell cmd: prompt on stdin -> title on stdout
    "summarize_timeout_secs": 60,
}

CONFIG_TEMPLATE = """\
# herdr-agent-inbox configuration

[title]
# Which user prompt seeds the session title:
#   "first" — the opening request, like ChatGPT/Claude chat titles
#   "last"  — the most recent request; re-derived when the agent finishes a turn
source = "first"

# Produce a meaningful title by piping the prompt through a command instead of
# showing the (truncated) raw prompt. The command receives the prompt text on
# stdin and must print a short title on stdout.
summarize = false
summarize_cmd = "claude -p --model claude-haiku-4-5-20251001 'Write a 4-8 word title for this coding-agent session request. Output ONLY the title, nothing else.'"
summarize_timeout_secs = 60
"""


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    path = config_file()
    if not os.path.exists(path):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(CONFIG_TEMPLATE)
        except OSError:
            pass
        return cfg
    if tomllib is None:
        return cfg
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
        title = data.get("title") or {}
        src = title.get("source")
        if src in ("first", "last"):
            cfg["title_source"] = src
        cfg["summarize"] = bool(title.get("summarize", False))
        cmd = title.get("summarize_cmd")
        if isinstance(cmd, str):
            cfg["summarize_cmd"] = cmd
        t = title.get("summarize_timeout_secs")
        if isinstance(t, int) and 5 <= t <= 600:
            cfg["summarize_timeout_secs"] = t
    except (OSError, ValueError) as e:
        sys.stderr.write("config load failed: %s\n" % e)
    return cfg


class Log:
    def __init__(self, path):
        self.path = path
        try:
            if os.path.exists(path) and os.path.getsize(path) > 1_000_000:
                os.replace(path, path + ".1")
        except OSError:
            pass

    def __call__(self, msg):
        line = "%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
        try:
            with open(self.path, "a") as f:
                f.write(line)
        except OSError:
            pass


def herdr_request(method, params, timeout=10.0):
    """One request on a fresh connection; the server closes it after replying."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(herdr_socket_path())
        payload = json.dumps({"id": "inbox", "method": method, "params": params})
        s.sendall((payload + "\n").encode())
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
        raise RuntimeError("%s: %s" % (method, resp["error"]))
    return resp.get("result") or {}


def fmt_dur(secs):
    secs = max(0, int(secs))
    if secs < 60:
        return "%ds" % secs
    if secs < 3600:
        return "%dm" % (secs // 60)
    if secs < 86400:
        h, m = divmod(secs // 60, 60)
        return "%dh%02dm" % (h, m) if m else "%dh" % h
    d, h = divmod(secs // 3600, 24)
    return "%dd%dh" % (d, h) if h else "%dd" % d


# ---------------------------------------------------------------- titles ----

_WS_RE = re.compile(r"\s+")
_TAGLINE_RE = re.compile(r"<[^>]{1,80}>")


def _clean_title(text):
    if not text:
        return None
    m = re.search(r"<command-args>(.*?)</command-args>", text, re.S)
    if m and m.group(1).strip():
        text = m.group(1)
    elif re.search(r"<(command-name|local-command-stdout|local-command-caveat)>", text):
        # A slash-command record with no real prompt text — not title material.
        return None
    # Drop system reminders, xml-ish noise, and URLs (they make lousy titles).
    text = re.sub(r"<system-reminder>.*?</system-reminder>", " ", text, flags=re.S)
    text = _TAGLINE_RE.sub(" ", text)
    text = re.sub(r"\(?\bhttps?://\S+\)?", " ", text)
    text = _WS_RE.sub(" ", text).strip()
    if not text or text.startswith("Caveat:"):
        return None
    if len(text) < 4:
        return None
    if len(text) > TITLE_MAX:
        cut = text[:TITLE_MAX]
        if " " in cut[20:]:
            cut = cut[: cut.rfind(" ")]
        text = cut.rstrip(" ,;:.") + "…"
    return text


def _texts_from_content(content):
    out = []
    if isinstance(content, str):
        out.append(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                t = part.get("text") or part.get("input_text")
                if isinstance(t, str):
                    out.append(t)
    return out


def _user_texts(obj):
    """Find user-authored text in one transcript JSON object (any agent format)."""
    if not isinstance(obj, dict):
        return []
    # claude-code: {"type":"user","message":{"role":"user","content":...}}
    # pi:          {"type":"message","message":{"role":"user","content":[...]}}
    msg = obj.get("message")
    if isinstance(msg, dict) and msg.get("role") == "user":
        if obj.get("isSidechain"):
            return []
        return _texts_from_content(msg.get("content"))
    # codex rollouts: {"type":"response_item","payload":{"role":"user","content":[...]}}
    payload = obj.get("payload")
    if isinstance(payload, dict) and payload.get("role") == "user":
        return _texts_from_content(payload.get("content"))
    if obj.get("role") == "user":
        return _texts_from_content(obj.get("content"))
    return []


def _scan_lines_for_prompt(lines, want_last):
    """Returns (clean_title, raw_text). For want_last, feed reversed lines."""
    summary = None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "summary" and obj.get("summary"):
            summary = summary or _clean_title(str(obj["summary"]))
            continue
        for text in _user_texts(obj):
            if "<system-reminder>" in text and "</system-reminder>" not in text:
                continue
            title = _clean_title(text)
            if title:
                raw = _raw_prompt(text)
                return (title if want_last else (summary or title)), raw
    return summary, None


def _raw_prompt(text):
    """The prompt as summarizer input: unwrapped but untruncated."""
    m = re.search(r"<command-args>(.*?)</command-args>", text, re.S)
    if m and m.group(1).strip():
        text = m.group(1)
    text = re.sub(r"<system-reminder>.*?</system-reminder>", " ", text, flags=re.S)
    text = _WS_RE.sub(" ", text).strip()
    return text[:4000]


def _tail_lines(path, max_bytes=512 * 1024):
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        f.seek(max(0, size - max_bytes))
        data = f.read()
    lines = data.split(b"\n")
    if size > max_bytes and lines:
        lines = lines[1:]  # drop the partial first line
    return [ln.decode("utf-8", "replace") for ln in lines]


def title_from_transcript(path, source="first"):
    """(clean_title, raw_prompt) from a session transcript.

    source "first": the opening user prompt (preferring claude summary lines).
    source "last":  the most recent user prompt (tail-scanned).
    """
    try:
        if source == "last":
            # Transcript lines can be huge (base64 images in tool results), so
            # widen the tail window until a user prompt shows up.
            size = os.path.getsize(path)
            cap = 512 * 1024
            while True:
                title, raw = _scan_lines_for_prompt(
                    reversed(_tail_lines(path, cap)), True)
                if title or cap >= size:
                    return title, raw
                cap *= 4
        with open(path, "r", errors="replace") as f:
            head = []
            for i, line in enumerate(f):
                if i > 400:
                    break
                head.append(line)
        return _scan_lines_for_prompt(head, False)
    except OSError:
        return None, None


def _munge_claude_cwd(cwd):
    return re.sub(r"[/.]", "-", cwd)


def resolve_transcript(agent_rec):
    """Locate the native session transcript for an agent pane, if any."""
    sess = agent_rec.get("agent_session")
    if not isinstance(sess, dict):
        return None
    kind, value = sess.get("kind"), sess.get("value")
    if not value:
        return None
    if kind == "path":
        return value if os.path.exists(value) else None
    if kind == "id":
        agent = agent_rec.get("agent")
        if agent == "claude":
            cwd = agent_rec.get("cwd") or ""
            p = os.path.expanduser(
                "~/.claude/projects/%s/%s.jsonl" % (_munge_claude_cwd(cwd), value)
            )
            if os.path.exists(p):
                return p
            hits = glob.glob(os.path.expanduser("~/.claude/projects/*/%s.jsonl" % value))
            return hits[0] if hits else None
        if agent == "codex":
            hits = glob.glob(
                os.path.expanduser("~/.codex/sessions/**/*%s*.jsonl" % value),
                recursive=True,
            )
            return hits[0] if hits else None
    return None


# ---------------------------------------------------------------- daemon ----


class InboxDaemon:
    def __init__(self):
        self.dir = state_dir()
        self.log = Log(os.path.join(self.dir, "daemon.log"))
        self.state_path = os.path.join(self.dir, "state.json")
        self.control_path = os.path.join(self.dir, "control.sock")
        self.lock = threading.RLock()
        self.dirty = threading.Event()
        self.stop = threading.Event()
        self.terminals = {}       # terminal_id -> persisted per-agent state
        self.last_report = {}     # terminal_id -> {"title":..., "tokens": {...}}
        self.ws_report = {}       # workspace_id -> tokens dict last reported
        self.pane_to_tid = {}     # pane_id -> terminal_id (from last refresh)
        self.cfg = load_config()
        self.cfg_mtime = self._cfg_mtime()
        self.sum_q = queue.Queue()
        self.sum_inflight = set()  # (tid, hash) queued or running
        self.sum_failed = set()    # hashes that failed this run; don't retry
        self._load()

    # -- persistence --

    def _load(self):
        try:
            with open(self.state_path) as f:
                data = json.load(f)
            self.terminals = data.get("terminals", {})
        except (OSError, ValueError):
            self.terminals = {}

    def _save(self):
        tmp = self.state_path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump({"terminals": self.terminals}, f)
            os.replace(tmp, self.state_path)
        except OSError as e:
            self.log("state save failed: %s" % e)

    # -- inbox model --

    @staticmethod
    def rank_for(status, settled, unread):
        # `settled` outranks `done`: settling is an explicit "I'm finished with
        # this thread". Fresh activity clears it (working/blocked transition),
        # so anything genuinely new still surfaces.
        if status == "blocked":
            return RANK_BLOCKED
        if unread:
            return RANK_ATTENTION
        if status == "working":
            return RANK_WORKING
        if settled:
            return RANK_SETTLED
        if status == "done":
            return RANK_ATTENTION
        if status == "idle":
            return RANK_IDLE
        return RANK_UNKNOWN

    def assert_view(self):
        try:
            herdr_request(
                "agent.view.set",
                {"source": SOURCE, "label": "Inbox", "sort": VIEW_SORT},
            )
            self.log("agent view asserted")
        except (OSError, RuntimeError) as e:
            self.log("agent.view.set failed: %s" % e)

    def _cfg_mtime(self):
        try:
            return os.path.getmtime(config_file())
        except OSError:
            return 0

    def _maybe_reload_config(self):
        m = self._cfg_mtime()
        if m == self.cfg_mtime:
            return
        self.cfg_mtime = m
        old = self.cfg
        self.cfg = load_config()
        if (old["title_source"], old["summarize"], old["summarize_cmd"]) != (
            self.cfg["title_source"], self.cfg["summarize"], self.cfg["summarize_cmd"]
        ):
            self.log("config changed: source=%s summarize=%s — retitling all"
                     % (self.cfg["title_source"], self.cfg["summarize"]))
            with self.lock:
                for st in self.terminals.values():
                    st["title"] = None
                    st["title_tried"] = 0
                    st["sum_hash"] = None
            self.sum_failed.clear()

    def _ensure_title(self, rec, st, now):
        """Generate/refresh the session title for one agent record."""
        sess = rec.get("agent_session") or {}
        sess_key = "%s:%s:%s" % (sess.get("kind"), sess.get("value"),
                                 self.cfg["title_source"])
        fresh = st.get("title_sess") == sess_key
        if st.get("title") and fresh and not st.get("title_stale"):
            return
        # Retry transcripts at most once per tick; they appear shortly after
        # the first prompt is sent.
        if st.get("title_tried", 0) > now - (TICK_SECS - 1) and fresh and not st.get("title_stale"):
            return
        st["title_tried"] = now
        if not fresh:
            st["title_sess"] = sess_key
            st["title"] = None
            st["sum_hash"] = None
        st["title_stale"] = False
        path = resolve_transcript(rec)
        if not path:
            if not st.get("title"):
                st["title"] = None
            return
        title, raw = title_from_transcript(path, self.cfg["title_source"])
        if not title:
            return
        summarize = bool(self.cfg["summarize"] and self.cfg["summarize_cmd"] and raw)
        if not summarize:
            if title != st.get("title"):
                st["title"] = title
                self.log("title %s -> %r" % (rec.get("pane_id"), title))
            return
        h = hashlib.sha1(raw.encode()).hexdigest()[:12]
        if st.get("sum_hash") == h and st.get("title"):
            return  # already summarized this content
        if not st.get("title"):
            st["title"] = title  # heuristic placeholder until the summary lands
        tid = rec.get("terminal_id")
        if h in self.sum_failed or (tid, h) in self.sum_inflight:
            return
        self.sum_inflight.add((tid, h))
        self.sum_q.put((tid, h, rec.get("agent"), rec.get("cwd"), raw))

    def summarize_loop(self):
        env = dict(os.environ)
        env["PATH"] = env.get("PATH", "") + ":/opt/homebrew/bin:/usr/local/bin"
        while not self.stop.is_set():
            try:
                tid, h, agent, cwd, raw = self.sum_q.get(timeout=1.0)
            except queue.Empty:
                continue
            payload = "Agent: %s\nWorkdir: %s\n\nRequest:\n%s" % (agent, cwd, raw)
            title = None
            try:
                r = subprocess.run(
                    self.cfg["summarize_cmd"], shell=True, env=env,
                    input=payload, capture_output=True, text=True,
                    timeout=self.cfg["summarize_timeout_secs"],
                )
                if r.returncode == 0:
                    title = _clean_title(r.stdout.strip().splitlines()[0] if r.stdout.strip() else "")
                else:
                    self.log("summarize_cmd rc=%d: %s" % (r.returncode, (r.stderr or "")[:200]))
            except (OSError, subprocess.TimeoutExpired) as e:
                self.log("summarize failed: %s" % e)
            with self.lock:
                self.sum_inflight.discard((tid, h))
                st = self.terminals.get(tid)
                if title and st:
                    st["title"] = title
                    st["sum_hash"] = h
                    self.log("summary %s -> %r" % (tid, title))
                    self.dirty.set()
                elif not title:
                    self.sum_failed.add(h)

    def refresh(self):
        now = time.time()
        self._maybe_reload_config()
        try:
            agents = herdr_request("agent.list", {}).get("agents", [])
        except (OSError, RuntimeError, ValueError) as e:
            self.log("agent.list failed: %s" % e)
            return
        with self.lock:
            self.pane_to_tid = {}
            seen_tids = set()
            ws_agents = {}
            for rec in agents:
                tid = rec.get("terminal_id")
                pane_id = rec.get("pane_id")
                if not tid or not pane_id:
                    continue
                seen_tids.add(tid)
                self.pane_to_tid[pane_id] = tid
                st = self.terminals.get(tid)
                if st is None or st.get("agent") != rec.get("agent"):
                    st = {
                        "agent": rec.get("agent"),
                        "first_seen": now,
                        "last_status": rec.get("agent_status"),
                        "last_change": now,
                        "settled": False,
                        "unread": False,
                        "flagged_at": 0,
                        "title": None,
                        "title_sess": None,
                        "title_tried": 0,
                    }
                    self.terminals[tid] = st
                status = rec.get("agent_status") or "unknown"
                if status != st.get("last_status"):
                    st["last_change"] = now
                    st["last_status"] = status
                    if status in ("working", "blocked"):
                        # New activity reopens the item, Theo-style.
                        st["settled"] = False
                        st["unread"] = False
                    elif self.cfg["title_source"] == "last":
                        # Turn ended — the latest prompt may have changed.
                        st["title_stale"] = True
                        st["title_tried"] = 0
                st["gone_since"] = None
                self._ensure_title(rec, st, now)

                title = (
                    st.get("title")
                    or rec.get("label")
                    or rec.get("terminal_title_stripped")
                    or st.get("agent")
                    or "agent"
                )
                rank = self.rank_for(status, st["settled"], st["unread"])
                flag = FLAG_SETTLED if st["settled"] else (FLAG_UNREAD if st["unread"] else "")
                tokens = {
                    "title": title,
                    "rank": rank,
                    "age": fmt_dur(now - st["first_seen"]),
                    "since": fmt_dur(now - st["last_change"]),
                    "flag": flag,
                }
                self._report_pane(pane_id, tid, st, tokens)

                ws = rec.get("workspace_id")
                if ws:
                    ws_agents.setdefault(ws, []).append((status, st, now))

            self._report_workspaces(ws_agents)
            self._prune(seen_tids, now)
            self._save()

    def _report_pane(self, pane_id, tid, st, tokens):
        meta_title = st.get("title")  # only override the pane title if generated
        want = {"title": meta_title, "tokens": tokens}
        if self.last_report.get(tid) == want:
            return
        params = {"pane_id": pane_id, "source": SOURCE, "tokens": tokens}
        if meta_title:
            params["title"] = meta_title
        try:
            herdr_request("pane.report_metadata", params)
            self.last_report[tid] = want
        except (OSError, RuntimeError) as e:
            self.log("report_metadata %s failed: %s" % (pane_id, e))

    def _report_workspaces(self, ws_agents):
        now = time.time()
        for ws, entries in ws_agents.items():
            counts = {"blocked": 0, "attention": 0, "working": 0, "idle": 0, "settled": 0}
            busiest = 0
            for status, st, _ in entries:
                if status == "blocked":
                    counts["blocked"] += 1
                elif status == "done" or st.get("unread"):
                    counts["attention"] += 1
                elif status == "working":
                    counts["working"] += 1
                    busiest = max(busiest, now - st.get("last_change", now))
                elif st.get("settled"):
                    counts["settled"] += 1
                else:
                    counts["idle"] += 1
            pieces = []
            # Idle is "○", NOT "·" — herdr joins row tokens with a "·"
            # separator, so a "·" glyph reads as a stray dash next to it.
            for key, sym in (
                ("blocked", "!"),
                ("attention", FLAG_UNREAD),
                ("working", "▸"),   # ▸
                ("idle", "○"),      # ○
                ("settled", FLAG_SETTLED),
            ):
                if not counts[key]:
                    continue
                # A lone idle agent is the workspace's default state — the
                # space's own status icon already says it; skip the noise.
                if key == "idle" and counts[key] == 1 and not pieces \
                        and not counts["settled"]:
                    continue
                pieces.append("%s%d" % (sym, counts[key]))
            tokens = {
                "agents": " ".join(pieces),
                "busy": ("▸%s" % fmt_dur(busiest)) if busiest else "",
            }
            if self.ws_report.get(ws) == tokens:
                continue
            try:
                herdr_request(
                    "workspace.report_metadata",
                    {"workspace_id": ws, "source": SOURCE, "tokens": tokens},
                )
                self.ws_report[ws] = tokens
            except (OSError, RuntimeError) as e:
                self.log("ws metadata %s failed: %s" % (ws, e))
        # Clear rollups for workspaces that no longer have agents.
        for ws in [w for w in self.ws_report if w not in ws_agents]:
            try:
                herdr_request(
                    "workspace.report_metadata",
                    {"workspace_id": ws, "source": SOURCE, "tokens": {"agents": None, "busy": None}},
                )
            except (OSError, RuntimeError):
                pass
            self.ws_report.pop(ws, None)

    def _prune(self, seen_tids, now):
        for tid, st in list(self.terminals.items()):
            if tid in seen_tids:
                continue
            gone = st.get("gone_since")
            if not gone:
                st["gone_since"] = now
            elif now - gone > STATE_KEEP_SECS:
                del self.terminals[tid]
                self.last_report.pop(tid, None)

    # -- control commands (from actions.py / inbox TUI) --

    def handle_command(self, cmd):
        op = cmd.get("cmd")
        now = time.time()
        with self.lock:
            if op in ("settle", "unread", "clear"):
                tid = self.pane_to_tid.get(cmd.get("pane_id"))
                if not tid or tid not in self.terminals:
                    return {"ok": False, "error": "no agent in pane %s" % cmd.get("pane_id")}
                st = self.terminals[tid]
                if op == "settle":
                    st["settled"] = True
                    st["unread"] = False
                elif op == "unread":
                    st["unread"] = True
                    st["settled"] = False
                    st["flagged_at"] = now
                else:
                    st["settled"] = False
                    st["unread"] = False
                title = st.get("title") or st.get("agent") or ""
            elif op == "settle-workspace":
                ws = cmd.get("workspace_id")
                n = 0
                try:
                    agents = herdr_request("agent.list", {}).get("agents", [])
                except (OSError, RuntimeError) as e:
                    return {"ok": False, "error": str(e)}
                for rec in agents:
                    if rec.get("workspace_id") != ws:
                        continue
                    tid = rec.get("terminal_id")
                    st = self.terminals.get(tid)
                    if st and rec.get("agent_status") in ("done", "idle", "unknown"):
                        st["settled"] = True
                        st["unread"] = False
                        n += 1
                title = "%d agents" % n
            elif op == "retitle":
                tid = self.pane_to_tid.get(cmd.get("pane_id"))
                st = self.terminals.get(tid)
                if not st:
                    return {"ok": False, "error": "no agent in pane %s" % cmd.get("pane_id")}
                st["title"] = None
                st["title_tried"] = 0
                st["sum_hash"] = None
                title = ""
            elif op == "ping":
                return {"ok": True, "pong": True}
            else:
                return {"ok": False, "error": "unknown cmd %r" % op}
        self.dirty.set()
        return {"ok": True, "title": title}

    def _on_focus(self, pane_id):
        with self.lock:
            tid = self.pane_to_tid.get(pane_id)
            st = self.terminals.get(tid)
            if st and st.get("unread") and time.time() - st.get("flagged_at", 0) > 1.5:
                st["unread"] = False
                self.dirty.set()

    # -- threads --

    def control_loop(self):
        try:
            os.unlink(self.control_path)
        except OSError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(self.control_path)
        os.chmod(self.control_path, 0o600)
        srv.listen(8)
        srv.settimeout(1.0)
        while not self.stop.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                conn.settimeout(3.0)
                data = b""
                while not data.endswith(b"\n"):
                    chunk = conn.recv(1 << 14)
                    if not chunk:
                        break
                    data += chunk
                resp = self.handle_command(json.loads(data))
            except (ValueError, OSError) as e:
                resp = {"ok": False, "error": str(e)}
            try:
                conn.sendall((json.dumps(resp) + "\n").encode())
            except OSError:
                pass
            conn.close()
        srv.close()

    def events_loop(self):
        subs = [
            {"type": "pane.updated"},
            {"type": "pane.created"},
            {"type": "pane.closed"},
            {"type": "pane.focused"},
            {"type": "pane.agent_detected"},
            {"type": "workspace.closed"},
        ]
        backoff = 1.0
        while not self.stop.is_set():
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(5.0)
                s.connect(herdr_socket_path())
                s.sendall(
                    (json.dumps({"id": "sub", "method": "events.subscribe",
                                 "params": {"subscriptions": subs}}) + "\n").encode()
                )
                s.settimeout(2.0)
                backoff = 1.0
                # A reconnect can mean the server restarted and lost all
                # reported metadata — forget what we think we've reported.
                with self.lock:
                    self.last_report.clear()
                    self.ws_report.clear()
                self.assert_view()   # re-assert after every (re)connect
                self.dirty.set()
                buf = b""
                while not self.stop.is_set():
                    try:
                        chunk = s.recv(1 << 16)
                    except socket.timeout:
                        continue
                    if not chunk:
                        raise OSError("event stream closed")
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        self._on_event(line)
            except OSError as e:
                self.log("event stream down (%s); retrying in %.0fs" % (e, backoff))
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            finally:
                try:
                    s.close()
                except OSError:
                    pass

    def _on_event(self, line):
        try:
            evt = json.loads(line)
        except ValueError:
            return
        name = evt.get("event")
        data = evt.get("data") or {}
        if name == "pane_focused":
            self._on_focus(data.get("pane_id"))
        if name in ("pane_updated", "pane_created", "pane_closed",
                    "pane_agent_detected", "workspace_closed", "pane_focused"):
            self.dirty.set()

    def run(self):
        self.log("daemon starting (pid %d, title_source=%s, summarize=%s)"
                 % (os.getpid(), self.cfg["title_source"], self.cfg["summarize"]))
        threading.Thread(target=self.control_loop, daemon=True).start()
        threading.Thread(target=self.events_loop, daemon=True).start()
        threading.Thread(target=self.summarize_loop, daemon=True).start()
        while not self.stop.is_set():
            fired = self.dirty.wait(TICK_SECS)
            if fired:
                self.dirty.clear()
                time.sleep(DEBOUNCE_SECS)  # coalesce event bursts
                self.dirty.clear()
            self.refresh()


def main():
    d = InboxDaemon()
    lock_path = os.path.join(d.dir, "daemon.lock")
    # "a" so a losing candidate doesn't truncate the winner's recorded pid.
    lock_file = open(lock_path, "a")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("herdr-agent-inbox daemon already running", file=sys.stderr)
        return 0
    lock_file.seek(0)
    lock_file.truncate()
    lock_file.write("%d\n" % os.getpid())
    lock_file.flush()
    try:
        d.run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

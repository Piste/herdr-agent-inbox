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

# Agents whose native title we can only find by working directory (herdr
# reports no session ref for them, or they never publish the title). Two live
# panes of the same such agent in one directory are indistinguishable, so we
# skip the lookup rather than risk labelling both with the same name. claude
# and pi are keyed by their own transcript and are never ambiguous.
CWD_KEYED_AGENTS = ("codex", "grok", "cursor", "hermes")

# Bump when title extraction changes so cached titles are re-derived instead
# of surviving forever in state.json.
TITLE_ALGO = 2

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
    os.makedirs(p, mode=0o700, exist_ok=True)
    try:
        # State holds prompt-derived titles and project paths — private.
        # makedirs won't tighten a pre-existing dir, so chmod explicitly
        # (this also gates control.sock on platforms where AF_UNIX connect
        # is governed by directory permissions).
        os.chmod(p, 0o700)
    except OSError:
        pass
    return p


def config_file():
    d = os.environ.get("HERDR_PLUGIN_CONFIG_DIR") or os.path.join(
        os.path.dirname(herdr_socket_path()), "plugins", "config", "herdr-agent-inbox"
    )
    return os.path.join(d, "config.toml")


DEFAULT_CONFIG = {
    "title_source": "first",       # "first" | "last" user prompt
    "prefer_agent_title": True,    # use the agent's own session name when it has one
    "summarize": False,            # pipe the prompt through summarize_cmd
    "summarize_cmd": "",           # shell cmd: prompt on stdin -> title on stdout
    "summarize_timeout_secs": 60,
    "tab_rename": False,           # rename tabs to their agent's session title
    "tab_max_chars": 24,           # truncate the label to this many characters
    "tab_ellipsis": "…",           # appended when the title is truncated
    "tab_respect_manual": True,    # never overwrite a label you set yourself
}

CONFIG_TEMPLATE = """\
# herdr-agent-inbox configuration

[title]
# Prefer the title the AGENT ITSELF gives the session, when it records one
# (claude-code's own session name; pi/codex don't publish one yet). Falls back
# to the prompt-derived title below whenever no native title exists.
prefer_agent_title = true

# Which user prompt seeds the session title when there's no native one:
#   "first" — the opening request, like ChatGPT/Claude chat titles
#   "last"  — the most recent request; re-derived when the agent finishes a turn
source = "first"

# Produce a meaningful title by piping the prompt through a command instead of
# showing the (truncated) raw prompt. The command receives the prompt text on
# stdin and must print a short title on stdout.
summarize = false
summarize_cmd = "claude -p --model claude-haiku-4-5-20251001 'Write a 4-8 word title for this coding-agent session request. Output ONLY the title, nothing else.'"
summarize_timeout_secs = 60

[tab]
# Rename each tab to its agent's session title automatically.
rename_from_agent_title = false

# Truncate the label to this many characters, appending `ellipsis` when cut.
max_chars = 24
ellipsis = "…"

# Never touch a tab you named yourself: a tab is only managed while its label
# is herdr's default (the tab number) or the last label this plugin set.
respect_manual = true
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
        if "prefer_agent_title" in title:
            cfg["prefer_agent_title"] = bool(title["prefer_agent_title"])
        cfg["summarize"] = bool(title.get("summarize", False))
        cmd = title.get("summarize_cmd")
        if isinstance(cmd, str):
            cfg["summarize_cmd"] = cmd
        t = title.get("summarize_timeout_secs")
        if isinstance(t, int) and 5 <= t <= 600:
            cfg["summarize_timeout_secs"] = t
        tab = data.get("tab") or {}
        cfg["tab_rename"] = bool(tab.get("rename_from_agent_title", False))
        n = tab.get("max_chars")
        if isinstance(n, int) and 4 <= n <= 200:
            cfg["tab_max_chars"] = n
        ell = tab.get("ellipsis")
        if isinstance(ell, str) and len(ell) <= 4:
            cfg["tab_ellipsis"] = ell
        cfg["tab_respect_manual"] = bool(tab.get("respect_manual", True))
    except (OSError, ValueError) as e:
        sys.stderr.write("config load failed: %s\n" % e)
    return cfg


class Log:
    def __init__(self, path):
        self.path = path
        self._writes = 0
        self._rotate()

    def _rotate(self):
        try:
            if os.path.exists(self.path) and os.path.getsize(self.path) > 1_000_000:
                os.replace(self.path, self.path + ".1")
        except OSError:
            pass

    def __call__(self, msg):
        line = "%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
        self._writes += 1
        if self._writes % 500 == 0:  # long-running daemons rotate too
            self._rotate()
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
    # Raise RuntimeError (not ValueError) for empty/truncated responses so
    # every `except (OSError, RuntimeError)` call site survives a herdr
    # restart mid-request.
    if not buf:
        raise RuntimeError("%s: connection closed without a reply" % method)
    try:
        resp = json.loads(buf)
    except ValueError as e:
        raise RuntimeError("%s: bad response: %s" % (method, e))
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
    # Defense in depth: drop C0/C1 control bytes (ESC, BEL, …) so terminal
    # escape sequences from transcript content or LLM output can never ride
    # a title, regardless of downstream normalization.
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", " ", text)
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


def _newest_json_title(candidates, keys, mtime_of=None):
    """(title, mtime) from the most recently touched candidate JSON file.

    candidates: iterable of file paths; keys: field names to try in order;
    mtime_of: optional callable(dict) -> sortable recency from file content.
    """
    best = None
    for path in candidates:
        try:
            stamp = os.path.getmtime(path)
            with open(path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        if mtime_of:
            stamp = mtime_of(data) or stamp
        title = None
        for key in keys:
            title = _clean_title(str(data.get(key) or ""))
            if title:
                break
        if title and (best is None or stamp > best[1]):
            best = (title, stamp)
    return best or (None, 0)


def codex_native_title(cwd):
    """codex keeps a per-thread `title` in ~/.codex/state_5.sqlite.

    It is the verbatim first prompt rather than a short generated name, so it
    gets truncated like any prompt-derived title — but it is authoritative,
    already indexed by cwd, and works for panes with no session ref at all.
    """
    if not cwd:
        return None
    db = os.path.expanduser("~/.codex/state_5.sqlite")
    if not os.path.exists(db):
        return None
    try:
        import sqlite3
        # Read-only URI so a live codex never blocks us and we never write.
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=0.5)
        try:
            con.execute("PRAGMA query_only = 1")
            row = con.execute(
                "SELECT title, preview, first_user_message FROM threads "
                "WHERE cwd = ? AND archived = 0 "
                "ORDER BY COALESCE(updated_at_ms, 0) DESC LIMIT 1",
                (cwd,),
            ).fetchone()
        finally:
            con.close()
    except Exception:
        return None
    for value in row or ():
        title = _clean_title(str(value or ""))
        if title:
            return title
    return None


def _sqlite_scalar(db, sql, params):
    """One read-only scalar query; never blocks or writes the agent's db."""
    if not os.path.exists(db):
        return None
    try:
        import sqlite3
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=0.5)
        try:
            con.execute("PRAGMA query_only = 1")
            row = con.execute(sql, params).fetchone()
        finally:
            con.close()
    except Exception:
        return None
    return row


def hermes_native_title(cwd):
    """hermes auto-titles sessions into ~/.hermes/profiles/<p>/state.db
    (`sessions.title`, best-effort and only set while NULL). It reports a
    session id + lifecycle state to herdr but never the title, so match on
    cwd across profiles, preferring a session that is still running."""
    if not cwd:
        return None
    best = None
    for db in glob.glob(os.path.expanduser("~/.hermes/profiles/*/state.db")):
        row = _sqlite_scalar(
            db,
            "SELECT title, ended_at IS NULL AS live, COALESCE(started_at, '') "
            "FROM sessions WHERE cwd = ? AND COALESCE(archived, 0) = 0 "
            "AND title IS NOT NULL AND title != '' "
            "ORDER BY live DESC, started_at DESC LIMIT 1",
            (cwd,),
        )
        if not row:
            continue
        title = _clean_title(str(row[0] or ""))
        if title:
            rank = (row[1] or 0, row[2] or "")
            if best is None or rank > best[1]:
                best = (title, rank)
    return best[0] if best else None


def grok_native_title(cwd):
    """grok stores `generated_title` per session under a url-encoded cwd dir."""
    if not cwd:
        return None
    import urllib.parse
    root = os.path.expanduser(
        "~/.grok/sessions/%s" % urllib.parse.quote(cwd, safe="")
    )
    return _newest_json_title(
        glob.glob(os.path.join(root, "*", "summary.json")),
        ("generated_title", "session_summary"),
    )[0]


def cursor_native_title(cwd):
    """cursor-agent stores `title` per chat under md5(cwd)/<chat-uuid>/."""
    if not cwd:
        return None
    digest = hashlib.md5(cwd.encode()).hexdigest()
    root = os.path.expanduser("~/.cursor/chats/%s" % digest)
    return _newest_json_title(
        glob.glob(os.path.join(root, "*", "meta.json")),
        ("title",),
        mtime_of=lambda d: (d.get("updatedAtMs") or 0) / 1000.0 or None,
    )[0]


def pi_native_title(path):
    """pi never auto-generates a title, but a name the user sets (`/name`,
    `--name`, picker rename, RPC set_session_name) is appended to the same
    transcript as {"type":"session_info","name":...}; the latest one wins and
    an empty name clears it — matching what pi's own resume picker shows."""
    if not path:
        return None
    try:
        size = os.path.getsize(path)
        cap = 256 * 1024
        while True:
            for line in reversed(_tail_lines(path, cap)):
                if '"session_info"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if obj.get("type") == "session_info" and "name" in obj:
                    # An empty name is pi's way of clearing it.
                    return _clean_title(str(obj.get("name") or "")) or None
            if cap >= min(size, 32 * 1024 * 1024):
                return None
            cap *= 4
    except OSError:
        return None


def native_title_from_transcript(path):
    """claude-code's own name for the session, if it has one.

    Two record types, both kept current as the conversation evolves:
      {"type":"custom-title","customTitle":...}  a name YOU set (/title)
      {"type":"ai-title","aiTitle":...}          claude's generated title
    A custom title wins — it is an explicit choice — so scan from the end and
    take the newest of each, preferring custom. This mirrors what claude's
    own resume picker displays.
    """
    try:
        size = os.path.getsize(path)
        cap = 256 * 1024
        while True:
            ai = None
            for line in reversed(_tail_lines(path, cap)):
                if '"customTitle"' not in line and '"aiTitle"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                kind = obj.get("type")
                if kind == "custom-title":
                    title = _clean_title(str(obj.get("customTitle") or ""))
                    if title:
                        return title  # explicit name beats the generated one
                elif kind == "ai-title" and ai is None:
                    ai = _clean_title(str(obj.get("aiTitle") or ""))
            if ai:
                return ai
            if cap >= min(size, 32 * 1024 * 1024):
                return None
            cap *= 4
    except OSError:
        return None


def native_title_for(rec, path):
    """The session name the AGENT ITSELF gives this conversation, or None.

    Where each agent keeps it (verified 2026-07-28):
      claude       transcript records {"type":"ai-title","aiTitle":...}   ✅
      grok         ~/.grok/sessions/<urlencoded cwd>/<id>/summary.json
                   -> generated_title                                    ✅
      cursor       ~/.cursor/chats/<md5 cwd>/<uuid>/meta.json -> title   ✅
      codex        ~/.codex/state_5.sqlite threads.title — the verbatim
                   first prompt, used as-is (truncated)                  ✅
      pi           no auto-title, but a user-set name lands in the same
                   transcript as {"type":"session_info","name":...}      ✅
      hermes       auto-titles into ~/.hermes/profiles/*/state.db
                   sessions.title (reports its id to herdr, never the
                   title), so read the db directly                       ✅

    codex, grok, cursor and hermes are keyed by cwd (herdr gets no session
    ref for the first three, and hermes never reports its title), so the
    newest session for that directory wins.
    """
    agent = rec.get("agent")
    if agent == "claude" and path:
        return native_title_from_transcript(path)
    if agent == "codex":
        return codex_native_title(rec.get("cwd"))
    if agent == "pi" and path:
        return pi_native_title(path)
    if agent == "hermes":
        return hermes_native_title(rec.get("cwd"))
    if agent == "grok":
        return grok_native_title(rec.get("cwd"))
    if agent == "cursor":
        return cursor_native_title(rec.get("cwd"))
    return None


def title_from_transcript(path, source="first", prefer_native=True):
    """(clean_title, raw_prompt) from a session transcript.

    A native agent title wins when present (raw is None so it is never sent
    to the summarizer — the agent already named it).
    source "first": the opening user prompt (preferring claude summary lines).
    source "last":  the most recent user prompt (tail-scanned).
    """
    if prefer_native:
        native = native_title_from_transcript(path)
        if native:
            return native, None
    try:
        if source == "last":
            # Transcript lines can be huge (base64 images in tool results), so
            # widen the tail window until a user prompt shows up — but cap the
            # read so a promptless multi-hundred-MB transcript can't be
            # re-slurped whole on every tick.
            size = os.path.getsize(path)
            cap = 512 * 1024
            while True:
                title, raw = _scan_lines_for_prompt(
                    reversed(_tail_lines(path, cap)), True)
                if title or cap >= min(size, 32 * 1024 * 1024):
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
    # A session *id* gets interpolated into paths/globs below — reject
    # anything that could traverse or glob outside the intended directories.
    if re.search(r"[/\\*?\[\]]|\.\.", value):
        return None
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
        self.ambiguous = set()    # (agent, cwd) pairs with >1 live pane
        self.tab_labels = {}      # tab_id -> label WE set (never clobber yours)
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
            self.tab_labels = data.get("tab_labels", {}) or {}
        except (OSError, ValueError):
            self.terminals = {}

    def _save(self):
        tmp = self.state_path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump({"terminals": self.terminals,
                           "tab_labels": self.tab_labels}, f)
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

    def _archive_chat(self, st, now):
        """Archive st's current chat: into its in-pane history (capped 10,
        drives the tree's ⚫ rows) and the durable history.jsonl (drives the
        ChatGPT-style history browser, resumable via stored session refs)."""
        hist = list(st.get("history") or [])
        # A pane archived on "gone" that reappears and closes for real would
        # archive the same chat twice — skip unchanged re-archives.
        fingerprint = (st.get("sess_ref"), st.get("title"))
        if fingerprint == tuple(st.get("_last_archived") or ()):
            return hist
        if st.get("title"):
            st["_last_archived"] = list(fingerprint)
            entry = {
                "agent": st.get("agent"),
                "title": st["title"],
                "closed": now,
                "first_seen": st.get("first_seen"),
                "workspace_id": st.get("ws"),
                "workspace": st.get("ws_label"),
                "pane_id": st.get("pane_id"),
                "cwd": st.get("cwd"),
                "sess_kind": st.get("sess_kind"),
                "sess_value": st.get("sess_value"),
            }
            hist.append({"agent": entry["agent"], "title": entry["title"],
                         "closed": now})
            self._append_history(entry)
        return hist[-10:]

    def _append_history(self, entry):
        path = os.path.join(self.dir, "history.jsonl")
        try:
            if os.path.exists(path) and os.path.getsize(path) > 400_000:
                with open(path) as f:
                    tail = f.readlines()[-1000:]
                tmp = path + ".tmp"
                with open(tmp, "w") as f:
                    f.writelines(tail)
                os.replace(tmp, path)  # atomic — readers never see a partial file
            with open(path, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError as e:
            self.log("history append failed: %s" % e)

    def _ensure_title(self, rec, st, now):
        """Generate/refresh the session title for one agent record."""
        sess = rec.get("agent_session") or {}
        sess_ref = "%s:%s" % (sess.get("kind"), sess.get("value"))
        if sess.get("value") and st.get("sess_ref") not in (None, sess_ref):
            # The pane started a NEW native session — archive the old chat.
            st["history"] = self._archive_chat(st, now)
        if sess.get("value"):
            st["sess_ref"] = sess_ref
            st["sess_kind"] = sess.get("kind")
            st["sess_value"] = sess.get("value")
        sess_key = "%s:%s:%s:%s:%s" % (sess.get("kind"), sess.get("value"),
                                       self.cfg["title_source"],
                                       self.cfg["prefer_agent_title"],
                                       TITLE_ALGO)
        fresh = st.get("title_sess") == sess_key
        if st.get("title_manual") and st.get("title"):
            if fresh:
                return
            st["title_manual"] = False  # new session ref supersedes manual title
        path = resolve_transcript(rec)

        # The agent's own name for the session is checked EVERY tick, before
        # any cached-title shortcut: an agent renames its session as work
        # evolves, and a session can gain a name long after it started.
        # The ambiguity guard applies ONLY to cwd-keyed agents — claude and pi
        # are looked up by their own transcript, so several of them in one
        # directory are fine.
        cwd_keyed = rec.get("agent") in CWD_KEYED_AGENTS
        if self.cfg["prefer_agent_title"] \
                and not (cwd_keyed
                         and (rec.get("agent"), rec.get("cwd")) in self.ambiguous):
            native = native_title_for(rec, path)
            if native:
                st["title_sess"] = sess_key
                st["title_native"] = True
                st["title_stale"] = False
                if native != st.get("title"):
                    st["title"] = native
                    self.log("title (agent) %s -> %r" % (rec.get("pane_id"), native))
                return
            if st.get("title_native"):
                # The agent's title went away (cleared) — re-derive our own.
                st["title_native"] = False
                st["title"] = None

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
        if not path:
            if not st.get("title"):
                st["title"] = None
            return
        # Native titles were already handled above; derive from prompts only.
        title, raw = title_from_transcript(path, self.cfg["title_source"],
                                           prefer_native=False)
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
        ws_labels = {}
        try:
            for wsr in herdr_request("workspace.list", {}).get("workspaces", []):
                ws_labels[wsr.get("workspace_id")] = wsr.get("label")
        except (OSError, RuntimeError, ValueError):
            pass
        pending = []
        ws_pending = []
        tab_best = {}
        seen_pairs = {}
        for rec in agents:
            key = (rec.get("agent"), rec.get("cwd"))
            seen_pairs[key] = seen_pairs.get(key, 0) + 1
        self.ambiguous = {k for k, n in seen_pairs.items() if n > 1}
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
                    old = st
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
                    if old:
                        st["history"] = self._archive_chat(old, now)
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
                st["gone_archived"] = False
                st["ws"] = rec.get("workspace_id")
                st["ws_label"] = ws_labels.get(rec.get("workspace_id"))
                st["pane_id"] = pane_id
                st["cwd"] = rec.get("cwd")
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
                pending.append(self._pane_report_item(pane_id, tid, st, tokens))

                # Per tab, the most attention-worthy agent names the tab
                # (rank asc, then most recent state change) — stable when a
                # tab holds several agents.
                tab_id = rec.get("tab_id")
                if tab_id and st.get("title"):
                    key = (rank, -(rec.get("state_change_seq") or 0))
                    prev = tab_best.get(tab_id)
                    if prev is None or key < prev[0]:
                        tab_best[tab_id] = (key, st["title"])

                ws = rec.get("workspace_id")
                if ws:
                    ws_agents.setdefault(ws, []).append((status, st, now))

            ws_pending = self._workspace_report_items(ws_agents)
            self._prune(seen_tids, now)
            self._save()
        # Socket round-trips happen OUTSIDE the lock so control commands
        # (settle/unread/…) never queue behind a slow herdr server.
        for item in pending:
            if item:
                self._send_pane_report(*item)
        for ws, tokens in ws_pending:
            self._send_ws_report(ws, tokens)
        if self.cfg["tab_rename"]:
            titles = {t: v[1] for t, v in tab_best.items()}
            for tab_id, label in self._tab_rename_items(titles):
                self._send_tab_rename(tab_id, label)

    def _pane_report_item(self, pane_id, tid, st, tokens):
        meta_title = st.get("title")  # only override the pane title if generated
        want = {"title": meta_title, "tokens": tokens}
        if self.last_report.get(tid) == want:
            return None
        params = {"pane_id": pane_id, "source": SOURCE, "tokens": tokens}
        if meta_title:
            params["title"] = meta_title
        return (pane_id, tid, params, want)

    def _send_pane_report(self, pane_id, tid, params, want):
        try:
            herdr_request("pane.report_metadata", params)
            with self.lock:
                self.last_report[tid] = want
        except (OSError, RuntimeError) as e:
            self.log("report_metadata %s failed: %s" % (pane_id, e))

    def _send_ws_report(self, ws, tokens):
        try:
            herdr_request(
                "workspace.report_metadata",
                {"workspace_id": ws, "source": SOURCE, "tokens": tokens},
            )
            with self.lock:
                if tokens.get("agents") is None:
                    self.ws_report.pop(ws, None)
                else:
                    self.ws_report[ws] = tokens
        except (OSError, RuntimeError) as e:
            self.log("ws metadata %s failed: %s" % (ws, e))
            if tokens.get("agents") is None:
                # Clearing a workspace that no longer exists (e.g. ids
                # regenerated by a server handoff) can never succeed — drop
                # it instead of retrying every tick forever.
                with self.lock:
                    self.ws_report.pop(ws, None)

    def _tab_label_for(self, title):
        """Truncate a session title to the configured tab-label length."""
        limit = self.cfg["tab_max_chars"]
        title = _WS_RE.sub(" ", title).strip()
        if len(title) <= limit:
            return title
        ell = self.cfg["tab_ellipsis"]
        cut = title[: max(1, limit - len(ell))]
        # Prefer a word boundary when one is reasonably close to the cut.
        if " " in cut[max(1, len(cut) // 2):]:
            cut = cut[: cut.rfind(" ")]
        return cut.rstrip(" ,;:.-") + ell

    def _tab_rename_items(self, tab_titles):
        """[(tab_id, label)] for tabs whose label should change.

        A tab is only managed while its label is herdr's default (the tab
        number) or the exact label this plugin last set — so a name you type
        yourself is never overwritten. Renaming one manually also releases
        the tab from management until it goes back to a default label.
        """
        items = []
        try:
            workspaces = herdr_request("workspace.list", {}).get("workspaces", [])
        except (OSError, RuntimeError):
            return items
        managed = self.tab_labels
        live = set()
        for ws in workspaces:
            ws_id = ws.get("workspace_id")
            try:
                tabs = herdr_request("tab.list", {"workspace_id": ws_id}).get("tabs", [])
            except (OSError, RuntimeError):
                continue
            for tab in tabs:
                tab_id = tab.get("tab_id")
                live.add(tab_id)
                want = tab_titles.get(tab_id)
                if not want:
                    continue
                label = tab.get("label") or ""
                default = str(tab.get("number", ""))
                ours = managed.get(tab_id)
                if self.cfg["tab_respect_manual"] \
                        and label not in ("", default) and label != ours:
                    continue  # a name Douglas typed — leave it alone
                want = self._tab_label_for(want)
                if want and want != label:
                    items.append((tab_id, want))
        for gone in [t for t in managed if t not in live]:
            managed.pop(gone, None)
        return items

    def _send_tab_rename(self, tab_id, label):
        try:
            herdr_request("tab.rename", {"tab_id": tab_id, "label": label})
            with self.lock:
                self.tab_labels[tab_id] = label
            self.log("tab %s -> %r" % (tab_id, label))
        except (OSError, RuntimeError) as e:
            self.log("tab rename %s failed: %s" % (tab_id, e))

    def _workspace_report_items(self, ws_agents):
        """Compute per-workspace rollup tokens; returns [(ws, tokens)] for
        entries that changed. Pure computation — no I/O (called under lock)."""
        items = []
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
            if self.ws_report.get(ws) != tokens:
                items.append((ws, tokens))
        # Clear rollups for workspaces that no longer have agents.
        for ws in [w for w in self.ws_report if w not in ws_agents]:
            items.append((ws, {"agents": None, "busy": None}))
        return items

    def _prune(self, seen_tids, now):
        for tid, st in list(self.terminals.items()):
            if tid in seen_tids:
                continue
            gone = st.get("gone_since")
            if not gone:
                st["gone_since"] = now
                continue
            # Archive the chat once the pane has been gone long enough that
            # this isn't just a server-restart or detection blip.
            if now - gone > 120 and not st.get("gone_archived"):
                st["gone_archived"] = True
                st["history"] = self._archive_chat(st, now)
            if now - gone > STATE_KEEP_SECS:
                del self.terminals[tid]
                self.last_report.pop(tid, None)

    # -- control commands (from actions.py / inbox TUI) --

    def handle_command(self, cmd):
        op = cmd.get("cmd")
        now = time.time()
        agents = None
        if op == "settle-workspace":
            # Network fetch happens BEFORE taking the lock.
            try:
                agents = herdr_request("agent.list", {}).get("agents", [])
            except (OSError, RuntimeError) as e:
                return {"ok": False, "error": str(e)}
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
            elif op == "set-title":
                tid = self.pane_to_tid.get(cmd.get("pane_id"))
                st = self.terminals.get(tid)
                if not st:
                    return {"ok": False, "error": "no agent in pane %s" % cmd.get("pane_id")}
                title = _clean_title(str(cmd.get("title") or ""))
                if not title:
                    return {"ok": False, "error": "empty/unusable title"}
                # Manual titles stick until the agent's session ref changes
                # (a new conversation) or an explicit retitle.
                st["title"] = title
                st["title_manual"] = True
                st["sum_hash"] = None
            elif op == "retitle":
                tid = self.pane_to_tid.get(cmd.get("pane_id"))
                st = self.terminals.get(tid)
                if not st:
                    return {"ok": False, "error": "no agent in pane %s" % cmd.get("pane_id")}
                st["title"] = None
                st["title_tried"] = 0
                st["sum_hash"] = None
                st["title_manual"] = False
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
                cmd = json.loads(data)
                if not isinstance(cmd, dict):
                    raise ValueError("command must be a JSON object")
                resp = self.handle_command(cmd)
            except Exception as e:  # the control thread must never die
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
            s = None
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
            except (OSError, RuntimeError) as e:
                self.log("event stream down (%s); retrying in %.0fs" % (e, backoff))
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            finally:
                if s is not None:
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
    # Everything this daemon writes derives from the user's private prompts;
    # nothing it creates should be group/world-readable.
    os.umask(0o077)
    d = InboxDaemon()
    for name in ("state.json", "history.jsonl", "daemon.log", "daemon.log.1",
                 "daemon.lock", "tui_prefs.json"):
        try:
            os.chmod(os.path.join(d.dir, name), 0o600)
        except OSError:
            pass
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

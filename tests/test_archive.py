import os
import tempfile
import threading
import unittest
from unittest import mock

import daemon
import inbox_tui


def agent(status="idle"):
    return {
        "agent": "codex",
        "agent_status": status,
        "agent_session": {"kind": "id", "value": "thread-1"},
        "pane_id": "w9:p2",
        "terminal_id": "term-1",
    }


class ArchiveTests(unittest.TestCase):
    def make_daemon(self, events):
        obj = daemon.InboxDaemon.__new__(daemon.InboxDaemon)
        obj.lock = threading.RLock()
        obj.pane_to_tid = {"w9:p2": "term-1"}
        obj.terminals = {
            "term-1": {
                "agent": "codex",
                "title": "Useful thread",
                "sess_ref": "id:thread-1",
                "sess_kind": "id",
                "sess_value": "thread-1",
                "history": [],
            }
        }
        obj.dirty = mock.Mock()
        obj._save = lambda: events.append("save")
        obj._resume_error = lambda rec: None

        def append(entry):
            events.append("append")
            return True

        obj._append_history = append
        return obj

    def test_rank_uses_only_real_agent_state(self):
        self.assertEqual(daemon.InboxDaemon.rank_for("blocked"), "0")
        self.assertEqual(daemon.InboxDaemon.rank_for("done"), "1")
        self.assertEqual(daemon.InboxDaemon.rank_for("working"), "2")
        self.assertEqual(daemon.InboxDaemon.rank_for("idle"), "3")
        self.assertEqual(daemon.InboxDaemon.rank_for("idle", unread=True), "1")
        self.assertEqual(daemon.InboxDaemon.rank_for("unknown"), "4")

    def test_unread_is_orthogonal_to_archive(self):
        events = []
        obj = self.make_daemon(events)
        response = obj.handle_command({"cmd": "unread", "pane_id": "w9:p2"})
        self.assertTrue(response["ok"])
        self.assertTrue(obj.terminals["term-1"]["unread"])
        self.assertNotIn("append", events)

    def test_archive_is_durable_before_close(self):
        events = []
        obj = self.make_daemon(events)

        def request(method, params):
            if method == "agent.list":
                events.append("list")
                return {"agents": [agent()]}
            if method == "pane.close":
                events.append("close")
                return {}
            raise AssertionError(method)

        with mock.patch.object(daemon, "herdr_request", side_effect=request):
            response = obj.handle_command({"cmd": "archive", "pane_id": "w9:p2"})
        self.assertTrue(response["ok"])
        self.assertLess(events.index("append"), events.index("close"))
        self.assertEqual(events, ["list", "append", "save", "list", "close"])

    def test_archive_refuses_working_and_unknown(self):
        for status in ("working", "blocked", "unknown"):
            events = []
            obj = self.make_daemon(events)
            with mock.patch.object(
                    daemon, "herdr_request", return_value={"agents": [agent(status)]}):
                response = obj.handle_command({"cmd": "archive", "pane_id": "w9:p2"})
            self.assertFalse(response["ok"])
            self.assertNotIn("append", events)

    def test_archive_rechecks_session_before_close(self):
        events = []
        obj = self.make_daemon(events)
        changed = agent()
        changed["agent_session"] = {"kind": "id", "value": "thread-2"}
        replies = [{"agents": [agent()]}, {"agents": [changed]}]

        def request(method, params):
            if method == "agent.list":
                return replies.pop(0)
            events.append("close")
            return {}

        with mock.patch.object(daemon, "herdr_request", side_effect=request):
            response = obj.handle_command({"cmd": "archive", "pane_id": "w9:p2"})
        self.assertFalse(response["ok"])
        self.assertNotIn("close", events)

    def test_archive_refuses_stale_daemon_metadata(self):
        events = []
        obj = self.make_daemon(events)
        obj.terminals["term-1"]["sess_value"] = "older-thread"
        with mock.patch.object(
                daemon, "herdr_request", return_value={"agents": [agent()]}):
            response = obj.handle_command({"cmd": "archive", "pane_id": "w9:p2"})
        self.assertFalse(response["ok"])
        self.assertNotIn("append", events)

    def test_codex_requires_exact_readable_rollout(self):
        obj = daemon.InboxDaemon.__new__(daemon.InboxDaemon)
        with tempfile.NamedTemporaryFile() as rollout:
            with mock.patch.object(
                    daemon, "_sqlite_scalar", return_value=(rollout.name,)):
                self.assertIsNone(obj._resume_error(agent()))
        with mock.patch.object(daemon, "_sqlite_scalar", return_value=None):
            self.assertIn("no readable rollout", obj._resume_error(agent()))


class ArchiveViewTests(unittest.TestCase):
    def test_search_matches_title_workspace_path_and_session(self):
        entries = [
            {"agent": "codex", "title": "Fix billing", "workspace": "api",
             "cwd": "/work/payments", "sess_kind": "id", "sess_value": "abc"},
            {"agent": "claude", "title": "Polish docs", "workspace": "web",
             "cwd": "/work/site", "sess_kind": "id", "sess_value": "def"},
        ]
        self.assertEqual(inbox_tui.filter_history(entries, "billing api"), entries[:1])
        self.assertEqual(inbox_tui.filter_history(entries, "SITE def"), entries[1:])

    def test_session_key_is_stable(self):
        item = {"agent": "codex", "sess_kind": "id", "sess_value": "abc"}
        self.assertEqual(inbox_tui.session_key(item), ("codex", "id", "abc"))


if __name__ == "__main__":
    unittest.main()

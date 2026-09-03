import json
import os
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime
from unittest import mock

import daemon
import inbox_tui
import restore


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
        obj.snoozes = {}
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
    def test_codex_restore_does_not_inherit_another_thread_id(self):
        entry = {"agent": "codex", "sess_kind": "id", "sess_value": "thread-7"}
        self.assertEqual(
            inbox_tui.resume_cmd(entry),
            "env -u CODEX_THREAD_ID codex resume thread-7",
        )

    def test_restore_reports_exact_session_to_herdr(self):
        entry = {"agent": "codex", "sess_kind": "id", "sess_value": "thread-7"}
        with mock.patch.object(restore.time, "time_ns", return_value=123), \
                mock.patch.object(restore, "herdr_request") as request:
            self.assertTrue(restore.report_resumed_session(entry, "w9:p4"))
        request.assert_called_once_with("pane.report_agent_session", {
            "pane_id": "w9:p4",
            "source": "herdr:codex",
            "agent": "codex",
            "seq": 123,
            "agent_session_id": "thread-7",
            "session_start_source": "resume",
        })

    def test_restore_focuses_new_pane_after_launch(self):
        entry = {
            "agent": "codex", "sess_kind": "id", "sess_value": "thread-7",
            "pane_id": "w9:p2", "cwd": "/work/project",
        }

        def cli(*args):
            if args == ("pane", "get", "w9:p2"):
                return {"pane": {"pane_id": "w9:p2"}}
            if args[:2] == ("pane", "split"):
                return {"pane": {"pane_id": "w9:p4"}}
            if args[:3] == ("pane", "run", "w9:p4"):
                return {}
            raise AssertionError(args)

        with mock.patch.object(restore, "herdr_cli", side_effect=cli), \
                mock.patch.object(restore, "report_resumed_session") as report, \
                mock.patch.object(restore, "herdr_request") as request:
            request.side_effect = lambda method, params: (
                {"agents": []} if method == "agent.list" else {})
            result = restore.resume_session(entry, focus=True)

        self.assertTrue(result["ok"])
        report.assert_called_once_with(entry, "w9:p4")
        request.assert_has_calls([
            mock.call("agent.list", {}),
            mock.call("pane.focus", {"pane_id": "w9:p4"}),
        ])

    def test_silent_herdr_success_is_not_a_restore_failure(self):
        completed = subprocess.CompletedProcess(
            args=["herdr"], returncode=0, stdout="", stderr="")
        with mock.patch.object(restore.subprocess, "run", return_value=completed):
            self.assertEqual(restore.herdr_cli("pane", "run", "w1:p1", "codex"), {})

    def test_background_restore_explicitly_preserves_focus(self):
        entry = {
            "agent": "codex", "sess_kind": "id", "sess_value": "thread-7",
            "pane_id": "w9:p2", "cwd": "/work/project",
        }
        cli_calls = []

        def cli(*args):
            cli_calls.append(args)
            if args == ("pane", "get", "w9:p2"):
                return {"pane": {"pane_id": "w9:p2"}}
            if args[:2] == ("pane", "split"):
                return {"pane": {"pane_id": "w9:p4"}}
            if args[:3] == ("pane", "run", "w9:p4"):
                return {}
            raise AssertionError(args)

        with mock.patch.object(restore, "herdr_cli", side_effect=cli), \
                mock.patch.object(restore, "report_resumed_session", return_value=True), \
                mock.patch.object(restore, "herdr_request",
                                  return_value={"agents": []}) as request:
            result = restore.resume_session(entry, focus=False)
        self.assertTrue(result["ok"])
        split = next(call for call in cli_calls if call[:2] == ("pane", "split"))
        self.assertIn("--no-focus", split)
        self.assertNotIn("--focus", split)
        self.assertEqual(request.call_args_list, [mock.call("agent.list", {})])

    def test_restore_fails_closed_when_live_sessions_cannot_be_checked(self):
        entry = {"agent": "codex", "sess_kind": "id", "sess_value": "thread-7"}
        with mock.patch.object(restore, "herdr_request",
                              side_effect=RuntimeError("offline")), \
                mock.patch.object(restore, "herdr_cli") as cli:
            result = restore.resume_session(entry)
        self.assertFalse(result["ok"])
        self.assertIn("could not check live sessions", result["error"])
        cli.assert_not_called()

    def test_popup_selects_the_invoking_thread(self):
        with mock.patch.dict(
                os.environ,
                {"HERDR_PLUGIN_CONTEXT_JSON": '{"focused_pane_id":"w7:p3"}'}):
            self.assertEqual(inbox_tui.invoking_pane_id(), "w7:p3")

    def test_bad_popup_context_falls_back_cleanly(self):
        with mock.patch.dict(
                os.environ, {"HERDR_PLUGIN_CONTEXT_JSON": "not json"}):
            self.assertIsNone(inbox_tui.invoking_pane_id())

    def test_search_matches_title_workspace_path_and_session(self):
        entries = [
            {"agent": "codex", "title": "Fix billing", "workspace": "api",
             "cwd": "/work/payments", "sess_kind": "id", "sess_value": "abc"},
            {"agent": "claude", "title": "Polish docs", "workspace": "web",
             "cwd": "/work/site", "sess_kind": "id", "sess_value": "def"},
        ]
        self.assertEqual(inbox_tui.filter_history(entries, "billing api"), entries[:1])
        self.assertEqual(inbox_tui.filter_history(entries, "SITE def"), entries[1:])

    def test_search_matches_snooze_reminder(self):
        entry = {"title": "Tickets", "snooze_message": "ask cat when back"}
        self.assertEqual(inbox_tui.filter_history([entry], "cat back"), [entry])

    def test_snoozed_history_is_stacked_above_archive(self):
        snoozed = [{"sess_value": "s1"}]
        archived = [{"sess_value": "a1"}]
        self.assertEqual(
            [kind for kind, _ in inbox_tui.build_history_lines(snoozed, archived)],
            ["history_header", "snoozed", "history_header", "hist"],
        )

    def test_session_key_is_stable(self):
        item = {"agent": "codex", "sess_kind": "id", "sess_value": "abc"}
        self.assertEqual(inbox_tui.session_key(item), ("codex", "id", "abc"))


class SnoozeTests(unittest.TestCase):
    make_daemon = ArchiveTests.make_daemon

    def test_relative_duration_and_optional_reminder(self):
        now = datetime(2026, 9, 2, 12, 0).timestamp()
        wake_at, reminder = daemon.parse_snooze_spec(
            " 3h ask cat when back", now=now)
        self.assertEqual(wake_at, now + 3 * 3600)
        self.assertEqual(reminder, "ask cat when back")

    def test_reminder_is_optional(self):
        now = datetime(2026, 9, 2, 12, 0).timestamp()
        wake_at, reminder = daemon.parse_snooze_spec("15m", now=now)
        self.assertEqual(wake_at, now + 15 * 60)
        self.assertEqual(reminder, "")

    def test_named_date_defaults_to_nine_local(self):
        now = datetime(2026, 9, 2, 12, 0).timestamp()
        wake_at, reminder = daemon.parse_snooze_spec(
            "1-oct when tickets should be available", now=now)
        self.assertEqual(datetime.fromtimestamp(wake_at), datetime(2026, 10, 1, 9, 0))
        self.assertEqual(reminder, "when tickets should be available")

    def test_date_accepts_explicit_time(self):
        now = datetime(2026, 9, 2, 12, 0).timestamp()
        wake_at, reminder = daemon.parse_snooze_spec(
            "1-oct 14:30 ask cat", now=now)
        self.assertEqual(datetime.fromtimestamp(wake_at), datetime(2026, 10, 1, 14, 30))
        self.assertEqual(reminder, "ask cat")

    def test_invalid_snooze_requires_time_expression(self):
        with self.assertRaisesRegex(ValueError, "duration.*date"):
            daemon.parse_snooze_spec("ask cat when back")

    def test_snooze_queue_is_written_atomically_and_privately(self):
        with tempfile.TemporaryDirectory() as directory:
            obj = daemon.InboxDaemon.__new__(daemon.InboxDaemon)
            obj.dir = directory
            obj.snooze_path = os.path.join(directory, "snoozes.json")
            obj.snoozes = {"key": {"wake_at": 123}}
            obj.log = mock.Mock()
            self.assertTrue(obj._save_snoozes())
            with open(obj.snooze_path) as handle:
                saved = json.load(handle)
            self.assertEqual(saved["snoozes"], obj.snoozes)
            self.assertEqual(os.stat(obj.snooze_path).st_mode & 0o077, 0)

    def test_snooze_is_durable_before_close(self):
        events = []
        obj = self.make_daemon(events)
        captured = []

        def append(entry):
            events.append("append")
            captured.append(dict(entry))
            return True

        obj._append_history = append
        obj._save_snoozes = lambda: events.append("snooze-save") or True

        def request(method, params):
            if method == "agent.list":
                events.append("list")
                return {"agents": [agent()]}
            if method == "pane.close":
                events.append("close")
                return {}
            raise AssertionError(method)

        with mock.patch.object(daemon, "herdr_request", side_effect=request):
            response = obj.handle_command({
                "cmd": "snooze", "pane_id": "w9:p2",
                "spec": "3h ask cat when back",
            })
        self.assertTrue(response["ok"])
        self.assertEqual(events,
                         ["list", "append", "snooze-save", "save", "list", "close",
                          "snooze-save"])
        self.assertEqual(captured[0]["snooze_message"], "ask cat when back")
        self.assertEqual(captured[0]["snooze_state"], "closing")
        self.assertEqual(next(iter(obj.snoozes.values()))["snooze_state"], "pending")

    def test_closing_snooze_is_not_consumed_during_close_race(self):
        events = []
        obj = self.make_daemon(events)
        key = obj._snooze_key(("codex", "id", "thread-1"))
        obj.snoozes[key] = {
            "agent": "codex", "sess_kind": "id", "sess_value": "thread-1",
            "title": "Tickets", "wake_at": 999, "snoozed_at": 100,
            "snooze_state": "closing",
        }
        obj._save_snoozes = mock.Mock(return_value=True)
        obj._save = mock.Mock()
        obj._notify_snooze_wake = mock.Mock()
        obj._process_snoozes([agent()], now=101)
        self.assertIn(key, obj.snoozes)
        self.assertEqual(obj.snoozes[key]["snooze_state"], "closing")

        obj._process_snoozes([], now=102)
        self.assertEqual(obj.snoozes[key]["snooze_state"], "pending")

    def test_due_snooze_wakes_without_focus_then_becomes_unread(self):
        events = []
        obj = self.make_daemon(events)
        key = obj._snooze_key(("codex", "id", "thread-1"))
        obj.snoozes[key] = {
            "agent": "codex", "sess_kind": "id", "sess_value": "thread-1",
            "title": "Tickets", "wake_at": 100,
            "snooze_message": "ask cat when back", "snooze_state": "pending",
        }
        obj._save_snoozes = mock.Mock(return_value=True)
        obj._save = mock.Mock()
        obj._notify_snooze_wake = mock.Mock()
        with mock.patch.object(
                daemon.restore, "resume_session",
                return_value={"ok": True, "pane_id": "w9:p4"}) as resume:
            obj._process_snoozes([], now=200)
        resume.assert_called_once_with(mock.ANY, focus=False)
        self.assertEqual(obj.snoozes[key]["snooze_state"], "waking")
        self.assertEqual(obj.snoozes[key]["wake_pane_id"], "w9:p4")

        obj.terminals["term-new"] = {"unread": False}
        live = agent()
        live.update({"pane_id": "w9:p4", "terminal_id": "term-new"})
        obj._process_snoozes([live], now=201)
        self.assertNotIn(key, obj.snoozes)
        self.assertTrue(obj.terminals["term-new"]["unread"])
        self.assertEqual(obj.terminals["term-new"]["reminder"], "ask cat when back")
        obj._notify_snooze_wake.assert_called_once()

    def test_transient_wake_failure_retries_without_losing_snooze(self):
        events = []
        obj = self.make_daemon(events)
        key = obj._snooze_key(("codex", "id", "thread-1"))
        obj.snoozes[key] = {
            "agent": "codex", "sess_kind": "id", "sess_value": "thread-1",
            "title": "Tickets", "wake_at": 100, "snooze_state": "pending",
        }
        obj._save_snoozes = mock.Mock(return_value=True)
        obj._save = mock.Mock()
        obj._notify_snooze_wake = mock.Mock()
        with mock.patch.object(
                daemon.restore, "resume_session",
                return_value={"ok": False, "error": "herdr restarting"}):
            obj._process_snoozes([], now=200)
        self.assertEqual(obj.snoozes[key]["snooze_state"], "pending")
        self.assertEqual(obj.snoozes[key]["wake_attempts"], 1)
        self.assertGreater(obj.snoozes[key]["retry_at"], 200)
        self.assertEqual(obj.snoozes[key]["last_error"], "herdr restarting")
        obj._notify_snooze_wake.assert_not_called()

    def test_manual_early_revive_consumes_snooze_without_unread(self):
        events = []
        obj = self.make_daemon(events)
        key = obj._snooze_key(("codex", "id", "thread-1"))
        obj.snoozes[key] = {
            "agent": "codex", "sess_kind": "id", "sess_value": "thread-1",
            "title": "Tickets", "wake_at": 999, "snooze_state": "pending",
        }
        obj._save_snoozes = mock.Mock(return_value=True)
        obj._save = mock.Mock()
        obj._notify_snooze_wake = mock.Mock()
        obj._process_snoozes([agent()], now=200)
        self.assertNotIn(key, obj.snoozes)
        self.assertFalse(obj.terminals["term-1"].get("unread", False))
        obj._notify_snooze_wake.assert_not_called()


if __name__ == "__main__":
    unittest.main()

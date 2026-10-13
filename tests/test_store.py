"""Tests for store."""

from contextlib import ExitStack
import json, sqlite3, subprocess, sys, tempfile, unittest
from unittest import mock
from pathlib import Path
from datetime import datetime, timedelta
from dispatch import agents, choose, cleanup, config, plane, redact, runs, verify
import main as dd_main
from helpers import (
    Base,
    HERE,
    MODEL,
    PY,
    REPO_CODE,
    S_BACK,
    S_IP,
    board,
    git,
    temporary_directory,
)


class ConnectionCleanup(unittest.TestCase):
    def test_main_closes_connection_on_exit_and_errors(self):
        for stage in ("once", "recover", "sleep", "drain", "start_health"):
            with self.subTest(stage=stage), ExitStack() as stack:
                conn = mock.Mock()
                cfg = {"db_path": "unused", "projects": {}}
                for owner, name in (
                    (config, "check_env_files"), (config, "check_groups"),
                    (cleanup, "retention_days"), (redact, "configure"),
                    (dd_main.alerts, "configure"), (dd_main.alerts, "start_health"),
                    (dd_main, "recover"), (dd_main, "cycle"), (dd_main, "drain"),
                    (dd_main, "log"), (dd_main.time, "sleep"),
                ):
                    patched = stack.enter_context(mock.patch.object(owner, name))
                    if name == stage:
                        patched.side_effect = KeyboardInterrupt
                stack.enter_context(mock.patch.object(config, "load", return_value=cfg))
                stack.enter_context(mock.patch.object(config, "poll_interval", return_value=1))
                stack.enter_context(mock.patch.object(runs, "connect", return_value=conn))
                stack.enter_context(mock.patch.object(dd_main, "code_version", return_value="test"))
                stack.enter_context(mock.patch.object(dd_main, "Scheduler"))
                argv = ["main.py"] if stage == "sleep" else ["main.py", "--once"]
                stack.enter_context(mock.patch.object(sys, "argv", argv))
                if stage == "once":
                    self.assertEqual(dd_main.main(), 0)
                else:
                    with self.assertRaises(KeyboardInterrupt):
                        dd_main.main()
                conn.close.assert_called_once()

    def test_connect_closes_connection_when_setup_fails(self):
        for stage, initialize in (("execute", False), ("executescript", True),
                                  ("__enter__", True)):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                conn = mock.MagicMock()
                getattr(conn, stage).side_effect = sqlite3.OperationalError("setup failed")
                with mock.patch.object(sqlite3, "connect", return_value=conn):
                    with self.assertRaisesRegex(sqlite3.OperationalError, "setup failed"):
                        runs.connect(Path(directory) / "runs.db", initialize=initialize)
                conn.close.assert_called_once()



class Store(unittest.TestCase):
    def test_active_states_block_duplicates(self):
        conn = runs.connect(temporary_directory(self) / "r.db")
        self.addCleanup(conn.close)
        issue = {"id": "x", "sequence_id": 1, "name": "n"}
        for status in runs.ACTIVE:
            conn.execute("DELETE FROM runs")
            rid = runs.enqueue(conn, project="p", issue=issue, agent="a", branch="b")
            conn.execute("UPDATE runs SET status=? WHERE id=?", (status, rid)); conn.commit()
            self.assertIsNone(runs.enqueue(conn, project="p", issue=issue, agent="a", branch="b"), status)
        conn.execute("UPDATE runs SET status=?", (runs.DONE,)); conn.commit()
        self.assertIsNotNone(runs.enqueue(conn, project="p", issue=issue, agent="a", branch="b"))

    def finished_run(self, conn, issue_id, agent):
        rid = runs.enqueue(conn, project="p", issue={"id": issue_id}, agent=agent, branch="b")
        runs.close(conn, rid, runs.DONE)
        return rid

    def test_last_builder_is_latest_earlier_run_that_started_its_agent(self):
        conn = runs.connect(temporary_directory(self) / "r.db")
        self.addCleanup(conn.close)
        first = self.finished_run(conn, "x", "a")
        self.assertIsNone(runs.last_builder(conn, "x", first))
        runs.record_usage(conn, first, "a")
        second = self.finished_run(conn, "x", "b")
        runs.record_usage(conn, second, "b")
        # Never started its agent, so it is skipped.
        never_started = self.finished_run(conn, "x", "c")
        other_story = self.finished_run(conn, "y", "d")
        runs.record_usage(conn, other_story, "d")
        current = runs.enqueue(conn, project="p", issue={"id": "x"}, agent="e", branch="b")
        runs.record_usage(conn, current, "e")
        self.assertEqual(runs.last_builder(conn, "x", current), "b")
        self.assertEqual(runs.last_builder(conn, "x", never_started), "b")
        self.assertEqual(runs.last_builder(conn, "x", second), "a")
        self.assertIsNone(runs.last_builder(conn, "z", current))

    def test_combined_note_is_latest_for_the_story(self):
        conn = runs.connect(temporary_directory(self) / "r.db")
        self.addCleanup(conn.close)
        first = self.finished_run(conn, "x", "a")
        self.assertIsNone(runs.combined_note(conn, "x"))
        runs.record_combine(conn, first, "sha1", True)
        self.assertIsNone(runs.combined_note(conn, "x"))
        runs.record_combine(conn, first, "sha2", True, note="first combine")
        second = self.finished_run(conn, "x", "a")
        runs.record_combine(conn, second, "sha3", True, note="second combine")
        other = self.finished_run(conn, "y", "a")
        runs.record_combine(conn, other, "sha4", True, note="other story")
        self.assertEqual(runs.combined_note(conn, "x"), "second combine")
        self.assertEqual(runs.combined_note(conn, "y"), "other story")
        self.assertIsNone(runs.combined_note(conn, "z"))

    def test_combine_commits_without_note_upgraded(self):
        path = temporary_directory(self) / "r.db"
        conn = runs.connect(path)
        rid = self.finished_run(conn, "x", "a")
        conn.execute("DROP TABLE combine_commits")
        conn.execute("CREATE TABLE combine_commits (sha TEXT PRIMARY KEY, run_id INTEGER NOT NULL, "
                     "clean INTEGER NOT NULL)")
        conn.execute("INSERT INTO combine_commits VALUES ('old', ?, 1)", (rid,))
        conn.commit(); conn.close()
        conn = runs.connect(path)
        self.addCleanup(conn.close)
        self.assertIn("note", {c["name"] for c in conn.execute("PRAGMA table_info(combine_commits)")})
        [row] = conn.execute("SELECT * FROM combine_commits").fetchall()
        self.assertEqual((row["sha"], row["run_id"], row["clean"], row["note"]), ("old", rid, 1, None))
        self.assertIsNone(runs.combined_note(conn, "x"))
        runs.record_combine(conn, rid, "new", True, note="combined")
        self.assertEqual(runs.combined_note(conn, "x"), "combined")



class Telemetry(Base):
    def test_stages_and_usage(self):
        self.add_issue(); self.cycle()
        timeline = runs.events(self.conn, run_id=1)
        self.assertEqual([e["type"] for e in timeline], [
            "queued", "worktree", "agent_started", "agent_finished", "testing",
            "saving", "combining", "main_current", "uploading", "pr_opened", "updating_plane", "outcome",
        ])
        self.assertTrue(all(e["agent"] == "claude-code" for e in timeline))
        self.assertTrue(all(datetime.fromisoformat(e["time"]).utcoffset() == timedelta(0)
                            for e in timeline))
        self.assertIn("in_review", timeline[-1]["detail"])
        [usage] = runs.usage_records(self.conn)
        self.assertEqual(usage["agent"], "claude-code")
        self.assertEqual(usage["issue_id"], "i1")
        self.assertTrue(json.loads(usage["usage"]))

    def test_blocked_and_cancelled_keep_usage(self):
        for cancelled in (False, True):
            with self.subTest(cancelled=cancelled):
                iid = "cancel" if cancelled else "blocked"
                self.add_issue(iid, seq=8 if cancelled else 7)
                board.get_states = [S_IP, S_BACK] if cancelled else None
                with mock.patch.object(verify, "run_verify", return_value=(cancelled, "bad tests")):
                    self.cycle()
                row = self.run_rows()[-1]
                self.assertEqual(row["status"], runs.CANCELLED if cancelled else runs.FAILED)
                timeline = runs.events(self.conn, run_id=row["id"])
                self.assertIn("cancelled" if cancelled else "blocked", timeline[-1]["detail"])
                if not cancelled:
                    self.assertEqual(timeline[-1]["detail"], "blocked")
                    self.assertTrue(any(e["type"] == "blocked" and "bad tests" in e["detail"] for e in timeline))
                usage = runs.usage_records(self.conn)[-1]
                self.assertTrue(json.loads(usage["usage"]))
                self.assertEqual(usage["run_id"], row["id"])

    def test_plane_retry_warning(self):
        self.add_issue(); board.fail_post = 3
        for stamp in ("2026-01-01T00:00:00+00:00", "2026-01-01T00:01:00+00:00", "2026-01-01T00:02:00+00:00"):
            with mock.patch.object(runs.timeline, "now", return_value=stamp):
                self.cycle()
        warnings = [e for e in runs.events(self.conn, run_id=1) if e["type"] == "warning"]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["repeat_count"], 3)
        self.assertEqual(warnings[0]["last_seen"], stamp)
        self.assertIn("will retry", warnings[0]["detail"])
        self.assertIn("down", warnings[0]["detail"])
        self.cycle()
        self.assertEqual(self.run_rows()[0]["status"], runs.DONE)
        self.assertEqual(len(runs.usage_records(self.conn)), 1)
        self.assertIn("retry", [e["type"] for e in runs.events(self.conn, run_id=1)])

    def test_failed_agent_usage_and_unexpected_stop(self):
        for index, (parser, style, mode, expected) in enumerate((
                ("claude_json", "claude", "fail_usage", 5),
                ("codex_json", "codex", "fail_usage", 122),
                ("claude_json", "claude", "fail", None),
                ("codex_json", "codex", "fail", None))):
            with self.subTest(parser=parser, mode=mode):
                agent = self.cfg["agents"]["claude-code"]
                agent["parser"] = parser
                agent["command"] = [PY, str(HERE / "fake_agent.py"), "{prompt}", mode, style]
                self.add_issue(f"i{index}", seq=7 + index)
                self.cycle()
                self.assertEqual(self.run_rows()[-1]["status"], runs.FAILED)
                usage = runs.usage_records(self.conn)[-1]["usage"]
                if expected is None:
                    self.assertIsNone(usage)
                else:
                    self.assertEqual(json.loads(usage)["output_tokens"], expected)
        self.add_issue("stopped", seq=20)
        with mock.patch.object(agents, "run", side_effect=RuntimeError("stopped")):
            self.cycle()
        self.assertEqual(self.run_rows()[-1]["status"], runs.FAILED)
        self.assertIsNone(runs.usage_records(self.conn)[-1]["usage"])
        self.assertIn("agent_finished", [e["type"] for e in runs.events(self.conn, run_id=5)])

    def test_resumed_branch_does_not_count_usage_twice(self):
        self.add_issue(); board.get_states = [S_IP, "FAIL"]; self.cycle()
        self.assertEqual(self.run_rows()[0]["status"], runs.FAILED)
        board.get_states = None
        board.issues["i1"]["state"] = {"id": S_IP}
        self.cycle()
        self.assertEqual(self.run_rows()[1]["status"], runs.DONE)
        self.assertEqual(len(runs.usage_records(self.conn)), 1)
        timeline = runs.events(self.conn, run_id=2)
        self.assertNotIn("agent_started", [e["type"] for e in timeline])
        self.assertTrue(any(e["type"] == "resumed" and "earlier run" in e["detail"] for e in timeline))

    def test_waiting_story_warning_does_not_create_run(self):
        self.add_issue()
        choose.cool_down(self.cfg, self.conn, "claude-code")
        for stamp in ("2099-01-01T00:00:00+00:00", "2099-01-01T00:01:00+00:00", "2099-01-01T00:02:00+00:00"):
            with mock.patch.object(runs.timeline, "now", return_value=stamp):
                self.cycle()
        [event] = runs.events(self.conn, project="dd", issue_id="i1")
        self.assertEqual(event["repeat_count"], 3)
        self.assertEqual(event["last_seen"], stamp)
        self.assertIsNone(event["run_id"])
        self.assertIn("No agent available", event["detail"])
        self.assertEqual(self.run_rows(), [])

    def test_board_warning_clears_between_failures(self):
        orch = plane.orchestrator(self.cfg)
        with mock.patch.object(plane, "orchestrator", return_value=orch):
            with mock.patch.object(orch, "issues_in_state", side_effect=plane.PlaneError("down")):
                for _ in range(3):
                    dd_main.poll_once(self.cfg, self.conn)
            dd_main.poll_once(self.cfg, self.conn)
            with mock.patch.object(orch, "issues_in_state", side_effect=plane.PlaneError("down")):
                dd_main.poll_once(self.cfg, self.conn)
        self.assertEqual([e["repeat_count"] for e in runs.events(self.conn)], [3, 1])

    def test_waiting_warning_clears_when_story_leaves_board(self):
        self.add_issue()
        choose.cool_down(self.cfg, self.conn, "claude-code")
        dd_main.poll_once(self.cfg, self.conn)
        dd_main._waiting.clear()  # As after a restart: the store owns the episode.
        board.issues["i1"]["state"] = {"id": S_BACK}
        dd_main.poll_once(self.cfg, self.conn)
        board.issues["i1"]["state"] = {"id": S_IP}
        dd_main.poll_once(self.cfg, self.conn)
        self.assertEqual([e["repeat_count"] for e in runs.events(self.conn) if e["type"] == "warning"], [1, 1])

    def test_event_failure_does_not_stop_story(self):
        self.conn.execute("DROP TABLE events")
        self.add_issue()
        with mock.patch.object(runs.timeline, "warn") as warning:
            self.cycle()
        self.assertTrue(warning.called)
        self.assertEqual(self.run_rows()[0]["status"], runs.DONE)

    def test_detail_scrubbed_before_capping(self):
        runs.record_event(self.conn, None, "warning", MODEL + "x" * 9000,
                          project="dd", issue_id="i1")
        [event] = runs.events(self.conn, project="dd", issue_id="i1")
        self.assertNotIn(MODEL, event["detail"])
        self.assertTrue(event["detail"].startswith("[redacted]"))
        self.assertEqual(len(event["detail"]), runs.DETAIL_LIMIT)

    def test_heartbeat_each_cycle(self):
        for stamp in ("2026-01-01T00:00:00+00:00", "2026-01-01T00:01:00+00:00"):
            with mock.patch.object(runs.small_tables, "now", return_value=stamp):
                dd_main.cycle(self.cfg, self.conn, "abc123")
            heartbeat = runs.heartbeat(self.conn)
            self.assertEqual((heartbeat["time"], heartbeat["version"]), (stamp, "abc123"))
        self.assertEqual(heartbeat["id"], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM heartbeats").fetchone()[0], 1)

    def test_startup_captures_version_once(self):
        versions = []
        original_cycle = dd_main.cycle
        started = git("rev-parse", "HEAD", cwd=self.repo).strip()
        def cycle(cfg, conn, version, scheduler=None):
            original_cycle(cfg, conn, version, scheduler)
            versions.append(runs.heartbeat(conn)["version"])
            if len(versions) == 1:
                git("-c", "user.name=Test", "-c", "user.email=t@x", "commit",
                    "--allow-empty", "-m", "new code", cwd=self.repo)
            if len(versions) == 3:
                raise KeyboardInterrupt
        with mock.patch.object(dd_main.config, "load", return_value=self.cfg), \
                mock.patch.object(dd_main.config, "check_env_files"), \
                mock.patch.object(dd_main.config, "check_groups"), \
                mock.patch.object(dd_main.redact, "configure"), \
                mock.patch.object(dd_main.alerts, "start_health"), \
                mock.patch.object(dd_main.runs, "connect", return_value=self.conn), \
                mock.patch.object(dd_main, "__file__", str(self.repo / "main.py")), \
                mock.patch.object(dd_main, "code_version", wraps=dd_main.code_version) as read, \
                mock.patch.object(dd_main, "cycle", side_effect=cycle), \
                mock.patch.object(dd_main.time, "sleep"), \
                mock.patch.object(sys, "argv", ["main.py"]):
            with self.assertRaises(KeyboardInterrupt):
                dd_main.main()
        read.assert_called_once()
        self.assertEqual(versions, [started] * 3)
        self.assertNotEqual(git("rev-parse", "HEAD", cwd=self.repo).strip(), started)

    def test_existing_heartbeats_and_events_upgraded(self):
        self.conn.execute("DROP TABLE events")
        self.conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, run_id INTEGER, project TEXT, issue_id TEXT, time TEXT, type TEXT, agent TEXT, detail TEXT)")
        self.conn.execute("INSERT INTO events VALUES (1, NULL, 'dd', 'i1', 'old', 'warning', NULL, 'waiting')")
        for stamp in ("a", "c", "b"):
            self.conn.execute("INSERT INTO heartbeats (time, version) VALUES (?, ?)", (stamp, stamp))
        self.conn.commit(); self.conn.close()
        self.conn = runs.connect(self.cfg["db_path"])
        self.addCleanup(self.conn.close)
        self.assertEqual(runs.heartbeat(self.conn)["version"], "c")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM heartbeats").fetchone()[0], 1)
        [event] = runs.events(self.conn)
        self.assertEqual((event["repeat_count"], event["last_seen"]), (1, "old"))

    def test_warning_episode_ends_on_clear_or_different_event(self):
        def record(kind="warning"):
            runs.record_event(self.conn, None, kind, "waiting", project="dd", issue_id="i1")
        record(); record()
        runs.clear_repeats(self.conn, project="dd", issue_id="i1")
        record(); record("resumed"); record()
        self.assertEqual([e["repeat_count"] for e in runs.events(self.conn)], [2, 1, 1, 1])

    def test_code_version_uses_own_folder_and_handles_failure(self):
        expected = git("rev-parse", "HEAD", cwd=self.repo).strip()
        with mock.patch.object(dd_main, "__file__", str(self.repo / "main.py")):
            self.assertEqual(dd_main.code_version(), expected)
        with mock.patch.object(dd_main.subprocess, "run", side_effect=OSError):
            self.assertEqual(dd_main.code_version(), "unknown")
        with mock.patch.object(dd_main.subprocess, "run") as command:
            command.return_value = subprocess.CompletedProcess([], 0, "abc123\n")
            self.assertEqual(dd_main.code_version(), "abc123")
            self.assertEqual(command.call_args.kwargs["cwd"], REPO_CODE)

    def test_old_database_upgraded_without_losing_runs(self):
        rid = runs.enqueue(self.conn, project="dd", issue={"id": "old"}, agent="claude-code", branch="b")
        for table in ("events", "agent_usage", "heartbeats"):
            self.conn.execute(f"DROP TABLE {table}")
        self.conn.commit(); self.conn.close()
        self.conn = runs.connect(self.cfg["db_path"])
        self.addCleanup(self.conn.close)
        self.assertEqual(runs.get(self.conn, rid)["issue_id"], "old")
        runs.record_event(self.conn, rid, "warning", "upgraded")
        runs.record_usage(self.conn, rid, "claude-code", {"input_tokens": 3})
        runs.record_heartbeat(self.conn, "unknown")
        self.assertEqual(len(runs.events(self.conn)), 1)
        self.assertEqual(len(runs.usage_records(self.conn)), 1)
        self.assertEqual(runs.heartbeat(self.conn)["version"], "unknown")


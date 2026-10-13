"""Tests for cleanup."""

from unittest import mock
from pathlib import Path
from datetime import datetime, timedelta, timezone
from dispatch import assessments, cleanup, config, gitops, runs
import main as dd_main
from helpers import (
    Base,
    S_IP,
    board,
    git,
)


class FailedCleanup(Base):
    """Failed runs' worktrees removed once they have been kept long enough."""

    def fail_story(self, days_ago):
        self.set_mode("fail"); self.add_issue(); self.cycle()
        [row] = self.run_rows()
        self.assertEqual(row["status"], runs.FAILED)
        finished = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(timespec="seconds")
        with self.conn:
            self.conn.execute("UPDATE runs SET finished_at = ? WHERE id = ?", (finished, row["id"]))
        return Path(row["worktree"])

    def clean(self):
        with mock.patch.object(cleanup, "log") as log, mock.patch.object(cleanup, "warn") as warn:
            cleanup.failed_worktrees(self.cfg, self.conn)
        return [c.args[0] for c in log.call_args_list], [c.args[0] for c in warn.call_args_list]

    def test_old_failed_folder_is_removed_and_its_branch_kept(self):
        self.cfg["failed_worktree_days"] = 3
        worktree = self.fail_story(days_ago=4)
        head = git("rev-parse", "dd-7", cwd=self.repo).strip()
        logged, warned = self.clean()
        self.assertFalse(worktree.exists())
        self.assertEqual(git("rev-parse", "dd-7", cwd=self.repo).strip(), head)
        self.assertEqual(len(logged), 1)
        self.assertIn(str(worktree), logged[0]); self.assertIn("branch dd-7 kept", logged[0])
        self.assertEqual(warned, [])
        self.assertIsNone(self.run_rows()[0]["worktree"])
        self.assertEqual(self.clean(), ([], []))  # nothing left to do, nothing logged again

    def test_removal_runs_once_per_cycle(self):
        self.cfg["failed_worktree_days"] = 3
        worktree = self.fail_story(days_ago=4)
        self.cycle()
        self.assertFalse(worktree.exists())

    def test_recent_failed_folder_is_kept(self):
        self.cfg["failed_worktree_days"] = 3
        worktree = self.fail_story(days_ago=2)
        self.assertEqual(self.clean(), ([], []))
        self.assertTrue(worktree.exists())

    def test_folder_of_a_story_picked_up_again_is_kept(self):
        self.cfg["failed_worktree_days"] = 3
        worktree = self.fail_story(days_ago=4)
        board.issues["i1"]["state"] = {"id": S_IP}
        dd_main.poll_once(self.cfg, self.conn)  # queued again: active on the same branch
        self.clean()
        self.assertTrue(worktree.exists())
        self.set_mode("nochange"); dd_main.drain(self.cfg, self.conn)  # a newer run, now closed
        with self.conn:
            self.conn.execute("UPDATE runs SET finished_at = '2000-01-01T00:00:00+00:00' WHERE id = 1")
        self.assertEqual([r["status"] for r in self.run_rows()], [runs.FAILED, runs.FAILED])
        self.assertEqual([r["id"] for r in runs.old_failed(self.conn, "2001-01-01")], [])

    def test_without_the_config_value_nothing_is_removed(self):
        worktree = self.fail_story(days_ago=400)
        self.assertEqual(self.clean(), ([], []))
        self.assertTrue(worktree.exists())

    def test_folder_with_uncommitted_changes_is_kept_and_warned_about(self):
        self.cfg["failed_worktree_days"] = 3
        worktree = self.fail_story(days_ago=4)
        head = git("rev-parse", "dd-7", cwd=self.repo).strip()
        (worktree / "notes.txt").write_text("not committed\n")
        with mock.patch.object(gitops, "remove_worktree") as remove:
            logged, warned = self.clean()
        remove.assert_not_called()
        self.assertEqual(logged, [])
        self.assertEqual(len(warned), 1); self.assertIn("uncommitted changes", warned[0])
        self.assertEqual((worktree / "notes.txt").read_text(), "not committed\n")
        self.assertEqual(git("rev-parse", "dd-7", cwd=self.repo).strip(), head)
        self.assertEqual(self.run_rows()[0]["worktree"], str(worktree))

    def test_folder_that_cannot_be_removed_is_warned_once_and_the_cycle_goes_on(self):
        self.cfg["failed_worktree_days"] = 3
        worktree = self.fail_story(days_ago=4)
        with mock.patch.object(gitops, "remove_worktree", return_value="locked"):
            logged, warned = self.clean()
            self.assertEqual(self.clean(), ([], []))  # retried, but not warned about again
        self.assertEqual(logged, [])
        self.assertEqual(len(warned), 1); self.assertIn("locked", warned[0])
        self.assertTrue(worktree.exists())
        self.add_issue("i2", seq=8); self.set_mode("edit")
        with mock.patch.object(gitops, "remove_worktree", side_effect=OSError("boom")):
            self.cycle()  # a failure here doesn't stop the cycle
        self.assertEqual(self.run_rows()[-1]["status"], runs.DONE)

    def test_bad_value_stops_startup(self):
        for bad in (-1, "7", True):
            with self.assertRaises(config.ConfigError):
                cleanup.retention_days({"failed_worktree_days": bad})
        self.assertEqual(cleanup.retention_days({"failed_worktree_days": 0}), 0)



class AssessmentFolderCleanup(Base):
    """Empty assessment folders a restart left behind, swept up each cycle."""

    def assessment(self, status):
        """An assessment of a fresh snapshot by claude-code, its run in this status. Returns its folder."""
        snapshot = runs.save_assessment_snapshot(self.conn, "dd", "cycle", "plan", "a" * 40, [], "2099-01-01",
                                                 ["claude-code"])
        runs.queue_assessments(self.conn, snapshot["id"])
        [row] = runs.assessments(self.conn, snapshot["id"])
        with self.conn:
            self.conn.execute("UPDATE runs SET status = ? WHERE id = ?", (status, row["run_id"]))
        return self.wt_root / assessments.folder_name(snapshot["id"], "claude-code"), row["run_id"]

    def clean(self):
        with mock.patch.object(cleanup, "log") as log, mock.patch.object(cleanup, "warn") as warn:
            cleanup.assessment_folders(self.cfg, self.conn)
        return [c.args[0] for c in log.call_args_list], [c.args[0] for c in warn.call_args_list]

    def events(self, event_type):
        return [e["detail"] for e in runs.events(self.conn, project="dd") if e["type"] == event_type]

    def test_empty_leftover_folder_is_removed_and_recorded(self):
        folder, _ = self.assessment(runs.INTERRUPTED)
        folder.mkdir()
        logged, warned = self.clean()
        self.assertFalse(folder.exists())
        self.assertEqual(warned, [])
        self.assertEqual(len(logged), 1); self.assertIn(str(folder), logged[0])
        [event] = self.events("assessment_folder_removed")
        self.assertIn(str(folder), event)
        self.assertEqual(self.clean(), ([], []))  # nothing left to do

    def test_removal_runs_once_per_cycle(self):
        folder = self.wt_root / assessments.folder_name(99, "claude-code")
        folder.mkdir()
        self.cycle()
        self.assertFalse(folder.exists())

    def test_folder_of_a_running_assessment_is_kept(self):
        folder, _ = self.assessment(runs.RUNNING)
        folder.mkdir()
        self.assertEqual(self.clean(), ([], []))
        self.assertTrue(folder.exists())
        self.assertEqual(self.events("assessment_folder_removed"), [])

    def test_folder_of_a_finished_assessment_still_holding_its_slot_is_kept(self):
        # The worker records the answer before discarding its folder, and only then releases the slot.
        folder, run_id = self.assessment(runs.DONE)
        folder.mkdir()
        with self.conn:
            self.conn.execute("INSERT INTO agent_slots VALUES (?, ?)", ("claude-code", run_id))
        self.assertEqual(self.clean(), ([], []))
        self.assertTrue(folder.exists())
        runs.release_slot(self.conn, run_id)
        self.clean()
        self.assertFalse(folder.exists())

    def test_folder_with_contents_is_kept_and_warned_about_once(self):
        folder, _ = self.assessment(runs.INTERRUPTED)
        (folder / "main-check").mkdir(parents=True)
        (folder / "main-check" / "app.py").write_text("print('hi')\n")
        logged, warned = self.clean()
        self.assertEqual(logged, [])
        self.assertEqual(len(warned), 1); self.assertIn(str(folder), warned[0]); self.assertIn("not empty", warned[0])
        self.assertEqual((folder / "main-check" / "app.py").read_text(), "print('hi')\n")
        self.assertEqual(len(self.events("warning")), 1)
        self.assertEqual(self.clean(), ([], []))  # retried, but not warned about again
        self.assertEqual(self.events("assessment_folder_removed"), [])

    def test_story_main_check_and_other_folders_are_untouched(self):
        kept = [self.wt_root / name for name in
                ("dd-7", "main-check", "plan-notes", "plan-12-short", "plan-x-0123456789abcdef", "other")]
        for folder in kept:
            folder.mkdir()
        target = self.tmp / "elsewhere"; target.mkdir()
        link = self.wt_root / assessments.folder_name(5, "claude-code")
        link.symlink_to(target)
        self.assertEqual(self.clean(), ([], []))
        self.assertTrue(all(folder.is_dir() for folder in kept))
        self.assertTrue(link.is_symlink()); self.assertTrue(target.is_dir())
        self.assertEqual(self.events("assessment_folder_removed"), [])

    def test_missing_worktree_root_is_skipped(self):
        self.wt_root.rmdir()
        self.assertEqual(self.clean(), ([], []))

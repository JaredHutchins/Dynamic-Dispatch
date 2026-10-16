"""Tests for recovery."""

import os, sqlite3
from unittest import mock
from dispatch import agents, choose, github, gitops, proc, runs, sync, verify, worker
import main as dd_main
from helpers import (
    Base,
    CLAUDE,
    GH,
    MainChanges,
    ORCH,
    PY,
    S_BACK,
    S_BLK,
    S_IP,
    S_REV,
    board,
    git,
    os,
    pr_failures,
    prs,
    sandbox_calls,
)


class Crash(BaseException):
    pass


class Restarts(Base):
    """A run cut off by a restart, crash or power cut is picked back up."""

    def setUp(self):
        super().setUp()
        self.reaps = []
        reap = mock.patch.object(proc, "reap", side_effect=lambda cfg, run_as=None: self.reaps.append(
            (run_as, len(sandbox_calls))))
        reap.start()
        self.addCleanup(reap.stop)

    def crash_at(self, target, name, calls=1):
        """Start the story's next run, and stop DD at the given call of target.name."""
        dd_main.poll_once(self.cfg, self.conn)
        run = runs.claim_next(self.conn)
        original, seen = getattr(target, name), []
        def stop(*args, **kwargs):
            seen.append(1)
            if len(seen) == calls:
                raise Crash()
            return original(*args, **kwargs)
        with mock.patch.object(target, name, side_effect=stop):
            with self.assertRaises(Crash):
                worker.execute(self.cfg, self.conn, dict(run))
        self.wait_assignments()
        self.assertEqual(runs.get(self.conn, run["id"])["status"], runs.RUNNING)
        return run

    def restart(self):
        self.reaps.clear(); sandbox_calls.clear()
        dd_main.recover(self.cfg, self.conn)

    def restart_comments(self):
        return [c for c in board.comments if "DD restarted" in c[2]]

    def test_interrupted_build_is_requeued_and_finishes(self):
        self.add_issue()
        self.set_mode("prompt")
        first = self.crash_at(verify, "run_verify")  # the agent has left uncommitted changes
        self.restart()
        rows = self.run_rows()
        self.assertEqual([r["status"] for r in rows], [runs.INTERRUPTED, runs.QUEUED])
        self.assertEqual(rows[1]["agent"], "claude-code")  # back to the agent building it
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM agent_slots").fetchone()[0], 0)
        self.assertEqual(board.comments, [])  # said once the retry has checked the story
        self.cycle()
        rows = self.run_rows()
        self.assertEqual([r["status"] for r in rows], [runs.INTERRUPTED, runs.DONE], rows[-1])
        self.assertEqual(board.issues["i1"]["state"], {"id": S_REV})
        [comment] = self.restart_comments()
        self.assertEqual(comment[0], ORCH)
        self.assertIn("DD restarted while this story was being worked on. Picking it back up "
                      "(restart 1 of 2).", comment[2])
        self.assertIn(f"run {first['id']} restarted", comment[2])
        # The unfinished changes are handed on, as for any stopped run.
        prompt = git("show", "dd-7:prompt.txt", cwd=self.origin)
        self.assertIn("Claude worked on this story in an earlier run", prompt)
        self.assertIn("- added: prompt.txt", prompt)
        self.assertEqual(len(prs), 1)
        # Another restart and poll say nothing more.
        self.cycle()
        self.assertEqual(len(self.restart_comments()), 1)

    def test_left_behind_processes_and_merge_are_cleaned_up_first(self):
        self.cfg["agents"]["claude-code"]["run_as"] = "dd-agent"
        self.add_issue()
        original = agents.run
        def moved_meanwhile(*args):
            MainChanges.advance_main(self)
            return original(*args)
        with mock.patch.object(agents, "run", side_effect=moved_meanwhile):
            first = self.crash_at(verify, "run_verify", calls=2)  # testing after combining with main
        worktree = self.wt_root / "dd-7"
        self.assertTrue((gitops.admin_dir(str(self.repo), worktree) / "MERGE_HEAD").exists())
        self.restart()
        self.assertEqual(self.reaps, [("dd-agent", 0)])  # before anything sandboxed runs
        self.assertFalse((gitops.admin_dir(str(self.repo), worktree) / "MERGE_HEAD").exists())
        self.assertIn("merge_aborted", [e["type"] for e in runs.events(self.conn, run_id=first["id"])])
        self.cycle()
        rows = self.run_rows()
        self.assertEqual([r["status"] for r in rows], [runs.INTERRUPTED, runs.DONE], rows[-1])
        self.assertIn("MAIN = 2", git("show", "dd-7:main.py", cwd=self.origin))

    def test_failed_cleanup_is_recorded_and_the_story_still_retried(self):
        self.add_issue()
        first = self.crash_at(verify, "run_verify")
        with mock.patch.object(proc, "reap", return_value="sudo: a password is required"):
            dd_main.recover(self.cfg, self.conn)
        warnings = [e["detail"] for e in runs.events(self.conn, run_id=first["id"]) if e["type"] == "warning"]
        self.assertTrue(any("password is required" in w for w in warnings), warnings)
        self.assertEqual([r["status"] for r in self.run_rows()], [runs.INTERRUPTED, runs.QUEUED])

    def test_committed_work_is_pushed_without_the_agent(self):
        self.add_issue()
        self.crash_at(gitops, "push")
        committed = git("rev-parse", "dd-7", cwd=self.repo).strip()
        self.restart()
        self.set_mode("fail")  # would block the story if it ran
        self.cycle()
        rows = self.run_rows()
        self.assertEqual([r["status"] for r in rows], [runs.INTERRUPTED, runs.DONE], rows[-1])
        self.assertEqual(len(sandbox_calls), 1)  # verify only
        self.assertEqual(git("rev-parse", "dd-7", cwd=self.origin).strip(), committed)
        # The Docs line the earlier run's check passed ends the description.
        self.assertEqual(rows[-1]["summary"], worker.RESTARTED_SUMMARY + "\n\n" + rows[0]["docs_line"])

    def test_pushed_work_is_kept(self):
        self.add_issue()
        self.crash_at(github, "open_pr")
        pushed = git("rev-parse", "dd-7", cwd=self.origin).strip()
        self.restart()
        self.set_mode("fail")
        self.cycle()
        rows = self.run_rows()
        self.assertEqual([r["status"] for r in rows], [runs.INTERRUPTED, runs.DONE], rows[-1])
        self.assertEqual(len(sandbox_calls), 1)  # verify only
        self.assertEqual(git("rev-parse", "dd-7", cwd=self.origin).strip(), pushed)
        [pr] = prs.values()
        self.assertEqual(rows[-1]["pr_url"], pr["url"])
        # The Docs line the earlier run's check passed ends the description.
        self.assertEqual(rows[-1]["summary"], worker.RESTARTED_PR_SUMMARY + "\n\n" + rows[0]["docs_line"])

    def test_open_pull_request_is_reused(self):
        self.add_issue()
        self.crash_at(runs, "await_review")  # the pull request is open
        [pr] = prs.values()
        self.restart()
        self.cycle()
        rows = self.run_rows()
        self.assertEqual([r["status"] for r in rows], [runs.INTERRUPTED, runs.DONE], rows[-1])
        self.assertEqual(list(prs.values()), [pr])
        self.assertEqual(rows[-1]["pr_url"], pr["url"])
        self.assertEqual(board.transitions, [(CLAUDE, "i1", S_REV)])
        review = [c for c in board.comments if c[0] == CLAUDE]
        self.assertEqual(len(review), 1)
        self.assertEqual(len(self.restart_comments()), 1)

    def test_second_interruption_in_a_row_goes_to_blocked(self):
        self.add_issue()
        self.crash_at(verify, "run_verify")
        self.restart()
        second = self.crash_at(verify, "run_verify")
        self.restart()
        rows = self.run_rows()
        self.assertEqual([r["status"] for r in rows], [runs.INTERRUPTED, runs.AWAITING_BLOCKED])
        self.cycle()
        self.assertEqual(runs.get(self.conn, second["id"])["status"], runs.FAILED)
        self.assertEqual(len(self.run_rows()), 2)
        self.assertEqual(board.issues["i1"]["state"], {"id": S_BLK})
        self.assertIn(runs.INTERRUPTED_TOO_OFTEN, board.comments[-1][2])
        self.assertIn("2 times in a row", runs.INTERRUPTED_TOO_OFTEN)
        self.assertEqual(len(self.restart_comments()), 1)

    def test_a_run_that_finishes_resets_the_count(self):
        self.add_issue()
        self.crash_at(verify, "run_verify")
        self.restart()
        self.cycle()
        self.assertEqual(self.run_rows()[-1]["status"], runs.DONE)
        board.issues["i1"]["state"] = {"id": S_IP}  # rework
        self.crash_at(verify, "run_verify")
        self.restart()
        self.assertEqual(self.run_rows()[-1]["status"], runs.QUEUED)
        self.cycle()
        self.assertEqual(self.run_rows()[-1]["status"], runs.DONE)
        self.assertEqual(len(self.restart_comments()), 2)

    def test_interrupted_refresh_is_retried_on_the_next_check(self):
        self.add_issue()
        self.cycle()
        MainChanges.advance_main(self)
        dd_main.poll_refreshes(self.cfg, self.conn)
        refresh = self.crash_at(gitops, "push")
        self.assertEqual(refresh["kind"], "refresh")
        comments = list(board.comments)
        self.restart()
        self.assertEqual(runs.get(self.conn, refresh["id"])["status"], runs.INTERRUPTED)
        self.assertEqual(board.comments, comments)
        self.assertEqual(self.reaps, [(None, 0)])
        self.cycle()
        rows = self.run_rows()
        self.assertEqual([(r["kind"], r["status"]) for r in rows],
                         [("story", runs.DONE), ("refresh", runs.INTERRUPTED), ("refresh", runs.DONE)])
        self.assertIn("MAIN = 2", git("show", "dd-7:main.py", cwd=self.origin))
        self.assertEqual(board.issues["i1"]["state"], {"id": S_REV})

    def test_story_moved_out_during_downtime_is_cancelled(self):
        self.add_issue()
        self.crash_at(gitops, "push")  # finished work committed, not pushed
        board.issues["i1"]["state"] = {"id": S_BACK}
        self.restart()
        self.cycle()
        rows = self.run_rows()
        self.assertEqual([r["status"] for r in rows], [runs.INTERRUPTED, runs.CANCELLED])
        self.assertIn("moved out of In Progress before work started", rows[-1]["note"])
        self.assertEqual(sandbox_calls, [])
        self.assertEqual(self.restart_comments(), [])
        self.assertNotIn("dd-7", git("branch", "--list", cwd=self.origin))
        # Moved back, the saved work is checked against the story, as work pulled out mid-run is.
        board.issues["i1"]["state"] = {"id": S_IP}
        self.set_mode("finished")
        self.cycle()
        rows = self.run_rows()
        self.assertEqual(rows[-1]["status"], runs.DONE, rows[-1])
        self.assertEqual(sandbox_calls[0]["env_file"], "/etc/dynamic-dispatch/claude.env")

    def test_waits_when_no_agent_is_free(self):
        self.add_issue()
        first = self.crash_at(verify, "run_verify")
        choose.cool_down(self.cfg, self.conn, "claude-code")
        self.restart()
        self.assertEqual([r["status"] for r in self.run_rows()], [runs.INTERRUPTED])
        self.assertEqual(runs.wait_for(self.conn, "i1")["run_id"], first["id"])
        self.assertIn("waiting in In Progress", board.comments[-1][2])



class Resume(Base):
    """A story whose finished work was committed but never pushed."""

    def block_before_push(self):
        self.add_issue()
        board.get_states = [S_IP, "FAIL"]  # start check passes, pre-push check can't reach Plane
        self.cycle()
        self.assertEqual(self.run_rows()[0]["status"], runs.FAILED)
        self.assertNotIn("dd-7", git("branch", "--list", cwd=self.origin))
        board.issues["i1"]["state"] = {"id": S_IP}  # human moves it back from the iPad
        sandbox_calls.clear()

    def test_moved_back_pushes_without_running_the_agent(self):
        self.block_before_push()
        self.set_mode("fail")  # would block the story if it ran
        self.cycle()
        rows = self.run_rows()
        self.assertEqual([r["status"] for r in rows], [runs.FAILED, runs.DONE], rows[-1])
        # only verify ran, not the agent
        self.assertEqual(len(sandbox_calls), 1)
        self.assertEqual(sandbox_calls[0]["command"][1:3], ["-m", "compileall"])
        self.assertIn("VALUE = 1", git("show", "dd-7:feature.py", cwd=self.origin))
        self.assertEqual(git("rev-list", "--count", "main..dd-7", cwd=self.origin).strip(), "1")
        self.assertEqual(len(prs), 1)
        self.assertEqual(board.issues["i1"]["state"], {"id": S_REV})
        self.assertIn("verified again", rows[-1]["summary"])

    def test_restart_after_saving_still_resumes_without_agent(self):
        self.block_before_push()
        with self.conn:
            self.conn.execute("UPDATE runs SET status = ? WHERE id = ?",
                              (runs.RUNNING, self.run_rows()[0]["id"]))
        [interrupted] = runs.interrupt_running(self.conn)
        sync.reconcile(self.cfg, self.conn, interrupted)
        board.issues["i1"]["state"] = {"id": S_IP}
        self.set_mode("fail")
        self.cycle()
        self.assertEqual(self.run_rows()[-1]["status"], runs.DONE)
        self.assertEqual(len(sandbox_calls), 1)  # verify only
        self.assertEqual(board.issues["i1"]["state"], {"id": S_REV})

    def test_resume_still_checks_in_progress_before_pushing(self):
        self.block_before_push()
        board.get_states = [S_IP, "FAIL"]
        self.cycle()
        self.assertEqual([r["status"] for r in self.run_rows()], [runs.FAILED, runs.FAILED])
        self.assertEqual(len(sandbox_calls), 1)  # verify only
        self.assertNotIn("dd-7", git("branch", "--list", cwd=self.origin))
        self.assertEqual(prs, {})

    def test_resume_with_failing_verify_blocks_and_pushes_nothing(self):
        self.block_before_push()
        self.cfg["projects"]["dd"]["verify_command"] = [PY, "-c", "import sys; sys.exit(1)"]
        self.cycle()
        rows = self.run_rows()
        self.assertEqual([r["status"] for r in rows], [runs.FAILED, runs.FAILED])
        self.assertIn("Verification failed", rows[-1]["error"])
        self.assertEqual(len(sandbox_calls), 1)  # verify only
        self.assertNotIn("dd-7", git("branch", "--list", cwd=self.origin))
        self.assertEqual(prs, {})
        self.assertEqual(board.issues["i1"]["state"], {"id": S_BLK})

    def test_uncommitted_changes_on_resume_block_and_push_nothing(self):
        self.block_before_push()
        (self.wt_root / "dd-7" / "stray.py").write_text("x = 1\n")
        self.cycle()
        rows = self.run_rows()
        self.assertEqual(rows[-1]["status"], runs.FAILED)
        self.assertIn("uncommitted changes", rows[-1]["error"])
        self.assertEqual(sandbox_calls, [])
        self.assertNotIn("dd-7", git("branch", "--list", cwd=self.origin))

    def test_normal_rework_still_runs_the_agent(self):
        self.add_issue()
        self.cycle()
        board.issues["i1"]["state"] = {"id": S_IP}
        sandbox_calls.clear()
        self.cycle()
        self.assertEqual(len(sandbox_calls), 2)  # agent, then verify
        self.assertEqual(sandbox_calls[0]["env_file"], "/etc/dynamic-dispatch/claude.env")
        self.assertEqual(git("rev-list", "--count", "main..dd-7", cwd=self.origin).strip(), "2")

    def test_has_unpushed_commits(self):
        self.assertFalse(gitops.has_unpushed_commits(str(self.repo), "main"))
        git("branch", "dd-7", "origin/main", cwd=self.repo)
        self.assertFalse(gitops.has_unpushed_commits(str(self.repo), "dd-7"))
        self.block_before_push()
        self.assertTrue(gitops.has_unpushed_commits(str(self.repo), "dd-7"))



class FinishedWorkRecheck(Base):
    """A story pulled out of In Progress mid-work, then moved back."""

    def pull_out(self):
        self.add_issue()
        board.get_states = [S_IP, S_BACK]  # start check passes, pre-push check finds it moved
        self.cycle()
        self.assertEqual(self.run_rows()[0]["status"], runs.CANCELLED)
        board.issues["i1"]["state"] = {"id": S_IP}
        board.issues["i1"]["description_stripped"] = "Add OTHER to feature.py"  # edited meanwhile
        sandbox_calls.clear()
        return git("rev-parse", "dd-7", cwd=self.repo).strip()

    def local_branches(self):
        return git("branch", "--list", "--format=%(refname:short)", cwd=self.repo).split()

    def test_moved_back_checks_current_story_and_keeps_both_commits(self):
        old = self.pull_out()
        self.set_mode("prompt")
        self.cycle()
        rows = self.run_rows()
        self.assertEqual([r["status"] for r in rows], [runs.CANCELLED, runs.DONE], rows[-1])
        self.assertEqual(len(sandbox_calls), 2)  # agent, then verify
        self.assertEqual(git("rev-list", "--count", "main..dd-7", cwd=self.origin).strip(), "2")
        prompt = git("show", "dd-7:prompt.txt", cwd=self.origin)
        self.assertIn("Add OTHER", prompt)
        self.assertIn(worker.FINISHED_WORK_NOTE, prompt)
        self.assertIn("VALUE = 1", git("show", "dd-7:feature.py", cwd=self.origin))
        self.assertEqual(git("rev-parse", "dd-7^", cwd=self.origin).strip(), old)
        self.assertEqual(self.local_branches(), ["dd-7", "main"])
        self.assertNotIn("set_aside", [e["type"] for e in runs.events(self.conn, project="dd", issue_id="i1")])
        self.assertEqual(len(prs), 1)
        self.assertEqual(board.issues["i1"]["state"], {"id": S_REV})

    def test_no_changes_still_tests_and_opens_review(self):
        old = self.pull_out()
        self.set_mode("finished")
        self.cycle()
        rows = self.run_rows()
        self.assertEqual(rows[-1]["status"], runs.DONE, rows[-1])
        self.assertEqual(len(sandbox_calls), 2)
        self.assertEqual(sandbox_calls[1]["command"][1:3], ["-m", "compileall"])
        self.assertEqual(git("rev-parse", "dd-7", cwd=self.origin).strip(), old)
        self.assertIn("already meets", rows[-1]["summary"])
        self.assertEqual(len(prs), 1)
        self.assertEqual(board.issues["i1"]["state"], {"id": S_REV})
        self.assertEqual(self.local_branches(), ["dd-7", "main"])

    def test_no_changes_combines_latest_main_before_review(self):
        old = self.pull_out()
        MainChanges.advance_main(self)
        self.set_mode("finished")
        self.cycle()
        row = self.run_rows()[-1]
        self.assertEqual(row["status"], runs.DONE, row)
        self.assertEqual(len(sandbox_calls), 3)  # agent, verify, verify combination
        self.assertEqual(git("rev-parse", "dd-7^1", cwd=self.origin).strip(), old)
        self.assertEqual(git("rev-parse", "dd-7^2", cwd=self.origin),
                         git("rev-parse", "main", cwd=self.origin))
        self.assertIn("VALUE = 1", git("show", "dd-7:feature.py", cwd=self.origin))
        self.assertIn("MAIN = 2", git("show", "dd-7:main.py", cwd=self.origin))
        self.assertEqual(board.issues["i1"]["state"], {"id": S_REV})
        self.assertEqual(len(prs), 1)

    def test_no_changes_failing_verification_does_not_upload(self):
        old = self.pull_out()
        self.set_mode("finished")
        with mock.patch.object(verify, "run_verify", return_value=(False, "bad tests")):
            self.cycle()
        self.assertEqual(self.run_rows()[-1]["status"], runs.FAILED)
        self.assertIn("Verification failed", self.run_rows()[-1]["error"])
        self.assertEqual(git("rev-parse", "dd-7", cwd=self.repo).strip(), old)
        self.assertNotIn("dd-7", git("branch", "--list", cwd=self.origin))
        self.assertEqual(prs, {})

    def test_existing_set_aside_branch_is_left_alone(self):
        self.pull_out()
        name = "dd-7-set-aside-1"
        git("branch", name, "origin/main", cwd=self.repo)
        old = git("rev-parse", name, cwd=self.repo)
        self.set_mode("finished")
        self.cycle()
        self.assertEqual(self.run_rows()[-1]["status"], runs.DONE)
        self.assertEqual(git("rev-parse", name, cwd=self.repo), old)
        self.assertEqual(self.local_branches(), ["dd-7", name, "main"])
        self.assertNotIn(name, git("branch", "--list", cwd=self.origin))

    def test_blocked_after_saving_more_work_still_resumes(self):
        self.pull_out()
        board.get_states = [S_IP, "FAIL"]
        self.cycle()
        self.assertEqual(self.run_rows()[-1]["status"], runs.FAILED)
        board.issues["i1"]["state"] = {"id": S_IP}
        sandbox_calls.clear()
        self.set_mode("fail")
        self.cycle()
        rows = self.run_rows()
        self.assertEqual([r["status"] for r in rows], [runs.CANCELLED, runs.FAILED, runs.DONE], rows[-1])
        self.assertEqual(len(sandbox_calls), 1)  # verify only
        self.assertEqual(self.local_branches(), ["dd-7", "main"])

    def test_cancel_before_start_does_not_hide_the_pulled_out_work(self):
        old = self.pull_out()
        board.fail_get = 1
        self.cycle()
        self.set_mode("finished")
        self.cycle()
        rows = self.run_rows()
        self.assertEqual([r["status"] for r in rows], [runs.CANCELLED, runs.CANCELLED, runs.DONE])
        self.assertEqual(git("rev-parse", "dd-7", cwd=self.origin).strip(), old)
        self.assertEqual(self.local_branches(), ["dd-7", "main"])

    def test_uncommitted_changes_block_and_nothing_is_renamed(self):
        old = self.pull_out()
        (self.wt_root / "dd-7" / "stray.py").write_text("x = 1\n")
        self.cycle()
        rows = self.run_rows()
        self.assertEqual(rows[-1]["status"], runs.FAILED)
        self.assertIn("uncommitted changes", rows[-1]["error"])
        self.assertEqual(self.local_branches(), ["dd-7", "main"])
        self.assertEqual(git("rev-parse", "dd-7", cwd=self.repo).strip(), old)
        self.assertTrue((self.wt_root / "dd-7" / "stray.py").exists())
        self.assertNotIn("dd-7", git("branch", "--list", cwd=self.origin))

    def test_normal_rework_runs_the_agent_on_the_existing_branch(self):
        self.add_issue()
        self.cycle()
        board.issues["i1"]["state"] = {"id": S_IP}
        sandbox_calls.clear()
        self.cycle()
        self.assertEqual(len(sandbox_calls), 2)  # agent, then verify
        self.assertEqual(git("rev-list", "--count", "main..dd-7", cwd=self.origin).strip(), "2")
        self.assertEqual(self.local_branches(), ["dd-7", "main"])

    def test_database_from_before_the_committed_column_is_upgraded(self):
        path = self.tmp / "old.db"
        old = sqlite3.connect(path)
        self.addCleanup(old.close)
        old.executescript(runs.SCHEMA.replace("    committed       INTEGER NOT NULL DEFAULT 0,\n", ""))
        old.close()
        conn = runs.connect(path)
        self.addCleanup(conn.close)
        columns = {c["name"] for c in conn.execute("PRAGMA table_info(runs)")}
        conn.close()
        self.assertIn("committed", columns)



class RetryPullRequest(Base):
    """A story whose branch was pushed but whose pull request could not be opened."""

    def block_at_pr(self):
        self.add_issue()
        pr_failures.append("POST /repos/owner/repo/pulls -> 502: Bad Gateway")
        self.cycle()
        [row] = self.run_rows()
        self.assertEqual(row["status"], runs.FAILED, row)
        self.assertEqual((row["committed"], row["pushed"]), (1, 1))
        self.assertIn("pull request could not be opened", row["error"])
        self.assertIn("move the story back to In Progress", row["error"])
        self.assertIn("dd-7", git("branch", "--list", cwd=self.origin))
        self.assertEqual(prs, {})
        board.issues["i1"]["state"] = {"id": S_IP}  # human moves it back from the iPad
        sandbox_calls.clear()
        self.set_mode("fail")  # would block the story if it ran
        return git("rev-parse", "dd-7", cwd=self.origin).strip()

    def test_moved_back_opens_the_pr_without_running_the_agent(self):
        pushed = self.block_at_pr()
        self.cycle()
        rows = self.run_rows()
        self.assertEqual([r["status"] for r in rows], [runs.FAILED, runs.DONE], rows[-1])
        # only verify ran, not the agent
        self.assertEqual(len(sandbox_calls), 1)
        self.assertEqual(sandbox_calls[0]["command"][1:3], ["-m", "compileall"])
        self.assertEqual(git("rev-parse", "dd-7", cwd=self.origin).strip(), pushed)
        [pr] = prs.values()
        self.assertEqual(pr["head"], "dd-7")
        self.assertIn("pull request opened without running the agent", pr["body"])
        self.assertEqual(rows[-1]["pr_url"], pr["url"])
        self.assertEqual(board.issues["i1"]["state"], {"id": S_REV})
        self.assertIn(pr["url"], board.comments[-1][2])

    def test_failing_twice_blocks_again_and_the_next_move_back_retries(self):
        pushed = self.block_at_pr()
        pr_failures.append("POST /repos/owner/repo/pulls -> 502: Bad Gateway")
        self.cycle()
        rows = self.run_rows()
        self.assertEqual([r["status"] for r in rows], [runs.FAILED, runs.FAILED])
        self.assertIn("pull request could not be opened", rows[-1]["error"])
        self.assertIn("move the story back to In Progress", rows[-1]["error"])
        self.assertEqual(board.issues["i1"]["state"], {"id": S_BLK})
        self.assertEqual(len(sandbox_calls), 1)  # verify only
        board.issues["i1"]["state"] = {"id": S_IP}
        sandbox_calls.clear()
        self.cycle()
        rows = self.run_rows()
        self.assertEqual([r["status"] for r in rows], [runs.FAILED, runs.FAILED, runs.DONE], rows[-1])
        self.assertEqual(len(sandbox_calls), 1)  # verify only
        self.assertEqual(git("rev-parse", "dd-7", cwd=self.origin).strip(), pushed)
        self.assertEqual(len(prs), 1)

    def test_retry_still_checks_in_progress_before_opening(self):
        self.block_at_pr()
        board.get_states = [S_IP, "FAIL"]
        self.cycle()
        rows = self.run_rows()
        self.assertEqual([r["status"] for r in rows], [runs.FAILED, runs.FAILED])
        self.assertIn("pull request was not opened", rows[-1]["error"])
        self.assertEqual(prs, {})
        board.issues["i1"]["state"] = {"id": S_IP}
        self.cycle()
        self.assertEqual(self.run_rows()[-1]["status"], runs.DONE)
        self.assertEqual(len(prs), 1)

    def test_already_merged_branch_blocks_without_an_empty_pr(self):
        self.block_at_pr()
        # Merged on GitHub; the push also moves the repository's known origin/main.
        git("push", "origin", "origin/dd-7:refs/heads/main", cwd=self.repo)
        self.cycle()
        rows = self.run_rows()
        self.assertEqual([r["status"] for r in rows], [runs.FAILED, runs.FAILED])
        self.assertIn("no commits that main doesn't already have", rows[-1]["error"])
        self.assertIn("already have been merged", rows[-1]["error"])
        self.assertEqual(board.issues["i1"]["state"], {"id": S_BLK})
        self.assertEqual(sandbox_calls, [])
        self.assertEqual(prs, {})

    def test_uncommitted_changes_block_and_open_nothing(self):
        self.block_at_pr()
        (self.wt_root / "dd-7" / "stray.py").write_text("x = 1\n")
        self.cycle()
        rows = self.run_rows()
        self.assertEqual(rows[-1]["status"], runs.FAILED)
        self.assertIn("uncommitted changes", rows[-1]["error"])
        self.assertEqual(sandbox_calls, [])
        self.assertEqual(prs, {})

    def test_rework_after_the_retry_runs_the_agent(self):
        self.block_at_pr()
        self.cycle()
        self.assertEqual(self.run_rows()[-1]["status"], runs.DONE)
        board.issues["i1"]["state"] = {"id": S_IP}  # sent back from review
        sandbox_calls.clear()
        self.set_mode("edit")
        self.cycle()
        self.assertEqual(len(sandbox_calls), 2)  # agent, then verify
        self.assertEqual(git("rev-list", "--count", "main..dd-7", cwd=self.origin).strip(), "2")
        self.assertEqual(len(prs), 1)

    def test_normal_rework_is_unchanged(self):
        self.add_issue()
        self.cycle()
        self.assertEqual(self.run_rows()[0]["pushed"], 1)
        board.issues["i1"]["state"] = {"id": S_IP}
        sandbox_calls.clear()
        self.cycle()
        rows = self.run_rows()
        self.assertEqual([r["status"] for r in rows], [runs.DONE, runs.DONE])
        self.assertEqual(len(sandbox_calls), 2)  # agent, then verify
        self.assertEqual(git("rev-list", "--count", "main..dd-7", cwd=self.origin).strip(), "2")
        self.assertEqual(len(prs), 1)

    def test_unpushed_resume_is_unchanged(self):
        self.add_issue()
        board.get_states = [S_IP, "FAIL"]  # blocked before its push
        self.cycle()
        self.assertEqual(self.run_rows()[0]["pushed"], 0)
        board.issues["i1"]["state"] = {"id": S_IP}
        sandbox_calls.clear()
        self.set_mode("fail")
        self.cycle()
        rows = self.run_rows()
        self.assertEqual([r["status"] for r in rows], [runs.FAILED, runs.DONE], rows[-1])
        self.assertEqual(rows[-1]["pushed"], 1)
        self.assertEqual(len(sandbox_calls), 1)  # verify only
        self.assertIn("pushed without running the agent", rows[-1]["summary"])
        self.assertIn("dd-7", git("branch", "--list", cwd=self.origin))

    def test_has_commits_beyond_base(self):
        git("branch", "dd-7", "origin/main", cwd=self.repo)
        self.assertFalse(gitops.has_commits_beyond_base(str(self.repo), "dd-7", "main"))
        self.block_at_pr()
        self.assertTrue(gitops.has_commits_beyond_base(str(self.repo), "dd-7", "main"))

    def test_has_commits_beyond_base_does_not_fetch(self):
        self.block_at_pr()
        known = git("rev-parse", "refs/remotes/origin/main", cwd=self.repo)
        # Merged on GitHub, but not yet fetched here.
        git("push", str(self.origin), "dd-7:refs/heads/main", cwd=self.repo)
        self.assertTrue(gitops.has_commits_beyond_base(str(self.repo), "dd-7", "main"))
        self.assertEqual(git("rev-parse", "refs/remotes/origin/main", cwd=self.repo), known)

    def test_database_from_before_the_pushed_column_is_upgraded(self):
        path = self.tmp / "old.db"
        old = sqlite3.connect(path)
        self.addCleanup(old.close)
        old.executescript(runs.SCHEMA.replace("    pushed          INTEGER NOT NULL DEFAULT 0,\n", "")
                                     .replace("    committed       INTEGER NOT NULL DEFAULT 0,\n", ""))
        old.execute("INSERT INTO runs (project, issue_id, issue_key, issue_name, agent, branch, status, queued_at) "
                    "VALUES ('dd', 'i1', '7', 'x', 'claude-code', 'dd-7', 'failed', 'then')")
        old.commit(); old.close()
        conn = runs.connect(path)
        self.addCleanup(conn.close)
        [row] = conn.execute("SELECT * FROM runs").fetchall()
        conn.close()
        self.assertEqual((row["committed"], row["pushed"]), (0, 0))



class UnfinishedWork(Base):
    """Uncommitted changes an earlier run left behind are handed on to the next agent."""

    def stop_partway(self):
        self.add_issue()
        self.set_mode("limit_mixed")
        self.cycle()
        self.assertEqual(self.run_rows()[0]["status"], runs.LIMITED)
        with self.conn:
            self.conn.execute("UPDATE cooldowns SET until = ?", ("2000-01-01T00:00:00+00:00",))

    def test_same_agent_resuming_is_told_about_its_unfinished_changes(self):
        os.environ["LEAK"] = CLAUDE
        self.stop_partway()
        self.set_mode("prompt")
        self.cycle()
        rows = self.run_rows()
        self.assertEqual([r["status"] for r in rows], [runs.LIMITED, runs.DONE], rows[-1])
        prompt = git("show", "dd-7:prompt.txt", cwd=self.origin)
        self.assertIn("Claude worked on this story in an earlier run", prompt)
        self.assertIn("They were never tested.", prompt)
        for line in ("- added: partial.py", "- changed: app.py", "- deleted: DEFINITION_OF_DONE.md"):
            self.assertIn(line, prompt)
        self.assertIn("+HALF = 1", prompt)
        self.assertNotIn(CLAUDE, prompt)
        self.assertNotIn("cut at", prompt)
        types = [e["type"] for e in runs.events(self.conn, run_id=rows[1]["id"])]
        self.assertIn("unfinished_found", types)
        self.assertIn("unfinished_in_prompt", types)
        self.assertNotIn("unfinished_restored", types)

    def test_clean_folder_gets_no_note(self):
        self.add_issue()
        self.cycle()
        board.issues["i1"]["state"] = {"id": S_IP}  # rework, in the same, now clean, folder
        self.set_mode("prompt")
        self.cycle()
        rows = self.run_rows()
        self.assertEqual([r["status"] for r in rows], [runs.DONE, runs.DONE])
        prompt = git("show", "dd-7:prompt.txt", cwd=self.origin)
        self.assertNotIn("uncommitted changes", prompt)
        self.assertNotIn("Changed files:", prompt)
        types = {e["type"] for e in runs.events(self.conn)}
        self.assertFalse(types & {"unfinished_found", "unfinished_restored", "unfinished_in_prompt"})
        tree, _ = gitops.ensure_worktree(self.repo, self.wt_root, "dd-7", "main", group=os.getegid())
        self.assertIsNone(gitops.unfinished_changes(self.repo, tree, worker.UNFINISHED_LIMIT))

    def test_large_changes_are_cut_and_every_file_still_listed(self):
        tree, _ = gitops.ensure_worktree(self.repo, self.wt_root, "dd-7", "main", group=os.getegid())
        names = [f"big{i}.py" for i in range(5)]
        for name in names:
            (tree / name).write_text("X = 1\n" * 4000)
        (tree / "app.py").write_text("print('changed')\n")
        (tree / "DEFINITION_OF_DONE.md").unlink()
        changes = gitops.unfinished_changes(self.repo, tree, worker.UNFINISHED_LIMIT)
        note, cut = worker.unfinished_note("Codex", changes)
        self.assertTrue(cut)
        self.assertIn("cut at 50 KB", note)
        for name in names:
            self.assertIn(f"- added: {name}\n", note)
        self.assertIn("- changed: app.py\n", note)
        self.assertIn("- deleted: DEFINITION_OF_DONE.md\n", note)
        diff = note.split("the rest):\n", 1)[1]
        self.assertLessEqual(len(diff.encode()), worker.UNFINISHED_LIMIT + 1)
        self.assertIn("+print('changed')", diff)

    def test_secret_is_scrubbed_before_the_cut(self):
        # A secret straddling the 50 KB mark must not leave its first half behind.
        tree, _ = gitops.ensure_worktree(self.repo, self.wt_root, "dd-7", "main", group=os.getegid())
        changes = gitops.unfinished_changes(self.repo, tree, worker.UNFINISHED_LIMIT)
        self.assertIsNone(changes)
        diff = "+" + "a" * (worker.UNFINISHED_LIMIT - 5) + GH + "\n"
        note, cut = worker.unfinished_note("Claude", {"files": [("added", "x.py")], "diff": diff})
        self.assertTrue(cut)
        self.assertNotIn(GH[:4], note.split("the rest):\n", 1)[1][-20:])
        note, cut = worker.unfinished_note("Claude", {"files": [("added", "x.py")], "diff": f"+T = '{GH}'\n"})
        self.assertFalse(cut)
        self.assertNotIn(GH, note)
        self.assertIn("+T = '[redacted]'", note)

    def test_restore_refuses_changes_saved_from_another_commit(self):
        tree, _ = gitops.ensure_worktree(self.repo, self.wt_root, "dd-7", "main", group=os.getegid())
        author = self.cfg["agents"]["claude-code"]["git_author"]
        (tree / "leftover.txt").write_text("unfinished")
        self.assertEqual(gitops.rebuild_for_group(self.repo, tree, "dd-7", "dd-7-leftover-1", author),
                         "dd-7-leftover-1")
        tree, _ = gitops.ensure_worktree(self.repo, self.wt_root, "dd-7", "main", group=os.getegid())
        (tree / "saved.txt").write_text("saved")
        gitops.commit_all(self.repo, tree, "moved on", author)
        with self.assertRaisesRegex(gitops.GitError, "not restored"):
            gitops.restore_unfinished(self.repo, tree, "dd-7", "dd-7-leftover-1", os.getegid())
        self.assertFalse((tree / "leftover.txt").exists())


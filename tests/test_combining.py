"""Tests for combining."""

import json, os, stat
from unittest import mock
from pathlib import Path
from dispatch import agents, choose, combine, github, gitops, plane, runs, sync, text, verify
import main as dd_main
from helpers import (
    Base,
    GH,
    MainChanges,
    ORCH,
    S_BLK,
    S_IP,
    S_REV,
    board,
    fake_gh_request,
    fake_plane_request,
    git,
    os,
    pr_failures,
    prs,
)


class CombineMain(Base):
    advance_main = MainChanges.advance_main

    def combine(self, conflict=False, repair=True, fail_tests=False):
        self.add_issue()
        original = agents.run
        prompts = []
        def agent(cfg, agent_cfg, prompt, worktree):
            prompts.append(prompt)
            if len(prompts) > 1:
                self.set_mode("repair" if repair else "garbage")
            result = original(cfg, agent_cfg, prompt, worktree)
            if len(prompts) == 1:
                self.advance_main(conflict)
            return result
        real_verify = verify.run_verify
        calls = []
        def check(*args):
            calls.append(1)
            if fail_tests and len(calls) == 2:
                return False, "1 failed: test_both: incompatible value"
            return real_verify(*args)
        with mock.patch.object(agents, "run", side_effect=agent), \
                mock.patch.object(verify, "run_verify", side_effect=check):
            self.cycle()
        return self.run_rows()[0], prompts

    def test_title_lookup_once_for_all_repair_comments(self):
        with mock.patch.object(github, "_request", wraps=fake_gh_request) as request:
            row, _ = self.combine(conflict=True, repair=False)
        lookups = [c for c in request.call_args_list if c.args[1].endswith("/pulls/42")]
        self.assertEqual(len(lookups), 1)
        self.assertEqual(lookups[0].args, ("GET", "/repos/owner/repo/pulls/42", GH))
        self.assertEqual(row["status"], runs.FAILED)
        for comment in board.comments:
            self.assertIn("Add main behaviour", comment[2])
            self.assertNotIn("Merge pull request", comment[2])

    def test_failed_title_lookup_keeps_run_going(self):
        with mock.patch.object(github, "pr_title", side_effect=TimeoutError("timed out")):
            row, _ = self.combine(conflict=True)
        self.assertEqual(row["status"], runs.DONE, row)
        self.assertIn("Merge pull request #42 from topic", board.comments[0][2])
        self.assertTrue(any(e["type"] == "warning" and "#42" in e["detail"]
                            for e in runs.events(self.conn, run_id=row["id"])))

    def test_commit_fallbacks_and_non_merge_title(self):
        self.add_issue()
        self.cycle()
        self.advance_main(message="Merge pull request #42 from topic\n\nFallback story")
        self.advance_main(conflict=True, message="Ordinary commit\n\nBody is not the title")
        tree, _ = gitops.ensure_worktree(self.repo, self.wt_root, "dd-7", "main")
        combined = gitops.combine_main(self.repo, tree, "dd-7", "main",
                                      self.cfg["agents"]["claude-code"]["git_author"])
        self.assertEqual(combined["pr_numbers"], [42, None])
        for error in (github.GitHubError("404"), github.GitHubError("rate limit")):
            with mock.patch.object(github, "pr_title", side_effect=error) as lookup:
                titles = combine._combined_titles(combined, self.cfg["github"], "owner/repo", mock.Mock())
            self.assertEqual(titles, ["Fallback story", "Ordinary commit"])
            lookup.assert_called_once()

    def test_github_title_is_one_line_and_capped(self):
        with mock.patch.object(github, "_request", return_value={"title": "Story\r\n" + "x" * 250}):
            title = github.pr_title("owner/repo", 42, GH)
        self.assertEqual(title, "Story " + "x" * 194)

    def test_repeated_pr_number_is_looked_up_once(self):
        combined = {"titles": ["first", "second", "ordinary"], "pr_numbers": [42, 42, None]}
        with mock.patch.object(github, "pr_title", return_value="Story") as lookup:
            self.assertEqual(combine._combined_titles(combined, self.cfg["github"], "owner/repo", mock.Mock()),
                             ["Story", "Story", "ordinary"])
        lookup.assert_called_once()

    def test_main_unchanged_keeps_original_commit(self):
        self.add_issue()
        self.cycle()
        self.assertEqual(self.run_rows()[0]["status"], runs.DONE)
        self.assertEqual(len(git("rev-list", "--parents", "-1", "dd-7", cwd=self.origin).split()), 2)
        self.assertNotIn("Combined with", board.comments[0][2])

    def test_external_merge_driver_refused(self):
        git("config", "merge.evil.driver", f"touch {self.marker}", cwd=self.repo)
        row, prompts = self.combine()
        self.assertEqual(row["status"], runs.FAILED)
        self.assertIn("merge drivers", row["error"])
        self.assertFalse(self.marker.exists())
        self.assertFalse(prs)

    def test_marker_scan_only_conflicted_files_without_following_symlinks(self):
        folder = self.tmp / "scan"
        folder.mkdir()
        (folder / "untracked.txt").write_text("<<<<<<< ours\nours\n=======\ntheirs\n>>>>>>> theirs\n")
        outside = self.tmp / "outside"
        outside.write_text("<<<<<<< outside\n")
        (folder / "link").symlink_to(outside)
        (folder / "heading.txt").write_text("Heading\n=======\n")
        detail = gitops.conflict_markers(folder, ["untracked.txt", "link"])
        self.assertIn("untracked.txt", detail)
        self.assertIn("4: theirs", detail)
        self.assertNotIn("outside", detail)

    def test_merge_files_writable_without_chmod_external_hardlinks(self):
        tree, _ = gitops.ensure_worktree(self.repo, self.wt_root, "dd-7", "main")
        external = self.tmp / "external"
        external.write_text("keep mode")
        external.chmod(0o600)
        os.link(external, tree / "hardlink")
        self.advance_main()
        gitops.combine_main(self.repo, tree, "dd-7", "main", self.cfg["agents"]["claude-code"]["git_author"])
        self.assertTrue((tree / "main.py").stat().st_mode & stat.S_IWGRP)
        self.assertEqual((tree / "main.py").stat().st_gid, tree.stat().st_gid)
        self.assertEqual(stat.S_IMODE(tree.stat().st_mode), 0o2770)
        self.assertEqual(stat.S_IMODE(external.stat().st_mode), 0o600)

    def test_new_pickup_aborts_merge_and_starts_with_zero_tries(self):
        row, _ = self.combine(conflict=True, repair=False)
        self.assertEqual(runs.repair_attempts(self.conn, row["id"]), 2)
        original_error = row["error"]
        tree = self.wt_root / "dd-7"
        (tree / "leftover.txt").write_text("unfinished repair")
        board.issues["i1"]["state"] = {"id": S_IP}
        self.set_mode("repair")
        original = gitops.combine_main
        def combine(*args):
            current = self.run_rows()[-1]
            self.assertEqual(current["repair_attempts"], 0)
            self.assertFalse((tree / "leftover.txt").exists())
            self.assertEqual((tree / "feature.py").read_text(), "VALUE = 1\n")
            return original(*args)
        with mock.patch.object(gitops, "combine_main", side_effect=combine):
            self.cycle()
        latest = self.run_rows()[-1]
        self.assertEqual(latest["status"], runs.DONE, latest)
        self.assertEqual(latest["repair_attempts"], 1)
        self.assertEqual(runs.get(self.conn, row["id"])["error"], original_error)
        self.assertIn("merge_aborted", [e["type"] for e in runs.events(self.conn, run_id=latest["id"])])

    def test_abort_restores_edited_clean_merge_and_preserves_non_merge_edits(self):
        tree, _ = gitops.ensure_worktree(self.repo, self.wt_root, "dd-7", "main")
        saved = git("rev-parse", "HEAD", cwd=tree)
        (tree / "scratch.txt").write_text("keep without merge")
        self.assertFalse(gitops.abort_pending_merge(self.repo, tree))
        self.assertTrue((tree / "scratch.txt").exists())
        self.advance_main()
        gitops.combine_main(self.repo, tree, "dd-7", "main", self.cfg["agents"]["claude-code"]["git_author"])
        (tree / "main.py").write_text("unfinished edits")
        (tree / "app.py").write_text("unfinished repair")
        (tree / "app.py").chmod(0o644)
        previous_umask = os.umask(0o022)
        try:
            self.assertTrue(gitops.abort_pending_merge(self.repo, tree))
        finally:
            os.umask(previous_umask)
        self.assertEqual((tree / "app.py").read_text(), "print('hi')\n")
        self.assertTrue((tree / "app.py").stat().st_mode & stat.S_IWGRP)
        self.assertEqual((tree / "app.py").stat().st_gid, tree.stat().st_gid)
        self.assertEqual(stat.S_IMODE(tree.stat().st_mode), 0o2770)
        self.assertEqual(git("rev-parse", "HEAD", cwd=tree), saved)
        self.assertFalse(gitops.has_changes(self.repo, tree))
        self.assertFalse((gitops.admin_dir(self.repo, tree) / "MERGE_HEAD").exists())

    def test_group_rebuild_cancels_pending_merge(self):
        tree, _ = gitops.ensure_worktree(self.repo, self.wt_root, "dd-7", "main", group=os.getegid())
        author = self.cfg["agents"]["claude-code"]["git_author"]
        saved = git("rev-parse", "dd-7", cwd=self.repo)
        self.advance_main()
        gitops.combine_main(self.repo, tree, "dd-7", "main", author)
        (tree / "main.py").write_text("unfinished repair")
        self.assertIsNone(gitops.rebuild_for_group(self.repo, tree, "dd-7", "dd-7-leftover-1", author))
        tree, _ = gitops.ensure_worktree(self.repo, self.wt_root, "dd-7", "main", group=os.getegid())
        self.assertEqual(git("rev-parse", "dd-7", cwd=self.repo), saved)
        self.assertFalse((tree / "main.py").exists())
        self.assertFalse((gitops.admin_dir(self.repo, tree) / "MERGE_HEAD").exists())
        self.assertEqual(stat.S_IMODE(tree.stat().st_mode), 0o2770)

    def test_failure_summary_uses_names_or_count_without_output(self):
        self.assertEqual(combine._failure_names("FAIL: test_one (Suite)\ntraceback\nFAILED (failures=1)"),
                         "test_one (Suite)")
        self.assertEqual(combine._failure_names("FAILED tests/test_x.py::test_one - detail"),
                         "tests/test_x.py::test_one")
        self.assertEqual(combine._failure_names("traceback\nFAILED (failures=2, errors=1)"),
                         "3 failing tests (names unavailable)")

    def test_heading_underline_does_not_block_repair(self):
        original = agents.run
        def agent(*args):
            result = original(*args)
            (Path(args[-1]) / "notes.txt").write_text("Heading\n=======\n")
            return result
        with mock.patch.object(agents, "run", side_effect=agent):
            row, _ = self.combine(conflict=True)
        self.assertEqual(row["status"], runs.DONE, row)

    def test_repair_usage_added_to_original(self):
        row, _ = self.combine(conflict=True)
        self.assertEqual(json.loads(row["usage"])["output_tokens"], 10)
        self.assertAlmostEqual(row["cost_usd"], 0.02)
        usage = runs.usage_records(self.conn)[0]
        self.assertEqual(json.loads(usage["usage"])["output_tokens"], 10)
        self.assertAlmostEqual(usage["cost_usd"], 0.02)

    def test_send_back_failure_does_not_stop_repair(self):
        with mock.patch.object(sync, "send_back", side_effect=plane.PlaneError("unavailable")):
            row, prompts = self.combine(conflict=True)
        self.assertEqual(row["status"], runs.DONE, row)
        self.assertEqual(len(prompts), 2)
        self.assertTrue(any(e["type"] == "warning" and "continuing repair" in e["detail"]
                            for e in runs.events(self.conn, run_id=row["id"])))

    def test_pr_retry_combines_new_main(self):
        self.add_issue()
        pr_failures.append(503)
        self.cycle()
        self.advance_main()
        board.issues["i1"]["state"] = {"id": S_IP}
        with mock.patch.object(agents, "run", side_effect=AssertionError("must reuse saved work")):
            self.cycle()
        row = self.run_rows()[-1]
        self.assertEqual(row["status"], runs.DONE, row)
        self.assertEqual(row["pushed"], 1)
        self.assertIn("MAIN = 2", git("show", "dd-7:main.py", cwd=self.origin))

    def test_repair_notice_is_idempotent(self):
        self.add_issue()
        rid = runs.enqueue(self.conn, project="dd", issue=board.issues["i1"], agent="claude-code", branch="dd-7")
        row = runs.get(self.conn, rid)
        sync.send_back(self.cfg, row, 1, "repair")
        sync.send_back(self.cfg, row, 1, "repair")
        self.assertEqual(len(board.comments), 1)
        self.assertEqual(board.comments[0][0], ORCH)

    def test_clean_combine(self):
        row, prompts = self.combine()
        self.assertEqual(row["status"], runs.DONE, row)
        self.assertEqual(len(prompts), 1)
        self.assertEqual(len(git("rev-list", "--parents", "-1", "dd-7", cwd=self.origin).split()), 3)
        self.assertIn("Combined with the latest main (Add main behaviour) and re-tested.", board.comments[-1][2])
        self.assertEqual(git("show", "dd-7:main.py", cwd=self.origin).strip(), "MAIN = 2")

    def test_clash_repaired(self):
        row, prompts = self.combine(conflict=True)
        self.assertEqual(row["status"], runs.DONE, row)
        self.assertEqual(len(prompts), 2)
        self.assertIn("Preserve your earlier story work", prompts[1])
        self.assertIn("Add main behaviour", prompts[1])
        self.assertIn("<<<<<<<", prompts[1])
        self.assertEqual(board.comments[0][0], ORCH)
        self.assertIn("While this story was being built, Add main behaviour was merged into main. "
                      "Combining them clashed in feature.py. Sent back to Claude to resolve (try 1 of 2).",
                      board.comments[0][2])
        self.assertEqual(git("show", "dd-7:feature.py", cwd=self.origin), "VALUE = 1\nMAIN = 2\n")

    def test_tests_repaired(self):
        row, prompts = self.combine(fail_tests=True)
        self.assertEqual(row["status"], runs.DONE, row)
        self.assertEqual(len(prompts), 2)
        self.assertIn("1 failed: test_both", prompts[1])
        self.assertIn("Combined this story with the latest main (Add main behaviour), and 1 "
                      "tests failed: test_both. "
                      "Sent back to Claude to fix (try 1 of 2).", board.comments[0][2])

    def test_markers_left_twice_block_with_sections(self):
        row, prompts = self.combine(conflict=True, repair=False)
        self.assertEqual(row["status"], runs.FAILED, row)
        self.assertEqual(len(prompts), 3)
        self.assertFalse(prs)
        self.assertIn("Couldn't combine this story with the latest main after 2 tries. "
                      "Merged meanwhile: Add main behaviour.", row["error"])
        for text in ("feature.py", "<<<<<<<", "VALUE = 1", "MAIN = 2", ">>>>>>>"):
            self.assertIn(text, row["error"])
        self.assertEqual([c[0] for c in board.comments], [ORCH, ORCH, ORCH])
        self.assertIn("try 2 of 2", board.comments[1][2])
        self.assertEqual(git("rev-list", "--count", "dd-7", cwd=self.repo).strip(), "2")

    def test_shared_limit_clash_then_tests(self):
        with mock.patch.object(verify, "run_verify", side_effect=[(True, "ok"), (False, "bad"), (False, "bad")]):
            row, prompts = self.combine(conflict=True)
        self.assertEqual(row["status"], runs.FAILED, row)
        self.assertEqual(len(prompts), 3)
        self.assertIn("bad", row["error"])
        self.assertIn("to fix (try 2 of 2)", board.comments[1][2])



class RefreshPullRequests(Base):
    advance_main = MainChanges.advance_main

    def prepare(self, conflict=False):
        self.add_issue()
        self.cycle()
        self.old_head = git("rev-parse", "dd-7", cwd=self.origin)
        self.advance_main(conflict)

    def test_current_main_is_left_alone(self):
        self.add_issue(); self.cycle()
        with mock.patch.object(agents, "run") as agent, mock.patch.object(gitops, "push") as push:
            self.cycle()
        agent.assert_not_called(); push.assert_not_called()
        self.assertEqual(len(self.run_rows()), 1)
        self.assertIn("main_unchanged", [e["type"] for e in runs.events(self.conn)])

    def test_done_stories_only_need_the_board_list(self):
        self.prepare()
        board.issues["i1"]["state"] = "done"
        with mock.patch.object(plane.Plane, "get_issue") as single, \
                mock.patch.object(plane.Plane, "_request", autospec=True,
                                  side_effect=fake_plane_request) as request, \
                mock.patch.object(github, "_request") as gh, \
                mock.patch.object(github, "token") as token:
            dd_main.poll_refreshes(self.cfg, self.conn)
        single.assert_not_called(); gh.assert_not_called(); token.assert_not_called()
        self.assertEqual(request.call_count, 1)
        self.assertIn("/issues/?per_page=100", request.call_args.args[2])

    def test_done_stories_have_no_event_writes_across_poll_cycles(self):
        for number in range(1, 5):
            self.add_issue(f"i{number}", number + 6)
        self.cycle(); self.cycle(); self.cycle(); self.cycle()
        done_ids = ("i1", "i2", "i3")
        # Include a previously folded result to catch updates as well as inserts.
        runs.record_event(self.conn, None, "refresh_skipped",
                          "Refresh skipped (merged, closed or moved)",
                          project="dd", issue_id="i1")
        for issue_id in done_ids:
            board.issues[issue_id]["state"] = "done"
        before = {issue_id: [dict(e) for e in runs.events(self.conn, issue_id=issue_id)]
                  for issue_id in done_ids}
        with mock.patch.object(runs, "record_event", wraps=runs.record_event) as record:
            for _ in range(3):
                dd_main.poll_refreshes(self.cfg, self.conn)
        self.assertEqual([call.kwargs["issue_id"] for call in record.call_args_list],
                         ["i4"] * 3)
        for issue_id in done_ids:
            self.assertEqual([dict(e) for e in runs.events(self.conn, issue_id=issue_id)],
                             before[issue_id])
        unchanged = [e for e in runs.events(self.conn, issue_id="i4")
                     if e["type"] == "main_unchanged"]
        self.assertEqual(len(unchanged), 1)
        self.assertEqual(unchanged[0]["repeat_count"], 3)

    def test_review_pages_share_main_lookup_each_cycle(self):
        self.add_issue(); self.add_issue("i2", 8)
        self.cycle(); self.cycle()
        board.page_size = 1
        board.pages_served.clear()
        with mock.patch.object(github, "main_revision", wraps=github.main_revision) as main:
            dd_main.poll_refreshes(self.cfg, self.conn)
            self.assertEqual(main.call_count, 1)
            dd_main.poll_refreshes(self.cfg, self.conn)
            self.assertEqual(main.call_count, 2)
        self.assertEqual(len(board.pages_served), 4)
        self.assertEqual(len(self.run_rows()), 2)

    def test_repeated_poll_results_fold_even_across_full_cycles(self):
        self.add_issue(); self.cycle()
        for _ in range(3):
            self.cycle()
        unchanged = [e for e in runs.events(self.conn) if e["type"] == "main_unchanged"]
        self.assertEqual(len(unchanged), 1)
        self.assertEqual(unchanged[0]["repeat_count"], 3)
        self.assertIsNotNone(unchanged[0]["last_seen"])
        prs["dd-7"]["state"] = "closed"
        for _ in range(3):
            self.cycle()
        skipped = [e for e in runs.events(self.conn) if e["type"] == "refresh_skipped"]
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["repeat_count"], 3)
        prs["dd-7"]["state"] = "open"
        self.cycle()
        self.assertEqual(sum(e["type"] == "main_unchanged" for e in runs.events(self.conn)), 2)

    def test_refresh_combines_verifies_uploads_and_cleans_up(self):
        self.prepare()
        assignments = list(board.assignments)
        self.cfg["projects"]["dd"]["agent_label_id"] = "lbl-agent"
        labels = list(board.issues["i1"]["labels"])
        # Even a story with no Start Date on record (say, worked before DD set them) gets none from a refresh.
        with self.conn:
            self.conn.execute("DELETE FROM start_dates")
        start_dates = list(board.start_dates)
        with mock.patch.object(agents, "run") as agent:
            self.cycle()
        agent.assert_not_called()
        row = self.run_rows()[-1]
        self.assertEqual((row["kind"], row["status"]), ("refresh", runs.DONE))
        self.assertEqual(board.assignments, assignments)
        self.assertEqual(board.label_updates, [])
        self.assertEqual(board.issues["i1"]["labels"], labels)
        self.assertEqual(board.start_dates, start_dates)
        self.assertFalse(runs.start_date_set(self.conn, "i1"))
        self.assertNotEqual(git("rev-parse", "dd-7", cwd=self.origin), self.old_head)
        git("merge-base", "--is-ancestor", "main", "dd-7", cwd=self.origin)
        self.assertFalse(Path(row["worktree"]).exists())
        self.assertEqual(plane.state_id(board.issues["i1"]), S_REV)
        self.assertEqual(board.comments[-1][0], ORCH)
        self.assertIn("Main moved on (Add main behaviour).", board.comments[-1][2])
        self.assertIn("all tests pass. The pull request has been updated.", board.comments[-1][2])
        self.cycle()
        self.assertEqual(len(self.run_rows()), 2)
        self.assertEqual(len(board.comments), 2)

    def test_conflict_goes_back_to_builder(self):
        self.prepare(conflict=True)
        self.set_mode("repair")
        with mock.patch.object(agents, "run", wraps=agents.run) as agent:
            self.cycle()
        self.assertEqual(agent.call_count, 1)
        self.assertIs(agent.call_args.args[1], self.cfg["agents"]["claude-code"])
        self.assertIn("Clashing files: feature.py", agent.call_args.args[2])
        self.assertEqual(self.run_rows()[-1]["repair_attempts"], 1)
        self.assertEqual(self.run_rows()[-1]["status"], runs.DONE)
        self.assertIn("Sent back to Claude to resolve (try 1 of 2)", board.comments[-2][2])

    def test_two_failed_repairs_block_without_changing_pr(self):
        self.prepare()
        with mock.patch.object(verify, "run_verify", return_value=(False, "1 failed: test_both")):
            self.cycle()
        row = self.run_rows()[-1]
        self.assertEqual(row["repair_attempts"], 2)
        self.assertEqual(row["status"], runs.FAILED)
        self.assertEqual(plane.state_id(board.issues["i1"]), S_BLK)
        self.assertEqual(git("rev-parse", "dd-7", cwd=self.origin), self.old_head)
        self.assertIn(sync.REFRESH_UNCHANGED, board.comments[-1][2])
        self.assertIn("test_both", board.comments[-1][2])
        self.assertFalse((gitops.admin_dir(self.repo, Path(row["worktree"])) / "MERGE_HEAD").exists())

    def test_merged_closed_or_moved_during_refresh_uploads_nothing(self):
        self.prepare()
        for change in ("merged", "closed", "moved"):
            with self.subTest(change=change):
                prs["dd-7"].update(state="open", merged=False)
                board.issues["i1"]["state"] = S_REV
                original = verify.run_verify
                def change_then_verify(*args):
                    if change == "moved":
                        board.issues["i1"]["state"] = "done"
                    else:
                        prs["dd-7"]["state"] = "closed"
                        prs["dd-7"]["merged"] = change == "merged"
                    return original(*args)
                with mock.patch.object(verify, "run_verify", side_effect=change_then_verify), \
                        mock.patch.object(gitops, "push") as push:
                    self.cycle()
                push.assert_not_called()
                self.assertEqual(self.run_rows()[-1]["status"], runs.CANCELLED)
                self.assertEqual(git("rev-parse", "dd-7", cwd=self.origin), self.old_head)
                self.assertIn("refresh_skipped", [e["type"] for e in runs.events(self.conn)])

    def test_merged_pr_is_never_queued(self):
        self.prepare()
        prs["dd-7"].update(state="closed", merged=True)
        self.cycle()
        self.assertEqual(len(self.run_rows()), 1)

    def test_moved_story_is_never_queued(self):
        self.prepare()
        board.issues["i1"]["state"] = "done"
        self.cycle()
        self.assertEqual(len(self.run_rows()), 1)

    def test_cooling_builder_waits_even_with_another_available_agent(self):
        self.prepare()
        self.cfg["agents"]["other"] = dict(self.cfg["agents"]["claude-code"])
        self.cfg["projects"]["dd"]["agents"] = ["claude-code", "other"]
        choose.cool_down(self.cfg, self.conn, "claude-code")
        self.cycle()
        self.assertEqual(len(self.run_rows()), 1)
        self.assertEqual(plane.state_id(board.issues["i1"]), S_REV)
        runs.save_cooldown(self.conn, "claude-code", "2000-01-01T00:00:00+00:00")
        self.cycle()
        self.assertEqual(self.run_rows()[-1]["agent"], "claude-code")
        self.assertEqual(self.run_rows()[-1]["status"], runs.DONE)

    def test_queued_refresh_is_not_duplicated_and_waits_for_builder(self):
        self.prepare()
        dd_main.poll_refreshes(self.cfg, self.conn)
        dd_main.poll_refreshes(self.cfg, self.conn)
        self.assertEqual(len(self.run_rows()), 2)
        choose.cool_down(self.cfg, self.conn, "claude-code")
        dd_main.drain(self.cfg, self.conn)
        self.assertEqual(self.run_rows()[-1]["status"], runs.CANCELLED)
        self.assertEqual(plane.state_id(board.issues["i1"]), S_REV)

    def test_usage_limit_during_repair_waits(self):
        self.prepare(conflict=True)
        # Another agent is free, but a refresh waits for its builder rather than switching.
        self.cfg["agents"]["other"] = dict(self.cfg["agents"]["claude-code"])
        self.cfg["projects"]["dd"]["agents"] = ["claude-code", "other"]
        comments = len(board.comments)
        self.set_mode("limit")
        self.cycle()
        self.assertEqual(self.run_rows()[-1]["status"], runs.CANCELLED)
        self.assertEqual(plane.state_id(board.issues["i1"]), S_REV)
        self.assertEqual(git("rev-parse", "dd-7", cwd=self.origin), self.old_head)
        self.cycle()
        self.assertEqual(len(self.run_rows()), 2)
        self.assertEqual({r["agent"] for r in self.run_rows()}, {"claude-code"})
        self.assertIsNone(runs.wait_for(self.conn, "i1"))
        self.assertFalse(any("usage limit" in c[2] for c in board.comments[comments:]))

    def test_closed_before_worker_starts_is_skipped(self):
        self.prepare()
        dd_main.poll_refreshes(self.cfg, self.conn)
        prs["dd-7"]["state"] = "closed"
        with mock.patch.object(gitops, "ensure_worktree") as setup:
            dd_main.drain(self.cfg, self.conn)
        setup.assert_not_called()
        self.assertEqual(self.run_rows()[-1]["status"], runs.CANCELLED)

    def test_saved_but_unuploaded_merge_is_not_mistaken_for_current_pr(self):
        self.prepare()
        original = verify.run_verify
        def move_then_verify(*args):
            board.issues["i1"]["state"] = "done"
            return original(*args)
        with mock.patch.object(verify, "run_verify", side_effect=move_then_verify):
            self.cycle()
        self.assertEqual(self.run_rows()[-1]["status"], runs.CANCELLED)
        target = git("rev-parse", "main", cwd=self.origin).strip()
        self.assertFalse(gitops.branch_contains_main(self.repo, "dd-7", target))
        board.issues["i1"]["state"] = S_REV
        self.cycle()
        self.assertEqual(self.run_rows()[-1]["status"], runs.DONE)
        self.assertTrue(gitops.branch_contains_main(self.repo, "dd-7", target))

    def test_refresh_comment_retries_once_as_orchestrator(self):
        self.prepare()
        board.lose_post_reply = 1
        self.cycle()
        self.assertEqual(self.run_rows()[-1]["status"], runs.AWAITING_REVIEW)
        self.assertEqual(len(board.comments), 2)
        with mock.patch.object(gitops, "push") as push:
            self.cycle()
        push.assert_not_called()
        self.assertEqual(self.run_rows()[-1]["status"], runs.DONE)
        self.assertEqual(len(board.comments), 2)
        self.assertEqual(board.comments[-1][0], ORCH)

    def test_refresh_aborts_leftover_merge_before_recreating_worktree(self):
        self.prepare(conflict=True)
        tree, _ = gitops.ensure_worktree(self.repo, self.wt_root, "dd-7", "main")
        gitops.combine_main(self.repo, tree, "dd-7", "main", self.cfg["agents"]["claude-code"]["git_author"])
        (tree / "unfinished.py").write_text("unwanted repair")
        self.set_mode("repair")
        self.cycle()
        self.assertEqual(self.run_rows()[-1]["status"], runs.DONE)
        self.assertNotIn("unfinished.py", git("ls-tree", "--name-only", "dd-7", cwd=self.origin))
        self.assertIn("merge_aborted", [e["type"] for e in runs.events(self.conn)])

    def test_unknown_new_main_check_is_read_only(self):
        self.prepare()
        target = git("rev-parse", "main", cwd=self.origin).strip()
        with mock.patch.object(gitops, "_git", wraps=gitops._git) as command:
            self.assertFalse(gitops.branch_contains_main(self.repo, "dd-7", target))
        self.assertEqual([c.args[0][0] for c in command.call_args_list], ["cat-file"])


"""Tests for board updates."""

import copy
import threading
from concurrent.futures import ThreadPoolExecutor
import html, shutil
from unittest import mock
from dispatch import gitops, plane, runs, sync, text, worker
import main as dd_main
import helpers
from helpers import (
    Base,
    CLAUDE,
    CODEX,
    ORCH,
    PID,
    S_BACK,
    S_BLK,
    S_IP,
    S_REV,
    board,
    git,
    prs,
    sandbox_calls,
)


class AgentLabel(Base):
    def test_adds_label_preserving_existing_on_build_and_rework(self):
        self.cfg["projects"]["dd"]["agent_label_id"] = "lbl-agent"
        self.add_issue(labels=[{"id": "lbl-existing"}, "lbl-other"])
        self.cycle()
        self.assertEqual(board.label_updates,
                         [(ORCH, "i1", ["lbl-existing", "lbl-other", "lbl-agent"])])
        board.issues["i1"]["state"] = {"id": S_IP}
        # A human removed the label before rework; each assignment adds it again.
        board.issues["i1"]["labels"] = ["lbl-new"]
        self.cycle()
        self.assertEqual(board.label_updates[-1], (ORCH, "i1", ["lbl-new", "lbl-agent"]))
        self.assertEqual(len(board.label_updates), 2)
        self.assertTrue(all(r["status"] == runs.DONE for r in self.run_rows()))

    def test_existing_agent_label_is_not_written(self):
        self.cfg["projects"]["dd"]["agent_label_id"] = "lbl-agent"
        labels = [{"id": "lbl-agent"}, {"id": "lbl-existing"}]
        self.add_issue(labels=labels)
        self.cycle()
        self.assertEqual(board.label_updates, [])
        self.assertEqual(board.issues["i1"]["labels"], labels)

    def test_no_setting_leaves_labels_unchanged(self):
        self.add_issue(labels=["lbl-existing"])
        self.cycle()
        self.assertEqual(board.label_updates, [])
        self.assertEqual(board.issues["i1"]["labels"], ["lbl-existing"])

    def test_slow_label_write_does_not_delay_story(self):
        self.cfg["projects"]["dd"]["agent_label_id"] = "lbl-agent"
        self.add_issue()
        entered, release = threading.Event(), threading.Event()
        original = plane.Plane.patch

        def stalled(client, path, payload):
            if "labels" in payload:
                entered.set()
                release.wait(10)
                raise TimeoutError("unreachable")
            return original(client, path, payload)

        with mock.patch.object(plane.Plane, "patch", stalled):
            try:
                dd_main.cycle(self.cfg, self.conn)
                self.assertTrue(entered.wait(2))
                self.assertEqual(self.run_rows()[0]["status"], runs.DONE)
                self.assertEqual(board.label_updates, [])
            finally:
                release.set()
                self.wait_assignments()
        self.assertTrue(any(e["type"] == "warning" and "Agent label" in e["detail"]
                            for e in runs.events(self.conn)))

    def test_label_failure_warns_and_story_finishes(self):
        self.cfg["projects"]["dd"]["agent_label_id"] = "unknown-label"
        self.add_issue(labels=["lbl-existing"])
        board.label_error = plane.PlaneError("Unknown label ID", status=400)
        self.cycle()
        self.assertEqual(self.run_rows()[0]["status"], runs.DONE)
        self.assertEqual(board.issues["i1"]["labels"], ["lbl-existing"])
        self.assertEqual(board.issues["i1"]["assignees"], ["user-claude"])
        self.assertEqual(len(board.start_dates), 1)
        self.assertTrue(any(e["type"] == "warning" and "Agent label" in e["detail"]
                            and "Unknown label ID" in e["detail"] for e in runs.events(self.conn)))



class Assignment(Base):
    def test_identity_is_cached_across_stories_and_client_setup(self):
        original_cfg = copy.deepcopy(self.cfg)
        self.add_issue()
        board.issues["i1"]["assignees"] = ["someone-else", "another-user"]
        self.cycle()
        self.add_issue("i2", seq=8)
        self.cycle()
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(plane.orchestrator, [self.cfg] * 8))
        self.wait_assignments()
        self.assertEqual(self.cfg, original_cfg)
        self.assertEqual(board.user_lookups, [CLAUDE])
        self.assertEqual(board.assignments, [(ORCH, "i1", ["user-claude"]),
                                             (ORCH, "i2", ["user-claude"])])

    def test_slow_old_assignment_cannot_overtake_handoff(self):
        self.cfg["agents"]["codex"] = {"plane_token": CODEX}
        self.add_issue()
        entered, release = threading.Event(), threading.Event()
        original = plane.Plane.set_assignee

        def stalled(client, project_id, issue_id, user_id):
            if user_id == "user-claude":
                entered.set()
                release.wait(10)
            return original(client, project_id, issue_id, user_id)

        orch = plane.orchestrator(self.cfg)
        with mock.patch.object(plane.Plane, "set_assignee", stalled):
            try:
                plane.assign_story(self.cfg, orch, PID, "i1", "claude-code", None)
                self.assertTrue(entered.wait(2))
                plane.assign_story(self.cfg, orch, PID, "i1", "codex", None)
                self.assertEqual(board.assignments, [])
            finally:
                release.set()
                self.wait_assignments()
        self.assertEqual(board.assignments, [(ORCH, "i1", ["user-claude"]),
                                             (ORCH, "i1", ["user-codex"])])
        self.assertEqual(board.issues["i1"]["assignees"], ["user-codex"])

    def test_slow_assignment_does_not_delay_story(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()

        def stalled(*args):
            entered.set()
            release.wait(10)
            finished.set()
            raise TimeoutError("unreachable")

        self.add_issue()
        with mock.patch.object(plane.Plane, "set_assignee", side_effect=stalled):
            try:
                dd_main.cycle(self.cfg, self.conn)
                self.assertTrue(entered.wait(2))
                self.assertFalse(finished.is_set(), "Story waited for the assignment request")
                self.assertEqual(self.run_rows()[0]["status"], runs.DONE)
            finally:
                release.set()
                self.wait_assignments()
        self.assertTrue(any(e["type"] == "warning" and "Could not assign" in e["detail"]
                            for e in runs.events(self.conn)))

    def test_slow_lookup_does_not_delay_story_and_is_retried(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()

        def stalled(*args):
            entered.set()
            release.wait(10)
            finished.set()
            raise TimeoutError("unreachable")

        self.add_issue()
        with mock.patch.object(plane.Plane, "current_user", side_effect=stalled) as lookup:
            try:
                dd_main.cycle(self.cfg, self.conn)
                self.assertTrue(entered.wait(2))
                self.assertFalse(finished.is_set(), "Story waited for identity discovery")
                self.assertEqual(self.run_rows()[0]["status"], runs.DONE)
            finally:
                release.set()
                self.wait_assignments()
            self.add_issue("i2", seq=8)
            self.cycle()
            self.assertEqual(lookup.call_count, 3)
        self.assertEqual(board.assignments, [])
        self.assertEqual(len([e for e in runs.events(self.conn)
                              if e["type"] == "warning" and "look up Plane user" in e["detail"]]), 2)

    def test_assignment_failure_does_not_stop_story(self):
        for error in (plane.PlaneError("refused", status=403), TimeoutError("unreachable"),
                      ValueError("invalid response")):
            with self.subTest(error=error):
                board.assignment_error = error
                iid = str(error)
                self.add_issue(iid, seq=10 + len(self.run_rows()))
                board.issues[iid]["assignees"] = ["previous-user"]
                self.cycle()
                row = self.run_rows()[-1]
                self.assertEqual(row["status"], runs.DONE)
                self.assertEqual(board.issues[iid]["assignees"], ["previous-user"])
                self.assertTrue(any(e["type"] == "warning" and "Could not assign" in e["detail"]
                                    for e in runs.events(self.conn, run_id=row["id"])))

    def test_failed_startup_lookup_is_retried_for_next_assignment(self):
        for error in (plane.PlaneError("down", status=503), TimeoutError("unreachable")):
            with self.subTest(error=error):
                if plane._assignments:
                    plane._assignments.executor.shutdown()
                    plane._assignments = None
                board.user_lookups.clear()
                board.user_error = error
                orch = plane.orchestrator(self.cfg)
                self.wait_assignments()
                self.assertEqual(board.user_lookups, [CLAUDE])
                board.user_error = None
                self.add_issue()
                plane.assign_story(self.cfg, orch, PID, "i1", "claude-code", None)
                self.wait_assignments()
                self.assertEqual(board.user_lookups, [CLAUDE, CLAUDE])
                self.assertEqual(board.issues["i1"]["assignees"], ["user-claude"])
                self.add_issue("i2", seq=8)
                plane.assign_story(self.cfg, orch, PID, "i2", "claude-code", None)
                self.wait_assignments()
                self.assertEqual(board.user_lookups, [CLAUDE, CLAUDE])

    def test_missing_identity_endpoint_warns_once_and_skips_assignment(self):
        board.user_error = plane.PlaneError("not found", status=404)
        with mock.patch.object(plane, "warn") as warning:
            self.add_issue()
            self.cycle()
            self.add_issue("i2", seq=8)
            self.cycle()
        self.assertEqual(board.user_lookups, [CLAUDE])
        warning.assert_called_once()
        self.assertEqual(board.assignments, [])
        self.assertEqual([r["status"] for r in self.run_rows()], [runs.DONE, runs.DONE])
        self.assertEqual(len([e for e in runs.events(self.conn)
                              if e["type"] == "warning" and "look up Plane user" in e["detail"]]), 1)

    def test_moved_story_is_not_assigned(self):
        self.add_issue()
        dd_main.poll_once(self.cfg, self.conn)
        board.issues["i1"]["state"] = S_BACK
        worker.execute(self.cfg, self.conn, runs.claim_next(self.conn))
        self.assertEqual(board.assignments, [])



class StartDate(Base):
    TODAY = "2026-03-14"

    def setUp(self):
        super().setUp()
        # The worker's local date, fixed so a run near midnight can't flake.
        today = mock.patch.object(worker, "date", mock.Mock(today=mock.Mock(
            return_value=mock.Mock(isoformat=mock.Mock(return_value=self.TODAY)))))
        today.start()
        self.addCleanup(today.stop)

    def rework(self):
        board.issues["i1"]["state"] = {"id": S_IP}
        self.cycle()

    def test_first_run_sets_start_date_and_later_runs_leave_it(self):
        self.add_issue()
        self.cycle()
        self.assertEqual(board.start_dates, [(ORCH, "i1", self.TODAY)])
        self.assertEqual(board.issues["i1"]["start_date"], self.TODAY)
        self.assertTrue(runs.start_date_set(self.conn, "i1"))
        board.issues["i1"]["start_date"] = "2026-04-01"  # a human changes it afterwards
        self.rework()
        self.assertEqual([r["status"] for r in self.run_rows()], [runs.DONE, runs.DONE])
        self.assertEqual(board.start_dates, [(ORCH, "i1", self.TODAY)])
        self.assertEqual(board.issues["i1"]["start_date"], "2026-04-01")
        self.assertEqual(len(board.assignments), 2)

    def test_first_run_overwrites_hand_set_date(self):
        self.add_issue()
        board.issues["i1"]["start_date"] = "2025-01-01"
        self.cycle()
        self.assertEqual(board.issues["i1"]["start_date"], self.TODAY)

    def test_refusal_is_a_warning_and_next_run_tries_again(self):
        for error in (plane.PlaneError("Start date cannot exceed target date", status=400),
                      TimeoutError("unreachable")):
            with self.subTest(error=error):
                board.start_date_error = error
                iid = str(error)
                self.add_issue(iid, seq=10 + len(self.run_rows()))
                self.cycle()
                row = self.run_rows()[-1]
                self.assertEqual(row["status"], runs.DONE)
                self.assertNotIn("start_date", board.issues[iid])
                self.assertEqual(board.issues[iid]["assignees"], ["user-claude"])
                self.assertTrue(any(e["type"] == "warning" and "Start Date" in e["detail"]
                                    for e in runs.events(self.conn, run_id=row["id"])))
                self.assertFalse(runs.start_date_set(self.conn, iid))
                board.start_date_error = None
                board.issues[iid]["state"] = {"id": S_IP}
                self.cycle()
                self.assertEqual(board.issues[iid]["start_date"], self.TODAY)
                self.assertTrue(runs.start_date_set(self.conn, iid))

    def test_write_already_queued_by_an_earlier_run_is_not_repeated(self):
        self.add_issue()
        orch = plane.orchestrator(self.cfg)
        plane.assign_story(self.cfg, orch, PID, "i1", "claude-code", None, "2026-03-13")
        plane.assign_story(self.cfg, orch, PID, "i1", "claude-code", None, "2026-03-14")
        self.wait_assignments()
        self.assertEqual(board.start_dates, [(ORCH, "i1", "2026-03-13")])

    def test_moved_story_gets_no_start_date(self):
        self.add_issue()
        dd_main.poll_once(self.cfg, self.conn)
        board.issues["i1"]["state"] = S_BACK
        worker.execute(self.cfg, self.conn, runs.claim_next(self.conn))
        self.wait_assignments()
        self.assertEqual(board.start_dates, [])
        self.assertFalse(runs.start_date_set(self.conn, "i1"))



class HumanOnly(Base):
    def poll(self):
        with mock.patch.object(dd_main, "log") as log:
            dd_main.poll_once(self.cfg, self.conn)
        return [c.args[0] for c in log.call_args_list]

    def test_labelled_story_is_skipped_and_its_neighbour_queued(self):
        self.cfg["projects"]["dd"]["human_only_label_id"] = "lbl-human"
        self.add_issue("i1", seq=7, labels=["lbl-other", "lbl-human"])
        self.add_issue("i2", seq=8, labels=["lbl-other"])
        lines = self.poll()
        self.assertEqual([r["issue_id"] for r in self.run_rows()], ["i2"])
        self.assertEqual([l for l in lines if "skipping" in l],
                         ["[dd] skipping 7, labelled human-only: Add a value"])

    def test_cycle_plan_item_in_progress_is_never_built(self):
        self.add_issue("i1", seq=7)
        self.add_issue("i2", seq=8)
        runs.save_cycle_plan(self.conn, "dd", "cycle", "i1")
        self.poll()
        self.assertEqual([r["issue_id"] for r in self.run_rows()], ["i2"])

    def test_skip_is_logged_once_not_every_poll(self):
        self.cfg["projects"]["dd"]["human_only_label_id"] = "lbl-human"
        self.add_issue("i1", labels=["lbl-human"])
        self.assertEqual(len(self.poll()), 1)
        self.assertEqual(self.poll(), [])
        self.assertEqual(self.run_rows(), [])

    def test_nested_label_objects_also_work(self):
        self.cfg["projects"]["dd"]["human_only_label_id"] = "lbl-human"
        self.add_issue("i1", labels=[{"id": "lbl-human", "name": "Human"}])
        self.poll()
        self.assertEqual(self.run_rows(), [])

    def test_without_the_config_value_nothing_is_skipped(self):
        self.add_issue("i1", seq=7, labels=["lbl-human"])
        self.add_issue("i2", seq=8)
        self.poll()
        self.assertEqual([r["issue_id"] for r in self.run_rows()], ["i1"])
        self.assertEqual([w["issue_id"] for w in runs.waits(self.conn, "dd")], ["i2"])



class Pagination(Base):
    def test_in_progress_stories_on_every_page_are_queued(self):
        # Pages of two: In Progress stories land on pages one, two and three.
        layout = [S_IP, S_BACK, S_REV, S_IP, S_BLK, S_IP, S_BACK]
        for n, state in enumerate(layout):
            self.add_issue(f"i{n}", seq=n + 1, state=state)
        board.page_size = 2
        dd_main.poll_once(self.cfg, self.conn)
        queued = sorted(r["issue_id"] for r in self.run_rows() if r["status"] == runs.QUEUED)
        self.assertEqual(queued, ["i0"])
        self.assertEqual([w["issue_id"] for w in runs.waits(self.conn, "dd")], ["i3", "i5"])
        cursors = [c for p, c in board.pages_served if p.endswith("/issues/")]
        self.assertEqual(cursors, [None, "2:1:0", "2:2:0", "2:3:0"])

    def test_only_in_progress_is_queued_when_plane_returns_every_story(self):
        # The fake Plane, like 1.4.2, ignores any state filter and hands back the whole board.
        for n, state in enumerate([S_BACK, S_IP, S_REV, S_BLK, S_IP]):
            self.add_issue(f"i{n}", seq=n + 1, state=state, nested=n % 2 == 0)
        dd_main.poll_once(self.cfg, self.conn)
        queued = sorted(r["issue_id"] for r in self.run_rows() if r["status"] == runs.QUEUED)
        self.assertEqual(queued, ["i1"])
        self.assertEqual([w["issue_id"] for w in runs.waits(self.conn, "dd")], ["i4"])

    def test_listing_stories_does_not_send_a_filter_plane_rejects(self):
        self.add_issue()
        self.assertEqual([i["id"] for i in plane.orchestrator(self.cfg).issues_in_state(PID, S_IP)], ["i1"])

    def test_marker_on_a_later_comment_page_is_not_posted_twice(self):
        self.add_issue(); board.lose_post_reply = 1
        self.cycle()
        self.assertEqual(self.run_rows()[0]["status"], runs.AWAITING_REVIEW)
        # Five other comments come first, so with pages of two ours is on page three.
        others = [(ORCH, "i1", f"<pre>note {n}</pre>") for n in range(5)]
        board.comments[:0] = others
        board.page_size = 2; board.pages_served.clear()
        self.cycle()
        [row] = self.run_rows()
        self.assertEqual(row["status"], runs.DONE)
        self.assertEqual(len(board.comments), 6)
        self.assertEqual([c for c in board.comments if c not in others][0][0], CLAUDE)
        cursors = [c for p, c in board.pages_served if p.endswith("/i1/comments/")]
        self.assertEqual(cursors, [None, "2:1:0", "2:2:0"])



class Reconciliation(Base):
    def test_failed_board_update_blocks_redispatch_and_retries(self):
        self.add_issue()
        board.fail_patch = 1
        self.cycle()
        [row] = self.run_rows()
        self.assertEqual(row["status"], runs.AWAITING_REVIEW)
        self.assertEqual(row["comment_posted"], 1)
        # story still In Progress on the board - it must NOT be dispatched again
        dd_main.poll_once(self.cfg, self.conn)
        self.assertEqual(len(self.run_rows()), 1)
        self.cycle()  # sync retries the board update only
        rows = self.run_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], runs.DONE)
        self.assertEqual(len(board.comments), 1)  # comment not duplicated
        self.assertEqual(len([c for c in sandbox_calls if c["env_file"]]), 1)  # agent ran once

    def test_unexpected_exception_goes_to_blocked(self):
        self.add_issue()
        original = gitops.has_changes
        gitops.has_changes = lambda *a: 1 / 0
        try:
            self.cycle()
        finally:
            gitops.has_changes = original
        [row] = self.run_rows()
        self.assertEqual(row["status"], runs.FAILED)
        self.assertIn("ZeroDivisionError", row["error"])
        self.assertEqual(board.issues["i1"]["state"], {"id": S_BLK})

    def test_story_deleted_while_awaiting(self):
        self.add_issue(); board.fail_patch = 1
        self.cycle()
        del board.issues["i1"]
        self.cycle()
        self.assertEqual(self.run_rows()[0]["status"], runs.DONE)



class SecondReview(Base):
    def test_plane_unreachable_before_push_blocks_and_keeps_work(self):
        self.add_issue()
        board.get_states = [S_IP, "FAIL"]  # start check passes, pre-push check can't reach Plane
        self.cycle()
        [row] = self.run_rows()
        self.assertEqual(row["status"], runs.FAILED)
        self.assertIn("could not be reached", row["error"])
        self.assertEqual(prs, {})
        self.assertNotIn("dd-7", git("branch", "--list", cwd=self.origin))
        self.assertIn("VALUE = 1", git("show", "dd-7:feature.py", cwd=self.repo))  # committed locally
        self.assertEqual(board.issues["i1"]["state"], {"id": S_BLK})


    def test_state_is_read_after_the_comment_right_before_the_write(self):
        self.add_issue()
        def human_moves_it(): board.issues["i1"]["state"] = {"id": S_BACK}
        board.after_post = human_moves_it
        self.cycle()
        [row] = self.run_rows()
        self.assertEqual(row["status"], runs.DONE)
        self.assertEqual(board.issues["i1"]["state"], {"id": S_BACK})
        self.assertEqual(board.transitions, [])
        self.assertIn("left alone", row["note"])


    def test_lost_comment_reply_is_not_posted_twice(self):
        self.add_issue(); board.lose_post_reply = 1
        self.cycle()
        self.assertEqual(self.run_rows()[0]["status"], runs.AWAITING_REVIEW)
        self.cycle()
        [row] = self.run_rows()
        self.assertEqual(row["status"], runs.DONE)
        self.assertEqual(len(board.comments), 1)
        self.assertIn(f"[Dynamic Dispatch run {row['id']}]", board.comments[0][2])


    def test_marker_does_not_match_a_longer_run_id(self):
        board.issues["i1"] = {"id": "i1"}
        board.comments.append((CLAUDE, "i1", "<pre>done\n\n[Dynamic Dispatch run 12]</pre>"))
        orch = plane.orchestrator(self.cfg)
        self.assertTrue(orch.has_comment_with(PID, "i1", sync.marker({"id": 12})))
        self.assertFalse(orch.has_comment_with(PID, "i1", sync.marker({"id": 1})))


    def test_comment_listing_unsupported_still_posts(self):
        for status in (404, 405):
            with self.subTest(status=status):
                board.reset(); self.conn.execute("DELETE FROM runs"); self.conn.commit()
                shutil.rmtree(self.wt_root); self.wt_root.mkdir()
                git("worktree", "prune", cwd=self.repo)
                self.add_issue(); board.list_status = status
                self.cycle()
                self.assertEqual(self.run_rows()[0]["status"], runs.DONE)
                self.assertEqual(len(board.comments), 1)


    def test_listing_outage_retries_instead_of_posting_blind(self):
        self.add_issue(); board.list_status = 503
        self.cycle()
        self.assertEqual(self.run_rows()[0]["status"], runs.AWAITING_REVIEW)
        self.assertEqual(board.comments, [])
        board.list_status = None
        self.cycle()
        self.assertEqual(self.run_rows()[0]["status"], runs.DONE)
        self.assertEqual(len(board.comments), 1)


    def test_pr_link_and_marker_survive_truncation(self):
        self.set_mode("long"); self.add_issue(); self.cycle()
        [row] = self.run_rows()
        text = board.comments[0][2]
        self.assertTrue(text.startswith("<pre>Pull request: https://gh/pr/1\n"), text[:80])
        self.assertIn("[... truncated ...]", text)
        self.assertTrue(text.endswith(f"[Dynamic Dispatch run {row['id']}]</pre>"))


    def github_pr(self):
        return mock.patch.object(helpers, "PR_URL", "https://github.com/o/r/pull/{}")


    def test_in_review_comment_opens_with_pr_link(self):
        with self.github_pr():
            self.add_issue(); self.cycle()
        [row] = self.run_rows()
        text = board.comments[0][2]
        self.assertTrue(text.startswith('<p><a href="https://github.com/o/r/pull/1/files">Pull request #1</a></p><pre>'), text[:120])
        self.assertNotIn("Pull request:", text)
        self.assertTrue(text.endswith(f"\n\n[Dynamic Dispatch run {row['id']}]</pre>"))
        self.assertEqual(text.split("<pre>", 1)[1][:-len("</pre>")],
                         html.escape(f"{row['summary']}\n\n{sync.marker(row)}"))


    def test_long_summary_is_truncated_after_the_link(self):
        self.set_mode("long")
        with self.github_pr():
            self.add_issue(); self.cycle()
        [row] = self.run_rows()
        text = board.comments[0][2]
        self.assertTrue(text.startswith('<p><a href="https://github.com/o/r/pull/1/files">Pull request #1</a></p><pre>'))
        self.assertIn("[... truncated ...]", text)
        self.assertTrue(text.endswith(f"[Dynamic Dispatch run {row['id']}]</pre>"))
        self.assertEqual(len(text.split("<pre>", 1)[1]),
                         len(html.escape(row["summary"][:plane.COMMENT_LIMIT])) + len(html.escape(f"\n\n[... truncated ...]\n\n{sync.marker(row)}")) + len("</pre>"))


    def test_lost_comment_reply_with_pr_link_is_not_posted_twice(self):
        with self.github_pr():
            self.add_issue(); board.lose_post_reply = 1
            self.cycle(); self.cycle()
        self.assertEqual(self.run_rows()[0]["status"], runs.DONE)
        self.assertEqual(len(board.comments), 1)
        self.assertIn("Pull request #1</a>", board.comments[0][2])


    def test_only_a_github_pr_address_becomes_a_link(self):
        board.issues["i1"] = {"id": "i1"}
        agent = plane.as_agent(self.cfg, self.cfg["agents"]["claude-code"])
        for url in ("https://gh/pr/1", "http://github.com/o/r/pull/1", "https://github.com/o/r/pull/1x",
                    "https://github.com/o/r/issues/1", "https://github.com.evil/o/r/pull/1",
                    'https://github.com/o/r"><script>/pull/1', "https://github.com/o/r/pull/1\nhi"):
            with self.subTest(url=url):
                board.comments.clear()
                agent.comment(PID, "i1", "done", marker="[m]", pr_url=url)
                self.assertEqual(board.comments[0][2], f"<pre>{html.escape(f'Pull request: {url}')}\n\ndone\n\n[m]</pre>")


    def test_summary_markup_and_urls_stay_plain_text(self):
        board.issues["i1"] = {"id": "i1"}
        agent = plane.as_agent(self.cfg, self.cfg["agents"]["claude-code"])
        summary = '<a href="https://evil">x</a> see https://github.com/o/r/pull/9'
        agent.comment(PID, "i1", summary, pr_url="https://github.com/o/r/pull/3")
        self.assertEqual(board.comments[0][2],
                         '<p><a href="https://github.com/o/r/pull/3/files">Pull request #3</a></p>'
                         f"<pre>{html.escape(summary)}</pre>")
        self.assertEqual(board.comments[0][2].count("<a "), 1)


    def test_refresh_and_blocked_comments_have_no_link(self):
        with self.github_pr():
            self.add_issue(state=S_REV)
            refresh = runs.enqueue(self.conn, project="dd", issue=board.issues["i1"], agent="claude-code",
                                   branch="dd-7", kind="refresh", pr_url="https://github.com/o/r/pull/1")
            runs.await_review(self.conn, refresh, summary="Brought up to date.", usage=None, cost_usd=None,
                              pr_url="https://github.com/o/r/pull/1", worktree=None)
            sync.reconcile(self.cfg, self.conn, runs.get(self.conn, refresh))
            self.add_issue("i2", 8)
            blocked = runs.enqueue(self.conn, project="dd", issue=board.issues["i2"], agent="claude-code", branch="dd-8")
            runs.await_blocked(self.conn, blocked, "https://github.com/o/r/pull/2 broke")
            sync.reconcile(self.cfg, self.conn, runs.get(self.conn, blocked))
        self.assertEqual([c[2] for c in board.comments], [
            f"<pre>Brought up to date.\n\n{sync.marker({'id': refresh})}</pre>",
            "<pre>Dynamic Dispatch could not complete this story.\n\n"
            f"https://github.com/o/r/pull/2 broke\n\n{sync.marker({'id': blocked})}</pre>",
        ])



class ThirdReview(Base):
    def test_agent_404_does_not_close_a_visible_story(self):
        self.add_issue(); board.agent_404 = True
        self.cycle()
        [row] = self.run_rows()
        self.assertEqual(row["status"], runs.AWAITING_REVIEW)  # still holds the story
        dd_main.poll_once(self.cfg, self.conn)
        self.assertEqual(len(self.run_rows()), 1)  # so it is not dispatched again
        board.agent_404 = False
        self.cycle()
        [row] = self.run_rows()
        self.assertEqual(row["status"], runs.DONE)
        self.assertEqual(board.issues["i1"]["state"], {"id": S_REV})
        self.assertEqual(len([c for c in sandbox_calls if c["env_file"]]), 1)  # agent ran once


    def test_agent_404_on_state_change_does_not_close(self):
        self.add_issue(); board.fail_patch = 1  # comment goes up, state change retried
        self.cycle()
        board.agent_404 = True
        self.cycle()
        self.assertEqual(self.run_rows()[0]["status"], runs.AWAITING_REVIEW)
        board.agent_404 = False
        self.cycle()
        self.assertEqual(self.run_rows()[0]["status"], runs.DONE)
        self.assertEqual(len(board.comments), 1)


    def test_blocked_recovery_instruction_says_to_move_it_back(self):
        self.add_issue()
        board.get_states = [S_IP, "FAIL"]
        self.cycle()
        [row] = self.run_rows()
        self.assertIn("move the story back to In Progress", row["error"])
        self.assertNotIn("git -C", row["error"])


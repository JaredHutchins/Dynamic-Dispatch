"""Tests for lights approvals."""

from unittest import mock
from dispatch import choose, github, lights, plane, runs, verify
import main as dd_main
from helpers import (
    Base,
    GH,
    MainChanges,
    S_BACK,
    S_BLK,
    S_IP,
    S_REV,
    board,
    fake_plane_request,
    gh_tokens,
    git,
    pr_comments,
    prs,
    review_failures,
    review_reads,
    reviews,
    status_failures,
    statuses,
)


class PullRequestLights(Base):
    advance_main = MainChanges.advance_main
    GREEN = ("success", "Up to date and tested")
    YELLOW = ("pending", "Updating with latest main")
    RED = ("failure", "Blocked: see the story in Plane")

    def head(self):
        return git("rev-parse", "dd-7", cwd=self.origin).strip()

    def open_pr(self):
        self.add_issue()
        self.cycle()
        self.assertEqual(plane.state_id(board.issues["i1"]), S_REV)

    def test_new_pull_request_gets_green(self):
        self.open_pr()
        self.assertEqual(statuses, [(self.head(), *self.GREEN)])
        # Unchanged on later checks: GitHub isn't asked again.
        self.cycle(); self.cycle()
        self.assertEqual(len(statuses), 1)

    def test_main_moving_turns_yellow_before_refresh_then_new_commit_green(self):
        self.open_pr()
        old = self.head()
        self.advance_main()
        dd_main.poll_refreshes(self.cfg, self.conn)
        # Queued, not started: the light is already yellow.
        self.assertEqual(self.run_rows()[-1]["status"], runs.QUEUED)
        self.assertEqual(statuses[-1], (old, *self.YELLOW))
        dd_main.drain(self.cfg, self.conn)
        self.assertEqual(self.run_rows()[-1]["status"], runs.DONE)
        self.assertNotEqual(self.head(), old)
        self.assertEqual(statuses[-1], (self.head(), *self.GREEN))
        self.assertEqual(len(statuses), 3)

    def test_yellow_is_set_once_while_the_builder_is_busy(self):
        self.open_pr()
        self.advance_main()
        choose.cool_down(self.cfg, self.conn, "claude-code")
        for _ in range(3):
            self.cycle()
        self.assertEqual(len(self.run_rows()), 1)
        self.assertEqual(statuses[1:], [(self.head(), *self.YELLOW)])

    def test_failed_refresh_turns_red(self):
        self.open_pr()
        old = self.head()
        self.advance_main()
        with mock.patch.object(verify, "run_verify", return_value=(False, "1 failed: test_both")):
            self.cycle()
        self.assertEqual(plane.state_id(board.issues["i1"]), S_BLK)
        self.assertEqual(self.head(), old)
        self.assertEqual(statuses[1:], [(old, *self.YELLOW), (old, *self.RED)])

    def test_blocked_rework_turns_red(self):
        self.open_pr()
        board.issues["i1"]["state"] = {"id": S_IP}
        with mock.patch.object(verify, "run_verify", return_value=(False, "1 failed")):
            self.cycle()
        self.assertEqual(plane.state_id(board.issues["i1"]), S_BLK)
        self.assertEqual(statuses[-1], (self.head(), *self.RED))

    def test_story_without_pull_request_sets_no_light(self):
        self.add_issue()
        with mock.patch.object(verify, "run_verify", return_value=(False, "1 failed")):
            self.cycle()
        self.assertEqual(plane.state_id(board.issues["i1"]), S_BLK)
        self.cycle()
        self.assertEqual(statuses, [])

    def test_github_error_records_warning_and_story_carries_on(self):
        status_failures.append("POST statuses -> 403: Resource not accessible by integration")
        self.open_pr()
        self.assertEqual(self.run_rows()[0]["status"], runs.DONE)
        self.assertEqual(statuses, [])
        warnings = [e["detail"] for e in runs.events(self.conn) if e["type"] == "warning"]
        self.assertTrue(any("Could not set the Dynamic Dispatch light" in w and "403" in w
                            for w in warnings), warnings)
        self.cycle()
        self.assertEqual(statuses, [(self.head(), *self.GREEN)])
        self.cycle()
        self.assertEqual(len(statuses), 1)

    def test_unreachable_github_never_fails_a_refresh(self):
        self.open_pr()
        self.advance_main()
        with mock.patch.object(github, "set_status", side_effect=OSError("network down")):
            self.cycle()
        self.assertEqual(self.run_rows()[-1]["status"], runs.DONE)
        self.assertEqual(plane.state_id(board.issues["i1"]), S_REV)
        self.cycle()
        self.assertEqual(statuses[-1], (self.head(), *self.GREEN))

    def test_merged_pull_request_is_left_alone(self):
        status_failures.append("unreachable")
        self.open_pr()
        prs["dd-7"].update(state="closed", merged=True)
        board.issues["i1"]["state"] = "done"
        self.cycle()
        self.assertEqual(statuses, [])
        self.assertEqual(lights.light_state(self.conn, "dd", "i1"), "success")
        # Settled: later checks don't ask GitHub about it again.
        calls = len(gh_tokens)
        self.cycle()
        self.assertEqual(len(gh_tokens), calls)

    def forget_lights(self):
        """As though DD started setting lights only now."""
        with self.conn:
            self.conn.execute("DELETE FROM pr_lights")
        statuses.clear()

    def test_pull_request_in_review_before_deploy_gets_green_on_next_check(self):
        self.open_pr()
        self.forget_lights()
        self.cycle()
        self.assertEqual(statuses, [(self.head(), *self.GREEN)])

    def test_blocked_pull_request_before_deploy_gets_red_on_next_check(self):
        self.open_pr()
        self.advance_main()
        with mock.patch.object(verify, "run_verify", return_value=(False, "1 failed")):
            self.cycle()
        self.forget_lights()
        self.cycle()
        self.assertEqual(statuses, [(self.head(), *self.RED)])
        self.cycle()
        self.assertEqual(len(statuses), 1)

    def test_commit_dd_has_not_tested_is_not_lit_green(self):
        self.open_pr()
        self.forget_lights()
        seed = self.tmp / "seed"
        git("fetch", "origin", "dd-7", cwd=seed)
        git("checkout", "-b", "dd-7", "FETCH_HEAD", cwd=seed)
        (seed / "extra.py").write_text("X = 1\n")
        git("add", "-A", cwd=seed)
        git("-c", "user.name=Someone", "-c", "user.email=s@x", "commit", "-m", "extra", cwd=seed)
        git("push", "origin", "dd-7", cwd=seed)
        self.cycle()
        self.assertEqual(statuses, [])

    def test_story_moved_to_blocked_by_hand_turns_red_on_next_check(self):
        self.open_pr()
        board.issues["i1"]["state"] = {"id": S_BLK}
        self.cycle()
        self.assertEqual(statuses[1:], [(self.head(), *self.RED)])
        self.cycle()
        self.assertEqual(len(statuses), 2)

    def test_failed_run_on_story_moved_out_of_in_progress_sets_no_red(self):
        self.open_pr()
        board.issues["i1"]["state"] = {"id": S_IP}
        def moved_during_run(*args, **kwargs):
            board.issues["i1"]["state"] = {"id": S_BACK}
            return False, "1 failed"
        with mock.patch.object(verify, "run_verify", side_effect=moved_during_run):
            self.cycle()
        self.assertEqual(self.run_rows()[-1]["status"], runs.FAILED)
        self.assertEqual(plane.state_id(board.issues["i1"]), S_BACK)
        self.cycle()
        self.assertEqual(statuses, [(self.head(), *self.GREEN),
                                    (self.head(), "pending", "Being reworked: don't merge"),
                                    (self.head(), *self.GREEN)])

    def test_story_moved_out_of_blocked_is_not_left_red(self):
        self.open_pr()
        board.issues["i1"]["state"] = {"id": S_BLK}
        self.cycle()
        self.assertEqual(statuses[-1], (self.head(), *self.RED))
        board.issues["i1"]["state"] = {"id": S_BACK}
        self.cycle()
        self.assertEqual(statuses[-1], (self.head(), *self.GREEN))
        self.cycle()
        self.assertEqual(len(statuses), 3)

    def test_red_github_refused_is_retried_while_story_is_blocked(self):
        self.open_pr()
        board.issues["i1"]["state"] = {"id": S_BLK}
        status_failures.append("POST statuses -> 502: Bad gateway")
        self.cycle()
        self.assertEqual(len(statuses), 1)
        self.assertTrue(lights.light_owed(self.conn, "dd", "i1"))
        self.cycle()
        self.assertEqual(statuses[1:], [(self.head(), *self.RED)])

    def test_red_github_refused_is_dropped_once_story_leaves_blocked(self):
        self.open_pr()
        board.issues["i1"]["state"] = {"id": S_BLK}
        status_failures.append("unreachable")
        self.cycle()
        board.issues["i1"]["state"] = {"id": S_REV}
        self.cycle()
        self.assertEqual(statuses, [(self.head(), *self.GREEN)] * 2)



class Approvals(Base):
    advance_main = MainChanges.advance_main
    head = PullRequestLights.head
    open_pr = PullRequestLights.open_pr
    GREEN = ("success", "Up to date, tested and approved")
    WAITING = ("pending", "Waiting for your approval")
    CHANGED = ("pending", "Changed since your approval: re-review")
    UPDATING = ("pending", "Updating with latest main")

    def setUp(self):
        super().setUp()
        self.cfg["github"]["approver"] = "Approver-User"
        sent = mock.patch.object(lights.alerts, "send")
        self.alerts = sent.start()
        self.addCleanup(sent.stop)

    def review(self, sha=None, login="Approver-User", state="APPROVED"):
        reviews.setdefault("1", []).append(
            {"user": {"login": login}, "state": state, "commit_id": sha or self.head()})

    def changed_alerts(self):
        return [c for c in self.alerts.call_args_list if "changed since your approval" in c.kwargs.get("suffix", "")]

    REWORKING = ("pending", "Being reworked: don't merge")

    def start_rework(self):
        self.open_pr()
        self.review()
        self.cycle()
        board.issues["i1"]["state"] = {"id": S_IP}

    def test_approved_rework_waiting_for_agent_is_yellow_once(self):
        self.start_rework()
        choose.cool_down(self.cfg, self.conn, "claude-code")
        for _ in range(3):
            self.cycle()
        self.assertEqual(len(self.run_rows()), 1)
        self.assertEqual(statuses[-2:], [(self.head(), *self.GREEN),
                                        (self.head(), *self.REWORKING)])
        self.assertEqual(len(statuses), 3)

    def test_rework_restores_approval_without_new_commit_even_with_active_run(self):
        self.start_rework()
        dd_main.poll_once(self.cfg, self.conn)
        with mock.patch.object(plane.Plane, "get_issue") as single, \
                mock.patch.object(plane.Plane, "_request", autospec=True,
                                  side_effect=fake_plane_request) as request:
            dd_main.poll_refreshes(self.cfg, self.conn)
        single.assert_not_called()
        self.assertEqual(request.call_count, 1)
        self.assertTrue(runs.is_active(self.conn, "i1"))
        self.assertEqual(statuses[-1], (self.head(), *self.REWORKING))
        board.issues["i1"]["state"] = {"id": S_REV}
        dd_main.poll_refreshes(self.cfg, self.conn)
        self.assertEqual(statuses[-1], (self.head(), *self.GREEN))

    def test_rework_restores_current_rules_without_new_commit(self):
        self.start_rework()
        for state, expected in ((S_REV, self.WAITING), (S_BACK, self.WAITING),
                                (S_BLK, PullRequestLights.RED)):
            with self.subTest(state=state):
                board.issues["i1"]["state"] = {"id": S_IP}
                dd_main.poll_refreshes(self.cfg, self.conn)
                reviews.clear()
                board.issues["i1"]["state"] = {"id": state}
                dd_main.poll_refreshes(self.cfg, self.conn)
                self.assertEqual(statuses[-1], (self.head(), *expected))
        board.issues["i1"]["state"] = {"id": S_IP}
        dd_main.poll_refreshes(self.cfg, self.conn)
        self.advance_main()
        board.issues["i1"]["state"] = {"id": S_REV}
        dd_main.poll_refreshes(self.cfg, self.conn)
        self.assertEqual(statuses[-1], (self.head(), *self.UPDATING))

    def test_rework_error_warns_and_retries_only_while_in_progress(self):
        self.start_rework()
        status_failures.append("POST statuses -> 502: Bad gateway")
        dd_main.poll_refreshes(self.cfg, self.conn)
        self.assertTrue(lights.light_owed(self.conn, "dd", "i1"))
        self.assertTrue(any("502" in e["detail"] for e in runs.events(self.conn)
                            if e["type"] == "warning"))
        lights.lights(self.cfg, self.conn)
        self.assertEqual(statuses[-1], (self.head(), *self.GREEN))
        dd_main.poll_refreshes(self.cfg, self.conn)
        self.assertEqual(statuses[-1], (self.head(), *self.REWORKING))
        board.issues["i1"]["state"] = {"id": S_REV}
        dd_main.poll_refreshes(self.cfg, self.conn)
        board.issues["i1"]["state"] = {"id": S_IP}
        status_failures.append("unreachable")
        dd_main.poll_refreshes(self.cfg, self.conn)
        board.issues["i1"]["state"] = {"id": S_REV}
        count = len(statuses)
        self.cycle()
        self.assertEqual(statuses[count:], [(self.head(), *self.GREEN)])

    def test_rework_leaves_merged_and_closed_pull_requests_alone(self):
        self.start_rework()
        for merged in (True, False):
            prs["dd-7"].update(state="closed", merged=merged)
            dd_main.poll_refreshes(self.cfg, self.conn)
            dd_main.poll_refreshes(self.cfg, self.conn)
        self.assertEqual(len(statuses), 2)

    def test_event_failure_does_not_repeat_lights_or_approval_notices(self):
        with self.conn:
            self.conn.execute("CREATE TRIGGER fail_events BEFORE INSERT ON events "
                              "BEGIN SELECT RAISE(FAIL, 'history unavailable'); END")
        status_failures.append("POST statuses -> 502: Bad gateway")
        self.open_pr()
        self.assertTrue(lights.light_owed(self.conn, "dd", "i1"))
        self.cycle()
        self.assertFalse(lights.light_owed(self.conn, "dd", "i1"))
        self.assertEqual(statuses, [(self.head(), *self.WAITING)])
        count = len(statuses)
        self.cycle()
        self.cycle()
        self.assertEqual(len(statuses), count)
        self.review()
        self.cycle()
        self.advance_main(conflict=True)
        self.set_mode("repair")
        self.cycle()
        self.assertEqual(statuses[-1], (self.head(), *self.CHANGED))
        count = len(statuses)
        self.cycle()
        self.cycle()
        self.assertEqual(len(statuses), count)
        self.assertEqual(len(pr_comments), 1)
        self.assertEqual(len(self.changed_alerts()), 1)
        self.assertTrue(runs.approval_noticed(self.conn, "dd", "i1", self.head()))

    def test_upgrade_does_not_repeat_light_comment_or_alert(self):
        self.open_pr()
        self.review()
        self.cycle()
        self.advance_main(conflict=True)
        self.set_mode("repair")
        self.cycle()
        count = len(statuses)
        # Leave only the legacy event history, as on an installation before this upgrade.
        with self.conn:
            self.conn.execute("DROP TABLE pr_lights")
            self.conn.execute("DROP TABLE approval_notices")
            self.conn.execute("DROP TABLE migrations")
        self.conn.close()
        self.conn = runs.connect(self.cfg["db_path"])
        self.addCleanup(self.conn.close)
        self.cycle()
        self.cycle()
        self.assertEqual(len(statuses), count)
        self.assertEqual(len(pr_comments), 1)
        self.assertEqual(len(self.changed_alerts()), 1)

    def test_tested_but_not_approved_stays_yellow(self):
        self.open_pr()
        self.cycle(); self.cycle()
        self.assertEqual(statuses, [(self.head(), *self.WAITING)])

    def test_approving_turns_green_on_the_next_check_and_is_then_left_alone(self):
        self.open_pr()
        self.review()
        self.cycle()
        self.assertEqual(statuses, [(self.head(), *self.WAITING), (self.head(), *self.GREEN)])
        reads = len(review_reads)
        self.cycle(); self.cycle()
        self.assertEqual(len(review_reads), reads)
        self.assertEqual(len(statuses), 2)

    def test_approval_by_anyone_else_does_not_count(self):
        self.open_pr()
        self.review(login="SomeoneElse")
        self.cycle()
        self.assertEqual(statuses, [(self.head(), *self.WAITING)])

    def test_later_change_request_or_dismissal_withdraws_approval(self):
        for state in ("CHANGES_REQUESTED", "DISMISSED"):
            reviews.clear()
            reviews["1"] = [{"user": {"login": "approver-user"}, "state": "APPROVED", "commit_id": "a" * 40},
                            {"user": {"login": "Approver-User"}, "state": state, "commit_id": "a" * 40}]
            self.assertIsNone(github.approved_commit("o/r", "https://gh/pr/1", "Approver-User", GH), state)
        reviews["1"][1]["state"] = "COMMENTED"
        self.assertEqual(github.approved_commit("o/r", "https://gh/pr/1", "Approver-User", GH), "a" * 40)

    def test_clean_refresh_after_approval_stays_green(self):
        self.open_pr()
        self.review()
        self.cycle()
        approved = self.head()
        self.advance_main()
        self.cycle()
        self.assertEqual(self.run_rows()[-1]["kind"], "refresh")
        self.assertEqual(self.run_rows()[-1]["status"], runs.DONE)
        self.assertNotEqual(self.head(), approved)
        self.assertTrue(runs.combine_commit(self.conn, self.head())["clean"])
        self.assertEqual(statuses[-2:], [(approved, *self.UPDATING), (self.head(), *self.GREEN)])
        self.assertEqual(pr_comments, [])
        self.assertEqual(self.changed_alerts(), [])

    def test_repair_after_approval_turns_yellow_with_alert_and_comment(self):
        self.cfg["projects"]["dd"]["plane_identifier"] = "DYNAMICDIS"
        self.open_pr()
        self.review()
        self.cycle()
        self.advance_main(conflict=True)
        self.set_mode("repair")
        self.cycle()
        self.assertEqual(self.run_rows()[-1]["status"], runs.DONE)
        self.assertFalse(runs.combine_commit(self.conn, self.head())["clean"])
        self.assertEqual(statuses[-1], (self.head(), *self.CHANGED))
        [(number, body)] = pr_comments
        self.assertEqual(number, "1")
        self.assertIn(self.head()[:12], body)
        [alert] = self.changed_alerts()
        self.assertEqual(alert.kwargs["suffix"], ": PR #1 changed since your approval")
        self.assertEqual(alert.kwargs["level"], "active")
        # Said once; the light stays yellow until the new commit is approved.
        self.cycle()
        self.assertEqual(len(pr_comments), 1)
        self.assertEqual(len(self.changed_alerts()), 1)
        self.review()
        self.cycle()
        self.assertEqual(statuses[-1], (self.head(), *self.GREEN))

    def test_rework_after_approval_turns_yellow_without_extra_alert(self):
        self.open_pr()
        self.review()
        self.cycle()
        old = self.head()
        board.issues["i1"]["state"] = {"id": S_IP}
        self.cycle()
        self.assertNotEqual(self.head(), old)
        self.assertEqual(statuses[-2], (old, *self.REWORKING))
        self.assertEqual(len(self.run_rows()), 2)
        self.assertEqual(self.run_rows()[-1]["status"], runs.DONE)
        self.assertEqual(statuses[-1], (self.head(), *self.CHANGED))
        self.assertEqual(pr_comments, [])
        self.assertEqual(self.changed_alerts(), [])

    def test_github_error_records_warning_and_is_retried(self):
        review_failures.append("GET reviews -> 502: Bad gateway")
        self.open_pr()
        self.assertEqual(self.run_rows()[0]["status"], runs.DONE)
        self.assertEqual(plane.state_id(board.issues["i1"]), S_REV)
        self.assertEqual(statuses, [])
        warnings = [e["detail"] for e in runs.events(self.conn) if e["type"] == "warning"]
        self.assertTrue(any("502" in w for w in warnings), warnings)
        self.review()
        self.cycle()
        self.assertEqual(statuses, [(self.head(), *self.GREEN)])

    def test_github_error_while_waiting_keeps_checking(self):
        self.open_pr()
        review_failures.append("GET reviews -> 502: Bad gateway")
        self.review()
        self.cycle()
        self.assertEqual(statuses, [(self.head(), *self.WAITING)])
        self.assertTrue(any("502" in e["detail"] for e in runs.events(self.conn) if e["type"] == "warning"))
        self.cycle()
        self.assertEqual(statuses[-1], (self.head(), *self.GREEN))


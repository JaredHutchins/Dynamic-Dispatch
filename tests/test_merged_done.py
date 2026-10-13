"""Moving a story to Done once its pull request is merged."""

import queue, types, unittest
from unittest import mock
from dispatch import lights
from helpers import (
    Base, ORCH, S_BACK, S_BLK, S_REV, board, dd_main, gh_tokens, github, prs, runs,
)

S_DONE = "st-done"
MERGED = "Pull request #1 was merged, so this story is now Done."


class MergedPullRequests(Base):
    def setUp(self):
        super().setUp()
        self.cfg["projects"]["dd"]["states"]["done"] = S_DONE
        self.add_issue()
        self.cycle()  # the story is built and goes In Review with pull request #1
        self.assertEqual(board.issues["i1"]["state"], {"id": S_REV})
        self.alerts = queue.Queue()
        self.cfg["_alerts"] = types.SimpleNamespace(queue=self.alerts)
        board.transitions.clear()

    def merge(self, merged=True):
        prs["dd-7"].update(state="closed", merged=merged)

    def comments(self):
        return [c for c in board.comments if "was merged" in c[2]]

    def sent(self):
        return [self.alerts.get_nowait() for _ in range(self.alerts.qsize())]

    def kinds(self):
        return [e["type"] for e in runs.events(self.conn, issue_id="i1")]

    def test_merged_pull_request_moves_its_story_to_done(self):
        self.merge()
        dd_main.poll_refreshes(self.cfg, self.conn)
        self.assertEqual(board.transitions, [(ORCH, "i1", S_DONE)])
        self.assertEqual(len(self.comments()), 1)
        self.assertEqual(self.comments()[0][0], ORCH)
        self.assertIn(MERGED, self.comments()[0][2])
        alerts = self.sent()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0][2], MERGED)
        self.assertEqual(alerts[0][5], "passive")
        self.assertIn("moved_to_done", self.kinds())
        # The plan estimate's history times the story from its first run.
        self.assertEqual(len(runs.done_durations(self.conn, "dd", 30)), 1)
        # Done now: GitHub isn't asked again and nothing more is said.
        asked = len(gh_tokens)
        dd_main.poll_refreshes(self.cfg, self.conn)
        self.assertEqual(len(gh_tokens), asked)
        self.assertEqual(len(self.comments()), 1)
        self.assertEqual(self.sent(), [])

    def test_merged_pull_request_moves_a_blocked_story_to_done(self):
        board.issues["i1"]["state"] = {"id": S_BLK}
        self.merge()
        dd_main.poll_refreshes(self.cfg, self.conn)
        self.assertEqual(board.issues["i1"]["state"], {"id": S_DONE})
        self.assertEqual(len(self.comments()), 1)

    def test_closed_unmerged_pull_request_is_left_alone(self):
        self.merge(merged=False)
        dd_main.poll_refreshes(self.cfg, self.conn)
        dd_main.poll_refreshes(self.cfg, self.conn)
        self.assertEqual(board.transitions, [])
        self.assertEqual(board.issues["i1"]["state"], {"id": S_REV})
        self.assertEqual(self.comments(), [])
        self.assertEqual(self.sent(), [])
        self.assertEqual(self.kinds().count("pr_closed"), 1)
        self.assertEqual(runs.done_durations(self.conn, "dd", 30), [])

    def test_story_already_done_or_elsewhere_is_left_alone(self):
        self.merge()
        for state in (S_DONE, S_BACK):
            with self.subTest(state=state):
                board.issues["i1"]["state"] = {"id": state}
                asked = len(gh_tokens)
                dd_main.poll_refreshes(self.cfg, self.conn)
                self.assertEqual(len(gh_tokens), asked)
                self.assertEqual(board.transitions, [])
                self.assertEqual(self.comments(), [])

    def test_without_a_done_state_nothing_changes(self):
        del self.cfg["projects"]["dd"]["states"]["done"]
        self.merge()
        dd_main.poll_refreshes(self.cfg, self.conn)
        self.assertEqual(board.transitions, [])
        self.assertEqual(board.issues["i1"]["state"], {"id": S_REV})
        self.assertEqual(self.comments(), [])
        self.assertIn("refresh_skipped", self.kinds())
        self.assertNotIn("moved_to_done", self.kinds())

    def test_github_error_warns_and_retries(self):
        self.merge()
        with mock.patch.object(github, "pr_state", side_effect=github.GitHubError("down")):
            dd_main.poll_refreshes(self.cfg, self.conn)
        self.assertEqual(board.transitions, [])
        self.assertIn("warning", self.kinds())
        dd_main.poll_refreshes(self.cfg, self.conn)
        self.assertEqual(board.transitions, [(ORCH, "i1", S_DONE)])
        self.assertEqual(len(self.comments()), 1)

    def test_raw_timeout_warns_and_the_light_and_refresh_checks_still_run(self):
        for state, kind, light in ((S_REV, "main_unchanged", "success"), (S_BLK, None, "failure")):
            with self.subTest(state=state):
                board.issues["i1"]["state"] = {"id": state}
                seen = len(self.kinds())
                # Only the merge check times out; the refresh check's own call gets through.
                replies = [TimeoutError("timed out"), "open"]
                with mock.patch.object(github, "pr_state", side_effect=replies):
                    dd_main.poll_refreshes(self.cfg, self.conn)
                since = self.kinds()[seen:]
                self.assertIn("warning", since)
                if kind:
                    self.assertIn(kind, since)
                self.assertEqual(lights.light_state(self.conn, "dd", "i1"), light)
                self.assertEqual(board.transitions, [])

    def test_plane_error_warns_and_retries_without_a_second_comment(self):
        self.merge()
        board.fail_patch = 1
        dd_main.poll_refreshes(self.cfg, self.conn)
        self.assertEqual(board.issues["i1"]["state"], {"id": S_REV})
        self.assertEqual(len(self.comments()), 1)
        self.assertIn("warning", self.kinds())
        dd_main.poll_refreshes(self.cfg, self.conn)
        self.assertEqual(board.issues["i1"]["state"], {"id": S_DONE})
        self.assertEqual(len(self.comments()), 1)
        self.assertEqual(len(self.sent()), 1)

    def test_lost_comment_reply_is_not_posted_twice(self):
        self.merge()
        board.lose_post_reply = 1
        dd_main.poll_refreshes(self.cfg, self.conn)
        self.assertEqual(board.transitions, [])
        self.assertIn("warning", self.kinds())
        dd_main.poll_refreshes(self.cfg, self.conn)
        self.assertEqual(board.transitions, [(ORCH, "i1", S_DONE)])
        self.assertEqual(len(self.comments()), 1)

    def test_story_with_an_active_run_waits_for_it(self):
        self.merge()
        with mock.patch.object(runs, "is_active", return_value=True):
            dd_main.poll_refreshes(self.cfg, self.conn)
        self.assertEqual(board.transitions, [])
        dd_main.poll_refreshes(self.cfg, self.conn)
        self.assertEqual(board.transitions, [(ORCH, "i1", S_DONE)])


if __name__ == "__main__":
    unittest.main()

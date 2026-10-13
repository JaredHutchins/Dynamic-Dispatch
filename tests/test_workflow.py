"""Tests for workflow."""

import os
import time
from unittest import mock
from pathlib import Path
from dispatch import agents, github, runs, sync, text, worker
import main as dd_main
from helpers import (
    Base,
    CLAUDE,
    GH,
    MainChanges,
    ORCH,
    S_BACK,
    S_BLK,
    S_IP,
    S_REV,
    board,
    comment_failures,
    description_failures,
    git,
    pr_comments,
    prs,
    sandbox_calls,
)


class HappyPath(Base):
    def test_story_to_review(self):
        self.add_issue()
        self.cycle()
        [row] = self.run_rows()
        self.assertEqual(row["status"], runs.DONE, row)
        self.assertEqual(board.issues["i1"]["state"], {"id": S_REV})
        self.assertEqual(board.assignments, [(ORCH, "i1", ["user-claude"])])
        self.assertEqual(board.issues["i1"]["assignees"], ["user-claude"])
        # work events are written as the agent, not the orchestrator
        self.assertEqual(board.transitions, [(CLAUDE, "i1", S_REV)])
        self.assertEqual(len(board.comments), 1)
        self.assertEqual(board.comments[0][0], CLAUDE)
        self.assertIn("https://gh/pr/1", board.comments[0][2])
        # branch reached the remote, authored as the agent, matching Plane
        log = git("log", "-1", "--format=%an <%ae>|%s", "dd-7", cwd=self.origin)
        self.assertEqual(log.strip(), "Claude <claude@example.com>|7: Add a value")
        # successful worktree removed
        self.assertFalse((self.wt_root / "dd-7").exists())
        # agent and verify both went through the sandbox; only the agent got an env file
        self.assertEqual(len(sandbox_calls), 2)
        self.assertEqual(sandbox_calls[0]["env_file"], "/etc/dynamic-dispatch/claude.env")
        self.assertIsNone(sandbox_calls[1]["env_file"])
        self.assertEqual(sandbox_calls[1]["command"][1:3], ["-m", "compileall"])

    def test_github_names_the_story_by_its_plane_key(self):
        self.cfg["projects"]["dd"]["plane_identifier"] = "DYNAMICDIS"
        self.add_issue()
        self.cycle()
        subject = git("log", "-1", "--format=%s", "dd-7", cwd=self.origin).strip()
        self.assertEqual(subject, "DYNAMICDIS-7: Add a value")
        [pr] = prs.values()
        self.assertEqual(pr["title"], "DYNAMICDIS-7: Add a value")
        self.assertIn("Story DYNAMICDIS-7: Add a value", pr["body"])
        # GitHub would link "#7" to its own issue or pull request 7.
        for text in (subject, pr["title"], pr["body"]):
            self.assertNotIn("#", text)

    def test_without_plane_identifier_the_plain_number_is_used(self):
        self.add_issue()
        self.cycle()
        [pr] = prs.values()
        self.assertEqual(pr["title"], "7: Add a value")
        self.assertIn("Story 7: Add a value", pr["body"])
        subject = git("log", "-1", "--format=%s", "dd-7", cwd=self.origin)
        for text in (subject, pr["title"], pr["body"]):
            self.assertNotIn("#", text)

    def test_story_logs_use_the_same_key_throughout_its_lifecycle(self):
        for identifier, seq, key in (("DYNAMICDIS", 12, "DYNAMICDIS-12"), ("", 13, "13")):
            with self.subTest(identifier=identifier):
                self.cfg["projects"]["dd"]["plane_identifier"] = identifier
                self.add_issue(f"i{seq}", seq=seq)
                with mock.patch.object(dd_main, "log") as scheduling, \
                        mock.patch.object(worker, "log") as working, \
                        mock.patch.object(sync, "log") as finished:
                    self.cycle()
                self.assertEqual(self.run_rows()[-1]["status"], runs.DONE)
                self.assertEqual([c.args[0] for c in scheduling.call_args_list], [
                    f"[dd] queued {key} for Claude: Add a value",
                    f"running {key} with claude-code on dd-{seq}",
                ])
                progress = [c.args[0] for c in working.call_args_list]
                self.assertTrue(progress)
                self.assertTrue(all(line.startswith(f"{key}: ") for line in progress), progress)
                self.assertEqual([c.args[0] for c in finished.call_args_list],
                                 [f"{key}: In Review"])

    def test_string_state_shape_also_works(self):
        self.add_issue(nested=False)
        self.cycle()
        self.assertEqual(self.run_rows()[0]["status"], runs.DONE)

    def test_prompt_contains_story_and_dod_from_base_branch(self):
        self.set_mode("prompt")
        self.add_issue()
        self.cycle()
        prompt = git("show", "dd-7:prompt.txt", cwd=self.origin)
        self.assertIn("Add VALUE to feature.py", prompt)
        self.assertIn("PR explains why", prompt)

    def test_rework_reuses_branch_and_pr(self):
        self.add_issue()
        self.cycle()
        board.issues["i1"]["state"] = {"id": S_IP}  # human sends it back
        self.cycle()
        rows = self.run_rows()
        self.assertEqual([r["status"] for r in rows], [runs.DONE, runs.DONE])
        self.assertEqual(len(prs), 1)
        count = git("rev-list", "--count", "main..dd-7", cwd=self.origin).strip()
        self.assertEqual(count, "2")



class ReworkedDescriptions(Base):
    advance_main = MainChanges.advance_main

    def build_then_rework(self, second="second round"):
        self.add_issue()
        os.environ["LEAK"] = "first round"
        self.cycle()
        self.first = prs["dd-7"]["body"]
        board.issues["i1"]["state"] = {"id": S_IP}  # human sends it back
        os.environ["LEAK"] = second
        self.cycle()
        return self.run_rows()[-1]

    def local_zone(self, name):
        self.addCleanup(time.tzset)  # cleanups run last-first, so this follows the TZ restore
        patch = mock.patch.dict(os.environ, {"TZ": name})
        patch.start(); self.addCleanup(patch.stop)
        time.tzset()

    def test_rework_replaces_description_and_keeps_the_old_one_as_a_comment(self):
        self.local_zone("America/Chicago")
        row = self.build_then_rework()
        self.assertEqual(row["status"], runs.DONE, row)
        self.assertEqual(len(prs), 1)
        self.assertEqual(prs["dd-7"]["body"],
                         "Changed things second round\n\n---\nStory 7: Add a value\nAgent: Claude")
        [(number, comment)] = pr_comments
        self.assertEqual(number, "1")
        self.assertRegex(comment, rf"^Previous description, replaced by run {row['id']} on "
                                  r"\d{4}-\d\d-\d\d \d\d:\d\d C[DS]T\n\n")
        self.assertIn(self.first, comment)
        self.assertIn("Changed things first round", comment)
        self.assertTrue(comment.endswith(f"<!-- Dynamic Dispatch run {row['id']}: previous description -->"))
        self.assertIn("pr_described", [e["type"] for e in runs.events(self.conn)])
        self.assertEqual(board.issues["i1"]["state"], {"id": S_REV})

    def test_new_description_and_comment_are_scrubbed_and_capped(self):
        self.build_then_rework(second=GH + " " + "x" * github.BODY_LIMIT)
        body = prs["dd-7"]["body"]
        self.assertNotIn(GH, body)
        self.assertTrue(body.endswith("[... truncated ...]"))
        self.assertLessEqual(len(body), github.BODY_LIMIT + 50)
        board.issues["i1"]["state"] = {"id": S_IP}
        os.environ["LEAK"] = "third round"
        self.cycle()
        comment = pr_comments[-1][1]
        self.assertNotIn(GH, comment)
        self.assertIn("[... truncated ...]", comment)
        self.assertTrue(comment.endswith("previous description -->"))

    def test_refresh_leaves_the_description_alone(self):
        self.add_issue()
        self.cycle()
        body = prs["dd-7"]["body"]
        self.advance_main()
        with mock.patch.object(agents, "run") as agent:
            self.cycle()
        agent.assert_not_called()
        row = self.run_rows()[-1]
        self.assertEqual((row["kind"], row["status"]), ("refresh", runs.DONE))
        self.assertEqual(prs["dd-7"]["body"], body)
        self.assertEqual(pr_comments, [])

    def test_failed_comment_leaves_description_and_story_still_reaches_review(self):
        comment_failures.append("GitHub is down")
        row = self.build_then_rework()
        self.assertEqual(row["status"], runs.DONE, row)
        self.assertEqual(board.issues["i1"]["state"], {"id": S_REV})
        self.assertEqual(prs["dd-7"]["body"], self.first)
        self.assertEqual(pr_comments, [])
        self.assertTrue(any(e["type"] == "warning" and "description" in e["detail"]
                            and "GitHub is down" in e["detail"] for e in runs.events(self.conn)))
        self.assertIn("Changed things second round", board.comments[-1][2])

    def test_raw_timeout_still_reaches_review(self):
        with mock.patch.object(github, "replace_description", side_effect=TimeoutError("timed out")):
            row = self.build_then_rework()
        self.assertEqual(row["status"], runs.DONE, row)
        self.assertEqual(board.issues["i1"]["state"], {"id": S_REV})
        self.assertEqual(prs["dd-7"]["body"], self.first)
        self.assertTrue(any(e["type"] == "warning" and "description" in e["detail"]
                            and "timed out" in e["detail"] for e in runs.events(self.conn)))
        self.assertNotIn("pr_described", [e["type"] for e in runs.events(self.conn)])

    def test_failed_update_keeps_the_comment_and_still_reaches_review(self):
        description_failures.append("GitHub is down")
        row = self.build_then_rework()
        self.assertEqual(row["status"], runs.DONE, row)
        self.assertEqual(board.issues["i1"]["state"], {"id": S_REV})
        self.assertEqual(prs["dd-7"]["body"], self.first)
        self.assertEqual(len(pr_comments), 1)
        self.assertTrue(any(e["type"] == "warning" and "GitHub is down" in e["detail"]
                            for e in runs.events(self.conn)))

    def test_retried_run_does_not_post_the_old_description_twice(self):
        self.add_issue()
        self.cycle()
        first = prs["dd-7"]["body"]
        pull = {"html_url": prs["dd-7"]["url"], "body": first}
        marker = "<!-- Dynamic Dispatch run 2: previous description -->"
        replace = lambda: github.replace_description(
            "owner/repo", pull, "New summary", "Previous description", marker, GH)
        description_failures.append("timed out")
        with self.assertRaises(github.GitHubError):
            replace()
        replace()  # the same run, retried after the update failed
        self.assertEqual(len(pr_comments), 1)
        self.assertIn(first, pr_comments[0][1])
        self.assertEqual(prs["dd-7"]["body"], "New summary")
        pull["body"] = prs["dd-7"]["body"]
        replace()  # and retried again after it succeeded
        self.assertEqual(len(pr_comments), 1)


class Failures(Base):
    def assert_blocked(self, text):
        [row] = self.run_rows()
        self.assertEqual(row["status"], runs.FAILED, row)
        self.assertEqual(board.issues["i1"]["state"], {"id": S_BLK})
        self.assertEqual(board.transitions, [(ORCH, "i1", S_BLK)])  # system event, orchestrator voice
        self.assertIn(text, board.comments[0][2])
        return row

    def test_agent_failure(self):
        self.set_mode("fail"); self.add_issue(); self.cycle()
        row = self.assert_blocked("Exited with code 3")
        self.assertTrue(Path(row["worktree"]).exists())  # kept for inspection

    def test_no_changes(self):
        self.set_mode("nochange"); self.add_issue(); self.cycle()
        self.assert_blocked("changed nothing")

    def test_verify_failure_never_reaches_review(self):
        self.set_mode("syntax"); self.add_issue(); self.cycle()
        self.assert_blocked("Verification failed")
        self.assertEqual(prs, {})
        self.assertNotIn("dd-7", git("branch", "--list", cwd=self.origin))

    def test_moved_out_before_start(self):
        self.add_issue()
        dd_main.poll_once(self.cfg, self.conn)
        board.issues["i1"]["state"] = {"id": S_BACK}
        dd_main.drain(self.cfg, self.conn)
        [row] = self.run_rows()
        self.assertEqual(row["status"], runs.CANCELLED)
        self.assertEqual(board.transitions, []); self.assertEqual(board.comments, [])

    def test_moved_out_during_work_is_not_pushed(self):
        self.add_issue()
        board.get_states = [S_IP, S_BACK]  # start check passes, pre-push check fails
        self.cycle()
        [row] = self.run_rows()
        self.assertEqual(row["status"], runs.CANCELLED)
        self.assertEqual(prs, {})
        self.assertEqual(board.transitions, [])
        self.assertNotIn("dd-7", git("branch", "--list", cwd=self.origin))

    def test_plane_unreachable_at_start_is_retried_not_blocked(self):
        self.add_issue(); board.fail_get = 1
        self.cycle()
        self.assertEqual(self.run_rows()[0]["status"], runs.CANCELLED)
        self.cycle()
        self.assertEqual([r["status"] for r in self.run_rows()], [runs.CANCELLED, runs.DONE])


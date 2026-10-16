"""Decision requests use recorded rework and a strict marker, never prose meaning."""

from contextlib import ExitStack
from datetime import datetime, timedelta
from pathlib import Path
import unittest
from unittest import mock

from dispatch import agents, alerts, github, gitops, lights, rework, runs, sync, text, verify, worker
import main as dd_main
from helpers import (Base, CLAUDE, GH, MainChanges, S_IP, S_REV, S_BLK, board,
                     comment_failures, fake_gh_request, git, pr_comments, prs, reviews, statuses,
                     temporary_directory)

DETAIL = "Instruction: change deploy/README.md.\nNot carried out: already correct.\nEvidence: deploy/README.md:12."
SUMMARY = rework.MARKER + "\n" + DETAIL


class Marker(unittest.TestCase):
    def test_exact_first_line_and_nonblank_explanation(self):
        for summary in (None, "", rework.MARKER, rework.MARKER + "\n \t\n",
                        "\n" + SUMMARY, "Preface\n" + SUMMARY, " " + SUMMARY,
                        rework.MARKER + " \nreason", rework.MARKER + ":\nreason"):
            with self.subTest(summary=summary):
                self.assertIsNone(rework.explanation(summary))
        self.assertEqual(rework.explanation(SUMMARY), DETAIL)
        self.assertEqual(rework.explanation(rework.MARKER + "\r\nanything"), "anything")

    def test_migrates_old_store_and_preserves_saved_outcome_on_reopen(self):
        path = temporary_directory(self) / "runs.db"
        conn = runs.connect(path)
        rid = runs.enqueue(conn, project="p", issue={"id": "i", "sequence_id": 1, "name": "n"},
                           agent="a", branch="b")
        for name in ("needs_decision", "no_changes", "decision_at"):
            conn.execute(f"ALTER TABLE runs DROP COLUMN {name}")
        conn.commit()
        conn.close()
        conn = runs.connect(path)
        self.addCleanup(conn.close)
        row = runs.get(conn, rid)
        self.assertEqual((row["needs_decision"], row["no_changes"], row["decision_at"]), (0, 0, None))
        runs.await_review(conn, rid, summary=DETAIL, usage=None, cost_usd=None, pr_url="https://gh/pr/1",
                          worktree="work", needs_decision=True, no_changes=True)
        conn.close()
        conn = runs.connect(path)
        self.addCleanup(conn.close)
        row = runs.get(conn, rid)
        self.assertEqual((row["status"], row["needs_decision"], row["no_changes"]),
                         (runs.AWAITING_REVIEW, 1, 1))
        self.assertIsNotNone(row["decision_at"])


class Decisions(Base):
    advance_main = MainChanges.advance_main

    def prepare(self):
        self.add_issue()
        self.cycle()
        board.issues["i1"]["state"] = {"id": S_IP}
        self.before = dict(prs["dd-7"])
        self.head = git("rev-parse", "dd-7", cwd=self.origin).strip()

    def answer(self, summary=SUMMARY, changed=False):
        def agent(cfg, agent_cfg, prompt, worktree):
            self.prompt = prompt
            if changed:
                (Path(worktree) / "another.py").write_text("VALUE = 42\n")
                return {"ok": True, "summary": summary + "\n\nDocs: none needed, because another.py is internal."}
            return {"ok": True, "summary": summary}
        return mock.patch.object(agents, "run", side_effect=agent)

    def test_run_132_unchanged_decision_returns_to_review_with_pr_untouched(self):
        self.prepare()
        with ExitStack() as stack:
            forbidden = [stack.enter_context(mock.patch.object(gitops, name))
                         for name in ("commit_all", "combine_main", "push")]
            forbidden.append(stack.enter_context(mock.patch.object(github, "replace_description")))
            forbidden.append(stack.enter_context(mock.patch.object(verify, "run_verify")))
            stack.enter_context(self.answer())
            self.cycle()
            for call in forbidden:
                call.assert_not_called()
        row = self.run_rows()[-1]
        self.assertEqual((row["status"], row["needs_decision"], row["no_changes"]), (runs.DONE, 1, 1))
        self.assertEqual(board.issues["i1"]["state"], {"id": S_REV})
        self.assertFalse(Path(row["worktree"]).exists())
        self.assertEqual(prs["dd-7"], self.before)
        self.assertEqual(git("rev-parse", "dd-7", cwd=self.origin).strip(), self.head)
        self.assertEqual(git("rev-parse", "dd-7", cwd=self.repo).strip(), self.head)
        self.assertEqual(row["summary"], DETAIL)
        self.assertEqual(len(pr_comments), 1)
        self.assertTrue(pr_comments[0][1].startswith(DETAIL))
        self.assertIn(f"run {row['id']}: decision needed", pr_comments[0][1])
        author, _, comment = board.comments[-1]
        self.assertEqual(author, CLAUDE)
        plain = text.html_to_text(comment)
        self.assertLess(plain.index(self.before["url"]), plain.index("No changes made."))
        self.assertIn("No changes made. Claude didn't carry out part of the rework and needs your decision:", plain)
        self.assertNotIn(rework.MARKER, comment)
        self.assertIn(self.before["url"], self.prompt)
        for phrase in ("reviewer's latest instructions", "Carry out every instruction", "already done",
                       "obsolete", "contradicts the current code", "file and line", "command output",
                       "already seen your objection", "make it"):
            self.assertIn(phrase, self.prompt)

    def test_rework_prompt_asks_for_the_usual_complete_summary_after_declined_instructions(self):
        prompt = rework.prompt("https://gh/pr/1")
        self.assertIn("After the declined instructions, also give your usual complete summary of what "
                      "you changed and why", prompt)
        self.assertIn("becomes the pull request description", prompt)
        self.assertEqual(rework.prompt(None), "")

    def test_github_down_during_rework_check_cancels_without_blocking_or_alert(self):
        self.prepare()
        comments = len(board.comments)
        with mock.patch.object(github, "pr_is_open", side_effect=github.GitHubError("offline")), \
                mock.patch.object(alerts, "send") as send, self.answer() as agent:
            self.cycle()
        agent.assert_not_called()
        send.assert_not_called()
        row = self.run_rows()[-1]
        self.assertEqual(row["status"], runs.CANCELLED)
        self.assertIn("next poll", row["note"])
        self.assertIn("warning", [e["type"] for e in runs.events(self.conn, run_id=row["id"])])
        self.assertEqual(board.issues["i1"]["state"], {"id": S_IP})
        self.assertEqual(len(board.comments), comments)
        self.assertEqual(prs["dd-7"], self.before)
        with self.answer():
            self.cycle()
        row = self.run_rows()[-1]
        self.assertEqual((row["status"], row["needs_decision"]), (runs.DONE, 1))
        self.assertEqual(board.issues["i1"]["state"], {"id": S_REV})
        self.assertIn(self.before["url"], self.prompt)

    def test_invalid_markers_block_rework_with_exact_existing_message(self):
        self.prepare()
        for summary in ("No work needed", "Preface\n" + SUMMARY, rework.MARKER, rework.MARKER + "\n \t"):
            with self.subTest(summary=summary), self.answer(summary):
                board.issues["i1"]["state"] = {"id": S_IP}
                self.cycle()
                row = self.run_rows()[-1]
                self.assertEqual(row["status"], runs.FAILED)
                self.assertEqual(row["error"], f"Claude finished but changed nothing.\n\nIts summary:\n{summary}")
                self.assertFalse(row["needs_decision"])
        self.assertEqual(pr_comments, [])

    def test_first_build_blocks_even_with_valid_marker_and_gets_no_rework_prompt(self):
        self.add_issue()
        with self.answer():
            self.cycle()
        row = self.run_rows()[-1]
        self.assertEqual(row["status"], runs.FAILED)
        self.assertEqual(board.issues["i1"]["state"], {"id": S_BLK})
        self.assertTrue(row["error"].endswith("A decision request only applies to rework of an open pull request."))
        self.assertNotIn(rework.MARKER, self.prompt)
        self.assertFalse(row["needs_decision"])
        self.assertEqual(prs, {})

    def test_changed_decision_follows_normal_path_and_flags_comment_and_alert(self):
        self.prepare()
        with self.answer(changed=True), mock.patch.object(alerts, "send") as send:
            self.cycle()
        row = self.run_rows()[-1]
        self.assertEqual((row["status"], row["needs_decision"], row["no_changes"]), (runs.DONE, 1, 0))
        self.assertTrue(row["committed"] and row["pushed"])
        self.assertNotEqual(git("rev-parse", "dd-7", cwd=self.origin).strip(), self.head)
        self.assertIn(DETAIL, prs["dd-7"]["body"])
        self.assertNotIn(rework.MARKER, prs["dd-7"]["body"])
        self.assertIn("Previous description", pr_comments[0][1])
        self.assertIn("needs your decision", board.comments[-1][2])
        self.assertNotIn("No changes made", board.comments[-1][2])
        self.assertEqual(send.call_args.kwargs["suffix"], " needs your decision")
        self.assertEqual(send.call_args.kwargs["level"], "active")
        self.assertEqual(send.call_args.kwargs["url"], self.before["url"])

    def test_comments_once_and_audible_alert_once_after_lost_plane_reply(self):
        self.prepare()
        board.lose_post_reply = 1
        with self.answer(), mock.patch.object(alerts, "send") as send:
            self.cycle()
            row = self.run_rows()[-1]
            self.assertEqual(row["status"], runs.AWAITING_REVIEW)
            self.assertEqual(len(pr_comments), 1)
            self.assertEqual(len(board.comments), 2)
            self.assertEqual(runs.interrupt_running(self.conn), [])
            with mock.patch.object(agents, "run") as agent:
                sync.pending(self.cfg, self.conn)
                sync.pending(self.cfg, self.conn)
            agent.assert_not_called()
            decisions = [c for c in send.call_args_list if c.kwargs.get("suffix") == " needs your decision"]
            self.assertEqual(len(decisions), 1)
            self.assertEqual(decisions[0].kwargs["level"], "active")  # Bark's audible level
            self.assertEqual(decisions[0].kwargs["url"], self.before["url"])
        self.assertEqual(len(pr_comments), 1)
        self.assertEqual(len(board.comments), 2)
        self.assertEqual(self.run_rows()[-1]["status"], runs.DONE)

    def test_lost_github_reply_and_board_retry_do_not_duplicate_comment(self):
        self.prepare()
        board.fail_patch = 1
        def request(method, path, token, payload=None, **kwargs):
            result = fake_gh_request(method, path, token, payload, **kwargs)
            if method == "POST" and path.endswith("/comments"):
                raise TimeoutError("reply lost")
            return result
        with self.answer(), mock.patch.object(github, "_request", side_effect=request):
            self.cycle()
        self.assertEqual(self.run_rows()[-1]["status"], runs.AWAITING_REVIEW)
        sync.pending(self.cfg, self.conn)
        self.assertEqual(len(pr_comments), 1)
        self.assertEqual(self.run_rows()[-1]["status"], runs.DONE)

    def test_github_comment_failure_does_not_block_review(self):
        self.prepare()
        comment_failures.append("offline")
        with self.answer():
            self.cycle()
        self.assertEqual(self.run_rows()[-1]["status"], runs.DONE)
        self.assertEqual(pr_comments, [])
        self.assertEqual(prs["dd-7"], self.before)

    def test_restart_before_outcome_save_runs_agent_again(self):
        class Crash(BaseException):
            pass
        self.prepare()
        dd_main.poll_once(self.cfg, self.conn)
        row = runs.claim_next(self.conn)
        with self.answer(), mock.patch.object(runs, "await_review", side_effect=Crash):
            with self.assertRaises(Crash):
                worker.execute(self.cfg, self.conn, row)
        self.wait_assignments()
        self.assertEqual(runs.get(self.conn, row["id"])["status"], runs.RUNNING)
        self.assertEqual(pr_comments, [])
        runs.interrupt_running(self.conn)
        with self.answer() as agent:
            self.cycle()
        agent.assert_called_once()
        self.assertEqual(self.run_rows()[-1]["status"], runs.DONE)
        self.assertEqual(len(pr_comments), 1)

    def test_restart_immediately_after_outcome_save_only_syncs(self):
        class Crash(BaseException):
            pass
        self.prepare()
        dd_main.poll_once(self.cfg, self.conn)
        row = runs.claim_next(self.conn)
        with self.answer(), mock.patch.object(sync, "reconcile", side_effect=Crash):
            with self.assertRaises(Crash):
                worker.execute(self.cfg, self.conn, row)
        self.wait_assignments()
        self.assertEqual(runs.get(self.conn, row["id"])["status"], runs.AWAITING_REVIEW)
        self.assertEqual(pr_comments, [])
        reopened = runs.connect(self.cfg["db_path"])
        self.addCleanup(reopened.close)
        self.assertEqual(runs.interrupt_running(reopened), [])
        with mock.patch.object(agents, "run") as agent:
            sync.pending(self.cfg, reopened)
            sync.pending(self.cfg, reopened)
        agent.assert_not_called()
        self.assertEqual(len(pr_comments), 1)
        self.assertEqual(self.run_rows()[-1]["status"], runs.DONE)

    def test_approval_before_return_waits_through_refresh_until_new_approval(self):
        self.cfg["github"]["approver"] = "reviewer"
        self.prepare()
        reviews["1"] = [{"user": {"login": "reviewer"}, "state": "APPROVED", "commit_id": self.head,
                         "submitted_at": "2000-01-01T00:00:00Z"}]
        lights.light(self.cfg, self.conn, "dd", "i1", "success")
        self.assertEqual(statuses[-1][2], "Up to date, tested and approved")
        with self.answer():
            self.cycle()
        row = self.run_rows()[-1]
        self.assertEqual(statuses[-1][2], "Waiting for your approval")
        self.advance_main()
        with mock.patch.object(agents, "run") as agent:
            self.cycle()
        agent.assert_not_called()
        self.assertEqual(self.run_rows()[-1]["kind"], "refresh")
        self.assertEqual(statuses[-1][2], "Waiting for your approval")
        after = datetime.fromisoformat(row["decision_at"]) + timedelta(seconds=1)
        reviews["1"].append({"user": {"login": "reviewer"}, "state": "APPROVED", "commit_id": self.head,
                             "submitted_at": after.isoformat()})
        self.cycle()
        self.assertEqual(statuses[-1][2], "Up to date, tested and approved")

    def test_approval_time_must_be_readable_and_after_the_return(self):
        cutoff = "2026-10-01T12:00:00+00:00"
        approve = {"user": {"login": "reviewer"}, "state": "APPROVED", "commit_id": "a" * 40}
        for submitted, counts in ((None, False), ("not a time", False), ("2026-10-01T12:00:00Z", False),
                                  ("2026-10-01T12:00:01Z", True)):
            with self.subTest(submitted=submitted):
                reviews["1"] = [dict(approve, submitted_at=submitted) if submitted else approve]
                approved = github.approved_commit("o/r", "https://gh/pr/1", "reviewer", GH, after=cutoff)
                self.assertEqual(approved, "a" * 40 if counts else None)
                self.assertEqual(github.approved_commit("o/r", "https://gh/pr/1", "reviewer", GH), "a" * 40)

    def test_eligibility_exclusions_use_dd_records_and_existing_branch(self):
        self.prepare()
        run = dict(self.run_rows()[-1], id=999)
        self.assertEqual(rework.pull_request(self.cfg, self.conn, run), self.before["url"])
        for change in ({"kind": "refresh"}, {"hold_data": "{}"}, {"id": 1},
                       {"project": "other"}, {"issue_id": "other"}, {"branch": "other"}):
            with self.subTest(change=change):
                self.assertIsNone(rework.pull_request(self.cfg, self.conn, dict(run, **change)))
        for state, merged in (("closed", False), ("closed", True)):
            prs["dd-7"].update(state=state, merged=merged)
            self.assertIsNone(rework.pull_request(self.cfg, self.conn, run))
        prs["dd-7"].update(state="open", merged=False)
        with mock.patch.object(gitops, "has_unpushed_commits", return_value=True):
            self.assertIsNone(rework.pull_request(self.cfg, self.conn, run))
        with mock.patch.object(gitops, "branch_exists", return_value=False):
            self.assertIsNone(rework.pull_request(self.cfg, self.conn, run))
        with mock.patch.object(runs, "last_pushed", return_value={"pr_url": None}):
            self.assertIsNone(rework.pull_request(self.cfg, self.conn, run))

    def test_missing_branch_is_not_rework_after_worktree_recreation(self):
        self.prepare()
        git("branch", "-D", "dd-7", cwd=self.repo)
        with self.answer():
            self.cycle()
        self.assertEqual(self.run_rows()[-1]["status"], runs.FAILED)
        self.assertNotIn(rework.MARKER, self.prompt)

    def test_github_marker_survives_truncation_and_pagination(self):
        self.prepare()
        marker = "<!-- Dynamic Dispatch run 132: decision needed -->"
        pr_comments.extend(("1", "older comment") for _ in range(github.COMMENTS_PAGE))
        for _ in range(2):
            github.comment_once("owner/repo", self.before["url"], "x" * (github.BODY_LIMIT + 1), marker, GH)
        self.assertEqual(len(pr_comments), github.COMMENTS_PAGE + 1)
        self.assertTrue(pr_comments[-1][1].endswith(marker))

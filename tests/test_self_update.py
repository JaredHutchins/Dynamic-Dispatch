"""DD updating itself after a merge to its own code, once main's tests pass on it."""

import io
import json
import sys
from pathlib import Path
from unittest import mock

from dispatch import alerts, github, gitops, plane, publicupdate, runs, selfupdate
import main as dd_main
from helpers import Base, GH, ORCH, S_REV, board, git


class SelfUpdate(Base):
    def setUp(self):
        super().setUp()
        selfupdate._waiting.update(target=None, idle=False)
        selfupdate._named.update(key=None, title=None)
        self.checkout = self.tmp / "dd"
        git("clone", str(self.origin), str(self.checkout), cwd=self.tmp)
        self.started = self.head()
        self.cfg["self_update"] = "dd"
        self.sent = []
        patch = mock.patch.object(alerts, "send", side_effect=lambda cfg, project, issue_id, message, **kw:
                                  self.sent.append((project, message, kw.get("level"))))
        patch.start()
        self.addCleanup(patch.stop)
        # Pull request titles as GitHub holds them; a number missing here fails its lookup.
        self.pr_titles = {}
        self.lookups = []
        patch = mock.patch.object(github, "pr_title", side_effect=self.pr_title)
        patch.start()
        self.addCleanup(patch.stop)
        # Story work is covered elsewhere; here it only matters whether new work starts.
        self.new_work = []
        poll_once = dd_main.poll_once
        patch = mock.patch.object(dd_main, "poll_once", side_effect=lambda cfg, conn, update=None:
                                  poll_once(cfg, conn, update) if update else self.new_work.append("poll_once"))
        patch.start()
        self.addCleanup(patch.stop)
        for name in ("poll_refreshes", "drain"):
            patch = mock.patch.object(dd_main, name, side_effect=lambda *a, name=name: self.new_work.append(name))
            patch.start()
            self.addCleanup(patch.stop)

    def pr_title(self, repo, number, token):
        self.assertEqual((repo, token), ("owner/repo", GH))
        self.lookups.append(number)
        if number not in self.pr_titles:
            raise github.GitHubError("Not Found")
        return self.pr_titles[number]

    def head(self):
        return git("rev-parse", "HEAD", cwd=self.checkout).strip()

    def merge(self, files, title="Add self-update", message=None):
        """Merge a pull request touching these files into main, the way GitHub words it.

        With message, commit that instead, as a plain commit or a merge whose title isn't on GitHub.
        """
        number = len(self.pr_titles) + 7
        if message is None:
            self.pr_titles[number] = title
            message = f"Merge pull request #{number} from owner/topic"
        seed = self.tmp / "seed"
        git("pull", "--quiet", "origin", "main", cwd=seed)
        for name in files:
            path = seed / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"# {title}\n")
        git("add", "-A", cwd=seed)
        git("-c", "user.name=Main", "-c", "user.email=m@x", "commit", "-q",
            "-m", message, cwd=seed)
        git("push", "-q", "origin", "main", cwd=seed)
        return git("rev-parse", "HEAD", cwd=seed).strip()

    def main_green(self, revision):
        runs.save_main_health(self.conn, "dd", revision=revision, state="green", since=runs.now(),
                              failing=[], broken_by=None, alert_owed=False)

    def cycle(self, scheduler=None):
        return dd_main.cycle(self.cfg, self.conn, scheduler=scheduler, own_checkout=self.checkout)

    def start_run(self, iid="busy"):
        runs.enqueue(self.conn, project="dd", issue={"id": iid, "sequence_id": 9, "name": "Busy"},
                     agent="claude-code", branch="dd-9")
        return runs.claim_next(self.conn)["id"]

    def finish_run(self, run_id):
        runs.close(self.conn, run_id, runs.DONE)
        runs.release_slot(self.conn, run_id)

    def events(self, kind):
        return [e["detail"] for e in runs.events(self.conn, project="dd", issue_id=selfupdate.STORY_ID)
                if e["type"] == kind]

    def public_update(self, revision):
        del self.cfg["self_update"]
        self.cfg["self_update_public_repo"] = "owner/public-dd"
        publicupdate._cached.update(repo=None, after=0, target=None)
        self.clock = self.enterContext(mock.patch.object(publicupdate.time, "monotonic", return_value=1000))
        self.checks = [{"name": name, "head_sha": revision, "app": {"slug": "github-actions"},
                        "status": "completed", "conclusion": "success"}
                       for name in ("Tests", "Lint")]
        def response(request, timeout):
            self.assertNotIn("Authorization", request.headers)
            self.assertEqual(timeout, 15)
            self.assertTrue(request.full_url.startswith("https://api.github.com/repos/owner/public-dd/"))
            data = {"sha": revision} if request.full_url.endswith("commits/main") else {
                "total_count": len(self.checks), "check_runs": self.checks}
            return io.BytesIO(json.dumps(data).encode())
        return self.enterContext(mock.patch.object(publicupdate.urllib.request, "urlopen", side_effect=response))

    def test_public_passed_updates_without_a_dd_project_and_blocks_new_work(self):
        revision = self.merge(["dispatch/worker.py"])
        api = self.public_update(revision)
        del self.cfg["projects"]["dd"]
        self.assertFalse(self.cycle())
        self.assertEqual(self.new_work, [])
        self.assertTrue(self.cycle())
        self.assertEqual(self.head(), revision)
        self.assertEqual(api.call_count, 2)
        self.assertEqual(self.lookups, [])
        selfupdate.announce(self.cfg, self.conn, revision)
        self.assertIsNone(self.sent[-1][0])

    def test_public_checks_wait_unless_all_pass(self):
        revision = self.merge(["dispatch/worker.py"])
        self.public_update(revision)
        for status, conclusion in (("queued", None), ("in_progress", None),
                                   ("completed", "failure"), ("completed", "cancelled"),
                                   ("completed", "skipped"), ("completed", "neutral")):
            with self.subTest(status=status, conclusion=conclusion):
                self.checks[0].update(status=status, conclusion=conclusion)
                self.clock.return_value += 300
                self.assertIsNone(selfupdate.pending(self.cfg, self.conn, self.checkout))
                self.assertEqual(self.head(), self.started)
        self.checks.clear()
        self.clock.return_value += 300
        self.assertIsNone(selfupdate.pending(self.cfg, self.conn, self.checkout))
        warnings = [e for e in runs.events(self.conn, issue_id=selfupdate.STORY_ID)
                    if "GitHub tests did not pass" in e["detail"]]
        self.assertEqual(len(warnings), 1)

    def test_public_renamed_and_added_jobs_allow_update(self):
        revision = self.merge(["main.py"])
        self.public_update(revision)
        self.checks[0]["name"] = "Renamed test job"
        self.checks.append(dict(self.checks[0], name="Added test job"))
        self.assertFalse(self.cycle())
        self.assertTrue(self.cycle())
        self.assertEqual(self.head(), revision)

    def test_public_no_actions_checks_warns_once_per_commit(self):
        revision = self.merge(["main.py"])
        self.public_update(revision)
        self.checks.clear()
        with mock.patch.object(publicupdate, "warn") as warning:
            for _ in range(2):
                self.clock.return_value += 300
                self.assertIsNone(selfupdate.pending(self.cfg, self.conn, self.checkout))
            warning.assert_called_once()
            self.assertIn("no GitHub Actions checks", warning.call_args.args[0])
        warnings = [e for e in runs.events(self.conn, issue_id=selfupdate.STORY_ID)
                    if "no GitHub Actions checks" in e["detail"]]
        self.assertEqual(len(warnings), 1)

    def test_public_wrong_source_waits(self):
        revision = self.merge(["main.py"])
        self.public_update(revision)
        for field, value in (("app", {"slug": "another-app"}), ("head_sha", "0" * 40)):
            checks = self.checks
            self.checks = [dict(check, **{field: value}) for check in checks]
            self.clock.return_value += 300
            self.assertIsNone(selfupdate.pending(self.cfg, self.conn, self.checkout))
            self.checks = checks

    def test_public_other_apps_do_not_block_successful_actions(self):
        revision = self.merge(["main.py"])
        self.public_update(revision)
        self.checks.append(dict(self.checks[0], app={"slug": "another-app"}, conclusion="failure"))
        self.assertIsNotNone(selfupdate.pending(self.cfg, self.conn, self.checkout))

    def test_public_running_work_waits_then_restarts(self):
        revision = self.merge(["README.md"])
        self.public_update(revision)
        run_id = self.start_run()
        self.assertFalse(self.cycle())
        self.assertFalse(self.cycle())
        self.assertEqual(self.head(), self.started)
        self.assertEqual(self.new_work, [])
        self.finish_run(run_id)
        self.assertFalse(self.cycle())
        self.assertTrue(self.cycle())
        self.assertEqual(self.head(), revision)

    def test_public_outage_warns_and_retries_after_interval(self):
        revision = self.merge(["dispatch/worker.py"])
        api = self.public_update(revision)
        response = api.side_effect
        api.side_effect = OSError("GitHub unreachable")
        with mock.patch.object(selfupdate, "warn") as warning:
            self.assertIsNone(selfupdate.pending(self.cfg, self.conn, self.checkout))
            self.assertIsNone(selfupdate.pending(self.cfg, self.conn, self.checkout))
            warning.assert_called_once()
            self.assertIn("will retry", warning.call_args.args[0])
        self.assertEqual(api.call_count, 1)
        api.side_effect = response
        self.clock.return_value += 300
        self.assertIsNotNone(selfupdate.pending(self.cfg, self.conn, self.checkout))
        self.assertEqual(api.call_count, 3)

    def test_public_main_moving_on_waits_for_new_checks(self):
        revision = self.merge(["dispatch/worker.py"])
        self.public_update(revision)
        self.merge(["main.py"])
        self.assertIsNone(selfupdate.pending(self.cfg, self.conn, self.checkout))
        self.assertEqual(self.head(), self.started)

    def test_waiting_comments_survive_polls_and_restart_and_later_update(self):
        self.cfg["projects"]["dd"]["plane_identifier"] = "DD"
        self.add_issue("waiting")
        self.add_issue("also-waiting", seq=8)
        self.add_issue("busy", seq=9)
        self.add_issue("review", seq=10, state=S_REV)
        run_id = self.start_run()
        target = self.merge(["dispatch/worker.py"], "Faster polling")
        self.main_green(target)
        expected = ("DD is updating itself after Faster polling was merged, and starts no new work "
                    "until the running work finishes (DD-9). This story will start automatically "
                    "after DD restarts. Nothing needs doing.")
        with mock.patch.object(plane.Plane, "issues_in_state",
                               autospec=True, wraps=None) as read:
            read.return_value = [i for i in board.issues.values() if i["id"] != "review"]
            self.assertFalse(self.cycle())
            self.assertEqual(read.call_count, 1)
        self.assertEqual({c[1] for c in board.comments}, {"waiting", "also-waiting"})
        for token, iid, body in board.comments:
            self.assertEqual(token, ORCH)
            self.assertIn(expected, body)
            self.assertIn(f"[Dynamic Dispatch self-update {target}]", body)
        self.assertEqual([m for _, m, _ in self.sent], [expected, expected])
        self.assertTrue(all(level is None for _, _, level in self.sent))  # send defaults to passive
        self.assertFalse(self.cycle())
        self.conn.close()
        self.conn = runs.connect(self.cfg["db_path"])
        self.addCleanup(self.conn.close)
        selfupdate._waiting.update(target=None, idle=False)
        self.assertFalse(self.cycle())
        self.assertEqual(len(board.comments), 2)
        later = self.merge(["main.py"], "Better updates")
        self.main_green(later)
        self.assertFalse(self.cycle())
        self.assertEqual(len(board.comments), 4)
        self.assertIn(f"[Dynamic Dispatch self-update {later}]", board.comments[-1][2])
        self.assertEqual(self.new_work, [])
        self.assertEqual(runs.get(self.conn, run_id)["status"], runs.RUNNING)
        self.assertEqual(self.head(), self.started)

    def test_waiting_comment_when_nothing_is_running(self):
        self.add_issue("waiting")
        target = self.merge(["dispatch/worker.py"], "Faster polling")
        self.main_green(target)
        self.assertFalse(self.cycle())
        expected = ("DD is updating itself after Faster polling was merged, and is restarting onto "
                    "the new code in a moment. This story will start automatically after that. "
                    "Nothing needs doing.")
        self.assertEqual(board.comments, [
            (ORCH, "waiting", f"<pre>{expected}\n\n[Dynamic Dispatch self-update {target}]</pre>")])
        self.assertEqual([m for _, m, _ in self.sent], [expected])
        self.assertEqual(self.new_work, [])

    def assert_waiting_comment(self, target, name):
        """Only the merge's name differs in the waiting comment, which stays the same over two polls."""
        self.cfg["projects"]["dd"]["plane_identifier"] = "DD"
        self.add_issue()
        self.start_run()
        self.main_green(target)
        self.assertFalse(self.cycle())
        self.assertFalse(self.cycle())
        self.assertEqual([body for _, _, body in board.comments], [
            f"<pre>DD is updating itself after {name} was merged, and starts no new work until the "
            "running work finishes (DD-9). This story will start automatically after DD restarts. "
            f"Nothing needs doing.\n\n[Dynamic Dispatch self-update {target}]</pre>"])

    def test_waiting_comment_names_the_pull_request_by_its_github_title(self):
        title = "DYNAMICDIS-90: Sprint-pull 7: Start Cycle stories in the approved order"
        self.assert_waiting_comment(self.merge(["dispatch/worker.py"], title), title)
        self.assertEqual(self.lookups, [7])  # looked up once, not on every poll
        self.assertEqual(self.events("warning"), [])

    def test_waiting_comment_falls_back_to_the_pull_request_number(self):
        target = self.merge(["dispatch/worker.py"], message="Merge pull request #85 from owner/topic")
        self.assert_waiting_comment(target, "pull request #85")
        self.assertEqual(self.lookups, [85])
        self.assertEqual(self.events("warning"),
                         ["Could not look up pull request #85's title; using its number: Not Found"])

    def test_waiting_comment_falls_back_to_a_plain_commits_first_line(self):
        target = self.merge(["dispatch/worker.py"], message="Tidy the poller\n\nShorter waits.")
        self.assert_waiting_comment(target, "Tidy the poller")
        self.assertEqual(self.lookups, [])

    def test_waiting_comment_skips_human_only_stories(self):
        self.cfg["projects"]["dd"]["human_only_label_id"] = "lbl-human"
        self.add_issue("human", labels=["lbl-other", "lbl-human"])
        self.add_issue("nested-human", seq=8, labels=[{"id": "lbl-human", "name": "Human"}])
        self.add_issue("waiting", seq=10)
        self.start_run()
        self.main_green(self.merge(["main.py"]))
        self.assertFalse(self.cycle())
        self.assertEqual([c[1] for c in board.comments], ["waiting"])
        self.assertEqual(self.new_work, [])

    def test_waiting_comment_retries_plane_failures(self):
        self.add_issue()
        self.start_run()
        self.main_green(self.merge(["main.py"]))
        with mock.patch.object(plane.Plane, "issues_in_state", side_effect=plane.PlaneError("down")) as read:
            self.assertFalse(self.cycle())
            self.assertEqual(read.call_count, 1)
        self.assertEqual(board.comments, [])
        board.list_status = 503
        self.assertFalse(self.cycle())
        self.assertEqual(board.comments, [])
        board.list_status = None
        board.fail_post = 1
        self.assertFalse(self.cycle())
        self.assertEqual(board.comments, [])
        self.assertFalse(self.cycle())
        self.assertEqual(len(board.comments), 1)
        self.assertFalse(self.cycle())
        self.assertEqual(len(board.comments), 1)
        self.assertEqual(self.new_work, [])

    def test_waiting_comment_lost_reply_does_not_repeat(self):
        self.add_issue()
        self.start_run()
        self.main_green(self.merge(["main.py"]))
        board.lose_post_reply = 1
        self.assertFalse(self.cycle())
        self.assertEqual(len(board.comments), 1)
        self.assertFalse(self.cycle())
        self.assertEqual(len(board.comments), 1)

    def test_without_the_setting_dd_never_updates(self):
        del self.cfg["self_update"]
        self.main_green(self.merge(["dispatch/worker.py"]))
        self.assertFalse(self.cycle())
        self.assertFalse(self.cycle())
        self.assertEqual(self.head(), self.started)
        self.assertEqual(self.new_work.count("poll_once"), 2)

    def test_automatic_rollback_skips_bad_commit_then_returns_to_newer_main(self):
        bad = self.merge(["dispatch/worker.py"], "Bad update")
        gitops.own_checkout(self.checkout, "main", bad)
        gitops.fast_forward_own_checkout(self.checkout, "main", bad)
        git("checkout", "--detach", self.started, cwd=self.checkout)
        runs.save_rollback_state(self.checkout, dict(last_good=self.started,
                                                     rolled_back=self.started, failed=bad))
        self.main_green(bad)
        self.assertIsNone(selfupdate.pending(self.cfg, self.conn, self.checkout))
        self.assertEqual(self.head(), self.started)
        self.assertEqual(self.sent, [])
        fixed = self.merge(["dispatch/worker.py"], "Fixed update")
        self.main_green(fixed)
        update = selfupdate.pending(self.cfg, self.conn, self.checkout)
        self.assertEqual(update["target"], fixed)
        self.assertFalse(selfupdate.when_idle(self.cfg, self.conn, update, busy=False))
        self.assertTrue(selfupdate.when_idle(self.cfg, self.conn, update, busy=False))
        self.assertEqual(self.head(), fixed)
        self.assertEqual(git("branch", "--show-current", cwd=self.checkout).strip(), "main")

    def test_fallback_return_refuses_local_changes_and_diverged_main(self):
        target = self.merge(["dispatch/worker.py"])
        gitops.own_checkout(self.checkout, "main", target)
        git("checkout", "--detach", self.started, cwd=self.checkout)
        tracked = next(path for path in self.checkout.iterdir() if path.is_file())
        original = tracked.read_bytes()
        tracked.write_text("local changes")
        with self.assertRaises(gitops.GitError):
            gitops.resume_own_checkout(self.checkout, "main", target, self.started)
        tracked.write_bytes(original)
        git("checkout", "main", cwd=self.checkout)
        tracked.write_text("local commit")
        git("add", tracked.name, cwd=self.checkout)
        git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "Local",
            cwd=self.checkout)
        git("checkout", "--detach", self.started, cwd=self.checkout)
        with self.assertRaises(gitops.GitError):
            gitops.resume_own_checkout(self.checkout, "main", target, self.started)
        self.assertEqual(self.head(), self.started)

    def test_code_change_waits_for_green_main_then_running_work_then_restarts(self):
        target = self.merge(["dispatch/worker.py"], "Faster polling")
        run_id = self.start_run()
        # Main not yet checked at the new commit: nothing is pending.
        self.main_green(self.started)
        self.assertFalse(self.cycle())
        self.assertIn("poll_once", self.new_work)
        runs.save_main_health(self.conn, "dd", revision=target, state="red", since=runs.now(),
                              failing=["test_x"], broken_by="Faster polling", alert_owed=False)
        self.assertFalse(self.cycle())
        self.assertEqual(self.head(), self.started)

        self.main_green(target)
        self.new_work.clear()
        self.assertFalse(self.cycle())
        self.assertEqual(self.new_work, [])  # no new story run or refresh
        self.assertEqual(self.head(), self.started)
        self.assertEqual(runs.get(self.conn, run_id)["status"], runs.RUNNING)  # left to finish
        self.assertTrue(self.events("self_update_waiting"))

        self.finish_run(run_id)
        self.assertFalse(self.cycle())  # one idle cycle first, so the last alerts go out
        self.assertEqual(self.head(), self.started)
        self.assertTrue(self.cycle())
        self.assertEqual(self.head(), target)
        self.assertEqual(git("rev-parse", "--abbrev-ref", "HEAD", cwd=self.checkout).strip(), "main")
        self.assertEqual(self.new_work, [])
        self.assertEqual(self.sent, [])

        # The restarted copy says so, once and silently.
        selfupdate.announce(self.cfg, self.conn, target)
        selfupdate.announce(self.cfg, self.conn, target)
        self.assertEqual(self.sent, [("dd", f"DD updated to {target[:12]} (Faster polling)", "passive")])

    def test_main_exits_with_the_restart_code(self):
        state = dict(last_good=self.started, failed="b" * 40)
        runs.save_rollback_state(self.checkout, state)
        with mock.patch.object(dd_main.config, "load", return_value=self.cfg), \
                mock.patch.object(dd_main.config, "check_env_files"), \
                mock.patch.object(dd_main.config, "check_groups"), \
                mock.patch.object(dd_main.redact, "configure"), \
                mock.patch.object(dd_main.alerts, "start_health"), \
                mock.patch.object(dd_main.runs, "connect", return_value=self.conn), \
                mock.patch.object(dd_main, "cycle", side_effect=[False, True]), \
                mock.patch.object(dd_main, "checkout", return_value=self.checkout), \
                mock.patch.object(dd_main.time, "sleep"), \
                mock.patch.object(dd_main.time, "time", return_value=1234), \
                mock.patch.object(sys, "argv", ["main.py"]):
            self.assertEqual(dd_main.main(), selfupdate.EXIT_CODE)
        self.assertEqual(runs.rollback_state(self.checkout), dict(state, planned_restart=1234))
        self.assertNotEqual(selfupdate.EXIT_CODE, 0)  # Restart=on-failure restarts only a failed exit

    def test_new_runs_dont_start_while_an_update_is_pending(self):
        target = self.merge(["main.py"])
        self.main_green(target)
        running = self.start_run()
        runs.enqueue(self.conn, project="dd", issue={"id": "waiting", "sequence_id": 10, "name": "Next"},
                     agent="claude-code", branch="dd-10")
        self.finish_run(running)
        scheduler = dd_main.Scheduler(self.cfg)
        self.addCleanup(scheduler.close)
        self.assertFalse(self.cycle(scheduler))
        [queued] = [r for r in self.run_rows() if r["issue_id"] == "waiting"]
        self.assertEqual(queued["status"], runs.QUEUED)  # kept for after the restart
        self.assertTrue(self.cycle(scheduler))
        self.assertEqual(self.head(), target)

    def test_docs_and_tests_only_update_without_restarting(self):
        target = self.merge(["ARCHITECTURE.md", "tests/test_new.py"])
        self.main_green(target)
        self.start_run()
        self.assertFalse(self.cycle())
        self.assertEqual(self.head(), target)  # straight away: running code is unchanged
        self.assertIn("poll_once", self.new_work)
        self.assertTrue(self.events("self_update"))
        self.assertFalse(self.cycle())
        self.assertEqual(self.sent, [])
        self.assertIsNone(runs.claim_self_update_restart(self.conn))

    def test_deploy_change_alerts_once_and_waits_to_be_updated_by_hand(self):
        target = self.merge(["dispatch/proc.py", "deploy/dd-agent-run", "deploy/sudoers-dynamic-dispatch"],
                            "Tighter launcher")
        self.main_green(target)
        for _ in range(3):
            self.assertFalse(self.cycle())
        self.assertEqual(self.head(), self.started)
        self.assertEqual(self.new_work.count("poll_once"), 3)  # stories carry on
        [(project, message, level)] = self.sent
        self.assertEqual((project, level), ("dd", "active"))
        self.assertIn("Tighter launcher", message)
        self.assertIn("deploy/dd-agent-run, deploy/sudoers-dynamic-dispatch", message)
        self.assertIn(f"cd {self.checkout} && git pull", message)
        self.assertIn("2. sudo python3 deploy/dd-install --agents claude", message)
        self.assertNotIn("which DD never installs", message)
        self.assertIn("sudo systemctl restart dynamic-dispatch", message)

    def test_deploy_markdown_updates_without_restarting(self):
        for files in (["deploy/README.md"],
                      ["deploy/README.md", "deploy/notes.md", "deploy/docs/setup.md",
                       "ARCHITECTURE.md", "tests/test_new.py"]):
            with self.subTest(files=files):
                target = self.merge(files, "Clearer steps")
                self.main_green(target)
                self.assertFalse(self.cycle())
                self.assertEqual(self.head(), target)
                self.assertIn("poll_once", self.new_work)
                self.assertTrue(self.events("self_update"))
                self.assertEqual(self.sent, [])
                self.assertIsNone(runs.claim_self_update_restart(self.conn))

    def test_deploy_readme_with_code_updates_and_restarts(self):
        for code in ("dispatch/worker.py", "main.py"):
            with self.subTest(code=code):
                before = self.head()
                target = self.merge(["deploy/README.md", code])
                self.main_green(target)
                self.assertFalse(self.cycle())
                self.assertEqual(self.head(), before)
                self.assertTrue(self.cycle())
                self.assertEqual(self.head(), target)
                self.assertEqual(self.new_work, [])
                self.assertEqual(self.sent, [])
                self.assertEqual(runs.claim_self_update_restart(self.conn)["revision"], target)

    def test_deploy_readme_with_launcher_alerts_only_for_launcher(self):
        target = self.merge(["deploy/README.md", "deploy/dd-install", "deploy/dd-agent-run"])
        self.main_green(target)
        for _ in range(2):
            self.assertFalse(self.cycle())
        self.assertEqual(self.head(), self.started)
        self.assertEqual(self.events("self_update"), [])
        [(_, message, level)] = self.sent
        self.assertEqual(level, "active")
        self.assertIn("it changes deploy/dd-agent-run, which must be installed.", message)
        self.assertNotIn("deploy/README.md", message)
        self.assertIn("2. sudo python3 deploy/dd-install --agents claude", message)
        self.assertIn("sudo systemctl restart dynamic-dispatch", message)

    def test_every_deploy_file_is_classified(self):
        root = Path(__file__).resolve().parents[1]
        # Python caches bytecode beside deploy/dd-install when tests load it; that isn't a deploy file.
        files = {str(p.relative_to(root)) for p in (root / "deploy").rglob("*")
                 if p.is_file() and "__pycache__" not in p.parts}
        self.assertEqual(files, set(selfupdate.DEPLOY_FILES))
        self.assertEqual(set(selfupdate.DEPLOY_FILES.values()),
                         {"installed", "checkout", "documentation"})

    def test_a_deploy_file_this_copy_of_dd_does_not_know_still_holds_back(self):
        target = self.merge(["deploy/new.conf", "deploy/NOTES.md"])
        self.main_green(target)
        self.assertFalse(self.cycle())
        self.assertEqual(self.head(), self.started)
        [(_, message, _)] = self.sent
        self.assertIn("it changes deploy/new.conf, which must be installed.", message)
        self.assertNotIn("NOTES.md", message)

    def test_installer_updates_without_restarting(self):
        target = self.merge(["deploy/dd-install"])
        self.main_green(target)
        self.assertFalse(self.cycle())
        self.assertEqual(self.head(), target)
        self.assertEqual(self.sent, [])

    def test_installer_agents_come_from_accounts_and_are_deduplicated(self):
        self.cfg["agents"].update({"other": {"run_as": "dd-codex"},
                                   "third": {"run_as": "dd-gemini"},
                                   "duplicate": {"run_as": "dd-agent"}})
        target = self.merge(["deploy/dd-agent-run"])
        self.main_green(target)
        self.cycle()
        self.assertEqual(self.head(), self.started)
        message = self.sent[0][1]
        self.assertIn(f"1. cd {self.checkout} && git pull", message)
        self.assertIn("2. sudo python3 deploy/dd-install --agents claude codex gemini\n", message)
        self.assertIn("3. sudo systemctl restart dynamic-dispatch", message)

    def test_unknown_account_uses_manual_steps(self):
        self.cfg["agents"]["claude-code"]["run_as"] = "custom-agent"
        target = self.merge(["deploy/dd-agent-run"])
        self.main_green(target)
        self.cycle()
        self.assertEqual(self.head(), self.started)
        message = self.sent[0][1]
        self.assertIn("deploy/dd-install doesn't know every agent account (custom-agent)", message)
        self.assertIn('"When a deploy file changes" in deploy/README.md by hand', message)
        self.assertNotIn("--agents", message)

    def test_deploy_alert_fits_without_losing_steps(self):
        # Worst-case form escaping: every encrypted byte becomes / in base64.
        class Cipher:
            def encrypt(self, iv, raw, aad):
                self.raw = raw
                return b"\xff" * (len(raw) + 16)

        files = [name for name, kind in selfupdate.DEPLOY_FILES.items() if kind == "installed"]
        update = {"project": "dd", "target": "a" * 40, "head": "b" * 40,
                  "title": "🦆" * selfupdate.TITLE_LIMIT}
        for account in ("dd-agent", "unknown" * 1000):
            with self.subTest(account=account[:20]):
                self.cfg["agents"]["claude-code"]["run_as"] = account
                problem = selfupdate._install_steps(self.cfg, Path("/" + "🦆" * 1000), files)
                update["target"] = ("c" if account == "dd-agent" else "d") * 40
                selfupdate._refuse(self.cfg, self.conn, update, "deploy", problem)
                message = self.sent[-1][1]
                cipher = Cipher()
                group = "dynamic-dispatch update"  # as alerts.Alerts sends a project alert
                form = alerts.encrypted_form(cipher, {"title": group, "body": message, "group": group,
                                                      "level": "active", "url": ""})
                self.assertLessEqual(len(form), alerts.MAX_PAYLOAD)
                self.assertEqual(json.loads(cipher.raw)["body"], message)
                for name in files:
                    self.assertIn(name, message)

    def test_a_failed_fast_forward_is_retried_on_the_next_check(self):
        target = self.merge(["dispatch/worker.py"])
        self.main_green(target)
        real = gitops.fast_forward_own_checkout
        failures = [gitops.GitError("index.lock exists")]

        def flaky(*args):
            if failures:
                raise failures.pop()
            return real(*args)

        with mock.patch.object(gitops, "fast_forward_own_checkout", side_effect=flaky):
            self.assertFalse(self.cycle())  # the idle cycle
            self.assertFalse(self.cycle())  # fails, and leaves the checkout as it was
            self.assertEqual(self.head(), self.started)
            self.assertTrue(any("will retry: index.lock exists" in w for w in self.events("warning")))
            self.assertEqual(self.sent, [])  # a passing failure is no reason to alert
            self.assertTrue(self.cycle())
        self.assertEqual(self.head(), target)

    def test_a_failed_docs_only_fast_forward_is_retried_too(self):
        target = self.merge(["ARCHITECTURE.md"])
        self.main_green(target)
        with mock.patch.object(gitops, "fast_forward_own_checkout", side_effect=gitops.GitError("busy")):
            self.assertFalse(self.cycle())
        self.assertEqual(self.head(), self.started)
        self.assertTrue(self.events("warning"))
        self.assertFalse(self.cycle())
        self.assertEqual(self.head(), target)
        self.assertEqual(self.sent, [])

    def test_local_changes_alert_once_and_dd_carries_on(self):
        target = self.merge(["dispatch/worker.py"])
        self.main_green(target)
        (self.checkout / "app.py").write_text("print('edited by hand')\n")
        for _ in range(3):
            self.assertFalse(self.cycle())
        self.assertEqual(self.head(), self.started)
        self.assertEqual(self.new_work.count("poll_once"), 3)
        [(_, message, level)] = self.sent
        self.assertEqual(level, "active")
        self.assertIn("local changes", message)
        self.assertEqual((self.checkout / "app.py").read_text(), "print('edited by hand')\n")

        # Once the change is gone, the next check updates.
        git("checkout", "--", "app.py", cwd=self.checkout)
        self.assertFalse(self.cycle())
        self.assertTrue(self.cycle())
        self.assertEqual(self.head(), target)

    def test_a_rolled_back_checkout_is_left_alone(self):
        target = self.merge(["dispatch/worker.py"])
        self.main_green(target)
        git("checkout", "-q", "--detach", "HEAD", cwd=self.checkout)
        self.assertFalse(self.cycle())
        self.assertFalse(self.cycle())
        self.assertEqual(self.head(), self.started)
        [(_, message, _)] = self.sent
        self.assertIn("not on main", message)

    def test_a_checkout_that_cant_fast_forward_is_never_merged(self):
        target = self.merge(["dispatch/worker.py"])
        self.main_green(target)
        (self.checkout / "local.txt").write_text("mine\n")
        git("add", "local.txt", cwd=self.checkout)
        git("-c", "user.name=J", "-c", "user.email=j@x", "commit", "-q", "-m", "local", cwd=self.checkout)
        local = self.head()
        self.assertFalse(self.cycle())
        self.assertFalse(self.cycle())
        self.assertEqual(self.head(), local)
        [(_, message, _)] = self.sent
        self.assertIn("can't be fast-forwarded", message)
        self.assertEqual(self.new_work.count("poll_once"), 2)

    def test_main_moved_past_the_green_commit_waits_for_its_check(self):
        green = self.merge(["dispatch/worker.py"])
        self.main_green(green)
        self.merge(["dispatch/choose.py"], "Not yet checked")
        self.assertFalse(self.cycle())
        self.assertFalse(self.cycle())
        self.assertEqual(self.head(), self.started)
        self.assertEqual(self.new_work.count("poll_once"), 2)

    def test_errors_record_a_warning_and_retry_without_blocking_stories(self):
        target = self.merge(["dispatch/worker.py"])
        self.main_green(target)
        git("remote", "set-url", "origin", str(self.tmp / "missing.git"), cwd=self.checkout)
        self.assertFalse(self.cycle())
        self.assertEqual(self.new_work.count("poll_once"), 1)
        self.assertTrue(any("Could not check for an update" in w for w in self.events("warning")))
        self.assertEqual(self.sent, [])

        git("remote", "set-url", "origin", str(self.origin), cwd=self.checkout)
        self.assertFalse(self.cycle())
        self.assertTrue(self.cycle())
        self.assertEqual(self.head(), target)

    def test_an_unknown_project_is_ignored(self):
        self.cfg["self_update"] = "elsewhere"
        self.main_green(self.merge(["dispatch/worker.py"]))
        with mock.patch.object(selfupdate, "warn") as warned:
            selfupdate.check_setting(self.cfg)
        warned.assert_called_once()
        self.assertFalse(self.cycle())
        self.assertEqual(self.head(), self.started)

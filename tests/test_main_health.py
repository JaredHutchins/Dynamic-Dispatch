"""Main checks: testing main itself after every change, and alerting when it fails twice."""

from datetime import datetime, timedelta
import json
import textwrap
from unittest import mock

from dispatch import alerts, choose, gitops, mainhealth, plane, runs, verify, worker
import main as dd_main
from helpers import CODEX, HERE, Base, MainChanges, PY, board, git, sandbox_calls, fake_plane_request

REAL_MAIN_ALERT = alerts.main_health

# Stands in for the verify command. Each call takes the next line of the plan:
# "pass", or "fail:" and the names of the tests that fail.
VERIFY = textwrap.dedent("""\
    import pathlib, sys
    plan = pathlib.Path(sys.argv[1])
    steps = plan.read_text().splitlines()
    step = steps.pop(0) if steps else "pass"
    plan.write_text("\\n".join(steps))
    if step == "pass":
        print("OK")
        sys.exit(0)
    names = step.split(":", 1)[1].split(",")
    for name in names:
        print(f"FAIL: {name} (tests.test_app.App.{name})", file=sys.stderr)
    print(f"FAILED (failures={len(names)})", file=sys.stderr)
    sys.exit(1)
""")


class MainCheck(MainChanges, Base):
    main_checks = True

    def setUp(self):
        super().setUp()
        self.cfg["agents"]["claude-code"]["run_as"] = "dd-claude"
        script = self.tmp / "verify.py"
        script.write_text(VERIFY)
        self.plan = self.tmp / "plan"
        self.plan.write_text("")
        self.cfg["projects"]["dd"]["verify_command"] = [PY, str(script), str(self.plan)]
        self.created_fixes = []
        # Existing health tests keep the created story off the polling board;
        # fix-flow tests publish it explicitly when they want an agent to run.
        self.publish_fixes = False

        def request(client, method, path, payload=None, **kwargs):
            if method == "GET" and "/states/" in path:
                return [{"id": sid, "group": "started"} for sid in self.cfg["projects"]["dd"]["states"].values()] + [
                    {"id": "backlog", "group": "backlog"}, {"id": "done", "group": "completed"}]
            if method == "POST" and path.endswith("/issues/"):
                iid = f"fix-{len(self.created_fixes) + 1}"
                issue = dict(payload, id=iid, sequence_id=100 + len(self.created_fixes),
                             description_stripped="Fix feature.py")
                self.created_fixes.append(issue)
                if self.publish_fixes:
                    board.issues[iid] = issue
                return issue
            if method == "GET" and any(path.endswith(f"/{i['id']}/") for i in self.created_fixes):
                return next(i for i in self.created_fixes if path.endswith(f"/{i['id']}/"))
            for issue in self.created_fixes:
                if path.endswith(f"/{issue['id']}/comments/") and issue["id"] not in board.issues:
                    board.issues[issue["id"]] = issue
                    try:
                        return fake_plane_request(client, method, path, payload, **kwargs)
                    finally:
                        del board.issues[issue["id"]]
            return fake_plane_request(client, method, path, payload, **kwargs)

        patch = mock.patch.object(plane.Plane, "_request", request)
        patch.start()
        self.addCleanup(patch.stop)
        self.alerts = []
        patch = mock.patch.object(alerts, "main_health",
                                  side_effect=lambda cfg, project, message, undelivered: self.alerts.append((project, message)))
        patch.start()
        self.addCleanup(patch.stop)

    def outcomes(self, *steps):
        self.plan.write_text("\n".join(steps))

    def main_runs(self):
        return [r for r in self.run_rows() if r["kind"] == runs.MAIN_CHECK]

    def health(self):
        return runs.main_health(self.conn, "dd")

    def head(self):
        return git("rev-parse", "main", cwd=self.origin).strip()

    def events(self, kind):
        return [e for e in runs.events(self.conn, project="dd") if e["type"] == kind]

    def go_red(self):
        self.outcomes("fail:test_one,test_two", "fail:test_one,test_two")
        self.cycle()
        self.assertEqual(self.health()["state"], "red")

    def test_fix_creation_and_restart(self):
        self.cycle()
        self.advance_main()
        self.go_red()
        [issue] = self.created_fixes
        self.assertEqual(issue["name"], "Fix main: tests failing since Add main behaviour")
        self.assertEqual(issue["state"], self.cfg["projects"]["dd"]["states"]["in_progress"])
        for expected in ("User story", "Acceptance criteria", "In scope", "Out of scope", "Notes",
                         "test_one", "FAILED", self.head(), "/pull/42", "strict mode"):
            self.assertIn(expected, issue["description_html"])
        self.conn.close()
        self.conn = runs.connect(self.cfg["db_path"])
        self.addCleanup(self.conn.close)
        self.assertEqual(self.health()["fix_issue_id"], issue["id"])
        mainhealth.maintain_fix(self.cfg, self.conn, "dd")
        self.assertEqual(len(self.created_fixes), 1)

    def test_only_fix_runs_and_combines_while_red(self):
        self.go_red()
        [issue] = self.created_fixes
        board.issues[issue["id"]] = issue
        self.add_issue()
        dd_main.poll_once(self.cfg, self.conn)
        self.assertEqual(self.stories("i1"), [])
        [fix] = self.stories(issue["id"])
        self.assertIn(fix["agent"], runs.busy_agents(self.conn))
        dd_main.drain(self.cfg, self.conn)
        self.assertEqual(runs.get(self.conn, fix["id"])["status"], runs.DONE)
        self.assertTrue(self.events("combining"))
        self.assertEqual(self.health()["state"], "red")
        self.assertFalse(mainhealth.stopped(self.conn, "dd", issue["id"]))
        self.assertTrue(mainhealth.stopped(self.conn, "dd", "i1"))

    def test_other_story_is_still_held_while_a_fix_story_exists(self):
        self.cycle()
        self.add_issue()
        dd_main.poll_once(self.cfg, self.conn)
        original = worker.agents.run

        def turn_red(*args, **kwargs):
            result = original(*args, **kwargs)
            conn = runs.connect(self.cfg["db_path"], initialize=False)
            try:
                runs.save_main_health(conn, "dd", revision=self.head(), state="red", since=runs.now(),
                                      failing=[], broken_by=None, alert_owed=False)
                runs.save_main_fix(conn, "dd", fix_issue_id="fix-elsewhere", fix_revision=self.head())
            finally:
                conn.close()
            return result

        with mock.patch.object(worker.agents, "run", side_effect=turn_red):
            dd_main.drain(self.cfg, self.conn)
        [held] = self.stories("i1")
        self.assertEqual(held["status"], runs.HELD)
        self.assertEqual(self.events("combining"), [])
        self.assertTrue(mainhealth.stopped(self.conn, "dd", "i1"))
        self.assertFalse(mainhealth.stopped(self.conn, "dd", "fix-elsewhere"))

    def test_red_updates_same_fix_and_green_comments_without_closing(self):
        self.go_red()
        [issue] = self.created_fixes
        board.issues[issue["id"]] = issue
        issue["state"] = self.cfg["projects"]["dd"]["states"]["blocked"]
        self.advance_main()
        self.outcomes("fail:test_new", "fail:test_new")
        self.cycle()
        self.assertEqual(len(self.created_fixes), 1)
        comments = [c[2] for c in board.comments if c[1] == issue["id"]]
        self.assertEqual(len(comments), 1)
        self.assertIn("test_new", comments[0])
        self.assertIn(self.head(), comments[0])
        self.assertTrue(mainhealth.stopped(self.conn, "dd"))
        self.assertEqual(self.stories(issue["id"]), [])
        self.advance_main(conflict=True, message="Fix main manually")
        self.outcomes("pass")
        self.cycle()
        self.assertFalse(mainhealth.stopped(self.conn, "dd"))
        comments = [c[2] for c in board.comments if c[1] == issue["id"]]
        self.assertEqual(len(comments), 2)
        self.assertIn("Main is passing again", comments[-1])
        self.assertEqual(issue["state"], self.cfg["projects"]["dd"]["states"]["blocked"])
        mainhealth.maintain_fix(self.cfg, self.conn, "dd")
        self.assertEqual(len(board.comments), 2)
        (self.tmp / "seed" / "another.py").write_text("BROKEN = True\n")
        self.advance_main(message="Break main again")
        self.go_red()
        self.assertEqual(len(self.created_fixes), 2)
        self.assertNotEqual(self.health()["fix_issue_id"], issue["id"])

    def test_creation_failure_alerts_once_and_retries_after_restart(self):
        with mock.patch.object(plane.Plane, "create_issue", side_effect=plane.PlaneError("offline")), \
                mock.patch.object(alerts, "send") as alert:
            self.go_red()
            mainhealth.maintain_fix(self.cfg, self.conn, "dd")
            self.conn.close()
            self.conn = runs.connect(self.cfg["db_path"])
            self.addCleanup(self.conn.close)
            mainhealth.maintain_fix(self.cfg, self.conn, "dd")
            self.assertIsNone(self.health()["fix_issue_id"])
            self.assertEqual(alert.call_count, 1)
            self.assertEqual(alert.call_args.kwargs["level"], "active")
            self.assertIn("couldn't create", alert.call_args.args[3])
            self.assertTrue(any("Could not create the fix story" in e["detail"] for e in self.events("warning")))
            self.assertEqual(self.main_runs()[-1]["status"], runs.DONE)
        mainhealth.maintain_fix(self.cfg, self.conn, "dd")
        self.assertEqual(len(self.created_fixes), 1)
        self.assertEqual(self.health()["fix_issue_id"], self.created_fixes[0]["id"])

    def test_lost_creation_response_recovers_existing_fix(self):
        self.publish_fixes = True
        original = plane.Plane.create_issue

        def lost(client, *args):
            original(client, *args)
            raise plane.PlaneError("response lost")

        with mock.patch.object(plane.Plane, "create_issue", lost):
            self.go_red()
        self.assertIsNone(self.health()["fix_issue_id"])
        mainhealth.maintain_fix(self.cfg, self.conn, "dd")
        self.assertEqual(len(self.created_fixes), 1)
        self.assertEqual(self.health()["fix_issue_id"], self.created_fixes[0]["id"])
        # Its description already has these failures, so recovering it adds no comment.
        self.assertEqual(board.comments, [])

    def test_creation_marker_write_failure_does_not_fail_main_check(self):
        with mock.patch.object(plane.Plane, "create_issue", side_effect=plane.PlaneError("offline")), \
                mock.patch.object(runs, "save_main_fix", side_effect=RuntimeError("store unavailable")):
            self.go_red()
        self.assertEqual(self.main_runs()[-1]["status"], runs.DONE)
        mainhealth.maintain_fix(self.cfg, self.conn, "dd")
        self.assertEqual(len(self.created_fixes), 1)

    def test_fix_verification_failure_blocks_and_alerts_normally(self):
        self.go_red()
        issue = self.created_fixes[0]
        board.issues[issue["id"]] = issue
        self.outcomes(*(["fail:test_still_broken"] * 10))
        with mock.patch.object(alerts, "send") as alert:
            dd_main.poll_once(self.cfg, self.conn)
            dd_main.drain(self.cfg, self.conn)
        self.assertEqual(plane.state_id(issue), self.cfg["projects"]["dd"]["states"]["blocked"])
        self.assertEqual(self.stories(issue["id"])[-1]["status"], runs.FAILED)
        self.assertTrue(mainhealth.stopped(self.conn, "dd"))
        self.assertTrue(any(call.args[2] == issue["id"] and call.kwargs.get("level") == "active"
                            for call in alert.call_args_list))

    def test_comments_retry_after_lost_response_and_green_failure(self):
        self.go_red()
        issue = self.created_fixes[0]
        issue["state"] = self.cfg["projects"]["dd"]["states"]["blocked"]
        board.issues[issue["id"]] = issue
        original = plane.Plane.comment

        def lost(client, *args, **kwargs):
            original(client, *args, **kwargs)
            raise plane.PlaneError("response lost")

        self.advance_main()
        with mock.patch.object(plane.Plane, "comment", lost):
            self.go_red()
        mainhealth.maintain_fix(self.cfg, self.conn, "dd")
        self.assertEqual(len(board.comments), 1)
        self.advance_main(conflict=True)
        self.outcomes("pass")
        with mock.patch.object(plane.Plane, "comment", side_effect=plane.PlaneError("offline")):
            self.cycle()
        self.assertFalse(mainhealth.stopped(self.conn, "dd"))
        self.assertEqual(self.health()["fix_issue_id"], issue["id"])
        self.conn.close()
        self.conn = runs.connect(self.cfg["db_path"])
        self.addCleanup(self.conn.close)
        mainhealth.maintain_fix(self.cfg, self.conn, "dd")
        self.assertEqual(len(board.comments), 2)
        self.assertIn("Main is passing again", board.comments[-1][2])

    def test_green_comments_on_backlog_fix_but_leaves_closed_fix_alone(self):
        for state, count in (("backlog", 1), ("done", 0)):
            with self.subTest(state=state):
                self.set_health("red")
                mainhealth.maintain_fix(self.cfg, self.conn, "dd")
                issue = self.created_fixes[-1]
                issue["state"] = state
                board.issues[issue["id"]] = issue
                mainhealth._passed(self.conn, "dd", "passing-" + state, self.health())
                mainhealth.maintain_fix(self.cfg, self.conn, "dd")
                comments = [c for c in board.comments if c[1] == issue["id"]]
                self.assertEqual(len(comments), count)
                self.assertEqual(issue["state"], state)

    def test_fix_can_be_handed_over_and_requeued_while_others_cannot(self):
        self.go_red()
        issue = self.created_fixes[0]
        run = {"id": None, "project": "dd", "issue_id": issue["id"], "issue_key": "100",
               "issue_name": issue["name"], "agent": "codex", "branch": "dd-100"}
        for action in (dd_main._hand_over, dd_main._requeue):
            with self.subTest(action=action.__name__):
                action(self.cfg, self.conn, dict(run, issue_id="other"))
                self.assertEqual(self.stories("other"), [])
                action(self.cfg, self.conn, run)
                claimed = runs.claim_next(self.conn)
                self.assertEqual(claimed["issue_id"], issue["id"])
                runs.close(self.conn, claimed["id"], runs.INTERRUPTED)
                runs.release_slot(self.conn, claimed["id"])

    def test_fix_refresh_can_queue_while_red(self):
        self.go_red()
        issue = self.created_fixes[0]
        board.issues[issue["id"]] = issue
        dd_main.poll_once(self.cfg, self.conn)
        dd_main.drain(self.cfg, self.conn)
        self.advance_main()
        dd_main.poll_refreshes(self.cfg, self.conn)
        refreshes = [r for r in self.stories(issue["id"]) if r["kind"] == "refresh"]
        self.assertEqual(len(refreshes), 1)
        self.assertEqual(runs.claim_next(self.conn)["id"], refreshes[0]["id"])

    def test_fix_payload_scrubs_and_caps_output(self):
        with mock.patch.object(plane.Plane, "post", return_value={}) as post:
            client = plane.orchestrator(self.cfg)
            secret = self.cfg["plane"]["orchestrator_token"]
            client.create_issue("project", "Fix " + secret, "<output>" + secret + "x" * 30000, "state")
            payload = post.call_args.args[1]
            self.assertNotIn(secret, str(payload))
            self.assertIn("&lt;output&gt;", payload["description_html"])
            self.assertLess(len(payload["description_html"]), 21000)
            self.assertIn("truncated", payload["description_html"])

    def failure_state(self):
        return runs.main_check_failures(self.conn, "dd")

    def at_retry(self):
        return mock.patch.object(runs.health, "now", return_value=self.failure_state()["retry_at"])

    def test_unavailable_main_alerts_once_slows_retries_and_recovers(self):
        with mock.patch.object(gitops, "main_check_folder", side_effect=gitops.GitError("unavailable")):
            for count in (1, 2):
                self.cycle()
                self.assertEqual(self.failure_state()["count"], count)
                self.assertEqual(self.alerts, [])
            self.cycle()
            self.assertEqual(self.alerts, [("dd", "DD can't test main: could not prepare the folder")])
            deadline = self.failure_state()["retry_at"]
            before = (datetime.fromisoformat(deadline) - timedelta(seconds=1)).isoformat()
            with mock.patch.object(runs.health, "now", return_value=before):
                self.cycle()
            self.assertEqual(len(self.main_runs()), 3)
            with self.at_retry():
                self.cycle()
            self.assertEqual(self.failure_state()["count"], 4)
            self.assertEqual(len(self.alerts), 1)
            self.assertEqual(datetime.fromisoformat(self.failure_state()["retry_at"]) -
                             datetime.fromisoformat(deadline), timedelta(minutes=10))
        with self.at_retry():
            self.cycle()
        self.assertEqual(self.failure_state()["count"], 0)
        self.assertIsNone(self.failure_state()["retry_at"])
        self.assertEqual(self.alerts[-1], ("dd", "DD can test main again"))
        self.cycle()
        self.assertEqual(len(self.alerts), 2)
        self.assertEqual(self.health()["state"], "green")

    def test_github_recovery_rechecks_even_when_main_has_not_moved(self):
        self.cycle()
        with mock.patch.object(mainhealth.github, "main_revision", side_effect=RuntimeError("offline")):
            for _ in range(3):
                mainhealth.poll(self.cfg, self.conn)
        with self.at_retry():
            self.cycle()
        self.assertEqual(len(self.main_runs()), 2)
        self.assertEqual(self.failure_state()["count"], 0)
        self.assertEqual(self.alerts[-1][1], "DD can test main again")

    def test_unavailable_count_survives_restart_and_is_per_project(self):
        with mock.patch.object(mainhealth.github, "main_revision", side_effect=RuntimeError("offline")):
            mainhealth.poll(self.cfg, self.conn)
            mainhealth.poll(self.cfg, self.conn)
            self.conn.close()
            self.conn = runs.connect(self.cfg["db_path"])
            self.addCleanup(self.conn.close)
            self.assertEqual(self.failure_state()["count"], 2)
            self.assertIsNone(runs.main_check_failures(self.conn, "other"))
            mainhealth.poll(self.cfg, self.conn)
            self.assertEqual(len(self.alerts), 1)
            with mock.patch.object(mainhealth.github, "main_revision") as revision:
                mainhealth.poll(self.cfg, self.conn)
                revision.assert_not_called()

    def test_sandbox_refusal_preserves_green_and_red_and_red_result_recovers(self):
        for state in ("green", "red"):
            runs.save_main_health(self.conn, "dd", revision="older", state=state,
                                  since=runs.now(), failing=[], broken_by=None, alert_owed=False)
            with mock.patch.object(mainhealth, "_verify", side_effect=mainhealth.NotTested("refused")):
                self.cycle()
            self.assertEqual(self.health()["state"], state)
            self.assertEqual(self.health()["revision"], "older")
        with mock.patch.object(mainhealth, "_verify", side_effect=mainhealth.NotTested("refused")):
            self.cycle()
        self.outcomes("fail:test_one", "fail:test_one")
        with self.at_retry():
            self.cycle()
        self.assertEqual(self.health()["state"], "red")
        self.assertEqual(self.failure_state()["count"], 0)
        self.assertEqual(self.alerts[-1][1], "DD can test main again")

    def test_availability_alert_delivery_is_retried_and_stale_callbacks_ignored(self):
        callbacks = []
        alerts.main_health.side_effect = lambda cfg, project, message, undelivered: callbacks.append(undelivered)
        with mock.patch.object(mainhealth.github, "main_revision", side_effect=RuntimeError("offline")):
            for _ in range(3):
                mainhealth.poll(self.cfg, self.conn)
        callbacks[0]()
        self.assertEqual(self.failure_state()["alert_owed"], 1)
        mainhealth.poll(self.cfg, self.conn)
        self.assertEqual(len(callbacks), 2)
        with self.at_retry():
            self.cycle()
        callbacks[0]()
        self.assertEqual(self.failure_state()["alert_owed"], 0)
        callbacks[-1]()
        mainhealth.poll(self.cfg, self.conn)
        self.assertEqual(len(callbacks), 4)
        self.assertTrue(any("did not deliver main check availability" in e["detail"]
                            for e in self.events("warning")))

    def test_availability_queue_error_is_owed_and_warns(self):
        alerts.main_health.side_effect = RuntimeError("queue unavailable")
        with mock.patch.object(mainhealth.github, "main_revision", side_effect=RuntimeError("offline")):
            for _ in range(3):
                mainhealth.poll(self.cfg, self.conn)
        self.assertEqual(self.failure_state()["alert_owed"], 1)
        self.assertTrue(any("Could not send main check availability" in e["detail"]
                            for e in self.events("warning")))
        alerts.main_health.side_effect = None
        mainhealth.poll(self.cfg, self.conn)
        self.assertEqual(self.failure_state()["alert_owed"], 0)

    def test_passing_main_changes_nothing(self):
        self.outcomes("pass")
        self.cycle()
        [check] = self.main_runs()
        self.assertEqual((check["status"], check["revision"]), (runs.DONE, self.head()))
        health = self.health()
        self.assertEqual((health["state"], health["revision"], health["alert_owed"]), ("green", self.head(), 0))
        self.assertEqual(self.alerts, [])
        self.assertEqual(self.events("warning"), [])
        self.assertEqual(len(sandbox_calls), 1)
        # Main unchanged: nothing more to check.
        self.cycle()
        self.assertEqual(len(self.main_runs()), 1)
        self.assertEqual(len(sandbox_calls), 1)

    def test_check_uses_an_agent_account_but_never_runs_an_agent(self):
        self.outcomes("pass")
        dd_main.poll_refreshes(self.cfg, self.conn)
        mainhealth.poll(self.cfg, self.conn)
        [check] = self.main_runs()
        self.assertEqual(check["agent"], "claude-code")
        self.assertIn("claude-code", runs.busy_agents(self.conn))  # its slot is taken until the check ends
        self.add_issue()
        dd_main.poll_once(self.cfg, self.conn)
        self.assertEqual(len(self.run_rows()), 1)  # the story waits for the account, as for any run
        dd_main.drain(self.cfg, self.conn)
        [call] = sandbox_calls
        self.assertEqual(call["command"], self.cfg["projects"]["dd"]["verify_command"])
        self.assertEqual(call["run_as"], "dd-claude")
        self.assertIsNone(call["env_file"])  # no model credential: nothing to run the agent with
        folder = self.wt_root / gitops.MAIN_CHECK_FOLDER
        self.assertEqual(call["cwd"], str(folder))
        self.assertEqual(git("rev-parse", "HEAD", cwd=folder).strip(), self.head())
        self.assertEqual([e for e in runs.events(self.conn, run_id=check["id"]) if e["type"] == "agent_started"], [])
        self.assertEqual(runs.busy_agents(self.conn), set())
        self.assertEqual(runs.counts_since(self.conn, "2000-01-01"), {})  # doesn't count as the agent's use

    def test_single_failure_that_passes_on_retry_only_warns(self):
        self.outcomes("fail:test_flaky", "pass")
        self.cycle()
        self.assertEqual(self.health()["state"], "green")
        self.assertEqual(self.alerts, [])
        [warning] = self.events("warning")
        self.assertIn("failed once", warning["detail"])
        self.assertIn("test_flaky", warning["detail"])
        self.assertEqual(len(sandbox_calls), 2)
        self.assertEqual({c["cwd"] for c in sandbox_calls}, {str(self.wt_root / gitops.MAIN_CHECK_FOLDER)})

    def test_two_failures_turn_main_red_with_one_alert(self):
        self.outcomes("pass")
        self.cycle()
        self.advance_main()  # "Merge pull request #42", titled "Add main behaviour" on GitHub
        self.outcomes(*["fail:" + ",".join(f"test_{n}" for n in range(7))] * 2)
        self.cycle()
        health = self.health()
        self.assertEqual((health["state"], health["revision"]), ("red", self.head()))
        self.assertEqual(health["broken_by"], "Add main behaviour")
        self.assertEqual(len(json.loads(health["failing"])), 7)
        [(project, message)] = self.alerts
        self.assertEqual(project, "dd")
        self.assertTrue(message.startswith("Main is failing tests since Add main behaviour"), message)
        self.assertIn("test_0 (tests.test_app.App.test_0)", message)
        self.assertIn("test_4", message)
        self.assertNotIn("test_5", message)
        self.assertIn("and 2 more", message)
        # Still red and unchanged: no second alert, no second check.
        self.cycle()
        self.assertEqual(len(self.alerts), 1)
        self.assertEqual(len(self.main_runs()), 2)

    def test_each_new_commit_is_checked_while_red(self):
        self.go_red()
        since = self.health()["since"]
        self.advance_main(message="Merge pull request #43 from other\n\nTouch main again")
        self.outcomes("fail:test_one", "fail:test_one")
        self.cycle()
        health = self.health()
        self.assertEqual((health["state"], health["revision"], health["since"]), ("red", self.head(), since))
        self.assertEqual(json.loads(health["failing"]), ["test_one (tests.test_app.App.test_one)"])
        self.assertEqual(len(self.main_runs()), 2)
        self.assertEqual(len(self.alerts), 1)

    def test_later_passing_commit_turns_main_green_with_one_alert(self):
        self.go_red()
        self.advance_main()
        self.outcomes("pass")
        self.cycle()
        health = self.health()
        self.assertEqual((health["state"], health["revision"], health["broken_by"]), ("green", self.head(), None))
        self.assertEqual([m for _, m in self.alerts][1:], ["Main is passing again"])
        self.cycle()
        self.assertEqual(len(self.alerts), 2)

    def test_red_survives_a_restart(self):
        self.go_red()
        self.conn.close()
        self.conn = runs.connect(self.cfg["db_path"])
        self.addCleanup(self.conn.close)
        dd_main.recover(self.cfg, self.conn)
        health = self.health()
        self.assertEqual((health["state"], health["revision"]), ("red", self.head()))
        self.assertEqual(len(json.loads(health["failing"])), 2)
        self.cycle()
        self.assertEqual(len(self.alerts), 1)
        self.assertEqual(len(self.main_runs()), 1)

    def test_interrupted_check_is_run_again_not_picked_up_as_a_story(self):
        dd_main.poll_refreshes(self.cfg, self.conn)
        mainhealth.poll(self.cfg, self.conn)
        [check] = self.main_runs()
        runs.claim_next(self.conn)
        with mock.patch.object(dd_main.proc, "reap", return_value=None):
            dd_main.recover(self.cfg, self.conn)
        self.assertEqual(runs.get(self.conn, check["id"])["status"], runs.INTERRUPTED)
        self.assertEqual(len(self.run_rows()), 1)
        self.assertEqual(board.transitions, [])
        self.outcomes("pass")
        self.cycle()
        self.assertEqual([r["status"] for r in self.main_runs()], [runs.INTERRUPTED, runs.DONE])
        self.assertEqual(self.health()["state"], "green")

    def test_reading_main_failing_warns_and_retries(self):
        with mock.patch.object(mainhealth.github, "main_revision", side_effect=mainhealth.github.GitHubError("down")):
            self.cycle()
        self.assertEqual(self.main_runs(), [])
        self.assertTrue(any("Could not queue a main check" in e["detail"] for e in self.events("warning")))
        self.outcomes("pass")
        self.cycle()
        self.assertEqual(self.health()["state"], "green")

    def test_folder_that_cannot_be_prepared_warns_and_retries(self):
        (self.wt_root / gitops.MAIN_CHECK_FOLDER).mkdir()
        self.cycle()
        [check] = self.main_runs()
        self.assertEqual(check["status"], runs.FAILED)
        self.assertIsNone(self.health())
        self.assertTrue(any("Could not test main" in e["detail"] for e in self.events("warning")))
        self.assertEqual(board.transitions, [])
        (self.wt_root / gitops.MAIN_CHECK_FOLDER).rmdir()
        self.outcomes("pass")
        self.cycle()
        self.assertEqual(self.health()["state"], "green")

    def test_sandbox_that_refuses_is_not_a_failing_main(self):
        self.go_red()
        self.advance_main()
        refused = dd_main.proc.Result(None, "", "Could not clear the sandbox user's processes", False, False)
        with mock.patch.object(mainhealth.proc, "run_sandboxed", return_value=refused):
            self.cycle()
        self.assertEqual(self.main_runs()[-1]["status"], runs.FAILED)
        self.assertNotEqual(self.health()["revision"], self.head())  # still unchecked at the new commit
        self.assertTrue(any("Could not test main" in e["detail"] for e in self.events("warning")))
        self.outcomes("pass")
        self.cycle()
        self.assertEqual(self.health()["state"], "green")
        self.assertEqual([m for _, m in self.alerts][1:], ["Main is passing again"])

    def test_alert_that_cannot_be_sent_warns_and_is_retried_once(self):
        alerts.main_health.side_effect = RuntimeError("queue gone")
        self.go_red()
        self.assertEqual(self.health()["alert_owed"], 1)
        self.assertTrue(any("Could not send the main alert" in e["detail"] for e in self.events("warning")))
        alerts.main_health.side_effect = lambda cfg, project, message, undelivered: self.alerts.append((project, message))
        self.cycle()
        self.cycle()
        self.assertEqual(len(self.alerts), 1)
        self.assertTrue(self.alerts[0][1].startswith("Main is failing tests since"))

    def test_alert_bark_does_not_deliver_warns_and_is_retried_once(self):
        self.cfg["alerts"] = dict(bark_url="http://bark.invalid", device_keys=["fake-device-one"],
                                  encryption_key="test-only-key-".ljust(32, "x"))
        alerts.configure(self.cfg)
        if "_alerts" not in self.cfg:
            self.skipTest("optional python3-cryptography is not installed")
        sender = self.cfg["_alerts"]
        attempts = []

        def bark(request, timeout):  # down for the first attempt only
            attempts.append(request)
            if len(attempts) == 1:
                raise OSError("Bark unreachable")
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = b'{"code":200}'
            return response

        with mock.patch.object(alerts, "main_health", REAL_MAIN_ALERT), \
                mock.patch.object(alerts.urllib.request, "urlopen", side_effect=bark), \
                mock.patch.object(alerts, "warn"):
            self.go_red()
            sender.queue.join()
            self.assertEqual(len(attempts), 1)
            self.assertEqual(self.health()["alert_owed"], 1)
            details = [e["detail"] for e in self.events("warning")]
            self.assertTrue(any("Could not send Bark alert" in d for d in details), details)
            self.assertTrue(any("did not deliver the main red alert" in d for d in details), details)
            self.cycle()
            sender.queue.join()
            self.assertEqual(len(attempts), 2)
            self.assertEqual(self.health()["alert_owed"], 0)
            self.cycle()
            sender.queue.join()
            self.assertEqual(len(attempts), 2)  # delivered once, never again

    def test_alert_for_an_old_state_is_not_owed_again(self):
        self.go_red()
        health = self.health()
        runs.save_main_health(self.conn, "dd", revision=health["revision"], state="green", since=health["since"],
                              failing=[], broken_by=None, alert_owed=False)
        runs.owe_main_alert(self.conn, "dd", "red")
        self.assertEqual(self.health()["alert_owed"], 0)

    def test_story_waits_while_main_is_red_and_resumes_when_green(self):
        self.go_red()
        self.add_issue()
        self.cycle()
        self.assertEqual(self.stories("i1"), [])
        self.advance_main()
        self.outcomes("pass")
        self.cycle()
        self.cycle()
        [story] = self.stories("i1")
        self.assertEqual(story["status"], runs.DONE)

    def set_health(self, state):
        runs.save_main_health(self.conn, "dd", revision=self.head(), state=state,
                              since=runs.now(), failing=[], broken_by=None, alert_owed=False)

    def test_running_agent_saves_and_holds_once_then_resumes_after_restart(self):
        self.cycle()
        self.add_issue()
        dd_main.poll_once(self.cfg, self.conn)
        original = worker.agents.run

        def turn_red(*args, **kwargs):
            result = original(*args, **kwargs)
            conn = runs.connect(self.cfg["db_path"], initialize=False)
            try:
                runs.save_main_health(conn, "dd", revision=self.head(), state="red", since=runs.now(),
                                      failing=[], broken_by=None, alert_owed=False)
            finally:
                conn.close()
            return result

        with mock.patch.object(worker.agents, "run", side_effect=turn_red) as agent:
            dd_main.drain(self.cfg, self.conn)
            self.assertEqual(agent.call_count, 1)
        [held] = self.stories("i1")
        self.assertEqual(held["status"], runs.HELD)
        self.assertTrue(held["committed"])
        self.assertEqual(git("status", "--porcelain", cwd=held["worktree"]), "")
        self.assertEqual(board.transitions, [])
        self.assertEqual(self.events("combining"), [])
        self.assertIsNone(held["pr_url"])
        self.conn.close()
        self.conn = runs.connect(self.cfg["db_path"])
        self.addCleanup(self.conn.close)
        dd_main.recover(self.cfg, self.conn)
        self.add_issue("waiting", 2)
        self.cycle()
        self.cycle()
        self.assertEqual(self.stories("waiting"), [])
        self.assertEqual(runs.get(self.conn, held["id"])["status"], runs.HELD)
        comments = [c for c in board.comments if mainhealth.HELD_MESSAGE in c[2]]
        self.assertEqual(len(comments), 1)
        self.advance_main()
        self.outcomes("pass")
        self.cycle()
        with mock.patch.object(worker.agents, "run", wraps=original) as agent:
            self.cycle()
            self.assertEqual(agent.call_count, 0)
        self.assertEqual(runs.get(self.conn, held["id"])["status"], runs.DONE)
        self.cycle()
        self.assertEqual(self.stories("waiting")[-1]["status"], runs.DONE)

    def test_hold_save_and_comment_errors_warn_and_retry_without_blocking(self):
        self.cycle()
        self.add_issue()
        dd_main.poll_once(self.cfg, self.conn)
        run = runs.claim_next(self.conn)
        original = worker.agents.run

        def turn_red(*args, **kwargs):
            result = original(*args, **kwargs)
            self.set_health("red")
            return result

        with mock.patch.object(worker.agents, "run", side_effect=turn_red), \
                mock.patch.object(gitops, "commit_all", side_effect=gitops.GitError("save unavailable")):
            worker.execute(self.cfg, self.conn, run)
        runs.release_slot(self.conn, run["id"])
        self.assertEqual(runs.get(self.conn, run["id"])["status"], runs.HELD)
        self.assertTrue(self.events("warning"))
        with mock.patch.object(mainhealth.sync, "notice", side_effect=RuntimeError("board unavailable")):
            self.cycle()
        self.assertEqual(board.transitions, [])
        self.assertTrue(runs.get(self.conn, run["id"])["committed"])
        self.cycle()
        self.cycle()
        self.assertEqual(len([c for c in board.comments if mainhealth.HELD_MESSAGE in c[2]]), 1)
        self.set_health("green")
        with mock.patch.object(runs, "resume_main_hold", side_effect=RuntimeError("busy store")):
            self.cycle()
        self.assertEqual(runs.get(self.conn, run["id"])["status"], runs.HELD)
        self.cycle()
        self.assertEqual(runs.get(self.conn, run["id"])["status"], runs.DONE)

    def test_main_turning_red_during_verification_holds_instead_of_blocking(self):
        self.cycle()
        self.add_issue()
        dd_main.poll_once(self.cfg, self.conn)
        run = runs.claim_next(self.conn)

        def failed_verify(*args):
            self.set_health("red")
            return False, "FAIL: test_base"

        with mock.patch.object(verify, "run_verify", side_effect=failed_verify):
            worker.execute(self.cfg, self.conn, run)
        runs.release_slot(self.conn, run["id"])
        self.assertEqual(runs.get(self.conn, run["id"])["status"], runs.HELD)
        self.assertEqual(board.transitions, [])
        self.assertEqual(self.events("combining"), [])

    def test_red_after_claim_defers_worker_until_green(self):
        self.cycle()
        self.add_issue()
        dd_main.poll_once(self.cfg, self.conn)
        run = runs.claim_next(self.conn)
        self.set_health("red")
        with mock.patch.object(worker.agents, "run") as agent:
            worker.execute(self.cfg, self.conn, run)
            agent.assert_not_called()
        runs.release_slot(self.conn, run["id"])
        self.assertEqual(runs.get(self.conn, run["id"])["status"], runs.QUEUED)
        self.assertIsNone(runs.claim_next(self.conn))
        self.set_health("green")
        self.cycle()
        self.assertEqual(runs.get(self.conn, run["id"])["status"], runs.DONE)

    def test_red_blocks_refreshes_and_queued_claims_but_not_main_checks(self):
        self.cycle()
        self.add_issue()
        self.cycle()
        self.advance_main()
        self.set_health("red")
        dd_main.poll_refreshes(self.cfg, self.conn)
        self.assertEqual([r for r in self.run_rows() if r["kind"] == "refresh"], [])
        self.add_issue("queued", 2)
        self.set_health("green")
        dd_main.poll_once(self.cfg, self.conn)
        self.set_health("red")
        self.assertIsNone(runs.claim_next(self.conn))
        self.advance_main(conflict=True)
        self.outcomes("pass")
        self.cycle()
        self.assertEqual(self.health()["state"], "green")
        self.cycle()
        self.assertEqual(self.stories("queued")[-1]["status"], runs.DONE)
        self.cycle()
        self.assertTrue(any(r["kind"] == "refresh" for r in self.run_rows()))

    def two_busy_accounts(self, *agents):
        """Codex allowed too, and these accounts building stories already under way."""
        self.cfg["agents"]["codex"] = {
            "display_name": "Codex", "plane_token": CODEX,
            "command": [PY, str(HERE / "fake_agent.py"), "{prompt}", "edit", "codex"],
            "parser": "codex_json", "run_as": "dd-codex",
            "git_author": {"name": "Codex", "email": "codex@example.com"},
            "timeout_seconds": 60}
        project = self.cfg["projects"]["dd"]
        del project["default_agent"]
        project["agents"] = ["claude-code", "codex"]
        busy = {}
        for seq, agent in enumerate(agents, start=10):
            self.add_issue(f"busy-{agent}", seq)
            runs.enqueue(self.conn, project="dd", issue=board.issues[f"busy-{agent}"], agent=agent,
                         branch=f"dd-{seq}")
            busy[agent] = runs.claim_next(self.conn)["id"]
        return busy

    def finish(self, agent, run_id):
        runs.close(self.conn, run_id, runs.DONE)
        runs.release_slot(self.conn, run_id)
        del board.issues[f"busy-{agent}"]

    def stories(self, issue_id):
        return [r for r in self.run_rows() if r["issue_id"] == issue_id]

    def test_waiting_main_check_gets_the_next_free_account_before_new_work(self):
        busy = self.two_busy_accounts("claude-code", "codex")
        self.add_issue()
        self.outcomes("pass")
        self.cycle()
        self.assertEqual((self.main_runs(), self.stories("i1")), ([], []))
        self.assertEqual(len(self.events("main_check_waiting")), 1)
        self.assertIsNone(self.failure_state())
        # An account comes free while the story is polled first: the story leaves it for the main check.
        self.finish("claude-code", busy["claude-code"])
        dd_main.poll_once(self.cfg, self.conn)
        self.assertEqual(self.stories("i1"), [])
        self.cycle()
        [check] = self.main_runs()
        self.assertEqual((check["agent"], check["status"]), ("claude-code", runs.DONE))
        self.assertEqual(self.stories("i1"), [])
        self.assertEqual(self.run_rows()[busy["codex"] - 1]["status"], runs.RUNNING)  # never interrupted
        # Checked: the account goes back to story work.
        self.cycle()
        [story] = self.stories("i1")
        self.assertGreater(story["id"], check["id"])
        self.assertEqual((story["agent"], story["status"]), ("claude-code", runs.DONE))

    def test_main_check_queues_before_new_work_in_the_same_cycle(self):
        self.two_busy_accounts("codex")
        self.add_issue()
        self.outcomes("pass")
        self.cycle()
        [check] = self.main_runs()
        self.assertEqual((check["agent"], check["status"]), ("claude-code", runs.DONE))
        self.assertEqual(self.stories("i1"), [])
        self.cycle()
        [story] = self.stories("i1")
        self.assertEqual(story["agent"], "claude-code")

    def test_main_check_that_cannot_be_queued_holds_no_account(self):
        self.two_busy_accounts("claude-code", "codex")
        self.cycle()
        self.assertEqual(choose._held, {"dd": {"claude-code", "codex"}})
        with mock.patch.object(mainhealth.github, "main_revision", side_effect=mainhealth.github.GitHubError("down")):
            mainhealth.poll(self.cfg, self.conn)
        self.assertEqual(choose._held, {})

    def test_project_without_verify_command_has_no_main_check(self):
        del self.cfg["projects"]["dd"]["verify_command"]
        self.cycle()
        self.assertEqual(self.main_runs(), [])
        self.assertIsNone(self.health())

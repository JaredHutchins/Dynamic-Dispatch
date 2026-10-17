"""Tests for concurrency agents."""

import threading
from concurrent.futures import ThreadPoolExecutor
import json, os, stat, time
from unittest import mock
from pathlib import Path
from datetime import datetime, timedelta, timezone
from dispatch import agents, choose, cleanup, github, gitops, redact, runs, sync, worker
import main as dd_main
from helpers import (
    Base,
    CLAUDE,
    CODEX,
    HERE,
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
    prs,
    sandbox_calls,
)


class TwoAgents(Base):
    """Claude and Codex both allowed; DD chooses which builds each story."""

    def setUp(self):
        super().setUp()
        self.cfg["agents"]["claude-code"]["run_as"] = "dd-agent"
        self.cfg["agents"]["codex"] = {
            "display_name": "Codex", "plane_token": CODEX,
            "command": [PY, str(HERE / "fake_agent.py"), "{prompt}", "edit", "codex"],
            "parser": "codex_json", "run_as": "dd-codex",
            "git_author": {"name": "Codex", "email": "codex@example.com"},
            "timeout_seconds": 60}
        project = self.cfg["projects"]["dd"]
        del project["default_agent"]
        project["agents"] = ["claude-code", "codex"]

    def set_codex_mode(self, mode):
        self.cfg["agents"]["codex"]["command"][3] = mode

    def add_issues(self, n):
        for i in range(n):
            self.add_issue(f"i{i + 1}", seq=7 + i)

    def agents_by_story(self):
        return {r["issue_id"]: r["agent"] for r in self.run_rows()}

    def test_new_stories_are_spread_across_agents(self):
        self.add_issues(3)
        with mock.patch.object(dd_main, "log") as log:
            self.cycle(); self.cycle()
        rows = self.run_rows()
        self.assertEqual([r["status"] for r in rows], [runs.DONE] * 3)
        self.assertEqual(board.user_lookups, [CLAUDE, CODEX])
        # Fewest recent runs first; a tie goes to the first in the list.
        self.assertEqual([r["agent"] for r in rows], ["claude-code", "codex", "claude-code"])
        self.assertTrue(any("queued 8 for Codex" in c.args[0] for c in log.call_args_list))
        # Codex's work carries Codex's identity everywhere, and ran as its own account.
        author = git("log", "-1", "--format=%an <%ae>", "dd-8", cwd=self.origin).strip()
        self.assertEqual(author, "Codex <codex@example.com>")
        self.assertTrue(prs["dd-8"]["body"].endswith("Agent: Codex"))
        self.assertTrue(prs["dd-7"]["body"].endswith("Agent: Claude"))
        self.assertIn((CODEX, "i2", S_REV), board.transitions)
        self.assertEqual([c[0] for c in board.comments if c[1] == "i2"], [CODEX])
        self.assertIn("Changed things", board.comments[[c[1] for c in board.comments].index("i2")][2])
        codex_calls = [c for c in sandbox_calls if c["command"][-1] == "codex" or c["run_as"] == "dd-codex"]
        self.assertEqual({c["run_as"] for c in codex_calls}, {"dd-codex"})
        self.assertEqual(len(codex_calls), 2)  # the agent and its verify
        # Codex has no env_file: it keeps its own login in its home.
        self.assertEqual([c["env_file"] for c in codex_calls], [None, None])
        usage = json.loads(self.run_rows()[1]["usage"])
        self.assertEqual(usage["output_tokens"], 122)

    def test_old_runs_do_not_count(self):
        runs.enqueue(self.conn, project="dd", issue={"id": "old", "sequence_id": 1, "name": "n"},
                     agent="claude-code", branch="dd-1")
        with self.conn:
            self.conn.execute("UPDATE runs SET status = ?, queued_at = ?",
                              (runs.DONE, (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()))
        self.assertEqual(choose.agent_for(self.cfg, self.conn, self.cfg["projects"]["dd"], "new"), "claude-code")
        with self.conn:
            self.conn.execute("UPDATE runs SET queued_at = ?", (runs.now(),))
        self.assertEqual(choose.agent_for(self.cfg, self.conn, self.cfg["projects"]["dd"], "new"), "codex")

    def test_rework_returns_to_its_builder(self):
        self.add_issues(3)
        self.cycle(); self.cycle()  # i1 Claude, i2 Codex, i3 Claude: Claude now has more runs
        board.issues["i1"]["state"] = {"id": S_IP}
        board.issues["i2"]["state"] = {"id": S_IP}
        self.cycle()
        rows = self.run_rows()
        self.assertEqual([(r["issue_id"], r["agent"]) for r in rows[3:]], [("i1", "claude-code"), ("i2", "codex")])
        self.assertEqual([r["status"] for r in rows], [runs.DONE] * 5)

    def test_rework_goes_elsewhere_when_its_builder_is_cooling_down(self):
        self.add_issue(); self.cycle()
        self.assertEqual(self.agents_by_story(), {"i1": "claude-code"})
        choose.cool_down(self.cfg, self.conn, "claude-code")
        board.issues["i1"]["state"] = {"id": S_IP}
        self.cycle()
        self.assertEqual(self.run_rows()[-1]["agent"], "codex")

    def expire_cool_downs(self):
        with self.conn:
            self.conn.execute("UPDATE cooldowns SET until = ?", ("2000-01-01T00:00:00+00:00",))

    def event_types(self, **where):
        return [e["type"] for e in runs.events(self.conn, **where)]

    def test_limit_while_building_switches_to_the_other_agent_with_one_comment(self):
        self.cfg["projects"]["dd"]["agents"] = ["codex", "claude-code"]
        self.set_codex_mode("limit"); self.add_issue()
        before = datetime.now(timezone.utc)
        self.cycle()
        rows = self.run_rows()
        # Handed over in the same cycle, never Blocked.
        self.assertEqual([(r["agent"], r["status"]) for r in rows],
                         [("codex", runs.LIMITED), ("claude-code", runs.DONE)])
        self.assertEqual(board.assignments, [(ORCH, "i1", ["user-codex"]),
                                             (ORCH, "i1", ["user-claude"])])
        self.assertEqual(board.issues["i1"]["assignees"], ["user-claude"])
        self.assertEqual(rows[1]["handed_from"], "codex")
        self.assertEqual(board.issues["i1"]["state"], {"id": S_REV})
        self.assertNotIn((ORCH, "i1", S_BLK), board.transitions)
        switched = [c for c in board.comments if "usage limit" in c[2]]
        self.assertEqual(len(switched), 1)
        self.assertEqual(switched[0][0], ORCH)
        self.assertIn("Codex reached its usage limit partway through this story. "
                      "Claude is picking it up now.", switched[0][2])
        self.assertNotIn("unfinished changes", switched[0][2])
        self.assertIn("usage_limit", self.event_types(run_id=rows[0]["id"]))
        self.assertIn("switched", self.event_types(run_id=rows[1]["id"]))
        # The cool-down starts as before, and is saved, so a restart (a new connection) still sees it.
        conn = runs.connect(self.cfg["db_path"])
        self.addCleanup(conn.close)
        [(agent, until)] = runs.cooldowns(conn, runs.now()).items()
        conn.close()
        self.assertEqual(agent, "codex")
        self.assertAlmostEqual((datetime.fromisoformat(until) - before).total_seconds(), 3600, delta=30)

    def test_switch_keeps_unfinished_changes_on_the_leftover_branch_and_names_it(self):
        self.cfg["projects"]["dd"]["agents"] = ["codex", "claude-code"]
        self.set_codex_mode("limit_partial"); self.add_issue()
        tree = self.wt_root / "dd-7"
        original = Path.stat
        def other_group(path, *args, **kwargs):
            # Codex's folder, as Claude's run finds it: another agent's group.
            st = original(path, *args, **kwargs)
            if path == tree and self.run_rows()[-1]["agent"] == "claude-code":
                values = list(st); values[5] = os.getegid() + 1
                return os.stat_result(values)
            return st
        with mock.patch.object(Path, "stat", other_group):
            self.cycle()
        self.assertEqual([r["status"] for r in self.run_rows()], [runs.LIMITED, runs.DONE])
        leftover = "dd-7-leftover-2"
        self.assertEqual(git("show", f"{leftover}:partial.py", cwd=self.repo), "HALF = 1\n")
        self.assertNotIn(leftover, git("branch", "--list", cwd=self.origin))
        # Handed on to Claude, which built on them.
        self.assertIn("partial.py", git("ls-tree", "--name-only", "dd-7", cwd=self.origin))
        [switched] = [c[2] for c in board.comments if "partway" in c[2]]
        self.assertIn("Claude is picking it up now. Codex&#x27;s unfinished changes are kept on "
                      f"local branch {leftover}.", switched)

    def test_switch_restores_unfinished_changes_into_the_new_folder_and_tells_the_agent(self):
        self.cfg["projects"]["dd"]["agents"] = ["codex", "claude-code"]
        self.set_codex_mode("limit_mixed"); self.set_mode("prompt"); self.add_issue()
        os.environ["LEAK"] = CODEX
        tree = self.wt_root / "dd-7"
        original = Path.stat
        def other_group(path, *args, **kwargs):
            st = original(path, *args, **kwargs)
            if path == tree and self.run_rows()[-1]["agent"] == "claude-code":
                values = list(st); values[5] = os.getegid() + 1
                return os.stat_result(values)
            return st
        restore = gitops.restore_unfinished
        found = {}
        def restore_and_look(*args, **kwargs):
            # What Claude is handed, before it changes anything.
            restore(*args, **kwargs)
            found["status"] = git("status", "--porcelain", "--untracked-files=all", cwd=tree)
            found["modes"] = {name: stat.S_IMODE((tree / name).stat().st_mode) for name in ("partial.py", "app.py")}
        previous = os.umask(0o022)
        try:
            with mock.patch.object(Path, "stat", other_group), \
                    mock.patch.object(gitops, "restore_unfinished", restore_and_look):
                self.cycle()
        finally:
            os.umask(previous)
        rows = self.run_rows()
        self.assertEqual([r["status"] for r in rows], [runs.LIMITED, runs.DONE], rows[-1])
        self.assertEqual(sorted(found["status"].splitlines()),
                         [" D DEFINITION_OF_DONE.md", " M app.py", "?? partial.py"])
        for mode in found["modes"].values():
            self.assertTrue(mode & stat.S_IWGRP)
        prompt = git("show", "dd-7:prompt.txt", cwd=self.origin)
        self.assertIn("Codex worked on this story in an earlier run", prompt)
        self.assertIn("They were never tested.", prompt)
        self.assertIn("if they are sound, build on them; if not, undo them and start over", prompt)
        self.assertIn("- added: partial.py\n", prompt)
        self.assertIn("- changed: app.py\n", prompt)
        self.assertIn("- deleted: DEFINITION_OF_DONE.md\n", prompt)
        self.assertIn("+HALF = 1", prompt)
        self.assertIn("+print('more')", prompt)
        self.assertNotIn(CODEX, prompt)
        self.assertIn("[redacted]", prompt)
        # The leftover branch is kept, and what Claude built on is what was pushed.
        self.assertEqual(git("show", "dd-7-leftover-2:partial.py", cwd=self.repo).splitlines()[0], "HALF = 1")
        pushed = git("ls-tree", "--name-only", "dd-7", cwd=self.origin).split()
        self.assertIn("partial.py", pushed)
        self.assertNotIn("DEFINITION_OF_DONE.md", pushed)
        types = self.event_types(run_id=rows[1]["id"])
        self.assertLess(types.index("unfinished_found"), types.index("unfinished_restored"))
        self.assertLess(types.index("unfinished_restored"), types.index("unfinished_in_prompt"))
        self.assertLess(types.index("unfinished_in_prompt"), types.index("agent_started"))

    def test_limit_with_no_other_agent_waits_with_one_comment_then_continues(self):
        self.cfg["projects"]["dd"]["agents"] = ["claude-code"]
        self.set_mode("limit"); self.add_issue()
        self.cycle()
        until = datetime.fromisoformat(runs.cooldowns(self.conn, runs.now())["claude-code"])
        self.cycle(); self.cycle()
        [row] = self.run_rows()
        self.assertEqual(row["status"], runs.LIMITED)
        self.assertEqual(board.issues["i1"]["state"], {"id": S_IP})
        self.assertEqual(board.assignments, [(ORCH, "i1", ["user-claude"])])
        self.assertEqual(board.issues["i1"]["assignees"], ["user-claude"])
        self.assertEqual(len(board.comments), 1)  # once per wait, not every cycle
        self.assertEqual(board.comments[0][0], ORCH)
        self.assertIn("All agents have reached their usage limits, so this story is waiting in "
                      "In Progress. Nothing needs doing. It will continue automatically at "
                      f"{until.astimezone():%H:%M} (in about 60 minutes), when Claude is available again.",
                      board.comments[0][2])
        self.assertIn(f"Claude is resting after its usage limit until {until.astimezone():%H:%M}.",
                      board.comments[0][2])
        self.assertIn(f"[Dynamic Dispatch run {row['id']} waiting]", board.comments[0][2])
        self.assertIn("waiting", self.event_types(run_id=row["id"]))
        # The first cool-down ends: the story starts by itself.
        self.expire_cool_downs(); self.set_mode("edit")
        self.cycle()
        rows = self.run_rows()
        self.assertEqual([r["status"] for r in rows], [runs.LIMITED, runs.DONE])
        self.assertIsNone(rows[1]["handed_from"])
        self.assertIn("resumed_after_wait", self.event_types(run_id=rows[1]["id"]))
        self.assertIsNone(runs.wait_for(self.conn, "i1"))
        self.assertEqual(len(board.comments), 2)
        self.assertEqual(board.issues["i1"]["state"], {"id": S_REV})

    def test_waiting_comment_is_retried_if_plane_refuses_it(self):
        self.cfg["projects"]["dd"]["agents"] = ["claude-code"]
        self.set_mode("limit"); self.add_issue()
        board.fail_post = 1
        self.cycle()
        self.assertEqual(board.comments, [])
        self.cycle(); self.cycle()
        self.assertEqual(len(board.comments), 1)
        self.assertIn("is waiting in In Progress", board.comments[0][2])

    def test_waiting_story_moved_out_of_in_progress_is_dropped(self):
        self.cfg["projects"]["dd"]["agents"] = ["claude-code"]
        self.set_mode("limit"); self.add_issue()
        self.cycle()
        self.assertEqual(len(board.comments), 1)
        board.issues["i1"]["state"] = {"id": S_BACK}
        self.cycle()
        self.assertIsNone(runs.wait_for(self.conn, "i1"))
        self.assertIn("wait_dropped", self.event_types(issue_id="i1"))
        # No retry once the cool-down ends, and no more comments.
        self.expire_cool_downs(); self.set_mode("edit")
        self.cycle()
        self.assertEqual(len(self.run_rows()), 1)
        self.assertEqual(len(board.comments), 1)
        # Moved back later, it starts as normal.
        board.issues["i1"]["state"] = {"id": S_IP}
        self.cycle()
        rows = self.run_rows()
        self.assertEqual([r["status"] for r in rows], [runs.LIMITED, runs.DONE])
        self.assertNotIn("resumed_after_wait", self.event_types(issue_id="i1"))

    def test_limit_during_repair_switches_and_resumes_the_saved_work(self):
        self.cfg["projects"]["dd"]["agents"] = ["claude-code", "codex"]
        self.set_codex_mode("repair"); self.add_issue()
        original = agents.run
        calls = []
        def agent(cfg, agent_cfg, prompt, worktree, project_cfg=None):
            calls.append(agent_cfg["display_name"])
            result = original(cfg, agent_cfg, prompt, worktree, project_cfg=project_cfg)
            if len(calls) == 1:
                MainChanges.advance_main(self, conflict=True)
                self.set_mode("limit")  # Claude's repair hits its limit
            return result
        with mock.patch.object(agents, "run", side_effect=agent):
            self.cycle()
        rows = self.run_rows()
        self.assertEqual([(r["agent"], r["status"]) for r in rows],
                         [("claude-code", runs.LIMITED), ("codex", runs.DONE)], rows[-1])
        # Codex repaired the clash; the story's work wasn't built again.
        self.assertEqual(calls, ["Claude", "Claude", "Codex"])
        # Claude's committed work resumed rather than being set aside.
        types = self.event_types(run_id=rows[1]["id"])
        self.assertIn("resumed", types)
        self.assertNotIn("set_aside", types)
        self.assertNotIn("set-aside", git("branch", "--list", cwd=self.repo))
        self.assertEqual(git("show", "dd-7:feature.py", cwd=self.origin), "VALUE = 1\nMAIN = 2\n")
        self.assertEqual(git("log", "--format=%an", "main..dd-7", cwd=self.origin).split(),
                         ["Codex", "Claude"])
        self.assertEqual(len([c for c in board.comments if "partway" in c[2]]), 1)
        self.assertEqual(board.issues["i1"]["state"], {"id": S_REV})

    def test_three_limits_in_a_row_block_and_a_normal_finish_resets_the_count(self):
        self.cfg["projects"]["dd"]["agents"] = ["claude-code"]
        self.set_mode("limit"); self.add_issue()
        self.cycle(); self.expire_cool_downs()
        self.cycle(); self.expire_cool_downs()
        self.set_mode("edit"); self.cycle()  # a normal finish
        self.assertEqual([r["status"] for r in self.run_rows()], [runs.LIMITED, runs.LIMITED, runs.DONE])
        board.issues["i1"]["state"] = {"id": S_IP}
        self.set_mode("limit")
        self.cycle(); self.expire_cool_downs()
        self.cycle(); self.expire_cool_downs()
        self.assertEqual(board.issues["i1"]["state"], {"id": S_IP})  # two in a row since
        self.cycle()
        rows = self.run_rows()
        self.assertEqual([r["status"] for r in rows],
                         [runs.LIMITED, runs.LIMITED, runs.DONE, runs.LIMITED, runs.LIMITED, runs.FAILED])
        self.assertEqual(board.issues["i1"]["state"], {"id": S_BLK})
        self.assertIn("This story has been stopped by usage limits 3 times in a row, so it has been "
                      "moved to Blocked. Move it back to In Progress when agents have usage again.",
                      board.comments[-1][2])
        self.assertIn("gave_up", self.event_types(run_id=rows[-1]["id"]))
        self.assertIsNone(runs.wait_for(self.conn, "i1"))
        self.assertEqual(list(runs.cooldowns(self.conn, runs.now())), ["claude-code"])

    def test_claude_usage_limit_is_recognised_too(self):
        self.set_mode("limit"); self.add_issue()
        self.cycle()
        self.assertIn("Claude reached its usage limit", board.comments[0][2])
        self.assertEqual(list(runs.cooldowns(self.conn, runs.now())), ["claude-code"])

    def test_cool_down_length_comes_from_config(self):
        self.cfg["usage_limit_cooldown_minutes"] = 5
        self.cfg["projects"]["dd"]["agents"] = ["claude-code"]
        self.set_mode("limit"); self.add_issue()
        self.cycle()
        self.assertIn("(in about 5 minutes)", board.comments[0][2])

    def test_reset_cooldown_and_fallback(self):
        rid = runs.enqueue(self.conn, project="dd", issue={"id": "limited"}, agent="codex", branch="b")
        now = datetime(2026, 3, 7, 22, tzinfo=timezone.utc)
        for reset, minutes, source in [(now + timedelta(minutes=15), 17, "agent"),
                                       (None, 60, "fallback"),
                                       (now - timedelta(seconds=1), 60, "fallback"),
                                       (now + timedelta(days=7, seconds=1), 60, "fallback"),
                                       (now + timedelta(days=7), 10082, "agent")]:
            with self.subTest(reset=reset):
                until = choose.cool_down(self.cfg, self.conn, "codex", at=now, usage_limit_until=reset, run_id=rid)
                self.assertEqual(until, now + timedelta(minutes=minutes))
                self.assertEqual(runs.cooldowns(self.conn, now.isoformat())["codex"], until.isoformat())
                self.assertIn("Reset time source: " + source, runs.events(self.conn)[-1]["detail"])

    def test_reported_reset_survives_failed_agent_exit(self):
        self.cfg["projects"]["dd"]["agents"] = ["codex"]
        self.set_codex_mode("limit_reset"); self.add_issue()
        now = datetime(2030, 3, 7, 22, tzinfo=timezone.utc)
        with mock.patch.dict(os.environ, {"TZ": "America/Chicago"}), \
                mock.patch.object(agents, "datetime", wraps=datetime) as clock, \
                mock.patch.object(choose, "datetime", wraps=datetime) as cooldown_clock:
            clock.now.return_value = cooldown_clock.now.return_value = now
            self.cycle()
        until = datetime.fromisoformat(runs.cooldowns(self.conn, now.isoformat())["codex"])
        self.assertEqual(until, (now + timedelta(minutes=17)).replace(microsecond=0))
        self.assertTrue(any("Reset time source: agent" in e["detail"] for e in runs.events(self.conn)))

    def test_agent_in_cool_down_is_skipped(self):
        choose.cool_down(self.cfg, self.conn, "claude-code")
        self.add_issues(2)
        self.cycle(); self.cycle()
        self.assertEqual(self.agents_by_story(), {"i1": "codex", "i2": "codex"})
        self.assertEqual([r["status"] for r in self.run_rows()], [runs.DONE] * 2)

    def test_an_expired_cool_down_is_ignored(self):
        choose.cool_down(self.cfg, self.conn, "claude-code", at=datetime.now(timezone.utc) - timedelta(hours=2))
        self.add_issue(); self.cycle()
        self.assertEqual(self.agents_by_story(), {"i1": "claude-code"})

    def test_waiting_story_uses_other_agent_after_usage_limit(self):
        self.set_mode("limit"); self.add_issues(3)
        for _ in range(3):
            self.cycle()
        rows = self.run_rows()
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["status"], runs.LIMITED)
        self.assertEqual({r["issue_id"] for r in rows if r["status"] == runs.DONE}, {"i1", "i2", "i3"})
        self.assertTrue(all(r["agent"] == "codex" for r in rows[1:]))

    def test_all_agents_cooling_down_starts_nothing_and_logs_once(self):
        choose.cool_down(self.cfg, self.conn, "claude-code")
        choose.cool_down(self.cfg, self.conn, "codex")
        self.add_issue()
        with mock.patch.object(dd_main, "log") as log:
            self.cycle(); self.cycle()
        self.assertEqual(self.run_rows(), [])
        self.assertEqual(sandbox_calls, [])
        waiting = [c.args[0] for c in log.call_args_list if "cooling down" in c.args[0]]
        self.assertEqual(len(waiting), 1); self.assertIn("7: agents cooling down", waiting[0])
        # Tried again on later polls: once a cool-down ends, the story starts.
        with self.conn:
            self.conn.execute("UPDATE cooldowns SET until = ? WHERE agent = 'codex'", ("2000-01-01T00:00:00+00:00",))
        self.cycle()
        [row] = self.run_rows()
        self.assertEqual((row["agent"], row["status"]), ("codex", runs.DONE))

    def test_last_queued_run_waits_when_every_agent_hits_the_limit(self):
        self.set_mode("limit"); self.set_codex_mode("limit"); self.add_issues(3)
        with mock.patch.object(dd_main, "log") as log:
            self.cycle(); self.cycle()
        rows = self.run_rows()
        self.assertEqual([(r["issue_id"], r["status"]) for r in rows],
                         [("i1", runs.LIMITED), ("i2", runs.LIMITED)])
        for iid in ("i1", "i2", "i3"):
            self.assertEqual(board.issues[iid]["state"], {"id": S_IP})
            self.assertIsNotNone(runs.wait_for(self.conn, iid))
        self.assertEqual(sorted(c[1] for c in board.comments), ["i1", "i2", "i3"])

    def test_unreadable_codex_output_blocks_the_story(self):
        self.cfg["projects"]["dd"]["agents"] = ["codex"]
        self.set_codex_mode("garbage"); self.add_issue()
        self.cycle()
        [row] = self.run_rows()
        self.assertEqual(row["status"], runs.FAILED)
        self.assertEqual(board.issues["i1"]["state"], {"id": S_BLK})
        self.assertIn("Codex&#x27;s output could not be read. Could not read the output as codex exec --json events",
                      board.comments[0][2])
        self.assertEqual(runs.cooldowns(self.conn, runs.now()), {})

    def test_codex_failure_that_is_not_a_limit_starts_no_cool_down(self):
        self.cfg["projects"]["dd"]["agents"] = ["codex"]
        self.set_codex_mode("fail"); self.add_issue()
        self.cycle()
        self.assertIn("Exited with code 3", board.comments[0][2])
        self.assertEqual(runs.cooldowns(self.conn, runs.now()), {})



class ConcurrentRuns(Base):
    def setUp(self):
        super().setUp()
        self.cfg["agents"]["codex"] = dict(self.cfg["agents"]["claude-code"],
                                             display_name="Codex", run_as="dd-codex")
        self.cfg["projects"]["dd"]["agents"] = ["claude-code", "codex"]
        self.control = self.tmp / "control"
        self.control.mkdir()
        for agent in self.cfg["agents"].values():
            agent["command"] = [PY, str(HERE / "fake_agent.py"), "{prompt}", f"pause:{self.control}"]
        self.scheduler = dd_main.Scheduler(self.cfg)

    def tearDown(self):
        for seq in range(7, 11):
            (self.control / f"dd-{seq}.release").touch()
        self.scheduler.close()
        super().tearDown()

    def tick(self):
        dd_main.cycle(self.cfg, self.conn, "concurrent", self.scheduler)

    def wait_for(self, predicate):
        deadline = time.monotonic() + 10
        while not predicate():
            if time.monotonic() > deadline:
                self.fail("Timed out waiting for worker")
            time.sleep(0.01)

    def test_two_agents_third_waits_heartbeat_and_no_double_booking(self):
        for i in range(3):
            self.add_issue(f"i{i+1}", 7+i)
        self.tick()
        self.wait_for(lambda: all((self.control / f"dd-{n}.started").exists() for n in (7, 8)))
        self.assertEqual([r["status"] for r in self.run_rows()], [runs.RUNNING] * 2)
        self.assertEqual(choose.available(self.cfg, self.conn, list(self.cfg["agents"])), [])
        with mock.patch.object(runs.small_tables, "now", return_value="2030-01-01T00:00:00+00:00"):
            self.tick()
        self.assertEqual(runs.heartbeat(self.conn)["time"], "2030-01-01T00:00:00+00:00")
        self.tick()
        comments = [c for c in board.comments if c[1] == "i3"]
        self.assertEqual(len(comments), 1)
        self.assertIn(sync.busy_message(), comments[0][2])
        self.assertEqual(board.issues["i3"]["state"], {"id": S_IP})
        self.assertFalse((self.control / "dd-9.started").exists())
        first = self.run_rows()[0]
        (self.control / "dd-7.release").touch()
        self.scheduler.pending[first["id"]].result(timeout=10)
        self.tick()
        self.wait_for(lambda: (self.control / "dd-9.started").exists())
        self.assertEqual(self.run_rows()[2]["agent"], first["agent"])
        self.assertEqual(self.run_rows()[1]["status"], runs.RUNNING)

    def test_busy_and_cooling_mix_says_both(self):
        choose.cool_down(self.cfg, self.conn, "codex")
        self.add_issue("i1", 7); self.add_issue("i2", 8)
        self.tick()
        self.wait_for(lambda: (self.control / "dd-7.started").exists())
        self.tick()
        comments = [c for c in board.comments if c[1] == "i2"]
        self.assertEqual(len(comments), 1)
        self.assertIn(sync.busy_message(some_cooling=True), comments[0][2])
        self.assertIn("Claude is working on", comments[0][2])
        self.assertIn("Codex is resting after its usage limit until", comments[0][2])
        self.assertNotIn(sync.busy_message(), comments[0][2])

    def test_refresh_reserves_builder_slot(self):
        self.add_issue(state=S_REV)
        rid = runs.enqueue(self.conn, project="dd", issue=board.issues["i1"], agent="claude-code",
                           branch="dd-7", kind="refresh", pr_url="https://gh/pr/1")
        entered, release = threading.Event(), threading.Event()
        def paused(cfg, conn, row):
            self.assertEqual(row["kind"], "refresh")
            entered.set()
            release.wait(10)
            runs.close(conn, row["id"], runs.DONE)
        with mock.patch.object(worker, "execute", side_effect=paused):
            try:
                self.scheduler.start(self.cfg, self.conn)
                self.assertTrue(entered.wait(5))
                self.assertEqual(choose.available(self.cfg, self.conn, ["claude-code", "codex"]), ["codex"])
                self.add_issue("i2", 8)
                dd_main.poll_once(self.cfg, self.conn)
                self.assertEqual(self.run_rows()[-1]["agent"], "codex")
            finally:
                release.set()
                self.scheduler.pending[rid].result(timeout=10)

    def test_claims_reserve_one_slot_and_restart_clears_both(self):
        for i, agent in enumerate(("claude-code", "claude-code", "codex")):
            self.add_issue(f"i{i}", i)
            issue = board.issues[f"i{i}"]
            runs.enqueue(self.conn, project="dd", issue=issue, agent=agent, branch=f"dd-{i}")
        first, second = runs.claim_next(self.conn), runs.claim_next(self.conn)
        self.assertEqual({first["agent"], second["agent"]}, {"claude-code", "codex"})
        self.assertIsNone(runs.claim_next(self.conn))
        interrupted = runs.interrupt_running(self.conn)
        self.assertEqual(len(interrupted), 2)
        self.assertTrue(all(r["status"] == runs.INTERRUPTED for r in interrupted))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM agent_slots").fetchone()[0], 0)
        self.assertEqual(sandbox_calls, [])

    def test_shared_git_operations_never_overlap(self):
        original = gitops._git
        active = 0
        maximum = 0
        unlocked = []
        guard = threading.Lock()
        def checked(*args, **kwargs):
            nonlocal active, maximum
            with guard:
                # Catch a missing lock even when scheduling hides the overlap.
                if not gitops.repository_lock(self.repo)._is_owned():
                    unlocked.append(args[0])
                active += 1
                maximum = max(maximum, active)
            try:
                time.sleep(0.002)
                return original(*args, **kwargs)
            finally:
                with guard:
                    active -= 1
        self.add_issue(); self.add_issue("i2", 8)
        with mock.patch.object(gitops, "_git", side_effect=checked):
            self.tick()
            self.wait_for(lambda: all((self.control / f"dd-{n}.started").exists() for n in (7, 8)))
            for n in (7, 8):
                (self.control / f"dd-{n}.release").touch()
            for future in self.scheduler.pending.values():
                future.result(timeout=10)
        self.assertEqual(unlocked, [])
        self.assertEqual(maximum, 1)
        self.assertEqual([r["status"] for r in self.run_rows()], [runs.DONE] * 2)

    def test_refresh_waits_for_busy_builder_even_when_other_agent_is_free(self):
        self.add_issue(state=S_REV)
        previous = runs.enqueue(self.conn, project="dd", issue=board.issues["i1"],
                                agent="claude-code", branch="dd-7", pr_url="https://gh/pr/1")
        runs.close(self.conn, previous, runs.DONE)
        self.add_issue("i2", 8)
        active = runs.enqueue(self.conn, project="dd", issue=board.issues["i2"],
                              agent="claude-code", branch="dd-8")
        runs.claim_next(self.conn)
        with mock.patch.object(github, "pr_is_open", return_value=True), \
                mock.patch.object(github, "main_revision", return_value="new-main"), \
                mock.patch.object(gitops, "branch_contains_main", return_value=False):
            dd_main.poll_refreshes(self.cfg, self.conn)
            self.assertEqual(len(self.run_rows()), 2)
            runs.close(self.conn, active, runs.DONE)
            # Terminal status alone must not release an account doing final cleanup.
            self.assertEqual(choose.available(self.cfg, self.conn, ["claude-code", "codex"]), ["codex"])
            runs.release_slot(self.conn, active)
            dd_main.poll_refreshes(self.cfg, self.conn)
        self.assertEqual((self.run_rows()[-1]["kind"], self.run_rows()[-1]["agent"]),
                         ("refresh", "claude-code"))

    def test_cleanup_skips_active_story_even_with_different_branch(self):
        self.add_issue()
        old = runs.enqueue(self.conn, project="dd", issue=board.issues["i1"], agent="claude-code", branch="old")
        runs.await_blocked(self.conn, old, "failed", str(self.wt_root / "old"))
        runs.close(self.conn, old, runs.FAILED)
        with self.conn:
            self.conn.execute("UPDATE runs SET finished_at = '2000-01-01' WHERE id = ?", (old,))
        runs.enqueue(self.conn, project="dd", issue=board.issues["i1"], agent="codex", branch="new")
        runs.claim_next(self.conn)
        self.cfg["failed_worktree_days"] = 0
        with mock.patch.object(cleanup, "_remove") as remove:
            cleanup.failed_worktrees(self.cfg, self.conn)
        remove.assert_not_called()
        self.assertIsNotNone(runs.get(self.conn, old)["worktree"])

    def test_app_token_refresh_is_shared_between_threads(self):
        settings = {"app_id": "fake-concurrent-app", "installation_id": "fake-installation",
                    "private_key_path": "/fake/key"}
        secret = "fake-concurrent-installation-token"
        barrier = threading.Barrier(2)
        def obtain(_):
            barrier.wait(timeout=5)
            return github.token(settings)
        with mock.patch.object(github, "_app_jwt", return_value="fake-jwt"), \
                mock.patch.object(github, "_request", return_value={
                    "token": secret, "expires_at": "2099-01-01T00:00:00Z"}) as request:
            with ThreadPoolExecutor(2) as pool:
                self.assertEqual(list(pool.map(obtain, range(2))), [secret, secret])
        request.assert_called_once()
        self.assertEqual(redact.scrub(secret), "[redacted]")

    def test_concurrent_secrets_are_all_scrubbed(self):
        barrier = threading.Barrier(2)
        secrets = ["fake-concurrent-secret-A", "fake-concurrent-secret-B"]
        def register(secret):
            barrier.wait(timeout=5)
            redact.add_secret(secret)
            barrier.wait(timeout=5)
            return redact.scrub(" ".join(secrets))
        original = redact.set_secrets
        def delayed(values):
            # Widen the lost-update window if add_secret does not hold its lock.
            time.sleep(0.02)
            original(values)
        with mock.patch.object(redact, "set_secrets", side_effect=delayed):
            with ThreadPoolExecutor(2) as pool:
                results = list(pool.map(register, secrets))
        self.assertEqual(results, ["[redacted] [redacted]"] * 2)


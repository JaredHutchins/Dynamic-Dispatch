"""Waiting comments describe accounts without changing their availability."""

import os
import time
import unittest
from datetime import datetime, timezone
from unittest import mock

from dispatch import choose, runs, sync


class WaitingAgents(unittest.TestCase):
    def setUp(self):
        self.conn = runs.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.cfg = {"agents": {name: {"display_name": name.title()}
                               for name in ("builder", "refresher", "tester", "resting", "held", "free")},
                    "projects": {"dd": {"plane_identifier": "DYNAMICDIS", "agents":
                                         ["builder", "refresher", "tester", "resting", "held", "free"]}}}
        self.addCleanup(choose.hold_for_main_check, "waiting-test", [])

    def enqueue(self, agent, **kwargs):
        return runs.enqueue(self.conn, project="dd", agent=agent, branch="test",
                            issue={"id": agent, "sequence_id": 69, "name": "Show account status"}, **kwargs)

    def test_all_statuses_in_project_order_and_local_time(self):
        self.enqueue("builder")
        self.enqueue("refresher", kind="refresh", pr_url="https://example.com/pull/42")
        runs.enqueue_main_check(self.conn, project="dd", agent="tester", base="main", revision="abcdef")
        runs.save_cooldown(self.conn, "resting", "2099-01-01T18:30:00+00:00")
        choose.hold_for_main_check("waiting-test", ["held"])
        before = choose.available(self.cfg, self.conn, self.cfg["projects"]["dd"]["agents"])
        try:
            with mock.patch.dict(os.environ, {"TZ": "EST5"}), \
                    mock.patch.object(sync, "datetime", wraps=datetime) as now:
                now.now.return_value = datetime(2099, 1, 1, 12, tzinfo=timezone.utc)
                time.tzset()
                message = sync.waiting_agents_message(self.cfg, self.conn, "dd")
        finally:
            time.tzset()
        self.assertEqual(message.splitlines(), [
            "Builder is working on DYNAMICDIS-69: Show account status.",
            "Refresher is refreshing pull request #42.",
            "Tester is testing main.",
            "Resting is resting after its usage limit until 13:30.",
            "Held is free; held for a main check waiting for an account.",
            "Free is free."])
        self.assertEqual(choose.available(self.cfg, self.conn, self.cfg["projects"]["dd"]["agents"]), before)

    def test_rest_on_another_local_day_includes_day(self):
        self.cfg["projects"]["dd"]["agents"] = ["resting"]
        runs.save_cooldown(self.conn, "resting", "2099-01-02T18:30:00+00:00")
        try:
            with mock.patch.dict(os.environ, {"TZ": "EST5"}), \
                    mock.patch.object(sync, "datetime", wraps=datetime) as now:
                # UTC is already Jan 2, but locally it is still Jan 1.
                now.now.return_value = datetime(2099, 1, 2, 1, tzinfo=timezone.utc)
                time.tzset()
                message = sync.waiting_agents_message(self.cfg, self.conn, "dd")
        finally:
            time.tzset()
        self.assertEqual(message, "Resting is resting after its usage limit until Fri 13:30.")

    def test_removed_project_has_no_agent_lines(self):
        del self.cfg["projects"]["dd"]
        self.assertEqual(sync.waiting_agents_message(self.cfg, self.conn, "dd"), "")

    def test_unconfigured_agent_explains_why_it_cannot_take_story(self):
        self.cfg["projects"]["dd"]["agents"] = ["missing"]
        self.assertEqual(sync.waiting_agents_message(self.cfg, self.conn, "dd"),
                         "missing is free; cannot take this story because it is not configured.")

    def test_claimed_slot_takes_precedence_and_work_matches_busy_accounts(self):
        queued = self.enqueue("builder")
        claimed = runs.claim_next(self.conn)
        self.assertEqual(claimed["id"], queued)
        runs.close(self.conn, queued, runs.DONE)
        other = runs.enqueue(self.conn, project="dd", agent="builder", branch="other",
                             issue={"id": "other", "sequence_id": 70, "name": "Next story"})
        self.assertEqual(runs.agent_work(self.conn)["builder"]["id"], queued)
        self.assertEqual(set(runs.agent_work(self.conn)), runs.busy_agents(self.conn))
        runs.release_slot(self.conn, queued)
        self.assertEqual(runs.agent_work(self.conn)["builder"]["id"], other)

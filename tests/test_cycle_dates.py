"""Exact Cycle boundaries and local dates, independent of the test host's zone."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from dispatch import cycle_dates, cycles, plan_report, plans, prompts, runs, sprint
import test_plan_report
from test_sprint import SprintBoard


class Dates(unittest.TestCase):
    def zone(self, zone):
        original = os.environ.get('TZ')
        def restore():
            if original is None:
                os.environ.pop('TZ', None)
            else:
                os.environ['TZ'] = original
            time.tzset()
        self.addCleanup(restore)
        os.environ['TZ'] = zone
        time.tzset()

    def test_timestamp_and_plain_date_boundaries_in_both_zones(self):
        for zone, start, end in (
                ('America/Chicago', '2026-10-08T05:00:00Z', '2026-10-09T04:59:00Z'),
                ('Etc/GMT-2', '2026-10-07T22:00:00Z', '2026-10-08T21:59:00Z')):
            with self.subTest(zone=zone):
                self.zone(zone)
                self.assertEqual(cycle_dates.moment(start).hour, 0)
                self.assertEqual(cycle_dates.local_date(start), '2026-10-08')
                self.assertEqual(cycle_dates.local_date(end), '2026-10-08')
                self.assertEqual(cycle_dates.moment(start), cycle_dates.moment('2026-10-08'))
                plain_end = cycle_dates.moment('2026-10-08', end=True)
                self.assertEqual((plain_end.hour, plain_end.minute, plain_end.second, plain_end.microsecond),
                                 (23, 59, 59, 999999))
                prompt = prompts.assessment_prompt({'revision': 'rev', 'end_date': end, 'stories': '[]'})
                self.assertIn('Cycle end date: 2026-10-08', prompt)

    def test_early_end_uses_target_days_zone_and_clamps_start(self):
        self.zone('America/Chicago')
        for day, start, expected in (
                ('2026-10-14', '2026-10-01', '2026-10-14T04:59:59.999999+00:00'),
                ('2026-10-14', '2026-10-14T05:00:00Z', '2026-10-15T04:59:59.999999+00:00'),
                ('2026-03-08', '2026-03-01', '2026-03-08T05:59:59.999999+00:00')):
            with self.subTest(day=day, start=start):
                now = datetime.fromisoformat(day + 'T12:00:00').astimezone()
                self.assertEqual(cycle_dates.early_end(start, now), expected)

    def test_plain_dates_use_the_offset_of_the_target_day(self):
        self.zone('America/Chicago')
        self.assertEqual(cycle_dates.moment('2026-01-08').utcoffset(), timedelta(hours=-6))
        self.assertEqual(cycle_dates.moment('2026-07-08').utcoffset(), timedelta(hours=-5))

    def test_future_cycle_and_same_day_exact_finish(self):
        self.zone('America/Chicago')
        merged = test_plan_report.Estimate().merged()
        now = datetime(2026, 10, 1, tzinfo=timezone.utc)
        end = '2026-10-09T04:59:00Z'
        start = '2026-10-08T05:00:00Z'
        estimate = plan_report.estimate(merged, [8 * 3600], 1, end, now, start)
        self.assertFalse(estimate['fits'])
        self.assertTrue(estimate['push_fits'])
        self.assertEqual(estimate['end_date'], '2026-10-08')
        self.assertEqual(estimate['finish'], '2026-10-09')
        self.assertTrue(plan_report.estimate(merged, [8 * 3600], 1, end, now)['fits'])
        # The finish is on the end's local day, but one minute too late.
        later = cycle_dates.moment('2026-10-08T06:00:00Z')
        estimate = plan_report.estimate(merged, [3600], 1, '2026-10-08T08:59:00Z', later, start)
        self.assertFalse(estimate['fits'])
        self.assertEqual(estimate['finish'], estimate['end_date'])

    def test_report_uses_the_persisted_cycle_start_after_restart(self):
        self.zone('America/Chicago')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'runs.db'
            conn = runs.connect(path)
            try:
                snapshot = runs.save_assessment_snapshot(
                    conn, 'p', 'c', 'plan', 'rev', test_plan_report.STORIES,
                    '2026-10-09T04:59:00Z', ['agent'], start_date='2026-10-08T05:00:00Z')
            finally:
                conn.close()

            conn = runs.connect(path)
            try:
                snapshot = runs.assessment_snapshot(conn, 'p', 'c')
                rows = [test_plan_report.story(sid) for sid in 'abc']
                items = [{'agent': 'agent', 'answer': json.dumps(test_plan_report.answer(rows, list('abc')))}]
                with mock.patch.object(runs, 'done_durations', return_value=[8 * 3600]), \
                        mock.patch.object(plans, 'datetime') as clock:
                    clock.now.return_value = datetime(2026, 10, 1, tzinfo=timezone.utc)
                    report = plans.make_report(conn, 'p', snapshot, items, {})
                estimate = json.loads(report['merged'])['estimate']
                self.assertFalse(estimate['fits'])
                self.assertIn("Cycle's end date, 2026-10-08", report['report'])
            finally:
                conn.close()

    def test_existing_snapshot_migrates_without_inventing_a_start(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'runs.db'
            conn = runs.connect(path)
            try:
                runs.save_assessment_snapshot(conn, 'p', 'c', 'plan', 'rev', [], '2026-10-08', ['agent'])
                with conn:
                    conn.execute('ALTER TABLE assessment_snapshots DROP COLUMN start_date')
            finally:
                conn.close()
            conn = runs.connect(path)
            try:
                snapshot = runs.assessment_snapshot(conn, 'p', 'c')
                self.assertIsNone(snapshot['start_date'])
                self.assertEqual(snapshot['end_date'], '2026-10-08')
            finally:
                conn.close()


class Timing(SprintBoard):
    def test_start_end_and_carry_over_at_exact_moments(self):
        self.approve()
        cycle = self.client.cycles.return_value[0]
        self.client.set_labels.side_effect = lambda p, i, labels: self.items[i].update(labels=labels)
        for cycle_id, start, end in (('central', '2026-10-08T05:00:00Z', '2026-10-09T04:59:00Z'),
                                     ('ahead', '2026-10-07T22:00:00Z', '2026-10-08T21:59:00Z')):
            with self.subTest(start=start):
                cycle.update(id=cycle_id, start_date=start, end_date=end)
                self.members[cycle_id] = list(self.items)
                runs.save_cycle_plan(self.conn, 'project', cycle_id, 'plan')
                self.approve(cycle_id)
                begin, finish = cycle_dates.moment(start), cycle_dates.moment(end)
                for instant, starts in ((begin - timedelta(microseconds=1), False), (begin, True),
                                        (finish, True), (finish + timedelta(microseconds=1), False)):
                    sprint.datetime.now.return_value = instant
                    self.assertEqual(bool(list(sprint.approved_cycles(
                        self.conn, self.client, 'project', self.project))), instant >= begin)
                    with mock.patch.object(sprint, 'start_order') as order:
                        sprint.start_ready(self.cfg, self.conn, self.client, 'project', self.project)
                        self.assertEqual(order.called, starts)
                for item in self.items.values():
                    item.update(state='sprint', labels=[])
                    runs.remember_cycle_member(self.conn, 'project', cycle_id, item['id'])
                with mock.patch.object(plans, 'plan_cycles'):
                    self.today.now.return_value = finish
                    cycles.reconcile(self.conn, self.client, 'project', self.project)
                    self.assertTrue(all(i['state'] == 'sprint' for i in self.items.values()))
                    self.today.now.return_value = finish + timedelta(microseconds=1)
                    cycles.reconcile(self.conn, self.client, 'project', self.project)
                    self.assertTrue(all(i['state'] == 'product' for i in self.items.values()))
                    self.assertEqual(self.members[cycle_id], [])

    def test_early_completion_waits_until_after_the_exact_end(self):
        self.approve()
        self.client.cycles.return_value[0]['end_date'] = '2026-10-09T04:59:00Z'
        for item in self.items.values():
            item['state'] = 'done'
        end = cycle_dates.moment('2026-10-09T04:59:00Z')
        sprint.datetime.now.return_value = end
        sprint.active_cycle(self.conn, self.client, 'project', self.project)
        self.assertFalse(runs.cycle_complete(self.conn, 'project', 'c'))
        sprint.datetime.now.return_value = end + timedelta(microseconds=1)
        sprint.active_cycle(self.conn, self.client, 'project', self.project)
        self.assertTrue(runs.cycle_complete(self.conn, 'project', 'c'))

"""Approved sprint starts against a fake board and restartable store."""

from datetime import datetime, timedelta, timezone
from unittest import mock

import main as dd_main
from dispatch import alerts, choose, config, mainhealth, plane, plans, runs, sprint, stories, sync
from dispatch.runs import sprint as sprint_store
from helpers import Base, MainChanges
from test_cycles import Board


class SprintBoard(Board):
    """The fake board with an approvable three-story Cycle."""

    def setUp(self):
        super().setUp()
        self.project.update(agents=['alpha', 'beta'])
        self.project['states'].update(in_progress='progress', in_review='review', blocked='blocked')
        self.project['cycles']['blocked_reminder_minutes'] = 60
        self.cfg['agents'] = {a: {'display_name': a} for a in ('alpha', 'beta')}
        self.enterContext(mock.patch.object(sprint, 'datetime', wraps=datetime)).now.return_value = datetime(2026, 10, 6).astimezone()
        self.items = {i: {'id': i, 'sequence_id': n, 'state': 'sprint'} for n, i in enumerate(('a', 'b', 'c'))}
        self.members['c'] = list(self.items)
        runs.save_cycle_plan(self.conn, 'project', 'c', 'plan')
        self.rules = {i: {'depends_on': [], 'overlaps': []} for i in self.items}
        self.order = ['b', 'a', 'c']

    def approve(self, cycle_id='c'):
        snapshot = runs.save_assessment_snapshot(self.conn, 'project', cycle_id, 'plan', 'revision',
                                                  [], '2026-10-14', ['alpha', 'beta'], members=list(self.items))
        runs.save_plan_report(self.conn, snapshot['id'], {'merged': {
            'order': self.order, 'stories': self.rules, 'loops': []}}, 'Approved report')
        runs.save_plan_approval(self.conn, 'project', cycle_id, {}, list(self.items))
        runs.set_plan_status(self.conn, 'project', cycle_id, runs.PLAN_APPROVED)

    def start(self):
        sprint.poll(self.cfg, self.conn)

    def moved(self):
        return [call.args[1] for call in self.client.set_state.call_args_list]


class Starts(SprintBoard):
    def test_cycle_and_normal_queue_use_the_same_story_branch(self):
        self.order = ['b']
        self.approve()
        self.client.issues_in_state.side_effect = lambda p, s: [
            dict(i) for i in self.items.values() if i['state'] == s]
        for prefix, expected in ((None, 'dd-1'), ('feature', 'feature-1')):
            with self.subTest(prefix=prefix):
                if prefix is not None:
                    self.project['branch_prefix'] = prefix
                self.items['b']['state'] = 'sprint'
                with mock.patch.object(runs, 'enqueue', return_value=1) as enqueue, \
                        mock.patch.object(stories, 'branch_name', wraps=stories.branch_name) as branch:
                    self.start()
                    dd_main.poll_once(self.cfg, self.conn)
                self.assertEqual([call.kwargs['branch'] for call in enqueue.call_args_list],
                                 [expected, expected])
                self.assertEqual(branch.call_args_list,
                                 [mock.call(self.project, self.items['b'])] * 2)

    def test_order_capacity_events_and_restart(self):
        self.approve()
        self.start()
        self.assertEqual(self.moved(), ['b', 'a'])
        self.assertEqual(runs.busy_agents(self.conn), {'alpha', 'beta'})
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events WHERE type = 'sprint_start'").fetchone()[0], 2)
        self.conn.close()
        self.conn = runs.connect(self.path)
        self.start()
        self.assertEqual(self.moved(), ['b', 'a'])

    def test_stories_start_on_the_end_date(self):
        self.approve()
        self.today.now.return_value = sprint.datetime.now.return_value = datetime(2026, 10, 14).astimezone()
        self.poll()
        self.start()
        self.assertEqual(self.moved(), ['b', 'a'])
        self.assertEqual(runs.cycle_plan(self.conn, 'project', 'c')['status'], runs.PLAN_APPROVED)
        self.client.create_issue.assert_not_called()
        self.assessments.assert_not_called()

    def test_completed_cycles_cost_no_membership_requests_even_after_restart(self):
        self.today.now.return_value = sprint.datetime.now.return_value = datetime(2026, 10, 15).astimezone()
        self.approve()
        for issue in self.items.values():
            issue['state'] = 'done'
        self.start()
        self.assertTrue(runs.cycle_complete(self.conn, 'project', 'c'))
        for n in range(5):
            cycle_id = f'finished{n}'
            self.client.cycles.return_value.append(dict(self.client.cycles.return_value[0], id=cycle_id))
            runs.save_cycle_plan(self.conn, 'project', cycle_id, f'plan{n}')
            self.approve(cycle_id)
            self.members[cycle_id] = []
            self.poll()
            self.start()
            self.assertTrue(runs.cycle_complete(self.conn, 'project', cycle_id))
            self.conn.close()
            self.conn = runs.connect(self.path)
            self.client.reset_mock()
            self.poll()
            self.start()
            self.assertIsNone(sprint.active_cycle(self.conn, self.client, 'project', self.project))
            self.client.cycle_issues.assert_not_called()
            self.assertEqual(self.client.cycles.call_count, 3)
            self.client.create_issue.assert_not_called()
            self.assessments.assert_not_called()

    def test_early_finish_keeps_membership_open_until_after_end_date(self):
        self.approve()
        for issue in self.items.values():
            issue['state'] = 'done'
        self.today.now.return_value = sprint.datetime.now.return_value = datetime(2026, 10, 7).astimezone()
        self.poll()
        self.start()
        self.assertIsNone(sprint.active_cycle(self.conn, self.client, 'project', self.project))
        self.assertFalse(runs.cycle_complete(self.conn, 'project', 'c'))
        self.conn.close()
        self.conn = runs.connect(self.path)
        self.items['new'] = {'id': 'new', 'name': 'New story', 'state': 'product', 'labels': []}
        self.members['c'].append('new')
        self.poll()
        self.assertEqual(self.items['new']['state'], 'sprint')
        self.client.create_issue.assert_called_once()
        self.assertEqual(runs.cycle_plan(self.conn, 'project', 'c')['status'], runs.PLAN_ASSESSING)
        self.assessments.assert_called_once()
        # Finish the new scope and approve its replacement plan.
        del self.items['plan']
        self.items['new']['state'] = 'done'
        self.approve()
        self.today.now.return_value = sprint.datetime.now.return_value = datetime(2026, 10, 14).astimezone()
        self.poll()
        self.start()
        self.assertIsNone(sprint.active_cycle(self.conn, self.client, 'project', self.project))
        self.assertFalse(runs.cycle_complete(self.conn, 'project', 'c'))
        self.today.now.return_value = sprint.datetime.now.return_value = datetime(2026, 10, 15).astimezone()
        self.poll()
        self.start()
        self.assertTrue(runs.cycle_complete(self.conn, 'project', 'c'))
        self.conn.close()
        self.conn = runs.connect(self.path)
        self.client.reset_mock()
        self.poll()
        self.start()
        self.assertIsNone(sprint.active_cycle(self.conn, self.client, 'project', self.project))
        self.client.cycle_issues.assert_not_called()

    def test_dependencies_wait_for_done_and_independent_work_continues(self):
        self.rules['b']['depends_on'] = ['a']
        self.items['a']['state'] = 'blocked'
        self.approve()
        self.start()
        self.assertEqual(self.moved(), ['c'])
        self.items['a']['state'] = 'review'
        self.start()
        self.assertEqual(self.moved(), ['c'])
        self.items['a']['state'] = 'done'
        self.start()
        self.assertEqual(self.moved(), ['c', 'b'])

    def test_overlap_with_new_start_and_review(self):
        self.rules['a']['overlaps'] = ['b']
        self.rules['b']['overlaps'] = ['a']
        self.approve()
        self.start()
        self.assertEqual(self.moved(), ['b', 'c'])

    def test_existing_review_overlap_waits(self):
        self.items['a']['state'] = 'review'
        self.rules['b']['overlaps'] = ['a']
        self.approve()
        self.start()
        self.assertEqual(self.moved(), ['c'])

    def test_no_starts_before_date_then_starts_on_date(self):
        self.client.cycles.return_value[0]['start_date'] = '2026-10-07'
        self.approve()
        self.start()
        self.assertEqual(self.moved(), [])
        self.client.cycles.return_value[0]['start_date'] = '2026-10-06'
        self.start()
        self.assertEqual(self.moved(), ['b', 'a'])

    def test_main_broken_and_pending_replan(self):
        self.approve()
        with mock.patch.object(mainhealth, 'stopped', return_value=True):
            self.start()
        self.assertEqual(self.moved(), [])
        runs.reopen_approved_plan(self.conn, 'project', 'c')
        self.conn.close()
        self.conn = runs.connect(self.path)
        self.start()
        self.assertEqual(self.moved(), [])

    def test_unapproved_cycle_and_changed_membership_wait(self):
        self.start()
        self.assertEqual(self.moved(), [])
        self.approve()
        self.members['c'].remove('a')
        self.start()
        self.assertEqual(self.moved(), [])

    def test_busy_and_cooling_agents_do_not_start_stories(self):
        self.approve()
        runs.enqueue(self.conn, project='project', issue={'id': 'other'}, agent='alpha', branch='other')
        runs.save_cooldown(self.conn, 'beta', '2099-01-01T00:00:00+00:00')
        self.start()
        self.assertEqual(self.moved(), [])

    def test_failed_move_does_not_queue_and_retries(self):
        self.approve()
        original = self.client.set_state.side_effect
        self.client.set_state.side_effect = plane.PlaneError('offline')
        self.start()
        self.assertEqual(runs.busy_agents(self.conn), set())
        self.client.set_state.reset_mock()
        self.client.set_state.side_effect = original
        self.start()
        self.assertEqual(self.moved(), ['b', 'a'])

    def test_lost_move_response_recovers_before_more_starts(self):
        self.approve()
        original = self.client.set_state.side_effect
        def lost(*args):
            original(*args)
            raise plane.PlaneError('lost response')
        self.client.set_state.side_effect = lost
        self.start()
        self.assertEqual(self.items['b']['state'], 'progress')
        self.assertEqual(runs.busy_agents(self.conn), set())
        self.conn.close()
        self.conn = runs.connect(self.path)
        self.client.set_state.side_effect = original
        self.client.issues_in_state.side_effect = lambda p, s: [
            dict(i) for i in self.items.values() if i['state'] == s]
        dd_main.poll_once(self.cfg, self.conn)
        self.start()
        self.assertEqual(self.moved(), ['b', 'a'])
        self.assertEqual(runs.busy_agents(self.conn), {'alpha', 'beta'})

    def test_main_check_reservation_is_respected(self):
        self.approve()
        choose.hold_for_main_check('project', ['alpha', 'beta'])
        self.addCleanup(choose.hold_for_main_check, 'project', [])
        self.start()
        self.assertEqual(self.moved(), [])

    def test_poll_integration_and_update_pause(self):
        calls = []
        scheduler = mock.Mock()
        scheduler.pending = {}
        for module, function in ((dd_main.runs, 'record_heartbeat'), (dd_main.lights, 'lights'),
                                 (dd_main.sync, 'pending'), (dd_main.cycles, 'poll'),
                                 (dd_main.mainhealth, 'poll'), (dd_main, 'poll_once'),
                                 (dd_main.sprint, 'poll'), (dd_main, 'poll_refreshes'),
                                 (dd_main.cleanup, 'failed_worktrees')):
            self.enterContext(mock.patch.object(module, function,
                side_effect=lambda *a, label=function: calls.append(label)))
        update = self.enterContext(mock.patch.object(dd_main.selfupdate, 'pending', return_value=None))
        self.enterContext(mock.patch.object(dd_main.selfupdate, 'when_idle', return_value=False))
        dd_main.cycle(self.cfg, self.conn, scheduler=scheduler)
        self.assertEqual(calls, ['record_heartbeat', 'lights', 'pending', 'poll', 'poll',
                                 'poll_once', 'poll', 'poll_refreshes', 'failed_worktrees'])
        calls.clear()
        update.return_value = {'pending': True}
        dd_main.cycle(self.cfg, self.conn, scheduler=scheduler)
        self.assertEqual(calls.count('poll'), 2)

    def test_selection_uses_approved_round_not_latest_report(self):
        self.approve()
        runs.reopen_approved_plan(self.conn, 'project', 'c')
        newer = runs.save_assessment_snapshot(self.conn, 'project', 'c', 'plan', 'new-revision',
                                               [], '2026-10-14', ['alpha'], members=list(self.items))
        runs.save_plan_report(self.conn, newer['id'], {'merged': {
            'order': ['c', 'a', 'b'], 'stories': self.rules, 'loops': []}}, 'Unapproved report')
        runs.set_plan_status(self.conn, 'project', 'c', runs.PLAN_APPROVED)
        self.start()
        self.assertEqual(self.moved(), ['b', 'a'])


class ActiveCycle(SprintBoard):
    def active(self):
        cycle = sprint.active_cycle(self.conn, self.client, 'project', self.project)
        return cycle and (cycle['id'], cycle['members'])

    def test_approved_started_cycle_with_unfinished_work(self):
        self.assertIsNone(self.active())
        self.approve()
        self.assertEqual(self.active(), ('c', {'a', 'b', 'c'}))
        for item in self.items.values():
            item['state'] = 'done'
        self.assertIsNone(self.active())

    def test_not_active_before_its_start_date(self):
        self.approve()
        self.client.cycles.return_value[0]['start_date'] = '2026-10-07'
        self.assertIsNone(self.active())

    def test_overrunning_cycle_stays_active_and_only_approved_past_cycles_are_read(self):
        self.approve()
        self.client.cycles.return_value.append({'id': 'old', 'start_date': '2026-09-01', 'end_date': '2026-09-14'})
        self.members['old'] = []
        sprint.datetime.now.return_value = datetime(2026, 10, 20).astimezone()
        self.assertEqual(self.active(), ('c', {'a', 'b', 'c'}))
        self.assertNotIn('old', [call.args[1] for call in self.client.cycle_issues.call_args_list])

    def test_replan_keeps_the_cycle_active(self):
        self.approve()
        runs.reopen_approved_plan(self.conn, 'project', 'c')
        self.assertEqual(self.active(), ('c', {'a', 'b', 'c'}))

    def test_plan_item_and_projects_without_cycles(self):
        self.approve()
        self.items['plan'] = {'id': 'plan', 'state': 'sprint'}
        self.members['c'] = ['plan']
        self.assertIsNone(self.active())
        self.assertIsNone(sprint.active_cycle(self.conn, self.client, 'project', {'states': {}}))


class Holds(SprintBoard):
    def setUp(self):
        super().setUp()
        self.items['x'] = {'id': 'x', 'sequence_id': 9, 'state': 'progress', 'name': 'Outside', 'labels': []}
        self.client.issues_in_state.side_effect = lambda p, s: [
            dict(i) for i in self.items.values() if i['state'] == s]

    def queue(self):
        dd_main.poll_once(self.cfg, self.conn)

    def queued(self):
        return {row['issue_id'] for row in self.conn.execute('SELECT issue_id FROM runs')}

    def notices(self):
        return [c['html'] for c in self.thread if 'cycle-hold' in c['html']]

    def test_outside_story_is_held_with_one_notice_then_released(self):
        self.approve()
        self.queue()
        self.queue()
        self.conn.close()
        self.conn = runs.connect(self.path)
        self.queue()
        self.assertEqual(self.queued(), set())
        self.assertEqual(len(self.notices()), 1)
        self.assertIn(sync.CYCLE_HOLD, self.notices()[0])
        self.assertEqual(self.items['x']['state'], 'progress')
        for issue_id in 'abc':
            self.items[issue_id]['state'] = 'done'
        self.queue()
        self.assertEqual(self.queued(), {'x'})
        self.assertEqual(len(self.notices()), 1)

    def test_no_active_cycle_queues_as_before(self):
        self.queue()
        self.assertEqual(self.queued(), {'x'})
        self.assertEqual(self.notices(), [])

    def test_lost_notice_reply_is_not_repeated(self):
        self.approve()
        original = self.client.comment.side_effect
        def lost(*args, **kwargs):
            original(*args, **kwargs)
            raise plane.PlaneError('lost response')
        self.client.comment.side_effect = lost
        self.queue()
        self.client.comment.side_effect = original
        self.queue()
        self.assertEqual(len(self.notices()), 1)
        self.assertEqual(self.queued(), set())

    def test_unreadable_cycle_holds_new_work_until_next_poll(self):
        self.approve()
        self.client.cycles.side_effect = plane.PlaneError('offline')
        self.queue()
        self.assertEqual((self.queued(), self.notices()), (set(), []))
        self.client.cycles.side_effect = None
        self.queue()
        self.assertEqual((self.queued(), len(self.notices())), (set(), 1))

    def test_fix_main_is_never_held(self):
        self.approve()
        with mock.patch.object(mainhealth, 'fix_story', return_value='x'):
            self.queue()
        self.assertEqual((self.queued(), self.notices()), ({'x'}, []))

    def test_cycle_rework_is_never_held(self):
        self.approve()
        self.items['a']['state'] = 'progress'
        self.queue()
        self.assertEqual(self.queued(), {'a'})
        self.assertEqual(len(self.notices()), 1)

    def test_work_from_before_approval_is_never_held(self):
        run_id = runs.enqueue(self.conn, project='project', issue=self.items['x'], agent='alpha', branch='dd-9')
        runs.close(self.conn, run_id, runs.FAILED)
        self.items['y'] = {'id': 'y', 'sequence_id': 10, 'state': 'progress', 'labels': []}
        runs.start_wait(self.conn, project='project', issue_id='y', run_id=None, agent='beta',
                        until='2026-10-06T00:00:00+00:00')
        self.approve()
        self.queue()
        self.assertEqual(self.queued(), {'x', 'y'})
        self.assertEqual(self.notices(), [])

    def test_reapproval_does_not_release_work_queued_after_the_first_approval(self):
        self.approve()
        def later(hours):
            return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(timespec='seconds')
        with mock.patch.object(runs.lifecycle, 'now', return_value=later(1)):
            run_id = runs.enqueue(self.conn, project='project', issue=self.items['x'], agent='alpha', branch='dd-9')
        runs.close(self.conn, run_id, runs.FAILED)
        with self.conn:
            self.conn.execute("INSERT INTO plan_approvals SELECT project, cycle_id, snapshot_id + 1, issue_id, "
                              "comment, members, described, deleted, ? FROM plan_approvals", (later(2),))
        self.queue()
        self.assertEqual(self.queued(), {'x'})
        self.assertEqual(runs.busy_agents(self.conn), set())
        self.assertEqual(len(self.notices()), 1)

    def test_approvals_saved_before_the_upgrade_count_from_the_upgrade(self):
        run_id = runs.enqueue(self.conn, project='project', issue=self.items['x'], agent='alpha', branch='dd-9')
        runs.close(self.conn, run_id, runs.FAILED)
        self.approve()
        with self.conn:
            self.conn.execute('ALTER TABLE plan_approvals DROP COLUMN approved_at')
        self.conn.close()
        self.conn = runs.connect(self.path)
        self.assertIsNotNone(runs.plan_approval(self.conn, 'project', 'c')['approved_at'])
        self.queue()
        self.assertEqual(self.notices(), [])
        self.assertEqual(runs.busy_agents(self.conn), {'alpha'})

    def test_active_runs_and_human_only_stories_get_no_notice(self):
        self.approve()
        runs.enqueue(self.conn, project='project', issue=self.items['x'], agent='alpha', branch='dd-9')
        self.items['h'] = {'id': 'h', 'sequence_id': 11, 'state': 'progress', 'labels': [{'id': 'human'}]}
        self.queue()
        self.assertEqual(self.notices(), [])
        self.assertEqual(runs.busy_agents(self.conn), {'alpha'})


class RefreshesDuringCycle(Base):
    advance_main = MainChanges.advance_main

    def test_open_pull_request_refresh_is_never_held(self):
        self.add_issue()
        self.cycle()
        self.advance_main()
        self.cfg['projects']['dd']['cycles'] = {'blocked_reminder_minutes': 60}
        with mock.patch.object(sprint, 'active_cycle', return_value={'id': 'c', 'members': set()}):
            dd_main.poll_refreshes(self.cfg, self.conn)
        self.assertEqual(self.run_rows()[-1]['kind'], 'refresh')
        self.assertEqual(self.run_rows()[-1]['status'], runs.QUEUED)


class Reminders(SprintBoard):
    def setUp(self):
        super().setUp()
        self.approve()
        self.items['a']['state'] = 'blocked'
        self.send = self.enterContext(mock.patch.object(alerts, 'send'))
        self.start_time = datetime(2026, 10, 6, 9, tzinfo=timezone.utc)

    def poll_at(self, minutes):
        at = (self.start_time + timedelta(minutes=minutes)).isoformat(timespec='seconds')
        with mock.patch.object(sprint_store, 'now', return_value=at):
            self.start()

    def reminders(self):
        return [(call.args[1:4], call.kwargs['suffix'], call.kwargs['sound']) for call in self.send.call_args_list]

    def test_blocked_by_hand_reminds_every_interval_across_restarts(self):
        self.poll_at(0)
        self.poll_at(59)
        self.assertEqual(self.reminders(), [])
        self.poll_at(60)
        self.poll_at(61)
        self.assertEqual(self.reminders(),
                         [(('project', 'a', sync.CYCLE_BLOCKED), ' blocked', config.blocked_sound(self.cfg))])
        self.conn.close()
        self.conn = runs.connect(self.path)
        self.poll_at(90)
        self.assertEqual(len(self.reminders()), 1)
        self.poll_at(120)
        self.assertEqual(len(self.reminders()), 2)

    def test_failed_run_alert_is_repeated_after_one_interval(self):
        self.cfg['alerts'] = {'blocked_sound': 'alarm'}
        with mock.patch.object(sprint_store, 'now', return_value=self.start_time.isoformat(timespec='seconds')):
            runs.mark_blocked_alert(self.conn, 'project', 'a', 'Tests failed')
        self.poll_at(30)
        self.assertEqual(self.reminders(), [])
        self.poll_at(60)
        self.assertEqual(self.reminders(), [(('project', 'a', 'Tests failed'), ' blocked', 'alarm')])

    def test_leaving_blocked_stops_reminders_and_restarts_the_clock(self):
        self.poll_at(0)
        self.items['a']['state'] = 'progress'
        self.poll_at(60)
        self.items['a']['state'] = 'blocked'
        self.poll_at(70)
        self.poll_at(120)
        self.assertEqual(self.reminders(), [])
        self.poll_at(130)
        self.assertEqual(len(self.reminders()), 1)

    def test_reminders_continue_while_main_is_red(self):
        with mock.patch.object(mainhealth, 'stopped', return_value=True):
            self.poll_at(0)
            self.poll_at(60)
        self.assertEqual(len(self.reminders()), 1)
        self.assertEqual(self.moved(), [])

    def test_only_cycle_stories_are_reminded(self):
        self.items['x'] = {'id': 'x', 'state': 'blocked'}
        self.items['a']['state'] = 'sprint'
        self.poll_at(0)
        self.poll_at(60)
        self.assertEqual(self.reminders(), [])


class EndOfCycle(SprintBoard):
    """After a Cycle's end date DD carries over what never started and holds the next Cycle until the rest is Done."""

    def setUp(self):
        super().setUp()
        names = {'a': 'Alpha', 'b': 'Beta', 'c': 'Gamma', 'd': 'Delta', 'e': 'Epsilon', 'f': 'Phi'}
        states = {'a': 'sprint', 'b': 'progress', 'c': 'review', 'd': 'blocked', 'e': 'done', 'f': 'sprint'}
        self.items = {i: {'id': i, 'name': names[i], 'state': states[i], 'labels': []} for i in names}
        self.items['f']['labels'] = [{'id': 'other'}]
        self.members['c'] = list(self.items)
        self.client.set_labels.side_effect = lambda p, i, labels: self.items[i].update(labels=labels)

    def on(self, day):
        self.today.now.return_value = sprint.datetime.now.return_value = datetime(2026, 10, day).astimezone()

    def restart(self):
        self.conn.close()
        self.conn = runs.connect(self.path)

    def carried(self):
        return [c['html'] for c in self.thread if '[dd:carried-over c]' in c['html']]

    def test_stories_still_in_sprint_backlog_are_carried_over_after_the_end_date(self):
        self.poll()
        self.on(13)
        self.poll()
        self.assertEqual(self.members['c'], list(self.items))
        self.on(14)
        self.poll()
        self.assertEqual(self.members['c'], list(self.items))
        self.on(15)
        self.poll()
        # Only what never started leaves; In Progress, In Review, Blocked and Done stay to finish normally.
        self.assertEqual(self.members['c'], ['b', 'c', 'd', 'e'])
        self.assertEqual({i: self.items[i]['state'] for i in 'abcdef'},
                         {'a': 'product', 'b': 'progress', 'c': 'review', 'd': 'blocked', 'e': 'done',
                          'f': 'product'})
        self.assertEqual(self.items['a']['labels'], ['carried'])
        self.assertEqual(self.items['f']['labels'], ['other', 'carried'])
        self.assertEqual(self.carried(), ['<p> Carried over from Sprint 1; not started.\n\n[dd:carried-over c]</p>'] * 2)
        self.assertEqual(runs.cycle_members(self.conn, 'project', 'c'), set())
        self.client.reset_mock()
        self.restart()
        self.poll()
        # Later polls that day leave the carried-over stories as they are.
        self.client.comment.assert_not_called()
        self.client.set_labels.assert_not_called()
        self.client.set_state.assert_not_called()
        self.client.reset_mock()
        self.on(15)
        self.poll()
        self.client.cycle_issues.assert_not_called()

    def test_approved_plan_is_not_reopened_by_carry_over(self):
        self.approve()
        self.poll()
        self.on(14)
        self.poll()
        self.assertIn('a', self.members['c'])
        self.assertEqual(runs.cycle_plan(self.conn, 'project', 'c')['status'], runs.PLAN_APPROVED)
        self.restart()
        self.on(15)
        self.poll()
        self.assertNotIn('a', self.members['c'])
        self.assertNotIn('f', self.members['c'])
        self.restart()
        self.poll()
        self.assertEqual(runs.cycle_plan(self.conn, 'project', 'c')['status'], runs.PLAN_APPROVED)
        self.client.create_issue.assert_not_called()
        self.assessments.assert_not_called()

    def test_failures_and_restarts_never_repeat_a_comment_or_label(self):
        self.poll()
        self.on(15)
        remove = self.client.remove_from_cycle.side_effect
        self.client.remove_from_cycle.side_effect = plane.PlaneError('offline')
        lost = self.client.comment.side_effect
        def lose_reply(p, i, text, marker):
            lost(p, i, text, marker)
            raise plane.PlaneError('response lost')
        self.client.comment.side_effect = lose_reply
        self.poll()
        self.client.comment.side_effect = lost
        self.poll()
        self.assertEqual(self.members['c'], list(self.items))
        self.assertEqual(self.items['a']['state'], 'product')
        self.assertIn('Could not carry over', [e['detail'] for e in runs.events(self.conn)][-1])
        self.client.cycles.side_effect = OSError('unreachable')
        self.poll()
        self.client.cycles.side_effect = None
        self.restart()
        self.client.remove_from_cycle.side_effect = remove
        self.poll()
        self.assertEqual(self.members['c'], ['b', 'c', 'd', 'e'])
        self.assertEqual(len(self.carried()), 2)
        self.assertEqual(self.client.set_labels.call_count, 2)
        self.assertEqual(self.items['f']['labels'], ['other', 'carried'])

    def test_a_story_started_before_it_was_carried_over_stays(self):
        self.poll()
        self.on(15)
        self.client.remove_from_cycle.side_effect = plane.PlaneError('offline')
        self.poll()
        self.items['a']['state'] = 'progress'
        self.client.remove_from_cycle.side_effect = lambda p, c, i: self.members[c].remove(i)
        self.poll()
        self.assertIn('a', self.members['c'])
        self.assertNotIn('f', self.members['c'])

    def add_sprint_2(self):
        self.client.cycles.return_value.append({'id': 'next', 'name': 'Sprint 2', 'start_date': '2026-10-16',
                                                'end_date': '2026-10-28'})
        self.items['j'] = {'id': 'j', 'name': 'Jay', 'state': 'product', 'labels': []}
        self.members['next'] = ['j']

    def test_next_cycle_waits_for_the_ended_one_and_its_plan_item_lists_the_carried_over_stories(self):
        self.members['c'] = ['a', 'b', 'c']
        self.items = {i: self.items[i] for i in 'abc'}
        self.approve()
        self.poll()
        self.on(15)
        self.add_sprint_2()
        self.poll()
        ask = [html for html in self.replies() if '[dd:plan next]' in html]
        self.assertEqual(ask, [f'<p> {sync.PLAN_ASK}\n\nThese stories were carried over without being started. '
                               'Add them first:\n- Alpha (from Sprint 1)\n\n[dd:plan next]</p>'])
        self.say('assess', issue='Plan: Sprint 2')
        self.on(20)
        self.poll()
        self.assertEqual(runs.cycle_plan(self.conn, 'project', 'next')['status'], runs.PLAN_WAITING)
        self.assertIn("Cycle 'Sprint 1' still has stories that aren't Done (Beta, Gamma)", self.replies()[-1])
        # The Cycle is complete once every story that stayed is Done.
        self.items['b']['state'] = self.items['c']['state'] = 'done'
        self.assertIsNone(sprint.active_cycle(self.conn, self.client, 'project', self.project))
        self.assertTrue(runs.cycle_complete(self.conn, 'project', 'c'))
        self.restart()
        self.client.reset_mock()
        self.say('assess', issue='Plan: Sprint 2')
        self.poll()
        self.assertEqual(runs.cycle_plan(self.conn, 'project', 'next')['status'], runs.PLAN_ASSESSING)
        self.client.cycles.assert_called_once_with('p')
        self.assertNotIn('c', [call.args[1] for call in self.client.cycle_issues.call_args_list])

    def test_carried_over_stories_are_listed_on_one_later_cycle(self):
        runs.save_carry_over(self.conn, 'project', 'c', 'Sprint 1', 'a', 'Alpha')
        self.assertEqual(runs.claim_carried_over(self.conn, 'project', 'c'), [])
        self.assertEqual([r['issue_id'] for r in runs.claim_carried_over(self.conn, 'project', 'next')], ['a'])
        runs.save_carry_over(self.conn, 'project', 'old', 'Sprint 0', 'z', 'Zed')
        self.assertEqual([r['issue_id'] for r in runs.claim_carried_over(self.conn, 'project', 'next')], ['a', 'z'])
        self.assertEqual(runs.claim_carried_over(self.conn, 'project', 'later'), [])


class EarlyFinish(SprintBoard):
    def setUp(self):
        super().setUp()
        self.rules['b']['depends_on'] = ['a']
        self.approve()
        self.project['states']['cancelled'] = 'cancelled'
        self.project['github_repo'] = 'fake/repo'
        self.cfg['github'] = {'token': 'fake-token'}
        for issue in self.items.values():
            issue['state'] = 'done'
        self.remote = self.enterContext(mock.patch.object(sprint.github, 'main_revision', return_value='last-merge'))
        self.enterContext(mock.patch.object(sprint.github, 'token', return_value='fake-token'))
        self.alert = self.enterContext(mock.patch.object(alerts, 'send'))
        self.health()

    def health(self, state='green', revision='last-merge'):
        runs.save_main_health(self.conn, 'project', revision=revision, state=state, since=runs.now(),
                              failing=[], broken_by=None, alert_owed=False)

    def test_done_ends_yesterday_once_across_restart(self):
        self.start()
        end = self.client.set_cycle_end.call_args.args[2]
        self.assertEqual(sprint.cycle_dates.local_date(end), '2026-10-05')
        self.assertEqual(sprint.cycle_dates.moment(end).time(), datetime.max.time())
        self.assertEqual(self.alert.call_args.kwargs['level'], 'passive')
        self.client.cycles.return_value[0]['end_date'] = end
        self.conn.close()
        self.conn = runs.connect(self.path)
        self.start()
        self.client.set_cycle_end.assert_called_once()
        self.alert.assert_called_once()
        self.assertTrue(runs.cycle_complete(self.conn, 'project', 'c'))

    def test_started_today_ends_today(self):
        self.client.cycles.return_value[0]['start_date'] = '2026-10-06'
        self.start()
        self.assertEqual(sprint.cycle_dates.local_date(self.client.set_cycle_end.call_args.args[2]), '2026-10-06')

    def test_cancelled_finishes_and_does_not_hold_other_work(self):
        self.items['a']['state'] = 'cancelled'
        self.start()
        self.client.set_cycle_end.assert_called_once()
        self.assertIsNone(sprint.active_cycle(self.conn, self.client, 'project', self.project))
        self.assertEqual(sprint.held_stories(self.cfg, self.conn, 'project', self.project,
                                            [{'id': 'outside', 'state': 'progress'}]), set())

    def test_cancelled_does_not_block_assessing_next_cycle(self):
        self.items['a']['state'] = 'cancelled'
        self.assertIsNone(plans.assess_refusal(
            self.conn, self.client, 'project', self.project, 'next',
            {'c': self.client.cycles.return_value[0]},
            {'c': list(self.items.values()), 'next': []}))

    def test_cancelled_dependency_never_starts_and_is_carried_over(self):
        self.items['a']['state'] = 'cancelled'
        self.items['b']['state'] = 'sprint'
        self.rules['b']['depends_on'] = ['a']
        self.approve()
        self.poll()
        self.start()
        self.assertEqual(self.items['b']['state'], 'sprint')
        self.client.set_cycle_end.assert_not_called()
        self.today.now.return_value = sprint.datetime.now.return_value = datetime(2026, 10, 15).astimezone()
        self.poll()
        self.start()
        self.assertEqual(self.items['b']['state'], 'product')
        self.assertNotIn('b', self.members['c'])
        self.assertTrue(runs.cycle_complete(self.conn, 'project', 'c'))

    def test_open_story_does_not_end(self):
        self.items['a']['state'] = 'review'
        self.start()
        self.client.set_cycle_end.assert_not_called()

    def test_main_not_checked_since_merge_then_fails_then_passes(self):
        self.health(revision='before-merge')
        self.start()
        self.client.set_cycle_end.assert_not_called()
        self.health('red')
        self.start()
        self.client.set_cycle_end.assert_not_called()
        self.health()
        self.start()
        self.client.set_cycle_end.assert_called_once()

    def test_no_main_check(self):
        self.conn.execute('DELETE FROM main_health')
        self.conn.commit()
        self.start()
        self.client.set_cycle_end.assert_not_called()

    def test_open_fix_even_after_stored_reference_cleared(self):
        self.items['fix'] = {'id': 'fix', 'name': 'Fix main: failing tests', 'state': 'review'}
        self.client.issue_is_open.return_value = True
        self.start()
        self.client.set_cycle_end.assert_not_called()
        self.client.issue_is_open.return_value = False
        self.start()
        self.client.set_cycle_end.assert_called_once()

    def test_cycle_work_active_and_slot_held_during_cleanup(self):
        run_id = runs.enqueue(self.conn, project='project', issue=self.items['a'], agent='alpha', branch='a')
        self.start()
        self.client.set_cycle_end.assert_not_called()
        runs.close(self.conn, run_id, runs.DONE)
        with self.conn:
            self.conn.execute('INSERT INTO agent_slots (agent, run_id) VALUES (?, ?)', ('alpha', run_id))
        self.start()
        self.client.set_cycle_end.assert_not_called()
        runs.release_slot(self.conn, run_id)
        self.start()
        self.client.set_cycle_end.assert_called_once()

    def test_cycle_assessment_still_active(self):
        snapshot = runs.assessment_snapshot(self.conn, 'project', 'c')
        runs.queue_assessments(self.conn, snapshot['id'])
        self.start()
        self.client.set_cycle_end.assert_not_called()

    def test_unrelated_work_does_not_prevent_ending(self):
        runs.enqueue(self.conn, project='project', issue={'id': 'outside', 'sequence_id': 9, 'name': 'Outside'},
                     agent='alpha', branch='outside')
        self.start()
        self.client.set_cycle_end.assert_called_once()

    def test_already_ended_and_unapproved_dates_unchanged(self):
        self.client.cycles.return_value[0]['end_date'] = '2026-10-05'
        self.start()
        self.client.set_cycle_end.assert_not_called()
        self.client.cycles.return_value[0]['end_date'] = '2026-10-14'
        runs.set_plan_status(self.conn, 'project', 'c', runs.PLAN_WAITING)
        self.start()
        self.client.set_cycle_end.assert_not_called()

    def test_plane_refusal_warns_and_retries(self):
        self.client.set_cycle_end.side_effect = plane.PlaneError('refused')
        self.start()
        self.alert.assert_not_called()
        self.assertTrue(self.conn.execute("SELECT 1 FROM events WHERE type = 'warning' AND detail LIKE '%refused%'").fetchone())
        self.client.set_cycle_end.side_effect = None
        self.start()
        self.assertEqual(self.client.set_cycle_end.call_count, 2)
        self.alert.assert_called_once()

    def test_lost_patch_response_recovers_after_end_date(self):
        def lost(pid, cid, end):
            self.client.cycles.return_value[0]['end_date'] = end
            raise plane.PlaneError('reply lost')
        self.client.set_cycle_end.side_effect = lost
        self.start()
        self.alert.assert_not_called()
        self.start()
        self.client.set_cycle_end.assert_called_once()
        self.alert.assert_called_once()

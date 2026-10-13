"""Approval, rejection and recovery against a fake Plane board."""

import json
import html
from unittest import mock

from dispatch import assessments, plane, plans, prompts, runs
from test_cycles import Board


class Decisions(Board):
    def setUp(self):
        super().setUp()
        self.project['agents'] = ['alpha', 'beta']
        self.project['states'].update(in_progress='working', in_review='review', blocked='blocked')
        self.cfg['github'] = {}
        self.project['github_repo'] = 'example/repo'
        self.description = ''
        self.client.get_cycle.side_effect = lambda p, c: {'description': self.description}
        self.client.set_cycle_description.side_effect = self.describe
        self.client.delete_issue.side_effect = self.delete
        self.client.get_issue.side_effect = self.get_issue
        self.client.add_to_cycle.side_effect = lambda p, c, i: self.members[c].append(i)
        self.client.set_description.side_effect = lambda p, i, text: self.items[i].update(description_html='<pre>' + html.escape(text) + '</pre>')
        self.poll()
        selected = assessments.snapshot_stories(self.conn, 'project', self.project, [self.items['i']])
        self.snapshot = runs.save_assessment_snapshot(self.conn, 'project', 'c', 'plan', 'revision', selected,
                                                       '2099-01-01', ['alpha', 'beta'])
        runs.save_plan_report(self.conn, self.snapshot['id'], {'merged': {'loops': [], 'order': ['i']}}, 'Report')
        runs.set_plan_status(self.conn, 'project', 'c', runs.PLAN_PLANNED)

    def describe(self, p, c, text):
        self.description = text

    def get_issue(self, p, i):
        if i not in self.items:
            raise plane.PlaneError('missing', status=404)
        return self.items[i].copy()

    def delete(self, p, i):
        del self.items[i]
        self.members['c'].remove(i)

    def restart(self):
        self.conn.close()
        self.conn = runs.connect(self.path)

    def status(self):
        return runs.cycle_plan(self.conn, 'project', 'c')['status']

    def test_approve_stores_report_and_comment_and_clears_only_plan(self):
        comment = self.say('approve')
        self.poll()
        self.assertEqual(self.status(), 'approved')
        saved = runs.plan_approval(self.conn, 'project', 'c')
        self.assertEqual(json.loads(saved['comment']), comment)
        self.assertEqual(saved['snapshot_id'], self.snapshot['id'])
        self.assertIn('Report', self.description)
        self.assertIn('approve', self.description)
        self.assertEqual(set(self.items), {'i'})
        self.restart()
        self.poll()
        self.client.set_cycle_description.assert_called_once()
        self.client.delete_issue.assert_called_once_with('p', 'plan')

    def test_lost_description_and_delete_responses_are_not_repeated(self):
        for step in ('description', 'delete'):
            with self.subTest(step=step):
                method = self.client.set_cycle_description if step == 'description' else self.client.delete_issue
                effect = self.describe if step == 'description' else self.delete
                def lost(*args):
                    effect(*args)
                    raise plane.PlaneError('lost response')
                method.side_effect = lost
                if step == 'description':
                    self.say('approve')
                self.poll()
                self.assertEqual(self.status(), 'approving')
                self.restart()
                method.side_effect = effect
        self.poll()
        self.assertEqual(self.status(), 'approved')
        self.client.set_cycle_description.assert_called_once()
        self.client.delete_issue.assert_called_once()

    def test_offline_before_effect_retries_after_restart(self):
        self.say('approve')
        self.client.set_cycle_description.side_effect = plane.PlaneError('offline')
        self.poll()
        self.assertEqual(self.status(), 'approving')
        self.client.delete_issue.assert_not_called()
        self.restart()
        self.client.set_cycle_description.side_effect = self.describe
        self.poll()
        self.assertEqual(self.status(), 'approved')

    def test_delete_guard_rejects_every_other_item(self):
        self.say('approve')
        self.client.set_cycle_description.side_effect = plane.PlaneError('offline')
        self.poll()
        for issue in ('i', 'unknown', 'another-plan'):
            with self.assertRaisesRegex(ValueError, 'recorded plan item'):
                plans.delete_plan_item(self.conn, self.client, 'project', 'p', 'c', issue)
        self.client.delete_issue.assert_not_called()
        with self.assertRaises(ValueError):
            plans.delete_plan_item(self.conn, self.client, 'project', 'p', 'other-cycle', 'plan')

    def test_reject_without_reasons_changes_nothing_and_reply_survives_restart(self):
        self.say('reject:  ')
        original = self.client.comment.side_effect
        def lost(*args, **kwargs):
            original(*args, **kwargs)
            raise plane.PlaneError('lost response')
        self.client.comment.side_effect = lost
        self.poll()
        self.assertEqual(self.status(), 'planned')
        self.assertFalse(runs.assessment_requested(self.conn, 'project', 'c'))
        count = self.client.comment.call_count
        self.restart()
        self.client.comment.side_effect = original
        self.poll()
        self.assertEqual(self.client.comment.call_count, count)
        self.assertIn('Please give reasons', self.replies()[-1])

    def test_plain_reject_also_requires_reasons(self):
        self.say('reject')
        self.poll()
        self.assertEqual(self.status(), 'planned')
        self.assertIn('Please give reasons', self.replies()[-1])

    def test_replacement_create_recovers_lost_response_after_restart(self):
        self.say('approve')
        self.poll()
        self.items['added'] = {'id': 'added', 'state': 'sprint'}
        self.members['c'].append('added')
        def lost(p, name, description, state):
            self.items['replacement'] = {'id': 'replacement', 'name': name, 'created_by': 'orch', 'state': state}
            raise plane.PlaneError('lost create response')
        self.client.create_issue.side_effect = lost
        self.poll()
        self.assertEqual(self.status(), 'reopening')
        self.restart()
        count = self.client.create_issue.call_count
        self.poll()
        self.assertEqual(self.status(), 'assessing')
        self.assertEqual(self.client.create_issue.call_count, count)
        self.assertEqual(runs.cycle_plan(self.conn, 'project', 'c')['issue_id'], 'replacement')
        self.assertIn('replacement', self.members['c'])
        self.assertEqual(runs.plan_issue_ids(self.conn, 'project'), {'plan', 'replacement'})

    def test_change_before_approval_does_not_silently_approve_added_story(self):
        self.items['added'] = {'id': 'added', 'state': 'sprint'}
        self.members['c'].append('added')
        self.say('approve')
        self.client.create_issue.side_effect = plane.PlaneError('offline')
        self.poll()
        self.assertEqual(self.status(), 'reopening')
        self.assertEqual(json.loads(runs.plan_approval(self.conn, 'project', 'c')['members']), ['i'])

    def start_round(self):
        plan = runs.cycle_plan(self.conn, 'project', 'c')
        with mock.patch.object(assessments.github, 'main_revision', return_value='next'), \
                mock.patch.object(assessments.github, 'token', return_value='fake'):
            # Board patches start; invoke its original function through the saved callable below.
            REAL_START(self.conn, self.client, 'project', self.project, plan,
                       {'end_date': '2099-01-01'}, [self.items[i] for i in self.members['c']], self.cfg)
        return runs.assessment_snapshot(self.conn, 'project', 'c')

    def test_rejection_reasons_reach_every_prompt_without_other_answers(self):
        self.say('reject: reduce scope and explain risk')
        self.poll()
        self.assertEqual(self.status(), 'assessing')
        self.restart()
        snapshot = self.start_round()
        self.assertEqual(snapshot['round'], 2)
        for item in runs.assessments(self.conn, snapshot['id']):
            data = runs.assessment_for_run(self.conn, item['run_id'])
            self.assertIn('reduce scope and explain risk', prompts.assessment_prompt(data))
            self.assertNotIn('private answer', prompts.assessment_prompt(data))
            runs.finish_assessment(self.conn, item['run_id'], answer={
                'stories': [{'id': 'i', 'size': 'S', 'risk': 'low', 'reason': 'private answer',
                             'depends_on': [], 'overlaps': [], 'confidence': 'high'}],
                'build_order': ['i'], 'push': []})
        with mock.patch.object(plans.alerts, 'send'), mock.patch.object(plans.plane, 'work_item_url', return_value='url'):
            self.poll()
        self.assertEqual(self.status(), 'planned')
        self.assertEqual(runs.cycle_plan(self.conn, 'project', 'c')['issue_id'], 'plan')
        self.assertIn('OK to begin', self.replies()[-1])

    def test_membership_change_reopens_only_unstarted_stories(self):
        for state in ('working', 'review', 'blocked', 'done'):
            self.items[state] = {'id': state, 'state': state}
            self.members['c'].append(state)
        # Record the membership of the report, including stories already running.
        with self.conn:
            self.conn.execute('UPDATE snapshot_members SET members = ?',
                              (json.dumps(sorted(i for i in self.members['c'] if i != 'plan')),))
        self.say('approve')
        self.poll()
        self.assertEqual(self.status(), 'approved')
        self.members['c'].remove('i')
        self.items['new'] = {'id': 'new', 'state': 'sprint'}
        self.members['c'].append('new')
        def create(*args):
            return {'id': 'new-plan', 'name': args[1], 'created_by': 'orch'}
        def saved_create(*args):
            item = create(*args)
            self.items[item['id']] = item
            return item
        self.client.create_issue.side_effect = saved_create
        self.restart()
        self.poll()
        self.assertEqual(self.status(), 'assessing')
        snapshot = self.start_round()
        self.assertEqual([s['id'] for s in json.loads(snapshot['stories'])], ['new'])
        self.assertEqual(runs.cycle_plan(self.conn, 'project', 'c')['issue_id'], 'new-plan')
        for state in ('working', 'review', 'blocked', 'done'):
            self.assertEqual(self.items[state]['state'], state)
        self.client.delete_issue.assert_called_once_with('p', 'plan')

    def test_reapproval_holds_new_queued_work_but_allows_existing_work_and_assessments(self):
        self.say('approve')
        self.poll()
        queued = runs.enqueue(self.conn, project='project', issue={'id': 'i'}, agent='alpha', branch='b')
        runs.reopen_approved_plan(self.conn, 'project', 'c')
        self.restart()
        self.assertIsNone(runs.claim_next(self.conn))
        self.assertNotIn('alpha', runs.busy_agents(self.conn))
        self.assertNotIn('alpha', runs.agent_work(self.conn))
        runs.queue_assessments(self.conn, self.snapshot['id'])
        assessment = runs.claim_next(self.conn)
        self.assertEqual(assessment['kind'], 'plan')
        runs.finish_assessment(self.conn, assessment['id'], answer={})
        runs.release_slot(self.conn, assessment['id'])
        # Existing work may continue even while a replacement plan awaits approval.
        with self.conn:
            self.conn.execute("UPDATE runs SET claimed_at = ? WHERE id = ?", (runs.now(), queued))
        self.assertEqual(runs.claim_next(self.conn)['id'], queued)

    def test_approval_releases_waiting_story(self):
        self.say('approve')
        self.poll()
        queued = runs.enqueue(self.conn, project='project', issue={'id': 'i'}, agent='alpha', branch='b')
        runs.reopen_approved_plan(self.conn, 'project', 'c')
        self.assertIsNone(runs.claim_next(self.conn))
        runs.set_plan_status(self.conn, 'project', 'c', runs.PLAN_APPROVED)
        self.assertEqual(runs.claim_next(self.conn)['id'], queued)


REAL_START = assessments.start

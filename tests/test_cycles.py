"""Cycle reconciliation with a fake Plane and a durable store."""

import copy
from datetime import datetime
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import helpers
from dispatch import cycles, plane, plans, runs, sync


class Board(unittest.TestCase):
    """A fake Plane board with one current Cycle holding one story."""

    def setUp(self):
        self.assessments = self.enterContext(mock.patch.object(plans.assessments, "start"))
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(self.temp) / 'runs.db'
        self.conn = runs.connect(self.path)
        self.addCleanup(lambda: self.conn.close())
        self.project = {'plane_project_id': 'p', 'human_only_label_id': 'human', 'states': {'done': 'done'},
                        'cycles': {'product_backlog_state': 'product', 'sprint_backlog_state': 'sprint',
                                   'carried_over_label_id': 'carried', 'max_stories': 10,
                                   'replan_quiet_minutes': 15}}
        self.cfg = {'projects': {'project': self.project}, 'plane': {'approver_user_id': 'approver'}}
        self.today = self.enterContext(mock.patch.object(cycles, 'datetime', wraps=datetime))
        self.today.now.return_value = datetime(2026, 10, 6).astimezone()
        self.client = mock.Mock()
        self.enterContext(mock.patch.object(plane, 'orchestrator', return_value=self.client))
        self.client.cycles.return_value = [{'id': 'c', 'name': 'Sprint 1', 'start_date': '2026-10-01',
                                            'end_date': '2026-10-14'}]
        self.items = {'i': {'id': 'i', 'state': 'product', 'labels': []}}
        self.members = {'c': ['i']}
        self.client.cycle_issues.side_effect = lambda p, c: [copy.deepcopy(self.items[i]) for i in self.members[c]]
        self.client.get_issue.side_effect = lambda p, i: copy.deepcopy(self.items[i])
        self.client.set_state.side_effect = lambda p, i, s: self.items[i].update(state=s)
        self.client.remove_from_cycle.side_effect = lambda p, c, i: self.members[c].remove(i)
        self.comments = set()
        self.client.has_comment_with.side_effect = lambda p, i, m: (i, m) in self.comments
        self.client.comment.side_effect = self.comment
        # The plan item's comments as Plane lists them, oldest first.
        self.thread = []
        self.client.comments.side_effect = lambda p, i: [dict(c) for c in self.thread if c['issue'] == i]
        self.client.current_user.return_value = 'orch'
        self.client.create_issue.side_effect = self.create
        self.client.issues.side_effect = lambda p: [copy.deepcopy(issue) for issue in self.items.values()]

    def comment(self, p, i, text, marker):
        self.comments.add((i, marker))
        self.say(text + '\n\n' + marker, author='orch', mention=None, issue=i)

    def create(self, p, name, description, state):
        issue_id = 'plan' if name == 'Plan: Sprint 1' else name
        self.items[issue_id] = {'id': issue_id, 'name': name, 'state': state, 'labels': [], 'created_by': 'orch'}
        return copy.deepcopy(self.items[issue_id])

    def say(self, text, author='approver', mention='orch', issue='plan'):
        """Add a comment written now, mentioning the user like Plane's editor does."""
        tag = (f'<mention-component entity_identifier="{mention}" entity_name="user_mention">'
               '</mention-component>') if mention else ''
        comment = {'id': f'comment{len(self.thread)}', 'issue': issue, 'author': author,
                   'created_at': f'2026-10-06T12:{len(self.thread):02d}:00Z', 'html': f'<p>{tag} {text}</p>'}
        self.thread.append(comment)
        return comment

    def replies(self):
        return [c['html'] for c in self.thread if c['author'] == 'orch']

    def poll(self):
        cycles.poll(self.cfg, self.conn)


class Cycles(Board):
    def test_add_and_remove_across_restart(self):
        self.poll()
        self.assertEqual(self.items['i']['state'], 'sprint')
        self.assertEqual(runs.cycle_members(self.conn, 'project', 'c'), {'i'})
        self.conn.close()
        self.conn = runs.connect(self.path)
        self.members['c'] = []
        self.poll()
        self.assertEqual(self.items['i']['state'], 'product')
        self.assertEqual(runs.cycle_members(self.conn, 'project', 'c'), set())
        self.assertEqual([e['type'] for e in runs.events(self.conn) if e['type'] != 'cycle_plan'],
                         ['cycle_move', 'cycle_move'])

    def test_other_columns_are_left_alone(self):
        for state in ('sprint', 'progress', 'review', 'done', 'blocked'):
            self.items['i']['state'] = state
            self.poll()
        self.client.set_state.assert_not_called()
        self.members['c'] = []
        self.poll()
        self.client.set_state.assert_not_called()

    def test_carried_label_removed_without_losing_other_labels(self):
        self.items['i']['labels'] = [{'id': 'carried'}, {'id': 'other'}]
        self.poll()
        self.client.set_labels.assert_called_once_with('p', 'i', ['other'])

    def test_human_only_comment_is_once_even_after_failed_removal_and_restart(self):
        self.items['i'].update(state='sprint', labels=['human'])
        remove = self.client.remove_from_cycle.side_effect
        self.client.remove_from_cycle.side_effect = plane.PlaneError('offline')
        self.poll()
        self.assertEqual(self.items['i']['state'], 'product')
        self.conn.close()
        self.conn = runs.connect(self.path)
        self.client.remove_from_cycle.side_effect = remove
        self.poll()
        self.assertEqual(self.members['c'], [])
        self.client.comment.assert_called_once()
        self.assertEqual(runs.cycle_members(self.conn, 'project', 'c'), set())

    def test_human_only_in_product_and_other_columns(self):
        for state in ('product', 'progress'):
            self.members['c'] = ['i']
            self.items['i'].update(state=state, labels=[{'id': 'human'}])
            self.poll()
            self.assertEqual(self.items['i']['state'], state)
            self.assertEqual(self.members['c'], [])
        self.client.set_state.assert_not_called()
        self.client.comment.assert_called_once()

    def test_undated_cycles_are_ignored_including_saved_membership(self):
        self.poll()
        for dates in ({}, {'start_date': 'start'}, {'end_date': 'end'}):
            self.client.cycles.return_value = [{'id': 'c', **dates}]
            self.client.cycle_issues.reset_mock()
            self.poll()
            self.client.cycle_issues.assert_not_called()
        self.assertEqual(runs.cycle_members(self.conn, 'project', 'c'), {'i'})

    def test_ended_cycle_members_are_not_reconciled_and_the_cycle_is_read_once(self):
        self.poll()
        self.client.cycles.return_value[0]['end_date'] = '2026-10-05'
        self.items['new'] = {'id': 'new', 'state': 'product', 'labels': ['carried']}
        self.members['c'] = ['new']
        self.client.reset_mock()
        self.poll()
        self.poll()
        # Read once to carry over what never started; nothing here was in Sprint Backlog.
        self.client.cycle_issues.assert_called_once_with('p', 'c')
        self.client.get_issue.assert_not_called()
        self.client.set_state.assert_not_called()
        self.client.set_labels.assert_not_called()
        self.assertEqual(runs.cycle_members(self.conn, 'project', 'c'), set())

    def test_started_and_upcoming_cycles_are_reconciled(self):
        for start, end in (('2026-10-01', '2026-10-07'),
                           ('2026-10-07', '2026-10-14')):
            with self.subTest(start=start, end=end):
                self.client.cycles.return_value = [{'id': 'c', 'start_date': start, 'end_date': end}]
                self.items['i']['state'] = 'product'
                self.client.reset_mock()
                self.poll()
                self.client.cycle_issues.assert_called_once_with('p', 'c')
                self.client.set_state.assert_called_once_with('p', 'i', 'sprint')

    def test_a_cycle_on_its_end_date_is_reconciled(self):
        self.poll()
        self.client.cycles.return_value[0]['end_date'] = '2026-10-06'
        self.client.set_labels.side_effect = lambda p, i, labels: self.items[i].update(labels=labels)
        self.items['i'].update(state='sprint')
        self.items['new'] = {'id': 'new', 'state': 'product', 'labels': []}
        self.members['c'].append('new')
        self.poll()
        self.poll()
        self.assertEqual(self.items['i'], {'id': 'i', 'state': 'sprint', 'labels': []})
        self.assertEqual(self.items['new']['state'], 'sprint')
        self.assertEqual(self.members['c'], ['i', 'new'])
        self.assertEqual(runs.cycle_members(self.conn, 'project', 'c'), {'i', 'new'})

    def test_transfer_does_not_move_back(self):
        self.poll()
        self.members = {'c': [], 'next': ['i']}
        self.client.cycles.return_value.append({'id': 'next', 'start_date': '2026-10-01', 'end_date': '2026-10-14'})
        self.client.set_state.reset_mock()
        self.poll()
        self.client.set_state.assert_not_called()
        self.assertEqual(runs.cycle_members(self.conn, 'project', 'next'), {'i'})

    def test_failed_removal_move_is_retried(self):
        self.poll()
        self.members['c'] = []
        move = self.client.set_state.side_effect
        self.client.set_state.side_effect = plane.PlaneError('offline')
        self.poll()
        self.assertEqual(runs.cycle_members(self.conn, 'project', 'c'), {'i'})
        self.client.set_state.side_effect = move
        self.poll()
        self.assertEqual(self.items['i']['state'], 'product')

    def test_failures_warn_and_retry(self):
        for method in ('cycles', 'cycle_issues', 'set_labels', 'set_state', 'has_comment_with', 'comment'):
            with self.subTest(method=method):
                self.items['i'].update(state='product', labels=['human'] if method in (
                    'has_comment_with', 'comment') else ['carried'])
                self.comments.clear()
                target = getattr(self.client, method)
                original = target.side_effect
                target.side_effect = plane.PlaneError('offline')
                self.poll()
                self.assertEqual(runs.events(self.conn)[-1]['type'], 'warning')
                target.side_effect = original
        self.items['i']['labels'] = []
        self.poll()
        self.assertEqual(self.items['i']['state'], 'sprint')

    def test_failed_first_write_still_remembers_all_observed_members(self):
        self.items['later'] = {'id': 'later', 'state': 'sprint', 'labels': []}
        self.members['c'].append('later')
        self.client.set_state.side_effect = plane.PlaneError('offline')
        self.poll()
        self.assertEqual(runs.cycle_members(self.conn, 'project', 'c'), {'i', 'later'})

    def test_reads_once_and_no_individual_reads_for_current_members(self):
        self.poll()
        self.client.cycles.assert_called_once_with('p')
        self.client.cycle_issues.assert_called_once_with('p', 'c')
        self.client.get_issue.assert_not_called()

    def test_lost_comment_reply_does_not_duplicate_notice(self):
        self.items['i']['labels'] = ['human']

        def lost_reply(p, i, text, marker):
            self.comments.add((i, marker))
            raise plane.PlaneError('lost reply')

        self.client.comment.side_effect = lost_reply
        self.poll()
        self.poll()
        self.client.comment.assert_called_once()
        self.assertEqual(self.members['c'], [])

    def test_removed_story_read_failure_preserves_membership(self):
        self.poll()
        self.members['c'] = []
        read = self.client.get_issue.side_effect
        self.client.get_issue.side_effect = plane.PlaneError('offline')
        self.poll()
        self.assertEqual(runs.cycle_members(self.conn, 'project', 'c'), {'i'})
        self.client.get_issue.side_effect = read
        self.poll()
        self.assertEqual(self.items['i']['state'], 'product')

    def test_main_calls_reconciliation_once_per_poll(self):
        with mock.patch.object(helpers.dd_main.cycles, 'poll') as poll:
            with mock.patch.multiple(helpers.dd_main, poll_once=mock.DEFAULT,
                                     poll_refreshes=mock.DEFAULT, drain=mock.DEFAULT), \
                    mock.patch.object(helpers.dd_main.lights, 'lights'), \
                    mock.patch.object(helpers.dd_main.sync, 'pending'), \
                    mock.patch.object(helpers.dd_main.mainhealth, 'poll'), \
                    mock.patch.object(helpers.dd_main.selfupdate, 'pending', return_value=None), \
                    mock.patch.object(helpers.dd_main.cleanup, 'failed_worktrees'):
                helpers.dd_main.cycle(self.cfg, self.conn)
            poll.assert_called_once_with(self.cfg, self.conn)

    def test_unconfigured_project_makes_no_requests(self):
        del self.project['cycles']
        self.poll()
        self.client.cycles.assert_not_called()


class PlanItem(Board):
    """The plan item each Cycle gets, and the approver's commands on it."""

    def restart(self):
        self.conn.close()
        self.conn = runs.connect(self.path)

    def status(self, cycle_id='c'):
        return runs.cycle_plan(self.conn, 'project', cycle_id)['status']

    def test_created_once_in_its_cycle_and_asks_for_stories(self):
        self.poll()
        self.client.create_issue.assert_called_once_with('p', 'Plan: Sprint 1', mock.ANY, 'sprint')
        self.client.add_to_cycle.assert_called_once_with('p', 'c', 'plan')
        self.assertEqual(self.replies(), [f'<p> {sync.PLAN_ASK}\n\n[dd:plan c]</p>'])
        self.assertEqual(self.status(), runs.PLAN_WAITING)
        self.members['c'].append('plan')
        self.poll()
        self.restart()
        self.poll()
        self.client.create_issue.assert_called_once()
        self.client.add_to_cycle.assert_called_once()
        self.assertEqual(len(self.replies()), 1)

    def test_plan_item_is_never_a_story(self):
        self.poll()
        self.items['plan'].update(state='product', labels=['carried'])
        self.members['c'].append('plan')
        self.poll()
        self.assertEqual(runs.cycle_members(self.conn, 'project', 'c'), {'i'})
        self.members['c'].remove('plan')
        self.poll()
        self.assertEqual(self.items['plan']['state'], 'product')
        self.client.set_labels.assert_not_called()
        self.client.get_issue.assert_not_called()
        self.assertTrue(runs.is_plan_item(self.conn, 'plan'))

    def test_no_plan_item_until_a_story_joins(self):
        self.members['c'] = []
        self.poll()
        self.items['i']['labels'] = ['human']
        self.members['c'] = ['i']
        self.poll()
        self.client.create_issue.assert_not_called()
        self.client.current_user.assert_not_called()
        self.items['i']['labels'] = []
        self.members['c'] = ['i']
        self.poll()
        self.client.create_issue.assert_called_once()

    def plan_items(self):
        return [issue for issue in self.items.values() if issue.get('name') == 'Plan: Sprint 1']

    def test_item_left_out_of_the_cycle_by_a_lost_create_response_is_taken_up(self):
        def lost(*args):
            self.create(*args)
            raise plane.PlaneError('lost reply')

        self.client.create_issue.side_effect = lost
        self.poll()
        self.assertEqual(runs.events(self.conn)[-1]['type'], 'warning')
        self.assertIsNone(runs.cycle_plan(self.conn, 'project', 'c'))
        self.restart()
        self.client.create_issue.side_effect = self.create
        self.poll()
        self.client.create_issue.assert_called_once()
        self.client.add_to_cycle.assert_called_once_with('p', 'c', 'plan')
        self.assertEqual(len(self.plan_items()), 1)
        self.assertEqual(runs.cycle_plan(self.conn, 'project', 'c')['issue_id'], 'plan')
        self.assertEqual(self.replies(), [f'<p> {sync.PLAN_ASK}\n\n[dd:plan c]</p>'])
        self.assertEqual(self.status(), runs.PLAN_WAITING)

    def test_item_of_the_same_name_by_someone_else_is_not_taken_up(self):
        self.items['theirs'] = {'id': 'theirs', 'name': 'Plan: Sprint 1', 'state': 'sprint', 'labels': [],
                                'created_by': {'id': 'approver'}}
        self.poll()
        self.client.create_issue.assert_called_once()
        self.assertEqual(runs.cycle_plan(self.conn, 'project', 'c')['issue_id'], 'plan')

    def test_item_left_in_the_cycle_by_a_lost_reply_is_taken_up_not_made_twice(self):
        self.items['plan'] = {'id': 'plan', 'name': 'Plan: Sprint 1', 'state': 'sprint', 'labels': [],
                              'created_by': {'id': 'orch'}}
        self.members['c'].append('plan')
        self.poll()
        self.client.create_issue.assert_not_called()
        self.client.add_to_cycle.assert_not_called()
        self.members['c'].remove('plan')
        self.poll()
        self.assertEqual(self.items['plan']['state'], 'sprint')
        self.assertEqual(runs.cycle_members(self.conn, 'project', 'c'), {'i'})

    def test_failed_steps_are_retried_without_repeats(self):
        for method in ('create_issue', 'add_to_cycle', 'comment'):
            with self.subTest(method=method):
                target = getattr(self.client, method)
                original, target.side_effect = target.side_effect, plane.PlaneError('offline')
                self.poll()
                target.side_effect = original
        self.restart()
        self.poll()
        self.assertEqual(self.client.create_issue.call_count, 2)  # the failed try, then one item
        self.assertEqual(self.status(), runs.PLAN_WAITING)
        self.assertEqual(len(self.replies()), 1)

    def test_assess_records_the_request(self):
        self.poll()
        self.say('assess')
        self.poll()
        self.assertEqual(self.status(), runs.PLAN_ASSESSING)
        self.assertEqual(len(self.replies()), 1)
        self.assertEqual(runs.plan_command(self.conn, 'project', 'comment1')['command'], 'assess')
        self.assessments.assert_called_once()
        self.assertEqual(self.assessments.call_args.args[6][0]['id'], 'i')

    def test_first_word_decides(self):
        for text, command in (('Assess.', 'assess'), ('assessment please', 'unclear'), ('', 'unclear'),
                              ('please assess', 'unclear')):
            with self.subTest(text=text):
                self.poll()
                comment = self.say(text)
                self.poll()
                self.assertEqual(runs.plan_command(self.conn, 'project', comment['id'])['command'], command)

    def test_anything_else_gets_one_reply_even_after_restart(self):
        self.poll()
        self.say('go ahead')
        self.poll()
        self.restart()
        self.poll()
        self.assertEqual(len(self.replies()), 2)
        self.assertIn(sync.PLAN_NOT_UNDERSTOOD, self.replies()[-1])
        self.assertIn('[dd:plan-reply comment1]', self.replies()[-1])

    def test_approve_and_reject_before_a_plan(self):
        self.poll()
        for text in ('approve', 'reject: too big'):
            self.say(text)
            self.poll()
            self.assertIn(sync.PLAN_NOT_POSTED, self.replies()[-1])
        self.assertEqual(self.status(), runs.PLAN_WAITING)

    def test_missing_report_refuses_approval_and_reject_records_reasons(self):
        self.poll()
        runs.set_plan_status(self.conn, 'project', 'c', runs.PLAN_PLANNED)
        approve, reject = self.say('approve'), self.say('reject: too big, split it')
        self.poll()
        self.assertEqual(len(self.replies()), 2)
        self.assertEqual(runs.plan_command(self.conn, 'project', approve['id'])['command'], 'approve')
        record = runs.plan_command(self.conn, 'project', reject['id'])
        self.assertEqual((record['command'], record['reasons']), ('reject', 'too big, split it'))

    def test_comments_by_others_or_without_the_mention_are_ignored(self):
        self.poll()
        self.say('assess', author='someone')
        self.say('assess', mention=None)
        self.say('assess', mention='someone')
        self.say('assess', author='orch-agent', mention='orch')
        self.poll()
        self.assertEqual(self.status(), runs.PLAN_WAITING)
        self.assertEqual(len(self.replies()), 1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM plan_commands').fetchone()[0], 0)

    def test_comments_older_than_dds_latest_question_are_ignored(self):
        self.say('assess')  # written before the item asked anything
        self.poll()
        self.assertEqual(self.status(), runs.PLAN_WAITING)
        self.assertEqual(len(self.replies()), 1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM plan_commands').fetchone()[0], 0)

    def test_every_comment_since_the_question_is_judged_once_oldest_first(self):
        self.poll()
        self.say('hmm')
        self.say('assess')  # written before DD answers the first
        self.poll()
        self.restart()
        self.poll()
        self.assertEqual(len(self.replies()), 2)
        self.assertIn(sync.PLAN_NOT_UNDERSTOOD, self.replies()[-1])
        self.assertIn('[dd:plan-reply comment1]', self.replies()[-1])
        self.assertEqual(self.status(), runs.PLAN_ASSESSING)
        self.assertEqual(runs.plan_command(self.conn, 'project', 'comment2')['command'], 'assess')
        self.assertEqual(len([e for e in runs.events(self.conn) if e['type'] == 'plan_command']), 2)

    def test_newest_first_replies_are_judged_by_creation_time_after_restart(self):
        self.poll()
        runs.set_plan_status(self.conn, 'project', 'c', runs.PLAN_PLANNED)
        reject = self.say('reject: split it')
        approve = self.say('approve')
        self.client.comments.side_effect = lambda p, i: [dict(c) for c in reversed(self.thread) if c['issue'] == i]
        self.restart()
        self.poll()
        self.assertEqual(runs.plan_command(self.conn, 'project', reject['id'])['reasons'], 'split it')
        self.assertEqual(runs.plan_command(self.conn, 'project', approve['id'])['reply'], sync.PLAN_NOT_POSTED)
        self.assertEqual([row['comment_id'] for row in self.conn.execute(
            'SELECT comment_id FROM plan_commands ORDER BY rowid')], [reject['id'], approve['id']])
        self.assertEqual(self.status(), runs.PLAN_ASSESSING)
        self.restart()
        self.poll()
        self.assertEqual(len([e for e in runs.events(self.conn) if e['type'] == 'plan_command']), 2)

    def test_edited_comment_is_judged_by_creation_time(self):
        self.poll()
        comment = self.say('go ahead')
        self.poll()
        comment['html'] = comment['html'].replace('go ahead', 'assess')
        self.poll()
        self.assertEqual(self.status(), runs.PLAN_WAITING)
        self.assertEqual(len(self.replies()), 2)

    def add_sprint_2(self, state):
        self.client.cycles.return_value.append({'id': 'next', 'name': 'Sprint 2', 'start_date': '2026-10-15',
                                                'end_date': '2026-10-28'})
        self.items['j'] = {'id': 'j', 'state': state, 'labels': []}
        self.members['next'] = ['j']

    def test_assess_refused_while_another_cycle_is_assessed_planned_or_unfinished_after_approval(self):
        self.add_sprint_2('progress')
        self.poll()
        for status, why in ((runs.PLAN_ASSESSING, 'is being assessed'),
                            (runs.PLAN_PLANNED, 'has a plan waiting for approval'),
                            (runs.PLAN_APPROVED, "still has stories that aren't Done")):
            with self.subTest(status=status):
                runs.set_plan_status(self.conn, 'project', 'next', status)
                self.say('assess')
                self.poll()
                self.assertEqual(self.status(), runs.PLAN_WAITING)
                self.assertIn(f"I can't assess this Cycle yet: Cycle 'Sprint 2' {why}", self.replies()[-1])
        self.items['j']['state'] = 'done'
        self.say('assess')
        self.poll()
        self.assertEqual(self.status(), runs.PLAN_ASSESSING)

    def test_cycles_being_prepared_never_block_each_other(self):
        self.add_sprint_2('progress')
        self.items['i']['state'] = 'progress'
        self.poll()
        self.assertEqual(self.status('next'), runs.PLAN_WAITING)
        self.say('assess')
        self.poll()
        self.assertEqual(self.status(), runs.PLAN_ASSESSING)
        # Once one is being assessed, the other waits for it.
        self.say('assess', issue='Plan: Sprint 2')
        self.poll()
        self.assertEqual(self.status('next'), runs.PLAN_WAITING)
        self.assertIn("Cycle 'Sprint 1' is being assessed", self.replies()[-1])

    def test_other_cycles_plan_items_do_not_count_as_unfinished(self):
        self.add_sprint_2('done')
        self.poll()
        runs.set_plan_status(self.conn, 'project', 'next', runs.PLAN_APPROVED)
        self.members['next'].append('Plan: Sprint 2')
        self.say('assess')
        self.poll()
        self.assertEqual(self.status(), runs.PLAN_ASSESSING)

    def test_assess_refused_over_the_story_limit(self):
        self.project['cycles']['max_stories'] = 1
        self.items.update(j={'id': 'j', 'state': 'sprint', 'labels': []},
                          k={'id': 'k', 'state': 'sprint', 'labels': []})
        self.members['c'] += ['j', 'k']
        self.poll()
        self.members['c'].append('plan')
        self.say('assess')
        self.poll()
        self.assertEqual(self.status(), runs.PLAN_WAITING)
        self.assertIn('it has 3 stories and the most is 1. Remove 2 stories', self.replies()[-1])

    def test_lost_reply_is_not_posted_twice(self):
        self.poll()
        self.say('go ahead')

        def lost(p, i, text, marker):
            self.comment(p, i, text, marker)
            raise plane.PlaneError('lost reply')

        self.client.comment.side_effect = lost
        self.poll()
        self.client.comment.side_effect = self.comment
        self.poll()
        self.assertEqual(len(self.replies()), 2)

    def test_failed_reply_is_posted_after_restart_without_judging_again(self):
        self.poll()
        self.say('go ahead')
        self.client.comment.side_effect = plane.PlaneError('offline')
        self.poll()
        self.assertEqual(runs.events(self.conn)[-1]['type'], 'warning')
        self.restart()
        self.client.comment.side_effect = self.comment
        self.poll()
        self.poll()
        self.assertEqual(len(self.replies()), 2)
        self.assertEqual(len([e for e in runs.events(self.conn) if e['type'] == 'plan_command']), 1)

    def test_mention_markup_from_older_plane(self):
        self.assertEqual(plans.command_after_mention(
            '<p><mention-component id="orch" target="users" label="Orchestrator"></mention-component>'
            ' approve</p>', 'orch').strip(), 'approve')
        self.assertIsNone(plans.command_after_mention('<p>@Orchestrator approve</p>', 'orch'))

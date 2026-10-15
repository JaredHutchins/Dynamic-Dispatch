"""Independent planning inputs, validation, retries and recovery without external services."""

import copy
import hashlib
import os
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import helpers
from dispatch import assessments, gitops, plans, prompts, runs, sync, worker


class Assessments(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.conn = runs.connect(Path(self.temp.name) / 'runs.db')
        self.addCleanup(self.conn.close)
        self.project = {'repo_path': self.temp.name, 'worktree_root': self.temp.name,
                        'plane_project_id': 'project', 'github_repo': 'owner/repo', 'agents': ['alpha', 'beta'],
                        'human_only_label_id': 'human'}
        self.cfg = {'github': {'token': 'fake-token'}, 'projects': {'p': self.project},
                    'agents': {'alpha': {'display_name': 'Alpha'}, 'beta': {'display_name': 'Beta'}}}
        self.plan = runs.save_cycle_plan(self.conn, 'p', 'cycle', 'plan')
        self.stories = [{'id': 'one', 'name': 'One', 'description_html': '<p>First story</p>'},
                        {'id': 'two', 'name': 'Two', 'description_stripped': 'Second story'}]
        self.cycle = {'end_date': '2099-01-01'}
        self.markers = set()
        self.client = Mock()
        self.client.cycles.return_value = []
        self.client.has_comment_with.side_effect = lambda p, i, m: m in self.markers
        self.client.comment.side_effect = lambda p, i, text, marker: self.markers.add(marker)

    def start(self, stories=None):
        with patch('dispatch.assessments.github.main_revision', return_value='a' * 40):
            assessments.start(self.conn, self.client, 'p', self.project, self.plan, self.cycle,
                              self.stories if stories is None else stories, self.cfg)
        return runs.assessment_snapshot(self.conn, 'p', 'cycle')

    def answer(self):
        return {'stories': [{'id': story['id'], 'size': 'M', 'risk': 'low', 'reason': 'Small surface',
                             'depends_on': [], 'overlaps': [], 'confidence': 'high'} for story in self.stories],
                'build_order': ['one', 'two'], 'push': []}

    def fenced(self, answer=None):
        return '```json\n' + json.dumps(self.answer() if answer is None else answer) + '\n```'

    def test_cooldown_notice_survives_restart_and_announces_extension(self):
        until = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(timespec='seconds')
        runs.save_cooldown(self.conn, 'alpha', until)
        with patch('dispatch.assessments.alerts.send') as send:
            snapshot = self.start()
            self.start()
            message = sync.assessment_cooldown('Alpha', until)
            self.assertEqual(self.client.comment.call_count, 2)
            self.assertEqual(self.client.comment.call_args.args[2], message)
            send.assert_called_once_with(self.cfg, 'p', 'plan', message, level='passive')
            self.conn.close()
            self.conn = runs.connect(Path(self.temp.name) / 'runs.db')
            self.addCleanup(self.conn.close)
            self.start()
            self.assertEqual(self.client.comment.call_count, 2)
            self.assertEqual(send.call_count, 1)
            extended = (datetime.fromisoformat(until) + timedelta(hours=1)).isoformat(timespec='seconds')
            runs.save_cooldown(self.conn, 'alpha', extended)
            self.start()
            self.start()
            self.assertEqual(self.client.comment.call_count, 3)
            self.assertEqual(send.call_count, 2)
            self.assertEqual(send.call_args.args[3], sync.assessment_cooldown('Alpha', extended))
            self.assertEqual(runs.get(self.conn, runs.assessments(self.conn, snapshot['id'])[0]['run_id'])['status'],
                             'queued')

    def test_assess_reply_names_cooling_agent_and_deadline(self):
        self.start()
        until = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(timespec='seconds')
        runs.save_cooldown(self.conn, 'alpha', until)
        record = plans.judge(self.conn, self.client, 'p', self.project, self.plan, 'waiting', 'assess',
                             {'cycle': self.cycle}, {'cycle': self.stories}, cfg=self.cfg)
        self.assertEqual(record['reply'], sync.assessment_cooldown('Alpha', until))
        self.assertIn(datetime.fromisoformat(until).astimezone().strftime('%a %H:%M'), record['reply'])

    def test_no_cooldown_notice_without_cooling_unfinished_agent(self):
        with patch('dispatch.assessments.alerts.send') as send:
            snapshot = self.start()
            self.start()
            self.assertEqual(self.client.comment.call_count, 1)
            send.assert_not_called()
            run = runs.claim_next(self.conn)
            runs.finish_assessment(self.conn, run['id'], answer=self.answer())
            runs.release_slot(self.conn, run['id'])
            runs.save_cooldown(self.conn, run['agent'], '2099-01-01T00:00:00+00:00')
            self.start()
            self.assertEqual(self.client.comment.call_count, 1)
            send.assert_not_called()

    def test_usage_limit_during_assessment_announces_each_new_cooldown(self):
        snapshot = self.start()
        beta = runs.assessments(self.conn, snapshot['id'])[1]
        runs.finish_assessment(self.conn, beta['run_id'], answer=self.answer())
        for index in range(2):
            runs.save_cooldown(self.conn, 'alpha', '2000-01-01T00:00:00+00:00')
            run = runs.claim_next(self.conn)
            with patch('dispatch.assessments.agents.run', return_value={'usage_limit': True}), \
                    patch('dispatch.assessments.alerts.send') as send:
                assessments.ask(self.cfg, self.conn, run, runs.assessment_for_run(self.conn, run['id']), self.temp.name)
                runs.release_slot(self.conn, run['id'])
                runs.save_cooldown(self.conn, run['agent'], f'2099-01-0{index + 1}T00:00:00+00:00')
                self.start()
                self.start()
                send.assert_called_once()

    def test_snapshot_excludes_plan_and_human_and_stays_fixed(self):
        self.cycle['start_date'] = '2098-12-31T22:00:00Z'
        snapshot = self.start(self.stories + [{'id': 'plan', 'name': 'Plan'},
                                             {'id': 'human', 'name': 'Human', 'labels': ['human']}])
        self.assertEqual([x['id'] for x in json.loads(snapshot['stories'])], ['one', 'two'])
        self.assertIn('First story', snapshot['stories'])
        self.assertEqual(snapshot['revision'], 'a' * 40)
        self.assertEqual(snapshot['start_date'], self.cycle['start_date'])
        self.cycle['start_date'] = '2099-01-01T00:00:00Z'
        self.stories[0]['name'] = 'Changed later'
        with patch('dispatch.assessments.github.main_revision', side_effect=AssertionError('refetched')):
            assessments.start(self.conn, self.client, 'p', self.project, self.plan, self.cycle, self.stories, self.cfg)
        self.assertEqual(runs.assessment_snapshot(self.conn, 'p', 'cycle')['stories'], snapshot['stories'])
        self.assertEqual(runs.assessment_snapshot(self.conn, 'p', 'cycle')['start_date'], snapshot['start_date'])
        self.assertEqual(len(runs.assessments(self.conn, snapshot['id'])), 2)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 2)

    def test_snapshot_and_queue_survive_database_reopen(self):
        snapshot = self.start()
        before = [dict(row) for row in runs.assessments(self.conn, snapshot['id'])]
        with runs.connect(Path(self.temp.name) / 'runs.db') as reopened:
            self.assertEqual(dict(runs.assessment_snapshot(reopened, 'p', 'cycle')), dict(snapshot))
            runs.queue_assessments(reopened, snapshot['id'])
            self.assertEqual([dict(row) for row in runs.assessments(reopened, snapshot['id'])], before)
        reopened.close()

    def test_start_reply_once_even_after_lost_response(self):
        def lost(p, i, text, marker):
            self.markers.add(marker)
            raise OSError('lost response')
        self.client.comment.side_effect = lost
        with self.assertRaises(OSError):
            self.start()
        self.start()
        self.client.comment.assert_called_once()
        self.assertEqual(self.client.comment.call_args.args[2],
                         "Assessing this Cycle with Alpha, Beta. I'll post the plan here when every assessment is in.")

    def test_prompts_identical_and_never_include_other_answer(self):
        snapshot = self.start()
        before = prompts.assessment_prompt(snapshot)
        run = runs.claim_next(self.conn)
        runs.finish_assessment(self.conn, run['id'], answer={'private': 'UNIQUE PEER ANSWER'})
        other = runs.assessment_for_run(self.conn, runs.assessments(self.conn, snapshot['id'])[1]['run_id'])
        self.assertEqual(prompts.assessment_prompt(other), before)
        self.assertNotIn('UNIQUE PEER ANSWER', before)
        for text in ('2099-01-01', 'build_order', 'confidence', 'overlaps', 'push', 'reason', 'S, M or L'):
            self.assertIn(text, before)

    def test_validation(self):
        self.assertEqual(assessments.validate(self.fenced(), self.stories)[1], [])
        changes = [lambda a: a['stories'].pop(),
                   lambda a: a['stories'].append(copy.deepcopy(a['stories'][0])),
                   lambda a: a['stories'][0].update(size='XL'),
                   lambda a: a['stories'][0].update(risk='severe'),
                   lambda a: a['stories'][0].update(confidence=None),
                   lambda a: a['stories'][0].update(reason=' '),
                   lambda a: a['stories'][0].update(depends_on=['outside']),
                   lambda a: a['stories'][0].update(overlaps=['one']),
                   lambda a: a.update(build_order=['one', 'one']),
                   lambda a: a.update(push=[{'id': 'outside', 'reason': 'too large'}]),
                   lambda a: a.update(push=[{'id': 'one', 'reason': ''}])]
        for change in changes:
            answer = self.answer()
            change(answer)
            with self.subTest(answer=answer):
                self.assertTrue(assessments.validate(self.fenced(answer), self.stories)[1])
        for text in ('no block', '```json\nnot JSON\n```', self.fenced() * 2):
            self.assertTrue(assessments.validate(text, self.stories)[1])

    def test_invalid_reprompt_once_with_exact_problems(self):
        self.start()
        run = runs.claim_next(self.conn)
        snapshot = runs.assessment_for_run(self.conn, run['id'])
        with patch('dispatch.assessments.agents.run', side_effect=[{'ok': True, 'summary': 'wrong'},
                                                                 {'ok': True, 'summary': self.fenced()}]) as agent:
            assessments.ask(self.cfg, self.conn, run, snapshot, Path(self.temp.name))
        self.assertEqual(agent.call_count, 2)
        self.assertIn('Return exactly one fenced json block.', agent.call_args.args[2])
        self.assertIsNotNone(runs.assessment_for_run(self.conn, run['id'])['answer'])

    def test_fail_twice_notify_once_then_assess_retries(self):
        snapshot = self.start()
        for attempt in range(2):
            row = runs.assessments(self.conn, snapshot['id'])[0]
            run = runs.get(self.conn, row['run_id'])
            with patch('dispatch.assessments.agents.run', return_value={'ok': True, 'summary': 'invalid'}) as agent:
                assessments.ask(self.cfg, self.conn, run, runs.assessment_for_run(self.conn, run['id']), self.temp.name)
            self.assertEqual(agent.call_count, 2)
            with patch('dispatch.assessments.alerts.send') as alert:
                self.start()
                self.start()
            self.assertEqual(alert.call_count, int(attempt == 1))
        stopped = runs.assessments(self.conn, snapshot['id'])[0]
        self.assertEqual(stopped['failures'], 2)
        self.assertIn('Alpha', self.client.comment.call_args.args[2])
        runs.record_plan_command(self.conn, 'p', 'cycle', 'retry', 'assess', status=runs.PLAN_ASSESSING, retry_snapshot=snapshot['id'])
        self.start()
        retried = runs.assessments(self.conn, snapshot['id'])[0]
        self.assertEqual(retried['failures'], 0)
        runs.finish_assessment(self.conn, retried['run_id'], error='failed again')
        self.start()
        second_retry = runs.assessments(self.conn, snapshot['id'])[0]
        runs.finish_assessment(self.conn, second_retry['run_id'], error='failed again')
        runs.record_plan_command(self.conn, 'p', 'cycle', 'retry', 'assess', status=runs.PLAN_ASSESSING, retry_snapshot=snapshot['id'])
        self.assertEqual(runs.assessments(self.conn, snapshot['id'])[0]['failures'], 2)
        self.assertNotEqual(retried['run_id'], stopped['run_id'])

    def command(self, comment='assess'):
        self.project.update(states={'done': 'done'}, cycles={'max_stories': 10})
        return plans.judge(self.conn, self.client, 'p', self.project, self.plan, comment, 'assess',
                           {'cycle': self.cycle}, {'cycle': self.stories})

    def settle(self, snapshot, failed=False):
        for row in runs.assessments(self.conn, snapshot['id']):
            runs.finish_assessment(self.conn, row['run_id'], answer={'private': row['agent']})
        if failed:
            with self.conn:
                self.conn.execute("UPDATE assessments SET answer = NULL, failures = 2 "
                                  "WHERE snapshot_id = ? AND agent = 'beta'", (snapshot['id'],))

    def test_changed_stories_create_new_round_without_old_answers(self):
        old = self.start()
        self.settle(old, failed=True)
        self.stories.append({'id': 'three', 'name': 'Three', 'description_stripped': 'New'})
        self.assertIsNone(self.command()['reply'])
        with patch('dispatch.assessments.github.main_revision', return_value='b' * 40):
            assessments.start(self.conn, self.client, 'p', self.project, self.plan, self.cycle,
                              self.stories, self.cfg)
        current = runs.assessment_snapshot(self.conn, 'p', 'cycle')
        self.assertEqual(current['round'], 2)
        self.assertEqual(current['revision'], 'b' * 40)
        self.assertEqual(len(json.loads(current['stories'])), 3)
        self.assertTrue(all(row['answer'] is None for row in runs.assessments(self.conn, current['id'])))
        self.assertIsNotNone(runs.assessments(self.conn, old['id'])[0]['answer'])
        self.assertEqual(self.client.comment.call_count, 2)
        reopened = runs.connect(Path(self.temp.name) / 'runs.db')
        self.addCleanup(reopened.close)
        self.assertEqual(dict(runs.assessment_snapshot(reopened, 'p', 'cycle')), dict(current))
        before = [dict(row) for row in runs.assessments(reopened, current['id'])]
        runs.queue_assessments(reopened, current['id'])
        self.assertEqual([dict(row) for row in runs.assessments(reopened, current['id'])], before)

    def test_unchanged_retry_keeps_good_answer_and_only_requeues_failed_agent(self):
        old = self.start()
        self.settle(old, failed=True)
        before = runs.assessments(self.conn, old['id'])
        self.stories.reverse()
        self.assertIsNone(self.command()['reply'])
        current = self.start()
        after = runs.assessments(self.conn, current['id'])
        self.assertEqual(current['id'], old['id'])
        self.assertEqual(dict(after[0]), dict(before[0]))
        self.assertNotEqual(after[1]['run_id'], before[1]['run_id'])
        self.assertEqual(after[1]['failures'], 0)

    def test_assess_while_running_replies_and_keeps_round(self):
        old = self.start()
        self.assertIn('already running', self.command()['reply'])
        self.assertEqual(self.start()['id'], old['id'])

    def test_completed_unchanged_cycle_starts_new_round(self):
        old = self.start()
        self.settle(old)
        self.command()
        current = self.start()
        self.assertEqual(current['round'], 2)
        self.assertTrue(all(row['answer'] is None for row in runs.assessments(self.conn, current['id'])))

    def test_completed_round_and_changed_description_start_fresh(self):
        for failed in (False, True):
            old = self.start()
            self.settle(old, failed=failed)
            self.stories[0]['description_html'] += ' changed'
            self.assertIsNone(self.command(str(failed))['reply'])
            self.assertIn('already running', self.command('pending' + str(failed))['reply'])
            with runs.connect(Path(self.temp.name) / 'runs.db') as reopened:
                self.assertTrue(runs.assessment_requested(reopened, 'p', 'cycle'))
            reopened.close()
            self.assertEqual(self.start()['round'], old['round'] + 1)

    def test_agent_failure_and_timeout_retry(self):
        snapshot = self.start()
        row = runs.assessments(self.conn, snapshot['id'])[0]
        run = runs.get(self.conn, row['run_id'])
        with patch('dispatch.assessments.agents.run', return_value={'ok': False, 'error': 'Timed out'}):
            assessments.ask(self.cfg, self.conn, run, runs.assessment_for_run(self.conn, run['id']), self.temp.name)
        self.start()
        retry = runs.assessments(self.conn, snapshot['id'])[0]
        self.assertEqual(retry['failures'], 1)
        self.assertNotEqual(retry['run_id'], run['id'])

    def test_restart_requeues_same_snapshot_without_repeating_completed_agent(self):
        snapshot = self.start()
        first = runs.claim_next(self.conn)
        runs.finish_assessment(self.conn, first['id'], answer=self.answer())
        runs.release_slot(self.conn, first['id'])
        second = runs.claim_next(self.conn)
        runs.interrupt_running(self.conn)
        self.start()
        rows = runs.assessments(self.conn, snapshot['id'])
        self.assertEqual(rows[0]['run_id'], first['id'])
        self.assertNotEqual(rows[1]['run_id'], second['id'])
        self.assertEqual(rows[1]['failures'], 0)
        self.assertEqual(runs.assessment_for_run(self.conn, rows[1]['run_id'])['revision'], 'a' * 40)

    def test_slots_and_usage_cooldown(self):
        self.start()
        first = runs.claim_next(self.conn)
        snapshot = runs.assessment_for_run(self.conn, first['id'])
        with patch('dispatch.assessments.agents.run', return_value={'usage_limit': True}), \
                patch('dispatch.assessments.choose.cool_down') as cool:
            assessments.ask(self.cfg, self.conn, first, snapshot, self.temp.name)
        cool.assert_called_once()
        self.assertEqual(runs.assessment_for_run(self.conn, first['id'])['failures'], 0)
        self.start()
        runs.save_cooldown(self.conn, 'alpha', '2999-01-01T00:00:00+00:00')
        second = runs.claim_next(self.conn)
        self.assertEqual(second['agent'], 'beta')
        self.assertIsNone(runs.claim_next(self.conn))
        runs.release_slot(self.conn, first['id'])
        self.assertIsNone(runs.claim_next(self.conn))


# Local repositories and fake adapters exercise the actual checkout and scheduler boundaries.


class AssessmentCheckout(helpers.Base):
    def prepare(self):
        revision = gitops.branch_revision(self.repo, 'main')
        snapshot = runs.save_assessment_snapshot(self.conn, 'dd', 'cycle', 'plan', revision,
                                                [{'id': 'one', 'name': 'One', 'description': 'Inspect app.py'}],
                                                '2099-01-01', ['claude-code'])
        runs.queue_assessments(self.conn, snapshot['id'])
        return snapshot

    def valid(self):
        return {'ok': True, 'summary': '```json\n' + json.dumps({
            'stories': [{'id': 'one', 'size': 'S', 'risk': 'low', 'reason': 'Small change',
                         'depends_on': [], 'overlaps': [], 'confidence': 'high'}],
            'build_order': ['one'], 'push': []}) + '\n```'}

    def test_pinned_read_only_checkout_discards_changes_and_records_warning(self):
        snapshot = self.prepare()
        (self.repo / 'later.txt').write_text('not part of the snapshot')
        helpers.git('add', 'later.txt', cwd=self.repo)
        helpers.git('-c', 'user.name=Test', '-c', 'user.email=test@example.com',
                    'commit', '-m', 'later', cwd=self.repo)
        seen = []

        def agent(cfg, agent_cfg, prompt, folder):
            seen.append(folder)
            self.assertEqual(helpers.git('rev-parse', 'HEAD', cwd=folder).strip(), snapshot['revision'])
            self.assertFalse((folder / 'later.txt').exists())
            self.assertEqual(folder.stat().st_gid, os.getegid())
            self.assertEqual(folder.stat().st_mode & 0o222, 0)
            self.assertEqual((folder / 'app.py').stat().st_mode & 0o222, 0)
            # The fake agent runs as DD in tests, so it can deliberately undo the read-only mode.
            (folder / 'app.py').chmod(0o600)
            (folder / 'app.py').write_text('changed')
            return self.valid()

        run = runs.claim_next(self.conn)
        with patch('dispatch.assessments.agents.run', side_effect=agent), \
                patch('dispatch.worker.sync.reconcile') as reconcile:
            worker.execute_thread(self.cfg, dict(run))
        reconcile.assert_not_called()
        self.assertEqual(runs.get(self.conn, run['id'])['status'], runs.DONE)
        self.assertFalse(seen[0].exists())
        self.assertFalse(seen[0].parent.exists())
        self.assertTrue(any('changes discarded' in e['detail'] for e in runs.events(self.conn)))
        self.assertFalse(runs.has_running(self.conn))
        self.assertEqual((self.repo / 'app.py').read_text(), "print('hi')\n")

    def test_cleanup_after_failure_preserves_story_and_main_folders(self):
        self.prepare()
        story = self.wt_root / 'dd-123'
        story.mkdir(parents=True)
        (story / 'keep.txt').write_text('story work')
        main = self.wt_root / 'main-check'
        main.mkdir()
        run = runs.claim_next(self.conn)
        with patch('dispatch.assessments.ask', side_effect=RuntimeError('fake failure')):
            assessments.run_assessment(self.cfg, self.conn, run)
        self.assertEqual(set(self.wt_root.iterdir()), {story, main})
        self.assertEqual((story / 'keep.txt').read_text(), 'story work')

    def test_cleanup_does_not_recursively_remove_nonempty_container(self):
        self.prepare()
        run = runs.claim_next(self.conn)
        seen = []

        def agent(cfg, agent_cfg, prompt, folder):
            seen.append(folder.parent)
            (folder.parent / 'keep.txt').write_text('unexpected file')
            return self.valid()

        with patch('dispatch.assessments.agents.run', side_effect=agent):
            assessments.run_assessment(self.cfg, self.conn, run)
        self.assertEqual((seen[0] / 'keep.txt').read_text(), 'unexpected file')
        self.assertTrue(any('Could not discard assessment folder' in e['detail'] for e in runs.events(self.conn)))

    def test_real_scheduler_limit_keeps_assessor_and_does_not_hand_over(self):
        snapshot = self.prepare()
        with patch('dispatch.assessments.agents.run', return_value={'usage_limit': True}), \
                patch.object(helpers.dd_main, '_hand_over') as handover:
            helpers.dd_main.drain(self.cfg, self.conn)
        handover.assert_not_called()
        item = runs.assessments(self.conn, snapshot['id'])[0]
        self.assertEqual(item['failures'], 0)
        runs.queue_assessments(self.conn, snapshot['id'])
        self.assertIsNone(runs.claim_next(self.conn))
        self.assertEqual(runs.assessments(self.conn, snapshot['id'])[0]['agent'], 'claude-code')

    def test_main_recovery_leaves_plan_retry_to_assessments(self):
        snapshot = self.prepare()
        run = runs.claim_next(self.conn)
        with patch.object(helpers.dd_main.proc, 'reap', return_value=None), \
                patch.object(helpers.dd_main, '_requeue') as requeue:
            helpers.dd_main.recover(self.cfg, self.conn)
        requeue.assert_not_called()
        self.assertEqual(runs.get(self.conn, run['id'])['status'], runs.INTERRUPTED)
        runs.queue_assessments(self.conn, snapshot['id'])
        next_run = runs.claim_next(self.conn)
        self.assertEqual(next_run['kind'], 'plan')
        self.assertEqual(next_run['agent'], run['agent'])
        self.assertEqual(runs.assessment_for_run(self.conn, next_run['id'])['revision'], snapshot['revision'])

    def test_restart_discards_interrupted_folder_before_fresh_attempt(self):
        snapshot = self.prepare()
        run = runs.claim_next(self.conn)
        with patch('dispatch.assessments.ask', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                assessments.run_assessment(self.cfg, self.conn, run)
        # A power cut skips finally; leave a real checkout to model that case.
        account = hashlib.sha256(run['agent'].encode()).hexdigest()[:16]
        root = self.wt_root / f"plan-{snapshot['id']}-{account}"
        abandoned = gitops.main_check_folder(self.repo, root, 'main', snapshot['revision'], group=os.getegid())
        (abandoned / 'app.py').write_text('interrupted edit')
        runs.interrupt_running(self.conn)
        runs.queue_assessments(self.conn, snapshot['id'])
        retry = runs.claim_next(self.conn)

        def fresh(cfg, agent_cfg, prompt, folder):
            self.assertEqual((folder / 'app.py').read_text(), "print('hi')\n")
            return self.valid()

        with patch('dispatch.assessments.agents.run', side_effect=fresh):
            worker.execute_thread(self.cfg, dict(retry))
        self.assertEqual(runs.get(self.conn, retry['id'])['status'], runs.DONE)
        self.assertFalse(abandoned.exists())
        self.assertFalse(root.exists())
        self.assertTrue(any('changes discarded' in e['detail'] for e in runs.events(self.conn)))


if __name__ == '__main__':
    unittest.main()

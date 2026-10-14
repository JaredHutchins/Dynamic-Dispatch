"""The consolidated plan: merging, the estimate, the report on the plan item and automatic new rounds."""

from datetime import datetime, timedelta, timezone
import json
import html
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from dispatch import plan_report, plane, plans, runs, sync

NOW = datetime(2026, 10, 7, 9, 0, tzinfo=timezone.utc)
HOUR = 3600


def story(sid, size='M', risk='low', depends_on=(), overlaps=()):
    return {'id': sid, 'size': size, 'risk': risk, 'reason': f'{sid} reason', 'depends_on': list(depends_on),
            'overlaps': list(overlaps), 'confidence': 'high'}


def answer(rows, order, push=()):
    return {'stories': rows, 'build_order': order, 'push': [{'id': sid, 'reason': f'push {sid}'} for sid in push]}


STORIES = [{'id': sid, 'name': sid.title(), 'description': ''} for sid in ('a', 'b', 'c')]


class Merge(unittest.TestCase):
    def test_unions_and_side_by_side_answers(self):
        merged = plan_report.merge(STORIES, {
            'alpha': answer([story('a', 'S'), story('b', depends_on=['a']), story('c', overlaps=['a'])],
                            ['a', 'b', 'c']),
            'beta': answer([story('a', 'L', 'high'), story('b'), story('c', depends_on=['b'])], ['a', 'b', 'c'],
                           push=['c'])})
        a, b, c = (merged['stories'][sid] for sid in 'abc')
        self.assertEqual(b['depends_on'], ['a'])
        self.assertEqual(c['depends_on'], ['b'])
        self.assertEqual(a['overlaps'], ['c'])
        self.assertEqual(c['overlaps'], ['a'])
        self.assertEqual(a['sizes'], {'alpha': 'S', 'beta': 'L'})
        self.assertEqual(a['risks']['beta'], {'risk': 'high', 'reason': 'a reason'})
        self.assertEqual(c['push'], {'beta': 'push c'})
        self.assertEqual(merged['loops'], [])
        self.assertEqual(plan_report.disagreements(merged),
                         [('a', 'size'), ('a', 'risk'), ('b', 'depends_on'), ('c', 'depends_on'), ('c', 'push')])

    def test_average_position_respects_dependencies(self):
        merged = plan_report.merge(STORIES, {
            'alpha': answer([story('a', depends_on=['c']), story('b'), story('c')], ['a', 'b', 'c']),
            'beta': answer([story('a'), story('b'), story('c')], ['a', 'b', 'c'])})
        # a averages first but needs c, which is placed before it.
        self.assertEqual(merged['order'], ['b', 'c', 'a'])

    def test_ties_keep_listing_order(self):
        merged = plan_report.merge(STORIES, {
            'alpha': answer([story('a'), story('b'), story('c')], ['a', 'b', 'c']),
            'beta': answer([story('a'), story('b'), story('c')], ['b', 'a', 'c'])})
        self.assertEqual(merged['stories']['a']['position'], merged['stories']['b']['position'])
        self.assertEqual(merged['order'], ['a', 'b', 'c'])

    def test_loop_across_agents_is_found_and_reported_first(self):
        merged = plan_report.merge(STORIES, {
            'alpha': answer([story('a', depends_on=['b']), story('b'), story('c')], ['a', 'b', 'c']),
            'beta': answer([story('a'), story('b', depends_on=['a']), story('c', depends_on=['a'])],
                           ['b', 'a', 'c'])})
        self.assertEqual(merged['loops'], [{'stories': ['a', 'b'],
                                            'links': [['a', 'b', ['alpha']], ['b', 'a', ['beta']]]}])
        self.assertEqual(sorted(merged['order']), ['a', 'b', 'c'])
        self.assertEqual(merged['order'][-1], 'c')
        text = plan_report.render(merged, {'based_on': 0}, {'alpha': 'Alpha', 'beta': 'Beta'}, 1)
        self.assertEqual(text.split('\n')[2], 'Needs your decision')
        self.assertIn('A needs B (Alpha); B needs A (Beta)', text)
        self.assertIn("can't be approved while a loop remains", text)


class Estimate(unittest.TestCase):
    def merged(self, push=(), depends=None, sizes='MMM'):
        depends = depends or {}
        rows = [story(sid, size, depends_on=depends.get(sid, ())) for sid, size in zip('abc', sizes)]
        return plan_report.merge(STORIES, {'alpha': answer(rows, ['a', 'b', 'c'], push=push)})

    def test_no_history_says_so(self):
        est = plan_report.estimate(self.merged(), [], 2, '2026-10-14', NOW)
        self.assertEqual(est, {'based_on': 0, 'agents': 2, 'end_date': '2026-10-14',
                               'fallback_sizes': ['S', 'M', 'L']})
        text = plan_report.render(self.merged(), est, {}, 1)
        self.assertIn("no Done stories to estimate from yet", text)
        self.assertNotIn('Stories to push', text)

    def test_weighted_median_spread_over_agents(self):
        # Median one day; S 0.5 + M 1 + L 2 = 3.5 days of work over four agents, but the L alone takes two.
        est = plan_report.estimate(self.merged(sizes='SML'), [12 * HOUR, 24 * HOUR, 99 * HOUR], 4,
                                   '2026-10-14', NOW)
        self.assertEqual((est['days'], est['finish'], est['fits'], est['based_on']), (2.0, '2026-10-09', True, 3))
        est = plan_report.estimate(self.merged(sizes='SML'), [12 * HOUR, 24 * HOUR, 99 * HOUR], 1,
                                   '2026-10-14', NOW)
        self.assertEqual((est['days'], est['finish']), (3.5, '2026-10-10'))
        self.assertEqual(est['median_hours'], 24.0)
        text = plan_report.render(self.merged(sizes='SML'), est, {}, 1)
        self.assertIn('This is rough. It is based on 3 past stories', text)
        self.assertIn('fits the Cycle', text)

    def test_never_shorter_than_longest_chain(self):
        merged = self.merged(depends={'b': ['a'], 'c': ['b']})
        est = plan_report.estimate(merged, [24 * HOUR], 3, '2026-10-14', NOW)
        self.assertEqual((est['days'], est['chain_days']), (3.0, 3.0))

    def test_cycle_that_does_not_fit_pushes_lowest_first_then_agent_suggestions(self):
        # Three days of work for one agent, two days left: pushing b is enough, then c is suggested.
        merged = self.merged(depends={'b': ['a']}, push=['c'])
        merged['order'] = ['a', 'c', 'b']
        est = plan_report.estimate(merged, [24 * HOUR], 1, '2026-10-09', NOW)
        self.assertFalse(est['fits'])
        self.assertEqual(est['push'], ['b', 'c'])
        self.assertTrue(est['push_fits'])
        text = plan_report.render(merged, est, {'alpha': 'Alpha'}, 1)
        self.assertIn('passes the Cycle', text)
        self.assertIn('- B (lowest in the order)', text)
        self.assertIn('- C (suggested by Alpha: push c)', text)

    def test_pushing_a_dependent_story_does_not_free_what_it_needs(self):
        # c needs b and b needs a. Pushing c must not make b pushable, nor b's push make a pushable.
        merged = self.merged(depends={'b': ['a'], 'c': ['b']}, push=['a'])
        est = plan_report.estimate(merged, [24 * HOUR], 1, '2026-10-08', NOW)
        self.assertEqual(est['push'], ['c'])
        self.assertEqual((est['push_finish'], est['push_fits']), ('2026-10-09', False))
        # Once pushing c fits, an agent's suggestion to push b, which c needs, is not offered either.
        merged = self.merged(depends={'b': ['a'], 'c': ['b']}, push=['b'])
        est = plan_report.estimate(merged, [24 * HOUR], 1, '2026-10-09', NOW)
        self.assertEqual(est['push'], ['c'])
        self.assertTrue(est['push_fits'])

    def test_finish_counts_what_kept_stories_depend_on(self):
        merged = self.merged(depends={'b': ['a'], 'c': ['b']})
        self.assertEqual(plan_report.finish(merged, ['c'], 24 * HOUR, 3, NOW)['chain_days'], 3.0)

    def test_a_story_others_need_is_never_pushed(self):
        # a and b need each other, so neither can go; a suggestion to push b is not offered either.
        merged = self.merged(depends={'a': ['b'], 'b': ['a']}, push=['b'])
        self.assertEqual(merged['order'], ['c', 'a', 'b'])
        est = plan_report.estimate(merged, [24 * HOUR], 1, '2026-10-08', NOW)
        self.assertEqual(est['push'], ['c'])
        self.assertFalse(est['push_fits'])
        self.assertIn('which still passes the end date', plan_report.render(merged, est, {}, 1))
        est = plan_report.estimate(merged, [24 * HOUR], 1, '2026-10-07', NOW)
        self.assertNotIn('a', est['push'])


class Report(unittest.TestCase):
    """Posting the report on the plan item, against a durable store and a fake Plane."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'runs.db'
        self.conn = runs.connect(self.path)
        self.addCleanup(lambda: self.conn.close())
        self.project = {'plane_project_id': 'project', 'agents': ['alpha', 'beta'], 'states': {'done': 'done'},
                        'cycles': {'max_stories': 10, 'replan_quiet_minutes': 15}}
        self.cfg = {'projects': {'p': self.project},
                    'plane': {'base_url': 'https://plane.example', 'workspace_slug': 'ws'},
                    'agents': {'alpha': {'display_name': 'Alpha'}, 'beta': {'display_name': 'Beta'}}}
        runs.save_cycle_plan(self.conn, 'p', 'cycle', 'plan')
        self.plan = runs.set_plan_status(self.conn, 'p', 'cycle', runs.PLAN_ASSESSING)
        self.stories = [{'id': 'a', 'name': 'A', 'description_stripped': 'First'},
                        {'id': 'b', 'name': 'B', 'description_stripped': 'Second'}]
        self.markers = set()
        self.client = Mock()
        self.client.get_issue.return_value = {}
        self.client.set_description.side_effect = lambda p, i, text: self.client.get_issue.return_value.update(
            description_html='<pre>' + html.escape(text) + '</pre>')
        self.client.has_comment_with.side_effect = lambda p, i, m: any(m in x for x in self.markers)
        self.client.comment.side_effect = lambda p, i, text, marker: self.markers.add(marker)
        self.alerts = self.enterContext(patch('dispatch.plans.alerts.send'))

    def round(self, stories=None):
        selected = [{'id': s['id'], 'name': s['name'], 'description': s['description_stripped']}
                    for s in (stories or self.stories)]
        snapshot = runs.save_assessment_snapshot(self.conn, 'p', 'cycle', 'plan', 'a' * 40, selected,
                                                 '2099-01-01', ['alpha', 'beta'])
        runs.queue_assessments(self.conn, snapshot['id'])
        return snapshot

    def answer_all(self, snapshot, loop=False):
        ids = [s['id'] for s in json.loads(snapshot['stories'])]
        for row in runs.assessments(self.conn, snapshot['id']):
            rows = [story(sid, depends_on=['b'] if loop and sid == 'a' and row['agent'] == 'alpha' else
                          ['a'] if loop and sid == 'b' and row['agent'] == 'beta' else []) for sid in ids]
            runs.finish_assessment(self.conn, row['run_id'], answer=answer(rows, ids))

    def post(self):
        plans.post_report(self.conn, self.client, 'p', self.project, self.plan, self.cfg)

    def status(self):
        return runs.cycle_plan(self.conn, 'p', 'cycle')['status']

    def test_waits_until_every_agent_is_in(self):
        snapshot = self.round()
        runs.finish_assessment(self.conn, runs.assessments(self.conn, snapshot['id'])[0]['run_id'],
                               answer=answer([story('a'), story('b')], ['a', 'b']))
        self.post()
        self.client.set_description.assert_not_called()
        self.assertIsNone(runs.plan_report(self.conn, snapshot['id']))
        self.assertEqual(self.status(), runs.PLAN_ASSESSING)

    def test_description_question_status_and_alert(self):
        snapshot = self.answer_round()
        self.post()
        saved = runs.plan_report(self.conn, snapshot['id'])
        self.assertEqual((saved['round'], saved['cycle_id']), (1, 'cycle'))
        self.client.set_description.assert_called_once_with('project', 'plan', saved['report'])
        self.assertIn('Size: Alpha M, Beta M', saved['report'])
        self.assertIn("no Done stories to estimate from yet", saved['report'])
        text, marker = self.client.comment.call_args.args[2], self.client.comment.call_args.kwargs['marker']
        self.assertTrue(text.startswith('OK to begin as scoped?'))
        self.assertIn(plans.question_marker('cycle'), marker)
        self.assertEqual(self.status(), runs.PLAN_PLANNED)
        self.alerts.assert_called_once()
        self.assertEqual(self.alerts.call_args.kwargs['url'], 'https://plane.example/ws/projects/project/issues/plan')
        self.assertEqual(self.alerts.call_args.kwargs['level'], 'active')

    def answer_round(self, loop=False):
        snapshot = self.round()
        self.answer_all(snapshot, loop=loop)
        return snapshot

    def test_replies_are_judged_against_the_new_question(self):
        self.answer_round()
        self.post()
        marker = self.client.comment.call_args.kwargs['marker']
        comments = [{'id': 'old', 'author': 'approver', 'created_at': '1', 'html': '<p>approve</p>'},
                    {'id': 'q', 'author': 'orch', 'created_at': '2', 'html': f'<p>OK? {marker}</p>'}]
        self.client.comments.return_value = comments
        with patch('dispatch.plans.judge') as judge:
            plans.read_replies(self.conn, self.client, 'p', self.project, 'approver', 'orch',
                               runs.cycle_plan(self.conn, 'p', 'cycle'), {}, {})
        judge.assert_not_called()

    def test_restart_after_lost_comment_reply_posts_same_report_once(self):
        snapshot = self.answer_round()

        def lost(p, i, text, marker):
            self.markers.add(marker)
            raise OSError('lost response')
        self.client.comment.side_effect = lost
        with patch('dispatch.plans.datetime') as clock:
            clock.now.return_value = NOW
            with self.assertRaises(OSError):
                self.post()
        first = runs.plan_report(self.conn, snapshot['id'])['report']
        self.assertEqual(self.status(), runs.PLAN_ASSESSING)
        self.conn.close()
        self.conn = runs.connect(self.path)
        # History added after the first attempt must not change the plan already made.
        runs.enqueue(self.conn, project='p', issue={'id': 'done', 'name': 'Done story'}, agent='alpha', branch='b')
        runs.record_done_story(self.conn, 'p', 'done')
        self.post()
        self.assertEqual(self.client.comment.call_count, 1)
        self.client.set_description.assert_called_once_with('project', 'plan', first)
        self.assertEqual(self.status(), runs.PLAN_PLANNED)
        self.alerts.assert_called_once()

    def test_loop_blocks_approve(self):
        self.answer_round(loop=True)
        self.post()
        self.assertIn('settle the dependency loop', self.client.comment.call_args.args[2])
        record = plans.judge(self.conn, self.client, 'p', self.project, runs.cycle_plan(self.conn, 'p', 'cycle'), 'c1',
                             'approve', {}, {})
        self.assertEqual(record['reply'], sync.PLAN_HAS_LOOPS)
        self.answer_round_without_loop()
        record = plans.judge(self.conn, self.client, 'p', self.project, runs.cycle_plan(self.conn, 'p', 'cycle'), 'c2',
                             'approve', {}, {})
        self.assertIsNone(record['reply'])

    def answer_round_without_loop(self):
        with self.conn:
            self.conn.execute("INSERT INTO assessment_requests VALUES ('p', 'cycle')")
        self.answer_all(self.round())
        runs.set_plan_status(self.conn, 'p', 'cycle', runs.PLAN_ASSESSING)
        self.post()

    def cycle_poll(self, stories):
        with patch('dispatch.plans.read_replies'), patch('dispatch.plans.assessments.start') as start:
            plans.plan_cycles(self.conn, self.client, 'p', self.project, 'approver', {'cycle': {'id': 'cycle'}},
                              {'cycle': []}, {'cycle': stories}, self.cfg)
        return start

    def age_change(self, minutes):
        since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(timespec='seconds')
        with self.conn:
            self.conn.execute("UPDATE plan_changes SET since = ?", (since,))

    def test_changed_stories_start_a_new_round_once_quiet_and_survive_restart(self):
        first = self.answer_round()
        self.post()
        self.cycle_poll(self.stories)
        self.assertEqual(self.status(), runs.PLAN_PLANNED)
        changed = self.stories + [{'id': 'c', 'name': 'C', 'description_stripped': 'New'}]
        self.cycle_poll(changed)
        self.age_change(10)
        self.conn.close()
        self.conn = runs.connect(self.path)
        self.cycle_poll(changed)
        self.assertEqual(self.status(), runs.PLAN_PLANNED)
        # Another edit restarts the quiet wait.
        changed[2]['description_stripped'] = 'Newer'
        self.cycle_poll(changed)
        self.age_change(14)
        self.cycle_poll(changed)
        self.assertEqual(self.status(), runs.PLAN_PLANNED)
        self.age_change(15)
        start = self.cycle_poll(changed)
        self.assertEqual(self.status(), runs.PLAN_ASSESSING)
        self.assertTrue(runs.assessment_requested(self.conn, 'p', 'cycle'))
        start.assert_called_once()
        # The new round posts its own report and question when every answer is in.
        second = self.round(changed)
        self.assertEqual(second['round'], 2)
        self.answer_all(second)
        self.cycle_poll(changed)
        self.assertEqual(self.status(), runs.PLAN_PLANNED)
        self.assertEqual(self.client.comment.call_count, 2)
        self.assertIn(plans.report_marker(second['id']), self.client.comment.call_args.kwargs['marker'])
        self.assertIn('3. C', runs.plan_report(self.conn, second['id'])['report'])
        self.assertIsNotNone(runs.plan_report(self.conn, first['id']))

    def test_reverted_change_starts_nothing(self):
        self.answer_round()
        self.post()
        self.cycle_poll(self.stories[:1])
        self.age_change(60)
        self.cycle_poll(self.stories)
        self.cycle_poll(self.stories[:1])
        self.assertEqual(self.status(), runs.PLAN_PLANNED)


class History(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.conn = runs.connect(Path(self.temp.name) / 'runs.db')
        self.addCleanup(self.conn.close)

    def test_done_story_timed_from_first_run_and_capped(self):
        for n in range(plan_report.HISTORY + 2):
            issue = f'i{n}'
            run = runs.enqueue(self.conn, project='p', issue={'id': issue, 'name': 'Story'}, agent='alpha',
                               branch=f'b{n}')
            started = (datetime.now(timezone.utc) - timedelta(hours=n + 1)).isoformat(timespec='seconds')
            with self.conn:
                self.conn.execute("UPDATE runs SET claimed_at = ? WHERE id = ?", (started, run))
            runs.record_done_story(self.conn, 'p', issue)
        runs.record_done_story(self.conn, 'p', 'never-ran')
        durations = runs.done_durations(self.conn, 'p', plan_report.HISTORY)
        self.assertEqual(len(durations), plan_report.HISTORY)
        self.assertAlmostEqual(min(durations) / HOUR, 1, delta=0.1)
        self.assertEqual(runs.done_durations(self.conn, 'other', plan_report.HISTORY), [])

    def record_history(self, sizes, hours):
        ids = [f'h{n}' for n in range(len(sizes))]
        snapshot = runs.save_assessment_snapshot(
            self.conn, 'p', 'old-cycle', 'plan', 'rev',
            [{'id': sid, 'name': sid, 'description': ''} for sid in ids], None, ['alpha', 'beta'])
        merged = {'stories': {sid: {'sizes': {'alpha': 'S', 'beta': size}}
                              for sid, size in zip(ids, sizes)}}
        runs.save_plan_report(self.conn, snapshot['id'], {'merged': merged}, 'Historical plan')
        for sid, duration in zip(ids, hours):
            first = runs.enqueue(self.conn, project='p', issue={'id': sid, 'name': sid},
                                 agent='alpha', branch=sid)
            runs.enqueue(self.conn, project='p', issue={'id': sid, 'name': sid},
                         agent='beta', branch=sid + '-retry')
            with self.conn:
                self.conn.execute('UPDATE runs SET claimed_at = ? WHERE id = ?',
                                  ((NOW - timedelta(hours=duration)).isoformat(), first))
            with patch('dispatch.runs.plan_reports.now', return_value=NOW.isoformat()):
                runs.record_done_story(self.conn, 'p', sid)
        return runs.done_durations(self.conn, 'p', plan_report.HISTORY, with_sizes=True)

    def test_recorded_history_missing_sizes_and_disagreement(self):
        history = self.record_history('LLL', [24, 48, 96])
        self.assertEqual({row['size'] for row in history}, {'L'})
        merged = Estimate().merged(sizes='SML')
        est = plan_report.estimate(merged, history, 3, '2026-10-10', NOW)
        self.assertEqual(est['fallback_sizes'], ['S', 'M'])
        self.assertEqual(est['days'], 2)
        self.assertEqual(est['slow']['days'], 4)
        self.assertTrue(est['fits'])
        self.assertFalse(est['slow']['fits'])
        self.assertEqual(est['slow']['finish'], '2026-10-11')
        self.assertIn('Too little size history: S, M.', plan_report.render(merged, est, {}, 1))

    def test_all_sizes_learned_and_larger_current_size_used(self):
        history = self.record_history('SSSMMMLLL', [1, 2, 3, 10, 20, 30, 100, 200, 300])
        merged = Estimate().merged(sizes='SML', depends={'b': ['a'], 'c': ['b']})
        merged['stories']['a']['sizes']['beta'] = 'L'
        est = plan_report.estimate(merged, history, 3, None, NOW)
        self.assertEqual(est['fallback_sizes'], [])
        self.assertEqual(est['days'], round(420 / 24, 1))
        self.assertEqual(est['slow']['days'], round(630 / 24, 1))
        self.assertIsNone(est['fits'])
        self.assertIsNone(est['slow']['fits'])

    def test_two_samples_still_use_weighting(self):
        history = self.record_history('LL', [24, 48])
        est = plan_report.estimate(Estimate().merged(sizes='LLL'), history, 3, None, NOW)
        self.assertEqual(est['fallback_sizes'], ['S', 'M', 'L'])
        self.assertEqual(est['days'], 3)
        self.assertEqual(est['slow']['days'], 4)

    def test_recorded_history_overlaps_serialize_without_changing_order(self):
        history = self.record_history('MMM', [24, 24, 24])
        merged = plan_report.merge(STORIES, {'alpha': answer(
            [story('a', overlaps=['b', 'c']), story('b', overlaps=['c']), story('c')], list('abc'))})
        est = plan_report.estimate(merged, history, 3, '2026-10-09', NOW)
        self.assertEqual(est['days'], 3)
        self.assertEqual(est['slow']['days'], 3)
        self.assertFalse(est['fits'])
        self.assertEqual(merged['order'], list('abc'))
        self.assertTrue(all(not row['depends_on'] for row in merged['stories'].values()))

    def test_empty_recorded_history_has_no_invented_finish(self):
        history = runs.done_durations(self.conn, 'p', plan_report.HISTORY, with_sizes=True)
        est = plan_report.estimate(Estimate().merged(), history, 2, '2026-10-14', NOW)
        text = plan_report.fit_lines(est)[0]
        self.assertIn('Likely and slow figures, finish dates and fit are unknown', text)
        self.assertIn('Too little size history: S, M, L', text)

    def test_existing_database_seeds_history_from_merges_once(self):
        path = Path(self.temp.name) / 'runs.db'
        run = runs.enqueue(self.conn, project='p', issue={'id': 'old', 'name': 'Old'}, agent='alpha', branch='b')
        with self.conn:
            self.conn.execute("UPDATE runs SET claimed_at = '2026-10-01T00:00:00+00:00' WHERE id = ?", (run,))
            self.conn.execute("DELETE FROM migrations WHERE name = 'done_stories'")
        runs.record_event(self.conn, None, 'moved_to_done', 'Merged', project='p', issue_id='old')
        with self.conn:
            self.conn.execute("UPDATE events SET time = '2026-10-02T00:00:00+00:00' WHERE type = 'moved_to_done'")
        reopened = runs.connect(path)
        self.addCleanup(reopened.close)
        self.assertEqual(runs.done_durations(reopened, 'p', plan_report.HISTORY), [24 * HOUR])
        with reopened:
            reopened.execute("DELETE FROM done_stories")
        runs.connect(path).close()
        self.assertEqual(runs.done_durations(reopened, 'p', plan_report.HISTORY), [])


class PlaneDescription(unittest.TestCase):
    def test_description_is_scrubbed_escaped_and_bounded(self):
        client = plane.Plane('https://plane.example', 'ws', 'fake-token')
        with patch.object(client, 'patch') as write:
            client.set_description('project', 'plan', '<b>' + 'x' * (plane.COMMENT_LIMIT + 5))
        path, payload = write.call_args.args
        self.assertEqual(path, '/projects/project/issues/plan/')
        self.assertTrue(payload['description_html'].startswith('<pre>&lt;b&gt;'))
        self.assertIn('[... truncated ...]', payload['description_html'])


if __name__ == '__main__':
    unittest.main()

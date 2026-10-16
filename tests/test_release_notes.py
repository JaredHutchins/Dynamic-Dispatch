"""Draft release notes written below the plan when a Cycle ends, against a fake board and restartable store."""

from datetime import datetime
import unittest
from unittest import mock

from dispatch import alerts, plane, release_notes, runs, sprint
from test_sprint import SprintBoard


class ReleaseNotes(SprintBoard):
    def setUp(self):
        super().setUp()
        self.approve()
        self.project['states']['cancelled'] = 'cancelled'
        self.project['github_repo'] = 'fake/repo'
        self.cfg['github'] = {'token': 'fake-token'}
        for issue in self.items.values():
            issue.update(state='done', name=f'Story {issue["id"].upper()}')
        self.enterContext(mock.patch.object(sprint.github, 'main_revision', return_value='last-merge'))
        self.enterContext(mock.patch.object(sprint.github, 'token', return_value='fake-token'))
        self.alert = self.enterContext(mock.patch.object(alerts, 'send'))
        self.description = 'Approved report'
        self.client.get_cycle.side_effect = lambda p, c: {'description': self.description}
        self.client.set_cycle_description.side_effect = self.describe
        self.health()
        for issue_id in self.items:
            self.built(issue_id, f'Changed {issue_id}.\n\nMore detail.\n\nDocs: none needed, because {issue_id}')

    def describe(self, p, c, text):
        self.description = text

    def health(self, state='green', revision='last-merge'):
        runs.save_main_health(self.conn, 'project', revision=revision, state=state, since=runs.now(),
                              failing=[], broken_by=None, alert_owed=False)

    def built(self, issue_id, summary, docs=True):
        run_id = runs.enqueue(self.conn, project='project', issue=self.items[issue_id], agent='alpha',
                              branch=issue_id)
        runs.await_review(self.conn, run_id, summary=summary, usage=None, cost_usd=None,
                          pr_url=f'https://github.example/pr/{issue_id}', worktree='w')
        if docs:
            runs.save_docs_line(self.conn, run_id, summary.splitlines()[-1])
        runs.close(self.conn, run_id, runs.DONE)
        return run_id

    def ended(self):
        self.today.now.return_value = sprint.datetime.now.return_value = datetime(2026, 10, 15).astimezone()

    def notes(self):
        self.assertIn('\n\nDraft release notes\n', self.description)
        plan, notes = self.description.split('\n\nDraft release notes\n')
        self.assertEqual(plan, 'Approved report')
        return notes

    def notes_alerts(self):
        return [c for c in self.alert.call_args_list if 'draft release notes' in c.args[3]]


class Ending(ReleaseNotes):
    def test_early_end_writes_notes_in_build_order(self):
        self.start()
        self.client.set_cycle_end.assert_called_once()
        self.client.cycles.return_value[0]['end_date'] = self.client.set_cycle_end.call_args.args[2]
        # An early end already confirmed main, so a later red main doesn't hold the notes.
        self.health('red')
        self.start()
        notes = self.notes()
        self.assertIn('Done, in build order:\n\n'
                      '1. Story B\n'
                      '   Pull request: https://github.example/pr/b\n'
                      '   Changed b.\n'
                      '   Docs: none needed, because b\n\n'
                      '2. Story A\n', notes)
        self.assertLess(notes.index('2. Story A'), notes.index('3. Story C'))
        self.assertNotIn('More detail', notes)
        self.assertIn('Cancelled:\n(none)\n\nCarried over:\n(none)', notes)
        [alert] = self.notes_alerts()
        self.assertEqual(alert.kwargs['level'], 'passive')
        self.assertIn('Sprint 1', alert.args[3])

    def test_normal_end_waits_for_main_check_at_current_main(self):
        self.ended()
        self.health(revision='before-merge')
        self.start()
        self.health('red')
        self.start()
        self.client.set_cycle_description.assert_not_called()
        self.assertTrue(runs.cycle_complete(self.conn, 'project', 'c'))
        self.health()
        self.start()
        self.assertIn('1. Story B', self.notes())
        self.assertEqual(len(self.notes_alerts()), 1)

    def test_not_before_every_story_is_finished(self):
        self.ended()
        self.items['a']['state'] = 'review'
        self.start()
        self.client.set_cycle_description.assert_not_called()
        self.items['a']['state'] = 'done'
        self.start()
        self.client.set_cycle_description.assert_called_once()

    def test_cancelled_and_carried_over_listed_by_title(self):
        self.items['a']['state'] = 'cancelled'
        self.items['c']['state'] = 'sprint'
        self.poll()
        self.ended()
        self.poll()
        self.assertNotIn('c', self.members['c'])
        self.start()
        notes = self.notes()
        self.assertIn('Done, in build order:\n\n1. Story B\n', notes)
        self.assertNotIn('2. ', notes)
        self.assertIn('Cancelled:\n- Story A\n\nCarried over:\n- Story C', notes)

    def test_missing_summary_and_docs_line(self):
        self.conn.execute("DELETE FROM runs WHERE issue_id IN ('a', 'c')")
        self.conn.commit()
        self.built('c', '## Summary\nCombined with the latest main (abc) and re-tested.\n\nOnly a summary.',
                   docs=False)
        with self.conn:
            self.conn.execute("INSERT INTO combine_commits VALUES ('sha', (SELECT MAX(id) FROM runs), 1, ?)",
                              ('Combined with the latest main (abc) and re-tested.',))
        self.ended()
        self.start()
        notes = self.notes()
        self.assertIn('2. Story A\n'
                      '   Pull request: (none recorded)\n'
                      '   (no summary recorded)\n'
                      '   Docs: (no Docs line recorded)\n', notes)
        self.assertIn('3. Story C\n'
                      '   Pull request: https://github.example/pr/c\n'
                      '   Only a summary.\n'
                      '   Docs: (no Docs line recorded)\n', notes)

    def test_later_rework_summary_is_the_final_one(self):
        self.built('b', 'Reworked b.\n\nDocs: updated README.md')
        refresh = runs.enqueue(self.conn, project='project', issue=self.items['b'], agent='alpha', branch='b',
                               kind='refresh', pr_url='https://github.example/pr/b')
        runs.await_review(self.conn, refresh, summary='Main moved on.', usage=None, cost_usd=None,
                          pr_url='https://github.example/pr/b', worktree='w')
        runs.close(self.conn, refresh, runs.DONE)
        self.ended()
        self.start()
        self.assertIn('1. Story B\n   Pull request: https://github.example/pr/b\n   Reworked b.\n'
                      '   Docs: updated README.md\n', self.notes())


class Once(ReleaseNotes):
    def test_restart_never_repeats(self):
        self.ended()
        self.start()
        written = self.description
        self.conn.close()
        self.conn = runs.connect(self.path)
        self.start()
        self.start()
        self.client.set_cycle_description.assert_called_once()
        self.assertEqual(self.description, written)
        self.assertEqual(len(self.notes_alerts()), 1)

    def test_lost_reply_is_not_written_again(self):
        self.ended()
        def lost(p, c, text):
            self.description = text
            raise plane.PlaneError('reply lost')
        self.client.set_cycle_description.side_effect = lost
        self.start()
        self.assertEqual(self.notes_alerts(), [])
        self.start()
        self.client.set_cycle_description.assert_called_once()
        self.assertEqual(self.description.count('Draft release notes'), 1)
        self.assertEqual(len(self.notes_alerts()), 1)

    def test_plane_refusal_warns_and_retries(self):
        self.ended()
        self.client.set_cycle_description.side_effect = plane.PlaneError('refused')
        self.start()
        self.assertEqual(self.notes_alerts(), [])
        self.assertFalse(runs.release_notes_written(self.conn, 'project', 'c'))
        self.assertTrue(self.conn.execute("SELECT 1 FROM events WHERE type = 'warning' "
                                          "AND detail LIKE '%release notes%refused%'").fetchone())
        self.client.set_cycle_description.side_effect = self.describe
        self.start()
        self.assertIn('1. Story B', self.notes())
        self.assertEqual(len(self.notes_alerts()), 1)

    def test_cycles_ended_before_the_upgrade_get_none(self):
        self.ended()
        runs.set_plan_status(self.conn, 'project', 'c', runs.PLAN_COMPLETE)
        with self.conn:
            self.conn.execute("DELETE FROM migrations WHERE name = 'release_notes'")
        self.conn.close()
        self.conn = runs.connect(self.path)
        self.start()
        self.client.set_cycle_description.assert_not_called()
        self.assertEqual(self.notes_alerts(), [])


class FirstParagraph(unittest.TestCase):
    def test_copied_unchanged(self):
        self.assertEqual(release_notes.first_paragraph('Line one\nline two.\n\nLater.', None, set()),
                         'Line one\nline two.')
        self.assertEqual(release_notes.first_paragraph('Docs: updated README.md', 'Docs: updated README.md',
                                                       set()), None)
        self.assertIsNone(release_notes.first_paragraph(None, None, set()))

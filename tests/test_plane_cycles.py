"""The Plane client's Cycle and comment calls, against a faked Plane API."""
import unittest
from unittest import mock

import helpers  # noqa: F401  (puts the repository on the path)
from dispatch import plane, redact


class PlaneCycles(unittest.TestCase):
    def setUp(self):
        self.client = plane.Plane('https://plane.invalid', 'workspace', 'fake-token')
        self.request = self.enterContext(mock.patch.object(self.client, '_request', return_value={}))

    def test_cycles_have_dates_and_follow_pages(self):
        self.request.side_effect = [
            {'results': [{'id': 'c1', 'name': 'Sprint 1', 'start_date': '2026-10-01',
                          'end_date': '2026-10-14', 'owned_by': 'user'}],
             'next_page_results': True, 'next_cursor': 'next'},
            {'results': [{'id': 'c2', 'name': 'Draft'}]}]
        self.assertEqual(self.client.cycles('project'), [
            {'id': 'c1', 'name': 'Sprint 1', 'start_date': '2026-10-01', 'end_date': '2026-10-14'},
            {'id': 'c2', 'name': 'Draft', 'start_date': None, 'end_date': None}])
        self.assertEqual(self.request.call_args_list, [
            mock.call('GET', '/projects/project/cycles/?per_page=100'),
            mock.call('GET', '/projects/project/cycles/?per_page=100&cursor=next')])

    def test_set_cycle_end_preserves_start(self):
        self.client.set_cycle_end('project', 'cycle', '2026-10-14T04:59:59.999999+00:00')
        self.request.assert_called_once_with('PATCH', '/projects/project/cycles/cycle/',
                                            {'end_date': '2026-10-14T04:59:59.999999+00:00'})

    def test_get_cycle(self):
        self.request.return_value = {'id': 'cycle', 'description': 'Approved', 'start_date': '2026-10-01'}
        self.assertEqual(self.client.get_cycle('project', 'cycle'), self.request.return_value)
        self.request.assert_called_once_with('GET', '/projects/project/cycles/cycle/')

    def test_cycle_issues(self):
        self.request.return_value = {'results': [{'id': 'i1', 'state': 's'}]}
        self.assertEqual(self.client.cycle_issues('project', 'cycle'), [{'id': 'i1', 'state': 's'}])
        self.request.assert_called_once_with('GET', '/projects/project/cycles/cycle/cycle-issues/?per_page=100')

    def test_set_labels(self):
        for labels in (['first', 'second'], []):
            with self.subTest(labels=labels):
                self.request.reset_mock()
                self.client.set_labels('project', 'issue', labels)
                self.request.assert_called_once_with(
                    'PATCH', '/projects/project/issues/issue/', {'labels': labels})

    def test_add_to_cycle(self):
        self.client.add_to_cycle('project', 'cycle', 'issue')
        self.request.assert_called_once_with(
            'POST', '/projects/project/cycles/cycle/cycle-issues/', {'issues': ['issue']})

    def test_remove_from_cycle_keeps_the_work_item(self):
        self.client.remove_from_cycle('project', 'cycle', 'issue')
        self.request.assert_called_once_with('DELETE', '/projects/project/cycles/cycle/cycle-issues/issue/')

    def test_delete_issue(self):
        self.client.delete_issue('project', 'issue')
        self.request.assert_called_once_with('DELETE', '/projects/project/issues/issue/')

    def test_cycle_description_is_scrubbed_and_bounded(self):
        secret = 'fake-cycle-secret'
        redact.set_secrets([secret])
        self.addCleanup(redact.set_secrets, [])
        self.client.set_cycle_description('project', 'cycle', f'Plan <b>\n{secret}')
        method, path, payload = self.request.call_args.args
        self.assertEqual((method, path), ('PATCH', '/projects/project/cycles/cycle/'))
        self.assertTrue(payload['description'].startswith('Plan <b>\n'))
        self.assertNotIn(secret, payload['description'])
        self.client.set_cycle_description('project', 'cycle', 'x' * (plane.COMMENT_LIMIT + 50))
        description = self.request.call_args.args[2]['description']
        self.assertEqual(description, 'x' * plane.COMMENT_LIMIT + '\n[... truncated ...]')

    def test_comments_have_author_time_and_html(self):
        self.request.side_effect = [
            {'results': [{'id': 'm2', 'actor': {'id': 'approver'}, 'created_at': '2026-10-02T09:00:00Z',
                          'comment_html': '<p>later</p>'},
                         {'id': 'm1', 'created_by': 'orchestrator', 'created_at': '2026-10-01T09:00:00Z',
                          'comment_html': '<p>first</p>'}],
             'next_page_results': True, 'next_cursor': 'next'},
            {'results': [{'id': 'm3', 'actor': 'agent', 'created_at': '2026-10-03T09:00:00Z'}]}]
        self.assertEqual(self.client.comments('project', 'issue'), [
            {'id': 'm1', 'author': 'orchestrator', 'created_at': '2026-10-01T09:00:00Z', 'html': '<p>first</p>'},
            {'id': 'm2', 'author': 'approver', 'created_at': '2026-10-02T09:00:00Z', 'html': '<p>later</p>'},
            {'id': 'm3', 'author': 'agent', 'created_at': '2026-10-03T09:00:00Z', 'html': ''}])
        self.assertEqual(self.request.call_args_list[0],
                         mock.call('GET', '/projects/project/issues/issue/comments/?per_page=100'))

    def test_errors_reach_the_caller(self):
        self.request.side_effect = plane.PlaneError('gone', status=404)
        for call in (lambda: self.client.cycles('project'),
                     lambda: self.client.remove_from_cycle('project', 'cycle', 'issue'),
                     lambda: self.client.comments('project', 'issue')):
            with self.assertRaises(plane.PlaneError):
                call()


class Delete(unittest.TestCase):
    def test_delete_sends_no_body(self):
        client = plane.Plane('https://plane.invalid', 'workspace', 'fake-token')
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b''
        with mock.patch.object(plane.Plane, '_request', helpers.real_plane_request), \
                mock.patch('urllib.request.urlopen', return_value=response) as urlopen, \
                mock.patch.object(plane.logins, 'login'):
            self.assertEqual(client.delete('/projects/project/issues/issue/'), {})
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_method(), 'DELETE')
        self.assertIsNone(request.data)
        self.assertEqual(request.full_url,
                         'https://plane.invalid/api/v1/workspaces/workspace/projects/project/issues/issue/')


if __name__ == '__main__':
    unittest.main()

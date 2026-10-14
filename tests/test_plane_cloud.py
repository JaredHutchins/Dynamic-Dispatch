"""Replay Plane responses without network access or real credentials."""
import io
import json
from pathlib import Path
import unittest
from unittest import mock
from urllib.error import HTTPError

import helpers
from dispatch import plane


RESPONSES = json.loads((Path(__file__).parent / 'samples/plane/responses.json').read_text())


class PlaneCloud(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.object(plane.Plane, '_request', helpers.real_plane_request))
        self.enterContext(mock.patch.object(plane.logins, 'login'))
        self.sleep = self.enterContext(mock.patch.object(plane.time, 'sleep'))
        self.urlopen = self.enterContext(mock.patch('urllib.request.urlopen'))
        self.client = plane.Plane('https://api.plane.so', 'workspace', 'fake-token')

    def response(self, name, headers=None):
        sample = RESPONSES[name]
        body = io.BytesIO(json.dumps(sample['body']).encode())
        self.addCleanup(body.close)
        if sample['status'] >= 400:
            return HTTPError('https://api.plane.so', sample['status'], 'error',
                             sample.get('headers', {}) if headers is None else headers, body)
        return body

    def test_user_agent_for_every_method_and_identity(self):
        for method in ('GET', 'POST', 'PATCH', 'DELETE'):
            with self.subTest(method=method):
                self.urlopen.return_value = self.response('issue')
                self.client._request(method, '/projects/project/issues/',
                                     {'name': 'example'} if method in ('POST', 'PATCH') else None)
                req = self.urlopen.call_args.args[0]
                self.assertTrue(req.get_header('User-agent').startswith('dynamic-dispatch/'))
                self.assertEqual(req.get_header('X-api-key'), 'fake-token')
        self.urlopen.return_value = io.BytesIO(b'{"id":"fake-user"}')
        self.client.current_user()
        self.assertEqual(self.urlopen.call_args.args[0].full_url, 'https://api.plane.so/api/v1/users/me/')
        self.assertTrue(self.urlopen.call_args.args[0].get_header('User-agent').startswith('dynamic-dispatch/'))

    def test_api_address_and_web_links_with_and_without_override(self):
        for override in (False, True):
            p = {'base_url': 'https://app.plane.so/', 'workspace_slug': 'workspace',
                 'orchestrator_token': 'fake-token'}
            if override:
                p['api_url'] = 'https://api.plane.so/'
            cfg = {'plane': p}
            with mock.patch.object(plane, '_assignments', object()):
                clients = [plane.orchestrator(cfg), plane.as_agent(cfg, {'plane_token': 'fake-agent'})]
            for client in clients:
                self.urlopen.return_value = self.response('issue')
                issue = client.get_issue('project', 'fake-issue')
                origin = 'https://api.plane.so' if override else 'https://app.plane.so'
                self.assertEqual(self.urlopen.call_args.args[0].full_url,
                                 origin + '/api/v1/workspaces/workspace/projects/project/issues/fake-issue/')
                self.assertEqual(plane.work_item_url(cfg, 'project', issue['id']),
                                 'https://app.plane.so/workspace/projects/project/issues/fake-issue')

    def test_self_hosted_member_request(self):
        self.urlopen.return_value = self.response('member_added')
        self.client.add_member('project', 'user')
        self.assertEqual(self.urlopen.call_count, 1)
        req = self.urlopen.call_args.args[0]
        self.assertIn('/api/v1/', req.full_url)
        self.assertEqual(json.loads(req.data), {'member': 'user', 'role': 20})

    def test_cloud_member_fallback(self):
        for role, name in ((20, 'admin'), (15, 'contributor'), (5, 'guest')):
            self.urlopen.reset_mock()
            self.urlopen.side_effect = [self.response('cloud_member_refusal'), self.response('member_added')]
            self.client.add_member('project', 'user', role)
            first, second = [call.args[0] for call in self.urlopen.call_args_list]
            self.assertEqual(json.loads(first.data), {'member': 'user', 'role': role})
            self.assertEqual(second.full_url,
                             'https://api.plane.so/api/v2/workspaces/workspace/projects/project/members/')
            self.assertEqual(json.loads(second.data), {'member_id': 'user', 'role': name})
            self.assertTrue(second.get_header('User-agent').startswith('dynamic-dispatch/'))

    def test_other_member_failures_do_not_fallback(self):
        for status in (400, 401, 403, 404, 500):
            self.urlopen.reset_mock()
            self.urlopen.side_effect = HTTPError('https://api.plane.so', status, 'error', {}, io.BytesIO(b'other'))
            with self.assertRaises(plane.PlaneError) as error:
                self.client.add_member('project', 'user')
            self.assertEqual(error.exception.status, status)
            self.assertEqual(self.urlopen.call_count, 1)

    def test_rate_limits_retry_same_request(self):
        for sample, headers, delay in (
                ('limited', None, 12), ('limited_without_delay', None, 5),
                ('limited', {'Retry-After': 'bad'}, 5),
                ('limited', {'Retry-After': '60'}, 60),
                ('limited', {'Retry-After': 'Thu, 01 Jan 1970 00:00:12 GMT'}, 12)):
            self.sleep.reset_mock()
            self.urlopen.reset_mock()
            self.urlopen.side_effect = [self.response(sample, headers), self.response('member_added')]
            with mock.patch.object(plane.time, 'time', return_value=0):
                self.client.add_member('project', 'user')
            self.sleep.assert_called_once_with(delay)
            self.assertIs(self.urlopen.call_args_list[0].args[0], self.urlopen.call_args_list[1].args[0])

    def test_rate_limit_exhaustion_reports_original_failure(self):
        self.urlopen.side_effect = [self.response('limited_without_delay') for _ in range(4)]
        with self.assertRaises(plane.PlaneError) as error:
            self.client.get('/projects/')
        self.assertEqual(error.exception.status, 429)
        self.assertIn('Request was throttled.', str(error.exception))
        self.assertEqual(self.urlopen.call_count, 4)
        self.assertEqual(self.sleep.call_count, 3)

    def test_rate_limit_long_wait_reports_failure_without_retry(self):
        for value in ('61', '3600', 'Thu, 01 Jan 1970 00:01:01 GMT'):
            with self.subTest(retry_after=value):
                self.sleep.reset_mock()
                self.urlopen.reset_mock()
                self.urlopen.side_effect = [
                    self.response('limited', {'Retry-After': value}) for _ in range(4)]
                with mock.patch.object(plane.time, 'time', return_value=0):
                    with self.assertRaises(plane.PlaneError) as error:
                        self.client.get('/projects/')
                self.assertEqual(error.exception.status, 429)
                self.assertIn('Request was throttled.', str(error.exception))
                self.assertEqual(self.urlopen.call_count, 1)
                self.sleep.assert_not_called()

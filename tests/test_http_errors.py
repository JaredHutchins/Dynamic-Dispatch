"""HTTP response ownership, including failures while reading and unused fakes."""
import io
import unittest
from unittest import mock

import helpers
from dispatch import plane, plane_apps, publicupdate


class HttpErrors(unittest.TestCase):
    def test_unused_fake_is_closed_by_cleanup(self):
        case = unittest.TestCase()
        error = helpers.http_error(case, 'https://service.invalid', 503, 'unavailable', {}, io.BytesIO())
        self.addCleanup(error.close)
        case.doCleanups()
        self.assertTrue(error.closed)

    def test_public_update_closes_and_reraises_same_error(self):
        error = helpers.http_error(self, 'https://github.invalid', 503, 'unavailable', {}, io.BytesIO())
        with mock.patch('urllib.request.urlopen', side_effect=error):
            with self.assertRaises(type(error)) as raised:
                publicupdate._read('example/repo', 'commits/main')
        self.assertIs(raised.exception, error)
        self.assertTrue(error.closed)

    def test_clients_close_errors_when_body_read_fails(self):
        client = plane.Plane('https://plane.invalid', 'workspace', 'fake-token')
        bot = dict(client_id='fake-client', client_secret='fake-secret', app_installation_id='fake-install')
        calls = (
            lambda: helpers.real_gh_request('GET', '/test', 'fake-token'),
            lambda: helpers.real_plane_request(client, 'GET', '/test'),
            lambda: plane_apps._fetch('https://plane.invalid', bot),
        )
        for call in calls:
            with self.subTest(call=call):
                error = helpers.http_error(self, 'https://service.invalid', 503, 'unavailable', {}, io.BytesIO())
                failure = OSError('fake read failure')
                with mock.patch('urllib.request.urlopen', side_effect=error), \
                        mock.patch.object(error, 'read', side_effect=failure):
                    with self.assertRaises(OSError) as raised:
                        call()
                self.assertIs(raised.exception, failure)
                self.assertTrue(error.closed)

    def test_plane_closes_rate_limit_before_waiting_to_retry(self):
        client = plane.Plane('https://plane.invalid', 'workspace', 'fake-token')
        error = helpers.http_error(self, 'https://plane.invalid', 429, 'limited',
                                   {'Retry-After': '0'}, io.BytesIO(b'limited'))
        response = self.enterContext(io.BytesIO(b'{}'))
        with mock.patch('urllib.request.urlopen', side_effect=[error, response]) as request, \
                mock.patch.object(plane.time, 'sleep', side_effect=lambda delay: self.assertTrue(error.closed)):
            self.assertEqual(helpers.real_plane_request(client, 'GET', '/test'), {})
        self.assertEqual(request.call_count, 2)

    def test_plane_closes_rejection_before_renewing_token(self):
        bot = dict(client_id='fake-client', client_secret='fake-secret', app_installation_id='fake-install')
        client = plane.Plane('https://plane.invalid', 'workspace', None, bot=bot)
        error = helpers.http_error(self, 'https://plane.invalid', 401, 'rejected', {}, io.BytesIO())
        response = self.enterContext(io.BytesIO(b'{}'))

        def token(*, stale=None):
            if stale is not None:
                self.assertTrue(error.closed)
            return 'fake-token'

        with mock.patch.object(client, '_bot_token', side_effect=token) as refresh, \
                mock.patch('urllib.request.urlopen', side_effect=[error, response]):
            self.assertEqual(helpers.real_plane_request(client, 'GET', '/test'), {})
        self.assertEqual(refresh.call_count, 2)

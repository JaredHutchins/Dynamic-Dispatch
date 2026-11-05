"""Connections to GitHub and Plane that drop after they were opened."""
import http.client
import unittest
import urllib.error
from unittest import mock

import helpers
from dispatch import doctor, github, plane, plane_apps, runs, worker

BOT = dict(client_id='fake-client', client_secret='fake-secret', app_installation_id='fake-install')

# Each kind of drop, where it happens, the wording DD gives it and Python's own text, which it never shows.
DROPS = (
    ('opening', lambda: http.client.RemoteDisconnected('Remote end closed connection without response'),
     '{} closed the connection without replying', 'Remote end closed'),
    ('opening', lambda: ConnectionResetError(104, 'Connection reset by peer'),
     'the connection to {} was reset', 'Connection reset by peer'),
    ('opening', lambda: TimeoutError('The read operation timed out'),
     '{} stopped replying before the time limit', 'read operation timed out'),
    ('opening', lambda: http.client.BadStatusLine('garbled status'),
     "{}'s reply could not be read", 'garbled status'),
    ('reading', lambda: http.client.IncompleteRead(b'{"html_url": "ht', 40),
     "{}'s reply was cut off part-way", 'IncompleteRead'),
    ('reading', lambda: ConnectionResetError(104, 'Connection reset by peer'),
     'the connection to {} was reset', 'Connection reset by peer'),
    ('reading', lambda: TimeoutError('The read operation timed out'),
     '{} stopped replying before the time limit', 'read operation timed out'),
    ('reading', lambda: OSError('fake socket failure'),
     'the connection to {} failed', 'fake socket failure'),
)


class Response:
    """An opened reply whose body never arrives."""

    def __init__(self, failure):
        self.failure = failure
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        raise self.failure


def clients():
    """Each request function, the service it names and the error type it raises."""
    client = plane.Plane('https://plane.invalid', 'workspace', 'fake-token')
    bot_client = plane.Plane('https://plane.invalid', 'workspace', None, bot=BOT, scopes=plane_apps.AGENT_SCOPES)
    return (
        ('GitHub', github.GitHubError, lambda: helpers.real_gh_request('POST', '/repos/o/r/pulls', 'fake-token', {})),
        ('Plane', plane.PlaneError, lambda: helpers.real_plane_request(client, 'GET', '/test')),
        ('Plane', plane_apps.BotLoginError,
         lambda: plane_apps._fetch('https://plane.invalid', BOT, plane_apps.AGENT_SCOPES)),
        ('Plane', plane.PlaneError, lambda: helpers.real_plane_request(bot_client, 'GET', '/test')),
    )


class DroppedConnections(unittest.TestCase):
    def test_every_drop_is_a_network_error_naming_the_service(self):
        for service, kind, call in clients():
            for where, make, wording, raw in DROPS:
                failure = make()
                with self.subTest(kind=kind, failure=repr(failure), where=where):
                    opened = failure if where == 'opening' else Response(failure)
                    with mock.patch('urllib.request.urlopen', side_effect=[opened]) as request:
                        with self.assertRaises(kind) as raised:
                            call()
                    message = str(raised.exception)
                    self.assertIn('network problem: ' + wording.format(service), message)
                    self.assertNotIn(raw, message)
                    self.assertNotIn(type(failure).__name__, message)
                    self.assertIsNone(raised.exception.status)
                    # Tried once, as a connection that can't be made is; retrying is the caller's job.
                    self.assertEqual(request.call_count, 1)
                    # The drop stays the cause, so doctor reports it as unreachable, not a rejection.
                    self.assertEqual(doctor.service_reason(raised.exception, service, 'login', 'other'),
                                     f"Can't reach {service}; check the service URL, network and availability.")
                    self.assertIsNone(getattr(raised.exception, 'sign_in_refused', None))

    def test_connection_that_cannot_be_made_keeps_its_wording(self):
        for service, kind, call in clients():
            with self.subTest(kind=kind):
                with mock.patch('urllib.request.urlopen',
                                side_effect=urllib.error.URLError(ConnectionRefusedError(111, 'Connection refused'))):
                    with self.assertRaises(kind) as raised:
                        call()
                self.assertTrue(str(raised.exception).endswith('-> [Errno 111] Connection refused'),
                                raised.exception)


class DroppedDuringARun(helpers.Base):
    def test_plane_dropping_before_the_push_fails_the_run_without_an_unexpected_error(self):
        check = worker._still_in_progress
        for n, (where, make, wording, raw) in enumerate(DROPS, start=1):
            failure = make()
            with self.subTest(failure=repr(failure), where=where):
                opened = failure if where == 'opening' else Response(failure)

                def dropping(*args):
                    # The last check before the push makes a real request, whose connection drops.
                    with mock.patch.object(plane.Plane, '_request', helpers.real_plane_request), \
                            mock.patch('urllib.request.urlopen', side_effect=[opened]):
                        return check(*args)

                self.add_issue(iid=f'i{n}', seq=n)
                with mock.patch.object(worker, '_still_in_progress', side_effect=dropping):
                    self.cycle()
                row = self.run_rows()[-1]
                self.assertEqual((row['issue_id'], row['status']), (f'i{n}', runs.FAILED), row)
                self.assertIn('Plane could not be reached to confirm the story was still In Progress',
                              row['error'])
                self.assertNotIn('Unexpected error', row['error'])
                self.assertEqual(row['pushed'], 0)
                self.assertEqual(helpers.board.issues[f'i{n}']['state'], {'id': helpers.S_BLK})

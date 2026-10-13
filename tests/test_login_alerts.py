"""Login monitoring uses fake responses and never contacts a service."""
import io
import json
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from dispatch import agents, alerts, github, gitops, logins, plane, runs
import helpers


class LoginAlerts(unittest.TestCase):
    def setUp(self):
        for owner, name, original in ((plane.Plane, "_request", helpers.real_plane_request),
                                      (github, "_request", helpers.real_gh_request)):
            patch = mock.patch.object(owner, name, original)
            patch.start()
            self.addCleanup(patch.stop)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.cfg = {'db_path': str(Path(directory.name) / 'runs.db'),
                    'github': {'token': 'fake-test-token'}}
        self.conn = runs.connect(self.cfg['db_path'])
        self.addCleanup(self.conn.close)
        self.health = alerts.Health(self.cfg)
        patch = mock.patch.object(logins, '_health', self.health)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(alerts, 'send')
        self.send = patch.start()
        self.addCleanup(patch.stop)

    def response(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{}'
        return response

    def assert_cycle(self, call, failure, success, exception):
        for outcome in (failure, failure, success, failure):
            with mock.patch('urllib.request.urlopen', side_effect=outcome if isinstance(outcome, Exception) else None,
                            return_value=outcome):
                if isinstance(outcome, Exception):
                    with self.assertRaises(exception):
                        call()
                else:
                    call()
        self.assertEqual(self.send.call_count, 2)
        self.assertTrue(all(c.kwargs['level'] == 'active' for c in self.send.call_args_list))

    def test_plane_identities_and_codes(self):
        for name in ('Orchestrator', 'Codex'):
            for code in (401, 403):
                self.send.reset_mock()
                client = plane.Plane('http://plane.invalid', 'test', f'fake-{name}-{code}',
                                     'Plane login for ' + name)
                error = urllib.error.HTTPError('fake', code, 'rejected', {}, io.BytesIO())
                self.assert_cycle(lambda: client.get('/test'), error, self.response(), plane.PlaneError)
                self.assertIn(name, self.send.call_args.args[3])

    def test_github_api_and_installation(self):
        for app in (False, True):
            self.send.reset_mock()
            error = urllib.error.HTTPError('fake', 401, 'rejected', {}, io.BytesIO())
            if app:
                cfg = dict(app_id='fake-app', installation_id='fake-install', private_key_path='fake-path')
                call = lambda: github.token(cfg)
            else:
                call = lambda: github._request('GET', '/test', 'fake-test-token')
            response = self.response()
            response.__enter__.return_value.read.return_value = b'{"token":"fake-install-token"}'
            with mock.patch.object(github, '_app_jwt', return_value='fake-jwt'):
                self.assert_cycle(call, error, response, github.GitHubError)

    def test_git_push(self):
        for index, outcome in enumerate((gitops.GitError('Authentication failed'), gitops.GitError('Authentication failed'),
                                        None, gitops.GitError('Permission denied (publickey)'))):
            with mock.patch.object(gitops, '_in_worktree', side_effect=outcome):
                if outcome:
                    with self.assertRaises(gitops.GitError):
                        gitops.push(f'/fake/repo-{index}', '/fake/worktree', 'branch')
                else:
                    gitops.push(f'/fake/repo-{index}', '/fake/worktree', 'branch')
        self.assertEqual(self.send.call_count, 2)

    def test_agent_output(self):
        for parser, messages, success in (
            ('claude_json', ['Invalid API key', 'OAuth token has expired', 'authentication_error',
                             'Please run /login'], '{"result":"done"}'),
            ('codex_json', ['401', 'Unauthorized', 'Failed to refresh token',
                            'Token refresh failed'], '{"type":"turn.completed","usage":{}}'),
        ):
            for message in messages:
                self.send.reset_mock()
                cfg = dict(parser=parser, display_name='Test agent', command=['fake'], run_as=message)
                for failed in (True, True, False, True):
                    result = mock.Mock(returncode=1 if failed else 0, timed_out=False,
                                       stdout=(json.dumps({'is_error': True, 'result': message}) if parser == 'claude_json'
                                               else json.dumps({'type': 'error', 'message': message}))
                                       if failed else success, stderr='', stdout_truncated=False)
                    with mock.patch.object(agents.proc, 'run_sandboxed', return_value=result):
                        agents.run(self.cfg, cfg, 'prompt', '/fake')
                self.assertEqual(self.send.call_count, 2, message)

    def test_transcripts_do_not_report_login_failures(self):
        text = '401 Unauthorized refresh token'
        for parser, stdout in (
            ('claude_json', json.dumps({'is_error': False, 'result': text})),
            ('claude_json', json.dumps({'is_error': True, 'result': 'Tests failed', 'transcript': text})),
            ('codex_json', '\n'.join(json.dumps(event) for event in (
                {'type': 'item.completed', 'item': {'type': 'command_execution', 'aggregated_output': text}},
                {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': text}},
                {'type': 'turn.failed', 'error': {'message': 'Tests failed'}},
            ))),
        ):
            for timed_out in (False, True):
                with self.subTest(parser=parser, timed_out=timed_out):
                    result = mock.Mock(returncode=1, timed_out=timed_out, stdout=stdout,
                                       stderr='', stdout_truncated=False)
                    cfg = dict(parser=parser, display_name='Test agent', command=['fake'])
                    with mock.patch.object(agents.proc, 'run_sandboxed', return_value=result):
                        agents.run(self.cfg, cfg, 'prompt', '/fake')
        self.send.assert_not_called()

    def test_agent_error_channels(self):
        for parser, stdout, stderr in (
            ('claude_json', '', 'Please run /login'),
            ('codex_json', json.dumps({'type': 'turn.failed', 'error': {'message': 'Unauthorized'}}), ''),
        ):
            self.send.reset_mock()
            result = mock.Mock(returncode=1, timed_out=False, stdout=stdout,
                               stderr=stderr, stdout_truncated=False)
            cfg = dict(parser=parser, display_name='Test agent', command=['fake'])
            with mock.patch.object(agents.proc, 'run_sandboxed', return_value=result):
                agents.run(self.cfg, cfg, 'prompt', '/fake')
            self.send.assert_called_once()

    def test_successes_skip_database_and_lock(self):
        self.health.login('recovered', 'Test login', 'API GET', True)
        self.health.login('recovered', 'Test login', 'API GET', False)
        with mock.patch.object(runs, 'connect') as connect, mock.patch.object(self.health, 'lock') as lock:
            for _ in range(3):
                self.health.login('new', 'Test login', 'API GET', False)
                self.health.login('recovered', 'Test login', 'API GET', False)
                with mock.patch('urllib.request.urlopen', return_value=self.response()):
                    plane.Plane('http://plane.invalid', 'test', 'fake-success').get('/test')
                    github._request('GET', '/test', 'fake-success')
            connect.assert_not_called()
            lock.__enter__.assert_not_called()

    def test_only_state_changes_open_database(self):
        with mock.patch.object(runs, 'connect', wraps=runs.connect) as connect:
            for rejected, connections in ((True, 1), (True, 1), (False, 2), (False, 2), (True, 3)):
                self.health.login('test', 'Test login', 'API GET', rejected)
                self.assertEqual(connect.call_count, connections)
        self.assertEqual(self.send.call_count, 2)

    def test_thresholds_and_restart(self):
        expiry = datetime(2030, 6, 1, tzinfo=timezone.utc)
        with mock.patch.object(github, 'token_expiry', return_value=expiry) as request:
            for days in (31, 30, 29, 7, 6, 1, 0):
                at = expiry - timedelta(days=days)
                self.health.check_expiry(at)
                alerts.Health(self.cfg).check_expiry(at)
            self.assertEqual(request.call_count, 7)
        self.assertEqual(self.send.call_count, 3)
        for call, days in zip(self.send.call_args_list, (30, 7, 1)):
            self.assertIn(f'within {days} days', call.args[3])

    def test_unreadable_expiry_warns_daily(self):
        at = datetime(2030, 1, 1, tzinfo=timezone.utc)
        for offset, error in enumerate((ValueError(), OSError(), TypeError())):
            with mock.patch.object(github, 'token_expiry', side_effect=error):
                self.health.check_expiry(at + timedelta(days=offset))
                self.health.check_expiry(at + timedelta(days=offset))
        self.cfg['github'] = {}
        self.health.check_expiry(at + timedelta(days=3))
        self.assertEqual(self.conn.execute("SELECT sum(repeat_count) FROM events WHERE type='warning'").fetchone()[0], 4)
        self.send.assert_not_called()

    def test_rejection_survives_restart_and_other_login_success(self):
        self.health.login('first', 'First login', 'API GET', True)
        restarted = alerts.Health(self.cfg)
        restarted.login('second', 'Second login', 'API GET', False)
        restarted.login('first', 'First login', 'API GET', True)
        self.assertEqual(self.send.call_count, 1)
        restarted.login('first', 'First login', 'API GET', False)
        restarted.login('first', 'First login', 'API GET', True)
        self.assertEqual(self.send.call_count, 2)

    def test_missing_header_warns_without_alert(self):
        response = self.response()
        response.__enter__.return_value.headers = {}
        with mock.patch('urllib.request.urlopen', return_value=response):
            self.health.check_expiry()
        self.send.assert_not_called()
        self.assertEqual(self.conn.execute("SELECT type FROM events").fetchone()[0], 'warning')

    def test_expiry_header(self):
        response = self.response()
        response.__enter__.return_value.headers = {'github-authentication-token-expiration': '2030-06-01 00:00:00 UTC'}
        with mock.patch('urllib.request.urlopen', return_value=response):
            self.assertEqual(github.token_expiry('fake-token'), datetime(2030, 6, 1, tzinfo=timezone.utc))

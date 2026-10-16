"""Plane app (bot) logins, replayed from recorded responses without network access or real credentials."""
import base64
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from urllib.parse import parse_qs

import helpers
import main
from dispatch import alerts, doctor, logins, plane, plane_apps, plane_install, redact, runs


RESPONSES = json.loads((Path(__file__).parent / 'samples/plane/responses.json').read_text())
SECRET = 'fake-app-client-secret'
AGENT_SECRET = 'fake-agent-client-secret'
TOKENS = ('fake-app-access-token-one', 'fake-app-access-token-two')
BOT = {'client_id': 'fake-client-id', 'client_secret': SECRET, 'app_installation_id': 'fake-installation'}
AGENT_BOT = {'client_id': 'fake-agent-client', 'client_secret': AGENT_SECRET,
             'app_installation_id': 'fake-agent-installation'}


def config():
    return {'plane': {'base_url': 'https://app.plane.so', 'api_url': 'https://api.plane.so',
                      'workspace_slug': 'workspace', 'orchestrator_bot': dict(BOT)},
            'github': {},
            'agents': {'claude-code': {'display_name': 'Claude', 'plane_bot': dict(AGENT_BOT)}}}


class AppCase(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.object(plane.Plane, '_request', helpers.real_plane_request))
        self.enterContext(mock.patch.object(plane_apps, '_tokens', {}))
        self.urlopen = self.enterContext(mock.patch('urllib.request.urlopen'))
        self.now = self.enterContext(mock.patch.object(plane_apps.time, 'time', return_value=1000))
        self.addCleanup(redact.set_secrets, [])
        redact.configure(config())

    def response(self, name):
        sample = RESPONSES[name]
        body = io.BytesIO(json.dumps(sample['body']).encode())
        self.addCleanup(body.close)
        if sample['status'] >= 400:
            return helpers.http_error(self, 'https://api.plane.so', sample['status'], 'error', {}, body)
        return body

    def requests(self):
        return [c.args[0] for c in self.urlopen.call_args_list]

    def assert_no_secret(self, *texts):
        for text in texts:
            for secret in (SECRET, AGENT_SECRET, *TOKENS, base64.b64encode(b'fake-client-id:' + SECRET.encode()).decode()):
                self.assertNotIn(secret, str(text))


class Tokens(AppCase):
    def setUp(self):
        super().setUp()
        self.login = self.enterContext(mock.patch.object(plane.logins, 'login'))
        self.client = plane.orchestrator_login(config())

    def test_gets_a_token_with_client_credentials_and_sends_it_as_bearer(self):
        self.urlopen.side_effect = [self.response('app_token'), self.response('app_user')]
        self.assertEqual(self.client.current_user(), 'fake-app-user')
        token, me = self.requests()
        self.assertEqual((token.full_url, token.get_method()), ('https://api.plane.so/auth/o/token/', 'POST'))
        self.assertEqual(token.get_header('Authorization'),
                         'Basic ' + base64.b64encode(f'fake-client-id:{SECRET}'.encode()).decode())
        self.assertEqual(token.get_header('Content-type'), 'application/x-www-form-urlencoded')
        self.assertEqual(parse_qs(token.data.decode(), keep_blank_values=True),
                         {'grant_type': ['client_credentials'], 'app_installation_id': ['fake-installation'],
                          'scope': ['']})
        self.assertEqual(me.full_url, 'https://api.plane.so/api/v1/users/me/')
        self.assertEqual(me.get_header('Authorization'), 'Bearer ' + TOKENS[0])
        self.assertIsNone(me.get_header('X-api-key'))
        self.assertTrue(me.get_header('User-agent').startswith('dynamic-dispatch/'))
        # The token is scrubbed from the moment it arrives.
        self.assertEqual(redact.scrub('Bearer ' + TOKENS[0]), 'Bearer ' + redact.REPLACEMENT)

    def test_reuses_the_token_then_renews_it_before_expiry(self):
        self.urlopen.side_effect = [self.response('app_token'), self.response('app_user')]
        self.client.current_user()
        # A new client for the same app, as each poll makes, shares the token.
        self.now.return_value = 1000 + 36000 - plane_apps.TOKEN_REFRESH_MARGIN - 1
        self.urlopen.side_effect = [self.response('app_user')]
        plane.orchestrator_login(config()).current_user()
        self.assertEqual(self.urlopen.call_count, 3)
        self.now.return_value += 1
        self.urlopen.side_effect = [self.response('app_token_renewed'), self.response('app_user')]
        self.client.current_user()
        renewal, me = self.requests()[3:]
        self.assertTrue(renewal.full_url.endswith('/auth/o/token/'))
        self.assertEqual(me.get_header('Authorization'), 'Bearer ' + TOKENS[1])

    def test_renews_once_after_a_rejection(self):
        self.urlopen.side_effect = [self.response('app_token'), self.response('app_token_rejected'),
                                    self.response('app_token_renewed'), self.response('app_user')]
        self.assertEqual(self.client.current_user(), 'fake-app-user')
        first, rejected, renewal, retried = self.requests()
        self.assertEqual(rejected.get_header('Authorization'), 'Bearer ' + TOKENS[0])
        self.assertTrue(renewal.full_url.endswith('/auth/o/token/'))
        self.assertEqual(retried.get_header('Authorization'), 'Bearer ' + TOKENS[1])
        self.assertFalse(any(c.args[3] for c in self.login.call_args_list))
        # The renewed token is the one kept for later requests.
        self.urlopen.side_effect = [self.response('app_user')]
        self.client.current_user()
        self.assertEqual(self.requests()[-1].get_header('Authorization'), 'Bearer ' + TOKENS[1])

    def test_a_second_rejection_is_not_renewed_again(self):
        self.urlopen.side_effect = [self.response('app_token'), self.response('app_token_rejected'),
                                    self.response('app_token_renewed'), self.response('app_token_rejected')]
        with self.assertRaises(plane.PlaneError) as error:
            self.client.current_user()
        self.assertEqual(error.exception.status, 401)
        self.assertEqual(self.urlopen.call_count, 4)
        self.assertTrue(self.login.call_args.args[3])
        self.assert_no_secret(error.exception)

    def test_forbidden_is_not_renewed(self):
        self.urlopen.side_effect = [self.response('app_token'),
                                    helpers.http_error(self, 'https://api.plane.so', 403, 'error', {}, io.BytesIO(b'{}'))]
        with self.assertRaises(plane.PlaneError) as error:
            self.client.get('/projects/')
        self.assertEqual((error.exception.status, self.urlopen.call_count), (403, 2))

    def test_token_reply_without_a_token_fails_without_caching(self):
        for body in (b'{}', b'{"access_token": "fake-app-access-token-one", "token_type": "mac"}', b'not json'):
            with self.subTest(body=body):
                self.urlopen.side_effect = [io.BytesIO(body)]
                with self.assertRaises(plane.PlaneError):
                    self.client.current_user()
                self.assertEqual(plane_apps._tokens, {})

    def test_network_failure_keeps_its_cause(self):
        self.urlopen.side_effect = [plane.urllib.error.URLError(OSError('fake-unreachable'))]
        with self.assertRaises(plane.PlaneError) as error:
            self.client.current_user()
        self.assertIsInstance(error.exception.__cause__, OSError)
        self.assertIsNone(error.exception.status)
        self.assertFalse(self.login.called)

    def test_personal_tokens_are_unchanged(self):
        cfg = {'plane': {'base_url': 'https://plane.invalid', 'workspace_slug': 'workspace',
                         'orchestrator_token': 'fake-personal-token'}}
        self.urlopen.side_effect = [self.response('app_user')]
        plane.orchestrator_login(cfg).current_user()
        (req,) = self.requests()
        self.assertEqual(req.get_header('X-api-key'), 'fake-personal-token')
        self.assertIsNone(req.get_header('Authorization'))


class RejectedLogin(AppCase):
    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        cfg = {'db_path': str(Path(directory.name) / 'runs.db'), 'github': {}}
        conn = runs.connect(cfg['db_path'])
        self.addCleanup(conn.close)
        self.enterContext(mock.patch.object(logins, '_health', alerts.Health(cfg)))
        self.send = self.enterContext(mock.patch.object(alerts, 'send'))

    def test_rejected_login_raises_the_login_alert_naming_the_identity(self):
        cfg = config()
        for client, name in ((plane.orchestrator_login(cfg), 'Orchestrator'),
                             (plane.as_agent(cfg, cfg['agents']['claude-code']), 'Claude')):
            with self.subTest(name=name):
                self.send.reset_mock()
                body = json.dumps({'error': 'invalid_client', 'echo': SECRET}).encode()
                self.urlopen.side_effect = [helpers.http_error(self, 'https://api.plane.so', 401, 'error', {}, io.BytesIO(body))]
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(stderr):
                    with self.assertRaises(plane.PlaneError) as error:
                        client.current_user()
                self.assertEqual(error.exception.status, 401)
                self.assertEqual(self.send.call_count, 1)
                message = self.send.call_args.args[3]
                self.assertIn(f'Plane login for {name} was rejected', message)
                self.assertIn('Plane app token request (HTTP 401)', message)
                self.assert_no_secret(message, error.exception, stderr.getvalue())
                self.assertIn(redact.REPLACEMENT, str(error.exception))

    def test_success_rearms_the_alert(self):
        client = plane.orchestrator_login(config())
        for name in ('app_login_rejected', 'app_login_rejected'):
            self.urlopen.side_effect = [self.response(name)]
            with self.assertRaises(plane.PlaneError):
                client.current_user()
        self.urlopen.side_effect = [self.response('app_token'), self.response('app_user')]
        client.current_user()
        self.assertEqual(self.send.call_count, 1)


class InstallLink(unittest.TestCase):
    def write(self, cfg):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / 'config.json'
        path.write_text(json.dumps(cfg))
        path.chmod(0o600)
        return path

    def run_command(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            result = plane_install.run(*args)
        return result, out.getvalue()

    def test_orchestrator_link_before_the_installation_id_is_known(self):
        cfg = config()
        del cfg['plane']['orchestrator_bot']['app_installation_id']
        result, out = self.run_command(self.write(cfg), 'orchestrator')
        self.assertEqual(result, 0)
        self.assertIn('https://api.plane.so/auth/o/authorize-app/?client_id=fake-client-id&response_type=code'
                      '&redirect_uri=http%3A%2F%2Flocalhost%2F&scope=' +
                      '%20'.join(s.replace(':', '%3A') for s in plane_apps.ORCHESTRATOR_SCOPES), out)
        self.assertIn('projects.work_items.comments:write profile:read', out)
        self.assertIn('app_installation_id=', out)
        self.assertIn('plane.orchestrator_bot.app_installation_id', out)
        self.assertNotIn(SECRET, out)

    def test_agent_link_asks_only_for_what_agents_use(self):
        result, out = self.run_command(self.write(config()), 'claude-code', 'https://example.com/back')
        self.assertEqual(result, 0)
        self.assertIn('client_id=fake-agent-client&response_type=code'
                      '&redirect_uri=https%3A%2F%2Fexample.com%2Fback&scope=', out)
        self.assertIn('permissions: ' + ' '.join(plane_apps.AGENT_SCOPES), out)
        self.assertNotIn('projects:write', out)
        self.assertIn('agents.claude-code.plane_bot.app_installation_id', out)
        self.assertNotIn(AGENT_SECRET, out)

    def test_refusals_never_show_the_secret(self):
        cfg = config()
        cfg['agents']['codex'] = {'plane_token': 'fake-codex-token'}
        path = self.write(cfg)
        for args in ((path, 'nobody'), (path, 'codex'), (path, 'orchestrator', 'ftp://localhost/'),
                     (Path(path).parent / 'missing.json', 'orchestrator')):
            with self.subTest(args=args):
                result, out = self.run_command(*args)
                self.assertEqual(result, 1)
                self.assertNotIn(SECRET, out)
                self.assertNotIn('fake-codex-token', out)
        path.chmod(0o644)
        self.assertEqual(self.run_command(path, 'orchestrator')[0], 1)

    def test_main_command(self):
        path = self.write(config())
        out = io.StringIO()
        with mock.patch('sys.argv', ['main.py', '--config', str(path), '--plane-install-link', 'orchestrator']), \
                contextlib.redirect_stdout(out):
            self.assertEqual(main.main(), 0)
        self.assertIn('redirect_uri=http%3A%2F%2Flocalhost%2F', out.getvalue())
        self.assertNotIn(SECRET, out.getvalue())


class DoctorCheck(AppCase):
    def run_doctor(self):
        cfg = dict(config(), db_path='fake-path', projects={})
        out = io.StringIO()
        with mock.patch.object(doctor.config, 'load', return_value=cfg), \
                mock.patch.object(Path, 'stat', return_value=mock.Mock(st_mode=0o100600)), \
                mock.patch.object(runs, 'check_writable'), \
                mock.patch.object(doctor, '_check_github'), mock.patch.object(doctor, '_check_git'), \
                mock.patch.object(doctor, '_check_agents', return_value={}), \
                mock.patch.object(doctor.doctor_sandbox, 'check'), mock.patch.object(doctor, '_check_services'), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            doctor.run()
        return out.getvalue()

    def route(self, rejected=()):
        def urlopen(req, timeout=None):
            if req.full_url.endswith('/auth/o/token/'):
                who = 'agent' if AGENT_SECRET.encode() in base64.b64decode(req.get_header('Authorization')[6:]) \
                    else 'orchestrator'
                if who in rejected:
                    raise self.response('app_login_rejected')
                return io.BytesIO(json.dumps({'access_token': f'fake-{who}-access-token', 'token_type': 'Bearer',
                                              'expires_in': 36000}).encode())
            return io.BytesIO(json.dumps({'id': 'fake-' + req.get_header('Authorization')[7:]}).encode())
        self.urlopen.side_effect = urlopen

    def test_each_bot_signs_in(self):
        self.route()
        out = self.run_doctor()
        self.assertIn('OK Plane Orchestrator: Plane is reachable and the Orchestrator app signs in.', out)
        self.assertIn('OK Plane agent 1: App signs in.', out)
        self.assert_no_secret(out)
        self.assertNotIn('access-token', out)

    def test_a_rejected_bot_fails(self):
        for who, line in (('orchestrator', 'FAIL Plane Orchestrator: Plane Orchestrator app login was rejected'),
                          ('agent', 'FAIL Plane agent 1: Plane agent app login was rejected')):
            with self.subTest(who=who):
                self.enterContext(mock.patch.object(plane_apps, '_tokens', {}))
                self.route(rejected=(who,))
                out = self.run_doctor()
                self.assertIn(line, out)
                self.assert_no_secret(out)
                self.assertNotIn('invalid_client', out)


if __name__ == '__main__':
    unittest.main()

"""Guided setup, against fake Plane and GitHub replies, never shows or leaks an entered secret."""
import base64
import contextlib
import getpass
import io
import json
import subprocess
import tempfile
import unittest
import urllib.error
import warnings
from pathlib import Path
from unittest import mock

from dispatch import config, github, plane, redact, setup
from helpers import real_gh_request, real_plane_request

# Fake secrets, made up for these tests.
ORCH = 'fake-orchestrator-plane-token'
CLAUDE = 'fake-claude-plane-token'
CODEX = 'fake-codex-plane-token'
WRONG = 'fake-mistyped-plane-token'
GH_TOKEN = 'fake-github-personal-token'
GH_WRONG = 'fake-github-mistyped-token'
INSTALLATION = 'fake-github-installation-token'
DEVICE = 'fake-bark-device-key'
BARK_KEY = 'fake-bark-encryption-key-32chars'
SECRETS = (ORCH, CLAUDE, CODEX, WRONG, GH_TOKEN, GH_WRONG, INSTALLATION, DEVICE, BARK_KEY)
PLANE = 'https://plane.example.com'
APPROVER_ID = '0f1e2d3c-0000-4000-8000-000000000001'
ORCH_APP, CLAUDE_APP = 'fake-orchestrator-app-secret', 'fake-claude-app-secret'
APPS = {ORCH_APP: 'fake-orchestrator-app-access', CLAUDE_APP: 'fake-claude-app-access'}
USERS = {ORCH: 'user-orchestrator', CLAUDE: 'user-claude', CODEX: 'user-codex',
         APPS[ORCH_APP]: 'user-orchestrator-app', APPS[CLAUDE_APP]: 'user-claude-app'}
PERMISSIONS = {'contents': 'read', 'pull_requests': 'write', 'statuses': 'write'}


class Response(io.BytesIO):
    def __init__(self, body):
        super().__init__(json.dumps(body).encode())
        self.headers = {}


class Services:
    """Plane and GitHub as setup sees them. Refusals echo the credential, as a careless server might."""

    def __init__(self, plane_api=PLANE):
        self.plane_api = plane_api
        self.permissions = dict(PERMISSIONS)
        # Permissions for the next installation tokens issued, before falling back to self.permissions.
        self.grants = []
        self.issued = 0

    def refuse(self, url, code, credential):
        return urllib.error.HTTPError(url, code, 'refused', {}, io.BytesIO(f'bad credential {credential}'.encode()))

    def __call__(self, request, timeout=None):
        url = request.full_url
        if url == self.plane_api + '/auth/o/token/':
            # A Plane app's token, for the client secret in its Basic login.
            secret = base64.b64decode(request.get_header('Authorization').removeprefix('Basic ')).decode()
            app = secret.partition(':')[2]
            if app not in APPS:
                raise self.refuse(url, 401, app)
            return Response({'access_token': APPS[app], 'token_type': 'Bearer', 'expires_in': 36000})
        if url.startswith(self.plane_api):
            token = request.get_header('X-api-key') or request.get_header('Authorization').removeprefix('Bearer ')
            if token not in USERS:
                raise self.refuse(url, 401, token)
            if url == self.plane_api + '/api/v1/users/me/':
                return Response({'id': USERS[token]})
            if url.startswith(self.plane_api + '/api/v1/workspaces/workspace/projects/'):
                return Response({'results': [], 'next_page_results': False})
            raise self.refuse(url, 404, token)
        credential = request.get_header('Authorization').removeprefix('Bearer ')
        if url.endswith('/app/installations/42/access_tokens'):
            self.issued += 1
            return Response({'token': INSTALLATION, 'expires_at': '2099-01-01T00:00:00Z',
                             'permissions': self.grants.pop(0) if self.grants else self.permissions})
        if credential not in (GH_TOKEN, INSTALLATION):
            raise self.refuse(url, 401, credential)
        if url.endswith('/user'):
            return Response({'login': 'someone'})
        if url.endswith('/users/reviewer'):
            return Response({'login': 'reviewer'})
        raise self.refuse(url, 404, credential)


class Terminal:
    """Answers prompts in order, noting which were read hidden."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts, self.hidden = [], []

    def input(self, prompt=''):
        self.prompts.append(prompt)
        print(prompt)
        if not self.answers:
            raise EOFError()
        return self.answers.pop(0)

    def getpass(self, prompt='Password: ', stream=None):
        answer = self.input(prompt)
        self.hidden.append(answer)
        return answer


class SetupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(directory.cleanup)
        # A throwaway App key, made for this test run only.
        cls.key = Path(directory.name) / 'app.pem'
        subprocess.run(['openssl', 'genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:2048',
                        '-out', str(cls.key)], check=True, capture_output=True)
        cls.key.chmod(0o600)

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        self.path = self.home / 'checkout' / 'config.json'
        self.path.parent.mkdir()
        self.services = Services()
        # Other tests replace the clients' requests; these run the real clients against fake replies.
        self.enterContext(mock.patch.object(github, '_request', real_gh_request))
        self.enterContext(mock.patch.object(plane.Plane, '_request', real_plane_request))
        self.output = io.StringIO()
        github._installation_tokens.clear()
        self.addCleanup(github._installation_tokens.clear)
        self.addCleanup(redact.set_secrets, [])

    def run_setup(self, answers, *, add_project=0):
        terminal = Terminal(answers)
        with contextlib.redirect_stdout(self.output), contextlib.redirect_stderr(self.output), \
                mock.patch('builtins.input', terminal.input), \
                mock.patch('getpass.getpass', terminal.getpass), \
                mock.patch('urllib.request.urlopen', self.services), \
                mock.patch.object(Path, 'home', return_value=self.home), \
                mock.patch.object(setup, '_account_exists', return_value=False), \
                mock.patch('dispatch.setup.add_project.run', return_value=add_project) as projects, \
                mock.patch('dispatch.setup.doctor.run', return_value=0) as doctor:
            result = setup.run(self.path)
        self.assertEqual(terminal.answers, [], 'every answer was asked for')
        return result, terminal, projects, doctor

    def first_answers(self):
        return [
            PLANE + '/', 'workspace', WRONG,           # a mistyped token is refused...
            '', '', ORCH,                              # ...and asked for again
            'not-an-id', APPROVER_ID,
            'yes', '1234', '42', str(self.key),        # a GitHub App
            'nobody', 'reviewer',                      # an unknown approver is refused
            'claude codex',
            ORCH,                                      # Claude's token can't be the Orchestrator's
            CLAUDE, '', 'claude@example.com',
            CODEX, '', 'not an email', 'codex@example.com',
            'yes', 'http://bark.example.com:8080', DEVICE, 'too short', BARK_KEY,
            'no',                                      # no project yet
        ]

    def assert_no_secret_leaked(self):
        for secret in SECRETS:
            self.assertNotIn(secret, self.output.getvalue())
        for file in self.home.rglob('*'):
            if file.is_file() and file != self.key:
                self.assertEqual(file.stat().st_mode & 0o777, 0o600, file)
                if file != self.path and not file.name.startswith('config.json.backup-'):
                    for secret in SECRETS:
                        self.assertNotIn(secret.encode(), file.read_bytes(), file)

    def backups(self):
        return sorted(self.path.parent.glob('config.json.backup-*'))

    def test_answers_are_checked_and_written_privately(self):
        result, terminal, projects, doctor = self.run_setup(self.first_answers())
        self.assertEqual(result, 0)
        cfg = json.loads(self.path.read_text())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(cfg['plane'], {'base_url': PLANE, 'workspace_slug': 'workspace',
                                        'orchestrator_token': ORCH, 'approver_user_id': APPROVER_ID})
        self.assertEqual(cfg['github'], {'app_id': '1234', 'installation_id': '42',
                                         'private_key_path': str(self.key.resolve()), 'approver': 'reviewer'})
        self.assertEqual(list(cfg['agents']), ['claude-code', 'codex'])
        self.assertEqual(cfg['agents']['claude-code']['plane_token'], CLAUDE)
        self.assertEqual(cfg['agents']['claude-code']['env_file'], '/etc/dynamic-dispatch/claude.env')
        self.assertEqual(cfg['agents']['codex']['git_author'], {'name': 'Codex', 'email': 'codex@example.com'})
        self.assertEqual(cfg['alerts'], {'bark_url': 'http://bark.example.com:8080', 'device_keys': [DEVICE],
                                         'encryption_key': BARK_KEY})
        self.assertEqual(cfg['projects'], {})
        self.assertEqual(config.load(self.path, allow_empty_projects=True)['db_path'],
                         str(self.home / '.local/share/dynamic-dispatch/runs.db'))
        self.assertTrue((self.home / '.local/share/dynamic-dispatch').is_dir())
        self.assertEqual(self.backups(), [])
        # Every secret was typed hidden, and nothing else was.
        self.assertEqual(terminal.hidden, [WRONG, ORCH, ORCH, CLAUDE, CODEX, DEVICE, 'too short', BARK_KEY])
        out = self.output.getvalue()
        self.assertIn('The Orchestrator\'s Plane token was rejected', out)
        self.assertIn('GitHub has no account with that username', out)
        self.assertIn('signs in as the Orchestrator or another agent', out)
        self.assertIn(setup.START_COMMAND, out)
        projects.assert_not_called()
        doctor.assert_called_once_with(self.path)
        self.assert_no_secret_leaked()

    def test_rerun_with_the_same_answers_changes_nothing(self):
        self.run_setup(self.first_answers())
        original = self.path.read_bytes()
        # Enter keeps every value, including the hidden ones.
        result, terminal, _, doctor = self.run_setup([''] * 21)
        self.assertEqual(result, 0)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.backups(), [])
        self.assertIn('Nothing changed', self.output.getvalue())
        self.assertTrue(any('Enter keeps the current one' in p for p in terminal.prompts))
        self.assertFalse(any(secret in p for p in terminal.prompts for secret in SECRETS))
        doctor.assert_called_once()
        self.assert_no_secret_leaked()

    def test_plane_apps_are_kept_and_checked(self):
        self.run_setup(self.first_answers())
        cfg = json.loads(self.path.read_text())
        del cfg['plane']['orchestrator_token'], cfg['agents']['claude-code']['plane_token']
        cfg['plane']['orchestrator_bot'] = {'client_id': 'orchestrator-app', 'client_secret': ORCH_APP,
                                            'app_installation_id': 'orchestrator-install'}
        cfg['agents']['claude-code']['plane_bot'] = {'client_id': 'claude-app', 'client_secret': CLAUDE_APP,
                                                     'app_installation_id': 'claude-install'}
        self.path.write_text(json.dumps(cfg))
        original = self.path.read_bytes()
        # No Plane token is asked for the Orchestrator or Claude.
        result, terminal, _, _ = self.run_setup([''] * 19)
        self.assertEqual(result, 0)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(any("Orchestrator's Plane API token" in p or "Claude's Plane API token" in p
                             for p in terminal.prompts))
        self.assertTrue(any("Codex's Plane API token" in p for p in terminal.prompts))
        out = self.output.getvalue()
        for secret in (ORCH_APP, CLAUDE_APP, *APPS.values()):
            self.assertNotIn(secret, out)
        self.assert_no_secret_leaked()

    def test_plane_cloud_with_an_orchestrator_app(self):
        self.services = Services('https://api.plane.so')
        bot = {'client_id': 'orchestrator-app', 'client_secret': ORCH_APP, 'app_installation_id': 'orchestrator-install'}
        self.path.write_text(json.dumps({'plane': {'orchestrator_bot': bot}, 'projects': {},
                                         'db_path': str(self.home / 'runs.db'),
                                         'sandbox': {'user': 'dd-agent', 'launcher': '/usr/local/bin/dd-agent-run'}}))
        self.path.chmod(0o600)
        answers = [
            'https://app.plane.so', 'workspace', '',  # no token is asked for the Orchestrator's app
            'yes', '1234', '42', str(self.key), 'reviewer',
            'claude codex',
            CLAUDE, '', 'claude@example.com',
            CODEX, '', 'codex@example.com',
            'no',                                     # no alerts
            'no',                                     # no project yet
        ]
        result, terminal, _, _ = self.run_setup(answers)
        self.assertEqual(result, 0)
        cfg = json.loads(self.path.read_text())
        self.assertEqual(cfg['plane'], {'base_url': 'https://app.plane.so', 'api_url': 'https://api.plane.so',
                                        'workspace_slug': 'workspace', 'orchestrator_bot': bot})
        self.assertEqual(config.load(self.path, allow_empty_projects=True)['plane']['orchestrator_bot'], bot)
        self.assertFalse(any("Orchestrator's Plane API token" in p for p in terminal.prompts))
        out = self.output.getvalue()
        for secret in (ORCH_APP, *APPS.values()):
            self.assertNotIn(secret, out)
        self.assert_no_secret_leaked()

    def test_a_rejected_plane_app_stops_setup_without_its_secret(self):
        self.run_setup(self.first_answers())
        cfg = json.loads(self.path.read_text())
        del cfg['agents']['claude-code']['plane_token']
        cfg['agents']['claude-code']['plane_bot'] = {'client_id': 'claude-app', 'client_secret': 'fake-wrong-app-secret',
                                                     'app_installation_id': 'claude-install'}
        self.path.write_text(json.dumps(cfg))
        original = self.path.read_bytes()
        result, _, _, _ = self.run_setup([''] * 10)
        self.assertEqual(result, 1)
        self.assertEqual(self.path.read_bytes(), original)
        out = self.output.getvalue()
        self.assertIn("Setup stopped: Claude's Plane app (plane_bot) was rejected", out)
        self.assertNotIn('fake-wrong-app-secret', out)

    def test_a_change_keeps_the_original_as_a_private_backup(self):
        self.run_setup(self.first_answers())
        original = self.path.read_bytes()
        answers = [''] * 21
        answers[12] = 'other@example.com'  # Claude's commit email
        self.run_setup(answers)
        cfg = json.loads(self.path.read_text())
        self.assertEqual(cfg['agents']['claude-code']['git_author']['email'], 'other@example.com')
        [backup] = self.backups()
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        self.assert_no_secret_leaked()

    def test_existing_values_keep_their_own_form(self):
        cfg = {'plane': {'base_url': PLANE + '/', 'workspace_slug': 'workspace', 'orchestrator_token': ORCH},
               'github': {'app_id': 1234, 'installation_id': 42, 'private_key_path': str(self.key),
                          'token': GH_TOKEN, 'approver': 'reviewer'},
               'sandbox': {'user': 'dd-agent', 'launcher': '/usr/local/bin/dd-agent-run'},
               'agents': {'codex': dict(setup.AGENTS['codex'][1], plane_token=CODEX,
                                        git_author={'name': 'Codex', 'email': 'codex@example.com'})},
               'projects': {}, 'db_path': str(self.home / 'runs.db')}
        self.path.write_text(json.dumps(cfg))
        self.path.chmod(0o600)
        original = self.path.read_bytes()
        # The approver's Plane ID stays unset when skipped.
        self.run_setup([''] * 15)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.backups(), [])
        self.assert_no_secret_leaked()

    def test_plain_token_is_checked_without_showing_it(self):
        answers = [PLANE, 'workspace', ORCH, '',
                   'no', GH_WRONG, GH_TOKEN, 'reviewer',
                   'codex', CODEX, '', 'codex@example.com',
                   'no', 'yes']
        result, _, projects, _ = self.run_setup(answers)
        self.assertEqual(result, 0)
        cfg = json.loads(self.path.read_text())
        self.assertEqual(cfg['github'], {'token': GH_TOKEN, 'approver': 'reviewer'})
        self.assertNotIn('approver_user_id', cfg['plane'])
        self.assertNotIn('alerts', cfg)
        self.assertIn('The GitHub login was rejected', self.output.getvalue())
        projects.assert_called_once_with(self.path)
        self.assert_no_secret_leaked()

    def test_app_with_too_few_permissions_is_refused(self):
        self.services.permissions = {'contents': 'read'}
        answers = self.first_answers()
        del answers[2:5]
        result, terminal, *_ = self.run_setup(answers[:9])
        self.assertEqual(result, 1)
        # Refused, so the App's details are asked for again rather than going on to the approver.
        self.assertTrue(terminal.prompts[-1].startswith('GitHub App ID [1234]'), terminal.prompts[-1])
        self.assertIn('needs contents read, pull requests write and commit statuses write', self.output.getvalue())
        self.assertFalse(self.path.exists())
        self.assert_no_secret_leaked()

    def test_app_is_asked_for_again_until_its_permissions_are_granted(self):
        self.services.grants = [{'contents': 'read'}]
        answers = self.first_answers()
        # Enter keeps the App ID, installation ID and key once the permissions are granted.
        answers[12:12] = ['', '', '']
        result, *_ = self.run_setup(answers)
        self.assertEqual(result, 0)
        self.assertIn('needs contents read, pull requests write and commit statuses write', self.output.getvalue())
        # The second answer was checked with a new token, not the one issued before the grant.
        self.assertEqual(self.services.issued, 2)
        self.assertEqual(json.loads(self.path.read_text())['github']['app_id'], '1234')
        self.assert_no_secret_leaked()

    def test_failed_first_project_is_not_reported_as_done(self):
        answers = self.first_answers()
        answers[-1] = 'yes'
        result, _, projects, doctor = self.run_setup(answers, add_project=1)
        self.assertEqual(result, 1)
        projects.assert_called_once_with(self.path)
        doctor.assert_not_called()
        out = self.output.getvalue()
        self.assertIn('Adding the project did not finish', out)
        self.assertIn('run python3 main.py --add-project', out)
        self.assertNotIn(setup.START_COMMAND, out)
        # The answers already given stay saved, so only the project needs doing again.
        self.assertEqual(json.loads(self.path.read_text())['plane']['orchestrator_token'], ORCH)
        self.assert_no_secret_leaked()

    def test_unsafe_answers_are_refused(self):
        answers = ['https://user:pw@plane.example.com', PLANE, 'work/space']
        self.assertEqual(self.run_setup(answers)[0], 1)
        out = self.output.getvalue()
        self.assertIn('without a login', out)
        self.assertIn('without slashes', out)
        self.assertFalse(self.path.exists())

    def test_secrets_are_never_read_with_echo(self):
        def no_terminal(prompt='', stream=None):
            # What getpass does when it can't turn echo off: warn, then read the answer visibly.
            warnings.warn('Can not control echo on the terminal.', getpass.GetPassWarning)
            return ORCH
        with contextlib.redirect_stdout(self.output), mock.patch('builtins.input', side_effect=[PLANE, 'workspace']), \
                mock.patch('getpass.getpass', no_terminal):
            self.assertEqual(setup.run(self.path), 1)
        self.assertIn('run it in a terminal', self.output.getvalue())
        self.assertEqual(list(self.home.rglob('*')), [self.path.parent])

    def test_cancel_writes_nothing(self):
        result, *_ = self.run_setup(self.first_answers()[:20])
        self.assertEqual(result, 1)
        self.assertIn('Setup cancelled', self.output.getvalue())
        self.assertEqual(list(self.home.rglob('*')), [self.path.parent])
        self.assert_no_secret_leaked()

    def test_public_config_is_refused_untouched(self):
        self.path.write_text('{}')
        self.path.chmod(0o644)
        self.assertEqual(self.run_setup([])[0], 1)
        self.assertIn('chmod 600', self.output.getvalue())
        self.assertEqual(self.path.read_text(), '{}')
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o644)


class SaveTests(unittest.TestCase):
    def test_invalid_candidate_leaves_nothing_behind(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            with self.assertRaises(config.ConfigError):
                config.save(path, {'github': {'token': 'fake-unsaved-secret'}})
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_a_file_changed_meanwhile_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            path.write_text('{}')
            path.chmod(0o600)
            with self.assertRaises(config.ConfigError):
                config.save(path, {}, b'{"old": 1}')
            with self.assertRaises(config.ConfigError):
                config.save(path, {})
            self.assertEqual(list(Path(directory).iterdir()), [path])
            self.assertEqual(path.read_text(), '{}')

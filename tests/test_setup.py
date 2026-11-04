"""Guided setup, against fake Plane and GitHub replies, never shows or leaks an entered secret."""
import contextlib
import getpass
import io
import json
import subprocess
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest import mock

from dispatch import config, github, plane, plane_apps, redact, setup
from helpers import fake_plane_app_token, http_error, real_gh_request, real_plane_request

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
# What each app was installed with: the Orchestrator's list, or an agent's, which can't list projects.
INSTALLS = {ORCH_APP: (plane_apps.ORCHESTRATOR_SCOPES, APPS[ORCH_APP]),
            CLAUDE_APP: (plane_apps.AGENT_SCOPES, APPS[CLAUDE_APP])}
USERS = {ORCH: 'user-orchestrator', CLAUDE: 'user-claude', CODEX: 'user-codex',
         APPS[ORCH_APP]: 'user-orchestrator-app', APPS[CLAUDE_APP]: 'user-claude-app'}
PERMISSIONS = {'contents': 'read', 'pull_requests': 'write', 'statuses': 'write'}


class Response(io.BytesIO):
    def __init__(self, body):
        super().__init__(json.dumps(body).encode())
        self.headers = {}


class Services:
    """Plane and GitHub as setup sees them. Refusals echo the credential, as a careless server might."""

    def __init__(self, test, plane_api=PLANE):
        self.test = test
        self.plane_api = plane_api
        self.permissions = dict(PERMISSIONS)
        # Permissions for the next installation tokens issued, before falling back to self.permissions.
        self.grants = []
        self.issued = 0

    def refuse(self, url, code, credential):
        return http_error(self.test, url, code, 'refused', {}, io.BytesIO(f'bad credential {credential}'.encode()))

    def __call__(self, request, timeout=None):
        url = request.full_url
        if url == self.plane_api + '/auth/o/token/':
            return fake_plane_app_token(self.test, request, INSTALLS)
        if url.startswith(self.plane_api):
            token = request.get_header('X-api-key') or request.get_header('Authorization').removeprefix('Bearer ')
            if token not in USERS:
                raise self.refuse(url, 401, token)
            if url == self.plane_api + '/api/v1/users/me/':
                return Response({'id': USERS[token]})
            if url.startswith(self.plane_api + '/api/v1/workspaces/workspace/projects/'):
                if token == APPS[CLAUDE_APP]:
                    raise self.refuse(url, 403, token)
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
        answer = self.answers.pop(0)
        # An answer can be what happens instead, such as Ctrl+C or a change to the file meanwhile.
        return answer() if callable(answer) else answer

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
        self.progress = self.path.parent / 'config.json.setup-progress'
        self.services = Services(self)
        # Other tests replace the clients' requests; these run the real clients against fake replies.
        self.enterContext(mock.patch.object(github, '_request', real_gh_request))
        self.enterContext(mock.patch.object(plane.Plane, '_request', real_plane_request))
        self.output = io.StringIO()
        plane_apps._tokens.clear()
        self.addCleanup(plane_apps._tokens.clear)
        github._installation_tokens.clear()
        self.addCleanup(github._installation_tokens.clear)
        self.addCleanup(redact.set_secrets, [])

    def run_setup(self, answers, *, add_project=0, accounts=()):
        terminal = Terminal(answers)
        with contextlib.redirect_stdout(self.output), contextlib.redirect_stderr(self.output), \
                mock.patch('builtins.input', terminal.input), \
                mock.patch('getpass.getpass', terminal.getpass), \
                mock.patch('urllib.request.urlopen', self.services), \
                mock.patch.object(Path, 'home', return_value=self.home), \
                mock.patch.object(setup, '_account_exists', side_effect=lambda name: name in accounts), \
                mock.patch('dispatch.setup.add_project.run', return_value=add_project) as projects, \
                mock.patch('dispatch.setup.doctor.run', return_value=0) as doctor:
            result = setup.run(self.path)
        self.assertEqual(terminal.answers, [], 'every answer was asked for')
        return result, terminal, projects, doctor

    def first_answers(self):
        return [
            PLANE + '/', '', 'workspace', 'no', WRONG,           # a mistyped token is refused...
            '', '', '', '', ORCH,                              # ...and asked for again
            'not-an-id', APPROVER_ID,
            'yes', '1234', '42', str(self.key),        # a GitHub App
            'nobody', 'reviewer',                      # an unknown approver is refused
            'claude codex',
            'no', ORCH,                                      # Claude's token can't be the Orchestrator's
            '', CLAUDE, '', 'claude@example.com',
            'no', CODEX, '', 'not an email', 'codex@example.com',
            'yes', 'http://bark.example.com:8080', DEVICE, 'too short', BARK_KEY,
            'no',                                      # no project yet
        ]

    def assert_no_secret_leaked(self):
        for secret in SECRETS:
            self.assertNotIn(secret, self.output.getvalue())
        for file in self.home.rglob('*'):
            if file.is_file() and file != self.key:
                self.assertEqual(file.stat().st_mode & 0o777, 0o600, file)
                if file not in (self.path, self.progress) and not file.name.startswith('config.json.backup-'):
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
        self.assertFalse(self.progress.exists())
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
        result, terminal, _, doctor = self.run_setup([''] * 25)
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
        result, terminal, _, _ = self.run_setup([''] * 29)
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
        self.services = Services(self, 'https://api.plane.so')
        bot = {'client_id': 'orchestrator-app', 'client_secret': ORCH_APP, 'app_installation_id': 'orchestrator-install'}
        self.path.write_text(json.dumps({'plane': {'orchestrator_bot': bot}, 'projects': {},
                                         'db_path': str(self.home / 'runs.db'),
                                         'sandbox': {'user': 'dd-agent', 'launcher': '/usr/local/bin/dd-agent-run'}}))
        self.path.chmod(0o600)
        answers = [
            'https://app.plane.so', '', 'workspace', '', '', '', '', '',  # no token is asked for the Orchestrator's app
            'yes', '1234', '42', str(self.key), 'reviewer',
            'claude codex',
            '', CLAUDE, '', 'claude@example.com',
            'no', CODEX, '', 'codex@example.com',
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

    def test_a_rejected_plane_app_is_asked_again_without_its_secret(self):
        self.run_setup(self.first_answers())
        cfg = json.loads(self.path.read_text())
        del cfg['agents']['claude-code']['plane_token']
        cfg['agents']['claude-code']['plane_bot'] = {
            'client_id': 'claude-app', 'client_secret': 'fake-wrong-app-secret',
            'app_installation_id': 'claude-install'}
        self.path.write_text(json.dumps(cfg))
        answers = [''] * 16 + ['', '', CLAUDE_APP, ''] + [''] * 11
        result, terminal, _, _ = self.run_setup(answers)
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(self.path.read_text())['agents']['claude-code']['plane_bot']['client_secret'],
                         CLAUDE_APP)
        out = self.output.getvalue()
        self.assertIn("Claude's Plane app sign-in was refused (Plane said: invalid_client); "
                      "check its Client ID, Client Secret and Installation ID.", out)
        self.assertNotIn('address and workspace slug', out)
        self.assertNotIn('fake-wrong-app-secret', out)
        self.assertIn(CLAUDE_APP, terminal.hidden)
        self.assert_no_secret_leaked()

    def test_choose_bots_from_scratch(self):
        answers = [PLANE, '', 'workspace', 'yes', 'orchestrator-app', ORCH_APP, 'orchestrator-install',
                   '', 'no', GH_TOKEN, 'reviewer', 'claude',
                   'yes', 'claude-app', CLAUDE_APP, 'claude-install', '', 'claude@example.com', 'no', 'no']
        result, terminal, _, _ = self.run_setup(answers)
        self.assertEqual(result, 0)
        cfg = config.load(self.path, allow_empty_projects=True)
        self.assertEqual(cfg['plane']['orchestrator_bot']['client_secret'], ORCH_APP)
        self.assertEqual(cfg['agents']['claude-code']['plane_bot']['client_secret'], CLAUDE_APP)
        self.assertNotIn('orchestrator_token', cfg['plane'])
        self.assertNotIn('plane_token', cfg['agents']['claude-code'])
        self.assertEqual(terminal.hidden, [ORCH_APP, GH_TOKEN, CLAUDE_APP])
        for secret in (ORCH_APP, CLAUDE_APP, *APPS.values()):
            self.assertNotIn(secret, self.output.getvalue())
        self.assert_no_secret_leaked()

    def test_an_agent_app_that_cannot_list_projects_is_accepted(self):
        # Plane answers 403 to the Claude app listing projects; setup checks it with who-am-I alone.
        answers = [PLANE, '', 'workspace', 'yes', 'orchestrator-app', ORCH_APP, 'orchestrator-install',
                   '', 'no', GH_TOKEN, 'reviewer', 'claude',
                   'yes', 'claude-app', CLAUDE_APP, 'claude-install', '', 'claude@example.com', 'no', 'no']
        result, terminal, _, _ = self.run_setup(answers)
        self.assertEqual(result, 0)
        self.assertEqual(terminal.hidden, [ORCH_APP, GH_TOKEN, CLAUDE_APP])
        out = self.output.getvalue()
        self.assertNotIn('rejected', out)
        self.assertNotIn('refused', out)

    def test_custom_api_address_and_rejected_orchestrator_bot(self):
        self.services = Services(self, 'https://api.example.com')
        answers = [PLANE, 'https://api.example.com', 'workspace', 'yes', 'orchestrator-app',
                   WRONG, 'orchestrator-install', '', '', '', '', '', ORCH_APP, '', '',
                   'no', GH_TOKEN, 'reviewer', 'codex', 'no', CODEX, '', 'codex@example.com',
                   'no', 'no']
        result, terminal, _, _ = self.run_setup(answers)
        self.assertEqual(result, 0)
        cfg = config.load(self.path, allow_empty_projects=True)
        self.assertEqual(cfg['plane']['api_url'], 'https://api.example.com')
        self.assertEqual(cfg['plane']['orchestrator_bot']['client_secret'], ORCH_APP)
        self.assertIn("Dynamic Dispatch's Plane app sign-in was refused (Plane said: invalid_client)",
                      self.output.getvalue())
        self.assertEqual(terminal.hidden, [WRONG, ORCH_APP, GH_TOKEN, CODEX])
        self.assertNotIn(ORCH_APP, self.output.getvalue())
        self.assert_no_secret_leaked()

    def test_switch_login_types_removes_old_credentials(self):
        settings = {'plane_token': CLAUDE}
        with mock.patch('builtins.input', side_effect=['yes', 'claude-app', 'claude-install']), \
                mock.patch('getpass.getpass', return_value=CLAUDE_APP):
            setup._plane_identity(settings, 'Claude', 'plane_bot', 'plane_token')
        self.assertNotIn('plane_token', settings)
        self.assertEqual(settings['plane_bot']['client_secret'], CLAUDE_APP)
        with mock.patch('builtins.input', return_value='no'), \
                mock.patch('getpass.getpass', return_value=CLAUDE):
            setup._plane_identity(settings, 'Claude', 'plane_bot', 'plane_token')
        self.assertEqual(settings, {'plane_token': CLAUDE})

    def test_a_change_keeps_the_original_as_a_private_backup(self):
        self.run_setup(self.first_answers())
        original = self.path.read_bytes()
        answers = [''] * 25
        answers[15] = 'other@example.com'  # Claude's commit email
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
        self.run_setup([''] * 18)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.backups(), [])
        self.assert_no_secret_leaked()

    def test_plain_token_is_checked_without_showing_it(self):
        answers = [PLANE, '', 'workspace', 'no', ORCH, '',
                   'no', GH_WRONG, GH_TOKEN, 'reviewer',
                   'codex', 'no', CODEX, '', 'codex@example.com',
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
        del answers[4:9]
        result, terminal, *_ = self.run_setup(answers[:11])
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
        answers[16:16] = ['', '', '']
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
        answers = ['https://user:pw@plane.example.com', PLANE, '', 'work/space']
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
        with contextlib.redirect_stdout(self.output), mock.patch('builtins.input', side_effect=[PLANE, '', 'workspace', 'no']), \
                mock.patch('getpass.getpass', no_terminal):
            self.assertEqual(setup.run(self.path), 1)
        self.assertIn('run it in a terminal', self.output.getvalue())
        self.assertEqual(list(self.home.rglob('*')), [self.path.parent])

    def saved(self):
        return json.loads(self.progress.read_text())['config']

    def test_a_stop_during_plane_saves_nothing(self):
        result, *_ = self.run_setup(self.first_answers()[:9])
        self.assertEqual(result, 1)
        self.assertIn('Setup stopped. Nothing was saved', self.output.getvalue())
        self.assertEqual(list(self.home.rglob('*')), [self.path.parent])
        self.assert_no_secret_leaked()

    def test_end_of_input_keeps_the_sections_before_it(self):
        result, *_ = self.run_setup(self.first_answers()[:20])
        self.assertEqual(result, 1)
        self.assertIn(setup.STOPPED, self.output.getvalue())
        self.assertFalse(self.path.exists())
        saved = self.saved()
        self.assertEqual(saved['plane']['orchestrator_token'], ORCH)
        self.assertEqual(saved['github']['approver'], 'reviewer')
        self.assertNotIn('agents', saved)
        self.assert_no_secret_leaked()

    def test_ctrl_c_keeps_the_sections_before_it(self):
        def interrupt():
            raise KeyboardInterrupt
        answers = self.first_answers()[:14] + [interrupt]  # during the GitHub App's questions
        result, *_ = self.run_setup(answers)
        self.assertEqual(result, 1)
        self.assertIn(setup.STOPPED, self.output.getvalue())
        self.assertFalse(self.path.exists())
        saved = self.saved()
        self.assertEqual(saved['plane'], {'base_url': PLANE, 'workspace_slug': 'workspace',
                                          'orchestrator_token': ORCH, 'approver_user_id': APPROVER_ID})
        self.assertNotIn('github', saved)
        self.assertEqual(self.progress.stat().st_mode & 0o777, 0o600)
        self.assert_no_secret_leaked()

    def failing_agent_login(self, answers):
        with mock.patch.object(plane, 'as_agent', side_effect=RuntimeError('fake-agent-failure')):
            return self.run_setup(answers)

    def test_a_failure_in_the_agents_section_keeps_plane_and_github(self):
        result, *_ = self.failing_agent_login(self.first_answers()[:20])
        self.assertEqual(result, 1)
        out = self.output.getvalue()
        self.assertIn(setup.STOPPED, out)
        self.assertNotIn('fake-agent-failure', out)
        self.assertFalse(self.path.exists())
        saved = self.saved()
        self.assertEqual(saved['plane']['orchestrator_token'], ORCH)
        self.assertEqual(saved['github'], {'app_id': '1234', 'installation_id': '42',
                                           'private_key_path': str(self.key.resolve()), 'approver': 'reviewer'})
        self.assert_no_secret_leaked()

        # The rerun asks every question again, offering the saved answers; Enter keeps them.
        answers = [''] * 11 + ['', 'no', CLAUDE, '', 'claude@example.com',
                                'no', CODEX, '', 'codex@example.com', 'no', 'no']
        result, terminal, _, _ = self.run_setup(answers)
        self.assertEqual(result, 0)
        self.assertTrue(terminal.prompts[0].endswith(f'[{PLANE}]: '), terminal.prompts[0])
        self.assertIn("The Orchestrator's Plane API token [Enter keeps the current one]: ", terminal.prompts)
        self.assertIn('GitHub App ID [1234]: ', terminal.prompts)
        self.assertFalse(any(secret in p for p in terminal.prompts for secret in SECRETS))
        cfg = config.load(self.path, allow_empty_projects=True)
        self.assertEqual(cfg['plane']['orchestrator_token'], ORCH)
        self.assertEqual(cfg['plane']['approver_user_id'], APPROVER_ID)
        self.assertEqual(cfg['github']['approver'], 'reviewer')
        self.assertEqual(cfg['agents']['codex']['plane_token'], CODEX)
        self.assertFalse(self.progress.exists())
        self.assertEqual(self.backups(), [])
        self.assert_no_secret_leaked()

    def test_a_stop_leaves_an_existing_config_as_it_was(self):
        self.run_setup(self.first_answers())
        original = self.path.read_bytes()
        answers = [''] * 5 + ['0f1e2d3c-0000-4000-8000-000000000002'] + [''] * 7
        result, *_ = self.failing_agent_login(answers)
        self.assertEqual(result, 1)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.backups(), [])
        self.assertEqual(self.saved()['plane']['approver_user_id'], '0f1e2d3c-0000-4000-8000-000000000002')
        # Agents in config.json stay in the saved answers too.
        self.assertEqual(self.saved()['agents']['claude-code']['plane_token'], CLAUDE)
        self.assert_no_secret_leaked()

    def test_an_agent_only_in_the_saved_answers_can_be_left_out(self):
        answers = self.first_answers()
        result, *_ = self.run_setup(answers[:27])  # stops during Codex's questions
        self.assertEqual(result, 1)
        self.assertEqual(list(self.saved()['agents']), ['claude-code', 'codex'])
        answers = [''] * 11 + ['claude', '', '', '', '', 'no', 'no']
        result, *_ = self.run_setup(answers)
        self.assertEqual(result, 0)
        self.assertEqual(list(json.loads(self.path.read_text())['agents']), ['claude-code'])

    def test_an_agent_saved_in_the_answers_is_offered_again(self):
        def interrupt():
            raise KeyboardInterrupt
        # Only Codex's account exists, but the user picks Claude, whose section passes before Ctrl+C.
        answers = self.first_answers()
        result, terminal, _, _ = self.run_setup(answers[:18] + ['claude'] + answers[19:25] + [interrupt],
                                                accounts=('dd-codex',))
        self.assertEqual(result, 1)
        self.assertIn('Agents chosen in the installer (claude codex gemini) [codex]: ', terminal.prompts)
        self.assertEqual(list(self.saved()['agents']), ['claude-code'])

        # Enter at the agent question keeps the saved choice, Claude alone, and each of its saved answers.
        result, terminal, _, _ = self.run_setup([''] * 16 + ['no', 'no'], accounts=('dd-codex',))
        self.assertEqual(result, 0)
        self.assertIn('Agents chosen in the installer (claude codex gemini) [claude]: ', terminal.prompts)
        agents = config.load(self.path, allow_empty_projects=True)['agents']
        self.assertEqual(agents['claude-code']['plane_token'], CLAUDE)
        self.assertEqual(agents['claude-code']['git_author']['email'], 'claude@example.com')
        self.assertFalse(self.progress.exists())
        self.assert_no_secret_leaked()

    def test_one_backup_per_run(self):
        self.run_setup(self.first_answers())
        original = self.path.read_bytes()
        answers = [''] * 25
        answers[5] = '0f1e2d3c-0000-4000-8000-000000000002'  # the approver's Plane ID
        answers[15] = 'other@example.com'                    # Claude's commit email
        answers[20] = 'no'                                   # no alerts
        self.assertEqual(self.run_setup(answers[:21] + ['no'])[0], 0)
        cfg = json.loads(self.path.read_text())
        self.assertEqual(cfg['plane']['approver_user_id'], '0f1e2d3c-0000-4000-8000-000000000002')
        self.assertEqual(cfg['agents']['claude-code']['git_author']['email'], 'other@example.com')
        self.assertNotIn('alerts', cfg)
        [backup] = self.backups()
        self.assertEqual(backup.read_bytes(), original)
        self.assertFalse(self.progress.exists())

    def test_a_config_changed_on_disk_meanwhile_is_left_alone(self):
        self.run_setup(self.first_answers())
        edited = json.loads(self.path.read_text())
        edited['poll_interval_seconds'] = 60

        def edit():
            self.path.write_text(json.dumps(edited))
            return ''
        answers = [''] * 25
        answers[15] = 'other@example.com'
        answers[21] = edit  # while the alerts are asked
        result, *_ = self.run_setup(answers[:24])
        self.assertEqual(result, 1)
        out = self.output.getvalue()
        self.assertIn('changed on disk while setup ran, so setup left it as it is', out)
        self.assertIn(setup.STOPPED, out)
        self.assertEqual(json.loads(self.path.read_text()), edited)
        self.assertEqual(self.backups(), [])
        self.assertEqual(self.saved()['agents']['claude-code']['git_author']['email'], 'other@example.com')
        self.assert_no_secret_leaked()

    def test_saved_answers_for_a_config_changed_since_are_not_offered(self):
        self.run_setup(self.first_answers()[:20])
        self.path.write_text('{}')
        self.path.chmod(0o600)
        result, terminal, *_ = self.run_setup([])
        self.assertEqual(result, 1)
        out = self.output.getvalue()
        self.assertIn('changed after setup stopped', out)
        self.assertNotIn(setup.STOPPED, out.split('changed after setup stopped')[1])
        self.assertEqual(terminal.prompts, [])
        self.assertEqual(self.path.read_text(), '{}')
        self.assertTrue(self.progress.exists())

    def test_public_config_is_refused_untouched(self):
        self.path.write_text('{}')
        self.path.chmod(0o644)
        self.assertEqual(self.run_setup([])[0], 1)
        self.assertIn('chmod 600', self.output.getvalue())
        self.assertEqual(self.path.read_text(), '{}')
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o644)


class SaveTests(unittest.TestCase):
    def test_progress_is_private_and_read_back(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            config.save_progress(path, {'plane': {'orchestrator_token': 'fake-saved-secret'}}, None)
            progress = config.progress_path(path)
            self.assertEqual(progress.name, 'config.json.setup-progress')
            self.assertEqual(progress.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(Path(directory).iterdir()), [progress])
            self.assertEqual(config.read_progress(path, None), {'plane': {'orchestrator_token': 'fake-saved-secret'}})
            with self.assertRaises(config.ConfigChanged):
                config.read_progress(path, b'{}')
            progress.chmod(0o644)
            with self.assertRaises(config.ConfigError):
                config.read_progress(path, None)
            config.clear_progress(path)
            self.assertIsNone(config.read_progress(path, None))

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

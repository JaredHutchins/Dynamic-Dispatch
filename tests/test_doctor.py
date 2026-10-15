"""Doctor diagnostics use fake services and never echo service/config secrets."""
import contextlib
import io
import os
from pathlib import Path
import pwd
import re
import shutil
import stat
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock
from datetime import datetime, timedelta, timezone

from dispatch import doctor, github, gitops, plane, proc, runs
import main
from helpers import real_gh_request, real_plane_request


class Doctor(unittest.TestCase):
    def setUp(self):
        self.cfg = {
            'db_path': 'fake-private-path',
            'plane': {'base_url': 'https://plane.invalid', 'workspace_slug': 'workspace',
                      'orchestrator_token': 'fake-orchestrator-credential'},
            'github': {'token': 'fake-personal-credential'},
            'sandbox': {'user': 'fake-private-account', 'launcher': '/fake-private-launcher'},
            'agents': {'fake-private-name': {'plane_token': 'fake-agent-credential',
                                             'command': ['fake-private-command'],
                                             'env_file': '/fake-private-env'}},
            'projects': {'fake-private-project': {'plane_project_id': 'project',
                         'github_repo': 'example/repo', 'repo_path': '/fake-private-repo',
                         'states': {'in_progress': 'state'},
                         'agent_label_id': 'label', 'agents': ['fake-private-name']}},
            'alerts': {'bark_url': 'https://bark.invalid', 'device_keys': ['fake-device-key'],
                       'encryption_key': 'x' * 32}}
        self.client = mock.Mock()
        self.client.current_user.return_value = 'user'
        self.client.states.return_value = [{'id': 'state'}]
        self.client.labels.return_value = [{'id': 'label'}]
        self.client.members.return_value = [{'id': 'user'}]
        patches = [mock.patch.object(doctor.config, 'load', return_value=self.cfg),
                   mock.patch.object(Path, 'stat', return_value=mock.Mock(st_mode=0o100600)),
                   mock.patch.object(runs, 'check_writable'),
                   mock.patch.object(doctor.plane, 'Plane', return_value=self.client),
                   mock.patch.object(github, 'repository', return_value={'permissions': {'push': True}}),
                   mock.patch.object(github, 'token_expiry', return_value=datetime.now(timezone.utc) + timedelta(days=90))]
        self.mocks = [self.enterContext(p) for p in patches]
        # The marker from the image would switch the doctor to its container checks.
        self.enterContext(mock.patch.dict(os.environ))
        os.environ.pop(doctor.CONTAINER_MARKER, None)
        self.host()
        self.fake_host()

    def host(self):
        """A host where DD's service, launcher, sudo rule and agent are all set up."""
        self.me = pwd.getpwuid(os.geteuid()).pw_name
        self.units = {doctor.SERVICE: {'LoadState': 'loaded', 'User': self.me,
                                       'WorkingDirectory': str(doctor.CHECKOUT)},
                      doctor.ROLLBACK_SERVICE: {'LoadState': 'loaded', 'User': '', 'WorkingDirectory': ''}}
        self.sudo = {'fake-private-account': 0}
        self.files = {'/fake-private-env': os.stat_result((stat.S_IFREG | 0o640, 0, 0, 1, 0, 1234, 0, 0, 0, 0)),
                      doctor.ROLLBACK_HELPER: os.stat_result((stat.S_IFREG | 0o755, 0, 0, 1, 0, 0, 0, 0, 0, 0)),
                      doctor.LAUNCHER: os.stat_result((stat.S_IFREG | 0o755, 0, 0, 1, 0, 0, 0, 0, 0, 0)),
                      '/fake-private-launcher': os.stat_result((stat.S_IFREG | 0o755, 0, 0, 1, 0, 0, 0, 0, 0, 0)),
                      doctor.START_STEP: os.stat_result((stat.S_IFREG | 0o755, 0, 0, 1, 0, 0, 0, 0, 0, 0)),
                      str(doctor.CHECKOUT / 'deploy' / 'dd-install'):
                          os.stat_result((stat.S_IFREG | 0o755, 0, 0, 1, 1001, 0, 0, 0, 0, 0)),
                      doctor.CREDENTIAL_FOLDER: os.stat_result((stat.S_IFDIR | 0o700, 0, 0, 1, 0, 0, 0, 0, 0, 0))}

    def fake_host(self):
        """Answer the doctor's host lookups from host()'s state, with every Git, systemctl and sudo call faked."""
        real_stat, real_lstat = os.stat, os.lstat

        def fake_stat(real):
            def stat_(path, *args, **kwargs):
                if str(path) in self.files:
                    if self.files[str(path)] is None:
                        raise FileNotFoundError('fake-private-missing')
                    return self.files[str(path)]
                return real(path, *args, **kwargs)
            return stat_

        def run_capped(command, **kwargs):
            if command[0] == 'systemctl':
                values = self.units.get(command[-1], {'LoadState': 'not-found'})
                return proc.Result(0, ''.join(f'{k}={v}\n' for k, v in values.items()), '', False, False)
            self.assertEqual(command[:4], ['sudo', '-n', '-l', '-u'])
            self.assertEqual(command[5:], ['/usr/local/bin/dd-agent-run'])
            return proc.Result(self.sudo.get(command[4], 1), '', 'fake-private-sudo', False, False)

        self.origin = self.enterContext(mock.patch.object(gitops, 'check_origin'))
        self.which = self.enterContext(mock.patch.object(shutil, 'which', return_value='/usr/bin/fake'))
        self.account = self.enterContext(mock.patch.object(pwd, 'getpwnam', return_value=mock.Mock()))
        self.group = self.enterContext(mock.patch.object(doctor.config, 'agent_group', return_value=1234))
        self.commands = self.enterContext(mock.patch.object(proc, 'run_capped', side_effect=run_capped))
        self.enterContext(mock.patch.object(os, 'stat', fake_stat(real_stat)))
        self.enterContext(mock.patch.object(os, 'lstat', fake_stat(real_lstat)))

    def run_doctor(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = doctor.run()
        return code, out.getvalue()

    def test_sandbox_failures_set_exit_status(self):
        def isolation(cfg, accounts, checkout, config_path, report):
            report('FAIL', 'Agent 1 config isolation', 'File permissions allow reading; fix permissions.')
        with mock.patch.object(doctor.doctor_sandbox, 'check', side_effect=isolation):
            code, output = self.run_doctor()
        self.assertEqual(code, 1)
        self.assertIn('FAIL Agent 1 config isolation:', output)

    def test_success_and_personal_warning(self):
        code, output = self.run_doctor()
        self.assertEqual(code, 0)
        for check in ('Python', 'Config', 'Config permissions', 'Store', 'Bark', 'Plane Orchestrator',
                      'Plane agent 1', 'Plane project 1 states', 'Plane project 1 agent label',
                      'Plane project 1 agent 1 membership', 'GitHub repository 1', 'GitHub expiry',
                      'Git push login 1', 'Agent 1 command', 'Agent 1 account', 'Agent 1 env file',
                      'Service', 'Service account', 'Service checkout', 'Rollback service',
                      'Rollback helper', 'Launcher', 'Sudo rule agent 1'):
            self.assertIn('OK ' + check + ':', output)
        self.assertNotIn('FAIL', output)
        self.assertNotIn('SKIP', output)
        self.assertIn("write permissions can't be checked for a personal token", output)
        self.assertIn('push permission is allowed', output)

    def test_failures(self):
        cases = [(0, 'Config'), (2, 'Store'), (3, 'Plane Orchestrator'), (4, 'GitHub repository 1')]
        for index, label in cases:
            with self.subTest(label=label):
                self.mocks[index].side_effect = RuntimeError('fake-secret-in-exception')
                code, output = self.run_doctor()
                self.assertEqual(code, 1)
                self.assertIn('FAIL ' + label, output)
                self.assertNotIn('fake-secret-in-exception', output)
                self.mocks[index].side_effect = None

    def test_permissions_bark_and_plane_failures(self):
        self.mocks[1].return_value.st_mode = 0o100644
        self.cfg['alerts']['encryption_key'] = 'bad'
        self.client.current_user.side_effect = ValueError('private')
        self.client.get.return_value = []
        self.client.get.side_effect = None
        code, output = self.run_doctor()
        self.assertEqual(code, 1)
        for label in ('Config permissions', 'Bark', 'Plane agent 1'):
            self.assertIn('FAIL ' + label, output)

    def test_blocked_sound(self):
        for sound, status in (('alarm', 'OK'), ('', 'WARN'), (None, 'FAIL')):
            with self.subTest(sound=sound):
                self.cfg['alerts']['blocked_sound'] = sound
                code, output = self.run_doctor()
                self.assertIn(status + ' Bark:', output)
                self.assertEqual(code, 1 if status == 'FAIL' else 0)
                self.assertEqual(output.count(' Bark:'), 1)

    def test_app_permissions_and_expiry(self):
        self.cfg['github'] = {'app_id': 1}
        details = {'permissions': dict.fromkeys(('contents', 'pull_requests', 'statuses'), 'write'),
                   'expires_at': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}
        with mock.patch.object(github, 'installation_details', return_value=('fake-installation', details)) as auth:
            code, output = self.run_doctor()
            self.assertEqual(code, 0)
            self.assertIn('OK GitHub expiry: App tokens renew automatically.', output)
            for permission, level in (('contents', None), ('pull_requests', 'read'), ('statuses', 'read')):
                details['permissions'][permission] = level
                code, output = self.run_doctor()
                self.assertEqual(code, 1)
                self.assertIn('FAIL GitHub App ' + permission, output)
                details['permissions'][permission] = 'write'
            # The App only reads code: DD pushes with the host's saved Git login.
            details['permissions']['contents'] = 'read'
            code, output = self.run_doctor()
            self.assertEqual(code, 0)
            self.assertIn("OK GitHub App contents: Installation can read the repository; "
                          "DD pushes with the host's saved Git login, not the App.", output)
            auth.side_effect = ValueError('private')
            self.assertIn('FAIL GitHub login', self.run_doctor()[1])

    def test_expiry_unknown_soon_and_expired(self):
        for days, status, code in ((10, 'WARN', 0), (-1, 'FAIL', 1)):
            self.mocks[5].return_value = datetime.now(timezone.utc) + timedelta(days=days)
            result, output = self.run_doctor()
            self.assertEqual(result, code)
            self.assertIn(status + ' GitHub expiry', output)
        self.mocks[5].side_effect = ValueError('private')
        self.assertIn('WARN GitHub expiry', self.run_doctor()[1])

    def test_no_config_secrets_printed(self):
        code, output = self.run_doctor()
        def strings(value):
            if isinstance(value, dict):
                for k, v in value.items():
                    yield k
                    yield from strings(v)
            elif isinstance(value, list):
                for v in value:
                    yield from strings(v)
            elif isinstance(value, str):
                yield value
        for value in strings(self.cfg):
            if value.startswith('fake-') or value == 'x' * 32:
                self.assertNotIn(value, output)

    def test_cli_does_not_start_dd(self):
        for code in (0, 1):
            with mock.patch('sys.argv', ['main.py', '--doctor']), mock.patch.object(doctor, 'run', return_value=code), \
                    mock.patch.object(main, 'code_version') as version, mock.patch.object(runs, 'connect') as connect:
                self.assertEqual(main.main(), code)
                version.assert_not_called()
                connect.assert_not_called()

    def test_project_checks_fail_independently(self):
        for method, label in (('states', 'states'), ('labels', 'agent label'),
                              ('members', 'agent 1 membership')):
            with self.subTest(method=method):
                action = getattr(self.client, method)
                saved = action.return_value
                action.return_value = []
                code, output = self.run_doctor()
                self.assertEqual(code, 1)
                self.assertIn('FAIL Plane project 1 ' + label, output)
                action.return_value = saved

    def sprint_pull(self):
        self.cfg['plane']['approver_user_id'] = 'approver'
        project = self.cfg['projects']['fake-private-project']
        project['states']['done'] = 'state'
        project['states']['cancelled'] = 'state'
        project['cycles'] = {'product_backlog_state': 'backlog', 'sprint_backlog_state': 'sprint',
                             'carried_over_label_id': 'carried'}
        self.client.states.return_value = [{'id': 'state'}, {'id': 'backlog'}, {'id': 'sprint'}]
        self.client.labels.return_value = [{'id': 'label'}, {'id': 'carried'}]
        self.client.members.return_value = [{'id': 'user'}, {'id': 'approver'}]

    def test_no_sprint_pull_checks_without_cycles(self):
        code, output = self.run_doctor()
        self.assertEqual(code, 0)
        for check in ('sprint-pull', 'carried-over', 'approver', 'Done state'):
            self.assertNotIn(check, output)

    def test_sprint_pull_checks(self):
        self.sprint_pull()
        code, output = self.run_doctor()
        self.assertEqual(code, 0)
        for check in ('sprint-pull states', 'carried-over label', 'approver membership', 'Done state'):
            self.assertIn('OK Plane project 1 ' + check + ':', output)

    def test_sprint_pull_requires_cancelled_state(self):
        self.sprint_pull()
        del self.cfg['projects']['fake-private-project']['states']['cancelled']
        code, output = self.run_doctor()
        self.assertNotEqual(code, 0)
        self.assertIn('FAIL Plane project 1 Cancelled state:', output)

    def test_sprint_pull_checks_fail_independently(self):
        cases = [('states', [{'id': 'state'}, {'id': 'backlog'}], 'sprint-pull states'),
                 ('states', [{'id': 'state'}, {'id': 'sprint'}], 'sprint-pull states'),
                 ('labels', [{'id': 'label'}], 'carried-over label'),
                 ('members', [{'id': 'user'}], 'approver membership')]
        for method, answer, label in cases:
            with self.subTest(label=label, answer=answer):
                self.sprint_pull()
                getattr(self.client, method).return_value = answer
                code, output = self.run_doctor()
                self.assertEqual(code, 1)
                self.assertEqual(output.count('FAIL'), 1, output)
                self.assertIn('FAIL Plane project 1 ' + label, output)
        self.sprint_pull()
        del self.cfg['projects']['fake-private-project']['states']['done']
        code, output = self.run_doctor()
        self.assertEqual(code, 1)
        self.assertEqual(output.count('FAIL'), 1, output)
        self.assertIn('FAIL Plane project 1 Done state: Sprint-pull moves merged stories to Done itself', output)

    def test_sprint_pull_checks_skip_without_orchestrator(self):
        self.sprint_pull()
        self.client.current_user.side_effect = [plane.PlaneError('private', 401), 'user']
        code, output = self.run_doctor()
        self.assertEqual(code, 1)
        for check in ('sprint-pull states', 'carried-over label', 'approver membership'):
            self.assertIn('SKIP Plane project 1 ' + check + ': skipped: the Plane Orchestrator check failed', output)
        self.assertIn('OK Plane project 1 Done state:', output)
        self.client.states.assert_not_called()
        self.client.labels.assert_not_called()
        self.client.members.assert_not_called()

    def test_plane_dependencies_skip_without_requests(self):
        for orchestrator in (True, False):
            with self.subTest(orchestrator=orchestrator):
                self.client.reset_mock()
                self.client.current_user.side_effect = ([plane.PlaneError('private', 401), 'user']
                    if orchestrator else ['user', plane.PlaneError('private', 401)])
                code, output = self.run_doctor()
                self.assertEqual(code, 1)
                reason = ('the Plane Orchestrator check failed' if orchestrator
                          else "this agent's Plane login failed")
                self.assertIn('SKIP Plane project 1 agent 1 membership: skipped: ' + reason, output)
                self.client.members.assert_not_called()
                if orchestrator:
                    self.assertIn('SKIP Plane project 1 states:', output)
                    self.assertIn('SKIP Plane project 1 agent label:', output)
                    self.client.states.assert_not_called()
                    self.client.labels.assert_not_called()
                self.assertNotIn('FAIL Plane project', output)

    def test_github_dependencies_skip(self):
        self.cfg['github'] = {'app_id': 1}
        with mock.patch.object(github, 'installation_details', side_effect=github.GitHubError('private', 401)):
            code, output = self.run_doctor()
        self.assertEqual(code, 1)
        for label in ('repository 1', 'App contents', 'App pull_requests', 'App statuses', 'expiry'):
            self.assertIn('SKIP GitHub ' + label + ': skipped: the GitHub login check failed', output)
        self.mocks[4].assert_not_called()

    def test_service_failure_reasons_do_not_echo_errors(self):
        for service, action, error_type in (('Plane', self.client.current_user, plane.PlaneError),
                                            ('GitHub', self.mocks[4], github.GitHubError)):
            for error, reason in ((error_type('fake-secret', 401), 'was rejected'),
                                  (error_type('fake-secret', 403), 'was rejected'),
                                  (error_type('fake-secret', 404), 'not found'),
                                  (urllib.error.URLError('fake-secret'), "Can't reach " + service),
                                  (TimeoutError('fake-secret'), "Can't reach " + service)):
                with self.subTest(service=service, reason=reason):
                    action.side_effect = error
                    code, output = self.run_doctor()
                    self.assertEqual(code, 1)
                    self.assertIn(reason, output)
                    self.assertNotIn('fake-secret', output)
            action.side_effect = None


    def test_python_version(self):
        code, output = self.run_doctor()
        self.assertIn('OK Python: Version %d.%d is new enough.' % sys.version_info[:2], output)
        with mock.patch.object(doctor, 'MIN_PYTHON', (99, 0)):
            code, output = self.run_doctor()
        self.assertEqual(code, 1)
        self.assertIn('FAIL Python: DD needs Python 99.0 or newer; this is %d.%d.' % sys.version_info[:2], output)

    def test_git_push_login(self):
        code, output = self.run_doctor()
        self.origin.assert_called_once_with('/fake-private-repo')
        self.assertIn('OK Git push login 1:', output)
        self.assertIn("WARN Git push login 1 write access: write access can't be confirmed without pushing", output)
        self.origin.side_effect = gitops.GitError('fake-private-git-error')
        code, output = self.run_doctor()
        self.assertEqual(code, 1)
        self.assertIn('FAIL Git push login 1:', output)
        self.assertIn('SKIP Git push login 1 write access: skipped: the Git push login 1 check failed', output)
        self.assertNotIn('fake-private', output)

    def test_agent_checks(self):
        code, output = self.run_doctor()
        self.which.assert_called_once_with('fake-private-command', path=doctor.AGENT_PATH)
        self.account.assert_any_call('fake-private-account')
        self.assertIn("WARN Agent 1 login: the login itself can't be checked without running the agent; "
                      "DD's login alerts report a rejected login on the first run.", output)
        env = self.files['/fake-private-env']
        for label, change in (('command', lambda: setattr(self.which, 'return_value', None)),
                              ('env file', lambda: self.files.update({'/fake-private-env': None})),
                              ('env file', lambda: self.files.update({'/fake-private-env': os.stat_result(
                                  (stat.S_IFREG | 0o644, 0, 0, 1, 0, 1234, 0, 0, 0, 0))})),
                              ('env file', lambda: self.files.update({'/fake-private-env': os.stat_result(
                                  (stat.S_IFREG | 0o640, 0, 0, 1, 1000, 1234, 0, 0, 0, 0))})),
                              ('env file', lambda: setattr(self.group, 'return_value', 99))):
            with self.subTest(label=label):
                change()
                code, output = self.run_doctor()
                self.assertEqual(code, 1)
                self.assertIn('FAIL Agent 1 ' + label + ':', output)
                self.assertIn(('SKIP' if label == 'command' else 'WARN') + ' Agent 1 login:', output)
                self.assertNotIn('fake-private', output)
                self.which.return_value, self.files['/fake-private-env'], self.group.return_value = '/usr/bin/fake', env, 1234

    def test_agent_account_missing_skips_dependants(self):
        self.account.side_effect = KeyError('fake-private-account')
        code, output = self.run_doctor()
        self.assertEqual(code, 1)
        self.assertIn('FAIL Agent 1 account:', output)
        self.assertIn("SKIP Agent 1 env file: skipped: this agent's account check failed", output)
        self.assertIn('SKIP Agent 1 login:', output)
        self.assertIn("SKIP Sudo rule agent 1: skipped: this agent's account check failed", output)
        self.assertFalse(any(call.args[0][0] == 'sudo' for call in self.commands.call_args_list))
        self.assertNotIn('fake-private', output)

    def test_agent_without_env_file(self):
        del self.cfg['agents']['fake-private-name']['env_file']
        self.files['/fake-private-env'] = None
        code, output = self.run_doctor()
        self.assertEqual(code, 0)
        self.assertIn('OK Agent 1 env file: No env file is configured', output)

    def test_env_file_is_never_read(self):
        with mock.patch('builtins.open', side_effect=AssertionError('opened')) as opened:
            self.run_doctor()
        self.assertFalse([c for c in opened.call_args_list if '/fake-private-env' in map(str, c.args)])

    def test_service_checks_fail(self):
        cases = [('Service account', lambda: self.units[doctor.SERVICE].update(User='fake-other')),
                 ('Service checkout', lambda: self.units[doctor.SERVICE].update(WorkingDirectory='/fake-other')),
                 ('Rollback service', lambda: self.units.pop(doctor.ROLLBACK_SERVICE)),
                 ('Rollback helper', lambda: self.files.update({doctor.ROLLBACK_HELPER: None})),
                 ('Launcher', lambda: self.files.update({doctor.LAUNCHER: os.stat_result(
                     (stat.S_IFREG | 0o755, 0, 0, 1, 1000, 0, 0, 0, 0, 0))})),
                 ('Launcher', lambda: self.files.update({doctor.LAUNCHER: os.stat_result(
                     (stat.S_IFREG | 0o775, 0, 0, 1, 0, 0, 0, 0, 0, 0))})),
                 ('Sudo rule agent 1', lambda: self.sudo.clear())]
        for label, change in cases:
            with self.subTest(label=label):
                change()
                code, output = self.run_doctor()
                self.assertEqual(code, 1)
                self.assertRegex(output, 'FAIL ' + label + ': .*deploy/README.md')
                self.assertEqual(output.count('FAIL'), 1)
                self.assertNotIn('fake-private', output)
                self.host()

    def test_service_missing_skips_dependants(self):
        for missing in ({'LoadState': 'not-found'}, None):
            with self.subTest(missing=missing):
                if missing:
                    self.units[doctor.SERVICE] = missing
                else:
                    self.commands.side_effect = FileNotFoundError('fake-private-systemctl')
                code, output = self.run_doctor()
                self.assertEqual(code, 1)
                self.assertIn('FAIL Service: dynamic-dispatch.service is not installed', output)
                self.assertIn('SKIP Service account: skipped: the Service check failed', output)
                self.assertIn('SKIP Service checkout: skipped: the Service check failed', output)
                self.assertNotIn('fake-private', output)

    def test_launcher_missing_skips_sudo_rule(self):
        self.files[doctor.LAUNCHER] = None
        code, output = self.run_doctor()
        self.assertEqual(code, 1)
        self.assertIn('FAIL Launcher:', output)
        self.assertIn('SKIP Sudo rule agent 1: skipped: the Launcher check failed', output)
        self.assertFalse(any(call.args[0][0] == 'sudo' for call in self.commands.call_args_list))

    def test_launcher_is_the_installed_path(self):
        self.assertEqual(doctor.LAUNCHER, '/usr/local/bin/dd-agent-run')
        self.files[doctor.LAUNCHER] = None
        self.files['/fake-private-launcher'] = None
        code, output = self.run_doctor()
        self.assertEqual(code, 1)
        self.assertIn('FAIL Launcher: /usr/local/bin/dd-agent-run is missing', output)
        self.assertIn('SKIP Sudo rule agent 1: skipped: the Launcher check failed', output)
        self.assertNotIn('fake-private', output)

    def test_configured_launcher_cannot_stand_in(self):
        self.files[doctor.LAUNCHER] = None
        self.files['/fake-private-other'] = os.stat_result((stat.S_IFREG | 0o755, 0, 0, 1, 0, 0, 0, 0, 0, 0))
        self.cfg['sandbox']['launcher'] = '/fake-private-other'
        code, output = self.run_doctor()
        self.assertEqual(code, 1)
        self.assertIn('FAIL Launcher:', output)
        self.assertNotIn('OK Launcher:', output)

    def test_service_reads_only(self):
        self.run_doctor()
        for call in self.commands.call_args_list:
            command = call.args[0]
            self.assertTrue(command[:2] == ['systemctl', 'show'] or command[:3] == ['sudo', '-n', '-l'], command)


class ContainerDoctor(Doctor):
    """The same host checks with the image's marker set, and the volume checks faked."""

    def setUp(self):
        super().setUp()
        os.environ[doctor.CONTAINER_MARKER] = '1'
        self.volumes = self.enterContext(mock.patch.object(doctor.doctor_sandbox, 'check_container',
                                                           side_effect=self.container_checks))

    def container_checks(self, volumes, credentials, report):
        for label, path, login in volumes + credentials:
            report('OK', label, 'Fake metadata allows it.')

    def test_success_and_personal_warning(self):
        code, output = self.run_doctor()
        self.assertEqual(code, 0)
        for check in ('Python', 'Config', 'Store', 'Agent 1 account', 'Start step', 'Installer',
                      'Credential mount folder', 'Rollback helper', 'Launcher', 'Sudo rule agent 1',
                      'Volume DD home', 'Volume checkout', 'Volume config', 'Volume history',
                      'Volume repositories', 'Volume story folders', 'Volume agent env files',
                      'Volume Gemini home', 'Gemini credential'):
            self.assertIn('OK ' + check + ':', output)
        for check in ('Service', 'Service account', 'Service checkout', 'Rollback service'):
            self.assertIn(f"SKIP {check}: skipped: systemd units don't apply in a container", output)
        self.assertNotIn('FAIL', output)
        self.assertFalse(any(call.args[0][0] == 'systemctl' for call in self.commands.call_args_list))

    def test_service_checks_fail(self):
        cases = [('Rollback helper', lambda: self.files.update({doctor.ROLLBACK_HELPER: None})),
                 ('Launcher', lambda: self.files.update({doctor.LAUNCHER: None})),
                 ('Sudo rule agent 1', lambda: self.sudo.clear()),
                 ('Start step', lambda: self.files.update({doctor.START_STEP: None})),
                 ('Start step', lambda: self.files.update({doctor.START_STEP: os.stat_result(
                     (stat.S_IFREG | 0o755, 0, 0, 1, 1001, 0, 0, 0, 0, 0))})),
                 ('Installer', lambda: self.files.update({str(doctor.CHECKOUT / 'deploy' / 'dd-install'): None})),
                 ('Credential mount folder', lambda: self.files.update({doctor.CREDENTIAL_FOLDER: os.stat_result(
                     (stat.S_IFDIR | 0o755, 0, 0, 1, 0, 0, 0, 0, 0, 0))})),
                 ('Credential mount folder', lambda: self.files.update({doctor.CREDENTIAL_FOLDER: None}))]
        for label, change in cases:
            with self.subTest(label=label):
                change()
                code, output = self.run_doctor()
                self.assertEqual(code, 1)
                self.assertRegex(output, 'FAIL ' + label + ': .*(deploy/README.md|checkout)')
                self.assertEqual(output.count('FAIL'), 1)
                self.assertNotIn('fake-private', output)
                self.host()

    def test_container_check_failures_set_exit_status(self):
        self.volumes.side_effect = lambda volumes, credentials, report: report(
            'FAIL', volumes[0][0], "Folder isn't writable by its account.")
        code, output = self.run_doctor()
        self.assertEqual(code, 1)
        self.assertIn('FAIL Volume DD home:', output)

    def test_volumes_match_compose(self):
        compose = (doctor.CHECKOUT / 'deploy' / 'compose.yaml').read_text()
        mounted = re.findall(r'^      - [\w-]+:(\S+)$', compose, re.M)
        self.assertEqual(sorted(path for label, path, login in doctor.VOLUMES), sorted(mounted))
        self.run_doctor()
        volumes, credentials, report = self.volumes.call_args.args
        self.assertEqual([path for label, path, login in volumes], [path for label, path, login in doctor.VOLUMES])
        self.assertEqual(credentials, [('Gemini credential', '/home/dd-gemini/gemini-credential.json', 'dd-gemini')])

    def test_service_missing_skips_dependants(self):
        self.units.clear()
        code, output = self.run_doctor()
        self.assertEqual(code, 0)
        self.assertNotIn('the Service check failed', output)

    def test_service_reads_only(self):
        self.run_doctor()
        for call in self.commands.call_args_list:
            self.assertEqual(call.args[0][:3], ['sudo', '-n', '-l'])

    def test_only_the_image_marker_counts(self):
        for value in ('0', '', 'true'):
            with self.subTest(value=value):
                os.environ[doctor.CONTAINER_MARKER] = value
                code, output = self.run_doctor()
                self.assertIn('OK Service: dynamic-dispatch.service is installed.', output)
                self.assertNotIn('Start step', output)
                self.volumes.assert_not_called()


class PlaneLists(unittest.TestCase):
    def test_pagination(self):
        client = plane.Plane('https://plane.invalid', 'workspace', 'fake-token')
        for method in ('states', 'labels', 'members'):
            with self.subTest(method=method), mock.patch.object(client, 'get', side_effect=[
                    {'results': [{'id': 'first'}], 'next_page_results': True, 'next_cursor': 'next'},
                    {'results': [{'id': 'last'}]}]) as get:
                self.assertEqual(getattr(client, method)('project'), [{'id': 'first'}, {'id': 'last'}])
                self.assertEqual(get.call_args_list, [
                    mock.call(f'/projects/project/{method}/?per_page=100'),
                    mock.call(f'/projects/project/{method}/?per_page=100&cursor=next')])


class StoreCheck(unittest.TestCase):
    def test_no_files_created_or_changed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'store.db'
            runs.check_writable(path)
            self.assertEqual(list(Path(directory).iterdir()), [])
            path.write_bytes(b'unchanged')
            runs.check_writable(path)
            self.assertEqual(path.read_bytes(), b'unchanged')
            with mock.patch.object(os, 'access', return_value=False):
                with self.assertRaises(runs.StoreError):
                    runs.check_writable(path)
            with self.assertRaises(runs.StoreError):
                runs.check_writable(path / 'missing' / 'store.db')


class GitHubChecks(unittest.TestCase):
    def test_installation_metadata_comes_from_token_response(self):
        settings = {'app_id': 'doctor-test', 'installation_id': 'doctor-installation',
                    'private_key_path': 'unused-test-key'}
        response = {'token': 'fake-doctor-installation',
                    'expires_at': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                    'permissions': {'contents': 'write', 'pull_requests': 'read', 'statuses': 'write'}}
        with mock.patch.dict(github._installation_tokens, clear=True), \
                mock.patch.dict(github._installation_details, clear=True), \
                mock.patch.object(github, '_app_jwt', return_value='fake-jwt'), \
                mock.patch.object(github, '_request', return_value=response) as request:
            credential, details = github.installation_details(settings)
            self.assertEqual(credential, response['token'])
            self.assertEqual(details['permissions'], response['permissions'])
            self.assertEqual(details['expires_at'], response['expires_at'])
            github.installation_details(settings)
            self.assertEqual(request.call_count, 1)
            self.assertEqual(request.call_args.args[:2],
                             ('POST', '/app/installations/doctor-installation/access_tokens'))

    def test_repository_only_reads(self):
        with mock.patch.object(github, '_request', return_value={'permissions': {'push': False}}) as request:
            self.assertFalse(github.repository('example/repo', 'fake-token')['permissions']['push'])
            request.assert_called_once_with('GET', '/repos/example/repo', 'fake-token')


class ServiceErrors(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.object(github, '_request', real_gh_request))
        self.enterContext(mock.patch.object(plane.Plane, '_request', real_plane_request))

    def test_http_status_is_public(self):
        for status in (401, 403, 404):
            with self.subTest(status=status):
                error = urllib.error.HTTPError('https://github.invalid', status, 'private', {}, io.BytesIO(b'private'))
                with mock.patch('urllib.request.urlopen', side_effect=error):
                    with self.assertRaises(github.GitHubError) as raised:
                        github.repository('example/repo', 'fake-token')
                self.assertEqual(raised.exception.status, status)
                error.close()

    def test_app_token_preserves_status_and_network_cause(self):
        settings = {'app_id': 'fake-app', 'installation_id': 'fake-install', 'private_key_path': 'fake-path'}
        for status in (401, 403, 404, None):
            error = github.GitHubError('private', status)
            if status is None:
                error.__cause__ = urllib.error.URLError('private')
            with mock.patch.dict(github._installation_tokens, clear=True), \
                    mock.patch.object(github, '_app_jwt', return_value='fake-jwt'), \
                    mock.patch.object(github, '_request', side_effect=error):
                with self.assertRaises(github.GitHubError) as raised:
                    github.installation_details(settings)
            self.assertEqual(raised.exception.status, status)
            self.assertIs(raised.exception.__cause__, error)

    def test_network_cause_survives_clients(self):
        client = plane.Plane('https://plane.invalid', 'workspace', 'fake-token')
        for action in (client.current_user, lambda: github.repository('example/repo', 'fake-token')):
            error = urllib.error.URLError('private')
            with mock.patch('urllib.request.urlopen', side_effect=error):
                with self.assertRaises((plane.PlaneError, github.GitHubError)) as raised:
                    action()
            self.assertIs(raised.exception.__cause__, error)

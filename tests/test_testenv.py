"""Project test settings and the real launcher's environment boundary."""

import json
import os
from pathlib import Path
import pwd
import subprocess
import tempfile
import unittest
from unittest import mock

from dispatch import agents, config, doctor, mainhealth, prepare, proc, redact, testenv, verify


class TestEnvironment(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        source = Path('deploy/dd-agent-run').read_text()
        self.launcher = self.folder / 'launcher'
        self.launcher.write_text(source.replace('WORKTREE_ROOT="/srv/dynamic-dispatch/worktrees"',
                                                f'WORKTREE_ROOT="{self.folder}"'))
        self.work = self.folder / 'work'
        self.work.mkdir()
        self.secret = self.folder / 'test.env'
        self.secret.write_text('TEST_PRIVATE=fake-test-value\n')
        self.secret.chmod(0o600)
        self.cfg = {'sandbox': {'user': 'unused', 'launcher': str(self.launcher)}}

    def launch(self, project=None, model=None):
        command = proc.sandboxed(self.cfg, ['/usr/bin/python3', '-c',
                                          'import os,json; print(json.dumps(dict(os.environ)))'],
                                 self.work, 10, project_cfg=project, env_file=model)
        return subprocess.run(['bash'] + command[4:], capture_output=True, text=True)

    def test_defaults_unchanged(self):
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        env = json.loads(result.stdout)
        self.assertEqual(env['PATH'], testenv.DEFAULT_PATH)
        self.assertEqual(env['HOME'], pwd.getpwuid(os.getuid()).pw_dir)
        self.assertNotIn('TEST_PRIVATE', env)

    def test_tests_and_agent_receive_settings_with_separate_model_login(self):
        model = self.folder / 'model.env'
        model.write_text('MODEL_LOGIN=fake-model-value\n')
        project = {'test_env': {'TEST_PLAIN': 'spaces = "quotes" $(literal)'},
                   'test_env_file': str(self.secret), 'test_path': ['/opt/first', '/opt/second']}
        for login in (None, str(model)):
            with self.subTest(agent=bool(login)):
                result = self.launch(project, login)
                self.assertEqual(result.returncode, 0, result.stderr)
                env = json.loads(result.stdout)
                self.assertEqual(env['TEST_PLAIN'], project['test_env']['TEST_PLAIN'])
                self.assertEqual(env['TEST_PRIVATE'], 'fake-test-value')
                self.assertEqual(env['PATH'], '/opt/first:/opt/second:' + testenv.DEFAULT_PATH)
                self.assertEqual('MODEL_LOGIN' in env, bool(login))

    def test_reserved_names_rejected_by_config_and_launcher(self):
        for name in ('HOME', 'PATH'):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    testenv.validate({'test_env': {name: '/opt/other'}})
                self.assertNotEqual(self.launch({'test_env': {name: '/opt/other'}}).returncode, 0)
                self.secret.write_text(name + '=/opt/other\n')
                with self.assertRaises(ValueError):
                    testenv.read_values(self.secret)
                self.assertNotEqual(self.launch({'test_env_file': str(self.secret)}).returncode, 0)

    def test_private_file_and_too_open_file(self):
        testenv.check_file(self.secret, [])
        self.secret.chmod(0o644)
        with self.assertRaises(ValueError):
            testenv.check_file(self.secret, [])
        cfg = {'projects': {'p': {'test_env_file': str(self.secret), 'agents': []}}}
        with self.assertRaises(config.ConfigError):
            config.check_test_env_files(cfg)

    def test_symlink_and_acl_rejected(self):
        link = self.folder / 'link'
        link.symlink_to(self.secret)
        with self.assertRaises(ValueError):
            testenv.check_file(link, [])
        with mock.patch.object(testenv.os, 'listxattr', return_value=['system.posix_acl_access']):
            with self.assertRaises(ValueError):
                testenv.check_file(self.secret, [])

    def test_secrets_are_scrubbed_and_normal_text_is_preserved(self):
        cfg = {'plane': {}, 'github': {}, 'agents': {},
               'projects': {'p': {'test_env_file': str(self.secret)}}}
        self.addCleanup(redact.set_secrets, [])
        redact.configure(cfg)
        self.assertEqual(redact.scrub('fake-test-value test 1 xyz'), '[redacted] test 1 xyz')

    def test_invalid_settings(self):
        for project in ({'test_env': {'BAD-NAME': 'x'}}, {'test_env': {'OK': 1}},
                        {'test_path': ['relative']}, {'test_path': ['/one:/two']},
                        {'test_env_file': 'relative'}):
            with self.subTest(project=project), self.assertRaises(ValueError):
                testenv.validate(project)
        self.secret.write_text('SETTING=first-fake-value\nSETTING=second-fake-value\n')
        with self.assertRaises(ValueError):
            testenv.read_values(self.secret)

    def test_every_execution_entry_passes_the_project_settings(self):
        project = {'test_env': {'APP_MODE': 'test'}, 'test_env_file': str(self.secret),
                   'test_path': ['/opt/tools'], 'prepare_commands': [['prepare']],
                   'verify_command': ['verify']}
        agent = {'parser': 'codex_json', 'display_name': 'Test agent', 'command': ['codex', '{prompt}'], 'env_file': '/model.env'}
        with mock.patch.object(proc, 'run_sandboxed', return_value=proc.Result(0, '', '', False, False)) as run:
            prepare.run({}, project, agent, self.work)
            verify.run_verify({}, project, agent, self.work)
            mainhealth._verify({}, project, agent, self.work)
            agents.run({}, agent, 'prompt', self.work, project_cfg=project)
        self.assertEqual(run.call_count, 5)
        for call in run.call_args_list:
            self.assertIs(call.kwargs['project_cfg'], project)
        self.assertEqual(run.call_args.kwargs['env_file'], '/model.env')
        self.assertTrue(all('env_file' not in call.kwargs for call in run.call_args_list[:-1]))

    def test_doctor_checks_each_tool_directory_and_uses_project_path(self):
        checks = []
        def check(name, action, good, bad):
            try:
                action()
            except ValueError:
                checks.append(False)
            else:
                checks.append(True)
        def require(value):
            if not value:
                raise ValueError('failed')
        project = {'test_path': [str(self.folder), str(self.folder / 'missing')],
                   'prepare_commands': [['tool']]}
        with mock.patch.object(doctor.shutil, 'which', return_value='/opt/tool') as which:
            doctor._check_prepare({'projects': {'p': project}}, check, require)
        self.assertEqual(checks, [True, False, True])
        which.assert_called_once_with('tool', path=testenv.path(project))

    def test_unrelated_group_member_is_rejected(self):
        self.secret.chmod(0o640)
        outsider = mock.Mock(pw_uid=os.geteuid() + 10000, pw_name='unrelated', pw_gid=10000)
        with mock.patch.object(testenv.pwd, 'getpwall', return_value=[outsider]), \
                mock.patch.object(testenv.os, 'getgrouplist', return_value=[self.secret.stat().st_gid]):
            with self.assertRaises(ValueError):
                testenv.check_file(self.secret, [])

"""Credential helper tests use generated fake values and no model service."""

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


loader = importlib.machinery.SourceFileLoader(
    'claude_login', str(Path(__file__).resolve().parents[1] / 'deploy/dd-claude-login'))
spec = importlib.util.spec_from_loader(loader.name, loader)
login = importlib.util.module_from_spec(spec)
loader.exec_module(login)


class ClaudeLoginTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'claude.env'
        self.token = 'sk-ant-oat01-' + 'fake' * 30
        self.output = io.StringIO()
        self.capture = contextlib.redirect_stdout(self.output)
        self.capture.__enter__()
        self.addCleanup(self.capture.__exit__, None, None, None)

    def read(self, lines):
        terminal = mock.mock_open(read_data='\n'.join(lines) + '\n')
        with mock.patch('builtins.open', terminal), \
             mock.patch.object(login.termios, 'tcgetattr', return_value=[0, 0, 0, 10, 0, 0, []]), \
             mock.patch.object(login.termios, 'tcsetattr') as settings:
            token = login.read_token()
        self.assertEqual(settings.call_count, 2)
        return token

    def test_pastes_produce_same_file_and_remove_whitespace(self):
        with mock.patch.object(login.os, 'fchown'):
            for lines in ([self.token, ''], [self.token[:79], self.token[79:], ''],
                          [' \t' + self.token[:79], self.token[79:] + ' \t', '']):
                token = self.read(lines)
                key, _ = login.login_kind(token)
                login.save_token(self.path, 123, key, token)
                self.assertEqual(self.path.read_text(), f'{key}={self.token}\n')
        self.assertNotIn(self.token, self.output.getvalue())

    def test_prefixes_and_invalid_characters(self):
        for token in ('', 'wrong', 'sk-ant-api;command', 'sk-ant-oat01-$value'):
            with self.assertRaises(ValueError):
                login.login_kind(token)
        self.assertEqual(login.login_kind('sk-ant-api' + 'fake' * 20)[0], 'ANTHROPIC_API_KEY')

    def run_main(self, token, answers):
        with mock.patch('sys.argv', ['dd-claude-login']), \
             mock.patch.object(login.os, 'geteuid', return_value=0), \
             mock.patch.object(login, 'destination', return_value=(self.path, 'agent', 123)), \
             mock.patch.object(login, 'read_token', return_value=token), \
             mock.patch('builtins.input', side_effect=answers), \
             mock.patch.object(login.os, 'fchown'):
            return login.main()

    def test_short_token_warns_and_requires_confirmation(self):
        token = 'sk-ant-oat01-' + 'fake'
        self.assertEqual(self.run_main(token, ['']), 1)
        self.assertFalse(self.path.exists())
        self.assertIn('This looks cut short; paste the whole token, including any second line', self.output.getvalue())
        self.assertEqual(self.run_main(token, ['yes', 'n']), 0)
        self.assertNotIn(token, self.output.getvalue())

    def test_invalid_token_does_not_replace_existing_file(self):
        self.path.write_text('existing\n')
        self.assertEqual(self.run_main('wrong', []), 1)
        self.assertEqual(self.path.read_text(), 'existing\n')

    def test_config_and_defaults(self):
        config = Path(self.tmp.name) / 'config.json'
        with mock.patch.object(login.grp, 'getgrnam', return_value=SimpleNamespace(gr_gid=123)) as group:
            for cfg in (None, {}):
                if cfg is not None:
                    config.write_text(json.dumps(cfg))
                self.assertEqual(login.destination(config), (Path(login.DEFAULT_ENV), 'dd-agent', 123))
                group.assert_called_with('dd-agent')
            config.write_text(json.dumps({'agents': {'claude-code': {
                'env_file': str(self.path), 'run_as': 'agent', 'group': 'agent-group'}}}))
            self.assertEqual(login.destination(config), (self.path, 'agent', 123))
            group.assert_called_with('agent-group')
        config.write_text(json.dumps({'sandbox': {'user': 'fallback'}, 'agents': {
            'claude-code': {'env_file': str(self.path)}}}))
        with mock.patch.object(login.pwd, 'getpwnam', return_value=SimpleNamespace(pw_gid=456)) as account:
            self.assertEqual(login.destination(config), (self.path, 'fallback', 456))
            account.assert_called_once_with('fallback')
        self.assertIn('using /etc/dynamic-dispatch/claude.env and group dd-agent', self.output.getvalue())

    def test_atomic_permissions_and_cleanup(self):
        real_replace = os.replace
        def replace(source, target):
            self.assertEqual(stat.S_IMODE(os.stat(source).st_mode), 0o640)
            self.assertEqual(Path(source).parent, self.path.parent)
            real_replace(source, target)
        with mock.patch.object(login.os, 'fchown') as owner, \
             mock.patch.object(login.os, 'replace', side_effect=replace):
            login.save_token(self.path, 123, 'KEY', self.token)
            owner.assert_called_once_with(mock.ANY, 0, 123)
        with mock.patch.object(login.os, 'fchown', side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                login.save_token(self.path, 123, 'KEY', 'replacement')
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])
        self.assertIn(self.token, self.path.read_text())

    @unittest.skipUnless(os.geteuid() == 0, 'actual ownership requires root')
    def test_real_owner_group_and_mode(self):
        login.save_token(self.path, os.getgid(), 'KEY', self.token)
        st = self.path.stat()
        self.assertEqual((st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)), (0, os.getgid(), 0o640))

    def test_smoke_test_is_bounded_drops_identity_and_hides_output(self):
        user = SimpleNamespace(pw_uid=123, pw_gid=456, pw_dir='/tmp')
        with mock.patch.object(login.pwd, 'getpwnam', return_value=user), \
             mock.patch.object(login.os, 'getgrouplist', return_value=[456]), \
             mock.patch.object(login.subprocess, 'run') as run:
            for response, expected in (({'result': 'OK'}, True),
                                       ({'is_error': True, 'subtype': self.token}, False)):
                run.return_value = SimpleNamespace(returncode=0, stdout=json.dumps(response), stderr=self.token)
                self.assertEqual(login.smoke_test('agent', 'KEY', self.token), expected)
            args, kwargs = run.call_args
            self.assertNotIn(self.token, args[0])
            self.assertEqual((kwargs['timeout'], kwargs['user'], kwargs['group']), (60, 123, 456))
            self.assertEqual(kwargs['env']['KEY'], self.token)
            run.side_effect = subprocess.TimeoutExpired('claude', 60, output=self.token)
            self.assertFalse(login.smoke_test('agent', 'KEY', self.token))
        self.assertIn('Claude replied OK', self.output.getvalue())
        self.assertIn('TimeoutExpired', self.output.getvalue())
        self.assertNotIn(self.token, self.output.getvalue())

    def test_rejected_login_gives_safe_recovery_message(self):
        user = SimpleNamespace(pw_uid=123, pw_gid=456, pw_dir='/tmp')
        private_text = 'provider detail that must stay hidden ' + self.token
        errors = [
            {'result': 'failed to authenticate ' + private_text},
            {'error': {'type': 'authentication_error', 'message': private_text}},
            {'errors': ['Authentication failed ' + private_text]},
            {'result': 'API Error: 401 ' + private_text},
            {'result': 'API Error: 403 ' + private_text},
        ]
        for field in ('status', 'status_code', 'statusCode'):
            for status in (401, '401', 403, '403'):
                errors.append({'error': {field: status, 'message': private_text}})
        with mock.patch.object(login.pwd, 'getpwnam', return_value=user), \
             mock.patch.object(login.os, 'getgrouplist', return_value=[456]), \
             mock.patch.object(login.subprocess, 'run') as run:
            for index, error in enumerate(errors):
                with self.subTest(error=index):
                    self.output.seek(0)
                    self.output.truncate(0)
                    reply = dict(error, is_error=True)
                    run.return_value = SimpleNamespace(
                        returncode=1, stdout=json.dumps(reply), stderr=private_text)
                    self.assertFalse(login.smoke_test('agent', 'KEY', self.token))
                    self.assertEqual(self.output.getvalue(),
                                     'Claude smoke test: login rejected. Make a new token '
                                     'with claude setup-token and run this helper again.\n')
                    self.assertNotIn(private_text, self.output.getvalue())
                    self.assertNotIn(self.token, self.output.getvalue())

    def test_other_model_error_stays_generic(self):
        user = SimpleNamespace(pw_uid=123, pw_gid=456, pw_dir='/tmp')
        reply = {'is_error': True, 'error': {'status': 500, 'message': self.token}}
        with mock.patch.object(login.pwd, 'getpwnam', return_value=user), \
             mock.patch.object(login.os, 'getgrouplist', return_value=[456]), \
             mock.patch.object(login.subprocess, 'run', return_value=SimpleNamespace(
                 returncode=1, stdout=json.dumps(reply), stderr=self.token)):
            self.assertFalse(login.smoke_test('agent', 'KEY', self.token))
        self.assertEqual(self.output.getvalue(), 'Claude smoke test: ModelError\n')

    def test_smoke_test_prompt(self):
        with mock.patch('builtins.input', return_value='n') as prompt:
            self.assertEqual(self.run_main(self.token, prompt), 0)
        prompt.assert_called_once_with(
            'Test the login now with one tiny request to Claude (60-second limit)? [y/N] ')

    def test_hidden_prompt_restores_terminal_on_interruption(self):
        terminal = mock.mock_open()
        terminal.return_value.readline.side_effect = KeyboardInterrupt
        original = [0, 0, 0, login.termios.ECHO | login.termios.ECHONL, 0, 0, []]
        with mock.patch('builtins.open', terminal), \
             mock.patch.object(login.termios, 'tcgetattr', return_value=original), \
             mock.patch.object(login.termios, 'tcsetattr') as settings:
            with self.assertRaises(KeyboardInterrupt):
                login.read_token()
        self.assertEqual(settings.call_args_list[0].args[2][3], 0)
        self.assertEqual(settings.call_args_list[-1].args[2], original)

    def test_refuses_non_root(self):
        with mock.patch('sys.argv', ['dd-claude-login']), mock.patch.object(login.os, 'geteuid', return_value=123):
            self.assertEqual(login.main(), 1)
        self.assertIn('Run as root', self.output.getvalue())

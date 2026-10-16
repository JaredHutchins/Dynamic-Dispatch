"""Recovery stays usable without importing the running DD code."""

import importlib.machinery
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from dispatch import runs, selfupdate
from helpers import Base, git


SCRIPT = Path(__file__).resolve().parents[1] / 'deploy' / 'dd-rollback'
loader = importlib.machinery.SourceFileLoader('rollback_helper', str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
helper = importlib.util.module_from_spec(spec)
loader.exec_module(helper)


class HealthyVersion(unittest.TestCase):
    def test_ten_minutes_completed_cycles_and_restart_resets_clock(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(selfupdate.time, 'monotonic') as clock:
            checkout = Path(folder)
            clock.return_value = 0
            health = selfupdate.HealthyVersion(checkout, 'a' * 40)
            for elapsed in (0, 30, 599):
                clock.return_value = elapsed
                health.completed_cycle()
                self.assertEqual(runs.rollback_state(checkout), {})
            clock.return_value = 600
            health.completed_cycle()
            self.assertEqual(runs.rollback_state(checkout)['last_good'], 'a' * 40)
            other = selfupdate.HealthyVersion(checkout, 'b' * 40)
            clock.return_value = 1199
            other.completed_cycle()
            self.assertEqual(runs.rollback_state(checkout)['last_good'], 'a' * 40)
            other.failed_cycle()
            clock.return_value = 1200
            other.completed_cycle()
            self.assertEqual(runs.rollback_state(checkout)['last_good'], 'a' * 40)
            clock.return_value = 1799
            other.completed_cycle()
            self.assertEqual(runs.rollback_state(checkout)['last_good'], 'b' * 40)

    def test_healthy_fallback_keeps_failed_commit_and_rollback_guard(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(selfupdate.time, 'monotonic') as clock:
            state = dict(last_good='a' * 40, rolled_back='a' * 40, failed='b' * 40)
            runs.save_rollback_state(folder, state)
            clock.return_value = 0
            health = selfupdate.HealthyVersion(folder, 'a' * 40)
            clock.return_value = 600
            health.completed_cycle()
            self.assertEqual(runs.rollback_state(folder), state)


class Recovery(Base):
    def setUp(self):
        super().setUp()
        self.checkout = self.tmp / 'recovery'
        git('clone', str(self.origin), str(self.checkout), cwd=self.tmp)
        self.good = git('rev-parse', 'HEAD', cwd=self.checkout).strip()
        (self.checkout / 'new.py').write_text('broken code\n')
        git('add', 'new.py', cwd=self.checkout)
        git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
            'commit', '-qm', 'Broken update', cwd=self.checkout)
        self.bad = git('rev-parse', 'HEAD', cwd=self.checkout).strip()
        runs.save_rollback_state(self.checkout, {'last_good': self.good})

    def test_switches_to_last_good_detached_and_never_rolls_back_twice(self):
        result = helper.recover(self.checkout)
        self.assertTrue(result['restart'])
        self.assertEqual(result['message'],
                         f'DD rolled back to {self.good[:12]} because {self.bad[:12]} kept failing to start')
        self.assertEqual(git('rev-parse', 'HEAD', cwd=self.checkout).strip(), self.good)
        self.assertEqual(git('rev-parse', '--abbrev-ref', 'HEAD', cwd=self.checkout).strip(), 'HEAD')
        state = runs.rollback_state(self.checkout)
        self.assertEqual(state['failed'], self.bad)
        self.assertEqual(state['rolled_back'], self.good)
        with mock.patch.object(helper, 'git', wraps=helper.git) as commands:
            result = helper.recover(self.checkout)
        self.assertFalse(result['restart'])
        self.assertEqual(result['message'], 'DD is down and needs a hand')
        self.assertFalse(any('checkout' in call.args[1:] for call in commands.call_args_list))
        self.assertIsNone(helper.recover(self.checkout)['message'])

    def test_recent_planned_restart_preserves_checkout_and_recovery_state(self):
        for age in (0, 299, 300):
            with self.subTest(age=age):
                state = dict(last_good=self.good, planned_restart=1000 - age)
                runs.save_rollback_state(self.checkout, state)
                with mock.patch.object(helper.time, 'time', return_value=1000), \
                        mock.patch.object(helper, 'git') as commands:
                    result = helper.recover(self.checkout)
                self.assertTrue(result['restart'])
                self.assertIsNone(result['message'])
                self.assertIn('planned restart', result['log'])
                commands.assert_not_called()
                self.assertEqual(runs.rollback_state(self.checkout), state)
                self.assertEqual(git('rev-parse', 'HEAD', cwd=self.checkout).strip(), self.bad)

    def test_host_planned_restart_exemption_is_unchanged(self):
        for marker in (None, '0'):
            with self.subTest(marker=marker), mock.patch.dict(helper.os.environ):
                helper.os.environ.pop('DD_CONTAINER', None)
                if marker is not None:
                    helper.os.environ['DD_CONTAINER'] = marker
                state = dict(last_good=self.good, planned_restart=1000)
                runs.save_rollback_state(self.checkout, state)
                with mock.patch.object(helper.time, 'time', return_value=1001):
                    result = helper.recover(self.checkout)
                self.assertTrue(result['restart'])
                self.assertIsNone(result['message'])
                self.assertIn('planned restart', result['log'])
                self.assertEqual(runs.rollback_state(self.checkout), state)
                self.assertEqual(git('rev-parse', 'HEAD', cwd=self.checkout).strip(), self.bad)

    def test_expired_planned_restart_does_not_suppress_crash_loop_rollback(self):
        runs.save_rollback_state(self.checkout, dict(last_good=self.good, planned_restart=699))
        with mock.patch.object(helper.time, 'time', return_value=1000):
            result = helper.recover(self.checkout)
        self.assertTrue(result['restart'])
        self.assertIn('DD rolled back', result['message'])
        self.assertEqual(git('rev-parse', 'HEAD', cwd=self.checkout).strip(), self.good)
        self.assertEqual(runs.rollback_state(self.checkout)['failed'], self.bad)

    def test_missing_good_and_dirty_checkout_stop_instead_of_destroying_changes(self):
        (self.checkout / 'new.py').write_text('local changes\n')
        result = helper.recover(self.checkout)
        self.assertFalse(result['restart'])
        self.assertEqual(git('rev-parse', 'HEAD', cwd=self.checkout).strip(), self.bad)
        self.assertEqual((self.checkout / 'new.py').read_text(), 'local changes\n')
        runs.save_rollback_state(self.checkout, {})
        self.assertEqual(helper.recover(self.checkout)['message'], 'DD is down and needs a hand')

    def test_recovery_disables_hooks_and_refuses_filters(self):
        hooks = self.tmp / 'hooks'
        hooks.mkdir()
        marker = self.tmp / 'hook-ran'
        hook = hooks / 'post-checkout'
        hook.write_text(f'#!/bin/sh\ntouch {marker}\n')
        hook.chmod(0o755)
        git('config', 'core.hooksPath', str(hooks), cwd=self.checkout)
        self.assertTrue(helper.recover(self.checkout)['restart'])
        self.assertFalse(marker.exists())
        git('-c', 'core.hooksPath=/dev/null', 'checkout', 'main', cwd=self.checkout)
        runs.save_rollback_state(self.checkout, {'last_good': self.good})
        git('config', 'filter.unsafe.clean', f'touch {marker}', cwd=self.checkout)
        self.assertFalse(helper.recover(self.checkout)['restart'])
        self.assertFalse(marker.exists())
        self.assertEqual(git('rev-parse', 'HEAD', cwd=self.checkout).strip(), self.bad)


class ServiceRecovery(unittest.TestCase):
    def test_bark_payload_has_sound_and_one_request_per_device(self):
        with tempfile.TemporaryDirectory() as folder:
            checkout = Path(folder)
            (checkout / 'config.json').write_text(json.dumps({'alerts': {
                'encryption_key': 'x' * 32, 'bark_url': 'https://bark.invalid',
                'device_keys': ['fake-device-one', 'fake-device-two'],
            }}))
            cipher = mock.Mock()
            cipher.encrypt.return_value = b'fake encrypted body'
            module = mock.Mock()
            module.AESGCM.return_value = cipher
            with mock.patch.dict(helper.sys.modules, {'cryptography.hazmat.primitives.ciphers.aead': module}), \
                    mock.patch.object(helper.urllib.request, 'urlopen',
                                      side_effect=lambda *a, **kw: io.BytesIO(b'{"code":200}')) as send:
                helper.notify(checkout, 'DD is down and needs a hand')
            self.assertEqual(send.call_count, 2)
            payload = json.loads(cipher.encrypt.call_args.args[1])
            self.assertEqual(payload['level'], 'active')
            self.assertEqual(payload['sound'], 'alarm')
            self.assertEqual(payload['body'], 'DD is down and needs a hand')

    def invoke(self, outcome, result='start-limit-hit', notify_fails=False,
               properties='WorkingDirectory=/srv/custom DD=checkout\nUser=dd-owner\n'):
        def command(args):
            if args[:2] == ['systemctl', 'show']:
                return subprocess.CompletedProcess(
                    args, 0, result + '\n' if '--property=Result' in args else properties)
            if 'recover' in args:
                return subprocess.CompletedProcess(args, 0, json.dumps(outcome))
            if notify_fails and 'notify' in args:
                raise subprocess.TimeoutExpired(args, 60)
            return subprocess.CompletedProcess(args, 0, '')
        with mock.patch.object(helper.sys, 'argv', [str(SCRIPT)]), \
                mock.patch.object(helper, 'command', side_effect=command) as calls:
            helper.main()
        return [call.args[0] for call in calls.call_args_list]

    def test_reads_service_account_and_directory_for_both_children(self):
        calls = self.invoke(dict(restart=True, message='rolled back'))
        self.assertIn(['systemctl', 'show', helper.SERVICE,
                       '--property=User', '--property=WorkingDirectory'], calls)
        child = ['runuser', '-u', 'dd-owner', '--', '/usr/bin/python3', str(SCRIPT)]
        self.assertIn([*child, 'recover', '/srv/custom DD=checkout'], calls)
        self.assertIn([*child, 'notify', '/srv/custom DD=checkout', 'rolled back'], calls)

    def test_missing_or_relative_service_settings_fail_before_stopping(self):
        for properties in ('', 'User=owner\n', 'WorkingDirectory=/srv/dd\n',
                           'User=owner\nWorkingDirectory=relative\n'):
            with self.subTest(properties=properties), self.assertRaises(RuntimeError):
                self.invoke({}, properties=properties)

    def test_children_use_supplied_checkout_without_systemd_access(self):
        checkout = Path('/srv/custom DD=checkout')
        for action in ('recover', 'notify'):
            args = [str(SCRIPT), action, str(checkout)]
            if action == 'notify':
                args.append('rolled back')
            with mock.patch.object(helper.sys, 'argv', args), \
                    mock.patch.object(helper, 'command') as command, \
                    mock.patch.object(helper, 'recover', return_value={}) as recover, \
                    mock.patch.object(helper, 'notify') as notify, \
                    mock.patch.object(helper.sys, 'stdout', new_callable=io.StringIO):
                helper.main()
            command.assert_not_called()
            if action == 'recover':
                recover.assert_called_once_with(checkout)
            else:
                notify.assert_called_once_with(checkout, 'rolled back')

    def test_restarts_and_alerts_once_after_successful_rollback(self):
        calls = self.invoke(dict(restart=True, message='rolled back'))
        self.assertIn(['systemctl', 'stop', helper.SERVICE], calls)
        self.assertIn(['systemctl', 'reset-failed', helper.SERVICE], calls)
        self.assertIn(['systemctl', 'restart', helper.SERVICE], calls)
        self.assertEqual(sum('notify' in args for args in calls), 1)
        self.assertTrue(all('runuser' == args[0] for args in calls if 'recover' in args or 'notify' in args))

    def test_planned_restart_resets_limit_and_restarts_with_log_but_no_alert(self):
        reason = 'Start limit hit after a planned restart within five minutes'
        with mock.patch.object(helper.sys, 'stderr', new_callable=io.StringIO) as log:
            calls = self.invoke(dict(restart=True, message=None, log=reason))
        self.assertIn(reason, log.getvalue())
        self.assertIn(['systemctl', 'reset-failed', helper.SERVICE], calls)
        self.assertIn(['systemctl', 'restart', helper.SERVICE], calls)
        self.assertLess(calls.index(['systemctl', 'reset-failed', helper.SERVICE]),
                        calls.index(['systemctl', 'restart', helper.SERVICE]))
        self.assertFalse(any('notify' in args for args in calls))

    def test_last_good_failure_stops_and_alerts_without_restart(self):
        calls = self.invoke(dict(restart=False, message='DD is down and needs a hand'))
        self.assertIn(['systemctl', 'stop', helper.SERVICE], calls)
        self.assertNotIn(['systemctl', 'restart', helper.SERVICE], calls)
        self.assertEqual(sum('notify' in args for args in calls), 1)

    def test_early_failure_does_nothing_and_bark_failure_does_not_prevent_restart(self):
        self.assertEqual(len(self.invoke({}, result='exit-code')), 1)
        calls = self.invoke(dict(restart=True, message='rolled back'), notify_fails=True)
        self.assertIn(['systemctl', 'restart', helper.SERVICE], calls)

    def test_units_limit_three_starts_and_use_installed_helper(self):
        service = (SCRIPT.parent / 'dynamic-dispatch.service').read_text()
        for setting in ('StartLimitIntervalSec=300', 'StartLimitBurst=3',
                        'OnFailure=dynamic-dispatch-rollback.service', 'Restart=on-failure'):
            self.assertIn(setting, service)
        unit = (SCRIPT.parent / 'dynamic-dispatch-rollback.service').read_text()
        self.assertIn('ExecStart=/usr/local/bin/dd-rollback', unit)

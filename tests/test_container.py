"""The container start step's decisions, without Docker, root or real credentials."""

import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import pwd
import re
import stat
import sys
import tempfile
import unittest
from unittest import mock

from dispatch import runs, selfupdate
from helpers import Base, git


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


start = load('container_start', ROOT / 'deploy' / 'dd-container-start')
installer = load('container_installer', ROOT / 'deploy' / 'dd-install')
# The Debian package that provides each system command the image relies on.
PACKAGES = {'groupadd': 'passwd', 'useradd': 'passwd', 'usermod': 'passwd', 'visudo': 'sudo',
            'runuser': 'util-linux', 'git': 'git'}

ACCOUNT = pwd.getpwuid(os.getuid())


def patch(test, name, value):
    patcher = mock.patch.object(start, name, value)
    patcher.start()
    test.addCleanup(patcher.stop)


class Folders(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name) / 'dd'
        self.checkout = self.home / 'checkout'
        self.gemini_home = Path(folder.name) / 'dd-gemini'
        self.credentials = Path(folder.name) / 'credentials'
        for path in (self.checkout, self.home / 'config', self.home / 'history',
                     self.gemini_home, self.credentials):
            path.mkdir(parents=True)
        self.credentials.chmod(0o700)
        for name, value in [('HOME', self.home), ('CHECKOUT', self.checkout),
                            ('CONFIG', self.home / 'config'), ('HISTORY', self.home / 'history'),
                            ('GEMINI_HOME', self.gemini_home), ('CREDENTIALS', self.credentials),
                            ('CREDENTIAL_MOUNT', self.credentials / 'gemini.json'),
                            ('CREDENTIAL', self.gemini_home / 'gemini-credential.json')]:
            patch(self, name, value)


class Bootstrap(Folders):
    def run_bootstrap(self, *, decisions=(0,), installer=0):
        """Start as root would, recording the order of each step instead of doing it."""
        steps = []
        answers = iter(decisions)

        def as_dd(*args):
            if 'clone' in args:
                steps.append('clone')
                (self.checkout / '.git').mkdir()
                return 0
            steps.append(args[-1])
            return next(answers) if args[-1] == 'decision' else 0

        def run(args, **kwargs):
            steps.append('install')
            self.assertEqual(args, start.INSTALL)
            self.assertEqual(kwargs, {'cwd': self.checkout})
            return mock.Mock(returncode=installer)

        with mock.patch.object(start, 'as_dd', side_effect=as_dd), \
                mock.patch.object(start.subprocess, 'run', side_effect=run), \
                mock.patch.object(start.pwd, 'getpwnam', return_value=ACCOUNT), \
                mock.patch.object(start.time, 'sleep') as sleep, \
                mock.patch.object(start, 'drop_to_dd', side_effect=lambda account: steps.append('drop')), \
                mock.patch('sys.stdout', new_callable=io.StringIO) as output:
            try:
                start.bootstrap()
            except RuntimeError as error:
                steps.append(str(error))
        self.output, self.sleep = output.getvalue(), sleep
        return steps

    def test_first_start_clones_public_main_as_dd_before_anything_else(self):
        self.assertEqual(self.run_bootstrap(), ['clone', 'decision', 'install', 'drop'])
        self.assertIn(start.PUBLIC_REPO, self.output)
        for path in (self.home, self.checkout, self.home / 'config', self.gemini_home):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)

    def test_normal_start_reinstalls_from_the_checkout_after_recovery(self):
        (self.checkout / '.git').mkdir()
        self.assertEqual(self.run_bootstrap(), ['decision', 'install', 'drop'])

    def test_a_folder_that_is_not_a_checkout_stops_startup(self):
        (self.checkout / 'stray.txt').write_text('left behind\n')
        steps = self.run_bootstrap()
        self.assertEqual(len(steps), 1)
        self.assertIn('neither empty nor a Git checkout', steps[0])

    def test_failed_installer_counts_as_a_failed_start_and_never_starts_dd(self):
        (self.checkout / '.git').mkdir()
        self.assertEqual(self.run_bootstrap(installer=1),
                         ['decision', 'install', 'failure', 'deploy/dd-install failed'])

    def test_latched_recovery_waits_for_a_hand_then_starts(self):
        (self.checkout / '.git').mkdir()
        steps = self.run_bootstrap(decisions=(start.DOWN, start.DOWN, 0))
        self.assertEqual(steps, ['decision'] * 3 + ['install', 'drop'])
        self.assertEqual(self.output.count('needs a hand'), 1)
        self.assertEqual(self.sleep.call_count, 2)

    def test_a_decision_that_fails_does_not_start_dd(self):
        (self.checkout / '.git').mkdir()
        self.assertEqual(self.run_bootstrap(decisions=(1,)),
                         ['decision', 'Could not decide whether DD needs rolling back'])


class Decision(Folders):
    def decide(self, failures, outcome=None, now=1000):
        start.save_failures(failures)
        result = mock.Mock(stdout=json.dumps(outcome) if outcome else '')
        with mock.patch.object(start.time, 'time', return_value=now), \
                mock.patch.object(start.subprocess, 'run', return_value=result) as run, \
                mock.patch('sys.stdout', new_callable=io.StringIO) as output:
            code = start.decision()
        self.output = output.getvalue()
        return code, run

    def test_fewer_than_three_recent_failures_start_without_rollback(self):
        for failures in ([], [998, 999], [699, 850, 999], [850, 999, 2000]):
            with self.subTest(failures=failures):
                code, run = self.decide(failures)
                self.assertEqual(code, 0)
                run.assert_not_called()

    def test_three_failures_in_five_minutes_roll_back_and_log_why(self):
        code, run = self.decide([700, 850, 999], {'restart': True, 'message': 'DD rolled back to aaa'})
        self.assertEqual(code, 0)
        run.assert_called_once_with([start.PYTHON, start.ROLLBACK, 'recover', str(self.checkout)],
                                    capture_output=True, text=True, timeout=120)
        self.assertIn('failed to start 3 times within 5 minutes', self.output)
        self.assertIn('DD rolled back to aaa', self.output)
        self.assertEqual(start.recent_failures(1000), [])

    def test_planned_restart_logs_and_starts_without_rollback(self):
        code, _ = self.decide([900, 950, 999], {'restart': True, 'message': None, 'log': 'after a planned restart'})
        self.assertEqual(code, 0)
        self.assertIn('after a planned restart', self.output)

    def test_refused_or_unreadable_recovery_stays_down(self):
        for outcome in ({'restart': False, 'message': 'DD is down and needs a hand'}, None):
            with self.subTest(outcome=outcome):
                code, _ = self.decide([900, 950, 999], outcome)
                self.assertEqual(code, start.DOWN)
                self.assertIn('needs a hand', self.output)

    def test_latched_down_never_tries_recovery_again(self):
        runs.save_rollback_state(self.checkout, {'down': True})
        code, run = self.decide([900, 950, 999])
        self.assertEqual(code, start.DOWN)
        run.assert_not_called()


class RealRollback(Base):
    """Three failures run the real installed helper, which goes back to the last good commit."""
    def test_rolls_back_to_last_good(self):
        checkout = self.tmp / 'container-checkout'
        git('clone', str(self.origin), str(checkout), cwd=self.tmp)
        good = git('rev-parse', 'HEAD', cwd=checkout).strip()
        (checkout / 'new.py').write_text('broken code\n')
        git('add', 'new.py', cwd=checkout)
        git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
            'commit', '-qm', 'Broken update', cwd=checkout)
        runs.save_rollback_state(checkout, {'last_good': good})
        patch(self, 'CHECKOUT', checkout)
        patch(self, 'PYTHON', sys.executable)
        patch(self, 'ROLLBACK', str(ROOT / 'deploy' / 'dd-rollback'))
        now = start.time.time()
        start.save_failures([now - 20, now - 10, now])
        with mock.patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(start.decision(), 0)
        self.assertEqual(git('rev-parse', 'HEAD', cwd=checkout).strip(), good)
        self.assertIn(f'DD rolled back to {good[:12]}', output.getvalue())


    def test_a_version_that_ran_ten_minutes_is_the_target_when_an_update_keeps_failing(self):
        """DD's own ten-minute record in the checkout volume is what the start step rolls back to."""
        checkout = self.tmp / 'container-checkout'
        git('clone', str(self.origin), str(checkout), cwd=self.tmp)
        good = git('rev-parse', 'HEAD', cwd=checkout).strip()
        patch(self, 'CHECKOUT', checkout)
        patch(self, 'CONFIG', self.tmp / 'config')
        patch(self, 'PYTHON', sys.executable)
        patch(self, 'ROLLBACK', str(ROOT / 'deploy' / 'dd-rollback'))
        with mock.patch.object(selfupdate.time, 'monotonic', return_value=0):
            healthy = selfupdate.HealthyVersion(checkout, good)
        for elapsed in (300, 599, 600):
            with mock.patch.object(selfupdate.time, 'monotonic', return_value=elapsed):
                healthy.completed_cycle()
            if elapsed < 600:
                self.assertNotIn('last_good', runs.rollback_state(checkout))
        self.assertEqual(runs.rollback_state(checkout), {'last_good': good})
        # A self-update moves the checkout on, and the new version crashes on every start.
        (checkout / 'new.py').write_text('broken code\n')
        git('add', 'new.py', cwd=checkout)
        git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
            'commit', '-qm', 'Broken update', cwd=checkout)
        now = start.time.time()
        for crash in range(start.FAILURES):
            with self.subTest(crash=crash):
                self.assertEqual(start.decision(), 0)
            with mock.patch.object(start.subprocess, 'Popen') as popen, \
                    mock.patch.object(start.signal, 'signal'), mock.patch.object(start.time, 'sleep'), \
                    mock.patch.object(start.time, 'time', return_value=now + 60 * crash):
                popen.return_value.__enter__.return_value.wait.return_value = 1
                start.supervise()
            self.assertEqual(popen.call_args.kwargs, {'cwd': checkout})
        self.assertNotEqual(git('rev-parse', 'HEAD', cwd=checkout).strip(), good)
        with mock.patch.object(start.time, 'time', return_value=now + 150), \
                mock.patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(start.decision(), 0)
        self.assertEqual(git('rev-parse', 'HEAD', cwd=checkout).strip(), good)
        self.assertIn(f'DD rolled back to {good[:12]}', output.getvalue())


    def test_an_update_whose_installer_fails_returns_to_last_good(self):
        """An installed-file update restarts the container; its installer failing rolls it back."""
        checkout = self.tmp / 'container-checkout'
        git('clone', str(self.origin), str(checkout), cwd=self.tmp)
        good = git('rev-parse', 'HEAD', cwd=checkout).strip()
        (checkout / 'deploy').mkdir(exist_ok=True)
        (checkout / 'deploy' / 'dd-agent-run').write_text('launcher the installer rejects\n')
        git('add', 'deploy', cwd=checkout)
        git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
            'commit', '-qm', 'Broken launcher', cwd=checkout)
        update = git('rev-parse', 'HEAD', cwd=checkout).strip()
        # As DD leaves it when it exits for the update's restart.
        runs.save_rollback_state(checkout, {'last_good': good, 'planned_restart': start.time.time()})
        patch(self, 'CHECKOUT', checkout)
        patch(self, 'PYTHON', sys.executable)
        patch(self, 'ROLLBACK', str(ROOT / 'deploy' / 'dd-rollback'))
        installed = []
        real_run = start.subprocess.run

        def as_dd(*args):
            return {'decision': start.decision, 'failure': start.record_failure}[args[-1]]()

        def run(args, **kwargs):
            if args != start.INSTALL:
                return real_run(args, **kwargs)
            installed.append(git('rev-parse', 'HEAD', cwd=checkout).strip())
            return mock.Mock(returncode=1 if installed[-1] == update else 0)

        def start_container():
            with mock.patch.object(start, 'as_dd', side_effect=as_dd), \
                    mock.patch.object(start.subprocess, 'run', side_effect=run), \
                    mock.patch.object(start.pwd, 'getpwnam', return_value=ACCOUNT), \
                    mock.patch.object(start, 'own'), \
                    mock.patch.object(start, 'hand_over_credential'), \
                    mock.patch.object(start, 'drop_to_dd', side_effect=lambda account: installed.append('DD')), \
                    mock.patch('sys.stdout', new_callable=io.StringIO) as output:
                try:
                    start.bootstrap()
                except RuntimeError as error:
                    self.assertEqual(str(error), 'deploy/dd-install failed')
            return output.getvalue()

        # Within five minutes of the update's restart, the planned-restart exemption holds off rollback.
        for _ in range(start.FAILURES):
            start_container()
        self.assertIn('after a planned restart', start_container())
        self.assertEqual(installed, [update] * (start.FAILURES + 1))
        installed.clear()
        state = runs.rollback_state(checkout)
        state['planned_restart'] -= 301
        runs.save_rollback_state(checkout, state)
        for _ in range(start.FAILURES - 1):
            start_container()
        output = start_container()
        self.assertIn(f'DD failed to start {start.FAILURES} times within 5 minutes', output)
        self.assertIn(f'DD rolled back to {good[:12]} because {update[:12]} kept failing to start', output)
        # The installer runs again from the rolled-back checkout, installs, and DD starts.
        self.assertEqual(installed, [update] * (start.FAILURES - 1) + [good, 'DD'])
        self.assertEqual(git('rev-parse', 'HEAD', cwd=checkout).strip(), good)
        self.assertEqual(runs.rollback_state(checkout)['failed'], update)

class Supervise(Folders):
    def test_counts_crashes_but_not_updates_or_clean_exits(self):
        for code in (1, start.UPDATE_EXIT, 0):
            with self.subTest(code=code), mock.patch.object(start.subprocess, 'Popen') as popen, \
                    mock.patch.object(start.signal, 'signal'), \
                    mock.patch.object(start.time, 'sleep') as sleep, \
                    mock.patch.object(start.time, 'time', return_value=1000):
                start.save_failures([])
                popen.return_value.__enter__.return_value.wait.return_value = code
                self.assertEqual(start.supervise(), code)
                self.assertEqual(start.recent_failures(1000), [1000] if code == 1 else [])
                sleep.assert_called_once_with(start.RESTART_DELAY)
                self.assertEqual(popen.call_args.args[0][-2:],
                                 ['--config', str(self.home / 'config' / 'config.json')])

    def test_a_stop_is_passed_on_and_is_not_a_failure(self):
        handlers = {}
        with mock.patch.object(start.subprocess, 'Popen') as popen, \
                mock.patch.object(start.signal, 'signal', side_effect=lambda sig, fn: handlers.update({sig: fn})), \
                mock.patch.object(start.time, 'sleep') as sleep:
            child = popen.return_value.__enter__.return_value
            child.wait.side_effect = lambda: (handlers[start.signal.SIGTERM](start.signal.SIGTERM, None), -15)[1]
            self.assertEqual(start.supervise(), -15)
        child.send_signal.assert_called_once_with(start.signal.SIGTERM)
        sleep.assert_not_called()
        self.assertEqual(start.recent_failures(1000), [])


class Credential(Folders):
    def test_copy_is_private_to_gemini_and_replaces_a_planted_link(self):
        (self.credentials / 'gemini.json').write_text('{"fake": "credential"}')
        target = self.home / 'config' / 'config.json'
        target.write_text('DD config\n')
        (self.gemini_home / 'gemini-credential.json').symlink_to(target)
        start.hand_over_credential(ACCOUNT)
        copy = self.gemini_home / 'gemini-credential.json'
        self.assertFalse(copy.is_symlink())
        self.assertEqual(copy.read_text(), '{"fake": "credential"}')
        self.assertEqual(stat.S_IMODE(copy.stat().st_mode), 0o400)
        self.assertEqual(copy.stat().st_uid, ACCOUNT.pw_uid)
        self.assertEqual(target.read_text(), 'DD config\n')
        self.assertEqual([p.name for p in self.gemini_home.iterdir()], ['gemini-credential.json'])

    def test_removing_the_mount_removes_the_copy(self):
        copy = self.gemini_home / 'gemini-credential.json'
        copy.write_text('old')
        start.hand_over_credential(ACCOUNT)
        self.assertFalse(copy.exists())
        start.hand_over_credential(ACCOUNT)  # and nothing mounted, nothing copied, is fine

    def test_a_folder_others_can_open_is_refused(self):
        (self.credentials / 'gemini.json').write_text('{}')
        self.credentials.chmod(0o755)
        with self.assertRaises(RuntimeError):
            start.hand_over_credential(ACCOUNT)
        self.assertFalse((self.gemini_home / 'gemini-credential.json').exists())


class Image(unittest.TestCase):
    def test_marks_the_container_and_installs_with_gemini(self):
        dockerfile = (ROOT / 'deploy' / 'Dockerfile').read_text()
        self.assertIn('ENV DD_CONTAINER=1', dockerfile)
        self.assertRegex(dockerfile, r'@google/gemini-cli@\d+\.\d+\.\d+ ')
        self.assertIn('dd-install --container --dd-user dd --agents gemini', dockerfile)
        self.assertEqual(start.INSTALL[1:], ['deploy/dd-install', '--container', '--dd-user', 'dd',
                                             '--agents', 'gemini'])

    def test_installs_every_command_the_installer_and_start_step_run(self):
        """Without Docker: ask deploy/dd-install itself which commands a fresh container needs."""
        dockerfile = (ROOT / 'deploy' / 'Dockerfile').read_text()
        listed = re.search(r'apt-get install -y --no-install-recommends(.*?)&&', dockerfile, re.S)
        packages = set(listed.group(1).replace('\\', ' ').split())
        dd = mock.Mock(pw_uid=1001, pw_gid=1001)
        checked, run = [], []
        with mock.patch.object(installer.os, 'geteuid', return_value=0), \
                mock.patch.object(installer.os, 'getcwd', return_value=str(ROOT)), \
                mock.patch.object(installer, 'user', side_effect=lambda name: dd if name == 'dd' else None), \
                mock.patch.object(installer, 'group', return_value=None), \
                mock.patch.object(installer.shutil, 'which',
                                  side_effect=lambda name, path=None: checked.append(name) or name), \
                mock.patch('sys.stdout', new_callable=io.StringIO):
            args = mock.Mock(container=True, dd_user='dd', agents=['gemini'])
            checkout = installer.preflight(args)
            # A fresh image: no accounts, groups or files yet, so every step is planned.
            with mock.patch.object(installer, 'inspect_path',
                                   return_value=mock.Mock(st_uid=0, st_mode=stat.S_IFDIR | 0o755)):
                plan = installer.build_plan('dd', checkout, ['gemini'], container=True)
        for _, action, step in plan.steps:
            if action is installer.command:
                run.append(step[0][0])
            elif step[0] == Path('/etc/sudoers.d/dynamic-dispatch'):
                run.append('visudo')  # checks the rule before installing it
        needed = set(checked) - {'gemini'} | set(run) | {'runuser', 'git'}
        self.assertTrue({'groupadd', 'useradd', 'usermod', 'visudo'} <= needed)
        self.assertNotIn('systemctl', needed)
        self.assertLessEqual(needed, set(PACKAGES), 'Name the package for a new command in PACKAGES')
        for name in sorted(needed):
            with self.subTest(command=name):
                self.assertIn(PACKAGES[name], packages)
        self.assertIn('python3', packages)

    def test_compose_keeps_state_in_named_volumes(self):
        compose = (ROOT / 'deploy' / 'compose.yaml').read_text()
        self.assertIn('restart: unless-stopped', compose)
        for path in ('/home/dd', '/home/dd/checkout', '/home/dd/config', '/home/dd/.local/share/dynamic-dispatch',
                     '/srv/dynamic-dispatch/repos', '/srv/dynamic-dispatch/worktrees', '/etc/dynamic-dispatch',
                     '/home/dd-gemini'):
            self.assertRegex(compose, rf'\n      - dd-[a-z-]+:{path}\n')


if __name__ == '__main__':
    unittest.main()

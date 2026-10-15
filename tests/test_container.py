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
import subprocess
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
        self.agent_homes = {'claude': Path(folder.name) / 'dd-agent', 'codex': Path(folder.name) / 'dd-codex',
                            'gemini': self.gemini_home}
        self.credentials = Path(folder.name) / 'credentials'
        for path in (self.checkout, self.home / 'config', self.home / 'history',
                     *self.agent_homes.values(), self.credentials):
            path.mkdir(parents=True)
        self.credentials.chmod(0o700)
        for name, value in [('HOME', self.home), ('CHECKOUT', self.checkout),
                            ('CONFIG', self.home / 'config'), ('HISTORY', self.home / 'history'),
                            ('GEMINI_HOME', self.gemini_home), ('AGENT_HOMES', self.agent_homes),
                            ('CREDENTIALS', self.credentials),
                            ('CREDENTIAL_MOUNT', self.credentials / 'gemini.json'),
                            ('CREDENTIAL', self.gemini_home / 'gemini-credential.json')]:
            patch(self, name, value)


class Bootstrap(Folders):
    def run_bootstrap(self, *, decisions=(0,), installer=0, agents=None):
        """Start as root would, recording the order of each step instead of doing it."""
        steps = []
        environment = {} if agents is None else {start.AGENTS_SETTING: agents}
        expected = list(dict.fromkeys((agents or start.DEFAULT_AGENTS).split()))
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
            self.assertEqual(args, start.INSTALL + expected)
            self.assertEqual(kwargs, {'cwd': self.checkout})
            return mock.Mock(returncode=installer)

        with mock.patch.object(start, 'as_dd', side_effect=as_dd), \
                mock.patch.object(start.subprocess, 'run', side_effect=run), \
                mock.patch.object(start.pwd, 'getpwnam', return_value=ACCOUNT) as accounts, \
                mock.patch.dict(os.environ, environment), \
                mock.patch.object(start, 'hand_over_credential',
                                  side_effect=lambda account: steps.append('credential')), \
                mock.patch.object(start.time, 'sleep') as sleep, \
                mock.patch.object(start, 'drop_to_dd', side_effect=lambda account: steps.append('drop')), \
                mock.patch('sys.stdout', new_callable=io.StringIO) as output:
            try:
                start.bootstrap()
            except RuntimeError as error:
                steps.append(str(error))
        self.output, self.sleep, self.accounts = output.getvalue(), sleep, accounts
        return steps

    def test_first_start_clones_public_main_as_dd_before_anything_else(self):
        self.assertEqual(self.run_bootstrap(), ['clone', 'decision', 'install', 'credential', 'drop'])
        self.assertIn(start.PUBLIC_REPO, self.output)
        for path in (self.home, self.checkout, self.home / 'config', self.gemini_home):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)

    def test_normal_start_reinstalls_from_the_checkout_after_recovery(self):
        (self.checkout / '.git').mkdir()
        self.assertEqual(self.run_bootstrap(), ['decision', 'install', 'credential', 'drop'])

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
        self.assertEqual(steps, ['decision'] * 3 + ['install', 'credential', 'drop'])
        self.assertEqual(self.output.count('needs a hand'), 1)
        self.assertEqual(self.sleep.call_count, 2)

    def test_a_decision_that_fails_does_not_start_dd(self):
        (self.checkout / '.git').mkdir()
        self.assertEqual(self.run_bootstrap(decisions=(1,)),
                         ['decision', 'Could not decide whether DD needs rolling back'])

    def test_each_agent_alone_and_all_together(self):
        """Only the included agents are installed, own their home volumes, and Gemini alone gets the credential."""
        (self.checkout / '.git').mkdir()
        for setting in ('claude', 'codex', 'gemini', 'claude codex gemini', 'gemini  claude gemini'):
            with self.subTest(setting=setting):
                for home in self.agent_homes.values():
                    home.chmod(0o755)
                agents = list(dict.fromkeys(setting.split()))
                credential = ['credential'] if 'gemini' in agents else []
                self.assertEqual(self.run_bootstrap(agents=setting),
                                 ['decision', 'install', *credential, 'drop'])
                for agent, home in self.agent_homes.items():
                    self.assertEqual(stat.S_IMODE(home.stat().st_mode), 0o700 if agent in agents else 0o755)
                looked_up = [call.args[0] for call in self.accounts.call_args_list]
                self.assertEqual(looked_up, ['dd'] + [start.AGENT_ACCOUNTS[agent] for agent in agents])

    def test_an_image_without_the_setting_included_only_gemini(self):
        (self.checkout / '.git').mkdir()
        self.agent_homes['claude'].chmod(0o755)
        os.environ.pop(start.AGENTS_SETTING, None)
        self.assertEqual(self.run_bootstrap(), ['decision', 'install', 'credential', 'drop'])
        self.assertEqual(stat.S_IMODE(self.agent_homes['claude'].stat().st_mode), 0o755)

    def test_an_unknown_or_empty_setting_stops_before_anything(self):
        self.home.chmod(0o755)
        for setting in ('', ' ', 'claude cursor', 'claude,codex'):
            with self.subTest(setting=setting):
                steps = self.run_bootstrap(agents=setting)
                self.assertEqual(len(steps), 1)
                self.assertIn('DD_AGENTS must name agents from claude, codex, gemini', steps[0])
                self.assertEqual(stat.S_IMODE(self.home.stat().st_mode), 0o755)


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


def build_step(dockerfile, beginning):
    """The shell script of the Dockerfile RUN step that begins with beginning."""
    step = re.search(r'^RUN ' + re.escape(beginning) + r'(?:.*\\\n)*.*$', dockerfile, re.M).group(0)
    # Docker joins continued lines into one before the shell sees them.
    return step.removeprefix('RUN ').replace('\\\n', '')


class Image(unittest.TestCase):
    # Each agent's npm package.
    TOOLS = {'claude': '@anthropic-ai/claude-code', 'codex': '@openai/codex', 'gemini': '@google/gemini-cli'}

    def setUp(self):
        self.dockerfile = (ROOT / 'deploy' / 'Dockerfile').read_text()

    def build(self, beginning, agents):
        """Run a build step's script with fake system commands, returning what they were asked to do."""
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        bin_folder, log = Path(folder.name), Path(folder.name) / 'log'
        for name in ('npm', 'id', 'groupadd', 'useradd', 'chmod'):
            fake = bin_folder / name
            # A fresh image: id finds no account yet, until useradd has made it.
            fake.write_text(f'#!/bin/sh\nif [ "$0" != "${{0%/id}}" ]; then grep -q " $2\\$" "{log}" 2>/dev/null; exit; fi\n'
                            f'echo "$(basename "$0") $*" >> "{log}"\n')
            fake.chmod(0o755)
        result = subprocess.run(['/bin/sh', '-c', build_step(self.dockerfile, beginning)], capture_output=True, text=True,
                                env={'PATH': f'{bin_folder}:/usr/bin:/bin', 'DD_AGENTS': agents})
        return result, log.read_text().splitlines() if log.exists() else []

    def test_marks_the_container_and_defaults_to_gemini(self):
        self.assertIn('ARG DD_AGENTS=gemini\n', self.dockerfile)
        self.assertIn('ENV DD_CONTAINER=1 PYTHONUNBUFFERED=1 DD_AGENTS="${DD_AGENTS}"\n', self.dockerfile)
        self.assertIn('dd-install --container --dd-user dd --agents $DD_AGENTS ', self.dockerfile)
        self.assertEqual(start.INSTALL[1:], ['deploy/dd-install', '--container', '--dd-user', 'dd', '--agents'])
        self.assertEqual(start.DEFAULT_AGENTS, 'gemini')
        self.assertEqual(start.AGENT_ACCOUNTS, installer.AGENTS)
        compose = (ROOT / 'deploy' / 'compose.yaml').read_text()
        self.assertIn('        DD_AGENTS: ${DD_AGENTS:-gemini}\n', compose)

    def test_installs_each_chosen_tool_at_a_pinned_version(self):
        for setting in ('claude', 'codex', 'gemini', 'claude codex gemini'):
            with self.subTest(setting=setting):
                result, log = self.build('set -eu; packages=', setting)
                self.assertEqual(result.returncode, 0, result.stderr)
                install, clean = log
                self.assertEqual(clean, 'npm cache clean --force')
                packages = install.split()[3:]
                self.assertEqual(install.split()[:3], ['npm', 'install', '--global'])
                self.assertEqual([package.rpartition('@')[0] for package in packages],
                                 [self.TOOLS[agent] for agent in setting.split()])
                for package in packages:
                    self.assertRegex(package, r'@\d+\.\d+\.\d+$')
        self.assertIn('@google/gemini-cli@0.59.0', self.dockerfile)

    def test_an_unknown_or_empty_choice_stops_the_build(self):
        for setting in ('', 'claude cursor', 'claude,codex'):
            with self.subTest(setting=setting):
                result, log = self.build('set -eu; packages=', setting)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('DD_AGENTS', result.stderr)
                self.assertEqual(log, [])

    def test_gives_each_chosen_agent_its_account_with_a_fixed_id(self):
        ids = {'claude': '1003', 'codex': '1004', 'gemini': '1002'}
        for setting in ('claude', 'codex', 'gemini', 'claude codex gemini', 'gemini gemini'):
            with self.subTest(setting=setting):
                result, log = self.build('set -eu; useradd', setting)
                self.assertEqual(result.returncode, 0, result.stderr)
                expected = ['useradd --create-home --uid 1001 --user-group --shell /bin/bash dd', 'chmod 700 /home/dd']
                for agent in dict.fromkeys(setting.split()):
                    account = start.AGENT_ACCOUNTS[agent]
                    expected += [f'groupadd --gid {ids[agent]} {account}',
                                 f'useradd --create-home --uid {ids[agent]} --gid {account} --shell /bin/bash {account}',
                                 f'chmod 700 /home/{account}']
                    self.assertEqual(start.AGENT_HOMES[agent], Path('/home') / account)
                self.assertEqual(log, expected)

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
            args = mock.Mock(container=True, dd_user='dd', agents=list(installer.AGENTS))
            checkout = installer.preflight(args)
            # A fresh image: no accounts, groups or files yet, so every step is planned.
            with mock.patch.object(installer, 'inspect_path',
                                   return_value=mock.Mock(st_uid=0, st_mode=stat.S_IFDIR | 0o755)):
                plan = installer.build_plan('dd', checkout, list(installer.AGENTS), container=True)
        for _, action, step in plan.steps:
            if action is installer.command:
                run.append(step[0][0])
            elif step[0] == Path('/etc/sudoers.d/dynamic-dispatch'):
                run.append('visudo')  # checks the rule before installing it
        needed = set(checked) - set(installer.AGENTS) | set(run) | {'runuser', 'git'}
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
                     '/home/dd-agent', '/home/dd-codex', '/home/dd-gemini'):
            self.assertRegex(compose, rf'\n      - dd-[a-z-]+:{path}\n')


if __name__ == '__main__':
    unittest.main()

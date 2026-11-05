"""Memory accounting and sandbox failure provenance, without killing the test account."""

import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from dispatch import agents, config, mainhealth, prepare, proc, verify
from helpers import Base, REPO_CODE, S_BLK, board, fake_run_sandboxed, real_run_sandboxed


MEMORY = 'dd-agent-run: memory limit exceeded: 32 MB\n'
USAGE = 'usage: dd-agent-run --cwd DIR --timeout SECONDS [--env-file FILE] -- COMMAND [ARGS...]\n'
TEST_MESSAGE = 'Tests were stopped after using more than 32 MB of memory; look for a loop that never ends'


def stopped():
    return proc.Result(proc.MEMORY_EXIT_CODE, '', MEMORY, False, False)


class Memory(unittest.TestCase):
    def test_machine_default_and_setting(self):
        with mock.patch('builtins.open', mock.mock_open(read_data='MemTotal:       8192000 kB\nMemFree: 1024 kB\n')):
            self.assertEqual(config.total_memory_mb(), 8000)
            self.assertEqual(config.memory_limit_mb({'sandbox': {}}), 3200)
        for value in (0, -1, True, 1.5, '32', None):
            with self.subTest(value=value), self.assertRaises(config.ConfigError):
                config.memory_limit_mb({'sandbox': {'memory_limit_mb': value}})
        cfg = {'sandbox': {'user': 'agent', 'launcher': 'launcher', 'memory_limit_mb': 32}}
        command = proc.sandboxed(cfg, ['test'], '/checkout', 60)
        self.assertEqual(command[command.index('--memory-limit-mb') + 1], '32')

    def test_codes_alone_are_normal_failures(self):
        for code in (64, proc.MEMORY_EXIT_CODE):
            result = proc.Result(code, '', 'project failure', False, False)
            self.assertIsNone(proc.memory_stopped(result))
            self.assertFalse(proc.launcher_outdated(result))
            self.assertIn(f'Exited with code {code}', proc.describe_failure(result, 10))
            with mock.patch.object(proc, 'run_sandboxed', return_value=result):
                self.assertFalse(verify.run_verify({}, {'verify_command': ['test']}, {}, '/checkout')[0])
        self.assertIsNone(proc.memory_stopped(proc.Result(1, '', MEMORY, False, False)))
        self.assertFalse(proc.launcher_outdated(proc.Result(1, '', USAGE, False, False)))
        self.assertFalse(proc.launcher_outdated(proc.Result(64, USAGE, '', False, False)))

    def test_test_and_prepare_messages(self):
        with mock.patch.object(proc, 'run_sandboxed', return_value=stopped()):
            for runner in (verify.run_verify, mainhealth._verify):
                self.assertEqual(runner({}, {'verify_command': ['test']}, {}, '/checkout'),
                                 (False, TEST_MESSAGE))
            ok, output = prepare.run({}, {'prepare_commands': [['prepare']]}, {}, '/checkout')
            self.assertFalse(ok)
            self.assertIn(TEST_MESSAGE, output)

    def test_old_launcher_is_not_failing_tests_or_prepare(self):
        result = proc.Result(64, '', USAGE, False, False)
        with mock.patch.object(proc, 'run_sandboxed', return_value=result):
            for runner, project in ((verify.run_verify, {'verify_command': ['test']}),
                                    (mainhealth._verify, {'verify_command': ['test']}),
                                    (prepare.run, {'prepare_commands': [['prepare']]})):
                with self.subTest(runner=runner), self.assertRaisesRegex(proc.LauncherOutdated, 'sudo python3 deploy/dd-install'):
                    runner({}, project, {}, '/checkout')

    def test_memory_wins_over_usage_and_login_output(self):
        result = stopped()
        result.stdout = json.dumps({'is_error': True, 'result': 'Usage limit reached; resets 5pm'})
        result.stderr = 'Invalid API key. Please run /login\n' + MEMORY
        with mock.patch.object(proc, 'run_sandboxed', return_value=result), mock.patch.object(agents.logins, 'login') as login:
            outcome = agents.run({}, {'parser': 'claude_json', 'command': ['agent'], 'display_name': 'Agent'}, '', '.')
        self.assertFalse(outcome['usage_limit'])
        self.assertTrue(outcome['memory_stopped'])
        self.assertIn('used more than 32 MB of memory and was stopped to protect the machine', outcome['error'])
        login.assert_not_called()

    def test_reaping_and_helper_marker_do_not_mask_memory(self):
        cfg = {'sandbox': {'user': 'agent', 'launcher': 'launcher'}}
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / 'failed'
            marker.touch()
            with mock.patch.object(proc, 'reap', side_effect=[None, 'cleanup failure']), \
                    mock.patch.object(proc, 'run_capped', return_value=stopped()), \
                    mock.patch.object(proc.services, 'around') as around:
                around.return_value.__enter__.return_value = (['test'], marker)
                result = real_run_sandboxed(cfg, ['test'], folder, 30)
        self.assertEqual(proc.memory_stopped(result), 32)

    def test_launcher_stops_real_memory_growth_and_leaves_small_command_alone(self):
        # Substitute only the process view and account-wide kill in a test copy.
        # Real RSS is read through symlinks; only explicitly registered test PIDs
        # can be killed. This works without root and cannot kill the test runner.
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            view = root / 'processes'
            view.mkdir()
            launcher = (REPO_CODE / 'deploy/dd-agent-run').read_text()
            launcher = launcher.replace('WORKTREE_ROOT="/srv/dynamic-dispatch/worktrees"', f'WORKTREE_ROOT="{folder}"')
            launcher = launcher.replace('/proc/[0-9]*', f'{view}/[0-9]*')
            launcher = launcher.replace('kill -KILL -1', f'for entry in {view}/[0-9]*; do kill -KILL "${{entry##*/}}" 2>/dev/null || true; done')
            script = root / 'launcher'
            script.write_text(launcher)
            work = root / 'work'
            work.mkdir()
            for grow in (False, True, "helpers"):
                code = (f'import os, time; os.symlink("/proc/" + str(os.getpid()), "{view}/" + str(os.getpid())); '
                        + ('import sys; sys.stderr.write("partial output"); sys.stderr.flush(); data = bytearray(96 * 1024 * 1024); time.sleep(10)' if grow else 'print("small command")'))
                if grow == "helpers":
                    child = (f'import os, time; os.symlink("/proc/" + str(os.getpid()), "{view}/" + str(os.getpid())); '
                             'data = bytearray(12 * 1024 * 1024); time.sleep(10)')
                    code = (f'import subprocess; child = subprocess.Popen(["/usr/bin/python3", "-c", {child!r}]); '
                            + code.replace('96 * 1024', '12 * 1024'))
                start = time.monotonic()
                result = subprocess.run(['bash', str(script), '--cwd', str(work), '--timeout', '15',
                                         '--memory-limit-mb', '32', '--', '/usr/bin/python3', '-c', code],
                                        capture_output=True, text=True, timeout=20)
                if grow:
                    self.assertEqual(result.returncode, proc.MEMORY_EXIT_CODE, result.stderr)
                    self.assertIn(MEMORY, result.stderr)
                    self.assertEqual(proc.memory_stopped(proc.Result(result.returncode, result.stdout,
                                                                    result.stderr, False, False)), 32)
                    self.assertLess(time.monotonic() - start, 3)
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn('small command', result.stdout)


class StoryMemory(Base):
    def test_memory_blocks_without_retry(self):
        self.add_issue()
        with mock.patch.object(proc, 'run_sandboxed', return_value=stopped()) as run:
            self.cycle()
        self.assertEqual(run.call_count, 1)
        self.assertEqual(board.issues['i1']['state']['id'], S_BLK)
        self.assertIn('stopped to protect the machine', self.run_rows()[0]['error'])
        self.assertIn('used more than 32 MB', board.comments[-1][2])

    def test_old_launcher_blocks_agent_test_and_prepare_without_retry(self):
        for stage in ('agent', 'test', 'prepare'):
            with self.subTest(stage=stage):
                iid = 'old-' + stage
                self.add_issue(iid=iid, seq=20 + len(self.run_rows()))
                project = self.cfg['projects']['dd']
                project['prepare_commands'] = [['prepare']] if stage == 'prepare' else []
                def launch(cfg, command, **kwargs):
                    is_agent = 'fake_agent.py' in ' '.join(command)
                    if stage == 'agent' or (stage == 'test' and not is_agent) or stage == 'prepare':
                        return proc.Result(64, '', USAGE, False, False)
                    return fake_run_sandboxed(cfg, command, **kwargs)
                with mock.patch.object(proc, 'run_sandboxed', side_effect=launch):
                    self.cycle()
                self.assertEqual(board.issues[iid]['state']['id'], S_BLK)
                self.assertIn(proc.LAUNCHER_REINSTALL, self.run_rows()[-1]['error'])

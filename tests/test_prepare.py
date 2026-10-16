"""Checkout preparation: sandbox execution and lifecycle gates."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dispatch import add_project, agents, combine, config, doctor, mainhealth, plane, prepare, proc, runs
from helpers import Base, MainChanges, board, S_BLK


class PrepareCommands(unittest.TestCase):
    def test_omitted_is_noop(self):
        with mock.patch.object(proc, 'run_sandboxed') as run:
            self.assertEqual(prepare.run({}, {}, {}, '/checkout'), (True, ''))
        run.assert_not_called()

    def test_order_account_folder_and_timeout(self):
        commands = [['install', 'dependencies'], ['generate']]
        project = dict(prepare_commands=commands, prepare_timeout_seconds=42)
        with mock.patch.object(proc, 'run_sandboxed', return_value=proc.Result(0, '', '', False, False)) as run:
            self.assertEqual(prepare.run({}, project, {'run_as': 'builder'}, '/checkout'), (True, ''))
        self.assertEqual(run.call_args_list, [
            mock.call({}, c, cwd='/checkout', timeout=42, run_as='builder') for c in commands])

    def test_failure_and_timeout_stop_sequence_with_output(self):
        for result in (proc.Result(1, 'install output', 'install error', False, False),
                       proc.Result(124, 'slow install', '', True, False)):
            with self.subTest(result=result), mock.patch.object(proc, 'run_sandboxed', return_value=result) as run:
                ok, output = prepare.run({}, {'prepare_commands': [['first'], ['second']]}, {}, '/checkout')
            self.assertFalse(ok)
            self.assertIn('first', output)
            self.assertIn(result.stdout, output)
            if result.timed_out:
                self.assertIn('900s', output)
            self.assertEqual(run.call_count, 1)

    def test_validation(self):
        for commands in (None, 'npm ci', [[]], ['npm ci'], [[1]], [['']], [['x', None]], [['x\0']]):
            with self.subTest(commands=commands), self.assertRaises(config.ConfigError):
                config._check_prepare({'projects': {'p': {'prepare_commands': commands}}})
        for timeout in (0, -1, True, 1.5, '900', None):
            with self.subTest(timeout=timeout), self.assertRaises(config.ConfigError):
                config._check_prepare({'projects': {'p': {'prepare_timeout_seconds': timeout}}})
        config._check_prepare({'projects': {'p': {'prepare_commands': [['x', '']], 'prepare_timeout_seconds': 1}}})

    def test_suggestions_and_confirmation_never_execute(self):
        examples = {
            'package-lock.json': ['npm', 'ci'],
            'pnpm-lock.yaml': ['pnpm', 'install', '--frozen-lockfile'],
            'yarn.lock': ['yarn', 'install', '--frozen-lockfile'],
            'go.mod': ['go', 'mod', 'download'],
            'Cargo.toml': ['cargo', 'fetch'],
        }
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(proc, 'run_sandboxed') as run:
            for filename, command in examples.items():
                path = Path(folder) / filename
                path.touch()
                self.assertEqual(add_project.suggest_prepare(folder), [command])
                path.unlink()
            self.assertEqual(add_project.suggest_prepare(folder), [])
            with mock.patch('builtins.input', side_effect=['', 'custom "two words"', 'skip']):
                self.assertEqual(add_project._prepare_answers([['npm', 'ci'], ['go'], ['cargo']]),
                                 [['npm', 'ci'], ['custom', 'two words']])
        run.assert_not_called()

    def test_doctor_checks_every_program_on_sandbox_path(self):
        def check(name, action, good, bad):
            action()
        def require(value):
            self.assertTrue(value)
        with mock.patch.object(doctor.shutil, 'which', return_value='/bin/tool') as which:
            doctor._check_prepare({'projects': {'p': {'prepare_commands': [['npm', 'ci'], ['go', 'mod']],
                                                       'repo_path': '/checkout'}}}, check, require)
        self.assertEqual(which.call_args_list, [mock.call(p, path=doctor.AGENT_PATH) for p in ('npm', 'go')])

    def test_saved_combination_does_not_repeat_startup_preparation(self):
        ctx = mock.Mock(cfg={}, project_cfg={'prepare_commands': [['install']]},
                        agent_cfg={}, worktree='/checkout')
        with mock.patch.object(prepare, 'run') as install, \
                mock.patch.object(combine.verify, 'run_verify', return_value=(True, '')) as verify, \
                mock.patch.object(mainhealth, 'hold', return_value=False), \
                mock.patch.object(combine.gitops, 'has_changes', return_value=False):
            self.assertTrue(combine._verify_saved(ctx, 'saved work', {}))
        install.assert_not_called()
        verify.assert_called_once_with({}, ctx.project_cfg, {}, '/checkout')

    def test_main_prepares_before_tests_and_failure_is_unavailable(self):
        project = {'prepare_commands': [['install'], ['generate']], 'verify_command': ['test']}
        with mock.patch.object(proc, 'run_sandboxed', return_value=proc.Result(0, '', '', False, False)) as run:
            self.assertTrue(mainhealth._verify({}, project, {'run_as': 'builder'}, '/main-check')[0])
        self.assertEqual([c.args[1] for c in run.call_args_list], [['install'], ['generate'], ['test']])
        self.assertTrue(all(c.kwargs['cwd'] == '/main-check' and c.kwargs['run_as'] == 'builder'
                            for c in run.call_args_list))
        with mock.patch.object(proc, 'run_sandboxed', return_value=proc.Result(1, 'bad setup', '', False, False)) as run:
            with self.assertRaisesRegex(mainhealth.NotTested, 'bad setup'):
                mainhealth._verify({}, project, {}, '/main-check')
        self.assertEqual(run.call_count, 1)


class PrepareLifecycle(MainChanges, Base):
    def setUp(self):
        super().setUp()
        self.cfg['projects']['dd']['prepare_commands'] = [['install'], ['generate']]

    def test_fresh_failure_blocks_without_agent(self):
        self.add_issue()
        with mock.patch.object(prepare, 'run', return_value=(False, 'dependency unavailable')), \
                mock.patch.object(agents, 'run') as agent:
            self.cycle()
        row = self.run_rows()[0]
        self.assertEqual(row['status'], runs.FAILED)
        self.assertEqual(plane.state_id(board.issues[row['issue_id']]), S_BLK)
        self.assertIn('Environment preparation failed', row['error'])
        self.assertIn('dependency unavailable', row['error'])
        agent.assert_not_called()

    def test_story_and_combination_prepare_in_order(self):
        self.add_issue()
        real_agent = agents.run
        calls = []
        def agent(*args):
            calls.append('agent')
            result = real_agent(*args)
            self.advance_main()
            return result
        real_sandbox = proc.run_sandboxed
        def sandbox(cfg, command, **kwargs):
            if command[0] in ('install', 'generate'):
                calls.append(command[0])
                self.assertEqual(Path(kwargs['cwd']).name, 'dd-7')
                return proc.Result(0, '', '', False, False)
            return real_sandbox(cfg, command, **kwargs)
        with mock.patch.object(proc, 'run_sandboxed', side_effect=sandbox), \
                mock.patch.object(agents, 'run', side_effect=agent):
            self.cycle()
        self.assertEqual(self.run_rows()[0]['status'], runs.DONE)
        self.assertEqual(calls, ['install', 'generate', 'agent', 'install', 'generate', 'install', 'generate'])

    def test_failure_after_agent_is_sent_back(self):
        self.add_issue()
        with mock.patch.object(prepare, 'run', side_effect=[
                (True, ''), (False, 'changed manifest failed'), (True, '')]), \
                mock.patch.object(agents, 'run', wraps=agents.run) as agent:
            self.cycle()
        self.assertEqual(agent.call_count, 2)
        self.assertIn('changed manifest failed', agent.call_args.args[2])
        self.assertTrue(any(e['type'] == 'sent_back' for e in runs.events(self.conn, run_id=self.run_rows()[0]['id'])))
        self.assertEqual(self.run_rows()[0]['status'], runs.DONE)

    def test_rebuilt_refresh_prepares_before_combining(self):
        self.add_issue()
        with mock.patch.object(prepare, 'run', return_value=(True, '')):
            self.cycle()
        self.advance_main()
        calls = []
        original_combine = combine.with_main
        def prepared(*args):
            calls.append('prepare')
            return True, ''
        def combined(*args):
            calls.append('combine')
            return original_combine(*args)
        with mock.patch.object(prepare, 'run', side_effect=prepared), \
                mock.patch.object(combine, 'with_main', side_effect=combined), \
                mock.patch.object(agents, 'run') as agent:
            self.cycle()
        self.assertEqual(calls, ['prepare', 'combine', 'prepare'])
        agent.assert_not_called()
        row = self.run_rows()[-1]
        self.assertEqual((row['kind'], row['status']), ('refresh', runs.DONE))

    def test_combined_prepare_failure_goes_back_to_agent(self):
        self.add_issue()
        original = agents.run
        prompts = []
        def agent(*args):
            prompts.append(args[2])
            result = original(*args)
            if len(prompts) == 1:
                self.advance_main()
            return result
        with mock.patch.object(prepare, 'run', side_effect=[
                (True, ''), (True, ''), (False, 'combined manifest failed'), (True, '')]), \
                mock.patch.object(agents, 'run', side_effect=agent):
            self.cycle()
        self.assertEqual(len(prompts), 2)
        self.assertIn('combined manifest failed', prompts[1])
        self.assertEqual(self.run_rows()[0]['status'], runs.DONE)


class PrepareMainCheck(Base):
    main_checks = True

    def test_main_check_prepares_each_rebuilt_folder_before_testing(self):
        self.cfg['projects']['dd']['prepare_commands'] = [['install'], ['generate']]
        real = proc.run_sandboxed
        calls = []
        test_count = 0
        def sandbox(cfg, command, **kwargs):
            nonlocal test_count
            calls.append(command[0] if command[0] in ('install', 'generate') else 'test')
            self.assertIn('main-check', str(kwargs['cwd']))
            if command[0] in ('install', 'generate'):
                return proc.Result(0, '', '', False, False)
            test_count += 1
            if test_count == 1:
                return proc.Result(1, 'transient test failure', '', False, False)
            return real(cfg, command, **kwargs)
        with mock.patch.object(proc, 'run_sandboxed', side_effect=sandbox):
            self.cycle()
        self.assertEqual(calls, ['install', 'generate', 'test'] * 2)
        self.assertEqual(runs.main_health(self.conn, 'dd')['state'], 'green')

    def test_main_prepare_failure_keeps_health_unknown(self):
        self.cfg['projects']['dd']['prepare_commands'] = [['install']]
        with mock.patch.object(prepare, 'run', return_value=(False, 'cannot install')):
            self.cycle()
        self.assertIsNone(runs.main_health(self.conn, 'dd'))
        self.assertTrue(any('cannot install' in e['detail'] for e in runs.events(self.conn)))

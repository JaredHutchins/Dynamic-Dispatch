"""Helper programs: started around each sandboxed run that needs them, and stopped after."""

import concurrent.futures
import contextlib
import getpass
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from dispatch import agents, config, doctor, mainhealth, plane, prepare, proc, runs, services, verify
from helpers import REPO_CODE, Base, real_run_sandboxed, MainChanges, S_BLK, board, temporary_directory

PY = sys.executable
ME = getpass.getuser()
# A stand-in database: listens on its port, notes its process in its data folder, and logs.
SERVER = """import os, socket, sys, time
from pathlib import Path
with open(sys.argv[1], 'a') as log:
    log.write('started\\n')
s = socket.socket()
s.bind(('127.0.0.1', int(os.environ['DD_SERVICE_DB_PORT'])))
s.listen()
Path(os.environ['DD_SERVICE_DB_DATA_DIR'], 'pid').write_text(str(os.getpid()))
print('helper output', flush=True)
time.sleep(60)
"""
# The tests: report what they were given, and log that they ran.
TESTS = """import json, os, sys
from pathlib import Path
data = Path(os.environ['DD_SERVICE_DB_DATA_DIR'])
with open(sys.argv[1], 'a') as log:
    log.write('tests\\n')
print(json.dumps([str(data), int((data / 'pid').read_text()), os.environ['DD_SERVICE_DB_PORT']]), flush=True)
"""


def helper(log, ready='port', command=None, timeout=5):
    return {'name': 'db', 'command': command or [PY, '-c', SERVER, str(log)], 'ready': ready,
            'timeout_seconds': timeout}


def gone(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def running(token):
    """Whether any process has token in its command line."""
    for entry in Path('/proc').iterdir():
        try:
            if token in (entry / 'cmdline').read_bytes().decode(errors='replace'):
                return True
        except OSError:
            continue
    return False


def unsandboxed(command, **kwargs):
    """run_capped without sudo or the launcher: the rest of the call is real."""
    if command[-1] == '--reap':
        return proc.Result(0, '', '', False, False)
    return real_run_capped(command[command.index('--') + 1:], **kwargs)


real_run_capped = proc.run_capped
CFG = {'sandbox': {'user': ME, 'launcher': '/usr/local/bin/dd-agent-run'}}


class Runs(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.log = self.folder / 'log'
        patch = mock.patch.object(proc, 'run_capped', side_effect=unsandboxed)
        self.run_capped = patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(proc, 'run_sandboxed', real_run_sandboxed)
        patch.start()
        self.addCleanup(patch.stop)

    def run_tests(self, project, code=TESTS, timeout=10, helpers=True):
        return proc.run_sandboxed(CFG, [PY, '-c', code, str(self.log)], self.folder, timeout,
                                  project_cfg=project, helpers=helpers)

    def test_no_setting_runs_the_command_unchanged(self):
        result = self.run_tests({}, 'print("plain")')
        self.assertEqual((result.returncode, result.stdout.strip()), (0, 'plain'))
        launched = self.run_capped.call_args_list[1].args[0]
        self.assertEqual(launched[launched.index('--') + 1:], [PY, '-c', 'print("plain")', str(self.log)])
        self.assertEqual(launched[launched.index('--timeout') + 1], '10')  # no extra time either

    def test_prepare_never_starts_helpers(self):
        project = {'services': [helper(self.log)], 'prepare_commands': [[PY, '-c', 'print(1)']]}
        self.assertEqual(prepare.run(CFG, project, {}, self.folder), (True, ''))
        launched = self.run_capped.call_args_list[1].args[0]
        self.assertEqual(launched[launched.index('--') + 1:], [PY, '-c', 'print(1)'])
        self.assertFalse(self.log.exists())

    def test_start_then_ready_then_tests_then_stop_and_data_removed(self):
        result = self.run_tests({'services': [helper(self.log)]})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(result.helpers_failed)
        data, pid, port = json.loads(result.stdout)
        self.assertEqual(self.log.read_text(), 'started\ntests\n')
        self.assertTrue(gone(pid))
        self.assertFalse(Path(data).parent.exists())
        self.assertTrue(port.isdigit())
        self.assertNotIn('helper output', result.stdout)  # the helper's output isn't the tests'
        self.assertEqual(services.PORTS, set())
        launched = self.run_capped.call_args_list[1].args[0]
        self.assertEqual(launched[launched.index('--timeout') + 1], str(10 + 5 + services.STOP_GRACE_SECONDS))

    def test_ready_command_with_placeholders(self):
        ready = [PY, '-c', 'import pathlib, sys; sys.exit(not pathlib.Path(sys.argv[1], "pid").exists())',
                 '${DD_SERVICE_DB_DATA_DIR}']
        result = self.run_tests({'services': [helper(self.log, ready)]})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.log.read_text(), 'started\ntests\n')

    def test_start_command_gets_its_port_as_an_argument(self):
        server = SERVER.replace("int(os.environ['DD_SERVICE_DB_PORT'])", 'int(sys.argv[2])')
        command = [PY, '-c', server, str(self.log), '${DD_SERVICE_DB_PORT}']
        result = self.run_tests({'services': [helper(self.log, command=command)]})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_time_out_fails_with_output_and_skips_tests(self):
        never = [PY, '-c', 'import sys; sys.exit(1)']
        result = self.run_tests({'services': [helper(self.log, never, timeout=1)]})
        self.assertTrue(result.helpers_failed)
        self.assertIsNone(result.returncode)
        self.assertIn('Helper program db was not ready within 1s', result.stderr)
        self.assertIn('helper output', result.stderr)
        self.assertEqual(self.log.read_text(), 'started\n')
        self.assertIn('was not ready', proc.describe_failure(result, 10))

    def test_start_failures_fail_with_output(self):
        crash = [PY, '-c', 'print("no room on disk", flush=True); raise SystemExit(3)']
        for command, expected in ((crash, 'exited with code 3 before it was ready'),
                                  (['/no/such/helper'], 'could not start')):
            with self.subTest(command=command):
                result = self.run_tests({'services': [helper(self.log, command=command)]})
                self.assertTrue(result.helpers_failed)
                self.assertIn(expected, result.stderr)
        self.assertIn('no room on disk', self.run_tests({'services': [helper(self.log, command=crash)]}).stderr)
        self.assertFalse(self.log.exists())

    def test_missing_ready_program_is_a_startup_failure(self):
        markers = []
        real_around = services.around
        @contextlib.contextmanager
        def around(*args):
            with real_around(*args) as (command, marker):
                markers.append(marker)
                yield command, marker
                self.assertTrue(marker.exists())  # the runner marked it a start-up failure
                self.assertFalse(marker.with_name('db').exists())  # and removed the helper's data
        agent = {'parser': 'claude_json', 'command': [PY, '-c', TESTS, str(self.log)], 'display_name': 'Builder'}
        project = {'services': [helper(self.log, ['/no/such/probe'])]}
        with mock.patch.object(services, 'around', around):
            result = agents.run(CFG, agent, 'prompt', self.folder, project)
        self.assertFalse(result['ok'])
        self.assertTrue(result['helpers_failed'])
        self.assertIn('Builder never started: Helper program db could not run its ready check', result['error'])
        self.assertIn('/no/such/probe', result['error'])
        self.assertNotIn('tests', self.log.read_text() if self.log.exists() else '')
        self.assertFalse(running(str(self.log)))  # the started helper was stopped
        self.assertFalse(markers[0].parent.exists())
        self.assertEqual(services.PORTS, set())

    def test_a_failing_test_is_not_a_helper_failure(self):
        result = self.run_tests({'services': [helper(self.log)]}, 'raise SystemExit(125)')
        self.assertEqual(result.returncode, 125)
        self.assertFalse(result.helpers_failed)

    def test_test_time_limit_still_stops_helpers(self):
        code = TESTS + 'import time; time.sleep(30)\n'
        result = self.run_tests({'services': [helper(self.log)]}, code, timeout=2)
        self.assertIn(result.returncode, proc.TIMEOUT_EXIT_CODES)
        self.assertTrue(gone(json.loads(result.stdout)[1]))

    def test_simultaneous_runs_get_separate_ports(self):
        code = TESTS + 'import time; time.sleep(1)\n'
        project = {'services': [helper(self.log)]}
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: self.run_tests(project, code), range(2)))
        self.assertEqual([r.returncode for r in results], [0, 0], [r.stderr for r in results])
        ports = {json.loads(r.stdout)[2] for r in results}
        self.assertEqual(len(ports), 2)
        self.assertEqual(services.PORTS, set())

    def test_agent_runs_with_helpers_and_can_reach_them(self):
        code = ('import json, os, socket; '
                's = socket.create_connection(("127.0.0.1", int(os.environ["DD_SERVICE_DB_PORT"]))); s.close(); '
                'print(json.dumps({"type": "result", "result": "Reached the database.", "is_error": False}))')
        agent = {'parser': 'claude_json', 'command': [PY, '-c', code, '{prompt}'], 'display_name': 'Builder'}
        result = agents.run(CFG, agent, 'prompt', self.folder, {'services': [helper(self.log)]})
        self.assertTrue(result['ok'], result)


class Failures(unittest.TestCase):
    failed = proc.Result(None, '', 'Helper program db was not ready within 1s. Its output:\nstarting',
                         False, False, helpers_failed=True)

    def test_main_check_could_not_test_main(self):
        project = {'services': [helper('/log')], 'verify_command': ['tests']}
        with mock.patch.object(proc, 'run_sandboxed', return_value=self.failed) as run:
            with self.assertRaisesRegex(mainhealth.NotTested, 'was not ready within 1s'):
                mainhealth._verify({}, project, {}, '/checkout')
        self.assertTrue(run.call_args.kwargs['helpers'])

    def test_story_tests_fail_with_the_helpers_output(self):
        with mock.patch.object(proc, 'run_sandboxed', return_value=self.failed) as run:
            ok, output = verify.run_verify({}, {'verify_command': ['tests']}, {}, '/checkout')
        self.assertFalse(ok)
        self.assertIn('Its output:\nstarting', output)
        self.assertTrue(run.call_args.kwargs['helpers'])

    def test_agent_never_started(self):
        agent = {'parser': 'claude_json', 'command': ['agent'], 'display_name': 'Builder'}
        with mock.patch.object(proc, 'run_sandboxed', return_value=self.failed):
            result = agents.run({}, agent, 'prompt', '/checkout', {})
        self.assertFalse(result['ok'])
        self.assertTrue(result['helpers_failed'])
        self.assertIn('Builder never started: Helper program db', result['error'])


class Settings(unittest.TestCase):
    def test_validation(self):
        services.validate({})
        services.validate({'services': []})
        services.validate({'services': [helper('/log'), dict(helper('/log', ['probe']), name='Cache_2')]})
        for value in (None, {}, [None], [dict(helper('/log'), name='bad-name')], [dict(helper('/log'), name='2db')],
                      [helper('/log'), dict(helper('/log'), name='DB')], [dict(helper('/log'), command=[])],
                      [dict(helper('/log'), command='postgres')], [dict(helper('/log'), command=[''])],
                      [dict(helper('/log'), ready=True)], [dict(helper('/log'), ready='socket')],
                      [{'name': 'db', 'ready': 'port'}], [dict(helper('/log'), timeout_seconds=True)],
                      [dict(helper('/log'), timeout_seconds=0)], [dict(helper('/log'), timeout_seconds=1.5)],
                      [dict(helper('/log'), port=5432)]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                services.validate({'services': value})

    def test_config_load_checks_helpers_and_names_the_project(self):
        cfg = json.loads((REPO_CODE / 'config.example.json').read_text())
        key = temporary_directory(self) / 'app.pem'
        key.write_text('fake key\n')
        key.chmod(0o600)
        cfg['github']['private_key_path'] = str(key)
        name, project = next(iter(cfg['projects'].items()))
        path = temporary_directory(self) / 'config.json'
        project['services'] = [{'name': 'postgres', 'command': ['postgres'], 'ready': ['pg_isready']}]
        path.write_text(json.dumps(cfg))
        path.chmod(0o600)
        self.assertEqual(config.load(path)['projects'][name]['services'], project['services'])
        project['services'][0]['ready'] = 'sometime'
        path.write_text(json.dumps(cfg))
        with self.assertRaisesRegex(config.ConfigError, f"project '{name}': Service postgres ready"):
            config.load(path)

    def test_default_time_limit(self):
        project = {'services': [dict(helper('/log'), timeout_seconds=None)]}
        del project['services'][0]['timeout_seconds']
        self.assertEqual(services.time_limit(project, 100), 100 + 60 + services.STOP_GRACE_SECONDS)
        self.assertEqual(services.time_limit({}, 100), 100)

    def test_doctor_checks_runner_start_and_ready_programs_on_sandbox_path(self):
        project = {'services': [helper('/log', ['probe'], ['postgres']),
                                helper('/log', 'port', ['bin/cache'])],
                   'repo_path': '/checkout', 'test_path': ['/opt/tools']}
        names = []
        def check(name, action, good, bad):
            names.append(name)
            action()
        with mock.patch.object(doctor.shutil, 'which', return_value='/bin/tool') as which:
            doctor._check_helpers({'projects': {'p': project}}, check, self.assertTrue)
        self.assertEqual([c.args[0] for c in which.call_args_list],
                         [services.PYTHON, 'postgres', 'probe', '/checkout/bin/cache'])
        self.assertTrue(all(c.kwargs['path'].startswith('/opt/tools:') for c in which.call_args_list))
        self.assertEqual(names[0], 'Project 1 helper runner')
        with mock.patch.object(doctor.shutil, 'which') as which:
            doctor._check_helpers({'projects': {'p': {}}}, check, self.assertTrue)
        which.assert_not_called()


class Lifecycle(MainChanges, Base):
    def setUp(self):
        super().setUp()
        self.cfg['projects']['dd']['services'] = [helper('/log')]

    def failing_helpers(self, which):
        real = proc.run_sandboxed
        def sandbox(cfg, command, **kwargs):
            self.assertIn('helpers', kwargs)
            if kwargs['helpers'] and which(command):
                return Failures.failed
            return real(cfg, command, **kwargs)
        return mock.patch.object(proc, 'run_sandboxed', side_effect=sandbox)

    def test_failure_before_the_agent_blocks_as_an_environment_problem(self):
        self.add_issue()
        verify_command = self.cfg['projects']['dd']['verify_command']
        with self.failing_helpers(lambda command: command != verify_command):
            self.cycle()
        row = self.run_rows()[0]
        self.assertEqual(row['status'], runs.FAILED)
        self.assertEqual(plane.state_id(board.issues[row['issue_id']]), S_BLK)
        self.assertIn('A helper program failed to start before the agent started; no agent work', row['error'])
        self.assertIn('Its output:\nstarting', row['error'])

    def test_missing_ready_program_blocks_as_an_environment_problem(self):
        log = temporary_directory(self) / 'log'
        self.cfg['projects']['dd']['services'] = [helper(log, ['/no/such/probe'])]
        self.add_issue()
        real = proc.run_sandboxed
        def sandbox(cfg, command, **kwargs):
            if not kwargs['helpers']:
                return real(cfg, command, **kwargs)
            with mock.patch.object(proc, 'run_capped', side_effect=unsandboxed):
                return real_run_sandboxed(CFG, command, **kwargs)
        with mock.patch.object(proc, 'run_sandboxed', side_effect=sandbox):
            self.cycle()
        row = self.run_rows()[0]
        self.assertEqual(row['status'], runs.FAILED)
        self.assertEqual(plane.state_id(board.issues[row['issue_id']]), S_BLK)
        self.assertIn('A helper program failed to start before the agent started; no agent work', row['error'])
        self.assertIn('Helper program db could not run its ready check', row['error'])
        self.assertIn('/no/such/probe', row['error'])
        self.assertFalse(running(str(log)))

    def test_failure_after_the_agents_changes_fails_the_tests(self):
        self.add_issue()
        verify_command = self.cfg['projects']['dd']['verify_command']
        with self.failing_helpers(lambda command: command == verify_command):
            self.cycle()
        row = self.run_rows()[0]
        self.assertEqual(row['status'], runs.FAILED)
        self.assertEqual(plane.state_id(board.issues[row['issue_id']]), S_BLK)
        self.assertIn('Verification failed', row['error'])
        self.assertIn('Its output:\nstarting', row['error'])
        self.assertNotIn('no agent work', row['error'])


if __name__ == '__main__':
    unittest.main()

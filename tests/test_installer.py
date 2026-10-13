"""The privileged installer is exercised with fake host state, never root."""

import argparse
import contextlib
import importlib.machinery
import importlib.util
import io
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / 'deploy/dd-install'
loader = importlib.machinery.SourceFileLoader('dd_installer', str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
installer = importlib.util.module_from_spec(spec)
loader.exec_module(installer)


class Installer(unittest.TestCase):
    def setUp(self):
        self.users = {'root': SimpleNamespace(pw_uid=0, pw_gid=0),
                      'dd': SimpleNamespace(pw_uid=1000, pw_gid=1000)}
        self.groups = {'root': SimpleNamespace(gr_gid=0, gr_mem=[])}
        self.files = {}
        self.commands = []
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(installer, 'user', side_effect=self.users.get))
        self.stack.enter_context(mock.patch.object(installer, 'group', side_effect=self.groups.get))
        self.stack.enter_context(mock.patch.object(installer, 'inspect_path', side_effect=self.inspect))
        self.stack.enter_context(mock.patch.object(installer, 'install_path', side_effect=self.install))
        self.stack.enter_context(mock.patch.object(installer, 'command', side_effect=self.command))
        self.stack.enter_context(mock.patch.object(Path, 'read_bytes', autospec=True,
                                                  side_effect=self.read_bytes))
        self.output = self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))

    def inspect(self, path, directory=False):
        return self.files.get(path, (None, None))[0]

    def read_bytes(self, path):
        if path in self.files:
            return self.files[path][1]
        with path.open('rb') as source:
            return source.read()

    def install(self, path, owner, group, mode, content):
        info = SimpleNamespace(st_uid=self.users[owner].pw_uid,
                               st_gid=self.groups[group].gr_gid, st_mode=mode)
        self.files[path] = info, content

    def command(self, args):
        self.commands.append(args)
        if args[0] == 'groupadd':
            self.groups[args[1]] = SimpleNamespace(gr_gid=2000 + len(self.groups), gr_mem=[])
        elif args[0] == 'useradd':
            name = args[-1]
            self.users[name] = SimpleNamespace(pw_uid=2000 + len(self.users),
                                               pw_gid=self.groups[name].gr_gid)
        elif args[0] == 'usermod':
            self.groups[args[-2]].gr_mem.append(args[-1])
        return SimpleNamespace(stdout='no\n')

    def plan(self, container=False, agents=('claude', 'codex', 'gemini')):
        return installer.build_plan('dd', SCRIPT.parent.parent, agents, container)

    def test_fresh_host_and_second_run(self):
        plan = self.plan()
        self.assertFalse(self.commands)
        self.assertFalse(self.files)
        self.assertEqual(plan.apply(), 0)
        self.assertEqual(set(self.users), {'root', 'dd', 'dd-agent', 'dd-codex', 'dd-gemini'})
        self.assertTrue(installer.member('dd', 'ddwork'))
        for name in installer.AGENTS.values():
            self.assertTrue(installer.member('dd', name))
            self.assertTrue(installer.member(name, 'ddwork'))
        for name in ('repos', 'worktrees'):
            info, _ = self.files[Path('/srv/dynamic-dispatch') / name]
            self.assertEqual(info.st_mode, 0o2750)
            self.assertEqual(info.st_uid, 1000)
        sudoers = self.files[Path('/etc/sudoers.d/dynamic-dispatch')][1]
        self.assertIn(b'dd ALL=(dd-agent, dd-codex, dd-gemini)', sudoers)
        self.assertEqual(self.files[Path('/etc/sudoers.d/dynamic-dispatch')][0].st_mode, 0o440)
        service = self.files[Path('/etc/systemd/system/dynamic-dispatch.service')][1]
        self.assertIn(b'User=dd\n', service)
        self.assertNotIn(b'DD_CHECKOUT', service)
        self.assertIn(['systemctl', 'daemon-reload'], self.commands)
        self.assertFalse(any('start' in c or 'enable' in c for c in self.commands))
        again = self.plan()
        self.assertEqual(again.steps, [])
        again.show()
        self.assertIn('Nothing to do.', self.output.getvalue())

    def test_container_skips_only_systemd(self):
        self.assertEqual(self.plan(container=True, agents=('gemini',)).apply(), 0)
        self.assertFalse(any(c[0] == 'systemctl' for c in self.commands))
        self.assertFalse(any('/systemd/' in str(p) for p in self.files))
        self.assertIn(Path('/usr/local/bin/dd-rollback'), self.files)
        self.assertIn(b'ALL=(dd-gemini)', self.files[Path('/etc/sudoers.d/dynamic-dispatch')][1])
        self.assertNotIn('dd-agent', self.users)
        self.assertEqual(self.plan(container=True, agents=('gemini',)).steps, [])

    def test_stop_on_failure_and_resume(self):
        plan = self.plan(container=True)
        real = self.command
        def fail(args):
            if args[0] == 'useradd':
                raise RuntimeError('fake failure')
            return real(args)
        with mock.patch.object(installer, 'command', side_effect=fail):
            plan = self.plan(container=True)
            with contextlib.redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(plan.apply(), 1)
        self.assertIn('FAILED: Create account', errors.getvalue())
        self.assertIn('Completed: Create group ddwork', errors.getvalue())
        self.assertFalse(self.files)
        self.assertEqual(self.plan(container=True).apply(), 0)
        self.assertEqual(self.plan(container=True).steps, [])

    def test_dry_run_decline_and_container_confirmation(self):
        for flags in (['--dry-run'], [], ['--container']):
            with self.subTest(flags=flags), mock.patch.object(installer, 'preflight',
                    return_value=SCRIPT.parent.parent), mock.patch('builtins.input', return_value='no') as ask:
                self.assertEqual(installer.main(['--dd-user', 'dd', *flags]), 0)
                self.assertEqual(ask.call_count, int(not flags))
                if '--container' not in flags:
                    self.assertFalse(self.files)
                    self.assertFalse(self.commands)
        self.assertTrue(self.files)

    def test_preflight_refusals_and_missing_tool_links(self):
        args = argparse.Namespace(dd_user='dd', container=False, agents=list(installer.AGENTS))
        with mock.patch.object(installer.os, 'geteuid', return_value=1000):
            with self.assertRaisesRegex(ValueError, 'sudo'):
                installer.preflight(args)
        with mock.patch.object(installer.os, 'geteuid', return_value=0), \
                mock.patch.object(Path, 'is_dir', return_value=False):
            with self.assertRaisesRegex(ValueError, 'systemd'):
                installer.preflight(args)
            args.container = True
            with mock.patch.object(installer.shutil, 'which', side_effect=lambda name, **kw:
                                   None if name in installer.AGENTS else '/usr/bin/' + name):
                installer.preflight(args)
                for url in installer.INSTALL_DOCS.values():
                    self.assertIn(url, self.output.getvalue())
            for name in ('root', 'missing', 'dd\nother', 'dd-agent'):
                args.dd_user = name
                with self.assertRaises(ValueError):
                    installer.preflight(args)
        self.assertFalse(self.files)
        self.assertFalse(self.commands)

    def test_unsafe_identity_reuse_refused_before_changes(self):
        for uid in (0, 1000):
            self.users['dd-agent'] = SimpleNamespace(pw_uid=uid, pw_gid=2000)
            with self.assertRaisesRegex(ValueError, 'distinct non-root UID'):
                self.plan()
        del self.users['dd-agent']
        self.groups['ddwork'] = SimpleNamespace(gr_gid=0, gr_mem=[])
        with self.assertRaisesRegex(ValueError, 'GID 0'):
            self.plan()
        self.assertFalse(self.commands)
        self.assertFalse(self.files)

    def test_repair_is_nonrecursive_and_preserves_unrelated_files(self):
        self.plan(container=True).apply()
        target = Path('/srv/dynamic-dispatch/worktrees')
        self.files[target][0].st_mode = 0o2777
        untouched = Path('/etc/dynamic-dispatch/claude.env')
        self.files[untouched] = SimpleNamespace(st_mode=0o640), b'fake-secret'
        plan = self.plan(container=True)
        self.assertEqual(len(plan.steps), 1)
        self.assertEqual(plan.apply(), 0)
        self.assertEqual(self.files[target][0].st_mode, 0o2750)
        self.assertEqual(self.files[untouched][1], b'fake-secret')

    def test_reload_retried_after_units_were_installed(self):
        self.plan().apply()
        with mock.patch.object(installer, 'command', return_value=SimpleNamespace(stdout='yes\n')):
            plan = self.plan()
        self.assertEqual(len(plan.steps), 1)
        self.assertIn('Reload systemd', plan.steps[0][0])


class FileSafety(unittest.TestCase):
    def test_platform_and_python_refused_before_importing_host_modules(self):
        for setup, message in [("sys.platform = 'darwin'", 'Linux'),
                               ('sys.version_info = (3, 10)', 'Python 3.11')]:
            result = subprocess.run([sys.executable, '-c',
                                     f'import sys, runpy; {setup}; runpy.run_path({str(SCRIPT)!r})'],
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(message, result.stderr)

    def test_symlink_and_hardlink_targets_refused(self):
        # Ancestor checks are separate; temporary folders normally live under /tmp.
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'target'
            path.symlink_to('missing')
            with mock.patch.object(Path, 'parents', new=()):
                with self.assertRaisesRegex(ValueError, 'Unsafe'):
                    installer.inspect_path(path)
                path.unlink()
                path.write_text('original')
                (Path(folder) / 'alias').hardlink_to(path)
                with self.assertRaisesRegex(ValueError, 'Unsafe'):
                    installer.inspect_path(path)

    def test_sudoers_validated_before_replace(self):
        target = Path('/etc/sudoers.d/dynamic-dispatch')
        with tempfile.TemporaryDirectory() as folder:
            factory = tempfile.NamedTemporaryFile
            def temporary(**kwargs):
                kwargs['dir'] = folder
                return factory(**kwargs)
            with mock.patch.object(installer, 'inspect_path'), \
                    mock.patch.object(installer, 'user', return_value=SimpleNamespace(pw_uid=0)), \
                    mock.patch.object(installer, 'group', return_value=SimpleNamespace(gr_gid=0)), \
                    mock.patch.object(installer.os, 'fchown'), \
                    mock.patch.object(installer.tempfile, 'NamedTemporaryFile', side_effect=temporary), \
                    mock.patch.object(installer.os, 'replace') as replace, \
                    mock.patch.object(installer, 'command', side_effect=RuntimeError('invalid')) as command:
                with self.assertRaisesRegex(RuntimeError, 'invalid'):
                    installer.install_path(target, 'root', 'root', 0o440, b'invalid')
                self.assertEqual(command.call_args.args[0][:2], ['visudo', '-cf'])
                self.assertTrue(Path(command.call_args.args[0][2]).name.startswith('.'))
                replace.assert_not_called()
                command.side_effect = None
                installer.install_path(target, 'root', 'root', 0o440, b'valid')
                replace.assert_called_once()
                self.assertEqual(replace.call_args.args[1], target)
            self.assertEqual(list(Path(folder).iterdir()), [])

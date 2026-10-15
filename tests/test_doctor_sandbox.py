"""Sandbox checks use fake metadata and never open files or run an agent."""
import os
import stat
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from dispatch import doctor_sandbox as sandbox


def info(mode=0o755, uid=100, gid=100, directory=True):
    return SimpleNamespace(st_mode=(stat.S_IFDIR if directory else stat.S_IFREG) | mode,
                           st_uid=uid, st_gid=gid)


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.account = SimpleNamespace(pw_uid=200, pw_gid=200)
        self.cfg = {'github': {'app_id': 1, 'private_key_path': '/key'},
                    'agents': {'agent': {}},
                    'projects': {'project': {'agents': ['agent'], 'worktree_root': '/work'}}}
        self.files = {'/': info(), '/config': info(0o600, directory=False),
                      '/key': info(0o640, directory=False), '/checkout': info(),
                      '/checkout/code': info(0o644, directory=False), '/work/dd-1': info(0o2770, gid=200)}
        self.enterContext(mock.patch.object(os, 'lstat', side_effect=lambda p: self.files[str(p)]))
        self.attrs = self.enterContext(mock.patch.object(os, 'listxattr', return_value=[]))
        self.enterContext(mock.patch.object(Path, 'iterdir', side_effect=None,
                                          autospec=True, wraps=None)).side_effect = lambda p: iter(
            [Path('/checkout/code')] if str(p) == '/checkout' else [Path('/work/dd-1')])
        self.enterContext(mock.patch.object(sandbox.pwd, 'getpwnam', return_value=self.account))
        self.memberships = self.enterContext(mock.patch.object(os, 'getgrouplist', return_value=[200]))
        self.group = self.enterContext(mock.patch.object(sandbox.grp, 'getgrgid', return_value=SimpleNamespace(gr_name='agents')))
        self.enterContext(mock.patch.object(sandbox.config, 'agent_group', return_value=200))
        self.opened = self.enterContext(mock.patch('builtins.open', side_effect=AssertionError('secret read')))

    def run_checks(self, login='agent'):
        results = {}
        sandbox.check(self.cfg, {'agent': login}, '/checkout', '/config',
                      lambda status, label, reason: results.update({label: (status, reason)}))
        self.opened.assert_not_called()
        return results

    def test_ok(self):
        self.assertTrue(all(status == 'OK' for status, reason in self.run_checks().values()))

    def test_fail_permissions(self):
        for path, label in [('/config', 'config isolation'), ('/key', 'private key isolation'),
                            ('/checkout/code', 'checkout isolation'), ('/checkout', 'checkout isolation')]:
            with self.subTest(path=path):
                original = self.files[path]
                self.files[path] = info(0o777, directory=path == '/checkout')
                self.assertEqual(self.run_checks()['Agent 1 ' + label][0], 'FAIL')
                self.files[path] = original

    def test_owner_and_group_precedence(self):
        for mode, uid, gid, groups, expected in [(0o604, 200, 100, {200}, True),
                                                (0o004, 200, 100, {200}, False),
                                                (0o040, 100, 300, {200, 300}, True),
                                                (0o004, 100, 200, {200}, False)]:
            self.files['/config'] = info(mode, uid, gid, False)
            self.assertEqual(sandbox._access('/config', self.account, groups, 4), expected)

    def test_parent_blocks_read(self):
        self.files['/'] = info(0o700)
        self.files['/config'] = info(0o644, directory=False)
        self.assertEqual(self.run_checks()['Agent 1 config isolation'][0], 'OK')

    def test_admin_primary_supplementary_and_root(self):
        for name in ['sudo', 'admin', 'wheel']:
            self.group.return_value.gr_name = name
            self.assertEqual(self.run_checks()['Agent 1 administrator groups'][0], 'FAIL')
        self.group.return_value.gr_name = 'agents'
        self.account.pw_uid = 0
        self.assertEqual(self.run_checks()['Agent 1 administrator groups'][0], 'FAIL')
        self.assertEqual(self.run_checks()['Agent 1 config isolation'][0], 'FAIL')

    def test_uncertain_metadata_warns(self):
        for failure in [PermissionError('private'), ValueError('private')]:
            self.attrs.side_effect = failure
            results = self.run_checks()
            for label in ['config isolation', 'private key isolation', 'checkout isolation']:
                self.assertEqual(results['Agent 1 ' + label][0], 'WARN')
            self.assertEqual(results['Story folders 1'][0], 'WARN')
        self.group.side_effect = KeyError('private')
        self.assertEqual(self.run_checks()['Agent 1 administrator groups'][0], 'WARN')

    def test_acl_and_symlink_warn(self):
        self.attrs.return_value = ['system.posix_acl_access']
        self.assertEqual(self.run_checks()['Agent 1 config isolation'][0], 'WARN')
        self.attrs.return_value = []
        self.files['/config'].st_mode = stat.S_IFLNK | 0o777
        self.assertEqual(self.run_checks()['Agent 1 config isolation'][0], 'WARN')

    def test_group_lookup_warns_and_account_failure_skips(self):
        self.memberships.side_effect = OSError('private')
        results = self.run_checks()
        for label in results:
            if label.startswith('Agent'):
                self.assertEqual(results[label][0], 'WARN')
        self.assertTrue(all(status == 'SKIP' for status, reason in self.run_checks(None).values()))

    def test_story_mismatch_warns(self):
        for mode, gid in [(0o770, 200), (0o2770, 300)]:
            self.files['/work/dd-1'] = info(mode, gid=gid)
            self.assertEqual(self.run_checks()['Story folders 1'][0], 'WARN')

    def test_all_accounts_and_project_dependency(self):
        self.cfg['agents']['second'] = {}
        results = []
        sandbox.check(self.cfg, {'agent': 'agent', 'second': None}, '/checkout', '/config',
                      lambda status, label, reason: results.append((status, label)))
        self.assertIn(('OK', 'Agent 1 config isolation'), results)
        self.assertIn(('SKIP', 'Agent 2 config isolation'), results)
        self.assertIn(('OK', 'Story folders 1'), results)

    def test_owner_can_restore_permissions(self):
        for path, label in [('/config', 'config isolation'), ('/checkout/code', 'checkout isolation')]:
            self.files[path] = info(0o000, uid=200, directory=False)
            self.assertEqual(self.run_checks()['Agent 1 ' + label][0], 'FAIL')

    def test_multiple_groups_need_assignment_verification(self):
        self.cfg['projects']['project']['agents'].append('second')
        self.cfg['agents']['second'] = {}
        with mock.patch.object(sandbox.config, 'agent_group', side_effect=[200, 300]):
            status, reason = sandbox._folders(self.cfg, self.cfg['projects']['project'], ['agent', 'second'])
        self.assertEqual(status, 'WARN')
        self.assertIn('assigned', reason)

    def test_no_app_or_folders(self):
        self.cfg['github'] = {'token': 'fake'}
        with mock.patch.object(Path, 'iterdir', return_value=iter([])):
            results = self.run_checks()
        self.assertEqual(results['Agent 1 private key isolation'][0], 'OK')
        self.assertEqual(results['Story folders 1'][0], 'OK')

    def test_mixed_worktree_area_checks_only_story_names(self):
        for prefix in ('dd', 'plan', 'custom.prefix'):
            with self.subTest(prefix=prefix):
                self.cfg['projects']['project']['branch_prefix'] = prefix
                story = Path('/work') / f'{prefix}-12'
                folders = [story, Path('/work/main-check'),
                           Path('/work/plan-1-28e174396028f226'), Path('/work/unrelated')]
                self.files[str(story)] = info(0o2770, gid=200)
                with mock.patch.object(Path, 'iterdir', return_value=iter(folders)):
                    self.assertEqual(sandbox._folders(self.cfg, self.cfg['projects']['project'], ['agent'])[0], 'OK')
                self.files[str(story)] = info(0o750, gid=200)
                with mock.patch.object(Path, 'iterdir', return_value=iter(folders)):
                    self.assertEqual(sandbox._folders(self.cfg, self.cfg['projects']['project'], ['agent'])[0], 'WARN')

    def test_working_folders_alone_do_not_trigger_multiple_group_warning(self):
        with mock.patch.object(Path, 'iterdir', return_value=iter([Path('/work/main-check'),
                Path('/work/plan-2-28e174396028f226')])), \
                mock.patch.object(sandbox.config, 'agent_group', side_effect=[200, 300]):
            self.assertEqual(sandbox._folders(self.cfg, self.cfg['projects']['project'], ['a', 'b'])[0], 'OK')


class Container(unittest.TestCase):
    def setUp(self):
        self.dd = SimpleNamespace(pw_uid=1001, pw_gid=1001)
        self.gemini = SimpleNamespace(pw_uid=1002, pw_gid=1002)
        self.files = {'/': info(), '/home': info(), '/home/dd': info(0o700, uid=1001, gid=1001),
                      '/home/dd-gemini': info(0o700, uid=1002, gid=1002),
                      '/home/dd-gemini/gemini-credential.json': info(0o400, uid=1002, gid=1002, directory=False)}

        def lstat(path):
            if self.files.get(str(path)) is None:
                raise FileNotFoundError('fake-private-missing')
            if isinstance(self.files[str(path)], OSError):
                raise self.files[str(path)]
            return self.files[str(path)]

        self.enterContext(mock.patch.object(os, 'lstat', side_effect=lstat))
        self.attrs = self.enterContext(mock.patch.object(os, 'listxattr', return_value=[]))
        self.enterContext(mock.patch.object(sandbox.pwd, 'getpwnam',
                                            side_effect=lambda name: {'dd': self.dd, 'dd-gemini': self.gemini}[name]))
        self.enterContext(mock.patch.object(os, 'getgrouplist', side_effect=lambda name, gid: [gid]))
        self.opened = self.enterContext(mock.patch('builtins.open', side_effect=AssertionError('secret read')))

    def run_checks(self):
        results = {}
        sandbox.check_container([('Volume DD home', '/home/dd', 'dd'), ('Volume Gemini home', '/home/dd-gemini', 'dd-gemini')],
                                [('Gemini credential', '/home/dd-gemini/gemini-credential.json', 'dd-gemini')],
                                lambda status, label, reason: results.update({label: (status, reason)}))
        self.opened.assert_not_called()
        for status, reason in results.values():
            self.assertNotIn('/', reason.replace('deploy/', ''))
        return results

    def test_ok(self):
        self.assertEqual({label: status for label, (status, reason) in self.run_checks().items()},
                         {'Volume DD home': 'OK', 'Volume Gemini home': 'OK', 'Gemini credential': 'OK'})

    def test_volume_missing_or_not_writable_fails(self):
        for change in [None, info(0o500, uid=1001, gid=1001), info(0o777, uid=0, directory=False),
                       info(0o700, uid=1002, gid=1002)]:
            with self.subTest(change=change):
                self.files['/home/dd'] = change
                self.assertEqual(self.run_checks()['Volume DD home'][0], 'FAIL')

    def test_group_write_is_enough(self):
        self.files['/home/dd'] = info(0o770, uid=0, gid=1001)
        self.assertEqual(self.run_checks()['Volume DD home'][0], 'OK')

    def test_uncertain_metadata_warns(self):
        self.attrs.return_value = ['system.posix_acl_access']
        results = self.run_checks()
        self.assertTrue(all(status == 'WARN' for status, reason in results.values()))

    def test_no_credential_mounted(self):
        self.files['/home/dd-gemini/gemini-credential.json'] = None
        self.assertEqual(self.run_checks()['Gemini credential'][0], 'OK')

    def test_credential_readable_by_others_fails(self):
        for change in [info(0o440, uid=1002, directory=False), info(0o404, uid=1002, directory=False),
                       info(0o400, uid=1001, directory=False), info(0o700, uid=1002)]:
            with self.subTest(change=change):
                self.files['/home/dd-gemini/gemini-credential.json'] = change
                self.assertEqual(self.run_checks()['Gemini credential'][0], 'FAIL')

    def test_private_home_hides_credential_from_dd(self):
        self.files['/home/dd-gemini/gemini-credential.json'] = PermissionError('fake-private')
        self.assertEqual(self.run_checks()['Gemini credential'][0], 'OK')
        self.files['/home/dd-gemini'] = info(0o750, uid=1002, gid=1002)
        self.assertEqual(self.run_checks()['Gemini credential'][0], 'WARN')

    def test_symlinked_credential_warns(self):
        self.files['/home/dd-gemini/gemini-credential.json'] = SimpleNamespace(
            st_mode=stat.S_IFLNK | 0o777, st_uid=1002, st_gid=1002)
        self.assertEqual(self.run_checks()['Gemini credential'][0], 'WARN')

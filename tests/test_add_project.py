"""Project setup uses fake services and temporary private configuration."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from dispatch import add_project, config, gitops, plane


class FakePlane:
    def __init__(self):
        self.project_list = []
        self.columns = []
        self.tags = []
        self.people = []
        self.writes = []
        self.fail_label = False

    def projects(self):
        return self.project_list

    def states(self, project):
        return self.columns

    def labels(self, project):
        return self.tags

    def members(self, project):
        return self.people

    def current_user(self):
        return 'orchestrator-id'

    def add_member(self, project, user, role):
        self.writes.append(('member', user))
        self.people.append({'id': user})

    def post(self, path, body):
        if 'labels' in path and self.fail_label:
            self.fail_label = False
            raise plane.PlaneError('fake-secret-plane')
        self.writes.append((path, body))
        row = dict(body, id=str(len(self.writes)))
        target = self.columns if 'states' in path else self.tags if 'labels' in path else self.project_list
        target.append(row)
        return row


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'config.json'
        self.cfg = {
            'plane': {'base_url': 'https://plane.example.com', 'workspace_slug': 'test',
                      'orchestrator_token': 'fake-secret-plane', 'approver_user_id': 'approver-id'},
            'github': {'token': 'fake-secret-github'},
            'sandbox': {'user': 'fake-agent', 'launcher': '/fake/launcher'},
            'agents': {'one': {'display_name': 'Agent', 'plane_token': 'fake-secret-agent',
                               'command': ['agent', '{prompt}'], 'parser': 'text',
                               'git_author': {'name': 'Agent', 'email': 'agent@example.com'}}},
            'projects': {}, 'db_path': str(Path(self.temp.name) / 'runs.db'),
            'projects_root': str(Path(self.temp.name) / 'repos'),
            'worktree_root': str(Path(self.temp.name) / 'worktrees'),
        }
        self.path.write_text(json.dumps(self.cfg))
        self.path.chmod(0o600)
        self.service = FakePlane()
        patches = {
            'plane.Plane': Mock(return_value=self.service),
            'plane.as_agent': Mock(return_value=Mock(current_user=Mock(return_value='agent-id'))),
            'github.uses_app': Mock(return_value=True),
            'github.installation_details': Mock(return_value=('fake-installation-secret', {
                'permissions': {'contents': 'read', 'pull_requests': 'write', 'statuses': 'write'}})),
            'github.repository': Mock(return_value={'default_branch': 'trunk'}),
            'gitops.check_repository_access': Mock(return_value=True), 'gitops.clone_project': Mock(),
            'doctor.run': Mock(return_value=0),
        }
        self.mocks = {}
        for name, value in patches.items():
            self.mocks[name] = self.enterContext(patch('dispatch.add_project.' + name, value))

    def run_setup(self, reuse=False, cycles=True):
        output = io.StringIO()
        answers = ['Example', 'example/project', 'python3 -m unittest', 'y' if cycles else 'n']
        if reuse:
            answers.append('y')
        with patch('builtins.input', side_effect=answers), contextlib.redirect_stdout(output):
            result = add_project.run(self.path)
        text = output.getvalue()
        for secret in ('fake-secret-plane', 'fake-secret-github', 'fake-secret-agent', 'fake-installation-secret'):
            self.assertNotIn(secret, text)
        return result, text

    def test_add_project_uses_separate_api_address(self):
        self.cfg['plane']['api_url'] = 'https://api.plane.so'
        self.cfg['plane']['base_url'] = 'https://app.plane.so'
        self.path.write_text(json.dumps(self.cfg))
        self.assertEqual(self.run_setup()[0], 0)
        self.mocks['plane.Plane'].assert_called_once_with(
            'https://api.plane.so', 'test', 'fake-secret-plane')
        self.assertEqual(config.load(self.path)['plane']['base_url'], 'https://app.plane.so')

    def test_all_steps_and_repeat(self):
        original = self.path.read_bytes()
        self.assertEqual(self.run_setup()[0], 0)
        saved = config.load(self.path)['projects']['Example']
        self.assertEqual(len(saved['states']), 7)
        self.assertEqual(saved['base_branch'], 'trunk')
        self.assertEqual(saved['verify_command'], ['python3', '-m', 'unittest'])
        self.assertEqual(saved['cycles']['carried_over_label_id'], saved['carried_over_label_id'])
        self.assertEqual({m['id'] for m in self.service.people}, {'orchestrator-id', 'agent-id', 'approver-id'})
        backup, = self.path.parent.glob('config.json.backup-*')
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        saved_bytes = self.path.read_bytes()
        writes = len(self.service.writes)
        result, text = self.run_setup(reuse=True)
        self.assertEqual(result, 0)
        self.assertIn('already saved', text)
        self.assertEqual(len(self.service.writes), writes)
        self.assertEqual(self.path.read_bytes(), saved_bytes)
        self.assertEqual(len(list(self.path.parent.glob('config.json.backup-*'))), 1)
        self.mocks['doctor.run'].assert_called_with(self.path, project='Example')

    def test_saves_only_what_the_file_held(self):
        project = {'plane_project_id': 'old', 'plane_identifier': 'EX', 'github_repo': 'example/project',
                   'repo_path': '/fake/repos/example', 'worktree_root': '/fake/worktrees',
                   'states': {'in_progress': 'a', 'in_review': 'b', 'blocked': 'c'}, 'agents': ['one'],
                   'cycles': {'product_backlog_state': 'a', 'sprint_backlog_state': 'b',
                              'carried_over_label_id': 'c'}}
        self.cfg['projects']['Example'] = project
        self.path.write_text(json.dumps(self.cfg))
        self.service.project_list.append({'id': 'old', 'name': 'Example', 'identifier': 'EX'})
        self.assertEqual(self.run_setup(reuse=True)[0], 0)
        saved = json.loads(self.path.read_text())['projects']['Example']
        self.assertNotIn('max_stories', saved['cycles'])
        self.assertEqual(saved['repo_path'], '/fake/repos/example')
        self.assertEqual(saved['plane_identifier'], 'EX')

    def test_identifier_is_short_and_unused(self):
        self.service.project_list.append({'id': 'other', 'name': 'Another', 'identifier': 'EXAMP'})
        self.assertEqual(self.run_setup()[0], 0)
        created = [body for path, body in self.service.writes if path == '/projects/']
        self.assertEqual(created, [{'name': 'Example', 'identifier': 'EXAM2'}])

    def test_empty_repository_stops_before_plane(self):
        self.mocks['gitops.check_repository_access'].return_value = False
        result, text = self.run_setup()
        self.assertEqual(result, 1)
        self.assertIn('empty', text)
        self.assertEqual(self.service.writes, [])

    def test_resume_partial_failure(self):
        original = self.path.read_bytes()
        self.service.fail_label = True
        self.assertEqual(self.run_setup()[0], 1)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(len(self.service.columns), 7)
        self.assertEqual(self.run_setup(reuse=True)[0], 0)
        self.assertEqual(len(self.service.columns), 7)
        self.assertEqual(len(self.service.project_list), 1)

    def test_existing_project_requires_consent(self):
        self.service.project_list.append({'id': 'existing', 'name': 'Example', 'identifier': 'EX'})
        with patch('builtins.input', side_effect=['Example', 'example/project', 'tests', 'n', 'n']), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(add_project.run(self.path), 1)
        self.assertEqual(self.service.writes, [])
        self.assertEqual(self.run_setup(reuse=True, cycles=False)[0], 0)
        self.assertNotIn('cycles', config.load(self.path)['projects']['Example'])

    def test_missing_github_steps_stop_before_plane(self):
        for name, failure in [('github.repository', add_project.github.GitHubError('fake-secret-github')),
                              ('gitops.check_repository_access', gitops.GitError('fake-secret-github'))]:
            with self.subTest(name=name):
                self.mocks[name].side_effect = failure
                self.assertEqual(self.run_setup()[0], 1)
                self.assertEqual(self.service.writes, [])
                self.mocks[name].side_effect = None
        self.mocks['github.uses_app'].return_value = False
        self.assertEqual(self.run_setup()[0], 1)
        self.assertEqual(self.service.writes, [])

    def test_doctor_failure_is_actionable_and_saved(self):
        self.mocks['doctor.run'].return_value = 1
        result, text = self.run_setup()
        self.assertEqual(result, 1)
        self.assertIn('FAIL and SKIP', text)
        self.assertIn('Example', config.load(self.path)['projects'])

    def test_clone_failure_can_resume(self):
        self.mocks['gitops.clone_project'].side_effect = gitops.GitError('fake-secret-github')
        self.assertEqual(self.run_setup()[0], 1)
        writes = len(self.service.writes)
        self.mocks['gitops.clone_project'].side_effect = None
        self.assertEqual(self.run_setup(reuse=True)[0], 0)
        self.assertEqual(len(self.service.writes), writes)

    def test_invalid_candidate_and_concurrent_edit_keep_config(self):
        original = self.path.read_bytes()
        with self.assertRaises(config.ConfigError):
            config.save_project(self.path, 'invalid', {}, original)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.glob('config.json.backup-*')), [])
        self.path.write_bytes(original + b'\n')
        with self.assertRaises(config.ConfigError):
            config.save_project(self.path, 'invalid', {}, original)
        self.assertEqual(self.path.read_bytes(), original + b'\n')

    def test_config_error_never_prints_secret(self):
        self.path.write_text('{fake-secret-plane')
        self.assertEqual(self.run_setup()[0], 1)

    def test_each_plane_step_recovers_after_lost_response(self):
        for suffix in ('/projects/', '/states/', '/labels/'):
            with self.subTest(step=suffix):
                self.service = FakePlane()
                self.mocks['plane.Plane'].return_value = self.service
                original = json.dumps(self.cfg).encode()
                self.path.write_bytes(original)
                post = self.service.post
                failed = False
                def lose_response(path, body):
                    nonlocal failed
                    result = post(path, body)
                    if path.endswith(suffix) and not failed:
                        failed = True
                        raise plane.PlaneError('fake-secret-plane')
                    return result
                self.service.post = lose_response
                self.assertEqual(self.run_setup()[0], 1)
                self.assertEqual(self.path.read_bytes(), original)
                self.assertEqual(self.run_setup(reuse=True)[0], 0)
                self.assertEqual(len(self.service.project_list), 1)
                self.assertEqual(len(self.service.columns), 7)
                self.assertEqual(len(self.service.tags), 3)

    def test_membership_failure_recovers_without_duplicates(self):
        add = self.service.add_member
        failed = False
        def lose_response(project, user, role):
            nonlocal failed
            add(project, user, role)
            if not failed:
                failed = True
                raise plane.PlaneError('fake-secret-agent')
        self.service.add_member = lose_response
        self.assertEqual(self.run_setup()[0], 1)
        self.assertEqual(self.run_setup(reuse=True)[0], 0)
        self.assertEqual(len(self.service.people), 3)

    def test_save_failure_leaves_original_and_resumes(self):
        original = self.path.read_bytes()
        with patch('dispatch.config.os.replace', side_effect=OSError('fake-secret-plane')):
            self.assertEqual(self.run_setup()[0], 1)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.run_setup(reuse=True)[0], 0)

    def test_missing_cycles_approver_and_wrong_state_group(self):
        self.cfg['plane'].pop('approver_user_id')
        self.path.write_text(json.dumps(self.cfg))
        self.assertEqual(self.run_setup()[0], 1)
        self.assertEqual(self.service.writes, [])
        self.service.columns.append({'id': 'wrong', 'name': 'Done', 'group': 'started'})
        self.assertEqual(self.run_setup(cycles=False)[0], 1)
        self.mocks['gitops.clone_project'].assert_not_called()


class PlaneSetupTests(unittest.TestCase):
    def test_projects_paginate_and_members_use_v1_payload(self):
        client = plane.Plane('https://plane.example.com', 'workspace', 'fake-plane-secret')
        with patch.object(client, 'get', side_effect=[
                {'results': [{'id': 'one'}], 'next_cursor': 'next', 'next_page_results': True},
                {'results': [{'id': 'two'}], 'next_page_results': False}]):
            self.assertEqual(client.projects(), [{'id': 'one'}, {'id': 'two'}])
        with patch.object(client, 'post') as post:
            client.add_member('project', 'account', 15)
            post.assert_called_once_with('/projects/project/members/', {'member': 'account', 'role': 15})


class CloneTests(unittest.TestCase):
    def test_clone_failure_cleanup_and_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'project'
            url = 'https://github.com/example/project.git'
            def fake_git(args, cwd, **kwargs):
                if args == gitops.FILTER_QUERY:
                    return Mock(returncode=1, stdout='', timed_out=False)
                if args[0] == 'clone':
                    (Path(args[-1]) / '.git').mkdir(parents=True)
                return Mock(stdout=url)
            with patch.object(gitops, '_git', side_effect=fake_git) as command, \
                    patch.object(gitops, 'check_origin'):
                gitops.clone_project(target, url)
                gitops.clone_project(target, url)
                self.assertEqual(sum(c.args[0][0] == 'clone' for c in command.call_args_list), 1)
                with self.assertRaises(gitops.GitError):
                    gitops.clone_project(target, url + '-other')
            other = Path(directory) / 'other'
            with patch.object(gitops, '_git', side_effect=gitops.GitError('fake failure')):
                with self.assertRaises(gitops.GitError):
                    gitops.clone_project(other, url)
            self.assertFalse(other.exists())
            self.assertEqual(list(Path(directory).glob('.dd-clone-*')), [])

    def test_new_clone_is_read_only_to_its_folder_group(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'project'
            def fake_git(args, cwd, **kwargs):
                if args == gitops.FILTER_QUERY:
                    return Mock(returncode=1, stdout='', timed_out=False)
                clone = Path(args[-1])
                (clone / '.git').mkdir(parents=True)
                (clone / 'tracked').write_text('x')
                (clone / 'tracked').chmod(0o666)
                (clone / 'link').symlink_to('/nonexistent')
                return Mock(stdout='')
            with patch.object(gitops, '_git', side_effect=fake_git):
                gitops.clone_project(target, 'https://github.com/example/project.git')
            for path in (target, target / '.git', target / 'tracked'):
                self.assertEqual(path.stat().st_gid, Path(directory).stat().st_gid)
                self.assertFalse(path.stat().st_mode & 0o022)
            self.assertTrue((target / 'link').is_symlink())

    def test_repository_access_reports_an_empty_repository(self):
        for output, expected in (('abc123\tHEAD\n', True), ('', False)):
            with patch.object(gitops, '_git', return_value=Mock(stdout=output)) as command:
                self.assertIs(gitops.check_repository_access('https://github.com/example/project.git'), expected)
            self.assertEqual(command.call_args.args[0][:2], ['ls-remote', '--'])

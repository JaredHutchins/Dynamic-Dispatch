"""Saving the Git push login and the push dry run, with temporary homes and local fake origins."""
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from dispatch import gitops, proc, push_login

TOKEN = 'github_pat_fake' + 'x' * 78


def git(*args, cwd):
    return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


class PushLoginCase(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.dict(os.environ, {
            'HOME': str(self.home), 'XDG_CONFIG_HOME': str(self.home / '.config'),
            'GIT_CONFIG_GLOBAL': str(self.home / '.gitconfig'), 'GIT_CONFIG_NOSYSTEM': '1'}))
        self.credentials = self.home / '.git-credentials'
        self.config = self.home / 'config.json'
        self.prompts = []

    def run_command(self, answers, token=TOKEN, path=None):
        answers = iter(answers)

        def ask(prompt):
            self.prompts.append(prompt)
            answer = next(answers, None)
            if answer is None:
                raise EOFError  # as input() does when nothing is typed in
            return answer

        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = push_login.run(str(path or self.config), ask=ask, secret=lambda prompt: token)
        output = out.getvalue()
        self.assertNotIn('fake' + 'x' * 10, output)
        self.assertNotIn('github_pat', output)
        return code, output

    def saved(self):
        return self.credentials.read_text().splitlines()

    def write_config(self, projects):
        self.config.write_text(json.dumps({'projects': projects}))
        self.config.chmod(0o600)

    def clone(self, name):
        origin = self.home / f'{name}.git'
        git('init', '-q', '--bare', '-b', 'main', str(origin), cwd=self.home)
        repo = self.home / name
        git('clone', '-q', str(origin), str(repo), cwd=self.home)
        git('-c', 'user.name=Test', '-c', 'user.email=t@x', 'commit', '-q', '--allow-empty', '-m', 'first', cwd=repo)
        git('push', '-q', 'origin', 'HEAD:main', cwd=repo)
        return repo, origin


class SavingTheLogin(PushLoginCase):
    def test_token_is_saved_private_with_whitespace_removed(self):
        pasted = TOKEN[:40] + '\n' + TOKEN[40:] + ' \r\n'
        code, output = self.run_command(['newuser'], token=pasted)
        self.assertEqual(code, 0)
        self.assertEqual(self.saved(), [f'https://newuser:{TOKEN}@github.com'])
        self.assertEqual(stat.S_IMODE(self.credentials.stat().st_mode), 0o600)
        self.assertIn('helper = store', (self.home / '.gitconfig').read_text())
        self.assertIn('Saved the push login for newuser', output)

    def test_existing_file_is_made_private(self):
        self.credentials.write_text('')
        self.credentials.chmod(0o644)
        self.run_command(['newuser'])
        self.assertEqual(stat.S_IMODE(self.credentials.stat().st_mode), 0o600)

    def test_same_username_is_replaced_without_asking(self):
        gitops.save_push_login('newuser', 'fake-old-token')
        code, output = self.run_command(['newuser'])
        self.assertEqual(code, 0)
        self.assertEqual(self.saved(), [f'https://newuser:{TOKEN}@github.com'])
        self.assertEqual(self.prompts, ['GitHub username: '])

    def test_other_usernames_are_offered_for_removal(self):
        gitops.save_push_login('olduser', 'fake-old-token')
        gitops.save_push_login('otheruser', 'fake-other-token')
        code, output = self.run_command(['newuser', '', 'n'])
        self.assertEqual(code, 0)
        self.assertEqual(self.prompts[1:], [
            'Remove the saved login for otheruser so Git uses the new token? [Y/n] ',
            'Remove the saved login for olduser so Git uses the new token? [Y/n] '])
        self.assertEqual(self.saved(), [f'https://newuser:{TOKEN}@github.com',
                                        'https://olduser:fake-old-token@github.com'])
        self.assertIn('Removed the saved login for otheruser.', output)
        self.assertNotIn('fake-old-token', output)

    def test_other_hosts_are_not_offered(self):
        self.credentials.write_text('https://someone:fake-other-token@example.com\n')
        self.run_command(['newuser'])
        self.assertEqual(self.prompts, ['GitHub username: '])
        self.assertIn('https://someone:fake-other-token@example.com', self.saved())

    def test_bad_answers_save_nothing(self):
        for username, token in (('', TOKEN), ('new user', TOKEN), ('newuser', ' \n')):
            with self.subTest(username=username, token=token):
                code, output = self.run_command([username], token=token)
                self.assertEqual(code, 1)
                self.assertIn('nothing was saved', output)
                self.assertFalse(self.credentials.exists())

    def test_no_input_fails_cleanly(self):
        code, output = self.run_command([])
        self.assertEqual(code, 1)
        self.assertIn('No answer was given', output)


class CheckingProjects(PushLoginCase):
    def test_works_with_no_config_or_no_projects(self):
        code, output = self.run_command(['newuser'])
        self.assertEqual(code, 0)
        self.assertIn('No projects yet; the push check runs once a project is added.', output)
        self.write_config({})
        code, output = self.run_command(['newuser'])
        self.assertEqual(code, 0)
        self.assertIn('No projects yet', output)

    def test_unreadable_config_is_reported(self):
        self.config.write_text('{')
        self.config.chmod(0o600)
        code, output = self.run_command(['newuser'])
        self.assertEqual(code, 1)
        self.assertIn('Cannot read config.json', output)
        self.assertEqual(self.saved(), [f'https://newuser:{TOKEN}@github.com'])

    def test_each_project_gets_one_line(self):
        repo, origin = self.clone('app')
        self.write_config({'app': {'github_repo': 'owner/app', 'repo_path': str(repo)},
                           'later': {'github_repo': 'owner/later', 'repo_path': str(self.home / 'missing')}})
        before = git('for-each-ref', cwd=origin)
        code, output = self.run_command(['newuser'])
        self.assertEqual(code, 0)
        self.assertIn('  owner/app: OK\n', output)
        self.assertIn('  owner/later: not downloaded yet, checked after the first start\n', output)
        self.assertEqual(git('for-each-ref', cwd=origin), before)

    def test_rejections_fail_the_command(self):
        self.write_config({'app': {'github_repo': 'owner/app', 'repo_path': '/fake-repo'}})
        for result, line, code in (
                (gitops.PUSH_INVALID, 'owner/app: rejected: the token is invalid or expired', 1),
                (gitops.PUSH_NO_WRITE, 'owner/app: rejected: the token has no write access to this repository', 1),
                (gitops.PUSH_UNREACHABLE, "owner/app: couldn't reach GitHub (not a token problem)", 0)):
            with self.subTest(result), mock.patch.object(gitops, 'check_push', return_value=result):
                status, output = self.run_command(['newuser'])
                self.assertEqual(status, code)
                self.assertIn(line, output)


class DryRun(PushLoginCase):
    def fake_git(self, returncode, stderr, timed_out=False):
        return mock.patch.object(proc, 'run_capped', return_value=proc.Result(returncode, '', stderr, timed_out, False))

    def make_clone(self):
        repo = self.home / 'repo'
        (repo / '.git').mkdir(parents=True)
        return repo

    def test_results_are_never_confused(self):
        repo = self.make_clone()
        cases = [
            (0, 'To https://github.com/owner/app.git\n * [new branch] HEAD -> dd-push-check-1', gitops.PUSH_OK),
            (128, "remote: Invalid username or token. Password authentication is not supported for Git operations.\n"
                  "fatal: Authentication failed for 'https://github.com/owner/app.git/'", gitops.PUSH_INVALID),
            (128, "fatal: could not read Username for 'https://github.com': terminal prompts disabled",
             gitops.PUSH_INVALID),
            (128, "remote: Permission to owner/app.git denied to newuser.\n"
                  "fatal: unable to access 'https://github.com/owner/app.git/': The requested URL returned error: 403",
             gitops.PUSH_NO_WRITE),
            (128, "remote: Repository not found.\nfatal: repository 'https://github.com/owner/app.git/' not found",
             gitops.PUSH_NO_WRITE),
            (128, "fatal: unable to access 'https://github.com/owner/app.git/': Could not resolve host: github.com",
             gitops.PUSH_UNREACHABLE),
            (128, "fatal: unable to access 'https://github.com/owner/app.git/': Failed to connect to github.com "
                  "port 443 after 130000 ms: Connection timed out", gitops.PUSH_UNREACHABLE),
            (128, "fatal: unable to access 'https://github.com/owner/app.git/': The requested URL returned error: 503",
             gitops.PUSH_UNREACHABLE),
            (128, "fatal: 'origin' does not appear to be a git repository", gitops.PUSH_FAILED),
        ]
        for returncode, stderr, expected in cases:
            with self.subTest(stderr), self.fake_git(returncode, stderr):
                self.assertEqual(gitops.check_push(repo), expected)
        with self.fake_git(-15, '', timed_out=True):
            self.assertEqual(gitops.check_push(repo), gitops.PUSH_UNREACHABLE)

    def test_every_result_has_its_own_line(self):
        self.assertEqual(len(set(push_login.RESULTS.values())), len(push_login.RESULTS))

    def test_dry_run_cannot_prompt_and_has_a_time_limit(self):
        repo = self.make_clone()
        with self.fake_git(0, '') as run:
            gitops.check_push(repo)
        command, kwargs = run.call_args.args[0], run.call_args.kwargs
        self.assertIn('--dry-run', command)
        self.assertIn('--no-verify', command)
        self.assertIn('core.hooksPath=/dev/null', command)
        refspec = command[-1]
        self.assertRegex(refspec, r'^HEAD:refs/heads/dd-push-check-[0-9a-f]{16}$')
        self.assertEqual(kwargs['timeout'], gitops.PUSH_CHECK_TIMEOUT)
        self.assertIsNone(kwargs['input_text'])  # stdin is /dev/null
        env = kwargs['env']
        self.assertEqual((env['GIT_TERMINAL_PROMPT'], env['GIT_ASKPASS'], env['SSH_ASKPASS']), ('0', '', ''))
        self.assertIn('BatchMode=yes', env['GIT_SSH_COMMAND'])

    def test_missing_login_fails_instead_of_prompting(self):
        # A local server that asks for a login, as GitHub does for a push.
        import http.server
        import threading

        class AskForLogin(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(401)
                self.send_header('WWW-Authenticate', 'Basic realm="fake"')
                self.send_header('Content-Length', '0')
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(('127.0.0.1', 0), AskForLogin)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        repo, _origin = self.clone('app')
        git('remote', 'set-url', 'origin', f'http://127.0.0.1:{server.server_port}/owner/app.git', cwd=repo)
        with mock.patch.dict(os.environ, {'GIT_ASKPASS': '/bin/false-fake-askpass'}):
            self.assertEqual(gitops.check_push(repo), gitops.PUSH_INVALID)

    def test_missing_or_not_a_clone(self):
        self.assertEqual(gitops.check_push(self.home / 'missing'), gitops.PUSH_NOT_DOWNLOADED)
        (self.home / 'folder').mkdir()
        self.assertEqual(gitops.check_push(self.home / 'folder'), gitops.PUSH_FAILED)


if __name__ == '__main__':
    unittest.main()

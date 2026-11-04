"""The Git push token's expiry warning, with fake GitHub replies, a fake Plane and temporary Git homes."""
import contextlib
import io
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from unittest import mock

from dispatch import alerts, github, gitops, plane, push_expiry, redact, runs
import helpers

TOKEN = 'github_pat_fakepush' + 'y' * 70
NEW_TOKEN = 'github_pat_fakenew' + 'z' * 70
EXPIRY = datetime(2026, 12, 5, 12, tzinfo=timezone.utc)
RENEWED = datetime(2027, 2, 2, 12, tzinfo=timezone.utc)
# The real lookup, kept before the tests fake it.
REAL_PUSH_LOGIN = gitops.push_login
APP = {'app_id': 1, 'installation_id': 2, 'private_key_path': '/fake/key.pem'}


class PushTokenWarning(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.object(github, '_request', helpers.real_gh_request))
        # Dates are shown in local time; these tests fix it.
        self.addCleanup(time.tzset)
        self.enterContext(mock.patch.dict(os.environ, {'TZ': 'UTC'}))
        time.tzset()
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        # An App is configured, as on the install whose token expired unnoticed.
        self.cfg = {'db_path': str(directory / 'runs.db'), 'github': dict(APP),
                    'projects': {'dd': {'plane_project_id': 'pid', 'repo_path': '/fake/dd',
                                        'states': {'product_backlog': 'backlog', 'done': 'done'},
                                        'human_only_label_id': 'human'}}}
        self.conn = runs.connect(self.cfg['db_path'])
        self.addCleanup(self.conn.close)
        self.orch = mock.Mock()
        self.orch.issues.return_value = []
        self.orch.create_issue.return_value = {'id': 'item-1'}
        self.orch.has_comment_with.return_value = False
        self.enterContext(mock.patch.object(plane, 'orchestrator', return_value=self.orch))
        self.send = self.enterContext(mock.patch.object(alerts, 'send'))
        self.logins = {'/fake/dd': TOKEN}
        self.enterContext(mock.patch.object(gitops, 'push_login', side_effect=lambda repo: self.logins.get(repo)))
        self.replies = {TOKEN: EXPIRY}
        self.asked = []
        self.enterContext(mock.patch('urllib.request.urlopen', side_effect=self.github))
        self.stderr = io.StringIO()
        self.enterContext(contextlib.redirect_stderr(self.stderr))

    def github(self, request, timeout=None):
        """GitHub's /rate_limit: the expiry header for a known token, 401 for a refused one."""
        token = request.get_header('Authorization').removeprefix('Bearer ')
        self.asked.append(token)
        reply = self.replies[token]
        if reply == push_expiry.REFUSED:
            raise helpers.http_error(self, request.full_url, 401, 'Bad credentials', {},
                                     io.BytesIO(f'{{"message": "Bad credentials {token}"}}'.encode()))
        response = mock.MagicMock()
        response.__enter__.return_value.headers = (
            {'github-authentication-token-expiration': reply.strftime('%Y-%m-%d %H:%M:%S UTC')} if reply else {})
        return response

    def check(self, at, health=None):
        (health or alerts.Health(self.cfg)).check_push_tokens(at)

    def days_before(self, expiry, days):
        return expiry - timedelta(days=days)

    def created_titles(self):
        return [c.args[1] for c in self.orch.create_issue.call_args_list]

    def bark_messages(self):
        return [c.args[3] for c in self.send.call_args_list]

    def test_a_token_without_expiry_gives_no_warning(self):
        self.replies[TOKEN] = None
        for days in range(40):
            self.check(EXPIRY - timedelta(days=days))
        self.orch.create_issue.assert_not_called()
        self.send.assert_not_called()

    def test_item_at_fourteen_days_only_once_with_the_human_label(self):
        self.check(self.days_before(EXPIRY, 15))
        self.orch.create_issue.assert_not_called()
        for days in (14, 13, 5):
            self.check(self.days_before(EXPIRY, days))
            self.check(self.days_before(EXPIRY, days))  # the same day again, and after a restart
        self.orch.create_issue.assert_called_once()
        project, name, description, state = self.orch.create_issue.call_args.args
        self.assertEqual((project, name, state), ('pid', 'Renew the GitHub push token (expires 5 Dec 2026)', 'backlog'))
        self.assertEqual(self.orch.create_issue.call_args.kwargs['labels'], ['human'])
        self.assertIn('regenerate it', description)
        self.assertIn('python3 main.py --save-push-login', description)

    def test_an_item_made_before_a_lost_reply_is_taken_up(self):
        self.orch.create_issue.side_effect = OSError('reply lost')
        self.check(self.days_before(EXPIRY, 14))
        self.orch.create_issue.side_effect = None
        made = self.orch.create_issue.call_args.args[2]
        self.orch.issues.return_value = [{'id': 'item-9', 'description_html': f'<pre>{made}</pre>'}]
        self.check(self.days_before(EXPIRY, 13))
        self.assertEqual(self.orch.create_issue.call_count, 1)
        self.assertEqual(runs.open_push_token_items(self.conn, 'dd')[0]['issue_id'], 'item-9')

    def test_a_refused_token_gets_its_own_item_and_one_alert(self):
        self.replies[TOKEN] = push_expiry.REFUSED
        for days in range(3):
            self.check(EXPIRY + timedelta(days=days))
        self.assertEqual(self.created_titles(), ["The GitHub push token isn't working"])
        self.assertIn('python3 main.py --save-push-login', self.orch.create_issue.call_args.args[2])
        self.assertEqual(len(self.bark_messages()), 1)
        self.assertIn('refuses', self.bark_messages()[0])
        self.assertEqual(self.send.call_args.kwargs['level'], 'active')

    def renew(self, expiry):
        self.check(self.days_before(EXPIRY, 10))
        self.logins['/fake/dd'] = NEW_TOKEN
        self.replies[NEW_TOKEN] = expiry
        self.check(self.days_before(EXPIRY, 9))
        self.check(self.days_before(EXPIRY, 8))

    def test_renewal_comments_and_moves_the_item_to_done(self):
        self.renew(RENEWED)
        self.orch.comment.assert_called_once()
        self.assertEqual(self.orch.comment.call_args.args[:3],
                         ('pid', 'item-1', 'The push token was renewed; it now expires 2 Feb 2027'))
        self.orch.set_state.assert_called_once_with('pid', 'item-1', 'done')
        self.assertEqual(runs.open_push_token_items(self.conn, 'dd'), [])

    def test_renewal_to_a_token_without_expiry(self):
        self.renew(None)
        self.assertEqual(self.orch.comment.call_args.args[2], 'The push token was renewed; it no longer expires')

    def test_renewal_without_a_done_state_only_comments(self):
        del self.cfg['projects']['dd']['states']['done']
        self.renew(RENEWED)
        self.orch.comment.assert_called_once()
        self.orch.set_state.assert_not_called()

    def test_a_new_token_still_close_to_expiry_gets_its_own_item(self):
        self.renew(EXPIRY + timedelta(days=3))
        self.orch.comment.assert_not_called()
        self.assertEqual(self.orch.create_issue.call_count, 2)

    def test_bark_at_thirty_seven_and_one_days(self):
        self.cfg['alerts'] = {}
        health = alerts.Health(self.cfg)
        for days in range(31, -1, -1):
            self.check(self.days_before(EXPIRY, days), health)
            alerts.Health(self.cfg).check_push_tokens(self.days_before(EXPIRY, days))
        messages = self.bark_messages()
        self.assertEqual(len(messages), 3)
        for message, days in zip(messages, (30, 7, 1)):
            self.assertIn(f'within {days} days', message)
            self.assertIn('5 Dec 2026', message)
        self.assertTrue(all(c.kwargs['level'] == 'active' for c in self.send.call_args_list))

    def test_a_token_first_seen_close_to_expiry_alerts_once(self):
        self.check(self.days_before(EXPIRY, 5))
        self.check(self.days_before(EXPIRY, 4))
        self.assertEqual(len(self.bark_messages()), 1)
        self.assertIn('within 7 days', self.bark_messages()[0])

    def test_github_is_asked_once_per_distinct_token(self):
        self.cfg['projects']['other'] = dict(self.cfg['projects']['dd'], repo_path='/fake/other',
                                             plane_project_id='other-pid')
        self.logins['/fake/other'] = TOKEN
        self.check(self.days_before(EXPIRY, 14))
        self.assertEqual(self.asked, [TOKEN])
        self.assertEqual([c.args[0] for c in self.orch.create_issue.call_args_list], ['pid', 'other-pid'])

    def test_the_app_setting_does_not_stop_the_check(self):
        for github_cfg in (dict(APP), dict(APP, token='fake-classic-credential'), {'token': 'fake-classic-credential'}):
            with self.subTest(github=github_cfg):
                self.asked.clear()
                self.cfg['github'] = github_cfg
                self.conn.execute('DELETE FROM push_token_notices')
                self.conn.commit()
                self.check(self.days_before(EXPIRY, 20))
                self.assertEqual(self.asked, [TOKEN])

    def test_no_item_without_the_backlog_state_or_human_label(self):
        self.cfg['alerts'] = {}
        for missing in ('product_backlog', 'human_only_label_id'):
            with self.subTest(missing=missing):
                self.send.reset_mock()
                self.conn.execute('DELETE FROM push_token_notices')
                self.conn.commit()
                project = self.cfg['projects']['dd'] = {
                    'plane_project_id': 'pid', 'repo_path': '/fake/dd',
                    'states': {'product_backlog': 'backlog'}, 'human_only_label_id': 'human'}
                if missing == 'product_backlog':
                    del project['states']['product_backlog']
                else:
                    del project[missing]
                self.check(self.days_before(EXPIRY, 20))
                self.check(self.days_before(EXPIRY, 14))
                self.orch.create_issue.assert_not_called()
                warning = self.conn.execute("SELECT detail FROM events WHERE type = 'warning'").fetchall()[-1][0]
                self.assertIn('Run --add-project for this project again to fix it', warning)
                self.assertIn('Run --add-project', self.stderr.getvalue())
                self.assertTrue(any('Run --add-project' in m for m in self.bark_messages()))

    def test_the_token_never_appears_in_output_logs_or_plane(self):
        self.cfg['alerts'] = {}
        redact.set_secrets([])
        self.addCleanup(redact.set_secrets, [])
        with tempfile.TemporaryDirectory() as home:
            repo = Path(home) / 'repo'
            environment = {'HOME': home, 'XDG_CONFIG_HOME': str(Path(home) / '.config'),
                           'GIT_CONFIG_GLOBAL': str(Path(home) / '.gitconfig'), 'GIT_CONFIG_NOSYSTEM': '1'}
            with mock.patch.dict(os.environ, environment):
                subprocess.run(['git', 'init', '-q', str(repo)], check=True)
                subprocess.run(['git', 'remote', 'add', 'origin', 'https://github.com/example/repo.git'],
                               cwd=repo, check=True)
                gitops.save_push_login('example-user', TOKEN)
                self.cfg['projects']['dd']['repo_path'] = str(repo)
                self.logins = {}
                with mock.patch.object(gitops, 'push_login', REAL_PUSH_LOGIN):
                    self.check(self.days_before(EXPIRY, 3))
                    self.replies[TOKEN] = push_expiry.REFUSED
                    self.check(EXPIRY + timedelta(days=1))
        self.assertEqual(self.asked, [TOKEN, TOKEN])
        self.assertEqual(len(self.created_titles()), 2)
        seen = [self.stderr.getvalue(), repr(self.orch.mock_calls), repr(self.send.mock_calls)]
        for table in ('events', 'push_token_items', 'push_token_notices', 'login_alerts'):
            seen.append(repr([tuple(row) for row in self.conn.execute(f'SELECT * FROM {table}')]))
        for text in seen:
            self.assertNotIn(TOKEN, text)
            self.assertNotIn('fakepush', text)
        self.assertEqual(redact.scrub(TOKEN), redact.REPLACEMENT)

    def test_unreadable_github_reply_warns_and_makes_no_item(self):
        self.replies[TOKEN] = 'not a date'
        with mock.patch.object(github, 'push_token_expiry', side_effect=github.GitHubError('down', status=503)):
            self.check(self.days_before(EXPIRY, 3))
        self.orch.create_issue.assert_not_called()
        self.assertIn('Could not ask GitHub', self.stderr.getvalue())


class ReadingThePushLogin(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.dict(os.environ, {
            'HOME': str(self.home), 'XDG_CONFIG_HOME': str(self.home / '.config'),
            'GIT_CONFIG_GLOBAL': str(self.home / '.gitconfig'), 'GIT_CONFIG_NOSYSTEM': '1'}))
        self.repo = self.home / 'repo'
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)

    def origin(self, url):
        subprocess.run(['git', 'remote', 'add', 'origin', url], cwd=self.repo, check=True)

    def test_the_saved_login_is_found_for_the_origin(self):
        self.origin('https://github.com/example/repo.git')
        gitops.save_push_login('example-user', TOKEN)
        self.assertEqual(gitops.push_login(self.repo), TOKEN)
        self.assertEqual(redact.scrub(TOKEN), redact.REPLACEMENT)

    def test_no_saved_login_gives_none_without_prompting(self):
        self.origin('https://github.com/example/repo.git')
        self.assertIsNone(gitops.push_login(self.repo))

    def test_ssh_origin_or_not_a_clone_gives_none(self):
        self.origin('git@github.com:example/repo.git')
        self.assertIsNone(gitops.push_login(self.repo))
        self.assertIsNone(gitops.push_login(self.home / 'missing'))


class ReadingTheExpiry(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.object(github, '_request', helpers.real_gh_request))

    def reply(self, headers):
        response = mock.MagicMock()
        response.__enter__.return_value.headers = headers
        return response

    def test_header_forms(self):
        for header, expected in (('2026-12-05 12:00:00 UTC', EXPIRY),
                                 ('2026-12-05 04:00:00 -0800', EXPIRY),
                                 ('2026-12-05T12:00:00Z', EXPIRY)):
            with self.subTest(header=header), mock.patch(
                    'urllib.request.urlopen',
                    return_value=self.reply({'github-authentication-token-expiration': header})):
                self.assertEqual(github.push_token_expiry('fake-push-credential'), expected)

    def test_no_header_means_no_expiry(self):
        with mock.patch('urllib.request.urlopen', return_value=self.reply({})):
            self.assertIsNone(github.push_token_expiry('fake-push-credential'))

    def test_refusal_is_reported_without_a_login_alert(self):
        error = helpers.http_error(self, 'https://api.github.com/rate_limit', 401, 'Bad credentials', {},
                                   io.BytesIO(b'{}'))
        with mock.patch('urllib.request.urlopen', side_effect=error), \
                mock.patch.object(github.logins, 'login') as login:
            self.assertEqual(push_expiry.expiry('fake-push-credential'), push_expiry.REFUSED)
        login.assert_not_called()

    def test_dates_are_local_and_short(self):
        self.addCleanup(time.tzset)
        for zone, expected in (('Pacific/Auckland', '6 Dec 2026'), ('UTC', '5 Dec 2026'),
                               ('America/Los_Angeles', '5 Dec 2026')):
            with self.subTest(zone=zone), mock.patch.dict(os.environ, {'TZ': zone}):
                time.tzset()
                self.assertEqual(push_expiry.day(EXPIRY), expected)


class StoreKeepsTheState(unittest.TestCase):
    def test_items_and_notices_survive_a_new_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'runs.db'
            conn = runs.connect(path)
            self.assertTrue(runs.claim_push_token_notice(conn, 'alert:x'))
            runs.save_push_token_item(conn, 'dd', 'fingerprint', '', 'item-1')
            conn.close()
            conn = runs.connect(path)
            try:
                self.assertFalse(runs.claim_push_token_notice(conn, 'alert:x'))
                self.assertEqual(runs.push_token_item(conn, 'dd', 'fingerprint', '')['issue_id'], 'item-1')
                runs.close_push_token_item(conn, 'dd', 'fingerprint', '')
                self.assertEqual(runs.open_push_token_items(conn, 'dd'), [])
            finally:
                conn.close()


if __name__ == '__main__':
    unittest.main()

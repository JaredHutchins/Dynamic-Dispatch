"""Bark transport and board integration, with fake services and credentials."""

import base64
import json
import threading
import unittest
import urllib.error
import urllib.parse
from unittest import mock

from dispatch import alerts, redact, runs, sync
import helpers
from helpers import Base, board, S_BLK, S_REV


def settings():
    return dict(bark_url='http://bark.invalid', device_keys=['fake-device-one', 'fake-device-two'],
                encryption_key='test-only-key-'.ljust(32, 'x'))


class Configuration(unittest.TestCase):
    def test_optional_and_invalid(self):
        with mock.patch.object(alerts, 'Alerts') as sender, mock.patch.object(alerts, 'warn') as warning:
            alerts.configure({})
            sender.assert_not_called()
            warning.assert_not_called()
            for key in ('short', 'é' * 32):
                cfg = {'alerts': dict(settings(), encryption_key=key)}
                alerts.configure(cfg)
                self.assertNotIn('_alerts', cfg)
            self.assertEqual(warning.call_count, 2)

    def test_non_text_blocked_sound_disables_alerts(self):
        for sound in (None, 7, ['telegraph']):
            with self.subTest(sound=sound), mock.patch.object(alerts, 'Alerts') as sender, \
                    mock.patch.object(alerts, 'warn') as warning:
                cfg = {'alerts': dict(settings(), blocked_sound=sound)}
                alerts.configure(cfg)
                self.assertNotIn('_alerts', cfg)
                sender.assert_not_called()
                self.assertIn('blocked_sound', warning.call_args.args[0])

    def test_missing_library(self):
        with mock.patch.dict('sys.modules', {'cryptography.hazmat.primitives.ciphers.aead': None}), \
                mock.patch.object(alerts, 'warn') as warning:
            cfg = {'alerts': settings()}
            alerts.configure(cfg)
            self.assertNotIn('_alerts', cfg)
            warning.assert_called_once()
            self.assertIn('python3-cryptography', warning.call_args.args[0])

    def test_short_device_keys_disable_alerts(self):
        for device in ('x', 'x' * (redact.MIN_SECRET_LENGTH - 1)):
            with self.subTest(length=len(device)), mock.patch.object(alerts, 'Alerts') as sender, \
                    mock.patch.object(alerts, 'warn') as warning:
                cfg = {'alerts': dict(settings(), device_keys=[device])}
                alerts.configure(cfg)
                self.assertNotIn('_alerts', cfg)
                sender.assert_not_called()
                warning.assert_called_once()
                self.assertIn('at least 8 characters', warning.call_args.args[0])

    def test_redaction_ignores_short_invalid_keys(self):
        cfg = dict(plane={'orchestrator_token': ''}, github={}, agents={}, alerts=settings())
        valid_secrets = [cfg['alerts']['encryption_key'], *cfg['alerts']['device_keys']]
        cfg['alerts']['device_keys'].append('x')
        self.addCleanup(redact.set_secrets, [])
        redact.configure(cfg)
        redact.add_secret('fake-renewed-github-token')
        for secret in valid_secrets:
            self.assertEqual(redact.scrub(secret), '[redacted]')
        self.assertEqual(redact.scrub('example text'), 'example text')


class Delivery(Base):
    def setUp(self):
        super().setUp()
        self.cfg['alerts'] = settings()
        self.cfg['projects']['dd']['plane_identifier'] = 'DD'
        alerts.configure(self.cfg)
        if '_alerts' not in self.cfg:
            self.skipTest('optional python3-cryptography is not installed')
        self.sender = self.cfg['_alerts']
        self.sent = []
        self.forms = []
        self.states = []
        patch = mock.patch.object(alerts.urllib.request, 'urlopen', side_effect=self.deliver)
        patch.start()
        self.addCleanup(patch.stop)

    def tearDown(self):
        if hasattr(self, 'sender'):
            self.sender.queue.join()
        super().tearDown()

    def deliver(self, request, timeout):
        self.assertEqual(timeout, 10)
        form = urllib.parse.parse_qs(request.data.decode())
        self.assertEqual(set(form), {'ciphertext', 'iv'})
        self.assertLessEqual(len(request.data), 3000)
        self.assertEqual(len(form['iv'][0]), 12)
        cipher = type(self.sender.cipher)(settings()['encryption_key'].encode('utf-8'))
        plaintext = cipher.decrypt(form['iv'][0].encode(),
                                              base64.b64decode(form['ciphertext'][0]), None)
        self.sent.append(json.loads(plaintext))
        self.forms.append(form)
        self.states.append(board.issues['i1']['state'])
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"code":200}'
        return response

    def test_system_alert_needs_no_plane(self):
        self.add_issue()
        with mock.patch.object(alerts.plane, 'orchestrator', side_effect=AssertionError('Plane unavailable')):
            alerts.system(self.cfg, 'Plane login for Codex was rejected. Noticed at Plane API GET.')
            self.sender.queue.join()
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(all(m['level'] == 'active' for m in self.sent))
        self.assertTrue(all(m['group'] == 'DD logins' for m in self.sent))

    def test_main_alert_needs_no_plane(self):
        self.add_issue()  # only so deliver() can note the story's state
        with mock.patch.object(alerts.plane, 'orchestrator', side_effect=AssertionError('Plane unavailable')):
            undelivered = mock.Mock()
            alerts.main_health(self.cfg, 'dd', 'Main is passing again', undelivered)
            self.sender.queue.join()
        undelivered.assert_not_called()
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(all(m['level'] == 'active' for m in self.sent))
        self.assertTrue(all(m['title'] == m['group'] == 'DD main' for m in self.sent))
        self.assertTrue(all(m['body'] == 'Main is passing again' for m in self.sent))

    def test_main_alert_missed_by_any_device_is_undelivered(self):
        self.add_issue()  # only so deliver() can note the story's state
        deliver = self.deliver

        def second_device_down(request, timeout):
            if 'fake-device-two' in request.full_url:
                raise urllib.error.URLError('down')
            return deliver(request, timeout)

        undelivered = mock.Mock()
        with mock.patch.object(alerts.urllib.request, 'urlopen', side_effect=second_device_down), \
                mock.patch.object(alerts, 'warn'):
            alerts.main_health(self.cfg, 'dd', 'Main is passing again', undelivered)
            self.sender.queue.join()
        self.assertEqual(len(self.sent), 1)
        undelivered.assert_called_once_with()

    def test_review_and_start(self):
        self.add_issue()
        self.cycle()
        self.sender.queue.join()
        self.assertEqual(len(self.sent), 4)
        starts = [m for m in self.sent if 'started by' in m['title']]
        reviews = [m for m in self.sent if 'ready for review' in m['title']]
        self.assertEqual(len(starts), 2)
        self.assertTrue(all(m['level'] == 'passive' for m in starts))
        self.assertEqual(len(reviews), 2)
        for message in reviews:
            self.assertEqual(message['level'], 'active')
            self.assertEqual(message['url'], 'https://gh/pr/1')
            self.assertIn('Add a value', message['body'])
        self.assertTrue(all(m['group'] == 'DD-7' for m in self.sent))
        self.assertEqual(len({form['iv'][0] for form in self.forms}), 4)
        self.assertEqual(self.states[-1], {'id': S_REV})

    def test_review_alert_opens_files_changed(self):
        with mock.patch.object(helpers, 'PR_URL', 'https://github.com/o/r/pull/{}'):
            self.add_issue()
            self.cycle()
        self.sender.queue.join()
        reviews = [m for m in self.sent if 'ready for review' in m['title']]
        self.assertEqual(len(reviews), 2)
        self.assertTrue(all(m['url'] == 'https://github.com/o/r/pull/1/files' for m in reviews))

    def test_blocked(self):
        self.add_issue()
        run_id = runs.enqueue(self.conn, project='dd', issue=board.issues['i1'], agent='claude-code', branch='dd-7')
        runs.await_blocked(self.conn, run_id, 'First reason\nSecond reason')
        sync.reconcile(self.cfg, self.conn, runs.get(self.conn, run_id))
        self.sender.queue.join()
        self.assertEqual(len(self.sent), 2)
        for message in self.sent:
            self.assertEqual(message['title'], 'DD-7 blocked')
            self.assertEqual(message['level'], 'active')
            self.assertIn('First reason', message['body'])
            self.assertNotIn('Second reason', message['body'])
            self.assertEqual(message['sound'], 'telegraph')
        self.assertEqual(self.states, [{'id': S_BLK}] * 2)
        # A Cycle story's Blocked reminders repeat this alert, timed from now.
        self.assertEqual(self.conn.execute('SELECT message FROM blocked_reminders').fetchone()[0], 'First reason')

    def blocked_sounds(self):
        self.add_issue()
        run_id = runs.enqueue(self.conn, project='dd', issue=board.issues['i1'], agent='claude-code', branch='dd-7')
        runs.await_blocked(self.conn, run_id, 'reason')
        sync.reconcile(self.cfg, self.conn, runs.get(self.conn, run_id))
        self.sender.queue.join()
        return [m.get('sound') for m in self.sent]

    def test_blocked_sound_is_a_setting(self):
        self.cfg['alerts']['blocked_sound'] = 'alarm'
        self.assertEqual(self.blocked_sounds(), ['alarm'] * 2)

    def test_empty_blocked_sound_plays_the_default(self):
        self.cfg['alerts']['blocked_sound'] = ''
        self.assertEqual(self.blocked_sounds(), [None] * 2)

    def test_other_alerts_carry_no_sound(self):
        self.add_issue()
        self.cycle()
        sync.notice(self.cfg, 'dd', 'i1', 'note', 'A notice')
        alerts.system(self.cfg, 'Plane login for Codex was rejected. Noticed at Plane API GET.')
        alerts.main_health(self.cfg, 'dd', 'Main is passing again', mock.Mock())
        self.sender.queue.join()
        self.assertEqual(len(self.sent), 10)
        self.assertTrue(all('sound' not in m for m in self.sent))

    def test_notice_once_after_plane_and_byte_trimming(self):
        self.add_issue()
        message = '😺漢字"\n' * 5000
        sync.notice(self.cfg, 'dd', 'i1', 'waiting', message)
        sync.notice(self.cfg, 'dd', 'i1', 'waiting', message)
        self.sender.queue.join()
        self.assertEqual(len(board.comments), 1)
        self.assertEqual(len(self.sent), 2)
        for payload in self.sent:
            self.assertEqual(payload['level'], 'passive')
            self.assertTrue(payload['body'].endswith('…'))
            self.assertTrue(payload['body'].startswith('Add a value'))
            self.assertGreater(len(payload['body'].encode('utf-8')), 500)

    def test_failure_does_not_hold_up_run_and_warns_for_each_device(self):
        self.add_issue()
        entered, release, timed_out = threading.Event(), threading.Event(), threading.Event()
        def failing(*args, **kwargs):
            entered.set()
            if not release.wait(10):
                timed_out.set()
            raise OSError('fake Bark failure')
        with mock.patch.object(alerts.urllib.request, 'urlopen', side_effect=failing):
            try:
                self.cycle()
                self.assertTrue(entered.is_set())
                self.assertFalse(timed_out.is_set(), "Bark held up story execution")
                self.assertEqual(self.run_rows()[0]['status'], runs.DONE)
            finally:
                release.set()
                self.sender.queue.join()
        events = runs.events(self.conn, project='dd', issue_id='i1')
        self.assertTrue(any(e['type'] == 'warning' and 'Bark' in e['detail'] for e in events))

    def test_outcome_waits_for_plane_retry(self):
        self.add_issue()
        run_id = runs.enqueue(self.conn, project='dd', issue=board.issues['i1'], agent='claude-code', branch='dd-7')
        runs.await_blocked(self.conn, run_id, 'reason')
        board.fail_patch = 1
        sync.reconcile(self.cfg, self.conn, runs.get(self.conn, run_id))
        self.sender.queue.join()
        self.assertEqual(self.sent, [])
        sync.reconcile(self.cfg, self.conn, runs.get(self.conn, run_id), retry=True)
        sync.reconcile(self.cfg, self.conn, runs.get(self.conn, run_id), retry=True)
        self.sender.queue.join()
        self.assertEqual(len(self.sent), 2)

    def test_delivery_warning_includes_only_safe_reason(self):
        self.add_issue()
        device = settings()['device_keys'][0]
        endpoint = settings()['bark_url'] + '/' + device
        for error in (urllib.error.URLError(endpoint), TimeoutError(endpoint),
                      urllib.error.HTTPError(endpoint, 503, device, {}, None)):
            with self.subTest(error=type(error).__name__), \
                    mock.patch.object(alerts.urllib.request, 'urlopen', side_effect=error), \
                    mock.patch.object(alerts, 'warn') as warning:
                alerts.send(self.cfg, 'dd', 'i1', 'Update')
                self.sender.queue.join()
                self.assertEqual(warning.call_count, 2)
                for call in warning.call_args_list:
                    self.assertIn(type(error).__name__, call.args[0])
                    self.assertNotIn(device, call.args[0])
                    self.assertNotIn(endpoint, call.args[0])
                event = runs.events(self.conn, project='dd', issue_id='i1')[-1]
                self.assertEqual(event['type'], 'warning')
                self.assertIn(type(error).__name__, event['detail'])
                self.assertNotIn(device, event['detail'])
                self.assertNotIn(endpoint, event['detail'])

    def test_repair_and_other_notices_are_silent(self):
        self.add_issue()
        row = dict(project='dd', issue_id='i1', id=1)
        sync.send_back(self.cfg, row, 1, 'Repair failing tests')
        for tag in ('waiting', 'switched', 'limited', 'restarted'):
            sync.notice(self.cfg, 'dd', 'i1', tag, tag)
        self.sender.queue.join()
        self.assertEqual(len(self.sent), 10)
        self.assertTrue(all(m['level'] == 'passive' for m in self.sent))

    def test_refresh_is_silent(self):
        self.add_issue(state=S_REV)
        run_id = runs.enqueue(self.conn, project='dd', issue=board.issues['i1'],
                              agent='claude-code', branch='dd-7', kind='refresh')
        runs.await_review(self.conn, run_id, summary='Refreshed with latest main', usage=None,
                          cost_usd=None, pr_url='https://gh/pr/1', worktree=None)
        sync.reconcile(self.cfg, self.conn, runs.get(self.conn, run_id))
        self.sender.queue.join()
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(all(m['level'] == 'passive' for m in self.sent))
        self.assertIn('Refreshed with latest main', self.sent[0]['body'])

    def test_refresh_run_does_not_send_start_alert(self):
        self.add_issue()
        self.cycle()
        self.sender.queue.join()
        self.sent.clear()
        helpers.MainChanges.advance_main(self)
        self.cycle()
        self.sender.queue.join()
        row = self.run_rows()[-1]
        self.assertEqual((row['kind'], row['status']), ('refresh', runs.DONE))
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(all('started by' not in m['title'] for m in self.sent))
        self.assertTrue(all(m['level'] == 'passive' for m in self.sent))

    def test_moved_story_does_not_get_false_blocked_alert(self):
        self.add_issue(state=S_REV)
        run_id = runs.enqueue(self.conn, project='dd', issue=board.issues['i1'],
                              agent='claude-code', branch='dd-7')
        runs.await_blocked(self.conn, run_id, 'reason')
        sync.reconcile(self.cfg, self.conn, runs.get(self.conn, run_id))
        self.sender.queue.join()
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(all(m['level'] == 'passive' for m in self.sent))

    def test_failed_notice_is_not_alerted(self):
        self.add_issue()
        board.fail_post = 1
        from dispatch import plane
        with self.assertRaises(plane.PlaneError):
            sync.notice(self.cfg, 'dd', 'i1', 'waiting', 'Waiting')
        self.sender.queue.join()
        self.assertEqual(self.sent, [])

    def test_state_already_updated_after_lost_reply_still_alerts(self):
        self.add_issue(state=S_BLK)
        run_id = runs.enqueue(self.conn, project='dd', issue=board.issues['i1'],
                              agent='claude-code', branch='dd-7')
        runs.await_blocked(self.conn, run_id, 'reason')
        sync.reconcile(self.cfg, self.conn, runs.get(self.conn, run_id), retry=True)
        self.sender.queue.join()
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(all(m['level'] == 'active' for m in self.sent))

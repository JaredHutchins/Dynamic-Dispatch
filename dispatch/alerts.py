"""Optional encrypted Bark delivery, entirely off the story execution path."""

from datetime import datetime, timezone
import base64
import json
import queue
import secrets
import string
import threading
import urllib.parse
import urllib.request

from . import config, github, logins, plane, runs
from .redact import MIN_SECRET_LENGTH, scrub, warn

MAX_PAYLOAD = 3000


def configure(cfg):
    if 'alerts' not in cfg:
        return
    try:
        settings = cfg['alerts']
        key = settings['encryption_key']
        if not isinstance(key, str) or len(key) != 32 or len(key.encode('utf-8')) != 32:
            raise ValueError('encryption_key must be 32 characters and 32 UTF-8 bytes')
        url = urllib.parse.urlsplit(settings['bark_url'])
        if url.scheme not in ('http', 'https') or not url.netloc or url.query or url.fragment:
            raise ValueError('bark_url must be an HTTP or HTTPS server URL without a query or fragment')
        devices = settings['device_keys']
        if not isinstance(devices, list) or not devices or not all(
                isinstance(k, str) and len(k) >= MIN_SECRET_LENGTH for k in devices):
            raise ValueError('device_keys must be a non-empty list of strings')
        if not isinstance(settings.get('blocked_sound', ''), str):
            raise ValueError('blocked_sound must be a Bark sound name')
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        cfg['_alerts'] = Alerts(cfg, AESGCM(key.encode('utf-8')))
    except ImportError:
        warn('Bark alerts disabled: install python3-cryptography (sudo apt install python3-cryptography).')
    except (ValueError, KeyError, TypeError):
        warn(f'Bark alerts disabled: check bark_url, device_keys (at least {MIN_SECRET_LENGTH} characters each), '
             'encryption_key (32 characters / 32 UTF-8 bytes) and blocked_sound (a Bark sound name).')


def main_health(cfg, project, message, undelivered):
    """Main's test result or ability to test changed: a project alert, with no Plane lookup.

    undelivered() runs on the delivery thread if any device didn't get the alert.
    """
    send(cfg, project, None, message, suffix=' main', level='active', undelivered=undelivered)


def send(cfg, project, issue_id, message, *, row=None, suffix=' update', level='passive', url='', sound='',
         undelivered=None):
    sender = cfg.get('_alerts')
    if sender is not None:
        sender.queue.put_nowait((project, issue_id, message, dict(row) if row is not None else None,
                                 suffix, level, url, sound, undelivered))


def system(cfg, message):
    send(cfg, None, None, message, suffix='DD login alert', level='active')


def start_health(cfg):
    try:
        health = Health(cfg)
        logins.observe(health)
        threading.Thread(target=health.work, name='login-health', daemon=True).start()
    except Exception:
        warn('Could not start login monitoring; carrying on.')


class Health:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.RLock()
        conn = runs.connect(cfg['db_path'], initialize=False)
        try:
            self.marked = runs.login_alert_keys(conn)
        finally:
            conn.close()

    def mark(self, key, present=True):
        # Routine successes must not wait behind a state change's database write.
        if (key in self.marked) == present:
            return False
        with self.lock:
            if (key in self.marked) == present:
                return False
            conn = runs.connect(self.cfg['db_path'], initialize=False)
            try:
                changed = runs.mark_login_alert(conn, key, present)
            finally:
                conn.close()
            if present:
                self.marked.add(key)
            else:
                self.marked.discard(key)
            return present and changed

    def login(self, key, name, where, rejected):
        if self.mark('login:' + key, rejected):
            system(self.cfg, f'{name} was rejected. Noticed at {where}.')

    def check_expiry(self, at=None):
        at = at or datetime.now(timezone.utc)
        if not self.mark('expiry-check:' + at.date().isoformat()):
            return
        try:
            token = self.cfg.get('github', {}).get('token')
            if not token:
                raise ValueError('No token')
            expiry = github.token_expiry(token)
            remaining = (expiry - at).total_seconds() / 86400
            for days in (30, 7, 1):
                key = f'expiry:{logins.fingerprint(token)}:{expiry.isoformat()}:{days}'
                if remaining <= days and self.mark(key):
                    system(self.cfg, f'GitHub token in config.json (github.token) expires at '
                           f'{expiry.isoformat()} (within {days} days). Noticed at GET /rate_limit.')
        except Exception:
            detail = 'Could not read GitHub token expiry at GET /rate_limit; carrying on.'
            warn(detail)
            conn = runs.connect(self.cfg['db_path'], initialize=False)
            try:
                runs.record_event(conn, None, 'warning', detail, project='')
            finally:
                conn.close()

    def work(self):
        while True:
            try:
                self.check_expiry()
            except Exception:
                warn('Could not record GitHub expiry warning; carrying on.')
            threading.Event().wait(3600)


def encrypted_form(cipher, payload):
    # Account for base64 AND form escaping, not just the plaintext or GCM tag.
    payload = {k: scrub(v) for k, v in payload.items()}
    body = payload['body'].encode('utf-8')
    while True:
        iv = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(12))
        raw = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        ciphertext = base64.b64encode(cipher.encrypt(iv.encode('utf-8'), raw, None)).decode('ascii')
        form = urllib.parse.urlencode({'ciphertext': ciphertext, 'iv': iv}).encode('ascii')
        if len(form) <= MAX_PAYLOAD:
            return form
        if not body:
            raise ValueError('Alert metadata exceeds payload limit')
        limit = max(0, min(len(body) - 32, int(len(body) * MAX_PAYLOAD / len(form)) - 32))
        body = body[:limit]
        payload['body'] = body.decode('utf-8', errors='ignore') + '…'


class Alerts:
    def __init__(self, cfg, cipher):
        self.cfg, self.cipher = cfg, cipher
        self.queue = queue.Queue()
        threading.Thread(target=self.work, name='bark-alerts', daemon=True).start()

    def warning(self, project, issue_id, row, error):
        # Exception text can contain a device URL or a server echo of the request.
        detail = f'Could not send Bark alert ({type(error).__name__}); carrying on.'
        warn(detail)
        try:
            conn = runs.connect(self.cfg['db_path'], initialize=False)
            try:
                runs.record_event(conn, row['id'] if row else None, 'warning', detail,
                                  project=project or '', issue_id=issue_id)
            finally:
                conn.close()
        except Exception:
            warn('Could not record Bark warning event; carrying on.')

    def work(self):
        while True:
            project, issue_id, message, row, suffix, level, url, sound, undelivered = self.queue.get()
            delivered = set()
            try:
                if project is None:
                    payload = dict(title=suffix, body=message, group='DD logins', level=level, url='')
                elif issue_id is None:
                    group = (self.cfg['projects'][project].get('plane_identifier') or project) + suffix
                    payload = dict(title=group, body=message, group=group, level=level, url=url or '')
                else:
                    project_cfg = self.cfg['projects'][project]
                    if row is None:
                        issue = plane.orchestrator(self.cfg).get_issue(project_cfg['plane_project_id'], issue_id)
                        key, name = str(issue['sequence_id']), issue.get('name', '')
                    else:
                        key, name = str(row['issue_key']), row['issue_name']
                    identifier = project_cfg.get('plane_identifier')
                    group = f'{identifier}-{key}' if identifier else key
                    payload = dict(title=group + suffix, body=f'{name}\n\n{message}',
                                   group=group, level=level, url=url or '')
                # Without a sound Bark plays its default, which every other alert keeps.
                if sound:
                    payload['sound'] = sound
                for device in self.cfg['alerts']['device_keys']:
                    try:
                        form = encrypted_form(self.cipher, payload)
                        endpoint = self.cfg['alerts']['bark_url'].rstrip('/') + '/' + urllib.parse.quote(device, safe='')
                        request = urllib.request.Request(endpoint, data=form,
                                                         headers={'Content-Type': 'application/x-www-form-urlencoded'})
                        with urllib.request.urlopen(request, timeout=10) as response:
                            result = json.loads(response.read(4096))
                            if result.get('code') != 200:
                                raise ValueError('Bark refused alert')
                    except Exception as error:
                        self.warning(project, issue_id, row, error)
                    else:
                        delivered.add(device)
            except Exception as error:
                self.warning(project, issue_id, row, error)
            finally:
                try:
                    if undelivered is not None and delivered != set(self.cfg['alerts']['device_keys']):
                        undelivered()
                except Exception:
                    warn('Could not rearm undelivered alert; carrying on.')
                self.queue.task_done()


def blocked(cfg, project, issue_id, message, *, row=None):
    """A story's Blocked alert, first sent or repeated, with the configured Blocked sound."""
    send(cfg, project, issue_id, message, row=row, suffix=' blocked', level='active',
         sound=config.blocked_sound(cfg))

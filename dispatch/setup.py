"""Guided setup that writes config.json. Entered values and service errors never reach output.

Secrets are typed hidden and kept only in config.json and its private backups,
which config.save writes the same way project onboarding does.
"""

import copy
import getpass
import importlib.util
import json
import os
import pwd
import re
import warnings
from pathlib import Path
from urllib.parse import urlsplit

from . import add_project, config, doctor, github, plane, redact

START_COMMAND = 'sudo systemctl enable --now dynamic-dispatch'
UUID = r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
EMAIL = r'[^\s@]+@[^\s@]+\.[^\s@]+'
# The installer's agent names, and the config entries they become. Each runs
# as the account the installer creates for it; see deploy/README.md.
AGENTS = {
    'claude': ('claude-code', {
        'max_concurrent': 1, 'display_name': 'Claude', 'prompt_via': 'stdin',
        'command': ['claude', '-p', '--model', 'opus', '--output-format', 'json', '--dangerously-skip-permissions'],
        'parser': 'claude_json', 'run_as': 'dd-agent', 'group': 'dd-agent',
        'env_file': '/etc/dynamic-dispatch/claude.env', 'timeout_seconds': 1800}),
    'codex': ('codex', {
        'max_concurrent': 1, 'display_name': 'Codex', 'prompt_via': 'stdin',
        'command': ['codex', 'exec', '--json', '--dangerously-bypass-approvals-and-sandbox', '--ephemeral', '-'],
        'parser': 'codex_json', 'run_as': 'dd-codex', 'group': 'dd-codex', 'timeout_seconds': 1800}),
    'gemini': ('gemini', {
        'max_concurrent': 1, 'display_name': 'Gemini', 'prompt_via': 'stdin',
        'command': ['gemini', '--yolo', '--output-format', 'json', '-m', 'gemini-2.5-pro'],
        'parser': 'gemini_json', 'run_as': 'dd-gemini', 'group': 'dd-gemini',
        'env_file': '/etc/dynamic-dispatch/gemini.env', 'timeout_seconds': 1800}),
}
INTRO = """\
DD setup writes config.json. Run it as DD's account, after the installer.
Before you start, have ready:
- Plane API tokens for the Orchestrator and each agent's Plane account, made in
  Plane's profile settings while signed in as that account.
- A GitHub App for DD (https://github.com/settings/apps/new) with contents read,
  pull requests write and commit statuses write, installed on your repositories,
  and its private key saved on this machine with chmod 600.
  Or a fine-grained token (https://github.com/settings/personal-access-tokens).
Secrets are typed hidden. Press Enter to keep a current value."""


class Invalid(Exception):
    """An answer failed its check; the message is safe to print."""


def _ask(label, current=None, *, secret=False, check=None, optional=False):
    if current and secret:
        label += ' [Enter keeps the current one]'
    elif current:
        label += f' [{current}]'
    while True:
        if secret:
            # getpass falls back to echoing input without a terminal; refuse that instead.
            with warnings.catch_warnings():
                warnings.simplefilter('error', getpass.GetPassWarning)
                value = getpass.getpass(label + ': ').strip()
        else:
            value = input(label + ': ').strip()
        value = value or current or ''
        if secret:
            redact.add_secret(value)
        try:
            if not value and not optional:
                raise Invalid('An answer is needed.')
            if value and check:
                check(value)
        except Invalid as error:
            print(error)
            continue
        return value


def _yes(label, default):
    def check(value):
        if value.lower() not in ('yes', 'no', 'y', 'n'):
            raise Invalid('Answer yes or no.')
    return _ask(label + ' (yes/no)', 'yes' if default else 'no', check=check).lower() in ('yes', 'y')


def _matches(pattern, message):
    def check(value):
        if not re.fullmatch(pattern, value):
            raise Invalid(message)
    return check


def _url(value):
    url = urlsplit(value)
    try:
        url.port
    except ValueError:
        raise Invalid('That port is not valid.') from None
    if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password \
            or url.query or url.fragment:
        raise Invalid('Enter an http:// or https:// address, without a login, query or #.')


def _plane_url(value):
    _url(value)
    host = urlsplit(value).hostname
    if host == 'plane.so' or host.endswith('.plane.so'):
        raise Invalid('Plane Cloud is not supported yet; enter your self-hosted Plane address.')


def _plane_user(p, token, who):
    """The token's Plane user ID, once it is shown to reach the workspace."""
    try:
        client = plane.Plane(p['base_url'], p['workspace_slug'], token)
        user = client.current_user()
        client.projects()
        return user
    except Exception as error:
        raise Invalid(doctor.service_reason(error, 'Plane', who, 'Plane gave an unexpected answer; '
                                            'check the address and workspace slug.')) from None


def _plane(cfg):
    """Ask for Plane's settings until the Orchestrator's token works; returns its Plane user ID."""
    p = cfg.setdefault('plane', {})
    while True:
        url = _ask('Plane address, as you open it in a browser', p.get('base_url'), check=_plane_url)
        p['base_url'] = _keep(p.get('base_url'), url.rstrip('/'), url)
        p['workspace_slug'] = _ask('Plane workspace slug (the first part of the path in any Plane page)',
                                   p.get('workspace_slug'),
                                   check=_matches(r'[\w-]+', 'Enter the slug alone, without slashes.'))
        p['orchestrator_token'] = _ask("Orchestrator's Plane API token", p.get('orchestrator_token'), secret=True)
        try:
            user = _plane_user(p, p['orchestrator_token'], "The Orchestrator's Plane token")
            break
        except Invalid as error:
            print(error)
    approver = _ask("Approver's Plane user ID (needed only for sprint-pull; Enter skips)",
                    p.get('approver_user_id'), optional=True,
                    check=_matches(UUID, 'A Plane user ID looks like 0f1e2d3c-0000-0000-0000-000000000000.'))
    if approver:
        p['approver_user_id'] = approver
    return user


def _key_file(value):
    path = Path(value).expanduser()
    try:
        private = path.is_file() and not path.stat().st_mode & 0o077 and os.access(path, os.R_OK)
    except OSError:
        private = False
    if not private:
        raise Invalid('That is not a readable file with mode 600; check the path and run chmod 600 on it.')


def _github_login(g, app):
    """A working credential for g, or Invalid saying what to fix."""
    try:
        if not app:
            github.current_user(g['token'])
            return g['token']
        credential, details = github.installation_details(g)
    except Exception as error:
        raise Invalid(doctor.service_reason(error, 'GitHub', 'The GitHub login',
                                            'Cannot sign in to GitHub; check the App ID, installation ID '
                                            'and key, and that openssl is installed.')) from None
    permissions = details.get('permissions', {})
    if permissions.get('contents') not in ('read', 'write') or \
            any(permissions.get(p) != 'write' for p in ('pull_requests', 'statuses')):
        # A fresh token after the next answer picks up permissions granted meanwhile.
        github.forget_token(g)
        raise Invalid('The App signed in, but needs contents read, pull requests write and commit statuses '
                      'write. Grant them and approve the change in GitHub, then answer again.')
    return credential


def _github(cfg):
    g = cfg.setdefault('github', {})
    app = _yes('Use a GitHub App (recommended)?', github.uses_app(g) or not g.get('token'))
    if not app:
        for key in github.APP_KEYS:
            g.pop(key, None)
    while True:
        if app:
            g['app_id'] = _keep(g.get('app_id'), _ask('GitHub App ID', _text(g.get('app_id')),
                                                      check=_matches(r'\d+', 'Enter digits only.')))
            g['installation_id'] = _keep(g.get('installation_id'), _ask(
                'GitHub App installation ID (the number at the end of its settings address)',
                _text(g.get('installation_id')), check=_matches(r'\d+', 'Enter digits only.')))
            key = _ask("Path to the App's private key file", g.get('private_key_path'), check=_key_file)
            g['private_key_path'] = _keep(g.get('private_key_path'), str(Path(key).expanduser().resolve()), key)
        else:
            g['token'] = _ask('GitHub token', g.get('token'), secret=True)
        try:
            credential = _github_login(g, app)
            break
        except Invalid as error:
            print(error)

    def approver(value):
        try:
            github.user(value, credential)
        except Exception as error:
            if getattr(error, 'status', None) == 404:
                raise Invalid('GitHub has no account with that username.') from None
            raise Invalid(doctor.service_reason(error, 'GitHub', 'The GitHub login',
                                                'Cannot look up that account; check the username.')) from None
    g['approver'] = _ask("Approver's GitHub username (their approval turns a pull request green)",
                         g.get('approver'), check=approver)


def _text(value):
    return None if value is None else str(value)


def _keep(current, value, answer=None):
    """The current value when it was kept, so a setting saved as a number or another spelling is unchanged."""
    return current if current is not None and _text(current) in (value, answer) else value


def _choose_agents(agents):
    """Add the installer's agents the user names; agents already configured stay."""
    configured = {short for short, (name, _) in AGENTS.items() if name in agents}
    installed = {short for short, (_, template) in AGENTS.items() if _account_exists(template['run_as'])}
    current = ' '.join(short for short in AGENTS if short in configured | installed) or 'claude codex'

    def check(value):
        names = value.split()
        if not all(name in AGENTS for name in names):
            raise Invalid('Use the installer\'s names: ' + ' '.join(AGENTS) + '.')
        if not configured <= set(names):
            raise Invalid('Configured agents stay; remove one by editing config.json.')
    for short in _ask('Agents chosen in the installer (' + ' '.join(AGENTS) + ')', current, check=check).split():
        name, template = AGENTS[short]
        agents.setdefault(name, copy.deepcopy(template))


def _account_exists(name):
    try:
        pwd.getpwnam(name)
        return True
    except KeyError:
        return False


def _agents(cfg, orchestrator):
    agents = cfg.setdefault('agents', {})
    _choose_agents(agents)
    users = {orchestrator}
    for name, agent in agents.items():
        who = agent.get('display_name') or name

        def token(value):
            user = _plane_user(cfg['plane'], value, f"{who}'s Plane token")
            if user in users:
                raise Invalid(f"That token signs in as the Orchestrator or another agent; use {who}'s own Plane account.")
            users.add(user)
        agent['plane_token'] = _ask(f"{who}'s Plane API token", agent.get('plane_token'), secret=True, check=token)
        author = agent.setdefault('git_author', {})
        author['name'] = _ask(f"{who}'s commit name", author.get('name') or who,
                              check=_matches(r'[^\n<>]+', 'Enter a name without < or >.'))
        author['email'] = _ask(f"{who}'s commit email", author.get('email'),
                               check=_matches(EMAIL, 'Enter an email address, such as a GitHub noreply address.'))


def _alerts(cfg):
    if not _yes('Send Blocked and review alerts to phones with Bark?', bool(cfg.get('alerts'))):
        cfg.pop('alerts', None)
        return
    a = cfg.setdefault('alerts', {})
    a['bark_url'] = _ask('Bark server address', a.get('bark_url'), check=_url)

    def devices(value):
        if any(len(key) < redact.MIN_SECRET_LENGTH for key in value.split()):
            raise Invalid('Each Bark device key is at least %d characters.' % redact.MIN_SECRET_LENGTH)
    a['device_keys'] = _ask('Bark device keys, separated by spaces', ' '.join(a.get('device_keys', [])),
                            secret=True, check=devices).split()

    def key(value):
        if not len(value) == len(value.encode()) == 32:
            raise Invalid('The encryption key is the 32 ASCII characters set in each Bark app.')
    a['encryption_key'] = _ask('Bark encryption key', a.get('encryption_key'), secret=True, check=key)
    if importlib.util.find_spec('cryptography') is None:
        print('Alerts need python3-cryptography: sudo apt install python3-cryptography')


def _existing(path):
    """The config file's own bytes and contents, or None and a new config's settings."""
    if not path.exists():
        return None, {'db_path': str(Path.home() / '.local/share/dynamic-dispatch/runs.db'),
                      'sandbox': {'user': 'dd-agent', 'launcher': '/usr/local/bin/dd-agent-run'},
                      'projects': {}}
    if path.stat().st_mode & 0o077:
        raise Invalid(f'{path} holds credentials and must be private: run chmod 600 {path}')
    original = path.read_bytes()
    try:
        cfg = json.loads(original)
    except ValueError:
        cfg = None
    if not isinstance(cfg, dict):
        raise Invalid(f'{path} is not a JSON object; fix it or move it aside, then run --setup again.')
    return original, cfg


def _finish(path, cfg):
    """Offer a project, run doctor and say how to start DD."""
    has_projects = bool(cfg.get('projects'))
    if _yes('Add another project now?' if has_projects else 'Add the first project now?', not has_projects):
        before = config.load(path, allow_empty_projects=True)['projects']
        result = add_project.run(path)
        projects = config.load(path, allow_empty_projects=True)['projects']
        # A saved project whose own doctor check failed is still added; doctor below reports it again.
        if result and projects == before:
            print('Adding the project did not finish, so no project was added; the reason is above. '
                  'config.json is saved: fix that, then run python3 main.py --add-project.')
            return 1
        has_projects = bool(projects)
    if not has_projects:
        print('Doctor needs a project to check; add one with: python3 main.py --add-project')
    print('Running doctor.')
    result = doctor.run(path)
    print('Finish the agent logins in deploy/README.md and fix any FAIL above. Then start DD with:')
    print('    ' + START_COMMAND)
    return result


def run(path=None):
    path = Path(path or config.DEFAULT_CONFIG_PATH)
    print(INTRO)
    try:
        original, cfg = _existing(path)
        orchestrator = _plane(cfg)
        _github(cfg)
        _agents(cfg, orchestrator)
        _alerts(cfg)
        cfg.setdefault('projects', {})
        config.save(path, cfg, original)
        if original is not None and path.read_bytes() == original:
            print('Nothing changed; config.json was left as it was.')
        else:
            print(f'Saved {path}.' + ('' if original is None else
                                      ' The previous version is kept beside it as a private backup.'))
        # DD makes this folder when it starts; making it now lets doctor's Store check pass first.
        Path(cfg['db_path']).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        return _finish(path, cfg)
    except Invalid as error:
        print('Setup stopped: ' + str(error))
    except getpass.GetPassWarning:
        print('Setup stopped: secrets can only be typed hidden, so run it in a terminal.')
    except (EOFError, KeyboardInterrupt):
        print('\nSetup cancelled. Run --setup again; anything already saved is offered as the current value.')
    except config.ConfigError as error:
        print('Setup stopped: ' + redact.scrub(str(error)))
    except Exception:
        # Service errors, API bodies and paths may hold credentials.
        print('Setup stopped unexpectedly. Check config.json permissions and the network, then run --setup again.')
    return 1

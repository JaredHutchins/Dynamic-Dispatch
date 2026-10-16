"""Read-only setup checks. Never include config values or exception text in output."""

from datetime import datetime, timezone
from pathlib import Path
import os
import pwd
import shutil
import stat
import sys
import http.client
from urllib.parse import urlsplit

from . import config, container, doctor_sandbox, github, gitops, plane, proc, runs
from .redact import MIN_SECRET_LENGTH

MIN_PYTHON = (3, 11)
CHECKOUT = Path(__file__).resolve().parent.parent
# The launcher runs agents with only this PATH, so their commands must be on it.
AGENT_PATH = '/usr/local/bin:/usr/bin:/bin'
SERVICE = 'dynamic-dispatch.service'
ROLLBACK_SERVICE = 'dynamic-dispatch-rollback.service'
ROLLBACK_HELPER = '/usr/local/bin/dd-rollback'
# Checked at its deployed path, not sandbox.launcher, so config can't point the check at another file.
LAUNCHER = '/usr/local/bin/dd-agent-run'
COMMAND_TIMEOUT = 30
START_STEP = '/usr/local/bin/dd-container-start'
CREDENTIAL_FOLDER = '/etc/dd-credentials'
# deploy/compose.yaml's volumes and the account that writes each; the image fixes the account names.
VOLUMES = [('DD home', '/home/dd', 'dd'), ('checkout', '/home/dd/checkout', 'dd'),
           ('config', '/home/dd/config', 'dd'), ('history', '/home/dd/.local/share/dynamic-dispatch', 'dd'),
           ('repositories', '/srv/dynamic-dispatch/repos', 'dd'),
           ('story folders', '/srv/dynamic-dispatch/worktrees', 'dd'),
           ('agent env files', '/etc/dynamic-dispatch', 'root')]
# The image's build setting naming the agents it includes, as deploy/dd-container-start
# reads it. An image built before the setting existed included only Gemini.
AGENTS_SETTING, DEFAULT_AGENTS = 'DD_AGENTS', 'gemini'
# Each agent the image can include: its name in reports, its tool, its account, its home
# (its own volume in deploy/compose.yaml), the env files that can hold its login and the
# login files it keeps in its home. Gemini's is the copy the start step makes of the mount.
CONTAINER_AGENTS = {
    'claude': ('Claude Code', 'claude', 'dd-agent', '/home/dd-agent', ['/etc/dynamic-dispatch/claude.env'], []),
    'codex': ('Codex', 'codex', 'dd-codex', '/home/dd-codex', ['/etc/dynamic-dispatch/codex.env'],
              ['/home/dd-codex/.codex/auth.json']),
    'gemini': ('Gemini', 'gemini', 'dd-gemini', '/home/dd-gemini', ['/etc/dynamic-dispatch/gemini.env'],
               ['/home/dd-gemini/gemini-credential.json']),
}


def service_reason(error, service, credential, fallback):
    """Why a service call failed, from its status and cause alone, never its message."""
    status = getattr(error, 'status', None)
    if status in (401, 403):
        return f"{credential} was rejected; check its validity and access permissions."
    if status == 404:
        return f"{service} resource was not found; check the configured location and account access."
    cause = error
    while cause is not None:
        if isinstance(cause, (OSError, http.client.HTTPException)):
            return f"Can't reach {service}; check the service URL, network and availability."
        cause = cause.__cause__
    return fallback


def _bark(settings):
    key = settings['encryption_key']
    if not (isinstance(key, str) and len(key) == 32 and len(key.encode()) == 32):
        raise ValueError('Invalid key')
    url = urlsplit(settings['bark_url'])
    if not (url.scheme in ('http', 'https') and url.hostname and not url.query and not url.fragment):
        raise ValueError('Invalid URL')
    if url.port is not None and not 0 < url.port < 65536:
        raise ValueError('Invalid port')
    devices = settings['device_keys']
    if not (isinstance(devices, list) and devices and
            all(isinstance(k, str) and len(k) >= MIN_SECRET_LENGTH for k in devices)):
        raise ValueError('Invalid devices')
    sound = settings.get('blocked_sound', config.DEFAULT_BLOCKED_SOUND)
    if not isinstance(sound, str):
        raise ValueError('Invalid sound')
    return sound


def _unit(name):
    """A systemd unit's settings, read with systemctl show. Fails unless it is installed."""
    result = proc.run_capped(['systemctl', 'show', '--property=LoadState,User,WorkingDirectory', '--', name],
                             timeout=COMMAND_TIMEOUT)
    if result.timed_out or result.returncode != 0:
        raise ValueError('systemctl failed')
    values = dict(line.partition('=')[::2] for line in result.stdout.splitlines())
    if values.get('LoadState') != 'loaded':
        raise ValueError('Not installed')
    return values


def _same_path(value):
    # systemd marks an optional directory with a leading "-".
    value = value.removeprefix('-')
    return bool(value) and Path(value).resolve() == CHECKOUT


def run(path=None, *, project=None):
    failed = False

    def report(status, check, reason):
        nonlocal failed
        failed |= status == 'FAIL'
        print(f'{status} {check}: {reason}')

    def check(name, action, good, bad, *, service=None, credential=None, skip=None):
        if skip:
            report('SKIP', name, 'skipped: ' + skip)
            return None
        try:
            value = action()
        except Exception as error:
            report('FAIL', name, service_reason(error, service, credential, bad) if service else bad)
            return None
        report('OK', name, good)
        return value

    def require(condition):
        if not condition:
            raise ValueError('Check failed')

    if sys.version_info[:2] >= MIN_PYTHON:
        report('OK', 'Python', 'Version %d.%d is new enough.' % sys.version_info[:2])
    else:
        report('FAIL', 'Python', 'DD needs Python %d.%d or newer; this is %d.%d.' % (*MIN_PYTHON, *sys.version_info[:2]))

    cfg = check('Config', lambda: config.load(path), 'config.json loads.',
                'Cannot load config.json; check its JSON, required settings and private file permissions.')
    check('Config permissions', lambda: require(stat.S_IMODE(Path(path or config.DEFAULT_CONFIG_PATH).stat().st_mode) == 0o600),
          'File mode is 600.', 'Set config.json permissions with chmod 600.')
    if cfg is None:
        return 1

    if project is not None:
        cfg['projects'] = {project: cfg['projects'][project]}

    check('Store', lambda: runs.check_writable(cfg['db_path']),
          'Database path is writable by file permissions; no write was attempted.',
          'Database path is not writable; create its directory and give DD read/write access.')
    if 'alerts' in cfg:
        try:
            sound = _bark(cfg['alerts'])
        except Exception:
            report('FAIL', 'Bark', 'Check Bark server URL, device keys, the 32-byte encryption key '
                   'and the Blocked alert sound name.')
        else:
            if sound:
                report('OK', 'Bark', 'Settings are valid; no alert sent.')
            else:
                report('WARN', 'Bark', 'alerts.blocked_sound is empty, so Blocked alerts play the default sound; '
                       'set it to a Bark sound name such as telegraph.')
    else:
        report('OK', 'Bark', 'Alerts are not configured; no action needed.')

    _check_plane(cfg, check, require)
    _check_github(cfg, check, report, require)
    _check_git(cfg, check, report)
    accounts = _check_agents(cfg, check, report, require)
    doctor_sandbox.check(cfg, accounts, CHECKOUT, path or config.DEFAULT_CONFIG_PATH, report)
    _check_services(cfg, accounts, check, report, require)
    return int(failed)


def _check_plane(cfg, check, require):
    def orchestrator():
        connection = plane.orchestrator_login(cfg)
        connection.current_user()
        return connection

    # A bot's check gets its token from Plane first, so it shows the app can sign in.
    bot = 'orchestrator_bot' in cfg['plane']
    orch = check('Plane Orchestrator', orchestrator,
                 'Plane is reachable and the Orchestrator app signs in.' if bot else
                 'Plane is reachable and the token works.',
                 'Cannot sign in to Plane; check its URL and Orchestrator settings.',
                 service='Plane', credential='Plane Orchestrator app login' if bot else 'Plane Orchestrator token')
    users = {}
    for index, (name, agent) in enumerate(cfg['agents'].items(), 1):
        bot = 'plane_bot' in agent
        users[name] = check(f'Plane agent {index}', lambda: plane.as_agent(cfg, agent).current_user(),
                            'App signs in.' if bot else 'Token works.',
                            'Cannot sign in; check Plane access and this agent ' + ('app.' if bot else 'token.'),
                            service='Plane', credential='Plane agent app login' if bot else 'Plane agent token')

    for index, project in enumerate(cfg['projects'].values(), 1):
        prefix = f'Plane project {index}'
        project_id = project['plane_project_id']
        dependency = 'the Plane Orchestrator check failed' if orch is None else None
        def states():
            ids = {s['id'] for s in orch.states(project_id)}
            require(all(value in ids for value in project['states'].values()))
        check(prefix + ' states', states, 'All configured states exist.',
              'Cannot verify states; check project access and every configured state ID.',
              service='Plane', credential='Plane Orchestrator token', skip=dependency)
        def label():
            ids = {s['id'] for s in orch.labels(project_id)}
            require(project.get('agent_label_id') in ids)
        check(prefix + ' agent label', label, 'Agent label exists.',
              'Cannot verify agent label; set agent_label_id to a label in this project and check access.',
              service='Plane', credential='Plane Orchestrator token', skip=dependency)
        for agent_index, name in enumerate(cfg['agents'], 1):
            if name not in config.project_agents(project):
                continue
            def member():
                members = orch.members(project_id)
                require(users[name] is not None and any(m.get('id') == users[name] for m in members))
            check(prefix + f' agent {agent_index} membership', member, 'Account is a project member.',
                  'Cannot verify membership; add this agent account to the project.',
                  service='Plane', credential='Plane Orchestrator token',
                  skip=dependency or ("this agent's Plane login failed" if users[name] is None else None))
        if config.cycles(project):
            _check_sprint_pull(cfg, project, orch, prefix, check, require, dependency)


def _check_sprint_pull(cfg, project, orch, prefix, check, require, dependency):
    """Checks for a project with sprint-pull configured; a project without it gets none."""
    project_id, cycles = project['plane_project_id'], config.cycles(project)
    def states():
        ids = {s['id'] for s in orch.states(project_id)}
        require(cycles['product_backlog_state'] in ids and cycles['sprint_backlog_state'] in ids)
    check(prefix + ' sprint-pull states', states, 'Product Backlog and Sprint Backlog states exist.',
          'Cannot verify sprint-pull states; set cycles product_backlog_state and sprint_backlog_state '
          'to states in this project.',
          service='Plane', credential='Plane Orchestrator token', skip=dependency)
    def label():
        ids = {s['id'] for s in orch.labels(project_id)}
        require(cycles['carried_over_label_id'] in ids)
    check(prefix + ' carried-over label', label, 'Carried-over label exists.',
          'Cannot verify carried-over label; set cycles carried_over_label_id to a label in this project.',
          service='Plane', credential='Plane Orchestrator token', skip=dependency)
    def approver():
        members = orch.members(project_id)
        require(any(m.get('id') == cfg['plane']['approver_user_id'] for m in members))
    check(prefix + ' approver membership', approver, "The approver's account is a project member.",
          'Cannot verify the approver; set plane approver_user_id to the Plane user ID of a project member.',
          service='Plane', credential='Plane Orchestrator token', skip=dependency)
    check(prefix + ' Cancelled state', lambda: require(project['states'].get('cancelled')),
          'states cancelled is set, as sprint-pull needs.',
          'Set states cancelled to the Cancelled state ID so cancelled stories finish their Cycle.')
    check(prefix + ' Done state', lambda: require(project['states'].get('done')),
          'states done is set, as sprint-pull needs.',
          'Sprint-pull moves merged stories to Done itself; set states done to the Done state ID.')


def _check_github(cfg, check, report, require):
    app = github.uses_app(cfg['github'])
    auth = check('GitHub login', lambda: github.installation_details(cfg['github']) if app else (github.token(cfg['github']), {}),
                 'App token obtained.' if app else 'Personal token loaded; repository checks verify access.',
                 'Cannot authenticate; check GitHub token or App settings.',
                 service='GitHub', credential='GitHub token')
    if auth is not None:
        credential, details = auth
        for index, project in enumerate(cfg['projects'].values(), 1):
            repo = check(f'GitHub repository {index}', lambda: github.repository(project['github_repo'], credential),
                         'GitHub is reachable and repository is accessible.',
                         'Cannot access repository; check credentials and repository access.',
                         service='GitHub', credential='GitHub token')
            if not app and repo is None:
                report('SKIP', f'GitHub repository {index} permissions',
                       'skipped: the GitHub repository check failed')
            elif not app:
                push = repo.get('permissions', {}).get('push') if isinstance(repo, dict) else None
                suffix = (' Repository push permission is ' + ('allowed.' if push else 'denied.')) if isinstance(push, bool) else ' Repository push permission was not reported.'
                report('WARN', f'GitHub repository {index} permissions',
                       "write permissions can't be checked for a personal token; verify contents, pull requests and commit statuses write access." + suffix)
        if app:
            # DD pushes with the host's saved Git login, never the App, so the App only reads code.
            check('GitHub App contents', lambda: require(details['permissions'].get('contents') in ('read', 'write')),
                  'Installation can read the repository; DD pushes with the host\'s saved Git login, not the App.',
                  'Grant this installation contents read permission and approve the updated App permissions.')
            for permission in ('pull_requests', 'statuses'):
                check('GitHub App ' + permission, lambda: require(details['permissions'].get(permission) == 'write'),
                      'Installation has write permission.', 'Grant this installation write permission and approve the updated App permissions.')
            report('OK', 'GitHub expiry', 'App tokens renew automatically.')
        else:
            try:
                expiry = github.token_expiry(credential)
                remaining = (expiry - datetime.now(timezone.utc)).total_seconds() / 86400
                status = 'FAIL' if remaining <= 0 else 'WARN' if remaining <= 30 else 'OK'
                report(status, 'GitHub expiry', 'Token expires at ' + expiry.isoformat() +
                       '; renew before this date.')
            except Exception:
                report('WARN', 'GitHub expiry', 'Expiry was not reported or could not be read; check credential expiry in GitHub.')
    else:
        for index, project in enumerate(cfg['projects'].values(), 1):
            report('SKIP', f'GitHub repository {index}', 'skipped: the GitHub login check failed')
        if app:
            for permission in ('contents', 'pull_requests', 'statuses'):
                report('SKIP', 'GitHub App ' + permission, 'skipped: the GitHub login check failed')
        report('SKIP', 'GitHub expiry', 'skipped: the GitHub login check failed')


def _check_git(cfg, check, report):
    for index, project in enumerate(cfg['projects'].values(), 1):
        name = f'Git push login {index}'
        reached = check(name, lambda: gitops.check_origin(project['repo_path']) or True,
                        "The host's saved Git login can read the repository; nothing was pushed.",
                        "Cannot reach origin from repo_path; check the clone, its remote and the host's saved "
                        "Git login in ~/.git-credentials, as deploy/README.md describes.")
        if reached:
            report('WARN', name + ' write access',
                   "write access can't be confirmed without pushing; DD's login alerts report a rejected push.")
        else:
            report('SKIP', name + ' write access', 'skipped: the ' + name + ' check failed')


def _check_agents(cfg, check, report, require):
    accounts = {}
    for index, (name, agent) in enumerate(cfg['agents'].items(), 1):
        prefix = f'Agent {index}'
        command = check(prefix + ' command', lambda: require(shutil.which(agent['command'][0], path=AGENT_PATH)) or True,
                        'Command is installed on the launcher\'s PATH.',
                        'Command is not installed; install it in /usr/local/bin, /usr/bin or /bin, '
                        'the only folders the launcher searches.')
        accounts[name] = check(prefix + ' account', lambda: pwd.getpwnam(config.run_as(cfg, name)) and config.run_as(cfg, name),
                               'Account exists.', 'Account does not exist; create the agent account as ARCHITECTURE.md describes.')
        if agent.get('env_file'):
            def env_file():
                # Only the file's metadata is read, never its contents.
                st = os.stat(agent['env_file'])
                require(stat.S_ISREG(st.st_mode) and st.st_uid == 0 and
                        st.st_gid == config.agent_group(cfg, name) and stat.S_IMODE(st.st_mode) == 0o640)
            check(prefix + ' env file', env_file, "File exists, owned by root and the agent's group, with mode 640; it was not read.",
                  "Env file is missing or has the wrong owner or mode; make it root:<agent group> with mode 640, "
                  'as deploy/README.md "Agent logins" describes.',
                  skip="this agent's account check failed" if accounts[name] is None else None)
        else:
            report('OK', prefix + ' env file', 'No env file is configured; the agent keeps its login in its own home.')
        if command is None or accounts[name] is None:
            report('SKIP', prefix + ' login', "skipped: this agent's command or account check failed")
        else:
            report('WARN', prefix + ' login', "the login itself can't be checked without running the agent; "
                   "DD's login alerts report a rejected login on the first run.")
    return accounts


def _included_agents(report):
    names = os.environ.get(AGENTS_SETTING, DEFAULT_AGENTS).split()
    if not names or any(name not in CONTAINER_AGENTS for name in names):
        report('FAIL', 'Included agents', 'DD_AGENTS names no agent, or one DD does not know; rebuild the image '
               'with any of claude, codex and gemini, as deploy/CONTAINER.md describes.')
        return []
    agents = list(dict.fromkeys(names))
    report('OK', 'Included agents', 'The image includes ' + ', '.join(CONTAINER_AGENTS[a][0] for a in agents) + '.')
    return agents


def _check_container_agents(check, report, require):
    """Each included agent's tool and account, and the volumes and login files of all of them."""
    volumes = [('Volume ' + label, path, login) for label, path, login in VOLUMES]
    env_files, logins = [], []
    for agent in _included_agents(report):
        label, tool, account, home, env_paths, login_paths = CONTAINER_AGENTS[agent]
        rebuild = f'rebuild the image with {agent} in DD_AGENTS, as deploy/CONTAINER.md describes.'
        check(label + ' tool', lambda: require(shutil.which(tool, path=AGENT_PATH)),
              f"{tool} is installed on the launcher's PATH.", f'{tool} is not installed; ' + rebuild)
        check(label + ' account', lambda: pwd.getpwnam(account), f'{account} exists.',
              f'{account} does not exist; ' + rebuild)
        volumes.append((f'Volume {label} home', home, account))
        env_files += [(f'{label} env file', path, account) for path in env_paths]
        logins += [(f'{label} login', path, account) for path in login_paths]
    doctor_sandbox.check_container(volumes, env_files, logins, report)


def _check_container(check, report, require):
    _check_container_agents(check, report, require)
    def start_step():
        st = os.lstat(START_STEP)
        require(stat.S_ISREG(st.st_mode) and st.st_uid == 0 and st.st_mode & 0o111)
    check('Start step', start_step, '/usr/local/bin/dd-container-start is installed and owned by root.',
          '/usr/local/bin/dd-container-start is missing or not owned by root; rebuild the image as '
          'deploy/README.md "Docker" describes.')
    def installer():
        require(stat.S_ISREG(os.lstat(CHECKOUT / 'deploy' / 'dd-install').st_mode))
    check('Installer', installer, "deploy/dd-install is in DD's checkout for the start step to run.",
          "deploy/dd-install is missing from DD's checkout, so the next start fails; restore the checkout.")
    def credential_folder():
        # Its contents are never listed: only root may open it, and the start step reads the mount as root.
        st = os.lstat(CREDENTIAL_FOLDER)
        require(stat.S_ISDIR(st.st_mode) and st.st_uid == 0 and not stat.S_IMODE(st.st_mode) & 0o077)
    check('Credential mount folder', credential_folder, '/etc/dd-credentials is a folder only root can open.',
          '/etc/dd-credentials is missing or open to accounts other than root; mount the credential file, '
          'not a folder, as deploy/README.md "A Gemini credential file" describes.')


def _check_services(cfg, accounts, check, report, require):
    if container.in_container():
        for name in ('Service', 'Service account', 'Service checkout', 'Rollback service'):
            report('SKIP', name, "skipped: systemd units don't apply in a container, whose start step "
                   'starts and recovers DD instead.')
        _check_container(check, report, require)
    else:
        _check_units(check, require)
    _check_installed(cfg, accounts, check, require)


def _check_units(check, require):
    me = pwd.getpwuid(os.geteuid()).pw_name
    service = check('Service', lambda: _unit(SERVICE), 'dynamic-dispatch.service is installed.',
                    'dynamic-dispatch.service is not installed; render and install it as deploy/README.md '
                    '"Install (once)" describes.')
    missing = 'the Service check failed' if service is None else None
    check('Service account', lambda: require(service.get('User') == me),
          'Service runs as the account running the doctor.',
          'Service runs as a different account; render dynamic-dispatch.service for this account and reinstall it '
          'as deploy/README.md "When a deploy file changes" describes.', skip=missing)
    check('Service checkout', lambda: require(_same_path(service.get('WorkingDirectory', ''))),
          'Service runs from this checkout.',
          'Service runs from a different checkout; render dynamic-dispatch.service for this checkout and reinstall it '
          'as deploy/README.md "When a deploy file changes" describes.', skip=missing)
    check('Rollback service', lambda: _unit(ROLLBACK_SERVICE), 'dynamic-dispatch-rollback.service is installed.',
          'dynamic-dispatch-rollback.service is not installed; install it as deploy/README.md "Install (once)" describes.')


def _check_installed(cfg, accounts, check, require):
    def helper():
        st = os.lstat(ROLLBACK_HELPER)
        require(stat.S_ISREG(st.st_mode) and st.st_mode & 0o111)
    check('Rollback helper', helper, '/usr/local/bin/dd-rollback is installed.',
          '/usr/local/bin/dd-rollback is not installed; install it as deploy/README.md "Install (once)" describes.')
    def launcher():
        st = os.lstat(LAUNCHER)
        require(stat.S_ISREG(st.st_mode) and st.st_uid == 0 and stat.S_IMODE(st.st_mode) == 0o755)
    launched = check('Launcher', lambda: launcher() or True, '/usr/local/bin/dd-agent-run is owned by root with mode 755.',
                     '/usr/local/bin/dd-agent-run is missing or not owned by root with mode 755; reinstall it as '
                     'deploy/README.md "When a deploy file changes" describes.')
    for index, name in enumerate(cfg['agents'], 1):
        def rule():
            # -l with a command only asks whether sudo would allow it; nothing runs.
            result = proc.run_capped(['sudo', '-n', '-l', '-u', accounts[name], LAUNCHER],
                                     timeout=COMMAND_TIMEOUT)
            require(result.returncode == 0 and not result.timed_out)
        check(f'Sudo rule agent {index}', rule, "DD's account may run the launcher as this agent's account.",
              "sudo does not let DD's account run the launcher as this agent's account; render and reinstall "
              'sudoers-dynamic-dispatch as deploy/README.md "When a deploy file changes" describes.',
              skip='the Launcher check failed' if launched is None else
                   "this agent's account check failed" if accounts[name] is None else None)

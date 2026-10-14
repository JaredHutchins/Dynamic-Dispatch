"""Interactive, resumable project setup. Service errors never reach terminal output."""

import json
import re
import shlex
import shutil
from pathlib import Path

from . import config, doctor, github, gitops, plane, redact

STATES = (
    ('product_backlog', 'Product Backlog', 'backlog'),
    ('sprint_backlog', 'Sprint Backlog', 'unstarted'),
    ('in_progress', 'In Progress', 'started'),
    ('in_review', 'In Review', 'started'),
    ('done', 'Done', 'completed'),
    ('blocked', 'Blocked', 'started'),
    ('cancelled', 'Cancelled', 'cancelled'),
)
LABELS = ('Agent', 'Human', 'Carried Over')


class SetupError(Exception):
    pass


def _answers(cfg):
    name = input('Project name: ').strip()
    repo = input('GitHub repository (OWNER/REPO or HTTPS URL): ').strip()
    repo = repo.removeprefix('https://github.com/').removesuffix('.git').rstrip('/')
    command = shlex.split(input('Test command (arguments, without shell operators): '))
    answer = input('Enable sprint-pull Cycles? [y/N]: ').strip().lower()
    if not name or len(name) > 255 or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
        raise SetupError('Enter a project name and a GitHub repository in OWNER/REPO form.')
    if any(part in ('.', '..') for part in repo.split('/')) or not command:
        raise SetupError('Enter a valid repository and a non-empty test command.')
    if answer not in ('', 'n', 'no', 'y', 'yes'):
        raise SetupError('Answer yes or no for Cycles.')
    enabled = answer in ('y', 'yes')
    if enabled and not cfg['plane'].get('approver_user_id'):
        raise SetupError('Set plane.approver_user_id before enabling sprint-pull Cycles.')
    old = cfg['projects'].get(name)
    if old and old['github_repo'].lower() != repo.lower():
        raise SetupError('That configured project uses a different repository; nothing was changed.')
    return name, repo, command, enabled


def _github(cfg, repo):
    if not github.uses_app(cfg['github']):
        raise SetupError('Configure the ND Dispatch GitHub App before adding a project.')
    try:
        credential, details = github.installation_details(cfg['github'])
        permissions = details.get('permissions', {})
        if (permissions.get('contents') not in ('read', 'write') or
                any(permissions.get(p) != 'write' for p in ('pull_requests', 'statuses'))):
            raise SetupError('Grant the ND Dispatch app contents read, pull requests write and statuses write access.')
        repository = github.repository(repo, credential)
    except github.GitHubError:
        raise SetupError('Cannot access the GitHub repository. Check that it exists and the ND Dispatch app has access.') from None
    url = f'https://github.com/{repo}.git'
    try:
        has_commit = gitops.check_repository_access(url)
    except gitops.GitError:
        raise SetupError('The saved push login cannot read the repository. Give it access and check its Git credentials.') from None
    if not has_commit:
        raise SetupError('The GitHub repository is empty; push a first commit to its default branch.')
    print('Push write access cannot be proved without pushing; confirm it in GitHub.')
    return url, repository.get('default_branch') or 'main'


def _project(client, name):
    projects = client.projects()
    found = [p for p in projects if p['name'].casefold() == name.casefold()]
    if len(found) > 1:
        raise SetupError('More than one Plane project has that name; resolve the ambiguity first.')
    if found:
        if input('A Plane project with this name exists. Use it? [y/N]: ').strip().lower() not in ('y', 'yes'):
            raise SetupError('Existing project left unchanged.')
        return found[0]
    return client.post('/projects/', {'name': name, 'identifier': _identifier(name, projects)})


def _identifier(name, projects):
    """A short work item prefix from the name, numbered when another project already has it."""
    base = re.sub(r'[^A-Z0-9]', '', name.upper())[:5] or 'P'
    used = {p.get('identifier') for p in projects}
    for suffix in ('', *map(str, range(2, 100))):
        if base[:5 - len(suffix)] + suffix not in used:
            return base[:5 - len(suffix)] + suffix
    raise SetupError('Every generated Plane identifier is in use; create the Plane project by hand, then run setup again.')


def _board(client, project_id):
    states = client.states(project_id)
    labels = client.labels(project_id)
    result, label_ids = {}, {}
    for key, name, group in STATES:
        matches = [s for s in states if s['name'].casefold() == name.casefold()]
        if len(matches) > 1 or (matches and matches[0].get('group') != group):
            raise SetupError('An existing DD column is ambiguous or in the wrong state group; check it in Plane.')
        state = matches[0] if matches else client.post(f'/projects/{project_id}/states/', {
            'name': name, 'group': group, 'color': '#60646C', 'sequence': len(result) * 1000,
        })
        result[key] = state['id']
    for name in LABELS:
        matches = [label for label in labels if label['name'].casefold() == name.casefold()]
        if len(matches) > 1:
            raise SetupError('An existing DD label is ambiguous; check it in Plane.')
        label = matches[0] if matches else client.post(f'/projects/{project_id}/labels/', {
            'name': name, 'color': '#60646C',
        })
        label_ids[name] = label['id']
    return result, label_ids


def _members(cfg, client, project_id, enabled):
    wanted = {client.current_user(): 20}
    for agent in cfg['agents'].values():
        wanted.setdefault(plane.as_agent(cfg, agent).current_user(), 15)
    if enabled:
        wanted.setdefault(cfg['plane']['approver_user_id'], 15)
    existing = {m['id'] for m in client.members(project_id)}
    for user_id, role in wanted.items():
        if user_id not in existing:
            client.add_member(project_id, user_id, role)


def _settings(cfg, old, repo, command, enabled, project, states, labels, branch):
    """The project's settings, merged over what config.json holds (not the defaults loading adds)."""
    parents = {str(Path(p['repo_path']).parent) for p in cfg['projects'].values()}
    roots = {p['worktree_root'] for p in cfg['projects'].values()}
    if not old and ((len(parents) > 1 and not cfg.get('projects_root')) or
                    (len(roots) > 1 and not cfg.get('worktree_root'))):
        raise SetupError('Projects use different code locations; set projects_root and worktree_root in config.json.')
    parent = cfg.get('projects_root') or next(iter(parents), '/srv/dynamic-dispatch/repos')
    root = cfg.get('worktree_root') or next(iter(roots), '/srv/dynamic-dispatch/worktrees')
    folder = repo.replace('/', '--')
    result = dict(old, plane_project_id=project['id'], plane_identifier=project['identifier'],
                  github_repo=repo, repo_path=old.get('repo_path', str(Path(parent) / folder)),
                  worktree_root=old.get('worktree_root', root), base_branch=branch,
                  agents=list(cfg['agents']), verify_command=command, states=states,
                  agent_label_id=labels['Agent'], human_only_label_id=labels['Human'],
                  carried_over_label_id=labels['Carried Over'])
    if enabled:
        result['cycles'] = dict(old.get('cycles', {}), product_backlog_state=states['product_backlog'],
                                sprint_backlog_state=states['sprint_backlog'],
                                carried_over_label_id=labels['Carried Over'])
    else:
        result.pop('cycles', None)
    return result


def run(path=None):
    print('Before starting: create the GitHub repository, give the ND Dispatch app access, and give '
          "the Pi's saved push login write access. DD checks access where possible; it changes none of these settings.")
    path = Path(path or config.DEFAULT_CONFIG_PATH)
    stage = 'loading config; check its settings and private permissions'
    try:
        original = path.read_bytes()
        cfg = config.load(path, allow_empty_projects=True)
        redact.configure(cfg)
        stage = 'reading project details; check the repository format and test command quoting'
        name, repo, command, enabled = _answers(cfg)
        stage = 'checking GitHub access'
        url, branch = _github(cfg, repo)
        stage = 'setting up Plane; check account access and project administration permissions'
        client = plane.orchestrator_login(cfg)
        project = _project(client, name)
        old = cfg['projects'].get(name)
        if old and old['plane_project_id'] != project['id']:
            raise SetupError('That configured name refers to a different Plane project; nothing was replaced.')
        states, labels = _board(client, project['id'])
        _members(cfg, client, project['id'], enabled)
        saved = json.loads(original)['projects'].get(name, {})
        settings = _settings(cfg, saved, repo, command, enabled, project, states, labels, branch)
        stage = 'cloning code; check the code directory, Git filters and saved push login'
        gitops.clone_project(settings['repo_path'], url)
        if saved == settings:
            print('Project is already saved in config. Running doctor for this project.')
        else:
            stage = 'saving config; check permissions and whether another process changed it'
            config.save_project(path, name, settings, original)
            print('Project saved. Running doctor for this project.')
        stage = 'running doctor; project is saved and setup can be run again'
        result = doctor.run(path, project=name)
        print('Still to do:')
        if result:
            print('- Resolve the FAIL and SKIP checks above, then run --doctor again.')
        if '/' not in command[0] and not shutil.which(command[0], path=doctor.AGENT_PATH):
            print('- Install the test command for the agent accounts, or check its project-relative path.')
        print('- Confirm push write access and test dependencies; doctor does not execute tests.')
        print('- Restart DD to load the project when ready.')
        return result
    except SetupError as error:
        print('Setup stopped: ' + str(error))
    except (EOFError, KeyboardInterrupt):
        print('Setup cancelled. Run --add-project again to resume.')
    except Exception:
        # API bodies, config errors, paths and subprocess output may contain credentials.
        print('Setup stopped while ' + stage + '. Run --add-project again to resume.')
    return 1

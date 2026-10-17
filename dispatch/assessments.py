"""Independent Cycle assessments: snapshot, validate, retry and discard work folders."""

import hashlib
import json
import os
from pathlib import Path
import re

from . import agents, alerts, choose, config, github, gitops, plane, prompts, runs, sync
from .text import html_to_text


def snapshot_stories(conn, name, project, stories, cycle_id=None):
    """The Cycle's stories as assessors see them; the plan item and human-only stories are left out."""
    excluded = runs.plan_issue_ids(conn, name)
    human = project.get('human_only_label_id')
    if cycle_id and runs.plan_approval(conn, name, cycle_id) is not None:
        started = {project['states'].get(key) for key in ('in_progress', 'in_review', 'blocked', 'done')} - {None}
        stories = [story for story in stories if plane.state_id(story) not in started]
    selected = []
    for story in stories:
        labels = [x.get('id') if isinstance(x, dict) else x for x in story.get('labels', [])]
        if story['id'] in excluded or (human and human in labels):
            continue
        selected.append({'id': story['id'], 'name': story.get('name', ''),
                         'description': html_to_text(story.get('description_html'))
                         or story.get('description_stripped') or ''})
    return selected


def start(conn, client, name, project, plan, cycle, stories, cfg):
    snapshot = runs.assessment_snapshot(conn, name, plan['cycle_id'])
    if snapshot is None or runs.assessment_requested(conn, name, plan['cycle_id']):
        selected = snapshot_stories(conn, name, project, stories, plan['cycle_id'])
        try:
            revision = github.main_revision(project['github_repo'], project.get('base_branch', 'main'),
                                            github.token(cfg['github']))
        except github.GitHubError as exc:
            runs.record_event(conn, None, 'warning', f'Could not snapshot main; retrying next poll: {exc}', project=name)
            return
        excluded = runs.plan_issue_ids(conn, name)
        snapshot = runs.save_assessment_snapshot(conn, name, plan['cycle_id'], plan['issue_id'], revision,
                                                selected, cycle.get('end_date'), config.project_agents(project),
                                                members=[s['id'] for s in stories
                                                         if s['id'] not in excluded], start_date=cycle.get('start_date'))
    items = runs.assessments(conn, snapshot['id'])
    names = [cfg.get('agents', {}).get(item['agent'], {}).get('display_name', item['agent']) for item in items]
    marker = f"[dd:assessment {snapshot['id']}]"
    if not client.has_comment_with(project['plane_project_id'], plan['issue_id'], marker):
        client.comment(project['plane_project_id'], plan['issue_id'], sync.assessment_started(names), marker=marker)
    for item, display in zip(items, names):
        if item['failures'] < 2 or item['alerted']:
            continue
        message = sync.assessment_failed(display)
        marker = f"[dd:assessment-failed {snapshot['id']} {item['run_id']}]"
        if not client.has_comment_with(project['plane_project_id'], plan['issue_id'], marker):
            client.comment(project['plane_project_id'], plan['issue_id'], message, marker=marker)
        alerts.send(cfg, name, plan['issue_id'], message, level='active')
        runs.mark_assessment_alerted(conn, snapshot['id'], item['agent'])
    runs.queue_assessments(conn, snapshot['id'])
    announce_cooldowns(conn, client, name, project, plan, snapshot, cfg)


def validate(text, stories):
    """Return the parsed answer and exact problems; story IDs disambiguate duplicate titles."""
    blocks = re.findall(r'```json\s*\n(.*?)\n```', text, re.S)
    if len(blocks) != 1 or text.count('```') != 2:
        return None, ['Return exactly one fenced json block.']
    try:
        answer = json.loads(blocks[0])
    except (ValueError, TypeError) as exc:
        return None, [f'Invalid JSON: {exc}']
    if not isinstance(answer, dict) or set(answer) != {'stories', 'build_order', 'push'}:
        return None, ['Top-level fields must be stories, build_order and push.']
    ids = {story['id'] for story in stories}
    problems = []
    rows = answer['stories']
    if not isinstance(rows, list):
        return None, ['stories must be a list.']
    seen = []
    for index, row in enumerate(rows):
        prefix = f'stories[{index}]'
        fields = {'id', 'size', 'risk', 'reason', 'depends_on', 'overlaps', 'confidence', 'scope_problems'}
        if not isinstance(row, dict) or set(row) != fields:
            problems.append(f'{prefix} must have exactly {", ".join(sorted(fields))}.')
            continue
        seen.append(row['id'])
        for field, allowed in [('size', ('S', 'M', 'L')), ('risk', ('low', 'medium', 'high')),
                               ('confidence', ('low', 'medium', 'high'))]:
            if row[field] not in allowed:
                problems.append(f'{prefix}.{field} must be one of {", ".join(allowed)}.')
        if not isinstance(row['reason'], str) or not row['reason'].strip():
            problems.append(f'{prefix}.reason must be non-empty text.')
        if (not isinstance(row['scope_problems'], list)
                or any(not isinstance(x, str) or not x.strip() for x in row['scope_problems'])):
            problems.append(f'{prefix}.scope_problems must be a list of non-empty text.')
        for field in ('depends_on', 'overlaps'):
            values = row[field]
            if (not isinstance(values, list) or any(not isinstance(x, str) or x not in ids or x == row['id']
                                                    for x in values) or len(values) != len(set(values))):
                problems.append(f'{prefix}.{field} must list unique other Cycle story IDs.')
    if len(seen) != len(ids) or any(not isinstance(x, str) or x not in ids for x in seen) or any(seen.count(x) != 1 for x in ids):
        problems.append('Every Cycle story must appear exactly once in stories.')
    order = answer['build_order']
    if (not isinstance(order, list) or len(order) != len(ids)
            or any(not isinstance(x, str) or x not in ids for x in order) or any(order.count(x) != 1 for x in ids)):
        problems.append('build_order must contain every Cycle story ID exactly once.')
    pushed = answer['push']
    if not isinstance(pushed, list):
        problems.append('push must be a list.')
    else:
        used = []
        for row in pushed:
            if (not isinstance(row, dict) or set(row) != {'id', 'reason'}
                    or not isinstance(row['id'], str) or row['id'] not in ids or row['id'] in used
                    or not isinstance(row['reason'], str) or not row['reason'].strip()):
                problems.append('Each push entry must contain a unique Cycle story id and non-empty reason.')
            else:
                used.append(row['id'])
    return answer, problems


def folder_state(folder):
    """Detect additions, deletions and edits, including ignored files and symlinks."""
    state = {}
    for root, dirs, files in os.walk(folder):
        dirs[:] = [name for name in dirs if name != '.git']
        for name in dirs + files:
            path = Path(root) / name
            if name == '.git':
                continue
            key = str(path.relative_to(folder))
            if path.is_symlink():
                state[key] = ('link', os.readlink(path))
            elif path.is_file():
                state[key] = ('file', hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mode)
            else:
                state[key] = ('directory',)
    return state


def ask(cfg, conn, run, snapshot, folder):
    prompt = prompts.assessment_prompt(snapshot)
    for attempt in range(2):
        result = agents.run(cfg, config.agent(cfg, run['agent']), prompt, folder, project_cfg=cfg["projects"][run["project"]])
        runs.record_usage(conn, run['id'], run['agent'], result.get('usage'), result.get('cost_usd'), additive=True)
        if result.get('usage_limit'):
            choose.cool_down(cfg, conn, run['agent'], usage_limit_until=result.get('usage_limit_until'), run_id=run['id'])
            return runs.finish_assessment(conn, run['id'], limited=True)
        if not result.get('ok'):
            return runs.finish_assessment(conn, run['id'], error=result.get('error') or result.get('summary') or 'Agent failed.')
        answer, problems = validate(result.get('summary') or '', json.loads(snapshot['stories']))
        if not problems:
            return runs.finish_assessment(conn, run['id'], answer=answer)
        prompt += '\nYour answer:\n' + result['summary'] + '\nCorrect these exact problems:\n' + '\n'.join(problems)
    runs.finish_assessment(conn, run['id'], error='Invalid assessment twice: ' + '; '.join(problems))


def discard_folder(conn, run, repo, folder, before=None):
    """Check for edits and restore DD's access before removing a disposable checkout."""
    try:
        changed = folder_state(folder) != before if before is not None else gitops.has_changes(repo, folder)
        if changed:
            runs.record_event(conn, run['id'], 'warning', 'Agent changed assessment files; changes discarded.')
    finally:
        folder.chmod(folder.stat().st_mode | 0o200)
        for current, dirs, files in os.walk(folder):
            for name in dirs:
                path = Path(current) / name
                if not path.is_symlink():
                    path.chmod(path.stat().st_mode | 0o200)
        problem = gitops.remove_worktree(repo, folder)
        if problem:
            raise gitops.GitError(problem)


def folder_name(snapshot_id, agent):
    """The assessment container for one agent's attempt at one snapshot, beside the story folders."""
    account = hashlib.sha256(agent.encode()).hexdigest()[:16]
    return f'plan-{snapshot_id}-{account}'


def is_folder_name(name):
    """Whether a folder name is one folder_name could have made."""
    return re.fullmatch(r'plan-[0-9]+-[0-9a-f]{16}', name) is not None


def run_assessment(cfg, conn, run):
    root, folder, before = None, None, None
    project = cfg['projects'][run['project']]
    repo = project['repo_path']
    try:
        snapshot = runs.assessment_for_run(conn, run['id'])
        root = Path(project['worktree_root']) / folder_name(snapshot['snapshot_id'], run['agent'])
        group = config.agent_group(cfg, run['agent'])
        root.mkdir(mode=0o750, parents=True, exist_ok=True)
        os.chown(root, -1, group)
        root.chmod(0o750)
        previous = root / gitops.MAIN_CHECK_FOLDER
        if previous.exists():
            discard_folder(conn, run, repo, previous)
        folder = gitops.main_check_folder(repo, root, project.get('base_branch', 'main'), snapshot['revision'],
                                          group=group)
        # DD owns the files; the agent's group may read but cannot write this checkout.
        for current, dirs, files in os.walk(folder):
            for name in files + dirs:
                path = Path(current) / name
                if not path.is_symlink():
                    path.chmod(path.stat().st_mode & ~0o222)
        folder.chmod(folder.stat().st_mode & ~0o222)
        before = folder_state(folder)
        ask(cfg, conn, run, snapshot, folder)
    except Exception as exc:
        runs.finish_assessment(conn, run['id'], error=f'Assessment failed: {exc}')
    finally:
        if folder is not None:
            try:
                discard_folder(conn, run, repo, folder, before)
            except (OSError, gitops.GitError) as exc:
                runs.record_event(conn, run['id'], 'warning', f'Could not discard assessment checkout: {exc}')
        if root is not None:
            try:
                # Remove only this attempt's empty container, never neighbouring story folders.
                root.rmdir()
            except FileNotFoundError:
                pass
            except OSError as exc:
                runs.record_event(conn, run['id'], 'warning', f'Could not discard assessment folder: {exc}')


def cooldown_messages(conn, snapshot, cfg):
    for row in runs.assessment_cooldowns(conn, snapshot['id']):
        display = (cfg or {}).get('agents', {}).get(row['agent'], {}).get('display_name', row['agent'])
        yield row, sync.assessment_cooldown(display, row['until'])


def waiting_reply(conn, name, cycle_id, cfg=None):
    snapshot = runs.assessment_snapshot(conn, name, cycle_id)
    messages = [message for _, message in cooldown_messages(conn, snapshot, cfg)] if snapshot else []
    return " ".join(messages) or sync.ASSESSMENT_RUNNING


def announce_cooldowns(conn, client, name, project, plan, snapshot, cfg):
    for row, message in cooldown_messages(conn, snapshot, cfg):
        key = (snapshot['id'], row['agent'], row['until'])
        if runs.assessment_cooldown_announced(conn, *key):
            continue
        digest = hashlib.sha256(json.dumps(key).encode()).hexdigest()
        marker = f"[dd:assessment-cooldown {digest}]"
        if not client.has_comment_with(project['plane_project_id'], plan['issue_id'], marker):
            client.comment(project['plane_project_id'], plan['issue_id'], message, marker=marker)
        if runs.claim_assessment_cooldown_notice(conn, *key):
            alerts.send(cfg, name, plan['issue_id'], message, level='passive')

"""Start ready approved Cycle stories, hold work outside an active Cycle, and remind about Blocked ones."""

from datetime import datetime
import http.client
import json

from . import alerts, choose, cycle_dates, github, mainhealth, plane, release_notes, runs, stories, sync


PLANE_ERRORS = (plane.PlaneError, OSError, ValueError, http.client.HTTPException)


def poll(cfg, conn):
    for name, project in cfg['projects'].items():
        if not project.get('cycles'):
            continue
        client = plane.orchestrator(cfg)
        try:
            cycle_list = client.cycles(project['plane_project_id'])
        except PLANE_ERRORS as exc:
            runs.record_event(conn, None, 'warning', f'Could not list Cycles; retrying next poll: {exc}', project=name)
            continue
        # Reminders carry on while main is red: a Blocked story still needs the approver.
        try:
            remind_blocked(cfg, conn, client, name, project, cycle_list)
        except PLANE_ERRORS as exc:
            runs.record_event(conn, None, 'warning',
                              f'Could not check Blocked Cycle stories; retrying next poll: {exc}', project=name)
        try:
            finish_early(cfg, conn, client, name, project, cycle_list)
        except PLANE_ERRORS + (github.GitHubError,) as exc:
            runs.record_event(conn, None, 'warning', f'Could not end Cycle early; retrying next poll: {exc}',
                              project=name)
        try:
            release_notes.write(cfg, conn, client, name, project, cycle_list)
        except PLANE_ERRORS + (github.GitHubError,) as exc:
            runs.record_event(conn, None, 'warning',
                              f'Could not write draft release notes; retrying next poll: {exc}', project=name)
        if mainhealth.stopped(conn, name):
            continue
        try:
            start_ready(cfg, conn, client, name, project, cycle_list)
        except PLANE_ERRORS as exc:
            runs.record_event(conn, None, 'warning', f'Could not start Cycle stories; retrying next poll: {exc}',
                              project=name)


def start_ready(cfg, conn, client, name, project, cycle_list=None):
    project_id = project['plane_project_id']
    now = datetime.now().astimezone()
    board = None
    for cycle in cycle_list if cycle_list is not None else client.cycles(project_id):
        if (not cycle.get('start_date') or not cycle.get('end_date')
                or not cycle_dates.moment(cycle['start_date']) <= now
                <= cycle_dates.moment(cycle['end_date'], end=True)):
            continue
        plan = runs.cycle_plan(conn, name, cycle['id'])
        if plan is None or plan['status'] != runs.PLAN_APPROVED:
            continue
        approval = runs.plan_approval(conn, name, cycle['id'])
        if approval is None:
            continue
        report = runs.plan_report(conn, approval['snapshot_id'])
        if report is None:
            continue
        merged = json.loads(report['merged'])['merged']
        if merged['loops']:
            continue
        members = {issue['id'] for issue in client.cycle_issues(project_id, cycle['id'])}
        # A failed membership reconciliation must not let an obsolete approval start work.
        if members - runs.plan_issue_ids(conn, name) != set(json.loads(approval['members'])):
            continue
        if board is None:
            board = {issue['id']: issue for issue in client.issues(project_id)}
        start_order(cfg, conn, client, name, project, merged, members, board)


def start_order(cfg, conn, client, name, project, merged, members, board):
    states = project['states']
    def state(issue_id):
        return plane.state_id(board.get(issue_id, {}))

    for issue_id in merged['order']:
        if (issue_id not in members or state(issue_id) != project['cycles']['sprint_backlog_state']
                or runs.is_active(conn, issue_id) or runs.is_plan_item(conn, issue_id)):
            continue
        issue = board[issue_id]
        labels = [label['id'] if isinstance(label, dict) else label for label in issue.get('labels') or []]
        if project.get('human_only_label_id') in labels:
            continue
        story = merged['stories'][issue_id]
        if any(state(dep) != states['done'] for dep in story['depends_on']):
            continue
        if any(state(other) in (states['in_progress'], states['in_review']) for other in story['overlaps']):
            continue
        agent = choose.agent_for(cfg, conn, project, issue_id)
        if agent is None or mainhealth.stopped(conn, name):
            return
        client.set_state(project['plane_project_id'], issue_id, states['in_progress'])
        issue['state'] = states['in_progress']
        runs.record_event(conn, None, 'sprint_start', 'Approved Cycle story moved to In Progress',
                          project=name, issue_id=issue_id)
        # The ordinary queue reserves this account across Cycles and projects. If DD stops
        # between the Plane write and enqueue, normal In Progress polling recovers the story.
        branch = stories.branch_name(project, issue)
        if runs.enqueue(conn, project=name, issue=issue, agent=agent, branch=branch) is None:
            return


def approved_cycles(conn, client, name, project, cycle_list=None):
    """Started Cycles whose plan has been approved, including during a re-plan and after the end date.

    Only these Cycles' members are read; an unapproved past Cycle's are not.
    """
    now = datetime.now().astimezone()
    for cycle in cycle_list if cycle_list is not None else client.cycles(project['plane_project_id']):
        if (not runs.cycle_complete(conn, name, cycle['id'])
                and cycle.get('start_date') and cycle.get('end_date')
                and cycle_dates.moment(cycle['start_date']) <= now
                and runs.plan_approval(conn, name, cycle['id']) is not None):
            yield cycle


def active_cycle(conn, client, name, project, skip=(), cycle_list=None):
    """The approved, started Cycle with a story not yet Done or Cancelled, or None, passing over the Cycle IDs in skip.

    Its 'members' are its stories' IDs and 'open' the stories not yet Done or Cancelled. An overrunning
    Cycle stays active after its end date until its last story is Done or Cancelled.
    """
    if not project.get('cycles'):
        return None
    for cycle in approved_cycles(conn, client, name, project, cycle_list):
        if cycle['id'] in skip:
            continue
        members = {issue['id']: issue for issue in client.cycle_issues(project['plane_project_id'], cycle['id'])
                   if not runs.is_plan_item(conn, issue['id'])}
        unfinished = [issue for issue in members.values() if not finished(project, issue)]
        if unfinished:
            return dict(cycle, members=set(members), open=unfinished)
        if cycle_dates.moment(cycle['end_date'], end=True) < datetime.now().astimezone():
            runs.complete_cycle(conn, name, cycle['id'])
    return None


def held_stories(cfg, conn, name, project, issues):
    """The IDs of In Progress stories that wait for the active Cycle instead of being queued.

    Each held story gets one notice per Cycle and is released once the Cycle is complete.
    Fix main, the Cycle's own stories (rework included) and work DD queued before the
    Cycle was approved are never held. Stories with an active run, and plan items, are the
    queue's to skip, so they are left alone here. With no active Cycle nothing is held.
    """
    if not project.get('cycles'):
        return set()
    fix = mainhealth.fix_story(conn, name)
    candidates = {issue['id'] for issue in issues if issue['id'] != fix and not runs.is_active(conn, issue['id'])
                  and not runs.is_plan_item(conn, issue['id'])}
    if not candidates:
        return set()
    client = plane.orchestrator(cfg)
    try:
        cycle = active_cycle(conn, client, name, project)
        if cycle is None:
            return set()
        held = {issue_id for issue_id in candidates if issue_id not in cycle['members']
                and not runs.started_before_approval(conn, name, cycle['id'], issue_id)}
        for issue_id in sorted(held):
            announce_hold(conn, client, name, project, cycle['id'], issue_id)
        return held
    except PLANE_ERRORS as exc:
        # Without the Cycle's members DD can't tell outside work apart, so nothing new starts this poll.
        runs.record_event(conn, None, 'warning', f'Could not check the active Cycle; retrying next poll: {exc}',
                          project=name)
        return candidates


def announce_hold(conn, client, name, project, cycle_id, issue_id):
    if runs.sprint_hold_announced(conn, name, cycle_id, issue_id):
        return
    # The marker finds a comment whose reply was lost before the store recorded it.
    marker = f'[dd:cycle-hold {cycle_id}]'
    if not client.has_comment_with(project['plane_project_id'], issue_id, marker):
        client.comment(project['plane_project_id'], issue_id, sync.CYCLE_HOLD, marker=marker)
    runs.mark_sprint_hold(conn, name, cycle_id, issue_id)


def remind_blocked(cfg, conn, client, name, project, cycle_list=None):
    """Repeat each Blocked Cycle story's alert every cycles.blocked_reminder_minutes until it leaves Blocked."""
    blocked = set()
    for cycle in approved_cycles(conn, client, name, project, cycle_list):
        members = [issue for issue in client.cycle_issues(project['plane_project_id'], cycle['id'])
                   if not runs.is_plan_item(conn, issue['id'])]
        if (cycle_dates.moment(cycle['end_date'], end=True) < datetime.now().astimezone()
                and all(finished(project, issue) for issue in members)):
            runs.complete_cycle(conn, name, cycle['id'])
        for issue in members:
            if plane.state_id(issue) == project['states']['blocked'] and not runs.is_plan_item(conn, issue['id']):
                blocked.add(issue['id'])
    runs.clear_blocked_reminders(conn, name, blocked)
    for issue_id in sorted(blocked):
        message = runs.blocked_reminder(conn, name, issue_id, project['cycles']['blocked_reminder_minutes'],
                                        sync.CYCLE_BLOCKED)
        if message is not None:
            alerts.blocked(cfg, name, issue_id, message)


def finished(project, issue):
    """Cancellation finishes Cycle work, but only Done satisfies a dependency."""
    return plane.state_id(issue) in {value for key in ('done', 'cancelled')
                                    if (value := project['states'].get(key))}


def finish_early(cfg, conn, client, name, project, cycle_list):
    """Shorten only approved, quiet Cycles on a freshly confirmed green main."""
    now = datetime.now().astimezone()
    pid = project['plane_project_id']
    for cycle in cycle_list:
        saved = runs.early_cycle_end(conn, name, cycle['id'])
        if saved and saved['alerted']:
            continue
        # Recover a successful PATCH whose response or subsequent store write was lost.
        if saved and cycle.get('end_date') and cycle_dates.same_minute(cycle['end_date'], saved['end_date']):
            announce_early(cfg, conn, name, cycle, saved['end_date'])
            continue
        plan = runs.cycle_plan(conn, name, cycle['id'])
        if (not plan or plan['status'] != runs.PLAN_APPROVED
                or not cycle.get('start_date') or not cycle.get('end_date')
                or not cycle_dates.moment(cycle['start_date']) <= now
                <= cycle_dates.moment(cycle['end_date'], end=True)):
            continue
        members = client.cycle_issues(pid, cycle['id'])
        stories = [issue for issue in members if not runs.is_plan_item(conn, issue['id'])]
        # A story removed since approval may be unfinished; the plan's reopening and re-approval decide instead.
        approval = runs.plan_approval(conn, name, cycle['id'])
        if approval is None or sorted(issue['id'] for issue in stories) != json.loads(approval['members']):
            continue
        if not all(finished(project, issue) for issue in stories):
            continue
        if runs.cycle_work_active(conn, name, cycle['id'], [issue['id'] for issue in members]):
            continue
        health = runs.main_health(conn, name)
        if health is None or health['state'] != mainhealth.GREEN or mainhealth.stopped(conn, name):
            continue
        if any((issue['id'] == health['fix_issue_id'] or (issue.get('name') or '').startswith('Fix main:'))
               and client.issue_is_open(pid, issue)
               for issue in client.issues(pid)):
            continue
        # Read after the finished stories: a green result from before the final merge cannot qualify.
        revision = github.main_revision(project['github_repo'], project.get('base_branch', 'main'),
                                        github.token(cfg['github']))
        if health['revision'] != revision:
            continue
        end = cycle_dates.early_end(cycle['start_date'], now)
        if cycle_dates.moment(end) >= cycle_dates.moment(cycle['end_date'], end=True):
            continue
        runs.save_early_cycle_end(conn, name, cycle['id'], end)
        client.set_cycle_end(pid, cycle['id'], end)
        announce_early(cfg, conn, name, cycle, end)


def announce_early(cfg, conn, name, cycle, end):
    if runs.claim_early_cycle_alert(conn, name, cycle['id']):
        alerts.send(cfg, name, None,
                    f"Cycle {cycle.get('name') or cycle['id']} finished early; new end date: {cycle_dates.local_date(end)}.",
                    suffix=' Cycle', level='passive')

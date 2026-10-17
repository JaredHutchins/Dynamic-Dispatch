"""Each Cycle's plan item: create it, post its plan, read the approver's replies there and judge their commands."""

from datetime import datetime
import functools
import json
import re

from . import alerts, assessments, plan_report, plane, runs, sprint, sync
from .text import html_to_text

COMMANDS = ("assess", "approve", "reject")
# Plane writes a mention as an empty element naming the user, in entity_identifier
# on current versions and in id on older ones.
MENTION = re.compile(r"<mention-component\b([^>]*?)(?:/>|>.*?</mention-component>)", re.S | re.I)
SPOT = ""  # private-use character standing in for the mention while the HTML becomes text
# Another Cycle in one of these statuses blocks assess; an approved one only while it has unfinished stories.
BUSY = (runs.PLAN_ASSESSING, runs.PLAN_PLANNED, "approving", "reopening")


def plan_cycles(conn, client, name, project, approver, cycles, listed, stories, cfg=None, cycle_list=None):
    """Give each Cycle with stories one plan item, then act on the approver's replies there."""
    whoami = functools.cache(client.current_user)
    for cycle_id, cycle in cycles.items():
        plan = runs.cycle_plan(conn, name, cycle_id)
        if plan is None:
            if not stories[cycle_id]:
                continue
            plan = create_plan(conn, client, name, project, whoami(), cycle)
            stories[cycle_id] = [issue for issue in stories[cycle_id] if issue["id"] != plan["issue_id"]]
        plan = resume_decision(conn, client, name, project, plan, cycle, stories[cycle_id], whoami)
        stories[cycle_id] = [s for s in stories[cycle_id] if s['id'] != plan['issue_id']]
        if plan['status'] in (runs.PLAN_APPROVED, 'approving'):
            continue
        if plan["status"] == runs.PLAN_NEW:
            plan = introduce_plan(conn, client, name, project, plan, listed[cycle_id])
        read_replies(conn, client, name, project, approver, whoami(), plan, cycles, stories, cycle_list=cycle_list, cfg=cfg)
        plan = runs.cycle_plan(conn, name, cycle_id)
        if plan['status'] == 'approving':
            plan = resume_decision(conn, client, name, project, plan, cycle, stories[cycle_id], whoami)
            if plan['status'] == runs.PLAN_APPROVED:
                continue
        if plan['status'] == runs.PLAN_PLANNED:
            selected = assessments.snapshot_stories(conn, name, project, stories[cycle_id], cycle_id)
            if runs.replan_when_quiet(conn, name, cycle_id, selected, project["cycles"]["replan_quiet_minutes"]):
                runs.record_event(conn, None, "cycle_replan", "The Cycle's stories changed; assessing it again",
                                  project=name, issue_id=plan["issue_id"])
            plan = runs.cycle_plan(conn, name, cycle_id)
        if plan['status'] == runs.PLAN_ASSESSING:
            assessments.start(conn, client, name, project, plan, cycle, stories[cycle_id], cfg)
            post_report(conn, client, name, project, plan, cfg)


def question_marker(cycle_id):
    """The marker on each question DD asks on the plan item; replies are judged against the latest."""
    return f"[dd:plan {cycle_id}]"


def report_marker(snapshot_id):
    """Marks the question asked with a round's report, so a retry never asks it twice."""
    return f"[dd:plan-report {snapshot_id}]"


def reply_marker(comment_id):
    return f"[dd:plan-reply {comment_id}]"


def creator(issue):
    user = issue.get("created_by")
    return user.get("id") if isinstance(user, dict) else user


def create_plan(conn, client, name, project, me, cycle):
    project_id, title = project["plane_project_id"], f"Plan: {cycle.get('name')}"
    # An item made before a lost create response is taken up rather than made twice,
    # wherever it is, since it may never have reached the Cycle.
    taken = runs.plan_issue_ids(conn, name)
    issue = next((issue for issue in client.issues(project_id)
                  if issue.get("name") == title and creator(issue) == me and issue["id"] not in taken), None)
    if issue is None:
        issue = client.create_issue(project_id, title,
                                    "DD's plan item for this Cycle. Reply here to assess, approve or reject it.",
                                    project["cycles"]["sprint_backlog_state"])
    previous = runs.cycle_plan(conn, name, cycle['id'])
    plan = (runs.replace_plan_item(conn, name, cycle['id'], issue['id'])
            if previous and previous['status'] == 'reopening'
            else runs.save_cycle_plan(conn, name, cycle["id"], issue["id"]))
    runs.record_event(conn, None, "cycle_plan", f"Created the plan item for Cycle {cycle.get('name')}",
                      project=name, issue_id=issue["id"])
    return plan


def introduce_plan(conn, client, name, project, plan, members):
    """Put the plan item in its Cycle and ask for stories; each step is retried until it holds."""
    project_id = project["plane_project_id"]
    if plan["issue_id"] not in {issue["id"] for issue in members}:
        client.add_to_cycle(project_id, plan["cycle_id"], plan["issue_id"])
    marker = question_marker(plan["cycle_id"])
    if not client.has_comment_with(project_id, plan["issue_id"], marker):
        # Claimed before posting, so a retry after a lost reply lists the same stories.
        carried = runs.claim_carried_over(conn, name, plan["cycle_id"])
        client.comment(project_id, plan["issue_id"], sync.plan_ask(carried), marker=marker)
    return runs.set_plan_status(conn, name, plan["cycle_id"], runs.PLAN_WAITING)


def make_report(conn, name, snapshot, items, cfg):
    """Merge the round's answers and estimate the fit, then save both so every retry posts the same report."""
    answers = {item["agent"]: json.loads(item["answer"]) for item in items}
    merged = plan_report.merge(json.loads(snapshot["stories"]), answers)
    estimate = plan_report.estimate(merged, runs.done_durations(conn, name, plan_report.HISTORY, with_sizes=True), len(answers),
                                    snapshot["end_date"], datetime.now().astimezone(), snapshot["start_date"])
    names = {agent: cfg.get("agents", {}).get(agent, {}).get("display_name", agent) for agent in answers}
    report = plan_report.render(merged, estimate, names, snapshot["round"])
    return runs.save_plan_report(conn, snapshot["id"], {"merged": merged, "estimate": estimate}, report)


def post_report(conn, client, name, project, plan, cfg):
    """Once every allowed agent has answered the current round, post the plan and ask to begin.

    Each step is reconciled against Plane before writing; the question's
    round marker stops it being asked twice.
    """
    cycle_id, project_id = plan["cycle_id"], project["plane_project_id"]
    snapshot = runs.assessment_snapshot(conn, name, cycle_id)
    if snapshot is None or runs.assessment_requested(conn, name, cycle_id):
        return
    items = runs.assessments(conn, snapshot["id"])
    if not items or not all(item["answer"] for item in items):
        return
    saved = runs.plan_report(conn, snapshot["id"]) or make_report(conn, name, snapshot, items, cfg)
    merged = json.loads(saved["merged"])["merged"]
    loops = bool(merged["loops"])
    current = client.get_issue(project_id, plan['issue_id'])
    expected = saved['report'][:plane.COMMENT_LIMIT] + (
        '\n[... truncated ...]' if len(saved['report']) > plane.COMMENT_LIMIT else '')
    if html_to_text(current.get('description_html')) != expected:
        client.set_description(project_id, plan["issue_id"], saved["report"])
    marker = report_marker(snapshot["id"])
    if not client.has_comment_with(project_id, plan["issue_id"], marker):
        client.comment(project_id, plan["issue_id"], sync.plan_question(loops),
                       marker=f"{question_marker(cycle_id)} {marker}")
    message = sync.plan_ready_alert(loops, len(plan_report.scope_problems(merged)))
    alerts.send(cfg, name, plan["issue_id"], message, suffix=" plan ready", level="active",
                url=plane.work_item_url(cfg, project_id, plan["issue_id"]))
    runs.set_plan_status(conn, name, cycle_id, runs.PLAN_PLANNED)
    runs.record_event(conn, None, "cycle_plan", f"Posted the plan for round {snapshot['round']}",
                      project=name, issue_id=plan["issue_id"])


def command_after_mention(comment_html, user_id):
    """The text after the comment's first mention of the user, or None without one."""
    def spot(match):
        attrs = dict(re.findall(r'([\w-]+)\s*=\s*"([^"]*)"', match.group(1)))
        return SPOT if user_id in (attrs.get("entity_identifier"), attrs.get("id")) else ""
    text = html_to_text(MENTION.sub(spot, comment_html or ""))
    return text.split(SPOT, 1)[1] if SPOT in text else None


def read_replies(conn, client, name, project, approver, me, plan, cycles, stories, cycle_list=None, cfg=None):
    """Act on the approver's commands written since DD's latest question, oldest first, each once."""
    project_id, marker = project["plane_project_id"], question_marker(plan["cycle_id"])
    comments = client.comments(project_id, plan["issue_id"])
    asked = max((c["created_at"] or "" for c in comments if c["author"] == me and marker in c["html"]),
                default=None)
    if asked is None:
        return
    for comment in sorted(comments, key=lambda c: c["created_at"] or ""):
        if comment["author"] != approver or (comment["created_at"] or "") <= asked:
            continue
        text = command_after_mention(comment["html"], me)
        if text is None:
            continue
        record = runs.plan_command(conn, name, comment["id"])
        if record is None:
            record = judge(conn, client, name, project, runs.cycle_plan(conn, name, plan["cycle_id"]),
                           comment["id"], text, cycles, stories, comment, cycle_list=cycle_list, cfg=cfg)
        if runs.cycle_plan(conn, name, plan['cycle_id'])['status'] == 'approving':
            return
        if record["reply"] and not record["replied"]:
            marker = reply_marker(comment["id"])
            if not client.has_comment_with(project_id, plan["issue_id"], marker):
                client.comment(project_id, plan["issue_id"], record["reply"], marker=marker)
            runs.mark_plan_replied(conn, name, comment["id"])


def judge(conn, client, name, project, plan, comment_id, text, cycles, stories, comment=None, cycle_list=None, cfg=None):
    """Decide one command and save it with its reply and any new status."""
    cycle_id = plan["cycle_id"]
    match = re.match(r"\s*([A-Za-z]+)(.*)", text, re.S)
    command = match.group(1).lower() if match else None
    reasons, reply, status, retry_snapshot = None, None, None, None
    if command not in COMMANDS:
        command, reply = "unclear", sync.PLAN_NOT_UNDERSTOOD
    elif command == "assess":
        reply = (assessments.waiting_reply(conn, name, cycle_id, cfg) if runs.assessment_running(conn, name, cycle_id)
                 else assess_refusal(conn, client, name, project, cycle_id, cycles, stories, cycle_list=cycle_list))
        if not reply:
            # Unchanged stories retry only the failed agents of the latest round; anything else starts a new one.
            selected = assessments.snapshot_stories(conn, name, project, stories[cycle_id], cycle_id)
            retry_snapshot = runs.assessment_retry(conn, name, cycle_id, selected)
        status = None if reply else runs.PLAN_ASSESSING
    elif plan["status"] != runs.PLAN_PLANNED:
        reply = sync.PLAN_NOT_POSTED
    elif command == "approve" and plan_has_loops(conn, name, cycle_id):
        reply = sync.PLAN_HAS_LOOPS
    elif command == "reject":
        rest = match.group(2).strip()
        if rest.startswith(":"):
            reasons = rest[1:].strip() or None
        reply = None if reasons else sync.PLAN_REJECT_REASONS
        status = runs.PLAN_ASSESSING if reasons else None
    approval = None
    if command == 'approve' and not reply and comment is not None:
        if runs.latest_plan_report(conn, name, cycle_id) is None:
            reply = sync.PLAN_NOT_POSTED
        else:
            approval = {'comment': json.dumps(comment), 'members': [s['id'] for s in stories[cycle_id]]}
    record = runs.record_plan_command(conn, name, cycle_id, comment_id, command, reasons, reply, status,
                                      retry_snapshot, approval)
    runs.record_event(conn, None, "plan_command", f"Plan reply: {command}" + (" (replied)" if reply else ""),
                      project=name, issue_id=plan["issue_id"])
    return record


def plan_has_loops(conn, name, cycle_id):
    report = runs.latest_plan_report(conn, name, cycle_id)
    return report is not None and bool(json.loads(report["merged"])["merged"]["loops"])


def assess_refusal(conn, client, name, project, cycle_id, cycles, stories, cycle_list=None):
    """Why the Cycle can't be assessed yet, or None when it can.

    Cycles that are only being prepared never block each other; one being
    assessed or planned does, and so does an approved one until it is Done,
    even after its end date.
    """
    busy, blocking = [], {cycle_id}
    for other in stories:
        plan = runs.cycle_plan(conn, name, other)
        status = plan["status"] if plan and other != cycle_id else None
        unfinished = [story_name(issue) for issue in stories[other] if not sprint.finished(project, issue)]
        if status in BUSY or (status == runs.PLAN_APPROVED and unfinished):
            busy.append((cycles[other].get("name"), status, unfinished if status == runs.PLAN_APPROVED else []))
            blocking.add(other)
    # A Cycle past its end date is no longer listed here, but stays active until its last story is Done.
    active = sprint.active_cycle(conn, client, name, project, skip=blocking, cycle_list=cycle_list)
    if active is not None:
        busy.append((active.get("name"), runs.PLAN_APPROVED, [story_name(issue) for issue in active["open"]]))
    if busy:
        return sync.plan_busy_message(busy)
    count, most = len(stories[cycle_id]), project["cycles"]["max_stories"]
    if count > most:
        return sync.plan_too_many_message(count, most)
    return None


def story_name(issue):
    return issue.get("name") or issue["id"]


def finish_approval(conn, client, name, project, plan):
    """Reconcile durable approval effects against Plane after any lost response."""
    approval = runs.plan_approval(conn, name, plan['cycle_id'])
    project_id, cycle_id = project['plane_project_id'], plan['cycle_id']
    report = runs.plan_report(conn, approval['snapshot_id'])
    comment = json.loads(approval['comment'])
    description = report['report'] + '\n\nApproval comment:\n' + html_to_text(comment['html'])
    if not approval['described']:
        remote = client.get_cycle(project_id, cycle_id)
        if remote.get('description') != description[:plane.COMMENT_LIMIT] + (
                '\n[... truncated ...]' if len(description) > plane.COMMENT_LIMIT else ''):
            client.set_cycle_description(project_id, cycle_id, description)
        runs.mark_approval_step(conn, name, cycle_id, 'described')
    if not approval['deleted']:
        delete_plan_item(conn, client, name, project_id, cycle_id, approval['issue_id'])
        runs.mark_approval_step(conn, name, cycle_id, 'deleted')
    runs.set_plan_status(conn, name, cycle_id, runs.PLAN_APPROVED)


def delete_plan_item(conn, client, name, project_id, cycle_id, issue_id):
    runs.guard_plan_deletion(conn, name, cycle_id, issue_id)
    try:
        client.get_issue(project_id, issue_id)
    except plane.PlaneError as exc:
        if exc.status != 404:
            raise
    else:
        try:
            client.delete_issue(project_id, issue_id)
        except plane.PlaneError as exc:
            if exc.status != 404:
                raise


def resume_decision(conn, client, name, project, plan, cycle, stories, whoami):
    cycle_id = cycle['id']
    if plan['status'] == 'approving':
        finish_approval(conn, client, name, project, plan)
        plan = runs.cycle_plan(conn, name, cycle_id)
    if plan['status'] == runs.PLAN_APPROVED:
        approval = runs.plan_approval(conn, name, cycle_id)
        if approval is None or sorted(s['id'] for s in stories) == json.loads(approval['members']):
            return plan
        runs.reopen_approved_plan(conn, name, cycle_id)
        plan = runs.cycle_plan(conn, name, cycle_id)
    if plan['status'] == 'reopening':
        plan = create_plan(conn, client, name, project, whoami(), cycle)
    if runs.plan_approval(conn, name, cycle_id) is not None and plan['status'] == runs.PLAN_ASSESSING:
        if plan['issue_id'] not in {s['id'] for s in client.cycle_issues(project['plane_project_id'], cycle_id)}:
            client.add_to_cycle(project['plane_project_id'], cycle_id, plan['issue_id'])
    return plan

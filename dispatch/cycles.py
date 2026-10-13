"""Keep dated Plane Cycle membership and backlog columns in step, and carry over what a Cycle never started."""

from datetime import datetime
import http.client

from . import cycle_dates, plane, plans, runs, sync

HUMAN_MARKER = "[dd:human-only-cycle]"
PLANE_ERRORS = (plane.PlaneError, OSError, ValueError, http.client.HTTPException)


def poll(cfg, conn):
    for name, project in cfg["projects"].items():
        if not project.get("cycles"):
            continue
        try:
            reconcile(conn, plane.orchestrator(cfg), name, project, cfg["plane"].get("approver_user_id"), cfg)
        except PLANE_ERRORS as exc:
            runs.record_event(conn, None, "warning", f"Could not reconcile Cycles; retrying next poll: {exc}",
                              project=name)


def move(conn, client, name, project_id, issue, target):
    before = plane.state_id(issue)
    client.set_state(project_id, issue["id"], target)
    issue["state"] = target
    runs.record_event(conn, None, "cycle_move", f"Cycle membership moved story from {before} to {target}",
                      project=name, issue_id=issue["id"])


def label_ids(issue):
    return [label["id"] if isinstance(label, dict) else label for label in issue.get("labels", [])]


def reconcile(conn, client, name, project, approver=None, cfg=None):
    project_id, settings = project["plane_project_id"], project["cycles"]
    now = datetime.now().astimezone()
    dated = [cycle for cycle in client.cycles(project_id) if cycle.get("start_date") and cycle.get("end_date")
             and not runs.cycle_complete(conn, name, cycle["id"])]
    try:
        carry_over(conn, client, name, project,
                   [cycle for cycle in dated if cycle_dates.moment(cycle["end_date"], end=True) < now])
    except PLANE_ERRORS as exc:
        runs.record_event(conn, None, "warning", f"Could not carry over Cycle stories; retrying next poll: {exc}",
                          project=name)
    cycles = {cycle["id"]: cycle for cycle in dated if cycle_dates.moment(cycle["end_date"], end=True) >= now}
    # Read eligible memberships before writes: a transfer between Cycles is not a removal.
    listed = {cycle_id: client.cycle_issues(project_id, cycle_id) for cycle_id in cycles}
    # A plan item is never a story, so it is never remembered, moved or counted.
    plan_ids = runs.plan_issue_ids(conn, name)
    memberships = {cycle_id: [issue for issue in issues if issue["id"] not in plan_ids]
                   for cycle_id, issues in listed.items()}
    present = {issue["id"] for issues in memberships.values() for issue in issues}
    running = memberships
    previous = {cycle_id: runs.cycle_members(conn, name, cycle_id) for cycle_id in running}
    # Save the whole observation before writes: failures must not hide later removals.
    for cycle_id, issues in running.items():
        for issue in issues:
            runs.remember_cycle_member(conn, name, cycle_id, issue["id"])
    for cycle_id, issues in running.items():
        for issue in issues:
            reconcile_member(conn, client, name, project, cycle_id, issue)
        for issue_id in previous[cycle_id] - {issue["id"] for issue in issues}:
            if issue_id not in present:
                try:
                    issue = client.get_issue(project_id, issue_id)
                except plane.PlaneError as exc:
                    if exc.status != 404:
                        raise
                else:
                    if plane.state_id(issue) == settings["sprint_backlog_state"]:
                        move(conn, client, name, project_id, issue, settings["product_backlog_state"])
            runs.forget_cycle_member(conn, name, cycle_id, issue_id)
    human = project.get("human_only_label_id")
    stories = {cycle_id: [issue for issue in issues if not human or human not in label_ids(issue)]
               for cycle_id, issues in memberships.items()}
    plans.plan_cycles(conn, client, name, project, approver, cycles, listed, stories, cfg, cycle_list=dated)


def reconcile_member(conn, client, name, project, cycle_id, issue):
    project_id, settings = project["plane_project_id"], project["cycles"]
    labels = label_ids(issue)
    product, sprint = settings["product_backlog_state"], settings["sprint_backlog_state"]
    carried = settings["carried_over_label_id"]
    if carried in labels:
        client.set_labels(project_id, issue["id"], [label for label in labels if label != carried])
    if project.get("human_only_label_id") in labels:
        if not client.has_comment_with(project_id, issue["id"], HUMAN_MARKER):
            client.comment(project_id, issue["id"], "Human-only stories are done outside Cycles.",
                           marker=HUMAN_MARKER)
        # Finish the column write before removal, so a failed write remains retryable.
        if plane.state_id(issue) == sprint:
            move(conn, client, name, project_id, issue, product)
        client.remove_from_cycle(project_id, cycle_id, issue["id"])
        runs.record_event(conn, None, "cycle_remove", "Removed human-only story from Cycle",
                          project=name, issue_id=issue["id"])
        runs.forget_cycle_member(conn, name, cycle_id, issue["id"])
        return
    if plane.state_id(issue) == product:
        move(conn, client, name, project_id, issue, sprint)



def carry_over(conn, client, name, project, ended):
    """Return each ended Cycle's stories still in Sprint Backlog to Product Backlog, marked Carried Over.

    This happens on the first poll after the Cycle's end moment. Only
    Cycles with remembered members are read, and their membership is forgotten once every
    unstarted story is carried over, so an ended Cycle is read only until that is complete.
    """
    project_id, sprint = project["plane_project_id"], project["cycles"]["sprint_backlog_state"]
    plan_ids = runs.plan_issue_ids(conn, name)
    human = project.get("human_only_label_id")
    for cycle in ended:
        if not runs.cycle_members(conn, name, cycle["id"]):
            continue
        members = {issue["id"]: issue for issue in client.cycle_issues(project_id, cycle["id"])
                   if issue["id"] not in plan_ids}
        cycle_name = cycle.get("name") or cycle["id"]
        # Save every unstarted story before writes: one moved but not yet removed is still finished later.
        for issue in members.values():
            if plane.state_id(issue) == sprint and (not human or human not in label_ids(issue)):
                runs.save_carry_over(conn, name, cycle["id"], cycle_name, issue["id"], issue.get("name") or issue["id"])
        for row in runs.unfinished_carry_overs(conn, name, cycle["id"]):
            carry_story(conn, client, name, project, cycle["id"], cycle_name, row["issue_id"],
                        members.get(row["issue_id"]))
        runs.end_cycle_membership(conn, name, cycle["id"])


def carry_story(conn, client, name, project, cycle_id, cycle_name, issue_id, issue):
    """Comment, label, move and finally remove one story; each step is checked first, so a retry never repeats one.

    Removal comes last, so a story no longer in the Cycle has had every step.
    """
    project_id, settings = project["plane_project_id"], project["cycles"]
    if issue is None:
        runs.finish_carry_over(conn, name, cycle_id, issue_id)
        return
    product, sprint = settings["product_backlog_state"], settings["sprint_backlog_state"]
    if plane.state_id(issue) not in (product, sprint):
        # Someone started it before DD finished: it stays in the Cycle and finishes normally.
        runs.drop_carry_over(conn, name, cycle_id, issue_id)
        return
    marker = f"[dd:carried-over {cycle_id}]"
    if not client.has_comment_with(project_id, issue_id, marker):
        client.comment(project_id, issue_id, sync.carried_over_message(cycle_name), marker=marker)
    labels, carried = label_ids(issue), settings["carried_over_label_id"]
    if carried not in labels:
        client.set_labels(project_id, issue_id, labels + [carried])
    if plane.state_id(issue) == sprint:
        move(conn, client, name, project_id, issue, product)
    try:
        client.remove_from_cycle(project_id, cycle_id, issue_id)
    except plane.PlaneError as exc:
        if exc.status != 404:
            raise
    runs.record_event(conn, None, "cycle_carry_over", f"Carried over from Cycle {cycle_name}",
                      project=name, issue_id=issue_id)
    runs.finish_carry_over(conn, name, cycle_id, issue_id)

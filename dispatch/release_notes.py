"""Draft release notes, written once below the plan in a Cycle's description when the Cycle ends.

Everything in them is copied from DD's records and the pull request summaries
DD wrote; nothing is reworded, so the approver checks them before publishing.
"""

import json
import re

from . import alerts, docs_line, github, mainhealth, plane, runs

HEADING = "Draft release notes"
INTRO = "Assembled by DD from its records and the pull request summaries. Check them before publishing."


def write(cfg, conn, client, name, project, cycle_list):
    """Write each officially ended Cycle's notes once, then send one silent alert.

    A Cycle has ended once DD has recorded it complete (its end date has passed and every
    story is Done or Cancelled), after DD ended it early or once main has passed its check
    at the current main revision. A refused write raises, so the caller warns and retries.
    """
    for cycle in cycle_list:
        if runs.release_notes_written(conn, name, cycle['id']):
            announce(cfg, conn, name, cycle)
            continue
        if not runs.cycle_complete(conn, name, cycle['id']):
            continue
        early = runs.early_cycle_end(conn, name, cycle['id'])
        if not (early and early['alerted']) and not main_passed(cfg, conn, name, project):
            continue
        pid = project['plane_project_id']
        members = [issue for issue in client.cycle_issues(pid, cycle['id']) if not runs.is_plan_item(conn, issue['id'])]
        text = notes(conn, name, project, cycle['id'], members)
        description = client.get_cycle(pid, cycle['id']).get('description') or ''
        # A write whose reply was lost already shows the heading, so it isn't added twice.
        if HEADING not in description.splitlines():
            client.set_cycle_description(pid, cycle['id'], f"{description.rstrip()}\n\n{text}".lstrip())
        runs.save_release_notes(conn, name, cycle['id'])
        announce(cfg, conn, name, cycle)


def main_passed(cfg, conn, name, project):
    """Whether main's latest check passed at the current main revision."""
    health = runs.main_health(conn, name)
    if health is None or health['state'] != mainhealth.GREEN or mainhealth.stopped(conn, name):
        return False
    return health['revision'] == github.main_revision(project['github_repo'], project.get('base_branch', 'main'),
                                                      github.token(cfg['github']))


def announce(cfg, conn, name, cycle):
    if runs.claim_release_notes_alert(conn, name, cycle['id']):
        alerts.send(cfg, name, None,
                    f"Cycle {cycle.get('name') or cycle['id']} has ended; its draft release notes are ready "
                    "in the Cycle's description.", suffix=' Cycle', level='passive')


def notes(conn, name, project, cycle_id, members):
    """The notes as plain text: Done stories in build order, then Cancelled and carried-over titles."""
    approval = runs.plan_approval(conn, name, cycle_id)
    report = runs.plan_report(conn, approval['snapshot_id']) if approval else None
    order = json.loads(report['merged'])['merged']['order'] if report else []
    # Stories missing from the approved order keep the Cycle's listing order after the rest.
    members = sorted(members, key=lambda issue: order.index(issue['id']) if issue['id'] in order else len(order))
    states = project['states']
    done = [issue for issue in members if plane.state_id(issue) == states['done']]
    cancelled = [issue for issue in members if states.get('cancelled')
                 and plane.state_id(issue) == states['cancelled']]
    carried = [row['issue_name'] for row in runs.carried_over(conn, name, cycle_id)]
    lines = [HEADING, INTRO, "", "Done, in build order:"]
    for number, issue in enumerate(done, 1):
        lines += [""] + done_entry(conn, name, number, issue)
    if not done:
        lines.append("(none)")
    lines += ["", "Cancelled:"] + titled([title(issue) for issue in cancelled])
    lines += ["", "Carried over:"] + titled(carried)
    return "\n".join(lines)


def done_entry(conn, name, number, issue):
    run = runs.final_pull_request(conn, name, issue['id'])
    summary = run['summary'] if run else None
    line = (run['docs_line'] if run else None) or docs_line.last_line(summary)
    paragraph = first_paragraph(summary, line, runs.combine_notes(conn, issue['id']))
    entry = [f"{number}. {title(issue)}",
             f"Pull request: {run['pr_url'] if run else '(none recorded)'}",
             *(paragraph or "(no summary recorded)").splitlines(),
             line or "Docs: (no Docs line recorded)"]
    return [entry[0]] + ["   " + text for text in entry[1:]]


def first_paragraph(summary, line, skip):
    """The summary's first paragraph, passing over DD's own combine notes, headings and the Docs line."""
    for paragraph in re.split(r"\n\s*\n", summary or ""):
        kept = [text.rstrip() for text in paragraph.strip().splitlines()
                if not text.lstrip().startswith("#") and text.strip() != line]
        if kept and "\n".join(kept) not in skip:
            return "\n".join(kept)
    return None


def titled(names):
    return [f"- {n}" for n in names] or ["(none)"]


def title(issue):
    return issue.get("name") or issue["id"]

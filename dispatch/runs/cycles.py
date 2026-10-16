"""Durable Cycle membership used to reconcile board columns, end-of-Cycle carry-overs, each Cycle's plan item
and whether its draft release notes are written."""

import json

from ..redact import scrub
from .store import ACTIVE, now
from .plan_decisions import save_plan_approval

# A plan item's status. New until it is in its Cycle and has asked for stories.
PLAN_NEW = "new"
PLAN_WAITING = "waiting"
PLAN_ASSESSING = "assessing"
PLAN_PLANNED = "planned"
# Approval completes after the report is copied and the recorded item is deleted.
PLAN_APPROVED = "approved"
PLAN_COMPLETE = "complete"


def cycle_members(conn, project, cycle_id):
    return {row["issue_id"] for row in conn.execute(
        "SELECT issue_id FROM cycle_members WHERE project = ? AND cycle_id = ?", (project, cycle_id))}


def remember_cycle_member(conn, project, cycle_id, issue_id):
    with conn:
        conn.execute("INSERT OR IGNORE INTO cycle_members VALUES (?, ?, ?)", (project, cycle_id, issue_id))


def forget_cycle_member(conn, project, cycle_id, issue_id):
    with conn:
        conn.execute("DELETE FROM cycle_members WHERE project = ? AND cycle_id = ? AND issue_id = ?",
                     (project, cycle_id, issue_id))


def end_cycle_membership(conn, project, cycle_id):
    """Forget remembered membership once carry-over or the Cycle itself is complete."""
    with conn:
        conn.execute("DELETE FROM cycle_members WHERE project = ? AND cycle_id = ?", (project, cycle_id))


def save_carry_over(conn, project, cycle_id, cycle_name, issue_id, issue_name):
    with conn:
        conn.execute("INSERT OR IGNORE INTO carry_overs VALUES (?, ?, ?, ?, ?, 0, NULL)",
                     (project, cycle_id, issue_id, cycle_name, issue_name))


def unfinished_carry_overs(conn, project, cycle_id):
    return conn.execute("SELECT * FROM carry_overs WHERE project = ? AND cycle_id = ? AND finished = 0 "
                        "ORDER BY rowid", (project, cycle_id)).fetchall()


def finish_carry_over(conn, project, cycle_id, issue_id):
    with conn:
        conn.execute("UPDATE carry_overs SET finished = 1 WHERE project = ? AND cycle_id = ? AND issue_id = ?",
                     (project, cycle_id, issue_id))


def drop_carry_over(conn, project, cycle_id, issue_id):
    """Forget a story someone started before DD finished carrying it over; it stays in its Cycle."""
    with conn:
        conn.execute("DELETE FROM carry_overs WHERE project = ? AND cycle_id = ? AND issue_id = ?",
                     (project, cycle_id, issue_id))


def carried_over(conn, project, cycle_id):
    """Every story this Cycle carried over, in the order they were saved."""
    return conn.execute("SELECT * FROM carry_overs WHERE project = ? AND cycle_id = ? ORDER BY rowid",
                        (project, cycle_id)).fetchall()


def claim_carried_over(conn, project, cycle_id):
    """The carried-over stories to list on this Cycle's plan item, claiming any no Cycle has listed yet.

    Each is listed on one later Cycle's plan item; a retry of that comment lists the same ones again.
    """
    with conn:
        conn.execute("UPDATE carry_overs SET listed_in = ? WHERE project = ? AND listed_in IS NULL "
                     "AND cycle_id != ?", (cycle_id, project, cycle_id))
    return conn.execute("SELECT * FROM carry_overs WHERE project = ? AND listed_in = ? ORDER BY rowid",
                        (project, cycle_id)).fetchall()


def cycle_plan(conn, project, cycle_id):
    return conn.execute("SELECT * FROM cycle_plans WHERE project = ? AND cycle_id = ?",
                        (project, cycle_id)).fetchone()


def plan_issue_ids(conn, project):
    return {row["issue_id"] for row in conn.execute(
        "SELECT issue_id FROM cycle_plans WHERE project = ? UNION "
        "SELECT issue_id FROM plan_approvals WHERE project = ?", (project, project))}


def is_plan_item(conn, issue_id):
    return conn.execute("SELECT 1 FROM cycle_plans WHERE issue_id = ? UNION "
                        "SELECT 1 FROM plan_approvals WHERE issue_id = ?", (issue_id, issue_id)).fetchone() is not None


def save_cycle_plan(conn, project, cycle_id, issue_id):
    """Save the Cycle's plan item; an item taken up after a lost reply stops being a remembered member."""
    with conn:
        conn.execute("DELETE FROM cycle_members WHERE project = ? AND issue_id = ?", (project, issue_id))
        conn.execute("INSERT OR IGNORE INTO cycle_plans VALUES (?, ?, ?, ?)",
                     (project, cycle_id, issue_id, PLAN_NEW))
    return cycle_plan(conn, project, cycle_id)


def set_plan_status(conn, project, cycle_id, status):
    with conn:
        conn.execute("UPDATE cycle_plans SET status = ? WHERE project = ? AND cycle_id = ?",
                     (status, project, cycle_id))
    return cycle_plan(conn, project, cycle_id)


def plan_command(conn, project, comment_id):
    return conn.execute("SELECT * FROM plan_commands WHERE project = ? AND comment_id = ?",
                        (project, comment_id)).fetchone()


def record_plan_command(conn, project, cycle_id, comment_id, command, reasons=None, reply=None, status=None,
                        retry_snapshot=None, approval=None):
    """Save a command, the reply it is owed and any status it sets, together, so none repeats or is lost.

    An accepted assess either retries the failed agents of retry_snapshot's round or asks for a new round.
    """
    with conn:
        inserted = conn.execute("INSERT OR IGNORE INTO plan_commands VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
                     (project, comment_id, cycle_id, command, scrub(reasons) if reasons else None, reply, now())).rowcount
        if not inserted:
            return plan_command(conn, project, comment_id)
        if command == "reject" and status == PLAN_ASSESSING:
            conn.execute("INSERT OR REPLACE INTO assessment_feedback VALUES (?, ?, ?)",
                         (project, cycle_id, scrub(reasons)))
        if status == PLAN_ASSESSING:
            if retry_snapshot is not None:
                conn.execute("UPDATE assessments SET failures = 0, alerted = 0 WHERE answer IS NULL "
                             "AND failures >= 2 AND snapshot_id = ?", (retry_snapshot,))
            else:
                conn.execute("INSERT OR IGNORE INTO assessment_requests VALUES (?, ?)", (project, cycle_id))
        if status:
            conn.execute("UPDATE cycle_plans SET status = ? WHERE project = ? AND cycle_id = ?",
                         (status, project, cycle_id))
        if approval is not None:
            save_plan_approval(conn, project, cycle_id, approval['comment'], approval['members'])
    return plan_command(conn, project, comment_id)


def mark_plan_replied(conn, project, comment_id):
    with conn:
        conn.execute("UPDATE plan_commands SET replied = 1 WHERE project = ? AND comment_id = ?",
                     (project, comment_id))


def cycle_complete(conn, project, cycle_id):
    plan = cycle_plan(conn, project, cycle_id)
    return plan is not None and plan["status"] == PLAN_COMPLETE


def complete_cycle(conn, project, cycle_id):
    """Persist terminal completion only after approval and any pending carry-over writes."""
    plan = cycle_plan(conn, project, cycle_id)
    if plan is not None and plan["status"] == PLAN_APPROVED and not unfinished_carry_overs(conn, project, cycle_id):
        set_plan_status(conn, project, cycle_id, PLAN_COMPLETE)
        end_cycle_membership(conn, project, cycle_id)


def early_cycle_end(conn, project, cycle_id):
    return conn.execute("SELECT * FROM early_cycle_ends WHERE project = ? AND cycle_id = ?",
                        (project, cycle_id)).fetchone()


def save_early_cycle_end(conn, project, cycle_id, end_date):
    with conn:
        conn.execute("INSERT INTO early_cycle_ends (project, cycle_id, end_date) VALUES (?, ?, ?) "
                     "ON CONFLICT(project, cycle_id) DO UPDATE SET end_date = excluded.end_date",
                     (project, cycle_id, end_date))


def claim_early_cycle_alert(conn, project, cycle_id):
    with conn:
        return conn.execute("UPDATE early_cycle_ends SET alerted = 1 WHERE project = ? AND cycle_id = ? "
                            "AND alerted = 0", (project, cycle_id)).rowcount == 1


def release_notes_written(conn, project, cycle_id):
    return conn.execute("SELECT 1 FROM release_notes WHERE project = ? AND cycle_id = ?",
                        (project, cycle_id)).fetchone() is not None


def save_release_notes(conn, project, cycle_id):
    with conn:
        conn.execute("INSERT OR IGNORE INTO release_notes (project, cycle_id) VALUES (?, ?)", (project, cycle_id))


def claim_release_notes_alert(conn, project, cycle_id):
    with conn:
        return conn.execute("UPDATE release_notes SET alerted = 1 WHERE project = ? AND cycle_id = ? "
                            "AND alerted = 0", (project, cycle_id)).rowcount == 1


def cycle_work_active(conn, project, cycle_id, members):
    """Include active outcomes, assessments and workers still holding their slot during cleanup."""
    ids = set(members) | cycle_members(conn, project, cycle_id)
    approvals = conn.execute("SELECT members FROM plan_approvals WHERE project = ? AND cycle_id = ?",
                             (project, cycle_id))
    for approval in approvals:
        ids.update(json.loads(approval['members']))
    rows = conn.execute("SELECT r.issue_id, r.status, s.run_id, a.cycle_id FROM runs r "
                        "LEFT JOIN agent_slots s ON s.run_id = r.id "
                        "LEFT JOIN assessments x ON x.run_id = r.id "
                        "LEFT JOIN assessment_snapshots a ON a.id = x.snapshot_id WHERE r.project = ?", (project,))
    return any((row['issue_id'] in ids or row['cycle_id'] == cycle_id)
               and (row['status'] in ACTIVE or row['run_id'] is not None) for row in rows)

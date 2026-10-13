"""Durable approval effects and membership changes after approval."""

import json

from ..redact import scrub
from .store import now


def plan_approval(conn, project, cycle):
    return conn.execute("SELECT * FROM plan_approvals WHERE project = ? AND cycle_id = ? "
                        "ORDER BY snapshot_id DESC LIMIT 1", (project, cycle)).fetchone()


def save_plan_approval(conn, project, cycle, comment, members):
    report = conn.execute("SELECT * FROM plan_reports WHERE project = ? AND cycle_id = ? "
                          "ORDER BY snapshot_id DESC LIMIT 1", (project, cycle)).fetchone()
    plan = conn.execute("SELECT * FROM cycle_plans WHERE project = ? AND cycle_id = ?",
                        (project, cycle)).fetchone()
    saved_members = conn.execute("SELECT members FROM snapshot_members WHERE snapshot_id = ?",
                                 (report['snapshot_id'],)).fetchone()
    with conn:
        conn.execute("INSERT OR IGNORE INTO plan_approvals (project, cycle_id, snapshot_id, issue_id, comment, "
                     "members, approved_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (project, cycle, report['snapshot_id'], plan['issue_id'], scrub(comment),
                      saved_members['members'] if saved_members else json.dumps(sorted(members)), now()))
        conn.execute("UPDATE cycle_plans SET status = 'approving' WHERE project = ? AND cycle_id = ?",
                     (project, cycle))
    return plan_approval(conn, project, cycle)


def mark_approval_step(conn, project, cycle, step):
    if step not in ('described', 'deleted'):
        raise ValueError('Unknown approval step')
    approval = plan_approval(conn, project, cycle)
    with conn:
        conn.execute(f"UPDATE plan_approvals SET {step} = 1 WHERE project = ? AND cycle_id = ? AND snapshot_id = ?",
                     (project, cycle, approval['snapshot_id']))


def reopen_approved_plan(conn, project, cycle):
    """Retain approval history while reserving a new plan item and a fresh round."""
    with conn:
        conn.execute("UPDATE cycle_plans SET status = 'reopening' WHERE project = ? AND cycle_id = ?",
                     (project, cycle))
        conn.execute("INSERT OR IGNORE INTO assessment_requests VALUES (?, ?)", (project, cycle))


def replace_plan_item(conn, project, cycle, issue_id):
    with conn:
        conn.execute("UPDATE cycle_plans SET issue_id = ?, status = 'assessing' "
                     "WHERE project = ? AND cycle_id = ? AND status = 'reopening'", (issue_id, project, cycle))
        conn.execute("DELETE FROM cycle_members WHERE project = ? AND issue_id = ?", (project, issue_id))
    return conn.execute("SELECT * FROM cycle_plans WHERE project = ? AND cycle_id = ?",
                        (project, cycle)).fetchone()


def guard_plan_deletion(conn, project, cycle, issue_id):
    plan = conn.execute("SELECT * FROM cycle_plans WHERE project = ? AND cycle_id = ?",
                        (project, cycle)).fetchone()
    approval = plan_approval(conn, project, cycle)
    if (plan is None or approval is None or plan['issue_id'] != issue_id
            or approval['issue_id'] != issue_id or plan['status'] != 'approving'):
        raise ValueError('Refusing to delete an item other than this Cycle\'s recorded plan item')


def cycle_start_allowed_sql(alias):
    """Shared queue predicate: reassessment holds new starts, never existing work or plan runs."""
    if alias not in ('runs', 'r', 'q'):
        raise ValueError('Unknown run alias')
    return f"""({alias}.kind != 'story'
        OR EXISTS (SELECT 1 FROM runs started WHERE started.project = {alias}.project
                   AND started.issue_id = {alias}.issue_id AND started.claimed_at IS NOT NULL)
        OR NOT EXISTS (SELECT 1 FROM cycle_members m JOIN cycle_plans p
                       ON p.project = m.project AND p.cycle_id = m.cycle_id
                       WHERE m.project = {alias}.project AND m.issue_id = {alias}.issue_id
                       AND p.status != 'approved' AND EXISTS
                           (SELECT 1 FROM plan_approvals a WHERE a.project = p.project AND a.cycle_id = p.cycle_id)))"""

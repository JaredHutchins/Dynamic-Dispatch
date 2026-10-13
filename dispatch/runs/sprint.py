"""Durable Cycle hold notices and Blocked reminder clocks."""

from datetime import datetime

from .store import now


def started_before_approval(conn, project, cycle, issue_id):
    """Whether DD queued or began waiting for this story before the Cycle's first approval.

    A later re-approval must not let more outside work through, so the first approval counts.
    """
    approved = conn.execute("SELECT MIN(approved_at) FROM plan_approvals WHERE project = ? AND cycle_id = ?",
                            (project, cycle)).fetchone()[0]
    if approved is None:
        return False
    # Wait times carry fractions of a second and approval times don't, so compare whole UTC seconds.
    return conn.execute("SELECT 1 FROM runs WHERE project = ? AND issue_id = ? "
                        "AND datetime(queued_at) <= datetime(?) "
                        "UNION SELECT 1 FROM waits WHERE project = ? AND issue_id = ? "
                        "AND datetime(since) <= datetime(?)",
                        (project, issue_id, approved, project, issue_id, approved)).fetchone() is not None


def sprint_hold_announced(conn, project, cycle, issue_id):
    return conn.execute("SELECT 1 FROM sprint_holds WHERE project = ? AND cycle_id = ? AND issue_id = ?",
                        (project, cycle, issue_id)).fetchone() is not None


def mark_sprint_hold(conn, project, cycle, issue_id):
    with conn:
        conn.execute("INSERT OR IGNORE INTO sprint_holds VALUES (?, ?, ?)", (project, cycle, issue_id))


def mark_blocked_alert(conn, project, issue_id, message):
    """Record a Blocked alert just sent, restarting the story's reminder clock."""
    with conn:
        conn.execute("INSERT OR REPLACE INTO blocked_reminders VALUES (?, ?, ?, ?)",
                     (project, issue_id, now(), message))


def blocked_reminder(conn, project, issue_id, minutes, first_seen):
    """The Blocked alert to repeat now, or None. A story seen Blocked for the first time starts its clock."""
    row = conn.execute("SELECT * FROM blocked_reminders WHERE project = ? AND issue_id = ?",
                       (project, issue_id)).fetchone()
    if row is None:
        mark_blocked_alert(conn, project, issue_id, first_seen)
        return None
    if (datetime.fromisoformat(now()) - datetime.fromisoformat(row["last_sent"])).total_seconds() < minutes * 60:
        return None
    mark_blocked_alert(conn, project, issue_id, row["message"])
    return row["message"]


def clear_blocked_reminders(conn, project, blocked):
    """Forget the clocks of stories no longer Blocked, so a later Blocked spell starts afresh."""
    with conn:
        for row in conn.execute("SELECT issue_id FROM blocked_reminders WHERE project = ?", (project,)).fetchall():
            if row["issue_id"] not in blocked:
                conn.execute("DELETE FROM blocked_reminders WHERE project = ? AND issue_id = ?",
                             (project, row["issue_id"]))

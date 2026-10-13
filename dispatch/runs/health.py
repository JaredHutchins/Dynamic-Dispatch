"""Main's health, the main checks that test it, and runs held while it is red."""

import json
import sqlite3
import uuid
from datetime import datetime, timedelta

from ..redact import scrub
from .store import HELD, MAIN_CHECK, QUEUED, now
from .timeline import record_event


def main_check_id(project):
    """The story ID a project's main checks are queued under, so it has one at a time."""
    return f"main-check:{project}"


def enqueue_main_check(conn, *, project, agent, base, revision):
    """Queue a main check of this commit in the agent's account. None if one is already active."""
    try:
        with conn:
            run_id = conn.execute(
                """INSERT INTO runs (project, issue_id, issue_key, issue_name, agent, branch, status,
                                     queued_at, kind, revision)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (project, main_check_id(project), "main", f"Main check of {revision[:12]}", agent, base,
                 QUEUED, now(), MAIN_CHECK, revision),
            ).lastrowid
        record_event(conn, run_id, "queued", f"Main check queued for {revision[:12]}")
        return run_id
    except sqlite3.IntegrityError:
        return None


def main_health(conn, project):
    """Main's last checked state for the project, or None before its first check."""
    return conn.execute("SELECT * FROM main_health WHERE project = ?", (project,)).fetchone()


def save_main_health(conn, project, *, revision, state, since, failing, broken_by, alert_owed, failure_output="",
                     breaking_revision=None, pr_links=None):
    """failing is a list of test names. alert_owed marks a change of state not yet alerted.

    Turning red starts a new episode: the previous fix story is forgotten, so the
    episode gets its own. breaking_revision and pr_links are kept unless given.
    """
    with conn:
        previous = main_health(conn, project)
        conn.execute(
            "INSERT INTO main_health (project, revision, state, since, failing, broken_by, alert_owed) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(project) DO UPDATE SET revision = excluded.revision, state = excluded.state, "
            "since = excluded.since, failing = excluded.failing, broken_by = excluded.broken_by, "
            "alert_owed = excluded.alert_owed",
            (project, revision, state, since, scrub(json.dumps(failing)), scrub(broken_by), int(alert_owed)))
        if state == "red" and (previous is None or previous["state"] != "red"):
            conn.execute("UPDATE main_health SET fix_issue_id = NULL, fix_revision = NULL, fix_alerted = 0, "
                         "fix_episode = ?, breaking_revision = NULL, pr_links = NULL WHERE project = ?",
                         (uuid.uuid4().hex, project))
        conn.execute("UPDATE main_health SET failure_output = ?, breaking_revision = COALESCE(?, breaking_revision), "
                     "pr_links = COALESCE(?, pr_links) WHERE project = ?",
                     (scrub(failure_output), breaking_revision, scrub(pr_links), project))


def claim_main_alert(conn, project):
    """Take the owed alert for main's state, so only one caller sends it. None if none is owed."""
    with conn:
        return conn.execute("UPDATE main_health SET alert_owed = 0 WHERE project = ? AND alert_owed = 1 "
                            "RETURNING *", (project,)).fetchone()


def owe_main_alert(conn, project, state):
    """The claimed alert for state couldn't be sent; the next check tries again.

    If main has changed state since, the newer change owes its own alert and
    the stale one is dropped.
    """
    with conn:
        conn.execute("UPDATE main_health SET alert_owed = 1 WHERE project = ? AND state = ?", (project, state))


def save_main_fix(conn, project, **fields):
    """Persist the fix story and its delivery markers alongside main's health."""
    allowed = {"fix_issue_id", "fix_revision", "fix_alerted"}
    if not fields or not fields.keys() <= allowed:
        raise ValueError("Unknown main fix fields")
    with conn:
        conn.execute("UPDATE main_health SET " + ", ".join(f"{key} = ?" for key in fields) +
                     " WHERE project = ?", tuple(scrub(v) if isinstance(v, str) else v
                                                for v in fields.values()) + (project,))


def main_check_failures(conn, project):
    return conn.execute("SELECT * FROM main_check_failures WHERE project = ?", (project,)).fetchone()


def main_check_due(conn, project):
    failure = main_check_failures(conn, project)
    return failure is None or failure["retry_at"] is None or now() >= failure["retry_at"]


def main_check_without_result(conn, project, reason):
    """Count one check, independently of main's last known test result."""
    retry_at = (datetime.fromisoformat(now()) + timedelta(minutes=10)).isoformat()
    with conn:
        conn.execute("INSERT OR IGNORE INTO main_check_failures (project) VALUES (?)", (project,))
        conn.execute(
            "UPDATE main_check_failures SET count = count + 1, reason = ?, "
            "retry_at = CASE WHEN count >= 2 THEN ? ELSE NULL END, "
            "generation = generation + CASE WHEN count = 2 THEN 1 ELSE 0 END, "
            "alert_owed = CASE WHEN count = 2 THEN 1 ELSE alert_owed END "
            "WHERE project = ?", (scrub(reason), retry_at, project))


def main_check_result(conn, project):
    """A green or red result ends the outage and owes recovery if it was announced."""
    with conn:
        conn.execute(
            "UPDATE main_check_failures SET count = 0, retry_at = NULL, "
            "generation = generation + CASE WHEN count >= 3 THEN 1 ELSE 0 END, "
            "alert_owed = CASE WHEN count >= 3 THEN announced ELSE alert_owed END, announced = 0 "
            "WHERE project = ? AND count > 0", (project,))


def claim_main_check_alert(conn, project):
    with conn:
        return conn.execute(
            "UPDATE main_check_failures SET alert_owed = 0, "
            "announced = CASE WHEN count >= 3 THEN 1 ELSE announced END "
            "WHERE project = ? AND alert_owed = 1 RETURNING *", (project,)).fetchone()


def owe_main_check_alert(conn, project, generation):
    # A late delivery failure must not resurrect an alert from an earlier outage.
    with conn:
        conn.execute("UPDATE main_check_failures SET alert_owed = 1 "
                     "WHERE project = ? AND generation = ?", (project, generation))


def main_is_red(conn, project, issue_id=None):
    health = main_health(conn, project)
    return (health is not None and health["state"] == "red"
            and (issue_id is None or issue_id != health["fix_issue_id"]))


def hold_for_main(conn, run_id, worktree, result):
    """Keep the finished agent's output durable before trying to save its files."""
    with conn:
        conn.execute("UPDATE runs SET status = ?, worktree = ?, hold_data = ? WHERE id = ?",
                     (HELD, str(worktree), scrub(json.dumps(result)), run_id))


def held_for_main(conn, project):
    return conn.execute("SELECT * FROM runs WHERE project = ? AND status = ? ORDER BY id",
                        (project, HELD)).fetchall()


def mark_hold_announced(conn, run_id):
    with conn:
        conn.execute("UPDATE runs SET hold_announced = 1 WHERE id = ?", (run_id,))


def resume_main_hold(conn, run_id):
    with conn:
        conn.execute("UPDATE runs SET status = ? WHERE id = ? AND status = ? "
                     "AND NOT EXISTS (SELECT 1 FROM main_health h WHERE h.project = runs.project "
                     "AND h.state = 'red' AND (h.fix_issue_id IS NULL OR h.fix_issue_id != runs.issue_id)) AND NOT EXISTS "
                     "(SELECT 1 FROM agent_slots s WHERE s.run_id = runs.id)", (QUEUED, run_id, HELD))

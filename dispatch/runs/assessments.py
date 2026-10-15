"""Durable independent assessments and their immutable shared inputs."""

import hashlib
import json

from ..redact import scrub
from .store import now


def assessment_snapshot(conn, project, cycle):
    return conn.execute("SELECT * FROM assessment_snapshots WHERE project = ? AND cycle_id = ? "
                        "ORDER BY id DESC LIMIT 1", (project, cycle)).fetchone()


def save_assessment_snapshot(conn, project, cycle, issue_id, revision, stories, end_date, agents,
                             members=None, start_date=None):
    with conn:
        current = assessment_snapshot(conn, project, cycle)
        if current is not None and not assessment_requested(conn, project, cycle):
            return current
        cur = conn.execute("INSERT INTO assessment_snapshots "
                           "(project, cycle_id, issue_id, revision, stories, end_date, round, start_date) "
                           "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                           (project, cycle, issue_id, revision, scrub(json.dumps(stories)), end_date,
                            current["round"] + 1 if current else 1, start_date))
        conn.execute("INSERT INTO snapshot_members VALUES (?, ?)",
                     (cur.lastrowid, json.dumps(sorted(members if members is not None else [s['id'] for s in stories]))))
        conn.execute("INSERT INTO snapshot_feedback SELECT ?, reasons FROM assessment_feedback "
                     "WHERE project = ? AND cycle_id = ?", (cur.lastrowid, project, cycle))
        conn.execute("DELETE FROM assessment_requests WHERE project = ? AND cycle_id = ?", (project, cycle))
        for agent in agents:
            conn.execute("INSERT INTO assessments (snapshot_id, agent) VALUES (?, ?)", (cur.lastrowid, agent))
    return assessment_snapshot(conn, project, cycle)


def assessments(conn, snapshot_id):
    return conn.execute("SELECT * FROM assessments WHERE snapshot_id = ? ORDER BY agent", (snapshot_id,)).fetchall()


def assessment_for_run(conn, run_id):
    return conn.execute("SELECT a.*, s.project, s.cycle_id, s.issue_id, s.revision, s.stories, s.end_date, "
                        "(SELECT reasons FROM snapshot_feedback WHERE snapshot_id = s.id) AS reasons "
                        "FROM assessments a JOIN assessment_snapshots s ON s.id = a.snapshot_id "
                        "WHERE a.run_id = ?", (run_id,)).fetchone()


def running_assessments(conn):
    """Snapshot ID and agent of every assessment whose run is running or whose worker still holds its slot.

    A worker finishes the assessment before it discards its folder, and releases
    the slot only after that.
    """
    return conn.execute("SELECT a.snapshot_id, a.agent FROM assessments a JOIN runs r ON r.id = a.run_id "
                        "WHERE r.status = 'running' OR r.id IN (SELECT run_id FROM agent_slots)").fetchall()


def queue_assessments(conn, snapshot_id):
    """Queue missing work atomically; interrupted workers retry the same immutable input."""
    with conn:
        snapshot = conn.execute("SELECT * FROM assessment_snapshots WHERE id = ?", (snapshot_id,)).fetchone()
        for item in assessments(conn, snapshot_id):
            if item['answer'] or item['failures'] >= 2:
                continue
            previous = conn.execute("SELECT status FROM runs WHERE id = ?", (item['run_id'],)).fetchone()
            if previous and previous['status'] in ('queued', 'running'):
                continue
            issue_id = f"plan:{snapshot_id}:{item['agent']}"
            cur = conn.execute("INSERT INTO runs (project, issue_id, issue_key, issue_name, agent, branch, "
                               "status, queued_at, kind) VALUES (?, ?, '', 'Cycle assessment', ?, '', 'queued', ?, 'plan')",
                               (snapshot['project'], issue_id, item['agent'], now()))
            conn.execute("UPDATE assessments SET run_id = ? WHERE snapshot_id = ? AND agent = ?",
                         (cur.lastrowid, snapshot_id, item['agent']))


def finish_assessment(conn, run_id, answer=None, error=None, limited=False):
    with conn:
        changed = conn.execute("UPDATE runs SET status = ?, finished_at = ?, error = ? WHERE id = ? "
                               "AND status IN ('queued', 'running')",
                               ('usage_limited' if limited else 'failed' if error else 'done',
                                now(), scrub(error), run_id)).rowcount
        if not changed:
            return
        if not limited:
            conn.execute("UPDATE assessments SET answer = ?, failures = failures + ?, error = ? WHERE run_id = ?",
                         (scrub(json.dumps(answer)) if answer is not None else None, int(error is not None),
                          scrub(error), run_id))


def mark_assessment_alerted(conn, snapshot_id, agent):
    with conn:
        conn.execute("UPDATE assessments SET alerted = 1 WHERE snapshot_id = ? AND agent = ?", (snapshot_id, agent))


def assessment_requested(conn, project, cycle):
    return conn.execute("SELECT 1 FROM assessment_requests WHERE project = ? AND cycle_id = ?",
                        (project, cycle)).fetchone() is not None


def assessment_running(conn, project, cycle):
    """Whether a new round is awaited or the latest still has an agent without an answer or two failures."""
    if assessment_requested(conn, project, cycle):
        return True
    snapshot = assessment_snapshot(conn, project, cycle)
    return snapshot is not None and any(not row['answer'] and row['failures'] < 2
                                        for row in assessments(conn, snapshot['id']))


def stories_fingerprint(stories):
    """The same for the same story IDs, names and descriptions in any order, as a snapshot stores them."""
    rows = sorted(json.loads(scrub(json.dumps(stories))), key=lambda row: row['id'])
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()


def assessment_retry(conn, project, cycle, stories):
    """The latest round's snapshot ID when it can be retried in place: same stories, some agent failed out."""
    snapshot = assessment_snapshot(conn, project, cycle)
    if snapshot is None:
        return None
    same = stories_fingerprint(json.loads(snapshot['stories'])) == stories_fingerprint(stories)
    if same and any(row['failures'] >= 2 for row in assessments(conn, snapshot['id'])):
        return snapshot['id']
    return None


def assessment_cooldowns(conn, snapshot_id):
    """Unfinished assessors currently waiting on usage, rather than doing work."""
    return conn.execute(
        "SELECT a.agent, c.until FROM assessments a JOIN cooldowns c ON c.agent = a.agent "
        "LEFT JOIN runs r ON r.id = a.run_id "
        "WHERE a.snapshot_id = ? AND a.answer IS NULL AND a.failures < 2 "
        "AND c.until > ? AND (r.status IS NULL OR r.status != 'running') ORDER BY a.agent",
        (snapshot_id, now())).fetchall()


def assessment_cooldown_announced(conn, snapshot_id, agent, until):
    return conn.execute(
        "SELECT 1 FROM assessment_cooldown_notices WHERE snapshot_id = ? AND agent = ? AND until = ?",
        (snapshot_id, agent, until)).fetchone() is not None


def claim_assessment_cooldown_notice(conn, snapshot_id, agent, until):
    """Claim before sending the best-effort alert so a restart cannot repeat it."""
    with conn:
        return conn.execute(
            "INSERT OR IGNORE INTO assessment_cooldown_notices VALUES (?, ?, ?)",
            (snapshot_id, agent, until)).rowcount == 1

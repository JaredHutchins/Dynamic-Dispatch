"""Each round's merged plan report, the quiet wait before an automatic new round, and Done-story history."""

from datetime import datetime, timedelta, timezone
import json

from ..redact import scrub
from .assessments import assessment_snapshot, stories_fingerprint
from .cycles import PLAN_ASSESSING, PLAN_PLANNED
from .store import now


def plan_report(conn, snapshot_id):
    return conn.execute("SELECT * FROM plan_reports WHERE snapshot_id = ?", (snapshot_id,)).fetchone()


def latest_plan_report(conn, project, cycle):
    """The report of the Cycle's current round, or None before it is in."""
    snapshot = assessment_snapshot(conn, project, cycle)
    return None if snapshot is None else plan_report(conn, snapshot['id'])


def save_plan_report(conn, snapshot_id, merged, report):
    """Keep the first report made for a round, so a restart posts exactly the same plan."""
    with conn:
        conn.execute("INSERT OR IGNORE INTO plan_reports "
                     "SELECT id, project, cycle_id, round, ?, ? FROM assessment_snapshots WHERE id = ?",
                     (scrub(json.dumps(merged)), scrub(report), snapshot_id))
    return plan_report(conn, snapshot_id)


def replan_when_quiet(conn, project, cycle, stories, quiet_minutes):
    """Ask for a new round once a planned Cycle's stories have differed from its round, unchanged, for quiet_minutes.

    Returns whether it asked. The request and the status change commit together.
    """
    snapshot = assessment_snapshot(conn, project, cycle)
    fingerprint = stories_fingerprint(stories)
    with conn:
        if snapshot is None or stories_fingerprint(json.loads(snapshot['stories'])) == fingerprint:
            conn.execute("DELETE FROM plan_changes WHERE project = ? AND cycle_id = ?", (project, cycle))
            return False
        seen = conn.execute("SELECT * FROM plan_changes WHERE project = ? AND cycle_id = ?",
                            (project, cycle)).fetchone()
        if seen is None or seen['fingerprint'] != fingerprint:
            conn.execute("INSERT OR REPLACE INTO plan_changes VALUES (?, ?, ?, ?)",
                         (project, cycle, fingerprint, now()))
            return False
        if datetime.fromisoformat(seen['since']) + timedelta(minutes=quiet_minutes) > datetime.now(timezone.utc):
            return False
        conn.execute("DELETE FROM plan_changes WHERE project = ? AND cycle_id = ?", (project, cycle))
        conn.execute("INSERT OR IGNORE INTO assessment_requests VALUES (?, ?)", (project, cycle))
        conn.execute("UPDATE cycle_plans SET status = ? WHERE project = ? AND cycle_id = ? AND status = ?",
                     (PLAN_ASSESSING, project, cycle, PLAN_PLANNED))
    return True


def record_done_story(conn, project, issue_id):
    """Remember when a story reached Done, timed from its first story run; a story without one is skipped."""
    with conn:
        conn.execute("INSERT OR IGNORE INTO done_stories "
                     "SELECT project, issue_id, MIN(COALESCE(claimed_at, queued_at)), ? FROM runs "
                     "WHERE project = ? AND issue_id = ? AND kind = 'story' GROUP BY project, issue_id",
                     (now(), project, issue_id))


def done_durations(conn, project, limit, *, with_sizes=False):
    """First-run-to-Done seconds for recent stories, optionally paired with their saved plan size.

    Unsized stories remain available for the estimate's fallback weighting.
    """
    rows = conn.execute("SELECT issue_id, started, done FROM done_stories WHERE project = ? ORDER BY done DESC LIMIT ?",
                        (project, limit)).fetchall()
    seconds = [(datetime.fromisoformat(r['done']) - datetime.fromisoformat(r['started'])).total_seconds()
               for r in rows]
    if not with_sizes:
        return [s for s in seconds if s >= 0]
    # Newest saved plan wins if a story has been assessed in several rounds.
    sizes = {}
    reports = conn.execute("SELECT merged FROM plan_reports WHERE project = ? ORDER BY snapshot_id DESC",
                           (project,)).fetchall()
    for report in reports:
        for sid, story in json.loads(report['merged']).get('merged', {}).get('stories', {}).items():
            recorded = [size for size in story.get('sizes', {}).values() if size in ('S', 'M', 'L')]
            if recorded and sid not in sizes:
                sizes[sid] = max(recorded, key=('S', 'M', 'L').index)
    return [{'seconds': duration, 'size': sizes.get(row['issue_id'])}
            for row, duration in zip(rows, seconds) if duration >= 0]

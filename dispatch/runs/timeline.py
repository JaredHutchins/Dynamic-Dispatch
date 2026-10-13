"""Events: best-effort history of each run and story, never state DD relies on."""

from ..redact import scrub, warn
from .store import DETAIL_LIMIT, get, now


def record_event(conn, run_id, event_type, detail, *, agent=None, project=None, issue_id=None):
    """Best-effort telemetry; waiting stories can have no run yet."""
    try:
        if run_id is not None:
            row = get(conn, run_id)
            project, issue_id = row["project"], row["issue_id"]
            agent = agent or row["agent"]
        event_type, agent, detail = scrub(event_type)[:64], scrub(agent), scrub(detail or "")[:DETAIL_LIMIT]
        stamp = now()
        with conn:
            previous = conn.execute(
                "SELECT * FROM events WHERE run_id IS ? AND project = ? AND issue_id IS ? ORDER BY id DESC LIMIT 1",
                (run_id, project, issue_id),
            ).fetchone()
            if (event_type in ("warning", "retry", "refresh_skipped", "main_unchanged", "refresh_waiting",
                               "main_check_waiting", "self_update_waiting")
                    and previous and previous["repeating"]
                    and (previous["type"], previous["agent"], previous["detail"]) == (event_type, agent, detail)):
                conn.execute("UPDATE events SET repeat_count = repeat_count + 1, last_seen = ? WHERE id = ?",
                             (stamp, previous["id"]))
            else:
                conn.execute(
                    "INSERT INTO events (run_id, project, issue_id, time, type, agent, detail, last_seen) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (run_id, project, issue_id, stamp, event_type, agent, detail, stamp),
                )
    except Exception as e:
        warn(f"Could not record event for run {run_id}: {e}")


def clear_repeats(conn, *, project, issue_id=None, waiting=None):
    """End cleared warnings, including waiting stories absent from a fresh poll."""
    try:
        with conn:
            if waiting is None:
                conn.execute("UPDATE events SET repeating = 0 WHERE type IN ('warning', 'retry') "
                             "AND run_id IS NULL AND project = ? AND issue_id IS ?",
                             (project, issue_id))
            else:
                placeholders = ", ".join("?" for _ in waiting)
                conn.execute(
                    "UPDATE events SET repeating = 0 WHERE type IN ('warning', 'retry') "
                    "AND run_id IS NULL AND project = ? "
                    f"AND (issue_id IS NULL OR issue_id NOT IN ({placeholders}))",
                    (project, *waiting),
                )
    except Exception as e:
        warn(f"Could not clear repeating events: {e}")


def events(conn, *, run_id=None, project=None, issue_id=None):
    """Timeline in insertion order, optionally narrowed to a run or story."""
    clauses, args = [], []
    for column, value in (("run_id", run_id), ("project", project), ("issue_id", issue_id)):
        if value is not None:
            clauses.append(f"{column} = ?")
            args.append(value)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    return conn.execute("SELECT * FROM events" + where + " ORDER BY id", args).fetchall()

"""Login alerts, start dates, agent usage and the heartbeat."""

import json

from ..redact import scrub, warn
from .store import get, now


def login_alert_keys(conn):
    return {row["key"] for row in conn.execute("SELECT key FROM login_alerts")}


def mark_login_alert(conn, key, present=True):
    with conn:
        if present:
            return conn.execute("INSERT OR IGNORE INTO login_alerts VALUES (?)", (key,)).rowcount == 1
        return conn.execute("DELETE FROM login_alerts WHERE key = ?", (key,)).rowcount == 1


def record_usage(conn, run_id, agent, usage=None, cost_usd=None, *, additive=False):
    """Save independently of the outcome, including unavailable usage as NULL."""
    try:
        if additive:
            previous = get(conn, run_id)
            totals = json.loads(previous["usage"] or "{}")
            for key, value in (usage or {}).items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    totals[key] = totals.get(key, 0) + value
            usage = totals or None
            if previous["cost_usd"] is not None:
                cost_usd = previous["cost_usd"] + (cost_usd or 0)
        encoded = scrub(json.dumps(usage)) if usage else None
        with conn:
            conn.execute(
                "INSERT INTO agent_usage (run_id, time, agent, usage, cost_usd) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET usage = excluded.usage, cost_usd = excluded.cost_usd",
                (run_id, now(), scrub(agent), encoded, cost_usd),
            )
            conn.execute("UPDATE runs SET usage = ?, cost_usd = ? WHERE id = ?",
                         (encoded, cost_usd, run_id))
    except Exception as e:
        warn(f"Could not record usage for run {run_id}: {e}")


def usage_records(conn):
    return conn.execute(
        "SELECT u.*, r.project, r.issue_id FROM agent_usage u JOIN runs r ON r.id = u.run_id "
        "ORDER BY u.time, u.run_id"
    ).fetchall()


def record_heartbeat(conn, version):
    try:
        with conn:
            conn.execute("INSERT INTO heartbeats (id, time, version) VALUES (1, ?, ?) "
                         "ON CONFLICT(id) DO UPDATE SET time = excluded.time, version = excluded.version",
                         (now(), scrub(version)[:128]))
    except Exception as e:
        warn(f"Could not record heartbeat: {e}")


def heartbeat(conn):
    return conn.execute("SELECT * FROM heartbeats ORDER BY id DESC LIMIT 1").fetchone()


def start_date_set(conn, issue_id):
    """True once DD has set this story's Plane Start Date; it is only ever set once."""
    return conn.execute("SELECT 1 FROM start_dates WHERE issue_id = ?", (issue_id,)).fetchone() is not None


def record_start_date(conn, issue_id, date):
    with conn:
        conn.execute("INSERT OR IGNORE INTO start_dates (issue_id, date) VALUES (?, ?)", (issue_id, date))

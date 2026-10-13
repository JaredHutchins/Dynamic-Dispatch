"""Agent slots, cool-downs and stories waiting for a free agent."""

from datetime import datetime, timezone

from .store import QUEUED, RUNNING, now
from .plan_decisions import cycle_start_allowed_sql


def set_agent(conn, run_id, agent):
    with conn:
        conn.execute("UPDATE runs SET agent = ? WHERE id = ?", (agent, run_id))
        conn.execute("UPDATE agent_slots SET agent = ? WHERE run_id = ?", (agent, run_id))


def save_cooldown(conn, agent, until):
    with conn:
        conn.execute(
            "INSERT INTO cooldowns (agent, until) VALUES (?, ?) "
            "ON CONFLICT(agent) DO UPDATE SET until = excluded.until",
            (agent, until),
        )


def cooldowns(conn, at):
    """Agents still cooling down at this time, as {agent: until}."""
    return {
        row["agent"]: row["until"]
        for row in conn.execute("SELECT agent, until FROM cooldowns WHERE until > ?", (at,))
    }


def busy_agents(conn, exclude=None):
    return {row[0] for row in conn.execute(
        "SELECT DISTINCT agent FROM runs WHERE status IN (?, ?) AND id != ? "
        f"AND (status = 'running' OR {cycle_start_allowed_sql('runs')}) "
        "AND (status = 'running' OR kind IN ('main_check', 'plan') OR NOT EXISTS "
        "(SELECT 1 FROM main_health h WHERE h.project = runs.project AND h.state = 'red' "
        "AND (h.fix_issue_id IS NULL OR h.fix_issue_id != runs.issue_id)))",
        (QUEUED, RUNNING, exclude or -1))} | {row[0] for row in conn.execute(
            "SELECT agent FROM agent_slots WHERE run_id != ?", (exclude or -1,))}


def agent_work(conn):
    """Work reserving each account, preferring its claimed slot over queued work."""
    rows = conn.execute(
        "SELECT r.* FROM runs r LEFT JOIN agent_slots s ON s.run_id = r.id "
        "WHERE s.run_id IS NOT NULL OR (r.status IN (?, ?) "
        f"AND (r.status = 'running' OR {cycle_start_allowed_sql('r')}) "
        "AND (r.status = 'running' OR r.kind IN ('main_check', 'plan') OR NOT EXISTS "
        "(SELECT 1 FROM main_health h WHERE h.project = r.project AND h.state = 'red' "
        "AND (h.fix_issue_id IS NULL OR h.fix_issue_id != r.issue_id)))) "
        "ORDER BY (s.run_id IS NOT NULL) DESC, (r.status = 'running') DESC, r.id",
        (QUEUED, RUNNING))
    work = {}
    for row in rows:
        work.setdefault(row["agent"], row)
    return work


def claim_next(conn):
    """Atomically claim the oldest queued run whose account has no worker."""
    with conn:
        row = conn.execute(
            f"""UPDATE runs SET status = ?, claimed_at = ?
               WHERE id = (SELECT q.id FROM runs q WHERE q.status = ?
                   AND {cycle_start_allowed_sql('q')}
                   AND (q.kind IN ('main_check', 'plan') OR NOT EXISTS
                       (SELECT 1 FROM main_health h WHERE h.project = q.project AND h.state = 'red'
                        AND (h.fix_issue_id IS NULL OR h.fix_issue_id != q.issue_id)))
                   AND (q.kind != 'plan' OR NOT EXISTS (SELECT 1 FROM cooldowns c WHERE c.agent = q.agent AND c.until > ?))
                   AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.agent = q.agent AND r.status = 'running')
                   AND NOT EXISTS (SELECT 1 FROM agent_slots s WHERE s.agent = q.agent)
                   ORDER BY q.id LIMIT 1)
               RETURNING *""",
            (RUNNING, now(), QUEUED, now()),
        ).fetchone()
        if row is not None:
            conn.execute("INSERT INTO agent_slots VALUES (?, ?)", (row["agent"], row["id"]))
        return row


def release_slot(conn, run_id):
    with conn:
        conn.execute("DELETE FROM agent_slots WHERE run_id = ?", (run_id,))


def start_wait(conn, *, project, issue_id, run_id, agent, until):
    """The story waits in In Progress until an agent is free; agent and until say which and when."""
    with conn:
        conn.execute(
            "INSERT INTO waits (issue_id, project, run_id, agent, until, since) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(issue_id) DO UPDATE SET project = excluded.project, run_id = excluded.run_id, "
            "agent = excluded.agent, until = excluded.until, since = excluded.since, comment_posted = 0",
            (issue_id, project, run_id, agent, until, datetime.now(timezone.utc).isoformat()),
        )


def wait_for(conn, issue_id):
    return conn.execute("SELECT * FROM waits WHERE issue_id = ?", (issue_id,)).fetchone()


def waits(conn, project):
    return conn.execute("SELECT * FROM waits WHERE project = ? ORDER BY since", (project,)).fetchall()


def mark_wait_announced(conn, issue_id):
    with conn:
        conn.execute("UPDATE waits SET comment_posted = 1 WHERE issue_id = ?", (issue_id,))


def end_wait(conn, issue_id):
    with conn:
        conn.execute("DELETE FROM waits WHERE issue_id = ?", (issue_id,))


def has_running(conn):
    """Whether any run is executing, or any worker still holds its account's slot."""
    return conn.execute(
        "SELECT 1 FROM runs WHERE status = ? UNION ALL SELECT 1 FROM agent_slots LIMIT 1", (RUNNING,)
    ).fetchone() is not None

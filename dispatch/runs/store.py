"""The database connection, its schema and migrations, and the states a run moves through."""

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

MIN_SQLITE = (3, 35, 0)  # UPDATE ... RETURNING
DETAIL_LIMIT = 8000

QUEUED = "queued"
RUNNING = "running"
HELD = "held_main"
AWAITING_REVIEW = "awaiting_review"    # work done, PR open, board not yet updated
AWAITING_BLOCKED = "awaiting_blocked"  # failed, board not yet updated
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"                # story left In Progress before work was pushed
LIMITED = "usage_limited"              # agent hit its usage limit; the story stays In Progress
INTERRUPTED = "interrupted"            # DD stopped mid-run; the story was queued again

MAIN_CHECK = "main_check"  # run kind: tests main in an agent's account, without an agent

RESTART_LIMIT = 2  # restarts in a row that interrupt a story before it goes to Blocked

ACTIVE = (HELD, QUEUED, RUNNING, AWAITING_REVIEW, AWAITING_BLOCKED)
AWAITING = (AWAITING_REVIEW, AWAITING_BLOCKED)

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project         TEXT NOT NULL,
    issue_id        TEXT NOT NULL,
    issue_key       TEXT NOT NULL,
    issue_name      TEXT NOT NULL,
    agent           TEXT NOT NULL,
    branch          TEXT NOT NULL,
    worktree        TEXT,
    status          TEXT NOT NULL,
    error           TEXT,
    summary         TEXT,
    usage           TEXT,
    cost_usd        REAL,
    pr_url          TEXT,
    needs_decision  INTEGER NOT NULL DEFAULT 0,
    no_changes      INTEGER NOT NULL DEFAULT 0,
    decision_at     TEXT,
    comment_posted  INTEGER NOT NULL DEFAULT 0,
    committed       INTEGER NOT NULL DEFAULT 0,
    pushed          INTEGER NOT NULL DEFAULT 0,
    kind            TEXT NOT NULL DEFAULT 'story',
    repair_attempts INTEGER NOT NULL DEFAULT 0,
    note            TEXT,
    handed_from     TEXT,
    revision        TEXT,
    hold_data       TEXT,
    docs_line       TEXT,
    hold_announced  INTEGER NOT NULL DEFAULT 0,
    queued_at       TEXT NOT NULL,
    claimed_at      TEXT,
    finished_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_active
    ON runs(issue_id) WHERE status IN ({", ".join(f"'{s}'" for s in ACTIVE)});
CREATE TABLE IF NOT EXISTS agent_slots (
    agent TEXT PRIMARY KEY,
    run_id INTEGER NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS cooldowns (
    agent           TEXT PRIMARY KEY,
    until           TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS waits (
    issue_id        TEXT PRIMARY KEY,
    project         TEXT NOT NULL,
    run_id          INTEGER,
    agent           TEXT NOT NULL,
    until           TEXT NOT NULL,
    since           TEXT NOT NULL,
    comment_posted  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS early_cycle_ends (
    project TEXT NOT NULL, cycle_id TEXT NOT NULL, end_date TEXT NOT NULL,
    alerted INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (project, cycle_id)
);
CREATE TABLE IF NOT EXISTS release_notes (
    project TEXT NOT NULL, cycle_id TEXT NOT NULL,
    alerted INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (project, cycle_id)
);
CREATE TABLE IF NOT EXISTS cycle_members (
    project TEXT NOT NULL,
    cycle_id TEXT NOT NULL,
    issue_id TEXT NOT NULL,
    PRIMARY KEY (project, cycle_id, issue_id)
);
CREATE TABLE IF NOT EXISTS cycle_plans (
    project TEXT NOT NULL,
    cycle_id TEXT NOT NULL,
    issue_id TEXT NOT NULL,
    status TEXT NOT NULL,
    PRIMARY KEY (project, cycle_id)
);
CREATE TABLE IF NOT EXISTS carry_overs (
    project TEXT NOT NULL, cycle_id TEXT NOT NULL, issue_id TEXT NOT NULL,
    cycle_name TEXT NOT NULL, issue_name TEXT NOT NULL,
    finished INTEGER NOT NULL DEFAULT 0, listed_in TEXT,
    PRIMARY KEY (project, cycle_id, issue_id)
);
CREATE TABLE IF NOT EXISTS sprint_holds (
    project TEXT NOT NULL, cycle_id TEXT NOT NULL, issue_id TEXT NOT NULL,
    PRIMARY KEY (project, cycle_id, issue_id)
);
CREATE TABLE IF NOT EXISTS blocked_reminders (
    project TEXT NOT NULL, issue_id TEXT NOT NULL, last_sent TEXT NOT NULL, message TEXT NOT NULL,
    PRIMARY KEY (project, issue_id)
);
CREATE TABLE IF NOT EXISTS plan_approvals (
    project TEXT NOT NULL, cycle_id TEXT NOT NULL, snapshot_id INTEGER NOT NULL,
    issue_id TEXT NOT NULL, comment TEXT NOT NULL, members TEXT NOT NULL,
    described INTEGER NOT NULL DEFAULT 0, deleted INTEGER NOT NULL DEFAULT 0, approved_at TEXT,
    PRIMARY KEY (project, cycle_id, snapshot_id)
);
CREATE TABLE IF NOT EXISTS assessment_feedback (
    project TEXT NOT NULL, cycle_id TEXT NOT NULL, reasons TEXT NOT NULL,
    PRIMARY KEY (project, cycle_id)
);
CREATE TABLE IF NOT EXISTS snapshot_members (
    snapshot_id INTEGER PRIMARY KEY, members TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshot_feedback (
    snapshot_id INTEGER PRIMARY KEY, reasons TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assessment_snapshots (
    id INTEGER PRIMARY KEY,
    project TEXT NOT NULL,
    cycle_id TEXT NOT NULL,
    issue_id TEXT NOT NULL,
    revision TEXT NOT NULL,
    stories TEXT NOT NULL,
    end_date TEXT,
    start_date TEXT,
    round INTEGER NOT NULL DEFAULT 1,
    UNIQUE(project, cycle_id, round)
);
CREATE TABLE IF NOT EXISTS assessment_requests (
    project TEXT NOT NULL,
    cycle_id TEXT NOT NULL,
    PRIMARY KEY(project, cycle_id)
);
CREATE TABLE IF NOT EXISTS assessments (
    snapshot_id INTEGER NOT NULL,
    agent TEXT NOT NULL,
    run_id INTEGER,
    failures INTEGER NOT NULL DEFAULT 0,
    answer TEXT,
    error TEXT,
    alerted INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(snapshot_id, agent)
);
CREATE TABLE IF NOT EXISTS assessment_cooldown_notices (
    snapshot_id INTEGER NOT NULL,
    agent TEXT NOT NULL,
    until TEXT NOT NULL,
    PRIMARY KEY(snapshot_id, agent, until)
);
CREATE TABLE IF NOT EXISTS plan_reports (
    snapshot_id INTEGER PRIMARY KEY,
    project TEXT NOT NULL,
    cycle_id TEXT NOT NULL,
    round INTEGER NOT NULL,
    merged TEXT NOT NULL,
    report TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plan_changes (
    project TEXT NOT NULL,
    cycle_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    since TEXT NOT NULL,
    PRIMARY KEY (project, cycle_id)
);
CREATE TABLE IF NOT EXISTS done_stories (
    project TEXT NOT NULL,
    issue_id TEXT NOT NULL,
    started TEXT NOT NULL,
    done TEXT NOT NULL,
    PRIMARY KEY (project, issue_id)
);
CREATE TABLE IF NOT EXISTS plan_commands (
    project TEXT NOT NULL,
    comment_id TEXT NOT NULL,
    cycle_id TEXT NOT NULL,
    command TEXT NOT NULL,
    reasons TEXT,
    reply TEXT,
    replied INTEGER NOT NULL DEFAULT 0,
    received TEXT NOT NULL,
    PRIMARY KEY (project, comment_id)
);
CREATE TABLE IF NOT EXISTS start_dates (
    issue_id        TEXT PRIMARY KEY,
    date            TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    project TEXT NOT NULL,
    issue_id TEXT,
    time TEXT NOT NULL,
    type TEXT NOT NULL,
    agent TEXT,
    detail TEXT NOT NULL,
    repeat_count INTEGER NOT NULL DEFAULT 1,
    last_seen TEXT,
    repeating INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, id);
CREATE INDEX IF NOT EXISTS idx_events_story ON events(project, issue_id, id);
CREATE TABLE IF NOT EXISTS agent_usage (
    run_id INTEGER PRIMARY KEY,
    time TEXT NOT NULL,
    agent TEXT NOT NULL,
    usage TEXT,
    cost_usd REAL
);
CREATE TABLE IF NOT EXISTS combine_commits (
    sha             TEXT PRIMARY KEY,
    run_id          INTEGER NOT NULL,
    clean           INTEGER NOT NULL,
    note            TEXT
);
CREATE TABLE IF NOT EXISTS pr_lights (
    project TEXT NOT NULL,
    issue_id TEXT NOT NULL,
    state TEXT NOT NULL,
    head TEXT,
    shown TEXT,
    disposition TEXT NOT NULL,
    PRIMARY KEY (project, issue_id)
);
CREATE TABLE IF NOT EXISTS approval_notices (
    project TEXT NOT NULL,
    issue_id TEXT NOT NULL,
    sha TEXT NOT NULL,
    PRIMARY KEY (project, issue_id, sha)
);
CREATE TABLE IF NOT EXISTS main_health (
    project         TEXT PRIMARY KEY,
    revision        TEXT NOT NULL,
    state           TEXT NOT NULL,
    since           TEXT NOT NULL,
    failing         TEXT NOT NULL DEFAULT '[]',
    broken_by       TEXT,
    alert_owed      INTEGER NOT NULL DEFAULT 0,
    fix_issue_id    TEXT,
    fix_revision    TEXT,
    fix_episode     TEXT,
    fix_alerted     INTEGER NOT NULL DEFAULT 0,
    failure_output  TEXT,
    breaking_revision TEXT,
    pr_links        TEXT
);
CREATE TABLE IF NOT EXISTS settled_prs (
    pr_url TEXT PRIMARY KEY,
    outcome TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS main_check_failures (
    project TEXT PRIMARY KEY,
    count INTEGER NOT NULL DEFAULT 0,
    retry_at TEXT,
    reason TEXT NOT NULL DEFAULT '',
    announced INTEGER NOT NULL DEFAULT 0,
    generation INTEGER NOT NULL DEFAULT 0,
    alert_owed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS migrations (name TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS login_alerts (key TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS heartbeats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    time TEXT NOT NULL,
    version TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS self_update_notices (
    revision TEXT NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY (revision, reason)
);
CREATE TABLE IF NOT EXISTS self_update_restart (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    project TEXT NOT NULL,
    revision TEXT NOT NULL,
    title TEXT NOT NULL
);
"""


class StoreError(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def check_sqlite():
    if sqlite3.sqlite_version_info < MIN_SQLITE:
        raise StoreError(
            f"SQLite {sqlite3.sqlite_version} is too old; "
            f"{'.'.join(map(str, MIN_SQLITE))} or newer is required"
        )


def connect(db_path, *, initialize=True):
    check_sqlite()
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        if not initialize:
            return conn
        conn.executescript(SCHEMA)
        # Databases made before these columns existed. Their runs count as never
        # having committed or pushed, so they are picked up as they were then:
        # unpushed work resumes, and a pull request that failed runs the agent again.
        columns = {c["name"] for c in conn.execute("PRAGMA table_info(runs)")}
        for column in ("committed", "pushed", "repair_attempts", "hold_announced", "needs_decision", "no_changes"):
            if column not in columns:
                with conn:
                    conn.execute(f"ALTER TABLE runs ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0")
        if "kind" not in columns:
            with conn:
                conn.execute("ALTER TABLE runs ADD COLUMN kind TEXT NOT NULL DEFAULT 'story'")
        for column in ("handed_from", "revision", "hold_data", "decision_at", "docs_line"):
            if column not in columns:
                with conn:
                    conn.execute(f"ALTER TABLE runs ADD COLUMN {column} TEXT")
        if "note" not in {c["name"] for c in conn.execute("PRAGMA table_info(combine_commits)")}:
            with conn:
                conn.execute("ALTER TABLE combine_commits ADD COLUMN note TEXT")
        columns = {c["name"] for c in conn.execute("PRAGMA table_info(main_health)")}
        with conn:
            for column, definition in (("fix_issue_id", "TEXT"), ("fix_revision", "TEXT"), ("fix_episode", "TEXT"),
                                       ("fix_alerted", "INTEGER NOT NULL DEFAULT 0"),
                                       ("failure_output", "TEXT"), ("breaking_revision", "TEXT"),
                                       ("pr_links", "TEXT")):
                if column not in columns:
                    conn.execute(f"ALTER TABLE main_health ADD COLUMN {column} {definition}")
            conn.execute("UPDATE main_health SET fix_episode = lower(hex(randomblob(16))) "
                         "WHERE state = 'red' AND fix_episode IS NULL")
        columns = {c["name"] for c in conn.execute("PRAGMA table_info(events)")}
        with conn:
            for column, definition in (("repeat_count", "INTEGER NOT NULL DEFAULT 1"),
                                       ("last_seen", "TEXT"), ("repeating", "INTEGER NOT NULL DEFAULT 1")):
                if column not in columns:
                    conn.execute(f"ALTER TABLE events ADD COLUMN {column} {definition}")
            conn.execute("UPDATE events SET last_seen = time WHERE last_seen IS NULL")
            conn.execute("DELETE FROM heartbeats WHERE id != (SELECT id FROM heartbeats ORDER BY time DESC, id DESC LIMIT 1)")
            conn.execute("UPDATE heartbeats SET id = 1")
        with conn:
            conn.execute("DROP INDEX IF EXISTS idx_runs_active")
            conn.execute("CREATE UNIQUE INDEX idx_runs_active ON runs(issue_id) WHERE status IN "
                         "(" + ", ".join(f"'{state}'" for state in ACTIVE) + ")")
        if "approved_at" not in {c["name"] for c in conn.execute("PRAGMA table_info(plan_approvals)")}:
            # Approvals from before this column count from the upgrade, so work already
            # running then is never held behind its Cycle.
            with conn:
                conn.execute("ALTER TABLE plan_approvals ADD COLUMN approved_at TEXT")
                conn.execute("UPDATE plan_approvals SET approved_at = ?", (now(),))
        if "start_date" not in {c["name"] for c in conn.execute("PRAGMA table_info(assessment_snapshots)")}:
            with conn:
                conn.execute("ALTER TABLE assessment_snapshots ADD COLUMN start_date TEXT")
        _migrate_lights(conn)
        _migrate_done_stories(conn)
        _migrate_release_notes(conn)
        return conn
    except BaseException:
        conn.close()
        raise


def get(conn, run_id):
    return conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()


def _migrate_lights(conn):
    # The marker and imported state commit together, including on an empty database.
    with conn:
        if not conn.execute("INSERT OR IGNORE INTO migrations VALUES ('pr_lights')").rowcount:
            return
        for row in conn.execute(
                "SELECT * FROM events WHERE type IN ('light', 'light_owed', 'light_left') "
                "AND id IN (SELECT MAX(id) FROM events "
                "WHERE type IN ('light', 'light_owed', 'light_left') GROUP BY project, issue_id)"):
            words = row["detail"].split()
            state = words[0]
            head = words[1].rstrip(":") if len(words) > 1 and words[1] != "closed:" else None
            shown = (words[2] if len(words) > 2 else state) if row["type"] == "light" else None
            conn.execute("INSERT OR IGNORE INTO pr_lights VALUES (?, ?, ?, ?, ?, ?)",
                         (row["project"], row["issue_id"], state, head, shown,
                          {"light": "set", "light_owed": "owed", "light_left": "left"}[row["type"]]))
        conn.execute("INSERT OR IGNORE INTO approval_notices "
                     "SELECT project, issue_id, detail FROM events WHERE type = 'approval_voided'")


def _migrate_done_stories(conn):
    # Stories merged before done_stories existed seed the plan estimate's history once.
    with conn:
        if not conn.execute("INSERT OR IGNORE INTO migrations VALUES ('done_stories')").rowcount:
            return
        conn.execute(
            "INSERT OR IGNORE INTO done_stories "
            "SELECT e.project, e.issue_id, MIN(COALESCE(r.claimed_at, r.queued_at)), MAX(e.time) "
            "FROM events e JOIN runs r ON r.project = e.project AND r.issue_id = e.issue_id AND r.kind = 'story' "
            "WHERE e.type = 'moved_to_done' GROUP BY e.project, e.issue_id")


def _migrate_release_notes(conn):
    # Cycles that ended before draft release notes existed never get them, so an upgrade sends no burst.
    with conn:
        if not conn.execute("INSERT OR IGNORE INTO migrations VALUES ('release_notes')").rowcount:
            return
        conn.execute("INSERT OR IGNORE INTO release_notes SELECT project, cycle_id, 1 FROM cycle_plans "
                     "WHERE status = 'complete' UNION "
                     "SELECT project, cycle_id, 1 FROM early_cycle_ends WHERE alerted = 1")


def check_writable(db_path):
    """Check filesystem access without creating a database, journal or directory.

    A write probe would alter an installation. This checks access rights, not
    available disk space or whether another process currently holds a lock.
    """
    check_sqlite()
    path = Path(db_path)
    if not path.parent.is_dir() or not os.access(path.parent, os.W_OK | os.X_OK):
        raise StoreError("Database directory is not writable")
    if path.exists() and (not path.is_file() or not os.access(path, os.R_OK | os.W_OK)):
        raise StoreError("Database is not readable and writable")

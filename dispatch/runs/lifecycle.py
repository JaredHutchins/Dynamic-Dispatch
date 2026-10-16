"""Runs from queueing to their outcome, and what earlier runs of a story left behind."""

import json
import sqlite3

from ..redact import scrub
from .store import (ACTIVE, AWAITING, AWAITING_BLOCKED, AWAITING_REVIEW, CANCELLED, DONE, FAILED, HELD, INTERRUPTED,
                    LIMITED, MAIN_CHECK, QUEUED, RESTART_LIMIT, RUNNING, get, now)
from .timeline import record_event


def enqueue(conn, *, project, issue, agent, branch, kind="story", pr_url=None, handed_from=None):
    """Queue a run. Returns the row id, or None if this story already has an active run.

    handed_from names the agent whose usage limit stopped the story's previous
    run, when this run takes over from it straight away.
    """
    try:
        with conn:
            cur = conn.execute(
                """INSERT INTO runs
                   (project, issue_id, issue_key, issue_name, agent, branch, status, queued_at, kind, pr_url,
                    handed_from)
                   SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                   WHERE NOT EXISTS (SELECT 1 FROM main_health WHERE project = ? AND state = 'red'
                                     AND (fix_issue_id IS NULL OR fix_issue_id != ?))""",
                (project, issue["id"], str(issue.get("sequence_id", "")),
                 scrub(issue.get("name", "")), agent, branch, QUEUED, now(), kind, pr_url, handed_from, project, issue["id"]),
            )
            if not cur.rowcount:
                return None
            run_id = cur.lastrowid
        record_event(conn, run_id, "queued", "Refresh queued" if kind == "refresh" else "Story queued")
        return run_id
    except sqlite3.IntegrityError:
        return None


def is_active(conn, issue_id):
    placeholders = ", ".join("?" for _ in ACTIVE)
    return conn.execute(
        f"SELECT 1 FROM runs WHERE issue_id = ? AND (status IN ({placeholders}) "
        "OR id IN (SELECT run_id FROM agent_slots))", (issue_id, *ACTIVE)
    ).fetchone() is not None


def last_agent(conn, issue_id):
    """The agent the story's latest run was given, or None for a story never run."""
    row = conn.execute(
        "SELECT agent FROM runs WHERE issue_id = ? ORDER BY id DESC LIMIT 1", (issue_id,)
    ).fetchone()
    return row["agent"] if row else None


def last_builder(conn, issue_id, before_id):
    """The agent of the latest run before this one that started its agent on the story, or None.

    Every agent start records the run's usage, so a run without it never got that far.
    """
    row = conn.execute(
        "SELECT r.agent FROM runs r JOIN agent_usage u ON u.run_id = r.id "
        "WHERE r.issue_id = ? AND r.id < ? ORDER BY r.id DESC LIMIT 1",
        (issue_id, before_id),
    ).fetchone()
    return row["agent"] if row else None


def counts_since(conn, since):
    """How many runs each agent was given since this time, as {agent: count}.

    Main checks run no agent, so they don't count towards an agent's use.
    """
    return {
        row["agent"]: row["n"]
        for row in conn.execute(
            "SELECT agent, COUNT(*) AS n FROM runs WHERE queued_at >= ? AND kind != ? GROUP BY agent",
            (since, MAIN_CHECK)
        )
    }


def await_review(conn, run_id, *, summary, usage, cost_usd, pr_url, worktree,
                 needs_decision=False, no_changes=False):
    with conn:
        conn.execute(
            """UPDATE runs SET status = ?, summary = ?, usage = ?, cost_usd = ?,
                               pr_url = ?, worktree = ?, needs_decision = ?, no_changes = ?, decision_at = ?
               WHERE id = ?""",
            (AWAITING_REVIEW, scrub(summary), scrub(json.dumps(usage)) if usage else None,
             cost_usd, pr_url, worktree, int(needs_decision), int(no_changes),
             now() if needs_decision else None, run_id),
        )


def await_blocked(conn, run_id, error, worktree=None):
    with conn:
        conn.execute(
            "UPDATE runs SET status = ?, error = ?, worktree = COALESCE(?, worktree) WHERE id = ?",
            (AWAITING_BLOCKED, scrub(error), worktree, run_id),
        )
    record_event(conn, run_id, "blocked", error)


def mark_committed(conn, run_id):
    with conn:
        conn.execute("UPDATE runs SET committed = 1 WHERE id = ?", (run_id,))


def last_committed(conn, issue_id, before_id):
    """The latest run before this one that committed work for the story, or None.

    Only a committing run changes what the branch holds unpushed, so it alone
    says whether that work was stopped by a failure or a usage limit, or pulled
    out by a human. Only the last is cancelled.
    """
    return conn.execute(
        "SELECT * FROM runs WHERE issue_id = ? AND id < ? AND committed = 1 ORDER BY id DESC LIMIT 1",
        (issue_id, before_id),
    ).fetchone()


def _streak(conn, issue_id, before_id, status):
    """IDs of the story's runs before this one that ended with this status in a row, latest first.

    Any run that finished otherwise ends the streak. Cancelled runs are passed
    over: they didn't finish at all, whether a poll found no agent for them or
    the story was moved out of In Progress. Refreshes don't count.
    """
    streak = []
    for row in conn.execute(
        "SELECT id, status FROM runs WHERE issue_id = ? AND id < ? AND kind = 'story' ORDER BY id DESC",
        (issue_id, before_id),
    ):
        if row["status"] == CANCELLED:
            continue
        if row["status"] != status:
            break
        streak.append(row["id"])
    return streak


def limit_streak(conn, issue_id, before_id):
    """How many of the story's runs before this one were stopped by a usage limit in a row."""
    return len(_streak(conn, issue_id, before_id, LIMITED))


def restarts(conn, issue_id, before_id):
    """IDs of the story's runs before this one that a restart interrupted in a row, latest first."""
    return _streak(conn, issue_id, before_id, INTERRUPTED)


def save_docs_line(conn, run_id, line):
    """The Docs line that passed DD's check for this run's work, so work resumed without the agent can be checked."""
    with conn:
        conn.execute("UPDATE runs SET docs_line = ? WHERE id = ?", (scrub(line) if line else None, run_id))


def mark_pushed(conn, run_id):
    with conn:
        conn.execute("UPDATE runs SET pushed = 1 WHERE id = ?", (run_id,))


def last_pushed(conn, issue_id, before_id):
    """The latest run before this one that pushed work or opened its pull request, or None.

    If that run has no pull request, its work reached origin but the pull
    request never opened. A run that only retried opening it doesn't push, but
    once it succeeds its pull request is what counts, so it is included.
    """
    return conn.execute(
        """SELECT * FROM runs WHERE issue_id = ? AND id < ? AND (pushed = 1 OR pr_url IS NOT NULL)
           ORDER BY id DESC LIMIT 1""",
        (issue_id, before_id),
    ).fetchone()


def earlier_pull_request(conn, run):
    """The latest run before this one that recorded a pull request for the story, or None."""
    return conn.execute(
        "SELECT * FROM runs WHERE project = ? AND issue_id = ? AND id < ? AND pr_url IS NOT NULL "
        "ORDER BY id DESC LIMIT 1", (run["project"], run["issue_id"], run["id"]),
    ).fetchone()


def decision_after(conn, project, issue_id, pr_url):
    """When the pull request last went back for the approver's decision, or None.

    Read from every run of the pull request, so later refreshes keep the cutoff.
    """
    return conn.execute(
        "SELECT MAX(decision_at) FROM runs WHERE project = ? AND issue_id = ? AND pr_url = ?",
        (project, issue_id, pr_url),
    ).fetchone()[0]


def mark_comment_posted(conn, run_id):
    with conn:
        conn.execute("UPDATE runs SET comment_posted = 1 WHERE id = ?", (run_id,))


def close(conn, run_id, status, note=None):
    """Make a run terminal. Only called once Plane reflects the outcome, or it no longer can."""
    with conn:
        row = conn.execute(
            "UPDATE runs SET status = ?, note = COALESCE(?, note), finished_at = ? WHERE id = ? "
            "RETURNING error, kind",
            (status, scrub(note), now(), run_id),
        ).fetchone()
    if row is not None:
        outcome = {DONE: "in_review", FAILED: "blocked", CANCELLED: "cancelled",
                   LIMITED: "usage_limit", INTERRUPTED: "interrupted"}.get(status, status)
        if row["kind"] == MAIN_CHECK:
            outcome = {DONE: "checked", FAILED: "not_checked"}.get(status, status)
        record_event(conn, run_id, "outcome", outcome + (f": {note}" if note else ""))


def awaiting(conn):
    placeholders = ", ".join("?" for _ in AWAITING)
    return conn.execute(
        f"SELECT * FROM runs WHERE status IN ({placeholders}) ORDER BY id", AWAITING
    ).fetchall()


def old_failed(conn, before):
    """Failed runs closed before this time whose worktree is still recorded.

    Only the latest run on its branch counts, and only while the branch has no
    active run, since any later run works in the same directory.
    """
    active = ", ".join("?" for _ in ACTIVE)
    return conn.execute(
        f"""SELECT * FROM runs AS r
            WHERE status = ? AND worktree IS NOT NULL AND finished_at < ?
              AND NOT EXISTS (SELECT 1 FROM runs n JOIN agent_slots s ON s.run_id = n.id
                              WHERE n.issue_id = r.issue_id OR n.worktree = r.worktree)
              AND NOT EXISTS (SELECT 1 FROM runs n WHERE n.issue_id = r.issue_id
                              AND n.status IN ('queued', 'running'))
              AND NOT EXISTS (SELECT 1 FROM runs AS n
                              WHERE n.project = r.project AND n.branch = r.branch
                                AND (n.id > r.id OR n.status IN ({active})))
            ORDER BY id""",
        (FAILED, before, *ACTIVE),
    ).fetchall()


def forget_worktree(conn, run_id):
    """The run's worktree is gone, so it is no longer considered for removal."""
    with conn:
        conn.execute("UPDATE runs SET worktree = NULL WHERE id = ?", (run_id,))


INTERRUPTED_TOO_OFTEN = (
    f"DD stopped while this run was in progress, {RESTART_LIMIT} times in a row, "
    "so it was not retried again. Check the branch, then move the story back to In Progress "
    "to try again."
)


def interrupt_running(conn):
    """Anything left 'running' at startup was interrupted mid-flight. Returns those runs, updated.

    Each closes as interrupted, so the story can be queued again: its committed
    or pushed work is resumed as for any earlier run, and pushes are never
    forced. A story interrupted RESTART_LIMIT times in a row moves to
    awaiting_blocked instead, which keeps it active until the board shows it
    Blocked, so a story that keeps stopping DD can't loop forever.
    """
    with conn:
        conn.execute("DELETE FROM agent_slots")
        interrupted = conn.execute("SELECT * FROM runs WHERE status = ? ORDER BY id", (RUNNING,)).fetchall()
    for row in interrupted:
        if row["hold_data"] is not None:
            with conn:
                conn.execute("UPDATE runs SET status = ? WHERE id = ?", (HELD, row["id"]))
        elif row["kind"] == "story" and len(restarts(conn, row["issue_id"], row["id"])) + 1 >= RESTART_LIMIT:
            await_blocked(conn, row["id"], INTERRUPTED_TOO_OFTEN)
        elif row["kind"] == "refresh":
            close(conn, row["id"], INTERRUPTED, note="DD restarted during this refresh; the next refresh check retries it.")
        elif row["kind"] == MAIN_CHECK:
            close(conn, row["id"], INTERRUPTED, note="DD restarted during this main check; the next check retries it.")
        else:
            close(conn, row["id"], INTERRUPTED, note="DD restarted during this run; the story is picked back up.")
    return [get(conn, row["id"]) for row in interrupted]


def repair_attempts(conn, run_id):
    return get(conn, run_id)["repair_attempts"]


def record_repair(conn, run_id):
    with conn:
        conn.execute("UPDATE runs SET repair_attempts = repair_attempts + 1 WHERE id = ?", (run_id,))


def record_combine(conn, run_id, sha, clean, note=None):
    """A "Combine latest main" commit DD made. Clean means main merged in without
    conflicts and no agent touched the result, so it can't void an approval.
    note says what was combined, for a later run that uploads this commit."""
    with conn:
        conn.execute("INSERT OR REPLACE INTO combine_commits (sha, run_id, clean, note) VALUES (?, ?, ?, ?)",
                     (sha, run_id, int(clean), scrub(note) if note else None))


def combine_commit(conn, sha):
    """The combine commit with its run's kind, or None if DD didn't make it by combining."""
    return conn.execute(
        "SELECT c.*, r.kind FROM combine_commits c LEFT JOIN runs r ON r.id = c.run_id WHERE c.sha = ?",
        (sha,),
    ).fetchone()


def combined_note(conn, issue_id):
    """What the story's latest combine commit says it combined, or None if DD never combined it."""
    row = conn.execute(
        "SELECT c.note FROM combine_commits c JOIN runs r ON r.id = c.run_id "
        "WHERE r.issue_id = ? AND c.note IS NOT NULL ORDER BY c.rowid DESC LIMIT 1",
        (issue_id,),
    ).fetchone()
    return row["note"] if row else None


def review_candidates(conn, project):
    """Latest successful PR for each story DD built in this project."""
    return conn.execute(
        "SELECT * FROM runs WHERE id IN (SELECT MAX(id) FROM runs "
        "WHERE project = ? AND status = ? AND pr_url IS NOT NULL GROUP BY issue_id)",
        (project, DONE),
    ).fetchall()


def defer_run(conn, run_id):
    """Main turned red after the claim but before the worker started."""
    with conn:
        conn.execute("UPDATE runs SET status = ? WHERE id = ?", (QUEUED, run_id))

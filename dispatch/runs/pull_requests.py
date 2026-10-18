"""Pull request status lights, approval notices and settled pull requests."""


def pr_settled(conn, pr_url):
    """Whether a pull request's merge or close has already been dealt with."""
    return conn.execute("SELECT 1 FROM settled_prs WHERE pr_url = ?", (pr_url,)).fetchone() is not None


def settle_pr(conn, pr_url, outcome):
    with conn:
        conn.execute("INSERT OR IGNORE INTO settled_prs VALUES (?, ?)", (pr_url, outcome))


def pull_request(conn, project, issue_id):
    return conn.execute(
        "SELECT pr_url, branch FROM runs WHERE project = ? AND issue_id = ? AND pr_url IS NOT NULL "
        "ORDER BY id DESC LIMIT 1", (project, issue_id),
    ).fetchone()


def light(conn, project, issue_id):
    return conn.execute("SELECT * FROM pr_lights WHERE project = ? AND issue_id = ?",
                        (project, issue_id)).fetchone()


def save_light(conn, project, issue_id, state, disposition, head=None, shown=None):
    with conn:
        conn.execute(
            "INSERT INTO pr_lights VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(project, issue_id) DO UPDATE SET state = excluded.state, "
            "head = excluded.head, shown = excluded.shown, disposition = excluded.disposition",
            (project, issue_id, state, head, shown, disposition))


def owed_lights(conn):
    return conn.execute("SELECT * FROM pr_lights WHERE disposition = 'owed'").fetchall()


def unapproved_lights(conn):
    return conn.execute("SELECT * FROM pr_lights WHERE disposition = 'set' AND state = 'success' "
                        "AND shown IN ('waiting', 'changed')").fetchall()


def approval_noticed(conn, project, issue_id, sha):
    return conn.execute("SELECT 1 FROM approval_notices WHERE project = ? AND issue_id = ? AND sha = ?",
                        (project, issue_id, sha)).fetchone() is not None


def mark_approval_noticed(conn, project, issue_id, sha):
    with conn:
        conn.execute("INSERT OR IGNORE INTO approval_notices VALUES (?, ?, ?)", (project, issue_id, sha))

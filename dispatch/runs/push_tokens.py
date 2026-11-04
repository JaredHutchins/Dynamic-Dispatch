"""The Git push token's warning work items and the alerts and checks already done for it.

A token is known only by its fingerprint. A refused token's item has an empty expiry.
"""


def claim_push_token_notice(conn, key):
    """True the first time key is claimed, so a check or alert happens once, across restarts too."""
    with conn:
        return conn.execute("INSERT OR IGNORE INTO push_token_notices VALUES (?)", (key,)).rowcount == 1


def push_token_item(conn, project, token, expiry):
    return conn.execute("SELECT * FROM push_token_items WHERE project = ? AND token = ? AND expiry = ?",
                        (project, token, expiry)).fetchone()


def save_push_token_item(conn, project, token, expiry, issue_id):
    with conn:
        conn.execute("INSERT OR IGNORE INTO push_token_items (project, token, expiry, issue_id) VALUES (?, ?, ?, ?)",
                     (project, token, expiry, issue_id))


def open_push_token_items(conn, project):
    return conn.execute("SELECT * FROM push_token_items WHERE project = ? AND closed = 0 ORDER BY rowid",
                        (project,)).fetchall()


def close_push_token_item(conn, project, token, expiry):
    with conn:
        conn.execute("UPDATE push_token_items SET closed = 1 WHERE project = ? AND token = ? AND expiry = ?",
                     (project, token, expiry))

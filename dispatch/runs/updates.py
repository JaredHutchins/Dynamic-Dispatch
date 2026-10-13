"""Self-update notices and restarts, and the rollback state kept outside the database."""

import json
import os
import tempfile
from pathlib import Path

from ..redact import scrub


def claim_self_update_notice(conn, revision, reason):
    """Mark the alert about not updating DD to revision for this reason. False if it was already sent."""
    with conn:
        return conn.execute("INSERT OR IGNORE INTO self_update_notices VALUES (?, ?)",
                            (revision, reason)).rowcount == 1


def save_self_update_restart(conn, project, revision, title):
    """DD is restarting onto revision; the started copy owes one alert saying so."""
    with conn:
        conn.execute("INSERT OR REPLACE INTO self_update_restart VALUES (1, ?, ?, ?)",
                     (project, revision, scrub(title)))


def claim_self_update_restart(conn):
    """Take the owed restart alert, so it is sent once. None if none is owed."""
    with conn:
        return conn.execute("DELETE FROM self_update_restart RETURNING *").fetchone()


# Separate JSON lets the installed recovery helper work without DD or SQLite.
def rollback_state(checkout):
    try:
        return json.loads((Path(checkout) / ".dd-recovery" / "state.json").read_text())
    except FileNotFoundError:
        return {}


def save_rollback_state(checkout, state):
    folder = Path(checkout) / ".dd-recovery"
    folder.mkdir(mode=0o700, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=folder, delete=False) as out:
            name = out.name
            json.dump(state, out)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, folder / "state.json")
        fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)

import tempfile
import unittest
from pathlib import Path

from dispatch import runs


class LightMigration(unittest.TestCase):
    def test_imports_latest_state_and_all_notices_only_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runs.db"
            conn = runs.connect(path)
            self.addCleanup(conn.close)
            with conn:
                conn.execute("DELETE FROM migrations")
            history = [
                ("a", "light", "success old waiting"),
                ("a", "light", "success new approved"),
                ("b", "light", "success old"),
                ("b", "light_owed", "failure"),
                ("c", "light_left", "success untested: DD has not tested this commit"),
                ("d", "light_left", "success closed: merged or closed pull requests are left alone"),
                ("e", "light", "success head changed"),
                ("a", "approval_voided", "first"),
                ("a", "approval_voided", "second"),
            ]
            for issue, kind, detail in history:
                runs.record_event(conn, None, kind, detail, project="p", issue_id=issue)
            conn.close()
            conn = runs.connect(path)
            self.addCleanup(conn.close)
            self.assertEqual((runs.light(conn, "p", "a")["head"], runs.light(conn, "p", "a")["shown"]),
                             ("new", "approved"))
            self.assertEqual([r["issue_id"] for r in runs.owed_lights(conn)], ["b"])
            self.assertEqual([r["issue_id"] for r in runs.unapproved_lights(conn)], ["e"])
            self.assertEqual(runs.light(conn, "p", "c")["head"], "untested")
            self.assertEqual(runs.light(conn, "p", "d")["disposition"], "left")
            for sha in ("first", "second"):
                self.assertTrue(runs.approval_noticed(conn, "p", "a", sha))
            runs.save_light(conn, "p", "a", "reworking", "set", "latest", "reworking")
            runs.record_event(conn, None, "light_owed", "failure", project="p", issue_id="a")
            conn.close()
            conn = runs.connect(path)
            self.addCleanup(conn.close)
            self.assertEqual(runs.light(conn, "p", "a")["head"], "latest")
            self.assertEqual(runs.light(conn, "p", "a")["disposition"], "set")

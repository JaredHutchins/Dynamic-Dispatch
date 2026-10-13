"""Tests for redaction."""

import json, os
from dispatch import redact
from helpers import (
    Base,
    CLAUDE,
    GH,
    MODEL,
    ORCH,
    board,
    git,
    os,
    prs,
)


class Redaction(Base):
    def test_secrets_never_leave(self):
        os.environ["LEAK"] = f"{GH} and {MODEL}"
        self.add_issue(); self.cycle()
        everything = json.dumps([c[2] for c in board.comments]) + json.dumps(prs) + json.dumps(self.run_rows())
        for secret in (GH, MODEL, ORCH, CLAUDE):
            self.assertNotIn(secret, everything)
        self.assertIn("[redacted]", board.comments[0][2])

    def test_scrub_longest_first(self):
        redact.set_secrets(["abcdefgh", "abcdefghijkl"])
        self.assertEqual(redact.scrub("x abcdefghijkl y"), "x [redacted] y")



class SecondReview(Base):
    def test_issue_name_scrubbed_on_enqueue_and_in_commit(self):
        self.add_issue(); board.issues["i1"]["name"] = f"Rotate {GH} please"
        self.cycle()
        [row] = self.run_rows()
        self.assertEqual(row["issue_name"], "Rotate [redacted] please")
        subject = git("log", "-1", "--format=%s", "dd-7", cwd=self.origin)
        self.assertNotIn(GH, subject); self.assertIn("[redacted]", subject)


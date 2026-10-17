"""Tests for redaction."""

import contextlib
import io
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
    def test_project_secrets_never_leave(self):
        secret = 'fake-project-test-value'
        path = self.tmp / 'test.env'
        path.write_text('TEST_PRIVATE=' + secret + '\n')
        path.chmod(0o600)
        project = self.cfg['projects']['dd']
        project['test_env_file'] = str(path)
        redact.add_test_secrets(project)
        os.environ['LEAK'] = secret + ' xyz'
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            redact.log(secret + ' xyz')
            self.add_issue()
            self.cycle()
        everything = (json.dumps([c[2] for c in board.comments]) + json.dumps(prs)
                      + json.dumps(self.run_rows()) + output.getvalue())
        self.assertNotIn(secret, everything)
        self.assertIn('xyz', everything)
        self.assertIn('[redacted]', board.comments[0][2])

    def test_secrets_never_leave(self):
        os.environ["LEAK"] = f"{GH} and {MODEL}"
        self.add_issue(); self.cycle()
        everything = json.dumps([c[2] for c in board.comments]) + json.dumps(prs) + json.dumps(self.run_rows())
        for secret in (GH, MODEL, ORCH, CLAUDE):
            self.assertNotIn(secret, everything)
        self.assertIn("[redacted]", board.comments[0][2])

    def test_plane_app_secrets_are_scrubbed(self):
        self.addCleanup(redact.set_secrets, [])
        cfg = {"plane": {"orchestrator_bot": {"client_id": "fake-client-id",
                                              "client_secret": "fake-orchestrator-app-secret"}},
               "github": {}, "agents": {"a": {"plane_token": "fake-agent-plane-token"},
                                        "b": {"plane_bot": {"client_secret": "fake-agent-app-secret"}}}}
        redact.configure(cfg)
        text = redact.scrub("fake-orchestrator-app-secret fake-agent-app-secret fake-agent-plane-token fake-client-id")
        self.assertEqual(text, "[redacted] [redacted] [redacted] fake-client-id")

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

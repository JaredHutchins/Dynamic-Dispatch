"""Tests for github app."""

import subprocess, tempfile
from unittest import mock
from pathlib import Path
from datetime import datetime, timezone
from dispatch import github, redact, runs
from helpers import (
    APP_TOKEN,
    Base,
    GH,
    S_BLK,
    app,
    board,
    gh_tokens,
    prs,
    statuses,
)


class GitHubApp(Base):
    @classmethod
    def setUpClass(cls):
        # A throwaway key, made for this test run only.
        directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(directory.cleanup)
        cls.keydir = Path(directory.name)
        cls.key, cls.public = cls.keydir / "app.pem", cls.keydir / "app.pub"
        subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048",
                        "-out", str(cls.key)], check=True, capture_output=True)
        subprocess.run(["openssl", "pkey", "-in", str(cls.key), "-pubout", "-out", str(cls.public)],
                       check=True, capture_output=True)
        cls.key.chmod(0o600)

    def use_app(self):
        github._installation_tokens.clear()
        self.cfg["github"].update(app_id=1234, installation_id=42, private_key_path=str(self.key))
        app["public_key"] = str(self.public)

    def test_installation_token_reused_and_renewed_before_expiry(self):
        self.use_app()
        start = datetime(2026, 9, 28, 11, tzinfo=timezone.utc).timestamp()
        with mock.patch.object(github.time, "time", return_value=start) as clock, \
                mock.patch.object(github, "_request", wraps=github._request) as request:
            for _ in range(3):
                github.existing_pr("owner/repo", "branch", github.token(self.cfg["github"]))
            self.assertEqual(sum(c.args[0] == "POST" for c in request.call_args_list), 1)
            clock.return_value = start + 3600 - github.TOKEN_REFRESH_MARGIN
            github.token(self.cfg["github"])
            self.assertEqual(sum(c.args[0] == "POST" for c in request.call_args_list), 2)
            clock.return_value = start + 3601
            github.token(self.cfg["github"])
            self.assertEqual(sum(c.args[0] == "POST" for c in request.call_args_list), 3)
        self.assertNotIn(APP_TOKEN, redact.scrub(APP_TOKEN))

    def test_pr_opened_with_the_app_token(self):
        self.use_app()
        self.add_issue(); self.cycle()
        self.assertEqual(self.run_rows()[0]["status"], runs.DONE)
        self.assertEqual(len(prs), 1)
        # Looked up, opened, then its light: the head read and the status set.
        self.assertEqual(gh_tokens, [APP_TOKEN] * 4)
        self.assertEqual(len(statuses), 1)
        self.assertEqual(redact.scrub(f"x {APP_TOKEN} y"), "x [redacted] y")

    def test_app_token_is_never_written_to_disk(self):
        self.use_app()
        self.add_issue(); self.cycle()
        self.conn.close()
        for path in self.tmp.rglob("*"):
            if path.is_file():
                self.assertNotIn(APP_TOKEN.encode(), path.read_bytes(), path)

    def test_without_the_app_the_plain_token_is_used(self):
        self.add_issue(); self.cycle()
        self.assertEqual(self.run_rows()[0]["status"], runs.DONE)
        self.assertEqual(gh_tokens, [GH] * 4)

    def test_failed_token_request_blocks_the_story(self):
        self.use_app()
        app["fail"] = "POST /app/installations/42/access_tokens -> 401: A JSON web token could not be decoded"
        self.add_issue(); self.cycle()
        [row] = self.run_rows()
        self.assertEqual(row["status"], runs.FAILED, row)
        self.assertEqual(board.issues["i1"]["state"], {"id": S_BLK})
        comment = board.comments[0][2]
        self.assertIn("pull request could not be opened", comment)
        self.assertIn("Could not get a GitHub App installation token", comment)
        self.assertIn("401", comment)
        self.assertEqual(prs, {})
        self.assertEqual(gh_tokens, [])

    def test_unusable_key_blocks_the_story(self):
        self.use_app()
        bad = self.tmp / "bad.pem"; bad.write_text("not a key\n"); bad.chmod(0o600)
        self.cfg["github"]["private_key_path"] = str(bad)
        self.add_issue(); self.cycle()
        self.assertEqual(board.issues["i1"]["state"], {"id": S_BLK})
        self.assertIn("openssl could not sign the GitHub App JWT", board.comments[0][2])
        self.assertEqual(prs, {})


"""Gemini CLI as an agent, using sample outputs in samples/gemini and never a real Gemini."""

import json, os, tempfile, unittest
from pathlib import Path
from unittest import mock
from dispatch import agents, alerts, logins, runs
import main as dd_main
from helpers import (
    Base,
    CODEX,
    HERE,
    MODEL,
    PY,
    S_BLK,
    S_REV,
    board,
    sandbox_calls,
)

SAMPLES = HERE / "samples" / "gemini"


def sample(name):
    return (SAMPLES / name).read_text()


class Parser(unittest.TestCase):
    def test_success_gives_response_and_tokens_summed_across_models(self):
        parsed = agents.parse_gemini_json(sample("success.stdout.json"), sample("success.stderr.txt"))
        self.assertEqual(parsed, {
            "ok": True, "summary": "Added a Gemini parser.\n\nTests pass.",
            "usage": {"input": 4488, "prompt": 25751, "candidates": 1060, "total": 26965,
                      "cached": 21263, "thoughts": 154, "tool": 0},
            "cost_usd": None, "usage_limit": False, "usage_limit_until": None})

    def test_error_gives_message_type_and_code(self):
        parsed = agents.parse_gemini_json("", sample("error.stderr.txt"))
        self.assertFalse(parsed["ok"])
        self.assertFalse(parsed["usage_limit"])
        self.assertIn("was not found or your project does not have access to it.]", parsed["summary"])
        self.assertTrue(parsed["summary"].endswith("(Error, 404)"))

    def test_error_on_stdout_is_read_too(self):
        parsed = agents.parse_gemini_json(sample("config.stderr.txt"))
        self.assertEqual((parsed["ok"], parsed["usage_limit"]), (False, False))
        self.assertIn("Could not load the default credentials", parsed["summary"])

    def test_quota_is_a_usage_limit_with_no_reset_time(self):
        parsed = agents.parse_gemini_json("", sample("quota.stderr.txt"))
        self.assertEqual((parsed["ok"], parsed["usage_limit"], parsed["usage_limit_until"]), (False, True, None))

    def test_quota_wording_is_matched_broadly(self):
        for message, code in (("Resource exhausted. Please try again later.", None),
                              ("RESOURCE_EXHAUSTED", None),
                              ("Quota exceeded for quota metric 'Generate content requests'", None),
                              ("You have exhausted your capacity on this model.", None),
                              ("Too Many Requests", None),
                              ("[API Error: rate limited]", None),
                              ("Request failed", 429)):
            with self.subTest(message=message):
                error = {"type": "Error", "message": message, "code": code}
                parsed = agents.parse_gemini_json(json.dumps({"error": error}))
                self.assertTrue(parsed["usage_limit"])
        for message in ("Your application has no quota project set.", "Model not found", "Internal error 500"):
            with self.subTest(message=message):
                parsed = agents.parse_gemini_json(json.dumps({"error": {"message": message, "code": 1}}))
                self.assertFalse(parsed["usage_limit"])

    def test_response_mentioning_quota_is_not_a_limit(self):
        parsed = agents.parse_gemini_json(json.dumps({"response": "Fixed the 429 quota handling.", "stats": {}}))
        self.assertEqual((parsed["ok"], parsed["usage_limit"]), (True, False))

    def test_unreadable_output_raises_agent_error(self):
        for stdout, stderr in (("", ""), ("not json", "Loaded cached credentials."), ("[1, 2]", ""),
                               (json.dumps({"stats": {}}), ""), (json.dumps({"error": None}), "")):
            with self.subTest(stdout=stdout), self.assertRaises(agents.AgentError):
                agents.parse_gemini_json(stdout, stderr)

    def test_odd_shapes_do_not_crash(self):
        parsed = agents.parse_gemini_json(json.dumps({
            "response": 5, "stats": {"models": {"a": {"tokens": {"total": "7", "prompt": True}}, "b": "odd",
                                                "c": {"tokens": {"total": 3}}}}}))
        self.assertEqual((parsed["ok"], parsed["summary"], parsed["usage"]), (True, None, {"total": 3}))
        parsed = agents.parse_gemini_json(json.dumps({"error": "odd"}))
        self.assertEqual((parsed["ok"], parsed["summary"]), (False, "odd"))


class Logins(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        cfg = {'db_path': str(Path(directory.name) / 'runs.db'), 'github': {'token': 'fake-test-token'}}
        runs.connect(cfg['db_path']).close()
        self.cfg = cfg
        patch = mock.patch.object(logins, '_health', alerts.Health(cfg))
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(alerts, 'send')
        self.send = patch.start()
        self.addCleanup(patch.stop)

    def run_agent(self, returncode, stdout='', stderr='', run_as='dd-gemini'):
        result = mock.Mock(returncode=returncode, timed_out=False, stdout=stdout, stderr=stderr,
                           stdout_truncated=False)
        agent_cfg = dict(parser='gemini_json', display_name='Gemini', command=['gemini'], run_as=run_as)
        with mock.patch.object(agents.proc, 'run_sandboxed', return_value=result):
            return agents.run(self.cfg, agent_cfg, 'prompt', '/fake')

    def test_rejected_logins_alert(self):
        for name, code in (('login.stderr.txt', 1), ('config.stderr.txt', 52)):
            with self.subTest(sample=name):
                self.send.reset_mock()
                self.run_agent(code, stderr=sample(name), run_as=name)
                self.send.assert_called_once()
                self.assertIn('Gemini login', self.send.call_args.args[3])

    def test_login_wording_is_matched_broadly(self):
        for message in ('Request had invalid authentication credentials.', 'UNAUTHENTICATED', 'HTTP 401',
                        'Missing credentials', 'Could not load the default credentials.',
                        'API key not valid. Please pass a valid API key.', 'invalid_grant',
                        'Please set an Auth method in your settings.json'):
            with self.subTest(message=message):
                self.send.reset_mock()
                self.run_agent(1, stderr=json.dumps({'error': {'type': 'Error', 'message': message, 'code': 1}}),
                               run_as=message)
                self.send.assert_called_once()

    def test_other_failures_and_other_agents_wording_do_not_alert(self):
        for message in ('Please run /login', 'Token refresh failed', 'Model not found'):
            with self.subTest(message=message):
                self.run_agent(1, stderr=json.dumps({'error': {'type': 'Error', 'message': message, 'code': 1}}))
        self.run_agent(1, stderr=sample('quota.stderr.txt'))
        self.run_agent(1, stderr=sample('error.stderr.txt'))
        self.send.assert_not_called()

    def test_response_mentioning_logins_does_not_alert(self):
        self.run_agent(0, stdout=json.dumps({'response': '401 UNAUTHENTICATED invalid credentials'}))
        self.send.assert_not_called()

    def test_success_rearms_the_alert(self):
        for code, stdout, stderr in ((1, '', sample('login.stderr.txt')), (0, sample('success.stdout.json'), ''),
                                     (1, '', sample('login.stderr.txt'))):
            self.run_agent(code, stdout, stderr)
        self.assertEqual(self.send.call_count, 2)


class Workflow(Base):
    """Gemini as the only agent, through the whole run lifecycle."""

    def setUp(self):
        super().setUp()
        self.cfg["agents"] = {"gemini": {
            # The fake Plane knows only the Claude and Codex users; Gemini borrows the second.
            "display_name": "Gemini", "plane_token": CODEX, "prompt_via": "stdin",
            "command": [PY, str(HERE / "fake_agent.py"), "-", "edit", "gemini"],
            "parser": "gemini_json", "run_as": "dd-gemini", "env_file": "/etc/dynamic-dispatch/gemini.env",
            "git_author": {"name": "Gemini", "email": "gemini@example.com"},
            "timeout_seconds": 60}}
        self.cfg["projects"]["dd"]["default_agent"] = "gemini"

    def set_mode(self, mode):
        self.cfg["agents"]["gemini"]["command"][3] = mode

    def test_success_opens_pull_request_with_response_and_usage(self):
        self.add_issue(); self.cycle()
        [row] = self.run_rows()
        self.assertEqual(row["status"], runs.DONE)
        self.assertEqual(board.issues["i1"]["state"], {"id": S_REV})
        self.assertEqual(json.loads(row["usage"])["total"], 26965)
        self.assertEqual({c["run_as"] for c in sandbox_calls}, {"dd-gemini"})
        self.assertIn("Changed things", board.comments[0][2])

    def test_error_blocks_with_geminis_message_scrubbed(self):
        os.environ["LEAK"] = MODEL
        self.set_mode("error"); self.add_issue(); self.cycle()
        [row] = self.run_rows()
        self.assertEqual(row["status"], runs.FAILED)
        self.assertEqual(board.issues["i1"]["state"], {"id": S_BLK})
        comment = board.comments[0][2]
        self.assertIn("Exited with code 1", comment)
        self.assertIn("was not found or your project does not have access to it", comment)
        self.assertNotIn(MODEL, comment)
        self.assertNotIn(MODEL, json.dumps([dict(e) for e in runs.events(self.conn)]))
        self.assertEqual(runs.cooldowns(self.conn, runs.now()), {})

    def test_config_refusal_blocks(self):
        self.set_mode("config"); self.add_issue(); self.cycle()
        self.assertEqual(board.issues["i1"]["state"], {"id": S_BLK})
        self.assertIn("Exited with code 52", board.comments[0][2])
        self.assertIn("Could not load the default credentials", board.comments[0][2])

    def test_quota_cools_down_with_default_length_instead_of_blocking(self):
        self.cfg["usage_limit_cooldown_minutes"] = 5
        self.set_mode("limit"); self.add_issue(); self.cycle()
        [row] = self.run_rows()
        self.assertEqual(row["status"], runs.LIMITED)
        self.assertNotEqual(board.issues["i1"]["state"], {"id": S_BLK})
        self.assertEqual(list(runs.cooldowns(self.conn, runs.now())), ["gemini"])
        self.assertIn("Gemini is resting after its usage limit", board.comments[0][2])
        self.assertIn("(in about 5 minutes)", board.comments[0][2])
        self.assertTrue(any("Reset time source: fallback" in e["detail"] for e in runs.events(self.conn)))


if __name__ == "__main__":
    unittest.main()

"""Every pull request updates its documentation or says why none is needed."""

import json
from pathlib import Path
import unittest
from unittest import mock

from dispatch import agents, config, docs_line, runs, worker
from helpers import Base, MainChanges, REPO_CODE, S_BLK, S_IP, S_REV, board, git, prs, temporary_directory

DEFAULTS = config.docs({})
UPDATED = "Added VALUE.\n\nDocs: updated README.md"
REASON = "Added VALUE.\n\nDocs: none needed, because VALUE is internal."
MISSING = "Added VALUE."


class Lines(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(DEFAULTS, {"check": True, "documentation": ["README.md", "docs/**"],
                                    "exempt": ["tests/**", "test/**"]})
        self.assertEqual(config.docs({"docs": {"check": False}}),
                         dict(DEFAULTS, check=False))
        self.assertEqual(config.docs({"docs": {"documentation": ["guide.md"]}})["exempt"], DEFAULTS["exempt"])
        config.docs({})["documentation"].append("changed.md")
        self.assertEqual(config.DEFAULT_DOCS["documentation"], ["README.md", "docs/**"])

    def test_patterns(self):
        for path, patterns, expected in (
                ("README.md", ["README.md"], True),
                ("docs/README.md", ["README.md"], False),
                ("docs/a/b.md", ["docs/**"], True),
                ("deploy/README.md", ["deploy/*.md"], True),
                ("deploy/sub/README.md", ["deploy/*.md"], False),
                ("tests/test_x.py", ["tests/**"], True),
                ("a/b/notes.md", ["**/notes.md"], True),
                ("notes.md", ["**/notes.md"], True),
                ("app.py", ["docs/**", "tests/**"], False)):
            with self.subTest(path=path, patterns=patterns):
                self.assertEqual(docs_line.matches(path, patterns), expected)

    def test_update_listed_correctly(self):
        self.assertIsNone(docs_line.problem(DEFAULTS, UPDATED, ["README.md", "app.py"]))
        self.assertIsNone(docs_line.problem(
            DEFAULTS, "Done.\n**Docs:** updated `README.md`, `docs/guide.md` and docs/setup.md (new section).\n\n",
            ["README.md", "app.py", "docs/guide.md", "docs/setup.md"]))

    def test_reason_given(self):
        self.assertIsNone(docs_line.problem(DEFAULTS, REASON, ["app.py"]))

    def test_missing_line(self):
        found = docs_line.problem(DEFAULTS, MISSING, ["app.py"])
        self.assertIn("doesn't end with a Docs line", found)
        self.assertIn("app.py", found)
        # Only the last line counts.
        self.assertIsNotNone(docs_line.problem(DEFAULTS, REASON + "\nOne more thing.", ["app.py"]))

    def test_named_file_that_was_not_changed(self):
        found = docs_line.problem(DEFAULTS, UPDATED, ["app.py"])
        self.assertIn("doesn't change: README.md", found)
        found = docs_line.problem(DEFAULTS, "Docs: updated app.py", ["app.py"])
        self.assertIn("don't count as documentation: app.py", found)

    def test_code_changed_with_no_reason(self):
        for summary in ("Docs: none needed, because", "Docs: none needed, because .", "Docs: updated",
                        "Docs: looked at it"):
            with self.subTest(summary=summary):
                self.assertIsNotNone(docs_line.problem(DEFAULTS, summary, ["app.py"]))

    def test_tests_and_documentation_only_changes_need_no_line(self):
        self.assertIsNone(docs_line.problem(DEFAULTS, MISSING, ["tests/test_app.py", "test/data.txt"]))
        self.assertIsNone(docs_line.problem(DEFAULTS, MISSING, ["README.md", "docs/a.md"]))
        self.assertIsNone(docs_line.problem(DEFAULTS, MISSING, []))
        # A line that is given is still checked.
        self.assertIsNotNone(docs_line.problem(DEFAULTS, UPDATED, ["tests/test_app.py"]))

    def test_check_turned_off(self):
        off = dict(DEFAULTS, check=False)
        self.assertIsNone(docs_line.problem(off, MISSING, ["app.py"]))
        self.assertEqual(docs_line.instruction(off), "")

    def test_instruction_names_the_forms_and_paths(self):
        text = docs_line.instruction(DEFAULTS)
        for phrase in ("Docs: updated <files>", "Docs: none needed, because <reason>",
                       "README.md, docs/**", "tests/**, test/**", "very last line"):
            self.assertIn(phrase, text)
        self.assertNotIn("its own Docs line", text)
        self.assertIn("This round needs its own Docs line", docs_line.instruction(DEFAULTS, rework=True))


class Settings(unittest.TestCase):
    def load(self, docs):
        c = json.loads((REPO_CODE / "config.example.json").read_text())
        key = temporary_directory(self) / "app.pem"
        key.write_text("fake key\n"); key.chmod(0o600)
        c["github"]["private_key_path"] = str(key)
        if docs is not None:
            c["projects"]["dynamic-dispatch"]["docs"] = docs
        path = temporary_directory(self) / "config.json"
        path.write_text(json.dumps(c)); path.chmod(0o600)
        return config.load(path)["projects"]["dynamic-dispatch"]

    def test_example_lists_dds_own_documentation(self):
        settings = config.docs(self.load(None))
        for path in ("README.md", "ARCHITECTURE.md", "SECURITY.md", "DEFINITION_OF_DONE.md", "REVIEW_RUBRIC.md",
                     "STORY_TEMPLATE.md", "deploy/README.md", "deploy/CONTAINER.md"):
            self.assertTrue(docs_line.matches(path, settings["documentation"]), path)
        self.assertFalse(docs_line.matches("deploy/dd-install", settings["documentation"]))

    def test_optional_and_validated(self):
        self.assertEqual(config.docs(self.load({})), DEFAULTS)
        self.assertFalse(config.docs(self.load({"check": False}))["check"])
        for bad in ([], "README.md", {"check": "yes"}, {"documentation": "README.md"},
                    {"documentation": [""]}, {"exempt": ["/etc/x"]}, {"documentation": []},
                    {"documentation": [3]}, {"skip": True}):
            with self.subTest(bad=bad), self.assertRaisesRegex(config.ConfigError, "docs"):
                self.load(bad)
        self.assertFalse(config.docs(self.load({"check": False, "documentation": []}))["check"])


class Agent:
    """Stands in for the agent: each call writes some files and gives a summary."""

    def __init__(self, test, *answers):
        self.answers = list(answers)
        self.prompts = []

    def __call__(self, cfg, agent_cfg, prompt, worktree):
        self.prompts.append(prompt)
        files, summary = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        for name in files:
            path = Path(worktree) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(path.read_text() + "more\n" if path.exists() else "new\n")
        return {"ok": True, "summary": summary, "usage": {"output_tokens": 1}, "cost_usd": 0.01}


class Runs(Base, MainChanges):
    def build(self, *answers):
        agent = Agent(self, *answers)
        with mock.patch.object(agents, "run", side_effect=agent):
            self.cycle()
        return self.run_rows()[-1], agent.prompts

    def test_update_listed_correctly_opens_the_pull_request(self):
        self.add_issue()
        row, prompts = self.build((["feature.py", "README.md"], UPDATED))
        self.assertEqual(row["status"], runs.DONE, row)
        self.assertEqual(len(prompts), 1)
        self.assertIn("Docs: updated <files>", prompts[0])
        self.assertIn("Docs: updated README.md", prs["dd-7"]["body"])

    def test_reason_given_opens_the_pull_request(self):
        self.add_issue()
        row, prompts = self.build((["feature.py"], REASON))
        self.assertEqual((row["status"], len(prompts)), (runs.DONE, 1))

    def test_missing_line_is_sent_back_until_it_is_right(self):
        self.add_issue()
        row, prompts = self.build((["feature.py"], MISSING), (["README.md"], UPDATED))
        self.assertEqual(row["status"], runs.DONE, row)
        self.assertEqual(len(prompts), 2)
        self.assertIn("doesn't end with a Docs line", prompts[1])
        self.assertIn("Files this branch changes: feature.py", prompts[1])
        self.assertIn("Your earlier summary:\nAdded VALUE.", prompts[1])
        self.assertTrue(prs["dd-7"]["body"].startswith(UPDATED))
        # The fix is committed with the rest of the work.
        self.assertIn("README.md", git("show", "--name-only", "--format=", "dd-7", cwd=self.origin))
        types = [e["type"] for e in runs.events(self.conn)]
        self.assertIn("docs_line_wrong", types)
        self.assertIn("sent_back", types)
        self.assertTrue(any("try 1 of 2" in c[2] for c in board.comments))
        self.assertEqual(row["cost_usd"], 0.02)

    def test_line_still_wrong_after_two_tries_blocks_without_a_pull_request(self):
        self.add_issue()
        row, prompts = self.build((["feature.py"], MISSING))
        self.assertEqual(row["status"], runs.FAILED)
        self.assertEqual(len(prompts), 1 + worker.DOCS_TRIES)
        self.assertIn("still wrong after 2 tries", row["error"])
        self.assertEqual(prs, {})
        self.assertFalse(row["pushed"])
        self.assertEqual(board.issues["i1"]["state"], {"id": S_BLK})

    def test_named_file_that_was_not_changed_is_sent_back(self):
        self.add_issue()
        row, prompts = self.build((["feature.py"], UPDATED), (["feature.py"], REASON))
        self.assertEqual((row["status"], len(prompts)), (runs.DONE, 2))
        self.assertIn("doesn't change: README.md", prompts[1])

    def test_code_changed_with_no_reason_is_sent_back(self):
        self.add_issue()
        row, prompts = self.build((["feature.py"], "Docs: none needed, because"), ([], REASON))
        self.assertEqual((row["status"], len(prompts)), (runs.DONE, 2))
        self.assertIn("gives no reason", prompts[1])

    def test_tests_only_changes_need_no_line(self):
        self.add_issue()
        row, prompts = self.build((["tests/test_feature.py"], MISSING))
        self.assertEqual((row["status"], len(prompts)), (runs.DONE, 1))

    def test_check_turned_off(self):
        self.cfg["projects"]["dd"]["docs"] = {"check": False}
        self.add_issue()
        row, prompts = self.build((["feature.py"], MISSING))
        self.assertEqual((row["status"], len(prompts)), (runs.DONE, 1))
        self.assertNotIn("Docs line", prompts[0])

    def test_each_rework_round_needs_its_own_line(self):
        self.add_issue()
        self.build((["feature.py", "README.md"], UPDATED))
        board.issues["i1"]["state"] = {"id": S_IP}  # human sends it back
        row, prompts = self.build((["feature.py"], "Renamed VALUE."), (["feature.py"], REASON))
        self.assertEqual(row["status"], runs.DONE, row)
        self.assertEqual(len(prompts), 2)
        self.assertIn("This round needs its own Docs line", prompts[0])
        self.assertIn("doesn't end with a Docs line", prompts[1])
        self.assertTrue(prs["dd-7"]["body"].startswith(REASON))
        self.assertEqual(board.issues["i1"]["state"], {"id": S_REV})

    def test_combine_with_main_commits_are_exempt(self):
        self.add_issue()
        self.build((["feature.py"], REASON))
        self.advance_main()
        with mock.patch.object(agents, "run") as agent:
            self.cycle()  # the refresh combines main into the branch without the agent
        agent.assert_not_called()
        self.assertEqual(self.run_rows()[-1]["kind"], "refresh")
        board.issues["i1"]["state"] = {"id": S_IP}
        row, prompts = self.build((["tests/test_feature.py"], "Added a test."), ([], REASON))
        self.assertEqual((row["status"], len(prompts)), (runs.DONE, 2), row)
        # main.py came in with main, so only the story's own changes are listed.
        self.assertIn("Files this branch changes: feature.py, tests/test_feature.py\n", prompts[1])

    def test_changed_files_cover_earlier_commits_and_new_work(self):
        self.add_issue()
        self.build((["feature.py"], REASON))
        board.issues["i1"]["state"] = {"id": S_IP}
        with mock.patch.object(docs_line, "problem", wraps=docs_line.problem) as check:
            row, _ = self.build((["docs/new.md", "app.py"], REASON))
        self.assertEqual(row["status"], runs.DONE, row)
        self.assertEqual(check.call_args.args[2], ["app.py", "docs/new.md", "feature.py"])


if __name__ == "__main__":
    unittest.main()

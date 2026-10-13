"""Tests for parsers."""

import html, json, os, unittest
from unittest import mock
from datetime import datetime, timezone
from dispatch import agents, prompts, runs, text
from helpers import (
    Base,
    GH,
    os,
)


class Parsers(unittest.TestCase):
    def codex(self, *events):
        return agents.parse_codex_json("\n".join(json.dumps(e) for e in events))

    def test_reset_times(self):
        now = datetime(2026, 3, 7, 22, 0, tzinfo=timezone.utc)
        cases = [
            ("claude", "Claude AI usage limit reached|1759075200",
             datetime.fromtimestamp(1759075200, timezone.utc)),
            ("claude", "5-hour limit reached ∙ resets 3pm", "2026-03-08T20:00:00+00:00"),
            ("claude", "You've hit your limit · resets 3:40pm", "2026-03-08T20:40:00+00:00"),
            ("claude", "You've hit your limit · resets 3pm (America/Los_Angeles)",
             "2026-03-07T23:00:00+00:00"),
            ("claude", "You've hit your limit · resets 3pm [America/Chicago]",
             "2026-03-08T20:00:00+00:00"),
            ("codex", "You've hit your usage limit. Try again at 5:22 PM.", "2026-03-07T23:22:00+00:00"),
            ("codex", "Usage limit. Try again at 3:22 PM.", "2026-03-08T20:22:00+00:00"),
            ("codex", "Usage limit. Try again in 12 minutes", "2026-03-07T22:12:00+00:00"),
            ("codex", "Usage limit. Try again in 2 hours", "2026-03-08T00:00:00+00:00"),
            ("codex", "Usage limit", None),
            ("claude", "Claude AI usage limit reached", None),
            ("claude", "limit reached ∙ resets 99pm", None),
            ("claude", "limit reached ∙ resets 3pm (Unknown/Zone)", None),
        ]
        with mock.patch.dict(os.environ, {"TZ": "America/Chicago"}), \
                mock.patch.object(agents, "datetime", wraps=datetime) as clock:
            clock.now.return_value = now
            for style, message, expected in cases:
                with self.subTest(message=message):
                    parsed = (agents.parse_claude_json(json.dumps({"is_error": True, "result": message}))
                              if style == "claude" else self.codex({"type": "error", "message": message}))
                    if isinstance(expected, str):
                        expected = datetime.fromisoformat(expected)
                    self.assertTrue(parsed["usage_limit"])
                    self.assertEqual(parsed["usage_limit_until"], expected)

    def test_codex_success(self):
        parsed = self.codex({"type": "thread.started", "thread_id": "t"}, {"type": "turn.started"},
                            {"type": "item.completed", "item": {"type": "agent_message", "text": "first"}},
                            {"type": "item.completed", "item": {"type": "agent_message", "text": "Done."}},
                            {"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 4}})
        self.assertEqual(parsed, {"ok": True, "summary": "Done.", "usage": {"input_tokens": 3, "output_tokens": 4},
                                  "cost_usd": None, "usage_limit": False, "usage_limit_until": None})

    def test_codex_usage_limit(self):
        message = "You've hit your usage limit. Try again in 2 hours."
        parsed = self.codex({"type": "error", "message": message},
                            {"type": "turn.failed", "error": {"message": message}})
        self.assertEqual((parsed["ok"], parsed["usage_limit"], parsed["summary"]), (False, True, message))

    def test_codex_other_failure(self):
        parsed = self.codex({"type": "turn.failed", "error": {"message": "stream disconnected"}})
        self.assertEqual((parsed["ok"], parsed["usage_limit"]), (False, False))

    def test_codex_unreadable_output_raises_agent_error(self):
        for stdout in ("", "not json\n", "[1, 2]\n", json.dumps({"type": "turn.started"}),
                       json.dumps({"type": "item.completed", "item": "odd"})):
            with self.assertRaises(agents.AgentError, msg=stdout):
                agents.parse_codex_json(stdout)

    def test_codex_odd_shapes_do_not_crash(self):
        parsed = self.codex({"type": "item.completed", "item": {"type": "agent_message", "text": 5}},
                            {"type": "turn.failed", "error": "odd"})
        self.assertEqual((parsed["ok"], parsed["usage_limit"]), (False, False))

    def test_claude_usage_limit(self):
        for text in ("Claude AI usage limit reached|1759075200", "5-hour limit reached ∙ resets 3pm",
                     "You've hit your limit · resets 3pm (Europe/London)"):
            parsed = agents.parse_claude_json(json.dumps({"is_error": True, "result": text}))
            self.assertTrue(parsed["usage_limit"], text)
        parsed = agents.parse_claude_json(json.dumps({"is_error": True, "result": "API Error: 500"}))
        self.assertFalse(parsed["usage_limit"])
        parsed = agents.parse_claude_json(json.dumps({"is_error": False, "result": "fixed the limit reached bug"}))
        self.assertFalse(parsed["usage_limit"])

    def test_claude_non_object_raises_agent_error(self):
        with self.assertRaises(agents.AgentError):
            agents.parse_claude_json("[1]")



class PlainText(unittest.TestCase):
    # As Plane 1.4.2 sends it, IDs shortened.
    PLANE = (
        '<p class="editor-paragraph-block" data-id="39d0f208">User story<br>As the person running '
        'Dynamic Dispatch, I want...</p><p class="editor-paragraph-block" data-id="e11f8acc">'
        'Acceptance criteria</p><ul class="not-prose pl-2 space-y-2" data-id="eda2392b" '
        'data-type="taskList"><li class="relative" data-id="b8792f1f" data-checked="false" '
        'data-type="taskItem"><label><input type="checkbox"><span></span></label><div>'
        '<p class="editor-paragraph-block" data-id="ce63d0a8">It explains that "max_concurrent" '
        'must stay 1.</p></div></li></ul><ul class="list-disc pl-7 space-y-(--list-spacing-y)" '
        'data-id="ab062e8c"><li class="not-prose space-y-2" data-id="7d71b58c">'
        '<p class="editor-paragraph-block" data-id="52de2aed"><a target="_blank" '
        'class="text-accent-secondary underline underline-offset-[3px] hover:text-accent-primary '
        'transition-colors cursor-pointer" href="http://main.py" rel="noopener noreferrer">main.py</a>'
        '</p></li></ul><p class="editor-paragraph-block" data-id="ddca6e50">Checklist items carry '
        'data-checked="true" or "false". </p>'
    )

    def test_plane_description(self):
        self.assertEqual(text.html_to_text(self.PLANE), (
            "User story\n"
            "As the person running Dynamic Dispatch, I want...\n"
            "\n"
            "Acceptance criteria\n"
            "\n"
            '- [ ] It explains that "max_concurrent" must stay 1.\n'
            "\n"
            "- main.py\n"
            "\n"
            'Checklist items carry data-checked="true" or "false".'
        ))

    def test_no_markup_left(self):
        out = text.html_to_text(self.PLANE)
        for leftover in ("<", ">", "data-id", "class=", "href", "editor-paragraph-block"):
            self.assertNotIn(leftover, out)

    def test_empty(self):
        for html in (None, "", "<p></p>", '<p class="editor-paragraph-block" data-id="x"></p>'):
            self.assertEqual(text.html_to_text(html), "", html)

    def test_entities(self):
        self.assertEqual(text.html_to_text("<p>a &amp; b &lt;c&gt; &quot;d&quot; &#39;e&#39; caf&eacute;</p>"),
                         "a & b <c> \"d\" 'e' café")

    def test_nested_lists(self):
        html = ("<ol><li><p>one</p><ul><li><p>a</p><ul><li><p>deep</p></li></ul></li>"
                "<li><p>b</p></li></ul></li><li><p>two</p></li></ol><p>after</p>")
        self.assertEqual(text.html_to_text(html),
                         "1. one\n   - a\n     - deep\n   - b\n2. two\n\nafter")

    def test_ticked_checklist_item(self):
        html = ('<ul data-type="taskList"><li data-checked="true" data-type="taskItem"><label>'
                '<input type="checkbox" checked><span></span></label><div><p>Done</p></div></li>'
                '<li data-checked="false" data-type="taskItem"><label><input type="checkbox">'
                '<span></span></label><div><p>Not yet</p></div></li></ul>')
        self.assertEqual(text.html_to_text(html), "- [x] Done\n- [ ] Not yet")

    def test_code_block_kept_exactly(self):
        html = "<p>Run:</p><pre><code>def f():\n    return 1 &lt; 2\n\n\nx  = 3</code></pre><p>Then</p>"
        self.assertEqual(text.html_to_text(html), "Run:\n\ndef f():\n    return 1 < 2\n\n\nx  = 3\n\nThen")

    def test_whitespace_between_tags_ignored(self):
        html = "\n<p>\n  Line   one\n</p>\n<ul>\n  <li>\n    <p>item</p>\n  </li>\n</ul>\n"
        self.assertEqual(text.html_to_text(html), "Line one\n\n- item")

    def test_prompt_uses_plain_text(self):
        issue = {"name": "S", "description_html": self.PLANE, "description_stripped": "flattened"}
        prompt = prompts.build_prompt({}, issue)
        self.assertIn("- [ ] It explains", prompt)
        self.assertNotIn("data-id", prompt)
        self.assertNotIn("flattened", prompt)

    def test_prompt_requires_finished_foreground_work_and_final_summary(self):
        prompt = " ".join(prompts.build_prompt({}, {"name": "S"}).split())
        for instruction in (
            "Run every command in the foreground and wait for it to finish.",
            "Do not start background jobs, monitors or waiters",
            "DD stops every process you started when your run ends",
            "you are never woken again",
            "Your final message becomes the pull request description.",
            "Write it only after all work and checks have finished",
            "it must be the complete summary, not a status update.",
        ):
            with self.subTest(instruction=instruction):
                self.assertIn(instruction, prompt)

    def test_prompt_falls_back_without_html(self):
        for html in (None, "", "<p></p>"):
            issue = {"name": "S", "description_html": html, "description_stripped": "Add VALUE"}
            self.assertIn("Add VALUE", prompts.build_prompt({}, issue))
        self.assertIn("(no description)", prompts.build_prompt({}, {"name": "S"}))



class SecondReview(Base):
    def test_usage_keeps_only_numbers_and_cost_is_a_float(self):
        os.environ["LEAK"] = GH
        self.set_mode("usage"); self.add_issue(); self.cycle()
        [row] = self.run_rows()
        self.assertEqual(row["status"], runs.DONE, row)
        self.assertEqual(json.loads(row["usage"]), {"output_tokens": 5, "input_tokens": 7.5})
        self.assertEqual(row["cost_usd"], 0.5)


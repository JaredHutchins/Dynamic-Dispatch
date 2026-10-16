"""Agent instructions describe their runner without assuming a project name."""

import ast
from pathlib import Path
import re
import unittest

from dispatch import plans, rework


ROOT = Path(__file__).resolve().parents[1]


class AgentInstructions(unittest.TestCase):
    def test_instruction_text_does_not_name_the_system(self):
        # Inspect authored text, not interpolated stories, diffs, test output or
        # project definitions of done, which may legitimately name the project.
        functions = {
            'prompts.py': {'build_prompt', 'assessment_prompt'},
            'rework.py': {'prompt'},
            'docs_line.py': {'instruction', 'send_back_note'},
            'worker.py': {'unfinished_note'},
            'mainhealth.py': {'_failure_notes'},
        }
        assignments = {
            'worker.py': {'FINISHED_WORK_NOTE'},
            'mainhealth.py': {'description'},
        }
        checked = set()
        for path in sorted((ROOT / 'dispatch').glob('*.py')):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                selected = (isinstance(node, ast.FunctionDef)
                            and node.name in functions.get(path.name, set()))
                if isinstance(node, (ast.Assign, ast.AugAssign)):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    names = {target.id for target in targets if isinstance(target, ast.Name)}
                    selected = bool(names & ({'prompt'} | assignments.get(path.name, set())))
                if not selected:
                    continue
                for literal in ast.walk(node):
                    if not isinstance(literal, ast.Constant) or not isinstance(literal.value, str):
                        continue
                    key = (path.name, literal.lineno)
                    if key in checked:
                        continue
                    checked.add(key)
                    text = literal.value.replace('DD-DECISION-NEEDED', '')
                    text = re.sub(r'\[dd:[^\]]*\]', '', text)
                    with self.subTest(file=path.name, line=literal.lineno):
                        self.assertNotRegex(text, r'\bDD\b|\bDynamic\s+Dispatch\b')
        self.assertTrue(checked)

    def test_protocol_markers_are_unchanged(self):
        self.assertEqual(rework.MARKER, 'DD-DECISION-NEEDED')
        self.assertIn('DD-DECISION-NEEDED', rework.prompt('https://example.test/pull/1'))
        self.assertEqual(rework.explanation('DD-DECISION-NEEDED\nEvidence'), 'Evidence')
        self.assertEqual(plans.question_marker('cycle'), '[dd:plan cycle]')

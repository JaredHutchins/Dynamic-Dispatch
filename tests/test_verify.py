"""Project verification limits and failure output formats."""

import unittest
from pathlib import Path
from unittest import mock

from dispatch import combine, mainhealth, proc, verify


class Verify(unittest.TestCase):
    def test_framework_samples(self):
        expected = {
            'unittest': ['test_total (test_cart.CartTests.test_total)'],
            'pytest': ['tests/test_cart.py::test_total'],
            'jest': ['cart › totals items'],
            'vitest': ['cart > totals items'],
            'mocha': ['cart totals items'],
            'go': ['TestCart', 'TestCart/total'],
            'rust': ['tests::total'],
        }
        for framework, names in expected.items():
            with self.subTest(framework=framework):
                output = (Path(__file__).parent / 'samples' / 'test_failures' / (framework + '.txt')).read_text()
                self.assertEqual(verify.failing_tests(output), names)
                self.assertEqual(verify.failing_tests('\x1b[31m' + output.replace('\n', '\x1b[0m\n\x1b[31m')), names)

    def test_extra_pattern_order_duplicates_and_unknown_output(self):
        project = {'test_failure_patterns': [r'^BROKEN: (.+)$']}
        output = 'BROKEN: custom case\nFAIL: builtin\nBROKEN: custom case\n'
        self.assertEqual(verify.failing_tests(output, project), ['custom case', 'builtin'])
        self.assertEqual(combine._failure_names(output, project), 'custom case; builtin')
        self.assertEqual(verify.failing_tests('unknown failure'), [])

    def test_mocha_multiple_failures_and_progress_without_details(self):
        output = ("  cart\n    1) totals items\n    2) empties cart\n\n"
                  "  2 failing\n\n  1) cart\n       totals items:\n\n"
                  "      AssertionError: wrong total\n\n"
                  "  2) cart\n       empties cart:\n\n"
                  "      AssertionError: not empty\n")
        self.assertEqual(verify.failing_tests(output), ['cart totals items', 'cart empties cart'])
        self.assertEqual(verify.failing_tests('  cart\n    1) totals items\n\n  1 failing\n'), [])

    def test_story_and_main_timeouts_and_output(self):
        for runner in (verify.run_verify, mainhealth._verify):
            for setting, expected in (({}, 900), ({'verify_timeout_seconds': 1800}, 1800)):
                project = dict(verify_command=['test'], **setting)
                for timed_out in (False, True):
                    with self.subTest(runner=runner, setting=setting, timed_out=timed_out):
                        result = proc.Result(1, 'unknown failure output', '', timed_out, False)
                        with mock.patch.object(proc, 'run_sandboxed', return_value=result) as run:
                            ok, output = runner({}, project, {}, '/checkout')
                        self.assertFalse(ok)
                        if not timed_out:
                            self.assertIn('unknown failure output', output)
                        self.assertEqual(run.call_args.kwargs['timeout'], expected)
                        if timed_out:
                            self.assertIn(f'{expected}s', output)

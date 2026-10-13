"""Exercise the suite guard in child interpreters so deliberate leaks stay isolated."""
import subprocess
import sys
from pathlib import Path
import unittest

from unraisable import install

install()


class UnraisableGuard(unittest.TestCase):
    def run_example(self, body, phase="runTest", following=False):
        code = f'''
import sys, unittest
sys.path.insert(0, {str(Path(__file__).resolve().parent)!r})
from unraisable import install
from dispatch import runs
install()
class Example(unittest.TestCase):
    def {phase}(self):
{body}
    {"def runTest(self): pass" if phase != "runTest" else ""}
{"class Following(unittest.TestCase):" if following else ""}
{"    def runTest(self): pass" if following else ""}
unittest.main()
'''
        return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)

    @unittest.skipIf(sys.version_info < (3, 13), "SQLite ResourceWarning needs Python 3.13")
    def test_sqlite_leaks_fail_in_every_test_phase(self):
        for phase in ("setUp", "runTest", "tearDown"):
            with self.subTest(phase=phase):
                result = self.run_example('        self.conn = runs.connect(":memory:")', phase)
                self.assertNotEqual(result.returncode, 0, result.stderr)
                self.assertIn("Unraisable exception in __main__.Example.runTest", result.stderr)
                self.assertIn("unclosed database", result.stderr)
                self.assertNotIn("Exception ignored", result.stderr)

    @unittest.skipIf(sys.version_info < (3, 13), "SQLite ResourceWarning needs Python 3.13")
    def test_leak_is_attributed_to_previous_test(self):
        result = self.run_example('        self.conn = runs.connect(":memory:")', following=True)
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("Unraisable exception in __main__.Example.runTest", result.stderr)
        self.assertNotIn("Unraisable exception in __main__.Following", result.stderr)
        self.assertNotIn("Exception ignored", result.stderr)

    def test_other_destructor_errors_fail(self):
        result = self.run_example('''        class Broken:
            def __del__(self):
                raise RuntimeError("destructor broke")
        self.addCleanup(lambda: Broken())''')
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("Unraisable exception in __main__.Example.runTest", result.stderr)
        self.assertIn("destructor broke", result.stderr)
        self.assertNotIn("Exception ignored", result.stderr)

    def test_closed_connection_passes_even_after_setup_error(self):
        result = self.run_example('''        conn = runs.connect(":memory:")
        self.addCleanup(conn.close)
        raise RuntimeError("setup broke")''', "setUp")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("setup broke", result.stderr)
        self.assertNotIn("Unraisable exception", result.stderr)
        self.assertNotIn("Exception ignored", result.stderr)

    def test_closed_connection_passes(self):
        result = self.run_example('''        conn = runs.connect(":memory:")
        self.addCleanup(conn.close)''')
        self.assertEqual(result.returncode, 0, result.stderr)

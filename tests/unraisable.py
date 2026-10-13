"""Make destructor errors fail unittest, including cyclic SQLite connections."""
import gc
import sys
import traceback
import unittest
import warnings


_installed = False
_current = None


class TestIdentity(unittest.TestCase):
    # Retaining the real test here would keep its unclosed connections alive.
    def __init__(self, test):
        super().__init__()
        self.name = test.id()
        self.description = str(test)

    def id(self):
        return self.name

    def __str__(self):
        return self.description

    def runTest(self):
        pass


def install():
    global _installed
    if _installed:
        return
    _installed = True
    original_hook = sys.unraisablehook
    original_start = unittest.TestResult.startTest
    original_run = unittest.TestSuite.run

    def unraisable(args):
        if _current is None:
            original_hook(args)
            return
        result, test = _current
        detail = "".join(traceback.format_exception(
            args.exc_type, args.exc_value, args.exc_traceback))
        error = AssertionError(f"Unraisable exception in {test.id()}:\n{detail}")
        result.addFailure(test, (AssertionError, error, None))

    def start_test(result, test):
        global _current
        # Collect the previous test before assigning blame to the next one.
        gc.collect()
        _current = (result, TestIdentity(test))
        original_start(result, test)

    def run_suite(suite, result, debug=False):
        # TextTestRunner installs its warning filters after test discovery.
        warnings.filterwarnings("error", category=ResourceWarning)
        try:
            return original_run(suite, result, debug)
        finally:
            # The last test has now been released by unittest's suite cleanup.
            gc.collect()

    sys.unraisablehook = unraisable
    unittest.TestResult.startTest = start_test
    unittest.TestSuite.run = run_suite

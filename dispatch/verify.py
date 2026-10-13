"""Running a project's verify command and reading which tests failed."""

import re

from . import proc

VERIFY_TIMEOUT = 900


def run_verify(cfg, project_cfg, agent_cfg, worktree):
    """Runs sandboxed, because it executes code the agent may have written.

    Returns (ok, output). No verify command configured means nothing to check.
    """
    command = project_cfg.get("verify_command")
    if not command:
        return True, "(no verify command configured)"
    result = proc.run_sandboxed(
        cfg, command, cwd=worktree, timeout=VERIFY_TIMEOUT, run_as=agent_cfg.get("run_as")
    )
    if result.timed_out or result.returncode != 0:
        return False, proc.describe_failure(result, VERIFY_TIMEOUT)
    return True, (result.stdout + result.stderr).strip() or "(no output)"


def failing_tests(output):
    """Names of the failing tests in verification output, in order, each once; empty if none are named."""
    names = re.findall(r"^(?:FAIL|ERROR): (.+)$|^FAILED (?!\()(\S+)|\bfailed: (test[\w.:/\[\]-]+)",
                       output, re.MULTILINE)
    return list(dict.fromkeys(next(part for part in match if part).rstrip(":") for match in names))

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
    timeout = project_cfg.get("verify_timeout_seconds", VERIFY_TIMEOUT)
    result = proc.run_sandboxed(
        cfg, command, cwd=worktree, timeout=timeout, project_cfg=project_cfg, run_as=agent_cfg.get("run_as")
    )
    if result.timed_out or result.returncode != 0:
        return False, proc.describe_failure(result, timeout)
    return True, (result.stdout + result.stderr).strip() or "(no output)"


# Match failure detail headings, not progress counters or failed suite filenames.
FAILURE_PATTERNS = (
    r"^(?:FAIL|ERROR): (.+)$",
    r"^FAILED (?!\()(\S+)",
    r"\bfailed: (test[\w.:/\[\]-]+)",
    r"^ +● (.+)$",  # Jest
    r"^ FAIL +\S+ > (.+)$",  # Vitest
    r"^ +\d+\) (.+(?:\n +[^\n]+)*?):$",  # Mocha failure details
    r"^\s*--- FAIL: (\S+) \(.*\)$",  # go test
    r"^test (.+) \.\.\. FAILED$",  # cargo test
)


def failing_tests(output, project_cfg=None):
    """Failing names in output order, once each; custom patterns capture one name."""
    output = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output)
    patterns = (*FAILURE_PATTERNS, *(project_cfg or {}).get("test_failure_patterns", []))
    matches = []
    for pattern in patterns:
        for match in re.finditer(pattern, output, re.MULTILINE):
            name = " ".join((match.group(1) or "").split()).rstrip(":")
            if name:
                matches.append((match.start(), name))
    return list(dict.fromkeys(name for _, name in sorted(matches, key=lambda item: item[0])))

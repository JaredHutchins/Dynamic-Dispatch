"""Project checkout preparation, using the same sandbox as verification."""

import shlex

from . import proc

DEFAULT_TIMEOUT = 900


def run(cfg, project_cfg, agent_cfg, folder):
    """Run argument lists in order, stopping at the first failure."""
    timeout = project_cfg.get("prepare_timeout_seconds", DEFAULT_TIMEOUT)
    for command in project_cfg.get("prepare_commands", []):
        result = proc.run_sandboxed(cfg, command, cwd=folder, timeout=timeout,
                                    project_cfg=project_cfg, run_as=agent_cfg.get("run_as"))
        if result.timed_out or result.returncode != 0:
            output = (result.stdout + result.stderr).strip()
            return False, (f"Prepare command {shlex.join(command)} failed:\n"
                           f"{proc.describe_failure(result, timeout)}\n\n{output}")
    return True, ""

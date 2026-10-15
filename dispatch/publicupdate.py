"""Anonymous GitHub test checks for DD installs without a project for DD itself."""

import json
import re
import time
import urllib.request

from . import runs
from .redact import warn

POLL_SECONDS = 300
# These are the jobs in .github/workflows/tests.yml. Missing jobs fail closed.
TEST_CHECKS = {f"Python {version} / {mode}" for version in ("3.11", "3.13")
               for mode in ("normal", "ResourceWarning")}
_cached = {"repo": None, "after": 0, "target": None}


def _read(repo, path):
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/{path}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "DD-self-update",
                 "X-GitHub-Api-Version": "2022-11-28"})
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


def target(repo, conn):
    """Latest main if every test passed; cache the decision between bounded polls."""
    now = time.monotonic()
    if _cached["repo"] == repo and now < _cached["after"]:
        return _cached["target"]
    # Failed requests consume the interval too: an outage must not cause a tight retry loop.
    _cached.update(repo=repo, after=now + POLL_SECONDS, target=None)
    revision = _read(repo, "commits/main")["sha"]
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("GitHub returned an invalid main commit")
    result = _read(repo, f"commits/{revision}/check-runs?filter=latest&per_page=100")
    checks = result["check_runs"]
    if result["total_count"] > len(checks):
        return None  # Bound API use rather than approving an incomplete page of checks.
    selected = [check for check in checks if check["name"] in TEST_CHECKS
                and check.get("app", {}).get("slug") == "github-actions"
                and check.get("head_sha") == revision]
    failed = any(check["status"] == "completed" and check["conclusion"] != "success"
                 for check in selected)
    if failed and runs.claim_self_update_notice(conn, revision, "github-tests"):
        message = f"DD waits to update: GitHub tests did not pass for {revision[:12]}"
        runs.record_event(conn, None, "warning", message, project="", issue_id="self-update")
        warn(message)
    if (not failed and {check["name"] for check in selected} == TEST_CHECKS
            and all(check["status"] == "completed" and check["conclusion"] == "success"
                    for check in selected)):
        _cached["target"] = revision
    return _cached["target"]

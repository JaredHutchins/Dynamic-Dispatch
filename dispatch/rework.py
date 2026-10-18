"""Rework eligibility and the agent's mechanical decision-request protocol."""

import json

from . import github, gitops, runs

MARKER = "DD-DECISION-NEEDED"
NEEDS_DECISION = "needs decision"


def pull_request(cfg, conn, run, *, continuing=False):
    """The earlier DD pull request eligible for rework, or None.

    Clean saved or held work resumes its existing outcome rather than doing rework.
    The worker marks unfinished hand-offs and DD-only combine commits as continuing.
    GitHub discovery alone never makes a first build count as rework.
    """
    if run["kind"] != "story" or run["hold_data"]:
        return None
    pushed = runs.last_pushed(conn, run["issue_id"], run["id"])
    if pushed is not None and not pushed["pr_url"]:
        return None
    previous = runs.earlier_pull_request(conn, run)
    if previous is None or previous["branch"] != run["branch"]:
        return None
    project = cfg["projects"][run["project"]]
    repo = project["repo_path"]
    if not gitops.branch_exists(repo, run["branch"]) or (not continuing and gitops.has_unpushed_commits(repo, run["branch"])):
        return None
    if github.pr_is_open(project["github_repo"], previous["pr_url"], github.token(cfg["github"])):
        return previous["pr_url"]
    return None


def prompt(pr_url):
    if not pr_url:
        return ""
    return (
        f"\n\nThis story already has an open pull request: {pr_url}\n"
        "The reviewer's latest instructions are in the story. Carry out every instruction you can. "
        "If, after investigating, a requested change should not be made (already done, obsolete, "
        "contradicts the current code, or would make it wrong), do not make it. "
        f"Begin your final message with the exact line {MARKER}, then for each instruction not "
        "carried out give the instruction, why, and evidence (file and line, or command output). "
        "After the declined instructions, also give your usual complete summary of what you "
        "changed and why, because it becomes the pull request description when there are changes. "
        "If the story says the reviewer has already seen your objection and still wants the "
        "change, make it."
    )


def explanation(summary):
    lines = (summary or "").splitlines()
    if lines and lines[0] == MARKER:
        return "\n".join(lines[1:]).strip() or None
    return None


def finish(cfg, conn, run, result, changed, pulled_out, pr_url, worktree):
    """Decide what a finished story run's changes lead to, recording a decision request.

    None means the normal path carries on (changed rework is flagged on result),
    NEEDS_DECISION that the unchanged decision request is saved for the board
    update, and any other value is the reason the run is Blocked. No
    interpretation of the explanation decides whether an objection is
    justified: that belongs to the reviewer.
    """
    if pulled_out:
        return None
    detail = explanation(result.get("summary"))
    if pr_url and detail:
        result["needs_decision"] = True
        result["summary"] = detail
        if changed:
            return None
        recorded = runs.get(conn, run["id"])
        runs.await_review(conn, run["id"], summary=detail,
                          usage=json.loads(recorded["usage"] or "null"), cost_usd=recorded["cost_usd"],
                          pr_url=pr_url, worktree=str(worktree), needs_decision=True, no_changes=True)
        return NEEDS_DECISION
    if changed:
        return None
    name = cfg["agents"][run["agent"]]["display_name"]
    reason = f"{name} finished but changed nothing.\n\nIts summary:\n{result.get('summary') or '(none)'}"
    if not pr_url and MARKER in (result.get("summary") or "").splitlines():
        reason += "\n\nA decision request only applies to rework of an open pull request."
    return reason


def comment(cfg, conn, row):
    """Best-effort, idempotent delivery of an unchanged decision to GitHub."""
    if not row["needs_decision"] or not row["no_changes"]:
        return
    project = cfg["projects"][row["project"]]
    marker = f"<!-- Dynamic Dispatch run {row['id']}: decision needed -->"
    try:
        github.comment_once(project["github_repo"], row["pr_url"], row["summary"], marker,
                            github.token(cfg["github"]))
    except Exception as e:
        runs.record_event(conn, row["id"], "warning", f"Could not post the decision request on GitHub: {e}")

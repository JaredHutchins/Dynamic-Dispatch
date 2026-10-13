"""Status lights and approval notices for Dynamic Dispatch pull requests."""

import sqlite3

from . import alerts, github, gitops, runs
from .redact import warn


def outcome(cfg, conn, row, terminal, reached_target):
    # Last, as nothing waits on it. A refresh's new commit, or a rework's, gets its own light.
    # Red is for a story in Blocked, not for a failed run: one moved elsewhere gets none.
    if terminal == runs.DONE:
        light(cfg, conn, row["project"], row["issue_id"], "success")
    elif reached_target:
        light(cfg, conn, row["project"], row["issue_id"], "failure")


def check_story(cfg, conn, project, issue_id, state, check_main):
    """Update a story's light and report whether its branch needs latest main.

    check_main returns whether the branch contains latest main, or None for a
    closed pull request. It shares the refresh check's per-project lookup and
    is called only when the story needs it, preserving the request counts.
    """
    states = cfg["projects"][project]["states"]
    lit = light_state(conn, project, issue_id)
    if state == states["blocked"]:
        if lit != "failure" or light_owed(conn, project, issue_id):
            light(cfg, conn, project, issue_id, "failure")
        return False
    if state == states["in_progress"]:
        if lit != "reworking" or light_owed(conn, project, issue_id):
            light(cfg, conn, project, issue_id, "reworking")
        return False
    in_review = state == states["in_review"]
    # State-dependent lights must be restored even outside In Review.
    if not in_review and lit not in ("failure", "reworking"):
        return False
    if runs.is_active(conn, issue_id) and lit != "reworking":
        return False
    contains_main = check_main()
    if contains_main is None:
        return False
    if contains_main:
        # Pull requests opened before DD set lights, or just out of Blocked, get theirs here.
        if lit != "success":
            light(cfg, conn, project, issue_id, "success")
        return False
    # Yellow as soon as main has moved on, before any refresh starts.
    if lit != "pending":
        light(cfg, conn, project, issue_id, "pending")
    return True


# The Dynamic Dispatch light on each pull request. Each change is recorded as
# owed before it is tried, so one GitHub refused or couldn't take is tried again
# on the next check. A light is never worth holding up, blocking or failing a
# story, so nothing here raises.
#
# With github.approver set, "success" only means tested: the light is green
# once the approver's approval also still counts, and yellow until then. The
# store keeps the requested and displayed lights separately.


def light_state(conn, project, issue_id):
    """The light last owed, set or left for the story's pull request, or None."""
    last = runs.light(conn, project, issue_id)
    return last["state"] if last else None


def light_owed(conn, project, issue_id):
    """Whether the story's last light is still waiting for GitHub to take it."""
    last = runs.light(conn, project, issue_id)
    return last is not None and last["disposition"] == "owed"


def light(cfg, conn, project, issue_id, state):
    """Give the story's pull request this light, if it has one."""
    try:
        if runs.pull_request(conn, project, issue_id) is None:
            return
        runs.save_light(conn, project, issue_id, state, "owed")
    except sqlite3.Error as e:
        return warn(f"Could not look up the pull request to light for {issue_id}: {e}")
    runs.record_event(conn, None, "light_owed", state, project=project, issue_id=issue_id)
    _set_light(cfg, conn, project, issue_id, state)


def _set_light(cfg, conn, project, issue_id, state, previous=None):
    """previous is the light last set, when only the approval may have changed it."""
    def event(kind, detail):
        runs.record_event(conn, None, kind, detail, project=project, issue_id=issue_id)
    try:
        project_cfg = cfg["projects"][project]
        pr = runs.pull_request(conn, project, issue_id)
        token = github.token(cfg["github"])
        head = github.pr_head(project_cfg["github_repo"], pr["pr_url"], token)
        if head is None:
            runs.save_light(conn, project, issue_id, state, "left", head=head)
            return event("light_left", f"{state} closed: merged or closed pull requests are left alone")
        if state == "success" and head != _pushed(project_cfg["repo_path"], pr["branch"]):
            runs.save_light(conn, project, issue_id, state, "left", head=head)
            return event("light_left", f"{state} {head}: DD has not tested this commit")
        shown = state
        if state == "success":
            shown = _approval(cfg, conn, project, issue_id, pr, head, token)
        detail = f"{state} {head}" + (f" {shown}" if shown != state else "")
        if (head, shown) == previous:
            return
        github.set_status(project_cfg["github_repo"], head, shown, token)
        runs.save_light(conn, project, issue_id, state, "set", head=head, shown=shown)
        event("light", detail)
    except Exception as e:
        event("warning", f"Could not set the Dynamic Dispatch light on the pull request; will retry: {e}")


def _approval(cfg, conn, project, issue_id, pr, head, token):
    """Which light a tested head gets: approved, waiting or changed, or plain success with no approver."""
    approver = (cfg["github"].get("approver") or "").lstrip("@")
    if not approver:
        return "success"
    project_cfg = cfg["projects"][project]
    after = runs.decision_after(conn, project, issue_id, pr["pr_url"])
    approved = github.approved_commit(project_cfg["github_repo"], pr["pr_url"], approver, token, after=after)
    if approved is None:
        return "waiting"
    changed = _first_change(conn, project_cfg["repo_path"], approved, head)
    if changed is None:
        return "approved"
    combine = runs.combine_commit(conn, changed)
    # Rework was asked for, and its In Review alert says so already.
    if combine is not None and combine["kind"] == "refresh":
        _warn_changed(cfg, conn, project, issue_id, pr, approved, changed, token)
    return "changed"


def _first_change(conn, repo, approved, head):
    """The first commit after the approved one that isn't a clean combine, or None.

    Only the branch's own line of commits counts: what a combine brings in from
    main is already merged there. An approved commit the head doesn't build on
    counts as changed by the head itself.
    """
    commits = gitops.first_parent_commits_since(repo, approved, head)
    if commits is None:
        return head
    for sha in commits:
        combine = runs.combine_commit(conn, sha)
        if combine is None or not combine["clean"]:
            return sha
    return None


def _warn_changed(cfg, conn, project, issue_id, pr, approved, changed, token):
    """Tell the approver, once per commit, that a refresh repair voided their approval.

    Failing here leaves the light alone; the next check tries again.
    """
    try:
        if runs.approval_noticed(conn, project, issue_id, changed):
            return
        combine = runs.combine_commit(conn, changed)
        row = runs.get(conn, combine["run_id"])
        name = cfg["agents"].get(row["agent"], {}).get("display_name", row["agent"]) if row else "An agent"
        message = (f"Commit {changed[:12]} changed this pull request after it was approved at "
                   f"{approved[:12]}: combining it with the latest main needed {name} to repair a clash "
                   "or failing tests. The approval no longer counts, so please review it again.")
        github.pr_comment(cfg["projects"][project]["github_repo"], pr["pr_url"], message, token)
        runs.mark_approval_noticed(conn, project, issue_id, changed)
        runs.record_event(conn, None, "approval_voided", changed, project=project, issue_id=issue_id)
        number = github.pr_number(pr["pr_url"])
        alerts.send(cfg, project, issue_id, message, row=row,
                    suffix=f": PR #{number} changed since your approval", level="active", url=pr["pr_url"])
    except Exception as e:
        runs.record_event(conn, None, "warning",
                          f"Could not say the pull request changed since its approval; will retry: {e}",
                          project=project, issue_id=issue_id)


def _pushed(repo, branch):
    """DD's last upload of the branch.

    DD pushes only commits its tests passed on, and fetches only main, so this
    is always a commit DD tested, never someone else's push.
    """
    return gitops.pushed_revision(repo, branch)


def lights(cfg, conn):
    """Try again the lights still owed, then look for new approvals.

    State-dependent lights are handled by check_story using the refresh check's
    board read. It also lights pull requests that were open before DD set lights.
    """
    try:
        _lights(cfg, conn)
    except sqlite3.Error as e:  # a store failure must not stop other stories
        warn(f"Could not check pull request lights; will retry: {e}")


def _lights(cfg, conn):
    owed = runs.owed_lights(conn)
    for row in owed:
        if row["project"] in cfg["projects"] and row["state"] not in ("failure", "reworking"):
            _set_light(cfg, conn, row["project"], row["issue_id"], row["state"])
    if not cfg["github"].get("approver"):
        return
    # Only tested lights still short of green: once green, a new commit is
    # the only thing that changes it, and that brings its own light.
    unapproved = runs.unapproved_lights(conn)
    for row in unapproved:
        if row["project"] in cfg["projects"]:
            _set_light(cfg, conn, row["project"], row["issue_id"], "success", previous=(row["head"], row["shown"]))

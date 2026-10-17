"""Bringing the board in line with finished runs.

A run whose work is over waits in awaiting_review or awaiting_blocked until
Plane actually shows the outcome. Only then does it become terminal. If Plane
is unreachable, the run simply stays waiting and is retried every cycle - and
because it is still active, its story cannot be dispatched again meanwhile.

The update is retried, never the agent run.
"""

import math
import threading
from datetime import datetime, timezone

from . import alerts, choose, config, github, gitops, lights, plane, rework, runs
from .redact import log, warn
from .stories import story_ref


def marker(row):
    """Ends every outcome comment, so a retry can tell the comment already went up."""
    return f"[Dynamic Dispatch run {row['id']}]"


_reconcile_lock = threading.RLock()


def reconcile(cfg, conn, row, retry=False):
    with _reconcile_lock:
        row = runs.get(conn, row["id"])
        if row["status"] not in (runs.AWAITING_REVIEW, runs.AWAITING_BLOCKED):
            return
        return _reconcile(cfg, conn, row, retry)


def _reconcile(cfg, conn, row, retry=False):
    # Failed retry attempts extend the warning episode instead of inserting
    # a stage event between identical failures.
    if not retry:
        runs.record_event(conn, row["id"], "updating_plane", "Updating Plane")
    project_cfg = cfg["projects"].get(row["project"])
    if project_cfg is None:
        runs.record_event(conn, row["id"], "warning", f"run {row['id']}: project '{row['project']}' is no longer in config; leaving it for you")
        warn(f"run {row['id']}: project '{row['project']}' is no longer in config; leaving it for you")
        return

    project_id = project_cfg["plane_project_id"]
    states = project_cfg["states"]
    orch = plane.orchestrator(cfg)

    if row["status"] == runs.AWAITING_REVIEW:
        agent_cfg = cfg["agents"].get(row["agent"])
        speaker = plane.as_agent(cfg, agent_cfg) if agent_cfg and row["kind"] != "refresh" else orch
        # The comment puts the pull request first, apart from the summary, so
        # truncating a long summary can never cut it off.
        pr_url, body = row["pr_url"], row["summary"] or '(no summary)'
        if row["needs_decision"]:
            name = agent_cfg["display_name"] if agent_cfg else row["agent"]
            prefix = "No changes made. " if row["no_changes"] else ""
            body = f"{prefix}{name} didn't carry out part of the rework and needs your decision:\n\n{body}"
            rework.comment(cfg, conn, row)
        message = f"Pull request: {pr_url}\n\n{body}"
        if row["kind"] == "refresh":
            pr_url = None
            message = body = row["summary"]
        target_state, terminal = states["in_review"], runs.DONE
    else:
        speaker, pr_url = orch, None
        message = body = f"Dynamic Dispatch could not complete this story.\n\n{row['error'] or '(no detail recorded)'}"
        target_state, terminal = states["blocked"], runs.FAILED

    note = None
    try:
        if not row["comment_posted"]:
            # A post that reached Plane but whose reply was lost leaves
            # comment_posted unset, so look for our marker before posting again.
            if not orch.has_comment_with(project_id, row["issue_id"], marker(row)):
                speaker.comment(project_id, row["issue_id"], body, marker=marker(row), pr_url=pr_url)
            runs.mark_comment_posted(conn, row["id"])
        # Read the state as late as possible, right before writing it, to keep
        # the window for overwriting a human's move as small as we can.
        issue = orch.get_issue(project_id, row["issue_id"])
        expected = states["in_review"] if row["kind"] == "refresh" else states["in_progress"]
        still_expected = plane.state_id(issue) == expected
        if still_expected and target_state != expected:
            speaker.set_state(project_id, row["issue_id"], target_state)
        elif not still_expected:
            expected_name = "In Review" if row["kind"] == "refresh" else "In Progress"
            note = f"The story had already been moved out of {expected_name}, so its state was left alone."
    except plane.PlaneError as e:
        if e.status == 404 and _deleted(orch, project_id, row["issue_id"]):
            runs.close(conn, row["id"], terminal, note="The story no longer exists in Plane.")
            log(f"{story_ref(project_cfg, row)}: story deleted in Plane; run closed")
        else:
            runs.record_event(conn, row["id"], "warning", f"#{row['issue_key']}: board not updated yet, will retry: {e}")
            warn(f"{story_ref(project_cfg, row)}: board not updated yet, will retry: {e}")
        return

    if retry:
        runs.record_event(conn, row["id"], "retry", "Retrying Plane update succeeded")
    runs.close(conn, row["id"], terminal, note=note)
    # A successful state write can lose its reply; the retry still alerts once
    # when Plane confirms the intended state.
    reached_target = still_expected or plane.state_id(issue) == target_state
    if reached_target and terminal == runs.DONE and row["kind"] != "refresh":
        review = plane.review_link(row["pr_url"])
        alerts.send(cfg, row["project"], row["issue_id"], row["summary"] or '(no summary)',
                    row=row, suffix=' needs your decision' if row['needs_decision'] else ' ready for review', level='active',
                    url=review[0] if review else row["pr_url"])
    elif reached_target and terminal == runs.FAILED:
        detail = (row["error"] or '(no detail recorded)').splitlines()[0]
        # A Cycle story's Blocked reminders repeat this alert, one interval from now.
        runs.mark_blocked_alert(conn, row["project"], row["issue_id"], detail)
        alerts.blocked(cfg, row["project"], row["issue_id"], detail, row=row)
    else:
        alerts.send(cfg, row["project"], row["issue_id"], message, row=row)
    outcome = "In Review" if terminal == runs.DONE else "Blocked"
    log(f"{story_ref(project_cfg, row)}: {outcome if still_expected else 'outcome posted; state left as you set it'}")

    # Successful worktrees are removed; failed ones are kept for you to inspect.
    if terminal == runs.DONE and row["worktree"]:
        problem = gitops.remove_worktree(project_cfg["repo_path"], row["worktree"])
        if problem:
            runs.record_event(conn, row["id"], "warning", f"#{row['issue_key']}: could not remove worktree {row['worktree']}: {problem}")
            warn(f"{story_ref(project_cfg, row)}: could not remove worktree {row['worktree']}: {problem}")

    lights.outcome(cfg, conn, row, terminal, reached_target)


def _deleted(orch, project_id, issue_id):
    """Whether the Orchestrator account itself gets 404 for the story.

    A 404 from another request (the agent's account, say) doesn't prove the
    story is gone, and closing the run would free the story to be dispatched
    again.
    """
    try:
        orch.get_issue(project_id, issue_id)
    except plane.PlaneError as e:
        return e.status == 404
    return False


def pending(cfg, conn):
    """Retry every run still waiting on a board update."""
    for row in runs.awaiting(conn):
        try:
            reconcile(cfg, conn, row, retry=True)
        except Exception as e:  # one bad row must not stop the others
            runs.record_event(conn, row["id"], "warning", f"run {row['id']}: unexpected error while updating the board: {e!r}")
            warn(f"run {row['id']}: unexpected error while updating the board: {e!r}")


def notice(cfg, project, issue_id, tag, message):
    """Post a notice as the Orchestrator. The tag marks it, so a retry never posts it twice."""
    orch = plane.orchestrator(cfg)
    project_id = cfg["projects"][project]["plane_project_id"]
    tag = f"[Dynamic Dispatch {tag}]"
    if not orch.has_comment_with(project_id, issue_id, tag):
        orch.comment(project_id, issue_id, message, marker=tag)
        alerts.send(cfg, project, issue_id, message)


# Cancelled has no configured ID, so a merge moves a story only out of DD's own
# columns. Anywhere else (Done, Cancelled, a backlog) was a human's choice.
MOVED_TO_DONE_FROM = ("in_progress", "in_review", "blocked")


def merged_message(number):
    return f"Pull request #{number} was merged, so this story is now Done."


def finish_merged(cfg, conn, project, issue, previous):
    """Move a story to Done once the approver has merged its pull request.

    issue comes from the board read the refresh check already made, and
    previous is the story's latest run with a pull request. Returns "open",
    "merged" or "closed" as GitHub reported it, or None if GitHub wasn't asked.
    Nothing here raises for Plane or GitHub: a failure is a warning, retried on
    the next check.
    """
    project_cfg = cfg["projects"][project]
    states, pr_url = project_cfg["states"], previous["pr_url"]
    if not states.get("done") or runs.pr_settled(conn, pr_url):
        return None
    # An active run owns the story's state until it finishes; the next check after it does this.
    if (plane.state_id(issue) not in [states[name] for name in MOVED_TO_DONE_FROM]
            or runs.is_active(conn, issue["id"])):
        return None

    def event(kind, detail):
        runs.record_event(conn, None, kind, detail, project=project, issue_id=issue["id"])
    ref = story_ref(project_cfg, previous)
    try:
        pr_state = github.pr_state(project_cfg["github_repo"], pr_url, github.token(cfg["github"]))
    except github.GitHubError as e:
        event("warning", f"Could not check whether the pull request was merged; will retry: {e}")
        return None
    if pr_state == "closed":
        # Closed without merging: left for the approver, and GitHub isn't asked about it again.
        runs.settle_pr(conn, pr_url, "closed")
        event("pr_closed", "Pull request closed without merging; story left alone")
        return pr_state
    if pr_state != "merged":
        return pr_state
    try:
        number = github.pr_number(pr_url)
        # The comment goes first: its marker is what stops a retry posting it again.
        notice(cfg, project, issue["id"], f"pull request {number} merged", merged_message(number))
        plane.orchestrator(cfg).set_state(project_cfg["plane_project_id"], issue["id"], states["done"])
    except (plane.PlaneError, github.GitHubError) as e:
        event("warning", f"Pull request merged but the story is not Done yet; will retry: {e}")
        warn(f"{ref}: pull request merged but the story is not Done yet; will retry: {e}")
        return pr_state
    # Remembered before settling, so a crash in between can't lose the plan estimate's history.
    runs.record_done_story(conn, project, issue["id"])
    runs.settle_pr(conn, pr_url, "merged")
    event("moved_to_done", f"Pull request #{number} merged; moved the story to Done")
    log(f"{ref}: pull request merged; moved to Done")
    return pr_state


def send_back(cfg, row, attempt, message):
    """Post repair notices, with a marker unique to the try."""
    notice(cfg, row["project"], row["issue_id"], f"run {row['id']} repair {attempt}", message)


REFRESH_UNCHANGED = (
    "The pull request itself was left unchanged, so it still shows the version from before main moved."
)


def refresh_summary(meanwhile):
    return (f"Main moved on ({meanwhile}). Combined this pull request with the latest main and "
            "re-tested: all tests pass. The pull request has been updated.")


GIVE_UP_AFTER = 3  # usage-limit stops in a row before a story goes to Blocked

GAVE_UP = (
    f"This story has been stopped by usage limits {GIVE_UP_AFTER} times in a row, so it has been "
    "moved to Blocked. Move it back to In Progress when agents have usage again."
)


def clock(until):
    """When, in DD's local time, and roughly how long until then."""
    minutes = max(1, math.ceil((until - datetime.now(timezone.utc)).total_seconds() / 60))
    return until.astimezone().strftime("%H:%M"), minutes


def switched_message(stopped, taking_over, leftover=None):
    message = (f"{stopped} reached its usage limit partway through this story. "
               f"{taking_over} is picking it up now.")
    if leftover:
        message += f" {stopped}'s unfinished changes are kept on local branch {leftover}."
    return message


def restarted_message(restart):
    return (f"DD restarted while this story was being worked on. Picking it back up "
            f"(restart {restart} of {runs.RESTART_LIMIT}).")


def waiting_message(why, name, until):
    """Why the story waits, then when it continues by itself. Nothing here is specific to usage limits."""
    at, minutes = clock(until)
    return (f"{why}, so this story is waiting in In Progress. Nothing needs doing. It will continue "
            f"automatically at {at} (in about {minutes} minutes), when {name} is available again.")


def self_update_message(title, running):
    if not running:
        return (f"DD is updating itself after {title} was merged, and is restarting onto the new "
                "code in a moment. This story will start automatically after that. Nothing needs doing.")
    work = ", ".join(running)
    return (f"DD is updating itself after {title} was merged, and starts no new work until "
            f"the running work finishes ({work}). This story will start automatically after "
            "DD restarts. Nothing needs doing.")


def busy_message(some_cooling=False):
    """Why a story waits for an occupied or reserved account."""
    why = ("Agents are occupied or have reached their usage limits"
           if some_cooling else "No agent is available")
    return (f"{why}, so this story is waiting in In Progress. "
            "Nothing needs doing. It will start automatically as soon as an agent is free.")


def waiting_agents_message(cfg, conn, project):
    """One current account status per agent allowed on this project."""
    project_cfg = cfg["projects"].get(project)
    if project_cfg is None:
        return ""
    lines = []
    for status in choose.agent_statuses(cfg, conn, config.project_agents(project_cfg)):
        agent = status["agent"]
        name = cfg["agents"].get(agent, {}).get("display_name", agent)
        work = status["work"]
        if work is not None:
            if work["kind"] == runs.MAIN_CHECK:
                doing = "is testing main"
            elif work["kind"] == "refresh":
                doing = f"is refreshing pull request #{github.pr_number(work['pr_url'])}"
            else:
                ref = story_ref(cfg["projects"].get(work["project"], {}), work)
                doing = f"is working on {ref}: {work['issue_name']}"
        elif status["until"]:
            at = local_resume_time(status["until"])
            doing = f"is resting after its usage limit until {at}"
        else:
            doing = "is free"
        if status["held"]:
            doing += "; held for a main check waiting for an account"
        if not status["configured"]:
            doing += "; cannot take this story because it is not configured"
        lines.append(f"{name} {doing}.")
    return "\n".join(lines)


# Replies on a Cycle's plan item. The approver answers by mentioning the Orchestrator account.
PLAN_ASK = "Add the stories for this Cycle. When it's ready, reply '@Orchestrator assess'."


def plan_ask(carried):
    """The plan item's first comment; it lists stories carried over from earlier Cycles so they go in first."""
    if not carried:
        return PLAN_ASK
    stories = "\n".join(f"- {row['issue_name']} (from {row['cycle_name']})" for row in carried)
    return f"{PLAN_ASK}\n\nThese stories were carried over without being started. Add them first:\n{stories}"


def carried_over_message(cycle_name):
    return f"Carried over from {cycle_name}; not started."


PLAN_NOT_UNDERSTOOD = "I didn't understand. Start with assess, approve or reject."

PLAN_NOT_POSTED = ("There is no plan yet, so there is nothing to approve or reject. "
                   "When the Cycle is ready, reply '@Orchestrator assess'.")


def plan_busy_message(busy):
    """Why assess is refused while other Cycles are being assessed, planned or built.

    busy holds each blocking Cycle's name, plan status and, for an approved Cycle
    that isn't finished, the names of its open stories.
    """
    why = {runs.PLAN_ASSESSING: "is being assessed", runs.PLAN_PLANNED: "has a plan waiting for approval",
           runs.PLAN_APPROVED: "still has stories that aren't Done"}
    reasons = "; ".join(f"Cycle '{name}' {why[status]}" + (f" ({', '.join(open_stories)})" if open_stories else "")
                        for name, status, open_stories in busy)
    return (f"I can't assess this Cycle yet: {reasons}. "
            "When that's settled, reply '@Orchestrator assess' again.")


def plan_too_many_message(count, most):
    """Why assess is refused for a Cycle over the story limit, and how many to remove."""
    extra = count - most
    return (f"I can't assess this Cycle yet: it has {count} stories and the most is {most}. "
            f"Remove {extra} {'story' if extra == 1 else 'stories'}, then reply '@Orchestrator assess' again.")


def assessment_started(names):
    return f"Assessing this Cycle with {', '.join(names)}. I'll post the plan here when every assessment is in."


def assessment_failed(name):
    return f"{name}'s Cycle assessment failed twice. Waiting for the approver; reply '@Orchestrator assess' to retry."


def plan_question(loops):
    """Asked with each posted plan; replies are judged against the latest one."""
    first = " First, settle the dependency loop listed at the top." if loops else ""
    return ("OK to begin as scoped? The plan is in this item's description." + first +
            " Reply '@Orchestrator approve', or '@Orchestrator reject: <your reasons>'.")


def plan_ready_alert(loops, scope_count=0):
    noun = "problem" if scope_count == 1 else "problems"
    return (f"Plan ready: {scope_count} scope {noun}. " +
            ("A dependency loop needs your decision." if loops else "OK to begin as scoped?"))


PLAN_HAS_LOOPS = ("The plan can't be approved while a dependency loop remains (listed at the top of the plan). "
                  "Change the stories to break it, and I'll assess the Cycle again and post a new plan.")

ASSESSMENT_RUNNING = "An assessment is already running for this Cycle. I'll wait for it to finish."

PLAN_REJECT_REASONS = "Please give reasons: @Orchestrator reject: <reasons>."

# The sprint gate's notice on a story moved to In Progress outside the active Cycle.
CYCLE_HOLD = ("A Cycle is active. This story is queued and will start once the Cycle's work "
              "has finished and been merged.")

# The repeated alert for a Cycle story someone moved to Blocked, which has no run error to repeat.
CYCLE_BLOCKED = "This Cycle story is still Blocked."


def local_resume_time(until):
    """Use the same local clock and day for account and assessment waits."""
    until = datetime.fromisoformat(until).astimezone()
    today = datetime.now(timezone.utc).astimezone().date()
    return until.strftime("%H:%M" if until.date() == today else "%a %H:%M")


def assessment_cooldown(name, until):
    return (f"{name} has reached its usage limit; its assessment will start "
            f"at about {local_resume_time(until)}.")

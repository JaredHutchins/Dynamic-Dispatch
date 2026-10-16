"""Executing one run, start to finish.

The sequence: confirm the story is still In Progress, prepare the worktree, run
the agent (sandboxed), check it changed something, run verification
(sandboxed), commit as the agent, confirm the story is still In Progress, push,
open the PR. Before upload, fetch and merge main without committing, repair
conflicts or failing verification with the same agent (at most twice per run),
and commit only the verified combination. A branch with commits that never reached origin skips the agent
and its commit: that work is verified again, then pushed. The exception is work
a human pulled out mid-run by moving the story out of In Progress: that branch
keeps its work and the agent checks it against the current story. A branch that was
pushed but never got its pull request skips the agent, the commit and the push:
it is verified again, then the pull request is opened.

Every failure ends in the same place: the run moves to awaiting_blocked and the
board sync takes it from there. A usage limit isn't a failure: the run closes as
usage_limited, the story stays In Progress, and the dispatcher hands it on. Only
repeated limits block it. Assignment is best-effort; outcome writes go through board sync.
"""

from datetime import date, datetime, timezone
import json
import sqlite3

from . import alerts, agents, assessments, choose, combine, config, docs_line, github, gitops, mainhealth, plane, prepare, prompts, rework, runs, sync, verify
from .redact import log, scrub, warn
from .stories import story_ref

# The earlier run's summary isn't kept when it fails, so a resumed run says what happened instead.
RESUMED_SUMMARY = (
    "This work was finished and verified in an earlier run that could not push it. "
    "It was verified again and pushed without running the agent again."
)
RETRIED_PR_SUMMARY = (
    "This work was finished, verified and pushed in an earlier run whose pull request "
    "could not be opened. It was verified again and the pull request opened without "
    "running the agent again."
)
RESTARTED_SUMMARY = (
    "This work was finished and verified in an earlier run that DD's restart cut off before "
    "it was pushed. It was verified again and pushed without running the agent again."
)
RESTARTED_PR_SUMMARY = (
    "This work was finished, verified and pushed in an earlier run that DD's restart cut off. "
    "It was verified again and its pull request opened, or the one already open reused, "
    "without running the agent again."
)


FINISHED_WORK_NOTE = (
    "\n\nThe work on this branch was finished and tested earlier but never reviewed. "
    "The story was moved out of In Progress before it could be uploaded. "
    "Check this work against the story's current description and acceptance criteria. "
    "Change only what is needed. If it already meets everything, say so and change nothing."
)

UNCHECKED_WORK_NOTE = (
    "\n\nThe work on this branch was finished and tested in an earlier run, but no Docs line that fits "
    "the branch's changes was saved with it, so it hasn't been uploaded. Check that the documentation "
    "is up to date with this work and update it only where needed. Your final message becomes the pull "
    "request description, so summarise all of the branch's work, ending with the Docs line."
)

UNFINISHED_LIMIT = 50 * 1024  # bytes of unfinished changes shown to the agent
DOCS_TRIES = 2  # times an agent is sent back to fix its Docs line


def _earlier_agent(cfg, conn, run):
    """Display name of the agent that last worked in the story's folder before this run."""
    maker = None
    try:
        maker = runs.last_builder(conn, run["issue_id"], run["id"])
    except sqlite3.Error as e:  # the name only labels the note, so the note just can't name the agent
        warn(f"Could not look up which agent left unfinished changes: {e}")
    if maker is None:
        return "An earlier agent"
    return cfg["agents"].get(maker, {}).get("display_name", maker)


def unfinished_note(maker, changes):
    """Returns (note, cut). Scrubbed before cutting, so no part of a secret survives the cut."""
    diff = scrub(changes["diff"])
    cut = len(diff.encode()) > UNFINISHED_LIMIT
    if cut:
        diff = diff.encode()[:UNFINISHED_LIMIT].decode(errors="ignore")
    files = "\n".join(f"- {kind}: {scrub(path)}" for kind, path in changes["files"])
    shown = (
        f"The changes, cut at {UNFINISHED_LIMIT // 1024} KB (every changed file is listed above; "
        "read the files themselves for the rest):"
        if cut else "The changes:"
    )
    note = (
        f"\n{maker} worked on this story in an earlier run that stopped before it finished, "
        "and left uncommitted changes in this folder. They were never tested. Look at them "
        "first: if they are sound, build on them; if not, undo them and start over.\n\n"
        f"Changed files:\n{files}\n\n{shown}\n{diff}\n"
    )
    return note, cut


def _still_in_progress(orch, project_id, issue_id, in_progress_state):
    """True/False, or None if Plane couldn't be reached."""
    try:
        issue = orch.get_issue(project_id, issue_id)
    except plane.PlaneError:
        return None
    return plane.state_id(issue) == in_progress_state


class RunContext:
    """What execute's steps share for one run.

    worktree and leftover_note are filled in as the run goes, and fail reads
    them at the time it is called, so a step sets them here rather than locally.
    The rest are decided by one step and read by later ones.
    """

    def __init__(self, cfg, conn, run):
        self.cfg = cfg
        self.conn = conn
        self.run = run
        self.project_cfg = cfg["projects"][run["project"]]
        self.repo = self.project_cfg["repo_path"]
        self.base = self.project_cfg.get("base_branch", "main")
        self.states = self.project_cfg["states"]
        self.agent_cfg = config.agent(cfg, run["agent"])
        self.orch = plane.orchestrator(cfg)
        self.refresh = run["kind"] == "refresh"
        self.worktree = None
        self.leftover_note = ""
        self.needs_build = False  # the agent still has to build the story
        self.resuming = False
        self.pulled_out = False
        self.retrying_pr = False
        self.earlier = None  # the run whose saved work this one resumes or retries
        self.unchecked = False  # saved work handed back to the agent for its Docs line
        self.changed = False
        self.result = None
        self.combined_note = ""

    def event(self, kind, detail):
        runs.record_event(self.conn, self.run["id"], kind, detail)

    def fail(self, reason):
        if self.refresh:
            reason += "\n\n" + sync.REFRESH_UNCHANGED
            if self.worktree:
                try:
                    if gitops.abort_pending_merge(self.repo, self.worktree):
                        self.event("merge_aborted", "Cancelled unfinished merge; restored last saved work")
                except gitops.GitError as e:
                    reason += f"\nCould not cancel unfinished merge: {e}"
        if self.leftover_note:
            reason += "\n\n" + self.leftover_note
        runs.await_blocked(self.conn, self.run["id"], reason, worktree=str(self.worktree) if self.worktree else None)
        log(f"{story_ref(self.project_cfg, self.run)}: failed: {reason.splitlines()[0]}")
        sync.reconcile(self.cfg, self.conn, runs.get(self.conn, self.run["id"]))

    def cancel(self, note):
        runs.close(self.conn, self.run["id"], runs.CANCELLED, note=note)
        log(f"{story_ref(self.project_cfg, self.run)}: cancelled: {note}")

    def limited(self, result):
        """The agent hit its usage limit: stop, but leave the story In Progress to be handed on."""
        run = self.run
        name = self.agent_cfg["display_name"]
        until = choose.cool_down(self.cfg, self.conn, run["agent"],
                                usage_limit_until=result.get("usage_limit_until"), run_id=run["id"])
        self.event("usage_limit", f"{name} reached its usage limit; cooling down until {until:%H:%M} UTC")
        log(f"{story_ref(self.project_cfg, run)}: {name} reached its usage limit; "
            f"skipping it until {until.astimezone():%H:%M}")
        if runs.limit_streak(self.conn, run["issue_id"], run["id"]) + 1 >= sync.GIVE_UP_AFTER:
            self.event("gave_up", sync.GAVE_UP)
            return self.fail(sync.GAVE_UP)
        runs.close(self.conn, run["id"], runs.LIMITED, note=f"{name} reached its usage limit.")

    def skip_refresh(self, note="Refresh skipped (merged, closed or moved)"):
        self.event("refresh_skipped", note)
        if self.worktree:
            problem = gitops.remove_worktree(self.repo, self.worktree)
            if problem:
                self.event("warning", problem)
        return self.cancel(note)


def _start(cfg, conn, run):
    """Return the context and startup values, or None when the run stops here."""
    if mainhealth.stopped(conn, run["project"], run["issue_id"]):
        runs.defer_run(conn, run["id"])
        return
    ctx = RunContext(cfg, conn, run)
    project_id = ctx.project_cfg["plane_project_id"]
    title = f"{story_ref(ctx.project_cfg, ctx.run)}: {ctx.run['issue_name']}"
    group = config.agent_group(ctx.cfg, ctx.run["agent"])

    # Nothing has happened yet, so an unreachable Plane just means try again later.
    try:
        issue = ctx.orch.get_issue(project_id, ctx.run["issue_id"])
    except plane.PlaneError as e:
        return ctx.cancel(f"Could not read the story before starting ({e}). It will be picked up on the next poll.")
    if ctx.refresh:
        ctx.event("refresh_started", "Refresh started")
        try:
            if (plane.state_id(issue) != ctx.states["in_review"] or
                    not github.pr_is_open(ctx.project_cfg["github_repo"], ctx.run["pr_url"], github.token(ctx.cfg["github"]))):
                return ctx.skip_refresh()
        except github.GitHubError as e:
            return ctx.skip_refresh(f"Could not check pull request; refresh will retry later: {e}")
    elif plane.state_id(issue) != ctx.states["in_progress"]:
        # Moved out while a restart had cut off the run: that run's unpushed work
        # now counts as pulled out, so the agent checks it against the story first.
        previous = runs.last_committed(ctx.conn, ctx.run["issue_id"], ctx.run["id"])
        if previous is not None and previous["status"] == runs.INTERRUPTED:
            runs.mark_committed(ctx.conn, ctx.run["id"])
        return ctx.cancel("The story was moved out of In Progress before work started.")

    # Checked before the started alert and assignment, so a retry after
    # GitHub was unreachable doesn't repeat them.
    try:
        rework_pr = rework.pull_request(ctx.cfg, ctx.conn, ctx.run)
    except github.GitHubError as e:
        ctx.event("warning", f"Could not check whether this is rework: {e}")
        return ctx.cancel(f"Could not check whether this is rework ({e}). It will be picked up on the next poll.")
    except gitops.GitError as e:
        return ctx.fail(f"Could not check whether this is rework:\n\n{e}")

    if not ctx.refresh:
        alerts.send(ctx.cfg, ctx.run["project"], ctx.run["issue_id"], '', row=ctx.run,
                    suffix=f" started by {ctx.agent_cfg['display_name']}")
        # Local date, like the other times DD shows; later runs leave a date DD has already set.
        start_date = None if runs.start_date_set(ctx.conn, ctx.run["issue_id"]) else date.today().isoformat()
        plane.assign_story(ctx.cfg, ctx.orch, project_id, ctx.run["issue_id"], ctx.run["agent"], ctx.run["id"], start_date,
                           agent_label_id=ctx.project_cfg.get("agent_label_id"),
                           current_labels=issue.get("labels") or [])
        restarts = runs.restarts(ctx.conn, ctx.run["issue_id"], ctx.run["id"])
        if restarts:
            # Marked with the interrupted run, so later runs of the story don't say it again.
            try:
                sync.notice(ctx.cfg, ctx.run["project"], ctx.run["issue_id"], f"run {restarts[0]} restarted",
                            sync.restarted_message(len(restarts)))
            except plane.PlaneError as e:
                ctx.event("warning", f"Could not post the comment about the restart; carrying on: {e}")

    try:
        existing = gitops.registered_worktree(ctx.repo, ctx.run["branch"])
        if existing is not None and existing.is_dir() and gitops.abort_pending_merge(ctx.repo, existing):
            ctx.event("merge_aborted", "Cancelled unfinished merge; restored last saved work")
    except gitops.GitError as e:
        return ctx.fail(f"Could not cancel the unfinished merge:\n\n{e}")

    saved = None
    try:
        if existing is not None and existing.is_dir() and existing.stat().st_gid != group:
            saved = gitops.rebuild_for_group(
                ctx.repo, existing, ctx.run["branch"], f"{ctx.run['branch']}-leftover-{ctx.run['id']}",
                ctx.agent_cfg["git_author"])
            ctx.leftover_note = "Rebuilt the work folder for the building agent's group."
            if saved:
                ctx.leftover_note += f" Uncommitted work was saved on local branch {saved}; it was not pushed."
            ctx.event("worktree_rebuilt", ctx.leftover_note)
    except gitops.GitError as e:
        return ctx.fail(f"Could not rebuild the work folder for this agent:\n\n{e}")

    if ctx.run["handed_from"]:
        # Said now, once the folder is rebuilt, so the comment can name the leftover branch.
        stopped = ctx.cfg["agents"].get(ctx.run["handed_from"], {}).get("display_name", ctx.run["handed_from"])
        try:
            sync.notice(ctx.cfg, ctx.run["project"], ctx.run["issue_id"], f"run {ctx.run['id']} switched",
                        sync.switched_message(stopped, ctx.agent_cfg["display_name"], saved))
        except plane.PlaneError as e:
            ctx.event("warning", f"Could not post the comment about switching agents; carrying on: {e}")

    return ctx, project_id, title, group, issue, rework_pr, existing, saved


def _prepare(ctx, group, existing, saved):
    """Set up the work folder and decide what kind of run this is.

    Return True to carry on, or None when the run stops here.
    """
    if ctx.refresh:
        ctx.event("worktree", "Recreating work folder from the story branch")
        try:
            if not gitops.branch_exists(ctx.repo, ctx.run["branch"]):
                raise gitops.GitError("The saved story branch is missing; nothing was uploaded.")
            if existing is not None and existing.is_dir():
                problem = gitops.remove_worktree(ctx.repo, existing)
                if problem:
                    raise gitops.GitError(problem)
            ctx.worktree, _ = gitops.ensure_worktree(
                ctx.repo, ctx.project_cfg["worktree_root"], ctx.run["branch"], ctx.base, group=group)
        except gitops.GitError as e:
            return ctx.fail(f"Could not prepare a worktree:\n\n{e}")
        ctx.result = {}
        return True

    # A human may have edited the story while it was out of In Progress.
    # Keep its finished work, but have the agent check the current requirements.
    try:
        previous = runs.last_committed(ctx.conn, ctx.run["issue_id"], ctx.run["id"])
        ctx.pulled_out = (
            previous is not None and previous["status"] == runs.CANCELLED
            and gitops.branch_exists(ctx.repo, ctx.run["branch"])
            and gitops.has_unpushed_commits(ctx.repo, ctx.run["branch"])
        )
    except gitops.GitError as e:
        return ctx.fail(f"Could not inspect the branch:\n\n{e}")
    # Work that reached origin in a run whose pull request then failed only
    # needs the pull request. Without the local branch there is nothing to
    # open one from, so the story runs as new.
    try:
        pushed = runs.last_pushed(ctx.conn, ctx.run["issue_id"], ctx.run["id"])
        ctx.retrying_pr = (
            pushed is not None and not pushed["pr_url"]
            and gitops.branch_exists(ctx.repo, ctx.run["branch"])
        )
    except gitops.GitError as e:
        return ctx.fail(f"Could not inspect the branch:\n\n{e}")

    ctx.event("worktree", "Setting up work folder")
    try:
        ctx.worktree, reused = gitops.ensure_worktree(
            ctx.repo, ctx.project_cfg["worktree_root"], ctx.run["branch"], ctx.base, group=group)
    except gitops.GitError as e:
        return ctx.fail(f"Could not prepare a worktree:\n\n{e}")
    log(f"{story_ref(ctx.project_cfg, ctx.run)}: worktree {'reused' if reused else 'created'}: {ctx.worktree}")

    # Unpushed work normally passed verification. A main hold instead records
    # its snapshot explicitly, and verifies it after combining with green main.
    # Either way, keep the finished agent's work rather than running it again.
    try:
        ctx.resuming = gitops.has_unpushed_commits(ctx.repo, ctx.run["branch"])
    except gitops.GitError as e:
        return ctx.fail(f"Could not inspect the branch:\n\n{e}")
    # Unpushed commits on top are pushed with the pull request by the resume below.
    ctx.retrying_pr = ctx.retrying_pr and not ctx.resuming

    if ctx.run["hold_data"]:
        ctx.result = json.loads(ctx.run["hold_data"])
        ctx.changed = gitops.has_changes(ctx.repo, ctx.worktree)
    elif ctx.retrying_pr:
        summary = RESTARTED_PR_SUMMARY if pushed["status"] == runs.INTERRUPTED else RETRIED_PR_SUMMARY
        ctx.event("pr_retry", summary)
        log(f"{story_ref(ctx.project_cfg, ctx.run)}: branch was pushed without a pull request; "
            "opening it instead of running the agent")
        ctx.result = {"summary": summary}
        ctx.earlier = pushed
        try:
            ctx.changed = gitops.has_changes(ctx.repo, ctx.worktree)
            ahead = gitops.has_commits_beyond_base(ctx.repo, ctx.run["branch"], ctx.base)
        except gitops.GitError as e:
            return ctx.fail(f"Could not inspect the branch:\n\n{e}")
        if ctx.changed:
            # Verification would check files that the pull request doesn't hold.
            return ctx.fail(
                f"Branch {ctx.run['branch']} was pushed without a pull request, but {ctx.worktree} "
                "also has uncommitted changes, so no pull request was opened. "
                "Commit or discard them, then move the story back to In Progress."
            )
        if not ahead:
            return ctx.fail(
                f"Branch {ctx.run['branch']} has no commits that {ctx.base} doesn't already have, "
                "so there is no pull request to open. Its work may already have been merged; "
                "if so, move the story to Done."
            )
    elif ctx.resuming and not ctx.pulled_out:
        restarted = previous is not None and previous["status"] == runs.INTERRUPTED
        summary = RESTARTED_SUMMARY if restarted else RESUMED_SUMMARY
        ctx.event("resumed", summary)
        log(f"{story_ref(ctx.project_cfg, ctx.run)}: branch has unpushed work; pushing it instead of running the agent")
        ctx.result = {"summary": summary}
        ctx.earlier = previous
        try:
            ctx.changed = gitops.has_changes(ctx.repo, ctx.worktree)
        except gitops.GitError as e:
            return ctx.fail(f"Could not inspect the worktree:\n\n{e}")
        if ctx.changed:
            # Verification would check files that the push leaves behind.
            return ctx.fail(
                f"Branch {ctx.run['branch']} has committed work that was never pushed, "
                f"but {ctx.worktree} also has uncommitted changes, so nothing was pushed. "
                "Commit or discard them, then move the story back to In Progress."
            )
    else:
        if ctx.pulled_out:
            try:
                if saved is not None or gitops.has_changes(ctx.repo, ctx.worktree):
                    return ctx.fail("The branch also has uncommitted changes, so nothing was pushed. "
                                "Commit or discard them, then move the story back to In Progress.")
            except gitops.GitError as e:
                return ctx.fail(f"Could not inspect the worktree:\n\n{e}")
        ctx.needs_build = True
    return True


def _build(ctx, title, issue, rework_pr, saved, group):
    """Run the agent if the story needs it, then verify and save its work.

    Return True to carry on, or None when the run stops here.
    """
    if not ctx.needs_build and not _check_saved_docs(ctx):
        return
    if ctx.needs_build:
        # Changes an earlier run left uncommitted are handed on rather than redone.
        restored = saved is not None
        maker = _earlier_agent(ctx.cfg, ctx.conn, ctx.run) if restored else None
        try:
            if restored:
                ctx.event("unfinished_found", f"Unfinished changes by {maker} were saved on local branch {saved}")
                gitops.restore_unfinished(ctx.repo, ctx.worktree, ctx.run["branch"], saved, group)
                ctx.event("unfinished_restored", f"Put the unfinished changes from {saved} back into the new "
                      "work folder, uncommitted; the branch is kept")
            unfinished = gitops.unfinished_changes(ctx.repo, ctx.worktree, UNFINISHED_LIMIT)
        except gitops.GitError as e:
            return ctx.fail(f"Could not hand on the unfinished changes:\n\n{e}")
        if not _prepare_checkout(ctx, issue, bool(unfinished or restored or ctx.pulled_out or rework_pr)):
            return
        prompt = (prompts.build_prompt(ctx.project_cfg, issue) + rework.prompt(rework_pr)
                  + docs_line.instruction(config.docs(ctx.project_cfg), rework=bool(rework_pr)))
        if ctx.pulled_out:
            prompt += UNCHECKED_WORK_NOTE if ctx.unchecked else FINISHED_WORK_NOTE
        if unfinished:
            maker = maker or _earlier_agent(ctx.cfg, ctx.conn, ctx.run)
            count = len(unfinished["files"])
            if not restored:
                ctx.event("unfinished_found", f"Unfinished changes by {maker} in {count} files")
            note, cut = unfinished_note(maker, unfinished)
            prompt += note
            ctx.event("unfinished_in_prompt", f"Told {ctx.agent_cfg['display_name']} about {count} files of unfinished "
                  "changes" + (f", with the changes cut at {UNFINISHED_LIMIT // 1024} KB" if cut else ""))
        ctx.event("agent_started", f"Agent started: {ctx.agent_cfg['display_name']}")
        runs.record_usage(ctx.conn, ctx.run["id"], ctx.run["agent"])
        result = None
        try:
            result = agents.run(ctx.cfg, ctx.agent_cfg, prompt, ctx.worktree)
        finally:
            ctx.event("agent_finished", "Agent finished" if result else "Agent stopped unexpectedly")
        runs.record_usage(ctx.conn, ctx.run["id"], ctx.run["agent"], result.get("usage"), result.get("cost_usd"))
        ctx.result = result
        if result.get("usage_limit"):
            return ctx.limited(result)
        if not result["ok"]:
            return ctx.fail(f"{ctx.agent_cfg['display_name']} did not complete the story:\n\n{result.get('error')}")

        try:
            ctx.changed = gitops.has_changes(ctx.repo, ctx.worktree)
        except gitops.GitError as e:
            return ctx.fail(f"Could not inspect the worktree:\n\n{e}")
        outcome = rework.finish(ctx.cfg, ctx.conn, ctx.run, result, ctx.changed, ctx.pulled_out, rework_pr, ctx.worktree)
        if outcome == rework.NEEDS_DECISION:
            sync.reconcile(ctx.cfg, ctx.conn, runs.get(ctx.conn, ctx.run["id"]))
            return
        if outcome:
            return ctx.fail(outcome)
        if not _check_docs(ctx, issue, rework_pr):
            return

    if mainhealth.hold(ctx.cfg, ctx.conn, ctx.run, ctx.worktree, ctx.result):
        return

    if not ctx.run["hold_data"]:
        if not _prepare_checkout(ctx, issue, True):
            return
        if ctx.project_cfg.get("prepare_commands") and not _check_docs(ctx, issue, rework_pr):
            return
        ctx.event("testing", "Running tests")
        ok, verify_output = verify.run_verify(ctx.cfg, ctx.project_cfg, ctx.agent_cfg, ctx.worktree)
        if mainhealth.hold(ctx.cfg, ctx.conn, ctx.run, ctx.worktree, ctx.result):
            return
        if not ok:
            return ctx.fail(f"Verification failed, so this never reached review:\n\n{verify_output}")

    if (not ctx.resuming or ctx.pulled_out) and not ctx.retrying_pr and ctx.changed:
        ctx.event("saving", "Saving changes")
        try:
            gitops.commit_all(ctx.repo, ctx.worktree, scrub(title), ctx.agent_cfg["git_author"])
        except gitops.GitError as e:
            return ctx.fail(f"Could not commit the agent's changes:\n\n{e}")
        runs.mark_committed(ctx.conn, ctx.run["id"])

    if ctx.resuming or ctx.retrying_pr:
        ctx.combined_note = runs.combined_note(ctx.conn, ctx.run["issue_id"]) or ""
    return True


def _check_docs(ctx, issue, rework_pr):
    """Send the agent back until its Docs line fits the branch's changes, at most DOCS_TRIES times.

    Checked before verification and the commit, so a fix is tested and saved
    like the rest of the work, and saved or held work has already passed.
    Return True to carry on, or None when the run stops here.
    """
    settings = config.docs(ctx.project_cfg)
    name = ctx.agent_cfg["display_name"]
    attempt = 0
    while True:
        try:
            changed = gitops.branch_changed_files(ctx.repo, ctx.worktree, ctx.base)
        except gitops.GitError as e:
            return ctx.fail(f"Could not list the branch's changes to check the Docs line:\n\n{e}")
        found = docs_line.problem(settings, ctx.result.get("summary"), changed)
        if found is None:
            runs.save_docs_line(ctx.conn, ctx.run["id"], docs_line.last_line(ctx.result.get("summary")))
            return True
        ctx.event("docs_line_wrong", found)
        if attempt >= DOCS_TRIES:
            return ctx.fail(f"The summary's Docs line was still wrong after {DOCS_TRIES} tries, so no "
                            f"pull request was opened or updated:\n\n{found}")
        attempt += 1
        message = f"{found} Sent back to {name} to fix (try {attempt} of {DOCS_TRIES})."
        try:
            sync.notice(ctx.cfg, ctx.run["project"], ctx.run["issue_id"], f"run {ctx.run['id']} docs {attempt}", message)
        except plane.PlaneError as e:
            ctx.event("warning", f"Could not post the comment about the Docs line; carrying on: {e}")
        ctx.event("sent_back", message)
        prompt = (prompts.build_prompt(ctx.project_cfg, issue) + docs_line.instruction(settings)
                  + docs_line.send_back_note(settings, found, changed, scrub(ctx.result.get("summary") or "")))
        ctx.event("agent_started", f"Docs line fix started: {name}")
        result = None
        try:
            result = agents.run(ctx.cfg, ctx.agent_cfg, prompt, ctx.worktree)
        finally:
            ctx.event("agent_finished", "Docs line fix finished" if result else "Docs line fix stopped unexpectedly")
        runs.record_usage(ctx.conn, ctx.run["id"], ctx.run["agent"], result.get("usage"), result.get("cost_usd"),
                          additive=True)
        if result.get("usage_limit"):
            return ctx.limited(result)
        if not result["ok"]:
            return ctx.fail(f"{name} did not finish fixing the Docs line:\n\n{result.get('error')}")
        # A decision request made earlier in the round still stands.
        result["needs_decision"] = ctx.result.get("needs_decision", False)
        ctx.result = result
        try:
            ctx.changed = gitops.has_changes(ctx.repo, ctx.worktree)
        except gitops.GitError as e:
            return ctx.fail(f"Could not inspect the worktree:\n\n{e}")
        outcome = rework.finish(ctx.cfg, ctx.conn, ctx.run, result, ctx.changed, ctx.pulled_out, rework_pr, ctx.worktree)
        if outcome == rework.NEEDS_DECISION:
            sync.reconcile(ctx.cfg, ctx.conn, runs.get(ctx.conn, ctx.run["id"]))
            return
        if outcome:
            return ctx.fail(outcome)


def _check_saved_docs(ctx):
    """Check the Docs line of work an earlier run saved, which reaches review without the agent.

    Held work keeps its agent's summary; resumed work and pull request retries
    have the line saved when the earlier run's check passed. Work saved without
    one, as before the check existed, or whose line no longer fits, can't be
    sent back to an agent that isn't running, so it is handed to this run's
    agent as finished work instead, and that agent's line is checked as usual.
    Nothing is pushed or opened before then. Return True to carry on, or None
    when the run stops here.
    """
    settings = config.docs(ctx.project_cfg)
    if ctx.run["hold_data"]:
        line = docs_line.last_line(ctx.result.get("summary"))
    else:
        line = ctx.earlier["docs_line"] if ctx.earlier is not None else None
    try:
        changed = gitops.branch_changed_files(ctx.repo, ctx.worktree, ctx.base)
    except gitops.GitError as e:
        return ctx.fail(f"Could not list the branch's changes to check the Docs line:\n\n{e}")
    found = docs_line.problem(settings, line, changed)
    if found is None:
        runs.save_docs_line(ctx.conn, ctx.run["id"], line)
        if line and docs_line.last_line(ctx.result.get("summary")) is None:
            ctx.result["summary"] = f"{ctx.result.get('summary') or ''}\n\n{line}".strip()
        return True
    ctx.event("docs_line_wrong", f"The saved work's Docs line isn't right: {found} Handed to "
                                 f"{ctx.agent_cfg['display_name']} as finished work to check.")
    ctx.needs_build = ctx.pulled_out = ctx.unchecked = True
    ctx.retrying_pr = False  # the agent's work is pushed with the pull request
    return True


def execute(cfg, conn, run):
    if run['kind'] == 'plan':
        return assessments.run_assessment(cfg, conn, run)
    started = _start(cfg, conn, run)
    if started is None:
        return
    ctx, project_id, title, group, issue, rework_pr, existing, saved = started

    if not _prepare(ctx, group, existing, saved):
        return
    if not ctx.needs_build and not _prepare_checkout(ctx, issue, True):
        return
    if not ctx.refresh and not _build(ctx, title, issue, rework_pr, saved, group):
        return
    combination = combine.with_main(ctx, issue, title, ctx.result)
    if combination is None:
        return
    meanwhile, note = combination
    if note:
        ctx.retrying_pr = False  # a PR retry now has a new verified merge to upload
        ctx.combined_note = note

    if mainhealth.hold(ctx.cfg, ctx.conn, ctx.run, ctx.worktree, ctx.result):
        return

    if ctx.refresh:
        return _upload_refresh(ctx, project_id, meanwhile)
    _upload_story(ctx, project_id, title)


def _upload_refresh(ctx, project_id, meanwhile):
    """Push a refreshed branch to its open pull request and record the outcome."""
    # Both services must positively confirm this is still review work.
    try:
        issue = ctx.orch.get_issue(project_id, ctx.run["issue_id"])
        if (plane.state_id(issue) != ctx.states["in_review"] or
                not github.pr_is_open(ctx.project_cfg["github_repo"], ctx.run["pr_url"], github.token(ctx.cfg["github"]))):
            return ctx.skip_refresh()
    except (plane.PlaneError, github.GitHubError) as e:
        return ctx.skip_refresh(f"Could not confirm review is still open; nothing uploaded; will retry: {e}")
    ctx.event("uploading", "Uploading refreshed branch")
    try:
        gitops.push(ctx.repo, ctx.worktree, ctx.run["branch"])
    except gitops.GitError as e:
        return ctx.fail(f"Could not push refreshed branch:\n\n{e}")
    runs.mark_pushed(ctx.conn, ctx.run["id"])
    ctx.event("refresh_uploaded", "Pull request updated with verified combination")
    recorded = runs.get(ctx.conn, ctx.run["id"])
    runs.await_review(ctx.conn, ctx.run["id"],
                      summary=sync.refresh_summary(meanwhile) + ("\n\n" + ctx.leftover_note if ctx.leftover_note else ""),
                      usage=json.loads(recorded["usage"] or "null"), cost_usd=recorded["cost_usd"],
                      pr_url=ctx.run["pr_url"], worktree=str(ctx.worktree))
    return sync.reconcile(ctx.cfg, ctx.conn, runs.get(ctx.conn, ctx.run["id"]))


def _upload_story(ctx, project_id, title):
    """Push the story's work, open or reuse its pull request and record the outcome."""
    # Last chance to respect a human decision before anything leaves the Pi.
    # Only a definite "still In Progress" lets the work out.
    still = _still_in_progress(ctx.orch, project_id, ctx.run["issue_id"], ctx.states["in_progress"])
    if still is False and ctx.retrying_pr:
        return ctx.cancel(
            "The story was moved out of In Progress while its work was verified again. "
            f"Branch {ctx.run['branch']} is pushed, but no pull request was opened."
        )
    if still is None and ctx.retrying_pr:
        return ctx.fail(
            "Plane could not be reached to confirm the story was still In Progress, "
            f"so the pull request was not opened. Branch {ctx.run['branch']} is pushed.\n\n"
            "To open the pull request, move the story back to In Progress. "
            "The agent will not run again."
        )
    if still is False:
        return ctx.cancel(
            "The story was moved out of In Progress while the agent was working. "
            f"The work is committed locally in {ctx.worktree} but was not pushed."
        )
    if still is None:
        return ctx.fail(
            "Plane could not be reached to confirm the story was still In Progress, "
            f"so nothing was pushed. The work passed verification and is committed "
            f"on branch {ctx.run['branch']}, in {ctx.worktree}.\n\n"
            "To push it and open the pull request, move the story back to In Progress. "
            "The agent will not run again: the saved work is verified once more, then pushed."
        )

    if not ctx.retrying_pr:
        ctx.event("uploading", "Uploading branch")
        try:
            gitops.push(ctx.repo, ctx.worktree, ctx.run["branch"])
        except gitops.GitError as e:
            return ctx.fail(f"Could not push the branch:\n\n{e}")
        runs.mark_pushed(ctx.conn, ctx.run["id"])

    summary = ctx.result.get("summary") or "(no summary provided)"
    if ctx.combined_note:
        summary = f"{ctx.combined_note}\n\n{summary}"
    comment_summary = summary + ("\n\n" + ctx.leftover_note if ctx.leftover_note else "")
    description = f"{summary}\n\n---\nStory {title}\nAgent: {ctx.agent_cfg['display_name']}"
    try:
        pr_url, already = github.open_pr(
            ctx.project_cfg["github_repo"],
            ctx.run["branch"],
            ctx.base,
            title,
            description,
            github.token(ctx.cfg["github"]),
        )
    except github.GitHubError as e:
        return ctx.fail(
            f"The branch was pushed, but the pull request could not be opened:\n\n{e}\n\n"
            "To try again, move the story back to In Progress. The agent will not run "
            "again: the pushed work is verified once more, then the pull request is opened."
        )

    if already:
        _replace_description(ctx.cfg, ctx.project_cfg, ctx.run, already, description, ctx.event)
    else:
        ctx.event("pr_opened", f"Pull request opened: {pr_url}")
    recorded = runs.get(ctx.conn, ctx.run["id"])
    runs.await_review(
        ctx.conn, ctx.run["id"], summary=comment_summary, usage=json.loads(recorded["usage"] or "null"),
        cost_usd=recorded["cost_usd"], pr_url=pr_url, worktree=str(ctx.worktree),
        needs_decision=ctx.result.get("needs_decision", False),
    )
    log(f"{story_ref(ctx.project_cfg, ctx.run)}: pull request: {pr_url}, built by {ctx.agent_cfg['display_name']}")
    sync.reconcile(ctx.cfg, ctx.conn, runs.get(ctx.conn, ctx.run["id"]))


def _replace_description(cfg, project_cfg, run, pull, description, event):
    """Make a reused pull request describe the code as it stands now.

    Best-effort: the old description is kept as a comment first, and a failure
    leaves it in place with a warning rather than holding up the story.
    """
    # Local time, as in DD's other messages, since the approver reads this on GitHub.
    now = datetime.now(timezone.utc).astimezone()
    heading = f"Previous description, replaced by run {run['id']} on {now:%Y-%m-%d %H:%M %Z}"
    # Hidden in the rendered comment; lets a retry of this run see it was posted.
    marker = f"<!-- Dynamic Dispatch run {run['id']}: previous description -->"
    try:
        github.replace_description(project_cfg["github_repo"], pull, description, heading, marker,
                                   github.token(cfg["github"]))
    except Exception as e:  # a timeout or odd reply must not block work that is already pushed
        event("warning", f"Could not replace the pull request description; it still shows "
                         f"an earlier run's summary: {e}")
        warn(f"{story_ref(project_cfg, run)}: could not replace the pull request description: {e}")
        return
    event("pr_described", f"Pull request description replaced: {pull['html_url']}")


def execute_thread(cfg, run):
    """Own the worker connection and retain the account slot through cleanup."""
    conn = runs.connect(cfg["db_path"], initialize=False)
    try:
        execute(cfg, conn, run)
    except Exception as e:  # a bug must fail one run, not the service
        runs.record_event(conn, run["id"], "warning", f"Unexpected error: {e!r}")
        warn(f"{story_ref(cfg['projects'].get(run['project'], {}), run)}: unexpected error: {e!r}")
        if runs.get(conn, run["id"])["status"] == runs.RUNNING:
            runs.await_blocked(conn, run["id"], f"Unexpected error in Dynamic Dispatch: {e!r}")
            sync.reconcile(cfg, conn, runs.get(conn, run["id"]))
    finally:
        try:
            runs.release_slot(conn, run["id"])
        finally:
            conn.close()


def _prepare_checkout(ctx, issue, has_work):
    """A bare checkout failure belongs to the environment; saved work can be repaired."""
    if not ctx.project_cfg.get("prepare_commands"):
        return True
    attempt = runs.repair_attempts(ctx.conn, ctx.run["id"])
    while True:
        ok, output = prepare.run(ctx.cfg, ctx.project_cfg, ctx.agent_cfg, ctx.worktree)
        if ok:
            ctx.changed = gitops.has_changes(ctx.repo, ctx.worktree)
            if ctx.changed:
                ctx.resuming = ctx.retrying_pr = False
            return True
        ctx.event("prepare_failed", output)
        if not has_work:
            return ctx.fail("Environment preparation failed before the agent started; "
                            "no agent work was attempted.\n\n" + output)
        if attempt >= 2:
            return ctx.fail("Preparation still failed after 2 repair tries:\n\n" + output)
        message = f"Preparation failed; sent back to the agent (try {attempt + 1} of 2).\n\n{output}"
        try:
            sync.send_back(ctx.cfg, ctx.run, attempt + 1, message)
        except plane.PlaneError as e:
            ctx.event("warning", f"Could not post preparation repair comment: {e}")
        ctx.event("sent_back", message)
        runs.record_repair(ctx.conn, ctx.run["id"])
        attempt += 1
        prompt = (prompts.build_prompt(ctx.project_cfg, issue)
                  + docs_line.instruction(config.docs(ctx.project_cfg))
                  + "\nRepair checkout preparation:\n" + output)
        ctx.event("agent_started", "Preparation repair started")
        result = None
        try:
            result = agents.run(ctx.cfg, ctx.agent_cfg, prompt, ctx.worktree)
        finally:
            ctx.event("agent_finished", "Preparation repair finished" if result else "Repair stopped unexpectedly")
        runs.record_usage(ctx.conn, ctx.run["id"], ctx.run["agent"], result.get("usage"),
                          result.get("cost_usd"), additive=True)
        if result.get("usage_limit"):
            return ctx.limited(result)
        if not result["ok"]:
            return ctx.fail("Preparation repair did not complete:\n" + str(result.get("error")))
        ctx.result = result
        ctx.changed = gitops.has_changes(ctx.repo, ctx.worktree)

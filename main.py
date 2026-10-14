#!/usr/bin/env python3
"""Dynamic Dispatch - watch the board, run agents, open pull requests.

Each cycle: retry pull request lights and board updates still owed, queue a
check of main when it has moved, queue runs for stories newly in In Progress
(or waiting there for an agent), queue refreshes, drain the queue, then remove
failed runs' worktrees that have been kept long enough. While an update to DD's
own code is pending, no new runs start; once nothing is running DD updates its
checkout and exits, for systemd to start it on the new code.
Each agent account has one worker slot; the service keeps polling while
workers run.
"""

import argparse
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from dispatch import (add_project, alerts, choose, cleanup, config, cycles, doctor, github, gitops, lights, mainhealth, plane,
                      plane_apps, plane_install, proc, redact, runs, selfupdate, setup, sprint, stories, sync, worker)
from dispatch.redact import log, warn
from dispatch.stories import story_ref


# Human-only stories already reported, per project, so the skip is logged once
# rather than every poll. Rebuilt each poll: a story that leaves In Progress or
# loses the label and later comes back is reported again.
_skipped = {}

# Stories left waiting because every allowed agent is cooling down, per
# project, logged once in the same way.
_waiting = {}


def _labelled(issue, label_id):
    """Whether the issue carries the label, whether Plane gave IDs or nested objects."""
    labels = [l.get("id") if isinstance(l, dict) else l for l in issue.get("labels") or []]
    return label_id in labels


def poll_once(cfg, conn, update=None):
    """Queue idle stories, or explain why a pending DD update keeps them waiting."""
    orch = plane.orchestrator(cfg)
    for project_name, project_cfg in cfg["projects"].items():
        stopped = mainhealth.stopped(conn, project_name)
        fix = mainhealth.fix_story(conn, project_name) if stopped else None
        if update is None and stopped and fix is None:
            continue
        try:
            issues = orch.issues_in_state(
                project_cfg["plane_project_id"], project_cfg["states"]["in_progress"]
            )
        except plane.PlaneError as e:
            runs.record_event(conn, None, "warning", f"Could not read board; will retry: {e}",
                              project=project_name)
            warn(f"[{project_name}] could not read the board: {e}")
            continue
        if update is not None:
            human_label = project_cfg.get("human_only_label_id")
            issues = [i for i in issues if not (human_label and _labelled(i, human_label))]
            selfupdate.announce_waiting(cfg, conn, update, project_name, issues)
            continue
        if stopped:
            # Only the fix story runs; every other story's waits and warnings stay as they were.
            _queue_stories(cfg, conn, project_name, project_cfg, [i for i in issues if i["id"] == fix])
            continue

        runs.clear_repeats(conn, project=project_name)
        # A waiting story moved out of In Progress is left alone: no retry, no more comments.
        present = {issue["id"] for issue in issues}
        for wait in runs.waits(conn, project_name):
            if wait["issue_id"] not in present:
                runs.end_wait(conn, wait["issue_id"])
                runs.record_event(conn, None, "wait_dropped", "Moved out of In Progress; no longer waiting",
                                  project=project_name, issue_id=wait["issue_id"])
                log(f"[{project_name}] stopped waiting for a story moved out of In Progress")
        skipped, waiting = _queue_stories(cfg, conn, project_name, project_cfg, issues)
        runs.clear_repeats(conn, project=project_name, waiting=waiting)
        _skipped[project_name] = skipped
        _waiting[project_name] = waiting


def _queue_stories(cfg, conn, project_name, project_cfg, issues):
    """Queue each story with no active run. Returns the stories skipped as human-only and those waiting."""
    human_label = project_cfg.get("human_only_label_id")
    skipped, waiting = set(), set()
    # Work outside an active Cycle waits for it to finish; human-only stories are never DD's to hold.
    held = sprint.held_stories(cfg, conn, project_name, project_cfg,
                               [i for i in issues if not (human_label and _labelled(i, human_label))])
    for issue in issues:
        key = str(issue.get("sequence_id", ""))
        if human_label and _labelled(issue, human_label):
            skipped.add(issue["id"])
            if issue["id"] not in _skipped.get(project_name, ()):
                log(f"[{project_name}] skipping {story_ref(project_cfg, {'issue_key': key})}, labelled human-only: {issue.get('name', '')}")
            continue
        # A Cycle's plan item is where the approver talks to DD, never something to build.
        if runs.is_active(conn, issue["id"]) or runs.is_plan_item(conn, issue["id"]):
            continue
        if issue["id"] in held:
            continue
        wait = runs.wait_for(conn, issue["id"])
        agent = choose.agent_for(cfg, conn, project_cfg, issue["id"])
        if agent is None:
            waiting.add(issue["id"])
            runs.record_event(conn, None, "warning", "No agent available; will retry next cycle",
                              project=project_name, issue_id=issue["id"])
        if agent is None and wait is not None:
            if not wait["comment_posted"]:
                _announce_wait(cfg, conn, wait)
            continue
        if agent is None:
            _wait(cfg, conn, {"project": project_name, "issue_id": issue["id"],
                             "id": None, "issue_key": key})
            continue
        branch = stories.branch_name(project_cfg, issue)
        run_id = runs.enqueue(conn, project=project_name, issue=issue, agent=agent, branch=branch)
        if run_id:
            log(f"[{project_name}] queued {story_ref(project_cfg, {'issue_key': key})} for {cfg['agents'][agent]['display_name']}: "
                f"{issue.get('name', '')}")
            if wait is not None:
                runs.end_wait(conn, issue["id"])
                runs.record_event(conn, run_id, "resumed_after_wait",
                                  f"Resumed after waiting: {cfg['agents'][agent]['display_name']} is available")
    return skipped, waiting


def poll_refreshes(cfg, conn, revisions=None):
    """Queue only DD's reviewed, still-open pull requests that lack latest main.

    The same read of the board keeps each pull request's light in step with its
    story: red in Blocked, yellow during rework, and restored when it moves on.
    It also moves a story to Done once its pull request is merged.
    revisions holds main's latest commit by project, as the main check already
    read it this cycle; commits read here are added to it. It is also returned.
    """
    orch = plane.orchestrator(cfg)
    revisions = {} if revisions is None else revisions
    for project, project_cfg in cfg["projects"].items():
        stopped = mainhealth.stopped(conn, project)
        fix = mainhealth.fix_story(conn, project) if stopped else None
        if stopped and fix is None:
            continue
        states = project_cfg["states"]
        try:
            stories = {issue["id"]: issue for issue in orch.issues(project_cfg["plane_project_id"])}
        except plane.PlaneError as e:
            runs.record_event(conn, None, "warning", f"Could not read review board; will retry: {e}",
                              project=project)
            continue
        target = revisions.get(project)
        target_error = None
        candidates = {row["issue_id"]: row for row in runs.review_candidates(conn, project)}
        for issue_id, issue in stories.items():
            if stopped and issue_id != fix:
                continue
            previous = candidates.get(issue_id)
            if previous is None:
                continue
            state = plane.state_id(issue)
            in_review = state == states["in_review"]
            def event(kind, detail):
                runs.record_event(conn, None, kind, detail, project=project, issue_id=issue_id)
            # With states.done set, the merge check may already have asked GitHub about this pull request.
            try:
                pr_state = sync.finish_merged(cfg, conn, project, issue, previous)
            except Exception as e:
                # Best-effort: a timeout or odd reply must not stop this story's light and refresh checks.
                event("warning", f"Could not check whether the pull request was merged; will retry: {e}")
                pr_state = None
            if pr_state == "merged":
                continue
            def check_main():
                nonlocal target, target_error
                token = github.token(cfg["github"])
                if in_review and not (pr_state == "open" if pr_state else
                                      github.pr_is_open(project_cfg["github_repo"], previous["pr_url"], token)):
                    event("refresh_skipped", "Refresh skipped (merged, closed or moved)")
                    return None
                if target_error is not None:
                    raise target_error
                if target is None:
                    try:
                        target = revisions[project] = github.main_revision(
                            project_cfg["github_repo"], project_cfg.get("base_branch", "main"), token)
                    except github.GitHubError as e:
                        target_error = e
                        raise
                if gitops.branch_contains_main(project_cfg["repo_path"], previous["branch"], target):
                    if in_review:
                        event("main_unchanged", "Main unchanged: pull request already contains latest main")
                    return True
                return False

            try:
                if not lights.check_story(cfg, conn, project, issue_id, state, check_main):
                    continue
                if not in_review or runs.is_active(conn, issue_id):
                    continue  # the story's next run brings in latest main
                builder = previous["agent"]
                if builder in config.project_agents(project_cfg) and builder in cfg["agents"]:
                    agent = builder if choose.available(cfg, conn, [builder]) else None
                else:
                    agent = choose.agent_for(cfg, conn, project_cfg, issue_id)
                if agent is None:
                    event("refresh_waiting", "Builder busy or cooling down; refresh will retry later")
                    continue
                runs.enqueue(conn, project=project, issue=issue, agent=agent,
                             branch=previous["branch"], kind="refresh", pr_url=previous["pr_url"])
            except (plane.PlaneError, github.GitHubError, gitops.GitError) as e:
                event("warning", f"Could not check pull request for refresh; will retry: {e}")
    return revisions


def _announce_wait(cfg, conn, wait):
    """Post the story's one waiting comment. If Plane can't take it, the next poll tries again."""
    name = cfg["agents"].get(wait["agent"], {}).get("display_name", wait["agent"])
    if wait["until"]:
        message = sync.waiting_message("All agents have reached their usage limits", name,
                                       datetime.fromisoformat(wait["until"]))
    else:
        # Busy, but some agents may be out of usage rather than busy; say so.
        project_cfg = cfg["projects"].get(wait["project"])
        allowed = config.project_agents(project_cfg) if project_cfg else []
        message = sync.busy_message(choose.next_available(cfg, conn, allowed) is not None)
    message += "\n\n" + sync.waiting_agents_message(cfg, conn, wait["project"])
    try:
        tag = f"run {wait['run_id']} waiting" if wait["run_id"] else f"waiting {wait['since']}"
        sync.notice(cfg, wait["project"], wait["issue_id"], tag, message)
    except plane.PlaneError as e:
        runs.record_event(conn, None, "warning", f"Could not post the waiting comment; will retry: {e}",
                          project=wait["project"], issue_id=wait["issue_id"])
        return
    runs.mark_wait_announced(conn, wait["issue_id"])


def _wait(cfg, conn, run):
    """Leave the story In Progress until an allowed agent is available."""
    project_cfg = cfg["projects"].get(run["project"])
    first = choose.next_available(cfg, conn, config.project_agents(project_cfg)) if project_cfg else None
    allowed = config.project_agents(project_cfg) if project_cfg else []
    if choose.waiting_on_busy(cfg, conn, allowed):
        runs.start_wait(conn, project=run["project"], issue_id=run["issue_id"],
                        run_id=run["id"], agent="", until="")
        runs.record_event(conn, run["id"], "waiting", "All agents are busy; waiting in In Progress",
                          project=run["project"], issue_id=run["issue_id"])
        _announce_wait(cfg, conn, runs.wait_for(conn, run["issue_id"]))
        return
    if first is None:
        return
    agent, until = first
    runs.start_wait(conn, project=run["project"], issue_id=run["issue_id"], run_id=run["id"],
                    agent=agent, until=until.isoformat(timespec="seconds"))
    if run["id"] is not None:
        runs.record_event(conn, run["id"], "waiting",
                          f"Every allowed agent is cooling down; waiting in In Progress for {agent} "
                          f"at {until:%H:%M} UTC")
    log(f"{story_ref(project_cfg, run)}: agents cooling down; waiting in In Progress until {until.astimezone():%H:%M}, "
        f"when {cfg['agents'][agent]['display_name']} is available again")
    _announce_wait(cfg, conn, runs.wait_for(conn, run["issue_id"]))


def _hand_over(cfg, conn, run):
    """A story stopped by a usage limit goes to another available agent now, or waits for one."""
    if mainhealth.stopped(conn, run["project"], run["issue_id"]):
        return
    project_cfg = cfg["projects"].get(run["project"])
    agent = choose.agent_for(cfg, conn, project_cfg, run["issue_id"]) if project_cfg else None
    if agent is None:
        return _wait(cfg, conn, run)
    issue = {"id": run["issue_id"], "sequence_id": run["issue_key"], "name": run["issue_name"]}
    run_id = runs.enqueue(conn, project=run["project"], issue=issue, agent=agent, branch=run["branch"],
                          handed_from=run["agent"])
    if run_id:
        runs.record_event(conn, run_id, "switched", f"{run['agent']} reached its usage limit; switched to {agent}")
        log(f"{story_ref(project_cfg, run)}: {run['agent']} reached its usage limit; switched to {agent}")


def _clean_up_after(cfg, conn, run):
    """Stop what an interrupted run left behind before its story runs again.

    Either can fail without stopping startup: the next run reaps again before
    anything sandboxed starts, and cancels an unfinished merge before it sets
    up its folder, and it goes to Blocked if it still can't.
    """
    agent_cfg = cfg["agents"].get(run["agent"])
    if agent_cfg is not None:
        try:
            problem = proc.reap(cfg, agent_cfg.get("run_as"))
        except OSError as e:
            problem = str(e)
        if problem:
            runs.record_event(conn, run["id"], "warning", f"Could not stop the interrupted agent's processes: {problem}")
            warn(f"run {run['id']}: could not stop the interrupted agent's processes: {problem}")
    project_cfg = cfg["projects"].get(run["project"])
    if project_cfg is None or run["kind"] in (runs.MAIN_CHECK, "plan"):
        return  # main checks and assessments merge nothing; their folders are rebuilt every time
    try:
        worktree = gitops.registered_worktree(project_cfg["repo_path"], run["branch"])
        if worktree is not None and worktree.is_dir() and gitops.abort_pending_merge(project_cfg["repo_path"], worktree):
            runs.record_event(conn, run["id"], "merge_aborted", "Cancelled unfinished merge; restored last saved work")
    except gitops.GitError as e:
        runs.record_event(conn, run["id"], "warning", f"Could not cancel the interrupted run's unfinished merge: {e}")
        warn(f"run {run['id']}: could not cancel the interrupted run's unfinished merge: {e}")


def recover(cfg, conn):
    """Pick up the runs a restart, crash or power cut interrupted.

    A story is queued again by the usual rules, so its builder is preferred and
    it waits if no agent is free. The new run checks the story is still In
    Progress, and cancels if it was moved while DD was down. An interrupted
    refresh needs nothing: the next refresh check finds the pull request still
    lacks latest main. Nor does a main check: main stays unchecked at that
    commit, so the next cycle queues it again.
    """
    for run in runs.interrupt_running(conn):
        try:
            _recover(cfg, conn, run)
        except Exception as e:  # startup must go on; the next poll queues the story as usual
            runs.record_event(conn, run["id"], "warning", f"Could not pick the run back up: {e!r}")
            warn(f"run {run['id']}: could not pick it back up: {e!r}")


def _recover(cfg, conn, run):
    project_cfg = cfg["projects"].get(run["project"])
    ref = story_ref(project_cfg or {}, run)
    _clean_up_after(cfg, conn, run)
    if run["status"] == runs.HELD or run["kind"] == "plan":
        return
    if run["status"] == runs.AWAITING_BLOCKED:
        warn(f"{ref} was interrupted by a restart {runs.RESTART_LIMIT} times in a row; it will be moved to Blocked")
    elif run["kind"] == "refresh":
        warn(f"{ref}: refresh was interrupted by a restart; the next refresh check retries it")
    elif run["kind"] == runs.MAIN_CHECK:
        warn(f"[{run['project']}] main check was interrupted by a restart; the next check retries it")
    elif project_cfg is None:
        warn(f"run {run['id']}: project '{run['project']}' is no longer in config; not picking it back up")
    else:
        warn(f"{ref} was interrupted by a restart; picking it back up")
        _requeue(cfg, conn, run)


def _requeue(cfg, conn, run):
    if mainhealth.stopped(conn, run["project"], run["issue_id"]):
        return
    agent = choose.agent_for(cfg, conn, cfg["projects"][run["project"]], run["issue_id"])
    if agent is None:
        return _wait(cfg, conn, run)
    issue = {"id": run["issue_id"], "sequence_id": run["issue_key"], "name": run["issue_name"]}
    run_id = runs.enqueue(conn, project=run["project"], issue=issue, agent=agent, branch=run["branch"])
    if run_id:
        runs.record_event(conn, run_id, "restarted", f"DD restarted during run {run['id']}; queued again for {agent}")


def _rechoose(cfg, conn, run):
    """A run queued for an agent that has since hit its usage limit gets another.

    Returns the run to execute, or None if no agent is available: then it is
    cancelled, and the next poll queues the story again once one is.
    """
    if choose.available(cfg, conn, [run["agent"]], exclude=run["id"], held=False):
        return run
    if run["kind"] == "plan":
        runs.close(conn, run["id"], runs.CANCELLED, note="Assessor unavailable; planning retries on the next poll.")
        return None
    if run["kind"] == "refresh":
        runs.record_event(conn, run["id"], "refresh_waiting", "Builder busy or cooling down; refresh will retry later")
        runs.close(conn, run["id"], runs.CANCELLED)
        return None
    project_cfg = cfg["projects"].get(run["project"])
    agent = choose.agent_for(cfg, conn, project_cfg, run["issue_id"]) if project_cfg else None
    if agent is None:
        runs.record_event(conn, run["id"], "warning", "No agent available; will retry on a later poll")
        runs.close(conn, run["id"], runs.CANCELLED,
                   note="Every allowed agent was cooling down after its usage limit, so it was not started.")
        runs.release_slot(conn, run["id"])
        if run["handed_from"]:
            return _wait(cfg, conn, run)  # still mid-hand-over, so it gets the waiting comment
        log(f"{story_ref(project_cfg or {}, run)} not started: every allowed agent is cooling down after its usage limit")
        _waiting.setdefault(run["project"], set()).add(run["issue_id"])  # the next poll needn't say it again
        return None
    runs.record_event(conn, run["id"], "warning",
                      f"{run['agent']} is cooling down; reassigning to {agent}")
    runs.set_agent(conn, run["id"], agent)
    log(f"{story_ref(project_cfg, run)}: {run['agent']} is cooling down after its usage limit; giving it to {agent}")
    return runs.get(conn, run["id"])


class Scheduler:
    """Main-thread scheduling; workers own their connections and account slots."""
    def __init__(self, cfg):
        self.pool = ThreadPoolExecutor(max_workers=len(cfg["agents"]))
        self.pending = {}

    def reap(self, cfg, conn):
        for run_id, future in list(self.pending.items()):
            if not future.done():
                continue
            del self.pending[run_id]
            try:
                future.result()
            except Exception as e:
                self.failed(conn, run_id, e)
            run = runs.get(conn, run_id)
            if run["status"] == runs.LIMITED and run["kind"] != "plan":
                _hand_over(cfg, conn, run)

    def start(self, cfg, conn):
        self.reap(cfg, conn)
        while (run := runs.claim_next(conn)) is not None:
            try:
                if run["kind"] == runs.MAIN_CHECK:
                    # No agent runs, so a usage limit doesn't matter: the claimed slot is all it needs.
                    log(f"[{run['project']}] checking main at {run['revision'][:12]} in {run['agent']}'s account")
                    self.pending[run["id"]] = self.pool.submit(mainhealth.execute_thread, cfg, dict(run))
                    continue
                chosen = _rechoose(cfg, conn, run)
                if chosen is None:
                    runs.release_slot(conn, run["id"])
                else:
                    log(f"running {story_ref(cfg['projects'][run['project']], run)} with {chosen['agent']} on {run['branch']}")
                    self.pending[run["id"]] = self.pool.submit(worker.execute_thread, cfg, dict(chosen))
            except Exception as e:
                self.failed(conn, run["id"], e)

    def failed(self, conn, run_id, error):
        runs.record_event(conn, run_id, "warning", f"Worker stopped unexpectedly: {error!r}")
        run = runs.get(conn, run_id)
        if run["status"] == runs.RUNNING and run["kind"] == runs.MAIN_CHECK:
            runs.close(conn, run_id, runs.FAILED, note="Main could not be tested.")
        elif run["status"] == runs.RUNNING and run["kind"] == "plan":
            runs.finish_assessment(conn, run_id, error=f"Worker stopped unexpectedly: {error!r}")
        elif run["status"] == runs.RUNNING:
            runs.await_blocked(conn, run_id, f"Unexpected error in Dynamic Dispatch: {error!r}")
        runs.release_slot(conn, run_id)

    def close(self):
        self.pool.shutdown(wait=True)


def drain(cfg, conn):
    """Finish queued work for callers that explicitly request a blocking drain."""
    scheduler = Scheduler(cfg)
    try:
        while True:
            scheduler.start(cfg, conn)
            if not scheduler.pending:
                return
            for future in list(scheduler.pending.values()):
                try:
                    future.result()
                except Exception:
                    pass  # reap records the failure using the main connection
    finally:
        scheduler.close()


def checkout():
    """DD's own checkout, the folder it runs from."""
    return Path(__file__).resolve().parent


def code_version():
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=checkout(),
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def cycle(cfg, conn, version="unknown", scheduler=None, own_checkout=None):
    """One cycle. Returns True when DD has updated its own checkout and must restart."""
    runs.record_heartbeat(conn, version)
    if scheduler is not None:
        scheduler.reap(cfg, conn)
    lights.lights(cfg, conn)
    sync.pending(cfg, conn)
    cycles.poll(cfg, conn)
    # Main checks queue first: one waiting for an account takes the next free one before new work.
    revisions = {}
    mainhealth.poll(cfg, conn, revisions)
    # Reads the main check's result, so it comes after it. While pending, nothing new starts.
    update = selfupdate.pending(cfg, conn, own_checkout or checkout())
    if update is None:
        poll_once(cfg, conn)
        sprint.poll(cfg, conn)
        poll_refreshes(cfg, conn, revisions)
        if scheduler is None:
            drain(cfg, conn)
        else:
            scheduler.start(cfg, conn)
    else:
        poll_once(cfg, conn, update)
    cleanup.failed_worktrees(cfg, conn)
    cleanup.assessment_folders(cfg, conn)
    if update is None:
        return False
    return selfupdate.when_idle(cfg, conn, update, busy=scheduler is not None and bool(scheduler.pending))


# New files are not group-writable. The agent shares a group with us, and a
# login umask of 002 (Raspberry Pi OS's default) would let it write to what we
# create in the main repository, such as a worktree's admin directory and its
# ready mark. Worktree contents are made group-writable on purpose, one by one.
UMASK = 0o022


def main():
    os.umask(UMASK)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Path to config.json")
    parser.add_argument("--once", action="store_true", help="Run a single cycle and exit")
    parser.add_argument("--doctor", action="store_true", help="Check setup without changing it")
    parser.add_argument("--add-project", action="store_true", help="Set up a project for DD")
    parser.add_argument("--setup", action="store_true", help="Write config.json by answering questions")
    parser.add_argument("--plane-install-link", metavar="IDENTITY",
                        help="Print the install link for a Plane app: orchestrator, or an agent's name in config")
    parser.add_argument("--redirect-uri", default=plane_apps.DEFAULT_REDIRECT_URI,
                        help="The Plane app's Redirect URI, for --plane-install-link (default: %(default)s)")
    args = parser.parse_args()
    if args.plane_install_link:
        return plane_install.run(args.config, args.plane_install_link, args.redirect_uri)
    if args.setup:
        return setup.run(args.config)
    if args.add_project:
        return add_project.run(args.config)
    if args.doctor:
        return doctor.run(args.config)
    version = code_version()

    try:
        cfg = config.load(args.config)
        config.check_env_files(cfg)
        config.check_groups(cfg)
        cleanup.retention_days(cfg)  # a bad value stops startup, not every cycle
        redact.configure(cfg)
        selfupdate.check_setting(cfg)
        conn = runs.connect(cfg["db_path"])
    except (config.ConfigError, runs.StoreError) as e:
        print(f"Cannot start: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"Cannot start: could not read an agent env_file ({e}). "
              f"DD needs to read it so it can redact the credential.", file=sys.stderr)
        return 1

    try:
        alerts.configure(cfg)
        alerts.start_health(cfg)
        selfupdate.announce(cfg, conn, version)
        recover(cfg, conn)

        interval = config.poll_interval(cfg)
        log(f"watching {len(cfg['projects'])} project(s), polling every {interval}s")

        scheduler = Scheduler(cfg)
        healthy = selfupdate.HealthyVersion(checkout(), version)
        while True:
            try:
                restart = cycle(cfg, conn, version, scheduler) is True
                if not restart:
                    healthy.completed_cycle()
            except Exception as e:  # the loop itself must never die
                healthy.failed_cycle()
                warn(f"cycle error: {e!r}")
                restart = False
            if restart:
                scheduler.close()
                state = runs.rollback_state(checkout())
                state["planned_restart"] = time.time()
                runs.save_rollback_state(checkout(), state)
                return selfupdate.EXIT_CODE
            if args.once:
                scheduler.close()
                scheduler.reap(cfg, conn)
                drain(cfg, conn)
                return 0
            time.sleep(interval)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())

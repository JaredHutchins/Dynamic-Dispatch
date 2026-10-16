"""Testing main itself whenever it moves, and alerting when it is really broken.

Each cycle reads main's latest commit. When it differs from the last commit
checked, DD queues a main check: a run of kind main_check that takes one free
agent account's slot, because the verify command runs code from the repository
and so must run sandboxed, but never starts an agent. It runs the project's
verify command on exactly that commit, in a folder kept only for main checks.
Main checks are queued before any new work each cycle; if every account is
busy, the project's accounts are held from new work until one comes free, so
the check gets it first. Work already running is never interrupted.

Tests that fail are run once more on the same commit. Main is red only when
both runs fail; a fail-then-pass records a warning naming the tests that failed
once. Main's state lives in the store, so it survives restarts, and each change
between green and red owes one alert until it has been sent. A red result stops
new story work except its automatic fix story and holds other finished work
before combining; every hold survives a restart. Checks without a result
preserve that state and record warnings. Three in a row owe an availability
alert and slow retries to ten minutes until a result arrives.
"""

import json
import shlex
import threading

from . import alerts, choose, config, github, gitops, plane, prepare, proc, runs, sync, verify
from .redact import log, scrub, warn
from .stories import story_ref

_fix_lock = threading.RLock()
FAILURE_LIMIT = 12000

GREEN = "green"
RED = "red"

ALERT_TESTS = 5  # failing tests named in a red alert
HELD_MESSAGE = "Held: main is broken. This resumes automatically when main passes again."


def poll(cfg, conn, revisions=None):
    """Queue a main check for each project whose main has moved since it was last checked.

    revisions holds main commits already read from GitHub this cycle, by
    project; commits read here are added to it, for the refresh check to use.
    """
    revisions = {} if revisions is None else revisions
    for project, project_cfg in cfg["projects"].items():
        held = []
        try:
            announce(cfg, conn, project)
            announce_availability(cfg, conn, project)
            maintain_fix(cfg, conn, project)
            held = _queue(cfg, conn, project, project_cfg, revisions)
        except Exception as e:  # main checks must never stop the cycle, or a story
            _warning(conn, project, f"Could not queue a main check; will retry: {e}")
            without_result(cfg, conn, project, "could not queue a main check")
        finally:
            resume(cfg, conn, project)
            # An error releases the hold: stories never wait on a check that can't be queued.
            choose.hold_for_main_check(project, held)


def _warning(conn, project, detail):
    runs.record_event(conn, None, "warning", detail, project=project, issue_id=runs.main_check_id(project))
    warn(f"[{project}] {detail}")


def _queue(cfg, conn, project, project_cfg, revisions):
    """Returns the accounts to hold from new work while the check waits for one."""
    if not project_cfg.get("verify_command"):
        return []  # nothing to test main with
    if runs.is_active(conn, runs.main_check_id(project)) or not runs.main_check_due(conn, project):
        return []
    base = project_cfg.get("base_branch", "main")
    revision = revisions.get(project)
    if revision is None:
        revision = revisions[project] = github.main_revision(project_cfg["github_repo"], base,
                                                             github.token(cfg["github"]))
    health = runs.main_health(conn, project)
    failure = runs.main_check_failures(conn, project)
    if health is not None and health["revision"] == revision and not (failure and failure["count"]):
        return []
    if runs.is_active(conn, runs.main_check_id(project)):
        return []  # one at a time; once it finishes, the next cycle checks the newest commit
    agent = _free_account(cfg, conn, project_cfg)
    if agent is None:
        runs.record_event(conn, None, "main_check_waiting",
                          f"Every agent account is busy; main check of {revision[:12]} will take the next one free",
                          project=project, issue_id=runs.main_check_id(project))
        return _eligible(cfg, project_cfg)
    if runs.enqueue_main_check(conn, project=project, agent=agent, base=base, revision=revision):
        log(f"[{project}] queued a main check of {revision[:12]} in {cfg['agents'][agent]['display_name']}'s account")
    return []


def _eligible(cfg, project_cfg):
    return [name for name in config.project_agents(project_cfg) if name in cfg["agents"]]


def _free_account(cfg, conn, project_cfg):
    """An allowed agent whose account has no run. One cooling down after its usage
    limit comes first: no agent runs, and it couldn't build a story anyway. Holds
    don't apply: they are kept for main checks."""
    busy = runs.busy_agents(conn)
    free = [name for name in _eligible(cfg, project_cfg) if name not in busy]
    if not free:
        return None
    cooling = runs.cooldowns(conn, runs.now())
    return min(free, key=lambda name: name not in cooling)


def execute(cfg, conn, run):
    """Test main at the run's commit, twice if need be, and record what that says about main."""
    project = run["project"]
    project_cfg = cfg["projects"][project]
    agent_cfg = config.agent(cfg, run["agent"])
    revision = run["revision"]

    def event(kind, detail):
        runs.record_event(conn, run["id"], kind, detail)

    event("main_check_started", f"Testing main at {revision[:12]} in {agent_cfg['display_name']}'s account; "
                                "no agent runs")
    failures = []
    try:
        for _ in range(2):
            folder = gitops.main_check_folder(project_cfg["repo_path"], project_cfg["worktree_root"],
                                              run["branch"], revision,
                                              group=config.agent_group(cfg, run["agent"]))
            ok, output = _verify(cfg, project_cfg, agent_cfg, folder)
            if ok:
                break
            failures.append(output)
            event("main_tests_failed", output)
    except (gitops.GitError, NotTested) as e:
        event("warning", f"Could not test main at {revision[:12]}; the next check retries: {e}")
        without_result(cfg, conn, project, "could not prepare the folder" if isinstance(e, gitops.GitError)
                       else "preparation or sandbox failed before tests")
        return runs.close(conn, run["id"], runs.FAILED, note="Main could not be tested.")

    # A new red episode clears the fix story; a fix story write in flight must not land on it.
    with _fix_lock:
        health = runs.main_health(conn, project)
        if ok:
            if failures:
                names = "; ".join(verify.failing_tests(failures[0])) or "names unavailable"
                event("warning", f"Main's tests failed once at {revision[:12]}, then passed: {names}")
            _passed(conn, project, revision, health)
        else:
            _failed(cfg, conn, project_cfg, project, revision, health,
                    verify.failing_tests(failures[-1]), event, failures[-1])
    try:
        runs.main_check_result(conn, project)
        announce_availability(cfg, conn, project)
    except Exception as e:
        _warning(conn, project, f"Could not record main check recovery; will retry: {e}")
    state = runs.main_health(conn, project)["state"]
    runs.close(conn, run["id"], runs.DONE, note=f"Main is {state} at {revision[:12]}.")
    log(f"[{project}] main is {state} at {revision[:12]}")
    announce(cfg, conn, project)
    maintain_fix(cfg, conn, project)


class NotTested(Exception):
    """Preparation or the sandbox prevented testing, so main has no test result."""


def _verify(cfg, project_cfg, agent_cfg, folder):
    """The story verify gate's command and limits, but a sandbox that couldn't be
    cleared is not a failing test: it would turn main red for the wrong reason."""
    ok, output = prepare.run(cfg, project_cfg, agent_cfg, folder)
    if not ok:
        raise NotTested(output)
    result = proc.run_sandboxed(cfg, project_cfg["verify_command"], cwd=folder, timeout=verify.VERIFY_TIMEOUT,
                                run_as=agent_cfg.get("run_as"))
    if result.returncode is None and not result.timed_out:
        raise NotTested(result.stderr)
    if result.timed_out or result.returncode != 0:
        return False, proc.describe_failure(result, verify.VERIFY_TIMEOUT)
    return True, (result.stdout + result.stderr).strip() or "(no output)"


def _passed(conn, project, revision, health):
    if health is not None and health["state"] == GREEN:
        since, turned = health["since"], False
    else:
        since, turned = runs.now(), health is not None  # a first check that passes changes nothing
    runs.save_main_health(conn, project, revision=revision, state=GREEN, since=since, failing=[],
                          broken_by=None, alert_owed=turned or (health is not None and health["alert_owed"]))


def _failed(cfg, conn, project_cfg, project, revision, health, failing, event, output):
    output = scrub(output)[:FAILURE_LIMIT]
    if health is not None and health["state"] == RED:
        # Still red: the merge that broke it and the time stay the same.
        runs.save_main_health(conn, project, revision=revision, state=RED, since=health["since"],
                              failing=failing, broken_by=health["broken_by"], alert_owed=health["alert_owed"],
                              failure_output=output)
        return
    last_green = health["revision"] if health is not None else None
    links = []
    broken_by = _breaking_merge(cfg, project_cfg, revision, last_green, event, links)
    runs.save_main_health(conn, project, revision=revision, state=RED, since=runs.now(), failing=failing,
                          broken_by=broken_by, alert_owed=True, failure_output=output,
                          breaking_revision=revision, pr_links="\n".join(links))


def _breaking_merge(cfg, project_cfg, revision, last_green, event, links=None):
    """Pull request titles of what reached main since it was last green, for the red alert.

    Each "Merge pull request #N" title is looked up on GitHub, falling back to
    the commit message; anything failing falls back to the commit ID.
    """
    try:
        titles, numbers = gitops.main_merges(project_cfg["repo_path"], revision, last_green)
    except gitops.GitError as e:
        event("warning", f"Could not read which merge broke main: {e}")
        return f"commit {revision[:12]}"
    named = []
    for title, number in zip(titles, numbers):
        if number is not None:
            if links is not None:
                links.append(f"https://github.com/{project_cfg['github_repo']}/pull/{number}")
            try:
                title = github.pr_title(project_cfg["github_repo"], number, github.token(cfg["github"])) or title
            except Exception as e:  # the alert still goes, with the commit message
                event("warning", f"Could not look up pull request #{number}'s title; using the commit message: {e}")
        named.append(title)
    return "; ".join(named) or f"commit {revision[:12]}"


def _failure_notes(health):
    names = "\n".join(json.loads(health["failing"]))
    return (f"Main checked at: {health['revision']}\n"
            f"Merge that made main red: {health['broken_by']}\n"
            f"Breaking commit: {health['breaking_revision'] or health['revision']}\n"
            f"Pull request: {health['pr_links'] or 'unavailable'}\n\n"
            f"Failing tests:\n{names[:2000]}\n\nOutput:\n{health['failure_output'] or '(unavailable)'}")


def maintain_fix(cfg, conn, project):
    """Best-effort Plane writes, serialized with health changes and retried by polling."""
    with _fix_lock:
        health = runs.main_health(conn, project)
        if health is None:
            return
        creating = health["state"] == RED and not health["fix_issue_id"]
        try:
            project_cfg = cfg["projects"][project]
            if not creating and (not health["fix_issue_id"] or health["fix_revision"] == health["revision"]):
                return
            orch = plane.orchestrator(cfg)
            pid = project_cfg["plane_project_id"]
            if creating:
                episode = health["fix_episode"] or f"{health['revision']}-{health['since']}"
                marker = f"dd-main-fix-episode-{episode}"
                # A timed-out POST may have created the story. Recover it before retrying.
                existing = next((issue for issue in orch.issues(pid)
                                 if marker in (issue.get("description_html") or "")
                                 or marker in (issue.get("description_stripped") or "")), None)
                if existing is not None:
                    # Its description already holds this commit's failures if main hasn't moved since.
                    described = health["revision"] in (existing.get("description_html") or "")
                    runs.save_main_fix(conn, project, fix_issue_id=existing["id"],
                                       fix_revision=health["revision"] if described else None)
                    return maintain_fix(cfg, conn, project)
                command = shlex.join(project_cfg["verify_command"])
                description = (
                    "User story\nAs the approver, I want main's tests to pass again without changing what "
                    "the tests check, unless a test itself is wrong (then say so plainly), so that "
                    "the line can restart.\n\nAcceptance criteria\n"
                    f"- [ ] The full verify command passes: {command}\n"
                    "- [ ] The full verify command also passes in strict mode "
                    "(PYTHONWARNINGS=error::ResourceWarning).\n\n"
                    "In scope\n- The code needed to fix main's failing tests.\n\n"
                    "Out of scope\n- Unrelated changes or weakening correct tests.\n"
                    "- Automatic merging; the approver merges the pull request.\n\nNotes\n" + marker + "\n" + _failure_notes(health))
                issue = orch.create_issue(pid, f"Fix main: tests failing since {health['broken_by']}",
                                          description, project_cfg["states"]["in_progress"])
                runs.save_main_fix(conn, project, fix_issue_id=issue["id"], fix_revision=health["revision"])
            else:
                iid = health["fix_issue_id"]
                if health["state"] == RED:
                    text = "Main moved and is still failing.\n\n" + _failure_notes(health)
                else:
                    issue = orch.get_issue(pid, iid)
                    if not orch.issue_is_open(pid, issue):
                        runs.save_main_fix(conn, project, fix_issue_id=None, fix_revision=health["revision"])
                        return
                    text = "Main is passing again. The line has resumed. Leaving this story for the approver to close."
                marker = f"dd-main-fix-{health['state']}-{health['revision']}"
                if not orch.has_comment_with(pid, iid, marker):
                    orch.comment(pid, iid, text, marker=marker)
                runs.save_main_fix(conn, project, fix_revision=health["revision"],
                                   fix_issue_id=iid if health["state"] == RED else None)
        except Exception as e:
            action = "create" if creating else "update"
            _warning(conn, project, f"Could not {action} the fix story; will retry: {e}")
            if creating and not health["fix_alerted"]:
                try:
                    runs.save_main_fix(conn, project, fix_alerted=1)
                    alerts.send(cfg, project, None, "DD couldn't create the Fix main story; will retry.",
                                suffix=" main", level="active")
                except Exception as alert_error:
                    _warning(conn, project, f"Could not send the fix story alert: {alert_error}")


def message(health):
    if health["state"] == GREEN:
        return "Main is passing again"
    failing = json.loads(health["failing"])
    shown = ", ".join(failing[:ALERT_TESTS]) or "names unavailable"
    more = f" and {len(failing) - ALERT_TESTS} more" if len(failing) > ALERT_TESTS else ""
    return f"Main is failing tests since {health['broken_by']}\n\nFailing: {shown}{more}"


def announce(cfg, conn, project):
    """Send the alert main's latest change of state still owes, once.

    The claim is taken before delivery so two callers can't both send it. If
    Bark then fails on any device, the delivery thread owes it again for the
    next check, unless main has since changed state and owes a newer alert.
    """
    health = runs.claim_main_alert(conn, project)
    if health is None:
        return
    state = health["state"]
    try:
        alerts.main_health(cfg, project, message(health), lambda: _undelivered(cfg, project, state))
    except Exception as e:
        runs.owe_main_alert(conn, project, state)
        _warning(conn, project, f"Could not send the main alert; will retry: {e}")


def _undelivered(cfg, project, state):
    """Runs on the Bark delivery thread, so it needs its own connection."""
    conn = runs.connect(cfg["db_path"], initialize=False)
    try:
        runs.owe_main_alert(conn, project, state)
        _warning(conn, project, f"Bark did not deliver the main {state} alert; will retry")
    finally:
        conn.close()


def without_result(cfg, conn, project, reason):
    try:
        runs.main_check_without_result(conn, project, reason)
        announce_availability(cfg, conn, project)
    except Exception as e:
        _warning(conn, project, f"Could not record main check failure; will retry: {e}")


def announce_availability(cfg, conn, project):
    try:
        failure = runs.claim_main_check_alert(conn, project)
        if failure is None:
            return
        message = (f"DD can't test main: {failure['reason']}" if failure["count"] >= 3
                   else "DD can test main again")
        try:
            alerts.main_health(cfg, project, message,
                               lambda: _availability_undelivered(cfg, project, failure["generation"]))
        except Exception:
            runs.owe_main_check_alert(conn, project, failure["generation"])
            raise
    except Exception as e:
        _warning(conn, project, f"Could not send main check availability alert; will retry: {e}")


def _availability_undelivered(cfg, project, generation):
    conn = runs.connect(cfg["db_path"], initialize=False)
    try:
        runs.owe_main_check_alert(conn, project, generation)
        _warning(conn, project, "Bark did not deliver main check availability alert; will retry")
    finally:
        conn.close()


def execute_thread(cfg, run):
    """Own the worker connection and keep the account slot until the check is over."""
    conn = runs.connect(cfg["db_path"], initialize=False)
    try:
        execute(cfg, conn, run)
    except Exception as e:  # a bug must end one check, not the service
        runs.record_event(conn, run["id"], "warning", f"Unexpected error in the main check; the next check retries: {e!r}")
        warn(f"[{run['project']}] main check: unexpected error: {e!r}")
        if runs.get(conn, run["id"])["status"] == runs.RUNNING:
            runs.close(conn, run["id"], runs.FAILED, note="Main could not be tested.")
    finally:
        try:
            runs.release_slot(conn, run["id"])
        finally:
            conn.close()


def fix_story(conn, project):
    """The Plane ID of the story that fixes red main, the one story the stopped line runs, or None."""
    try:
        health = runs.main_health(conn, project)
    except Exception as e:
        _warning(conn, project, f"Could not read main's fix story; will retry: {e}")
        return None
    return health["fix_issue_id"] if health is not None and health["state"] == RED else None


def stopped(conn, project, issue_id=None):
    """Whether red main stops issue_id's work, or the project's when no story is named.

    The fix story is never stopped. An unreadable health record leaves the line
    stopped until the next poll.
    """
    try:
        return runs.main_is_red(conn, project, issue_id)
    except Exception as e:
        _warning(conn, project, f"Could not read main's state; will retry: {e}")
        return True


def save_hold(cfg, conn, run):
    """Saving or announcing a hold must never fail the story; the poll retries."""
    project_cfg = cfg["projects"][run["project"]]
    try:
        gitops.abort_pending_merge(project_cfg["repo_path"], run["worktree"])
        if gitops.has_changes(project_cfg["repo_path"], run["worktree"]):
            gitops.commit_all(project_cfg["repo_path"], run["worktree"],
                              f"Save held work: {story_ref(project_cfg, run)}",
                              config.agent(cfg, run["agent"])["git_author"])
            runs.mark_committed(conn, run["id"])
        if not run["hold_announced"]:
            sync.notice(cfg, run["project"], run["issue_id"], f"run {run['id']} held", HELD_MESSAGE)
            runs.mark_hold_announced(conn, run["id"])
        return True
    except Exception as e:
        runs.record_event(conn, run["id"], "warning", f"Could not save or announce main hold; will retry: {e}")
        return False


def hold(cfg, conn, run, worktree, result):
    if not stopped(conn, run["project"], run["issue_id"]):
        return False
    runs.hold_for_main(conn, run["id"], worktree, result)
    save_hold(cfg, conn, runs.get(conn, run["id"]))
    return True


def resume(cfg, conn, project):
    try:
        held = runs.held_for_main(conn, project)
    except Exception as e:
        _warning(conn, project, f"Could not read main holds; will retry: {e}")
        return
    for run in held:
        try:
            if run["agent"] in runs.busy_agents(conn):
                continue
            if save_hold(cfg, conn, run) and not stopped(conn, project, run["issue_id"]):
                runs.resume_main_hold(conn, run["id"])
        except Exception as e:
            runs.record_event(conn, run["id"], "warning", f"Could not resume main hold; will retry: {e}")

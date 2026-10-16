"""DD updating itself after a merge to its own code, once main's tests pass on it.

The optional self_update setting names the project whose repository is DD
itself. DD's own checkout, the folder it runs from, is separate from that
project's repository, where agents work. An update is pending when the
checkout is behind the project's main and the main check found main green at
exactly main's latest commit. Alternatively, self_update_public_repo gates
updates on GitHub checks without needing a DD project or GitHub login.

What the update changes decides what happens:
  - deploy files that deploy/FILES.md, read from the commit being updated to,
    lists as installed or doesn't list (unless Markdown): DD sends one alert with
    the steps to run deploy/dd-install and waits to be updated by hand. In a
    container they restart DD instead, like code, since the container's start
    step re-runs the installer before DD starts;
  - dispatch/ or main.py: no new run starts while runs already going finish.
    Once a whole cycle has found nothing running, so alerts the last runs
    queued have gone, DD fast-forwards the checkout and exits with EXIT_CODE,
    and systemd (Restart=on-failure) starts it on the new code;
  - anything else (documents, including *.md under deploy/, tests, and deploy
    files that run from the checkout, such as deploy/dd-install, or that only
    the container image uses): DD fast-forwards the checkout and carries on,
    without restarting. In a container, a change to an image file also sends
    one alert saying to rebuild the image.

A checkout with local changes, manually off main, or that can't be fast-forwarded is
left alone, with one alert per commit. Errors, a failed fast-forward among
them, record a warning and are retried on the next check; an update never
fails a story.
"""

import re
import shlex
import time
from pathlib import Path

from . import alerts, config, container, github, gitops, publicupdate, mainhealth, plane, runs, sync
from .redact import log, scrub, warn
from .stories import story_ref

EXIT_CODE = 75  # EX_TEMPFAIL: nonzero, so systemd's Restart=on-failure starts DD again
STORY_ID = "self-update"  # self-update events are kept under this, apart from any story
TITLE_LIMIT = 200

CODE_FOLDER, CODE_FILE, DEPLOY = "dispatch/", "main.py", "deploy/"
# deploy/FILES.md is the one list of how each deploy file reaches the machine. It is
# read from the commit being updated to, since a merge adding a deploy file lists it
# there, and the running copy's own list would be too old to know it.
FILES_PAGE = "deploy/FILES.md"
CATEGORIES = ("installed", "checkout", "image", "documentation")
INSTALLER_AGENTS = {"dd-agent": "claude", "dd-codex": "codex", "dd-gemini": "gemini"}
# deploy/CONTAINER.md's rebuild, with the dc helper that guide defines.
REBUILD = "dc up -d --build"

# The pending update, and whether a cycle has already found nothing running for
# it. In memory only: after a restart, DD just waits one more cycle.
_waiting = {"target": None, "idle": False}
# The pending update's title, looked up on GitHub once rather than on every poll.
_named = {"key": None, "title": None}


def project_name(cfg):
    """The project whose repository is DD itself, or None if DD doesn't update itself."""
    name = cfg.get("self_update")
    return name if isinstance(name, str) and name in cfg["projects"] else None


def check_setting(cfg):
    """Warn at startup about a self_update setting that can never update DD."""
    if "self_update" not in cfg:
        return
    name = project_name(cfg)
    if name is None:
        warn(f"self_update names no project in config.json ({cfg['self_update']!r}); DD won't update itself")
    elif not cfg["projects"][name].get("verify_command"):
        warn(f"self_update project '{name}' has no verify_command, so main is never checked and DD "
             "never updates itself")


def pending(cfg, conn, checkout):
    """The code update DD must restart for, or None. A documents-only update is applied on the way."""
    project = project_name(cfg)
    update = None
    if project is not None or cfg.get("self_update_public_repo"):
        try:
            if cfg.get("self_update_public_repo"):
                target = publicupdate.target(cfg["self_update_public_repo"], conn)
                if target:
                    update = _checked_update(cfg, conn, "", Path(checkout), target, "main", public=True)
            else:
                update = _check(cfg, conn, project, Path(checkout))
        except Exception as e:  # an update never fails or blocks a story
            _warning(conn, project, f"Could not check for an update to DD; will retry: {e}")
    target = update["target"] if update else None
    if target != _waiting["target"]:
        _waiting.update(target=target, idle=False)
        if update:
            log(f"DD update to {target[:12]} ({update['title']}) is pending: no new runs start, "
                "and DD restarts once running work has finished")
    return update


def _check(cfg, conn, project, checkout):
    health = runs.main_health(conn, project)
    if health is None or health["state"] != mainhealth.GREEN:
        return None
    base = cfg["projects"][project].get("base_branch", "main")
    return _checked_update(cfg, conn, project, checkout, health["revision"], base)


def _checked_update(cfg, conn, project, checkout, target, base, *, public=False):
    recovery = runs.rollback_state(checkout)
    if target == recovery.get("failed"):
        return None
    head = gitops.head_revision(checkout)
    if head == target:
        return None
    state = gitops.own_checkout(checkout, base, target)
    if state["tip"] != target or state["contains"]:
        return None  # main moved on, and its own check comes first; or the checkout is already there
    update = {"project": project, "checkout": checkout, "base": base, "head": head, "target": target,
              "title": f"commit {target[:12]}" if public else _title(cfg, conn, project, checkout, target, head),
              "rollback": recovery.get("rolled_back")}
    kinds, why = _categories(checkout, target, state["files"])
    deploy = sorted(name for name, kind in kinds.items() if kind == "installed")
    in_container = container.in_container()
    if deploy and not in_container:
        _refuse(cfg, conn, update, "deploy", _install_steps(cfg, checkout, deploy, why))
        return None
    if state["dirty"]:
        reason, problem = "local changes", (f"its checkout at {checkout} has local changes. "
                                            "Commit or discard them and DD updates on its next check.")
    elif state["branch"] != base and not (state["branch"] is None and
                                         head == recovery.get("rolled_back")):
        reason, problem = "off main", (f"its checkout at {checkout} is not on {base} (rolled back by hand?). "
                                       f"Check out {base} and DD updates on its next check.")
    elif not state["behind"]:
        reason, problem = "fast-forward", f"its checkout at {checkout} can't be fast-forwarded to it."
    else:
        reason = None
    if reason:
        _refuse(cfg, conn, update, reason, problem)
        return None
    image = sorted(name for name, kind in kinds.items() if kind == "image")
    if image and in_container:
        _rebuild(cfg, conn, update, image)
    # Installed files only get this far in a container, whose start step installs them on the restart.
    if not public and not deploy and not any(name.startswith(CODE_FOLDER) or name == CODE_FILE
                                             for name in state["files"]):
        if _fast_forward(conn, update):
            _event(conn, project, "self_update",
                   f"Updated DD's checkout to {target[:12]} ({update['title']}); "
                   "no running code changed, so DD keeps running")
            log(f"updated DD's checkout to {target[:12]}: no running code changed, so no restart")
        return None
    return update


def announce_waiting(cfg, conn, update, project, issues):
    """Explain the pending update once per story and target, using Plane's durable marker."""
    running = []
    for row in runs.agent_work(conn).values():
        if row["status"] == runs.QUEUED:
            continue
        if row["kind"] == runs.MAIN_CHECK:
            running.append(f"{row['project']} main check")
        else:
            running.append(story_ref(cfg["projects"].get(row["project"], {}), row))
    message = sync.self_update_message(update["title"], running)
    for issue in issues:
        if runs.is_active(conn, issue["id"]):
            continue
        try:
            sync.notice(cfg, project, issue["id"], f"self-update {update['target']}", message)
        except plane.PlaneError as e:
            runs.record_event(conn, None, "warning",
                              f"Could not post the DD update waiting comment; will retry: {e}",
                              project=project, issue_id=issue["id"])


def when_idle(cfg, conn, update, busy):
    """Apply a pending code update once nothing has run for a whole cycle. True if DD must now restart."""
    project, target = update["project"], update["target"]
    try:
        if busy or runs.has_running(conn):
            _waiting["idle"] = False
            _event(conn, project, "self_update_waiting",
                   f"Update to {target[:12]} waits for running work to finish; no new runs start")
            return False
        if not _waiting["idle"]:
            _waiting["idle"] = True  # one more cycle, so alerts the last runs queued are sent first
            return False
    except Exception as e:
        _warning(conn, project, f"Could not check whether DD is idle for its update; will retry: {e}")
        return False
    if not _fast_forward(conn, update):
        return False  # still pending, so the next check tries again
    try:
        runs.save_self_update_restart(conn, project, target, update["title"])
    except Exception as e:  # the checkout has moved, so DD restarts anyway; only the alert is lost
        warn(f"Could not record DD's update for its restart alert: {e}")
    _event(conn, project, "self_update", f"Updated DD's checkout to {target[:12]} ({update['title']}); restarting")
    log(f"updated DD's checkout to {target[:12]} ({update['title']}); restarting on the new code")
    return True


def announce(cfg, conn, version):
    """After restarting onto an update, send its one silent alert."""
    try:
        restart = runs.claim_self_update_restart(conn)
        if restart is None:
            return
        if restart["revision"] != version:
            warn(f"DD restarted on {version[:12]}, not its update to {restart['revision'][:12]}")
            return
        alerts.send(cfg, restart["project"] or None, None, f"DD updated to {version[:12]} ({restart['title']})",
                    suffix=" update", level="passive")
        _event(conn, restart["project"], "self_update_restarted", f"DD restarted on {version[:12]}")
    except Exception as e:
        warn(f"Could not announce DD's update: {e}")


def _fast_forward(conn, update):
    try:
        if update.get("rollback"):
            gitops.resume_own_checkout(update["checkout"], update["base"], update["target"],
                                      update["rollback"])
        else:
            gitops.fast_forward_own_checkout(update["checkout"], update["base"], update["target"])
        return True
    except gitops.GitError as e:
        # The checkout is left as it was. A lasting cause (local changes, off main) gets
        # its one alert from the next check; anything else is retried quietly.
        _warning(conn, update["project"], f"Could not fast-forward DD's checkout to {update['target'][:12]}; "
                                          f"will retry: {e}")
        return False


def _refuse(cfg, conn, update, reason, problem):
    """Leave DD on the code it has, with one alert per commit and reason."""
    project, target = update["project"], update["target"]
    if not runs.claim_self_update_notice(conn, target, reason):
        return
    title = update["title"]
    # Leave room for the install steps, which a cut-off alert would lose.
    if reason == "deploy" and len(title) > 40:
        title = title[:39] + "…"
    message = (f"DD didn't update itself to {target[:12]} ({title}): {problem}\n\n"
               f"It carries on with {update['head'][:12]}.")
    _event(conn, project, "self_update_refused", message)
    warn(message)
    alerts.send(cfg, project or None, None, message, suffix=" update", level="active")


def _rebuild(cfg, conn, update, files):
    """Say once per commit that the container's image must be rebuilt for files to take effect."""
    project, target = update["project"], update["target"]
    if not runs.claim_self_update_notice(conn, target, "image"):
        return
    title = update["title"]
    if len(title) > 40:
        title = title[:39] + "…"
    message = (f"DD is updating itself to {target[:12]} ({title}), which changes {', '.join(files)}. "
               "They take effect only when the image is rebuilt: on the host, in DD's folder, run "
               f"git pull, then {REBUILD} (the dc helper is in deploy/CONTAINER.md). "
               "DD carries on with the new checkout meanwhile.")
    _event(conn, project, "self_update_rebuild", message)
    log(message)
    alerts.send(cfg, project or None, None, message, suffix=" update", level="active")


def _title(cfg, conn, project, checkout, target, head):
    """Pull request titles of what reached main after head, for the comments and alerts."""
    if _named["key"] == (target, head):
        return _named["title"]
    try:
        titles, numbers = gitops.main_merges(checkout, target, head)
    except gitops.GitError as e:  # not kept, so the next check reads it again
        _warning(conn, project, f"Could not read what DD's update brings in: {e}")
        return f"commit {target[:12]}"
    title = "; ".join(_named_merges(cfg, conn, project, titles, numbers)) or f"commit {target[:12]}"
    title = title if len(title) <= TITLE_LIMIT else title[:TITLE_LIMIT - 1] + "…"
    _named.update(key=(target, head), title=title)
    return title


def _named_merges(cfg, conn, project, titles, numbers):
    """Each pull request by its GitHub title, else its number; a plain commit by its first line."""
    named = []
    for title, number in zip(titles, numbers):
        if number is not None:
            # Git's merge message names the branch, not the change, so it is never used.
            title = f"pull request #{number}"
            try:
                title = github.pr_title(cfg["projects"][project]["github_repo"], number,
                                        github.token(cfg["github"]))
            except Exception as e:  # the comment still goes, naming the number
                _warning(conn, project, f"Could not look up pull request #{number}'s title; "
                                        f"using its number: {scrub(str(e))}")
        named.append(title)
    return named


def deploy_files(text):
    """Each file deploy/FILES.md's table lists, with its category. A row naming no known category
    is left out, so its file counts as unlisted."""
    files = {}
    for line in text.splitlines():
        cells = [cell.strip().strip("`") for cell in line.strip().strip("|").split("|")]
        if len(cells) == 2 and cells[0].startswith(DEPLOY) and cells[1] in CATEGORIES:
            files[cells[0]] = cells[1]
    return files


def _categories(checkout, target, files):
    """Each changed deploy file's category, from deploy/FILES.md at target, and why any of them
    count as installed without being listed so. Unlisted files count as installed unless they're
    Markdown, to be safe, and so does every changed deploy file when the list can't be read."""
    changed = [name for name in files if name.startswith(DEPLOY)]
    if not changed:
        return {}, ""
    text = gitops.read_commit_file(checkout, target, FILES_PAGE)
    listed = deploy_files(text) if text is not None else {}
    kinds = {name: listed.get(name, "documentation" if name.endswith(".md") else "installed")
             for name in changed}
    unlisted = sorted(name for name in changed if name not in listed and kinds[name] == "installed")
    if not unlisted:
        return kinds, ""
    if not listed:
        return kinds, (f"DD couldn't read {FILES_PAGE} at that commit, so it treats every changed "
                       "deploy file but Markdown as installed.")
    return kinds, (f"{FILES_PAGE} at that commit doesn't list {', '.join(unlisted)}, so DD treats "
                   f"{'it' if len(unlisted) == 1 else 'them'} as installed.")


def _install_steps(cfg, checkout, files, why=""):
    """The steps to install changed deploy files with deploy/dd-install, kept short enough to fit an alert.

    why, saying why files count as installed without being listed so, comes last: if an alert
    must be cut, it loses that rather than the steps, and the event log keeps it whole."""
    problem = f"it changes {', '.join(files)}, which must be installed. "
    why = f"\n\n{why}" if why else ""
    accounts = {config.run_as(cfg, name) for name in cfg["agents"]}
    unknown = sorted(accounts - INSTALLER_AGENTS.keys())
    if unknown or not accounts:
        named = ", ".join(unknown)
        named = f" ({named[:39] + '…' if len(named) > 40 else named})" if named else ""
        return (problem + f"deploy/dd-install doesn't know every agent account{named}, so follow "
                "\"When a deploy file changes\" in deploy/README.md by hand, then restart DD." + why)
    agents = " ".join(name for account, name in INSTALLER_AGENTS.items() if account in accounts)
    # A very long checkout path could push the steps past the alert size limit.
    pull = f"cd {shlex.quote(str(checkout))} && git pull"
    if len(pull.encode("utf-8")) > 200:
        pull = "In DD's checkout, run git pull"
    return (problem + "Update it by hand:\n\n"
            f"1. {pull}\n"
            f"2. sudo python3 deploy/dd-install --agents {agents}\n"
            "3. sudo systemctl restart dynamic-dispatch" + why)


def _event(conn, project, kind, detail):
    runs.record_event(conn, None, kind, detail, project=project, issue_id=STORY_ID)


def _warning(conn, project, detail):
    _event(conn, project or "", "warning", detail)
    warn(f"[{project}] {detail}")


class HealthyVersion:
    """Only completed cycles in this process can qualify its loaded code."""
    def __init__(self, checkout, version):
        self.checkout, self.version = checkout, version
        self.since = time.monotonic()
        self.recorded = False

    def failed_cycle(self):
        self.since = time.monotonic()

    def completed_cycle(self):
        if self.recorded or time.monotonic() - self.since < 600:
            return
        if not re.fullmatch(r"[0-9a-f]{40,64}", self.version):
            return
        state = runs.rollback_state(self.checkout)
        state["last_good"] = self.version
        # A healthy fallback must not rearm rollback to itself or retry the bad code.
        if self.version != state.get("rolled_back"):
            state = {"last_good": self.version}
        runs.save_rollback_state(self.checkout, state)
        self.recorded = True

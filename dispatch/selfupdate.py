"""DD updating itself after a merge to its own code, once main's tests pass on it.

The optional self_update setting names the project whose repository is DD
itself. DD's own checkout, the folder it runs from, is separate from that
project's repository, where agents work. An update is pending when the
checkout is behind the project's main and the main check found main green at
exactly main's latest commit. Without the setting, DD never updates itself.

What the update changes decides what happens:
  - non-Markdown files under deploy/: DD never installs those, so
    it sends one alert with the commands to run and waits to be updated by hand;
  - dispatch/ or main.py: no new run starts while runs already going finish.
    Once a whole cycle has found nothing running, so alerts the last runs
    queued have gone, DD fast-forwards the checkout and exits with EXIT_CODE,
    and systemd (Restart=on-failure) starts it on the new code;
  - anything else (documents, including *.md under deploy/, and tests): DD
    fast-forwards the checkout and carries on, without restarting.

A checkout with local changes, manually off main, or that can't be fast-forwarded is
left alone, with one alert per commit. Errors, a failed fast-forward among
them, record a warning and are retried on the next check; an update never
fails a story.
"""

import os
import pwd
import re
import shlex
import time
from pathlib import Path

from . import alerts, github, gitops, mainhealth, plane, runs, sync
from .redact import log, scrub, warn
from .stories import story_ref

EXIT_CODE = 75  # EX_TEMPFAIL: nonzero, so systemd's Restart=on-failure starts DD again
STORY_ID = "self-update"  # self-update events are kept under this, apart from any story
TITLE_LIMIT = 200

CODE_FOLDER, CODE_FILE = "dispatch/", "main.py"
DEPLOY = "deploy/"

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
    if project is not None:
        try:
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
    target = health["revision"]
    recovery = runs.rollback_state(checkout)
    if target == recovery.get("failed"):
        return None
    head = gitops.head_revision(checkout)
    if head == target:
        return None
    base = cfg["projects"][project].get("base_branch", "main")
    state = gitops.own_checkout(checkout, base, target)
    if state["tip"] != target or state["contains"]:
        return None  # main moved on, and its own check comes first; or the checkout is already there
    update = {"project": project, "checkout": checkout, "base": base, "head": head, "target": target,
              "title": _title(cfg, conn, project, checkout, target, head),
              "rollback": recovery.get("rolled_back")}
    deploy = [name for name in state["files"] if name.startswith(DEPLOY) and not name.endswith(".md")]
    if deploy:
        _refuse(cfg, conn, update, "deploy",
                f"it changes {', '.join(deploy)}, which DD never installs. Update it by hand:\n\n"
                + "\n".join(_commands(checkout, deploy)))
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
    if not any(name.startswith(CODE_FOLDER) or name == CODE_FILE for name in state["files"]):
        if _fast_forward(conn, update):
            _event(conn, project, "self_update",
                   f"Updated DD's checkout to {target[:12]} ({update['title']}); "
                   "documents and tests only, so DD keeps running")
            log(f"updated DD's checkout to {target[:12]}: documents and tests only, so no restart")
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
        alerts.send(cfg, restart["project"], None, f"DD updated to {version[:12]} ({restart['title']})",
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
    message = (f"DD didn't update itself to {target[:12]} ({update['title']}): {problem}\n\n"
               f"It carries on with {update['head'][:12]}.")
    _event(conn, project, "self_update_refused", message)
    warn(message)
    alerts.send(cfg, project, None, message, suffix=" update", level="active")


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


def _commands(checkout, files):
    """deploy/README.md's commands for these deploy files, in order."""
    lines = [f"cd {shlex.quote(str(checkout))} && git pull"]
    templates = {"deploy/dynamic-dispatch.service", "deploy/sudoers-dynamic-dispatch"}
    if templates.intersection(files):
        lines += [
            f"export DD_USER={shlex.quote(pwd.getpwuid(os.geteuid()).pw_name)}",
            f"export DD_CHECKOUT={shlex.quote(str(checkout))}",
            'DD_DEPLOY_TMP="$(mktemp -d)"',
            "export DD_DEPLOY_TMP",
            """python3 - <<'RENDER'
import os
from pathlib import Path

source = Path(os.environ['DD_CHECKOUT']) / 'deploy'
target = Path(os.environ['DD_DEPLOY_TMP'])
user = os.environ['DD_USER']
checkout = os.environ['DD_CHECKOUT'].replace('%', '%%')
service = (source / 'dynamic-dispatch.service').read_text()
(target / 'dynamic-dispatch.service').write_text(
    service.replace('DD_USER', user).replace('DD_CHECKOUT', checkout))
sudoers = (source / 'sudoers-dynamic-dispatch').read_text()
(target / 'sudoers-dynamic-dispatch').write_text(sudoers.replace('DD_USER', user))
RENDER""",
        ]
    for name in files:
        source = shlex.quote(str(Path(checkout) / name))
        if name in templates:
            source = f'"$DD_DEPLOY_TMP/{Path(name).name}"'
        if name == "deploy/dd-agent-run":
            lines.append(f"sudo install -o root -g root -m 755 {source} /usr/local/bin/dd-agent-run")
        elif name == "deploy/sudoers-dynamic-dispatch":
            lines += [f"sudo visudo -cf {source} && "
                      f"sudo install -o root -g root -m 440 {source} /etc/sudoers.d/dynamic-dispatch",
                      "sudo visudo -c"]
        elif name == "deploy/dd-rollback":
            lines.append(f"sudo install -o root -g root -m 755 {source} /usr/local/bin/dd-rollback")
        elif name in ("deploy/dynamic-dispatch.service", "deploy/dynamic-dispatch-rollback.service"):
            unit = Path(name).name
            lines += [f"sudo install -o root -g root -m 644 {source} /etc/systemd/system/{unit}",
                      "sudo systemctl daemon-reload"]
        else:
            lines.append(f"{name}: see deploy/README.md")
    if templates.intersection(files):
        lines.append('rm -r -- "$DD_DEPLOY_TMP"')
    lines.append("sudo systemctl restart dynamic-dispatch")
    return lines


def _event(conn, project, kind, detail):
    runs.record_event(conn, None, kind, detail, project=project, issue_id=STORY_ID)


def _warning(conn, project, detail):
    _event(conn, project, "warning", detail)
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

"""Warning before the Git push token expires: a Human work item on the board, and Bark alerts.

About once a day each project's push token is read the way Git would push with
it, and GitHub is asked once per distinct token when it expires. Tokens stay in
memory, masked; only their fingerprints are kept, to know a token again. The
work item goes in the project's Product Backlog with its Human label, so no
agent ever picks it up.
"""

from datetime import datetime, timezone
import hashlib

from . import github, gitops, logins, plane, runs
from .redact import warn

# A working token this close to expiry gets a work item; one further off closes it.
WINDOW_DAYS = 14
ALERT_DAYS = (30, 7, 1)
REFUSED = "refused"
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
RENEWAL = (
    "To renew it:\n"
    "1. On GitHub, open Settings > Developer settings > Personal access tokens, find the token Dynamic Dispatch pushes "
    "with and regenerate it, keeping its access to the repositories (Contents read and write).\n"
    "2. On Dynamic Dispatch's host, as Dynamic Dispatch's account, run python3 main.py --save-push-login and paste the new token. "
    "In a container, run it inside the container, as deploy/CONTAINER.md \"Renew the push token\" describes.\n\n"
    "Dynamic Dispatch comments here and moves this item to Done when its next daily check finds the renewed token. "
    "It is labelled Human, so no agent picks it up."
)
MISSING = "Run --add-project for this project again to fix it"


def day(expiry):
    """The expiry's local date in the style 5 Dec 2026, whatever the locale."""
    local = expiry.astimezone()
    return f"{local.day} {MONTHS[local.month - 1]} {local.year}"


def remaining_days(expiry, at):
    return (expiry - at).total_seconds() / 86400


def expiry(token):
    """A token's expiry, None if it never expires, or REFUSED if GitHub refuses it."""
    try:
        return github.push_token_expiry(token)
    except github.GitHubError as e:
        if e.status == 401:
            return REFUSED
        raise


def title(found):
    if found == REFUSED:
        return "The GitHub push token for Dynamic Dispatch isn't working"
    return f"Renew the GitHub push token for Dynamic Dispatch (expires {day(found)})"


def description(found):
    if found == REFUSED:
        problem = ("Dynamic Dispatch's daily check found that GitHub refuses the token Git pushes with for this project: "
                   "it has expired or been revoked. Until it is renewed, Dynamic Dispatch can't push story branches.")
    else:
        problem = (f"Dynamic Dispatch's daily check found that the token Git pushes with for this project expires on "
                   f"{day(found)}. After that, Dynamic Dispatch can't push story branches.")
    return f"{problem}\n\n{RENEWAL}"


def _warning(conn, project, detail):
    warn(detail)
    runs.record_event(conn, None, "warning", detail, project=project or "")


def check(cfg, conn, alert, at=None):
    """The day's check, once per local day; alert(message) sends a Bark alert when Bark is set up."""
    at = at or datetime.now(timezone.utc)
    key = "check:" + at.astimezone().date().isoformat()
    if runs.push_token_notice_recorded(conn, key):
        return
    found, results, complete = _ask_github(conn, cfg["projects"])
    for fingerprint, result in results.items():
        _alert(conn, fingerprint, result, at, alert)
    for name, fingerprint in found.items():
        if fingerprint in results:
            try:
                _project(cfg, conn, name, fingerprint, results[fingerprint], at, alert)
            except Exception as e:
                complete = False
                _warning(conn, name, f"Could not update the push token work item; will retry: {e}")
    # Only completed checks suppress another attempt; item and alert state survives retries.
    if complete:
        runs.claim_push_token_notice(conn, key)


def _ask_github(conn, projects):
    """{project: token fingerprint} and {fingerprint: expiry, None or REFUSED}, asking once per token."""
    found, tokens = {}, {}
    complete = True
    for name, project in projects.items():
        try:
            token = gitops.push_login(project["repo_path"])
        except Exception:
            complete = False
            _warning(conn, name, "Could not read the Git push login for this project; carrying on.")
            continue
        if token:
            found[name] = logins.fingerprint(token)
            tokens[found[name]] = token
    results = {}
    for fingerprint, token in tokens.items():
        try:
            results[fingerprint] = expiry(token)
        except Exception:
            complete = False
            _warning(conn, None, "Could not ask GitHub when the Git push token expires; will retry.")
    return found, results, complete


def _alert(conn, fingerprint, found, at, alert):
    """Bark at 30, 7 and 1 days before expiry, and once for a refused token; one alert per token, not project."""
    if found == REFUSED:
        if runs.claim_push_token_notice(conn, "refused:" + fingerprint):
            alert("GitHub refuses the Git push token, so Dynamic Dispatch can't push. Regenerate it on GitHub, "
                  "then run python3 main.py --save-push-login.")
        return
    if found is None:
        return
    reached = [days for days in ALERT_DAYS if remaining_days(found, at) <= days]
    # All claimed, so a token first seen at 5 days gets one alert, not two.
    claimed = [runs.claim_push_token_notice(conn, f"alert:{fingerprint}:{found.isoformat()}:{days}")
               for days in reached]
    if any(claimed):
        alert(f"The Git push token for Dynamic Dispatch expires on {day(found)} (within {min(reached)} days). Regenerate it on "
              "GitHub, then run python3 main.py --save-push-login.")


def _project(cfg, conn, name, fingerprint, found, at, alert):
    if found == REFUSED:
        _close_items(cfg, conn, name, found, except_token=fingerprint)
        _add_item(cfg, conn, name, fingerprint, found, alert)
    elif found is not None and remaining_days(found, at) <= WINDOW_DAYS:
        _close_items(cfg, conn, name, found, except_token=fingerprint)
        _add_item(cfg, conn, name, fingerprint, found, alert)
    else:
        _close_items(cfg, conn, name, found)


def _marker(name, fingerprint, key):
    # Derived from the fingerprint one way again, so the board never shows even that.
    return "dd-push-token-" + hashlib.sha256(f"{name}\0{fingerprint}\0{key}".encode()).hexdigest()[:20]


def _add_item(cfg, conn, name, fingerprint, found, alert):
    """The project's one work item for this token and expiry, made only with its Human label."""
    key = "" if found == REFUSED else found.isoformat()
    if runs.push_token_item(conn, name, fingerprint, key) is not None:
        return
    project = cfg["projects"][name]
    backlog = (project["states"].get("product_backlog")
               or project.get("cycles", {}).get("product_backlog_state"))
    human = project.get("human_only_label_id")
    if not backlog or not human:
        # Without the Human label an agent could pick the item up, so none is made.
        detail = (f"Couldn't add \"{title(found)}\" to project {name}'s board: the Product Backlog column (states.product_backlog or cycles.product_backlog_state) or "
                  f"human_only_label_id isn't set. {MISSING}.")
        _warning(conn, name, detail)
        if runs.claim_push_token_notice(conn, f"missing:{name}:{fingerprint}:{key}"):
            alert(detail)
        return
    orch, pid = plane.orchestrator(cfg), project["plane_project_id"]
    marker = _marker(name, fingerprint, key)
    # A timed-out create may have made the item; take it up rather than make it twice.
    issue = next((issue for issue in orch.issues(pid)
                  if marker in (issue.get("description_html") or "")
                  or marker in (issue.get("description_stripped") or "")), None)
    if issue is None:
        issue = orch.create_issue(pid, title(found), f"{description(found)}\n\n{marker}", backlog, labels=[human])
    runs.save_push_token_item(conn, name, fingerprint, key, issue["id"])
    runs.record_event(conn, None, "push_token", f"Added the work item \"{title(found)}\"",
                      project=name, issue_id=issue["id"])


def _close_items(cfg, conn, name, found, except_token=None):
    """Comment on each open item that the token was renewed, and move it to Done if that state is set."""
    items = [item for item in runs.open_push_token_items(conn, name) if item["token"] != except_token]
    if not items:
        return
    project = cfg["projects"][name]
    orch, pid, done = plane.orchestrator(cfg), project["plane_project_id"], project["states"].get("done")
    if found == REFUSED:
        text = "The push token was replaced, but GitHub refuses the replacement; see its new warning item."
    else:
        text = "The push token was renewed; " + (f"it now expires {day(found)}" if found else "it no longer expires")
    for item in items:
        marker = _marker(name, item["token"], item["expiry"]) + "-renewed"
        try:
            if not orch.has_comment_with(pid, item["issue_id"], marker):
                orch.comment(pid, item["issue_id"], text, marker=marker)
            if done:
                orch.set_state(pid, item["issue_id"], done)
        except plane.PlaneError as e:
            # Someone deleted the item; there is nothing left to close.
            if e.status != 404:
                raise
        runs.close_push_token_item(conn, name, item["token"], item["expiry"])

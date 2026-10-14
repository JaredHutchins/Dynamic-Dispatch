"""Plane API client.

Every write carries an identity. DD records system events under its
Orchestrator account; work events are written under the agent's account, using
the agent's token which DD holds on its behalf. Agent processes never see a
Plane credential.
"""

from concurrent.futures import ThreadPoolExecutor
import html
import http.client
import json
import re
import threading
import time
from email.utils import parsedate_to_datetime
import urllib.error
import urllib.request

from . import logins, runs
from .redact import scrub, warn

_identity_lock = threading.Lock()
_assignments = None

RATE_LIMIT_RETRIES = 3
RATE_LIMIT_DELAY = 5

COMMENT_LIMIT = 20000  # characters

# Only a pull request address GitHub gave us, in exactly this shape, becomes a link.
_PR_URL = re.compile(r"https://github\.com/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/pull/([0-9]+)")


def review_link(pr_url):
    """The pull request's Files changed address and number, or None if it isn't a GitHub one.

    Reviews and approvals happen on that tab, so review links open there.
    """
    match = _PR_URL.fullmatch(pr_url) if pr_url and scrub(pr_url) == pr_url else None
    return (f"{pr_url}/files", match.group(1)) if match else None


def work_item_url(cfg, project_id, issue_id):
    """The work item's page in Plane's web app."""
    p = cfg["plane"]
    return f"{p['base_url'].rstrip('/')}/{p['workspace_slug']}/projects/{project_id}/issues/{issue_id}"


def api_url(plane_cfg):
    """The API origin, falling back to the existing shared web/API address."""
    return plane_cfg.get("api_url", plane_cfg["base_url"])


def _retry_delay(value):
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        try:
            return max(0, parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return RATE_LIMIT_DELAY


def _description_html(text):
    text = scrub(text)
    if len(text) > COMMENT_LIMIT:
        text = text[:COMMENT_LIMIT] + "\n[... truncated ...]"
    return f"<pre>{html.escape(text)}</pre>"


class PlaneError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def state_id(issue):
    """The issue's state ID, whether the API returned a string or a nested object."""
    state = issue.get("state")
    if isinstance(state, dict):
        return state.get("id")
    return state


class Plane:
    def __init__(self, base_url, workspace_slug, token, name="Plane login"):
        self.name = name
        self.login_key = "plane:" + logins.fingerprint(base_url + token)
        self.api = f"{base_url.rstrip('/')}/api/v1"
        self.base = f"{base_url.rstrip('/')}/api/v1/workspaces/{workspace_slug}"
        self.v2_base = f"{base_url.rstrip('/')}/api/v2/workspaces/{workspace_slug}"
        self.token = token

    def _request(self, method, path, payload=None, *, base=None):
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"X-API-Key": self.token, "User-Agent": "dynamic-dispatch/1"}
        if data:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            f"{self.base if base is None else base}{path}", data=data, headers=headers, method=method
        )
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    logins.login(self.login_key, self.name, f"Plane API {method}", False)
                    body = resp.read()
                    return json.loads(body) if body else {}
            except urllib.error.HTTPError as e:
                with e:
                    detail = e.read().decode(errors="replace")[:500]
                    delay = _retry_delay(e.headers.get("Retry-After")) if e.code == 429 else None
                if e.code == 429 and attempt < RATE_LIMIT_RETRIES:
                    time.sleep(delay)
                    continue
                if e.code in (401, 403):
                    logins.login(self.login_key, self.name, f"Plane API {method} (HTTP {e.code})", True)
                raise PlaneError(scrub(f"{method} {path} -> {e.code}: {detail}"), status=e.code) from None
            except urllib.error.URLError as e:
                raise PlaneError(scrub(f"{method} {path} -> {e.reason}")) from e

    def get(self, path):
        return self._request("GET", path)

    def patch(self, path, payload):
        return self._request("PATCH", path, payload)

    def post(self, path, payload):
        return self._request("POST", path, payload)

    def delete(self, path):
        return self._request("DELETE", path)

    def create_issue(self, project_id, name, description, state):
        """Create a story with scrubbed, escaped and bounded plain-text content."""
        return self.post(f"/projects/{project_id}/issues/", {
            "name": scrub(name)[:255], "description_html": _description_html(description), "state": state,
        })

    def set_description(self, project_id, issue_id, description):
        """Replace a work item's description with scrubbed, escaped and bounded plain text."""
        return self.patch(f"/projects/{project_id}/issues/{issue_id}/",
                          {"description_html": _description_html(description)})

    def issue_is_open(self, project_id, issue):
        state = issue.get("state")
        group = state.get("group") if isinstance(state, dict) else None
        if group is None:
            states = self._all_pages(f"/projects/{project_id}/states/")
            group = next((s.get("group") for s in states if s["id"] == state_id(issue)), None)
        if group is None:
            raise PlaneError("Could not determine whether the fix story is open")
        return group not in ("completed", "cancelled")

    def get_issue(self, project_id, issue_id):
        return self.get(f"/projects/{project_id}/issues/{issue_id}/")

    def _all_pages(self, path):
        """Every result from a list endpoint, following pagination rather than reading page one."""
        cursor = None
        while True:
            page = self.get(f"{path}?per_page=100" + (f"&cursor={cursor}" if cursor else ""))
            if isinstance(page, list):  # some endpoints aren't paginated
                yield from page
                return
            yield from page.get("results", [])
            cursor = page.get("next_cursor")
            if not page.get("next_page_results") or not cursor:
                return

    def projects(self):
        return list(self._all_pages("/projects/"))

    def add_member(self, project_id, user_id, role=20):
        path = f"/projects/{project_id}/members/"
        try:
            return self.post(path, {"member": user_id, "role": role})
        except PlaneError as e:
            # Cloud's v1 endpoint expects invitation fields instead of a member ID.
            refusal = "Expected email, display_name, workspace_slug, project_id, one or more of the fields are missing."
            roles = {20: "admin", 15: "member", 5: "guest"}
            if e.status != 400 or refusal not in str(e) or role not in roles:
                raise
        return self._request("POST", path, {"member_id": user_id, "role": roles[role]}, base=self.v2_base)

    def states(self, project_id):
        return list(self._all_pages(f"/projects/{project_id}/states/"))

    def labels(self, project_id):
        return list(self._all_pages(f"/projects/{project_id}/labels/"))

    def members(self, project_id):
        return list(self._all_pages(f"/projects/{project_id}/members/"))

    def issues(self, project_id):
        return list(self._all_pages(f"/projects/{project_id}/issues/"))

    def issues_in_state(self, project_id, wanted_state):
        # Plane 1.4.2's public API documents no state filter for this list and ignores a
        # `state` parameter; `filters` and `pql` get a 400 on the community edition. So the
        # whole list is read and filtered here, which also stays right if a later Plane
        # filters differently.
        return [i for i in self.issues(project_id) if state_id(i) == wanted_state]

    def has_comment_with(self, project_id, issue_id, marker):
        """True/False, or None if this Plane can't list comments (404 or 405).

        A 404 can also mean the issue is gone; the post that follows finds that out.
        """
        try:
            for c in self._all_pages(f"/projects/{project_id}/issues/{issue_id}/comments/"):
                if marker in (c.get("comment_html") or "") or marker in (c.get("comment_stripped") or ""):
                    return True
        except PlaneError as e:
            if e.status in (404, 405):
                return None
            raise
        return False

    def cycles(self, project_id):
        """The project's Cycles, each as its id, name, start_date and end_date.

        A draft Cycle has no dates yet, so either date may be None.
        """
        return [{"id": c["id"], "name": c.get("name"), "start_date": c.get("start_date"),
                 "end_date": c.get("end_date")}
                for c in self._all_pages(f"/projects/{project_id}/cycles/")]

    def get_cycle(self, project_id, cycle_id):
        """Read one Cycle, including its description and dates."""
        return self.get(f"/projects/{project_id}/cycles/{cycle_id}/")

    def cycle_issues(self, project_id, cycle_id):
        return list(self._all_pages(f"/projects/{project_id}/cycles/{cycle_id}/cycle-issues/"))

    def add_to_cycle(self, project_id, cycle_id, issue_id):
        return self.post(f"/projects/{project_id}/cycles/{cycle_id}/cycle-issues/", {"issues": [issue_id]})

    def remove_from_cycle(self, project_id, cycle_id, issue_id):
        """Take a work item out of a Cycle; the work item itself is kept."""
        return self.delete(f"/projects/{project_id}/cycles/{cycle_id}/cycle-issues/{issue_id}/")

    def set_cycle_description(self, project_id, cycle_id, text):
        """Replace a Cycle's description with scrubbed, bounded plain text.

        Plane stores a Cycle's description as plain text, so nothing is escaped.
        """
        text = scrub(text)
        if len(text) > COMMENT_LIMIT:
            text = text[:COMMENT_LIMIT] + "\n[... truncated ...]"
        return self.patch(f"/projects/{project_id}/cycles/{cycle_id}/", {"description": text})

    def delete_issue(self, project_id, issue_id):
        return self.delete(f"/projects/{project_id}/issues/{issue_id}/")

    def comments(self, project_id, issue_id):
        """The work item's comments, oldest first, each as its id, author, created_at and html.

        The author is the commenting user's ID. Plane names it actor, or
        created_by on some versions, and may nest it as an object.
        """
        found = []
        for c in self._all_pages(f"/projects/{project_id}/issues/{issue_id}/comments/"):
            author = c.get("actor") or c.get("created_by")
            if isinstance(author, dict):
                author = author.get("id")
            found.append({"id": c.get("id"), "author": author, "created_at": c.get("created_at"),
                          "html": c.get("comment_html") or ""})
        return sorted(found, key=lambda c: c["created_at"] or "")

    def current_user(self):
        user = self._request("GET", "/users/me/", base=self.api)
        if not isinstance(user, dict) or not isinstance(user.get("id"), str) or not user["id"]:
            raise PlaneError("Current user response has no user ID")
        return user["id"]

    def set_assignee(self, project_id, issue_id, user_id):
        return self.patch(f"/projects/{project_id}/issues/{issue_id}/", {"assignees": [user_id]})

    def set_start_date(self, project_id, issue_id, date):
        return self.patch(f"/projects/{project_id}/issues/{issue_id}/", {"start_date": date})

    def set_labels(self, project_id, issue_id, labels):
        return self.patch(f"/projects/{project_id}/issues/{issue_id}/", {"labels": labels})

    def set_state(self, project_id, issue_id, new_state):
        return self.patch(f"/projects/{project_id}/issues/{issue_id}/", {"state": new_state})

    def comment(self, project_id, issue_id, text, marker=None, pr_url=None):
        """Post a plain-text comment: scrubbed, escaped, and capped in size.

        The marker goes on the end after truncation, so it is never cut off.
        A pull request address goes first, as a link outside the truncated
        text when it is a GitHub one, otherwise as a plain line.
        """
        link = ""
        review = review_link(pr_url)
        if review:
            link = f'<p><a href="{html.escape(review[0])}">Pull request #{review[1]}</a></p>'
        elif pr_url:
            text = f"Pull request: {pr_url}\n\n{text}"
        text = scrub(text)
        if len(text) > COMMENT_LIMIT:
            text = text[:COMMENT_LIMIT] + "\n\n[... truncated ...]"
        if marker:
            text += f"\n\n{marker}"
        return self.post(
            f"/projects/{project_id}/issues/{issue_id}/comments/",
            {"comment_html": f"{link}<pre>{html.escape(text)}</pre>"},
        )


class _Assignments:
    """Serialize optional writes so a slow old run cannot overwrite its successor."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.users = {}
        self.problems = {}
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.submit(self.discover)

    def submit(self, job):
        self.executor.submit(self.work, job)

    def work(self, job):
        try:
            job()
        except Exception as e:
            # Optional display updates must not kill the queue either.
            warn(scrub(f"Plane assignment background task failed: {e}"))

    def lookup(self, name):
        try:
            self.users[name] = as_agent(self.cfg, self.cfg["agents"][name]).current_user()
        except (PlaneError, OSError, ValueError, http.client.HTTPException) as e:
            detail = scrub(f"Could not look up Plane user for {name}; skipping assignment: {e}")
            warn(detail)
            self.problems[name] = detail
            if isinstance(e, PlaneError) and e.status == 404:
                self.users[name] = None

    def discover(self):
        for name in self.cfg["agents"]:
            self.lookup(name)

    def assign(self, orch, project_id, issue_id, agent, run_id, start_date=None, labels=None):
        if agent not in self.users:
            self.lookup(agent)
        user_id = self.users.get(agent)
        problem = self.problems.pop(agent, None)
        if user_id is not None:
            try:
                orch.set_assignee(project_id, issue_id, user_id)
            except (PlaneError, OSError, ValueError, http.client.HTTPException) as e:
                problem = f"Could not assign story in Plane; carrying on: {e}"
        if not problem and not start_date and labels is None:
            return
        # SQLite connections belong to their thread, not the calling worker.
        conn = runs.connect(self.cfg["db_path"], initialize=False)
        try:
            if problem:
                runs.record_event(conn, run_id, "warning", problem)
            if labels is not None:
                try:
                    orch.patch(f"/projects/{project_id}/issues/{issue_id}/", {"labels": labels})
                except (PlaneError, OSError, ValueError, http.client.HTTPException) as e:
                    runs.record_event(conn, run_id, "warning",
                                      f"Could not set the story's Agent label in Plane; carrying on: {e}")
            if start_date:
                self.start(conn, orch, project_id, issue_id, run_id, start_date)
        finally:
            conn.close()

    def start(self, conn, orch, project_id, issue_id, run_id, date):
        # Checked again here: an earlier run's write may have succeeded after this run was queued.
        if runs.start_date_set(conn, issue_id):
            return
        # A separate request, so a refused date (say, after the Due Date) still leaves the assignee set.
        try:
            orch.set_start_date(project_id, issue_id, date)
        except (PlaneError, OSError, ValueError, http.client.HTTPException) as e:
            runs.record_event(conn, run_id, "warning",
                              f"Could not set the story's Start Date in Plane; carrying on: {e}")
            return
        runs.record_start_date(conn, issue_id, date)


def orchestrator(cfg):
    global _assignments
    # Discovery runs once per DD process, off the polling and story paths.
    with _identity_lock:
        if _assignments is None:
            _assignments = _Assignments(cfg)
    p = cfg["plane"]
    return Plane(api_url(p), p["workspace_slug"], p["orchestrator_token"], "Plane login for Orchestrator")


def as_agent(cfg, agent_cfg):
    p = cfg["plane"]
    return Plane(api_url(p), p["workspace_slug"], agent_cfg["plane_token"],
                 "Plane login for " + agent_cfg.get("display_name", "agent"))


def assign_story(cfg, orch, project_id, issue_id, agent, run_id, start_date=None,
                 agent_label_id=None, current_labels=()):
    """Queue the assignee and optional Start Date and Agent label behind earlier writes."""
    labels = None
    if agent_label_id:
        existing = [label["id"] if isinstance(label, dict) else label for label in current_labels]
        if agent_label_id not in existing:
            # Plane replaces the entire list; keep the labels from the worker's state check.
            labels = existing + [agent_label_id]
    assignments = _assignments
    assignments.submit(lambda: assignments.assign(orch, project_id, issue_id, agent, run_id, start_date, labels))

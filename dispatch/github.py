"""GitHub pull requests.

Pull requests are opened as a GitHub App when one is configured, so they come
from a bot the reviewer can formally approve. Otherwise the plain "token" is
used. Pushing never goes through here: it uses the Pi's saved Git credentials.
"""

from datetime import datetime

import base64
import json
import subprocess
import time
import threading
import urllib.error
import urllib.parse
import urllib.request

from . import logins
from .redact import add_secret, scrub

API = "https://api.github.com"
TITLE_LIMIT = 250
BODY_LIMIT = 60000
COMMENTS_PAGE = 100
APP_KEYS = ("app_id", "installation_id", "private_key_path")
# GitHub refuses an App JWT that lasts more than 10 minutes. Backdating covers
# a Pi clock running slightly ahead of GitHub's.
JWT_BACKDATE = 60
JWT_LIFETIME = 540
OPENSSL_TIMEOUT = 30
TOKEN_REFRESH_MARGIN = 300
_installation_tokens = {}
_token_logins = {}
_installation_details = {}
_token_lock = threading.Lock()


class GitHubError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def _request(method, path, token, payload=None, *, login_key=None, expiry=False):
    identity = logins.fingerprint(token)
    key = login_key or _token_logins.get(identity, "github:" + identity)
    name = "GitHub App login" if login_key else "GitHub API login"
    where = "GitHub App installation token request" if login_key else f"GitHub API {method}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"{API}{path}",
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "dynamic-dispatch",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            logins.login(key, name, where, False)
            if expiry:
                return resp.headers.get("github-authentication-token-expiration")
            body = resp.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        if e.code == 401:
            logins.login(key, name, where + " (HTTP 401)", True)
        detail = e.read().decode(errors="replace")[:500]
        raise GitHubError(scrub(f"{method} {path} -> {e.code}: {detail}"), status=e.code) from None
    except urllib.error.URLError as e:
        raise GitHubError(scrub(f"{method} {path} -> {e.reason}")) from e


def _b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _app_jwt(app_id, private_key_path):
    """An RS256 JWT for the App. The standard library can't sign RSA, so openssl does."""
    now = int(time.time())
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    claims = _b64url(json.dumps(
        {"iat": now - JWT_BACKDATE, "exp": now + JWT_LIFETIME, "iss": str(app_id)}
    ).encode())
    signing_input = f"{header}.{claims}".encode()
    try:
        result = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", str(private_key_path)],
            input=signing_input, capture_output=True, timeout=OPENSSL_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise GitHubError(f"Could not run openssl to sign the GitHub App JWT: {e}") from None
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip()[:500]
        raise GitHubError(scrub(f"openssl could not sign the GitHub App JWT: {detail}"))
    jwt = f"{header}.{claims}.{_b64url(result.stdout)}"
    add_secret(jwt)
    return jwt


def uses_app(github_cfg):
    return any(github_cfg.get(k) for k in APP_KEYS)


def token(github_cfg):
    with _token_lock:
        return _token(github_cfg)


def _token(github_cfg):
    """The token to open and look up pull requests with.

    App installation tokens are kept only in memory until five minutes before expiry.
    """
    if not uses_app(github_cfg):
        return github_cfg["token"]
    key = tuple(str(github_cfg[k]) for k in APP_KEYS)
    cached = _installation_tokens.get(key)
    if cached and time.time() < cached[1] - TOKEN_REFRESH_MARGIN:
        add_secret(cached[0])
        return cached[0]
    jwt = _app_jwt(github_cfg["app_id"], github_cfg["private_key_path"])
    installation = urllib.parse.quote(str(github_cfg["installation_id"]), safe="")
    try:
        result = _request("POST", f"/app/installations/{installation}/access_tokens", jwt,
                          login_key="github-app:" + logins.fingerprint(key))
    except GitHubError as e:
        raise GitHubError(f"Could not get a GitHub App installation token: {e}", status=e.status) from e
    installation_token = result.get("token") if isinstance(result, dict) else None
    if not installation_token:
        raise GitHubError("GitHub's reply to the App installation token request held no token")
    _installation_details[key] = {"permissions": result.get("permissions", {}),
                                  "expires_at": result.get("expires_at")}
    # Scrubbed from here on, before it can reach a log, a comment or a pull request.
    add_secret(installation_token)
    _token_logins[logins.fingerprint(installation_token)] = "github-installation:" + logins.fingerprint(key)
    try:
        expires = datetime.fromisoformat(result["expires_at"].replace("Z", "+00:00"))
        if expires.tzinfo is not None:
            _installation_tokens[key] = (installation_token, expires.timestamp())
    except (KeyError, TypeError, ValueError, AttributeError):
        pass  # Without a usable expiry, ask for a new token on the next call.
    return installation_token


def _open_pull(repo, branch, token):
    owner = repo.split("/")[0]
    head = urllib.parse.quote(f"{owner}:{branch}")
    results = _request("GET", f"/repos/{repo}/pulls?head={head}&state=open", token)
    return results[0] if results else None


def existing_pr(repo, branch, token):
    pull = _open_pull(repo, branch, token)
    return pull["html_url"] if pull else None


def _capped(text, limit=BODY_LIMIT):
    text = scrub(text)
    if len(text) > limit:
        text = text[:limit] + "\n\n[... truncated ...]"
    return text


def open_pr(repo, branch, base, title, body, token):
    """Open a PR, or find the one the branch already has.

    Returns its address and, when it already existed, GitHub's record of it.
    """
    already = _open_pull(repo, branch, token)
    if already:
        return already["html_url"], already
    title = scrub(title)[:TITLE_LIMIT]
    result = _request(
        "POST",
        f"/repos/{repo}/pulls",
        token,
        {"title": title, "head": branch, "base": base, "body": _capped(body)},
    )
    return result["html_url"], None


def _has_comment_with(repo, number, marker, token):
    page = 1
    while True:
        comments = _request("GET", f"/repos/{repo}/issues/{number}/comments"
                                   f"?per_page={COMMENTS_PAGE}&page={page}", token)
        if not isinstance(comments, list):
            raise GitHubError("GitHub's reply held no list of comments")
        if any(marker in (c.get("body") or "") for c in comments):
            return True
        if len(comments) < COMMENTS_PAGE:
            return False
        page += 1


def replace_description(repo, pull, body, heading, marker, token):
    """Keep the current description as a comment, then replace it with body.

    The comment ends with the marker, after truncation so it is never cut off,
    and isn't posted again if one carrying the marker is already there. A
    description already equal to body is left alone, so a retry changes nothing.
    """
    number = pr_number(pull["html_url"])
    old = pull.get("body") or "(no description)"
    body = _capped(body)
    if old == body:
        return
    if not _has_comment_with(repo, number, marker, token):
        comment = _capped(f"{heading}\n\n{old}") + f"\n\n{marker}"
        _request("POST", f"/repos/{repo}/issues/{number}/comments", token, {"body": comment})
    _request("PATCH", f"/repos/{repo}/pulls/{number}", token, {"body": body})


def comment_once(repo, pr_url, body, marker, token):
    """Post body as a pull request comment, unless one carrying the marker is already there.

    The marker goes after truncation, so a retry after a lost reply still finds it.
    """
    number = pr_number(pr_url)
    if not _has_comment_with(repo, number, marker, token):
        _request("POST", f"/repos/{repo}/issues/{number}/comments", token,
                 {"body": _capped(body) + f"\n\n{marker}"})


def pr_title(repo, number, token):
    result = _request("GET", f"/repos/{repo}/pulls/{number}", token)
    title = result.get("title") if isinstance(result, dict) else None
    if not isinstance(title, str) or not title.strip():
        raise GitHubError("GitHub's reply held no pull request title")
    return " ".join(scrub(title).split())[:200]


def pr_number(pr_url):
    number = pr_url.rstrip("/").rsplit("/", 1)[-1]
    if not number.isdigit():
        raise GitHubError("Recorded pull request URL has no pull request number")
    return number


def _pull(repo, pr_url, token):
    return _request("GET", f"/repos/{repo}/pulls/{pr_number(pr_url)}", token)


def pr_state(repo, pr_url, token):
    """"open", "merged" or "closed". GitHub calls a merged pull request closed too."""
    result = _pull(repo, pr_url, token)
    if result.get("merged"):
        return "merged"
    return "open" if result.get("state") == "open" else "closed"


def pr_is_open(repo, pr_url, token):
    return pr_state(repo, pr_url, token) == "open"


def pr_head(repo, pr_url, token):
    """The pull request's latest commit, or None once it is merged or closed."""
    result = _pull(repo, pr_url, token)
    if result.get("state") != "open" or result.get("merged"):
        return None
    sha = (result.get("head") or {}).get("sha")
    if not isinstance(sha, str) or not sha:
        raise GitHubError("GitHub's reply held no pull request head commit")
    return sha


REVIEWS_PAGE = 100


def _submitted_after(review, after):
    # A missing or unreadable time can't show the approval came later.
    try:
        submitted = datetime.fromisoformat(review["submitted_at"].replace("Z", "+00:00"))
    except (KeyError, AttributeError, ValueError):
        return False
    return submitted > datetime.fromisoformat(after)


def approved_commit(repo, pr_url, approver, token, *, after=None):
    """The commit the approver last approved, or None if their approval doesn't stand.

    Only their latest approving or change-requesting review decides: a comment
    leaves an approval standing, and GitHub marks a dismissed one DISMISSED.
    Given after (an ISO time), an approval submitted at or before it doesn't stand.
    """
    number = pr_number(pr_url)
    approved, page = None, 1
    while True:
        reviews = _request("GET", f"/repos/{repo}/pulls/{number}/reviews"
                                  f"?per_page={REVIEWS_PAGE}&page={page}", token)
        if not isinstance(reviews, list):
            raise GitHubError("GitHub's reply held no list of reviews")
        for review in reviews:  # oldest first
            user = (review.get("user") or {}).get("login") or ""
            if user.lower() != approver.lower():
                continue
            if review.get("state") == "APPROVED":
                approved = review.get("commit_id") or None
                if after and not _submitted_after(review, after):
                    approved = None
            elif review.get("state") in ("CHANGES_REQUESTED", "DISMISSED"):
                approved = None
        if len(reviews) < REVIEWS_PAGE:
            return approved
        page += 1


def pr_comment(repo, pr_url, body, token):
    _request("POST", f"/repos/{repo}/issues/{pr_number(pr_url)}/comments", token,
             {"body": scrub(body)[:BODY_LIMIT]})


# The light next to the merge button. GitHub keeps one per context and commit.
# Each light is a GitHub state plus its description; GitHub shows pending as yellow.
STATUS_CONTEXT = "Dynamic Dispatch"
STATUS_LIGHTS = {
    "reworking": ("pending", "Being reworked: don't merge"),
    "pending": ("pending", "Updating with latest main"),
    "success": ("success", "Up to date and tested"),
    "approved": ("success", "Up to date, tested and approved"),
    "waiting": ("pending", "Waiting for your approval"),
    "changed": ("pending", "Changed since your approval: re-review"),
    "failure": ("failure", "Blocked: see the story in Plane"),
}


def set_status(repo, sha, light, token):
    state, description = STATUS_LIGHTS[light]
    sha = urllib.parse.quote(sha, safe="")
    _request("POST", f"/repos/{repo}/statuses/{sha}", token, {
        "state": state, "context": STATUS_CONTEXT, "description": description,
    })


def main_revision(repo, base, token):
    branch = urllib.parse.quote(base, safe="")
    return _request("GET", f"/repos/{repo}/branches/{branch}", token)["commit"]["sha"]


def token_expiry(token):
    raw = _request("GET", "/rate_limit", token, expiry=True)
    expiry = datetime.fromisoformat(raw.replace("Z", "+00:00").replace(" UTC", "+00:00"))
    if expiry.tzinfo is None:
        raise ValueError("Expiry has no timezone")
    return expiry


def installation_details(github_cfg):
    """Permissions and expiry reported when GitHub issued the installation token."""
    with _token_lock:
        credential = _token(github_cfg)
        key = tuple(str(github_cfg[k]) for k in APP_KEYS)
        return credential, dict(_installation_details[key])


def repository(repo, credential):
    """Read repository access, including push permission when GitHub reports it."""
    path = "/".join(urllib.parse.quote(part, safe="") for part in repo.split("/"))
    return _request("GET", f"/repos/{path}", credential)

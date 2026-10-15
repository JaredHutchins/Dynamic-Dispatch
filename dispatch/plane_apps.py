"""Plane app (bot) logins.

A Plane identity in config is either a personal API token or a Plane app,
which DD signs in as itself with OAuth client credentials. The access token
lives only in this process's memory: it is scrubbed from output the moment it
arrives, renewed before it expires, and never written anywhere.
"""

import base64
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import logins
from .config import PLANE_BOT as KEYS
from .redact import add_secret, scrub

TOKEN_PATH = "/auth/o/token/"
INSTALL_PATH = "/auth/o/authorize-app/"
# Renewed this long before Plane says it expires, so a request never carries a token about to lapse.
TOKEN_REFRESH_MARGIN = 300
DEFAULT_REDIRECT_URI = "http://localhost/"

# What DD does as each identity, and no more. Plane refuses an install link without scopes.
ORCHESTRATOR_SCOPES = (
    "projects:read", "projects:write", "projects.members:read", "projects.members:write",
    "projects.states:read", "projects.states:write", "projects.labels:read", "projects.labels:write",
    "projects.cycles:read", "projects.cycles:write", "projects.work_items:read",
    "projects.work_items:write", "projects.work_items.comments:read",
    "projects.work_items.comments:write", "profile:read",
)
# An agent's account only comments, moves its story and looks itself up.
AGENT_SCOPES = (
    "projects.work_items:read", "projects.work_items:write",
    "projects.work_items.comments:write", "profile:read",
)

_tokens = {}
_lock = threading.Lock()


class BotLoginError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def identity(origin, bot):
    """A hashed key for one app installation on one Plane, safe to keep and compare."""
    return logins.fingerprint((origin.rstrip("/"), *(str(bot[k]) for k in KEYS)))


def token(origin, bot, *, stale=None):
    """A current access token for bot, from memory while it has time left.

    stale is a token Plane just rejected: it is dropped and a new one fetched,
    unless another request already replaced it.
    """
    key = identity(origin, bot)
    with _lock:
        cached = _tokens.get(key)
        if cached and cached[0] != stale and time.time() < cached[1] - TOKEN_REFRESH_MARGIN:
            # Again each time, since redact.configure replaces the whole list.
            add_secret(cached[0])
            return cached[0]
        _tokens.pop(key, None)
        access, expires_in = _fetch(origin, bot)
        if expires_in is not None:
            _tokens[key] = (access, time.time() + expires_in)
        return access


def _fetch(origin, bot):
    """Ask Plane for a token with the app's client credentials; returns it and its lifetime in seconds."""
    pair = f"{bot['client_id']}:{bot['client_secret']}"
    basic = base64.b64encode(pair.encode()).decode()
    # The encoded pair holds the secret as surely as the secret itself.
    add_secret(basic)
    data = urllib.parse.urlencode({"grant_type": "client_credentials",
                                   "app_installation_id": str(bot["app_installation_id"]),
                                   "scope": ""}).encode()
    req = urllib.request.Request(
        origin.rstrip("/") + TOKEN_PATH, data=data, method="POST",
        headers={"Authorization": "Basic " + basic, "Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": "dynamic-dispatch/1"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        with e:
            detail = e.read().decode(errors="replace")[:500]
        raise BotLoginError(scrub(f"POST {TOKEN_PATH} -> {e.code}: {detail}"), status=e.code) from None
    except urllib.error.URLError as e:
        raise BotLoginError(scrub(f"POST {TOKEN_PATH} -> {e.reason}")) from e
    try:
        reply = json.loads(body)
    except ValueError:
        reply = None
    access = reply.get("access_token") if isinstance(reply, dict) else None
    if not isinstance(access, str) or not access or str(reply.get("token_type", "Bearer")).lower() != "bearer":
        raise BotLoginError("Plane's reply to the app token request held no Bearer token")
    # Scrubbed from here on, before it can reach a log, a comment or the store.
    add_secret(access)
    expires_in = reply.get("expires_in")
    if isinstance(expires_in, bool) or not isinstance(expires_in, (int, float)) or expires_in <= 0:
        expires_in = None  # Without a usable lifetime, ask again on the next request.
    return access, expires_in


def install_link(origin, client_id, scopes, redirect_uri=DEFAULT_REDIRECT_URI):
    """The address that installs the app in a workspace, asking for exactly these scopes."""
    query = urllib.parse.urlencode({"client_id": client_id, "response_type": "code",
                                    "redirect_uri": redirect_uri, "scope": " ".join(scopes)},
                                   quote_via=urllib.parse.quote)
    return f"{origin.rstrip('/')}{INSTALL_PATH}?{query}"

"""Login-alert tracking that every API client and agent run reports to.

Kept apart from alerts, so the clients alerts uses can report a login without
importing alerts back. alerts.start_health registers the observer that turns
a change of state into an alert.
"""

import hashlib

from .redact import warn

# One process-wide observer covers API clients created by polling and workers.
_health = None


def fingerprint(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def login(key, name, where, rejected):
    if _health is not None:
        try:
            _health.login(key, name, where, rejected)
        except Exception:
            warn('Could not record login alert; carrying on.')


def observe(health):
    """Report every later login to health."""
    global _health
    _health = health

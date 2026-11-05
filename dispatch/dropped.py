"""Connections to GitHub and Plane that drop after they were opened.

urllib turns a connection that can't be made into URLError, but one that
breaks once open (closed without a reply, reset, a reply cut off part-way, a
read timeout) reaches the caller as a bare http.client or socket error. The
API clients catch DROPPED after HTTPError and URLError, so those keep their
own handling, and describe the drop with reason, never Python's exception text.
"""

import http.client

# URLError is itself an OSError, which is why callers catch it first.
DROPPED = (http.client.HTTPException, OSError)


def reason(service, error):
    """What went wrong, naming the service, for an error in DROPPED."""
    # RemoteDisconnected is also a ConnectionResetError, so it comes first.
    if isinstance(error, http.client.RemoteDisconnected):
        what = f"{service} closed the connection without replying"
    elif isinstance(error, TimeoutError):
        what = f"{service} stopped replying before the time limit"
    elif isinstance(error, ConnectionError):
        what = f"the connection to {service} was reset"
    elif isinstance(error, http.client.IncompleteRead):
        what = f"{service}'s reply was cut off part-way"
    elif isinstance(error, http.client.HTTPException):
        what = f"{service}'s reply could not be read"
    else:
        what = f"the connection to {service} failed"
    return f"network problem: {what}"


def error_body(error):
    """An HTTP error reply's body, or "" when the connection drops while reading it.

    The status already arrived, so the error is still handled as that status.
    """
    try:
        return error.read().decode(errors="replace")
    except DROPPED:
        return ""

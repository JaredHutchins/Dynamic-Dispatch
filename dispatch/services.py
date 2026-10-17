"""Project helper programs, such as a test database, started around one sandboxed run.

DD reaps every process of the agent's account before and after each sandboxed
call, so a helper can only live inside the call it serves. The call therefore
runs service_runner.py, which starts the helpers, waits until each is ready,
runs the real command and stops them again. Ports are chosen here, by DD, so
runs in different agent accounts at the same time never share one.
"""

import contextlib
import json
import os
from pathlib import Path
import pwd
import re
import socket
import tempfile
import threading

DEFAULT_TIMEOUT = 60
# Readable by every agent account, unlike the interpreter DD itself may run under.
PYTHON = "/usr/bin/python3"
# Room for the runner to stop helpers and remove their data after the command ends.
STOP_GRACE_SECONDS = 15
FIELDS = {"name", "command", "ready", "timeout_seconds"}

# Ports handed to runs that haven't finished, so two runs never get the same one.
PORTS = set()
LOCK = threading.Lock()


def validate(project):
    helpers = project.get("services", [])
    if not isinstance(helpers, list):
        raise ValueError("services must be a list of helper programs")
    names = set()
    for helper in helpers:
        if not isinstance(helper, dict) or set(helper) - FIELDS:
            raise ValueError("Each service needs only name, command, ready and timeout_seconds")
        name = helper.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name):
            raise ValueError("Each service name must start with a letter and use only letters, digits and underscores")
        if name.upper() in names:
            raise ValueError(f"Service name {name} is used twice, ignoring case")
        names.add(name.upper())
        _check_command(helper.get("command"), f"Service {name} command")
        if helper.get("ready") != "port":
            _check_command(helper.get("ready"), f'Service {name} ready, unless it is "port",')
        timeout = helper.get("timeout_seconds", DEFAULT_TIMEOUT)
        if type(timeout) is not int or timeout < 1:
            raise ValueError(f"Service {name} timeout_seconds must be a positive whole number")


def _check_command(command, what):
    if (not isinstance(command, list) or not command or not all(isinstance(a, str) and "\0" not in a for a in command)
            or not command[0].strip()):
        raise ValueError(f"{what} must be a non-empty argument list")


def programs(project):
    """Every program a project's helpers run: start commands and ready commands."""
    found = []
    for helper in project.get("services", []):
        found.append(helper["command"][0])
        if helper["ready"] != "port":
            found.append(helper["ready"][0])
    return found


def time_limit(project, timeout):
    """The sandbox's limit: the command's own, plus every helper's start-up and stopping."""
    helpers = project.get("services", [])
    if not helpers:
        return timeout
    return int(timeout) + sum(h.get("timeout_seconds", DEFAULT_TIMEOUT) for h in helpers) + STOP_GRACE_SECONDS


def _free_port():
    with LOCK:
        while True:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            if port not in PORTS:
                PORTS.add(port)
                return port


@contextlib.contextmanager
def around(project, command, account, timeout):
    """Yield (command, marker): the command wrapped to run between its helpers.

    marker exists afterwards if a helper failed to start, so the command never ran.
    Without helpers, the command is unchanged and marker is None. Ports stay
    reserved, and the data folder stays, until the caller has reaped the run.
    """
    helpers = project.get("services", [])
    if not helpers:
        yield command, None
        return
    ports = []
    try:
        # The runner removes the helpers' data itself, since a helper may make
        # files DD cannot delete; this only catches what a killed runner left.
        with tempfile.TemporaryDirectory(prefix="dd-services-", ignore_cleanup_errors=True) as folder:
            # Shared through the agent's group, as worktrees are.
            os.chown(folder, -1, pwd.getpwnam(account).pw_gid)
            os.chmod(folder, 0o770)
            settings = []
            for helper in helpers:
                ports.append(_free_port())
                settings.append(dict(helper, port=ports[-1], timeout_seconds=helper.get("timeout_seconds", DEFAULT_TIMEOUT)))
            source = Path(__file__).with_name("service_runner.py").read_text()
            yield ([PYTHON, "-c", source, json.dumps(settings), folder, str(int(timeout)), *command],
                   Path(folder, "startup-failed"))
    finally:
        with LOCK:
            PORTS.difference_update(ports)

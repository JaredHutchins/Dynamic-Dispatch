"""Starts a project's helpers, runs one command between them, then stops them.

Runs inside the sandbox as the agent's account, passed to the interpreter as
source text by services.around, so it imports nothing from DD.
Arguments: the helpers as JSON, the run's data folder, the command's time
limit in seconds, then the command itself.
"""

from collections import deque
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

STARTUP_FAILED = 125
TIMED_OUT = 124  # what coreutils timeout returns, so DD reports it the same way
OUTPUT_CHUNKS = 64  # of 4096 bytes: the end of a failed helper's output


def expand(command, env):
    # Only DD's own settings are filled in; no shell ever reads the arguments.
    for key, value in env.items():
        if key.startswith("DD_SERVICE_"):
            command = [arg.replace("${" + key + "}", value) for arg in command]
    return command


def drain(stream, output):
    with stream:
        for chunk in iter(lambda: stream.read(4096), b""):
            output.append(chunk)


def stop(child):
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    child.wait()


def ready(helper, env, deadline):
    if helper["ready"] == "port":
        try:
            with socket.create_connection(("127.0.0.1", helper["port"]), timeout=0.5):
                return True
        except OSError:
            return False
    child = subprocess.Popen(expand(helper["ready"], env), env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        return child.wait(timeout=max(0.001, deadline - time.monotonic())) == 0
    except subprocess.TimeoutExpired:
        return False
    finally:
        stop(child)


def start(helper, env, children):
    """Start one helper and wait until it is ready. Returns None, or what went wrong."""
    output = deque(maxlen=OUTPUT_CHUNKS)
    try:
        child = subprocess.Popen(expand(helper["command"], env), env=env, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as error:
        return f"could not start: {error}"
    reader = threading.Thread(target=drain, args=(child.stdout, output), daemon=True)
    children.append((child, reader))
    reader.start()
    deadline = time.monotonic() + helper["timeout_seconds"]
    while True:
        if child.poll() is not None:
            problem = f"exited with code {child.returncode} before it was ready"
            break
        if ready(helper, env, deadline) and child.poll() is None:
            return None
        if time.monotonic() >= deadline:
            problem = f"was not ready within {helper['timeout_seconds']}s"
            break
        time.sleep(0.1)
    stop(child)
    reader.join(timeout=5)
    text = b"".join(output).decode(errors="replace").strip()
    return f"{problem}. Its output:\n{text or '(no output)'}"


def run(helpers, folder, timeout, command):
    env = dict(os.environ)
    data = []
    for helper in helpers:
        prefix = "DD_SERVICE_" + helper["name"].upper()
        data.append(Path(folder) / helper["name"].lower())
        data[-1].mkdir()
        env[prefix + "_PORT"] = str(helper["port"])
        env[prefix + "_DATA_DIR"] = str(data[-1])
    children = []
    try:
        for helper in helpers:
            problem = start(helper, env, children)
            if problem:
                Path(folder, "startup-failed").touch()
                print(f"Helper program {helper['name']} {problem}", file=sys.stderr)
                return STARTUP_FAILED
        child = subprocess.Popen(command, env=env, start_new_session=True)
        try:
            return child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return TIMED_OUT
        finally:
            stop(child)
    finally:
        for child, reader in children:
            stop(child)
            reader.join(timeout=5)
        for path in data:
            shutil.rmtree(path, ignore_errors=True)


def interrupted(signum, frame):
    # The launcher's time limit, or DD stopping: the finally blocks still stop the helpers.
    raise SystemExit(128 + signum)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    sys.exit(run(json.loads(sys.argv[1]), sys.argv[2], int(sys.argv[3]), sys.argv[4:]))

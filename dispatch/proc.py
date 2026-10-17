"""Running subprocesses with bounded output, and running them sandboxed.

Anything an agent can influence - the agent itself, and the verify command,
which executes code the agent may have written - runs as the restricted
sandbox user through the launcher script, with a minimal environment. That
user cannot read config.json or any of DD's credentials.

Before and after every sandboxed call, every process the sandbox user owns is
killed ("reaped"), so nothing the agent left running in the background
survives into DD's next step or the next run. That relies on one
run at a time; concurrent runs will need a user or cgroup per run.
"""

import subprocess
import threading

from . import redact


DEFAULT_OUTPUT_LIMIT = 2 * 1024 * 1024  # bytes kept from each end of a stream

# The launcher enforces its own timeout as the sandbox user, because the
# DD cannot signal another user's processes. This is the backstop.
BACKSTOP_GRACE_SECONDS = 60
REAP_TIMEOUT_SECONDS = 30


class CappedBuffer:
    """Keeps the first and last `limit` bytes of a stream, drops the middle."""

    def __init__(self, limit):
        self.limit = limit
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0

    def add(self, chunk):
        self.total += len(chunk)
        room = self.limit - len(self.head)
        if room > 0:
            self.head += chunk[:room]
            chunk = chunk[room:]
        if chunk:
            self.tail += chunk
            if len(self.tail) > self.limit:
                del self.tail[: len(self.tail) - self.limit]

    @property
    def truncated(self):
        return self.total > len(self.head) + len(self.tail)

    def text(self):
        head = self.head.decode(errors="replace")
        if not self.tail:
            return head
        tail = self.tail.decode(errors="replace")
        if self.truncated:
            dropped = self.total - len(self.head) - len(self.tail)
            return f"{head}\n\n[... {dropped} bytes omitted ...]\n\n{tail}"
        return head + tail


def _drain(stream, buffer):
    for chunk in iter(lambda: stream.read(65536), b""):
        buffer.add(chunk)
    stream.close()


def _feed(stream, text):
    # Write alongside the output readers so a full pipe cannot prevent timeout.
    try:
        with stream:
            stream.write(text.encode("utf-8"))
    except BrokenPipeError:
        pass  # The process may exit before consuming all of its input.


class Result:
    def __init__(self, returncode, stdout, stderr, timed_out, stdout_truncated):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out
        self.stdout_truncated = stdout_truncated


def run_capped(command, cwd=None, timeout=None, limit=DEFAULT_OUTPUT_LIMIT, env=None, input_text=None):
    """Run a command, reading its output without holding more than a bounded amount."""
    proc = subprocess.Popen(
        command,
        cwd=str(cwd) if cwd else None,
        env=env,
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    out, err = CappedBuffer(limit), CappedBuffer(limit)
    readers = [
        threading.Thread(target=_drain, args=(proc.stdout, out), daemon=True),
        threading.Thread(target=_drain, args=(proc.stderr, err), daemon=True),
    ]
    if input_text is not None:
        readers.append(threading.Thread(target=_feed, args=(proc.stdin, input_text), daemon=True))
    for t in readers:
        t.start()

    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.terminate()  # sudo forwards SIGTERM to the sandboxed child
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    for t in readers:
        t.join(timeout=10)

    return Result(proc.returncode, out.text(), err.text(), timed_out, out.truncated)


def sandboxed(cfg, command, cwd, timeout, run_as=None, env_file=None, project_cfg=None):
    """Wrap a command so it runs as the sandbox user via the launcher."""
    sandbox = cfg["sandbox"]
    wrapped = [
        "sudo", "-n", "-u", run_as or sandbox["user"], sandbox["launcher"],
        "--cwd", str(cwd),
        "--timeout", str(int(timeout)),
    ]
    if env_file:
        wrapped += ["--env-file", env_file]
    project = project_cfg or {}
    for key, value in project.get('test_env', {}).items():
        wrapped += ['--test-env', key + '=' + value]
    if project.get('test_env_file'):
        wrapped += ['--test-env-file', project['test_env_file']]
    for directory in project.get('test_path', []):
        wrapped += ['--test-path', directory]
    return wrapped + ["--"] + list(command)


def reap(cfg, run_as=None):
    """Kill every process the sandbox user owns. Returns None, or what went wrong."""
    sandbox = cfg["sandbox"]
    result = run_capped(
        ["sudo", "-n", "-u", run_as or sandbox["user"], sandbox["launcher"], "--reap"],
        timeout=REAP_TIMEOUT_SECONDS, limit=64 * 1024,
    )
    if result.timed_out or result.returncode != 0:
        return describe_failure(result, REAP_TIMEOUT_SECONDS)
    return None


def run_sandboxed(cfg, command, cwd, timeout, run_as=None, env_file=None,
                  limit=DEFAULT_OUTPUT_LIMIT, input_text=None, project_cfg=None):
    """Run a command as the sandbox user, with no process of that user alive before or after.

    If either reap fails, the result is a failure: before, the sandbox isn't
    working; after, something may still be changing the worktree.
    """
    redact.add_test_secrets(project_cfg or {})
    problem = reap(cfg, run_as)
    if problem:
        return _refused(f"Could not clear the sandbox user's processes before starting: {problem}")
    try:
        result = run_capped(
            sandboxed(cfg, command, cwd, timeout, run_as=run_as, env_file=env_file, project_cfg=project_cfg),
            timeout=int(timeout) + BACKSTOP_GRACE_SECONDS,
            limit=limit,
            input_text=input_text,
        )
    finally:
        problem = reap(cfg, run_as)
    if problem:
        return _refused(f"Could not clear the sandbox user's processes after the run: {problem}")
    return result


def _refused(message):
    return Result(None, "", message, False, False)


# Exit codes from coreutils `timeout`, which the launcher uses.
TIMEOUT_EXIT_CODES = (124, 137)


def describe_failure(result, timeout):
    if result.returncode is None and not result.timed_out:
        return result.stderr  # never started; see _refused
    if result.timed_out or result.returncode in TIMEOUT_EXIT_CODES:
        return f"Exceeded its {int(timeout)}s time limit."
    detail = (result.stderr.strip() or result.stdout.strip())[-4000:]
    return f"Exited with code {result.returncode}.\n\n{detail or '(no output)'}"

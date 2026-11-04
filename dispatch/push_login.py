"""Saving the GitHub login Git pushes with, and proving it can push to each project.

The token is pasted hidden and goes to Git's credential store on Git's input;
it is never printed or logged. Works without config.json, so it can run before
guided setup.
"""

import re
import termios
from pathlib import Path

from . import config, gitops

# What each push check result means, as this command and doctor report it.
RESULTS = {
    gitops.PUSH_OK: "OK",
    gitops.PUSH_INVALID: "rejected: the token is invalid or expired",
    gitops.PUSH_NO_WRITE: "rejected: the token has no write access to this repository",
    gitops.PUSH_UNREACHABLE: "couldn't reach GitHub (not a token problem)",
    gitops.PUSH_NOT_DOWNLOADED: "not downloaded yet, checked after the first start",
    gitops.PUSH_FAILED: "couldn't check: Git failed for another reason; check the clone at repo_path and its origin",
}
REJECTED = (gitops.PUSH_INVALID, gitops.PUSH_NO_WRITE, gitops.PUSH_FAILED)
# GitHub usernames are letters, digits and hyphens; anything else is a typing slip.
USERNAME = re.compile(r"[A-Za-z0-9-]+")
TERMINAL = "/dev/tty"
TOKEN_PROMPT = "GitHub push token (hidden; paste it, then press Return on an empty line): "


def read_hidden(prompt):
    """Lines typed at the terminal up to an empty one, joined, with echo off throughout.

    getpass would stop at the first line, cutting a token pasted with a line
    break in it. Echo stays off for the whole paste, because changing terminal
    settings between lines can drop lines already queued.
    """
    print(prompt, end="", flush=True)
    lines = []
    with open(TERMINAL, "r") as terminal:
        original = termios.tcgetattr(terminal.fileno())
        hidden = list(original)
        hidden[3] &= ~(termios.ECHO | termios.ECHONL)
        try:
            termios.tcsetattr(terminal.fileno(), termios.TCSANOW, hidden)
            while True:
                line = terminal.readline()
                if not line:
                    raise EOFError
                if not line.strip():
                    break
                lines.append(line)
        finally:
            termios.tcsetattr(terminal.fileno(), termios.TCSANOW, original)
            print()
    return "".join(lines)


def check(repo):
    """The push check's result for one clone; Git failing to run at all counts as a failed check."""
    try:
        return gitops.check_push(repo)
    except Exception:
        return gitops.PUSH_FAILED


def _projects(path):
    """{repository: clone} for each configured project; empty with no config. Raises if config can't be read."""
    if not Path(path or config.DEFAULT_CONFIG_PATH).exists():
        return {}
    projects = config.read(path).get("projects") or {}
    if not isinstance(projects, dict):
        raise config.ConfigError("projects must be an object")
    return {project.get("github_repo") or name: project.get("repo_path", "") for name, project in projects.items()}


def _check_projects(path):
    try:
        projects = _projects(path)
    except config.ConfigError as error:
        print(f"Cannot read config.json: {error}; no project was checked.")
        return 1
    except Exception:
        print("Cannot read config.json; check that it is JSON. No project was checked.")
        return 1
    if not projects:
        print("No projects yet; the push check runs once a project is added.")
        return 0
    print("Checking each project with a push dry run (nothing is pushed):")
    results = {repository: check(repo) if repo else gitops.PUSH_FAILED for repository, repo in projects.items()}
    for repository, result in results.items():
        print(f"  {repository}: {RESULTS[result]}")
    return int(any(result in REJECTED for result in results.values()))


def _yes(answer):
    return answer.strip().lower() in ("", "y", "yes")


def run(path=None, *, ask=input, secret=read_hidden):
    try:
        username = ask("GitHub username: ").strip()
        if not USERNAME.fullmatch(username):
            print("A GitHub username is letters, digits and hyphens; nothing was saved.")
            return 1
        # A pasted token can bring spaces or line breaks with it; a token never has them.
        try:
            token = "".join(secret(TOKEN_PROMPT).split())
        except (OSError, termios.error):
            print("The token can only be pasted in a terminal, so it stays hidden; nothing was saved.")
            return 1
        if not token:
            print("No token was given; nothing was saved.")
            return 1
        try:
            gitops.save_push_login(username, token)
        except (gitops.GitError, OSError, ValueError):
            print("Couldn't save the login in Git's credential store; check that Git is installed "
                  f"and {gitops.push_credentials_path()} can be written.")
            return 1
        print(f"Saved the push login for {username} in {gitops.push_credentials_path()}, readable by this account alone.")
        for other in gitops.saved_push_logins():
            if other != username and _yes(ask(f"Remove the saved login for {other} so Git uses the new token? [Y/n] ")):
                try:
                    gitops.remove_push_login(other)
                except (gitops.GitError, OSError):
                    print(f"Couldn't remove the saved login for {other}; Git may still push with it.")
                else:
                    print(f"Removed the saved login for {other}.")
    except EOFError:
        print("\nNo answer was given; run the command again in a terminal.")
        return 1
    except KeyboardInterrupt:
        print("\nStopped; the project check didn't run.")
        return 1
    return _check_projects(path)

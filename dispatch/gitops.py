"""Git operations, run by DD.

One worktree per story branch. Rework reuses the branch; stale registrations
are pruned first, so a deleted worktree directory doesn't wedge the branch.

Worktree contents are written by the sandboxed agent, so they are untrusted.
When DD runs Git inside a worktree it therefore:
  - names the worktree's admin directory explicitly, found from the main
    repository, instead of trusting the .git file the agent could rewrite;
  - disables hooks and fsmonitor, so no agent-planted code runs as us;
  - refuses to run at all while any filter driver is configured, because the
    agent's .gitattributes decides which files a filter runs on, and status,
    add and checkout all run them;
  - never prompts, so a credential problem fails instead of hanging.
"""

import errno
import os
import re
import shutil
import stat
import tempfile
import threading
from functools import wraps
from pathlib import Path

from . import logins, proc
from .redact import scrub

GIT_TIMEOUT = 300
GIT_OUTPUT_LIMIT = 256 * 1024

SAFE_CONFIG = [
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=false",
]


class GitError(Exception):
    pass


_locks = {}
_locks_guard = threading.Lock()


def repository_lock(repo):
    key = str(Path(repo).resolve())
    with _locks_guard:
        return _locks.setdefault(key, threading.RLock())


def _serialized(function):
    @wraps(function)
    def locked(repo, *args, **kwargs):
        with repository_lock(repo):
            return function(repo, *args, **kwargs)
    return locked


def _git(args, cwd, check=True, git_dir=None, work_tree=None, extra_config=()):
    command = ["git"]
    if git_dir:
        command += [f"--git-dir={git_dir}", f"--work-tree={work_tree}"]
    command += SAFE_CONFIG + list(extra_config) + list(args)
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    result = proc.run_capped(command, cwd=cwd, timeout=GIT_TIMEOUT, limit=GIT_OUTPUT_LIMIT, env=env)
    if check and (result.timed_out or result.returncode != 0):
        detail = (result.stderr or result.stdout).strip()
        raise GitError(scrub(f"git {' '.join(args)} failed: {detail}"))
    return result


@_serialized
def _common_dir(repo):
    out = _git(["rev-parse", "--git-common-dir"], repo).stdout.strip()
    path = Path(out)
    return path if path.is_absolute() else (Path(repo) / path).resolve()


@_serialized
def admin_dir(repo, worktree):
    """The worktree's admin directory inside the main repo - which the agent cannot write."""
    wanted = Path(worktree).resolve()
    base = _common_dir(repo) / "worktrees"
    if base.is_dir():
        for entry in base.iterdir():
            try:
                pointer = Path((entry / "gitdir").read_text().strip())
            except OSError:
                continue
            if pointer.parent.resolve() == wanted:
                return entry
    raise GitError(f"{worktree} is not a registered worktree of {repo}")


FILTER_QUERY = ["config", "--get-regexp", r"^filter\."]


def _refuse_filters(result):
    """Raise unless a `git config --get-regexp ^filter.` result found nothing."""
    if result.returncode == 1 and not result.stdout.strip():
        return  # exit 1 means no matches
    if result.timed_out or result.returncode != 0:
        raise GitError(scrub(f"Could not check for filter drivers: {(result.stderr or result.stdout).strip()}"))
    keys = sorted({line.split()[0] for line in result.stdout.strip().splitlines()})
    raise GitError(
        "Git has filter drivers configured, and the agent's .gitattributes could "
        "make Git run them on its files. Nothing was done. Remove these settings "
        f"on the host first: {', '.join(keys)}"
    )


@_serialized
def _in_worktree(repo, worktree, args, extra_config=(), check=True):
    git_dir = admin_dir(repo, worktree)
    _refuse_filters(_git(FILTER_QUERY, cwd=worktree, check=False, git_dir=git_dir, work_tree=worktree))
    return _git(args, cwd=worktree, git_dir=git_dir, work_tree=worktree, extra_config=extra_config, check=check)


@_serialized
def branch_revision(repo, branch):
    return _git(["rev-parse", f"refs/heads/{branch}"], repo).stdout.strip()


@_serialized
def pushed_revision(repo, branch):
    """The locally recorded tip of the last push, or None if absent."""
    result = _git(["rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}"],
                  repo, check=False)
    return result.stdout.strip() or None


@_serialized
def first_parent_commits_since(repo, approved, head):
    """Commits after approved in order, or None if head does not build on it."""
    if approved == head:
        return []
    if _git(["merge-base", "--is-ancestor", approved, head], repo, check=False).returncode != 0:
        return None
    return _git(["rev-list", "--first-parent", "--reverse", head, "--not", approved],
                repo).stdout.split()


@_serialized
def branch_exists(repo, branch):
    result = _git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], repo, check=False)
    return result.returncode == 0


@_serialized
def registered_worktree(repo, branch):
    """Where this branch is checked out as a worktree, or None."""
    listing = _git(["worktree", "list", "--porcelain"], repo).stdout
    current = None
    for line in listing.splitlines():
        if line.startswith("worktree "):
            current = Path(line.split(" ", 1)[1])
        elif line == f"branch refs/heads/{branch}":
            return current
    return None


def grant_group_write(worktree, owned_only=False, group=None):
    """Let the sandbox user edit a fresh checkout. The worktree root's setgid bit sets the group.

    Setting a group, or owned_only after a merge, limits changes to DD-owned
    directories and files without hard links. Agent files and links to external
    files are untouched. The root is private to DD and the agent group.
    Nothing here follows a symlink: the walk doesn't descend into them, and each entry is opened relative to its
    directory's descriptor with O_NOFOLLOW, then changed through that descriptor,
    so an entry swapped for a link mid-walk is never followed.

    Symlinks are skipped, as is anything else that isn't a regular file or
    directory. Any other failure raises GitError: a worktree the agent can't
    fully write must not be marked ready, or it would never be repaired.
    """
    try:
        for _root, dirs, files, dir_fd in os.fwalk(worktree, follow_symlinks=False, onerror=_reraise):
            # fwalk lists a symlink to a directory as a directory; never descend into one.
            dirs[:] = [
                d for d in dirs
                if d != ".git" and stat.S_ISDIR(os.stat(d, dir_fd=dir_fd, follow_symlinks=False).st_mode)
            ]
            _add_group_write(dir_fd, owned_only, group, root=(_root == str(worktree)))
            for name in files:
                try:
                    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
                except OSError as e:
                    if e.errno == errno.ELOOP:
                        continue  # a symlink
                    raise
                try:
                    if stat.S_ISREG(os.fstat(fd).st_mode):
                        _add_group_write(fd, owned_only, group)
                finally:
                    os.close(fd)
    except OSError as e:
        raise GitError(scrub(f"Could not make {worktree} writable for the agent: {e}")) from e


def _reraise(err):
    raise err


def _add_group_write(fd, owned_only=False, group=None, root=False):
    info = os.fstat(fd)
    if (owned_only or group is not None) and (info.st_uid != os.geteuid() or
                       (stat.S_ISREG(info.st_mode) and info.st_nlink != 1)):
        return
    if group is not None:
        os.fchown(fd, -1, group)
    mode = 0o2770 if root and group is not None else info.st_mode | stat.S_IWGRP
    os.fchmod(fd, mode)


# Marks a worktree whose setup finished. It lives in the worktree's admin
# directory, inside the main repo, where the agent can't create or remove it.
READY = "dynamic-dispatch-ready"


@_serialized
def ensure_worktree(repo, worktree_root, branch, base_branch, group=None):
    """Return (path, reused) for this branch's worktree, creating what's needed."""
    target = Path(worktree_root) / branch

    _git(["worktree", "prune"], repo)  # forget worktrees whose directories are gone

    registered = registered_worktree(repo, branch)
    if registered is not None:
        if registered.resolve() != target.resolve() or not target.is_dir():
            raise GitError(f"Branch {branch} is already checked out at {registered}, not {target}.")
        if group is not None and target.stat().st_gid != group:
            raise GitError(f"{target} belongs to another agent group; rebuild it before reuse")
        if (admin_dir(repo, target) / READY).exists():
            # The agent has had this directory, so its contents aren't ours to walk.
            # Our own files were made group-writable when it was created.
            return target, True
        # Not marked ready means setting it up never finished (DD stopped
        # part way), so the agent never had it and it holds nothing to
        # keep. Reusing it could hand the agent an empty tree, or one it can't
        # write to. Start again.
        _git(["worktree", "remove", "--force", str(target)], repo)

    if target.exists():
        raise GitError(
            f"{target} already exists but is not a worktree for {branch}. "
            f"Remove it by hand, then move the story back to In Progress."
        )

    _git(["fetch", "origin", base_branch], repo)
    _refuse_filters(_git(FILTER_QUERY, repo, check=False))  # checkout runs smudge filters

    # Created empty, because Git can load extra config just for this branch or
    # worktree (includeIf onbranch:/gitdir:), which the check above can't see.
    # The files are checked out through _in_worktree, which checks again with
    # the config Git will really use here.
    if branch_exists(repo, branch):
        _git(["worktree", "add", "--no-checkout", str(target), branch], repo)
        reused = True
    else:
        _git(["worktree", "add", "--no-checkout", "-b", branch, str(target), f"origin/{base_branch}"], repo)
        reused = False

    try:
        grant_group_write(target, group=group)
        _in_worktree(repo, target, ["reset", "--hard", "--quiet"])
        grant_group_write(target, group=group)
        # Written last, and only then is the worktree handed to the agent.
        try:
            (admin_dir(repo, target) / READY).touch()
        except OSError as e:
            raise GitError(scrub(f"Could not mark {target} ready: {e}")) from e
    except GitError:
        # Not marked ready, so a retry would rebuild it anyway; tidy up now.
        _git(["worktree", "remove", "--force", str(target)], repo, check=False)
        raise

    return target, reused


@_serialized
def read_base_file(repo, base_branch, relpath):
    """A file's contents on the base branch, read from Git rather than any worktree."""
    result = _git(["show", f"origin/{base_branch}:{relpath}"], repo, check=False)
    return result.stdout if result.returncode == 0 else None


@_serialized
def read_commit_file(repo, revision, relpath):
    """A file's contents at exactly this commit, or None if it isn't there or can't be read."""
    result = _git(["show", f"{_commit_id(revision)}:{relpath}"], repo, check=False)
    return result.stdout if result.returncode == 0 else None


@_serialized
def has_changes(repo, worktree):
    result = _in_worktree(repo, worktree, ["status", "--porcelain", "--untracked-files=all"])
    return bool(result.stdout.strip())


# Host config can name programs that diff runs on the agent's files; never run them as DD.
DIFF_OPTIONS = ["--no-ext-diff", "--no-textconv", "--no-color", "--no-renames"]


@_serialized
def unfinished_changes(repo, worktree, limit):
    """The worktree's uncommitted changes as {"files": [(kind, path)], "diff": text}, or None.

    kind is "added", "changed" or "deleted". Read-only: untracked files are
    compared with /dev/null one at a time, because plain `git diff` leaves them
    out and adding them to the index would change what the next commit holds.
    Only regular files and symlinks are read (a named pipe could hang Git), and
    reading stops once the diff passes limit, which the caller then cuts, so
    every file is listed but not every file is read.
    """
    listing = _in_worktree(repo, worktree, ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames"])
    files, untracked = [], []
    for entry in listing.stdout.split("\0"):
        if not entry:
            continue
        code, path = entry[:2], entry[3:]
        if "D" in code:
            files.append(("deleted", path))
        elif code == "??" or "A" in code:
            files.append(("added", path))
        else:
            files.append(("changed", path))
        if code == "??":
            untracked.append(path)
    if not files:
        return None
    diff = _in_worktree(repo, worktree, ["diff", *DIFF_OPTIONS, "HEAD", "--"]).stdout
    for path in untracked:
        if len(diff.encode()) > limit:
            break
        try:
            mode = os.lstat(Path(worktree) / path).st_mode
        except OSError:
            continue
        if not (stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
            continue
        result = _in_worktree(repo, worktree, ["diff", "--no-index", *DIFF_OPTIONS, "--", "/dev/null", path], check=False)
        if result.timed_out or result.returncode not in (0, 1):
            raise GitError(scrub(f"Could not read the unfinished change to {path}: {(result.stderr or result.stdout).strip()}"))
        diff += result.stdout
    return {"files": files, "diff": diff}


@_serialized
def branch_changed_files(repo, worktree, base_branch):
    """Every path the story changes, committed or not, compared with where it last met main.

    Read-only. The comparison starts at the merge base with origin's base
    branch, so main's work merged into the branch is left out and only the
    story's own changes are listed. Untracked files count; ignored ones don't.
    """
    since = _in_worktree(repo, worktree, ["merge-base", "HEAD", f"refs/remotes/origin/{base_branch}"]).stdout.strip()
    tracked = _in_worktree(repo, worktree, ["diff", "--name-only", "-z", "--no-renames", since, "--"]).stdout
    untracked = _in_worktree(repo, worktree, ["ls-files", "--others", "--exclude-standard", "-z"]).stdout
    return sorted({path for path in (tracked + "\0" + untracked).split("\0") if path})


@_serialized
def restore_unfinished(repo, worktree, branch, leftover, group=None):
    """Put the changes saved on leftover back into branch's fresh worktree, uncommitted.

    rebuild_for_group commits them directly on top of the branch, so while the
    branch hasn't moved, leftover's tree is the branch plus those changes. Only
    the working tree is restored, without overlay, so new and changed files are
    written and deleted ones removed while the index stays at the branch's
    commit. The leftover branch is kept.
    """
    parent = _git(["rev-parse", "--verify", f"refs/heads/{leftover}^"], repo).stdout.strip()
    tip = _git(["rev-parse", "--verify", f"refs/heads/{branch}"], repo).stdout.strip()
    if parent != tip:
        raise GitError(f"{leftover} was not saved from {branch} as it is now, so its changes were not restored.")
    _in_worktree(repo, worktree, ["restore", f"--source=refs/heads/{leftover}", "--worktree", "--no-overlay", "--", "."])
    grant_group_write(worktree, owned_only=True, group=group)


@_serialized
def has_unpushed_commits(repo, branch):
    """True if the branch has commits that no ref from origin contains.

    Read from the main repository's refs, which the agent can't write. A branch
    that was pushed, or never committed to, has none.
    """
    result = _git(["rev-list", "--count", f"refs/heads/{branch}", "--not", "--remotes=origin"], repo)
    return int(result.stdout.strip() or 0) > 0


@_serialized
def unpushed_commits(repo, branch):
    """Commit IDs absent from every origin ref, read from trusted local refs."""
    return _git(["rev-list", f"refs/heads/{branch}", "--not", "--remotes=origin"], repo).stdout.split()


@_serialized
def has_commits_beyond_base(repo, branch, base_branch):
    """True if the branch has commits the base branch on origin doesn't contain.

    Read-only: compared with origin's base branch as last fetched, so work merged
    since then still counts as ahead. Read from the main repository, as above.
    """
    result = _git(
        ["rev-list", "--count", f"refs/heads/{branch}", "--not", f"refs/remotes/origin/{base_branch}"], repo
    )
    return int(result.stdout.strip() or 0) > 0


@_serialized
def commit_all(repo, worktree, message, author):
    identity = ["-c", f"user.name={author['name']}", "-c", f"user.email={author['email']}"]
    _in_worktree(repo, worktree, ["add", "-A"])
    _in_worktree(repo, worktree, ["commit", "--no-verify", "-m", message], extra_config=identity)


@_serialized
def push(repo, worktree, branch):
    """Push using the host's credential store; no token ever appears in a command."""
    # DD's repositories share the host's saved GitHub login.
    key = 'git-push:host'
    try:
        _in_worktree(repo, worktree, ["push", "--no-verify", "--set-upstream", "origin", branch])
    except GitError as e:
        if re.search(r"authentication failed|authentication failure|could not read (?:username|password)|"
                     r"permission denied.*publickey|invalid username or (?:password|token)|"
                     r"invalid credentials|http basic: access denied", str(e), re.I):
            logins.login(key, 'Git push login', 'git push to origin', True)
        raise
    else:
        logins.login(key, 'Git push login', 'git push to origin', False)


@_serialized
def check_origin(repo):
    """Reach origin with the host's saved Git login, as push does, without pushing.

    Read-only: lists origin's HEAD and changes no ref. A read proves the login
    can see the repository, not that it may write to it.
    """
    _git(["ls-remote", "origin", "HEAD"], repo)


@_serialized
def remove_worktree(repo, worktree):
    """Returns None on success, or a message describing why it couldn't be removed."""
    result = _git(["worktree", "remove", "--force", str(worktree)], repo, check=False)
    if result.returncode != 0:
        return (result.stderr or result.stdout).strip()
    return None


@_serialized
def abort_pending_merge(repo, worktree):
    """Discard an interrupted combination, keeping the last verified commit."""
    if not (admin_dir(repo, worktree) / "MERGE_HEAD").exists():
        return False
    # --hard also discards edits to cleanly merged files that can prevent --abort.
    _in_worktree(repo, worktree, ["reset", "--hard", "HEAD"])
    _in_worktree(repo, worktree, ["clean", "-fd"])
    grant_group_write(worktree, owned_only=True, group=os.stat(worktree).st_gid)
    return True


def _first_parent_titles(repo, revisions):
    """Titles and pull request numbers (None for a plain commit) of first-parent commits, oldest first."""
    history = _git(["log", "--first-parent", "--reverse", "--format=%s%n%b%x00", *revisions], repo).stdout
    titles = []
    pr_numbers = []
    for entry in history.split("\0"):
        lines = [line.strip() for line in entry.strip().splitlines() if line.strip()]
        if lines:
            merge = re.match(r"^Merge pull request #(\d+)\b", lines[0])
            pr_numbers.append(int(merge.group(1)) if merge else None)
            titles.append(lines[1] if merge and len(lines) > 1 else lines[0])
    return titles, pr_numbers


@_serialized
def combine_main(repo, worktree, branch, base, author):
    """Fetch and start an uncommitted merge; return None if already current."""
    _git(["fetch", "origin", f"+refs/heads/{base}:refs/remotes/origin/{base}"], repo)
    target = _git(["rev-parse", f"refs/remotes/origin/{base}"], repo).stdout.strip()
    ahead = _git(["rev-list", "--count", target, "--not", f"refs/heads/{branch}"], repo)
    if int(ahead.stdout.strip()) == 0:
        return None
    titles, pr_numbers = _first_parent_titles(repo, [target, "--not", f"refs/heads/{branch}"])
    # Attributes can select host-configured merge commands. Never run those as DD.
    drivers = _in_worktree(repo, worktree, ["config", "--get-regexp", r"^merge\..*\.driver$"], check=False)
    if drivers.timed_out or drivers.returncode != 1 or drivers.stdout.strip():
        raise GitError("Could not safely merge: remove configured merge drivers first.")
    result = _in_worktree(repo, worktree, ["merge", "--no-commit", "--no-ff", target], check=False,
                           extra_config=["-c", f"user.name={author['name']}",
                                         "-c", f"user.email={author['email']}"])
    conflicts = _in_worktree(repo, worktree, ["diff", "--name-only", "--diff-filter=U"]).stdout.strip()
    if result.timed_out or (result.returncode != 0 and not conflicts):
        raise GitError(scrub(f"Could not merge main: {result.stderr or result.stdout}"))
    grant_group_write(worktree, owned_only=True, group=os.stat(worktree).st_gid)
    return {"titles": titles, "pr_numbers": pr_numbers, "files": conflicts, "target": target}


def conflict_markers(worktree, conflicted_files):
    """Inspect regular files without following agent-controlled symlinks."""
    wanted = set(conflicted_files)
    found = []
    try:
        for root, dirs, files, dir_fd in os.fwalk(worktree, follow_symlinks=False, onerror=_reraise):
            dirs[:] = [d for d in dirs if d != ".git"]
            for name in files:
                if name == ".git" or str(Path(root).relative_to(worktree) / name) not in wanted:
                    continue
                try:
                    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
                except OSError as e:
                    if e.errno == errno.ELOOP:
                        continue
                    raise
                with os.fdopen(fd, "rb") as source:
                    if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                        continue
                    section = []
                    for number, line in enumerate(source, 1):
                        text = line.decode("utf-8", errors="replace").rstrip()
                        if re.match(r"^(<{7,}|>{7,})( |$)", text):
                            section.append(f"{number}: {text}")
                        elif section and not section[-1].split(": ", 1)[-1].startswith(">>>>>>>"):
                            section.append(f"{number}: {text}")
                    if section:
                        found.append(f"{Path(root).relative_to(worktree) / name}:\n" + "\n".join(section))
    except OSError as e:
        raise GitError(scrub(f"Could not inspect conflict markers: {e}")) from e
    return "\n\n".join(found)


@_serialized
def branch_contains_main(repo, branch, target):
    """Read only: compare the last uploaded branch with GitHub's current base SHA.

    A new base object may not have been fetched yet; then a refresh must fetch it.
    Remote refs deliberately exclude verified local commits not yet uploaded.
    """
    known = _git(["cat-file", "-e", f"{target}^{{commit}}"], repo, check=False)
    if known.returncode != 0:
        return False
    result = _git(["merge-base", "--is-ancestor", target,
                   f"refs/remotes/origin/{branch}"], repo, check=False)
    if result.returncode not in (0, 1):
        raise GitError(scrub(result.stderr or "Could not inspect uploaded branch"))
    return result.returncode == 0


@_serialized
def rebuild_for_group(repo, worktree, branch, leftover, author):
    """Keep dirty work locally, then remove the old agent's checkout.

    The story branch keeps its last verified commit. The snapshot branch is
    never used for upload or as evidence that the edits passed verification.
    """
    abort_pending_merge(repo, worktree)
    saved = None
    if has_changes(repo, worktree):
        _in_worktree(repo, worktree, ["checkout", "-b", leftover])
        try:
            commit_all(repo, worktree, "Save leftover work before changing agent", author)
        except GitError:
            _in_worktree(repo, worktree, ["symbolic-ref", "HEAD", f"refs/heads/{branch}"])
            raise
        saved = leftover
    try:
        if saved:
            _in_worktree(repo, worktree, ["checkout", branch])
        problem = remove_worktree(repo, worktree)
        if problem:
            raise GitError(problem)
    except GitError as e:
        if saved:
            raise GitError(f"Leftover work is saved locally on {saved}, not pushed. Could not finish rebuilding: {e}") from e
        raise
    return saved


# The folder main checks use, inside the project's worktree root. Story
# branches are named <prefix>-<number>, so no story's folder can take it.
MAIN_CHECK_FOLDER = "main-check"


def _commit_id(revision):
    """A full commit ID, checked: it comes from GitHub and is passed to Git as an argument."""
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise GitError(f"Not a commit ID: {revision!r}")
    return revision


@_serialized
def main_check_folder(repo, worktree_root, base_branch, revision, group=None):
    """A fresh, detached checkout of exactly this commit of main, for a main check.

    Rebuilt for every test run: tests ran sandboxed in the last one, so nothing
    left in it is trusted, and a second run must not see what the first left.
    It is checked out the same guarded way as a story's folder.
    """
    revision = _commit_id(revision)
    target = Path(worktree_root) / MAIN_CHECK_FOLDER
    _git(["worktree", "prune"], repo)
    listing = _git(["worktree", "list", "--porcelain"], repo).stdout
    if any(line.startswith("worktree ") and Path(line.split(" ", 1)[1]).resolve() == target.resolve()
           for line in listing.splitlines()):
        _git(["worktree", "remove", "--force", str(target)], repo)
    if target.exists():
        raise GitError(f"{target} already exists but is not DD's main check folder. Remove it by hand.")

    _git(["fetch", "origin", f"+refs/heads/{base_branch}:refs/remotes/origin/{base_branch}"], repo)
    _git(["cat-file", "-e", f"{revision}^{{commit}}"], repo)
    _refuse_filters(_git(FILTER_QUERY, repo, check=False))  # checkout runs smudge filters
    _git(["worktree", "add", "--no-checkout", "--detach", str(target), revision], repo)
    try:
        grant_group_write(target, group=group)
        _in_worktree(repo, target, ["reset", "--hard", "--quiet"])
        grant_group_write(target, group=group)
    except GitError:
        _git(["worktree", "remove", "--force", str(target)], repo, check=False)
        raise
    return target


@_serialized
def main_merges(repo, revision, since=None):
    """Titles and pull request numbers of what reached main after since, up to revision.

    Without since, or when since is unknown here, only revision's own commit.
    Pull request numbers are None for plain commits; titles are looked up elsewhere.
    """
    revision = _commit_id(revision)
    if since is not None and _git(["cat-file", "-e", f"{_commit_id(since)}^{{commit}}"],
                                  repo, check=False).returncode == 0:
        titles, numbers = _first_parent_titles(repo, [revision, "--not", since])
        if titles:
            return titles, numbers
    return _first_parent_titles(repo, ["-1", revision])


# DD's own checkout, the folder it runs from. Agents never write to it, but
# the same guards apply: no hooks, no filters, never a prompt.

@_serialized
def head_revision(repo):
    """The commit a checkout has out, read without fetching."""
    return _git(["rev-parse", "HEAD"], repo).stdout.strip()


@_serialized
def own_checkout(checkout, base_branch, target):
    """Where DD's own checkout stands against target, after fetching base_branch.

    Returns {"tip": base_branch's latest commit on origin}, and while that is
    target also "branch" (None when detached), "dirty" (tracked files changed),
    "behind" (HEAD is an ancestor of target), "contains" (target is an
    ancestor of HEAD) and "files" (paths that differ between HEAD and target).
    Read-only apart from the fetch.
    """
    target = _commit_id(target)
    _git(["fetch", "origin", f"+refs/heads/{base_branch}:refs/remotes/origin/{base_branch}"], checkout)
    tip = _git(["rev-parse", f"refs/remotes/origin/{base_branch}"], checkout).stdout.strip()
    if tip != target:
        return {"tip": tip}
    _refuse_filters(_git(FILTER_QUERY, checkout, check=False))  # status runs clean filters
    branch = _git(["symbolic-ref", "--quiet", "--short", "HEAD"], checkout, check=False).stdout.strip()
    dirty = _git(["status", "--porcelain", "--untracked-files=no"], checkout).stdout.strip()
    files = _git(["diff", "--name-only", *DIFF_OPTIONS, "HEAD", target, "--"], checkout).stdout.split("\n")
    return {
        "tip": tip,
        "branch": branch or None,
        "dirty": bool(dirty),
        "behind": _git(["merge-base", "--is-ancestor", "HEAD", target], checkout, check=False).returncode == 0,
        "contains": _git(["merge-base", "--is-ancestor", target, "HEAD"], checkout, check=False).returncode == 0,
        "files": sorted(name for name in files if name),
    }


@_serialized
def fast_forward_own_checkout(checkout, base_branch, target):
    """Move DD's own checkout forward to target on base_branch: a fast-forward only.

    Never a merge or a reset. Refuses a checkout off base_branch (rolled back
    by hand, say) or with changed tracked files, and leaves it as it was.
    """
    target = _commit_id(target)
    branch = _git(["symbolic-ref", "--quiet", "--short", "HEAD"], checkout, check=False).stdout.strip()
    if branch != base_branch:
        raise GitError(f"{checkout} is not on {base_branch}, so it was not updated.")
    _refuse_filters(_git(FILTER_QUERY, checkout, check=False))  # checkout runs smudge filters
    if _git(["status", "--porcelain", "--untracked-files=no"], checkout).stdout.strip():
        raise GitError(f"{checkout} has local changes, so it was not updated.")
    _git(["merge", "--ff-only", "--quiet", target], checkout)
    if _git(["rev-parse", "HEAD"], checkout).stdout.strip() != target:
        raise GitError(f"{checkout} did not reach {target[:12]} after fast-forwarding.")


@_serialized
def resume_own_checkout(checkout, base_branch, target, rollback):
    """Return an automatically detached fallback to main, without rewriting main."""
    target, rollback = _commit_id(target), _commit_id(rollback)
    branch = _git(["symbolic-ref", "--quiet", "--short", "HEAD"], checkout, check=False).stdout.strip()
    if branch == base_branch:
        return fast_forward_own_checkout(checkout, base_branch, target)
    if branch or head_revision(checkout) != rollback:
        raise GitError("DD's fallback checkout has moved; leaving it alone.")
    _refuse_filters(_git(FILTER_QUERY, checkout, check=False))
    if _git(["status", "--porcelain", "--untracked-files=no"], checkout).stdout.strip():
        raise GitError("DD's fallback checkout has local changes.")
    for revision in (rollback, f"refs/heads/{base_branch}"):
        if _git(["merge-base", "--is-ancestor", revision, target], checkout, check=False).returncode:
            raise GitError("DD's main cannot be fast-forwarded to the update.")
    # Advance the detached checkout first: switching to main never runs the known bad code.
    _git(["merge", "--ff-only", "--quiet", target], checkout)
    _git(["branch", "-f", base_branch, target], checkout)
    _git(["checkout", "--quiet", base_branch], checkout)


def check_repository_access(url):
    """Check the host login without changing remote data or prompting.

    Returns whether the repository has a commit: an empty one has no base branch to work from.
    """
    return bool(_git(["ls-remote", "--", url, "HEAD"], cwd=None).stdout.strip())


def _share_read_only(clone, gid):
    """Give a fresh clone the projects folder's group, which agents are in, without group write.

    The clone is still in DD's private scratch folder, so nothing else can swap entries mid-walk.
    """
    for root, _dirs, files in os.walk(clone):
        for path in [root, *(os.path.join(root, name) for name in files)]:
            if os.path.islink(path):
                continue
            os.chown(path, -1, gid)
            os.chmod(path, stat.S_IMODE(os.stat(path).st_mode) & ~(stat.S_IWGRP | stat.S_IWOTH))


@_serialized
def clone_project(repo, url):
    """Reuse only the matching clone; publish a completed clone atomically."""
    repo = Path(repo)
    if repo.exists() or repo.is_symlink():
        if repo.is_symlink() or not (repo / ".git").is_dir():
            raise GitError("Project code path is not a normal clone")
        origin = _git(["remote", "get-url", "origin"], repo).stdout.strip()
        if origin != url:
            raise GitError("Existing clone has a different origin")
        check_origin(repo)
        return
    repo.parent.mkdir(parents=True, exist_ok=True)
    # A failed clone is private scratch space, never mistaken for a finished clone.
    temporary = Path(tempfile.mkdtemp(prefix=".dd-clone-", dir=repo.parent))
    try:
        _refuse_filters(_git(FILTER_QUERY, cwd=repo.parent, check=False))
        _git(["clone", "--", url, str(temporary / "repo")], repo.parent)
        try:
            _share_read_only(temporary / "repo", repo.parent.stat().st_gid)
        except OSError as e:
            raise GitError(scrub(f"Could not share the clone read-only with its folder's group: {e}")) from e
        os.rename(temporary / "repo", repo)
    finally:
        shutil.rmtree(temporary)

"""Removing the worktrees of failed runs once they have been kept long enough,
and the empty assessment folders a restart left behind.

A failed run's worktree is kept so it can be inspected (a successful one is
removed when its story reaches In Review). If failed_worktree_days is set, a
failed run's worktree is removed once the run has been closed for that long,
as long as its branch has no newer run and no active one: a newer run may be
using the same directory, and its worktree has no uncommitted changes. Only
the directory goes. The branch, and everything committed on it, stays.

An assessment removes its own empty "plan-..." folder when it finishes, but a
restart part way through leaves it behind. Every cycle, such a folder is
removed if it is empty and no assessment is using it. One that still
has contents is left for a person to look at.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import assessments, gitops, runs
from .config import ConfigError
from .redact import log, warn

# Folders already warned about, so one that can't be removed is reported once
# rather than every cycle. It is still retried each cycle.
_warned = set()


def retention_days(cfg):
    """failed_worktree_days, or None if it isn't set. Raises ConfigError if it isn't a number of days."""
    days = cfg.get("failed_worktree_days")
    if days is None:
        return None
    if isinstance(days, bool) or not isinstance(days, (int, float)) or days < 0:
        raise ConfigError(f"failed_worktree_days must be a number of days, 0 or more, not {days!r}")
    return days


def failed_worktrees(cfg, conn):
    days = retention_days(cfg)
    if days is None:
        return
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    for row in runs.old_failed(conn, cutoff):
        try:
            _remove(cfg, conn, row, days)
        except Exception as e:  # one bad folder must not stop the others, or the cycle
            _warn_once(row, f"unexpected error: {e!r}")


def _remove(cfg, conn, row, days):
    project_cfg = cfg["projects"].get(row["project"])
    if project_cfg is None:
        return  # its repository is no longer known, so leave the folder alone
    worktree = row["worktree"]
    if Path(worktree).exists():
        # remove_worktree forces the removal, so check first: uncommitted work
        # is left for a person to look at rather than thrown away.
        if gitops.has_changes(project_cfg["repo_path"], worktree):
            _warn_once(row, "it has uncommitted changes, so it was left in place")
            return
        problem = gitops.remove_worktree(project_cfg["repo_path"], worktree)
        if problem:
            _warn_once(row, problem)
            return
        log(f"#{row['issue_key']}: removed worktree {worktree}, failed over {days} day(s) ago; "
            f"branch {row['branch']} kept")
    runs.forget_worktree(conn, row["id"])
    _warned.discard(worktree)


def _warn_once(row, problem):
    if row["worktree"] not in _warned:
        _warned.add(row["worktree"])
        warn(f"#{row['issue_key']}: could not remove worktree {row['worktree']}: {problem}")


def assessment_folders(cfg, conn):
    # Runs are claimed on this thread before their workers start, and keep their
    # slot until their folder is gone, so any attempt that could be using its
    # folder right now is already counted here.
    in_use = {assessments.folder_name(row["snapshot_id"], row["agent"]) for row in runs.running_assessments(conn)}
    for name, project_cfg in cfg["projects"].items():
        root = project_cfg.get("worktree_root")
        if root is None:
            continue
        try:
            folders = sorted(Path(root).iterdir())
        except FileNotFoundError:
            continue
        except OSError as e:
            _warn_folder_once(conn, name, root, f"could not list it: {e}")
            continue
        for folder in folders:
            if assessments.is_folder_name(folder.name) and folder.name not in in_use:
                try:
                    _remove_assessment_folder(conn, name, folder)
                except Exception as e:  # one bad folder must not stop the others, or the cycle
                    _warn_folder_once(conn, name, folder, f"unexpected error: {e!r}")


def _remove_assessment_folder(conn, project, folder):
    if folder.is_symlink() or not folder.is_dir():
        return  # DD never makes these, so it doesn't remove them either
    if any(folder.iterdir()):
        # Only the empty container is DD's to remove. Contents may be a
        # checkout the agent could have changed, so a person decides.
        _warn_folder_once(conn, project, folder, "it is not empty, so it was left in place")
        return
    try:
        folder.rmdir()
    except FileNotFoundError:
        return
    except OSError as e:
        _warn_folder_once(conn, project, folder, str(e))
        return
    _warned.discard(str(folder))
    log(f"{project}: removed leftover assessment folder {folder}")
    runs.record_event(conn, None, "assessment_folder_removed", f"Removed leftover empty assessment folder {folder}",
                      project=project)


def _warn_folder_once(conn, project, folder, problem):
    if str(folder) not in _warned:
        _warned.add(str(folder))
        warn(f"{project}: could not remove assessment folder {folder}: {problem}")
        runs.record_event(conn, None, "warning", f"Could not remove assessment folder {folder}: {problem}",
                          project=project)

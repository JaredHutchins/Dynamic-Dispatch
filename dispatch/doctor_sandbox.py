"""Read-only sandbox diagnostics, based on account and filesystem metadata."""

import grp
import os
from pathlib import Path
import pwd
import stat

from . import config, stories


def _bits(info, account, groups):
    shift = 6 if info.st_uid == account.pw_uid else 3 if info.st_gid in groups else 0
    return (info.st_mode >> shift) & 7


def _metadata(path):
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode):
        raise ValueError('symlink')
    # ACL values are not needed: their presence makes a mode-only verdict uncertain.
    if any('acl' in name.lower() for name in os.listxattr(path, follow_symlinks=False)):
        raise ValueError('ACL')
    return info


def _access(path, account, groups, mask):
    path = Path(os.path.abspath(path))
    infos = [_metadata(parent) for parent in reversed(path.parents)]
    info = _metadata(path)
    if account.pw_uid == 0:
        return True
    return all(_bits(parent, account, groups) & 1 for parent in infos) and bool(
        _bits(info, account, groups) & mask)


def _secret(path, account, groups):
    if _metadata(path).st_uid == account.pw_uid or _access(path, account, groups, 4):
        return 'FAIL', 'File ownership or permissions allow reading; remove agent ownership and group/other read access.'
    return 'OK', 'Owner, group and mode deny reading; no file contents were read.'


def _checkout(path, account, groups):
    # A writable ancestor can allow replacing the checkout even when its files are read-only.
    for parent in Path(path).parents:
        parent_info = _metadata(parent)
        if parent_info.st_uid == account.pw_uid:
            return 'FAIL', 'Agent owns a checkout ancestor; move DD under directories the agent cannot change.'
        if _access(parent, account, groups, 2) and _access(parent, account, groups, 1):
            if parent_info.st_mode & stat.S_ISVTX:
                raise ValueError('sticky ancestor')
            return 'FAIL', 'An ancestor is writable; remove agent write access to DD checkout ancestors.'
    pending = [Path(path)]
    while pending:
        current = pending.pop()
        info = _metadata(current)
        if info.st_uid == account.pw_uid:
            return 'FAIL', 'Agent owns checkout contents and can change their modes; give ownership to DD.'
        if _access(current, account, groups, 2):
            if not stat.S_ISDIR(info.st_mode) or _access(current, account, groups, 1):
                return 'FAIL', 'Checkout contents are writable; remove agent ownership and write permissions from DD code.'
        if stat.S_ISDIR(info.st_mode):
            pending.extend(current.iterdir())
    return 'OK', 'Owner, group and mode deny writes to the checkout and its contents.'


def _administrator(account, groups):
    names = {grp.getgrgid(gid).gr_name for gid in groups}
    if account.pw_uid == 0 or names & {'sudo', 'admin', 'wheel'}:
        return 'FAIL', 'Account is root or in an administrator group; use an unprivileged account outside sudo, admin and wheel.'
    return 'OK', 'Account is not root and belongs to none of sudo, admin or wheel.'


def _judge(report, label, action):
    try:
        status, reason = action()
    except (OSError, KeyError, ValueError, TypeError, config.ConfigError):
        status, reason = 'WARN', ("Cannot judge from available metadata (including ACLs or symlinks); "
                                  "inspect the setup and verify access as the agent account. No access was attempted.")
    report(status, label, reason)


def check(cfg, accounts, checkout, config_path, report):
    """Report isolation without opening secrets, impersonating accounts or changing files."""
    for index, (name, login) in enumerate(accounts.items(), 1):
        prefix = f'Agent {index} '
        actions = {
            'config isolation': lambda a, g: _secret(config_path, a, g),
            'private key isolation': lambda a, g: _secret(cfg['github']['private_key_path'], a, g),
            'checkout isolation': lambda a, g: _checkout(checkout, a, g),
            'administrator groups': _administrator,
        }
        try:
            account = pwd.getpwnam(login) if login else None
            groups = set(os.getgrouplist(login, account.pw_gid)) | {account.pw_gid} if account else None
        except (OSError, KeyError, TypeError):
            account, groups = None, None
        for label, action in actions.items():
            if login is None:
                report('SKIP', prefix + label, "skipped: this agent's account check failed")
            elif label == 'private key isolation' and not cfg['github'].get('app_id'):
                report('OK', prefix + label, 'No GitHub App private key is configured.')
            elif account is None:
                report('WARN', prefix + label, 'Cannot resolve account groups; repair account lookup and verify as the agent account.')
            else:
                _judge(report, prefix + label, lambda: action(account, groups))
    for index, project in enumerate(cfg['projects'].values(), 1):
        label = f'Story folders {index}'
        names = config.project_agents(project)
        if any(accounts.get(name) is None for name in names):
            report('SKIP', label, "skipped: a project agent's account check failed")
        else:
            _judge(report, label, lambda: _folders(cfg, project, names))


def _folders(cfg, project, names):
    groups = {config.agent_group(cfg, name) for name in names}
    folders = [folder for folder in Path(project['worktree_root']).iterdir()
               if stories.is_story_folder(project, folder.name)]
    for folder in folders:
        info = _metadata(folder)
        if not stat.S_ISDIR(info.st_mode) or info.st_gid not in groups or stat.S_IMODE(info.st_mode) != 0o2770:
            return 'WARN', "Story folders differ; set each folder's group to its agent's group and mode to 2770."
    if folders and len(groups) > 1:
        return 'WARN', 'Folder groups and modes match project agents; verify each folder belongs to the agent assigned its story.'
    return 'OK', 'Existing story folders use project agent groups and mode 2770; no folders were changed.'

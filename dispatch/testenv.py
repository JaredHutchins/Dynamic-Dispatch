"""Project test environments, separate from agent model credentials."""

import os
import pwd
import re
import stat

DEFAULT_PATH = '/usr/local/bin:/usr/bin:/bin'


def validate_values(values):
    if not isinstance(values, dict):
        raise ValueError('test_env must be an object of names and text values')
    for key, value in values.items():
        if (not isinstance(key, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key)
                or key in ('HOME', 'PATH') or not isinstance(value, str) or '\0' in value):
            raise ValueError('Test settings need valid names and text values; HOME and PATH are reserved')


def validate(project):
    validate_values(project.get('test_env', {}))
    paths = project.get('test_path', [])
    if not isinstance(paths, list) or any(not isinstance(p, str) or not p.startswith('/')
                                         or ':' in p or '\0' in p for p in paths):
        raise ValueError('test_path must be a list of absolute directories without colons')
    if 'test_env_file' in project:
        path = project['test_env_file']
        if not isinstance(path, str) or not path.startswith('/') or '\0' in path:
            raise ValueError('test_env_file must be an absolute file path')


def read_values(path):
    """Literal KEY=VALUE lines, never shell code; errors never contain values."""
    values = {}
    with open(path, newline='') as stream:
        for line in stream:
            line = line.rstrip('\n')
            if not line or line.startswith('#'):
                continue
            key, separator, value = line.partition('=')
            if not separator:
                raise ValueError('Malformed test_env_file line; use KEY=VALUE without quotes')
            if key in values:
                raise ValueError('Duplicate setting in test_env_file')
            values[key] = value
    validate_values(values)
    return values


def check_file(path, accounts):
    """Fail closed for ACLs and symlinks; allow only DD, root and project agents."""
    info = os.lstat(path)
    if (not stat.S_ISREG(info.st_mode)
            or any('acl' in name.lower() for name in os.listxattr(path))):
        raise ValueError('test_env_file must be a regular file without ACLs or symlinks')
    allowed = {0, os.geteuid()} | {pwd.getpwnam(name).pw_uid for name in accounts}
    if info.st_uid not in allowed or info.st_mode & 0o007:
        raise ValueError('test_env_file is accessible outside DD and project agent accounts')
    if info.st_mode & 0o070:
        for account in pwd.getpwall():
            if account.pw_uid not in allowed and info.st_gid in os.getgrouplist(account.pw_name, account.pw_gid):
                raise ValueError('test_env_file group includes an unrelated account')


def path(project):
    return ':'.join(project.get('test_path', []) + [DEFAULT_PATH])

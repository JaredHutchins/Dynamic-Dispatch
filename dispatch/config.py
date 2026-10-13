"""Configuration loading.

Everything environment-specific lives in config.json, which is gitignored and
must be readable only by DD's user. Nothing in this package
hardcodes a path, a repo, or an agent name.
"""

import grp
import json
import os
import pwd
import shutil
import stat
import tempfile
from pathlib import Path

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"

REQUIRED_TOP = ("plane", "github", "sandbox", "agents", "projects", "db_path")
REQUIRED_AGENT = ("display_name", "plane_token", "command", "parser", "git_author")
REQUIRED_PROJECT = ("plane_project_id", "github_repo", "repo_path", "worktree_root", "states")
REQUIRED_STATES = ("in_progress", "in_review", "blocked")
GITHUB_APP = ("app_id", "installation_id", "private_key_path")
DEFAULT_COOLDOWN_MINUTES = 60
REQUIRED_CYCLES = ("product_backlog_state", "sprint_backlog_state", "carried_over_label_id")
# Sprint-pull's whole numbers: (default, smallest allowed).
CYCLE_NUMBERS = {
    "max_stories": (10, 1),
    "replan_quiet_minutes": (15, 0),
    "blocked_reminder_minutes": (60, 1),
}
DEFAULT_BLOCKED_SOUND = "telegraph"


class ConfigError(Exception):
    pass


def _require(mapping, keys, where):
    missing = [k for k in keys if k not in mapping]
    if missing:
        raise ConfigError(f"{where} is missing: {', '.join(missing)}")


def _require_private(path):
    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ConfigError(f"{path} holds credentials and must be private: run chmod 600 {path}")


def _check_github(github_cfg):
    """Either all of the App's settings, or none of them and a plain token."""
    present = [k for k in GITHUB_APP if github_cfg.get(k)]
    if not present:
        _require(github_cfg, ("token",), "github")
        return
    _require({k: v for k, v in github_cfg.items() if v}, GITHUB_APP, "github (for the GitHub App)")
    key = Path(github_cfg["private_key_path"])
    try:
        is_file = key.is_file()
    except OSError as e:
        raise ConfigError(f"github private_key_path {key} cannot be read: {e.strerror}") from None
    if not is_file:
        raise ConfigError(f"github private_key_path {key} is not a file")
    _require_private(key)
    if not shutil.which("openssl"):
        raise ConfigError("github uses a GitHub App, which needs the openssl command to sign in")


def load(path=None, *, allow_empty_projects=False):
    path = Path(path or DEFAULT_CONFIG_PATH)
    if not path.exists():
        raise ConfigError(f"No config at {path}. Copy config.example.json and fill it in.")
    _require_private(path)

    with open(path) as f:
        cfg = json.load(f)

    _require(cfg, REQUIRED_TOP, "config.json")
    _require(cfg["sandbox"], ("user", "launcher"), "sandbox")
    _check_github(cfg["github"])
    if not cfg["agents"]:
        raise ConfigError("config.json defines no agents")
    if not cfg["projects"] and not allow_empty_projects:
        raise ConfigError("config.json defines no projects")

    for name, agent_cfg in cfg["agents"].items():
        maximum = agent_cfg.setdefault("max_concurrent", 1)
        if type(maximum) is not int or maximum != 1:
            raise ConfigError(f"agent '{name}' max_concurrent must be 1: cleaning up an agent's "
                              "processes kills everything its account runs, so two runs on the "
                              "same account would kill each other.")
        _require(agent_cfg, REQUIRED_AGENT, f"agent '{name}'")
        _require(agent_cfg["git_author"], ("name", "email"), f"agent '{name}' git_author")
        prompt_via = agent_cfg.setdefault("prompt_via", "argument")
        if prompt_via not in ("argument", "stdin"):
            raise ConfigError(f"agent '{name}' prompt_via must be 'argument' or 'stdin'")
        if prompt_via == "argument" and "{prompt}" not in agent_cfg["command"]:
            raise ConfigError(f"agent '{name}' command has no {{prompt}} placeholder (prompt_via='argument')")
        if prompt_via == "stdin" and any("{prompt}" in part for part in agent_cfg["command"]):
            raise ConfigError(f"agent '{name}' command must not contain {{prompt}} when prompt_via='stdin'")
        if "group" in agent_cfg and (not isinstance(agent_cfg["group"], str) or not agent_cfg["group"].strip()):
            raise ConfigError(f"agent '{name}' group must be a non-empty group name")
        if "env" in agent_cfg:
            raise ConfigError(
                f"agent '{name}' has an 'env' block. Agent credentials belong in its "
                f"env_file, readable by the sandbox user - never in config.json."
            )

    # Agents sharing an account could read each other's credentials and kill
    # each other's processes when the sandbox is reaped.
    accounts = {}
    for name in cfg["agents"]:
        account = run_as(cfg, name)
        if account in accounts:
            raise ConfigError(
                f"agents '{accounts[account]}' and '{name}' both run as {account}. "
                f"Give each agent its own account with run_as."
            )
        accounts[account] = name

    for name, project in cfg["projects"].items():
        _require(project, REQUIRED_PROJECT, f"project '{name}'")
        _require(project["states"], REQUIRED_STATES, f"project '{name}' states")
        done = project["states"].get("done")
        if done is not None and (not isinstance(done, str) or not done.strip()):
            raise ConfigError(f"project '{name}' states done must be a Plane state ID, or left out")
        if "agents" not in project and "default_agent" not in project:
            raise ConfigError(f"project '{name}' is missing: agents")
        allowed = project_agents(project)
        if not isinstance(allowed, list) or not allowed or not all(isinstance(a, str) for a in allowed):
            raise ConfigError(f"project '{name}' agents must be a list of agent names, not {allowed!r}")
        for agent_name in allowed:
            if agent_name not in cfg["agents"]:
                raise ConfigError(
                    f"project '{name}' lists agent '{agent_name}', which is not defined in agents"
                )

    _check_cycles(cfg)
    cooldown_minutes(cfg)
    return cfg


def _check_cycles(cfg):
    """Validate each project's optional sprint-pull settings, filling in the defaults."""
    sprint_pull = False
    for name, project in cfg["projects"].items():
        if "cycles" not in project:
            continue
        cycles = project["cycles"]
        where = f"project '{name}' cycles"
        if not isinstance(cycles, dict):
            raise ConfigError(f"{where} must be an object of sprint-pull settings")
        _require(cycles, REQUIRED_CYCLES, where)
        for key in REQUIRED_CYCLES:
            if not isinstance(cycles[key], str) or not cycles[key].strip():
                raise ConfigError(f"{where} {key} must be a Plane ID")
        for key, (default, least) in CYCLE_NUMBERS.items():
            value = cycles.setdefault(key, default)
            if isinstance(value, bool) or not isinstance(value, int) or value < least:
                raise ConfigError(f"{where} {key} must be a whole number, {least} or more, not {value!r}")
        sprint_pull = True
    approver = cfg["plane"].get("approver_user_id")
    if approver is not None and (not isinstance(approver, str) or not approver.strip()):
        raise ConfigError("plane approver_user_id must be the approver's Plane user ID, or left out")
    if sprint_pull and approver is None:
        raise ConfigError("plane is missing: approver_user_id (sprint-pull needs the approver's Plane user ID)")


def cycles(project_cfg):
    """A project's sprint-pull settings, or None when sprint-pull is off for it."""
    return project_cfg.get("cycles")


def project_agents(project_cfg):
    """The agents allowed to build a project's stories, in order of preference.

    A config from before the list existed names one default_agent instead.
    """
    if "agents" in project_cfg:
        return project_cfg["agents"]
    return [project_cfg["default_agent"]]


def run_as(cfg, name):
    """The account an agent runs as. Without run_as, the sandbox user."""
    return cfg["agents"][name].get("run_as") or cfg["sandbox"]["user"]


def cooldown_minutes(cfg):
    """How long an agent is skipped after hitting its usage limit. Raises ConfigError if it isn't a number."""
    minutes = cfg.get("usage_limit_cooldown_minutes", DEFAULT_COOLDOWN_MINUTES)
    if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or minutes < 0:
        raise ConfigError(f"usage_limit_cooldown_minutes must be a number of minutes, 0 or more, not {minutes!r}")
    return minutes


def blocked_sound(cfg):
    """The Bark sound Blocked alerts play. An empty name leaves Bark's default sound."""
    return cfg.get("alerts", {}).get("blocked_sound", DEFAULT_BLOCKED_SOUND)


def _account(name):
    """(uid, group ids) of an account on this machine. Raises ConfigError if there is none."""
    try:
        entry = pwd.getpwnam(name)
    except KeyError:
        raise ConfigError(f"{name} is not an account on this machine") from None
    return entry.pw_uid, set(os.getgrouplist(name, entry.pw_gid))


def _can_read(st, uid, gids):
    """By the file's mode bits alone. An ACL, or a directory above it, isn't considered."""
    if uid == 0:
        return True
    if st.st_uid == uid:
        return bool(st.st_mode & stat.S_IRUSR)
    if st.st_gid in gids:
        return bool(st.st_mode & stat.S_IRGRP)
    return bool(st.st_mode & stat.S_IROTH)


def check_env_files(cfg):
    """Refuse an agent's env_file that another agent's account can read.

    Separate from load() because it looks up real accounts and files, so it
    only means something on the machine that runs the agents.
    """
    for name, agent_cfg in cfg["agents"].items():
        env_file = agent_cfg.get("env_file")
        if not env_file:
            continue
        try:
            st = os.stat(env_file)
        except OSError as e:
            raise ConfigError(f"agent '{name}' env_file {env_file} cannot be read: {e.strerror}") from None
        for other in cfg["agents"]:
            if other == name:
                continue
            account = run_as(cfg, other)
            if _can_read(st, *_account(account)):
                raise ConfigError(
                    f"agent '{name}' env_file {env_file} can be read by {account}, the account "
                    f"agent '{other}' runs as. Make it readable only by {run_as(cfg, name)} "
                    f"and DD."
                )


def agent(cfg, name):
    try:
        return cfg["agents"][name]
    except KeyError:
        raise ConfigError(f"Unknown agent: {name}") from None


def poll_interval(cfg):
    return int(cfg.get("poll_interval_seconds", 30))


def agent_group(cfg, name):
    """Resolve an explicit group, or the run_as account's primary group."""
    group = cfg["agents"][name].get("group")
    account = run_as(cfg, name)
    if group:
        try:
            return grp.getgrnam(group).gr_gid
        except KeyError:
            raise ConfigError(f"agent '{name}' account {account}: group {group!r} does not exist") from None
    try:
        gid = pwd.getpwnam(account).pw_gid
    except KeyError:
        raise ConfigError(f"agent '{name}': account {account} does not exist") from None
    try:
        grp.getgrgid(gid)
    except KeyError:
        raise ConfigError(
            f"agent '{name}' account {account}: primary group ID {gid} does not exist; "
            "fix the account's primary group"
        ) from None
    return gid


def check_groups(cfg):
    """Check host memberships before any story can run."""
    orchestrator = pwd.getpwuid(os.geteuid()).pw_name
    memberships = set(os.getgroups()) | {os.getegid()}
    groups = {name: agent_group(cfg, name) for name in cfg["agents"]}
    for name, gid in groups.items():
        group = grp.getgrgid(gid).gr_name
        if gid not in memberships:
            raise ConfigError(f"DD's account {orchestrator} must be a member of group {group}; restart DD after fixing membership")
        account = run_as(cfg, name)
        uid, own_groups = _account(account)
        if gid not in own_groups:
            raise ConfigError(f"Account {account} must be a member of its agent group {group}")
        for other, other_gid in groups.items():
            if other != name and (uid == 0 or other_gid in own_groups):
                raise ConfigError(f"Account {account} must not be a member of agent '{other}' group {grp.getgrgid(other_gid).gr_name}")


def save_project(path, name, project, original):
    """Validate privately, retain an exact private backup, then atomically replace."""
    path = Path(path)
    if path.read_bytes() != original:
        raise ConfigError("Config changed during setup; run setup again")
    _require_private(path)
    mode = stat.S_IMODE(path.stat().st_mode)
    cfg = json.loads(original)
    cfg["projects"][name] = project
    candidate = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".dd-config-", delete=False) as f:
            candidate = Path(f.name)
            os.fchmod(f.fileno(), mode)
            f.write((json.dumps(cfg, indent=2) + "\n").encode())
            f.flush()
            os.fsync(f.fileno())
        load(candidate)
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + ".backup-", delete=False) as f:
            os.fchmod(f.fileno(), mode)
            f.write(original)
            f.flush()
            os.fsync(f.fileno())
        if path.read_bytes() != original:
            raise ConfigError("Config changed during setup; run setup again")
        os.replace(candidate, path)
    finally:
        if candidate is not None:
            candidate.unlink(missing_ok=True)

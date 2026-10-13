"""Choosing which agent builds a story.

Every choice is made here, so what "available" means lives in one place. An
agent is available only when its account has a free slot and it is not
cooling down after hitting its usage limit. Queued runs reserve their slots.
While a main check waits for an account, its project's accounts are held from
new work, so the next one to come free goes to the main check.

Rework goes back to the agent that had the story's previous run, if it is
still allowed and available. A new story goes to the available agent with the
fewest runs in the last RECENT_DAYS days, ties to the first in the project's
list. Nothing here knows any agent by name.
"""

from datetime import datetime, timedelta, timezone

from . import config, runs

RECENT_DAYS = 7

# Accounts held for a main check that is waiting for one, by project. Only the
# main check's own poll sets this, each cycle, so a restart simply starts over.
_held = {}


def _stamp(moment):
    return moment.isoformat(timespec="seconds")


def hold_for_main_check(project, names):
    """Keep these accounts from new work until the project's main check has one; no names releases them."""
    if names:
        _held[project] = set(names)
    else:
        _held.pop(project, None)


def available(cfg, conn, names, at=None, exclude=None, *, held=True):
    """The agents among these that may start work now, in the same order.

    held=False is for a run that has already claimed its slot: a waiting main
    check gets the next free account, but never takes one from work under way.
    """
    at = at or datetime.now(timezone.utc)
    cooling = runs.cooldowns(conn, _stamp(at))
    busy = runs.busy_agents(conn, exclude)
    if held:
        busy |= set().union(*_held.values())
    return [name for name in names if name in cfg["agents"] and name not in cooling and name not in busy]


def agent_statuses(cfg, conn, names, at=None):
    """Read account work, rest times and holds without choosing or reserving an agent."""
    at = at or datetime.now(timezone.utc)
    cooling = runs.cooldowns(conn, _stamp(at))
    work = runs.agent_work(conn)
    held = set().union(*_held.values())
    return [{"agent": name, "work": work.get(name), "until": cooling.get(name),
             "held": name in held, "configured": name in cfg["agents"]} for name in names]


def agent_for(cfg, conn, project_cfg, issue_id, at=None):
    """The agent to build this story, or None if no allowed agent is available."""
    at = at or datetime.now(timezone.utc)
    ready = available(cfg, conn, config.project_agents(project_cfg), at)
    if not ready:
        return None
    previous = runs.last_agent(conn, issue_id)
    if previous in ready:
        return previous
    counts = runs.counts_since(conn, _stamp(at - timedelta(days=RECENT_DAYS)))
    # min() keeps the first of equals, so a tie goes to the first in the list.
    return min(ready, key=lambda name: counts.get(name, 0))


def waiting_on_busy(cfg, conn, names, at=None):
    """Busy wording applies when a non-cooling account is occupied."""
    at = at or datetime.now(timezone.utc)
    cooling = runs.cooldowns(conn, _stamp(at))
    busy = runs.busy_agents(conn)
    return any(name in cfg["agents"] and name in busy and name not in cooling for name in names)


def next_available(cfg, conn, names, at=None):
    """Which of these agents' cool-downs ends first, as (agent, until), or None if none is cooling down."""
    at = at or datetime.now(timezone.utc)
    cooling = runs.cooldowns(conn, _stamp(at))
    ends = [(datetime.fromisoformat(cooling[name]), name) for name in names
            if name in cfg["agents"] and name in cooling]
    if not ends:
        return None
    # min() keeps the first of equals, so a tie goes to the first in the list.
    until, name = min(ends, key=lambda end: end[0])
    return name, until


def cool_down(cfg, conn, agent, at=None, usage_limit_until=None, run_id=None):
    """Skip this agent until its cool-down is over. Returns when that is."""
    at = at or datetime.now(timezone.utc)
    reported = (usage_limit_until is not None
                and at < usage_limit_until <= at + timedelta(days=7))
    until = (usage_limit_until.astimezone(timezone.utc) + timedelta(minutes=2) if reported
             else at + timedelta(minutes=config.cooldown_minutes(cfg)))
    runs.save_cooldown(conn, agent, _stamp(until))
    if run_id is not None:
        runs.record_event(conn, run_id, "cooldown",
                          f"Reset time source: {'agent' if reported else 'fallback'}; until {_stamp(until)}",
                          agent=agent)
    return until

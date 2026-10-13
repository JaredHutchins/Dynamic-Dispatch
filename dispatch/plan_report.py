"""Merging a round's assessments into one plan, and DD's rough estimate of whether it fits.

Plain functions over plain data, with no AI and no I/O, so the same answers
always give the same plan.
"""

from datetime import timedelta
import statistics

from . import cycle_dates

WEIGHTS = {'S': 0.5, 'M': 1, 'L': 2}
HISTORY = 30  # Done stories the estimate looks back over


def merge(stories, answers):
    """One plan from every agent's answer; stories is the round's snapshot, answers is keyed by agent.

    Dependencies and overlaps are unions, sizes and risks stay per agent, and
    the order is the average position, moved so nothing precedes what it needs.
    """
    ids = [story['id'] for story in stories]
    agents = sorted(answers)
    rows = {agent: {row['id']: row for row in answers[agent]['stories']} for agent in agents}
    merged = {}
    for story in stories:
        sid = story['id']
        # An overlap is mutual, whichever side an agent named it from.
        overlapping = {x for a in agents for x in rows[a][sid]['overlaps']}
        overlapping |= {x for a in agents for x in ids if sid in rows[a][x]['overlaps']}
        merged[sid] = {
            'name': story['name'],
            'depends_on': [x for x in ids if any(x in rows[a][sid]['depends_on'] for a in agents)],
            'depends_by': {a: [x for x in ids if x in rows[a][sid]['depends_on']] for a in agents},
            'overlaps': [x for x in ids if x in overlapping],
            'sizes': {a: rows[a][sid]['size'] for a in agents},
            'risks': {a: {'risk': rows[a][sid]['risk'], 'reason': rows[a][sid]['reason']} for a in agents},
            'push': {a: row['reason'] for a in agents for row in answers[a]['push'] if row['id'] == sid},
            'position': statistics.fmean(answers[a]['build_order'].index(sid) + 1 for a in agents),
        }
    loops = find_loops(ids, merged)
    return {'agents': agents, 'stories': merged, 'loops': loops, 'order': build_order(ids, merged, loops)}


def reaches(merged, start):
    """Every story start depends on, directly or through others."""
    seen, todo = set(), list(merged[start]['depends_on'])
    while todo:
        sid = todo.pop()
        if sid not in seen:
            seen.add(sid)
            todo.extend(merged[sid]['depends_on'])
    return seen


def find_loops(ids, merged):
    """Each group of stories that depend on each other, with who said each link."""
    reach = {sid: reaches(merged, sid) for sid in ids}
    loops, looped = [], set()
    for sid in ids:
        if sid in looped or sid not in reach[sid]:
            continue
        group = [x for x in ids if x == sid or (x in reach[sid] and sid in reach[x])]
        looped.update(group)
        links = [[a, b, [agent for agent, deps in merged[a]['depends_by'].items() if b in deps]]
                 for a in group for b in merged[a]['depends_on'] if b in group]
        loops.append({'stories': group, 'links': links})
    return loops


def build_order(ids, merged, loops):
    """Lowest average position first, a story only once what it needs is placed.

    Ties keep the Cycle's listing order. Inside a loop nothing can wait for
    everything it needs, so its lowest-placed story goes first.
    """
    looped = {sid for loop in loops for sid in loop['stories']}
    remaining = sorted(ids, key=lambda sid: (merged[sid]['position'], ids.index(sid)))
    placed = []
    while remaining:
        ready = next((sid for sid in remaining if all(d in placed for d in merged[sid]['depends_on'])), None)
        if ready is None:
            ready = next(sid for sid in remaining if sid in looped)
        placed.append(ready)
        remaining.remove(ready)
    return placed


def disagreements(merged):
    """(story ID, what differs) for each story where the agents' answers part."""
    found = []
    for sid in merged['order']:
        story = merged['stories'][sid]
        if len(set(story['sizes'].values())) > 1:
            found.append((sid, 'size'))
        if len({r['risk'] for r in story['risks'].values()}) > 1:
            found.append((sid, 'risk'))
        if len({tuple(deps) for deps in story['depends_by'].values()}) > 1:
            found.append((sid, 'depends_on'))
        if story['push']:
            found.append((sid, 'push'))
    return found


def finish(merged, keep, unit, agents, now):
    """When the kept stories would be done: the work spread over agents, never shorter than the longest chain.

    Whatever a kept story depends on is counted too, so a dependency is never
    dropped from the chain just because its story was left out of keep.
    """
    needed = set(keep).union(*(reaches(merged['stories'], sid) for sid in keep))
    work = {sid: unit * statistics.fmean(WEIGHTS[size] for size in merged['stories'][sid]['sizes'].values())
            for sid in needed}
    chain = {}
    for sid in merged['order']:
        if sid in work:
            chain[sid] = work[sid] + max((chain[d] for d in merged['stories'][sid]['depends_on'] if d in chain),
                                         default=0)
    longest = max(chain.values(), default=0)
    seconds = max(sum(work.values()) / agents, longest)
    at = now + timedelta(seconds=seconds)
    # The date is shown in DD's local time; fit compares the exact moment.
    return {'days': round(seconds / 86400, 1), 'chain_days': round(longest / 86400, 1),
            'finish': at.astimezone().date().isoformat(), 'finish_at': at.isoformat()}


def pushable(merged, sid):
    """Whether no other Cycle story depends on it.

    Checked against the whole Cycle, not the stories still kept, so pushing a
    dependent story never frees what it needed to be pushed as well.
    """
    return not any(sid in merged['stories'][other]['depends_on'] for other in merged['order'] if other != sid)


def estimate(merged, durations, agents, end_date, now, start_date=None):
    """DD's rough finish from its own history, and the stories to push if that passes end_date.

    durations are seconds from first run to Done for recent Done stories. With
    none there is no estimate, only the count of zero. The finish counts from
    the later of now and start_date, the Cycle's start.
    """
    # A Cycle planned in advance can only start its work at its start moment.
    if start_date:
        now = max(now, cycle_dates.moment(start_date))
    end = cycle_dates.moment(end_date, end=True) if end_date else None
    result = {'based_on': len(durations), 'agents': agents,
              'end_date': cycle_dates.local_date(end_date)}
    if not durations:
        return result
    unit = statistics.median(durations)
    keep = list(merged['order'])
    result.update(finish(merged, keep, unit, agents, now), median_hours=round(unit / 3600, 1))
    result['fits'] = None if not end_date else cycle_dates.moment(result['finish_at']) <= end
    if result['fits'] is not False:
        return result
    pushed, after = [], result
    while cycle_dates.moment(after['finish_at']) > end:
        sid = next((sid for sid in reversed(keep) if pushable(merged, sid)), None)
        if sid is None:
            break
        keep.remove(sid)
        pushed.append(sid)
        after = finish(merged, keep, unit, agents, now)
    # The agents' own suggestions are offered too, under the same dependency rule.
    for sid in reversed(merged['order']):
        if merged['stories'][sid]['push'] and sid in keep and pushable(merged, sid):
            keep.remove(sid)
            pushed.append(sid)
    after = finish(merged, keep, unit, agents, now)
    result.update(push=pushed, push_finish=after['finish'], push_fits=cycle_dates.moment(after['finish_at']) <= end)
    return result


def agent_answers(story, field, names):
    return ', '.join(f"{names.get(agent, agent)} {value}" for agent, value in story[field].items())


def story_names(merged, ids):
    return ', '.join(merged['stories'][sid]['name'] for sid in ids) or 'none'


def fit_lines(est):
    if not est['based_on']:
        return ["DD has no Done stories to estimate from yet, so it can't say whether this Cycle fits its dates."]
    stories = 'story' if est['based_on'] == 1 else 'stories'
    lines = [f"Rough estimate: about {est['days']} days, finishing around {est['finish']}.",
             f"This is rough. It is based on {est['based_on']} past {stories}: the median time from a story's "
             f"first run to Done ({est['median_hours']} hours), weighted S 0.5, M 1, L 2, spread over "
             f"{est['agents']} {'agent' if est['agents'] == 1 else 'agents'} and never shorter than the longest "
             f"chain of dependencies ({est['chain_days']} days)."]
    if est['fits'] is None:
        lines.append("The Cycle has no end date to compare with.")
    elif est['fits']:
        lines.append(f"That fits the Cycle's end date, {est['end_date']}.")
    else:
        lines.append(f"That passes the Cycle's end date, {est['end_date']}.")
    return lines


def push_lines(merged, est, names):
    if not est.get('push'):
        return (["No story can be pushed without leaving behind a story that depends on it."]
                if est.get('fits') is False else [])
    lines = []
    for sid in est['push']:
        story = merged['stories'][sid]
        why = '; '.join(f"{names.get(a, a)}: {reason}" for a, reason in story['push'].items())
        lines.append(f"- {story['name']}" + (f" (suggested by {why})" if why else " (lowest in the order)"))
    outcome = "fits" if est['push_fits'] else "still passes"
    lines.append(f"Without them the estimate finishes around {est['push_finish']}, which {outcome} the end date.")
    return lines


def disagreement_line(merged, sid, kind, names):
    story = merged['stories'][sid]
    if kind == 'size':
        return f"- {story['name']}: sizes differ ({agent_answers(story, 'sizes', names)})."
    if kind == 'risk':
        risks = ', '.join(f"{names.get(a, a)} {r['risk']}" for a, r in story['risks'].items())
        return f"- {story['name']}: risks differ ({risks})."
    if kind == 'depends_on':
        deps = '; '.join(f"{names.get(a, a)}: {story_names(merged, d)}" for a, d in story['depends_by'].items())
        return f"- {story['name']}: dependencies differ ({deps})."
    who = '; '.join(f"{names.get(a, a)}: {reason}" for a, reason in story['push'].items())
    return f"- {story['name']}: wanted pushed ({who})."


def render(merged, est, names, round_number):
    """The plain-text report for the plan item's description; names maps agents to display names."""
    lines = [f"Plan for this Cycle (assessment round {round_number})", ""]
    if merged['loops']:
        lines += ["Needs your decision", ""]
        for loop in merged['loops']:
            links = '; '.join(f"{merged['stories'][a]['name']} needs {merged['stories'][b]['name']} "
                              f"({', '.join(names.get(x, x) for x in who)})" for a, b, who in loop['links'])
            lines.append(f"- Dependency loop: {links}.")
        lines += ["The plan can't be approved while a loop remains. Change the stories to break it, and DD "
                  "will assess the Cycle again.", ""]
    lines += ["Fit", ""] + fit_lines(est) + [""]
    pushes = push_lines(merged, est, names)
    if pushes:
        lines += ["Stories to push", ""] + pushes + [""]
    lines += ["Build order", ""]
    for number, sid in enumerate(merged['order'], 1):
        story = merged['stories'][sid]
        risks = '; '.join(f"{names.get(a, a)} {r['risk']}: {r['reason']}" for a, r in story['risks'].items())
        lines += [f"{number}. {story['name']}",
                  f"   Size: {agent_answers(story, 'sizes', names)}",
                  f"   Risk: {risks}",
                  f"   Depends on: {story_names(merged, story['depends_on'])}",
                  f"   Overlaps: {story_names(merged, story['overlaps'])}"]
    found = disagreements(merged)
    lines += ["", "Disagreements", ""]
    lines += [disagreement_line(merged, sid, kind, names) for sid, kind in found] or ["None."]
    return '\n'.join(lines)

"""The Docs line that ends an agent's summary, and checking it against the branch's changes.

The line takes one of two forms, "Docs: updated <files>" or "Docs: none
needed, because <reason>". Every file it names must be changed on the branch
and count as documentation. Changes outside the documentation and exempt
paths need the line; a branch that changes only those paths may leave it out.
"""

import re

UPDATED = "Docs: updated"
NONE_NEEDED = "Docs: none needed, because"
# Agents often bold or quote the label, so formatting around it is ignored.
_LINE = re.compile(r"[*_`\s]*Docs\s*:[*_`\s]*(.*)", re.IGNORECASE)
_FORMS = re.compile(r"(updated|none needed,? because)\b(.*)", re.IGNORECASE)


def _pattern(pattern):
    """A path pattern as a regular expression: ** spans folders, * and ? stay within one."""
    parts = []
    for token in re.split(r"(\*\*/|\*\*|\*|\?)", pattern):
        parts.append({"**/": "(?:.*/)?", "**": ".*", "*": "[^/]*", "?": "[^/]"}.get(token, re.escape(token)))
    return re.compile("".join(parts))


def matches(path, patterns):
    return any(_pattern(p.removeprefix("./")).fullmatch(path) for p in patterns)


def find(summary):
    """The summary's last line as (form, rest), form being "updated", "none" or "unknown".

    None when that line isn't a Docs line at all.
    """
    lines = [line for line in (summary or "").splitlines() if line.strip()]
    found = _LINE.fullmatch(lines[-1].strip()) if lines else None
    if not found:
        return None
    text = found.group(1).strip().strip("*_`").strip()
    form = _FORMS.match(text)
    if not form:
        return "unknown", text
    kind = "updated" if form.group(1).lower() == "updated" else "none"
    return kind, form.group(2).strip().strip("*_`:").strip()


def named_files(rest):
    """The file paths an "updated" line names, split on commas and "and"."""
    rest = re.sub(r"\([^)]*\)", "", rest)
    names = []
    for part in re.split(r",|\band\b", rest):
        name = part.strip(" `'\"*").rstrip(" `'\"*.").removeprefix("./")
        if name:
            names.append(name)
    return names


def _listing(paths):
    return ", ".join(paths) if paths else "(none)"


def problem(settings, summary, changed):
    """Why the summary's Docs line doesn't fit the changed files, or None when it does."""
    if not settings["check"]:
        return None
    documentation, exempt = settings["documentation"], settings["exempt"]
    undocumented = [p for p in changed if not matches(p, documentation) and not matches(p, exempt)]
    line = find(summary)
    if line is None:
        if not undocumented:
            return None
        return (f"Your summary doesn't end with a Docs line, but these changes may need documentation: "
                f"{_listing(undocumented)}.")
    kind, rest = line
    if kind == "unknown":
        return f"Your Docs line isn't in either of the two forms: \"Docs: {rest}\"."
    if kind == "none":
        if not rest.strip(" ."):
            return "Your Docs line says no documentation is needed but gives no reason."
        return None
    names = named_files(rest)
    if not names:
        return "Your Docs line says documentation was updated but names no files."
    unchanged = [n for n in names if n not in changed]
    if unchanged:
        return f"Your Docs line names files this branch doesn't change: {_listing(unchanged)}."
    not_documentation = [n for n in names if not matches(n, documentation)]
    if not_documentation:
        return f"Your Docs line names files that don't count as documentation: {_listing(not_documentation)}."
    return None


def instruction(settings, rework=False):
    """What the build and rework instructions tell the agent about the Docs line; empty when the check is off."""
    if not settings["check"]:
        return ""
    # An earlier round's line describes that round, so each round gives its own.
    again = "This round needs its own Docs line, even if an earlier summary had one. " if rework else ""
    return (
        "\nEnd your summary with a Docs line as its very last line, in one of these two forms:\n"
        f"{UPDATED} <files>\n"
        f"{NONE_NEEDED} <reason>\n"
        "Name each documentation file you changed by its path from the repository root, "
        "separated by commas. In this project, documentation is "
        f"{_listing(settings['documentation'])}, and changes only to {_listing(settings['exempt'])} "
        "never need it. Before the pull request is opened or updated, every file the line names "
        "must be changed and count as documentation, and any other change needs either a "
        f"documentation update or a reason. {again}\n"
    )


def send_back_note(settings, found, changed, summary):
    """The note a sent-back agent gets: what was wrong, what changed, and its earlier summary."""
    return (
        f"\nThe system running you checked the Docs line at the end of your summary and it isn't right yet:\n"
        f"{found}\n\n"
        f"Files this branch changes: {_listing(changed)}\n"
        f"Documentation is {_listing(settings['documentation'])}; changes only to "
        f"{_listing(settings['exempt'])} never need it.\n\n"
        "Update the documentation if the change needs it, or explain why none is needed. "
        "Your final message replaces your earlier summary as the pull request description, so give "
        "the complete summary again, ending with a correct Docs line. Do not run Git commands that "
        "change history: do not merge, rebase, reset, commit or push.\n\n"
        f"Your earlier summary:\n{summary or '(none)'}\n"
    )

"""How DD names a story in logs, commits and GitHub."""


def branch_name(project_cfg, issue):
    """The build branch for a Plane story."""
    return f"{project_cfg.get('branch_prefix', 'dd')}-{issue.get('sequence_id', '')}"


def is_story_folder(project_cfg, name):
    """Whether a folder name follows DD's Plane story branch naming rule."""
    prefix = project_cfg.get('branch_prefix', 'dd') + '-'
    sequence = name.removeprefix(prefix)
    return name.startswith(prefix) and sequence.isascii() and sequence.isdecimal()


def story_ref(project_cfg, run):
    """How the story is named on GitHub, e.g. "DYNAMICDIS-7".

    Never "#7": GitHub would link that to its own issue or pull request 7.
    """
    identifier = project_cfg.get("plane_identifier")
    return f"{identifier}-{run['issue_key']}" if identifier else str(run["issue_key"])

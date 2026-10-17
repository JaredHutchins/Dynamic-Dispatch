"""Combining a story with the latest main before upload.

Main is fetched and merged without committing. A clash, or a clean merge that
fails verification, goes back to the same agent to repair, at most twice per
run, and only the verified combination is committed. A red main holds the work
before combining, before each repair and after each repair or re-test.

Verification and prompts go through their modules' public names at call time,
so a test that replaces one there sees every call.
"""

import re
import sqlite3

from . import agents, choose, github, gitops, mainhealth, plane, prepare, prompts, runs, sync, verify
from .redact import log, scrub
from .stories import story_ref


def _combined_titles(combined, github_cfg, repo, event):
    resolved = {}
    titles = []
    for fallback, number in zip(combined["titles"], combined["pr_numbers"]):
        if number is not None and number not in resolved:
            try:
                resolved[number] = github.pr_title(repo, number, github.token(github_cfg))
            except Exception as e:
                # Title enrichment must never stop combining, even if authentication or decoding fails.
                resolved[number] = None
                event("warning", f"Could not look up pull request #{number}'s title; "
                      f"using the commit message: {scrub(str(e))}")
        titles.append(resolved.get(number) or fallback)
    return titles


def _failure_count(output):
    """Verification commands vary; only report a count when their output gives one."""
    counts = re.findall(r"(?:failures|errors)=(\d+)", output)
    if counts:
        return str(sum(map(int, counts)))
    match = re.search(r"(\d+) failed\b", output)
    return match.group(1) if match else "one or more"


def _failure_names(output, project_cfg=None):
    return "; ".join(verify.failing_tests(output, project_cfg)) or f"{_failure_count(output)} failing tests (names unavailable)"


def with_main(ctx, issue, title, result):
    """Combine the story branch with main, repairing it with the agent if needed.

    result is what a main hold saves ({} for a refresh). Returns None when the
    run has ended here (failed, cancelled, held or usage-limited); otherwise
    (meanwhile, note), where note is "" if the branch already had main.
    """
    meanwhile = ctx.base
    combined_note = ""
    if mainhealth.hold(ctx.cfg, ctx.conn, ctx.run, ctx.worktree, result):
        return
    ctx.event("combining", "Combining with latest main")
    try:
        combined = gitops.combine_main(ctx.repo, ctx.worktree, ctx.run["branch"], ctx.base, ctx.agent_cfg["git_author"])
        if combined is not None:
            titles = _combined_titles(combined, ctx.cfg["github"], ctx.project_cfg["github_repo"], ctx.event)
            meanwhile = "; ".join(titles) or "no pull request titles recorded"
            details = gitops.conflict_markers(ctx.worktree, combined["files"].splitlines())
            clash = bool(combined["files"] or details)
            details = f"Clashing files: {combined['files']}\n\n{details}" if clash else ""
            attempt = runs.repair_attempts(ctx.conn, ctx.run["id"])
            repaired = False  # an agent touched this combination, so it can void an approval
            while True:
                if not clash:
                    ctx.event("testing", "Running tests after combining with main")
                    ok, details = prepared_verify(ctx)
                    if ok:
                        break
                if mainhealth.hold(ctx.cfg, ctx.conn, ctx.run, ctx.worktree, result):
                    return
                ctx.event("clash" if clash else "combined_tests_failed", details)
                if attempt >= 2:
                    return ctx.fail(
                        "Couldn't combine this story with the latest main after 2 tries. "
                        f"Merged meanwhile: {meanwhile}.\n\n{details}"
                    )
                attempt += 1
                if clash:
                    message = (
                        f"While this story was being built, {meanwhile} was merged into main. "
                        f"Combining them clashed in {combined['files'] or details}. "
                        f"Sent back to {ctx.agent_cfg['display_name']} to resolve (try {attempt} of 2)."
                    )
                else:
                    message = (
                        f"Combined this story with the latest main ({meanwhile}), and {_failure_count(details)} "
                        f"tests failed: {_failure_names(details, ctx.project_cfg)}. Sent back to {ctx.agent_cfg['display_name']} "
                        f"to fix (try {attempt} of 2)."
                    )
                try:
                    sync.send_back(ctx.cfg, ctx.run, attempt, message)
                except plane.PlaneError as e:
                    warning = f"Could not post repair comment; continuing repair: {e}"
                    ctx.event("warning", warning)
                    log(f"{story_ref(ctx.project_cfg, ctx.run)}: {warning}")
                runs.record_repair(ctx.conn, ctx.run["id"])
                ctx.event("sent_back", message)
                prompt = prompts.build_prompt(ctx.project_cfg, issue) + (
                    f"\nThe system running you has merged the latest main into your earlier saved work.\n"
                    f"Pull requests merged meanwhile: {meanwhile}\n"
                    f"{details}\n"
                    "Resolve clashes and fix your own change so both changes keep their intended "
                    "behaviour. Preserve your earlier story work and the changes from main. "
                    "Remove all conflict markers. Do not run Git commands that change history: "
                    "do not merge, rebase, reset, commit or push. The system running you handles Git."
                )
                ctx.event("agent_started", f"Repair started: {ctx.agent_cfg['display_name']}")
                repaired = True
                repair = None
                try:
                    repair = agents.run(ctx.cfg, ctx.agent_cfg, prompt, ctx.worktree, project_cfg=ctx.project_cfg)
                finally:
                    ctx.event("agent_finished", "Repair finished" if repair else "Repair stopped unexpectedly")
                runs.record_usage(ctx.conn, ctx.run["id"], ctx.run["agent"], repair.get("usage"), repair.get("cost_usd"),
                                  additive=True)
                if repair.get("usage_limit"):
                    if ctx.refresh:
                        choose.cool_down(ctx.cfg, ctx.conn, ctx.run["agent"],
                                         usage_limit_until=repair.get("usage_limit_until"), run_id=ctx.run["id"])
                        gitops.abort_pending_merge(ctx.repo, ctx.worktree)
                        ctx.event("merge_aborted", "Cancelled unfinished merge; restored last saved work")
                        ctx.event("refresh_waiting", "Builder cooling down; refresh will retry later")
                        return ctx.cancel("Builder reached its usage limit; refresh will retry later.")
                    # The story's own work is already committed, so the next run resumes it.
                    return ctx.limited(repair)
                if mainhealth.hold(ctx.cfg, ctx.conn, ctx.run, ctx.worktree, result):
                    return
                markers = gitops.conflict_markers(ctx.worktree, combined["files"].splitlines())
                clash = bool(markers)
                if clash:
                    details = markers
                elif not repair["ok"]:
                    return ctx.fail(f"Repair agent did not complete: {repair.get('error')}\n"
                                f"Merged meanwhile: {meanwhile}\n{details}")
            ctx.event("saving", "Saving verified merge")
            gitops.commit_all(ctx.repo, ctx.worktree, scrub(f"Combine latest {ctx.base}: {title}"), ctx.agent_cfg["git_author"])
            runs.mark_committed(ctx.conn, ctx.run["id"])
            combined_note = f"Combined with the latest main ({meanwhile}) and re-tested."
            try:
                sha = gitops.branch_revision(ctx.repo, ctx.run["branch"])
                runs.record_combine(ctx.conn, ctx.run["id"], sha, clean=not repaired, note=combined_note)
            except (gitops.GitError, sqlite3.Error) as e:
                # Unrecorded, the commit only counts as changing the code: an approval shows as voided.
                ctx.event("warning", f"Could not record the combine commit: {e}")
            ctx.event("combined_cleanly", combined_note)
        else:
            ctx.event("main_current", "Story already contains latest main")
            if (ctx.refresh or ctx.run["hold_data"]) and not _verify_saved(ctx, title, result):
                return
    except (gitops.GitError, plane.PlaneError) as e:
        return ctx.fail(f"Could not combine with latest main:\n\n{e}")
    return meanwhile, combined_note


def prepared_verify(ctx):
    """Prepare the combined checkout before its test gate."""
    ok, output = prepare.run(ctx.cfg, ctx.project_cfg, ctx.agent_cfg, ctx.worktree)
    if not ok:
        return False, output
    return verify.run_verify(ctx.cfg, ctx.project_cfg, ctx.agent_cfg, ctx.worktree)


def _verify_saved(ctx, title, result):
    # Startup already prepared this folder; no new combination changed its inputs.
    ctx.event("testing", "Re-testing saved combination before upload")
    ok, details = verify.run_verify(ctx.cfg, ctx.project_cfg, ctx.agent_cfg, ctx.worktree)
    if mainhealth.hold(ctx.cfg, ctx.conn, ctx.run, ctx.worktree, result):
        return
    if not ok:
        return ctx.fail(f"Verification of saved combination failed:\n\n{details}")
    if ctx.project_cfg.get("prepare_commands") and gitops.has_changes(ctx.repo, ctx.worktree):
        gitops.commit_all(ctx.repo, ctx.worktree, scrub(f"Prepare saved work: {title}"),
                          ctx.agent_cfg["git_author"])
        runs.mark_committed(ctx.conn, ctx.run["id"])
    return True

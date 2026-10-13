# Definition of Done — Dynamic Dispatch

A story is done when its pull request meets every item below and the approver
has merged it. The agent works toward this list; the approver checks it at
review.

## The change

1. **Every acceptance criterion in the story is met.** If one can't be met
   within the story's scope, stop and say so in the summary rather than
   widening the scope.
2. **It stays in scope.** Change only what the story's "In scope" section
   names. If something else has to change, keep it minimal and say why in the
   summary. Nothing listed under "Out of scope" is touched.
3. **Standard library only.** No new third-party packages unless the story
   asks for one.
4. **It reads like the code around it.** Same naming, patterns and comment
   style. Comments explain why, not what.

## Tests

5. **The full suite passes:** `python3 -m unittest discover -s tests`
   It must also run cleanly with `python3 -W error::ResourceWarning -m unittest discover -s tests`.
6. **New behaviour has a test.** A bug fix has a test that fails without the
   fix.
7. **Tests use fake services and fake secrets.** No test reaches the real
   Plane, GitHub or model provider.

## Safety

8. **No secrets anywhere.** No tokens, keys or passwords in code, tests,
   config examples or the summary.
9. **Safety-sensitive changes are called out first.** If the change touches
   any of these, the summary opens with that and explains the effect:
   - `deploy/`: the sandbox launcher and sudoers rule
   - `dispatch/proc.py`: how the agent is run and stopped
   - `dispatch/gitops.py`: Git safety and worktree setup
   - `dispatch/redact.py`: secret scrubbing
   - `dispatch/config.py`: config loading and its checks

## Explaining it

10. **The summary is plain English.** What changed, why, and anything the
    agent was unsure about. The summary is the agent's final message, written
    only after all work and checks have finished. It becomes the pull request
    description, so it must be complete, not a status update.
11. **Decisions are written down.** If the change alters a design decision,
    `ARCHITECTURE.md` is updated in the same pull request.

## Design rules

12. **All database access goes through the `dispatch/runs/` package.** Other modules
    call runs functions and never run SQL themselves.
    Reason: One database boundary keeps persistence rules consistent.
13. **State DD relies on is never kept only in the events table.** Events
    are best-effort history, not state, so no `dispatch/` module outside the
    `dispatch/runs/` package reads them. A guard in
    `tests/test_design_rules.py` enforces this.
    Reason: Missing history must not break DD's behaviour.
14. **Git commands run only through public functions in `dispatch/gitops.py`.**
    Tests are exempt.
    Reason: One Git boundary keeps safety checks consistent.
15. **No module calls another module's private (underscore) functions or
    reads its private names.** If something is needed, add a public function.
    Tests are exempt.
    Reason: Public interfaces let modules change their internals safely.
16. **New behaviour goes in the module whose job it is.** A file that would
    gain a second, unrelated responsibility gets a new module instead.
    Reason: Focused modules keep changes easier to understand and maintain.
17. **New material in `ARCHITECTURE.md` goes into the existing numbered
    section it belongs to.** Outdated text is replaced, not appended to.
    Reason: A single current account avoids conflicting design guidance.
18. **Any rule a change bends is listed under "Decisions you may want to
    revisit" in the summary.**
    Reason: Explicit exceptions let reviewers assess the tradeoff.
19. **"DD" names the system and "Orchestrator" names only DD's Plane
    account,** in docs, messages and comments. Code identifiers keep their
    existing names.
    Reason: One name per thing stops the system and its account being confused.
20. **No personal names, home paths, private addresses or employer references
    in the repository, except entries in `ALLOWED_EXCEPTIONS` in
    `tests/test_personal_details.py`.**
    Reason: The guard keeps future changes from undoing the clean-up.
21. **No function in `dispatch/` or `main.py` is longer than 120 lines.**
    Split a growing function into named steps.
    Reason: Short functions keep the run lifecycle readable.
22. **`dispatch/` modules import each other in one direction only.** A helper
    that several modules share moves to a small module of its own. A guard in
    `tests/test_design_rules.py` fails on any import loop and names it.
    Reason: One-way dependencies keep modules easy to change and add to.

# Story template

Every story the agent works on uses this shape. Copy the part below the line
into the Plane story's description. The title and description are all the
agent sees, along with `DEFINITION_OF_DONE.md`, so anything it needs to know
goes here.

## Ready check

Before a story moves to Sprint Backlog:

- **Small.** Its pull request can be read properly in one sitting.
- **Testable.** Each acceptance criterion can be checked by reading the pull
  request or running the tests.
- **No open questions.** Anything that needs a decision has been decided.
- **Agent work only.** No step needs a person: no Pi setup, no secrets, no
  Plane or GitHub settings. Those stay out of this project, because every
  story moved to In Progress here goes to the agent.

## Title

A short action: "Add a retry limit to board updates", not "Board updates".

---

**User story**
As a <who>, I want <what>, so that <why>.

**Acceptance criteria**
- [ ] <something a reviewer can check>
- [ ] <another>

**In scope**
- <files or areas the agent may change>

**Out of scope**
- <things the agent must not do, even if they look related>

**Notes** (optional)
<context, links, gotchas, how you'd approach it>

## Sending a story back for rework

The agent only reads the story's description, so review comments left
anywhere else never reach it. Paste your findings at the bottom of the
description using the block below, then move the story from In Review back to
In Progress.

---

**Rework (round 1)**
The pull request is already open on this branch. Keep the work that's there and fix only what's listed below.

**Fix**
- [ ] <what's wrong> →

**Keep**
-

**Don't**
-

---

If it comes back again, add "Rework (round 2)" below round 1 rather than
replacing it, so the agent sees the whole history.

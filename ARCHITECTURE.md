# Dynamic Dispatch — Architecture

**Status:** Source of truth. Update this when a decision changes.
**Last updated:** 2026-10-07

## 1. What this is

Dynamic Dispatch is a self-hosted, SDLC-driven system for orchestrating coding agents: backlog → sprint → isolated branch → PR → human review → merge. Agents are selected at runtime through config. Success means a story posted in the morning produces a reviewable PR that evening without supervision.

## 2. Who it is for, and why

**One user: the person who runs it**, possibly collaborators later. Do no work for hypothetical users, but avoid choices that would be costly to undo. Independent worktrees let work happen while the approver is busy or asleep.

**Code-review literacy is a goal alongside throughput.** Small stories make review manageable; one human, the approver, approves every PR and owns the consequences. When AI review arrives, read the PR before its findings, then compare what each caught, missed or misread.

## 3. Principles

- **The tools change, the process doesn't.**
- **One state, one unambiguous job.** If a column's meaning depends on context, it is two columns.
- **The human is the only approver.** No agent ever gates a merge.
- **An agent gets its own model credential and nothing else.** No Plane tokens, no GitHub token, no config, no inherited environment. That extends to anything the agent can influence, including code DD later runs on its behalf. See §10.
- **Nothing reaches a real branch without a human.** Isolation is structural, not procedural.

---

## 4. Requirements

### R1 — Agent-agnostic

Workflow code never branches on agent name. Dispatch selects an agent from config: display name, command template, output parser, Plane token, Unix account and git author. `dispatch/agents.py` exposes `run(cfg, agent_cfg, prompt, worktree)` and normalises results; provider-specific output belongs in parsers, not workflow decisions.

This supports using different model providers and keeping their billing separate across independent installs.

### R2 — Multi-install

Installs on different machines are independent, with no shared state. Repo and worktree roots are configured per install; nothing assumes a single always-on host. Cloud deployment, container or storage abstraction, and a Pi-specific portability audit are not requirements. Avoid new single-machine assumptions where an equally simple alternative exists.

## 5. Core model

**Agent type** — `claude-code`, `codex`, `gemini-cli`. A config entry with an invocation template and an output parser.

**Instance** — one dispatch in a fresh process, with its own worktree and story branch. Accounts and permissions enforce isolation (§10); recovery explicitly supplies prior work (§9).

Concurrency is N instances, potentially of the same type, using distinct agent accounts (§9).

**Run** — one execution of one story by one instance. Durable record: run id, story, agent type, repo, branch, worktree path, status, timestamps, result, token usage.

**Project** — a config entry: repo, worktree root, board mapping, conventions, and which agents are permitted to work it.

**Identity** — who the board sees acting. See §10.

---

## 6. The four seams

These boundaries make later features additive:

1. **Agent adapter:** config selects the agent; parsers normalise its output.
2. **Durable run queue:** workers claim rows, reserve one slot per agent account and mark outcomes. Each worker has its own SQLite connection. The queue supports concurrency, recovery, rework and metrics.
3. **Project as config:** repo, board mapping and allowed agents belong to each project.
4. **Distinct identities:** DD (as the Orchestrator account) and agents are separate actors (§10).

### Code boundaries and tests

- All database access goes through public functions in the `dispatch/runs/` package, which has one module per area (connection, schema and migrations; runs; agent slots and waits; pull request lights; main health; self-update; small tables; events) and exposes them all as `runs.<name>`. Events are best-effort history; state DD relies on must also have durable store records.
- All Git commands go through public functions in `dispatch/gitops.py`.
- Production modules neither call other modules' private functions nor read private names; tests are exempt.
- Put behaviour in the module responsible for it. Add a module when a file would acquire an unrelated responsibility.
- Modules in `dispatch/` import each other in one direction only, with no import loops. A helper that several modules share gets a small module of its own rather than living in one of its callers: `dispatch/stories.py` names a story (`story_ref`), `dispatch/prompts.py` builds an agent's story prompt, `dispatch/verify.py` runs the verify command and names the failing tests, and `dispatch/logins.py` takes the login results that API clients, Git pushes and agent runs report, passing them to the observer `alerts.py` registers (see Login health alerts, §9).
- Only the `dispatch/runs/` package reads the events table. Decisions come from DD's other tables, never from event history.

Tests are split by topic in `tests/test_*.py`, with shared fixtures and fake services in `tests/helpers.py`. `tests/test_design_rules.py` guards the database and private-name boundaries, function length, import loops and event reads across `main.py` and `dispatch/`; the other rules are checked at review. Tests use fake services and secrets, never real Plane, GitHub or model providers. The full suite is `python3 -m unittest discover -s tests`, and must also pass with `-W error::ResourceWarning`.

## 7. Scope

The working loop is board-driven dispatch, isolated worktree and story branch, sandboxed agent and verification, DD's commit/push/PR creation, and human review and merge. Failures surface on the story in Blocked.

The initial Claude Code MVP (`--model opus`, following latest Opus) has expanded to Claude, Codex and Gemini CLI, concurrent accounts, same-branch rework, PR refreshes, status lights and optional notifications. AI peer review, richer rework context, multiple-project rollout, auto-merge, deployment and a QA gate remain deferred (§11).

## 8. Workflow and board

```
Product Backlog → Sprint Backlog → In Progress → In Review → Done
                                        ↓
                                     Blocked
```

| State | Plane group | Job | Moved by |
|---|---|---|---|
| Product Backlog | Backlog | Scoped, not committed to a sprint | Human |
| Sprint Backlog | Unstarted | Committed this sprint, not started | DD for Cycle members, or human |
| In Progress | Started | Dispatched; an agent is working it | DD for approved Cycle starts, or human in; DD out |
| In Review | Started | PR open, waiting on the approver | DD in, human out |
| Done | Completed | Merged, meets Definition of Done | DD on merge, or human |
| Blocked | Started | Dispatch failed; error on the item | DD |
| Cancelled | Cancelled | Decided against | Human |

**Every story follows `STORY_TEMPLATE.md`**: a user story, acceptance criteria, what is in scope and what is out, plus a ready check before it enters Sprint Backlog. The title, description and Definition of Done form the task prompt; recovery and repair add context (§9). Plane CE has no story templates (a Pro feature), so a copy of the fill-in part lives on a Plane Page for pasting; the repo file is the official one.

**Blocked sits in the Started group** so stalled work counts against the sprint instead of quietly vanishing from the burndown.

**The code matches on Plane state IDs, never display names.** Columns can then be renamed freely.

**`In Progress` is the single dispatch trigger** — fresh work and rework both. Acceptable because dispatch can distinguish them by branch existence.

After confirming In Progress, DD replaces the story's assignees with its chosen agent alone, including on usage-limit handoff. Waiting and finished stories retain their last assignee; refreshes leave it alone. At the first Plane client setup per DD start, each agent token looks up `/api/v1/users/me/`. Success is cached; a 404 warns once and disables assignment for that agent until restart; other failures retry when assignment is next needed. Lookups and writes use one ordered executor so slow Plane calls neither delay execution nor let an old assignment overtake a handoff. Failures warn without deferring work. These best-effort display updates are not persisted across shutdown and never affect selection.

The first run that starts work sets Plane `start_date` to the DD machine's local date (YYYY-MM-DD), overwriting a hand-set date. It is a separate request on the assignment queue, so rejection (such as a date after the Due Date) cannot prevent assignment. Success is saved in `start_dates` and never repeated by rework, repair, retry, handoff or refresh. Failure warns and retries on the next story run.

Optional `agent_label_id` names an existing Plane "Agent" label. Build/rework adds it using labels from the state check, preserving existing labels and skipping the write if already present. DD never removes it; refreshes leave labels alone. Its separate request on the assignment executor cannot prevent assignment or Start Date updates; failures warn and work continues. Omit the setting to leave labels untouched.

**A merged pull request moves its story to Done.** With optional `states.done` set, the refresh check (§9) also looks for merges, using the story list it already reads, so it adds no Plane requests. GitHub is asked only about the latest pull request DD recorded for a story still in In Progress, In Review or Blocked with no active run, and only until that pull request is settled. A merged one moves the story to Done and the Orchestrator posts one marked comment, "Pull request #<n> was merged, so this story is now Done.", with one silent Bark alert. One closed without merging is left alone, with no state change or comment. Settled pull requests are kept in the store's `settled_prs` table, so GitHub isn't asked about them again. Stories anywhere else (Done, Cancelled, the backlogs) are a human's choice and are left alone; Cancelled has no configured ID, so DD can only recognise its own columns. The comment goes up before the state change, so its marker stops a retry posting it twice. A Plane or GitHub error records a warning event and the next check retries; nothing is failed or blocked. Like the rest of the refresh check, it pauses while main is red. Without `states.done`, Done stays a human move.

**A "Human" label keeps a story away from the agent.** If a project sets `human_only_label_id`, stories carrying that Plane label are never queued, so the approver's own tasks can sit in In Progress on the same board. The skip is logged once per story, not every poll.

**QA is not a column yet.** With nothing deployed to click through it would be a state with no job, which violates the one-state-one-job principle. It returns when auto-deploy exists.

### Sprint-pull (Cycles)

Sprint-pull is off unless a project has a `cycles` section in `config.json`. Once per poll, `dispatch/cycles.py` reads that project's Cycles and each unfinished current or upcoming Cycle's work items once through the paged Plane client. The Cycle list's dates determine eligibility: Cycles missing either date or ending before today in local time are ignored before reading their members. The end date is a normal working day, including membership reconciliation and approved story starts. In current and upcoming Cycles, DD moves members from Product Backlog to Sprint Backlog, leaving other columns alone, and removes the configured Carried Over label while preserving other labels. A human-only member receives one marked comment explaining that human-only stories are done outside Cycles, returns from Sprint Backlog to Product Backlog if needed, and is removed from the Cycle; stories already in other columns stay there.

The store's `cycle_members` table records membership by project, Cycle and story before writes. A member subsequently absent from an eligible Cycle returns to Product Backlog only if it is still in Sprint Backlog and is not in another eligible Cycle. Only removed members need an individual story read. Membership survives restarts and is forgotten only after removal reconciliation succeeds. Plane failures record a warning and retry next poll; marked comments prevent duplicate human-only notices after a failed removal or lost comment response. Every column move and human-only Cycle removal gets an event. Events are history, never reconciliation state.

**End of a Cycle.** `dispatch/cycle_dates.py` is the shared parser for all Cycle boundaries: timestamps retain their exact moment, while plain dates mean the start or end of that day in DD's local time. On the first poll after a Cycle's end moment, `dispatch/cycles.py` carries over what the Cycle never started, before reconciling the eligible Cycles. Only ended Cycles with remembered members are read. Every member still in Sprint Backlog (plan items and human-only stories aside) is first saved to the store's `carry_overs` table, then gets one comment, "Carried over from <Cycle name>; not started.", carrying `[dd:carried-over <cycle id>]`, the Carried Over label added to its other labels, a move to Product Backlog and, last, removal from the Cycle, with a `cycle_carry_over` event. Each step is checked against Plane first (the marker, the label list, the column), so a retry after a failure, lost response or restart repeats none of them; a story no longer in the Cycle has had every step, and one someone has started meanwhile is dropped from the carry-over and stays. Once every saved story is finished, the Cycle's `cycle_members` rows are forgotten, so the carry-over never reads it again. Carry-over is outside the plan's scope comparison: it never reopens approval, creates a plan item or requests an assessment. Stories In Progress, In Review or Blocked stay in the Cycle and finish normally. Once the end date has passed, every remaining story is Done and any pending carry-over writes have finished, DD records the approved Cycle as `complete` in `cycle_plans`. Completion survives restarts and is terminal: reconciliation, starts, reminders and active-Cycle checks never read that Cycle's members again, while a Cycle whose stories finish early stays open to membership changes through its end date. Finished Cycles therefore add no Plane requests to later checks. A carry-over failure records a warning and retries next poll without stopping reconciliation.

Config checks the `cycles` settings (`product_backlog_state`, `sprint_backlog_state`, `carried_over_label_id`, `max_stories` default 10, `replan_quiet_minutes` default 15, `blocked_reminder_minutes` default 60) and requires `plane.approver_user_id` once any project has them. The doctor checks those states, the label, the approver's project membership and `states.done`.

**Starting approved stories.** Once per poll, after main checks and existing In Progress work have reserved their accounts, `dispatch/sprint.py` reads the approved Cycle's `runs.plan_approval` and that immutable round's report. From the Cycle's exact start moment through its exact end moment, it scans the approved order, skipping stories whose dependencies are not all Done or whose overlaps are In Progress or In Review. A Blocked dependency holds only the stories that need it; independent stories carry on. Only current members still in Sprint Backlog can start, and changed membership waits for reconciliation and re-approval. Human-only work and plan items are never started.

Each selected story moves to In Progress, records a `sprint_start` event, and enters the ordinary build queue unchanged. The queue reserves its chosen agent before another story is selected, including across Cycles and projects; busy, cooling-down and main-check-held accounts are unavailable. Red main, an unfinished re-plan approval, or a pending DD update prevents new Cycle starts. Selection has no in-memory progress cursor: after a restart the durable approval, queue and current board determine what remains. If DD stops after the Plane move but before queueing, ordinary In Progress polling recovers the story before sprint selection. Plane failures stop that project's selection until the next poll.

**Active Cycle and held work.** `sprint.active_cycle` names the project's active Cycle, if any: a started Cycle whose plan has been approved (a re-plan in progress keeps it approved) and which still has a story outside Done, even after its end date. Before reading members it excludes durably complete Cycles and narrows the Cycle list to started Cycles with a recorded approval, so an overrunning approved Cycle is read and an unapproved past Cycle's members never are. The assess refusal reuses it, so the next Cycle waits for one that has run past its end date. Reconciliation passes its already-read Cycle list to assess checks; sprint polling shares one Cycle list between reminders and starts.

While a Cycle is active, ordinary In Progress queueing in `main.py` makes one call, `sprint.held_stories`, and skips what it returns. A story outside the Cycle stays in In Progress and gets one comment, "A Cycle is active. This story is queued and will start once the Cycle's work has finished and been merged.", carrying a `[dd:cycle-hold <cycle id>]` marker and recorded in the store's `sprint_holds` table so neither a restart nor a lost reply repeats it. Once every Cycle story is Done or Cancelled, the next poll queues it like any other story. Never held: Fix main; the Cycle's own stories, including rework moved back to In Progress; work DD queued, or began waiting for an agent for, before the Cycle's first approval (`plan_approvals.approved_at`; approvals saved before that column existed count from the upgrade); stories with an active run; and pull request refreshes, which never pass through this queue. Human-only stories are skipped before the check, so they never get the comment. The Cycle is read only when some story could be held. If that read fails, nothing new starts in that project until the next poll. Without an active Cycle, queueing is unchanged.

**Early completion.** The sprint poll shortens an approved, started Cycle only when every story is Done or Cancelled, no active run or worker slot belongs to its membership (including approved and remembered members) or assessments, main is green at the current GitHub main revision read after inspecting the finished stories, and no Fix main story is open. Cancellation finishes work but never satisfies a dependency; its unstarted dependents follow ordinary end-date carry-over. The end becomes yesterday's local end of day, clamped to the local start day, serialized as UTC. Ended and unapproved Cycle dates are untouched. `early_cycle_ends` stores the intended timestamp before PATCH and claims the silent alert once after success; a matching Plane timestamp recovers a lost response even after the new end has passed. Plane refusal records a warning and retries on later polls. Existing terminal completion remains after the end date and pending carry-over writes.

**Blocked reminders.** Each sprint poll, even while main is red, `dispatch/sprint.py` finds the Blocked stories of started, approved Cycles and repeats each one's Blocked alert every `cycles.blocked_reminder_minutes` with `alerts.blocked_sound`, until it leaves Blocked. The store's `blocked_reminders` table keeps each story's last alert time and message: a failed run's Blocked alert records its own, so the first reminder repeats that alert one interval later, and a story moved to Blocked by hand starts its clock when DD first sees it there and is reminded with "This Cycle story is still Blocked." Because the clock is stored, a restart sends no extra reminders. Leaving Blocked forgets the clock. A pending DD update pauses reminders along with the rest of sprint polling.

**The plan item.** `dispatch/plans.py` runs each Cycle's plan item after `dispatch/cycles.py` has synced the columns. When an eligible Cycle with no plan item gets its first story (human-only members don't count), DD creates one work item named "Plan: <Cycle name>" in Sprint Backlog, adds it to the Cycle and comments "Add the stories for this Cycle. When it's ready, reply '@Orchestrator assess'." That first comment also lists, by name and Cycle, the carried-over stories no earlier plan item has listed, so they can be added first. They are claimed for this Cycle in `carry_overs` just before posting, so a retry after a lost reply lists the same ones, and each is listed on one plan item only. The store's `cycle_plans` table keeps the item's ID and status (`new` until it is in its Cycle and has asked, then `waiting`, `assessing`, `planned` once the plan report is posted, `approved` once approval has been copied to the Cycle and the item deleted, and `complete` once its end date has passed and all remaining stories are Done or Cancelled; `approving` and `reopening` persist unfinished transitions). The item is never a story: reconciliation leaves it out of membership, column moves, label changes and story counts, and DD never builds it, even from In Progress. Before creating one, DD reads the project's work items (only then) and looks for an item with exactly that name, made by the Orchestrator account and not already another Cycle's plan item, so if the create response was lost the next poll takes that item up, saves it, adds it to the Cycle if needed and asks as usual instead of making a second one.

The approver talks to DD by replying on the plan item. A reply counts only if `plane.approver_user_id` wrote it, it @mentions the Orchestrator account (the Orchestrator's user ID is read at most once per poll, only while a plan item exists or is being made), and it was created after DD's latest question there (a comment carrying `[dd:plan <cycle id>]`: the request for stories, then the "OK to begin as scoped?" question posted with each plan report); creation time decides, so editing an old comment changes nothing. The first word after the mention is the command: `assess`, `approve` or `reject` (a reject must give reasons after a colon). Anything else gets "I didn't understand. Start with assess, approve or reject." Every approver comment since that question is judged once, by comment ID, oldest first, even if DD has replied to an earlier one in between. DD explicitly sorts replies by creation time, independently of Plane's listing order (including newest-first listings).

- **assess** is refused, with a reply saying why, while another eligible Cycle is `assessing` or `planned`, or is `approved` and still has stories outside `states.done` and `states.cancelled` (its plan item doesn't count), or while `sprint.active_cycle` names another Cycle, which covers a Cycle past its end date until its last story is Done or Cancelled; a reply about an unfinished Cycle names its open stories. Cycles that are only being prepared never block each other. It is also refused while this Cycle has more than `max_stories` stories (the reply says how many to remove). Otherwise the status becomes `assessing`. An `assess` while the latest round still has work running or awaiting retry is ignored with an already-running reply. Otherwise it starts a new numbered round with fresh inputs. If the latest round has exhausted agents and the eligible story IDs, names and descriptions are unchanged (regardless of listing order), it instead retries only those agents in that round, preserving its successful answers.
- **approve** and **reject** before the status is `planned` get a reply that there is no plan yet. An **approve** while the current report has a dependency loop gets a reply that the loop must be broken first. Accepted approval atomically saves the command, the approval comment (including its author, ID and creation time), a reference to the immutable round's merged result and report, the round's full membership, and status `approving`. DD copies the report and approval comment into the Cycle description, deletes only that Cycle's recorded plan item, then sets `approved`. `plan_approvals` retains this history and completion flags. The deletion guard requires both the current Cycle record and its approval to name that exact item. Before retrying an unfinished write, DD reads the Cycle description or the work item: a matching description or a missing item completes the step without repeating it. Plane failures leave the transition pending across restarts.
- **reject: <reasons>** saves the feedback and a request for a fresh round atomically with status `assessing`. A rejection without reasons receives a marked reply asking for them and leaves the plan unchanged. Feedback is copied into immutable `snapshot_feedback` for every assessor's prompt; agents still never see another agent's answer. The new report and question use the same plan item.

After approval and before durable completion, adding or removing a story immediately invalidates the plan. End-of-Cycle carry-over is excluded from this scope check. DD saves `reopening` and the assessment request before creating a replacement item, recovering lost create responses as for the first item. The next round includes only stories outside In Progress, In Review, Blocked and Done. `snapshot_members` retains the full membership separately from those assessment inputs, so normal progress does not trigger another round. Historical plan-item IDs remain excluded from stories. While reapproval is pending, the queue's claim predicate holds Cycle stories that have never been claimed; previously started work and assessment runs can continue. Held work does not reserve agent accounts needed by assessors. The sprint starter also waits for the replacement plan to be approved before moving any more stories into In Progress.

Each command counts once: the store's `plan_commands` table saves it by comment ID, together with the reply it is owed and any status it sets, before anything is posted, so a restart neither judges it again nor loses it. Each reply ends with a marker naming the comment it answers (`[dd:plan-reply <comment id>]`, and `[dd:plan <cycle id>]` on the first comment), so a retry after a lost response never posts it twice.

**Independent assessments.** `dispatch/assessments.py` owns assessment execution, separately from the plan conversation. Each Cycle's latest numbered round is current. Accepted requests are saved atomically with the command in `assessment_requests`, so a restart before snapshot creation cannot lose them. At assessment start, `assessment_snapshots` records main's current commit from GitHub, Cycle start and end values and each eligible story's ID, name and description. Plan items and human-only stories are excluded. DD posts "Assessing this Cycle with <agent names>. I'll post the plan here when every assessment is in." with a snapshot marker before queueing one `plan` run per allowed agent. Each round has its own snapshot and agent list, which survive restarts and are never refreshed between assessors. Earlier rounds remain stored but their answers are never reused in new rounds. Each run claims the normal account slot and respects usage cool-downs. Usage limits release the slot and retry after cool-down without counting as failures.

Each assessor reads a fresh detached checkout at the recorded commit inside a folder accessible only to DD and its agent group. Files and directories are read-only for the agent. DD compares file contents, modes and symlinks after execution, records attempted changes that persist as a warning, and discards the checkout and its empty assessment container after each attempt. Container removal is non-recursive and leaves unexpected contents in place with a warning. No assessment answer is included in another agent's prompt. The prompt requests one fenced JSON block: every story's size (S/M/L), risk and reason, dependencies, file overlaps and confidence; a complete build order; and stories to defer with reasons if the Cycle cannot fit its end date. References use story IDs so duplicate titles are unambiguous. Validation checks complete, unique coverage, valid fields and Cycle-only references. An invalid answer receives its exact problems once; a second invalid answer fails that attempt.

The `assessments` table stores valid answers, failure counts and the current run separately from events. Failed or timed-out attempts retry on the next poll through the normal queue. After two failed attempts for an agent, DD posts a marked comment naming it and sends a Bark alert, then waits for the approver's next `assess`. Interrupted runs retry the same snapshot without counting a service restart as an agent failure. Start and failure comments recover from lost responses through their markers. Completed assessments leave the Cycle `assessing` until the plan report is posted.

**The plan report.** Once every allowed agent in the current round has a valid answer, `dispatch/plans.py` merges them mechanically, with no AI, using plain functions in `dispatch/plan_report.py`. Dependencies and overlaps are unions (an overlap counts from either side); each story's size and risk (with its reason) are shown per agent, side by side. The order is each story's average position across the build orders, ties keeping the Cycle's listing order, adjusted so a story is placed only once everything it depends on is. Stories that depend on each other, directly or through others, form a dependency loop: the report lists each loop first under "Needs your decision", naming which agent claimed each link, its stories are ordered by average position, and `approve` is refused while the current round has one. The report then lists disagreements: different sizes, risks or dependencies, and stories an agent wanted pushed.

The fit is DD's own rough estimate. The store's `done_stories` table records, when DD moves a story to Done after its merge, the time of the story's first run and of Done (an existing database seeds it once, the first time it opens, from past `moved_to_done` events). The estimate uses the project's latest 30 Done stories and the sizes in their newest saved plan reports, taking the larger size when agents disagreed. Each size with at least three finished stories uses its own median first-run-to-Done duration for the likely figure and nearest-rank 80th percentile for the slow figure. Sizes with fewer samples fall back to the overall median or 80th percentile, respectively, weighted S 0.5, M 1, L 2; unsized history contributes only to that fallback. The report names the sparse sizes. Each figure schedules stories in the existing build order onto the earliest available agent, waiting for dependencies and previously scheduled overlapping stories to finish. This changes only the estimate, not build order or execution. Finish counts from the later of now and the Cycle start moment. Both figures show a finish date and whether they fit the exact Cycle end moment; displayed dates use DD's local time. Existing assessment snapshots without a start value retain the estimate from now. With no history, both figures and their fit are explicitly unknown: there is no duration to apply the fallback weights to. The report explains the method in one short paragraph. If the likely finish passes the Cycle's end date, DD lists stories to push: lowest in the order first, never one that another Cycle story depends on, until the estimate fits, followed by the agents' own push suggestions under the same rule, with their reasons, and says when the rest would finish. The rule is checked against the whole Cycle's merged dependencies, not the stories still kept, so pushing a dependent story never frees what it needs to be pushed too, and the remaining finish still counts everything a kept story depends on.

The merged result, the estimate and the report text are saved together in `plan_reports` with their round before anything is posted, so a restart posts exactly the same report. DD checks the current description before writing the report as the plan item's description, comments "OK to begin as scoped?" carrying `[dd:plan <cycle id>]` and the round's `[dd:plan-report <snapshot id>]` (so replies are judged against this question and a retry never asks twice), sends an active Bark alert that opens the plan item, and sets the status to `planned`. If the Cycle's eligible stories (IDs, names, descriptions) change while the plan is `planned`, `plan_changes` keeps a fingerprint of them and when it was first seen; another change restarts the wait and a revert clears it. Once they have stayed unchanged for `replan_quiet_minutes`, the request for a new round and the status `assessing` are saved together, and the new round's report and question follow as usual.

### Definition of Done

Lives in the repo as `DEFINITION_OF_DONE.md`, one per project. DD reads it from the base branch, never from a worktree, and includes it in every prompt. It is what the agent builds toward and what the human checks at the gate.

A Plane Page was the first choice, since that's where it would be seen. Plane 1.4.2 doesn't expose Pages through its API (it returns 404), so DD couldn't read one. A side benefit of the repo file: an agent can only change it through a PR that a human merges. Revisit if a later Plane release adds the Pages API.

---

## 9. Execution model

- **One run per agent account at a time.** Worker threads let different accounts run simultaneously.
- **Worktree per run, branch per story.** Rework reuses the existing branch. Stale worktree registrations are pruned first, so a deleted worktree directory doesn't wedge the branch. ("branch already exists" was a repeated real failure in the prior build.)
- **Worktree cleanup.** A successful worktree is removed once its story reaches In Review. A failed one is kept so it can be inspected or retried. If `failed_worktree_days` is set, a failed run's worktree is removed once per cycle after the run has been closed that many days, as long as its branch has no newer run and no active run and the worktree has no uncommitted changes (a dirty one is left in place and warned about, never force-removed). Only the directory goes; the branch and its commits are kept, and nothing is deleted on GitHub. Each removal is logged; a folder that can't be removed is warned about once and retried each cycle. Without the setting, failed worktrees are removed by hand (`git worktree remove --force <path>`). The same per-cycle clean-up, whatever the setting, removes empty `plan-<snapshot>-<account>` assessment containers that a restart left behind, unless an assessment's run is running or its worker still holds the agent's slot (runs are claimed on the main thread before their workers start, and a worker releases its slot only after discarding its folder). Removal is non-recursive and matches only that name pattern, so story folders, the main-check folder and symlinks are never touched; a container with contents is left in place and warned about once. Each removal is recorded as an event.

### Choosing an agent

DD chooses which agent builds each story; nobody picks a model by hand. Each project lists the agents allowed to build its stories (`agents` in its config, in order of preference). A project that still names a single `default_agent` is treated as a list of one.

Selection lives in `dispatch/choose.py`. An available agent has no cooldown and no queued or executing run; queued runs reserve account slots. While a main check waits for an account, that project's allowed accounts are also held from new work (see Checking main, below).

- **Rework goes back to its builder.** If the story has run before, it goes to the agent its latest run was given, as long as that agent is still allowed and available. That keeps a story with the agent whose work is on its branch.
- **A new story goes to the least-used agent:** the available agent with the fewest runs queued in the last 7 days. A tie goes to the first agent in the project's list.
- **An agent that hits its usage limit cools down.** Its parser reports the limit as a standard result (`usage_limit`), so recognising an agent's wording is the parser's job and nothing else depends on it. The parser also reports `usage_limit_until` as a UTC time when available. The agent is skipped until that reset time plus two minutes. Missing or past times, or times more than seven days away, fall back to `usage_limit_cooldown_minutes` (default 60). The cooldown event records whether the time came from the agent or the fallback. Cool-downs are saved in the run database, so a restart doesn't forget them.
- **A queued run whose agent has since hit its limit** (several stories were queued in one poll, say) is given to another available agent before it starts. If none is available it is cancelled, and a later poll queues the story again.
- **If every allowed agent is cooling down, a story that hasn't started isn't started.** DD logs that once and tries again on each later poll.

The chosen agent is recorded on the run, and the log names the agent a story was queued for and the agent that built it.

### When an agent hits its usage limit

A story whose agent hits its usage limit, while building or while repairing a clash or failing tests, stays In Progress; nobody has to move it back. The run closes as `usage_limited` (not `cancelled`, which means a human pulled the story out) and the agent's cool-down starts. Then DD hands the story on straight away:

- **Another allowed agent is available:** queue its run in the same cycle, recording the previous agent. After confirming In Progress and setting up the folder, post one Orchestrator comment naming the usage-limited agent and its replacement. If rebuilding for a different group saved unfinished changes, also name their local leftover branch.
- **Every allowed agent is cooling down:** keep the story In Progress and persist the stopped run, story, earliest available agent and reset time. Post one comment explaining the automatic wait, agent and local 24-hour resume time plus minutes remaining. A stopped-run marker prevents duplicates; failed comments retry next poll. Poll until an agent is available, then select normally (preferring the builder) and end the wait. Busy accounts use the separate slot-wait message (Concurrency, below).
- **A waiting story moved out of In Progress:** drop the wait without retries or further comments. Moving it back starts normally.
- **Three consecutive usage-limit stops:** move the story to Blocked with the reason and instructions to move it back when agents have usage. Any other finished outcome ends the streak; cancelled runs and refreshes are skipped.

Handoffs keep the story branch. A folder belonging to another agent group is rebuilt under the leftover-branch rules in §10.

Half-done work is handed on, not thrown away. When a run is about to start the agent and the story's folder holds uncommitted changes from an earlier run (stopped by a usage limit, a failure, a restart, or the story being moved out), DD adds a note to the prompt: which agent made them, that they were never tested, every changed file (added, changed or deleted), and the changes themselves. The agent is told to build on them if they're sound, or undo them and start over if not. When the folder was rebuilt for a different agent's group, DD first puts the leftover branch's changes back into the new folder, still uncommitted (new and deleted files included) and writable for the new agent, then adds the same note. It only does this while the story branch is where the leftover was saved from; otherwise, the leftover is kept but not restored. The changes in the note are scrubbed of secrets, then cut at 50 KB; a cut note says so and still lists every file. A clean folder gets no note. Only uncommitted changes count: saved work is handled as below, and a run that resumes or retries its pull request never starts the agent, so it isn't told. Events record `unfinished_found`, `unfinished_restored` and `unfinished_in_prompt`.

Work that was already committed (a limit during a repair, say) resumes and combines as described below, because a `usage_limited` run is not a human pull-out.

Refreshes of open pull requests are unchanged: they wait for their builder and never switch (see below). Events record each step: `usage_limit`, `switched`, `waiting`, `resumed_after_wait`, `wait_dropped` and `gave_up`.

### Outcome reconciliation

When the board is updated afterwards, the state is read immediately before it is written, so a human's move is almost always respected. It is not guaranteed: if a human moves the story in the fraction of a second between that read and the write, the write wins. Plane has no conditional update to close that gap.

Each outcome comment ends with a marker, `[Dynamic Dispatch run N]`. Before posting, DD lists the story's comments and skips the post if the marker is already there, so a post whose reply was lost is not duplicated on retry. If this Plane can't list comments (404 or 405), it posts without checking. The pull request comes first in the In Review comment, as a clickable link outside the truncated summary, so a long summary can't cut it off. Only the address GitHub returned is linked, and only when it has the form `https://github.com/<owner>/<repo>/pull/<number>`. The link opens the pull request's Files changed tab (the address plus `/files`), where reviews and approvals happen; anything else is shown as a plain `Pull request: <address>` line. Nothing in the agent's summary is ever turned into a link.

A 404 while updating the board closes the run as "story deleted" only if the Orchestrator account's own lookup of the story also returns 404. A 404 on the agent account's request alone (lost access, say) leaves the run waiting and retrying, because closing it would free the story to be dispatched again.

### Run lifecycle

`queued → running → awaiting_review | awaiting_blocked → done | failed`, or `cancelled`, or `usage_limited` (the story stays In Progress and is handed on, above), or `interrupted` (a restart cut the run off and the story was picked back up, below). A finished agent can also wait in `held_main` while main is red, returning to `queued` when main passes.

A run stays *active* until Plane actually shows its outcome, and a story with an active run cannot be dispatched again. If the board update fails, only the update is retried, never the agent run. That closes the duplicate-dispatch bug found in review.

A run cut off by a restart, crash or power cut is picked back up, so the Pi can be updated without first checking that nothing is In Progress. Startup closes leftover `running` rows as `interrupted` and clears their account slots. First DD reaps each affected sandbox user and cancels unfinished combinations with main. Failure warns; the next run's own reap and merge checks still stop it safely. Normal selection then retries with the builder, a replacement or an In Progress wait. The retry checks the story's state: a human move while DD was down cancels it, making committed unpushed work count as pulled out (below). Once confirmed, one Orchestrator comment reports the restart and retry count, marked with the interrupted run to avoid duplicates.

Recovery preserves saved work: uncommitted edits get the unfinished-work prompt; committed unpushed work resumes without the agent; pushed work lacking a recorded PR uses the PR retry. Summaries explain the interruption. PR creation reuses an open PR for the branch, preventing duplicates after a crash. Only the retry reaching `awaiting_review` posts the In Review comment. Pushes are never forced.

A reused pull request with uploaded work (rework, or a resumed or retried run) gets the new run's description, so reviewers never read a summary that contradicts the code. DD first posts the current description as a pull request comment headed "Previous description, replaced by run N on <date and time>" in DD's local time with its zone abbreviation (for example `2026-10-01 14:05 CDT`), then replaces it with the new summary in the same format as a new pull request: summary, `---`, the `Story` and `Agent:` lines. Both are scrubbed and capped like a new pull request's body. The comment ends with a hidden marker, `<!-- Dynamic Dispatch run N: previous description -->`, added after truncation; DD lists the pull request's comments and skips the post if the marker is there, and leaves a description that already matches alone, so a retried run posts nothing twice. The description is only replaced once the comment is up. Any failure at either step, including a raw timeout or a malformed reply, records a warning event and leaves the description as it is; the story still goes to In Review. Refreshes never touch the description, since no agent changed the work.

An agent doing rework can hand a requested change back to the approver instead of making it. The rules live in `dispatch/rework.py`. Its `pull_request` function alone decides whether a run is rework, from DD's own records: an earlier run of the story recorded a pull request, that pull request is still open on GitHub, and the story branch exists locally with nothing unpushed. Refreshes, first builds, held work, resumed work and pull request retries are never rework. The check runs before the started alert and the assignment, so a retry repeats neither: if GitHub can't be reached it records a warning and cancels the run for the next poll to retry, like Plane being unreachable at the start, while a local Git error Blocks the story. Only a rework prompt carries the pull request link and the protocol: carry out every instruction it can; leave out a change that, after investigating, should not be made (already done, obsolete, contradicts the current code, or would make it wrong); and begin the final message with the exact line `DD-DECISION-NEEDED`, then give each instruction not carried out, why, and the evidence. After the declined instructions the agent still gives its usual complete summary, since that becomes the pull request description when there are changes. If the story says the approver has already seen the objection and still wants the change, the agent makes it.

The outcome is decided mechanically, never by reading the explanation. `rework.finish` only decides it and records a decision request, returning it to the worker, which starts the board update or Blocks the run; `rework` never imports `sync`, so the two have no import cycle (a design-rule test checks this). The marker counts only as the exact first line with a non-blank explanation after it.

- **Rework, nothing changed, valid marker: "needs your decision".** Nothing is tested, committed, combined or pushed, and the branch, pull request and description are left as they are. The explanation, without the marker line, is posted best-effort as a pull request comment ending in `<!-- Dynamic Dispatch run N: decision needed -->`, which is checked first so a retry never posts it twice; a failure records a warning. The agent's Plane comment gives the pull request link, then "No changes made. <agent> didn't carry out part of the rework and needs your decision:" and the explanation. The story goes to In Review through the normal board update, the run ends `done` and its work folder is removed, and the alert plays a sound, says "needs your decision" instead of "ready for review", and opens the pull request.
- **Rework with changes and a valid marker:** the normal path (verify, commit, combine, push, replace the description, with the marker line dropped from the summary), with the In Review comment and alert also saying "needs your decision".
- **Nothing changed otherwise:** Blocked with the usual "finished but changed nothing" message. Outside rework, a reply carrying the marker gets one more sentence saying that a decision request only applies to rework of an open pull request. Pulled-out work keeps its own exception (below).

The run row's `needs_decision`, `no_changes` and `decision_at` fields are saved in the same write that moves it to `awaiting_review`. A restart before that write runs the agent again, like any unfinished run; after it, only the board update is retried, and the comment markers and the single transition to `done` keep the comments and alert from repeating.

After `RESTART_LIMIT` (2) consecutive interruptions, Block the story with instructions to check the branch and move it back to In Progress. Count from run records; another finished outcome resets the streak and cancelled runs are skipped. Interrupted refreshes close without comments, do not count towards the streak and are queued by the next refresh check if latest main is still missing.

The worker checks that the story is still In Progress before starting and again before pushing. If a human has moved it, the worker stops. Nothing is pushed unless Plane positively confirms the story is still In Progress. If Plane can't be reached at that point, the story goes to Blocked and the work stays committed on its branch. The Blocked comment says to move the story back to In Progress to push it.

A story branch with commits absent from every origin ref resumes without running the agent: verify again, confirm In Progress, push and open/reuse the PR. DD commits verified work, except for local snapshots held while main is red; those are explicitly recorded and must pass verification after combining with passing main before upload. Failed re-verification Blocks without pushing. A branch with nothing unpushed, including ordinary rework or failure before commit, runs the agent.

If a human moves the story during execution, DD can cancel with verified work committed but unpushed. Each run records whether it committed; the latest earlier committing run decides recovery. If cancelled, run the agent with a note that the work was tested but never reviewed: check current criteria, change only what is needed, or report that it already meets them. Then verify, combine latest main, upload and create the PR; edits become commits on top. Uncommitted changes block this path. Create no new set-aside branches and leave existing ones alone. Failed, usage-limited or interrupted runs resume without the agent. Legacy rows lacking commit records count as never committed, so their unpushed work resumes.

For pushed work whose PR creation failed, the latest earlier run that pushed or opened a PR decides recovery. If it pushed without a PR and the local branch exists, skip agent, commit and push: verify, confirm In Progress and open/reuse the PR. A PR-only retry does not count as a push, allowing repeated retries after Blocked; the failure comment explains this. Before verification, read local refs without fetching to ensure the branch has commits absent from the last-fetched base. Otherwise Block with an empty-PR explanation on every pickup while the branch exists. Uncommitted changes also block. Additional unpushed commits take the resume path and get uploaded. Legacy rows without push records count as never pushed and run the agent again.

Combining with main and its repair loop live in `dispatch/combine.py`, which the worker calls once per run. Before uploading or retrying a pull request, DD fetches the latest base branch from origin. The agent's own work is still verified and committed first. If the branch already contains the fetched main, execution continues unchanged. Otherwise DD merges that exact fetched revision with `--no-commit --no-ff`, through the guarded Git runner (explicit trusted admin directory, disabled hooks and fsmonitor, refused filters). Configured external merge drivers are also refused, because worktree attributes could select a command running as DD. DD alone merges, commits and pushes; the agent is instructed not to change Git history.

The combined tree must pass the full configured verification command before DD commits the merge. A conflict leaves Git's markers in place. DD sends the same agent the original story, incoming pull request titles (for each "Merge pull request #N" commit on main's first parent, `combine.py` looks up #N's title on GitHub once, kept to one line and 200 characters; gitops stays offline and only reports the numbers. A failed lookup logs a warning and falls back to the commit's second line, or its first; other commits use their first line), the conflicting files and sections, and instructions to preserve both changes' intent and its earlier work. A clean merge that fails verification instead sends the failing output and asks the agent to adapt its change to main. After each repair DD scans only the files that clashed, without following symlinks, for remaining conflict markers (a standalone heading underline of equals signs is not a conflict) and runs verification only when none remain. Conflict and test repairs share two attempts per pickup, recorded on that run; a later pickup starts with two fresh attempts. Exhaustion goes to Blocked with the incoming titles and conflict sections or verification output. Failed work stays available for inspection until the next pickup. Before checking for saved work, DD cancels any unfinished merge through its safe Git runner, restores the last saved branch state, discards unfinished repair files, and records an event. The earlier Blocked comment retains the failure details. A usage limit during a repair starts the normal cool-down and hands the story on like any other usage limit; the next run resumes the committed work and repairs the combination afresh. Repair usage is added to the original run’s totals.

Each send-back posts an Orchestrator comment through board sync, marked with its run and attempt to prevent duplicates. Send-back comments name failing tests, or give their count when names are unavailable; full output stays in repair prompts and Blocked details. A failed send-back comment logs a warning event and does not stop repair. Combining, conflicts, verification failures, send-backs and successful combinations produce events. Successful review comments include “Combined with the latest main (<titles>) and re-tested.” This context is retained when a verified merge later resumes after a failed upload or pull request creation. PR retries normally skip pushing, but a newly verified merge must be uploaded. The final In Progress check still happens after verification and before upload. Open pull requests use the same combining and repair steps when main moves again, as described below.

### Refreshing open pull requests

Each cycle reads the story list once per project through the paged board client, then checks the latest successful pull requests recorded by DD. Only In Review stories are refresh candidates. The same list also drives the Blocked and In Progress lights and restores them when stories move on; other historical stories need no individual Plane or GitHub requests or events, apart from the merge check that moves a story to Done (§8). Only stories still In Review whose recorded GitHub pull request is open and unmerged qualify. DD reads the latest base SHA from GitHub once per project per cycle and checks whether the last uploaded branch contains it, through the guarded, read-only Git runner. An unknown SHA means main has moved beyond the locally fetched objects. The remote-tracking branch is used so a verified local merge whose upload was cancelled cannot be mistaken for an updated pull request. Already-current branches need no agent or tests.

A refresh has `kind=refresh` in the ordinary durable run queue, sharing its active-story uniqueness constraint and per-account worker slots. Older rows default to `story`. The builder is preferred using the same allowed-agent rules as rework; unlike ordinary rework, a builder cooling down makes the refresh wait rather than switch agents or Block the story. If the builder is no longer configured or allowed, normal rework selection supplies an available replacement. Availability is checked again when the queued refresh is claimed.

Refreshes keep the story In Review. The worker confirms the PR is still open, cancels any unfinished merge and recreates the worktree from the saved story branch. It skips initial story development and enters the finishing flow's existing combination, full verification and repair loop. Conflicts and test failures share the same two repair attempts and Orchestrator send-back comments. A usage limit during repair cancels the unfinished merge, starts the builder's cooldown and leaves the story waiting In Review for a later cycle. Exhausted repairs move it to Blocked with the usual details plus: “The pull request itself was left unchanged, so it still shows the version from before main moved.” The unfinished merge is cancelled.

Immediately before uploading, both Plane and GitHub must confirm that the story is still In Review and the recorded PR is still open and unmerged. A merged or closed PR or a moved story ends the refresh with a skip event and no upload. An unavailable service also prevents upload and allows a later retry. As with the existing Plane check, these are last-moment checks, not atomic locks against human changes after the checks.

On success, the verified branch is pushed to update the existing PR; no new PR is opened. DD posts one marked, retry-safe Orchestrator comment: “Main moved on (<incoming titles>). Combined this pull request with the latest main and re-tested: all tests pass. The pull request has been updated.” A retry of an already-saved combination uses the base branch name if incoming titles are unavailable. The state remains In Review and successful worktree cleanup runs as usual. Events cover starting, unchanged main, waiting, combining, repairs, testing, upload, and skipped refreshes. Repeated unchanged, skipped, and waiting polling results fold into one event with a repeat count and last-seen time. DD never merges into main.

### Checking main

DD tests main itself whenever it moves, so a broken base is noticed straight away rather than through the next story that builds on it. Each cycle reads main's latest commit from GitHub once per project, before queuing any new work, and the refresh check reuses it. In projects with a verify command, a commit other than the last one checked gets a **main check** (`dispatch/mainhealth.py`): a run of `kind=main_check` in the ordinary durable queue, keyed per project so only one is active at a time. It takes a free agent account's slot, because the verify command runs code from the repository and must run sandboxed in that account. It never starts an agent, gets no model credential, and doesn't count towards the agent's use when choosing agents. An account cooling down after its usage limit is preferred, since it can't build a story anyway. A pending main check has first claim on the next free account. Main checks are queued each cycle before new stories and refreshes, so a free account goes to the check. With none free, the check waits (a folded `main_check_waiting` event), and until it is queued every account allowed in its project is held from new work: `choose.py` treats them as unavailable to any new choice of agent (new stories, refreshes and hand-overs). So an account that comes free, even part way through a cycle, is left for the check, which takes it at the start of the next cycle. A run already queued or running keeps its account and is never interrupted. These account reservations are in memory and set afresh each cycle; the stop caused by a recorded red main is durable, as described below. Any error while queuing the check (reading main, say) releases them, so stories never wait for a check that can't be queued, and after a restart the next cycle's main check simply goes first. Stories that find no account because of a hold wait In Progress with the usual busy comment.

The check runs the project's verify command, with the story gate's time limit, on exactly that commit, in a detached checkout in `main-check` under the worktree root. Story folders are named after their branches, so none can take it. The folder is rebuilt from scratch for every test run with the same guards as a story's folder, so a second run never sees what the first left. If the tests fail they run once more on the same commit, and main is red only if both runs fail. A fail-then-pass records a warning naming the tests that failed once, and main stays green. A sandbox that can't be cleared before or after the tests says nothing about main: like a Git failure, it ends the check with a warning, and the check retries the commit at the pace described below. A restart during a check also leaves main unchecked and retries on the next cycle.

Main's state lives in the store's `main_health` table, one row per project: the commit last checked, green or red, since when, the failing tests, and the pull request titles (looked up on GitHub, falling back to the commit message) of what reached main since it was last green, which is what made it red. A first check that passes changes nothing. Turning red sends one active Bark alert, "Main is failing tests since <title>", naming up to five failing tests; a later passing commit sends one, "Main is passing again". While main is red, every new commit is checked the same way, and staying red sends nothing more. The row marks each change of state as owing an alert. A check claims the owed alert (clearing the mark) before handing it to the Bark delivery thread, so two callers can't both send it. If queuing it fails, or Bark then fails to deliver it to any configured device, the mark is set again and a warning event recorded, so the next check sends it again; with several devices, one that already received it gets it a second time. The mark is restored only if main is still in the state the alert describes: once main has changed state again, the newer alert replaces the stale one. An alert still on the in-memory delivery queue at shutdown is lost, as with every Bark alert. Main-check errors record warnings rather than failing a story.

The separate `main_check_failures` store table counts consecutive checks without a result per project, including folder preparation failures, sandbox refusals and errors queuing a check (such as being unable to read main from GitHub). Waiting for an account does not count. The count, retry deadline and alert markers survive restarts; main's last green/red state stays unchanged. The third failure owes one active Bark alert, "DD can't test main: <short reason>". From then on, checks retry at the first poll at least ten minutes after the preceding failure, even if main has moved, without repeating the alert. A result, green or red, resets the count and retry pace and owes "DD can test main again" if the outage alert was handed to Bark. After an outage, even an unchanged commit is tested to establish recovery. Availability alerts use the same claim-before-delivery and re-owe-on-failure pattern as result alerts, and delivery is retried each poll even during the ten-minute delay. A generation marker prevents a late failed delivery from restoring an alert from an earlier outage or recovery. These failures add no story hold or failure.

Turning red also creates one Plane story in In Progress, using the Orchestrator token: "Fix main: tests failing since <the merge's pull request title>". Its description follows `STORY_TEMPLATE.md`: restore passing tests without weakening what they check (explain any test that is itself wrong), pass the full verify command normally and in strict mode, and include the failing names, scrubbed output capped at 12,000 characters, breaking commit, and available pull request links. The story ID, last reported revision, creation-failure alert marker, output, and breaking-merge details live with main's health in `main_health`, surviving restarts. The story is created after the red alert is handed to Bark, so a slow Plane never delays the alert. Health changes and fix-story writes are serialized, so a fix story from one red episode can't be recorded against the next. Creation puts a stable episode marker in the description; before retrying, DD looks on the board for a story whose creation succeeded but whose response was lost. A later red commit adds a marked, retry-safe comment with the new failures to the same story. Plane failures never fail a main check: they record a warning and retry on the next poll, and a creation failure also sends one active Bark alert per episode.

A recorded red main stops the line for that project, except for its recorded fix story: no other new stories or refreshes are queued, and other queued work cannot be claimed. Polling reads the board only for the fix story, leaving every other story's waits and warnings as they were. The fix story reserves its agent's account normally and is exempt at queueing, claiming, worker start, hand-over, restart recovery, refresh, and every hold before combining or uploading. It follows ordinary verification, combining and repairs, pull request creation, lights, cross-agent review, approval, and the approver's manual merge. If it becomes Blocked, the normal alert applies and main remains red. Main checks still run, including when a stopped queue had reserved the only account. An agent already working on another story finishes; DD saves its changes locally and holds before combining with main, starting a repair, or uploading a pull request. Verification of held work waits until main passes, so a broken base cannot fail that story's verify gate. The story stays In Progress with one Orchestrator comment: "Held: main is broken. This resumes automatically when main passes again."

A held run stays active as `held_main`, with its worktree, agent summary and announcement marker stored in the run row, not events. It releases its account for main checks. Saving or announcing a hold is retried with a warning on error; the comment uses a stable marker to prevent duplicates. When main passes, the next poll queues the same held run, combines its saved work with passing main without rerunning its builder, verifies the result, and continues uploading. Waiting stories and refreshes are eligible again too. Whether the fix story or a manual fix made main pass, DD comments on an open fix story that the line has resumed and leaves its state for the approver to close. That comment retries on Plane errors without delaying the line. The next red episode gets a new fix story. Persisted health gates queue claims after restart, and held runs survive startup without counting towards the repeated-restart limit.

### Updating DD itself

With the optional `self_update` setting naming the project whose repository is DD itself, DD updates and restarts itself after a merge to its own code (`dispatch/selfupdate.py`). Without it, DD never updates itself. DD's own checkout, the folder it runs from, is separate from that project's repository, where agents work. Each cycle, after the main check, DD compares the checkout with main. An update is pending only when the project's `main_health` row is green at exactly main's latest commit, which DD fetches into its checkout, and the checkout's commit is an ancestor of it. Main moving on before its own check has passed, or red main, means no update yet.

What the update changes decides what happens. Any non-Markdown file under `deploy/` (currently `dd-agent-run`, `sudoers-dynamic-dispatch`, `dynamic-dispatch.service`, `dd-rollback` and `dynamic-dispatch-rollback.service`, plus any added later) is never installed by DD: it sends one active Bark alert naming the files and giving `deploy/README.md`'s commands to run, then carries on with the code it has until updated by hand. A change touching `dispatch/` or `main.py` stops new work: no new story run, refresh or other queued run starts, and runs already going finish normally. Once a cycle finds nothing running and no account slot held, DD waits one more cycle, so alerts the last runs queued are sent, then fast-forwards its checkout and exits with code 75. The service's `Restart=on-failure` starts it on the new code; it must keep that setting, since a restart is how the update takes effect. Queued runs stay queued in the store and start after the restart. The restarted copy sends one passive alert, "DD updated to <short commit> (<pull request title>)", claimed from the store's `self_update_restart` row so it is sent once. That title, also used in the "DD is updating itself" comment and the update's other alerts, is each merged pull request's GitHub title, looked up once per pending update; if the lookup fails it is "pull request #N", and a plain commit is named by its first line. Git's merge message is never used, since it names the branch rather than the change. Markdown documents under `deploy/` (`*.md`, including `deploy/README.md`) do not require a manual update and are not listed among the changed files in that alert. Alongside code changes they follow the normal code update and restart path. A change touching only documents or tests fast-forwards the checkout at once without restarting, since the running code is unchanged.

During ordinary updates the checkout is only ever fast-forwarded on its main branch (`git merge --ff-only`, through `dispatch/gitops.py` with hooks off and filters refused), never merged or reset. A checkout with changed tracked files, off main (rolled back by hand, say) or whose commit isn't an ancestor of main's is left alone with one active alert per commit and reason, marked in the store's `self_update_notices` table. Each cycle looks again, so cleaning it up lets the update go ahead. Errors reading the checkout or the store record a warning event under the `self-update` story ID and are retried on the next check, with new work carrying on meanwhile. A fast-forward that fails when run (a held lock, say) leaves the checkout as it was, records a warning without an alert and is tried again on the next check; for a code update, new work stays held until it succeeds, since the update is still pending. An automatic recovery record also permits a detached checkout at exactly its recorded fallback to return to main when a newer tested commit arrives. `gitops.resume_own_checkout` checks clean tracked files, refuses filters, and requires both the fallback and local main to be ancestors of the target before advancing them. Manually detached checkouts remain untouched.

DD's loaded startup revision becomes last good only after ten minutes of completed cycles in the same process; an uncaught cycle error resets the monotonic timer. `dispatch/runs/updates.py` persists recovery state as atomically replaced, file-and-directory-fsynced JSON in `.dd-recovery/state.json` in DD's checkout, independent of SQLite and best-effort events. Document-only checkout advances do not change the running code's identity. The service permits three starts per five-minute interval with ten-second restart delays. When the next restart is refused, `OnFailure=dynamic-dispatch-rollback.service` invokes the separately installed, root-owned `/usr/local/bin/dd-rollback`. The helper checks for `start-limit-hit` so early failure callbacks do nothing. Before exiting with code 75, DD durably records a `planned_restart` Unix timestamp in that JSON, preserving the other recovery fields. Systemd counts planned and manual starts too: if the helper finds a planned restart within the preceding five minutes, it logs the reason, clears the start limit and restarts without changing Git or sending a rollback alert. The marker expires after five minutes; a genuine crash loop with no recent marker follows normal rollback. It stops DD and runs recovery operations as DD's account, claiming the attempted fallback before checking it out detached. Only service control runs as root; no sudoers or agent-launcher changes are needed.

The helper deliberately duplicates the minimal JSON persistence, safe Git invocation (hooks and fsmonitor disabled, filters and dirty tracked files refused), and encrypted Bark delivery without importing any DD module. This is an explicit exception to the Git module boundary: the newest DD code may not import at all. It uses only the standard library for recovery, plus the already-supported optional cryptography package for Bark. It attempts one active notification per configured device with sound, "DD rolled back to <short commit> because <new commit> kept failing to start", then clears the service start limit and restarts. Sending before restart keeps a slow notification from swallowing the fallback's OnFailure callback while the helper unit is active. Delivery errors cannot prevent restart and are not retried.

A repeated attempt to use the same last good commit, failure of that commit itself, missing last good state, or unsafe/failed checkout leaves DD stopped with one active "DD is down and needs a hand" alert. The durable `rolled_back`, `failed` and `down` fields prevent repeated recovery and notifications; an interrupted attempt is conservatively treated as used. The healthy fallback retains those guards and self-update skips the failed commit. A newer running version clears the guards only after qualifying as healthy. The approver can clear the state while DD is stopped to intervene manually. Recovery is available only after a version supporting this protocol has run healthily for ten minutes; it cannot repair a hang, undetected logical failure, unavailable host, or failed alert service.

### The Dynamic Dispatch light

Status light decisions and approval notices live in `dispatch/lights.py`; all their persistent state is owned by `dispatch/runs/pull_requests.py`.

Every open pull request DD manages carries a GitHub commit status named "Dynamic Dispatch" on its latest commit, next to the merge button. The approver merges only on green.

- **Yellow (pending), "Being reworked: don't merge":** set on the current PR commit when the existing board list shows In Progress, even while waiting for an agent. It replaces any previous light. Retries use that same state check; leaving In Progress restores the normal approval, main-update or blocked light on the next check, even without a new commit. A rework push gets its normal light.
- **Yellow (pending), "Updating with latest main":** set by the refresh check the moment it sees main has moved past the pull request's branch, before a refresh is queued, and left on while the refresh and any repair run, or while the refresh waits for its builder.
- **Green (success), "Up to date and tested":** set when a pull request opens and after each successful refresh, and by the refresh check on a pull request that already contains latest main. It goes only on the commit DD last pushed: DD pushes only commits its tests passed on, and fetches only main, so the remote-tracking branch is always a commit DD tested. A commit someone else pushed gets no green.
- **Red (failure), "Blocked: see the story in Plane":** follows the story's state in Plane, not DD's run outcome. It is set when a run's board update puts the story in Blocked, and by the refresh check on any story it finds in Blocked, including one the approver moved there by hand. That check already reads every story in the project (Plane's list can't be filtered by state, so it is read whole), so it needs no extra Plane calls. A failed run whose story was moved elsewhere gets no red. A red light is never left once the story is out of Blocked: the refresh check sets green if the pull request contains latest main, or yellow if it doesn't. Outside In Review, yellow then means the story's next run brings latest main in; only In Review stories are refreshed.

#### The approver's approval

With `github.approver` set to the approver's GitHub username, green also needs their approval, so they never merge code they haven't seen. Only an "Approve" review by that user counts; their later "Request changes", or a dismissal, withdraws it, and a plain comment leaves it standing. "Tested" then gives one of three lights:

- **Green (success), "Up to date, tested and approved":** their approval still counts for the tested commit.
- **Yellow (pending), "Waiting for your approval":** they haven't approved yet.
- **Yellow (pending), "Changed since your approval: re-review":** an agent changed the code after they approved it.

An approval given before a "needs your decision" return no longer counts, even on the same commit: the light shows "Waiting for your approval" until they approve again. GitHub's review time is compared with the latest `decision_at` recorded for the pull request, so the cutoff outlasts later refreshes.

An approval still counts if it was given on the current commit, or if every commit after it on the branch's own line (`git rev-list --first-parent`) is a clean DD "Combine latest main" commit: main merged in without conflicts, the tests passed first time, and no agent touched the result. What such a merge brings in is already on main, so it changes nothing they haven't been able to see. The worker records each combine commit it makes in the `combine_commits` table, marked clean or not, once it is saved. Anything else after the approval voids it: a clash or test repair during a refresh, rework, or a commit DD didn't record. A combine whose record couldn't be written counts as a change, so the error is on the side of asking for another look.

When a refresh repair voids an approval, DD posts a short comment on the pull request naming the commit that changed it, and sends an alert with sound, "<story>: PR #<number> changed since your approval". Each is sent once per commit (the store’s `approval_notices` table marks it). Rework needs neither: the approver asked for it, and its In Review alert already tells them.

GitHub reviews supply user, state and approved `commit_id`. Check approval whenever setting a tested light and each cycle for the two approval yellows; a new approval then turns green on the next check. Green is not polled again for approval, though board moves to In Progress or Blocked replace it. Errors warn and retry without failing or blocking stories. Notices need "Pull requests: Read and write", already needed by the App for PR creation. Without `github.approver`, green means up to date and tested.

A status belongs to one commit, so every push (refresh, repair or rework) starts without one until DD tests it and sets its own. Lights are set from the board update after the run has closed, and from the refresh check on the main loop, never in a way that holds up, blocks or fails a story. The store’s `pr_lights` table keeps the requested light, commit, displayed light and whether it is owed, set or left alone. Each change is saved as owed before GitHub is asked, then as set once accepted. Existing event-based state is imported once automatically; events remain best-effort history. If GitHub refuses or can't be reached, DD records a warning, and every cycle retries the lights still owed. Owed red and rework lights are retried by the refresh check only while the story is still in their corresponding state. A light already showing is not sent again, so an unchanged light needs no repeated status writes, and GitHub's cap on statuses per commit is never approached. Merged or closed pull requests are left alone (saved as left in the store). Pull requests open before lights existed get theirs on the next cycle from the refresh check: In Review ones green or yellow, Blocked ones red, In Progress ones rework yellow. Statuses are posted with the same GitHub App token as pull requests, which needs the "Commit statuses: Read and write" permission.

### Dashboard records

The store keeps the story timeline, agent usage and heartbeat. Startup creates the additional tables automatically for existing databases. Events carry a run ID, project and story IDs, UTC time, short type, agent and detail. Stories waiting for an available agent have no run yet, so those warnings have a null run ID and retain their project and story IDs. Details are scrubbed before being capped at 8,000 characters. Event writes are best effort: a failure logs a warning and does not stop the story. Consecutive identical warnings and retries update a repeat count and UTC last-seen time; a different event or a cleared warning starts a new entry.

Each agent start creates a usage record with its run, agent and UTC start time; the returned usage and cost are saved immediately, before tests or publishing can fail or cancel the run. Missing usage is null (unknown), not zero. Resumed branches that skip the agent do not count as new agent usage. The run's existing usage fields remain available too. The adapter also reads parsable usage after failed processes.

Every cycle updates a single UTC heartbeat with the git commit captured once when DD starts, or `unknown` if it cannot be read. Pulling code does not change that version until DD restarts. Startup reduces legacy heartbeat history to the latest row. Events and usage are retained indefinitely; worktree cleanup does not delete these records. Store readers expose timelines, usage joined to story IDs, and the latest heartbeat for the future read-only dashboard service.

### Mechanical verify gate

Each project's config names a **verify command**: whatever proves the code is not broken in that repo (test suite, build, lint, or some combination). It runs in the agent's worktree after the agent finishes and before the PR reaches review. **It runs as the sandbox user**, because it executes code the agent may have written.

Nonzero exit sends the story to Blocked with the output on the item. It never reaches the human review queue.

Verification protects the scarcest resource, human review attention; it does not replace code review. Together with the PR flow and the privilege separation in §10, it is why pointing DD at a repo that matters is reasonable.

### Concurrency: one run per agent account at a time

Claude and Codex can build different stories simultaneously. Each agent's `max_concurrent` defaults to 1; startup rejects every other value. Reaping kills everything owned by that agent's account, so two runs on the same account would kill each other. Higher concurrency needs an account or container per run first.

The main loop keeps polling Plane, reconciling outcomes, checking refreshes and recording its heartbeat while workers run. Every worker has its own SQLite connection. Atomic queue claims reserve a durable account slot until the worker finishes, including cleanup. Selection follows the rules above; refreshes wait for their builder's slot, and main checks take any free one, ahead of new work. Stories without a slot stay In Progress with one busy comment and start on a later poll. Cooldowns use the usage-limit waiting message.

Git operations use a reentrant lock per repository, retaining the existing safe Git runner. Fetching, worktree setup/removal/pruning, branch renames, ready marks, commits and pushes are serialized, and so are the read-only revision and history lookups behind lights and combine records; agents, verification and repairs run outside the lock. Secret registration and scrubbing, GitHub App token caching, and outcome reconciliation are protected from concurrent access. Failed-folder cleanup excludes active stories and occupied slots.

**The real ceiling is story independence, not hardware.** Two agents on adjacent code produce clean PRs that conflict at merge, and that cost is paid as rework. The cap belongs in sprint planning as much as in config.

**Runs are not blindly retryable.** A stateless API call that fails can be re-fired; a run that failed *after committing and pushing* cannot. Every run must be idempotent against work it may have already done.

### Encrypted Bark alerts

Optional phone notifications use the configured personal Bark server. The sole exception to stdlib-only Python is the system `python3-cryptography` package: AES-256-GCM is required, and `openssl enc` cannot provide GCM. Only `dispatch/alerts.py` imports it, and only when alerts are configured. Invalid settings or a missing package disable alerts with a startup warning. Device keys must meet redaction's eight-character minimum; shorter values disable alerts and are excluded from secret redaction.

After successful Plane writes, DD queues outcome and comment notifications on a daemon worker. Build/rework starts are queued after confirming the story is still in the expected state. In Review from build/rework, Blocked and main turning red or green or becoming unavailable to test/recovering use `active`; other Orchestrator comments and run starts use `passive`. Main alerts are about the project, not a story, so they need no Plane lookup and are grouped as "<project> main". The story key groups notifications, and review alerts carry the PR URL, pointing at its Files changed tab (`/files`) when it passes the same check as the In Review comment's link. A story moved to a different state than the intended outcome gets a silent notice rather than a misleading state-change alert. Refresh runs do not send start alerts.

Blocked alerts also name a sound, `alerts.blocked_sound` (default `telegraph`), so a stuck story can be told apart by ear; an empty value leaves Bark's default sound, and `doctor` reports it as WARN. Other alerts name no sound. Like every other field, the sound travels only inside the encrypted JSON.

The complete JSON (title, body, group, level, url, and sound when set) is encrypted with a 32-byte UTF-8 key and a fresh random 12-character ASCII IV per delivery. Bark receives only `ciphertext` (base64 ciphertext plus GCM tag) and `iv` as form fields, with the device key in its route. Byte-aware body trimming includes JSON, GCM, base64 and form escaping in the 3,000-byte limit. Keys join secret redaction. Delivery failures record warning events with only the exception type, excluding request and response details, and never fail a run; the queue is deliberately best-effort, not persisted or drained on shutdown, so phone availability cannot hold up DD. The one exception is main-health alerts, whose owed state lives in the store and which are owed again after a failed delivery (see the main check above).

### Login health alerts

DD sends active (sound-enabled) Bark alerts when Plane rejects an Orchestrator or agent login with HTTP 401/403, GitHub returns HTTP 401 (including App installation-token requests), Git push reports an authentication failure, or an agent reports a failed login. Detection uses Codex error and turn.failed messages, Gemini's JSON error (message, type and code), or Claude error result text and stderr, so transcript and tool output cannot trigger a login alert. Each parser has its own login wording in `LOGIN_PATTERNS`; no agent's wording stands in for another's. Messages identify the login and observation point without copying error output or credentials. System alerts need no Plane lookup. Existing story failure and Blocked handling is unchanged.

A SQLite `login_alerts` table, owned by the runs store, remembers failures across restarts. The observer loads its keys at startup and writes only state changes; routine successful calls skip both the database and the observer lock. A successful call using the same login rearms its alert. Plane and GitHub API credentials use hashed identities; App token acquisition and installation API calls use separate identities derived from the App configuration; agent identities use their account, environment-file path and parser. Git push uses one host-login identity, matching the shared saved GitHub credential. Agent wording patterns live beside the usage-limit patterns in `dispatch/agents.py`.

A background monitor checks `github.token` with `GET /rate_limit` once per UTC day, independently of story execution, even when PRs use a GitHub App. It reads `github-authentication-token-expiration` and alerts once per token, expiry and 30/7/1-day threshold. A first observation inside multiple thresholds sends each crossed threshold once. A missing token, missing/invalid date or failed request records a system warning event (empty project, no story) at most once that day. No Claude or Plane expiry checks are made. The existing optional Bark settings and phone sound settings apply; without Bark, warning events still work. Delivery remains best effort: queued alerts can be lost at shutdown, and a failed delivery does not rearm an already recorded alert.

### Reviewer policy, when AI review arrives

**A different instance than the author, preferring a different type when one is available.** This has an answer in every configuration, including a single-agent install.

**Same-type review is weaker, not equivalent.** A model finds its own patterns reasonable because they are its patterns. A second instance of the same model catches typos, missed edge cases, and convention violations; it is much less likely to catch "this whole approach is wrong." When only one model type is available, more of the real review burden sits with the human. Plan accordingly.

**AI review never sets pass/fail.** Findings only, risk-scored, with file/line/what/why/fix. The human is the only one who decides. An agent's mistaken read on its own review should not be unappealable.

**Findings are posted on the PR, not as board comments.** Code review belongs where the code is.

---

## 10. Identity and security

### The rule

> **An agent process gets its own model credential and nothing else.** No Plane tokens, no GitHub token, no config file, no inherited environment. The rule extends to anything the agent can influence, including code DD later runs on its behalf.

### Actors

| Actor | Unix user | Holds | Plane writes | Git author |
|---|---|---|---|---|
| The approver | Their own | Their own accounts | Human decisions | — |
| DD (Orchestrator on Plane) | DD's account (the approver's own in the example setup, §15) | `config.json` (every Plane and GitHub token), the GitHub App's private key, git credential store | System events: blocked, failures | — |
| Claude (agent) | dd-agent | Its model credential only | None directly. Work events are written on its behalf with its Plane token | `Claude <claude@example.com>` |
| Codex (agent) | dd-codex | Its model credential only | None directly. Work events are written on its behalf with its Plane token | `Codex <codex@example.com>` |
| Gemini (agent, optional) | dd-gemini | Its model credential only | None directly. Work events are written on its behalf with its Plane token | `Gemini <gemini@example.com>` |

Every agent has its own Unix account (`run_as` in its config entry), its own Plane account and its own git author. Its model login is either an env file named in its config entry (Claude: `/etc/dynamic-dispatch/claude.env`; Gemini: `/etc/dynamic-dispatch/gemini.env`, its Vertex AI settings and the path of a key file in its own home) or, with no `env_file`, whatever the agent keeps in its own home (Codex: `/home/dd-codex/.codex/auth.json`, readable only by `dd-codex`). Everything that identifies the agent comes from its config entry, so a third agent is a new config entry (and a parser, if its output format is new) plus an account, its login and a line in the sudoers rule. Commits, the pull request's "Agent:" line and the Plane comment on success all use the building agent's identity, so there is no ambiguity about what wrote what.

### How it is enforced

- **A separate Unix user per agent.** Each agent runs as its own account (`dd-agent` for Claude, `dd-codex` for Codex) through one root-owned launcher, `/usr/local/bin/dd-agent-run`. One sudoers rule lets DD's user run that launcher as `dd-agent` or `dd-codex`, and nothing else. Neither account has sudo rights. The agent's verify command runs as the same account.
- **Agents can't read each other's credentials.** At startup DD refuses to run if two agents share a `run_as` account (an agent without `run_as` uses `sandbox.user`), or if any agent's env file can be read by another agent's account. The check uses the file's owner, group and mode bits; it doesn't look at ACLs or at the directories above the file. An agent without an env file has nothing to check: a login it keeps in its own home is protected by that home's permissions, which DD doesn't read.
- **An empty environment.** The launcher starts from nothing, adds the basics (including `HOME`, so an agent finds a login kept there), and loads only the agent's own env file, if it has one. Nothing is inherited from DD.
- **Verification is sandboxed too.** The verify command runs code from the worktree the agent just wrote, so it runs as the building agent's account without loading its model env file. A login kept in that account's home stays readable to it.
- **Credentials are unreadable.** `config.json` is mode 600 inside DD's private home directory, and DD refuses to start if the file is readable by anyone else. The GitHub push credential lives in DD's git credential store, in the same home.
- **Pull requests come from a bot.** When `github` in `config.json` names a GitHub App (`app_id`, `installation_id`, `private_key_path`), pull requests are opened and looked up as that App, so the approver can formally approve them and it is always clear what the agent made. DD signs a JWT with the App's key, using the system's `openssl` because the standard library has no RSA signing, and exchanges it for an installation token that lasts about an hour. Installation tokens are cached in memory per App configuration and renewed five minutes before expiry; it is added to the scrubbed secrets the moment it arrives and is never written to disk. The key file follows the same rule as `config.json`: DD refuses to start if anyone else can read it. Without an App, the plain `token` is used. Either needs "Commit statuses: Read and write" for the Dynamic Dispatch light (§9). Pushing is unaffected either way and still uses the git credential store. A failed token request sends the story to Blocked.
- **Git never trusts the worktree.** DD's Git commands name the worktree's admin directory explicitly, so a `.git` file rewritten by the agent is ignored, and they disable hooks and fsmonitor. The agent cannot write to the main repository's Git directory.
- **No filter drivers.** A filter driver runs a command from Git config on whichever files `.gitattributes` names, and the agent writes `.gitattributes`. `status`, `add` and checkout all run them. So before any Git command in a worktree, and before creating one, DD checks for configured filter drivers and refuses to go on if there are any. Git can load extra config only for some branches or worktrees (`includeIf`), which a check from the main checkout can't see, so a new worktree is created empty, checked from inside, and only then filled. Once it is filled and made writable for the agent, DD marks it ready, in the worktree's admin directory where the agent can't reach. A worktree without that mark was left half set up when DD stopped part way. It is removed and rebuilt rather than reused, so the agent never gets an empty tree or one it can't write to. A ready worktree is reused only for the same agent group. If its group differs, DD cancels an unfinished merge, saves any remaining uncommitted edits on a local `<story>-leftover-<run>` branch (never pushed), removes the folder and checks out the saved story branch into a fresh folder. The leftover snapshot is not treated as verified work. An event and the story comment explain the rebuild and name the leftover branch. This rules out Git LFS on the host for now.
- **DD creates nothing group-writable by default.** It sets its umask to 022 at startup, whatever the login default (Raspberry Pi OS uses 002). Without that, the agent's group could write to what DD creates in the main repository, including each worktree's admin directory. Worktree contents are the one exception, granted file by file below.
- **Per-agent worktree groups.** An agent's optional `group` setting names its worktree group; without it DD uses the primary group of its `run_as` account. The doctor's story-folder check selects names matching the configured story branch prefix and numeric Plane sequence ID; main-check and assessment containers are not story folders. DD's account owns each story folder with mode 2770, and DD-created contents belong to the building agent's group and are group-writable. Setgid preserves the group for new contents. At startup DD checks that its running account belongs to every agent group, that each agent belongs to its own group, and that no agent belongs to another agent's group. Missing accounts or group entries also stop startup, including a primary group ID with no group entry. Errors name the account and group to fix. `ddwork` only allows agents to reach the worktree area; it does not grant access to story folders.
- **Permissions are granted without following links.** The permission walk changes groups and modes through open file descriptors, never follows symlinks, and touches only DD-owned regular files and directories. Files with multiple hard links are skipped. The same walk repairs DD-created contents after merging or cancelling a merge. A setup failure removes the incomplete worktree without marking it ready.
- **No agent process outlives its run.** Before and after every sandboxed call, the launcher's `--reap` mode kills every process the agent's account owns, then checks `/proc` until none is left alive, waiting up to 10 seconds for any stuck in the kernel. It reads each process's state from `/proc/PID/status`, where the process name (which the agent chooses) is escaped and can't fake a state. A zombie counts as dead only if it has a single thread, since a process whose main thread has exited looks like a zombie while its other threads run. Anything it can't read counts as alive. So nothing the agent started in the background can change the worktree while DD commits it. If a reap fails, or something survives it, the call counts as failed.
- **The agent never runs credentialed Git.** It edits files. DD commits and pushes.
- **Redaction at every exit.** Every configured secret, including the model credential, is scrubbed from anything stored, logged, posted to Plane, or put in a pull request. That includes story titles, which are scrubbed when queued and again in the commit message. Only numeric fields are kept from an agent's usage report, and cost must be a number.
- **The launcher confines the working directory** to the worktree root, and enforces the time limit itself, because DD cannot signal another user's processes.

### What is still exposed, knowingly

- The agent's own model credential. It needs it to work.
- Codex's login, in its output. DD can't read `/home/dd-codex/.codex/auth.json`, and shouldn't, so it can't scrub that credential from what Codex prints. The same holds for Gemini's Vertex AI key file in `/home/dd-gemini`. Redaction covers only credentials in `config.json` and env files.
- Anything world-readable on the Pi. A separate account is not a container.
- The repository it is working on. Never commit a credential to a repo an agent works in.
- The shared `ddwork` group permits access to the worktree area, but grants no access inside another agent’s story folder.
- Reaping kills every process of the agent's account, which is only safe while that account has one run at a time (§9).

Plane accounts were created by ordinary password sign-up; the instance has no SMTP.

---

## 11. After the MVP

Remaining work, ordered roughly by value:

1. **AI peer review** on the PR, under the policy in §9.
2. **Richer rework context:** reconstruct the original task, prior summary, review findings and human instructions on the existing branch. Basic same-branch rework already works; keep context in durable run records.
3. **Multiple projects.**
4. **Auto-merge on Done**, after human approval.
5. **Auto-deploy, and QA returns as a column.**
6. **Finer permission scoping:** agents still run with their permission checks off (Claude `--dangerously-skip-permissions`, Codex `--dangerously-bypass-approvals-and-sandbox`), contained by their sandbox accounts (§10). Narrowing what they may do inside the sandbox is further hardening.

## 12. Rejected and deferred, with reasons

| Decision | Why |
|---|---|
| Cloud/infrastructure portability as a requirement | Prefer simple portable choices; no abstraction requirement (§4). |
| Agent-agnosticism as "Core Requirement #1" | The intent is real (R1) and the implementation is a day's work. It is a seam, not a philosophy. |
| "Proving ground before touching another project" framing | DD is the development process, not a temporary test harness for another project. |
| Findings as board comments | Review happens on the PR. |
| A toy test repo (`hello.py`) | Tests the plumbing, not the product. Real repos are the test target once PRs exist. |
| Third-party orchestration tools (amux et al.) | Evaluated Aug 2026. No Plane integration; adopting one replaces the board rather than augmenting it. Building it personally serves the code-literacy goal directly. **Revisit if** the bug rate stops falling, or scale exceeds what the custom design was built for. |
| Concurrency as a non-goal | Concurrency is a principal goal and now works per account (§9). |

---

## 13. Known hazards

**Rubber-stamping.** Five PRs at 10pm and "looks fine" becomes the default, at which point the learning purpose is dead and the quality gate is theatre. Likely mitigation: a WIP limit. Don't let more land than there is appetite to actually read.

**Story quality is the throttle on the whole system.** An agent will faithfully build exactly what a vague story describes, and that only surfaces at review, after the tokens are spent. The per-story spec — acceptance criteria, files in scope, what is explicitly out of scope — is load-bearing infrastructure, not paperwork.

**Self-modification.** Dynamic Dispatch builds its own features. Worktrees mean an agent edits a copy rather than the live checkout, so it is survivable. DD restarts itself only on code main's tests passed on, and never installs deploy files (§9), but passing tests don't prove DD starts and works: a bad merge can still take down the thing that would have reported the breakage, and nothing rolls it back automatically yet.

**No definition of "working."** There is no measure of whether the pipeline earns its keep. A simple log — stories completed, rework cycles, findings caught versus missed, escapes — would make that answerable instead of a feeling.

---

## 14. Open questions

- **A first real project to vet the pipeline against** — genuine stakes, low blast radius.

---

## 15. Infrastructure

**Example setup: the author's.** This section describes one working install, for illustration. None of it is a requirement: other hardware, account names and paths work too.

| Component | Role |
|---|---|
| Raspberry Pi 4 (8GB, SSD via SATA HAT) | DD, worktrees |
| Raspberry Pi 5 (16GB, NVMe) | Plane CE, self-hosted via Docker |
| Mac mini | Workstation |

The Pi's value is being always-on: work proceeding while the approver is asleep or busy. Worktrees alone would have solved single-machine collision on any host.

### Paths on the Pi, in this example

| Path | Owner and mode | Purpose |
|---|---|---|
| `~/dynamic-dispatch` | DD's account, private home | DD's code and `config.json` (600) |
| `/srv/dynamic-dispatch/repos/<repo>` | DD's account:ddwork, group read-only | Target repository clones |
| `/srv/dynamic-dispatch/worktrees` | DD's account:ddwork, setgid, group read-only at the root | Parent remains mode 2750; story folders are owned by DD's account and the agent's group, mode 2770 |
| `/etc/dynamic-dispatch/claude.env` | root:dd-agent, 640 | Claude's model credential. Readable by `dd-agent`, and by DD for redaction |
| `/home/dd-codex/.codex/auth.json` | dd-codex, readable only by it | Codex's login, made by signing Codex in as `dd-codex`. Codex has no env file |
| `/usr/local/bin/dd-agent-run` | root, 755 | The sandbox launcher, shared by every agent account |
| `/etc/sudoers.d/dynamic-dispatch` | root, 440 | From `deploy/sudoers-dynamic-dispatch`: the launcher as `dd-agent` or `dd-codex`, nothing else. Reinstall it after it changes |

| Account or group | Members | Why |
|---|---|---|
| `dd-agent` | — | Claude's account. The name predates the second agent and stays |
| `dd-codex` | — | Codex's account. Its login lives in its home, in `.codex/auth.json` |
| `dd-agent` group | DD's account, `dd-agent` | Claude story folders, and owns `claude.env` so only Claude and DD can read it |
| `dd-codex` group | DD's account, `dd-codex` | Codex story folders |
| `ddwork` | DD's account, `dd-agent`, `dd-codex` | Allows traversal of the worktree area, not access inside story folders |

Env files can't be owned by `ddwork` any more: every agent is in it, and DD refuses to start while one agent's env file is readable by another. Nothing under `repos/` may be group-writable, since the agents are in `ddwork`: the clone is made read-only to the group with `chmod -R g-w` after cloning, and the login umask is 022.

`python3 main.py --doctor` (`dispatch/doctor.py`) checks this setup without changing it: config, Plane and GitHub access, Git, each agent's account, env file and command, the services, the rollback helper, the launcher and the sudo rules. The sandbox checks in `dispatch/doctor_sandbox.py` inspect metadata only: agent access to config and the App key, write access throughout DD’s checkout, administrator groups, and story folder groups and mode 2770. ACLs, symlinks and unavailable metadata produce WARN instead of an isolation claim; failed account checks skip dependent checks. Folder ownership is compared with project agent groups; with several possible groups, assignment to the individual story needs manual verification and produces WARN. These checks never run as an agent or alter permissions. Each check reports OK, WARN, FAIL or SKIP, never config values or exception text. `deploy/README.md` lists what it checks.

### Setup and project onboarding

`python3 main.py --setup` (`dispatch/setup.py`) writes `config.json` from
answers, after the installer has made the accounts and folders. Each answer is
checked as it is given, with the same Plane and GitHub clients DD uses, and a
failure is explained with doctor's reasons (rejected, not found, unreachable),
never the reply's text, which can echo the credential. Secrets are read with
`getpass`; its fallback that echoes input without a terminal is refused, so
setup stops instead. An existing file's own contents are the current values,
not the defaults loading adds, and a value kept with Enter keeps its saved
form, so the same answers save nothing. Agents come from the installer's
names, and setup only adds them: removing one could orphan a project's agent
list. The App's private key stays in its own file; setup records its path.
An App without contents read, pull requests write and commit statuses write
is refused like a rejected login, and its cached installation token is
dropped so the next answer is checked against the permissions GitHub reports
then. It ends by offering `--add-project`, running doctor and printing the
start command; it never starts DD. If `--add-project` fails without adding a
project, setup stops there with a failure, keeping the saved config, so
neither doctor nor the start command suggests setup finished.

`python3 main.py --add-project` coordinates onboarding in `dispatch/add_project.py`.
GitHub repository creation and access grants remain manual prerequisites. Setup
uses the configured App and host Git login to check access, then discovers or
creates Plane resources by name and members by account ID. It asks before reusing
a project and never deletes or renames remote resources. Re-running after a
partial failure uses the same discovery rather than an events-table checkpoint.
Git cloning stays behind `gitops`; completed clones take the projects folder's
group without group write, as the paths table requires, and are published from
temporary folders. An empty repository stops setup, since it has no base branch.
Config replacement stays behind `config.save`, which both commands use: validate a
private candidate, retain an exact backup with mode 600, then replace atomically. Settings
are merged over the file's own contents, not the defaults loading adds, and an
unchanged project is not saved again.
Doctor checks the saved project before the maintainer restarts DD. Test dependencies
and push write permission still need human confirmation; setup runs no tests and
installs no software.

### Plane

Plane requests use optional `plane.api_url`, falling back to `plane.base_url`;
web links always use `base_url`. DD identifies itself in every API request.
HTTP 429 is retried up to three times after Retry-After (seconds or HTTP date),
or five seconds when absent or invalid. Member creation starts with v1 and
uses v2 only for Cloud's missing invitation-fields response.

- **Project: Dynamic Dispatch** — this product's own backlog. Cycles, Views and Pages enabled.
- State IDs captured via `GET /api/v1/workspaces/{slug}/projects/{id}/states/` with `X-API-Key`.

---

## 16. Maintaining this document

Two rules, learned from an earlier, discarded planning doc, which promoted passing remarks into requirements:

**Section weight implies priority.** Anything mentioned in passing that acquires a heading starts looking load-bearing. Write requirements, not context.

**Keep it short enough to actually read.** A source of truth nobody opens is just a file.

Put new material in its existing numbered section; replace superseded text instead of appending it. Keep decisions, rules and known hazards explicit. Record any bent design rule under "Decisions you may want to revisit" in the change summary. Call the system "DD" (or "Dynamic Dispatch") and keep "Orchestrator" for DD's Plane account.

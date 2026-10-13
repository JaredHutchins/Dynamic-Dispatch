# Dynamic Dispatch

Dynamic Dispatch (DD) takes stories from a Plane board and
has AI coding agents, such as Claude Code and Codex, build them as pull
requests. A human reviews every pull request and merges it; DD never merges
anything itself.

The aim is that a story posted in the morning becomes a reviewable, tested
pull request by the evening, without anyone watching over it.

## Who it is for

DD is for one person, or a small team, who already plans work as small
stories and wants agents to do the building while they keep the review. It is
self-hosted: it runs on your own machine (a Raspberry Pi is enough), with your
own Plane board, GitHub repository and agent logins.

It is a good fit if you are comfortable reading every pull request before you
merge it. It is not a hosted service, and it does not replace code review.

## How it works

1. **Write a story.** Each story follows [STORY_TEMPLATE.md](STORY_TEMPLATE.md):
   a user story, acceptance criteria, and what is in and out of scope.
2. **Move it to In Progress.** DD sees it on its next check, picks an agent
   that is free, and gives it a fresh, isolated copy of the repository on its
   own branch.
3. **The agent builds it.** The agent gets the story and the project's
   [Definition of Done](DEFINITION_OF_DONE.md), and edits files. It never
   commits, pushes or touches the board itself.
4. **DD tests it.** DD runs the project's tests on the agent's work, combines
   it with the latest main and tests it again. If something fails, the agent
   gets the failures back and a chance to fix them; if it still fails, the
   story moves to Blocked with the details on it.
5. **DD opens a pull request** and moves the story to In Review.
6. **You review and merge.** A status light on the pull request tells you when
   it is safe to merge. If you want changes, move the story back to In
   Progress and say what to change; an agent reworks the same branch.
   Once you merge, DD moves the story to Done.

While pull requests wait for review, DD keeps them up to date: when main moves
on, it combines each one with the latest main and re-tests it.

### Sprint-pull (Cycles)

Instead of moving stories one at a time, you can plan a sprint as a Plane
Cycle:

1. Add stories to a Cycle. DD moves them into Sprint Backlog and creates a
   plan item for the Cycle.
2. Reply `@Orchestrator assess` on the plan item. Each agent sizes the stories
   independently, and DD merges their answers into one plan: a build order,
   dependencies, where the agents disagree, and a rough estimate of whether
   the Cycle fits its dates.
3. Reply `@Orchestrator approve`, or `@Orchestrator reject:` with your
   reasons to get a new plan.
4. Once the Cycle starts, DD starts its stories in the approved order, as
   agents become free and the stories they depend on are done.
5. When the Cycle ends, stories that never started are carried over to the
   Product Backlog with a note.

Sprint-pull is off unless you turn it on for a project.

## Keeping work safe

- **Sandboxed agent accounts.** Each agent runs as its own Unix account with
  an empty environment and only its own model login. Plane and GitHub tokens
  stay with DD, and secrets are scrubbed from everything DD stores, logs or
  posts.
- **Tests before every push.** Nothing is pushed until the project's tests
  pass on it, run inside the agent's sandbox.
- **Combining with the latest main.** Work is combined with the latest main
  and re-tested before it is pushed, and again whenever main moves on.
- **The status light and human approval.** A "Dynamic Dispatch" light on each
  pull request turns green only when it is up to date, tested and, if you set
  it up, approved by you. Only a human merges.
- **Stopping the line when main breaks.** DD tests main whenever it changes.
  If main starts failing, DD stops starting new work, alerts you and raises a
  story to fix main first.
- **Self-update with rollback.** DD can update itself after a change to its
  own code is merged and main passes. If the new version won't run, it rolls
  back to the last version that did and tells you.
- **The doctor command.** `python3 main.py --doctor` checks the setup without
  changing it: config, Plane and GitHub access, agent accounts and logins, the
  services, and that the sandbox walls hold.

## Read more

- [ARCHITECTURE.md](ARCHITECTURE.md): how DD is designed, and why.
- [SECURITY.md](SECURITY.md): how to report a security problem, and DD's
  security model.
- [deploy/README.md](deploy/README.md): setting DD up and running it as a
  service.
- [LICENSE](LICENSE): the licence DD is released under.

Setting DD up is currently a manual process, described in
[deploy/README.md](deploy/README.md). These steps will be simplified by an
installer later.

# Dynamic Dispatch

[![Tests on main](https://github.com/JaredHutchins/Dynamic-Dispatch/actions/workflows/tests.yml/badge.svg?branch=main&event=push)](../../actions/workflows/tests.yml?query=branch%3Amain)

Dynamic Dispatch (DD) is an AI agent orchestration system for software work.
It takes stories from a Plane board, has AI coding agents such as Claude Code
and Codex build them as pull requests, and keeps those pull requests tested
and up to date. A human reviews every pull request and merges it; DD never
merges anything itself.

The aim is that a story posted in the morning becomes a reviewable, tested
pull request by the evening, without anyone watching over it.

**Status: early release.** See [Releases](../../releases) for the current
version and what changed. DD builds its own stories every day on its author's
setup, and that is the only setup tested so far. See
[What you need](#what-you-need).

## Who it is for

DD is for one person, or a small team, who already plans work as small
stories and wants agents to do the building while they keep the review. It is
self-hosted: it runs on your own machine (a Raspberry Pi is enough), with your
own Plane board, GitHub repository and agent logins.

It is a good fit if you are comfortable reading every pull request before you
merge it. It is not a hosted service, and it does not replace code review.

## What you need

- **A Linux machine** with systemd and sudo. DD's sandbox runs each agent as
  its own Linux account. A Raspberry Pi 4 is enough.
- **Python 3.11 or newer.** DD uses only the standard library, plus
  `cryptography` if you turn on phone alerts.
- **Plane:** self-hosted Plane Community Edition (tested with 1.4.2), or
  Plane Cloud, where the Orchestrator and agents can sign in as Plane apps (bots).
  See [Set up Plane Cloud](deploy/PLANE_CLOUD.md).
- **A GitHub repository,** a GitHub App for DD (it opens pull requests and
  sets the status light), and a Git login on the machine that can push to the
  repository.
- **At least one agent:** Claude Code, Codex or Gemini CLI, installed and
  logged in. Gemini CLI has so far been tested with its recorded output, not
  a live account.
- **Optional:** a self-hosted [Bark server](https://github.com/Finb/bark-server)
  and the [Bark app](https://github.com/Finb/Bark) (iPhone and iPad) for
  encrypted phone alerts.

To set it up, first run the [installer](deploy/README.md#recommended-system-setup),
then the [guided setup](deploy/README.md#guided-setup), as DD's account from
the checkout:

    python3 main.py --setup

It asks for the Plane address and workspace, the Orchestrator's token, the
GitHub App's details (or a plain token), the approver, each agent's Plane
token and commit name and email, and optional phone alerts. It checks each
answer as it goes, types secrets hidden, and writes `config.json` readable only
by DD's account. Then it offers to add the first project, runs the doctor and
prints the command that starts DD. Agent logins are a separate step (below).

### Agent logins

DD has been tested with Claude Code and Codex signed in with personal
subscription plans. Each plan has its own terms on automated use, so check
yours before running DD unattended. API keys haven't been tested with DD yet;
support for them is planned.

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
2. Reply `@Orchestrator assess` on the plan item (Orchestrator is DD's Plane
   account). Each agent sizes the stories independently, and DD merges their
   answers into one plan: a build order, dependencies, where the agents
   disagree, and a rough estimate of whether the Cycle fits its dates.
3. Reply `@Orchestrator approve`, or `@Orchestrator reject:` with your
   reasons to get a new plan.
4. Once the Cycle starts, DD starts its stories in the approved order, as
   agents become free and the stories they depend on are done.
5. When the Cycle ends, stories that never started are carried over to the
   Product Backlog with a note.

A Cycle ends early once all its stories are Done or Cancelled and main has
passed its tests. A Cancelled story never releases the stories that depend on
it; they are carried over instead.

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
- **Phone alerts (optional).** Alerts through your own Bark server when a pull
  request is ready for review, a story is blocked, or a login is rejected. DD
  encrypts each alert before sending it, so neither the Bark server nor
  Apple's push service can read it.
- **The doctor command.** `python3 main.py --doctor` checks the setup without
  changing it: config, Plane and GitHub access, agent accounts, the services,
  and that the sandbox walls hold. It can't check agent logins without running
  an agent; DD alerts you when a login is rejected.

## Read more

- [ARCHITECTURE.md](ARCHITECTURE.md): how DD is designed, and why.
- [SECURITY.md](SECURITY.md): how to report a security problem, and DD's
  security model.
- [deploy/README.md](deploy/README.md): setting DD up and running it as a
  service.
- [LICENSE](LICENSE): the licence DD is released under.

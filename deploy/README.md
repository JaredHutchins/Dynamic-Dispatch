# Running Dynamic Dispatch as a service

`dynamic-dispatch.service` makes the Pi start Dynamic Dispatch when it boots,
once the network is up, and start it again 10 seconds after it stops
unexpectedly. It runs as the account and from the checkout chosen below.
Its output goes to the system journal instead of `~/dd.log`.

The sandbox permissions are unchanged; the sudoers template grants the chosen
DD account access to the existing agent launcher.

## Recommended system setup

From a trusted DD checkout, run this as the existing account that will run DD:

    sudo python3 deploy/dd-install --agents claude codex

Choose any combination of `claude`, `codex` and `gemini`. The default is Claude
and Codex. DD's account defaults to `SUDO_USER`; use `--dd-user ACCOUNT` when
running as root or installing for another existing account. It must be a
non-root account separate from the agent accounts. The checkout's current
absolute path is written into the service. Run only one installer at a time.

The installer requires Linux, Python 3.11 or newer, and the Debian-family
`groupadd`, `useradd`, `usermod` and `visudo` tools. Normal host setup also
requires a running systemd installation and `systemctl`. Install prerequisites
yourself first; the installer never downloads or installs software. It checks
selected agent commands in the launcher's `/usr/local/bin:/usr/bin:/bin` path
and prints official installation links for missing commands. These warnings do
not prevent system setup; install the tools before running stories.

It prints every planned change and already-completed step, then asks for one
`yes`. To inspect the same plan without changing anything:

    sudo python3 deploy/dd-install --agents claude codex gemini --dry-run

For a Debian-based Docker image build, after creating DD's non-root account
and copying its checkout, use that checkout as the working directory and run:

    python3 deploy/dd-install --container --dd-user dd --agents claude codex

`--container` runs as root without a prompt and skips only the systemd units
and daemon reload. It still installs the rollback executable, launcher and
validated sudoers rule. It can be combined with `--dry-run`.

Setup creates missing agent accounts (`dd-agent`, `dd-codex`, `dd-gemini`) with
matching groups and private homes. It adds DD to each selected agent group,
and DD and selected agents to `ddwork`. Existing accounts and homes are kept;
existing logins are never edited. It manages these directories non-recursively:

| Directory | Owner | Mode |
|---|---|---|
| `/srv/dynamic-dispatch` | DD's account:ddwork | 750 |
| `/srv/dynamic-dispatch/repos` | DD's account:ddwork | 2750 |
| `/srv/dynamic-dispatch/worktrees` | DD's account:ddwork | 2750 |
| `/etc/dynamic-dispatch` | root:ddwork | 750 |
| `/etc/sudoers.d` (only if missing) | root:root | 750 |

Existing `/usr/local/bin` and `/etc/sudoers.d` belong to the operating system:
the installer checks that they are directories owned by root and not writable
by group or others, and never changes their ownership or modes. An unsafe
directory or missing `/usr/local/bin` stops setup before any changes.

The root-owned launcher and rollback helper use mode 755, the sudoers rule
uses 440, and the rendered service and rollback unit use 644. The sudo rule
allows exactly the selected agents; include all desired agents on subsequent
runs. The rule is checked with `visudo -cf` before atomic installation. The
rollback unit needs no substitutions: its helper reads DD's account and
checkout from the rendered main service at recovery time.

Rerunning skips matching state and reports “Nothing to do” when setup matches.
Changed deployment files are replaced, and managed ownership and modes are
restored to the table above. Nothing is recursively changed or deleted.
Symlink targets/ancestors, hard-linked file targets, and group/world-writable
ancestors are refused; correct unsafe paths manually before retrying. On a
failure it stops, lists completed work and the failed step, and the same
command inspects current state and continues. A failed step may be partially
complete. No rollback deletes previously completed work.

The installer does **not** edit `config.json`, secrets or agent logins, enable
or start DD, create projects, or contact Plane or GitHub. Log out and back in
afterwards so the new group memberships apply. Then run the guided setup.

## Guided setup

Run this as DD's account from the checkout, without sudo:

    python3 main.py --setup

Have these ready first; setup tells you where to make them but makes none:

- The Plane address and workspace slug, and an API token for the
  Orchestrator and for each agent's Plane account, made in Plane's settings
  while signed in as that account. Plane Cloud isn't supported.
- A GitHub App for DD with contents read, pull requests write and commit
  statuses write, installed on your repositories, and its private key saved
  on this machine with `chmod 600`. A fine-grained token works instead, but
  `--add-project` needs the App.
- The approver's GitHub username, and their Plane user ID if you will use
  sprint-pull.

Setup asks which agents the installer set up (`claude`, `codex`, `gemini`)
and checks each answer as it is given: the Plane address and each token sign
in and reach the workspace, each agent has its own Plane account, the App
signs in with the permissions above (or the token works) and the approver's
GitHub account exists. A
failed check says what to fix, never the secret, and asks again. Secrets are
typed hidden and are written only to `config.json`, with mode 600.

Run it again to change something: Enter keeps each current value. A changed
`config.json` is first backed up beside it as `config.json.backup-*`, with
mode 600, as `--add-project` does; the same answers change nothing. Agents
already configured are kept, and other settings in the file are left alone.
An identity that `config.json` gives a Plane app (see "Using Plane Cloud")
keeps it: setup asks for no token and checks that the app signs in.

It then offers to add the first project with `--add-project`, runs the
doctor, and prints the command that starts DD. Finish the "Agent logins" and
fix any doctor failures before running it:

    sudo systemctl enable --now dynamic-dispatch

If adding the project stops, setup says so and ends there, without running
the doctor or printing that command. The answers already given stay saved:
fix what `--add-project` reported, then run `python3 main.py --add-project`.

The sections marked "manual reference" below cover the same steps by hand.

## Choose the account and checkout (manual reference)

Run these commands as the account that will run DD, from its checkout.
For an existing installation, use its service account and checkout instead:

    export DD_USER="$(id -un)"
    export DD_CHECKOUT="$(pwd -P)"

Commands below use these variables. The service and sudoers files are templates;
do not install them without replacing `DD_USER` and `DD_CHECKOUT`.

Render both templates into a temporary directory. Python leaves the absolute service path
unquoted (including spaces) and escapes systemd's percent specifiers:

    DD_DEPLOY_TMP="$(mktemp -d)"
    export DD_DEPLOY_TMP
    python3 - <<'RENDER'
    import os
    from pathlib import Path

    source = Path(os.environ['DD_CHECKOUT']) / 'deploy'
    target = Path(os.environ['DD_DEPLOY_TMP'])
    user = os.environ['DD_USER']
    checkout = os.environ['DD_CHECKOUT'].replace('%', '%%')
    service = (source / 'dynamic-dispatch.service').read_text()
    (target / 'dynamic-dispatch.service').write_text(
        service.replace('DD_USER', user).replace('DD_CHECKOUT', checkout))
    sudoers = (source / 'sudoers-dynamic-dispatch').read_text()
    (target / 'sudoers-dynamic-dispatch').write_text(sudoers.replace('DD_USER', user))
    RENDER

## Set up config.json (manual reference)

`python3 main.py --setup` (see "Guided setup") writes this file for you.
DD reads its settings from `$DD_CHECKOUT/config.json`, which is never
committed. Start from the example and make it readable only by you, as DD
refuses to start otherwise:

    cd "$DD_CHECKOUT" && cp config.example.json config.json && chmod 600 config.json

Every value in capitals in the example is a placeholder. Replace each one:

- `YOUR_USER` in `db_path` and `private_key_path`: the Pi account DD runs as.
- `https://plane.example.com` in `plane.base_url`: the address you open
  Plane at in a browser, without a trailing path.
- Optional `plane.api_url`: a separate API address; omit it to use `plane.base_url`
  for both requests and web links. For Plane Cloud, set `plane.base_url` to
  `https://app.plane.so` and `plane.api_url` to `https://api.plane.so` in the
  config file. Web links always use `plane.base_url`. Cloud account login setup
  is separate from this API configuration.
- `YOUR_WORKSPACE_SLUG`: the first part of the path in any Plane page's
  address, as in `https://plane.example.com/YOUR_WORKSPACE_SLUG/projects/...`.
- `PLANE_ORCHESTRATOR_PAT`, `PLANE_CLAUDE_PAT` and `PLANE_CODEX_PAT`: an API
  token made in Plane's settings while signed in as the Orchestrator, Claude
  and Codex accounts respectively. On Plane Cloud, any of them can be a Plane
  app instead; see "Using Plane Cloud".
- `GITHUB_FINE_GRAINED_PAT`: a fine-grained token made on GitHub under
  Settings, Developer settings, Personal access tokens, with access to the
  repository.
- `YOUR_GITHUB_USERNAME` in `github.approver`: the GitHub account whose
  approval a pull request needs before it turns green. Leave the key out to
  skip approvals.
- `GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID` and the `.pem` path: from the
  GitHub App's settings page (App ID, and a private key you generate there)
  and the number at the end of the installation's settings address. Leave
  all three out to use the plain token instead.
- `claude@example.com` and `codex@example.com` in `git_author`: the email
  each agent's commits are made with. Use an address you control, or a
  GitHub noreply address.
- The project's key, `dynamic-dispatch`: any short name you like for the
  project. If it is DD's own repository and you use `self_update`, set that
  setting to the same name.
- `PLANE_PROJECT_ID`: the ID after `/projects/` in the address of the
  project's work items page in Plane.
- `YOUR_PLANE_IDENTIFIER`: the project's short identifier in Plane, the
  letters before the number in a story's ID (for example `ABC` in `ABC-12`).
- `OWNER/REPO` in `github_repo`: the repository's owner and name as they
  appear in its GitHub address. Replace `REPO` in `repo_path` with the
  folder the clone lives in.
- `PLANE_IN_PROGRESS_STATE_ID`, `PLANE_IN_REVIEW_STATE_ID`,
  `PLANE_BLOCKED_STATE_ID` and the optional `PLANE_DONE_STATE_ID`: the `id`
  of each matching state, listed by this request with the Orchestrator's
  token in place of `TOKEN`:

      curl -s -H "X-API-Key: TOKEN" https://plane.example.com/api/v1/workspaces/YOUR_WORKSPACE_SLUG/projects/PLANE_PROJECT_ID/states/

- `PLANE_AGENT_LABEL_ID`, optional: the `id` of the "Agent" label described
  below, from the same request with `labels/` in place of `states/`.
  `PLANE_HUMAN_LABEL_ID` comes from there too; DD doesn't read it today, so
  it can be left out.
- The project's `cycles` section and `PLANE_APPROVER_USER_ID` in
  `plane.approver_user_id`, both optional: see "Sprint-pull (Cycles)" below,
  or remove them.
- The `alerts` placeholders: see "Optional encrypted phone alerts" below,
  or remove the section.

Then check it loads, as described in "Adding a new setting".

## Using Plane Cloud

On Plane Cloud, the Orchestrator and each agent can be a Plane app (a bot)
instead of a Plane user with its own email address and personal token. Each
identity uses one or the other: a personal token, or an app. You can mix
them, say an app for the Orchestrator and personal tokens for the agents.
Identities that keep their personal tokens work exactly as before.

Set the addresses first, as "Set up config.json" describes:
`plane.base_url` is `https://app.plane.so` and `plane.api_url` is
`https://api.plane.so`.

### Create each app

Make one app per identity: one for the Orchestrator and one for each agent
that should be a bot, so the board shows who did what. Signed in to Plane as
a workspace admin:

1. Open the workspace settings, then Integrations, then Build your own.
2. Give the app the identity's name, such as "Orchestrator" or "Claude".
3. Set the Redirect URI to `http://localhost/`. Nothing needs to run there:
   the browser only has to land on that address so you can read it.
4. Leave Setup URL and Webhook URL empty. DD checks Plane on its own
   schedule and needs no webhooks.
5. For the Orchestrator's app only, turn on App Mentions.
6. Save, and note the Client ID and Client Secret. Plane shows the secret
   once; keep it only in `config.json`.

### What each app may do

The install link asks for these permissions, and no more:

- The Orchestrator: `projects:read projects:write projects.members:read
  projects.members:write projects.states:read projects.states:write
  projects.labels:read projects.labels:write projects.cycles:read
  projects.cycles:write projects.work_items:read projects.work_items:write
  projects.work_items.comments:read projects.work_items.comments:write
  profile:read`. It sets up projects, moves and assigns stories, plans
  Cycles and posts DD's own comments.
- An agent: `projects.work_items:read projects.work_items:write
  projects.work_items.comments:write profile:read`. DD uses an agent's
  login only to comment on its story, move it to In Review and look up its
  own user ID.

### Add it to config.json

In place of the identity's personal token, give its app's settings. For
the Orchestrator, replace `plane.orchestrator_token` with:

    "orchestrator_bot": {
      "client_id": "PLANE_ORCHESTRATOR_APP_CLIENT_ID",
      "client_secret": "PLANE_ORCHESTRATOR_APP_CLIENT_SECRET",
      "app_installation_id": "PLANE_ORCHESTRATOR_APP_INSTALLATION_ID"
    }

For an agent, replace its `plane_token` with `plane_bot`, with the same
three entries from that agent's app. DD refuses to start if an identity has
both a token and an app, or neither. Put the client ID and secret in now;
the installation ID comes from installing the app, next.

### Install it

Plane's own Install button fails without a Setup URL, so DD prints the
link instead. From the checkout, as DD's account:

    python3 main.py --plane-install-link orchestrator
    python3 main.py --plane-install-link claude-code

Name `orchestrator`, or an agent by its name in `config.json`. The command
reads only the app's client ID, never its secret. It prints the link, with
the permissions above, and these steps:

1. Open the link in a browser signed in to Plane as a workspace admin, and
   approve the app.
2. The browser lands on `http://localhost/?app_installation_id=...&code=...`.
   The page itself won't load; that is expected. Copy the value after
   `app_installation_id=` from the address bar, up to the next `&`.
3. Put it in `config.json` as that identity's `app_installation_id`.

If the app has a different Redirect URI, add
`--redirect-uri` with exactly that address. An installed app is a member
of the workspace's existing projects straight away. Then run
`python3 main.py --doctor`: its Plane checks show whether each app signs in.

DD gets each app's access token itself, keeps it in memory only and renews
it before it runs out (about ten hours on Plane Cloud), and once more if
Plane rejects it. A rejected app login sends the usual Plane login alert,
naming the identity. Client secrets and access tokens are scrubbed from
everything DD stores, logs or posts, like other secrets.

## Add a project

From DD's checkout on the Pi, as DD's account, run:

```sh
python3 main.py --add-project
```

Use `--config /path/to/config.json` for a different configuration. First create
and initialise the GitHub repository, give the ND Dispatch app access, and give
the Pi's saved HTTPS Git push login write access. The repository needs at least
one commit. The command checks App access and permissions, the host login's read
access, and that the repository is not empty. Read access cannot prove write
access; confirm the push login's write permission in GitHub yourself.

Enter the project name, `OWNER/REPO` (or its HTTPS GitHub URL), test command,
and whether to enable sprint-pull Cycles. The test command is split into arguments;
shell operators are not interpreted. Cycles require `plane.approver_user_id`.
The Plane accounts must already belong to the workspace, and Orchestrator must
be allowed to create projects and administer project members. The installed
Plane API must support adding members with `POST .../projects/{id}/members/`.

Setup creates the seven DD columns and three labels and adds Orchestrator and
all configured agents; Cycles also adds the configured approver. It asks before
using a same-name Plane project. A new project's work item prefix comes from
the first five letters and digits of its name, numbered if another project has it. Existing columns, labels and members are reused;
nothing is deleted or renamed, including Plane's default columns. A DD column
with an incompatible state group stops setup for manual correction.

Clones go beside existing configured clones, named `OWNER--REPO`, and use the
existing worktree root. Optional top-level `projects_root` and `worktree_root`
choose these locations explicitly (required if existing locations are ambiguous).
For a first project, the defaults are `/srv/dynamic-dispatch/repos` and
`/srv/dynamic-dispatch/worktrees`; an empty `projects` object is allowed during
setup. Existing matching configured clones are reused. A new clone takes the
group of the folder it is placed in (`ddwork` for `repos/`) and nothing in it is
group-writable, matching "Paths on the Pi" in `ARCHITECTURE.md`.

Before replacing config, setup validates a private candidate and saves the exact
original as `config.json.backup-*` beside it with mode 600.
Keep those backups private. Run only one setup command at a time. If a step fails,
run the same command with the same answers: it detects completed Plane resources
and clones, and leaves config untouched if the project is already saved. Approve
reusing the Plane project when prompted.

Doctor runs for the saved project and reports outstanding setup checks. Install
any needed test tools yourself, confirm dependencies under the agent accounts,
and restart DD when ready. Setup never installs software or runs repository tests.

## Install service files (manual reference)

First stop any copy started by hand with nohup, so two don't run at once:

    pkill -f "python3 main.py"

Render the templates as described above, then install the sudoers rule only
if validation succeeds:

    sudo visudo -cf "$DD_DEPLOY_TMP/sudoers-dynamic-dispatch" && sudo install -o root -g root -m 440 "$DD_DEPLOY_TMP/sudoers-dynamic-dispatch" /etc/sudoers.d/dynamic-dispatch
    sudo visudo -c

Then copy the service file into place, tell systemd about it, and turn it on
so it starts now and at every boot:

    sudo install -o root -g root -m 755 "$DD_CHECKOUT"/deploy/dd-rollback /usr/local/bin/dd-rollback
    sudo install -o root -g root -m 644 "$DD_CHECKOUT"/deploy/dynamic-dispatch-rollback.service /etc/systemd/system/dynamic-dispatch-rollback.service
    sudo install -o root -g root -m 644 "$DD_DEPLOY_TMP/dynamic-dispatch.service" /etc/systemd/system/dynamic-dispatch.service
    sudo systemctl daemon-reload
    sudo systemctl enable --now dynamic-dispatch

Check that it is running:

    systemctl status dynamic-dispatch

Remove the rendered temporary files after installation with
`rm -r -- "$DD_DEPLOY_TMP"`.

If you change the service file later, render and install it again and run
`sudo systemctl daemon-reload` followed by a restart.

A project can optionally set `agent_label_id` in `config.json`, next to
`human_only_label_id`, to the Plane ID of an existing "Agent" label. DD adds it
when assigning a build or rework, keeps existing labels, and never removes it.
Refreshes leave labels alone. Omit the setting to leave labels unchanged. Label
writes run in the assignment background queue; failures produce warning events
without stopping work.

A project can also set `done` under `states` to the Plane ID of its Done
state. DD then moves a story to Done by itself once its pull request is
merged, on the next check, if the story is still in In Progress, In Review or
Blocked and DD isn't working on it. The Orchestrator posts "Pull request #<n>
was merged, so this story is now Done." and one silent alert goes out. A pull
request closed without merging is left alone, and so is a story already
moved elsewhere. If Plane or GitHub can't be reached, a warning event is
recorded and the next check tries again; the comment never goes up twice.
Leave `done` out to keep moving stories to Done by hand. While main is
failing tests the check is paused with the rest of that project's board
checks, so merged stories move to Done once main passes again.

## Start, stop, restart

    sudo systemctl start dynamic-dispatch
    sudo systemctl stop dynamic-dispatch
    sudo systemctl restart dynamic-dispatch

Restart after pulling new code by hand or editing `config.json`; the running
copy doesn't notice either.

Stopping or restarting while an agent is working ends that run. When Dynamic
Dispatch starts again it cleans up what the agent left behind and picks the
story back up, keeping any work already saved, with a comment on the story
saying so. A story cut off twice in a row goes to Blocked instead.

A stop doesn't last past a reboot. To keep it off at boot as well:

    sudo systemctl disable --now dynamic-dispatch

## Read the logs

    journalctl -u dynamic-dispatch -f          # follow live; Ctrl-C to quit
    journalctl -u dynamic-dispatch -b          # everything since the last boot
    journalctl -u dynamic-dispatch --since "1 hour ago"

If it keeps failing to start, for example because `config.json` is missing or
readable by others, the reason is in the log as a "Cannot start" line. It
tries again every 10 seconds, up to three starts within five minutes, then
invokes the independent rollback helper described below.

## Running stories side by side

Each agent works on one story at a time, so with Claude and Codex up to two
stories can run at once. A third story waits in In Progress with a comment
explaining the wait. It starts by itself when an agent is free.

Keep `max_concurrent` at `1` for every agent in `config.json`. Cleaning up an
agent's processes stops everything its account runs, so two stories using
the same agent account would stop each other's work.

To see which agent has which story, look for the queued and running log lines:

    journalctl -u dynamic-dispatch --since today --no-pager | grep -E "queued|running"

## Updating itself after a merge

With `"self_update": "dynamic-dispatch"` in `config.json` (the name of the
project whose repository is DD itself), DD updates itself. After a merge, it
waits for its main check to pass on exactly main's latest commit. Then:

- If `dispatch/` or `main.py` changed, no new story run or refresh starts.
  Runs already going finish normally. Once nothing has been running for one
  poll, DD fast-forwards `$DD_CHECKOUT` to that commit and exits, and
  systemd starts it again on the new code. One silent alert then says
  "DD updated to <short commit> (<pull request title>)".
- If only documents or tests changed, including this README and any other
  `*.md` under `deploy/`, DD fast-forwards the checkout and keeps running.
  These Markdown documents alongside code changes follow the normal code
  update and restart path above.
- If any non-Markdown file under `deploy/` changed, DD doesn't update, even
  alongside code or document changes. Today these files are `dd-agent-run`,
  `sudoers-dynamic-dispatch`, `dynamic-dispatch.service`, `dd-rollback` and
  `dynamic-dispatch-rollback.service`; any non-Markdown
  file added later also blocks the update. DD sends one alert with sound
  naming only those files and the commands to run, from "When a deploy file
  changes" below; run them to update by hand.

Self-update only fast-forwards the checkout, without merge commits or resets. If it has
local changes, isn't on `main` (except an automatic rollback) or has commits `main`
doesn't, so it can't be fast-forwarded, DD sends one alert with sound saying
why and carries on with the code it has. Undo the local changes, or check out
`main` again, and it updates on its next poll. If the fast-forward itself
fails (a Git lock left behind, say), DD leaves the checkout as it was, logs a
warning without an alert and tries again on its next poll; a code update
keeps new runs waiting until then.

This relies on the service file's `Restart=on-failure`: DD exits with code
75 to restart itself, and systemd starts only a service that failed again.
Keep that setting. The project needs a `verify_command`, or main is never
checked and DD never updates itself. Without `self_update`, update by hand
as below.

## Updating by hand after a merge

An update is only needed when `dispatch/`, `main.py` or a non-Markdown file
under `deploy/` changed; tests or documents alone need nothing. Stories In
Progress don't need to finish first: a restart cuts them off, and they are picked back up when the
service starts again.

This pulls the latest code into the Pi checkout and restarts the service:

    cd "$DD_CHECKOUT" && git pull && sudo systemctl restart dynamic-dispatch

This shows the last five service log lines so you can check it started:

    journalctl -u dynamic-dispatch -n 5 --no-pager

The last line should say "watching ... polling every 30s".

## When a deploy file changes

After pulling changes, set the variables and render fresh templates using
“Choose the account and checkout” above. Install any changed deploy files below,
then restart as above.

For `deploy/dd-agent-run`, this installs the launcher with root ownership
and mode 755:

    sudo install -o root -g root -m 755 "$DD_CHECKOUT"/deploy/dd-agent-run /usr/local/bin/dd-agent-run

For `deploy/sudoers-dynamic-dispatch`, this checks the new sudoers rule
before installation:

    sudo visudo -cf "$DD_DEPLOY_TMP/sudoers-dynamic-dispatch"

Only if that command says "parsed OK", this installs the rule with root
ownership and mode 440:

    sudo install -o root -g root -m 440 "$DD_DEPLOY_TMP/sudoers-dynamic-dispatch" /etc/sudoers.d/dynamic-dispatch

This checks the complete sudoers configuration after installation:

    sudo visudo -c

For `deploy/dynamic-dispatch.service`, this installs the service file with
root ownership and mode 644:

    sudo install -o root -g root -m 644 "$DD_DEPLOY_TMP/dynamic-dispatch.service" /etc/systemd/system/dynamic-dispatch.service

For `deploy/dd-rollback` and `deploy/dynamic-dispatch-rollback.service`, install
both before enabling the main service's `OnFailure=` hook:

    sudo install -o root -g root -m 755 "$DD_CHECKOUT"/deploy/dd-rollback /usr/local/bin/dd-rollback
    sudo install -o root -g root -m 644 "$DD_CHECKOUT"/deploy/dynamic-dispatch-rollback.service /etc/systemd/system/dynamic-dispatch-rollback.service

The rollback unit is triggered by DD's service; do not enable it at boot.
These files are safety-sensitive and you install them; DD never does.
This README is documentation and does not need installation.

This tells systemd to reload its service definitions before you restart
as above:

    sudo systemctl daemon-reload

## Rolling back a bad update

Startup crash loops are handled automatically once the recovery files above
are installed. DD records its loaded commit as last good after ten continuous
minutes with completed polling cycles and no uncaught cycle error. A restart
or cycle error resets that timer. State is atomically replaced and fsynced in
`$DD_CHECKOUT/.dd-recovery/state.json`, outside the database and event
history. Allow ten healthy minutes after first installing this feature before
relying on recovery; older DD versions cannot write this record.

The service allows three starts in five minutes, including planned and manual
restarts. The next start hits the limit and `OnFailure=` runs the installed
`/usr/local/bin/dd-rollback`. Before exiting with code 75 for an update, DD
durably records a `planned_restart` timestamp in its recovery state. If that
restart was within the last five minutes, the helper logs why, runs
`systemctl reset-failed`, and restarts DD without changing the checkout or
sending a rollback alert. The marker expires after five minutes, so a crash
loop without a recent planned restart still triggers rollback.

For rollback, it stops DD, claims one recovery attempt durably, and switches the checkout
to the last good commit detached. It attempts one active Bark notification per
configured phone, with sound: "DD rolled back to <short commit> because
<new commit> kept failing to start", clears the service's start limit, and
restarts it. Notification is attempted before restart so slow delivery cannot
hide a second failure while the helper is still active. Delivery is best effort;
missing or broken Bark settings cannot prevent recovery. The helper uses the
existing optional `python3-cryptography` package for encrypted Bark delivery,
but imports no DD modules. Only systemd control runs as root; Git, recovery
state and alerts run as the installed service's `User`, in its
`WorkingDirectory`. The helper reads both with `systemctl show`; missing user
or non-absolute directory settings stop recovery before it changes anything.
No additional sudoers permission is needed.

The same last good commit is never tried twice in succession. If it also
fails, DD stays stopped and one sound alert says "DD is down and needs a hand".
Missing last good state, dirty tracked files, configured Git filters, or a failed
checkout also stop recovery with that alert. Inspect the recovery log with:

    journalctl -u dynamic-dispatch-rollback --no-pager -n 30

While automatically rolled back, self-update skips the failed commit, even if
main's tests pass. A newer tested commit on main can return the checkout to
main by fast-forward, after running work finishes. A healthy fallback keeps
the failed-commit guard; a newer version clears it after ten healthy minutes.
Manual detached checkouts without an automatic recovery record stay put.

Use the manual steps below for a running but misbehaving version, before any
last good record exists, or when automatic recovery has stopped and needs help.
First stop DD so it cannot race your checkout change:

    sudo systemctl stop dynamic-dispatch

This lists the last ten merges into main, newest first. The one to go back
to is the merge just before the one that broke things:

    cd "$DD_CHECKOUT" && git log --first-parent --oneline -10 main

This switches the Pi checkout to that version. Replace GOOD with the short
code at the start of its line:

    cd "$DD_CHECKOUT" && git checkout GOOD

For a manual rollback, clear any automatic recovery record while DD is stopped:

    rm -f "$DD_CHECKOUT/.dd-recovery/state.json"
    sudo systemctl reset-failed dynamic-dispatch

DD won't update itself from this manual detached checkout, so it stays put.
If the bad update changed a deploy file, install that file again from this
older version as described in "When a deploy file changes". Then restart and
check the log as described in "Updating by hand after a merge". Stories cut off by the
restart are picked back up, as with any restart.

Older versions work with the newer database and settings: DD's database
changes only ever add tables and columns, and settings an older version
doesn't know are ignored, so nothing needs undoing.

While rolled back, the checkout sits on an older version instead of main, so
`git pull` alone won't update it. Once the fix is merged, this returns to the
latest main and restarts:

    sudo systemctl stop dynamic-dispatch
    cd "$DD_CHECKOUT" && git checkout main && git pull
    rm -f "$DD_CHECKOUT/.dd-recovery/state.json"
    sudo systemctl reset-failed dynamic-dispatch
    sudo systemctl restart dynamic-dispatch

## Sprint-pull (Cycles)

Sprint-pull lets DD run a project's Plane Cycles: it moves a Cycle's
stories into Sprint Backlog, makes a plan item where the approver asks for
an assessment and approves the plan, starts the approved stories in order,
holds other work while the Cycle is active, and, on a Cycle's end date,
sends stories that never started back to Product Backlog with
the "Carried Over" label. A project without a `cycles` section keeps
sprint-pull off. To switch it on for a project:

1. Create the label. In Plane, open the project's settings, then Labels,
   and add a label named "Carried Over". DD only uses an existing label;
   it never creates one.
2. Add the settings to `config.json` (see "Adding a new setting" for
   editing it), starting from the `cycles` section in
   `config.example.json`:
   - `PLANE_PRODUCT_BACKLOG_STATE_ID` and `PLANE_SPRINT_BACKLOG_STATE_ID`:
     the `id` of the project's Product Backlog and Sprint Backlog states,
     from the `states/` request above.
   - `PLANE_CARRIED_OVER_LABEL_ID`: the `id` of the "Carried Over" label,
     from the same request with `labels/` in place of `states/`.
   - `PLANE_APPROVER_USER_ID` in `plane.approver_user_id`: the approver's
     Plane user ID: the `id` next to the approver's name in the same
     request with `members/` in place of `states/`. Any project with a
     `cycles` section needs it.
   - `done` and `cancelled` under the project's `states`, set to the Done
     and Cancelled states' IDs.
   - `max_stories` (default 10), `replan_quiet_minutes` (default 15) and
     `blocked_reminder_minutes` (default 60) can be left out to use their
     defaults.
3. Run the doctor, `python3 main.py --doctor` (see "Check the setup"). It
   checks the two backlog states, the label, the approver's project
   membership and `done`/`cancelled`. Fix anything it reports before going on.
4. Restart DD as in "Start, stop, restart".

Done and Cancelled stories both finish their part of a Cycle and release holds on
other work. Only Done satisfies a dependency: dependents of a Cancelled story
stay unstarted and are carried over at the end date.

DD shortens an approved Cycle once every story is finished, no Cycle work is
active, and its main check has passed on the current main after the final merge.
Main must not be broken, and no Fix main story may remain open. Until then DD
leaves dates alone and checks again each poll. The new end is the end of
yesterday in DD's local time zone, allowing a new Cycle today. If the Cycle
started today, it ends today instead, allowing the next one tomorrow. DD never
changes start dates, unapproved Cycles, or dates that have already ended.
A successful change sends one silent alert with the new date, remembered across
restarts. A refused Plane update records a warning and is retried later.

To switch it off again, remove the project's `cycles` section and restart.

## Adding a new setting

When a merged story says `config.json` needs a new setting, this opens the
Pi's configuration for editing:

    nano "$DD_CHECKOUT/config.json"

This checks the saved configuration and prints "config OK" if it loads
successfully:

    cd "$DD_CHECKOUT" && python3 -c "from dispatch import config; config.load(); print('config OK')"

Check it before restarting as above.

## Agent logins

Claude's login token lives in `/etc/dynamic-dispatch/claude.env`, owned by
`root:dd-agent` with mode 640. The file has one line beginning
`CLAUDE_CODE_OAUTH_TOKEN=` followed by the token, with no quotes and no
`export`.

Run this on another computer to create the Claude login token:

    claude setup-token

Codex's login lives in its own home at `/home/dd-codex/.codex/auth.json`,
readable only by `dd-codex`. Run the following commands from `/tmp` because
`dd-codex` cannot read most other folders.

This checks whether Codex is logged in as `dd-codex`:

    cd /tmp && sudo -u dd-codex -H codex login status

This starts a Codex login and shows a web address and code to enter on
another device, where you sign in with ChatGPT:

    cd /tmp && sudo -u dd-codex -H codex login --device-auth

### Adding Gemini CLI

For system setup, rerun the installer with all desired agents, for example
`sudo python3 deploy/dd-install --agents claude codex gemini`. It renders the
Gemini sudo permission automatically. The account and sudo commands below are
the manual alternative; credentials still need the separate steps below.

Gemini CLI can build stories too. DD reads its JSON output, in the format
Gemini CLI 0.59.0 prints. Like the other agents it needs its own account and
group, its own Plane account, and a login only it and DD can read. Here the
login is Vertex AI on Google Cloud.

Create the account and group, let DD's account into the group, and let the
new account reach the worktree area:

    sudo useradd --create-home --user-group dd-gemini
    sudo usermod -aG dd-gemini "$DD_USER"
    sudo usermod -aG ddwork dd-gemini

Install Gemini CLI so `gemini` is in `/usr/local/bin` or `/usr/bin`, where the
launcher looks, for example with `sudo npm install -g @google/gemini-cli`.

The sudoers template names only `dd-agent` and `dd-codex`. After rendering
it as above, add `dd-gemini` to the rendered copy, then validate and install
it as in "Install service files (manual reference)":

    sed -i 's/(dd-agent, dd-codex)/(dd-agent, dd-codex, dd-gemini)/' "$DD_DEPLOY_TMP/sudoers-dynamic-dispatch"

The template itself still names only `dd-agent` and `dd-codex`, so repeat
this step every time you render and install the sudoers rule again, for
example after an update changes the template. Otherwise the reinstalled rule
drops `dd-gemini` and DD can no longer start Gemini.

Gemini's settings live in `/etc/dynamic-dispatch/gemini.env`, owned by
`root:dd-gemini` with mode 640, in the same `KEY=VALUE` form as `claude.env`:

    sudo install -o root -g dd-gemini -m 640 /dev/null /etc/dynamic-dispatch/gemini.env
    sudo nano /etc/dynamic-dispatch/gemini.env

The file holds these lines, with your Google Cloud project's ID in place of
`YOUR_GCP_PROJECT_ID`:

    GOOGLE_GENAI_USE_VERTEXAI=true
    GOOGLE_CLOUD_PROJECT=YOUR_GCP_PROJECT_ID
    GOOGLE_CLOUD_LOCATION=global
    GOOGLE_APPLICATION_CREDENTIALS=/home/dd-gemini/.config/gemini-vertex-key.json

Keep `GOOGLE_CLOUD_LOCATION` at `global`: `us-central1` answers 404 for
current models. The project needs the Vertex AI API turned on.
`GOOGLE_APPLICATION_CREDENTIALS` names a key for a service account with the
Vertex AI User role. Create `/home/dd-gemini/.config` owned by `dd-gemini`
with mode 700, so only that account can reach it, then copy the key there
owned by `dd-gemini` with mode 600:

    sudo install -d -o dd-gemini -g dd-gemini -m 700 /home/dd-gemini/.config
    sudo install -o dd-gemini -g dd-gemini -m 600 vertex-key.json /home/dd-gemini/.config/gemini-vertex-key.json

DD scrubs the env file's values from what it stores and posts, but it can't
read the key file, as with Codex's login.

Then add the agent to `agents` in `config.json`, with the Gemini Plane
account's token in place of `PLANE_GEMINI_PAT`, and add `gemini` to each
project's `agents` list:

```json
"gemini": {
  "max_concurrent": 1,
  "display_name": "Gemini",
  "plane_token": "PLANE_GEMINI_PAT",
  "prompt_via": "stdin",
  "command": ["gemini", "--yolo", "--output-format", "json", "-m", "gemini-2.5-pro"],
  "parser": "gemini_json",
  "run_as": "dd-gemini",
  "group": "dd-gemini",
  "env_file": "/etc/dynamic-dispatch/gemini.env",
  "git_author": {"name": "Gemini", "email": "gemini@example.com"},
  "timeout_seconds": 1800
}
```

`--yolo` approves every tool call without asking, as the other agents' flags
do; the account is what contains it. `-m` pins the model so a new default
can't change what builds stories; use a model your project can reach in the
`global` location. With `prompt_via` set to `stdin`, Gemini CLI reads the
story from its input and runs headless, which keeps long prompts off the
command line. If your version doesn't, put `"-p", "{prompt}"` at the end of
the command instead and remove `prompt_via`.

This checks the login as `dd-gemini`, using the same settings:

    cd /tmp && echo "Reply with OK" | sudo -u dd-gemini -H bash -c 'set -a; . /etc/dynamic-dispatch/gemini.env; exec gemini --output-format json -m gemini-2.5-pro'

A working login prints a JSON object with `"response"`. A failure prints
`"error"` with a type, message and code instead; exit code 1 is an API or
model error and 52 means Gemini refused its settings. When DD runs Gemini, a
rejected login (for example 401, UNAUTHENTICATED, or missing or invalid
credentials) sends a login alert, and running out of quota (for example 429
or RESOURCE_EXHAUSTED) cools Gemini down like any other usage limit. Gemini
gives no reset time, so the cool-down lasts `usage_limit_cooldown_minutes`.

## Tokens and when they expire

- The fine-grained GitHub token named "dynamic-dispatch-pi4" was regenerated
  on 28 Sept 2026 for one year, so it expires around Sept 2027. It is stored
  in two places: `github.token` in `$DD_CHECKOUT/config.json` and
  `~/.git-credentials`. Update both when it is renewed. The credentials file
  holds one line exactly of the form `https://<USERNAME>:<TOKEN>@github.com`,
  with your GitHub username and the real token in their places and nothing
  else on the line.
  A missing `@github.com` makes Git ask for a username.
- The Claude login token was made in Sept 2026 and expires about a year
  later. It lives in `/etc/dynamic-dispatch/claude.env`.
- The GitHub App "ND Dispatch" private key lives at
  `~/.config/dynamic-dispatch/github-app.pem` with mode 600 and does not
  expire.
- The Plane tokens for Orchestrator, Claude ("dispatch-claude-code-pi4")
  and Codex ("dispatch-codex-pi4") each live in
  `$DD_CHECKOUT/config.json`. They expire as set when they were
  created. A Plane app's client secret lives there too; DD renews the app's
  access token itself and never saves it.

Never paste `config.json`, `~/.git-credentials`, env files or tokens
anywhere. Keep login files and private keys private too; these notes record
where secrets live, never their values.

## The Dynamic Dispatch light

Each open pull request DD manages shows a "Dynamic Dispatch" check next to
the merge button:

- Yellow, "Being reworked: don't merge": the story is back in In Progress,
  including while it waits for an agent. The next check sets it on the current
  commit. A new commit gets its normal light; leaving In Progress restores
  the normal light even if no commit changed.
- Yellow, "Updating with latest main": main has moved on and DD is
  combining the pull request with it and testing it. Wait.
- Green, "Up to date and tested": the pull request contains the latest main
  and DD's tests passed on exactly this commit. Safe to merge.
- Red, "Blocked: see the story in Plane": the story is in Blocked, either
  because DD couldn't finish (its comment says why) or because you moved it
  there. It follows the story: move it out of Blocked and the red goes on
  DD's next check.

A commit with no light yet has not been tested by DD. Merge only on green.

With `github.approver` set to your GitHub username in `config.json`, green
also needs your approval of exactly the code as it stands:

- Yellow, "Waiting for your approval": tested, but you haven't approved it.
  Approve it on GitHub ("Review changes", then "Approve") and it turns green
  on DD's next check.
- Yellow, "Changed since your approval: re-review": an agent changed the
  code after you approved it. Review it again.
- Green, "Up to date, tested and approved": safe to merge.

Only an "Approve" review from the `github.approver` account counts. Your
approval still counts when the only commits after it are DD's clean
"Combine latest main" commits, where main merged in without conflicts and no
agent changed anything. A refresh that needed an agent to repair a clash or
failing tests voids it: DD comments on the pull request with the commit that
changed it and sends an alert with sound, "PR #... changed since your
approval". Rework you asked for voids it too, without that extra alert.
Without `github.approver`, green means up to date and tested, as before.

The GitHub App (or the plain token, without an App) needs the
**Commit statuses: Read and write** permission for this. Add it in the
App's settings on GitHub under Permissions, then accept the new permission
on the installation. Until then, DD records a warning each time it tries to
set a light and carries on; stories are not affected, and the lights appear
on the next check once the permission is granted.

## Main checks

Whenever main moves to a new commit, DD tests main itself: it runs the
project's `verify_command` on exactly that commit, in a folder called
`main-check` in the project's worktree root. That folder is only for main
checks and is rebuilt every time; don't keep anything in it. The check
borrows a free agent's account, because the tests must run sandboxed, but
no agent runs. While it runs, that agent can't start a story. If every
agent is busy, the check waits and gets the next account to come free,
before any new story or refresh; work already running is never stopped
for it. Projects without a `verify_command`
are not checked.

If the tests fail, DD runs them once more. Main counts as failing only when
both runs fail:

- Failing twice sends one alert with sound, "Main is failing tests since
  <pull request title>", naming up to five failing tests.
- A later commit that passes sends one alert, "Main is passing again".
- Failing once and then passing only records a warning naming the test.

Main's state survives restarts, so a restart doesn't repeat an alert. If
Bark can't deliver a main alert to one of your phones, DD records a warning
and sends it again on its next check, to every phone, so one that already
got it may see it twice. An alert still waiting to be sent when DD stops is
lost. Every new commit on main is checked, including while main is failing.
If a check can't run (folder preparation, GitHub or the sandbox fails), DD
records a warning and keeps main's last green or red state. After three
consecutive checks without a result, it sends one alert with sound,
"DD can't test main: <short reason>". Later failures don't repeat it, and
checks retry every ten minutes (on the next poll when due), instead of
every cycle. Waiting for a free agent account doesn't count as a failure.
The count and retry deadline survive restarts.

The next check with a result, passing or failing, clears the count and
restores the normal pace. If the outage alert was sent, DD also sends
"DD can test main again". Even unchanged main is checked after an outage.
Failed alert delivery is owed and retried each poll, including during the
ten-minute delay, just like main's result alerts. Being unable to test
adds no story failure or hold; any hold from main's last red result remains.

To see the checks:

    journalctl -u dynamic-dispatch --since today --no-pager | grep "main"

Alerts need the optional phone alert settings below. Without them, main's
state is still kept and the log still shows it.

## Known cautions

- After upgrading Git on the Pi, run the full suite,
  `python3 -m unittest discover -s tests`, before restarting DD. DD's Git
  safety checks are tested in `tests/test_git_safety.py`; start there if
  anything fails.
- Git LFS must not be installed on the Pi. DD refuses to work while any
  Git filter is configured.

### Optional encrypted phone alerts

Install the one optional Python dependency on the Pi that runs DD:

    sudo apt install python3-cryptography

Add the `alerts` section from `config.example.json` to your private
`config.json`. Set `bark_url` to your Bark server's HTTP or HTTPS base URL,
`device_keys` to every phone's Bark device key, and `encryption_key` to the
same 32 ASCII characters configured in each Bark app for AES-256-GCM.
Replace all example placeholders. Restart DD after changing these settings.
Keep the config mode at 600; these keys are credentials.

Review and blocked notifications have sound; starts and other Orchestrator
comments are silent. Review notifications open the pull request. Content,
level and links are encrypted; the server sees the device route and a fresh
IV, but not the notification content. Bodies are shortened to keep the whole
form under 3,000 bytes. Delivery is best-effort in a background queue; failures
record warnings without interrupting stories. Queued alerts can be lost on
shutdown. Remove the section to disable alerts. A missing library or invalid
settings produce one startup warning and disable alerts.

### Login health alerts

The same optional Bark settings also enable sound alerts for rejected Plane
logins (Orchestrator or agent, by token or Plane app), GitHub API/App logins, Git push authentication,
and Claude, Codex and Gemini logins. Alerts name the login and where DD noticed the failure,
without copying credentials or error output. They work even when Plane is
unavailable. Each login alerts once until it succeeds and fails again; this
state survives DD restarts. Git push uses the shared host login described above.

DD checks `github.token` once per UTC day in the background, even when PRs use
a GitHub App. GitHub's expiry header triggers one alert at each of 30, 7 and 1
days remaining. If DD first sees a token already inside several thresholds,
it sends each crossed threshold once. A missing token, unreadable expiry or
network failure records at most one daily system warning event (no story).
These checks never hold up stories. Claude and Plane expiry are not checked.

Keep phone sounds enabled for Bark's active notifications. Without configured
Bark delivery, daily warning events still work. Delivery is best effort, as
with story alerts: queued messages may be lost at shutdown, and a failed
notification delivery does not rearm an already recorded alert.

### Check the setup

Run `python3 main.py --doctor` from the checkout (or add `--config /path/to/config.json`).
Each check prints OK, WARN, FAIL or SKIP with a reason and guidance. Checks whose
prerequisites failed are skipped; SKIP does not affect the exit code. Any FAIL gives
exit code 1. Projects and agents are numbered in config order so config values
never appear in diagnostics. Fix failures and run the command again.

Doctor does not start DD, send alerts, change files or write repository data.
An App login issues an installation token and reads its reported permissions.
The App needs only read access to contents, since DD pushes with the host's
saved Git login, never the App; it needs write access to pull requests and
commit statuses. Personal token write permissions cannot be verified without a
write, so they produce a warning. Personal token expiry within 30 days produces a warning; an
expired personal token fails. App expiry reports OK because App tokens renew
automatically. Database writability is checked
using filesystem access rights without creating a database or journal; it does
not test free space or concurrent locks. Bark settings are validated without
sending a notification.

The doctor also checks the rest of the host:

- Python: DD needs 3.11 or newer.
- Git push login: for each project, `git ls-remote` from its `repo_path`
  reaches origin with the host's saved Git login (see "Tokens and when they
  expire"). It pushes nothing, so write access is reported as a warning; a
  rejected push raises a login alert on DD's first push.
- Agents: each agent's command is on the launcher's PATH (`/usr/local/bin`,
  `/usr/bin` or `/bin`), its account exists, and its env file, if it has one,
  is owned by root and the agent's group with mode 640 (see "Agent logins").
  The env file's metadata is read, never its contents. The model login itself
  can't be checked without running the agent, so it is a warning; DD's login
  alerts report a rejected login on the first run.
- Sprint-pull: for each project with a `cycles` section, its Product
  Backlog and Sprint Backlog states and its carried-over label exist in the
  project, the approver's Plane user ID is a project member, and `done` is
  set under `states`. A project without the section gets none of these.
- Service: `dynamic-dispatch.service` is installed with `User` and
  `WorkingDirectory` matching the account and checkout running the doctor;
  `dynamic-dispatch-rollback.service` and `/usr/local/bin/dd-rollback` are
  installed; `/usr/local/bin/dd-agent-run` is owned by root with mode 755;
  and `sudo -n -l` shows that DD's account may run it as each agent's
  account. Both use that installed path, not the configured
  `sandbox.launcher`. Each failure names the file to reinstall, as described in
  "Install (once)" and "When a deploy file changes" above. Run the doctor as
  DD's account, from its checkout, or the service checks report a mismatch.

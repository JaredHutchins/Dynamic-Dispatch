# Run DD in a container

This guide takes a fresh installation through a working container with any
mix of DD's agents: Claude Code, Codex and Gemini. The supplied image
supports ARM64 and Intel/AMD64 and installs Python, Git, sudo and, for each
agent you choose when building it, that agent's tool at a pinned version:
Claude Code 2.1.283, Codex 0.158.0 and Gemini CLI 0.59.0. Without a choice it
includes Gemini only, as earlier images did. DD runs as `dd`; Claude Code runs
as `dd-agent`, Codex as `dd-codex` and Gemini as `dd-gemini`, each with its own
group, sudo rule and home volume, as on a Linux host. No privileged container
or Docker socket mount is needed.

## 1. What you need

- Docker with the Compose plugin, running on Linux or Docker Desktop on a Mac.
  Colima with Docker and Compose installed is another Mac option; start it
  with `colima start`. Check `docker info` and `docker compose version`.
- Git on the host and a checkout of DD. From this repository's GitHub page,
  copy its HTTPS clone URL, run `git clone URL dynamic-dispatch` (replace
  `URL`), then `cd dynamic-dispatch`. All host commands below start here.
- A GitHub repository with at least one commit. Create a GitHub App with
  Contents: read, Pull requests: write and Commit statuses: write, and install
  it on that repository. Save its private key outside the checkout. A
  fine-grained token can replace the App for normal operation, but guided
  project creation (`--add-project`) requires the App. DD also needs a
  separate HTTPS Git login with write access for pushing commits.
- A Plane workspace, with a separate identity for the Orchestrator (DD's
  Plane account) and for each agent you include. Follow
  [Set up Plane Cloud](PLANE_CLOUD.md) to create and install the apps, or
  prepare each identity's personal token. Note the workspace slug,
  credentials and approver's GitHub username. For Cycles, also obtain the
  approver's Plane user ID as described there. Use this guide's container
  commands instead of that page's host installer and systemd commands. Its
  install-link helper can run in the container shell opened in step 5.
- For each agent you include, a way for it to log in:
  - **Claude Code:** a Claude subscription that includes Claude Code, to
    create a long-lived login token with `claude setup-token` on another
    computer, or an Anthropic API key.
  - **Codex:** a ChatGPT plan that includes Codex, to sign in from a browser
    on another device, or an OpenAI API key.
  - **Gemini:** a Google Cloud project with billing and the Vertex AI API
    enabled, access to the configured model, and permission to arrange the
    login and spending controls in step 3.

## 2. Choose the agents

The image's `DD_AGENTS` build setting lists the agents it includes, separated
by spaces: any of `claude`, `codex` and `gemini`. Record your choice in a
host-local Compose override file, so every later build uses the same agents.
Keep only the names you want on the `DD_AGENTS` line:

```sh
export DD_CONTAINER_OVERRIDE="$HOME/.config/dd/container.yaml"
mkdir -p "$(dirname "$DD_CONTAINER_OVERRIDE")"
cat > "$DD_CONTAINER_OVERRIDE" <<'YAML'
services:
  dd:
    build:
      args:
        DD_AGENTS: claude codex gemini
YAML
```

The override is a new host-local file, not a supplied repository file. The
build stops with an error naming a word it doesn't know. The image installs
each chosen tool, creates its account with a fixed ID and runs
`deploy/dd-install --container` for exactly those agents; every start runs the
installer again for the same agents. The other agents' tools, accounts and
sudo rules are not in the image, and their home volumes stay empty.

To change the agents later, edit the override and rebuild with
`dc up -d --build` (step 5 defines `dc`). Before leaving an agent out, remove
it from `agents` and every project's `agents` list in `config.json`, or DD
cannot run it. After adding one, run setup again (step 5) to add it to
`config.json`, then log it in (step 6).

## 3. Gemini only: a limited Google login and usage limits

Skip this step if you did not include Gemini.

### Give Gemini a limited Google login

Use a dedicated service account in a dedicated project, permitted to use
Vertex AI only. Ask the administrator to grant Vertex AI User
(`roles/aiplatform.user`) in that project, with no unrelated roles or access
in other projects. Do not grant Owner, Editor, IAM administration or quota
administration. Have the administrator review inherited permissions too.

Create a JSON key for that service account. Keep it outside both the image
and the repository, in a private host directory, with file mode 600. Never
paste its contents into a command, config example or story. See Google's
[service-account key instructions](https://docs.cloud.google.com/iam/docs/keys-create-delete).

**Using a person's own Google login instead lets the agent reach everything
that person can reach in Google Cloud.** The Linux account sandbox does not
reduce the Google permissions carried by a credential. Do not use a broad
personal login to get past a service-account restriction.

If your organization blocks service-account key creation, ask its Google
Cloud administrator for an approved way for this container to authenticate
as a Vertex-only service account: for example, workload identity federation
or service-account impersonation supported by the installed Gemini CLI.
Alternatively request a narrowly scoped key-policy exception if policy allows
it. Ask for the credential configuration, renewal procedure and any required
mounts; do not disable the organization policy yourself. The key-file steps
below require an approved key; a keyless arrangement needs administrator
validation before continuing. See [Google's policy documentation](https://docs.cloud.google.com/organization-policy/restrict-service-accounts)
and [Gemini authentication](https://geminicli.com/docs/get-started/authentication/).

### Set usage limits before the first Gemini run

In the Google Cloud console, select the project, open **IAM & Admin → Quotas
& System Limits**, and filter for the Vertex AI API. Find the adjustable
inference quota for the model and location you will actually use. Lower its
request or token allowance to a conservative starting value and wait until
that limit is effective. Ask an administrator if you cannot change it.

Do not assume an unrelated quota limits Gemini: some models use shared
capacity without an adjustable per-project inference quota. If no applicable
quota exists, ask the administrator to establish an enforceable usage control
for that model before unattended operation. A rate quota is not a monthly
money cap. Consult the [Vertex AI quota documentation](https://cloud.google.com/vertex-ai/generative-ai/docs/quotas)
for the selected model.

Then open **Billing → Budgets & alerts**, create a budget scoped to this
project, choose an amount you can afford, and add actual-spend alerts (for
example at 50%, 80% and 100%) to an inbox someone monitors. Confirm the
recipients. An alerts-only budget does not stop requests or cap charges;
notifications may arrive after spending occurs. See [Google's budget guide](https://docs.cloud.google.com/billing/docs/how-to/budgets).
Do this before the login smoke test as well as before running stories.

## 4. Keep secrets on volumes

Guided setup writes `/home/dd/config/config.json` on the `dd-config` Docker
volume, owned by `dd` with mode 600, inside a directory with mode 700. Only
DD's account (and container root) can read it. Plane credentials and the
GitHub API credentials belong there, never in the image or repository.
Setup's `config.json.backup-*` files are equally private and contain secrets.

DD's Git login lives on `dd-home`; its App key can live on `dd-config`.
The other named volumes retain the checkout, history database, project clones,
story worktrees, agent environment files and each agent's home
(`dd-claude-home`, `dd-codex-home` and `dd-gemini-home`). Each agent's home
is private to its account, so neither DD nor another agent can read a login
kept there. Protect backups of all these volumes. `docker compose down`
retains them; adding `-v` deletes them, including credentials and work.

If you included Gemini, set this host variable to the absolute path of your
existing Google key, then add the key's mount to the override from step 2,
keeping your own `DD_AGENTS` line:

```sh
export DD_GEMINI_CREDENTIAL=/absolute/path/outside-the-repository/vertex-key.json
chmod 600 "$DD_GEMINI_CREDENTIAL"
cat > "$DD_CONTAINER_OVERRIDE" <<'YAML'
services:
  dd:
    build:
      args:
        DD_AGENTS: claude codex gemini
    volumes:
      - type: bind
        source: ${DD_GEMINI_CREDENTIAL:?Set the absolute Google key path}
        target: /etc/dd-credentials/gemini.json
        read_only: true
        bind:
          create_host_path: false
YAML
```

It mounts only the key, read-only. The start step copies it to
`/home/dd-gemini/gemini-credential.json`, owned by `dd-gemini`, mode 400.
Gemini's home is private; DD and other agents cannot read that copy. Removing
the mount removes the copy on the next start. Never mount a directory over
`/etc/dd-credentials`: that directory must remain root-only.

## 5. Build, start and configure

Define this host shell helper so every command uses the same override and
Compose project. In a new terminal, return to this checkout and set
`DD_CONTAINER_OVERRIDE` (and `DD_GEMINI_CREDENTIAL`, with Gemini) again
before defining the helper.

```sh
dc() { docker compose -f deploy/compose.yaml -f "$DD_CONTAINER_OVERRIDE" "$@"; }
dc up -d --build
dc logs --tail=50 dd
```

First start clones public DD `main` into `/home/dd/checkout`; the image does
not contain your host checkout or config. Wait for the clone and installer
to finish (check logs again). Missing config failures are expected now.
Stop the service while setting it up, so it cannot begin processing stories:

```sh
dc stop dd
dc run --rm --user dd --entrypoint /bin/bash dd
```

You are now in a temporary container shell sharing the service's volumes.
It starts in `/home/dd/checkout`. Run the Plane install-link helper from
[PLANE_CLOUD.md](PLANE_CLOUD.md#3-install-each-app) here if using Plane apps.
For a GitHub App, in another host terminal use the following to copy its key
onto the config volume (replace the source path):

```sh
dc run --rm -T --user dd --entrypoint /bin/sh dd -c \
  'umask 077; cat > /home/dd/config/github-app.pem' < /absolute/path/to/github-app.pem
```

Back in the container shell, save a Git push login without putting the token
in shell history. This helper prompts privately and stores it on DD's home
volume. Use a token with repository Contents: read and write permission:

```sh
python3 - <<'PY'
import getpass
import os
from pathlib import Path
import subprocess
os.umask(0o077)
username = input('GitHub username: ').strip()
token = getpass.getpass('GitHub Git push token: ')
subprocess.run(['git', 'config', '--global', 'credential.helper', 'store'], check=True)
subprocess.run(['git', 'credential', 'approve'], input=
               f'protocol=https\nhost=github.com\nusername={username}\npassword={token}\n\n',
               text=True, check=True)
Path.home().joinpath('.git-credentials').chmod(0o600)
PY
python3 main.py --setup --config /home/dd/config/config.json
```

When setup asks for the agents, it suggests the ones the image includes;
choose exactly those. For the App key path enter
`/home/dd/config/github-app.pem`. Setup checks the Plane identities, GitHub
login and approver, then offers to add the first project. Accept, and supply
its repository, name and test command. It creates the Plane columns and
labels and clones the repository. Leave the default roots at
`/srv/dynamic-dispatch/repos` and `/srv/dynamic-dispatch/worktrees`.
If onboarding fails, fix the reported problem and rerun:

```sh
python3 main.py --add-project --config /home/dd/config/config.json
```

A token-only installation must instead configure an existing project using
the [manual config reference](README.md#set-up-configjson-manual-reference);
it cannot use this automatic project-creation step.

Enable public updates and ensure history stays on its volume. This reads the
public repository name from the supplied launcher, avoiding a second copy
of that address. Run in the same container shell:

```sh
python3 - <<'PY'
import json
import runpy
from pathlib import Path
path = Path('/home/dd/config/config.json')
cfg = json.loads(path.read_text())
repo = runpy.run_path('deploy/dd-container-start')['PUBLIC_REPO']
cfg['self_update_public_repo'] = repo.removeprefix('https://github.com/').removesuffix('.git')
cfg['db_path'] = '/home/dd/.local/share/dynamic-dispatch/runs.db'
path.write_text(json.dumps(cfg, indent=2) + '\n')
path.chmod(0o600)
PY
exit
```

## 6. Log each agent in

Do the part for each agent you included, from the host. Each login is
readable only by its agent's account (and DD, for env files, so it can scrub
their values from what it stores and posts). An env file lives on the
`dd-agent-env` volume, owned by `root` and the agent's own group with mode
640; a login an agent saves itself lives in its home volume. Never paste a
token or key into a command line, config example or story: each command
below reads it from a prompt or a file.

Each smoke test makes one small, paid model call as the agent's account.
Doctor (step 7) checks access and sandbox setup but makes no model call.

### Claude Code

**Privacy:** with a Claude subscription, open Claude's settings, find
**Privacy** and turn off the setting that lets Anthropic use your chats and
coding sessions to improve its models. An Anthropic API key is covered by
Anthropic's commercial terms, under which its inputs and outputs aren't used
for training by default. Check the provider's current settings and terms
before the first run.

Claude Code reads its login from `/etc/dynamic-dispatch/claude.env`. On
another computer with Claude Code, run `claude setup-token` to create a
long-lived subscription token. Then write the env file; the prompt doesn't
echo what you paste:

```sh
dc run --rm --user root --entrypoint /bin/bash dd -c \
  'read -rsp "Claude token: " token; echo; umask 027;
   printf "CLAUDE_CODE_OAUTH_TOKEN=%s\n" "$token" > /etc/dynamic-dispatch/claude.env;
   chown root:dd-agent /etc/dynamic-dispatch/claude.env; chmod 640 /etc/dynamic-dispatch/claude.env'
```

With an API key instead, replace `CLAUDE_CODE_OAUTH_TOKEN` with
`ANTHROPIC_API_KEY`. Keep only one of the two lines in the file. Setup already
points the `claude-code` agent at this file. Smoke test:

```sh
dc run --rm --user dd-agent --workdir /tmp --entrypoint /bin/bash dd -c \
  'set -a; . /etc/dynamic-dispatch/claude.env; printf "Reply with OK\n" | claude -p --output-format json'
```

A working login prints JSON with `"result"`. The subscription token expires
after about a year; write the file again with a new one.

### Codex

**Privacy:** signed in with ChatGPT, open ChatGPT's **Settings → Data
controls** and turn off **Improve the model for everyone**, so Codex sessions
aren't used for training; ChatGPT business plans exclude them by default. An
OpenAI API key's data isn't used for training by default. Check the
provider's current settings and terms before the first run.

To sign in with ChatGPT, start a device login as Codex's account. It shows a
web address and a code to enter on another device, then saves the login in
`/home/dd-codex/.codex/auth.json` on Codex's home volume, readable only by
`dd-codex`. If ChatGPT refuses the code, allow device code sign-in for Codex
in ChatGPT's security settings and try again:

```sh
dc run --rm --user dd-codex --workdir /home/dd-codex --entrypoint codex dd login --device-auth
dc run --rm --user dd-codex --workdir /home/dd-codex --entrypoint codex dd login status
```

To use an OpenAI API key instead, write Codex's env file and point the agent
at it:

```sh
dc run --rm --user root --entrypoint /bin/bash dd -c \
  'read -rsp "OpenAI API key: " key; echo; umask 027;
   printf "CODEX_API_KEY=%s\n" "$key" > /etc/dynamic-dispatch/codex.env;
   chown root:dd-codex /etc/dynamic-dispatch/codex.env; chmod 640 /etc/dynamic-dispatch/codex.env'
dc run --rm -T --user dd --entrypoint python3 dd - <<'PY'
import json
from pathlib import Path
path = Path('/home/dd/config/config.json')
cfg = json.loads(path.read_text())
cfg['agents']['codex']['env_file'] = '/etc/dynamic-dispatch/codex.env'
path.write_text(json.dumps(cfg, indent=2) + '\n')
path.chmod(0o600)
PY
```

Smoke test, which works with either login:

```sh
dc run --rm --user dd-codex --workdir /tmp --entrypoint /bin/bash dd -c \
  'set -a; [ ! -e /etc/dynamic-dispatch/codex.env ] || . /etc/dynamic-dispatch/codex.env;
   printf "Reply with OK\n" | codex exec --json --skip-git-repo-check -'
```

A working login prints JSON events ending in `turn.completed`.

### Gemini

**Privacy:** Gemini here uses Vertex AI with the service account from step 3.
Google Cloud's terms say Vertex AI doesn't use your prompts and responses to
train its models without your permission, so there is no setting to turn
off. Don't switch to a free Gemini API key or a personal Google login, whose
terms may let Google use your prompts to improve its products. Check the
provider's current terms before the first run.

The mounted key from step 4 is Gemini's credential. Write Gemini's
environment file, replacing the project ID; these lines contain settings and a
credential path, not the key itself:

```sh
dc run --rm -T --user root --entrypoint /bin/sh dd -c \
  'umask 027; cat > /etc/dynamic-dispatch/gemini.env; chown root:dd-gemini /etc/dynamic-dispatch/gemini.env; chmod 640 /etc/dynamic-dispatch/gemini.env' <<'ENV'
GOOGLE_GENAI_USE_VERTEXAI=true
GOOGLE_CLOUD_PROJECT=YOUR_GCP_PROJECT_ID
GOOGLE_CLOUD_LOCATION=global
GOOGLE_APPLICATION_CREDENTIALS=/home/dd-gemini/gemini-credential.json
ENV
```

The first start in step 5 already copied the key into Gemini's home. Smoke
test:

```sh
dc run --rm --user dd-gemini --workdir /tmp --entrypoint /bin/bash dd -c \
  'set -a; . /etc/dynamic-dispatch/gemini.env; printf "Reply with OK\n" | gemini --output-format json -m gemini-2.5-pro'
```

A successful smoke test returns JSON with `response`. The configured model
is `gemini-2.5-pro`; confirm your project can still use it, or choose an
available model in `agents.gemini.command` and use the same model in the
smoke test.

## 7. Check and start

The initial setup's doctor may have reported missing env files; now rerun it
explicitly:

```sh
dc run --rm --user dd --entrypoint python3 dd main.py --doctor --config /home/dd/config/config.json
```

Besides its usual checks, in the container it reports which agents the image
includes and, for each, that its tool is installed, its account exists, its
home volume is writable by it, and its env file and saved login are readable
only by that agent. Resolve every FAIL before continuing. Install any
project-specific test tools before handing it stories; the supplied image
has only the tools listed above.

If the first starts reached recovery's failure limit, follow the latch reset
in troubleshooting before starting. Ignore any systemd start command printed
by setup; in this installation use:

```sh
dc start dd
dc logs --tail=50 dd
dc exec --user dd dd python3 main.py --doctor --config /home/dd/config/config.json
```

Look for `watching ... polling every 30s` (or your configured interval), no
repeated startup failures, and a passing doctor. Docker's `Up` status alone
does not prove DD is healthy. Move a small story to In Progress in Plane for
each agent and check that it picks the story up.

## 8. Day to day

From the host, with the helper and variables from step 5:

```sh
dc logs --tail=100 dd
dc logs -f dd                 # Ctrl-C stops following logs, not DD
dc stop dd
dc start dd
dc restart dd                # after editing config or agent settings
```

Stopping ends active agent runs; restart recovery preserves saved work and
picks the story back up. Keep the same checkout directory/Compose project
name so these commands use the same named volumes.

With `self_update_public_repo` set, DD checks public `main` about every five
minutes. It accepts an update only when GitHub Actions checks on that exact
commit exist and all have completed successfully. Missing, pending or failed
checks leave it on the current version. It waits for active work to finish,
updates and exits; Docker restarts it. Documentation-only updates can
fast-forward the checkout without restarting the running process. The initial clone is simply public
`main`, not a CI-gated update. Check the repository's main test badge before
first installation.

On each start, the root start step installs the launcher and sudoers from
DD's checkout before dropping to `dd`. That checkout is trusted executable
code. Updates needing changed installed deployment files can be held for
manual installation; see [Updating itself](README.md#updating-itself-after-a-merge).
Image, Compose and container-start changes require an updated host checkout
and `dc up -d --build`; rebuilding does not replace the persistent DD checkout.

To see the version actually reported by DD's last polling cycle and its time:

```sh
dc exec --user dd dd python3 -c 'from dispatch import runs; c = runs.connect("/home/dd/.local/share/dynamic-dispatch/runs.db", initialize=False); h = runs.heartbeat(c); print(dict(h) if h else "No heartbeat yet"); c.close()'
```

The `version` is the running commit SHA. A stale heartbeat means DD is not
currently polling. `git rev-parse HEAD` alone would show the checkout, which
may have changed since the process started.

## 9. Keep a Mac awake

The Mac, its container runtime and network must stay running. Keep it plugged
in with its lid open. For one terminal session, run `caffeinate -i` and leave
it running; Ctrl-C releases the idle-sleep prevention. This does not prevent
manual sleep or guarantee operation with the lid closed.

For a login-session launch agent, create this host file:

```sh
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$HOME/Library/LaunchAgents/local.dd.awake.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>local.dd.awake</string>
  <key>ProgramArguments</key>
  <array><string>/usr/bin/caffeinate</string><string>-i</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict>
</plist>
PLIST
plutil -lint "$HOME/Library/LaunchAgents/local.dd.awake.plist"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/local.dd.awake.plist"
```

This prevents idle system sleep for the whole login session, even when DD is
stopped. It does not start Docker Desktop or Colima: arrange their startup
separately. To stop it and allow idle sleep again:

```sh
launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/local.dd.awake.plist"
rm "$HOME/Library/LaunchAgents/local.dd.awake.plist"
```

## 10. Troubleshooting

| Symptom | What to check |
|---|---|
| Docker cannot connect | Start Docker Desktop or `colima start`; check `docker info`. |
| Clone/build fails | Check host and container DNS, HTTPS access to GitHub and package registries, and available disk space. The checkout volume must be empty or a valid Git checkout. |
| Missing config or permission failure | Use `/home/dd/config/config.json` with every setup/doctor command, owned by `dd`, mode 600. Run setup as `dd`, never root. |
| Plane rejects login | Cloud uses `https://app.plane.so` for web links and `https://api.plane.so` for API calls. Check workspace slug, app installation, permissions and distinct identities in [Plane Cloud setup](PLANE_CLOUD.md). |
| GitHub API works but cloning/pushing fails | The App and Git push login are separate. Check the saved Git login as `dd`, repository access and Contents write permission. `--add-project` requires an App; read access alone does not prove push access. |
| Build stops at `DD_AGENTS` | Use only `claude`, `codex` and `gemini`, separated by spaces, on the override's `DD_AGENTS` line. |
| Claude Code 401 or "invalid API key" | Check `claude.env` has one line, `CLAUDE_CODE_OAUTH_TOKEN=` or `ANTHROPIC_API_KEY=` followed by the value with no quotes, and that the token hasn't expired. Do not print the file to debug it. |
| Codex says it isn't logged in | Run the `codex login status` command from step 6 as `dd-codex`, and sign in again if needed. With an API key, check `codex.env` and the agent's `env_file` in config. |
| Gemini 401/403 or missing credentials | Check the mounted file exists, its service account roles, Vertex API enablement and project ID. Confirm the env file points to the private copy. Do not print the key to debug it. |
| Gemini 404 | Check model availability in your project and that the env file uses the intended location (`global` here). Match the smoke-test model to config. |
| Gemini 429 / RESOURCE_EXHAUSTED | Check the applicable quota and usage. DD cools the agent down for `usage_limit_cooldown_minutes`; do not raise limits without reviewing spending. |
| Doctor reports a missing tool or account | The image doesn't include that agent. Add it to `DD_AGENTS` and rebuild, or remove it from `config.json`. |
| Doctor reports sudo, group or env failures | Confirm setup selected exactly the image's agents, and each env file is owned by `root` and its agent's group (`dd-agent`, `dd-codex` or `dd-gemini`) with mode 640. Restart to rerun the container installer; inspect its log before changing sandbox permissions. |
| Tests cannot find a tool | Supply the project's test dependencies in the container; installing them on the host does not install them for the agents. Ad-hoc container installs disappear on recreation. |
| No stories start | Check a fresh heartbeat, doctor results, Plane state/assignment, GitHub main checks and quota cooldown. The Mac or container runtime may have slept. |
| No update arrives | Check exact-main GitHub Actions results, network/rate-limit warnings, active runs and any deployment-file hold in logs. |

Three failures within five minutes trigger recovery. DD can roll back to the
last version that ran for ten minutes; if recovery cannot restore service,
logs say `DD is down and needs a hand`. The container can remain Up while DD
is latched off. After fixing the underlying problem, stop it and clear the
latch and recent container failure count:

```sh
dc stop dd
dc run --rm --user dd --entrypoint /bin/rm dd -f \
  /home/dd/checkout/.dd-recovery/state.json \
  /home/dd/checkout/.dd-recovery/container-failures.json
dc start dd
dc logs --tail=50 dd
```

This forgets the recorded last-good version too. Use it after initial setup
if missing config caused the failures, not as a way to hide repeated crashes.

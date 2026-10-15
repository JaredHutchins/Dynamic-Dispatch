# Run DD in a container

This guide takes a fresh installation through a working Gemini container.
The supplied image supports ARM64 and Intel/AMD64 and installs Python, Git,
sudo and Gemini CLI 0.59.0. It installs only the Gemini agent; choose `gemini`
in setup. DD runs as `dd`, and Gemini runs as `dd-gemini`. No privileged
container or Docker socket mount is needed.

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
  Plane account) and Gemini. Follow [Set up Plane Cloud](PLANE_CLOUD.md) to
  create and install the apps, or prepare each identity's personal token.
  Note the workspace slug, credentials and approver's GitHub username.
  For Cycles, also obtain the approver's Plane user ID as described there.
  Use this guide's container commands instead of that page's host installer
  and systemd commands. Its install-link helper can run in the container
  shell opened in step 5.
- For Gemini, a Google Cloud project with billing and the Vertex AI API
  enabled, access to the configured model, and permission to arrange the
  login and spending controls below.

## 2. Give Gemini a limited Google login

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

## 3. Set usage limits before the first Gemini run

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
story worktrees, agent environment files and Gemini's home. Protect backups
of all these volumes. `docker compose down` retains them; adding `-v` deletes
them, including credentials and work.

For the Google key, set these host variables, replacing the example path
with the absolute path to your existing file:

```sh
export DD_GEMINI_CREDENTIAL=/absolute/path/outside-the-repository/vertex-key.json
chmod 600 "$DD_GEMINI_CREDENTIAL"
export DD_CONTAINER_OVERRIDE="$HOME/.config/dd/container.yaml"
mkdir -p "$(dirname "$DD_CONTAINER_OVERRIDE")"
cat > "$DD_CONTAINER_OVERRIDE" <<'YAML'
services:
  dd:
    volumes:
      - type: bind
        source: ${DD_GEMINI_CREDENTIAL:?Set the absolute Google key path}
        target: /etc/dd-credentials/gemini.json
        read_only: true
        bind:
          create_host_path: false
YAML
```

The override is a new host-local file, not a supplied repository file. It
mounts only the key, read-only. The start step copies it to
`/home/dd-gemini/gemini-credential.json`, owned by `dd-gemini`, mode 400.
Gemini's home is private; DD and other agents cannot read that copy. Removing
the mount removes the copy on the next start. Never mount a directory over
`/etc/dd-credentials`: that directory must remain root-only.

## 5. Build, start and configure

Define this host shell helper so every command uses the same override and
Compose project. In a new terminal, return to this checkout and set the two
variables above again before defining the helper.

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

Choose only `gemini`. For the App key path enter
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

Back on the host, write Gemini's environment file. Replace the project ID;
these lines contain settings and a credential path, not the key itself:

```sh
dc run --rm -T --user root --entrypoint /bin/sh dd -c \
  'umask 027; cat > /etc/dynamic-dispatch/gemini.env; chown root:dd-gemini /etc/dynamic-dispatch/gemini.env; chmod 640 /etc/dynamic-dispatch/gemini.env' <<'ENV'
GOOGLE_GENAI_USE_VERTEXAI=true
GOOGLE_CLOUD_PROJECT=YOUR_GCP_PROJECT_ID
GOOGLE_CLOUD_LOCATION=global
GOOGLE_APPLICATION_CREDENTIALS=/home/dd-gemini/gemini-credential.json
ENV
```

The initial setup's doctor may have reported the missing environment file;
now rerun it explicitly. Doctor checks access and sandbox setup but does not
make a model call. Run a small paid smoke test separately as Gemini:

```sh
dc run --rm --user dd --entrypoint python3 dd main.py --doctor --config /home/dd/config/config.json
dc run --rm --user dd-gemini --workdir /tmp --entrypoint /bin/bash dd -c \
  'set -a; . /etc/dynamic-dispatch/gemini.env; printf "Reply with OK\n" | gemini --output-format json -m gemini-2.5-pro'
```

Resolve every FAIL before continuing. A successful smoke test returns JSON
with `response`. The configured model is `gemini-2.5-pro`; confirm your project
can still use it, or choose an available model in `agents.gemini.command` and
use the same model in the smoke test. Install any project-specific test tools
before handing it stories; the supplied image has only the tools listed above.

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
does not prove DD is healthy. Move a small story to In Progress in Plane and
check that Gemini picks it up.

## 6. Day to day

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
code. An update that changes those installed files is never held back: DD
updates its checkout and restarts as for a code update, and the start step's
installer installs the new files before DD starts. If the installer fails, the
start counts as a failure, and recovery returns DD to its last good version
and logs why (see troubleshooting). Because of the planned-restart
exemption, that can take up to five minutes after the update's restart.

Image, Compose and container-start changes (`deploy/Dockerfile`,
`deploy/compose.yaml` and `deploy/dd-container-start`) take effect only when
the image is rebuilt. DD sends one alert for each such update and carries on
with the new checkout meanwhile. To rebuild, update the host checkout and
rebuild; rebuilding does not replace the persistent DD checkout:

```sh
git pull
dc up -d --build
```

To see the version actually reported by DD's last polling cycle and its time:

```sh
dc exec --user dd dd python3 -c 'from dispatch import runs; c = runs.connect("/home/dd/.local/share/dynamic-dispatch/runs.db", initialize=False); h = runs.heartbeat(c); print(dict(h) if h else "No heartbeat yet"); c.close()'
```

The `version` is the running commit SHA. A stale heartbeat means DD is not
currently polling. `git rev-parse HEAD` alone would show the checkout, which
may have changed since the process started.

## 7. Keep a Mac awake

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

## 8. Troubleshooting

| Symptom | What to check |
|---|---|
| Docker cannot connect | Start Docker Desktop or `colima start`; check `docker info`. |
| Clone/build fails | Check host and container DNS, HTTPS access to GitHub and package registries, and available disk space. The checkout volume must be empty or a valid Git checkout. |
| Missing config or permission failure | Use `/home/dd/config/config.json` with every setup/doctor command, owned by `dd`, mode 600. Run setup as `dd`, never root. |
| Plane rejects login | Cloud uses `https://app.plane.so` for web links and `https://api.plane.so` for API calls. Check workspace slug, app installation, permissions and distinct identities in [Plane Cloud setup](PLANE_CLOUD.md). |
| GitHub API works but cloning/pushing fails | The App and Git push login are separate. Check the saved Git login as `dd`, repository access and Contents write permission. `--add-project` requires an App; read access alone does not prove push access. |
| Gemini 401/403 or missing credentials | Check the mounted file exists, its service account roles, Vertex API enablement and project ID. Confirm the env file points to the private copy. Do not print the key to debug it. |
| Gemini 404 | Check model availability in your project and that the env file uses the intended location (`global` here). Match the smoke-test model to config. |
| Gemini 429 / RESOURCE_EXHAUSTED | Check the applicable quota and usage. DD cools the agent down for `usage_limit_cooldown_minutes`; do not raise limits without reviewing spending. |
| Doctor reports sudo, group or env failures | Confirm setup selected only Gemini, env ownership is `root:dd-gemini` and mode is 640. Restart to rerun the container installer; inspect its log before changing sandbox permissions. |
| Tests cannot find a tool | Supply the project's test dependencies in the container; installing them on the host does not install them for Gemini. Ad-hoc container installs disappear on recreation. |
| No stories start | Check a fresh heartbeat, doctor results, Plane state/assignment, GitHub main checks and quota cooldown. The Mac or container runtime may have slept. |
| No update arrives | Check exact-main GitHub Actions results, network/rate-limit warnings and active runs in logs. |

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

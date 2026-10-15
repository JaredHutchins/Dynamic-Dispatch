# Set up Plane Cloud

On Plane Cloud, the Orchestrator and each agent can be a Plane app (a bot)
instead of a Plane user with its own email address and personal token. Each
identity uses one or the other: a personal token, or an app. You can mix
them, say an app for the Orchestrator and personal tokens for the agents.
Identities that keep their personal tokens work exactly as before.

## 1. Prepare DD and create a workspace

Complete the [recommended system setup](README.md#recommended-system-setup).
Run the commands below from DD's checkout as DD's account, without sudo.

Sign up or sign in at https://app.plane.so and create a workspace. Choose its
name and workspace URL. Open it and note the short name (slug): in
`https://app.plane.so/my-workspace/...`, it is `my-workspace`, without slashes.
You need workspace admin access to install apps.
Set your Plane profile's time zone to your own, so Cycle start and end dates fall on your local days.

Plane Cloud's web address is `https://app.plane.so`; its separate API address
is `https://api.plane.so`. Guided setup asks for both.

Prepare the GitHub App and private key described in
[guided setup](README.md#guided-setup), and complete the
[agent logins](README.md#agent-logins). Project onboarding requires a GitHub
App with contents read, pull requests write and commit statuses write.

## 2. Create each app

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
   once; keep it securely until setup asks for it hidden. Do not put the
   secret in a shell command.

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

## 3. Install each app

Plane's own Install button fails without a Setup URL, so DD prints the
link instead. The command needs the client ID before setup can check the
installed app. Run this helper once per app. It prompts for public identifiers,
writes a temporary config, runs DD's install-link command and removes the file.
It does not change your real config.json or ask for secrets.

```sh
python3 -c '
import json
import subprocess
import sys
import tempfile
identity = input("Identity (orchestrator, claude-code, codex or gemini): ").strip()
client_id = input("Plane app client ID: ").strip()
settings = {"plane": {"base_url": "https://app.plane.so", "api_url": "https://api.plane.so"},
            "agents": {}}
bot = {"client_id": client_id}
if identity == "orchestrator":
    settings["plane"]["orchestrator_bot"] = bot
else:
    settings["agents"][identity] = {"plane_bot": bot}
with tempfile.NamedTemporaryFile(mode="w+", suffix=".json") as temporary:
    json.dump(settings, temporary)
    temporary.flush()
    subprocess.run([sys.executable, "main.py", "--config", temporary.name,
                    "--plane-install-link", identity], check=True)
'
```

This invokes `python3 main.py --config TEMP_FILE --plane-install-link IDENTITY`.
Use `claude-code` for Claude, `codex` for Codex and `gemini` for Gemini.
The command prints the link with the permissions above. For each app:

1. Open the link in a browser signed in to Plane as a workspace admin, and
   approve the app.
2. The browser lands on `http://localhost/?app_installation_id=...&code=...`.
   The page itself won't load; that is expected. Copy the value after
   `app_installation_id=` from the address bar, up to the next `&`.
3. Keep it with that app's Client ID and Client Secret for guided setup.
   DD does not need the authorization code.

If the app has a different Redirect URI, add
`--redirect-uri` with exactly that address to the command's argument list.
An installed app is a member of the workspace's existing projects straight away.

## 4. Run guided setup

For sprint-pull, first find the approver's Plane user ID: using that person's
personal token, send `GET https://api.plane.so/api/v1/users/me/` with headers
`X-API-Key` set to their token and `User-Agent` set to `dynamic-dispatch/1`.
Copy the `id` from the JSON response and enter it when setup asks for the
approver's Plane user ID. This is the same lookup DD uses in
[`Plane.current_user()`](../dispatch/plane.py); use the approver's token,
not an app's credentials, so you get the person's ID.

```sh
python3 main.py --setup
```

Enter the web address, accept the separate Cloud API address and enter the
workspace slug. Choose a Plane app for the Orchestrator, then enter its Client
ID, hidden Client Secret and app installation ID. Setup checks that it signs
in and reaches the workspace. If it fails, check the credentials, installation
and permissions, then answer again.

Complete the GitHub questions and select the agents installed on your host.
For each agent, choose its own app and enter its three values. You can also
choose a personal token for any identity and enter it hidden.

Setup writes credentials to private config.json (mode 600). Enter preserves
current values on rerun, including bot credentials. Changes keep a private
backup beside the config. To add the project separately in the next step,
answer no when offered the first project; doctor cannot fully pass yet.

## 5. Create DD's project columns and labels

Create and initialise the GitHub repository with at least one commit. Install
the GitHub App on it and give the host's Git push login write access. Run:

```sh
python3 main.py --add-project
```

Enter the project name, GitHub repository, test command and whether to use
sprint-pull Cycles (which needs the approver's Plane user ID). DD creates the
Plane project, seven DD columns and three labels, and adds the identities.
For an existing project, approve reuse when asked. Matching resources are
reused without deleting or renaming existing ones. See
[Add a project](README.md#add-a-project) for repository and membership checks.
If you already added the project during setup, this step is complete.

## 6. Run doctor and start DD

```sh
python3 main.py --doctor
```

Run as DD's account and resolve every FAIL, including Plane access, GitHub
permissions, agent logins and host setup. The
[doctor guide](README.md#check-the-setup) explains the checks. Once it passes:

```sh
sudo systemctl enable --now dynamic-dispatch
```

DD gets each app's access token itself, keeps it in memory only and renews
it before it runs out (about ten hours on Plane Cloud), and once more if
Plane rejects it. A rejected app login sends the usual Plane login alert,
naming the identity. Client secrets and access tokens are scrubbed from
everything DD stores, logs or posts, like other secrets.

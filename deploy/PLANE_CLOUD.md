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

The install link asks for these permissions, and no more. Each time DD
signs in as an app it asks for the same list again; Plane refuses a sign-in
that asks for none, or for more than the app was installed with.

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

```sh
python3 main.py --setup
```

For the Plane address, enter `https://app.plane.so`. Pasting your workspace's
address from the browser, such as `https://app.plane.so/my-workspace/`, works
too: setup keeps the site and offers `my-workspace` as the workspace slug.
Accept the separate Cloud API address and the workspace slug. Choose a Plane
app for the Orchestrator, then enter its Client ID, hidden Client Secret and
app installation ID. Setup checks that it signs in and reaches the workspace.
If it fails, check the credentials, installation and permissions, then answer
again; [Troubleshooting](#7-troubleshooting) explains a refused sign-in.

Setup then asks who approves Cycle plans, which only sprint-pull needs. It
lists everyone in the workspace's projects by number, display name and email,
labelling Dynamic Dispatch's own login (and, on a rerun, each agent) rather
than hiding it. The installed app is already a member of the existing
projects, so no extra permission is needed. Type the approver's number, or
press Enter to skip; on a rerun the prompt names the saved approver, Enter
keeps them and `none` removes them. If the workspace has no projects yet,
setup asks for the approver's Plane user ID instead: it ends the address of
their profile page, `https://app.plane.so/my-workspace/profile/USER_ID`, and
Enter skips it; you can rerun setup after adding a project to pick from the
list.

Complete the GitHub questions and select the agents installed on your host.
For each agent, choose its own app and enter its three values. Setup checks
that it signs in and looks up its own user, as doctor does; an agent's app
can't list projects, so setup doesn't ask it to. You can also choose a
personal token for any identity and enter it hidden.

Setup writes credentials to private config.json (mode 600) once it finishes.
If it stops part-way, for example on a refused sign-in or Ctrl+C, the answers
that already passed their checks are kept in private
`config.json.setup-progress`; rerun `--setup` safely and they are offered as
the current values. Enter preserves current values on rerun, including bot
credentials. Changes keep one private backup beside the config per finished run. To add the project separately in the next step,
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
On a new board, Plane's Backlog and Todo become Product Backlog and Sprint
Backlog with the same IDs, and Product Backlog becomes the default. Only
missing Dynamic Dispatch columns are created. For an existing project, approve
reuse when asked, then choose whether to rename those starting states and
change the default. Enter accepts; no preserves their names and the default
and lists unused columns. Work items stay in their states. If either old and
new name both exist, no names or defaults change. A starting state that is
duplicated or in an unexpected state group is left as it is and listed as
unused. Setup lists unused states: to remove one in Plane, make another state
the default first, move its work items out, then delete it. Dynamic Dispatch
never deletes states. If Plane refuses the default change, setup finishes
and tells you to open Product Backlog's state menu and choose **Mark as default**.
Matching resources are reused and rerunning completed setup changes nothing. See
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

DD gets each app's access token itself, asking for the permissions in
[What each app may do](#what-each-app-may-do), keeps it in memory only and renews
it before it runs out (about ten hours on Plane Cloud), and once more if
Plane rejects it. A rejected app login sends the usual Plane login alert,
naming the identity. Client secrets and access tokens are scrubbed from
everything DD stores, logs or posts, like other secrets.

## 7. Troubleshooting

### Plane app sign-in was refused

Setup or doctor says, for example, "Dynamic Dispatch's Plane app sign-in was
refused (Plane said: invalid_client); check its Client ID, Client Secret and
Installation ID." Plane refused the app's token request, and the word in
brackets is Plane's reason. DD shows only that word, never the secret or
Plane's full reply.

- `invalid_client`: Plane doesn't recognise the Client ID and Client Secret
  together. Check both against the app's settings in Plane, and that the
  secret was entered whole. If the secret is lost, make a new one in Plane
  and enter it in setup.
- `invalid_scope`: the permissions DD asked for don't match the
  installation. Check that these are this identity's own app's values (an
  agent's app entered as the Orchestrator's, or the other way round, gives
  this), and that the app was installed from the link
  `--plane-install-link` printed for this identity. If in doubt, install it
  again from that link and enter the new Installation ID.
- Any other word, or no reason: check the Installation ID is the one from
  this app's install, and that the app is still installed in the workspace.

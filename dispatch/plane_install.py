"""Printing the link that installs a Plane app (bot) for one of DD's Plane identities.

Only the app's client ID is read from its settings. The client secret is
never read, so it can't reach the output.
"""

from urllib.parse import urlsplit

from . import config, plane, plane_apps

ORCHESTRATOR = "orchestrator"


def _setting(identity):
    """Where the identity's app settings go in config.json."""
    return "plane.orchestrator_bot" if identity == ORCHESTRATOR else f"agents.{identity}.plane_bot"


def _where(cfg, identity):
    """(the identity's name for people, its app settings in config, the scopes it needs)."""
    if identity == ORCHESTRATOR:
        return "the Orchestrator", cfg["plane"]["orchestrator_bot"], plane_apps.ORCHESTRATOR_SCOPES
    agent = cfg["agents"][identity]
    return agent.get("display_name") or identity, agent["plane_bot"], plane_apps.AGENT_SCOPES


def _valid_redirect(value):
    url = urlsplit(value)
    return url.scheme in ("http", "https") and bool(url.hostname) and not url.fragment


def run(path, identity, redirect_uri=plane_apps.DEFAULT_REDIRECT_URI):
    if not _valid_redirect(redirect_uri):
        print("The redirect URI must be an http:// or https:// address, as set in the app's settings in Plane.")
        return 1
    try:
        cfg = config.read(path)
        agents = list(cfg.get("agents", {}))
    except config.ConfigError as error:
        print(f"Cannot read config.json: {error}")
        return 1
    except Exception:
        print("Cannot read config.json; check that it is JSON.")
        return 1
    if identity != ORCHESTRATOR and identity not in agents:
        print(f"Name {ORCHESTRATOR} or one of the agents in config.json: {', '.join(agents) or '(none)'}.")
        return 1
    setting = _setting(identity)
    try:
        who, bot, scopes = _where(cfg, identity)
        client_id = bot["client_id"]
        origin = plane.api_url(cfg["plane"])
        if not isinstance(client_id, str) or not client_id.strip() or not isinstance(origin, str):
            raise ValueError("No client ID")
    except Exception:
        print(f"Set {setting}.client_id in config.json to the app's Client ID first, and plane.base_url "
              '(and plane.api_url for Plane Cloud), as deploy/README.md "Using Plane Cloud" describes.')
        return 1
    print(f"Install link for {who}'s Plane app:\n")
    print("    " + plane_apps.install_link(origin, client_id.strip(), scopes, redirect_uri) + "\n")
    print("It asks for these permissions: " + " ".join(scopes) + "\n")
    print(f"1. Check the app's Redirect URI in Plane is exactly {redirect_uri}\n"
          "   (or run this again with --redirect-uri and the one it has).\n"
          "2. Open the link in a browser signed in to Plane as a workspace admin, and approve the app.\n"
          f"3. The browser then goes to {redirect_uri} with ?app_installation_id=...&code=... added.\n"
          "   The page itself may not load; that is expected. From the address bar, copy the value\n"
          "   after app_installation_id= up to the next &, or the end.\n"
          f"4. Put it in config.json as {setting}.app_installation_id, then run python3 main.py --doctor.")
    return 0

"""Secret redaction.

Every configured credential is scrubbed from text before it is stored,
printed, or sent anywhere. Scrubbing happens at the exits - the Plane client,
the GitHub client, the run store, and log() - so no caller can forget.
"""

import sys
import threading
from datetime import datetime

_secrets = []
_lock = threading.RLock()

MIN_SECRET_LENGTH = 8
REPLACEMENT = "[redacted]"


def configure(cfg):
    """Collect every secret DD knows about.

    A Plane app's access token isn't in config; it is added when DD gets it.
    """
    found = [cfg["plane"].get("orchestrator_token"), _bot_secret(cfg["plane"].get("orchestrator_bot")),
             cfg["github"].get("token")]
    for agent_cfg in cfg["agents"].values():
        found.extend([agent_cfg.get("plane_token"), _bot_secret(agent_cfg.get("plane_bot"))])
        env_file = agent_cfg.get("env_file")
        if env_file:
            found.extend(read_env_values(env_file))
    alerts = cfg.get("alerts")
    alert_secrets = []
    if isinstance(alerts, dict):
        devices = alerts.get("device_keys", [])
        alert_secrets = [alerts.get("encryption_key")]
        if isinstance(devices, list):
            alert_secrets.extend(devices)
        alert_secrets = [value for value in alert_secrets if isinstance(value, str) and value]
    set_secrets(found + alert_secrets)


def _bot_secret(bot):
    return bot.get("client_secret") if isinstance(bot, dict) else None


def set_secrets(values):
    global _secrets
    with _lock:
        unique = {v for v in values if v and len(v) >= MIN_SECRET_LENGTH}
        # Longest first, so a secret containing another is replaced whole.
        _secrets = sorted(unique, key=len, reverse=True)


def add_secret(value):
    """For a credential obtained while running, such as a GitHub App installation token."""
    with _lock:
        if value and len(value) >= MIN_SECRET_LENGTH and value not in _secrets:
            _secrets.append(value)
            _secrets.sort(key=len, reverse=True)


def read_env_values(path):
    """Values from a KEY=VALUE file. Raises OSError if it can't be read."""
    values = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            values.append(line.split("=", 1)[1].strip().strip("'\""))
    return values


def scrub(text):
    with _lock:
        if text is None:
            return None
        text = str(text)
        for secret in _secrets:
            text = text.replace(secret, REPLACEMENT)
        return text


def log(message):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{stamp} {scrub(message)}", flush=True)


def warn(message):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{stamp} {scrub(message)}", file=sys.stderr, flush=True)

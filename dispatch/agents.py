"""Agent adapters.

An agent is a config entry, not a branch in the code. Adding one means adding a
config block and, if its output format is new, a parser function below. Nothing
else in this package knows which agent it is talking to.

Agents always run sandboxed: each as its own restricted account (run_as),
through the launcher, with
only their own model credential in the environment (loaded from their env_file
by the launcher, never passed through DD).
"""

import json
import math
import re
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import logins, proc
from .redact import scrub


class AgentError(Exception):
    pass


# Each parser takes the agent's stdout and stderr, and returns ok, summary,
# usage, cost_usd, usage_limit_until (a UTC datetime or None), and usage_limit,
# which is True when the account ran out of usage. Recognising that is the
# parser's job, so nothing else depends on how an agent words it.

# Claude Code's result text when a plan limit stops it, in the forms it has
# used: "Claude AI usage limit reached|<time>", "5-hour limit reached ∙ resets
# 3pm", "You've hit your limit · resets 3pm".
CLAUDE_LIMIT = re.compile(r"\blimit reached\b|\bhit your (?:\w+ )?limit\b", re.IGNORECASE)


CLAUDE_LOGIN = re.compile(r"invalid api key|oauth token.{0,30}expired|authentication_error|(?:run|use|please|log in).{0,30}/login", re.I)
CODEX_LOGIN = re.compile(r"\b401\b|unauthorized|(?:refresh.{0,40}token|token.{0,40}refresh)", re.I)


def _local_zone():
    # /etc/localtime carries the Pi's DST rules; TZ also supports test overrides.
    name = os.environ.get("TZ")
    if name:
        return ZoneInfo(name.lstrip(":"))
    with open("/etc/localtime", "rb") as source:
        return ZoneInfo.from_file(source)


def _limit_until(message, clock_prefix, at=None):
    at = at or datetime.now(timezone.utc)
    try:
        stamp = re.search(r"\|(\d+)\b", message)
        if stamp:
            return datetime.fromtimestamp(int(stamp[1]), timezone.utc)
        relative = re.search(r"\btry again in (\d+) (minutes?|hours?)\b", message, re.I)
        if relative:
            minutes = int(relative[1]) * (60 if relative[2].lower().startswith("hour") else 1)
            return at + timedelta(minutes=minutes)
        clock = re.search(clock_prefix + r"\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b"
                          r"(?:\s*[\[(]([^\])]+)[\])])?", message, re.I)
        if clock:
            hour, minute = int(clock[1]), int(clock[2] or 0)
            if not 1 <= hour <= 12 or minute > 59:
                return None
            zone = ZoneInfo(clock[4]) if clock[4] else _local_zone()
            local = at.astimezone(zone)
            reset = local.replace(hour=hour % 12 + (12 if clock[3].lower() == "pm" else 0),
                                  minute=minute, second=0, microsecond=0)
            if reset.astimezone(timezone.utc) <= at:
                reset += timedelta(days=1)
            return reset.astimezone(timezone.utc)
    except (ValueError, OverflowError, OSError, KeyError):
        # An unreadable reset time must not hide the usage limit itself.
        pass
    return None


def parse_claude_json(stdout, stderr=""):
    """Claude Code with --output-format json returns a single JSON object."""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        raise AgentError(f"Could not parse output as JSON: {stdout[:500]}") from None
    if not isinstance(data, dict):
        raise AgentError(f"Expected a JSON object, got: {stdout[:500]}")
    failed = bool(data.get("is_error", False))
    summary = data.get("result")
    limited = failed and isinstance(summary, str) and bool(CLAUDE_LIMIT.search(summary))
    return {
        "ok": not failed,
        "summary": summary,
        "usage": data.get("usage", {}),
        "cost_usd": data.get("total_cost_usd"),
        "usage_limit": limited,
        "usage_limit_until": _limit_until(summary, r"\bresets") if limited else None,
    }


# Codex's message when a plan limit stops it: "You've hit your usage limit. ...
# try again at 5:22 PM."
CODEX_LIMIT = re.compile(r"\busage limit\b", re.IGNORECASE)


def _codex_error(event):
    """The message of an error or turn.failed event, or None."""
    if event.get("type") == "error":
        message = event.get("message")
    elif event.get("type") == "turn.failed":
        error = event.get("error")
        message = error.get("message") if isinstance(error, dict) else None
    else:
        return None
    return message if isinstance(message, str) and message else "(no message)"


def parse_codex_json(stdout, stderr=""):
    """`codex exec --json` prints one JSON event per line.

    A turn ends with turn.completed, which carries the token usage, or with
    turn.failed. What Codex says to the user arrives as agent_message items,
    and the last one is its summary. Codex reports no cost.
    """
    events = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)

    summary = None
    usage = None
    errors = []
    for event in events:
        item = event.get("item")
        if (event.get("type") == "item.completed" and isinstance(item, dict)
                and item.get("type") == "agent_message" and isinstance(item.get("text"), str)):
            summary = item["text"]
        elif event.get("type") == "turn.completed":
            usage = event.get("usage", {})
        else:
            error = _codex_error(event)
            if error:
                errors.append(error)

    if usage is None and not errors:
        raise AgentError(
            "Could not read the output as codex exec --json events: it has no "
            f"turn.completed or error event. It began:\n{stdout[:500] or '(no output)'}"
        )
    if errors:
        limits = [e for e in errors if CODEX_LIMIT.search(e)]
        resets = [_limit_until(e, r"\btry again at") for e in limits]
        # turn.failed usually repeats the error event before it; say it once.
        return {
            "ok": False,
            "summary": "\n".join(dict.fromkeys(errors)),
            "usage": usage or {},
            "cost_usd": None,
            "usage_limit": bool(limits),
            "usage_limit_until": next((reset for reset in resets if reset is not None), None),
        }
    return {"ok": True, "summary": summary, "usage": usage, "cost_usd": None, "usage_limit": False, "usage_limit_until": None}


# Gemini CLI's quota wording is unconfirmed, so match it broadly: HTTP 429,
# RESOURCE_EXHAUSTED, "Quota exceeded for quota metric ...", "You have
# exhausted your capacity on this model". A Google Cloud "quota project" is a
# login setting, not a limit.
GEMINI_LIMIT = re.compile(r"\b429\b|resource.?exhausted|too many requests|rate.?limit"
                          r"|\bquota\b(?!\s+project)|\bexhausted your\b", re.I)
GEMINI_LOGIN = re.compile(r"\b401\b|unauthenticated|unauthori[sz]ed|invalid_grant|api key not valid"
                          r"|invalid api key|(?:missing|invalid|expired|default|no) (?:\w+ )?credentials"
                          r"|credentials.{0,40}(?:missing|invalid|expired|not found)|auth(?:entication)? method",
                          re.I)


def _gemini_object(text):
    """The JSON object Gemini CLI printed, past any log lines before it.

    It prints one indented object, so the object starts a line.
    """
    decoder = json.JSONDecoder()
    for start in re.finditer(r"^\{", text, re.M):
        try:
            data, _ = decoder.raw_decode(text, start.start())
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and ("response" in data or data.get("error")):
            return data
    return None


def _gemini_tokens(stats):
    """stats.models.<model>.tokens, added up across the models the run used."""
    models = stats.get("models") if isinstance(stats, dict) else None
    usage = {}
    for model in models.values() if isinstance(models, dict) else ():
        tokens = model.get("tokens") if isinstance(model, dict) else None
        for key, value in tokens.items() if isinstance(tokens, dict) else ():
            if _is_number(value):
                usage[key] = usage.get(key, 0) + value
    return usage


def _gemini_error(error):
    """Gemini's error as one line: its message, then its type and code."""
    if not isinstance(error, dict):
        return str(error)
    message = error.get("message")
    message = message if isinstance(message, str) and message else "(no message)"
    details = [str(error[key]) for key in ("type", "code") if error.get(key) not in (None, "")]
    return f"{message} ({', '.join(details)})" if details else message


def parse_gemini_json(stdout, stderr=""):
    """`gemini --output-format json` prints a single JSON object.

    A finished run has the summary in "response" and token counts per model
    in "stats"; a failed one has "error" with a type, message and code, which
    Gemini CLI writes to stderr. Gemini reports no cost.
    """
    data = _gemini_object(stdout) or _gemini_object(stderr)
    if data is None:
        raise AgentError(
            "Could not read the output as gemini --output-format json: it has no "
            f"response or error. It began:\n{(stdout or stderr)[:500] or '(no output)'}"
        )
    usage = _gemini_tokens(data.get("stats"))
    if data.get("error"):
        message = _gemini_error(data["error"])
        limited = bool(GEMINI_LIMIT.search(message))
        return {
            "ok": False,
            "summary": message,
            "usage": usage,
            "cost_usd": None,
            "usage_limit": limited,
            "usage_limit_until": _limit_until(message, r"\btry again at") if limited else None,
        }
    summary = data.get("response")
    return {"ok": True, "summary": summary if isinstance(summary, str) else None, "usage": usage,
            "cost_usd": None, "usage_limit": False, "usage_limit_until": None}


PARSERS = {
    "claude_json": parse_claude_json,
    "codex_json": parse_codex_json,
    "gemini_json": parse_gemini_json,
}

# How each agent words a rejected login, by parser. Only an agent's own error
# messages are matched, never its transcript.
LOGIN_PATTERNS = {
    "claude_json": CLAUDE_LOGIN,
    "codex_json": CODEX_LOGIN,
    "gemini_json": GEMINI_LOGIN,
}


def _is_number(value):
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _numeric_usage(usage):
    """Only the numbers. Anything else in an agent's usage block could carry text."""
    if not isinstance(usage, dict):
        return {}
    return {scrub(str(key))[:64]: value for key, value in usage.items() if _is_number(value)}


def _cost(value):
    if isinstance(value, bool):
        return None
    try:
        cost = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return cost if math.isfinite(cost) else None


def build_command(agent_cfg, prompt):
    """Substitute the prompt into the configured command template."""
    if agent_cfg.get("prompt_via", "argument") == "stdin":
        return list(agent_cfg["command"])
    return [prompt if part == "{prompt}" else part for part in agent_cfg["command"]]


def _failed(error, usage_limit=False):
    return {"ok": False, "summary": None, "usage": {}, "cost_usd": None,
            "usage_limit": usage_limit, "usage_limit_until": None, "error": error}


def _hit_limit(parser, result):
    """Whether a failed run stopped at its usage limit.

    An agent stopped that way exits with an error, and only its output says why.
    """
    if result.timed_out or result.returncode is None or result.stdout_truncated:
        return False
    try:
        return bool(parser(result.stdout, result.stderr).get("usage_limit"))
    except AgentError:
        return False


def run(cfg, agent_cfg, prompt, worktree):
    """Run an agent to completion. Never raises on agent failure - returns ok=False."""
    parser = PARSERS.get(agent_cfg.get("parser"))
    if parser is None:
        return _failed(f"Unknown parser in config: {agent_cfg.get('parser')}")

    timeout = int(agent_cfg.get("timeout_seconds", 1800))
    result = proc.run_sandboxed(
        cfg,
        build_command(agent_cfg, prompt),
        cwd=worktree,
        timeout=timeout,
        run_as=agent_cfg.get("run_as"),
        env_file=agent_cfg.get("env_file"),
        input_text=prompt if agent_cfg.get("prompt_via", "argument") == "stdin" else None,
    )

    login_key = 'agent:' + logins.fingerprint((agent_cfg.get('run_as'), agent_cfg.get('env_file'),
                                               agent_cfg.get('parser')))
    pattern = LOGIN_PATTERNS[agent_cfg['parser']]
    try:
        parsed_login = parser(result.stdout, result.stderr)
    except AgentError:
        parsed_login = {}
    successful = result.returncode == 0 and not result.timed_out and parsed_login.get('ok', False)
    # Parsed failure summaries contain provider errors, not commands or tool output.
    message = parsed_login.get('summary') if parsed_login.get('ok') is False else ''
    message = message if isinstance(message, str) else ''
    if agent_cfg.get('parser') == 'claude_json':
        message += '\n' + result.stderr
    if not successful and pattern.search(message):
        logins.login(login_key, agent_cfg['display_name'] + ' login', 'agent output', True)
    elif successful and not result.stdout_truncated:
        logins.login(login_key, agent_cfg['display_name'] + ' login', 'agent output', False)

    if result.timed_out or result.returncode != 0:
        failed = _failed(f"{agent_cfg['display_name']}: {proc.describe_failure(result, timeout)}",
                         usage_limit=_hit_limit(parser, result))
        try:
            parsed = parser(result.stdout, result.stderr)
        except AgentError:
            pass
        else:
            if failed["usage_limit"]:
                failed["usage_limit_until"] = parsed.get("usage_limit_until")
            failed["usage"] = _numeric_usage(parsed.get("usage"))
            failed["cost_usd"] = _cost(parsed.get("cost_usd"))
        return failed
    if result.stdout_truncated:
        return _failed(f"{agent_cfg['display_name']} produced more output than the limit allows.")

    try:
        parsed = parser(result.stdout, result.stderr)
    except AgentError as e:
        return _failed(f"{agent_cfg['display_name']}'s output could not be read. {e}")

    parsed["usage"] = _numeric_usage(parsed.get("usage"))
    parsed["cost_usd"] = _cost(parsed.get("cost_usd"))
    parsed["usage_limit"] = bool(parsed.get("usage_limit"))
    if not parsed["ok"]:
        parsed["error"] = parsed.get("summary") or "The agent reported an error."
    return parsed

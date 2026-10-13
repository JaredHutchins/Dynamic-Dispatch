"""Stands in for Claude Code, or for Codex if a third argument says "codex".

Behaviour chosen by the mode argument. Codex's output follows `codex exec --json`.
"""
import json, os, sys, time
from pathlib import Path
prompt = sys.stdin.read() if sys.argv[1] == "-" else sys.argv[1]
mode = sys.argv[2]
style = sys.argv[3] if len(sys.argv) > 3 else "claude"
secret = os.environ.get("LEAK", "")
if mode.startswith("pause:"):
    control = Path(mode.split(":", 1)[1])
    name = Path.cwd().name
    (control / (name + ".started")).touch()
    deadline = time.monotonic() + 20
    while not (control / (name + ".release")).exists():
        if time.monotonic() > deadline:
            sys.exit("Timed out waiting for test release")
        time.sleep(0.01)
    mode = "edit"
if mode == "fail":
    print("boom", file=sys.stderr); sys.exit(3)
if mode == "repair":
    with open("feature.py", "w") as f: f.write("VALUE = 1\nMAIN = 2\n")
if mode == "edit":
    with open("feature.py", "a") as f: f.write("VALUE = 1\n")
if mode == "syntax":
    with open("broken.py", "w") as f: f.write("def nope(:\n")
if mode == "hijack":
    # Agent tries to redirect the orchestrator's git at a directory it controls.
    os.makedirs("evil/hooks", exist_ok=True)
    with open("evil/hooks/pre-commit", "w") as f: f.write("#!/bin/sh\ntouch " + os.environ["MARKER"] + "\n")
    os.chmod("evil/hooks/pre-commit", 0o755)
    with open(".git", "w") as f: f.write("gitdir: " + os.path.abspath("evil") + "\n")
    with open("feature.py", "a") as f: f.write("VALUE = 2\n")
if mode == "prompt":
    with open("prompt.txt", "w") as f: f.write(prompt)
if mode == "filter":
    # Asks for a filter on every file; the filter itself would come from host config.
    with open(".gitattributes", "w") as f: f.write("* filter=evil\n")
    with open("feature.py", "a") as f: f.write("VALUE = 3\n")
if mode == "limit_partial":
    # Stopped partway: unfinished changes are left behind.
    with open("partial.py", "w") as f: f.write("HALF = 1\n")
if mode == "limit_mixed":
    # Stopped partway with a file added, one changed and one deleted, one holding a secret.
    with open("partial.py", "w") as f: f.write("HALF = 1\nTOKEN = '" + secret + "'\n")
    with open("app.py", "a") as f: f.write("print('more')\n")
    os.remove("DEFINITION_OF_DONE.md")
if mode in ("usage", "long"):
    with open("feature.py", "a") as f: f.write("VALUE = 4\n")
result = "Changed things " + secret
if mode == "finished":
    assert "finished and tested earlier but never reviewed" in prompt
    assert "current description and acceptance criteria" in prompt
    assert "say so and change nothing" in prompt
    assert "VALUE = 1" in Path("feature.py").read_text()
    result = "The saved work already meets everything; no changes needed."
usage = {"output_tokens": 5}
cost = 0.01
if mode == "usage":
    usage = {"output_tokens": 5, "input_tokens": 7.5, "note": "text " + secret, "flag": True,
             "nested": {"x": 1}, "service_tier": "standard"}
    cost = "0.5"
if mode == "long":
    result = "x" * 50000

if style == "codex":
    def event(**fields): print(json.dumps(fields))
    event(type="thread.started", thread_id="0199a213-81c0-7800-8aa1-bbab2a035a53")
    event(type="turn.started")
    if mode == "garbage":
        print("Welcome to Codex! Something unexpected happened."); sys.exit(0)
    if mode in ("limit", "limit_partial", "limit_mixed", "limit_reset"):
        message = ("You've hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro) "
                   "or try again later.")
        if mode == "limit_reset":
            message = "Usage limit. Try again in 15 minutes."
        event(type="error", message=message)
        event(type="turn.failed", error={"message": message})
        sys.exit(1)
    event(type="item.completed", item={"id": "item_0", "type": "reasoning", "text": "**Adding the value**"})
    event(type="item.started", item={"id": "item_1", "type": "command_execution", "command": "bash -lc ls",
                                     "aggregated_output": "", "exit_code": None, "status": "in_progress"})
    event(type="item.completed", item={"id": "item_1", "type": "command_execution", "command": "bash -lc ls",
                                       "aggregated_output": "app.py\n", "exit_code": 0, "status": "completed"})
    event(type="item.completed", item={"id": "item_2", "type": "agent_message", "text": result})
    event(type="turn.completed", usage={"input_tokens": 24763, "cached_input_tokens": 24448, "output_tokens": 122})
    sys.exit(3 if mode == "fail_usage" else 0)

if mode in ("limit", "limit_partial", "limit_mixed", "limit_reset"):
    print(json.dumps({"type": "result", "subtype": "success", "is_error": True,
                      "result": "Claude AI usage limit reached", "usage": usage}))
    sys.exit(1)
print(json.dumps({"is_error": False, "result": result, "usage": usage, "total_cost_usd": cost}))

if mode == "fail_usage":
    sys.exit(3)

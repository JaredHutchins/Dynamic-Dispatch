"""Tests for Dynamic Dispatch.

Run from the repository root:

    python3 -m unittest discover -s tests

Plane and GitHub are faked, a local bare repository stands in for the GitHub
remote, and tests/fake_agent.py stands in for Claude Code and Codex. Real git runs
against real repositories. The sudo boundary is NOT exercised: the sandboxed
launcher is replaced by a direct call, since it needs the dd-agent user.

Set DD_TEST_GIT to a git binary if the one on PATH is unusable.
"""


import json, os, re, shutil, subprocess, sys, tempfile, unittest
from unittest import mock
from pathlib import Path

from unraisable import install

install()

HERE = Path(__file__).resolve().parent
REPO_CODE = HERE.parent
sys.path.insert(0, str(REPO_CODE))
PY = sys.executable
GIT = os.environ.get("DD_TEST_GIT") or shutil.which("git")

# Keep the host's own git config (a git-lfs filter, say) out of the tests.
os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
os.environ["GIT_CONFIG_GLOBAL"] = os.devnull

from dispatch import choose, config, plane, github, runs, proc, redact, cleanup, text, mainhealth
import main as dd_main

ORCH, CLAUDE, GH = "orch-token-AAAAAAAA", "claude-token-BBBBBBBB", "gh-token-CCCCCCCC"
MODEL = "model-token-DDDDDDDD"
CODEX = "codex-token-FFFFFFFF"
S_IP, S_REV, S_BLK, S_BACK = "st-inprogress", "st-review", "st-blocked", "st-backlog"
PID = "proj-1"


def git(*args, cwd):
    return subprocess.run([GIT, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


# ---- fake Plane, reached through the real client code -------------------
class Board:
    def reset(self):
        self.issues, self.comments, self.transitions = {}, [], []
        self.fail_patch = self.fail_post = self.fail_get = 0
        self.assignments, self.user_lookups = [], []
        self.assignment_error = self.user_error = None
        self.start_dates, self.start_date_error = [], None
        self.label_updates, self.label_error = [], None
        self.lose_post_reply = 0   # the comment is saved, but the reply never arrives
        self.list_status = None    # e.g. 405: this Plane can't list comments
        self.after_post = None     # called after a comment is saved
        self.agent_404 = False     # the agent's own requests get 404, though the story exists
        # Optional list of states to report on successive single-issue GETs; "FAIL" means unreachable.
        self.get_states = None
        # Split list answers into pages this size, as real Plane does; None means one page.
        self.page_size = None
        self.pages_served = []     # (path, cursor) of every list page handed out
board = Board(); board.reset()

def paged(path, items):
    """A list answer shaped like Plane's, honouring the request's cursor.

    Plane's cursor is "per_page:page:offset". The last page still carries a
    next_cursor; only next_page_results says there is nothing more.
    """
    cursor = re.search(r"[?&]cursor=([^&]+)", path)
    board.pages_served.append((path.split("?")[0], cursor and cursor.group(1)))
    if board.page_size is None:
        return {"results": items, "next_page_results": False}
    page = int(cursor.group(1).split(":")[1]) if cursor else 0
    start = page * board.page_size
    return {"results": items[start:start + board.page_size],
            "next_page_results": start + board.page_size < len(items),
            "next_cursor": f"{board.page_size}:{page + 1}:0",
            "prev_page_results": page > 0}

def fake_plane_request(self, method, path, payload=None, *, base=None):
    if method == "GET" and path == "/users/me/":
        assert base == self.api
        board.user_lookups.append(self.token)
        if board.user_error:
            raise board.user_error
        return {"id": {CLAUDE: "user-claude", CODEX: "user-codex"}[self.token]}
    m = re.match(r"/projects/([^/]+)/issues/(\?.*)?$", path)
    if method == "GET" and m:
        # Plane 1.4.2 has no state filter here and refuses the structured ones outright.
        if re.search(r"[?&](pql|filters)=", path): raise plane.PlaneError("unsupported", status=400)
        return paged(path, [dict(i) for i in board.issues.values()])
    m = re.match(r"/projects/([^/]+)/issues/([^/]+)/$", path)
    if m:
        iid = m.group(2)
        if method == "GET":
            if board.fail_get: board.fail_get -= 1; raise plane.PlaneError("down", status=503)
            if iid not in board.issues: raise plane.PlaneError("gone", status=404)
            issue = dict(board.issues[iid])
            if board.get_states:
                state = board.get_states.pop(0)
                if state == "FAIL": raise plane.PlaneError("down", status=503)
                issue["state"] = {"id": state}
            return issue
        if method == "PATCH":
            if "labels" in payload:
                if board.label_error:
                    raise board.label_error
                board.issues[iid]["labels"] = payload["labels"]
                board.label_updates.append((self.token, iid, payload["labels"]))
                return {}
            if "assignees" in payload:
                if board.assignment_error:
                    raise board.assignment_error
                board.issues[iid]["assignees"] = payload["assignees"]
                board.assignments.append((self.token, iid, payload["assignees"]))
                return {}
            if "start_date" in payload:
                if board.start_date_error:
                    raise board.start_date_error
                board.issues[iid]["start_date"] = payload["start_date"]
                board.start_dates.append((self.token, iid, payload["start_date"]))
                return {}
            if board.agent_404 and self.token == CLAUDE: raise plane.PlaneError("not found", status=404)
            if board.fail_patch: board.fail_patch -= 1; raise plane.PlaneError("down", status=503)
            board.issues[iid]["state"] = {"id": payload["state"]}
            board.transitions.append((self.token, iid, payload["state"]))
            return {}
    m = re.match(r"/projects/([^/]+)/issues/([^/]+)/comments/(\?.*)?$", path)
    if m and method == "GET":
        if board.list_status: raise plane.PlaneError("no", status=board.list_status)
        mine = [{"comment_html": c[2]} for c in board.comments if c[1] == m.group(2)]
        return paged(path, mine)
    if m and method == "POST":
        if board.agent_404 and self.token == CLAUDE: raise plane.PlaneError("not found", status=404)
        if board.fail_post: board.fail_post -= 1; raise plane.PlaneError("down", status=503)
        if m.group(2) not in board.issues: raise plane.PlaneError("gone", status=404)
        board.comments.append((self.token, m.group(2), payload["comment_html"]))
        if board.after_post: board.after_post()
        if board.lose_post_reply: board.lose_post_reply -= 1; raise plane.PlaneError("timed out")
        return {}
    raise AssertionError(f"unexpected Plane call {method} {path}")

real_plane_request = plane.Plane._request
plane.Plane._request = fake_plane_request

# ---- fake GitHub ---------------------------------------------------------
APP_TOKEN = "ghs-app-token-EEEEEEEE"
prs = {}
gh_tokens = []       # the token behind every pull request call
app = {}             # public_key: checks App JWTs; fail: an error to raise instead of a token
pr_failures = []     # errors to raise on successive attempts to open a pull request
statuses = []        # (sha, state, description) of every light set, in order
status_failures = [] # errors to raise on successive attempts to set a light
reviews = {}         # pull request number -> its reviews, oldest first, as GitHub gives them
review_failures = [] # errors to raise on successive review lookups
review_reads = []    # pull request number of every review lookup
pr_comments = []     # (pull request number, body) of every pull request comment
comment_failures = []     # errors to raise on successive attempts to post a pull request comment
description_failures = [] # errors to raise on successive attempts to replace a description
PR_URL = "https://gh/pr/{}"  # tests patch in a real GitHub shape when they need one

def b64url_decode(text):
    return __import__("base64").urlsafe_b64decode(text + "=" * (-len(text) % 4))

def fake_app_token(installation, jwt):
    if app.get("fail"): raise github.GitHubError(app["fail"])
    assert installation == "42", installation
    header, claims, signature = jwt.split(".")
    with tempfile.NamedTemporaryFile() as sig:
        sig.write(b64url_decode(signature)); sig.flush()
        subprocess.run(["openssl", "dgst", "-sha256", "-verify", app["public_key"], "-signature", sig.name],
                       input=f"{header}.{claims}".encode(), check=True, capture_output=True)
    assert json.loads(b64url_decode(header))["alg"] == "RS256"
    claims = json.loads(b64url_decode(claims))
    assert claims["iss"] == "1234" and claims["exp"] - claims["iat"] <= 600, claims
    return {"token": APP_TOKEN, "expires_at": "2026-09-28T12:00:00Z"}

def fake_gh_request(method, path, token, payload=None, *, login_key=None):
    m = re.match(r"/app/installations/([^/]+)/access_tokens$", path)
    if method == "POST" and m:
        return fake_app_token(m.group(1), token)
    assert token in (GH, APP_TOKEN)
    gh_tokens.append(token)
    m = re.search(r"/statuses/([0-9a-f]+)$", path)
    if method == "POST" and m:
        if status_failures: raise github.GitHubError(status_failures.pop(0))
        assert payload["context"] == "Dynamic Dispatch", payload
        statuses.append((m.group(1), payload["state"], payload["description"]))
        return {}
    m = re.search(r"/pulls/(\d+)/reviews\?per_page=100&page=(\d+)$", path)
    if method == "GET" and m:
        review_reads.append(m.group(1))
        if review_failures: raise github.GitHubError(review_failures.pop(0))
        page = int(m.group(2))
        return reviews.get(m.group(1), [])[(page - 1) * 100:page * 100]
    m = re.search(r"/issues/(\d+)/comments$", path)
    if method == "POST" and m:
        if comment_failures: raise github.GitHubError(comment_failures.pop(0))
        pr_comments.append((m.group(1), payload["body"]))
        return {}
    m = re.search(r"/issues/(\d+)/comments\?per_page=100&page=(\d+)$", path)
    if method == "GET" and m:
        page = int(m.group(2))
        return [{"body": b} for n, b in pr_comments if n == m.group(1)][(page - 1) * 100:page * 100]
    m = re.search(r"/pulls/(\d+)$", path)
    if method == "PATCH" and m:
        if description_failures: raise github.GitHubError(description_failures.pop(0))
        pr = next(p for p in prs.values() if p["url"].endswith("/" + m.group(1)))
        pr["body"] = payload["body"]
        return {}
    if method == "GET":
        if "/branches/" in path:
            return {"commit": {"sha": git("rev-parse", "main", cwd=board.origin).strip()}}
        number = re.search(r"/pulls/(\d+)$", path)
        if number:
            if number.group(1) == "42":
                return {"title": "Add main behaviour"}
            pr = next(p for p in prs.values() if p["url"].endswith("/" + number.group(1)))
            return {"state": pr.get("state", "open"), "merged": pr.get("merged", False),
                    "head": {"sha": git("rev-parse", pr["head"], cwd=board.origin).strip()}}
        head = re.search(r"head=([^&]+)", path).group(1).replace("%3A", ":").split(":")[1]
        return [{"html_url": prs[head]["url"], "body": prs[head]["body"]}] if head in prs else []
    if pr_failures: raise github.GitHubError(pr_failures.pop(0))
    prs[payload["head"]] = {"url": PR_URL.format(len(prs) + 1), **payload}
    return {"html_url": prs[payload["head"]]["url"]}
real_gh_request = github._request
github._request = fake_gh_request

# ---- sandbox stand-in: same call shape, no sudo --------------------------
real_run_sandboxed = proc.run_sandboxed
sandbox_calls = []
def fake_run_sandboxed(cfg, command, cwd, timeout, run_as=None, env_file=None, limit=proc.DEFAULT_OUTPUT_LIMIT, input_text=None):
    sandbox_calls.append({"command": list(command), "cwd": str(cwd), "run_as": run_as, "env_file": env_file})
    return proc.run_capped(command, cwd=cwd, timeout=timeout, limit=limit, input_text=input_text)
proc.run_sandboxed = fake_run_sandboxed


class Base(unittest.TestCase):
    mode = "edit"
    # Main checks take an agent's slot and add runs, so only tests about them turn them on.
    main_checks = False

    def setUp(self):
        board.reset(); prs.clear(); gh_tokens.clear(); app.clear(); pr_failures.clear(); statuses.clear(); status_failures.clear(); reviews.clear(); review_failures.clear(); review_reads.clear(); pr_comments.clear(); comment_failures.clear(); description_failures.clear(); sandbox_calls.clear(); dd_main._skipped.clear(); dd_main._waiting.clear(); choose._held.clear(); cleanup._warned.clear()
        self.tmp = temporary_directory(self)
        if os.environ.get("DD_TEST_GIT"):
            bindir = self.tmp / "bin"
            bindir.mkdir()
            (bindir / "git").symlink_to(GIT)
            path_patch = mock.patch.dict(os.environ, {"PATH": str(bindir) + os.pathsep + os.environ["PATH"]})
            path_patch.start()
            self.addCleanup(path_patch.stop)
        self.origin = self.tmp / "origin.git"
        board.origin = self.origin
        git("init", "--bare", "-b", "main", str(self.origin), cwd=self.tmp)
        seed = self.tmp / "seed"
        git("clone", str(self.origin), str(seed), cwd=self.tmp)
        (seed / "app.py").write_text("print('hi')\n")
        (seed / "DEFINITION_OF_DONE.md").write_text("- tests pass\n- PR explains why\n")
        (seed / ".gitignore").write_text("__pycache__/\n")
        git("add", "-A", cwd=seed)
        git("-c", "user.name=Seed", "-c", "user.email=s@x", "commit", "-m", "seed", cwd=seed)
        git("push", "origin", "main", cwd=seed)
        self.repo = self.tmp / "repo"
        git("clone", str(self.origin), str(self.repo), cwd=self.tmp)
        self.wt_root = self.tmp / "worktrees"; self.wt_root.mkdir()
        self.marker = self.tmp / "HOOK_RAN"
        os.environ["MARKER"] = str(self.marker)
        os.environ["LEAK"] = ""

        self.cfg = {
            "db_path": str(self.tmp / "runs.db"),
            "plane": {"base_url": "http://plane", "workspace_slug": "ws", "orchestrator_token": ORCH},
            "github": {"token": GH},
            "sandbox": {"user": "dd-agent", "launcher": "/usr/local/bin/dd-agent-run"},
            "agents": {"claude-code": {
                "display_name": "Claude", "plane_token": CLAUDE,
                "command": [PY, str(HERE / "fake_agent.py"), "{prompt}", self.mode],
                "parser": "claude_json", "env_file": "/etc/dynamic-dispatch/claude.env",
                "git_author": {"name": "Claude", "email": "claude@example.com"},
                "timeout_seconds": 60}},
            "projects": {"dd": {
                "plane_project_id": PID, "github_repo": "owner/repo",
                "repo_path": str(self.repo), "worktree_root": str(self.wt_root),
                "base_branch": "main", "default_agent": "claude-code", "branch_prefix": "dd",
                "verify_command": [PY, "-m", "compileall", "-q", "."],
                "definition_of_done_path": "DEFINITION_OF_DONE.md",
                "states": {"in_progress": S_IP, "in_review": S_REV, "blocked": S_BLK}}},
        }
        redact.set_secrets([ORCH, CLAUDE, GH, MODEL, CODEX])
        group_patch = mock.patch.object(config, "agent_group", return_value=os.getegid())
        group_patch.start()
        self.addCleanup(group_patch.stop)
        if not self.main_checks:
            main_patch = mock.patch.object(mainhealth, "poll")
            main_patch.start()
            self.addCleanup(main_patch.stop)
        self.conn = runs.connect(self.cfg["db_path"])
        self.addCleanup(self.conn.close)

    def wait_assignments(self):
        if plane._assignments:
            plane._assignments.executor.submit(lambda: None).result(timeout=10)

    def tearDown(self):
        self.wait_assignments()
        if plane._assignments:
            plane._assignments.executor.shutdown()
            plane._assignments = None

    def add_issue(self, iid="i1", seq=7, state=S_IP, nested=True, labels=()):
        board.issues[iid] = {"id": iid, "sequence_id": seq, "name": "Add a value",
                             "description_stripped": "Add VALUE to feature.py",
                             "state": {"id": state} if nested else state,
                             "labels": list(labels)}

    def cycle(self):
        dd_main.cycle(self.cfg, self.conn)
        self.wait_assignments()

    def run_rows(self):
        conn = runs.connect(self.cfg["db_path"], initialize=False)
        try:
            return [dict(r) for r in conn.execute("SELECT * FROM runs ORDER BY id")]
        finally:
            conn.close()

    def set_mode(self, mode):
        self.cfg["agents"]["claude-code"]["command"][-1] = mode


def temporary_directory(test):
    """Register removal immediately, including when fixture setup fails."""
    directory = tempfile.TemporaryDirectory()
    test.addCleanup(directory.cleanup)
    return Path(directory.name)


class MainChanges:
    def advance_main(self, conflict=False, message="Merge pull request #42 from topic"):
        seed = self.tmp / "seed"
        (seed / ("feature.py" if conflict else "main.py")).write_text("MAIN = 2\n")
        git("add", "-A", cwd=seed)
        git("-c", "user.name=Main", "-c", "user.email=m@x", "commit", "-m",
            message, cwd=seed)
        git("push", "origin", "main", cwd=seed)


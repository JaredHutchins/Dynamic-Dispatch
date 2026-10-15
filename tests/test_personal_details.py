"""Keep personal and work details out of the repository's files."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
GIT = os.environ.get("DD_TEST_GIT") or "git"
FORBIDDEN_TERMS = (
    "jaredhutchins",
    "nomaddynamics",
    "nomad ops",
    "nomad-ops",
    "192.168.",
    "draftbase",
    "getac",
)
# Entries are (repository-relative file, term, one-line reason), for deliberate
# release uses such as a LICENSE author credit or a README public repository link.
ALLOWED_EXCEPTIONS = [
    ("deploy/dd-container-start", "jaredhutchins", "Public repository cloned on first container start."),
    ("README.md", "jaredhutchins", "Public repository URL for the main test status badge."),
]


def violations(root):
    """Forbidden terms in the files of root's working tree, as "file:line: term".

    DD checks a story's changes before committing them, so new files that
    aren't ignored count, and tracked files the story deleted are skipped.
    """
    # The suite disables global Git config, including checkout ownership trust.
    listed = subprocess.run(
        [GIT, "-c", f"safe.directory={root}", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root, check=True, capture_output=True,
    ).stdout
    allowed = {(file, term.lower()) for file, term, reason in ALLOWED_EXCEPTIONS}
    own_file = Path(__file__).resolve().relative_to(ROOT).as_posix() if root == ROOT else None
    found = []
    for name in sorted(set(listed.split(b"\0"))):
        if not name:
            continue
        file = os.fsdecode(name)
        path = root / file
        if file == own_file or not path.is_file():
            continue
        data = path.read_bytes()
        # NUL bytes distinguish binary assets without relying on extensions.
        if b"\0" in data:
            continue
        for number, line in enumerate(data.splitlines(), 1):
            for term in FORBIDDEN_TERMS:
                if (file, term) not in allowed and term.encode("ascii") in line.lower():
                    found.append(f"{file}:{number}: {term}")
    return found


class PersonalDetails(unittest.TestCase):
    def test_repository_files_have_no_personal_details(self):
        found = violations(ROOT)
        self.assertFalse(found, "Forbidden personal or work details:\n" + "\n".join(found))

    def repository(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        # Keep the host's Git config (hooks or signing, say) out of the throwaway repository.
        env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
        subprocess.run([GIT, "init", "-q"], cwd=root, env=env, check=True, capture_output=True)
        return root, env

    def test_tracked_file_deleted_in_working_tree_is_skipped(self):
        root, env = self.repository()
        (root / "moved.py").write_text("x = 1\n")
        for args in (["add", "moved.py"],
                     ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-q", "-m", "Add"]):
            subprocess.run([GIT, *args], cwd=root, env=env, check=True, capture_output=True)
        (root / "moved.py").unlink()
        self.assertEqual(violations(root), [])

    def test_new_untracked_file_is_checked(self):
        root, env = self.repository()
        (root / ".gitignore").write_text("ignored.txt\n")
        (root / "ignored.txt").write_text("draftbase\n")
        (root / "new.py").write_text("ok = 1\nhost = '192.168.1.2'\n")
        self.assertEqual(violations(root), ["new.py:2: 192.168."])


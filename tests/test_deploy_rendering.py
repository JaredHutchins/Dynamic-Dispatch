"""Exercise the documented template rendering commands."""

from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import textwrap
import unittest



DEPLOY = Path(__file__).resolve().parents[1] / "deploy"


class DeployRendering(unittest.TestCase):
    def test_readme_render_absolute_unquoted_service_path(self):
        readme = (DEPLOY / "README.md").read_text()
        documented = textwrap.dedent(
            readme.split("    python3 - <<'RENDER'\n", 1)[1].split("    RENDER", 1)[0])
        for folder in ("checkout", "DD checkout's 50% $(false)"):
            with self.subTest(folder=folder), tempfile.TemporaryDirectory() as tmp:
                checkout = Path(tmp) / folder
                shutil.copytree(DEPLOY, checkout / "deploy")
                target = Path(tmp) / "rendered"
                target.mkdir()
                script = "\n".join([
                    "export DD_USER=dd-owner",
                    f"export DD_CHECKOUT={shlex.quote(str(checkout))}",
                    f"export DD_DEPLOY_TMP={shlex.quote(str(target))}",
                    "python3 - <<'RENDER'\n" + documented + "RENDER"])
                subprocess.run(["sh", "-eu", "-c", script], check=True, capture_output=True, text=True)
                service = (target / "dynamic-dispatch.service").read_text()
                self.assertIn("User=dd-owner\n", service)
                working = next(line.split("=", 1)[1] for line in service.splitlines()
                               if line.startswith("WorkingDirectory="))
                self.assertTrue(Path(working).is_absolute())
                self.assertEqual(working, str(checkout).replace("%", "%%"))
                self.assertFalse(working.startswith(('"', "'")))
                rule = (target / "sudoers-dynamic-dispatch").read_text()
                self.assertIn("dd-owner ALL=(dd-agent, dd-codex)", rule)
                for rendered in (service, rule):
                    self.assertNotIn("DD_USER", rendered)
                    self.assertNotIn("DD_CHECKOUT", rendered)

"""Exercise the documented and alert-provided template rendering commands."""

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from unittest import mock

from dispatch import selfupdate


DEPLOY = Path(__file__).resolve().parents[1] / "deploy"


class DeployRendering(unittest.TestCase):
    def test_readme_and_alert_render_absolute_unquoted_service_path(self):
        readme = (DEPLOY / "README.md").read_text()
        documented = textwrap.dedent(
            readme.split("    python3 - <<'RENDER'\n", 1)[1].split("    RENDER", 1)[0])
        for folder in ("checkout", "DD checkout's 50% $(false)"):
            with self.subTest(folder=folder), tempfile.TemporaryDirectory() as tmp:
                checkout = Path(tmp) / folder
                shutil.copytree(DEPLOY, checkout / "deploy")
                target = Path(tmp) / "rendered"
                target.mkdir()
                with mock.patch.object(selfupdate.pwd, "getpwuid") as account:
                    account.return_value.pw_name = "dd-owner"
                    commands = selfupdate._commands(checkout, [
                        "deploy/dynamic-dispatch.service", "deploy/sudoers-dynamic-dispatch"])
                    account.assert_called_once_with(os.geteuid())
                self.assertIn("export DD_USER=dd-owner", commands)
                self.assertIn(f"export DD_CHECKOUT={shlex.quote(str(checkout))}", commands)
                alert_render = next(c for c in commands if c.startswith("python3 -"))
                self.assertEqual(alert_render, "python3 - <<'RENDER'\n" + documented + "RENDER")
                for install in (c for c in commands if "sudo install" in c):
                    self.assertIn('"$DD_DEPLOY_TMP/', install)
                    self.assertNotIn(str(checkout / "deploy"), install)
                sudoers = next(c for c in commands if "sudo visudo -cf" in c)
                self.assertIn('sudo visudo -cf "$DD_DEPLOY_TMP/sudoers-dynamic-dispatch" && '
                              'sudo install', sudoers)
                # Use the alert's actual account/path assignments, with an owned temp target.
                script = "\n".join(commands[1:3] + [
                    f"export DD_DEPLOY_TMP={shlex.quote(str(target))}", alert_render])
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

"""Tests for processes."""

import os, re, subprocess, tempfile, time, unittest
from unittest import mock
from pathlib import Path
from dispatch import agents, proc, text
import main as dd_main
from helpers import (
    Base,
    HERE,
    PY,
    REPO_CODE,
    os,
    real_run_sandboxed,
)


class Reaping(unittest.TestCase):
    """H3: every sandboxed call is bracketed by killing the sandbox user's processes."""
    cfg = {"sandbox": {"user": "dd-agent", "launcher": "/usr/local/bin/dd-agent-run"}}

    def run_with(self, outcomes):
        """outcomes: returncode for each successive run_capped call (or an exception)."""
        calls, queue = [], list(outcomes)
        def fake(command, **kw):
            calls.append(command)
            out = queue.pop(0)
            if isinstance(out, Exception): raise out
            return proc.Result(out, "", "", False, False)
        original = proc.run_capped
        proc.run_capped = fake
        try:
            try:
                result = real_run_sandboxed(self.cfg, ["claude", "-p", "x"], "/srv/w/dd-1", 60)
            except Exception as e:
                result = e
        finally:
            proc.run_capped = original
        return calls, result

    def test_unreadable_test_secrets_file_refuses_before_reaping(self):
        # Moved away, or replaced by something that can't be read as a file.
        with tempfile.TemporaryDirectory() as folder:
            for path in (Path(folder) / "moved.env", Path(folder)):
                with self.subTest(path=path.name):
                    calls, result = [], None
                    with mock.patch.object(proc, "run_capped", side_effect=lambda command, **kw: calls.append(command)):
                        result = real_run_sandboxed(self.cfg, ["claude", "-p", "x"], "/srv/w/dd-1", 60,
                                                    project_cfg={"test_env_file": str(path)})
                    self.assertEqual(calls, [])
                    self.assertIsNone(result.returncode)
                    self.assertIn("test_env_file", proc.describe_failure(result, 60))
                    self.assertNotIn(folder, result.stderr)

    def test_reaps_before_and_after(self):
        calls, result = self.run_with([0, 0, 0])
        self.assertEqual(result.returncode, 0)
        reap = ["sudo", "-n", "-u", "dd-agent", "/usr/local/bin/dd-agent-run", "--reap"]
        self.assertEqual(calls[0], reap); self.assertEqual(calls[2], reap)
        self.assertEqual(calls[1][-3:], ["claude", "-p", "x"])

    def test_failed_reap_before_does_not_start(self):
        calls, result = self.run_with([1])
        self.assertEqual(len(calls), 1)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("before starting", proc.describe_failure(result, 60))

    def test_failed_reap_after_fails_the_call(self):
        calls, result = self.run_with([0, 0, 1])
        self.assertIsNone(result.returncode)
        self.assertIn("after the run", proc.describe_failure(result, 60))

    def test_reaps_after_even_if_the_call_raises(self):
        calls, result = self.run_with([0, OSError("no sudo"), 0])
        self.assertIsInstance(result, OSError)
        self.assertEqual(calls[-1][-1], "--reap")

    def test_launcher_parses(self):
        subprocess.run(["bash", "-n", str(REPO_CODE / "deploy" / "dd-agent-run")], check=True)



class Service(unittest.TestCase):
    """The systemd unit that replaces starting DD by hand with nohup."""

    def unit(self):
        import configparser
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        parser.optionxform = str  # systemd keys are case-sensitive
        parser.read(REPO_CODE / "deploy" / "dynamic-dispatch.service")
        return parser

    def test_runs_main_with_account_and_checkout_placeholders(self):
        service = self.unit()["Service"]
        self.assertEqual(service["User"], "DD_USER")
        self.assertEqual(service["WorkingDirectory"], "DD_CHECKOUT")
        self.assertEqual(service["ExecStart"].split()[-1], "main.py")
        self.assertEqual(os.path.basename(service["ExecStart"].split()[0]), "python3")

    def test_restarts_after_a_delay(self):
        service = self.unit()["Service"]
        self.assertEqual(service["Restart"], "on-failure")
        self.assertEqual(service["RestartSec"], "10")

    def test_umask_matches_main(self):
        self.assertEqual(int(self.unit()["Service"]["UMask"], 8), dd_main.UMASK)

    def test_starts_at_boot_after_the_network(self):
        unit = self.unit()
        self.assertIn("network-online.target", unit["Unit"]["After"])
        self.assertIn("network-online.target", unit["Unit"]["Wants"])
        self.assertEqual(unit["Install"]["WantedBy"], "multi-user.target")
        self.assertEqual(unit["Service"]["StandardOutput"], "journal")



class Output(unittest.TestCase):
    def test_capped_buffer_keeps_head_and_tail(self):
        r = proc.run_capped([PY, "-c", "import sys; sys.stdout.write('A'*1000 + 'B'*5_000_000 + 'C'*1000)"], limit=10_000)
        self.assertTrue(r.stdout_truncated)
        self.assertTrue(r.stdout.startswith("A" * 1000))
        self.assertTrue(r.stdout.endswith("C" * 1000))
        self.assertLess(len(r.stdout), 25_000)

    def test_timeout(self):
        t = time.time()
        r = proc.run_capped([PY, "-c", "import time; time.sleep(30)"], timeout=1)
        self.assertTrue(r.timed_out); self.assertLess(time.time() - t, 15)

    def test_sandbox_wrapper_shape(self):
        cfg = {"sandbox": {"user": "dd-agent", "launcher": "/usr/local/bin/dd-agent-run"}}
        cmd = proc.sandboxed(cfg, ["claude", "-p", "x"], "/srv/w/dd-1", 60, env_file="/etc/e")
        self.assertEqual(cmd[:5], ["sudo", "-n", "-u", "dd-agent", "/usr/local/bin/dd-agent-run"])
        self.assertEqual(cmd[cmd.index("--") + 1:], ["claude", "-p", "x"])



class PromptInput(unittest.TestCase):
    def test_agent_prompt_transport(self):
        cfg = {"sandbox": {"user": "dd-agent", "launcher": "/usr/local/bin/dd-agent-run"}}
        original = proc.run_capped
        for via in (None, "argument", "stdin"):
            for style in ("claude", "codex"):
                with self.subTest(via=via, style=style), tempfile.TemporaryDirectory() as folder:
                    prompt = "Instructions: quotes ' \" $() {prompt} café 😀\r\n" * (6000 if via == "stdin" else 1)
                    if via == "stdin":
                        self.assertGreater(len(prompt.encode("utf-8")), 200 * 1024)
                    agent = {"display_name": "Fake", "parser": style + "_json",
                             "command": [PY, str(HERE / "fake_agent.py"),
                                         "-" if via == "stdin" else "{prompt}", "prompt", style]}
                    if via is not None:
                        agent["prompt_via"] = via
                    calls = []
                    def run_wrapped(command, **kwargs):
                        calls.append((command, kwargs))
                        if command[-1] == "--reap":
                            self.assertNotIn("input_text", kwargs)
                            return proc.Result(0, "", "", False, False)
                        self.assertEqual(command[:5], ["sudo", "-n", "-u", "dd-agent", cfg["sandbox"]["launcher"]])
                        if via == "stdin":
                            self.assertEqual(kwargs["input_text"], prompt)
                            self.assertFalse(any(prompt in part for part in command))
                            self.assertNotIn("{prompt}", " ".join(command))
                        else:
                            self.assertIsNone(kwargs["input_text"])
                            self.assertIn(prompt, command)
                        return original(command[command.index("--") + 1:], cwd=folder, **kwargs)
                    with mock.patch.object(proc, "run_sandboxed", real_run_sandboxed), \
                         mock.patch.object(proc, "run_capped", side_effect=run_wrapped):
                        result = agents.run(cfg, agent, prompt, folder)
                    self.assertTrue(result["ok"], result)
                    self.assertEqual((Path(folder) / "prompt.txt").read_bytes(), prompt.encode("utf-8"))
                    self.assertEqual(len(calls), 3)

    def test_input_does_not_block_output_or_caps(self):
        command = [PY, "-c", "import sys; sys.stdout.write('x'*300000); sys.stdout.flush(); "
                   "data = sys.stdin.buffer.read(); sys.stderr.write(str(len(data)))"]
        result = proc.run_capped(command, input_text="y" * 300000, limit=1000, timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "300000")
        self.assertTrue(result.stdout_truncated)
        self.assertLess(len(result.stdout), 2200)

    def test_timeout_when_process_does_not_read_input(self):
        result = proc.run_capped([PY, "-c", "import time; time.sleep(30)"],
                                 input_text="x" * 300000, timeout=0.1)
        self.assertTrue(result.timed_out)

    def test_process_can_close_input_early(self):
        result = proc.run_capped([PY, "-c", "import os; os.close(0); print('done')"],
                                 input_text="x" * 300000, timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "done")



class Sudoers(unittest.TestCase):
    def test_launcher_runs_as_dd_agent_or_dd_codex_only(self):
        lines = [l.strip() for l in (REPO_CODE / "deploy" / "sudoers-dynamic-dispatch").read_text().splitlines()
                 if l.strip() and not l.lstrip().startswith("#")]
        rules = [l for l in lines if not l.startswith("Defaults")]
        self.assertEqual(rules, ["DD_USER ALL=(dd-agent, dd-codex) NOPASSWD: /usr/local/bin/dd-agent-run"])



class FourthReview(Base):
    def others_alive(self, processes):
        """Run the launcher's others_alive against a fake /proc; True if it reports one alive."""
        fake = Path(tempfile.mkdtemp(dir=self.tmp))
        for pid, files in processes.items():
            (fake / pid).mkdir(parents=True)
            for name, text in files.items():
                (fake / pid / name).write_text(text)
        launcher = (REPO_CODE / "deploy" / "dd-agent-run").read_text()
        func = re.search(r"^others_alive\(\) \{.*?^\}$", launcher, re.S | re.M).group(0)
        self.assertEqual(func.count("/proc/"), 2)  # the glob, and stripping it back to a pid
        script = func.replace("/proc/", f"{fake}/") + "\nothers_alive"
        return subprocess.run(["bash", "-c", script]).returncode == 0


    def test_reap_check_reads_state_from_status(self):
        zombie = {"status": "Name:\tx\nState:\tZ (zombie)\nThreads:\t1\n", "stat": "10 (x) Z 1\n"}
        self.assertFalse(self.others_alive({"10": zombie}))
        # A name built to fake a zombie in stat, where it appears unescaped.
        # In status the kernel escapes it, so it can't forge a State: line.
        tricky = {"stat": "11 (a) Z \n) S 1\n",
                  "status": "Name:\ta) Z \\nState: Z\\nThreads: 1\nState:\tS (sleeping)\nThreads:\t1\n"}
        self.assertTrue(self.others_alive({"10": zombie, "11": tricky}))


    def test_reap_check_zombie_leader_with_live_threads_is_alive(self):
        # The main thread has exited, but another thread of the process still runs.
        self.assertTrue(self.others_alive({"14": {"status": "Name:\tx\nState:\tZ (zombie)\nThreads:\t2\n"}}))
        self.assertTrue(self.others_alive({"15": {"status": "Name:\tx\nState:\tZ (zombie)\n"}}))  # count unknown


    def test_reap_check_counts_unreadable_as_alive(self):
        self.assertTrue(self.others_alive({"12": {}}))  # no status file, but the process is there
        self.assertTrue(self.others_alive({"13": {"status": "Name:\tx\n"}}))  # no State: line


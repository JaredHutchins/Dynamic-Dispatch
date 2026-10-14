"""Tests for config."""

import json, os, stat, sys, unittest
from unittest import mock
from dispatch import agents, config, redact
import main as dd_main
from helpers import (
    MODEL,
    REPO_CODE,
    os,
    temporary_directory,
)


class Config(unittest.TestCase):
    def write(self, cfg, mode=0o600):
        d = temporary_directory(self); p = d / "config.json"
        p.write_text(json.dumps(cfg)); p.chmod(mode); return p

    def base(self, key_mode=0o600):
        """The example, with its GitHub App key path pointed at a fake key."""
        c = json.loads((REPO_CODE / "config.example.json").read_text())
        key = temporary_directory(self) / "app.pem"
        key.write_text("fake key\n"); key.chmod(key_mode)
        c["github"]["private_key_path"] = str(key)
        return c

    def test_plane_api_url_is_optional_and_validated(self):
        c = self.base()
        self.assertNotIn("api_url", config.load(self.write(c))["plane"])
        for invalid in (None, "", 42, "ftp://plane.invalid", "https://", "https://user:pw@plane.invalid",
                        "https://plane.invalid?query", "https://plane.invalid/#fragment", " https://plane.invalid"):
            c["plane"]["api_url"] = invalid
            with self.subTest(invalid=invalid), self.assertRaisesRegex(config.ConfigError, "plane api_url"):
                config.load(self.write(c))
        c["plane"]["api_url"] = "https://api.plane.so"
        self.assertEqual(config.load(self.write(c))["plane"]["api_url"], "https://api.plane.so")

    def test_prompt_via_validation(self):
        c = self.base()
        agent = c["agents"]["codex"]
        for invalid in ("pipe", "", None, True, []):
            agent["prompt_via"] = invalid
            with self.subTest(invalid=invalid), self.assertRaisesRegex(config.ConfigError, "codex.*prompt_via must"):
                config.load(self.write(c))
        agent["prompt_via"] = "stdin"
        for placeholder in ("{prompt}", "--prompt={prompt}"):
            agent["command"] = ["codex", placeholder]
            with self.assertRaisesRegex(config.ConfigError, "codex.*must not contain.*prompt.*stdin"):
                config.load(self.write(c))
        agent["command"] = ["codex", "-"]
        for via in (None, "argument"):
            if via is None:
                del agent["prompt_via"]
            else:
                agent["prompt_via"] = via
            with self.assertRaisesRegex(config.ConfigError, "codex.*no.*prompt.*argument"):
                config.load(self.write(c))
        agent["command"] = ["codex", "{prompt}"]
        del agent["prompt_via"]
        loaded = config.load(self.write(c))
        self.assertEqual(loaded["agents"]["codex"]["prompt_via"], "argument")

    def test_done_state_is_optional(self):
        c = self.base()
        self.assertEqual(config.load(self.write(c))["projects"]["dynamic-dispatch"]["states"]["done"],
                         "PLANE_DONE_STATE_ID")
        for invalid in ("", " ", 7, []):
            c["projects"]["dynamic-dispatch"]["states"]["done"] = invalid
            with self.subTest(invalid=invalid), self.assertRaisesRegex(config.ConfigError, "states done must"):
                config.load(self.write(c))
        del c["projects"]["dynamic-dispatch"]["states"]["done"]
        self.assertNotIn("done", config.load(self.write(c))["projects"]["dynamic-dispatch"]["states"])

    def test_cancelled_state_is_optional_but_validated(self):
        c = self.base()
        for invalid in ('', ' ', 7, []):
            c['projects']['dynamic-dispatch']['states']['cancelled'] = invalid
            with self.subTest(invalid=invalid), self.assertRaisesRegex(config.ConfigError, 'states cancelled must'):
                config.load(self.write(c))
        del c['projects']['dynamic-dispatch']['states']['cancelled']
        config.load(self.write(c))

    def test_sprint_pull_is_optional(self):
        c = self.base()
        del c["projects"]["dynamic-dispatch"]["cycles"]
        del c["plane"]["approver_user_id"]
        loaded = config.load(self.write(c))
        self.assertIsNone(config.cycles(loaded["projects"]["dynamic-dispatch"]))
        self.assertNotIn("cycles", loaded["projects"]["dynamic-dispatch"])

    def test_sprint_pull_defaults(self):
        c = self.base()
        cycles = c["projects"]["dynamic-dispatch"]["cycles"]
        for key in ("max_stories", "replan_quiet_minutes", "blocked_reminder_minutes"):
            del cycles[key]
        loaded = config.cycles(config.load(self.write(c))["projects"]["dynamic-dispatch"])
        self.assertEqual((loaded["max_stories"], loaded["replan_quiet_minutes"],
                          loaded["blocked_reminder_minutes"]), (10, 15, 60))
        cycles.update(max_stories=3, replan_quiet_minutes=0, blocked_reminder_minutes=1)
        loaded = config.cycles(config.load(self.write(c))["projects"]["dynamic-dispatch"])
        self.assertEqual((loaded["max_stories"], loaded["replan_quiet_minutes"],
                          loaded["blocked_reminder_minutes"]), (3, 0, 1))

    def test_bad_sprint_pull_settings_refused(self):
        cases = [("cycles", []), ("product_backlog_state", None), ("sprint_backlog_state", ""),
                 ("carried_over_label_id", 7), ("max_stories", 0), ("max_stories", 2.5),
                 ("max_stories", True), ("replan_quiet_minutes", -1), ("replan_quiet_minutes", "15"),
                 ("replan_quiet_minutes", 1.5), ("replan_quiet_minutes", False),
                 ("blocked_reminder_minutes", 0), ("blocked_reminder_minutes", None),
                 ("blocked_reminder_minutes", 1.5), ("blocked_reminder_minutes", True)]
        for key, bad in cases:
            c = self.base()
            project = c["projects"]["dynamic-dispatch"]
            if key == "cycles":
                project["cycles"] = bad
            else:
                project["cycles"][key] = bad
            with self.subTest(key=key, bad=bad), self.assertRaisesRegex(config.ConfigError, "dynamic-dispatch.*cycles"):
                config.load(self.write(c))
        for missing in ("product_backlog_state", "sprint_backlog_state", "carried_over_label_id"):
            c = self.base(); del c["projects"]["dynamic-dispatch"]["cycles"][missing]
            with self.subTest(missing=missing), self.assertRaisesRegex(config.ConfigError, "cycles is missing: " + missing):
                config.load(self.write(c))

    def test_approver_user_id(self):
        c = self.base(); del c["plane"]["approver_user_id"]
        with self.assertRaisesRegex(config.ConfigError, "plane is missing: approver_user_id"):
            config.load(self.write(c))
        for bad in ("", " ", 7, None):
            c["plane"]["approver_user_id"] = bad
            with self.subTest(bad=bad), self.assertRaisesRegex(config.ConfigError, "approver_user_id"):
                config.load(self.write(c))
        # Without sprint-pull the setting is optional, but must still be an ID when given.
        del c["projects"]["dynamic-dispatch"]["cycles"]
        c["plane"]["approver_user_id"] = ""
        with self.assertRaisesRegex(config.ConfigError, "approver_user_id must"):
            config.load(self.write(c))
        c["plane"]["approver_user_id"] = "PLANE_APPROVER_USER_ID"
        config.load(self.write(c))

    def test_max_concurrent_only_accepts_one(self):
        c = self.base()
        del c["agents"]["codex"]["max_concurrent"]
        self.assertEqual(config.load(self.write(c))["agents"]["codex"]["max_concurrent"], 1)
        for maximum in (0, 2, -1, True, 1.0, "1", None):
            c["agents"]["codex"]["max_concurrent"] = maximum
            with self.assertRaisesRegex(config.ConfigError, "max_concurrent must be 1.*kill each other"):
                config.load(self.write(c))

    def test_example_is_valid(self):
        config.load(self.write(self.base()))

    def test_example_without_the_app_is_valid(self):
        c = self.base()
        for k in ("app_id", "installation_id", "private_key_path"): del c["github"][k]
        config.load(self.write(c))

    def test_invalid_group_name_refused(self):
        for group in (None, "", " ", 123, []):
            c = self.base(); c["agents"]["codex"]["group"] = group
            with self.assertRaisesRegex(config.ConfigError, "group must"):
                config.load(self.write(c))

    def test_readable_app_key_refused(self):
        for mode in (0o640, 0o604):
            with self.assertRaisesRegex(config.ConfigError, "must be private"):
                config.load(self.write(self.base(mode)))

    def test_missing_app_key_refused(self):
        c = self.base(); c["github"]["private_key_path"] = str(temporary_directory(self) / "none.pem")
        with self.assertRaisesRegex(config.ConfigError, "not a file"): config.load(self.write(c))

    def test_partial_app_settings_refused(self):
        c = self.base(); del c["github"]["installation_id"]
        with self.assertRaisesRegex(config.ConfigError, "installation_id"): config.load(self.write(c))

    def test_no_github_credential_refused(self):
        c = self.base(); c["github"] = {}
        with self.assertRaisesRegex(config.ConfigError, "token"): config.load(self.write(c))

    def test_readable_config_refused(self):
        with self.assertRaises(config.ConfigError): config.load(self.write(self.base(), 0o644))

    def test_env_block_refused(self):
        c = self.base(); c["agents"]["claude-code"]["env"] = {"X": "y"}
        with self.assertRaisesRegex(config.ConfigError, "env_file"): config.load(self.write(c))

    def test_missing_author_refused(self):
        c = self.base(); del c["agents"]["claude-code"]["git_author"]
        with self.assertRaises(config.ConfigError): config.load(self.write(c))

    def test_example_has_two_agents_on_their_own_accounts(self):
        c = config.load(self.write(self.base()))
        self.assertEqual(config.project_agents(c["projects"]["dynamic-dispatch"]), ["claude-code", "codex"])
        codex = c["agents"]["codex"]
        self.assertEqual((codex["run_as"], codex["parser"]), ("dd-codex", "codex_json"))
        self.assertNotIn("env_file", codex)
        self.assertIn("--ephemeral", codex["command"])
        self.assertEqual(codex["git_author"], {"name": "Codex", "email": "codex@example.com"})
        self.assertIn(codex["parser"], agents.PARSERS)

    def test_default_agent_still_works_as_a_one_item_list(self):
        c = self.base(); p = c["projects"]["dynamic-dispatch"]
        del p["agents"]; p["default_agent"] = "codex"
        self.assertEqual(config.project_agents(config.load(self.write(c))["projects"]["dynamic-dispatch"]), ["codex"])

    def test_unknown_or_empty_agents_refused(self):
        for bad in (["claude-code", "gemini"], [], "codex"):
            c = self.base(); c["projects"]["dynamic-dispatch"]["agents"] = bad
            with self.assertRaises(config.ConfigError, msg=bad): config.load(self.write(c))
        c = self.base(); del c["projects"]["dynamic-dispatch"]["agents"]
        with self.assertRaisesRegex(config.ConfigError, "agents"): config.load(self.write(c))

    def test_shared_run_as_refused(self):
        c = self.base(); c["agents"]["codex"]["run_as"] = "dd-agent"
        with self.assertRaisesRegex(config.ConfigError, "both run as dd-agent"): config.load(self.write(c))
        c = self.base(); del c["agents"]["claude-code"]["run_as"]; del c["agents"]["codex"]["run_as"]
        with self.assertRaisesRegex(config.ConfigError, "both run as"): config.load(self.write(c))

    def test_bad_cooldown_refused(self):
        for bad in (-1, "60", True):
            c = self.base(); c["usage_limit_cooldown_minutes"] = bad
            with self.assertRaises(config.ConfigError, msg=bad): config.load(self.write(c))
        c = self.base(); del c["usage_limit_cooldown_minutes"]
        self.assertEqual(config.cooldown_minutes(config.load(self.write(c))), 60)

    def test_blocked_sound_defaults_to_telegraph(self):
        c = self.base()
        self.assertNotIn("blocked_sound", c["alerts"])
        self.assertEqual(config.blocked_sound(config.load(self.write(c))), "telegraph")
        c["alerts"]["blocked_sound"] = "alarm"
        self.assertEqual(config.blocked_sound(config.load(self.write(c))), "alarm")

    def env_files(self, claude_mode, codex_mode, codex_gid=200):
        """Two agents' env files, owned by root; claude's group 100, codex's 200.

        Fake accounts: dd-agent is uid 1001 in group 100, dd-codex uid 1002 in group 200.
        """
        c = self.base(); d = temporary_directory(self)
        modes = {"claude-code": (claude_mode, 100), "codex": (codex_mode, codex_gid)}
        stats = {}
        for name, (mode, gid) in modes.items():
            path = d / f"{name}.env"; path.write_text("KEY=value\n")
            c["agents"][name]["env_file"] = str(path)
            stats[str(path)] = os.stat_result((stat.S_IFREG | mode, 0, 0, 1, 0, gid, 10, 0, 0, 0))
        accounts = {"dd-agent": (1001, {100}), "dd-codex": (1002, {200})}
        real_stat = os.stat
        with mock.patch.object(config.os, "stat", side_effect=lambda p: stats.get(str(p)) or real_stat(p)), \
             mock.patch.object(config, "_account", side_effect=accounts.__getitem__):
            config.check_env_files(c)

    def test_private_env_files_accepted(self):
        self.env_files(0o640, 0o640)

    def test_env_file_readable_by_another_agent_refused(self):
        with self.assertRaisesRegex(config.ConfigError, "claude.*can be read by dd-codex"):
            self.env_files(0o644, 0o640)
        with self.assertRaisesRegex(config.ConfigError, "codex.*can be read by dd-agent"):
            self.env_files(0o640, 0o640, codex_gid=100)  # both files in a group dd-agent is in

    def test_agent_without_env_file_is_supported(self):
        c = self.base()
        self.assertNotIn("env_file", c["agents"]["codex"])
        d = temporary_directory(self); path = d / "claude.env"; path.write_text(f"KEY={MODEL}\n")
        c["agents"]["claude-code"]["env_file"] = str(path)
        c = config.load(self.write(c))
        # Claude's file is private to its group; Codex has none to check.
        st = os.stat_result((stat.S_IFREG | 0o640, 0, 0, 1, 0, 100, 10, 0, 0, 0))
        accounts = {"dd-agent": (1001, {100}), "dd-codex": (1002, {200})}
        with mock.patch.object(config.os, "stat", return_value=st), \
             mock.patch.object(config, "_account", side_effect=accounts.__getitem__):
            config.check_env_files(c)
        try:
            redact.configure(c)
            self.assertEqual(redact.scrub(f"key {MODEL}"), f"key {redact.REPLACEMENT}")
        finally:
            redact.set_secrets([])

    def test_missing_agent_account_refused(self):
        c = self.base(); c["agents"]["codex"]["env_file"] = str(REPO_CODE / "config.example.json")
        del c["agents"]["claude-code"]["env_file"]
        with mock.patch.object(config.pwd, "getpwnam", side_effect=KeyError("dd-agent")):
            with self.assertRaisesRegex(config.ConfigError, "not an account"):
                config.check_env_files(c)

    def test_main_refuses_to_start_on_a_readable_env_file(self):
        with mock.patch.object(config, "load", return_value={}), \
             mock.patch.object(config, "check_env_files", side_effect=config.ConfigError("can be read by")), \
             mock.patch.object(sys, "argv", ["main.py", "--once"]):
            self.assertEqual(dd_main.main(), 1)



class GroupConfig(unittest.TestCase):
    def setUp(self):
        self.cfg = {'sandbox': {'user': 'claude'}, 'agents': {'claude': {}, 'codex': {'run_as': 'codex', 'group': 'codex-group'}}}
        self.accounts = {'claude': (101, {10}), 'codex': (102, {20})}
        patches = [
            mock.patch.object(config.pwd, 'getpwnam', side_effect=lambda n: mock.Mock(pw_gid={'claude': 10, 'codex': 20}[n])),
            mock.patch.object(config.pwd, 'getpwuid', return_value=mock.Mock(pw_name='orchestrator')),
            mock.patch.object(config.grp, 'getgrnam', return_value=mock.Mock(gr_gid=20)),
            mock.patch.object(config.grp, 'getgrgid', side_effect=lambda gid: mock.Mock(gr_name={10: 'claude-group', 20: 'codex-group'}[gid])),
            mock.patch.object(config.os, 'getgroups', return_value=[10, 20]),
            mock.patch.object(config.os, 'getegid', return_value=30),
            mock.patch.object(config, '_account', side_effect=self.accounts.__getitem__),
        ]
        for patch in patches:
            patch.start(); self.addCleanup(patch.stop)

    def test_default_primary_group_and_explicit_group(self):
        self.assertEqual(config.agent_group(self.cfg, 'claude'), 10)
        self.assertEqual(config.agent_group(self.cfg, 'codex'), 20)
        config.check_groups(self.cfg)

    def test_missing_explicit_group_names_account_and_group(self):
        with mock.patch.object(config.grp, "getgrnam", side_effect=KeyError):
            with self.assertRaisesRegex(config.ConfigError, "account codex: group 'codex-group' does not exist"):
                config.check_groups(self.cfg)

    def test_missing_run_as_account_is_actionable(self):
        with mock.patch.object(config.pwd, "getpwnam", side_effect=KeyError):
            with self.assertRaisesRegex(config.ConfigError, "account claude does not exist"):
                config.check_groups(self.cfg)

    def test_missing_primary_group_names_account_and_group_id(self):
        with mock.patch.object(config.grp, "getgrgid", side_effect=KeyError):
            with self.assertRaisesRegex(config.ConfigError, "account claude: primary group ID 10 does not exist"):
                config.check_groups(self.cfg)

    def test_missing_orchestrator_membership(self):
        with mock.patch.object(config.os, 'getgroups', return_value=[10]):
            with self.assertRaisesRegex(config.ConfigError, 'orchestrator.*codex-group'):
                config.check_groups(self.cfg)

    def test_overlapping_agent_membership(self):
        self.accounts['codex'] = (102, {10, 20})
        with self.assertRaisesRegex(config.ConfigError, 'codex.*claude-group'):
            config.check_groups(self.cfg)

    def test_missing_own_membership(self):
        self.accounts['codex'] = (102, {30})
        with self.assertRaisesRegex(config.ConfigError, 'codex.*codex-group'):
            config.check_groups(self.cfg)

    def test_startup_refuses_invalid_groups(self):
        with mock.patch.object(config, 'load', return_value=self.cfg), \
                mock.patch.object(config, 'check_env_files'), \
                mock.patch.object(config.os, 'getgroups', return_value=[]), \
                mock.patch.object(sys, 'argv', ['main.py', '--once']):
            self.assertEqual(dd_main.main(), 1)


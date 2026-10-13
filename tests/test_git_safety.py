"""Tests for git safety."""

import errno, os, shutil, stat, subprocess
from unittest import mock
from pathlib import Path
from dispatch import gitops, runs
import main as dd_main
from helpers import (
    Base,
    GIT,
    REPO_CODE,
    S_BLK,
    app,
    board,
    git,
    os,
)


class Worktrees(Base):
    def test_deleted_worktree_dir_is_pruned_and_recreated(self):
        self.set_mode("fail"); self.add_issue(); self.cycle()
        shutil.rmtree(self.wt_root / "dd-7")
        path, reused = gitops.ensure_worktree(str(self.repo), str(self.wt_root), "dd-7", "main")
        self.assertTrue(path.is_dir()); self.assertTrue(reused)

    def test_unrelated_directory_at_target_is_refused(self):
        (self.wt_root / "dd-7").mkdir()
        self.add_issue(); self.cycle()
        [row] = self.run_rows()
        self.assertEqual(row["status"], runs.FAILED)
        self.assertIn("is not a worktree", row["error"])

    def test_group_write_granted(self):
        path, _ = gitops.ensure_worktree(str(self.repo), str(self.wt_root), "dd-9", "main")
        self.assertTrue(os.stat(path / "app.py").st_mode & stat.S_IWGRP)



class GitSafety(Base):
    def test_revision_and_history_reads_hold_repository_lock(self):
        before = git("rev-parse", "HEAD", cwd=self.repo).strip()
        git("-c", "user.name=Test", "-c", "user.email=t@x", "commit", "--allow-empty",
            "-m", "next", cwd=self.repo)
        after = git("rev-parse", "HEAD", cwd=self.repo).strip()
        original = gitops._git
        def checked(*args, **kwargs):
            self.assertTrue(gitops.repository_lock(self.repo)._is_owned())
            return original(*args, **kwargs)
        with mock.patch.object(gitops, "_git", side_effect=checked):
            self.assertEqual(gitops.branch_revision(self.repo, "main"), after)
            self.assertEqual(gitops.pushed_revision(self.repo, "main"), before)
            self.assertIsNone(gitops.pushed_revision(self.repo, "missing"))
            self.assertEqual(gitops.first_parent_commits_since(self.repo, before, after), [after])
            self.assertEqual(gitops.first_parent_commits_since(self.repo, after, after), [])
            self.assertIsNone(gitops.first_parent_commits_since(self.repo, after, before))

    def test_origin_check_reads_without_changing_refs(self):
        refs = lambda path: git("for-each-ref", cwd=path) + git("rev-parse", "HEAD", cwd=path)
        before = refs(self.repo), refs(self.origin)
        gitops.check_origin(self.repo)
        self.assertEqual((refs(self.repo), refs(self.origin)), before)
        git("remote", "set-url", "origin", str(self.tmp / "missing.git"), cwd=self.repo)
        with self.assertRaises(gitops.GitError):
            gitops.check_origin(self.repo)

    def test_hooks_do_not_run_and_git_file_hijack_is_ignored(self):
        hook = self.repo / ".git" / "hooks" / "pre-commit"
        hook.write_text(f"#!/bin/sh\ntouch {self.marker}\n"); hook.chmod(0o755)
        self.set_mode("hijack"); self.add_issue(); self.cycle()
        self.assertFalse(self.marker.exists(), "a hook ran as the orchestrator")
        [row] = self.run_rows()
        self.assertEqual(row["status"], runs.DONE, row)
        self.assertIn("VALUE = 2", git("show", "dd-7:feature.py", cwd=self.origin))



class SixthReview(Base):
    """Tests for the sixth and seventh Codex reviews: setup failures aren't ignored."""

    def assert_setup_refused(self, branch):
        with self.assertRaisesRegex(gitops.GitError, "writable for the agent"):
            gitops.ensure_worktree(str(self.repo), str(self.wt_root), branch, "main")
        self.assertFalse((self.wt_root / branch).exists())
        self.assertIsNone(gitops.registered_worktree(str(self.repo), branch))

    def test_permission_change_failure_stops_setup(self):
        denied = PermissionError(errno.EPERM, "Operation not permitted")
        with mock.patch.object(gitops.os, "fchmod", side_effect=denied):
            self.assert_setup_refused("dd-50")
        # Nothing was marked ready, so the next attempt builds it properly.
        target, _ = gitops.ensure_worktree(str(self.repo), str(self.wt_root), "dd-50", "main")
        self.assertTrue(os.stat(target / "app.py").st_mode & stat.S_IWGRP)

    def test_file_that_cannot_be_opened_stops_setup(self):
        real_open = os.open
        def refuse_app(path, flags, *a, **kw):
            if path == "app.py" and flags & os.O_NOFOLLOW:
                raise PermissionError(errno.EACCES, "Permission denied")
            return real_open(path, flags, *a, **kw)
        with mock.patch.object(gitops.os, "open", side_effect=refuse_app):
            self.assert_setup_refused("dd-51")

    def test_unreadable_directory_stops_the_walk(self):
        tree = self.tmp / "tree"; (tree / "locked").mkdir(parents=True)
        (tree / "locked").chmod(0)
        self.addCleanup((tree / "locked").chmod, 0o755)
        with self.assertRaisesRegex(gitops.GitError, "writable for the agent"):
            gitops.grant_group_write(tree)

    def test_ready_mark_failure_stops_setup(self):
        # Seventh review: the last step of setup fails like any other.
        full = OSError(errno.ENOSPC, "No space left on device")
        with mock.patch.object(Path, "touch", side_effect=full):
            with self.assertRaisesRegex(gitops.GitError, "Could not mark .* ready"):
                gitops.ensure_worktree(str(self.repo), str(self.wt_root), "dd-53", "main")
        self.assertFalse((self.wt_root / "dd-53").exists())
        self.assertIsNone(gitops.registered_worktree(str(self.repo), "dd-53"))

    def test_committed_symlinks_are_skipped_not_failed(self):
        (self.repo / "docs").mkdir()
        (self.repo / "docs" / "guide.md").write_text("x\n")
        (self.repo / "link-to-file").symlink_to("app.py")
        (self.repo / "link-to-dir").symlink_to("docs")
        (self.repo / "dangling").symlink_to("nowhere")
        git("add", "-A", cwd=self.repo)
        git("-c", "user.name=Seed", "-c", "user.email=s@x", "commit", "-m", "links", cwd=self.repo)
        git("push", "origin", "main", cwd=self.repo)
        target, _ = gitops.ensure_worktree(str(self.repo), str(self.wt_root), "dd-52", "main")
        self.assertTrue(os.stat(target / "docs" / "guide.md").st_mode & stat.S_IWGRP)
        self.assertTrue((gitops.admin_dir(str(self.repo), target) / gitops.READY).exists())



class PiUmask(Base):
    """Found in Pi setup: the login umask there is 002, which made new files group-writable."""

    def group_writable_in_git_dir(self):
        found = []
        for root, dirs, files in os.walk(self.repo / ".git"):
            for name in dirs + files:
                path = Path(root) / name
                if not path.is_symlink() and os.stat(path).st_mode & stat.S_IWGRP:
                    found.append(str(path.relative_to(self.repo)))
        return found

    def run_story_with_umask(self, umask):
        old = os.umask(umask)
        self.addCleanup(os.umask, old)
        # The clone as the setup steps leave it, after `chmod -R g-w`.
        subprocess.run(["chmod", "-R", "g-w", str(self.repo)], check=True)
        self.set_mode("fail")  # stop after the worktree exists, keeping it
        self.add_issue(); self.cycle()
        return self.wt_root / "dd-7"

    def test_pi_umask_would_expose_the_admin_directory(self):
        # Without the fix: what the orchestrator creates in the repo is group-writable.
        self.run_story_with_umask(0o002)
        self.assertTrue(any("worktrees" in p for p in self.group_writable_in_git_dir()))

    def test_orchestrator_umask_keeps_the_repo_read_only_to_the_group(self):
        worktree = self.run_story_with_umask(dd_main.UMASK)  # as main() sets it
        self.assertEqual(self.group_writable_in_git_dir(), [])
        # The worktree itself is still writable for the agent.
        self.assertTrue(os.stat(worktree).st_mode & stat.S_IWGRP)
        self.assertTrue(os.stat(worktree / "app.py").st_mode & stat.S_IWGRP)

    def test_main_sets_the_umask_first(self):
        source = (REPO_CODE / "main.py").read_text()
        body = source[source.index("def main():"):].splitlines()[1].strip()
        self.assertEqual(body, "os.umask(UMASK)")
        self.assertEqual(dd_main.UMASK, 0o022)



class AgentFolderGroups(Base):
    def test_new_checkout_and_dd_files_have_group_and_permissions(self):
        tree, _ = gitops.ensure_worktree(self.repo, self.wt_root, 'dd-7', 'main', group=os.getegid())
        self.assertEqual(stat.S_IMODE(tree.stat().st_mode), 0o2770)
        for path in (tree, tree / 'app.py', tree / '.git'):
            self.assertEqual(path.stat().st_gid, os.getegid())
            self.assertTrue(path.stat().st_mode & stat.S_IWGRP)
        (tree / 'new.txt').write_text('DD wrote this')
        gitops.grant_group_write(tree, owned_only=True, group=os.getegid())
        self.assertEqual((tree / 'new.txt').stat().st_gid, os.getegid())
        self.assertTrue((tree / 'new.txt').stat().st_mode & stat.S_IWGRP)

    def test_group_assignment_failure_removes_incomplete_checkout(self):
        denied = PermissionError(errno.EPERM, "Operation not permitted")
        with mock.patch.object(gitops.os, "fchown", side_effect=denied):
            with self.assertRaisesRegex(gitops.GitError, "writable for the agent"):
                gitops.ensure_worktree(self.repo, self.wt_root, "dd-7", "main", group=os.getegid())
        self.assertFalse((self.wt_root / "dd-7").exists())
        self.assertIsNone(gitops.registered_worktree(self.repo, "dd-7"))
        tree, _ = gitops.ensure_worktree(self.repo, self.wt_root, "dd-7", "main", group=os.getegid())
        self.assertEqual(tree.stat().st_gid, os.getegid())
        self.assertEqual(stat.S_IMODE(tree.stat().st_mode), 0o2770)
        self.assertTrue((gitops.admin_dir(self.repo, tree) / gitops.READY).exists())

    def test_group_walk_skips_links_and_foreign_owners(self):
        tree = self.tmp / 'tree'; tree.mkdir()
        outside = self.tmp / 'outside'; outside.mkdir()
        file = outside / 'file'; file.write_text('unchanged'); file.chmod(0o600)
        (tree / 'link').symlink_to(outside, target_is_directory=True)
        os.link(file, tree / 'hardlink')
        foreign = tree / 'foreign'; foreign.write_text('other owner'); foreign.chmod(0o600)
        original = os.fstat
        inode = foreign.stat().st_ino
        def info(fd):
            st = original(fd)
            if st.st_ino == inode:
                values = list(st); values[4] = os.geteuid() + 1
                return os.stat_result(values)
            return st
        with mock.patch.object(gitops.os, 'fstat', side_effect=info), \
                mock.patch.object(gitops.os, 'fchown', wraps=os.fchown) as chown:
            gitops.grant_group_write(tree, group=os.getegid())
        self.assertEqual(chown.call_count, 1)  # only the root directory
        self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(foreign.stat().st_mode), 0o600)

    def test_other_group_rebuilt_and_leftovers_stay_local(self):
        tree, _ = gitops.ensure_worktree(self.repo, self.wt_root, 'dd-7', 'main', group=os.getegid())
        (tree / 'leftover.txt').write_text('unfinished work')
        original = Path.stat
        def other_group(path, *args, **kwargs):
            st = original(path, *args, **kwargs)
            if path == tree:
                values = list(st); values[5] = os.getegid() + 1
                return os.stat_result(values)
            return st
        self.add_issue()
        with mock.patch.object(Path, 'stat', other_group):
            self.cycle()
        row = self.run_rows()[0]
        self.assertEqual(row['status'], runs.DONE, row)
        leftover = 'dd-7-leftover-1'
        self.assertEqual(git('show', f'{leftover}:leftover.txt', cwd=self.repo), 'unfinished work')
        self.assertNotIn(leftover, git('branch', '--list', cwd=self.origin))
        # Also handed on: restored into the new folder, so the agent built on it.
        self.assertIn('leftover.txt', git('ls-tree', '--name-only', 'dd-7', cwd=self.origin))
        self.assertIn(leftover, board.comments[-1][2])
        self.assertIn('worktree_rebuilt', [e['type'] for e in runs.events(self.conn)])

    def test_clean_rebuild_keeps_saved_story_commits(self):
        tree, _ = gitops.ensure_worktree(self.repo, self.wt_root, 'dd-7', 'main', group=os.getegid())
        author = self.cfg['agents']['claude-code']['git_author']
        (tree / 'saved.txt').write_text('saved')
        gitops.commit_all(self.repo, tree, 'saved', author)
        self.assertIsNone(gitops.rebuild_for_group(self.repo, tree, 'dd-7', 'dd-7-leftover-1', author))
        tree, _ = gitops.ensure_worktree(self.repo, self.wt_root, 'dd-7', 'main', group=os.getegid())
        self.assertEqual((tree / 'saved.txt').read_text(), 'saved')
        self.assertFalse(gitops.branch_exists(self.repo, 'dd-7-leftover-1'))



class SecondReview(Base):
    def test_filter_driver_refuses_commit(self):
        git("config", "filter.evil.clean", f"touch {self.marker}; cat", cwd=self.repo)
        self.set_mode("filter"); self.add_issue(); self.cycle()
        self.assertFalse(self.marker.exists(), "a filter driver ran as the orchestrator")
        [row] = self.run_rows()
        self.assertEqual(row["status"], runs.FAILED)
        self.assertIn("filter.evil.clean", row["error"])
        self.assertEqual(board.issues["i1"]["state"], {"id": S_BLK})
        self.assertNotIn("dd-7", git("branch", "--list", cwd=self.origin))


    def test_filter_driver_refuses_checkout(self):
        git("config", "filter.evil.smudge", f"touch {self.marker}; cat", cwd=self.repo)
        with self.assertRaisesRegex(gitops.GitError, "filter.evil.smudge"):
            gitops.ensure_worktree(str(self.repo), str(self.wt_root), "dd-9", "main")
        self.assertFalse((self.wt_root / "dd-9").exists())


    def test_grant_follows_no_links_and_skips_special_files(self):
        outside = self.tmp / "outside.txt"; outside.write_text("x"); outside.chmod(0o600)
        tree = self.tmp / "tree"; (tree / "sub").mkdir(parents=True)
        (tree / "sub" / "f.txt").write_text("y"); (tree / "sub" / "f.txt").chmod(0o600)
        (tree / "link").symlink_to(outside)
        (tree / "dirlink").symlink_to(self.tmp)
        os.mkfifo(tree / "pipe"); (tree / "pipe").chmod(0o600)
        gitops.grant_group_write(tree)
        self.assertTrue(os.stat(tree / "sub" / "f.txt").st_mode & stat.S_IWGRP)
        self.assertTrue(os.stat(tree / "sub").st_mode & stat.S_IWGRP)
        self.assertEqual(stat.S_IMODE(os.stat(outside).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(tree / "pipe").st_mode), 0o600)


    def test_reused_worktree_is_not_walked(self):
        self.set_mode("fail"); self.add_issue(); self.cycle()
        app = self.wt_root / "dd-7" / "app.py"
        app.chmod(0o644)
        _, reused = gitops.ensure_worktree(str(self.repo), str(self.wt_root), "dd-7", "main")
        self.assertTrue(reused)
        self.assertEqual(stat.S_IMODE(os.stat(app).st_mode), 0o644)



class ThirdReview(Base):
    def test_conditional_filter_config_cannot_run_on_checkout(self):
        # A committed .gitattributes asks for the filter, and host config
        # defines it only for story branches or for worktrees - so a check of
        # the main checkout alone sees nothing.
        (self.repo / ".gitattributes").write_text("app.py filter=evil\n")
        git("add", ".gitattributes", cwd=self.repo)
        git("-c", "user.name=Seed", "-c", "user.email=s@x", "commit", "-m", "attrs", cwd=self.repo)
        git("push", "origin", "main", cwd=self.repo)
        extra = self.tmp / "extra.gitconfig"
        extra.write_text(f"[filter \"evil\"]\n\tsmudge = touch {self.marker}; cat\n")
        for n, condition in enumerate(["onbranch:dd-*", "gitdir:**/worktrees/**"]):
            with self.subTest(condition=condition):
                git("config", f"includeIf.{condition}.path", str(extra), cwd=self.repo)
                main_view = subprocess.run([GIT, "config", "--get-regexp", "^filter\\."], cwd=self.repo)
                self.assertEqual(main_view.returncode, 1)  # the main checkout sees no filter
                branch = f"dd-{20 + n}"
                with self.assertRaisesRegex(gitops.GitError, "filter.evil.smudge"):
                    gitops.ensure_worktree(str(self.repo), str(self.wt_root), branch, "main")
                self.assertFalse(self.marker.exists(), "a filter driver ran as the orchestrator")
                # Refused worktrees are removed, so a retry can't reuse an empty one.
                self.assertFalse((self.wt_root / branch).exists())
                self.assertIsNone(gitops.registered_worktree(str(self.repo), branch))
                git("config", "--unset", f"includeIf.{condition}.path", cwd=self.repo)


    def test_checkout_still_populates_the_worktree(self):
        target, reused = gitops.ensure_worktree(str(self.repo), str(self.wt_root), "dd-30", "main")
        self.assertFalse(reused)
        self.assertEqual((target / "app.py").read_text(), "print('hi')\n")
        self.assertFalse(gitops.has_changes(str(self.repo), target))
        self.assertTrue(os.stat(target / "app.py").st_mode & stat.S_IWGRP)



class FourthReview(Base):
    def test_interrupted_checkout_is_rebuilt_not_reused(self):
        # As if DD stopped between creating the worktree and filling it.
        git("worktree", "add", "--no-checkout", "-b", "dd-7", str(self.wt_root / "dd-7"), "origin/main", cwd=self.repo)
        self.assertFalse((self.wt_root / "dd-7" / "app.py").exists())
        self.add_issue(); self.cycle()
        [row] = self.run_rows()
        self.assertEqual(row["status"], runs.DONE, row)
        self.assertIn("app.py", git("ls-tree", "--name-only", "dd-7", cwd=self.origin))
        self.assertIn("VALUE = 1", git("show", "dd-7:feature.py", cwd=self.origin))


    def test_checkout_without_permissions_is_rebuilt(self):
        # As if DD stopped after filling the worktree but before
        # letting the agent's group write to it.
        wt = self.wt_root / "dd-41"
        git("worktree", "add", "--no-checkout", "-b", "dd-41", str(wt), "origin/main", cwd=self.repo)
        git("reset", "--hard", "--quiet", cwd=wt)
        for path in (wt, wt / "app.py"):
            path.chmod(stat.S_IMODE(os.stat(path).st_mode) & ~stat.S_IWGRP)
        target, _ = gitops.ensure_worktree(str(self.repo), str(self.wt_root), "dd-41", "main")
        self.assertTrue(os.stat(target / "app.py").st_mode & stat.S_IWGRP)
        self.assertTrue(os.stat(target).st_mode & stat.S_IWGRP)
        self.assertTrue((gitops.admin_dir(str(self.repo), target) / gitops.READY).exists())


    def test_filled_worktree_with_agent_work_is_kept(self):
        target, _ = gitops.ensure_worktree(str(self.repo), str(self.wt_root), "dd-40", "main")
        (target / "unfinished.py").write_text("half done\n")
        again, reused = gitops.ensure_worktree(str(self.repo), str(self.wt_root), "dd-40", "main")
        self.assertTrue(reused)
        self.assertEqual((again / "unfinished.py").read_text(), "half done\n")


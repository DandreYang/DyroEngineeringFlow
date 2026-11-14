"""--abort for unfinished journaled line merges.

Called by: unittest discovery (`python -m unittest tests.test_line_merge_abort`).
No src/ importer. User instruction: "Tests: new tests/test_line_merge_abort.py".
Sibling tests/test_line_merge_cross.py covers --recover, not --abort.
"""
from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from dyro import line_ops, workspace
from dyro.cli import main
from dyro.config import load
from dyro.errors import DyroError
from dyro.events import read_events_fail_closed
from dyro.process import Result
from dyro.workspace import (
    abort_line_merge,
    create_line,
    line_repository_path,
    merge_line_from,
    merge_line_into_base,
)

from .support import WorkspaceCase, publish_origin_branch, shell
from .test_line_merge_cross import _commit_text, _stdout


class LineMergeAbortTests(WorkspaceCase):
    def _enable_web(self, repo_id="web"):
        web = self.root / f"repositories/{repo_id}"
        web.mkdir(parents=True)
        shell("git", "init", "-b", "main", cwd=web)
        shell("git", "config", "user.name", "Test User", cwd=web)
        shell("git", "config", "user.email", "test@example.com", cwd=web)
        (web / "README.md").write_text("web\n", encoding="utf-8")
        shell("git", "add", "README.md", cwd=web)
        shell("git", "commit", "-m", "chore: initial", cwd=web)
        config_path = self.root / "dyro.toml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8")
            + f'\n[repositories.{repo_id}]\npath = "repositories/{repo_id}"\nmount = "clients/{repo_id}"\n',
            encoding="utf-8",
        )
        return load(self.root), web

    def _sibling_lines(self):
        config, _web = self._enable_web()
        publish_origin_branch(self.anchor, "feat/alpha")
        publish_origin_branch(self.root / "repositories/web", "feat/alpha")
        alpha = create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        beta = create_line(config, line_id="beta", branch="feat/beta", base="main")
        return load(self.root), alpha, beta

    def _crash_after_probe(self, action) -> None:
        probe = workspace._probe_line_merge_plans

        def leave_merge_head(plans):
            probe(plans)
            raise SystemExit("crash after probe")

        with patch.object(workspace, "_probe_line_merge_plans", side_effect=leave_merge_head):
            with self.assertRaises(SystemExit):
                action()

    def _leave_committed_unfinished(self, config, target_id: str, source_id: str):
        with patch.object(workspace, "finish_op", side_effect=SystemExit("unfinished")):
            with self.assertRaises(SystemExit):
                merge_line_from(config, target_id, source_id)
        op = line_ops.find_unfinished_op(config, target_id, kinds=("merge",))
        self.assertIsNotNone(op)
        return op

    def _git_dir(self, repo: Path) -> Path:
        return Path(_stdout("git", "rev-parse", "--absolute-git-dir", cwd=repo))

    def test_probe_merge_head_dry_run_then_abort(self) -> None:
        config, alpha, beta = self._sibling_lines()
        _commit_text(
            line_repository_path(config, beta, "api"),
            "conflict.txt",
            "from-beta\n",
            "feat: conflict",
        )
        target = line_repository_path(config, alpha, "api")
        original = _stdout("git", "rev-parse", "HEAD", cwd=target)
        self._crash_after_probe(lambda: merge_line_from(config, alpha.id, beta.id))
        self.assertTrue(workspace._has_merge_head(target))
        (target / "conflict.txt").write_text("hand-edited\n", encoding="utf-8")
        shell("git", "add", "conflict.txt", cwd=target)
        op = line_ops.find_unfinished_op(config, alpha.id, kinds=("merge",))
        self.assertIsNotNone(op)
        op_path = line_ops.op_path(config, op["op_id"])
        before = op_path.read_bytes()
        result = abort_line_merge(
            config, alpha.id, op_id=op["op_id"], kinds=("merge",), dry_run=True
        )
        self.assertTrue(result.dry_run)
        self.assertEqual(result.actions["api"], "merge_abort")
        self.assertIn("conflict.txt", result.discarded["api"])
        self.assertEqual(op_path.read_bytes(), before)
        self.assertEqual(_stdout("git", "rev-parse", "HEAD", cwd=target), original)
        self.assertTrue(workspace._has_merge_head(target))

        aborted = abort_line_merge(
            config, alpha.id, op_id=op["op_id"], kinds=("merge",)
        )
        self.assertEqual(aborted.outcomes["api"], "not_started")
        self.assertEqual(_stdout("git", "rev-parse", "HEAD", cwd=target), original)
        self.assertFalse(workspace._has_merge_head(target))
        self.assertEqual(_stdout("git", "status", "--porcelain", cwd=target), "")
        self.assertEqual(line_ops.load_op(config, op["op_id"])["phase"], "aborted")
        line_ops.require_no_unfinished_op(config, alpha.id, kinds=("merge",))
        merge_line_from(config, alpha.id, beta.id)

    def test_unstaged_edit_on_merged_path_refused_and_unrelated_edit_kept(self) -> None:
        config, alpha, beta = self._sibling_lines()
        _commit_text(
            line_repository_path(config, beta, "api"), "merged.txt", "from-beta\n", "feat: merged"
        )
        target = line_repository_path(config, alpha, "api")
        self._crash_after_probe(lambda: merge_line_from(config, alpha.id, beta.id))
        op = line_ops.find_unfinished_op(config, alpha.id, kinds=("merge",))
        (target / "merged.txt").write_text("hand\n", encoding="utf-8")
        before = line_ops.op_path(config, op["op_id"]).read_bytes()
        with self.assertRaisesRegex(DyroError, "merged.txt"):
            abort_line_merge(config, alpha.id, op_id=op["op_id"], kinds=("merge",))
        self.assertTrue(workspace._has_merge_head(target))
        self.assertEqual(line_ops.op_path(config, op["op_id"]).read_bytes(), before)
        shell("git", "add", "merged.txt", cwd=target)
        (target / "README.md").write_text("unrelated\n", encoding="utf-8")
        plan = abort_line_merge(
            config, alpha.id, op_id=op["op_id"], kinds=("merge",), dry_run=True
        )
        self.assertEqual(plan.discarded["api"], ("merged.txt",))
        abort_line_merge(config, alpha.id, op_id=op["op_id"], kinds=("merge",))
        self.assertEqual((target / "README.md").read_text(encoding="utf-8"), "unrelated\n")
        self.assertFalse((target / "merged.txt").exists())

    def test_committed_unpushed_reset_keep(self) -> None:
        config, alpha, beta = self._sibling_lines()
        _commit_text(
            line_repository_path(config, beta, "api"),
            "c.txt",
            "c\n",
            "feat: c",
        )
        target = line_repository_path(config, alpha, "api")
        original = _stdout("git", "rev-parse", "HEAD", cwd=target)
        op = self._leave_committed_unfinished(config, alpha.id, beta.id)
        new_tip = op["repositories"]["api"]["steps"]["merge"]["result"]["new_tip"]
        self.assertEqual(_stdout("git", "rev-parse", "HEAD", cwd=target), new_tip)
        result = abort_line_merge(config, alpha.id, op_id=op["op_id"], kinds=("merge",))
        self.assertEqual(result.actions["api"], "reset_keep")
        self.assertEqual(result.outcomes["api"], "rolled_back")
        self.assertEqual(_stdout("git", "rev-parse", "HEAD", cwd=target), original)
        self.assertEqual(
            line_ops.load_op(config, op["op_id"])["repositories"]["api"]["outcome"],
            "rolled_back",
        )
        self.assertEqual(line_ops.load_op(config, op["op_id"])["phase"], "aborted")

    def test_committed_origin_contains_refused(self) -> None:
        config, alpha, beta = self._sibling_lines()
        _commit_text(
            line_repository_path(config, beta, "api"),
            "o.txt",
            "o\n",
            "feat: o",
        )
        target = line_repository_path(config, alpha, "api")
        op = self._leave_committed_unfinished(config, alpha.id, beta.id)
        new_tip = op["repositories"]["api"]["steps"]["merge"]["result"]["new_tip"]
        shell(
            "git",
            "update-ref",
            f"refs/remotes/origin/{alpha.branch}",
            new_tip,
            cwd=target,
        )
        op_path = line_ops.op_path(config, op["op_id"])
        before = op_path.read_bytes()
        with self.assertRaisesRegex(DyroError, "origin 已包含"):
            abort_line_merge(config, alpha.id, op_id=op["op_id"], kinds=("merge",))
        self.assertEqual(_stdout("git", "rev-parse", "HEAD", cwd=target), new_tip)
        self.assertEqual(op_path.read_bytes(), before)
        self.assertEqual(line_ops.load_op(config, op["op_id"])["phase"], "pending")

    def test_committed_push_step_refused(self) -> None:
        config, alpha, beta = self._sibling_lines()
        _commit_text(
            line_repository_path(config, beta, "api"),
            "p.txt",
            "p\n",
            "feat: p",
        )
        target = line_repository_path(config, alpha, "api")
        op = self._leave_committed_unfinished(config, alpha.id, beta.id)
        new_tip = op["repositories"]["api"]["steps"]["merge"]["result"]["new_tip"]
        line_ops.record_repo_intent(
            config,
            op["op_id"],
            "api",
            "push",
            original_head=op["repositories"]["api"]["snapshot"]["original_head"],
            target_sha=new_tip,
            branch=alpha.branch,
            remote="origin",
        )
        op_path = line_ops.op_path(config, op["op_id"])
        before = op_path.read_bytes()
        with self.assertRaisesRegex(DyroError, "push 步骤"):
            abort_line_merge(config, alpha.id, op_id=op["op_id"], kinds=("merge",))
        self.assertEqual(_stdout("git", "rev-parse", "HEAD", cwd=target), new_tip)
        self.assertEqual(op_path.read_bytes(), before)

    def test_committed_dirty_worktree_refused(self) -> None:
        config, alpha, beta = self._sibling_lines()
        _commit_text(
            line_repository_path(config, beta, "api"),
            "d.txt",
            "d\n",
            "feat: d",
        )
        target = line_repository_path(config, alpha, "api")
        op = self._leave_committed_unfinished(config, alpha.id, beta.id)
        new_tip = op["repositories"]["api"]["steps"]["merge"]["result"]["new_tip"]
        (target / "dirty.txt").write_text("dirty\n", encoding="utf-8")
        with self.assertRaisesRegex(DyroError, "不干净"):
            abort_line_merge(config, alpha.id, op_id=op["op_id"], kinds=("merge",))
        self.assertEqual(_stdout("git", "rev-parse", "HEAD", cwd=target), new_tip)

    def test_head_drifted_refused(self) -> None:
        config, alpha, beta = self._sibling_lines()
        _commit_text(
            line_repository_path(config, beta, "api"),
            "h.txt",
            "h\n",
            "feat: h",
        )
        target = line_repository_path(config, alpha, "api")
        op = self._leave_committed_unfinished(config, alpha.id, beta.id)
        new_tip = op["repositories"]["api"]["steps"]["merge"]["result"]["new_tip"]
        drifted = _commit_text(target, "drift.txt", "drift\n", "feat: drift")
        with self.assertRaisesRegex(DyroError, "既不是 original_head"):
            abort_line_merge(config, alpha.id, op_id=op["op_id"], kinds=("merge",))
        self.assertEqual(_stdout("git", "rev-parse", "HEAD", cwd=target), drifted)
        self.assertNotEqual(drifted, new_tip)

    def test_no_merge_head_dirty_refused(self) -> None:
        config, alpha, beta = self._sibling_lines()
        target = line_repository_path(config, alpha, "api")
        original = _stdout("git", "rev-parse", "HEAD", cwd=target)
        op = line_ops.create_op(
            config,
            kind="merge",
            subject_line_ids=[alpha.id],
            manifest_snapshot={
                "target": alpha.id,
                "source": beta.id,
                "repositories": ["api"],
            },
            repos_ordered=["api"],
            effective_options={
                "allow_partial": False,
                "integration": "from",
                "source": beta.id,
                "target": alpha.id,
            },
            repository_snapshots={
                "api": {
                    "path": str(target),
                    "storage_mode": "linked-worktree",
                    "original_head": original,
                    "source_head": _stdout(
                        "git",
                        "rev-parse",
                        "HEAD",
                        cwd=line_repository_path(config, beta, "api"),
                    ),
                }
            },
            event_facts={"op_id": "dirtyop01" + "0" * 24, "repo_count": 1, "partial": False},
            op_id="dirtyop01" + "0" * 24,
        )
        (target / "dirty.txt").write_text("dirty\n", encoding="utf-8")
        with self.assertRaisesRegex(DyroError, "不干净"):
            abort_line_merge(config, alpha.id, op_id=op["op_id"], kinds=("merge",))
        self.assertEqual(_stdout("git", "rev-parse", "HEAD", cwd=target), original)
        self.assertFalse(workspace._has_merge_head(target))

    def test_cherry_pick_or_rebase_refused(self) -> None:
        config, alpha, beta = self._sibling_lines()
        target = line_repository_path(config, alpha, "api")
        original = _stdout("git", "rev-parse", "HEAD", cwd=target)
        source_head = _stdout(
            "git", "rev-parse", "HEAD", cwd=line_repository_path(config, beta, "api")
        )
        git_dir = self._git_dir(target)
        for name, setup in (
            ("cherry-pick", lambda: (git_dir / "CHERRY_PICK_HEAD").write_text(source_head + "\n")),
            ("rebase", lambda: (git_dir / "rebase-merge").mkdir()),
        ):
            with self.subTest(name=name):
                op_id = (name.replace("-", "") + "01").ljust(32, "0")
                op = line_ops.create_op(
                    config,
                    kind="merge",
                    subject_line_ids=[alpha.id],
                    manifest_snapshot={
                        "target": alpha.id,
                        "source": beta.id,
                        "repositories": ["api"],
                    },
                    repos_ordered=["api"],
                    effective_options={
                        "allow_partial": False,
                        "integration": "from",
                        "source": beta.id,
                        "target": alpha.id,
                    },
                    repository_snapshots={
                        "api": {
                            "path": str(target),
                            "storage_mode": "linked-worktree",
                            "original_head": original,
                            "source_head": source_head,
                        }
                    },
                    event_facts={"op_id": op_id, "repo_count": 1, "partial": False},
                    op_id=op_id,
                )
                setup()
                try:
                    with self.assertRaisesRegex(DyroError, "进行中的 Git 操作"):
                        abort_line_merge(
                            config, alpha.id, op_id=op["op_id"], kinds=("merge",)
                        )
                    self.assertEqual(
                        _stdout("git", "rev-parse", "HEAD", cwd=target), original
                    )
                finally:
                    (git_dir / "CHERRY_PICK_HEAD").unlink(missing_ok=True)
                    rebase = git_dir / "rebase-merge"
                    if rebase.exists():
                        rebase.rmdir()

    def test_terminal_op_refused(self) -> None:
        config, alpha, beta = self._sibling_lines()
        _commit_text(
            line_repository_path(config, beta, "api"),
            "t.txt",
            "t\n",
            "feat: t",
        )
        result = merge_line_from(config, alpha.id, beta.id)
        head = _stdout(
            "git", "rev-parse", "HEAD", cwd=line_repository_path(config, alpha, "api")
        )
        with self.assertRaisesRegex(DyroError, "不能 abort"):
            abort_line_merge(
                config, alpha.id, op_id=result.op_id, kinds=("merge",)
            )
        self.assertEqual(
            _stdout(
                "git", "rev-parse", "HEAD", cwd=line_repository_path(config, alpha, "api")
            ),
            head,
        )
        self.assertEqual(line_ops.load_op(config, result.op_id)["phase"], "completed")

    def test_merge_abort_failure_marks_unknown(self) -> None:
        config, alpha, beta = self._sibling_lines()
        _commit_text(
            line_repository_path(config, beta, "api"),
            "f.txt",
            "f\n",
            "feat: f",
        )
        target = line_repository_path(config, alpha, "api")
        original = _stdout("git", "rev-parse", "HEAD", cwd=target)
        self._crash_after_probe(lambda: merge_line_from(config, alpha.id, beta.id))
        op = line_ops.find_unfinished_op(config, alpha.id, kinds=("merge",))
        original_git = workspace.git

        def flaky(repo, *args, **kwargs):
            if args[:1] == ("merge",) and "--abort" in args:
                return Result(("git", "-C", str(repo), *args), 1, "simulated abort failure")
            return original_git(repo, *args, **kwargs)

        with patch.object(workspace, "git", side_effect=flaky):
            with self.assertRaisesRegex(DyroError, "手动："):
                abort_line_merge(config, alpha.id, op_id=op["op_id"], kinds=("merge",))
        loaded = line_ops.load_op(config, op["op_id"])
        self.assertEqual(loaded["phase"], "pending")
        self.assertEqual(loaded["repositories"]["api"]["outcome"], "unknown")
        self.assertTrue(workspace._has_merge_head(target))
        self.assertEqual(_stdout("git", "rev-parse", "HEAD", cwd=target), original)

    def test_two_repos_committed_and_merge_head(self) -> None:
        config, alpha, beta = self._sibling_lines()
        _commit_text(
            line_repository_path(config, beta, "api"),
            "a.txt",
            "a\n",
            "feat: a",
        )
        _commit_text(
            line_repository_path(config, beta, "web"),
            "w.txt",
            "w\n",
            "feat: w",
        )
        alpha_api = line_repository_path(config, alpha, "api")
        alpha_web = line_repository_path(config, alpha, "web")
        original_api = _stdout("git", "rev-parse", "HEAD", cwd=alpha_api)
        original_web = _stdout("git", "rev-parse", "HEAD", cwd=alpha_web)
        source_api = _stdout(
            "git", "rev-parse", "HEAD", cwd=line_repository_path(config, beta, "api")
        )
        source_web = _stdout(
            "git", "rev-parse", "HEAD", cwd=line_repository_path(config, beta, "web")
        )
        op = line_ops.create_op(
            config,
            kind="merge",
            subject_line_ids=[alpha.id],
            manifest_snapshot={
                "target": alpha.id,
                "source": beta.id,
                "repositories": ["api", "web"],
            },
            repos_ordered=["api", "web"],
            effective_options={
                "allow_partial": False,
                "integration": "from",
                "source": beta.id,
                "target": alpha.id,
            },
            repository_snapshots={
                "api": {
                    "path": str(alpha_api),
                    "storage_mode": "linked-worktree",
                    "original_head": original_api,
                    "source_head": source_api,
                },
                "web": {
                    "path": str(alpha_web),
                    "storage_mode": "linked-worktree",
                    "original_head": original_web,
                    "source_head": source_web,
                },
            },
            event_facts={"op_id": "tworepo01" + "0" * 24, "repo_count": 2, "partial": False},
            op_id="tworepo01" + "0" * 24,
        )
        line_ops.record_repo_intent(
            config,
            op["op_id"],
            "api",
            "merge",
            original_head=original_api,
            target_sha=source_api,
        )
        shell("git", "merge", "--no-ff", "-m", "merge api", source_api, cwd=alpha_api)
        new_tip = _stdout("git", "rev-parse", "HEAD", cwd=alpha_api)
        line_ops.record_repo_result(
            config, op["op_id"], "api", "merge", outcome="committed", new_tip=new_tip
        )
        line_ops.update_repo_phase(
            config, op["op_id"], "api", phase="completed", outcome="committed"
        )
        shell("git", "merge", "--no-ff", "--no-commit", source_web, cwd=alpha_web)
        result = abort_line_merge(config, alpha.id, op_id=op["op_id"], kinds=("merge",))
        self.assertEqual(result.actions["api"], "reset_keep")
        self.assertEqual(result.actions["web"], "merge_abort")
        self.assertEqual(result.outcomes["api"], "rolled_back")
        self.assertEqual(result.outcomes["web"], "not_started")
        self.assertEqual(_stdout("git", "rev-parse", "HEAD", cwd=alpha_api), original_api)
        self.assertEqual(_stdout("git", "rev-parse", "HEAD", cwd=alpha_web), original_web)
        self.assertFalse(workspace._has_merge_head(alpha_web))
        self.assertEqual(line_ops.load_op(config, op["op_id"])["phase"], "aborted")

    def test_two_repos_classify_before_mutate(self) -> None:
        config, alpha, beta = self._sibling_lines()
        _commit_text(
            line_repository_path(config, beta, "api"),
            "a.txt",
            "a\n",
            "feat: a",
        )
        _commit_text(
            line_repository_path(config, beta, "web"),
            "w.txt",
            "w\n",
            "feat: w",
        )
        alpha_api = line_repository_path(config, alpha, "api")
        alpha_web = line_repository_path(config, alpha, "web")
        original_api = _stdout("git", "rev-parse", "HEAD", cwd=alpha_api)
        original_web = _stdout("git", "rev-parse", "HEAD", cwd=alpha_web)
        source_api = _stdout(
            "git", "rev-parse", "HEAD", cwd=line_repository_path(config, beta, "api")
        )
        source_web = _stdout(
            "git", "rev-parse", "HEAD", cwd=line_repository_path(config, beta, "web")
        )
        op = line_ops.create_op(
            config,
            kind="merge",
            subject_line_ids=[alpha.id],
            manifest_snapshot={
                "target": alpha.id,
                "source": beta.id,
                "repositories": ["api", "web"],
            },
            repos_ordered=["api", "web"],
            effective_options={
                "allow_partial": False,
                "integration": "from",
                "source": beta.id,
                "target": alpha.id,
            },
            repository_snapshots={
                "api": {
                    "path": str(alpha_api),
                    "storage_mode": "linked-worktree",
                    "original_head": original_api,
                    "source_head": source_api,
                },
                "web": {
                    "path": str(alpha_web),
                    "storage_mode": "linked-worktree",
                    "original_head": original_web,
                    "source_head": source_web,
                },
            },
            event_facts={"op_id": "refuseb01" + "0" * 24, "repo_count": 2, "partial": False},
            op_id="refuseb01" + "0" * 24,
        )
        line_ops.record_repo_intent(
            config,
            op["op_id"],
            "api",
            "merge",
            original_head=original_api,
            target_sha=source_api,
        )
        shell("git", "merge", "--no-ff", "-m", "merge api", source_api, cwd=alpha_api)
        new_tip = _stdout("git", "rev-parse", "HEAD", cwd=alpha_api)
        line_ops.record_repo_result(
            config, op["op_id"], "api", "merge", outcome="committed", new_tip=new_tip
        )
        line_ops.update_repo_phase(
            config, op["op_id"], "api", phase="completed", outcome="committed"
        )
        _commit_text(alpha_web, "drift.txt", "drift\n", "feat: web drift")
        op_path = line_ops.op_path(config, op["op_id"])
        before = op_path.read_bytes()
        with self.assertRaisesRegex(DyroError, "web:"):
            abort_line_merge(config, alpha.id, op_id=op["op_id"], kinds=("merge",))
        self.assertEqual(_stdout("git", "rev-parse", "HEAD", cwd=alpha_api), new_tip)
        self.assertEqual(op_path.read_bytes(), before)

    def test_merge_into_base_merge_head_and_clean_original(self) -> None:
        config = load(self.root)
        publish_origin_branch(self.anchor, "feat/ship")
        line = create_line(config, line_id="ship", branch="feat/ship", base="main")
        _commit_text(
            line_repository_path(config, line, "api"),
            "ship.txt",
            "ship\n",
            "feat: ship",
        )
        original = _stdout("git", "rev-parse", "HEAD", cwd=self.anchor)
        self._crash_after_probe(lambda: merge_line_into_base(config, line.id))
        self.assertTrue(workspace._has_merge_head(self.anchor))
        op = line_ops.find_unfinished_op(config, line.id, kinds=("merge_into_base",))
        aborted = abort_line_merge(
            config, line.id, op_id=op["op_id"], kinds=("merge_into_base",)
        )
        self.assertEqual(aborted.actions["api"], "merge_abort")
        self.assertEqual(aborted.outcomes["api"], "not_started")
        self.assertEqual(_stdout("git", "rev-parse", "HEAD", cwd=self.anchor), original)
        self.assertFalse(workspace._has_merge_head(self.anchor))
        self.assertEqual(line_ops.load_op(config, op["op_id"])["phase"], "aborted")

        other = create_line(config, line_id="dock", branch="feat/dock", base="main")
        _commit_text(
            line_repository_path(config, other, "api"),
            "dock.txt",
            "dock\n",
            "feat: dock",
        )
        with patch.object(
            workspace, "_probe_line_merge_plans", side_effect=SystemExit("before probe")
        ):
            with self.assertRaises(SystemExit):
                merge_line_into_base(config, other.id)
        clean_op = line_ops.find_unfinished_op(
            config, other.id, kinds=("merge_into_base",)
        )
        clean_head = _stdout("git", "rev-parse", "HEAD", cwd=self.anchor)
        result = abort_line_merge(
            config, other.id, op_id=clean_op["op_id"], kinds=("merge_into_base",)
        )
        self.assertEqual(result.actions["api"], "none")
        self.assertEqual(result.outcomes["api"], "not_started")
        self.assertEqual(_stdout("git", "rev-parse", "HEAD", cwd=self.anchor), clean_head)
        self.assertEqual(line_ops.load_op(config, clean_op["op_id"])["phase"], "aborted")

    def test_cli_abort_flags_and_yes(self) -> None:
        config, alpha, beta = self._sibling_lines()
        cases = (
            (["line", "merge", alpha.id, "--abort", "--recover", "--yes"], "--recover"),
            (["line", "merge", alpha.id, "--abort", "--from", beta.id, "--yes"], "--from"),
            (["line", "merge", alpha.id, "--abort", "--into", "alpha", "--yes"], "--into"),
            (["line", "merge", alpha.id, "--abort", "--push", "--yes"], "--push"),
            (["line", "merge", alpha.id, "--abort"], "放弃未完成的合并"),
            (
                ["line", "merge-into-base", beta.id, "--abort", "--recover", "--yes"],
                "--recover",
            ),
            (
                ["line", "merge-into-base", beta.id, "--abort", "--push", "--yes"],
                "--push",
            ),
        )
        for argv, needle in cases:
            with self.subTest(argv=argv):
                err = io.StringIO()
                with redirect_stderr(err):
                    with self.assertRaises(SystemExit) as caught:
                        main(["--root", str(self.root), *argv])
                self.assertEqual(caught.exception.code, 2)
                self.assertIn(needle, err.getvalue())

        _commit_text(
            line_repository_path(config, beta, "api"),
            "cli.txt",
            "cli\n",
            "feat: cli",
        )
        self._crash_after_probe(lambda: merge_line_from(config, alpha.id, beta.id))
        op = line_ops.find_unfinished_op(config, alpha.id, kinds=("merge",))
        op_path = line_ops.op_path(config, op["op_id"])
        before_op = op_path.read_bytes()
        before = {path: path.read_bytes() for path in (self.root / ".dyro").rglob("*") if path.is_file()}
        target = line_repository_path(config, alpha, "api")
        head = _stdout("git", "rev-parse", "HEAD", cwd=target)
        out = io.StringIO()
        with redirect_stdout(out):
            main(
                [
                    "--root",
                    str(self.root),
                    "line",
                    "merge",
                    alpha.id,
                    "--abort",
                    op["op_id"],
                    "--dry-run",
                ]
            )
        after = {path: path.read_bytes() for path in (self.root / ".dyro").rglob("*") if path.is_file()}
        self.assertEqual(after, before)
        self.assertEqual(_stdout("git", "rev-parse", "HEAD", cwd=target), head)
        self.assertTrue(workspace._has_merge_head(target))
        self.assertIn("merge_abort", out.getvalue())
        self.assertEqual(op_path.read_bytes(), before_op)


    # A crash inside --abort must leave --recover unable to invent a success.
    def _reset_keep_hook(self, hook):
        original_git = workspace.git

        def patched(repo, *args, **kwargs):
            if args[:2] == ("reset", "--keep"):
                outcome = hook(Path(repo))
                if outcome is not None:
                    return outcome
            return original_git(repo, *args, **kwargs)

        return patch.object(workspace, "git", side_effect=patched)

    def test_failed_reset_keep_then_recover_is_not_committed(self) -> None:
        config, alpha, beta = self._sibling_lines()
        _commit_text(line_repository_path(config, beta, "api"), "k.txt", "k\n", "feat: k")
        target = line_repository_path(config, alpha, "api")
        original = _stdout("git", "rev-parse", "HEAD", cwd=target)
        op = self._leave_committed_unfinished(config, alpha.id, beta.id)
        fail = lambda repo: Result(("git",), 1, "simulated reset failure")  # noqa: E731
        with self._reset_keep_hook(fail):
            with self.assertRaisesRegex(DyroError, "手动："):
                abort_line_merge(config, alpha.id, op_id=op["op_id"], kinds=("merge",))
        ledger = config.ledger_file.read_bytes()
        with redirect_stdout(io.StringIO()):
            recovered = workspace.recover_line_merge(config, alpha.id, op_id=op["op_id"])
        self.assertEqual(recovered.outcomes["api"], "unknown")
        self.assertEqual(line_ops.load_op(config, op["op_id"])["phase"], "pending")
        self.assertEqual(config.ledger_file.read_bytes(), ledger)
        abort_line_merge(config, alpha.id, op_id=op["op_id"], kinds=("merge",))
        self.assertEqual(_stdout("git", "rev-parse", "HEAD", cwd=target), original)
        self.assertEqual(line_ops.load_op(config, op["op_id"])["phase"], "aborted")

    def test_crash_between_repos_keeps_op_open_for_recover(self) -> None:
        config, alpha, beta = self._sibling_lines()
        _commit_text(line_repository_path(config, beta, "api"), "a.txt", "a\n", "feat: a")
        _commit_text(line_repository_path(config, beta, "web"), "w.txt", "w\n", "feat: w")
        alpha_web = line_repository_path(config, alpha, "web")
        op = self._leave_committed_unfinished(config, alpha.id, beta.id)

        def crash_on_web(repo):
            if repo.resolve() == alpha_web.resolve():
                raise SystemExit("power loss")
            return None

        with self._reset_keep_hook(crash_on_web):
            with self.assertRaises(SystemExit):
                abort_line_merge(config, alpha.id, op_id=op["op_id"], kinds=("merge",))
        with redirect_stdout(io.StringIO()):
            recovered = workspace.recover_line_merge(config, alpha.id, op_id=op["op_id"])
        self.assertEqual(recovered.outcomes["api"], "rolled_back")
        self.assertEqual(recovered.outcomes["web"], "unknown")
        self.assertEqual(line_ops.load_op(config, op["op_id"])["phase"], "pending")
        abort_line_merge(config, alpha.id, op_id=op["op_id"], kinds=("merge",))
        self.assertEqual(line_ops.load_op(config, op["op_id"])["phase"], "aborted")

    def test_already_included_without_ancestry_refused(self) -> None:
        config, alpha, beta = self._sibling_lines()
        op = self._leave_committed_unfinished(config, alpha.id, beta.id)
        self.assertEqual(op["repositories"]["api"]["outcome"], "already_included")
        stray = _commit_text(
            line_repository_path(config, beta, "api"), "s.txt", "s\n", "feat: s"
        )
        path = line_ops.op_path(config, op["op_id"])
        data = json.loads(path.read_text(encoding="utf-8"))
        data["repositories"]["api"]["snapshot"]["source_head"] = stray
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        before = path.read_bytes()
        with self.assertRaisesRegex(DyroError, "already_included 核验失败"):
            abort_line_merge(config, alpha.id, op_id=op["op_id"], kinds=("merge",))
        self.assertEqual(path.read_bytes(), before)

    def test_into_merge_is_aborted_through_parent(self) -> None:
        config = load(self.root)
        publish_origin_branch(self.anchor, "feat/alpha")
        parent = create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        child = workspace.spawn_line(config, parent.id, "child")
        _commit_text(line_repository_path(config, child, "api"), "child.txt", "child", "child")
        target = line_repository_path(config, parent, "api")
        original = _stdout("git", "rev-parse", "HEAD", cwd=target)
        self._crash_after_probe(lambda: workspace.merge_line(config, child.id, parent.id))
        op = line_ops.find_unfinished_op(config, parent.id)
        with self.assertRaisesRegex(DyroError, "没有未完成的 merge 操作"):
            abort_line_merge(config, child.id, kinds=("merge",))
        abort_line_merge(config, parent.id, op_id=op["op_id"], kinds=("merge",))
        self.assertEqual(_stdout("git", "rev-parse", "HEAD", cwd=target), original)
        self.assertFalse(workspace._has_merge_head(target))
        workspace.merge_line(config, child.id, parent.id)

    def test_aborted_already_included_op_never_recovered_as_success(self) -> None:
        config, alpha, beta = self._sibling_lines()
        op = self._leave_committed_unfinished(config, alpha.id, beta.id)
        self.assertEqual(
            {repo["outcome"] for repo in op["repositories"].values()}, {"already_included"}
        )
        before = line_ops.op_path(config, op["op_id"]).read_bytes()
        with self.assertRaisesRegex(DyroError, "--recover 收口"):
            abort_line_merge(config, alpha.id, op_id=op["op_id"], kinds=("merge",))
        self.assertEqual(line_ops.op_path(config, op["op_id"]).read_bytes(), before)
        # Defensive: an op already closed as aborted never gains a success record.
        line_ops.finish_op(config, op["op_id"], phase="aborted")
        ledger = config.ledger_file.read_bytes()
        events = len(read_events_fail_closed(config))
        with redirect_stdout(io.StringIO()):
            workspace.recover_line_merge(config, alpha.id, op_id=op["op_id"])
        self.assertEqual(config.ledger_file.read_bytes(), ledger)
        self.assertEqual(len(read_events_fail_closed(config)), events)
        self.assertEqual(line_ops.load_op(config, op["op_id"])["phase"], "aborted")

    def test_untracked_file_on_merge_deleted_path_refused(self) -> None:
        config, alpha, beta = self._sibling_lines()
        alpha_api = line_repository_path(config, alpha, "api")
        beta_api = line_repository_path(config, beta, "api")
        tracked = _stdout("git", "ls-files", cwd=beta_api).splitlines()[0]
        shell("git", "rm", "-q", tracked, cwd=beta_api)
        shell("git", "commit", "-qm", "chore: drop", cwd=beta_api)
        self._crash_after_probe(lambda: merge_line_from(config, alpha.id, beta.id))
        op = line_ops.find_unfinished_op(config, alpha.id, kinds=("merge",))
        (alpha_api / tracked).write_text("mine\n", encoding="utf-8")
        before = line_ops.op_path(config, op["op_id"]).read_bytes()
        with self.assertRaisesRegex(DyroError, "未跟踪的同名文件"):
            abort_line_merge(config, alpha.id, op_id=op["op_id"], kinds=("merge",))
        self.assertEqual(line_ops.op_path(config, op["op_id"]).read_bytes(), before)
        self.assertEqual((alpha_api / tracked).read_text(encoding="utf-8"), "mine\n")

    def test_merge_head_with_committed_result_refused(self) -> None:
        config, alpha, beta = self._sibling_lines()
        _commit_text(line_repository_path(config, beta, "api"), "h.txt", "h\n", "feat: h")
        target = line_repository_path(config, alpha, "api")
        original = _stdout("git", "rev-parse", "HEAD", cwd=target)
        self._crash_after_probe(lambda: merge_line_from(config, alpha.id, beta.id))
        op = line_ops.find_unfinished_op(config, alpha.id, kinds=("merge",))
        if "merge" not in op["repositories"]["api"]["steps"]:
            line_ops.record_repo_intent(
                config, op["op_id"], "api", "merge", original_head=original,
                target_sha=op["repositories"]["api"]["snapshot"]["source_head"],
            )
        line_ops.record_repo_result(
            config, op["op_id"], "api", "merge", outcome="committed", new_tip=original
        )
        before = line_ops.op_path(config, op["op_id"]).read_bytes()
        with self.assertRaisesRegex(DyroError, "已记 committed 但仍有 MERGE_HEAD"):
            abort_line_merge(config, alpha.id, op_id=op["op_id"], kinds=("merge",))
        self.assertEqual(line_ops.op_path(config, op["op_id"]).read_bytes(), before)
        self.assertTrue(workspace._has_merge_head(target))

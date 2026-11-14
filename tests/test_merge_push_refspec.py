"""Merge --push paths pin the branch tip and push that SHA with an explicit refspec."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from dyro import line_ops, workspace
from dyro.config import load
from dyro.errors import DyroError
from dyro.tasks import answer_task, load_task, merge_task, review_task, run_task, task_template
from dyro.workspace import create_line, get_line, line_repository_path

from .support import WorkspaceCase, executor_writes_receipt, publish_origin_branch, shell
from .test_line_merge_cross import _commit_text, _stdout
from .test_tasks import TaskTests


def _track_main(path: Path, branch: str) -> None:
    shell("git", "config", "push.default", "upstream", cwd=path)
    shell("git", "config", f"branch.{branch}.remote", "origin", cwd=path)
    shell("git", "config", f"branch.{branch}.merge", "refs/heads/main", cwd=path)


def _bare_sha(bare: Path, branch: str) -> str:
    return _stdout("git", "rev-parse", f"refs/heads/{branch}", cwd=bare)


def _allow_push(config):
    return replace(config, policy=replace(config.policy, allow_push=True))


class MergePushRefspecTests(WorkspaceCase):
    def _published_parent_child(self):
        bare = publish_origin_branch(self.anchor, "main")
        publish_origin_branch(self.anchor, "feat/alpha")
        config = load(self.root)
        parent = create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        child = workspace.spawn_line(config, parent.id, "child")
        return _allow_push(config), parent, child, bare

    def test_line_merge_into_push_pins_tip_despite_upstream(self) -> None:
        config, parent, child, bare = self._published_parent_child()
        _commit_text(line_repository_path(config, child, "api"), "child.txt", "child\n", "child")
        parent_repo = line_repository_path(config, parent, "api")
        _track_main(parent_repo, parent.branch)
        main_before = _bare_sha(bare, "main")

        workspace.merge_line(config, child.id, parent.id, push=True)

        tip = _stdout("git", "rev-parse", f"refs/heads/{parent.branch}", cwd=parent_repo)
        self.assertNotEqual(tip, main_before)
        self.assertEqual(_bare_sha(bare, "main"), main_before)
        self.assertEqual(_bare_sha(bare, parent.branch), tip)
        self.assertEqual(get_line(config, parent.id).remote_anchors["api"], tip)
        op_path = next((self.root / ".dyro/ops").glob("*.json"))
        step = line_ops.load_op(config, op_path.stem)["repositories"]["api"]["steps"]["push"]
        self.assertEqual(step["intent"]["target_sha"], tip)
        self.assertEqual(step["result"]["new_tip"], tip)

    def test_line_sync_push_pins_child_tip_despite_upstream(self) -> None:
        config, parent, child, bare = self._published_parent_child()
        _commit_text(line_repository_path(config, parent, "api"), "parent.txt", "parent\n", "parent")
        child_repo = line_repository_path(config, child, "api")
        _track_main(child_repo, child.branch)
        main_before = _bare_sha(bare, "main")

        workspace.sync_line(config, child.id, push=True)

        tip = _stdout("git", "rev-parse", f"refs/heads/{child.branch}", cwd=child_repo)
        self.assertNotEqual(tip, main_before)
        self.assertEqual(_bare_sha(bare, "main"), main_before)
        self.assertEqual(_bare_sha(bare, child.branch), tip)
        self.assertEqual(get_line(config, child.id).remote_anchors["api"], tip)

    def test_task_merge_push_pins_line_tip_despite_upstream(self) -> None:
        bare = publish_origin_branch(self.anchor, "main")
        publish_origin_branch(self.anchor, "feat/alpha")
        main_before = _bare_sha(bare, "main")
        config = load(self.root)
        create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        task_path = config.task_specs_dir / "TASK-1"
        task_path.mkdir(parents=True)
        task_path.joinpath("task.toml").write_text(
            task_template("TASK-1", "verify task lifecycle", "alpha", "api", "services/api").replace(
                'agent = "codex"', 'agent = "noop"'
            ),
            encoding="utf-8",
        )
        task_path.joinpath("handoff.md").write_text("# handoff\n", encoding="utf-8")
        task = load_task(config, "TASK-1")
        with executor_writes_receipt(task.directory, "result: QUESTION\n"):
            self.assertEqual(run_task(config, task), "waiting_answer")
        task_repository = self.root / "worktrees/alpha/TASK-1/services/api"
        _commit_text(task_repository, "README.md", "task change\n", "feat: task change")
        task_path.joinpath("receipt.md").write_text("result: DONE\n", encoding="utf-8")
        self.assertEqual(answer_task(config, task, "continue"), "review")
        TaskTests._write_bound_review(task_path)
        self.assertEqual(review_task(config, task), "done")
        config = _allow_push(config)
        line_repository = self.root / "versions/alpha/services/api"
        _track_main(line_repository, "feat/alpha")

        merge_task(config, task, push=True)

        tip = _stdout("git", "rev-parse", "refs/heads/feat/alpha", cwd=line_repository)
        self.assertNotEqual(tip, main_before)
        self.assertEqual(_bare_sha(bare, "main"), main_before)
        self.assertEqual(_bare_sha(bare, "feat/alpha"), tip)
        self.assertEqual(get_line(config, "alpha").remote_anchors["api"], tip)

    def test_journaled_push_keeps_pinned_tip_when_branch_moves(self) -> None:
        config, parent, child, bare = self._published_parent_child()
        _commit_text(line_repository_path(config, child, "api"), "child.txt", "child\n", "child")
        parent_repo = line_repository_path(config, parent, "api")
        main_before = _bare_sha(bare, "main")
        original_git = workspace.git
        seen: dict[str, str] = {}

        def race_before_push(repo, *args, **kwargs):
            if args[:1] == ("push",) and "--dry-run" not in args:
                pinned = _stdout(
                    "git", "rev-parse", f"refs/heads/{parent.branch}", cwd=Path(repo)
                )
                seen["tip"] = pinned
                seen["refspec"] = args[2]
                _commit_text(Path(repo), "race.txt", "race\n", "race")
            return original_git(repo, *args, **kwargs)

        # The injected commit moves HEAD after the merge snapshot. The existing
        # ledger check compares HEAD with that snapshot and raises. The push and
        # the anchor are already recorded from the pinned tip.
        with patch.object(workspace, "git", side_effect=race_before_push):
            with self.assertRaisesRegex(DyroError, "committed 记录与 HEAD 不一致"):
                workspace.merge_line(config, child.id, parent.id, push=True)

        tip = seen["tip"]
        self.assertEqual(seen["refspec"], f"{tip}:refs/heads/{parent.branch}")
        self.assertEqual(_bare_sha(bare, "main"), main_before)
        self.assertEqual(_bare_sha(bare, parent.branch), tip)
        self.assertNotEqual(
            _stdout("git", "rev-parse", "HEAD", cwd=parent_repo),
            tip,
        )
        self.assertEqual(get_line(config, parent.id).remote_anchors["api"], tip)
        op_path = next((self.root / ".dyro/ops").glob("*.json"))
        step = line_ops.load_op(config, op_path.stem)["repositories"]["api"]["steps"]["push"]
        self.assertEqual(step["intent"]["target_sha"], tip)
        self.assertEqual(step["result"]["new_tip"], tip)

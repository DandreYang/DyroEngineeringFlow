from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from io import StringIO
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from dyro import events, families, line_ops, line_retire, tasks, workspace
from dyro.cli import build_parser, cmd_line_inbox, cmd_line_retire
from dyro.config import load
from dyro.errors import DyroError
from dyro.process import git, require_ok
from dyro.read_limits import ObservationLimits, ReadBudget
from dyro.state import exclusive_lock

from .support import WorkspaceCase, shell


class LineRetireTests(WorkspaceCase):
    def setUp(self) -> None:
        super().setUp()
        self.config = load(self.root)
        self.line = workspace.create_line(self.config, line_id="alpha", branch="feat/alpha", base="main")
        self.path = workspace.line_repository_path(self.config, self.line, "api")
        self.sha = self._head(self.path)
        self.saved_ref = "refs/dyro/retired/alpha/api"

    def _head(self, path: Path, ref: str = "HEAD") -> str:
        return require_ok(git(path, "rev-parse", "--verify", ref), "read head").stdout.strip()

    def _commit(self, path: Path) -> str:
        (path / "new.txt").write_text("new content\n")
        shell("git", "add", "new.txt", cwd=path)
        shell("git", "commit", "-m", "new commit", cwd=path)
        return self._head(path)

    def _open(self) -> dict:
        op = line_ops.find_unfinished_op(self.config, "alpha")
        self.assertIsNotNone(op)
        return op

    def _crash(self, action: str, **options) -> dict:
        original = line_ops.record_repo_result

        def crash(config, op_id, repo_id, step, **kwargs):
            if step == action:
                raise SystemExit("power loss before result")
            return original(config, op_id, repo_id, step, **kwargs)

        with patch.object(line_ops, "record_repo_result", side_effect=crash):
            with self.assertRaises(SystemExit):
                line_retire.retire_line(self.config, "alpha", **options)
        return self._open()

    def _recover(self, **options) -> dict:
        return line_retire.retire_line(self.config, "alpha", recover="", **options)

    def test_retires_in_order_and_preserves_lock_and_head(self) -> None:
        append = events.append_event_locked

        def checked_append(config, **kwargs):
            self.assertTrue(line_retire.retired_path(config, "alpha").exists())
            self.assertFalse(workspace._state_path(config, "line", "alpha").exists())
            self.assertFalse(self.path.exists())
            self.assertEqual(git(self.anchor, "show-ref", "--verify", "refs/heads/feat/alpha").code, 128)
            return append(config, **kwargs)

        with patch.object(events, "append_event_locked", side_effect=checked_append):
            op = line_retire.retire_line(self.config, "alpha")
        self.assertEqual(op["phase"], "completed")
        self.assertEqual(list(op["repositories"]["api"]["steps"]), ["r1", "r2", "r3", "r4", "r5"])
        self.assertEqual(self._head(self.anchor, self.saved_ref), self.sha)
        self.assertTrue(workspace.merge_lock_path(self.config, "alpha").exists())
        self.assertFalse(any("merge.lock" in row for row in workspace.doctor(self.config)))
        rows, _ = events.read_events(self.config)
        self.assertEqual(rows[-1]["facts"], {"op_id": op["op_id"], "repo_count": 1})
        for create in (lambda: workspace.create_line(self.config, line_id="alpha", branch="feat/alpha", base="main"),
                       lambda: workspace.spawn_line(self.config, "alpha", "child")):
            with self.assertRaises(DyroError):
                create()

    def test_dry_run_only_observes_and_validates(self) -> None:
        before = git(self.anchor, "show-ref").stdout
        manifest = workspace._state_path(self.config, "line", "alpha").read_bytes()
        with patch.object(line_retire, "git", side_effect=AssertionError("Git mutation")):
            plan = line_retire.retire_line(self.config, "alpha", dry_run=True)
        self.assertTrue(plan["dry_run"])
        self.assertEqual(git(self.anchor, "show-ref").stdout, before)
        self.assertEqual(workspace._state_path(self.config, "line", "alpha").read_bytes(), manifest)
        self.assertFalse((self.root / ".dyro/ops").exists())
        self.assertTrue(self.path.exists())
        self.assertFalse(line_retire.retired_path(self.config, "alpha").exists())

    def test_dirty_detached_and_git_operation_refused_even_with_force(self) -> None:
        (self.path / "dirty.txt").write_text("dirty")
        with self.assertRaisesRegex(DyroError, "不干净"):
            line_retire.retire_line(self.config, "alpha", force=True)
        (self.path / "dirty.txt").unlink()
        shell("git", "switch", "--detach", cwd=self.path)
        with self.assertRaisesRegex(DyroError, "DETACHED"):
            line_retire.retire_line(self.config, "alpha", force=True, missing_ok=True)
        shell("git", "switch", "feat/alpha", cwd=self.path)
        git_dir = Path(git(self.path, "rev-parse", "--absolute-git-dir").stdout.strip())
        (git_dir / "CHERRY_PICK_HEAD").write_text(self.sha)
        with self.assertRaisesRegex(DyroError, "CHERRY_PICK_HEAD"):
            line_retire.retire_line(self.config, "alpha", force=True)

    def test_missing_branch_requires_missing_path_and_permission(self) -> None:
        shell("git", "update-ref", "-d", "refs/heads/feat/alpha", cwd=self.anchor)
        with self.assertRaisesRegex(DyroError, "分支不存在"):
            line_retire.retire_line(self.config, "alpha", missing_ok=True)
        # Repair the branch, then remove the worktree using Git, without force.
        shell("git", "update-ref", "refs/heads/feat/alpha", self.sha, cwd=self.anchor)
        shell("git", "worktree", "remove", str(self.path), cwd=self.anchor)
        shell("git", "update-ref", "-d", "refs/heads/feat/alpha", cwd=self.anchor)
        op = line_retire.retire_line(self.config, "alpha", missing_ok=True)
        steps = op["repositories"]["api"]["steps"]
        self.assertEqual(steps["r1"]["result"]["outcome"], "skipped_no_head")
        self.assertEqual(steps["r2"]["result"]["outcome"], "skipped_missing")

    def test_force_only_relaxes_integration_and_keeps_preservation_ref(self) -> None:
        head = self._commit(self.path)
        with self.assertRaisesRegex(DyroError, "尚未被集成"):
            line_retire.retire_line(self.config, "alpha")
        op = line_retire.retire_line(self.config, "alpha", force=True)
        self.assertEqual(self._head(self.anchor, self.saved_ref), head)
        self.assertEqual(op["effective_options"]["integration_ref"], "main")

    def test_integration_ref_uses_parent_or_per_repo_base(self) -> None:
        head = self._commit(self.path)
        child = workspace.spawn_line(self.config, "alpha", "child")
        op = line_retire.retire_line(self.config, child.id)
        self.assertEqual(op["effective_options"]["integration_ref"], "feat/alpha")
        self.assertEqual(op["repositories"]["api"]["snapshot"]["integration_sha"], head)
        shell("git", "branch", "included", head, cwd=self.anchor)
        workspace._write_line(self.config, replace(self.line, repository_bases={"api": "included"}))
        op = line_retire.retire_line(self.config, "alpha")
        self.assertEqual(op["effective_options"]["integration_ref"], "included")

    def test_shared_branch_and_anchor_checked_branch_are_never_deleted(self) -> None:
        workspace._write_line(self.config, replace(self.line, id="other", kind="hotfix"))
        op = line_retire.retire_line(self.config, "alpha", force=True)
        self.assertEqual(op["repositories"]["api"]["steps"]["r3"]["result"]["outcome"], "skipped_unowned")
        self.assertEqual(self._head(self.anchor, "feat/alpha"), self.sha)

    def test_anchor_current_branch_is_not_deleted(self) -> None:
        shell("git", "worktree", "remove", str(self.path), cwd=self.anchor)
        shell("git", "switch", "feat/alpha", cwd=self.anchor)
        op = line_retire.retire_line(self.config, "alpha", missing_ok=True, force=True)
        self.assertEqual(op["repositories"]["api"]["steps"]["r3"]["result"]["outcome"], "skipped_unowned")

    def test_anchor_reference_unlinks_only_and_preserves_branch(self) -> None:
        reference = workspace.create_line(self.config, line_id="reference", branch="main", base="main",
                                          storage_modes={"api": "anchor-reference"})
        path = workspace.line_repository_path(self.config, reference, "api")
        (self.anchor / "dirty.txt").write_text("anchor changes")
        op = line_retire.retire_line(self.config, "reference", force=True)
        self.assertFalse(path.is_symlink())
        self.assertTrue((self.anchor / "dirty.txt").exists())
        self.assertEqual(self._head(self.anchor, "main"), self.sha)
        self.assertEqual(op["repositories"]["api"]["steps"]["r3"]["result"]["outcome"], "skipped_unowned")

    def test_moved_tip_after_removal_is_skipped(self) -> None:
        original = line_retire._delete_branch
        new_sha = require_ok(git(self.anchor, "commit-tree", "HEAD^{tree}", "-p", self.sha, "-m", "moved"), "commit tree").stdout.strip()

        def moved(*args):
            shell("git", "update-ref", "refs/heads/feat/alpha", new_sha, cwd=self.anchor)
            return original(*args)

        with patch.object(line_retire, "_delete_branch", side_effect=moved):
            op = line_retire.retire_line(self.config, "alpha", force=True)
        self.assertEqual(self._head(self.anchor, "feat/alpha"), new_sha)
        self.assertEqual(op["repositories"]["api"]["steps"]["r3"]["result"]["outcome"], "skipped_moved")

    def test_identity_preflight_and_recovery_recheck_children_and_tasks(self) -> None:
        self._crash("r1")
        child = workspace.spawn_line(self.config, "alpha", "child")
        with patch.object(line_retire, "git", side_effect=AssertionError("must not mutate")):
            with self.assertRaisesRegex(DyroError, "直接子线.*alpha_child"):
                self._recover()
        line_retire.retire_line(self.config, child.id)
        task_path = tasks.create_task(self.config, "T1", title="unfinished", line_id="alpha", repositories=["api"])
        with self.assertRaisesRegex(DyroError, "未完成任务.*T1"):
            self._recover()
        self.assertTrue(self.path.exists())
        (task_path / "status").write_text("done\n")
        self.assertEqual(self._recover()["phase"], "completed")
        self.assertEqual(tasks.load_task(self.config, "T1").line, "alpha")
        self.assertEqual(tasks.list_tasks(self.config)[0].id, "T1")
        task, current = tasks.load_task_observation_bounded(
            self.config, "T1", ReadBudget(ObservationLimits()), known_line_ids=frozenset(),
        )
        self.assertEqual((task.line, current), ("alpha", "done"))

    def test_damaged_child_blocks_parent_until_missing_ok_retirement(self) -> None:
        child = workspace.spawn_line(self.config, "alpha", "child")
        path = workspace.line_repository_path(self.config, child, "api")
        shell("git", "worktree", "remove", str(path), cwd=self.anchor)
        with self.assertRaisesRegex(DyroError, "直接子线"):
            line_retire.retire_line(self.config, "alpha", force=True)
        line_retire.retire_line(self.config, child.id, missing_ok=True)
        line_retire.retire_line(self.config, "alpha")

    def test_r1_and_r2_action_result_gaps_are_confirmed_without_replay(self) -> None:
        self._crash("r1", keep_branch=True)
        original = line_retire.git
        calls = []

        def no_save_replay(path, *args, **kwargs):
            calls.append(args)
            self.assertFalse(args[0] == "update-ref" and args[1] == self.saved_ref)
            return original(path, *args, **kwargs)

        with patch.object(line_retire, "git", side_effect=no_save_replay):
            op = self._recover()
        self.assertTrue(op["effective_options"]["keep_branch"])
        self.assertEqual(self._head(self.anchor, "feat/alpha"), self.sha)
        self.assertTrue(any(args[:2] == ("worktree", "remove") for args in calls))

    def test_r2_result_gap_confirms_absent_path(self) -> None:
        self._crash("r2")
        self.assertFalse(self.path.exists())
        original = line_retire.git

        def no_remove_replay(path, *args, **kwargs):
            self.assertNotEqual(args[:2], ("worktree", "remove"))
            return original(path, *args, **kwargs)

        with patch.object(line_retire, "git", side_effect=no_remove_replay):
            self.assertEqual(self._recover()["phase"], "completed")

    def test_r3_result_gap_confirms_absent_branch(self) -> None:
        self._crash("r3")
        self.assertEqual(self._recover()["phase"], "completed")

    def test_r4_result_gap_uses_tombstone_and_r5_deduplicates(self) -> None:
        self._crash("r4")
        self.assertFalse(workspace._state_path(self.config, "line", "alpha").exists())
        with patch.object(line_retire, "get_line", side_effect=AssertionError("active lookup")):
            self.assertEqual(self._recover()["phase"], "completed")
        op_id = json.loads(next((self.root / ".dyro/ops").glob("*.json")).read_text())["op_id"]
        self.assertEqual(line_retire.retire_line(self.config, "alpha", recover=op_id)["phase"], "completed")

    def test_r5_result_gap_deduplicates_event(self) -> None:
        self._crash("r5")
        self.assertEqual(self._recover()["phase"], "completed")
        rows, _ = events.read_events(self.config)
        self.assertEqual(sum(row["kind"] == "retire" for row in rows), 1)

    def test_r1_confirmed_ref_missing_or_conflicting_never_overwritten(self) -> None:
        with patch.object(line_retire, "_remove", side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                line_retire.retire_line(self.config, "alpha")
        shell("git", "update-ref", "-d", self.saved_ref, cwd=self.anchor)
        with self.assertRaisesRegex(DyroError, "保全 ref 丢失"):
            self._recover()
        new_sha = self._commit(self.anchor)
        shell("git", "update-ref", self.saved_ref, new_sha, cwd=self.anchor)
        with self.assertRaisesRegex(DyroError, "冲突"):
            self._recover()
        self.assertEqual(self._head(self.anchor, self.saved_ref), new_sha)
        self.assertTrue(self.path.exists())
        self.assertFalse(line_retire.retired_path(self.config, "alpha").exists())

    def test_r1_existing_ref_without_intent_is_unknown(self) -> None:
        shell("git", "update-ref", self.saved_ref, self.sha, cwd=self.anchor)
        with self.assertRaisesRegex(DyroError, "没有 intent"):
            line_retire.retire_line(self.config, "alpha")
        self.assertNotIn("r1", self._open()["repositories"]["api"]["steps"])

    def test_r1_pending_intent_uses_pinned_sha(self) -> None:
        with patch.object(line_retire, "git", side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                line_retire.retire_line(self.config, "alpha")
        shell("git", "switch", "--detach", cwd=self.path)
        new_sha = self._commit(self.path)
        with self.assertRaisesRegex(DyroError, "R2 漂移"):
            self._recover()
        self.assertEqual(self._head(self.anchor, self.saved_ref), self.sha)
        self.assertEqual(self._head(self.path), new_sha)

    def test_r1_pending_conflicting_ref_is_never_overwritten(self) -> None:
        self._crash("r1")
        new_sha = self._commit(self.anchor)
        shell("git", "update-ref", self.saved_ref, new_sha, cwd=self.anchor)
        with self.assertRaisesRegex(DyroError, "永不覆盖"):
            self._recover()
        self.assertEqual(self._head(self.anchor, self.saved_ref), new_sha)

    def test_r2_detached_new_clean_commit_blocks_and_can_be_reconciled(self) -> None:
        self._crash("r1")
        shell("git", "switch", "--detach", cwd=self.path)
        new_sha = self._commit(self.path)
        with self.assertRaisesRegex(DyroError, "R2 漂移.*HEAD=.*\n.*unknown") as failure:
            self._recover()
        self.assertIn("破坏性", str(failure.exception))
        self.assertEqual(self._head(self.path), new_sha)
        self.assertTrue(self.path.exists())
        self.assertIsNotNone(self._open())
        shell("git", "switch", "feat/alpha", cwd=self.path)
        self.assertEqual(self._recover()["phase"], "completed")

    def test_r2_missing_without_intent_obeys_frozen_missing_ok(self) -> None:
        self._crash("r1", missing_ok=True)
        shell("git", "worktree", "remove", str(self.path), cwd=self.anchor)
        op = self._recover()
        self.assertEqual(op["repositories"]["api"]["steps"]["r2"]["result"]["outcome"], "skipped_missing")

    def test_r2_missing_without_permission_blocks_and_option_conflicts(self) -> None:
        self._crash("r1", keep_branch=True)
        with self.assertRaisesRegex(DyroError, "恢复选项冲突.*keep_branch"):
            self._recover(keep_branch=False)
        with self.assertRaisesRegex(DyroError, "恢复选项冲突.*force"):
            self._recover(force=True)
        with self.assertRaisesRegex(DyroError, "恢复选项冲突.*integration_ref"):
            self._recover(integration_ref="other")
        shell("git", "worktree", "remove", str(self.path), cwd=self.anchor)
        with self.assertRaisesRegex(DyroError, "未获准"):
            self._recover()
        with self.assertRaisesRegex(DyroError, "恢复选项冲突.*missing_ok"):
            self._recover(missing_ok=True)

    def test_r2_completed_path_reappearing_is_unknown(self) -> None:
        self._crash("r3")
        self.path.mkdir(parents=True)
        with self.assertRaisesRegex(DyroError, "路径仍在"):
            self._recover()
        self.assertFalse(line_retire.retired_path(self.config, "alpha").exists())

    def test_r4_conflicting_tombstone_blocks_before_git(self) -> None:
        self._crash("r1")
        retired = line_retire.retired_path(self.config, "alpha")
        retired.parent.mkdir(parents=True)
        retired.write_text("conflicting manifest\n")
        with patch.object(line_retire, "git", side_effect=AssertionError("Git mutation")):
            with self.assertRaisesRegex(DyroError, "R4 unknown"):
                self._recover()
        self.assertEqual(retired.read_text(), "conflicting manifest\n")

    def test_all_repos_before_archive_and_unknown_keeps_manifest_active(self) -> None:
        config_path = self.root / "dyro.toml"
        config_path.write_text(config_path.read_text() + '\n[repositories.web]\npath = "repositories/web"\nmount = "web"\n')
        web = self.root / "repositories/web"
        shell("git", "clone", "--no-hardlinks", str(self.anchor), str(web), cwd=self.root)
        shell("git", "config", "user.name", "Test User", cwd=web)
        shell("git", "config", "user.email", "test@example.com", cwd=web)
        self.config = load(self.root)
        workspace._write_line(self.config, replace(self.line, repositories=("web", "api")))
        self.line = workspace.get_line(self.config, "alpha")
        web_path = workspace.line_repository_path(self.config, self.line, "web")
        shell("git", "worktree", "add", "-b", "feat/alpha", str(web_path), "main", cwd=web)
        original = line_retire._remove

        def failing(config, op, repo_id, snapshot, sha):
            if repo_id == "web":
                raise DyroError("web cannot remove")
            return original(config, op, repo_id, snapshot, sha)

        with patch.object(line_retire, "_remove", side_effect=failing):
            with self.assertRaisesRegex(DyroError, "api: done.*\nweb: unknown"):
                line_retire.retire_line(self.config, "alpha")
        op = self._open()
        self.assertEqual(op["repos_ordered"], ["api", "web"])
        self.assertFalse(self.path.exists())
        self.assertTrue(web_path.exists())
        self.assertTrue(workspace._state_path(self.config, "line", "alpha").exists())
        self.assertFalse(line_retire.retired_path(self.config, "alpha").exists())
        self.assertEqual(self._recover()["phase"], "completed")

    def test_unfinished_op_blocks_new_retire_before_preflight(self) -> None:
        op = self._crash("r1")
        with patch.object(line_retire, "get_line", side_effect=AssertionError("no preflight")):
            with self.assertRaisesRegex(DyroError, f"{op['op_id']}.*--recover"):
                line_retire.retire_line(self.config, "alpha")
        self.assertEqual(len(list((self.root / ".dyro/ops").glob("*.json"))), 1)

    def test_invalid_event_is_rejected_before_git_or_op(self) -> None:
        with patch.object(events, "validate_event_write", side_effect=events.EventLogError("EVENT_WRITE_INVALID")):
            with patch.object(line_retire, "git", side_effect=AssertionError("Git mutation")):
                with self.assertRaisesRegex(DyroError, "EVENT_WRITE_INVALID"):
                    line_retire.retire_line(self.config, "alpha")
        self.assertFalse((self.root / ".dyro/ops").exists())

    def test_event_failure_leaves_tombstone_gap_and_open_op(self) -> None:
        with patch.object(events, "append_event_locked", side_effect=events.EventLogError("EVENT_WRITE_FAILED")):
            with self.assertRaisesRegex(DyroError, "EVENT_WRITE_FAILED"):
                line_retire.retire_line(self.config, "alpha")
        self.assertTrue(events.events_gap_path(self.config).exists())
        self.assertTrue(line_retire.retired_path(self.config, "alpha").exists())
        self.assertIsNotNone(self._open())

    def test_locks_and_inner_preflight_precede_git(self) -> None:
        held = []

        @contextmanager
        def traced(path, **kwargs):
            with exclusive_lock(path, **kwargs):
                held.append(path)
                try:
                    yield
                finally:
                    held.pop()

        original = line_retire.git

        def checked_git(*args, **kwargs):
            self.assertEqual(held, [line_ops.line_identity_lock_path(self.config),
                                   workspace.merge_lock_path(self.config, "alpha"),
                                   line_ops.anchor_lock_path(self.config, self.anchor)])
            return original(*args, **kwargs)

        with patch.object(line_ops, "exclusive_lock", side_effect=traced), patch.object(
            line_retire, "exclusive_lock", side_effect=traced
        ), patch.object(line_retire, "git", side_effect=checked_git):
            line_retire.retire_line(self.config, "alpha")

    def test_locked_preflight_rechecks_changes(self) -> None:
        original = line_ops.anchor_locks

        @contextmanager
        def changed(*args, **kwargs):
            with original(*args, **kwargs):
                (self.path / "dirty.txt").write_text("became dirty")
                yield

        with patch.object(line_retire.line_ops, "anchor_locks", side_effect=changed):
            with self.assertRaisesRegex(DyroError, "不干净"):
                line_retire.retire_line(self.config, "alpha")
        self.assertFalse((self.root / ".dyro/ops").exists())

    def test_retired_family_history_and_cli_remain_read_only(self) -> None:
        child = workspace.spawn_line(self.config, "alpha", "child")
        families.post_channel_message(self.config, sender=child.id, kind="shipped", body="ready")
        line_retire.retire_line(self.config, child.id)
        line_retire.retire_line(self.config, "alpha")
        before = {path: path.read_bytes() for path in (self.root / ".dyro/families").rglob("*") if path.is_file()}
        inbox = families.list_inbox(self.config, viewer=child.id)
        self.assertEqual(inbox["family"], "alpha")
        self.assertEqual(inbox["messages"][0]["body"], "ready")
        args = build_parser().parse_args(["line", "inbox", "--line", child.id])
        with patch("dyro.cli._config", return_value=self.config), patch("sys.stdout", new_callable=StringIO):
            cmd_line_inbox(args)
        for sender, recipient, family in ((child.id, "", ""), ("operator", child.id, "alpha"),
                                          ("operator", "", "alpha")):
            with self.assertRaisesRegex(DyroError, "FAMILY_RETIRED_READ_ONLY"):
                families.post_channel_message(self.config, sender=sender, recipient=recipient,
                                              family=family, kind="decision", body="refuse")
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_cli_flags_and_dry_run_plan(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["line", "retire", "alpha", "--dry-run", "--keep-branch"])
        self.assertIsNone(args.force)
        self.assertIsNone(args.recover)
        self.assertEqual(parser.parse_args(["line", "retire", "alpha", "--recover"]).recover, "")
        self.assertEqual(parser.parse_args(["line", "retire", "alpha", "--recover", "op"]).recover, "op")
        self.assertTrue(parser.parse_args(["--dry-run", "line", "retire", "alpha"]).dry_run)
        with patch("dyro.cli._config", return_value=self.config), patch("sys.stdout", new_callable=StringIO) as out:
            cmd_line_retire(args)
        self.assertIn("DRY RUN", out.getvalue())
        self.assertIn("保留分支", out.getvalue())
        self.assertIn("集成目标 main", out.getvalue())


if __name__ == "__main__":
    unittest.main()

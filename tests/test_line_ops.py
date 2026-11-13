from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, redirect_stdout
from dataclasses import replace
from io import StringIO
import json
from unittest.mock import patch

from dyro import line_ops, tasks, workspace
from dyro.cli import main
from dyro.config import Repository, load
from dyro.errors import DyroError, ValidationError
from dyro.state import exclusive_lock

from .support import WorkspaceCase


class LineOpsTests(WorkspaceCase):
    def setUp(self) -> None:
        super().setUp()
        self.config = load(self.root)

    def _create_op(self, **overrides):
        arguments = dict(
            kind="retire", subject_line_ids=["alpha"],
            manifest_snapshot={"id": "alpha", "repositories": ["api", "web"]},
            repos_ordered=["web", "api"],
            effective_options={"keep_branch": True, "force": False, "integration_ref": "main"},
            repository_snapshots={"api": {"path": str(self.anchor), "storage_mode": "linked-worktree"}},
            event_facts={"repo_count": 2},
        )
        arguments.update(overrides)
        return line_ops.create_op(self.config, **arguments)

    def _finish_repos(self, op_id, outcome="completed"):
        for repo in ("api", "web"):
            line_ops.update_repo_phase(
                self.config, op_id, repo, phase="completed", outcome=outcome,
            )

    def test_anchor_key_normalizes_absolute_paths_and_symlinks(self) -> None:
        alias = self.root / "alias"
        alias.symlink_to(self.anchor, target_is_directory=True)
        key = line_ops.anchor_lock_path(self.config, self.anchor)
        self.assertEqual(key, line_ops.anchor_lock_path(self.config, alias))
        self.assertEqual(key, line_ops.anchor_lock_path(self.config, self.anchor / ".." / "api"))
        self.assertEqual(key.parent, self.config.root / ".dyro/locks/anchor")
        self.assertNotEqual(key, line_ops.anchor_lock_path(self.config, self.root / "other"))
        self.assertEqual(line_ops.line_identity_lock_path(self.config), self.config.root / ".dyro/locks/line-identity.lock")

    def test_many_anchors_lock_in_repo_order_and_release_in_reverse(self) -> None:
        config = replace(self.config, repositories={
            "z": Repository("z", "repositories/a", "a"),
            "a": Repository("a", "repositories/z", "z"),
        })
        events = []

        @contextmanager
        def traced(path, **kwargs):
            events.append(("enter", path))
            try:
                yield
            finally:
                events.append(("exit", path))

        a = line_ops.anchor_lock_path(config, self.root / "repositories/z")
        z = line_ops.anchor_lock_path(config, self.root / "repositories/a")
        with patch.object(line_ops, "exclusive_lock", side_effect=traced):
            with self.assertRaisesRegex(RuntimeError, "failure"):
                with line_ops.anchor_locks(config, ["z", "a", "z"]):
                    raise RuntimeError("failure")
        self.assertEqual(events, [("enter", a), ("enter", z), ("exit", z), ("exit", a)])

    def test_failed_second_lock_releases_first(self) -> None:
        config = replace(self.config, repositories={
            **self.config.repositories, "web": Repository("web", "repositories/web", "web"),
        })
        released = []

        @contextmanager
        def failing(path, **kwargs):
            if path == line_ops.anchor_lock_path(config, self.root / "repositories/web"):
                raise DyroError("blocked")
            try:
                yield
            finally:
                released.append(path)

        with patch.object(line_ops, "exclusive_lock", side_effect=failing):
            with self.assertRaisesRegex(DyroError, "blocked"):
                with line_ops.anchor_locks(config, ["web", "api"]):
                    self.fail("must not enter")
        self.assertEqual(released, [line_ops.anchor_lock_path(config, self.anchor)])

    def test_journal_roundtrip_and_frozen_snapshot(self) -> None:
        op = self._create_op(op_id="test-op")
        self.assertEqual(op["repos_ordered"], ["api", "web"])
        self.assertEqual(op["repositories"]["api"]["snapshot"]["path"], str(self.anchor))
        self.assertEqual(op["event_facts"], {"repo_count": 2})
        op["effective_options"]["keep_branch"] = False
        op["manifest_snapshot"]["repositories"].clear()
        frozen = line_ops.load_op(self.config, "test-op")
        self.assertTrue(frozen["effective_options"]["keep_branch"])
        self.assertEqual(frozen["manifest_snapshot"]["repositories"], ["api", "web"])
        self.assertEqual(json.loads(line_ops.op_path(self.config, "test-op").read_text()), frozen)
        with self.assertRaisesRegex(DyroError, "已存在"):
            self._create_op(op_id="test-op")

    def test_intent_result_gap_survives_reload_and_pins_sha(self) -> None:
        op_id = self._create_op()["op_id"]
        with self.assertRaisesRegex(DyroError, "先行意图"):
            line_ops.record_repo_result(self.config, op_id, "api", "save", outcome="ok")
        line_ops.record_repo_intent(
            self.config, op_id, "api", "save", original_head="old", target_sha="pinned", path="worktree",
        )
        step = line_ops.load_op(self.config, op_id)["repositories"]["api"]["steps"]["save"]
        self.assertEqual(step["intent"]["target_sha"], "pinned")
        self.assertNotIn("result", step)
        with self.assertRaisesRegex(DyroError, "冻结"):
            line_ops.record_repo_intent(self.config, op_id, "api", "save", original_head="old", target_sha="drifted")
        with self.assertRaisesRegex(DyroError, "未对账"):
            line_ops.record_repo_intent(self.config, op_id, "api", "remove", original_head="old", target_sha="pinned")
        self._finish_repos(op_id)
        with self.assertRaisesRegex(DyroError, "对账"):
            line_ops.finish_op(self.config, op_id, phase="completed")
        line_ops.record_repo_result(self.config, op_id, "api", "save", outcome="ok", new_tip="pinned")
        line_ops.finish_op(self.config, op_id, phase="completed")
        self.assertIsNone(line_ops.find_unfinished_op(self.config, "alpha"))

    def test_unknown_stays_unfinished_until_reconciled_then_can_abort(self) -> None:
        op_id = self._create_op(kind="merge")["op_id"]
        self._finish_repos(op_id, outcome="unknown")
        for phase in ("completed", "aborted"):
            with self.assertRaisesRegex(DyroError, "对账"):
                line_ops.finish_op(self.config, op_id, phase=phase)
        line_ops.record_repo_intent(self.config, op_id, "api", "commit", original_head="old", target_sha="new")
        line_ops.record_repo_result(self.config, op_id, "api", "commit", outcome="unknown", error="interrupted")
        self._finish_repos(op_id, outcome="not_started")
        with self.assertRaisesRegex(DyroError, "对账"):
            line_ops.finish_op(self.config, op_id, phase="aborted")
        line_ops.record_repo_result(self.config, op_id, "api", "commit", outcome="not_started", new_tip="old")
        line_ops.update_repo_phase(self.config, op_id, "api", phase="unknown", outcome="not_started")
        with self.assertRaisesRegex(DyroError, "对账"):
            line_ops.finish_op(self.config, op_id, phase="aborted")
        line_ops.update_repo_phase(self.config, op_id, "api", phase="completed", outcome="not_started")
        line_ops.finish_op(self.config, op_id, phase="aborted")
        line_ops.finish_op(self.config, op_id, phase="aborted")
        with self.assertRaisesRegex(DyroError, "已以 aborted"):
            line_ops.finish_op(self.config, op_id, phase="completed")
        with self.assertRaisesRegex(DyroError, "已结束"):
            line_ops.update_repo_phase(self.config, op_id, "api", phase="pending", outcome="pending")

    def test_unfinished_selection_and_block_include_subject_and_recovery(self) -> None:
        self.assertIsNone(line_ops.find_unfinished_op(self.config, "alpha"))
        first = self._create_op(op_id="first")
        second = self._create_op(kind="merge", subject_line_ids=["alpha", "beta"], op_id="second")
        self.assertEqual(line_ops.find_unfinished_op(self.config, "alpha")["op_id"], second["op_id"])
        self.assertEqual(line_ops.find_unfinished_op(self.config, "alpha", kinds=["retire"])["op_id"], first["op_id"])
        self.assertEqual(line_ops.find_unfinished_op(self.config, "beta")["op_id"], second["op_id"])
        with self.assertRaisesRegex(DyroError, "second.*dyro line merge beta --recover second"):
            line_ops.require_no_unfinished_op(self.config, "beta")
        self._finish_repos(second["op_id"])
        line_ops.finish_op(self.config, second["op_id"], phase="completed")
        line_ops.require_no_unfinished_op(self.config, "beta")
        self.assertEqual(line_ops.find_unfinished_op(self.config, "alpha")["op_id"], first["op_id"])

    def test_recovery_uses_frozen_options_and_rejects_conflicts(self) -> None:
        op = self._create_op()
        frozen = line_ops.require_compatible_options(op, {})
        self.assertTrue(frozen["keep_branch"])
        self.assertEqual(frozen["integration_ref"], "main")
        line_ops.require_compatible_options(op, {"force": False})
        for conflicting in ({"keep_branch": False}, {"integration_ref": "other"}, {"new_flag": True}):
            with self.assertRaisesRegex(DyroError, "恢复选项冲突.*journal 冻结值"):
                line_ops.require_compatible_options(op, conflicting)
        frozen["keep_branch"] = False
        self.assertTrue(op["effective_options"]["keep_branch"])

    def test_invalid_journal_fails_closed_and_ids_cannot_escape(self) -> None:
        with self.assertRaises(ValidationError):
            line_ops.load_op(self.config, "../escape")
        path = line_ops.op_path(self.config, "broken")
        path.parent.mkdir(parents=True)
        for content in ("{", "[]", '{"schema_version": 1}', "\ufffd"):
            path.write_text(content)
            with self.assertRaises(ValidationError):
                line_ops.require_no_unfinished_op(self.config, "alpha")

    def test_journal_update_uses_atomic_write_and_failure_preserves_previous(self) -> None:
        op_id = self._create_op()["op_id"]
        before = line_ops.op_path(self.config, op_id).read_bytes()
        with patch("dyro.state.os.replace", side_effect=OSError("replace failed")):
            with self.assertRaises(OSError):
                line_ops.update_repo_phase(self.config, op_id, "api", phase="r1", outcome="ok")
        self.assertEqual(line_ops.op_path(self.config, op_id).read_bytes(), before)
        self.assertEqual(list(line_ops.op_path(self.config, op_id).parent.glob(".*.json.*")), [])


class LineLockWiringTests(WorkspaceCase):
    def _parent_child(self):
        config = load(self.root)
        parent = workspace.create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        child = workspace.spawn_line(config, parent.id, "child")
        return config, parent, child

    @contextmanager
    def _traced_locks(self):
        held = []
        acquisitions = []

        @contextmanager
        def traced(path, **kwargs):
            with exclusive_lock(path, **kwargs):
                held.append(path)
                acquisitions.append(path)
                try:
                    yield
                finally:
                    self.assertEqual(held.pop(), path)

        with patch.object(line_ops, "exclusive_lock", side_effect=traced), patch.object(
            workspace, "exclusive_lock", side_effect=traced,
        ), patch.object(tasks, "exclusive_lock", side_effect=traced):
            yield held, acquisitions

    def test_create_and_spawn_check_identity_and_git_under_locks(self) -> None:
        config = load(self.root)
        original_plan = workspace._plan_line_creation
        original_base = workspace._spawn_base_for
        original_git = workspace.git
        with self._traced_locks() as (held, acquisitions):
            def check_locks():
                self.assertEqual(held, [
                    line_ops.line_identity_lock_path(config),
                    line_ops.anchor_lock_path(config, self.anchor),
                ])

            def planned(*args):
                check_locks()
                return original_plan(*args)

            def base(*args):
                check_locks()
                return original_base(*args)

            def git(*args, **kwargs):
                if args[1] == "worktree":
                    check_locks()
                return original_git(*args, **kwargs)

            with patch.object(workspace, "_plan_line_creation", side_effect=planned), patch.object(
                workspace, "_spawn_base_for", side_effect=base,
            ), patch.object(workspace, "git", side_effect=git):
                parent = workspace.create_line(config, line_id="alpha", branch="feat/alpha", base="main")
                workspace.spawn_line(config, parent.id, "child")
        self.assertEqual(len(acquisitions), 4)

    def test_tombstones_refuse_create_and_spawn_including_dry_run(self) -> None:
        config = load(self.root)
        workspace.create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        retired = config.lines_state_dir / "retired"
        retired.mkdir()
        for line_id in ("old", "alpha_old"):
            (retired / f"{line_id}.toml").write_text('id = "archived"\n')
        for dry_run in (False, True):
            with self.assertRaisesRegex(DyroError, "已退役.*不可复用"):
                workspace.create_line(config, line_id="old", branch="feat/old", base="main", dry_run=dry_run)
            with self.assertRaisesRegex(DyroError, "已退役.*不可复用"):
                workspace.spawn_line(config, "alpha", "old", dry_run=dry_run)
        self.assertFalse((self.root / "versions/old").exists())
        self.assertFalse((self.root / "versions/alpha_old").exists())

    def test_task_cli_create_and_rebinding_hold_identity_through_write(self) -> None:
        config, parent, child = self._parent_child()
        original = tasks.atomic_write_text
        with self._traced_locks() as (held, _acquisitions):
            def checked_write(path, content):
                self.assertEqual(held[0], line_ops.line_identity_lock_path(config))
                return original(path, content)

            with patch.object(tasks, "atomic_write_text", side_effect=checked_write):
                with redirect_stdout(StringIO()):
                    main(["--root", str(self.root), "task", "create", "T1", "--title", "example", "--line", parent.id, "--repository", "api"])
                task = tasks.load_task(config, "T1")
                before = (task.directory / "task.toml").read_text()
                rebound = tasks.rebind_task_line(config, task, child.id)
                self.assertEqual(rebound.line, child.id)
                self.assertEqual((task.directory / "task.toml").read_text(), before.replace('line = "alpha"', 'line = "alpha_child"'))
                with self.assertRaisesRegex(DyroError, "未登记"):
                    tasks.rebind_task_line(config, rebound, "missing")
                self.assertEqual(tasks.load_task(config, task.id).line, child.id)

    def test_identity_lock_blocks_create_spawn_and_task_binding(self) -> None:
        config, parent, child = self._parent_child()
        tasks.create_task(config, "T1", title="example", line_id=parent.id, repositories=["api"])
        task = tasks.load_task(config, "T1")
        actions = [
            lambda: workspace.create_line(config, line_id="new", branch="feat/new", base="main"),
            lambda: workspace.spawn_line(config, parent.id, "new"),
            lambda: tasks.create_task(config, "T2", title="example", line_id=parent.id, repositories=["api"]),
            lambda: tasks.rebind_task_line(config, task, child.id),
        ]

        def short_lock(path, **kwargs):
            return exclusive_lock(path, timeout_seconds=0.05)

        with exclusive_lock(line_ops.line_identity_lock_path(config)):
            with patch.object(line_ops, "exclusive_lock", side_effect=short_lock):
                with ThreadPoolExecutor(max_workers=1) as pool:
                    for action in actions:
                        with self.assertRaisesRegex(DyroError, "等待状态锁超时"):
                            pool.submit(action).result(timeout=5)
        self.assertFalse((config.task_specs_dir / "T2").exists())
        self.assertEqual(tasks.load_task(config, task.id).line, parent.id)

    def test_shared_anchor_blocks_all_mutating_entries(self) -> None:
        config, parent, child = self._parent_child()
        tasks.create_task(config, "T1", title="example", line_id=parent.id, repositories=["api"])
        task = tasks.load_task(config, "T1")
        actions = [
            lambda: workspace.create_line(config, line_id="new", branch="feat/new", base="main"),
            lambda: workspace.spawn_line(config, parent.id, "new"),
            lambda: workspace.merge_line(config, child.id, parent.id, dry_run=True),
            lambda: workspace.sync_line(config, child.id, dry_run=True),
            lambda: tasks._merge_task_repositories(config, task, push=False, dry_run=True),
        ]

        def short_lock(path, **kwargs):
            return exclusive_lock(path, timeout_seconds=0.05)

        with line_ops.anchor_lock(config, self.anchor):
            with patch.object(line_ops, "exclusive_lock", side_effect=short_lock):
                with ThreadPoolExecutor(max_workers=1) as pool:
                    for action in actions:
                        with self.assertRaisesRegex(DyroError, "等待状态锁超时"):
                            pool.submit(action).result(timeout=5)

    def test_merge_and_sync_take_line_before_anchor_even_for_probes(self) -> None:
        config, parent, child = self._parent_child()
        original = workspace.git
        for action, target in ((lambda: workspace.merge_line(config, child.id, parent.id, dry_run=True), parent),
                               (lambda: workspace.sync_line(config, child.id, dry_run=True), child)):
            with self._traced_locks() as (held, _acquisitions):
                def checked_git(*args, **kwargs):
                    if args[1] in {"merge", "commit", "reset", "push"}:
                        self.assertEqual(held, [workspace.merge_lock_path(config, target.id), line_ops.anchor_lock_path(config, self.anchor)])
                    return original(*args, **kwargs)

                with patch.object(workspace, "git", side_effect=checked_git) as git:
                    action()
                    self.assertTrue(any(call.args[1] == "merge" for call in git.call_args_list))

    def test_task_merge_preflight_holds_line_then_anchor(self) -> None:
        config, parent, _child = self._parent_child()
        tasks.create_task(config, "T1", title="example", line_id=parent.id, repositories=["api"])
        task = tasks.load_task(config, "T1")
        with self._traced_locks() as (held, _acquisitions):
            def checked_prepare(*args, **kwargs):
                self.assertEqual(held, [workspace.merge_lock_path(config, parent.id), line_ops.anchor_lock_path(config, self.anchor)])
                raise DyroError("preflight reached")

            with patch.object(tasks, "_prepare_merge", side_effect=checked_prepare):
                with self.assertRaisesRegex(DyroError, "preflight reached"):
                    tasks._merge_task_repositories(config, task, push=False, dry_run=True)

    def test_task_create_rechecks_line_after_identity_lock(self) -> None:
        config, parent, _child = self._parent_child()

        @contextmanager
        def retired_under_lock(*args, **kwargs):
            with line_ops.line_identity_lock(*args, **kwargs):
                (config.lines_state_dir / f"{parent.id}.toml").unlink()
                yield

        with patch.object(tasks, "line_identity_lock", side_effect=retired_under_lock):
            with self.assertRaisesRegex(DyroError, "未登记"):
                tasks.create_task(config, "T1", title="example", line_id=parent.id, repositories=["api"])
        self.assertFalse((config.task_specs_dir / "T1").exists())

    def test_create_rechecks_tombstone_after_identity_lock(self) -> None:
        config = load(self.root)

        @contextmanager
        def tombstone_under_lock(*args, **kwargs):
            with line_ops.line_identity_lock(*args, **kwargs):
                retired = config.lines_state_dir / "retired"
                retired.mkdir(parents=True)
                (retired / "old.toml").write_text('id = "old"\n')
                yield

        with patch.object(workspace, "line_identity_lock", side_effect=tombstone_under_lock):
            with self.assertRaisesRegex(DyroError, "已退役"):
                workspace.create_line(config, line_id="old", branch="feat/old", base="main")
        self.assertFalse((self.root / "versions/old").exists())

    def test_rebinding_dry_run_preserves_contract(self) -> None:
        config, parent, child = self._parent_child()
        path = tasks.create_task(config, "T1", title="example", line_id=parent.id, repositories=["api"])
        task = tasks.load_task(config, "T1")
        before = (path / "task.toml").read_bytes()
        self.assertEqual(tasks.rebind_task_line(config, task, child.id, dry_run=True).line, child.id)
        self.assertEqual((path / "task.toml").read_bytes(), before)

    def test_merge_rechecks_dirty_tree_after_acquiring_anchor(self) -> None:
        config, parent, child = self._parent_child()

        @contextmanager
        def changed_under_lock(*args, **kwargs):
            with line_ops.anchor_locks(*args, **kwargs):
                (workspace.line_repository_path(config, parent, "api") / "README.md").write_text("became dirty\n")
                yield

        with patch.object(workspace, "anchor_locks", side_effect=changed_under_lock):
            with self.assertRaisesRegex(DyroError, "不干净"):
                workspace.merge_line(config, child.id, parent.id)
        self.assertFalse((workspace.line_repository_path(config, parent, "api") / ".git/MERGE_HEAD").is_file())

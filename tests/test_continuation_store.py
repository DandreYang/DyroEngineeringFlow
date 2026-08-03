from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shutil
from threading import Event
import unittest
from unittest.mock import patch

from dyro.config import load
from dyro.continuation import objective_storage as storage_module
from dyro.continuation.store import (
    add_objective_target,
    assert_legacy_scheduler_allowed,
    create_objective,
    derive_objective_result,
    get_objective,
    list_objectives,
    pause_objective,
    reconcile_objective,
    remove_objective_target,
    resume_objective,
    stop_objective,
)
from dyro.errors import DyroError, ValidationError
from dyro.tasks import _reserve_local_execution, load_task, task_template
from dyro.workspace import create_line

from .support import WorkspaceCase


def _contract(objective_id: str, target: str, *, mode: str = "supervised") -> str:
    return f'''schema_version = 1
id = "{objective_id}"
title = "Objective {objective_id}"
line = "alpha"
targets = ["{target}"]

[continuation]
requested_mode = "{mode}"
operations = ["execute", "review"]
'''


class ObjectiveStoreTests(WorkspaceCase):
    def setUp(self) -> None:
        super().setUp()
        self.config = load(self.root)
        create_line(
            self.config,
            line_id="alpha",
            branch="feat/alpha",
            base="main",
        )
        self._write_task("TASK-A")
        self._write_task("TASK-B", depends_on=("TASK-A",))
        self._write_task("TASK-C")

    def _write_task(self, task_id: str, *, depends_on: tuple[str, ...] = ()) -> Path:
        directory = self.config.task_specs_dir / task_id
        directory.mkdir(parents=True)
        manifest = task_template(task_id, f"Task {task_id}", "alpha", "api", "services/api")
        manifest = manifest.replace("depends_on = []", f"depends_on = {list(depends_on)!r}")
        directory.joinpath("task.toml").write_text(
            manifest.replace('agent = "codex"', 'agent = "noop"'),
            encoding="utf-8",
        )
        directory.joinpath("handoff.md").write_text("# handoff\n", encoding="utf-8")
        return directory

    def test_create_list_pause_resume_stop_and_dry_run(self) -> None:
        preview = create_objective(self.config, _contract("preview", "TASK-B"), dry_run=True)
        self.assertEqual(preview.scope, ("TASK-A", "TASK-B"))
        self.assertFalse(self.config.objectives_dir.exists())

        created = create_objective(self.config, _contract("release", "TASK-B"))
        self.assertEqual(created.revision, 1)
        self.assertEqual(created.scope, ("TASK-A", "TASK-B"))
        self.assertEqual([item.objective.id for item in list_objectives(self.config)], ["release"])
        self.assertEqual(derive_objective_result(self.config, created), "incomplete")

        self.assertEqual(pause_objective(self.config, "release").operator_state, "paused")
        self.assertEqual(resume_objective(self.config, "release").operator_state, "active")
        self.assertEqual(stop_objective(self.config, "release").operator_state, "stopped")
        with self.assertRaisesRegex(DyroError, "不能恢复"):
            resume_objective(self.config, "release")

    def test_scope_ownership_observe_overlap_and_legacy_guard(self) -> None:
        primary = create_objective(self.config, _contract("primary", "TASK-B"))
        self.assertTrue(primary.owns_mutation_scope)
        with self.assertRaisesRegex(DyroError, "mutation scope"):
            create_objective(self.config, _contract("conflict", "TASK-A"))

        observed = create_objective(self.config, _contract("observe", "TASK-A", mode="observe"))
        self.assertFalse(observed.owns_mutation_scope)
        with self.assertRaisesRegex(DyroError, "不能绕过 ownership"):
            assert_legacy_scheduler_allowed(self.config, ("TASK-A",))
        assert_legacy_scheduler_allowed(self.config, ("TASK-C",))

    def test_scope_changes_and_reconcile_pin_new_revision(self) -> None:
        created = create_objective(self.config, _contract("release", "TASK-B"))
        expanded = add_objective_target(self.config, "release", "TASK-C")
        self.assertEqual(expanded.revision, 2)
        self.assertEqual(expanded.objective.targets, ("TASK-B", "TASK-C"))
        reduced = remove_objective_target(self.config, "release", "TASK-C")
        self.assertEqual(reduced.revision, 3)
        self.assertEqual(reduced.objective.targets, ("TASK-B",))

        task_file = self.config.task_specs_dir / "TASK-A" / "task.toml"
        task_file.write_text(task_file.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")
        self.assertEqual(derive_objective_result(self.config, get_objective(self.config, "release")), "repair_required")
        reconciled = reconcile_objective(self.config, "release")
        self.assertEqual(reconciled.revision, 4)
        self.assertEqual(derive_objective_result(self.config, reconciled), "incomplete")
        self.assertEqual(created.revision, 1)

    def test_candidate_scope_and_transitions_reject_inflight_tasks(self) -> None:
        create_objective(self.config, _contract("release", "TASK-A"))
        self.config.task_specs_dir.joinpath("TASK-C", "status").write_text("in_progress\n", encoding="utf-8")
        with self.assertRaisesRegex(DyroError, "reserved/started/running"):
            add_objective_target(self.config, "release", "TASK-C")
        self.config.task_specs_dir.joinpath("TASK-A", "status").write_text("in_progress\n", encoding="utf-8")
        with self.assertRaisesRegex(DyroError, "reserved/started/running"):
            pause_objective(self.config, "release")
        self.config.task_specs_dir.joinpath("TASK-A", "status").unlink()

        task_a = self.config.task_specs_dir / "TASK-A" / "task.toml"
        task_a.write_text(
            task_a.read_text(encoding="utf-8").replace("depends_on = []", 'depends_on = ["TASK-C"]'),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(DyroError, "reserved/started/running"):
            reconcile_objective(self.config, "release")

    def test_legacy_reservation_rechecks_objective_ownership(self) -> None:
        create_objective(self.config, _contract("release", "TASK-A"))
        task = load_task(self.config, "TASK-A")
        with self.assertRaisesRegex(DyroError, "不能绕过 ownership"):
            _reserve_local_execution(
                self.config,
                task,
                allowed=("backlog",),
                action="启动执行",
                dry_run=False,
                legacy_scheduler=True,
            )
        self.assertFalse((task.directory / "status").exists())

    def test_event_tail_and_projection_tampering_fail_closed(self) -> None:
        create_objective(self.config, _contract("release", "TASK-A"))
        directory = self.config.objectives_dir / "release"
        (directory / "events.jsonl").write_text(
            (directory / "events.jsonl").read_text(encoding="utf-8").rstrip("\n"),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValidationError, "断尾"):
            get_objective(self.config, "release")

    def test_event_seq_hash_and_checkpoint_tampering_fail_closed(self) -> None:
        for objective_id, field, replacement, message in (
            ("seq-bad", '"seq":1', '"seq":2', "seq"),
            ("hash-bad", '"sha256":"', '"sha256":"0', "哈希"),
        ):
            with self.subTest(objective_id=objective_id):
                create_objective(self.config, _contract(objective_id, "TASK-A", mode="observe"))
                events = self.config.objectives_dir / objective_id / "events.jsonl"
                events.write_text(
                    events.read_text(encoding="utf-8").replace(field, replacement, 1),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ValidationError, message):
                    get_objective(self.config, objective_id)

        create_objective(self.config, _contract("checkpoint-bad", "TASK-C", mode="observe"))
        checkpoint = self.config.objectives_dir / "checkpoint-bad" / "checkpoint.json"
        checkpoint.write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "checkpoint"):
            get_objective(self.config, "checkpoint-bad")

    def test_projection_rejects_boolean_numeric_type_bypass(self) -> None:
        create_objective(self.config, _contract("bool-bad", "TASK-A"))
        state = self.config.objectives_dir / "bool-bad" / "state.json"
        state.write_text(
            state.read_text(encoding="utf-8").replace('"revision":1', '"revision":true'),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValidationError, "投影"):
            get_objective(self.config, "bool-bad")

    def test_pause_releases_scope_and_resume_rechecks_ownership(self) -> None:
        create_objective(self.config, _contract("first", "TASK-B"))
        pause_objective(self.config, "first")
        create_objective(self.config, _contract("second", "TASK-A"))
        with self.assertRaisesRegex(DyroError, "mutation scope"):
            resume_objective(self.config, "first")

    def test_resume_rejects_contract_drift_until_reconciled(self) -> None:
        create_objective(self.config, _contract("release", "TASK-B"))
        pause_objective(self.config, "release")
        task_file = self.config.task_specs_dir / "TASK-A" / "task.toml"
        task_file.write_text(task_file.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")
        with self.assertRaisesRegex(DyroError, "reconcile"):
            resume_objective(self.config, "release")
        reconcile_objective(self.config, "release")
        self.assertEqual(resume_objective(self.config, "release").operator_state, "active")

    def test_concurrent_same_id_creation_has_one_winner(self) -> None:
        def create() -> str:
            try:
                return create_objective(self.config, _contract("same-id", "TASK-A")).objective.id
            except DyroError:
                return "rejected"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = tuple(pool.map(lambda _: create(), range(2)))
        self.assertEqual(results.count("same-id"), 1)
        self.assertEqual(results.count("rejected"), 1)

    def test_concurrent_overlapping_scope_has_one_winner(self) -> None:
        def create(objective_id: str, target: str) -> str:
            try:
                return create_objective(self.config, _contract(objective_id, target)).objective.id
            except DyroError:
                return "rejected"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = tuple(pool.map(lambda item: create(*item), (("one", "TASK-B"), ("two", "TASK-A"))))
        self.assertEqual(sum(result in {"one", "two"} for result in results), 1)
        self.assertEqual(results.count("rejected"), 1)

    def test_dry_run_scope_and_lifecycle_commands_do_not_create_lock_files(self) -> None:
        create_objective(self.config, _contract("release", "TASK-A"))
        lock = self.config.root / ".dyro" / "objectives.lock"
        lock.unlink()
        before = {
            path.relative_to(self.config.objectives_dir): path.read_bytes()
            for path in self.config.objectives_dir.rglob("*")
            if path.is_file()
        }

        reconcile_objective(self.config, "release", dry_run=True)
        add_objective_target(self.config, "release", "TASK-C", dry_run=True)
        pause_objective(self.config, "release", dry_run=True)
        resume_objective(self.config, "release", dry_run=True)
        stop_objective(self.config, "release", dry_run=True)

        after = {
            path.relative_to(self.config.objectives_dir): path.read_bytes()
            for path in self.config.objectives_dir.rglob("*")
            if path.is_file()
        }
        self.assertEqual(after, before)
        self.assertFalse(lock.exists())

    def test_interrupted_contract_and_event_transactions_recover(self) -> None:
        with patch("dyro.continuation.objective_storage._append_file", side_effect=OSError("simulated crash")):
            with self.assertRaisesRegex(OSError, "simulated crash"):
                create_objective(self.config, _contract("release", "TASK-A"))
        recovered = create_objective(self.config, _contract("release", "TASK-A"))
        self.assertEqual(recovered.revision, 1)

        with patch("dyro.continuation.objective_storage.write_projection", side_effect=OSError("simulated crash")):
            with self.assertRaisesRegex(OSError, "simulated crash"):
                pause_objective(self.config, "release")
        recovered = get_objective(self.config, "release")
        self.assertEqual(recovered.operator_state, "paused")
        self.assertFalse((self.config.objectives_dir / "release" / "pending.json").exists())

    def test_reader_waits_for_live_pending_transaction_before_recovery(self) -> None:
        entered = Event()
        release = Event()
        original_append = storage_module._append_file

        def delayed_append(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(timeout=5))
            return original_append(*args, **kwargs)

        with patch("dyro.continuation.objective_storage._append_file", side_effect=delayed_append):
            with ThreadPoolExecutor(max_workers=2) as pool:
                writer = pool.submit(create_objective, self.config, _contract("release", "TASK-A"))
                self.assertTrue(entered.wait(timeout=5))
                reader = pool.submit(list_objectives, self.config)
                self.assertFalse(reader.done())
                release.set()
                self.assertEqual(writer.result(timeout=5).objective.id, "release")
                self.assertEqual([item.objective.id for item in reader.result(timeout=5)], ["release"])

    @unittest.skipUnless(hasattr(Path, "symlink_to"), "symlink support is required")
    def test_symlinked_objective_directory_is_rejected(self) -> None:
        self.config.objectives_dir.mkdir(parents=True)
        target = self.root / "outside"
        target.mkdir()
        (self.config.objectives_dir / "unsafe").symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(ValidationError, "符号链接"):
            list_objectives(self.config)

    @unittest.skipUnless(hasattr(Path, "symlink_to"), "symlink support is required")
    def test_objective_data_symlink_is_rejected_without_external_write(self) -> None:
        create_objective(self.config, _contract("release", "TASK-A"))
        outside = self.root / "outside-state.json"
        outside.write_text('{"outside":true}\n', encoding="utf-8")
        state = self.config.objectives_dir / "release" / "state.json"
        state.unlink()
        state.symlink_to(outside)

        with self.assertRaisesRegex(ValidationError, "无法安全读取 Objective 投影"):
            pause_objective(self.config, "release")
        self.assertEqual(outside.read_text(encoding="utf-8"), '{"outside":true}\n')

    @unittest.skipUnless(hasattr(Path, "symlink_to"), "symlink support is required")
    def test_symlinked_dyro_parent_is_rejected_before_lock_write(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        shutil.rmtree(self.root / ".dyro")
        (self.root / ".dyro").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValidationError, "父目录不能是符号链接"):
            create_objective(self.config, _contract("release", "TASK-A"))
        self.assertFalse((outside / "objectives.lock").exists())
        self.assertFalse((outside / "objectives").exists())


if __name__ == "__main__":
    unittest.main()

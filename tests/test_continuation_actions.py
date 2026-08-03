from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

from dyro.config import load
from dyro.continuation import objective_storage as storage_module
from dyro.continuation import owner_lease as owner_lease_module
from dyro.continuation.actions import (
    ActionIntent,
    ActionReceipt,
    ActionStatus,
    acquire_owner_lease,
    list_actions,
    read_action,
    read_owner_lease,
    record_action_receipt,
    release_owner_lease,
    renew_owner_lease,
    reserve_action,
    start_action,
)
from dyro.continuation.budgets import BudgetReservation
from dyro.continuation.models import ActionKind
from dyro.continuation.objective_storage import open_objective_directory
from dyro.continuation.store import (
    add_objective_target,
    acquire_objective_owner_lease,
    create_objective,
    get_objective,
    get_objective_action,
    list_objective_actions,
    pause_objective,
    record_objective_action_receipt,
    reconcile_objective,
    remove_objective_target,
    resume_objective,
    reserve_objective_action,
    start_objective_action,
)
from dyro.errors import DyroError, ValidationError
from dyro.tasks import task_template
from dyro.workspace import create_line

from .support import WorkspaceCase


def _contract(objective_id: str, target: str) -> str:
    return f'''schema_version = 1
id = "{objective_id}"
title = "Objective {objective_id}"
line = "alpha"
targets = ["{target}"]

[continuation]
requested_mode = "supervised"
operations = ["execute", "review"]
'''


class ActionJournalTests(WorkspaceCase):
    def setUp(self) -> None:
        super().setUp()
        self.config = load(self.root)
        create_line(self.config, line_id="alpha", branch="feat/alpha", base="main")
        self._write_task("TASK-A")
        self.record = create_objective(self.config, _contract("release", "TASK-A"))
        self.now = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)

    def _write_task(self, task_id: str) -> Path:
        directory = self.config.task_specs_dir / task_id
        directory.mkdir(parents=True)
        manifest = task_template(task_id, f"Task {task_id}", "alpha", "api", "services/api")
        directory.joinpath("task.toml").write_text(
            manifest.replace('agent = "codex"', 'agent = "noop"'), encoding="utf-8"
        )
        directory.joinpath("handoff.md").write_text("# handoff\n", encoding="utf-8")
        return directory

    def _intent(self, *, action_id: str, generation: int) -> ActionIntent:
        return ActionIntent(
            action_id=action_id,
            objective_id="release",
            objective_revision=self.record.revision,
            objective_event_seq=self.record.event_seq,
            objective_event_sha256=self.record.event_sha256,
            scope_sha256=self.record.scope_sha256,
            snapshot_sha256="a" * 64,
            plan_sha256="b" * 64,
            operation=ActionKind.EXECUTE_TASK,
            subject_id="TASK-A",
            owner_generation=generation,
            expected_operation_generation=0,
            authority_sha256="c" * 64,
            budget_reservation=BudgetReservation("release", "TASK-A"),
            created_at=self.now,
        )

    def _intent_for(self, *, action_id: str, generation: int, operation: ActionKind, subject_id: str) -> ActionIntent:
        return ActionIntent(
            action_id=action_id,
            objective_id="release",
            objective_revision=self.record.revision,
            objective_event_seq=self.record.event_seq,
            objective_event_sha256=self.record.event_sha256,
            scope_sha256=self.record.scope_sha256,
            snapshot_sha256="a" * 64,
            plan_sha256="b" * 64,
            operation=operation,
            subject_id=subject_id,
            owner_generation=generation,
            expected_operation_generation=0,
            authority_sha256="c" * 64,
            budget_reservation=BudgetReservation("release", subject_id),
            created_at=self.now,
        )

    def _acquire(self, directory, *, now: datetime | None = None, token: str = "1" * 64):
        return acquire_owner_lease(
            directory,
            objective_id="release",
            now=now or self.now,
            ttl_seconds=30,
            pid=123,
            process_start="boot-1",
            owner_token=token,
        )

    def test_create_only_intent_start_and_receipt_are_idempotent(self) -> None:
        with open_objective_directory(self.config, "release") as directory:
            grant = self._acquire(directory)
            intent = self._intent(action_id="action-1", generation=grant.lease.generation)
            reserved = reserve_action(directory, intent)
            self.assertEqual(reserved.status, ActionStatus.RESERVED)
            self.assertEqual(reserve_action(directory, intent), reserved)
            retried = reserve_action(
                directory,
                replace(intent, action_id="action-duplicate", created_at=self.now + timedelta(seconds=1)),
            )
            self.assertEqual(retried, reserved)

            started = start_action(directory, action_id="action-1", grant=grant, now=self.now + timedelta(seconds=1))
            self.assertEqual(started.status, ActionStatus.UNCERTAIN)
            repeated_start = start_action(directory, action_id="action-1", grant=grant, now=self.now + timedelta(seconds=2))
            self.assertEqual(repeated_start.start, started.start)

            receipt = ActionReceipt(
                action_id="action-1",
                idempotency_key=intent.idempotency_key,
                owner_generation=grant.lease.generation,
                status=ActionStatus.SUCCEEDED,
                summary="gate-passed",
                recorded_at=self.now + timedelta(seconds=3),
            )
            completed = record_action_receipt(directory, receipt)
            self.assertEqual(completed.status, ActionStatus.SUCCEEDED)
            self.assertEqual(record_action_receipt(directory, receipt).receipt, receipt)
            self.assertEqual(list_actions(directory), (completed,))

    def test_fencing_blocks_old_start_but_accepts_old_started_receipt(self) -> None:
        with open_objective_directory(self.config, "release") as directory:
            first = self._acquire(directory, token="1" * 64)
            first_intent = self._intent(action_id="action-old", generation=first.lease.generation)
            reserve_action(directory, first_intent)
            start_action(directory, action_id="action-old", grant=first, now=self.now + timedelta(seconds=1))

            second = self._acquire(directory, now=self.now + timedelta(seconds=31), token="2" * 64)
            self.assertEqual(second.lease.generation, 2)
            next_intent = self._intent(action_id="action-new", generation=second.lease.generation)
            reserve_action(directory, next_intent)
            with self.assertRaisesRegex(DyroError, "失效|围栏"):
                start_action(directory, action_id="action-new", grant=first, now=self.now + timedelta(seconds=32))

            finished = record_action_receipt(
                directory,
                ActionReceipt(
                    action_id="action-old",
                    idempotency_key=first_intent.idempotency_key,
                    owner_generation=first.lease.generation,
                    status=ActionStatus.FAILED,
                    summary="runner-exited",
                    recorded_at=self.now + timedelta(seconds=32),
                ),
            )
            self.assertEqual(finished.status, ActionStatus.FAILED)

    def test_control_plane_facade_rechecks_objective_and_lease_bindings(self) -> None:
        grant = acquire_objective_owner_lease(
            self.config,
            "release",
            now=self.now,
            ttl_seconds=30,
            pid=123,
            process_start="boot-1",
            owner_token="1" * 64,
        )
        intent = self._intent(action_id="action-facade", generation=grant.lease.generation)
        reserved = reserve_objective_action(
            self.config,
            "release",
            intent=intent,
            grant=grant,
            now=self.now,
        )
        self.assertEqual(reserved.status, ActionStatus.RESERVED)
        started = start_objective_action(
            self.config,
            "release",
            action_id="action-facade",
            grant=grant,
            now=self.now + timedelta(seconds=1),
        )
        self.assertEqual(started.status, ActionStatus.UNCERTAIN)
        completed = record_objective_action_receipt(
            self.config,
            "release",
            receipt=ActionReceipt(
                action_id="action-facade",
                idempotency_key=intent.idempotency_key,
                owner_generation=grant.lease.generation,
                status=ActionStatus.SUCCEEDED,
                summary="facade-complete",
                recorded_at=self.now + timedelta(seconds=2),
            ),
        )
        self.assertEqual(list_objective_actions(self.config, "release"), (completed,))

    def test_control_plane_facade_requires_current_owner_to_cancel_unstarted_action(self) -> None:
        grant = acquire_objective_owner_lease(
            self.config,
            "release",
            now=self.now,
            ttl_seconds=30,
            pid=123,
            process_start="boot-1",
            owner_token="1" * 64,
        )
        intent = self._intent(action_id="action-facade-cancel", generation=grant.lease.generation)
        reserve_objective_action(self.config, "release", intent=intent, grant=grant, now=self.now)
        receipt = ActionReceipt(
            action_id=intent.action_id,
            idempotency_key=intent.idempotency_key,
            owner_generation=grant.lease.generation,
            status=ActionStatus.CANCELLED,
            summary="cancelled-before-start",
            recorded_at=self.now + timedelta(seconds=1),
        )
        with self.assertRaisesRegex(DyroError, "owner grant"):
            record_objective_action_receipt(self.config, "release", receipt=receipt)
        cancelled = record_objective_action_receipt(
            self.config,
            "release",
            receipt=receipt,
            grant=grant,
            now=self.now + timedelta(seconds=1),
        )
        self.assertEqual(cancelled.status, ActionStatus.CANCELLED)

    def test_idempotency_key_binds_authority_budget_and_active_objective(self) -> None:
        with open_objective_directory(self.config, "release") as directory:
            grant = self._acquire(directory)
            intent = self._intent(action_id="action-bound", generation=grant.lease.generation)
            changed_authority = replace(intent, action_id="action-authority", authority_sha256="d" * 64, idempotency_key="")
            changed_budget = replace(
                intent,
                action_id="action-budget",
                budget_reservation=BudgetReservation("release", "TASK-A", attempts=2),
                idempotency_key="",
            )
            self.assertNotEqual(intent.idempotency_key, changed_authority.idempotency_key)
            self.assertNotEqual(intent.idempotency_key, changed_budget.idempotency_key)

        pause_objective(self.config, "release")
        with self.assertRaisesRegex(DyroError, "未处于 active"):
            acquire_objective_owner_lease(
                self.config,
                "release",
                now=self.now,
                ttl_seconds=30,
                pid=123,
                process_start="boot-1",
                owner_token="2" * 64,
            )

    def test_control_plane_facade_rejects_out_of_scope_or_unauthorized_operations(self) -> None:
        grant = acquire_objective_owner_lease(
            self.config,
            "release",
            now=self.now,
            ttl_seconds=30,
            pid=123,
            process_start="boot-1",
            owner_token="1" * 64,
        )
        outside = self._intent_for(
            action_id="action-outside",
            generation=grant.lease.generation,
            operation=ActionKind.EXECUTE_TASK,
            subject_id="OUTSIDE-TASK",
        )
        with self.assertRaisesRegex(DyroError, "mutation scope"):
            reserve_objective_action(self.config, "release", intent=outside, grant=grant, now=self.now)
        unauthorized = self._intent_for(
            action_id="action-merge",
            generation=grant.lease.generation,
            operation=ActionKind.MERGE_TASK,
            subject_id="TASK-A",
        )
        with self.assertRaisesRegex(DyroError, "未获"):
            reserve_objective_action(self.config, "release", intent=unauthorized, grant=grant, now=self.now)

    def test_pause_resume_fences_unstarted_action_from_the_prior_objective_event(self) -> None:
        grant = acquire_objective_owner_lease(
            self.config,
            "release",
            now=self.now,
            ttl_seconds=30,
            pid=123,
            process_start="boot-1",
            owner_token="1" * 64,
        )
        intent = self._intent(action_id="action-paused", generation=grant.lease.generation)
        reserve_objective_action(self.config, "release", intent=intent, grant=grant, now=self.now)
        pause_objective(self.config, "release")
        self.assertEqual(get_objective_action(self.config, "release", "action-paused").status, ActionStatus.CANCELLED)
        resume_objective(self.config, "release")
        with self.assertRaisesRegex(DyroError, "事件"):
            start_objective_action(
                self.config,
                "release",
                action_id="action-paused",
                grant=grant,
                now=self.now + timedelta(seconds=1),
            )

    def test_lease_takeover_cancels_reserved_actions(self) -> None:
        first = acquire_objective_owner_lease(
            self.config,
            "release",
            now=self.now,
            ttl_seconds=30,
            pid=123,
            process_start="boot-1",
            owner_token="1" * 64,
        )
        intent = self._intent(action_id="action-takeover", generation=first.lease.generation)
        reserve_objective_action(self.config, "release", intent=intent, grant=first, now=self.now)
        second = acquire_objective_owner_lease(
            self.config,
            "release",
            now=self.now + timedelta(seconds=31),
            ttl_seconds=30,
            pid=456,
            process_start="boot-2",
            owner_token="2" * 64,
        )
        self.assertEqual(second.lease.generation, 2)
        self.assertEqual(get_objective_action(self.config, "release", intent.action_id).status, ActionStatus.CANCELLED)

    def test_lease_takeover_cancellation_recovers_after_owner_lease_is_durable(self) -> None:
        first = acquire_objective_owner_lease(
            self.config,
            "release",
            now=self.now,
            ttl_seconds=30,
            pid=123,
            process_start="boot-1",
            owner_token="1" * 64,
        )
        intent = self._intent(action_id="action-takeover-recovery", generation=first.lease.generation)
        reserve_objective_action(self.config, "release", intent=intent, grant=first, now=self.now)
        with patch.object(owner_lease_module, "apply_action_cancellation", side_effect=DyroError("simulated crash")):
            with self.assertRaisesRegex(DyroError, "simulated crash"):
                acquire_objective_owner_lease(
                    self.config,
                    "release",
                    now=self.now + timedelta(seconds=31),
                    ttl_seconds=30,
                    pid=456,
                    process_start="boot-2",
                    owner_token="2" * 64,
                )
        self.assertEqual(get_objective_action(self.config, "release", intent.action_id).status, ActionStatus.RESERVED)
        with open_objective_directory(self.config, "release") as directory:
            recovered = read_owner_lease(directory)
        assert recovered is not None
        self.assertEqual(recovered.generation, 2)
        self.assertEqual(get_objective_action(self.config, "release", intent.action_id).status, ActionStatus.CANCELLED)

    def test_owner_takeover_recovery_accepts_only_the_exact_prior_lease(self) -> None:
        first = acquire_objective_owner_lease(
            self.config,
            "release",
            now=self.now,
            ttl_seconds=30,
            pid=123,
            process_start="boot-1",
            owner_token="1" * 64,
        )
        second = acquire_objective_owner_lease(
            self.config,
            "release",
            now=self.now + timedelta(seconds=31),
            ttl_seconds=30,
            pid=456,
            process_start="boot-2",
            owner_token="2" * 64,
        )
        intent = self._intent(action_id="action-takeover-rollback", generation=second.lease.generation)
        reserve_objective_action(
            self.config,
            "release",
            intent=intent,
            grant=second,
            now=self.now + timedelta(seconds=32),
        )
        with patch.object(owner_lease_module, "_replace_json", side_effect=DyroError("simulated pre-lease crash")):
            with self.assertRaisesRegex(DyroError, "simulated pre-lease crash"):
                acquire_objective_owner_lease(
                    self.config,
                    "release",
                    now=self.now + timedelta(seconds=62),
                    ttl_seconds=30,
                    pid=789,
                    process_start="boot-3",
                    owner_token="3" * 64,
                )
        with open_objective_directory(self.config, "release") as directory:
            owner_lease_module._replace_json(
                directory.fd,
                "scheduler-owner.json",
                owner_lease_module._lease_payload(first.lease),
                "test unexpected rollback",
            )
            pending = owner_lease_module._read_json(
                directory.fd,
                "scheduler-owner-pending.json",
                "test takeover pending",
            )
            assert isinstance(pending, dict)
            pending["previous_lease"] = owner_lease_module._lease_payload(first.lease)
            owner_lease_module._replace_json(
                directory.fd,
                "scheduler-owner-pending.json",
                pending,
                "test tampered takeover pending",
            )
            with self.assertRaisesRegex(ValidationError, "takeover pending"):
                read_owner_lease(directory)
        self.assertEqual(get_objective_action(self.config, "release", intent.action_id).status, ActionStatus.RESERVED)

    def test_rejected_objective_mutations_do_not_cancel_reserved_actions(self) -> None:
        grant = acquire_objective_owner_lease(
            self.config,
            "release",
            now=self.now,
            ttl_seconds=30,
            pid=123,
            process_start="boot-1",
            owner_token="1" * 64,
        )
        first = self._intent(action_id="action-survive-add", generation=grant.lease.generation)
        reserve_objective_action(self.config, "release", intent=first, grant=grant, now=self.now)
        with self.assertRaisesRegex(ValidationError, "TaskGraph"):
            add_objective_target(self.config, "release", "UNKNOWN-TASK")
        self.assertEqual(get_objective_action(self.config, "release", first.action_id).status, ActionStatus.RESERVED)

        self._write_task("TASK-B")
        self.record = add_objective_target(self.config, "release", "TASK-B")
        self.assertEqual(get_objective_action(self.config, "release", first.action_id).status, ActionStatus.CANCELLED)
        second = self._intent(action_id="action-survive-reject", generation=grant.lease.generation)
        reserve_objective_action(self.config, "release", intent=second, grant=grant, now=self.now)
        with self.assertRaisesRegex(DyroError, "target 不存在"):
            remove_objective_target(self.config, "release", "UNKNOWN-TASK")
        self.assertEqual(get_objective_action(self.config, "release", second.action_id).status, ActionStatus.RESERVED)

        (self.config.task_specs_dir / "TASK-A" / "task.toml").unlink()
        with self.assertRaisesRegex(ValidationError, "缺少 TaskGraph"):
            reconcile_objective(self.config, "release")
        self.assertEqual(get_objective_action(self.config, "release", second.action_id).status, ActionStatus.RESERVED)

    def test_rejected_pause_does_not_cancel_reserved_action(self) -> None:
        grant = acquire_objective_owner_lease(
            self.config,
            "release",
            now=self.now,
            ttl_seconds=30,
            pid=123,
            process_start="boot-1",
            owner_token="1" * 64,
        )
        intent = self._intent(action_id="action-survive-pause", generation=grant.lease.generation)
        reserve_objective_action(self.config, "release", intent=intent, grant=grant, now=self.now)
        self.config.task_specs_dir.joinpath("TASK-A", "status").write_text("in_progress\n", encoding="utf-8")
        with self.assertRaisesRegex(DyroError, "拒绝变更"):
            pause_objective(self.config, "release")
        self.assertEqual(get_objective_action(self.config, "release", intent.action_id).status, ActionStatus.RESERVED)

    def test_lifecycle_cancellation_recovers_only_after_its_event_is_durable(self) -> None:
        grant = acquire_objective_owner_lease(
            self.config,
            "release",
            now=self.now,
            ttl_seconds=30,
            pid=123,
            process_start="boot-1",
            owner_token="1" * 64,
        )
        intent = self._intent(action_id="action-transactional-pause", generation=grant.lease.generation)
        reserve_objective_action(self.config, "release", intent=intent, grant=grant, now=self.now)
        with patch.object(storage_module, "_apply_pending_action_cancellation", side_effect=DyroError("simulated crash")):
            with self.assertRaisesRegex(DyroError, "simulated crash"):
                pause_objective(self.config, "release")
        self.assertEqual(get_objective_action(self.config, "release", intent.action_id).status, ActionStatus.RESERVED)
        self.assertEqual(get_objective(self.config, "release").operator_state, "paused")
        self.assertEqual(get_objective_action(self.config, "release", intent.action_id).status, ActionStatus.CANCELLED)

    def test_uncertain_action_retains_scope_and_blocks_reconcile(self) -> None:
        grant = acquire_objective_owner_lease(
            self.config,
            "release",
            now=self.now,
            ttl_seconds=30,
            pid=123,
            process_start="boot-1",
            owner_token="1" * 64,
        )
        intent = self._intent(action_id="action-uncertain", generation=grant.lease.generation)
        reserve_objective_action(self.config, "release", intent=intent, grant=grant, now=self.now)
        start_objective_action(
            self.config,
            "release",
            action_id=intent.action_id,
            grant=grant,
            now=self.now + timedelta(seconds=1),
        )
        pause_objective(self.config, "release")
        with self.assertRaisesRegex(DyroError, "uncertain"):
            reconcile_objective(self.config, "release")
        with self.assertRaisesRegex(DyroError, "mutation scope"):
            create_objective(self.config, _contract("overlap", "TASK-A"))
        record_objective_action_receipt(
            self.config,
            "release",
            receipt=ActionReceipt(
                action_id=intent.action_id,
                idempotency_key=intent.idempotency_key,
                owner_generation=grant.lease.generation,
                status=ActionStatus.FAILED,
                summary="runner-exited",
                recorded_at=self.now + timedelta(seconds=2),
            ),
        )
        self.assertEqual(create_objective(self.config, _contract("overlap", "TASK-A")).objective.id, "overlap")

    def test_renew_release_and_unstarted_cancellation_are_bound_to_owner(self) -> None:
        with open_objective_directory(self.config, "release") as directory:
            grant = self._acquire(directory)
            renewed = renew_owner_lease(directory, grant=grant, now=self.now + timedelta(seconds=1), ttl_seconds=30)
            self.assertEqual(renewed.lease.generation, grant.lease.generation)

            intent = self._intent(action_id="action-cancel", generation=renewed.lease.generation)
            reserve_action(directory, intent)
            cancelled = record_action_receipt(
                directory,
                ActionReceipt(
                    action_id="action-cancel",
                    idempotency_key=intent.idempotency_key,
                    owner_generation=renewed.lease.generation,
                    status=ActionStatus.CANCELLED,
                    summary="paused-before-start",
                    recorded_at=self.now + timedelta(seconds=2),
                ),
                grant=renewed,
                now=self.now + timedelta(seconds=2),
            )
            self.assertEqual(cancelled.status, ActionStatus.CANCELLED)
            released = release_owner_lease(directory, grant=renewed, now=self.now + timedelta(seconds=3))
            self.assertFalse(released.active)
            self.assertEqual(
                record_action_receipt(
                    directory,
                    ActionReceipt(
                        action_id="action-cancel",
                        idempotency_key=intent.idempotency_key,
                        owner_generation=renewed.lease.generation,
                        status=ActionStatus.CANCELLED,
                        summary="paused-before-start",
                        recorded_at=self.now + timedelta(seconds=2),
                    ),
                ).receipt.status,
                ActionStatus.CANCELLED,
            )
            next_grant = self._acquire(directory, now=self.now + timedelta(seconds=4), token="2" * 64)
            self.assertEqual(next_grant.lease.generation, 2)

    @unittest.skipUnless(hasattr(Path, "symlink_to"), "symlink support is required")
    def test_symlinked_action_directory_and_bad_json_fail_closed(self) -> None:
        with open_objective_directory(self.config, "release") as directory:
            grant = self._acquire(directory)
            intent = self._intent(action_id="action-safe", generation=grant.lease.generation)
            reserve_action(directory, intent)

        action_directory = self.config.objectives_dir / "release" / "actions"
        outside = self.root / "outside-actions"
        outside.mkdir()
        shutil.rmtree(action_directory)
        action_directory.symlink_to(outside, target_is_directory=True)
        with open_objective_directory(self.config, "release") as directory:
            with self.assertRaisesRegex(ValidationError, "目录"):
                list_actions(directory)
        self.assertFalse(any(outside.iterdir()))

    def test_incompatible_or_unstarted_receipts_fail_closed(self) -> None:
        with open_objective_directory(self.config, "release") as directory:
            grant = self._acquire(directory)
            intent = self._intent(action_id="action-open", generation=grant.lease.generation)
            reserve_action(directory, intent)
            with self.assertRaisesRegex(DyroError, "未 start"):
                record_action_receipt(
                    directory,
                    ActionReceipt(
                        action_id="action-open",
                        idempotency_key=intent.idempotency_key,
                        owner_generation=grant.lease.generation,
                        status=ActionStatus.SUCCEEDED,
                        summary="must-not-complete",
                        recorded_at=self.now,
                    ),
                )
            with self.assertRaisesRegex(DyroError, "owner grant"):
                record_action_receipt(
                    directory,
                    ActionReceipt(
                        action_id="action-open",
                        idempotency_key=intent.idempotency_key,
                        owner_generation=grant.lease.generation,
                        status=ActionStatus.CANCELLED,
                        summary="unauthorized-cancel",
                        recorded_at=self.now,
                    ),
                )
            with self.assertRaisesRegex(ValidationError, "idempotency_key"):
                ActionIntent(
                    **{
                        **self._intent(action_id="action-bad", generation=grant.lease.generation).__dict__,
                        "idempotency_key": "0" * 64,
                    }
                )
            action_file = self.config.objectives_dir / "release" / "actions" / "action-open.json"
            action_file.write_bytes((b"[" * 10_000) + (b"]" * 10_000))
            with self.assertRaisesRegex(ValidationError, "JSON"):
                read_action(directory, "action-open")

    def test_orphan_start_or_receipt_fails_closed(self) -> None:
        with open_objective_directory(self.config, "release") as directory:
            grant = self._acquire(directory)
            intent = self._intent(action_id="action-valid", generation=grant.lease.generation)
            reserve_action(directory, intent)
        orphan = self.config.objectives_dir / "release" / "action-starts"
        orphan.mkdir()
        orphan.joinpath("action-orphan.json").write_text("{}", encoding="utf-8")
        with open_objective_directory(self.config, "release") as directory:
            with self.assertRaisesRegex(ValidationError, "没有 intent"):
                list_actions(directory)


if __name__ == "__main__":
    unittest.main()

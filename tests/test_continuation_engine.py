from __future__ import annotations

from datetime import datetime, timezone
import tempfile
import unittest
from pathlib import Path

from dyro.continuation.engine import (
    WaveDeferralReason,
    build_scheduler_tick,
    scheduler_tick_payload,
)
from dyro.continuation.models import (
    ActionKind,
    AttentionItem,
    AttentionKind,
    ContinuationPlan,
    PlanCompletion,
    PlannedAction,
    ReasonCode,
)
from dyro.continuation.snapshot import SchedulerSnapshot, SchedulerTaskSnapshot
from dyro.tasks import Task


class SchedulerTickTests(unittest.TestCase):
    def test_tick_selects_one_deterministic_wave_and_defers_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            sensitive_agent = "runner --token=do-not-disclose"
            snapshot = self._snapshot(
                Path(temporary),
                (
                    self._task(
                        Path(temporary),
                        "TASK-A",
                        conflict_group="release",
                        executor=sensitive_agent,
                    ),
                    self._task(
                        Path(temporary),
                        "TASK-B",
                        conflict_group="release",
                        executor="cursor",
                    ),
                    self._task(Path(temporary), "TASK-C", executor=sensitive_agent),
                ),
            )
            plan = self._plan(
                PlannedAction(ActionKind.EXECUTE_TASK, "TASK-A", ReasonCode.TASK_READY),
                PlannedAction(ActionKind.EXECUTE_TASK, "TASK-B", ReasonCode.TASK_READY),
                PlannedAction(ActionKind.EXECUTE_TASK, "TASK-C", ReasonCode.TASK_READY),
            )

            tick = build_scheduler_tick(snapshot, plan, max_parallel=3)

            self.assertEqual([item.subject_id for item in tick.wave], ["TASK-A"])
            self.assertEqual(
                [item.action.subject_id for item in tick.deferred], ["TASK-B", "TASK-C"]
            )
            self.assertEqual(
                tick.deferred[0].reason, WaveDeferralReason.RESOURCE_CONFLICT
            )
            self.assertEqual(
                dict(tick.deferred[0].facts),
                {
                    "resource_class": "conflict",
                    "selected_subject_id": "TASK-A",
                },
            )
            self.assertEqual(
                tick.deferred[1].reason, WaveDeferralReason.RESOURCE_CONFLICT
            )
            self.assertEqual(dict(tick.deferred[1].facts)["resource_class"], "agent")
            self.assertNotIn(sensitive_agent, str(scheduler_tick_payload(tick)))

    def test_capacity_is_enforced_after_resource_admission(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot = self._snapshot(
                root,
                (
                    self._task(root, "TASK-A", executor="codex"),
                    self._task(root, "TASK-B", executor="cursor"),
                ),
            )
            plan = self._plan(
                PlannedAction(ActionKind.EXECUTE_TASK, "TASK-A", ReasonCode.TASK_READY),
                PlannedAction(ActionKind.EXECUTE_TASK, "TASK-B", ReasonCode.TASK_READY),
            )

            tick = build_scheduler_tick(snapshot, plan, max_parallel=1)

            self.assertEqual([item.subject_id for item in tick.wave], ["TASK-A"])
            self.assertEqual(
                tick.deferred[0].reason, WaveDeferralReason.PARALLEL_CAPACITY
            )
            self.assertEqual(
                dict(tick.deferred[0].facts),
                {
                    "active_parallel": "0",
                    "available_parallel": "1",
                    "max_parallel": "1",
                },
            )

    def test_observed_running_task_exhausts_wave_capacity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            running = self._task(root, "TASK-A", executor="codex")
            ready = self._task(root, "TASK-B", executor="cursor")
            snapshot = SchedulerSnapshot(
                observed_at=datetime(2026, 8, 4, 8, 0, tzinfo=timezone.utc),
                tasks=(
                    SchedulerTaskSnapshot(
                        running, "in_progress", False, "not_required"
                    ),
                    SchedulerTaskSnapshot(ready, "backlog", False, "not_required"),
                ),
                decisions=(),
                execution_mode="local",
                candidate_ids=("TASK-A", "TASK-B"),
                snapshot_sha256="a" * 64,
                objective_id="release",
                objective_revision=1,
                objective_state="active",
                objective_scope=("TASK-A", "TASK-B"),
                objective_targets=("TASK-B",),
                objective_requested_mode="supervised",
                objective_operations=("execute", "review"),
            )
            plan = self._plan(
                PlannedAction(ActionKind.EXECUTE_TASK, "TASK-B", ReasonCode.TASK_READY)
            )

            tick = build_scheduler_tick(snapshot, plan, max_parallel=1)

            self.assertEqual(tick.wave, ())
            self.assertEqual(tick.active_parallel, 1)
            self.assertEqual(tick.available_parallel, 0)
            self.assertEqual(
                tick.deferred[0].reason, WaveDeferralReason.PARALLEL_CAPACITY
            )
            self.assertEqual(
                dict(tick.deferred[0].facts),
                {
                    "active_parallel": "1",
                    "available_parallel": "0",
                    "max_parallel": "1",
                },
            )

    def test_non_mutating_actions_remain_visible_but_never_enter_mutation_wave(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot = self._snapshot(root, (self._task(root, "TASK-A"),))
            ask = PlannedAction(
                ActionKind.ASK_USER, "TASK-A", ReasonCode.ANSWER_REQUIRED
            )
            plan = self._plan(ask)

            tick = build_scheduler_tick(snapshot, plan, max_parallel=1)

            self.assertEqual(tick.wave, ())
            self.assertEqual(tick.deferred, ())
            self.assertEqual(tick.non_mutating_actions, (ask,))

    def test_tick_payload_is_deterministic_and_path_free(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot = self._snapshot(root, (self._task(root, "TASK-A"),))
            plan = self._plan(
                PlannedAction(ActionKind.EXECUTE_TASK, "TASK-A", ReasonCode.TASK_READY)
            )

            first = build_scheduler_tick(snapshot, plan, max_parallel=1)
            second = build_scheduler_tick(snapshot, plan, max_parallel=1)
            payload = scheduler_tick_payload(first)

            self.assertEqual(first, second)
            self.assertEqual(first.tick_sha256, second.tick_sha256)
            self.assertNotIn(str(root), str(payload))
            self.assertEqual(payload["wave"][0]["subject_id"], "TASK-A")

    @staticmethod
    def _task(
        root: Path, task_id: str, *, conflict_group: str = "", executor: str = "noop"
    ) -> Task:
        return Task(
            id=task_id,
            title=task_id,
            line="alpha",
            risk="write",
            executor=executor,
            reviewer="reviewer",
            repositories=("api",),
            conflict_group=conflict_group,
            directory=root / task_id,
        )

    @staticmethod
    def _snapshot(root: Path, tasks: tuple[Task, ...]) -> SchedulerSnapshot:
        return SchedulerSnapshot(
            observed_at=datetime(2026, 8, 4, 8, 0, tzinfo=timezone.utc),
            tasks=tuple(
                SchedulerTaskSnapshot(task, "backlog", False, "not_required")
                for task in tasks
            ),
            decisions=(),
            execution_mode="local",
            candidate_ids=tuple(task.id for task in tasks),
            snapshot_sha256="a" * 64,
            objective_id="release",
            objective_revision=1,
            objective_state="active",
            objective_scope=tuple(task.id for task in tasks),
            objective_targets=(tasks[0].id,),
            objective_requested_mode="supervised",
            objective_operations=("execute", "review"),
        )

    @staticmethod
    def _plan(*actions: PlannedAction) -> ContinuationPlan:
        return ContinuationPlan(
            objective_id="release",
            snapshot_sha256="a" * 64,
            plan_sha256="b" * 64,
            completion=PlanCompletion.INCOMPLETE,
            selected_actions=actions,
            attention=(
                AttentionItem(
                    "ready:TASK-A", AttentionKind.READY, "TASK-A", ReasonCode.TASK_READY
                ),
            ),
        )


if __name__ == "__main__":
    unittest.main()

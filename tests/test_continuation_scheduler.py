from __future__ import annotations

from datetime import datetime, timezone
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dyro.continuation.models import (
    ActionKind,
    BudgetLimit,
    CompletionRule,
    Objective,
    Operation,
    PlanCompletion,
    ReasonCode,
    RequestedMode,
)
from dyro.continuation.planner import (
    build_continuation_plan,
    build_scheduler_projection,
    build_task_readiness,
    projection_payload,
    render_projection_mermaid,
)
from dyro.continuation.snapshot import (
    SchedulerSnapshot,
    SchedulerTaskSnapshot,
    build_scheduler_snapshot,
)
from dyro.graph import TaskGraph
from dyro.tasks import Task, external_claim_active


class ContinuationSchedulerSnapshotTests(unittest.TestCase):
    def test_snapshot_is_canonical_and_samples_known_status_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task_a = self._task(root, "A")
            task_b = self._task(root, "B")
            graph = TaskGraph(
                line=None,
                tasks=(task_a, task_b),
                known_tasks=(task_a, task_b),
                decisions={"D-1": "open"},
                execution_mode="local",
            )
            config = SimpleNamespace(policy=SimpleNamespace(execution_mode="local"))
            def clock() -> datetime:
                return datetime(2026, 8, 4, 8, 0, tzinfo=timezone.utc)
            with (
                patch("dyro.graph.build_task_graph", return_value=graph),
                patch("dyro.graph.validate_task_graph", return_value=()),
                patch("dyro.tasks.status", return_value="backlog") as read_status,
            ):
                first = build_scheduler_snapshot(config, candidates=(task_b,), clock=clock)
                second = build_scheduler_snapshot(config, candidates=(task_b,), clock=clock)

            self.assertEqual(first.snapshot_sha256, second.snapshot_sha256)
            self.assertEqual(first.candidate_ids, ("B",))
            self.assertEqual([item.task.id for item in first.tasks], ["A", "B"])
            self.assertEqual(read_status.call_count, 4)

    def test_pure_readiness_exposes_stable_reason_codes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            complete = self._task(root, "A")
            ready = self._task(root, "B", depends_on=("A",))
            waiting = self._task(root, "C", blocked_on=("D-1",))
            snapshot = SchedulerSnapshot(
                observed_at=datetime(2026, 8, 4, 8, 0, tzinfo=timezone.utc),
                tasks=(
                    SchedulerTaskSnapshot(complete, "done", False, "integrated"),
                    SchedulerTaskSnapshot(ready, "backlog", False, "not_required"),
                    SchedulerTaskSnapshot(waiting, "backlog", False, "not_required"),
                ),
                decisions=(("D-1", "open"),),
                execution_mode="local",
                candidate_ids=("B", "C"),
                snapshot_sha256="a" * 64,
            )

            readiness = build_task_readiness(snapshot)

            self.assertEqual([task.id for task in readiness.ready], ["B"])
            self.assertEqual(readiness.blocked[0].subject_id, "C")
            self.assertEqual(readiness.blocked[0].reason, ReasonCode.DECISION_OPEN)

    def test_readiness_covers_all_blocking_reason_classes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tasks = (
                self._task(root, "A"),
                self._task(root, "B"),
                self._task(root, "C", depends_on=("A",)),
                self._task(root, "D", depends_on=("B",)),
                self._task(root, "E", blocked_on=("D-1",)),
                self._task(root, "F"),
                self._task(root, "G", conflict_group="release"),
                self._task(root, "H", conflict_group="release"),
            )
            snapshot = SchedulerSnapshot(
                observed_at=datetime(2026, 8, 4, 8, 0, tzinfo=timezone.utc),
                tasks=(
                    SchedulerTaskSnapshot(tasks[0], "backlog", False, "not_required"),
                    SchedulerTaskSnapshot(tasks[1], "done", False, "pending"),
                    SchedulerTaskSnapshot(tasks[2], "backlog", False, "not_required"),
                    SchedulerTaskSnapshot(tasks[3], "backlog", False, "not_required"),
                    SchedulerTaskSnapshot(tasks[4], "backlog", False, "not_required"),
                    SchedulerTaskSnapshot(tasks[5], "assigned", True, "not_required"),
                    SchedulerTaskSnapshot(tasks[6], "backlog", False, "not_required"),
                    SchedulerTaskSnapshot(tasks[7], "in_progress", False, "not_required"),
                ),
                decisions=(("D-1", "open"),),
                execution_mode="external",
                candidate_ids=("C", "D", "E", "F", "G"),
                snapshot_sha256="b" * 64,
            )

            reasons = {item.reason for item in build_task_readiness(snapshot).blocked}

            self.assertEqual(
                reasons,
                {
                    ReasonCode.DEPENDENCY_PENDING,
                    ReasonCode.TASK_INTEGRATION_PENDING,
                    ReasonCode.DECISION_OPEN,
                    ReasonCode.EXTERNAL_CLAIM_ACTIVE,
                    ReasonCode.CONFLICT_GROUP_ACTIVE,
                },
            )

    def test_objective_plan_and_projection_are_deterministic_and_path_free(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task = self._task(Path(temporary), "A")
            snapshot = SchedulerSnapshot(
                observed_at=datetime(2026, 8, 4, 8, 0, tzinfo=timezone.utc),
                tasks=(SchedulerTaskSnapshot(task, "backlog", False, "not_required"),),
                decisions=(),
                execution_mode="local",
                candidate_ids=("A",),
                snapshot_sha256="c" * 64,
                objective_id="release",
                objective_revision=1,
                objective_state="active",
                objective_scope=("A",),
                objective_targets=("A",),
                objective_requested_mode="supervised",
                objective_operations=("execute", "review"),
            )

            first = build_continuation_plan(snapshot)
            second = build_continuation_plan(snapshot)
            projection = build_scheduler_projection(snapshot, first)
            payload = projection_payload(projection)
            mermaid = render_projection_mermaid(projection)

            self.assertEqual(first.plan_sha256, second.plan_sha256)
            self.assertEqual(first.completion, PlanCompletion.INCOMPLETE)
            self.assertEqual(first.selected_actions[0].kind, ActionKind.EXECUTE_TASK)
            self.assertEqual([node["id"] for node in payload["nodes"]], ["action:execute_task:A", "objective:release", "task:A"])
            self.assertIn("flowchart LR", mermaid)
            self.assertNotIn(str(task.directory), mermaid)

    def test_snapshot_uses_its_clock_for_external_claim_liveness(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = self._task(root, "A")
            task.directory.mkdir()
            task.directory.joinpath("claim.json").write_text(
                '{"task_id":"A","runner":"agent","lease_expires_at":"2027-01-01T00:00:00+00:00"}',
                encoding="utf-8",
            )
            graph = TaskGraph(None, (task,), (task,), {}, "external")
            config = SimpleNamespace(policy=SimpleNamespace(execution_mode="external"))
            observed_at = datetime(2030, 1, 1, tzinfo=timezone.utc)
            with (
                patch("dyro.graph.build_task_graph", return_value=graph),
                patch("dyro.graph.validate_task_graph", return_value=()),
                patch("dyro.tasks.status", return_value="assigned"),
            ):
                snapshot = build_scheduler_snapshot(config, clock=lambda: observed_at)

            self.assertFalse(snapshot.tasks[0].external_claim_active)
            self.assertFalse(external_claim_active(task, now=observed_at))

    def test_objective_contract_is_sampled_once_per_task(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = self._task(root, "A")
            graph = TaskGraph(None, (task,), (task,), {}, "local")
            objective = Objective(
                schema_version=1,
                id="release",
                title="Release",
                line="alpha",
                targets=("A",),
                completion=CompletionRule.ALL_TARGETS_INTEGRATED,
                requested_mode=RequestedMode.SUPERVISED,
                operations=(Operation.EXECUTE,),
                budget=BudgetLimit(1, 1, 1, 1, 1),
            )
            record = SimpleNamespace(
                objective=objective,
                revision=1,
                operator_state="active",
                scope=("A",),
                task_contract_sha256=(("A", "a" * 64),),
            )
            config = SimpleNamespace(policy=SimpleNamespace(execution_mode="local"))
            with (
                patch("dyro.graph.build_task_graph", return_value=graph),
                patch("dyro.graph.validate_task_graph", return_value=()),
                patch("dyro.tasks.status", return_value="backlog"),
                patch("dyro.continuation.snapshot._task_contract_sha256", return_value="a" * 64) as digest,
            ):
                snapshot = build_scheduler_snapshot(config, objective=record)

            self.assertEqual(digest.call_count, 1)
            self.assertFalse(snapshot.objective_drifted)

    @staticmethod
    def _task(
        root: Path,
        task_id: str,
        *,
        depends_on: tuple[str, ...] = (),
        blocked_on: tuple[str, ...] = (),
        conflict_group: str = "",
    ) -> Task:
        return Task(
            id=task_id,
            title=task_id,
            line="alpha",
            risk="write",
            executor="noop",
            reviewer="noop",
            repositories=("api",),
            depends_on=depends_on,
            blocked_on=blocked_on,
            conflict_group=conflict_group,
            directory=root / task_id,
        )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from datetime import datetime, timezone
import tempfile
import unittest
from pathlib import Path

from dyro.continuation.attention import (
    build_attention_projection,
    attention_projection_payload,
)
from dyro.continuation.models import (
    ActionKind,
    AttentionItem,
    AttentionKind,
    BudgetLimit,
    ContinuationPlan,
    PlanCompletion,
    PlannedAction,
    ReasonCode,
)
from dyro.continuation.planner import (
    build_continuation_plan,
    build_scheduler_projection,
)
from dyro.continuation.snapshot import SchedulerSnapshot, SchedulerTaskSnapshot
from dyro.errors import ValidationError
from dyro.tasks import Task


class AttentionProjectionTests(unittest.TestCase):
    def test_projection_prioritizes_and_derives_safe_attention(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot = self._snapshot(root)
            plan = ContinuationPlan(
                objective_id="release",
                snapshot_sha256="a" * 64,
                plan_sha256="b" * 64,
                completion=PlanCompletion.INCOMPLETE,
                selected_actions=(
                    PlannedAction(
                        ActionKind.EXECUTE_TASK, "TASK-C", ReasonCode.TASK_READY
                    ),
                ),
                blocked=(
                    PlannedAction(
                        ActionKind.EXECUTE_TASK,
                        "TASK-D",
                        ReasonCode.DEPENDENCY_PENDING,
                        facts=(("dependency_id", "TASK-A"),),
                    ),
                ),
                attention=(
                    AttentionItem(
                        "raw-repair",
                        AttentionKind.REPAIR_REQUIRED,
                        "TASK-A",
                        ReasonCode.CONTRACT_DRIFT,
                    ),
                    AttentionItem(
                        "raw-needs",
                        AttentionKind.NEEDS_USER,
                        "TASK-B",
                        ReasonCode.ANSWER_REQUIRED,
                    ),
                    AttentionItem(
                        "raw-paused",
                        AttentionKind.PAUSED,
                        "release",
                        ReasonCode.OBJECTIVE_PAUSED,
                    ),
                ),
            )
            scheduler = build_scheduler_projection(snapshot, plan)

            projection = build_attention_projection(
                snapshot,
                plan,
                scheduler,
                budget=BudgetLimit(4, 2, 2, 2, 2),
            )
            payload = attention_projection_payload(projection)

            self.assertEqual(
                [item.kind for item in projection.items],
                [
                    AttentionKind.REPAIR_REQUIRED,
                    AttentionKind.NEEDS_USER,
                    AttentionKind.READY,
                    AttentionKind.PAUSED,
                    AttentionKind.WAITING,
                ],
            )
            self.assertEqual(
                [item.subject_id for item in projection.items],
                ["TASK-A", "TASK-B", "TASK-C", "release", "TASK-D"],
            )
            self.assertTrue(
                all(item.id.startswith("attention-") for item in projection.items)
            )
            self.assertEqual(payload["budget"]["max_parallel"], "2")
            self.assertEqual(
                dict(projection.items[-1].facts),
                {"has_pending_dependency": "true"},
            )
            self.assertNotIn(str(root), str(payload))

    def test_projection_is_deterministic_and_binds_the_scheduler_graph(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot = self._snapshot(root)
            plan = ContinuationPlan(
                objective_id="release",
                snapshot_sha256="a" * 64,
                plan_sha256="b" * 64,
                completion=PlanCompletion.INCOMPLETE,
                selected_actions=(
                    PlannedAction(ActionKind.WAIT, "release", ReasonCode.NO_PROGRESS),
                ),
            )
            scheduler = build_scheduler_projection(snapshot, plan)

            first = build_attention_projection(
                snapshot, plan, scheduler, budget=BudgetLimit(4, 2, 2, 2, 2)
            )
            second = build_attention_projection(
                snapshot, plan, scheduler, budget=BudgetLimit(4, 2, 2, 2, 2)
            )

            self.assertEqual(first, second)
            self.assertEqual(first.attention_sha256, second.attention_sha256)
            self.assertEqual(len(first.items), 1)
            self.assertEqual(first.items[0].kind, AttentionKind.WAITING)

    def test_unsafe_fact_is_rejected_instead_of_entering_public_projection(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot = self._snapshot(root)
            plan = ContinuationPlan(
                objective_id="release",
                snapshot_sha256="a" * 64,
                plan_sha256="b" * 64,
                completion=PlanCompletion.INCOMPLETE,
                blocked=(
                    PlannedAction(
                        ActionKind.EXECUTE_TASK,
                        "TASK-A",
                        ReasonCode.DEPENDENCY_PENDING,
                        facts=(("unsafe", "runner --token=do-not-disclose"),),
                    ),
                ),
            )
            scheduler = build_scheduler_projection(snapshot, plan)

            with self.assertRaisesRegex(ValidationError, "事实"):
                build_attention_projection(
                    snapshot, plan, scheduler, budget=BudgetLimit(4, 2, 2, 2, 2)
                )

    def test_conflict_value_is_redacted_to_a_boolean_fact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot = self._snapshot(root)
            secret_conflict_value = "runner-token-do-not-disclose"
            plan = ContinuationPlan(
                objective_id="release",
                snapshot_sha256="a" * 64,
                plan_sha256="b" * 64,
                completion=PlanCompletion.INCOMPLETE,
                blocked=(
                    PlannedAction(
                        ActionKind.EXECUTE_TASK,
                        "TASK-A",
                        ReasonCode.CONFLICT_GROUP_ACTIVE,
                        facts=(("conflict_group", secret_conflict_value),),
                    ),
                ),
            )
            scheduler = build_scheduler_projection(snapshot, plan)

            projection = build_attention_projection(
                snapshot,
                plan,
                scheduler,
                budget=BudgetLimit(4, 2, 2, 2, 2),
            )
            payload = attention_projection_payload(projection)

            self.assertEqual(payload["items"][0]["facts"], {"has_conflict": "true"})
            self.assertNotIn(secret_conflict_value, str(payload))

    def test_planner_open_decision_is_redacted_without_rejecting_attention(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            secret_decision_id = "decision-token-do-not-disclose"
            task = Task(
                id="TASK-A",
                title="TASK-A",
                line="alpha",
                risk="write",
                executor="noop",
                reviewer="noop",
                repositories=("api",),
                blocked_on=(secret_decision_id,),
                directory=root / "TASK-A",
            )
            snapshot = SchedulerSnapshot(
                observed_at=datetime(2026, 8, 4, 8, 0, tzinfo=timezone.utc),
                tasks=(SchedulerTaskSnapshot(task, "backlog", False, "not_required"),),
                decisions=((secret_decision_id, "open"),),
                execution_mode="local",
                candidate_ids=("TASK-A",),
                snapshot_sha256="a" * 64,
                objective_id="release",
                objective_revision=1,
                objective_state="active",
                objective_scope=("TASK-A",),
                objective_targets=("TASK-A",),
                objective_requested_mode="supervised",
                objective_operations=("execute", "review"),
            )
            plan = build_continuation_plan(snapshot)
            scheduler = build_scheduler_projection(snapshot, plan)

            projection = build_attention_projection(
                snapshot,
                plan,
                scheduler,
                budget=BudgetLimit(4, 2, 2, 2, 2),
            )
            payload = attention_projection_payload(projection)

            self.assertEqual(projection.items[0].kind, AttentionKind.NEEDS_USER)
            self.assertEqual(
                payload["items"][0]["facts"], {"has_open_decision": "true"}
            )
            self.assertNotIn(secret_decision_id, str(payload))

    @staticmethod
    def _snapshot(root: Path) -> SchedulerSnapshot:
        tasks = tuple(
            Task(
                id=task_id,
                title=task_id,
                line="alpha",
                risk="write",
                executor="noop",
                reviewer="noop",
                repositories=("api",),
                directory=root / task_id,
            )
            for task_id in ("TASK-A", "TASK-B", "TASK-C", "TASK-D")
        )
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
            objective_targets=("TASK-A",),
            objective_requested_mode="supervised",
            objective_operations=("execute", "review"),
        )


if __name__ == "__main__":
    unittest.main()

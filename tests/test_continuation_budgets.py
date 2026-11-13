from __future__ import annotations

from datetime import datetime, timezone
import unittest

from dyro.continuation.budgets import (
    BudgetCaps,
    BudgetDecisionInput,
    BudgetReason,
    BudgetRequest,
    BudgetReservation,
    BudgetUsage,
    ProgressFacts,
    decide_no_progress,
    decide_budget,
    progress_fingerprint,
)
from dyro.continuation.models import BudgetLimit


class ContinuationBudgetTests(unittest.TestCase):
    def test_workspace_reservations_and_local_caps_are_intersected(self) -> None:
        requested = BudgetLimit(10, 3, 4, 3, 4)
        workspace = BudgetCaps(max_actions=2, max_parallel=1)
        activation = BudgetCaps(max_actions=2, max_parallel=2)
        usage = BudgetUsage(actions=1, attempts_by_task=(("TASK-A", 1),))
        reservations = (
            BudgetReservation("other", "TASK-B", actions=1, attempts=1, parallel=1),
        )
        decision = decide_budget(
            BudgetDecisionInput(
                objective_id="release",
                requested=requested,
                workspace=workspace,
                activation=activation,
                usage=usage,
                workspace_usage=BudgetUsage(actions=1),
                reservations=reservations,
                now=datetime(2026, 8, 4, tzinfo=timezone.utc),
                request=BudgetRequest("TASK-A"),
            )
        )

        self.assertFalse(decision.allowed)
        self.assertIn(BudgetReason.ACTION_LIMIT, decision.reasons)
        self.assertIn(BudgetReason.PARALLEL_LIMIT, decision.reasons)
        self.assertEqual(decision.effective.max_actions, 2)
        self.assertEqual(decision.effective.max_parallel, 1)

    def test_deadline_clock_and_untrusted_provider_usage_fail_closed_for_automatic(self) -> None:
        decision = decide_budget(
            BudgetDecisionInput(
                objective_id="release",
                requested=BudgetLimit(5, 2, 2, 2, 1, datetime(2026, 8, 4, tzinfo=timezone.utc)),
                workspace=BudgetCaps(max_provider_usage=100),
                activation=None,
                usage=BudgetUsage(provider_usage=10, provider_usage_trusted=False),
                workspace_usage=BudgetUsage(provider_usage=10, provider_usage_trusted=False),
                reservations=(),
                now=datetime(2026, 8, 4, tzinfo=timezone.utc),
                last_observed_at=datetime(2026, 8, 5, tzinfo=timezone.utc),
                automatic=True,
                request=BudgetRequest("TASK-A", provider_usage=1),
            )
        )

        self.assertFalse(decision.allowed)
        self.assertIn(BudgetReason.DEADLINE_EXCEEDED, decision.reasons)
        self.assertIn(BudgetReason.CLOCK_ROLLBACK, decision.reasons)
        self.assertIn(BudgetReason.PROVIDER_USAGE_UNTRUSTED, decision.reasons)

    def test_progress_fingerprint_ignores_trigger_churn_but_tracks_delivery_facts(self) -> None:
        initial = ProgressFacts(
            task_states=(("TASK-A", "review"),),
            integration_heads=(("TASK-A", "abc"),),
            decisions=(("D-1", "open"),),
            effective_evidence=(("TASK-A", "receipt-1"),),
            trigger_observations=(("time_due", "one"),),
        )
        trigger_only = ProgressFacts(
            task_states=initial.task_states,
            integration_heads=initial.integration_heads,
            decisions=initial.decisions,
            effective_evidence=initial.effective_evidence,
            trigger_observations=(("time_due", "two"),),
        )
        delivered = ProgressFacts(
            task_states=(("TASK-A", "done"),),
            integration_heads=initial.integration_heads,
            decisions=initial.decisions,
            effective_evidence=initial.effective_evidence,
        )

        self.assertEqual(progress_fingerprint(initial), progress_fingerprint(trigger_only))
        self.assertNotEqual(progress_fingerprint(initial), progress_fingerprint(delivered))

    def test_no_progress_resets_only_after_delivery_and_decisions_are_deterministic(self) -> None:
        initial = ProgressFacts(task_states=(("TASK-A", "review"),))
        no_change = decide_no_progress(
            previous_fingerprint=progress_fingerprint(initial),
            previous_cycles=1,
            current=ProgressFacts(
                task_states=initial.task_states,
                trigger_observations=(("manual_signal", "seen"),),
            ),
            maximum=2,
        )
        progressed = decide_no_progress(
            previous_fingerprint=progress_fingerprint(initial),
            previous_cycles=1,
            current=ProgressFacts(task_states=(("TASK-A", "done"),)),
            maximum=2,
        )
        input = BudgetDecisionInput(
            objective_id="release",
            requested=BudgetLimit(5, 2, 2, 2, 2),
            workspace=BudgetCaps(max_actions=5, max_parallel=2),
            activation=None,
            usage=BudgetUsage(),
            workspace_usage=BudgetUsage(),
            reservations=(),
            now=datetime(2026, 8, 4, tzinfo=timezone.utc),
            request=BudgetRequest("TASK-A"),
        )

        self.assertEqual((no_change.cycles, no_change.reset, no_change.exhausted), (2, False, True))
        self.assertEqual((progressed.cycles, progressed.reset, progressed.exhausted), (0, True, False))
        self.assertEqual(decide_budget(input), decide_budget(input))
        workspace_stop = decide_budget(
            BudgetDecisionInput(
                objective_id="release",
                requested=BudgetLimit(5, 2, 2, 2, 2),
                workspace=BudgetCaps(max_no_progress_cycles=1),
                activation=None,
                usage=BudgetUsage(),
                workspace_usage=BudgetUsage(no_progress_cycles=1),
                reservations=(),
                now=datetime(2026, 8, 4, tzinfo=timezone.utc),
                request=BudgetRequest("TASK-A"),
            )
        )
        self.assertIn(BudgetReason.NO_PROGRESS_LIMIT, workspace_stop.reasons)

    def test_shared_failure_state_and_provider_truth_are_fail_closed(self) -> None:
        failure_stop = decide_budget(
            BudgetDecisionInput(
                objective_id="release",
                requested=BudgetLimit(5, 2, 5, 5, 2),
                workspace=BudgetCaps(),
                activation=BudgetCaps(max_consecutive_failures=1),
                usage=BudgetUsage(),
                workspace_usage=BudgetUsage(),
                reservations=(BudgetReservation("release", "TASK-A", failures=1),),
                now=datetime(2026, 8, 4, tzinfo=timezone.utc),
                request=BudgetRequest("TASK-A", failures=1),
            )
        )
        workspace_stop = decide_budget(
            BudgetDecisionInput(
                objective_id="release",
                requested=BudgetLimit(5, 2, 5, 5, 2),
                workspace=BudgetCaps(max_no_progress_cycles=1),
                activation=None,
                usage=BudgetUsage(),
                workspace_usage=BudgetUsage(no_progress_cycles=1),
                reservations=(),
                now=datetime(2026, 8, 4, tzinfo=timezone.utc),
                request=BudgetRequest("TASK-A"),
            )
        )

        self.assertIn(BudgetReason.CONSECUTIVE_FAILURE_LIMIT, failure_stop.reasons)
        self.assertIn(BudgetReason.NO_PROGRESS_LIMIT, workspace_stop.reasons)
        with self.assertRaisesRegex(TypeError, "必须是 bool"):
            BudgetUsage(provider_usage_trusted="false")  # type: ignore[arg-type]

    def test_hard_provider_cap_requires_a_trusted_action_reservation(self) -> None:
        base = dict(
            objective_id="release",
            requested=BudgetLimit(5, 2, 5, 5, 2),
            workspace=BudgetCaps(max_provider_usage=10),
            activation=None,
            usage=BudgetUsage(provider_usage_trusted=True),
            workspace_usage=BudgetUsage(provider_usage_trusted=True),
            reservations=(),
            now=datetime(2026, 8, 4, tzinfo=timezone.utc),
        )
        unknown = decide_budget(BudgetDecisionInput(request=BudgetRequest("TASK-A"), automatic=True, **base))
        explicit_zero = decide_budget(
            BudgetDecisionInput(
                request=BudgetRequest("TASK-A", provider_usage=0, provider_usage_trusted=True),
                automatic=True,
                **base,
            )
        )

        self.assertIn(BudgetReason.PROVIDER_USAGE_UNTRUSTED, unknown.reasons)
        self.assertTrue(explicit_zero.allowed)
        self.assertFalse(BudgetUsage().provider_usage_trusted)
        with self.assertRaisesRegex(TypeError, "automatic 必须是 bool"):
            BudgetDecisionInput(request=BudgetRequest("TASK-A"), automatic=0, **base)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()

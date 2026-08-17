from __future__ import annotations

import unittest

from dyro.continuation.briefing import (
    EMPTY_ATTENTION,
    INVENTORY_MATTER,
    UNREAD_MATTER,
    briefing_payload,
    follow_up_argv,
    inventory_briefing,
    matter_for,
    primary_attention,
    render_briefing_text,
    unread_briefing,
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


def _plan(
    *attention: AttentionItem,
    completion: PlanCompletion = PlanCompletion.INCOMPLETE,
    selected: tuple[PlannedAction, ...] = (),
) -> ContinuationPlan:
    return ContinuationPlan(
        objective_id="release",
        snapshot_sha256="a" * 64,
        plan_sha256="b" * 64,
        completion=completion,
        selected_actions=selected,
        attention=attention,
    )


class BriefingProjectionTests(unittest.TestCase):
    def test_primary_attention_ranks_repair_ahead_of_ready(self) -> None:
        plan = _plan(
            AttentionItem(
                "ready", AttentionKind.READY, "TASK-B", ReasonCode.TASK_READY
            ),
            AttentionItem(
                "repair",
                AttentionKind.REPAIR_REQUIRED,
                "TASK-A",
                ReasonCode.CONTRACT_DRIFT,
            ),
        )
        item = primary_attention(plan)
        assert item is not None
        self.assertEqual(item.kind, AttentionKind.REPAIR_REQUIRED)
        self.assertEqual(item.subject_id, "TASK-A")

    def test_empty_attention_is_unread_not_done(self) -> None:
        plan = _plan()
        self.assertIsNone(primary_attention(plan))
        self.assertEqual(matter_for(plan), EMPTY_ATTENTION)
        payload = briefing_payload(
            plan, command="dyro --workspace demo objective attention release"
        )
        self.assertEqual(payload["matter"], EMPTY_ATTENTION)
        self.assertNotIn("没有事情", payload["matter"])
        self.assertNotIn("全部正常", "".join(payload["lines"]))

    def test_follow_up_is_read_only_tick_or_attention(self) -> None:
        ready = _plan(
            AttentionItem(
                "ready", AttentionKind.READY, "TASK-A", ReasonCode.TASK_READY
            )
        )
        self.assertEqual(
            follow_up_argv(ready), ("objective", "tick", "release")
        )
        selected_ready = _plan(
            selected=(
                PlannedAction(
                    ActionKind.EXECUTE_TASK, "TASK-A", ReasonCode.TASK_READY
                ),
            )
        )
        self.assertEqual(
            follow_up_argv(selected_ready), ("objective", "tick", "release")
        )
        self.assertEqual(matter_for(selected_ready), "有任务可以继续做（TASK-A）")
        needs_user = _plan(
            AttentionItem(
                "ask",
                AttentionKind.NEEDS_USER,
                "TASK-A",
                ReasonCode.ANSWER_REQUIRED,
            ),
            selected=(
                PlannedAction(
                    ActionKind.EXECUTE_TASK, "TASK-B", ReasonCode.TASK_READY
                ),
            ),
        )
        self.assertEqual(
            follow_up_argv(needs_user), ("objective", "attention", "release")
        )

    def test_payload_is_path_free_and_uses_supplied_command(self) -> None:
        plan = _plan(
            AttentionItem(
                "ready", AttentionKind.READY, "TASK-A", ReasonCode.TASK_READY
            )
        )
        command = "dyro --workspace demo objective explain release"
        payload = briefing_payload(plan, command=command, title="Release")
        blob = render_briefing_text(payload)
        self.assertTrue(payload["available"])
        self.assertEqual(payload["command"], command)
        self.assertEqual(payload["reason"], "TASK_READY")
        self.assertIn("有任务可以继续做（TASK-A）", payload["matter"])
        self.assertIn("Release · 未完成", blob)
        self.assertIn(command, blob)
        self.assertNotIn("/Users/", blob)
        self.assertNotIn("session", blob.lower())

    def test_unread_and_inventory_do_not_claim_idle(self) -> None:
        unread = unread_briefing("dyro --workspace demo objective list")
        inventory = inventory_briefing("dyro --workspace demo objective list", 2)
        self.assertFalse(unread["available"])
        self.assertEqual(unread["matter"], UNREAD_MATTER)
        self.assertIn("未知", unread["lines"][1])
        self.assertTrue(inventory["available"])
        self.assertEqual(inventory["matter"], INVENTORY_MATTER)
        self.assertIn("2 个未停止的目标", inventory["lines"][0])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest

from dyro.continuation.briefing import (
    ATTENTION_CLOSER,
    EMPTY_ATTENTION,
    INVENTORY_MATTER,
    TICK_CLOSER,
    UNREAD_MATTER,
    arrival_lines,
    briefing_payload,
    describe_action,
    follow_up_argv,
    follow_up_from_kind,
    inventory_briefing,
    matter_for,
    primary_attention,
    render_briefing_text,
    render_human_attention,
    render_human_wave,
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
        self.assertEqual(
            follow_up_from_kind("ready", "release"),
            ("objective", "tick", "release"),
        )
        self.assertEqual(
            follow_up_from_kind("needs_user", "release"),
            ("objective", "attention", "release"),
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

    def test_arrival_does_not_repeat_the_follow_up_command(self) -> None:
        plan = _plan(
            AttentionItem(
                "ready", AttentionKind.READY, "TASK-A", ReasonCode.TASK_READY
            )
        )
        lines = arrival_lines(plan, "Release", TICK_CLOSER)
        blob = "\n".join(lines)
        self.assertEqual(lines[0], "Release · 未完成")
        self.assertEqual(lines[1], "有任务可以继续做（TASK-A）")
        self.assertEqual(lines[2], TICK_CLOSER)
        self.assertNotIn("下一步：", blob)
        self.assertNotIn("objective tick", blob)
        self.assertNotIn("objective attention", blob)
        self.assertNotIn("session", blob.lower())

    def test_human_wave_and_attention_use_reason_labels(self) -> None:
        action = PlannedAction(
            ActionKind.EXECUTE_TASK, "TASK-A", ReasonCode.TASK_READY
        )
        self.assertEqual(describe_action(action), "执行 · 有任务可以继续做（TASK-A）")
        self.assertEqual(
            render_human_wave((action,)),
            ["本轮可以推进：", "- 执行 · 有任务可以继续做（TASK-A）"],
        )
        self.assertEqual(render_human_wave(()), ["本轮没有可推进的写入。"])
        self.assertEqual(
            render_human_attention(((ReasonCode.ANSWER_REQUIRED, "TASK-A"),)),
            ["需要关注：", "- 需要你回答一个问题（TASK-A）"],
        )
        self.assertEqual(render_human_attention(()), [EMPTY_ATTENTION])
        attention_lines = arrival_lines(
            _plan(
                AttentionItem(
                    "ask",
                    AttentionKind.NEEDS_USER,
                    "TASK-A",
                    ReasonCode.ANSWER_REQUIRED,
                )
            ),
            "Release",
            ATTENTION_CLOSER,
        )
        self.assertEqual(attention_lines[2], ATTENTION_CLOSER)
        self.assertNotIn("objective apply", "\n".join(attention_lines))


if __name__ == "__main__":
    unittest.main()

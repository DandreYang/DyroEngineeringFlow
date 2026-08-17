"""Path-free switch-tool briefing. Projection only; not a session layer."""

from __future__ import annotations

from .models import (
    ActionKind,
    AttentionItem,
    AttentionKind,
    ContinuationPlan,
    PlanCompletion,
    PlannedAction,
    ReasonCode,
)

EMPTY_ATTENTION = "摘要未列出关注项"
UNREAD_MATTER = "目标简报未读到"
INVENTORY_MATTER = "有多个未停止的目标，换工具后先读其中一个的权威简报。"

_KIND_RANK = {
    AttentionKind.REPAIR_REQUIRED: 0,
    AttentionKind.NEEDS_USER: 1,
    AttentionKind.READY: 2,
    AttentionKind.WAITING: 3,
    AttentionKind.PAUSED: 4,
}

_HUMAN_KINDS = frozenset(
    {AttentionKind.REPAIR_REQUIRED, AttentionKind.NEEDS_USER}
)
_READY_ACTIONS = frozenset(
    {ActionKind.EXECUTE_TASK, ActionKind.REVIEW_TASK, ActionKind.MERGE_TASK}
)

_REASON_LABELS = {
    ReasonCode.TASK_READY: "有任务可以继续做",
    ReasonCode.TASK_REVIEW_READY: "有任务等你复核",
    ReasonCode.DEPENDENCY_PENDING: "还在等依赖完成",
    ReasonCode.DECISION_OPEN: "有个决定还没做",
    ReasonCode.ANSWER_REQUIRED: "需要你回答一个问题",
    ReasonCode.EXTERNAL_CLAIM_ACTIVE: "这条任务还被别人占着",
    ReasonCode.CONFLICT_GROUP_ACTIVE: "和另一条任务冲突，还不能并行",
    ReasonCode.TASK_INTEGRATION_PENDING: "做完了，还没合入",
    ReasonCode.TASK_FAILED: "有任务失败了",
    ReasonCode.TRIGGER_NOT_DUE: "还没到下次检查时间",
    ReasonCode.BUDGET_EXHAUSTED: "这轮预算用完了",
    ReasonCode.NO_PROGRESS: "连续几轮没有进展",
    ReasonCode.CONTRACT_DRIFT: "目标和实际状态对不上",
    ReasonCode.ACTION_UNCERTAIN: "下一步还不明确",
    ReasonCode.TARGETS_INTEGRATED: "目标已经合入",
    ReasonCode.OBJECTIVE_SCOPE_CONFLICT: "目标和范围冲突",
    ReasonCode.OBJECTIVE_PAUSED: "这个目标已暂停",
    ReasonCode.ACTIVATION_REQUIRED: "需要你确认后才能继续",
    ReasonCode.POLICY_DISALLOWS_OPERATION: "当前策略不允许这一步",
    ReasonCode.PROOF_DECAYED: "证据已衰减，需要重新核对",
}

_COMPLETION_LABELS = {
    PlanCompletion.INCOMPLETE: "未完成",
    PlanCompletion.COMPLETE: "已完成",
    PlanCompletion.REPAIR_REQUIRED: "需要修复",
}


def primary_attention(plan: ContinuationPlan) -> AttentionItem | None:
    """Return the highest-priority attention item, or None if the plan lists none."""
    if not plan.attention:
        return None
    return min(
        plan.attention,
        key=lambda item: (
            _KIND_RANK.get(item.kind, len(_KIND_RANK)),
            item.subject_id,
            item.reason.value,
            item.id,
        ),
    )


def _ready_action(plan: ContinuationPlan) -> PlannedAction | None:
    for action in plan.selected_actions:
        if action.kind in _READY_ACTIONS:
            return action
    return None


def follow_up_argv(plan: ContinuationPlan) -> tuple[str, ...]:
    """Return one read-only follow-up. Never apply, dispatch, or resume a chat."""
    item = primary_attention(plan)
    if item is not None and item.kind in _HUMAN_KINDS:
        return ("objective", "attention", plan.objective_id)
    if item is not None and item.kind is AttentionKind.READY:
        return ("objective", "tick", plan.objective_id)
    if _ready_action(plan) is not None:
        return ("objective", "tick", plan.objective_id)
    return ("objective", "attention", plan.objective_id)


def reason_label(reason: ReasonCode) -> str:
    return _REASON_LABELS.get(reason, reason.value)


def _matter_line(label: str, subject_id: str, objective_id: str) -> str:
    if subject_id and subject_id != objective_id:
        return f"{label}（{subject_id}）"
    return label


def matter_for(plan: ContinuationPlan) -> str:
    item = primary_attention(plan)
    if item is not None:
        return _matter_line(reason_label(item.reason), item.subject_id, plan.objective_id)
    action = _ready_action(plan)
    if action is not None:
        return _matter_line(
            reason_label(action.reason), action.subject_id, plan.objective_id
        )
    if plan.completion is PlanCompletion.COMPLETE:
        return _REASON_LABELS[ReasonCode.TARGETS_INTEGRATED]
    if plan.completion is PlanCompletion.REPAIR_REQUIRED:
        return _REASON_LABELS[ReasonCode.CONTRACT_DRIFT]
    return EMPTY_ATTENTION


def _focus(plan: ContinuationPlan) -> tuple[str, str, str]:
    item = primary_attention(plan)
    if item is not None:
        return item.kind.value, item.reason.value, item.subject_id
    action = _ready_action(plan)
    if action is not None:
        return AttentionKind.READY.value, action.reason.value, action.subject_id
    return "", "", ""


def briefing_payload(
    plan: ContinuationPlan,
    *,
    command: str,
    title: str = "",
) -> dict[str, object]:
    """Build a stable, path-free briefing. `command` must already be scoped."""
    kind, reason, subject_id = _focus(plan)
    heading = title.strip() or plan.objective_id
    completion = _COMPLETION_LABELS[plan.completion]
    matter = matter_for(plan)
    lines = (f"{heading} · {completion}", matter, f"下一步：{command}")
    return {
        "available": True,
        "objective_id": plan.objective_id,
        "title": heading,
        "completion": plan.completion.value,
        "kind": kind,
        "reason": reason,
        "subject_id": subject_id,
        "matter": matter,
        "command": command,
        "lines": list(lines),
    }


def unread_briefing(command: str) -> dict[str, object]:
    """Fail closed: unread is unknown, not 'nothing to do'."""
    lines = (UNREAD_MATTER, "关注项未知，不是没有事情", f"下一步：{command}")
    return {
        "available": False,
        "objective_id": "",
        "title": "",
        "completion": "",
        "kind": "",
        "reason": "",
        "subject_id": "",
        "matter": UNREAD_MATTER,
        "command": command,
        "lines": list(lines),
    }


def inventory_briefing(command: str, count: int) -> dict[str, object]:
    """Several live Objectives: do not pick one silently."""
    heading = f"有 {count} 个未停止的目标"
    lines = (heading, INVENTORY_MATTER, f"下一步：{command}")
    return {
        "available": True,
        "objective_id": "",
        "title": "",
        "completion": "",
        "kind": "",
        "reason": "",
        "subject_id": "",
        "matter": INVENTORY_MATTER,
        "command": command,
        "lines": list(lines),
    }


def render_briefing_text(payload: dict[str, object]) -> str:
    lines = payload.get("lines")
    if not isinstance(lines, list):
        return ""
    return "\n".join(item for item in lines if isinstance(item, str) and item)

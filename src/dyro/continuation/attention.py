"""Path-free, deterministic Attention projections for continuation readers.

The projection is deliberately read-only.  It consumes one already-sampled
SchedulerSnapshot and its matching plan/graph projection; it never looks up a
workspace path, Action Journal, environment value, or localized display text.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Iterable

from ..canonical import canonical_json_bytes
from ..config import validate_id
from ..errors import ValidationError
from .models import (
    ActionKind,
    AttentionKind,
    BudgetLimit,
    ContinuationPlan,
    PlannedAction,
    ReasonCode,
    SchedulerReadProjection,
)
from .planner import projection_payload
from .snapshot import SchedulerSnapshot


_KIND_PRIORITY = {
    AttentionKind.REPAIR_REQUIRED: 0,
    AttentionKind.NEEDS_USER: 1,
    AttentionKind.READY: 2,
    AttentionKind.PAUSED: 3,
    AttentionKind.WAITING: 4,
}
_SAFE_FACT_KEY = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_SAFE_FACT_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._,:-]{0,127}$")
_SAFE_FACT_KEYS = frozenset(
    {
        "active_task_ids",
        "dependency_id",
        "dependency_status",
        "has_conflict",
        "has_open_decision",
        "has_pending_dependency",
        "objective_revision",
        "operation",
        "operator_state",
        "requested_mode",
        "status",
    }
)
_BUDGET_KEYS = frozenset(
    {
        "max_actions",
        "max_attempts_per_task",
        "max_failures",
        "max_no_progress_cycles",
        "max_parallel",
    }
)
_MUTATION_ACTIONS = frozenset(
    {ActionKind.EXECUTE_TASK, ActionKind.REVIEW_TASK, ActionKind.MERGE_TASK}
)


def _utc(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise TypeError(f"{label} 必须是带时区的 datetime")
    return value.astimezone(timezone.utc)


def _safe_facts(facts: Iterable[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
    items = tuple(facts)
    if len(items) > 16:
        raise ValidationError("Attention 事实数量超过上限")
    validated: list[tuple[str, str]] = []
    for item in items:
        if not isinstance(item, tuple) or len(item) != 2:
            raise ValidationError("Attention 事实结构无效")
        key, value = item
        if key == "conflict_group":
            validated.append(("has_conflict", "true"))
            continue
        if key == "decision_ids":
            validated.append(("has_open_decision", "true"))
            continue
        if key == "dependency_id":
            validated.append(("has_pending_dependency", "true"))
            continue
        if (
            not isinstance(key, str)
            or not isinstance(value, str)
            or key not in _SAFE_FACT_KEYS
            or not _SAFE_FACT_KEY.fullmatch(key)
            or not _SAFE_FACT_VALUE.fullmatch(value)
        ):
            raise ValidationError("Attention 事实不在安全白名单内")
        validated.append((key, value))
    if len({key for key, _ in validated}) != len(validated):
        raise ValidationError("Attention 事实不能重复")
    return tuple(sorted(validated))


def _budget_items(value: Iterable[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
    items = tuple(value)
    if len(items) != len(_BUDGET_KEYS):
        raise TypeError("AttentionReadProjection.budget 字段无效")
    validated: list[tuple[str, str]] = []
    for item in items:
        if not isinstance(item, tuple) or len(item) != 2:
            raise TypeError("AttentionReadProjection.budget 字段无效")
        key, raw = item
        if (
            not isinstance(key, str)
            or not isinstance(raw, str)
            or key not in _BUDGET_KEYS
            or not raw.isdecimal()
            or int(raw) < 1
        ):
            raise TypeError("AttentionReadProjection.budget 字段无效")
        validated.append((key, raw))
    if set(dict(validated)) != _BUDGET_KEYS or len(dict(validated)) != len(validated):
        raise TypeError("AttentionReadProjection.budget 字段无效")
    return tuple(sorted(validated))


def _attention_id(objective_id: str, subject_id: str, reason: ReasonCode) -> str:
    validate_id(objective_id, "Objective ID")
    validate_id(subject_id, "Attention subject ID")
    digest = hashlib.sha256(
        canonical_json_bytes(
            {
                "schema_version": 1,
                "objective_id": objective_id,
                "subject_id": subject_id,
                "reason": reason.value,
            }
        )
    ).hexdigest()
    return f"attention-{digest[:32]}"


@dataclass(frozen=True)
class AttentionReadItem:
    """A safe, locale-free concern shown by any continuation presentation."""

    id: str
    kind: AttentionKind
    subject_id: str
    reason: ReasonCode
    facts: tuple[tuple[str, str], ...] = ()
    action_kind: ActionKind | None = None

    def __post_init__(self) -> None:
        validate_id(self.id, "Attention ID")
        validate_id(self.subject_id, "Attention subject ID")
        if not isinstance(self.kind, AttentionKind):
            raise TypeError("AttentionReadItem.kind 必须是 AttentionKind")
        if not isinstance(self.reason, ReasonCode):
            raise TypeError("AttentionReadItem.reason 必须是 ReasonCode")
        if self.action_kind is not None and not isinstance(
            self.action_kind, ActionKind
        ):
            raise TypeError("AttentionReadItem.action_kind 必须是 ActionKind 或 None")
        object.__setattr__(self, "facts", _safe_facts(self.facts))


def _item_payload(item: AttentionReadItem) -> dict[str, object]:
    return {
        "id": item.id,
        "kind": item.kind.value,
        "subject_id": item.subject_id,
        "reason": item.reason.value,
        "facts": dict(item.facts),
        "action_kind": None if item.action_kind is None else item.action_kind.value,
    }


def _budget_payload(budget: BudgetLimit) -> dict[str, str]:
    return {
        "max_actions": str(budget.max_actions),
        "max_attempts_per_task": str(budget.max_attempts_per_task),
        "max_failures": str(budget.max_failures),
        "max_no_progress_cycles": str(budget.max_no_progress_cycles),
        "max_parallel": str(budget.max_parallel),
    }


@dataclass(frozen=True)
class AttentionReadProjection:
    """A stable, path-free Objective attention projection."""

    schema_version: int
    objective_id: str
    objective_revision: int
    snapshot_sha256: str
    plan_sha256: str
    graph_sha256: str
    attention_sha256: str
    budget: tuple[tuple[str, str], ...]
    next_wake_at: datetime | None = None
    items: tuple[AttentionReadItem, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise TypeError("AttentionReadProjection 仅支持 schema_version = 1")
        validate_id(self.objective_id, "Objective ID")
        if type(self.objective_revision) is not int or self.objective_revision < 1:
            raise TypeError("AttentionReadProjection Objective revision 无效")
        for digest, label in (
            (self.snapshot_sha256, "snapshot_sha256"),
            (self.plan_sha256, "plan_sha256"),
            (self.graph_sha256, "graph_sha256"),
        ):
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)
            ):
                raise TypeError(
                    f"AttentionReadProjection.{label} 必须是 SHA-256 十六进制摘要"
                )
        budget = _budget_items(self.budget)
        items = tuple(self.items)
        if not all(isinstance(item, AttentionReadItem) for item in items):
            raise TypeError(
                "AttentionReadProjection.items 必须只包含 AttentionReadItem"
            )
        if tuple(sorted(items, key=_attention_sort_key)) != items:
            raise TypeError("AttentionReadProjection.items 必须按固定优先级排序")
        if len({item.id for item in items}) != len(items):
            raise TypeError("AttentionReadProjection.items 不能重复")
        if self.next_wake_at is not None:
            object.__setattr__(
                self, "next_wake_at", _utc(self.next_wake_at, "next_wake_at")
            )
        expected = _attention_sha256(
            objective_id=self.objective_id,
            objective_revision=self.objective_revision,
            snapshot_sha256=self.snapshot_sha256,
            plan_sha256=self.plan_sha256,
            graph_sha256=self.graph_sha256,
            budget=budget,
            next_wake_at=self.next_wake_at,
            items=items,
        )
        if self.attention_sha256 and self.attention_sha256 != expected:
            raise ValidationError(
                "AttentionReadProjection.attention_sha256 与内容不匹配"
            )
        object.__setattr__(self, "budget", budget)
        object.__setattr__(self, "items", items)
        object.__setattr__(self, "attention_sha256", expected)


def _attention_sort_key(item: AttentionReadItem) -> tuple[int, str]:
    return (_KIND_PRIORITY[item.kind], item.id)


def _attention_sha256(
    *,
    objective_id: str,
    objective_revision: int,
    snapshot_sha256: str,
    plan_sha256: str,
    graph_sha256: str,
    budget: tuple[tuple[str, str], ...],
    next_wake_at: datetime | None,
    items: Iterable[AttentionReadItem],
) -> str:
    payload = {
        "schema_version": 1,
        "objective_id": objective_id,
        "objective_revision": objective_revision,
        "snapshot_sha256": snapshot_sha256,
        "plan_sha256": plan_sha256,
        "graph_sha256": graph_sha256,
        "budget": dict(budget),
        "next_wake_at": None if next_wake_at is None else next_wake_at.isoformat(),
        "items": [_item_payload(item) for item in items],
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _kind_for_selected(action: PlannedAction) -> AttentionKind | None:
    if action.kind in _MUTATION_ACTIONS:
        return AttentionKind.READY
    if action.kind is ActionKind.ASK_USER:
        return AttentionKind.NEEDS_USER
    if action.kind is ActionKind.PAUSE:
        return AttentionKind.PAUSED
    if action.kind is ActionKind.WAIT:
        return AttentionKind.WAITING
    if action.kind is ActionKind.REPAIR_REQUIRED:
        return AttentionKind.REPAIR_REQUIRED
    return None


def _kind_for_blocked(action: PlannedAction) -> AttentionKind:
    if action.reason in {ReasonCode.CONTRACT_DRIFT, ReasonCode.ACTION_UNCERTAIN}:
        return AttentionKind.REPAIR_REQUIRED
    if action.reason in {
        ReasonCode.ANSWER_REQUIRED,
        ReasonCode.DECISION_OPEN,
        ReasonCode.TASK_FAILED,
        ReasonCode.OBJECTIVE_SCOPE_CONFLICT,
        ReasonCode.ACTIVATION_REQUIRED,
        ReasonCode.POLICY_DISALLOWS_OPERATION,
    }:
        return AttentionKind.NEEDS_USER
    if action.reason in {
        ReasonCode.BUDGET_EXHAUSTED,
        ReasonCode.NO_PROGRESS,
        ReasonCode.OBJECTIVE_PAUSED,
    }:
        return AttentionKind.PAUSED
    return AttentionKind.WAITING


def _from_action(
    objective_id: str,
    action: PlannedAction,
    *,
    kind: AttentionKind,
) -> AttentionReadItem:
    return AttentionReadItem(
        id=_attention_id(objective_id, action.subject_id, action.reason),
        kind=kind,
        subject_id=action.subject_id,
        reason=action.reason,
        facts=action.facts,
        action_kind=action.kind,
    )


def build_attention_projection(
    snapshot: SchedulerSnapshot,
    plan: ContinuationPlan,
    scheduler: SchedulerReadProjection,
    *,
    budget: BudgetLimit,
) -> AttentionReadProjection:
    """Create one immutable, safe projection from matching planner artifacts."""
    if not isinstance(budget, BudgetLimit):
        raise TypeError("budget 必须是 BudgetLimit")
    if (
        plan.objective_id != snapshot.objective_id
        or plan.snapshot_sha256 != snapshot.snapshot_sha256
        or scheduler.objective_id != snapshot.objective_id
        or scheduler.objective_revision != snapshot.objective_revision
        or scheduler.snapshot_sha256 != snapshot.snapshot_sha256
        or scheduler.plan_sha256 != plan.plan_sha256
    ):
        raise ValidationError(
            "Attention 投影的 snapshot、plan 或 scheduler graph 不匹配"
        )
    graph_sha256 = hashlib.sha256(
        canonical_json_bytes(projection_payload(scheduler))
    ).hexdigest()
    candidates: list[AttentionReadItem] = []
    for item in plan.attention:
        candidates.append(
            AttentionReadItem(
                id=_attention_id(snapshot.objective_id, item.subject_id, item.reason),
                kind=item.kind,
                subject_id=item.subject_id,
                reason=item.reason,
                facts=item.facts,
            )
        )
    for action in plan.selected_actions:
        kind = _kind_for_selected(action)
        if kind is not None:
            candidates.append(_from_action(snapshot.objective_id, action, kind=kind))
    for action in plan.blocked:
        candidates.append(
            _from_action(snapshot.objective_id, action, kind=_kind_for_blocked(action))
        )
    unique: dict[str, AttentionReadItem] = {}
    for item in candidates:
        existing = unique.get(item.id)
        if (
            existing is None
            or existing.action_kind is not None
            and item.action_kind is None
        ):
            unique[item.id] = item
    items = tuple(sorted(unique.values(), key=_attention_sort_key))
    return AttentionReadProjection(
        schema_version=1,
        objective_id=snapshot.objective_id,
        objective_revision=snapshot.objective_revision,
        snapshot_sha256=snapshot.snapshot_sha256,
        plan_sha256=plan.plan_sha256,
        graph_sha256=graph_sha256,
        attention_sha256="",
        budget=tuple(sorted(_budget_payload(budget).items())),
        next_wake_at=plan.next_wake_at,
        items=items,
    )


def attention_projection_payload(
    projection: AttentionReadProjection,
) -> dict[str, object]:
    return {
        "schema_version": projection.schema_version,
        "objective_id": projection.objective_id,
        "objective_revision": projection.objective_revision,
        "snapshot_sha256": projection.snapshot_sha256,
        "plan_sha256": projection.plan_sha256,
        "graph_sha256": projection.graph_sha256,
        "attention_sha256": projection.attention_sha256,
        "budget": dict(projection.budget),
        "next_wake_at": None
        if projection.next_wake_at is None
        else projection.next_wake_at.isoformat(),
        "items": [_item_payload(item) for item in projection.items],
    }


def render_attention_text(projection: AttentionReadProjection) -> str:
    lines = [
        f"Objective: {projection.objective_id}",
        f"Attention SHA-256: {projection.attention_sha256}",
    ]
    for item in projection.items:
        lines.append(f"{item.kind.value}: {item.subject_id} ({item.reason.value})")
    return "\n".join(lines)


def render_attention_json(projection: AttentionReadProjection) -> str:
    return json.dumps(
        attention_projection_payload(projection),
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    )

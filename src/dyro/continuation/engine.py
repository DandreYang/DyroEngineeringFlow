"""Pure, bounded scheduler ticks for supervised continuation.

This module turns a complete deterministic plan into one *proposed* mutation
wave.  It never reserves an ActionIntent, changes a Task, starts an Agent, or
opens a workspace.  The later apply boundary must re-read authority, budget,
locks, and the Action Journal before it may perform any of those operations.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from typing import Iterable

from ..canonical import canonical_json_bytes
from ..errors import ValidationError
from .models import ActionKind, ContinuationPlan, PlannedAction
from .snapshot import SchedulerSnapshot


class WaveDeferralReason(str, Enum):
    """Locale-free reasons why a planned mutation is not in this wave."""

    RESOURCE_CONFLICT = "RESOURCE_CONFLICT"
    PARALLEL_CAPACITY = "PARALLEL_CAPACITY"


_MUTATING_ACTIONS = frozenset(
    {ActionKind.EXECUTE_TASK, ActionKind.REVIEW_TASK, ActionKind.MERGE_TASK}
)
_RESOURCE_CLASSES = frozenset({"task", "conflict", "agent", "line"})


def _facts(**values: object) -> tuple[tuple[str, str], ...]:
    return tuple(
        sorted((name, str(value)) for name, value in values.items() if value != "")
    )


def _action_payload(action: PlannedAction) -> dict[str, object]:
    return {
        "kind": action.kind.value,
        "subject_id": action.subject_id,
        "reason": action.reason.value,
        "facts": dict(action.facts),
    }


@dataclass(frozen=True)
class WaveDeferral:
    """One planned mutation intentionally left for a later scheduler tick."""

    action: PlannedAction
    reason: WaveDeferralReason
    facts: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.action.kind not in _MUTATING_ACTIONS:
            raise TypeError("WaveDeferral.action 必须是可变更 Action")
        if not isinstance(self.reason, WaveDeferralReason):
            raise TypeError("WaveDeferral.reason 必须是 WaveDeferralReason")
        facts = tuple(self.facts)
        if not all(
            isinstance(item, tuple)
            and len(item) == 2
            and all(isinstance(part, str) for part in item)
            for item in facts
        ):
            raise TypeError("WaveDeferral.facts 必须只包含两个字符串组成的键值对")
        object.__setattr__(self, "facts", tuple(sorted(facts)))


@dataclass(frozen=True)
class SchedulerTick:
    """A deterministic, path-free preview of one bounded action wave."""

    objective_id: str
    snapshot_sha256: str
    plan_sha256: str
    max_parallel: int
    active_parallel: int
    available_parallel: int
    wave: tuple[PlannedAction, ...] = ()
    deferred: tuple[WaveDeferral, ...] = ()
    non_mutating_actions: tuple[PlannedAction, ...] = ()
    tick_sha256: str = ""

    def __post_init__(self) -> None:
        if not self.objective_id:
            raise TypeError("SchedulerTick.objective_id 不能为空")
        if type(self.max_parallel) is not int or self.max_parallel < 1:
            raise TypeError("SchedulerTick.max_parallel 必须是正整数")
        if type(self.active_parallel) is not int or self.active_parallel < 0:
            raise TypeError("SchedulerTick.active_parallel 必须是非负整数")
        if type(self.available_parallel) is not int or self.available_parallel < 0:
            raise TypeError("SchedulerTick.available_parallel 必须是非负整数")
        if self.available_parallel != max(0, self.max_parallel - self.active_parallel):
            raise ValidationError("SchedulerTick.available_parallel 与并行容量不匹配")
        for digest, label in (
            (self.snapshot_sha256, "snapshot_sha256"),
            (self.plan_sha256, "plan_sha256"),
        ):
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)
            ):
                raise TypeError(f"SchedulerTick.{label} 必须是 SHA-256 十六进制摘要")
        wave = tuple(self.wave)
        deferred = tuple(self.deferred)
        passive = tuple(self.non_mutating_actions)
        if not all(action.kind in _MUTATING_ACTIONS for action in wave):
            raise TypeError("SchedulerTick.wave 必须只包含可变更 Action")
        if not all(isinstance(item, WaveDeferral) for item in deferred):
            raise TypeError("SchedulerTick.deferred 必须只包含 WaveDeferral")
        if not all(action.kind not in _MUTATING_ACTIONS for action in passive):
            raise TypeError("SchedulerTick.non_mutating_actions 不能包含可变更 Action")
        action_keys = [
            (action.kind.value, action.subject_id)
            for action in (*wave, *(item.action for item in deferred))
        ]
        if len(set(action_keys)) != len(action_keys):
            raise TypeError("SchedulerTick mutation Action 不能重复")
        expected = _tick_sha256(
            self.objective_id,
            self.snapshot_sha256,
            self.plan_sha256,
            self.max_parallel,
            self.active_parallel,
            self.available_parallel,
            wave,
            deferred,
            passive,
        )
        if self.tick_sha256 and self.tick_sha256 != expected:
            raise ValidationError("SchedulerTick.tick_sha256 与内容不匹配")
        object.__setattr__(self, "wave", wave)
        object.__setattr__(self, "deferred", deferred)
        object.__setattr__(self, "non_mutating_actions", passive)
        object.__setattr__(self, "tick_sha256", expected)


def _tick_sha256(
    objective_id: str,
    snapshot_sha256: str,
    plan_sha256: str,
    max_parallel: int,
    active_parallel: int,
    available_parallel: int,
    wave: Iterable[PlannedAction],
    deferred: Iterable[WaveDeferral],
    passive: Iterable[PlannedAction],
) -> str:
    payload = {
        "schema_version": 1,
        "objective_id": objective_id,
        "snapshot_sha256": snapshot_sha256,
        "plan_sha256": plan_sha256,
        "max_parallel": max_parallel,
        "active_parallel": active_parallel,
        "available_parallel": available_parallel,
        "wave": [_action_payload(action) for action in wave],
        "deferred": [
            {
                "action": _action_payload(item.action),
                "reason": item.reason.value,
                "facts": dict(item.facts),
            }
            for item in deferred
        ],
        "non_mutating_actions": [_action_payload(action) for action in passive],
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _action_resources(
    snapshot: SchedulerSnapshot, action: PlannedAction
) -> tuple[str, ...]:
    """Return the resource declarations for one planned mutation.

    The resource names remain internal to the selector.  Public projections
    expose only a conflict's safe class and selected subject ID, not task
    directories, argv, prompts, environment, or tool configuration.
    """
    task = snapshot.tasks_by_id.get(action.subject_id)
    if task is None:
        raise ValidationError(
            f"计划 Action subject 不在调度快照中：{action.subject_id}"
        )
    if action.kind is ActionKind.EXECUTE_TASK:
        resources = [f"task:{task.task.id}", f"agent:{task.task.executor}"]
        if task.task.conflict_group:
            resources.append(f"conflict:{task.task.conflict_group}")
        return tuple(sorted(resources))
    if action.kind is ActionKind.REVIEW_TASK:
        return tuple(sorted((f"task:{task.task.id}", f"agent:{task.task.reviewer}")))
    if action.kind is ActionKind.MERGE_TASK:
        return tuple(sorted((f"task:{task.task.id}", f"line:{task.task.line}:merge")))
    raise TypeError("只有可变更 Action 可以声明 scheduler resource")


def _resource_class(resource: str) -> str:
    """Map an internal resource name to its public, safe category only."""
    resource_class, separator, _value = resource.partition(":")
    if not separator or resource_class not in _RESOURCE_CLASSES:
        raise ValidationError("SchedulerTick 内部 resource 类别无效")
    return resource_class


def _active_parallel(snapshot: SchedulerSnapshot) -> int:
    """Count Objective work already occupying a bounded mutation slot.

    An in-progress local Task and an active external claim both consume a
    slot.  Only the accepted Objective scope is counted: unrelated workspace
    work belongs to the later workspace-wide budget check, not this pure
    Objective tick preview.
    """
    scope = set(snapshot.objective_scope)
    return sum(
        item.task.id in scope
        and (
            item.status == "in_progress"
            or (item.status == "assigned" and item.external_claim_active)
        )
        for item in snapshot.tasks
    )


def build_scheduler_tick(
    snapshot: SchedulerSnapshot,
    plan: ContinuationPlan,
    *,
    max_parallel: int,
) -> SchedulerTick:
    """Select one deterministic mutation wave from a fixed plan and snapshot.

    Selection is deliberately stricter than planning: every action first gets
    a task, conflict-group or agent/line resource.  It then competes for the
    Objective's bounded parallel capacity.  The output is still a preview;
    using it never creates an ActionIntent or delivery side effect.
    """
    if plan.objective_id != snapshot.objective_id:
        raise ValidationError("SchedulerTick 计划与快照 Objective 不匹配")
    if plan.snapshot_sha256 != snapshot.snapshot_sha256:
        raise ValidationError("SchedulerTick 计划与快照摘要不匹配")
    if type(max_parallel) is not int or max_parallel < 1:
        raise ValidationError("SchedulerTick max_parallel 必须是正整数")

    active_parallel = _active_parallel(snapshot)
    available_parallel = max(0, max_parallel - active_parallel)
    selected = tuple(
        sorted(
            plan.selected_actions, key=lambda item: (item.kind.value, item.subject_id)
        )
    )
    mutation_keys = [
        (item.kind.value, item.subject_id)
        for item in selected
        if item.kind in _MUTATING_ACTIONS
    ]
    if len(set(mutation_keys)) != len(mutation_keys):
        raise ValidationError("SchedulerTick 计划不能含重复 mutation Action")
    resources: dict[str, str] = {}
    wave: list[PlannedAction] = []
    deferred: list[WaveDeferral] = []
    passive: list[PlannedAction] = []
    for action in selected:
        if action.kind not in _MUTATING_ACTIONS:
            passive.append(action)
            continue
        claims = _action_resources(snapshot, action)
        collision = next((claim for claim in claims if claim in resources), "")
        if collision:
            deferred.append(
                WaveDeferral(
                    action,
                    WaveDeferralReason.RESOURCE_CONFLICT,
                    _facts(
                        resource_class=_resource_class(collision),
                        selected_subject_id=resources[collision],
                    ),
                )
            )
            continue
        if len(wave) >= available_parallel:
            deferred.append(
                WaveDeferral(
                    action,
                    WaveDeferralReason.PARALLEL_CAPACITY,
                    _facts(
                        max_parallel=max_parallel,
                        active_parallel=active_parallel,
                        available_parallel=available_parallel,
                    ),
                )
            )
            continue
        wave.append(action)
        resources.update({claim: action.subject_id for claim in claims})
    return SchedulerTick(
        objective_id=plan.objective_id,
        snapshot_sha256=plan.snapshot_sha256,
        plan_sha256=plan.plan_sha256,
        max_parallel=max_parallel,
        active_parallel=active_parallel,
        available_parallel=available_parallel,
        wave=tuple(wave),
        deferred=tuple(deferred),
        non_mutating_actions=tuple(passive),
    )


def scheduler_tick_payload(tick: SchedulerTick) -> dict[str, object]:
    """Return the safe JSON representation shared by CLI and future Console."""
    return {
        "schema_version": 1,
        "objective_id": tick.objective_id,
        "snapshot_sha256": tick.snapshot_sha256,
        "plan_sha256": tick.plan_sha256,
        "tick_sha256": tick.tick_sha256,
        "max_parallel": tick.max_parallel,
        "active_parallel": tick.active_parallel,
        "available_parallel": tick.available_parallel,
        "wave": [_action_payload(action) for action in tick.wave],
        "deferred": [
            {
                "action": _action_payload(item.action),
                "reason": item.reason.value,
                "facts": dict(item.facts),
            }
            for item in tick.deferred
        ],
        "non_mutating_actions": [
            _action_payload(action) for action in tick.non_mutating_actions
        ],
    }


def render_scheduler_tick_text(tick: SchedulerTick) -> str:
    """Render a concise human preview without implying execution occurred."""
    lines = [
        f"Objective: {tick.objective_id}",
        f"Tick SHA-256: {tick.tick_sha256}",
        (
            f"Mutation wave: {len(tick.wave)}/{tick.available_parallel} "
            f"（总容量 {tick.max_parallel}，已占用 {tick.active_parallel}；仅预览，未执行）"
        ),
    ]
    for action in tick.wave:
        lines.append(
            f"Wave: {action.kind.value} {action.subject_id} ({action.reason.value})"
        )
    for item in tick.deferred:
        lines.append(
            f"Deferred: {item.action.kind.value} {item.action.subject_id} ({item.reason.value})"
        )
    for action in tick.non_mutating_actions:
        lines.append(
            f"Notice: {action.kind.value} {action.subject_id} ({action.reason.value})"
        )
    return "\n".join(lines)


def render_scheduler_tick_json(tick: SchedulerTick) -> str:
    return json.dumps(
        scheduler_tick_payload(tick), ensure_ascii=False, sort_keys=True, indent=2
    )

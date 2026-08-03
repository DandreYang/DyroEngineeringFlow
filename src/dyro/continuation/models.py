"""Pure immutable domain types shared by continuation stages.

These types deliberately express data only. They must not read a workspace,
sample a clock, invoke a process, or resolve environment variables.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any


def _frozen_items(value: Any, label: str) -> tuple[Any, ...]:
    """Copy an arbitrary collection into a tuple before publishing it."""
    if isinstance(value, (str, bytes)):
        raise TypeError(f"{label} 必须是集合，不能是字符串")
    try:
        return tuple(value)
    except TypeError as exc:
        raise TypeError(f"{label} 必须是集合") from exc


def _frozen_strings(value: Any, label: str) -> tuple[str, ...]:
    items = _frozen_items(value, label)
    if not all(isinstance(item, str) for item in items):
        raise TypeError(f"{label} 必须只包含字符串")
    return items


def _frozen_facts(value: Any, label: str) -> tuple[tuple[str, str], ...]:
    facts: list[tuple[str, str]] = []
    for item in _frozen_items(value, label):
        pair = _frozen_items(item, label)
        if len(pair) != 2 or not all(isinstance(part, str) for part in pair):
            raise TypeError(f"{label} 必须只包含两个字符串组成的键值对")
        facts.append((pair[0], pair[1]))
    return tuple(facts)


class RequestedMode(str, Enum):
    OBSERVE = "observe"
    SUPERVISED = "supervised"
    AUTOMATIC = "automatic"


class Operation(str, Enum):
    EXECUTE = "execute"
    REVIEW = "review"
    MERGE = "merge"


class ActionKind(str, Enum):
    EXECUTE_TASK = "execute_task"
    REVIEW_TASK = "review_task"
    MERGE_TASK = "merge_task"
    PROBE_TRIGGER = "probe_trigger"
    WAIT = "wait"
    ASK_USER = "ask_user"
    PAUSE = "pause"
    COMPLETE = "complete"
    REPAIR_REQUIRED = "repair_required"


class CompletionRule(str, Enum):
    ALL_TARGETS_INTEGRATED = "all_targets_integrated"


class PlanCompletion(str, Enum):
    INCOMPLETE = "incomplete"
    COMPLETE = "complete"
    REPAIR_REQUIRED = "repair_required"


class ReasonCode(str, Enum):
    TASK_READY = "TASK_READY"
    DEPENDENCY_PENDING = "DEPENDENCY_PENDING"
    DECISION_OPEN = "DECISION_OPEN"
    ANSWER_REQUIRED = "ANSWER_REQUIRED"
    EXTERNAL_CLAIM_ACTIVE = "EXTERNAL_CLAIM_ACTIVE"
    TRIGGER_NOT_DUE = "TRIGGER_NOT_DUE"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    NO_PROGRESS = "NO_PROGRESS"
    CONTRACT_DRIFT = "CONTRACT_DRIFT"
    ACTION_UNCERTAIN = "ACTION_UNCERTAIN"
    TARGETS_INTEGRATED = "TARGETS_INTEGRATED"
    OBJECTIVE_SCOPE_CONFLICT = "OBJECTIVE_SCOPE_CONFLICT"
    ACTIVATION_REQUIRED = "ACTIVATION_REQUIRED"
    POLICY_DISALLOWS_OPERATION = "POLICY_DISALLOWS_OPERATION"


class TriggerState(str, Enum):
    PENDING = "pending"
    SATISFIED = "satisfied"
    ERROR = "error"
    DISABLED = "disabled"


class AttentionKind(str, Enum):
    REPAIR_REQUIRED = "repair_required"
    NEEDS_USER = "needs_user"
    READY = "ready"
    PAUSED = "paused"
    WAITING = "waiting"


@dataclass(frozen=True)
class BudgetLimit:
    max_actions: int
    max_attempts_per_task: int
    max_failures: int
    max_no_progress_cycles: int
    max_parallel: int
    deadline: datetime | None = None


@dataclass(frozen=True)
class Objective:
    schema_version: int
    id: str
    title: str
    line: str
    targets: tuple[str, ...]
    completion: CompletionRule
    requested_mode: RequestedMode
    operations: tuple[Operation, ...]
    budget: BudgetLimit

    def __post_init__(self) -> None:
        if not isinstance(self.completion, CompletionRule):
            raise TypeError("Objective.completion 必须是 CompletionRule")
        if not isinstance(self.requested_mode, RequestedMode):
            raise TypeError("Objective.requested_mode 必须是 RequestedMode")
        if not isinstance(self.budget, BudgetLimit):
            raise TypeError("Objective.budget 必须是 BudgetLimit")
        targets = _frozen_strings(self.targets, "Objective.targets")
        operations = _frozen_items(self.operations, "Objective.operations")
        if not all(isinstance(item, Operation) for item in operations):
            raise TypeError("Objective.operations 必须只包含 Operation")
        object.__setattr__(self, "targets", targets)
        object.__setattr__(self, "operations", operations)


@dataclass(frozen=True)
class TriggerObservation:
    trigger_id: str
    state: TriggerState
    summary: str
    evidence_ref: str = ""
    observed_at: datetime | None = None
    next_probe_at: datetime | None = None


@dataclass(frozen=True)
class PlannedAction:
    kind: ActionKind
    subject_id: str
    reason: ReasonCode
    facts: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ActionKind):
            raise TypeError("PlannedAction.kind 必须是 ActionKind")
        if not isinstance(self.reason, ReasonCode):
            raise TypeError("PlannedAction.reason 必须是 ReasonCode")
        object.__setattr__(self, "facts", _frozen_facts(self.facts, "PlannedAction.facts"))


@dataclass(frozen=True)
class AttentionItem:
    id: str
    kind: AttentionKind
    subject_id: str
    reason: ReasonCode
    facts: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.kind, AttentionKind):
            raise TypeError("AttentionItem.kind 必须是 AttentionKind")
        if not isinstance(self.reason, ReasonCode):
            raise TypeError("AttentionItem.reason 必须是 ReasonCode")
        object.__setattr__(self, "facts", _frozen_facts(self.facts, "AttentionItem.facts"))


@dataclass(frozen=True)
class ContinuationSnapshot:
    objective_id: str
    objective_revision: int
    observed_at: datetime
    snapshot_sha256: str
    task_states: tuple[tuple[str, str], ...] = ()
    facts: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_states", _frozen_facts(self.task_states, "ContinuationSnapshot.task_states"))
        object.__setattr__(self, "facts", _frozen_facts(self.facts, "ContinuationSnapshot.facts"))


@dataclass(frozen=True)
class ContinuationPlan:
    objective_id: str
    snapshot_sha256: str
    plan_sha256: str
    completion: PlanCompletion
    selected_actions: tuple[PlannedAction, ...] = ()
    blocked: tuple[PlannedAction, ...] = ()
    attention: tuple[AttentionItem, ...] = ()
    next_wake_at: datetime | None = None
    facts: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.completion, PlanCompletion):
            raise TypeError("ContinuationPlan.completion 必须是 PlanCompletion")
        selected_actions = _frozen_items(self.selected_actions, "ContinuationPlan.selected_actions")
        blocked = _frozen_items(self.blocked, "ContinuationPlan.blocked")
        attention = _frozen_items(self.attention, "ContinuationPlan.attention")
        if not all(isinstance(item, PlannedAction) for item in selected_actions + blocked):
            raise TypeError("ContinuationPlan actions 必须只包含 PlannedAction")
        if not all(isinstance(item, AttentionItem) for item in attention):
            raise TypeError("ContinuationPlan.attention 必须只包含 AttentionItem")
        object.__setattr__(self, "selected_actions", selected_actions)
        object.__setattr__(self, "blocked", blocked)
        object.__setattr__(self, "attention", attention)
        object.__setattr__(self, "facts", _frozen_facts(self.facts, "ContinuationPlan.facts"))

"""Pure budget accounting for the native continuation engine.

The functions in this module accept only immutable facts supplied by a caller.
They never read a workspace, sample a clock, start a process, or reserve state;
PR-07 later persists the returned reservation amounts alongside ActionIntents.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
from typing import Any, Iterable

from ..canonical import canonical_json_bytes
from .models import BudgetLimit


def _items(value: Any, label: str) -> tuple[Any, ...]:
    if isinstance(value, (str, bytes)):
        raise TypeError(f"{label} 必须是集合")
    try:
        return tuple(value)
    except TypeError as exc:
        raise TypeError(f"{label} 必须是集合") from exc


def _pairs(value: Any, label: str) -> tuple[tuple[str, str], ...]:
    result: list[tuple[str, str]] = []
    for pair in _items(value, label):
        item = _items(pair, label)
        if len(item) != 2 or not all(isinstance(part, str) for part in item):
            raise TypeError(f"{label} 必须只包含两个字符串组成的键值对")
        result.append((item[0], item[1]))
    return tuple(result)


def _optional_limit(value: int | None, label: str) -> int | None:
    if value is not None and (type(value) is not int or value < 1):
        raise TypeError(f"{label} 必须是正整数或 None")
    return value


def _non_negative(value: int, label: str) -> int:
    if type(value) is not int or value < 0:
        raise TypeError(f"{label} 必须是非负整数")
    return value


def _utc(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise TypeError(f"{label} 必须是带时区的 datetime")
    return value.astimezone(timezone.utc)


def _min_limit(requested: int, *caps: int | None) -> int:
    return min((requested, *(cap for cap in caps if cap is not None)))


class BudgetReason(str, Enum):
    ACTION_LIMIT = "ACTION_LIMIT"
    ATTEMPT_LIMIT = "ATTEMPT_LIMIT"
    FAILURE_LIMIT = "FAILURE_LIMIT"
    CONSECUTIVE_FAILURE_LIMIT = "CONSECUTIVE_FAILURE_LIMIT"
    PARALLEL_LIMIT = "PARALLEL_LIMIT"
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
    CLOCK_ROLLBACK = "CLOCK_ROLLBACK"
    PROVIDER_USAGE_LIMIT = "PROVIDER_USAGE_LIMIT"
    PROVIDER_USAGE_UNTRUSTED = "PROVIDER_USAGE_UNTRUSTED"
    NO_PROGRESS_LIMIT = "NO_PROGRESS_LIMIT"


@dataclass(frozen=True)
class BudgetCaps:
    """Optional workspace or activation caps; ``None`` means no extra cap."""

    max_actions: int | None = None
    max_attempts_per_task: int | None = None
    max_failures: int | None = None
    max_consecutive_failures: int | None = None
    max_no_progress_cycles: int | None = None
    max_parallel: int | None = None
    max_provider_usage: int | None = None
    deadline: datetime | None = None

    def __post_init__(self) -> None:
        for field in (
            "max_actions",
            "max_attempts_per_task",
            "max_failures",
            "max_consecutive_failures",
            "max_no_progress_cycles",
            "max_parallel",
            "max_provider_usage",
        ):
            _optional_limit(getattr(self, field), f"BudgetCaps.{field}")
        if self.deadline is not None:
            object.__setattr__(self, "deadline", _utc(self.deadline, "BudgetCaps.deadline"))


@dataclass(frozen=True)
class EffectiveBudget:
    max_actions: int
    max_attempts_per_task: int
    max_failures: int
    max_consecutive_failures: int
    max_no_progress_cycles: int
    max_parallel: int
    max_provider_usage: int | None
    deadline: datetime | None


@dataclass(frozen=True)
class BudgetReservation:
    """A worst-case reservation held by any active Objective in a workspace."""

    objective_id: str
    task_id: str
    actions: int = 1
    attempts: int = 1
    failures: int = 1
    parallel: int = 1
    provider_usage: int = 0

    def __post_init__(self) -> None:
        if not self.objective_id or not self.task_id:
            raise TypeError("BudgetReservation 必须包含 Objective 和 Task")
        for field in ("actions", "attempts", "failures", "parallel", "provider_usage"):
            _non_negative(getattr(self, field), f"BudgetReservation.{field}")


@dataclass(frozen=True)
class BudgetUsage:
    """Committed Objective facts before any pending reservations are added."""

    actions: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    active_parallel: int = 0
    provider_usage: int = 0
    provider_usage_trusted: bool = False
    no_progress_cycles: int = 0
    attempts_by_task: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        for field in (
            "actions",
            "failures",
            "consecutive_failures",
            "active_parallel",
            "provider_usage",
            "no_progress_cycles",
        ):
            _non_negative(getattr(self, field), f"BudgetUsage.{field}")
        if type(self.provider_usage_trusted) is not bool:
            raise TypeError("BudgetUsage.provider_usage_trusted 必须是 bool")
        pairs: list[tuple[str, int]] = []
        for item in _items(self.attempts_by_task, "BudgetUsage.attempts_by_task"):
            pair = _items(item, "BudgetUsage.attempts_by_task")
            if len(pair) != 2 or not isinstance(pair[0], str) or not pair[0]:
                raise TypeError("BudgetUsage.attempts_by_task 必须包含 Task ID 和计数")
            pairs.append((pair[0], _non_negative(pair[1], "BudgetUsage.attempts_by_task")))
        if len({task_id for task_id, _ in pairs}) != len(pairs):
            raise TypeError("BudgetUsage.attempts_by_task 不能重复")
        object.__setattr__(self, "attempts_by_task", tuple(sorted(pairs)))


@dataclass(frozen=True)
class BudgetRequest:
    task_id: str
    actions: int = 1
    attempts: int = 1
    failures: int = 1
    parallel: int = 1
    provider_usage: int = 0
    provider_usage_trusted: bool = False

    def __post_init__(self) -> None:
        if not self.task_id:
            raise TypeError("BudgetRequest.task_id 不能为空")
        for field in ("actions", "attempts", "failures", "parallel", "provider_usage"):
            value = _non_negative(getattr(self, field), f"BudgetRequest.{field}")
            # A review consumes an Action and a scheduler slot but is not a
            # new execution attempt.  Keep that distinction expressible at
            # the budget boundary instead of charging reviews against the
            # per-task execution-attempt ceiling.
            if field in {"actions", "parallel"} and value < 1:
                raise TypeError(f"BudgetRequest.{field} 必须是正整数")
        if type(self.provider_usage_trusted) is not bool:
            raise TypeError("BudgetRequest.provider_usage_trusted 必须是 bool")


@dataclass(frozen=True)
class BudgetDecisionInput:
    objective_id: str
    requested: BudgetLimit
    workspace: BudgetCaps
    activation: BudgetCaps | None
    usage: BudgetUsage
    workspace_usage: BudgetUsage
    reservations: tuple[BudgetReservation, ...]
    now: datetime
    request: BudgetRequest
    last_observed_at: datetime | None = None
    automatic: bool = False

    def __post_init__(self) -> None:
        if not self.objective_id:
            raise TypeError("BudgetDecisionInput.objective_id 不能为空")
        if type(self.automatic) is not bool:
            raise TypeError("BudgetDecisionInput.automatic 必须是 bool")
        if not isinstance(self.requested, BudgetLimit):
            raise TypeError("BudgetDecisionInput.requested 必须是 BudgetLimit")
        if not isinstance(self.workspace, BudgetCaps):
            raise TypeError("BudgetDecisionInput.workspace 必须是 BudgetCaps")
        if self.activation is not None and not isinstance(self.activation, BudgetCaps):
            raise TypeError("BudgetDecisionInput.activation 必须是 BudgetCaps 或 None")
        if (
            not isinstance(self.usage, BudgetUsage)
            or not isinstance(self.workspace_usage, BudgetUsage)
            or not isinstance(self.request, BudgetRequest)
        ):
            raise TypeError("BudgetDecisionInput usage、workspace_usage 或 request 无效")
        reservations = _items(self.reservations, "BudgetDecisionInput.reservations")
        if not all(isinstance(item, BudgetReservation) for item in reservations):
            raise TypeError("BudgetDecisionInput.reservations 必须只包含 BudgetReservation")
        object.__setattr__(self, "reservations", tuple(sorted(reservations, key=lambda item: (item.objective_id, item.task_id))))
        object.__setattr__(self, "now", _utc(self.now, "BudgetDecisionInput.now"))
        if self.last_observed_at is not None:
            object.__setattr__(self, "last_observed_at", _utc(self.last_observed_at, "BudgetDecisionInput.last_observed_at"))


@dataclass(frozen=True)
class BudgetDecision:
    allowed: bool
    reasons: tuple[BudgetReason, ...]
    effective: EffectiveBudget
    reservation: BudgetReservation
    facts: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class ProgressFacts:
    """Only delivery facts may reset no-progress accounting.

    ``trigger_observations`` is retained for callers but intentionally excluded
    from :func:`progress_fingerprint`.
    """

    task_states: tuple[tuple[str, str], ...] = ()
    integration_heads: tuple[tuple[str, str], ...] = ()
    decisions: tuple[tuple[str, str], ...] = ()
    effective_evidence: tuple[tuple[str, str], ...] = ()
    trigger_observations: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        for field in (
            "task_states",
            "integration_heads",
            "decisions",
            "effective_evidence",
            "trigger_observations",
        ):
            object.__setattr__(self, field, tuple(sorted(_pairs(getattr(self, field), f"ProgressFacts.{field}"))))


@dataclass(frozen=True)
class NoProgressDecision:
    fingerprint: str
    cycles: int
    reset: bool
    exhausted: bool


def effective_budget(
    requested: BudgetLimit,
    workspace: BudgetCaps,
    activation: BudgetCaps | None,
) -> EffectiveBudget:
    """Intersect Objective, workspace, and optional activation limits."""
    if not isinstance(requested, BudgetLimit):
        raise TypeError("requested 必须是 BudgetLimit")
    if not isinstance(workspace, BudgetCaps):
        raise TypeError("workspace 必须是 BudgetCaps")
    if activation is not None and not isinstance(activation, BudgetCaps):
        raise TypeError("activation 必须是 BudgetCaps 或 None")
    local = activation or BudgetCaps()
    deadlines = tuple(
        deadline
        for deadline in (requested.deadline, workspace.deadline, local.deadline)
        if deadline is not None
    )
    return EffectiveBudget(
        max_actions=_min_limit(requested.max_actions, workspace.max_actions, local.max_actions),
        max_attempts_per_task=_min_limit(
            requested.max_attempts_per_task,
            workspace.max_attempts_per_task,
            local.max_attempts_per_task,
        ),
        max_failures=_min_limit(requested.max_failures, workspace.max_failures, local.max_failures),
        max_consecutive_failures=_min_limit(
            requested.max_failures,
            workspace.max_consecutive_failures,
            local.max_consecutive_failures,
        ),
        max_no_progress_cycles=_min_limit(
            requested.max_no_progress_cycles,
            workspace.max_no_progress_cycles,
            local.max_no_progress_cycles,
        ),
        max_parallel=_min_limit(requested.max_parallel, workspace.max_parallel, local.max_parallel),
        max_provider_usage=(
            min(cap for cap in (workspace.max_provider_usage, local.max_provider_usage) if cap is not None)
            if workspace.max_provider_usage is not None or local.max_provider_usage is not None
            else None
        ),
        deadline=min(deadlines) if deadlines else None,
    )


def _reservation_amount(input: BudgetDecisionInput) -> BudgetReservation:
    request = input.request
    return BudgetReservation(
        objective_id=input.objective_id,
        task_id=request.task_id,
        actions=request.actions,
        attempts=request.attempts,
        failures=request.failures,
        parallel=request.parallel,
        provider_usage=request.provider_usage,
    )


def _attempts_for_task(usage: BudgetUsage, reservations: Iterable[BudgetReservation], task_id: str) -> int:
    committed = dict(usage.attempts_by_task).get(task_id, 0)
    return committed + sum(item.attempts for item in reservations if item.task_id == task_id)


def decide_budget(input: BudgetDecisionInput) -> BudgetDecision:
    """Return the deterministic budget decision for one prospective action."""
    effective = effective_budget(input.requested, input.workspace, input.activation)
    reservation = _reservation_amount(input)
    prior = input.reservations
    local_prior = tuple(item for item in prior if item.objective_id == input.objective_id)
    local = effective_budget(input.requested, BudgetCaps(), input.activation)
    reasons: list[BudgetReason] = []
    local_actions = input.usage.actions + sum(item.actions for item in local_prior) + reservation.actions
    workspace_actions = input.workspace_usage.actions + sum(item.actions for item in prior) + reservation.actions
    if local_actions > local.max_actions or (
        input.workspace.max_actions is not None and workspace_actions > input.workspace.max_actions
    ):
        reasons.append(BudgetReason.ACTION_LIMIT)
    task_attempts = _attempts_for_task(input.usage, local_prior, reservation.task_id) + reservation.attempts
    workspace_task_attempts = (
        _attempts_for_task(input.workspace_usage, prior, reservation.task_id) + reservation.attempts
    )
    if task_attempts > local.max_attempts_per_task or (
        input.workspace.max_attempts_per_task is not None
        and workspace_task_attempts > input.workspace.max_attempts_per_task
    ):
        reasons.append(BudgetReason.ATTEMPT_LIMIT)
    local_failures = input.usage.failures + sum(item.failures for item in local_prior) + reservation.failures
    workspace_failures = input.workspace_usage.failures + sum(item.failures for item in prior) + reservation.failures
    if local_failures > local.max_failures or (
        input.workspace.max_failures is not None and workspace_failures > input.workspace.max_failures
    ):
        reasons.append(BudgetReason.FAILURE_LIMIT)
    local_consecutive_failures = (
        input.usage.consecutive_failures
        + sum(item.failures for item in local_prior)
        + reservation.failures
    )
    workspace_consecutive_failures = (
        input.workspace_usage.consecutive_failures
        + sum(item.failures for item in prior)
        + reservation.failures
    )
    if local_consecutive_failures > local.max_consecutive_failures or (
        input.workspace.max_consecutive_failures is not None
        and workspace_consecutive_failures > input.workspace.max_consecutive_failures
    ):
        reasons.append(BudgetReason.CONSECUTIVE_FAILURE_LIMIT)
    local_parallel = input.usage.active_parallel + sum(item.parallel for item in local_prior) + reservation.parallel
    workspace_parallel = input.workspace_usage.active_parallel + sum(item.parallel for item in prior) + reservation.parallel
    if local_parallel > local.max_parallel or (
        input.workspace.max_parallel is not None and workspace_parallel > input.workspace.max_parallel
    ):
        reasons.append(BudgetReason.PARALLEL_LIMIT)
    if effective.deadline is not None and input.now >= effective.deadline:
        reasons.append(BudgetReason.DEADLINE_EXCEEDED)
    if input.last_observed_at is not None and input.now < input.last_observed_at:
        reasons.append(BudgetReason.CLOCK_ROLLBACK)
    local_provider_usage = input.usage.provider_usage + sum(item.provider_usage for item in local_prior) + reservation.provider_usage
    workspace_provider_usage = (
        input.workspace_usage.provider_usage
        + sum(item.provider_usage for item in prior)
        + reservation.provider_usage
    )
    if effective.max_provider_usage is not None:
        if input.automatic and (
            not input.usage.provider_usage_trusted
            or not input.workspace_usage.provider_usage_trusted
            or not input.request.provider_usage_trusted
        ):
            reasons.append(BudgetReason.PROVIDER_USAGE_UNTRUSTED)
        elif (
            (input.activation is not None and input.activation.max_provider_usage is not None and local_provider_usage > input.activation.max_provider_usage)
            or (input.workspace.max_provider_usage is not None and workspace_provider_usage > input.workspace.max_provider_usage)
        ):
            reasons.append(BudgetReason.PROVIDER_USAGE_LIMIT)
    if input.usage.no_progress_cycles >= local.max_no_progress_cycles or (
        input.workspace.max_no_progress_cycles is not None
        and input.workspace_usage.no_progress_cycles >= input.workspace.max_no_progress_cycles
    ):
        reasons.append(BudgetReason.NO_PROGRESS_LIMIT)
    facts = tuple(sorted(
        (
            ("actions", str(workspace_actions)),
            ("attempts_for_task", str(task_attempts)),
            ("failures", str(workspace_failures)),
            ("parallel", str(workspace_parallel)),
            ("provider_usage", str(workspace_provider_usage)),
        )
    ))
    return BudgetDecision(
        allowed=not reasons,
        reasons=tuple(reasons),
        effective=effective,
        reservation=reservation,
        facts=facts,
    )


def progress_fingerprint(facts: ProgressFacts) -> str:
    """Hash delivery facts while deliberately ignoring Trigger observations."""
    if not isinstance(facts, ProgressFacts):
        raise TypeError("facts 必须是 ProgressFacts")
    payload = {
        "schema_version": 1,
        "task_states": [list(item) for item in facts.task_states],
        "integration_heads": [list(item) for item in facts.integration_heads],
        "decisions": [list(item) for item in facts.decisions],
        "effective_evidence": [list(item) for item in facts.effective_evidence],
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def decide_no_progress(
    *,
    previous_fingerprint: str,
    previous_cycles: int,
    current: ProgressFacts,
    maximum: int,
) -> NoProgressDecision:
    """Advance no-progress state without letting Trigger-only churn reset it."""
    _non_negative(previous_cycles, "previous_cycles")
    if type(maximum) is not int or maximum < 1:
        raise TypeError("maximum 必须是正整数")
    current_fingerprint = progress_fingerprint(current)
    reset = bool(previous_fingerprint) and previous_fingerprint != current_fingerprint
    cycles = 0 if reset else previous_cycles + 1
    return NoProgressDecision(
        fingerprint=current_fingerprint,
        cycles=cycles,
        reset=reset,
        exhausted=cycles >= maximum,
    )

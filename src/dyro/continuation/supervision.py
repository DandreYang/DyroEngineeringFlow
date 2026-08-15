"""The explicit-confirmation apply boundary for supervised Objectives.

Planning remains pure.  This module is the narrow bridge from a user-confirmed
wave to the existing Task APIs: it re-plans every action, writes a fenced
ActionIntent, crosses the durable Action-start barrier, and only then invokes
the Task operation.  It never calls merge or push, and any failure after the
start barrier is recorded as ``uncertain`` rather than replayed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
import secrets
from typing import Callable

from ..canonical import canonical_json_bytes
from ..config import Config
from ..errors import DyroError, ValidationError
from ..tasks import Task, review_task, run_task
from .action_models import ActionIntent, ActionReceipt, ActionStatus
from .budgets import BudgetReservation
from .engine import SchedulerTick, build_scheduler_tick
from .models import ActionKind, PlannedAction, RequestedMode
from .planner import build_continuation_plan
from .snapshot import SchedulerSnapshot, build_scheduler_snapshot
from .store import (
    acquire_objective_owner_lease,
    get_objective,
    get_objective_action,
    list_objective_actions,
    record_objective_action_receipt,
    release_objective_owner_lease,
    reserve_supervised_objective_action,
    start_objective_action,
)


SUPERVISION_LEASE_SECONDS = 3_600
_SUPPORTED_ACTIONS = frozenset({ActionKind.EXECUTE_TASK, ActionKind.REVIEW_TASK})


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise TypeError("监督执行时钟必须带时区")
    return value.astimezone(timezone.utc)


def _action_payload(action: PlannedAction) -> dict[str, object]:
    return {
        "kind": action.kind.value,
        "subject_id": action.subject_id,
        "reason": action.reason.value,
        "facts": dict(action.facts),
    }


@dataclass(frozen=True)
class SupervisedWave:
    """A user-visible, immutable proposal; building it has zero side effects."""

    objective_id: str
    snapshot_sha256: str
    plan_sha256: str
    tick_sha256: str
    confirmation_sha256: str
    actions: tuple[PlannedAction, ...]

    def __post_init__(self) -> None:
        if not self.objective_id:
            raise TypeError("SupervisedWave.objective_id 不能为空")
        for value, label in (
            (self.snapshot_sha256, "snapshot_sha256"),
            (self.plan_sha256, "plan_sha256"),
            (self.tick_sha256, "tick_sha256"),
            (self.confirmation_sha256, "confirmation_sha256"),
        ):
            if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                raise TypeError(f"SupervisedWave.{label} 必须是 SHA-256 十六进制摘要")
        actions = tuple(self.actions)
        if not actions or not all(action.kind in _SUPPORTED_ACTIONS for action in actions):
            raise TypeError("SupervisedWave.actions 必须包含 execute_task 或 review_task")
        if len({(action.kind, action.subject_id) for action in actions}) != len(actions):
            raise ValidationError("SupervisedWave.actions 不能重复")
        object.__setattr__(self, "actions", actions)


@dataclass(frozen=True)
class SupervisedOutcome:
    """A path-free Action result safe for CLI and future Console projections."""

    action_id: str
    operation: ActionKind
    subject_id: str
    status: ActionStatus
    result: str

    def __post_init__(self) -> None:
        if not self.action_id or self.operation not in _SUPPORTED_ACTIONS or not self.subject_id:
            raise TypeError("SupervisedOutcome 字段无效")
        if self.status not in {ActionStatus.SUCCEEDED, ActionStatus.FAILED, ActionStatus.UNCERTAIN}:
            raise TypeError("SupervisedOutcome.status 必须是已执行终态")
        if not self.result or len(self.result) > 64:
            raise TypeError("SupervisedOutcome.result 无效")


def _confirmation_sha256(
    *,
    objective: object,
    snapshot: SchedulerSnapshot,
    tick: SchedulerTick,
    actions: tuple[PlannedAction, ...],
) -> str:
    """Hash the confirmed semantics while deliberately excluding sample time.

    ``snapshot_sha256`` and ``tick_sha256`` correctly include ``observed_at``
    for audit binding, but therefore cannot be copied from a preview into a
    later command.  This digest captures every safety-relevant fact used for a
    supervised wave without that wall-clock sample, so a user can confirm the
    displayed proposal across processes and any semantic drift is rejected.
    """
    payload = {
        "schema_version": 1,
        "objective": {
            "id": objective.objective.id,
            "revision": objective.revision,
            "event_seq": objective.event_seq,
            "event_sha256": objective.event_sha256,
            "scope_sha256": objective.scope_sha256,
        },
        "snapshot": {
            "execution_mode": snapshot.execution_mode,
            "candidate_ids": list(snapshot.candidate_ids),
            "tasks": [
                {
                    "id": item.task.id,
                    "line": item.task.line,
                    "status": item.status,
                    "depends_on": list(item.task.depends_on),
                    "blocked_on": list(item.task.blocked_on),
                    "conflict_group": item.task.conflict_group,
                    "external_claim_active": item.external_claim_active,
                    "integration_state": item.integration_state,
                    "contract_sha256": item.contract_sha256,
                }
                for item in snapshot.tasks
            ],
            "decisions": [list(item) for item in snapshot.decisions],
            "objective_state": snapshot.objective_state,
            "objective_scope": list(snapshot.objective_scope),
            "objective_targets": list(snapshot.objective_targets),
            "objective_requested_mode": snapshot.objective_requested_mode,
            "objective_operations": list(snapshot.objective_operations),
            "objective_drifted": snapshot.objective_drifted,
        },
        "wave": {
            "max_parallel": tick.max_parallel,
            "active_parallel": tick.active_parallel,
            "available_parallel": tick.available_parallel,
            "actions": [_action_payload(action) for action in actions],
        },
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def build_supervised_wave(
    config: Config,
    objective_id: str,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> SupervisedWave:
    """Create one exact, read-only confirmation proposal from current state."""
    record = get_objective(config, objective_id, recover=False)
    if record.objective.requested_mode is not RequestedMode.SUPERVISED:
        raise DyroError("当前 Objective 不是 supervised 模式；拒绝使用受监督 apply")
    snapshot = build_scheduler_snapshot(config, objective=record, clock=clock)
    plan = build_continuation_plan(snapshot)
    tick = build_scheduler_tick(snapshot, plan, max_parallel=record.objective.budget.max_parallel)
    actions = tuple(action for action in tick.wave if action.kind in _SUPPORTED_ACTIONS)
    if len(actions) != len(tick.wave):
        raise DyroError("当前 wave 含尚未获受监督执行支持的 Action；拒绝部分执行")
    if not actions:
        raise DyroError("当前没有可由受监督阶段执行的 Action；请运行 objective attention 或 tick 查看原因")
    return SupervisedWave(
        objective_id=record.objective.id,
        snapshot_sha256=snapshot.snapshot_sha256,
        plan_sha256=plan.plan_sha256,
        tick_sha256=tick.tick_sha256,
        confirmation_sha256=_confirmation_sha256(
            objective=record,
            snapshot=snapshot,
            tick=tick,
            actions=actions,
        ),
        actions=actions,
    )


def supervised_wave_payload(wave: SupervisedWave) -> dict[str, object]:
    return {
        "schema_version": 1,
        "objective_id": wave.objective_id,
        "snapshot_sha256": wave.snapshot_sha256,
        "plan_sha256": wave.plan_sha256,
        "tick_sha256": wave.tick_sha256,
        "confirmation_sha256": wave.confirmation_sha256,
        "actions": [_action_payload(action) for action in wave.actions],
        "execution": "serial_supervised",
        "merge": "not_supported",
        "push": "not_supported",
    }


def render_supervised_wave_text(wave: SupervisedWave) -> str:
    lines = [
        f"Objective: {wave.objective_id}",
        f"Tick SHA-256: {wave.tick_sha256}",
        f"Confirmation SHA-256: {wave.confirmation_sha256}",
        f"Supervised Action wave: {len(wave.actions)}（按顺序执行；每项均在 durable Action-start 后才调用 Task API）",
    ]
    lines.extend(
        f"Action: {action.kind.value} {action.subject_id} ({action.reason.value})"
        for action in wave.actions
    )
    lines.append("不会自动 merge 或 push；非交互执行必须显式确认该 Confirmation SHA-256。")
    return "\n".join(lines)


def render_supervised_wave_json(wave: SupervisedWave) -> str:
    return json.dumps(supervised_wave_payload(wave), ensure_ascii=False, sort_keys=True, indent=2)


def _current_action(
    config: Config,
    objective_id: str,
    expected: PlannedAction,
    *,
    clock: Callable[[], datetime],
) -> tuple[object, SchedulerSnapshot, SchedulerTick, PlannedAction, Task]:
    """Re-plan before each mutation and retain only the confirmed exact Action."""
    record = get_objective(config, objective_id, recover=False)
    if record.objective.requested_mode is not RequestedMode.SUPERVISED:
        raise DyroError("Objective 模式已变化；拒绝执行已确认 Action")
    snapshot = build_scheduler_snapshot(config, objective=record, clock=clock)
    plan = build_continuation_plan(snapshot)
    tick = build_scheduler_tick(snapshot, plan, max_parallel=record.objective.budget.max_parallel)
    actual = next((action for action in tick.wave if action == expected), None)
    if actual is None:
        raise DyroError("确认后的 Action 已不再位于当前安全 wave；请重新运行 objective apply")
    task_snapshot = snapshot.tasks_by_id.get(actual.subject_id)
    if task_snapshot is None or not task_snapshot.contract_sha256:
        raise DyroError("当前 Action 缺少已固定的 Task contract 摘要；拒绝执行")
    return record, snapshot, tick, actual, task_snapshot.task


def _operation_generation(config: Config, objective_id: str, action: PlannedAction) -> int:
    return sum(
        record.start is not None
        and record.intent.operation is action.kind
        and record.intent.subject_id == action.subject_id
        for record in list_objective_actions(config, objective_id)
    )


def _reservation(objective_id: str, action: PlannedAction) -> BudgetReservation:
    if action.kind is ActionKind.EXECUTE_TASK:
        return BudgetReservation(objective_id, action.subject_id, actions=1, attempts=1, failures=1, parallel=1)
    if action.kind is ActionKind.REVIEW_TASK:
        return BudgetReservation(objective_id, action.subject_id, actions=1, attempts=0, failures=1, parallel=1)
    raise DyroError("受监督执行只支持 execute_task 与 review_task")


def _authority_sha256(
    *,
    objective: object,
    snapshot: SchedulerSnapshot,
    tick: SchedulerTick,
    action: PlannedAction,
    operation_generation: int,
) -> str:
    # StoredObjective is intentionally duck-typed here so this module never
    # exposes storage implementation details through its public API.
    payload = {
        "schema_version": 1,
        "mode": RequestedMode.SUPERVISED.value,
        "objective_id": objective.objective.id,
        "objective_revision": objective.revision,
        "objective_event_seq": objective.event_seq,
        "objective_event_sha256": objective.event_sha256,
        "scope_sha256": objective.scope_sha256,
        "snapshot_sha256": snapshot.snapshot_sha256,
        "tick_sha256": tick.tick_sha256,
        "operation_generation": operation_generation,
        "action": _action_payload(action),
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _intent(
    *,
    objective: object,
    snapshot: SchedulerSnapshot,
    plan_sha256: str,
    tick: SchedulerTick,
    action: PlannedAction,
    generation: int,
    operation_generation: int,
    now: datetime,
) -> ActionIntent:
    authority_sha256 = _authority_sha256(
        objective=objective,
        snapshot=snapshot,
        tick=tick,
        action=action,
        operation_generation=operation_generation,
    )
    return ActionIntent(
        action_id=f"action-{authority_sha256[:40]}",
        objective_id=objective.objective.id,
        objective_revision=objective.revision,
        objective_event_seq=objective.event_seq,
        objective_event_sha256=objective.event_sha256,
        scope_sha256=objective.scope_sha256,
        snapshot_sha256=snapshot.snapshot_sha256,
        plan_sha256=plan_sha256,
        operation=action.kind,
        subject_id=action.subject_id,
        owner_generation=generation,
        expected_operation_generation=operation_generation,
        authority_sha256=authority_sha256,
        budget_reservation=_reservation(objective.objective.id, action),
        created_at=now,
    )


def _task_result_status(action: PlannedAction, result: object) -> tuple[ActionStatus, str]:
    if not isinstance(result, str):
        return ActionStatus.UNCERTAIN, "unrecognized_task_result"
    expected_success = {
        ActionKind.EXECUTE_TASK: frozenset({"review", "waiting_answer"}),
        ActionKind.REVIEW_TASK: frozenset({"done", "review_pending_signoff"}),
    }
    expected_failure = {
        ActionKind.EXECUTE_TASK: frozenset({"failed"}),
        ActionKind.REVIEW_TASK: frozenset({"review", "failed"}),
    }
    if result in expected_success[action.kind]:
        return ActionStatus.SUCCEEDED, result
    if result in expected_failure[action.kind]:
        return ActionStatus.FAILED, result
    return ActionStatus.UNCERTAIN, "unrecognized_task_result"


def _dispatch(config: Config, action: PlannedAction, task: Task, *, expected_contract_sha256: str) -> object:
    if action.kind is ActionKind.EXECUTE_TASK:
        return run_task(config, task, expected_contract_sha256=expected_contract_sha256)
    if action.kind is ActionKind.REVIEW_TASK:
        return review_task(config, task, expected_contract_sha256=expected_contract_sha256)
    raise DyroError("受监督执行只支持 execute_task 与 review_task")


def apply_supervised_wave(
    config: Config,
    wave: SupervisedWave,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> tuple[SupervisedOutcome, ...]:
    """Apply a user-confirmed wave serially through the durable Action Journal.

    A serial launcher is deliberate in the initial supervised stage: it keeps
    Task API compatibility while still enforcing the planner's resource and
    parallel bounds.  Future automatic execution needs a separate process
    barrier and must not change this explicit-confirmation path.
    """
    # Rebuild the whole semantic wave before writing an owner lease.  This
    # closes both the preview-to-apply race and a direct API caller trying to
    # provide only a safe-looking subset of actions.  The separate per-action
    # replan below still protects longer serial waves as preceding actions
    # change Task state.
    current = build_supervised_wave(config, wave.objective_id, clock=clock)
    if (
        current.confirmation_sha256 != wave.confirmation_sha256
        or current.actions != wave.actions
    ):
        raise DyroError("确认后的 wave 已发生语义变化；请重新运行 objective apply --dry-run")

    from ..host.doctor import assert_projections_allow_mutation

    assert_projections_allow_mutation(config)

    acquired_at = _utc(clock())
    grant = acquire_objective_owner_lease(
        config,
        wave.objective_id,
        now=acquired_at,
        ttl_seconds=SUPERVISION_LEASE_SECONDS,
        pid=os.getpid(),
        process_start=f"supervised-{os.getpid()}-{secrets.token_hex(8)}",
    )
    outcomes: list[SupervisedOutcome] = []
    primary_error: BaseException | None = None
    try:
        for expected in wave.actions:
            record, snapshot, tick, action, task = _current_action(
                config, wave.objective_id, expected, clock=clock
            )
            now = _utc(clock())
            operation_generation = _operation_generation(config, wave.objective_id, action)
            intent = _intent(
                objective=record,
                snapshot=snapshot,
                plan_sha256=tick.plan_sha256,
                tick=tick,
                action=action,
                generation=grant.lease.generation,
                operation_generation=operation_generation,
                now=now,
            )
            reserved = reserve_supervised_objective_action(
                config,
                wave.objective_id,
                intent=intent,
                grant=grant,
                now=now,
            )
            if reserved[0].receipt is not None:
                raise DyroError("Action idempotency key 已有终态 receipt；请重新规划")
            try:
                start_objective_action(
                    config,
                    wave.objective_id,
                    action_id=intent.action_id,
                    grant=grant,
                    now=_utc(clock()),
                )
            except BaseException as exc:
                # The normal failure path has not invoked a Task API or child
                # process.  Still inspect durable state first: an I/O failure
                # after a create-only start write is uncertain, never
                # cancellable.
                started = get_objective_action(config, wave.objective_id, intent.action_id).start is not None
                try:
                    record_objective_action_receipt(
                        config,
                        wave.objective_id,
                        receipt=ActionReceipt(
                            intent.action_id,
                            intent.idempotency_key,
                            intent.owner_generation,
                            ActionStatus.UNCERTAIN if started else ActionStatus.CANCELLED,
                            "action_start_write_uncertain" if started else "action_start_rejected_before_task_invocation",
                            _utc(clock()),
                        ),
                        grant=None if started else grant,
                        now=None if started else _utc(clock()),
                    )
                except Exception as receipt_exc:
                    exc.add_note(f"Action-start recovery receipt also failed: {receipt_exc}")
                    raise exc
                raise
            contract_sha256 = snapshot.tasks_by_id[action.subject_id].contract_sha256
            try:
                result = _dispatch(config, action, task, expected_contract_sha256=contract_sha256)
            except BaseException as exc:
                receipt = ActionReceipt(
                    intent.action_id,
                    intent.idempotency_key,
                    intent.owner_generation,
                    ActionStatus.UNCERTAIN,
                    "task_api_raised_after_action_start",
                    _utc(clock()),
                )
                try:
                    record_objective_action_receipt(config, wave.objective_id, receipt=receipt)
                except Exception as receipt_exc:
                    exc.add_note(f"uncertain Action receipt also failed: {receipt_exc}")
                    raise exc
                outcomes.append(SupervisedOutcome(intent.action_id, action.kind, action.subject_id, receipt.status, "uncertain"))
                if not isinstance(exc, Exception):
                    raise
                break
            receipt_status, rendered_result = _task_result_status(action, result)
            receipt = ActionReceipt(
                intent.action_id,
                intent.idempotency_key,
                intent.owner_generation,
                receipt_status,
                f"{action.kind.value}:{rendered_result}",
                _utc(clock()),
            )
            record_objective_action_receipt(config, wave.objective_id, receipt=receipt)
            outcomes.append(SupervisedOutcome(intent.action_id, action.kind, action.subject_id, receipt.status, rendered_result))
            if receipt_status is ActionStatus.UNCERTAIN:
                break
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        try:
            release_objective_owner_lease(
                config,
                wave.objective_id,
                grant=grant,
                now=_utc(clock()),
            )
        except Exception:
            # A lease-release failure is safe (the lease expires) but must not
            # hide an Action failure or turn it into a misleading success.
            if primary_error is None:
                raise
    return tuple(outcomes)


def render_supervised_outcomes(outcomes: tuple[SupervisedOutcome, ...]) -> str:
    return "\n".join(
        f"Action: {item.operation.value} {item.subject_id} -> {item.status.value} ({item.result})"
        for item in outcomes
    )


def supervised_outcomes_payload(outcomes: tuple[SupervisedOutcome, ...]) -> list[dict[str, str]]:
    """Return a JSON-safe terminal projection without Journal internals."""
    return [
        {
            "action_id": item.action_id,
            "operation": item.operation.value,
            "subject_id": item.subject_id,
            "status": item.status.value,
            "result": item.result,
        }
        for item in outcomes
    ]

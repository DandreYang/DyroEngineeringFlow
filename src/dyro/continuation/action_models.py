"""Immutable Action Journal records and their canonical validation rules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib

from ..canonical import canonical_json_bytes
from ..config import validate_id
from ..errors import ValidationError
from .budgets import BudgetReservation
from .models import ActionKind


ACTION_SCHEMA_VERSION = 1
MAX_RECEIPT_SUMMARY_LENGTH = 512
_DIGEST_LENGTH = 64
_MUTATING_OPERATIONS = frozenset({ActionKind.EXECUTE_TASK, ActionKind.REVIEW_TASK, ActionKind.MERGE_TASK})


class ActionStatus(str, Enum):
    RESERVED = "reserved"
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNCERTAIN = "uncertain"
    CANCELLED = "cancelled"


_TERMINAL_STATUSES = frozenset(
    {ActionStatus.SUCCEEDED, ActionStatus.FAILED, ActionStatus.UNCERTAIN, ActionStatus.CANCELLED}
)


def utc(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise TypeError(f"{label} 必须是带时区的 datetime")
    return value.astimezone(timezone.utc)


def timestamp(value: datetime) -> str:
    return utc(value, "timestamp").isoformat().replace("+00:00", "Z")


def parse_timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{label} 必须是带时区的 ISO-8601 时间")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{label} 必须是带时区的 ISO-8601 时间") from exc
    try:
        return utc(parsed, label)
    except TypeError as exc:
        raise ValidationError(f"{label} 必须是带时区的 ISO-8601 时间") from exc


def require_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != _DIGEST_LENGTH or any(char not in "0123456789abcdef" for char in value):
        raise ValidationError(f"{label} 必须是 SHA-256 十六进制摘要")
    return value


def safe_summary(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_RECEIPT_SUMMARY_LENGTH:
        raise ValidationError("Action receipt summary 无效")
    if any(ord(character) < 32 for character in value):
        raise ValidationError("Action receipt summary 不能包含控制字符")
    return value


def reservation_payload(reservation: BudgetReservation) -> dict[str, object]:
    return {
        "objective_id": reservation.objective_id,
        "task_id": reservation.task_id,
        "actions": reservation.actions,
        "attempts": reservation.attempts,
        "failures": reservation.failures,
        "parallel": reservation.parallel,
        "provider_usage": reservation.provider_usage,
    }


def reservation_from_payload(value: object) -> BudgetReservation:
    if not isinstance(value, dict) or set(value) != {
        "objective_id", "task_id", "actions", "attempts", "failures", "parallel", "provider_usage"
    }:
        raise ValidationError("Action budget_reservation 无效")
    try:
        return BudgetReservation(
            objective_id=value["objective_id"], task_id=value["task_id"], actions=value["actions"],
            attempts=value["attempts"], failures=value["failures"], parallel=value["parallel"],
            provider_usage=value["provider_usage"],
        )
    except (KeyError, TypeError) as exc:
        raise ValidationError("Action budget_reservation 无效") from exc


def action_idempotency_key(
    *, objective_id: str, objective_revision: int, objective_event_seq: int, objective_event_sha256: str,
    scope_sha256: str, snapshot_sha256: str, plan_sha256: str, operation: ActionKind, subject_id: str,
    owner_generation: int, expected_operation_generation: int, authority_sha256: str,
    budget_reservation: BudgetReservation,
) -> str:
    validate_id(objective_id, "Objective ID")
    validate_id(subject_id, "Action subject ID")
    if type(objective_revision) is not int or objective_revision < 1:
        raise TypeError("objective_revision 必须是正整数")
    if type(objective_event_seq) is not int or objective_event_seq < 1:
        raise TypeError("objective_event_seq 必须是正整数")
    if type(owner_generation) is not int or owner_generation < 1:
        raise TypeError("owner_generation 必须是正整数")
    if type(expected_operation_generation) is not int or expected_operation_generation < 0:
        raise TypeError("expected_operation_generation 必须是非负整数")
    if not isinstance(operation, ActionKind) or operation not in _MUTATING_OPERATIONS:
        raise TypeError("operation 必须是可变更的 ActionKind")
    for value, label in ((scope_sha256, "scope_sha256"), (objective_event_sha256, "objective_event_sha256"),
                         (snapshot_sha256, "snapshot_sha256"), (plan_sha256, "plan_sha256"),
                         (authority_sha256, "authority_sha256")):
        require_digest(value, label)
    if not isinstance(budget_reservation, BudgetReservation):
        raise TypeError("budget_reservation 必须是 BudgetReservation")
    if budget_reservation.objective_id != objective_id or budget_reservation.task_id != subject_id:
        raise ValidationError("budget_reservation 必须绑定同一 Objective 和 Task")
    payload = {
        "schema_version": ACTION_SCHEMA_VERSION, "objective_id": objective_id, "objective_revision": objective_revision,
        "objective_event_seq": objective_event_seq, "objective_event_sha256": objective_event_sha256,
        "scope_sha256": scope_sha256, "snapshot_sha256": snapshot_sha256, "plan_sha256": plan_sha256,
        "operation": operation.value, "subject_id": subject_id, "owner_generation": owner_generation,
        "expected_operation_generation": expected_operation_generation, "authority_sha256": authority_sha256,
        "budget_reservation": reservation_payload(budget_reservation),
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


@dataclass(frozen=True)
class ActionIntent:
    action_id: str
    objective_id: str
    objective_revision: int
    objective_event_seq: int
    objective_event_sha256: str
    scope_sha256: str
    snapshot_sha256: str
    plan_sha256: str
    operation: ActionKind
    subject_id: str
    owner_generation: int
    expected_operation_generation: int
    authority_sha256: str
    budget_reservation: BudgetReservation
    created_at: datetime
    idempotency_key: str = ""

    def __post_init__(self) -> None:
        validate_id(self.action_id, "Action ID")
        validate_id(self.objective_id, "Objective ID")
        validate_id(self.subject_id, "Action subject ID")
        if type(self.objective_revision) is not int or self.objective_revision < 1:
            raise TypeError("ActionIntent.objective_revision 必须是正整数")
        if type(self.objective_event_seq) is not int or self.objective_event_seq < 1:
            raise TypeError("ActionIntent.objective_event_seq 必须是正整数")
        if type(self.owner_generation) is not int or self.owner_generation < 1:
            raise TypeError("ActionIntent.owner_generation 必须是正整数")
        if type(self.expected_operation_generation) is not int or self.expected_operation_generation < 0:
            raise TypeError("ActionIntent.expected_operation_generation 必须是非负整数")
        if not isinstance(self.operation, ActionKind) or self.operation not in _MUTATING_OPERATIONS:
            raise TypeError("ActionIntent.operation 必须是可变更的 ActionKind")
        for value, label in ((self.scope_sha256, "ActionIntent.scope_sha256"),
                             (self.objective_event_sha256, "ActionIntent.objective_event_sha256"),
                             (self.snapshot_sha256, "ActionIntent.snapshot_sha256"),
                             (self.plan_sha256, "ActionIntent.plan_sha256"),
                             (self.authority_sha256, "ActionIntent.authority_sha256")):
            require_digest(value, label)
        if not isinstance(self.budget_reservation, BudgetReservation):
            raise TypeError("ActionIntent.budget_reservation 必须是 BudgetReservation")
        if self.budget_reservation.objective_id != self.objective_id or self.budget_reservation.task_id != self.subject_id:
            raise ValidationError("ActionIntent budget_reservation 必须绑定同一 Objective 和 Task")
        object.__setattr__(self, "created_at", utc(self.created_at, "ActionIntent.created_at"))
        expected = action_idempotency_key(
            objective_id=self.objective_id, objective_revision=self.objective_revision,
            objective_event_seq=self.objective_event_seq, objective_event_sha256=self.objective_event_sha256,
            scope_sha256=self.scope_sha256, snapshot_sha256=self.snapshot_sha256, plan_sha256=self.plan_sha256,
            operation=self.operation, subject_id=self.subject_id, owner_generation=self.owner_generation,
            expected_operation_generation=self.expected_operation_generation, authority_sha256=self.authority_sha256,
            budget_reservation=self.budget_reservation,
        )
        if self.idempotency_key and self.idempotency_key != expected:
            raise ValidationError("ActionIntent idempotency_key 与 authority binding 不匹配")
        object.__setattr__(self, "idempotency_key", expected)


@dataclass(frozen=True)
class ActionStart:
    action_id: str
    idempotency_key: str
    owner_generation: int
    owner_token_sha256: str
    started_at: datetime

    def __post_init__(self) -> None:
        validate_id(self.action_id, "Action ID")
        require_digest(self.idempotency_key, "ActionStart.idempotency_key")
        require_digest(self.owner_token_sha256, "ActionStart.owner_token_sha256")
        if type(self.owner_generation) is not int or self.owner_generation < 1:
            raise TypeError("ActionStart.owner_generation 必须是正整数")
        object.__setattr__(self, "started_at", utc(self.started_at, "ActionStart.started_at"))


@dataclass(frozen=True)
class ActionReceipt:
    action_id: str
    idempotency_key: str
    owner_generation: int
    status: ActionStatus
    summary: str
    recorded_at: datetime

    def __post_init__(self) -> None:
        validate_id(self.action_id, "Action ID")
        require_digest(self.idempotency_key, "ActionReceipt.idempotency_key")
        if type(self.owner_generation) is not int or self.owner_generation < 1:
            raise TypeError("ActionReceipt.owner_generation 必须是正整数")
        if not isinstance(self.status, ActionStatus) or self.status not in _TERMINAL_STATUSES:
            raise TypeError("ActionReceipt.status 必须是终态 ActionStatus")
        object.__setattr__(self, "summary", safe_summary(self.summary))
        object.__setattr__(self, "recorded_at", utc(self.recorded_at, "ActionReceipt.recorded_at"))


@dataclass(frozen=True)
class ActionRecord:
    intent: ActionIntent
    start: ActionStart | None
    receipt: ActionReceipt | None

    @property
    def status(self) -> ActionStatus:
        if self.receipt is not None:
            return self.receipt.status
        return ActionStatus.UNCERTAIN if self.start is not None else ActionStatus.RESERVED

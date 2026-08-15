"""Objective v1 contract parsing, validation, canonicalization, and hashing."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import tomllib
from typing import Any, Mapping

from ..canonical import canonical_json_bytes
from ..config import validate_id
from ..errors import ValidationError
from .models import BudgetLimit, CompletionRule, Objective, Operation, RequestedMode


OBJECTIVE_SCHEMA_VERSION = 1
DEFAULT_BUDGET = BudgetLimit(
    max_actions=20,
    max_attempts_per_task=2,
    max_failures=3,
    max_no_progress_cycles=2,
    max_parallel=3,
    deadline=None,
)
MAX_TITLE_LENGTH = 240
MAX_TARGETS = 512
MAX_BUDGET_VALUE = 1_000_000
ROOT_FIELDS = frozenset(
    {
        "schema_version",
        "id",
        "title",
        "line",
        "targets",
        "completion",
        "continuation",
        "budget",
    }
)
CONTINUATION_FIELDS = frozenset({"requested_mode", "operations"})
BUDGET_FIELDS = frozenset(
    {
        "max_actions",
        "max_attempts_per_task",
        "max_failures",
        "max_no_progress_cycles",
        "max_parallel",
        "deadline",
    }
)


def _require_table(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError(f"{label} 必须是表")
    return value


def _reject_unknown(raw: dict[str, Any], allowed: frozenset[str], label: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValidationError(f"{label} 包含未知字段：{', '.join(unknown)}")


def _required_string(raw: dict[str, Any], field: str, label: str) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label}.{field} 必须是非空字符串")
    return value.strip()


def _positive_int(raw: dict[str, Any], field: str, *, default: int) -> int:
    value = raw.get(field, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > MAX_BUDGET_VALUE:
        raise ValidationError(f"budget.{field} 必须是 1 到 {MAX_BUDGET_VALUE} 的有限整数")
    return value


def _deadline(raw: dict[str, Any]) -> datetime | None:
    value = raw.get("deadline")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("budget.deadline 必须是带时区的 ISO-8601 时间或省略")
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValidationError("budget.deadline 必须是带时区的 ISO-8601 时间") from exc
    if parsed.tzinfo is None:
        raise ValidationError("budget.deadline 必须包含时区")
    return parsed.astimezone(timezone.utc)


def _enum(enum_type: type[RequestedMode] | type[CompletionRule], value: str, label: str):
    try:
        return enum_type(value)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in enum_type)
        raise ValidationError(f"{label} 只能是：{allowed}") from exc


def _operations(raw: dict[str, Any]) -> tuple[Operation, ...]:
    value = raw.get("operations", [Operation.EXECUTE.value, Operation.REVIEW.value])
    if not isinstance(value, list) or not value:
        raise ValidationError("continuation.operations 必须是非空字符串数组")
    operations: list[Operation] = []
    for item in value:
        if not isinstance(item, str):
            raise ValidationError("continuation.operations 必须是字符串数组")
        try:
            operation = Operation(item)
        except ValueError as exc:
            allowed = ", ".join(candidate.value for candidate in Operation)
            raise ValidationError(f"continuation.operations 包含未知 operation；可选：{allowed}") from exc
        if operation in operations:
            raise ValidationError("continuation.operations 不能重复")
        operations.append(operation)
    return tuple(operations)


def validate_objective_scope(objective: Objective, task_lines: Mapping[str, str]) -> None:
    """Reject an Objective whose explicit targets are absent or cross its line.

    The caller supplies already-resolved Task-to-line facts, keeping this
    contract boundary pure. Persistent Objective creation and reconcile must
    pass the TaskGraph-derived mapping before accepting a contract.
    """
    missing = sorted(target for target in objective.targets if target not in task_lines)
    if missing:
        raise ValidationError(f"Objective.targets 缺少 TaskGraph line 事实：{', '.join(missing)}")
    cross_line = sorted(
        target
        for target in objective.targets
        if not isinstance(task_lines[target], str) or task_lines[target] != objective.line
    )
    if cross_line:
        raise ValidationError(f"Objective.targets 不能跨 line：{', '.join(cross_line)}")


def parse_contract(
    content: str | bytes,
    *,
    task_lines: Mapping[str, str] | None = None,
) -> Objective:
    """Parse one complete Objective v1 TOML document without touching disk."""
    try:
        raw = tomllib.loads(content.decode("utf-8") if isinstance(content, bytes) else content)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValidationError(f"Objective 契约 TOML 无效：{exc}") from exc
    if not isinstance(raw, dict):
        raise ValidationError("Objective 契约必须是 TOML 表")
    _reject_unknown(raw, ROOT_FIELDS, "Objective 契约")
    if type(raw.get("schema_version")) is not int or raw.get("schema_version") != OBJECTIVE_SCHEMA_VERSION:
        raise ValidationError(f"Objective 契约必须使用 schema_version = {OBJECTIVE_SCHEMA_VERSION}")
    objective_id = validate_id(_required_string(raw, "id", "Objective"), "Objective ID")
    title = _required_string(raw, "title", "Objective")
    if len(title) > MAX_TITLE_LENGTH:
        raise ValidationError(f"Objective.title 不能超过 {MAX_TITLE_LENGTH} 个字符")
    line = validate_id(_required_string(raw, "line", "Objective"), "Objective line")
    targets_raw = raw.get("targets")
    if not isinstance(targets_raw, list) or not targets_raw or len(targets_raw) > MAX_TARGETS:
        raise ValidationError(f"Objective.targets 必须是 1 到 {MAX_TARGETS} 个显式 Task ID 的数组")
    if not all(isinstance(item, str) for item in targets_raw):
        raise ValidationError("Objective.targets 必须是字符串数组")
    targets = tuple(validate_id(item, "Objective target") for item in targets_raw)
    if len(set(targets)) != len(targets):
        raise ValidationError("Objective.targets 不能重复")
    completion = _enum(
        CompletionRule,
        str(raw.get("completion", CompletionRule.ALL_TARGETS_INTEGRATED.value)),
        "Objective.completion",
    )
    continuation = _require_table(raw.get("continuation", {}), "continuation")
    _reject_unknown(continuation, CONTINUATION_FIELDS, "continuation")
    requested_mode = _enum(
        RequestedMode,
        str(continuation.get("requested_mode", RequestedMode.SUPERVISED.value)),
        "continuation.requested_mode",
    )
    operations = _operations(continuation)
    budget_raw = _require_table(raw.get("budget", {}), "budget")
    _reject_unknown(budget_raw, BUDGET_FIELDS, "budget")
    budget = BudgetLimit(
        max_actions=_positive_int(budget_raw, "max_actions", default=DEFAULT_BUDGET.max_actions),
        max_attempts_per_task=_positive_int(
            budget_raw,
            "max_attempts_per_task",
            default=DEFAULT_BUDGET.max_attempts_per_task,
        ),
        max_failures=_positive_int(budget_raw, "max_failures", default=DEFAULT_BUDGET.max_failures),
        max_no_progress_cycles=_positive_int(
            budget_raw,
            "max_no_progress_cycles",
            default=DEFAULT_BUDGET.max_no_progress_cycles,
        ),
        max_parallel=_positive_int(budget_raw, "max_parallel", default=DEFAULT_BUDGET.max_parallel),
        deadline=_deadline(budget_raw),
    )
    objective = Objective(
        schema_version=OBJECTIVE_SCHEMA_VERSION,
        id=objective_id,
        title=title,
        line=line,
        targets=targets,
        completion=completion,
        requested_mode=requested_mode,
        operations=operations,
        budget=budget,
    )
    if task_lines is not None:
        validate_objective_scope(objective, task_lines)
    return objective


def canonical_contract(objective: Objective) -> dict[str, object]:
    """Return a stable, JSON-compatible Objective contract projection."""
    budget: dict[str, object] = {
        "max_actions": objective.budget.max_actions,
        "max_attempts_per_task": objective.budget.max_attempts_per_task,
        "max_failures": objective.budget.max_failures,
        "max_no_progress_cycles": objective.budget.max_no_progress_cycles,
        "max_parallel": objective.budget.max_parallel,
    }
    if objective.budget.deadline is not None:
        budget["deadline"] = objective.budget.deadline.isoformat().replace("+00:00", "Z")
    return {
        "schema_version": objective.schema_version,
        "id": objective.id,
        "title": objective.title,
        "line": objective.line,
        "targets": sorted(objective.targets),
        "completion": objective.completion.value,
        "continuation": {
            "requested_mode": objective.requested_mode.value,
            "operations": sorted(operation.value for operation in objective.operations),
        },
        "budget": budget,
    }


def canonical_contract_bytes(objective: Objective) -> bytes:
    return canonical_json_bytes(canonical_contract(objective))


def contract_sha256(objective: Objective) -> str:
    return hashlib.sha256(canonical_contract_bytes(objective)).hexdigest()

"""Immutable, read-only workspace observations shared by presentation layers.

The Console must never obtain facts by parsing CLI text or by reimplementing
the scheduler.  This module is the Core-owned composition point: it samples
the existing task and Objective readers, performs no mutation, and returns
only immutable domain observations.  A presentation boundary must still apply
its own explicit field whitelist before exposing an observation to a browser.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from typing import Callable

from .canonical import canonical_json_bytes
from .config import Config
from .continuation.attention import build_attention_projection
from .continuation.planner import build_continuation_plan, build_scheduler_projection
from .continuation.snapshot import build_scheduler_snapshot
from .continuation.store import list_objectives
from .errors import DyroError, ValidationError
from .workspace import list_lines


READ_SNAPSHOT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ReadFailure:
    """A deliberately terse component failure suitable for a later DTO."""

    component: str
    code: str


@dataclass(frozen=True)
class WorkspaceLineObservation:
    id: str
    kind: str
    branch: str
    base: str
    repository_count: int


@dataclass(frozen=True)
class WorkspaceTaskObservation:
    id: str
    title: str
    line: str
    status: str
    risk: str
    depends_on: tuple[str, ...]
    blocked_on: tuple[str, ...]
    conflict_group: str
    executor: str
    reviewer: str
    integration_state: str
    external_claim_active: bool


@dataclass(frozen=True)
class ObjectiveActionObservation:
    kind: str
    subject_id: str
    reason: str


@dataclass(frozen=True)
class ObjectiveAttentionObservation:
    kind: str
    subject_id: str
    reason: str


@dataclass(frozen=True)
class WorkspaceObjectiveObservation:
    id: str
    title: str
    line: str
    revision: int
    operator_state: str
    derived_result: str
    requested_mode: str
    operations: tuple[str, ...]
    scope_count: int
    budget: tuple[tuple[str, int], ...]
    selected_actions: tuple[ObjectiveActionObservation, ...]
    blocked_actions: tuple[ObjectiveActionObservation, ...]
    attention: tuple[ObjectiveAttentionObservation, ...]
    contract_sha256: str
    scope_sha256: str
    event_sha256: str


@dataclass(frozen=True)
class WorkspaceReadSnapshot:
    """One immutable capture of Core facts for future Console endpoints.

    ``workspace_revision`` deliberately excludes ``observed_at``.  It changes
    when captured domain facts change, while a later HTTP ETag remains stable
    across otherwise identical polling captures.
    """

    schema_version: int
    workspace_name: str
    observed_at: datetime
    capture_id: str
    workspace_revision: str
    source_digests: tuple[tuple[str, str], ...]
    completeness: str
    lines: tuple[WorkspaceLineObservation, ...]
    tasks: tuple[WorkspaceTaskObservation, ...]
    objectives: tuple[WorkspaceObjectiveObservation, ...]
    failures: tuple[ReadFailure, ...] = ()


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValidationError("工作区读取时钟必须提供带时区的 datetime")
    return value.astimezone(timezone.utc)


def _task_observation(item: object) -> WorkspaceTaskObservation:
    # ``SchedulerTaskSnapshot`` is intentionally kept internal to the
    # scheduler.  This tiny adapter avoids leaking its Task.directory path.
    task = item.task  # type: ignore[attr-defined]
    return WorkspaceTaskObservation(
        id=task.id,
        title=task.title,
        line=task.line,
        status=item.status,  # type: ignore[attr-defined]
        risk=task.risk,
        depends_on=tuple(task.depends_on),
        blocked_on=tuple(task.blocked_on),
        conflict_group=task.conflict_group,
        executor=task.executor,
        reviewer=task.reviewer,
        integration_state=item.integration_state,  # type: ignore[attr-defined]
        external_claim_active=item.external_claim_active,  # type: ignore[attr-defined]
    )


def _objective_observation(config: Config, record: object, *, clock: Callable[[], datetime]) -> WorkspaceObjectiveObservation:
    snapshot = build_scheduler_snapshot(
        config,
        objective=record,
        clock=clock,
        inspect_integration=False,
    )
    plan = build_continuation_plan(snapshot)
    scheduler = build_scheduler_projection(snapshot, plan)
    attention = build_attention_projection(
        snapshot,
        plan,
        scheduler,
        budget=record.objective.budget,  # type: ignore[attr-defined]
    )
    objective = record.objective  # type: ignore[attr-defined]
    return WorkspaceObjectiveObservation(
        id=objective.id,
        title=objective.title,
        line=objective.line,
        revision=record.revision,  # type: ignore[attr-defined]
        operator_state=record.operator_state,  # type: ignore[attr-defined]
        derived_result=plan.completion.value,
        requested_mode=objective.requested_mode.value,
        operations=tuple(operation.value for operation in objective.operations),
        scope_count=len(record.scope),  # type: ignore[attr-defined]
        budget=(
            ("max_actions", objective.budget.max_actions),
            ("max_attempts_per_task", objective.budget.max_attempts_per_task),
            ("max_failures", objective.budget.max_failures),
            ("max_no_progress_cycles", objective.budget.max_no_progress_cycles),
            ("max_parallel", objective.budget.max_parallel),
        ),
        selected_actions=tuple(
            ObjectiveActionObservation(action.kind.value, action.subject_id, action.reason.value)
            for action in plan.selected_actions
        ),
        blocked_actions=tuple(
            ObjectiveActionObservation(action.kind.value, action.subject_id, action.reason.value)
            for action in plan.blocked
        ),
        attention=tuple(
            ObjectiveAttentionObservation(item.kind.value, item.subject_id, item.reason.value)
            for item in attention.items
        ),
        contract_sha256=record.contract_sha256,  # type: ignore[attr-defined]
        scope_sha256=record.scope_sha256,  # type: ignore[attr-defined]
        event_sha256=record.event_sha256,  # type: ignore[attr-defined]
    )


def _revision_payload(
    *,
    workspace_name: str,
    lines: tuple[WorkspaceLineObservation, ...],
    tasks: tuple[WorkspaceTaskObservation, ...],
    objectives: tuple[WorkspaceObjectiveObservation, ...],
    failures: tuple[ReadFailure, ...],
) -> dict[str, object]:
    return {
        "schema_version": READ_SNAPSHOT_SCHEMA_VERSION,
        "workspace_name": workspace_name,
        "lines": [
            {
                "id": item.id,
                "kind": item.kind,
                "branch": item.branch,
                "base": item.base,
                "repository_count": item.repository_count,
            }
            for item in lines
        ],
        "tasks": [
            {
                "id": item.id,
                "title": item.title,
                "line": item.line,
                "status": item.status,
                "risk": item.risk,
                "depends_on": list(item.depends_on),
                "blocked_on": list(item.blocked_on),
                "conflict_group": item.conflict_group,
                "executor": item.executor,
                "reviewer": item.reviewer,
                "integration_state": item.integration_state,
                "external_claim_active": item.external_claim_active,
            }
            for item in tasks
        ],
        "objectives": [
            {
                "id": item.id,
                "title": item.title,
                "line": item.line,
                "revision": item.revision,
                "operator_state": item.operator_state,
                "derived_result": item.derived_result,
                "requested_mode": item.requested_mode,
                "operations": list(item.operations),
                "scope_count": item.scope_count,
                "budget": dict(item.budget),
                "selected_actions": [action.__dict__ for action in item.selected_actions],
                "blocked_actions": [action.__dict__ for action in item.blocked_actions],
                "attention": [attention.__dict__ for attention in item.attention],
                "contract_sha256": item.contract_sha256,
                "scope_sha256": item.scope_sha256,
                "event_sha256": item.event_sha256,
            }
            for item in objectives
        ],
        "failures": [failure.__dict__ for failure in failures],
    }


def capture_workspace_read_snapshot(
    config: Config,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> WorkspaceReadSnapshot:
    """Capture Core facts without writing state, calling agents, or networking.

    Component failures remain scoped: a malformed Objective cannot erase task
    state from the Console.  We intentionally publish stable codes only; raw
    exception text can contain local paths or untrusted workspace content.
    """
    observed_at = _utc(clock())

    def sample_clock() -> datetime:
        return observed_at

    failures: list[ReadFailure] = []
    source_digests: list[tuple[str, str]] = []

    try:
        scheduler_snapshot = build_scheduler_snapshot(
            config,
            clock=sample_clock,
            inspect_integration=False,
        )
        tasks = tuple(_task_observation(item) for item in scheduler_snapshot.tasks)
        source_digests.append(("tasks", scheduler_snapshot.snapshot_sha256))
    except (DyroError, ValidationError, OSError, UnicodeError):
        tasks = ()
        failures.append(ReadFailure("tasks", "TASKS_UNAVAILABLE"))

    try:
        lines = tuple(
            WorkspaceLineObservation(
                id=line.id,
                kind=line.kind,
                branch=line.branch,
                base=line.base,
                repository_count=len(line.repositories),
            )
            for line in list_lines(config)
        )
        source_digests.append(
            (
                "lines",
                hashlib.sha256(
                    canonical_json_bytes(
                        [
                            {
                                "id": item.id,
                                "kind": item.kind,
                                "branch": item.branch,
                                "base": item.base,
                                "repository_count": item.repository_count,
                            }
                            for item in lines
                        ]
                    )
                ).hexdigest(),
            )
        )
    except (DyroError, ValidationError, OSError, UnicodeError):
        lines = ()
        failures.append(ReadFailure("lines", "LINES_UNAVAILABLE"))

    objectives: tuple[WorkspaceObjectiveObservation, ...]
    try:
        records = list_objectives(config, recover=False)
        objectives = tuple(
            _objective_observation(config, record, clock=sample_clock)
            for record in records
        )
        source_digests.extend(
            (f"objective:{item.id}", item.event_sha256) for item in objectives
        )
    except (DyroError, ValidationError, OSError, UnicodeError):
        objectives = ()
        failures.append(ReadFailure("objectives", "OBJECTIVES_UNAVAILABLE"))

    frozen_failures = tuple(sorted(failures, key=lambda item: (item.component, item.code)))
    payload = _revision_payload(
        workspace_name=config.name,
        lines=lines,
        tasks=tasks,
        objectives=objectives,
        failures=frozen_failures,
    )
    revision = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    frozen_sources = tuple(sorted(source_digests))
    return WorkspaceReadSnapshot(
        schema_version=READ_SNAPSHOT_SCHEMA_VERSION,
        workspace_name=config.name,
        observed_at=observed_at,
        capture_id=f"capture-{revision[:24]}",
        workspace_revision=revision,
        source_digests=frozen_sources,
        completeness="complete" if not frozen_failures else "partial",
        lines=lines,
        tasks=tasks,
        objectives=objectives,
        failures=frozen_failures,
    )

"""Typed, path-free Bridge observations. No CLI, recovery, or gate execution."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..config import Config
from ..continuation.resolution import (
    WorkspaceResolutionError,
    resolve_workspace_readonly,
)
from ..continuation.store import get_objective, list_objectives
from ..errors import DyroError, ValidationError
from ..hub import load_registry_bounded
from ..observations import capture_workspace_read_snapshot
from ..read_limits import ObservationLimits, ReadBudget
from ..tasks import list_tasks, load_task
from ..workspace import list_lines
from .identity import config_revision_v1, workspace_identity_v1

UNAVAILABLE = "OPERATION_UNAVAILABLE"


class BridgeObservationError(DyroError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


def default_read_budget() -> ReadBudget:
    return ReadBudget(ObservationLimits())


def _budget(budget: ReadBudget | None) -> ReadBudget:
    return budget if budget is not None else default_read_budget()


def _clock(clock: Callable[[], datetime] | None) -> Callable[[], datetime]:
    return clock if clock is not None else (lambda: datetime.now(timezone.utc))


def _resolve(*, start, workspace, cwd, budget):
    try:
        return resolve_workspace_readonly(
            start=start, workspace=workspace, cwd=cwd, budget=budget
        )
    except WorkspaceResolutionError as exc:
        raise BridgeObservationError(exc.code.value) from exc


def resolve_workspace_observation(
    *,
    start: str | Path | None,
    workspace: str | None,
    cwd: Path,
    budget: ReadBudget | None = None,
) -> dict[str, object]:
    resolved = _resolve(start=start, workspace=workspace, cwd=cwd, budget=_budget(budget))
    config = resolved.profile.config
    return {
        "workspace": {
            "id": workspace_identity_v1(
                canonical_root=resolved.profile.root, profile_name=config.name
            ),
            "name": config.name,
        },
        "resolution_source": resolved.source.value,
        "config_revision": config_revision_v1(resolved.profile.profile_bytes),
    }


def list_workspaces_observation(*, budget: ReadBudget | None = None) -> dict[str, object]:
    limits = _budget(budget)
    registry = load_registry_bounded(limits)
    items: list[dict[str, object]] = []
    failures: list[dict[str, str]] = []
    for record in registry.workspaces:
        try:
            profile = resolve_workspace_readonly(
                start=None,
                workspace=record.name,
                cwd=Path("/"),
                budget=limits,
            ).profile
            items.append(
                {
                    "alias": record.name,
                    "name": profile.config.name,
                    "status": "ok",
                    "is_default": record.name == registry.default,
                }
            )
        except WorkspaceResolutionError as exc:
            status = (
                "unreadable"
                if exc.code.value == "HOST_READ_PERMISSION_REQUIRED"
                else "stale"
            )
            items.append(
                {
                    "alias": record.name,
                    "name": record.name,
                    "status": status,
                    "is_default": record.name == registry.default,
                }
            )
            failures.append({"component": record.name, "code": exc.code.value})
    return {
        "partial": bool(failures),
        "workspaces": items,
        "failures": failures,
    }


def observe_workspace(
    *,
    start: str | Path | None,
    workspace: str | None,
    cwd: Path,
    budget: ReadBudget | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, object]:
    resolved = _resolve(start=start, workspace=workspace, cwd=cwd, budget=_budget(budget))
    snapshot = capture_workspace_read_snapshot(resolved.profile.config, clock=_clock(clock))
    return {
        "workspace": {
            "id": workspace_identity_v1(
                canonical_root=resolved.profile.root,
                profile_name=resolved.profile.config.name,
            ),
            "name": snapshot.workspace_name,
        },
        "resolution_source": resolved.source.value,
        "integration_inspection": "not_inspected",
        "proof_inspection": snapshot.proof_inspection,
        "completeness": snapshot.completeness,
        "lines": [
            {
                "id": item.id,
                "kind": item.kind,
                "branch": item.branch,
                "base": item.base,
                "repository_count": item.repository_count,
            }
            for item in snapshot.lines
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
                "integration_state": "not_inspected",
                "external_claim_active": item.external_claim_active,
            }
            for item in snapshot.tasks
        ],
        "objectives": [
            {
                "id": item.id,
                "title": item.title,
                "line": item.line,
                "revision": item.revision,
                "operator_state": item.operator_state,
                "requested_mode": item.requested_mode,
                "operations": list(item.operations),
                "scope_count": item.scope_count,
            }
            for item in snapshot.objectives
        ],
        "failures": [failure.__dict__ for failure in snapshot.failures],
    }


def list_lines_observation(config: Config) -> dict[str, object]:
    return {
        "lines": [
            {
                "id": line.id,
                "kind": line.kind,
                "branch": line.branch,
                "base": line.base,
                "repository_count": len(line.repositories),
            }
            for line in list_lines(config)
        ]
    }


def list_tasks_observation(config: Config) -> dict[str, object]:
    return {
        "integration_inspection": "not_inspected",
        "tasks": [
            {
                "id": task.id,
                "title": task.title,
                "line": task.line,
                "risk": task.risk,
                "executor": task.executor,
                "reviewer": task.reviewer,
                "depends_on": list(task.depends_on),
                "blocked_on": list(task.blocked_on),
                "conflict_group": task.conflict_group,
            }
            for task in list_tasks(config)
        ],
    }


def list_objectives_observation(config: Config) -> dict[str, object]:
    records = list_objectives(config, recover=False)
    return {
        "objectives": [
            {
                "id": record.objective.id,
                "title": record.objective.title,
                "line": record.objective.line,
                "revision": record.revision,
                "operator_state": record.operator_state,
                "requested_mode": record.objective.requested_mode.value,
            }
            for record in records
        ]
    }


def objective_status_observation(config: Config, objective_id: str) -> dict[str, object]:
    record = get_objective(config, objective_id, recover=False)
    return {
        "id": record.objective.id,
        "title": record.objective.title,
        "line": record.objective.line,
        "revision": record.revision,
        "operator_state": record.operator_state,
        "requested_mode": record.objective.requested_mode.value,
        "integration_inspection": "not_inspected",
        "ready": None,
        "blocked": None,
    }


def gate_definitions(config: Config, task_id: str) -> dict[str, object]:
    try:
        task = load_task(config, task_id)
    except (DyroError, ValidationError, OSError) as exc:
        raise BridgeObservationError("TASK_NOT_FOUND") from exc
    return {
        "task_id": task.id,
        "gates": [
            {"name": gate.name, "timeout_seconds": gate.timeout_seconds}
            for gate in task.gates
        ],
    }


def explain_task(_config: Config, _task_id: str) -> None:
    unavailable_git_observation("task.explain")


def task_graph(_config: Config, _task_id: str) -> None:
    unavailable_git_observation("task.graph")


def unavailable_git_observation(operation: str) -> None:
    raise BridgeObservationError(
        UNAVAILABLE,
        f"{operation} 需要已审查的 Git 观察适配器",
    )

"""Typed, bounded, read-only Core observations for Agent Bridge Phase 0."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import stat
import time
from typing import Callable, Literal

from ..canonical import canonical_json_bytes
from ..config import Config, LoadedProfile, load_profile_exact, validate_id
from ..continuation.resolution import (
    ResolvedWorkspace,
    WorkspaceResolutionError,
    WorkspaceResolutionSource,
    resolve_workspace_readonly,
)
from ..continuation.store import get_objective
from ..errors import DyroError, ValidationError
from ..hub import WorkspaceRecord, load_registry_bounded
from ..read_limits import (
    ObservationLimits,
    ReadBudget,
    ReadLimitCode,
    ReadLimitError,
)
from ..tasks import load_task_bounded, load_task_observation_bounded
from ..workspace import load_line_bounded
from .models import (
    BridgeError,
    BridgeNextAction,
    ErrorCode,
    NextActionKind,
    config_revision,
    workspace_identity,
)


IntegrationInspection = Literal["complete", "not_inspected", "partial"]


_ERROR_PRESENTATION = {
    ErrorCode.SCHEMA_VALIDATION_FAILED: "The observation input is invalid.",
    ErrorCode.LOCAL_PROFILE_INVALID: "The local Dyro Profile is invalid.",
    ErrorCode.REGISTRY_INVALID: "The workspace registry is invalid.",
    ErrorCode.WORKSPACE_NOT_REGISTERED: "The selected workspace is not registered.",
    ErrorCode.REGISTERED_ROOT_STALE: "The registered workspace root is unavailable.",
    ErrorCode.HOST_READ_PERMISSION_REQUIRED: "Host read permission is required.",
    ErrorCode.AMBIGUOUS_WORKSPACE: "Multiple workspaces require an explicit selection.",
    ErrorCode.WORKSPACE_NOT_FOUND: "No usable Dyro workspace was found.",
    ErrorCode.RESOURCE_LIMIT_EXCEEDED: "The observation resource limit was exceeded.",
    ErrorCode.OBSERVATION_DEADLINE_EXCEEDED: "The observation deadline was exceeded.",
    ErrorCode.OBSERVATION_PARTIAL: "The required observation is partial.",
    ErrorCode.RECORD_INVALID: "The requested workspace record is invalid.",
    ErrorCode.INTERNAL_ERROR: "The observation failed.",
}


class BridgeObservationError(DyroError):
    def __init__(self, error: BridgeError) -> None:
        super().__init__(error.code.value)
        self.error = error


def _observation_tuple(
    value: object,
    item_type: type,
    label: str,
    *,
    maximum: int = 100,
) -> None:
    if not isinstance(value, tuple):
        raise ValidationError(f"{label} must be an immutable tuple")
    if len(value) > maximum or not all(isinstance(item, item_type) for item in value):
        raise ValidationError(f"{label} contains invalid items")


def _observation_text(value: object, label: str, *, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum:
        raise ValidationError(f"{label} must be bounded non-empty text")
    return value


@dataclass(frozen=True)
class WorkspaceRef:
    id: str
    name: str
    profile_schema_version: int = 1

    def __post_init__(self) -> None:
        if (
            not isinstance(self.id, str)
            or not self.id.startswith("workspace:")
            or len(self.id) != len("workspace:") + 64
            or any(character not in "0123456789abcdef" for character in self.id[10:])
        ):
            raise ValidationError("workspace identity is invalid")
        validate_id(self.name, "workspace name")
        if self.profile_schema_version != 1:
            raise ValidationError("profile schema version must be 1")

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "profile_schema_version": self.profile_schema_version,
        }


@dataclass(frozen=True)
class WorkspaceResolutionObservation:
    workspace: WorkspaceRef
    resolution_source: str
    health: str = "available"

    def __post_init__(self) -> None:
        if not isinstance(self.workspace, WorkspaceRef):
            raise ValidationError("resolution workspace is invalid")
        if self.resolution_source not in {"explicit", "local", "default", "unique"}:
            raise ValidationError("resolution source is invalid")
        if self.health != "available":
            raise ValidationError("resolved workspace health must be available")

    def as_dict(self) -> dict[str, object]:
        return {
            "workspace": self.workspace.as_dict(),
            "resolution_source": self.resolution_source,
            "health": self.health,
        }


@dataclass(frozen=True)
class WorkspaceListItem:
    registry_alias: str
    health: str
    default: bool
    failure_code: str | None
    workspace: WorkspaceRef | None = None

    def __post_init__(self) -> None:
        validate_id(self.registry_alias, "registry alias")
        if not isinstance(self.default, bool):
            raise ValidationError("workspace default marker must be boolean")
        if self.health == "available":
            if (
                not isinstance(self.workspace, WorkspaceRef)
                or self.failure_code is not None
            ):
                raise ValidationError("available workspace list item is inconsistent")
        elif self.health == "unavailable":
            if self.workspace is not None or self.failure_code not in {
                item.value for item in ErrorCode
            }:
                raise ValidationError("unavailable workspace list item is inconsistent")
        else:
            raise ValidationError("workspace list health is invalid")

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "registry_alias": self.registry_alias,
            "health": self.health,
            "default": self.default,
            "failure_code": self.failure_code,
        }
        if self.workspace is not None:
            result["workspace"] = self.workspace.as_dict()
        return result


@dataclass(frozen=True)
class WorkspaceListObservation:
    workspaces: tuple[WorkspaceListItem, ...]
    partial: bool = False
    truncated: bool = False

    def __post_init__(self) -> None:
        _observation_tuple(self.workspaces, WorkspaceListItem, "workspaces")
        if not isinstance(self.partial, bool) or not isinstance(self.truncated, bool):
            raise ValidationError("workspace list markers must be boolean")

    def as_dict(self) -> dict[str, object]:
        return {"workspaces": [item.as_dict() for item in self.workspaces]}


@dataclass(frozen=True)
class ObservedItem:
    id: str
    status: str | None
    integration_inspection: IntegrationInspection = "not_inspected"

    def __post_init__(self) -> None:
        validate_id(self.id, "observed record ID")
        if self.status is not None:
            _observation_text(self.status, "observed status", maximum=80)
        if self.integration_inspection not in {
            "complete",
            "not_inspected",
            "partial",
        }:
            raise ValidationError("integration inspection state is invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "status": self.status,
            "integration_inspection": self.integration_inspection,
        }


@dataclass(frozen=True)
class ObservationFailure:
    component: str
    code: str

    def __post_init__(self) -> None:
        _observation_text(self.component, "failure component", maximum=128)
        if self.code not in {item.value for item in ErrorCode}:
            raise ValidationError("observation failure code is invalid")

    def as_dict(self) -> dict[str, str]:
        return {"component": self.component, "code": self.code}


@dataclass(frozen=True)
class WorkspaceObservation:
    workspace: WorkspaceRef
    observed_at: str
    capture_id: str
    workspace_revision: str
    completeness: str
    integration_inspection: IntegrationInspection
    lines: tuple[ObservedItem, ...]
    tasks: tuple[ObservedItem, ...]
    objectives: tuple[ObservedItem, ...]
    failures: tuple[ObservationFailure, ...]
    truncated: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.workspace, WorkspaceRef):
            raise ValidationError("observation workspace is invalid")
        _observation_text(self.observed_at, "observed_at", maximum=64)
        _observation_text(self.capture_id, "capture_id", maximum=80)
        if (
            not isinstance(self.workspace_revision, str)
            or not self.workspace_revision.startswith("sha256:")
            or len(self.workspace_revision) != 71
            or any(
                character not in "0123456789abcdef"
                for character in self.workspace_revision[7:]
            )
        ):
            raise ValidationError("workspace_revision must be a SHA-256 digest")
        if self.completeness not in {"complete", "partial"}:
            raise ValidationError("observation completeness is invalid")
        if self.integration_inspection not in {
            "complete",
            "not_inspected",
            "partial",
        }:
            raise ValidationError("integration inspection state is invalid")
        _observation_tuple(self.lines, ObservedItem, "lines")
        _observation_tuple(self.tasks, ObservedItem, "tasks")
        _observation_tuple(self.objectives, ObservedItem, "objectives")
        _observation_tuple(self.failures, ObservationFailure, "failures")
        if not isinstance(self.truncated, bool):
            raise ValidationError("observation truncated marker must be boolean")
        if self.completeness == "complete" and (self.failures or self.truncated):
            raise ValidationError("complete observation cannot contain partial markers")

    @property
    def partial(self) -> bool:
        return self.completeness == "partial"

    def failure_pairs(self) -> set[tuple[str, str]]:
        return {(item.component, item.code) for item in self.failures}

    def as_dict(self) -> dict[str, object]:
        return {
            "workspace": self.workspace.as_dict(),
            "observed_at": self.observed_at,
            "capture_id": self.capture_id,
            "workspace_revision": self.workspace_revision,
            "completeness": self.completeness,
            "integration_inspection": self.integration_inspection,
            "lines": [item.as_dict() for item in self.lines],
            "tasks": [item.as_dict() for item in self.tasks],
            "objectives": [item.as_dict() for item in self.objectives],
            "failures": [item.as_dict() for item in self.failures],
        }


@dataclass(frozen=True)
class GateDefinition:
    name: str
    required: bool = True
    description: str | None = None

    def __post_init__(self) -> None:
        validate_id(self.name, "gate name")
        if not isinstance(self.required, bool):
            raise ValidationError("gate required marker must be boolean")
        if self.description is not None:
            _observation_text(self.description, "gate description", maximum=512)

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "required": self.required,
            "description": self.description,
        }


@dataclass(frozen=True)
class GateDefinitionsObservation:
    task_id: str
    gates: tuple[GateDefinition, ...]

    def __post_init__(self) -> None:
        validate_id(self.task_id, "task ID")
        _observation_tuple(self.gates, GateDefinition, "gates", maximum=64)

    def as_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "gates": [gate.as_dict() for gate in self.gates],
        }


@dataclass(frozen=True)
class _EntryScan:
    names: tuple[str, ...]
    invalid_entries: int
    truncated: bool


def _bridge_error(code: ErrorCode) -> BridgeObservationError:
    actions: tuple[BridgeNextAction, ...] = ()
    if code is ErrorCode.LOCAL_PROFILE_INVALID:
        actions = (
            BridgeNextAction(
                NextActionKind.INSPECT_PROFILE, "Inspect the local Profile"
            ),
        )
    elif code is ErrorCode.AMBIGUOUS_WORKSPACE:
        actions = (
            BridgeNextAction(NextActionKind.SELECT_WORKSPACE, "Select one workspace"),
        )
    elif code in {ErrorCode.REGISTRY_INVALID, ErrorCode.REGISTERED_ROOT_STALE}:
        actions = (
            BridgeNextAction(
                NextActionKind.CHECK_REGISTRY, "Inspect the workspace registry"
            ),
        )
    elif code is ErrorCode.HOST_READ_PERMISSION_REQUIRED:
        actions = (
            BridgeNextAction(NextActionKind.GRANT_HOST_READ, "Grant host read access"),
        )
    return BridgeObservationError(
        BridgeError(code=code, message=_ERROR_PRESENTATION[code], next_actions=actions)
    )


def _map_resolution_error(error: WorkspaceResolutionError) -> BridgeObservationError:
    return _bridge_error(ErrorCode(error.code.value))


def _map_limit_error(error: ReadLimitError) -> BridgeObservationError:
    if error.code is ReadLimitCode.DEADLINE_EXCEEDED:
        return _bridge_error(ErrorCode.OBSERVATION_DEADLINE_EXCEEDED)
    return _bridge_error(ErrorCode.RESOURCE_LIMIT_EXCEEDED)


def _workspace_ref(profile: LoadedProfile) -> WorkspaceRef:
    return WorkspaceRef(
        id=workspace_identity(profile.root, profile.config.name),
        name=profile.config.name,
    )


def _new_budget(
    limits: ObservationLimits | None,
    monotonic: Callable[[], float],
) -> ReadBudget:
    return ReadBudget(limits or ObservationLimits(), monotonic=monotonic)


def _resolve(
    *,
    workspace: str | None,
    start: str | Path | None,
    cwd: Path,
    budget: ReadBudget,
) -> ResolvedWorkspace:
    try:
        if workspace is not None and not isinstance(workspace, str):
            raise ValidationError("workspace selector must be a string")
        if start is not None and not isinstance(start, (str, Path)):
            raise ValidationError("Bridge start must be a string or Path")
        if not isinstance(cwd, Path):
            raise ValidationError("Bridge cwd must be a Path")
        if workspace is not None:
            validate_id(workspace, "workspace selector")
        if start is not None and len(str(start).encode("utf-8")) > 4096:
            raise ValidationError("Bridge start 路径超过 4096 字节上限")
        return resolve_workspace_readonly(
            workspace=workspace,
            start=start,
            cwd=cwd,
            budget=budget,
        )
    except WorkspaceResolutionError as exc:
        raise _map_resolution_error(exc) from None
    except ReadLimitError as exc:
        raise _map_limit_error(exc) from None
    except ValidationError:
        raise _bridge_error(ErrorCode.SCHEMA_VALIDATION_FAILED) from None
    except PermissionError:
        raise _bridge_error(ErrorCode.HOST_READ_PERMISSION_REQUIRED) from None
    except OSError:
        raise _bridge_error(ErrorCode.INTERNAL_ERROR) from None


def resolve_workspace_observation(
    *,
    workspace: str | None,
    start: str | Path | None,
    cwd: Path,
    limits: ObservationLimits | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> WorkspaceResolutionObservation:
    budget = _new_budget(limits, monotonic)
    resolved = _resolve(workspace=workspace, start=start, cwd=cwd, budget=budget)
    try:
        budget.check_root_identity(resolved.profile.root)
        result = WorkspaceResolutionObservation(
            workspace=_workspace_ref(resolved.profile),
            resolution_source=resolved.source.value,
        )
        budget.check_deadline()
    except ReadLimitError as exc:
        if exc.code is ReadLimitCode.UNSAFE_FILE:
            code = (
                ErrorCode.LOCAL_PROFILE_INVALID
                if resolved.source is WorkspaceResolutionSource.LOCAL
                else ErrorCode.REGISTERED_ROOT_STALE
            )
            raise _bridge_error(code) from None
        raise _map_limit_error(exc) from None
    except PermissionError:
        raise _bridge_error(ErrorCode.HOST_READ_PERMISSION_REQUIRED) from None
    except (OSError, ValidationError):
        code = (
            ErrorCode.LOCAL_PROFILE_INVALID
            if resolved.source is WorkspaceResolutionSource.LOCAL
            else ErrorCode.REGISTERED_ROOT_STALE
        )
        raise _bridge_error(code) from None
    return result


def _load_registry_for_bridge(budget: ReadBudget):
    try:
        return load_registry_bounded(budget)
    except ReadLimitError as exc:
        if exc.code is ReadLimitCode.UNSAFE_FILE:
            raise _bridge_error(ErrorCode.REGISTRY_INVALID) from None
        raise _map_limit_error(exc) from None
    except PermissionError:
        raise _bridge_error(ErrorCode.HOST_READ_PERMISSION_REQUIRED) from None
    except (OSError, ValidationError):
        raise _bridge_error(ErrorCode.REGISTRY_INVALID) from None


def _load_registered_for_list(
    record: WorkspaceRecord, budget: ReadBudget
) -> LoadedProfile | ErrorCode:
    try:
        return load_profile_exact(record.root, budget)
    except ReadLimitError as exc:
        if exc.code is ReadLimitCode.UNSAFE_FILE:
            return ErrorCode.REGISTERED_ROOT_STALE
        if exc.code is ReadLimitCode.DEADLINE_EXCEEDED:
            return ErrorCode.OBSERVATION_DEADLINE_EXCEEDED
        return ErrorCode.RESOURCE_LIMIT_EXCEEDED
    except PermissionError:
        return ErrorCode.HOST_READ_PERMISSION_REQUIRED
    except (OSError, ValidationError):
        return ErrorCode.REGISTERED_ROOT_STALE


def list_workspace_observations(
    *,
    limits: ObservationLimits | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> WorkspaceListObservation:
    budget = _new_budget(limits, monotonic)
    registry = _load_registry_for_bridge(budget)
    maximum = min(budget.limits.registry_records, budget.limits.response_records)
    selected = registry.workspaces[:maximum]
    truncated = len(registry.workspaces) > maximum
    items: list[WorkspaceListItem] = []
    partial = truncated
    for record in selected:
        loaded = _load_registered_for_list(record, budget)
        if isinstance(loaded, ErrorCode):
            partial = True
            items.append(
                WorkspaceListItem(
                    registry_alias=record.name,
                    health="unavailable",
                    default=record.name == registry.default,
                    failure_code=loaded.value,
                )
            )
            continue
        try:
            budget.check_root_identity(loaded.root)
            workspace_ref = _workspace_ref(loaded)
        except ReadLimitError as exc:
            partial = True
            code = (
                ErrorCode.REGISTERED_ROOT_STALE
                if exc.code is ReadLimitCode.UNSAFE_FILE
                else ErrorCode.OBSERVATION_DEADLINE_EXCEEDED
                if exc.code is ReadLimitCode.DEADLINE_EXCEEDED
                else ErrorCode.RESOURCE_LIMIT_EXCEEDED
            )
            items.append(
                WorkspaceListItem(
                    registry_alias=record.name,
                    health="unavailable",
                    default=record.name == registry.default,
                    failure_code=code.value,
                )
            )
            continue
        except PermissionError:
            partial = True
            items.append(
                WorkspaceListItem(
                    registry_alias=record.name,
                    health="unavailable",
                    default=record.name == registry.default,
                    failure_code=ErrorCode.HOST_READ_PERMISSION_REQUIRED.value,
                )
            )
            continue
        except ValidationError:
            partial = True
            items.append(
                WorkspaceListItem(
                    registry_alias=record.name,
                    health="unavailable",
                    default=record.name == registry.default,
                    failure_code=ErrorCode.REGISTERED_ROOT_STALE.value,
                )
            )
            continue
        items.append(
            WorkspaceListItem(
                registry_alias=record.name,
                workspace=workspace_ref,
                health="available",
                default=record.name == registry.default,
                failure_code=None,
            )
        )
    result = WorkspaceListObservation(
        tuple(items), partial=partial, truncated=truncated
    )
    if not any(
        item.failure_code == ErrorCode.OBSERVATION_DEADLINE_EXCEEDED.value
        for item in items
    ):
        try:
            budget.check_deadline()
        except ReadLimitError as exc:
            raise _map_limit_error(exc) from None
    return result


def _scan_records(
    parent: Path,
    *,
    workspace_root: Path,
    maximum: int,
    directories: bool,
    suffix: str = "",
    budget: ReadBudget,
) -> _EntryScan:
    budget.check_deadline()
    with budget.open_safe_directory_chain(
        workspace_root, parent, allow_missing=True
    ) as parent_fd:
        if parent_fd is None:
            return _EntryScan((), 0, False)
        invalid = 0
        enumerated = 0
        names: list[str] = []
        with os.scandir(parent_fd) as entries:
            for entry in entries:
                budget.check_deadline()
                enumerated += 1
                if enumerated > maximum:
                    # Overflow is normalized independently of filesystem order.
                    return _EntryScan((), 0, True)
                if entry.name.startswith("."):
                    continue
                if suffix and not entry.name.endswith(suffix):
                    continue
                record_name = entry.name[: -len(suffix)] if suffix else entry.name
                try:
                    validate_id(record_name, "record ID")
                except ValidationError:
                    invalid += 1
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    invalid += 1
                    continue
                correct_kind = (
                    stat.S_ISDIR(info.st_mode)
                    if directories
                    else stat.S_ISREG(info.st_mode)
                )
                if not correct_kind:
                    invalid += 1
                    continue
                identity = (info.st_dev, info.st_ino)
                record_path = parent / entry.name
                if directories:
                    budget.bind_directory_identity(record_path, identity)
                else:
                    budget.bind_file_identity(record_path, identity)
                names.append(record_name)
        return _EntryScan(tuple(sorted(names)), invalid, False)


def _record_failure(component: str, error: BaseException) -> ObservationFailure:
    if isinstance(error, ReadLimitError):
        if error.code is ReadLimitCode.DEADLINE_EXCEEDED:
            code = ErrorCode.OBSERVATION_DEADLINE_EXCEEDED
        elif error.code is ReadLimitCode.UNSAFE_FILE:
            code = ErrorCode.RECORD_INVALID
        else:
            code = ErrorCode.RESOURCE_LIMIT_EXCEEDED
    elif isinstance(error, PermissionError):
        code = ErrorCode.HOST_READ_PERMISSION_REQUIRED
    else:
        code = ErrorCode.RECORD_INVALID
    return ObservationFailure(component, code.value)


def _read_lines(
    config: Config, budget: ReadBudget
) -> tuple[
    tuple[ObservedItem, ...], frozenset[str], tuple[ObservationFailure, ...], bool
]:
    items: list[ObservedItem] = []
    known_line_ids: set[str] = set()
    failures: list[ObservationFailure] = []
    truncated = False
    remaining = budget.limits.line_records
    for kind, parent in (
        ("line", config.lines_state_dir),
        ("hotfix", config.hotfixes_state_dir),
    ):
        try:
            scan = _scan_records(
                parent,
                workspace_root=config.root,
                maximum=max(remaining, 0),
                directories=False,
                suffix=".toml",
                budget=budget,
            )
        except (DyroError, OSError, UnicodeError) as exc:
            failures.append(_record_failure("lines", exc))
            truncated = True
            continue
        if scan.invalid_entries:
            failures.append(ObservationFailure("lines", ErrorCode.RECORD_INVALID.value))
        if scan.truncated:
            truncated = True
            failures.append(
                ObservationFailure("lines", ErrorCode.RESOURCE_LIMIT_EXCEEDED.value)
            )
            if not scan.names:
                break
        for line_id in scan.names:
            try:
                line = load_line_bounded(
                    parent / f"{line_id}.toml",
                    budget,
                    workspace_root=config.root,
                )
                if line.kind != kind:
                    raise ValidationError("line kind does not match state root")
                known_line_ids.add(line.id)
                if len(items) < budget.limits.response_records:
                    items.append(ObservedItem(line.id, None))
                else:
                    truncated = True
            except (DyroError, OSError, UnicodeError) as exc:
                failures.append(_record_failure(f"line:{line_id}", exc))
        remaining -= len(scan.names)
    frozen = tuple(sorted(items, key=lambda item: item.id))
    return frozen, frozenset(known_line_ids), tuple(failures), truncated


def _read_tasks(
    config: Config, budget: ReadBudget, known_line_ids: frozenset[str]
) -> tuple[tuple[ObservedItem, ...], tuple[ObservationFailure, ...], bool]:
    try:
        scan = _scan_records(
            config.task_specs_dir,
            workspace_root=config.root,
            maximum=budget.limits.task_records,
            directories=True,
            budget=budget,
        )
    except (DyroError, OSError, UnicodeError) as exc:
        return (), (_record_failure("tasks", exc),), True
    failures: list[ObservationFailure] = []
    if scan.invalid_entries:
        failures.append(ObservationFailure("tasks", ErrorCode.RECORD_INVALID.value))
    if scan.truncated:
        failures.append(
            ObservationFailure("tasks", ErrorCode.RESOURCE_LIMIT_EXCEEDED.value)
        )
    items: list[ObservedItem] = []
    output_truncated = False
    for task_id in scan.names:
        try:
            task, task_status = load_task_observation_bounded(
                config,
                task_id,
                budget,
                known_line_ids=known_line_ids,
            )
            observed = ObservedItem(task.id, task_status)
            if len(items) < budget.limits.response_records:
                items.append(observed)
            else:
                output_truncated = True
        except (DyroError, OSError, UnicodeError) as exc:
            failures.append(_record_failure(f"task:{task_id}", exc))
    return tuple(items), tuple(failures), scan.truncated or output_truncated


def _read_objectives(
    config: Config, budget: ReadBudget
) -> tuple[tuple[ObservedItem, ...], tuple[ObservationFailure, ...], bool]:
    try:
        scan = _scan_records(
            config.objectives_dir,
            workspace_root=config.root,
            maximum=budget.limits.objective_records,
            directories=True,
            budget=budget,
        )
    except (DyroError, OSError, UnicodeError) as exc:
        return (), (_record_failure("objectives", exc),), True
    failures: list[ObservationFailure] = []
    if scan.invalid_entries:
        failures.append(
            ObservationFailure("objectives", ErrorCode.RECORD_INVALID.value)
        )
    if scan.truncated:
        failures.append(
            ObservationFailure("objectives", ErrorCode.RESOURCE_LIMIT_EXCEEDED.value)
        )
    items: list[ObservedItem] = []
    output_truncated = False
    for objective_id in scan.names:
        try:
            record = get_objective(
                config,
                objective_id,
                recover=False,
                read_budget=budget,
            )
            observed = ObservedItem(record.objective.id, record.operator_state)
            if len(items) < budget.limits.response_records:
                items.append(observed)
            else:
                output_truncated = True
        except (DyroError, OSError, UnicodeError) as exc:
            failures.append(_record_failure(f"objective:{objective_id}", exc))
    return tuple(items), tuple(failures), scan.truncated or output_truncated


def _observed_at(clock: Callable[[], datetime]) -> str:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValidationError("Bridge observation clock 必须返回带时区 datetime")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _capture_workspace(
    profile: LoadedProfile,
    budget: ReadBudget,
    *,
    clock: Callable[[], datetime],
) -> WorkspaceObservation:
    lines, line_ids, line_failures, lines_truncated = _read_lines(
        profile.config, budget
    )
    tasks, task_failures, tasks_truncated = _read_tasks(
        profile.config, budget, line_ids
    )
    objectives, objective_failures, objectives_truncated = _read_objectives(
        profile.config, budget
    )
    ordered_failures = sorted(
        set((*line_failures, *task_failures, *objective_failures)),
        key=lambda item: (item.component, item.code),
    )
    failure_truncated = len(ordered_failures) > budget.limits.response_records
    if failure_truncated:
        ordered_failures = [
            *ordered_failures[: budget.limits.response_records - 1],
            ObservationFailure("observation", ErrorCode.RESOURCE_LIMIT_EXCEEDED.value),
        ]
    failures = tuple(ordered_failures)
    truncated = lines_truncated or tasks_truncated or objectives_truncated
    truncated = truncated or failure_truncated
    workspace = _workspace_ref(profile)
    revision_payload = {
        "workspace": workspace.as_dict(),
        "config_revision": config_revision(profile.profile_bytes),
        "integration_inspection": "not_inspected",
        "lines": [item.as_dict() for item in lines],
        "tasks": [item.as_dict() for item in tasks],
        "objectives": [item.as_dict() for item in objectives],
        "failures": [item.as_dict() for item in failures],
        "truncated": truncated,
    }
    revision = (
        "sha256:" + hashlib.sha256(canonical_json_bytes(revision_payload)).hexdigest()
    )
    observed_at = _observed_at(clock)
    if not any(
        failure.code == ErrorCode.OBSERVATION_DEADLINE_EXCEEDED.value
        for failure in failures
    ):
        budget.check_root_identity(profile.root)
        budget.check_deadline()
    return WorkspaceObservation(
        workspace=workspace,
        observed_at=observed_at,
        capture_id=f"capture-{revision[7:31]}",
        workspace_revision=revision,
        completeness="partial" if failures or truncated else "complete",
        integration_inspection="not_inspected",
        lines=lines,
        tasks=tasks,
        objectives=objectives,
        failures=failures,
        truncated=truncated,
    )


def observe_workspace(
    *,
    workspace: str | None,
    start: str | Path | None,
    cwd: Path,
    limits: ObservationLimits | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    monotonic: Callable[[], float] = time.monotonic,
) -> WorkspaceObservation:
    budget = _new_budget(limits, monotonic)
    resolved = _resolve(workspace=workspace, start=start, cwd=cwd, budget=budget)
    try:
        return _capture_workspace(resolved.profile, budget, clock=clock)
    except ReadLimitError as exc:
        if exc.code is ReadLimitCode.UNSAFE_FILE:
            raise _bridge_error(ErrorCode.RECORD_INVALID) from None
        raise _map_limit_error(exc) from None
    except PermissionError:
        raise _bridge_error(ErrorCode.HOST_READ_PERMISSION_REQUIRED) from None
    except OSError:
        raise _bridge_error(ErrorCode.RECORD_INVALID) from None
    except ValidationError:
        raise _bridge_error(ErrorCode.RECORD_INVALID) from None


def get_gate_definitions_observation(
    *,
    task_id: str,
    workspace: str | None,
    start: str | Path | None,
    cwd: Path,
    limits: ObservationLimits | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> GateDefinitionsObservation:
    try:
        validate_id(task_id, "task_id")
    except ValidationError:
        raise _bridge_error(ErrorCode.SCHEMA_VALIDATION_FAILED) from None
    budget = _new_budget(limits, monotonic)
    resolved = _resolve(workspace=workspace, start=start, cwd=cwd, budget=budget)
    try:
        _, line_ids, failures, _ = _read_lines(resolved.profile.config, budget)
        if failures:
            raise _bridge_error(ErrorCode.OBSERVATION_PARTIAL)
        task = load_task_bounded(
            resolved.profile.config,
            task_id,
            budget,
            known_line_ids=line_ids,
        )
        if len(task.gates) > 64:
            raise ReadLimitError(
                ReadLimitCode.RECORD_LIMIT_EXCEEDED,
                "Task gate definition limit exceeded",
            )
        gates: list[GateDefinition] = []
        for gate in task.gates:
            validate_id(gate.name, "gate name")
            gates.append(GateDefinition(gate.name))
        result = GateDefinitionsObservation(task.id, tuple(gates))
        budget.check_deadline()
        return result
    except ReadLimitError as exc:
        if exc.code is ReadLimitCode.UNSAFE_FILE:
            raise _bridge_error(ErrorCode.RECORD_INVALID) from None
        raise _map_limit_error(exc) from None
    except BridgeObservationError:
        raise
    except (DyroError, OSError, UnicodeError):
        raise _bridge_error(ErrorCode.RECORD_INVALID) from None

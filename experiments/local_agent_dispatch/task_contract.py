"""Five-part TaskContract validation (ADR-0002)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .errors import DispatchValidationError


TASK_FIELDS = (
    "briefing",
    "locations",
    "objective",
    "constraints",
    "output_contract",
)


@dataclass(frozen=True)
class TaskBody:
    briefing: str
    locations: str
    objective: str
    constraints: str
    output_contract: str

    def to_mapping(self) -> dict[str, str]:
        return {
            "briefing": self.briefing,
            "locations": self.locations,
            "objective": self.objective,
            "constraints": self.constraints,
            "output_contract": self.output_contract,
        }


@dataclass(frozen=True)
class TaskContract:
    schema_version: int
    backend: str
    mode: str
    strict: bool
    files: tuple[str, ...]
    task: TaskBody

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "backend": self.backend,
            "mode": self.mode,
            "strict": self.strict,
            "files": list(self.files),
            "task": self.task.to_mapping(),
        }


def _require_nonempty_str(value: object, field: str) -> str:
    if type(value) is not str or not value.strip():
        raise DispatchValidationError(f"task field must be a non-empty string: {field}")
    if len(value) > 200_000:
        raise DispatchValidationError(f"task field too large: {field}")
    return value


def parse_task_contract(payload: Mapping[str, Any]) -> TaskContract:
    if payload.get("schema_version") != 1:
        raise DispatchValidationError("schema_version must be 1")

    backend = payload.get("backend", "auto")
    if type(backend) is not str or not backend.strip():
        raise DispatchValidationError("backend must be a non-empty string")

    mode = payload.get("mode", "read-only")
    if mode not in {"read-only", "edit"}:
        raise DispatchValidationError("mode must be read-only or edit")

    strict = payload.get("strict", False)
    if type(strict) is not bool:
        raise DispatchValidationError("strict must be a boolean")
    if strict and mode == "edit":
        raise DispatchValidationError(
            "strict shadow isolation is only valid with mode=read-only"
        )

    files = payload.get("files")
    if not isinstance(files, Sequence) or isinstance(files, (str, bytes)):
        raise DispatchValidationError("files must be a list of glob strings")
    if len(files) == 0:
        raise DispatchValidationError("files must be a non-empty minimal set")
    if any(type(item) is not str or not item.strip() for item in files):
        raise DispatchValidationError("files entries must be non-empty strings")
    if len(files) > 50:
        raise DispatchValidationError("files contains too many glob entries")
    if any(item.strip().lstrip("!") in {"*", "**", "**/*", "*/**", "**/**"} for item in files):
        raise DispatchValidationError(
            "files must not use an unrestricted glob; provide a minimal sufficient set"
        )

    raw_task = payload.get("task")
    if not isinstance(raw_task, Mapping):
        raise DispatchValidationError("task must be an object")
    body_fields = {
        name: _require_nonempty_str(raw_task.get(name), name) for name in TASK_FIELDS
    }
    task = TaskBody(**body_fields)

    return TaskContract(
        schema_version=1,
        backend=backend.strip(),
        mode=mode,
        strict=strict,
        files=tuple(str(item).strip() for item in files),
        task=task,
    )

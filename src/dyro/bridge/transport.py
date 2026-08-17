"""One-shot JSON transport. No CLI parser, no console entry point, no apply."""

from __future__ import annotations

import json
from pathlib import Path
import secrets
from typing import BinaryIO, Callable

from .. import __version__ as DYRO_VERSION
from ..continuation.resolution import (
    WorkspaceResolutionError,
    resolve_workspace_readonly,
)
from ..errors import ValidationError
from ..read_limits import ReadBudget, ReadLimitCode, ReadLimitError
from .catalog import build_default_catalog, compact_catalog
from .constants import PLANNER_REVISIONS
from .models import Availability
from .observations import (
    BridgeObservationError,
    default_read_budget,
    gate_definitions,
    list_lines_observation,
    list_objectives_observation,
    list_tasks_observation,
    list_workspaces_observation,
    objective_status_observation,
    observe_workspace,
    resolve_workspace_observation,
)
from .parse import MAX_REQUEST_BYTES, BoundedJSONError, load_bounded_json
from .plans import (
    objective_attention,
    objective_explain,
    objective_graph,
    objective_plan,
    objective_tick,
)
from .redaction import echo_request_id, presentation_message
from .schemas import operation_schema, validate_input

SERVER_MAJOR = 1
SERVER_MINOR = 0
BRIDGE_VERSION = "1.0"
MAX_RESPONSE_BYTES = 1024 * 1024
SAFE_INT_MAX = 9_007_199_254_740_991
ENVELOPE_FIELDS = frozenset({"protocol", "request_id", "client", "operation", "input"})
FORBIDDEN_FIELDS = frozenset(
    {"actor", "approval", "confirmation", "command", "argv", "shell", "apply", "dry_run"}
)
MESSAGES = {
    "INVALID_JSON": "The request is not one valid JSON object.",
    "REQUEST_TOO_LARGE": "The request exceeds the transport size limit.",
    "PROTOCOL_MAJOR_UNSUPPORTED": "The requested protocol major is unsupported.",
    "PROTOCOL_MINOR_UNSUPPORTED": "The requested protocol minor is newer than this server.",
    "SCHEMA_VALIDATION_FAILED": "The request envelope or operation input is invalid.",
    "OPERATION_UNKNOWN": "The requested operation is not in the catalog.",
    "OPERATION_UNAVAILABLE": "The requested operation is unavailable.",
    "LOCAL_PROFILE_INVALID": "The local Dyro Profile is invalid.",
    "REGISTRY_INVALID": "The global workspace registry cannot be trusted.",
    "WORKSPACE_NOT_REGISTERED": "The requested workspace alias is not registered.",
    "REGISTERED_ROOT_STALE": "The registered workspace root is no longer valid.",
    "HOST_READ_PERMISSION_REQUIRED": "The host cannot read the selected resource.",
    "AMBIGUOUS_WORKSPACE": "Multiple workspaces require an explicit selector.",
    "WORKSPACE_NOT_FOUND": "No usable workspace was found.",
    "RESOURCE_LIMIT_EXCEEDED": "A bounded workspace read budget was exhausted.",
    "OBSERVATION_DEADLINE_EXCEEDED": "The bounded observation deadline elapsed.",
    "OBJECTIVE_NOT_FOUND": "The requested Objective was not found.",
    "TASK_NOT_FOUND": "The requested Task was not found.",
    "INTERNAL_ERROR": "The request failed.",
}

ExitAndPayload = tuple[int, dict[str, object]]


class _TransportError(Exception):
    def __init__(
        self,
        code: str,
        exit_code: int,
        *,
        requested_protocol: dict[str, int] | None = None,
        operation: str | None = None,
        schema_version: int | None = None,
        planner_revision: str | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.exit_code = exit_code
        self.requested_protocol = requested_protocol
        self.operation = operation
        self.schema_version = schema_version
        self.planner_revision = planner_revision
        self.request_id = request_id


def handle_request(
    raw: bytes,
    *,
    cwd: Path,
    exposure: str = "public",
    clock: Callable | None = None,
    platform: str | None = None,
) -> ExitAndPayload:
    catalog = build_default_catalog(platform=platform)
    try:
        payload = _dispatch(raw, cwd=cwd, exposure=exposure, clock=clock, catalog=catalog)
        return 0, payload
    except _TransportError as exc:
        return exc.exit_code, _error_payload(exc, catalog)


def serve_once(
    stdin: BinaryIO,
    stdout: BinaryIO,
    *,
    cwd: Path,
    exposure: str = "public",
    platform: str | None = None,
) -> int:
    raw = stdin.read(MAX_REQUEST_BYTES + 1)
    try:
        stdin.close()
    except OSError:
        pass
    exit_code, payload = handle_request(
        raw, cwd=cwd, exposure=exposure, platform=platform
    )
    encoded = _encode_response(payload)
    try:
        stdout.write(encoded)
        stdout.flush()
    except BrokenPipeError:
        return 5
    except OSError as exc:
        if getattr(exc, "errno", None) == 32:
            return 5
        raise
    return exit_code


def _dispatch(
    raw: bytes,
    *,
    cwd: Path,
    exposure: str,
    clock: Callable | None,
    catalog,
) -> dict[str, object]:
    try:
        parsed = load_bounded_json(raw)
    except BoundedJSONError as exc:
        raise _TransportError(
            "REQUEST_TOO_LARGE" if exc.code == "REQUEST_TOO_LARGE" else "INVALID_JSON",
            2,
        ) from exc
    envelope = _envelope(parsed)
    record = catalog.record(envelope["operation"])
    if record is None:
        raise _TransportError(
            "OPERATION_UNKNOWN",
            2,
            requested_protocol=envelope["requested_protocol"],
            operation=envelope["operation"],
            request_id=envelope["request_id"],
        )
    if not _callable(record.availability, exposure):
        raise _TransportError(
            "OPERATION_UNAVAILABLE",
            4,
            requested_protocol=envelope["requested_protocol"],
            operation=envelope["operation"],
            schema_version=record.schema_version,
            planner_revision=PLANNER_REVISIONS.get(record.id),
            request_id=envelope["request_id"],
        )
    schema = operation_schema(record.id, catalog=catalog)
    try:
        validate_input(schema["input"], envelope["input"])
    except ValidationError as exc:
        raise _TransportError(
            "SCHEMA_VALIDATION_FAILED",
            2,
            requested_protocol=envelope["requested_protocol"],
            operation=envelope["operation"],
            schema_version=record.schema_version,
            planner_revision=PLANNER_REVISIONS.get(record.id),
            request_id=envelope["request_id"],
        ) from exc
    data, warnings, partial = _call(
        record.id, envelope["input"], cwd=cwd, clock=clock, catalog=catalog
    )
    if envelope["redacted"]:
        warnings = [*warnings, {"code": "REQUEST_ID_REDACTED"}]
    return {
        "ok": True,
        "meta": _meta(
            catalog,
            requested_protocol=envelope["requested_protocol"],
            operation=record.id,
            schema_version=record.schema_version,
            planner_revision=PLANNER_REVISIONS.get(record.id),
            request_id=envelope["request_id"],
            partial=partial,
        ),
        "data": data,
        "warnings": warnings,
    }


def _envelope(parsed: object) -> dict[str, object]:
    if not isinstance(parsed, dict):
        raise _TransportError("INVALID_JSON", 2)
    if FORBIDDEN_FIELDS & set(parsed):
        raise _TransportError("SCHEMA_VALIDATION_FAILED", 2)
    unknown = set(parsed) - ENVELOPE_FIELDS
    if unknown:
        raise _TransportError("SCHEMA_VALIDATION_FAILED", 2)
    missing = {"protocol", "client", "operation", "input"} - set(parsed)
    if missing:
        raise _TransportError("SCHEMA_VALIDATION_FAILED", 2)
    request_id, redacted = _optional_request_id(parsed.get("request_id"))
    requested = _protocol(parsed.get("protocol"))
    client = parsed.get("client")
    if not isinstance(client, dict) or set(client) - {"name", "version"}:
        raise _TransportError(
            "SCHEMA_VALIDATION_FAILED", 2, requested_protocol=requested, request_id=request_id
        )
    if not isinstance(client.get("name"), str) or not isinstance(client.get("version"), str):
        raise _TransportError(
            "SCHEMA_VALIDATION_FAILED", 2, requested_protocol=requested, request_id=request_id
        )
    operation = parsed.get("operation")
    if not isinstance(operation, str) or not operation:
        raise _TransportError(
            "SCHEMA_VALIDATION_FAILED",
            2,
            requested_protocol=requested,
            request_id=request_id,
        )
    payload = parsed.get("input")
    if not isinstance(payload, dict) or FORBIDDEN_FIELDS & set(payload):
        raise _TransportError(
            "SCHEMA_VALIDATION_FAILED",
            2,
            requested_protocol=requested,
            operation=operation,
            request_id=request_id,
        )
    start = payload.get("start")
    if isinstance(start, str) and start.startswith("~"):
        raise _TransportError(
            "SCHEMA_VALIDATION_FAILED",
            2,
            requested_protocol=requested,
            operation=operation,
            request_id=request_id,
        )
    return {
        "requested_protocol": requested,
        "operation": operation,
        "input": payload,
        "request_id": request_id,
        "redacted": redacted,
    }


def _optional_request_id(value: object) -> tuple[str | None, bool]:
    if value is None:
        return None, False
    echoed, redacted = echo_request_id(value)
    return echoed, redacted


def _protocol(value: object) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) - {"major", "minor"}:
        raise _TransportError("SCHEMA_VALIDATION_FAILED", 2)
    major = value.get("major")
    minor = value.get("minor")
    if not _safe_int(major) or not _safe_int(minor):
        raise _TransportError("SCHEMA_VALIDATION_FAILED", 2)
    if major != SERVER_MAJOR:
        raise _TransportError(
            "PROTOCOL_MAJOR_UNSUPPORTED",
            2,
            requested_protocol={"major": major, "minor": minor},
        )
    if minor > SERVER_MINOR:
        raise _TransportError(
            "PROTOCOL_MINOR_UNSUPPORTED",
            2,
            requested_protocol={"major": major, "minor": minor},
        )
    return {"major": major, "minor": minor}


def _safe_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= SAFE_INT_MAX


def _callable(availability: Availability, exposure: str) -> bool:
    if exposure == "public":
        return availability is Availability.PUBLIC_AVAILABLE
    if exposure == "testable":
        return availability in {
            Availability.PUBLIC_AVAILABLE,
            Availability.IMPLEMENTED_TESTABLE,
        }
    raise _TransportError("INTERNAL_ERROR", 2)


def _call(
    operation: str,
    payload: dict[str, object],
    *,
    cwd: Path,
    clock: Callable | None,
    catalog,
) -> tuple[object, list[dict[str, str]], bool]:
    budget = default_read_budget()
    try:
        data = _invoke(
            operation, payload, cwd=cwd, clock=clock, budget=budget, catalog=catalog
        )
    except BridgeObservationError as exc:
        exit_code = 4 if exc.code == "OPERATION_UNAVAILABLE" else 3
        raise _TransportError(exc.code if exc.code in MESSAGES else "INTERNAL_ERROR", exit_code) from exc
    except WorkspaceResolutionError as exc:
        raise _TransportError(exc.code.value, 3) from exc
    except ReadLimitError as exc:
        code = (
            "OBSERVATION_DEADLINE_EXCEEDED"
            if exc.code is ReadLimitCode.DEADLINE_EXCEEDED
            else "RESOURCE_LIMIT_EXCEEDED"
        )
        raise _TransportError(code, 3) from exc
    except ValidationError as exc:
        raise _TransportError("SCHEMA_VALIDATION_FAILED", 2) from exc
    partial = bool(isinstance(data, dict) and data.get("partial"))
    return data, [], partial


def _invoke(
    operation: str,
    payload: dict[str, object],
    *,
    cwd: Path,
    clock: Callable | None,
    budget: ReadBudget,
    catalog,
) -> object:
    if operation == "bridge.hello":
        return {
            "protocol": {"major": SERVER_MAJOR, "minor": SERVER_MINOR},
            "dyro_version": DYRO_VERSION,
            "bridge_version": BRIDGE_VERSION,
        }
    if operation == "bridge.capabilities.compact":
        return compact_catalog(catalog)
    if operation == "bridge.operation.schema":
        return operation_schema(str(payload["operation"]), catalog=catalog)
    if operation == "workspace.list":
        return list_workspaces_observation(budget=budget)
    if operation == "workspace.resolve":
        return resolve_workspace_observation(
            start=payload.get("start"),
            workspace=_alias(payload.get("workspace")),
            cwd=cwd,
            budget=budget,
        )
    if operation == "workspace.observe":
        return observe_workspace(
            start=payload.get("start"),
            workspace=_alias(payload.get("workspace")),
            cwd=cwd,
            budget=budget,
            clock=clock,
        )
    resolved = resolve_workspace_readonly(
        start=payload.get("start"),
        workspace=_alias(payload.get("workspace")),
        cwd=cwd,
        budget=budget,
    )
    config = resolved.profile.config
    if operation == "line.list":
        return list_lines_observation(config)
    if operation == "task.list":
        return list_tasks_observation(config)
    if operation == "task.gate_definitions.get":
        return gate_definitions(config, str(payload["task_id"]))
    if operation == "objective.list":
        return list_objectives_observation(config)
    if operation == "objective.status":
        return objective_status_observation(config, str(payload["objective_id"]))
    plan_builders = {
        "objective.plan": objective_plan,
        "objective.explain": objective_explain,
        "objective.graph": objective_graph,
        "objective.tick": objective_tick,
        "objective.attention": objective_attention,
    }
    builder = plan_builders.get(operation)
    if builder is None:
        raise _TransportError("OPERATION_UNAVAILABLE", 4)
    return builder(
        config,
        str(payload["objective_id"]),
        profile_bytes=resolved.profile.profile_bytes,
        clock=clock,
    )


def _alias(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _meta(
    catalog,
    *,
    requested_protocol: dict[str, int] | None,
    operation: str | None,
    schema_version: int | None,
    planner_revision: str | None,
    request_id: str | None,
    partial: bool = False,
    truncated: bool = False,
) -> dict[str, object]:
    return {
        "server_protocol": {"major": SERVER_MAJOR, "minor": SERVER_MINOR},
        "requested_protocol": requested_protocol,
        "dyro_version": DYRO_VERSION,
        "bridge_version": BRIDGE_VERSION,
        "operation": operation,
        "operation_schema_version": schema_version,
        "planner_revision": planner_revision,
        "request_id": request_id,
        "event_id": f"evt_{secrets.token_hex(8)}",
        "capabilities_digest": catalog.digest,
        "partial": partial,
        "truncated": truncated,
    }


def _error_payload(exc: _TransportError, catalog) -> dict[str, object]:
    return {
        "ok": False,
        "meta": _meta(
            catalog,
            requested_protocol=exc.requested_protocol,
            operation=exc.operation,
            schema_version=exc.schema_version,
            planner_revision=exc.planner_revision,
            request_id=exc.request_id,
        ),
        "error": {
            "code": exc.code,
            "message": presentation_message(MESSAGES.get(exc.code, MESSAGES["INTERNAL_ERROR"])),
            "retryable": False,
            "details": {},
            "next_actions": [],
        },
    }


def _encode_response(payload: dict[str, object]) -> bytes:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_RESPONSE_BYTES:
        catalog = build_default_catalog()
        encoded = json.dumps(
            {
                "ok": False,
                "meta": _meta(
                    catalog,
                    requested_protocol=None,
                    operation=None,
                    schema_version=None,
                    planner_revision=None,
                    request_id=None,
                    truncated=True,
                ),
                "error": {
                    "code": "RESOURCE_LIMIT_EXCEEDED",
                    "message": MESSAGES["RESOURCE_LIMIT_EXCEEDED"],
                    "retryable": False,
                    "details": {},
                    "next_actions": [],
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    return encoded + b"\n"

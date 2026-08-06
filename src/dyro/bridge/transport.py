"""Strict one-request/one-response stdio transport for Agent Bridge Phase 0."""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import json
import os
from pathlib import Path
import platform as runtime_platform
import re
import secrets
import sys
from types import MappingProxyType
from typing import BinaryIO, Callable, Mapping

from jsonschema import Draft202012Validator

from .. import __version__ as DYRO_VERSION
from ..canonical import canonical_json_bytes
from ..errors import ValidationError
from .catalog import CATALOG, ExposureCatalog
from .constants import (
    BRIDGE_VERSION,
    MAX_PROTOCOL_COMPONENT,
    PROTOCOL_MAJOR,
    PROTOCOL_MINOR,
)
from .models import (
    AvailabilityState,
    ErrorCode,
    OperationKind,
    OperationSpec,
    PlatformState,
    ProtocolVersion,
    ResponseMetadata,
)
from .observations import (
    BridgeObservationError,
    get_gate_definitions_observation,
    list_workspace_observations,
    observe_workspace,
    resolve_workspace_observation,
)
from .plans import (
    attention_objective,
    compute_plan_sha256,
    explain_objective,
    graph_objective,
    plan_objective,
    tick_objective,
)
from .redaction import normalize_error, redact_operation_data, safe_request_id
from .schemas import get_operation_schema, get_request_envelope_schema


MAX_REQUEST_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_JSON_DEPTH = 64
MAX_JSON_NODES = 10_000
MAX_NUMBER_BYTES = 128

_OPERATION_ID = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")


class _DuplicateKey(ValueError):
    pass


class _RequestRejected(Exception):
    def __init__(self, code: ErrorCode, exit_code: int) -> None:
        super().__init__(code.value)
        self.code = code
        self.exit_code = exit_code


Handler = Callable[[dict[str, object], "TransportContext"], object]


def _platform_name() -> str:
    if sys.platform == "darwin":
        try:
            major = int(runtime_platform.mac_ver()[0].split(".", 1)[0])
        except Exception:
            return "unsupported"
        return "macos-15" if major == 15 else "unsupported"
    if sys.platform.startswith("linux"):
        try:
            release = runtime_platform.freedesktop_os_release()
        except Exception:
            return "unsupported"
        if release.get("ID") == "ubuntu" and release.get("VERSION_ID") == "24.04":
            return "linux-ubuntu-24.04"
        return "unsupported"
    return "windows" if sys.platform == "win32" else "unsupported"


@dataclass(frozen=True)
class TransportContext:
    """Explicit process context; test-only availability cannot come from env/argv."""

    catalog: ExposureCatalog = CATALOG
    platform: str = ""
    cwd: Path = Path(".")
    allow_test_services: bool = False
    handlers: Mapping[str, Handler] | None = None
    event_id_factory: Callable[[], str] = lambda: f"evt_{secrets.token_hex(12)}"

    def __post_init__(self) -> None:
        if not isinstance(self.catalog, ExposureCatalog):
            raise ValidationError("transport catalog is invalid")
        if self.platform and not isinstance(self.platform, str):
            raise ValidationError("transport platform is invalid")
        if not isinstance(self.cwd, Path):
            raise ValidationError("transport cwd is invalid")
        if not isinstance(self.allow_test_services, bool):
            raise ValidationError("test availability marker is invalid")

    @property
    def effective_platform(self) -> str:
        return self.platform or _platform_name()


def bridge_hello(_: dict[str, object], __: TransportContext) -> dict[str, object]:
    return {
        "dyro_version": DYRO_VERSION,
        "bridge_version": BRIDGE_VERSION,
        "server_protocol": {"major": PROTOCOL_MAJOR, "minor": PROTOCOL_MINOR},
    }


def bridge_capabilities(
    _: dict[str, object], context: TransportContext
) -> dict[str, object]:
    platform = context.effective_platform
    return {
        "operations": list(context.catalog.compact_capabilities(platform)),
        "capabilities_digest": context.catalog.capabilities_digest(platform),
    }


def _is_available(operation: OperationSpec, context: TransportContext) -> bool:
    if operation.available_on(context.effective_platform):
        return True
    if not context.allow_test_services:
        return False
    if operation.availability_state is not AvailabilityState.IMPLEMENTED_TESTABLE:
        return False
    return any(
        item.platform == context.effective_platform
        and item.state is not PlatformState.UNAVAILABLE
        for item in operation.platforms
    )


def bridge_operation_schema(
    request: dict[str, object], context: TransportContext
) -> dict[str, object]:
    operation_id = request["operation"]
    if not isinstance(operation_id, str):
        raise _RequestRejected(ErrorCode.SCHEMA_VALIDATION_FAILED, 2)
    try:
        operation = context.catalog.get(operation_id)
    except ValidationError:
        raise _RequestRejected(ErrorCode.OPERATION_UNKNOWN, 2) from None
    if not _is_available(operation, context):
        raise _RequestRejected(ErrorCode.OPERATION_UNAVAILABLE, 4)
    return get_operation_schema(operation_id).public_dict()


def _workspace_selector(request: dict[str, object]) -> dict[str, object]:
    return {"workspace": request.get("workspace"), "start": request.get("start")}


def _resolve_workspace(request: dict[str, object], context: TransportContext) -> object:
    return resolve_workspace_observation(
        cwd=context.cwd, **_workspace_selector(request)
    )


def _list_workspaces(_: dict[str, object], __: TransportContext) -> object:
    return list_workspace_observations()


def _observe_workspace(request: dict[str, object], context: TransportContext) -> object:
    return observe_workspace(cwd=context.cwd, **_workspace_selector(request))


def _gate_definitions(request: dict[str, object], context: TransportContext) -> object:
    return get_gate_definitions_observation(
        task_id=request["task_id"],
        cwd=context.cwd,
        **_workspace_selector(request),
    )


def _objective_plan(
    builder: Callable[..., object],
    request: dict[str, object],
    context: TransportContext,
) -> object:
    return builder(
        objective_id=request["objective_id"],
        cwd=context.cwd,
        **_workspace_selector(request),
    )


_STATIC_HANDLERS: Mapping[str, Handler] = MappingProxyType(
    {
        "dyro.bridge.transport.bridge_capabilities": bridge_capabilities,
        "dyro.bridge.transport.bridge_hello": bridge_hello,
        "dyro.bridge.transport.bridge_operation_schema": bridge_operation_schema,
        "dyro.bridge.observations.get_gate_definitions_observation": _gate_definitions,
        "dyro.bridge.observations.list_workspace_observations": _list_workspaces,
        "dyro.bridge.observations.observe_workspace": _observe_workspace,
        "dyro.bridge.observations.resolve_workspace_observation": _resolve_workspace,
        "dyro.bridge.plans.attention_objective": lambda request, context: (
            _objective_plan(attention_objective, request, context)
        ),
        "dyro.bridge.plans.explain_objective": lambda request, context: _objective_plan(
            explain_objective, request, context
        ),
        "dyro.bridge.plans.graph_objective": lambda request, context: _objective_plan(
            graph_objective, request, context
        ),
        "dyro.bridge.plans.plan_objective": lambda request, context: _objective_plan(
            plan_objective, request, context
        ),
        "dyro.bridge.plans.tick_objective": lambda request, context: _objective_plan(
            tick_objective, request, context
        ),
    }
)


def _duplicate_checked_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey
        result[key] = value
    return result


def _bounded_number(value: str) -> int | float:
    if len(value.encode("ascii")) > MAX_NUMBER_BYTES:
        raise ValueError("number limit")
    return float(value) if any(marker in value for marker in ".eE") else int(value)


def _scan_shape(text: str) -> None:
    depth = 0
    nodes = 0
    in_string = False
    escaped = False
    index = 0
    while index < len(text):
        character = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            index += 1
            continue
        if character == '"':
            in_string = True
            nodes += 1
        elif character in "[{":
            depth += 1
            nodes += 1
            if depth > MAX_JSON_DEPTH:
                raise _RequestRejected(ErrorCode.INVALID_JSON, 2)
        elif character in "]}":
            depth -= 1
            if depth < 0:
                raise _RequestRejected(ErrorCode.INVALID_JSON, 2)
        elif character == "-" or character.isdigit():
            nodes += 1
            end = index + 1
            while end < len(text) and text[end] in "+-.0123456789Ee":
                end += 1
            if len(text[index:end].encode("utf-8")) > MAX_NUMBER_BYTES:
                raise _RequestRejected(ErrorCode.INVALID_JSON, 2)
            if nodes > MAX_JSON_NODES:
                raise _RequestRejected(ErrorCode.INVALID_JSON, 2)
            index = end
            continue
        elif character.isalpha():
            nodes += 1
            end = index + 1
            while end < len(text) and text[end].isalpha():
                end += 1
            index = end
            if nodes > MAX_JSON_NODES:
                raise _RequestRejected(ErrorCode.INVALID_JSON, 2)
            continue
        if nodes > MAX_JSON_NODES:
            raise _RequestRejected(ErrorCode.INVALID_JSON, 2)
        index += 1
    if in_string or depth != 0:
        raise _RequestRejected(ErrorCode.INVALID_JSON, 2)


def _count_nodes(document: object) -> None:
    count = 0
    pending = [document]
    while pending:
        value = pending.pop()
        count += 1
        if count > MAX_JSON_NODES:
            raise _RequestRejected(ErrorCode.INVALID_JSON, 2)
        if isinstance(value, dict):
            if any(
                any(0xD800 <= ord(character) <= 0xDFFF for character in key)
                for key in value
            ):
                raise _RequestRejected(ErrorCode.INVALID_JSON, 2)
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
        elif isinstance(value, str) and any(
            0xD800 <= ord(character) <= 0xDFFF for character in value
        ):
            raise _RequestRejected(ErrorCode.INVALID_JSON, 2)


def _parse_request(raw: bytes) -> dict[str, object]:
    if len(raw) > MAX_REQUEST_BYTES:
        raise _RequestRejected(ErrorCode.REQUEST_TOO_LARGE, 2)
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise _RequestRejected(ErrorCode.INVALID_JSON, 2) from None
    _scan_shape(text)
    decoder = json.JSONDecoder(
        object_pairs_hook=_duplicate_checked_object,
        parse_int=_bounded_number,
        parse_float=_bounded_number,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("constant")),
    )
    try:
        stripped = text.lstrip(" \t\r\n")
        document, offset = decoder.raw_decode(stripped)
        if stripped[offset:].strip(" \t\r\n"):
            raise ValueError("trailing data")
    except (ValueError, RecursionError, _DuplicateKey):
        raise _RequestRejected(ErrorCode.INVALID_JSON, 2) from None
    if not isinstance(document, dict):
        raise _RequestRejected(ErrorCode.INVALID_JSON, 2)
    _count_nodes(document)
    return document


def _valid_protocol(value: object) -> ProtocolVersion | None:
    if not isinstance(value, dict) or set(value) != {"major", "minor"}:
        return None
    major = value.get("major")
    minor = value.get("minor")
    if (
        not isinstance(major, int)
        or isinstance(major, bool)
        or not 0 <= major <= MAX_PROTOCOL_COMPONENT
        or not isinstance(minor, int)
        or isinstance(minor, bool)
        or not 0 <= minor <= MAX_PROTOCOL_COMPONENT
    ):
        return None
    return ProtocolVersion(major, minor)


def _validate(schema: dict[str, object], instance: object) -> bool:
    return next(Draft202012Validator(schema).iter_errors(instance), None) is None


def _response_meta(
    context: TransportContext,
    *,
    requested_protocol: ProtocolVersion | None,
    operation: OperationSpec | None,
    request_id: str | None,
    partial: bool = False,
    truncated: bool = False,
) -> ResponseMetadata:
    return ResponseMetadata(
        server_protocol=ProtocolVersion(PROTOCOL_MAJOR, PROTOCOL_MINOR),
        requested_protocol=requested_protocol,
        dyro_version=DYRO_VERSION,
        bridge_version=BRIDGE_VERSION,
        operation=operation.operation_id if operation is not None else None,
        operation_schema_version=(
            operation.schema_version if operation is not None else None
        ),
        planner_revision=(
            operation.planner_revision if operation is not None else None
        ),
        request_id=request_id,
        event_id=context.event_id_factory(),
        capabilities_digest=context.catalog.capabilities_digest(
            context.effective_platform
        ),
        partial=partial,
        truncated=truncated,
    )


def _error_response(
    context: TransportContext,
    code: ErrorCode,
    *,
    requested_protocol: ProtocolVersion | None,
    operation: OperationSpec | None,
    request_id: str | None,
    truncated: bool = False,
) -> dict[str, object]:
    return {
        "ok": False,
        "meta": _response_meta(
            context,
            requested_protocol=requested_protocol,
            operation=operation,
            request_id=request_id,
            truncated=truncated,
        ).as_dict(),
        "error": normalize_error(code).as_dict(),
    }


def _error_exit(code: ErrorCode) -> int:
    if code is ErrorCode.OPERATION_UNAVAILABLE:
        return 4
    if code in {
        ErrorCode.INVALID_JSON,
        ErrorCode.REQUEST_TOO_LARGE,
        ErrorCode.PROTOCOL_MAJOR_UNSUPPORTED,
        ErrorCode.PROTOCOL_MINOR_UNSUPPORTED,
        ErrorCode.SCHEMA_VALIDATION_FAILED,
        ErrorCode.OPERATION_UNKNOWN,
    }:
        return 2
    return 3


def _as_data(result: object) -> dict[str, object]:
    if isinstance(result, dict):
        return dict(result)
    converter = getattr(result, "as_dict", None)
    if not callable(converter):
        raise ValueError("Core result has no bounded representation")
    data = converter()
    if not isinstance(data, dict):
        raise ValueError("Core result is not an object")
    return data


def _active_context(
    context: TransportContext | None,
) -> tuple[TransportContext, bool]:
    if context is not None:
        return context, False
    try:
        cwd = Path.cwd()
    except (OSError, RuntimeError):
        return TransportContext(cwd=Path("/")), True
    return TransportContext(cwd=cwd), False


@contextmanager
def _isolated_handler_output():
    """Keep Python and descriptor-level Core output away from the protocol."""
    saved_stdout = os.dup(1)
    saved_stderr = os.dup(2)
    try:
        with open(os.devnull, "w", encoding="utf-8") as sink:
            os.dup2(sink.fileno(), 1)
            os.dup2(sink.fileno(), 2)
            with redirect_stdout(sink), redirect_stderr(sink):
                yield
    finally:
        try:
            os.dup2(saved_stdout, 1)
            os.dup2(saved_stderr, 2)
        finally:
            os.close(saved_stdout)
            os.close(saved_stderr)


def handle_request_bytes(
    raw: bytes, context: TransportContext | None = None
) -> tuple[dict[str, object], int]:
    """Validate and execute one request without writing to process streams."""
    active, context_failed = _active_context(context)
    requested_protocol: ProtocolVersion | None = None
    operation: OperationSpec | None = None
    request_id: str | None = None
    request_id_redacted = False
    if context_failed:
        return (
            _error_response(
                active,
                ErrorCode.INTERNAL_ERROR,
                requested_protocol=None,
                operation=None,
                request_id=None,
            ),
            3,
        )
    try:
        request = _parse_request(raw)
        requested_protocol = _valid_protocol(request.get("protocol"))
        request_id, request_id_redacted = safe_request_id(request.get("request_id"))
        operation_id = request.get("operation")
        if isinstance(operation_id, str) and _OPERATION_ID.fullmatch(operation_id):
            try:
                operation = active.catalog.get(operation_id)
            except ValidationError:
                operation = None

        if not _validate(get_request_envelope_schema(), request):
            raise _RequestRejected(ErrorCode.SCHEMA_VALIDATION_FAILED, 2)
        if requested_protocol is None:
            raise _RequestRejected(ErrorCode.SCHEMA_VALIDATION_FAILED, 2)
        if requested_protocol.major != PROTOCOL_MAJOR:
            raise _RequestRejected(ErrorCode.PROTOCOL_MAJOR_UNSUPPORTED, 2)
        if requested_protocol.minor > PROTOCOL_MINOR:
            raise _RequestRejected(ErrorCode.PROTOCOL_MINOR_UNSUPPORTED, 2)
        if operation is None:
            raise _RequestRejected(ErrorCode.OPERATION_UNKNOWN, 2)

        operation_schema = get_operation_schema(operation.operation_id)
        operation_input = request["input"]
        if not _validate(operation_schema.input_schema(), operation_input):
            raise _RequestRejected(ErrorCode.SCHEMA_VALIDATION_FAILED, 2)
        if not _is_available(operation, active):
            raise _RequestRejected(ErrorCode.OPERATION_UNAVAILABLE, 4)
        handlers = active.handlers if active.handlers is not None else _STATIC_HANDLERS
        handler = handlers.get(operation.service_id or "")
        if handler is None:
            raise _RequestRejected(ErrorCode.OPERATION_UNAVAILABLE, 4)

        with _isolated_handler_output():
            result = handler(operation_input, active)
            data = _as_data(result)
            partial = bool(getattr(result, "partial", False))
            truncated = bool(getattr(result, "truncated", False))
        if not _validate(operation_schema.output_schema(), data):
            raise _RequestRejected(ErrorCode.INTERNAL_ERROR, 3)
        if operation.kind is OperationKind.PLAN and data.get(
            "plan_sha256"
        ) != compute_plan_sha256(data):
            raise _RequestRejected(ErrorCode.INTERNAL_ERROR, 3)
        try:
            data, data_redacted = redact_operation_data(
                operation.operation_id, operation.kind, data
            )
        except ValueError:
            raise _RequestRejected(ErrorCode.INTERNAL_ERROR, 3) from None
        if not _validate(operation_schema.output_schema(), data):
            raise _RequestRejected(ErrorCode.INTERNAL_ERROR, 3)

        warnings: list[dict[str, str]] = []
        if request_id_redacted:
            warnings.append(
                {
                    "code": "REQUEST_ID_REDACTED",
                    "message": "The request ID was not echoed.",
                }
            )
        if data_redacted:
            warnings.append(
                {
                    "code": "RESULT_REDACTED",
                    "message": "Sensitive result text was redacted.",
                }
            )
        response: dict[str, object] = {
            "ok": True,
            "meta": _response_meta(
                active,
                requested_protocol=requested_protocol,
                operation=operation,
                request_id=request_id,
                partial=partial,
                truncated=truncated,
            ).as_dict(),
            "data": data,
            "warnings": warnings,
        }
        if len(canonical_json_bytes(response)) + 1 > MAX_RESPONSE_BYTES:
            return (
                _error_response(
                    active,
                    ErrorCode.RESOURCE_LIMIT_EXCEEDED,
                    requested_protocol=requested_protocol,
                    operation=operation,
                    request_id=request_id,
                    truncated=True,
                ),
                3,
            )
        return response, 0
    except BridgeObservationError as exc:
        code = exc.error.code
        return (
            _error_response(
                active,
                code,
                requested_protocol=requested_protocol,
                operation=operation,
                request_id=request_id,
            ),
            _error_exit(code),
        )
    except _RequestRejected as exc:
        return (
            _error_response(
                active,
                exc.code,
                requested_protocol=requested_protocol,
                operation=operation,
                request_id=request_id,
            ),
            exc.exit_code,
        )
    except (Exception, KeyboardInterrupt):
        return (
            _error_response(
                active,
                ErrorCode.INTERNAL_ERROR,
                requested_protocol=requested_protocol,
                operation=operation,
                request_id=request_id,
            ),
            3,
        )


def _write_all(stream: BinaryIO, payload: bytes) -> bool:
    view = memoryview(payload)
    try:
        while view:
            written = stream.write(view)
            if written is None or written <= 0:
                return False
            view = view[written:]
        stream.flush()
    except (BrokenPipeError, OSError, ValueError):
        return False
    return True


def run(
    stdin_binary: BinaryIO,
    stdout_binary: BinaryIO,
    context: TransportContext | None = None,
) -> int:
    """Run the bounded stdio contract once."""
    active, context_failed = _active_context(context)
    input_closed = False
    input_close_failed = False
    try:
        raw = stdin_binary.read(MAX_REQUEST_BYTES + 1)
    except (Exception, KeyboardInterrupt):
        raw = b""
        response = _error_response(
            active,
            ErrorCode.INTERNAL_ERROR,
            requested_protocol=None,
            operation=None,
            request_id=None,
        )
        exit_code = 3
    else:
        try:
            stdin_binary.close()
            input_closed = True
        except (Exception, KeyboardInterrupt):
            input_closed = True
            input_close_failed = True
        if context_failed or input_close_failed:
            response = _error_response(
                active,
                ErrorCode.INTERNAL_ERROR,
                requested_protocol=None,
                operation=None,
                request_id=None,
            )
            exit_code = 3
        else:
            response, exit_code = handle_request_bytes(raw, active)
    finally:
        if not input_closed:
            try:
                stdin_binary.close()
            except (Exception, KeyboardInterrupt):
                pass
    payload = canonical_json_bytes(response) + b"\n"
    if not _write_all(stdout_binary, payload):
        return 5
    return exit_code


def main() -> int:
    """Console-script entry point with no human CLI or environment override."""
    exit_code = run(sys.stdin.buffer, sys.stdout.buffer)
    if exit_code == 5:
        # Prevent CPython's final buffered flush from printing an ignored EPIPE.
        try:
            null_fd = os.open(os.devnull, os.O_WRONLY)
            try:
                os.dup2(null_fd, sys.stdout.fileno())
            finally:
                os.close(null_fd)
        except (OSError, ValueError):
            pass
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

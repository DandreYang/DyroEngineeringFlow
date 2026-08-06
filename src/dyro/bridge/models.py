"""Immutable Agent Bridge Phase 0 contract models."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import os
from pathlib import Path
import re

from ..canonical import canonical_json_bytes
from ..config import validate_id
from ..errors import ValidationError


MAX_PROFILE_BYTES = 1024 * 1024
WORKSPACE_IDENTITY_DOMAIN = b"dyro.workspace.identity/v1\0"
CONFIG_REVISION_DOMAIN = b"dyro.config.raw/v1\0"

_OPERATION_ID = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
_SCHEMA_ID = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_SERVICE_ID = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)+$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


class OperationKind(str, Enum):
    INSPECT = "inspect"
    PLAN = "plan"


class RiskClass(str, Enum):
    R0 = "R0"
    PLAN = "PLAN"
    R1 = "R1"
    R2 = "R2"
    R3 = "R3"


class AvailabilityState(str, Enum):
    DECLARED = "declared"
    IMPLEMENTED_TESTABLE = "implemented_testable"
    PUBLIC_AVAILABLE = "public_available"


class PlatformState(str, Enum):
    DECLARED = "declared"
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class ErrorCode(str, Enum):
    INVALID_JSON = "INVALID_JSON"
    REQUEST_TOO_LARGE = "REQUEST_TOO_LARGE"
    PROTOCOL_MAJOR_UNSUPPORTED = "PROTOCOL_MAJOR_UNSUPPORTED"
    SCHEMA_VALIDATION_FAILED = "SCHEMA_VALIDATION_FAILED"
    OPERATION_UNKNOWN = "OPERATION_UNKNOWN"
    OPERATION_UNAVAILABLE = "OPERATION_UNAVAILABLE"
    LOCAL_PROFILE_INVALID = "LOCAL_PROFILE_INVALID"
    REGISTRY_INVALID = "REGISTRY_INVALID"
    WORKSPACE_NOT_REGISTERED = "WORKSPACE_NOT_REGISTERED"
    REGISTERED_ROOT_STALE = "REGISTERED_ROOT_STALE"
    HOST_READ_PERMISSION_REQUIRED = "HOST_READ_PERMISSION_REQUIRED"
    AMBIGUOUS_WORKSPACE = "AMBIGUOUS_WORKSPACE"
    WORKSPACE_NOT_FOUND = "WORKSPACE_NOT_FOUND"
    OBSERVATION_PARTIAL = "OBSERVATION_PARTIAL"
    RESOURCE_LIMIT_EXCEEDED = "RESOURCE_LIMIT_EXCEEDED"
    OBSERVATION_DEADLINE_EXCEEDED = "OBSERVATION_DEADLINE_EXCEEDED"
    RECORD_INVALID = "RECORD_INVALID"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class NextActionKind(str, Enum):
    INSPECT_INPUT = "inspect_input"
    INSPECT_PROFILE = "inspect_profile"
    SELECT_WORKSPACE = "select_workspace"
    CHECK_REGISTRY = "check_registry"
    GRANT_HOST_READ = "grant_host_read"
    RETRY = "retry"
    UPGRADE_CLIENT = "upgrade_client"


def _bounded_text(value: str, label: str, *, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{label} 必须是非空字符串")
    if len(value.encode("utf-8")) > maximum:
        raise ValidationError(f"{label} 超过 {maximum} 字节上限")
    return value


def _operation_id(value: str) -> str:
    if not isinstance(value, str) or not _OPERATION_ID.fullmatch(value):
        raise ValidationError(f"Bridge operation ID 不合法：{value!r}")
    return value


@dataclass(frozen=True)
class ProtocolVersion:
    major: int
    minor: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.major, int)
            or isinstance(self.major, bool)
            or self.major < 0
        ):
            raise ValidationError("protocol major 必须是非负整数")
        if (
            not isinstance(self.minor, int)
            or isinstance(self.minor, bool)
            or self.minor < 0
        ):
            raise ValidationError("protocol minor 必须是非负整数")

    def as_dict(self) -> dict[str, int]:
        return {"major": self.major, "minor": self.minor}


@dataclass(frozen=True)
class PlatformAvailability:
    platform: str
    state: PlatformState

    def __post_init__(self) -> None:
        _bounded_text(self.platform, "platform", maximum=80)
        if not isinstance(self.state, PlatformState):
            raise ValidationError("platform state 必须是 PlatformState")


@dataclass(frozen=True)
class OperationSpec:
    operation_id: str
    kind: OperationKind
    maximum_risk: RiskClass
    schema_version: int
    planner_revision: str | None
    input_schema_id: str
    output_schema_id: str
    must_be_available: bool = False
    availability_state: AvailabilityState = AvailabilityState.DECLARED
    service_id: str | None = None
    platforms: tuple[PlatformAvailability, ...] = ()

    def __post_init__(self) -> None:
        _operation_id(self.operation_id)
        if not isinstance(self.kind, OperationKind):
            raise ValidationError("operation kind 必须是 OperationKind")
        if not isinstance(self.maximum_risk, RiskClass):
            raise ValidationError("maximum risk 必须是 RiskClass")
        if not isinstance(self.availability_state, AvailabilityState):
            raise ValidationError("availability state 必须是 AvailabilityState")
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version < 1
        ):
            raise ValidationError("operation schema version 必须是正整数")
        for label, value in (
            ("input schema ID", self.input_schema_id),
            ("output schema ID", self.output_schema_id),
        ):
            if not isinstance(value, str) or not _SCHEMA_ID.fullmatch(value):
                raise ValidationError(f"{label} 不合法：{value!r}")
        if self.kind is OperationKind.INSPECT:
            if self.maximum_risk is not RiskClass.R0:
                raise ValidationError("inspect operation 的 maximum risk 必须是 R0")
            if self.planner_revision is not None:
                raise ValidationError("inspect operation 不能声明 planner revision")
        elif self.kind is OperationKind.PLAN:
            if self.maximum_risk is not RiskClass.PLAN:
                raise ValidationError("plan operation 的 maximum risk 必须是 PLAN")
            _bounded_text(self.planner_revision or "", "planner revision", maximum=128)
        if not isinstance(self.must_be_available, bool):
            raise ValidationError("must_be_available 必须是布尔值")
        if self.service_id is not None and (
            not isinstance(self.service_id, str)
            or not _SERVICE_ID.fullmatch(self.service_id)
        ):
            raise ValidationError(f"Core service ID 不合法：{self.service_id!r}")
        if not isinstance(self.platforms, tuple):
            raise ValidationError("operation platforms 必须是不可变 tuple")
        if not all(isinstance(item, PlatformAvailability) for item in self.platforms):
            raise ValidationError("operation platforms 必须只包含 PlatformAvailability")
        platform_names = tuple(item.platform for item in self.platforms)
        if len(set(platform_names)) != len(platform_names):
            raise ValidationError(f"operation {self.operation_id} 包含重复 platform")
        if platform_names != tuple(sorted(platform_names)):
            raise ValidationError(
                f"operation {self.operation_id} 的 platform 必须稳定排序"
            )
        if (
            self.availability_state is AvailabilityState.DECLARED
            and self.service_id is not None
        ):
            raise ValidationError("declared operation 不能绑定可调用 Core service")
        if (
            self.availability_state
            in {
                AvailabilityState.IMPLEMENTED_TESTABLE,
                AvailabilityState.PUBLIC_AVAILABLE,
            }
            and self.service_id is None
        ):
            raise ValidationError("implemented/public operation 必须绑定 Core service")
        if self.availability_state is AvailabilityState.PUBLIC_AVAILABLE:
            if not any(
                item.state is PlatformState.AVAILABLE for item in self.platforms
            ):
                raise ValidationError(
                    "public operation 至少需要一个 available platform"
                )

    @property
    def public_available(self) -> bool:
        return self.availability_state is AvailabilityState.PUBLIC_AVAILABLE

    def available_on(self, platform: str) -> bool:
        _bounded_text(platform, "platform", maximum=80)
        return self.public_available and any(
            item.platform == platform and item.state is PlatformState.AVAILABLE
            for item in self.platforms
        )

    def capability(self, platform: str) -> dict[str, object]:
        return {
            "operation": self.operation_id,
            "kind": self.kind.value,
            "maximum_risk": self.maximum_risk.value,
            "available": self.available_on(platform),
            "operation_schema_version": self.schema_version,
            "planner_revision": self.planner_revision,
        }


@dataclass(frozen=True)
class RequestMetadata:
    requested_protocol: ProtocolVersion
    request_id: str | None
    client_name: str
    client_version: str
    operation: str

    def __post_init__(self) -> None:
        if not isinstance(self.requested_protocol, ProtocolVersion):
            raise ValidationError("requested protocol 必须是 ProtocolVersion")
        if self.request_id is not None:
            _bounded_text(self.request_id, "request ID", maximum=128)
        _bounded_text(self.client_name, "client name", maximum=128)
        _bounded_text(self.client_version, "client version", maximum=128)
        _operation_id(self.operation)


@dataclass(frozen=True)
class ResponseMetadata:
    server_protocol: ProtocolVersion
    requested_protocol: ProtocolVersion | None
    dyro_version: str
    bridge_version: str
    operation: str | None
    operation_schema_version: int | None
    planner_revision: str | None
    request_id: str | None
    event_id: str
    capabilities_digest: str
    partial: bool = False
    truncated: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.server_protocol, ProtocolVersion):
            raise ValidationError("server protocol 必须是 ProtocolVersion")
        if self.requested_protocol is not None and not isinstance(
            self.requested_protocol, ProtocolVersion
        ):
            raise ValidationError("requested protocol 必须是 ProtocolVersion 或 null")
        _bounded_text(self.dyro_version, "Dyro version", maximum=128)
        _bounded_text(self.bridge_version, "Bridge version", maximum=128)
        if self.operation is not None:
            _operation_id(self.operation)
        if self.operation_schema_version is not None and (
            not isinstance(self.operation_schema_version, int)
            or isinstance(self.operation_schema_version, bool)
            or self.operation_schema_version < 1
        ):
            raise ValidationError("operation schema version 必须是正整数或 null")
        if self.planner_revision is not None:
            _bounded_text(self.planner_revision, "planner revision", maximum=128)
        if self.request_id is not None:
            _bounded_text(self.request_id, "request ID", maximum=128)
        _bounded_text(self.event_id, "event ID", maximum=128)
        if not isinstance(self.capabilities_digest, str) or not _SHA256.fullmatch(
            self.capabilities_digest
        ):
            raise ValidationError("capabilities digest 必须是 sha256:<64 hex>")
        if not isinstance(self.partial, bool) or not isinstance(self.truncated, bool):
            raise ValidationError("partial/truncated 必须是布尔值")

    def as_dict(self) -> dict[str, object]:
        return {
            "server_protocol": self.server_protocol.as_dict(),
            "requested_protocol": (
                self.requested_protocol.as_dict()
                if self.requested_protocol is not None
                else None
            ),
            "dyro_version": self.dyro_version,
            "bridge_version": self.bridge_version,
            "operation": self.operation,
            "operation_schema_version": self.operation_schema_version,
            "planner_revision": self.planner_revision,
            "request_id": self.request_id,
            "event_id": self.event_id,
            "capabilities_digest": self.capabilities_digest,
            "partial": self.partial,
            "truncated": self.truncated,
        }


@dataclass(frozen=True)
class BridgeWarning:
    code: str
    message: str

    def __post_init__(self) -> None:
        _bounded_text(self.code, "warning code", maximum=128)
        _bounded_text(self.message, "warning message")

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class BridgeNextAction:
    kind: NextActionKind
    label: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, NextActionKind):
            raise ValidationError("next action kind 必须是 NextActionKind")
        _bounded_text(self.label, "next action label", maximum=256)

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind.value, "label": self.label}


@dataclass(frozen=True)
class BridgeError:
    code: ErrorCode
    message: str
    retryable: bool = False
    details: tuple[tuple[str, str | int | bool | None], ...] = ()
    next_actions: tuple[BridgeNextAction, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.code, ErrorCode):
            raise ValidationError("error code 必须是 ErrorCode")
        _bounded_text(self.message, "error message")
        if not isinstance(self.retryable, bool):
            raise ValidationError("retryable 必须是布尔值")
        if not isinstance(self.details, tuple):
            raise ValidationError("error details 必须是不可变 tuple")
        if not all(isinstance(item, tuple) and len(item) == 2 for item in self.details):
            raise ValidationError("error details item 必须是二元 tuple")
        for key, value in self.details:
            _bounded_text(key, "error detail key", maximum=128)
            if isinstance(value, str):
                _bounded_text(value, "error detail value")
            elif value is not None and not isinstance(value, (int, bool)):
                raise ValidationError("error detail value 必须是 JSON scalar")
        detail_keys = tuple(key for key, _ in self.details)
        if len(set(detail_keys)) != len(detail_keys):
            raise ValidationError("error details 不能包含重复字段")
        if not isinstance(self.next_actions, tuple):
            raise ValidationError("next actions 必须是不可变 tuple")
        if not all(
            isinstance(action, BridgeNextAction) for action in self.next_actions
        ):
            raise ValidationError("next actions 必须只包含 BridgeNextAction")

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code.value,
            "message": self.message,
            "retryable": self.retryable,
            "details": dict(self.details),
            "next_actions": [action.as_dict() for action in self.next_actions],
        }


def workspace_identity(canonical_root: Path, profile_name: str) -> str:
    """Return the ADR-0006 opaque identity for an already canonical Profile root."""

    if not isinstance(canonical_root, Path) or not canonical_root.is_absolute():
        raise ValidationError("workspace canonical root 必须是绝对路径")
    normalized_root = Path(os.path.normpath(os.fspath(canonical_root)))
    if normalized_root != canonical_root:
        raise ValidationError("workspace canonical root 必须已完成词法规范化")
    try:
        resolved_root = canonical_root.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValidationError("workspace canonical root 无法解析") from exc
    if resolved_root != canonical_root:
        raise ValidationError(
            "workspace canonical root 必须是已解析的真实 Profile root"
        )
    if not isinstance(profile_name, str):
        raise ValidationError("Profile name 必须是字符串")
    validated_name = validate_id(profile_name, "Profile name")
    payload = {
        "canonical_root": canonical_root.as_posix(),
        "profile_name": validated_name,
    }
    digest = hashlib.sha256(
        WORKSPACE_IDENTITY_DOMAIN + canonical_json_bytes(payload)
    ).hexdigest()
    return f"workspace:{digest}"


def config_revision(profile_bytes: bytes) -> str:
    """Return the ADR-0006 exact-byte Profile revision without parsing or writing."""

    if not isinstance(profile_bytes, bytes):
        raise ValidationError("Profile revision 输入必须是 bytes")
    if len(profile_bytes) > MAX_PROFILE_BYTES:
        raise ValidationError(f"dyro.toml 超过 {MAX_PROFILE_BYTES} 字节上限")
    digest = hashlib.sha256(CONFIG_REVISION_DOMAIN + profile_bytes).hexdigest()
    return f"sha256:{digest}"

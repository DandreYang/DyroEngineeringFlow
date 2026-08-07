"""Typed, read-only MCP adapter for the Dyro Agent Bridge.

The optional MCP SDK is imported only while constructing the server.  All
business validation and observation work continues to cross the strict Bridge
transport so this module cannot become a second operation engine.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib
import json
import re
import sys
from typing import Callable, Literal, Protocol, TypeAlias

from ..canonical import canonical_json_bytes
from .constants import PROTOCOL_MAJOR, PROTOCOL_MINOR
from .transport import handle_request_bytes


INTEGRATION_NAME = "dyro-readonly"
INTEGRATION_VERSION = "0.1.0"

TOOL_OPERATION_PAIRS = (
    ("dyro_hello", "bridge.hello"),
    ("dyro_capabilities", "bridge.capabilities.compact"),
    ("dyro_operation_schema", "bridge.operation.schema"),
    ("dyro_workspace_resolve", "workspace.resolve"),
    ("dyro_workspace_list", "workspace.list"),
    ("dyro_workspace_observe", "workspace.observe"),
    ("dyro_objective_plan", "objective.plan"),
)
TOOL_NAMES = tuple(name for name, _ in TOOL_OPERATION_PAIRS)
EXPOSED_OPERATIONS = tuple(operation for _, operation in TOOL_OPERATION_PAIRS)
TOOL_LIST_DIGEST = (
    "sha256:"
    + hashlib.sha256(
        canonical_json_bytes(
            {
                "integration": INTEGRATION_NAME,
                "integration_version": INTEGRATION_VERSION,
                "tools": [
                    {"name": name, "operation": operation}
                    for name, operation in TOOL_OPERATION_PAIRS
                ],
            }
        )
    ).hexdigest()
)

ExposedOperation: TypeAlias = Literal[
    "bridge.hello",
    "bridge.capabilities.compact",
    "bridge.operation.schema",
    "workspace.resolve",
    "workspace.list",
    "workspace.observe",
    "objective.plan",
]

_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_SEMVER = re.compile(
    r"^(?P<major>0|[1-9][0-9]*)\.(?P<minor>0|[1-9][0-9]*)\."
    r"(?P<patch>0|[1-9][0-9]*)(?:[-+][0-9A-Za-z.-]+)?$"
)
_BRIDGE_VERSION = re.compile(r"^(?P<major>0|[1-9][0-9]*)\.(?P<minor>0|[1-9][0-9]*)$")


class _Server(Protocol):
    def tool(self, **kwargs: object): ...

    def run(self) -> object: ...


class IntegrationUnavailable(RuntimeError):
    """Stable, non-sensitive adapter failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class MCPDependencyUnavailable(IntegrationUnavailable):
    def __init__(self) -> None:
        super().__init__("MCP_DEPENDENCY_UNAVAILABLE")


@dataclass(frozen=True)
class CompatibilityPolicy:
    """Independent integration compatibility declaration."""

    core_major: int = 0
    core_minor_min: int = 5
    core_minor_max: int = 6
    bridge_major: int = 1
    bridge_minor_min: int = 0
    bridge_minor_max: int = 0
    protocol_major: int = PROTOCOL_MAJOR
    protocol_minor_min: int = 0
    protocol_minor_max: int = PROTOCOL_MINOR
    operation_schema_min: int = 1
    operation_schema_max: int = 1

    def __post_init__(self) -> None:
        ranges = (
            (self.core_minor_min, self.core_minor_max),
            (self.bridge_minor_min, self.bridge_minor_max),
            (self.protocol_minor_min, self.protocol_minor_max),
            (self.operation_schema_min, self.operation_schema_max),
        )
        values = (
            self.core_major,
            self.bridge_major,
            self.protocol_major,
            *(item for bounds in ranges for item in bounds),
        )
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in values
        ) or any(lower > upper for lower, upper in ranges):
            raise ValueError("invalid compatibility policy")

    def public_dict(self) -> dict[str, object]:
        return {
            "core": {
                "major": self.core_major,
                "minor": {
                    "minimum": self.core_minor_min,
                    "maximum": self.core_minor_max,
                },
            },
            "bridge": {
                "major": self.bridge_major,
                "minor": {
                    "minimum": self.bridge_minor_min,
                    "maximum": self.bridge_minor_max,
                },
            },
            "protocol": {
                "major": self.protocol_major,
                "minor": {
                    "minimum": self.protocol_minor_min,
                    "maximum": self.protocol_minor_max,
                },
            },
            "operation_schema": {
                "minimum": self.operation_schema_min,
                "maximum": self.operation_schema_max,
            },
            "planner": {"objective.plan": ["objective-plan/1"]},
        }


DEFAULT_COMPATIBILITY = CompatibilityPolicy()


@dataclass(frozen=True)
class IntegrationHandshake:
    core_version: str
    bridge_version: str
    protocol_major: int
    protocol_minor: int
    capabilities_digest: str

    def public_dict(self, policy: CompatibilityPolicy) -> dict[str, object]:
        return {
            "name": INTEGRATION_NAME,
            "version": INTEGRATION_VERSION,
            "scope": "inspect-and-plan",
            "core_version": self.core_version,
            "bridge_version": self.bridge_version,
            "protocol": {
                "major": self.protocol_major,
                "minor": self.protocol_minor,
            },
            "compatibility": policy.public_dict(),
            "capabilities_digest": self.capabilities_digest,
            "tool_list_digest": TOOL_LIST_DIGEST,
        }


Transport = Callable[[bytes], tuple[dict[str, object], int]]


def _version_pair(
    value: object, pattern: re.Pattern[str], code: str
) -> tuple[int, int]:
    if not isinstance(value, str):
        raise IntegrationUnavailable(code)
    match = pattern.fullmatch(value)
    if match is None:
        raise IntegrationUnavailable(code)
    return int(match.group("major")), int(match.group("minor"))


def _object(value: object, code: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise IntegrationUnavailable(code)
    return value


class ReadonlyBridge:
    """Pinned typed projection over the strict Bridge byte transport."""

    def __init__(
        self,
        *,
        transport: Transport = handle_request_bytes,
        compatibility: CompatibilityPolicy = DEFAULT_COMPATIBILITY,
    ) -> None:
        self._transport = transport
        self._compatibility = compatibility
        self._handshake: IntegrationHandshake | None = None

    @property
    def handshake_result(self) -> IntegrationHandshake:
        if self._handshake is None:
            raise IntegrationUnavailable("INTEGRATION_HANDSHAKE_REQUIRED")
        return self._handshake

    def _request(
        self, operation: str, input_data: dict[str, object]
    ) -> dict[str, object]:
        request = {
            "protocol": {"major": PROTOCOL_MAJOR, "minor": PROTOCOL_MINOR},
            "client": {"name": INTEGRATION_NAME, "version": INTEGRATION_VERSION},
            "operation": operation,
            "input": input_data,
        }
        response, exit_code = self._transport(canonical_json_bytes(request))
        if not isinstance(exit_code, int) or not isinstance(response, dict):
            raise IntegrationUnavailable("CORE_TRANSPORT_UNAVAILABLE")
        if (exit_code == 0) != (response.get("ok") is True):
            raise IntegrationUnavailable("CORE_TRANSPORT_UNAVAILABLE")
        return response

    def _validate_response_binding(
        self,
        response: dict[str, object],
        operation: str,
        *,
        core_version: str,
        bridge_version: str,
        protocol_major: int,
        protocol_minor: int,
        capabilities_digest: str | None,
    ) -> str:
        if not isinstance(response.get("ok"), bool):
            raise IntegrationUnavailable("CORE_RESPONSE_INVALID")
        meta = _object(response.get("meta"), "CORE_RESPONSE_INVALID")
        protocol = _object(meta.get("server_protocol"), "CORE_PROTOCOL_INCOMPATIBLE")
        if (
            protocol.get("major") != protocol_major
            or protocol.get("minor") != protocol_minor
            or meta.get("requested_protocol")
            != {"major": PROTOCOL_MAJOR, "minor": PROTOCOL_MINOR}
            or meta.get("dyro_version") != core_version
            or meta.get("bridge_version") != bridge_version
            or meta.get("operation") != operation
        ):
            raise IntegrationUnavailable("CORE_HANDSHAKE_CHANGED")
        schema_version = meta.get("operation_schema_version")
        if (
            not isinstance(schema_version, int)
            or isinstance(schema_version, bool)
            or not self._compatibility.operation_schema_min
            <= schema_version
            <= self._compatibility.operation_schema_max
        ):
            raise IntegrationUnavailable("OPERATION_SCHEMA_INCOMPATIBLE")
        expected_planner = "objective-plan/1" if operation == "objective.plan" else None
        if meta.get("planner_revision") != expected_planner:
            raise IntegrationUnavailable("PLANNER_REVISION_INCOMPATIBLE")
        digest = meta.get("capabilities_digest")
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise IntegrationUnavailable("CAPABILITIES_DIGEST_INVALID")
        if capabilities_digest is not None and digest != capabilities_digest:
            raise IntegrationUnavailable("CORE_HANDSHAKE_CHANGED")
        return digest

    def handshake(self) -> IntegrationHandshake:
        if self._handshake is not None:
            return self._handshake
        hello = self._request("bridge.hello", {})
        if hello.get("ok") is not True:
            raise IntegrationUnavailable("CORE_HANDSHAKE_UNAVAILABLE")
        hello_data = _object(hello.get("data"), "CORE_HANDSHAKE_INVALID")
        server_protocol = _object(
            hello_data.get("server_protocol"), "CORE_PROTOCOL_INCOMPATIBLE"
        )
        major = server_protocol.get("major")
        minor = server_protocol.get("minor")
        policy = self._compatibility
        if (
            not isinstance(major, int)
            or isinstance(major, bool)
            or major != policy.protocol_major
            or not isinstance(minor, int)
            or isinstance(minor, bool)
            or not policy.protocol_minor_min <= minor <= policy.protocol_minor_max
        ):
            raise IntegrationUnavailable("CORE_PROTOCOL_INCOMPATIBLE")

        core_major, core_minor = _version_pair(
            hello_data.get("dyro_version"), _SEMVER, "CORE_VERSION_INCOMPATIBLE"
        )
        if core_major != policy.core_major or not (
            policy.core_minor_min <= core_minor <= policy.core_minor_max
        ):
            raise IntegrationUnavailable("CORE_VERSION_INCOMPATIBLE")
        bridge_major, bridge_minor = _version_pair(
            hello_data.get("bridge_version"),
            _BRIDGE_VERSION,
            "BRIDGE_VERSION_INCOMPATIBLE",
        )
        if bridge_major != policy.bridge_major or not (
            policy.bridge_minor_min <= bridge_minor <= policy.bridge_minor_max
        ):
            raise IntegrationUnavailable("BRIDGE_VERSION_INCOMPATIBLE")

        hello_digest = self._validate_response_binding(
            hello,
            "bridge.hello",
            core_version=str(hello_data["dyro_version"]),
            bridge_version=str(hello_data["bridge_version"]),
            protocol_major=major,
            protocol_minor=minor,
            capabilities_digest=None,
        )
        capabilities = self._request("bridge.capabilities.compact", {})
        capability_digest = self._validate_response_binding(
            capabilities,
            "bridge.capabilities.compact",
            core_version=str(hello_data["dyro_version"]),
            bridge_version=str(hello_data["bridge_version"]),
            protocol_major=major,
            protocol_minor=minor,
            capabilities_digest=hello_digest,
        )
        if capabilities.get("ok") is not True:
            raise IntegrationUnavailable("CORE_HANDSHAKE_UNAVAILABLE")
        capability_data = _object(capabilities.get("data"), "CORE_HANDSHAKE_INVALID")
        digest = capability_data.get("capabilities_digest")
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise IntegrationUnavailable("CAPABILITIES_DIGEST_INVALID")
        capability_digest = self._validate_response_binding(
            capabilities,
            "bridge.capabilities.compact",
            core_version=str(hello_data["dyro_version"]),
            bridge_version=str(hello_data["bridge_version"]),
            protocol_major=major,
            protocol_minor=minor,
            capabilities_digest=digest,
        )
        if hello_digest != digest or capability_digest != digest:
            raise IntegrationUnavailable("CAPABILITIES_DIGEST_MISMATCH")

        operations = capability_data.get("operations")
        if not isinstance(operations, list):
            raise IntegrationUnavailable("CORE_CAPABILITIES_INVALID")
        by_id: dict[str, dict[str, object]] = {}
        for item in operations:
            if not isinstance(item, dict) or not isinstance(item.get("operation"), str):
                raise IntegrationUnavailable("CORE_CAPABILITIES_INVALID")
            operation = item["operation"]
            if operation in by_id:
                raise IntegrationUnavailable("CORE_CAPABILITIES_INVALID")
            by_id[operation] = item
        for operation in EXPOSED_OPERATIONS:
            capability = by_id.get(operation)
            if capability is None:
                raise IntegrationUnavailable("CORE_OPERATION_UNKNOWN")
            expected_kind = "plan" if operation == "objective.plan" else "inspect"
            expected_risk = "PLAN" if operation == "objective.plan" else "R0"
            if (
                capability.get("available") is not True
                or capability.get("kind") != expected_kind
                or capability.get("maximum_risk") != expected_risk
            ):
                raise IntegrationUnavailable("CORE_OPERATION_UNAVAILABLE")
            schema_version = capability.get("operation_schema_version")
            if (
                not isinstance(schema_version, int)
                or isinstance(schema_version, bool)
                or not policy.operation_schema_min
                <= schema_version
                <= policy.operation_schema_max
            ):
                raise IntegrationUnavailable("OPERATION_SCHEMA_INCOMPATIBLE")
            expected_planner = (
                "objective-plan/1" if operation == "objective.plan" else None
            )
            if capability.get("planner_revision") != expected_planner:
                raise IntegrationUnavailable("PLANNER_REVISION_INCOMPATIBLE")

        result = IntegrationHandshake(
            core_version=str(hello_data["dyro_version"]),
            bridge_version=str(hello_data["bridge_version"]),
            protocol_major=major,
            protocol_minor=minor,
            capabilities_digest=digest,
        )
        self._handshake = result
        return result

    def _call(self, operation: str, input_data: dict[str, object]) -> dict[str, object]:
        if operation not in EXPOSED_OPERATIONS:
            raise IntegrationUnavailable("INTEGRATION_OPERATION_UNKNOWN")
        handshake = self.handshake()
        response = self._request(operation, input_data)
        self._validate_response_binding(
            response,
            operation,
            core_version=handshake.core_version,
            bridge_version=handshake.bridge_version,
            protocol_major=handshake.protocol_major,
            protocol_minor=handshake.protocol_minor,
            capabilities_digest=handshake.capabilities_digest,
        )
        return response

    def dyro_hello(self) -> dict[str, object]:
        """Report the versioned Dyro read-only integration handshake."""
        response = self._call("bridge.hello", {})
        if response.get("ok") is not True:
            return response
        return {
            "ok": True,
            "integration": self.handshake_result.public_dict(self._compatibility),
            "core": response,
        }

    def dyro_capabilities(self) -> dict[str, object]:
        """List the pinned Dyro inspect-and-plan capability surface."""
        response = self._call("bridge.capabilities.compact", {})
        if response.get("ok") is not True:
            return response
        projected = json.loads(json.dumps(response))
        data = _object(projected.get("data"), "CORE_RESPONSE_INVALID")
        operations = data.get("operations")
        if not isinstance(operations, list):
            raise IntegrationUnavailable("CORE_RESPONSE_INVALID")
        by_id = {
            item.get("operation"): item for item in operations if isinstance(item, dict)
        }
        try:
            data["operations"] = [by_id[operation] for operation in EXPOSED_OPERATIONS]
        except KeyError:
            raise IntegrationUnavailable("CORE_RESPONSE_INVALID") from None
        data["integration_version"] = INTEGRATION_VERSION
        data["tool_list_digest"] = TOOL_LIST_DIGEST
        return projected

    def dyro_operation_schema(self, operation: ExposedOperation) -> dict[str, object]:
        """Return the strict schema for one pinned Dyro operation."""
        if operation not in EXPOSED_OPERATIONS:
            raise IntegrationUnavailable("INTEGRATION_OPERATION_UNKNOWN")
        return self._call("bridge.operation.schema", {"operation": operation})

    def dyro_workspace_resolve(self, workspace: str | None = None) -> dict[str, object]:
        """Resolve a Dyro workspace without changing recency or state."""
        return self._call("workspace.resolve", {"workspace": workspace})

    def dyro_workspace_list(self) -> dict[str, object]:
        """List registered Dyro workspaces as bounded observations."""
        return self._call("workspace.list", {})

    def dyro_workspace_observe(self, workspace: str | None = None) -> dict[str, object]:
        """Observe bounded Dyro workspace control-plane state."""
        return self._call("workspace.observe", {"workspace": workspace})

    def dyro_objective_plan(
        self,
        objective_id: str,
        workspace: str | None = None,
    ) -> dict[str, object]:
        """Build a non-executable Dyro objective plan with no authorization."""
        response = self._call(
            "objective.plan",
            {"objective_id": objective_id, "workspace": workspace},
        )
        if response.get("ok") is True:
            data = _object(response.get("data"), "CORE_RESPONSE_INVALID")
            if (
                data.get("executable") is not False
                or data.get("authorization") != "none"
            ):
                raise IntegrationUnavailable("PLAN_SAFETY_INVARIANT_FAILED")
        return response


def _load_mcp_runtime() -> tuple[type[_Server], Callable[..., object]]:
    try:
        server_module = importlib.import_module("mcp.server")
        types_module = importlib.import_module("mcp.types")
        server = getattr(server_module, "MCPServer")
        annotations = getattr(types_module, "ToolAnnotations")
    except (ImportError, AttributeError):
        raise MCPDependencyUnavailable() from None
    return server, annotations


def _tool_text(response: dict[str, object]) -> str:
    return canonical_json_bytes(response).decode("utf-8")


def create_server(
    *,
    bridge: ReadonlyBridge | None = None,
    server_class: type[_Server] | None = None,
    annotation_factory: Callable[..., object] | None = None,
) -> _Server:
    """Construct the fixed MCP surface after a successful Core handshake."""
    if server_class is None:
        factory, annotations = _load_mcp_runtime()
    else:
        factory = server_class
        annotations = annotation_factory or (lambda **values: values)
    active = bridge or ReadonlyBridge()
    active.handshake()
    server = factory("Dyro Readonly")

    def dyro_hello() -> str:
        """Report the versioned Dyro read-only integration handshake."""
        return _tool_text(active.dyro_hello())

    def dyro_capabilities() -> str:
        """List the pinned Dyro inspect-and-plan capability surface."""
        return _tool_text(active.dyro_capabilities())

    def dyro_operation_schema(operation: ExposedOperation) -> str:
        """Return the strict schema for one pinned Dyro operation."""
        return _tool_text(active.dyro_operation_schema(operation))

    def dyro_workspace_resolve(workspace: str | None = None) -> str:
        """Resolve a Dyro workspace without changing recency or state."""
        return _tool_text(active.dyro_workspace_resolve(workspace))

    def dyro_workspace_list() -> str:
        """List registered Dyro workspaces as bounded observations."""
        return _tool_text(active.dyro_workspace_list())

    def dyro_workspace_observe(workspace: str | None = None) -> str:
        """Observe bounded Dyro workspace control-plane state."""
        return _tool_text(active.dyro_workspace_observe(workspace))

    def dyro_objective_plan(objective_id: str, workspace: str | None = None) -> str:
        """Build a non-executable Dyro objective plan with no authorization."""
        return _tool_text(active.dyro_objective_plan(objective_id, workspace))

    tool_functions = (
        dyro_hello,
        dyro_capabilities,
        dyro_operation_schema,
        dyro_workspace_resolve,
        dyro_workspace_list,
        dyro_workspace_observe,
        dyro_objective_plan,
    )
    hints = annotations(read_only_hint=True, open_world_hint=False)
    for function in tool_functions:
        server.tool(annotations=hints, structured_output=False)(function)
    return server


def _unavailable_payload(code: str) -> bytes:
    return (
        canonical_json_bytes(
            {
                "ok": False,
                "integration": {
                    "name": INTEGRATION_NAME,
                    "version": INTEGRATION_VERSION,
                },
                "error": {
                    "code": code,
                    "message": "The optional read-only MCP integration is unavailable.",
                },
            }
        )
        + b"\n"
    )


def main() -> int:
    """Run the optional stdio MCP server with bounded startup failures."""
    try:
        server = create_server()
        server.run()
    except MCPDependencyUnavailable as exc:
        sys.stderr.buffer.write(_unavailable_payload(exc.code))
        return 4
    except IntegrationUnavailable as exc:
        sys.stderr.buffer.write(_unavailable_payload(exc.code))
        return 4
    except Exception:
        sys.stderr.buffer.write(_unavailable_payload("MCP_SERVER_UNAVAILABLE"))
        return 3
    return 0


__all__ = [
    "CompatibilityPolicy",
    "DEFAULT_COMPATIBILITY",
    "EXPOSED_OPERATIONS",
    "INTEGRATION_NAME",
    "INTEGRATION_VERSION",
    "IntegrationUnavailable",
    "MCPDependencyUnavailable",
    "ReadonlyBridge",
    "TOOL_LIST_DIGEST",
    "TOOL_NAMES",
    "create_server",
    "main",
]

from __future__ import annotations

import asyncio
from io import BytesIO, TextIOWrapper
import inspect
import json
import sys
import unittest
from unittest.mock import patch

from dyro.bridge import mcp


_DIGEST = "sha256:" + "a" * 64


def _capability(operation: str) -> dict[str, object]:
    planning = operation == "objective.plan"
    return {
        "operation": operation,
        "kind": "plan" if planning else "inspect",
        "maximum_risk": "PLAN" if planning else "R0",
        "available": True,
        "operation_schema_version": 1,
        "planner_revision": "objective-plan/1" if planning else None,
    }


class CoreFixture:
    def __init__(
        self,
        *,
        core_version: str = "0.6.0",
        bridge_version: str = "1.0",
        protocol_major: int = 1,
        protocol_minor: int = 0,
        operations: list[dict[str, object]] | None = None,
        digest: str = _DIGEST,
    ) -> None:
        self.core_version = core_version
        self.bridge_version = bridge_version
        self.protocol_major = protocol_major
        self.protocol_minor = protocol_minor
        self.operations = operations or [
            _capability(operation) for operation in mcp.EXPOSED_OPERATIONS
        ]
        self.digest = digest
        self.requests: list[dict[str, object]] = []

    def __call__(self, raw: bytes) -> tuple[dict[str, object], int]:
        request = json.loads(raw)
        self.requests.append(request)
        operation = request["operation"]
        planner = "objective-plan/1" if operation == "objective.plan" else None
        meta = {
            "server_protocol": {
                "major": self.protocol_major,
                "minor": self.protocol_minor,
            },
            "requested_protocol": request["protocol"],
            "dyro_version": self.core_version,
            "bridge_version": self.bridge_version,
            "operation": operation,
            "operation_schema_version": 1,
            "planner_revision": planner,
            "request_id": None,
            "event_id": "evt_test",
            "capabilities_digest": self.digest,
            "partial": False,
            "truncated": False,
        }
        if operation == "bridge.hello":
            data: dict[str, object] = {
                "dyro_version": self.core_version,
                "bridge_version": self.bridge_version,
                "server_protocol": {
                    "major": self.protocol_major,
                    "minor": self.protocol_minor,
                },
            }
        elif operation == "bridge.capabilities.compact":
            data = {
                "operations": self.operations,
                "capabilities_digest": self.digest,
            }
        elif operation == "bridge.operation.schema":
            data = {
                "operation": request["input"]["operation"],
                "operation_schema_version": 1,
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object"},
                "schema_digest": "sha256:" + "b" * 64,
            }
        elif operation == "objective.plan":
            data = {
                "executable": False,
                "authorization": "none",
                "operation": operation,
            }
        else:
            data = {"operation": operation, "input": request["input"]}
        return {"ok": True, "meta": meta, "data": data, "warnings": []}, 0


class FakeMCPServer:
    def __init__(self, name: str) -> None:
        self.name = name
        self.tools: dict[str, object] = {}
        self.tool_options: dict[str, dict[str, object]] = {}

    def tool(self, **options: object):
        def register(function):
            self.tools[function.__name__] = function
            self.tool_options[function.__name__] = options
            return function

        return register

    def run(self) -> None:
        return None


class BridgeMCPTests(unittest.TestCase):
    def test_module_import_and_missing_dependency_are_fail_closed(self) -> None:
        stderr_bytes = BytesIO()
        stderr = TextIOWrapper(stderr_bytes, encoding="utf-8")
        with (
            patch.object(
                mcp, "_load_mcp_runtime", side_effect=mcp.MCPDependencyUnavailable()
            ),
            patch.object(sys, "stderr", stderr),
        ):
            exit_code = mcp.main()
            stderr.flush()
        payload = stderr_bytes.getvalue().decode("utf-8")
        self.assertEqual(exit_code, 4)
        self.assertEqual(
            json.loads(payload)["error"]["code"], "MCP_DEPENDENCY_UNAVAILABLE"
        )
        self.assertNotIn("Traceback", payload)
        self.assertNotIn("\x1b", payload)

    def test_server_registers_exact_pinned_typed_tool_surface(self) -> None:
        core = CoreFixture()
        server = mcp.create_server(
            bridge=mcp.ReadonlyBridge(transport=core), server_class=FakeMCPServer
        )
        self.assertEqual(tuple(server.tools), mcp.TOOL_NAMES)
        self.assertEqual(
            set(inspect.signature(server.tools["dyro_workspace_resolve"]).parameters),
            {"workspace"},
        )
        self.assertEqual(
            set(inspect.signature(server.tools["dyro_objective_plan"]).parameters),
            {"objective_id", "workspace"},
        )
        operation_annotation = (
            inspect.signature(server.tools["dyro_operation_schema"])
            .parameters["operation"]
            .annotation
        )
        self.assertEqual(operation_annotation, "ExposedOperation")
        for tool in server.tools.values():
            self.assertNotIn("**", str(inspect.signature(tool)))
            self.assertTrue(inspect.getdoc(tool))
        for options in server.tool_options.values():
            self.assertIs(options["structured_output"], False)
            self.assertEqual(
                options["annotations"],
                {"read_only_hint": True, "open_world_hint": False},
            )

    def test_every_tool_maps_to_one_fixed_core_request(self) -> None:
        core = CoreFixture()
        bridge = mcp.ReadonlyBridge(transport=core)
        bridge.handshake()
        cases = (
            (bridge.dyro_hello, (), "bridge.hello", {}),
            (bridge.dyro_capabilities, (), "bridge.capabilities.compact", {}),
            (
                bridge.dyro_operation_schema,
                ("workspace.observe",),
                "bridge.operation.schema",
                {"operation": "workspace.observe"},
            ),
            (
                bridge.dyro_workspace_resolve,
                ("demo",),
                "workspace.resolve",
                {"workspace": "demo"},
            ),
            (bridge.dyro_workspace_list, (), "workspace.list", {}),
            (
                bridge.dyro_workspace_observe,
                ("demo",),
                "workspace.observe",
                {"workspace": "demo"},
            ),
            (
                bridge.dyro_objective_plan,
                ("OBJ-1", "demo"),
                "objective.plan",
                {"objective_id": "OBJ-1", "workspace": "demo"},
            ),
        )
        for function, args, operation, expected_input in cases:
            before = len(core.requests)
            function(*args)
            self.assertEqual(len(core.requests), before + 1)
            request = core.requests[-1]
            self.assertEqual(request["operation"], operation)
            self.assertEqual(request["input"], expected_input)
            self.assertEqual(
                request["client"], {"name": "dyro-readonly", "version": "0.1.0"}
            )
            self.assertEqual(request["protocol"], {"major": 1, "minor": 0})

    def test_capabilities_are_filtered_and_new_core_operation_does_not_widen_tools(
        self,
    ) -> None:
        extra = _capability("task.list")
        core = CoreFixture(
            operations=[
                *[_capability(operation) for operation in mcp.EXPOSED_OPERATIONS],
                extra,
            ]
        )
        bridge = mcp.ReadonlyBridge(transport=core)
        bridge.handshake()
        response = bridge.dyro_capabilities()
        operation_ids = tuple(
            item["operation"] for item in response["data"]["operations"]
        )
        self.assertEqual(operation_ids, mcp.EXPOSED_OPERATIONS)
        self.assertEqual(response["data"]["tool_list_digest"], mcp.TOOL_LIST_DIGEST)

    def test_current_and_n_minus_one_core_versions_are_compatible(self) -> None:
        for version in ("0.6.9", "0.5.0"):
            with self.subTest(version=version):
                bridge = mcp.ReadonlyBridge(transport=CoreFixture(core_version=version))
                self.assertEqual(bridge.handshake().core_version, version)

    def test_core_newer_and_integration_newer_fail_closed(self) -> None:
        with self.assertRaisesRegex(
            mcp.IntegrationUnavailable, "CORE_VERSION_INCOMPATIBLE"
        ):
            mcp.ReadonlyBridge(transport=CoreFixture(core_version="0.7.0")).handshake()
        newer_integration = mcp.CompatibilityPolicy(core_minor_min=7, core_minor_max=8)
        with self.assertRaisesRegex(
            mcp.IntegrationUnavailable, "CORE_VERSION_INCOMPATIBLE"
        ):
            mcp.ReadonlyBridge(
                transport=CoreFixture(core_version="0.6.0"),
                compatibility=newer_integration,
            ).handshake()

    def test_major_schema_planner_unknown_and_digest_skew_fail_closed(self) -> None:
        scenarios: list[tuple[CoreFixture, str]] = [
            (CoreFixture(core_version="1.6.0"), "CORE_VERSION_INCOMPATIBLE"),
            (CoreFixture(protocol_major=2), "CORE_PROTOCOL_INCOMPATIBLE"),
            (CoreFixture(digest="not-a-digest"), "CAPABILITIES_DIGEST_INVALID"),
        ]
        missing = [
            _capability(operation)
            for operation in mcp.EXPOSED_OPERATIONS
            if operation != "workspace.observe"
        ]
        scenarios.append((CoreFixture(operations=missing), "CORE_OPERATION_UNKNOWN"))
        future_schema = [_capability(operation) for operation in mcp.EXPOSED_OPERATIONS]
        future_schema[-1]["operation_schema_version"] = 2
        scenarios.append(
            (CoreFixture(operations=future_schema), "OPERATION_SCHEMA_INCOMPATIBLE")
        )
        unknown_planner = [
            _capability(operation) for operation in mcp.EXPOSED_OPERATIONS
        ]
        unknown_planner[-1]["planner_revision"] = "objective-plan/unknown"
        scenarios.append(
            (CoreFixture(operations=unknown_planner), "PLANNER_REVISION_INCOMPATIBLE")
        )
        for fixture, code in scenarios:
            with (
                self.subTest(code=code),
                self.assertRaisesRegex(mcp.IntegrationUnavailable, code),
            ):
                mcp.ReadonlyBridge(transport=fixture).handshake()

    def test_objective_plan_reasserts_no_execution_or_authorization(self) -> None:
        core = CoreFixture()
        bridge = mcp.ReadonlyBridge(transport=core)
        bridge.handshake()
        response = bridge.dyro_objective_plan("OBJ-1")
        self.assertIs(response["data"]["executable"], False)
        self.assertEqual(response["data"]["authorization"], "none")

    def test_error_response_must_remain_bound_to_the_handshake(self) -> None:
        core = CoreFixture()
        bridge = mcp.ReadonlyBridge(transport=core)
        bridge.handshake()

        def drifted_error(raw: bytes) -> tuple[dict[str, object], int]:
            response, _ = core(raw)
            response["ok"] = False
            response.pop("data", None)
            response["error"] = {"code": "OPERATION_UNAVAILABLE"}
            response["meta"]["capabilities_digest"] = "sha256:" + "c" * 64
            return response, 4

        bridge._transport = drifted_error
        with self.assertRaisesRegex(
            mcp.IntegrationUnavailable, "CORE_HANDSHAKE_CHANGED"
        ):
            bridge.dyro_workspace_list()

    def test_second_handshake_error_is_validated_before_failing_closed(self) -> None:
        core = CoreFixture()

        def drifted_capabilities(raw: bytes) -> tuple[dict[str, object], int]:
            response, exit_code = core(raw)
            request = json.loads(raw)
            if request["operation"] == "bridge.capabilities.compact":
                response["ok"] = False
                response.pop("data", None)
                response["error"] = {"code": "OPERATION_UNAVAILABLE"}
                response["meta"]["capabilities_digest"] = "sha256:" + "c" * 64
                exit_code = 4
            return response, exit_code

        with self.assertRaisesRegex(
            mcp.IntegrationUnavailable, "CORE_HANDSHAKE_CHANGED"
        ):
            mcp.ReadonlyBridge(transport=drifted_capabilities).handshake()

    def test_bound_second_handshake_error_fails_without_exposing_core_error(
        self,
    ) -> None:
        core = CoreFixture()

        def unavailable_capabilities(raw: bytes) -> tuple[dict[str, object], int]:
            response, exit_code = core(raw)
            request = json.loads(raw)
            if request["operation"] == "bridge.capabilities.compact":
                response["ok"] = False
                response.pop("data", None)
                response["error"] = {"code": "OPERATION_UNAVAILABLE"}
                exit_code = 4
            return response, exit_code

        with self.assertRaisesRegex(
            mcp.IntegrationUnavailable, "CORE_HANDSHAKE_UNAVAILABLE"
        ):
            mcp.ReadonlyBridge(transport=unavailable_capabilities).handshake()

    def test_mcp_surface_has_no_caller_supplied_start_path(self) -> None:
        server = mcp.create_server(
            bridge=mcp.ReadonlyBridge(transport=CoreFixture()),
            server_class=FakeMCPServer,
        )
        for name in (
            "dyro_workspace_resolve",
            "dyro_workspace_observe",
            "dyro_objective_plan",
        ):
            self.assertNotIn("start", inspect.signature(server.tools[name]).parameters)


def _has_mcp_v2() -> bool:
    try:
        from mcp import Client  # noqa: F401
        from mcp.server import MCPServer  # noqa: F401
    except ImportError:
        return False
    return True


@unittest.skipUnless(_has_mcp_v2(), "mcp v2 optional dependency is not installed")
class MCPV2IntegrationTests(unittest.TestCase):
    def test_sdk_advertises_the_exact_tool_list(self) -> None:
        from mcp import Client

        async def inspect_tools() -> tuple[str, ...]:
            server = mcp.create_server(
                bridge=mcp.ReadonlyBridge(transport=CoreFixture())
            )
            async with Client(server) as client:
                result = await client.list_tools()
                return tuple(tool.name for tool in result.tools)

        self.assertEqual(asyncio.run(inspect_tools()), mcp.TOOL_NAMES)


if __name__ == "__main__":
    unittest.main()

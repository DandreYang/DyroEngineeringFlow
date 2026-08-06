from __future__ import annotations

from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import tempfile
import unittest

from dyro.bridge.models import (
    MAX_PROFILE_BYTES,
    AvailabilityState,
    BridgeError,
    BridgeNextAction,
    BridgeWarning,
    ErrorCode,
    NextActionKind,
    OperationKind,
    OperationSpec,
    PlatformAvailability,
    PlatformState,
    ProtocolVersion,
    RequestMetadata,
    ResponseMetadata,
    RiskClass,
    config_revision,
    workspace_identity,
)
from dyro.errors import ValidationError


class BridgeModelTests(unittest.TestCase):
    @staticmethod
    def _vectors() -> dict[str, object]:
        return json.loads(
            Path("tests/fixtures/bridge/contracts-v1.json").read_text(encoding="utf-8")
        )

    def test_protocol_and_metadata_are_immutable_and_json_safe(self) -> None:
        protocol = ProtocolVersion(1, 0)
        request = RequestMetadata(
            requested_protocol=protocol,
            request_id="req-1",
            client_name="codex-integration",
            client_version="0.1.0",
            operation="workspace.resolve",
        )
        response = ResponseMetadata(
            server_protocol=protocol,
            requested_protocol=protocol,
            dyro_version="0.6.0",
            bridge_version="1.0",
            operation="workspace.resolve",
            operation_schema_version=1,
            planner_revision=None,
            request_id=request.request_id,
            event_id="evt_123",
            capabilities_digest="sha256:" + "a" * 64,
        )

        self.assertEqual(
            response.as_dict(),
            {
                "server_protocol": {"major": 1, "minor": 0},
                "requested_protocol": {"major": 1, "minor": 0},
                "dyro_version": "0.6.0",
                "bridge_version": "1.0",
                "operation": "workspace.resolve",
                "operation_schema_version": 1,
                "planner_revision": None,
                "request_id": "req-1",
                "event_id": "evt_123",
                "capabilities_digest": "sha256:" + "a" * 64,
                "partial": False,
                "truncated": False,
            },
        )
        with self.assertRaises(FrozenInstanceError):
            protocol.major = 2  # type: ignore[misc]

    def test_transport_error_metadata_can_omit_unparsed_request_fields(self) -> None:
        metadata = ResponseMetadata(
            server_protocol=ProtocolVersion(1, 0),
            requested_protocol=None,
            dyro_version="0.6.0",
            bridge_version="1.0",
            operation=None,
            operation_schema_version=None,
            planner_revision=None,
            request_id=None,
            event_id="evt_parse",
            capabilities_digest="sha256:" + "b" * 64,
        )
        self.assertIsNone(metadata.as_dict()["requested_protocol"])
        self.assertIsNone(metadata.as_dict()["operation"])

    def test_warning_and_error_expose_bounded_structured_values(self) -> None:
        warning = BridgeWarning("RESULT_TRUNCATED", "Some records were omitted.")
        error = BridgeError(
            ErrorCode.SCHEMA_VALIDATION_FAILED,
            "The operation input is invalid.",
            details=(("field", "workspace"),),
            next_actions=(
                BridgeNextAction(
                    NextActionKind.INSPECT_INPUT,
                    "Inspect the operation input",
                ),
            ),
        )
        self.assertEqual(warning.as_dict()["code"], "RESULT_TRUNCATED")
        self.assertEqual(error.as_dict()["code"], "SCHEMA_VALIDATION_FAILED")
        self.assertEqual(error.as_dict()["details"], {"field": "workspace"})
        self.assertEqual(
            error.as_dict()["next_actions"],
            [{"kind": "inspect_input", "label": "Inspect the operation input"}],
        )

    def test_contracts_reject_wrong_runtime_types_and_mutable_containers(self) -> None:
        base = {
            "operation_id": "workspace.resolve",
            "kind": OperationKind.INSPECT,
            "maximum_risk": RiskClass.R0,
            "schema_version": 1,
            "planner_revision": None,
            "input_schema_id": "workspace.resolve.input.v1",
            "output_schema_id": "workspace.resolve.output.v1",
        }
        for override in (
            {"kind": "inspect"},
            {"maximum_risk": "R0"},
            {"availability_state": "declared"},
            {"platforms": []},
            {"platforms": ("linux-ubuntu-24.04",)},
        ):
            with self.subTest(override=override), self.assertRaises(ValidationError):
                OperationSpec(**(base | override))  # type: ignore[arg-type]

        with self.assertRaises(ValidationError):
            BridgeError("SCHEMA_VALIDATION_FAILED", "bad code")  # type: ignore[arg-type]
        with self.assertRaises(ValidationError):
            BridgeError(ErrorCode.INTERNAL_ERROR, "bad details", details=[])  # type: ignore[arg-type]
        with self.assertRaises(ValidationError):
            BridgeError(ErrorCode.INTERNAL_ERROR, "bad actions", next_actions=[])  # type: ignore[arg-type]
        with self.assertRaises(ValidationError):
            BridgeError(
                ErrorCode.INTERNAL_ERROR,
                "shell action",
                next_actions=("dyro doctor",),  # type: ignore[arg-type]
            )
        with self.assertRaises(ValidationError):
            BridgeError(
                ErrorCode.INTERNAL_ERROR,
                "bad detail key",
                details=(([], "value"),),  # type: ignore[arg-type]
            )
        with self.assertRaises(ValidationError):
            RequestMetadata(
                requested_protocol={"major": 1, "minor": 0},  # type: ignore[arg-type]
                request_id=None,
                client_name="codex",
                client_version="1",
                operation="workspace.resolve",
            )
        with self.assertRaises(ValidationError):
            ResponseMetadata(
                server_protocol=ProtocolVersion(1, 0),
                requested_protocol=None,
                dyro_version="0.6.0",
                bridge_version="1.0",
                operation=None,
                operation_schema_version=None,
                planner_revision=None,
                request_id=None,
                event_id="evt_invalid_digest",
                capabilities_digest=7,  # type: ignore[arg-type]
            )

    def test_operation_spec_rejects_invalid_risk_and_lifecycle_combinations(
        self,
    ) -> None:
        with self.assertRaises(ValidationError):
            OperationSpec(
                operation_id="workspace.resolve",
                kind=OperationKind.INSPECT,
                maximum_risk=RiskClass.R1,
                schema_version=1,
                planner_revision=None,
                input_schema_id="workspace.resolve.input.v1",
                output_schema_id="workspace.resolve.output.v1",
            )
        with self.assertRaises(ValidationError):
            OperationSpec(
                operation_id="objective.plan",
                kind=OperationKind.PLAN,
                maximum_risk=RiskClass.PLAN,
                schema_version=1,
                planner_revision=None,
                input_schema_id="objective.plan.input.v1",
                output_schema_id="objective.plan.output.v1",
            )
        with self.assertRaises(ValidationError):
            OperationSpec(
                operation_id="workspace.resolve",
                kind=OperationKind.INSPECT,
                maximum_risk=RiskClass.R0,
                schema_version=1,
                planner_revision=None,
                input_schema_id="workspace.resolve.input.v1",
                output_schema_id="workspace.resolve.output.v1",
                availability_state=AvailabilityState.PUBLIC_AVAILABLE,
            )

    def test_public_operation_requires_service_and_available_platform(self) -> None:
        spec = OperationSpec(
            operation_id="workspace.resolve",
            kind=OperationKind.INSPECT,
            maximum_risk=RiskClass.R0,
            schema_version=1,
            planner_revision=None,
            input_schema_id="workspace.resolve.input.v1",
            output_schema_id="workspace.resolve.output.v1",
            availability_state=AvailabilityState.PUBLIC_AVAILABLE,
            service_id="dyro.bridge.observations.resolve_workspace",
            platforms=(
                PlatformAvailability("linux-ubuntu-24.04", PlatformState.AVAILABLE),
            ),
        )
        self.assertTrue(spec.public_available)
        self.assertTrue(spec.available_on("linux-ubuntu-24.04"))
        self.assertFalse(spec.available_on("windows"))
        self.assertFalse(spec.available_on("unknown-platform"))
        self.assertEqual(spec.capability("linux-ubuntu-24.04")["available"], True)
        self.assertEqual(spec.capability("windows")["available"], False)
        self.assertNotIn("service_id", spec.capability("linux-ubuntu-24.04"))
        self.assertNotIn("input_schema_id", spec.capability("linux-ubuntu-24.04"))

    def test_implemented_operation_requires_a_bound_service(self) -> None:
        with self.assertRaises(ValidationError):
            OperationSpec(
                operation_id="workspace.resolve",
                kind=OperationKind.INSPECT,
                maximum_risk=RiskClass.R0,
                schema_version=1,
                planner_revision=None,
                input_schema_id="workspace.resolve.input.v1",
                output_schema_id="workspace.resolve.output.v1",
                availability_state=AvailabilityState.IMPLEMENTED_TESTABLE,
            )

    def test_workspace_and_config_revision_match_frozen_vectors(self) -> None:
        vectors = self._vectors()
        for item in vectors["workspace_identities"]:
            self.assertEqual(
                workspace_identity(Path(item["canonical_root"]), item["profile_name"]),
                item["value"],
            )
        for item in vectors["config_revisions"]:
            self.assertEqual(
                config_revision(bytes.fromhex(item["bytes_hex"])), item["value"]
            )
        with self.assertRaises(ValidationError):
            workspace_identity(Path("relative/workspace"), "acme")
        with self.assertRaises(ValidationError):
            workspace_identity(Path("/work/team/../acme"), "acme")
        with self.assertRaises(ValidationError):
            workspace_identity(Path("/work/acme"), 7)  # type: ignore[arg-type]
        with self.assertRaises(ValidationError):
            config_revision(b"x" * (MAX_PROFILE_BYTES + 1))
        self.assertNotEqual(
            workspace_identity(Path("/work/acme"), "acme"),
            workspace_identity(Path("/work/acme-moved"), "acme"),
        )
        self.assertNotEqual(
            workspace_identity(Path("/work/acme"), "acme"),
            workspace_identity(Path("/work/acme"), "acme-renamed"),
        )
        self.assertNotEqual(config_revision(b"a\n"), config_revision(b"a\r\n"))
        self.assertRegex(
            config_revision(b"x" * MAX_PROFILE_BYTES), r"^sha256:[0-9a-f]{64}$"
        )

    def test_workspace_identity_rejects_an_unresolved_symlink_alias(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "canonical"
            target.mkdir()
            alias = root / "alias"
            alias.symlink_to(target, target_is_directory=True)
            with self.assertRaises(ValidationError):
                workspace_identity(alias, "acme")


if __name__ == "__main__":
    unittest.main()

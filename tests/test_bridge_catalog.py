from __future__ import annotations

from dataclasses import replace
import ast
import importlib
import inspect
import json
from pathlib import Path
import tomllib
import unittest

from dyro.bridge import catalog as catalog_module
from dyro.bridge import constants as constants_module
from dyro.bridge import git_read as git_read_module
from dyro.bridge import models as models_module
from dyro.bridge import observations as observations_module
from dyro.bridge import plans as plans_module
from dyro.bridge import schemas as schemas_module
from dyro.bridge.catalog import (
    MANDATORY_OPERATION_IDS,
    PHASE0_DECLARED_OPERATION_IDS,
    ExposureCatalog,
    capabilities_digest,
    compact_capabilities,
    get_operation,
    list_operations,
)
from dyro.bridge.models import AvailabilityState, PlatformAvailability, PlatformState
from dyro.bridge.schemas import get_operation_schema, operation_schema_digest
from dyro.bridge.schemas import OperationSchema
from dyro.canonical import canonical_json_text
from dyro.errors import ValidationError


class BridgeCatalogTests(unittest.TestCase):
    @staticmethod
    def _vectors() -> dict[str, object]:
        return json.loads(
            Path("tests/fixtures/bridge/contracts-v1.json").read_text(encoding="utf-8")
        )

    def test_bridge_package_is_declared_for_built_artifacts(self) -> None:
        project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
        self.assertIn("dyro.bridge", project["tool"]["setuptools"]["packages"])

    def test_catalog_tracks_s3_internal_implementations_and_remains_deny_by_default(
        self,
    ) -> None:
        operations = list_operations()
        self.assertEqual(
            tuple(item.operation_id for item in operations),
            PHASE0_DECLARED_OPERATION_IDS,
        )
        self.assertEqual(
            MANDATORY_OPERATION_IDS,
            (
                "bridge.capabilities.compact",
                "bridge.hello",
                "bridge.operation.schema",
                "objective.plan",
                "workspace.list",
                "workspace.observe",
                "workspace.resolve",
            ),
        )
        self.assertEqual(len(operations), 18)
        self.assertTrue(
            all(
                item.must_be_available == (item.operation_id in MANDATORY_OPERATION_IDS)
                for item in operations
            )
        )
        implemented = {
            item.operation_id
            for item in operations
            if item.availability_state is AvailabilityState.IMPLEMENTED_TESTABLE
        }
        self.assertEqual(
            implemented,
            {
                "bridge.capabilities.compact",
                "bridge.hello",
                "bridge.operation.schema",
                "objective.attention",
                "objective.explain",
                "objective.graph",
                "objective.plan",
                "objective.tick",
                "task.gate_definitions.get",
                "workspace.list",
                "workspace.observe",
                "workspace.resolve",
            },
        )
        self.assertTrue(
            all(
                item.availability_state is AvailabilityState.DECLARED
                for item in operations
                if item.operation_id not in implemented
            )
        )
        self.assertFalse(any(item.public_available for item in operations))
        self.assertEqual(
            get_operation("task.explain").availability_state, AvailabilityState.DECLARED
        )
        self.assertFalse(get_operation("task.explain").must_be_available)

    def test_compact_capabilities_are_sorted_minimal_and_digest_stable(self) -> None:
        first = compact_capabilities("linux-ubuntu-24.04")
        second = compact_capabilities("linux-ubuntu-24.04")
        self.assertEqual(first, second)
        self.assertEqual(
            [item["operation"] for item in first],
            sorted(PHASE0_DECLARED_OPERATION_IDS),
        )
        for item in first:
            self.assertEqual(
                set(item),
                {
                    "operation",
                    "kind",
                    "maximum_risk",
                    "available",
                    "operation_schema_version",
                    "planner_revision",
                },
            )
            self.assertFalse(item["available"])
        digest = capabilities_digest("linux-ubuntu-24.04")
        self.assertEqual(digest, self._vectors()["capabilities"]["digest"])
        self.assertEqual(digest, capabilities_digest("linux-ubuntu-24.04"))
        self.assertEqual(digest, capabilities_digest("windows"))

    def test_every_catalog_operation_has_strict_fresh_schemas(self) -> None:
        for operation in list_operations():
            bundle = get_operation_schema(operation.operation_id)
            self.assertEqual(bundle.operation_id, operation.operation_id)
            self.assertEqual(bundle.schema_version, operation.schema_version)
            request = bundle.input_schema()
            response = bundle.output_schema()
            for document in (request, response):
                self.assertEqual(
                    document["$schema"], "https://json-schema.org/draft/2020-12/schema"
                )
                self.assertEqual(document["type"], "object")
                self.assertIs(document["additionalProperties"], False)
            request["type"] = "array"
            self.assertEqual(bundle.input_schema()["type"], "object")
            nested = bundle.input_schema()["properties"]
            nested["mutated"] = {"type": "string"}
            self.assertNotIn("mutated", bundle.input_schema()["properties"])
            self.assertEqual(
                operation_schema_digest(operation.operation_id),
                self._vectors()["schema_digests"][operation.operation_id],
            )

    def test_schema_instances_validate_meta_schema_and_digest_their_own_content(
        self,
    ) -> None:
        original = get_operation_schema("bridge.hello")
        changed_input = original.input_schema()
        changed_input["properties"]["future"] = {"type": "string"}
        changed = OperationSchema(
            operation_id=original.operation_id,
            schema_version=original.schema_version,
            input_schema_id=original.input_schema_id,
            output_schema_id=original.output_schema_id,
            _input_json=canonical_json_text(changed_input),
            _output_json=canonical_json_text(original.output_schema()),
        )
        self.assertNotEqual(
            changed.public_dict()["schema_digest"],
            original.public_dict()["schema_digest"],
        )

        invalid_input = original.input_schema()
        invalid_input["properties"] = []
        with self.assertRaises(ValidationError):
            OperationSchema(
                operation_id=original.operation_id,
                schema_version=original.schema_version,
                input_schema_id=original.input_schema_id,
                output_schema_id=original.output_schema_id,
                _input_json=canonical_json_text(invalid_input),
                _output_json=canonical_json_text(original.output_schema()),
            )

    def test_unknown_operation_and_duplicate_catalog_fail_closed(self) -> None:
        with self.assertRaises(ValidationError):
            get_operation("task.gates.run")
        duplicate = list_operations()[0]
        with self.assertRaises(ValidationError):
            ExposureCatalog(
                (duplicate, duplicate), mandatory_ids=(duplicate.operation_id,)
            )
        with self.assertRaises(ValidationError):
            ExposureCatalog((duplicate,), mandatory_ids=(duplicate.operation_id,) * 2)

    def test_release_validation_cannot_pass_with_declared_or_missing_operations(
        self,
    ) -> None:
        catalog = ExposureCatalog(
            list_operations(), mandatory_ids=MANDATORY_OPERATION_IDS
        )
        with self.assertRaises(ValidationError):
            catalog.validate_release(("linux-ubuntu-24.04", "macos-15"))
        with self.assertRaises(ValidationError):
            ExposureCatalog(
                list_operations()[:-1], mandatory_ids=MANDATORY_OPERATION_IDS
            )

    def test_release_fixture_requires_every_mandatory_public_operation(self) -> None:
        ready = tuple(
            replace(
                operation,
                availability_state=AvailabilityState.PUBLIC_AVAILABLE,
                service_id=f"dyro.bridge.services.{operation.operation_id.replace('.', '_')}",
                platforms=(
                    PlatformAvailability("linux-ubuntu-24.04", PlatformState.AVAILABLE),
                    PlatformAvailability("macos-15", PlatformState.AVAILABLE),
                    PlatformAvailability("windows", PlatformState.UNAVAILABLE),
                ),
            )
            for operation in list_operations()
        )
        release_catalog = ExposureCatalog(ready, mandatory_ids=MANDATORY_OPERATION_IDS)
        release_catalog.validate_release(("linux-ubuntu-24.04", "macos-15"))
        self.assertNotEqual(
            release_catalog.capabilities_digest("linux-ubuntu-24.04"),
            release_catalog.capabilities_digest("windows"),
        )
        with self.assertRaises(ValidationError):
            release_catalog.validate_release(["linux-ubuntu-24.04"])  # type: ignore[arg-type]

    def test_selector_and_unavailable_workspace_schemas_fail_closed(self) -> None:
        resolve_input = get_operation_schema("workspace.resolve").input_schema()
        workspace = resolve_input["properties"]["workspace"]
        self.assertEqual(workspace["type"], ["string", "null"])
        self.assertEqual(workspace["minLength"], 1)
        self.assertEqual(workspace["pattern"], r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}$")
        self.assertEqual(resolve_input["properties"]["start"]["minLength"], 1)
        self.assertEqual(resolve_input["properties"]["start"]["pattern"], r"^(?!~).+$")

        objective = get_operation_schema("objective.plan").input_schema()["properties"]
        self.assertEqual(
            objective["objective_id"]["pattern"],
            r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}$",
        )

        records = get_operation_schema("workspace.list").output_schema()["properties"][
            "workspaces"
        ]["items"]
        self.assertIn("oneOf", records)
        unavailable = records["oneOf"][1]
        self.assertNotIn("workspace", unavailable["properties"])
        self.assertEqual(unavailable["properties"]["health"], {"const": "unavailable"})

    def test_plan_operations_have_complete_operation_specific_envelopes(self) -> None:
        expected_projection_fields = {
            "objective.plan": {
                "completion",
                "selected_actions",
                "blocked",
                "attention",
            },
            "objective.explain": {
                "summary_code",
                "reasons",
                "selected_actions",
                "blocked",
                "attention",
            },
            "objective.graph": {"nodes", "edges", "issues"},
            "objective.tick": {
                "selected_actions",
                "blocked",
                "attention",
                "tick_wave",
                "deferred",
                "non_mutating_actions",
            },
            "objective.attention": {"attention", "next_wake_at"},
        }
        for operation, projection_fields in expected_projection_fields.items():
            with self.subTest(operation=operation):
                output = get_operation_schema(operation).output_schema()
                required = set(output["required"])
                self.assertTrue(
                    {
                        "workspace",
                        "protocol_major",
                        "normalized_input",
                        "read_set",
                        "projection",
                        "expires_at",
                        "plan_sha256",
                    }.issubset(required)
                )
                projection = output["properties"]["projection"]
                self.assertEqual(projection["type"], "object")
                self.assertIs(projection["additionalProperties"], False)
                self.assertEqual(set(projection["properties"]), projection_fields)

        task_edges = get_operation_schema("task.graph").output_schema()["properties"][
            "edges"
        ]
        self.assertEqual(task_edges["maxItems"], 100)

    def test_bridge_core_modules_do_not_import_cli(self) -> None:
        for module in (
            models_module,
            schemas_module,
            catalog_module,
            constants_module,
            git_read_module,
            observations_module,
            plans_module,
        ):
            tree = ast.parse(inspect.getsource(module))
            imports = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.extend(item.name for item in node.names)
                elif isinstance(node, ast.ImportFrom):
                    imports.append(node.module or "")
            self.assertFalse(
                any(name == "dyro.cli" or name.endswith(".cli") for name in imports),
                f"{module.__name__} imports CLI: {imports}",
            )

    def test_every_implemented_service_id_resolves_to_a_callable(self) -> None:
        implemented = (
            item
            for item in list_operations()
            if item.availability_state is AvailabilityState.IMPLEMENTED_TESTABLE
        )
        for operation in implemented:
            assert operation.service_id is not None
            module_name, attribute = operation.service_id.rsplit(".", 1)
            service = getattr(importlib.import_module(module_name), attribute)
            self.assertTrue(callable(service), operation.operation_id)


if __name__ == "__main__":
    unittest.main()

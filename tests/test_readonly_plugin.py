from __future__ import annotations

import ast
from importlib import metadata
from importlib.resources import files
import json
from pathlib import Path
import unittest

from dyro.bridge import mcp


ROOT = Path(__file__).parents[1]
PLUGIN = ROOT / "src" / "dyro" / "integrations" / "assets" / "dyro-readonly"


class ReadonlyPluginTests(unittest.TestCase):
    def test_distribution_declares_mcp_entrypoint_extra_and_package_data(self) -> None:
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")

        self.assertIn('dyro-mcp = "dyro.bridge.mcp:main"', pyproject)
        self.assertIn('mcp = ["mcp>=2,<3"]', pyproject)
        for relative in (
            "assets/dyro-readonly/.codex-plugin/plugin.json",
            "assets/dyro-readonly/.mcp.json",
            "assets/dyro-readonly/compatibility.json",
        ):
            self.assertIn(f'"{relative}"', pyproject)
        self.assertIn("recursive-include src/dyro/integrations/assets *", manifest)

    def test_runtime_resource_api_can_read_all_plugin_assets(self) -> None:
        root = files("dyro.integrations").joinpath("assets", "dyro-readonly")
        resources = (
            root.joinpath(".codex-plugin", "plugin.json"),
            root.joinpath(".mcp.json"),
            root.joinpath("compatibility.json"),
        )
        for resource in resources:
            self.assertTrue(resource.is_file(), str(resource))
            self.assertIsInstance(
                json.loads(resource.read_text(encoding="utf-8")), dict
            )

    def test_installed_metadata_exposes_exact_mcp_console_script(self) -> None:
        entry_points = metadata.entry_points(group="console_scripts")
        matches = [entry for entry in entry_points if entry.name == "dyro-mcp"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].value, "dyro.bridge.mcp:main")

    def test_plugin_manifest_is_minimal_versioned_beta_artifact(self) -> None:
        manifest = json.loads(
            (PLUGIN / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["name"], "dyro-readonly")
        self.assertEqual(manifest["version"], mcp.INTEGRATION_VERSION)
        self.assertEqual(manifest["mcpServers"], "./.mcp.json")
        self.assertNotIn("skills", manifest)
        self.assertNotIn("apps", manifest)
        wording = json.dumps(manifest, ensure_ascii=False).lower()
        self.assertIn("beta", wording)
        self.assertIn("available for validation", wording)
        self.assertNotIn("codex supported", wording)
        self.assertNotIn("todo", wording)

    def test_mcp_manifest_uses_only_installed_console_executable(self) -> None:
        manifest = json.loads((PLUGIN / ".mcp.json").read_text(encoding="utf-8"))
        self.assertEqual(set(manifest), {"mcpServers"})
        self.assertEqual(set(manifest["mcpServers"]), {"dyro-readonly"})
        server = manifest["mcpServers"]["dyro-readonly"]
        self.assertEqual(server["command"], "dyro-mcp")
        self.assertEqual(server["args"], [])
        serialized = json.dumps(server, ensure_ascii=False).lower()
        self.assertNotIn("python", serialized)
        self.assertIn("inspect-and-plan", serialized)
        self.assertIn("non-executable", serialized)
        self.assertIn("no authorization", serialized)

    def test_compatibility_manifest_matches_runtime_handshake(self) -> None:
        manifest = json.loads(
            (PLUGIN / "compatibility.json").read_text(encoding="utf-8")
        )
        policy = mcp.DEFAULT_COMPATIBILITY.public_dict()
        self.assertEqual(manifest["integration"]["version"], mcp.INTEGRATION_VERSION)
        self.assertEqual(manifest["core"], policy["core"])
        self.assertEqual(manifest["bridge"], policy["bridge"])
        self.assertEqual(manifest["protocol"], policy["protocol"])
        self.assertEqual(manifest["operation_schema"], policy["operation_schema"])
        self.assertEqual(manifest["planner"], policy["planner"])
        self.assertEqual(tuple(manifest["tools"]["names"]), mcp.TOOL_NAMES)
        self.assertEqual(manifest["tools"]["digest"], mcp.TOOL_LIST_DIGEST)

    def test_tool_names_and_descriptions_contain_no_mutation_surface(self) -> None:
        forbidden = {
            "invoke",
            "shell",
            "apply",
            "execute",
            "dispatch",
            "gate",
            "review",
            "signoff",
            "merge",
            "push",
            "release",
            "publish",
            "cleanup",
        }
        self.assertEqual(
            mcp.TOOL_NAMES,
            (
                "dyro_hello",
                "dyro_capabilities",
                "dyro_operation_schema",
                "dyro_workspace_resolve",
                "dyro_workspace_list",
                "dyro_workspace_observe",
                "dyro_objective_plan",
            ),
        )
        for name in mcp.TOOL_NAMES:
            description = getattr(mcp.ReadonlyBridge, name).__doc__ or ""
            words = {word.strip(".,-_").lower() for word in description.split()}
            self.assertTrue(forbidden.isdisjoint(words), (name, words & forbidden))

    def test_adapter_has_no_direct_mutation_subsystem_imports(self) -> None:
        source_path = ROOT / "src" / "dyro" / "bridge" / "mcp.py"
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        forbidden_fragments = {
            "cli",
            "continuation",
            "dispatch",
            "integrations.manager",
            "signing",
            "state",
            "tasks",
        }
        self.assertFalse(
            {
                module
                for module in imported
                if any(fragment in module for fragment in forbidden_fragments)
            }
        )

    def test_public_artifact_metadata_has_no_absolute_path_or_sensitive_surface(
        self,
    ) -> None:
        text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (
                PLUGIN / ".codex-plugin" / "plugin.json",
                PLUGIN / ".mcp.json",
                PLUGIN / "compatibility.json",
            )
        ).lower()
        for marker in ("/users/", "/home/", "traceback", "credential", "api_key"):
            self.assertNotIn(marker, text)


if __name__ == "__main__":
    unittest.main()

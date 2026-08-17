from __future__ import annotations

from pathlib import Path
import ast
import unittest

from dyro.bridge.models import Availability, CatalogRecord, ProtocolVersion, Risk


ROOT = Path(__file__).resolve().parents[1]


class BridgeModelTests(unittest.TestCase):
    def test_protocol_and_catalog_record_are_frozen(self) -> None:
        version = ProtocolVersion(1, 0)
        record = CatalogRecord(
            id="bridge.hello",
            risk=Risk.R0,
            availability=Availability.DECLARED,
            schema_version=1,
            must_be_available=True,
            core_service="dyro.bridge.transport.hello",
        )
        self.assertEqual(version.major, 1)
        self.assertEqual(record.risk, Risk.R0)
        with self.assertRaises(TypeError):
            CatalogRecord(
                id="bridge.hello",
                risk=Risk.R0,
                availability=Availability.DECLARED,
                schema_version=1,
                must_be_available=True,
                core_service="dyro.cli.main",
            )

    def test_bridge_modules_do_not_import_cli(self) -> None:
        root = ROOT / "src" / "dyro" / "bridge"
        for path in sorted(root.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertFalse(
                            alias.name == "dyro.cli" or alias.name.startswith("dyro.cli."),
                            path.name,
                        )
                if isinstance(node, ast.ImportFrom) and node.module:
                    self.assertFalse(
                        node.module == "dyro.cli" or node.module.startswith("dyro.cli."),
                        path.name,
                    )
                    if node.level == 2 and node.module == "cli":
                        self.fail(f"{path.name} imports sibling cli")

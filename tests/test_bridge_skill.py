from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import unittest

from dyro.bridge.catalog import compact_catalog, build_default_catalog
from dyro.bridge.schemas import operation_schema
from dyro.integrations.manager import DISPATCH_SKILL_NAME, SKILL_NAME

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "src" / "dyro" / "bridge" / "skill" / "SKILL.md"
MANIFEST = ROOT / "src" / "dyro" / "bridge" / "skill" / "manifest.json"
PYPROJECT = ROOT / "pyproject.toml"

_POSITIVE = ("inspect", "plan", "workspace", "objective")
_NEGATIVE = (
    "objective apply",
    "task run",
    "task gates",
    "dyro dispatch",
    "dyro console",
)


class BridgeSkillTests(unittest.TestCase):
    def test_skill_is_source_only_and_not_an_integration_asset(self) -> None:
        metadata = PYPROJECT.read_text(encoding="utf-8")
        self.assertTrue(SKILL.is_file())
        self.assertFalse(json.loads(MANIFEST.read_text(encoding="utf-8"))["installable"])
        self.assertNotIn("dyro-agent-bridge", metadata)
        self.assertNotIn("bridge/skill", metadata)
        self.assertEqual(SKILL_NAME, "dyro-control-plane")
        self.assertEqual(DISPATCH_SKILL_NAME, "dyro-dispatch")

    def test_skill_triggers_and_budget(self) -> None:
        text = SKILL.read_text(encoding="utf-8")
        lowered = text.lower()
        for needle in _POSITIVE:
            self.assertIn(needle, lowered)
        for needle in _NEGATIVE:
            self.assertIn(needle, lowered)
        self.assertIn("bridge.capabilities.compact", text)
        self.assertIn("bridge.operation.schema", text)
        self.assertIn("python -m dyro.bridge", text)
        self.assertNotIn("run dyro-bridge", lowered)
        compact = json.dumps(compact_catalog(build_default_catalog(platform="linux")))
        schema = json.dumps(operation_schema("workspace.resolve", platform="linux"))
        total = len(text.encode("utf-8")) + len(compact.encode("utf-8")) + len(schema.encode("utf-8"))
        self.assertLessEqual(len(text.encode("utf-8")), 8192)
        self.assertLessEqual(total, 32768)

    def test_module_entry_is_public_and_host_gated(self) -> None:
        request = json.dumps(
            {
                "protocol": {"major": 1, "minor": 0},
                "client": {"name": "bridge-skill-test", "version": "0.0.1"},
                "operation": "bridge.hello",
                "input": {},
            },
            separators=(",", ":"),
        ).encode("utf-8")
        completed = subprocess.run(
            (sys.executable, "-m", "dyro.bridge"),
            input=request,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertTrue(completed.stdout.endswith(b"\n"))
        self.assertEqual(completed.stderr, b"")
        payload = json.loads(completed.stdout.decode("utf-8"))
        if sys.platform.startswith("linux"):
            self.assertEqual(completed.returncode, 0)
            self.assertTrue(payload["ok"])
        else:
            self.assertEqual(completed.returncode, 4)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["error"]["code"], "OPERATION_UNAVAILABLE")

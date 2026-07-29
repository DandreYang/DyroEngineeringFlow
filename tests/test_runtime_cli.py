"""CLI smoke for ``dyro runtime`` / external semantic runtime status."""

from __future__ import annotations

import json
import unittest
from io import StringIO
from unittest import mock

from experiments.external_workflow_runner.cli import main as runtime_main


class RuntimeCliTests(unittest.TestCase):
    def test_production_gate_is_not_ready(self) -> None:
        buf = StringIO()
        with mock.patch("sys.stdout", buf):
            code = runtime_main(["production-gate"])
        self.assertEqual(code, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload.get("verdict"), "NOT_READY")
        self.assertFalse(payload.get("production_ready"))

    def test_status_includes_entry_points(self) -> None:
        buf = StringIO()
        with mock.patch("sys.stdout", buf):
            code = runtime_main(["status"])
        self.assertEqual(code, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload.get("kind"), "external-semantic-runtime-status")
        self.assertTrue(payload.get("shipped_with_dyro_wheel"))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(ROOT / "tools"))

from verify_release_gates import main, missing_gates  # noqa: E402


class ReleaseGateTests(unittest.TestCase):
    def test_current_tree_has_1_0_gate_markers_smoke_only(self) -> None:
        """Substring markers exist. This is not a 1.0 release pass."""
        self.assertEqual(missing_gates(ROOT), [])

    def test_physics_train_refuses_0_6_publish_tag(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            main(["--root", str(ROOT), "--release-tag", "v0.6.0"])
        self.assertIn("0.6", str(raised.exception))

    def test_physics_train_refuses_published_0_6_9_tag(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            main(["--root", str(ROOT), "--release-tag", "v0.6.9"])
        self.assertIn("0.6", str(raised.exception))

    def test_0_7_release_runs_gates_without_claiming_1_0(self) -> None:
        stdout = StringIO()
        with redirect_stdout(stdout):
            code = main(["--root", str(ROOT), "--release-tag", "v0.7.0"])
        self.assertEqual(code, 0)
        self.assertIn("0.7 gates present", stdout.getvalue())
        self.assertNotIn("1.0 gates present", stdout.getvalue())
        self.assertNotIn("skip 1.0 gates", stdout.getvalue())

    def test_untagged_0_7_runs_0_7_gates(self) -> None:
        stdout = StringIO()
        with redirect_stdout(stdout):
            code = main(["--root", str(ROOT)])
        self.assertEqual(code, 0)
        self.assertIn("0.7 gates present", stdout.getvalue())
        self.assertNotIn("1.0 gates present", stdout.getvalue())

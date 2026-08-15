from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(ROOT / "tools"))

from verify_release_gates import main, missing_gates  # noqa: E402


class ReleaseGateTests(unittest.TestCase):
    def test_current_tree_has_1_0_evidence(self) -> None:
        self.assertEqual(missing_gates(ROOT), [])

    def test_physics_train_refuses_0_6_publish_tag(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            main(["--root", str(ROOT), "--release-tag", "v0.6.0"])
        self.assertIn("0.6", str(raised.exception))

from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

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
            code = main(["--root", str(ROOT), "--release-tag", "v0.7.5"])
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

    def test_later_0_7_x_runs_0_7_gates_without_claiming_1_0(self) -> None:
        stdout = StringIO()
        with patch("verify_release_gates._version", return_value="0.7.99"):
            with redirect_stdout(stdout):
                code = main(["--root", str(ROOT), "--release-tag", "v0.7.99"])
        self.assertEqual(code, 0)
        self.assertIn("0.7 gates present", stdout.getvalue())
        self.assertNotIn("1.0 gates present", stdout.getvalue())
        self.assertNotIn("1.0 identity gates present", stdout.getvalue())
        self.assertNotIn("skip 1.0 gates", stdout.getvalue())
        self.assertNotIn("skip physics gates", stdout.getvalue())

    def test_commented_python_marker_does_not_count(self) -> None:
        from verify_release_gates import _contains_marker

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "probe.py"
            path.write_text("# def verify_bundle\nvalue = 1\n", encoding="utf-8")
            self.assertFalse(_contains_marker(path, "def verify_bundle"))
            path.write_text("def verify_bundle():\n    return None\n", encoding="utf-8")
            self.assertTrue(_contains_marker(path, "def verify_bundle"))

    def test_0_8_and_0_9_are_refused_as_wrong_feature_numbers(self) -> None:
        for version in ("0.8.0", "0.9.1"):
            with self.subTest(version=version):
                with patch("verify_release_gates._version", return_value=version):
                    with self.assertRaises(SystemExit) as raised:
                        main(["--root", str(ROOT), "--release-tag", f"v{version}"])
                self.assertIn("0.7.x", str(raised.exception))

    def test_1_0_0_keeps_the_stricter_0_7_x_contract(self) -> None:
        stdout = StringIO()
        with patch("verify_release_gates._version", return_value="1.0.0"):
            with redirect_stdout(stdout):
                code = main(["--root", str(ROOT), "--release-tag", "v1.0.0"])
        self.assertEqual(code, 0)
        self.assertIn("1.0 identity gates present", stdout.getvalue())
        self.assertNotIn("skip 1.0 gates", stdout.getvalue())
        self.assertNotIn("skip physics gates", stdout.getvalue())

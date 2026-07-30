"""Regression tests for read-only external-runtime diagnostics."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from experiments.external_workflow_runner.doctor import (
    collect_runtime_diagnostics,
)


class RuntimeDoctorTests(unittest.TestCase):
    def test_missing_docker_blocks_local_poc(self) -> None:
        with mock.patch(
            "experiments.external_workflow_runner.doctor.shutil.which",
            return_value=None,
        ):
            report = collect_runtime_diagnostics()
        self.assertEqual(report["verdict"], "BLOCKED")
        self.assertFalse(report["ready_for_local_poc"])
        checks = {item["id"]: item for item in report["checks"]}
        self.assertEqual(checks["LOCAL-02"]["status"], "fail")
        self.assertEqual(checks["LOCAL-03"]["status"], "blocked")
        self.assertFalse(report["production_ready"])

    def test_daemon_failure_does_not_probe_image(self) -> None:
        with (
            mock.patch(
                "experiments.external_workflow_runner.doctor.shutil.which",
                return_value="/usr/local/bin/docker",
            ),
            mock.patch(
                "experiments.external_workflow_runner.doctor._probe",
                return_value=(False, "daemon unavailable"),
            ) as probe,
        ):
            report = collect_runtime_diagnostics()
        self.assertFalse(report["ready_for_local_poc"])
        self.assertEqual(probe.call_count, 1)
        checks = {item["id"]: item for item in report["checks"]}
        self.assertEqual(checks["LOCAL-03"]["status"], "fail")
        self.assertEqual(checks["LOCAL-04"]["status"], "blocked")

    def test_local_substrate_can_pass_without_real_provider(self) -> None:
        with (
            mock.patch(
                "experiments.external_workflow_runner.doctor.shutil.which",
                return_value="/usr/local/bin/docker",
            ),
            mock.patch(
                "experiments.external_workflow_runner.doctor._probe",
                side_effect=((True, "27.0.0"), (True, "sha256:runtime")),
            ),
        ):
            report = collect_runtime_diagnostics()
        self.assertEqual(report["verdict"], "PASS")
        self.assertTrue(report["ready_for_local_poc"])
        checks = {item["id"]: item for item in report["checks"]}
        self.assertEqual(checks["LOCAL-05"]["status"], "not_configured")
        self.assertFalse(checks["LOCAL-05"]["blocks_local"])

    def test_provider_path_requires_explicit_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            provider = Path(temporary) / "provider"
            provider.write_text("fixture", encoding="utf-8")
            with (
                mock.patch(
                    "experiments.external_workflow_runner.doctor.shutil.which",
                    return_value="/usr/local/bin/docker",
                ),
                mock.patch(
                    "experiments.external_workflow_runner.doctor._probe",
                    side_effect=((True, "27.0.0"), (True, "sha256:runtime")),
                ),
            ):
                report = collect_runtime_diagnostics(
                    provider_path=provider,
                )
        self.assertFalse(report["ready_for_local_poc"])
        checks = {item["id"]: item for item in report["checks"]}
        self.assertEqual(checks["LOCAL-05"]["status"], "fail")
        self.assertIn("允许根目录", checks["LOCAL-05"]["detail"])

    def test_provider_pin_passes_under_allowed_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provider = root / "provider"
            provider.write_text("fixture", encoding="utf-8")
            with (
                mock.patch(
                    "experiments.external_workflow_runner.doctor.shutil.which",
                    return_value="/usr/local/bin/docker",
                ),
                mock.patch(
                    "experiments.external_workflow_runner.doctor._probe",
                    side_effect=((True, "27.0.0"), (True, "sha256:runtime")),
                ),
            ):
                report = collect_runtime_diagnostics(
                    provider_path=provider,
                    provider_roots=(root,),
                )
        self.assertTrue(report["ready_for_local_poc"])
        checks = {item["id"]: item for item in report["checks"]}
        self.assertEqual(checks["LOCAL-05"]["status"], "pass")


if __name__ == "__main__":
    unittest.main()

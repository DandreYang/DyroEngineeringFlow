"""CLI smoke for ``dyro runtime`` / external semantic runtime status."""

from __future__ import annotations

import json
from pathlib import Path
import unittest
from io import StringIO
from unittest import mock

from experiments.external_workflow_runner.cli import main as runtime_main


class RuntimeCliTests(unittest.TestCase):
    def test_production_gate_is_not_ready(self) -> None:
        buf = StringIO()
        with mock.patch("sys.stdout", buf):
            code = runtime_main(["production-gate"])
        self.assertEqual(code, 3)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload.get("verdict"), "NOT_READY")
        self.assertFalse(payload.get("production_ready"))
        self.assertEqual(payload.get("exit_code"), 3)
        self.assertEqual(
            payload.get("next_command"),
            "dyro runtime production-acceptance schemas --human",
        )
        self.assertIsInstance(payload.get("checked_at"), str)
        self.assertTrue(payload.get("release_approval_required"))
        self.assertFalse(payload["production_acceptance"]["provided"])
        for schema_path in payload["production_acceptance"]["schemas"].values():
            self.assertTrue(Path(schema_path).is_file())

    def test_production_gate_human_output_is_actionable(self) -> None:
        buf = StringIO()
        with mock.patch("sys.stdout", buf):
            code = runtime_main(["production-gate", "--human"])
        self.assertEqual(code, 3)
        output = buf.getvalue()
        self.assertIn("生产门禁：未通过", output)
        self.assertIn("PROD-01", output)
        self.assertIn(
            "下一步：dyro runtime production-acceptance schemas --human",
            output,
        )

    def test_status_includes_entry_points(self) -> None:
        buf = StringIO()
        with mock.patch("sys.stdout", buf):
            code = runtime_main(["status"])
        self.assertEqual(code, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload.get("kind"), "external-semantic-runtime-status")
        self.assertTrue(payload.get("shipped_with_dyro_wheel"))
        self.assertEqual(payload.get("next_command"), "dyro runtime doctor")
        self.assertTrue(payload["capabilities"]["stage5_supervisor_api"])
        self.assertFalse(payload["capabilities"]["operator_run_cli"])
        self.assertTrue(
            payload["capabilities"]["signed_production_acceptance"]
        )
        self.assertTrue(
            payload["capabilities"]["production_acceptance_operator_kit"]
        )

    def test_status_human_output_explains_safety_boundary(self) -> None:
        buf = StringIO()
        with mock.patch("sys.stdout", buf):
            code = runtime_main(["status", "--human"])
        self.assertEqual(code, 0)
        output = buf.getvalue()
        self.assertIn("外部语义运行时", output)
        self.assertIn("生产状态：未就绪", output)
        self.assertIn("不会执行 review、signoff、merge 或 push", output)

    def test_status_human_ready_gate_still_requires_release_approval(
        self,
    ) -> None:
        ready = {
            "production_ready": True,
            "verdict": "READY",
            "blocker_count": 0,
        }
        buf = StringIO()
        with (
            mock.patch("sys.stdout", buf),
            mock.patch(
                "experiments.external_workflow_runner.stage5.production_gate."
                "evaluate_production_readiness",
                return_value=ready,
            ),
        ):
            code = runtime_main(["status", "--human"])
        self.assertEqual(code, 0)
        self.assertIn(
            "门禁已通过（仍需独立发布批准）",
            buf.getvalue(),
        )

    def test_doctor_returns_blocked_exit_code(self) -> None:
        report = {
            "schema_version": 1,
            "kind": "external-semantic-runtime-doctor",
            "mode": "local",
            "verdict": "BLOCKED",
            "ready_for_local_poc": False,
            "production_ready": False,
            "blocking_count": 1,
            "checks": [
                {
                    "id": "LOCAL-02",
                    "label": "Docker daemon",
                    "status": "fail",
                    "detail": "Docker daemon is not reachable",
                    "remediation": "Start Docker and retry.",
                    "blocks_local": True,
                }
            ],
            "next_steps": ["Start Docker and retry."],
        }
        buf = StringIO()
        with (
            mock.patch("sys.stdout", buf),
            mock.patch(
                "experiments.external_workflow_runner.doctor.collect_runtime_diagnostics",
                return_value=report,
            ),
        ):
            code = runtime_main(["doctor", "--json"])
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(buf.getvalue())["verdict"], "BLOCKED")

    def test_doctor_human_output_does_not_claim_production_ready(self) -> None:
        report = {
            "schema_version": 1,
            "kind": "external-semantic-runtime-doctor",
            "mode": "local",
            "verdict": "PASS",
            "ready_for_local_poc": True,
            "production_ready": False,
            "blocking_count": 0,
            "checks": [
                {
                    "id": "LOCAL-01",
                    "label": "Runtime identity",
                    "status": "pass",
                    "detail": "Runtime lock matches source.",
                    "remediation": "",
                    "blocks_local": True,
                }
            ],
            "next_steps": ["Run dyro runtime production-gate."],
        }
        buf = StringIO()
        with (
            mock.patch("sys.stdout", buf),
            mock.patch(
                "experiments.external_workflow_runner.doctor.collect_runtime_diagnostics",
                return_value=report,
            ),
        ):
            code = runtime_main(["doctor", "--human"])
        self.assertEqual(code, 0)
        output = buf.getvalue()
        self.assertIn("本地 PoC 环境：可用", output)
        self.assertIn("生产就绪：否", output)

    def test_plan_is_read_only_and_lists_core_handoff(self) -> None:
        buf = StringIO()
        with mock.patch("sys.stdout", buf):
            code = runtime_main(["plan"])
        self.assertEqual(code, 0)
        payload = json.loads(buf.getvalue())
        self.assertFalse(payload["mutates_state"])
        self.assertEqual(payload["target"], "production")
        phase_ids = [phase["id"] for phase in payload["phases"]]
        self.assertIn("core-evidence-handoff", phase_ids)
        self.assertIn("environment-acceptance", phase_ids)

    def test_claim_prepare_forwards_dry_run_without_writing(self) -> None:
        report = {
            "kind": "external-runtime-claim-preparation",
            "verdict": "DRY_RUN",
            "task_id": "TASK-1",
            "runner_id": "runner-1",
            "control_claim_id": "claim-1",
            "control_generation": 1,
            "output": "/tmp/stage5-claim.json",
            "written": False,
        }
        buf = StringIO()
        with (
            mock.patch("sys.stdout", buf),
            mock.patch(
                "experiments.external_workflow_runner.stage5.core_handoff.prepare_stage5_claim",
                return_value=report,
            ) as prepare,
        ):
            code = runtime_main(
                [
                    "--dry-run",
                    "claim",
                    "prepare",
                    "--core-claim",
                    "/tmp/core-claim.json",
                    "--output",
                    "/tmp/stage5-claim.json",
                    "--json",
                ]
            )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(buf.getvalue())["verdict"], "DRY_RUN")
        self.assertTrue(prepare.call_args.kwargs["dry_run"])

    def test_handoff_never_claims_core_import(self) -> None:
        report = {
            "kind": "external-runtime-core-evidence-handoff",
            "verdict": "BUILT",
            "task_id": "TASK-1",
            "runtime_pack_sha256": "a" * 64,
            "core_bundle": "/tmp/core.zip",
            "core_import_attempted": False,
            "review_attempted": False,
            "signoff_attempted": False,
            "merge_attempted": False,
            "push_attempted": False,
            "next_command": (
                "dyro task evidence execution TASK-1 "
                "--bundle /tmp/core.zip"
            ),
        }
        buf = StringIO()
        with (
            mock.patch("sys.stdout", buf),
            mock.patch(
                "experiments.external_workflow_runner.stage5.core_handoff.build_core_evidence_handoff",
                return_value=report,
            ),
        ):
            code = runtime_main(
                [
                    "handoff",
                    "--root",
                    "/tmp/root",
                    "--task",
                    "TASK-1",
                    "--pack",
                    "/tmp/pack",
                    "--workspace",
                    "/tmp/workspace",
                    "--core-claim",
                    "/tmp/core-claim.json",
                    "--output",
                    "/tmp/core.zip",
                    "--signing-key",
                    "/secure/runner.pem",
                    "--key-id",
                    "runner-key",
                    "--json",
                ]
            )
        self.assertEqual(code, 0)
        payload = json.loads(buf.getvalue())
        self.assertFalse(payload["core_import_attempted"])
        self.assertFalse(payload["merge_attempted"])

    def test_handoff_forwards_dry_run_without_claiming_gate_success(
        self,
    ) -> None:
        report = {
            "kind": "external-runtime-core-evidence-handoff",
            "verdict": "DRY_RUN",
            "task_id": "TASK-1",
            "runtime_pack_sha256": "a" * 64,
            "core_bundle": "/tmp/core.zip",
            "core_bundle_written": False,
            "gates_executed": False,
            "gates_passed": None,
            "workspace_heads_verified": False,
            "signature_created": False,
            "ready_for_core_import": False,
            "core_import_attempted": False,
            "review_attempted": False,
            "signoff_attempted": False,
            "merge_attempted": False,
            "push_attempted": False,
            "next_command": "dyro runtime handoff --help",
            "remediation": "Repeat without --dry-run.",
        }
        buf = StringIO()
        with (
            mock.patch("sys.stdout", buf),
            mock.patch(
                "experiments.external_workflow_runner.stage5.core_handoff.build_core_evidence_handoff",
                return_value=report,
            ) as handoff,
        ):
            code = runtime_main(
                [
                    "--dry-run",
                    "handoff",
                    "--root",
                    "/tmp/root",
                    "--task",
                    "TASK-1",
                    "--pack",
                    "/tmp/pack",
                    "--workspace",
                    "/tmp/workspace",
                    "--core-claim",
                    "/tmp/core-claim.json",
                    "--output",
                    "/tmp/core.zip",
                    "--signing-key",
                    "/secure/runner.pem",
                    "--key-id",
                    "runner-key",
                    "--json",
                ]
            )
        self.assertEqual(code, 0)
        payload = json.loads(buf.getvalue())
        self.assertFalse(payload["gates_executed"])
        self.assertIsNone(payload["gates_passed"])
        self.assertTrue(handoff.call_args.kwargs["dry_run"])

    def test_handoff_gate_failure_is_a_blocking_result(self) -> None:
        report = {
            "kind": "external-runtime-core-evidence-handoff",
            "verdict": "BLOCKED",
            "task_id": "TASK-1",
            "runtime_pack_sha256": "a" * 64,
            "core_bundle": "/tmp/diagnostic.zip",
            "core_bundle_written": True,
            "gates_executed": True,
            "gates_passed": False,
            "workspace_heads_verified": True,
            "signature_created": True,
            "ready_for_core_import": False,
            "core_import_attempted": False,
            "review_attempted": False,
            "signoff_attempted": False,
            "merge_attempted": False,
            "push_attempted": False,
            "next_command": None,
            "remediation": "Fix the failing gate and rebuild.",
        }
        buf = StringIO()
        with (
            mock.patch("sys.stdout", buf),
            mock.patch(
                "experiments.external_workflow_runner.stage5.core_handoff.build_core_evidence_handoff",
                return_value=report,
            ),
        ):
            code = runtime_main(
                [
                    "handoff",
                    "--root",
                    "/tmp/root",
                    "--task",
                    "TASK-1",
                    "--pack",
                    "/tmp/pack",
                    "--workspace",
                    "/tmp/workspace",
                    "--core-claim",
                    "/tmp/core-claim.json",
                    "--output",
                    "/tmp/diagnostic.zip",
                    "--signing-key",
                    "/secure/runner.pem",
                    "--key-id",
                    "runner-key",
                    "--json",
                ]
            )
        self.assertEqual(code, 3)
        payload = json.loads(buf.getvalue())
        self.assertFalse(payload["ready_for_core_import"])
        self.assertIsNone(payload["next_command"])



if __name__ == "__main__":
    unittest.main()

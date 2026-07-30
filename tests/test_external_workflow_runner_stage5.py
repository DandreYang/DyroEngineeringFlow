from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

from experiments.external_workflow_runner.artifacts import ArtifactPolicy
from experiments.external_workflow_runner.errors import Stage0ValidationError
from experiments.external_workflow_runner.sandbox import BUN_IMAGE
from experiments.external_workflow_runner.stage1.canonical import CanonicalInput
from experiments.external_workflow_runner.stage1.claim import ClaimRecord, ClaimStore
from experiments.external_workflow_runner.stage3.claim_matrix import ClaimDeadlineMatrix
from experiments.external_workflow_runner.stage4.evidence_pack import (
    CleanupProof,
    EvidencePackResult,
    pack_run_evidence,
)
from experiments.external_workflow_runner.stage5.bundle import assemble_stage5_bundle
from experiments.external_workflow_runner.stage5.evidence_dry_run import (
    dry_run_validate_pack,
    refuse_production_actions,
)
from experiments.external_workflow_runner.stage5.host_provider import (
    pin_host_provider,
    write_host_fixture_cli,
)
from experiments.external_workflow_runner.stage5.production_gate import (
    assert_not_production_ready,
    evaluate_production_readiness,
)
from experiments.external_workflow_runner.stage5.supervisor import (
    EXECUTION_KEY_ENV,
    PROVIDER_TOKEN,
    RAW_MARKER,
    STDERR_MARKER,
    Stage5Supervisor,
    Stage5SupervisorConfig,
)


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_LOCK = ROOT / "experiments/external_workflow_runner/runtime-lock.json"
_DOCKER_TEMP_DIR = ROOT


def _docker_image_available() -> bool:
    import shutil
    import subprocess

    if shutil.which("docker") is None:
        return False
    return (
        subprocess.run(
            ["docker", "image", "inspect", BUN_IMAGE],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    )


def _build_unit_pack(
    root: Path,
    *,
    workflow_run_id: str,
) -> EvidencePackResult:
    artifact = root / "report.md"
    artifact.write_text("# Stage 5 report\n", encoding="utf-8")
    artifact_sha = hashlib.sha256(artifact.read_bytes()).hexdigest()
    return pack_run_evidence(
        pack_root=root / "pack",
        workflow_run_id=workflow_run_id,
        claim={
            "schema_version": 1,
            "task_id": "t",
            "runner_id": "r",
            "generation": 2,
            "execution_key_id": "k",
            "issued_at": 1.0,
            "expires_at": 2.0,
        },
        canonical_input_sha256="a" * 64,
        envelope={
            "status": "DONE",
            "workflow_run_id": workflow_run_id,
            "artifacts": [
                {
                    "repository": "repo",
                    "path": "report.md",
                    "sha256": artifact_sha,
                }
            ],
        },
        artifact_paths=(("repo", "report.md", artifact),),
        provider_pin={"source": "host", "content_sha256": "b" * 64},
        claim_matrix={"total_ms": 1},
        cleanup=CleanupProof(
            sandbox_cleanup_verified=True,
            broker_cleanup_verified=True,
            broker_containers_absent=True,
            raw_marker_leaked=False,
            provider_token_leaked=False,
        ),
        mid_run_renewals=1,
        provider_mode="argv-cli",
        telemetry_text='{"raw_destroyed":true}\n',
    )


class HostProviderUnitTests(unittest.TestCase):
    def test_pin_and_allowlist(self) -> None:
        with tempfile.TemporaryDirectory(dir=_DOCKER_TEMP_DIR, prefix=".s5u-") as tmp:
            root = Path(tmp)
            fixture = write_host_fixture_cli(root / "host_cli.ts")
            pin = pin_host_provider(fixture, allowed_roots=(root,))
            pin.verify()
            outside = Path("/tmp") / f"dyro-s5-outside-{os.getpid()}.ts"
            outside.write_text("x", encoding="utf-8")
            try:
                bad = pin_host_provider(outside, allowed_roots=(root,))
                with self.assertRaisesRegex(Stage0ValidationError, "allowed_roots"):
                    bad.verify()
            finally:
                outside.unlink(missing_ok=True)

    def test_tamper_detected(self) -> None:
        with tempfile.TemporaryDirectory(dir=_DOCKER_TEMP_DIR, prefix=".s5t-") as tmp:
            root = Path(tmp)
            fixture = write_host_fixture_cli(root / "host_cli.ts")
            pin = pin_host_provider(fixture, allowed_roots=(root,))
            fixture.write_text("tampered\n", encoding="utf-8")
            with self.assertRaises(Stage0ValidationError):
                pin.verify()


class EvidenceDryRunUnitTests(unittest.TestCase):
    def test_refuse_production_actions(self) -> None:
        for key in ("merge", "push", "signoff", "import_evidence"):
            with self.assertRaises(Stage0ValidationError):
                refuse_production_actions({key: True})

    def test_dry_run_on_valid_pack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pack = _build_unit_pack(
                root,
                workflow_run_id="run-dry-1",
            )
            dry = dry_run_validate_pack(pack.pack_dir)
            self.assertTrue(dry.pack_sha256_verified)
            self.assertEqual(dry.report["verdict"], "ACCEPT_FOR_HUMAN_REVIEW_ONLY")
            self.assertFalse(dry.candidate_record["production_import"])
            self.assertTrue(dry.report_path.is_file())

    def test_dry_run_rejects_unknown_seal_kind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pack = _build_unit_pack(
                root,
                workflow_run_id="run-seal-kind",
            )
            seal_path = pack.pack_dir / "seal.json"
            seal = json.loads(seal_path.read_text(encoding="utf-8"))
            seal["kind"] = "untrusted-seal"
            seal_path.write_text(json.dumps(seal), encoding="utf-8")
            with self.assertRaisesRegex(
                Stage0ValidationError,
                "seal kind",
            ):
                dry_run_validate_pack(pack.pack_dir)

    def test_dry_run_rejects_incomplete_forbidden_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pack = _build_unit_pack(
                root,
                workflow_run_id="run-seal-actions",
            )
            seal_path = pack.pack_dir / "seal.json"
            seal = json.loads(seal_path.read_text(encoding="utf-8"))
            seal["actions_forbidden"] = ["signoff", "merge"]
            seal_path.write_text(json.dumps(seal), encoding="utf-8")
            with self.assertRaisesRegex(
                Stage0ValidationError,
                "must forbid",
            ):
                dry_run_validate_pack(pack.pack_dir)

    def test_dry_run_rejects_cross_file_workflow_id_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pack = _build_unit_pack(
                root,
                workflow_run_id="run-bound",
            )
            seal_path = pack.pack_dir / "seal.json"
            seal = json.loads(seal_path.read_text(encoding="utf-8"))
            seal["workflow_run_id"] = "run-substituted"
            seal_path.write_text(json.dumps(seal), encoding="utf-8")
            with self.assertRaisesRegex(
                Stage0ValidationError,
                "not bound",
            ):
                dry_run_validate_pack(pack.pack_dir)

    def test_dry_run_refuses_without_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = root / "report.md"
            artifact.write_text("x\n", encoding="utf-8")
            with self.assertRaisesRegex(Stage0ValidationError, "cleanup"):
                pack_run_evidence(
                    pack_root=root / "pack",
                    workflow_run_id="run-x",
                    claim={"schema_version": 1, "task_id": "t"},
                    canonical_input_sha256="c" * 64,
                    envelope={"status": "DONE"},
                    artifact_paths=(("repo", "report.md", artifact),),
                    provider_pin={},
                    claim_matrix={},
                    cleanup=CleanupProof(
                        sandbox_cleanup_verified=True,
                        broker_cleanup_verified=False,
                        broker_containers_absent=True,
                        raw_marker_leaked=False,
                        provider_token_leaked=False,
                    ),
                    mid_run_renewals=0,
                    provider_mode="argv-cli",
                )


class ProductionGateTests(unittest.TestCase):
    def test_not_ready(self) -> None:
        report = evaluate_production_readiness()
        self.assertFalse(report["production_ready"])
        self.assertEqual(report["verdict"], "NOT_READY")
        self.assertGreaterEqual(report["blocker_count"], 1)
        checklist = {
            item["id"]: item for item in report["checklist"]
        }
        self.assertEqual(checklist["PROD-03"]["status"], "pass")
        self.assertNotIn(
            "PROD-03",
            {item["id"] for item in report["blockers"]},
        )
        assert_not_production_ready(report)


@unittest.skipUnless(_docker_image_available(), f"requires local image {BUN_IMAGE}")
class Stage5EndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix=".e5-",
            dir=_DOCKER_TEMP_DIR,
        )
        self.root = Path(self.temporary.name)
        os.environ.pop(EXECUTION_KEY_ENV, None)
        self.host_cli = write_host_fixture_cli(self.root / "host" / "provider_cli.ts")
        self.host_pin = pin_host_provider(
            self.host_cli,
            allowed_roots=(self.root, ROOT),
        )

    def tearDown(self) -> None:
        os.environ.pop(EXECUTION_KEY_ENV, None)
        self.temporary.cleanup()

    def _assemble(self) -> dict[str, object]:
        return assemble_stage5_bundle(
            self.root / "bundle",
            runtime_lock_path=RUNTIME_LOCK,
            host_provider=self.host_pin,
        )

    def test_host_provider_pack_and_dry_run(self) -> None:
        assembled = self._assemble()
        worktree = self.root / "worktrees" / "docs"
        worktree.mkdir(parents=True)
        run_root = self.root / "run"
        run_root.mkdir()
        ipc_root = self.root / "ipc"
        ipc_root.mkdir()
        claim_path = self.root / "claim.json"
        pack_root = self.root / "evidence-pack"
        matrix = ClaimDeadlineMatrix(
            phase1_hold_ms=1200,
            phase2_agent_budget_ms=4000,
            phase3_hold_ms=1200,
            cleanup_ms=1500,
            safety_ms=1500,
        )
        now = time.time()
        ClaimStore(claim_path).write(
            ClaimRecord(
                task_id="task-stage5",
                runner_id="stage5-runner",
                generation=1,
                execution_key_id="exec-key-stage5",
                issued_at=now - 4.0,
                expires_at=now + 6.0,
            )
        )
        result = Stage5Supervisor(
            Stage5SupervisorConfig(
                bundle_root=assembled["bundle_root"],
                bundle_manifest=assembled["manifest"],
                bundle_identity=assembled["identity"],
                run_root=run_root,
                worktrees={"docs": worktree},
                ipc_root=ipc_root,
                claim_path=claim_path,
                pack_root=pack_root,
                host_provider=self.host_pin,
                canonical_input=CanonicalInput(
                    workflow_run_id="run-stage5-001",
                    task_id="task-stage5",
                    runner_id="stage5-runner",
                    claim_generation=1,
                    branches=("analysis-a", "analysis-b"),
                    artifact_repository="docs",
                    artifact_path="report.md",
                    model="fake-model",
                    max_agent_calls=4,
                ),
                artifact_policy=ArtifactPolicy(
                    repository_roots={"docs": worktree},
                    allowed_paths={("docs", "report.md")},
                    max_artifacts=4,
                    max_artifact_bytes=64 * 1024,
                ),
                claim_matrix=matrix,
                provider_mode="argv-cli",
            )
        ).execute()

        self.assertEqual(result.supervised.envelope["status"], "DONE")
        self.assertTrue(result.dual_cleanup_verified)
        self.assertEqual(result.host_provider["source"], "host")
        self.assertFalse(result.raw_marker_leaked)
        self.assertFalse(result.provider_token_leaked_to_sandbox_surface)
        self.assertNotIn(RAW_MARKER, result.telemetry_text)
        self.assertNotIn(STDERR_MARKER, result.telemetry_text)
        self.assertNotIn(PROVIDER_TOKEN, result.telemetry_text)
        self.assertNotIn(str(self.host_cli), result.telemetry_text)
        self.assertIn("host_provider", result.telemetry_text)
        self.assertGreaterEqual(result.mid_run_renewals, 1)

        report = (worktree / "report.md").read_text(encoding="utf-8")
        self.assertIn("Stage 5 workflow report", report)
        self.assertNotIn(str(self.host_cli), report)

        self.assertTrue(result.evidence_pack.zip_path.is_file())
        self.assertEqual(
            result.evidence_dry_run.report["verdict"],
            "ACCEPT_FOR_HUMAN_REVIEW_ONLY",
        )
        self.assertFalse(result.evidence_dry_run.candidate_record["production_merge"])
        self.assertEqual(result.production_gate["verdict"], "NOT_READY")
        self.assertFalse((self.root / ".dyro").exists())

    def test_import_action_rejected(self) -> None:
        assembled = self._assemble()
        worktree = self.root / "wt"
        worktree.mkdir()
        with self.assertRaisesRegex(Stage0ValidationError, "forbids production action"):
            Stage5Supervisor(
                Stage5SupervisorConfig(
                    bundle_root=assembled["bundle_root"],
                    bundle_manifest=assembled["manifest"],
                    bundle_identity=assembled["identity"],
                    run_root=self.root / "run",
                    worktrees={"docs": worktree},
                    ipc_root=self.root / "ipc",
                    claim_path=self.root / "claim.json",
                    pack_root=self.root / "pack",
                    host_provider=self.host_pin,
                    canonical_input=CanonicalInput(
                        workflow_run_id="run-x",
                        task_id="t",
                        runner_id="stage5-runner",
                        claim_generation=1,
                        branches=("a",),
                        artifact_repository="docs",
                        artifact_path="report.md",
                        model="fake-model",
                        max_agent_calls=1,
                    ),
                    artifact_policy=ArtifactPolicy(
                        repository_roots={"docs": worktree},
                        allowed_paths={("docs", "report.md")},
                        max_artifacts=1,
                        max_artifact_bytes=1024,
                    ),
                    claim_matrix=ClaimDeadlineMatrix(),
                    production_actions={"import_evidence": True},
                )
            )

    def test_execution_key_before_start_rejected(self) -> None:
        assembled = self._assemble()
        worktree = self.root / "worktrees" / "docs"
        worktree.mkdir(parents=True)
        run_root = self.root / "run"
        run_root.mkdir()
        claim_path = self.root / "claim.json"
        now = time.time()
        ClaimStore(claim_path).write(
            ClaimRecord(
                task_id="task-stage5",
                runner_id="stage5-runner",
                generation=1,
                execution_key_id="exec-key-stage5",
                issued_at=now,
                expires_at=now + 300,
            )
        )
        os.environ[EXECUTION_KEY_ENV] = "block"
        with self.assertRaisesRegex(Stage0ValidationError, "execution key"):
            Stage5Supervisor(
                Stage5SupervisorConfig(
                    bundle_root=assembled["bundle_root"],
                    bundle_manifest=assembled["manifest"],
                    bundle_identity=assembled["identity"],
                    run_root=run_root,
                    worktrees={"docs": worktree},
                    ipc_root=self.root / "ipc",
                    claim_path=claim_path,
                    pack_root=self.root / "pack",
                    host_provider=self.host_pin,
                    canonical_input=CanonicalInput(
                        workflow_run_id="run-stage5-002",
                        task_id="task-stage5",
                        runner_id="stage5-runner",
                        claim_generation=1,
                        branches=("analysis-a",),
                        artifact_repository="docs",
                        artifact_path="report.md",
                        model="fake-model",
                        max_agent_calls=2,
                    ),
                    artifact_policy=ArtifactPolicy(
                        repository_roots={"docs": worktree},
                        allowed_paths={("docs", "report.md")},
                        max_artifacts=2,
                        max_artifact_bytes=64 * 1024,
                    ),
                    claim_matrix=ClaimDeadlineMatrix(),
                )
            ).execute()


if __name__ == "__main__":
    unittest.main()

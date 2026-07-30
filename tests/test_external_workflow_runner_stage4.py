from __future__ import annotations

import hashlib
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
from experiments.external_workflow_runner.stage4.bundle import assemble_stage4_bundle
from experiments.external_workflow_runner.stage4.evidence_pack import (
    CleanupProof,
    pack_run_evidence,
    refuse_if_merge_requested,
)
from experiments.external_workflow_runner.stage4.provider_pin import (
    ProviderBinaryPin,
    pin_from_bundle_fixture,
)
from experiments.external_workflow_runner.stage4.supervisor import (
    EXECUTION_KEY_ENV,
    PROVIDER_TOKEN,
    RAW_MARKER,
    STDERR_MARKER,
    Stage4Supervisor,
    Stage4SupervisorConfig,
)
from experiments.external_workflow_runner.stage4.worktree_quota import WorktreeQuota


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


class ProviderPinTests(unittest.TestCase):
    def test_pin_from_fixture_and_verify(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = root / "fake_provider_cli.ts"
            fixture.write_text("console.log('ok')\n", encoding="utf-8")
            pin = pin_from_bundle_fixture(root)
            self.assertEqual(pin.relative_path, "fake_provider_cli.ts")
            self.assertEqual(
                pin.content_sha256,
                hashlib.sha256(fixture.read_bytes()).hexdigest(),
            )
            pin.verify(root)
            fixture.write_text("tampered\n", encoding="utf-8")
            with self.assertRaises(Stage0ValidationError):
                pin.verify(root)

    def test_pin_rejects_bad_sha(self) -> None:
        with self.assertRaises(Stage0ValidationError):
            ProviderBinaryPin(
                relative_path="x.ts",
                content_sha256="deadbeef",
                argv=("bun", "/opt/workflow/x.ts"),
            )


class EvidencePackUnitTests(unittest.TestCase):
    def test_refuse_merge_push_signoff(self) -> None:
        for key in ("merge", "push", "signoff", "import_evidence"):
            with self.assertRaises(Stage0ValidationError):
                refuse_if_merge_requested({key: True})

    def test_pack_requires_dual_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = root / "report.md"
            artifact.write_text("# report\n", encoding="utf-8")
            with self.assertRaisesRegex(Stage0ValidationError, "sandbox cleanup"):
                pack_run_evidence(
                    pack_root=root / "pack",
                    workflow_run_id="run-1",
                    claim={"task_id": "t"},
                    canonical_input_sha256="a" * 64,
                    envelope={"status": "DONE"},
                    artifact_paths=(("repo", "report.md", artifact),),
                    provider_pin={},
                    claim_matrix={},
                    cleanup=CleanupProof(
                        sandbox_cleanup_verified=False,
                        broker_cleanup_verified=True,
                        broker_containers_absent=True,
                        raw_marker_leaked=False,
                        provider_token_leaked=False,
                    ),
                    mid_run_renewals=0,
                    provider_mode="argv-cli",
                )

    def test_pack_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = root / "report.md"
            artifact.write_text("# report\n", encoding="utf-8")
            artifact_sha = hashlib.sha256(artifact.read_bytes()).hexdigest()
            result = pack_run_evidence(
                pack_root=root / "pack",
                workflow_run_id="run-1",
                claim={"schema_version": 1, "task_id": "t", "generation": 2},
                canonical_input_sha256="b" * 64,
                envelope={
                    "status": "DONE",
                    "workflow_run_id": "run-1",
                    "artifacts": [
                        {
                            "repository": "repo",
                            "path": "report.md",
                            "sha256": artifact_sha,
                        }
                    ],
                },
                artifact_paths=(("repo", "report.md", artifact),),
                provider_pin={"content_sha256": "c" * 64},
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
            self.assertTrue(result.zip_path.is_file())
            self.assertTrue(result.manifest_path.is_file())
            self.assertEqual(len(result.pack_sha256), 64)
            self.assertIn("no_merge", result.manifest["non_goals"])


class WorktreeQuotaTests(unittest.TestCase):
    def test_quota_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wt = root / "docs"
            wt.mkdir()
            (wt / "big.bin").write_bytes(b"x" * 200)
            quota = WorktreeQuota(
                max_bytes_per_worktree=100,
                max_total_bytes=1000,
                max_files_per_worktree=10,
            )
            with self.assertRaisesRegex(Stage0ValidationError, "quota exceeded"):
                quota.assert_within({"docs": wt})


@unittest.skipUnless(_docker_image_available(), f"requires local image {BUN_IMAGE}")
class Stage4EndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix=".e4-",
            dir=_DOCKER_TEMP_DIR,
        )
        self.root = Path(self.temporary.name)
        os.environ.pop(EXECUTION_KEY_ENV, None)

    def tearDown(self) -> None:
        os.environ.pop(EXECUTION_KEY_ENV, None)
        self.temporary.cleanup()

    def _assemble(self) -> dict[str, object]:
        return assemble_stage4_bundle(
            self.root / "bundle",
            runtime_lock_path=RUNTIME_LOCK,
        )

    def test_pinned_provider_dual_cleanup_and_evidence_pack(self) -> None:
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
                task_id="task-stage4",
                runner_id="stage4-runner",
                generation=1,
                execution_key_id="exec-key-stage4",
                issued_at=now - 4.0,
                expires_at=now + 6.0,
            )
        )
        result = Stage4Supervisor(
            Stage4SupervisorConfig(
                bundle_root=assembled["bundle_root"],
                bundle_manifest=assembled["manifest"],
                bundle_identity=assembled["identity"],
                run_root=run_root,
                worktrees={"docs": worktree},
                ipc_root=ipc_root,
                claim_path=claim_path,
                pack_root=pack_root,
                canonical_input=CanonicalInput(
                    workflow_run_id="run-stage4-001",
                    task_id="task-stage4",
                    runner_id="stage4-runner",
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
                provider_pin=assembled["provider_pin"],
                provider_mode="argv-cli",
                ipc_protocol_version=2,
            )
        ).execute()

        self.assertEqual(result.supervised.envelope["status"], "DONE")
        self.assertTrue(result.dual_cleanup_verified)
        self.assertTrue(result.sandbox_cleanup_verified)
        self.assertTrue(result.broker_cleanup_verified)
        self.assertEqual(result.provider_mode, "argv-cli")
        self.assertEqual(len(result.provider_pin["content_sha256"]), 64)
        self.assertFalse(result.raw_marker_leaked)
        self.assertFalse(result.provider_token_leaked_to_sandbox_surface)
        self.assertNotIn(RAW_MARKER, result.telemetry_text)
        self.assertNotIn(STDERR_MARKER, result.telemetry_text)
        self.assertNotIn(PROVIDER_TOKEN, result.telemetry_text)
        self.assertRegex(
            result.telemetry_text.replace(" ", ""),
            r'"raw_destroyed":true',
        )
        self.assertIn("provider_pin_verified", result.telemetry_text)
        self.assertGreaterEqual(result.mid_run_renewals, 1)
        self.assertGreaterEqual(result.claim.generation, 2)

        report = (worktree / "report.md").read_text(encoding="utf-8")
        self.assertIn("Stage 4 workflow report", report)
        self.assertIn("argv-cli", report)
        self.assertNotIn(RAW_MARKER, report)
        self.assertNotIn(PROVIDER_TOKEN, report)

        self.assertTrue(result.evidence_pack.zip_path.is_file())
        self.assertTrue(result.evidence_pack.manifest_path.is_file())
        self.assertEqual(len(result.evidence_pack.pack_sha256), 64)
        self.assertIn("no_merge", result.evidence_pack.manifest["non_goals"])
        self.assertIn("no_push", result.evidence_pack.manifest["non_goals"])
        self.assertFalse((self.root / ".dyro").exists())
        self.assertLessEqual(result.worktree_usage["total_bytes"], 64 * 1024)

    def test_merge_flag_rejected(self) -> None:
        assembled = self._assemble()
        worktree = self.root / "wt"
        worktree.mkdir(parents=True)
        with self.assertRaisesRegex(Stage0ValidationError, "forbids action: merge"):
            Stage4Supervisor(
                Stage4SupervisorConfig(
                    bundle_root=assembled["bundle_root"],
                    bundle_manifest=assembled["manifest"],
                    bundle_identity=assembled["identity"],
                    run_root=self.root / "run",
                    worktrees={"docs": worktree},
                    ipc_root=self.root / "ipc",
                    claim_path=self.root / "claim.json",
                    pack_root=self.root / "pack",
                    canonical_input=CanonicalInput(
                        workflow_run_id="run-x",
                        task_id="t",
                        runner_id="stage4-runner",
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
                    forbidden_actions={"merge": True},
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
                task_id="task-stage4",
                runner_id="stage4-runner",
                generation=1,
                execution_key_id="exec-key-stage4",
                issued_at=now,
                expires_at=now + 300,
            )
        )
        os.environ[EXECUTION_KEY_ENV] = "block"
        with self.assertRaisesRegex(Stage0ValidationError, "execution key"):
            Stage4Supervisor(
                Stage4SupervisorConfig(
                    bundle_root=assembled["bundle_root"],
                    bundle_manifest=assembled["manifest"],
                    bundle_identity=assembled["identity"],
                    run_root=run_root,
                    worktrees={"docs": worktree},
                    ipc_root=self.root / "ipc",
                    claim_path=claim_path,
                    pack_root=self.root / "pack",
                    canonical_input=CanonicalInput(
                        workflow_run_id="run-stage4-002",
                        task_id="task-stage4",
                        runner_id="stage4-runner",
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

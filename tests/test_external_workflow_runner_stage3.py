from __future__ import annotations

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
from experiments.external_workflow_runner.stage3.bundle import assemble_stage3_bundle
from experiments.external_workflow_runner.stage3.claim_matrix import ClaimDeadlineMatrix
from experiments.external_workflow_runner.stage3.supervisor import (
    EXECUTION_KEY_ENV,
    PROVIDER_TOKEN,
    RAW_MARKER,
    STDERR_MARKER,
    Stage3Supervisor,
    Stage3SupervisorConfig,
)


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_LOCK = ROOT / "experiments/external_workflow_runner/runtime-lock.json"
CACHED_TARBALL = Path("/tmp/ewr-tgz/dyro-semantic-flow-0.2.0.tgz")
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


def _tarball_source() -> Path | None:
    return CACHED_TARBALL if CACHED_TARBALL.is_file() else None


class ClaimMatrixTests(unittest.TestCase):
    def test_matrix_totals_and_recommendations(self) -> None:
        matrix = ClaimDeadlineMatrix(
            phase1_hold_ms=1000,
            phase2_agent_budget_ms=2000,
            phase3_hold_ms=1000,
            cleanup_ms=500,
            safety_ms=500,
        )
        self.assertEqual(matrix.total_ms, 5000)
        self.assertGreaterEqual(matrix.recommend_extend_seconds(), 5.0)
        self.assertGreaterEqual(matrix.recommend_workflow_timeout_seconds(), 15.0)

    def test_matrix_rejects_negative(self) -> None:
        with self.assertRaises(Stage0ValidationError):
            ClaimDeadlineMatrix(phase1_hold_ms=-1)


@unittest.skipUnless(_docker_image_available(), f"requires local image {BUN_IMAGE}")
class Stage3EndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix=".e3-",
            dir=_DOCKER_TEMP_DIR,
        )
        self.root = Path(self.temporary.name)
        os.environ.pop(EXECUTION_KEY_ENV, None)

    def tearDown(self) -> None:
        os.environ.pop(EXECUTION_KEY_ENV, None)
        self.temporary.cleanup()

    def _assemble(self) -> dict[str, object]:
        return assemble_stage3_bundle(
            self.root / "bundle",
            runtime_lock_path=RUNTIME_LOCK,
            tarball_source=_tarball_source(),
        )

    def test_argv_cli_raw_isolation_and_claim_matrix(self) -> None:
        assembled = self._assemble()
        worktree = self.root / "worktrees" / "docs"
        worktree.mkdir(parents=True)
        run_root = self.root / "run"
        run_root.mkdir()
        ipc_root = self.root / "ipc"
        ipc_root.mkdir()
        claim_path = self.root / "claim.json"
        matrix = ClaimDeadlineMatrix(
            phase1_hold_ms=1200,
            phase2_agent_budget_ms=4000,
            phase3_hold_ms=1200,
            cleanup_ms=1500,
            safety_ms=1500,
        )
        now = time.time()
        # Force mid-run renewal: short remaining half-life after phase1 starts.
        ClaimStore(claim_path).write(
            ClaimRecord(
                task_id="task-stage3",
                runner_id="stage3-runner",
                generation=1,
                execution_key_id="exec-key-stage3",
                issued_at=now - 1.0,
                expires_at=now + 3.0,
            )
        )
        result = Stage3Supervisor(
            Stage3SupervisorConfig(
                bundle_root=assembled["bundle_root"],
                bundle_manifest=assembled["manifest"],
                bundle_identity=assembled["identity"],
                run_root=run_root,
                worktrees={"docs": worktree},
                ipc_root=ipc_root,
                claim_path=claim_path,
                canonical_input=CanonicalInput(
                    workflow_run_id="run-stage3-001",
                    task_id="task-stage3",
                    runner_id="stage3-runner",
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
                ipc_protocol_version=2,
            )
        ).execute()

        self.assertEqual(result.supervised.envelope["status"], "DONE")
        self.assertTrue(result.cleanup_verified)
        self.assertEqual(result.provider_mode, "argv-cli")
        self.assertFalse(result.raw_marker_leaked)
        self.assertFalse(result.provider_token_leaked_to_sandbox_surface)
        self.assertNotIn(RAW_MARKER, result.telemetry_text)
        self.assertNotIn(STDERR_MARKER, result.telemetry_text)
        self.assertNotIn(PROVIDER_TOKEN, result.telemetry_text)
        self.assertRegex(
            result.telemetry_text.replace(" ", ""),
            r'"raw_destroyed":true',
        )
        self.assertIn("argv-cli", result.telemetry_text)
        self.assertGreaterEqual(result.mid_run_renewals, 1)
        self.assertGreaterEqual(result.claim.generation, 2)
        self.assertEqual(result.claim_matrix["phase1_hold_ms"], 1200)
        report = (worktree / "report.md").read_text(encoding="utf-8")
        self.assertIn("Stage 3 workflow report", report)
        self.assertIn("argv-cli", report)
        self.assertNotIn(RAW_MARKER, report)
        self.assertNotIn(PROVIDER_TOKEN, report)
        self.assertFalse((self.root / ".dyro").exists())

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
                task_id="task-stage3",
                runner_id="stage3-runner",
                generation=1,
                execution_key_id="exec-key-stage3",
                issued_at=now,
                expires_at=now + 300,
            )
        )
        os.environ[EXECUTION_KEY_ENV] = "block"
        with self.assertRaisesRegex(Stage0ValidationError, "execution key"):
            Stage3Supervisor(
                Stage3SupervisorConfig(
                    bundle_root=assembled["bundle_root"],
                    bundle_manifest=assembled["manifest"],
                    bundle_identity=assembled["identity"],
                    run_root=run_root,
                    worktrees={"docs": worktree},
                    ipc_root=self.root / "ipc",
                    claim_path=claim_path,
                    canonical_input=CanonicalInput(
                        workflow_run_id="run-stage3-002",
                        task_id="task-stage3",
                        runner_id="stage3-runner",
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

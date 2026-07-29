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
from experiments.external_workflow_runner.stage2.bundle import assemble_stage2_bundle
from experiments.external_workflow_runner.stage2.protocol import (
    SUPPORTED_PROTOCOL_VERSIONS,
    AgentCallRequestV2,
)
from experiments.external_workflow_runner.stage2.supervisor import (
    EXECUTION_KEY_ENV,
    RAW_MARKER,
    Stage2Supervisor,
    Stage2SupervisorConfig,
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
    probe = subprocess.run(
        ["docker", "image", "inspect", BUN_IMAGE],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return probe.returncode == 0


def _tarball_source() -> Path | None:
    if CACHED_TARBALL.is_file():
        return CACHED_TARBALL
    return None


class ProtocolVersionTests(unittest.TestCase):
    def test_v1_and_v2_accepted(self) -> None:
        self.assertEqual(SUPPORTED_PROTOCOL_VERSIONS, frozenset({1, 2}))
        v1 = AgentCallRequestV2.from_mapping(
            {
                "protocol_version": 1,
                "type": "agent.call",
                "call_id": "c1",
                "prompt": "hello",
                "model": "fake-model",
                "cwd": "/worktrees/docs",
                "deadline_ms": 1000,
            }
        )
        self.assertEqual(v1.protocol_version, 1)
        v2 = AgentCallRequestV2.from_mapping(
            {
                "protocol_version": 2,
                "type": "agent.call",
                "call_id": "c2",
                "prompt": "hello",
                "model": "fake-model",
                "cwd": "/worktrees/docs",
                "deadline_ms": 1000,
                "schema_hint": "text",
            }
        )
        self.assertEqual(v2.schema_hint, "text")

    def test_v3_rejected(self) -> None:
        with self.assertRaisesRegex(Stage0ValidationError, "unsupported"):
            AgentCallRequestV2.from_mapping(
                {
                    "protocol_version": 3,
                    "type": "agent.call",
                    "call_id": "c3",
                    "prompt": "hello",
                    "model": "fake-model",
                    "cwd": "/worktrees/docs",
                    "deadline_ms": 1000,
                }
            )

    def test_v1_rejects_schema_hint(self) -> None:
        with self.assertRaisesRegex(Stage0ValidationError, "schema_hint"):
            AgentCallRequestV2.from_mapping(
                {
                    "protocol_version": 1,
                    "type": "agent.call",
                    "call_id": "c4",
                    "prompt": "hello",
                    "model": "fake-model",
                    "cwd": "/worktrees/docs",
                    "deadline_ms": 1000,
                    "schema_hint": "text",
                }
            )


@unittest.skipUnless(_docker_image_available(), f"requires local image {BUN_IMAGE}")
class Stage2EndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix=".e2-",
            dir=_DOCKER_TEMP_DIR,
        )
        self.root = Path(self.temporary.name)
        os.environ.pop(EXECUTION_KEY_ENV, None)

    def tearDown(self) -> None:
        os.environ.pop(EXECUTION_KEY_ENV, None)
        self.temporary.cleanup()

    def _assemble(self) -> dict[str, object]:
        return assemble_stage2_bundle(
            self.root / "bundle",
            runtime_lock_path=RUNTIME_LOCK,
            tarball_source=_tarball_source(),
        )

    def test_simulated_cli_raw_isolation_and_claim_renewal(self) -> None:
        assembled = self._assemble()
        worktree = self.root / "worktrees" / "docs"
        worktree.mkdir(parents=True)
        run_root = self.root / "run"
        run_root.mkdir()
        ipc_root = self.root / "ipc"
        ipc_root.mkdir()
        claim_path = self.root / "claim.json"
        now = time.time()
        # Lifetime ~8s; half-life near now+3. Renewal fires during 2.5s hold
        # even under suite load, without expiring mid-run.
        ClaimStore(claim_path).write(
            ClaimRecord(
                task_id="task-stage2",
                runner_id="stage2-runner",
                generation=1,
                execution_key_id="exec-key-stage2",
                issued_at=now - 1,
                expires_at=now + 7,
            )
        )
        canonical = CanonicalInput(
            workflow_run_id="run-stage2-001",
            task_id="task-stage2",
            runner_id="stage2-runner",
            claim_generation=1,
            branches=("analysis-a", "analysis-b"),
            artifact_repository="docs",
            artifact_path="report.md",
            model="fake-model",
            max_agent_calls=4,
        )
        result = Stage2Supervisor(
            Stage2SupervisorConfig(
                bundle_root=assembled["bundle_root"],
                bundle_manifest=assembled["manifest"],
                bundle_identity=assembled["identity"],
                run_root=run_root,
                worktrees={"docs": worktree},
                ipc_root=ipc_root,
                claim_path=claim_path,
                canonical_input=canonical,
                artifact_policy=ArtifactPolicy(
                    repository_roots={"docs": worktree},
                    allowed_paths={("docs", "report.md")},
                    max_artifacts=4,
                    max_artifact_bytes=64 * 1024,
                ),
                provider_mode="simulated-cli",
                workflow_hold_ms=2500,
                claim_extend_seconds=30.0,
                claim_renewal_interval_seconds=0.2,
                ipc_protocol_version=2,
            )
        ).execute()

        self.assertEqual(result.supervised.envelope["status"], "DONE")
        self.assertTrue(result.cleanup_verified)
        self.assertFalse(result.execution_key_present_during_run)
        self.assertTrue(result.execution_key_mounted_after_cleanup)
        self.assertEqual(result.provider_mode, "simulated-cli")
        self.assertFalse(result.raw_marker_leaked)
        self.assertNotIn(RAW_MARKER, result.telemetry_text)
        self.assertNotIn("BEGIN PRIVATE KEY", result.telemetry_text)
        self.assertIn("raw_destroyed", result.telemetry_text)
        self.assertRegex(
            result.telemetry_text.replace(" ", ""), r'"raw_destroyed":true'
        )
        self.assertGreaterEqual(result.mid_run_renewals, 1)
        self.assertGreaterEqual(result.claim.generation, 2)
        report = (worktree / "report.md").read_text(encoding="utf-8")
        self.assertIn("Stage 2 workflow report", report)
        self.assertNotIn(RAW_MARKER, report)
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
                task_id="task-stage2",
                runner_id="stage2-runner",
                generation=1,
                execution_key_id="exec-key-stage2",
                issued_at=now,
                expires_at=now + 300,
            )
        )
        os.environ[EXECUTION_KEY_ENV] = "should-block"
        with self.assertRaisesRegex(Stage0ValidationError, "execution key"):
            Stage2Supervisor(
                Stage2SupervisorConfig(
                    bundle_root=assembled["bundle_root"],
                    bundle_manifest=assembled["manifest"],
                    bundle_identity=assembled["identity"],
                    run_root=run_root,
                    worktrees={"docs": worktree},
                    ipc_root=self.root / "ipc",
                    claim_path=claim_path,
                    canonical_input=CanonicalInput(
                        workflow_run_id="run-stage2-002",
                        task_id="task-stage2",
                        runner_id="stage2-runner",
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
                )
            ).execute()


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time
import unittest

from experiments.external_workflow_runner.artifacts import ArtifactPolicy
from experiments.external_workflow_runner.errors import Stage0ValidationError
from experiments.external_workflow_runner.sandbox import BUN_IMAGE
from experiments.external_workflow_runner.stage1.bundle import assemble_stage1_bundle
from experiments.external_workflow_runner.stage1.canonical import CanonicalInput
from experiments.external_workflow_runner.stage1.claim import (
    ClaimLease,
    ClaimRecord,
    ClaimStore,
)
from experiments.external_workflow_runner.stage1.package_runtime import (
    IMPLEMENTATION_NAME,
    RUNTIME_VERSION,
    hash_runtime_tree,
    load_runtime_lock,
    package_semantic_flow_runtime,
    RUNTIME_SOURCE,
    verify_runtime_lock,
)
from experiments.external_workflow_runner.stage1.protocol import (
    AgentCallRequest,
    AgentCallResponse,
    dumps_strict,
    loads_strict,
    sanitize_text,
)
from experiments.external_workflow_runner.stage1.broker_server import (
    BrokerProcess,
    default_fake_provider,
)
from experiments.external_workflow_runner.stage1.supervisor import (
    EXECUTION_KEY_ENV,
    Stage1Supervisor,
    Stage1SupervisorConfig,
)


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_LOCK = ROOT / "experiments/external_workflow_runner/runtime-lock.json"
# Colima (and similar VMs) only share project paths; keep Docker bind mounts under cwd.
_DOCKER_TEMP_DIR = ROOT


def _docker_image_available() -> bool:
    import shutil as sh
    import subprocess

    if sh.which("docker") is None:
        return False
    probe = subprocess.run(
        ["docker", "image", "inspect", BUN_IMAGE],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return probe.returncode == 0


class RuntimePackageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="ewr-stage1-pkg-")
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_runtime_lock_matches_first_party_identity(self) -> None:
        lock = load_runtime_lock(RUNTIME_LOCK)
        content = hash_runtime_tree(RUNTIME_SOURCE)
        verify_runtime_lock(lock, content_sha256=content)
        self.assertEqual(
            lock["workflow_runtime"]["implementation"], IMPLEMENTATION_NAME
        )
        self.assertEqual(lock["workflow_runtime"]["version"], RUNTIME_VERSION)
        self.assertEqual(lock["workflow_runtime"]["content_sha256"], content)

    def test_package_semantic_flow_writes_vendor_tree(self) -> None:
        result = package_semantic_flow_runtime(
            self.root / "pkg",
            runtime_lock_path=RUNTIME_LOCK,
        )
        self.assertTrue((result.package_root / "src" / "parallel.ts").is_file())
        self.assertTrue((result.package_root / "package.json").is_file())
        lock = json.loads(
            (result.vendor_root / "runtime-package-lock.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(lock["transitive_count"], 0)
        self.assertEqual(
            lock["packages"]["@dyro/semantic-flow"]["content_sha256"],
            result.content_sha256,
        )

    def test_package_rejects_lock_mismatch(self) -> None:
        bad_lock = self.root / "bad-lock.json"
        bad_lock.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "workflow_runtime": {
                        "implementation": IMPLEMENTATION_NAME,
                        "version": RUNTIME_VERSION,
                        "content_sha256": "0" * 64,
                    },
                    "runtime": load_runtime_lock(RUNTIME_LOCK)["runtime"],
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(Stage0ValidationError, "first-party|identity"):
            package_semantic_flow_runtime(
                self.root / "pkg",
                runtime_lock_path=bad_lock,
            )


class ProtocolAndClaimTests(unittest.TestCase):
    def test_sanitize_redacts_secret_markers(self) -> None:
        text = sanitize_text("token sk-abc execution-key BEGIN PRIVATE KEY")
        self.assertNotIn("sk-", text)
        self.assertNotIn("execution-key", text)
        self.assertNotIn("BEGIN PRIVATE KEY", text)

    def test_request_response_round_trip(self) -> None:
        request = AgentCallRequest(
            call_id="call-1",
            prompt="summarize docs",
            model="fake-model",
            cwd="/worktrees/docs",
            deadline_ms=1000,
        )
        encoded = dumps_strict(request.to_mapping())
        decoded = AgentCallRequest.from_mapping(loads_strict(encoded))
        self.assertEqual(decoded, request)
        response = default_fake_provider(decoded)
        self.assertEqual(response.status, "ok")
        self.assertIn("fake-provider", response.text)

    def test_claim_renewal_increments_generation(self) -> None:
        now = time.time()
        claim = ClaimRecord(
            task_id="task-1",
            runner_id="stage1-runner",
            generation=1,
            execution_key_id="exec-key-1",
            issued_at=now - 100,
            expires_at=now + 100,
        )
        lease = ClaimLease(record=claim, renewals=[])
        self.assertTrue(lease.should_renew(now=now))
        renewed = lease.renew(extend_seconds=60, now=now)
        self.assertEqual(renewed.generation, 2)
        self.assertEqual(len(lease.renewals), 1)

    def test_claim_store_rejects_expired(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="ewr-stage1-claim-")
        try:
            path = Path(temporary.name) / "claim.json"
            store = ClaimStore(path)
            now = time.time()
            store.write(
                ClaimRecord(
                    task_id="task-1",
                    runner_id="stage1-runner",
                    generation=1,
                    execution_key_id="exec-key-1",
                    issued_at=now - 20,
                    expires_at=now - 1,
                )
            )
            with self.assertRaisesRegex(Stage0ValidationError, "expired"):
                store.assert_matches(runner_id="stage1-runner", now=now)
        finally:
            temporary.cleanup()


class BrokerIpcTests(unittest.TestCase):
    def test_fake_provider_over_unix_socket(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="ewr-stage1-broker-")
        try:
            root = Path(temporary.name)
            broker = BrokerProcess.start(root, max_concurrency=1)
            try:
                import socket

                request = AgentCallRequest(
                    call_id="call-socket-1",
                    prompt="hello broker",
                    model="fake-model",
                    cwd="/worktrees/docs",
                    deadline_ms=2000,
                )
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(3)
                    client.connect(os.fspath(broker.socket_path))
                    client.sendall((dumps_strict(request.to_mapping()) + "\n").encode())
                    line = b""
                    while not line.endswith(b"\n"):
                        chunk = client.recv(4096)
                        self.assertTrue(chunk)
                        line += chunk
                response = AgentCallResponse.from_mapping(
                    loads_strict(line.decode("utf-8").strip())
                )
                self.assertEqual(response.status, "ok")
                self.assertEqual(response.call_id, "call-socket-1")
                telemetry = broker.telemetry_path.read_text(encoding="utf-8")
                self.assertIn("agent_call", telemetry)
                self.assertNotIn("BEGIN PRIVATE KEY", telemetry)
            finally:
                broker.stop()
        finally:
            temporary.cleanup()


@unittest.skipUnless(_docker_image_available(), f"requires local image {BUN_IMAGE}")
class Stage1EndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        # Short directory names keep Unix-socket paths under the AF_UNIX limit.
        self.temporary = tempfile.TemporaryDirectory(
            prefix=".e1-",
            dir=_DOCKER_TEMP_DIR,
        )
        self.root = Path(self.temporary.name)
        os.environ.pop(EXECUTION_KEY_ENV, None)

    def tearDown(self) -> None:
        os.environ.pop(EXECUTION_KEY_ENV, None)
        self.temporary.cleanup()

    def test_stage1_fixed_bundle_broker_and_key_gate(self) -> None:
        assembled = assemble_stage1_bundle(
            self.root / "bundle",
            runtime_lock_path=RUNTIME_LOCK,
        )
        worktree = self.root / "worktrees" / "docs"
        worktree.mkdir(parents=True)
        run_root = self.root / "run"
        run_root.mkdir()
        ipc_root = self.root / "ipc"
        ipc_root.mkdir()
        claim_path = self.root / "claim.json"
        now = time.time()
        ClaimStore(claim_path).write(
            ClaimRecord(
                task_id="task-stage1",
                runner_id="stage1-runner",
                generation=1,
                execution_key_id="exec-key-stage1",
                issued_at=now - 10,
                expires_at=now + 300,
            )
        )
        canonical = CanonicalInput(
            workflow_run_id="run-stage1-001",
            task_id="task-stage1",
            runner_id="stage1-runner",
            claim_generation=1,
            branches=("analysis-a", "analysis-b"),
            artifact_repository="docs",
            artifact_path="report.md",
            model="fake-model",
            max_agent_calls=4,
        )
        supervisor = Stage1Supervisor(
            Stage1SupervisorConfig(
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
                workflow_timeout_seconds=90.0,
            )
        )
        result = supervisor.execute()
        self.assertTrue(result.cleanup_verified)
        self.assertFalse(result.execution_key_present_during_run)
        self.assertTrue(result.execution_key_mounted_after_cleanup)
        self.assertEqual(result.supervised.envelope["status"], "DONE")
        self.assertEqual(len(result.supervised.artifacts), 1)
        report = worktree / "report.md"
        self.assertTrue(report.is_file())
        self.assertIn("Stage 1 workflow report", report.read_text(encoding="utf-8"))
        telemetry = result.broker_telemetry_path.read_text(encoding="utf-8")
        self.assertGreaterEqual(telemetry.count("agent_call"), 2)
        self.assertNotIn(EXECUTION_KEY_ENV, os.environ)
        # No evidence / signoff / merge side effects.
        self.assertFalse((self.root / ".dyro").exists())

    def test_execution_key_before_start_is_rejected(self) -> None:
        assembled = assemble_stage1_bundle(
            self.root / "bundle",
            runtime_lock_path=RUNTIME_LOCK,
        )
        worktree = self.root / "worktrees" / "docs"
        worktree.mkdir(parents=True)
        run_root = self.root / "run"
        run_root.mkdir()
        claim_path = self.root / "claim.json"
        now = time.time()
        ClaimStore(claim_path).write(
            ClaimRecord(
                task_id="task-stage1",
                runner_id="stage1-runner",
                generation=1,
                execution_key_id="exec-key-stage1",
                issued_at=now,
                expires_at=now + 300,
            )
        )
        os.environ[EXECUTION_KEY_ENV] = "should-block-start"
        supervisor = Stage1Supervisor(
            Stage1SupervisorConfig(
                bundle_root=assembled["bundle_root"],
                bundle_manifest=assembled["manifest"],
                bundle_identity=assembled["identity"],
                run_root=run_root,
                worktrees={"docs": worktree},
                ipc_root=self.root / "ipc",
                claim_path=claim_path,
                canonical_input=CanonicalInput(
                    workflow_run_id="run-stage1-002",
                    task_id="task-stage1",
                    runner_id="stage1-runner",
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
        )
        with self.assertRaisesRegex(Stage0ValidationError, "execution key"):
            supervisor.execute()


if __name__ == "__main__":
    unittest.main()

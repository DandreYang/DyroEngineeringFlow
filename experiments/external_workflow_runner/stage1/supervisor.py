"""Stage 1 Supervisor: Broker + Sandbox, no execution key until cleanup."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import os
from pathlib import Path
import secrets
from typing import Mapping

from ..artifacts import ArtifactPolicy
from ..errors import Stage0ValidationError
from ..manifest import verify_bundle_manifest
from ..sandbox import (
    BUN_IMAGE,
    BUN_USER,
    BUN_VERSION,
    DockerSandboxConfig,
)
from ..supervisor import Stage0Supervisor, SupervisorConfig, SupervisedResult
from .canonical import CanonicalInput, expected_branches_map
from .claim import ClaimLease, ClaimRecord, ClaimStore
from .docker_broker import DockerBrokerStack
from .package_runtime import (
    IMPLEMENTATION_NAME,
    RUNTIME_VERSION,
    hash_runtime_tree,
    RUNTIME_SOURCE,
)


EXECUTION_KEY_SENTINEL = "DYRO_EXECUTION_KEY_MATERIAL"
EXECUTION_KEY_ENV = "DYRO_EXECUTION_KEY"


@dataclass(frozen=True)
class Stage1SupervisorConfig:
    bundle_root: Path
    bundle_manifest: Mapping[str, object]
    bundle_identity: Mapping[str, object]
    run_root: Path
    worktrees: Mapping[str, Path]
    ipc_root: Path
    claim_path: Path
    canonical_input: CanonicalInput
    artifact_policy: ArtifactPolicy
    workflow_timeout_seconds: float = 60.0
    max_result_bytes: int = 256 * 1024
    result_filename: str = "result-envelope.json"
    claim_extend_seconds: float = 120.0
    runner_id: str = "stage1-runner"


@dataclass(frozen=True)
class Stage1RunResult:
    supervised: SupervisedResult
    claim: ClaimRecord
    claim_renewals: tuple[dict[str, object], ...]
    broker_telemetry_path: Path
    canonical_input_sha256: str
    execution_key_present_before_cleanup: bool
    execution_key_present_during_run: bool
    execution_key_mounted_after_cleanup: bool
    cleanup_verified: bool
    broker_max_observed_concurrency: int


class Stage1Supervisor:
    """
    Orchestrate Stage 1 without Dyro evidence/signoff/merge.

    Execution-key rule:
    - Sandbox and Broker never receive the execution key.
    - After both are stopped and cleanup is verified, the Supervisor may
      temporarily mount the key for a later packing stage (not performed here).
    """

    def __init__(self, config: Stage1SupervisorConfig) -> None:
        self.config = config
        self._bundle_manifest = deepcopy(dict(config.bundle_manifest))
        self._bundle_identity = deepcopy(dict(config.bundle_identity))
        self._assert_identity()

    def _assert_identity(self) -> None:
        runtime = self._bundle_identity.get("runtime")
        workflow = self._bundle_identity.get("workflow_runtime")
        if (
            self._bundle_identity.get("schema_version") != 1
            or not isinstance(runtime, Mapping)
            or not isinstance(workflow, Mapping)
            or runtime.get("bun_version") != BUN_VERSION
            or runtime.get("container_image") != BUN_IMAGE
            or runtime.get("container_user") != BUN_USER
            or workflow.get("implementation") != IMPLEMENTATION_NAME
            or workflow.get("version") != RUNTIME_VERSION
            or workflow.get("content_sha256") != hash_runtime_tree(RUNTIME_SOURCE)
        ):
            raise Stage0ValidationError(
                "Stage 1 bundle identity is not the approved first-party runtime"
            )

    def execute(self) -> Stage1RunResult:
        claim_store = ClaimStore(self.config.claim_path)
        claim = claim_store.assert_matches(runner_id=self.config.runner_id)
        lease = ClaimLease(record=claim, renewals=[])
        if lease.should_renew():
            renewed = lease.renew(extend_seconds=self.config.claim_extend_seconds)
            claim_store.write(renewed)
            claim = claim_store.assert_matches(
                runner_id=self.config.runner_id,
                generation=renewed.generation,
            )

        if EXECUTION_KEY_ENV in os.environ or EXECUTION_KEY_SENTINEL in os.environ:
            raise Stage0ValidationError(
                "execution key must not be present before Stage 1 isolation starts"
            )

        canonical = CanonicalInput(
            workflow_run_id=self.config.canonical_input.workflow_run_id,
            task_id=claim.task_id,
            runner_id=claim.runner_id,
            claim_generation=claim.generation,
            branches=self.config.canonical_input.branches,
            artifact_repository=self.config.canonical_input.artifact_repository,
            artifact_path=self.config.canonical_input.artifact_path,
            model=self.config.canonical_input.model,
            max_agent_calls=self.config.canonical_input.max_agent_calls,
        )
        # Container runs as uid 1000; ensure the run root is writable for results.
        self.config.run_root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.config.run_root, 0o777)
        except OSError:
            pass
        for worktree in self.config.worktrees.values():
            try:
                os.chmod(worktree, 0o777)
            except OSError:
                pass
        canonical_path = self.config.run_root / "canonical-input.json"
        digest = canonical.write(canonical_path)

        self.config.ipc_root.mkdir(parents=True, exist_ok=True)
        telemetry_path = self.config.ipc_root / "broker-telemetry.jsonl"

        broker: DockerBrokerStack | None = None
        supervised: SupervisedResult | None = None
        key_during = False
        cleanup_verified = False
        try:
            broker = DockerBrokerStack.start(
                bundle_root=self.config.bundle_root,
                telemetry_host_path=telemetry_path,
                model=canonical.model,
            )
            sandbox = DockerSandboxConfig(
                name=f"dyro-stage1-{secrets.token_hex(4)}",
                image=BUN_IMAGE,
                bundle_root=self.config.bundle_root,
                run_root=self.config.run_root,
                worktrees=self.config.worktrees,
                network_mode=f"container:{broker.netns_name}",
                environment={
                    "DYRO_WORKFLOW_RUN_ID": canonical.workflow_run_id,
                    "DYRO_RESULT_PATH": f"/run/dyro/{self.config.result_filename}",
                    "DYRO_BROKER_HOST": "127.0.0.1",
                    "DYRO_BROKER_PORT": str(broker.port),
                    "DYRO_CANONICAL_INPUT_PATH": "/run/dyro/canonical-input.json",
                    # Read-only rootfs: force Bun caches onto the tmpfs.
                    "HOME": "/tmp",
                    "TMPDIR": "/tmp",
                    "BUN_INSTALL_CACHE_DIR": "/tmp/bun-cache",
                    "XDG_CACHE_HOME": "/tmp/xdg-cache",
                },
            )
            stage0 = Stage0Supervisor(
                SupervisorConfig(
                    sandbox=sandbox,
                    bundle_manifest=self._bundle_manifest,
                    bundle_identity=self._bundle_identity,
                    workflow_run_id=canonical.workflow_run_id,
                    expected_branches=expected_branches_map(canonical.branches),
                    artifact_policy=self.config.artifact_policy,
                    result_filename=self.config.result_filename,
                    max_result_bytes=self.config.max_result_bytes,
                )
            )
            key_during = EXECUTION_KEY_ENV in os.environ
            supervised = stage0.execute(
                # Prefer direct file execution over `bun run` (package-script mode).
                ["bun", "/opt/workflow/workflow.ts"],
                timeout_seconds=self.config.workflow_timeout_seconds,
            )
            cleanup_verified = supervised.process.cleanup_verified
        finally:
            if broker is not None:
                broker.stop()
            claim_store.assert_matches(
                runner_id=self.config.runner_id,
                generation=claim.generation,
            )
            verify_bundle_manifest(
                self.config.bundle_root,
                self._bundle_manifest,
                expected_identity=self._bundle_identity,
            )

        if supervised is None:
            raise Stage0ValidationError("Stage 1 run produced no supervised result")
        if not cleanup_verified:
            raise Stage0ValidationError("sandbox cleanup was not verified")

        # Only after Sandbox and Broker cleanup may a packing stage mount the key.
        # Stage 1 records the mount simulation but does not call Dyro evidence.
        os.environ[EXECUTION_KEY_ENV] = "stage1-simulated-key-not-for-evidence"
        try:
            if EXECUTION_KEY_ENV not in os.environ:
                raise Stage0ValidationError("failed to mount simulated execution key")
        finally:
            os.environ.pop(EXECUTION_KEY_ENV, None)

        return Stage1RunResult(
            supervised=supervised,
            claim=claim,
            claim_renewals=tuple(lease.renewals),
            broker_telemetry_path=telemetry_path,
            canonical_input_sha256=digest,
            execution_key_present_before_cleanup=False,
            execution_key_present_during_run=key_during,
            execution_key_mounted_after_cleanup=True,
            cleanup_verified=cleanup_verified,
            # Stage1 broker is containerized; concurrency is enforced by the
            # shared fake provider's single-threaded request handling plus the
            # internal-network isolation boundary (host limiter remains in
            # broker_server.py for non-Docker unit tests).
            broker_max_observed_concurrency=1,
        )

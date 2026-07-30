"""Stage 2 Supervisor: simulated-cli broker, mid-run claim renewal, no evidence."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import os
from pathlib import Path
import secrets
import sys
from typing import Mapping

from ..artifacts import ArtifactPolicy
from ..errors import report_cleanup_failures, Stage0ValidationError
from ..manifest import verify_bundle_manifest
from ..sandbox import (
    BUN_IMAGE,
    BUN_USER,
    BUN_VERSION,
    DockerSandboxConfig,
)
from ..stage1.canonical import CanonicalInput, expected_branches_map
from ..stage1.claim import ClaimLease, ClaimRecord, ClaimStore
from ..stage1.package_runtime import (
    IMPLEMENTATION_NAME,
    RUNTIME_VERSION,
    RUNTIME_SOURCE,
    hash_runtime_tree,
)
from ..supervisor import Stage0Supervisor, SupervisorConfig, SupervisedResult
from .claim_renewal import ClaimRenewalLoop
from .docker_broker import Stage2DockerBrokerStack


EXECUTION_KEY_ENV = "DYRO_EXECUTION_KEY"
EXECUTION_KEY_SENTINEL = "DYRO_EXECUTION_KEY_MATERIAL"
RAW_MARKER = "RAW_VENDOR_SECRET_MUST_NOT_ESCAPE"


@dataclass(frozen=True)
class Stage2SupervisorConfig:
    bundle_root: Path
    bundle_manifest: Mapping[str, object]
    bundle_identity: Mapping[str, object]
    run_root: Path
    worktrees: Mapping[str, Path]
    ipc_root: Path
    claim_path: Path
    canonical_input: CanonicalInput
    artifact_policy: ArtifactPolicy
    workflow_timeout_seconds: float = 90.0
    max_result_bytes: int = 256 * 1024
    result_filename: str = "result-envelope.json"
    claim_extend_seconds: float = 30.0
    claim_renewal_interval_seconds: float = 0.2
    runner_id: str = "stage2-runner"
    provider_mode: str = "simulated-cli"
    max_broker_concurrency: int = 2
    ipc_protocol_version: int = 2
    workflow_hold_ms: int = 2500


@dataclass(frozen=True)
class Stage2RunResult:
    supervised: SupervisedResult
    claim: ClaimRecord
    claim_renewals: tuple[dict[str, object], ...]
    mid_run_renewals: int
    broker_telemetry_path: Path
    canonical_input_sha256: str
    execution_key_present_during_run: bool
    execution_key_mounted_after_cleanup: bool
    cleanup_verified: bool
    provider_mode: str
    raw_marker_leaked: bool
    telemetry_text: str


class Stage2Supervisor:
    def __init__(self, config: Stage2SupervisorConfig) -> None:
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
            or self._bundle_identity.get("stage") != 2
        ):
            raise Stage0ValidationError(
                "Stage 2 bundle identity is not the approved first-party runtime"
            )

    def execute(self) -> Stage2RunResult:
        claim_store = ClaimStore(self.config.claim_path)
        claim = claim_store.assert_matches(runner_id=self.config.runner_id)
        lease = ClaimLease(record=claim, renewals=[])

        if EXECUTION_KEY_ENV in os.environ or EXECUTION_KEY_SENTINEL in os.environ:
            raise Stage0ValidationError(
                "execution key must not be present before Stage 2 isolation starts"
            )

        # Bind canonical input to the current claim generation at start.
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
        digest = canonical.write(self.config.run_root / "canonical-input.json")

        self.config.ipc_root.mkdir(parents=True, exist_ok=True)
        telemetry_path = self.config.ipc_root / "broker-telemetry.jsonl"

        renewal = ClaimRenewalLoop(
            store=claim_store,
            lease=lease,
            extend_seconds=self.config.claim_extend_seconds,
            interval_seconds=self.config.claim_renewal_interval_seconds,
            runner_id=self.config.runner_id,
        )
        broker: Stage2DockerBrokerStack | None = None
        supervised: SupervisedResult | None = None
        final_claim: ClaimRecord | None = None
        key_during = False
        cleanup_verified = False
        try:
            # Keep authority live during Docker startup, not only after readiness.
            renewal.start()
            broker = Stage2DockerBrokerStack.start(
                bundle_root=self.config.bundle_root,
                telemetry_host_path=telemetry_path,
                model=canonical.model,
                provider_mode=self.config.provider_mode,
                max_concurrency=self.config.max_broker_concurrency,
            )
            sandbox = DockerSandboxConfig(
                name=f"dyro-stage2-{secrets.token_hex(4)}",
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
                    "DYRO_IPC_PROTOCOL_VERSION": str(self.config.ipc_protocol_version),
                    "DYRO_PROVIDER_MODE": self.config.provider_mode,
                    "DYRO_STAGE2_HOLD_MS": str(self.config.workflow_hold_ms),
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
                ["bun", "/opt/workflow/workflow.ts"],
                timeout_seconds=self.config.workflow_timeout_seconds,
            )
            cleanup_verified = supervised.process.cleanup_verified
        finally:
            primary_error = sys.exception()
            cleanup_errors: list[str] = []
            try:
                renewal.stop()
            except Exception as exc:  # noqa: BLE001 - continue mandatory cleanup
                cleanup_errors.append(f"claim renewal stop: {exc}")
            if broker is not None:
                try:
                    broker.stop()
                except Exception as exc:  # noqa: BLE001 - aggregate after all cleanup
                    cleanup_errors.append(f"broker stop: {exc}")
            try:
                final_claim = claim_store.assert_matches(
                    runner_id=self.config.runner_id,
                    generation=lease.record.generation,
                    execution_key_id=lease.record.execution_key_id,
                    task_id=lease.record.task_id,
                )
            except Exception as exc:  # noqa: BLE001 - aggregate invariant failures
                cleanup_errors.append(f"claim verification: {exc}")
            try:
                verify_bundle_manifest(
                    self.config.bundle_root,
                    self._bundle_manifest,
                    expected_identity=self._bundle_identity,
                )
            except Exception as exc:  # noqa: BLE001 - aggregate invariant failures
                cleanup_errors.append(f"bundle verification: {exc}")
            report_cleanup_failures(
                "Stage 2",
                cleanup_errors,
                primary_error=primary_error,
            )

        if supervised is None:
            raise Stage0ValidationError("Stage 2 run produced no supervised result")
        if final_claim is None:
            raise Stage0ValidationError("Stage 2 final claim was not verified")
        if not cleanup_verified:
            raise Stage0ValidationError("sandbox cleanup was not verified")

        telemetry_text = (
            telemetry_path.read_text(encoding="utf-8")
            if telemetry_path.is_file()
            else ""
        )
        report_path = (
            Path(self.config.worktrees[canonical.artifact_repository])
            / canonical.artifact_path
        )
        report_text = (
            report_path.read_text(encoding="utf-8") if report_path.is_file() else ""
        )
        leaked = (
            RAW_MARKER in telemetry_text
            or RAW_MARKER in report_text
            or "BEGIN PRIVATE KEY" in telemetry_text
            or "BEGIN PRIVATE KEY" in report_text
            or "sk-simulated-vendor-token" in telemetry_text
            or "sk-simulated-vendor-token" in report_text
        )
        if leaked:
            raise Stage0ValidationError(
                "raw provider material leaked into telemetry or artifacts"
            )

        os.environ[EXECUTION_KEY_ENV] = "stage2-simulated-key-not-for-evidence"
        try:
            if EXECUTION_KEY_ENV not in os.environ:
                raise Stage0ValidationError("failed to mount simulated execution key")
        finally:
            os.environ.pop(EXECUTION_KEY_ENV, None)

        return Stage2RunResult(
            supervised=supervised,
            claim=final_claim,
            claim_renewals=tuple(lease.renewals),
            mid_run_renewals=renewal.renewals_observed,
            broker_telemetry_path=telemetry_path,
            canonical_input_sha256=digest,
            execution_key_present_during_run=key_during,
            execution_key_mounted_after_cleanup=True,
            cleanup_verified=cleanup_verified,
            provider_mode=self.config.provider_mode,
            raw_marker_leaked=False,
            telemetry_text=telemetry_text,
        )

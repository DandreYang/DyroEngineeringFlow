"""Stage 3 Supervisor: argv-cli provider, claim matrix, no evidence."""

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
from ..stage2.claim_renewal import ClaimRenewalLoop
from ..supervisor import Stage0Supervisor, SupervisorConfig, SupervisedResult
from .claim_matrix import ClaimDeadlineMatrix
from .docker_broker import Stage3DockerBrokerStack


EXECUTION_KEY_ENV = "DYRO_EXECUTION_KEY"
EXECUTION_KEY_SENTINEL = "DYRO_EXECUTION_KEY_MATERIAL"
RAW_MARKER = "RAW_VENDOR_SECRET_MUST_NOT_ESCAPE"
STDERR_MARKER = "RAW_VENDOR_STDERR_MARKER"
PROVIDER_TOKEN = "stage3-broker-only-token"


@dataclass(frozen=True)
class Stage3SupervisorConfig:
    bundle_root: Path
    bundle_manifest: Mapping[str, object]
    bundle_identity: Mapping[str, object]
    run_root: Path
    worktrees: Mapping[str, Path]
    ipc_root: Path
    claim_path: Path
    canonical_input: CanonicalInput
    artifact_policy: ArtifactPolicy
    claim_matrix: ClaimDeadlineMatrix
    workflow_timeout_seconds: float | None = None
    max_result_bytes: int = 256 * 1024
    result_filename: str = "result-envelope.json"
    claim_renewal_interval_seconds: float = 0.2
    runner_id: str = "stage3-runner"
    provider_mode: str = "argv-cli"
    max_broker_concurrency: int = 2
    ipc_protocol_version: int = 2


@dataclass(frozen=True)
class Stage3RunResult:
    supervised: SupervisedResult
    claim: ClaimRecord
    claim_renewals: tuple[dict[str, object], ...]
    mid_run_renewals: int
    claim_matrix: dict[str, object]
    broker_telemetry_path: Path
    canonical_input_sha256: str
    execution_key_present_during_run: bool
    execution_key_mounted_after_cleanup: bool
    cleanup_verified: bool
    provider_mode: str
    raw_marker_leaked: bool
    provider_token_leaked_to_sandbox_surface: bool
    telemetry_text: str


class Stage3Supervisor:
    def __init__(self, config: Stage3SupervisorConfig) -> None:
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
            or self._bundle_identity.get("stage") != 3
        ):
            raise Stage0ValidationError(
                "Stage 3 bundle identity is not the approved first-party runtime"
            )

    def execute(self) -> Stage3RunResult:
        matrix = self.config.claim_matrix
        claim_store = ClaimStore(self.config.claim_path)
        claim = claim_store.assert_matches(runner_id=self.config.runner_id)
        lease = ClaimLease(record=claim, renewals=[])

        if EXECUTION_KEY_ENV in os.environ or EXECUTION_KEY_SENTINEL in os.environ:
            raise Stage0ValidationError(
                "execution key must not be present before Stage 3 isolation starts"
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
        # Prove provider token is not placed on the sandbox-mounted run root.
        for path in self.config.run_root.rglob("*"):
            if path.is_file() and PROVIDER_TOKEN in path.read_text(
                encoding="utf-8", errors="ignore"
            ):
                raise Stage0ValidationError(
                    "provider token leaked onto sandbox-visible run root"
                )

        self.config.ipc_root.mkdir(parents=True, exist_ok=True)
        telemetry_path = self.config.ipc_root / "broker-telemetry.jsonl"
        timeout = (
            self.config.workflow_timeout_seconds
            if self.config.workflow_timeout_seconds is not None
            else matrix.recommend_workflow_timeout_seconds()
        )

        renewal = ClaimRenewalLoop(
            store=claim_store,
            lease=lease,
            extend_seconds=matrix.recommend_extend_seconds(),
            interval_seconds=self.config.claim_renewal_interval_seconds,
            runner_id=self.config.runner_id,
        )
        broker: Stage3DockerBrokerStack | None = None
        supervised: SupervisedResult | None = None
        final_claim: ClaimRecord | None = None
        key_during = False
        cleanup_verified = False
        try:
            # Keep authority live during Docker startup, not only after readiness.
            renewal.start()
            broker = Stage3DockerBrokerStack.start(
                bundle_root=self.config.bundle_root,
                telemetry_host_path=telemetry_path,
                model=canonical.model,
                provider_mode=self.config.provider_mode,
                max_concurrency=self.config.max_broker_concurrency,
                provider_fake_token=PROVIDER_TOKEN,
            )
            sandbox = DockerSandboxConfig(
                name=f"dyro-stage3-{secrets.token_hex(4)}",
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
                    "DYRO_STAGE3_PHASE1_MS": str(matrix.phase1_hold_ms),
                    "DYRO_STAGE3_PHASE3_MS": str(matrix.phase3_hold_ms),
                    # Explicitly do NOT pass DYRO_PROVIDER_FAKE_TOKEN or execution keys.
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
                timeout_seconds=timeout,
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
                "Stage 3",
                cleanup_errors,
                primary_error=primary_error,
            )

        if supervised is None:
            raise Stage0ValidationError("Stage 3 run produced no supervised result")
        if final_claim is None:
            raise Stage0ValidationError("Stage 3 final claim was not verified")
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
        leaked = any(
            marker in surface
            for surface in (telemetry_text, report_text)
            for marker in (
                RAW_MARKER,
                STDERR_MARKER,
                "BEGIN PRIVATE KEY",
                "sk-stage3-cli-token",
            )
        )
        token_leaked = PROVIDER_TOKEN in report_text or PROVIDER_TOKEN in telemetry_text
        if leaked:
            raise Stage0ValidationError(
                "raw provider material leaked into telemetry or artifacts"
            )
        if token_leaked:
            raise Stage0ValidationError(
                "provider token leaked into sandbox-visible surfaces"
            )

        os.environ[EXECUTION_KEY_ENV] = "stage3-simulated-key-not-for-evidence"
        try:
            if EXECUTION_KEY_ENV not in os.environ:
                raise Stage0ValidationError("failed to mount simulated execution key")
        finally:
            os.environ.pop(EXECUTION_KEY_ENV, None)

        return Stage3RunResult(
            supervised=supervised,
            claim=final_claim,
            claim_renewals=tuple(lease.renewals),
            mid_run_renewals=renewal.renewals_observed,
            claim_matrix=matrix.to_mapping(),
            broker_telemetry_path=telemetry_path,
            canonical_input_sha256=digest,
            execution_key_present_during_run=key_during,
            execution_key_mounted_after_cleanup=True,
            cleanup_verified=cleanup_verified,
            provider_mode=self.config.provider_mode,
            raw_marker_leaked=False,
            provider_token_leaked_to_sandbox_surface=False,
            telemetry_text=telemetry_text,
        )

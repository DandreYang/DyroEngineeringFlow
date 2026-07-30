from __future__ import annotations

import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile
import zlib

from experiments.external_workflow_runner.docker_cleanup import (
    CLEANUP_LABEL,
    container_absent,
    network_absent,
    remove_and_verify,
)
from experiments.external_workflow_runner.errors import (
    report_cleanup_failures,
    Stage0ValidationError,
)
from experiments.external_workflow_runner.stage1.claim import (
    ClaimLease,
    ClaimRecord,
    ClaimStore,
)
from experiments.external_workflow_runner.stage1.docker_broker import (
    DockerBrokerStack,
)
from experiments.external_workflow_runner.stage2.claim_renewal import ClaimRenewalLoop
from experiments.external_workflow_runner.stage2.docker_broker import (
    Stage2DockerBrokerStack,
)
from experiments.external_workflow_runner.stage3.docker_broker import (
    Stage3DockerBrokerStack,
)
from experiments.external_workflow_runner.stage4.evidence_pack import (
    CleanupProof,
    MAX_EVIDENCE_ARTIFACT_BYTES,
    pack_run_evidence,
)
from experiments.external_workflow_runner.stage5.docker_broker import (
    Stage5DockerBrokerStack,
    _container_absent,
)
from experiments.external_workflow_runner.stage5.evidence_dry_run import (
    dry_run_validate_pack,
)
import experiments.external_workflow_runner.stage5.evidence_dry_run as dry_run_module


def _claim(
    *,
    runner_id: str,
    generation: int,
    execution_key_id: str,
) -> ClaimRecord:
    return ClaimRecord(
        task_id="TASK-1",
        runner_id=runner_id,
        generation=generation,
        execution_key_id=execution_key_id,
        issued_at=100.0,
        expires_at=200.0,
    )


def _forge_zip_central_metadata(
    path: Path,
    *,
    member: str,
    file_size: int,
    crc32: int,
) -> None:
    payload = bytearray(path.read_bytes())
    cursor = 0
    while True:
        offset = payload.find(b"PK\x01\x02", cursor)
        if offset < 0:
            raise AssertionError(f"central directory member not found: {member}")
        name_size, extra_size, comment_size = struct.unpack_from(
            "<HHH",
            payload,
            offset + 28,
        )
        name_start = offset + 46
        name_end = name_start + name_size
        if payload[name_start:name_end].decode("utf-8") == member:
            struct.pack_into("<I", payload, offset + 16, crc32)
            struct.pack_into("<I", payload, offset + 24, file_size)
            path.write_bytes(payload)
            return
        cursor = name_end + extra_size + comment_size


def _forge_zip_eocd_counts(path: Path, *, count: int) -> None:
    payload = bytearray(path.read_bytes())
    offset = payload.rfind(b"PK\x05\x06")
    if offset < 0:
        raise AssertionError("zip end record not found")
    struct.pack_into("<HH", payload, offset + 8, count, count)
    path.write_bytes(payload)


def _forge_zip_central_disk_start(path: Path, *, disk_start: int) -> None:
    payload = bytearray(path.read_bytes())
    offset = payload.find(b"PK\x01\x02")
    if offset < 0:
        raise AssertionError("zip central directory entry not found")
    struct.pack_into("<H", payload, offset + 34, disk_start)
    path.write_bytes(payload)


def _reseal_pack_zip(pack_dir: Path, zip_path: Path) -> None:
    seal_path = pack_dir / "seal.json"
    seal = json.loads(seal_path.read_text(encoding="utf-8"))
    seal["pack_sha256"] = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    seal_path.write_text(
        json.dumps(seal, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


class ClaimCasTests(unittest.TestCase):
    def test_stale_renewal_cannot_overwrite_reassigned_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ClaimStore(Path(tmp) / "claim.json")
            stale = _claim(runner_id="runner-a", generation=1, execution_key_id="key-a")
            store.write(stale)
            lease = ClaimLease(record=stale, renewals=[])
            loop = ClaimRenewalLoop(
                lease=lease,
                store=store,
                runner_id="runner-a",
                extend_seconds=100.0,
                interval_seconds=0.01,
            )

            reassigned = _claim(
                runner_id="runner-b",
                generation=99,
                execution_key_id="key-b",
            )
            store.write(reassigned)

            with self.assertRaisesRegex(Stage0ValidationError, "claim changed"):
                loop.renew_once(now=150.0)
            self.assertEqual(store.read(), reassigned)
            self.assertEqual(lease.record, stale)

    def test_claim_reader_rejects_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target_store = ClaimStore(root / "target.json")
            target_store.write(
                _claim(runner_id="runner-a", generation=1, execution_key_id="key-a")
            )
            link = root / "claim.json"
            link.symlink_to(target_store.path)

            with self.assertRaisesRegex(Stage0ValidationError, "unreadable"):
                ClaimStore(link).read()

    def test_claim_store_rejects_non_finite_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ClaimStore(Path(tmp) / "claim.json")
            invalid = ClaimRecord(
                task_id="TASK-1",
                runner_id="runner-a",
                generation=1,
                execution_key_id="key-a",
                issued_at=float("nan"),
                expires_at=200.0,
            )
            with self.assertRaisesRegex(Stage0ValidationError, "timestamps"):
                store.write(invalid)


class CleanupFailureReportingTests(unittest.TestCase):
    def test_primary_failure_remains_propagated_with_cleanup_note(self) -> None:
        primary = RuntimeError("primary execution failure")
        with self.assertRaises(RuntimeError) as raised:
            try:
                raise primary
            finally:
                report_cleanup_failures(
                    "Stage 4",
                    ["broker stop: cleanup verification failed"],
                    primary_error=sys.exception(),
                )

        self.assertIs(raised.exception, primary)
        self.assertIn(
            "Stage 4 cleanup/invariant verification failed",
            "\n".join(raised.exception.__notes__),
        )

    def test_cleanup_failure_raises_when_there_is_no_primary_failure(self) -> None:
        with self.assertRaisesRegex(
            Stage0ValidationError,
            "Stage 4 cleanup/invariant verification failed",
        ):
            report_cleanup_failures(
                "Stage 4",
                ["broker stop: cleanup verification failed"],
                primary_error=None,
            )


class CleanupProofTests(unittest.TestCase):
    def test_cleanup_settle_window_retries_late_resources(self) -> None:
        owner_token = "a" * 32
        successful = subprocess.CompletedProcess(
            args=["docker"],
            returncode=0,
            stdout="resource-id",
            stderr="",
        )
        missing_container = subprocess.CompletedProcess(
            args=["docker"],
            returncode=1,
            stdout="",
            stderr="Error: No such container",
        )
        missing_network = subprocess.CompletedProcess(
            args=["docker"],
            returncode=1,
            stdout="",
            stderr="Error: No such network",
        )
        inspection_round = 0
        removed_ids: list[str] = []

        def delayed_cleanup(
            argv: list[str],
            *,
            timeout: float,
        ) -> subprocess.CompletedProcess[str]:
            nonlocal inspection_round
            if argv[:2] == ["docker", "inspect"]:
                inspection_format = argv[argv.index("--format") + 1]
                if CLEANUP_LABEL in inspection_format:
                    if inspection_round == 0:
                        return missing_container
                    return subprocess.CompletedProcess(
                        args=argv,
                        returncode=0,
                        stdout=f"container-id|{owner_token}\n",
                        stderr="",
                    )
                return (
                    successful
                    if inspection_round == 0
                    else missing_container
                )
            if argv[:3] == ["docker", "network", "inspect"]:
                if "--format" in argv:
                    if inspection_round == 0:
                        return missing_network
                    return subprocess.CompletedProcess(
                        args=argv,
                        returncode=0,
                        stdout=f"network-id|{owner_token}\n",
                        stderr="",
                    )
                result = (
                    successful
                    if inspection_round == 0
                    else missing_network
                )
                if argv[-1] == "dyro-net-late":
                    inspection_round += 1
                return result
            if argv[:2] == ["docker", "rm"]:
                removed_ids.append(argv[-1])
            if argv[:3] == ["docker", "network", "rm"]:
                removed_ids.append(argv[-1])
            return successful

        with (
            patch(
                "experiments.external_workflow_runner.docker_cleanup."
                "time.monotonic",
                side_effect=[0.0, 0.0, 1.0],
            ),
            patch(
                "experiments.external_workflow_runner.docker_cleanup.time.sleep"
            ) as sleep,
        ):
            proof = remove_and_verify(
                run_docker=delayed_cleanup,
                container_names=("dyro-ns-late", "dyro-broker-late"),
                network_name="dyro-net-late",
                owner_token=owner_token,
                settle_seconds=1.0,
                retry_interval_seconds=1.0,
            )

        self.assertTrue(proof.containers_absent)
        self.assertTrue(proof.network_absent)
        self.assertEqual(inspection_round, 2)
        self.assertEqual(
            removed_ids,
            ["container-id", "container-id", "network-id"],
        )
        sleep.assert_called_once_with(1.0)

    def test_cleanup_refuses_foreign_resources_without_removing_them(
        self,
    ) -> None:
        owner_token = "a" * 32
        foreign_token = "b" * 32
        calls: list[list[str]] = []

        def foreign_resources(
            argv: list[str],
            *,
            timeout: float,
        ) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            if argv[:2] == ["docker", "inspect"]:
                inspection_format = argv[argv.index("--format") + 1]
                if CLEANUP_LABEL in inspection_format:
                    return subprocess.CompletedProcess(
                        args=argv,
                        returncode=0,
                        stdout=f"foreign-container|{foreign_token}\n",
                        stderr="",
                    )
                return subprocess.CompletedProcess(
                    args=argv,
                    returncode=0,
                    stdout="foreign-container\n",
                    stderr="",
                )
            if argv[:3] == ["docker", "network", "inspect"]:
                if "--format" in argv:
                    return subprocess.CompletedProcess(
                        args=argv,
                        returncode=0,
                        stdout=f"foreign-network|{foreign_token}\n",
                        stderr="",
                    )
                return subprocess.CompletedProcess(
                    args=argv,
                    returncode=0,
                    stdout="[]\n",
                    stderr="",
                )
            raise AssertionError(f"unexpected destructive call: {argv}")

        with self.assertRaisesRegex(
            Stage0ValidationError,
            "owned by another run",
        ):
            remove_and_verify(
                run_docker=foreign_resources,
                container_names=("dyro-ns-collision",),
                network_name="dyro-net-collision",
                owner_token=owner_token,
            )

        self.assertFalse(
            any(
                argv[:2] == ["docker", "rm"]
                or argv[:3] == ["docker", "network", "rm"]
                for argv in calls
            )
        )

    def test_cleanup_proves_observed_id_absent_after_name_disappears(
        self,
    ) -> None:
        owner_token = "a" * 32
        missing_container = subprocess.CompletedProcess(
            args=["docker"],
            returncode=1,
            stdout="",
            stderr="Error: No such container",
        )
        missing_network = subprocess.CompletedProcess(
            args=["docker"],
            returncode=1,
            stdout="",
            stderr="network dyro-net not found",
        )
        existing = subprocess.CompletedProcess(
            args=["docker"],
            returncode=0,
            stdout="resource-id\n",
            stderr="",
        )

        def renamed_resource(
            argv: list[str],
            *,
            timeout: float,
        ) -> subprocess.CompletedProcess[str]:
            del timeout
            if argv[:2] == ["docker", "inspect"]:
                if CLEANUP_LABEL in argv[argv.index("--format") + 1]:
                    return subprocess.CompletedProcess(
                        args=argv,
                        returncode=0,
                        stdout=f"owned-id|{owner_token}\n",
                        stderr="",
                    )
                if argv[-1] == "owned-id":
                    return existing
                return missing_container
            if argv[:3] == ["docker", "network", "inspect"]:
                return missing_network
            return subprocess.CompletedProcess(
                args=argv,
                returncode=0,
                stdout="",
                stderr="",
            )

        with self.assertRaisesRegex(
            Stage0ValidationError,
            "containers remain",
        ):
            remove_and_verify(
                run_docker=renamed_resource,
                container_names=("dyro-renamed",),
                network_name="dyro-net",
                owner_token=owner_token,
            )

    def test_docker_daemon_failure_is_not_container_absence(self) -> None:
        failed = subprocess.CompletedProcess(
            args=["docker", "inspect"],
            returncode=125,
            stdout="",
            stderr="Cannot connect to the Docker daemon",
        )
        with patch(
            "experiments.external_workflow_runner.stage5.docker_broker._run_docker",
            return_value=failed,
        ):
            with self.assertRaisesRegex(Stage0ValidationError, "inspect failed"):
                _container_absent("dyro-broker")

    def test_container_absence_inspection_is_type_scoped(self) -> None:
        missing = subprocess.CompletedProcess(
            args=["docker", "inspect"],
            returncode=1,
            stdout="",
            stderr="Error: No such container: dyro-broker",
        )
        runner = Mock(return_value=missing)
        self.assertTrue(container_absent(run_docker=runner, name="dyro-broker"))
        self.assertEqual(
            runner.call_args.args[0],
            [
                "docker",
                "inspect",
                "--type",
                "container",
                "--format",
                "{{.Id}}",
                "dyro-broker",
            ],
        )

    def test_generic_network_not_found_is_not_absence_proof(self) -> None:
        ambiguous = subprocess.CompletedProcess(
            args=["docker", "network", "inspect"],
            returncode=1,
            stdout="",
            stderr="request failed: endpoint not found",
        )
        with self.assertRaisesRegex(Stage0ValidationError, "inspect failed"):
            network_absent(
                run_docker=Mock(return_value=ambiguous),
                name="dyro-net",
            )

    def test_named_network_not_found_is_absence_proof(self) -> None:
        missing = subprocess.CompletedProcess(
            args=["docker", "network", "inspect"],
            returncode=1,
            stdout="[]",
            stderr=(
                "Error response from daemon: network dyro-net-123 not found"
            ),
        )
        self.assertTrue(
            network_absent(
                run_docker=Mock(return_value=missing),
                name="dyro-net-123",
            )
        )

    def test_readiness_failure_attempts_stack_cleanup(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["docker"],
            returncode=0,
            stdout="",
            stderr="",
        )
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch(
                "experiments.external_workflow_runner.stage5.docker_broker._run_docker",
                return_value=completed,
            ),
            patch.object(
                Stage5DockerBrokerStack,
                "_wait_until_ready",
                side_effect=Stage0ValidationError("not ready"),
            ),
            patch.object(
                Stage5DockerBrokerStack,
                "stop",
                autospec=True,
            ) as stop,
        ):
            bundle = Path(tmp)
            provider = bundle / "provider"
            provider.write_text("#!/bin/sh\n", encoding="utf-8")
            from experiments.external_workflow_runner.stage5.host_provider import (
                pin_host_provider,
            )

            host_provider = pin_host_provider(
                provider,
                allowed_roots=(bundle,),
            )
            with self.assertRaisesRegex(Stage0ValidationError, "not ready"):
                Stage5DockerBrokerStack.start(
                    bundle_root=bundle,
                    telemetry_host_path=bundle / "telemetry.jsonl",
                    host_provider=host_provider,
                    provider_mode="fake",
                    model="test",
                    max_concurrency=1,
                    provider_fake_token="fake",
                )
            stop.assert_called_once()

    def test_stage1_through_stage3_start_failures_preserve_cleanup_errors(
        self,
    ) -> None:
        succeeded = subprocess.CompletedProcess(
            args=["docker"],
            returncode=0,
            stdout="resource-id",
            stderr="",
        )
        failure_cases = (
            (
                "internal network",
                [
                    subprocess.CompletedProcess(
                        args=["docker"],
                        returncode=125,
                        stdout="",
                        stderr="network startup failed",
                    ),
                ],
                "network startup failed",
            ),
            (
                "network namespace",
                [
                    succeeded,
                    subprocess.CompletedProcess(
                        args=["docker"],
                        returncode=125,
                        stdout="",
                        stderr="namespace startup failed",
                    ),
                ],
                "namespace startup failed",
            ),
            (
                "broker container",
                [
                    succeeded,
                    succeeded,
                    subprocess.CompletedProcess(
                        args=["docker"],
                        returncode=125,
                        stdout="",
                        stderr="broker startup failed",
                    ),
                ],
                "broker startup failed",
            ),
        )
        stages = (
            (
                "stage1",
                "experiments.external_workflow_runner.stage1.docker_broker",
                DockerBrokerStack,
            ),
            (
                "stage2",
                "experiments.external_workflow_runner.stage2.docker_broker",
                Stage2DockerBrokerStack,
            ),
            (
                "stage3",
                "experiments.external_workflow_runner.stage3.docker_broker",
                Stage3DockerBrokerStack,
            ),
        )
        for stage, module, stack_type in stages:
            for label, docker_results, primary_error in failure_cases:
                with (
                    self.subTest(stage=stage, step=label),
                    tempfile.TemporaryDirectory() as tmp,
                    patch(
                        f"{module}._run_docker",
                        side_effect=list(docker_results),
                    ),
                    patch(
                        f"{module}.remove_and_verify",
                        side_effect=Stage0ValidationError(
                            "cleanup verification failed"
                        ),
                    ),
                ):
                    root = Path(tmp)
                    with self.assertRaises(Stage0ValidationError) as raised:
                        stack_type.start(
                            bundle_root=root,
                            telemetry_host_path=root / "telemetry.jsonl",
                            model="test",
                        )

                message = str(raised.exception)
                self.assertIn(primary_error, message)
                self.assertIn("cleanup verification failed", message)

    def test_stage1_through_stage3_start_exceptions_use_settled_cleanup(
        self,
    ) -> None:
        succeeded = subprocess.CompletedProcess(
            args=["docker"],
            returncode=0,
            stdout="resource-id",
            stderr="",
        )
        stages = (
            (
                "stage1",
                "experiments.external_workflow_runner.stage1.docker_broker",
                DockerBrokerStack,
            ),
            (
                "stage2",
                "experiments.external_workflow_runner.stage2.docker_broker",
                Stage2DockerBrokerStack,
            ),
            (
                "stage3",
                "experiments.external_workflow_runner.stage3.docker_broker",
                Stage3DockerBrokerStack,
            ),
        )
        for stage, module, stack_type in stages:
            for step, successful_steps in (
                ("internal network", 0),
                ("network namespace", 1),
                ("broker container", 2),
            ):
                with (
                    self.subTest(stage=stage, step=step),
                    tempfile.TemporaryDirectory() as tmp,
                    patch(
                        f"{module}._run_docker",
                        side_effect=[
                            *([succeeded] * successful_steps),
                            Stage0ValidationError("daemon timed out"),
                        ],
                    ) as run_docker,
                    patch(f"{module}.remove_and_verify") as cleanup,
                ):
                    root = Path(tmp)
                    with self.assertRaisesRegex(
                        Stage0ValidationError,
                        "daemon timed out",
                    ):
                        stack_type.start(
                            bundle_root=root,
                            telemetry_host_path=root / "telemetry.jsonl",
                            model="test",
                        )

                cleanup.assert_called_once()
                labels = {
                    call.args[0][call.args[0].index("--label") + 1]
                    for call in run_docker.call_args_list
                }
                self.assertEqual(len(labels), 1)
                label = labels.pop()
                self.assertTrue(label.startswith(f"{CLEANUP_LABEL}="))
                self.assertEqual(
                    cleanup.call_args.kwargs["owner_token"],
                    label.split("=", 1)[1],
                )
                self.assertGreater(
                    cleanup.call_args.kwargs["settle_seconds"],
                    0,
                )

    def test_stage1_through_stage3_name_collision_preserves_foreign_owner(
        self,
    ) -> None:
        foreign_token = "b" * 32
        stages = (
            (
                "stage1",
                "experiments.external_workflow_runner.stage1.docker_broker",
                DockerBrokerStack,
            ),
            (
                "stage2",
                "experiments.external_workflow_runner.stage2.docker_broker",
                Stage2DockerBrokerStack,
            ),
            (
                "stage3",
                "experiments.external_workflow_runner.stage3.docker_broker",
                Stage3DockerBrokerStack,
            ),
        )
        for stage, module, stack_type in stages:
            destructive_calls: list[list[str]] = []

            def collision(
                argv: list[str],
                *,
                timeout: float = 30.0,
            ) -> subprocess.CompletedProcess[str]:
                if argv[:3] == ["docker", "network", "create"]:
                    return subprocess.CompletedProcess(
                        args=argv,
                        returncode=1,
                        stdout="",
                        stderr="network already exists",
                    )
                if argv[:2] == ["docker", "inspect"]:
                    return subprocess.CompletedProcess(
                        args=argv,
                        returncode=1,
                        stdout="",
                        stderr="Error: No such container",
                    )
                if argv[:3] == ["docker", "network", "inspect"]:
                    if "--format" in argv:
                        return subprocess.CompletedProcess(
                            args=argv,
                            returncode=0,
                            stdout=f"foreign-network|{foreign_token}\n",
                            stderr="",
                        )
                    return subprocess.CompletedProcess(
                        args=argv,
                        returncode=0,
                        stdout="[]\n",
                        stderr="",
                    )
                destructive_calls.append(argv)
                raise AssertionError(f"unexpected destructive call: {argv}")

            with (
                self.subTest(stage=stage),
                tempfile.TemporaryDirectory() as tmp,
                patch(f"{module}._run_docker", side_effect=collision),
                patch(f"{module}.PARTIAL_START_SETTLE_SECONDS", 0.0),
            ):
                root = Path(tmp)
                with self.assertRaises(Stage0ValidationError) as raised:
                    stack_type.start(
                        bundle_root=root,
                        telemetry_host_path=root / "telemetry.jsonl",
                        model="test",
                    )

            message = str(raised.exception)
            self.assertIn("network already exists", message)
            self.assertIn("owned by another run", message)
            self.assertEqual(destructive_calls, [])

    def test_stage3_start_failure_preserves_primary_and_cleanup_errors(self) -> None:
        succeeded = subprocess.CompletedProcess(
            args=["docker"],
            returncode=0,
            stdout="resource-id",
            stderr="",
        )
        cases = (
            (
                "network namespace",
                [
                    succeeded,
                    subprocess.CompletedProcess(
                        args=["docker"],
                        returncode=125,
                        stdout="",
                        stderr="namespace startup failed",
                    ),
                ],
                "namespace startup failed",
            ),
            (
                "broker container",
                [
                    succeeded,
                    succeeded,
                    subprocess.CompletedProcess(
                        args=["docker"],
                        returncode=125,
                        stdout="",
                        stderr="broker startup failed",
                    ),
                ],
                "broker startup failed",
            ),
        )
        for label, docker_results, primary_error in cases:
            with (
                self.subTest(step=label),
                tempfile.TemporaryDirectory() as tmp,
                patch(
                    "experiments.external_workflow_runner.stage3."
                    "docker_broker._run_docker",
                    side_effect=docker_results,
                ),
                patch(
                    "experiments.external_workflow_runner.stage3."
                    "docker_broker.remove_and_verify",
                    side_effect=Stage0ValidationError(
                        "cleanup verification failed"
                    ),
                ),
                self.assertRaises(Stage0ValidationError) as raised,
            ):
                root = Path(tmp)
                Stage3DockerBrokerStack.start(
                    bundle_root=root,
                    telemetry_host_path=root / "telemetry.jsonl",
                    model="test",
                    provider_mode="fake",
                    max_concurrency=1,
                )

            message = str(raised.exception)
            self.assertIn(primary_error, message)
            self.assertIn("cleanup verification failed", message)


class EvidenceBindingTests(unittest.TestCase):
    def _build_pack(self, root: Path):
        artifact = root / "report.md"
        data = b"# verified report\n"
        artifact.write_bytes(data)
        return pack_run_evidence(
            pack_root=root / "pack",
            workflow_run_id="run-1",
            claim={
                "schema_version": 1,
                "task_id": "TASK-1",
                "runner_id": "runner-1",
                "generation": 1,
                "execution_key_id": "runner-key-1",
                "issued_at": 1.0,
                "expires_at": 4_102_444_800.0,
            },
            canonical_input_sha256="a" * 64,
            envelope={
                "schema_version": 1,
                "status": "DONE",
                "workflow_run_id": "run-1",
                "artifacts": [
                    {
                        "repository": "repo",
                        "path": "report.md",
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                ],
            },
            artifact_paths=(("repo", "report.md", artifact),),
            provider_pin={},
            claim_matrix={},
            cleanup=CleanupProof(
                sandbox_cleanup_verified=True,
                broker_cleanup_verified=True,
                broker_containers_absent=True,
                raw_marker_leaked=False,
                provider_token_leaked=False,
            ),
            mid_run_renewals=0,
            provider_mode="fake",
        )

    def test_pack_rejects_artifact_changed_after_envelope_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = root / "report.md"
            original = b"# verified report\n"
            artifact.write_bytes(original)
            envelope = {
                "schema_version": 1,
                "status": "DONE",
                "workflow_run_id": "run-1",
                "artifacts": [
                    {
                        "repository": "repo",
                        "path": "report.md",
                        "sha256": hashlib.sha256(original).hexdigest(),
                    }
                ],
            }
            artifact.write_text("# replaced report\n", encoding="utf-8")

            with self.assertRaisesRegex(Stage0ValidationError, "envelope"):
                pack_run_evidence(
                    pack_root=root / "pack",
                    workflow_run_id="run-1",
                    claim={"task_id": "TASK-1"},
                    canonical_input_sha256="a" * 64,
                    envelope=envelope,
                    artifact_paths=(("repo", "report.md", artifact),),
                    provider_pin={},
                    claim_matrix={},
                    cleanup=CleanupProof(
                        sandbox_cleanup_verified=True,
                        broker_cleanup_verified=True,
                        broker_containers_absent=True,
                        raw_marker_leaked=False,
                        provider_token_leaked=False,
                    ),
                    mid_run_renewals=0,
                    provider_mode="fake",
                )
            self.assertFalse((root / "pack").exists())

    def test_pack_rejects_oversized_artifact_without_partial_pack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = root / "oversized.bin"
            with artifact.open("wb") as stream:
                stream.truncate(MAX_EVIDENCE_ARTIFACT_BYTES + 1)

            with self.assertRaisesRegex(Stage0ValidationError, "byte limit"):
                pack_run_evidence(
                    pack_root=root / "pack",
                    workflow_run_id="run-1",
                    claim={"task_id": "TASK-1"},
                    canonical_input_sha256="a" * 64,
                    envelope={
                        "schema_version": 1,
                        "status": "DONE",
                        "workflow_run_id": "run-1",
                        "artifacts": [
                            {
                                "repository": "repo",
                                "path": "oversized.bin",
                                "sha256": "b" * 64,
                            }
                        ],
                    },
                    artifact_paths=(("repo", "oversized.bin", artifact),),
                    provider_pin={},
                    claim_matrix={},
                    cleanup=CleanupProof(
                        sandbox_cleanup_verified=True,
                        broker_cleanup_verified=True,
                        broker_containers_absent=True,
                        raw_marker_leaked=False,
                        provider_token_leaked=False,
                    ),
                    mid_run_renewals=0,
                    provider_mode="fake",
                )
            self.assertFalse((root / "pack").exists())
            self.assertEqual(list(root.glob(".pack.*")), [])

    def test_dry_run_counts_actual_output_when_zip_size_is_underreported(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pack = self._build_pack(Path(tmp))
            with zipfile.ZipFile(
                pack.zip_path,
                "a",
                compression=zipfile.ZIP_DEFLATED,
            ) as archive:
                with archive.open("bomb.bin", "w") as member:
                    for _ in range(32):
                        member.write(b"\0" * (64 * 1024))

            _forge_zip_central_metadata(
                pack.zip_path,
                member="bomb.bin",
                file_size=1,
                crc32=zlib.crc32(b"\0"),
            )
            _reseal_pack_zip(pack.pack_dir, pack.zip_path)

            with (
                patch.object(
                    dry_run_module,
                    "MAX_PACK_UNCOMPRESSED_BYTES",
                    1024 * 1024,
                ),
                self.assertRaisesRegex(
                    Stage0ValidationError,
                    "uncompressed byte limit",
                ),
            ):
                dry_run_validate_pack(pack.pack_dir)

    def test_dry_run_rejects_actual_member_overflow_before_zipfile_parse(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pack = self._build_pack(Path(tmp))
            with zipfile.ZipFile(
                pack.zip_path,
                "w",
                compression=zipfile.ZIP_STORED,
            ) as archive:
                for index in range(dry_run_module.MAX_PACK_FILES + 1):
                    archive.writestr(f"empty-{index}.txt", b"")
            _forge_zip_eocd_counts(pack.zip_path, count=1)
            _reseal_pack_zip(pack.pack_dir, pack.zip_path)

            with (
                patch.object(
                    dry_run_module.zipfile,
                    "ZipFile",
                    side_effect=AssertionError(
                        "ZipFile must not run before member-count rejection"
                    ),
                ),
                self.assertRaisesRegex(
                    Stage0ValidationError,
                    "too many members",
                ),
            ):
                dry_run_validate_pack(pack.pack_dir)

    def test_dry_run_rejects_large_central_directory_before_zipfile_parse(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pack = self._build_pack(Path(tmp))
            with zipfile.ZipFile(
                pack.zip_path,
                "w",
                compression=zipfile.ZIP_STORED,
            ) as archive:
                for index in range(17):
                    info = zipfile.ZipInfo(f"empty-{index}.txt")
                    info.comment = b"x" * 65535
                    archive.writestr(info, b"")
            _reseal_pack_zip(pack.pack_dir, pack.zip_path)

            with (
                patch.object(
                    dry_run_module.zipfile,
                    "ZipFile",
                    side_effect=AssertionError(
                        "ZipFile must not run before directory-size rejection"
                    ),
                ),
                self.assertRaisesRegex(
                    Stage0ValidationError,
                    "central directory exceeds byte limit",
                ),
            ):
                dry_run_validate_pack(pack.pack_dir)

    def test_dry_run_validates_one_snapshot_when_source_zip_is_replaced(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pack = self._build_pack(root)
            original_sha = hashlib.sha256(
                pack.zip_path.read_bytes()
            ).hexdigest()
            replacement = root / "replacement.zip"
            with zipfile.ZipFile(pack.zip_path, "r") as source:
                members = [
                    (info.filename, source.read(info.filename))
                    for info in source.infolist()
                    if not info.is_dir()
                ]
            with zipfile.ZipFile(
                replacement,
                "w",
                compression=zipfile.ZIP_STORED,
            ) as target:
                target.comment = b"different archive, same members"
                for name, data in members:
                    target.writestr(name, data)
            replacement_sha = hashlib.sha256(
                replacement.read_bytes()
            ).hexdigest()
            self.assertNotEqual(original_sha, replacement_sha)

            real_preflight = dry_run_module._preflight_zip_directory

            def replace_source_after_preflight(path: Path) -> int:
                self.assertNotEqual(Path(path), pack.zip_path)
                entry_count = real_preflight(path)
                replacement.replace(pack.zip_path)
                return entry_count

            with patch.object(
                dry_run_module,
                "_preflight_zip_directory",
                side_effect=replace_source_after_preflight,
            ):
                result = dry_run_validate_pack(pack.pack_dir)

            self.assertEqual(
                result.candidate_record["pack_sha256"],
                original_sha,
            )
            self.assertEqual(
                hashlib.sha256(pack.zip_path.read_bytes()).hexdigest(),
                replacement_sha,
            )

    def test_dry_run_rejects_nonzero_central_entry_disk_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pack = self._build_pack(Path(tmp))
            _forge_zip_central_disk_start(
                pack.zip_path,
                disk_start=1,
            )
            _reseal_pack_zip(pack.pack_dir, pack.zip_path)

            with self.assertRaisesRegex(
                Stage0ValidationError,
                "one complete disk",
            ):
                dry_run_validate_pack(pack.pack_dir)

    def test_dry_run_rejects_manifest_changed_after_pack_seal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pack = self._build_pack(Path(tmp))
            manifest = json.loads(pack.manifest_path.read_text(encoding="utf-8"))
            manifest["provider_mode"] = "tampered"
            pack.manifest_path.write_text(
                json.dumps(manifest, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(Stage0ValidationError, "manifest sha256"):
                dry_run_validate_pack(pack.pack_dir)

    def test_dry_run_rejects_unsafe_artifact_storage_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pack = self._build_pack(Path(tmp))
            manifest = json.loads(pack.manifest_path.read_text(encoding="utf-8"))
            manifest["artifacts"][0]["stored_name"] = "../../outside"
            pack.manifest_path.write_text(
                json.dumps(
                    manifest,
                    indent=2,
                    sort_keys=True,
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            zip_path = pack.pack_dir / "evidence-pack.zip"
            with zipfile.ZipFile(zip_path, "r") as source:
                members = {
                    info.filename: source.read(info.filename)
                    for info in source.infolist()
                    if not info.is_dir()
                }
            members["pack-manifest.json"] = pack.manifest_path.read_bytes()
            with zipfile.ZipFile(
                zip_path,
                "w",
                compression=zipfile.ZIP_DEFLATED,
            ) as target:
                for name, data in members.items():
                    target.writestr(name, data)
            seal_path = pack.pack_dir / "seal.json"
            seal = json.loads(seal_path.read_text(encoding="utf-8"))
            seal["pack_manifest_sha256"] = hashlib.sha256(
                pack.manifest_path.read_bytes()
            ).hexdigest()
            seal["pack_sha256"] = hashlib.sha256(zip_path.read_bytes()).hexdigest()
            seal_path.write_text(
                json.dumps(seal, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(Stage0ValidationError, "stored_name is unsafe"):
                dry_run_validate_pack(pack.pack_dir)


if __name__ == "__main__":
    unittest.main()

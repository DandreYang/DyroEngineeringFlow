from __future__ import annotations

from contextlib import ExitStack
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from experiments.external_workflow_runner.artifacts import ArtifactPolicy
from experiments.external_workflow_runner.errors import Stage0ValidationError
from experiments.external_workflow_runner.stage1.canonical import CanonicalInput
from experiments.external_workflow_runner.stage1.claim import ClaimRecord, ClaimStore
from experiments.external_workflow_runner.stage2.bundle import assemble_stage2_bundle
from experiments.external_workflow_runner.stage2.supervisor import (
    Stage2Supervisor,
    Stage2SupervisorConfig,
)
from experiments.external_workflow_runner.stage3.bundle import assemble_stage3_bundle
from experiments.external_workflow_runner.stage3.claim_matrix import (
    ClaimDeadlineMatrix,
)
from experiments.external_workflow_runner.stage3.supervisor import (
    Stage3Supervisor,
    Stage3SupervisorConfig,
)
from experiments.external_workflow_runner.stage4.bundle import assemble_stage4_bundle
from experiments.external_workflow_runner.stage4.supervisor import (
    Stage4Supervisor,
    Stage4SupervisorConfig,
)
from experiments.external_workflow_runner.stage5.bundle import assemble_stage5_bundle
from experiments.external_workflow_runner.stage5.host_provider import (
    pin_host_provider,
    write_host_fixture_cli,
)
from experiments.external_workflow_runner.stage5.supervisor import (
    Stage5Supervisor,
    Stage5SupervisorConfig,
)


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_LOCK = ROOT / "experiments/external_workflow_runner/runtime-lock.json"


class StartupAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ.pop("DYRO_EXECUTION_KEY", None)

    def tearDown(self) -> None:
        os.environ.pop("DYRO_EXECUTION_KEY", None)

    def _stage_paths(
        self,
        root: Path,
        stage: int,
    ) -> tuple[Path, Path, Path, Path, Path]:
        stage_root = root / f"stage{stage}"
        worktree = stage_root / "worktrees" / "docs"
        run_root = stage_root / "run"
        ipc_root = stage_root / "ipc"
        claim_path = stage_root / "claim.json"
        worktree.mkdir(parents=True)
        run_root.mkdir()
        ipc_root.mkdir()
        return stage_root, worktree, run_root, ipc_root, claim_path

    def _claim_and_contract(
        self,
        *,
        claim_path: Path,
        worktree: Path,
        stage: int,
    ) -> tuple[ClaimRecord, CanonicalInput, ArtifactPolicy]:
        runner_id = f"stage{stage}-runner"
        record = ClaimRecord(
            task_id=f"task-stage{stage}",
            runner_id=runner_id,
            generation=1,
            execution_key_id=f"exec-key-stage{stage}",
            issued_at=time.time(),
            expires_at=time.time() + 60.0,
        )
        ClaimStore(claim_path).write(record)
        canonical = CanonicalInput(
            workflow_run_id=f"run-stage{stage}-startup",
            task_id=record.task_id,
            runner_id=record.runner_id,
            claim_generation=record.generation,
            branches=("analysis-a", "analysis-b"),
            artifact_repository="docs",
            artifact_path="report.md",
            model="fake-model",
            max_agent_calls=4,
        )
        policy = ArtifactPolicy(
            repository_roots={"docs": worktree},
            allowed_paths={("docs", "report.md")},
            max_artifacts=4,
            max_artifact_bytes=64 * 1024,
        )
        return record, canonical, policy

    def _build_cases(
        self,
        root: Path,
    ) -> list[tuple[str, str, str, object, ClaimRecord]]:
        cases: list[tuple[str, str, str, object, ClaimRecord]] = []
        matrix = ClaimDeadlineMatrix()

        stage_root, worktree, run_root, ipc_root, claim_path = self._stage_paths(
            root,
            2,
        )
        record, canonical, policy = self._claim_and_contract(
            claim_path=claim_path,
            worktree=worktree,
            stage=2,
        )
        assembled = assemble_stage2_bundle(
            stage_root / "bundle",
            runtime_lock_path=RUNTIME_LOCK,
        )
        cases.append(
            (
                "stage2",
                "experiments.external_workflow_runner.stage2.supervisor",
                "Stage2DockerBrokerStack",
                Stage2Supervisor(
                    Stage2SupervisorConfig(
                        bundle_root=assembled["bundle_root"],
                        bundle_manifest=assembled["manifest"],
                        bundle_identity=assembled["identity"],
                        run_root=run_root,
                        worktrees={"docs": worktree},
                        ipc_root=ipc_root,
                        claim_path=claim_path,
                        canonical_input=canonical,
                        artifact_policy=policy,
                    )
                ),
                record,
            )
        )

        stage_root, worktree, run_root, ipc_root, claim_path = self._stage_paths(
            root,
            3,
        )
        record, canonical, policy = self._claim_and_contract(
            claim_path=claim_path,
            worktree=worktree,
            stage=3,
        )
        assembled = assemble_stage3_bundle(
            stage_root / "bundle",
            runtime_lock_path=RUNTIME_LOCK,
        )
        cases.append(
            (
                "stage3",
                "experiments.external_workflow_runner.stage3.supervisor",
                "Stage3DockerBrokerStack",
                Stage3Supervisor(
                    Stage3SupervisorConfig(
                        bundle_root=assembled["bundle_root"],
                        bundle_manifest=assembled["manifest"],
                        bundle_identity=assembled["identity"],
                        run_root=run_root,
                        worktrees={"docs": worktree},
                        ipc_root=ipc_root,
                        claim_path=claim_path,
                        canonical_input=canonical,
                        artifact_policy=policy,
                        claim_matrix=matrix,
                    )
                ),
                record,
            )
        )

        stage_root, worktree, run_root, ipc_root, claim_path = self._stage_paths(
            root,
            4,
        )
        record, canonical, policy = self._claim_and_contract(
            claim_path=claim_path,
            worktree=worktree,
            stage=4,
        )
        assembled = assemble_stage4_bundle(
            stage_root / "bundle",
            runtime_lock_path=RUNTIME_LOCK,
        )
        cases.append(
            (
                "stage4",
                "experiments.external_workflow_runner.stage4.supervisor",
                "Stage4DockerBrokerStack",
                Stage4Supervisor(
                    Stage4SupervisorConfig(
                        bundle_root=assembled["bundle_root"],
                        bundle_manifest=assembled["manifest"],
                        bundle_identity=assembled["identity"],
                        run_root=run_root,
                        worktrees={"docs": worktree},
                        ipc_root=ipc_root,
                        claim_path=claim_path,
                        pack_root=stage_root / "pack",
                        canonical_input=canonical,
                        artifact_policy=policy,
                        claim_matrix=matrix,
                        provider_pin=assembled["provider_pin"],
                    )
                ),
                record,
            )
        )

        stage_root, worktree, run_root, ipc_root, claim_path = self._stage_paths(
            root,
            5,
        )
        record, canonical, policy = self._claim_and_contract(
            claim_path=claim_path,
            worktree=worktree,
            stage=5,
        )
        host_provider = pin_host_provider(
            write_host_fixture_cli(stage_root / "host" / "provider_cli.ts"),
            allowed_roots=(stage_root,),
        )
        assembled = assemble_stage5_bundle(
            stage_root / "bundle",
            runtime_lock_path=RUNTIME_LOCK,
            host_provider=host_provider,
        )
        cases.append(
            (
                "stage5",
                "experiments.external_workflow_runner.stage5.supervisor",
                "Stage5DockerBrokerStack",
                Stage5Supervisor(
                    Stage5SupervisorConfig(
                        bundle_root=assembled["bundle_root"],
                        bundle_manifest=assembled["manifest"],
                        bundle_identity=assembled["identity"],
                        run_root=run_root,
                        worktrees={"docs": worktree},
                        ipc_root=ipc_root,
                        claim_path=claim_path,
                        pack_root=stage_root / "pack",
                        host_provider=host_provider,
                        canonical_input=canonical,
                        artifact_policy=policy,
                        claim_matrix=matrix,
                    )
                ),
                record,
            )
        )
        return cases

    def test_renewal_precedes_broker_and_stops_on_startup_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for stage, module, broker_type, supervisor, record in self._build_cases(
                Path(tmp)
            ):
                events: list[str] = []

                def renewal_start(_loop: object) -> None:
                    events.append("renewal.start")

                def broker_start(*_args: object, **_kwargs: object) -> None:
                    events.append("broker.start")
                    raise Stage0ValidationError("broker startup failed")

                def renewal_stop(_loop: object) -> None:
                    events.append("renewal.stop")

                with self.subTest(stage=stage):
                    with ExitStack() as stack:
                        stack.enter_context(
                            patch(
                                f"{module}.ClaimRenewalLoop.start",
                                autospec=True,
                                side_effect=renewal_start,
                            )
                        )
                        stack.enter_context(
                            patch(
                                f"{module}.{broker_type}.start",
                                side_effect=broker_start,
                            )
                        )
                        stack.enter_context(
                            patch(
                                f"{module}.ClaimRenewalLoop.stop",
                                autospec=True,
                                side_effect=renewal_stop,
                            )
                        )
                        stack.enter_context(
                            patch(
                                f"{module}.ClaimStore.assert_matches",
                                autospec=True,
                                side_effect=[
                                    record,
                                    Stage0ValidationError(
                                        "claim invariant failed"
                                    ),
                                ],
                            )
                        )
                        with self.assertRaises(Stage0ValidationError) as raised:
                            supervisor.execute()  # type: ignore[attr-defined]

                    self.assertEqual(
                        events,
                        ["renewal.start", "broker.start", "renewal.stop"],
                    )
                    message = str(raised.exception)
                    self.assertIn("broker startup failed", message)
                    self.assertTrue(
                        any(
                            "claim invariant failed" in note
                            for note in getattr(
                                raised.exception,
                                "__notes__",
                                (),
                            )
                        ),
                        "claim invariant failure must be retained as an "
                        "exception note",
                    )


if __name__ == "__main__":
    unittest.main()

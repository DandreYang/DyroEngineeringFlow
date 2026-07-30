"""Production-candidate claim and Core evidence handoff regression tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from experiments.external_workflow_runner.errors import Stage0ValidationError
from experiments.external_workflow_runner.stage1.claim import ClaimLease
from experiments.external_workflow_runner.stage5.core_handoff import (
    _validate_signing_key_path,
    _validate_workspace_artifacts,
    prepare_stage5_claim,
    stage5_claim_from_core,
)


def _core_claim(*, expires_at: datetime) -> dict[str, object]:
    return {
        "task_id": "TASK-CORE-1",
        "claim_id": "claim-core-1",
        "runner": "runner-prod-1",
        "execution_key_id": "runner-key-1",
        "claimed_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        ),
        "lease_expires_at": expires_at.isoformat(timespec="seconds"),
        "generation": 7,
    }


def _write_core_claim(
    path: Path,
    payload: dict[str, object],
) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)


class RuntimeCoreClaimTests(unittest.TestCase):
    def test_core_claim_permissions_must_be_private(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX permission bits are unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            core_claim = root / "core-claim.json"
            core_claim.write_text(
                json.dumps(
                    _core_claim(
                        expires_at=(
                            datetime.now(timezone.utc)
                            + timedelta(hours=1)
                        )
                    )
                ),
                encoding="utf-8",
            )
            core_claim.chmod(0o644)
            with self.assertRaisesRegex(
                Stage0ValidationError,
                "permissions",
            ):
                stage5_claim_from_core(core_claim)

    def test_signing_key_must_be_outside_runtime_workspaces(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            key = root / "runner.pem"
            key.write_text("not-read-during-preflight\n", encoding="utf-8")
            key.chmod(0o600)
            with self.assertRaisesRegex(
                Stage0ValidationError,
                "must be outside",
            ):
                _validate_signing_key_path(
                    key,
                    forbidden_roots=(root,),
                )

    def test_handoff_rejects_artifact_changed_after_pack(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            repository = workspace / "services/api"
            repository.mkdir(parents=True)
            artifact = repository / "report.md"
            artifact.write_text("sealed\n", encoding="utf-8")
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            candidate = {
                "artifacts": [
                    {
                        "repository": "api",
                        "path": "report.md",
                        "sha256": digest,
                        "bytes": len(artifact.read_bytes()),
                    }
                ]
            }
            artifact.write_text("forged\n", encoding="utf-8")
            config = SimpleNamespace(
                repositories={
                    "api": SimpleNamespace(mount="services/api")
                }
            )
            task = SimpleNamespace(repositories=("api",))
            with self.assertRaisesRegex(
                Stage0ValidationError,
                "SHA-256 mismatch",
            ):
                _validate_workspace_artifacts(
                    config=config,
                    task=task,
                    workspace=workspace,
                    candidate=candidate,
                )

    def test_prepare_preserves_control_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            expires = datetime.now(timezone.utc) + timedelta(hours=1)
            core_claim = root / "core-claim.json"
            _write_core_claim(
                core_claim,
                _core_claim(expires_at=expires),
            )
            output = root / "stage5-claim.json"
            report = prepare_stage5_claim(
                core_claim=core_claim,
                output=output,
            )
            prepared = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(report["verdict"], "PREPARED")
        self.assertEqual(prepared["control_claim_id"], "claim-core-1")
        self.assertEqual(prepared["control_generation"], 7)
        self.assertEqual(prepared["runner_id"], "runner-prod-1")
        self.assertEqual(
            prepared["execution_key_id"],
            "runner-key-1",
        )
        self.assertLessEqual(
            prepared["expires_at"],
            prepared["authority_expires_at"],
        )

    def test_prepare_dry_run_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            core_claim = root / "core-claim.json"
            _write_core_claim(
                core_claim,
                _core_claim(
                    expires_at=(
                        datetime.now(timezone.utc)
                        + timedelta(hours=1)
                    )
                ),
            )
            output = root / "stage5-claim.json"
            report = prepare_stage5_claim(
                core_claim=core_claim,
                output=output,
                dry_run=True,
            )
            self.assertFalse(output.exists())
        self.assertEqual(report["verdict"], "DRY_RUN")
        self.assertFalse(report["written"])

    def test_prepare_refuses_expired_core_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            core_claim = root / "core-claim.json"
            _write_core_claim(
                core_claim,
                _core_claim(
                    expires_at=(
                        datetime.now(timezone.utc)
                        - timedelta(seconds=1)
                    )
                ),
            )
            with self.assertRaisesRegex(
                Stage0ValidationError,
                "expired",
            ):
                stage5_claim_from_core(core_claim)

    def test_runtime_renewal_cannot_outlive_core_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            now = datetime.now(timezone.utc)
            core_claim = root / "core-claim.json"
            _write_core_claim(
                core_claim,
                _core_claim(
                    expires_at=now + timedelta(seconds=30)
                ),
            )
            record = stage5_claim_from_core(
                core_claim,
                now=now.timestamp(),
            )
            renewed = ClaimLease(record).build_renewal(
                extend_seconds=3600,
                now=now.timestamp() + 10,
            )
        self.assertEqual(
            renewed.expires_at,
            record.authority_expires_at,
        )
        self.assertEqual(
            renewed.control_claim_id,
            record.control_claim_id,
        )
        self.assertEqual(
            renewed.control_generation,
            record.control_generation,
        )

    def test_prepare_refuses_to_overwrite_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            core_claim = root / "core-claim.json"
            _write_core_claim(
                core_claim,
                _core_claim(
                    expires_at=(
                        datetime.now(timezone.utc)
                        + timedelta(hours=1)
                    )
                ),
            )
            output = root / "stage5-claim.json"
            output.write_text("owned-by-user", encoding="utf-8")
            with self.assertRaisesRegex(
                Stage0ValidationError,
                "overwrite",
            ):
                prepare_stage5_claim(
                    core_claim=core_claim,
                    output=output,
                )
            self.assertEqual(
                output.read_text(encoding="utf-8"),
                "owned-by-user",
            )

    def test_prepare_refuses_dangling_symlink_without_following_it(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            core_claim = root / "core-claim.json"
            _write_core_claim(
                core_claim,
                _core_claim(
                    expires_at=(
                        datetime.now(timezone.utc)
                        + timedelta(hours=1)
                    )
                ),
            )
            target = root / "attacker-target.json"
            output = root / "stage5-claim.json"
            output.symlink_to(target)
            with self.assertRaisesRegex(
                Stage0ValidationError,
                "overwrite",
            ):
                prepare_stage5_claim(
                    core_claim=core_claim,
                    output=output,
                )
            self.assertTrue(output.is_symlink())
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()

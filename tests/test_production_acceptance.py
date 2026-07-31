from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from dyro import __version__
from dyro.canonical import canonical_json_bytes
from dyro.errors import ValidationError
from dyro.signing import (
    generate_keypair,
    sign_record,
    trust_public_key,
)
from experiments.external_workflow_runner.sandbox import BUN_IMAGE
from experiments.external_workflow_runner.cli import main as runtime_main
from experiments.external_workflow_runner.stage5.production_acceptance import (
    ATTESTATION_PURPOSES,
    RELEASE_PURPOSE,
    read_production_json,
    verify_production_acceptance,
)
from experiments.external_workflow_runner.stage5.production_gate import (
    evaluate_production_readiness,
)


SHA256_A = "a" * 64
SHA256_B = "b" * 64
SHA256_C = "c" * 64
SHA256_D = "d" * 64
SHA256_E = "e" * 64


class ProductionAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="dyro-prod-accept-")
        self.root = Path(self.temporary.name)
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.private_keys: dict[str, Path] = {}
        for purpose, key_id in (
            (RELEASE_PURPOSE, "release-approver"),
            (ATTESTATION_PURPOSES["PROD-01"], "security-reviewer"),
            (ATTESTATION_PURPOSES["PROD-02"], "provider-operator"),
            (ATTESTATION_PURPOSES["PROD-09"], "quota-reviewer"),
        ):
            self.private_keys[purpose] = self._create_trusted_key(
                purpose,
                key_id,
            )
        self.manifest_path = self._write_release_manifest()
        self.manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self.manifest_sha256 = hashlib.sha256(
            canonical_json_bytes(self.manifest)
        ).hexdigest()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _create_trusted_key(
        self,
        purpose: str,
        key_id: str,
        *,
        private_key: Path | None = None,
        public_key: Path | None = None,
    ) -> Path:
        private = private_key or self.root / f"{key_id}.private.pem"
        public = public_key or self.root / f"{key_id}.public.pem"
        if private_key is None:
            generate_keypair(
                key_id,
                private_key=private,
                public_key=public,
            )
        trust_public_key(
            self.root,
            key_id,
            purpose=purpose,
            source=public,
        )
        return private

    def _write_json(self, name: str, payload: dict[str, object]) -> Path:
        path = self.root / name
        path.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )
        return path

    def _write_release_manifest(self) -> Path:
        record: dict[str, object] = {
            "schema_version": 1,
            "kind": "dyro-production-deployment-manifest",
            "release_id": "dyro-0.5.1-prod.1",
            "environment_id": "prod/tw-primary",
            "dyro_version": __version__,
            "source_commit": "1" * 40,
            "runtime_image": BUN_IMAGE,
            "artifacts": {
                "wheel_sha256": SHA256_A,
                "sdist_sha256": SHA256_B,
                "sbom_sha256": SHA256_C,
                "provenance_sha256": SHA256_D,
            },
            "providers": {
                "codex": SHA256_A,
                "claude": SHA256_B,
            },
            "operations": {
                "deployment_sha256": SHA256_A,
                "canary_plan_sha256": SHA256_B,
                "rollback_plan_sha256": SHA256_C,
                "observability_plan_sha256": SHA256_D,
                "runbook_sha256": SHA256_E,
            },
            "created_at": (self.now - timedelta(minutes=1)).isoformat(),
        }
        signed = sign_record(
            record,
            purpose=RELEASE_PURPOSE,
            key_id="release-approver",
            private_key=self.private_keys[RELEASE_PURPOSE],
        )
        return self._write_json("release-manifest.json", signed)

    def _assertions(self, check_id: str) -> dict[str, object]:
        if check_id == "PROD-01":
            return {
                "multi_host_escape_tested": True,
                "tenant_boundary_tested": True,
                "orchestrator_policy_verified": True,
                "kernel_hardening_verified": True,
                "storage_isolation_verified": True,
                "network_isolation_verified": True,
                "high_findings_open": 0,
                "critical_findings_open": 0,
            }
        if check_id == "PROD-02":
            return {
                "provider_binary_pins_verified": True,
                "broker_only_credentials_verified": True,
                "credential_rotation_tested": True,
                "credential_revocation_tested": True,
                "failure_recovery_tested": True,
                "canary_runs": 3,
                "high_findings_open": 0,
                "critical_findings_open": 0,
            }
        if check_id == "PROD-09":
            return {
                "all_writable_mounts_declared": True,
                "byte_limits_enforced": True,
                "inode_limits_enforced": True,
                "file_count_limits_enforced": True,
                "exhaustion_tested": True,
                "concurrent_tenant_tested": True,
                "writable_mount_count": 7,
                "high_findings_open": 0,
                "critical_findings_open": 0,
            }
        raise AssertionError(f"unsupported check: {check_id}")

    def _write_attestation(
        self,
        check_id: str,
        *,
        verdict: str = "pass",
        assertions: dict[str, object] | None = None,
        private_key: Path | None = None,
        key_id: str | None = None,
        expires_at: datetime | None = None,
    ) -> Path:
        purpose = ATTESTATION_PURPOSES[check_id]
        signer_ids = {
            "PROD-01": "security-reviewer",
            "PROD-02": "provider-operator",
            "PROD-09": "quota-reviewer",
        }
        record: dict[str, object] = {
            "schema_version": 1,
            "kind": "dyro-production-attestation",
            "check_id": check_id,
            "release_manifest_sha256": self.manifest_sha256,
            "environment_id": "prod/tw-primary",
            "verdict": verdict,
            "issued_at": (self.now - timedelta(minutes=1)).isoformat(),
            "expires_at": (expires_at or self.now + timedelta(days=7)).isoformat(),
            "evidence": [
                {
                    "uri": f"https://evidence.example/{check_id.lower()}/report.json",
                    "sha256": SHA256_E,
                    "summary": f"{check_id} independent acceptance evidence",
                }
            ],
            "assertions": (
                self._assertions(check_id) if assertions is None else assertions
            ),
        }
        signed = sign_record(
            record,
            purpose=purpose,
            key_id=key_id or signer_ids[check_id],
            private_key=private_key or self.private_keys[purpose],
        )
        return self._write_json(f"{check_id.lower()}-attestation.json", signed)

    def _all_attestations(self) -> tuple[Path, ...]:
        return tuple(
            self._write_attestation(check_id)
            for check_id in ("PROD-01", "PROD-02", "PROD-09")
        )

    def test_four_independent_signers_can_clear_external_blockers(self) -> None:
        attestations = self._all_attestations()

        acceptance = verify_production_acceptance(
            root=self.root,
            release_manifest_path=self.manifest_path,
            attestation_paths=attestations,
        )
        report = evaluate_production_readiness(
            root=self.root,
            release_manifest=self.manifest_path,
            attestations=attestations,
        )

        self.assertEqual(acceptance.release_id, "dyro-0.5.1-prod.1")
        self.assertEqual(len(set(acceptance.signer_fingerprints.values())), 4)
        self.assertTrue(report["production_ready"])
        self.assertEqual(report["verdict"], "READY")
        self.assertEqual(report["exit_code"], 0)
        self.assertEqual(report["blocker_count"], 0)
        self.assertTrue(report["release_approval_required"])
        self.assertEqual(
            report["production_acceptance"]["release_manifest_sha256"],
            self.manifest_sha256,
        )

    def test_partial_acceptance_stays_not_ready_and_names_missing_checks(
        self,
    ) -> None:
        security = self._write_attestation("PROD-01")

        report = evaluate_production_readiness(
            root=self.root,
            release_manifest=self.manifest_path,
            attestations=(security,),
        )

        self.assertFalse(report["production_ready"])
        self.assertEqual(report["blocker_count"], 2)
        self.assertEqual(
            {item["id"] for item in report["blockers"]},
            {"PROD-02", "PROD-09"},
        )
        self.assertEqual(
            report["production_acceptance"]["missing_checks"],
            ["PROD-02", "PROD-09"],
        )

    def test_tampered_manifest_and_weak_pass_fail_closed(self) -> None:
        tampered = dict(self.manifest)
        tampered["source_commit"] = "2" * 40
        tampered_path = self._write_json("tampered-manifest.json", tampered)
        with self.assertRaisesRegex(ValidationError, "signature"):
            verify_production_acceptance(
                root=self.root,
                release_manifest_path=tampered_path,
                attestation_paths=(),
            )

        weak = self._assertions("PROD-09")
        weak["inode_limits_enforced"] = False
        weak_path = self._write_attestation(
            "PROD-09",
            assertions=weak,
        )
        with self.assertRaisesRegex(
            ValidationError,
            "PROD-09.*inode_limits_enforced",
        ):
            verify_production_acceptance(
                root=self.root,
                release_manifest_path=self.manifest_path,
                attestation_paths=(weak_path,),
            )

        open_high_finding = self._assertions("PROD-01")
        open_high_finding["high_findings_open"] = 1
        open_high_finding_path = self._write_attestation(
            "PROD-01",
            assertions=open_high_finding,
        )
        with self.assertRaisesRegex(
            ValidationError,
            "PROD-01.*high_findings_open",
        ):
            verify_production_acceptance(
                root=self.root,
                release_manifest_path=self.manifest_path,
                attestation_paths=(open_high_finding_path,),
            )

    def test_manifest_reader_rejects_symlink_and_duplicate_fields(
        self,
    ) -> None:
        linked_manifest = self.root / "linked-manifest.json"
        linked_manifest.symlink_to(self.manifest_path)
        with self.assertRaisesRegex(ValidationError, "普通文件|安全打开"):
            verify_production_acceptance(
                root=self.root,
                release_manifest_path=linked_manifest,
                attestation_paths=(),
            )

        duplicate_manifest = self.root / "duplicate-manifest.json"
        duplicate_manifest.write_text(
            self.manifest_path.read_text(encoding="utf-8").replace(
                '"schema_version": 1',
                '"schema_version": 1, "schema_version": 1',
                1,
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValidationError, "重复字段"):
            verify_production_acceptance(
                root=self.root,
                release_manifest_path=duplicate_manifest,
                attestation_paths=(),
            )

    def test_manifest_reader_rejects_path_replacement_after_snapshot(
        self,
    ) -> None:
        victim = self.root / "replaceable-manifest.json"
        victim.write_bytes(self.manifest_path.read_bytes())
        original_read = os.read
        replaced = False

        def replace_after_read(descriptor: int, size: int) -> bytes:
            nonlocal replaced
            chunk = original_read(descriptor, size)
            if chunk and not replaced:
                replaced = True
                victim.rename(self.root / "original-manifest.json")
                victim.write_text("{}", encoding="utf-8")
            return chunk

        with (
            mock.patch(
                "experiments.external_workflow_runner.stage5."
                "production_acceptance.os.read",
                side_effect=replace_after_read,
            ),
            self.assertRaisesRegex(ValidationError, "读取期间发生变化"),
        ):
            read_production_json(victim, "生产发布清单")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires FIFO support")
    def test_manifest_reader_rejects_fifo_without_blocking(self) -> None:
        fifo = self.root / "manifest.fifo"
        os.mkfifo(fifo)

        with self.assertRaisesRegex(ValidationError, "普通文件"):
            verify_production_acceptance(
                root=self.root,
                release_manifest_path=fifo,
                attestation_paths=(),
            )

    def test_expired_attestation_is_rejected(self) -> None:
        expired = self._write_attestation(
            "PROD-01",
            expires_at=self.now,
        )
        with self.assertRaisesRegex(ValidationError, "已过期"):
            verify_production_acceptance(
                root=self.root,
                release_manifest_path=self.manifest_path,
                attestation_paths=(expired,),
            )

    def test_signed_fail_attestation_remains_a_blocker(self) -> None:
        failed = self._write_attestation(
            "PROD-02",
            verdict="fail",
        )

        report = evaluate_production_readiness(
            root=self.root,
            release_manifest=self.manifest_path,
            attestations=(failed,),
        )

        checklist = {item["id"]: item for item in report["checklist"]}
        self.assertEqual(checklist["PROD-02"]["status"], "fail")
        self.assertIn(
            "PROD-02",
            {item["id"] for item in report["blockers"]},
        )

    def test_same_public_key_cannot_approve_multiple_roles(self) -> None:
        release_private = self.private_keys[RELEASE_PURPOSE]
        release_public = self.root / "release-approver.public.pem"
        self._create_trusted_key(
            ATTESTATION_PURPOSES["PROD-01"],
            "release-as-security",
            private_key=release_private,
            public_key=release_public,
        )
        security = self._write_attestation(
            "PROD-01",
            private_key=release_private,
            key_id="release-as-security",
        )

        with self.assertRaisesRegex(ValidationError, "独立公钥"):
            verify_production_acceptance(
                root=self.root,
                release_manifest_path=self.manifest_path,
                attestation_paths=(security,),
            )

    def test_default_gate_remains_not_ready_without_external_evidence(
        self,
    ) -> None:
        report = evaluate_production_readiness()

        self.assertFalse(report["production_ready"])
        self.assertEqual(report["verdict"], "NOT_READY")
        self.assertFalse(report["production_acceptance"]["provided"])

    def test_runtime_cli_verifies_complete_acceptance(self) -> None:
        security, provider, quota = self._all_attestations()
        output = StringIO()
        with mock.patch("sys.stdout", output):
            code = runtime_main(
                [
                    "production-gate",
                    "--root",
                    str(self.root),
                    "--release-manifest",
                    str(self.manifest_path),
                    "--security-attestation",
                    str(security),
                    "--provider-attestation",
                    str(provider),
                    "--quota-attestation",
                    str(quota),
                    "--json",
                ]
            )

        self.assertEqual(code, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["verdict"], "READY")
        self.assertTrue(payload["release_approval_required"])

    def test_runtime_cli_rejects_attestation_without_release_manifest(
        self,
    ) -> None:
        security = self._write_attestation("PROD-01")
        errors = StringIO()
        with mock.patch("sys.stderr", errors):
            code = runtime_main(
                [
                    "production-gate",
                    "--root",
                    str(self.root),
                    "--security-attestation",
                    str(security),
                ]
            )

        self.assertEqual(code, 2)
        self.assertIn("--release-manifest", errors.getvalue())

    def test_runtime_cli_rejects_attestation_in_wrong_role_flag(
        self,
    ) -> None:
        provider = self._write_attestation("PROD-02")
        errors = StringIO()
        with mock.patch("sys.stderr", errors):
            code = runtime_main(
                [
                    "production-gate",
                    "--root",
                    str(self.root),
                    "--release-manifest",
                    str(self.manifest_path),
                    "--security-attestation",
                    str(provider),
                ]
            )

        self.assertEqual(code, 2)
        self.assertIn(
            "PROD-01 参数不能接受 PROD-02",
            errors.getvalue(),
        )

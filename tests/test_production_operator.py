from __future__ import annotations

import ast
import base64
from datetime import datetime, timedelta, timezone
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import shlex
import stat
import tempfile
import unittest
from unittest import mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from dyro.canonical import canonical_json_bytes
from dyro.errors import ValidationError
from dyro.signing import (
    generate_keypair,
    signature_message,
    trust_public_key,
)
from experiments.external_workflow_runner.cli import main as runtime_main
from experiments.external_workflow_runner.stage5 import (
    production_operator as operator,
)
from experiments.external_workflow_runner.stage5.production_acceptance import (
    ATTESTATION_PURPOSES,
    RELEASE_PURPOSE,
)
from experiments.external_workflow_runner.stage5.production_gate import (
    evaluate_production_readiness,
)


class ProductionOperatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="dyro-prod-operator-")
        self.root = Path(self.temporary.name)
        self.private_keys: dict[str, Path] = {}
        for purpose, key_id in (
            (RELEASE_PURPOSE, "release-operator"),
            (ATTESTATION_PURPOSES["PROD-01"], "security-reviewer"),
            (ATTESTATION_PURPOSES["PROD-02"], "provider-operator"),
            (ATTESTATION_PURPOSES["PROD-09"], "quota-reviewer"),
        ):
            private = self.root / f"{key_id}.private.pem"
            public = self.root / f"{key_id}.public.pem"
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
            self.private_keys[purpose] = private
        self.artifacts = {
            "wheel_sha256": self._write_bytes("dyro.whl", b"wheel-content"),
            "sdist_sha256": self._write_bytes("dyro.tar.gz", b"sdist-content"),
            "sbom_sha256": self._write_bytes("sbom.json", b'{"sbom":true}'),
            "provenance_sha256": self._write_bytes(
                "provenance.json",
                b'{"provenance":true}',
            ),
        }
        self.providers = (
            ("codex", self._write_bytes("codex", b"codex-provider")),
            ("claude", self._write_bytes("claude", b"claude-provider")),
        )
        self.operations = {
            "deployment_sha256": self._write_bytes(
                "deployment.yaml",
                b"deployment",
            ),
            "canary_plan_sha256": self._write_bytes(
                "canary.md",
                b"canary",
            ),
            "rollback_plan_sha256": self._write_bytes(
                "rollback.md",
                b"rollback",
            ),
            "observability_plan_sha256": self._write_bytes(
                "observability.md",
                b"observability",
            ),
            "runbook_sha256": self._write_bytes("runbook.md", b"runbook"),
        }
        self.unsigned_manifest = self.root / "release.unsigned.json"
        operator.prepare_release_manifest(
            release_id="dyro-0.5.1-prod.1",
            environment_id="prod/tw-primary",
            source_commit="1" * 40,
            artifacts=self.artifacts,
            providers=self.providers,
            operations=self.operations,
            output=self.unsigned_manifest,
        )
        self.signed_manifest = self.root / "release.signed.json"
        self.release_attachment = operator.attach_production_signature(
            root=self.root,
            record_path=self.unsigned_manifest,
            key_id="release-operator",
            signature_base64=self._external_signature(
                self.unsigned_manifest,
                RELEASE_PURPOSE,
            ),
            output=self.signed_manifest,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_bytes(self, name: str, content: bytes) -> Path:
        path = self.root / name
        path.write_bytes(content)
        return path

    def _write_json(self, name: str, value: object) -> Path:
        path = self.root / name
        path.write_text(
            json.dumps(value, ensure_ascii=False),
            encoding="utf-8",
        )
        return path

    def _external_signature(self, record_path: Path, purpose: str) -> str:
        record = json.loads(record_path.read_text(encoding="utf-8"))
        private = serialization.load_pem_private_key(
            self.private_keys[purpose].read_bytes(),
            password=None,
        )
        self.assertIsInstance(private, Ed25519PrivateKey)
        signature = private.sign(signature_message(record, purpose))
        return base64.b64encode(signature).decode("ascii")

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
                "canary_runs": 2,
                "high_findings_open": 0,
                "critical_findings_open": 0,
            }
        return {
            "all_writable_mounts_declared": True,
            "byte_limits_enforced": True,
            "inode_limits_enforced": True,
            "file_count_limits_enforced": True,
            "exhaustion_tested": True,
            "concurrent_tenant_tested": True,
            "writable_mount_count": 4,
            "high_findings_open": 0,
            "critical_findings_open": 0,
        }

    def test_schemas_are_locatable_and_export_is_create_only(self) -> None:
        located = operator.describe_production_schemas()
        self.assertEqual(located["verdict"], "LOCATED")
        self.assertFalse(located["written"])
        self.assertFalse(located["private_key_loaded"])
        self.assertFalse(located["release_approval_granted"])
        self.assertFalse(located["deployment_attempted"])
        for record in located["schemas"].values():
            self.assertTrue(Path(record["path"]).is_file())
            self.assertEqual(len(record["sha256"]), 64)

        dry_target = self.root / "dry-schemas"
        dry = operator.describe_production_schemas(
            output_dir=dry_target,
            dry_run=True,
        )
        self.assertEqual(dry["verdict"], "DRY_RUN")
        self.assertFalse(dry_target.exists())

        target = self.root / "exported-schemas"
        exported = operator.describe_production_schemas(output_dir=target)
        self.assertTrue(exported["written"])
        self.assertTrue(
            (target / "production-deployment-manifest.schema.json").is_file()
        )
        self.assertTrue(
            (target / "production-attestation.schema.json").is_file()
        )
        with self.assertRaisesRegex(ValidationError, "拒绝覆盖"):
            operator.describe_production_schemas(output_dir=target)

    def test_release_preparation_hashes_real_files_and_dry_run_writes_nothing(
        self,
    ) -> None:
        dry_output = self.root / "dry-release.json"
        dry = operator.prepare_release_manifest(
            release_id="dyro-0.5.1-prod.2",
            environment_id="prod/tw-primary",
            source_commit="2" * 40,
            artifacts=self.artifacts,
            providers=self.providers,
            operations=self.operations,
            output=dry_output,
            dry_run=True,
        )
        self.assertEqual(dry["verdict"], "DRY_RUN")
        self.assertFalse(dry_output.exists())
        self.assertFalse(dry["signed"])
        self.assertEqual(
            dry["record"]["artifacts"]["wheel_sha256"],
            hashlib.sha256(b"wheel-content").hexdigest(),
        )

        output = self.root / "release output;safe.json"
        prepared = operator.prepare_release_manifest(
            release_id="dyro-0.5.1-prod.2",
            environment_id="prod/tw-primary",
            source_commit="2" * 40,
            artifacts=self.artifacts,
            providers=self.providers,
            operations=self.operations,
            output=output,
        )
        record = json.loads(output.read_text(encoding="utf-8"))
        self.assertTrue(prepared["written"])
        self.assertFalse(prepared["private_key_loaded"])
        self.assertFalse(prepared["release_approval_granted"])
        self.assertFalse(prepared["deployment_attempted"])
        self.assertIn(
            shlex.quote(str(output.parent.resolve() / output.name)),
            prepared["next_command"],
        )
        self.assertNotIn("signature", record)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
        self.assertEqual(
            prepared["record_sha256"],
            hashlib.sha256(canonical_json_bytes(record)).hexdigest(),
        )
        with self.assertRaisesRegex(ValidationError, "拒绝覆盖"):
            operator.prepare_release_manifest(
                release_id="dyro-0.5.1-prod.2",
                environment_id="prod/tw-primary",
                source_commit="2" * 40,
                artifacts=self.artifacts,
                providers=self.providers,
                operations=self.operations,
                output=output,
            )

    def test_cli_release_dry_run_is_machine_readable_and_write_free(
        self,
    ) -> None:
        output_path = self.root / "cli-release.unsigned.json"
        stdout = StringIO()
        argv = [
            "--dry-run",
            "production-acceptance",
            "release-prepare",
            "--release-id",
            "dyro-0.5.1-prod.cli",
            "--environment-id",
            "prod/tw-primary",
            "--source-commit",
            "3" * 40,
            "--wheel",
            str(self.artifacts["wheel_sha256"]),
            "--sdist",
            str(self.artifacts["sdist_sha256"]),
            "--sbom",
            str(self.artifacts["sbom_sha256"]),
            "--provenance",
            str(self.artifacts["provenance_sha256"]),
        ]
        for provider_id, path in self.providers:
            argv.extend(["--provider", f"{provider_id}={path}"])
        argv.extend(
            [
                "--deployment",
                str(self.operations["deployment_sha256"]),
                "--canary-plan",
                str(self.operations["canary_plan_sha256"]),
                "--rollback-plan",
                str(self.operations["rollback_plan_sha256"]),
                "--observability-plan",
                str(self.operations["observability_plan_sha256"]),
                "--runbook",
                str(self.operations["runbook_sha256"]),
                "--output",
                str(output_path),
                "--json",
            ]
        )
        with mock.patch("sys.stdout", stdout):
            code = runtime_main(argv)

        self.assertEqual(code, 0)
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["verdict"], "DRY_RUN")
        self.assertFalse(report["written"])
        self.assertFalse(output_path.exists())

    def test_hashing_rejects_links_fifo_and_path_replacement(self) -> None:
        linked = self.root / "linked-wheel"
        linked.symlink_to(self.artifacts["wheel_sha256"])
        with self.assertRaisesRegex(ValidationError, "普通文件"):
            operator._hash_stable_file(
                linked,
                "linked input",
                max_bytes=1024,
            )

        if hasattr(os, "mkfifo"):
            fifo = self.root / "artifact.fifo"
            os.mkfifo(fifo)
            with self.assertRaisesRegex(ValidationError, "普通文件"):
                operator._hash_stable_file(
                    fifo,
                    "fifo input",
                    max_bytes=1024,
                )

        victim = self._write_bytes("replace-me.bin", b"original")
        backup = self.root / "replace-me.original"
        original_read = operator.os.read
        replaced = False

        def replace_after_read(descriptor: int, size: int) -> bytes:
            nonlocal replaced
            chunk = original_read(descriptor, size)
            if chunk and not replaced:
                replaced = True
                victim.rename(backup)
                victim.write_bytes(b"replacement")
            return chunk

        with (
            mock.patch.object(
                operator.os,
                "read",
                side_effect=replace_after_read,
            ),
            self.assertRaisesRegex(ValidationError, "发生变化"),
        ):
            operator._hash_stable_file(
                victim,
                "replaceable input",
                max_bytes=1024,
            )

    def test_attestation_requires_explicit_strong_assertions_and_hashes_evidence(
        self,
    ) -> None:
        evidence = self._write_bytes("security-report.json", b'{"passed":true}')
        weak = self._assertions("PROD-01")
        weak["tenant_boundary_tested"] = False
        weak_path = self._write_json("weak-assertions.json", weak)
        weak_output = self.root / "weak-attestation.json"
        with self.assertRaisesRegex(
            ValidationError,
            "tenant_boundary_tested",
        ):
            operator.prepare_production_attestation(
                root=self.root,
                release_manifest=self.signed_manifest,
                check_id="PROD-01",
                verdict="pass",
                assertions_path=weak_path,
                evidence=(
                    (
                        "https://evidence.example/security/report.json",
                        evidence,
                        "independent security report",
                    ),
                ),
                expires_at=(
                    datetime.now(timezone.utc) + timedelta(days=7)
                ).isoformat(),
                output=weak_output,
            )
        self.assertFalse(weak_output.exists())

        assertions = self._write_json(
            "security-assertions.json",
            self._assertions("PROD-01"),
        )
        output = self.root / "security.unsigned.json"
        prepared = operator.prepare_production_attestation(
            root=self.root,
            release_manifest=self.signed_manifest,
            check_id="PROD-01",
            verdict="pass",
            assertions_path=assertions,
            evidence=(
                (
                    "https://evidence.example/security/report.json",
                    evidence,
                    "independent security report",
                ),
            ),
            expires_at=(
                datetime.now(timezone.utc) + timedelta(days=7)
            ).isoformat(),
            output=output,
        )
        record = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(prepared["requested_verdict"], "pass")
        self.assertFalse(prepared["signed"])
        self.assertFalse(prepared["private_key_loaded"])
        self.assertFalse(prepared["release_approval_granted"])
        self.assertFalse(prepared["deployment_attempted"])
        self.assertEqual(
            record["evidence"][0]["sha256"],
            hashlib.sha256(b'{"passed":true}').hexdigest(),
        )
        self.assertEqual(
            record["release_manifest_sha256"],
            hashlib.sha256(
                canonical_json_bytes(
                    json.loads(self.signed_manifest.read_text(encoding="utf-8"))
                )
            ).hexdigest(),
        )

    def test_payload_is_exact_domain_separated_bytes_and_attach_verifies(
        self,
    ) -> None:
        payload_file = self.root / "release.payload"
        payload = operator.build_production_signing_payload(
            record_path=self.unsigned_manifest,
            output=payload_file,
        )
        record = json.loads(
            self.unsigned_manifest.read_text(encoding="utf-8")
        )
        expected = signature_message(record, RELEASE_PURPOSE)
        self.assertEqual(payload_file.read_bytes(), expected)
        self.assertEqual(
            base64.b64decode(payload["payload_base64"], validate=True),
            expected,
        )
        self.assertEqual(
            payload["payload_sha256"],
            hashlib.sha256(expected).hexdigest(),
        )
        self.assertFalse(payload["private_key_loaded"])

        with self.assertRaisesRegex(ValidationError, "已经包含 signature"):
            operator.build_production_signing_payload(
                record_path=self.signed_manifest,
            )

        wrong_output = self.root / "wrong-signed.json"
        with self.assertRaisesRegex(ValidationError, "验证失败"):
            operator.attach_production_signature(
                root=self.root,
                record_path=self.unsigned_manifest,
                key_id="release-operator",
                signature_base64=base64.b64encode(b"\0" * 64).decode("ascii"),
                output=wrong_output,
            )
        self.assertFalse(wrong_output.exists())

    def test_operator_outputs_can_clear_gate_without_granting_release(
        self,
    ) -> None:
        self.assertFalse(self.release_attachment["release_approval_granted"])
        self.assertFalse(self.release_attachment["deployment_attempted"])
        signed_attestations: dict[str, Path] = {}
        signer_ids = {
            "PROD-01": "security-reviewer",
            "PROD-02": "provider-operator",
            "PROD-09": "quota-reviewer",
        }
        for check_id, purpose in ATTESTATION_PURPOSES.items():
            assertions = self._write_json(
                f"{check_id}.assertions.json",
                self._assertions(check_id),
            )
            evidence = self._write_bytes(
                f"{check_id}.evidence.json",
                f'{{"check":"{check_id}"}}'.encode(),
            )
            unsigned = self.root / f"{check_id}.unsigned.json"
            operator.prepare_production_attestation(
                root=self.root,
                release_manifest=self.signed_manifest,
                check_id=check_id,
                verdict="pass",
                assertions_path=assertions,
                evidence=(
                    (
                        f"https://evidence.example/{check_id.lower()}/report.json",
                        evidence,
                        f"{check_id} independent production evidence",
                    ),
                ),
                expires_at=(
                    datetime.now(timezone.utc) + timedelta(days=7)
                ).isoformat(),
                output=unsigned,
            )
            payload = operator.build_production_signing_payload(
                record_path=unsigned,
                root=self.root,
                release_manifest=self.signed_manifest,
            )
            private = serialization.load_pem_private_key(
                self.private_keys[purpose].read_bytes(),
                password=None,
            )
            signature = base64.b64encode(
                private.sign(
                    base64.b64decode(
                        payload["payload_base64"],
                        validate=True,
                    )
                )
            ).decode("ascii")
            signed = self.root / f"{check_id}.signed.json"
            attachment = operator.attach_production_signature(
                root=self.root,
                record_path=unsigned,
                key_id=signer_ids[check_id],
                signature_base64=signature,
                output=signed,
                release_manifest=self.signed_manifest,
            )
            self.assertTrue(attachment["signature_verified"])
            self.assertFalse(attachment["release_approval_granted"])
            self.assertFalse(attachment["deployment_attempted"])
            signed_attestations[check_id] = signed

        report = evaluate_production_readiness(
            root=self.root,
            release_manifest=self.signed_manifest,
            attestations=signed_attestations,
        )
        self.assertTrue(report["production_ready"])
        self.assertTrue(report["release_approval_required"])
        self.assertEqual(report["next_action"], "independent_release_approval")
        self.assertIsNone(report["next_command"])

    def test_signature_file_and_cli_never_expose_private_key_option(self) -> None:
        signature_file = self._write_bytes(
            "signature.b64",
            (self._external_signature(
                self.unsigned_manifest,
                RELEASE_PURPOSE,
            ) + "\n").encode("ascii"),
        )
        self.assertEqual(
            operator.read_external_signature(signature_file),
            signature_file.read_text(encoding="ascii").strip(),
        )
        linked = self.root / "linked-signature.b64"
        linked.symlink_to(signature_file)
        with self.assertRaisesRegex(ValidationError, "普通文件"):
            operator.read_external_signature(linked)

        output = StringIO()
        with (
            mock.patch("sys.stdout", output),
            self.assertRaises(SystemExit) as raised,
        ):
            runtime_main(
                [
                    "production-acceptance",
                    "signature-attach",
                    "--help",
                ]
            )
        self.assertEqual(raised.exception.code, 0)
        help_text = output.getvalue()
        self.assertIn("--signature-file", help_text)
        self.assertNotIn("--signing-key", help_text)
        self.assertNotIn("--private-key", help_text)

    def test_operator_source_cannot_gain_signing_or_delivery_authority(
        self,
    ) -> None:
        source = Path(operator.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        forbidden_imports = {
            "Ed25519PrivateKey",
            "_load_private_key",
            "generate_keypair",
            "load_pem_private_key",
            "sign_record",
        }
        imported_names = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        self.assertTrue(forbidden_imports.isdisjoint(imported_names))

        forbidden_calls = {
            "_load_private_key",
            "build_core_evidence_handoff",
            "generate_keypair",
            "load_pem_private_key",
            "merge_task",
            "sign_record",
            "signoff_task",
        }
        called_names: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name):
                called_names.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                called_names.add(node.func.attr)
        self.assertTrue(forbidden_calls.isdisjoint(called_names))

        forbidden_parameters = {"private_key", "signing_key"}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                parameter_names = {
                    argument.arg
                    for argument in (
                        *node.args.posonlyargs,
                        *node.args.args,
                        *node.args.kwonlyargs,
                    )
                }
                self.assertTrue(
                    forbidden_parameters.isdisjoint(parameter_names),
                    node.name,
                )


if __name__ == "__main__":
    unittest.main()

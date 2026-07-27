from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dyro.errors import ValidationError
from dyro.provenance import (
    build_external_attempt_record,
    external_execution_plan,
    import_external_execution_attempt,
)
from dyro.reviews import build_signed_review_record, load_review_evidence
from dyro.signing import (
    generate_keypair,
    sign_record,
    trust_public_key,
    trusted_keys_directory,
    verify_record,
)
from dyro.tasks import Task, execution_claim_binding


class SignedSecurityBoundaryTests(unittest.TestCase):
    def _keys(self, root: Path, key_id: str, purpose: str) -> tuple[Path, Path]:
        private_key = root / f"{key_id}.private.pem"
        public_key = root / f"{key_id}.public.pem"
        generate_keypair(key_id, private_key=private_key, public_key=public_key)
        trust_public_key(root, key_id, purpose=purpose, source=public_key)
        return private_key, public_key

    def test_required_signature_does_not_downgrade_when_trust_store_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(ValidationError, "策略要求"):
                verify_record(
                    {"schema_version": 1},
                    purpose="execution",
                    trust_directory=trusted_keys_directory(root, "execution"),
                    required=True,
                )

    def test_stale_claim_generation_is_rejected_by_authoritative_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task_directory = root / "task"
            task_directory.mkdir()
            (task_directory / "task.toml").write_text(
                'schema_version = 1\nid = "A"\n',
                encoding="utf-8",
            )
            task = Task(
                id="A",
                title="A",
                line="alpha",
                risk="write",
                executor="noop",
                reviewer="noop",
                repositories=("api",),
                directory=task_directory,
            )
            private_key, _ = self._keys(root, "runner-1", "execution")
            old_claim = {
                "task_id": "A",
                "claim_id": "old-claim",
                "runner": "runner-1",
                "execution_key_id": "runner-1",
                "generation": 1,
                "lease_expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            }
            new_claim = dict(old_claim, claim_id="new-claim", generation=2, runner="runner-2")
            (task_directory / "claim.json").write_text(json.dumps(new_claim), encoding="utf-8")
            old_plan = external_execution_plan(
                task,
                "external",
                claim_binding={
                    "claim_id": old_claim["claim_id"],
                    "generation": old_claim["generation"],
                    "runner": old_claim["runner"],
                    "execution_key_id": old_claim["execution_key_id"],
                },
            )
            record = build_external_attempt_record(
                task_directory,
                "A",
                old_plan,
                result="QUESTION",
                receipt_sha256="a" * 64,
            )
            signed = sign_record(
                record,
                purpose="execution",
                key_id="runner-1",
                private_key=private_key,
            )
            provenance = root / "provenance.json"
            provenance.write_text(json.dumps(signed), encoding="utf-8")
            expected_plan = external_execution_plan(
                task,
                "external",
                claim_binding=execution_claim_binding(task),
            )

            with self.assertRaisesRegex(ValidationError, "权威计划"):
                import_external_execution_attempt(
                    task_directory,
                    "A",
                    provenance=provenance,
                    receipt_sha256="a" * 64,
                    result="QUESTION",
                    expected_plan=expected_plan,
                    trusted_keys_dir=trusted_keys_directory(root, "execution"),
                    require_signature=True,
                    dry_run=True,
                )

    def test_signed_review_authenticates_content_and_reviewer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private_key, _ = self._keys(root, "reviewer-1", "review")
            content = b"verdict: PASS\nreceipt_sha256: " + (b"a" * 64) + b"\n"
            record = build_signed_review_record(
                "A",
                reviewer="reviewer-1",
                review_content=content,
                signing_key=private_key,
                key_id="reviewer-1",
            )
            path = root / "review.json"
            path.write_text(json.dumps(record), encoding="utf-8")

            evidence = load_review_evidence(
                path,
                task_id="A",
                trust_directory=trusted_keys_directory(root, "review"),
                require_signature=True,
            )
            self.assertEqual(evidence.content, content)
            self.assertEqual(evidence.reviewer, "reviewer-1")
            self.assertEqual(evidence.key_id, "reviewer-1")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

from dyro.proof.bundle import export_bundle, proof_from_payload, verify_bundle
from dyro.proof.models import Proof, ProofKind, ProofStatus, ProofSubstrate
from dyro.proof.project import VERIFY_EXIT_INCONCLUSIVE, verify_exit_code


def _git_head(root: Path) -> tuple[Path, str]:
    work = root / "work"
    work.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=work, check=True, stdout=subprocess.PIPE)
    subprocess.run(["git", "config", "user.name", "T"], cwd=work, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=work, check=True)
    (work / "README").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "README"], cwd=work, check=True)
    subprocess.run(["git", "commit", "-m", "x"], cwd=work, check=True, stdout=subprocess.PIPE)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=work,
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout.strip()
    return work, sha


def _proof(
    *,
    kind: ProofKind = ProofKind.REVIEW_VERDICT,
    require_signed: bool = False,
    heads: tuple[tuple[str, str], ...] = (),
    status: ProofStatus = ProofStatus.INCONCLUSIVE,
    decay_reason: str = "",
) -> Proof:
    policy = ()
    if kind is ProofKind.REVIEW_VERDICT:
        policy = (("require_signed_review", "true" if require_signed else "false"),)
    elif kind is ProofKind.SIGNOFF:
        policy = (("require_signed_signoff", "true" if require_signed else "false"),)
    return Proof(
        id="a" * 64,
        kind=kind,
        subject="TASK-A",
        substrate=ProofSubstrate(repo_heads=heads, plan_sha256="plan"),
        procedure="review.md rebind",
        bytes_sha256="b" * 64,
        generation="1",
        status=status,
        decay_reason=decay_reason,
        policy_require_signed=policy,
        declared_key_ids=(),
    )


class ProofBundleIntegrityTests(unittest.TestCase):
    def test_signed_policy_without_declared_keys_is_inconclusive(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dyro-bundle-") as tmp:
            bundle = Path(tmp) / "signed.zip"
            export_bundle((_proof(require_signed=True),), bundle)
            proofs = verify_bundle(bundle, git_dirs=())
            self.assertEqual(proofs[0].status, ProofStatus.INCONCLUSIVE)
            self.assertEqual(proofs[0].decay_reason, "missing_declared_keys")
            self.assertEqual(verify_exit_code(proofs), VERIFY_EXIT_INCONCLUSIVE)

    def test_wrong_schema_version_is_inconclusive(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dyro-bundle-") as tmp:
            bundle = Path(tmp) / "v2.zip"
            with zipfile.ZipFile(bundle, "w") as archive:
                archive.writestr(
                    "manifest.json",
                    json.dumps({"kind": "dyro.proof.bundle", "schema_version": 2, "proof_ids": []}) + "\n",
                )
            proofs = verify_bundle(bundle, git_dirs=())
            self.assertEqual(proofs[0].status, ProofStatus.INCONCLUSIVE)
            self.assertNotEqual(proofs[0].status, ProofStatus.LIVE)

    def test_payload_round_trip_preserves_pins(self) -> None:
        proof = _proof(heads=(("api", "abc1234"),))
        restored = proof_from_payload(
            {
                "id": proof.id,
                "kind": proof.kind.value,
                "subject": proof.subject,
                "procedure": proof.procedure,
                "bytes_sha256": proof.bytes_sha256,
                "generation": proof.generation,
                "declared_key_ids": [],
                "policy_require_signed": {"require_signed_review": "false"},
                "substrate": {
                    "repo_heads": {"api": "abc1234"},
                    "plan_sha256": "plan",
                    "attempt_id": "",
                    "contract_hash": "",
                    "extra": {},
                },
            }
        )
        self.assertEqual(restored.substrate.repo_heads, (("api", "abc1234"),))

    def test_stranger_script_uses_caller_git_objects(self) -> None:
        script = Path(__file__).resolve().parents[1] / "tools" / "verify_bundle_stranger.py"
        completed = subprocess.run(
            [sys.executable, str(script), sys.executable, "-m", "dyro"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["mode"], "integrity")

    def test_headless_receipt_without_git_dir_is_not_live(self) -> None:
        receipt = Proof(
            id="c" * 64,
            kind=ProofKind.ACTION_RECEIPT,
            subject="OBJ",
            substrate=ProofSubstrate(plan_sha256="plan", attempt_id="act"),
            procedure="action journal receipt bytes",
            bytes_sha256="d" * 64,
            generation="1",
            status=ProofStatus.INCONCLUSIVE,
        )
        with tempfile.TemporaryDirectory(prefix="dyro-bundle-") as tmp:
            bundle = Path(tmp) / "headless.zip"
            export_bundle((receipt,), bundle)
            proofs = verify_bundle(bundle, git_dirs=())
            self.assertEqual(proofs[0].status, ProofStatus.INCONCLUSIVE)
            self.assertEqual(proofs[0].decay_reason, "missing_git_objects")
            self.assertIsNot(proofs[0].status, ProofStatus.LIVE)
            self.assertEqual(verify_exit_code(proofs), VERIFY_EXIT_INCONCLUSIVE)

    def test_missing_proof_digest_is_not_live(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dyro-bundle-") as tmp:
            root = Path(tmp)
            work, sha = _git_head(root)
            bundle = root / "digest.zip"
            export_bundle((_proof(heads=(("api", sha),)),), bundle)
            with zipfile.ZipFile(bundle) as archive:
                manifest = json.loads(archive.read("manifest.json"))
                body = archive.read(f"proofs/{'a' * 64}.json")
            payload = json.loads(body)
            payload["procedure"] = "tampered"
            rewritten = root / "rewritten.zip"
            manifest["proof_sha256"] = {}
            with zipfile.ZipFile(rewritten, "w") as archive:
                archive.writestr("manifest.json", json.dumps(manifest) + "\n")
                archive.writestr(f"proofs/{'a' * 64}.json", json.dumps(payload) + "\n")
            proofs = verify_bundle(rewritten, git_dirs=(work,))
            self.assertTrue(all(item.status is not ProofStatus.LIVE for item in proofs))
            self.assertEqual(proofs[0].decay_reason, "bundle_bytes_mismatch")

    def test_json_boolean_require_signed_without_keys_is_inconclusive(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dyro-bundle-") as tmp:
            root = Path(tmp)
            work, sha = _git_head(root)
            proof_id = "e" * 64
            payload = {
                "bytes_sha256": "f" * 64,
                "declared_key_ids": [],
                "generation": "1",
                "id": proof_id,
                "kind": "review_verdict",
                "policy_require_signed": {"require_signed_review": True},
                "procedure": "review.md rebind",
                "subject": "TASK-A",
                "substrate": {
                    "attempt_id": "",
                    "contract_hash": "",
                    "extra": {},
                    "plan_sha256": "plan",
                    "repo_heads": {"api": sha},
                },
            }
            body = json.dumps(payload, sort_keys=True) + "\n"
            bundle = root / "bool.zip"
            manifest = {
                "kind": "dyro.proof.bundle",
                "proof_ids": [proof_id],
                "proof_sha256": {proof_id: hashlib.sha256(body.encode("utf-8")).hexdigest()},
                "schema_version": 1,
            }
            with zipfile.ZipFile(bundle, "w") as archive:
                archive.writestr("manifest.json", json.dumps(manifest) + "\n")
                archive.writestr(f"proofs/{proof_id}.json", body)
            proofs = verify_bundle(bundle, git_dirs=(work,))
            self.assertEqual(proofs[0].status, ProofStatus.INCONCLUSIVE)
            self.assertEqual(proofs[0].decay_reason, "missing_declared_keys")
            self.assertIsNot(proofs[0].status, ProofStatus.LIVE)

    def test_gate_log_ignores_workspace_signed_review_policy(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dyro-bundle-") as tmp:
            root = Path(tmp)
            work, sha = _git_head(root)
            proof = Proof(
                id="a" * 64,
                kind=ProofKind.GATE_LOG,
                subject="TASK-A",
                substrate=ProofSubstrate(repo_heads=(("api", sha),), plan_sha256="plan"),
                procedure="logs/gate-n.log",
                bytes_sha256="b" * 64,
                generation="1",
                status=ProofStatus.INCONCLUSIVE,
                policy_require_signed=(("require_signed_review", "true"),),
                declared_key_ids=(),
            )
            bundle = root / "gate.zip"
            export_bundle((proof,), bundle)
            proofs = verify_bundle(bundle, git_dirs=(work,))
            self.assertEqual(proofs[0].status, ProofStatus.LIVE)

    def test_export_strips_workspace_rebind_status(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dyro-bundle-") as tmp:
            bundle = Path(tmp) / "portable.zip"
            export_bundle(
                (_proof(status=ProofStatus.DECAYED, decay_reason="review_acceptance"),),
                bundle,
            )
            with zipfile.ZipFile(bundle) as archive:
                payload = json.loads(archive.read(f"proofs/{'a' * 64}.json"))
            self.assertEqual(payload["status"], "inconclusive")
            self.assertEqual(payload["decay_reason"], "")
            self.assertEqual(payload["observed_at"], "")

    def test_evidence_markers_with_manifest_are_inconclusive(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dyro-bundle-") as tmp:
            bundle = Path(tmp) / "mixed.zip"
            with zipfile.ZipFile(bundle, "w") as archive:
                archive.writestr("receipt.md", "result: DONE\n")
                archive.writestr(
                    "manifest.json",
                    json.dumps({"kind": "dyro.proof.bundle", "schema_version": 1, "proof_ids": []}) + "\n",
                )
            proofs = verify_bundle(bundle, git_dirs=())
            self.assertEqual(proofs[0].status, ProofStatus.INCONCLUSIVE)
            self.assertEqual(proofs[0].kind, ProofKind.BUNDLE_FAILURE)
            self.assertIsNot(proofs[0].status, ProofStatus.LIVE)

    def test_short_sha_is_unresolved(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dyro-bundle-") as tmp:
            root = Path(tmp)
            work, sha = _git_head(root)
            bundle = root / "short.zip"
            export_bundle((_proof(heads=(("api", sha[:7]),)),), bundle)
            proofs = verify_bundle(bundle, git_dirs=(work,))
            self.assertEqual(proofs[0].status, ProofStatus.INCONCLUSIVE)
            self.assertEqual(proofs[0].decay_reason, "object_unresolved")


if __name__ == "__main__":
    unittest.main()


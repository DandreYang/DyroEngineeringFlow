from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from dyro import signing
from dyro.errors import ValidationError
from dyro.signing import (
    generate_keypair,
    read_trust_audit,
    revoke_public_key,
    sign_record,
    trust_public_key,
    trusted_key_ids,
    trusted_key_principal,
    trusted_key_records,
    trusted_keys_directory,
    verify_record,
)


class Ed25519SigningTests(unittest.TestCase):
    def test_concurrent_delivery_trust_rejects_cross_purpose_key_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private_key = root / "shared.private.pem"
            public_key = root / "shared.public.pem"
            generate_keypair("shared", private_key=private_key, public_key=public_key)
            first_metadata_install = threading.Event()
            original_install = signing._atomic_install

            def delayed_install(path: Path, content: bytes, mode: int = 0o600) -> None:
                if path.name.endswith(".metadata.json") and not first_metadata_install.is_set():
                    first_metadata_install.set()
                    time.sleep(0.2)
                original_install(path, content, mode)

            def trust(purpose: str) -> str:
                try:
                    trust_public_key(
                        root,
                        f"shared-{purpose}",
                        purpose=purpose,
                        source=public_key,
                        principal_id=f"{purpose}-principal",
                    )
                    return "trusted"
                except ValidationError:
                    return "rejected"

            with patch("dyro.signing._atomic_install", side_effect=delayed_install):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    first = pool.submit(trust, "execution")
                    self.assertTrue(first_metadata_install.wait(timeout=2.0))
                    second = pool.submit(trust, "review")
                    outcomes = (first.result(timeout=5.0), second.result(timeout=5.0))

            self.assertEqual(sorted(outcomes), ["rejected", "trusted"])

    def test_delivery_key_principal_is_immutable_and_a_key_cannot_cross_delivery_purposes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private_key = root / "runner-private.pem"
            public_key = root / "runner-public.pem"
            generate_keypair("runner", private_key=private_key, public_key=public_key)

            trust_public_key(
                root,
                "runner",
                purpose="execution",
                source=public_key,
                principal_id="runner-principal",
            )
            self.assertEqual(trusted_key_principal(root, "execution", "runner"), "runner-principal")

            with self.assertRaisesRegex(ValidationError, "principal"):
                trust_public_key(
                    root,
                    "runner",
                    purpose="execution",
                    source=public_key,
                    principal_id="other-principal",
                )
            with self.assertRaisesRegex(ValidationError, "用途"):
                trust_public_key(
                    root,
                    "runner-review",
                    purpose="review",
                    source=public_key,
                    principal_id="runner-principal",
                )

    def test_signed_record_verifies_and_tampering_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private_key = root / "runner-private.pem"
            public_key = root / "runner-public.pem"
            generate_keypair("runner-2026", private_key=private_key, public_key=public_key)
            trust_public_key(
                root,
                "runner-2026",
                purpose="execution",
                source=public_key,
            )
            signed = sign_record(
                {"schema_version": 1, "task_id": "A", "result": "DONE"},
                purpose="execution",
                key_id="runner-2026",
                private_key=private_key,
            )

            self.assertTrue(
                verify_record(
                    signed,
                    purpose="execution",
                    trust_directory=trusted_keys_directory(root, "execution"),
                )
            )
            tampered = dict(signed)
            tampered["result"] = "QUESTION"
            with self.assertRaisesRegex(ValidationError, "验证失败"):
                verify_record(
                    tampered,
                    purpose="execution",
                    trust_directory=trusted_keys_directory(root, "execution"),
                )

    def test_configured_trust_store_rejects_unsigned_and_cross_purpose_records(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private_key = root / "private.pem"
            public_key = root / "public.pem"
            generate_keypair("key-1", private_key=private_key, public_key=public_key)
            trust_public_key(root, "key-1", purpose="execution", source=public_key)
            unsigned = {"schema_version": 1, "task_id": "A"}

            with self.assertRaisesRegex(ValidationError, "策略要求"):
                verify_record(
                    unsigned,
                    purpose="execution",
                    trust_directory=trusted_keys_directory(root, "execution"),
                    required=True,
                )
            signed = sign_record(
                unsigned,
                purpose="execution",
                key_id="key-1",
                private_key=private_key,
            )
            signoff_private = root / "signoff-private.pem"
            signoff_public = root / "signoff-public.pem"
            generate_keypair("signoff-1", private_key=signoff_private, public_key=signoff_public)
            trust_public_key(root, "signoff-1", purpose="signoff", source=signoff_public)
            with self.assertRaisesRegex(ValidationError, "envelope"):
                verify_record(
                    signed,
                    purpose="signoff",
                    trust_directory=trusted_keys_directory(root, "signoff"),
                )

    def test_multiple_key_ids_support_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for key_id in ("runner-old", "runner-new"):
                private_key = root / f"{key_id}.private.pem"
                public_key = root / f"{key_id}.public.pem"
                generate_keypair(key_id, private_key=private_key, public_key=public_key)
                trust_public_key(root, key_id, purpose="execution", source=public_key)
                signed = sign_record(
                    {"schema_version": 1, "key": key_id},
                    purpose="execution",
                    key_id=key_id,
                    private_key=private_key,
                )
                self.assertTrue(
                    verify_record(
                        signed,
                        purpose="execution",
                        trust_directory=trusted_keys_directory(root, "execution"),
                    )
                )

            self.assertEqual(
                trusted_key_ids(root, "execution"),
                ("runner-new", "runner-old"),
            )

    def test_revoked_and_out_of_window_keys_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private_key = root / "private.pem"
            public_key = root / "public.pem"
            generate_keypair("runner", private_key=private_key, public_key=public_key)
            trust_public_key(
                root,
                "runner",
                purpose="execution",
                source=public_key,
                not_after="2999-01-01T00:00:00+00:00",
            )
            signed = sign_record(
                {"schema_version": 1, "task_id": "A"},
                purpose="execution",
                key_id="runner",
                private_key=private_key,
            )
            self.assertTrue(
                verify_record(
                    signed,
                    purpose="execution",
                    trust_directory=trusted_keys_directory(root, "execution"),
                )
            )

            revoke_public_key(root, "runner", purpose="execution", reason="runner retired")

            with self.assertRaisesRegex(ValidationError, "revoked"):
                verify_record(
                    signed,
                    purpose="execution",
                    trust_directory=trusted_keys_directory(root, "execution"),
                )
            self.assertEqual(trusted_key_ids(root, "execution"), ())
            self.assertEqual(trusted_key_records(root, "execution")[0]["status"], "revoked")
            self.assertEqual([record["event"] for record in read_trust_audit(root)], ["trust", "revoke"])

    def test_future_key_and_wide_private_key_permissions_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private_key = root / "private.pem"
            public_key = root / "public.pem"
            generate_keypair("future", private_key=private_key, public_key=public_key)
            trust_public_key(
                root,
                "future",
                purpose="execution",
                source=public_key,
                not_before="2999-01-01T00:00:00+00:00",
            )
            private_key.chmod(0o644)
            with self.assertRaisesRegex(ValidationError, "0600"):
                sign_record(
                    {"schema_version": 1},
                    purpose="execution",
                    key_id="future",
                    private_key=private_key,
                )
            private_key.chmod(0o600)
            signed = sign_record(
                {"schema_version": 1},
                purpose="execution",
                key_id="future",
                private_key=private_key,
            )
            with self.assertRaisesRegex(ValidationError, "pending"):
                verify_record(
                    signed,
                    purpose="execution",
                    trust_directory=trusted_keys_directory(root, "execution"),
                )

    def test_trust_rejects_dangling_symlink_without_following_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private_key = root / "private.pem"
            public_key = root / "public.pem"
            generate_keypair("runner", private_key=private_key, public_key=public_key)
            directory = trusted_keys_directory(root, "execution")
            directory.mkdir(parents=True)
            outside = root / "outside.pem"
            (directory / "runner.pem").symlink_to(outside)

            with self.assertRaisesRegex(ValidationError, "不能是符号链接"):
                trust_public_key(root, "runner", purpose="execution", source=public_key)
            self.assertFalse(outside.exists())


if __name__ == "__main__":
    unittest.main()

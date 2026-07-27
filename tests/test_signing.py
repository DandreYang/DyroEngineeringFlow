from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dyro.errors import ValidationError
from dyro.signing import (
    generate_keypair,
    sign_record,
    trust_public_key,
    trusted_key_ids,
    trusted_keys_directory,
    verify_record,
)


class Ed25519SigningTests(unittest.TestCase):
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
            trust_public_key(root, "key-1", purpose="signoff", source=public_key)
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


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from dyro.canonical import canonical_json_text
from dyro.signing import sign_record


class TypeScriptInteropVectorTests(unittest.TestCase):
    def test_python_matches_typescript_reference_vector(self) -> None:
        fixture_path = (
            Path(__file__).parents[1]
            / "examples"
            / "typescript-runner"
            / "fixtures"
            / "interop-vector.json"
        )
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        self.assertEqual(canonical_json_text(fixture["record"]), fixture["canonical_json"])

        with tempfile.TemporaryDirectory() as temporary:
            private_key = Path(temporary) / "fixture.pem"
            private_key.write_text(fixture["private_key_pem"], encoding="utf-8")
            private_key.chmod(0o600)
            signed = sign_record(
                fixture["record"],
                purpose=fixture["purpose"],
                key_id=fixture["key_id"],
                private_key=private_key,
            )

        self.assertEqual(
            signed["signature"]["value"],
            fixture["expected_signature_base64"],
        )


if __name__ == "__main__":
    unittest.main()

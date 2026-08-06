from __future__ import annotations

import unittest

from dyro.bridge.models import ErrorCode, OperationKind
from dyro.bridge.redaction import (
    REDACTED_TEXT,
    contains_sensitive_text,
    normalize_error,
    redact_operation_data,
    safe_request_id,
)


class BridgeRedactionTests(unittest.TestCase):
    def test_safe_request_id_keeps_only_bounded_correlation_text(self) -> None:
        self.assertEqual(safe_request_id("req-01:retry.2"), ("req-01:retry.2", False))
        for unsafe in (
            "Bearer abcdefghijklmnop",
            "sk-abcdefghijklmnop",
            "/Users/example/private",
            "https://user:password@example.test",
            "ssh://user:password@example.test/repo",
            "file:///root/private",
            "\\\\server\\private\\file",
            "See /root/private/secret",
            "remote https://example.test/repo?access_token=value",
            "glpat-abcdefghijklmnop",
            "xoxb-1234567890-secret",
            "--token=abcdefghijklmnop",
            "--token abcdefghijklmnop",
            "--password hunter2",
            "--api-key abcdefghijklmnop",
            "Cookie: session=abcdefghijklmnop",
            "SK" + "a" * 32,
            "Basic dXNlcjpwYXNzd29yZA==",
            "redis://:password@localhost/0",
            "sk_live_abcdefghijklmnopqrstuv",
            "SG.abcdefghijklmnop.qrstuvwxyz123456",
            "pypi-AgEIcHlwaS5vcmcCJGFiY2RlZmdoaWprbG1ub3BxcnN0dXZ3eHl6",
            "npm_abcdefghijklmnopqrstuvwxyz012345",
            "github_pat_" + "A" * 40,
            "sk_live_" + "A" * 32,
            "npm_" + "A" * 36,
            "pypi-" + "A" * 40,
            "-----BEGIN PGP PRIVATE KEY BLOCK-----",
            "path:/root/private",
            "line one\nline two",
            "x" * 129,
        ):
            self.assertEqual(safe_request_id(unsafe), (None, True))
        self.assertTrue(contains_sensitive_text("Basic dXNlcjpwYXNzd29yZA=="))

    def test_r0_redacts_sensitive_keys_and_values(self) -> None:
        clean, changed = redact_operation_data(
            "workspace.observe",
            OperationKind.INSPECT,
            {
                "password": "not-returned",
                "passwd": "not-returned",
                "cookie": "session=abcdefghijklmnop",
                "access_key": "not-returned",
                "nested": ["safe", "Bearer abcdefghijklmnop"],
                "path": "/Users/example/workspace",
                "basic": "Basic dXNlcjpwYXNzd29yZA==",
                "remote": "Clone git@example.com:org/repo",
            },
        )
        self.assertTrue(changed)
        self.assertEqual(clean["password"], REDACTED_TEXT)
        self.assertEqual(clean["passwd"], REDACTED_TEXT)
        self.assertEqual(clean["cookie"], REDACTED_TEXT)
        self.assertEqual(clean["access_key"], REDACTED_TEXT)
        self.assertEqual(clean["nested"], ["safe", REDACTED_TEXT])
        self.assertEqual(clean["path"], REDACTED_TEXT)
        self.assertEqual(clean["basic"], REDACTED_TEXT)
        self.assertEqual(clean["remote"], REDACTED_TEXT)
        for text in (
            "--token abcdefghijklmnop",
            "--password hunter2",
            "--api-key abcdefghijklmnop",
        ):
            clean, changed = redact_operation_data(
                "workspace.observe", OperationKind.INSPECT, {"status": text}
            )
            self.assertTrue(changed)
            self.assertEqual(clean["status"], REDACTED_TEXT)

    def test_plan_output_is_never_rewritten(self) -> None:
        with self.assertRaises(ValueError):
            redact_operation_data(
                "objective.plan",
                OperationKind.PLAN,
                {"projection": {"authorization": "Bearer abcdefghijklmnop"}},
            )

    def test_schema_discovery_is_generated_data_not_text_redacted(self) -> None:
        data = {"input_schema": {"$schema": "https://json-schema.org/test"}}
        self.assertEqual(
            redact_operation_data(
                "bridge.operation.schema", OperationKind.INSPECT, data
            ),
            (data, False),
        )

    def test_benign_secret_prefix_ids_are_not_false_positives(self) -> None:
        for value in (
            "npm_install",
            "pypi_publish",
            "sk_feature",
            "pk_release",
            "rk_review",
            "ghp_branch",
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    redact_operation_data(
                        "workspace.observe",
                        OperationKind.INSPECT,
                        {"status": value},
                    ),
                    ({"status": value}, False),
                )
        for value in ("Basic authentication", "Bearer authentication"):
            self.assertEqual(
                redact_operation_data(
                    "workspace.observe", OperationKind.INSPECT, {"status": value}
                ),
                ({"status": value}, False),
            )

    def test_normalized_error_never_accepts_exception_text(self) -> None:
        secret = "sk-abcdefghijklmnop"
        for code in ErrorCode:
            rendered = normalize_error(code).as_dict()
            self.assertNotIn(secret, str(rendered))
            self.assertEqual(rendered["code"], code.value)
            self.assertEqual(rendered["details"], {})
        self.assertEqual(
            normalize_error(ErrorCode.PROTOCOL_MINOR_UNSUPPORTED).next_actions, ()
        )
        self.assertEqual(
            normalize_error(ErrorCode.PROTOCOL_MAJOR_UNSUPPORTED).next_actions, ()
        )


if __name__ == "__main__":
    unittest.main()

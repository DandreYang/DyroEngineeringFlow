from __future__ import annotations

import unittest

from dyro.bridge.redaction import echo_request_id, looks_like_absolute_path, looks_sensitive


class BridgeRedactionTests(unittest.TestCase):
    def test_safe_request_id_is_echoed(self) -> None:
        echoed, redacted = echo_request_id("client-1")
        self.assertEqual(echoed, "client-1")
        self.assertFalse(redacted)

    def test_path_and_secret_request_ids_are_redacted(self) -> None:
        for value in ("/tmp/secret", "C:\\Windows\\x", "bearer abc.def", "https://u:p@host/x"):
            echoed, redacted = echo_request_id(value)
            self.assertIsNone(echoed)
            self.assertTrue(redacted)
        self.assertTrue(looks_like_absolute_path("/Users/example/project"))
        self.assertTrue(looks_sensitive("password=hunter2"))
        self.assertFalse(looks_sensitive("client-1"))

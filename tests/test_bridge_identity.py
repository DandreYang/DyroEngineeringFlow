from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from dyro.bridge.identity import (
    CONFIG_REVISION_DOMAIN,
    PROFILE_MAX_BYTES,
    WORKSPACE_IDENTITY_DOMAIN,
    config_revision_v1,
    workspace_identity_v1,
)
from dyro.errors import ValidationError


class BridgeIdentityTests(unittest.TestCase):
    def test_identity_changes_when_root_or_name_changes(self) -> None:
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            left = Path(first)
            right = Path(second)
            same = workspace_identity_v1(canonical_root=left, profile_name="alpha")
            again = workspace_identity_v1(canonical_root=left, profile_name="alpha")
            renamed = workspace_identity_v1(canonical_root=left, profile_name="beta")
            moved = workspace_identity_v1(canonical_root=right, profile_name="alpha")
            self.assertEqual(same, again)
            self.assertTrue(same.startswith("workspace:"))
            self.assertNotEqual(same, renamed)
            self.assertNotEqual(same, moved)
            self.assertNotIn(str(left), same)

    def test_config_revision_uses_exact_bytes_and_domain(self) -> None:
        first = config_revision_v1(b'schema_version = 1\n')
        commented = config_revision_v1(b'schema_version = 1\n# note\n')
        self.assertNotEqual(first, commented)
        self.assertEqual(len(first), 64)
        self.assertTrue(WORKSPACE_IDENTITY_DOMAIN.startswith(b"dyro.workspace.identity/v1"))
        self.assertTrue(CONFIG_REVISION_DOMAIN.startswith(b"dyro.config.raw/v1"))

    def test_oversized_profile_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValidationError, "字节上限"):
            config_revision_v1(b"x" * (PROFILE_MAX_BYTES + 1))

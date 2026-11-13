"""WorkspaceIdentityV1 and ConfigRevisionV1. Not credentials."""

from __future__ import annotations

from pathlib import Path

from ..canonical import canonical_json_bytes
from ..errors import ValidationError

WORKSPACE_IDENTITY_DOMAIN = b"dyro.workspace.identity/v1\0"
CONFIG_REVISION_DOMAIN = b"dyro.config.raw/v1\0"
PROFILE_MAX_BYTES = 1_048_576


def workspace_identity_v1(*, canonical_root: Path, profile_name: str) -> str:
    """Return ``workspace:<hex>``. Changing root or name changes the value."""
    if not isinstance(canonical_root, Path):
        raise ValidationError("canonical_root 必须是路径")
    if not isinstance(profile_name, str) or not profile_name.strip():
        raise ValidationError("profile_name 必须是非空字符串")
    payload = {
        "canonical_root": canonical_root.resolve().as_posix(),
        "profile_name": profile_name.strip(),
    }
    digest = _sha256(WORKSPACE_IDENTITY_DOMAIN + canonical_json_bytes(payload))
    return f"workspace:{digest}"


def config_revision_v1(profile_bytes: bytes) -> str:
    """Hash exact Profile bytes after the file is proven bounded."""
    if not isinstance(profile_bytes, (bytes, bytearray)):
        raise ValidationError("Profile 必须是字节")
    if len(profile_bytes) > PROFILE_MAX_BYTES:
        raise ValidationError("Profile 超过 Phase 0 字节上限")
    return _sha256(CONFIG_REVISION_DOMAIN + bytes(profile_bytes))


def _sha256(payload: bytes) -> str:
    import hashlib

    return hashlib.sha256(payload).hexdigest()

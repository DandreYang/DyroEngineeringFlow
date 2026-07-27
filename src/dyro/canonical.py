from __future__ import annotations

import rfc8785

from .errors import ValidationError


def canonical_json_bytes(value: object) -> bytes:
    """Return RFC 8785 JSON Canonicalization Scheme bytes."""
    try:
        return rfc8785.dumps(value)
    except rfc8785.CanonicalizationError as exc:
        raise ValidationError(f"记录无法按 RFC 8785 JCS 规范化：{exc}") from exc


def canonical_json_text(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")

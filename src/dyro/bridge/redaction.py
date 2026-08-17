"""Boundary redaction for request IDs, paths, credentials, and argv."""

from __future__ import annotations

import re

REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_URL_CREDENTIAL = re.compile(r"://[^/\s:]+:[^/\s@]+@")
_SECRET = re.compile(
    r"(?i)(api[_-]?key|password|secret|authorization|bearer\s+[A-Za-z0-9._\-+=/]+)"
)
_MAX_MESSAGE = 4096


def looks_like_absolute_path(value: str) -> bool:
    if value.startswith("/") and len(value) > 1:
        return True
    return (
        len(value) >= 3
        and value[0].isalpha()
        and value[1] == ":"
        and value[2] in "\\/"
    )


def looks_sensitive(value: str) -> bool:
    if looks_like_absolute_path(value):
        return True
    if _URL_CREDENTIAL.search(value) or _SECRET.search(value):
        return True
    return False


def echo_request_id(value: object) -> tuple[str | None, bool]:
    """Return a safe request_id and whether it was redacted."""
    if not isinstance(value, str) or not REQUEST_ID_RE.fullmatch(value):
        return None, True
    if looks_sensitive(value):
        return None, True
    return value, False


def presentation_message(text: str) -> str:
    cleaned = "".join(ch for ch in text if ch >= " " or ch in "\t")
    if len(cleaned) > _MAX_MESSAGE:
        return cleaned[:_MAX_MESSAGE]
    return cleaned

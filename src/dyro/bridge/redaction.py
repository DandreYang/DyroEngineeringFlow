"""Fail-closed presentation and redaction at the Agent Bridge boundary."""

from __future__ import annotations

import base64
import binascii
import re
from typing import Mapping

from .models import (
    BridgeError,
    BridgeNextAction,
    ErrorCode,
    NextActionKind,
    OperationKind,
)


REDACTED_TEXT = "[REDACTED]"

_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_BASIC_AUTH = re.compile(
    r"(?i)\bbasic\s+([A-Za-z0-9+/]{8,}={0,2})(?=$|[^A-Za-z0-9+/=])"
)
_BEARER_AUTH = re.compile(r"(?i)\bbearer\s+([A-Za-z0-9._~+/=-]{8,})\b")
_SENSITIVE_KEY = re.compile(
    r"(?:access[_-]?key|api[_-]?key|authorization|cookie|credential|passwd|password|private[_-]?key|secret|token)",
    re.IGNORECASE,
)
_SENSITIVE_TEXT = (
    re.compile(
        r"-----BEGIN (?:[A-Z ]*PRIVATE KEY|PGP PRIVATE KEY BLOCK)-----",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(
        r"(?i)\b(?:gh[pous]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
        r"(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}|"
        r"glpat-[A-Za-z0-9_-]{12,}|npm_[A-Za-z0-9]{20,}|"
        r"pypi-[A-Za-z0-9_-]{20,}|AIza[0-9A-Za-z_-]{30,})\b"
    ),
    re.compile(r"\bya29\.[A-Za-z0-9._-]{8,}\b"),
    re.compile(r"\bSK[0-9a-fA-F]{32}\b"),
    re.compile(
        r"\b(?:gh[opsu]_[A-Za-z0-9]{20,}|glpat-[A-Za-z0-9_-]{12,}|"
        r"sk-[A-Za-z0-9_-]{16,}|sk_(?:live|test)_[A-Za-z0-9]{16,}|"
        r"npm_[A-Za-z0-9]{20,}|pypi-[A-Za-z0-9_-]{20,}|"
        r"xox[baprs]-[A-Za-z0-9-]{10,})\b"
    ),
    re.compile(r"\bSG\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"(?i)[a-z][a-z0-9+.-]*://[^/@\s:]*:[^/@\s]+@"),
    re.compile(r"(?i)[a-z][a-z0-9+.-]*://[^\s?#]+\?[^\s#]+"),
    re.compile(
        r"(?i)\b(?:cookie|password|passwd|api[_-]?key|secret|token)\s*[=:]\s*\S+"
    ),
    re.compile(
        r"(?i)(?:--)?(?:password|passwd|api[_-]?key|secret|token)"
        r"\s+[A-Za-z0-9._~+/=-]{6,}"
    ),
    re.compile(
        r"(?i)(?:^|[\s\"'(=:])(?:/[^\s]+|[A-Za-z]:[\\/][^\s]+|\\\\[^\s]+|file://[^\s]+)"
    ),
    re.compile(r"(?:^|\s)[^@\s]+@[^:\s]+:[^\s]+"),
)

_ERROR_MESSAGES = {
    ErrorCode.INVALID_JSON: "The request is not one valid JSON object.",
    ErrorCode.REQUEST_TOO_LARGE: "The request exceeds the transport limit.",
    ErrorCode.PROTOCOL_MAJOR_UNSUPPORTED: "The protocol major is unsupported.",
    ErrorCode.PROTOCOL_MINOR_UNSUPPORTED: "The protocol minor is unsupported.",
    ErrorCode.SCHEMA_VALIDATION_FAILED: "The request does not match its schema.",
    ErrorCode.OPERATION_UNKNOWN: "The requested operation is unknown.",
    ErrorCode.OPERATION_UNAVAILABLE: "The requested operation is unavailable.",
    ErrorCode.LOCAL_PROFILE_INVALID: "The local Dyro Profile is invalid.",
    ErrorCode.REGISTRY_INVALID: "The workspace registry is invalid.",
    ErrorCode.WORKSPACE_NOT_REGISTERED: "The selected workspace is not registered.",
    ErrorCode.REGISTERED_ROOT_STALE: "The registered workspace root is unavailable.",
    ErrorCode.HOST_READ_PERMISSION_REQUIRED: "Host read permission is required.",
    ErrorCode.AMBIGUOUS_WORKSPACE: "Multiple workspaces require an explicit selection.",
    ErrorCode.WORKSPACE_NOT_FOUND: "No usable Dyro workspace was found.",
    ErrorCode.OBSERVATION_PARTIAL: "The required observation is partial.",
    ErrorCode.RESOURCE_LIMIT_EXCEEDED: "The operation resource limit was exceeded.",
    ErrorCode.OBSERVATION_DEADLINE_EXCEEDED: "The observation deadline was exceeded.",
    ErrorCode.RECORD_INVALID: "The requested workspace record is invalid.",
    ErrorCode.INTERNAL_ERROR: "The operation failed.",
}


def normalize_error(code: ErrorCode) -> BridgeError:
    """Build one fixed error without copying exception text or raw details."""
    actions: tuple[BridgeNextAction, ...] = ()
    if code in {
        ErrorCode.INVALID_JSON,
        ErrorCode.REQUEST_TOO_LARGE,
        ErrorCode.SCHEMA_VALIDATION_FAILED,
    }:
        actions = (
            BridgeNextAction(NextActionKind.INSPECT_INPUT, "Inspect the request input"),
        )
    elif code is ErrorCode.LOCAL_PROFILE_INVALID:
        actions = (
            BridgeNextAction(
                NextActionKind.INSPECT_PROFILE, "Inspect the local Profile"
            ),
        )
    elif code is ErrorCode.AMBIGUOUS_WORKSPACE:
        actions = (
            BridgeNextAction(NextActionKind.SELECT_WORKSPACE, "Select one workspace"),
        )
    elif code in {ErrorCode.REGISTRY_INVALID, ErrorCode.REGISTERED_ROOT_STALE}:
        actions = (
            BridgeNextAction(
                NextActionKind.CHECK_REGISTRY, "Inspect the workspace registry"
            ),
        )
    elif code is ErrorCode.HOST_READ_PERMISSION_REQUIRED:
        actions = (
            BridgeNextAction(NextActionKind.GRANT_HOST_READ, "Grant host read access"),
        )
    elif code in {ErrorCode.OPERATION_UNAVAILABLE, ErrorCode.OBSERVATION_PARTIAL}:
        actions = (BridgeNextAction(NextActionKind.RETRY, "Retry the operation"),)
    return BridgeError(code=code, message=_ERROR_MESSAGES[code], next_actions=actions)


def contains_sensitive_text(value: str) -> bool:
    bearer = _BEARER_AUTH.search(value)
    if bearer is not None and bearer.group(1).lower() not in {
        "authentication",
        "credentials",
    }:
        return True
    basic = _BASIC_AUTH.search(value)
    if basic is not None:
        try:
            decoded = base64.b64decode(basic.group(1), validate=True)
        except (binascii.Error, ValueError):
            decoded = b""
        if b":" in decoded:
            return True
    return any(pattern.search(value) for pattern in _SENSITIVE_TEXT)


def safe_request_id(value: object) -> tuple[str | None, bool]:
    """Return a correlation ID only when it is bounded and presentation-safe."""
    if not isinstance(value, str):
        return None, value is not None
    encoded = value.encode("utf-8")
    if len(encoded) > 128 or not _SAFE_REQUEST_ID.fullmatch(value):
        return None, True
    if contains_sensitive_text(value):
        return None, True
    return value, False


def _redact(value: object, *, key: str | None = None) -> tuple[object, bool]:
    if (
        key is not None
        and _SENSITIVE_KEY.search(key)
        and not (key.lower() == "authorization" and value == "none")
    ):
        return REDACTED_TEXT, True
    if isinstance(value, str):
        return (
            (REDACTED_TEXT, True) if contains_sensitive_text(value) else (value, False)
        )
    if isinstance(value, list):
        changed = False
        result: list[object] = []
        for item in value:
            clean, item_changed = _redact(item)
            result.append(clean)
            changed = changed or item_changed
        return result, changed
    if isinstance(value, dict):
        changed = False
        result: dict[str, object] = {}
        for item_key, item in value.items():
            clean, item_changed = _redact(item, key=item_key)
            result[item_key] = clean
            changed = changed or item_changed
        return result, changed
    return value, False


def redact_operation_data(
    operation: str,
    kind: OperationKind,
    data: Mapping[str, object],
) -> tuple[dict[str, object], bool]:
    """Redact R0 data, while refusing to rewrite self-verifying PLAN payloads."""
    if operation == "bridge.operation.schema":
        return dict(data), False
    clean, changed = _redact(dict(data))
    if kind is OperationKind.PLAN and changed:
        raise ValueError("PLAN output requires boundary redaction")
    if not isinstance(clean, dict):
        raise ValueError("operation output is not an object")
    return clean, changed

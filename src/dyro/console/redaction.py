"""Small, explicit display-field whitelist for the local Console."""

from __future__ import annotations

import re
import unicodedata


REDACTED = "REDACTED"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_SAFE_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_CREDENTIAL = re.compile(
    r"(?i)(?:"
    r"(?:token|secret|password|api[_-]?key|authorization)\s*(?:=|:)"
    r"|(?:token|secret|password|api[_-]?key|authorization)\s+[A-Za-z0-9._-]{8,}"
    r"|(?:token|secret|password|api[_-]?key|authorization)[._-][A-Za-z0-9._-]{6,}"
    r"|bearer\s+[A-Za-z0-9._-]{8,}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}"
    r"|(?:sk|rk|pk|ghp|gho|ghs|ghu|github_pat|glpat|npm|pypi|AIza)[_-][A-Za-z0-9._-]{6,}"
    r"|AKIA[A-Z0-9]{16}"
    r"|ya29\.[A-Za-z0-9._-]{8,}"
    r")"
)
_REMOTE = re.compile(r"(?i)(?:[a-z][a-z0-9+.-]*://|git@[^\s:]+:)")
_ABSOLUTE_PATH = re.compile(r"(?:^|[^A-Za-z0-9._-])(?:~|/|[A-Za-z]:[\\/])")


def _safe_text(value: object, *, limit: int) -> str:
    if not isinstance(value, str):
        return REDACTED
    normalized = unicodedata.normalize("NFC", value).strip()
    if (
        not normalized
        or len(normalized) > limit
        or _CONTROL.search(normalized)
        or _CREDENTIAL.search(normalized)
        or _REMOTE.search(normalized)
        or _ABSOLUTE_PATH.search(normalized)
    ):
        return REDACTED
    return normalized


def safe_title(value: object) -> str:
    return _safe_text(value, limit=160)


def safe_id(value: object) -> str:
    return (
        value
        if isinstance(value, str)
        and _SAFE_ID.fullmatch(value)
        and not _CREDENTIAL.search(value)
        else REDACTED
    )


def safe_branch(value: object) -> str:
    if (
        not isinstance(value, str)
        or not _SAFE_BRANCH.fullmatch(value)
        or ".." in value
        or _CREDENTIAL.search(value)
    ):
        return REDACTED
    return value


def safe_sha256(value: object) -> str:
    return value if isinstance(value, str) and _SHA256.fullmatch(value) else REDACTED

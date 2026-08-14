"""Result envelope construction and validation."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Mapping, Sequence

from .context_guard import assert_content_allowed, is_credential_field_name
from .errors import DispatchValidationError
from .locator_verify import EvidenceItem, verify_evidence


@dataclass
class ResultEnvelope:
    schema_version: int
    run_id: str
    status: str
    summary: str
    confidence: str = "medium"
    evidence: list[EvidenceItem] = field(default_factory=list)
    patch_ref: str | None = None
    takeover: str | None = None
    usage: dict[str, object] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    backend: str = ""
    error_code: str = ""
    execution_kind: str = "provider"
    isolation: str = "not-applicable"

    def to_mapping(self) -> dict[str, object]:
        normalized_usage = _normalize_safe_result_value(
            self.usage, "result.usage"
        )
        if type(normalized_usage) is not dict:
            raise DispatchValidationError("result.usage must be an object")
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "status": self.status,
            "summary": self.summary,
            "confidence": self.confidence,
            "evidence": [item.to_mapping() for item in self.evidence],
            "patch_ref": self.patch_ref,
            "takeover": self.takeover,
            "usage": normalized_usage,
            "warnings": list(self.warnings),
            "backend": self.backend,
            "error_code": self.error_code,
            "execution_kind": self.execution_kind,
            "isolation": self.isolation,
            "verified_ratio": (
                sum(1 for e in self.evidence if e.verified is True) / len(self.evidence)
                if self.evidence
                else 0.0
            ),
        }


def build_result(
    *,
    run_id: str,
    status: str,
    summary: str,
    cwd,
    evidence: Sequence[Mapping[str, object]] | None = None,
    confidence: str = "medium",
    patch_ref: str | None = None,
    takeover: str | None = None,
    usage: Mapping[str, object] | None = None,
    warnings: Sequence[str] | None = None,
    backend: str = "",
    error_code: str = "",
    execution_kind: str = "provider",
    isolation: str = "not-applicable",
) -> ResultEnvelope:
    if status not in {"ok", "error", "timeout", "cancelled"}:
        raise DispatchValidationError(f"invalid result status: {status}")
    if confidence not in {"high", "medium", "low"}:
        raise DispatchValidationError(f"invalid confidence: {confidence}")
    if execution_kind not in {"provider", "offline-simulation"}:
        raise DispatchValidationError(f"invalid execution_kind: {execution_kind}")
    if isolation not in {"strict", "context-projection", "best-effort-unconfined", "not-applicable"}:
        raise DispatchValidationError(f"invalid isolation: {isolation}")
    _assert_safe_result_text(summary, "result.summary")
    _assert_safe_result_text(backend, "result.backend")
    _assert_safe_result_text(error_code, "result.error_code")
    if patch_ref is not None:
        _assert_safe_result_text(patch_ref, "result.patch_ref")
    if takeover is not None:
        _assert_safe_result_text(takeover, "result.takeover")
    for index, warning in enumerate(warnings or ()):
        if type(warning) is not str:
            raise DispatchValidationError("result.warnings entries must be strings")
        _assert_safe_result_text(warning, f"result.warnings[{index}]")
    for index, item in enumerate(evidence or ()):
        if not isinstance(item, Mapping):
            raise DispatchValidationError("result.evidence entries must be objects")
        for name in ("file", "claim", "lines"):
            value = item.get(name)
            if isinstance(value, str):
                _assert_safe_result_text(value, f"result.evidence[{index}].{name}")
    normalized_usage = _normalize_safe_result_value(
        usage if usage is not None else {}, "result.usage"
    )
    if type(normalized_usage) is not dict:
        raise DispatchValidationError("result.usage must be an object")
    verified = verify_evidence(list(evidence or ()), cwd=cwd)
    return ResultEnvelope(
        schema_version=1,
        run_id=run_id,
        status=status,
        summary=summary,
        confidence=confidence,
        evidence=verified,
        patch_ref=patch_ref,
        takeover=takeover,
        usage=normalized_usage,
        warnings=list(warnings or ()),
        backend=backend,
        error_code=error_code,
        execution_kind=execution_kind,
        isolation=isolation,
    )


def _assert_safe_result_text(value: str, label: str) -> None:
    if type(value) is not str:
        raise DispatchValidationError(f"{label} must be a string")
    if value:
        assert_content_allowed(value, label=label)


def _normalize_safe_result_value(
    value: object,
    label: str,
    *,
    _seen: set[int] | None = None,
    _depth: int = 0,
    _credential_parent: bool = False,
) -> object:
    """Return one validated, plain-JSON snapshot for result metadata."""
    if _depth > 16:
        raise DispatchValidationError(f"{label} exceeds the nesting limit")
    if _credential_parent:
        exact_empty = (
            value is None
            or (type(value) is str and not value)
            or (type(value) in {tuple, list, dict} and len(value) == 0)
        )
        if exact_empty:
            if type(value) is tuple:
                return []
            return value
        raise DispatchValidationError(
            f"secret-like content is not allowed in {label}"
        )
    if type(value) is str:
        _assert_safe_result_text(value, label)
        return value
    if value is None or type(value) in {bool, int}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise DispatchValidationError(f"{label} must be finite")
        return value
    seen = _seen if _seen is not None else set()
    identity = id(value)
    if identity in seen:
        raise DispatchValidationError(f"{label} contains a recursive value")
    seen.add(identity)
    try:
        if type(value) is dict:
            normalized: dict[str, object] = {}
            for key, item in value.items():
                if type(key) is not str:
                    raise DispatchValidationError(
                        f"{label} keys must be strings"
                    )
                _assert_safe_result_text(key, f"{label}.key")
                credential_parent = (
                    _credential_parent or is_credential_field_name(key)
                )
                normalized[key] = _normalize_safe_result_value(
                    item,
                    f"{label}.{key}",
                    _seen=seen,
                    _depth=_depth + 1,
                    _credential_parent=credential_parent,
                )
            return normalized
        if type(value) in {list, tuple}:
            normalized_items: list[object] = []
            for index, item in enumerate(value):
                normalized_items.append(
                    _normalize_safe_result_value(
                        item,
                        f"{label}[{index}]",
                        _seen=seen,
                        _depth=_depth + 1,
                        _credential_parent=_credential_parent,
                    )
                )
            return normalized_items
    finally:
        seen.discard(identity)
    raise DispatchValidationError(f"{label} contains a non-JSON value")


def result_from_mapping(payload: Mapping[str, Any], *, cwd) -> ResultEnvelope:
    if payload.get("schema_version") != 1:
        raise DispatchValidationError("result schema_version must be 1")
    raw_evidence = payload.get("evidence") or []
    if not isinstance(raw_evidence, list):
        raise DispatchValidationError("result.evidence must be a list")
    return build_result(
        run_id=str(payload.get("run_id") or ""),
        status=str(payload.get("status") or "error"),
        summary=str(payload.get("summary") or ""),
        cwd=cwd,
        evidence=raw_evidence,
        confidence=str(payload.get("confidence") or "medium"),
        patch_ref=payload.get("patch_ref") if payload.get("patch_ref") is None else str(payload.get("patch_ref")),
        takeover=payload.get("takeover") if payload.get("takeover") is None else str(payload.get("takeover")),
        usage=payload.get("usage") if isinstance(payload.get("usage"), dict) else {},
        warnings=[str(x) for x in (payload.get("warnings") or [])],
        backend=str(payload.get("backend") or ""),
        error_code=str(payload.get("error_code") or ""),
        execution_kind=str(payload.get("execution_kind") or "provider"),
        isolation=str(payload.get("isolation") or "not-applicable"),
    )

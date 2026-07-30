"""Result envelope construction and validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

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

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "status": self.status,
            "summary": self.summary,
            "confidence": self.confidence,
            "evidence": [item.to_mapping() for item in self.evidence],
            "patch_ref": self.patch_ref,
            "takeover": self.takeover,
            "usage": dict(self.usage),
            "warnings": list(self.warnings),
            "backend": self.backend,
            "error_code": self.error_code,
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
) -> ResultEnvelope:
    if status not in {"ok", "error", "timeout", "cancelled"}:
        raise DispatchValidationError(f"invalid result status: {status}")
    if confidence not in {"high", "medium", "low"}:
        raise DispatchValidationError(f"invalid confidence: {confidence}")
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
        usage=dict(usage or {}),
        warnings=list(warnings or ()),
        backend=backend,
        error_code=error_code,
    )


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
    )

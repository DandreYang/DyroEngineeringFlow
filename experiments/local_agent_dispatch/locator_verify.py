"""Evidence locator verification: path containment + line ranges (ADR-0002)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Mapping, Sequence

from .errors import DispatchValidationError


_LINES_RE = re.compile(r"^(\d+)(?:\s*-\s*(\d+))?$")


@dataclass(frozen=True)
class EvidenceItem:
    file: str
    claim: str
    lines: str | None = None
    verified: bool | None = None
    verify_note: str = ""

    def to_mapping(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "file": self.file,
            "claim": self.claim,
            "verified": self.verified,
            "verify_note": self.verify_note,
        }
        if self.lines is not None:
            payload["lines"] = self.lines
        return payload


def _parse_item(raw: Mapping[str, object]) -> EvidenceItem:
    file = raw.get("file")
    claim = raw.get("claim")
    if type(file) is not str or not file.strip():
        raise DispatchValidationError("evidence.file must be a non-empty string")
    if type(claim) is not str or not claim.strip():
        raise DispatchValidationError("evidence.claim must be a non-empty string")
    lines = raw.get("lines")
    if lines is not None and type(lines) is not str:
        raise DispatchValidationError("evidence.lines must be a string when present")
    return EvidenceItem(file=file.strip(), claim=claim.strip(), lines=lines)


def verify_evidence_item(item: EvidenceItem, *, cwd: Path) -> EvidenceItem:
    root = Path(cwd).resolve()
    # Disallow absolute paths that escape and .. segments.
    candidate = Path(item.file)
    if candidate.is_absolute():
        abs_path = candidate
    else:
        abs_path = (root / candidate).resolve()
    try:
        abs_path.relative_to(root)
    except ValueError:
        return EvidenceItem(
            file=item.file,
            claim=item.claim,
            lines=item.lines,
            verified=False,
            verify_note="file path escapes workspace",
        )
    if not abs_path.is_file():
        return EvidenceItem(
            file=item.file,
            claim=item.claim,
            lines=item.lines,
            verified=False,
            verify_note="file missing or not readable",
        )
    if item.lines is None or item.lines == "":
        return EvidenceItem(
            file=item.file,
            claim=item.claim,
            lines=item.lines,
            verified=True,
            verify_note="",
        )
    match = _LINES_RE.match(item.lines.strip())
    if not match:
        return EvidenceItem(
            file=item.file,
            claim=item.claim,
            lines=item.lines,
            verified=False,
            verify_note=f"unparseable lines: {item.lines}",
        )
    start = int(match.group(1))
    end = int(match.group(2) or match.group(1))
    try:
        total = len(abs_path.read_text(encoding="utf-8").splitlines())
    except (OSError, UnicodeDecodeError):
        return EvidenceItem(
            file=item.file,
            claim=item.claim,
            lines=item.lines,
            verified=False,
            verify_note="file unreadable for line check",
        )
    if start < 1 or end < start or end > total:
        return EvidenceItem(
            file=item.file,
            claim=item.claim,
            lines=item.lines,
            verified=False,
            verify_note=f"line range out of bounds (file has {total} lines)",
        )
    return EvidenceItem(
        file=item.file,
        claim=item.claim,
        lines=item.lines,
        verified=True,
        verify_note="",
    )


def verify_evidence(
    evidence: Sequence[Mapping[str, object]], *, cwd: Path
) -> list[EvidenceItem]:
    return [verify_evidence_item(_parse_item(item), cwd=cwd) for item in evidence]


def verified_ratio(items: Sequence[EvidenceItem]) -> float:
    if not items:
        return 1.0
    ok = sum(1 for item in items if item.verified is True)
    return ok / len(items)

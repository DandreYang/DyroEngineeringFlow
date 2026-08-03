"""Versioned Console DTO primitives.

The mapping returned by :meth:`ConsoleEnvelope.to_payload` is intentionally a
fresh JSON-compatible value.  The immutable dataclass is the internal contract
shared by subsequent HTTP endpoints.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping


CONSOLE_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ConsoleEnvelope:
    captured_at: datetime
    snapshot_sha256: str
    freshness_state: str
    partial: bool
    warnings: tuple[str, ...]
    data: Mapping[str, object]

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": CONSOLE_SCHEMA_VERSION,
            "captured_at": self.captured_at.isoformat(),
            "snapshot_sha256": self.snapshot_sha256,
            "freshness": {
                "state": self.freshness_state,
                "partial": self.partial,
                "warnings": [{"code": code} for code in self.warnings],
            },
            "data": deepcopy(dict(self.data)),
        }

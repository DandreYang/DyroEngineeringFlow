"""Adapter protocol for headless backend CLIs."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Protocol

from ..task_contract import TaskContract


@dataclass
class AdapterResult:
    status: str  # ok | error | timeout
    summary: str
    evidence: list[dict[str, object]] = field(default_factory=list)
    confidence: str = "medium"
    patch_ref: str | None = None
    takeover: str | None = None
    usage: dict[str, object] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    error_code: str = ""
    raw_preview: str = ""
    execution_kind: str = "provider"


class BackendAdapter(Protocol):
    id: str
    command: str
    strict_isolation: bool

    def available(self) -> bool: ...

    def authenticated(self) -> bool: ...

    def run(
        self,
        *,
        contract: TaskContract,
        cwd: Path,
        context_files: Mapping[str, str],
        timeout_seconds: float,
    ) -> AdapterResult: ...

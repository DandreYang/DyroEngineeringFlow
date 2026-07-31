"""Deterministic offline adapter for tests and dry local runs."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

from ..task_contract import TaskContract
from .base import AdapterResult


class EchoAdapter:
    id = "echo"
    command = "echo"
    strict_isolation = True

    def available(self) -> bool:
        return True

    def authenticated(self) -> bool:
        return True

    def run(
        self,
        *,
        contract: TaskContract,
        cwd: Path,
        context_files: Mapping[str, str],
        timeout_seconds: float,
    ) -> AdapterResult:
        del timeout_seconds
        file_list = sorted(context_files.keys())
        evidence: list[dict[str, object]] = []
        for rel in file_list[:5]:
            content = context_files[rel]
            lines = max(1, len(content.splitlines()) or 1)
            evidence.append(
                {
                    "file": rel,
                    "lines": f"1-{min(3, lines)}",
                    "claim": f"context file included for analysis: {rel}",
                }
            )
        summary = (
            f"[echo-adapter] backend=echo mode={contract.mode} strict={contract.strict} "
            f"files={len(file_list)} objective={contract.task.objective[:200]}"
        )
        return AdapterResult(
            status="ok",
            summary=summary,
            evidence=evidence,
            confidence="low",
            warnings=["echo adapter is offline/deterministic; not a real model"],
            usage={"duration_ms": 1, "files": len(file_list)},
            execution_kind="offline-simulation",
        )

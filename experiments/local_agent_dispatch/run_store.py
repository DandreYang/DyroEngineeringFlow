"""Persisted run lifecycle store (ADR-0002 L1)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import secrets
import time
from typing import Any, Mapping

from .errors import DispatchValidationError
from .json_store import atomic_write_json, read_json
from .paths import runs_dir
from .task_contract import TaskContract


RUN_STATUSES = frozenset(
    {"accepted", "running", "completed", "failed", "timeout", "cancelled"}
)


@dataclass
class RunRecord:
    run_id: str
    status: str
    contract: dict[str, object]
    project_root: str
    backend: str
    created_at: float
    updated_at: float
    result: dict[str, object] | None = None
    error: str = ""
    shadow_path: str = ""
    lease_slots: list[str] | None = None
    panel_id: str = ""
    thread_id: str = ""

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "run_id": self.run_id,
            "status": self.status,
            "contract": self.contract,
            "project_root": self.project_root,
            "backend": self.backend,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "result": self.result,
            "error": self.error,
            "shadow_path": self.shadow_path,
            "lease_slots": list(self.lease_slots or ()),
            "panel_id": self.panel_id,
            "thread_id": self.thread_id,
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> RunRecord:
        if payload.get("schema_version") != 1:
            raise DispatchValidationError("run schema_version must be 1")
        status = str(payload.get("status") or "")
        if status not in RUN_STATUSES:
            raise DispatchValidationError(f"invalid run status: {status}")
        contract = payload.get("contract")
        if not isinstance(contract, dict):
            raise DispatchValidationError("run.contract must be an object")
        result = payload.get("result")
        if result is not None and not isinstance(result, dict):
            raise DispatchValidationError("run.result must be an object when set")
        slots = payload.get("lease_slots") or []
        if not isinstance(slots, list):
            raise DispatchValidationError("run.lease_slots must be a list")
        return cls(
            run_id=str(payload["run_id"]),
            status=status,
            contract=contract,
            project_root=str(payload.get("project_root") or ""),
            backend=str(payload.get("backend") or ""),
            created_at=float(payload.get("created_at") or 0),
            updated_at=float(payload.get("updated_at") or 0),
            result=result,
            error=str(payload.get("error") or ""),
            shadow_path=str(payload.get("shadow_path") or ""),
            lease_slots=[str(s) for s in slots],
            panel_id=str(payload.get("panel_id") or ""),
            thread_id=str(payload.get("thread_id") or ""),
        )


class RunStore:
    def __init__(self, home: Path | None = None) -> None:
        self.home = home
        self.root = runs_dir(home)

    def _path(self, run_id: str) -> Path:
        if not run_id or "/" in run_id or ".." in run_id:
            raise DispatchValidationError("invalid run_id")
        return self.root / f"{run_id}.json"

    def create(
        self,
        *,
        contract: TaskContract,
        project_root: Path,
        backend: str,
        panel_id: str = "",
        thread_id: str = "",
    ) -> RunRecord:
        now = time.time()
        run_id = f"run-{secrets.token_hex(8)}"
        record = RunRecord(
            run_id=run_id,
            status="accepted",
            contract=contract.to_mapping(),
            project_root=str(Path(project_root).resolve()),
            backend=backend,
            created_at=now,
            updated_at=now,
            panel_id=panel_id,
            thread_id=thread_id or run_id,
        )
        self.save(record)
        return record

    def save(self, record: RunRecord) -> None:
        if record.status not in RUN_STATUSES:
            raise DispatchValidationError(f"invalid run status: {record.status}")
        record.updated_at = time.time()
        atomic_write_json(self._path(record.run_id), record.to_mapping())

    def load(self, run_id: str) -> RunRecord:
        payload = read_json(self._path(run_id))
        if payload is None:
            raise DispatchValidationError(f"run not found: {run_id}")
        return RunRecord.from_mapping(payload)

    def list_runs(self) -> list[RunRecord]:
        items: list[RunRecord] = []
        for path in sorted(self.root.glob("run-*.json")):
            payload = read_json(path)
            if payload is None:
                continue
            try:
                items.append(RunRecord.from_mapping(payload))
            except DispatchValidationError:
                continue
        return items

    def update_status(
        self,
        run_id: str,
        status: str,
        *,
        error: str = "",
        result: dict[str, object] | None = None,
        shadow_path: str | None = None,
        lease_slots: list[str] | None = None,
    ) -> RunRecord:
        record = self.load(run_id)
        if status not in RUN_STATUSES:
            raise DispatchValidationError(f"invalid run status: {status}")
        record.status = status
        if error:
            record.error = error
        if result is not None:
            record.result = result
        if shadow_path is not None:
            record.shadow_path = shadow_path
        if lease_slots is not None:
            record.lease_slots = lease_slots
        self.save(record)
        return record

    def delete(self, run_id: str) -> None:
        path = self._path(run_id)
        if path.is_file():
            path.unlink()

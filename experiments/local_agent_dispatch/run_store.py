"""Persisted run lifecycle store (ADR-0002 L1)."""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
import secrets
import signal
import time
from typing import Any, Mapping

from .errors import DispatchValidationError
from .file_lock import exclusive_file_lock, file_lock_is_held
from .json_store import atomic_write_json, read_json
from .paths import dispatch_home_path, runs_dir
from .process_identity import process_identity_is_dead, process_started_at
from .task_contract import TaskContract


RUN_STATUSES = frozenset(
    {"accepted", "running", "completed", "failed", "timeout", "cancelled"}
)
TERMINAL_RUN_STATUSES = frozenset(
    {"completed", "failed", "timeout", "cancelled"}
)
ASYNC_RESERVATION_GRACE_SECONDS = 10.0
_POSIX_PROCESS_GROUPS = (
    os.name == "posix"
    and hasattr(os, "getpgid")
    and hasattr(os, "killpg")
    and hasattr(signal, "SIGKILL")
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
    revision: int = 0
    worker_token: str = ""
    worker_pid: int = 0
    worker_started_at: str = ""
    backend_pid: int = 0
    backend_pgid: int = 0
    backend_started_at: str = ""
    backend_lock_path: str = ""

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
            "revision": self.revision,
            "worker_token": self.worker_token,
            "worker_pid": self.worker_pid,
            "worker_started_at": self.worker_started_at,
            "backend_pid": self.backend_pid,
            "backend_pgid": self.backend_pgid,
            "backend_started_at": self.backend_started_at,
            "backend_lock_path": self.backend_lock_path,
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
        revision = payload.get("revision", 0)
        if type(revision) is not int or revision < 0:
            raise DispatchValidationError("run.revision must be a non-negative integer")
        worker_pid = payload.get("worker_pid", 0)
        if type(worker_pid) is not int or worker_pid < 0:
            raise DispatchValidationError(
                "run.worker_pid must be a non-negative integer"
            )
        worker_started_at = payload.get("worker_started_at", "")
        if type(worker_started_at) is not str:
            raise DispatchValidationError(
                "run.worker_started_at must be a string"
            )
        if bool(worker_pid) != bool(worker_started_at):
            raise DispatchValidationError(
                "run worker process identity must be complete"
            )
        backend_pid = payload.get("backend_pid", 0)
        if type(backend_pid) is not int or backend_pid < 0:
            raise DispatchValidationError(
                "run.backend_pid must be a non-negative integer"
            )
        backend_pgid = payload.get("backend_pgid", 0)
        if type(backend_pgid) is not int or backend_pgid < 0:
            raise DispatchValidationError(
                "run.backend_pgid must be a non-negative integer"
            )
        backend_started_at = payload.get("backend_started_at", "")
        backend_lock_path = payload.get("backend_lock_path", "")
        if (
            type(backend_started_at) is not str
            or type(backend_lock_path) is not str
        ):
            raise DispatchValidationError(
                "run backend process identity fields must be strings"
            )
        if bool(backend_pid) != bool(
            backend_pgid and backend_started_at and backend_lock_path
        ):
            raise DispatchValidationError(
                "run backend process identity must be complete"
            )
        if backend_pid and backend_pgid != backend_pid:
            raise DispatchValidationError(
                "run backend must lead its dedicated process group"
            )
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
            revision=revision,
            worker_token=str(payload.get("worker_token") or ""),
            worker_pid=worker_pid,
            worker_started_at=worker_started_at,
            backend_pid=backend_pid,
            backend_pgid=backend_pgid,
            backend_started_at=backend_started_at,
            backend_lock_path=backend_lock_path,
        )


class RunStore:
    def __init__(self, home: Path | None = None, *, create: bool = True) -> None:
        self.home = home
        self.root = (
            runs_dir(home)
            if create
            else dispatch_home_path(home) / "runs"
        )

    def _path(self, run_id: str) -> Path:
        if not run_id or "/" in run_id or ".." in run_id:
            raise DispatchValidationError("invalid run_id")
        return self.root / f"{run_id}.json"

    def _lock_path(self, run_id: str) -> Path:
        self._path(run_id)
        return self.root / f".{run_id}.lock"

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
        with exclusive_file_lock(self._lock_path(record.run_id)):
            self._save_unlocked(record)

    def _save_unlocked(self, record: RunRecord) -> None:
        if record.status not in RUN_STATUSES:
            raise DispatchValidationError(f"invalid run status: {record.status}")
        record.updated_at = time.time()
        atomic_write_json(self._path(record.run_id), record.to_mapping())

    def load(self, run_id: str) -> RunRecord:
        path = self._path(run_id)
        if path.is_symlink():
            raise DispatchValidationError(f"run state is a symbolic link: {run_id}")
        payload = read_json(path)
        if payload is None:
            raise DispatchValidationError(f"run not found: {run_id}")
        return RunRecord.from_mapping(payload)

    def list_runs(self) -> list[RunRecord]:
        items: list[RunRecord] = []
        for path in sorted(self.root.glob("run-*.json")):
            if path.is_symlink():
                continue
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
        expected_worker_token: str | None = None,
    ) -> RunRecord:
        with exclusive_file_lock(self._lock_path(run_id)):
            record = self.load(run_id)
            if status not in RUN_STATUSES:
                raise DispatchValidationError(f"invalid run status: {status}")
            if (
                record.status in TERMINAL_RUN_STATUSES
                and status != record.status
            ):
                raise DispatchValidationError(
                    "terminal run status cannot be changed: "
                    f"{record.status} -> {status}"
                )
            if record.status == "running" and status != "running":
                if (
                    not expected_worker_token
                    or record.worker_token != expected_worker_token
                ):
                    raise DispatchValidationError(
                        "run terminal transition rejected: worker token mismatch"
                    )
                if record.backend_pid:
                    raise DispatchValidationError(
                        "run terminal transition rejected: backend cleanup "
                        "is not proven"
                    )
            record.status = status
            if error:
                record.error = error
            if result is not None:
                record.result = result
            if shadow_path is not None:
                record.shadow_path = shadow_path
            if lease_slots is not None:
                record.lease_slots = lease_slots
            record.revision += 1
            self._save_unlocked(record)
            return record

    def reserve_async_worker(
        self,
        run_id: str,
        *,
        worker_token: str,
    ) -> RunRecord:
        """Bind an accepted run to one parent-spawned worker generation."""
        if not worker_token:
            raise DispatchValidationError("worker token must not be empty")
        with exclusive_file_lock(self._lock_path(run_id)):
            record = self.load(run_id)
            if record.status != "accepted" or record.worker_token:
                raise DispatchValidationError(
                    f"run is not available for async spawn: {run_id} "
                    f"status={record.status}"
                )
            record.worker_token = worker_token
            record.revision += 1
            self._save_unlocked(record)
            return record

    def claim_for_execution(
        self,
        run_id: str,
        *,
        worker_token: str,
        lease_slots: list[str],
        worker_pid: int = 0,
        worker_started_at: str = "",
    ) -> RunRecord:
        if not worker_token:
            raise DispatchValidationError("worker token must not be empty")
        if type(worker_pid) is not int or worker_pid < 0:
            raise DispatchValidationError(
                "worker_pid must be a non-negative integer"
            )
        if type(worker_started_at) is not str:
            raise DispatchValidationError("worker_started_at must be a string")
        if bool(worker_pid) != bool(worker_started_at):
            raise DispatchValidationError(
                "worker process identity must include pid and started_at"
            )
        with exclusive_file_lock(self._lock_path(run_id)):
            record = self.load(run_id)
            if record.status != "accepted":
                raise DispatchValidationError(
                    f"run is not claimable: {run_id} status={record.status}"
                )
            if (
                record.worker_token
                and record.worker_token != worker_token
            ):
                raise DispatchValidationError(
                    "run claim rejected: reserved worker token mismatch"
                )
            record.status = "running"
            record.worker_token = worker_token
            record.worker_pid = worker_pid
            record.worker_started_at = worker_started_at
            record.lease_slots = list(lease_slots)
            record.revision += 1
            self._save_unlocked(record)
            return record

    def bind_backend_process(
        self,
        run_id: str,
        *,
        worker_token: str,
        backend_pid: int,
        backend_pgid: int,
        backend_started_at: str,
    ) -> RunRecord:
        """Persist the exact backend process group before it may exec."""
        if not _POSIX_PROCESS_GROUPS:
            raise DispatchValidationError(
                "backend process tracking requires POSIX process groups"
            )
        if type(backend_pid) is not int or backend_pid <= 0:
            raise DispatchValidationError(
                "backend_pid must be a positive integer"
            )
        if (
            type(backend_pgid) is not int
            or backend_pgid <= 0
            or backend_pgid != backend_pid
        ):
            raise DispatchValidationError(
                "backend must lead a dedicated process group"
            )
        if not backend_started_at:
            raise DispatchValidationError(
                "backend_started_at must not be empty"
            )
        self._path(run_id)
        lifetime_path = self.root / f"{run_id}.backend.lifetime"
        with exclusive_file_lock(self._lock_path(run_id)):
            if file_lock_is_held(lifetime_path) is not True:
                raise DispatchValidationError(
                    "backend lifetime lock is missing, unlocked, or unsafe"
                )
            try:
                live_pgid = os.getpgid(backend_pid)
            except OSError as exc:
                raise DispatchValidationError(
                    "backend process group cannot be verified"
                ) from exc
            if live_pgid != backend_pgid:
                raise DispatchValidationError(
                    "backend process group changed before binding"
                )
            live_started_at = process_started_at(backend_pid)
            if (
                backend_started_at.startswith("unknown-")
                or live_started_at is None
                or live_started_at != backend_started_at
            ):
                raise DispatchValidationError(
                    "backend process generation cannot be verified"
                )
            record = self.load(run_id)
            if (
                record.status != "running"
                or record.worker_token != worker_token
                or record.backend_pid
            ):
                raise DispatchValidationError(
                    "backend process binding rejected: worker ownership changed"
                )
            record.backend_pid = backend_pid
            record.backend_pgid = backend_pgid
            record.backend_started_at = backend_started_at
            record.backend_lock_path = str(lifetime_path)
            record.revision += 1
            self._save_unlocked(record)
            return record

    @staticmethod
    def _process_group_exists(process_group_id: int) -> bool:
        if not _POSIX_PROCESS_GROUPS:
            raise DispatchValidationError(
                "backend cleanup requires POSIX process groups"
            )
        try:
            os.killpg(process_group_id, 0)
        except ProcessLookupError:
            return False
        except (PermissionError, OSError):
            return True
        return True

    def _backend_cleanup_proven(self, record: RunRecord) -> bool:
        if record.backend_pid <= 0:
            return True
        if not _POSIX_PROCESS_GROUPS:
            raise DispatchValidationError(
                "backend cleanup requires POSIX process groups"
            )
        if (
            record.backend_pgid <= 0
            or record.backend_pgid != record.backend_pid
        ):
            return False
        expected_lock = self.root / f"{record.run_id}.backend.lifetime"
        if (
            record.backend_lock_path != str(expected_lock)
            or expected_lock.is_symlink()
        ):
            return False
        lock_held = file_lock_is_held(expected_lock)
        group_exists = self._process_group_exists(record.backend_pgid)
        if lock_held is False and not group_exists:
            return True
        if lock_held is not True:
            return False
        if record.backend_started_at.startswith("unknown-"):
            return False
        live_started_at = process_started_at(record.backend_pid)
        if (
            live_started_at is None
            or live_started_at != record.backend_started_at
        ):
            return False
        try:
            live_pgid = os.getpgid(record.backend_pid)
        except OSError:
            return False
        if live_pgid != record.backend_pgid:
            return False
        try:
            os.killpg(record.backend_pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError:
            return False
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            if file_lock_is_held(expected_lock) is False:
                break
            time.sleep(0.02)
        try:
            os.killpg(record.backend_pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            return False
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if (
                file_lock_is_held(expected_lock) is False
                and not self._process_group_exists(record.backend_pgid)
            ):
                return True
            time.sleep(0.02)
        return (
            file_lock_is_held(expected_lock) is False
            and not self._process_group_exists(record.backend_pgid)
        )

    def cleanup_backend_if_owned(
        self,
        run_id: str,
        *,
        worker_token: str,
    ) -> bool:
        """Terminate and prove cleanup of the exact worker's tracked backend."""
        if not _POSIX_PROCESS_GROUPS:
            raise DispatchValidationError(
                "backend cleanup requires POSIX process groups"
            )
        with exclusive_file_lock(self._lock_path(run_id)):
            record = self.load(run_id)
            if (
                record.status != "running"
                or record.worker_token != worker_token
            ):
                return record.status in TERMINAL_RUN_STATUSES
            if record.backend_pid <= 0:
                return True
            if not self._backend_cleanup_proven(record):
                record.error = (
                    "backend process-group cleanup could not be proven"
                )
                record.revision += 1
                self._save_unlocked(record)
                return False
            lifetime_path = (
                Path(record.backend_lock_path)
                if record.backend_lock_path
                else None
            )
            record.backend_pid = 0
            record.backend_pgid = 0
            record.backend_started_at = ""
            record.backend_lock_path = ""
            record.revision += 1
            self._save_unlocked(record)
            if lifetime_path is not None:
                try:
                    lifetime_path.unlink(missing_ok=True)
                except OSError:
                    pass
            return True

    def reconcile_orphaned_workers(
        self,
        *,
        run_ids: set[str] | None = None,
    ) -> list[str]:
        """Fail running async generations whose recorded process has ended."""
        reconciled: list[str] = []
        for record in self.list_runs():
            if run_ids is not None and record.run_id not in run_ids:
                continue
            if (
                record.status == "accepted"
                and record.worker_token
                and record.worker_pid == 0
                and (
                    not math.isfinite(record.updated_at)
                    or time.time() - record.updated_at
                    >= ASYNC_RESERVATION_GRACE_SECONDS
                )
            ):
                updated = self.fail_if_reserved_worker(
                    record.run_id,
                    worker_token=record.worker_token,
                    error=(
                        "async worker reservation expired before process claim"
                    ),
                )
                if updated.status == "failed":
                    reconciled.append(record.run_id)
                continue
            if (
                record.status != "running"
                or not record.worker_token
                or record.worker_pid <= 0
                or not record.worker_started_at
            ):
                continue
            if not process_identity_is_dead(
                pid=record.worker_pid,
                started_at=record.worker_started_at,
            ):
                continue
            if not self.cleanup_backend_if_owned(
                record.run_id,
                worker_token=record.worker_token,
            ):
                continue
            updated = self.fail_if_active_worker(
                record.run_id,
                worker_token=record.worker_token,
                error=(
                    "worker process exited before publishing a terminal result "
                    f"(pid={record.worker_pid})"
                ),
            )
            if updated.status == "failed":
                reconciled.append(record.run_id)
        return reconciled

    def fail_if_accepted(self, run_id: str, *, error: str) -> RunRecord:
        """Atomically terminalize a worker that exited before claiming its run."""
        with exclusive_file_lock(self._lock_path(run_id)):
            record = self.load(run_id)
            if record.status != "accepted" or record.worker_token:
                return record
            record.status = "failed"
            record.error = error
            record.revision += 1
            self._save_unlocked(record)
            return record

    def fail_if_running(
        self,
        run_id: str,
        *,
        worker_token: str,
        error: str,
    ) -> RunRecord:
        """Fail only the exact worker generation that still owns a running run."""
        with exclusive_file_lock(self._lock_path(run_id)):
            record = self.load(run_id)
            if (
                record.status != "running"
                or record.worker_token != worker_token
                or record.backend_pid
            ):
                return record
            record.status = "failed"
            record.error = error
            record.revision += 1
            self._save_unlocked(record)
            return record

    def fail_if_active_worker(
        self,
        run_id: str,
        *,
        worker_token: str,
        error: str,
    ) -> RunRecord:
        """Fail an accepted/running run only for the exact worker generation."""
        with exclusive_file_lock(self._lock_path(run_id)):
            record = self.load(run_id)
            if (
                record.status not in {"accepted", "running"}
                or record.worker_token != worker_token
                or (record.status == "running" and record.backend_pid)
            ):
                return record
            record.status = "failed"
            record.error = error
            record.revision += 1
            self._save_unlocked(record)
            return record

    def fail_if_reserved_worker(
        self,
        run_id: str,
        *,
        worker_token: str,
        error: str,
    ) -> RunRecord:
        """Fail only an accepted run reserved for the exact worker generation."""
        with exclusive_file_lock(self._lock_path(run_id)):
            record = self.load(run_id)
            if (
                record.status != "accepted"
                or record.worker_token != worker_token
            ):
                return record
            record.status = "failed"
            record.error = error
            record.revision += 1
            self._save_unlocked(record)
            return record

    def delete(self, run_id: str) -> None:
        with exclusive_file_lock(self._lock_path(run_id)):
            path = self._path(run_id)
            if path.is_file():
                path.unlink()

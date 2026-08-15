"""Persisted run lifecycle store (ADR-0002 L1)."""

from __future__ import annotations

from dataclasses import dataclass
import errno
import json
import math
import os
from pathlib import Path
import secrets
import signal
import stat
import time
from typing import Any, Mapping

from .adapters.registry import (
    execution_profile_sha256,
    normalize_execution_profile,
)
from .errors import DispatchValidationError
from .file_lock import exclusive_file_lock, file_lock_is_held
from .json_store import atomic_write_json
from .paths import dispatch_home_path, runs_dir
from .process_identity import (
    process_group_has_live_members,
    process_identity_is_dead,
    process_started_at,
)
from .task_contract import TaskContract


RUN_STATUSES = frozenset(
    {"accepted", "running", "completed", "failed", "timeout", "cancelled"}
)
TERMINAL_RUN_STATUSES = frozenset(
    {"completed", "failed", "timeout", "cancelled"}
)
ASYNC_RESERVATION_GRACE_SECONDS = 10.0
MAX_CANCEL_REASON_CHARS = 500
MAX_ORCHESTRATION_ID_CHARS = 256
MAX_RUN_STATE_BYTES = 2 * 1024 * 1024
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
    cancel_requested_at: float = 0.0
    cancel_reason: str = ""
    orchestration_id: str = ""
    planned_context_sha256: str = ""
    planned_base_head: str = ""
    planned_execution_profile_sha256: str = ""
    planned_execution_profile: dict[str, str] | None = None

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
            "cancel_requested_at": self.cancel_requested_at,
            "cancel_reason": self.cancel_reason,
            "orchestration_id": self.orchestration_id,
            "planned_context_sha256": self.planned_context_sha256,
            "planned_base_head": self.planned_base_head,
            "planned_execution_profile_sha256": (
                self.planned_execution_profile_sha256
            ),
            "planned_execution_profile": dict(
                self.planned_execution_profile or {}
            ),
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
        cancel_requested_at = payload.get("cancel_requested_at", 0.0)
        if (
            isinstance(cancel_requested_at, bool)
            or not isinstance(cancel_requested_at, (int, float))
            or not math.isfinite(float(cancel_requested_at))
            or cancel_requested_at < 0
        ):
            raise DispatchValidationError(
                "run.cancel_requested_at must be a finite non-negative number"
            )
        cancel_reason = payload.get("cancel_reason", "")
        if type(cancel_reason) is not str:
            raise DispatchValidationError("run.cancel_reason must be a string")
        if len(cancel_reason) > MAX_CANCEL_REASON_CHARS:
            raise DispatchValidationError(
                "run.cancel_reason exceeds the character limit"
            )
        if not cancel_requested_at and cancel_reason:
            raise DispatchValidationError(
                "run.cancel_reason requires cancel_requested_at"
            )
        orchestration_id = payload.get("orchestration_id", "")
        if type(orchestration_id) is not str:
            raise DispatchValidationError("run.orchestration_id must be a string")
        if len(orchestration_id) > MAX_ORCHESTRATION_ID_CHARS:
            raise DispatchValidationError(
                "run.orchestration_id exceeds the character limit"
            )
        planned_context_sha256 = payload.get("planned_context_sha256", "")
        if type(planned_context_sha256) is not str or (
            planned_context_sha256
            and (
                len(planned_context_sha256) != 64
                or any(
                    char not in "0123456789abcdef"
                    for char in planned_context_sha256
                )
            )
        ):
            raise DispatchValidationError(
                "run.planned_context_sha256 must be empty or a lowercase SHA-256 digest"
            )
        planned_base_head = payload.get("planned_base_head", "")
        if type(planned_base_head) is not str or (
            planned_base_head
            and (
                len(planned_base_head) not in {40, 64}
                or any(char not in "0123456789abcdef" for char in planned_base_head)
            )
        ):
            raise DispatchValidationError(
                "run.planned_base_head must be empty or a lowercase Git object ID"
            )
        if planned_base_head and (
            not planned_context_sha256 or contract.get("mode") != "edit"
        ):
            raise DispatchValidationError(
                "run.planned_base_head requires edit mode and a planned context digest"
            )
        planned_execution_profile_sha256 = payload.get(
            "planned_execution_profile_sha256", ""
        )
        if type(planned_execution_profile_sha256) is not str or (
            planned_execution_profile_sha256
            and (
                len(planned_execution_profile_sha256) != 64
                or any(
                    char not in "0123456789abcdef"
                    for char in planned_execution_profile_sha256
                )
            )
        ):
            raise DispatchValidationError(
                "run.planned_execution_profile_sha256 must be empty or a "
                "lowercase SHA-256 digest"
            )
        raw_execution_profile = payload.get("planned_execution_profile", {})
        if not isinstance(raw_execution_profile, Mapping):
            raise DispatchValidationError(
                "run.planned_execution_profile must be an object"
            )
        planned_execution_profile = (
            normalize_execution_profile(
                raw_execution_profile,
                backend=str(payload.get("backend") or ""),
            )
            if raw_execution_profile
            else {}
        )
        if bool(planned_execution_profile) != bool(
            planned_execution_profile_sha256
        ) or (
            planned_execution_profile
            and execution_profile_sha256(planned_execution_profile)
            != planned_execution_profile_sha256
        ):
            raise DispatchValidationError(
                "run planned execution profile digest does not match its profile"
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
            cancel_requested_at=float(cancel_requested_at),
            cancel_reason=cancel_reason,
            orchestration_id=orchestration_id,
            planned_context_sha256=planned_context_sha256,
            planned_base_head=planned_base_head,
            planned_execution_profile_sha256=planned_execution_profile_sha256,
            planned_execution_profile=planned_execution_profile,
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
        planned_context_sha256: str = "",
        planned_base_head: str = "",
        planned_execution_profile_sha256: str = "",
        planned_execution_profile: Mapping[str, str] | None = None,
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
            planned_context_sha256=planned_context_sha256,
            planned_base_head=planned_base_head,
            planned_execution_profile_sha256=(
                planned_execution_profile_sha256
            ),
            planned_execution_profile=dict(planned_execution_profile or {}),
        )
        self.save(record)
        return record

    def ensure_created(
        self,
        *,
        run_id: str,
        contract: TaskContract,
        project_root: Path,
        backend: str,
        orchestration_id: str,
        thread_id: str,
        panel_id: str = "",
        planned_context_sha256: str = "",
        planned_base_head: str = "",
        planned_execution_profile_sha256: str = "",
        planned_execution_profile: Mapping[str, str] | None = None,
    ) -> RunRecord:
        """Create one deterministic run, or verify an identical prior create."""
        if type(orchestration_id) is not str or not orchestration_id:
            raise DispatchValidationError("orchestration_id must not be empty")
        if len(orchestration_id) > MAX_ORCHESTRATION_ID_CHARS:
            raise DispatchValidationError(
                "orchestration_id exceeds the character limit"
            )
        if (
            type(planned_context_sha256) is not str
            or (
                planned_context_sha256
                and (
                    len(planned_context_sha256) != 64
                    or any(
                        char not in "0123456789abcdef"
                        for char in planned_context_sha256
                    )
                )
            )
        ):
            raise DispatchValidationError(
                "planned_context_sha256 must be empty or a lowercase SHA-256 digest"
            )
        if (
            type(planned_base_head) is not str
            or (
                planned_base_head
                and (
                    len(planned_base_head) not in {40, 64}
                    or any(
                        char not in "0123456789abcdef"
                        for char in planned_base_head
                    )
                )
            )
        ):
            raise DispatchValidationError(
                "planned_base_head must be empty or a lowercase Git object ID"
            )
        if planned_base_head and (
            not planned_context_sha256 or contract.mode != "edit"
        ):
            raise DispatchValidationError(
                "planned_base_head requires edit mode and a planned context digest"
            )
        if (
            type(planned_execution_profile_sha256) is not str
            or (
                planned_execution_profile_sha256
                and (
                    len(planned_execution_profile_sha256) != 64
                    or any(
                        char not in "0123456789abcdef"
                        for char in planned_execution_profile_sha256
                    )
                )
            )
        ):
            raise DispatchValidationError(
                "planned_execution_profile_sha256 must be empty or a "
                "lowercase SHA-256 digest"
            )
        normalized_execution_profile = (
            normalize_execution_profile(
                planned_execution_profile,
                backend=backend,
            )
            if planned_execution_profile
            else {}
        )
        if bool(normalized_execution_profile) != bool(
            planned_execution_profile_sha256
        ) or (
            normalized_execution_profile
            and execution_profile_sha256(normalized_execution_profile)
            != planned_execution_profile_sha256
        ):
            raise DispatchValidationError(
                "planned execution profile digest does not match its profile"
            )
        expected_contract = contract.to_mapping()
        expected_project_root = str(Path(project_root).resolve())
        expected = {
            "contract": expected_contract,
            "project_root": expected_project_root,
            "backend": backend,
            "orchestration_id": orchestration_id,
            "thread_id": thread_id,
            "panel_id": panel_id,
            "planned_context_sha256": planned_context_sha256,
            "planned_base_head": planned_base_head,
            "planned_execution_profile_sha256": (
                planned_execution_profile_sha256
            ),
            "planned_execution_profile": normalized_execution_profile,
        }
        path = self._path(run_id)
        with exclusive_file_lock(self._lock_path(run_id)):
            if path.exists() or path.is_symlink():
                record = self.load(run_id)
                actual = {
                    "contract": record.contract,
                    "project_root": record.project_root,
                    "backend": record.backend,
                    "orchestration_id": record.orchestration_id,
                    "thread_id": record.thread_id,
                    "panel_id": record.panel_id,
                    "planned_context_sha256": record.planned_context_sha256,
                    "planned_base_head": record.planned_base_head,
                    "planned_execution_profile_sha256": (
                        record.planned_execution_profile_sha256
                    ),
                    "planned_execution_profile": dict(
                        record.planned_execution_profile or {}
                    ),
                }
                if actual != expected:
                    raise DispatchValidationError(
                        "existing run conflicts with deterministic create: "
                        f"{run_id}"
                    )
                return record

            now = time.time()
            record = RunRecord(
                run_id=run_id,
                status="accepted",
                contract=expected_contract,
                project_root=expected_project_root,
                backend=backend,
                created_at=now,
                updated_at=now,
                panel_id=panel_id,
                thread_id=thread_id,
                orchestration_id=orchestration_id,
                planned_context_sha256=planned_context_sha256,
                planned_base_head=planned_base_head,
                planned_execution_profile_sha256=(
                    planned_execution_profile_sha256
                ),
                planned_execution_profile=normalized_execution_profile,
            )
            self._save_unlocked(record)
            return record

    def save(self, record: RunRecord) -> None:
        with exclusive_file_lock(self._lock_path(record.run_id)):
            self._save_unlocked(record)

    def _save_unlocked(self, record: RunRecord) -> None:
        if record.status not in RUN_STATUSES:
            raise DispatchValidationError(f"invalid run status: {record.status}")
        record.updated_at = time.time()
        payload = record.to_mapping()
        RunRecord.from_mapping(payload)
        encoded = (
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
            + "\n"
        ).encode("utf-8")
        if len(encoded) > MAX_RUN_STATE_BYTES:
            raise DispatchValidationError(
                f"run state exceeds {MAX_RUN_STATE_BYTES} bytes"
            )
        atomic_write_json(self._path(record.run_id), payload)

    def _read_payload(self, path: Path) -> dict[str, Any]:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError as exc:
            raise DispatchValidationError(
                f"run not found: {path.stem}"
            ) from exc
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.EMLINK} or path.is_symlink():
                raise DispatchValidationError(
                    f"run state is a symbolic link: {path.stem}"
                ) from exc
            raise DispatchValidationError(
                f"run state cannot be opened safely: {path.stem}"
            ) from exc
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise DispatchValidationError(
                    f"run state is not a regular file: {path.stem}"
                )
            if opened.st_size > MAX_RUN_STATE_BYTES:
                raise DispatchValidationError(
                    f"run state exceeds {MAX_RUN_STATE_BYTES} bytes"
                )
            linked = os.stat(path, follow_symlinks=False)
            if stat.S_ISLNK(linked.st_mode) or not os.path.samestat(opened, linked):
                raise DispatchValidationError(
                    f"run state path changed while opening: {path.stem}"
                )
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                raw = handle.read(MAX_RUN_STATE_BYTES + 1)
            if len(raw) > MAX_RUN_STATE_BYTES:
                raise DispatchValidationError(
                    f"run state exceeds {MAX_RUN_STATE_BYTES} bytes"
                )
        except FileNotFoundError as exc:
            raise DispatchValidationError(
                f"run state path changed while opening: {path.stem}"
            ) from exc
        finally:
            os.close(descriptor)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DispatchValidationError(
                f"run state is corrupt: {path.stem}"
            ) from exc
        if not isinstance(payload, dict):
            raise DispatchValidationError(
                f"run state must be an object: {path.stem}"
            )
        return payload

    def load(self, run_id: str) -> RunRecord:
        path = self._path(run_id)
        return RunRecord.from_mapping(self._read_payload(path))

    def list_runs(self) -> list[RunRecord]:
        items: list[RunRecord] = []
        for path in sorted(self.root.glob("run-*.json")):
            try:
                items.append(self.load(path.stem))
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
                if record.cancel_requested_at and status != "cancelled":
                    raise DispatchValidationError(
                        "run terminal transition rejected: cancellation requested"
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

    def request_cancel(
        self,
        run_id: str,
        *,
        reason: str = "cancel requested",
    ) -> RunRecord:
        """Persist an idempotent cooperative cancellation request."""
        if type(reason) is not str:
            raise DispatchValidationError("cancel reason must be a string")
        if len(reason) > MAX_CANCEL_REASON_CHARS:
            raise DispatchValidationError(
                "cancel reason exceeds the character limit"
            )
        with exclusive_file_lock(self._lock_path(run_id)):
            record = self.load(run_id)
            if record.status in TERMINAL_RUN_STATUSES:
                return record
            if record.cancel_requested_at:
                return record
            requested_at = time.time()
            if not math.isfinite(requested_at) or requested_at <= 0:
                raise DispatchValidationError(
                    "cancel request time must be finite and positive"
                )
            record.cancel_requested_at = requested_at
            record.cancel_reason = reason
            if record.status == "accepted":
                record.status = "cancelled"
            record.revision += 1
            self._save_unlocked(record)
            return record

    def cancel_requested(
        self,
        run_id: str,
        *,
        worker_token: str,
    ) -> bool:
        """Return cancellation only to the exact active worker generation."""
        if not worker_token:
            raise DispatchValidationError("worker token must not be empty")
        record = self.load(run_id)
        return bool(
            record.status == "running"
            and record.worker_token == worker_token
            and record.cancel_requested_at
        )

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
        group_has_live_members = process_group_has_live_members(
            record.backend_pgid
        )
        if lock_held is False and group_has_live_members is False:
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
            term_delivered = False
        except OSError:
            return False
        else:
            term_delivered = True
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            lock_held = file_lock_is_held(expected_lock)
            group_has_live_members = process_group_has_live_members(
                record.backend_pgid
            )
            if lock_held is False and group_has_live_members is False:
                return True
            if lock_held is None or group_has_live_members is None:
                return False
            time.sleep(0.02)
        # The trusted wrapper keeps the lifetime lock held for longer than the
        # TERM grace window. Only escalate while that generation anchor is
        # still present; after release, a bare PGID is never safe to signal.
        if term_delivered and file_lock_is_held(expected_lock) is True:
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
                and process_group_has_live_members(record.backend_pgid) is False
            ):
                return True
            time.sleep(0.02)
        return (
            file_lock_is_held(expected_lock) is False
            and process_group_has_live_members(record.backend_pgid) is False
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
            if record.cancel_requested_at:
                record.status = "cancelled"
            else:
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
            if record.cancel_requested_at:
                record.status = "cancelled"
            else:
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
            if record.cancel_requested_at:
                record.status = "cancelled"
            else:
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

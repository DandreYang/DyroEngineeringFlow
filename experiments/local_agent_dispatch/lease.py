"""Dual-scope slot leases with process identity (ADR-0002 L1)."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import secrets
import stat
import threading
import time
from typing import Callable, Iterator

from .errors import DispatchValidationError
from .file_lock import exclusive_file_lock
from .paths import locks_dir
from .process_identity import (
    current_identity,
    identity_matches,
    process_identity_is_dead,
)
from .run_store import RunRecord, TERMINAL_RUN_STATUSES


LEASE_TTL_MS = 180_000
LEASE_INIT_GRACE_MS = 15_000
MAX_LEASE_JSON_BYTES = 64 * 1024
MAX_RUN_JSON_BYTES = 2 * 1024 * 1024


def _require_secure_dir_fd() -> None:
    required = (
        os.open,
        os.mkdir,
        os.rename,
        os.rmdir,
        os.stat,
        os.unlink,
    )
    supports_dir_fd = getattr(os, "supports_dir_fd", ())
    if any(function not in supports_dir_fd for function in required):
        raise DispatchValidationError(
            "slot lease management requires secure descriptor-relative paths"
        )


@contextmanager
def _open_directory_path(path: Path) -> Iterator[int]:
    _require_secure_dir_fd()
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise DispatchValidationError(
            f"slot directory cannot be opened safely: {path}"
        ) from exc
    try:
        opened = os.fstat(descriptor)
        linked = os.stat(path, follow_symlinks=False)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or stat.S_ISLNK(linked.st_mode)
            or not os.path.samestat(opened, linked)
        ):
            raise DispatchValidationError(
                f"slot directory path changed while opening: {path}"
            )
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _open_directory_at(
    parent_descriptor: int,
    name: str,
) -> Iterator[int]:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except OSError as exc:
        raise DispatchValidationError(
            f"slot directory component cannot be opened safely: {name}"
        ) from exc
    try:
        opened = os.fstat(descriptor)
        linked = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(opened.st_mode)
            or stat.S_ISLNK(linked.st_mode)
            or not os.path.samestat(opened, linked)
        ):
            raise DispatchValidationError(
                f"slot directory component changed while opening: {name}"
            )
        yield descriptor
    finally:
        os.close(descriptor)


def _read_json_at(
    directory_descriptor: int,
    name: str,
    *,
    max_bytes: int = MAX_LEASE_JSON_BYTES,
) -> dict[str, object] | None:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(
            name,
            flags,
            dir_fd=directory_descriptor,
        )
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise DispatchValidationError(
            f"slot state cannot be opened safely: {name}"
        ) from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size > max_bytes
        ):
            raise DispatchValidationError(
                f"slot state is not a bounded regular file: {name}"
            )
        raw = bytearray()
        while len(raw) <= max_bytes:
            chunk = os.read(
                descriptor,
                min(
                    16 * 1024,
                    max_bytes + 1 - len(raw),
                ),
            )
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(descriptor)
        if (
            len(raw) > max_bytes
            or not os.path.samestat(before, after)
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
        ):
            raise DispatchValidationError(
                f"slot state changed while reading: {name}"
            )
    finally:
        os.close(descriptor)
    try:
        payload = json.loads(bytes(raw).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _atomic_write_json_at(
    directory_descriptor: int,
    name: str,
    payload: dict[str, object],
) -> None:
    raw = (
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
        + "\n"
    ).encode("utf-8")
    if len(raw) > MAX_LEASE_JSON_BYTES:
        raise DispatchValidationError(
            f"slot state exceeds byte limit: {name}"
        )
    temporary_name = f".{name}.{secrets.token_hex(8)}.tmp"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    temporary_exists = False
    try:
        descriptor = os.open(
            temporary_name,
            flags,
            0o600,
            dir_fd=directory_descriptor,
        )
        temporary_exists = True
        try:
            offset = 0
            while offset < len(raw):
                written = os.write(descriptor, raw[offset:])
                if written <= 0:
                    raise OSError("slot state write made no progress")
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.rename(
            temporary_name,
            name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
        )
        temporary_exists = False
        os.fsync(directory_descriptor)
    finally:
        if temporary_exists:
            try:
                os.unlink(temporary_name, dir_fd=directory_descriptor)
            except OSError:
                pass


@dataclass
class SlotLease:
    root_dir: Path
    slot_dir: Path
    lock_path: Path
    scope: str
    index: int
    owner_token: str

    @contextmanager
    def _open_slot(self) -> Iterator[tuple[int, int]]:
        with _open_directory_path(self.root_dir) as root_descriptor:
            with _open_directory_at(
                root_descriptor,
                self.scope,
            ) as scope_descriptor:
                with _open_directory_at(
                    scope_descriptor,
                    f"slot-{self.index}",
                ) as slot_descriptor:
                    yield scope_descriptor, slot_descriptor

    def bind_run(self, run_id: str) -> None:
        if (
            not run_id
            or "/" in run_id
            or "\\" in run_id
            or ".." in run_id
        ):
            raise DispatchValidationError("invalid run_id for slot lease")
        with exclusive_file_lock(self.lock_path):
            with self._open_slot() as (_scope_descriptor, slot_descriptor):
                payload = _read_json_at(slot_descriptor, "lease.json") or {}
                if payload.get("owner_token") != self.owner_token:
                    raise DispatchValidationError(
                        "lease binding rejected: owner token mismatch"
                    )
                payload["run_id"] = run_id
                _atomic_write_json_at(
                    slot_descriptor,
                    "lease.json",
                    payload,
                )

    def renew(self) -> None:
        with exclusive_file_lock(self.lock_path):
            with self._open_slot() as (_scope_descriptor, slot_descriptor):
                payload = _read_json_at(slot_descriptor, "lease.json") or {}
                if payload.get("owner_token") != self.owner_token:
                    raise DispatchValidationError(
                        "lease renewal rejected: owner token mismatch"
                    )
                identity = current_identity()
                _atomic_write_json_at(
                    slot_descriptor,
                    "lease.json",
                    {
                        "pid": identity.pid,
                        "started_at": identity.started_at,
                        "acquired_at": payload.get(
                            "acquired_at",
                            time.time(),
                        ),
                        "renewed_at": time.time(),
                        "scope": self.scope,
                        "index": self.index,
                        "owner_token": self.owner_token,
                        "run_id": payload.get("run_id", ""),
                    },
                )

    def release(self) -> bool:
        with exclusive_file_lock(self.lock_path):
            try:
                with _open_directory_path(self.root_dir) as root_descriptor:
                    with _open_directory_at(
                        root_descriptor,
                        self.scope,
                    ) as scope_descriptor:
                        slot_name = f"slot-{self.index}"
                        with _open_directory_at(
                            scope_descriptor,
                            slot_name,
                        ) as slot_descriptor:
                            payload = (
                                _read_json_at(slot_descriptor, "lease.json")
                                or {}
                            )
                            if payload.get("owner_token") != self.owner_token:
                                return False
                            if set(os.listdir(slot_descriptor)) != {
                                "lease.json"
                            }:
                                return False
                            os.unlink(
                                "lease.json",
                                dir_fd=slot_descriptor,
                            )
                        os.rmdir(slot_name, dir_fd=scope_descriptor)
            except (DispatchValidationError, OSError):
                return False
            return True


class LeaseHeartbeat:
    def __init__(
        self,
        leases: list[SlotLease],
        *,
        interval_seconds: float = LEASE_TTL_MS / 3000,
        on_failure: Callable[[Exception], None] | None = None,
    ) -> None:
        self.leases = list(leases)
        self.interval_seconds = interval_seconds
        self._on_failure = on_failure
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._error: Exception | None = None
        self._failure_callback_error: Exception | None = None
        self._failure_lock = threading.Lock()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                for lease in self.leases:
                    lease.renew()
            except Exception as exc:  # noqa: BLE001 - surface during stop
                with self._failure_lock:
                    self._error = exc
                if self._on_failure is not None:
                    try:
                        self._on_failure(exc)
                    except Exception as callback_exc:  # noqa: BLE001
                        with self._failure_lock:
                            self._failure_callback_error = callback_exc
                return

    def check(self) -> None:
        """Raise as soon as heartbeat renewal has failed."""
        with self._failure_lock:
            error = self._error
            callback_error = self._failure_callback_error
        if error is not None:
            detail = f"lease heartbeat failed: {error}"
            if callback_error is not None:
                detail += f"; failure cleanup failed: {callback_error}"
            raise DispatchValidationError(detail) from error
        thread = self._thread
        if (
            thread is not None
            and not thread.is_alive()
            and not self._stop.is_set()
        ):
            raise DispatchValidationError(
                "lease heartbeat stopped unexpectedly"
            )

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.interval_seconds + 0.5))
            self._thread = None
        self.check()


class SlotManager:
    def __init__(
        self,
        home: Path | None = None,
        *,
        max_per_backend: int = 2,
        max_global: int = 4,
    ) -> None:
        if max_per_backend < 1 or max_global < 1:
            raise DispatchValidationError("slot limits must be >= 1")
        self.home = home
        self.root = locks_dir(home)
        self.max_per_backend = max_per_backend
        self.max_global = max_global

    def _ensure_scope(self, scope: str) -> None:
        if (
            not scope
            or "/" in scope
            or "\\" in scope
            or scope in {".", ".."}
        ):
            raise DispatchValidationError("invalid slot scope")
        with _open_directory_path(self.root) as root_descriptor:
            try:
                os.mkdir(scope, dir_fd=root_descriptor)
            except FileExistsError:
                pass
            except OSError as exc:
                raise DispatchValidationError(
                    f"slot scope cannot be created safely: {scope}"
                ) from exc
            with _open_directory_at(root_descriptor, scope):
                pass

    def _bound_run_state(
        self,
        slot_dir: Path,
        lease: dict[str, object],
    ) -> str:
        run_id = lease.get("run_id")
        if run_id is None or run_id == "":
            return self._legacy_run_state(slot_dir, lease)
        if (
            type(run_id) is not str
            or "/" in run_id
            or "\\" in run_id
            or ".." in run_id
        ):
            return "unknown"
        runs_root = self.root.parent / "runs"
        try:
            with _open_directory_path(runs_root) as runs_descriptor:
                record = _read_json_at(
                    runs_descriptor,
                    f"{run_id}.json",
                    max_bytes=MAX_RUN_JSON_BYTES,
                )
        except DispatchValidationError:
            return "unknown"
        if not record:
            return "unknown"
        try:
            run = RunRecord.from_mapping(record)
        except Exception:
            return "unknown"
        if run.run_id != run_id:
            return "unknown"
        exact_owner = (
            str(slot_dir) in (run.lease_slots or [])
            and run.worker_pid == lease.get("pid")
            and run.worker_started_at == lease.get("started_at")
            and bool(run.worker_token)
        )
        if not exact_owner:
            return "unknown"
        if run.status == "running":
            return "active"
        if run.status in TERMINAL_RUN_STATUSES:
            return "terminal"
        return "unknown"

    def _legacy_run_state(
        self,
        slot_dir: Path,
        lease: dict[str, object],
    ) -> str:
        """Prove an unbound pre-run_id lease has no active run reference."""
        runs_root = self.root.parent / "runs"
        try:
            with _open_directory_path(runs_root) as runs_descriptor:
                before = os.fstat(runs_descriptor)
                names = sorted(os.listdir(runs_descriptor))
                for name in names:
                    if not (
                        name.startswith("run-")
                        and name.endswith(".json")
                    ):
                        continue
                    record = _read_json_at(
                        runs_descriptor,
                        name,
                        max_bytes=MAX_RUN_JSON_BYTES,
                    )
                    if not record:
                        return "unknown"
                    try:
                        run = RunRecord.from_mapping(record)
                    except Exception:
                        return "unknown"
                    if name != f"{run.run_id}.json":
                        return "unknown"
                    if str(slot_dir) not in (run.lease_slots or []):
                        continue
                    if run.status in TERMINAL_RUN_STATUSES:
                        continue
                    exact_owner = (
                        run.status == "running"
                        and run.worker_pid == lease.get("pid")
                        and run.worker_started_at
                        == lease.get("started_at")
                        and bool(run.worker_token)
                    )
                    return "active" if exact_owner else "unknown"
                after = os.fstat(runs_descriptor)
                if (
                    before.st_mtime_ns != after.st_mtime_ns
                    or before.st_ctime_ns != after.st_ctime_ns
                ):
                    return "unknown"
        except (DispatchValidationError, OSError):
            return "unknown"
        return "unbound"

    def _try_acquire_scope(self, scope: str, max_slots: int) -> SlotLease | None:
        self._ensure_scope(scope)
        scope_dir = self.root / scope
        identity = current_identity()
        for index in range(max_slots):
            slot_name = f"slot-{index}"
            slot_dir = scope_dir / slot_name
            lock_path = self.root / f".{scope}.slot-{index}.lock"
            with exclusive_file_lock(lock_path):
                owner_token = secrets.token_hex(16)
                with _open_directory_path(self.root) as root_descriptor:
                    with _open_directory_at(
                        root_descriptor,
                        scope,
                    ) as scope_descriptor:
                        try:
                            os.mkdir(
                                slot_name,
                                dir_fd=scope_descriptor,
                            )
                        except FileExistsError:
                            try:
                                with _open_directory_at(
                                    scope_descriptor,
                                    slot_name,
                                ) as slot_descriptor:
                                    lease = _read_json_at(
                                        slot_descriptor,
                                        "lease.json",
                                    )
                                    slot_mtime = os.fstat(
                                        slot_descriptor
                                    ).st_mtime
                            except DispatchValidationError:
                                continue
                            if lease and identity_matches(lease):
                                continue
                            owner_is_dead = (
                                lease is not None
                                and type(lease.get("pid")) is int
                                and type(lease.get("started_at")) is str
                                and process_identity_is_dead(
                                    pid=lease["pid"],
                                    started_at=lease["started_at"],
                                )
                            )
                            run_state = (
                                self._bound_run_state(slot_dir, lease)
                                if lease is not None
                                else "unknown"
                            )
                            if (
                                lease is not None
                                and run_state in {"active", "unknown"}
                            ):
                                continue
                            immediate_reclaim = (
                                owner_is_dead
                                and run_state in {"terminal", "unbound"}
                            )
                            if lease is not None and not immediate_reclaim:
                                renewed_at = lease.get("renewed_at")
                                if (
                                    type(renewed_at) in {int, float}
                                    and math.isfinite(float(renewed_at))
                                ):
                                    age_ms = (
                                        time.time() - float(renewed_at)
                                    ) * 1000
                                    if age_ms < LEASE_TTL_MS:
                                        continue
                            if lease is None:
                                age_ms = (
                                    time.time() - slot_mtime
                                ) * 1000
                                if age_ms < LEASE_INIT_GRACE_MS:
                                    continue
                            reclaim_name = (
                                f"reclaim-{index}-{os.getpid()}-"
                                f"{time.time_ns()}"
                            )
                            try:
                                os.rename(
                                    slot_name,
                                    reclaim_name,
                                    src_dir_fd=scope_descriptor,
                                    dst_dir_fd=scope_descriptor,
                                )
                                with _open_directory_at(
                                    scope_descriptor,
                                    reclaim_name,
                                ) as reclaim_descriptor:
                                    entries = os.listdir(reclaim_descriptor)
                                    if any(
                                        stat.S_ISDIR(
                                            os.stat(
                                                child,
                                                dir_fd=reclaim_descriptor,
                                                follow_symlinks=False,
                                            ).st_mode
                                        )
                                        for child in entries
                                    ):
                                        raise DispatchValidationError(
                                            "unexpected directory in lease slot"
                                        )
                                    for child in entries:
                                        os.unlink(
                                            child,
                                            dir_fd=reclaim_descriptor,
                                        )
                                os.rmdir(
                                    reclaim_name,
                                    dir_fd=scope_descriptor,
                                )
                                os.mkdir(
                                    slot_name,
                                    dir_fd=scope_descriptor,
                                )
                            except (
                                DispatchValidationError,
                                OSError,
                            ):
                                try:
                                    os.rename(
                                        reclaim_name,
                                        slot_name,
                                        src_dir_fd=scope_descriptor,
                                        dst_dir_fd=scope_descriptor,
                                    )
                                except OSError:
                                    pass
                                continue
                        except OSError:
                            continue
                        try:
                            with _open_directory_at(
                                scope_descriptor,
                                slot_name,
                            ) as slot_descriptor:
                                _atomic_write_json_at(
                                    slot_descriptor,
                                    "lease.json",
                                    {
                                        "pid": identity.pid,
                                        "started_at": identity.started_at,
                                        "acquired_at": time.time(),
                                        "renewed_at": time.time(),
                                        "scope": scope,
                                        "index": index,
                                        "owner_token": owner_token,
                                        "run_id": "",
                                    },
                                )
                        except BaseException as operation_error:
                            cleanup_error: Exception | None = None
                            try:
                                os.stat(
                                    slot_name,
                                    dir_fd=scope_descriptor,
                                    follow_symlinks=False,
                                )
                                with _open_directory_at(
                                    scope_descriptor,
                                    slot_name,
                                ) as slot_descriptor:
                                    entries = os.listdir(slot_descriptor)
                                    if any(
                                        stat.S_ISDIR(
                                            os.stat(
                                                child,
                                                dir_fd=slot_descriptor,
                                                follow_symlinks=False,
                                            ).st_mode
                                        )
                                        for child in entries
                                    ):
                                        raise DispatchValidationError(
                                            "unexpected directory in new "
                                            "lease slot"
                                        )
                                    for child in entries:
                                        os.unlink(
                                            child,
                                            dir_fd=slot_descriptor,
                                        )
                                    os.fsync(slot_descriptor)
                                os.rmdir(
                                    slot_name,
                                    dir_fd=scope_descriptor,
                                )
                                os.fsync(scope_descriptor)
                            except FileNotFoundError:
                                cleanup_error = None
                            except Exception as exc:
                                cleanup_error = exc
                            if cleanup_error is not None:
                                raise DispatchValidationError(
                                    "lease initialization failed and partial "
                                    "slot cleanup could not be proven: "
                                    f"{cleanup_error}"
                                ) from operation_error
                            raise
                return SlotLease(
                    root_dir=self.root,
                    slot_dir=slot_dir,
                    lock_path=lock_path,
                    scope=scope,
                    index=index,
                    owner_token=owner_token,
                )
        return None

    def acquire(self, backend: str) -> list[SlotLease]:
        """Acquire backend-scoped and global slots; release both if either fails."""
        backend_key = backend.replace("/", "_").replace("\\", "_") or "auto"
        backend_lease = self._try_acquire_scope(
            f"backend-{backend_key}",
            self.max_per_backend,
        )
        if backend_lease is None:
            raise DispatchValidationError(
                f"no free slot for backend={backend} (max={self.max_per_backend})"
            )
        try:
            global_lease = self._try_acquire_scope(
                "global",
                self.max_global,
            )
        except BaseException as operation_error:
            try:
                released = backend_lease.release()
            except Exception as cleanup_error:
                raise DispatchValidationError(
                    "global slot acquisition failed and partial backend "
                    f"slot cleanup raised: {cleanup_error}"
                ) from operation_error
            if not released:
                raise DispatchValidationError(
                    "global slot acquisition failed and partial backend "
                    "slot cleanup could not be proven"
                ) from operation_error
            raise
        if global_lease is None:
            try:
                released = backend_lease.release()
            except Exception as cleanup_error:
                raise DispatchValidationError(
                    "no free global dispatch slot and partial backend "
                    f"slot cleanup raised: {cleanup_error}"
                ) from cleanup_error
            if not released:
                raise DispatchValidationError(
                    "no free global dispatch slot and partial backend "
                    "slot cleanup could not be proven"
                )
            raise DispatchValidationError(
                f"no free global dispatch slot (max={self.max_global})"
            )
        return [backend_lease, global_lease]

    def release_all(self, leases: list[SlotLease]) -> None:
        failures: list[str] = []
        for lease in leases:
            try:
                released = lease.release()
            except Exception as exc:
                failures.append(f"{lease.scope}/{lease.index}: {exc}")
                continue
            if not released:
                failures.append(
                    f"{lease.scope}/{lease.index}: owner no longer matched"
                )
        if failures:
            raise DispatchValidationError(
                "slot release could not be proven: " + "; ".join(failures)
            )

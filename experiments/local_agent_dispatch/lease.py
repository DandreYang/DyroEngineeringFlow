"""Dual-scope slot leases with process identity (ADR-0002 L1)."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import time

from .errors import DispatchValidationError
from .json_store import atomic_write_json, read_json
from .paths import locks_dir
from .process_identity import current_identity, identity_matches


LEASE_TTL_MS = 180_000
LEASE_INIT_GRACE_MS = 15_000


@dataclass
class SlotLease:
    slot_dir: Path
    scope: str
    index: int

    def renew(self) -> None:
        lease_path = self.slot_dir / "lease.json"
        payload = read_json(lease_path) or {}
        identity = current_identity()
        if payload.get("pid") != identity.pid:
            # Still allow renew if we own the directory file we created.
            pass
        atomic_write_json(
            lease_path,
            {
                "pid": identity.pid,
                "started_at": identity.started_at,
                "acquired_at": payload.get("acquired_at", time.time()),
                "renewed_at": time.time(),
                "scope": self.scope,
                "index": self.index,
            },
        )

    def release(self) -> None:
        try:
            if self.slot_dir.exists():
                for child in self.slot_dir.iterdir():
                    child.unlink(missing_ok=True)
                self.slot_dir.rmdir()
        except OSError:
            pass


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

    def _try_acquire_scope(self, scope: str, max_slots: int) -> SlotLease | None:
        scope_dir = self.root / scope
        scope_dir.mkdir(parents=True, exist_ok=True)
        identity = current_identity()
        for index in range(max_slots):
            slot_dir = scope_dir / f"slot-{index}"
            try:
                os.mkdir(slot_dir)
            except FileExistsError:
                lease = read_json(slot_dir / "lease.json")
                if lease and identity_matches(lease):
                    continue
                if lease is None:
                    try:
                        age_ms = (time.time() - slot_dir.stat().st_mtime) * 1000
                        if age_ms < LEASE_INIT_GRACE_MS:
                            continue
                    except OSError:
                        continue
                # reclaim zombie
                reclaim = scope_dir / f"reclaim-{index}-{os.getpid()}-{time.time_ns()}"
                try:
                    os.rename(slot_dir, reclaim)
                    for child in reclaim.iterdir():
                        child.unlink(missing_ok=True)
                    reclaim.rmdir()
                    os.mkdir(slot_dir)
                except OSError:
                    continue
            atomic_write_json(
                slot_dir / "lease.json",
                {
                    "pid": identity.pid,
                    "started_at": identity.started_at,
                    "acquired_at": time.time(),
                    "renewed_at": time.time(),
                    "scope": scope,
                    "index": index,
                },
            )
            return SlotLease(slot_dir=slot_dir, scope=scope, index=index)
        return None

    def acquire(self, backend: str) -> list[SlotLease]:
        """Acquire backend-scoped and global slots; release both if either fails."""
        backend_key = backend.replace("/", "_") or "auto"
        backend_lease = self._try_acquire_scope(f"backend-{backend_key}", self.max_per_backend)
        if backend_lease is None:
            raise DispatchValidationError(
                f"no free slot for backend={backend} (max={self.max_per_backend})"
            )
        global_lease = self._try_acquire_scope("global", self.max_global)
        if global_lease is None:
            backend_lease.release()
            raise DispatchValidationError(
                f"no free global dispatch slot (max={self.max_global})"
            )
        return [backend_lease, global_lease]

    def release_all(self, leases: list[SlotLease]) -> None:
        for lease in leases:
            lease.release()

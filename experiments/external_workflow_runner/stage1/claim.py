"""Claim lease model and Supervisor-only renewal design for Stage 1."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import fcntl
import math
import os
from pathlib import Path
import stat
import tempfile
import time
from typing import Mapping

from ..errors import Stage0ValidationError
from .protocol import dumps_strict, loads_strict


@dataclass(frozen=True)
class ClaimRecord:
    task_id: str
    runner_id: str
    generation: int
    execution_key_id: str
    issued_at: float
    expires_at: float

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "task_id": self.task_id,
            "runner_id": self.runner_id,
            "generation": self.generation,
            "execution_key_id": self.execution_key_id,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> ClaimRecord:
        if payload.get("schema_version") != 1:
            raise Stage0ValidationError("claim schema_version is unsupported")
        for field_name in (
            "task_id",
            "runner_id",
            "execution_key_id",
        ):
            value = payload.get(field_name)
            if not isinstance(value, str) or not value or len(value) > 128:
                raise Stage0ValidationError(f"claim field is invalid: {field_name}")
        generation = payload.get("generation")
        issued_at = payload.get("issued_at")
        expires_at = payload.get("expires_at")
        if type(generation) is not int or generation < 1:
            raise Stage0ValidationError("claim generation is invalid")
        if (
            type(issued_at) not in (int, float)
            or type(expires_at) not in (int, float)
            or not math.isfinite(float(issued_at))
            or not math.isfinite(float(expires_at))
            or expires_at <= issued_at
        ):
            raise Stage0ValidationError("claim timestamps are invalid")
        return cls(
            task_id=str(payload["task_id"]),
            runner_id=str(payload["runner_id"]),
            generation=generation,
            execution_key_id=str(payload["execution_key_id"]),
            issued_at=float(issued_at),
            expires_at=float(expires_at),
        )


@dataclass
class ClaimLease:
    """In-memory lease that only the Supervisor may renew."""

    record: ClaimRecord
    renewals: list[dict[str, object]] = field(default_factory=list)

    def remaining_seconds(self, *, now: float | None = None) -> float:
        current = time.time() if now is None else now
        return self.record.expires_at - current

    def is_expired(self, *, now: float | None = None) -> bool:
        return self.remaining_seconds(now=now) <= 0

    def should_renew(self, *, now: float | None = None) -> bool:
        """Renew only before the half-life of the remaining lease window."""
        current = time.time() if now is None else now
        lifetime = self.record.expires_at - self.record.issued_at
        if lifetime <= 0:
            return False
        midpoint = self.record.issued_at + (lifetime / 2.0)
        return current >= midpoint and not self.is_expired(now=current)

    def build_renewal(
        self,
        *,
        extend_seconds: float,
        now: float | None = None,
    ) -> ClaimRecord:
        if (
            type(extend_seconds) not in (int, float)
            or not math.isfinite(float(extend_seconds))
            or extend_seconds <= 0
            or extend_seconds > 86_400
        ):
            raise Stage0ValidationError("claim extend_seconds is invalid")
        current = time.time() if now is None else now
        if not math.isfinite(float(current)):
            raise Stage0ValidationError("claim renewal time is invalid")
        if self.is_expired(now=current):
            raise Stage0ValidationError("refusing to renew an expired claim")
        return ClaimRecord(
            task_id=self.record.task_id,
            runner_id=self.record.runner_id,
            generation=self.record.generation + 1,
            execution_key_id=self.record.execution_key_id,
            issued_at=current,
            expires_at=current + float(extend_seconds),
        )

    def commit_renewal(self, renewed: ClaimRecord) -> None:
        previous = self.record
        if (
            renewed.task_id != previous.task_id
            or renewed.runner_id != previous.runner_id
            or renewed.execution_key_id != previous.execution_key_id
            or renewed.generation != previous.generation + 1
        ):
            raise Stage0ValidationError("claim renewal does not extend the current owner")
        self.renewals.append(
            {
                "previous_generation": previous.generation,
                "new_generation": renewed.generation,
                "renewed_at": renewed.issued_at,
                "expires_at": renewed.expires_at,
            }
        )
        self.record = renewed

    def renew(self, *, extend_seconds: float, now: float | None = None) -> ClaimRecord:
        renewed = self.build_renewal(
            extend_seconds=extend_seconds,
            now=now,
        )
        self.commit_renewal(renewed)
        return renewed


class ClaimStore:
    """Filesystem claim binding used by the Supervisor only."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_name(f".{self.path.name}.lock")

    @contextmanager
    def _exclusive(self, *, timeout_seconds: float = 2.0):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = (
            os.O_CREAT
            | os.O_RDWR
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(self.lock_path, flags, 0o600)
        except OSError as exc:
            raise Stage0ValidationError("claim lock cannot be opened safely") from exc
        try:
            with os.fdopen(descriptor, "r+", encoding="utf-8") as handle:
                deadline = time.monotonic() + timeout_seconds
                while True:
                    try:
                        fcntl.flock(
                            handle.fileno(),
                            fcntl.LOCK_EX | fcntl.LOCK_NB,
                        )
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise Stage0ValidationError(
                                "timed out acquiring claim lock"
                            )
                        time.sleep(0.01)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            raise Stage0ValidationError("claim lock failed") from exc

    def _write_unlocked(self, claim: ClaimRecord) -> None:
        ClaimRecord.from_mapping(claim.to_mapping())
        text = dumps_strict(claim.to_mapping()) + "\n"
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=str(self.path.parent),
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _read_unlocked(self) -> ClaimRecord:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(self.path, flags)
        except OSError as exc:
            raise Stage0ValidationError("claim file is unreadable") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 64 * 1024:
                raise Stage0ValidationError("claim file is not a bounded regular file")
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 16 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            after = os.fstat(descriptor)
            if (
                metadata.st_dev != after.st_dev
                or metadata.st_ino != after.st_ino
                or metadata.st_size != after.st_size
                or metadata.st_mtime_ns != after.st_mtime_ns
            ):
                raise Stage0ValidationError("claim file changed while being read")
            try:
                text = b"".join(chunks).decode("utf-8").strip()
            except UnicodeDecodeError as exc:
                raise Stage0ValidationError("claim file is unreadable") from exc
            payload = loads_strict(text)
            return ClaimRecord.from_mapping(payload)
        finally:
            os.close(descriptor)

    def write(self, claim: ClaimRecord) -> None:
        with self._exclusive():
            self._write_unlocked(claim)

    def compare_and_swap(
        self,
        *,
        expected: ClaimRecord,
        replacement: ClaimRecord,
    ) -> None:
        """Replace a claim only while the complete expected owner record matches."""
        with self._exclusive():
            current = self._read_unlocked()
            if current != expected:
                raise Stage0ValidationError(
                    "claim changed before renewal; refusing stale overwrite"
                )
            self._write_unlocked(replacement)

    def read(self) -> ClaimRecord:
        return self._read_unlocked()

    def assert_matches(
        self,
        *,
        runner_id: str,
        generation: int | None = None,
        execution_key_id: str | None = None,
        task_id: str | None = None,
        now: float | None = None,
    ) -> ClaimRecord:
        claim = self.read()
        current = time.time() if now is None else now
        if claim.runner_id != runner_id:
            raise Stage0ValidationError("claim runner_id mismatch")
        if generation is not None and claim.generation != generation:
            raise Stage0ValidationError("claim generation mismatch")
        if (
            execution_key_id is not None
            and claim.execution_key_id != execution_key_id
        ):
            raise Stage0ValidationError("claim execution_key_id mismatch")
        if task_id is not None and claim.task_id != task_id:
            raise Stage0ValidationError("claim task_id mismatch")
        if claim.expires_at <= current:
            raise Stage0ValidationError("claim is expired")
        return claim

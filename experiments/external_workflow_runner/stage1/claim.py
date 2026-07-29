"""Claim lease model and Supervisor-only renewal design for Stage 1."""

from __future__ import annotations

from dataclasses import dataclass
import time
from pathlib import Path
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
        for field in (
            "task_id",
            "runner_id",
            "execution_key_id",
        ):
            value = payload.get(field)
            if not isinstance(value, str) or not value or len(value) > 128:
                raise Stage0ValidationError(f"claim field is invalid: {field}")
        generation = payload.get("generation")
        issued_at = payload.get("issued_at")
        expires_at = payload.get("expires_at")
        if type(generation) is not int or generation < 1:
            raise Stage0ValidationError("claim generation is invalid")
        if (
            not isinstance(issued_at, (int, float))
            or not isinstance(expires_at, (int, float))
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
    renewals: list[dict[str, object]]

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

    def renew(self, *, extend_seconds: float, now: float | None = None) -> ClaimRecord:
        if (
            not isinstance(extend_seconds, (int, float))
            or extend_seconds <= 0
            or extend_seconds > 86_400
        ):
            raise Stage0ValidationError("claim extend_seconds is invalid")
        current = time.time() if now is None else now
        if self.is_expired(now=current):
            raise Stage0ValidationError("refusing to renew an expired claim")
        renewed = ClaimRecord(
            task_id=self.record.task_id,
            runner_id=self.record.runner_id,
            generation=self.record.generation + 1,
            execution_key_id=self.record.execution_key_id,
            issued_at=current,
            expires_at=current + float(extend_seconds),
        )
        self.renewals.append(
            {
                "previous_generation": self.record.generation,
                "new_generation": renewed.generation,
                "renewed_at": current,
                "expires_at": renewed.expires_at,
            }
        )
        self.record = renewed
        return renewed


class ClaimStore:
    """Filesystem claim binding used by the Supervisor only."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def write(self, claim: ClaimRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(dumps_strict(claim.to_mapping()) + "\n", encoding="utf-8")
        self.path.chmod(0o600)

    def read(self) -> ClaimRecord:
        try:
            payload = loads_strict(self.path.read_text(encoding="utf-8").strip())
        except (OSError, UnicodeDecodeError) as exc:
            raise Stage0ValidationError("claim file is unreadable") from exc
        return ClaimRecord.from_mapping(payload)

    def assert_matches(
        self,
        *,
        runner_id: str,
        generation: int | None = None,
        now: float | None = None,
    ) -> ClaimRecord:
        claim = self.read()
        current = time.time() if now is None else now
        if claim.runner_id != runner_id:
            raise Stage0ValidationError("claim runner_id mismatch")
        if generation is not None and claim.generation != generation:
            raise Stage0ValidationError("claim generation mismatch")
        if claim.expires_at <= current:
            raise Stage0ValidationError("claim is expired")
        return claim

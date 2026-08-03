"""One-time bootstrap exchange and in-memory Console sessions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hmac
import secrets
import threading
import time
from typing import Callable


BOOTSTRAP_TTL_SECONDS = 60.0
BOOTSTRAP_MAX_FAILURES = 5
SESSION_IDLE_TTL_SECONDS = 30 * 60.0
SESSION_ABSOLUTE_TTL_SECONDS = 8 * 60 * 60.0


class SessionRejected(Exception):
    """A deliberately detail-free rejected bootstrap or bearer exchange."""


@dataclass(frozen=True)
class SessionView:
    token: str
    expires_at: datetime


@dataclass
class _Session:
    token: str
    idle_deadline: float
    absolute_deadline: float
    absolute_expires_at: datetime


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ConsoleSessionStore:
    """Memory-only bearer state; no cookie, disk, or workspace side effect."""

    def __init__(
        self,
        *,
        bootstrap_secret: str | None = None,
        monotonic_clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        secret = bootstrap_secret if bootstrap_secret is not None else secrets.token_urlsafe(32)
        if not isinstance(secret, str) or len(secret) < 43:
            raise ValueError("bootstrap secret 必须至少包含 256 bit 的 URL-safe 熵")
        self._bootstrap_secret: str | None = secret
        self._clock = monotonic_clock
        self._wall_clock = wall_clock
        self._bootstrap_deadline = monotonic_clock() + BOOTSTRAP_TTL_SECONDS
        self._bootstrap_failures = 0
        self._sessions: dict[str, _Session] = {}
        self._lock = threading.Lock()

    @property
    def bootstrap_secret(self) -> str:
        """Return the one-time secret only to the local foreground launcher."""
        with self._lock:
            if self._bootstrap_secret is None:
                raise SessionRejected()
            return self._bootstrap_secret

    def exchange(self, supplied: str) -> SessionView:
        if not isinstance(supplied, str) or len(supplied) > 256:
            raise SessionRejected()
        with self._lock:
            now = self._clock()
            expected = self._bootstrap_secret
            if (
                expected is None
                or now >= self._bootstrap_deadline
                or self._bootstrap_failures >= BOOTSTRAP_MAX_FAILURES
                or not hmac.compare_digest(expected, supplied)
            ):
                self._bootstrap_failures += 1
                if self._bootstrap_failures >= BOOTSTRAP_MAX_FAILURES:
                    self._bootstrap_secret = None
                raise SessionRejected()
            self._bootstrap_secret = None
            token = secrets.token_urlsafe(32)
            absolute_expires_at = self._wall_clock().astimezone(timezone.utc) + timedelta(
                seconds=SESSION_ABSOLUTE_TTL_SECONDS
            )
            session = _Session(
                token=token,
                idle_deadline=now + SESSION_IDLE_TTL_SECONDS,
                absolute_deadline=now + SESSION_ABSOLUTE_TTL_SECONDS,
                absolute_expires_at=absolute_expires_at,
            )
            self._sessions[token] = session
            return SessionView(token=token, expires_at=absolute_expires_at)

    def authorize(self, supplied: str) -> SessionView:
        if not isinstance(supplied, str) or len(supplied) > 256:
            raise SessionRejected()
        with self._lock:
            now = self._clock()
            session = self._sessions.get(supplied)
            if (
                session is None
                or not hmac.compare_digest(session.token, supplied)
                or now >= session.idle_deadline
                or now >= session.absolute_deadline
            ):
                if session is not None:
                    self._sessions.pop(supplied, None)
                raise SessionRejected()
            session.idle_deadline = min(
                now + SESSION_IDLE_TTL_SECONDS, session.absolute_deadline
            )
            remaining = min(session.idle_deadline, session.absolute_deadline) - now
            expires_at = self._wall_clock().astimezone(timezone.utc) + timedelta(
                seconds=max(0.0, remaining)
            )
            return SessionView(token=session.token, expires_at=expires_at)

    def clear(self) -> None:
        with self._lock:
            self._bootstrap_secret = None
            self._sessions.clear()

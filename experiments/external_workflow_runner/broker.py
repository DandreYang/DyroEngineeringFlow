"""Concurrency and deadline enforcement for a future narrow Agent Broker."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import math
from typing import TypeVar

from .errors import Stage0ValidationError


T = TypeVar("T")


class BrokerLimiter:
    """Count queue time in the deadline and expose observed concurrency."""

    def __init__(self, *, max_concurrency: int, default_timeout_seconds: float) -> None:
        if type(max_concurrency) is not int or max_concurrency <= 0:
            raise Stage0ValidationError("max_concurrency must be a positive integer")
        if (
            isinstance(default_timeout_seconds, bool)
            or not isinstance(default_timeout_seconds, (int, float))
            or not math.isfinite(default_timeout_seconds)
            or default_timeout_seconds <= 0
        ):
            raise Stage0ValidationError("default_timeout_seconds must be positive")
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._default_timeout_seconds = default_timeout_seconds
        self._active_calls = 0
        self._max_observed_concurrency = 0
        self._call_ids: set[str] = set()
        self._state_lock = asyncio.Lock()

    @property
    def active_calls(self) -> int:
        return self._active_calls

    @property
    def max_observed_concurrency(self) -> int:
        return self._max_observed_concurrency

    async def call(
        self,
        call_id: str,
        handler: Callable[[str], Awaitable[T]],
        *,
        timeout_seconds: float | None = None,
    ) -> T:
        if not isinstance(call_id, str) or not call_id or len(call_id) > 128:
            raise Stage0ValidationError("call_id must contain 1 to 128 characters")
        timeout = (
            self._default_timeout_seconds
            if timeout_seconds is None
            else timeout_seconds
        )
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise Stage0ValidationError("timeout_seconds must be positive")
        async with self._state_lock:
            if call_id in self._call_ids:
                raise Stage0ValidationError(f"duplicate active call_id: {call_id}")
            self._call_ids.add(call_id)
        active = False
        try:
            try:
                async with asyncio.timeout(timeout):
                    async with self._semaphore:
                        self._active_calls += 1
                        active = True
                        self._max_observed_concurrency = max(
                            self._max_observed_concurrency,
                            self._active_calls,
                        )
                        return await handler(call_id)
            except TimeoutError as exc:
                raise TimeoutError(f"Agent call timed out: {call_id}") from exc
        finally:
            if active:
                self._active_calls -= 1
            async with self._state_lock:
                self._call_ids.discard(call_id)

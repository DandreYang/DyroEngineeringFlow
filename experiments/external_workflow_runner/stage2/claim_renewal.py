"""Supervisor-only claim renewal loop for long-running Stage 2 workflows."""

from __future__ import annotations

from dataclasses import dataclass, field
import threading
import time

from ..errors import Stage0ValidationError
from ..stage1.claim import ClaimLease, ClaimStore


@dataclass
class ClaimRenewalLoop:
    """
    Periodically renews a claim while a workflow is running.

    Only the Supervisor process runs this loop. Sandbox and Broker never see
    the claim file.
    """

    store: ClaimStore
    lease: ClaimLease
    extend_seconds: float
    interval_seconds: float = 0.25
    runner_id: str = "stage2-runner"
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)
    _thread: threading.Thread | None = field(default=None, repr=False)
    _error: str | None = field(default=None, repr=False)
    renewals_observed: int = 0

    def start(self) -> None:
        if self._thread is not None:
            raise Stage0ValidationError("claim renewal loop already started")
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            if self._thread.is_alive():
                raise Stage0ValidationError(
                    "claim renewal loop did not stop before the deadline"
                )
            self._thread = None
        if self._error is not None:
            raise Stage0ValidationError(self._error)

    def renew_once(self, *, now: float | None = None) -> bool:
        """Atomically renew the exact claim generation currently owned by this lease."""
        current = time.time() if now is None else now
        if self.lease.is_expired(now=current):
            raise Stage0ValidationError("claim expired during Stage 2 workflow")
        if not self.lease.should_renew(now=current):
            return False
        previous = self.lease.record
        if previous.runner_id != self.runner_id:
            raise Stage0ValidationError("claim runner_id mismatch before renewal")
        renewed = self.lease.build_renewal(
            extend_seconds=self.extend_seconds,
            now=current,
        )
        self.store.compare_and_swap(
            expected=previous,
            replacement=renewed,
        )
        self.lease.commit_renewal(renewed)
        self.renewals_observed += 1
        return True

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.renew_once()
            except Exception as exc:  # noqa: BLE001 - surface on stop()
                self._error = f"claim renewal failed: {exc}"
                return
            self._stop.wait(self.interval_seconds)

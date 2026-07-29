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
            self._thread = None
        if self._error is not None:
            raise Stage0ValidationError(self._error)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                now = time.time()
                if self.lease.is_expired(now=now):
                    self._error = "claim expired during Stage 2 workflow"
                    return
                if self.lease.should_renew(now=now):
                    renewed = self.lease.renew(
                        extend_seconds=self.extend_seconds,
                        now=now,
                    )
                    self.store.write(renewed)
                    self.store.assert_matches(
                        runner_id=self.runner_id,
                        generation=renewed.generation,
                        now=now,
                    )
                    self.renewals_observed += 1
            except Exception as exc:  # noqa: BLE001 - surface on stop()
                self._error = f"claim renewal failed: {exc}"
                return
            self._stop.wait(self.interval_seconds)

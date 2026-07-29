"""Process identity helpers for slot leases (ADR-0002 L0 foundation)."""

from __future__ import annotations

from dataclasses import dataclass
import os
import subprocess
import time
from typing import Mapping


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    started_at: str

    def to_mapping(self) -> dict[str, object]:
        return {"pid": self.pid, "started_at": self.started_at}


def process_started_at(pid: int | None = None) -> str | None:
    """
    Return a stable process start token for ``pid`` (default: current process).

    Uses ``ps -o lstart=`` when available. Falls back to a monotonic-incompatible
    but still non-empty token so callers can detect missing identity rather than
    silently treating all pids as equal.
    """
    target = os.getpid() if pid is None else pid
    try:
        completed = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(target)],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    token = completed.stdout.strip()
    return token or None


def current_identity() -> ProcessIdentity:
    started = process_started_at() or f"unknown-{os.getpid()}-{time.time_ns()}"
    return ProcessIdentity(pid=os.getpid(), started_at=started)


def identity_matches(lease: Mapping[str, object], *, pid: int | None = None) -> bool:
    """True when lease pid is alive and started_at still matches."""
    lease_pid = lease.get("pid")
    lease_started = lease.get("started_at")
    if type(lease_pid) is not int or type(lease_started) is not str:
        return False
    if pid is not None and lease_pid != pid:
        return False
    live = process_started_at(lease_pid)
    return live is not None and live == lease_started

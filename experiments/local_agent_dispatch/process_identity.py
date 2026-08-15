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


def identity_for_pid(pid: int) -> ProcessIdentity:
    if type(pid) is not int or pid <= 0:
        raise ValueError("pid must be a positive integer")
    started = process_started_at(pid) or f"unknown-{pid}-{time.time_ns()}"
    return ProcessIdentity(pid=pid, started_at=started)


def process_is_alive(pid: int) -> bool:
    """Return false only when the operating system proves ``pid`` is absent."""
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def process_state(pid: int) -> str | None:
    """Best-effort process state token; ``Z`` denotes an exited zombie."""
    try:
        completed = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    state = completed.stdout.strip()
    return state or None


def process_group_has_live_members(process_group_id: int) -> bool | None:
    """Return whether a process group contains a non-zombie member.

    ``False`` means every observed member has exited (and may only be waiting
    to be reaped), while ``None`` keeps callers fail-closed when the process
    table cannot be inspected reliably.
    """
    if type(process_group_id) is not int or process_group_id <= 0:
        return None
    ps_path = "/bin/ps"
    if not os.path.isfile(ps_path) or not os.access(ps_path, os.X_OK):
        return None
    try:
        completed = subprocess.run(
            [ps_path, "-axo", "pgid=,stat="],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    parsed_rows = 0
    for line in completed.stdout.splitlines():
        if not line.strip():
            continue
        fields = line.split()
        if len(fields) < 2:
            return None
        try:
            member_pgid = int(fields[0])
        except ValueError:
            return None
        parsed_rows += 1
        if member_pgid != process_group_id:
            continue
        if not fields[1].startswith("Z"):
            return True
    return False if parsed_rows else None


def process_identity_is_dead(*, pid: int, started_at: str) -> bool:
    """
    Return true only with positive evidence that the generation ended.

    False means alive *or unverifiable*; in particular, equality of the
    second-resolution ``ps`` token is not proof of generation ownership.
    """
    if not process_is_alive(pid):
        return True
    state = process_state(pid)
    if state is not None and state.startswith("Z"):
        return True
    if started_at.startswith("unknown-"):
        return False
    live_started_at = process_started_at(pid)
    return live_started_at is not None and live_started_at != started_at


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

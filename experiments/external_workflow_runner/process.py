"""Bounded argv execution with explicit environment and process-group teardown."""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
from typing import Mapping, Sequence

from .errors import Stage0ValidationError


@dataclass(frozen=True)
class ProcessLimits:
    timeout_seconds: float
    max_stdout_bytes: int
    max_stderr_bytes: int
    terminate_grace_seconds: float = 0.5

    def __post_init__(self) -> None:
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise Stage0ValidationError("timeout_seconds must be positive")
        if (
            type(self.max_stdout_bytes) is not int
            or type(self.max_stderr_bytes) is not int
            or self.max_stdout_bytes <= 0
            or self.max_stderr_bytes <= 0
        ):
            raise Stage0ValidationError("process output limits must be positive")
        if (
            isinstance(self.terminate_grace_seconds, bool)
            or not isinstance(self.terminate_grace_seconds, (int, float))
            or not math.isfinite(self.terminate_grace_seconds)
            or self.terminate_grace_seconds < 0
        ):
            raise Stage0ValidationError("terminate_grace_seconds must not be negative")


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    output_limited: bool
    descendant_pipe_lingered: bool

    @property
    def succeeded(self) -> bool:
        return (
            self.returncode == 0
            and not self.timed_out
            and not self.output_limited
            and not self.descendant_pipe_lingered
        )


class _BoundedReader(threading.Thread):
    def __init__(self, stream, limit: int, overflow: threading.Event) -> None:
        super().__init__(daemon=True)
        self._stream = stream
        self._limit = limit
        self._overflow = overflow
        self.buffer = bytearray()

    def run(self) -> None:
        try:
            while True:
                chunk = self._stream.read(64 * 1024)
                if not chunk:
                    return
                remaining = self._limit - len(self.buffer)
                if remaining > 0:
                    self.buffer.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    self._overflow.set()
        finally:
            self._stream.close()


def _terminate_process_group(
    process: subprocess.Popen[bytes], grace_seconds: float
) -> None:
    process_group_id = process.pid

    def group_exists() -> bool:
        try:
            os.killpg(process_group_id, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return process.poll() is None
        return True

    try:
        os.killpg(process_group_id, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        if process.poll() is None:
            process.terminate()
    deadline = time.monotonic() + grace_seconds
    while group_exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    if group_exists():
        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            if process.poll() is None:
                process.kill()
    if process.poll() is None:
        try:
            process.wait(timeout=max(0.1, grace_seconds))
        except subprocess.TimeoutExpired:
            process.kill()
        process.wait()


def _validated_environment(environment: Mapping[str, str]) -> dict[str, str]:
    validated: dict[str, str] = {}
    for name, value in environment.items():
        if (
            not isinstance(name, str)
            or not name
            or "=" in name
            or "\x00" in name
            or not isinstance(value, str)
            or "\x00" in value
        ):
            raise Stage0ValidationError("process environment contains an invalid entry")
        validated[name] = value
    return validated


def run_bounded_process(
    argv: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    limits: ProcessLimits,
) -> ProcessResult:
    """Run without a shell, inherit no environment, and bound both output streams."""
    if not argv or any(
        not isinstance(item, str) or not item or "\x00" in item for item in argv
    ):
        raise Stage0ValidationError("process argv must contain non-empty strings")
    cwd = Path(cwd)
    if cwd.is_symlink() or not cwd.is_dir():
        raise Stage0ValidationError(
            "process cwd must be an existing non-symlink directory"
        )

    try:
        process = subprocess.Popen(
            list(argv),
            cwd=cwd.resolve(strict=True),
            env=_validated_environment(environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        raise Stage0ValidationError(f"process could not be started: {argv[0]}") from exc
    assert process.stdout is not None
    assert process.stderr is not None
    overflow = threading.Event()
    stdout_reader = _BoundedReader(process.stdout, limits.max_stdout_bytes, overflow)
    stderr_reader = _BoundedReader(process.stderr, limits.max_stderr_bytes, overflow)
    stdout_reader.start()
    stderr_reader.start()

    deadline = time.monotonic() + limits.timeout_seconds
    timed_out = False
    while process.poll() is None:
        if overflow.is_set():
            _terminate_process_group(process, limits.terminate_grace_seconds)
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            _terminate_process_group(process, limits.terminate_grace_seconds)
            break
        overflow.wait(min(remaining, 0.02))

    if process.poll() is None:
        _terminate_process_group(process, limits.terminate_grace_seconds)
    process.wait()

    join_timeout = max(0.1, limits.terminate_grace_seconds)
    stdout_reader.join(join_timeout)
    stderr_reader.join(join_timeout)
    descendant_pipe_lingered = stdout_reader.is_alive() or stderr_reader.is_alive()
    if descendant_pipe_lingered:
        _terminate_process_group(process, limits.terminate_grace_seconds)
        stdout_reader.join(join_timeout)
        stderr_reader.join(join_timeout)

    return ProcessResult(
        returncode=process.returncode,
        stdout=bytes(stdout_reader.buffer).decode("utf-8", errors="replace"),
        stderr=bytes(stderr_reader.buffer).decode("utf-8", errors="replace"),
        timed_out=timed_out,
        output_limited=overflow.is_set(),
        descendant_pipe_lingered=descendant_pipe_lingered,
    )

"""Bounded subprocess execution with process-group termination."""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import tempfile
import time
from typing import Callable, Mapping, Sequence

from .file_lock import open_inherited_lifetime_lock


DEFAULT_MAX_OUTPUT_BYTES = 1024 * 1024
TERMINATION_GRACE_SECONDS = 0.5
LIFETIME_ANCHOR_GUARD_SECONDS = 2.0


@dataclass(frozen=True)
class BoundedCompletedProcess:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    output_limited: bool = False
    cancelled: bool = False
    stdout_bytes: bytes = b""
    stderr_bytes: bytes = b""


def _terminate_process_group(
    process: subprocess.Popen[bytes],
    *,
    grace_seconds: float = TERMINATION_GRACE_SECONDS,
) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait(timeout=TERMINATION_GRACE_SECONDS)
        return
    except OSError:
        process.terminate()
    # Keep the leader unreaped until both signals have been sent. Its live or
    # zombie pid pins the dedicated pgid, preventing reuse between phases.
    if grace_seconds:
        time.sleep(grace_seconds)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        process.kill()
    try:
        process.wait(timeout=TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=TERMINATION_GRACE_SECONDS)


def run_bounded(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout_seconds: float,
    env: Mapping[str, str] | None = None,
    input_text: str = "",
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    on_spawn: Callable[[int], None] | None = None,
    lifetime_lock_path: Path | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> BoundedCompletedProcess:
    if not argv:
        raise ValueError("argv must not be empty")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(float(timeout_seconds))
        or timeout_seconds <= 0
    ):
        raise ValueError("timeout_seconds must be positive")
    if (
        type(max_output_bytes) is not int
        or max_output_bytes <= 0
    ):
        raise ValueError("max_output_bytes must be positive")
    if (on_spawn is None) != (lifetime_lock_path is None):
        raise ValueError(
            "on_spawn and lifetime_lock_path must be provided together"
        )
    if on_spawn is not None and os.name != "posix":
        raise ValueError("tracked subprocess execution requires POSIX")

    process: subprocess.Popen[bytes] | None = None
    selector: selectors.BaseSelector | None = None
    gate_read: int | None = None
    gate_write: int | None = None
    lifetime_handle = None
    buffers: dict[str, bytearray] = {
        "stdout": bytearray(),
        "stderr": bytearray(),
    }
    timed_out = False
    output_limited = False
    cancelled = False
    streaming_complete = False
    process_group_terminated = False

    with tempfile.TemporaryFile() as stdin_file:
        stdin_file.write(input_text.encode("utf-8"))
        stdin_file.seek(0)
        try:
            process_argv = list(argv)
            popen_options: dict[str, object] = {}
            if on_spawn is not None and lifetime_lock_path is not None:
                lifetime_handle = open_inherited_lifetime_lock(
                    lifetime_lock_path
                )
                gate_read, gate_write = os.pipe()
                os.set_inheritable(gate_read, True)
                bootstrap = (
                    "import os,signal,subprocess,sys,time;"
                    "termination_requested=[False];"
                    "signal.signal(signal.SIGTERM,lambda *_:"
                    "termination_requested.__setitem__(0,True));"
                    "fd=int(sys.argv[1]);"
                    "token=os.read(fd,1);"
                    "os.close(fd);"
                    "os._exit(125) if token!=b'1' else None;"
                    "child=subprocess.Popen(sys.argv[2:],close_fds=True);"
                    "returncode=child.wait();"
                    f"time.sleep({LIFETIME_ANCHOR_GUARD_SECONDS!r}) "
                    "if termination_requested[0] else None;"
                    "os._exit(returncode if returncode>=0 else 128-returncode)"
                )
                process_argv = [
                    sys.executable,
                    "-I",
                    "-c",
                    bootstrap,
                    str(gate_read),
                    *process_argv,
                ]
                popen_options["pass_fds"] = (
                    gate_read,
                    lifetime_handle.fileno(),
                )
            process = subprocess.Popen(
                process_argv,
                cwd=str(Path(cwd)),
                stdin=stdin_file,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=dict(env) if env is not None else None,
                start_new_session=True,
                close_fds=True,
                **popen_options,
            )
            if gate_read is not None:
                os.close(gate_read)
                gate_read = None
            if on_spawn is not None:
                assert lifetime_handle is not None
                # The trusted wrapper already inherited this open file
                # description. Close the parent's copy before the observer
                # probes ownership so BSD flock semantics cannot mistake a
                # same-process re-lock for an unlocked lifetime file.
                lifetime_handle.close()
                lifetime_handle = None
                on_spawn(process.pid)
                assert gate_write is not None
                os.write(gate_write, b"1")
                os.close(gate_write)
                gate_write = None
            assert process.stdout is not None
            assert process.stderr is not None
            selector = selectors.DefaultSelector()
            for stream, label in (
                (process.stdout, "stdout"),
                (process.stderr, "stderr"),
            ):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, label)

            captured = 0
            deadline = time.monotonic() + timeout_seconds
            while selector.get_map():
                if cancel_check is not None and cancel_check():
                    cancelled = True
                    _terminate_process_group(process)
                    process_group_terminated = True
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    _terminate_process_group(process)
                    process_group_terminated = True
                    break
                events = selector.select(timeout=min(remaining, 0.1))
                if not events:
                    continue
                for key, _ in events:
                    try:
                        chunk = os.read(key.fileobj.fileno(), 64 * 1024)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                        continue
                    if captured + len(chunk) > max_output_bytes:
                        allowed = max(0, max_output_bytes - captured)
                        if allowed:
                            buffers[key.data].extend(chunk[:allowed])
                        captured = max_output_bytes
                        output_limited = True
                        _terminate_process_group(process)
                        process_group_terminated = True
                        break
                    buffers[key.data].extend(chunk)
                    captured += len(chunk)
                if output_limited:
                    break
            streaming_complete = not timed_out and not output_limited
        finally:
            if gate_read is not None:
                os.close(gate_read)
            if gate_write is not None:
                os.close(gate_write)
            if lifetime_handle is not None:
                lifetime_handle.close()
            if process is not None and not process_group_terminated:
                # EOF only proves that every descendant closed or redirected
                # the captured streams.  Always signal the still-pinned
                # dedicated group before reaping the leader so a daemonized
                # descendant cannot outlive a successful-looking CLI exit.
                _terminate_process_group(
                    process,
                    grace_seconds=(
                        0.0 if streaming_complete else TERMINATION_GRACE_SECONDS
                    ),
                )
                process_group_terminated = True
            if selector is not None:
                for key in list(selector.get_map().values()):
                    try:
                        selector.unregister(key.fileobj)
                    except Exception:
                        pass
                    key.fileobj.close()
                selector.close()
            if process is not None:
                for stream in (process.stdout, process.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()

    if process is None:  # pragma: no cover - Popen either returns or raises.
        raise RuntimeError("subprocess was not started")
    returncode = process.wait()
    stdout_bytes = bytes(buffers["stdout"])
    stderr_bytes = bytes(buffers["stderr"])
    return BoundedCompletedProcess(
        args=tuple(str(item) for item in argv),
        returncode=returncode,
        stdout=stdout_bytes.decode("utf-8", errors="replace"),
        stderr=stderr_bytes.decode("utf-8", errors="replace"),
        timed_out=timed_out,
        output_limited=output_limited,
        cancelled=cancelled,
        stdout_bytes=stdout_bytes,
        stderr_bytes=stderr_bytes,
    )

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Iterator, TextIO

from .errors import DyroError


_LOCK_CONDITION = threading.Condition()
_HELD_LOCKS: dict[Path, tuple[int, int, TextIO | None]] = {}


def _fsync_directory(path: Path) -> None:
    """Best-effort directory sync after replacing a state file."""
    if os.name == "nt":
        return
    try:
        descriptor = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def atomic_write_bytes(path: Path, content: bytes) -> None:
    """Durably replace one small state file without exposing a partial write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(prefix=f".{path.name}.", dir=path.parent, delete=False) as handle:
            temporary_name = handle.name
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        _fsync_directory(path.parent)
    finally:
        if temporary_name:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass


def atomic_write_text(path: Path, content: str) -> None:
    atomic_write_bytes(path, content.encode("utf-8"))


def append_text(path: Path, content: str) -> None:
    """Append a complete ledger entry and flush it to disk before returning."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _try_lock(handle: TextIO) -> bool:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        if not handle.read(1):
            handle.seek(0)
            handle.write("0")
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def _unlock(handle: TextIO) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def exclusive_lock(path: Path, *, timeout_seconds: float = 15.0) -> Iterator[None]:
    """Acquire a process-safe, thread-reentrant lock for a small state transition."""
    resolved = path.resolve()
    owner = threading.get_ident()
    deadline = time.monotonic() + timeout_seconds
    reentrant = False

    with _LOCK_CONDITION:
        while resolved in _HELD_LOCKS:
            held_owner, depth, handle = _HELD_LOCKS[resolved]
            if held_owner == owner and handle is not None:
                _HELD_LOCKS[resolved] = (owner, depth + 1, handle)
                reentrant = True
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DyroError(f"等待状态锁超时：{path}")
            _LOCK_CONDITION.wait(timeout=remaining)
        if not reentrant:
            _HELD_LOCKS[resolved] = (owner, 1, None)

    handle: TextIO | None = None
    try:
        if not reentrant:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            handle = resolved.open("a+", encoding="utf-8")
            while not _try_lock(handle):
                if time.monotonic() >= deadline:
                    raise DyroError(f"等待状态锁超时：{path}")
                time.sleep(0.05)
            with _LOCK_CONDITION:
                _HELD_LOCKS[resolved] = (owner, 1, handle)
        yield
    finally:
        release_handle: TextIO | None = None
        with _LOCK_CONDITION:
            held = _HELD_LOCKS.get(resolved)
            if held is not None and held[0] == owner:
                held_owner, depth, held_handle = held
                if depth > 1:
                    _HELD_LOCKS[resolved] = (held_owner, depth - 1, held_handle)
                else:
                    _HELD_LOCKS.pop(resolved, None)
                    release_handle = held_handle
                    _LOCK_CONDITION.notify_all()
        if release_handle is not None:
            try:
                _unlock(release_handle)
            finally:
                release_handle.close()
        elif handle is not None:
            handle.close()

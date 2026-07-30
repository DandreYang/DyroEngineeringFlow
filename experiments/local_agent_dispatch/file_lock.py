"""Cross-platform advisory lock helper for local dispatch state transitions."""

from __future__ import annotations

from contextlib import contextmanager
from io import TextIOWrapper
import os
from pathlib import Path
import stat
from typing import Iterator

from .errors import DispatchValidationError

if os.name == "nt":
    import msvcrt
else:
    import fcntl


def _lock(handle) -> None:
    if os.name == "nt":
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write("\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock(handle) -> None:
    if os.name == "nt":
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _open_lock_descriptor(path: Path, *, create: bool) -> int:
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
    if create:
        flags |= os.O_CREAT
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise DispatchValidationError(
            f"state lock cannot be opened safely: {path}"
        ) from exc
    try:
        opened = os.fstat(descriptor)
        linked = os.stat(path, follow_symlinks=False)
        if stat.S_ISLNK(linked.st_mode) or not os.path.samestat(opened, linked):
            raise DispatchValidationError(
                f"state lock path changed while opening: {path}"
            )
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def open_inherited_lifetime_lock(path: Path) -> TextIOWrapper:
    """Acquire a lock whose open file description may be inherited by a child."""
    if os.name != "posix":
        raise DispatchValidationError(
            "inherited lifetime locks require a POSIX host"
        )
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = _open_lock_descriptor(path, create=True)
    handle = os.fdopen(descriptor, "r+", encoding="utf-8")
    try:
        _lock(handle)
        os.set_inheritable(handle.fileno(), True)
    except Exception:
        handle.close()
        raise
    return handle


def file_lock_is_held(path: Path) -> bool | None:
    """Return held/unlocked, or ``None`` when ownership cannot be proven."""
    if os.name != "posix":
        return None
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        return None
    try:
        descriptor = _open_lock_descriptor(path, create=False)
    except DispatchValidationError:
        return None
    with os.fdopen(descriptor, "r+", encoding="utf-8") as handle:
        try:
            fcntl.flock(
                handle.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError:
            return True
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            return None
    return False


@contextmanager
def exclusive_file_lock(path: Path) -> Iterator[None]:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = _open_lock_descriptor(path, create=True)
    with os.fdopen(descriptor, "r+", encoding="utf-8") as handle:
        try:
            _lock(handle)
            yield
        finally:
            _unlock(handle)

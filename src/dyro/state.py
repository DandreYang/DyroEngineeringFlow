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
_HELD_LOCKS: dict[object, tuple[int, int, TextIO | None]] = {}


def fsync_directory(path: Path) -> None:
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
        fsync_directory(path.parent)
    finally:
        if temporary_name:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass


def atomic_write_text(path: Path, content: str) -> None:
    atomic_write_bytes(path, content.encode("utf-8"))


def create_only_bytes(path: Path, content: bytes, *, mode: int = 0o600) -> None:
    """Durably create a small file once, refusing symlinks and replacement."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, mode)
    except FileExistsError as exc:
        raise DyroError(f"拒绝覆盖已存在的状态文件：{path}") from exc
    except OSError as exc:
        raise DyroError(f"无法安全创建状态文件：{path}") from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        fsync_directory(path.parent)


def create_only_text(path: Path, content: str, *, mode: int = 0o600) -> None:
    create_only_bytes(path, content.encode("utf-8"), mode=mode)


def _directory_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _open_safe_directory(path: Path) -> int:
    try:
        return os.open(path, _directory_flags())
    except OSError as exc:
        raise DyroError(f"无法安全打开状态目录：{path}") from exc


def open_safe_directory(path: Path) -> int:
    """Open an existing directory without following a terminal symlink.

    The caller owns the descriptor and must close it.  It is intended for
    state stores that need to keep a directory identity stable across a whole
    transaction rather than re-resolving a mutable pathname.
    """
    return _open_safe_directory(path)


def open_safe_child_directory(parent_fd: int, name: str, *, create: bool = False, mode: int = 0o700) -> int:
    """Open one direct non-symlink child directory relative to ``parent_fd``."""
    if not name or Path(name).name != name:
        raise DyroError(f"状态目录名非法：{name!r}")
    try:
        return os.open(name, _directory_flags(), dir_fd=parent_fd)
    except FileNotFoundError:
        if not create:
            raise DyroError(f"状态目录不存在：{name}") from None
        try:
            os.mkdir(name, mode=mode, dir_fd=parent_fd)
        except FileExistsError:
            pass
        try:
            descriptor = os.open(name, _directory_flags(), dir_fd=parent_fd)
        except OSError as exc:
            raise DyroError(f"状态目录必须是安全的普通目录：{name}") from exc
        os.fsync(parent_fd)
        return descriptor


def ensure_safe_child_directory(parent: Path, name: str, *, mode: int = 0o700) -> Path:
    """Ensure one direct child directory using directory descriptors on POSIX.

    The child is opened relative to an already-opened parent, so replacing a
    pathname with a symlink between validation and creation cannot redirect
    state writes outside that parent.
    """
    if not name or Path(name).name != name:
        raise DyroError(f"状态目录名非法：{name!r}")
    child = parent / name
    if os.name == "nt":
        if child.is_symlink() or (child.exists() and not child.is_dir()):
            raise DyroError(f"状态目录必须是安全的普通目录：{child}")
        child.mkdir(mode=mode, exist_ok=True)
        return child
    parent_fd = _open_safe_directory(parent)
    try:
        child_fd = open_safe_child_directory(parent_fd, name, create=True, mode=mode)
    finally:
        os.close(parent_fd)
    os.close(child_fd)
    return child


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
    resolved = path.absolute()
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
            flags = os.O_RDWR | os.O_CREAT
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                descriptor = os.open(resolved, flags, 0o600)
            except OSError as exc:
                raise DyroError(f"无法安全打开状态锁：{path}") from exc
            handle = os.fdopen(descriptor, "a+", encoding="utf-8")
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


@contextmanager
def exclusive_directory_lock(directory: Path, name: str, *, timeout_seconds: float = 15.0) -> Iterator[None]:
    """Lock one file below an opened non-symlink directory on POSIX.

    Unlike ``exclusive_lock(directory / name)``, the lock file is opened with
    ``dir_fd``.  A concurrent replacement of ``directory`` by a symlink thus
    cannot redirect the lock creation outside the workspace.
    """
    if not name or Path(name).name != name:
        raise DyroError(f"状态锁名非法：{name!r}")
    if os.name == "nt":
        raise DyroError("Windows 暂不支持安全的目录描述符锁；拒绝创建状态锁以避免 reparse-point 路径逃逸")
    if not hasattr(os, "O_NOFOLLOW"):
        raise DyroError("当前平台缺少安全的目录描述符锁；拒绝创建状态锁以避免路径逃逸")

    directory_fd = _open_safe_directory(directory)
    handle: TextIO | None = None
    key: object | None = None
    owner = threading.get_ident()
    deadline = time.monotonic() + timeout_seconds
    reentrant = False
    try:
        identity = os.fstat(directory_fd)
        key = ("directory-lock", identity.st_dev, identity.st_ino, name)
        with _LOCK_CONDITION:
            while key in _HELD_LOCKS:
                held_owner, depth, held_handle = _HELD_LOCKS[key]
                if held_owner == owner and held_handle is not None:
                    _HELD_LOCKS[key] = (owner, depth + 1, held_handle)
                    reentrant = True
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DyroError(f"等待状态锁超时：{directory / name}")
                _LOCK_CONDITION.wait(timeout=remaining)
            if not reentrant:
                _HELD_LOCKS[key] = (owner, 1, None)
        if not reentrant:
            flags = os.O_RDWR | os.O_CREAT
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                descriptor = os.open(name, flags, 0o600, dir_fd=directory_fd)
            except OSError as exc:
                raise DyroError(f"无法安全打开状态锁：{directory / name}") from exc
            handle = os.fdopen(descriptor, "a+", encoding="utf-8")
            while not _try_lock(handle):
                if time.monotonic() >= deadline:
                    raise DyroError(f"等待状态锁超时：{directory / name}")
                time.sleep(0.05)
            with _LOCK_CONDITION:
                _HELD_LOCKS[key] = (owner, 1, handle)
        yield
    finally:
        release_handle: TextIO | None = None
        if key is not None:
            with _LOCK_CONDITION:
                held = _HELD_LOCKS.get(key)
                if held is not None and held[0] == owner:
                    held_owner, depth, held_handle = held
                    if depth > 1:
                        _HELD_LOCKS[key] = (held_owner, depth - 1, held_handle)
                    else:
                        _HELD_LOCKS.pop(key, None)
                        release_handle = held_handle
                        _LOCK_CONDITION.notify_all()
        try:
            if release_handle is not None:
                try:
                    _unlock(release_handle)
                finally:
                    release_handle.close()
            elif handle is not None:
                handle.close()
        finally:
            os.close(directory_fd)

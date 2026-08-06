"""Bounded, side-effect-free file reads for machine-facing observations."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
import math
import os
from pathlib import Path
import stat
import time
from typing import Callable, Iterator

from .errors import ValidationError


class ReadLimitCode(str, Enum):
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    AGGREGATE_BYTES_EXCEEDED = "AGGREGATE_BYTES_EXCEEDED"
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
    RECORD_LIMIT_EXCEEDED = "RECORD_LIMIT_EXCEEDED"
    UNSAFE_FILE = "UNSAFE_FILE"


class ReadLimitError(ValidationError):
    """A typed observation limit failure whose message is never transported."""

    def __init__(self, code: ReadLimitCode, message: str) -> None:
        super().__init__(message)
        self.code = code


def _positive_int(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValidationError(f"{label} 必须是正整数")


_PROTOCOL_LIMIT_CEILINGS = {
    "profile_bytes": 1024 * 1024,
    "registry_bytes": 1024 * 1024,
    "registry_records": 500,
    "task_manifest_bytes": 256 * 1024,
    "task_status_bytes": 4096,
    "task_records": 2000,
    "line_manifest_bytes": 256 * 1024,
    "line_records": 2000,
    "objective_metadata_bytes": 256 * 1024,
    "objective_events_bytes": 8 * 1024 * 1024,
    "objective_event_records": 10_000,
    "objective_records": 500,
    "response_records": 100,
    "aggregate_bytes": 64 * 1024 * 1024,
}
_PROTOCOL_DEADLINE_SECONDS = 5.0


@dataclass(frozen=True)
class ObservationLimits:
    profile_bytes: int = _PROTOCOL_LIMIT_CEILINGS["profile_bytes"]
    registry_bytes: int = _PROTOCOL_LIMIT_CEILINGS["registry_bytes"]
    registry_records: int = _PROTOCOL_LIMIT_CEILINGS["registry_records"]
    task_manifest_bytes: int = _PROTOCOL_LIMIT_CEILINGS["task_manifest_bytes"]
    task_status_bytes: int = _PROTOCOL_LIMIT_CEILINGS["task_status_bytes"]
    task_records: int = _PROTOCOL_LIMIT_CEILINGS["task_records"]
    line_manifest_bytes: int = _PROTOCOL_LIMIT_CEILINGS["line_manifest_bytes"]
    line_records: int = _PROTOCOL_LIMIT_CEILINGS["line_records"]
    objective_metadata_bytes: int = _PROTOCOL_LIMIT_CEILINGS["objective_metadata_bytes"]
    objective_events_bytes: int = _PROTOCOL_LIMIT_CEILINGS["objective_events_bytes"]
    objective_event_records: int = _PROTOCOL_LIMIT_CEILINGS["objective_event_records"]
    objective_records: int = _PROTOCOL_LIMIT_CEILINGS["objective_records"]
    response_records: int = _PROTOCOL_LIMIT_CEILINGS["response_records"]
    aggregate_bytes: int = _PROTOCOL_LIMIT_CEILINGS["aggregate_bytes"]
    deadline_seconds: float = _PROTOCOL_DEADLINE_SECONDS

    def __post_init__(self) -> None:
        for label, ceiling in _PROTOCOL_LIMIT_CEILINGS.items():
            value = getattr(self, label)
            _positive_int(value, label)
            if value > ceiling:
                raise ValidationError(f"{label} 不得超过协议上限 {ceiling}")
        if (
            isinstance(self.deadline_seconds, bool)
            or not isinstance(self.deadline_seconds, (int, float))
            or not math.isfinite(self.deadline_seconds)
            or not 0 < self.deadline_seconds <= _PROTOCOL_DEADLINE_SECONDS
        ):
            raise ValidationError(
                f"deadline_seconds 必须是不超过 {_PROTOCOL_DEADLINE_SECONDS} 的有限正数"
            )


def _directory_flags() -> int:
    if os.name == "nt" or not hasattr(os, "O_NOFOLLOW"):
        raise ReadLimitError(
            ReadLimitCode.UNSAFE_FILE,
            "Platform lacks safe directory traversal support",
        )
    return os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW


def _checked_absolute(path: Path, label: str) -> Path:
    if any(part in {".", ".."} for part in path.parts):
        raise ValidationError(f"{label} 不得包含点路径分量")
    absolute = path.absolute()
    if not absolute.is_absolute() or not absolute.anchor:
        raise ValidationError(f"{label} 必须是绝对路径")
    return absolute


def _open_absolute_directory(
    path: Path, check: Callable[[], None] | None = None
) -> int:
    """Open every path component without following directory symlinks."""

    absolute = _checked_absolute(path, "directory")
    flags = _directory_flags()
    if check is not None:
        check()
    descriptor = os.open(absolute.anchor, flags)
    try:
        for part in absolute.parts[1:]:
            if check is not None:
                check()
            child = os.open(part, flags, dir_fd=descriptor)
            parent = descriptor
            descriptor = child
            os.close(parent)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


@contextmanager
def open_safe_directory_chain(
    root: Path,
    directory: Path,
    *,
    allow_missing: bool = False,
    expected_root_identity: tuple[int, int] | None = None,
    check: Callable[[], None] | None = None,
    identity_check: Callable[[Path, tuple[int, int] | None], None] | None = None,
) -> Iterator[int | None]:
    """Hold a descriptor-bound, non-symlink directory chain below ``root``."""

    absolute_root = _checked_absolute(root, "workspace root")
    absolute_directory = _checked_absolute(directory, "state directory")
    try:
        relative = absolute_directory.relative_to(absolute_root)
    except ValueError as exc:
        raise ValidationError(
            "Observation state directory escapes workspace root"
        ) from exc

    descriptor: int | None = None
    root_opened = False
    try:
        descriptor = _open_absolute_directory(absolute_root, check)
        root_opened = True
        root_info = os.fstat(descriptor)
        root_identity = (root_info.st_dev, root_info.st_ino)
        if (
            expected_root_identity is not None
            and root_identity != expected_root_identity
        ):
            raise ReadLimitError(
                ReadLimitCode.UNSAFE_FILE,
                "Observation workspace root identity changed",
            )
        if identity_check is not None:
            identity_check(absolute_root, root_identity)
        current = absolute_root
        for part in relative.parts:
            if check is not None:
                check()
            next_path = current / part
            try:
                child = os.open(part, _directory_flags(), dir_fd=descriptor)
            except FileNotFoundError:
                if identity_check is not None:
                    identity_check(next_path, None)
                raise
            parent = descriptor
            descriptor = child
            os.close(parent)
            current = next_path
            child_info = os.fstat(descriptor)
            if identity_check is not None:
                identity_check(current, (child_info.st_dev, child_info.st_ino))
    except FileNotFoundError as exc:
        if descriptor is not None:
            os.close(descriptor)
            descriptor = None
        if allow_missing and root_opened:
            yield None
            return
        if not root_opened:
            raise ReadLimitError(
                ReadLimitCode.UNSAFE_FILE,
                "Observation workspace root is not safe",
            ) from exc
        raise
    except PermissionError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise ReadLimitError(
            ReadLimitCode.UNSAFE_FILE,
            "Observation state directory is not safe",
        ) from exc
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        raise
    try:
        yield descriptor
    finally:
        if descriptor is not None:
            os.close(descriptor)


@dataclass
class ReadBudget:
    limits: ObservationLimits
    monotonic: Callable[[], float] = time.monotonic
    _started_at: float = field(init=False, repr=False)
    _bytes_read: int = field(default=0, init=False, repr=False)
    _root_identities: dict[str, tuple[int, int]] = field(
        default_factory=dict, init=False, repr=False
    )
    _directory_identities: dict[str, tuple[int, int] | None] = field(
        default_factory=dict, init=False, repr=False
    )
    _file_identities: dict[str, tuple[int, int]] = field(
        default_factory=dict, init=False, repr=False
    )

    def __post_init__(self) -> None:
        if not isinstance(self.limits, ObservationLimits):
            raise ValidationError("read budget limits 必须是 ObservationLimits")
        if not callable(self.monotonic):
            raise ValidationError("read budget monotonic 必须可调用")
        started_at = self.monotonic()
        if (
            isinstance(started_at, bool)
            or not isinstance(started_at, (int, float))
            or not math.isfinite(started_at)
        ):
            raise ValidationError("read budget monotonic 必须返回有限数值")
        self._started_at = started_at

    @property
    def bytes_read(self) -> int:
        return self._bytes_read

    def check_deadline(self) -> None:
        current = self.monotonic()
        if (
            isinstance(current, bool)
            or not isinstance(current, (int, float))
            or not math.isfinite(current)
            or current < self._started_at
        ):
            raise ValidationError("read budget monotonic 返回了无效数值")
        if current - self._started_at > self.limits.deadline_seconds:
            raise ReadLimitError(
                ReadLimitCode.DEADLINE_EXCEEDED,
                "Core observation deadline exceeded",
            )

    def remaining_seconds(self) -> float:
        """Return the bounded wall budget available to an allowed subprocess."""
        current = self.monotonic()
        if (
            isinstance(current, bool)
            or not isinstance(current, (int, float))
            or not math.isfinite(current)
            or current < self._started_at
        ):
            raise ValidationError("read budget monotonic 返回了无效数值")
        remaining = self.limits.deadline_seconds - (current - self._started_at)
        if remaining <= 0:
            raise ReadLimitError(
                ReadLimitCode.DEADLINE_EXCEEDED,
                "Core observation deadline exceeded",
            )
        return remaining

    def _charge(self, size: int) -> None:
        if self._bytes_read + size > self.limits.aggregate_bytes:
            raise ReadLimitError(
                ReadLimitCode.AGGREGATE_BYTES_EXCEEDED,
                "Aggregate observation byte budget exceeded",
            )
        self._bytes_read += size

    def _root_identity(self, root: Path) -> tuple[int, int]:
        absolute = _checked_absolute(root, "workspace root")
        key = str(absolute)
        expected = self._root_identities.get(key)
        try:
            descriptor = _open_absolute_directory(absolute, self.check_deadline)
        except PermissionError:
            raise
        except OSError as exc:
            raise ReadLimitError(
                ReadLimitCode.UNSAFE_FILE,
                "Observation workspace root is not safe",
            ) from exc
        try:
            info = os.fstat(descriptor)
            current = (info.st_dev, info.st_ino)
        finally:
            os.close(descriptor)
        if expected is None:
            self._root_identities[key] = current
            return current
        if current != expected:
            raise ReadLimitError(
                ReadLimitCode.UNSAFE_FILE,
                "Observation workspace root identity changed",
            )
        return expected

    def _bind_directory_identity(
        self, path: Path, identity: tuple[int, int] | None
    ) -> None:
        key = str(_checked_absolute(path, "state directory"))
        if key not in self._directory_identities:
            self._directory_identities[key] = identity
            return
        if self._directory_identities[key] != identity:
            raise ReadLimitError(
                ReadLimitCode.UNSAFE_FILE,
                "Observation state directory identity changed",
            )

    def bind_directory_identity(self, path: Path, identity: tuple[int, int]) -> None:
        """Bind a directory identity captured during bounded enumeration."""

        self._bind_directory_identity(path, identity)

    def bind_file_identity(self, path: Path, identity: tuple[int, int]) -> None:
        """Bind a regular-file identity captured during bounded enumeration."""

        key = str(_checked_absolute(path, "state file"))
        expected = self._file_identities.get(key)
        if expected is None:
            self._file_identities[key] = identity
            return
        if expected != identity:
            raise ReadLimitError(
                ReadLimitCode.UNSAFE_FILE,
                "Observation state file identity changed",
            )

    @contextmanager
    def open_safe_directory_chain(
        self,
        root: Path,
        directory: Path,
        *,
        allow_missing: bool = False,
    ) -> Iterator[int | None]:
        expected = self._root_identity(root)
        with open_safe_directory_chain(
            root,
            directory,
            allow_missing=allow_missing,
            expected_root_identity=expected,
            check=self.check_deadline,
            identity_check=self._bind_directory_identity,
        ) as descriptor:
            if descriptor is None:
                self._bind_directory_identity(directory, None)
            yield descriptor

    def check_root_identity(self, root: Path) -> None:
        self._root_identity(root)

    def read_descriptor_bytes(
        self,
        descriptor: int,
        *,
        size: int,
        maximum_bytes: int,
        label: str,
    ) -> bytes:
        self.check_deadline()
        if size > maximum_bytes:
            raise ReadLimitError(
                ReadLimitCode.FILE_TOO_LARGE,
                f"{label} exceeds its byte limit",
            )
        if self._bytes_read + size > self.limits.aggregate_bytes:
            raise ReadLimitError(
                ReadLimitCode.AGGREGATE_BYTES_EXCEEDED,
                "Aggregate observation byte budget exceeded",
            )
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size != size:
            raise ReadLimitError(
                ReadLimitCode.UNSAFE_FILE,
                f"{label} changed before bounded read",
            )
        chunks: list[bytes] = []
        remaining = size
        while remaining > 0:
            self.check_deadline()
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            self._charge(len(chunk))
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        after = os.fstat(descriptor)
        if (
            len(content) != size
            or after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
            or after.st_ctime_ns != before.st_ctime_ns
        ):
            raise ReadLimitError(
                ReadLimitCode.UNSAFE_FILE,
                f"{label} changed during bounded read",
            )
        return content

    def read_regular_bytes(
        self,
        path: Path,
        *,
        maximum_bytes: int,
        label: str,
    ) -> bytes:
        """Read one final-path regular file through the descriptor that was checked."""

        self.check_deadline()
        try:
            before = path.lstat()
        except OSError:
            raise
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise ReadLimitError(
                ReadLimitCode.UNSAFE_FILE,
                f"{label} is not a safe regular file",
            )
        if before.st_size > maximum_bytes:
            raise ReadLimitError(
                ReadLimitCode.FILE_TOO_LARGE,
                f"{label} exceeds its byte limit",
            )
        flags = (
            os.O_RDONLY
            | (os.O_NOFOLLOW if hasattr(os, "O_NOFOLLOW") else 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_dev != before.st_dev
                or opened.st_ino != before.st_ino
            ):
                raise ReadLimitError(
                    ReadLimitCode.UNSAFE_FILE,
                    f"{label} changed during safe open",
                )
            return self.read_descriptor_bytes(
                descriptor,
                size=opened.st_size,
                maximum_bytes=maximum_bytes,
                label=label,
            )
        finally:
            os.close(descriptor)

    def read_regular_text(
        self,
        path: Path,
        *,
        maximum_bytes: int,
        label: str,
    ) -> str:
        return self.read_regular_bytes(
            path, maximum_bytes=maximum_bytes, label=label
        ).decode("utf-8")

    def read_regular_bytes_at(
        self,
        *,
        root: Path,
        directory: Path,
        name: str,
        maximum_bytes: int,
        label: str,
    ) -> bytes:
        """Read a file relative to a stable, safely traversed directory FD."""

        if not name or Path(name).name != name:
            raise ValidationError(f"{label} 文件名无效")
        self.check_deadline()
        with self.open_safe_directory_chain(root, directory) as directory_fd:
            assert directory_fd is not None
            return self.read_regular_bytes_from_directory_fd(
                directory_fd,
                name=name,
                maximum_bytes=maximum_bytes,
                label=label,
                identity_path=directory / name,
            )

    def read_regular_bytes_from_directory_fd(
        self,
        directory_fd: int,
        *,
        name: str,
        maximum_bytes: int,
        label: str,
        identity_path: Path | None = None,
    ) -> bytes:
        """Read a safe regular file relative to an already-bound directory."""

        self.check_deadline()
        if not name or Path(name).name != name:
            raise ValidationError(f"{label} 文件名无效")
        flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
        try:
            descriptor = os.open(name, flags, dir_fd=directory_fd)
        except (FileNotFoundError, PermissionError):
            raise
        except OSError as exc:
            raise ReadLimitError(
                ReadLimitCode.UNSAFE_FILE,
                f"{label} is not a safe regular file",
            ) from exc
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise ReadLimitError(
                    ReadLimitCode.UNSAFE_FILE,
                    f"{label} is not a safe regular file",
                )
            if identity_path is not None:
                self.bind_file_identity(
                    identity_path,
                    (info.st_dev, info.st_ino),
                )
            return self.read_descriptor_bytes(
                descriptor,
                size=info.st_size,
                maximum_bytes=maximum_bytes,
                label=label,
            )
        finally:
            os.close(descriptor)

    def read_regular_text_at(
        self,
        *,
        root: Path,
        directory: Path,
        name: str,
        maximum_bytes: int,
        label: str,
    ) -> str:
        return self.read_regular_bytes_at(
            root=root,
            directory=directory,
            name=name,
            maximum_bytes=maximum_bytes,
            label=label,
        ).decode("utf-8")


def require_safe_directory_chain(
    root: Path, directory: Path, *, allow_missing: bool = False
) -> bool:
    """Validate a directory chain using the same safe traversal primitive."""

    with open_safe_directory_chain(
        root, directory, allow_missing=allow_missing
    ) as descriptor:
        return descriptor is not None

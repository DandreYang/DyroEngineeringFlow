"""Path and content secret guard before context injection (ADR-0002)."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat
from typing import Iterable, Mapping

from .errors import DispatchValidationError


CREDENTIAL_BASENAMES = frozenset(
    {
        ".env",
        ".npmrc",
        ".netrc",
        ".pgpass",
        "credentials",
        "credentials.json",
        "service-account.json",
        "id_rsa",
        "id_ed25519",
        "id_ecdsa",
    }
)
CREDENTIAL_PREFIXES = (".env.", "id_rsa.", "id_ed25519.")
CREDENTIAL_EXTENSIONS = frozenset({".pem", ".key", ".p12", ".pfx", ".keystore"})
CREDENTIAL_DIRS = frozenset({".ssh", ".aws", ".gnupg", ".kube"})

CONTENT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{36,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)
MAX_CONTEXT_FILE_BYTES = 512 * 1024


@dataclass(frozen=True)
class GuardVerdict:
    allowed: bool
    reason: str = ""

    def to_mapping(self) -> dict[str, object]:
        return {"allowed": self.allowed, "reason": self.reason}


def _resolve_existing(path: Path) -> Path:
    if path.exists():
        return path.resolve()
    return path.expanduser().absolute()


def check_path(abs_file: Path, root_dir: Path) -> GuardVerdict:
    root = root_dir.resolve()
    candidate = _resolve_existing(Path(abs_file))
    try:
        candidate.relative_to(root)
    except ValueError:
        return GuardVerdict(False, f"path escapes workspace: {abs_file}")

    base = candidate.name
    if base in CREDENTIAL_BASENAMES:
        return GuardVerdict(False, f"credential basename: {base}")
    if any(base.startswith(prefix) for prefix in CREDENTIAL_PREFIXES):
        return GuardVerdict(False, f"credential prefix: {base}")
    if candidate.suffix in CREDENTIAL_EXTENSIONS:
        return GuardVerdict(False, f"credential extension: {candidate.suffix}")
    for segment in candidate.parts:
        if segment in CREDENTIAL_DIRS:
            return GuardVerdict(False, f"sensitive directory segment: {segment}")
    return GuardVerdict(True)


def check_content(content: str, *, file_label: str = "") -> GuardVerdict:
    for pattern in CONTENT_PATTERNS:
        if pattern.search(content):
            label = file_label or "<buffer>"
            return GuardVerdict(
                False,
                f"secret-like content matched /{pattern.pattern[:40]}/ in {label}",
            )
    return GuardVerdict(True)


def _same_file_identity(
    left: os.stat_result,
    right: os.stat_result,
) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _read_stable_descriptor(
    file_descriptor: int,
    *,
    raw_path: Path,
    max_bytes: int,
) -> tuple[bytearray, os.stat_result]:
    before = os.fstat(file_descriptor)
    if not stat.S_ISREG(before.st_mode):
        raise DispatchValidationError(
            f"context path is not a regular file: {raw_path}"
        )
    if before.st_size > max_bytes:
        raise DispatchValidationError(
            f"context file exceeds byte limit: {before.st_size} > {max_bytes}"
        )
    raw = bytearray()
    while len(raw) <= max_bytes:
        chunk = os.read(
            file_descriptor,
            min(64 * 1024, max_bytes + 1 - len(raw)),
        )
        if not chunk:
            break
        raw.extend(chunk)
    if len(raw) > max_bytes:
        raise DispatchValidationError(
            f"context file exceeds byte limit while reading: {raw_path}"
        )
    after = os.fstat(file_descriptor)
    if (
        not _same_file_identity(before, after)
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        raise DispatchValidationError(
            f"context file changed while being read: {raw_path}"
        )
    return raw, after


def _snapshot_path_chain(
    root: Path,
    relative: Path,
    *,
    raw_path: Path,
) -> tuple[list[tuple[Path, os.stat_result]], os.stat_result]:
    """Snapshot a resolved, non-link path chain for no-dir_fd platforms."""
    directories: list[tuple[Path, os.stat_result]] = []
    current = root
    for component in (".", *relative.parts[:-1]):
        if component != ".":
            current /= component
        metadata = current.lstat()
        is_junction = getattr(current, "is_junction", None)
        if (
            stat.S_ISLNK(metadata.st_mode)
            or (callable(is_junction) and is_junction())
            or not stat.S_ISDIR(metadata.st_mode)
        ):
            raise DispatchValidationError(
                f"context path cannot be opened safely: {raw_path}"
            )
        directories.append((current, metadata))

    file_path = root / relative
    file_metadata = file_path.lstat()
    is_junction = getattr(file_path, "is_junction", None)
    if (
        stat.S_ISLNK(file_metadata.st_mode)
        or (callable(is_junction) and is_junction())
        or not stat.S_ISREG(file_metadata.st_mode)
    ):
        raise DispatchValidationError(
            f"context path is not a regular file: {raw_path}"
        )
    return directories, file_metadata


def read_guarded_file(
    path: Path,
    root_dir: Path,
    *,
    max_bytes: int = MAX_CONTEXT_FILE_BYTES,
) -> tuple[str, str]:
    """Read one regular UTF-8 file through the safest available path API."""
    raw_path = Path(path)
    if raw_path.is_symlink():
        raise DispatchValidationError(f"context path is a symbolic link: {raw_path}")
    try:
        root = Path(root_dir).resolve(strict=True)
        candidate = raw_path.resolve(strict=True)
        relative = candidate.relative_to(root)
    except (OSError, ValueError) as exc:
        raise DispatchValidationError(
            f"context path escapes workspace: {raw_path}"
        ) from exc
    if not relative.parts:
        raise DispatchValidationError(
            f"context path is not a regular file: {raw_path}"
        )
    verdict = check_path(candidate, root)
    if not verdict.allowed:
        raise DispatchValidationError(verdict.reason)
    if type(max_bytes) is not int or max_bytes <= 0:
        raise DispatchValidationError("context byte limit must be positive")

    file_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    root_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptors: list[int] = []
    try:
        if os.open in getattr(os, "supports_dir_fd", ()):
            current = os.open(root, root_flags)
            descriptors.append(current)
            for component in relative.parts[:-1]:
                current = os.open(
                    component,
                    root_flags,
                    dir_fd=current,
                )
                descriptors.append(current)
            file_descriptor = os.open(
                relative.parts[-1],
                file_flags,
                dir_fd=current,
            )
            descriptors.append(file_descriptor)
            raw, _after = _read_stable_descriptor(
                file_descriptor,
                raw_path=raw_path,
                max_bytes=max_bytes,
            )
        else:
            before_directories, before_path = _snapshot_path_chain(
                root,
                relative,
                raw_path=raw_path,
            )
            file_descriptor = os.open(candidate, file_flags)
            descriptors.append(file_descriptor)
            opened = os.fstat(file_descriptor)
            if not _same_file_identity(before_path, opened):
                raise DispatchValidationError(
                    f"context file changed while being opened: {raw_path}"
                )
            raw, after = _read_stable_descriptor(
                file_descriptor,
                raw_path=raw_path,
                max_bytes=max_bytes,
            )
            after_directories, after_path = _snapshot_path_chain(
                root,
                relative,
                raw_path=raw_path,
            )
            if (
                len(before_directories) != len(after_directories)
                or any(
                    before_item[0] != after_item[0]
                    or not _same_file_identity(
                        before_item[1],
                        after_item[1],
                    )
                    for before_item, after_item in zip(
                        before_directories,
                        after_directories,
                        strict=True,
                    )
                )
                or not _same_file_identity(before_path, after_path)
                or not _same_file_identity(after, after_path)
                or after.st_size != after_path.st_size
                or after.st_mtime_ns != after_path.st_mtime_ns
            ):
                raise DispatchValidationError(
                    f"context path changed while being read: {raw_path}"
                )
    except (OSError, NotImplementedError) as exc:
        raise DispatchValidationError(
            f"context file cannot be opened safely: {raw_path}"
        ) from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)

    if b"\0" in raw:
        raise DispatchValidationError(
            f"binary file rejected for context injection: {raw_path}"
        )
    try:
        text = bytes(raw).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DispatchValidationError(
            f"non-utf8 file rejected for context injection: {raw_path}"
        ) from exc
    content_verdict = check_content(text, file_label=relative.as_posix())
    if not content_verdict.allowed:
        raise DispatchValidationError(content_verdict.reason)
    return relative.as_posix(), text


def guard_file(
    path: Path,
    root_dir: Path,
    *,
    read_content: bool = True,
    max_bytes: int = MAX_CONTEXT_FILE_BYTES,
) -> GuardVerdict:
    path = Path(path)
    path_verdict = check_path(path, root_dir)
    if not path_verdict.allowed:
        return path_verdict
    if not read_content:
        return path_verdict
    try:
        read_guarded_file(path, root_dir, max_bytes=max_bytes)
    except DispatchValidationError as exc:
        return GuardVerdict(False, str(exc))
    return GuardVerdict(True)


def assert_files_allowed(
    paths: Iterable[Path],
    root_dir: Path,
    *,
    read_content: bool = True,
    max_bytes: int = MAX_CONTEXT_FILE_BYTES,
) -> list[dict[str, object]]:
    """Return per-file verdicts; raise if any file is denied."""
    reports: list[dict[str, object]] = []
    denied: list[str] = []
    for path in paths:
        verdict = guard_file(
            path,
            root_dir,
            read_content=read_content,
            max_bytes=max_bytes,
        )
        entry = {"path": str(path), **verdict.to_mapping()}
        reports.append(entry)
        if not verdict.allowed:
            denied.append(f"{path}: {verdict.reason}")
    if denied:
        raise DispatchValidationError(
            "context guard denied file(s):\n" + "\n".join(denied)
        )
    return reports


def materialize_strict_shadow(
    *,
    shadow_root: Path,
    root_dir: Path,
    relative_files: Mapping[str, str] | None = None,
    file_paths: Iterable[Path] | None = None,
) -> Path:
    """
    Materialize guarded file contents under shadow_root preserving relative paths.

    Provide either relative_files (rel_posix -> content) or file_paths under root_dir.
    """
    shadow_root = Path(shadow_root)
    if shadow_root.exists():
        raise DispatchValidationError("shadow_root already exists")
    shadow_root.mkdir(parents=True)
    shadow_resolved = shadow_root.resolve()

    pairs: list[tuple[str, str]] = []
    if relative_files is not None:
        for rel, content in relative_files.items():
            if ".." in Path(rel).parts or Path(rel).is_absolute():
                raise DispatchValidationError(f"invalid relative path: {rel}")
            content_verdict = check_content(content, file_label=rel)
            if not content_verdict.allowed:
                raise DispatchValidationError(content_verdict.reason)
            pairs.append((rel, content))
    elif file_paths is not None:
        root = Path(root_dir).resolve()
        for path in file_paths:
            pairs.append(read_guarded_file(Path(path), root))
    else:
        raise DispatchValidationError("materialize_strict_shadow requires inputs")

    for rel, content in pairs:
        dest = (shadow_root / rel).resolve()
        try:
            dest.relative_to(shadow_resolved)
        except ValueError as exc:
            raise DispatchValidationError(f"shadow path escapes root: {rel}") from exc
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        os.chmod(dest, 0o600)
    return shadow_root

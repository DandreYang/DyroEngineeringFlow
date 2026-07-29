"""Race-resistant artifact validation rooted in trusted task worktrees."""

from __future__ import annotations

from collections.abc import Set
from dataclasses import dataclass
import hashlib
import hmac
import os
from pathlib import Path, PurePosixPath
import re
import stat
from types import MappingProxyType
from typing import Iterable, Mapping

from .errors import Stage0ValidationError


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REPOSITORY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@dataclass(frozen=True)
class ArtifactPolicy:
    repository_roots: Mapping[str, Path]
    allowed_paths: Set[tuple[str, str]]
    max_artifacts: int
    max_artifact_bytes: int
    max_total_bytes: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.repository_roots, Mapping) or not self.repository_roots:
            raise Stage0ValidationError("repository_roots must be a non-empty object")
        normalized_roots: dict[str, Path] = {}
        for repository, raw_root in dict(self.repository_roots).items():
            if not isinstance(repository, str) or not _REPOSITORY_ID.fullmatch(
                repository
            ):
                raise Stage0ValidationError(
                    f"repository root ID is invalid: {repository!r}"
                )
            try:
                root = Path(raw_root)
            except TypeError as exc:
                raise Stage0ValidationError(
                    f"repository root is invalid: {repository}"
                ) from exc
            if not root.is_absolute():
                raise Stage0ValidationError(
                    f"repository root must be absolute: {repository}"
                )
            if root.is_symlink() or not root.is_dir():
                raise Stage0ValidationError(
                    f"repository root must be an existing non-symlink directory: {repository}"
                )
            normalized_roots[repository] = root.resolve(strict=True)
        if not isinstance(self.allowed_paths, Set):
            raise Stage0ValidationError("allowed_paths must be a set")
        normalized_allowed_paths = frozenset(self.allowed_paths)
        for artifact_id in normalized_allowed_paths:
            if (
                not isinstance(artifact_id, tuple)
                or len(artifact_id) != 2
                or not all(isinstance(value, str) for value in artifact_id)
            ):
                raise Stage0ValidationError(
                    "allowed_paths contains an invalid declaration"
                )
            repository, raw_path = artifact_id
            if repository not in normalized_roots:
                raise Stage0ValidationError(
                    f"allowlisted artifact has no trusted root: {repository}"
                )
            _relative_parts(raw_path)
        object.__setattr__(
            self,
            "repository_roots",
            MappingProxyType(normalized_roots),
        )
        object.__setattr__(self, "allowed_paths", normalized_allowed_paths)
        if type(self.max_artifacts) is not int or self.max_artifacts <= 0:
            raise Stage0ValidationError("max_artifacts must be positive")
        if type(self.max_artifact_bytes) is not int or self.max_artifact_bytes <= 0:
            raise Stage0ValidationError("max_artifact_bytes must be positive")
        if self.max_total_bytes is not None and (
            type(self.max_total_bytes) is not int or self.max_total_bytes <= 0
        ):
            raise Stage0ValidationError("max_total_bytes must be positive")


@dataclass(frozen=True)
class ValidatedArtifact:
    repository: str
    path: str
    size: int
    sha256: str


def _relative_parts(raw: str) -> tuple[str, ...]:
    if not raw or "\x00" in raw or "\\" in raw:
        raise Stage0ValidationError(f"artifact path is unsafe: {raw!r}")
    if raw.startswith("/") or raw.startswith("~"):
        raise Stage0ValidationError(f"artifact path must be relative: {raw!r}")
    raw_parts = raw.split("/")
    if any(part in ("", ".", "..") for part in raw_parts):
        raise Stage0ValidationError(
            f"artifact path contains traversal or empty segments: {raw!r}"
        )
    if any(":" in part or part.endswith((" ", ".")) for part in raw_parts):
        raise Stage0ValidationError(f"artifact path is platform-ambiguous: {raw!r}")
    parsed = PurePosixPath(raw)
    if parsed.is_absolute() or parsed.parts != tuple(raw_parts):
        raise Stage0ValidationError(f"artifact path is not canonical: {raw!r}")
    return tuple(raw_parts)


def _open_component(parent_fd: int, name: str, *, directory: bool) -> int:
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise Stage0ValidationError(
            f"artifact path component cannot be inspected: {name}"
        ) from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise Stage0ValidationError(f"artifact path contains a symbolic link: {name}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    if directory:
        flags |= getattr(os, "O_DIRECTORY", 0)
    try:
        return os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        label = "directory" if directory else "file"
        raise Stage0ValidationError(
            f"artifact {label} cannot be opened safely: {name}"
        ) from exc


def _hash_regular_file(
    root: Path, parts: tuple[str, ...], *, max_bytes: int
) -> tuple[int, str]:
    root = Path(root)
    if root.is_symlink():
        raise Stage0ValidationError(
            f"repository root must not be a symbolic link: {root}"
        )
    if not root.is_dir():
        raise Stage0ValidationError(f"repository root is not a directory: {root}")
    root_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptors: list[int] = []
    try:
        current = os.open(root, root_flags)
        descriptors.append(current)
        for part in parts[:-1]:
            current = _open_component(current, part, directory=True)
            descriptors.append(current)
        file_fd = _open_component(current, parts[-1], directory=False)
        descriptors.append(file_fd)
        metadata = os.fstat(file_fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise Stage0ValidationError("artifact must be a regular file")
        if metadata.st_size > max_bytes:
            raise Stage0ValidationError(
                f"artifact exceeds byte limit: {metadata.st_size} > {max_bytes}"
            )
        digest = hashlib.sha256()
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(file_fd, min(1024 * 1024, remaining))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
        if remaining == 0 and os.read(file_fd, 1):
            raise Stage0ValidationError(
                "artifact grew beyond the byte limit while hashing"
            )
        final_metadata = os.fstat(file_fd)
        if (
            final_metadata.st_dev != metadata.st_dev
            or final_metadata.st_ino != metadata.st_ino
            or final_metadata.st_size != metadata.st_size
            or final_metadata.st_mtime_ns != metadata.st_mtime_ns
        ):
            raise Stage0ValidationError("artifact changed while it was being hashed")
        return metadata.st_size, digest.hexdigest()
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def validate_artifacts(
    artifacts: Iterable[Mapping[str, object]],
    policy: ArtifactPolicy,
) -> tuple[ValidatedArtifact, ...]:
    records = list(artifacts)
    if len(records) > policy.max_artifacts:
        raise Stage0ValidationError(
            f"artifact count exceeds limit: {len(records)} > {policy.max_artifacts}"
        )
    seen: set[tuple[str, str]] = set()
    validated: list[ValidatedArtifact] = []
    total_bytes = 0
    for index, artifact in enumerate(records):
        if not isinstance(artifact, Mapping):
            raise Stage0ValidationError(f"artifact must be an object at index {index}")
        if set(artifact) != {"repository", "path", "sha256"}:
            raise Stage0ValidationError(f"artifact fields are invalid at index {index}")
        repository = artifact.get("repository")
        raw_path = artifact.get("path")
        expected_hash = artifact.get("sha256")
        if not isinstance(repository, str) or not _REPOSITORY_ID.fullmatch(repository):
            raise Stage0ValidationError(
                f"artifact repository is invalid at index {index}"
            )
        if not isinstance(raw_path, str):
            raise Stage0ValidationError(f"artifact path is invalid at index {index}")
        if not isinstance(expected_hash, str) or not _SHA256.fullmatch(expected_hash):
            raise Stage0ValidationError(f"artifact SHA-256 is invalid at index {index}")
        artifact_id = (repository, raw_path)
        if artifact_id in seen:
            raise Stage0ValidationError(f"duplicate artifact: {repository}/{raw_path}")
        seen.add(artifact_id)
        if artifact_id not in policy.allowed_paths:
            raise Stage0ValidationError(
                f"artifact is not allowlisted: {repository}/{raw_path}"
            )
        root = policy.repository_roots.get(repository)
        if root is None:
            raise Stage0ValidationError(
                f"artifact repository has no trusted root: {repository}"
            )
        parts = _relative_parts(raw_path)
        size, actual_hash = _hash_regular_file(
            Path(root),
            parts,
            max_bytes=policy.max_artifact_bytes,
        )
        if not hmac.compare_digest(actual_hash, expected_hash):
            raise Stage0ValidationError(
                f"artifact SHA-256 mismatch: {repository}/{raw_path}"
            )
        total_bytes += size
        if policy.max_total_bytes is not None and total_bytes > policy.max_total_bytes:
            raise Stage0ValidationError(
                f"total artifact bytes exceed limit: {total_bytes} > {policy.max_total_bytes}"
            )
        validated.append(
            ValidatedArtifact(
                repository=repository,
                path=raw_path,
                size=size,
                sha256=actual_hash,
            )
        )
    return tuple(validated)

"""Deterministic bundle manifest construction and verification."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any, Mapping

from .errors import Stage0ValidationError


MANIFEST_NAME = "bundle-manifest.json"
MAX_BUNDLE_FILES = 4096
MAX_BUNDLE_ENTRIES = 8192
MAX_BUNDLE_FILE_BYTES = 64 * 1024 * 1024
MAX_BUNDLE_TOTAL_BYTES = 512 * 1024 * 1024


def _canonical_bytes(value: object) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise Stage0ValidationError(
            "bundle manifest contains a non-JSON value"
        ) from exc
    return encoded.encode("utf-8")


def _open_component(parent_fd: int, name: str, *, directory: bool) -> int:
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise Stage0ValidationError(
            f"bundle path component cannot be inspected: {name}"
        ) from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise Stage0ValidationError(f"bundle contains a symbolic link: {name}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    if directory:
        flags |= getattr(os, "O_DIRECTORY", 0)
    try:
        return os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise Stage0ValidationError(
            f"bundle path component cannot be opened safely: {name}"
        ) from exc


def _file_record(root_fd: int, relative: str) -> dict[str, Any]:
    descriptors: list[int] = []
    current = root_fd
    try:
        parts = tuple(relative.split("/"))
        for part in parts[:-1]:
            current = _open_component(current, part, directory=True)
            descriptors.append(current)
        file_fd = _open_component(current, parts[-1], directory=False)
        descriptors.append(file_fd)
        metadata = os.fstat(file_fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise Stage0ValidationError(
                f"bundle contains a non-regular file: {relative}"
            )
        if metadata.st_size > MAX_BUNDLE_FILE_BYTES:
            raise Stage0ValidationError(
                f"bundle file exceeds byte limit: {relative} ({metadata.st_size})"
            )
        digest = hashlib.sha256()
        bytes_read = 0
        while True:
            chunk = os.read(file_fd, 1024 * 1024)
            if not chunk:
                break
            bytes_read += len(chunk)
            if bytes_read > MAX_BUNDLE_FILE_BYTES:
                raise Stage0ValidationError(
                    f"bundle file grew beyond its limit: {relative}"
                )
            digest.update(chunk)
        final = os.fstat(file_fd)
        if (
            final.st_dev != metadata.st_dev
            or final.st_ino != metadata.st_ino
            or final.st_size != metadata.st_size
            or final.st_mtime_ns != metadata.st_mtime_ns
            or bytes_read != final.st_size
        ):
            raise Stage0ValidationError(
                f"bundle file changed while hashing: {relative}"
            )
        return {
            "path": relative,
            "bytes": final.st_size,
            "sha256": digest.hexdigest(),
        }
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _bundle_root(root: Path) -> Path:
    root = Path(root)
    if root.is_symlink():
        raise Stage0ValidationError("bundle root must not be a symbolic link")
    if not root.is_dir():
        raise Stage0ValidationError(f"bundle root is not a directory: {root}")
    return root


def _bundle_files(root: Path) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    root_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    root_fd = os.open(root, root_flags)
    try:
        paths: list[Path] = []
        for path in root.rglob("*"):
            if len(paths) >= MAX_BUNDLE_ENTRIES:
                raise Stage0ValidationError(
                    f"bundle entry count exceeds limit: {MAX_BUNDLE_ENTRIES}"
                )
            paths.append(path)
        paths.sort(key=lambda candidate: candidate.relative_to(root).as_posix())
        total_bytes = 0
        for path in paths:
            relative = path.relative_to(root).as_posix()
            if relative == MANIFEST_NAME:
                raise Stage0ValidationError(
                    f"bundle contains reserved manifest path: {MANIFEST_NAME}"
                )
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise Stage0ValidationError(
                    f"bundle contains a symbolic link: {relative}"
                )
            if stat.S_ISDIR(mode):
                continue
            if not stat.S_ISREG(mode):
                raise Stage0ValidationError(
                    f"bundle contains a non-regular file: {relative}"
                )
            if len(files) >= MAX_BUNDLE_FILES:
                raise Stage0ValidationError(
                    f"bundle file count exceeds limit: {MAX_BUNDLE_FILES}"
                )
            record = _file_record(root_fd, relative)
            total_bytes += int(record["bytes"])
            if total_bytes > MAX_BUNDLE_TOTAL_BYTES:
                raise Stage0ValidationError(
                    f"bundle total bytes exceed limit: {MAX_BUNDLE_TOTAL_BYTES}"
                )
            files.append(record)
    finally:
        os.close(root_fd)
    return files


def _identity_copy(identity: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(identity, Mapping) or not identity:
        raise Stage0ValidationError("bundle identity must be a non-empty object")
    try:
        copied = json.loads(_canonical_bytes(dict(identity)))
    except (
        json.JSONDecodeError
    ) as exc:  # pragma: no cover - canonical bytes are produced locally.
        raise Stage0ValidationError("bundle identity is invalid") from exc
    if not isinstance(copied, dict):
        raise Stage0ValidationError("bundle identity must be an object")
    return copied


def build_bundle_manifest(
    root: Path,
    *,
    identity: Mapping[str, object],
) -> dict[str, object]:
    """Bind every regular payload file and the declared runtime identity."""
    root = _bundle_root(root)
    unsigned: dict[str, object] = {
        "schema_version": 1,
        "identity": _identity_copy(identity),
        "files": _bundle_files(root),
    }
    return {
        **unsigned,
        "bundle_manifest_sha256": hashlib.sha256(
            _canonical_bytes(unsigned)
        ).hexdigest(),
    }


def verify_bundle_manifest(
    root: Path,
    manifest: Mapping[str, object],
    *,
    expected_identity: Mapping[str, object],
) -> None:
    """Rebuild the manifest and reject any identity, file-set, size, or hash drift."""
    if not isinstance(manifest, Mapping):
        raise Stage0ValidationError("bundle manifest must be an object")
    supplied = dict(manifest)
    supplied_hash = supplied.pop("bundle_manifest_sha256", None)
    if not isinstance(supplied_hash, str) or len(supplied_hash) != 64:
        raise Stage0ValidationError("bundle manifest root hash is invalid")
    calculated_hash = hashlib.sha256(_canonical_bytes(supplied)).hexdigest()
    if calculated_hash != supplied_hash:
        raise Stage0ValidationError("bundle manifest root hash mismatch")
    if (
        type(supplied.get("schema_version")) is not int
        or supplied.get("schema_version") != 1
    ):
        raise Stage0ValidationError("bundle manifest schema version is unsupported")
    if supplied.get("identity") != _identity_copy(expected_identity):
        raise Stage0ValidationError("bundle identity mismatch")

    current = build_bundle_manifest(root, identity=expected_identity)
    if _canonical_bytes(current) != _canonical_bytes(dict(manifest)):
        raise Stage0ValidationError(
            "bundle file set or content does not match manifest"
        )

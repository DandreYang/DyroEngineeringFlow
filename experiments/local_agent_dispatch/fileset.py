"""Expand contract file globs and apply context guards."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Iterable, Mapping

from dyro.canonical import canonical_json_bytes

from .context_guard import (
    MAX_CONTEXT_FILE_BYTES,
    assert_files_allowed,
    guard_file,
    read_guarded_file,
)
from .errors import DispatchValidationError


MAX_CONTEXT_FILES = 200
MAX_TOTAL_CONTEXT_BYTES = 2 * 1024 * 1024
SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        "dist",
        "build",
        ".dyro",
    }
)


def _iter_project_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(root).parts
        if any(part in SKIP_DIRS for part in rel_parts):
            continue
        yield path


def _validate_pattern(pattern: str) -> None:
    normalized = pattern.lstrip("!")
    if (
        not normalized
        or "\x00" in normalized
        or "\\" in normalized
        or normalized.startswith(("/", "~"))
        or ".." in Path(normalized).parts
    ):
        raise DispatchValidationError(f"unsafe files glob: {pattern!r}")
    if normalized in {"*", "**", "**/*", "*/**", "**/**"}:
        raise DispatchValidationError(
            "files glob is unrestricted; provide a minimal sufficient set"
        )


def _glob_files(root: Path, pattern: str) -> set[Path]:
    _validate_pattern(pattern)
    try:
        candidates = root.glob(pattern)
    except (OSError, ValueError) as exc:
        raise DispatchValidationError(f"invalid files glob: {pattern!r}") from exc
    matched: set[Path] = set()
    for path in candidates:
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        if any(part in SKIP_DIRS for part in relative.parts):
            continue
        if path.is_file():
            matched.add(path)
    return matched


def expand_files(patterns: Iterable[str], project_root: Path) -> list[Path]:
    root = Path(project_root).resolve()
    if not root.is_dir():
        raise DispatchValidationError(f"project_root is not a directory: {root}")

    includes: list[str] = []
    excludes: list[str] = []
    for raw in patterns:
        pattern = raw.strip()
        if not pattern:
            continue
        _validate_pattern(pattern)
        if pattern.startswith("!"):
            excludes.append(pattern[1:])
        else:
            includes.append(pattern)
    if not includes:
        raise DispatchValidationError("files must include at least one positive glob")

    matched: set[Path] = set()
    for pattern in includes:
        matched.update(_glob_files(root, pattern))
    for pattern in excludes:
        matched.difference_update(_glob_files(root, pattern))

    if not matched:
        raise DispatchValidationError(
            "files globs matched zero files under project_root"
        )
    # Cap to keep context bounded.
    if len(matched) > MAX_CONTEXT_FILES:
        raise DispatchValidationError(
            f"files matched too many paths ({len(matched)} > {MAX_CONTEXT_FILES}); narrow globs"
        )
    return sorted(matched)


def collect_guarded_files(
    patterns: Iterable[str], project_root: Path
) -> list[Path]:
    paths = expand_files(patterns, project_root)
    assert_files_allowed(paths, Path(project_root).resolve())
    return paths


def collect_guarded_context(
    patterns: Iterable[str],
    project_root: Path,
    *,
    max_file_bytes: int = MAX_CONTEXT_FILE_BYTES,
    max_total_bytes: int = MAX_TOTAL_CONTEXT_BYTES,
) -> dict[str, str]:
    """Return one sealed, fully scanned snapshot for prompt and shadow materialization."""
    root = Path(project_root).resolve(strict=True)
    context: dict[str, str] = {}
    total_bytes = 0
    for path in expand_files(patterns, root):
        relative, content = read_guarded_file(
            path,
            root,
            max_bytes=max_file_bytes,
        )
        total_bytes += len(content.encode("utf-8"))
        if total_bytes > max_total_bytes:
            raise DispatchValidationError(
                f"context exceeds total byte limit: {total_bytes} > {max_total_bytes}"
            )
        context[relative] = content
    return context


def guarded_context_sha256(context: Mapping[str, str]) -> str:
    """Digest one sealed context snapshot without retaining its full contents."""
    entries = [
        {
            "path": path,
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        }
        for path, content in sorted(context.items())
    ]
    return hashlib.sha256(
        canonical_json_bytes({"files": entries})
    ).hexdigest()


def filter_readable(paths: Iterable[Path], project_root: Path) -> list[Path]:
    root = Path(project_root).resolve()
    allowed: list[Path] = []
    for path in paths:
        if guard_file(path, root).allowed:
            allowed.append(path)
    return allowed

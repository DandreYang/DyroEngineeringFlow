"""Expand contract file globs and apply context guards."""

from __future__ import annotations

from pathlib import Path
import fnmatch
from typing import Iterable

from .context_guard import assert_files_allowed, guard_file
from .errors import DispatchValidationError


def _iter_project_files(root: Path) -> Iterable[Path]:
    skip_dirs = {
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
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(root).parts
        if any(part in skip_dirs for part in rel_parts):
            continue
        yield path


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
        if pattern.startswith("!"):
            excludes.append(pattern[1:])
        else:
            includes.append(pattern)
    if not includes:
        raise DispatchValidationError("files must include at least one positive glob")

    matched: list[Path] = []
    for path in _iter_project_files(root):
        rel = path.relative_to(root).as_posix()
        if not any(fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(path.name, pat) for pat in includes):
            continue
        if any(fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(path.name, pat) for pat in excludes):
            continue
        matched.append(path)

    if not matched:
        raise DispatchValidationError(
            "files globs matched zero files under project_root"
        )
    # Cap to keep context bounded.
    if len(matched) > 200:
        raise DispatchValidationError(
            f"files matched too many paths ({len(matched)} > 200); narrow globs"
        )
    return sorted(matched)


def collect_guarded_files(
    patterns: Iterable[str], project_root: Path
) -> list[Path]:
    paths = expand_files(patterns, project_root)
    assert_files_allowed(paths, Path(project_root).resolve())
    return paths


def filter_readable(paths: Iterable[Path], project_root: Path) -> list[Path]:
    root = Path(project_root).resolve()
    allowed: list[Path] = []
    for path in paths:
        if guard_file(path, root).allowed:
            allowed.append(path)
    return allowed

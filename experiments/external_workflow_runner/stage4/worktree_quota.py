"""Bounded worktree storage quota for Stage 4 experiments."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from ..errors import Stage0ValidationError


@dataclass(frozen=True)
class WorktreeQuota:
    max_bytes_per_worktree: int = 8 * 1024 * 1024
    max_total_bytes: int = 16 * 1024 * 1024
    max_files_per_worktree: int = 256

    def __post_init__(self) -> None:
        for name in (
            "max_bytes_per_worktree",
            "max_total_bytes",
            "max_files_per_worktree",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise Stage0ValidationError(f"worktree quota invalid: {name}")

    def measure(self, worktrees: Mapping[str, Path]) -> dict[str, object]:
        per: dict[str, dict[str, int]] = {}
        total_bytes = 0
        total_files = 0
        for name, root in worktrees.items():
            root = Path(root)
            bytes_used = 0
            files = 0
            if root.is_dir():
                for path in root.rglob("*"):
                    if path.is_file():
                        files += 1
                        try:
                            bytes_used += path.stat().st_size
                        except OSError:
                            pass
            per[name] = {"bytes": bytes_used, "files": files}
            total_bytes += bytes_used
            total_files += files
        return {
            "per_worktree": per,
            "total_bytes": total_bytes,
            "total_files": total_files,
        }

    def assert_within(self, worktrees: Mapping[str, Path]) -> dict[str, object]:
        measured = self.measure(worktrees)
        for name, stats in measured["per_worktree"].items():  # type: ignore[union-attr]
            if stats["bytes"] > self.max_bytes_per_worktree:
                raise Stage0ValidationError(
                    f"worktree quota exceeded for {name}: "
                    f"{stats['bytes']} > {self.max_bytes_per_worktree} bytes"
                )
            if stats["files"] > self.max_files_per_worktree:
                raise Stage0ValidationError(
                    f"worktree file quota exceeded for {name}: "
                    f"{stats['files']} > {self.max_files_per_worktree}"
                )
        if measured["total_bytes"] > self.max_total_bytes:
            raise Stage0ValidationError(
                f"worktree total quota exceeded: "
                f"{measured['total_bytes']} > {self.max_total_bytes} bytes"
            )
        return measured

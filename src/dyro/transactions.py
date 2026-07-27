from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from .errors import ValidationError


class FileTransaction:
    """Stage a bounded set of files and replace them with rollback on failure."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.staging = Path(tempfile.mkdtemp(prefix=".dyro-transaction-", dir=self.root))
        self._entries: dict[Path, Path] = {}
        self._closed = False

    def stage_path(self, target: Path, data: bytes) -> None:
        if self._closed:
            raise RuntimeError("file transaction is already closed")
        resolved = target.resolve(strict=False)
        try:
            relative = resolved.relative_to(self.root)
        except ValueError as exc:
            raise ValidationError(f"事务目标路径位于任务目录外：{target}") from exc
        if relative in self._entries:
            raise ValidationError(f"事务重复写入同一目标：{relative}")
        staged = self.staging / "new" / relative
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(data)
        self._entries[relative] = staged

    def commit(self) -> None:
        if self._closed:
            raise RuntimeError("file transaction is already closed")
        replaced: list[tuple[Path, Path | None]] = []
        try:
            for relative, staged in sorted(self._entries.items(), key=lambda item: str(item[0])):
                target = self.root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                backup: Path | None = None
                if target.exists() or target.is_symlink():
                    backup = self.staging / "backup" / relative
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(target, backup)
                replaced.append((target, backup))
                os.replace(staged, target)
        except Exception:
            for target, backup in reversed(replaced):
                if target.exists() or target.is_symlink():
                    target.unlink()
                if backup is not None and backup.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(backup, target)
            raise
        finally:
            self._closed = True
            shutil.rmtree(self.staging, ignore_errors=True)

    def abort(self) -> None:
        if not self._closed:
            self._closed = True
            shutil.rmtree(self.staging, ignore_errors=True)

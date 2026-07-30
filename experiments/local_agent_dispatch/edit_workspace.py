"""Isolated Git worktree creation and patch-only sealing for edit dispatches."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import shutil

from .bounded_process import run_bounded
from .errors import DispatchValidationError
from .paths import edit_worktrees_dir, patches_dir


GIT_TIMEOUT_SECONDS = 30.0
MAX_PATCH_BYTES = 8 * 1024 * 1024


def _git(
    project_root: Path,
    arguments: list[str],
    *,
    timeout_seconds: float = GIT_TIMEOUT_SECONDS,
    max_output_bytes: int = MAX_PATCH_BYTES,
):
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "GIT_TERMINAL_PROMPT": "0",
    }
    completed = run_bounded(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-C",
            str(project_root),
            *arguments,
        ],
        cwd=project_root,
        timeout_seconds=timeout_seconds,
        env=environment,
        max_output_bytes=max_output_bytes,
    )
    if completed.timed_out:
        raise DispatchValidationError(f"git command timed out: {arguments[:2]}")
    if completed.output_limited:
        raise DispatchValidationError(f"git command output exceeded limit: {arguments[:2]}")
    if completed.returncode != 0:
        raise DispatchValidationError(
            f"git command failed ({arguments[:2]}): "
            f"{(completed.stderr or completed.stdout)[-1000:]}"
        )
    return completed


@dataclass
class EditWorkspace:
    project_root: Path
    worktree_root: Path
    patch_path: Path
    _created: bool = False

    @classmethod
    def create(
        cls,
        *,
        project_root: Path,
        home: Path | None,
        run_id: str,
    ) -> EditWorkspace:
        project_root = Path(project_root).resolve(strict=True)
        top_level = Path(
            _git(project_root, ["rev-parse", "--show-toplevel"]).stdout.strip()
        ).resolve(strict=True)
        if top_level != project_root:
            raise DispatchValidationError(
                "edit mode requires project_root to be the Git worktree root"
            )
        worktree_root = edit_worktrees_dir(home) / run_id
        patch_root = patches_dir(home) / run_id
        if worktree_root.exists() or patch_root.exists():
            raise DispatchValidationError("edit workspace already exists for run")
        patch_root.mkdir(parents=True, mode=0o700)
        patch_path = patch_root / "changes.patch"
        workspace = cls(
            project_root=project_root,
            worktree_root=worktree_root,
            patch_path=patch_path,
        )
        try:
            _git(
                project_root,
                ["worktree", "add", "--detach", str(worktree_root), "HEAD"],
            )
        except Exception:
            try:
                if worktree_root.exists():
                    _git(
                        project_root,
                        ["worktree", "remove", "--force", str(worktree_root)],
                    )
            except Exception:
                shutil.rmtree(worktree_root, ignore_errors=True)
            try:
                _git(project_root, ["worktree", "prune"])
            except Exception:
                pass
            shutil.rmtree(patch_root, ignore_errors=True)
            raise
        workspace._created = True
        return workspace

    def seal_patch(self) -> str | None:
        if not self._created:
            raise DispatchValidationError("edit workspace was not created")
        _git(self.worktree_root, ["add", "--intent-to-add", "--all"])
        completed = _git(
            self.worktree_root,
            ["diff", "--binary", "--no-ext-diff", "HEAD", "--"],
            max_output_bytes=MAX_PATCH_BYTES,
        )
        diff = completed.stdout_bytes
        if not diff:
            return None
        if len(diff) > MAX_PATCH_BYTES:
            raise DispatchValidationError("generated patch exceeds byte limit")
        self.patch_path.write_bytes(diff)
        os.chmod(self.patch_path, 0o600)
        digest = hashlib.sha256(diff).hexdigest()
        return f"{self.patch_path}#sha256={digest}"

    def cleanup(self) -> None:
        if not self._created:
            return
        errors: list[str] = []
        try:
            _git(
                self.project_root,
                ["worktree", "remove", "--force", str(self.worktree_root)],
            )
        except Exception as exc:
            errors.append(f"worktree remove: {exc}")
        try:
            _git(self.project_root, ["worktree", "prune"])
        except Exception as exc:
            errors.append(f"worktree prune: {exc}")
        if not self.worktree_root.exists():
            self._created = False
        if errors:
            raise DispatchValidationError("; ".join(errors))

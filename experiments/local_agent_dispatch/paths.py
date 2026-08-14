"""State roots for local agent dispatch (isolated from Dyro task state)."""

from __future__ import annotations

import os
from pathlib import Path

from .errors import DispatchValidationError


DEFAULT_HOME_ENV = "DYRO_LOCAL_AGENT_DISPATCH_HOME"
MANAGED_DIRECTORIES = (
    "runs",
    "locks",
    "shadow",
    "edit-worktrees",
    "skills",
    "panels",
    "orchestrations",
    "patches",
)


def dispatch_home_path(override: Path | None = None) -> Path:
    """Resolve the state root without creating it."""
    if override is not None:
        root = Path(override).expanduser().resolve()
    else:
        env = os.environ.get(DEFAULT_HOME_ENV)
        if env:
            root = Path(env).expanduser().resolve()
        else:
            root = Path.home() / ".dyro" / "local-agent-dispatch"
    return root


def dispatch_home(override: Path | None = None) -> Path:
    root = dispatch_home_path(override)
    root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir():
        raise DispatchValidationError(
            f"dispatch home is not a directory: {root}"
        )
    for name in MANAGED_DIRECTORIES:
        path = root / name
        if path.is_symlink():
            raise DispatchValidationError(
                f"dispatch managed directory is a symbolic link: {path}"
            )
        path.mkdir(parents=True, exist_ok=True)
        if (
            path.is_symlink()
            or not path.is_dir()
            or path.resolve(strict=True).parent != root
        ):
            raise DispatchValidationError(
                f"dispatch managed directory escapes state root: {path}"
            )
    return root


def existing_managed_dir(
    override: Path | None,
    name: str,
) -> Path:
    """Resolve an existing managed directory without creating or following links."""
    if name not in MANAGED_DIRECTORIES:
        raise DispatchValidationError(f"unknown dispatch managed directory: {name}")
    root = dispatch_home_path(override)
    path = root / name
    if path.is_symlink():
        raise DispatchValidationError(
            f"dispatch managed directory is a symbolic link: {path}"
        )
    if path.exists():
        if (
            not path.is_dir()
            or path.resolve(strict=True).parent != root
        ):
            raise DispatchValidationError(
                f"dispatch managed directory escapes state root: {path}"
            )
    return path


def runs_dir(home: Path | None = None) -> Path:
    return dispatch_home(home) / "runs"


def locks_dir(home: Path | None = None) -> Path:
    return dispatch_home(home) / "locks"


def shadow_dir(home: Path | None = None) -> Path:
    return dispatch_home(home) / "shadow"


def edit_worktrees_dir(home: Path | None = None) -> Path:
    return dispatch_home(home) / "edit-worktrees"


def skills_dir(home: Path | None = None) -> Path:
    return dispatch_home(home) / "skills"


def panels_dir(home: Path | None = None) -> Path:
    return dispatch_home(home) / "panels"


def orchestrations_dir(home: Path | None = None) -> Path:
    return dispatch_home(home) / "orchestrations"


def patches_dir(home: Path | None = None) -> Path:
    return dispatch_home(home) / "patches"

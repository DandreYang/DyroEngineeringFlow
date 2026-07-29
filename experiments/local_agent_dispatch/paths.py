"""State roots for local agent dispatch (isolated from Dyro task state)."""

from __future__ import annotations

import os
from pathlib import Path


DEFAULT_HOME_ENV = "DYRO_LOCAL_AGENT_DISPATCH_HOME"


def dispatch_home(override: Path | None = None) -> Path:
    if override is not None:
        root = Path(override).expanduser().resolve()
    else:
        env = os.environ.get(DEFAULT_HOME_ENV)
        if env:
            root = Path(env).expanduser().resolve()
        else:
            root = Path.home() / ".dyro" / "local-agent-dispatch"
    root.mkdir(parents=True, exist_ok=True)
    for name in ("runs", "locks", "shadow", "skills", "panels", "patches"):
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


def runs_dir(home: Path | None = None) -> Path:
    return dispatch_home(home) / "runs"


def locks_dir(home: Path | None = None) -> Path:
    return dispatch_home(home) / "locks"


def shadow_dir(home: Path | None = None) -> Path:
    return dispatch_home(home) / "shadow"


def skills_dir(home: Path | None = None) -> Path:
    return dispatch_home(home) / "skills"


def panels_dir(home: Path | None = None) -> Path:
    return dispatch_home(home) / "panels"


def patches_dir(home: Path | None = None) -> Path:
    return dispatch_home(home) / "patches"

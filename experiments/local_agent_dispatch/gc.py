"""Garbage-collect aged runs, shadows, and panels."""

from __future__ import annotations

from pathlib import Path
import shutil
import time

from .json_store import read_json
from .paths import dispatch_home, panels_dir, shadow_dir
from .run_store import RunStore


def gc(
    *,
    home: Path | None = None,
    max_age_seconds: float = 7 * 24 * 3600,
    dry_run: bool = False,
) -> dict[str, object]:
    root = dispatch_home(home)
    now = time.time()
    removed_runs: list[str] = []
    removed_shadows: list[str] = []
    removed_panels: list[str] = []

    store = RunStore(home)
    for record in store.list_runs():
        if now - record.updated_at < max_age_seconds:
            continue
        if record.status not in {"completed", "failed", "timeout", "cancelled"}:
            continue
        removed_runs.append(record.run_id)
        if not dry_run:
            store.delete(record.run_id)
            if record.shadow_path:
                shadow = Path(record.shadow_path)
                if shadow.exists() and str(shadow).startswith(str(shadow_dir(home))):
                    shutil.rmtree(shadow, ignore_errors=True)
                    removed_shadows.append(str(shadow))

    # Orphan shadows
    for path in shadow_dir(home).iterdir():
        if not path.is_dir():
            continue
        age = now - path.stat().st_mtime
        if age < max_age_seconds:
            continue
        removed_shadows.append(str(path))
        if not dry_run:
            shutil.rmtree(path, ignore_errors=True)

    for path in panels_dir(home).glob("panel-*.json"):
        payload = read_json(path) or {}
        created = float(payload.get("created_at") or path.stat().st_mtime)
        if now - created < max_age_seconds:
            continue
        removed_panels.append(path.name)
        if not dry_run:
            path.unlink(missing_ok=True)

    return {
        "home": str(root),
        "dry_run": dry_run,
        "max_age_seconds": max_age_seconds,
        "removed_runs": removed_runs,
        "removed_shadows": removed_shadows,
        "removed_panels": removed_panels,
    }

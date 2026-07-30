"""Garbage-collect aged runs, shadows, and panels."""

from __future__ import annotations

import math
from pathlib import Path
import shutil
import time

from .errors import DispatchValidationError
from .json_store import read_json
from .paths import dispatch_home, dispatch_home_path, existing_managed_dir
from .run_store import RunRecord, RunStore


def gc(
    *,
    home: Path | None = None,
    max_age_seconds: float = 7 * 24 * 3600,
    dry_run: bool = False,
) -> dict[str, object]:
    if (
        isinstance(max_age_seconds, bool)
        or not isinstance(max_age_seconds, (int, float))
        or not math.isfinite(float(max_age_seconds))
        or max_age_seconds < 0
    ):
        raise ValueError("max_age_seconds must be finite and non-negative")
    root = dispatch_home(home) if not dry_run else dispatch_home_path(home)
    now = time.time()
    removed_runs: list[str] = []
    removed_shadows: list[str] = []
    shadow_root = existing_managed_dir(home, "shadow")
    removed_panels: list[str] = []
    protected_shadows: set[Path] = set()

    store = RunStore(home, create=not dry_run)
    if not dry_run:
        store.reconcile_orphaned_workers()
    records = store.list_runs()
    record_states: list[tuple[RunRecord, bool]] = []
    for record in records:
        terminal = record.status in {
            "completed",
            "failed",
            "timeout",
            "cancelled",
        }
        aged = (
            math.isfinite(record.updated_at)
            and now - record.updated_at >= max_age_seconds
        )
        record_states.append((record, terminal and aged))
        if not terminal or not aged:
            protected_shadows.add((shadow_root / record.run_id).resolve())
            if record.shadow_path:
                try:
                    candidate = Path(record.shadow_path).resolve(strict=True)
                    candidate.relative_to(shadow_root)
                except (OSError, ValueError):
                    pass
                else:
                    if candidate != shadow_root:
                        protected_shadows.add(candidate)
    for record, removable in record_states:
        if not removable:
            continue
        removed_runs.append(record.run_id)
        deletable_shadow: Path | None = None
        if record.shadow_path:
            shadow = Path(record.shadow_path)
            expected_shadow = shadow_root / record.run_id
            try:
                resolved = shadow.resolve(strict=True)
                expected_resolved = expected_shadow.resolve(strict=True)
            except (OSError, ValueError):
                resolved = None
            if (
                resolved is not None
                and shadow == expected_shadow
                and resolved == expected_resolved
                and resolved != shadow_root
                and resolved not in protected_shadows
                and not expected_shadow.is_symlink()
                and not shadow.is_symlink()
                and resolved.is_dir()
            ):
                deletable_shadow = resolved
                removed_shadows.append(str(resolved))
        if not dry_run:
            store.delete(record.run_id)
            if deletable_shadow is not None:
                shutil.rmtree(deletable_shadow, ignore_errors=True)

    # Orphan shadows
    if shadow_root.is_dir():
        for path in shadow_root.iterdir():
            if path.is_symlink() or not path.is_dir():
                continue
            resolved_path = path.resolve()
            if resolved_path in protected_shadows:
                continue
            try:
                current_record = store.load(path.name)
            except DispatchValidationError:
                current_record = None
            if current_record is not None:
                current_terminal = current_record.status in {
                    "completed",
                    "failed",
                    "timeout",
                    "cancelled",
                }
                current_aged = (
                    math.isfinite(current_record.updated_at)
                    and now - current_record.updated_at >= max_age_seconds
                )
                if not current_terminal or not current_aged:
                    continue
            age = now - path.stat().st_mtime
            if age < max_age_seconds:
                continue
            if str(path) not in removed_shadows:
                removed_shadows.append(str(path))
            if not dry_run:
                shutil.rmtree(path, ignore_errors=True)

    panels_root = existing_managed_dir(home, "panels")
    for path in panels_root.glob("panel-*.json"):
        if path.is_symlink() or not path.is_file():
            continue
        payload = read_json(path) or {}
        try:
            created = float(
                payload.get("created_at") or path.stat().st_mtime
            )
        except (TypeError, ValueError, OSError):
            continue
        if not math.isfinite(created):
            continue
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

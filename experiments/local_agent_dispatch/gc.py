"""Garbage-collect aged runs, shadows, and panels."""

from __future__ import annotations

import math
from pathlib import Path
import shutil
import time

from .edit_workspace import EditWorkspace
from .errors import DispatchValidationError
from .file_lock import file_lock_is_held
from .json_store import read_json
from .orchestration_store import OrchestrationManifest, OrchestrationStore
from .paths import (
    dispatch_home,
    dispatch_home_path,
    existing_managed_dir,
)
from .run_store import RunRecord, RunStore


def _cleanup_edit_worktree(
    *,
    run_id: str,
    project_root: Path,
    edit_root: Path,
    patch_root: Path,
) -> bool:
    worktree = edit_root / run_id
    if not worktree.exists() and not worktree.is_symlink():
        return True
    if worktree.is_symlink() or not worktree.is_dir():
        return False
    try:
        if worktree.resolve(strict=True).parent != edit_root.resolve(strict=True):
            return False
        workspace = EditWorkspace(
            project_root=Path(project_root).resolve(strict=True),
            worktree_root=worktree,
            patch_path=patch_root / run_id / "changes.patch",
            _created=True,
        )
        workspace.cleanup()
    except (OSError, DispatchValidationError):
        return False
    return not worktree.exists()


def _remove_file(path: Path) -> bool:
    if not path.exists() and not path.is_symlink():
        return True
    if not path.is_symlink() and not path.is_file():
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


def _remove_tree(path: Path, *, parent: Path) -> bool:
    if not path.exists() and not path.is_symlink():
        return True
    if path.is_symlink():
        return _remove_file(path)
    if not path.is_dir():
        return False
    try:
        if path.resolve(strict=True).parent != parent.resolve(strict=True):
            return False
        shutil.rmtree(path)
    except OSError:
        return False
    return not path.exists()


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
    removed_orchestrations: list[str] = []
    protected_shadows: set[Path] = set()

    store = RunStore(home, create=not dry_run)
    if not dry_run:
        store.reconcile_orphaned_workers()
    records = store.list_runs()
    records_by_id = {record.run_id: record for record in records}
    protected_runs: set[str] = set()
    orchestration_root = existing_managed_dir(home, "orchestrations")
    orchestration_store = OrchestrationStore(home, create=not dry_run)
    orchestration_candidates: dict[Path, OrchestrationManifest] = {}
    invalid_orchestrations: set[str] = set()
    referenced_run_ids: set[str] = set()
    for path in sorted(orchestration_root.glob("orch-*.json")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            manifest = orchestration_store.load(path.stem)
        except DispatchValidationError:
            # Corrupt state cannot prove which deterministic member runs are
            # safe to discard. Run records referring to it remain protected.
            invalid_orchestrations.add(path.stem)
            continue
        referenced_run_ids.update(member.run_id for member in manifest.members)
        member_records = [
            records_by_id.get(member.run_id) for member in manifest.members
        ]
        manifest_aged = (
            math.isfinite(manifest.updated_at)
            and now - manifest.updated_at >= max_age_seconds
        )
        members_removable = all(
            record is None
            or (
                record.status in {
                    "completed",
                    "failed",
                    "timeout",
                    "cancelled",
                }
                and math.isfinite(record.updated_at)
                and now - record.updated_at >= max_age_seconds
            )
            for record in member_records
        )
        if manifest_aged and members_removable:
            orchestration_candidates[path] = manifest
        else:
            protected_runs.update(member.run_id for member in manifest.members)
    for record in records:
        if (
            record.orchestration_id in invalid_orchestrations
        ):
            protected_runs.add(record.run_id)

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
        removable = terminal and aged and record.run_id not in protected_runs
        record_states.append((record, removable))
        if not removable:
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

    edit_root = existing_managed_dir(home, "edit-worktrees")
    patch_root = existing_managed_dir(home, "patches")
    selected_orchestrations: dict[Path, OrchestrationManifest] = {}
    blocked_run_ids: set[str] = set()
    for path, manifest in orchestration_candidates.items():
        cleanup_proven = True
        if not dry_run:
            for member, planned in zip(
                manifest.members,
                manifest.plan.members,
                strict=True,
            ):
                if planned.normalized_contract.get("mode") != "edit":
                    continue
                if not _cleanup_edit_worktree(
                    run_id=member.run_id,
                    project_root=Path(manifest.plan.project_root),
                    edit_root=edit_root,
                    patch_root=patch_root,
                ):
                    cleanup_proven = False
                    break
        if cleanup_proven:
            selected_orchestrations[path] = manifest
        else:
            blocked_run_ids.update(member.run_id for member in manifest.members)

    revised_states: list[tuple[RunRecord, bool]] = []
    for record, removable in record_states:
        if removable and record.run_id not in blocked_run_ids and not dry_run:
            try:
                contract_mode = str(record.contract.get("mode") or "")
            except AttributeError:
                contract_mode = ""
            if contract_mode == "edit" and not _cleanup_edit_worktree(
                run_id=record.run_id,
                project_root=Path(record.project_root),
                edit_root=edit_root,
                patch_root=patch_root,
            ):
                removable = False
        revised_states.append((record, removable and record.run_id not in blocked_run_ids))
    record_states = revised_states

    removable_run_ids = {
        record.run_id for record, removable in record_states if removable
    }
    for path, manifest in list(selected_orchestrations.items()):
        if any(
            member.run_id in records_by_id
            and member.run_id not in removable_run_ids
            for member in manifest.members
        ):
            selected_orchestrations.pop(path)
            blocked_run_ids.update(member.run_id for member in manifest.members)
    if blocked_run_ids:
        record_states = [
            (record, removable and record.run_id not in blocked_run_ids)
            for record, removable in record_states
        ]

    # Remove the manifest before its member run records. If the process stops
    # here, terminal aged runs remain self-identifying orphans and the next GC
    # can continue instead of waiting for a now-incomplete manifest forever.
    deleted_orchestrations: dict[Path, OrchestrationManifest] = {}
    for path, manifest in selected_orchestrations.items():
        if dry_run:
            removed_orchestrations.append(path.name)
            deleted_orchestrations[path] = manifest
            continue
        with orchestration_store.mutation_locks(
            orchestration_id=manifest.orchestration_id,
            request_id=manifest.request_id,
        ):
            try:
                fresh = orchestration_store.load(manifest.orchestration_id)
            except DispatchValidationError:
                blocked_run_ids.update(
                    member.run_id for member in manifest.members
                )
                continue
            fresh_records: list[RunRecord | None] = []
            for member in fresh.members:
                try:
                    fresh_records.append(store.load(member.run_id))
                except DispatchValidationError:
                    fresh_records.append(None)
            still_removable = (
                fresh.request_id == manifest.request_id
                and fresh.plan_sha256 == manifest.plan_sha256
                and math.isfinite(fresh.updated_at)
                and now - fresh.updated_at >= max_age_seconds
                and all(
                    record is None
                    or (
                        record.status
                        in {"completed", "failed", "timeout", "cancelled"}
                        and math.isfinite(record.updated_at)
                        and now - record.updated_at >= max_age_seconds
                    )
                    for record in fresh_records
                )
            )
            if not still_removable:
                blocked_run_ids.update(
                    member.run_id for member in manifest.members
                )
                continue
            try:
                # Heal manifests written by older versions or interrupted
                # between the manifest and tombstone durability points. Never
                # delete the only idempotency record unless this succeeds.
                orchestration_store.bind_manifest_tombstone(fresh)
            except (OSError, DispatchValidationError):
                blocked_run_ids.update(
                    member.run_id for member in manifest.members
                )
                continue
            path.unlink()
            removed_orchestrations.append(path.name)
            deleted_orchestrations[path] = fresh
    selected_orchestrations = deleted_orchestrations
    if blocked_run_ids:
        record_states = [
            (record, removable and record.run_id not in blocked_run_ids)
            for record, removable in record_states
        ]

    for record, removable in record_states:
        if not removable:
            continue
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
        if not dry_run:
            if deletable_shadow is not None and not _remove_tree(
                deletable_shadow,
                parent=shadow_root,
            ):
                continue
            if not _remove_tree(
                patch_root / record.run_id,
                parent=patch_root,
            ):
                continue
            if not _remove_tree(
                store.root / f".{record.run_id}.{record.backend}.home",
                parent=store.root,
            ):
                continue
            for artifact in (
                store.root / f"{record.run_id}.worker.log",
                store.root / f"{record.run_id}.backend.lifetime",
            ):
                if not _remove_file(artifact):
                    break
            else:
                store.delete(record.run_id)
                lock_path = store.root / f".{record.run_id}.lock"
                if file_lock_is_held(lock_path) is False:
                    _remove_file(lock_path)
                removed_runs.append(record.run_id)
                if deletable_shadow is not None:
                    removed_shadows.append(str(deletable_shadow))
                continue
            continue
        removed_runs.append(record.run_id)
        if deletable_shadow is not None:
            removed_shadows.append(str(deletable_shadow))

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
                state_path = store.root / f"{path.name}.json"
                if state_path.exists() or state_path.is_symlink():
                    # Damaged state cannot prove the backend is no longer using
                    # this directory as its cwd.
                    continue
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
                if not _remove_tree(path, parent=shadow_root):
                    removed_shadows.remove(str(path))

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

    surviving_references = referenced_run_ids - {
        member.run_id
        for manifest in selected_orchestrations.values()
        for member in manifest.members
    }
    if patch_root.is_dir():
        for path in patch_root.glob("run-*"):
            run_id = path.name
            state_path = store.root / f"{run_id}.json"
            if (
                run_id in surviving_references
                or state_path.exists()
                or state_path.is_symlink()
            ):
                continue
            try:
                aged = now - path.lstat().st_mtime >= max_age_seconds
            except OSError:
                continue
            if aged and not dry_run:
                _remove_tree(path, parent=patch_root)

    for pattern, suffix in (
        ("run-*.worker.log", ".worker.log"),
        ("run-*.backend.lifetime", ".backend.lifetime"),
        (".run-*.lock", ".lock"),
    ):
        for path in store.root.glob(pattern):
            name = path.name[1:] if path.name.startswith(".") else path.name
            run_id = name[: -len(suffix)]
            state_path = store.root / f"{run_id}.json"
            if (
                run_id in surviving_references
                or state_path.exists()
                or state_path.is_symlink()
            ):
                continue
            try:
                aged = now - path.lstat().st_mtime >= max_age_seconds
            except OSError:
                continue
            if not aged or dry_run:
                continue
            if suffix == ".lock" and file_lock_is_held(path) is not False:
                continue
            _remove_file(path)

    for path in store.root.glob(".run-*.home"):
        identity = path.name[1:-len(".home")]
        run_id, separator, _backend = identity.rpartition(".")
        if not separator:
            continue
        state_path = store.root / f"{run_id}.json"
        if (
            run_id in surviving_references
            or state_path.exists()
            or state_path.is_symlink()
        ):
            continue
        try:
            aged = now - path.lstat().st_mtime >= max_age_seconds
        except OSError:
            continue
        if aged and not dry_run:
            _remove_tree(path, parent=store.root)

    return {
        "home": str(root),
        "dry_run": dry_run,
        "max_age_seconds": max_age_seconds,
        "removed_runs": removed_runs,
        "removed_shadows": removed_shadows,
        "removed_panels": removed_panels,
        "removed_orchestrations": removed_orchestrations,
    }

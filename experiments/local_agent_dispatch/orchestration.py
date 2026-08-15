"""Persistent, independently recoverable Batch V1 orchestration."""

from __future__ import annotations

import math
from pathlib import Path
import time
from typing import Any, Mapping

from .adapters.registry import (
    adapter_execution_profile,
    adapter_is_authenticated,
    execution_profile_sha256,
    get_adapter,
)
from .batch_contract import (
    BatchMemberPlan,
    BatchPlan,
    effects_for_members,
    parse_batch_request,
)
from .context_guard import safe_error_text
from .edit_workspace import review_edit_snapshot
from .errors import DispatchValidationError
from .fileset import collect_guarded_context, guarded_context_sha256
from .orchestration_store import OrchestrationManifest, OrchestrationStore
from .panel import candidate_provider_ids
from .run_store import RunRecord, RunStore, TERMINAL_RUN_STATUSES
from .supervisor import DispatchSupervisor
from .task_contract import TaskContract, parse_task_contract


MAX_PER_BACKEND = 2
MAX_RESULT_WAIT_SECONDS = 3600.0
_RESULT_FIELDS = (
    "summary",
    "confidence",
    "verified_ratio",
    "evidence",
    "warnings",
    "patch_ref",
    "error_code",
)


def _select_backend(
    requested: str,
    *,
    ready: tuple[str, ...],
    counts: dict[str, int],
    mode: str,
    strict: bool,
) -> str:
    candidates = ready if requested == "auto" else (requested,)
    if requested != "auto" and requested not in ready:
        raise DispatchValidationError(
            f"batch backend is not installed: {requested}"
        )
    for backend in sorted(
        candidates,
        key=lambda item: (counts.get(item, 0), ready.index(item)),
    ):
        if counts.get(backend, 0) >= MAX_PER_BACKEND:
            continue
        adapter = get_adapter(backend)
        if strict and not getattr(adapter, "strict_isolation", False):
            if requested != "auto":
                raise DispatchValidationError(
                    f"backend does not provide strict isolation: {backend}"
                )
            continue
        supported_modes = getattr(
            adapter,
            "supported_modes",
            frozenset({"read-only", "edit"}),
        )
        if mode not in supported_modes:
            if requested != "auto":
                raise DispatchValidationError(
                    f"batch backend does not support mode={mode}: {backend}"
                )
            continue
        counts[backend] = counts.get(backend, 0) + 1
        return backend
    if requested == "auto":
        raise DispatchValidationError(
            "no installed provider has capacity and supports this batch member"
        )
    raise DispatchValidationError(
        f"batch backend exceeds the {MAX_PER_BACKEND}-member limit: {requested}"
    )


def _normalized_contract(
    contract: TaskContract,
    *,
    backend: str,
) -> TaskContract:
    payload = contract.to_mapping()
    payload["backend"] = backend
    return parse_task_contract(payload)


def plan_batch(
    payload: Mapping[str, Any],
    *,
    project_root: Path,
    home: Path | None = None,
) -> BatchPlan:
    """Build a side-effect-free plan bound to Providers, context, and edit HEAD."""
    del home  # Planning never resolves or creates dispatch state.
    request = parse_batch_request(payload)
    try:
        root = Path(project_root).expanduser().resolve(strict=True)
    except OSError as exc:
        raise DispatchValidationError(
            f"batch project root does not exist: {project_root}"
        ) from exc
    if not root.is_dir():
        raise DispatchValidationError(
            f"batch project root is not a directory: {root}"
        )

    ready = tuple(candidate_provider_ids())
    if not ready:
        raise DispatchValidationError(
            "no installed integrated provider is available for batch dispatch"
        )
    counts: dict[str, int] = {}
    planned: list[BatchMemberPlan] = []
    for member in request.members:
        contract = member.contract
        backend = _select_backend(
            contract.backend,
            ready=ready,
            counts=counts,
            mode=contract.mode,
            strict=contract.strict,
        )
        adapter = get_adapter(backend)
        if (
            not contract.strict
            and not getattr(adapter, "strict_isolation", False)
            and not contract.allow_unconfined_provider
        ):
            raise DispatchValidationError(
                "real provider access requires allow_unconfined_provider=true: "
                f"{member.role_id}"
            )
        context = collect_guarded_context(contract.files, root)
        base_head = (
            review_edit_snapshot(root, tuple(context))
            if contract.mode == "edit"
            else None
        )
        normalized = _normalized_contract(contract, backend=backend)
        planned.append(
            BatchMemberPlan(
                role_id=member.role_id,
                resolved_backend=backend,
                context_file_count=len(context),
                context_sha256=guarded_context_sha256(context),
                base_head=base_head,
                execution_profile=adapter_execution_profile(adapter),
                timeout_seconds=member.timeout_seconds,
                normalized_contract=normalized.to_mapping(),
            )
        )
    return BatchPlan(
        project_root=root,
        request_id=request.request_id,
        strategy=request.strategy,
        effects=effects_for_members(planned),
        members=tuple(planned),
    )


def start_batch(
    payload: Mapping[str, Any],
    *,
    expected_plan_sha256: str,
    project_root: Path,
    home: Path | None = None,
) -> dict[str, object]:
    """Persist and asynchronously start an already reviewed Batch V1 plan."""
    plan = plan_batch(payload, project_root=project_root, home=home)
    if plan.plan_sha256 != expected_plan_sha256:
        raise DispatchValidationError(
            "batch plan digest changed; run batch-plan again before starting"
        )

    # Active authentication probes are deferred until the explicit start
    # boundary and complete before any manifest or run state is written.
    for backend in dict.fromkeys(
        member.resolved_backend for member in plan.members
    ):
        adapter = get_adapter(backend)
        if not adapter.available() or not adapter_is_authenticated(adapter):
            raise DispatchValidationError(
                f"batch backend is not ready and authenticated: {backend}"
            )
    for member in plan.members:
        adapter = get_adapter(member.resolved_backend)
        if adapter_execution_profile(adapter) != dict(member.execution_profile):
            raise DispatchValidationError(
                "batch backend execution profile changed; run batch-plan again: "
                f"{member.resolved_backend}"
            )

    manifest_store = OrchestrationStore(home)
    run_store = RunStore(home)
    records: list[RunRecord] = []
    def initialize_members(manifest: OrchestrationManifest) -> None:
        for member, planned in zip(
            manifest.members,
            plan.members,
            strict=True,
        ):
            records.append(
                run_store.ensure_created(
                    run_id=member.run_id,
                    contract=parse_task_contract(planned.normalized_contract),
                    project_root=Path(plan.project_root),
                    backend=member.backend,
                    orchestration_id=manifest.orchestration_id,
                    thread_id=member.role_id,
                    planned_context_sha256=planned.context_sha256,
                    planned_base_head=planned.base_head or "",
                    planned_execution_profile_sha256=(
                        execution_profile_sha256(planned.execution_profile)
                    ),
                    planned_execution_profile=planned.execution_profile,
                )
            )

    manifest = manifest_store.create_or_load_initialized(
        plan,
        initialize_members,
    )

    supervisor = DispatchSupervisor(home=home)
    for member, record in zip(manifest.members, records, strict=True):
        current_manifest = manifest_store.load(manifest.orchestration_id)
        if current_manifest.cancel_requested:
            supervisor.cancel(
                member.run_id,
                reason=f"batch cancelled: {manifest.orchestration_id}",
            )
            continue
        current = run_store.load(record.run_id)
        if current.status != "accepted" or current.worker_token:
            continue
        try:
            supervisor.execute(
                current.run_id,
                timeout_seconds=member.timeout_seconds,
                sync=False,
            )
        except Exception as exc:  # noqa: BLE001 - preserve independent members
            run_store.fail_if_accepted(
                current.run_id,
                error="batch worker start failed: " + safe_error_text(exc),
            )
    return get_batch_status(
        manifest.orchestration_id,
        home=home,
        reconcile=False,
    )


def _status_projection(
    manifest: OrchestrationManifest,
    records: list[RunRecord | None],
    *,
    reconciled_runs: list[str],
) -> dict[str, object]:
    statuses = [record.status if record is not None else "unknown" for record in records]
    counts: dict[str, int] = {}
    for status in statuses:
        counts[status] = counts.get(status, 0) + 1
    if "unknown" in counts:
        overall = "attention_required"
    elif any(status in {"accepted", "running"} for status in statuses):
        overall = "cancelling" if manifest.cancel_requested else "running"
    elif statuses and all(status == "completed" for status in statuses):
        overall = "completed"
    elif statuses and all(status == "cancelled" for status in statuses):
        overall = "cancelled"
    elif any(status == "completed" for status in statuses):
        overall = "partial"
    elif statuses and all(status in TERMINAL_RUN_STATUSES for status in statuses):
        overall = "failed"
    else:
        overall = "attention_required"
    members = [
        {
            "role_id": member.role_id,
            "backend": member.backend,
            "run_id": member.run_id,
            "status": statuses[index],
        }
        for index, member in enumerate(manifest.members)
    ]
    return {
        "schema_version": 1,
        "kind": "local-agent-dispatch-batch-status",
        "orchestration_id": manifest.orchestration_id,
        "request_id": manifest.request_id,
        "plan_sha256": manifest.plan_sha256,
        "status": overall,
        "cancel_requested": manifest.cancel_requested,
        "counts": counts,
        "members": members,
        "reconciled_runs": reconciled_runs,
    }


def _load_batch_records(
    manifest: OrchestrationManifest,
    *,
    home: Path | None,
    reconcile: bool,
) -> tuple[list[RunRecord | None], list[str]]:
    store = RunStore(home, create=reconcile)
    reconciled = (
        store.reconcile_orphaned_workers(
            run_ids={member.run_id for member in manifest.members}
        )
        if reconcile
        else []
    )
    records: list[RunRecord | None] = []
    for index, member in enumerate(manifest.members):
        planned = manifest.plan.members[index]
        try:
            record = store.load(member.run_id)
            if (
                record.orchestration_id != manifest.orchestration_id
                or record.backend != member.backend
                or record.thread_id != member.role_id
                or record.project_root != str(manifest.plan.project_root)
                or record.contract != planned.normalized_contract
                or record.planned_context_sha256 != planned.context_sha256
                or record.planned_base_head != (planned.base_head or "")
                or record.planned_execution_profile_sha256
                != execution_profile_sha256(planned.execution_profile)
                or dict(record.planned_execution_profile or {})
                != dict(planned.execution_profile)
            ):
                raise DispatchValidationError(
                    f"batch member run identity mismatch: {member.run_id}"
                )
        except DispatchValidationError:
            record = None
        records.append(record)
    return records, reconciled


def get_batch_status(
    orchestration_id: str,
    *,
    home: Path | None = None,
    reconcile: bool = True,
) -> dict[str, object]:
    manifest = OrchestrationStore(home, create=reconcile).load(orchestration_id)
    records, reconciled = _load_batch_records(
        manifest,
        home=home,
        reconcile=reconcile,
    )
    return _status_projection(
        manifest,
        records,
        reconciled_runs=reconciled,
    )


def _validate_wait_timeout(timeout_seconds: float) -> float:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(float(timeout_seconds))
        or timeout_seconds <= 0
        or timeout_seconds > MAX_RESULT_WAIT_SECONDS
    ):
        raise DispatchValidationError(
            f"batch result timeout must be positive and at most {MAX_RESULT_WAIT_SECONDS:g}"
        )
    return float(timeout_seconds)


def get_batch_result(
    orchestration_id: str,
    *,
    home: Path | None = None,
    wait: bool = False,
    timeout_seconds: float = 300.0,
    reconcile: bool = True,
) -> dict[str, object]:
    timeout = _validate_wait_timeout(timeout_seconds)
    deadline = time.monotonic() + timeout
    while True:
        manifest = OrchestrationStore(home, create=reconcile).load(orchestration_id)
        records, reconciled = _load_batch_records(
            manifest,
            home=home,
            reconcile=reconcile,
        )
        status = _status_projection(
            manifest,
            records,
            reconciled_runs=reconciled,
        )
        ready = bool(records) and all(
            record is not None and record.status in TERMINAL_RUN_STATUSES
            for record in records
        )
        if ready or not wait or time.monotonic() >= deadline:
            break
        time.sleep(0.2)

    result_members: list[dict[str, object]] = []
    for member, record in zip(manifest.members, records, strict=True):
        item: dict[str, object] = {
            "role_id": member.role_id,
            "backend": member.backend,
            "run_id": member.run_id,
            "status": record.status if record is not None else "unknown",
        }
        if ready and record is not None:
            persisted = record.result or {}
            for field in _RESULT_FIELDS:
                if field in persisted:
                    item[field] = persisted[field]
            if record.error:
                item["error"] = safe_error_text(record.error)
        result_members.append(item)
    return {
        "schema_version": 1,
        "kind": "local-agent-dispatch-batch-result",
        "orchestration_id": manifest.orchestration_id,
        "status": status["status"],
        "ready": ready,
        "members": result_members,
    }


def cancel_batch(
    orchestration_id: str,
    *,
    home: Path | None = None,
) -> dict[str, object]:
    manifest_store = OrchestrationStore(home)
    manifest = manifest_store.request_cancel(orchestration_id)
    supervisor = DispatchSupervisor(home=home)
    for member in manifest.members:
        try:
            supervisor.cancel(
                member.run_id,
                reason=f"batch cancelled: {manifest.orchestration_id}",
            )
        except DispatchValidationError:
            # Missing or corrupt member state remains visible as attention_required.
            continue
    return get_batch_status(
        manifest.orchestration_id,
        home=home,
        reconcile=False,
    )

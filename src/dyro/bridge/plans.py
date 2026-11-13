"""Deterministic, non-executable Bridge plans. No apply, lease, or reservation."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from typing import Callable

from ..canonical import canonical_json_bytes
from ..config import Config
from ..errors import DyroError, ValidationError
from ..continuation.attention import (
    attention_projection_payload,
    build_attention_projection,
)
from ..continuation.engine import build_scheduler_tick, scheduler_tick_payload
from ..continuation.planner import (
    build_continuation_plan,
    build_scheduler_projection,
    continuation_plan_payload,
    projection_payload,
)
from ..continuation.snapshot import build_scheduler_snapshot
from ..continuation.store import get_objective
from .constants import PLANNER_REVISIONS
from .identity import config_revision_v1, workspace_identity_v1
from .observations import BridgeObservationError


def _clock(clock: Callable[[], datetime] | None) -> Callable[[], datetime]:
    return clock if clock is not None else (lambda: datetime.now(timezone.utc))


def _identity(config: Config, profile_bytes: bytes) -> tuple[str, str]:
    return (
        workspace_identity_v1(canonical_root=config.root, profile_name=config.name),
        config_revision_v1(profile_bytes),
    )


def _envelope(
    *,
    operation: str,
    workspace_id: str,
    config_revision: str,
    normalized_input: dict[str, object],
    read_set: dict[str, object],
    projection: dict[str, object],
) -> dict[str, object]:
    payload: dict[str, object] = {
        "authorization": "none",
        "effects": [],
        "effective_risk": "PLAN",
        "executable": False,
        "maximum_risk": "PLAN",
        "normalized_input": normalized_input,
        "operation": operation,
        "operation_schema_version": 1,
        "planner_revision": PLANNER_REVISIONS[operation],
        "projection": projection,
        "protocol_major": 1,
        "read_set": read_set,
        "warnings": [],
        "workspace": {
            "config_sha256": f"sha256:{config_revision}",
            "id": workspace_id,
        },
    }
    digest = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    payload["plan_sha256"] = f"sha256:{digest}"
    return payload


def _snapshot(config: Config, objective_id: str, clock: Callable[[], datetime]):
    try:
        record = get_objective(config, objective_id, recover=False)
    except (DyroError, ValidationError, OSError) as exc:
        raise BridgeObservationError("OBJECTIVE_NOT_FOUND") from exc
    snapshot = build_scheduler_snapshot(
        config,
        objective=record,
        clock=clock,
        inspect_integration=False,
        inspect_proofs=False,
    )
    return record, snapshot, build_continuation_plan(snapshot)


def _read_set(record, snapshot) -> dict[str, object]:
    return {
        "execution_mode": snapshot.execution_mode,
        "integration_inspection": "not_inspected",
        "objective": {
            "contract_sha256": record.contract_sha256,
            "event_sha256": record.event_sha256,
            "id": record.objective.id,
            "operator_state": record.operator_state,
            "requested_mode": record.objective.requested_mode.value,
            "revision": record.revision,
            "scope_sha256": record.scope_sha256,
        },
        "observed_at": snapshot.observed_at.isoformat(),
        "snapshot_sha256": snapshot.snapshot_sha256,
    }


def objective_plan(
    config: Config,
    objective_id: str,
    *,
    profile_bytes: bytes,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, object]:
    workspace_id, revision = _identity(config, profile_bytes)
    record, snapshot, plan = _snapshot(config, objective_id, _clock(clock))
    return _envelope(
        operation="objective.plan",
        workspace_id=workspace_id,
        config_revision=revision,
        normalized_input={"objective_id": objective_id},
        read_set=_read_set(record, snapshot),
        projection=continuation_plan_payload(plan),
    )


def objective_explain(
    config: Config,
    objective_id: str,
    *,
    profile_bytes: bytes,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, object]:
    workspace_id, revision = _identity(config, profile_bytes)
    record, snapshot, plan = _snapshot(config, objective_id, _clock(clock))
    continuation = continuation_plan_payload(plan)
    return _envelope(
        operation="objective.explain",
        workspace_id=workspace_id,
        config_revision=revision,
        normalized_input={"objective_id": objective_id},
        read_set=_read_set(record, snapshot),
        projection={
            "completion": plan.completion.value,
            "selected_actions": continuation["selected_actions"],
            "blocked": continuation["blocked"],
            "attention": continuation["attention"],
        },
    )


def objective_graph(
    config: Config,
    objective_id: str,
    *,
    profile_bytes: bytes,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, object]:
    workspace_id, revision = _identity(config, profile_bytes)
    record, snapshot, plan = _snapshot(config, objective_id, _clock(clock))
    projection = build_scheduler_projection(snapshot, plan)
    return _envelope(
        operation="objective.graph",
        workspace_id=workspace_id,
        config_revision=revision,
        normalized_input={"objective_id": objective_id},
        read_set=_read_set(record, snapshot),
        projection=projection_payload(projection),
    )


def objective_tick(
    config: Config,
    objective_id: str,
    *,
    profile_bytes: bytes,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, object]:
    workspace_id, revision = _identity(config, profile_bytes)
    record, snapshot, plan = _snapshot(config, objective_id, _clock(clock))
    tick = build_scheduler_tick(
        snapshot, plan, max_parallel=record.objective.budget.max_parallel
    )
    return _envelope(
        operation="objective.tick",
        workspace_id=workspace_id,
        config_revision=revision,
        normalized_input={"objective_id": objective_id},
        read_set=_read_set(record, snapshot),
        projection=scheduler_tick_payload(tick),
    )


def objective_attention(
    config: Config,
    objective_id: str,
    *,
    profile_bytes: bytes,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, object]:
    workspace_id, revision = _identity(config, profile_bytes)
    record, snapshot, plan = _snapshot(config, objective_id, _clock(clock))
    scheduler = build_scheduler_projection(snapshot, plan)
    attention = build_attention_projection(
        snapshot, plan, scheduler, budget=record.objective.budget
    )
    return _envelope(
        operation="objective.attention",
        workspace_id=workspace_id,
        config_revision=revision,
        normalized_input={"objective_id": objective_id},
        read_set=_read_set(record, snapshot),
        projection=attention_projection_payload(attention),
    )

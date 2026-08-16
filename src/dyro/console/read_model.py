"""Explicit, path-free Console projection of Core workspace observations."""

from __future__ import annotations

import hashlib

from ..canonical import canonical_json_bytes
from ..observations import WorkspaceReadSnapshot
from .models import ConsoleEnvelope
from .redaction import REDACTED, safe_branch, safe_id, safe_sha256, safe_title


def _action_payload(action: object) -> dict[str, str]:
    return {
        "kind": safe_id(action.kind),
        "subject_id": safe_id(action.subject_id),
        "reason": safe_id(action.reason),
    }


def _attention_payload(item: object) -> dict[str, str]:
    return {
        "kind": safe_id(item.kind),
        "subject_id": safe_id(item.subject_id),
        "reason": safe_id(item.reason),
    }


def _data(snapshot: WorkspaceReadSnapshot) -> dict[str, object]:
    status_counts: dict[str, int] = {}
    for task in snapshot.tasks:
        status = safe_id(task.status)
        status_counts[status] = status_counts.get(status, 0) + 1
    return {
        "workspace": {
            "name": safe_title(snapshot.workspace_name),
            "workspace_revision": safe_sha256(snapshot.workspace_revision),
            "completeness": snapshot.completeness if snapshot.completeness in {"complete", "partial", "unavailable"} else "unavailable",
            "proof_inspection": (
                snapshot.proof_inspection
                if snapshot.proof_inspection in {"not_inspected", "inspected"}
                else "not_inspected"
            ),
        },
        "task_status_counts": dict(sorted(status_counts.items())),
        "lines": [
            {
                "id": safe_id(line.id),
                "kind": safe_id(line.kind),
                "branch": safe_branch(line.branch),
                "base": safe_branch(line.base),
                "repository_count": line.repository_count,
            }
            for line in snapshot.lines
        ],
        "tasks": [
            {
                "id": safe_id(task.id),
                "title": safe_title(task.title),
                "line": safe_id(task.line),
                "status": safe_id(task.status),
                "risk": safe_id(task.risk),
                "depends_on": [safe_id(item) for item in task.depends_on],
                "blocked_on": [safe_id(item) for item in task.blocked_on],
                "conflict_group": safe_id(task.conflict_group) if task.conflict_group else "",
                "executor": safe_id(task.executor),
                "reviewer": safe_id(task.reviewer),
                "integration_state": safe_id(task.integration_state),
                "external_claim_active": task.external_claim_active,
            }
            for task in snapshot.tasks
        ],
        "objectives": [
            {
                "id": safe_id(objective.id),
                "title": safe_title(objective.title),
                "line": safe_id(objective.line),
                "revision": objective.revision,
                "operator_state": safe_id(objective.operator_state),
                "derived_result": safe_id(objective.derived_result),
                "requested_mode": safe_id(objective.requested_mode),
                "operations": [safe_id(operation) for operation in objective.operations],
                "scope_count": objective.scope_count,
                "budget": dict(objective.budget),
                "selected_actions": [_action_payload(item) for item in objective.selected_actions],
                "blocked_actions": [_action_payload(item) for item in objective.blocked_actions],
                "attention": [_attention_payload(item) for item in objective.attention],
                "contract_sha256": safe_sha256(objective.contract_sha256),
                "scope_sha256": safe_sha256(objective.scope_sha256),
                "event_sha256": safe_sha256(objective.event_sha256),
            }
            for objective in snapshot.objectives
        ],
    }


def proof_inspect_data(snapshot: WorkspaceReadSnapshot) -> dict[str, object]:
    """Whitelisted inspect facts. No argv, paths, logs, or procedure text."""
    return {
        "proof_inspection": (
            snapshot.proof_inspection
            if snapshot.proof_inspection in {"not_inspected", "inspected"}
            else "not_inspected"
        ),
        "proofs": [
            {
                "id": safe_sha256(item.id),
                "kind": safe_id(item.kind),
                "subject": safe_id(item.subject),
                "status": safe_id(item.status),
                "decay_reason": safe_id(item.decay_reason) if item.decay_reason else "",
            }
            for item in snapshot.proofs
            if safe_sha256(item.id) != REDACTED
        ],
        "objectives": [
            {
                "id": safe_id(objective.id),
                "attention": [_attention_payload(item) for item in objective.attention],
            }
            for objective in snapshot.objectives
            if safe_id(objective.id) != REDACTED
        ],
    }


def proof_inspect_envelope(snapshot: WorkspaceReadSnapshot) -> dict[str, object]:
    """Path-free Proof inspect DTO. Separate from summary workspace_envelope."""
    data = proof_inspect_data(snapshot)
    digest = hashlib.sha256(canonical_json_bytes(data)).hexdigest()
    warnings = tuple(sorted({failure.code for failure in snapshot.failures}))
    partial = snapshot.completeness != "complete"
    return ConsoleEnvelope(
        captured_at=snapshot.observed_at,
        snapshot_sha256=digest,
        freshness_state="partial" if partial else "fresh",
        partial=partial,
        warnings=warnings,
        data=data,
    ).to_payload()


def workspace_envelope(snapshot: WorkspaceReadSnapshot) -> dict[str, object]:
    """Return the shared Console response envelope for one Core capture.

    The digest binds only the sanitized presentation facts.  A fresh sampling
    timestamp or future browser locale selection cannot invalidate an ETag.
    """
    data = _data(snapshot)
    digest = hashlib.sha256(canonical_json_bytes(data)).hexdigest()
    warnings = tuple(sorted({failure.code for failure in snapshot.failures}))
    partial = snapshot.completeness != "complete"
    return ConsoleEnvelope(
        captured_at=snapshot.observed_at,
        snapshot_sha256=digest,
        freshness_state="partial" if partial else "fresh",
        partial=partial,
        warnings=warnings,
        data=data,
    ).to_payload()

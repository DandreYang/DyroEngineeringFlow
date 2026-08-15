"""Pure continuation planning and path-free read projections.

All I/O belongs to :mod:`dyro.continuation.snapshot`.  Every function here is
deterministic for a supplied snapshot and may therefore be reused by CLI,
daemon compatibility wrappers, and the local control console.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Iterable

from ..canonical import canonical_json_bytes
from ..errors import ValidationError
from ..tasks import Task
from .models import (
    ActionKind,
    AttentionItem,
    AttentionKind,
    ContinuationPlan,
    PlanCompletion,
    PlannedAction,
    ReasonCode,
    SchedulerEdge,
    SchedulerNode,
    SchedulerReadProjection,
)
from .snapshot import SchedulerSnapshot


@dataclass(frozen=True)
class TaskReadiness:
    """The shared task scheduler result used by legacy and Objective flows."""

    ready: tuple[Task, ...]
    review: tuple[Task, ...]
    blocked: tuple[PlannedAction, ...]


def _facts(**values: object) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((key, str(value)) for key, value in values.items() if value != ""))


def _action(
    kind: ActionKind,
    subject_id: str,
    reason: ReasonCode,
    **facts: object,
) -> PlannedAction:
    return PlannedAction(kind=kind, subject_id=subject_id, reason=reason, facts=_facts(**facts))


def _active_conflicts(snapshot: SchedulerSnapshot) -> dict[str, tuple[str, ...]]:
    active: dict[str, list[str]] = {}
    for item in snapshot.tasks:
        task = item.task
        if not task.conflict_group:
            continue
        if item.status == "in_progress" or (
            snapshot.execution_mode == "external"
            and item.status == "assigned"
            and item.external_claim_active
        ):
            active.setdefault(task.conflict_group, []).append(task.id)
    return {group: tuple(sorted(task_ids)) for group, task_ids in active.items()}


def build_task_readiness(
    snapshot: SchedulerSnapshot,
    *,
    candidate_ids: Iterable[str] | None = None,
) -> TaskReadiness:
    """Classify task execution/review eligibility without reading workspace state."""
    by_id = snapshot.tasks_by_id
    requested = tuple(sorted(snapshot.candidate_ids if candidate_ids is None else candidate_ids))
    unknown = sorted(set(requested) - set(by_id))
    if unknown:
        raise ValidationError(f"调度候选不在快照中：{', '.join(unknown)}")
    if len(set(requested)) != len(requested):
        raise ValidationError("调度候选不能重复")
    decision_states = dict(snapshot.decisions)
    active_conflicts = _active_conflicts(snapshot)
    ready: list[Task] = []
    review: list[Task] = []
    blocked: list[PlannedAction] = []
    for task_id in requested:
        item = by_id[task_id]
        task = item.task
        if item.status == "review":
            review.append(task)
            continue
        if item.status not in {"backlog", "assigned"}:
            continue
        if item.external_claim_active:
            blocked.append(
                _action(
                    ActionKind.EXECUTE_TASK,
                    task.id,
                    ReasonCode.EXTERNAL_CLAIM_ACTIVE,
                    status=item.status,
                )
            )
            continue
        unresolved = tuple(sorted(key for key in task.blocked_on if decision_states.get(key) != "resolved"))
        if unresolved:
            blocked.append(
                _action(
                    ActionKind.EXECUTE_TASK,
                    task.id,
                    ReasonCode.DECISION_OPEN,
                    decision_ids=",".join(unresolved),
                )
            )
            continue
        dependencies = tuple(sorted(task.depends_on))
        pending_dependency = next(
            (
                dependency
                for dependency in dependencies
                if by_id[dependency].status != "done"
            ),
            "",
        )
        if pending_dependency:
            blocked.append(
                _action(
                    ActionKind.EXECUTE_TASK,
                    task.id,
                    ReasonCode.DEPENDENCY_PENDING,
                    dependency_id=pending_dependency,
                    dependency_status=by_id[pending_dependency].status,
                )
            )
            continue
        integration_pending = next(
            (
                dependency
                for dependency in dependencies
                if by_id[dependency].integration_state != "integrated"
            ),
            "",
        )
        if integration_pending:
            blocked.append(
                _action(
                    ActionKind.EXECUTE_TASK,
                    task.id,
                    ReasonCode.TASK_INTEGRATION_PENDING,
                    dependency_id=integration_pending,
                )
            )
            continue
        conflicts = tuple(
            item_id
            for item_id in active_conflicts.get(task.conflict_group, ())
            if item_id != task.id
        )
        if conflicts:
            blocked.append(
                _action(
                    ActionKind.EXECUTE_TASK,
                    task.id,
                    ReasonCode.CONFLICT_GROUP_ACTIVE,
                    conflict_group=task.conflict_group,
                    active_task_ids=",".join(conflicts),
                )
            )
            continue
        ready.append(task)
    return TaskReadiness(
        ready=tuple(sorted(ready, key=lambda task: task.id)),
        review=tuple(sorted(review, key=lambda task: task.id)),
        blocked=tuple(sorted(blocked, key=lambda action: action.subject_id)),
    )


def _action_payload(action: PlannedAction) -> dict[str, object]:
    return {
        "kind": action.kind.value,
        "subject_id": action.subject_id,
        "reason": action.reason.value,
        "facts": dict(action.facts),
    }


def _attention_payload(item: AttentionItem) -> dict[str, object]:
    return {
        "id": item.id,
        "kind": item.kind.value,
        "subject_id": item.subject_id,
        "reason": item.reason.value,
        "facts": dict(item.facts),
    }


def continuation_plan_payload(plan: ContinuationPlan) -> dict[str, object]:
    """Return a JSON-compatible plan representation with no workspace paths."""
    return {
        "schema_version": 1,
        "objective_id": plan.objective_id,
        "snapshot_sha256": plan.snapshot_sha256,
        "plan_sha256": plan.plan_sha256,
        "completion": plan.completion.value,
        "selected_actions": [_action_payload(item) for item in plan.selected_actions],
        "blocked": [_action_payload(item) for item in plan.blocked],
        "attention": [_attention_payload(item) for item in plan.attention],
        "next_wake_at": None if plan.next_wake_at is None else plan.next_wake_at.isoformat(),
        "facts": dict(plan.facts),
    }


def _build_plan(
    snapshot: SchedulerSnapshot,
    completion: PlanCompletion,
    selected: Iterable[PlannedAction] = (),
    blocked: Iterable[PlannedAction] = (),
    attention: Iterable[AttentionItem] = (),
    **facts: object,
) -> ContinuationPlan:
    selected_actions = tuple(sorted(selected, key=lambda item: (item.kind.value, item.subject_id)))
    blocked_actions = tuple(sorted(blocked, key=lambda item: (item.kind.value, item.subject_id)))
    attention_items = tuple(sorted(attention, key=lambda item: item.id))
    payload = {
        "schema_version": 1,
        "objective_id": snapshot.objective_id,
        "snapshot_sha256": snapshot.snapshot_sha256,
        "completion": completion.value,
        "selected_actions": [_action_payload(item) for item in selected_actions],
        "blocked": [_action_payload(item) for item in blocked_actions],
        "attention": [_attention_payload(item) for item in attention_items],
        "facts": dict(_facts(**facts)),
    }
    plan_sha256 = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    return ContinuationPlan(
        objective_id=snapshot.objective_id,
        snapshot_sha256=snapshot.snapshot_sha256,
        plan_sha256=plan_sha256,
        completion=completion,
        selected_actions=selected_actions,
        blocked=blocked_actions,
        attention=attention_items,
        facts=_facts(**facts),
    )


def build_continuation_plan(snapshot: SchedulerSnapshot) -> ContinuationPlan:
    """Choose plan-only Objective actions from a fixed read snapshot."""
    if not snapshot.objective_id or snapshot.objective_revision < 1:
        raise ValidationError("Objective 计划必须使用带 revision 的调度快照")
    if snapshot.objective_drifted:
        action = _action(ActionKind.REPAIR_REQUIRED, snapshot.objective_id, ReasonCode.CONTRACT_DRIFT)
        attention = AttentionItem(
            id=f"repair:{snapshot.objective_id}",
            kind=AttentionKind.REPAIR_REQUIRED,
            subject_id=snapshot.objective_id,
            reason=ReasonCode.CONTRACT_DRIFT,
        )
        return _build_plan(snapshot, PlanCompletion.REPAIR_REQUIRED, (action,), attention=(attention,))
    if snapshot.objective_state != "active":
        action = _action(ActionKind.PAUSE, snapshot.objective_id, ReasonCode.OBJECTIVE_PAUSED)
        attention = AttentionItem(
            id=f"paused:{snapshot.objective_id}",
            kind=AttentionKind.PAUSED,
            subject_id=snapshot.objective_id,
            reason=ReasonCode.OBJECTIVE_PAUSED,
            facts=_facts(operator_state=snapshot.objective_state),
        )
        return _build_plan(snapshot, PlanCompletion.INCOMPLETE, (action,), attention=(attention,))
    by_id = snapshot.tasks_by_id
    decayed_attention = tuple(
        AttentionItem(
            id=f"proof-decayed:{task_id}",
            kind=AttentionKind.NEEDS_USER,
            subject_id=task_id,
            reason=ReasonCode.PROOF_DECAYED,
            facts=_facts(status="decayed"),
        )
        for task_id in snapshot.decayed_merge_subjects
        if task_id in snapshot.objective_scope or task_id in snapshot.objective_targets
    )
    target_complete = all(
        target in by_id
        and by_id[target].status == "done"
        and by_id[target].integration_state == "integrated"
        for target in snapshot.objective_targets
    )
    if target_complete:
        action = _action(ActionKind.COMPLETE, snapshot.objective_id, ReasonCode.TARGETS_INTEGRATED)
        return _build_plan(snapshot, PlanCompletion.COMPLETE, (action,), attention=decayed_attention)
    scope = tuple(sorted(set(snapshot.objective_scope) & set(snapshot.candidate_ids)))
    readiness = build_task_readiness(snapshot, candidate_ids=scope)
    selected: list[PlannedAction] = []
    blocked = list(readiness.blocked)
    attention: list[AttentionItem] = list(decayed_attention)
    execute_allowed = (
        snapshot.objective_requested_mode != "observe"
        and "execute" in snapshot.objective_operations
    )
    for task in readiness.ready:
        if execute_allowed:
            selected.append(_action(ActionKind.EXECUTE_TASK, task.id, ReasonCode.TASK_READY))
        else:
            blocked.append(
                _action(
                    ActionKind.EXECUTE_TASK,
                    task.id,
                    ReasonCode.POLICY_DISALLOWS_OPERATION,
                    requested_mode=snapshot.objective_requested_mode,
                    operation="execute",
                )
            )
    review_allowed = (
        snapshot.objective_requested_mode != "observe"
        and "review" in snapshot.objective_operations
    )
    for task in readiness.review:
        if review_allowed:
            selected.append(_action(ActionKind.REVIEW_TASK, task.id, ReasonCode.TASK_REVIEW_READY))
        else:
            blocked.append(
                _action(
                    ActionKind.REVIEW_TASK,
                    task.id,
                    ReasonCode.POLICY_DISALLOWS_OPERATION,
                    requested_mode=snapshot.objective_requested_mode,
                    operation="review",
                )
            )
    for task_id in scope:
        item = by_id[task_id]
        if item.status == "waiting_answer":
            selected.append(_action(ActionKind.ASK_USER, task_id, ReasonCode.ANSWER_REQUIRED))
            attention.append(
                AttentionItem(
                    id=f"answer:{task_id}",
                    kind=AttentionKind.NEEDS_USER,
                    subject_id=task_id,
                    reason=ReasonCode.ANSWER_REQUIRED,
                )
            )
        elif item.status == "failed":
            attention.append(
                AttentionItem(
                    id=f"failed:{task_id}",
                    kind=AttentionKind.NEEDS_USER,
                    subject_id=task_id,
                    reason=ReasonCode.TASK_FAILED,
                )
            )
        elif item.status == "done" and item.integration_state != "integrated":
            blocked.append(
                _action(
                    ActionKind.WAIT
                    if item.integration_state == "not_inspected"
                    else ActionKind.MERGE_TASK,
                    task_id,
                    ReasonCode.TASK_INTEGRATION_PENDING,
                )
            )
    if not selected and not blocked and not attention:
        selected.append(_action(ActionKind.WAIT, snapshot.objective_id, ReasonCode.NO_PROGRESS))
    return _build_plan(
        snapshot,
        PlanCompletion.INCOMPLETE,
        selected,
        blocked,
        attention,
        objective_revision=snapshot.objective_revision,
    )


def build_scheduler_projection(
    snapshot: SchedulerSnapshot,
    plan: ContinuationPlan,
) -> SchedulerReadProjection:
    """Build the single path-free graph payload consumed by all presentation layers."""
    if plan.objective_id != snapshot.objective_id:
        raise ValidationError("计划与快照 Objective 不匹配")
    nodes: list[SchedulerNode] = [
        SchedulerNode(
            id=f"objective:{snapshot.objective_id}",
            kind="objective",
            state=plan.completion.value,
            facts=_facts(revision=snapshot.objective_revision, operator_state=snapshot.objective_state),
        )
    ]
    edges: list[SchedulerEdge] = []
    for item in snapshot.tasks:
        task = item.task
        nodes.append(
            SchedulerNode(
                id=f"task:{task.id}",
                kind="task",
                state=item.status,
                facts=_facts(line=task.line, integration_state=item.integration_state),
            )
        )
        for dependency in sorted(task.depends_on):
            edges.append(SchedulerEdge(f"task:{dependency}", f"task:{task.id}", "requires"))
        for decision in sorted(task.blocked_on):
            decision_id = f"decision:{decision}"
            nodes.append(
                SchedulerNode(
                    id=decision_id,
                    kind="decision",
                    state=dict(snapshot.decisions).get(decision, "missing"),
                )
            )
            edges.append(SchedulerEdge(decision_id, f"task:{task.id}", "blocks"))
    for action in (*plan.selected_actions, *plan.blocked):
        action_id = f"action:{action.kind.value}:{action.subject_id}"
        nodes.append(
            SchedulerNode(
                id=action_id,
                kind="action",
                state=action.reason.value,
                facts=action.facts,
            )
        )
        target = f"objective:{snapshot.objective_id}" if action.subject_id == snapshot.objective_id else f"task:{action.subject_id}"
        edges.append(SchedulerEdge(action_id, target, "acts_on"))
    unique_nodes = {node.id: node for node in nodes}
    constraints = tuple(
        (f"conflict_group:{item.task.conflict_group}", item.task.id)
        for item in snapshot.tasks
        if item.task.conflict_group
    )
    return SchedulerReadProjection(
        schema_version=1,
        objective_id=snapshot.objective_id,
        objective_revision=snapshot.objective_revision,
        snapshot_sha256=snapshot.snapshot_sha256,
        plan_sha256=plan.plan_sha256,
        completion=plan.completion,
        selected_actions=plan.selected_actions,
        blocked=plan.blocked,
        attention=plan.attention,
        nodes=tuple(unique_nodes[key] for key in sorted(unique_nodes)),
        edges=tuple(sorted(set(edges), key=lambda edge: (edge.source, edge.target, edge.kind))),
        constraints=tuple(sorted(constraints)),
        facts=plan.facts,
    )


def projection_payload(projection: SchedulerReadProjection) -> dict[str, object]:
    return {
        "schema_version": projection.schema_version,
        "objective_id": projection.objective_id,
        "objective_revision": projection.objective_revision,
        "snapshot_sha256": projection.snapshot_sha256,
        "plan_sha256": projection.plan_sha256,
        "completion": projection.completion.value,
        "selected_actions": [_action_payload(item) for item in projection.selected_actions],
        "blocked": [_action_payload(item) for item in projection.blocked],
        "attention": [_attention_payload(item) for item in projection.attention],
        "nodes": [
            {"id": node.id, "kind": node.kind, "state": node.state, "facts": dict(node.facts)}
            for node in projection.nodes
        ],
        "edges": [
            {"source": edge.source, "target": edge.target, "kind": edge.kind}
            for edge in projection.edges
        ],
        "constraints": [
            {"kind": kind, "subject_id": subject_id}
            for kind, subject_id in projection.constraints
        ],
        "facts": dict(projection.facts),
    }


def render_plan_text(plan: ContinuationPlan) -> str:
    lines = [
        f"Objective: {plan.objective_id}",
        f"Completion: {plan.completion.value}",
        f"Snapshot SHA-256: {plan.snapshot_sha256}",
        f"Plan SHA-256: {plan.plan_sha256}",
    ]
    for label, actions in (("Selected", plan.selected_actions), ("Blocked", plan.blocked)):
        for action in actions:
            lines.append(f"{label}: {action.kind.value} {action.subject_id} ({action.reason.value})")
    for item in plan.attention:
        lines.append(f"Attention: {item.kind.value} {item.subject_id} ({item.reason.value})")
    return "\n".join(lines)


def render_projection_json(projection: SchedulerReadProjection) -> str:
    return json.dumps(projection_payload(projection), ensure_ascii=False, sort_keys=True, indent=2)


def render_projection_mermaid(projection: SchedulerReadProjection) -> str:
    node_ids = {node.id: f"N{index}" for index, node in enumerate(projection.nodes)}
    lines = ["flowchart LR"]
    for node in projection.nodes:
        label = f"{node.id}<br/>[{node.kind}:{node.state}]".replace('"', "&quot;")
        lines.append(f'  {node_ids[node.id]}["{label}"]')
    for edge in projection.edges:
        if edge.source in node_ids and edge.target in node_ids:
            lines.append(f"  {node_ids[edge.source]} -->|{edge.kind}| {node_ids[edge.target]}")
    return "\n".join(lines)

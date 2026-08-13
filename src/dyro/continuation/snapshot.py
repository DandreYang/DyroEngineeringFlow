"""Read-only inputs for the deterministic continuation planner.

This module samples workspace state once.  It deliberately performs no state
transition, Agent invocation, or network access; planner callers can safely
reuse its immutable result for text, JSON, and graph renderings.

Integration sampling may issue a local, read-only ``git merge-base`` probe for
completed dependencies.  That probe is represented in the returned snapshot;
the planner itself never starts a process.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from typing import Callable, Iterable

from ..canonical import canonical_json_bytes
from ..config import Config
from ..errors import DyroError, ValidationError
from .. import graph as task_graph
from .. import tasks as task_module
from ..read_limits import ReadBudget
from ..tasks import Task
from ..workspace import list_lines
from .objective_storage import StoredObjective


@dataclass(frozen=True)
class SchedulerTaskSnapshot:
    """Internal planner facts sampled for one task.

    ``Task`` retains its local directory only while evaluating the scheduler.
    Public consumers receive the separately path-free read projection.
    """

    task: Task
    status: str
    external_claim_active: bool
    integration_state: str
    contract_sha256: str = ""

    def __post_init__(self) -> None:
        if self.integration_state not in {
            "not_required",
            "not_inspected",
            "integrated",
            "pending",
        }:
            raise TypeError("调度快照 integration_state 无效")


@dataclass(frozen=True)
class SchedulerSnapshot:
    """An immutable, single-read scheduler input with a stable fingerprint."""

    observed_at: datetime
    tasks: tuple[SchedulerTaskSnapshot, ...]
    decisions: tuple[tuple[str, str], ...]
    execution_mode: str
    candidate_ids: tuple[str, ...]
    snapshot_sha256: str
    objective_id: str = ""
    objective_revision: int = 0
    objective_state: str = ""
    objective_scope: tuple[str, ...] = ()
    objective_targets: tuple[str, ...] = ()
    objective_requested_mode: str = ""
    objective_operations: tuple[str, ...] = ()
    objective_drifted: bool = False

    @property
    def tasks_by_id(self) -> dict[str, SchedulerTaskSnapshot]:
        return {item.task.id: item for item in self.tasks}


def _utc(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ValidationError("调度快照时钟必须带时区")
    return now.astimezone(timezone.utc)


def _payload(
    *,
    observed_at: datetime,
    tasks: tuple[SchedulerTaskSnapshot, ...],
    decisions: tuple[tuple[str, str], ...],
    execution_mode: str,
    candidate_ids: tuple[str, ...],
    objective: StoredObjective | None,
    task_contracts: tuple[tuple[str, str], ...],
    objective_drifted: bool,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "observed_at": observed_at.isoformat(),
        "execution_mode": execution_mode,
        "tasks": [
            {
                "id": item.task.id,
                "line": item.task.line,
                "status": item.status,
                "depends_on": list(item.task.depends_on),
                "blocked_on": list(item.task.blocked_on),
                "conflict_group": item.task.conflict_group,
                "external_claim_active": item.external_claim_active,
                "integration_state": item.integration_state,
                "contract_sha256": item.contract_sha256,
            }
            for item in tasks
        ],
        "decisions": dict(decisions),
        "candidate_ids": list(candidate_ids),
    }
    if objective is not None:
        payload["objective"] = {
            "id": objective.objective.id,
            "revision": objective.revision,
            "operator_state": objective.operator_state,
            "scope": list(objective.scope),
            "targets": list(objective.objective.targets),
            "requested_mode": objective.objective.requested_mode.value,
            "operations": [item.value for item in objective.objective.operations],
            "task_contract_sha256": [list(item) for item in task_contracts],
            "drifted": objective_drifted,
        }
    return payload


def _task_contract_sha256(task: Task) -> str:
    try:
        return hashlib.sha256((task.directory / "task.toml").read_bytes()).hexdigest()
    except OSError as exc:
        raise ValidationError(f"无法读取任务 {task.id} contract") from exc


def _current_scope(
    objective: StoredObjective,
    known_by_id: dict[str, Task],
    contract_sha256_by_id: dict[str, str],
) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...], bool]:
    """Derive an Objective closure from the already-sampled graph.

    The third return value means the graph cannot safely represent the stored
    Objective and therefore must be repaired rather than scheduled.
    """
    closure: set[str] = set()
    pending = list(objective.objective.targets)
    invalid = False
    while pending:
        task_id = pending.pop()
        if task_id in closure:
            continue
        task = known_by_id.get(task_id)
        if task is None or task.line != objective.objective.line:
            invalid = True
            continue
        closure.add(task_id)
        pending.extend(task.depends_on)
    scope = tuple(sorted(closure))
    try:
        contracts = tuple(
            (task_id, contract_sha256_by_id[task_id]) for task_id in scope
        )
    except KeyError:
        return scope, (), True
    return scope, contracts, invalid


def _integration_state(
    config: Config,
    task: Task,
    task_status: str,
    *,
    required: bool,
    inspect: bool,
) -> str:
    if task_status != "done" or not required:
        return "not_required"
    if not inspect:
        # Summary-only Console captures never start a Git subprocess.  This is
        # distinct from ``pending``: the fact was not inspected, not judged
        # broken or healthy.
        return "not_inspected"
    try:
        task_module._assert_dependency_integrated(config, task)
    except (DyroError, ValidationError):
        return "pending"
    return "integrated"


def build_scheduler_snapshot(
    config: Config,
    *,
    objective: StoredObjective | None = None,
    candidates: Iterable[Task] | None = None,
    inspect_integration: bool = True,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> SchedulerSnapshot:
    """Read the task graph exactly once and publish canonical, immutable facts."""
    graph = task_graph.build_task_graph(config)
    issues = task_graph.validate_task_graph(graph)
    if issues:
        details = "; ".join(issue.message for issue in issues[:5])
        raise ValidationError(f"任务图结构无效：{details}")
    observed_at = _utc(clock())
    known_tasks = tuple(sorted(graph.known_tasks, key=lambda item: item.id))
    known_by_id = {task.id: task for task in known_tasks}
    candidate_ids = tuple(
        sorted(
            task.id
            for task in (known_tasks if candidates is None else tuple(candidates))
        )
    )
    unknown = sorted(set(candidate_ids) - set(known_by_id))
    if unknown:
        raise ValidationError(f"调度候选不在 TaskGraph 中：{', '.join(unknown)}")
    if len(set(candidate_ids)) != len(candidate_ids):
        raise ValidationError("调度候选不能重复")
    integration_required_ids = {
        dependency
        for task_id in candidate_ids
        for dependency in known_by_id[task_id].depends_on
    }
    if objective is not None:
        integration_required_ids.update(objective.objective.targets)
    statuses = {task.id: task_module.status(config, task) for task in known_tasks}
    contract_sha256_by_id = (
        {task.id: _task_contract_sha256(task) for task in known_tasks}
        if objective is not None
        else {}
    )
    tasks = tuple(
        SchedulerTaskSnapshot(
            task=task,
            status=statuses[task.id],
            external_claim_active=(
                graph.execution_mode == "external"
                and statuses[task.id] == "assigned"
                and task_module.external_claim_active(task, now=observed_at)
            ),
            integration_state=_integration_state(
                config,
                task,
                statuses[task.id],
                required=task.id in integration_required_ids,
                inspect=inspect_integration,
            ),
            contract_sha256=contract_sha256_by_id.get(task.id, ""),
        )
        for task in known_tasks
    )
    return build_scheduler_snapshot_from_facts(
        tasks=tasks,
        decisions=tuple(sorted(graph.decisions.items())),
        execution_mode=graph.execution_mode,
        candidate_ids=candidate_ids,
        objective=objective,
        observed_at=observed_at,
    )


def build_scheduler_snapshot_bounded(
    config: Config,
    *,
    objective: StoredObjective,
    budget: ReadBudget,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> SchedulerSnapshot:
    """Sample planner facts through bounded, symlink-safe manifest reads."""

    observed_at = _utc(clock())
    known_line_ids = frozenset(
        line.id for line in list_lines(config, read_budget=budget)
    )
    sampled = tuple(
        task_module.load_task_planning_bounded(
            config,
            task_id,
            budget,
            known_line_ids=known_line_ids,
        )
        for task_id in task_module.list_task_ids_bounded(config, budget)
    )
    known_tasks = tuple(item[0] for item in sampled)
    statuses = {task.id: current for task, current, _digest in sampled}
    digests = {task.id: digest for task, _current, digest in sampled}
    decisions = task_module.decisions_bounded(config, budget)
    graph = task_graph.TaskGraph(
        line=None,
        tasks=known_tasks,
        known_tasks=known_tasks,
        decisions=decisions,
        execution_mode=config.policy.execution_mode,
    )
    issues = task_graph.validate_task_graph(graph)
    if issues:
        details = "; ".join(issue.message for issue in issues[:5])
        raise ValidationError(f"任务图结构无效：{details}")

    integration_required_ids = {
        dependency for task in known_tasks for dependency in task.depends_on
    }
    integration_required_ids.update(objective.objective.targets)
    facts = tuple(
        SchedulerTaskSnapshot(
            task=task,
            status=statuses[task.id],
            external_claim_active=(
                config.policy.execution_mode == "external"
                and statuses[task.id] == "assigned"
                and task_module.external_claim_active_bounded(
                    config, task, budget, now=observed_at
                )
            ),
            integration_state=(
                task_module.dependency_integration_state_bounded(
                    config, task, budget
                )
                if statuses[task.id] == "done"
                and task.id in integration_required_ids
                else "not_required"
            ),
            contract_sha256=digests[task.id],
        )
        for task in known_tasks
    )
    candidate_ids = tuple(task.id for task in known_tasks)
    return build_scheduler_snapshot_from_facts(
        tasks=facts,
        decisions=tuple(sorted(decisions.items())),
        execution_mode=config.policy.execution_mode,
        candidate_ids=candidate_ids,
        objective=objective,
        observed_at=observed_at,
    )


def build_scheduler_snapshot_from_facts(
    *,
    tasks: Iterable[SchedulerTaskSnapshot],
    decisions: Iterable[tuple[str, str]],
    execution_mode: str,
    candidate_ids: Iterable[str],
    observed_at: datetime,
    objective: StoredObjective | None = None,
) -> SchedulerSnapshot:
    """Build a scheduler snapshot from one caller-owned, already sampled fact set.

    Machine-facing readers use this entry point after bounded filesystem and Git
    observations.  It performs no I/O and therefore cannot silently fall back to
    the legacy, presentation-oriented loaders.
    """
    observed_at = _utc(observed_at)
    frozen_tasks = tuple(sorted(tasks, key=lambda item: item.task.id))
    if len({item.task.id for item in frozen_tasks}) != len(frozen_tasks):
        raise ValidationError("调度快照 Task ID 不能重复")
    frozen_candidates = tuple(sorted(candidate_ids))
    known_by_id = {item.task.id: item.task for item in frozen_tasks}
    unknown = sorted(set(frozen_candidates) - set(known_by_id))
    if unknown:
        raise ValidationError(f"调度候选不在 Task facts 中：{', '.join(unknown)}")
    if len(set(frozen_candidates)) != len(frozen_candidates):
        raise ValidationError("调度候选不能重复")
    frozen_decisions = tuple(sorted(decisions))
    if len({key for key, _value in frozen_decisions}) != len(frozen_decisions):
        raise ValidationError("调度快照 decision ID 不能重复")
    if execution_mode not in {"local", "external"}:
        raise ValidationError("调度快照 execution mode 无效")

    contract_sha256_by_id = {
        item.task.id: item.contract_sha256
        for item in frozen_tasks
        if item.contract_sha256
    }
    task_contracts: tuple[tuple[str, str], ...] = ()
    objective_drifted = False
    if objective is not None:
        current_scope, task_contracts, invalid_scope = _current_scope(
            objective,
            known_by_id,
            contract_sha256_by_id,
        )
        objective_drifted = (
            invalid_scope
            or current_scope != objective.scope
            or task_contracts != objective.task_contract_sha256
        )
    digest = hashlib.sha256(
        canonical_json_bytes(
            _payload(
                observed_at=observed_at,
                tasks=frozen_tasks,
                decisions=frozen_decisions,
                execution_mode=execution_mode,
                candidate_ids=frozen_candidates,
                objective=objective,
                task_contracts=task_contracts,
                objective_drifted=objective_drifted,
            )
        )
    ).hexdigest()
    return SchedulerSnapshot(
        observed_at=observed_at,
        tasks=frozen_tasks,
        decisions=frozen_decisions,
        execution_mode=execution_mode,
        candidate_ids=frozen_candidates,
        snapshot_sha256=digest,
        objective_id="" if objective is None else objective.objective.id,
        objective_revision=0 if objective is None else objective.revision,
        objective_state="" if objective is None else objective.operator_state,
        objective_scope=() if objective is None else objective.scope,
        objective_targets=() if objective is None else objective.objective.targets,
        objective_requested_mode=""
        if objective is None
        else objective.objective.requested_mode.value,
        objective_operations=()
        if objective is None
        else tuple(item.value for item in objective.objective.operations),
        objective_drifted=objective_drifted,
    )

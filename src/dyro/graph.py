from __future__ import annotations

import json
from dataclasses import dataclass

from .config import Config
from .errors import DyroError
from .tasks import Task, decisions, external_claim_active, list_tasks, status


@dataclass(frozen=True)
class GraphIssue:
    code: str
    message: str
    task_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class TaskGraph:
    line: str | None
    tasks: tuple[Task, ...]
    known_tasks: tuple[Task, ...]
    decisions: dict[str, str]
    execution_mode: str


def build_task_graph(config: Config, *, line: str | None = None) -> TaskGraph:
    known_tasks = tuple(list_tasks(config))
    tasks = tuple(task for task in known_tasks if line is None or task.line == line)
    return TaskGraph(
        line=line,
        tasks=tasks,
        known_tasks=known_tasks,
        decisions=decisions(config),
        execution_mode=config.policy.execution_mode,
    )


def _cycles(graph: TaskGraph) -> list[tuple[str, ...]]:
    task_ids = {task.id for task in graph.tasks}
    dependencies = {
        task.id: tuple(
            dependency
            for dependency in task.depends_on
            if dependency in task_ids and dependency != task.id
        )
        for task in graph.tasks
    }
    colors: dict[str, str] = {}
    stack: list[str] = []
    found: list[tuple[str, ...]] = []
    seen: set[tuple[str, ...]] = set()

    def visit(task_id: str) -> None:
        colors[task_id] = "visiting"
        stack.append(task_id)
        for dependency in dependencies[task_id]:
            if colors.get(dependency) == "visiting":
                start = stack.index(dependency)
                cycle = tuple(stack[start:] + [dependency])
                key = tuple(sorted(set(cycle)))
                if key not in seen:
                    seen.add(key)
                    found.append(cycle)
            elif colors.get(dependency) != "visited":
                visit(dependency)
        stack.pop()
        colors[task_id] = "visited"

    for task_id in sorted(task_ids):
        if task_id not in colors:
            visit(task_id)
    return found


def validate_task_graph(graph: TaskGraph) -> tuple[GraphIssue, ...]:
    issues: list[GraphIssue] = []
    known_by_id: dict[str, Task] = {}
    duplicate_ids: set[str] = set()
    for task in graph.known_tasks:
        if task.id in known_by_id:
            duplicate_ids.add(task.id)
        known_by_id[task.id] = task

    for task_id in sorted(duplicate_ids):
        issues.append(
            GraphIssue(
                code="duplicate_task",
                message=f"任务 ID 重复：{task_id}",
                task_ids=(task_id,),
            )
        )

    for task in graph.tasks:
        if len(set(task.depends_on)) != len(task.depends_on):
            issues.append(
                GraphIssue(
                    code="duplicate_dependency",
                    message=f"任务 {task.id} 包含重复依赖",
                    task_ids=(task.id,),
                )
            )
        if len(set(task.blocked_on)) != len(task.blocked_on):
            issues.append(
                GraphIssue(
                    code="duplicate_decision",
                    message=f"任务 {task.id} 包含重复决策点",
                    task_ids=(task.id,),
                )
            )
        for dependency in task.depends_on:
            if dependency == task.id:
                issues.append(
                    GraphIssue(
                        code="self_dependency",
                        message=f"任务 {task.id} 不能依赖自身",
                        task_ids=(task.id,),
                    )
                )
                continue
            dependency_task = known_by_id.get(dependency)
            if dependency_task is None:
                issues.append(
                    GraphIssue(
                        code="missing_dependency",
                        message=f"任务 {task.id} 引用了不存在的依赖 {dependency}",
                        task_ids=(task.id, dependency),
                    )
                )
            elif dependency_task.line != task.line:
                issues.append(
                    GraphIssue(
                        code="cross_line_dependency",
                        message=(
                            f"任务 {task.id} 位于开发线 {task.line}，不能依赖开发线 "
                            f"{dependency_task.line} 的任务 {dependency}"
                        ),
                        task_ids=(task.id, dependency),
                    )
                )
        for decision_id in task.blocked_on:
            if decision_id not in graph.decisions:
                issues.append(
                    GraphIssue(
                        code="missing_decision",
                        message=f"任务 {task.id} 引用了不存在的决策点 {decision_id}",
                        task_ids=(task.id,),
                    )
                )

    for cycle in _cycles(graph):
        issues.append(
            GraphIssue(
                code="dependency_cycle",
                message=f"任务依赖存在环：{' -> '.join(cycle)}",
                task_ids=cycle,
            )
        )
    return tuple(issues)


def explain_task(config: Config, task_id: str) -> dict[str, object]:
    graph = build_task_graph(config)
    for task in graph.tasks:
        if task.id == task_id:
            return _explain_with_config(config, graph, task)
    raise DyroError(f"任务不存在：{task_id}")


def _explain_with_config(config: Config, graph: TaskGraph, task: Task) -> dict[str, object]:
    known_by_id = {candidate.id: candidate for candidate in graph.known_tasks}
    task_status = status(config, task)
    reasons: list[str] = []
    dependencies: list[dict[str, str]] = []
    decision_rows: list[dict[str, str]] = []
    conflicts: list[str] = []

    if task_status not in ("backlog", "assigned"):
        reasons.append(f"任务状态为 {task_status}，只有 backlog 或 assigned 可以调度")

    for dependency in task.depends_on:
        dependency_task = known_by_id.get(dependency)
        dependency_status = "missing" if dependency_task is None else status(config, dependency_task)
        dependencies.append({"id": dependency, "status": dependency_status})
        if dependency_status != "done":
            reasons.append(f"依赖 {dependency} 尚未完成，当前状态为 {dependency_status}")

    for decision_id in task.blocked_on:
        decision_status = graph.decisions.get(decision_id, "missing")
        decision_rows.append({"id": decision_id, "status": decision_status})
        if decision_status != "resolved":
            reasons.append(f"决策点 {decision_id} 尚未解决，当前状态为 {decision_status}")

    if task.conflict_group:
        conflicts = sorted(
            candidate.id
            for candidate in graph.known_tasks
            if candidate.id != task.id
            and candidate.conflict_group == task.conflict_group
            and (
                status(config, candidate) == "in_progress"
                or (
                    graph.execution_mode == "external"
                    and status(config, candidate) == "assigned"
                    and external_claim_active(candidate)
                )
            )
        )
        if conflicts:
            reasons.append(
                f"冲突组 {task.conflict_group} 已被任务 {', '.join(conflicts)} 占用"
            )

    return {
        "id": task.id,
        "title": task.title,
        "line": task.line,
        "status": task_status,
        "dispatchable": not reasons,
        "depends_on": dependencies,
        "blocked_on": decision_rows,
        "conflict_group": task.conflict_group,
        "active_conflicts": conflicts,
        "reasons": reasons,
    }


def render_task_explanation(report: dict[str, object]) -> str:
    lines = [
        f"任务：{report['id']}  {report['title']}",
        f"开发线：{report['line']}",
        f"状态：{report['status']}",
        f"可调度：{'YES' if report['dispatchable'] else 'NO'}",
    ]
    conflict_group = str(report["conflict_group"])
    if conflict_group:
        lines.append(f"冲突组：{conflict_group}")
    reasons = list(report["reasons"])  # type: ignore[arg-type]
    if reasons:
        lines.append("原因：")
        lines.extend(f"- {reason}" for reason in reasons)
    else:
        lines.append("原因：所有依赖、决策点与冲突约束均已满足")
    return "\n".join(lines) + "\n"


def _graph_payload(config: Config, graph: TaskGraph) -> dict[str, object]:
    tasks_by_id = {task.id: task for task in graph.tasks}
    issues = validate_task_graph(graph)
    referenced_decisions = sorted(
        {decision_id for task in graph.tasks for decision_id in task.blocked_on}
    )
    nodes: list[dict[str, object]] = []
    for task in sorted(graph.tasks, key=lambda item: item.id):
        explanation = _explain_with_config(config, graph, task)
        nodes.append(
            {
                "id": task.id,
                "type": "task",
                "title": task.title,
                "line": task.line,
                "status": explanation["status"],
                "dispatchable": explanation["dispatchable"],
                "executor": task.executor,
                "reviewer": task.reviewer,
                "risk": task.risk,
                "conflict_group": task.conflict_group,
            }
        )
    for decision_id in referenced_decisions:
        nodes.append(
            {
                "id": f"decision:{decision_id}",
                "type": "decision",
                "status": graph.decisions.get(decision_id, "missing"),
            }
        )

    edges: list[dict[str, str]] = []
    for task in sorted(graph.tasks, key=lambda item: item.id):
        for dependency in sorted(task.depends_on):
            if dependency in tasks_by_id:
                edges.append({"from": dependency, "to": task.id, "type": "requires"})
        for decision_id in sorted(task.blocked_on):
            edges.append(
                {
                    "from": f"decision:{decision_id}",
                    "to": task.id,
                    "type": "blocks",
                }
            )

    conflict_groups: dict[str, list[str]] = {}
    for task in graph.tasks:
        if task.conflict_group:
            conflict_groups.setdefault(task.conflict_group, []).append(task.id)
    for task_ids in conflict_groups.values():
        task_ids.sort()

    return {
        "schema_version": 1,
        "line": graph.line,
        "nodes": nodes,
        "edges": edges,
        "constraints": {"conflict_groups": dict(sorted(conflict_groups.items()))},
        "issues": [
            {
                "code": issue.code,
                "message": issue.message,
                "task_ids": list(issue.task_ids),
            }
            for issue in issues
        ],
    }


def render_task_graph(config: Config, graph: TaskGraph, *, output_format: str) -> str:
    if output_format == "json":
        return json.dumps(
            _graph_payload(config, graph),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n"
    if output_format != "mermaid":
        raise DyroError(f"不支持的任务图格式：{output_format}")
    return _render_mermaid(config, graph)


def _mermaid_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _render_mermaid(config: Config, graph: TaskGraph) -> str:
    tasks = sorted(graph.tasks, key=lambda item: item.id)
    task_nodes = {task.id: f"T{index}" for index, task in enumerate(tasks)}
    decision_ids = sorted({decision for task in tasks for decision in task.blocked_on})
    decision_nodes = {
        decision_id: f"D{index}" for index, decision_id in enumerate(decision_ids)
    }
    lines = ["flowchart LR"]
    if not tasks:
        lines.append('  EMPTY["No tasks"]')
        return "\n".join(lines) + "\n"

    for task in tasks:
        task_status = status(config, task)
        label = _mermaid_label(f"{task.id}<br/>{task.title}<br/>[{task_status}]")
        lines.append(f'  {task_nodes[task.id]}["{label}"]')
    for decision_id in decision_ids:
        decision_status = graph.decisions.get(decision_id, "missing")
        label = _mermaid_label(f"{decision_id}<br/>[{decision_status}]")
        lines.append(f'  {decision_nodes[decision_id]}{{"{label}"}}')

    for task in tasks:
        for dependency in sorted(task.depends_on):
            if dependency in task_nodes:
                lines.append(
                    f"  {task_nodes[dependency]} -->|requires| {task_nodes[task.id]}"
                )
        for decision_id in sorted(task.blocked_on):
            lines.append(
                f"  {decision_nodes[decision_id]} -.->|blocks| {task_nodes[task.id]}"
            )

    lines.extend(
        [
            "  classDef backlog fill:#f8fafc,stroke:#64748b,color:#0f172a",
            "  classDef assigned fill:#e0f2fe,stroke:#0284c7,color:#0c4a6e",
            "  classDef in_progress fill:#fef3c7,stroke:#d97706,color:#78350f",
            "  classDef review fill:#ede9fe,stroke:#7c3aed,color:#4c1d95",
            "  classDef done fill:#dcfce7,stroke:#16a34a,color:#14532d",
            "  classDef failed fill:#fee2e2,stroke:#dc2626,color:#7f1d1d",
        ]
    )
    supported_classes = {"backlog", "assigned", "in_progress", "review", "done", "failed"}
    for task in tasks:
        task_status = status(config, task)
        if task_status in supported_classes:
            lines.append(f"  class {task_nodes[task.id]} {task_status}")
    for group, task_ids in sorted(
        _graph_payload(config, graph)["constraints"]["conflict_groups"].items()  # type: ignore[index,union-attr]
    ):
        lines.append(f"  %% conflict_group {group}: {', '.join(task_ids)}")
    return "\n".join(lines) + "\n"

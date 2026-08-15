"""Peer-wave scheduling: parallel task executors, not one writer plus watchers."""

from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import Iterable, Mapping, Sequence

from .errors import ValidationError
from .tasks import ScheduleBlock, ScheduleWave, Task

_READY_TTL_SECONDS = 30.0
_READY_LOCK = threading.Lock()
_READY_CACHE: tuple[float, tuple[str, ...]] | None = None


AUTO_EXECUTOR = "auto"
PEER_WAVE_CAP = 3
MAX_PER_BACKEND = 2
CURSOR_WRITE_PROVIDER = "cursor-agent"


@dataclass(frozen=True)
class ExecutorBinding:
    task_id: str
    executor: str
    pinned: bool
    source: str


@dataclass(frozen=True)
class HarnessDecision:
    bindings: tuple[ExecutorBinding, ...]
    deferred: tuple[ScheduleBlock, ...]
    warnings: tuple[str, ...]

    @property
    def bound_tasks(self) -> tuple[str, ...]:
        return tuple(item.task_id for item in self.bindings)

    def executor_for(self, task_id: str) -> str | None:
        for item in self.bindings:
            if item.task_id == task_id:
                return item.executor
        return None


def write_capable_dispatch_ids() -> tuple[str, ...]:
    from experiments.local_agent_dispatch.adapters.registry import (
        get_adapter,
        list_real_provider_ids,
    )

    capable: list[str] = []
    for provider in list_real_provider_ids():
        adapter = get_adapter(provider)
        modes = getattr(
            adapter, "supported_modes", frozenset({"read-only", "edit"})
        )
        if "edit" in modes:
            capable.append(provider)
    return tuple(capable)


def discover_available_write_providers() -> tuple[str, ...]:
    from experiments.local_agent_dispatch.adapters.registry import get_adapter

    return tuple(
        provider
        for provider in write_capable_dispatch_ids()
        if get_adapter(provider).available()
    )


def discover_ready_write_providers(*, force: bool = False) -> tuple[str, ...]:
    from experiments.local_agent_dispatch.adapters.registry import (
        adapter_is_authenticated,
        get_adapter,
    )

    global _READY_CACHE
    now = time.monotonic()
    with _READY_LOCK:
        if (
            not force
            and _READY_CACHE is not None
            and now - _READY_CACHE[0] < _READY_TTL_SECONDS
        ):
            return _READY_CACHE[1]
    ready: list[str] = []
    for provider in write_capable_dispatch_ids():
        adapter = get_adapter(provider)
        if adapter.available() and adapter_is_authenticated(adapter):
            ready.append(provider)
    found = tuple(ready)
    with _READY_LOCK:
        _READY_CACHE = (time.monotonic(), found)
    return found


def recommended_max_parallel(requested: int, ready_count: int) -> int:
    if type(requested) is not int or requested < 1:
        raise ValidationError("max_parallel 必须是正整数")
    if type(ready_count) is not int or ready_count < 0:
        raise ValidationError("ready_count 必须是非负整数")
    if ready_count <= 0:
        return requested
    return max(1, min(requested, ready_count))


def empty_conflict_group_warnings(
    tasks: Sequence[Task], *, max_parallel: int
) -> tuple[str, ...]:
    if max_parallel <= 1:
        return ()
    empty = tuple(task.id for task in tasks if not task.conflict_group)
    if not empty:
        return ()
    return (
        "parallel wave includes tasks without conflict_group: "
        + ", ".join(empty),
    )


def assert_write_executor_allowed(executor: str, *, risk: str) -> None:
    if risk == "write" and executor == CURSOR_WRITE_PROVIDER:
        raise ValidationError(
            "Cursor Agent 不能进入写波次；其 edit 在沙箱进程生命周期获证前保持 fail-closed"
        )


def bind_wave_executors(
    tasks: Sequence[Task],
    ready_write: Sequence[str],
    *,
    max_per_backend: int = MAX_PER_BACKEND,
) -> HarnessDecision:
    if type(max_per_backend) is not int or max_per_backend < 1:
        raise ValidationError("max_per_backend 必须是正整数")
    ready = tuple(provider for provider in ready_write if provider)
    write_ids = frozenset(write_capable_dispatch_ids())
    counts: dict[str, int] = {}
    bindings: list[ExecutorBinding] = []
    deferred: list[ScheduleBlock] = []
    auto_pool = [provider for provider in ready if provider != CURSOR_WRITE_PROVIDER]

    for task in tasks:
        try:
            assert_write_executor_allowed(task.executor, risk=task.risk)
        except ValidationError as exc:
            deferred.append(ScheduleBlock(task=task, reason=str(exc)))
            continue
        if task.executor == AUTO_EXECUTOR:
            chosen = _take_idle(auto_pool, counts, max_per_backend)
            if chosen is None:
                deferred.append(
                    ScheduleBlock(
                        task=task,
                        reason="没有空闲的可写 Harness 可分配给 auto executor",
                    )
                )
                continue
            counts[chosen] = counts.get(chosen, 0) + 1
            bindings.append(
                ExecutorBinding(
                    task_id=task.id,
                    executor=chosen,
                    pinned=False,
                    source="auto",
                )
            )
            continue
        if task.executor in write_ids and task.executor in ready:
            if counts.get(task.executor, 0) >= max_per_backend:
                deferred.append(
                    ScheduleBlock(
                        task=task,
                        reason=(
                            f"写 Harness {task.executor} 已达到每后端上限 "
                            f"{max_per_backend}"
                        ),
                    )
                )
                continue
            counts[task.executor] = counts.get(task.executor, 0) + 1
            bindings.append(
                ExecutorBinding(
                    task_id=task.id,
                    executor=task.executor,
                    pinned=True,
                    source="task",
                )
            )
            continue
        counts[task.executor] = counts.get(task.executor, 0) + 1
        bindings.append(
            ExecutorBinding(
                task_id=task.id,
                executor=task.executor,
                pinned=True,
                source="profile",
            )
        )
    warnings = empty_conflict_group_warnings(tasks, max_parallel=max(1, len(tasks)))
    return HarnessDecision(
        bindings=tuple(bindings),
        deferred=tuple(deferred),
        warnings=warnings,
    )


def apply_harness_bindings(
    wave: ScheduleWave,
    ready_write: Sequence[str],
    *,
    max_per_backend: int = MAX_PER_BACKEND,
) -> tuple[tuple[Task, ...], HarnessDecision]:
    decision = bind_wave_executors(
        wave.tasks, ready_write, max_per_backend=max_per_backend
    )
    bound_ids = set(decision.bound_tasks)
    bound_tasks = tuple(task for task in wave.tasks if task.id in bound_ids)
    return bound_tasks, HarnessDecision(
        bindings=decision.bindings,
        deferred=wave.deferred + decision.deferred,
        warnings=decision.warnings,
    )


def peer_wave_overlay(
    *,
    tasks: Sequence[Task],
    max_parallel: int,
    bindings: Sequence[ExecutorBinding] = (),
    deferred: Sequence[ScheduleBlock] = (),
    extra_warnings: Sequence[str] = (),
) -> dict[str, object]:
    warnings = list(empty_conflict_group_warnings(tasks, max_parallel=max_parallel))
    warnings.extend(extra_warnings)
    return {
        "peer_wave": {
            "schema_version": 1,
            "warnings": warnings,
            "executor_bindings": [
                {
                    "task_id": item.task_id,
                    "executor": item.executor,
                    "pinned": item.pinned,
                    "source": item.source,
                }
                for item in bindings
            ],
            "harness_deferred": [
                {"task_id": item.task.id, "reason": item.reason} for item in deferred
            ],
        }
    }


def _take_idle(
    pool: Sequence[str], counts: Mapping[str, int], max_per_backend: int
) -> str | None:
    for provider in pool:
        if counts.get(provider, 0) < max_per_backend:
            return provider
    return None


def annotate_objective_tick(
    snapshot: object,
    plan: object,
    tick: object,
    ready_write: Sequence[str],
) -> dict[str, object]:
    from .continuation.models import ActionKind

    execute_tasks: list[Task] = []
    wave_tasks: list[Task] = []
    tasks_by_id = getattr(snapshot, "tasks_by_id", {})
    for action in getattr(plan, "selected_actions", ()):
        if getattr(action, "kind", None) is not ActionKind.EXECUTE_TASK:
            continue
        item = tasks_by_id.get(action.subject_id)
        if item is None:
            continue
        execute_tasks.append(item.task)
    for action in getattr(tick, "wave", ()):
        if getattr(action, "kind", None) is not ActionKind.EXECUTE_TASK:
            continue
        item = tasks_by_id.get(action.subject_id)
        if item is not None:
            wave_tasks.append(item.task)
    decision = bind_wave_executors(wave_tasks, ready_write)
    return peer_wave_overlay(
        tasks=execute_tasks or wave_tasks,
        max_parallel=int(getattr(tick, "max_parallel", 1)),
        bindings=decision.bindings,
        deferred=decision.deferred,
    )


def iter_bound_pairs(
    tasks: Iterable[Task], decision: HarnessDecision
) -> tuple[tuple[Task, str], ...]:
    by_id = {task.id: task for task in tasks}
    pairs: list[tuple[Task, str]] = []
    for binding in decision.bindings:
        task = by_id.get(binding.task_id)
        if task is not None:
            pairs.append((task, binding.executor))
    return tuple(pairs)

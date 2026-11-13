"""Run a Core task through a dispatch adapter inside its existing worktree."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

from experiments.local_agent_dispatch.adapters.registry import get_adapter
from experiments.local_agent_dispatch.context_guard import guard_file
from experiments.local_agent_dispatch.errors import DispatchValidationError
from experiments.local_agent_dispatch.fileset import SKIP_DIRS
from experiments.local_agent_dispatch.task_contract import parse_task_contract

from .capability.cards import card_forbids_execute
from .errors import ValidationError
from .peer_wave import AUTO_EXECUTOR, assert_write_executor_allowed
from .process import Result
from .tasks import Task


_MAX_BOUND_FILES = 20


def is_dispatch_provider(executor: str) -> bool:
    from experiments.local_agent_dispatch.adapters.registry import list_real_provider_ids

    return executor in list_real_provider_ids()


def is_dispatch_write_ready(executor: str) -> bool:
    if not is_dispatch_provider(executor):
        return False
    from experiments.local_agent_dispatch.adapters.registry import (
        adapter_is_authenticated,
    )

    adapter = get_adapter(executor)
    modes = getattr(adapter, "supported_modes", frozenset({"read-only", "edit"}))
    return (
        "edit" in modes
        and adapter.available()
        and adapter_is_authenticated(adapter)
    )


def collect_bound_files(workspace: Path) -> tuple[str, ...]:
    root = Path(workspace).resolve(strict=True)
    matched: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in SKIP_DIRS for part in relative.parts):
            continue
        try:
            verdict = guard_file(path, root, read_content=True)
            if not verdict.allowed:
                continue
        except DispatchValidationError:
            continue
        matched.append(relative.as_posix())
        if len(matched) >= _MAX_BOUND_FILES:
            break
    return tuple(matched)


def build_bound_contract(
    task: Task,
    *,
    executor: str,
    workspace: Path,
    prompt: str,
) -> object:
    files = collect_bound_files(workspace)
    if not files:
        raise ValidationError(
            f"任务 {task.id} 的 worktree 没有可供派发的守卫文件"
        )
    mounts = ", ".join(task.repositories) or workspace.name
    return parse_task_contract(
        {
            "schema_version": 1,
            "backend": executor,
            "mode": "edit" if task.risk == "write" else "read-only",
            "strict": False,
            "allow_unconfined_provider": False,
            "allow_offline_simulation": executor == "echo",
            "files": list(files),
            "task": {
                "briefing": f"Dyro task {task.id} on line {task.line}.",
                "locations": f"Work only inside {workspace}; repositories: {mounts}.",
                "objective": prompt,
                "constraints": (
                    "Do not merge, push, sign off, or leave the task worktree. "
                    "Write the receipt at the path given in the objective."
                ),
                "output_contract": (
                    "Update the task receipt with result: DONE, result: BLOCKED, "
                    "or result: QUESTION."
                ),
            },
        }
    )


def run_task_bound_dispatch(
    task: Task,
    *,
    executor: str,
    workspace: Path,
    prompt: str,
    timeout_seconds: float,
    dry_run: bool = False,
    capabilities: Mapping[str, object] | None = None,
) -> Result:
    if executor == AUTO_EXECUTOR:
        raise ValidationError("auto executor 必须在派发前绑定到具体 Harness")
    if task.risk == "write":
        if capabilities is None:
            raise ValidationError("write dispatch 必须提供 Capability 平面")
        if card_forbids_execute(capabilities.get(executor)):
            raise ValidationError(
                f"Capability {executor} 未授予 execute，不能作为任务执行器"
            )
    assert_write_executor_allowed(executor, risk=task.risk)
    argv = ("dyro", "task-dispatch", executor, task.id)
    if dry_run:
        return Result(argv, 0, "")
    if executor != "echo" and not is_dispatch_provider(executor):
        raise ValidationError(f"executor 不是 dispatch Provider：{executor}")
    adapter = get_adapter(executor)
    modes = getattr(adapter, "supported_modes", frozenset({"read-only", "edit"}))
    required_mode = "edit" if task.risk == "write" else "read-only"
    if required_mode not in modes:
        raise ValidationError(
            f"dispatch Provider 不支持 mode={required_mode}：{executor}"
        )
    contract = build_bound_contract(
        task, executor=executor, workspace=workspace, prompt=prompt
    )
    context: Mapping[str, str] = {}
    try:
        result = adapter.run(
            contract=contract,
            cwd=workspace,
            context_files=context,
            timeout_seconds=timeout_seconds,
        )
    except DispatchValidationError as exc:
        raise ValidationError(str(exc)) from exc
    stdout = "\n".join(
        part
        for part in (result.summary, result.raw_preview, "\n".join(result.warnings))
        if part
    )
    code = 0 if result.status == "ok" else 1
    return Result(argv, code, stdout)

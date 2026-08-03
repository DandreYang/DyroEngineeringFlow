"""Durable, fail-closed Objective storage and mutation-scope ownership.

The store is deliberately independent from execution.  It records accepted
Objective revisions and their TaskGraph-derived scope, but it never starts an
Agent, changes a Task state, or treats a lifecycle flag as completion proof.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
from typing import Iterable, Iterator

from ..config import Config, validate_id
from ..errors import DyroError, ValidationError
from ..graph import build_task_graph, validate_task_graph
from ..state import (
    ensure_safe_child_directory,
    exclusive_directory_lock,
    exclusive_lock,
)
from ..tasks import Task, list_tasks, status as task_status
from .contracts import canonical_contract, contract_sha256, parse_contract, validate_objective_scope
from .models import Objective
from .objective_storage import (
    OBJECTIVE_STORE_SCHEMA_VERSION,
    OPERATOR_STATES,
    ObjectiveDirectory,
    StoredObjective,
    _sha256,
    commit_event as _commit_event,
    list_objective_ids,
    open_objective_directory,
    read_stored as _read_stored,
    recover_pending as _recover_pending,
)


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _objective_root(config: Config, *, create: bool = True) -> Path:
    if os.name == "nt":
        raise DyroError("Windows 暂不支持安全的 Objective 持久化；拒绝访问以避免 reparse-point 路径逃逸")
    parent = config.root / ".dyro"
    if parent.is_symlink():
        raise ValidationError(f"Objective 状态父目录不能是符号链接：{parent}")
    if parent.exists() and not parent.is_dir():
        raise ValidationError(f"Objective 状态父路径不是目录：{parent}")
    if not parent.exists():
        if not create:
            return config.objectives_dir
        try:
            ensure_safe_child_directory(config.root, ".dyro")
        except DyroError as exc:
            raise ValidationError(f"Objective 状态父目录必须是安全的普通目录：{parent}") from exc
    root = config.objectives_dir
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise ValidationError(f"Objective 状态目录必须是安全的普通目录：{root}")
    if not root.exists() and create:
        try:
            ensure_safe_child_directory(parent, "objectives")
        except DyroError as exc:
            raise ValidationError(f"Objective 状态目录必须是安全的普通目录：{root}") from exc
    return root


@contextmanager
def _objective_lock(config: Config, *, create: bool = True) -> Iterator[None]:
    """Lock Objective state only after validating its non-symlink parent.

    The lock is opened relative to a non-symlink directory descriptor, after
    establishing the safe ``.dyro/objectives`` tree.  Replacing ``.dyro`` by
    a symlink cannot redirect lock creation outside the workspace.
    """
    root = _objective_root(config, create=create)
    if not root.exists():
        yield
        return
    with exclusive_directory_lock(config.root / ".dyro", "objectives.lock"):
        yield


@contextmanager
def _objective_mutation_lock(config: Config) -> Iterator[None]:
    """Serialize Objective ownership changes with Task reservations.

    The global order is Objective lock, then dispatch lock.  Legacy automated
    schedulers take the same order immediately before reserving a Task, so an
    Objective cannot be accepted between their ownership check and launch.
    """
    from ..tasks import _dispatch_lock_path

    with _objective_lock(config):
        with exclusive_lock(_dispatch_lock_path(config)):
            yield


def _objective_dir(
    config: Config,
    objective_id: str,
    *,
    must_exist: bool = True,
    create_root: bool = True,
) -> Path:
    validate_id(objective_id, "Objective ID")
    path = _objective_root(config, create=create_root) / objective_id
    if path.is_symlink():
        raise ValidationError(f"Objective 目录不能是符号链接：{path}")
    if must_exist and not path.is_dir():
        raise DyroError(f"Objective 不存在：{objective_id}")
    if path.exists() and not path.is_dir():
        raise ValidationError(f"Objective 路径不是目录：{path}")
    return path


def _contract_path(directory: Path, revision: int) -> Path:
    if type(revision) is not int or revision < 1:
        raise ValidationError("Objective revision 必须是正整数")
    return directory / f"contract-{revision}.toml"


def _safe_file(path: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_file():
        raise ValidationError(f"{label} 必须是安全的普通文件：{path}")
    return path


def _render_contract(objective: Objective) -> str:
    """Render a complete canonical TOML representation for an accepted revision."""
    rendered = canonical_contract(objective)
    continuation = rendered["continuation"]
    budget = rendered["budget"]
    assert isinstance(continuation, dict) and isinstance(budget, dict)
    targets = ", ".join(_toml_string(item) for item in rendered["targets"])
    operations = ", ".join(_toml_string(item) for item in continuation["operations"])
    lines = [
        "schema_version = 1",
        f"id = {_toml_string(str(rendered['id']))}",
        f"title = {_toml_string(str(rendered['title']))}",
        f"line = {_toml_string(str(rendered['line']))}",
        f"targets = [{targets}]",
        f"completion = {_toml_string(str(rendered['completion']))}",
        "",
        "[continuation]",
        f"requested_mode = {_toml_string(str(continuation['requested_mode']))}",
        f"operations = [{operations}]",
        "",
        "[budget]",
    ]
    for field in (
        "max_actions",
        "max_attempts_per_task",
        "max_failures",
        "max_no_progress_cycles",
        "max_parallel",
    ):
        lines.append(f"{field} = {budget[field]}")
    if "deadline" in budget:
        lines.append(f"deadline = {_toml_string(str(budget['deadline']))}")
    return "\n".join(lines) + "\n"


def _task_contract_sha256(task: Task) -> str:
    manifest = _safe_file(task.directory / "task.toml", f"任务 {task.id} contract")
    try:
        return hashlib.sha256(manifest.read_bytes()).hexdigest()
    except OSError as exc:
        raise ValidationError(f"无法读取任务 {task.id} contract：{manifest}") from exc


def _scope_for(config: Config, objective: Objective) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
    graph = build_task_graph(config, line=objective.line)
    issues = validate_task_graph(graph)
    if issues:
        details = "；".join(issue.message for issue in issues[:5])
        raise ValidationError(f"TaskGraph 无法用于 Objective scope：{details}")
    known = {task.id: task for task in graph.known_tasks}
    task_lines = {task_id: task.line for task_id, task in known.items()}
    validate_objective_scope(objective, task_lines)
    closure: set[str] = set()
    pending = list(objective.targets)
    while pending:
        task_id = pending.pop()
        if task_id in closure:
            continue
        task = known.get(task_id)
        if task is None:
            raise ValidationError(f"Objective target 不存在：{task_id}")
        if task.line != objective.line:
            raise ValidationError(f"Objective target 不能跨 line：{task_id}")
        closure.add(task_id)
        pending.extend(task.depends_on)
    scope = tuple(sorted(closure))
    contracts = tuple((task_id, _task_contract_sha256(known[task_id])) for task_id in scope)
    return scope, contracts


def _scope_sha256(scope: tuple[str, ...], contracts: tuple[tuple[str, str], ...]) -> str:
    return _sha256({"scope": list(scope), "task_contract_sha256": [list(item) for item in contracts]})


def _record_payload(record: StoredObjective) -> dict[str, object]:
    return {
        "schema_version": OBJECTIVE_STORE_SCHEMA_VERSION,
        "id": record.objective.id,
        "revision": record.revision,
        "operator_state": record.operator_state,
        "scope": list(record.scope),
        "task_contract_sha256": [list(item) for item in record.task_contract_sha256],
        "scope_sha256": record.scope_sha256,
        "contract_sha256": record.contract_sha256,
    }


def _record_from_payload(
    directory: Path,
    payload: object,
    *,
    event_seq: int,
    event_sha256: str,
    contract_content: bytes | None = None,
) -> StoredObjective:
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "id",
        "revision",
        "operator_state",
        "scope",
        "task_contract_sha256",
        "scope_sha256",
        "contract_sha256",
    }:
        raise ValidationError(f"Objective 投影结构无效：{directory}")
    if type(payload.get("schema_version")) is not int or payload.get("schema_version") != OBJECTIVE_STORE_SCHEMA_VERSION:
        raise ValidationError(f"Objective 投影版本无效：{directory}")
    raw_id = payload.get("id")
    if not isinstance(raw_id, str):
        raise ValidationError(f"Objective ID 投影无效：{directory}")
    objective_id = validate_id(raw_id, "Objective ID")
    revision = payload.get("revision")
    if type(revision) is not int or revision < 1:
        raise ValidationError(f"Objective revision 无效：{directory}")
    operator_state = payload.get("operator_state")
    if not isinstance(operator_state, str) or operator_state not in OPERATOR_STATES:
        raise ValidationError(f"Objective 操作者状态无效：{directory}")
    scope_raw = payload.get("scope")
    if not isinstance(scope_raw, list) or not scope_raw or not all(isinstance(item, str) for item in scope_raw):
        raise ValidationError(f"Objective scope 无效：{directory}")
    scope = tuple(validate_id(item, "Objective scope task") for item in scope_raw)
    if scope != tuple(sorted(scope)) or len(set(scope)) != len(scope):
        raise ValidationError(f"Objective scope 必须排序且不重复：{directory}")
    contracts_raw = payload.get("task_contract_sha256")
    if not isinstance(contracts_raw, list):
        raise ValidationError(f"Objective task contract 投影无效：{directory}")
    contracts: list[tuple[str, str]] = []
    for item in contracts_raw:
        if not isinstance(item, list) or len(item) != 2 or not all(isinstance(part, str) for part in item):
            raise ValidationError(f"Objective task contract 投影无效：{directory}")
        task_id = validate_id(item[0], "Objective scope task")
        digest = item[1]
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValidationError(f"Objective task contract 哈希无效：{directory}")
        contracts.append((task_id, digest))
    frozen_contracts = tuple(contracts)
    if tuple(task_id for task_id, _ in frozen_contracts) != scope:
        raise ValidationError(f"Objective scope 与 task contract 不一致：{directory}")
    scope_sha = payload.get("scope_sha256")
    if not isinstance(scope_sha, str) or scope_sha != _scope_sha256(scope, frozen_contracts):
        raise ValidationError(f"Objective scope 哈希无效：{directory}")
    contract_sha = payload.get("contract_sha256")
    if not isinstance(contract_sha, str) or len(contract_sha) != 64 or any(char not in "0123456789abcdef" for char in contract_sha):
        raise ValidationError(f"Objective contract 哈希无效：{directory}")
    if contract_content is None:
        contract_file = _safe_file(_contract_path(directory, revision), "Objective contract")
        try:
            contract_content = contract_file.read_bytes()
        except OSError as exc:
            raise ValidationError(f"无法读取 Objective contract：{contract_file}") from exc
    objective = parse_contract(contract_content)
    if objective.id != objective_id or contract_sha256(objective) != contract_sha:
        raise ValidationError(f"Objective contract 与投影不匹配：{directory}")
    return StoredObjective(
        objective=objective,
        revision=revision,
        operator_state=operator_state,
        scope=scope,
        task_contract_sha256=frozen_contracts,
        scope_sha256=scope_sha,
        contract_sha256=contract_sha,
        event_seq=event_seq,
        event_sha256=event_sha256,
    )


def _make_record(
    objective: Objective,
    *,
    revision: int,
    operator_state: str,
    scope: tuple[str, ...],
    contracts: tuple[tuple[str, str], ...],
    event_seq: int = 0,
    event_sha256: str = "",
) -> StoredObjective:
    if operator_state not in OPERATOR_STATES:
        raise ValidationError(f"非法 Objective 操作者状态：{operator_state}")
    return StoredObjective(
        objective=objective,
        revision=revision,
        operator_state=operator_state,
        scope=scope,
        task_contract_sha256=contracts,
        scope_sha256=_scope_sha256(scope, contracts),
        contract_sha256=contract_sha256(objective),
        event_seq=event_seq,
        event_sha256=event_sha256,
    )


def _assert_ownership_available(
    config: Config,
    record: StoredObjective,
    *,
    exclude_id: str = "",
    recover: bool = True,
) -> None:
    if not record.owns_mutation_scope:
        return
    requested = set(record.scope)
    for other in list_objectives(config, recover=recover):
        if other.objective.id == exclude_id or not other.owns_mutation_scope:
            continue
        overlap = sorted(requested & set(other.scope))
        if overlap:
            raise DyroError(
                f"Objective mutation scope 与 active Objective {other.objective.id} 重叠：{', '.join(overlap)}"
            )


def _assert_no_inflight_tasks(config: Config, record: StoredObjective) -> None:
    tasks = {task.id: task for task in list_tasks(config)}
    in_flight = [
        task_id
        for task_id in record.scope
        if task_id in tasks and task_status(config, tasks[task_id]) in {"assigned", "in_progress"}
    ]
    if in_flight:
        raise DyroError(f"存在 reserved/started/running Task，拒绝变更 Objective：{', '.join(in_flight)}")


def create_objective(config: Config, content: str | bytes, *, dry_run: bool = False) -> StoredObjective:
    """Accept one Objective contract and pin its TaskGraph-derived scope."""
    objective = parse_contract(content)
    if dry_run:
        scope, contracts = _scope_for(config, objective)
        record = _make_record(
            objective,
            revision=1,
            operator_state="active",
            scope=scope,
            contracts=contracts,
        )
        _assert_ownership_available(config, record, recover=False)
        _assert_no_inflight_tasks(config, record)
        return record
    with _objective_mutation_lock(config):
        candidate_path = _objective_dir(config, objective.id, must_exist=False)
        if candidate_path.exists():
            with open_objective_directory(config, objective.id) as directory:
                recovered = _recover_pending(directory)
            if not recovered:
                raise DyroError(f"Objective 已存在：{objective.id}")
        scope, contracts = _scope_for(config, objective)
        record = _make_record(
            objective,
            revision=1,
            operator_state="active",
            scope=scope,
            contracts=contracts,
        )
        _assert_ownership_available(config, record)
        _assert_no_inflight_tasks(config, record)
        with open_objective_directory(config, objective.id, create=True) as directory:
            return _commit_event(
                directory,
                record,
                "created",
                contract_content=_render_contract(objective).encode("utf-8"),
            )


def _list_objectives_unlocked(config: Config, *, recover: bool) -> list[StoredObjective]:
    records: list[StoredObjective] = []
    for objective_id in list_objective_ids(config):
        with open_objective_directory(config, objective_id) as directory:
            records.append(_read_stored(config, objective_id, recover=recover, directory=directory))
    return records


def list_objectives(config: Config, *, recover: bool = True) -> list[StoredObjective]:
    if not recover:
        return _list_objectives_unlocked(config, recover=recover)
    # Recovery and normal reads share the writer's lock.  This prevents a
    # reader from observing a pending marker immediately after its initial
    # scan, then treating a live transaction as an abandoned one.
    with _objective_lock(config, create=False):
        return _list_objectives_unlocked(config, recover=True)


def get_objective(config: Config, objective_id: str, *, recover: bool = True) -> StoredObjective:
    if recover:
        with _objective_lock(config, create=False):
            with open_objective_directory(config, objective_id) as directory:
                return _read_stored(config, objective_id, recover=True, directory=directory)
    with open_objective_directory(config, objective_id) as directory:
        return _read_stored(config, objective_id, recover=False, directory=directory)


def _persist_revision(
    config: Config,
    current: StoredObjective,
    objective: Objective,
    *,
    directory: ObjectiveDirectory,
    operator_state: str,
    event_name: str,
) -> StoredObjective:
    scope, contracts = _scope_for(config, objective)
    candidate = _make_record(
        objective,
        revision=current.revision + 1,
        operator_state=operator_state,
        scope=scope,
        contracts=contracts,
        event_seq=current.event_seq,
        event_sha256=current.event_sha256,
    )
    _assert_ownership_available(config, candidate, exclude_id=current.objective.id)
    _assert_no_inflight_tasks(config, candidate)
    return _commit_event(
        directory,
        candidate,
        event_name,
        contract_content=_render_contract(objective).encode("utf-8"),
    )


def reconcile_objective(config: Config, objective_id: str, *, dry_run: bool = False) -> StoredObjective:
    if dry_run:
        current = get_objective(config, objective_id, recover=False)
        if current.operator_state == "stopped":
            raise DyroError("已停止的 Objective 不能 reconcile；请创建新的 Objective")
        scope, contracts = _scope_for(config, current.objective)
        candidate = _make_record(
            current.objective,
            revision=current.revision + 1,
            operator_state=current.operator_state,
            scope=scope,
            contracts=contracts,
            event_seq=current.event_seq,
            event_sha256=current.event_sha256,
        )
        _assert_ownership_available(config, candidate, exclude_id=current.objective.id, recover=False)
        _assert_no_inflight_tasks(config, candidate)
        return candidate
    with _objective_mutation_lock(config):
        with open_objective_directory(config, objective_id) as directory:
            current = _read_stored(config, objective_id, directory=directory)
            if current.operator_state == "stopped":
                raise DyroError("已停止的 Objective 不能 reconcile；请创建新的 Objective")
            return _persist_revision(
                config,
                current,
                current.objective,
                directory=directory,
                operator_state=current.operator_state,
                event_name="reconciled",
            )


def _with_targets(objective: Objective, targets: Iterable[str]) -> Objective:
    target_set = tuple(sorted({validate_id(target, "Objective target") for target in targets}))
    if not target_set:
        raise ValidationError("Objective 必须至少保留一个 target")
    return Objective(
        schema_version=objective.schema_version,
        id=objective.id,
        title=objective.title,
        line=objective.line,
        targets=target_set,
        completion=objective.completion,
        requested_mode=objective.requested_mode,
        operations=objective.operations,
        budget=objective.budget,
    )


def add_objective_target(config: Config, objective_id: str, task_id: str, *, dry_run: bool = False) -> StoredObjective:
    if dry_run:
        current = get_objective(config, objective_id, recover=False)
        if current.operator_state == "stopped":
            raise DyroError("已停止的 Objective 不能调整 scope")
        updated = _with_targets(current.objective, (*current.objective.targets, task_id))
        scope, contracts = _scope_for(config, updated)
        candidate = _make_record(updated, revision=current.revision + 1, operator_state=current.operator_state, scope=scope, contracts=contracts, event_seq=current.event_seq, event_sha256=current.event_sha256)
        _assert_ownership_available(config, candidate, exclude_id=current.objective.id, recover=False)
        _assert_no_inflight_tasks(config, candidate)
        return candidate
    with _objective_mutation_lock(config):
        with open_objective_directory(config, objective_id) as directory:
            current = _read_stored(config, objective_id, directory=directory)
            if current.operator_state == "stopped":
                raise DyroError("已停止的 Objective 不能调整 scope")
            updated = _with_targets(current.objective, (*current.objective.targets, task_id))
            return _persist_revision(
                config,
                current,
                updated,
                directory=directory,
                operator_state=current.operator_state,
                event_name="scope_added",
            )


def remove_objective_target(config: Config, objective_id: str, task_id: str, *, dry_run: bool = False) -> StoredObjective:
    if dry_run:
        current = get_objective(config, objective_id, recover=False)
        if current.operator_state == "stopped":
            raise DyroError("已停止的 Objective 不能调整 scope")
        if task_id not in current.objective.targets:
            raise DyroError(f"Objective target 不存在：{task_id}")
        updated = _with_targets(current.objective, (item for item in current.objective.targets if item != task_id))
        scope, contracts = _scope_for(config, updated)
        candidate = _make_record(updated, revision=current.revision + 1, operator_state=current.operator_state, scope=scope, contracts=contracts, event_seq=current.event_seq, event_sha256=current.event_sha256)
        _assert_ownership_available(config, candidate, exclude_id=current.objective.id, recover=False)
        _assert_no_inflight_tasks(config, candidate)
        return candidate
    with _objective_mutation_lock(config):
        with open_objective_directory(config, objective_id) as directory:
            current = _read_stored(config, objective_id, directory=directory)
            if current.operator_state == "stopped":
                raise DyroError("已停止的 Objective 不能调整 scope")
            if task_id not in current.objective.targets:
                raise DyroError(f"Objective target 不存在：{task_id}")
            updated = _with_targets(current.objective, (item for item in current.objective.targets if item != task_id))
            return _persist_revision(
                config,
                current,
                updated,
                directory=directory,
                operator_state=current.operator_state,
                event_name="scope_removed",
            )


def _transition_objective(config: Config, objective_id: str, next_state: str, *, dry_run: bool = False) -> StoredObjective:
    def transition(directory: ObjectiveDirectory) -> StoredObjective:
        current = _read_stored(config, objective_id, directory=directory)
        if next_state == "active" and current.operator_state == "stopped":
            raise DyroError("已停止的 Objective 不能恢复；请创建新的 Objective")
        if next_state == "active" and drifted_objective(config, current):
            raise DyroError("Objective contract 或 scope 已漂移；请先运行 objective reconcile")
        if current.operator_state == next_state:
            return current
        candidate = _make_record(
            current.objective,
            revision=current.revision,
            operator_state=next_state,
            scope=current.scope,
            contracts=current.task_contract_sha256,
            event_seq=current.event_seq,
            event_sha256=current.event_sha256,
        )
        if next_state == "active":
            _assert_ownership_available(config, candidate, exclude_id=current.objective.id)
        _assert_no_inflight_tasks(config, candidate)
        return _commit_event(directory, candidate, f"state_{next_state}")

    if dry_run:
        current = get_objective(config, objective_id, recover=False)
        if next_state == "active" and current.operator_state == "stopped":
            raise DyroError("已停止的 Objective 不能恢复；请创建新的 Objective")
        if next_state == "active" and drifted_objective(config, current):
            raise DyroError("Objective contract 或 scope 已漂移；请先运行 objective reconcile")
        if current.operator_state == next_state:
            return current
        candidate = _make_record(
            current.objective,
            revision=current.revision,
            operator_state=next_state,
            scope=current.scope,
            contracts=current.task_contract_sha256,
            event_seq=current.event_seq,
            event_sha256=current.event_sha256,
        )
        if next_state == "active":
            _assert_ownership_available(config, candidate, exclude_id=current.objective.id, recover=False)
        _assert_no_inflight_tasks(config, candidate)
        return candidate
    with _objective_mutation_lock(config):
        with open_objective_directory(config, objective_id) as directory:
            return transition(directory)


def pause_objective(config: Config, objective_id: str, *, dry_run: bool = False) -> StoredObjective:
    return _transition_objective(config, objective_id, "paused", dry_run=dry_run)


def resume_objective(config: Config, objective_id: str, *, dry_run: bool = False) -> StoredObjective:
    return _transition_objective(config, objective_id, "active", dry_run=dry_run)


def stop_objective(config: Config, objective_id: str, *, dry_run: bool = False) -> StoredObjective:
    return _transition_objective(config, objective_id, "stopped", dry_run=dry_run)


def drifted_objective(config: Config, record: StoredObjective) -> bool:
    try:
        scope, contracts = _scope_for(config, record.objective)
    except (DyroError, ValidationError):
        return True
    return scope != record.scope or contracts != record.task_contract_sha256


def derive_objective_result(config: Config, record: StoredObjective) -> str:
    """Derive, never persist, completion or repair state from current facts."""
    if drifted_objective(config, record):
        return "repair_required"
    tasks = {task.id: task for task in list_tasks(config)}
    if all(task_status(config, tasks[target]) == "done" for target in record.objective.targets):
        from ..tasks import _assert_dependency_integrated

        try:
            for target in record.objective.targets:
                _assert_dependency_integrated(config, tasks[target])
        except (DyroError, ValidationError):
            return "incomplete"
        return "complete"
    return "incomplete"


def assert_legacy_scheduler_allowed(config: Config, task_ids: Iterable[str]) -> None:
    """Fail closed until Objective actions own old loop/daemon delegation."""
    requested = {validate_id(task_id, "任务 ID") for task_id in task_ids}
    if not requested:
        return
    for record in list_objectives(config):
        if record.owns_mutation_scope and requested & set(record.scope):
            raise DyroError(
                f"任务位于 active Objective {record.objective.id} 的 mutation scope；"
                "请使用 plan-only Objective 命令，旧 task loop/daemon 不能绕过 ownership"
            )


@contextmanager
def legacy_scheduler_reservation(config: Config, task_ids: Iterable[str]) -> Iterator[None]:
    """Hold the Objective fence through one automated Task reservation.

    Manual ``task run`` remains an explicit operator action.  Old loop/daemon
    scheduling enters this context immediately before reserving a Task, closing
    the check-then-launch race with Objective start and scope mutations.
    """
    requested = tuple(validate_id(task_id, "任务 ID") for task_id in task_ids)
    root = _objective_root(config, create=False)
    if not root.exists():
        yield
        return
    with _objective_lock(config, create=False):
        assert_legacy_scheduler_allowed(config, requested)
        yield

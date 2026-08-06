"""Bounded, typed and non-executable Objective plans for Agent Bridge."""

from __future__ import annotations

from contextlib import contextmanager, ExitStack
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import time
import tomllib
from typing import Callable, Iterable, Iterator, Mapping

from jsonschema import Draft202012Validator

from ..canonical import canonical_json_bytes, canonical_json_text
from ..config import Config, validate_id
from ..continuation.attention import build_attention_projection
from ..continuation.engine import build_scheduler_tick
from ..continuation.models import PlannedAction
from ..continuation.objective_storage import StoredObjective
from ..continuation.planner import (
    build_continuation_plan,
    build_scheduler_projection,
)
from ..continuation.snapshot import (
    SchedulerSnapshot,
    SchedulerTaskSnapshot,
    build_scheduler_snapshot_from_facts,
)
from ..continuation.store import get_objective
from ..errors import DyroError, ValidationError
from ..graph import TaskGraph, validate_task_graph
from ..read_limits import ObservationLimits, ReadBudget, ReadLimitCode, ReadLimitError
from ..tasks import Task, load_task_planning_bounded
from ..workspace import line_repository_path, load_line_bounded, repository_path
from .constants import PLAN_OPERATION_REVISIONS, PLAN_TTL_SECONDS, PROTOCOL_MAJOR
from .git_read import (
    GitAncestryObservation,
    GitReadError,
    GitReadFailure,
    inspect_ancestry_readonly,
)
from .models import ErrorCode, config_revision, workspace_identity
from .observations import (
    BridgeObservationError,
    bridge_error,
    map_limit_error,
    read_bounded_line_facts,
    resolve_bounded_workspace,
    scan_bounded_records,
)
from .schemas import get_operation_schema


PLAN_OPERATIONS = tuple(sorted(PLAN_OPERATION_REVISIONS))
_MAX_PLAN_ITEMS = 100
_MAX_GIT_PROCESSES = 100
_ATTENTION_PRIORITY = {
    "repair_required": 0,
    "needs_user": 1,
    "ready": 2,
    "paused": 3,
    "waiting": 4,
}
_KNOWN_FACT_KEYS = frozenset(
    {
        "active_task_ids",
        "conflict_group",
        "decision_ids",
        "dependency_id",
        "dependency_status",
        "has_conflict",
        "has_open_decision",
        "has_pending_dependency",
        "objective_revision",
        "operation",
        "operator_state",
        "requested_mode",
        "status",
    }
)


@dataclass(frozen=True)
class BridgePlan:
    """Immutable canonical plan document with a self-verifying digest."""

    operation: str
    canonical_json: str

    def __post_init__(self) -> None:
        if self.operation not in PLAN_OPERATIONS:
            raise ValidationError("Bridge plan operation 无效")
        try:
            payload = json.loads(self.canonical_json)
        except (TypeError, ValueError) as exc:
            raise ValidationError("Bridge plan canonical JSON 无效") from exc
        if not isinstance(payload, dict) or payload.get("operation") != self.operation:
            raise ValidationError("Bridge plan canonical operation 不匹配")
        if canonical_json_text(payload) != self.canonical_json:
            raise ValidationError("Bridge plan 必须使用 RFC 8785 canonical JSON")
        if payload.get("plan_sha256") != compute_plan_sha256(payload):
            raise ValidationError("Bridge plan SHA-256 与内容不匹配")
        errors = tuple(
            Draft202012Validator(
                get_operation_schema(self.operation).output_schema()
            ).iter_errors(payload)
        )
        if errors:
            raise ValidationError("Bridge plan 不符合 operation output schema")

    def as_dict(self) -> dict[str, object]:
        return json.loads(self.canonical_json)


@dataclass(frozen=True)
class _BoundGitMetadata:
    git_dir_fd: int
    common_dir_fd: int
    object_dir_fd: int


def compute_plan_sha256(payload: Mapping[str, object]) -> str:
    """Hash every visible plan field except the digest itself."""
    if not isinstance(payload, Mapping):
        raise ValidationError("Bridge plan digest 输入必须是 mapping")
    body = dict(payload)
    body.pop("plan_sha256", None)
    return "sha256:" + hashlib.sha256(canonical_json_bytes(body)).hexdigest()


def _utc(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValidationError(f"{label} 必须是带时区 datetime")
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _utc(value, "Bridge plan timestamp").isoformat().replace("+00:00", "Z")


def _bounded(items: Iterable[object], label: str) -> tuple[object, ...]:
    frozen = tuple(items)
    if len(frozen) > _MAX_PLAN_ITEMS:
        raise ReadLimitError(
            ReadLimitCode.RECORD_LIMIT_EXCEEDED,
            f"{label} exceeds the Bridge plan record limit",
        )
    return frozen


def _resource_tokens(
    tasks: tuple[Task, ...],
    *,
    value: Callable[[Task], str],
    prefix: str,
) -> dict[str, str | None]:
    groups: dict[str, list[str]] = {}
    for task in tasks:
        raw = value(task)
        if raw:
            groups.setdefault(raw, []).append(task.id)
    ordered = sorted(tuple(sorted(ids)) for ids in groups.values())
    by_members = {
        members: f"{prefix}-slot:{index + 1}" for index, members in enumerate(ordered)
    }
    result: dict[str, str | None] = {task.id: None for task in tasks}
    for ids in groups.values():
        members = tuple(sorted(ids))
        for task_id in members:
            result[task_id] = by_members[members]
    return result


def _agent_resource_tokens(
    tasks: tuple[Task, ...],
) -> tuple[dict[str, str], dict[str, str]]:
    uses: dict[str, list[tuple[str, str]]] = {}
    for task in tasks:
        uses.setdefault(task.executor, []).append((task.id, "executor"))
        uses.setdefault(task.reviewer, []).append((task.id, "reviewer"))
    ordered = sorted(tuple(sorted(members)) for members in uses.values())
    by_members = {
        members: f"agent-slot:{index + 1}" for index, members in enumerate(ordered)
    }
    execution: dict[str, str] = {}
    review: dict[str, str] = {}
    for members in uses.values():
        frozen = tuple(sorted(members))
        token = by_members[frozen]
        for task_id, role in frozen:
            if role == "executor":
                execution[task_id] = token
            else:
                review[task_id] = token
    return execution, review


def _read_decisions(config: Config, budget: ReadBudget) -> tuple[tuple[str, str], ...]:
    try:
        content = budget.read_regular_bytes_at(
            root=config.root,
            directory=config.decisions_file.parent,
            name=config.decisions_file.name,
            maximum_bytes=budget.limits.line_manifest_bytes,
            label="decision facts",
        )
    except FileNotFoundError:
        return ()
    try:
        raw = tomllib.loads(content.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError, RecursionError) as exc:
        raise ValidationError("Decision facts are invalid") from exc
    entries = raw.get("decisions", {})
    if not isinstance(entries, dict) or len(entries) > _MAX_PLAN_ITEMS:
        raise ValidationError("Decision facts exceed the authoritative plan surface")
    result: list[tuple[str, str]] = []
    for decision_id, value in entries.items():
        validate_id(decision_id, "decision ID")
        if not isinstance(value, dict) or value.get("status", "open") not in {
            "open",
            "resolved",
        }:
            raise ValidationError("Decision status is invalid")
        result.append((decision_id, str(value.get("status", "open"))))
    return tuple(sorted(result))


def _claim_active(
    config: Config, task: Task, budget: ReadBudget, *, observed_at: datetime
) -> bool:
    try:
        content = budget.read_regular_bytes_at(
            root=config.root,
            directory=task.directory,
            name="claim.json",
            maximum_bytes=budget.limits.task_manifest_bytes,
            label="task claim",
        )
    except FileNotFoundError:
        return False
    try:
        payload = json.loads(content)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValidationError("Task claim is invalid") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("task_id") != task.id
        or not isinstance(payload.get("runner"), str)
        or not payload.get("runner")
    ):
        raise ValidationError("Task claim is invalid")
    expires_at = payload.get("lease_expires_at")
    if expires_at is None:
        return True
    if not isinstance(expires_at, str):
        raise ValidationError("Task claim expiry is invalid")
    try:
        expires = datetime.fromisoformat(expires_at)
    except ValueError as exc:
        raise ValidationError("Task claim expiry is invalid") from exc
    if expires.tzinfo is None:
        raise ValidationError("Task claim expiry must include a timezone")
    return _utc(expires, "Task claim expiry") > observed_at


def _load_line_for_task(config: Config, task: Task, budget: ReadBudget):
    matches = []
    for parent in (config.lines_state_dir, config.hotfixes_state_dir):
        path = parent / f"{task.line}.toml"
        try:
            matches.append(load_line_bounded(path, budget, workspace_root=config.root))
        except FileNotFoundError:
            continue
    if len(matches) != 1:
        raise ValidationError("Task line cannot be resolved uniquely")
    return matches[0]


def _metadata_path(root: Path, base: Path, raw: str, label: str) -> Path:
    if not raw or "\x00" in raw or "\n" in raw or "\r" in raw:
        raise ValidationError(f"{label} is invalid")
    value = Path(raw)
    candidate = value if value.is_absolute() else base / value
    normalized = Path(os.path.abspath(candidate))
    try:
        normalized.relative_to(root.absolute())
    except ValueError as exc:
        raise ValidationError(f"{label} escapes the workspace") from exc
    return normalized


def _metadata_line(content: bytes, label: str) -> str:
    try:
        lines = content.decode("utf-8").splitlines()
    except UnicodeError as exc:
        raise ValidationError(f"{label} is invalid") from exc
    if len(lines) != 1 or not lines[0].strip():
        raise ValidationError(f"{label} is invalid")
    return lines[0].strip()


def _validate_local_git_config(content: bytes) -> None:
    if content.startswith(b"\xef\xbb\xbf") or b"\x00" in content:
        raise ValidationError("Git config encoding is unavailable to Bridge")
    try:
        lines = content.decode("utf-8").splitlines()
    except UnicodeError as exc:
        raise ValidationError("Git config is invalid") from exc
    section = ""
    for line in lines:
        stripped = line.strip(" \t\v\f\r")
        normalized = "".join(
            character for character in stripped.lower() if character not in " \t\v\f\r"
        )
        if not normalized or normalized.startswith(("#", ";")):
            continue
        if stripped.endswith("\\"):
            raise ValidationError("Git config continuations are unavailable to Bridge")
        if normalized.startswith("[include") or normalized.startswith("include.path="):
            raise ValidationError("Git config includes are unavailable to Bridge")
        if normalized.startswith("["):
            closing = normalized.find("]")
            if closing < 0:
                raise ValidationError("Git config section is invalid")
            tail = normalized[closing + 1 :]
            if tail and not tail.startswith(("#", ";")):
                raise ValidationError("Git config section is invalid")
            section = normalized[1:closing].split('"', 1)[0]
            if section == "extensions":
                raise ValidationError(
                    "Git repository extensions are unavailable to Bridge"
                )
            continue
        if section == "core" and normalized.startswith("repositoryformatversion"):
            if normalized not in {
                "repositoryformatversion=0",
                "repositoryformatversion0",
            }:
                raise ValidationError("Git repository format is unavailable to Bridge")


@contextmanager
def _bind_git_metadata(
    config: Config,
    destination: Path,
    repository_fd: int,
    budget: ReadBudget,
) -> Iterator[_BoundGitMetadata]:
    try:
        dot_git = os.stat(".git", dir_fd=repository_fd, follow_symlinks=False)
    except OSError as exc:
        raise ValidationError("Git metadata is unavailable") from exc
    if stat.S_ISLNK(dot_git.st_mode):
        raise ValidationError("Git metadata cannot be a symlink")
    if stat.S_ISDIR(dot_git.st_mode):
        git_dir = destination / ".git"
    elif stat.S_ISREG(dot_git.st_mode):
        content = budget.read_regular_bytes_at(
            root=config.root,
            directory=destination,
            name=".git",
            maximum_bytes=4096,
            label="Git directory reference",
        )
        line = _metadata_line(content, "Git directory reference")
        if not line.lower().startswith("gitdir:"):
            raise ValidationError("Git directory reference is invalid")
        git_dir = _metadata_path(
            config.root, destination, line.split(":", 1)[1].strip(), "Git directory"
        )
    else:
        raise ValidationError("Git metadata is not a regular file or directory")
    with ExitStack() as stack:
        git_fd = stack.enter_context(
            budget.open_safe_directory_chain(config.root, git_dir)
        )
        if git_fd is None:
            raise ValidationError("Git directory is unavailable")
        try:
            common_content = budget.read_regular_bytes_at(
                root=config.root,
                directory=git_dir,
                name="commondir",
                maximum_bytes=4096,
                label="Git common directory reference",
            )
        except FileNotFoundError:
            common_dir = git_dir
        else:
            common_dir = _metadata_path(
                config.root,
                git_dir,
                _metadata_line(common_content, "Git common directory reference"),
                "Git common directory",
            )
        common_fd = stack.enter_context(
            budget.open_safe_directory_chain(config.root, common_dir)
        )
        if common_fd is None:
            raise ValidationError("Git common directory is unavailable")
        objects_fd = stack.enter_context(
            budget.open_safe_directory_chain(config.root, common_dir / "objects")
        )
        if objects_fd is None:
            raise ValidationError("Git object directory is unavailable")
        for directory, name in (
            (common_dir, "config"),
            (git_dir, "config.worktree"),
        ):
            try:
                local_config = budget.read_regular_bytes_at(
                    root=config.root,
                    directory=directory,
                    name=name,
                    maximum_bytes=budget.limits.line_manifest_bytes,
                    label="Git local config",
                )
            except FileNotFoundError:
                if name == "config":
                    raise ValidationError("Git config is unavailable") from None
            else:
                _validate_local_git_config(local_config)
        for alternate_name in ("alternates", "http-alternates"):
            try:
                budget.read_regular_bytes_at(
                    root=config.root,
                    directory=common_dir / "objects/info",
                    name=alternate_name,
                    maximum_bytes=4096,
                    label="Git object alternate",
                )
            except FileNotFoundError:
                continue
            raise ValidationError("Git object alternates are unavailable to Bridge")
        yield _BoundGitMetadata(
            git_dir_fd=git_fd,
            common_dir_fd=common_fd,
            object_dir_fd=objects_fd,
        )


def _task_heads(
    config: Config, task: Task, budget: ReadBudget
) -> dict[str, str] | None:
    try:
        content = budget.read_regular_bytes_at(
            root=config.root,
            directory=task.directory,
            name="task-heads.json",
            maximum_bytes=budget.limits.task_manifest_bytes,
            label="task head evidence",
        )
    except FileNotFoundError:
        return None
    try:
        payload = json.loads(content)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValidationError("Task head evidence is invalid") from exc
    repositories = payload.get("repositories") if isinstance(payload, dict) else None
    expected_branch = f"{config.policy.task_branch_prefix}{task.id}"
    if (
        not isinstance(payload, dict)
        or set(payload)
        != {"schema_version", "task_id", "line", "branch", "repositories"}
        or payload.get("schema_version") != 1
        or payload.get("task_id") != task.id
        or payload.get("line") != task.line
        or payload.get("branch") != expected_branch
        or not isinstance(repositories, dict)
        or set(repositories) != set(task.repositories)
    ):
        raise ValidationError("Task head evidence is invalid")
    result: dict[str, str] = {}
    for repository_id in task.repositories:
        head = repositories.get(repository_id)
        if (
            not isinstance(head, str)
            or len(head) != 40
            or any(character not in "0123456789abcdef" for character in head.lower())
        ):
            raise ValidationError("Task head evidence contains an invalid commit")
        result[repository_id] = head.lower()
    return result


def _integration_state(
    config: Config,
    task: Task,
    status: str,
    *,
    required: bool,
    budget: ReadBudget,
    git_reader: Callable[..., GitAncestryObservation],
) -> tuple[str, tuple[dict[str, object], ...]]:
    if status != "done" or not required:
        return "not_required", ()
    heads = _task_heads(config, task, budget)
    if heads is None:
        return "pending", ()
    line = _load_line_for_task(config, task, budget)
    checks: list[dict[str, object]] = []
    integrated = True
    for repository_id, head in sorted(heads.items()):
        destination = (
            repository_path(config, repository_id)
            if line.storage_for(repository_id) == "anchor-reference"
            else line_repository_path(config, line, repository_id)
        )
        with budget.open_safe_directory_chain(config.root, destination) as directory_fd:
            if directory_fd is None:
                raise ValidationError("Integration repository is unavailable")
            with _bind_git_metadata(
                config, destination, directory_fd, budget
            ) as metadata:
                timeout = min(3.0, budget.remaining_seconds())
                observation = git_reader(
                    destination,
                    head,
                    directory_fd=directory_fd,
                    git_dir_fd=metadata.git_dir_fd,
                    common_dir_fd=metadata.common_dir_fd,
                    object_dir_fd=metadata.object_dir_fd,
                    timeout_seconds=timeout,
                )
            if not isinstance(observation, GitAncestryObservation):
                raise ValidationError("Bridge Git reader returned an invalid result")
            checks.append(
                {
                    "repository_id": repository_id,
                    "task_head_sha256": observation.task_head_sha256,
                    "destination_head_sha256": observation.destination_head_sha256,
                    "is_ancestor": observation.is_ancestor,
                }
            )
            integrated = integrated and observation.is_ancestor
        # Re-open the exact path after Git exits. The budget has already bound
        # its device/inode identity, so a persistent path replacement fails
        # closed instead of being accepted as the sampled repository.
        with budget.open_safe_directory_chain(config.root, destination) as directory_fd:
            if directory_fd is None:
                raise ValidationError("Integration repository is unavailable")
            with _bind_git_metadata(config, destination, directory_fd, budget):
                pass
        budget.check_deadline()
    return ("integrated" if integrated else "pending"), tuple(checks)


def _sample_snapshot(
    *,
    config: Config,
    record: StoredObjective,
    budget: ReadBudget,
    observed_at: datetime,
    git_reader: Callable[..., GitAncestryObservation],
) -> tuple[SchedulerSnapshot, dict[str, object]]:
    _lines, line_ids, failures, lines_truncated = read_bounded_line_facts(
        config, budget
    )
    if failures:
        failure_codes = {item.code for item in failures}
        for code in (
            ErrorCode.HOST_READ_PERMISSION_REQUIRED,
            ErrorCode.OBSERVATION_DEADLINE_EXCEEDED,
            ErrorCode.RESOURCE_LIMIT_EXCEEDED,
            ErrorCode.RECORD_INVALID,
        ):
            if code.value in failure_codes:
                raise bridge_error(code)
        raise bridge_error(ErrorCode.RECORD_INVALID)
    if lines_truncated:
        raise bridge_error(ErrorCode.RESOURCE_LIMIT_EXCEEDED)
    scan = scan_bounded_records(
        config.task_specs_dir,
        workspace_root=config.root,
        maximum=min(budget.limits.task_records, _MAX_PLAN_ITEMS),
        directories=True,
        budget=budget,
    )
    if scan.invalid_entries:
        raise ValidationError("Task facts contain invalid directory entries")
    if scan.truncated:
        raise ReadLimitError(
            ReadLimitCode.RECORD_LIMIT_EXCEEDED,
            "Task facts are incomplete",
        )
    loaded: list[tuple[Task, str, str]] = []
    for task_id in scan.names:
        loaded.append(
            load_task_planning_bounded(
                config,
                task_id,
                budget,
                known_line_ids=line_ids,
            )
        )
    tasks = tuple(item[0] for item in loaded)
    _bounded(record.scope, "objective scope")
    _bounded(record.objective.targets, "objective targets")
    for task in tasks:
        _bounded(task.repositories, f"{task.id} repositories")
        _bounded(task.depends_on, f"{task.id} dependencies")
        _bounded(task.blocked_on, f"{task.id} blockers")
    statuses = {task.id: status for task, status, _digest in loaded}
    contracts = {task.id: digest for task, _status, digest in loaded}
    decisions = _read_decisions(config, budget)
    graph = TaskGraph(
        line=None,
        tasks=tasks,
        known_tasks=tasks,
        decisions=dict(decisions),
        execution_mode=config.policy.execution_mode,
    )
    if validate_task_graph(graph):
        raise ValidationError("Task graph facts are invalid")

    required_ids = {
        dependency for task in tasks for dependency in task.depends_on
    } | set(record.objective.targets)
    git_processes = 2 * sum(
        len(task.repositories)
        for task in tasks
        if statuses[task.id] == "done" and task.id in required_ids
    )
    if git_processes > _MAX_GIT_PROCESSES:
        raise ReadLimitError(
            ReadLimitCode.RECORD_LIMIT_EXCEEDED,
            "Git inspection process budget exceeded",
        )
    external_claims = {
        task.id: (
            config.policy.execution_mode == "external"
            and statuses[task.id] == "assigned"
            and _claim_active(config, task, budget, observed_at=observed_at)
        )
        for task in tasks
    }
    integration_results = {
        task.id: _integration_state(
            config,
            task,
            statuses[task.id],
            required=task.id in required_ids,
            budget=budget,
            git_reader=git_reader,
        )
        for task in tasks
    }
    integration = {
        task_id: result[0] for task_id, result in integration_results.items()
    }
    integration_checks = {
        task_id: result[1] for task_id, result in integration_results.items()
    }
    execution_slots, review_slots = _agent_resource_tokens(tasks)
    conflict_slots = _resource_tokens(
        tasks, value=lambda task: task.conflict_group, prefix="conflict"
    )
    merge_slots = _resource_tokens(tasks, value=lambda task: task.line, prefix="line")
    safe_tasks = tuple(
        replace(
            task,
            title=task.id,
            executor=execution_slots[task.id] or "agent-slot:0",
            reviewer=review_slots[task.id] or "agent-slot:0",
            repositories=(),
            conflict_group=conflict_slots[task.id] or "",
            directory=Path("/"),
        )
        for task in tasks
    )
    by_id = {task.id: task for task in safe_tasks}
    scheduler_tasks = tuple(
        SchedulerTaskSnapshot(
            task=by_id[task.id],
            status=statuses[task.id],
            external_claim_active=external_claims[task.id],
            integration_state=integration[task.id],
            contract_sha256=contracts[task.id],
        )
        for task in tasks
    )
    snapshot = build_scheduler_snapshot_from_facts(
        tasks=scheduler_tasks,
        decisions=decisions,
        execution_mode=config.policy.execution_mode,
        candidate_ids=tuple(task.id for task in tasks),
        observed_at=observed_at,
        objective=record,
    )
    active_by_conflict: dict[str, tuple[str, ...]] = {}
    for task in tasks:
        slot = conflict_slots[task.id]
        if slot is None:
            continue
        active_by_conflict.setdefault(slot, tuple())
    for slot in tuple(active_by_conflict):
        active_by_conflict[slot] = tuple(
            sorted(
                task.id
                for task in tasks
                if conflict_slots[task.id] == slot
                and (
                    statuses[task.id] == "in_progress"
                    or (statuses[task.id] == "assigned" and external_claims[task.id])
                )
            )
        )
    task_facts = [
        {
            "id": task.id,
            "line_id": task.line,
            "contract_sha256": f"sha256:{contracts[task.id]}",
            "status": statuses[task.id],
            "depends_on": list(sorted(task.depends_on)),
            "blocked_on": list(sorted(task.blocked_on)),
            "external_claim_active": external_claims[task.id],
            "integration_state": integration[task.id],
            "integration_checks": list(integration_checks[task.id]),
            "active_conflict_task_ids": list(
                active_by_conflict.get(conflict_slots[task.id] or "", ())
            ),
            "conflict_slot": conflict_slots[task.id],
            "execution_slot": execution_slots[task.id],
            "review_slot": review_slots[task.id],
            "merge_slot": merge_slots[task.id],
        }
        for task in tasks
    ]
    read_set: dict[str, object] = {
        "observed_at": _timestamp(observed_at),
        "integration_inspection": "complete",
        "execution_mode": config.policy.execution_mode,
        "objective": _objective_facts(record),
        "tasks": task_facts,
        "decisions": [
            {"id": decision_id, "status": status} for decision_id, status in decisions
        ],
    }
    budget.check_root_identity(config.root)
    budget.check_deadline()
    return snapshot, read_set


def _objective_facts(record: StoredObjective) -> dict[str, object]:
    budget = record.objective.budget
    return {
        "id": record.objective.id,
        "revision": record.revision,
        "event_sequence": record.event_seq,
        "contract_sha256": f"sha256:{record.contract_sha256}",
        "scope_sha256": f"sha256:{record.scope_sha256}",
        "event_sha256": f"sha256:{record.event_sha256}",
        "operator_state": record.operator_state,
        "completion_rule": record.objective.completion.value,
        "requested_mode": record.objective.requested_mode.value,
        "operations": sorted(item.value for item in record.objective.operations),
        "scope": list(sorted(record.scope)),
        "targets": list(sorted(record.objective.targets)),
        "budget": {
            "deadline": _timestamp(budget.deadline),
            "max_actions": budget.max_actions,
            "max_attempts_per_task": budget.max_attempts_per_task,
            "max_failures": budget.max_failures,
            "max_no_progress_cycles": budget.max_no_progress_cycles,
            "max_parallel": budget.max_parallel,
        },
    }


def _predicates(
    facts: Iterable[tuple[str, str]],
    *,
    action_kind: str | None = None,
) -> dict[str, object]:
    pairs = tuple(facts)
    if len({key for key, _value in pairs}) != len(pairs):
        raise ValidationError("Planner action facts contain duplicate keys")
    unknown = {key for key, _value in pairs} - _KNOWN_FACT_KEYS
    if unknown:
        raise ValidationError("Planner action facts contain unreviewed keys")
    source = dict(pairs)
    result: dict[str, object] = {}
    if action_kind is not None:
        result["action_kind"] = action_kind
    if "status" in source:
        result["observed_status"] = source["status"]
    related = []
    if "dependency_id" in source:
        related.append(validate_id(source["dependency_id"], "related subject ID"))
    if "active_task_ids" in source:
        related.extend(
            validate_id(item, "related subject ID")
            for item in source["active_task_ids"].split(",")
            if item
        )
    if related:
        result["related_subject_ids"] = sorted(set(related))
    if "decision_ids" in source:
        result["has_open_decision"] = True
    if source.get("has_open_decision") == "true":
        result["has_open_decision"] = True
    if "conflict_group" in source:
        result["has_conflict"] = True
    if source.get("has_conflict") == "true":
        result["has_conflict"] = True
    if "dependency_status" in source:
        result["observed_status"] = source["dependency_status"]
        result["has_pending_dependency"] = True
    if source.get("has_pending_dependency") == "true":
        result["has_pending_dependency"] = True
    for key in ("operation", "operator_state", "requested_mode"):
        if key in source:
            result[key] = source[key]
    return result


def _action(action: PlannedAction) -> dict[str, object]:
    validate_id(action.subject_id, "plan action subject")
    return {
        "kind": action.kind.value,
        "subject_id": action.subject_id,
        "reason": action.reason.value,
        "predicates": _predicates(action.facts),
    }


def _attention(item: object) -> dict[str, object]:
    kind = item.kind.value
    return {
        "id": item.id,
        "kind": kind,
        "subject_id": item.subject_id,
        "reason": item.reason.value,
        "priority": _ATTENTION_PRIORITY[kind],
        "predicates": _predicates(
            item.facts,
            action_kind=None if item.action_kind is None else item.action_kind.value,
        ),
    }


def _node_id(value: str) -> str:
    return "node-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def _ensure_disjoint(projection: Mapping[str, object]) -> None:
    blocked = {
        item["subject_id"]
        for item in projection.get("blocked", [])  # type: ignore[union-attr]
    }
    for field in ("selected_actions", "tick_wave"):
        overlap = blocked & {
            item["subject_id"]
            for item in projection.get(field, [])  # type: ignore[union-attr]
        }
        if overlap:
            raise ValidationError("Bridge plan contains contradictory subjects")


def _projection(
    operation: str,
    *,
    snapshot: SchedulerSnapshot,
    record: StoredObjective,
    read_set: dict[str, object],
) -> dict[str, object]:
    plan = build_continuation_plan(snapshot)
    graph = build_scheduler_projection(snapshot, plan)
    attention = build_attention_projection(
        snapshot,
        plan,
        graph,
        budget=record.objective.budget,
    )
    selected = [_action(item) for item in plan.selected_actions]
    blocked = [_action(item) for item in plan.blocked]
    attention_items = [_attention(item) for item in attention.items]
    if operation == "objective.plan":
        result = {
            "completion": plan.completion.value,
            "selected_actions": selected,
            "blocked": blocked,
            "attention": attention_items,
        }
    elif operation == "objective.explain":
        reasons = sorted(
            {
                item.reason.value
                for item in (*plan.selected_actions, *plan.blocked, *attention.items)
            }
        )
        result = {
            "summary_code": plan.completion.value,
            "reasons": reasons,
            "selected_actions": selected,
            "blocked": blocked,
            "attention": attention_items,
        }
    elif operation == "objective.graph":
        identifiers = {node.id: _node_id(node.id) for node in graph.nodes}
        nodes = [
            {"id": identifiers[node.id], "kind": node.kind, "status": node.state}
            for node in graph.nodes
        ]
        edges = [
            {
                "source": identifiers[edge.source],
                "target": identifiers[edge.target],
                "kind": edge.kind,
            }
            for edge in graph.edges
        ]
        result = {"nodes": nodes, "edges": edges, "issues": []}
    elif operation == "objective.tick":
        tick = build_scheduler_tick(
            snapshot,
            plan,
            max_parallel=record.objective.budget.max_parallel,
        )
        read_set["capacity"] = {
            "active_parallel": tick.active_parallel,
            "available_parallel": tick.available_parallel,
            "max_parallel": tick.max_parallel,
        }
        result = {
            "selected_actions": selected,
            "blocked": blocked,
            "attention": attention_items,
            "tick_wave": [_action(item) for item in tick.wave],
            "deferred": [_deferred(item) for item in tick.deferred],
            "non_mutating_actions": [
                _action(item) for item in tick.non_mutating_actions
            ],
        }
    elif operation == "objective.attention":
        next_wake_at = _timestamp(attention.next_wake_at)
        read_set["next_wake_at"] = next_wake_at
        result = {"attention": attention_items, "next_wake_at": next_wake_at}
    else:  # pragma: no cover - guarded by the public entry point
        raise ValidationError("Unknown Bridge plan operation")
    for field, value in result.items():
        if isinstance(value, list):
            _bounded(value, f"{operation}.{field}")
    _ensure_disjoint(result)
    return result


def _deferred(item: object) -> dict[str, object]:
    predicates: dict[str, object] = {}
    facts = dict(item.facts)
    if item.reason.value == "RESOURCE_CONFLICT":
        predicates = {
            "resource_class": facts["resource_class"],
            "selected_subject_id": validate_id(
                facts["selected_subject_id"], "selected subject ID"
            ),
        }
    elif item.reason.value == "PARALLEL_CAPACITY":
        predicates = {
            "max_parallel": int(facts["max_parallel"]),
            "active_parallel": int(facts["active_parallel"]),
            "available_parallel": int(facts["available_parallel"]),
        }
    else:
        raise ValidationError("Scheduler deferral reason is not allowlisted")
    return {
        "action": _action(item.action),
        "reason": item.reason.value,
        "predicates": predicates,
    }


def _expiration(
    observed_at: datetime, record: StoredObjective
) -> tuple[datetime, list[dict[str, str]]]:
    default = observed_at + timedelta(seconds=PLAN_TTL_SECONDS)
    deadline = record.objective.budget.deadline
    if deadline is None or _utc(deadline, "Objective deadline") >= default:
        return default, []
    return max(observed_at, _utc(deadline, "Objective deadline")), [
        {"code": "PLAN_EXPIRES_AT_OBJECTIVE_DEADLINE"}
    ]


def build_objective_bridge_plan(
    *,
    operation: str,
    objective_id: str,
    workspace: str | None,
    start: str | Path | None,
    cwd: Path,
    limits: ObservationLimits | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    monotonic: Callable[[], float] = time.monotonic,
    git_reader: Callable[..., GitAncestryObservation] = inspect_ancestry_readonly,
) -> BridgePlan:
    """Build one authoritative PLAN response without mutation or recovery."""
    if operation not in PLAN_OPERATIONS:
        raise bridge_error(ErrorCode.OPERATION_UNKNOWN)
    try:
        validate_id(objective_id, "objective_id")
    except ValidationError:
        raise bridge_error(ErrorCode.SCHEMA_VALIDATION_FAILED) from None
    try:
        budget = ReadBudget(limits or ObservationLimits(), monotonic=monotonic)
    except (TypeError, ValueError, ValidationError):
        raise bridge_error(ErrorCode.INTERNAL_ERROR) from None
    resolved = resolve_bounded_workspace(
        workspace=workspace, start=start, cwd=cwd, budget=budget
    )
    try:
        observed_at = _utc(clock(), "Bridge plan clock")
    except (TypeError, ValueError, ValidationError):
        raise bridge_error(ErrorCode.INTERNAL_ERROR) from None
    try:
        record = get_objective(
            resolved.profile.config,
            objective_id,
            recover=False,
            read_budget=budget,
        )
        snapshot, read_set = _sample_snapshot(
            config=resolved.profile.config,
            record=record,
            budget=budget,
            observed_at=observed_at,
            git_reader=git_reader,
        )
        projection = _projection(
            operation,
            snapshot=snapshot,
            record=record,
            read_set=read_set,
        )
        expires_at, warnings = _expiration(observed_at, record)
        body: dict[str, object] = {
            "executable": False,
            "authorization": "none",
            "protocol_major": PROTOCOL_MAJOR,
            "operation": operation,
            "operation_schema_version": 1,
            "planner_revision": PLAN_OPERATION_REVISIONS[operation],
            "workspace": {
                "id": workspace_identity(
                    resolved.profile.root, resolved.profile.config.name
                ),
                "config_sha256": config_revision(resolved.profile.profile_bytes),
            },
            "normalized_input": {"objective_id": objective_id},
            "read_set": read_set,
            "projection": projection,
            "effects": [],
            "warnings": warnings,
            "maximum_risk": "PLAN",
            "effective_risk": "PLAN",
            "expires_at": _timestamp(expires_at),
        }
        body["plan_sha256"] = compute_plan_sha256(body)
        try:
            result = BridgePlan(operation, canonical_json_text(body))
        except ValidationError:
            raise bridge_error(ErrorCode.INTERNAL_ERROR) from None
        budget.check_root_identity(resolved.profile.root)
        budget.check_deadline()
        return result
    except BridgeObservationError:
        raise
    except ReadLimitError as exc:
        raise map_limit_error(exc) from None
    except GitReadError as exc:
        if exc.code is GitReadFailure.UNAVAILABLE:
            raise bridge_error(ErrorCode.OPERATION_UNAVAILABLE) from None
        if exc.code is GitReadFailure.PERMISSION:
            raise bridge_error(ErrorCode.HOST_READ_PERMISSION_REQUIRED) from None
        raise bridge_error(ErrorCode.OBSERVATION_PARTIAL) from None
    except PermissionError:
        raise bridge_error(ErrorCode.HOST_READ_PERMISSION_REQUIRED) from None
    except (DyroError, OSError, TypeError, UnicodeError, ValueError, ValidationError):
        raise bridge_error(ErrorCode.RECORD_INVALID) from None


def plan_objective(**kwargs: object) -> BridgePlan:
    return build_objective_bridge_plan(operation="objective.plan", **kwargs)


def explain_objective(**kwargs: object) -> BridgePlan:
    return build_objective_bridge_plan(operation="objective.explain", **kwargs)


def graph_objective(**kwargs: object) -> BridgePlan:
    return build_objective_bridge_plan(operation="objective.graph", **kwargs)


def tick_objective(**kwargs: object) -> BridgePlan:
    return build_objective_bridge_plan(operation="objective.tick", **kwargs)


def attention_objective(**kwargs: object) -> BridgePlan:
    return build_objective_bridge_plan(operation="objective.attention", **kwargs)

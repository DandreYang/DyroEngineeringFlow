from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tomllib
import uuid
from typing import Any, Iterable

from .config import (
    Config,
    expand_argv,
    external_security_errors,
    strict_bool,
    validate_id,
)
from .evidence_store import (
    CURRENT_EVIDENCE_FILE,
    EVIDENCE_GENERATIONS_DIR,
    GENERATION_PATTERN,
    MANIFEST_FILE,
    EvidenceGeneration,
    cleanup_evidence_generations,
    list_evidence_generations,
    publish_evidence_generation,
    resolve_evidence_path,
)
from .errors import DyroError, ValidationError
from .process import git, git_read, require_ok, run
from .read_limits import (
    ReadBudget,
    ReadLimitCode,
    ReadLimitError,
    bounded_directory_names,
)
from .provenance import (
    ExecutionAttempt,
    begin_execution_attempt,
    current_execution_attempt_id,
    external_execution_plan,
    finish_execution_attempt,
    import_external_execution_attempt,
    review_binding,
    validate_review_binding,
)
from .state import append_text, atomic_write_bytes, atomic_write_text, exclusive_lock
from .workspace import Line, get_line, line_repository_path, repository_path


STATUSES = (
    "backlog",
    "assigned",
    "in_progress",
    "waiting_answer",
    "review",
    "review_pending_signoff",
    "done",
    "failed",
)
QUALITY_GATE_STATUSES = frozenset({"review", "review_pending_signoff", "done"})
TRANSITIONS = {
    "backlog": {"assigned"},
    "assigned": {"in_progress", "failed"},
    "in_progress": {"waiting_answer", "review", "failed"},
    "waiting_answer": {"assigned", "in_progress", "failed"},
    "review": {"review_pending_signoff", "done", "failed"},
    "review_pending_signoff": {"done", "failed"},
    "failed": {"assigned"},
    "done": set(),
}
RESULT_RE = re.compile(r"^result:\s*(DONE|BLOCKED|QUESTION)\s*$", re.IGNORECASE)
VERDICT_RE = re.compile(r"^verdict:\s*(PASS|FAIL)\s*$", re.IGNORECASE)
RECEIPT_SHA_RE = re.compile(
    r"^receipt_sha256:\s*([0-9a-f]{64})\s*$", re.IGNORECASE | re.MULTILINE
)
TASK_HEADS_SHA_RE = re.compile(
    r"^task_heads_sha256:\s*([0-9a-f]{64})\s*$", re.IGNORECASE | re.MULTILINE
)
GIT_HEAD_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$", re.IGNORECASE)
TASK_HEADS_FILE = "task-heads.json"
REVIEW_IDENTITY_FILE = "review-identity.json"
MERGE_LOCK_TIMEOUT_SECONDS = 1800.0
MAX_TASK_TIMEOUT_MINUTES = 24 * 60
MAX_GATE_TIMEOUT_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class Gate:
    name: str
    argv: tuple[str, ...]
    cwd: str
    timeout_seconds: int = 1800


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    line: str
    risk: str
    executor: str
    reviewer: str
    repositories: tuple[str, ...]
    depends_on: tuple[str, ...] = ()
    blocked_on: tuple[str, ...] = ()
    conflict_group: str = ""
    timeout_minutes: int = 60
    review_timeout_minutes: int = 45
    gates: tuple[Gate, ...] = ()
    merge_auto: bool = False
    merge_push: bool = False
    directory: Path = field(default_factory=Path)


@dataclass(frozen=True)
class MergePlan:
    repository: str
    target: Path
    source_head: str
    original_head: str


def task_dir(config: Config, task_id: str) -> Path:
    validate_id(task_id, "任务 ID")
    path = config.task_specs_dir / task_id
    if not (path / "task.toml").is_file():
        raise DyroError(f"任务不存在或缺少 task.toml：{path}")
    return path


def _strings(raw: Any, label: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or not all(
        isinstance(item, str) and item for item in raw
    ):
        raise ValidationError(f"{label} 必须是字符串数组")
    return tuple(raw)


def _positive_int(raw: Any, label: str, *, maximum: int) -> int:
    if type(raw) is not int or raw < 1 or raw > maximum:
        raise ValidationError(f"{label} 必须是 1 到 {maximum} 之间的整数")
    return raw


def _string(raw: Any, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(raw, str) or (not allow_empty and not raw.strip()):
        qualifier = "字符串" if allow_empty else "非空字符串"
        raise ValidationError(f"{label} 必须是{qualifier}")
    return raw.strip()


def _table(raw: Any, label: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValidationError(f"{label} 必须是表")
    return raw


def _parse_task_content(path: Path, content: bytes) -> Task:
    try:
        raw = tomllib.loads(content.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError, RecursionError) as exc:
        raise ValidationError(f"任务清单格式错误 {path}: {exc}") from exc
    if raw.get("schema_version") != 1:
        raise ValidationError(f"任务清单必须使用 schema_version = 1：{path}")
    task_id = validate_id(_string(raw.get("id"), "任务 ID"), "任务 ID")
    title = _string(raw.get("title"), f"任务 {task_id} title")
    line = validate_id(_string(raw.get("line"), f"任务 {task_id} 开发线"), "任务开发线")
    risk = _string(raw.get("risk", "write"), f"任务 {task_id} risk")
    if risk not in ("read", "write"):
        raise ValidationError(f"任务 {task_id} 的 title 或 risk 无效")
    executor_raw = _table(raw.get("executor", {}), f"任务 {task_id} executor")
    reviewer_raw = _table(raw.get("reviewer", {}), f"任务 {task_id} reviewer")
    executor = _string(executor_raw.get("agent"), f"任务 {task_id} executor.agent")
    reviewer = _string(reviewer_raw.get("agent"), f"任务 {task_id} reviewer.agent")
    repo_entries = raw.get("repositories", [])
    if not isinstance(repo_entries, list) or not repo_entries:
        raise ValidationError(f"任务 {task_id} 至少包含一个 [[repositories]]")
    repositories: list[str] = []
    for entry in repo_entries:
        if not isinstance(entry, dict):
            raise ValidationError(f"任务 {task_id} repositories 结构无效")
        repositories.append(
            validate_id(
                _string(entry.get("id"), f"任务 {task_id} repository id"),
                "任务仓库 id",
            )
        )
    if len(set(repositories)) != len(repositories):
        raise ValidationError(f"任务 {task_id} repositories 不能重复")
    gates_raw = raw.get("gates", [])
    if not isinstance(gates_raw, list):
        raise ValidationError(f"任务 {task_id} gates 必须是表数组")
    gates: list[Gate] = []
    for entry in gates_raw:
        if not isinstance(entry, dict):
            raise ValidationError(f"任务 {task_id} gates 结构无效")
        name = _string(entry.get("name"), f"任务 {task_id} gate name")
        argv = entry.get("argv")
        cwd = _string(entry.get("cwd", "."), f"任务 {task_id} gate {name} cwd")
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(item, str) and item for item in argv)
        ):
            raise ValidationError(f"任务 {task_id} gate 必须包含 name 与 argv 数组")
        cwd_path = Path(cwd)
        if cwd_path.is_absolute() or ".." in cwd_path.parts:
            raise ValidationError(f"任务 {task_id} gate cwd 必须位于 task worktree 内")
        if any(gate.name == name for gate in gates):
            raise ValidationError(f"任务 {task_id} gate 名称不能重复：{name}")
        gates.append(
            Gate(
                name,
                tuple(argv),
                cwd,
                _positive_int(
                    entry.get("timeout_seconds", 1800),
                    f"任务 {task_id} gate {name} timeout_seconds",
                    maximum=MAX_GATE_TIMEOUT_SECONDS,
                ),
            )
        )
    merge = raw.get("merge", {})
    if not isinstance(merge, dict):
        raise ValidationError(f"任务 {task_id} merge 必须是表")
    return Task(
        id=task_id,
        title=title,
        line=line,
        risk=risk,
        executor=executor,
        reviewer=reviewer,
        repositories=tuple(repositories),
        depends_on=_strings(raw.get("depends_on", []), "depends_on"),
        blocked_on=_strings(raw.get("blocked_on", []), "blocked_on"),
        conflict_group=_string(
            raw.get("conflict_group", ""),
            f"任务 {task_id} conflict_group",
            allow_empty=True,
        ),
        timeout_minutes=_positive_int(
            raw.get("timeout_minutes", 60),
            f"任务 {task_id} timeout_minutes",
            maximum=MAX_TASK_TIMEOUT_MINUTES,
        ),
        review_timeout_minutes=_positive_int(
            raw.get("review_timeout_minutes", 45),
            f"任务 {task_id} review_timeout_minutes",
            maximum=MAX_TASK_TIMEOUT_MINUTES,
        ),
        gates=tuple(gates),
        merge_auto=strict_bool(merge.get("auto", False), "merge.auto"),
        merge_push=strict_bool(merge.get("push", False), "merge.push"),
        directory=path,
    )


def _parse_task(path: Path) -> Task:
    return _parse_task_content(path, (path / "task.toml").read_bytes())


def load_task(config: Config, task_id: str) -> Task:
    task = _parse_task(task_dir(config, task_id))
    if task.id != task_id:
        raise ValidationError(
            f"目录任务 ID 与 task.toml 不一致：{task_id} != {task.id}"
        )
    unknown = [
        repo_id for repo_id in task.repositories if repo_id not in config.repositories
    ]
    if unknown:
        raise ValidationError(f"任务 {task.id} 引用了未配置仓库：{', '.join(unknown)}")
    get_line(config, task.line)
    return task


def load_task_bounded(
    config: Config,
    task_id: str,
    budget: ReadBudget,
    *,
    known_line_ids: frozenset[str],
) -> Task:
    """Load one Task manifest without an unbounded line re-scan."""
    validate_id(task_id, "任务 ID")
    directory = config.task_specs_dir / task_id
    try:
        content = budget.read_regular_bytes_at(
            root=config.root,
            directory=directory,
            name="task.toml",
            maximum_bytes=budget.limits.task_manifest_bytes,
            label="task.toml",
        )
    except FileNotFoundError as exc:
        raise ValidationError(f"任务不存在：{task_id}") from exc
    return _validate_bounded_task(
        config,
        task_id,
        directory,
        content,
        known_line_ids=known_line_ids,
    )


def _validate_bounded_task(
    config: Config,
    task_id: str,
    directory: Path,
    content: bytes,
    *,
    known_line_ids: frozenset[str],
) -> Task:
    task = _parse_task_content(directory, content)
    if task.id != task_id:
        raise ValidationError(
            f"目录任务 ID 与 task.toml 不一致：{task_id} != {task.id}"
        )
    unknown = [
        repo_id for repo_id in task.repositories if repo_id not in config.repositories
    ]
    if unknown:
        raise ValidationError(f"任务 {task.id} 引用了未配置仓库：{', '.join(unknown)}")
    if task.line not in known_line_ids:
        raise ValidationError(f"任务 {task.id} 引用了未登记开发线：{task.line}")
    return task


def load_task_observation_bounded(
    config: Config,
    task_id: str,
    budget: ReadBudget,
    *,
    known_line_ids: frozenset[str],
) -> tuple[Task, str]:
    """Load one Task manifest and status from the same stable directory FD."""

    task, current, _content = _load_task_observation_details_bounded(
        config,
        task_id,
        budget,
        known_line_ids=known_line_ids,
    )
    return task, current


def load_task_planning_bounded(
    config: Config,
    task_id: str,
    budget: ReadBudget,
    *,
    known_line_ids: frozenset[str],
) -> tuple[Task, str, str]:
    """Load one Task/status pair and bind its exact manifest digest."""

    task, current, content = _load_task_observation_details_bounded(
        config,
        task_id,
        budget,
        known_line_ids=known_line_ids,
    )
    return task, current, hashlib.sha256(content).hexdigest()


def _load_task_observation_details_bounded(
    config: Config,
    task_id: str,
    budget: ReadBudget,
    *,
    known_line_ids: frozenset[str],
) -> tuple[Task, str, bytes]:
    validate_id(task_id, "任务 ID")
    directory = config.task_specs_dir / task_id
    try:
        with budget.open_safe_directory_chain(config.root, directory) as directory_fd:
            assert directory_fd is not None
            content = budget.read_regular_bytes_from_directory_fd(
                directory_fd,
                name="task.toml",
                maximum_bytes=budget.limits.task_manifest_bytes,
                label="task.toml",
            )
            try:
                status_content = budget.read_regular_bytes_from_directory_fd(
                    directory_fd,
                    name="status",
                    maximum_bytes=budget.limits.task_status_bytes,
                    label="task status",
                )
            except FileNotFoundError:
                status_content = b"backlog"
    except FileNotFoundError as exc:
        raise ValidationError(f"任务不存在：{task_id}") from exc
    task = _validate_bounded_task(
        config,
        task_id,
        directory,
        content,
        known_line_ids=known_line_ids,
    )
    try:
        current = status_content.decode("utf-8").strip()
    except UnicodeError as exc:
        raise ValidationError(f"任务 {task.id} 状态不是 UTF-8") from exc
    if current not in STATUSES:
        raise ValidationError(f"任务 {task.id} 状态非法")
    return task, current, content


def list_tasks(config: Config) -> list[Task]:
    if not config.task_specs_dir.exists():
        return []
    return [
        load_task(config, path.parent.name)
        for path in sorted(config.task_specs_dir.glob("*/task.toml"))
    ]


def list_task_ids_bounded(config: Config, budget: ReadBudget) -> tuple[str, ...]:
    """Enumerate Task directories without following symlinks or exceeding limits."""

    with budget.open_safe_directory_chain(
        config.root, config.task_specs_dir, allow_missing=True
    ) as directory_fd:
        if directory_fd is None:
            return ()
        task_ids: list[str] = []
        for name in sorted(
            bounded_directory_names(
                directory_fd,
                budget,
                maximum_records=budget.limits.task_records,
                label="Task",
            )
        ):
            try:
                info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError as exc:
                raise ReadLimitError(
                    ReadLimitCode.UNSAFE_FILE,
                    "Task root contains an unsafe entry",
                ) from exc
            if stat.S_ISLNK(info.st_mode):
                raise ReadLimitError(
                    ReadLimitCode.UNSAFE_FILE,
                    "Task root contains an unsafe entry",
                )
            if not stat.S_ISDIR(info.st_mode):
                continue
            validate_id(name, "任务 ID")
            budget.bind_directory_identity(
                config.task_specs_dir / name, (info.st_dev, info.st_ino)
            )
            task_ids.append(name)
        return tuple(task_ids)


def decisions_bounded(config: Config, budget: ReadBudget) -> dict[str, str]:
    """Read decision facts through the machine-facing bounded reader."""

    try:
        content = budget.read_regular_bytes_at(
            root=config.root,
            directory=config.decisions_file.parent,
            name=config.decisions_file.name,
            maximum_bytes=budget.limits.task_manifest_bytes,
            label="decisions.toml",
        )
    except FileNotFoundError:
        return {}
    try:
        raw = tomllib.loads(content.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError, RecursionError) as exc:
        raise ValidationError("决策点格式错误") from exc
    entries = raw.get("decisions", {})
    if not isinstance(entries, dict):
        raise ValidationError("decisions.toml 必须使用 [decisions.<id>]")
    return {
        str(key): str(value.get("status", "open"))
        for key, value in entries.items()
        if isinstance(value, dict)
    }


def external_claim_active_bounded(
    config: Config,
    task: Task,
    budget: ReadBudget,
    *,
    now: datetime,
) -> bool:
    """Read claim liveness without following a Task-directory symlink."""

    try:
        content = budget.read_regular_bytes_at(
            root=config.root,
            directory=task.directory,
            name=_claim_path(task).name,
            maximum_bytes=budget.limits.task_manifest_bytes,
            label="task claim",
        )
    except FileNotFoundError:
        return False
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValidationError(f"任务 {task.id} 领取记录格式错误") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("task_id") != task.id
        or not isinstance(payload.get("runner"), str)
    ):
        raise ValidationError(f"任务 {task.id} 领取记录无效")
    return not _claim_expired(payload, now=now)


def status(config: Config, task: Task) -> str:
    file = task.directory / "status"
    current = file.read_text(encoding="utf-8").strip() if file.exists() else "backlog"
    if current not in STATUSES:
        raise ValidationError(f"任务 {task.id} 状态非法：{current}")
    return current


def status_bounded(config: Config, task: Task, budget: ReadBudget) -> str:
    try:
        current = budget.read_regular_text_at(
            root=config.root,
            directory=task.directory,
            name="status",
            maximum_bytes=budget.limits.task_status_bytes,
            label="task status",
        ).strip()
    except FileNotFoundError:
        return "backlog"
    except UnicodeError as exc:
        raise ValidationError(f"任务 {task.id} 状态不是 UTF-8") from exc
    if current not in STATUSES:
        raise ValidationError(f"任务 {task.id} 状态非法")
    return current


def _claim_path(task: Task) -> Path:
    return task.directory / "claim.json"


def _state_lock_path(task: Task) -> Path:
    return task.directory / ".state.lock"


def _dispatch_lock_path(config: Config) -> Path:
    return config.root / ".dyro" / "dispatch.lock"


def _execution_lock_path(task: Task) -> Path:
    return task.directory / ".execution.lock"


def _review_lock_path(task: Task) -> Path:
    return task.directory / ".review.lock"


def _merge_lock_path(config: Config, line_id: str) -> Path:
    validate_id(line_id, "开发线 ID")
    return config.root / ".dyro" / "lines" / f"{line_id}.merge.lock"


def _claim(task: Task) -> dict[str, object] | None:
    path = _claim_path(task)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"任务 {task.id} 领取记录格式错误") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("task_id") != task.id
        or not isinstance(payload.get("runner"), str)
    ):
        raise ValidationError(f"任务 {task.id} 领取记录无效")
    return payload


DEFAULT_CLAIM_LEASE_SECONDS = 3600
MAX_CLAIM_LEASE_SECONDS = 604800


def _claim_lease_seconds(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationError("claim lease seconds 必须是正整数")
    if value > MAX_CLAIM_LEASE_SECONDS:
        raise ValidationError(f"claim lease seconds 不能超过 {MAX_CLAIM_LEASE_SECONDS}")
    return value


def _claim_expired(claim: dict[str, object], *, now: datetime | None = None) -> bool:
    expires_at = claim.get("lease_expires_at")
    if expires_at is None:
        return False
    if not isinstance(expires_at, str):
        raise ValidationError("claim lease_expires_at 无效")
    try:
        expires = datetime.fromisoformat(expires_at)
    except ValueError as exc:
        raise ValidationError("claim lease_expires_at 格式错误") from exc
    if expires.tzinfo is None:
        raise ValidationError("claim lease_expires_at 必须包含时区")
    return expires <= (now or datetime.now(timezone.utc))


def _require_external_security(config: Config) -> None:
    requirements = external_security_errors(config.policy)
    if requirements:
        raise DyroError(
            "external Profile 的身份边界尚未迁移；必须显式启用 "
            + "、".join(requirements)
            + "；先运行 dyro doctor"
        )


def external_claim_active(task: Task, *, now: datetime | None = None) -> bool:
    """Return claim liveness at an explicit instant when a caller has one."""
    claim = _claim(task)
    return claim is not None and not _claim_expired(claim, now=now)


def execution_claim_binding(
    task: Task, *, claim_file: Path | None = None
) -> dict[str, object]:
    if claim_file is None:
        claim = _claim(task)
    else:
        if not claim_file.is_file():
            raise ValidationError(f"claim 文件不存在：{claim_file}")
        try:
            claim = json.loads(claim_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationError(f"claim 文件损坏：{claim_file}") from exc
    required = ("claim_id", "runner", "execution_key_id")
    if (
        not isinstance(claim, dict)
        or claim.get("task_id") != task.id
        or any(
            not isinstance(claim.get(field), str) or not claim.get(field)
            for field in required
        )
        or not isinstance(claim.get("generation"), int)
        or int(claim["generation"]) < 1
    ):
        raise ValidationError(f"任务 {task.id} 的 signed execution claim 无效")
    if _claim_expired(claim):
        raise DyroError(f"任务 {task.id} 的 claim 已过期")
    return {
        "claim_id": str(claim["claim_id"]),
        "generation": int(claim["generation"]),
        "runner": str(claim["runner"]),
        "execution_key_id": str(claim["execution_key_id"]),
    }


def claim_task(
    config: Config,
    task: Task,
    *,
    runner: str,
    key_id: str | None = None,
    lease_seconds: int = DEFAULT_CLAIM_LEASE_SECONDS,
    dry_run: bool = False,
) -> str:
    """Atomically reserve an external task for one runner identity."""
    if config.policy.execution_mode != "external":
        raise DyroError("task claim 仅用于 execution_mode = external 的 Profile")
    _require_external_security(config)
    runner = runner.strip()
    if not runner:
        raise ValidationError("执行器标识不能为空")
    require_signed_execution = getattr(config.policy, "require_signed_execution", False)
    if require_signed_execution and not key_id:
        raise ValidationError(
            "require_signed_execution = true 时 claim 必须提供 --key-id"
        )
    if key_id:
        from .signing import trusted_key_ids, trusted_key_principal, validate_key_id

        key_id = validate_key_id(key_id)
        if key_id not in trusted_key_ids(config.root, "execution"):
            raise ValidationError(f"execution key ID 尚未受信任：{key_id}")
        if (
            require_signed_execution
            and trusted_key_principal(config.root, "execution", key_id) != runner
        ):
            raise ValidationError(
                "execution claim runner 必须等于 trusted key 的 principal"
            )
    lease_seconds = _claim_lease_seconds(lease_seconds)
    with exclusive_lock(_dispatch_lock_path(config)):
        with exclusive_lock(_state_lock_path(task)):
            check_dispatchable(config, task)
            current = status(config, task)
            existing = _claim(task)
            expired = existing is not None and _claim_expired(existing)
            if current not in ("backlog", "assigned", "waiting_answer") and not (
                current == "in_progress" and expired
            ):
                raise DyroError(
                    f"仅 backlog、assigned 或 waiting_answer 任务可领取：{task.id}"
                )
            if existing is not None and not expired:
                raise DyroError(f"任务 {task.id} 已被领取")
            target_status = (
                "waiting_answer" if current == "waiting_answer" else "assigned"
            )
            if dry_run:
                return target_status
            now = datetime.now(timezone.utc)
            payload = {
                "task_id": task.id,
                "claim_id": uuid.uuid4().hex,
                "runner": runner,
                "execution_key_id": key_id or "",
                "claimed_at": now.isoformat(timespec="seconds"),
                "lease_expires_at": (now + timedelta(seconds=lease_seconds)).isoformat(
                    timespec="seconds"
                ),
                "generation": int(existing.get("generation", 0)) + 1 if existing else 1,
            }
            atomic_write_text(
                _claim_path(task),
                json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
            )
            if current in ("backlog", "in_progress"):
                set_status(config, task, "assigned", force=current == "in_progress")
            ledger(
                config,
                task.id,
                "claim_takeover" if expired else "claim",
                runner=runner,
                lease_seconds=lease_seconds,
                previous_runner=existing.get("runner", "")
                if expired and existing
                else "",
                claim_id=payload["claim_id"],
                generation=payload["generation"],
                execution_key_id=payload["execution_key_id"],
            )
            return target_status


def renew_task_claim(
    config: Config,
    task: Task,
    *,
    runner: str,
    lease_seconds: int = DEFAULT_CLAIM_LEASE_SECONDS,
    dry_run: bool = False,
) -> str:
    if config.policy.execution_mode != "external":
        raise DyroError("task claim renew 仅用于 execution_mode = external 的 Profile")
    _require_external_security(config)
    runner = runner.strip()
    lease_seconds = _claim_lease_seconds(lease_seconds)
    with exclusive_lock(_dispatch_lock_path(config)):
        with exclusive_lock(_state_lock_path(task)):
            claim = _claim(task)
            if claim is None:
                raise DyroError(f"任务 {task.id} 尚未领取")
            if _claim_expired(claim):
                raise DyroError(f"任务 {task.id} 的 claim 已过期；请重新领取")
            if claim["runner"] != runner:
                raise DyroError(f"任务 {task.id} 由其他 runner 领取")
            if dry_run:
                return status(config, task)
            now = datetime.now(timezone.utc)
            renewed = dict(claim)
            renewed["lease_expires_at"] = (
                now + timedelta(seconds=lease_seconds)
            ).isoformat(timespec="seconds")
            renewed["renewed_at"] = now.isoformat(timespec="seconds")
            atomic_write_text(
                _claim_path(task),
                json.dumps(renewed, ensure_ascii=False, sort_keys=True) + "\n",
            )
            ledger(
                config,
                task.id,
                "claim_renew",
                runner=runner,
                lease_seconds=lease_seconds,
            )
            return status(config, task)


def release_task_claim(
    config: Config,
    task: Task,
    *,
    runner: str,
    dry_run: bool = False,
) -> str:
    if config.policy.execution_mode != "external":
        raise DyroError(
            "task claim release 仅用于 execution_mode = external 的 Profile"
        )
    _require_external_security(config)
    runner = runner.strip()
    with exclusive_lock(_dispatch_lock_path(config)):
        with exclusive_lock(_state_lock_path(task)):
            claim = _claim(task)
            if claim is None:
                raise DyroError(f"任务 {task.id} 尚未领取")
            if claim["runner"] != runner:
                raise DyroError(f"任务 {task.id} 由其他 runner 领取")
            current = status(config, task)
            next_status = (
                "backlog"
                if current == "assigned"
                else "assigned"
                if current == "in_progress"
                else current
            )
            if dry_run:
                return next_status
            _claim_path(task).unlink()
            if next_status != current:
                set_status(config, task, next_status, force=True)
            ledger(
                config,
                task.id,
                "claim_release",
                runner=runner,
                from_status=current,
                to_status=next_status,
            )
            return next_status


def set_status(
    config: Config,
    task: Task,
    next_status: str,
    *,
    force: bool = False,
    dry_run: bool = False,
    _allow_quality_gate: bool = False,
) -> None:
    if next_status not in STATUSES:
        raise ValidationError(f"非法状态 {next_status}，可选：{', '.join(STATUSES)}")
    if next_status in QUALITY_GATE_STATUSES and not _allow_quality_gate:
        raise DyroError(
            "质量门状态只能由执行证据、独立复核或签收流程写入；"
            "task status 仅可用于非质量门状态恢复"
        )
    with exclusive_lock(_state_lock_path(task)):
        current = status(config, task)
        if current == next_status:
            return
        if (
            config.policy.require_external_signoff
            and next_status == "done"
            and not _valid_external_signoff(config, task)
        ):
            raise DyroError(
                "当前 Profile 要求外部签收；请先使用 task signoff 写入与回执、复核绑定的签收记录"
            )
        if not force and next_status not in TRANSITIONS[current]:
            raise DyroError(
                f"拒绝状态跳转 {current} -> {next_status}；如确有人工恢复需求，使用 --force 并留下审计记录"
            )
        if not dry_run:
            atomic_write_text(task.directory / "status", next_status + "\n")
            ledger(
                config, task.id, "status", from_status=current, to_status=next_status
            )


def _set_quality_gate_status(
    config: Config,
    task: Task,
    next_status: str,
    *,
    dry_run: bool = False,
) -> None:
    """Private transition used only after the relevant evidence has been verified."""
    if next_status not in QUALITY_GATE_STATUSES:
        raise AssertionError(f"not a quality-gate status: {next_status}")
    set_status(
        config,
        task,
        next_status,
        dry_run=dry_run,
        _allow_quality_gate=True,
    )


def ledger(config: Config, task_id: str, phase: str, **fields: object) -> None:
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "task_id": task_id,
        "phase": phase,
        **fields,
    }
    with exclusive_lock(config.root / ".dyro" / "ledger.lock"):
        append_text(
            config.ledger_file,
            json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
        )


def decisions(config: Config) -> dict[str, str]:
    if not config.decisions_file.exists():
        return {}
    try:
        raw = tomllib.loads(config.decisions_file.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ValidationError(f"决策点格式错误：{exc}") from exc
    entries = raw.get("decisions", {})
    if not isinstance(entries, dict):
        raise ValidationError("decisions.toml 必须使用 [decisions.<id>]")
    return {
        str(key): str(value.get("status", "open"))
        for key, value in entries.items()
        if isinstance(value, dict)
    }


def _assert_task_graph_valid(config: Config) -> None:
    from .graph import build_task_graph, validate_task_graph

    issues = validate_task_graph(build_task_graph(config))
    if issues:
        details = "; ".join(issue.message for issue in issues[:5])
        suffix = f"；另有 {len(issues) - 5} 项" if len(issues) > 5 else ""
        raise ValidationError(f"任务图结构无效：{details}{suffix}")


def check_dispatchable(
    config: Config, task: Task, *, validate_graph: bool = True
) -> None:
    if validate_graph:
        _assert_task_graph_valid(config)
    states = decisions(config)
    unresolved = [
        decision for decision in task.blocked_on if states.get(decision) != "resolved"
    ]
    if unresolved:
        raise DyroError(
            f"任务 {task.id} 被未 resolved 的决策点阻塞：{', '.join(unresolved)}"
        )
    for dependency in task.depends_on:
        dependency_task = load_task(config, dependency)
        if status(config, dependency_task) != "done":
            raise DyroError(
                f"任务 {task.id} 依赖 {dependency}，当前状态为 {status(config, dependency_task)}"
            )
        _assert_dependency_integrated(config, dependency_task)
    if task.conflict_group:
        active = [
            other.id
            for other in list_tasks(config)
            if other.id != task.id
            and other.conflict_group == task.conflict_group
            and (
                status(config, other) == "in_progress"
                or (
                    config.policy.execution_mode == "external"
                    and status(config, other) == "assigned"
                    and external_claim_active(other)
                )
            )
        ]
        if active:
            raise DyroError(
                f"任务 {task.id} 与活跃任务 {', '.join(active)} 共用冲突组 {task.conflict_group}"
            )


@dataclass(frozen=True)
class ScheduleBlock:
    task: Task
    reason: str


@dataclass(frozen=True)
class SchedulePlan:
    ready: tuple[Task, ...]
    blocked: tuple[ScheduleBlock, ...]
    review: tuple[Task, ...] = ()


@dataclass(frozen=True)
class ScheduleWave:
    tasks: tuple[Task, ...]
    deferred: tuple[ScheduleBlock, ...]


def plan_tasks(
    config: Config,
    *,
    candidates: tuple[Task, ...] | list[Task] | None = None,
) -> SchedulePlan:
    """Adapt the shared, immutable scheduler plan for legacy task callers."""
    from .continuation.planner import build_task_readiness
    from .continuation.snapshot import build_scheduler_snapshot

    snapshot = build_scheduler_snapshot(config, candidates=candidates)
    readiness = build_task_readiness(snapshot)
    by_id = snapshot.tasks_by_id
    return SchedulePlan(
        ready=readiness.ready,
        blocked=tuple(
            ScheduleBlock(
                task=by_id[action.subject_id].task,
                reason=_schedule_block_reason(action.reason.value, dict(action.facts)),
            )
            for action in readiness.blocked
        ),
        review=readiness.review,
    )


def _schedule_block_reason(reason: str, facts: dict[str, str]) -> str:
    """Keep legacy human diagnostics while the planner itself remains locale-free."""
    if reason == "DECISION_OPEN":
        return f"任务被未 resolved 的决策点阻塞：{facts.get('decision_ids', '')}"
    if reason == "DEPENDENCY_PENDING":
        return f"任务依赖 {facts.get('dependency_id', '')}，当前状态为 {facts.get('dependency_status', '')}"
    if reason == "TASK_INTEGRATION_PENDING":
        return f"任务依赖 {facts.get('dependency_id', '')} 已完成但尚未集成"
    if reason == "CONFLICT_GROUP_ACTIVE":
        return f"任务与活跃任务 {facts.get('active_task_ids', '')} 共用冲突组 {facts.get('conflict_group', '')}"
    if reason == "EXTERNAL_CLAIM_ACTIVE":
        return "任务已有有效的外部执行 claim"
    if reason == "PROOF_DECAYED":
        return "任务的 merge 绑定已衰减（PROOF_DECAYED），当前工作区无法按原复核合并"
    return reason


def select_task_wave(plan: SchedulePlan, *, limit: int) -> ScheduleWave:
    """Select one deterministic parallel wave from a ready task set."""
    capacity = max(1, limit)
    selected: list[Task] = []
    deferred: list[ScheduleBlock] = []
    reserved_groups: dict[str, str] = {}
    for task in plan.ready:
        if task.conflict_group and task.conflict_group in reserved_groups:
            deferred.append(
                ScheduleBlock(
                    task=task,
                    reason=(
                        f"冲突组 {task.conflict_group} 已在本轮分配给任务 "
                        f"{reserved_groups[task.conflict_group]}"
                    ),
                )
            )
            continue
        if len(selected) >= capacity:
            deferred.append(
                ScheduleBlock(
                    task=task,
                    reason=f"本轮并行容量已达到 {capacity}",
                )
            )
            continue
        selected.append(task)
        if task.conflict_group:
            reserved_groups[task.conflict_group] = task.id
    return ScheduleWave(tasks=tuple(selected), deferred=tuple(deferred))


def _execution_plan_snapshot(
    config: Config,
    task: Task,
    *,
    continuation: dict[str, object] | None = None,
) -> dict[str, object]:
    schedule = plan_tasks(config)
    known_by_id = {candidate.id: candidate for candidate in list_tasks(config)}
    decision_states = decisions(config)
    snapshot: dict[str, object] = {
        "schema_version": 1,
        "task": {
            "id": task.id,
            "line": task.line,
            "risk": task.risk,
            "executor": task.executor,
            "reviewer": task.reviewer,
            "repositories": list(task.repositories),
            "depends_on": list(task.depends_on),
            "blocked_on": list(task.blocked_on),
            "conflict_group": task.conflict_group,
            "gates": [
                {
                    "name": gate.name,
                    "argv": list(gate.argv),
                    "cwd": gate.cwd,
                    "timeout_seconds": gate.timeout_seconds,
                }
                for gate in task.gates
            ],
        },
        "dependency_states": {
            dependency: (
                status(config, known_by_id[dependency])
                if dependency in known_by_id
                else "missing"
            )
            for dependency in task.depends_on
        },
        "decision_states": {
            decision_id: decision_states.get(decision_id, "missing")
            for decision_id in task.blocked_on
        },
        "ready_set": [candidate.id for candidate in schedule.ready],
        "blocked": [
            {"task_id": block.task.id, "reason": block.reason}
            for block in schedule.blocked
        ],
        "execution_mode": config.policy.execution_mode,
    }
    if continuation is not None:
        snapshot["continuation"] = continuation
    return snapshot


def _complete_execution_attempt(
    config: Config,
    task: Task,
    attempt: ExecutionAttempt,
    execute,
) -> str:
    ledger(
        config,
        task.id,
        "attempt_started",
        run_id=attempt.run_id,
        attempt_id=attempt.attempt_id,
        attempt_number=attempt.attempt_number,
        task_contract_sha256=attempt.task_contract_sha256,
        plan_sha256=attempt.plan_sha256,
    )
    try:
        result = execute()
    except Exception as exc:
        try:
            finish_execution_attempt(attempt, error=exc)
        except Exception as finish_exc:  # keep the executor failure authoritative
            exc.add_note(f"attempt finalization also failed: {finish_exc}")
        try:
            if (
                current_execution_attempt_id(task.directory) == attempt.attempt_id
                and status(config, task) == "in_progress"
            ):
                set_status(config, task, "failed")
        except Exception as state_exc:  # preserve the executor failure for callers
            exc.add_note(f"task failure transition also failed: {state_exc}")
        ledger(
            config,
            task.id,
            "attempt_failed",
            run_id=attempt.run_id,
            attempt_id=attempt.attempt_id,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        raise
    finish_execution_attempt(attempt, result=result)
    ledger(
        config,
        task.id,
        "attempt_completed",
        run_id=attempt.run_id,
        attempt_id=attempt.attempt_id,
        result=result,
    )
    return result


def _reserve_local_execution(
    config: Config,
    task: Task,
    *,
    allowed: tuple[str, ...],
    action: str,
    dry_run: bool,
    legacy_scheduler: bool = False,
    expected_contract_sha256: str | None = None,
) -> None:
    """Check dispatch constraints and atomically reserve the task before starting an Agent."""

    def reserve() -> None:
        with exclusive_lock(_state_lock_path(task)):
            _assert_expected_task_contract(task, expected_contract_sha256)
            current = status(config, task)
            if current not in allowed:
                raise DyroError(f"任务 {task.id} 当前为 {current}，不能{action}")
            check_dispatchable(config, task)
            if dry_run:
                return
            if current in ("backlog", "failed"):
                set_status(config, task, "assigned", force=current == "failed")
            set_status(config, task, "in_progress")

    if legacy_scheduler:
        from .continuation.store import legacy_scheduler_reservation

        with legacy_scheduler_reservation(config, (task.id,)):
            with exclusive_lock(_dispatch_lock_path(config)):
                reserve()
        return
    with exclusive_lock(_dispatch_lock_path(config)):
        reserve()


def _assert_expected_task_contract(
    task: Task, expected_contract_sha256: str | None
) -> None:
    """Fail closed when a supervised Action's pinned Task contract drifted.

    This executes under the Task state lock for execution and immediately
    before review dispatch.  Direct operator Task commands pass ``None`` and
    retain their established explicit-command behaviour.
    """
    if expected_contract_sha256 is None:
        return
    if (
        not isinstance(expected_contract_sha256, str)
        or len(expected_contract_sha256) != 64
        or any(
            character not in "0123456789abcdef"
            for character in expected_contract_sha256
        )
    ):
        raise ValidationError("受监督 Action 的 Task contract 摘要无效")
    try:
        actual = hashlib.sha256((task.directory / "task.toml").read_bytes()).hexdigest()
    except OSError as exc:
        raise ValidationError(f"无法读取任务 {task.id} contract") from exc
    if actual != expected_contract_sha256:
        raise DyroError(
            "Task contract 已在受监督 Action 确认后变化；请 objective reconcile 后重新确认"
        )


def worktree_root(config: Config, task: Task) -> Path:
    return config.root / config.layout.tasks / task.line / task.id


def _resolved_git_common_dir(path: Path) -> Path:
    raw = require_ok(
        git(path, "rev-parse", "--git-common-dir"), f"读取 Git common dir：{path}"
    ).stdout.strip()
    common_dir = Path(raw)
    return (
        common_dir.resolve()
        if common_dir.is_absolute()
        else (path / common_dir).resolve()
    )


def _validate_task_worktree(
    config: Config, task: Task, repo_id: str, destination: Path, branch: str
) -> None:
    if git(destination, "rev-parse", "--is-inside-work-tree").stdout.strip() != "true":
        raise DyroError(f"不是有效的任务 Git worktree：{destination}")
    top_level = require_ok(
        git(destination, "rev-parse", "--show-toplevel"),
        f"读取 {repo_id} worktree 根目录",
    ).stdout.strip()
    if Path(top_level).resolve() != destination.resolve():
        raise DyroError(f"任务 worktree 根目录错误：{destination} 实际为 {top_level}")
    current = require_ok(
        git(destination, "branch", "--show-current"), f"读取 {repo_id} 任务分支"
    ).stdout.strip()
    if current != branch:
        raise DyroError(
            f"任务 worktree 分支错误：{destination} 当前 {current or 'DETACHED'}，期望 {branch}"
        )
    anchor = repository_path(config, repo_id)
    if _resolved_git_common_dir(destination) != _resolved_git_common_dir(anchor):
        raise DyroError(f"任务 worktree 不属于配置的仓库 anchor：{destination}")


def existing_task_workspace(config: Config, task: Task) -> Path:
    """Return a fully validated existing task workspace without creating it."""

    root = worktree_root(config, task)
    if not root.is_dir():
        raise DyroError(
            f"任务 {task.id} 尚未创建可进入的工作区。下一步：dyro task run {task.id}"
        )
    branch = f"{config.policy.task_branch_prefix}{task.id}"
    for repo_id in task.repositories:
        destination = root / config.repositories[repo_id].mount
        if not destination.is_dir():
            raise DyroError(
                f"任务 {task.id} 工作区不完整，缺少 {repo_id}：{destination}；请先运行 dyro doctor"
            )
        _validate_task_worktree(config, task, repo_id, destination, branch)
    return root


def _ensure_task_worktrees(
    config: Config, task: Task, line: Line, *, dry_run: bool = False
) -> Path:
    root = worktree_root(config, task)
    branch = f"{config.policy.task_branch_prefix}{task.id}"
    not_on_line = [
        repo_id for repo_id in task.repositories if repo_id not in line.repositories
    ]
    if not_on_line:
        raise ValidationError(
            f"任务 {task.id} 引用的仓库不在开发线 {line.id}：{', '.join(not_on_line)}"
        )
    for repo_id in task.repositories:
        anchor = repository_path(config, repo_id)
        destination = root / config.repositories[repo_id].mount
        if destination.exists():
            _validate_task_worktree(config, task, repo_id, destination, branch)
            continue
        require_ok(
            git(anchor, "rev-parse", "--verify", f"{line.branch}^{{commit}}"),
            f"校验 {repo_id} 开发线基线",
        )
        branch_exists = (
            git(anchor, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}").code
            == 0
        )
        command: tuple[str, ...] = ("worktree", "add")
        if not branch_exists:
            command += ("-b", branch)
        command += (str(destination), branch if branch_exists else line.branch)
        if not dry_run:
            destination.parent.mkdir(parents=True, exist_ok=True)
        require_ok(
            git(anchor, *command, dry_run=dry_run, timeout=300),
            f"创建任务 worktree {repo_id}",
        )
    return root


def _collect_task_heads(config: Config, task: Task) -> dict[str, str]:
    branch = f"{config.policy.task_branch_prefix}{task.id}"
    root = worktree_root(config, task)
    heads: dict[str, str] = {}
    for repo_id in task.repositories:
        destination = root / config.repositories[repo_id].mount
        _validate_task_worktree(config, task, repo_id, destination, branch)
        dirty = require_ok(
            git(destination, "status", "--porcelain=v1", "-uall"),
            f"读取 {repo_id} 任务 worktree 状态",
        ).stdout.strip()
        if dirty:
            raise DyroError(f"任务 worktree 不干净，必须先提交全部改动：{destination}")
        heads[repo_id] = require_ok(
            git(destination, "rev-parse", "HEAD"), f"读取 {repo_id} 任务 HEAD"
        ).stdout.strip()
    return heads


def _task_heads_payload(
    config: Config, task: Task, heads: dict[str, str]
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "task_id": task.id,
        "line": task.line,
        "branch": f"{config.policy.task_branch_prefix}{task.id}",
        "repositories": heads,
    }


def _validate_task_heads_payload(
    config: Config, task: Task, payload: object
) -> dict[str, str]:
    expected_branch = f"{config.policy.task_branch_prefix}{task.id}"
    if not isinstance(payload, dict):
        raise ValidationError("任务 HEAD 证据必须是 JSON 对象")
    repositories = payload.get("repositories")
    if (
        payload.get("schema_version") != 1
        or payload.get("task_id") != task.id
        or payload.get("line") != task.line
        or not isinstance(payload.get("branch"), str)
        or not isinstance(repositories, dict)
    ):
        raise ValidationError(
            "任务 HEAD 证据的 schema_version、task_id、line、branch 或 repositories 无效"
        )
    if payload["branch"] != expected_branch:
        raise ValidationError(f"任务 HEAD 证据分支错误：期望 {expected_branch}")
    if set(repositories) != set(task.repositories):
        raise ValidationError("任务 HEAD 证据必须与任务 repositories 一一对应")
    heads: dict[str, str] = {}
    for repo_id in task.repositories:
        head = repositories[repo_id]
        if not isinstance(head, str) or not GIT_HEAD_RE.fullmatch(head):
            raise ValidationError(f"任务 HEAD 证据包含无效提交：{repo_id}")
        heads[repo_id] = head.lower()
    return heads


def _serialize_task_heads(config: Config, task: Task, heads: dict[str, str]) -> bytes:
    payload = _task_heads_payload(config, task, heads)
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode()


def _record_task_heads(config: Config, task: Task) -> str:
    content = _serialize_task_heads(config, task, _collect_task_heads(config, task))
    target = task.directory / TASK_HEADS_FILE
    atomic_write_bytes(target, content)
    return hashlib.sha256(content).hexdigest()


def _load_task_heads(config: Config, task: Task) -> dict[str, str]:
    path = resolve_evidence_path(task.directory, TASK_HEADS_FILE)
    if not path.is_file():
        raise DyroError(f"缺少任务 HEAD 证据：{path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"任务 HEAD 证据不是有效 JSON：{path}") from exc
    return _validate_task_heads_payload(config, task, payload)


def _json_object_bounded(content: bytes, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(content)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValidationError(f"{label} 不是有效 JSON") from exc
    if not isinstance(payload, dict):
        raise ValidationError(f"{label} 必须是 JSON 对象")
    return payload


def _load_task_heads_bounded(
    config: Config, task: Task, budget: ReadBudget
) -> dict[str, str]:
    """Load legacy or imported task-head evidence through bounded safe reads."""

    try:
        pointer_content = budget.read_regular_bytes_at(
            root=config.root,
            directory=task.directory,
            name=CURRENT_EVIDENCE_FILE,
            maximum_bytes=budget.limits.evidence_pointer_bytes,
            label="Current evidence pointer",
        )
    except FileNotFoundError:
        heads_content = budget.read_regular_bytes_at(
            root=config.root,
            directory=task.directory,
            name=TASK_HEADS_FILE,
            maximum_bytes=budget.limits.task_heads_bytes,
            label="Task heads evidence",
        )
        return _validate_task_heads_payload(
            config,
            task,
            _json_object_bounded(heads_content, "任务 HEAD 证据"),
        )

    pointer = _json_object_bounded(pointer_content, "当前证据指针")
    generation = pointer.get("generation")
    manifest_sha256 = pointer.get("manifest_sha256")
    if (
        pointer.get("schema_version") != 1
        or not isinstance(generation, str)
        or GENERATION_PATTERN.fullmatch(generation) is None
        or not isinstance(manifest_sha256, str)
        or len(manifest_sha256) != 64
    ):
        raise ValidationError("当前证据指针格式无效")
    generation_directory = (
        task.directory / EVIDENCE_GENERATIONS_DIR / generation
    )
    manifest_content = budget.read_regular_bytes_at(
        root=config.root,
        directory=generation_directory,
        name=MANIFEST_FILE,
        maximum_bytes=budget.limits.evidence_manifest_bytes,
        label="Evidence generation manifest",
    )
    if hashlib.sha256(manifest_content).hexdigest() != manifest_sha256:
        raise ValidationError("当前证据世代 manifest 哈希不匹配")
    manifest = _json_object_bounded(manifest_content, "证据世代 manifest")
    files = manifest.get("files")
    if (
        manifest.get("schema_version") != 1
        or manifest.get("generation") != generation
        or not isinstance(files, dict)
    ):
        raise ValidationError("证据世代 manifest 格式无效")
    heads_content = budget.read_regular_bytes_at(
        root=config.root,
        directory=generation_directory,
        name=TASK_HEADS_FILE,
        maximum_bytes=budget.limits.task_heads_bytes,
        label="Task heads evidence",
    )
    entry = files.get(TASK_HEADS_FILE)
    if (
        not isinstance(entry, dict)
        or not isinstance(entry.get("sha256"), str)
        or isinstance(entry.get("size"), bool)
        or not isinstance(entry.get("size"), int)
        or len(heads_content) != entry["size"]
        or hashlib.sha256(heads_content).hexdigest() != entry["sha256"]
    ):
        raise ValidationError("不可变任务 HEAD 证据缺失或哈希不匹配")
    return _validate_task_heads_payload(
        config,
        task,
        _json_object_bounded(heads_content, "任务 HEAD 证据"),
    )


def dependency_integration_state_bounded(
    config: Config, task: Task, budget: ReadBudget
) -> str:
    """Return the real integration state within the shared observation budget."""

    try:
        line = get_line(config, task.line, read_budget=budget)
        heads = _load_task_heads_bounded(config, task, budget)
        for repository_id, task_head in heads.items():
            destination = line_repository_path(config, line, repository_id)
            result = git_read(
                destination,
                "merge-base",
                "--is-ancestor",
                task_head,
                "HEAD",
                read_budget=budget,
            )
            if result.code != 0:
                return "pending"
    except ReadLimitError:
        raise
    except (DyroError, OSError, ValidationError):
        return "pending"
    return "integrated"


def _assert_dependency_integrated(config: Config, task: Task) -> None:
    line = get_line(config, task.line)
    heads = _load_task_heads(config, task)
    for repository_id, task_head in heads.items():
        destination = line_repository_path(config, line, repository_id)
        result = run(
            ("git", "merge-base", "--is-ancestor", task_head, "HEAD"),
            cwd=destination,
        )
        if result.code != 0:
            raise DyroError(
                f"任务 {task.id} 已复核但尚未集成到开发线 {task.line} 的仓库 "
                f"{repository_id}；请先执行 task merge"
            )


def _assert_task_heads_current(config: Config, task: Task) -> dict[str, str]:
    expected = _load_task_heads(config, task)
    current = _collect_task_heads(config, task)
    if current != expected:
        changed = sorted(
            repo_id
            for repo_id in task.repositories
            if current.get(repo_id) != expected.get(repo_id)
        )
        raise DyroError(
            f"任务代码已偏离已记录 HEAD，必须重新执行与复核：{', '.join(changed)}"
        )
    return expected


def _receipt_result(task: Task) -> str:
    receipt = resolve_evidence_path(task.directory, "receipt.md")
    if not receipt.exists():
        return ""
    first = receipt.read_text(encoding="utf-8").splitlines()
    match = RESULT_RE.match(first[0]) if first else None
    return match.group(1).upper() if match else ""


def _file_sha256(path: Path) -> str:
    if not path.is_file():
        raise DyroError(f"缺少证据文件：{path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _review_decision(task: Task) -> tuple[str, str, str]:
    review = task.directory / "review.md"
    if not review.is_file():
        return "", "", ""
    content = review.read_text(encoding="utf-8")
    lines = content.splitlines()
    verdict = (
        VERDICT_RE.match(lines[0]).group(1).upper()
        if lines and VERDICT_RE.match(lines[0])
        else ""
    )
    receipt_hash = RECEIPT_SHA_RE.search(content)
    task_heads_hash = TASK_HEADS_SHA_RE.search(content)
    return (
        verdict,
        receipt_hash.group(1).lower() if receipt_hash else "",
        task_heads_hash.group(1).lower() if task_heads_hash else "",
    )


def _external_execution_and_reviewer_principals(
    config: Config, task: Task
) -> tuple[str, str]:
    """Return the independently authenticated execution and review principals."""
    from .signing import trusted_key_principal

    claim = _require_external_claim(config, task)
    execution_principal = trusted_key_principal(
        config.root,
        "execution",
        str(claim["execution_key_id"]),
    )
    identity_path = task.directory / REVIEW_IDENTITY_FILE
    try:
        review_identity = json.loads(identity_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError("缺少可信的 external review identity") from exc
    if not isinstance(review_identity, dict):
        raise ValidationError("external review identity 无效")
    key_id = review_identity.get("key_id")
    reviewer_principal = review_identity.get("principal_id")
    review_sha256 = review_identity.get("review_sha256")
    if (
        review_identity.get("task_id") != task.id
        or not isinstance(key_id, str)
        or not key_id
        or not isinstance(reviewer_principal, str)
        or not reviewer_principal
        or not isinstance(review_sha256, str)
        or review_sha256 != _file_sha256(task.directory / "review.md")
        or trusted_key_principal(config.root, "review", key_id) != reviewer_principal
    ):
        raise ValidationError("external review identity 与当前复核证据不匹配")
    return execution_principal, reviewer_principal


def _valid_external_signoff(config: Config, task: Task) -> bool:
    signoff_path = task.directory / "signoff.json"
    if not signoff_path.is_file():
        return False
    try:
        signoff = json.loads(signoff_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    if (
        not isinstance(signoff, dict)
        or not isinstance(signoff.get("approver"), str)
        or not signoff["approver"].strip()
    ):
        return False
    try:
        from .signing import (
            signature_key_id,
            trusted_key_principal,
            trusted_keys_directory,
            verify_record,
        )

        verify_record(
            signoff,
            purpose="signoff",
            trust_directory=trusted_keys_directory(config.root, "signoff"),
            required=getattr(config.policy, "require_signed_signoff", False),
        )
        if config.policy.execution_mode == "external":
            signoff_key_id = signature_key_id(signoff)
            if signoff_key_id is None:
                return False
            approver_principal = trusted_key_principal(
                config.root, "signoff", signoff_key_id
            )
            if (
                signoff.get("actor") != signoff["approver"]
                or approver_principal != signoff["approver"]
            ):
                return False
            execution_principal, reviewer_principal = (
                _external_execution_and_reviewer_principals(config, task)
            )
            if approver_principal in (execution_principal, reviewer_principal):
                return False
    except (DyroError, ValidationError):
        return False
    if not _valid_review_acceptance(config, task):
        return False
    verdict, reviewed_receipt_hash, reviewed_task_heads_hash = _review_decision(task)
    try:
        receipt_hash = _file_sha256(resolve_evidence_path(task.directory, "receipt.md"))
        review_hash = _file_sha256(task.directory / "review.md")
        task_heads_hash = _file_sha256(
            resolve_evidence_path(task.directory, TASK_HEADS_FILE)
        )
    except DyroError:
        return False
    review_content = (task.directory / "review.md").read_text(encoding="utf-8")
    binding_matches, expected_binding, _ = validate_review_binding(
        task.directory, review_content
    )
    if not binding_matches:
        return False
    if config.policy.execution_mode == "local":
        try:
            _assert_task_heads_current(config, task)
        except (DyroError, ValidationError):
            return False
    return (
        verdict == "PASS"
        and reviewed_receipt_hash == receipt_hash
        and reviewed_task_heads_hash == task_heads_hash
        and signoff.get("task_id") == task.id
        and signoff.get("receipt_sha256") == receipt_hash
        and signoff.get("task_heads_sha256") == task_heads_hash
        and signoff.get("review_sha256") == review_hash
        and (
            expected_binding is None
            or (
                signoff.get("attempt_id") == expected_binding[0]
                and signoff.get("plan_sha256") == expected_binding[1]
            )
        )
    )


def _valid_review_acceptance(config: Config, task: Task) -> bool:
    """Return whether an accepted review still binds the current execution proof."""
    verdict, reviewed_receipt_hash, reviewed_task_heads_hash = _review_decision(task)
    try:
        receipt_hash = _file_sha256(resolve_evidence_path(task.directory, "receipt.md"))
        task_heads_hash = _file_sha256(
            resolve_evidence_path(task.directory, TASK_HEADS_FILE)
        )
    except DyroError:
        return False
    review = task.directory / "review.md"
    if not review.is_file():
        return False
    binding_matches, _, _ = validate_review_binding(
        task.directory,
        review.read_text(encoding="utf-8"),
    )
    if not binding_matches:
        return False
    if config.policy.execution_mode == "local":
        try:
            _assert_task_heads_current(config, task)
        except (DyroError, ValidationError):
            return False
    return (
        verdict == "PASS"
        and reviewed_receipt_hash == receipt_hash
        and reviewed_task_heads_hash == task_heads_hash
    )


def _require_local_execution(config: Config, action: str, *, dry_run: bool) -> None:
    if config.policy.execution_mode == "external" and not dry_run:
        raise DyroError(
            f"当前 Profile 要求外部隔离执行器；本机 dyro 不能执行 {action}。"
            "请由受信任的外部 runner 运行并导入其证据，或仅使用 --dry-run 进行计划核验。"
        )


def _adapter_argv(
    config: Config, agent: str, mode: str, *, workspace: Path, prompt: str, task: Task
) -> tuple[str, ...]:
    try:
        adapter = config.adapters[agent]
    except KeyError as exc:
        raise ValidationError(
            f"任务 {task.id} 使用的 Agent adapter 未配置：{agent}"
        ) from exc
    card = getattr(config, "capabilities", {}).get(agent)
    if card is not None and mode == "write" and "execute" not in getattr(card, "intents", ()):
        raise DyroError(f"Capability {agent} 未授予 execute，不能作为任务执行器")
    template = adapter.write if mode == "write" else adapter.read
    return expand_argv(
        template,
        workspace=workspace,
        root=config.root,
        prompt=prompt,
        task=task.id,
        line=task.line,
    )


def _prompt(task: Task, phase: str, workspace: Path) -> str:
    receipt = task.directory / "receipt.md"
    review = task.directory / "review.md"
    task_heads = task.directory / TASK_HEADS_FILE
    handoff = task.directory / "handoff.md"
    if phase == "executor":
        return (
            f"你是执行位。阅读 {handoff}；只在 {workspace} 内工作。完成后在 {receipt} 写回执，首行必须是 "
            "result: DONE、result: BLOCKED 或 result: QUESTION。若需决策，把问题写入同目录 questions.md。禁止 push、禁止合并开发线。"
        )
    if phase == "continuation":
        return (
            f"你是继续执行的执行位。阅读 {handoff}、{task.directory / 'questions.md'} 与 "
            f"{task.directory / 'answers.md'}；此前成果保留在 {workspace}。完成后更新 {receipt}，首行必须为 "
            "result: DONE、result: BLOCKED 或 result: QUESTION。禁止 push、禁止合并开发线。"
        )
    binding = review_binding(task.directory)
    provenance_requirement = ""
    if binding is not None:
        provenance_requirement = (
            f"同时写入 attempt_id: {binding[0]} 与 plan_sha256: {binding[1]}；"
        )
    return (
        f"你是独立复核位。阅读 {handoff}、{receipt} 与 {task_heads}；"
        f"在 {workspace} 用只读证据核验规格、回归、测试和越界改动。"
        f"在 {review} 写复核，首行必须是 verdict: PASS 或 verdict: FAIL，并写入 "
        "receipt_sha256: <所读取回执的 SHA-256> 与 "
        "task_heads_sha256: <所读取任务 HEAD 证据的 SHA-256>；"
        f"{provenance_requirement}"
        "禁止修改任何源码、push 或合并。"
    )


def _capture(task: Task, filename: str, output: str, *, dry_run: bool = False) -> Path:
    target = task.directory / "logs" / filename
    if not dry_run:
        atomic_write_text(target, output)
    return target


def _copy_external_evidence(
    task: Task, source: Path, target_name: str, *, dry_run: bool = False
) -> Path:
    if not source.is_file():
        raise DyroError(f"外部证据文件不存在：{source}")
    relative = Path(target_name)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValidationError(f"外部证据目标路径非法：{target_name}")
    target = task.directory / relative
    if not dry_run:
        atomic_write_bytes(target, source.read_bytes())
    return target


def _validate_external_gates(
    task: Task, receipt_sha256: str, gates: Path | None
) -> tuple[bytes, tuple[tuple[str, bytes], ...]]:
    if gates is None:
        if task.gates:
            raise DyroError(
                f"任务 {task.id} 配置了门禁，导入执行证据时必须提供 --gates"
            )
        return b"", ()
    if not gates.is_file():
        raise DyroError(f"外部门禁证据文件不存在：{gates}")
    data = gates.read_bytes()
    try:
        payload = json.loads(data)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"外部门禁证据必须是 JSON：{gates}") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or payload.get("task_id") != task.id
    ):
        raise ValidationError("外部门禁证据的 schema_version 或 task_id 无效")
    if payload.get("receipt_sha256") != receipt_sha256:
        raise DyroError("外部门禁证据未绑定当前回执")
    entries = payload.get("gates")
    if not isinstance(entries, list):
        raise ValidationError("外部门禁证据必须包含 gates 数组")
    expected = {gate.name for gate in task.gates}
    observed: dict[str, int] = {}
    logs: list[tuple[str, bytes]] = []
    evidence_root = gates.parent.resolve()
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("name"), str)
            or isinstance(entry.get("exit_code"), bool)
            or not isinstance(entry.get("exit_code"), int)
        ):
            raise ValidationError("外部门禁条目必须包含 name 和整数 exit_code")
        name = entry["name"]
        if name in observed:
            raise ValidationError(f"外部门禁证据重复声明门禁：{name}")
        log = entry.get("log")
        log_sha256 = entry.get("log_sha256")
        if (
            not isinstance(log, str)
            or not log
            or Path(log).is_absolute()
            or ".." in Path(log).parts
        ):
            raise ValidationError(
                f"外部门禁 {name} 必须提供 gates JSON 相对目录内的 log"
            )
        if not isinstance(log_sha256, str) or not re.fullmatch(
            r"[0-9a-f]{64}", log_sha256, re.IGNORECASE
        ):
            raise ValidationError(f"外部门禁 {name} 必须提供 log_sha256")
        log_path = (gates.parent / log).resolve()
        try:
            log_path.relative_to(evidence_root)
        except ValueError as exc:
            raise ValidationError(
                f"外部门禁 {name} 的 log 不得位于 gates JSON 目录外"
            ) from exc
        if not log_path.is_file():
            raise DyroError(f"外部门禁 {name} 的日志不存在：{log_path}")
        log_bytes = log_path.read_bytes()
        if hashlib.sha256(log_bytes).hexdigest() != log_sha256.lower():
            raise DyroError(f"外部门禁 {name} 的日志哈希不匹配")
        observed[name] = entry["exit_code"]
        logs.append((name, log_bytes))
    if set(observed) != expected:
        raise DyroError(
            f"外部门禁集合与任务不一致；期望 {', '.join(sorted(expected)) or '-'}"
        )
    failures = [name for name, exit_code in observed.items() if exit_code != 0]
    if failures:
        raise DyroError(f"外部门禁未通过：{', '.join(sorted(failures))}")
    return data, tuple(logs)


def _validate_external_heads(config: Config, task: Task, heads: Path | None) -> bytes:
    if heads is None:
        raise DyroError(
            f"任务 {task.id} 完成时必须提供 --heads，绑定执行后的逐仓 Git HEAD"
        )
    if not heads.is_file():
        raise DyroError(f"外部任务 HEAD 证据文件不存在：{heads}")
    data = heads.read_bytes()
    try:
        payload = json.loads(data)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"外部任务 HEAD 证据必须是 JSON：{heads}") from exc
    _validate_task_heads_payload(config, task, payload)
    return data


def _require_external_claim(config: Config, task: Task) -> dict[str, object]:
    if config.policy.execution_mode != "external":
        raise DyroError("导入外部证据要求 Profile 使用 execution_mode = external")
    _require_external_security(config)
    claim = _claim(task)
    if claim is None:
        raise DyroError(f"任务 {task.id} 尚未领取；请先运行 task claim")
    if _claim_expired(claim):
        raise DyroError(f"任务 {task.id} 的 claim 已过期；请重新领取")
    return claim


def run_gates(config: Config, task: Task, *, dry_run: bool = False) -> bool:
    _require_local_execution(config, "门禁", dry_run=dry_run)
    root = worktree_root(config, task)
    all_passed = True
    for index, gate in enumerate(task.gates, start=1):
        cwd = root / gate.cwd
        argv = expand_argv(
            gate.argv, workspace=root, root=config.root, task=task.id, line=task.line
        )
        result = run(argv, cwd=cwd, timeout=gate.timeout_seconds, dry_run=dry_run)
        _capture(task, f"gate-{index}.log", result.stdout, dry_run=dry_run)
        passed = result.code == 0
        all_passed = all_passed and passed
        if not dry_run:
            ledger(
                config,
                task.id,
                "gate",
                name=gate.name,
                argv=list(argv),
                passed=passed,
                exit_code=result.code,
            )
    return all_passed


def _resolve_run_executor(task: Task, executor_override: str | None) -> str:
    from .peer_wave import (
        AUTO_EXECUTOR,
        bind_wave_executors,
        discover_available_write_providers,
    )

    if executor_override:
        return executor_override
    if task.executor != AUTO_EXECUTOR:
        return task.executor
    decision = bind_wave_executors((task,), discover_available_write_providers())
    chosen = decision.executor_for(task.id)
    if chosen is None:
        reason = (
            decision.deferred[0].reason
            if decision.deferred
            else "无法绑定 auto executor"
        )
        raise ValidationError(reason)
    return chosen


def _execute_task_agent(
    config: Config,
    task: Task,
    *,
    workspace: Path,
    prompt: str,
    log_name: str,
    dry_run: bool,
    executor_override: str | None = None,
) -> object:
    from .peer_wave import assert_write_executor_allowed
    from .task_dispatch import is_dispatch_write_ready, run_task_bound_dispatch

    executor = _resolve_run_executor(task, executor_override)
    if task.risk == "write":
        assert_write_executor_allowed(executor, risk=task.risk)
    if is_dispatch_write_ready(executor):
        result = run_task_bound_dispatch(
            task,
            executor=executor,
            workspace=workspace,
            prompt=prompt,
            timeout_seconds=float(task.timeout_minutes * 60),
            dry_run=dry_run,
        )
    elif executor in config.adapters:
        argv = _adapter_argv(
            config,
            executor,
            "write" if task.risk == "write" else "read",
            workspace=workspace,
            prompt=prompt,
            task=task,
        )
        result = run(
            argv, cwd=workspace, timeout=task.timeout_minutes * 60, dry_run=dry_run
        )
    else:
        raise ValidationError(f"任务 {task.id} 使用的 Agent adapter 未配置：{executor}")
    _capture(task, log_name, result.stdout, dry_run=dry_run)
    return result


def run_task(
    config: Config,
    task: Task,
    *,
    dry_run: bool = False,
    legacy_scheduler: bool = False,
    expected_contract_sha256: str | None = None,
    executor_override: str | None = None,
) -> str:
    _require_local_execution(config, "任务", dry_run=dry_run)
    if dry_run:
        _assert_expected_task_contract(task, expected_contract_sha256)
        return _run_task(
            config, task, dry_run=True, executor_override=executor_override
        )
    with exclusive_lock(_execution_lock_path(task), timeout_seconds=1.0):
        try:
            _reserve_local_execution(
                config,
                task,
                allowed=("backlog", "assigned", "failed"),
                action="启动执行",
                dry_run=False,
                legacy_scheduler=legacy_scheduler,
                expected_contract_sha256=expected_contract_sha256,
            )
            attempt = begin_execution_attempt(
                task.directory,
                task.id,
                _execution_plan_snapshot(config, task),
            )
        except Exception:
            if status(config, task) == "in_progress":
                set_status(config, task, "failed")
            raise
        return _complete_execution_attempt(
            config,
            task,
            attempt,
            lambda: _run_task(
                config,
                task,
                dry_run=False,
                reserved=True,
                executor_override=executor_override,
            ),
        )


def _run_task(
    config: Config,
    task: Task,
    *,
    dry_run: bool,
    reserved: bool = False,
    executor_override: str | None = None,
) -> str:
    if not reserved:
        _reserve_local_execution(
            config,
            task,
            allowed=("backlog", "assigned", "failed"),
            action="启动执行",
            dry_run=dry_run,
        )
    try:
        line = get_line(config, task.line)
        workspace = _ensure_task_worktrees(config, task, line, dry_run=dry_run)
    except (DyroError, ValidationError):
        if not dry_run:
            set_status(config, task, "failed")
        raise
    result = _execute_task_agent(
        config,
        task,
        workspace=workspace,
        prompt=_prompt(task, "executor", workspace),
        log_name="executor.log",
        dry_run=dry_run,
        executor_override=executor_override,
    )
    if not dry_run:
        ledger(
            config,
            task.id,
            "executor",
            agent=task.executor,
            argv=list(result.argv),
            exit_code=result.code,
        )
    if result.code != 0:
        set_status(config, task, "failed", dry_run=dry_run)
        return "failed"
    if dry_run:
        return "dry-run"
    receipt = _receipt_result(task)
    if receipt == "QUESTION":
        set_status(config, task, "waiting_answer")
        return "waiting_answer"
    if receipt != "DONE":
        set_status(config, task, "failed")
        return "failed"
    if not run_gates(config, task):
        set_status(config, task, "failed")
        return "failed"
    try:
        task_heads_hash = _record_task_heads(config, task)
    except (DyroError, ValidationError):
        set_status(config, task, "failed")
        raise
    ledger(config, task.id, "execution_heads", task_heads_sha256=task_heads_hash)
    _set_quality_gate_status(config, task, "review")
    return "review"


def import_execution_evidence(
    config: Config,
    task: Task,
    *,
    receipt: Path,
    gates: Path | None = None,
    heads: Path | None = None,
    provenance: Path | None = None,
    allow_legacy_provenance: bool = False,
    dry_run: bool = False,
) -> str:
    with exclusive_lock(_state_lock_path(task)):
        return _import_execution_evidence(
            config,
            task,
            receipt=receipt,
            gates=gates,
            heads=heads,
            provenance=provenance,
            allow_legacy_provenance=allow_legacy_provenance,
            dry_run=dry_run,
        )


def _import_execution_evidence(
    config: Config,
    task: Task,
    *,
    receipt: Path,
    gates: Path | None = None,
    heads: Path | None = None,
    provenance: Path | None = None,
    allow_legacy_provenance: bool = False,
    dry_run: bool = False,
) -> str:
    """Import execution proof produced by the runner that claimed this task."""
    claim = _require_external_claim(config, task)
    current = status(config, task)
    if current not in ("assigned", "in_progress"):
        raise DyroError(f"仅 assigned 或 in_progress 任务可导入执行证据：{task.id}")
    if not receipt.is_file():
        raise DyroError(f"外部回执文件不存在：{receipt}")
    if provenance is None and not allow_legacy_provenance:
        raise DyroError(
            "外部执行证据缺少 provenance；旧证据必须显式使用 --allow-legacy"
        )
    receipt_bytes = receipt.read_bytes()
    receipt_hash = hashlib.sha256(receipt_bytes).hexdigest()
    receipt_lines = receipt_bytes.decode("utf-8").splitlines()
    receipt_match = RESULT_RE.match(receipt_lines[0]) if receipt_lines else None
    result = receipt_match.group(1).upper() if receipt_match else ""
    gate_bytes, gate_logs = (
        _validate_external_gates(task, receipt_hash, gates)
        if result == "DONE"
        else (b"", ())
    )
    task_heads_bytes = (
        _validate_external_heads(config, task, heads) if result == "DONE" else b""
    )
    claim_binding = (
        execution_claim_binding(task)
        if getattr(config.policy, "require_signed_execution", False)
        else None
    )
    expected_plan = external_execution_plan(
        task,
        config.policy.execution_mode,
        claim_binding=claim_binding,
    )
    from .signing import signature_key_id, trusted_key_principal, trusted_keys_directory

    external_attempt = import_external_execution_attempt(
        task.directory,
        task.id,
        provenance=provenance,
        receipt_sha256=receipt_hash,
        result=result,
        expected_plan=expected_plan,
        gates_sha256=hashlib.sha256(gate_bytes).hexdigest() if gate_bytes else "",
        task_heads_sha256=hashlib.sha256(task_heads_bytes).hexdigest()
        if task_heads_bytes
        else "",
        trusted_keys_dir=trusted_keys_directory(config.root, "execution"),
        require_signature=getattr(config.policy, "require_signed_execution", False),
        dry_run=True,
    )
    if (
        claim_binding is not None
        and signature_key_id(external_attempt) != claim_binding["execution_key_id"]
    ):
        raise ValidationError("execution signature key ID 与当前 claim 不匹配")
    if claim_binding is not None:
        execution_principal = trusted_key_principal(
            config.root,
            "execution",
            str(claim_binding["execution_key_id"]),
        )
        if (
            external_attempt.get("actor") != claim_binding["runner"]
            or execution_principal != claim_binding["runner"]
        ):
            raise ValidationError(
                "execution signature actor 必须等于当前 claim runner principal"
            )
    if dry_run:
        return (
            "review"
            if result == "DONE"
            else "waiting_answer"
            if result == "QUESTION"
            else "failed"
        )
    from .provenance import persist_external_execution_attempt

    generation_files: dict[str | Path, bytes] = {
        "receipt.md": receipt_bytes,
        "provenance.json": (
            json.dumps(external_attempt, ensure_ascii=False, sort_keys=True, indent=2)
            + "\n"
        ).encode("utf-8"),
    }
    if gate_bytes:
        generation_files["gates.json"] = gate_bytes
        for index, (_, log_bytes) in enumerate(gate_logs, start=1):
            generation_files[f"gates/gate-{index}.log"] = log_bytes
    if task_heads_bytes:
        generation_files[TASK_HEADS_FILE] = task_heads_bytes

    def collect_attempt_artifact(target: Path, content: bytes) -> None:
        generation_files[target.relative_to(task.directory)] = content

    persist_external_execution_attempt(
        task.directory,
        task.id,
        external_attempt,
        result=result,
        writer=collect_attempt_artifact,
    )
    publish_evidence_generation(
        task.directory,
        str(external_attempt["attempt_id"]),
        generation_files,
    )
    if current == "assigned":
        set_status(config, task, "in_progress")
    ledger(
        config,
        task.id,
        "external_execution_import",
        runner=claim["runner"],
        receipt_sha256=receipt_hash,
        task_heads_sha256=hashlib.sha256(task_heads_bytes).hexdigest()
        if task_heads_bytes
        else "",
        run_id=external_attempt["run_id"],
        attempt_id=external_attempt["attempt_id"],
        plan_sha256=external_attempt["plan_sha256"],
        legacy_provenance=bool(external_attempt.get("legacy_provenance", False)),
    )
    if result == "QUESTION":
        set_status(config, task, "waiting_answer")
        return "waiting_answer"
    if result != "DONE":
        set_status(config, task, "failed")
        return "failed"
    _set_quality_gate_status(config, task, "review")
    return "review"


def answer_task(
    config: Config, task: Task, answer: str, *, dry_run: bool = False
) -> str:
    if config.policy.execution_mode == "external":
        with exclusive_lock(_execution_lock_path(task), timeout_seconds=1.0):
            claim = _require_external_claim(config, task)
            if status(config, task) != "waiting_answer":
                raise DyroError(
                    f"任务 {task.id} 当前不是 waiting_answer，不能记录外部续跑答案"
                )
            if dry_run:
                return "dry-run"
            atomic_write_text(task.directory / "answers.md", answer.rstrip() + "\n")
            set_status(config, task, "assigned")
            ledger(
                config,
                task.id,
                "external_answer_recorded",
                runner=claim["runner"],
                answer_sha256=hashlib.sha256(answer.encode("utf-8")).hexdigest(),
            )
            return "assigned"
    _require_local_execution(config, "任务续跑", dry_run=dry_run)
    if dry_run:
        return _answer_task(config, task, answer, dry_run=True)
    with exclusive_lock(_execution_lock_path(task), timeout_seconds=1.0):
        _reserve_local_execution(
            config,
            task,
            allowed=("waiting_answer",),
            action="继续执行",
            dry_run=False,
        )
        parent_attempt_id = current_execution_attempt_id(task.directory)
        attempt = begin_execution_attempt(
            task.directory,
            task.id,
            _execution_plan_snapshot(
                config,
                task,
                continuation={
                    "parent_attempt_id": parent_attempt_id or "",
                    "answer_sha256": hashlib.sha256(answer.encode("utf-8")).hexdigest(),
                },
            ),
            parent_attempt_id=parent_attempt_id,
        )
        return _complete_execution_attempt(
            config,
            task,
            attempt,
            lambda: _answer_task(config, task, answer, dry_run=False, reserved=True),
        )


def _answer_task(
    config: Config, task: Task, answer: str, *, dry_run: bool, reserved: bool = False
) -> str:
    if not reserved:
        _reserve_local_execution(
            config,
            task,
            allowed=("waiting_answer",),
            action="继续执行",
            dry_run=dry_run,
        )
    if not dry_run:
        atomic_write_text(task.directory / "answers.md", answer.rstrip() + "\n")
    try:
        line = get_line(config, task.line)
        workspace = _ensure_task_worktrees(config, task, line, dry_run=dry_run)
    except (DyroError, ValidationError):
        if not dry_run:
            set_status(config, task, "failed")
        raise
    result = _execute_task_agent(
        config,
        task,
        workspace=workspace,
        prompt=_prompt(task, "continuation", workspace),
        log_name="executor-continuation.log",
        dry_run=dry_run,
    )
    if result.code != 0:
        set_status(config, task, "failed", dry_run=dry_run)
        return "failed"
    if dry_run:
        return "dry-run"
    receipt = _receipt_result(task)
    if receipt == "QUESTION":
        set_status(config, task, "waiting_answer")
        return "waiting_answer"
    if receipt != "DONE" or not run_gates(config, task):
        set_status(config, task, "failed")
        return "failed"
    try:
        task_heads_hash = _record_task_heads(config, task)
    except (DyroError, ValidationError):
        set_status(config, task, "failed")
        raise
    ledger(config, task.id, "execution_heads", task_heads_sha256=task_heads_hash)
    _set_quality_gate_status(config, task, "review")
    return "review"


def _apply_review_decision(config: Config, task: Task, *, dry_run: bool = False) -> str:
    verdict, reviewed_receipt_hash, reviewed_task_heads_hash = _review_decision(task)
    receipt_hash = _file_sha256(resolve_evidence_path(task.directory, "receipt.md"))
    task_heads_hash = _file_sha256(
        resolve_evidence_path(task.directory, TASK_HEADS_FILE)
    )
    review_content = (
        (task.directory / "review.md").read_text(encoding="utf-8")
        if (task.directory / "review.md").is_file()
        else ""
    )
    binding_matches, expected_binding, reviewed_binding = validate_review_binding(
        task.directory, review_content
    )
    if verdict in ("PASS", "FAIL") and not binding_matches:
        if not dry_run:
            ledger(
                config,
                task.id,
                "review_rejected",
                reason="attempt_id or plan_sha256 mismatch or missing",
                expected_attempt_id=expected_binding[0] if expected_binding else "",
                reviewed_attempt_id=reviewed_binding[0],
                expected_plan_sha256=expected_binding[1] if expected_binding else "",
                reviewed_plan_sha256=reviewed_binding[1],
            )
        return "review"
    if verdict == "PASS":
        if (
            reviewed_receipt_hash != receipt_hash
            or reviewed_task_heads_hash != task_heads_hash
        ):
            if not dry_run:
                ledger(
                    config,
                    task.id,
                    "review_rejected",
                    reason="receipt_sha256 or task_heads_sha256 mismatch or missing",
                    expected_receipt_sha256=receipt_hash,
                    reviewed_receipt_sha256=reviewed_receipt_hash,
                    expected_task_heads_sha256=task_heads_hash,
                    reviewed_task_heads_sha256=reviewed_task_heads_hash,
                )
            return "review"
        next_status = (
            "review_pending_signoff"
            if config.policy.require_external_signoff
            else "done"
        )
        if config.policy.execution_mode == "local":
            _assert_task_heads_current(config, task)
        _set_quality_gate_status(config, task, next_status, dry_run=dry_run)
        if not dry_run:
            ledger(
                config,
                task.id,
                "review_accepted",
                receipt_sha256=receipt_hash,
                task_heads_sha256=task_heads_hash,
                review_sha256=_file_sha256(task.directory / "review.md"),
                attempt_id=expected_binding[0] if expected_binding else "",
                plan_sha256=expected_binding[1] if expected_binding else "",
            )
        if next_status != "done":
            return next_status
        return "done"
    if verdict == "FAIL":
        set_status(config, task, "failed")
        return "failed"
    return "review"


def review_task(
    config: Config,
    task: Task,
    *,
    dry_run: bool = False,
    expected_contract_sha256: str | None = None,
) -> str:
    _require_local_execution(config, "复核", dry_run=dry_run)
    if dry_run:
        return _review_task(
            config,
            task,
            dry_run=True,
            expected_contract_sha256=expected_contract_sha256,
        )
    with exclusive_lock(_review_lock_path(task), timeout_seconds=1.0):
        return _review_task(
            config,
            task,
            dry_run=False,
            expected_contract_sha256=expected_contract_sha256,
        )


def _review_task(
    config: Config,
    task: Task,
    *,
    dry_run: bool,
    expected_contract_sha256: str | None = None,
) -> str:
    _assert_expected_task_contract(task, expected_contract_sha256)
    if status(config, task) != "review":
        raise DyroError(f"仅 review 任务可启动复核：{task.id}")
    workspace = worktree_root(config, task)
    if not workspace.exists():
        raise DyroError(f"任务 worktree 不存在：{workspace}")
    if not dry_run:
        _assert_task_heads_current(config, task)
    argv = _adapter_argv(
        config,
        task.reviewer,
        "read",
        workspace=workspace,
        prompt=_prompt(task, "reviewer", workspace),
        task=task,
    )
    result = run(
        argv, cwd=workspace, timeout=task.review_timeout_minutes * 60, dry_run=dry_run
    )
    _capture(task, "reviewer.log", result.stdout, dry_run=dry_run)
    if not dry_run:
        ledger(
            config,
            task.id,
            "review",
            agent=task.reviewer,
            argv=list(argv),
            exit_code=result.code,
        )
        try:
            _assert_task_heads_current(config, task)
        except (DyroError, ValidationError) as exc:
            ledger(config, task.id, "review_source_changed", error=str(exc))
            raise DyroError(
                f"复核期间任务源码发生变化，拒绝接受复核结果：{exc}"
            ) from exc
    if result.code != 0:
        return "review"
    if dry_run:
        return "dry-run"
    return _apply_review_decision(config, task)


def import_review_evidence(
    config: Config, task: Task, *, review: Path, dry_run: bool = False
) -> str:
    with exclusive_lock(_state_lock_path(task)):
        return _import_review_evidence(config, task, review=review, dry_run=dry_run)


def _import_review_evidence(
    config: Config, task: Task, *, review: Path, dry_run: bool = False
) -> str:
    """Import a receipt-bound independent review, signed when policy requires it."""
    if status(config, task) != "review":
        raise DyroError(f"仅 review 任务可导入复核证据：{task.id}")
    from .reviews import load_review_evidence
    from .signing import trusted_key_principal, trusted_keys_directory

    evidence = load_review_evidence(
        review,
        task_id=task.id,
        trust_directory=trusted_keys_directory(config.root, "review"),
        require_signature=getattr(config.policy, "require_signed_review", False),
    )
    if config.policy.execution_mode == "external":
        claim = _require_external_claim(config, task)
        if (
            not evidence.signed
            or evidence.key_id is None
            or evidence.principal_id is None
        ):
            raise ValidationError(
                "external review 必须使用带 principal 的 signed review"
            )
        execution_key_id = str(claim.get("execution_key_id", ""))
        execution_principal = trusted_key_principal(
            config.root, "execution", execution_key_id
        )
        if evidence.principal_id == execution_principal:
            raise ValidationError("execution claimant 不得复核自己的结果")
        reviewer = evidence.principal_id
    elif not evidence.signed:
        reviewer = "local-reviewer"
    else:
        reviewer = evidence.reviewer
    if dry_run:
        return "dry-run"
    atomic_write_bytes(task.directory / "review.md", evidence.content)
    if (
        evidence.signed
        and evidence.key_id is not None
        and evidence.principal_id is not None
    ):
        atomic_write_text(
            task.directory / REVIEW_IDENTITY_FILE,
            json.dumps(
                {
                    "task_id": task.id,
                    "key_id": evidence.key_id,
                    "principal_id": evidence.principal_id,
                    "review_sha256": _file_sha256(task.directory / "review.md"),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n",
        )
    ledger(
        config,
        task.id,
        "external_review_import",
        reviewer=reviewer,
        review_key_id=evidence.key_id or "",
        signed=evidence.signed,
        review_sha256=_file_sha256(task.directory / "review.md"),
    )
    return _apply_review_decision(config, task)


def signoff_task(
    config: Config,
    task: Task,
    *,
    approver: str,
    signing_key: Path | None = None,
    key_id: str | None = None,
    dry_run: bool = False,
) -> str:
    """Record a human or external-system approval for a receipt-bound review."""
    with exclusive_lock(_state_lock_path(task)):
        return _signoff_task(
            config,
            task,
            approver=approver,
            signing_key=signing_key,
            key_id=key_id,
            dry_run=dry_run,
        )


def _signoff_task(
    config: Config,
    task: Task,
    *,
    approver: str,
    signing_key: Path | None = None,
    key_id: str | None = None,
    dry_run: bool = False,
) -> str:
    """Perform one lock-held external sign-off state transition."""
    if not config.policy.require_external_signoff:
        raise DyroError("当前 Profile 未启用 require_external_signoff，无需签收")
    if status(config, task) != "review_pending_signoff":
        raise DyroError(f"仅 review_pending_signoff 任务可签收：{task.id}")
    approver = approver.strip()
    if not approver:
        raise ValidationError("签收人不能为空")
    verdict, reviewed_receipt_hash, reviewed_task_heads_hash = _review_decision(task)
    receipt_hash = _file_sha256(resolve_evidence_path(task.directory, "receipt.md"))
    task_heads_hash = _file_sha256(
        resolve_evidence_path(task.directory, TASK_HEADS_FILE)
    )
    if (
        verdict != "PASS"
        or reviewed_receipt_hash != receipt_hash
        or reviewed_task_heads_hash != task_heads_hash
    ):
        raise DyroError("复核结论未通过或未绑定当前回执与任务 HEAD；请重新复核")
    review_content = (task.directory / "review.md").read_text(encoding="utf-8")
    binding_matches, expected_binding, _ = validate_review_binding(
        task.directory, review_content
    )
    if not binding_matches:
        raise DyroError("复核结论未绑定当前 execution attempt 与 plan；请重新复核")
    signoff = {
        "task_id": task.id,
        "approver": approver,
        "actor": approver,
        "receipt_sha256": receipt_hash,
        "task_heads_sha256": task_heads_hash,
        "review_sha256": _file_sha256(task.directory / "review.md"),
        "attempt_id": expected_binding[0] if expected_binding else "",
        "plan_sha256": expected_binding[1] if expected_binding else "",
        "signed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if (signing_key is None) != (key_id is None):
        raise ValidationError("--signing-key 与 --key-id 必须同时提供")
    from .signing import (
        sign_record,
        trusted_key_principal,
        trusted_keys_directory,
        verify_record,
    )

    if signing_key is not None and key_id is not None:
        signoff = sign_record(
            signoff,
            purpose="signoff",
            key_id=key_id,
            private_key=signing_key.expanduser().resolve(),
        )
    verify_record(
        signoff,
        purpose="signoff",
        trust_directory=trusted_keys_directory(config.root, "signoff"),
        required=getattr(config.policy, "require_signed_signoff", False),
    )
    if config.policy.execution_mode == "external":
        if key_id is None:
            raise ValidationError("external signoff 必须提供 --signing-key 与 --key-id")
        approver_principal = trusted_key_principal(config.root, "signoff", key_id)
        if approver_principal != approver or signoff.get("actor") != approver:
            raise ValidationError("signoff actor 必须等于 signoff key 的 principal")
        execution_principal, reviewer_principal = (
            _external_execution_and_reviewer_principals(config, task)
        )
        if approver_principal in (execution_principal, reviewer_principal):
            raise ValidationError(
                "signoff principal 必须独立于 execution 与 review principal"
            )
    if not dry_run:
        atomic_write_text(
            task.directory / "signoff.json",
            json.dumps(signoff, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        )
        _set_quality_gate_status(config, task, "done")
        ledger(
            config,
            task.id,
            "signoff",
            approver=approver,
            receipt_sha256=receipt_hash,
            task_heads_sha256=task_heads_hash,
            attempt_id=expected_binding[0] if expected_binding else "",
            plan_sha256=expected_binding[1] if expected_binding else "",
            signature_key_id=key_id or "",
        )
    return "done" if not dry_run else "dry-run"


def _prepare_merge(
    config: Config,
    task: Task,
    *,
    push: bool,
    dry_run: bool,
) -> tuple[Line, tuple[MergePlan, ...]]:
    if push and not config.policy.allow_push:
        raise DyroError(
            "当前 Profile 禁止 push；请在 dyro.toml 的 policy.allow_push 显式开启"
        )
    line = get_line(config, task.line)
    task_heads = _assert_task_heads_current(config, task)
    plans: list[MergePlan] = []
    for repo_id in task.repositories:
        target = line_repository_path(config, line, repo_id)
        if git(target, "rev-parse", "--is-inside-work-tree").stdout.strip() != "true":
            raise DyroError(f"开发线 worktree 不存在或不是 Git：{target}")
        dirty = require_ok(
            git(target, "status", "--porcelain=v1", "-uall"), f"读取 {repo_id} 状态"
        ).stdout.strip()
        if dirty:
            raise DyroError(f"开发线仓库不干净，拒绝合并：{target}")
        current = require_ok(
            git(target, "branch", "--show-current"), f"读取 {repo_id} 分支"
        ).stdout.strip()
        if current != line.branch:
            raise DyroError(
                f"开发线仓库分支错误：{target} 当前 {current or 'DETACHED'}，期望 {line.branch}"
            )
        original_head = require_ok(
            git(target, "rev-parse", "HEAD"), f"读取 {repo_id} 开发线 HEAD"
        ).stdout.strip()
        plans.append(MergePlan(repo_id, target, task_heads[repo_id], original_head))
    if push:
        for plan in plans:
            require_ok(
                git(
                    plan.target,
                    "push",
                    "--dry-run",
                    "origin",
                    line.branch,
                    dry_run=dry_run,
                ),
                f"预检推送 {plan.repository}",
            )
    return line, tuple(plans)


def _rollback_merges(
    plans: Iterable[MergePlan], committed_heads: dict[str, str]
) -> list[str]:
    failures: list[str] = []
    for plan in reversed(tuple(plans)):
        merge_head = git(plan.target, "rev-parse", "--verify", "-q", "MERGE_HEAD")
        if merge_head.code == 0:
            result = git(plan.target, "merge", "--abort")
        else:
            committed_head = committed_heads.get(plan.repository)
            if committed_head is None:
                continue
            current = git(plan.target, "rev-parse", "HEAD")
            if current.code != 0:
                failures.append(f"{plan.repository}: cannot read HEAD during rollback")
                continue
            if current.stdout.strip() != committed_head:
                failures.append(
                    f"{plan.repository}: HEAD changed concurrently; manual recovery required"
                )
                continue
            result = git(plan.target, "reset", "--keep", plan.original_head)
        if result.code != 0:
            failures.append(
                f"{plan.repository}: {result.stdout.strip() or 'rollback failed'}"
            )
    return failures


def _merge_task_repositories(
    config: Config, task: Task, *, push: bool, dry_run: bool
) -> None:
    # Serialize merges into the same delivery line across concurrent dyro processes.
    with exclusive_lock(
        _merge_lock_path(config, task.line), timeout_seconds=MERGE_LOCK_TIMEOUT_SECONDS
    ):
        _merge_task_repositories_locked(config, task, push=push, dry_run=dry_run)


def _merge_task_repositories_locked(
    config: Config, task: Task, *, push: bool, dry_run: bool
) -> None:
    line, plans = _prepare_merge(config, task, push=push, dry_run=dry_run)
    message = f"merge(task): {task.id} {task.title}"
    if dry_run:
        for plan in plans:
            require_ok(
                git(
                    plan.target,
                    "merge",
                    "--no-ff",
                    "--no-commit",
                    plan.source_head,
                    dry_run=True,
                    timeout=300,
                ),
                f"合并 {plan.repository}",
            )
        return

    committed_heads: dict[str, str] = {}
    try:
        for plan in plans:
            result = git(
                plan.target,
                "merge",
                "--no-ff",
                "--no-commit",
                plan.source_head,
                timeout=300,
            )
            require_ok(result, f"合并 {plan.repository}")
        for plan in plans:
            if git(plan.target, "rev-parse", "--verify", "-q", "MERGE_HEAD").code == 0:
                require_ok(
                    git(plan.target, "commit", "-m", message, timeout=300),
                    f"提交 {plan.repository} 合并",
                )
                committed_heads[plan.repository] = require_ok(
                    git(plan.target, "rev-parse", "HEAD"),
                    f"读取 {plan.repository} 合并提交",
                ).stdout.strip()
    except DyroError as exc:
        recovery_failures = _rollback_merges(plans, committed_heads)
        ledger(
            config,
            task.id,
            "merge_failed",
            error=str(exc),
            recovered=not recovery_failures,
            recovery_failures=recovery_failures,
        )
        if recovery_failures:
            raise DyroError(
                f"{exc}\n自动恢复未完全成功：{'; '.join(recovery_failures)}"
            ) from exc
        raise

    pushed: list[str] = []
    if push:
        for plan in plans:
            result = git(plan.target, "push", "origin", line.branch)
            if result.code != 0:
                ledger(
                    config,
                    task.id,
                    "push_failed",
                    repository=plan.repository,
                    pushed_repositories=pushed,
                    error=result.stdout.strip(),
                )
                raise DyroError(
                    f"推送 {plan.repository} 失败；本地合并已保留，已推送仓库：{', '.join(pushed) or '-'}"
                    f"\n{result.stdout.strip()}"
                )
            pushed.append(plan.repository)

    for plan in plans:
        result_head = require_ok(
            git(plan.target, "rev-parse", "HEAD"), f"读取 {plan.repository} 合并结果"
        ).stdout.strip()
        ledger(
            config,
            task.id,
            "merge",
            repository=plan.repository,
            branch=line.branch,
            source_head=plan.source_head,
            previous_head=plan.original_head,
            result_head=result_head,
            pushed=push,
        )


def merge_task(
    config: Config, task: Task, *, push: bool = False, dry_run: bool = False
) -> None:
    _require_local_execution(config, "合并", dry_run=dry_run)
    if status(config, task) != "done":
        raise DyroError(f"仅 done 任务可合并：{task.id}")
    if not _valid_review_acceptance(config, task):
        raise DyroError(
            "仅具有有效的独立复核、当前回执与任务 HEAD 绑定的 done 任务可合并（PROOF_DECAYED）"
        )
    if config.policy.require_external_signoff and not _valid_external_signoff(
        config, task
    ):
        raise DyroError("当前 Profile 要求有效的外部签收后才能合并（PROOF_DECAYED）")
    _merge_task_repositories(config, task, push=push, dry_run=dry_run)


def maintain_evidence_generations(
    config: Config,
    task: Task,
    *,
    prune: bool = False,
    older_than_days: int = 30,
    keep: int = 10,
    dry_run: bool = False,
) -> tuple[tuple[EvidenceGeneration, ...], tuple[EvidenceGeneration, ...]]:
    with exclusive_lock(_state_lock_path(task)):
        records = list_evidence_generations(task.directory)
        targets: tuple[EvidenceGeneration, ...] = ()
        if prune:
            targets = cleanup_evidence_generations(
                task.directory,
                older_than_days=older_than_days,
                keep=keep,
                dry_run=dry_run,
            )
            if not dry_run:
                ledger(
                    config,
                    task.id,
                    "evidence_generation_cleanup",
                    removed=[record.generation_id for record in targets],
                    older_than_days=older_than_days,
                    keep=keep,
                )
        return records, targets


def board(config: Config) -> str:
    rows = [
        "# DyroEngineeringFlow task board",
        "",
        "| Task | Line | Status | Risk | Depends |",
        "| --- | --- | --- | --- | --- |",
    ]
    for task in list_tasks(config):
        rows.append(
            f"| {task.id} | {task.line} | {status(config, task)} | {task.risk} | {', '.join(task.depends_on) or '-'} |"
        )
    return "\n".join(rows) + "\n"


def stats(config: Config) -> dict[str, dict[str, int]]:
    if not config.ledger_file.exists():
        return {}
    result: dict[str, dict[str, int]] = {}
    for line in config.ledger_file.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        agent = event.get("agent")
        if not agent:
            continue
        counters = result.setdefault(
            str(agent), {"executor": 0, "executor_ok": 0, "review": 0, "review_ok": 0}
        )
        if event.get("phase") == "executor":
            counters["executor"] += 1
            if event.get("exit_code") == 0:
                counters["executor_ok"] += 1
        if event.get("phase") == "review":
            counters["review"] += 1
            if event.get("exit_code") == 0:
                counters["review_ok"] += 1
    return result


def loop_tasks(config: Config, *, dry_run: bool = False) -> list[tuple[str, str]]:
    """Run every dispatchable queued task serially, then review newly-ready tasks.

    This is deliberately deterministic.  `task daemon` is the concurrent
    scheduler; `task loop` is the inspectable, one-pass coordination command.
    """
    from .continuation.store import assert_legacy_scheduler_allowed

    all_tasks = list_tasks(config)
    assert_legacy_scheduler_allowed(config, (task.id for task in all_tasks))
    outcomes: list[tuple[str, str]] = []
    schedule = plan_tasks(config, candidates=all_tasks)
    blocked = {item.task.id: item for item in schedule.blocked}
    ready_ids = {task.id for task in schedule.ready}
    for task in all_tasks:
        if task.id in blocked:
            outcomes.append((task.id, f"skipped: {blocked[task.id].reason}"))
            continue
        if task.id not in ready_ids:
            continue
        try:
            outcomes.append(
                (
                    task.id,
                    run_task(config, task, dry_run=dry_run, legacy_scheduler=True),
                )
            )
        except DyroError as exc:
            outcomes.append((task.id, f"skipped: {exc}"))
    for task in plan_tasks(config).review:
        try:
            outcomes.append((task.id, review_task(config, task, dry_run=dry_run)))
        except DyroError as exc:
            outcomes.append((task.id, f"review pending: {exc}"))
    return outcomes


def task_template(
    task_id: str, title: str, line: str, repository: str, mount: str
) -> str:
    quoted_title = json.dumps(title, ensure_ascii=False)
    quoted_mount = json.dumps(mount, ensure_ascii=False)
    return f'''schema_version = 1
id = "{task_id}"
title = {quoted_title}
line = "{line}"
risk = "write"
timeout_minutes = 60
review_timeout_minutes = 45
depends_on = []
blocked_on = []
# Overlapping slices must share a conflict_group; distinct slices stay parallel.
conflict_group = ""

[executor]
agent = "codex"

[reviewer]
agent = "codex"

[[repositories]]
id = "{repository}"

[[gates]]
name = "diff-check"
argv = ["git", "diff", "--check"]
cwd = {quoted_mount}
timeout_seconds = 120

[merge]
auto = false
push = false
'''

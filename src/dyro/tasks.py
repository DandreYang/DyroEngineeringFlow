from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import tomllib
from typing import Any, Iterable

from .config import Config, expand_argv, strict_bool, validate_id
from .errors import DyroError, ValidationError
from .process import git, require_ok, run
from .state import append_text, atomic_write_bytes, atomic_write_text, exclusive_lock
from .workspace import Line, get_line, line_repository_path, repository_path


STATUSES = ("backlog", "assigned", "in_progress", "waiting_answer", "review", "review_pending_signoff", "done", "failed")
TRANSITIONS = {
    "backlog": {"assigned"},
    "assigned": {"in_progress", "failed"},
    "in_progress": {"waiting_answer", "review", "failed"},
    "waiting_answer": {"in_progress", "failed"},
    "review": {"review_pending_signoff", "done", "failed"},
    "review_pending_signoff": {"done", "failed"},
    "failed": {"assigned"},
    "done": set(),
}
RESULT_RE = re.compile(r"^result:\s*(DONE|BLOCKED|QUESTION)\s*$", re.IGNORECASE)
VERDICT_RE = re.compile(r"^verdict:\s*(PASS|FAIL)\s*$", re.IGNORECASE)
RECEIPT_SHA_RE = re.compile(r"^receipt_sha256:\s*([0-9a-f]{64})\s*$", re.IGNORECASE | re.MULTILINE)
TASK_HEADS_SHA_RE = re.compile(r"^task_heads_sha256:\s*([0-9a-f]{64})\s*$", re.IGNORECASE | re.MULTILINE)
GIT_HEAD_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$", re.IGNORECASE)
TASK_HEADS_FILE = "task-heads.json"


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
    if not isinstance(raw, list) or not all(isinstance(item, str) and item for item in raw):
        raise ValidationError(f"{label} 必须是字符串数组")
    return tuple(raw)


def _parse_task(path: Path) -> Task:
    try:
        raw = tomllib.loads((path / "task.toml").read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ValidationError(f"任务清单格式错误 {path}: {exc}") from exc
    if raw.get("schema_version") != 1:
        raise ValidationError(f"任务清单必须使用 schema_version = 1：{path}")
    task_id = validate_id(str(raw.get("id", "")), "任务 ID")
    title = str(raw.get("title", "")).strip()
    line = validate_id(str(raw.get("line", "")), "任务开发线")
    risk = str(raw.get("risk", "write"))
    if not title or risk not in ("read", "write"):
        raise ValidationError(f"任务 {task_id} 的 title 或 risk 无效")
    executor = str(raw.get("executor", {}).get("agent", "")).strip()
    reviewer = str(raw.get("reviewer", {}).get("agent", "")).strip()
    if not executor or not reviewer:
        raise ValidationError(f"任务 {task_id} 必须配置 executor.agent 与 reviewer.agent")
    repo_entries = raw.get("repositories", [])
    if not isinstance(repo_entries, list) or not repo_entries:
        raise ValidationError(f"任务 {task_id} 至少包含一个 [[repositories]]")
    repositories: list[str] = []
    for entry in repo_entries:
        if not isinstance(entry, dict):
            raise ValidationError(f"任务 {task_id} repositories 结构无效")
        repositories.append(validate_id(str(entry.get("id", "")), "任务仓库 id"))
    if len(set(repositories)) != len(repositories):
        raise ValidationError(f"任务 {task_id} repositories 不能重复")
    gates: list[Gate] = []
    for entry in raw.get("gates", []):
        if not isinstance(entry, dict):
            raise ValidationError(f"任务 {task_id} gates 结构无效")
        name = str(entry.get("name", "")).strip()
        argv = entry.get("argv")
        cwd = str(entry.get("cwd", "."))
        if not name or not isinstance(argv, list) or not argv or not all(isinstance(item, str) and item for item in argv):
            raise ValidationError(f"任务 {task_id} gate 必须包含 name 与 argv 数组")
        cwd_path = Path(cwd)
        if cwd_path.is_absolute() or ".." in cwd_path.parts:
            raise ValidationError(f"任务 {task_id} gate cwd 必须位于 task worktree 内")
        if any(gate.name == name for gate in gates):
            raise ValidationError(f"任务 {task_id} gate 名称不能重复：{name}")
        gates.append(Gate(name, tuple(argv), cwd, int(entry.get("timeout_seconds", 1800))))
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
        conflict_group=str(raw.get("conflict_group", "")),
        timeout_minutes=int(raw.get("timeout_minutes", 60)),
        review_timeout_minutes=int(raw.get("review_timeout_minutes", 45)),
        gates=tuple(gates),
        merge_auto=strict_bool(merge.get("auto", False), "merge.auto"),
        merge_push=strict_bool(merge.get("push", False), "merge.push"),
        directory=path,
    )


def load_task(config: Config, task_id: str) -> Task:
    task = _parse_task(task_dir(config, task_id))
    if task.id != task_id:
        raise ValidationError(f"目录任务 ID 与 task.toml 不一致：{task_id} != {task.id}")
    unknown = [repo_id for repo_id in task.repositories if repo_id not in config.repositories]
    if unknown:
        raise ValidationError(f"任务 {task.id} 引用了未配置仓库：{', '.join(unknown)}")
    get_line(config, task.line)
    return task


def list_tasks(config: Config) -> list[Task]:
    if not config.task_specs_dir.exists():
        return []
    return [load_task(config, path.parent.name) for path in sorted(config.task_specs_dir.glob("*/task.toml"))]


def status(config: Config, task: Task) -> str:
    file = task.directory / "status"
    current = file.read_text(encoding="utf-8").strip() if file.exists() else "backlog"
    if current not in STATUSES:
        raise ValidationError(f"任务 {task.id} 状态非法：{current}")
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


def _claim(task: Task) -> dict[str, object] | None:
    path = _claim_path(task)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"任务 {task.id} 领取记录格式错误") from exc
    if not isinstance(payload, dict) or payload.get("task_id") != task.id or not isinstance(payload.get("runner"), str):
        raise ValidationError(f"任务 {task.id} 领取记录无效")
    return payload


def claim_task(config: Config, task: Task, *, runner: str, dry_run: bool = False) -> str:
    """Atomically reserve an external task for one runner identity."""
    if config.policy.execution_mode != "external":
        raise DyroError("task claim 仅用于 execution_mode = external 的 Profile")
    runner = runner.strip()
    if not runner:
        raise ValidationError("执行器标识不能为空")
    with exclusive_lock(_dispatch_lock_path(config)):
        with exclusive_lock(_state_lock_path(task)):
            check_dispatchable(config, task)
            current = status(config, task)
            if current not in ("backlog", "assigned"):
                raise DyroError(f"仅 backlog 或 assigned 任务可领取：{task.id}")
            if _claim(task) is not None:
                raise DyroError(f"任务 {task.id} 已被领取")
            if dry_run:
                return "assigned"
            payload = {
                "task_id": task.id,
                "runner": runner,
                "claimed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            atomic_write_text(_claim_path(task), json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
            if current == "backlog":
                set_status(config, task, "assigned")
            ledger(config, task.id, "claim", runner=runner)
            return "assigned"


def set_status(config: Config, task: Task, next_status: str, *, force: bool = False, dry_run: bool = False) -> None:
    if next_status not in STATUSES:
        raise ValidationError(f"非法状态 {next_status}，可选：{', '.join(STATUSES)}")
    with exclusive_lock(_state_lock_path(task)):
        current = status(config, task)
        if current == next_status:
            return
        if config.policy.require_external_signoff and next_status == "done" and not _valid_external_signoff(config, task):
            raise DyroError("当前 Profile 要求外部签收；请先使用 task signoff 写入与回执、复核绑定的签收记录")
        if not force and next_status not in TRANSITIONS[current]:
            raise DyroError(f"拒绝状态跳转 {current} -> {next_status}；如确有人工恢复需求，使用 --force 并留下审计记录")
        if not dry_run:
            atomic_write_text(task.directory / "status", next_status + "\n")
            ledger(config, task.id, "status", from_status=current, to_status=next_status)


def ledger(config: Config, task_id: str, phase: str, **fields: object) -> None:
    payload = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "task_id": task_id, "phase": phase, **fields}
    with exclusive_lock(config.root / ".dyro" / "ledger.lock"):
        append_text(config.ledger_file, json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


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
    return {str(key): str(value.get("status", "open")) for key, value in entries.items() if isinstance(value, dict)}


def check_dispatchable(config: Config, task: Task) -> None:
    states = decisions(config)
    unresolved = [decision for decision in task.blocked_on if states.get(decision) != "resolved"]
    if unresolved:
        raise DyroError(f"任务 {task.id} 被未 resolved 的决策点阻塞：{', '.join(unresolved)}")
    for dependency in task.depends_on:
        dependency_task = load_task(config, dependency)
        if status(config, dependency_task) != "done":
            raise DyroError(f"任务 {task.id} 依赖 {dependency}，当前状态为 {status(config, dependency_task)}")
    if task.conflict_group:
        active_states = ("assigned", "in_progress") if config.policy.execution_mode == "external" else ("in_progress",)
        active = [
            other.id
            for other in list_tasks(config)
            if other.id != task.id
            and other.conflict_group == task.conflict_group
            and status(config, other) in active_states
        ]
        if active:
            raise DyroError(f"任务 {task.id} 与活跃任务 {', '.join(active)} 共用冲突组 {task.conflict_group}")


def _reserve_local_execution(
    config: Config,
    task: Task,
    *,
    allowed: tuple[str, ...],
    action: str,
    dry_run: bool,
) -> None:
    """Check dispatch constraints and atomically reserve the task before starting an Agent."""
    with exclusive_lock(_dispatch_lock_path(config)):
        with exclusive_lock(_state_lock_path(task)):
            current = status(config, task)
            if current not in allowed:
                raise DyroError(f"任务 {task.id} 当前为 {current}，不能{action}")
            check_dispatchable(config, task)
            if dry_run:
                return
            if current in ("backlog", "failed"):
                set_status(config, task, "assigned", force=current == "failed")
            set_status(config, task, "in_progress")


def worktree_root(config: Config, task: Task) -> Path:
    return config.root / config.layout.tasks / task.line / task.id


def _resolved_git_common_dir(path: Path) -> Path:
    raw = require_ok(git(path, "rev-parse", "--git-common-dir"), f"读取 Git common dir：{path}").stdout.strip()
    common_dir = Path(raw)
    return common_dir.resolve() if common_dir.is_absolute() else (path / common_dir).resolve()


def _validate_task_worktree(config: Config, task: Task, repo_id: str, destination: Path, branch: str) -> None:
    if git(destination, "rev-parse", "--is-inside-work-tree").stdout.strip() != "true":
        raise DyroError(f"不是有效的任务 Git worktree：{destination}")
    top_level = require_ok(git(destination, "rev-parse", "--show-toplevel"), f"读取 {repo_id} worktree 根目录").stdout.strip()
    if Path(top_level).resolve() != destination.resolve():
        raise DyroError(f"任务 worktree 根目录错误：{destination} 实际为 {top_level}")
    current = require_ok(git(destination, "branch", "--show-current"), f"读取 {repo_id} 任务分支").stdout.strip()
    if current != branch:
        raise DyroError(f"任务 worktree 分支错误：{destination} 当前 {current or 'DETACHED'}，期望 {branch}")
    anchor = repository_path(config, repo_id)
    if _resolved_git_common_dir(destination) != _resolved_git_common_dir(anchor):
        raise DyroError(f"任务 worktree 不属于配置的仓库 anchor：{destination}")


def _ensure_task_worktrees(config: Config, task: Task, line: Line, *, dry_run: bool = False) -> Path:
    root = worktree_root(config, task)
    branch = f"{config.policy.task_branch_prefix}{task.id}"
    not_on_line = [repo_id for repo_id in task.repositories if repo_id not in line.repositories]
    if not_on_line:
        raise ValidationError(f"任务 {task.id} 引用的仓库不在开发线 {line.id}：{', '.join(not_on_line)}")
    for repo_id in task.repositories:
        anchor = repository_path(config, repo_id)
        destination = root / config.repositories[repo_id].mount
        if destination.exists():
            _validate_task_worktree(config, task, repo_id, destination, branch)
            continue
        require_ok(git(anchor, "rev-parse", "--verify", f"{line.branch}^{{commit}}"), f"校验 {repo_id} 开发线基线")
        branch_exists = git(anchor, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}").code == 0
        command: tuple[str, ...] = ("worktree", "add")
        if not branch_exists:
            command += ("-b", branch)
        command += (str(destination), branch if branch_exists else line.branch)
        if not dry_run:
            destination.parent.mkdir(parents=True, exist_ok=True)
        require_ok(git(anchor, *command, dry_run=dry_run, timeout=300), f"创建任务 worktree {repo_id}")
    return root


def _collect_task_heads(config: Config, task: Task) -> dict[str, str]:
    branch = f"{config.policy.task_branch_prefix}{task.id}"
    root = worktree_root(config, task)
    heads: dict[str, str] = {}
    for repo_id in task.repositories:
        destination = root / config.repositories[repo_id].mount
        _validate_task_worktree(config, task, repo_id, destination, branch)
        dirty = require_ok(
            git(destination, "status", "--porcelain=v1", "-uall"), f"读取 {repo_id} 任务 worktree 状态"
        ).stdout.strip()
        if dirty:
            raise DyroError(f"任务 worktree 不干净，必须先提交全部改动：{destination}")
        heads[repo_id] = require_ok(git(destination, "rev-parse", "HEAD"), f"读取 {repo_id} 任务 HEAD").stdout.strip()
    return heads


def _task_heads_payload(config: Config, task: Task, heads: dict[str, str]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "task_id": task.id,
        "line": task.line,
        "branch": f"{config.policy.task_branch_prefix}{task.id}",
        "repositories": heads,
    }


def _validate_task_heads_payload(config: Config, task: Task, payload: object) -> dict[str, str]:
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
        raise ValidationError("任务 HEAD 证据的 schema_version、task_id、line、branch 或 repositories 无效")
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
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def _record_task_heads(config: Config, task: Task) -> str:
    content = _serialize_task_heads(config, task, _collect_task_heads(config, task))
    target = task.directory / TASK_HEADS_FILE
    atomic_write_bytes(target, content)
    return hashlib.sha256(content).hexdigest()


def _load_task_heads(config: Config, task: Task) -> dict[str, str]:
    path = task.directory / TASK_HEADS_FILE
    if not path.is_file():
        raise DyroError(f"缺少任务 HEAD 证据：{path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"任务 HEAD 证据不是有效 JSON：{path}") from exc
    return _validate_task_heads_payload(config, task, payload)


def _assert_task_heads_current(config: Config, task: Task) -> dict[str, str]:
    expected = _load_task_heads(config, task)
    current = _collect_task_heads(config, task)
    if current != expected:
        changed = sorted(repo_id for repo_id in task.repositories if current.get(repo_id) != expected.get(repo_id))
        raise DyroError(f"任务代码已偏离已记录 HEAD，必须重新执行与复核：{', '.join(changed)}")
    return expected


def _receipt_result(task: Task) -> str:
    receipt = task.directory / "receipt.md"
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
    verdict = VERDICT_RE.match(lines[0]).group(1).upper() if lines and VERDICT_RE.match(lines[0]) else ""
    receipt_hash = RECEIPT_SHA_RE.search(content)
    task_heads_hash = TASK_HEADS_SHA_RE.search(content)
    return (
        verdict,
        receipt_hash.group(1).lower() if receipt_hash else "",
        task_heads_hash.group(1).lower() if task_heads_hash else "",
    )


def _valid_external_signoff(config: Config, task: Task) -> bool:
    signoff_path = task.directory / "signoff.json"
    if not signoff_path.is_file():
        return False
    try:
        signoff = json.loads(signoff_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    if not isinstance(signoff, dict) or not isinstance(signoff.get("approver"), str) or not signoff["approver"].strip():
        return False
    verdict, reviewed_receipt_hash, reviewed_task_heads_hash = _review_decision(task)
    try:
        receipt_hash = _file_sha256(task.directory / "receipt.md")
        review_hash = _file_sha256(task.directory / "review.md")
        task_heads_hash = _file_sha256(task.directory / TASK_HEADS_FILE)
    except DyroError:
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
    )


def _require_local_execution(config: Config, action: str, *, dry_run: bool) -> None:
    if config.policy.execution_mode == "external" and not dry_run:
        raise DyroError(
            f"当前 Profile 要求外部隔离执行器；本机 dyro 不能执行 {action}。"
            "请由受信任的外部 runner 运行并导入其证据，或仅使用 --dry-run 进行计划核验。"
        )


def _adapter_argv(config: Config, agent: str, mode: str, *, workspace: Path, prompt: str, task: Task) -> tuple[str, ...]:
    try:
        adapter = config.adapters[agent]
    except KeyError as exc:
        raise ValidationError(f"任务 {task.id} 使用的 Agent adapter 未配置：{agent}") from exc
    template = adapter.write if mode == "write" else adapter.read
    return expand_argv(template, workspace=workspace, root=config.root, prompt=prompt, task=task.id, line=task.line)


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
    return (
        f"你是独立复核位。阅读 {handoff}、{receipt} 与 {task_heads}；"
        f"在 {workspace} 用只读证据核验规格、回归、测试和越界改动。"
        f"在 {review} 写复核，首行必须是 verdict: PASS 或 verdict: FAIL，并写入 "
        "receipt_sha256: <所读取回执的 SHA-256> 与 "
        "task_heads_sha256: <所读取任务 HEAD 证据的 SHA-256>；"
        "禁止修改任何源码、push 或合并。"
    )


def _capture(task: Task, filename: str, output: str, *, dry_run: bool = False) -> Path:
    target = task.directory / "logs" / filename
    if not dry_run:
        atomic_write_text(target, output)
    return target


def _copy_external_evidence(task: Task, source: Path, target_name: str, *, dry_run: bool = False) -> Path:
    if not source.is_file():
        raise DyroError(f"外部证据文件不存在：{source}")
    relative = Path(target_name)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValidationError(f"外部证据目标路径非法：{target_name}")
    target = task.directory / relative
    if not dry_run:
        atomic_write_bytes(target, source.read_bytes())
    return target


def _validate_external_gates(task: Task, receipt_sha256: str, gates: Path | None) -> tuple[bytes, tuple[tuple[str, Path], ...]]:
    if gates is None:
        if task.gates:
            raise DyroError(f"任务 {task.id} 配置了门禁，导入执行证据时必须提供 --gates")
        return b"", ()
    if not gates.is_file():
        raise DyroError(f"外部门禁证据文件不存在：{gates}")
    data = gates.read_bytes()
    try:
        payload = json.loads(data)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"外部门禁证据必须是 JSON：{gates}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1 or payload.get("task_id") != task.id:
        raise ValidationError("外部门禁证据的 schema_version 或 task_id 无效")
    if payload.get("receipt_sha256") != receipt_sha256:
        raise DyroError("外部门禁证据未绑定当前回执")
    entries = payload.get("gates")
    if not isinstance(entries, list):
        raise ValidationError("外部门禁证据必须包含 gates 数组")
    expected = {gate.name for gate in task.gates}
    observed: dict[str, int] = {}
    logs: list[tuple[str, Path]] = []
    evidence_root = gates.parent.resolve()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str) or isinstance(entry.get("exit_code"), bool) or not isinstance(entry.get("exit_code"), int):
            raise ValidationError("外部门禁条目必须包含 name 和整数 exit_code")
        name = entry["name"]
        if name in observed:
            raise ValidationError(f"外部门禁证据重复声明门禁：{name}")
        log = entry.get("log")
        log_sha256 = entry.get("log_sha256")
        if not isinstance(log, str) or not log or Path(log).is_absolute() or ".." in Path(log).parts:
            raise ValidationError(f"外部门禁 {name} 必须提供 gates JSON 相对目录内的 log")
        if not isinstance(log_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", log_sha256, re.IGNORECASE):
            raise ValidationError(f"外部门禁 {name} 必须提供 log_sha256")
        log_path = (gates.parent / log).resolve()
        try:
            log_path.relative_to(evidence_root)
        except ValueError as exc:
            raise ValidationError(f"外部门禁 {name} 的 log 不得位于 gates JSON 目录外") from exc
        if not log_path.is_file():
            raise DyroError(f"外部门禁 {name} 的日志不存在：{log_path}")
        if _file_sha256(log_path) != log_sha256.lower():
            raise DyroError(f"外部门禁 {name} 的日志哈希不匹配")
        observed[name] = entry["exit_code"]
        logs.append((name, log_path))
    if set(observed) != expected:
        raise DyroError(f"外部门禁集合与任务不一致；期望 {', '.join(sorted(expected)) or '-'}")
    failures = [name for name, exit_code in observed.items() if exit_code != 0]
    if failures:
        raise DyroError(f"外部门禁未通过：{', '.join(sorted(failures))}")
    return data, tuple(logs)


def _validate_external_heads(config: Config, task: Task, heads: Path | None) -> bytes:
    if heads is None:
        raise DyroError(f"任务 {task.id} 完成时必须提供 --heads，绑定执行后的逐仓 Git HEAD")
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
    claim = _claim(task)
    if claim is None:
        raise DyroError(f"任务 {task.id} 尚未领取；请先运行 task claim")
    return claim


def run_gates(config: Config, task: Task, *, dry_run: bool = False) -> bool:
    _require_local_execution(config, "门禁", dry_run=dry_run)
    root = worktree_root(config, task)
    all_passed = True
    for index, gate in enumerate(task.gates, start=1):
        cwd = root / gate.cwd
        argv = expand_argv(gate.argv, workspace=root, root=config.root, task=task.id, line=task.line)
        result = run(argv, cwd=cwd, timeout=gate.timeout_seconds, dry_run=dry_run)
        _capture(task, f"gate-{index}.log", result.stdout, dry_run=dry_run)
        passed = result.code == 0
        all_passed = all_passed and passed
        if not dry_run:
            ledger(config, task.id, "gate", name=gate.name, argv=list(argv), passed=passed, exit_code=result.code)
    return all_passed


def run_task(config: Config, task: Task, *, dry_run: bool = False) -> str:
    _require_local_execution(config, "任务", dry_run=dry_run)
    if dry_run:
        return _run_task(config, task, dry_run=True)
    with exclusive_lock(_execution_lock_path(task), timeout_seconds=1.0):
        return _run_task(config, task, dry_run=False)


def _run_task(config: Config, task: Task, *, dry_run: bool) -> str:
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
    argv = _adapter_argv(config, task.executor, "write" if task.risk == "write" else "read", workspace=workspace, prompt=_prompt(task, "executor", workspace), task=task)
    result = run(argv, cwd=workspace, timeout=task.timeout_minutes * 60, dry_run=dry_run)
    _capture(task, "executor.log", result.stdout, dry_run=dry_run)
    if not dry_run:
        ledger(config, task.id, "executor", agent=task.executor, argv=list(argv), exit_code=result.code)
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
    set_status(config, task, "review")
    return "review"


def import_execution_evidence(
    config: Config,
    task: Task,
    *,
    receipt: Path,
    gates: Path | None = None,
    heads: Path | None = None,
    dry_run: bool = False,
) -> str:
    with exclusive_lock(_state_lock_path(task)):
        return _import_execution_evidence(
            config,
            task,
            receipt=receipt,
            gates=gates,
            heads=heads,
            dry_run=dry_run,
        )


def _import_execution_evidence(
    config: Config,
    task: Task,
    *,
    receipt: Path,
    gates: Path | None = None,
    heads: Path | None = None,
    dry_run: bool = False,
) -> str:
    """Import execution proof produced by the runner that claimed this task."""
    claim = _require_external_claim(config, task)
    current = status(config, task)
    if current not in ("assigned", "in_progress"):
        raise DyroError(f"仅 assigned 或 in_progress 任务可导入执行证据：{task.id}")
    if not receipt.is_file():
        raise DyroError(f"外部回执文件不存在：{receipt}")
    receipt_bytes = receipt.read_bytes()
    receipt_hash = hashlib.sha256(receipt_bytes).hexdigest()
    receipt_lines = receipt_bytes.decode("utf-8").splitlines()
    receipt_match = RESULT_RE.match(receipt_lines[0]) if receipt_lines else None
    result = receipt_match.group(1).upper() if receipt_match else ""
    gate_bytes, gate_logs = _validate_external_gates(task, receipt_hash, gates) if result == "DONE" else (b"", ())
    task_heads_bytes = _validate_external_heads(config, task, heads) if result == "DONE" else b""
    if dry_run:
        return "review" if result == "DONE" else "waiting_answer" if result == "QUESTION" else "failed"
    if current == "assigned":
        set_status(config, task, "in_progress")
    _copy_external_evidence(task, receipt, "receipt.md")
    if gate_bytes:
        atomic_write_bytes(task.directory / "evidence" / "external-gates.json", gate_bytes)
        for index, (_, log_path) in enumerate(gate_logs, start=1):
            _copy_external_evidence(task, log_path, f"evidence/gates/gate-{index}.log")
    if task_heads_bytes:
        atomic_write_bytes(task.directory / TASK_HEADS_FILE, task_heads_bytes)
    ledger(
        config,
        task.id,
        "external_execution_import",
        runner=claim["runner"],
        receipt_sha256=receipt_hash,
        task_heads_sha256=hashlib.sha256(task_heads_bytes).hexdigest() if task_heads_bytes else "",
    )
    if result == "QUESTION":
        set_status(config, task, "waiting_answer")
        return "waiting_answer"
    if result != "DONE":
        set_status(config, task, "failed")
        return "failed"
    set_status(config, task, "review")
    return "review"


def answer_task(config: Config, task: Task, answer: str, *, dry_run: bool = False) -> str:
    _require_local_execution(config, "任务续跑", dry_run=dry_run)
    if dry_run:
        return _answer_task(config, task, answer, dry_run=True)
    with exclusive_lock(_execution_lock_path(task), timeout_seconds=1.0):
        return _answer_task(config, task, answer, dry_run=False)


def _answer_task(config: Config, task: Task, answer: str, *, dry_run: bool) -> str:
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
    argv = _adapter_argv(config, task.executor, "write" if task.risk == "write" else "read", workspace=workspace, prompt=_prompt(task, "continuation", workspace), task=task)
    result = run(argv, cwd=workspace, timeout=task.timeout_minutes * 60, dry_run=dry_run)
    _capture(task, "executor-continuation.log", result.stdout, dry_run=dry_run)
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
    set_status(config, task, "review")
    return "review"


def _apply_review_decision(config: Config, task: Task, *, dry_run: bool = False) -> str:
    verdict, reviewed_receipt_hash, reviewed_task_heads_hash = _review_decision(task)
    receipt_hash = _file_sha256(task.directory / "receipt.md")
    task_heads_hash = _file_sha256(task.directory / TASK_HEADS_FILE)
    if verdict == "PASS":
        if reviewed_receipt_hash != receipt_hash or reviewed_task_heads_hash != task_heads_hash:
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
        next_status = "review_pending_signoff" if config.policy.require_external_signoff else "done"
        if config.policy.execution_mode == "local":
            _assert_task_heads_current(config, task)
        if next_status == "done" and task.merge_auto and config.policy.execution_mode == "local":
            try:
                _merge_task_repositories(config, task, push=task.merge_push, dry_run=dry_run)
            except DyroError as exc:
                if not dry_run:
                    ledger(config, task.id, "auto_merge_failed", error=str(exc))
                raise
        set_status(config, task, next_status, dry_run=dry_run)
        if not dry_run:
            ledger(
                config,
                task.id,
                "review_accepted",
                receipt_sha256=receipt_hash,
                task_heads_sha256=task_heads_hash,
                review_sha256=_file_sha256(task.directory / "review.md"),
            )
        if next_status != "done":
            return next_status
        return "done"
    if verdict == "FAIL":
        set_status(config, task, "failed")
        return "failed"
    return "review"


def review_task(config: Config, task: Task, *, dry_run: bool = False) -> str:
    _require_local_execution(config, "复核", dry_run=dry_run)
    if dry_run:
        return _review_task(config, task, dry_run=True)
    with exclusive_lock(_review_lock_path(task), timeout_seconds=1.0):
        return _review_task(config, task, dry_run=False)


def _review_task(config: Config, task: Task, *, dry_run: bool) -> str:
    if status(config, task) != "review":
        raise DyroError(f"仅 review 任务可启动复核：{task.id}")
    workspace = worktree_root(config, task)
    if not workspace.exists():
        raise DyroError(f"任务 worktree 不存在：{workspace}")
    if not dry_run:
        _assert_task_heads_current(config, task)
    argv = _adapter_argv(config, task.reviewer, "read", workspace=workspace, prompt=_prompt(task, "reviewer", workspace), task=task)
    result = run(argv, cwd=workspace, timeout=task.review_timeout_minutes * 60, dry_run=dry_run)
    _capture(task, "reviewer.log", result.stdout, dry_run=dry_run)
    if not dry_run:
        ledger(config, task.id, "review", agent=task.reviewer, argv=list(argv), exit_code=result.code)
        try:
            _assert_task_heads_current(config, task)
        except (DyroError, ValidationError) as exc:
            ledger(config, task.id, "review_source_changed", error=str(exc))
            raise DyroError(f"复核期间任务源码发生变化，拒绝接受复核结果：{exc}") from exc
    if result.code != 0:
        return "review"
    if dry_run:
        return "dry-run"
    return _apply_review_decision(config, task)


def import_review_evidence(config: Config, task: Task, *, review: Path, dry_run: bool = False) -> str:
    with exclusive_lock(_state_lock_path(task)):
        return _import_review_evidence(config, task, review=review, dry_run=dry_run)


def _import_review_evidence(config: Config, task: Task, *, review: Path, dry_run: bool = False) -> str:
    """Import a receipt-bound review emitted by the runner that claimed the task."""
    claim = _require_external_claim(config, task)
    if status(config, task) != "review":
        raise DyroError(f"仅 review 任务可导入复核证据：{task.id}")
    if dry_run:
        return "dry-run"
    _copy_external_evidence(task, review, "review.md")
    ledger(config, task.id, "external_review_import", runner=claim["runner"], review_sha256=_file_sha256(task.directory / "review.md"))
    return _apply_review_decision(config, task)


def signoff_task(config: Config, task: Task, *, approver: str, dry_run: bool = False) -> str:
    """Record a human or external-system approval for a receipt-bound review."""
    with exclusive_lock(_state_lock_path(task)):
        return _signoff_task(config, task, approver=approver, dry_run=dry_run)


def _signoff_task(config: Config, task: Task, *, approver: str, dry_run: bool = False) -> str:
    """Perform one lock-held external sign-off state transition."""
    if not config.policy.require_external_signoff:
        raise DyroError("当前 Profile 未启用 require_external_signoff，无需签收")
    if status(config, task) != "review_pending_signoff":
        raise DyroError(f"仅 review_pending_signoff 任务可签收：{task.id}")
    approver = approver.strip()
    if not approver:
        raise ValidationError("签收人不能为空")
    verdict, reviewed_receipt_hash, reviewed_task_heads_hash = _review_decision(task)
    receipt_hash = _file_sha256(task.directory / "receipt.md")
    task_heads_hash = _file_sha256(task.directory / TASK_HEADS_FILE)
    if (
        verdict != "PASS"
        or reviewed_receipt_hash != receipt_hash
        or reviewed_task_heads_hash != task_heads_hash
    ):
        raise DyroError("复核结论未通过或未绑定当前回执与任务 HEAD；请重新复核")
    signoff = {
        "task_id": task.id,
        "approver": approver,
        "receipt_sha256": receipt_hash,
        "task_heads_sha256": task_heads_hash,
        "review_sha256": _file_sha256(task.directory / "review.md"),
        "signed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if not dry_run:
        atomic_write_text(task.directory / "signoff.json", json.dumps(signoff, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        set_status(config, task, "done")
        ledger(
            config,
            task.id,
            "signoff",
            approver=approver,
            receipt_sha256=receipt_hash,
            task_heads_sha256=task_heads_hash,
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
        raise DyroError("当前 Profile 禁止 push；请在 dyro.toml 的 policy.allow_push 显式开启")
    line = get_line(config, task.line)
    task_heads = _assert_task_heads_current(config, task)
    plans: list[MergePlan] = []
    for repo_id in task.repositories:
        target = line_repository_path(config, line, repo_id)
        if git(target, "rev-parse", "--is-inside-work-tree").stdout.strip() != "true":
            raise DyroError(f"开发线 worktree 不存在或不是 Git：{target}")
        dirty = require_ok(git(target, "status", "--porcelain=v1", "-uall"), f"读取 {repo_id} 状态").stdout.strip()
        if dirty:
            raise DyroError(f"开发线仓库不干净，拒绝合并：{target}")
        current = require_ok(git(target, "branch", "--show-current"), f"读取 {repo_id} 分支").stdout.strip()
        if current != line.branch:
            raise DyroError(f"开发线仓库分支错误：{target} 当前 {current or 'DETACHED'}，期望 {line.branch}")
        original_head = require_ok(git(target, "rev-parse", "HEAD"), f"读取 {repo_id} 开发线 HEAD").stdout.strip()
        plans.append(MergePlan(repo_id, target, task_heads[repo_id], original_head))
    if push:
        for plan in plans:
            require_ok(
                git(plan.target, "push", "--dry-run", "origin", line.branch, dry_run=dry_run),
                f"预检推送 {plan.repository}",
            )
    return line, tuple(plans)


def _rollback_merges(plans: Iterable[MergePlan], committed_heads: dict[str, str]) -> list[str]:
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
                failures.append(f"{plan.repository}: HEAD changed concurrently; manual recovery required")
                continue
            result = git(plan.target, "reset", "--keep", plan.original_head)
        if result.code != 0:
            failures.append(f"{plan.repository}: {result.stdout.strip() or 'rollback failed'}")
    return failures


def _merge_task_repositories(config: Config, task: Task, *, push: bool, dry_run: bool) -> None:
    line, plans = _prepare_merge(config, task, push=push, dry_run=dry_run)
    message = f"merge(task): {task.id} {task.title}"
    if dry_run:
        for plan in plans:
            require_ok(
                git(plan.target, "merge", "--no-ff", "--no-commit", plan.source_head, dry_run=True, timeout=300),
                f"合并 {plan.repository}",
            )
        return

    committed_heads: dict[str, str] = {}
    try:
        for plan in plans:
            result = git(plan.target, "merge", "--no-ff", "--no-commit", plan.source_head, timeout=300)
            require_ok(result, f"合并 {plan.repository}")
        for plan in plans:
            if git(plan.target, "rev-parse", "--verify", "-q", "MERGE_HEAD").code == 0:
                require_ok(git(plan.target, "commit", "-m", message, timeout=300), f"提交 {plan.repository} 合并")
                committed_heads[plan.repository] = require_ok(
                    git(plan.target, "rev-parse", "HEAD"), f"读取 {plan.repository} 合并提交"
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
            raise DyroError(f"{exc}\n自动恢复未完全成功：{'; '.join(recovery_failures)}") from exc
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
        result_head = require_ok(git(plan.target, "rev-parse", "HEAD"), f"读取 {plan.repository} 合并结果").stdout.strip()
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


def merge_task(config: Config, task: Task, *, push: bool = False, dry_run: bool = False) -> None:
    _require_local_execution(config, "合并", dry_run=dry_run)
    if status(config, task) != "done":
        raise DyroError(f"仅 done 任务可合并：{task.id}")
    _merge_task_repositories(config, task, push=push, dry_run=dry_run)


def board(config: Config) -> str:
    rows = ["# DyroEngineeringFlow task board", "", "| Task | Line | Status | Risk | Depends |", "| --- | --- | --- | --- | --- |"]
    for task in list_tasks(config):
        rows.append(f"| {task.id} | {task.line} | {status(config, task)} | {task.risk} | {', '.join(task.depends_on) or '-'} |")
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
        counters = result.setdefault(str(agent), {"executor": 0, "executor_ok": 0, "review": 0, "review_ok": 0})
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
    outcomes: list[tuple[str, str]] = []
    for task in list_tasks(config):
        if status(config, task) not in ("backlog", "assigned"):
            continue
        try:
            outcomes.append((task.id, run_task(config, task, dry_run=dry_run)))
        except DyroError as exc:
            outcomes.append((task.id, f"skipped: {exc}"))
    for task in list_tasks(config):
        if status(config, task) != "review":
            continue
        try:
            outcomes.append((task.id, review_task(config, task, dry_run=dry_run)))
        except DyroError as exc:
            outcomes.append((task.id, f"review pending: {exc}"))
    return outcomes


def task_template(task_id: str, title: str, line: str, repository: str, mount: str) -> str:
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

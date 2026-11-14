"""Shared lifecycle locks and single-file operation journals.

Callers hold L0 (identity), then L1 (line merge locks), then L2 (anchors),
as applicable, through validation, Git mutations and journal updates. These
helpers do not make Git and journal writes atomic, or replay Git operations.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Iterable, Iterator, Mapping
import uuid

from .config import Config, validate_id
from .errors import DyroError, ValidationError
from .state import atomic_write_text, exclusive_lock


LOCK_TIMEOUT_SECONDS = 1800.0
TERMINAL_PHASES = frozenset({"completed", "aborted"})


def line_identity_lock_path(config: Config) -> Path:
    return config.root / ".dyro" / "locks" / "line-identity.lock"


@contextmanager
def line_identity_lock(
    config: Config, *, timeout_seconds: float = LOCK_TIMEOUT_SECONDS
) -> Iterator[None]:
    with exclusive_lock(line_identity_lock_path(config), timeout_seconds=timeout_seconds):
        yield


def anchor_lock_path(config: Config, anchor: Path) -> Path:
    """Key physical locks by normalized absolute anchor, never by a worktree."""
    normalized = os.path.normcase(str(anchor.resolve()))
    digest = hashlib.sha256(os.fsencode(normalized)).hexdigest()
    return config.root / ".dyro" / "locks" / "anchor" / f"{digest}.lock"


@contextmanager
def anchor_lock(
    config: Config, anchor: Path, *, timeout_seconds: float = LOCK_TIMEOUT_SECONDS,
) -> Iterator[None]:
    with exclusive_lock(anchor_lock_path(config, anchor), timeout_seconds=timeout_seconds):
        yield


@contextmanager
def anchor_locks(
    config: Config,
    repositories: Iterable[str],
    *,
    timeout_seconds: float = LOCK_TIMEOUT_SECONDS,
) -> Iterator[None]:
    """Acquire L2 in repo ID order; release in reverse, including on failure."""
    paths: list[Path] = []
    for repo_id in sorted(set(repositories)):
        if repo_id not in config.repositories:
            raise ValidationError(f"未配置的仓库：{repo_id}")
        path = anchor_lock_path(config, config.root / config.repositories[repo_id].path)
        if path not in paths:
            paths.append(path)
    with ExitStack() as stack:
        for path in paths:
            stack.enter_context(exclusive_lock(path, timeout_seconds=timeout_seconds))
        yield


def op_path(config: Config, op_id: str) -> Path:
    validate_id(op_id, "操作 ID")
    return config.root / ".dyro" / "ops" / f"{op_id}.json"


def _write_op(config: Config, op: dict) -> None:
    op["updated_at"] = datetime.now(timezone.utc).isoformat()
    atomic_write_text(
        op_path(config, op["op_id"]),
        json.dumps(op, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n",
    )


def create_op(
    config: Config,
    *,
    kind: str,
    subject_line_ids: Iterable[str],
    manifest_snapshot: Mapping[str, object],
    repos_ordered: Iterable[str],
    effective_options: Mapping[str, object],
    repository_snapshots: Mapping[str, Mapping[str, object]] | None = None,
    event_facts: Mapping[str, object] | None = None,
    op_id: str | None = None,
) -> dict:
    """Create R0/M0 under the caller's lifecycle locks, freezing its inputs.

    Repository snapshots hold paths, storage mode and resolved integration ref;
    event facts are a draft that the caller validates before any Git mutation.
    """
    op_id = op_id if op_id is not None else uuid.uuid4().hex
    path = op_path(config, op_id)
    if path.exists():
        raise DyroError(f"操作日志已存在：{op_id}")
    validate_id(kind, "操作类型")
    subjects = sorted(set(subject_line_ids))
    if not subjects:
        raise ValidationError("操作至少需要一条主体开发线")
    for subject in subjects:
        validate_id(subject, "主体开发线 ID")
    repos = sorted(set(repos_ordered))
    for repo in repos:
        validate_id(repo, "仓库 ID")
    snapshots = dict(repository_snapshots or {})
    if set(snapshots) - set(repos):
        raise ValidationError("仓库快照包含未计划仓库")
    op = {
        "schema_version": 1,
        "op_id": op_id,
        "kind": kind,
        "subject_line_ids": subjects,
        "manifest_snapshot": dict(manifest_snapshot),
        "repos_ordered": repos,
        "effective_options": dict(effective_options),
        "event_facts": dict(event_facts or {}),
        "phase": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "repositories": {
            repo: {
                "snapshot": dict(snapshots.get(repo, {})),
                "phase": "pending",
                "outcome": "pending",
                "steps": {},
            }
            for repo in repos
        },
    }
    _write_op(config, op)
    return load_op(config, op_id)


def load_op(config: Config, op_id: str) -> dict:
    path = op_path(config, op_id)
    try:
        op = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError, RecursionError) as exc:
        raise ValidationError(f"无法读取操作日志：{path}") from exc
    if (
        not isinstance(op, dict)
        or op.get("schema_version") != 1
        or op.get("op_id") != op_id
        or not isinstance(op.get("kind"), str)
        or not isinstance(op.get("phase"), str)
        or not isinstance(op.get("created_at"), str)
        or not isinstance(op.get("manifest_snapshot"), dict)
        or not isinstance(op.get("effective_options"), dict)
        or not isinstance(op.get("subject_line_ids"), list)
        or not op["subject_line_ids"]
        or not all(isinstance(item, str) for item in op["subject_line_ids"])
        or not isinstance(op.get("repos_ordered"), list)
        or not all(isinstance(item, str) for item in op["repos_ordered"])
        or op["repos_ordered"] != sorted(set(op["repos_ordered"]))
        or not isinstance(op.get("repositories"), dict)
        or set(op["repositories"]) != set(op["repos_ordered"])
    ):
        raise ValidationError(f"操作日志格式错误：{path}")
    for repo in op["repositories"].values():
        if (
            not isinstance(repo, dict)
            or not isinstance(repo.get("phase"), str)
            or not isinstance(repo.get("outcome"), str)
            or not isinstance(repo.get("steps"), dict)
            or any(
                not isinstance(step, dict) or not isinstance(step.get("intent"), dict)
                or ("result" in step and not isinstance(step["result"], dict))
                for step in repo["steps"].values()
            )
        ):
            raise ValidationError(f"操作日志仓库记录格式错误：{path}")
    return op


def _repo_for_update(config: Config, op_id: str, repo_id: str) -> tuple[dict, dict]:
    op = load_op(config, op_id)
    if op["phase"] in TERMINAL_PHASES:
        raise DyroError(f"操作已结束，不能再修改：{op_id}")
    if repo_id not in op["repositories"]:
        raise ValidationError(f"操作 {op_id} 未计划仓库：{repo_id}")
    return op, op["repositories"][repo_id]


def record_repo_intent(
    config: Config, op_id: str, repo_id: str, action: str, *,
    original_head: str, target_sha: str, **details: object,
) -> None:
    """Persist a pinned intent before Git; never replace an existing intent."""
    op, repo = _repo_for_update(config, op_id, repo_id)
    validate_id(action, "操作动作")
    intent = dict(details, repo=repo_id, action=action, original_head=original_head, target_sha=target_sha)
    previous = repo["steps"].get(action)
    if previous is not None:
        if previous["intent"] != intent:
            raise DyroError(f"拒绝修改已冻结的操作意图：{op_id}/{repo_id}/{action}")
        return
    if any("result" not in step for step in repo["steps"].values()):
        raise DyroError(f"仓库存在未对账的操作意图：{op_id}/{repo_id}")
    repo["steps"][action] = {"intent": intent}
    _write_op(config, op)


def record_repo_result(
    config: Config, op_id: str, repo_id: str, action: str, *,
    outcome: str, new_tip: str = "", error: str = "",
) -> None:
    """Persist the observed result after Git, or after recovery reconciliation."""
    op, repo = _repo_for_update(config, op_id, repo_id)
    if action not in repo["steps"]:
        raise DyroError(f"操作结果缺少先行意图：{op_id}/{repo_id}/{action}")
    previous = repo["steps"][action].get("result")
    if previous is not None and previous.get("outcome") != "unknown":
        raise DyroError(f"拒绝覆盖已确认的操作结果：{op_id}/{repo_id}/{action}")
    repo["steps"][action]["result"] = dict(outcome=outcome, new_tip=new_tip, error=error)
    _write_op(config, op)


def update_repo_phase(
    config: Config, op_id: str, repo_id: str, *, phase: str, outcome: str,
) -> None:
    op, repo = _repo_for_update(config, op_id, repo_id)
    repo.update(phase=phase, outcome=outcome)
    _write_op(config, op)


def finish_op(config: Config, op_id: str, *, phase: str) -> None:
    """Close reconciled operations, allowing report-only recovery of pushes."""
    if phase not in TERMINAL_PHASES:
        raise ValidationError("操作终态必须是 completed 或 aborted")
    op = load_op(config, op_id)
    if op["phase"] in TERMINAL_PHASES:
        if op["phase"] != phase:
            raise DyroError(f"操作已以 {op['phase']} 结束：{op_id}")
        return
    for repo_id, repo in op["repositories"].items():
        # Recovery may acknowledge an unknown idempotent push after the merge
        # is reconciled. A missing push result must still keep the op open.
        if repo["phase"] == "unknown" or repo["outcome"] in {"", "pending", "unknown"} or any(
            "result" not in step or step["result"].get("outcome") in {None, "", "pending"}
            or (step["result"].get("outcome") == "unknown" and not (
                action == "push" and op["kind"] == "merge"
                and op["effective_options"].get("push")
                and repo["outcome"] in {"committed", "already_included"}
            ))
            for action, step in repo["steps"].items()
        ):
            raise DyroError(f"仓库尚未完成对账，不能结束操作：{op_id}/{repo_id}")
    op["phase"] = phase
    _write_op(config, op)


def find_unfinished_op(
    config: Config, subject_line_id: str, *, kinds: Iterable[str] | None = None,
) -> dict | None:
    validate_id(subject_line_id, "主体开发线 ID")
    selected_kinds = set(kinds) if kinds is not None else None
    matches = []
    for path in sorted((config.root / ".dyro" / "ops").glob("*.json")):
        op = load_op(config, path.stem)
        if (
            subject_line_id in op["subject_line_ids"]
            and op["phase"] not in TERMINAL_PHASES
            and (selected_kinds is None or op["kind"] in selected_kinds)
        ):
            matches.append(op)
    return max(matches, key=lambda op: (op["created_at"], op["op_id"]), default=None)


def require_no_unfinished_op(
    config: Config, subject_line_id: str, *, kinds: Iterable[str] | None = None,
) -> None:
    op = find_unfinished_op(config, subject_line_id, kinds=kinds)
    if op is not None:
        command = op["kind"].replace("_", "-")
        raise DyroError(
            f"开发线 {subject_line_id} 有未完成操作 {op['op_id']}；"
            f"请运行 dyro line {command} {subject_line_id} --recover {op['op_id']}"
        )


def require_compatible_options(op: Mapping[str, object], supplied: Mapping[str, object]) -> dict:
    """Check explicitly supplied options only; return the frozen effective set."""
    frozen = op["effective_options"]
    if not isinstance(frozen, dict):
        raise ValidationError("操作日志缺少 effective_options")
    for key, value in supplied.items():
        if key not in frozen or value != frozen[key] or type(value) is not type(frozen[key]):
            raise DyroError(
                f"恢复选项冲突：{key}={value!r}；journal 冻结值为 {frozen.get(key)!r}"
            )
    return json.loads(json.dumps(frozen))

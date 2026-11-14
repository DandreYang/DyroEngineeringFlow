from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
from typing import Iterable, Mapping

from .config import Config, external_security_errors, validate_id
from .errors import DyroError, ValidationError
from .line_ops import (
    TERMINAL_PHASES,
    anchor_locks,
    create_op,
    finish_op,
    find_unfinished_op,
    load_op,
    line_identity_lock,
    record_repo_intent,
    record_repo_result,
    require_compatible_options,
    require_no_unfinished_op,
    update_repo_phase,
)
from .process import git, git_read, require_ok
from .read_limits import (
    ReadBudget,
    ObservationLimits,
    ReadLimitCode,
    ReadLimitError,
    apply_control_plane_fanout,
    bounded_directory_names,
)
from .state import atomic_write_text, exclusive_lock


STORAGE_MODES = frozenset({"linked-worktree", "anchor-reference"})
LINE_MANIFEST_SCHEMAS = frozenset({1, 2, 3, 4})
MERGE_LOCK_TIMEOUT_SECONDS = 1800.0
OBSERVATION_DEADLINE_FINDING = (
    "FAIL observation: deadline exceeded before every worktree was inspected"
)
OBSERVATION_TIMEOUT_SCOPE = "observation"
OBSERVATION_TIMEOUT_BRANCH = "TIMEOUT"
_STASHED_FINDINGS = "_control_plane_findings"
_STASHED_ROWS = "_control_plane_rows"
ObservationStatusRow = tuple[str, str, str, str, str, int]


@dataclass(frozen=True)
class Line:
    id: str
    kind: str
    branch: str
    base: str
    repositories: tuple[str, ...]
    repository_bases: Mapping[str, str] = field(default_factory=dict)
    storage_modes: Mapping[str, str] = field(default_factory=dict)
    parent: str = ""
    remote_anchors: Mapping[str, str] = field(default_factory=dict)

    def base_for(self, repo_id: str) -> str:
        return self.repository_bases.get(repo_id, self.base)

    def storage_for(self, repo_id: str) -> str:
        return self.storage_modes.get(repo_id, "linked-worktree")


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_key(value: str) -> str:
    return _toml_string(value)


def _state_path(config: Config, kind: str, line_id: str) -> Path:
    validate_id(line_id, "开发线 ID")
    if kind == "line":
        return config.lines_state_dir / f"{line_id}.toml"
    if kind == "hotfix":
        return config.hotfixes_state_dir / f"{line_id}.toml"
    raise ValidationError(f"未知开发线类型：{kind}")


def _write_line(config: Config, line: Line, *, dry_run: bool = False) -> None:
    path = _state_path(config, line.kind, line.id)
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    repo_items = ", ".join(_toml_string(repo_id) for repo_id in line.repositories)
    bases = tuple((repo_id, line.base_for(repo_id)) for repo_id in line.repositories if line.base_for(repo_id) != line.base)
    storage = tuple((repo_id, line.storage_for(repo_id)) for repo_id in line.repositories if line.storage_for(repo_id) != "linked-worktree")
    schema_version = 4 if line.remote_anchors else (3 if line.parent else 2)
    chunks = [
        f"schema_version = {schema_version}",
        f"id = {_toml_string(line.id)}",
        f"kind = {_toml_string(line.kind)}",
        f"branch = {_toml_string(line.branch)}",
        f"base = {_toml_string(line.base)}",
    ]
    if line.parent:
        chunks.append(f"parent = {_toml_string(line.parent)}")
    chunks.append(f"repositories = [{repo_items}]")
    if bases:
        chunks.extend(("", "[repository_bases]"))
        chunks.extend(f"{_toml_key(repo_id)} = {_toml_string(base)}" for repo_id, base in bases)
    if storage:
        chunks.extend(("", "[storage_modes]"))
        chunks.extend(f"{_toml_key(repo_id)} = {_toml_string(mode)}" for repo_id, mode in storage)
    if line.remote_anchors:
        chunks.extend(("", "[remote_anchors]"))
        chunks.extend(f"{_toml_key(k)} = {_toml_string(v)}" for k, v in sorted(line.remote_anchors.items()))
    chunks.append("")
    atomic_write_text(path, "\n".join(chunks))


def _parse_line_content(path: Path, content: bytes) -> Line:
    import tomllib

    try:
        raw = tomllib.loads(content.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError, RecursionError) as exc:
        raise ValidationError(f"开发线清单格式错误：{path}: {exc}") from exc
    if raw.get("schema_version") not in LINE_MANIFEST_SCHEMAS:
        raise ValidationError(f"不支持的开发线清单版本：{path}")
    line_id = validate_id(str(raw.get("id", "")), "开发线 ID")
    kind = str(raw.get("kind", ""))
    if kind not in ("line", "hotfix"):
        raise ValidationError(f"开发线类型非法：{path}")
    branch = str(raw.get("branch", ""))
    base = str(raw.get("base", ""))
    repos = raw.get("repositories", [])
    if not branch or not base or not isinstance(repos, list) or not repos:
        raise ValidationError(f"开发线清单缺少 branch/base/repositories：{path}")
    repositories = tuple(validate_id(str(item), "开发线仓库 ID") for item in repos)
    bases_raw = raw.get("repository_bases", {})
    storage_raw = raw.get("storage_modes", {})
    if not isinstance(bases_raw, dict) or not isinstance(storage_raw, dict):
        raise ValidationError(f"开发线清单 repository_bases/storage_modes 格式错误：{path}")
    unknown = (set(bases_raw) | set(storage_raw)) - set(repositories)
    if unknown:
        raise ValidationError(f"开发线清单包含未选择的仓库：{', '.join(sorted(unknown))}")
    repository_bases: dict[str, str] = {}
    for repo_id, value in bases_raw.items():
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"开发线清单基线无效：{repo_id}")
        repository_bases[str(repo_id)] = value.strip()
    storage_modes: dict[str, str] = {}
    for repo_id, value in storage_raw.items():
        if value not in STORAGE_MODES:
            raise ValidationError(f"开发线清单存储方式无效：{repo_id}={value!r}")
        storage_modes[str(repo_id)] = value
    anchors = raw.get("remote_anchors", {})
    if ("remote_anchors" in raw and raw["schema_version"] != 4) or not isinstance(anchors, dict):
        raise ValidationError("remote_anchors requires schema_version 4")
    if set(anchors) - set(repositories) or any(not isinstance(v, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", v) for v in anchors.values()):
        raise ValidationError("invalid remote_anchors")
    parent = _parse_parent_field(raw.get("parent", ""), line_id, path)
    return Line(
        line_id, kind, branch, base, repositories, repository_bases, storage_modes, parent, anchors
    )


def _parse_parent_field(raw: object, line_id: str, path: Path) -> str:
    if raw in ("", None):
        return ""
    if not isinstance(raw, str) or not raw.strip():
        raise ValidationError(f"开发线清单 parent 必须是开发线 ID：{path}")
    parent = validate_id(raw.strip(), "父开发线 ID")
    if parent == line_id:
        raise ValidationError(f"开发线不能以自身为父线：{path}")
    return parent


def _parse_line(path: Path) -> Line:
    return _parse_line_content(path, path.read_bytes())


def load_line_bounded(path: Path, budget: ReadBudget, *, workspace_root: Path) -> Line:
    content = budget.read_regular_bytes_at(
        root=workspace_root,
        directory=path.parent,
        name=path.name,
        maximum_bytes=budget.limits.line_manifest_bytes,
        label="line manifest",
    )
    line = _parse_line_content(path, content)
    if path.stem != line.id:
        raise ValidationError(f"开发线文件名与清单 ID 不一致：{path.stem} != {line.id}")
    return line


def _list_lines_bounded(
    config: Config, wanted: tuple[str, ...], budget: ReadBudget
) -> list[Line]:
    entries: list[tuple[str, Path, str]] = []
    records_seen = 0
    for current_kind in wanted:
        parent = (
            config.lines_state_dir
            if current_kind == "line"
            else config.hotfixes_state_dir
        )
        with budget.open_safe_directory_chain(
            config.root, parent, allow_missing=True
        ) as directory_fd:
            if directory_fd is None:
                continue
            names = bounded_directory_names(
                directory_fd,
                budget,
                maximum_records=budget.limits.line_records - records_seen,
                label="Line manifest",
            )
            records_seen += len(names)
            for name in names:
                if not name.endswith(".toml"):
                    continue
                entries.append((current_kind, parent, name))

    lines: list[Line] = []
    for current_kind, parent, name in sorted(entries):
        with budget.open_safe_directory_chain(config.root, parent) as directory_fd:
            assert directory_fd is not None
            try:
                info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError as exc:
                raise ReadLimitError(
                    ReadLimitCode.UNSAFE_FILE,
                    "Line manifest is not a safe regular file",
                ) from exc
            if not stat.S_ISREG(info.st_mode):
                raise ReadLimitError(
                    ReadLimitCode.UNSAFE_FILE,
                    "Line manifest is not a safe regular file",
                )
            path = parent / name
            budget.bind_file_identity(path, (info.st_dev, info.st_ino))
            content = budget.read_regular_bytes_from_directory_fd(
                directory_fd,
                name=name,
                maximum_bytes=budget.limits.line_manifest_bytes,
                label="line manifest",
                identity_path=path,
            )
        line = _parse_line_content(path, content)
        if path.stem != line.id or line.kind != current_kind:
            raise ValidationError(f"开发线文件名或类型与清单不一致：{path}")
        lines.append(line)
    return lines


def list_lines(
    config: Config,
    kind: str | None = None,
    *,
    read_budget: ReadBudget | None = None,
) -> list[Line]:
    wanted = (kind,) if kind else ("line", "hotfix")
    if read_budget is not None:
        return sorted(
            _list_lines_bounded(config, wanted, read_budget),
            key=lambda line: (line.kind, line.id),
        )
    lines: list[Line] = []
    for current_kind in wanted:
        parent = config.lines_state_dir if current_kind == "line" else config.hotfixes_state_dir
        if parent.exists():
            lines.extend(_parse_line(path) for path in sorted(parent.glob("*.toml")))
    return sorted(lines, key=lambda line: (line.kind, line.id))


def get_line(
    config: Config,
    line_id: str,
    kind: str | None = None,
    *,
    read_budget: ReadBudget | None = None,
) -> Line:
    matches = [
        line
        for line in list_lines(config, kind, read_budget=read_budget)
        if line.id == line_id
    ]
    if not matches:
        raise DyroError(f"未登记的开发线：{line_id}")
    if len(matches) > 1:
        raise DyroError(f"开发线 ID 同时存在于 line 与 hotfix：{line_id}")
    return matches[0]


def line_root(config: Config, line: Line) -> Path:
    parent = config.layout.lines if line.kind == "line" else config.layout.hotfixes
    return config.root / parent / line.id


def repository_path(config: Config, repo_id: str) -> Path:
    try:
        return config.root / config.repositories[repo_id].path
    except KeyError as exc:
        raise ValidationError(f"开发线引用未配置仓库：{repo_id}") from exc


def line_repository_path(config: Config, line: Line, repo_id: str) -> Path:
    repo = config.repositories[repo_id]
    return line_root(config, line) / repo.mount


def _is_git_repo(path: Path, *, read_budget: ReadBudget | None = None) -> bool:
    return (
        git_read(
            path,
            "rev-parse",
            "--git-dir",
            read_budget=read_budget,
        ).code
        == 0
    )


def _ensure_clean(path: Path) -> None:
    result = require_ok(
        git_read(path, "status", "--porcelain=v1", "-uall"), f"读取 {path} 状态"
    )
    if result.stdout.strip():
        raise DyroError(f"仓库不干净，拒绝创建或合并 worktree：{path}")



def _ref_exists(path: Path, ref: str, *, read_budget: ReadBudget | None = None) -> bool:
    return (
        git_read(
            path,
            "show-ref",
            "--verify",
            "--quiet",
            ref,
            read_budget=read_budget,
        ).code
        == 0
    )


def _normalize_upstream(upstream: str) -> str:
    if upstream.startswith("refs/remotes/"):
        return upstream[len("refs/remotes/") :]
    if upstream.startswith("refs/heads/"):
        return upstream[len("refs/heads/") :]
    return upstream


def _expected_remote_branch(branch: str) -> str:
    return f"origin/{branch}"


def _branch_upstream(
    path: Path, branch: str | None = None, *, read_budget: ReadBudget | None = None
) -> str:
    spec = "@{upstream}" if branch is None else f"{branch}@{{upstream}}"
    result = git_read(
        path,
        "rev-parse",
        "--abbrev-ref",
        "--symbolic-full-name",
        spec,
        read_budget=read_budget,
    )
    return _normalize_upstream(result.stdout.strip()) if result.code == 0 else ""


def _rev_parse(
    path: Path, spec: str, *, read_budget: ReadBudget | None = None
) -> str:
    result = git_read(path, "rev-parse", spec, read_budget=read_budget)
    return result.stdout.strip() if result.code == 0 else ""


def _validate_existing_local_branch(
    anchor: Path, repo_id: str, line: Line, repo_base: str
) -> None:
    expected = _expected_remote_branch(line.branch)
    remote_ref = f"refs/remotes/{expected}"
    remote_exists = _ref_exists(anchor, remote_ref)
    upstream = _branch_upstream(anchor, line.branch)
    if upstream and upstream != expected:
        raise DyroError(
            f"{repo_id} 既有分支 {line.branch} 的上游是 {upstream}，不是 {expected}；"
            f"从父级 feat 检出或跟踪其他开发线的分支不能作为本线工作区"
        )
    local_sha = _rev_parse(anchor, line.branch)
    remote_sha = _rev_parse(anchor, expected) if remote_exists else ""
    is_remote_feat = bool(remote_sha) and (
        local_sha == remote_sha or upstream == expected
    )
    ancestry = git_read(anchor, "merge-base", "--is-ancestor", repo_base, line.branch)
    if not (is_remote_feat or ancestry.code == 0):
        raise DyroError(
            f"{repo_id} 既有分支 {line.branch} 不包含声明的基线 {repo_base}"
        )


def _linked_worktree_command(
    anchor: Path, repo_id: str, line: Line, repo_base: str, destination: Path
) -> tuple[str, ...]:
    local_ref = f"refs/heads/{line.branch}"
    remote_name = _expected_remote_branch(line.branch)
    remote_ref = f"refs/remotes/{remote_name}"
    if _ref_exists(anchor, local_ref):
        _validate_existing_local_branch(anchor, repo_id, line, repo_base)
        return ("worktree", "add", str(destination), line.branch)
    if _ref_exists(anchor, remote_ref):
        return (
            "worktree",
            "add",
            "--track",
            "-b",
            line.branch,
            str(destination),
            remote_name,
        )
    return (
        "worktree",
        "add",
        "--no-track",
        "-b",
        line.branch,
        str(destination),
        repo_base,
    )


def _remove_line_worktree(
    config: Config, line: Line, repo_id: str, destination: Path, *, new_branch: bool = False
) -> str | None:
    """Best-effort cleanup for one worktree or anchor-reference created during line setup.

    ``new_branch`` means this setup created ``line.branch`` with ``-b``; delete it too,
    otherwise a retry with another base would silently reuse the leftover branch.
    The delete is attempted even after a path-cleanup failure; Git refuses it while
    the branch is still checked out, and both failures are reported.
    """
    failure: str | None = None
    try:
        if line.storage_for(repo_id) == "anchor-reference" or destination.is_symlink():
            if destination.is_symlink() or destination.exists():
                destination.unlink()
            return None
        if destination.exists() or destination.is_symlink():
            anchor = repository_path(config, repo_id)
            result = git(anchor, "worktree", "remove", "--force", str(destination), timeout=120)
            if result.code != 0:
                # Fall back to deleting the path when Git no longer tracks it as a worktree.
                if destination.is_symlink():
                    destination.unlink()
                elif destination.exists():
                    shutil.rmtree(destination)
                    prune = git(anchor, "worktree", "prune", timeout=60)
                    if prune.code != 0:
                        failure = f"{repo_id}: worktree remove failed: {result.stdout.strip() or 'unknown error'}"
    except OSError as exc:
        failure = f"{repo_id}: {exc}"
    if new_branch:
        deleted = git(repository_path(config, repo_id), "branch", "-D", line.branch, timeout=60)
        if deleted.code != 0:
            note = (
                f"{repo_id}: 删除本次新建分支 {line.branch} 失败："
                f"{deleted.stdout.strip() or 'unknown error'}"
            )
            failure = f"{failure}; {note}" if failure else note
    return failure


def _rollback_line_creations(
    config: Config, line: Line, created: list[tuple[str, bool]]
) -> list[str]:
    failures: list[str] = []
    for repo_id, new_branch in reversed(created):
        destination = line_repository_path(config, line, repo_id)
        failure = _remove_line_worktree(
            config, line, repo_id, destination, new_branch=new_branch
        )
        if failure:
            failures.append(failure)
    target_root = line_root(config, line)
    if target_root.exists():
        try:
            # Remove empty parents left behind after a partial multi-repo create.
            for path in sorted(target_root.rglob("*"), reverse=True):
                if path.is_dir() and not any(path.iterdir()):
                    path.rmdir()
            if target_root.is_dir() and not any(target_root.iterdir()):
                target_root.rmdir()
        except OSError:
            pass
    return failures


def _remove_empty_parents(destination: Path, stop: Path) -> str | None:
    """Remove empty ancestors of destination, stopping before stop itself."""
    current = destination.parent
    try:
        while current != stop and stop in current.parents:
            if not current.is_dir() or any(current.iterdir()):
                return None
            current.rmdir()
            current = current.parent
    except OSError as exc:
        return str(exc)
    return None


def _build_line(
    config: Config,
    *,
    line_id: str,
    branch: str,
    base: str,
    repositories: Iterable[str] | None = None,
    repository_bases: Mapping[str, str] | None = None,
    storage_modes: Mapping[str, str] | None = None,
    kind: str = "line",
    parent: str = "",
) -> Line:
    """Validate line arguments and turn them into one immutable creation plan."""
    validate_id(line_id, "开发线 ID")
    if not branch or not base:
        raise ValidationError("branch 与 base 都必须明确指定")
    if kind not in ("line", "hotfix"):
        raise ValidationError("kind 只能是 line 或 hotfix")
    tombstone = config.lines_state_dir / "retired" / f"{line_id}.toml"
    if tombstone.exists() or tombstone.is_symlink():
        raise DyroError(f"开发线 ID 已退役，不可复用：{line_id}")
    if _state_path(config, kind, line_id).exists():
        raise DyroError(f"开发线已登记：{line_id}")
    selected = tuple(repositories or config.repositories.keys())
    if not selected:
        raise ValidationError("至少选择一个仓库")
    unknown = [repo_id for repo_id in selected if repo_id not in config.repositories]
    if unknown:
        raise ValidationError(f"未配置的仓库：{', '.join(unknown)}")
    base_overrides = dict(repository_bases or {})
    storage_overrides = dict(storage_modes or {})
    unselected = (set(base_overrides) | set(storage_overrides)) - set(selected)
    if unselected:
        raise ValidationError(f"仓库基线或存储方式包含未选择仓库：{', '.join(sorted(unselected))}")
    for repo_id, repo_base in base_overrides.items():
        if not isinstance(repo_base, str) or not repo_base.strip():
            raise ValidationError(f"{repo_id} 的基线不能为空")
        base_overrides[repo_id] = repo_base.strip()
    for repo_id, storage_mode in storage_overrides.items():
        if storage_mode not in STORAGE_MODES:
            raise ValidationError(f"{repo_id} 的存储方式必须是：{', '.join(sorted(STORAGE_MODES))}")
    parent_id = _validate_parent_link(config, line_id, parent)
    return Line(
        line_id, kind, branch, base, selected, base_overrides, storage_overrides, parent_id
    )


def _plan_repository_creation(
    config: Config, line: Line, repo_id: str
) -> tuple[str, Path, tuple[str, ...]]:
    """Validate one repository and return its worktree destination and Git command."""
    anchor = repository_path(config, repo_id)
    destination = line_repository_path(config, line, repo_id)
    if not _is_git_repo(anchor):
        raise DyroError(f"仓库 anchor 不存在或不是 Git 仓库：{anchor}")
    _ensure_clean(anchor)
    repo_base = line.base_for(repo_id)
    require_ok(
        git_read(anchor, "rev-parse", "--verify", f"{repo_base}^{{commit}}"),
        f"校验 {repo_id} 基线 {repo_base}",
    )
    if destination.exists() or destination.is_symlink():
        raise DyroError(f"worktree 目标已存在：{destination}")
    if line.storage_for(repo_id) == "anchor-reference":
        anchor_branch = require_ok(
            git_read(anchor, "branch", "--show-current"),
            f"读取 {repo_id} anchor 分支",
        ).stdout.strip()
        if anchor_branch != line.branch:
            raise DyroError(
                f"{repo_id} 的 anchor-reference 要求 anchor 正位于 {line.branch}，"
                f"当前为 {anchor_branch or 'DETACHED'}"
            )
        return (repo_id, destination, ())
    command = _linked_worktree_command(anchor, repo_id, line, repo_base, destination)
    return (repo_id, destination, command)


def _plan_line_creation(
    config: Config, line: Line
) -> list[tuple[str, Path, tuple[str, ...]]]:
    """Validate every Git-side prerequisite without creating a worktree."""
    target_root = line_root(config, line)
    if target_root.exists() and any(target_root.iterdir()):
        raise DyroError(f"目标工作区已存在且非空：{target_root}")

    # Validate every repository before mutating any worktree so partial creates are rare.
    return [
        _plan_repository_creation(config, line, repo_id) for repo_id in line.repositories
    ]


def preflight_line(
    config: Config,
    *,
    line_id: str,
    branch: str,
    base: str,
    repositories: Iterable[str] | None = None,
    repository_bases: Mapping[str, str] | None = None,
    storage_modes: Mapping[str, str] | None = None,
    kind: str = "line",
    parent: str = "",
) -> Line:
    """Verify that a line can be created without changing Git state.

    The home wizard uses this before asking for confirmation. ``create_line``
    repeats the check immediately before mutation because repositories can
    change between the preview and the user's confirmation.
    """
    line = _build_line(
        config,
        line_id=line_id,
        branch=branch,
        base=base,
        repositories=repositories,
        repository_bases=repository_bases,
        storage_modes=storage_modes,
        kind=kind,
        parent=parent,
    )
    _plan_line_creation(config, line)
    return line


def create_line(
    config: Config,
    *,
    line_id: str,
    branch: str,
    base: str,
    repositories: Iterable[str] | None = None,
    repository_bases: Mapping[str, str] | None = None,
    storage_modes: Mapping[str, str] | None = None,
    kind: str = "line",
    parent: str = "",
    dry_run: bool = False,
) -> Line:
    """Create isolated linked worktrees from configured repository anchors."""
    with line_identity_lock(config):
        line = _build_line(
            config,
            line_id=line_id,
            branch=branch,
            base=base,
            repositories=repositories,
            repository_bases=repository_bases,
            storage_modes=storage_modes,
            kind=kind,
            parent=parent,
        )
        with anchor_locks(config, line.repositories):
            return _create_line_worktrees(config, line, dry_run=dry_run)


def _create_repository_worktree(
    config: Config,
    line: Line,
    repo_id: str,
    destination: Path,
    command: tuple[str, ...],
    *,
    dry_run: bool,
) -> bool:
    """Create one worktree. Return whether a path was actually created."""
    if line.storage_for(repo_id) == "anchor-reference":
        if dry_run:
            return False
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.symlink_to(repository_path(config, repo_id), target_is_directory=True)
        return True
    if not dry_run:
        destination.parent.mkdir(parents=True, exist_ok=True)
    require_ok(
        git(repository_path(config, repo_id), *command, dry_run=dry_run, timeout=300),
        f"创建 {repo_id} worktree",
    )
    return not dry_run


def _create_line_worktrees(config: Config, line: Line, *, dry_run: bool) -> Line:
    """Create and roll back only while the caller holds L0 and every L2."""
    planned = _plan_line_creation(config, line)

    created: list[tuple[str, bool]] = []
    try:
        for repo_id, destination, command in planned:
            if _create_repository_worktree(
                config, line, repo_id, destination, command, dry_run=dry_run
            ):
                created.append((repo_id, "-b" in command))
        _write_line(config, line, dry_run=dry_run)
    except Exception as exc:
        if created:
            recovery_failures = _rollback_line_creations(config, line, created)
            if recovery_failures:
                detail = "; ".join(recovery_failures)
                raise DyroError(f"{exc}\n自动清理未完全成功：{detail}") from exc
        raise
    if not dry_run:
        from .instructions import seed_line_overlay

        seed_line_overlay(line_root(config, line), line_id=line.id, branch=line.branch)
    return line


def merge_lock_path(config: Config, line_id: str) -> Path:
    validate_id(line_id, "开发线 ID")
    return config.root / ".dyro" / "lines" / f"{line_id}.merge.lock"


def _validate_parent_link(config: Config, child_id: str, parent: str) -> str:
    if not parent:
        return ""
    parent_id = validate_id(parent, "父开发线 ID")
    if parent_id == child_id:
        raise ValidationError(f"开发线不能以自身为父线：{child_id}")
    ancestor = get_line(config, parent_id)
    seen = {child_id, parent_id}
    current = ancestor.parent
    while current:
        if current == child_id or current in seen:
            raise ValidationError(f"开发线父子关系不能成环：{child_id} -> {parent_id}")
        seen.add(current)
        current = get_line(config, current).parent
    return parent_id


def resolve_spawn_child_id(parent_id: str, child: str) -> str:
    validate_id(parent_id, "父开发线 ID")
    token = child.strip() if isinstance(child, str) else ""
    if not token:
        raise ValidationError("子开发线 ID 不能为空")
    candidate = token if token.startswith(parent_id) else f"{parent_id}_{token}"
    return validate_id(candidate, "开发线 ID")


def _spawn_base_for(config: Config, parent: Line, repo_id: str) -> str:
    worktree = line_repository_path(config, parent, repo_id)
    if _rev_parse(worktree, parent.branch) or _rev_parse(worktree, "HEAD"):
        return parent.branch
    anchor = repository_path(config, repo_id)
    if _rev_parse(anchor, parent.branch):
        return parent.branch
    remote = _expected_remote_branch(parent.branch)
    if _ref_exists(anchor, f"refs/remotes/{remote}"):
        return remote
    raise DyroError(
        f"{repo_id} 找不到父线分支 {parent.branch} 或其 origin 跟踪引用"
    )


def spawn_line(
    config: Config,
    parent_id: str,
    child: str,
    *,
    repositories: Iterable[str] | None = None,
    dry_run: bool = False,
) -> Line:
    """Create a child line from an existing parent line. No fetch, no push."""
    with line_identity_lock(config):
        parent = get_line(config, parent_id)
        child_id = resolve_spawn_child_id(parent.id, child)
        if repositories is None:
            selected = parent.repositories
        else:
            selected = tuple(repositories)
            if not selected:
                raise ValidationError("至少选择一个仓库")
            extra = [repo_id for repo_id in selected if repo_id not in parent.repositories]
            if extra:
                raise ValidationError(
                    f"子线仓库必须是父线 {parent.id} 仓库的子集：{', '.join(extra)}"
                )
        with anchor_locks(config, selected):
            bases = {repo_id: _spawn_base_for(config, parent, repo_id) for repo_id in selected}
            default_base = bases[selected[0]]
            repository_bases = {
                repo_id: base for repo_id, base in bases.items() if base != default_base
            }
            storage_modes = {
                repo_id: parent.storage_for(repo_id)
                for repo_id in selected
                if parent.storage_for(repo_id) != "linked-worktree"
            }
            line = _build_line(
                config,
                line_id=child_id,
                branch=f"feat/{child_id}",
                base=default_base,
                repositories=selected,
                repository_bases=repository_bases,
                storage_modes=storage_modes,
                kind="line",
                parent=parent.id,
            )
            line = _create_line_worktrees(config, line, dry_run=dry_run)
    if not dry_run:
        from .events import append_event

        append_event(
            config,
            kind="spawn",
            actor=parent.id,
            subject=line.id,
            family=parent.id,
            facts={"parent": parent.id, "child": line.id},
        )
    return line


def add_line_repository(
    config: Config,
    line_id: str,
    repo_id: str,
    *,
    base: str | None = None,
    storage: str | None = None,
    dry_run: bool = False,
) -> Line:
    """Add one already-registered repository to one existing line.

    Child lines of this line are not modified. The worktree is created before
    the manifest rewrite; a later failure removes only that new destination.
    """
    with line_identity_lock(config):
        with exclusive_lock(
            merge_lock_path(config, line_id),
            timeout_seconds=MERGE_LOCK_TIMEOUT_SECONDS,
        ):
            line = get_line(config, line_id)
            require_no_unfinished_op(config, line.id)
            if repo_id not in config.repositories:
                raise ValidationError(f"未配置的仓库：{repo_id}")
            if repo_id in line.repositories:
                raise DyroError(f"仓库 {repo_id} 已包含在开发线 {line.id} 中")
            if line.parent:
                parent = get_line(config, line.parent)
                if repo_id not in parent.repositories:
                    raise ValidationError(
                        f"子线仓库必须是父线 {parent.id} 仓库的子集：{repo_id}；"
                        f"请先把该仓库加入父线 {parent.id}"
                    )
                if base is not None or storage is not None:
                    raise ValidationError(
                        f"子线 {line.id} 的仓库基线与存储方式必须从父线 {parent.id} 派生，"
                        "不能指定 --base 或 --storage"
                    )
                resolved_base = _spawn_base_for(config, parent, repo_id)
                resolved_storage = parent.storage_for(repo_id)
            else:
                resolved_base = (base or line.base).strip()
                if not resolved_base:
                    raise ValidationError(f"{repo_id} 的基线不能为空")
                resolved_storage = storage or "linked-worktree"
                if resolved_storage not in STORAGE_MODES:
                    raise ValidationError(
                        f"{repo_id} 的存储方式必须是：{', '.join(sorted(STORAGE_MODES))}"
                    )
            updated = replace(
                line,
                repositories=line.repositories + (repo_id,),
                repository_bases=(
                    {**line.repository_bases, repo_id: resolved_base}
                    if resolved_base != line.base
                    else dict(line.repository_bases)
                ),
                storage_modes=(
                    {**line.storage_modes, repo_id: resolved_storage}
                    if resolved_storage != "linked-worktree"
                    else dict(line.storage_modes)
                ),
            )
            with anchor_locks(config, [repo_id]):
                _repo_id, destination, command = _plan_repository_creation(
                    config, updated, repo_id
                )
                created = False
                try:
                    created = _create_repository_worktree(
                        config,
                        updated,
                        repo_id,
                        destination,
                        command,
                        dry_run=dry_run,
                    )
                    _write_line(config, updated, dry_run=dry_run)
                except Exception as exc:
                    if created:
                        failures: list[str] = []
                        failure = _remove_line_worktree(
                            config, updated, repo_id, destination, new_branch="-b" in command
                        )
                        if failure:
                            failures.append(failure)
                        parent_failure = _remove_empty_parents(
                            destination, line_root(config, line)
                        )
                        if parent_failure:
                            failures.append(parent_failure)
                        if failures:
                            detail = "; ".join(failures)
                            raise DyroError(f"{exc}\n自动清理未完全成功：{detail}") from exc
                    raise
            return updated


@dataclass(frozen=True)
class _LineMergePlan:
    repository: str
    target: Path
    source_head: str
    original_head: str


def _line_ledger(config: Config, subject_id: str, phase: str, **fields: object) -> None:
    from .tasks import ledger

    ledger(config, subject_id, phase, **fields)


def _require_push_allowed(config: Config, *, push: bool) -> None:
    if push and not config.policy.allow_push:
        raise DyroError(
            "当前 Profile 禁止 push；请在 dyro.toml 的 policy.allow_push 显式开启"
        )


def _require_line_worktrees(
    config: Config, line: Line, *, clean: bool, label: str
) -> None:
    for repo_id in line.repositories:
        target = line_repository_path(config, line, repo_id)
        inside = git(target, "rev-parse", "--is-inside-work-tree")
        if inside.code != 0 or inside.stdout.strip() != "true":
            raise DyroError(f"{label} worktree 不存在或不是 Git：{target}")
        current = require_ok(
            git(target, "branch", "--show-current"), f"读取 {repo_id} 分支"
        ).stdout.strip()
        if current != line.branch:
            raise DyroError(
                f"{label}仓库分支错误：{target} 当前 {current or 'DETACHED'}，期望 {line.branch}"
            )
        if clean:
            dirty = require_ok(
                git(target, "status", "--porcelain=v1", "-uall"), f"读取 {repo_id} 状态"
            ).stdout.strip()
            if dirty:
                raise DyroError(f"{label}仓库不干净，拒绝合并：{target}")


def _child_blocking_doctor_findings(config: Config, child: Line) -> list[str]:
    prefix = f"FAIL {child.kind}:{child.id}/"
    return [
        item
        for item in doctor(config)
        if item.startswith(prefix) and not is_missing_origin_finding(item)
    ]


def _prepare_line_merge_plans(
    config: Config,
    *,
    target: Line,
    source: Line,
    repositories: tuple[str, ...],
    push: bool,
    dry_run: bool,
) -> tuple[_LineMergePlan, ...]:
    missing = [repo_id for repo_id in repositories if repo_id not in target.repositories]
    if missing:
        raise DyroError(
            f"仓库不在目标开发线 {target.id} 上：{', '.join(missing)}"
        )
    missing_source = [
        repo_id for repo_id in repositories if repo_id not in source.repositories
    ]
    if missing_source:
        raise DyroError(
            f"仓库不在源开发线 {source.id} 上：{', '.join(missing_source)}"
        )
    plans: list[_LineMergePlan] = []
    for repo_id in repositories:
        target_path = line_repository_path(config, target, repo_id)
        source_path = line_repository_path(config, source, repo_id)
        source_head = require_ok(
            git(source_path, "rev-parse", "HEAD"), f"读取 {repo_id} 源 HEAD"
        ).stdout.strip()
        original_head = require_ok(
            git(target_path, "rev-parse", "HEAD"), f"读取 {repo_id} 目标 HEAD"
        ).stdout.strip()
        plans.append(_LineMergePlan(repo_id, target_path, source_head, original_head))
    if push:
        for plan in plans:
            require_ok(
                git(
                    plan.target,
                    "push",
                    "--dry-run",
                    "origin",
                    target.branch,
                ),
                f"预检推送 {plan.repository}",
            )
    return tuple(plans)


def _rollback_line_merges(
    plans: Iterable[_LineMergePlan], committed_heads: dict[str, str]
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


def _abort_line_merge_probes(plans: Iterable[_LineMergePlan]) -> list[str]:
    return _rollback_line_merges(plans, {})


def _record_line_merge_changeset(
    config: Config, line: Line, repositories: tuple[str, ...]
) -> str:
    from datetime import datetime, timezone

    from .changesets import create_changeset

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    changeset_id = f"merge_{line.id}_{stamp}"
    if len(changeset_id) > 80:
        changeset_id = f"merge_{stamp}"
    try:
        create_changeset(
            config,
            changeset_id=changeset_id,
            line_id=line.id,
            repositories=repositories,
        )
    except DyroError:
        return ""
    return changeset_id


def _merge_line_repositories(
    config: Config,
    *,
    target: Line,
    source: Line,
    repositories: tuple[str, ...],
    lock_line_id: str,
    message: str,
    phase: str,
    push: bool,
    dry_run: bool,
    extra_ledger: Mapping[str, object] | None = None,
) -> None:
    with exclusive_lock(
        merge_lock_path(config, lock_line_id),
        timeout_seconds=MERGE_LOCK_TIMEOUT_SECONDS,
    ):
        with anchor_locks(
            config, repositories, timeout_seconds=MERGE_LOCK_TIMEOUT_SECONDS
        ):
            if get_line(config, target.id) != target or get_line(config, source.id) != source:
                raise DyroError("开发线清单已变化，请重新运行合并或同步")
            require_no_unfinished_op(config, target.id, kinds=MERGE_OP_KINDS)
            for line in (target, source):
                require_no_unfinished_op(config, line.id, kinds=["retire"])
            _require_line_worktrees(config, target, clean=True, label="目标开发线")
            _require_line_worktrees(config, source, clean=False, label="源开发线")
            _merge_line_repositories_locked(
                config,
                target=target,
                source=source,
                repositories=repositories,
                message=message,
                phase=phase,
                push=push,
                dry_run=dry_run,
                extra_ledger=extra_ledger,
            )


def _merge_line_repositories_locked(
    config: Config,
    *,
    target: Line,
    source: Line,
    repositories: tuple[str, ...],
    message: str,
    phase: str,
    push: bool,
    dry_run: bool,
    extra_ledger: Mapping[str, object] | None = None,
) -> None:
    plans = _prepare_line_merge_plans(
        config,
        target=target,
        source=source,
        repositories=repositories,
        push=push,
        dry_run=dry_run,
    )
    fields = dict(extra_ledger or {})
    probed: list[_LineMergePlan] = []
    try:
        for plan in plans:
            probed.append(plan)
            result = git(
                plan.target,
                "merge",
                "--no-ff",
                "--no-commit",
                plan.source_head,
                timeout=300,
            )
            if result.code != 0:
                raise DyroError(
                    f"预检合并 {plan.repository} 存在冲突，拒绝合并"
                    + (f"\n{result.stdout.strip()}" if result.stdout.strip() else "")
                )
        if dry_run:
            recovery_failures = _abort_line_merge_probes(probed)
            if recovery_failures:
                raise DyroError(
                    "预检合并后清理未完全成功：" + "; ".join(recovery_failures)
                )
            return
    except DyroError as exc:
        recovery_failures = _abort_line_merge_probes(probed)
        if not dry_run:
            _line_ledger(
                config,
                source.id,
                f"{phase}_failed",
                error=str(exc),
                recovered=not recovery_failures,
                recovery_failures=recovery_failures,
                **fields,
            )
        if recovery_failures:
            raise DyroError(
                f"{exc}\n自动恢复未完全成功：{'; '.join(recovery_failures)}"
            ) from exc
        raise

    committed_heads: dict[str, str] = {}
    try:
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
        recovery_failures = _rollback_line_merges(plans, committed_heads)
        _line_ledger(
            config,
            source.id,
            f"{phase}_failed",
            error=str(exc),
            recovered=not recovery_failures,
            recovery_failures=recovery_failures,
            **fields,
        )
        if recovery_failures:
            raise DyroError(
                f"{exc}\n自动恢复未完全成功：{'; '.join(recovery_failures)}"
            ) from exc
        raise

    pushed: list[str] = []
    if push:
        for plan in plans:
            result = git(plan.target, "push", "origin", target.branch)
            if result.code != 0:
                _line_ledger(
                    config,
                    source.id,
                    "push_failed",
                    repository=plan.repository,
                    pushed_repositories=pushed,
                    error=result.stdout.strip(),
                    **fields,
                )
                raise DyroError(
                    f"推送 {plan.repository} 失败；本地合并已保留，已推送仓库：{', '.join(pushed) or '-'}"
                    f"\n{result.stdout.strip()}"
                )
            pushed.append(plan.repository)
            target = record_remote_anchor(config, target, plan.repository, plan.target)

    changeset_id = ""
    if not dry_run:
        changeset_id = _record_line_merge_changeset(config, target, repositories)

    for plan in plans:
        result_head = require_ok(
            git(plan.target, "rev-parse", "HEAD"), f"读取 {plan.repository} 合并结果"
        ).stdout.strip()
        _line_ledger(
            config,
            source.id,
            phase,
            repository=plan.repository,
            branch=target.branch,
            source_head=plan.source_head,
            previous_head=plan.original_head,
            result_head=result_head,
            pushed=push,
            changeset_id=changeset_id,
            **fields,
        )


def record_remote_anchor(config: Config, line: Line, repo_id: str, path: Path) -> Line:
    if line.storage_for(repo_id) != "linked-worktree":
        return line
    head = require_ok(git_read(path, "rev-parse", "HEAD"), "读取已推送 HEAD").stdout.strip()
    updated = replace(line, remote_anchors={**line.remote_anchors, repo_id: head})
    _write_line(config, updated)
    return updated


def merge_line(
    config: Config,
    child_id: str,
    parent_id: str,
    *,
    push: bool = False,
    dry_run: bool = False,
) -> None:
    """Merge a child line into its direct parent. One level only."""
    _require_push_allowed(config, push=push)
    child = get_line(config, child_id)
    parent = get_line(config, parent_id)
    if not child.parent:
        raise DyroError(f"开发线 {child.id} 没有父线，无法合并")
    if child.parent != parent.id:
        raise DyroError(
            f"只能将子线合并到其直接父线：{child.id} 的父线是 {child.parent}，不是 {parent.id}"
        )
    require_no_unfinished_op(config, parent.id, kinds=MERGE_OP_KINDS)
    _require_line_worktrees(config, parent, clean=True, label="父开发线")
    _require_line_worktrees(config, child, clean=False, label="子开发线")
    blocking = _child_blocking_doctor_findings(config, child)
    if blocking:
        raise DyroError(f"子线 {child.id} 未通过 doctor，拒绝合并：{blocking[0]}")
    missing = set(child.repositories) - set(parent.repositories)
    if missing:
        raise DyroError(f"仓库不在目标开发线 {parent.id} 上：{', '.join(sorted(missing))}")
    _refuse_anchor_reference_targets(parent, child.repositories)
    _run_journaled_line_merge(
        config,
        kind="merge",
        target=parent,
        source=child,
        repositories=tuple(sorted(child.repositories)),
        message=f"merge(line): {child.id} -> {parent.id}",
        effective_options={
            "integration": "into",
            "allow_partial": False,
            "source": child.id,
            "target": parent.id,
        },
        event_kind="merge",
        event_actor=child.id,
        event_subject=parent.id,
        event_family=parent.id,
        partial=False,
        excluded=(),
        ledger_subject_id=child.id,
        ledger_extra={"parent": parent.id, "child": child.id},
        push=push,
        dry_run=dry_run,
    )


def sync_line(
    config: Config,
    child_id: str,
    *,
    push: bool = False,
    dry_run: bool = False,
) -> None:
    """Merge parent commits into a child line. Requires a parent."""
    _require_push_allowed(config, push=push)
    child = get_line(config, child_id)
    if not child.parent:
        raise DyroError(f"开发线 {child.id} 没有父线，无法同步")
    parent = get_line(config, child.parent)
    require_no_unfinished_op(config, child.id, kinds=MERGE_OP_KINDS)
    _require_line_worktrees(config, child, clean=True, label="子开发线")
    _require_line_worktrees(config, parent, clean=False, label="父开发线")
    _merge_line_repositories(
        config,
        target=child,
        source=parent,
        repositories=child.repositories,
        lock_line_id=child.id,
        message=f"sync(line): {parent.id} -> {child.id}",
        phase="line_sync",
        push=push,
        dry_run=dry_run,
        extra_ledger={"parent": parent.id, "child": child.id},
    )
    if not dry_run:
        from .events import append_event

        append_event(
            config,
            kind="sync",
            actor=parent.id,
            subject=child.id,
            family=parent.id,
            facts={"parent": parent.id, "child": child.id},
        )


MERGE_OP_KINDS = frozenset({"merge", "merge_into_base"})
_MERGE_KNOWN_OUTCOMES = frozenset(
    {"not_started", "committed", "already_included", "rolled_back"}
)
_GIT_IN_PROGRESS_NAMES = (
    "MERGE_HEAD",
    "CHERRY_PICK_HEAD",
    "REBASE_HEAD",
    "REVERT_HEAD",
    "BISECT_LOG",
)


@dataclass(frozen=True)
class LineMergeResult:
    op_id: str
    target_id: str
    source_id: str
    repositories: tuple[str, ...]
    excluded_repositories: tuple[str, ...]
    partial: bool
    outcomes: Mapping[str, str]
    dry_run: bool = False


def _repos_digest(repositories: Iterable[str]) -> str:
    digest = "+".join(sorted(set(repositories)))
    return digest[:80]


def _is_ancestor(repo: Path, maybe_ancestor: str, head: str = "HEAD") -> bool:
    return git(repo, "merge-base", "--is-ancestor", maybe_ancestor, head).code == 0


def _head_sha(repo: Path) -> str:
    return require_ok(git(repo, "rev-parse", "HEAD"), f"读取 {repo} HEAD").stdout.strip()


def _has_merge_head(repo: Path) -> bool:
    return git(repo, "rev-parse", "--verify", "-q", "MERGE_HEAD").code == 0


def _git_in_progress(repo: Path) -> bool:
    if any(
        git(repo, "rev-parse", "--verify", "-q", name).code == 0
        for name in _GIT_IN_PROGRESS_NAMES
    ):
        return True
    git_dir = require_ok(
        git(repo, "rev-parse", "--git-dir"), f"读取 {repo} git-dir"
    ).stdout.strip()
    root = Path(git_dir) if Path(git_dir).is_absolute() else repo / git_dir
    return (root / "rebase-merge").exists() or (root / "rebase-apply").exists()


def _refuse_anchor_reference_targets(target: Line, repositories: Iterable[str]) -> None:
    blocked = [
        repo_id
        for repo_id in repositories
        if target.storage_for(repo_id) == "anchor-reference"
    ]
    if blocked:
        raise DyroError(
            "Wave 1 拒绝对 anchor-reference 目标执行 git merge："
            + ", ".join(blocked)
        )


def _intersection_repositories(
    target: Line, source: Line
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    shared = tuple(sorted(set(target.repositories) & set(source.repositories)))
    if not shared:
        raise DyroError(
            f"开发线 {source.id} 与 {target.id} 没有可合入的仓库交集"
        )
    excluded = tuple(sorted(set(source.repositories) - set(target.repositories)))
    return shared, excluded


def _ledger_has_op_repo(config: Config, *, op_id: str, repository: str) -> bool:
    if not config.ledger_file.exists():
        return False
    try:
        lines = config.ledger_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for raw in lines:
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if (
            isinstance(row, dict)
            and row.get("op_id") == op_id
            and row.get("repository") == repository
        ):
            return True
    return False


def _event_has_op_id(config: Config, op_id: str) -> bool:
    from .events import read_events_fail_closed

    return any(
        isinstance(event.get("facts"), dict) and event["facts"].get("op_id") == op_id
        for event in read_events_fail_closed(config)
    )


def _merge_manual_recovery_commands(
    repo_id: str, target_path: Path, original_head: str, *, has_merge_head: bool
) -> list[str]:
    quoted = str(target_path)
    commands = []
    if has_merge_head:
        commands.append(f"git -C {quoted} merge --abort")
    commands.append(
        f"先把要保留的提交另存：git -C {quoted} branch <keep-name> HEAD"
    )
    commands.append(
        f"破坏性：git -C {quoted} reset --hard {original_head}"
        f"（会丢弃工作区未提交改动；仓 {repo_id}）"
    )
    commands.append(
        "不要手动完成合并提交来收口；回到 original_head 后重跑 "
        "`dyro line merge … --recover`，再以新 op 重新合并"
    )
    return commands


def _write_merge_ledger_row(
    config: Config,
    *,
    subject_id: str,
    phase: str,
    repository: str,
    op_id: str,
    branch: str,
    source_head: str,
    previous_head: str,
    result_head: str,
    outcome: str,
    extra: Mapping[str, object] | None = None,
) -> None:
    if _ledger_has_op_repo(config, op_id=op_id, repository=repository):
        return
    fields = dict(extra or {})
    _line_ledger(
        config,
        subject_id,
        phase,
        repository=repository,
        op_id=op_id,
        branch=branch,
        source_head=source_head,
        previous_head=previous_head,
        result_head=result_head,
        outcome=outcome,
        **fields,
    )


def _require_anchor_base_ready(
    config: Config, repo_id: str, *, base_ref: str
) -> Path:
    anchor = repository_path(config, repo_id)
    inside = git(anchor, "rev-parse", "--is-inside-work-tree")
    if inside.code != 0 or inside.stdout.strip() != "true":
        raise DyroError(f"基线仓不是 Git worktree：{anchor}")
    dirty = require_ok(
        git(anchor, "status", "--porcelain=v1", "-uall"), f"读取 {repo_id} 基线状态"
    ).stdout.strip()
    if dirty:
        raise DyroError(f"基线仓不干净，拒绝合入：{anchor}")
    current = require_ok(
        git(anchor, "branch", "--show-current"), f"读取 {repo_id} 基线分支"
    ).stdout.strip()
    if current != base_ref:
        raise DyroError(
            f"基线仓当前分支为 {current or 'DETACHED'}，期望 {base_ref}；"
            "不会自动 checkout"
        )
    if _git_in_progress(anchor):
        raise DyroError(f"基线仓有进行中的 Git 操作，拒绝合入：{anchor}")
    return anchor


def _resolve_merge_into_base_refs(
    config: Config,
    line: Line,
    repositories: tuple[str, ...],
    *,
    base_ref: str | None,
    policy_default: bool,
) -> dict[str, str]:
    if base_ref and policy_default:
        raise ValidationError("--base-ref 与 --policy-default 不能同时使用")
    resolved: dict[str, str] = {}
    for repo_id in repositories:
        if policy_default:
            resolved[repo_id] = config.policy.default_base
        elif base_ref:
            resolved[repo_id] = base_ref
        else:
            resolved[repo_id] = line.base_for(repo_id)
    return resolved


def _prepare_cross_merge_plans(
    config: Config,
    *,
    target_paths: Mapping[str, Path],
    source: Line,
    repositories: tuple[str, ...],
) -> tuple[_LineMergePlan, ...]:
    plans: list[_LineMergePlan] = []
    for repo_id in repositories:
        target_path = target_paths[repo_id]
        source_path = line_repository_path(config, source, repo_id)
        source_head = require_ok(
            git(source_path, "rev-parse", "HEAD"), f"读取 {repo_id} 源 HEAD"
        ).stdout.strip()
        original_head = require_ok(
            git(target_path, "rev-parse", "HEAD"), f"读取 {repo_id} 目标 HEAD"
        ).stdout.strip()
        plans.append(_LineMergePlan(repo_id, target_path, source_head, original_head))
    return tuple(plans)


def _probe_line_merge_plans(plans: tuple[_LineMergePlan, ...]) -> None:
    probed: list[_LineMergePlan] = []
    try:
        for plan in plans:
            probed.append(plan)
            result = git(
                plan.target,
                "merge",
                "--no-ff",
                "--no-commit",
                plan.source_head,
                timeout=300,
            )
            if result.code != 0:
                raise DyroError(
                    f"预检合并 {plan.repository} 存在冲突，拒绝合并"
                    + (f"\n{result.stdout.strip()}" if result.stdout.strip() else "")
                )
    except DyroError:
        recovery_failures = _abort_line_merge_probes(probed)
        if recovery_failures:
            raise DyroError(
                "预检合并后清理未完全成功：" + "; ".join(recovery_failures)
            )
        raise


def _persist_rollback_outcomes(
    config: Config,
    op_id: str,
    plans: tuple[_LineMergePlan, ...],
    committed_heads: Mapping[str, str],
    recovery_failures: list[str],
) -> dict[str, str]:
    failed_repos = {
        item.split(":", 1)[0]
        for item in recovery_failures
        if ":" in item
    }
    outcomes: dict[str, str] = {}
    for plan in plans:
        repo_id = plan.repository
        if repo_id in failed_repos:
            update_repo_phase(config, op_id, repo_id, phase="unknown", outcome="unknown")
            outcomes[repo_id] = "unknown"
            continue
        current = _head_sha(plan.target)
        if current != plan.original_head or _has_merge_head(plan.target):
            update_repo_phase(config, op_id, repo_id, phase="unknown", outcome="unknown")
            outcomes[repo_id] = "unknown"
            continue
        if repo_id not in committed_heads:
            if "merge" in load_op(config, op_id)["repositories"][repo_id]["steps"]:
                step = load_op(config, op_id)["repositories"][repo_id]["steps"]["merge"]
                if "result" not in step:
                    record_repo_result(
                        config,
                        op_id,
                        repo_id,
                        "merge",
                        outcome="not_started",
                        new_tip=plan.original_head,
                    )
            update_repo_phase(
                config, op_id, repo_id, phase="completed", outcome="not_started"
            )
            outcomes[repo_id] = "not_started"
            continue
        record_repo_intent(
            config,
            op_id,
            repo_id,
            "rollback",
            original_head=plan.original_head,
            target_sha=plan.original_head,
            from_head=committed_heads[repo_id],
        )
        record_repo_result(
            config,
            op_id,
            repo_id,
            "rollback",
            outcome="rolled_back",
            new_tip=plan.original_head,
        )
        update_repo_phase(
            config, op_id, repo_id, phase="completed", outcome="rolled_back"
        )
        outcomes[repo_id] = "rolled_back"
    return outcomes


def _commit_journaled_merge_plans(
    config: Config,
    *,
    op_id: str,
    plans: tuple[_LineMergePlan, ...],
    message: str,
) -> dict[str, str]:
    committed_heads: dict[str, str] = {}
    outcomes: dict[str, str] = {}
    try:
        for plan in plans:
            record_repo_intent(
                config,
                op_id,
                plan.repository,
                "merge",
                original_head=plan.original_head,
                target_sha=plan.source_head,
            )
            if _has_merge_head(plan.target):
                require_ok(
                    git(plan.target, "commit", "-m", message, timeout=300),
                    f"提交 {plan.repository} 合并",
                )
                result_head = _head_sha(plan.target)
                record_repo_result(
                    config,
                    op_id,
                    plan.repository,
                    "merge",
                    outcome="committed",
                    new_tip=result_head,
                )
                update_repo_phase(
                    config,
                    op_id,
                    plan.repository,
                    phase="completed",
                    outcome="committed",
                )
                committed_heads[plan.repository] = result_head
                outcomes[plan.repository] = "committed"
                continue
            if not _is_ancestor(plan.target, plan.source_head, plan.original_head):
                raise DyroError(
                    f"仓库 {plan.repository} 无 MERGE_HEAD 且源提交不是目标祖先，拒绝合入"
                )
            record_repo_result(
                config,
                op_id,
                plan.repository,
                "merge",
                outcome="already_included",
                new_tip=plan.original_head,
            )
            update_repo_phase(
                config,
                op_id,
                plan.repository,
                phase="completed",
                outcome="already_included",
            )
            outcomes[plan.repository] = "already_included"
    except DyroError as exc:
        recovery_failures = _rollback_line_merges(plans, committed_heads)
        outcomes.update(
            _persist_rollback_outcomes(
                config, op_id, plans, committed_heads, recovery_failures
            )
        )
        if recovery_failures:
            raise DyroError(
                f"{exc}\n自动恢复未完全成功：{'; '.join(recovery_failures)}"
            ) from exc
        raise
    return outcomes


def _ledger_successful_merge_repos(
    config: Config,
    *,
    op: Mapping[str, object],
    subject_id: str,
    branches: Mapping[str, str],
    extra: Mapping[str, object] | None = None,
) -> None:
    op_id = str(op["op_id"])
    repositories = op["repositories"]
    if not isinstance(repositories, dict):
        raise ValidationError("操作日志仓库表格式错误")
    for repo_id, repo in repositories.items():
        if not isinstance(repo, dict):
            continue
        outcome = str(repo.get("outcome") or "")
        if outcome not in {"committed", "already_included"}:
            continue
        snapshot = repo.get("snapshot") if isinstance(repo.get("snapshot"), dict) else {}
        source_head = str(snapshot.get("source_head") or "")
        original_head = str(snapshot.get("original_head") or "")
        result_head = original_head
        merge_step = repo.get("steps", {}).get("merge") if isinstance(repo.get("steps"), dict) else None
        if isinstance(merge_step, dict) and isinstance(merge_step.get("result"), dict):
            tip = merge_step["result"].get("new_tip")
            if isinstance(tip, str) and tip:
                result_head = tip
        target_path = Path(str(snapshot.get("path") or ""))
        if outcome == "committed" and (
            not target_path.exists() or not isinstance(merge_step, dict)
            or merge_step.get("result", {}).get("outcome") != "committed"
            or _head_sha(target_path) != result_head or _has_merge_head(target_path)
        ):
            raise DyroError(f"{repo_id}: committed 记录与 HEAD 不一致；请 --recover 对账")
        if outcome == "already_included":
            if (not target_path.exists() or not source_head
                    or _head_sha(target_path) != original_head
                    or _has_merge_head(target_path)
                    or not _is_ancestor(target_path, source_head)):
                if op["phase"] not in TERMINAL_PHASES:
                    update_repo_phase(config, op_id, repo_id, phase="unknown", outcome="unknown")
                raise DyroError(f"{repo_id}: already_included 核验失败；请 --recover 对账")
        repo_extra = extra
        if op["effective_options"].get("push"):
            push_step = repo["steps"].get("push", {})
            repo_extra = dict(
                extra or {}, pushed=push_step.get("result", {}).get("outcome") == "pushed"
            )
        _write_merge_ledger_row(
            config,
            subject_id=subject_id,
            phase="line_merge",
            repository=str(repo_id),
            op_id=op_id,
            branch=branches[str(repo_id)],
            source_head=source_head,
            previous_head=original_head,
            result_head=result_head,
            outcome=outcome,
            extra=repo_extra,
        )


def _append_merge_event_if_needed(
    config: Config,
    *,
    op_id: str,
    kind: str,
    actor: str,
    subject: str,
    family: str,
    facts: Mapping[str, object],
) -> None:
    if _event_has_op_id(config, op_id):
        return
    from .events import append_event

    append_event(
        config,
        kind=kind,
        actor=actor,
        subject=subject,
        family=family,
        facts=facts,
    )


def _create_merge_op(
    config: Config,
    *,
    kind: str,
    subject_line_ids: Iterable[str],
    target_id: str,
    source_id: str,
    plans: tuple[_LineMergePlan, ...],
    target_storage: Mapping[str, str],
    effective_options: Mapping[str, object],
    event_kind: str,
    event_actor: str,
    event_subject: str,
    event_family: str,
    partial: bool,
) -> dict:
    import uuid

    from .events import validate_event_write

    repos = tuple(plan.repository for plan in plans)
    op_id = uuid.uuid4().hex
    event_facts = {
        "op_id": op_id,
        "repo_count": len(repos),
        "repos_digest": _repos_digest(repos),
        "partial": partial,
    }
    validate_event_write(
        kind=event_kind,
        actor=event_actor,
        subject=event_subject,
        family=event_family,
        facts=event_facts,
    )
    snapshots = {
        plan.repository: {
            "path": str(plan.target),
            "storage_mode": target_storage.get(plan.repository, "linked-worktree"),
            "original_head": plan.original_head,
            "source_head": plan.source_head,
        }
        for plan in plans
    }
    create_op(
        config,
        op_id=op_id,
        kind=kind,
        subject_line_ids=subject_line_ids,
        manifest_snapshot={
            "target": target_id,
            "source": source_id,
            "repositories": list(repos),
            "event": {
                "kind": event_kind, "actor": event_actor,
                "subject": event_subject, "family": event_family,
            },
        },
        repos_ordered=repos,
        effective_options=effective_options,
        repository_snapshots=snapshots,
        event_facts=event_facts,
    )
    for repo_id in repos:
        update_repo_phase(
            config, op_id, repo_id, phase="pending", outcome="not_started"
        )
    return load_op(config, op_id)


def _run_journaled_line_merge(
    config: Config,
    *,
    kind: str,
    target: Line | None,
    source: Line,
    repositories: tuple[str, ...],
    message: str,
    effective_options: Mapping[str, object],
    event_kind: str,
    event_actor: str,
    event_subject: str,
    event_family: str,
    partial: bool,
    excluded: tuple[str, ...],
    ledger_subject_id: str,
    ledger_extra: Mapping[str, object] | None = None,
    push: bool = False,
    dry_run: bool = False,
) -> LineMergeResult:
    repos = repositories
    target_id = target.id if target else "base"
    lock_line_id = target.id if target else source.id
    with exclusive_lock(
        merge_lock_path(config, lock_line_id),
        timeout_seconds=MERGE_LOCK_TIMEOUT_SECONDS,
    ):
        with anchor_locks(
            config, repos, timeout_seconds=MERGE_LOCK_TIMEOUT_SECONDS
        ):
            require_no_unfinished_op(config, lock_line_id, kinds=MERGE_OP_KINDS)
            for line in (source, target):
                if line is not None:
                    require_no_unfinished_op(config, line.id, kinds=["retire"])
            if get_line(config, source.id) != source or (
                target is not None and get_line(config, target.id) != target
            ):
                raise DyroError("开发线清单已变化，请重新运行合并")
            _require_line_worktrees(config, source, clean=False, label="源开发线")
            if target is not None:
                _require_line_worktrees(config, target, clean=True, label="目标开发线")
                paths = {repo: line_repository_path(config, target, repo) for repo in repos}
                branches = {repo: target.branch for repo in repos}
                storage = {repo: target.storage_for(repo) for repo in repos}
            else:
                branches = dict(effective_options["base_refs"])
                paths = {
                    repo: _require_anchor_base_ready(config, repo, base_ref=branches[repo])
                    for repo in repos
                }
                storage = {repo: "anchor" for repo in repos}
            for repo, path in paths.items():
                if target is not None and _git_in_progress(path):
                    raise DyroError(f"{repo} 有进行中的 Git 操作，拒绝合并")
            plans = _prepare_cross_merge_plans(
                config, target_paths=paths, source=source, repositories=repos
            )
            if push:
                for plan in plans:
                    require_ok(
                        git(plan.target, "push", "--dry-run", "origin", branches[plan.repository]),
                        f"预检推送 {plan.repository}",
                    )
            if dry_run:
                _probe_line_merge_plans(plans)
                failures = _abort_line_merge_probes(plans)
                if failures:
                    raise DyroError("预检合并后清理未完全成功：" + "; ".join(failures))
                return LineMergeResult(
                    op_id="", target_id=target_id, source_id=source.id,
                    repositories=repos, excluded_repositories=excluded, partial=partial,
                    outcomes={repo: "dry_run" for repo in repos}, dry_run=True,
                )
            op = _create_merge_op(
                config,
                kind=kind,
                subject_line_ids=(lock_line_id,),
                target_id=target_id,
                source_id=source.id,
                plans=plans,
                target_storage=storage,
                effective_options=dict(effective_options, push=True) if push else effective_options,
                event_kind=event_kind,
                event_actor=event_actor,
                event_subject=event_subject,
                event_family=event_family,
                partial=partial,
            )
            op_id = op["op_id"]
            try:
                _probe_line_merge_plans(plans)
                outcomes = _commit_journaled_merge_plans(
                    config, op_id=op_id, plans=plans, message=message
                )
            except DyroError:
                op = load_op(config, op_id)
                if all(
                    isinstance(repo, dict)
                    and str(repo.get("outcome")) in _MERGE_KNOWN_OUTCOMES
                    for repo in op["repositories"].values()
                ):
                    try:
                        finish_op(config, op_id, phase="aborted")
                    except DyroError:
                        pass
                raise
            op = load_op(config, op_id)
            if effective_options.get("integration") == "into":
                ledger_extra = dict(
                    ledger_extra or {}, pushed=push,
                    changeset_id=_record_line_merge_changeset(config, target, repos),
                )
            if push:
                pushed: list[str] = []
                for index, plan in enumerate(plans):
                    push_head = _head_sha(plan.target)
                    record_repo_intent(
                        config, op_id, plan.repository, "push",
                        original_head=plan.original_head, target_sha=push_head,
                        branch=branches[plan.repository], remote="origin",
                    )
                    result = git(plan.target, "push", "origin", branches[plan.repository])
                    if result.code == 0:
                        target = record_remote_anchor(config, target, plan.repository, plan.target)
                    record_repo_result(
                        config, op_id, plan.repository, "push",
                        outcome="pushed" if result.code == 0 else "push_failed",
                        new_tip=push_head if result.code == 0 else "",
                        error=result.stdout.strip() if result.code != 0 else "",
                    )
                    if result.code != 0:
                        for remaining in plans[index + 1:]:
                            record_repo_intent(
                                config, op_id, remaining.repository, "push",
                                original_head=remaining.original_head, target_sha=_head_sha(remaining.target),
                                branch=branches[remaining.repository], remote="origin",
                            )
                            record_repo_result(
                                config, op_id, remaining.repository, "push", outcome="not_attempted",
                            )
                        _line_ledger(
                            config, source.id, "push_failed", repository=plan.repository,
                            pushed_repositories=pushed, error=result.stdout.strip(),
                            **dict(ledger_extra or {}, pushed=False),
                        )
                        _ledger_successful_merge_repos(
                            config,
                            op=load_op(config, op_id),
                            subject_id=ledger_subject_id,
                            branches=branches,
                            extra=ledger_extra,
                        )
                        finish_op(config, op_id, phase="completed")
                        raise DyroError(
                            f"推送 {plan.repository} 失败；本地合并已保留，已推送仓库："
                            f"{', '.join(pushed) or '-'}\n{result.stdout.strip()}"
                        )
                    pushed.append(plan.repository)
            _ledger_successful_merge_repos(
                config,
                op=load_op(config, op_id) if push else op,
                subject_id=ledger_subject_id,
                branches=branches,
                extra=ledger_extra,
            )
            finish_op(config, op_id, phase="completed")
    _append_merge_event_if_needed(
        config,
        op_id=op_id,
        kind=event_kind,
        actor=event_actor,
        subject=event_subject,
        family=event_family,
        facts=dict(load_op(config, op_id)["event_facts"]),
    )
    return LineMergeResult(
        op_id=op_id,
        target_id=target_id,
        source_id=source.id,
        repositories=repos,
        excluded_repositories=excluded,
        partial=partial,
        outcomes=outcomes,
    )


def merge_line_from(
    config: Config,
    target_id: str,
    source_id: str,
    *,
    allow_partial: bool = False,
    push: bool = False,
    dry_run: bool = False,
) -> LineMergeResult:
    """Merge source line tips into target line worktrees without changing parent."""
    if push:
        raise DyroError("跨线 merge --from 暂不支持 --push")
    _require_push_allowed(config, push=push)
    target = get_line(config, target_id)
    source = get_line(config, source_id)
    if target.id == source.id:
        raise DyroError("不能将开发线合并到自身")
    require_no_unfinished_op(config, target.id, kinds=MERGE_OP_KINDS)
    repositories, excluded = _intersection_repositories(target, source)
    partial = bool(excluded)
    _refuse_anchor_reference_targets(target, repositories)
    _require_line_worktrees(config, target, clean=True, label="目标开发线")
    _require_line_worktrees(config, source, clean=False, label="源开发线")
    return _run_journaled_line_merge(
        config,
        kind="merge",
        target=target,
        source=source,
        repositories=repositories,
        message=f"merge(line): {source.id} -> {target.id}",
        effective_options={
            "allow_partial": allow_partial,
            "integration": "from",
            "source": source.id,
            "target": target.id,
        },
        event_kind="merge_cross",
        event_actor=source.id,
        event_subject=target.id,
        event_family=target.id,
        partial=partial,
        excluded=excluded,
        ledger_subject_id=source.id,
        ledger_extra={"target": target.id, "source": source.id},
        dry_run=dry_run,
    )


def merge_line_into_base(
    config: Config,
    line_id: str,
    *,
    base_ref: str | None = None,
    policy_default: bool = False,
    repositories: Iterable[str] | None = None,
    push: bool = False,
    dry_run: bool = False,
) -> LineMergeResult:
    """Merge a line into currently checked-out baseline branches on anchors."""
    if push:
        raise DyroError("merge-into-base 暂不支持 --push")
    _require_push_allowed(config, push=push)
    line = get_line(config, line_id)
    require_no_unfinished_op(config, line.id, kinds=MERGE_OP_KINDS)
    selected = tuple(repositories) if repositories is not None else line.repositories
    if not selected:
        raise ValidationError("至少选择一个仓库")
    missing = [repo_id for repo_id in selected if repo_id not in line.repositories]
    if missing:
        raise DyroError(f"仓库不在开发线 {line.id} 上：{', '.join(missing)}")
    selected = tuple(sorted(set(selected)))
    base_refs = _resolve_merge_into_base_refs(
        config,
        line,
        selected,
        base_ref=base_ref,
        policy_default=policy_default,
    )
    anchor_ref_repos = [
        repo_id
        for repo_id in selected
        if line.storage_for(repo_id) == "anchor-reference"
    ]
    if anchor_ref_repos:
        raise DyroError(
            "merge-into-base 拒绝 anchor-reference 仓库（源与基线同为 anchor）："
            + ", ".join(anchor_ref_repos)
        )
    _require_line_worktrees(config, line, clean=False, label="源开发线")
    return _run_journaled_line_merge(
        config,
        kind="merge_into_base",
        target=None,
        source=line,
        repositories=selected,
        message=f"merge(line): {line.id} -> base",
        effective_options={
            "integration": "into_base",
            "base_ref": base_ref or "",
            "policy_default": policy_default,
            "base_refs": base_refs,
        },
        event_kind="merge_cross",
        event_actor=line.id,
        event_subject=line.id,
        event_family=line.parent or line.id,
        partial=False,
        excluded=(),
        ledger_subject_id=line.id,
        ledger_extra={"target": "base", "source": line.id},
        dry_run=dry_run,
    )


def _reconcile_merge_repo(
    config: Config,
    op: dict,
    repo_id: str,
    *,
    report: list[str],
) -> str:
    repo = op["repositories"][repo_id]
    snapshot = repo.get("snapshot") if isinstance(repo.get("snapshot"), dict) else {}
    target_path = Path(str(snapshot.get("path") or ""))
    original_head = str(snapshot.get("original_head") or "")
    source_head = str(snapshot.get("source_head") or "")
    steps = repo.get("steps") if isinstance(repo.get("steps"), dict) else {}
    merge_step = steps.get("merge") if isinstance(steps, dict) else None
    merge_result = (
        merge_step.get("result")
        if isinstance(merge_step, dict) and isinstance(merge_step.get("result"), dict)
        else None
    )
    rollback = steps.get("rollback") if isinstance(steps, dict) else None
    current_outcome = str(repo.get("outcome") or "")

    if not target_path.exists() or not original_head:
        update_repo_phase(config, op["op_id"], repo_id, phase="unknown", outcome="unknown")
        report.append(f"{repo_id}: unknown（目标路径或 original_head 不可用）")
        return "unknown"

    try:
        head = _head_sha(target_path)
    except DyroError:
        update_repo_phase(config, op["op_id"], repo_id, phase="unknown", outcome="unknown")
        report.append(f"{repo_id}: unknown（无法读取 HEAD）")
        return "unknown"

    has_merge = _has_merge_head(target_path)
    result_head = ""
    if isinstance(merge_result, dict):
        tip = merge_result.get("new_tip")
        if isinstance(tip, str):
            result_head = tip
        merge_outcome = str(merge_result.get("outcome") or "")
    else:
        merge_outcome = ""

    # unknown is sticky only for one pass; always re-enter the table.
    if isinstance(rollback, dict) and isinstance(rollback.get("result"), dict):
        if str(rollback["result"].get("outcome")) == "rolled_back":
            if head == original_head and not has_merge:
                update_repo_phase(
                    config, op["op_id"], repo_id, phase="completed", outcome="rolled_back"
                )
                report.append(f"{repo_id}: rolled_back（已调和）")
                return "rolled_back"
            update_repo_phase(
                config, op["op_id"], repo_id, phase="unknown", outcome="unknown"
            )
            report.append(f"{repo_id}: unknown（标记 rolled_back 但 HEAD 已漂移）")
            for command in _merge_manual_recovery_commands(
                repo_id, target_path, original_head, has_merge_head=has_merge
            ):
                report.append(f"  手动：{command}")
            return "unknown"

    if merge_outcome == "committed" and result_head:
        if head == result_head and not has_merge:
            update_repo_phase(
                config, op["op_id"], repo_id, phase="completed", outcome="committed"
            )
            report.append(f"{repo_id}: committed（已调和）")
            return "committed"
        if head == original_head and not has_merge:
            record_repo_intent(
                config,
                op["op_id"],
                repo_id,
                "rollback",
                original_head=original_head,
                target_sha=original_head,
                from_head=result_head,
            )
            record_repo_result(
                config,
                op["op_id"],
                repo_id,
                "rollback",
                outcome="rolled_back",
                new_tip=original_head,
            )
            update_repo_phase(
                config, op["op_id"], repo_id, phase="completed", outcome="rolled_back"
            )
            report.append(f"{repo_id}: rolled_back（合并后已回滚；不补成功 ledger）")
            return "rolled_back"
        update_repo_phase(config, op["op_id"], repo_id, phase="unknown", outcome="unknown")
        report.append(f"{repo_id}: unknown（committed 记录与 HEAD 不一致）")
        for command in _merge_manual_recovery_commands(
            repo_id, target_path, original_head, has_merge_head=has_merge
        ):
            report.append(f"  手动：{command}")
        return "unknown"

    if merge_outcome == "already_included" or current_outcome == "already_included":
        if (
            head == original_head
            and not has_merge
            and source_head
            and _is_ancestor(target_path, source_head, head)
        ):
            update_repo_phase(
                config,
                op["op_id"],
                repo_id,
                phase="completed",
                outcome="already_included",
            )
            report.append(f"{repo_id}: already_included（已调和）")
            return "already_included"
        update_repo_phase(config, op["op_id"], repo_id, phase="unknown", outcome="unknown")
        report.append(f"{repo_id}: unknown（already_included 核验失败或 HEAD 漂移）")
        for command in _merge_manual_recovery_commands(
            repo_id, target_path, original_head, has_merge_head=has_merge
        ):
            report.append(f"  手动：{command}")
        return "unknown"

    if merge_result is None:
        if has_merge:
            update_repo_phase(
                config, op["op_id"], repo_id, phase="unknown", outcome="unknown"
            )
            report.append(f"{repo_id}: unknown（存在 MERGE_HEAD）")
            for command in _merge_manual_recovery_commands(
                repo_id, target_path, original_head, has_merge_head=True
            ):
                report.append(f"  手动：{command}")
            return "unknown"
        if head == original_head:
            if source_head and _is_ancestor(target_path, source_head, head):
                current = load_op(config, op["op_id"])["repositories"][repo_id]
                steps_now = current.get("steps") if isinstance(current.get("steps"), dict) else {}
                merge_now = steps_now.get("merge") if isinstance(steps_now, dict) else None
                if not isinstance(merge_now, dict):
                    record_repo_intent(
                        config,
                        op["op_id"],
                        repo_id,
                        "merge",
                        original_head=original_head,
                        target_sha=source_head,
                    )
                    merge_now = {"intent": {}}
                if "result" not in merge_now:
                    record_repo_result(
                        config,
                        op["op_id"],
                        repo_id,
                        "merge",
                        outcome="already_included",
                        new_tip=original_head,
                    )
                update_repo_phase(
                    config,
                    op["op_id"],
                    repo_id,
                    phase="completed",
                    outcome="already_included",
                )
                report.append(f"{repo_id}: already_included")
                return "already_included"
            if isinstance(merge_step, dict) and "result" not in merge_step:
                record_repo_result(
                    config,
                    op["op_id"],
                    repo_id,
                    "merge",
                    outcome="not_started",
                    new_tip=original_head,
                )
            update_repo_phase(
                config, op["op_id"], repo_id, phase="completed", outcome="not_started"
            )
            report.append(
                f"{repo_id}: not_started；需要合并请重新执行 dyro line merge（新 op）"
            )
            return "not_started"
        update_repo_phase(config, op["op_id"], repo_id, phase="unknown", outcome="unknown")
        report.append(
            f"{repo_id}: unknown（无 result 且 HEAD 不等于 original_head；禁止补成功 ledger）"
        )
        for command in _merge_manual_recovery_commands(
            repo_id, target_path, original_head, has_merge_head=False
        ):
            report.append(f"  手动：{command}")
        return "unknown"

    if current_outcome == "rolled_back":
        if head == original_head and not has_merge:
            update_repo_phase(
                config, op["op_id"], repo_id, phase="completed", outcome="rolled_back"
            )
            report.append(f"{repo_id}: rolled_back（已调和）")
            return "rolled_back"
        update_repo_phase(config, op["op_id"], repo_id, phase="unknown", outcome="unknown")
        report.append(f"{repo_id}: unknown（rolled_back 但 HEAD 漂移）")
        return "unknown"

    if current_outcome == "not_started" and head == original_head and not has_merge:
        update_repo_phase(
            config, op["op_id"], repo_id, phase="completed", outcome="not_started"
        )
        report.append(f"{repo_id}: not_started")
        return "not_started"

    update_repo_phase(config, op["op_id"], repo_id, phase="unknown", outcome="unknown")
    report.append(f"{repo_id}: unknown")
    for command in _merge_manual_recovery_commands(
        repo_id, target_path, original_head, has_merge_head=has_merge
    ):
        report.append(f"  手动：{command}")
    return "unknown"


def recover_line_merge(
    config: Config,
    subject_line_id: str,
    *,
    op_id: str | None = None,
    allow_partial: bool | None = None,
    base_ref: str | None = None,
    policy_default: bool | None = None,
    repositories: Iterable[str] | None = None,
    kinds: Iterable[str] | None = None,
) -> LineMergeResult:
    """Report-only merge reconciliation: journal/ledger only, never Git mutation."""
    selected_kinds = tuple(kinds) if kinds is not None else tuple(MERGE_OP_KINDS)
    if op_id:
        op = load_op(config, op_id)
        if subject_line_id not in op["subject_line_ids"]:
            raise DyroError(f"操作 {op_id} 不属于开发线 {subject_line_id}")
        if op["kind"] not in selected_kinds:
            raise DyroError(f"操作 {op_id} 不是 merge 类操作")
    else:
        found = find_unfinished_op(config, subject_line_id, kinds=selected_kinds)
        if found is None:
            raise DyroError(f"开发线 {subject_line_id} 没有未完成的 merge 操作")
        op = found
        op_id = op["op_id"]
    supplied: dict[str, object] = {}
    if allow_partial is not None:
        supplied["allow_partial"] = allow_partial
    if base_ref is not None:
        supplied["base_ref"] = base_ref
    if policy_default is not None:
        supplied["policy_default"] = policy_default
    if repositories is not None and sorted(set(repositories)) != op["repos_ordered"]:
        raise DyroError(f"恢复选项冲突：repo；journal 冻结值为 {op['repos_ordered']!r}")
    frozen = require_compatible_options(op, supplied)
    terminal = op["phase"] in TERMINAL_PHASES
    report = [f"对账操作 {op_id}（kind={op['kind']}）"]
    outcomes: dict[str, str] = {}
    for repo_id in op["repos_ordered"]:
        repo = op["repositories"][repo_id]
        push_step = repo["steps"].get("push")
        if push_step is not None or frozen.get("push"):
            push_outcome = push_step.get("result", {}).get("outcome", "unknown") if push_step else "not_started"
            if push_step is not None and "result" not in push_step and not terminal:
                # The merge is reconciled separately; an idempotent push can be
                # left explicitly unknown and retried manually after closing.
                record_repo_result(config, op_id, repo_id, "push", outcome="unknown")
            report.append(f"{repo_id}: push {push_outcome}")
            if push_outcome != "pushed":
                report.append(
                    "  手动：重新执行 dyro line merge "
                    f"{shlex.quote(str(op['manifest_snapshot']['source']))} --into "
                    f"{shlex.quote(str(op['manifest_snapshot']['target']))} --push"
                )
                if push_step is not None:
                    path = shlex.quote(str(repo["snapshot"]["path"]))
                    branch = shlex.quote(str(push_step["intent"]["branch"]))
                    report.append(f"  手动：git -C {path} push origin {branch}")
        outcomes[repo_id] = (
            op["repositories"][repo_id]["outcome"] if terminal
            else _reconcile_merge_repo(config, op, repo_id, report=report)
        )
    op = load_op(config, op_id)
    # Stale ledger note when journal says rolled_back/unknown.
    for repo_id, outcome in outcomes.items():
        if outcome in {"rolled_back", "unknown", "not_started"} and _ledger_has_op_repo(
            config, op_id=op_id, repository=repo_id
        ):
            report.append(f"{repo_id}: ledger 陈旧（存在 op_id 行但 journal 非成功终态）")
    success_outcomes = {"committed", "already_included"}
    if all(outcome in success_outcomes for outcome in outcomes.values()):
        manifest = op.get("manifest_snapshot") if isinstance(op.get("manifest_snapshot"), dict) else {}
        if op["kind"] == "merge":
            target = get_line(config, str(manifest.get("target") or subject_line_id))
            branches = {repo: target.branch for repo in op["repos_ordered"]}
            ledger_subject = str(manifest.get("source") or subject_line_id)
            extra = {
                "target": str(manifest.get("target") or ""),
                "source": str(manifest.get("source") or ""),
            }
            event_actor = str(manifest.get("source") or subject_line_id)
            event_subject = str(manifest.get("target") or subject_line_id)
            event_family = event_subject
            if frozen.get("integration") == "into":
                extra = {"parent": event_subject, "child": event_actor}
        else:
            branches = frozen["base_refs"]
            ledger_subject = subject_line_id
            extra = {"target": "base", "source": subject_line_id}
            event_actor = subject_line_id
            event_subject = subject_line_id
            event_family = subject_line_id
        _ledger_successful_merge_repos(
            config,
            op=load_op(config, op_id),
            subject_id=ledger_subject,
            branches=branches,
            extra=extra,
        )
        if not terminal:
            finish_op(config, op_id, phase="completed")
        facts = op.get("event_facts") if isinstance(op.get("event_facts"), dict) else {}
        if "op_id" not in facts:
            facts = {
                "op_id": op_id,
                "repo_count": len(op["repos_ordered"]),
                "repos_digest": _repos_digest(op["repos_ordered"]),
                "partial": bool(frozen.get("allow_partial") and False)
                or bool(facts.get("partial")),
            }
        event = manifest.get("event") or {
            "kind": "merge" if frozen.get("integration") == "into" else "merge_cross",
            "actor": event_actor, "subject": event_subject, "family": event_family,
        }
        _append_merge_event_if_needed(
            config,
            op_id=op_id,
            **event,
            facts=facts,
        )
        report.append(f"操作已 {op['phase'] if terminal else 'completed'}")
    elif all(outcome in _MERGE_KNOWN_OUTCOMES for outcome in outcomes.values()) and not any(
        outcome in success_outcomes for outcome in outcomes.values()
    ):
        if not terminal:
            finish_op(config, op_id, phase="aborted")
        report.append(
            "操作已 aborted；需要合并请重新执行 dyro line merge（新 op）"
        )
    elif all(outcome in _MERGE_KNOWN_OUTCOMES for outcome in outcomes.values()):
        # Mix of success and rolled_back/not_started after in-process failure.
        if any(outcome in success_outcomes for outcome in outcomes.values()) and any(
            outcome in {"rolled_back", "not_started"} for outcome in outcomes.values()
        ):
            if not terminal:
                finish_op(config, op_id, phase="aborted")
            report.append("操作已 aborted（存在回滚/未开始仓）")
        else:
            if not terminal:
                finish_op(config, op_id, phase="completed")
            report.append("操作已 completed")
    else:
        report.append("仍有 unknown 仓；操作保持打开并继续拦截")
    for line in report:
        print(line)
    manifest = op.get("manifest_snapshot") if isinstance(op.get("manifest_snapshot"), dict) else {}
    return LineMergeResult(
        op_id=op_id,
        target_id=str(manifest.get("target") or subject_line_id),
        source_id=str(manifest.get("source") or subject_line_id),
        repositories=tuple(op["repos_ordered"]),
        excluded_repositories=(),
        partial=bool(isinstance(op.get("event_facts"), dict) and op["event_facts"].get("partial")),
        outcomes=outcomes,
    )


def _line_status_scope(line: Line) -> str:
    scope = f"{line.kind}:{line.id}"
    if line.parent:
        return f"{scope} ({line.parent})"
    return scope


def _short_status(
    path: Path, *, read_budget: ReadBudget | None = None
) -> tuple[str, str, str, int]:
    branch = (
        require_ok(
            git_read(
                path,
                "branch",
                "--show-current",
                read_budget=read_budget,
            ),
            f"读取 {path} 分支",
        ).stdout.strip()
        or "DETACHED"
    )
    head = require_ok(
        git_read(
            path,
            "rev-parse",
            "--short=12",
            "HEAD",
            read_budget=read_budget,
        ),
        f"读取 {path} HEAD",
    ).stdout.strip()
    upstream_result = git_read(
        path,
        "rev-parse",
        "--abbrev-ref",
        "--symbolic-full-name",
        "@{upstream}",
        read_budget=read_budget,
    )
    upstream = upstream_result.stdout.strip() if upstream_result.code == 0 else "-"
    dirty = len(
        require_ok(
            git_read(
                path,
                "status",
                "--porcelain=v1",
                "-uall",
                read_budget=read_budget,
            ),
            f"读取 {path} 状态",
        ).stdout.splitlines()
    )
    return branch, head, upstream, dirty


def git_observation_scope_count(
    config: Config, *, read_budget: ReadBudget | None = None
) -> int:
    """Count anchors plus each line/hotfix worktree from manifests (no git)."""

    count = len(config.repositories)
    for line in list_lines(config, read_budget=read_budget):
        count += len(line.repositories)
    return max(count, 1)


def is_observation_deadline_finding(finding: str) -> bool:
    return finding == OBSERVATION_DEADLINE_FINDING


def observation_timeout_row() -> ObservationStatusRow:
    return (
        OBSERVATION_TIMEOUT_SCOPE,
        "-",
        OBSERVATION_TIMEOUT_BRANCH,
        "-",
        "-",
        -1,
    )


def is_observation_timeout_row(row: ObservationStatusRow) -> bool:
    return (
        row[0] == OBSERVATION_TIMEOUT_SCOPE
        and row[2] == OBSERVATION_TIMEOUT_BRANCH
    )


def stash_observation_findings(
    read_budget: ReadBudget | None, findings: list[str]
) -> None:
    if read_budget is None:
        return
    setattr(read_budget, _STASHED_FINDINGS, list(findings))


def stashed_observation_findings(read_budget: ReadBudget | None) -> list[str]:
    if read_budget is None:
        return []
    raw = getattr(read_budget, _STASHED_FINDINGS, None)
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, str)]


def stash_observation_rows(
    read_budget: ReadBudget | None, rows: list[ObservationStatusRow]
) -> None:
    if read_budget is None:
        return
    setattr(read_budget, _STASHED_ROWS, list(rows))


def stashed_observation_rows(
    read_budget: ReadBudget | None,
) -> list[ObservationStatusRow]:
    if read_budget is None:
        return []
    raw = getattr(read_budget, _STASHED_ROWS, None)
    if not isinstance(raw, list):
        return []
    kept: list[ObservationStatusRow] = []
    for item in raw:
        if (
            isinstance(item, tuple)
            and len(item) == 6
            and all(isinstance(part, str) for part in item[:5])
            and isinstance(item[5], int)
            and not isinstance(item[5], bool)
        ):
            kept.append(item)
    return kept


def _scale_control_plane_budget(
    config: Config, read_budget: ReadBudget | None
) -> None:
    if read_budget is None or read_budget.limits.deadline_seconds != 5.0:
        return
    apply_control_plane_fanout(
        read_budget,
        git_observation_scope_count(config, read_budget=read_budget),
    )


def status_rows(
    config: Config, *, read_budget: ReadBudget | None = None
) -> list[tuple[str, str, str, str, str, int]]:
    rows: list[tuple[str, str, str, str, str, int]] = []
    try:
        _scale_control_plane_budget(config, read_budget)
        for repo_id in sorted(config.repositories):
            path = repository_path(config, repo_id)
            if _is_git_repo(path, read_budget=read_budget):
                branch, head, upstream, dirty = _short_status(
                    path, read_budget=read_budget
                )
                rows.append(("anchor", repo_id, branch, head, upstream, dirty))
            else:
                rows.append(("anchor", repo_id, "MISSING", "-", "-", -1))
        lines = list_lines(config, read_budget=read_budget)
        if read_budget is not None:
            read_budget._status_lines = lines
        for line in lines:
            for repo_id in line.repositories:
                path = line_repository_path(config, line, repo_id)
                if _is_git_repo(path, read_budget=read_budget):
                    branch, head, upstream, dirty = _short_status(
                        path, read_budget=read_budget
                    )
                    rows.append(
                        (
                            _line_status_scope(line),
                            repo_id,
                            branch,
                            head,
                            upstream,
                            dirty,
                        )
                    )
                else:
                    rows.append(
                        (
                            _line_status_scope(line),
                            repo_id,
                            "MISSING",
                            "-",
                            "-",
                            -1,
                        )
                    )
    except ReadLimitError as exc:
        if read_budget is None or exc.code is not ReadLimitCode.DEADLINE_EXCEEDED:
            stash_observation_rows(read_budget, rows)
            raise
        if not any(is_observation_timeout_row(row) for row in rows):
            rows.append(observation_timeout_row())
    stash_observation_rows(read_budget, rows)
    return rows


def _worktree_inventory(
    anchor: Path, *, read_budget: ReadBudget | None = None
) -> dict[Path, tuple[str, str]]:
    """Map每个与 anchor 共用 Git 公共目录的 worktree 到 (HEAD, 分支)。

    ``git worktree list`` 只报告同一个仓库的 worktree，所以"出现在这份清单里"
    本身就是共用公共目录的证据；这取代了过去对每个 worktree 各发一对
    ``rev-parse --git-common-dir`` 的做法。
    """
    result = git_read(
        anchor, "worktree", "list", "--porcelain", read_budget=read_budget
    )
    if result.code != 0:
        return {}
    inventory: dict[Path, tuple[str, str]] = {}
    path: Path | None = None
    head = ""
    branch = ""
    prunable = False

    def flush() -> None:
        nonlocal path, head, branch, prunable
        if path is not None and not prunable:
            inventory[path] = (head, branch)
        path, head, branch, prunable = None, "", "", False

    for raw in result.stdout.splitlines():
        entry = raw.strip()
        if not entry:
            flush()
            continue
        key, _, value = entry.partition(" ")
        if key == "worktree":
            flush()
            path = Path(value).resolve()
        elif key == "HEAD":
            head = value.strip()
        elif key == "branch":
            branch = value.strip()
            if branch.startswith("refs/heads/"):
                branch = branch[len("refs/heads/") :]
        elif key == "prunable":
            prunable = True
    flush()
    return inventory


def _ref_inventory(
    anchor: Path, *, read_budget: ReadBudget | None = None
) -> tuple[dict[str, str], dict[str, str]]:
    """返回 (本地分支 → 上游, 引用全名 → 对象 ID)。

    引用存放在共用的公共目录里，所以从 anchor 读到的就是每个 linked worktree
    会读到的同一份；一条 ``for-each-ref`` 取代了逐开发线的 ``show-ref``、
    ``rev-parse <origin/branch>`` 与 ``rev-parse @{upstream}``。
    """
    upstreams: dict[str, str] = {}
    objects: dict[str, str] = {}
    result = git_read(
        anchor,
        "for-each-ref",
        "--format=%(refname)%09%(objectname)%09%(upstream)",
        "refs/heads",
        "refs/remotes/origin",
        read_budget=read_budget,
    )
    if result.code != 0:
        return upstreams, objects
    for raw in result.stdout.splitlines():
        parts = raw.split("\t")
        if len(parts) != 3:
            continue
        refname, objectname, upstream = parts
        objects[refname] = objectname
        if refname.startswith("refs/heads/"):
            upstreams[refname[len("refs/heads/") :]] = _normalize_upstream(upstream)
    return upstreams, objects


def doctor(config: Config, *, read_budget: ReadBudget | None = None) -> list[str]:
    """Return diagnostics.  Callers decide whether any FAIL means non-zero."""
    findings: list[str] = []
    lines = None
    observed = {}
    try:
        from .host.doctor import integration_avatar_warnings

        findings.extend(integration_avatar_warnings(read_budget=read_budget))
        for requirement in external_security_errors(config.policy):
            findings.append(f"FAIL external Profile requires {requirement}")
        root_git = _is_git_repo(config.root, read_budget=read_budget)
        findings.append(("WARN" if root_git else "PASS") + " workspace root " + ("is a Git repository" if root_git else "is not a Git repository"))
        from .instructions import overlay_instruction_warning

        overlay_warning = overlay_instruction_warning(config.root)
        if overlay_warning:
            findings.append(overlay_warning)
        findings.extend(overlay_template_warnings(config, read_budget=read_budget))
        overlay_budget = read_budget or ReadBudget(ObservationLimits())
        line_overlays = _read_line_overlays(config, overlay_budget)
        _scale_control_plane_budget(config, read_budget)
        for repo_id in sorted(config.repositories):
            anchor = repository_path(config, repo_id)
            if _is_git_repo(anchor, read_budget=read_budget):
                findings.append(f"PASS repository {repo_id}: {anchor}")
            else:
                findings.append(f"FAIL repository {repo_id}: missing or not Git: {anchor}")
        lines = list_lines(config, read_budget=read_budget)
        from .instructions import render_line_persona
        for line in lines:
            findings.extend(_overlay_warnings(line_root(config, line), render_line_persona(line_id=line.id, branch=line.branch), f'line {line.id}', overlay_budget, contents=line_overlays))
        for repo_id in sorted(
            {repo for line in lines for repo in line.repositories}
            & set(config.repositories)
        ):
            anchor = repository_path(config, repo_id)
            worktrees = _worktree_inventory(anchor, read_budget=read_budget)
            upstreams, objects = _ref_inventory(anchor, read_budget=read_budget)
            observed[repo_id] = (worktrees, upstreams, objects)
        for line in lines:
            for repo_id in line.repositories:
                anchor = repository_path(config, repo_id)
                worktree = line_repository_path(config, line, repo_id)
                storage_mode = line.storage_for(repo_id)
                worktrees, upstreams, objects = observed.get(repo_id, ({}, {}, {}))
                entry = worktrees.get(worktree.resolve())
                if entry is None and not _is_git_repo(worktree, read_budget=read_budget):
                    findings.append(f"FAIL {line.kind}:{line.id}/{repo_id}: missing worktree")
                    continue
                if entry is not None:
                    head, actual_branch = entry
                else:
                    # A Git repository the anchor does not own: read its branch
                    # directly so a foreign checkout still reports as before.
                    probe = git_read(
                        worktree,
                        "branch",
                        "--show-current",
                        read_budget=read_budget,
                    )
                    head = ""
                    actual_branch = (
                        probe.stdout.strip() if probe.code == 0 else "UNREADABLE"
                    )
                if actual_branch != line.branch:
                    findings.append(f"FAIL {line.kind}:{line.id}/{repo_id}: expected {line.branch}, found {actual_branch or 'DETACHED'}")
                    continue
                if storage_mode == "anchor-reference":
                    if not worktree.is_symlink():
                        findings.append(f"FAIL {line.kind}:{line.id}/{repo_id}: expected anchor-reference symlink")
                    elif worktree.resolve() != anchor.resolve():
                        findings.append(f"FAIL {line.kind}:{line.id}/{repo_id}: symlink does not target configured anchor")
                    else:
                        findings.append(f"PASS {line.kind}:{line.id}/{repo_id}: references configured anchor")
                    continue
                if worktree.is_symlink():
                    findings.append(f"FAIL {line.kind}:{line.id}/{repo_id}: linked-worktree cannot be a symlink")
                    continue
                if entry is None:
                    findings.append(f"FAIL {line.kind}:{line.id}/{repo_id}: unexpected Git common-dir")
                    continue
                expected_remote = _expected_remote_branch(line.branch)
                remote_head = objects.get(f"refs/remotes/{expected_remote}")
                if remote_head is None:
                    findings.append(
                        f"WARN {line.kind}:{line.id}/{repo_id}: missing {expected_remote}"
                    )
                    continue
                upstream = upstreams.get(line.branch, "")
                if upstream == expected_remote or (
                    not upstream and head and head == remote_head
                ):
                    findings.append(
                        f"PASS {line.kind}:{line.id}/{repo_id}: linked to configured anchor"
                    )
                else:
                    findings.append(
                        f"FAIL {line.kind}:{line.id}/{repo_id}: "
                        f"expected upstream {expected_remote}, found {upstream or '-'}"
                    )
    except ReadLimitError as exc:
        stash_doctor_snapshot(read_budget, lines, observed, False)
        if read_budget is None or exc.code is not ReadLimitCode.DEADLINE_EXCEEDED:
            stash_observation_findings(read_budget, findings)
            raise
        if not any(is_observation_deadline_finding(item) for item in findings):
            findings.append(OBSERVATION_DEADLINE_FINDING)
    stash_doctor_snapshot(read_budget, lines, observed, not any(is_observation_deadline_finding(x) for x in findings))
    stash_observation_findings(read_budget, findings)
    return findings


_SAFE_FINDING_ID = r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}"
_MISSING_ORIGIN_FINDING = re.compile(
    rf"^(?:FAIL|WARN) (?:line|hotfix):{_SAFE_FINDING_ID}/{_SAFE_FINDING_ID}: missing origin/\S+$"
)
_LINE_OR_HOTFIX_FAIL = re.compile(
    rf"^FAIL (?:line|hotfix):({_SAFE_FINDING_ID})/"
)
_REPOSITORY_FAIL = re.compile(rf"^FAIL repository ({_SAFE_FINDING_ID}):")


def is_missing_origin_finding(finding: str) -> bool:
    """True only for doctor findings that mean origin/<line.branch> is absent.

    Matches the constructed shape
    ``WARN|FAIL <kind>:<id>/<repo>: missing origin/<branch>`` and nothing else.
    A path or message that merely embeds ``: missing origin/`` does not match.

    Dyro does not push, so an unpublished line branch is advisory. Join
    completion, setup post-doctor, home create-and-open, and
    ``existing_line_workspace`` / ``dyro open`` skip this constructed
    shape so SHA-pinned / local-only lines can exist (and be opened)
    before the remote-tracking ref is published. Workspace-level
    ``FAIL external Profile requires …``, the target line's own FAIL,
    and repository / observation-deadline FAILs still block open.
    ``dyro next`` and ``dyro start`` only stop on FAIL, so a
    missing-origin WARN is ready. Other lines' ``FAIL line:`` /
    ``FAIL hotfix:`` findings are filtered at those callers (§4).
    """
    return _MISSING_ORIGIN_FINDING.fullmatch(finding) is not None


def is_blocking_line_doctor_finding(finding: str, line: Line) -> bool:
    """True when a doctor FAIL should block open/start for ``line``.

    Filtering is caller-side: ``doctor`` still scans every line. Other
    lines' ``FAIL line:`` / ``FAIL hotfix:`` findings may be ignored.
    Still blocks ``FAIL external…``, the target line's own prefix FAIL,
    workspace-root FAIL, ``FAIL repository`` for repos the target line
    uses, observation-deadline FAIL, and any unrecognized FAIL shape.
    Missing-origin remains a separate skip via
    ``is_missing_origin_finding``.
    """
    if not finding.startswith("FAIL"):
        return False
    if is_observation_deadline_finding(finding):
        return True
    if finding.startswith("FAIL external"):
        return True
    if "workspace root" in finding:
        return True
    line_match = _LINE_OR_HOTFIX_FAIL.match(finding)
    if line_match is not None:
        return line_match.group(1) == line.id
    repo_match = _REPOSITORY_FAIL.match(finding)
    if repo_match is not None:
        return repo_match.group(1) in line.repositories
    return True


def blocking_line_doctor_failures(
    config: Config,
    line: Line,
    *,
    findings: list[str] | None = None,
) -> list[str]:
    """FAIL findings that block open/start for ``line`` (missing-origin skipped)."""
    items = doctor(config) if findings is None else findings
    return [
        item
        for item in items
        if is_blocking_line_doctor_finding(item, line)
        and not is_missing_origin_finding(item)
    ]

def stash_doctor_snapshot(budget, lines, observed, complete):
    if budget is not None:
        budget._doctor_lines = lines
        budget._doctor_observed = dict(observed)
        budget._doctor_complete = complete


def overlay_template_warnings(config, *, read_budget=None):
    from .instructions import render_workspace_overlay
    budget = read_budget or ReadBudget(ObservationLimits())
    return _overlay_warnings(config.root, render_workspace_overlay(), 'workspace', budget)


def _read_line_overlays(config, budget):
    """Read overlay bytes before manifests; template comparison needs their branches."""
    from .instructions import INSTRUCTION_NAMES
    contents = {}
    for relative in (config.layout.lines, config.layout.hotfixes):
        parent = config.root / relative
        with budget.open_safe_directory_chain(config.root, parent, allow_missing=True) as fd:
            if fd is None:
                continue
            names = bounded_directory_names(fd, budget, maximum_records=budget.limits.line_records, label='Line overlay')
        for name in sorted(names):
            for instruction in INSTRUCTION_NAMES:
                path = parent / name / instruction
                if not path.exists() and not path.is_symlink():
                    continue
                try:
                    contents[path] = budget.read_regular_bytes(path, maximum_bytes=budget.limits.line_manifest_bytes, label='overlay')
                    contents[path].decode('utf-8')
                except ReadLimitError as exc:
                    if exc.code not in (ReadLimitCode.FILE_TOO_LARGE, ReadLimitCode.UNSAFE_FILE):
                        raise
                    contents[path] = exc
                except (OSError, UnicodeError) as exc:
                    contents[path] = exc
    return contents


def _overlay_warnings(directory, body, label, budget, *, contents=None):
    from .instructions import INSTRUCTION_NAMES
    findings = []
    expected = (body if body.endswith('\n') else body + '\n').encode('utf-8')
    for name in INSTRUCTION_NAMES:
        path = directory / name
        if not path.exists() and not path.is_symlink():
            continue
        prefix = f'WARN overlay {label} {name}'
        try:
            if contents is None:
                content = budget.read_regular_bytes(path, maximum_bytes=budget.limits.line_manifest_bytes, label='overlay')
            else:
                if path not in contents:
                    continue
                content = contents[path]
                if isinstance(content, Exception):
                    raise content
            content.decode('utf-8')
        except ReadLimitError as exc:
            if exc.code not in (ReadLimitCode.FILE_TOO_LARGE, ReadLimitCode.UNSAFE_FILE):
                raise
            findings.append(prefix + ' 无法核对；运行 dyro host seed --force')
        except (OSError, UnicodeError):
            findings.append(prefix + ' 无法核对；运行 dyro host seed --force')
        else:
            if content != expected:
                findings.append(prefix + ' 与当前模板不一致；运行 dyro host seed --force')
    return findings


def next_line_context(config, line, lines, budget):
    observed = getattr(budget, '_doctor_observed', {})
    repositories = []
    states = []
    calls = 0
    limited = None
    for repo_id in line.repositories:
        storage = line.storage_for(repo_id)
        expected = f'origin/{line.branch}'
        trees, upstreams, objects = observed.get(repo_id, ({}, {}, {}))
        head = trees.get(line_repository_path(config, line, repo_id).resolve(), ('', ''))[0]
        remote = objects.get(f'refs/remotes/{expected}')
        upstream = upstreams.get(line.branch, '')
        state = ('not_applicable' if storage == 'anchor-reference' else
                 'unknown' if repo_id not in observed else
                 'missing' if remote is None else
                 'unknown' if not head or not remote else
                 'ok' if upstream == expected else
                 'mismatch' if upstream else
                 'same_sha_unconfigured' if head == remote else 'unconfigured')
        item = dict(id=repo_id, storage=storage, expected_upstream=expected,
                    upstream=upstream, upstream_state=state, ahead=None, behind=None,
                    unpushed_commits=None)
        if storage == 'linked-worktree':
            states.append(state)
            if head and remote and limited is None:
                if head == remote:
                    item.update(ahead=0, behind=0, unpushed_commits=0)
                elif calls >= 4:
                    item['counts_omitted'] = True
                elif budget is not None:
                    try:
                        if budget.remaining_seconds() >= 1:
                            calls += 1
                            result = git_read(repository_path(config, repo_id), 'rev-list', '--left-right', '--count', f'{remote}...{head}', read_budget=budget)
                            if result.code == 0:
                                behind, ahead = map(int, result.stdout.split())
                                item.update(ahead=ahead, behind=behind, unpushed_commits=ahead)
                    except ReadLimitError as exc:
                        limited = exc.code
                    except ValueError:
                        pass
        repositories.append(item)
    ready = None if not states or 'unknown' in states or not getattr(budget, '_doctor_complete', False) else all(x in ('ok', 'same_sha_unconfigured') for x in states)
    return dict(id=line.id, kind=line.kind, branch=line.branch, parent=line.parent,
                children=[x.id for x in lines if x.parent == line.id],
                push_ready=ready, repositories=repositories), limited

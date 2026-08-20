from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import shutil
import stat
from typing import Iterable, Mapping

from .config import Config, external_security_errors, validate_id
from .errors import DyroError, ValidationError
from .process import git, git_read, require_ok
from .read_limits import (
    ReadBudget,
    ReadLimitCode,
    ReadLimitError,
    bounded_directory_names,
)
from .state import atomic_write_text, exclusive_lock


STORAGE_MODES = frozenset({"linked-worktree", "anchor-reference"})
LINE_MANIFEST_SCHEMAS = frozenset({1, 2, 3})
MERGE_LOCK_TIMEOUT_SECONDS = 1800.0


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
    schema_version = 3 if line.parent else 2
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
    parent = _parse_parent_field(raw.get("parent", ""), line_id, path)
    return Line(
        line_id, kind, branch, base, repositories, repository_bases, storage_modes, parent
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


def _remove_line_worktree(config: Config, line: Line, repo_id: str, destination: Path) -> str | None:
    """Best-effort cleanup for one worktree or anchor-reference created during line setup."""
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
                        return f"{repo_id}: worktree remove failed: {result.stdout.strip() or 'unknown error'}"
            return None
    except OSError as exc:
        return f"{repo_id}: {exc}"
    return None


def _rollback_line_creations(config: Config, line: Line, created: list[str]) -> list[str]:
    failures: list[str] = []
    for repo_id in reversed(created):
        destination = line_repository_path(config, line, repo_id)
        failure = _remove_line_worktree(config, line, repo_id, destination)
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


def _plan_line_creation(
    config: Config, line: Line
) -> list[tuple[str, Path, tuple[str, ...]]]:
    """Validate every Git-side prerequisite without creating a worktree."""
    target_root = line_root(config, line)
    if target_root.exists() and any(target_root.iterdir()):
        raise DyroError(f"目标工作区已存在且非空：{target_root}")

    # Validate every repository before mutating any worktree so partial creates are rare.
    planned: list[tuple[str, Path, tuple[str, ...]]] = []
    for repo_id in line.repositories:
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
            planned.append((repo_id, destination, ()))
            continue
        command = _linked_worktree_command(
            anchor, repo_id, line, repo_base, destination
        )
        planned.append((repo_id, destination, command))
    return planned


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
    planned = _plan_line_creation(config, line)

    created: list[str] = []
    try:
        for repo_id, destination, command in planned:
            if line.storage_for(repo_id) == "anchor-reference":
                if not dry_run:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.symlink_to(repository_path(config, repo_id), target_is_directory=True)
                    created.append(repo_id)
                continue
            if not dry_run:
                destination.parent.mkdir(parents=True, exist_ok=True)
            require_ok(
                git(repository_path(config, repo_id), *command, dry_run=dry_run, timeout=300),
                f"创建 {repo_id} worktree",
            )
            if not dry_run:
                created.append(repo_id)
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
    anchor = repository_path(config, repo_id)
    remote = _expected_remote_branch(parent.branch)
    if _ref_exists(anchor, f"refs/remotes/{remote}"):
        return remote
    if _rev_parse(anchor, parent.branch):
        return parent.branch
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
    line = create_line(
        config,
        line_id=child_id,
        branch=f"feat/{child_id}",
        base=default_base,
        repositories=selected,
        repository_bases=repository_bases,
        storage_modes=storage_modes,
        kind="line",
        parent=parent.id,
        dry_run=dry_run,
    )
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
                    dry_run=dry_run,
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
            result = git(
                plan.target,
                "merge",
                "--no-ff",
                "--no-commit",
                plan.source_head,
                timeout=300,
            )
            probed.append(plan)
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
    _require_line_worktrees(config, parent, clean=True, label="父开发线")
    _require_line_worktrees(config, child, clean=False, label="子开发线")
    blocking = _child_blocking_doctor_findings(config, child)
    if blocking:
        raise DyroError(f"子线 {child.id} 未通过 doctor，拒绝合并：{blocking[0]}")
    _merge_line_repositories(
        config,
        target=parent,
        source=child,
        repositories=child.repositories,
        lock_line_id=parent.id,
        message=f"merge(line): {child.id} -> {parent.id}",
        phase="line_merge",
        push=push,
        dry_run=dry_run,
        extra_ledger={"parent": parent.id, "child": child.id},
    )
    if not dry_run:
        from .events import append_event

        append_event(
            config,
            kind="merge",
            actor=child.id,
            subject=parent.id,
            family=parent.id,
            facts={"parent": parent.id, "child": child.id},
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


def status_rows(
    config: Config, *, read_budget: ReadBudget | None = None
) -> list[tuple[str, str, str, str, str, int]]:
    rows: list[tuple[str, str, str, str, str, int]] = []
    for repo_id in sorted(config.repositories):
        path = repository_path(config, repo_id)
        if _is_git_repo(path, read_budget=read_budget):
            branch, head, upstream, dirty = _short_status(
                path, read_budget=read_budget
            )
            rows.append(("anchor", repo_id, branch, head, upstream, dirty))
        else:
            rows.append(("anchor", repo_id, "MISSING", "-", "-", -1))
    for line in list_lines(config, read_budget=read_budget):
        for repo_id in line.repositories:
            path = line_repository_path(config, line, repo_id)
            if _is_git_repo(path, read_budget=read_budget):
                branch, head, upstream, dirty = _short_status(
                    path, read_budget=read_budget
                )
                rows.append((_line_status_scope(line), repo_id, branch, head, upstream, dirty))
            else:
                rows.append((_line_status_scope(line), repo_id, "MISSING", "-", "-", -1))
    return rows


def doctor(config: Config, *, read_budget: ReadBudget | None = None) -> list[str]:
    """Return diagnostics.  Callers decide whether any FAIL means non-zero."""
    findings: list[str] = []
    for requirement in external_security_errors(config.policy):
        findings.append(f"FAIL external Profile requires {requirement}")
    root_git = _is_git_repo(config.root, read_budget=read_budget)
    findings.append(("WARN" if root_git else "PASS") + " workspace root " + ("is a Git repository" if root_git else "is not a Git repository"))
    from .instructions import overlay_instruction_warning

    overlay_warning = overlay_instruction_warning(config.root)
    if overlay_warning:
        findings.append(overlay_warning)
    for repo_id in sorted(config.repositories):
        anchor = repository_path(config, repo_id)
        if _is_git_repo(anchor, read_budget=read_budget):
            findings.append(f"PASS repository {repo_id}: {anchor}")
        else:
            findings.append(f"FAIL repository {repo_id}: missing or not Git: {anchor}")
    for line in list_lines(config, read_budget=read_budget):
        for repo_id in line.repositories:
            anchor = repository_path(config, repo_id)
            worktree = line_repository_path(config, line, repo_id)
            storage_mode = line.storage_for(repo_id)
            if not _is_git_repo(worktree, read_budget=read_budget):
                findings.append(f"FAIL {line.kind}:{line.id}/{repo_id}: missing worktree")
                continue
            actual_branch = git_read(
                worktree,
                "branch",
                "--show-current",
                read_budget=read_budget,
            )
            if actual_branch.code != 0 or actual_branch.stdout.strip() != line.branch:
                actual = actual_branch.stdout.strip() if actual_branch.code == 0 else "UNREADABLE"
                findings.append(f"FAIL {line.kind}:{line.id}/{repo_id}: expected {line.branch}, found {actual or 'DETACHED'}")
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
            anchor_common = git_read(
                anchor,
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
                read_budget=read_budget,
            )
            worktree_common = git_read(
                worktree,
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
                read_budget=read_budget,
            )
            if not (
                anchor_common.code == 0
                and worktree_common.code == 0
                and anchor_common.stdout.strip() == worktree_common.stdout.strip()
            ):
                findings.append(f"FAIL {line.kind}:{line.id}/{repo_id}: unexpected Git common-dir")
                continue
            expected_remote = _expected_remote_branch(line.branch)
            if not _ref_exists(
                worktree, f"refs/remotes/{expected_remote}", read_budget=read_budget
            ):
                findings.append(
                    f"FAIL {line.kind}:{line.id}/{repo_id}: missing {expected_remote}"
                )
                continue
            upstream = _branch_upstream(worktree, read_budget=read_budget)
            head = _rev_parse(worktree, "HEAD", read_budget=read_budget)
            remote_head = _rev_parse(worktree, expected_remote, read_budget=read_budget)
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
    return findings


_MISSING_ORIGIN_TOKEN = ": missing origin/"


def is_missing_origin_finding(finding: str) -> bool:
    """True only for doctor FAILs that mean origin/<line.branch> is absent.

    Join completion, setup post-doctor, start, next, and home-open skip these
    so SHA-pinned / local-only lines can exist before the remote-tracking ref
    is published. Wrong upstream, wrong branch, missing worktree, common-dir,
    and symlink FAILs still fail.
    """
    return finding.startswith("FAIL ") and _MISSING_ORIGIN_TOKEN in finding

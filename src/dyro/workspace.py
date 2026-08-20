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
from .state import atomic_write_text


STORAGE_MODES = frozenset({"linked-worktree", "anchor-reference"})


@dataclass(frozen=True)
class Line:
    id: str
    kind: str
    branch: str
    base: str
    repositories: tuple[str, ...]
    repository_bases: Mapping[str, str] = field(default_factory=dict)
    storage_modes: Mapping[str, str] = field(default_factory=dict)

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
    chunks = [
        "schema_version = 2",
        f"id = {_toml_string(line.id)}",
        f"kind = {_toml_string(line.kind)}",
        f"branch = {_toml_string(line.branch)}",
        f"base = {_toml_string(line.base)}",
        f"repositories = [{repo_items}]",
    ]
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
    if raw.get("schema_version") not in (1, 2):
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
    return Line(line_id, kind, branch, base, repositories, repository_bases, storage_modes)


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
    return Line(line_id, kind, branch, base, selected, base_overrides, storage_overrides)


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
    return line


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
                rows.append((f"{line.kind}:{line.id}", repo_id, branch, head, upstream, dirty))
            else:
                rows.append((f"{line.kind}:{line.id}", repo_id, "MISSING", "-", "-", -1))
    return rows


def doctor(config: Config, *, read_budget: ReadBudget | None = None) -> list[str]:
    """Return diagnostics.  Callers decide whether any FAIL means non-zero."""
    findings: list[str] = []
    for requirement in external_security_errors(config.policy):
        findings.append(f"FAIL external Profile requires {requirement}")
    root_git = _is_git_repo(config.root, read_budget=read_budget)
    findings.append(("WARN" if root_git else "PASS") + " workspace root " + ("is a Git repository" if root_git else "is not a Git repository"))
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

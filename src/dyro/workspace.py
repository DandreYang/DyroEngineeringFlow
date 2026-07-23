from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Iterable, Mapping

from .config import Config, validate_id
from .errors import DyroError, ValidationError
from .process import git, require_ok
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


def _parse_line(path: Path) -> Line:
    import tomllib

    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
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


def list_lines(config: Config, kind: str | None = None) -> list[Line]:
    wanted = (kind,) if kind else ("line", "hotfix")
    lines: list[Line] = []
    for current_kind in wanted:
        parent = config.lines_state_dir if current_kind == "line" else config.hotfixes_state_dir
        if parent.exists():
            lines.extend(_parse_line(path) for path in sorted(parent.glob("*.toml")))
    return sorted(lines, key=lambda line: (line.kind, line.id))


def get_line(config: Config, line_id: str, kind: str | None = None) -> Line:
    matches = [line for line in list_lines(config, kind) if line.id == line_id]
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


def _is_git_repo(path: Path) -> bool:
    return git(path, "rev-parse", "--git-dir").code == 0


def _ensure_clean(path: Path) -> None:
    result = require_ok(git(path, "status", "--porcelain=v1", "-uall"), f"读取 {path} 状态")
    if result.stdout.strip():
        raise DyroError(f"仓库不干净，拒绝创建或合并 worktree：{path}")


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
    line = Line(line_id, kind, branch, base, selected, base_overrides, storage_overrides)
    target_root = line_root(config, line)
    if target_root.exists() and any(target_root.iterdir()):
        raise DyroError(f"目标工作区已存在且非空：{target_root}")

    for repo_id in selected:
        anchor = repository_path(config, repo_id)
        destination = line_repository_path(config, line, repo_id)
        if not _is_git_repo(anchor):
            raise DyroError(f"仓库 anchor 不存在或不是 Git 仓库：{anchor}")
        _ensure_clean(anchor)
        repo_base = line.base_for(repo_id)
        require_ok(git(anchor, "rev-parse", "--verify", f"{repo_base}^{{commit}}"), f"校验 {repo_id} 基线 {repo_base}")
        if destination.exists() or destination.is_symlink():
            raise DyroError(f"worktree 目标已存在：{destination}")
        branch_check = git(anchor, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}")
        if branch_check.code == 0:
            ancestry = git(anchor, "merge-base", "--is-ancestor", repo_base, branch)
            if ancestry.code != 0:
                raise DyroError(f"{repo_id} 既有分支 {branch} 不包含声明的基线 {repo_base}")
        if line.storage_for(repo_id) == "anchor-reference":
            anchor_branch = require_ok(git(anchor, "branch", "--show-current"), f"读取 {repo_id} anchor 分支").stdout.strip()
            if anchor_branch != branch:
                raise DyroError(f"{repo_id} 的 anchor-reference 要求 anchor 正位于 {branch}，当前为 {anchor_branch or 'DETACHED'}")
            if not dry_run:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.symlink_to(anchor, target_is_directory=True)
            continue
        command = ("worktree", "add")
        if branch_check.code != 0:
            command += ("-b", branch)
        command += (str(destination), branch if branch_check.code == 0 else repo_base)
        if not dry_run:
            destination.parent.mkdir(parents=True, exist_ok=True)
        require_ok(git(anchor, *command, dry_run=dry_run, timeout=300), f"创建 {repo_id} worktree")
    _write_line(config, line, dry_run=dry_run)
    return line


def _short_status(path: Path) -> tuple[str, str, str, int]:
    branch = require_ok(git(path, "branch", "--show-current"), f"读取 {path} 分支").stdout.strip() or "DETACHED"
    head = require_ok(git(path, "rev-parse", "--short=12", "HEAD"), f"读取 {path} HEAD").stdout.strip()
    upstream_result = git(path, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    upstream = upstream_result.stdout.strip() if upstream_result.code == 0 else "-"
    dirty = len(require_ok(git(path, "status", "--porcelain=v1", "-uall"), f"读取 {path} 状态").stdout.splitlines())
    return branch, head, upstream, dirty


def status_rows(config: Config) -> list[tuple[str, str, str, str, str, int]]:
    rows: list[tuple[str, str, str, str, str, int]] = []
    for repo_id in sorted(config.repositories):
        path = repository_path(config, repo_id)
        if _is_git_repo(path):
            branch, head, upstream, dirty = _short_status(path)
            rows.append(("anchor", repo_id, branch, head, upstream, dirty))
        else:
            rows.append(("anchor", repo_id, "MISSING", "-", "-", -1))
    for line in list_lines(config):
        for repo_id in line.repositories:
            path = line_repository_path(config, line, repo_id)
            if _is_git_repo(path):
                branch, head, upstream, dirty = _short_status(path)
                rows.append((f"{line.kind}:{line.id}", repo_id, branch, head, upstream, dirty))
            else:
                rows.append((f"{line.kind}:{line.id}", repo_id, "MISSING", "-", "-", -1))
    return rows


def doctor(config: Config) -> list[str]:
    """Return diagnostics.  Callers decide whether any FAIL means non-zero."""
    findings: list[str] = []
    root_git = _is_git_repo(config.root)
    findings.append(("WARN" if root_git else "PASS") + " workspace root " + ("is a Git repository" if root_git else "is not a Git repository"))
    for repo_id in sorted(config.repositories):
        anchor = repository_path(config, repo_id)
        if _is_git_repo(anchor):
            findings.append(f"PASS repository {repo_id}: {anchor}")
        else:
            findings.append(f"FAIL repository {repo_id}: missing or not Git: {anchor}")
    for line in list_lines(config):
        for repo_id in line.repositories:
            anchor = repository_path(config, repo_id)
            worktree = line_repository_path(config, line, repo_id)
            storage_mode = line.storage_for(repo_id)
            if not _is_git_repo(worktree):
                findings.append(f"FAIL {line.kind}:{line.id}/{repo_id}: missing worktree")
                continue
            actual_branch = git(worktree, "branch", "--show-current")
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
            anchor_common = git(anchor, "rev-parse", "--path-format=absolute", "--git-common-dir")
            worktree_common = git(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir")
            if anchor_common.code == 0 and worktree_common.code == 0 and anchor_common.stdout.strip() == worktree_common.stdout.strip():
                findings.append(f"PASS {line.kind}:{line.id}/{repo_id}: linked to configured anchor")
            else:
                findings.append(f"FAIL {line.kind}:{line.id}/{repo_id}: unexpected Git common-dir")
    return findings

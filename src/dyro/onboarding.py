from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
from typing import Callable

from .config import CONFIG_NAME, Config, load, validate_id
from .errors import DyroError, ValidationError
from .process import run
from .state import atomic_write_text, exclusive_lock


@dataclass(frozen=True)
class RepositoryInput:
    id: str
    path: str
    mount: str
    remote: str = ""


_DISCOVERY_SKIP_DIRS = frozenset({
    ".dyro",
    ".git",
    ".pytest_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "node_modules",
    "venv",
    # Delivery-line and task worktrees are derived state, never repository
    # anchors.  Skipping these conventional roots prevents a mature workspace
    # from registering the same repository more than once during onboarding.
    "versions",
    "worktrees",
    "hotfixes",
})
_BARE_TOML_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _repository_id(value: str) -> str:
    candidate = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip(".-_").lower()
    if not candidate:
        candidate = "repository"
    if not candidate[0].isalnum():
        candidate = "repository-" + candidate
    return candidate[:80]


def _unique_repository_id(value: str, used: set[str]) -> str:
    base = _repository_id(value)
    candidate = base
    index = 2
    while candidate in used:
        suffix = f"-{index}"
        candidate = base[: 80 - len(suffix)] + suffix
        index += 1
    used.add(candidate)
    return candidate


def _suggest_mount(relative_path: Path) -> str:
    parts = relative_path.parts
    for marker in ("services", "clients", "apps"):
        if marker in parts:
            return Path(*parts[parts.index(marker) :]).as_posix()
    if parts and parts[0] in ("anchors", "repos", "repositories") and len(parts) > 1:
        return Path(*parts[1:]).as_posix()
    return relative_path.as_posix()


def _relative_path(value: str, label: str) -> str:
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise ValidationError(f"{label} 必须是工作区内的相对路径：{value!r}")
    return path.as_posix()


def _toml_table_key(value: str) -> str:
    return value if _BARE_TOML_KEY.fullmatch(value) else _quote(value)


def _origin_url(repository: Path) -> str:
    result = run(("git", "-C", str(repository), "remote", "get-url", "origin"), timeout=10)
    return result.stdout.strip() if result.code == 0 else ""


def _is_git_repository(path: Path) -> bool:
    return run(("git", "-C", str(path), "rev-parse", "--git-dir"), timeout=10).code == 0


def discover_repositories(root: Path) -> list[RepositoryInput]:
    """Discover Git repository roots beneath a workspace without following nested trees."""
    workspace = root.resolve()
    repositories: list[RepositoryInput] = []
    used_ids: set[str] = set()
    for current, directories, _ in os.walk(workspace):
        directories[:] = sorted(directory for directory in directories if directory not in _DISCOVERY_SKIP_DIRS)
        candidate = Path(current)
        if candidate == workspace or not (candidate / ".git").exists() or not _is_git_repository(candidate):
            continue
        relative = candidate.relative_to(workspace)
        repositories.append(
            RepositoryInput(
                id=_unique_repository_id(candidate.name, used_ids),
                path=relative.as_posix(),
                mount=_suggest_mount(relative),
                remote=_origin_url(candidate),
            )
        )
        directories[:] = []
    return repositories


def repository_input_from_path(
    workspace: Path,
    value: str,
    *,
    repository_id: str | None = None,
    mount: str | None = None,
    remote: str | None = None,
) -> RepositoryInput:
    """Build one safe, workspace-relative repository entry from a CLI path."""
    root = workspace.resolve()
    candidate = Path(value).expanduser()
    destination = (root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    try:
        relative = destination.relative_to(root)
    except ValueError as exc:
        raise ValidationError(f"仓库必须位于工作区内：{destination}") from exc
    if not relative.parts:
        raise ValidationError("工作区根目录不能作为 repository anchor")
    if destination.exists() and not _is_git_repository(destination):
        raise ValidationError(f"仓库路径不是 Git 仓库：{destination}")
    if not destination.exists() and not remote:
        raise ValidationError(f"仓库路径不存在；请提供 --remote 供 bootstrap clone：{destination}")
    selected_id = repository_id or _repository_id(destination.name)
    validate_id(selected_id, "repository id")
    selected_mount = _relative_path(mount or _suggest_mount(relative), "repository mount")
    return RepositoryInput(
        id=selected_id,
        path=relative.as_posix(),
        mount=selected_mount,
        remote=remote if remote is not None else _origin_url(destination),
    )


def append_repository(config: Config, repository: RepositoryInput, *, dry_run: bool = False) -> None:
    """Append a repository table without reformatting or discarding existing Profile comments."""
    validate_id(repository.id, "repository id")
    _relative_path(repository.path, "repository path")
    _relative_path(repository.mount, "repository mount")
    config_file = config.root / CONFIG_NAME
    chunks = [
        f"[repositories.{_toml_table_key(repository.id)}]",
        f"path = {_quote(repository.path)}",
        f"mount = {_quote(repository.mount)}",
    ]
    if repository.remote:
        chunks.append(f"remote = {_quote(repository.remote)}")
    chunks.append("verify = []")
    if dry_run:
        return
    with exclusive_lock(config.root / ".dyro" / "profile.lock"):
        current = load(config.root)
        if repository.id in current.repositories:
            raise DyroError(f"仓库已配置：{repository.id}")
        content = config_file.read_text(encoding="utf-8").rstrip() + "\n\n" + "\n".join(chunks) + "\n"
        atomic_write_text(config_file, content)


def _quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_config(name: str, repositories: list[RepositoryInput], default_base: str = "main") -> str:
    if not repositories:
        raise ValidationError("向导至少需要一个仓库")
    validate_id(name, "workspace 名称")
    chunks = [
        "schema_version = 1",
        "",
        "[workspace]",
        f"name = {_quote(name)}",
        "",
        "[layout]",
        'anchors = "repositories"',
        'lines = "versions"',
        'hotfixes = "hotfixes"',
        'tasks = "worktrees"',
        "",
        "[policy]",
        f"default_base = {_quote(default_base)}",
        'task_branch_prefix = "task/"',
        "allow_push = false",
        "require_clean_merge = true",
        "require_external_signoff = false",
        'execution_mode = "local"',
        "",
        "[adapters.codex]",
        'launch = ["codex", "-C", "{workspace}"]',
        'read = ["codex", "exec", "--skip-git-repo-check", "--sandbox", "workspace-write", "{prompt}"]',
        'write = ["codex", "exec", "--skip-git-repo-check", "--sandbox", "workspace-write", "{prompt}"]',
    ]
    for repo in repositories:
        validate_id(repo.id, "repository id")
        chunks.extend(("", f"[repositories.{_toml_table_key(repo.id)}]", f"path = {_quote(repo.path)}", f"mount = {_quote(repo.mount)}"))
        if repo.remote:
            chunks.append(f"remote = {_quote(repo.remote)}")
        chunks.append("verify = []")
    return "\n".join(chunks) + "\n"


def ask_for_workspace(name_default: str, ask: Callable[[str], str] = input) -> tuple[str, list[RepositoryInput], str]:
    name = ask(f"工作区名称 [{name_default}]：").strip() or name_default
    validate_id(name, "workspace 名称")
    base = ask("默认基线分支 [main]：").strip() or "main"
    repositories: list[RepositoryInput] = []
    print("逐个登记仓库；仓库 ID 留空即结束。路径相对工作区，例如 repositories/services/api。")
    while True:
        repo_id = ask("仓库 ID：").strip()
        if not repo_id:
            break
        validate_id(repo_id, "repository id")
        path = ask(f"{repo_id} anchor 路径：").strip()
        if not path:
            raise ValidationError("anchor 路径不能为空")
        mount = ask(f"{repo_id} 在开发线内挂载路径 [{repo_id}]：").strip() or repo_id
        remote = ask(f"{repo_id} Git remote（可空，供 bootstrap clone）：").strip()
        repositories.append(RepositoryInput(repo_id, path, mount, remote))
    if not repositories:
        raise ValidationError("至少登记一个仓库")
    return name, repositories, base


def bootstrap(config: Config, *, dry_run: bool = False) -> list[str]:
    """Clone only absent repository anchors with configured remotes.

    An existing non-Git directory is an error, never a target for overwrite.
    """
    messages: list[str] = []
    for repo_id, repo in sorted(config.repositories.items()):
        destination = config.root / repo.path
        if destination.exists():
            check = run(("git", "-C", str(destination), "rev-parse", "--git-dir"), dry_run=False)
            if check.code == 0:
                messages.append(f"PASS {repo_id}: 已存在")
                continue
            raise DyroError(f"拒绝覆盖非 Git 目录：{destination}")
        if not repo.remote:
            raise DyroError(f"{repo_id} 缺少 remote，无法 bootstrap：{destination}")
        command = ("git", "clone", repo.remote, str(destination))
        messages.append(("DRY RUN " if dry_run else "CLONE ") + f"{repo_id}: {' '.join(command)}")
        if not dry_run:
            destination.parent.mkdir(parents=True, exist_ok=True)
            result = run(command, timeout=600)
            if result.code != 0:
                raise DyroError(f"clone {repo_id} 失败：{result.stdout.strip()}")
    return messages

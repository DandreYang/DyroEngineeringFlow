from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Callable

from .config import CONFIG_NAME, Config, load, validate_id
from .errors import DyroError, ValidationError
from .process import run
from .read_limits import open_safe_directory_chain
from .state import atomic_write_text, exclusive_lock


@dataclass(frozen=True)
class RepositoryInput:
    id: str
    path: str
    mount: str
    remote: str = ""
    verify: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True)
class SetupPlan:
    """A previewable first-run plan.  Rendering it must never mutate a workspace."""

    root: Path
    name: str
    repositories: tuple[RepositoryInput, ...]
    default_base: str
    line_id: str | None
    branch: str | None
    provider_preset: str | None = None

    @property
    def needs_bootstrap(self) -> bool:
        return any(not (self.root / repository.path).exists() for repository in self.repositories)


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


def is_git_repository(path: Path) -> bool:
    """Return whether *path* is a Git worktree without exposing Git internals to the CLI."""

    return _is_git_repository(path)


def origin_url(repository: Path) -> str:
    """Return a repository's origin URL when one is configured."""

    return _origin_url(repository)


def current_branch(repository: Path) -> str:
    """Return the checked-out branch name, or an empty string for detached HEAD."""

    result = run(("git", "-C", str(repository), "branch", "--show-current"), timeout=10)
    return result.stdout.strip() if result.code == 0 else ""


def repository_from_remote(remote: str, *, path: str | None = None) -> RepositoryInput:
    """Create a conservative repository proposal for an explicitly supplied remote."""

    candidate = remote.strip()
    if not candidate or "\n" in candidate or "\r" in candidate:
        raise ValidationError("Git remote 必须是单行非空地址")
    tail = candidate.rstrip("/").rsplit("/", 1)[-1]
    if ":" in tail:
        tail = tail.rsplit(":", 1)[-1]
    name = tail[:-4] if tail.endswith(".git") else tail
    repository_id = _repository_id(name)
    relative_path = _relative_path(path or f"repositories/{repository_id}", "repository path")
    return RepositoryInput(
        id=repository_id,
        path=relative_path,
        mount=repository_id,
        remote=candidate,
    )


def sibling_workspace_for(repository: Path) -> Path:
    """Suggest, but never create, a workspace beside a repository root."""

    return repository.parent / f"{repository.name}-dyro"


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
    chunks.append(f"verify = {_quote([list(command) for command in repository.verify])}")
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


def render_config(
    name: str,
    repositories: list[RepositoryInput],
    default_base: str = "main",
    *,
    adapter_presets: tuple[str, ...] = (),
    recommended_tool: str = "",
) -> str:
    if not repositories:
        raise ValidationError("向导至少需要一个仓库")
    validate_id(name, "workspace 名称")
    if recommended_tool:
        validate_id(recommended_tool, "workspace.recommended_tool")
    chunks = [
        "schema_version = 1",
        "",
        "[workspace]",
        f"name = {_quote(name)}",
    ]
    if recommended_tool:
        chunks.append(f"recommended_tool = {_quote(recommended_tool)}")
    chunks.extend(
        [
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
        "require_signed_execution = false",
        "require_signed_review = false",
        "require_signed_signoff = false",
        'execution_mode = "local"',
        ]
    )
    for preset in adapter_presets:
        if preset != "codex":
            raise ValidationError(f"首次设置不支持的 Agent preset：{preset}")
        chunks.extend(
            (
                "",
                "[adapters.codex]",
                'launch = ["codex", "-C", "{workspace}"]',
                'read = ["codex", "exec", "--skip-git-repo-check", "--sandbox", "workspace-write", "{prompt}"]',
                'write = ["codex", "exec", "--skip-git-repo-check", "--sandbox", "workspace-write", "{prompt}"]',
            )
        )
    for repo in repositories:
        validate_id(repo.id, "repository id")
        chunks.extend(("", f"[repositories.{_toml_table_key(repo.id)}]", f"path = {_quote(repo.path)}", f"mount = {_quote(repo.mount)}"))
        if repo.remote:
            chunks.append(f"remote = {_quote(repo.remote)}")
        chunks.append(f"verify = {_quote([list(command) for command in repo.verify])}")
    return "\n".join(chunks) + "\n"


def render_setup_plan(plan: SetupPlan) -> tuple[str, ...]:
    """Render a human-readable plan without leaking implementation-only details."""

    lines = [
        f"工作区：{plan.root}",
        f"Profile：{plan.name}",
        f"默认基线：{plan.default_base}",
    ]
    for repository in plan.repositories:
        state = "clone" if not (plan.root / repository.path).exists() else "register"
        lines.append(f"仓库：{repository.id} → {repository.path}（{state}）")
    if plan.line_id:
        lines.append(f"开发线：{plan.line_id}（{plan.branch}）")
    else:
        lines.append("开发线：暂不创建")
    if plan.provider_preset:
        lines.append(f"Agent：{plan.provider_preset}")
    else:
        lines.append("Agent：暂不配置")
    return tuple(lines)


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


def validate_bootstrap_destination(config: Config, relative: str) -> Path:
    """Reject clone targets whose path can escape through a symlink parent."""

    destination = config.root / relative
    try:
        root_info = config.root.lstat()
    except OSError as exc:
        raise DyroError(f"bootstrap workspace root 无法读取：{config.root}") from exc
    current = config.root
    if current.is_symlink() or not current.is_dir():
        raise DyroError(f"bootstrap 路径不能经过符号链接：{current}")
    for part in Path(relative).parts:
        current /= part
        if current.is_symlink():
            raise DyroError(f"bootstrap 路径不能经过符号链接：{current}")
        if current != destination and current.exists() and not current.is_dir():
            raise DyroError(f"bootstrap 父路径必须是目录：{current}")
    current_root_info = config.root.lstat()
    if (current_root_info.st_dev, current_root_info.st_ino) != (
        root_info.st_dev,
        root_info.st_ino,
    ):
        raise DyroError("bootstrap workspace root 在预检期间发生变化")
    return destination


@contextmanager
def _open_bootstrap_parent(config: Config, relative: str):
    """Hold the clone destination parent by descriptor until atomic publish."""

    required = (os.open, os.mkdir, os.rename, os.stat)
    if (
        os.name != "posix"
        or not hasattr(os, "O_NOFOLLOW")
        or any(item not in os.supports_dir_fd for item in required)
    ):
        raise DyroError("当前平台缺少安全的 descriptor-bound bootstrap 能力")
    parts = Path(relative).parts
    if not parts:
        raise ValidationError("bootstrap 仓库路径不能为空")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW
    with open_safe_directory_chain(config.root, config.root) as root_fd:
        assert root_fd is not None
        current_fd = os.dup(root_fd)
        try:
            for part in parts[:-1]:
                try:
                    child_fd = os.open(part, flags, dir_fd=current_fd)
                except FileNotFoundError:
                    os.mkdir(part, mode=0o700, dir_fd=current_fd)
                    child_fd = os.open(part, flags, dir_fd=current_fd)
                parent_fd = current_fd
                current_fd = child_fd
                os.close(parent_fd)
            leaf = parts[-1]
            try:
                os.stat(leaf, dir_fd=current_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise DyroError(f"拒绝覆盖已有 bootstrap 目标：{config.root / relative}")
            yield current_fd, leaf
        finally:
            os.close(current_fd)


def _copy_bootstrap_tree(source: Path, destination_fd: int) -> None:
    for entry in os.scandir(source):
        info = entry.stat(follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            os.symlink(os.readlink(entry.path), entry.name, dir_fd=destination_fd)
            continue
        if stat.S_ISDIR(info.st_mode):
            os.mkdir(entry.name, mode=stat.S_IMODE(info.st_mode), dir_fd=destination_fd)
            child_fd = os.open(
                entry.name,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW,
                dir_fd=destination_fd,
            )
            try:
                _copy_bootstrap_tree(Path(entry.path), child_fd)
                os.fchmod(child_fd, stat.S_IMODE(info.st_mode))
            finally:
                os.close(child_fd)
            continue
        if not stat.S_ISREG(info.st_mode):
            raise DyroError(f"clone 产物包含不支持的文件类型：{entry.name}")
        source_fd = os.open(entry.path, os.O_RDONLY | os.O_NOFOLLOW)
        destination_file_fd = os.open(
            entry.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            stat.S_IMODE(info.st_mode),
            dir_fd=destination_fd,
        )
        try:
            while True:
                chunk = os.read(source_fd, 1024 * 1024)
                if not chunk:
                    break
                view = memoryview(chunk)
                while view:
                    written = os.write(destination_file_fd, view)
                    view = view[written:]
            os.fchmod(destination_file_fd, stat.S_IMODE(info.st_mode))
        finally:
            os.close(destination_file_fd)
            os.close(source_fd)


def _clear_bootstrap_directory(directory_fd: int) -> None:
    for entry in os.scandir(directory_fd):
        info = entry.stat(follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
            child_fd = os.open(
                entry.name,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            try:
                _clear_bootstrap_directory(child_fd)
            finally:
                os.close(child_fd)
            os.rmdir(entry.name, dir_fd=directory_fd)
        else:
            os.unlink(entry.name, dir_fd=directory_fd)


def _publish_bootstrap_tree(parent_fd: int, leaf: str, source: Path) -> None:
    os.mkdir(leaf, mode=0o700, dir_fd=parent_fd)
    destination_fd = os.open(
        leaf,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW,
        dir_fd=parent_fd,
    )
    try:
        _copy_bootstrap_tree(source, destination_fd)
    except BaseException:
        try:
            _clear_bootstrap_directory(destination_fd)
            os.rmdir(leaf, dir_fd=parent_fd)
        except OSError:
            pass
        raise
    finally:
        os.close(destination_fd)


def bootstrap(
    config: Config,
    *,
    branch: str | None = None,
    dry_run: bool = False,
) -> list[str]:
    """Clone only absent repository anchors with configured remotes.

    An existing non-Git directory is an error, never a target for overwrite.
    """
    messages: list[str] = []
    for repo_id, repo in sorted(config.repositories.items()):
        destination = validate_bootstrap_destination(config, repo.path)
        if destination.exists() or destination.is_symlink():
            check = run(("git", "-C", str(destination), "rev-parse", "--git-dir"), dry_run=False)
            if check.code == 0:
                messages.append(f"PASS {repo_id}: 已存在")
                continue
            raise DyroError(f"拒绝覆盖非 Git 目录：{destination}")
        if not repo.remote:
            raise DyroError(f"{repo_id} 缺少 remote，无法 bootstrap：{destination}")
        command = ("git", "clone")
        if branch:
            # A bare remote can retain an empty or stale symbolic HEAD even
            # when the requested base exists.  Explicitly checking out the
            # accepted base guarantees that create_line can resolve a local
            # anchor without trusting that remote default.
            command += (f"--branch={branch}",)
        display_command = (*command, repo.remote, str(destination))
        messages.append(("DRY RUN " if dry_run else "CLONE ") + f"{repo_id}: {' '.join(display_command)}")
        if dry_run:
            continue
        with tempfile.TemporaryDirectory(prefix="dyro-bootstrap-") as temp_root:
            stage_path = Path(temp_root) / "repository"
            result = run(
                (*command, repo.remote, str(stage_path)),
                timeout=600,
            )
            if result.code != 0:
                raise DyroError(f"clone {repo_id} 失败：{result.stdout.strip()}")
            with _open_bootstrap_parent(config, repo.path) as (parent_fd, leaf):
                _publish_bootstrap_tree(parent_fd, leaf, stage_path)
    return messages

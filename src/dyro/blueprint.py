from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import tomllib
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .config import CONFIG_NAME, Config, load, validate_id
from .errors import DyroError, ValidationError
from .onboarding import RepositoryInput, render_config
from .process import git, require_ok, run
from .state import atomic_write_text, exclusive_lock
from .workspace import STORAGE_MODES, Line, create_line, doctor, get_line


BLUEPRINT_FILENAME = "dyro-blueprint.toml"
JOIN_STATE_FILE = ".dyro/join.json"
MAX_BLUEPRINT_BYTES = 1024 * 1024
_FULL_OBJECT_ID = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")


@dataclass(frozen=True)
class BlueprintRepository:
    id: str
    remote: str
    path: str
    mount: str
    verify: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True)
class BlueprintLine:
    id: str
    branch: str
    repository_bases: Mapping[str, str]
    storage_modes: Mapping[str, str]

    def base_for(self, repository_id: str) -> str:
        return self.repository_bases[repository_id]

    def storage_for(self, repository_id: str) -> str:
        return self.storage_modes.get(repository_id, "linked-worktree")


@dataclass(frozen=True)
class Blueprint:
    name: str
    suggested_directory: str
    default_line: str
    default_base: str
    repositories: Mapping[str, BlueprintRepository]
    lines: Mapping[str, BlueprintLine]
    recommended_tool: str = ""


@dataclass(frozen=True)
class BlueprintDocument:
    blueprint: Blueprint
    content: bytes
    sha256: str
    source: str


@dataclass(frozen=True)
class JoinPlan:
    document: BlueprintDocument
    root: Path
    line: BlueprintLine
    profile: str


def _table(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError(f"{label} 必须是 TOML 表")
    return value


def _reject_unknown(raw: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = set(raw) - allowed
    if unknown:
        raise ValidationError(f"{label} 包含未知字段：{', '.join(sorted(unknown))}")


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label} 必须是非空字符串")
    if any(character in value for character in ("\x00", "\n", "\r")):
        raise ValidationError(f"{label} 必须是单行字符串")
    return value.strip()


def _relative_path(value: Any, label: str) -> str:
    raw = _string(value, label)
    path = Path(raw)
    if path.is_absolute() or raw in (".", "..") or ".." in path.parts:
        raise ValidationError(f"{label} 必须是工作区内的相对路径：{raw!r}")
    return path.as_posix()


def _commands(value: Any, label: str) -> tuple[tuple[str, ...], ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValidationError(f"{label} 必须是 argv 数组的数组")
    commands: list[tuple[str, ...]] = []
    for index, command in enumerate(value, start=1):
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(argument, str) and argument for argument in command)
        ):
            raise ValidationError(f"{label} 第 {index} 项必须是非空 argv 字符串数组")
        commands.append(tuple(command))
    return tuple(commands)


def _safe_remote(value: Any, label: str) -> str:
    remote = _string(value, label)
    if remote.startswith("-"):
        raise ValidationError(f"{label} 不能以连字符开头")
    if Path(remote).is_absolute():
        return remote
    parsed = urlsplit(remote)
    if parsed.scheme in ("http", "https"):
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValidationError(
                f"{label} 不得内嵌凭据、查询参数或 fragment；请使用 Git credential helper 或 SSH"
            )
        if parsed.scheme != "https":
            raise ValidationError(f"{label} 必须使用 HTTPS 或 SSH")
    elif parsed.scheme in ("ssh", "file"):
        if parsed.password or parsed.query or parsed.fragment:
            raise ValidationError(f"{label} 不得内嵌密码、查询参数或 fragment")
    elif parsed.scheme:
        raise ValidationError(f"{label} 使用了不支持的协议：{parsed.scheme}")
    elif not parsed.scheme and ":" not in remote and not Path(remote).is_absolute():
        raise ValidationError(f"{label} 的本地路径必须是绝对路径")
    return remote


def _git_branch(value: Any, label: str) -> str:
    branch = _string(value, label)
    invalid = (
        branch.startswith(("-", ".", "/"))
        or branch.endswith((".", "/"))
        or ".." in branch
        or "@{" in branch
        or "//" in branch
        or any(character in branch for character in " ~^:?*[\\")
        or any(ord(character) < 32 or ord(character) == 127 for character in branch)
    )
    if invalid:
        raise ValidationError(f"{label} 不是安全的 Git 分支名称：{branch!r}")
    return branch


def _object_id(value: Any, label: str) -> str:
    object_id = _string(value, label).lower()
    if not _FULL_OBJECT_ID.fullmatch(object_id):
        raise ValidationError(f"{label} 必须是完整提交 SHA，不能使用会移动的分支或 tag")
    return object_id


def _ensure_non_overlapping(values: Mapping[str, str], label: str) -> None:
    items = sorted((Path(value).parts, repository_id) for repository_id, value in values.items())
    for index, (parts, repository_id) in enumerate(items):
        for other_parts, other_id in items[index + 1 :]:
            shortest = min(len(parts), len(other_parts))
            if parts[:shortest] == other_parts[:shortest]:
                raise ValidationError(
                    f"{label} 不能相同或相互嵌套：{repository_id} 与 {other_id}"
                )


def parse_blueprint(content: bytes) -> Blueprint:
    if not content or len(content) > MAX_BLUEPRINT_BYTES:
        raise ValidationError(f"蓝图必须是 1 到 {MAX_BLUEPRINT_BYTES} 字节的 UTF-8 TOML 文件")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError("蓝图必须使用 UTF-8 编码") from exc
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValidationError(f"蓝图 TOML 格式错误：{exc}") from exc
    _reject_unknown(raw, {"schema_version", "workspace", "repositories", "lines"}, "蓝图")
    if raw.get("schema_version") != 1:
        raise ValidationError("蓝图仅支持 schema_version = 1")

    workspace = _table(raw.get("workspace"), "workspace")
    _reject_unknown(
        workspace,
        {
            "name",
            "suggested_directory",
            "default_line",
            "default_base",
            "recommended_tool",
        },
        "workspace",
    )
    name = validate_id(_string(workspace.get("name"), "workspace.name"), "workspace.name")
    suggested_directory = validate_id(
        _string(workspace.get("suggested_directory", name), "workspace.suggested_directory"),
        "workspace.suggested_directory",
    )
    default_line = validate_id(
        _string(workspace.get("default_line"), "workspace.default_line"),
        "workspace.default_line",
    )
    default_base = _string(workspace.get("default_base", "main"), "workspace.default_base")
    recommended_tool_raw = workspace.get("recommended_tool", "")
    if not isinstance(recommended_tool_raw, str):
        raise ValidationError("workspace.recommended_tool 必须是字符串")
    recommended_tool = recommended_tool_raw.strip()
    if recommended_tool:
        validate_id(recommended_tool, "workspace.recommended_tool")

    repositories_raw = _table(raw.get("repositories"), "repositories")
    if not repositories_raw:
        raise ValidationError("蓝图至少需要一个 repositories.<id>")
    repositories: dict[str, BlueprintRepository] = {}
    for repository_id, value in repositories_raw.items():
        validate_id(repository_id, "repository id")
        entry = _table(value, f"repositories.{repository_id}")
        _reject_unknown(entry, {"remote", "path", "mount", "verify"}, f"repositories.{repository_id}")
        repositories[repository_id] = BlueprintRepository(
            id=repository_id,
            remote=_safe_remote(entry.get("remote"), f"repositories.{repository_id}.remote"),
            path=_relative_path(entry.get("path"), f"repositories.{repository_id}.path"),
            mount=_relative_path(entry.get("mount", repository_id), f"repositories.{repository_id}.mount"),
            verify=_commands(entry.get("verify", []), f"repositories.{repository_id}.verify"),
        )
    _ensure_non_overlapping(
        {repository_id: repository.path for repository_id, repository in repositories.items()},
        "repository paths",
    )
    _ensure_non_overlapping(
        {repository_id: repository.mount for repository_id, repository in repositories.items()},
        "repository mounts",
    )

    lines_raw = _table(raw.get("lines"), "lines")
    if not lines_raw:
        raise ValidationError("蓝图至少需要一个 lines.<id>")
    lines: dict[str, BlueprintLine] = {}
    repository_ids = set(repositories)
    for line_id, value in lines_raw.items():
        validate_id(line_id, "开发线 ID")
        entry = _table(value, f"lines.{line_id}")
        _reject_unknown(entry, {"branch", "bases", "storage_modes"}, f"lines.{line_id}")
        bases_raw = _table(entry.get("bases"), f"lines.{line_id}.bases")
        if set(bases_raw) != repository_ids:
            missing = repository_ids - set(bases_raw)
            unknown = set(bases_raw) - repository_ids
            details: list[str] = []
            if missing:
                details.append("缺少 " + ", ".join(sorted(missing)))
            if unknown:
                details.append("未知 " + ", ".join(sorted(unknown)))
            raise ValidationError(f"lines.{line_id}.bases 必须覆盖全部仓库：{'；'.join(details)}")
        bases = {
            repository_id: _object_id(value, f"lines.{line_id}.bases.{repository_id}")
            for repository_id, value in bases_raw.items()
        }
        storage_raw = _table(entry.get("storage_modes", {}), f"lines.{line_id}.storage_modes")
        unknown_storage = set(storage_raw) - repository_ids
        if unknown_storage:
            raise ValidationError(
                f"lines.{line_id}.storage_modes 包含未知仓库：{', '.join(sorted(unknown_storage))}"
            )
        storage_modes: dict[str, str] = {}
        for repository_id, mode in storage_raw.items():
            if mode not in STORAGE_MODES:
                raise ValidationError(
                    f"lines.{line_id}.storage_modes.{repository_id} 必须是："
                    + ", ".join(sorted(STORAGE_MODES))
                )
            if mode != "linked-worktree":
                raise ValidationError(
                    f"lines.{line_id}.storage_modes.{repository_id} 必须是 linked-worktree；"
                    "join 不会让开发线共享 anchor"
                )
            storage_modes[repository_id] = mode
        lines[line_id] = BlueprintLine(
            id=line_id,
            branch=_git_branch(entry.get("branch"), f"lines.{line_id}.branch"),
            repository_bases=bases,
            storage_modes=storage_modes,
        )
    if default_line not in lines:
        raise ValidationError(f"workspace.default_line 未定义：{default_line}")
    return Blueprint(
        name=name,
        suggested_directory=suggested_directory,
        default_line=default_line,
        default_base=default_base,
        recommended_tool=recommended_tool,
        repositories=repositories,
        lines=lines,
    )


def _read_file(path: Path) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise DyroError(f"无法读取蓝图：{path}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValidationError(f"蓝图必须是普通文件：{path}")
        if metadata.st_size > MAX_BLUEPRINT_BYTES:
            raise ValidationError(f"蓝图超过 {MAX_BLUEPRINT_BYTES} 字节限制：{path}")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            content = handle.read(MAX_BLUEPRINT_BYTES + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(content) > MAX_BLUEPRINT_BYTES:
        raise ValidationError(f"蓝图超过 {MAX_BLUEPRINT_BYTES} 字节限制：{path}")
    return content


def _blueprint_relative_path(value: str) -> Path:
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise ValidationError("--file 必须是蓝图仓库内的相对路径")
    return path


def _sanitized_url(value: str) -> str:
    parsed = urlsplit(value)
    hostname = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    return urlunsplit((parsed.scheme, hostname + port, parsed.path, "", ""))


def _validate_remote_source(source: str) -> None:
    if source.startswith("-") or any(character in source for character in ("\x00", "\n", "\r")):
        raise ValidationError("蓝图来源无效")
    candidate = source[4:] if source.startswith("git+") else source
    if Path(candidate).is_absolute():
        return
    parsed = urlsplit(candidate)
    if parsed.scheme in ("http", "https"):
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValidationError("蓝图来源不得内嵌凭据、查询参数或 fragment")
        if parsed.scheme != "https":
            raise ValidationError("远程蓝图必须使用 HTTPS 或 SSH")
    elif parsed.scheme in ("ssh", "file"):
        if parsed.password or parsed.query or parsed.fragment:
            raise ValidationError("蓝图来源不得内嵌密码、查询参数或 fragment")
    elif parsed.scheme:
        raise ValidationError(f"蓝图来源使用了不支持的协议：{parsed.scheme}")


def _looks_like_git_source(source: str) -> bool:
    return (
        source.startswith("git+")
        or source.startswith("git@")
        or source.startswith("ssh://")
        or source.endswith(".git")
    )


def _load_git_blueprint(source: str, *, git_ref: str | None, relative_file: Path) -> tuple[bytes, str]:
    remote = source[4:] if source.startswith("git+") else source
    _validate_remote_source(source)
    with tempfile.TemporaryDirectory(prefix="dyro-blueprint-source-") as temporary:
        checkout = Path(temporary) / "source"
        command = ["git", "clone", "--depth=1"]
        if git_ref:
            command.extend(("--branch", _git_branch(git_ref, "--ref")))
        command.extend(("--", remote, str(checkout)))
        require_ok(run(command, timeout=300), "读取 Git 蓝图仓库")
        _reject_symlink_components(checkout, relative_file, "蓝图文件路径")
        content = _read_file(checkout / relative_file)
    label = _sanitized_url(remote) if "://" in remote else remote
    return content, label


def _load_https_blueprint(source: str) -> tuple[bytes, str]:
    _validate_remote_source(source)
    parsed = urlsplit(source)
    if parsed.scheme != "https":
        raise ValidationError("远程蓝图文件必须使用 HTTPS；Git 仓库可使用 SSH")
    request = Request(source, headers={"User-Agent": "dyro-blueprint/1"})
    try:
        with urlopen(request, timeout=30) as response:
            final_url = response.geturl()
            _validate_remote_source(final_url)
            if urlsplit(final_url).scheme != "https":
                raise ValidationError("蓝图下载被重定向到非 HTTPS 地址")
            content = response.read(MAX_BLUEPRINT_BYTES + 1)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise DyroError(f"下载蓝图失败：{_sanitized_url(source)}") from exc
    if len(content) > MAX_BLUEPRINT_BYTES:
        raise ValidationError(f"蓝图超过 {MAX_BLUEPRINT_BYTES} 字节限制")
    return content, _sanitized_url(source)


def load_blueprint_source(
    source: str,
    *,
    git_ref: str | None = None,
    blueprint_file: str = BLUEPRINT_FILENAME,
) -> BlueprintDocument:
    source = source.strip()
    if not source:
        raise ValidationError("蓝图来源不能为空")
    relative_file = _blueprint_relative_path(blueprint_file)
    local = Path(source).expanduser()
    if local.exists():
        if local.is_dir():
            _reject_symlink_components(local, relative_file, "蓝图文件路径")
            selected = local / relative_file
        else:
            selected = local
        content = _read_file(selected)
        label = str(selected.resolve())
    elif _looks_like_git_source(source):
        content, label = _load_git_blueprint(
            source,
            git_ref=git_ref,
            relative_file=relative_file,
        )
    else:
        content, label = _load_https_blueprint(source)
    blueprint = parse_blueprint(content)
    return BlueprintDocument(
        blueprint=blueprint,
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
        source=label,
    )


def default_join_root(blueprint: Blueprint) -> Path:
    override = os.environ.get("DYRO_PROJECTS_HOME", "").strip()
    parent = Path(override).expanduser() if override else Path.home() / "DyroProjects"
    return (parent / blueprint.suggested_directory).absolute()


def build_join_plan(
    document: BlueprintDocument,
    *,
    target: str | Path | None,
    line_id: str | None,
) -> JoinPlan:
    blueprint = document.blueprint
    selected_line = line_id or blueprint.default_line
    validate_id(selected_line, "开发线 ID")
    try:
        line = blueprint.lines[selected_line]
    except KeyError as exc:
        raise ValidationError(
            f"蓝图中没有开发线 {selected_line}；可选：{', '.join(sorted(blueprint.lines))}"
        ) from exc
    root = (
        Path(target).expanduser().absolute()
        if target is not None
        else default_join_root(blueprint)
    )
    repositories = [
        RepositoryInput(
            id=repository.id,
            path=repository.path,
            mount=repository.mount,
            remote=repository.remote,
            verify=repository.verify,
        )
        for repository in blueprint.repositories.values()
    ]
    profile = render_config(
        blueprint.name,
        repositories,
        blueprint.default_base,
        recommended_tool=blueprint.recommended_tool,
    )
    return JoinPlan(document=document, root=root, line=line, profile=profile)


def render_join_plan(plan: JoinPlan) -> tuple[str, ...]:
    blueprint = plan.document.blueprint
    lines = [
        f"工作区：{blueprint.name}",
        f"目标目录：{plan.root}",
        f"开发线：{plan.line.id}（{plan.line.branch}）",
        f"蓝图 SHA-256：{plan.document.sha256}",
    ]
    if blueprint.recommended_tool:
        lines.append(f"推荐编码工具：{blueprint.recommended_tool}（仅推荐，不自动安装）")
    for repository_id, repository in blueprint.repositories.items():
        destination = plan.root / repository.path
        action = "复用并核验" if destination.is_dir() else "独立 clone"
        lines.append(
            f"仓库：{repository_id} → {repository.path}（{action}，{plan.line.base_for(repository_id)[:12]}）"
        )
    lines.extend(("不会覆盖非空目录", "不会执行 push"))
    return tuple(lines)


def _join_state(plan: JoinPlan, status: str) -> str:
    payload = {
        "schema_version": 1,
        "blueprint_sha256": plan.document.sha256,
        "line": plan.line.id,
        "status": status,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _read_join_state(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise DyroError(f"join 状态不是安全的普通文件：{path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DyroError(f"join 状态无法读取：{path}") from exc
    if not isinstance(raw, dict) or set(raw) != {
        "schema_version",
        "blueprint_sha256",
        "line",
        "status",
    }:
        raise DyroError(f"join 状态结构无效：{path}")
    if raw.get("schema_version") != 1 or raw.get("status") not in ("in_progress", "complete"):
        raise DyroError(f"join 状态版本或状态无效：{path}")
    return raw


def _prepare_join_state(plan: JoinPlan) -> None:
    state_path = plan.root / JOIN_STATE_FILE
    state = _read_join_state(state_path)
    if state is None:
        allowed = plan.root / ".dyro"
        unexpected = [entry for entry in plan.root.iterdir() if entry != allowed]
        if unexpected:
            raise DyroError(f"目标目录已存在且非空：{plan.root}")
        if allowed.exists():
            unexpected_state = [entry for entry in allowed.iterdir() if entry.name != "join.lock"]
            if unexpected_state:
                raise DyroError(f"目标目录已存在且非空：{plan.root}")
        atomic_write_text(state_path, _join_state(plan, "in_progress"))
        return
    if state["blueprint_sha256"] != plan.document.sha256 or state["line"] != plan.line.id:
        raise DyroError("目标目录的 join 蓝图或开发线与本次计划不一致")


def _validate_profile(config: Config, plan: JoinPlan) -> None:
    blueprint = plan.document.blueprint
    if config.name != blueprint.name or config.policy.default_base != blueprint.default_base:
        raise DyroError("现有 Profile 与 join 蓝图的工作区信息不一致")
    if set(config.repositories) != set(blueprint.repositories):
        raise DyroError("现有 Profile 与 join 蓝图的仓库集合不一致")
    for repository_id, expected in blueprint.repositories.items():
        actual = config.repositories[repository_id]
        if (
            actual.path,
            actual.mount,
            actual.remote,
            actual.verify,
        ) != (
            expected.path,
            expected.mount,
            expected.remote,
            expected.verify,
        ):
            raise DyroError(f"现有 Profile 与 join 蓝图的仓库配置不一致：{repository_id}")


def _existing_anchor_matches(destination: Path, repository: BlueprintRepository, object_id: str) -> None:
    if destination.is_symlink():
        raise DyroError(f"拒绝复用符号链接仓库：{destination}")
    if not destination.is_dir() or git(destination, "rev-parse", "--git-dir").code != 0:
        raise DyroError(f"拒绝复用非 Git 仓库：{destination}")
    origin = require_ok(
        git(destination, "remote", "get-url", "origin"),
        f"读取 {repository.id} origin",
    ).stdout.strip()
    if origin != repository.remote:
        raise DyroError(f"{repository.id} origin 与蓝图不一致：{destination}")
    status = require_ok(
        git(destination, "status", "--porcelain=v1", "-uall"),
        f"读取 {repository.id} 状态",
    ).stdout.strip()
    if status:
        raise DyroError(f"{repository.id} anchor 不干净，拒绝覆盖：{destination}")
    head = require_ok(git(destination, "rev-parse", "HEAD"), f"读取 {repository.id} HEAD").stdout.strip()
    if head.lower() != object_id:
        raise DyroError(f"{repository.id} anchor HEAD 与蓝图固定基线不一致：{head[:12]}")
    branch = require_ok(
        git(destination, "branch", "--show-current"),
        f"读取 {repository.id} anchor 分支",
    ).stdout.strip()
    if branch:
        raise DyroError(f"{repository.id} anchor 必须保持 detached HEAD，当前为 {branch}")


def _clone_anchor(destination: Path, repository: BlueprintRepository, object_id: str) -> None:
    if destination.exists() or destination.is_symlink():
        _existing_anchor_matches(destination, repository, object_id)
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".dyro-{repository.id}-", dir=destination.parent) as temporary:
        staged = Path(temporary) / "anchor"
        require_ok(
            run(
                ("git", "clone", "--no-checkout", "--", repository.remote, str(staged)),
                timeout=600,
            ),
            f"clone {repository.id}",
        )
        resolved = require_ok(
            git(staged, "rev-parse", "--verify", f"{object_id}^{{commit}}"),
            f"校验 {repository.id} 固定基线 {object_id}",
        ).stdout.strip()
        if resolved.lower() != object_id:
            raise DyroError(f"{repository.id} 固定基线解析结果不一致")
        require_ok(git(staged, "checkout", "--detach", object_id), f"固定 {repository.id} anchor")
        os.replace(staged, destination)
    _existing_anchor_matches(destination, repository, object_id)


def _reject_symlink_components(root: Path, relative: Path, label: str) -> None:
    if root.is_symlink():
        raise DyroError(f"{label} 不能经过符号链接：{root}")
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise DyroError(f"{label} 不能经过符号链接：{current}")


def _validate_workspace_paths(config: Config, plan: JoinPlan) -> None:
    _reject_symlink_components(plan.root, Path(CONFIG_NAME), "Profile 路径")
    _reject_symlink_components(plan.root, Path(".dyro"), "状态目录")
    for repository in config.repositories.values():
        _reject_symlink_components(plan.root, Path(repository.path), f"{repository.id} anchor 路径")
        line_relative = Path(config.layout.lines) / plan.line.id / repository.mount
        _reject_symlink_components(plan.root, line_relative, f"{repository.id} 开发线路径")


def _line_from_blueprint(plan: JoinPlan) -> Line:
    repository_ids = tuple(plan.document.blueprint.repositories)
    default_base = plan.line.base_for(repository_ids[0])
    base_overrides = {
        repository_id: plan.line.base_for(repository_id)
        for repository_id in repository_ids
        if plan.line.base_for(repository_id) != default_base
    }
    storage_overrides = {
        repository_id: plan.line.storage_for(repository_id)
        for repository_id in repository_ids
        if plan.line.storage_for(repository_id) != "linked-worktree"
    }
    return Line(
        id=plan.line.id,
        kind="line",
        branch=plan.line.branch,
        base=default_base,
        repositories=repository_ids,
        repository_bases=base_overrides,
        storage_modes=storage_overrides,
    )


def _ensure_line(config: Config, plan: JoinPlan) -> Line:
    expected = _line_from_blueprint(plan)
    try:
        existing = get_line(config, expected.id, "line")
    except DyroError:
        return create_line(
            config,
            line_id=expected.id,
            branch=expected.branch,
            base=expected.base,
            repositories=expected.repositories,
            repository_bases=expected.repository_bases,
            storage_modes=expected.storage_modes,
            kind="line",
        )
    if existing != expected:
        raise DyroError(f"现有开发线与 join 蓝图不一致：{expected.id}")
    return existing


def preflight_join_plan(plan: JoinPlan) -> None:
    if shutil.which("git") is None:
        raise DyroError("未找到 Git；请先安装 Git 后重试")
    if plan.root.is_symlink():
        raise DyroError(f"目标工作区不能是符号链接：{plan.root}")
    if plan.root.exists() and not plan.root.is_dir():
        raise DyroError(f"目标工作区不是目录：{plan.root}")
    if not plan.root.exists():
        return
    state_directory = plan.root / ".dyro"
    if state_directory.is_symlink():
        raise DyroError(f"状态目录不能是符号链接：{state_directory}")
    state = _read_join_state(plan.root / JOIN_STATE_FILE)
    if state is None:
        unexpected = [entry for entry in plan.root.iterdir() if entry != state_directory]
        if unexpected:
            raise DyroError(f"目标目录已存在且非空：{plan.root}")
        if state_directory.exists():
            unexpected_state = [
                entry for entry in state_directory.iterdir() if entry.name != "join.lock"
            ]
            if unexpected_state:
                raise DyroError(f"目标目录已存在且非空：{plan.root}")
        return
    if state["blueprint_sha256"] != plan.document.sha256 or state["line"] != plan.line.id:
        raise DyroError("目标目录的 join 蓝图或开发线与本次计划不一致")
    profile_path = plan.root / CONFIG_NAME
    if profile_path.is_symlink():
        raise DyroError(f"Profile 不能是符号链接：{profile_path}")
    if not profile_path.exists():
        if state["status"] == "complete":
            raise DyroError("join 状态为 complete，但 Profile 已缺失")
        return
    config = load(plan.root)
    _validate_profile(config, plan)
    _validate_workspace_paths(config, plan)
    for repository_id, repository in plan.document.blueprint.repositories.items():
        destination = plan.root / repository.path
        if destination.exists() or destination.is_symlink():
            _existing_anchor_matches(
                destination,
                repository,
                plan.line.base_for(repository_id),
            )
    try:
        existing_line = get_line(config, plan.line.id, "line")
    except DyroError:
        return
    if existing_line != _line_from_blueprint(plan):
        raise DyroError(f"现有开发线与 join 蓝图不一致：{plan.line.id}")


def apply_join_plan(plan: JoinPlan) -> Config:
    preflight_join_plan(plan)
    plan.root.mkdir(parents=True, exist_ok=True)
    with exclusive_lock(plan.root / ".dyro/join.lock", timeout_seconds=30):
        _prepare_join_state(plan)
        profile_path = plan.root / CONFIG_NAME
        if profile_path.is_symlink():
            raise DyroError(f"Profile 不能是符号链接：{profile_path}")
        if not profile_path.exists():
            atomic_write_text(profile_path, plan.profile)
        config = load(plan.root)
        _validate_profile(config, plan)
        _validate_workspace_paths(config, plan)
        for relative in (".dyro/tasks", ".dyro/lines", ".dyro/hotfixes", ".dyro/changes"):
            (plan.root / relative).mkdir(parents=True, exist_ok=True)
        for repository_id, repository in plan.document.blueprint.repositories.items():
            _clone_anchor(
                plan.root / repository.path,
                repository,
                plan.line.base_for(repository_id),
            )
        _ensure_line(config, plan)
        findings = doctor(config)
        failures = [finding for finding in findings if finding.startswith("FAIL")]
        if failures:
            raise DyroError("join 完成后 doctor 仍发现结构错误：\n" + "\n".join(failures))
        atomic_write_text(plan.root / JOIN_STATE_FILE, _join_state(plan, "complete"))
        return config

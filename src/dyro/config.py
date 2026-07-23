from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import tomllib
from typing import Any

from .errors import ValidationError


CONFIG_NAME = "dyro.toml"
TASKS_DIR = ".dyro/tasks"
LINES_DIR = ".dyro/lines"
HOTFIXES_DIR = ".dyro/hotfixes"
DECISIONS_FILE = ".dyro/decisions.toml"
LEDGER_FILE = ".dyro/ledger.jsonl"
CHANGESETS_DIR = ".dyro/changes"

SAFE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}$")


@dataclass(frozen=True)
class Layout:
    anchors: str = "repositories"
    lines: str = "versions"
    hotfixes: str = "hotfixes"
    tasks: str = "worktrees"


@dataclass(frozen=True)
class Repository:
    id: str
    path: str
    mount: str
    remote: str = ""
    verify: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True)
class Adapter:
    id: str
    launch: tuple[str, ...]
    read: tuple[str, ...]
    write: tuple[str, ...]


@dataclass(frozen=True)
class Policy:
    default_base: str = "main"
    task_branch_prefix: str = "task/"
    allow_push: bool = False
    require_clean_merge: bool = True
    require_external_signoff: bool = False
    execution_mode: str = "local"


@dataclass(frozen=True)
class Config:
    root: Path
    name: str
    layout: Layout
    repositories: dict[str, Repository]
    adapters: dict[str, Adapter]
    policy: Policy

    @property
    def task_specs_dir(self) -> Path:
        return self.root / TASKS_DIR

    @property
    def lines_state_dir(self) -> Path:
        return self.root / LINES_DIR

    @property
    def hotfixes_state_dir(self) -> Path:
        return self.root / HOTFIXES_DIR

    @property
    def decisions_file(self) -> Path:
        return self.root / DECISIONS_FILE

    @property
    def ledger_file(self) -> Path:
        return self.root / LEDGER_FILE

    @property
    def changesets_dir(self) -> Path:
        return self.root / CHANGESETS_DIR


def validate_id(value: str, label: str = "ID") -> str:
    if not SAFE_ID.fullmatch(value):
        raise ValidationError(f"{label} 只能包含字母、数字、点、下划线和连字符：{value!r}")
    return value


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{label} 必须是非空字符串")
    return value


def strict_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValidationError(f"{label} 必须是布尔值 true 或 false")
    return value


def _argv(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(x, str) and x for x in value):
        raise ValidationError(f"{label} 必须是非空字符串数组（argv），不接受 shell 字符串")
    return tuple(value)


def _relative(value: str, label: str) -> str:
    p = Path(value)
    if p.is_absolute() or ".." in p.parts:
        raise ValidationError(f"{label} 必须是工作区内的相对路径：{value!r}")
    return value


def find_root(start: Path) -> Path:
    here = start.resolve()
    for candidate in (here, *here.parents):
        if (candidate / CONFIG_NAME).is_file():
            return candidate
    raise ValidationError(f"从 {start} 起未找到 {CONFIG_NAME}；请先运行 dyro init")


def load(root: Path | None = None) -> Config:
    workspace = find_root(root or Path.cwd())
    config_file = workspace / CONFIG_NAME
    try:
        raw = tomllib.loads(config_file.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ValidationError(f"{config_file} TOML 格式错误：{exc}") from exc
    if raw.get("schema_version") != 1:
        raise ValidationError("仅支持 schema_version = 1")

    workspace_raw = raw.get("workspace", {})
    name = _string(workspace_raw.get("name"), "workspace.name")
    layout_raw = raw.get("layout", {})
    layout = Layout(
        anchors=_relative(str(layout_raw.get("anchors", "repositories")), "layout.anchors"),
        lines=_relative(str(layout_raw.get("lines", "versions")), "layout.lines"),
        hotfixes=_relative(str(layout_raw.get("hotfixes", "hotfixes")), "layout.hotfixes"),
        tasks=_relative(str(layout_raw.get("tasks", "worktrees")), "layout.tasks"),
    )
    policy_raw = raw.get("policy", {})
    policy = Policy(
        default_base=_string(policy_raw.get("default_base", "main"), "policy.default_base"),
        task_branch_prefix=_string(policy_raw.get("task_branch_prefix", "task/"), "policy.task_branch_prefix"),
        allow_push=strict_bool(policy_raw.get("allow_push", False), "policy.allow_push"),
        require_clean_merge=strict_bool(policy_raw.get("require_clean_merge", True), "policy.require_clean_merge"),
        require_external_signoff=strict_bool(
            policy_raw.get("require_external_signoff", False), "policy.require_external_signoff"
        ),
        execution_mode=_string(policy_raw.get("execution_mode", "local"), "policy.execution_mode"),
    )
    if policy.execution_mode not in ("local", "external"):
        raise ValidationError("policy.execution_mode 只能是 local 或 external")
    if not policy.require_clean_merge:
        raise ValidationError("policy.require_clean_merge 必须为 true；事务合并不允许脏工作区")

    repositories: dict[str, Repository] = {}
    for repo_id, entry in raw.get("repositories", {}).items():
        validate_id(repo_id, "repository id")
        if not isinstance(entry, dict):
            raise ValidationError(f"repositories.{repo_id} 必须是表")
        path = _relative(_string(entry.get("path"), f"repositories.{repo_id}.path"), "repository path")
        mount = _relative(_string(entry.get("mount", repo_id), f"repositories.{repo_id}.mount"), "repository mount")
        remote = entry.get("remote", "")
        if remote is None:
            remote = ""
        if not isinstance(remote, str):
            raise ValidationError(f"repositories.{repo_id}.remote 必须是字符串")
        verify = tuple(_argv(item, f"repositories.{repo_id}.verify") for item in entry.get("verify", []))
        repositories[repo_id] = Repository(repo_id, path, mount, remote, verify)
    if not repositories:
        raise ValidationError("至少配置一个 repositories.<id>")

    adapters: dict[str, Adapter] = {}
    for adapter_id, entry in raw.get("adapters", {}).items():
        validate_id(adapter_id, "adapter id")
        if not isinstance(entry, dict):
            raise ValidationError(f"adapters.{adapter_id} 必须是表")
        read = _argv(entry.get("read", entry.get("command")), f"adapters.{adapter_id}.read")
        write = _argv(entry.get("write", entry.get("command")), f"adapters.{adapter_id}.write")
        launch = _argv(entry.get("launch", entry.get("command", entry.get("write"))), f"adapters.{adapter_id}.launch")
        adapters[adapter_id] = Adapter(adapter_id, launch, read, write)
    return Config(workspace, name, layout, repositories, adapters, policy)


def expand_argv(argv: tuple[str, ...], **values: str | Path) -> tuple[str, ...]:
    allowed = {key: str(value) for key, value in values.items()}
    try:
        return tuple(part.format(**allowed) for part in argv)
    except KeyError as exc:
        raise ValidationError(f"命令模板引用未知占位符：{exc.args[0]}") from exc

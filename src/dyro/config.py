from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
import tomllib
from typing import Any

from .errors import ValidationError
from .read_limits import ReadBudget


CONFIG_NAME = "dyro.toml"
TASKS_DIR = ".dyro/tasks"
LINES_DIR = ".dyro/lines"
HOTFIXES_DIR = ".dyro/hotfixes"
DECISIONS_FILE = ".dyro/decisions.toml"
LEDGER_FILE = ".dyro/ledger.jsonl"
CHANGESETS_DIR = ".dyro/changes"
OBJECTIVES_DIR = ".dyro/objectives"

SAFE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}$")
PUSH_DISABLED_NOTE = "Dyro 不会 push；普通 `git push` 仍可用。"


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
    require_signed_execution: bool = False
    require_signed_review: bool = False
    require_signed_signoff: bool = False
    execution_mode: str = "local"
    allow_unattended_execute: bool = False
    allow_unattended_review: bool = False
    allow_unattended_merge: bool = False


@dataclass(frozen=True)
class Config:
    root: Path
    name: str
    layout: Layout
    repositories: dict[str, Repository]
    adapters: dict[str, Adapter]
    policy: Policy
    recommended_tool: str = ""
    capabilities: dict[str, object] = field(default_factory=dict)
    max_provider_usage: int | None = None

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

    @property
    def objectives_dir(self) -> Path:
        return self.root / OBJECTIVES_DIR


@dataclass(frozen=True)
class LoadedProfile:
    config: Config
    root: Path
    profile_bytes: bytes


def external_security_errors(policy: Policy) -> tuple[str, ...]:
    """Return the explicit migration requirements for an external Profile."""
    if policy.execution_mode != "external":
        return ()
    missing: list[str] = []
    if not getattr(policy, "require_signed_execution", True):
        missing.append("policy.require_signed_execution = true")
    if not getattr(policy, "require_signed_review", True):
        missing.append("policy.require_signed_review = true")
    if getattr(policy, "require_external_signoff", False) and not getattr(
        policy, "require_signed_signoff", True
    ):
        missing.append("policy.require_signed_signoff = true")
    return tuple(missing)




def push_disclosure(policy: Policy) -> str:
    """User-facing note when Dyro itself will not push."""
    if policy.allow_push:
        return ""
    return PUSH_DISABLED_NOTE


def push_policy_fields(policy: Policy) -> dict[str, object]:
    if policy.allow_push:
        return {"allow_push": True}
    return {"allow_push": False, "push_note": PUSH_DISABLED_NOTE}


def validate_id(value: str, label: str = "ID") -> str:
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise ValidationError(
            f"{label} 只能包含字母、数字、点、下划线和连字符：{value!r}"
        )
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
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(x, str) and x for x in value)
    ):
        raise ValidationError(
            f"{label} 必须是非空字符串数组（argv），不接受 shell 字符串"
        )
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


def _parse_config(workspace: Path, profile_bytes: bytes) -> Config:
    config_file = workspace / CONFIG_NAME
    try:
        raw = tomllib.loads(profile_bytes.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError, RecursionError) as exc:
        raise ValidationError(f"{config_file} TOML 格式错误：{exc}") from exc
    if raw.get("schema_version") != 1:
        raise ValidationError("仅支持 schema_version = 1")

    def table(name: str) -> dict[str, Any]:
        value = raw.get(name, {})
        if not isinstance(value, dict):
            raise ValidationError(f"{name} 必须是表")
        return value

    workspace_raw = table("workspace")
    name = _string(workspace_raw.get("name"), "workspace.name")
    recommended_tool_raw = workspace_raw.get("recommended_tool", "")
    if not isinstance(recommended_tool_raw, str):
        raise ValidationError("workspace.recommended_tool 必须是字符串")
    recommended_tool = recommended_tool_raw.strip()
    if recommended_tool:
        validate_id(recommended_tool, "workspace.recommended_tool")
    max_provider_usage_raw = workspace_raw.get("max_provider_usage")
    if max_provider_usage_raw is None:
        max_provider_usage = None
    elif (
        isinstance(max_provider_usage_raw, bool)
        or not isinstance(max_provider_usage_raw, int)
        or max_provider_usage_raw < 1
    ):
        raise ValidationError("workspace.max_provider_usage 必须是正整数")
    else:
        max_provider_usage = max_provider_usage_raw
    layout_raw = table("layout")
    layout = Layout(
        anchors=_relative(
            _string(layout_raw.get("anchors", "repositories"), "layout.anchors"),
            "layout.anchors",
        ),
        lines=_relative(
            _string(layout_raw.get("lines", "versions"), "layout.lines"),
            "layout.lines",
        ),
        hotfixes=_relative(
            _string(layout_raw.get("hotfixes", "hotfixes"), "layout.hotfixes"),
            "layout.hotfixes",
        ),
        tasks=_relative(
            _string(layout_raw.get("tasks", "worktrees"), "layout.tasks"),
            "layout.tasks",
        ),
    )
    policy_raw = table("policy")
    policy = Policy(
        default_base=_string(
            policy_raw.get("default_base", "main"), "policy.default_base"
        ),
        task_branch_prefix=_string(
            policy_raw.get("task_branch_prefix", "task/"), "policy.task_branch_prefix"
        ),
        allow_push=strict_bool(
            policy_raw.get("allow_push", False), "policy.allow_push"
        ),
        require_clean_merge=strict_bool(
            policy_raw.get("require_clean_merge", True), "policy.require_clean_merge"
        ),
        require_external_signoff=strict_bool(
            policy_raw.get("require_external_signoff", False),
            "policy.require_external_signoff",
        ),
        require_signed_execution=strict_bool(
            policy_raw.get("require_signed_execution", False),
            "policy.require_signed_execution",
        ),
        require_signed_review=strict_bool(
            policy_raw.get("require_signed_review", False),
            "policy.require_signed_review",
        ),
        require_signed_signoff=strict_bool(
            policy_raw.get("require_signed_signoff", False),
            "policy.require_signed_signoff",
        ),
        execution_mode=_string(
            policy_raw.get("execution_mode", "local"), "policy.execution_mode"
        ),
        allow_unattended_execute=strict_bool(
            policy_raw.get("allow_unattended_execute", False),
            "policy.allow_unattended_execute",
        ),
        allow_unattended_review=strict_bool(
            policy_raw.get("allow_unattended_review", False),
            "policy.allow_unattended_review",
        ),
        allow_unattended_merge=strict_bool(
            policy_raw.get("allow_unattended_merge", False),
            "policy.allow_unattended_merge",
        ),
    )
    if policy.execution_mode not in ("local", "external"):
        raise ValidationError("policy.execution_mode 只能是 local 或 external")
    if not policy.require_clean_merge:
        raise ValidationError(
            "policy.require_clean_merge 必须为 true；事务合并不允许脏工作区"
        )
    if (
        policy.require_signed_execution
        or policy.require_signed_review
        or policy.require_signed_signoff
    ) and policy.execution_mode != "external":
        raise ValidationError("require_signed_* 策略仅适用于 execution_mode = external")
    if policy.require_signed_signoff and not policy.require_external_signoff:
        raise ValidationError(
            "require_signed_signoff = true 要求 require_external_signoff = true"
        )

    repositories: dict[str, Repository] = {}
    for repo_id, entry in table("repositories").items():
        validate_id(repo_id, "repository id")
        if not isinstance(entry, dict):
            raise ValidationError(f"repositories.{repo_id} 必须是表")
        path = _relative(
            _string(entry.get("path"), f"repositories.{repo_id}.path"),
            "repository path",
        )
        mount = _relative(
            _string(entry.get("mount", repo_id), f"repositories.{repo_id}.mount"),
            "repository mount",
        )
        remote = entry.get("remote", "")
        if remote is None:
            remote = ""
        if not isinstance(remote, str):
            raise ValidationError(f"repositories.{repo_id}.remote 必须是字符串")
        verify_raw = entry.get("verify", [])
        if not isinstance(verify_raw, list):
            raise ValidationError(
                f"repositories.{repo_id}.verify 必须是 argv 数组的数组"
            )
        verify = tuple(
            _argv(item, f"repositories.{repo_id}.verify") for item in verify_raw
        )
        repositories[repo_id] = Repository(repo_id, path, mount, remote, verify)
    if not repositories:
        raise ValidationError("至少配置一个 repositories.<id>")

    adapters: dict[str, Adapter] = {}
    for adapter_id, entry in table("adapters").items():
        validate_id(adapter_id, "adapter id")
        if not isinstance(entry, dict):
            raise ValidationError(f"adapters.{adapter_id} 必须是表")
        read = _argv(
            entry.get("read", entry.get("command")), f"adapters.{adapter_id}.read"
        )
        write = _argv(
            entry.get("write", entry.get("command")), f"adapters.{adapter_id}.write"
        )
        launch = _argv(
            entry.get("launch", entry.get("command", entry.get("write"))),
            f"adapters.{adapter_id}.launch",
        )
        adapters[adapter_id] = Adapter(adapter_id, launch, read, write)
    from .capability.cards import merge_capability_plane, parse_capability_tables

    cards = parse_capability_tables(raw.get("capabilities"))
    adapters, cards = merge_capability_plane(adapters, cards)
    return Config(
        workspace,
        name,
        layout,
        repositories,
        adapters,
        policy,
        recommended_tool,
        cards,
        max_provider_usage,
    )


def load(root: Path | None = None) -> Config:
    workspace = find_root(root or Path.cwd())
    return _parse_config(workspace, (workspace / CONFIG_NAME).read_bytes())


def load_profile_exact(root: Path, budget: ReadBudget) -> LoadedProfile:
    """Load exactly ``root/dyro.toml`` from the same bounded bytes that are parsed."""

    try:
        canonical_root = root.absolute().resolve(strict=False)
    except PermissionError:
        raise
    except (OSError, RuntimeError) as exc:
        raise ValidationError("Profile root 无法解析") from exc
    profile_bytes = budget.read_regular_bytes_at(
        root=canonical_root,
        directory=canonical_root,
        name=CONFIG_NAME,
        maximum_bytes=budget.limits.profile_bytes,
        label="dyro.toml",
    )
    config = _parse_config(canonical_root, profile_bytes)
    validate_id(config.name, "workspace name")
    return LoadedProfile(
        config=config,
        root=canonical_root,
        profile_bytes=profile_bytes,
    )


def expand_argv(argv: tuple[str, ...], **values: str | Path) -> tuple[str, ...]:
    allowed = {key: str(value) for key, value in values.items()}
    try:
        return tuple(part.format(**allowed) for part in argv)
    except KeyError as exc:
        raise ValidationError(f"命令模板引用未知占位符：{exc.args[0]}") from exc

from __future__ import annotations

from dataclasses import dataclass, replace
import difflib
import json
import os
from pathlib import Path
from typing import Callable

from .config import Config, load, validate_id
from .errors import DyroError, ValidationError
from .read_limits import ReadBudget, ReadLimitCode, ReadLimitError
from .state import atomic_write_text, exclusive_lock


REGISTRY_SCHEMA_VERSION = 1
REGISTRY_FILE = "workspaces.json"
REGISTRY_LOCK = "workspaces.lock"
TARGET_KINDS = frozenset({"line", "hotfix", "task"})


@dataclass(frozen=True)
class WorkspaceRecord:
    name: str
    root: Path
    last_kind: str = ""
    last_target: str = ""
    last_agent: str = ""


@dataclass(frozen=True)
class WorkspaceRegistry:
    default: str = ""
    workspaces: tuple[WorkspaceRecord, ...] = ()


@dataclass(frozen=True)
class WorkspaceRegistrationPlan:
    """A read-only preview of adding one workspace to the global entry list."""

    name: str
    root: Path
    already_registered: bool
    becomes_default: bool



def looks_like_workspace_path(value: str) -> bool:
    if not isinstance(value, str) or not value:
        return False
    return Path(value).is_absolute() or "/" in value or "\\" in value


def workspace_path_as_alias_error(value: str) -> ValidationError:
    return ValidationError(
        f"--workspace 接受已登记的工作区别名，不能是路径：{value}；请改用 --root"
    )


def _alias_fold(name: str) -> str:
    return name.casefold()


def workspace_alias_matches(
    workspaces: tuple[WorkspaceRecord, ...], name: str
) -> tuple[WorkspaceRecord, ...]:
    """Return registered records whose aliases fold equal to ``name``."""
    key = _alias_fold(name)
    return tuple(record for record in workspaces if _alias_fold(record.name) == key)


def alias_fold_collides(name: str, names: tuple[str, ...]) -> bool:
    """True when two or more registered aliases fold equal to ``name``."""
    key = _alias_fold(name)
    return sum(1 for item in names if _alias_fold(item) == key) > 1


def unique_registered_alias(name: str, names: tuple[str, ...]) -> str | None:
    """Return the sole registered spelling that folds equal to ``name``."""
    key = _alias_fold(name)
    matches = tuple(item for item in names if _alias_fold(item) == key)
    if len(matches) == 1:
        return matches[0]
    return None


def same_workspace_root(left: Path | None, right: Path | None) -> bool:
    """True when both paths resolve to the same workspace root."""
    if left is None or right is None:
        return False
    try:
        return Path(left).resolve() == Path(right).resolve()
    except OSError:
        return False


def workspace_alias_retargets_root(
    alias: str,
    root: Path,
    workspaces: tuple[WorkspaceRecord, ...] | None = None,
) -> bool:
    """True when ``--workspace alias`` uniquely fold-resolves to another root.

    A miss or fold collision is not a working selector for a different root.
    Registry read failures cannot prove the selector stays on this root.
    """
    if not isinstance(alias, str) or not alias:
        return False
    try:
        records = (
            workspaces if workspaces is not None else load_registry().workspaces
        )
    except (DyroError, ValidationError, OSError, TypeError, AttributeError):
        return True
    matches = workspace_alias_matches(records, alias)
    if len(matches) != 1:
        return False
    return not same_workspace_root(matches[0].root, root)


class WorkspaceAliasCollisionError(DyroError):
    """More than one registered alias folds to the same lookup key."""


def colliding_workspace_aliases_error(
    name: str, colliding: tuple[str, ...]
) -> WorkspaceAliasCollisionError:
    listed = "、".join(colliding)
    return WorkspaceAliasCollisionError(
        f"工作区别名大小写冲突：{name} 同时匹配 {listed}"
    )


def select_workspace_record(
    workspaces: tuple[WorkspaceRecord, ...],
    name: str,
    *,
    exact_on_collision: bool = False,
) -> WorkspaceRecord:
    """Resolve one registered alias.

    A unique case-insensitive match returns the canonical registered record.
    Two or more aliases that fold equal fail closed unless
    ``exact_on_collision`` is set and ``name`` equals one registered spelling.
    A total miss keeps the existing unregistered suggestion.
    """
    matches = workspace_alias_matches(workspaces, name)
    if len(matches) == 1:
        return matches[0]
    if matches:
        if exact_on_collision:
            exact = next((record for record in matches if record.name == name), None)
            if exact is not None:
                return exact
        raise colliding_workspace_aliases_error(
            name, tuple(record.name for record in matches)
        )
    raise unregistered_workspace_error(name, tuple(record.name for record in workspaces))


def _close_workspace_names(name: str, names: tuple[str, ...]) -> tuple[str, ...]:
    exact_ci = tuple(
        item for item in names if _alias_fold(item) == _alias_fold(name) and item != name
    )
    if exact_ci:
        return exact_ci
    return tuple(difflib.get_close_matches(name, names, n=5, cutoff=0.4))


def unregistered_workspace_error(name: str, names: tuple[str, ...]) -> DyroError:
    close = _close_workspace_names(name, names)
    if close:
        return DyroError(f"未登记工作区：{name}；你是不是指 {'、'.join(close)}？")
    if names:
        return DyroError(f"未登记工作区：{name}；已登记：{'、'.join(names)}")
    return DyroError(f"未登记工作区：{name}；运行 dyro workspace list 查看可用项目")


def registry_home() -> Path:
    override = os.environ.get("DYRO_HOME", "").strip()
    if override:
        return Path(override).expanduser().absolute()
    if os.name == "nt":
        base = os.environ.get("APPDATA", "").strip()
        if base:
            return Path(base).expanduser().absolute() / "Dyro"
    base = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if base:
        return Path(base).expanduser().absolute() / "dyro"
    return Path.home() / ".config" / "dyro"


def _registry_path() -> Path:
    return registry_home() / REGISTRY_FILE


def _registry_lock_path() -> Path:
    return registry_home() / REGISTRY_LOCK


def _record_from_json(raw: object, *, index: int, expand_home: bool = True) -> WorkspaceRecord:
    if not isinstance(raw, dict):
        raise ValidationError(f"全局工作区记录第 {index} 项必须是对象")
    expected = {"name", "root", "last_kind", "last_target", "last_agent"}
    unknown = set(raw) - expected
    if unknown:
        raise ValidationError(
            f"全局工作区记录包含未知字段：{', '.join(sorted(unknown))}"
        )
    name_raw = raw.get("name")
    if not isinstance(name_raw, str) or not name_raw:
        raise ValidationError(f"全局工作区记录第 {index} 项的别名必须是非空字符串")
    name = validate_id(name_raw, "工作区别名")
    root_raw = raw.get("root")
    if not isinstance(root_raw, str) or not root_raw or "\x00" in root_raw:
        raise ValidationError(f"全局工作区 {name} 的路径无效")
    if not expand_home and root_raw.startswith("~"):
        raise ValidationError(f"全局工作区 {name} 的路径禁止 home expansion")
    root = Path(root_raw).expanduser() if expand_home else Path(os.path.normpath(root_raw))
    if not root.is_absolute():
        raise ValidationError(f"全局工作区 {name} 必须使用绝对路径")
    if expand_home:
        root = root.resolve()
    last_kind = raw.get("last_kind", "")
    last_target = raw.get("last_target", "")
    last_agent = raw.get("last_agent", "")
    if not all(
        isinstance(value, str) for value in (last_kind, last_target, last_agent)
    ):
        raise ValidationError(f"全局工作区 {name} 的最近使用记录无效")
    if last_kind and last_kind not in TARGET_KINDS:
        raise ValidationError(f"全局工作区 {name} 的最近目标类型无效：{last_kind}")
    if bool(last_kind) != bool(last_target):
        raise ValidationError(f"全局工作区 {name} 的最近目标记录不完整")
    if last_target:
        validate_id(last_target, "最近目标 ID")
    if last_agent:
        validate_id(last_agent, "最近 Agent ID")
    return WorkspaceRecord(name, root, last_kind, last_target, last_agent)


def _registry_from_json(
    raw: object,
    *,
    path: Path,
    expand_home: bool,
    maximum_records: int | None = None,
) -> WorkspaceRegistry:
    if (
        not isinstance(raw, dict)
        or raw.get("schema_version") != REGISTRY_SCHEMA_VERSION
    ):
        raise ValidationError(f"全局工作区记录版本无效：{path}")
    if set(raw) - {"schema_version", "default", "workspaces"}:
        raise ValidationError(f"全局工作区记录包含未知顶层字段：{path}")
    default = raw.get("default", "")
    entries = raw.get("workspaces", [])
    if not isinstance(default, str) or not isinstance(entries, list):
        raise ValidationError(f"全局工作区记录结构无效：{path}")
    if maximum_records is not None and len(entries) > maximum_records:
        raise ReadLimitError(
            ReadLimitCode.RECORD_LIMIT_EXCEEDED,
            "Global workspace registry record limit exceeded",
        )
    workspaces = tuple(
        _record_from_json(entry, index=index, expand_home=expand_home)
        for index, entry in enumerate(entries, start=1)
    )
    names = [record.name for record in workspaces]
    roots = [record.root for record in workspaces]
    if len(set(names)) != len(names):
        raise ValidationError("全局工作区记录包含重复别名")
    if len(set(roots)) != len(roots):
        raise ValidationError("全局工作区记录包含重复路径")
    if default and default not in names:
        raise ValidationError(f"全局默认工作区不存在：{default}")
    return WorkspaceRegistry(default, workspaces)


def load_registry() -> WorkspaceRegistry:
    return load_registry_from_home(registry_home())


def load_registry_from_home(home: Path) -> WorkspaceRegistry:
    """Load the global workspace list from an explicit Dyro home."""
    path = Path(home) / REGISTRY_FILE
    if not path.exists() and not path.is_symlink():
        return WorkspaceRegistry()
    if path.is_symlink() or not path.is_file():
        raise ValidationError(f"全局工作区记录不是安全的普通文件：{path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValidationError(
            f"无法读取全局工作区记录 {path}；请修复或备份后移走该文件"
        ) from exc
    return _registry_from_json(raw, path=path, expand_home=True)


def load_registry_bounded(budget: ReadBudget) -> WorkspaceRegistry:
    """Load the registry without locks, writes, home expansion, or unbounded I/O."""
    path = _registry_path()
    try:
        path.lstat()
    except FileNotFoundError:
        return WorkspaceRegistry()
    try:
        canonical_home = path.parent.resolve(strict=True)
        content = budget.read_regular_bytes_at(
            root=canonical_home,
            directory=canonical_home,
            name=path.name,
            maximum_bytes=budget.limits.registry_bytes,
            label="global workspace registry",
        )
        raw = json.loads(content.decode("utf-8"))
    except ReadLimitError:
        raise
    except PermissionError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValidationError("无法读取全局工作区记录") from exc
    return _registry_from_json(
        raw,
        path=path,
        expand_home=False,
        maximum_records=budget.limits.registry_records,
    )


def _registry_json(registry: WorkspaceRegistry) -> str:
    payload = {
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "default": registry.default,
        "workspaces": [
            {
                "name": record.name,
                "root": str(record.root),
                "last_kind": record.last_kind,
                "last_target": record.last_target,
                "last_agent": record.last_agent,
            }
            for record in registry.workspaces
        ],
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _update_registry(
    update: Callable[[WorkspaceRegistry], WorkspaceRegistry],
) -> WorkspaceRegistry:
    with exclusive_lock(_registry_lock_path()):
        current = load_registry()
        updated = update(current)
        atomic_write_text(_registry_path(), _registry_json(updated))
        return updated


def _profile_root(path: str | Path) -> Config:
    candidate = Path(path).expanduser().absolute()
    try:
        return load(candidate)
    except (DyroError, ValidationError) as exc:
        raise DyroError(
            f"这里不是可用的 Dyro 工作区：{candidate}；请确认其中存在有效 dyro.toml"
        ) from exc


def preview_workspace_registration(
    path: str | Path, *, name: str, make_default: bool = False
) -> WorkspaceRegistrationPlan:
    """Validate and describe a future global workspace registration without writing."""

    root = Path(path).expanduser().absolute().resolve()
    alias = validate_id(name, "工作区别名")
    registry = load_registry()
    same_name = next(
        (item for item in registry.workspaces if item.name == alias), None
    )
    same_root = next(
        (item for item in registry.workspaces if item.root == root), None
    )
    if same_name is not None and same_name.root != root:
        raise DyroError(f"工作区别名 {alias} 已指向 {same_name.root}")
    if same_root is not None and same_root.name != alias:
        raise DyroError(f"工作区路径已经登记为 {same_root.name}：{root}")
    return WorkspaceRegistrationPlan(
        name=alias,
        root=root,
        already_registered=same_name is not None or same_root is not None,
        becomes_default=make_default or not registry.default,
    )


def add_workspace(
    path: str | Path, *, name: str | None = None, make_default: bool = False
) -> WorkspaceRecord:
    config = _profile_root(path)
    alias = validate_id(name or config.name, "工作区别名")
    root = config.root.resolve()
    record = WorkspaceRecord(alias, root)

    def update(current: WorkspaceRegistry) -> WorkspaceRegistry:
        same_name = next(
            (item for item in current.workspaces if item.name == alias), None
        )
        same_root = next(
            (item for item in current.workspaces if item.root == root), None
        )
        if same_name is not None and same_name.root != root:
            raise DyroError(f"工作区别名 {alias} 已指向 {same_name.root}")
        if same_root is not None and same_root.name != alias:
            raise DyroError(f"工作区路径已经登记为 {same_root.name}：{root}")
        selected = same_name or same_root or record
        remaining = tuple(
            item for item in current.workspaces if item.name != selected.name
        )
        default = alias if make_default or not current.default else current.default
        return WorkspaceRegistry(default, (selected, *remaining))

    updated = _update_registry(update)
    return next(item for item in updated.workspaces if item.name == alias)


def ensure_workspace(path: str | Path) -> WorkspaceRecord:
    config = _profile_root(path)
    root = config.root.resolve()
    registry = load_registry()
    existing = next((item for item in registry.workspaces if item.root == root), None)
    if existing is not None:
        return existing
    names = {item.name for item in registry.workspaces}
    alias = config.name
    suffix = 2
    while alias in names:
        alias = f"{config.name[:75]}-{suffix}"
        suffix += 1
    return add_workspace(root, name=alias, make_default=not registry.default)


def get_workspace(name: str, *, exact_on_collision: bool = False) -> WorkspaceRecord:
    if looks_like_workspace_path(name):
        raise workspace_path_as_alias_error(name)
    validate_id(name, "工作区别名")
    registry = load_registry()
    return select_workspace_record(
        registry.workspaces, name, exact_on_collision=exact_on_collision
    )


def set_default_workspace(name: str) -> None:
    validate_id(name, "工作区别名")

    def update(current: WorkspaceRegistry) -> WorkspaceRegistry:
        selected = select_workspace_record(current.workspaces, name)
        return replace(current, default=selected.name)

    _update_registry(update)


def remove_workspace(name: str) -> None:
    validate_id(name, "工作区别名")

    def update(current: WorkspaceRegistry) -> WorkspaceRegistry:
        selected = select_workspace_record(
            current.workspaces, name, exact_on_collision=True
        )
        remaining = tuple(
            record for record in current.workspaces if record.name != selected.name
        )
        default = (
            current.default
            if current.default != selected.name
            else (remaining[0].name if remaining else "")
        )
        return WorkspaceRegistry(default, remaining)

    _update_registry(update)


def mark_workspace_used(
    name: str,
    *,
    target_kind: str,
    target_id: str,
    agent: str = "",
) -> None:
    validate_id(name, "工作区别名")
    if target_kind not in TARGET_KINDS:
        raise ValidationError(f"最近目标类型无效：{target_kind}")
    validate_id(target_id, "最近目标 ID")
    if agent:
        validate_id(agent, "最近 Agent ID")

    def update(current: WorkspaceRegistry) -> WorkspaceRegistry:
        try:
            selected = next(
                record for record in current.workspaces if record.name == name
            )
        except StopIteration as exc:
            raise DyroError(f"未登记工作区：{name}") from exc
        selected = replace(
            selected, last_kind=target_kind, last_target=target_id, last_agent=agent
        )
        remaining = tuple(
            record for record in current.workspaces if record.name != name
        )
        return WorkspaceRegistry(current.default or name, (selected, *remaining))

    _update_registry(update)

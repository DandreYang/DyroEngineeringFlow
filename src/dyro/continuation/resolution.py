"""Deterministic workspace, line, and Objective resolution without side effects."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
import stat
import sys
from typing import Callable

from ..config import CONFIG_NAME, Config, LoadedProfile, load, load_profile_exact, validate_id
from ..errors import DyroError, ValidationError
from ..hub import WorkspaceRecord, load_registry, load_registry_bounded
from ..read_limits import ReadBudget, ReadLimitCode, ReadLimitError
from ..tasks import list_tasks, worktree_root
from ..workspace import Line, get_line, line_root, list_lines
from .store import StoredObjective, get_objective, list_objectives


Chooser = Callable[[str, tuple[str, ...]], str]


class WorkspaceResolutionSource(str, Enum):
    EXPLICIT = "explicit"
    LOCAL = "local"
    DEFAULT = "default"
    UNIQUE = "unique"


class WorkspaceResolutionFailure(str, Enum):
    LOCAL_PROFILE_INVALID = "LOCAL_PROFILE_INVALID"
    REGISTRY_INVALID = "REGISTRY_INVALID"
    WORKSPACE_NOT_REGISTERED = "WORKSPACE_NOT_REGISTERED"
    REGISTERED_ROOT_STALE = "REGISTERED_ROOT_STALE"
    HOST_READ_PERMISSION_REQUIRED = "HOST_READ_PERMISSION_REQUIRED"
    AMBIGUOUS_WORKSPACE = "AMBIGUOUS_WORKSPACE"
    WORKSPACE_NOT_FOUND = "WORKSPACE_NOT_FOUND"


class WorkspaceResolutionError(DyroError):
    def __init__(self, code: WorkspaceResolutionFailure) -> None:
        super().__init__(code.value)
        self.code = code


@dataclass(frozen=True)
class ResolvedWorkspace:
    profile: LoadedProfile
    source: WorkspaceResolutionSource
    registry_alias: str | None


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _choose(label: str, values: tuple[str, ...], chooser: Chooser | None, interactive: bool) -> str:
    if len(values) == 1:
        return values[0]
    if not interactive:
        raise DyroError(f"{label} 存在多个候选；非交互模式必须显式指定：{', '.join(values)}")
    if chooser is not None:
        selected = chooser(label, values)
        if selected in values:
            return selected
        raise DyroError(f"{label} 选择无效：{selected}")
    raise DyroError(f"{label} 存在多个候选；请显式指定：{', '.join(values)}")


def _usable_records() -> tuple[WorkspaceRecord, ...]:
    records: list[WorkspaceRecord] = []
    for record in load_registry().workspaces:
        try:
            load(record.root)
        except (DyroError, ValidationError):
            continue
        records.append(record)
    return tuple(records)


def _local_profile_root(location: Path) -> Path | None:
    """Locate a local Profile without treating a broken profile as absence."""
    here = location.resolve()
    for candidate in (here, *here.parents):
        profile = candidate / CONFIG_NAME
        if not profile.exists() and not profile.is_symlink():
            continue
        if profile.is_symlink() or not profile.is_file():
            raise ValidationError(f"本地 Profile 配置必须是安全的普通文件：{profile}")
        return candidate
    return None


def resolve_workspace(
    *,
    start: Path | None = None,
    workspace: str | None = None,
    interactive: bool | None = None,
    chooser: Chooser | None = None,
) -> Config:
    """Resolve explicit selector, local Profile, then registered default/unique Profile."""
    if workspace:
        registry = load_registry()
        matches = tuple(record for record in registry.workspaces if record.name == workspace)
        if len(matches) != 1:
            raise DyroError(f"未登记工作区：{workspace}")
        return load(matches[0].root)
    location = (start or Path.cwd()).expanduser()
    local_root = _local_profile_root(location)
    if local_root is not None:
        # A discovered local Profile is authoritative.  In particular, never
        # reinterpret a malformed workspace as an invitation to mutate the
        # registry default somewhere else.
        return load(local_root)
    registry = load_registry()
    records = _usable_records()
    default = next((record for record in records if record.name == registry.default), None)
    if default is not None:
        return load(default.root)
    selected = _choose(
        "工作区",
        tuple(record.name for record in records),
        chooser,
        _interactive() if interactive is None else interactive,
    )
    return load(next(record.root for record in records if record.name == selected))


def _readonly_location(start: str | Path | None, cwd: Path) -> Path:
    if not cwd.is_absolute():
        raise ValidationError("Bridge cwd 必须是绝对路径")
    raw = "." if start is None else str(start)
    if not raw or raw.startswith("~") or "\x00" in raw:
        raise ValidationError("Bridge start 路径无效")
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = cwd / candidate
    try:
        location = candidate.resolve(strict=False)
        if location.is_file():
            return location.parent
        return location
    except PermissionError as exc:
        raise WorkspaceResolutionError(
            WorkspaceResolutionFailure.HOST_READ_PERMISSION_REQUIRED
        ) from exc
    except (OSError, RuntimeError) as exc:
        raise ValidationError("Bridge start 路径无法解析") from exc


def _readonly_local_root(location: Path) -> Path | None:
    for candidate in (location, *location.parents):
        profile = candidate / CONFIG_NAME
        try:
            info = profile.lstat()
        except FileNotFoundError:
            continue
        except PermissionError as exc:
            raise WorkspaceResolutionError(
                WorkspaceResolutionFailure.HOST_READ_PERMISSION_REQUIRED
            ) from exc
        except OSError as exc:
            raise WorkspaceResolutionError(
                WorkspaceResolutionFailure.LOCAL_PROFILE_INVALID
            ) from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise WorkspaceResolutionError(
                WorkspaceResolutionFailure.LOCAL_PROFILE_INVALID
            )
        return candidate
    return None


def _bounded_registry(budget: ReadBudget):
    try:
        return load_registry_bounded(budget)
    except ReadLimitError as exc:
        if exc.code is ReadLimitCode.UNSAFE_FILE:
            raise WorkspaceResolutionError(
                WorkspaceResolutionFailure.REGISTRY_INVALID
            ) from exc
        raise
    except PermissionError as exc:
        raise WorkspaceResolutionError(
            WorkspaceResolutionFailure.HOST_READ_PERMISSION_REQUIRED
        ) from exc
    except (OSError, ValidationError) as exc:
        raise WorkspaceResolutionError(
            WorkspaceResolutionFailure.REGISTRY_INVALID
        ) from exc


def _registered_profile(record: WorkspaceRecord, budget: ReadBudget) -> LoadedProfile:
    try:
        return load_profile_exact(record.root, budget)
    except ReadLimitError as exc:
        if exc.code is ReadLimitCode.UNSAFE_FILE:
            raise WorkspaceResolutionError(
                WorkspaceResolutionFailure.REGISTERED_ROOT_STALE
            ) from exc
        raise
    except PermissionError as exc:
        raise WorkspaceResolutionError(
            WorkspaceResolutionFailure.HOST_READ_PERMISSION_REQUIRED
        ) from exc
    except (OSError, ValidationError) as exc:
        raise WorkspaceResolutionError(
            WorkspaceResolutionFailure.REGISTERED_ROOT_STALE
        ) from exc


def resolve_workspace_readonly(
    *,
    start: str | Path | None,
    workspace: str | None,
    cwd: Path,
    budget: ReadBudget,
) -> ResolvedWorkspace:
    """Resolve one workspace without interaction, writes, recovery, or fallback drift."""
    if workspace is not None:
        validate_id(workspace, "工作区别名")
        registry = _bounded_registry(budget)
        matches = tuple(item for item in registry.workspaces if item.name == workspace)
        if len(matches) != 1:
            raise WorkspaceResolutionError(
                WorkspaceResolutionFailure.WORKSPACE_NOT_REGISTERED
            )
        record = matches[0]
        return ResolvedWorkspace(
            _registered_profile(record, budget),
            WorkspaceResolutionSource.EXPLICIT,
            record.name,
        )

    location = _readonly_location(start, cwd)
    local_root = _readonly_local_root(location)
    if local_root is not None:
        try:
            profile = load_profile_exact(local_root, budget)
        except ReadLimitError as exc:
            if exc.code is ReadLimitCode.UNSAFE_FILE:
                raise WorkspaceResolutionError(
                    WorkspaceResolutionFailure.LOCAL_PROFILE_INVALID
                ) from exc
            raise
        except PermissionError as exc:
            raise WorkspaceResolutionError(
                WorkspaceResolutionFailure.HOST_READ_PERMISSION_REQUIRED
            ) from exc
        except (OSError, ValidationError) as exc:
            raise WorkspaceResolutionError(
                WorkspaceResolutionFailure.LOCAL_PROFILE_INVALID
            ) from exc
        return ResolvedWorkspace(profile, WorkspaceResolutionSource.LOCAL, None)

    registry = _bounded_registry(budget)
    if registry.default:
        record = next(item for item in registry.workspaces if item.name == registry.default)
        return ResolvedWorkspace(
            _registered_profile(record, budget),
            WorkspaceResolutionSource.DEFAULT,
            record.name,
        )

    usable: list[tuple[WorkspaceRecord, LoadedProfile]] = []
    for record in registry.workspaces:
        try:
            profile = _registered_profile(record, budget)
        except WorkspaceResolutionError as exc:
            if exc.code is WorkspaceResolutionFailure.REGISTERED_ROOT_STALE:
                continue
            raise
        usable.append((record, profile))
        if len(usable) > 1:
            raise WorkspaceResolutionError(
                WorkspaceResolutionFailure.AMBIGUOUS_WORKSPACE
            )
    if not usable:
        raise WorkspaceResolutionError(WorkspaceResolutionFailure.WORKSPACE_NOT_FOUND)
    record, profile = usable[0]
    return ResolvedWorkspace(profile, WorkspaceResolutionSource.UNIQUE, record.name)


def _line_from_directory(config: Config, start: Path) -> tuple[Line, ...]:
    location = start.expanduser().resolve()
    matches: list[Line] = []
    for line in list_lines(config):
        try:
            location.relative_to(line_root(config, line).resolve())
        except ValueError:
            continue
        matches.append(line)
    for task in list_tasks(config):
        try:
            location.relative_to(worktree_root(config, task).resolve())
        except ValueError:
            continue
        candidate = get_line(config, task.line)
        if candidate not in matches:
            matches.append(candidate)
    return tuple(sorted(matches, key=lambda item: (item.kind, item.id)))


def resolve_line(
    config: Config,
    *,
    line: str | None = None,
    start: Path | None = None,
    interactive: bool | None = None,
    chooser: Chooser | None = None,
) -> Line:
    if line:
        return get_line(config, line)
    local = _line_from_directory(config, start or Path.cwd())
    if local:
        selected = _choose(
            "开发线",
            tuple(item.id for item in local),
            chooser,
            _interactive() if interactive is None else interactive,
        )
        return get_line(config, selected)
    candidates = tuple(item.id for item in list_lines(config))
    selected = _choose(
        "开发线",
        candidates,
        chooser,
        _interactive() if interactive is None else interactive,
    )
    return get_line(config, selected)


def resolve_objective(
    config: Config,
    *,
    objective_id: str | None = None,
    line: str | None = None,
    interactive: bool | None = None,
    chooser: Chooser | None = None,
) -> StoredObjective:
    if objective_id:
        return get_objective(config, objective_id)
    records = [record for record in list_objectives(config) if record.operator_state != "stopped"]
    if line:
        records = [record for record in records if record.objective.line == line]
    selected = _choose(
        "Objective",
        tuple(record.objective.id for record in records),
        chooser,
        _interactive() if interactive is None else interactive,
    )
    return get_objective(config, selected)

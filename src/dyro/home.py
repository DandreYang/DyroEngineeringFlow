from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shlex
import shutil
import sys
from urllib.parse import urlencode

from .config import CONFIG_NAME, Config, expand_argv, load, validate_id
from .errors import DyroError, ValidationError
from .hub import (
    WorkspaceRecord,
    add_workspace,
    ensure_workspace,
    get_workspace,
    load_registry,
    mark_workspace_used,
)
from .tasks import (
    existing_task_workspace,
    list_tasks,
    load_task,
    status as task_status,
    worktree_root,
)
from .tooling import (
    TOOL_DEFINITIONS,
    ToolDefinition,
    ToolPreferences,
    ToolState,
    install_tool,
    load_tool_preferences,
    tool_definition,
    tool_definition_for_command,
    tool_runtime_issue,
)
from .process import git
from .terminal import danger, muted, success, title, value, warning
from .workspace import (
    Line,
    create_line,
    doctor,
    get_line,
    line_root,
    list_lines,
    preflight_line,
    repository_path,
    status_rows,
)


DISCOVERABLE_AGENTS = tuple(
    (definition.command, definition.profile_preset)
    for definition in TOOL_DEFINITIONS
    if definition.interface != "desktop"
)

TOOL_LABELS = (
    {definition.command: definition.label for definition in TOOL_DEFINITIONS}
    | {definition.id: definition.label for definition in TOOL_DEFINITIONS}
    | {"shell": "Shell"}
)


@dataclass(frozen=True)
class HomeTarget:
    kind: str
    id: str
    label: str


@dataclass(frozen=True)
class HomeTool:
    id: str
    label: str
    kind: str
    argv: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]
    state: ToolState

    @property
    def available(self) -> bool:
        return self.state in {ToolState.READY, ToolState.NEEDS_SETUP}


@dataclass(frozen=True)
class HomeChoice:
    value: str
    label: str
    recommended: bool = False


@dataclass(frozen=True)
class HomeBase:
    base: str
    label: str
    repository_bases: tuple[tuple[str, str], ...] = ()

    def base_for(self, repo_id: str) -> str:
        return dict(self.repository_bases).get(repo_id, self.base)

    def overrides(self) -> dict[str, str]:
        return dict(self.repository_bases)


_BACK = object()


def _is_back(value: object) -> bool:
    return value is _BACK


def interactive_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def print_status(config: Config) -> None:
    print("\n" + title("━━ 当前项目状态 ━━"))
    print(
        muted(
            f"{'SCOPE':24} {'REPOSITORY':14} {'BRANCH':24} {'HEAD':12} {'DIRTY':>5} UPSTREAM"
        )
    )
    for scope, repo_id, branch, head, upstream, dirty in status_rows(config):
        dirty_text = "-" if dirty < 0 else str(dirty)
        dirty_display = (
            danger(f"{dirty_text:>5}") if dirty > 0 else muted(f"{dirty_text:>5}")
        )
        print(
            f"{scope:24} {value(f'{repo_id:14}')} {value(f'{branch:24}')} "
            f"{head:12} {dirty_display} {upstream}"
        )


def print_all_status() -> None:
    registry = load_registry()
    if not registry.workspaces:
        print("还没有登记全局工作区。下一步：dyro workspace add <路径>")
        return
    print("\n" + title("━━ 全部项目状态 ━━"))
    print(muted("按全局工作区逐一检查；不会修改项目文件。"))
    for index, record in enumerate(registry.workspaces):
        if index:
            print()
        print(f"工作区：{value(record.name)} ({value(record.root)})")
        try:
            config = load(record.root)
        except (DyroError, ValidationError) as exc:
            print(danger(f"不可用：{exc}"))
            continue
        print_status(config)


def _adapter_executable(
    config: Config, adapter_id: str, *, workspace: Path | None = None
) -> str:
    adapter = config.adapters[adapter_id]
    return expand_argv(
        adapter.launch,
        workspace=workspace or config.root,
        root=config.root,
        task="",
        line="",
        prompt="",
    )[0]


def executable_available(executable: str, *, cwd: Path | None = None) -> bool:
    if "/" in executable or "\\" in executable:
        path = Path(executable).expanduser()
        if not path.is_absolute():
            path = (cwd or Path.cwd()) / path
        return path.is_file() and os.access(path, os.X_OK)
    return shutil.which(executable) is not None


def available_adapters(config: Config, *, workspace: Path | None = None) -> list[str]:
    return [
        adapter_id
        for adapter_id in sorted(config.adapters)
        if executable_available(
            _adapter_executable(config, adapter_id, workspace=workspace),
            cwd=workspace or config.root,
        )
    ]


def _shell_argv() -> tuple[str, ...]:
    configured = (
        os.environ.get("SHELL", "").strip() or os.environ.get("COMSPEC", "").strip()
    )
    if configured:
        return (configured,)
    fallback = shutil.which("zsh") or shutil.which("bash") or shutil.which("sh")
    return (fallback or "sh",)


def _tool_label(adapter_id: str, executable: str) -> str:
    command = Path(executable).name
    label = TOOL_LABELS.get(command, adapter_id)
    if adapter_id != command and label != adapter_id:
        return f"{label} [{adapter_id}]"
    return label


def _openclaw_needs_setup() -> bool:
    configured = os.environ.get("OPENCLAW_CONFIG_PATH", "").strip()
    if configured:
        path = Path(configured).expanduser()
    else:
        state = os.environ.get("OPENCLAW_STATE_DIR", "").strip()
        if state:
            state_path = Path(state).expanduser()
        else:
            configured_home = os.environ.get("OPENCLAW_HOME", "").strip()
            home = (
                Path(configured_home).expanduser() if configured_home else Path.home()
            )
            profile = os.environ.get("OPENCLAW_PROFILE", "").strip()
            state_path = home / (
                f".openclaw-{profile}"
                if profile and profile != "default"
                else ".openclaw"
            )
        path = state_path / "openclaw.json"
    try:
        return not path.is_file() or path.stat().st_size == 0
    except OSError:
        return True


def _cursor_desktop_argv(workspace: Path) -> tuple[tuple[str, ...], bool]:
    if shutil.which("cursor") is not None:
        return ("cursor", str(workspace)), True
    if sys.platform == "darwin":
        candidates = (
            Path("/Applications/Cursor.app"),
            Path.home() / "Applications" / "Cursor.app",
        )
        if any(candidate.is_dir() for candidate in candidates) and shutil.which("open"):
            return ("open", "-a", "Cursor", str(workspace)), True
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        if local_app_data:
            executable = Path(local_app_data) / "Programs" / "cursor" / "Cursor.exe"
            if executable.is_file():
                return (str(executable), str(workspace)), True
    return ("cursor", str(workspace)), False


def _macos_app_name(*names: str) -> str | None:
    if sys.platform != "darwin":
        return None
    for name in names:
        candidates = (
            Path("/Applications") / f"{name}.app",
            Path.home() / "Applications" / f"{name}.app",
        )
        if any(candidate.is_dir() for candidate in candidates):
            return name
    return None


def _codex_desktop_argv(workspace: Path) -> tuple[tuple[str, ...], bool]:
    argv = ("codex", "app", str(workspace))
    app_name = _macos_app_name("Codex", "ChatGPT")
    if app_name is None:
        return argv, False
    if shutil.which("codex") is not None:
        return argv, True
    if shutil.which("open") is not None:
        return ("open", "-a", app_name, str(workspace)), True
    return argv, False


def _claude_desktop_argv(workspace: Path) -> tuple[tuple[str, ...], bool]:
    url = "claude://code/new?" + urlencode({"folder": str(workspace)})
    if (
        _macos_app_name("Claude", "Claude Code URL Handler") is not None
        and shutil.which("open") is not None
    ):
        return ("open", url), True
    return ("open", url), False


def _zcode_desktop_argv(workspace: Path) -> tuple[tuple[str, ...], bool]:
    if shutil.which("zcode") is not None:
        return ("zcode", str(workspace)), True
    if sys.platform == "darwin":
        candidates = (
            Path("/Applications/ZCode.app"),
            Path.home() / "Applications" / "ZCode.app",
        )
        if any(candidate.is_dir() for candidate in candidates) and shutil.which("open"):
            return ("open", "-a", "ZCode", str(workspace)), True
    return ("zcode", str(workspace)), False


def _launcher_tool(
    definition: ToolDefinition, *, root: Path, workspace: Path
) -> HomeTool:
    if definition.id == "cursor-desktop":
        argv, installed = _cursor_desktop_argv(workspace)
    elif definition.id == "codex-desktop":
        argv, installed = _codex_desktop_argv(workspace)
    elif definition.id == "claude-desktop":
        argv, installed = _claude_desktop_argv(workspace)
    elif definition.id == "zcode":
        argv, installed = _zcode_desktop_argv(workspace)
    else:
        argv = expand_argv(
            definition.launch,
            workspace=workspace,
            root=root,
            task="",
            line="",
            prompt="",
        )
        installed = executable_available(argv[0], cwd=workspace)
    environment = tuple(
        (
            key,
            value.format(
                workspace=workspace,
                root=root,
                task="",
                line="",
                prompt="",
            ),
        )
        for key, value in definition.environment
    )
    runtime_issue = tool_runtime_issue(definition) if installed else ""
    if runtime_issue:
        state = ToolState.UNAVAILABLE
    elif installed:
        needs_setup = definition.id == "openclaw" and _openclaw_needs_setup()
        state = ToolState.NEEDS_SETUP if needs_setup else ToolState.READY
        if needs_setup:
            argv = (
                "openclaw",
                "onboard",
                "--workspace",
                str(workspace),
                "--skip-bootstrap",
            )
            environment = ()
    else:
        state = (
            ToolState.INSTALLABLE
            if definition.install is not None
            else ToolState.UNAVAILABLE
        )
    return HomeTool(
        definition.id,
        definition.label,
        "launcher",
        argv,
        environment,
        state,
    )


def launcher_tools(workspace: Path) -> list[HomeTool]:
    """Detect supported launch-only tools without requiring a saved Profile."""

    return [
        _launcher_tool(definition, root=workspace, workspace=workspace)
        for definition in TOOL_DEFINITIONS
    ]


def home_tools(config: Config, *, workspace: Path) -> list[HomeTool]:
    tools: list[HomeTool] = []
    configured_commands: set[str] = set()
    for adapter_id in sorted(config.adapters):
        executable = _adapter_executable(config, adapter_id, workspace=workspace)
        command = Path(executable).name
        configured_commands.add(command)
        definition = tool_definition_for_command(command)
        installed = executable_available(executable, cwd=workspace)
        runtime_issue = (
            tool_runtime_issue(definition) if definition and installed else ""
        )
        tools.append(
            HomeTool(
                adapter_id,
                _tool_label(adapter_id, executable),
                "adapter",
                (),
                (),
                (
                    ToolState.READY
                    if installed and not runtime_issue
                    else ToolState.INSTALLABLE
                    if definition and definition.install and not runtime_issue
                    else ToolState.UNAVAILABLE
                ),
            )
        )

    for definition in TOOL_DEFINITIONS:
        if (
            definition.id in config.adapters
            or definition.command in configured_commands
        ):
            continue
        tools.append(_launcher_tool(definition, root=config.root, workspace=workspace))

    shell_argv = _shell_argv()
    if (
        "shell" not in config.adapters
        and Path(shell_argv[0]).name not in configured_commands
    ):
        tools.append(
            HomeTool(
                "shell",
                TOOL_LABELS["shell"],
                "launcher",
                shell_argv,
                (),
                (
                    ToolState.READY
                    if executable_available(shell_argv[0], cwd=workspace)
                    else ToolState.UNAVAILABLE
                ),
            )
        )
    return tools


def sort_home_tools(
    tools: list[HomeTool],
    *,
    last_tool: str,
    recommended_tool: str,
    preferences: ToolPreferences,
) -> list[HomeTool]:
    state_rank = {
        ToolState.READY: 0,
        ToolState.NEEDS_SETUP: 1,
        ToolState.INSTALLABLE: 2,
        ToolState.UNAVAILABLE: 3,
    }
    pinned = {tool_id: index for index, tool_id in enumerate(preferences.pinned_tools)}

    def preference_rank(tool: HomeTool) -> tuple[int, int]:
        if tool.id == last_tool:
            return 0, 0
        if tool.id == recommended_tool:
            return 1, 0
        if tool.id == preferences.default_tool:
            return 2, 0
        if tool.id in pinned:
            return 3, pinned[tool.id]
        return 4, 0

    return sorted(
        tools,
        key=lambda tool: (
            tool.id == "shell",
            state_rank[tool.state],
            *preference_rank(tool),
            tool.kind != "adapter",
            tool.id,
        ),
    )


def print_agent_discovery(config: Config) -> None:
    configured_commands: dict[str, list[str]] = {}
    for adapter_id in config.adapters:
        command = Path(_adapter_executable(config, adapter_id)).name
        configured_commands.setdefault(command, []).append(adapter_id)
    print("\n" + title("━━ 本机编码工具 ━━"))
    print(muted("已配置工具可由 Dyro 启动；其他工具只会打开工作区。"))
    print(muted(f"{'命令':16} {'本机':10} {'Dyro 状态':16} 说明"))

    def state_cell(state: str, *, installed: bool, configured: bool) -> str:
        rendered = f"{state:16}"
        if configured and installed:
            return success(rendered)
        if configured or installed:
            return warning(rendered)
        return muted(rendered)

    known_commands = {command for command, _ in DISCOVERABLE_AGENTS}
    for command, integrated in DISCOVERABLE_AGENTS:
        definition = tool_definition_for_command(command)
        detected = shutil.which(command) is not None
        runtime_issue = (
            tool_runtime_issue(definition) if definition is not None and detected else ""
        )
        installed = detected and not runtime_issue
        configured_as = ",".join(configured_commands.get(command, ()))
        if runtime_issue and configured_as:
            state = f"已配置但不兼容:{configured_as}"
            note = runtime_issue
        elif runtime_issue:
            state = "运行环境不兼容"
            note = runtime_issue
        elif configured_as and installed:
            state = f"已配置:{configured_as}"
            note = "可由当前 Profile 启动"
        elif configured_as:
            state = f"已配置但不可用:{configured_as}"
            note = "命令不可用；请检查安装或 adapter 配置"
        elif installed and integrated:
            preset = definition.id if definition is not None else command
            state = "尚未配置"
            note = f"运行 dyro agent add {preset} --preset {preset}"
        elif installed:
            state = "尚未集成"
            note = "首页可仅打开工作区；不获得执行、门禁或复核权限"
        else:
            state = "-"
            note = (
                f"未安装；可运行 dyro tool install {definition.id}"
                if definition and definition.install
                else "未安装；暂无内置安装方案"
            )
        if runtime_issue:
            installed_cell = warning(f"{'不兼容':10}")
        elif installed:
            installed_cell = success(f"{'已检测':10}")
        else:
            installed_cell = muted(f"{'-':10}")
        print(
            f"{value(f'{command:16}')}{installed_cell}"
            f"{state_cell(state, installed=detected, configured=bool(configured_as))} "
            f"{muted(note)}"
        )
    for definition in TOOL_DEFINITIONS:
        if definition.interface != "desktop":
            continue
        desktop_tool = _launcher_tool(
            definition, root=config.root, workspace=config.root
        )
        installed = desktop_tool.available
        desktop_state = "已检测" if installed else "-"
        desktop_note = (
            "首页可打开工作区；不获得执行、门禁或复核权限"
            if installed
            else f"未安装；可运行 dyro tool install {definition.id}"
        )
        desktop_integration = "尚未集成" if installed else "-"
        print(
            f"{value(f'{definition.id:16}')}"
            f"{success(f'{desktop_state:10}') if installed else muted(f'{desktop_state:10}')}"
            f"{warning(f'{desktop_integration:16}') if installed else muted(f'{desktop_integration:16}')} "
            f"{muted(desktop_note)}"
        )
    for adapter_id in sorted(config.adapters):
        executable = _adapter_executable(config, adapter_id)
        if Path(executable).name in known_commands:
            continue
        installed = executable_available(executable, cwd=config.root)
        state = f"已配置:{adapter_id}" if installed else f"已配置但不可用:{adapter_id}"
        note = (
            "可由当前 Profile 启动" if installed else "命令不可用；请检查 adapter 配置"
        )
        installed_cell = success(f"{'已检测':10}") if installed else muted(f"{'-':10}")
        print(
            f"{value(f'{executable:16}')}{installed_cell}"
            f"{state_cell(state, installed=installed, configured=True)} {muted(note)}"
        )


def ready_home_tools(config: Config, *, workspace: Path) -> list[HomeTool]:
    return [
        tool
        for tool in home_tools(config, workspace=workspace)
        if tool.available and tool.id != "shell"
    ]


def resolve_start_tool(
    config: Config,
    *,
    requested: str | None,
    workspace: Path,
    last_tool: str = "",
) -> HomeTool | None:
    """Pick a ready adapter or installed launcher.

    Returns None when the user must choose among multiple tools.
    """

    ready = ready_home_tools(config, workspace=workspace)
    if not ready:
        raise DyroError(
            "未发现可启动的编码工具；安装本机 Agent 后运行 dyro agent discover"
        )

    def match(value: str) -> HomeTool | None:
        for tool in ready:
            if tool.id == value:
                return tool
        definition = tool_definition(value) or tool_definition_for_command(value)
        if definition is None:
            return None
        for tool in ready:
            if tool.id == definition.id or tool.id == definition.command:
                return tool
        return None

    if requested:
        tool = match(requested)
        if tool is None:
            available = "、".join(tool.id for tool in ready)
            raise DyroError(
                f"未找到可启动的 Agent：{requested}。"
                f"本机可用：{available}。运行 dyro agent discover 查看详情"
            )
        return tool

    preferences = load_tool_preferences()
    for candidate in (preferences.default_tool, last_tool):
        if candidate:
            tool = match(candidate)
            if tool is not None:
                return tool
    adapters = [tool for tool in ready if tool.kind == "adapter"]
    if len(adapters) == 1:
        return adapters[0]
    if len(ready) == 1:
        return ready[0]
    return None


def launch_start_tool(
    config: Config,
    *,
    workspace: Path,
    tool: HomeTool,
    line: str = "",
    task: str = "",
    prompt: str = "",
    dry_run: bool,
) -> None:
    if tool.kind == "adapter":
        launch_adapter(
            config,
            workspace=workspace,
            adapter_id=tool.id,
            line=line,
            task=task,
            prompt=prompt,
            dry_run=dry_run,
        )
        return
    if not tool.argv:
        rebuilt = next(
            (
                item
                for item in home_tools(config, workspace=workspace)
                if item.id == tool.id and item.argv
            ),
            None,
        )
        if rebuilt is None:
            raise DyroError(
                f"{tool.label} 没有可执行的启动命令；运行 dyro agent discover 查看详情"
            )
        tool = rebuilt
    launch_home_tool(workspace=workspace, tool=tool, dry_run=dry_run)


def launch_adapter(
    config: Config,
    *,
    workspace: Path,
    adapter_id: str,
    line: str = "",
    task: str = "",
    prompt: str = "",
    dry_run: bool,
) -> None:
    try:
        adapter = config.adapters[adapter_id]
    except KeyError as exc:
        raise DyroError(
            f"未配置 Agent：{adapter_id}；运行 dyro agent discover 查看下一步"
        ) from exc
    if not workspace.is_dir():
        raise DyroError(f"工作目录不存在：{workspace}")
    argv = expand_argv(
        adapter.launch,
        workspace=workspace,
        root=config.root,
        task=task,
        line=line,
        prompt=prompt,
    )
    if not executable_available(argv[0], cwd=workspace):
        raise DyroError(
            f"Agent {adapter_id} 的命令不可用：{argv[0]}；运行 dyro agent discover 查看本机状态"
        )
    print(f"工作目录：{workspace}")
    print("$ " + shlex.join(argv))
    if dry_run:
        return
    os.chdir(workspace)
    try:
        os.execvp(argv[0], list(argv))
    except OSError as exc:
        raise DyroError(f"无法启动 Agent {adapter_id}：{exc}") from exc


def launch_home_tool(*, workspace: Path, tool: HomeTool, dry_run: bool) -> None:
    if tool.kind != "launcher" or not tool.argv:
        raise ValidationError(f"首页启动器无效：{tool.id}")
    if not workspace.is_dir():
        raise DyroError(f"工作目录不存在：{workspace}")
    if not executable_available(tool.argv[0], cwd=workspace):
        raise DyroError(
            f"{tool.label} 未安装或不可执行：{tool.argv[0]}；"
            "运行 dyro agent discover 查看本机状态"
        )
    print(f"工作目录：{workspace}")
    assignments = [f"{key}={value}" for key, value in tool.environment]
    print("$ " + shlex.join([*assignments, *tool.argv]))
    if dry_run:
        return
    os.chdir(workspace)
    try:
        environment = os.environ.copy()
        environment.update(dict(tool.environment))
        os.execvpe(tool.argv[0], list(tool.argv), environment)
    except OSError as exc:
        raise DyroError(f"无法启动编码工具 {tool.label}：{exc}") from exc


def existing_line_workspace(
    config: Config, line_id: str, kind: str | None
) -> tuple[Line, Path]:
    line = get_line(config, line_id, kind)
    relevant = {f"FAIL repository {repo_id}:" for repo_id in line.repositories}
    relevant.add(f"FAIL {line.kind}:{line.id}/")
    failures = [
        finding
        for finding in doctor(config)
        if any(finding.startswith(prefix) for prefix in relevant)
    ]
    if failures:
        raise DyroError(
            f"{line.kind} {line.id} 尚未就绪：{failures[0]}。下一步：dyro doctor"
        )
    workspace = line_root(config, line)
    if not workspace.is_dir():
        raise DyroError(f"开发线工作区不存在：{workspace}。下一步：dyro doctor")
    return line, workspace


def open_line(
    config: Config,
    line_id: str,
    *,
    kind: str | None,
    agent: str | None,
    prompt: str,
    dry_run: bool,
    last_tool: str = "",
) -> None:
    line, workspace = existing_line_workspace(config, line_id, kind)
    tool = resolve_start_tool(
        config,
        requested=agent or None,
        workspace=workspace,
        last_tool=last_tool,
    )
    if tool is None:
        ready = "、".join(
            item.id for item in ready_home_tools(config, workspace=workspace)
        )
        raise DyroError(
            f"有多个可启动的编码工具；请用 --agent <id> 指定。本机可用：{ready}"
        )
    launch_start_tool(
        config,
        workspace=workspace,
        tool=tool,
        line=line.id,
        prompt=prompt,
        dry_run=dry_run,
    )


def open_task(
    config: Config,
    task_id: str,
    *,
    agent: str | None,
    prompt: str,
    dry_run: bool,
    last_tool: str = "",
) -> None:
    task = load_task(config, task_id)
    workspace = existing_task_workspace(config, task)
    tool = resolve_start_tool(
        config,
        requested=agent or None,
        workspace=workspace,
        last_tool=last_tool,
    )
    if tool is None:
        ready = "、".join(
            item.id for item in ready_home_tools(config, workspace=workspace)
        )
        raise DyroError(
            f"有多个可启动的编码工具；请用 --agent <id> 指定。本机可用：{ready}"
        )
    launch_start_tool(
        config,
        workspace=workspace,
        tool=tool,
        line=task.line,
        task=task.id,
        prompt=prompt,
        dry_run=dry_run,
    )


def _record_for_root(root: Path) -> WorkspaceRecord | None:
    resolved = root.resolve()
    return next(
        (record for record in load_registry().workspaces if record.root == resolved),
        None,
    )


def _registered_home_config() -> tuple[Config, WorkspaceRecord] | None:
    registry = load_registry()
    if not registry.workspaces:
        return None
    preferred = registry.default or registry.workspaces[0].name
    ordered = sorted(registry.workspaces, key=lambda record: record.name != preferred)
    failures: list[str] = []
    for record in ordered:
        try:
            return load(record.root), record
        except (DyroError, ValidationError):
            failures.append(record.name)
    raise DyroError(
        "已登记工作区都不可用："
        + "、".join(failures)
        + "。下一步：dyro workspace list；修复路径后重新 add，或 remove 失效入口"
    )


def resolve_home_config(
    *,
    root: str | None,
    workspace: str | None,
    dry_run: bool,
) -> tuple[Config, WorkspaceRecord | None] | None:
    if root or workspace:
        config = load(
            Path(root).expanduser() if root else get_workspace(workspace or "").root
        )
        record = _record_for_root(config.root)
        if record is None and not dry_run:
            record = ensure_workspace(config.root)
        return config, record
    try:
        config = load(Path.cwd())
    except ValidationError:
        if any(
            (candidate / CONFIG_NAME).is_file()
            for candidate in (Path.cwd(), *Path.cwd().parents)
        ):
            raise
        return _registered_home_config()
    record = _record_for_root(config.root)
    if record is None and not dry_run:
        record = ensure_workspace(config.root)
        print(success(f"已将当前项目加入全局首页：{record.name}"))
    return config, record


def _first_use(dry_run: bool) -> None:
    print("\n" + title("━━ 欢迎使用 Dyro ━━"))
    print(muted("这里还没有可直接打开的工作区。选择一种开始方式：") + "\n")
    print("  1) 加入团队工作区：dyro join <蓝图地址>")
    print("  2) 设置一个新项目：dyro setup")
    print("  3) 登记已有工作区：dyro workspace add <路径>")
    if not interactive_terminal():
        print("在交互终端中再次运行 dyro，可直接选择下一步。")
        return
    while True:
        choice = (
            input("\n请选择（直接回车默认加入团队工作区，q 退出）：").strip().lower()
        )
        if choice in {"q", "quit"}:
            print("已退出；没有修改任何文件。")
            return
        if choice in {"", "1"}:
            source = input("团队蓝图地址或本地文件（q 返回）：").strip()
            if source.lower() in {"q", "quit"}:
                continue
            if source:
                print(f"下一步：dyro join {shlex.quote(source)}")
                return
            print("需要团队蓝图地址或本地文件；请重新选择。")
            continue
        if choice == "2":
            print("下一步：dyro setup（确认设置计划前不会修改任何文件）")
            return
        if choice == "3":
            raw_path = input("已有 Dyro 工作区路径（q 返回）：").strip()
            if raw_path.lower() in {"q", "quit"}:
                continue
            if not raw_path:
                print("需要工作区路径；请重新选择。")
                continue
            try:
                config = load(Path(raw_path).expanduser())
            except (DyroError, OSError) as exc:
                print(f"无法登记该路径：{exc}。请检查路径后重试。")
                continue
            if dry_run:
                print(f"DRY RUN: 将登记工作区 {config.name} -> {config.root.resolve()}")
                return
            record = add_workspace(config.root, name=config.name, make_default=True)
            print(f"已登记工作区：{record.name}。接下来选择要做什么。")
            _run_config_home(config, record, dry_run)
            return
        print("请选择 1、2、3 或 q。")


def _targets(config: Config, record: WorkspaceRecord | None) -> list[HomeTarget]:
    targets = [
        HomeTarget(
            line.kind,
            line.id,
            f"进入{'开发线' if line.kind == 'line' else 'Hotfix'}：{line.id}",
        )
        for line in list_lines(config)
    ]
    targets.extend(
        HomeTarget(
            "task", task.id, f"继续任务：{task.id} [{task_status(config, task)}]"
        )
        for task in list_tasks(config)
        if worktree_root(config, task).is_dir()
    )
    if record and record.last_kind and record.last_target:
        for index, target in enumerate(targets):
            if (target.kind, target.id) == (record.last_kind, record.last_target):
                recent = targets.pop(index)
                targets.insert(
                    0,
                    HomeTarget(
                        recent.kind,
                        recent.id,
                        "继续上次：" + recent.label.partition("：")[2],
                    ),
                )
                break
    return targets


def _choose_action(
    targets: list[HomeTarget], *, can_switch: bool
) -> tuple[str, str] | None:
    print("\n" + title("━━ 今天做什么 ━━"))
    if targets:
        print("\n" + muted("继续工作"))
    for index, target in enumerate(targets, start=1):
        prefix, delimiter, target_value = target.label.partition("：")
        rendered_target = (
            f"{prefix}{delimiter}{value(target_value)}"
            if delimiter
            else value(target.label)
        )
        print(f"  {index}) {rendered_target}")
    status_index = len(targets) + 1
    print("\n" + muted("查看与管理"))
    print(f"  {status_index}) 查看当前项目状态")
    console_index = status_index + 1
    print(f"  {console_index}) 查看全部项目控制台")
    switch_index = console_index + 1
    if can_switch:
        print(f"  {switch_index}) 切换项目")
    new_line_index = switch_index + 1 if can_switch else console_index + 1
    new_hotfix_index = new_line_index + 1
    print("\n" + muted("开始新的工作"))
    print(f"  {new_line_index}) 开启新的功能开发线")
    print(f"  {new_hotfix_index}) 处理新的线上问题 / Hotfix")
    print("  " + muted("q) 退出"))
    if not interactive_terminal():
        print(
            "\n在交互终端运行 dyro 可选择并直接进入；也可使用 dyro open 或 dyro task open。"
        )
        return None
    default = "1" if targets else str(new_line_index)
    while True:
        raw = (
            input(f"\n输入编号（回车={default}，q=退出）：").strip().lower() or default
        )
        if raw in {"q", "quit"}:
            return None
        if not raw.isdigit():
            print(warning("请输入菜单编号，或输入 q 退出。"))
            continue
        selected = int(raw)
        if 1 <= selected <= len(targets):
            target = targets[selected - 1]
            return target.kind, target.id
        if selected == status_index:
            return "status", ""
        if selected == console_index:
            return "console", ""
        if can_switch and selected == switch_index:
            return "switch", ""
        if selected == new_line_index:
            return "new-line", ""
        if selected == new_hotfix_index:
            return "new-hotfix", ""
        print(warning("该编号不在当前菜单中；请重新选择，或输入 q 退出。"))


def _tool_state_labels(
    tool: HomeTool,
    *,
    default: HomeTool,
    last_tool: str,
    recommended_tool: str,
    preferences: ToolPreferences,
) -> list[str]:
    definition = tool_definition(tool.id)
    if tool.kind == "adapter":
        if tool.state == ToolState.READY:
            labels = ["Dyro 已接入"]
        elif tool.state == ToolState.INSTALLABLE:
            labels = ["已接入，待安装"]
        else:
            labels = ["已接入但不可用"]
    elif tool.state == ToolState.READY:
        interface = definition.interface if definition else "terminal"
        if interface == "desktop":
            labels = ["桌面应用"]
        elif interface == "runtime":
            labels = ["外部运行时"]
        elif tool.id == "shell":
            labels = ["终端兜底"]
        else:
            labels = ["打开工作区"]
    elif tool.state == ToolState.NEEDS_SETUP:
        labels = ["待初始化"]
    elif tool.state == ToolState.INSTALLABLE:
        labels = ["未安装，可引导安装"]
    else:
        labels = ["不可用"]
    if tool.id == last_tool:
        labels.append("上次")
    if tool.id == recommended_tool:
        labels.append("项目推荐")
    if tool.id == preferences.default_tool:
        labels.append("默认")
    if tool.id in preferences.pinned_tools:
        labels.append("置顶")
    if tool.id == default.id:
        labels.append("回车默认")
    return labels


def _primary_home_tools(
    tools: list[HomeTool],
    *,
    default: HomeTool,
    last_tool: str,
    recommended_tool: str,
    preferences: ToolPreferences,
) -> list[HomeTool]:
    by_id = {tool.id: tool for tool in tools}
    preferred_ids = (
        default.id,
        last_tool,
        recommended_tool,
        preferences.default_tool,
        *preferences.pinned_tools,
    )
    primary: list[HomeTool] = []
    for tool_id in preferred_ids:
        tool = by_id.get(tool_id)
        if tool and (tool.available or tool == default) and tool not in primary:
            primary.append(tool)
    for tool in tools:
        if tool.kind == "adapter" and tool.available and tool not in primary:
            primary.append(tool)
    return primary[:3]


def _matches_home_tool(tool: HomeTool, value: str) -> bool:
    if tool.id.lower() == value or tool.label.lower() == value:
        return True
    definition = tool_definition(tool.id)
    return bool(definition and definition.command.lower() == value)


def _install_id(config: Config, tool: HomeTool) -> str:
    if tool.kind != "adapter":
        return tool.id
    command = Path(_adapter_executable(config, tool.id)).name
    definition = tool_definition_for_command(command)
    return definition.id if definition else tool.id


def _confirm_setup(tool: HomeTool, *, workspace: Path, dry_run: bool) -> bool:
    if tool.state != ToolState.NEEDS_SETUP:
        return True
    print(f"\n{tool.label} 已安装，但还需要完成官方初始化。")
    print(f"Dyro 会把所选路径设为 OpenClaw 的默认工作区：{workspace}")
    print("注意：OpenClaw 工作区不是系统沙箱；它仍可能在当前用户权限内访问其他路径。")
    print("Dyro 会要求 OpenClaw 跳过在项目中生成 bootstrap 文件。")
    print("它不会因此获得 Dyro 的门禁、复核、合并或 push 权限。")
    if dry_run:
        print("DRY RUN: 将启动官方 onboarding；未执行。")
        return True
    return input("现在启动初始化？[y/N]：").strip().lower() in {"y", "yes"}


def _choose_tool(
    config: Config,
    record: WorkspaceRecord | None,
    *,
    workspace: Path,
    dry_run: bool,
) -> HomeTool | None:
    preferences = load_tool_preferences()
    last_tool = record.last_agent if record else ""
    tools = sort_home_tools(
        home_tools(config, workspace=workspace),
        last_tool=last_tool,
        recommended_tool=config.recommended_tool,
        preferences=preferences,
    )
    ready = [tool for tool in tools if tool.state == ToolState.READY]
    selectable = [
        tool for tool in tools if tool.available or tool.state == ToolState.INSTALLABLE
    ]
    if not selectable:
        print_agent_discovery(config)
        raise DyroError("当前项目没有可启动的编码工具；请选择可引导安装的工具")
    default = ready[0] if ready else selectable[0]
    show_all = False
    while True:
        displayed = (
            tools
            if show_all
            else _primary_home_tools(
                tools,
                default=default,
                last_tool=last_tool,
                recommended_tool=config.recommended_tool,
                preferences=preferences,
            )
        )
        if show_all:
            print("\n" + title("━━ 全部编码工具 ━━"))
            print(muted("可用、可安装与暂不可用工具均列在这里。"))
        else:
            print("\n" + title("━━ 常用编码工具 ━━"))
            print(muted("按上次使用、项目推荐与个人偏好排序。"))
        for index, tool in enumerate(displayed, start=1):
            labels = _tool_state_labels(
                tool,
                default=default,
                last_tool=last_tool,
                recommended_tool=config.recommended_tool,
                preferences=preferences,
            )
            print(f"  {index:>2}) {value(tool.label)}")
            print(f"      {muted(' · '.join(labels))}")
        if show_all:
            print("  " + muted("b) 返回常用工具"))
        else:
            hidden_ready = sum(
                tool.available and tool not in displayed for tool in tools
            )
            installable = sum(tool.state == ToolState.INSTALLABLE for tool in tools)
            details = []
            if hidden_ready:
                details.append(f"{hidden_ready} 个已安装")
            if installable:
                details.append(f"{installable} 个可安装")
            suffix = f"（{'，'.join(details)}）" if details else ""
            print("  " + muted(f"m) 更多工具{suffix}"))
        print("  " + muted("q) 退出"))
        controls = "b=常用" if show_all else "m=更多"
        raw = (
            input(f"\n输入编号或工具名（回车={default.label}，{controls}，q=退出）：")
            .strip()
            .lower()
        )
        if not raw:
            return default
        if raw in {"q", "quit"}:
            return None
        if raw in {"m", "more", "更多"}:
            show_all = True
            continue
        if raw in {"b", "back", "返回"} and show_all:
            show_all = False
            continue
        if raw.isdigit() and 1 <= int(raw) <= len(displayed):
            selected = displayed[int(raw) - 1]
        else:
            selected = next(
                (tool for tool in tools if _matches_home_tool(tool, raw)), None
            )
        if selected is None:
            print(
                warning(
                    "未找到该编码工具；请重新选择、输入 m 查看全部工具，或输入 q 退出。"
                )
            )
            continue
        if selected.state == ToolState.UNAVAILABLE:
            print(
                warning(
                    f"{selected.label} 当前不可用；请选择可用工具，"
                    "或选择标有“未安装，可引导安装”的工具。"
                )
            )
            continue
        break
    if selected.state == ToolState.INSTALLABLE:
        install_id = _install_id(config, selected)
        if not install_tool(install_id, yes=False, dry_run=dry_run):
            return None
        tools = sort_home_tools(
            home_tools(config, workspace=workspace),
            last_tool=last_tool,
            recommended_tool=config.recommended_tool,
            preferences=preferences,
        )
        selected = next((tool for tool in tools if tool.id == selected.id), None)
        if selected is None or not selected.available:
            raise DyroError(
                f"安装命令已完成，但当前终端仍未检测到 {install_id}；"
                "请重新打开终端后再运行 dyro"
            )
    if not _confirm_setup(selected, workspace=workspace, dry_run=dry_run):
        print("已取消初始化；没有修改工作区。")
        return None
    return selected


def _switch_workspace(dry_run: bool) -> None:
    available: list[WorkspaceRecord] = []
    for record in load_registry().workspaces:
        try:
            load(record.root)
        except (DyroError, ValidationError):
            continue
        available.append(record)
    if len(available) < 2:
        raise DyroError("没有其他可用工作区；运行 dyro workspace add <路径> 添加项目")
    while True:
        print("\n" + title("━━ 切换项目 ━━"))
        print(muted("选择一个已登记的工作区："))
        for index, record in enumerate(available, start=1):
            print(f"  {index}) {value(record.name)}")
        print("  " + muted("q) 返回"))
        raw = input("输入编号（q=返回）：").strip().lower()
        if raw in {"q", "quit"}:
            return
        if raw.isdigit() and 1 <= int(raw) <= len(available):
            record = available[int(raw) - 1]
            _run_config_home(load(record.root), record, dry_run)
            return
        print(warning("请输入有效编号，或输入 q 返回。"))


def _ask_hotfix_id() -> str | None:
    while True:
        raw = input("问题 ID（字母、数字、点、下划线或连字符；q 取消）：").strip()
        if raw.lower() in {"q", "quit"}:
            print("已取消；没有修改任何 Git 工作区。")
            return None
        try:
            return validate_id(raw, "问题 ID")
        except ValidationError as exc:
            print(f"{exc}。请重新输入，或输入 q 取消。")


def _ask_choice(
    question: str,
    choices: tuple[HomeChoice, ...],
    *,
    allow_back: bool = False,
) -> HomeChoice | object | None:
    if not choices:
        raise ValueError("至少提供一个选项")
    default_index = next(
        (index for index, choice in enumerate(choices, start=1) if choice.recommended),
        1,
    )
    print("\n" + title(question))
    for index, choice in enumerate(choices, start=1):
        suffix = success("（推荐）") if choice.recommended else ""
        print(f"  {index}) {choice.label}{suffix}")
    if allow_back:
        print("  " + muted("b) 返回上一步"))
    print("  " + muted("q) 取消"))
    while True:
        controls = "b=上一步，q=取消" if allow_back else "q=取消"
        raw = input(f"输入编号（回车={default_index}，{controls}）：").strip().lower()
        if raw in {"q", "quit"}:
            print("已取消；没有修改任何 Git 工作区。")
            return None
        if allow_back and raw in {"b", "back", "返回"}:
            return _BACK
        selected = default_index if not raw else int(raw) if raw.isdigit() else 0
        if 1 <= selected <= len(choices):
            return choices[selected - 1]
        print("请输入有效编号，或输入 q 取消。")


def _ask_line_id() -> str | None:
    while True:
        raw = input(
            "\n[1/3] 功能 ID（字母、数字、点、下划线或连字符；q 取消）："
        ).strip()
        if raw.lower() in {"q", "quit"}:
            print("已取消；没有修改任何 Git 工作区。")
            return None
        try:
            return validate_id(raw, "功能 ID")
        except ValidationError as exc:
            print(f"{exc}。请重新输入，或输入 q 取消。")


def _ask_line_repositories(config: Config) -> tuple[str, ...] | object | None:
    repositories = tuple(config.repositories)
    if len(repositories) == 1:
        print(f"\n[2/3] 参与仓库：{repositories[0]}（唯一已配置仓库）")
        return repositories
    custom_choice = HomeChoice("", "自定义：仅选择受影响的仓库")
    choice = _ask_choice(
        "\n[2/3] 选择参与仓库：",
        (
            HomeChoice(
                "all",
                f"全部已配置仓库（{len(repositories)} 个）",
                recommended=True,
            ),
            custom_choice,
        ),
        allow_back=True,
    )
    if _is_back(choice):
        return _BACK
    if choice is None:
        return None
    if choice is not custom_choice:
        return repositories
    print("\n可选仓库：")
    for index, repo_id in enumerate(repositories, start=1):
        print(f"  {index}) {repo_id}")
    while True:
        raw = input(
            "输入受影响的仓库序号或 ID（逗号分隔，如 1,3 或 miniapp；b 上一步，q 取消）："
        ).strip()
        if raw.lower() in {"q", "quit"}:
            print("已取消；没有修改任何 Git 工作区。")
            return None
        if raw.lower() in {"b", "back", "返回"}:
            return _BACK
        selected, error = _parse_repository_selection(raw, repositories)
        if error is not None:
            print(error)
            continue
        return selected


def _parse_repository_selection(
    raw: str, repositories: tuple[str, ...]
) -> tuple[tuple[str, ...] | None, str | None]:
    """Resolve comma-separated indices and/or repository IDs.

    Exact repository ID matches win over 1-based indices so pure-numeric IDs
    are not silently reinterpreted as list positions.

    Returns ``(selected_ids, None)`` on success, or ``(None, error_message)``.
    """
    tokens = [
        item.strip()
        for item in raw.replace("，", ",").split(",")
        if item.strip()
    ]
    if not tokens:
        return None, "至少选择一个仓库，或输入 q 取消。"
    selected: list[str] = []
    unknown: list[str] = []
    for token in tokens:
        # Prefer exact repository ID matches so pure-numeric IDs are not
        # silently reinterpreted as 1-based list indices.
        if token in repositories:
            selected.append(token)
            continue
        if token.isdigit():
            index = int(token)
            if 1 <= index <= len(repositories):
                selected.append(repositories[index - 1])
            else:
                return (
                    None,
                    f"序号超出范围：{token}（有效 1–{len(repositories)}）。请重新选择。",
                )
            continue
        unknown.append(token)
    if unknown:
        return None, f"未配置的仓库：{'、'.join(unknown)}。请重新选择。"
    return tuple(dict.fromkeys(selected)), None


def _ask_line_base(config: Config) -> str | object | None:
    manual_choice = HomeChoice("", "其他：输入分支、tag 或 commit SHA")
    choice = _ask_choice(
        "\n[3/3] 选择功能开发基线：",
        (
            HomeChoice(
                config.policy.default_base,
                f"{config.policy.default_base}（工作区默认基线）",
                recommended=True,
            ),
            manual_choice,
        ),
        allow_back=True,
    )
    if _is_back(choice):
        return _BACK
    if choice is None:
        return None
    if choice is not manual_choice:
        return choice.value
    while True:
        raw = input(
            "功能开发基线（分支、tag 或 commit SHA；b 上一步，q 取消）："
        ).strip()
        if raw.lower() in {"q", "quit"}:
            print("已取消；没有修改任何 Git 工作区。")
            return None
        if raw.lower() in {"b", "back", "返回"}:
            return _BACK
        if raw:
            return raw
        print("开发基线不能为空；请重新输入，或输入 q 取消。")


def _unresolved_base_repositories(
    config: Config,
    repositories: tuple[str, ...],
    selection: HomeBase,
) -> tuple[tuple[str, str], ...]:
    unresolved: list[tuple[str, str]] = []
    for repo_id in repositories:
        base = selection.base_for(repo_id)
        if (
            git(
                repository_path(config, repo_id),
                "rev-parse",
                "--verify",
                f"{base}^{{commit}}",
            ).code
            != 0
        ):
            unresolved.append((repo_id, base))
    return tuple(unresolved)


def _resolve_repository_base(
    config: Config, repo_id: str, requested_base: str
) -> str | None:
    """Resolve a user-friendly branch spelling against one repository anchor."""
    anchor = repository_path(config, repo_id)
    candidates = [requested_base]
    if requested_base.startswith("origin/"):
        candidates.append(requested_base.removeprefix("origin/"))
    elif "/" not in requested_base:
        candidates.append(f"origin/{requested_base}")
    for candidate in dict.fromkeys(candidates):
        if git(anchor, "rev-parse", "--verify", f"{candidate}^{{commit}}").code == 0:
            return candidate
    return None


def _normalize_repository_bases(
    config: Config,
    repositories: tuple[str, ...],
    requested_base: str,
) -> HomeBase | object | None:
    """Resolve equivalent refs per repository and ask only for true exceptions."""
    bases: dict[str, str] = {}
    unresolved: list[str] = []
    for repo_id in repositories:
        resolved = _resolve_repository_base(config, repo_id, requested_base)
        if resolved is None:
            unresolved.append(repo_id)
        else:
            bases[repo_id] = resolved
    for repo_id in unresolved:
        print(
            f"{repo_id} 无法解析 {requested_base}；请为这个仓库单独选择已核实的基线。"
        )
        selected = _ask_repository_base(config, repo_id, requested_base)
        if _is_back(selected):
            return _BACK
        if selected is None:
            return None
        bases[repo_id] = selected
    base = bases[repositories[0]]
    overrides = tuple(
        (repo_id, repo_base)
        for repo_id, repo_base in bases.items()
        if repo_base != base
    )
    return HomeBase(base, requested_base, overrides)


def _print_unresolved_bases(unresolved: tuple[tuple[str, str], ...]) -> None:
    print("\n基线未就绪；尚未创建任何 Git worktree：")
    for repo_id, base in unresolved:
        print(f"  - {repo_id}: {base}")
    print("请重新选择基线，或输入 q 取消。")


def _print_repository_base_map(
    repositories: tuple[str, ...], selection: HomeBase | Line
) -> None:
    """Render a short, scannable per-repository base map for terminal users."""
    width = max(len(repo_id) for repo_id in repositories)
    for repo_id in repositories:
        marker = "默认" if selection.base_for(repo_id) == selection.base else "覆盖"
        print(
            f"    {value(f'{repo_id:<{width}}')}  "
            f"{value(selection.base_for(repo_id))}  {muted(f'[{marker}]')}"
        )


def _retry_after_preflight_failure(kind: str, exc: Exception) -> bool:
    print("\n" + danger(f"━━ 创建{kind}前检查未通过 ━━"))
    print(muted("尚未创建任何 Git worktree。"))
    print(danger(f"原因：{exc}"))
    next_step = _ask_choice(
        "下一步：",
        (
            HomeChoice("retry", f"重新填写{kind}信息", recommended=True),
            HomeChoice("home", "返回首页"),
        ),
    )
    return next_step is not None and next_step.value == "retry"


def _previous_editable_step(config: Config) -> int:
    """Return the preceding editable wizard step for a selected base."""
    return 2 if len(config.repositories) > 1 else 1


def _create_line_from_home(config: Config, dry_run: bool) -> Line | None:
    print("\n" + title("━━ 开启功能开发线 ━━"))
    print(muted("新功能开发线会创建隔离 Git worktree；确认前不会修改 Git 工作区。"))
    print(muted("步骤：功能 ID → 参与仓库 → 开发基线 → 创建确认"))
    step = 1
    line_id = ""
    repositories: tuple[str, ...] = ()
    while True:
        if step == 1:
            line_id = _ask_line_id()
            if line_id is None:
                return None
            step = 2
            continue
        if step == 2:
            selected_repositories = _ask_line_repositories(config)
            if _is_back(selected_repositories):
                step = 1
                continue
            if selected_repositories is None:
                return None
            repositories = selected_repositories
            step = 3
            continue
        if step == 3:
            base = _ask_line_base(config)
            if _is_back(base):
                step = _previous_editable_step(config)
                continue
            if base is None:
                return None
            base_selection = _normalize_repository_bases(config, repositories, base)
            if _is_back(base_selection):
                continue
            if base_selection is None:
                return None
            base_selection = _ask_repository_base_overrides(
                config,
                base_selection,
                kind="功能开发线",
                repositories=repositories,
            )
            if _is_back(base_selection):
                continue
            if base_selection is None:
                return None
            unresolved = _unresolved_base_repositories(
                config, repositories, base_selection
            )
            if not unresolved:
                step = 4
                continue
            _print_unresolved_bases(unresolved)
            continue

        branch = f"feat/{line_id}"
        try:
            planned = preflight_line(
                config,
                line_id=line_id,
                branch=branch,
                base=base_selection.base,
                repositories=repositories,
                repository_bases=base_selection.overrides(),
                kind="line",
            )
        except (DyroError, ValidationError, OSError) as exc:
            if _retry_after_preflight_failure("功能开发线", exc):
                step = 1
                continue
            return None
        print("\n" + title("━━ 创建前确认 ━━"))
        print(f"  功能 ID：{value(line_id)}")
        print(f"  分支：{value(branch)}")
        print(f"  基线：{value(base_selection.base)}")
        if base_selection.repository_bases:
            print("  仓库基线：")
            _print_repository_base_map(planned.repositories, planned)
        print(f"  仓库：{value('、'.join(repositories))}")
        print(f"  位置：{value(line_root(config, planned))}")
        if dry_run:
            print("DRY RUN: 已展示创建计划；不会创建 Git worktree。")
            return None
        confirmation = input(
            "\n确认创建这些隔离 worktree？[Y/b/n；回车确认，b 返回基线]："
        ).strip().lower()
        if confirmation in {"b", "back", "返回"}:
            step = 3
            continue
        if confirmation not in {"", "y", "yes"}:
            print("已取消；没有修改任何 Git 工作区。")
            return None
        try:
            line = create_line(
                config,
                line_id=line_id,
                branch=branch,
                base=base_selection.base,
                repositories=repositories,
                repository_bases=base_selection.overrides(),
                kind="line",
            )
        except (DyroError, ValidationError, OSError) as exc:
            print(danger(f"创建功能开发线失败：{exc}"))
            print(warning("没有完整创建开发线；请重新运行 dyro 后重试。"))
            return None
        print(
            success("已创建功能开发线：")
            + value(line.id)
            + "。接下来选择编码工具并打开隔离工作区。"
        )
        return line


def _shared_tag_candidates(
    config: Config,
    *,
    repositories: tuple[str, ...] | None = None,
    limit: int = 4,
) -> tuple[HomeBase, ...]:
    """Return recent tags that resolve from every selected repository anchor."""
    selected_repositories = repositories or tuple(config.repositories)
    common: set[str] | None = None
    ordered: list[str] = []
    for repo_id in selected_repositories:
        result = git(
            repository_path(config, repo_id),
            "for-each-ref",
            "--sort=-creatordate",
            "--format=%(refname:short)",
            "refs/tags",
        )
        if result.code != 0:
            return ()
        tags = [tag.strip() for tag in result.stdout.splitlines() if tag.strip()]
        if common is None:
            ordered = tags
            common = set(tags)
        else:
            common.intersection_update(tags)
    candidates: list[HomeBase] = []
    for tag in ordered:
        if tag not in (common or set()):
            continue
        if all(
            git(
                repository_path(config, repo_id),
                "rev-parse",
                "--verify",
                f"{tag}^{{commit}}",
            ).code
            == 0
            for repo_id in selected_repositories
        ):
            candidates.append(
                HomeBase(tag, f"发布 tag {tag}（所有已配置仓库均可解析）")
            )
        if len(candidates) == limit:
            break
    return tuple(candidates)


def _shared_release_branch_candidates(
    config: Config,
    *,
    repositories: tuple[str, ...] | None = None,
    limit: int = 4,
) -> tuple[HomeBase, ...]:
    """Return common release branch names, allowing per-repository ref spelling.

    A repository can expose the same branch as ``origin/release`` while another
    only has a local ``release`` branch.  ``create_line`` already supports this
    through repository-specific bases, so the home flow should surface it rather
    than asking the user to guess a string that works everywhere.
    """
    selected_repositories = repositories or tuple(config.repositories)
    common: set[str] | None = None
    ordered: list[str] = []
    refs_by_repo: dict[str, dict[str, str]] = {}
    for repo_id in selected_repositories:
        result = git(
            repository_path(config, repo_id),
            "for-each-ref",
            "--sort=-committerdate",
            "--format=%(refname)",
            "refs/heads",
            "refs/remotes",
        )
        if result.code != 0:
            return ()
        refs: dict[str, tuple[int, str]] = {}
        for ref in (item.strip() for item in result.stdout.splitlines()):
            if ref.startswith("refs/heads/"):
                name = ref.removeprefix("refs/heads/")
                priority = 1
                short_ref = name
            elif ref.startswith("refs/remotes/"):
                remote_and_name = ref.removeprefix("refs/remotes/")
                remote, separator, name = remote_and_name.partition("/")
                if not separator:
                    continue
                priority = 3 if remote == "origin" else 2
                short_ref = f"{remote}/{name}"
            else:
                continue
            if name == "HEAD" or not (name == "release" or name.startswith("release/")):
                continue
            current = refs.get(name)
            if current is None or priority > current[0]:
                refs[name] = (priority, short_ref)
            if repo_id == selected_repositories[0] and name not in ordered:
                ordered.append(name)
        refs_by_repo[repo_id] = {name: ref for name, (_, ref) in refs.items()}
        names = set(refs_by_repo[repo_id])
        if common is None:
            common = names
        else:
            common.intersection_update(names)

    candidates: list[HomeBase] = []
    for name in ordered:
        if name not in (common or set()):
            continue
        bases = tuple(
            (repo_id, refs_by_repo[repo_id][name]) for repo_id in selected_repositories
        )
        base = bases[0][1]
        selection = HomeBase(
            base,
            f"发布分支 {name}（所有已配置仓库均可解析）",
            tuple((repo_id, ref) for repo_id, ref in bases if ref != base),
        )
        if not _unresolved_base_repositories(config, selected_repositories, selection):
            candidates.append(selection)
        if len(candidates) == limit:
            break
    return tuple(candidates)


def _repository_base_suggestions(
    config: Config, repo_id: str, current_base: str
) -> tuple[str, ...]:
    """Return safe, local candidates when a single repository needs a base override."""
    anchor = repository_path(config, repo_id)
    result = git(
        anchor,
        "for-each-ref",
        "--format=%(refname:short)",
        "refs/heads/main",
        "refs/heads/master",
        "refs/heads/release",
        "refs/remotes/origin/main",
        "refs/remotes/origin/master",
        "refs/remotes/origin/release",
        "refs/tags",
    )
    candidates = [current_base]
    if result.code == 0:
        candidates.extend(
            ref.strip() for ref in result.stdout.splitlines() if ref.strip()
        )
    return tuple(
        dict.fromkeys(
            ref
            for ref in candidates
            if git(anchor, "rev-parse", "--verify", f"{ref}^{{commit}}").code == 0
        )
    )


def _ask_repository_base(
    config: Config, repo_id: str, current_base: str
) -> str | object | None:
    suggestions = _repository_base_suggestions(config, repo_id, current_base)
    manual_choice = HomeChoice("", "其他：输入分支、tag 或 commit SHA")
    choices = tuple(
        HomeChoice(
            base,
            f"{base}{'（当前映射）' if base == current_base else ''}",
            recommended=base == current_base,
        )
        for base in suggestions
    ) + (manual_choice,)
    choice = _ask_choice(f"选择 {repo_id} 的已核实生产基线：", choices, allow_back=True)
    if _is_back(choice):
        return _BACK
    if choice is None:
        return None
    if choice is not manual_choice:
        return choice.value
    anchor = repository_path(config, repo_id)
    while True:
        raw = input(
            f"{repo_id} 的生产基线（分支、tag 或 commit SHA；b 上一步，q 取消）："
        ).strip()
        if raw.lower() in {"q", "quit"}:
            print("已取消；没有修改任何 Git 工作区。")
            return None
        if raw.lower() in {"b", "back", "返回"}:
            return _BACK
        if not raw:
            print("生产基线不能为空；请重新输入，或输入 q 取消。")
            continue
        if git(anchor, "rev-parse", "--verify", f"{raw}^{{commit}}").code == 0:
            return raw
        print(f"{repo_id} 无法解析 {raw}；该仓库当前没有这个分支、tag 或提交。")


def _ask_repository_base_overrides(
    config: Config,
    selection: HomeBase,
    *,
    kind: str,
    repositories: tuple[str, ...] | None = None,
) -> HomeBase | object | None:
    selected_repositories = repositories or tuple(config.repositories)
    if len(selected_repositories) == 1:
        return selection
    initial_mapping = {
        repo_id: selection.base_for(repo_id) for repo_id in selected_repositories
    }
    print("\n当前仓库基线：")
    _print_repository_base_map(selected_repositories, selection)
    scope = _ask_choice(
        f"确认{kind}的仓库基线：",
        (
            HomeChoice("keep", "使用当前映射", recommended=True),
            HomeChoice("adjust", "按仓库单独调整基线"),
        ),
        allow_back=True,
    )
    if _is_back(scope):
        return _BACK
    if scope is None:
        return None
    if scope.value == "keep":
        return selection

    bases = dict(initial_mapping)
    while True:
        choices = tuple(
            HomeChoice(repo_id, f"{repo_id}：{bases[repo_id]}")
            for repo_id in selected_repositories
        ) + (HomeChoice("done", "完成仓库基线设置", recommended=True),)
        repository_choice = _ask_choice("选择要调整的仓库：", choices, allow_back=True)
        if _is_back(repository_choice):
            return _BACK
        if repository_choice is None:
            return None
        if repository_choice.value == "done":
            overrides = tuple(
                (repo_id, base)
                for repo_id, base in bases.items()
                if base != selection.base
            )
            return HomeBase(selection.base, selection.label, overrides)
        repo_id = repository_choice.value
        base = _ask_repository_base(config, repo_id, bases[repo_id])
        if _is_back(base):
            continue
        if base is None:
            return None
        bases[repo_id] = base


def _ask_manual_hotfix_base(kind: str) -> HomeBase | object | None:
    while True:
        raw = input(f"已核实的生产 {kind}（b 上一步，q 取消）：").strip()
        if raw.lower() in {"q", "quit"}:
            print("已取消；没有修改任何 Git 工作区。")
            return None
        if raw.lower() in {"b", "back", "返回"}:
            return _BACK
        if raw:
            return HomeBase(raw, raw)
        print("生产基线不能为空；Dyro 不会替你猜测或默认选择 main。")


def _ask_hotfix_base(
    config: Config, repositories: tuple[str, ...]
) -> HomeBase | object | None:
    candidates = _shared_tag_candidates(
        config, repositories=repositories
    ) + _shared_release_branch_candidates(config, repositories=repositories)
    if candidates:
        manual_choice = HomeChoice(
            "", "其他：输入已核实的 release 分支、tag 或部署 SHA"
        )
        choices = tuple(
            HomeChoice(
                str(index),
                candidate.label,
                recommended=index == 0,
            )
            for index, candidate in enumerate(candidates)
        ) + (manual_choice,)
        print("Dyro 找到以下共同 Git 基线；它们只是可解析的候选，不代表已部署到生产。")
        choice = _ask_choice("选择你已核实的生产基线：", choices, allow_back=True)
        if _is_back(choice):
            return _BACK
        if choice is None:
            return None
        if choice is not manual_choice:
            return _ask_repository_base_overrides(
                config,
                candidates[int(choice.value)],
                kind="Hotfix",
                repositories=repositories,
            )
        selection = _ask_manual_hotfix_base("基线（release 分支、tag 或部署 SHA）")
        if _is_back(selection):
            return _BACK
        normalized = (
            _normalize_repository_bases(config, repositories, selection.base)
            if selection is not None
            else None
        )
        if _is_back(normalized):
            return _BACK
        return (
            _ask_repository_base_overrides(
                config, normalized, kind="Hotfix", repositories=repositories
            )
            if normalized is not None
            else None
        )

    print("Dyro 没有发现可供推荐的共同 Git 基线；请按已核实的部署记录选择基线形式。")
    choice = _ask_choice(
        "选择生产基线形式：",
        (
            HomeChoice("release 分支", "已发布的 release 分支", recommended=True),
            HomeChoice("tag", "已发布的 tag"),
            HomeChoice("部署 SHA", "已部署的 commit SHA"),
        ),
        allow_back=True,
    )
    if _is_back(choice):
        return _BACK
    if choice is None:
        return None
    selection = _ask_manual_hotfix_base(choice.value)
    if _is_back(selection):
        return _BACK
    normalized = (
        _normalize_repository_bases(config, repositories, selection.base)
        if selection is not None
        else None
    )
    if _is_back(normalized):
        return _BACK
    return (
        _ask_repository_base_overrides(
            config, normalized, kind="Hotfix", repositories=repositories
        )
        if normalized is not None
        else None
    )


def _create_hotfix_from_home(config: Config, dry_run: bool) -> Line | None:
    print("\n" + title("━━ 处理线上问题 / Hotfix ━━"))
    print(muted("Hotfix 需要已核实的生产 release 分支、tag 或部署 SHA。"))
    print(muted("Dyro 只校验 Git 可解析性；是否已部署到生产仍须由你确认。"))
    print(muted("步骤：问题 ID → 参与仓库 → 生产基线 → 创建确认"))
    step = 1
    issue_id = ""
    repositories: tuple[str, ...] = ()
    while True:
        if step == 1:
            issue_id = _ask_hotfix_id()
            if issue_id is None:
                return None
            step = 2
            continue
        if step == 2:
            selected_repositories = _ask_line_repositories(config)
            if _is_back(selected_repositories):
                step = 1
                continue
            if selected_repositories is None:
                return None
            repositories = selected_repositories
            step = 3
            continue
        if step == 3:
            base_selection = _ask_hotfix_base(config, repositories)
            if _is_back(base_selection):
                step = _previous_editable_step(config)
                continue
            if base_selection is None:
                return None
            unresolved = _unresolved_base_repositories(
                config, repositories, base_selection
            )
            if not unresolved:
                step = 4
                continue
            _print_unresolved_bases(unresolved)
            continue

        branch = f"hotfix/{issue_id}"
        try:
            planned = preflight_line(
                config,
                line_id=issue_id,
                branch=branch,
                base=base_selection.base,
                repositories=repositories,
                repository_bases=base_selection.overrides(),
                kind="hotfix",
            )
        except (DyroError, ValidationError, OSError) as exc:
            if _retry_after_preflight_failure("Hotfix", exc):
                step = 1
                continue
            return None
        print("\n" + title("━━ 创建前确认 ━━"))
        print(f"  问题 ID：{value(issue_id)}")
        print(f"  分支：{value(branch)}")
        print(f"  已核实的生产基线：{value(base_selection.base)}")
        if base_selection.repository_bases:
            print("  仓库基线：")
            _print_repository_base_map(planned.repositories, planned)
        print(f"  仓库：{value('、'.join(planned.repositories))}")
        print(f"  位置：{value(line_root(config, planned))}")
        if dry_run:
            print("DRY RUN: 已展示创建计划；不会创建 Git worktree。")
            return None

        confirmation = (
            input(
                "\n确认该基线已在生产核实，并创建这些隔离 worktree？[y/b/N；b 返回基线]："
            )
            .strip()
            .lower()
        )
        if confirmation in {"b", "back", "返回"}:
            step = 3
            continue
        if confirmation not in {"y", "yes"}:
            print("已取消；没有修改任何 Git 工作区。")
            return None
        try:
            line = create_line(
                config,
                line_id=issue_id,
                branch=branch,
                base=base_selection.base,
                repositories=repositories,
                repository_bases=base_selection.overrides(),
                kind="hotfix",
            )
        except (DyroError, ValidationError, OSError) as exc:
            print(danger(f"创建 Hotfix 失败：{exc}"))
            print(warning("没有完整创建 Hotfix；请重新运行 dyro 后重试。"))
            return None
        print(
            success("已创建 Hotfix：")
            + value(line.id)
            + "。接下来选择编码工具并打开隔离工作区。"
        )
        return line


def _run_config_home(
    config: Config, record: WorkspaceRecord | None, dry_run: bool
) -> None:
    print("\n" + title(f"━━ {config.name} ━━"))
    print(f"工作区  {value(config.root)}")
    failures = [finding for finding in doctor(config) if finding.startswith("FAIL")]
    if failures:
        print(
            f"\n检测到 {len(failures)} 个结构问题；只会阻止进入受影响的目标。"
            "运行 dyro doctor 查看详情。"
        )
    action = _choose_action(
        _targets(config, record), can_switch=len(load_registry().workspaces) > 1
    )
    if action is None:
        return
    kind, target_id = action
    if kind == "status":
        print_status(config)
        return
    if kind == "console":
        from .console.launcher import launch_console, render_console_plan

        initial_workspace = record.name if record else config.name
        target_root = None if record else config.root
        if dry_run:
            render_console_plan(
                port=0,
                no_open=False,
                initial_workspace=initial_workspace,
                target_root=target_root,
            )
        else:
            launch_console(
                initial_workspace=initial_workspace,
                target_root=target_root,
            )
        return
    if kind == "switch":
        _switch_workspace(dry_run)
        return
    if kind == "new-line":
        line = _create_line_from_home(config, dry_run)
        if line is None:
            return
        kind, target_id = line.kind, line.id
    if kind == "new-hotfix":
        line = _create_hotfix_from_home(config, dry_run)
        if line is None:
            return
        kind, target_id = line.kind, line.id
    if kind == "task":
        task = load_task(config, target_id)
        target_workspace = existing_task_workspace(config, task)
    else:
        _, target_workspace = existing_line_workspace(config, target_id, kind)
    tool = _choose_tool(config, record, workspace=target_workspace, dry_run=dry_run)
    if tool is None:
        return
    if record and not dry_run:
        mark_workspace_used(
            record.name, target_kind=kind, target_id=target_id, agent=tool.id
        )
    if tool.kind == "launcher":
        launch_home_tool(workspace=target_workspace, tool=tool, dry_run=dry_run)
    elif kind == "task":
        open_task(config, target_id, agent=tool.id, prompt="", dry_run=dry_run)
    else:
        open_line(
            config, target_id, kind=kind, agent=tool.id, prompt="", dry_run=dry_run
        )


def run_home(*, root: str | None, workspace: str | None, dry_run: bool) -> None:
    try:
        resolved = resolve_home_config(root=root, workspace=workspace, dry_run=dry_run)
        if resolved is None:
            _first_use(dry_run)
            return
        config, record = resolved
        _run_config_home(config, record, dry_run)
    except (KeyboardInterrupt, EOFError):
        print("\n已中断当前引导；没有执行后续步骤。")

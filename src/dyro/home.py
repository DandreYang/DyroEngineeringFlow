from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shlex
import shutil
import sys

from .config import CONFIG_NAME, Config, expand_argv, load
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
)
from .workspace import Line, doctor, get_line, line_root, list_lines, status_rows


DISCOVERABLE_AGENTS = tuple(
    (definition.command, definition.id == "codex")
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


def interactive_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def print_status(config: Config) -> None:
    print(
        f"{'SCOPE':24} {'REPOSITORY':14} {'BRANCH':24} {'HEAD':12} {'DIRTY':>5} UPSTREAM"
    )
    for scope, repo_id, branch, head, upstream, dirty in status_rows(config):
        dirty_text = "-" if dirty < 0 else str(dirty)
        print(
            f"{scope:24} {repo_id:14} {branch:24} {head:12} {dirty_text:>5} {upstream}"
        )


def print_all_status() -> None:
    registry = load_registry()
    if not registry.workspaces:
        print("还没有登记全局工作区。下一步：dyro workspace add <路径>")
        return
    for index, record in enumerate(registry.workspaces):
        if index:
            print()
        print(f"工作区：{record.name} ({record.root})")
        try:
            config = load(record.root)
        except (DyroError, ValidationError) as exc:
            print(f"不可用：{exc}")
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
    definition: ToolDefinition, *, config: Config, workspace: Path
) -> HomeTool:
    if definition.id == "cursor-desktop":
        argv, installed = _cursor_desktop_argv(workspace)
    elif definition.id == "zcode":
        argv, installed = _zcode_desktop_argv(workspace)
    else:
        argv = expand_argv(
            definition.launch,
            workspace=workspace,
            root=config.root,
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
                root=config.root,
                task="",
                line="",
                prompt="",
            ),
        )
        for key, value in definition.environment
    )
    if installed:
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


def home_tools(config: Config, *, workspace: Path) -> list[HomeTool]:
    tools: list[HomeTool] = []
    configured_commands: set[str] = set()
    for adapter_id in sorted(config.adapters):
        executable = _adapter_executable(config, adapter_id, workspace=workspace)
        command = Path(executable).name
        configured_commands.add(command)
        definition = tool_definition_for_command(command)
        installed = executable_available(executable, cwd=workspace)
        tools.append(
            HomeTool(
                adapter_id,
                _tool_label(adapter_id, executable),
                "adapter",
                (),
                (),
                (
                    ToolState.READY
                    if installed
                    else ToolState.INSTALLABLE
                    if definition and definition.install
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
        tools.append(_launcher_tool(definition, config=config, workspace=workspace))

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
    print(f"{'命令':16} {'本机':10} {'Dyro 状态':16} 说明")
    known_commands = {command for command, _ in DISCOVERABLE_AGENTS}
    for command, integrated in DISCOVERABLE_AGENTS:
        definition = tool_definition_for_command(command)
        installed = shutil.which(command) is not None
        configured_as = ",".join(configured_commands.get(command, ()))
        if configured_as and installed:
            state = f"已配置:{configured_as}"
            note = "可由当前 Profile 启动"
        elif configured_as:
            state = f"已配置但不可用:{configured_as}"
            note = "命令不可用；请检查安装或 adapter 配置"
        elif installed and integrated:
            state = "尚未配置"
            note = f"运行 dyro agent add {command} --preset {command}"
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
        print(f"{command:16} {'已检测' if installed else '-':10} {state:16} {note}")
    cursor_definition = next(
        definition
        for definition in TOOL_DEFINITIONS
        if definition.id == "cursor-desktop"
    )
    cursor_desktop = _launcher_tool(
        cursor_definition, config=config, workspace=config.root
    )
    desktop_state = "已检测" if cursor_desktop.available else "-"
    desktop_note = (
        "首页可打开工作区；不获得执行、门禁或复核权限"
        if cursor_desktop.available
        else "未安装；可运行 dyro tool install cursor-desktop"
    )
    desktop_integration = "尚未集成" if cursor_desktop.available else "-"
    print(
        f"{'cursor-desktop':16} {desktop_state:10} "
        f"{desktop_integration:16} {desktop_note}"
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
        print(f"{executable:16} {'已检测' if installed else '-':10} {state:16} {note}")


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
    agent: str,
    prompt: str,
    dry_run: bool,
) -> None:
    line, workspace = existing_line_workspace(config, line_id, kind)
    launch_adapter(
        config,
        workspace=workspace,
        adapter_id=agent,
        line=line.id,
        prompt=prompt,
        dry_run=dry_run,
    )


def open_task(
    config: Config, task_id: str, *, agent: str, prompt: str, dry_run: bool
) -> None:
    task = load_task(config, task_id)
    launch_adapter(
        config,
        workspace=existing_task_workspace(config, task),
        adapter_id=agent,
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
        print(f"已将当前项目加入全局首页：{record.name}")
    return config, record


def _first_use(dry_run: bool) -> None:
    print("欢迎使用 Dyro。这里还没有可直接打开的工作区。")
    print("  1) 加入团队工作区：dyro join <蓝图地址>")
    print("  2) 设置一个新项目：dyro setup")
    print("  3) 登记已有工作区：dyro workspace add <路径>")
    if not interactive_terminal():
        print("在交互终端中再次运行 dyro，可直接选择下一步。")
        return
    choice = input("\n请选择（直接回车默认加入团队工作区，q 退出）：").strip().lower()
    if choice in {"q", "quit"}:
        print("已退出；没有修改任何文件。")
    elif choice in {"", "1"}:
        source = input("团队蓝图地址或本地文件：").strip()
        if source:
            print(f"下一步：dyro join {shlex.quote(source)}")
        else:
            print("下一步：dyro join <蓝图地址>")
    elif choice == "2":
        print("下一步：dyro setup（确认设置计划前不会修改任何文件）")
    elif choice == "3":
        raw_path = input("已有 Dyro 工作区路径：").strip()
        if not raw_path:
            print("已取消；没有修改任何文件。")
        elif dry_run:
            config = load(Path(raw_path).expanduser())
            print(f"DRY RUN: 将登记工作区 {config.name} -> {config.root.resolve()}")
        else:
            config = load(Path(raw_path).expanduser())
            record = add_workspace(config.root, name=config.name, make_default=True)
            print(f"已登记工作区：{record.name}。再次运行 dyro 即可进入。")
    else:
        raise DyroError("请选择 1、2、3 或 q")


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
    print("\n今天做什么？\n")
    for index, target in enumerate(targets, start=1):
        print(f"  {index}) {target.label}")
    status_index = len(targets) + 1
    print(f"  {status_index}) 查看当前项目状态")
    console_index = status_index + 1
    print(f"  {console_index}) 查看全部项目控制台")
    switch_index = console_index + 1
    if can_switch:
        print(f"  {switch_index}) 切换项目")
    new_line_index = switch_index + 1 if can_switch else console_index + 1
    new_hotfix_index = new_line_index + 1
    print(f"  {new_line_index}) 开启新的功能开发线")
    print(f"  {new_hotfix_index}) 处理新的线上问题 / Hotfix")
    print("  q) 退出")
    if not interactive_terminal():
        print(
            "\n在交互终端运行 dyro 可选择并直接进入；也可使用 dyro open 或 dyro task open。"
        )
        return None
    default = "1" if targets else str(status_index)
    raw = input(f"\n请选择（直接回车默认 {default}）：").strip().lower() or default
    if raw in {"q", "quit"}:
        return None
    if not raw.isdigit():
        raise DyroError("请输入菜单编号或 q")
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
    raise DyroError("菜单编号超出范围")


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
        tool
        for tool in tools
        if tool.available or tool.state == ToolState.INSTALLABLE
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
        print("\n全部编码工具：\n" if show_all else "\n常用编码工具：\n")
        for index, tool in enumerate(displayed, start=1):
            labels = _tool_state_labels(
                tool,
                default=default,
                last_tool=last_tool,
                recommended_tool=config.recommended_tool,
                preferences=preferences,
            )
            print(f"  {index}) {tool.label}（{'，'.join(labels)}）")
        if show_all:
            print("  b) 返回常用工具")
        else:
            hidden_ready = sum(
                tool.available and tool not in displayed for tool in tools
            )
            installable = sum(
                tool.state == ToolState.INSTALLABLE for tool in tools
            )
            details = []
            if hidden_ready:
                details.append(f"{hidden_ready} 个已安装")
            if installable:
                details.append(f"{installable} 个可安装")
            suffix = f"（{'，'.join(details)}）" if details else ""
            print(f"  m) 更多工具{suffix}")
        print("  q) 退出")
        raw = (
            input(
                f"\n请选择（编号或工具名，直接回车默认 {default.label}）："
            )
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
            selected = next((tool for tool in tools if _matches_home_tool(tool, raw)), None)
        if selected is None:
            raise DyroError("无效的编码工具选择")
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
    if selected.state == ToolState.UNAVAILABLE:
        raise DyroError(
            f"{selected.label} 未安装或不可执行，且暂无内置安装方案；"
            "运行 dyro agent discover 查看详情"
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
    print("请选择工作区：")
    for index, record in enumerate(available, start=1):
        print(f"  {index}) {record.name}")
    raw = input("编号：").strip()
    if not raw.isdigit() or not (1 <= int(raw) <= len(available)):
        raise DyroError("无效的工作区选择")
    record = available[int(raw) - 1]
    _run_config_home(load(record.root), record, dry_run)


def _print_new_line_guidance(config: Config) -> None:
    print("\n新功能开发线会创建隔离 Git worktree。")
    print("先确认功能 ID、范围、参与仓库与基线；确认后才会修改 Git 工作区。")
    print(
        "下一步："
        f"dyro line create <ID> --base {shlex.quote(config.policy.default_base)} --yes"
    )


def _print_new_hotfix_guidance() -> None:
    print("\nHotfix 需要已核实的生产 release 分支、tag 或部署 SHA。")
    print("确认问题 ID 和基线后，才会创建隔离的 Hotfix worktree。")
    print("下一步：dyro hotfix create <问题ID> --base <已核实的 release/tag/SHA> --yes")


def _run_config_home(
    config: Config, record: WorkspaceRecord | None, dry_run: bool
) -> None:
    print(f"\n项目：{config.name}")
    print(f"位置：{config.root}")
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
        _print_new_line_guidance(config)
        return
    if kind == "new-hotfix":
        _print_new_hotfix_guidance()
        return
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
    resolved = resolve_home_config(root=root, workspace=workspace, dry_run=dry_run)
    if resolved is None:
        _first_use(dry_run)
        return
    config, record = resolved
    _run_config_home(config, record, dry_run)

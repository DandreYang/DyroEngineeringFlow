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
from .workspace import Line, doctor, get_line, line_root, list_lines, status_rows


DISCOVERABLE_AGENTS = (
    ("codex", True),
    ("claude", False),
    ("cursor-agent", False),
    ("grok", False),
    ("opencode", False),
    ("hermes", False),
    ("kimi", False),
)


@dataclass(frozen=True)
class HomeTarget:
    kind: str
    id: str
    label: str


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


def print_agent_discovery(config: Config) -> None:
    configured_commands: dict[str, list[str]] = {}
    for adapter_id in config.adapters:
        command = Path(_adapter_executable(config, adapter_id)).name
        configured_commands.setdefault(command, []).append(adapter_id)
    print(f"{'命令':16} {'本机':10} {'Dyro 状态':16} 说明")
    known_commands = {command for command, _ in DISCOVERABLE_AGENTS}
    for command, integrated in DISCOVERABLE_AGENTS:
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
            note = "已检测到命令，但不会绕过 Profile 授权"
        else:
            state = "-"
            note = "未安装"
        print(f"{command:16} {'已检测' if installed else '-':10} {state:16} {note}")
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
    switch_index = status_index + 1
    if can_switch:
        print(f"  {switch_index}) 切换项目")
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
    if can_switch and selected == switch_index:
        return "switch", ""
    raise DyroError("菜单编号超出范围")


def _choose_agent(
    config: Config, record: WorkspaceRecord | None, *, workspace: Path
) -> str:
    available = available_adapters(config, workspace=workspace)
    if not available:
        print_agent_discovery(config)
        selector = f"--workspace {record.name}" if record else f"--root {config.root}"
        raise DyroError(
            f"当前项目没有可启动的 Agent。下一步：dyro {selector} agent add codex --preset codex"
        )
    default = (
        record.last_agent if record and record.last_agent in available else available[0]
    )
    if len(available) == 1:
        return available[0]
    print("\n使用哪个 Agent？\n")
    for index, adapter_id in enumerate(available, start=1):
        suffix = "（上次使用）" if adapter_id == default else ""
        print(f"  {index}) {adapter_id}{suffix}")
    raw = input(f"\n请选择（直接回车默认 {default}）：").strip()
    if not raw:
        return default
    if not raw.isdigit() or not (1 <= int(raw) <= len(available)):
        raise DyroError("无效的 Agent 选择")
    return available[int(raw) - 1]


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
    if kind == "switch":
        _switch_workspace(dry_run)
        return
    if kind == "task":
        task = load_task(config, target_id)
        target_workspace = existing_task_workspace(config, task)
    else:
        _, target_workspace = existing_line_workspace(config, target_id, kind)
    agent = _choose_agent(config, record, workspace=target_workspace)
    if record and not dry_run:
        mark_workspace_used(
            record.name, target_kind=kind, target_id=target_id, agent=agent
        )
    if kind == "task":
        open_task(config, target_id, agent=agent, prompt="", dry_run=dry_run)
    else:
        open_line(config, target_id, kind=kind, agent=agent, prompt="", dry_run=dry_run)


def run_home(*, root: str | None, workspace: str | None, dry_run: bool) -> None:
    resolved = resolve_home_config(root=root, workspace=workspace, dry_run=dry_run)
    if resolved is None:
        _first_use(dry_run)
        return
    config, record = resolved
    _run_config_home(config, record, dry_run)

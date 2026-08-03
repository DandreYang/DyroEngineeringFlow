from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import time

from . import __version__
from .blueprint import (
    BLUEPRINT_FILENAME,
    apply_join_plan,
    build_join_plan,
    load_blueprint_source,
    preflight_join_plan,
    render_join_plan,
)
from .changesets import create_changeset, get_changeset, list_changesets, verify_changeset
from .config import CONFIG_NAME, Config, load, validate_id
from .continuation.models import Operation, RequestedMode
from .continuation.planner import (
    build_continuation_plan,
    build_scheduler_projection,
    continuation_plan_payload,
    render_plan_text,
    render_projection_json,
    render_projection_mermaid,
)
from .continuation.resolution import resolve_line, resolve_workspace
from .continuation.snapshot import build_scheduler_snapshot
from .continuation.store import (
    add_objective_target,
    create_objective,
    derive_objective_result,
    get_objective,
    list_objectives,
    pause_objective,
    reconcile_objective,
    remove_objective_target,
    resume_objective,
    stop_objective,
)
from .evidence import build_execution_bundle, unpack_execution_bundle
from .errors import DyroError, ValidationError
from .home import (
    home_tools,
    open_line,
    open_task,
    print_agent_discovery,
    print_all_status,
    print_status,
    resolve_home_config,
    run_home,
    sort_home_tools,
)
from .hub import (
    add_workspace,
    get_workspace,
    load_registry,
    remove_workspace,
    set_default_workspace,
)
from .onboarding import (
    SetupPlan,
    append_repository,
    ask_for_workspace,
    bootstrap,
    current_branch,
    discover_repositories,
    is_git_repository,
    origin_url,
    repository_from_remote,
    render_config,
    render_setup_plan,
    repository_input_from_path,
    sibling_workspace_for,
)
from .profile import append_adapter, command_adapter, config_value, preset_adapter, set_config_value, test_adapter
from .state import atomic_write_text, exclusive_lock
from .graph import (
    build_task_graph,
    explain_task,
    render_task_explanation,
    render_task_graph,
    validate_task_graph,
)
from .provenance import (
    list_execution_attempts,
    render_execution_attempts,
    render_review_binding,
    review_binding,
)
from .signing import TRUST_PURPOSES
from .tasks import (
    STATUSES,
    answer_task,
    board,
    claim_task,
    decisions,
    import_execution_evidence,
    import_review_evidence,
    list_tasks,
    load_task,
    loop_tasks,
    merge_task,
    plan_tasks,
    review_task,
    run_gates,
    run_task,
    select_task_wave,
    set_status,
    signoff_task,
    stats,
    status as task_status,
    task_template,
)
from .terminology import load_terminology_policy, scan_terminology
from .tooling import (
    ToolState,
    install_tool,
    load_tool_preferences,
    set_default_tool,
    set_pinned_tools,
)
from .updates import (
    EXPLICIT_CHECK_TIMEOUT,
    UpdateKind,
    check_for_update,
    fetch_latest_version,
    load_update_state,
    perform_update,
    set_auto_patch,
    set_update_enabled,
)
from .workspace import create_line, doctor, get_line, list_lines, status_rows


CONFIG_TEMPLATE = '''schema_version = 1

[workspace]
name = "{name}"

[layout]
anchors = "repositories"
lines = "versions"
hotfixes = "hotfixes"
tasks = "worktrees"

[policy]
default_base = "main"
task_branch_prefix = "task/"
allow_push = false
require_clean_merge = true
# Set external when task execution and review must occur in a separately
# controlled runner. Local Dyro then permits planning only.
execution_mode = "local"
# Keep false for lightweight teams. When true, PASS review waits for task signoff.
require_external_signoff = false
require_signed_execution = false
require_signed_review = false
require_signed_signoff = false

# Add each repository anchor once.  A release line or task receives linked
# worktrees under the configured layout paths.
[repositories.api]
path = "repositories/services/api"
mount = "services/api"
verify = [["python3", "-m", "pytest", "-q"]]

[repositories.web]
path = "repositories/clients/web"
mount = "clients/web"
verify = [["npm", "test", "--", "--runInBand"]]
'''


def _config(args: argparse.Namespace) -> Config:
    root_arg = getattr(args, "root", None)
    workspace_arg = getattr(args, "workspace_alias", None)
    if root_arg:
        root = Path(root_arg).expanduser()
    elif workspace_arg:
        root = get_workspace(workspace_arg).root
    else:
        interactive = sys.stdin.isatty() and sys.stdout.isatty()
        return resolve_workspace(
            start=Path.cwd(),
            interactive=interactive,
            chooser=(lambda label, values: _choose(label, list(values))) if interactive else None,
        )
    return load(root)


def _repositories(raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    values = [item.strip() for item in raw.split(",") if item.strip()]
    if not values:
        raise DyroError("--repos 不能为空")
    return values


def _repository_assignments(values: list[str] | None, label: str) -> dict[str, str]:
    assignments: dict[str, str] = {}
    for value in values or []:
        repo_id, separator, assigned = value.partition("=")
        repo_id = repo_id.strip()
        assigned = assigned.strip()
        if not separator or not repo_id or not assigned:
            raise DyroError(f"{label} 必须使用 REPOSITORY=VALUE，例如 api=origin/main")
        validate_id(repo_id, "repository id")
        if repo_id in assignments:
            raise DyroError(f"{label} 不能重复指定同一仓库：{repo_id}")
        assignments[repo_id] = assigned
    return assignments


def _require_yes(args: argparse.Namespace, label: str) -> None:
    if not args.yes and not args.dry_run:
        raise DyroError(f"{label} 会创建或修改 Git worktree；请先使用 --dry-run 检查，再加 --yes 执行")


def _require_objective_yes(args: argparse.Namespace, label: str) -> None:
    if not args.yes and not args.dry_run:
        raise DyroError(f"{label} 会修改 Objective 状态；请先使用 --dry-run 检查，再加 --yes 执行")


def _objective_contract_from_args(args: argparse.Namespace, config: Config) -> str:
    if args.file:
        path = Path(args.file).expanduser()
        if not path.is_file() or path.is_symlink():
            raise DyroError(f"Objective 合约文件必须是安全的普通文件：{path}")
        return path.read_text(encoding="utf-8")
    if not args.id or not args.title:
        raise DyroError("非文件模式必须提供 --id 与 --title")
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    selected_line = args.line or resolve_line(
        config,
        interactive=interactive,
        chooser=(lambda label, values: _choose(label, list(values))) if interactive else None,
    ).id
    targets = tuple(item.strip() for item in (args.targets or "").split(",") if item.strip())
    if not targets:
        raise DyroError("非文件模式必须提供 --targets TASK_ID[,TASK_ID...]；交互选择将在后续版本提供")
    mode = args.mode or RequestedMode.SUPERVISED.value
    operations = tuple(args.operation or (Operation.EXECUTE.value, Operation.REVIEW.value))
    return "\n".join(
        (
            "schema_version = 1",
            f"id = {json.dumps(args.id, ensure_ascii=False)}",
            f"title = {json.dumps(args.title, ensure_ascii=False)}",
            f"line = {json.dumps(selected_line, ensure_ascii=False)}",
            "targets = [" + ", ".join(json.dumps(item, ensure_ascii=False) for item in targets) + "]",
            "",
            "[continuation]",
            f"requested_mode = {json.dumps(mode)}",
            "operations = [" + ", ".join(json.dumps(item) for item in operations) + "]",
            "",
        )
    )


def _print_objective(config: Config, record) -> None:
    result = derive_objective_result(config, record)
    print(
        f"{record.objective.id:28} {record.operator_state:8} {result:16} "
        f"r{record.revision:<3} {record.objective.line:20} {', '.join(record.objective.targets)}"
    )


def _print_command(argv: tuple[str, ...]) -> None:
    print("$ " + shlex.join(argv))


def cmd_init(args: argparse.Namespace) -> None:
    root = Path(args.path).expanduser().resolve()
    config_file = root / CONFIG_NAME
    if config_file.exists():
        raise DyroError(f"配置已存在：{config_file}")
    if args.dry_run:
        print(f"DRY RUN: 将创建 {config_file}")
        return
    root.mkdir(parents=True, exist_ok=True)
    if args.wizard:
        name, repositories, base = ask_for_workspace(args.name)
        content = render_config(name, repositories, base)
    elif args.discover:
        repositories = discover_repositories(root)
        if not repositories:
            raise DyroError("未发现 Git 仓库；可先 clone 仓库，或使用 dyro init --wizard")
        content = render_config(args.name, repositories, args.base)
    else:
        content = CONFIG_TEMPLATE.format(name=args.name)
    atomic_write_text(config_file, content)
    for relative in (".dyro/tasks", ".dyro/lines", ".dyro/hotfixes", ".dyro/changes"):
        (root / relative).mkdir(parents=True, exist_ok=True)
    print(f"已初始化 {root}")
    if args.discover:
        print(f"已自动登记 {len(repositories)} 个本地 Git 仓库；下一步：运行 dyro doctor")
    else:
        print("下一步：登记 repositories，随后运行 dyro doctor")


def _default_workspace_name(root: Path) -> str:
    candidate = "".join(character if character.isascii() and character.isalnum() else "-" for character in root.name).strip("-._")
    candidate = candidate or "my-workspace"
    if not candidate[0].isalnum():
        candidate = "workspace-" + candidate
    return candidate[:80]


def _ensure_state_directories(root: Path) -> None:
    for relative in (".dyro/tasks", ".dyro/lines", ".dyro/hotfixes", ".dyro/changes"):
        (root / relative).mkdir(parents=True, exist_ok=True)


def _ask_value(prompt: str, *, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt}{suffix}：").strip()
    return value or default


def _ask_yes_no(prompt: str, *, default: bool = False) -> bool:
    rendered_default = "Y/n" if default else "y/N"
    value = input(f"{prompt} [{rendered_default}]：").strip().lower()
    if not value:
        return default
    if value in {"y", "yes", "是"}:
        return True
    if value in {"n", "no", "否"}:
        return False
    raise DyroError("请选择 y 或 n")


def _setup_provider_preset() -> str | None:
    """Offer only a Provider that Core can configure and execute safely today."""

    discovered = [
        command
        for command in ("codex", "claude", "cursor-agent", "grok", "opencode", "hermes", "kimi")
        if shutil.which(command)
    ]
    if not discovered:
        print("未发现本机 Agent；可稍后运行 dyro agent add <id> --command '…'。")
        return None
    if "codex" in discovered:
        if _ask_yes_no("检测到 Codex。将它加入 Dyro Profile 吗", default=True):
            return "codex"
    unsupported = [command for command in discovered if command != "codex"]
    if unsupported:
        print(
            "已发现 "
            + "、".join(unsupported)
            + "；它们尚无 Core 的受审计适配器，因此不会写入配置。"
        )
    return None


def _render_interactive_setup_plan(plan: SetupPlan) -> None:
    print("\n设置计划（尚未修改任何文件）：")
    for item in render_setup_plan(plan):
        print("  - " + item)
    if plan.needs_bootstrap:
        print("  - 将仅 clone 缺失且已明确提供 remote 的仓库")
    print("  - 不会移动、覆盖或清理现有 Git 仓库")


def _apply_setup_plan(plan: SetupPlan, *, dry_run: bool) -> None:
    if dry_run:
        print("DRY RUN: 上述计划不会写入文件、执行 clone 或创建 Git worktree")
        return
    config_file = plan.root / CONFIG_NAME
    if config_file.exists():
        raise DyroError(f"配置已存在：{config_file}")
    plan.root.mkdir(parents=True, exist_ok=True)
    adapter_presets = (plan.provider_preset,) if plan.provider_preset else ()
    atomic_write_text(
        config_file,
        render_config(
            plan.name,
            list(plan.repositories),
            plan.default_base,
            adapter_presets=adapter_presets,
        ),
    )
    config = load(plan.root)
    _ensure_state_directories(config.root)
    if plan.needs_bootstrap:
        for message in bootstrap(config, branch=plan.default_base):
            print(message)
    if plan.line_id:
        line = create_line(
            config,
            line_id=plan.line_id,
            branch=plan.branch or f"feat/{plan.line_id}",
            base=plan.default_base,
            kind="line",
        )
        print(f"已创建开发线 {line.id}（{line.branch}）")
    findings = doctor(config)
    for finding in findings:
        print(finding)
    if any(finding.startswith("FAIL") for finding in findings):
        raise DyroError("设置已保存，但 doctor 发现问题；请修复后运行 dyro next")
    print("\n设置完成。下一步：dyro next")


def _interactive_setup(args: argparse.Namespace) -> None:
    root = Path(args.path).expanduser().resolve()
    config_file = root / CONFIG_NAME
    suggested_base = args.base or "main"
    print("Dyro 首次设置。先检查环境，确认前不会修改文件。")
    if config_file.exists():
        config = load(root)
        print(f"已发现 Profile：{config_file}（{len(config.repositories)} 个仓库）")
        for finding in doctor(config):
            print(finding)
        print("下一步：dyro next")
        return

    repositories = discover_repositories(root) if root.exists() and not is_git_repository(root) else []
    if is_git_repository(root):
        remote = origin_url(root)
        if not remote:
            print("当前目录是 Git 仓库，但没有 origin。Dyro 不会把控制状态写进该仓库。")
            print("请先为它配置 origin，或在包含多个仓库的独立目录中运行 dyro setup。")
            return
        source_branch = current_branch(root)
        if source_branch and not args.base:
            suggested_base = source_branch
        suggested_root = sibling_workspace_for(root)
        raw_root = _ask_value("为这个项目创建独立 Dyro 工作区", default=str(suggested_root))
        root = Path(raw_root).expanduser().resolve()
        if root.exists() and any(root.iterdir()):
            raise DyroError(f"建议工作区必须为空或不存在：{root}")
        repository = repository_from_remote(remote)
        repositories = [repository]
        print("将从当前仓库的 origin clone 新 anchor；当前仓库保持不变。")
    elif not repositories:
        remote = _ask_value("未发现本地 Git 仓库。输入一个 Git remote（留空退出）")
        if not remote:
            print("已取消；没有修改任何文件。")
            return
        repositories = [repository_from_remote(remote)]

    name = _ask_value("工作区名称", default=args.name or _default_workspace_name(root))
    validate_id(name, "workspace 名称")
    base = _ask_value("默认基线分支", default=suggested_base)
    line_id = _ask_value("首条开发线 ID（留空则仅创建 Profile）", default="dev")
    if line_id:
        validate_id(line_id, "开发线 ID")
    provider = _setup_provider_preset()
    plan = SetupPlan(
        root=root,
        name=name,
        repositories=tuple(repositories),
        default_base=base,
        line_id=line_id or None,
        branch=f"feat/{line_id}" if line_id else None,
        provider_preset=provider,
    )
    _render_interactive_setup_plan(plan)
    if args.dry_run:
        _apply_setup_plan(plan, dry_run=True)
        return
    if not args.yes and not _ask_yes_no("应用此设置计划", default=False):
        print("已取消；没有修改任何文件。")
        return
    _apply_setup_plan(plan, dry_run=False)


def _setup_quick(args: argparse.Namespace) -> None:
    root = Path(args.path).expanduser().resolve()
    config = load(root)
    print(f"检查现有 Profile：{config.root}")
    for finding in doctor(config):
        print(finding)
    print("下一步：dyro next")


def _non_interactive_setup(args: argparse.Namespace) -> None:
    """Create a usable Profile and, optionally, its first safe development line."""
    root = Path(args.path).expanduser().resolve()
    config_file = root / CONFIG_NAME
    created = False
    if config_file.exists():
        config = load(root)
        print(f"复用已有 Profile：{config_file}")
    else:
        repositories = discover_repositories(root)
        if not repositories:
            raise DyroError("未发现 Git 仓库；请先 clone 仓库到工作区，或使用 dyro init --wizard")
        name = args.name or _default_workspace_name(root)
        validate_id(name, "workspace 名称")
        if args.dry_run:
            print(f"DRY RUN: 将创建 {config_file}，自动登记 {len(repositories)} 个 Git 仓库")
            if not args.no_line:
                print(f"DRY RUN: 将创建开发线 {args.line}（分支 {args.branch or f'feat/{args.line}'}）")
            return
        root.mkdir(parents=True, exist_ok=True)
        atomic_write_text(config_file, render_config(name, repositories, args.base or "main"))
        config = load(root)
        created = True
        print(f"已创建 Profile，并自动登记 {len(repositories)} 个 Git 仓库")
    if not args.dry_run:
        _ensure_state_directories(config.root)
    if not args.no_line:
        _require_yes(args, "setup 创建开发线")
        try:
            existing = get_line(config, args.line, "line")
        except DyroError:
            branch = args.branch or f"feat/{args.line}"
            line = create_line(
                config,
                line_id=args.line,
                branch=branch,
                base=args.base or config.policy.default_base,
                kind="line",
                dry_run=args.dry_run,
            )
            print(f"{'DRY RUN: ' if args.dry_run else ''}已创建开发线 {line.id}（{line.branch}）")
        else:
            print(f"开发线已存在：{existing.id}（{existing.branch}）")
    findings = doctor(config)
    for finding in findings:
        print(finding)
    if any(finding.startswith("FAIL") for finding in findings):
        raise DyroError("setup 已完成基础配置，但 doctor 仍发现结构错误")
    if created:
        print("下一步：dyro next")


def _uses_interactive_setup(args: argparse.Namespace) -> bool:
    if args.interactive:
        return True
    if args.non_interactive or args.quick:
        return False
    has_explicit_plan = any(
        (
            args.name,
            args.base,
            args.branch,
            args.no_line,
            args.yes,
            args.line != "dev",
        )
    )
    return sys.stdin.isatty() and not has_explicit_plan


def cmd_setup(args: argparse.Namespace) -> None:
    if args.quick:
        _setup_quick(args)
        return
    if _uses_interactive_setup(args):
        _interactive_setup(args)
        return
    _non_interactive_setup(args)


def cmd_repo_list(args: argparse.Namespace) -> None:
    config = _config(args)
    print(f"{'ID':18} {'ANCHOR':36} {'MOUNT':28} REMOTE")
    for repository_id, repository in sorted(config.repositories.items()):
        print(f"{repository_id:18} {repository.path:36} {repository.mount:28} {'configured' if repository.remote else '-'}")


def cmd_repo_add(args: argparse.Namespace) -> None:
    config = _config(args)
    repository = repository_input_from_path(
        config.root,
        args.path,
        repository_id=args.id,
        mount=args.mount,
        remote=args.remote,
    )
    if repository.id in config.repositories:
        raise DyroError(f"仓库已配置：{repository.id}")
    if args.dry_run:
        print(
            "DRY RUN: 将登记 "
            f"{repository.id} path={repository.path} mount={repository.mount} remote={'configured' if repository.remote else '-'}"
        )
        return
    append_repository(config, repository)
    print(f"已登记仓库：{repository.id}（{repository.path} -> {repository.mount}）")


def cmd_bootstrap(args: argparse.Namespace) -> None:
    _require_yes(args, "bootstrap")
    config = _config(args)
    for message in bootstrap(config, dry_run=args.dry_run):
        print(message)
    if not args.dry_run:
        for finding in doctor(config):
            print(finding)


def cmd_doctor(args: argparse.Namespace) -> None:
    findings = doctor(_config(args))
    for finding in findings:
        print(finding)
    if any(item.startswith("FAIL") for item in findings):
        raise DyroError("doctor 发现结构错误")


def cmd_terminology_check(args: argparse.Namespace) -> None:
    root = _config(args).root if args.workspace_alias else Path(args.root or ".").expanduser().resolve()
    policy = load_terminology_policy(
        root,
        policy_file=Path(args.policy_file) if args.policy_file else None,
    )
    result = scan_terminology(
        root,
        policy,
        base_ref=args.base_ref,
        candidate_messages=tuple(args.message),
    )
    print(json.dumps(result.as_dict(), ensure_ascii=False, sort_keys=True))
    if result.violations:
        raise DyroError("术语策略扫描发现不合规候选；详情仅显示位置与计数")


def cmd_home(args: argparse.Namespace) -> None:
    run_home(
        root=getattr(args, "root", None),
        workspace=getattr(args, "workspace_alias", None),
        dry_run=args.dry_run,
    )


def cmd_workspace_add(args: argparse.Namespace) -> None:
    if args.dry_run:
        config = load(Path(args.path).expanduser())
        print(
            "DRY RUN: 将登记工作区 "
            f"{args.name or config.name} -> {config.root.resolve()}"
        )
        return
    record = add_workspace(
        args.path,
        name=args.name,
        make_default=args.default,
    )
    print(f"已登记工作区：{record.name} -> {record.root}")


def cmd_workspace_list(args: argparse.Namespace) -> None:
    registry = load_registry()
    if not registry.workspaces:
        print("还没有登记全局工作区。下一步：dyro workspace add <路径>")
        return
    print(f"{'默认':4} {'名称':20} {'状态':8} 路径")
    for record in registry.workspaces:
        marker = "*" if record.name == registry.default else "-"
        try:
            load(record.root)
        except (DyroError, ValidationError):
            state = "不可用"
        else:
            state = "可用"
        print(f"{marker:4} {record.name:20} {state:8} {record.root}")


def cmd_workspace_default(args: argparse.Namespace) -> None:
    if args.dry_run:
        get_workspace(args.name)
        print(f"DRY RUN: 将默认工作区设为 {args.name}")
        return
    set_default_workspace(args.name)
    print(f"默认工作区：{args.name}")


def cmd_workspace_remove(args: argparse.Namespace) -> None:
    get_workspace(args.name)
    if not args.yes and not args.dry_run:
        raise DyroError(
            "移除只会删除全局首页入口，不会删除项目文件；确认后请加 --yes"
        )
    if args.dry_run:
        print(f"DRY RUN: 将移除工作区入口 {args.name}；不会删除项目文件")
        return
    remove_workspace(args.name)
    print(f"已移除工作区入口：{args.name}；项目文件未改动")


def _blueprint_document(args: argparse.Namespace):
    return load_blueprint_source(
        args.source,
        git_ref=args.blueprint_ref,
        blueprint_file=args.blueprint_file,
    )


def cmd_blueprint_validate(args: argparse.Namespace) -> None:
    document = _blueprint_document(args)
    blueprint = document.blueprint
    print(f"PASS 蓝图：{blueprint.name}")
    print(f"来源：{document.source}")
    print(f"SHA-256：{document.sha256}")
    print(f"仓库：{len(blueprint.repositories)} 个")
    if blueprint.recommended_tool:
        print(f"推荐编码工具：{blueprint.recommended_tool}（仅推荐，不自动安装）")
    print(
        "开发线："
        + "、".join(
            line_id + ("（默认）" if line_id == blueprint.default_line else "")
            for line_id in blueprint.lines
        )
    )


def _select_blueprint_line(args: argparse.Namespace, document) -> str:
    blueprint = document.blueprint
    if args.line:
        return args.line
    if len(blueprint.lines) == 1 or not sys.stdin.isatty():
        return blueprint.default_line
    line_ids = list(blueprint.lines)
    print("\n请选择开发线：")
    for index, line_id in enumerate(line_ids, start=1):
        suffix = "（默认）" if line_id == blueprint.default_line else ""
        print(f"  {index}) {line_id}{suffix}")
    default_index = line_ids.index(blueprint.default_line) + 1
    answer = input(f"编号（直接回车默认 {default_index}）：").strip()
    if not answer:
        return blueprint.default_line
    if not answer.isdigit() or not 1 <= int(answer) <= len(line_ids):
        raise DyroError("无效的开发线选择")
    return line_ids[int(answer) - 1]


def cmd_join(args: argparse.Namespace) -> None:
    document = _blueprint_document(args)
    line_id = _select_blueprint_line(args, document)
    plan = build_join_plan(document, target=args.path, line_id=line_id)
    preflight_join_plan(plan)
    print("\n加入计划（尚未修改任何文件）：")
    for item in render_join_plan(plan):
        print("  - " + item)
    if args.dry_run:
        print("DRY RUN: 不会创建目录、clone 仓库、创建 worktree 或登记全局入口")
        return
    if not args.yes:
        if not sys.stdin.isatty():
            raise DyroError("join 会创建工作区和 Git worktree；请先使用 --dry-run，再加 --yes 执行")
        if not _ask_yes_no("应用此加入计划", default=False):
            print("已取消；没有修改任何文件。")
            return
    config = apply_join_plan(plan)
    if not args.no_register:
        record = add_workspace(
            config.root,
            name=config.name,
            make_default=args.make_default,
        )
        print(f"已登记全局入口：{record.name}")
    print("\n工作区已就绪")
    print(f"位置：{config.root}")
    print(f"开发线：{plan.line.id}")
    clean_scopes: dict[str, set[str]] = {
        repository_id: set() for repository_id in config.repositories
    }
    selected_scopes = {"anchor", f"line:{plan.line.id}"}
    for scope, repository_id, _branch, _head, _upstream, dirty in status_rows(config):
        if scope in selected_scopes and dirty == 0:
            clean_scopes[repository_id].add(scope)
    clean_count = sum(
        scopes == selected_scopes for scopes in clean_scopes.values()
    )
    print(f"仓库：{clean_count}/{len(config.repositories)} clean")
    print("下一步：dyro")


def cmd_status(args: argparse.Namespace) -> None:
    if args.all:
        print_all_status()
        return
    print_status(_config(args))


def cmd_agent_list(args: argparse.Namespace) -> None:
    config = _config(args)
    for adapter_id, adapter in sorted(config.adapters.items()):
        print(f"{adapter_id:16} launch={shlex.join(adapter.launch)}")


def cmd_agent_add(args: argparse.Namespace) -> None:
    config = _config(args)
    if args.preset:
        adapter = preset_adapter(args.id, args.preset)
    else:
        try:
            command = shlex.split(args.command)
        except ValueError as exc:
            raise DyroError(f"Agent command 解析失败：{exc}") from exc
        adapter = command_adapter(args.id, command)
    append_adapter(config, adapter, dry_run=args.dry_run)
    print(f"{'DRY RUN: 将添加' if args.dry_run else '已添加'} Agent adapter：{adapter.id}")


def cmd_agent_test(args: argparse.Namespace) -> None:
    checks = test_adapter(_config(args), args.id)
    failures = []
    for mode, available, executable in checks:
        print(f"{'PASS' if available else 'FAIL'} {args.id}.{mode}: {executable}")
        if not available:
            failures.append(mode)
    if failures:
        raise DyroError(f"Agent adapter 不可用：{args.id}（{', '.join(failures)}）")


def cmd_agent_discover(args: argparse.Namespace) -> None:
    print_agent_discovery(_config(args))


def cmd_tool_list(args: argparse.Namespace) -> None:
    resolved = resolve_home_config(
        root=getattr(args, "root", None),
        workspace=getattr(args, "workspace_alias", None),
        dry_run=True,
    )
    if resolved is None:
        raise DyroError("还没有可用工作区；先运行 dyro setup 或 dyro join")
    config, record = resolved
    preferences = load_tool_preferences()
    tools = sort_home_tools(
        home_tools(config, workspace=config.root),
        last_tool=record.last_agent if record else "",
        recommended_tool=config.recommended_tool,
        preferences=preferences,
    )
    labels = {
        ToolState.READY: "可使用",
        ToolState.NEEDS_SETUP: "待初始化",
        ToolState.INSTALLABLE: "可引导安装",
        ToolState.UNAVAILABLE: "不可用",
    }
    print(f"{'ID':20} {'状态':12} {'类型':10} 名称")
    for tool in tools:
        markers: list[str] = []
        if record and tool.id == record.last_agent:
            markers.append("上次使用")
        if tool.id == config.recommended_tool:
            markers.append("项目推荐")
        if tool.id == preferences.default_tool:
            markers.append("个人默认")
        suffix = f" [{' / '.join(markers)}]" if markers else ""
        print(
            f"{tool.id:20} {labels[tool.state]:12} {tool.kind:10} "
            f"{tool.label}{suffix}"
        )


def cmd_tool_install(args: argparse.Namespace) -> None:
    if (
        not args.yes
        and not args.dry_run
        and not (sys.stdin.isatty() and sys.stdout.isatty())
    ):
        raise DyroError(
            "非交互环境不会安装工具；请在终端中运行，或审阅计划后显式添加 --yes"
        )
    install_tool(args.id, yes=args.yes, dry_run=args.dry_run)


def cmd_tool_default(args: argparse.Namespace) -> None:
    tool_id = "" if args.clear else (args.id or "")
    if not tool_id and not args.clear:
        raise DyroError("请提供工具 ID，或使用 --clear 清除个人默认")
    if args.dry_run:
        print(
            "DRY RUN: 将清除个人默认工具"
            if not tool_id
            else f"DRY RUN: 将个人默认工具设为 {tool_id}"
        )
        return
    set_default_tool(tool_id)
    print("已清除个人默认工具" if not tool_id else f"个人默认工具：{tool_id}")


def cmd_tool_pin(args: argparse.Namespace) -> None:
    tool_ids = () if args.clear else tuple(args.ids)
    if not tool_ids and not args.clear:
        raise DyroError("请提供至少一个工具 ID，或使用 --clear 清除置顶顺序")
    if args.dry_run:
        print(
            "DRY RUN: 将清除工具置顶顺序"
            if not tool_ids
            else "DRY RUN: 将工具置顶顺序设为 " + ", ".join(tool_ids)
        )
        return
    set_pinned_tools(tool_ids)
    print(
        "已清除工具置顶顺序"
        if not tool_ids
        else "工具置顶顺序：" + ", ".join(tool_ids)
    )


def _print_update_result(result) -> None:
    if result.error:
        raise DyroError(result.error)
    if result.kind == UpdateKind.NONE:
        print(f"Dyro {result.current_version} 已是最新稳定版本。")
        return
    print(
        f"发现 Dyro {result.latest_version}（当前 {result.current_version}，"
        f"{result.kind.value} 更新）"
    )


def cmd_update_check(args: argparse.Namespace) -> None:
    result = _explicit_update_check()
    _print_update_result(result)
    if result.kind != UpdateKind.NONE:
        print("运行 dyro update now 可确认并完成更新。")


def cmd_update_now(args: argparse.Namespace) -> None:
    result = _explicit_update_check(persist=not args.dry_run)
    _print_update_result(result)
    if result.kind == UpdateKind.NONE:
        return
    if (
        not args.yes
        and not args.dry_run
        and not (sys.stdin.isatty() and sys.stdout.isatty())
    ):
        raise DyroError(
            "非交互环境不会更新 Dyro；请在终端中运行，或审阅计划后显式添加 --yes"
        )
    perform_update(
        result.latest_version,
        yes=args.yes,
        dry_run=args.dry_run,
    )


def _explicit_update_check(*, persist: bool = True):
    return check_for_update(
        __version__,
        force=True,
        persist=persist,
        fetch=lambda current: fetch_latest_version(
            current, timeout=EXPLICIT_CHECK_TIMEOUT
        ),
    )


def cmd_update_auto(args: argparse.Namespace) -> None:
    if args.mode == "status":
        state = load_update_state()
        print("补丁版本自动更新：" + ("已开启" if state.auto_patch else "已关闭"))
        return
    enabled = args.mode == "on"
    if args.dry_run:
        print("DRY RUN: 将" + ("开启" if enabled else "关闭") + "补丁版本自动更新")
        return
    set_auto_patch(enabled)
    print("补丁版本自动更新：" + ("已开启" if enabled else "已关闭"))
    if enabled:
        print("仅同一主版本、次版本内的补丁更新会自动安装。")


def cmd_update_enabled(args: argparse.Namespace) -> None:
    enabled = args.update_command == "enable"
    if args.dry_run:
        print("DRY RUN: 将" + ("开启" if enabled else "关闭") + "每日更新检测")
        return
    state = set_update_enabled(enabled)
    print("每日更新检测：" + ("已开启" if enabled else "已关闭"))
    if not state.check_enabled:
        print("补丁版本自动更新也已关闭。")


def cmd_config_get(args: argparse.Namespace) -> None:
    value = config_value(_config(args), args.key)
    print(json.dumps(value, ensure_ascii=False))


def cmd_config_set(args: argparse.Namespace) -> None:
    config = _config(args)
    value = set_config_value(config, args.key, args.value, dry_run=args.dry_run)
    if not args.dry_run:
        load(config.root)
    print(f"{'DRY RUN: 将设置' if args.dry_run else '已设置'} {args.key} = {json.dumps(value, ensure_ascii=False)}")


def cmd_open(args: argparse.Namespace) -> None:
    open_line(
        _config(args),
        args.line,
        kind=args.kind,
        agent=args.agent,
        prompt=args.prompt or "",
        dry_run=args.dry_run,
    )


def _choose(label: str, values: list[str]) -> str:
    if not values:
        raise DyroError(f"没有可选的 {label}")
    if len(values) == 1:
        return values[0]
    print(f"请选择{label}：")
    for index, value in enumerate(values, start=1):
        print(f"  {index}) {value}")
    raw = input("编号：").strip()
    if not raw.isdigit() or not (1 <= int(raw) <= len(values)):
        raise DyroError(f"无效的{label}选择")
    return values[int(raw) - 1]


def cmd_start(args: argparse.Namespace) -> None:
    """Newcomer-friendly path: validate, select a line, then open an Agent."""
    config = _config(args)
    findings = doctor(config)
    failures = [finding for finding in findings if finding.startswith("FAIL")]
    if failures:
        print("\n".join(failures))
        raise DyroError("工作区尚未就绪；先修复 doctor 失败项，或运行 dyro bootstrap --yes")
    if not config.adapters:
        raise DyroError("尚未配置可启动的 Agent；先运行 dyro next 查看安全的下一步")
    line_id = args.line or _choose("开发线", [line.id for line in list_lines(config)])
    line = get_line(config, line_id, args.kind)
    agent = args.agent or _choose("Agent", sorted(config.adapters))
    open_args = argparse.Namespace(root=str(config.root), line=line.id, kind=line.kind, agent=agent, prompt=args.prompt or "", dry_run=args.dry_run)
    cmd_open(open_args)


def cmd_next(args: argparse.Namespace) -> None:
    """Give newcomers one safe, concrete next step without making changes."""

    try:
        config = _config(args)
    except ValidationError:
        print("尚未发现 Dyro 工作区。")
        print("加入团队项目：dyro join <蓝图地址>")
        print("设置一个新项目：dyro setup")
        return
    findings = doctor(config)
    failures = [finding for finding in findings if finding.startswith("FAIL")]
    if failures:
        print("工作区还不能开始任务：")
        for finding in failures:
            print("  " + finding)
        print("下一步：dyro doctor；若仓库缺失且已配置 remote，则运行 dyro bootstrap --yes")
        return
    lines = list_lines(config)
    if not lines:
        print("Profile 已就绪，但还没有开发线。下一步：dyro line create dev --yes")
        return
    if not config.adapters:
        if shutil.which("codex"):
            print("工作区已就绪，检测到 Codex 尚未加入 Profile。下一步：dyro agent add codex --preset codex")
        else:
            print("工作区已就绪，但尚未配置可启动的 Agent。下一步：dyro agent add <id> --command '…'")
        return
    if len(lines) == 1 and len(config.adapters) == 1:
        print(f"工作区已就绪。下一步：dyro start --line {lines[0].id} --agent {next(iter(config.adapters))}")
        return
    print("工作区已就绪。下一步：dyro start")


def cmd_line_list(args: argparse.Namespace) -> None:
    config = _config(args)
    lines = list_lines(config, args.kind)
    if not lines:
        print("暂无已登记开发线")
        return
    print(f"{'KIND':8} {'ID':28} {'BRANCH':30} {'BASE':24} REPOSITORIES")
    for line in lines:
        repositories = ", ".join(
            f"{repo_id}@{line.base_for(repo_id)}[{line.storage_for(repo_id)}]" for repo_id in line.repositories
        )
        print(f"{line.kind:8} {line.id:28} {line.branch:30} {line.base:24} {repositories}")


def _create_line(args: argparse.Namespace, kind: str) -> None:
    config = _config(args)
    _require_yes(args, "创建开发线")
    branch = args.branch or (f"hotfix/{args.id}" if kind == "hotfix" else f"feat/{args.id}")
    if kind == "hotfix" and not args.base:
        raise DyroError("Hotfix 必须显式提供 --base（已核实的 release/tag/deployed SHA）")
    base = args.base or config.policy.default_base
    repository_bases = _repository_assignments(args.repo_base, "--repo-base")
    storage_modes = _repository_assignments(args.storage, "--storage")
    line = create_line(
        config,
        line_id=args.id,
        branch=branch,
        base=base,
        repositories=_repositories(args.repos),
        repository_bases=repository_bases,
        storage_modes=storage_modes,
        kind=kind,
        dry_run=args.dry_run,
    )
    bases = ", ".join(f"{repo_id}={line.base_for(repo_id)}" for repo_id in line.repositories)
    print(f"{'DRY RUN: ' if args.dry_run else ''}已创建 {line.kind} {line.id}，分支 {line.branch}，仓库基线：{bases}")


def cmd_line_create(args: argparse.Namespace) -> None:
    _create_line(args, "line")


def cmd_hotfix_create(args: argparse.Namespace) -> None:
    _create_line(args, "hotfix")


def cmd_changeset_create(args: argparse.Namespace) -> None:
    config = _config(args)
    changeset = create_changeset(
        config,
        changeset_id=args.id,
        line_id=args.line,
        repositories=_repositories(args.repos),
        dry_run=args.dry_run,
    )
    heads = ", ".join(f"{repository}={changeset.heads[repository][:12]}" for repository in changeset.repositories)
    print(f"{'DRY RUN: ' if args.dry_run else ''}已创建 Change Set {changeset.id}：{heads}")


def cmd_changeset_list(args: argparse.Namespace) -> None:
    changesets = list_changesets(_config(args))
    if not changesets:
        print("暂无 Change Set")
        return
    print(f"{'ID':28} {'LINE':24} {'BRANCH':28} REPOSITORIES")
    for changeset in changesets:
        print(f"{changeset.id:28} {changeset.line:24} {changeset.branch:28} {', '.join(changeset.repositories)}")


def cmd_changeset_verify(args: argparse.Namespace) -> None:
    config = _config(args)
    findings = verify_changeset(config, get_changeset(config, args.id))
    for finding in findings:
        print(finding)
    if any(finding.startswith("FAIL") for finding in findings):
        raise DyroError(f"Change Set {args.id} 未通过核验")


def cmd_task_create(args: argparse.Namespace) -> None:
    config = _config(args)
    validate_id(args.id, "任务 ID")
    get_line(config, args.line)
    if args.repository not in config.repositories:
        raise DyroError(f"未配置仓库：{args.repository}")
    path = config.task_specs_dir / args.id
    if args.dry_run:
        print(f"DRY RUN: 将创建 {path}")
        return
    with exclusive_lock(config.task_specs_dir / ".tasks.lock"):
        if path.exists():
            raise DyroError(f"任务目录已存在：{path}")
        path.mkdir(parents=True)
        mount = config.repositories[args.repository].mount
        atomic_write_text(path / "task.toml", task_template(args.id, args.title, args.line, args.repository, mount))
        atomic_write_text(path / "handoff.md", f"# {args.title}\n\n- 目标：\n- 范围：\n- 验收：\n")
    print(f"已创建任务：{path}")


def cmd_task_graph(args: argparse.Namespace) -> None:
    config = _config(args)
    if args.line:
        get_line(config, args.line)
    graph = build_task_graph(config, line=args.line)
    if args.action == "check":
        issues = validate_task_graph(graph)
        if issues:
            for issue in issues:
                print(f"FAIL [{issue.code}] {issue.message}")
            raise DyroError(f"任务图存在 {len(issues)} 个结构问题")
        print(f"PASS: 任务图结构有效，共 {len(graph.tasks)} 个任务")
        return
    print(render_task_graph(config, graph, output_format=args.format), end="")


def cmd_task_explain(args: argparse.Namespace) -> None:
    print(render_task_explanation(explain_task(_config(args), args.id)), end="")


def cmd_task_attempts(args: argparse.Namespace) -> None:
    config = _config(args)
    task = load_task(config, args.id)
    attempts = list_execution_attempts(task.directory)
    print(render_execution_attempts(task.id, attempts), end="")


def cmd_task_binding(args: argparse.Namespace) -> None:
    config = _config(args)
    task = load_task(config, args.id)
    print(render_review_binding(task.id, review_binding(task.directory)), end="")


def cmd_task_list(args: argparse.Namespace) -> None:
    config = _config(args)
    for task in list_tasks(config):
        print(f"{task.id:30} {task_status(config, task):16} {task.line:20} {task.title}")


def cmd_task_board(args: argparse.Namespace) -> None:
    print(board(_config(args)), end="")


def cmd_task_status(args: argparse.Namespace) -> None:
    config = _config(args)
    task = load_task(config, args.id)
    if args.value is None:
        print(task_status(config, task))
        return
    set_status(config, task, args.value, force=args.force, dry_run=args.dry_run)
    print(f"{'DRY RUN: ' if args.dry_run else ''}{task.id} -> {args.value}")


def cmd_task_run(args: argparse.Namespace) -> None:
    config = _config(args)
    task = load_task(config, args.id)
    result = run_task(config, task, dry_run=args.dry_run)
    print(f"{task.id} -> {result}")


def cmd_task_open(args: argparse.Namespace) -> None:
    open_task(
        _config(args),
        args.id,
        agent=args.agent,
        prompt=args.prompt or "",
        dry_run=args.dry_run,
    )


def cmd_task_claim(args: argparse.Namespace) -> None:
    config = _config(args)
    task = load_task(config, args.id)
    requested_output = (
        Path(args.output).expanduser() if args.output else None
    )
    if requested_output is not None and (
        requested_output.exists() or requested_output.is_symlink()
    ):
        raise DyroError(
            f"拒绝覆盖已有 claim 导出文件：{requested_output}"
        )
    output = (
        requested_output.parent.resolve() / requested_output.name
        if requested_output is not None
        else None
    )
    descriptor: int | None = None
    if output is not None and not args.dry_run:
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise DyroError(
                f"无法创建 claim 导出目录：{output.parent}"
            ) from exc
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        initial_mode = 0o600 if os.name == "nt" else 0o000
        try:
            descriptor = os.open(output, flags, initial_mode)
        except FileExistsError as exc:
            raise DyroError(
                f"拒绝覆盖已有 claim 导出文件：{output}"
            ) from exc
        except OSError as exc:
            raise DyroError(f"无法创建 claim 导出文件：{output}") from exc
    try:
        result = claim_task(
            config,
            task,
            runner=args.by,
            key_id=args.key_id,
            lease_seconds=args.lease_seconds,
            dry_run=args.dry_run,
        )
        if descriptor is not None:
            source = task.directory / "claim.json"
            content = source.read_bytes()
            if len(content) > 64 * 1024:
                raise DyroError("claim 文件超过安全导出大小限制")
            view = memoryview(content)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("claim 导出未取得进展")
                view = view[written:]
            os.fsync(descriptor)
            if os.name != "nt":
                # Keep the reserved final path unreadable until the complete
                # claim has reached stable storage.
                os.fchmod(descriptor, 0o600)
                os.fsync(descriptor)
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
            descriptor = None
            assert output is not None
            output.unlink(missing_ok=True)
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
    print(f"{task.id} -> {result}")
    if output is not None:
        if args.dry_run:
            print(f"{task.id} claim -> dry-run: {output}")
            return
        print(f"{task.id} claim -> exported: {output}")


def cmd_task_claim_renew(args: argparse.Namespace) -> None:
    from .tasks import renew_task_claim

    config = _config(args)
    task = load_task(config, args.id)
    print(
        f"{task.id} -> "
        f"{renew_task_claim(config, task, runner=args.by, lease_seconds=args.lease_seconds, dry_run=args.dry_run)}"
    )


def cmd_task_claim_release(args: argparse.Namespace) -> None:
    from .tasks import release_task_claim

    config = _config(args)
    task = load_task(config, args.id)
    print(f"{task.id} -> {release_task_claim(config, task, runner=args.by, dry_run=args.dry_run)}")


def cmd_task_next(args: argparse.Namespace) -> None:
    config = _config(args)
    candidates = list(plan_tasks(config).ready)
    if args.id:
        candidates = [task for task in candidates if task.id == args.id]
    if not candidates:
        raise DyroError("没有可执行任务；可检查 task board 与 decisions")
    if not args.run:
        for task in candidates:
            print(f"{task.id:30} {task.line:20} {task.title}")
        print("运行：dyro task next --run --yes" + (" --id <任务ID>" if len(candidates) > 1 else ""))
        return
    _require_yes(args, "启动下一个任务")
    if len(candidates) > 1:
        if not args.id:
            selected_id = _choose("任务", [task.id for task in candidates])
            candidates = [task for task in candidates if task.id == selected_id]
    task = candidates[0]
    print(f"{task.id} -> {run_task(config, task, dry_run=args.dry_run)}")


def cmd_task_answer(args: argparse.Namespace) -> None:
    config = _config(args)
    task = load_task(config, args.id)
    if args.file:
        answer = Path(args.file).read_text(encoding="utf-8")
    else:
        answer = args.text
    if not answer.strip():
        raise DyroError("回答不能为空")
    print(f"{task.id} -> {answer_task(config, task, answer, dry_run=args.dry_run)}")


def cmd_task_gates(args: argparse.Namespace) -> None:
    config = _config(args)
    task = load_task(config, args.id)
    passed = run_gates(config, task, dry_run=args.dry_run)
    print("PASS" if passed else "FAIL")
    if not passed:
        raise DyroError(f"任务 {task.id} 门禁未通过")


def cmd_task_review(args: argparse.Namespace) -> None:
    config = _config(args)
    task = load_task(config, args.id)
    print(f"{task.id} -> {review_task(config, task, dry_run=args.dry_run)}")


def cmd_task_signoff(args: argparse.Namespace) -> None:
    config = _config(args)
    task = load_task(config, args.id)
    result = signoff_task(
        config,
        task,
        approver=args.by,
        signing_key=Path(args.signing_key) if args.signing_key else None,
        key_id=args.key_id,
        dry_run=args.dry_run,
    )
    print(f"{task.id} -> {result}")


def cmd_task_evidence_execution(args: argparse.Namespace) -> None:
    config = _config(args)
    task = load_task(config, args.id)
    if args.bundle:
        if args.gates or args.heads:
            raise DyroError("--bundle 不能与 --gates 或 --heads 同时使用")
        with unpack_execution_bundle(Path(args.bundle)) as evidence:
            result = import_execution_evidence(
                config,
                task,
                receipt=evidence["receipt"],
                gates=evidence["gates"] if evidence["gates"].is_file() else None,
                heads=evidence["heads"] if evidence["heads"].is_file() else None,
                provenance=evidence["provenance"] if evidence["provenance"].is_file() else None,
                allow_legacy_provenance=args.allow_legacy,
                dry_run=args.dry_run,
            )
    else:
        result = import_execution_evidence(
            config,
            task,
            receipt=Path(args.receipt),
            gates=Path(args.gates) if args.gates else None,
            heads=Path(args.heads) if args.heads else None,
            provenance=Path(args.provenance) if args.provenance else None,
            allow_legacy_provenance=args.allow_legacy,
            dry_run=args.dry_run,
        )
    print(f"{task.id} -> {result}")


def cmd_task_evidence_build(args: argparse.Namespace) -> None:
    config = _config(args)
    task = load_task(config, args.id)
    bundle = build_execution_bundle(
        config,
        task,
        workspace=Path(args.workspace),
        receipt=Path(args.receipt),
        output=Path(args.output),
        signing_key=Path(args.signing_key) if args.signing_key else None,
        key_id=args.key_id,
        claim=Path(args.claim) if args.claim else None,
        dry_run=args.dry_run,
    )
    print(f"{task.id} -> {bundle.result}: {bundle.output}")
    if not bundle.gates_passed:
        raise DyroError("外部门禁失败；已输出证据包供排查，不能导入为 DONE")


def cmd_key_generate(args: argparse.Namespace) -> None:
    from .signing import generate_keypair

    if args.dry_run:
        print(f"{args.id} -> dry-run")
        return
    generate_keypair(
        args.id,
        private_key=Path(args.private_key),
        public_key=Path(args.public_key),
    )
    print(f"{args.id} -> generated")


def cmd_key_trust(args: argparse.Namespace) -> None:
    from .signing import trust_public_key

    config = _config(args)
    if args.dry_run:
        print(f"{args.id} -> dry-run")
        return
    target = trust_public_key(
        config.root,
        args.id,
        purpose=args.purpose,
        source=Path(args.public_key),
        principal_id=args.principal,
        not_before=args.not_before,
        not_after=args.not_after,
    )
    print(f"{args.id} -> trusted: {target}")


def cmd_key_revoke(args: argparse.Namespace) -> None:
    from .signing import revoke_public_key

    config = _config(args)
    if args.dry_run:
        print(f"{args.id} -> dry-run")
        return
    target = revoke_public_key(
        config.root,
        args.id,
        purpose=args.purpose,
        reason=args.reason,
    )
    print(f"{args.id} -> revoked: {target}")


def cmd_key_list(args: argparse.Namespace) -> None:
    from .signing import trusted_key_ids, trusted_key_records

    config = _config(args)
    if args.show_status:
        for record in trusted_key_records(config.root, args.purpose):
            print(
                f"{record['key_id']}\t{record['principal_id'] or '-'}\t{record['status']}\t"
                f"{record['not_before'] or '-'}\t{record['not_after'] or '-'}"
            )
        return
    for key_id in trusted_key_ids(config.root, args.purpose):
        print(key_id)


def cmd_key_audit(args: argparse.Namespace) -> None:
    from .signing import read_trust_audit

    config = _config(args)
    for record in read_trust_audit(config.root):
        print(json.dumps(record, ensure_ascii=False, sort_keys=True))


def cmd_key_audit_sync(args: argparse.Namespace) -> None:
    from .audit_remote import default_audit_workspace_id, sync_trust_audit

    config = _config(args)
    token = os.environ.get(args.token_env) if args.token_env else None
    result = sync_trust_audit(
        config.root,
        workspace_id=args.workspace_id or default_audit_workspace_id(config.name),
        witness=args.witness,
        endpoint=args.endpoint,
        signing_key=Path(args.signing_key),
        key_id=args.key_id,
        witness_key_id=args.witness_key_id,
        recovery_key_id=args.witness_recovery_key_id,
        token=token,
        allow_insecure_http=args.allow_insecure_http,
        timeout_seconds=args.timeout_seconds,
        dry_run=args.dry_run,
    )
    if result.synced and result.batch and result.batch.get("events"):
        state = "synced"
    elif result.synced:
        state = "verified"
    else:
        state = "dry-run"
    print(
        f"{args.witness} -> {state}: "
        f"sequence={result.sequence} head={result.head_sha256}"
    )


def cmd_witness_serve(args: argparse.Namespace) -> None:
    from .witness import WitnessConfig, serve_witness

    if args.dry_run:
        raise ValidationError("dry-run 不支持启动常驻 Witness 服务")
    token = os.environ.get(args.auth_token_env)
    if not token and not args.allow_unauthenticated:
        raise ValidationError(
            f"环境变量 {args.auth_token_env} 未设置；本地测试才可使用 --allow-unauthenticated"
        )
    if args.allow_unauthenticated and args.host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValidationError("--allow-unauthenticated 只能绑定 loopback host")
    if (args.tls_cert is None) != (args.tls_key is None):
        raise ValidationError("Witness TLS cert 与 key 必须同时设置")
    if args.tls_cert is None:
        if not args.allow_http:
            raise ValidationError("Witness 必须设置 TLS，或显式使用仅本地的 --allow-http")
        if args.host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValidationError("--allow-http 只能绑定 loopback host")
    bindings: dict[str, str] = {}
    for value in args.client_workspace_binding:
        key_id, separator, workspace_id = value.partition("=")
        if not separator or not key_id or not workspace_id:
            raise ValidationError("--client-workspace-binding 必须为 KEY_ID=WORKSPACE_ID")
        if key_id in bindings:
            raise ValidationError(f"--client-workspace-binding 重复：{key_id}")
        bindings[key_id] = workspace_id
    config = WitnessConfig(
        storage_root=Path(args.storage_root),
        client_trust_root=Path(args.client_trust_root),
        witness_id=args.witness_id,
        receipt_key_id=args.receipt_key_id,
        receipt_signing_key=Path(args.receipt_signing_key),
        record_archive_root=(
            Path(args.record_archive_root)
            if args.record_archive_root is not None
            else None
        ),
        auth_token=token,
        workspace_id=args.workspace_id,
        client_workspace_bindings=bindings or None,
        expected_endpoint=args.expected_endpoint,
        transition_key_id=args.transition_key_id,
        transition_signing_key=(
            Path(args.transition_signing_key)
            if args.transition_signing_key is not None
            else None
        ),
        transition_purpose=args.transition_purpose,
    )
    serve_witness(
        config,
        host=args.host,
        port=args.port,
        tls_cert=Path(args.tls_cert) if args.tls_cert is not None else None,
        tls_key=Path(args.tls_key) if args.tls_key is not None else None,
        max_concurrent_requests=args.max_concurrent_requests,
        read_timeout_seconds=args.read_timeout_seconds,
        on_listening=lambda: print(
            f"Witness {args.witness_id} listening on {args.host}:{args.port}"
        ),
    )


def cmd_task_evidence_review(args: argparse.Namespace) -> None:
    config = _config(args)
    task = load_task(config, args.id)
    print(f"{task.id} -> {import_review_evidence(config, task, review=Path(args.file), dry_run=args.dry_run)}")


def cmd_task_evidence_review_build(args: argparse.Namespace) -> None:
    from .reviews import build_signed_review_record
    from .state import atomic_write_bytes

    config = _config(args)
    task = load_task(config, args.id)
    output = Path(args.output).expanduser().resolve()
    if output.exists():
        raise ValidationError(f"拒绝覆盖已有 signed review：{output}")
    review_content = Path(args.file).expanduser().resolve().read_bytes()
    record = build_signed_review_record(
        task.id,
        reviewer=args.reviewer,
        review_content=review_content,
        signing_key=Path(args.signing_key).expanduser().resolve(),
        key_id=args.key_id,
    )
    if args.dry_run:
        print(f"{task.id} -> dry-run")
        return
    atomic_write_bytes(
        output,
        json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n",
    )
    print(f"{task.id} -> signed review: {output}")


def cmd_task_evidence_generations(args: argparse.Namespace) -> None:
    from .tasks import maintain_evidence_generations

    config = _config(args)
    task = load_task(config, args.id)
    if args.prune and not args.dry_run:
        _require_yes(args, f"清理任务 {task.id} 的历史证据 generation")
    records, targets = maintain_evidence_generations(
        config,
        task,
        prune=args.prune,
        older_than_days=args.older_than_days,
        keep=args.keep,
        dry_run=args.dry_run,
    )
    target_ids = {record.generation_id for record in targets}
    for record in records:
        state = "current" if record.current else "temporary" if record.temporary else "history"
        action = "prune" if record.generation_id in target_ids else "keep"
        print(
            f"{record.generation_id}\t{state}\t{record.modified_at.isoformat()}\t"
            f"{record.size_bytes}\t{action}"
        )
    if args.prune:
        outcome = "would remove" if args.dry_run else "removed"
        print(f"{task.id} -> {outcome} {len(targets)} generation(s)")


def cmd_task_merge(args: argparse.Namespace) -> None:
    _require_yes(args, "合并任务")
    config = _config(args)
    task = load_task(config, args.id)
    merge_task(config, task, push=args.push, dry_run=args.dry_run)
    print(f"{'DRY RUN: ' if args.dry_run else ''}已合并 {task.id}" + (" 并推送" if args.push else ""))


def cmd_task_decisions(args: argparse.Namespace) -> None:
    items = decisions(_config(args))
    if not items:
        print("暂无决策点")
        return
    for key, value in sorted(items.items()):
        print(f"{key:32} {value}")


def cmd_task_stats(args: argparse.Namespace) -> None:
    report = stats(_config(args))
    if not report:
        print("台账为空")
        return
    print(f"{'AGENT':18} {'EXEC':>5} {'EXEC OK':>8} {'REVIEW':>7} {'REVIEW OK':>10}")
    for agent, counters in sorted(report.items()):
        print(f"{agent:18} {counters['executor']:>5} {counters['executor_ok']:>8} {counters['review']:>7} {counters['review_ok']:>10}")


def cmd_task_loop(args: argparse.Namespace) -> None:
    for task_id, result in loop_tasks(_config(args), dry_run=args.dry_run):
        print(f"{task_id} -> {result}")


def cmd_objective_start(args: argparse.Namespace) -> None:
    config = _config(args)
    content = _objective_contract_from_args(args, config)
    _require_objective_yes(args, "创建 Objective")
    record = create_objective(config, content, dry_run=args.dry_run)
    prefix = "DRY RUN: " if args.dry_run else ""
    print(
        f"{prefix}Objective {record.objective.id} r{record.revision}："
        f"line={record.objective.line} targets={', '.join(record.objective.targets)} "
        f"scope={', '.join(record.scope)}"
    )


def cmd_objective_list(args: argparse.Namespace) -> None:
    config = _config(args)
    records = list_objectives(config)
    if not records:
        print("暂无 Objective。下一步：dyro objective start --file <objective.toml> --yes")
        return
    print(f"{'OBJECTIVE':28} {'STATE':8} {'RESULT':16} {'REV':4} {'LINE':20} TARGETS")
    for record in records:
        _print_objective(config, record)


def cmd_objective_status(args: argparse.Namespace) -> None:
    config = _config(args)
    record = get_objective(config, args.id)
    print(f"Objective: {record.objective.id}")
    print(f"Operator state: {record.operator_state}")
    print(f"Derived result: {derive_objective_result(config, record)}")
    print(f"Revision: {record.revision}")
    print(f"Line: {record.objective.line}")
    print(f"Targets: {', '.join(record.objective.targets)}")
    print(f"Scope: {', '.join(record.scope)}")
    print(f"Contract SHA-256: {record.contract_sha256}")


def _read_objective_plan(config: Config, objective_id: str):
    """Build an Objective plan without recovery, mutation, dispatch, or agents."""
    record = get_objective(config, objective_id, recover=False)
    snapshot = build_scheduler_snapshot(config, objective=record)
    return snapshot, build_continuation_plan(snapshot)


def cmd_objective_plan(args: argparse.Namespace) -> None:
    _, plan = _read_objective_plan(_config(args), args.id)
    if args.format == "json":
        print(json.dumps(continuation_plan_payload(plan), ensure_ascii=False, sort_keys=True, indent=2))
        return
    print(render_plan_text(plan))


def cmd_objective_explain(args: argparse.Namespace) -> None:
    _, plan = _read_objective_plan(_config(args), args.id)
    if args.format == "json":
        print(json.dumps(continuation_plan_payload(plan), ensure_ascii=False, sort_keys=True, indent=2))
        return
    print(render_plan_text(plan))


def cmd_objective_graph(args: argparse.Namespace) -> None:
    snapshot, plan = _read_objective_plan(_config(args), args.id)
    projection = build_scheduler_projection(snapshot, plan)
    if args.format == "json":
        print(render_projection_json(projection))
        return
    print(render_projection_mermaid(projection))


def _cmd_objective_transition(args: argparse.Namespace, action: str) -> None:
    config = _config(args)
    _require_objective_yes(args, f"Objective {action}")
    handlers = {
        "pause": pause_objective,
        "resume": resume_objective,
        "stop": stop_objective,
        "reconcile": reconcile_objective,
    }
    record = handlers[action](config, args.id, dry_run=args.dry_run)
    print(f"{'DRY RUN: ' if args.dry_run else ''}{record.objective.id} -> {record.operator_state} r{record.revision}")


def cmd_objective_pause(args: argparse.Namespace) -> None:
    _cmd_objective_transition(args, "pause")


def cmd_objective_resume(args: argparse.Namespace) -> None:
    _cmd_objective_transition(args, "resume")


def cmd_objective_stop(args: argparse.Namespace) -> None:
    _cmd_objective_transition(args, "stop")


def cmd_objective_reconcile(args: argparse.Namespace) -> None:
    _cmd_objective_transition(args, "reconcile")


def cmd_objective_scope_add(args: argparse.Namespace) -> None:
    config = _config(args)
    _require_objective_yes(args, "扩展 Objective scope")
    record = add_objective_target(config, args.id, args.task, dry_run=args.dry_run)
    print(f"{'DRY RUN: ' if args.dry_run else ''}{record.objective.id} r{record.revision} targets={', '.join(record.objective.targets)}")


def cmd_objective_scope_remove(args: argparse.Namespace) -> None:
    config = _config(args)
    _require_objective_yes(args, "缩减 Objective scope")
    record = remove_objective_target(config, args.id, args.task, dry_run=args.dry_run)
    print(f"{'DRY RUN: ' if args.dry_run else ''}{record.objective.id} r{record.revision} targets={', '.join(record.objective.targets)}")


def _daemon_select_runnable(config: Config, tasks: list, *, limit: int) -> list:
    """Compatibility wrapper around the shared deterministic scheduler."""
    plan = plan_tasks(config, candidates=tasks)
    return list(select_task_wave(plan, limit=limit).tasks)


def cmd_task_daemon(args: argparse.Namespace) -> None:
    from .continuation.store import assert_legacy_scheduler_allowed

    config = _config(args)
    while True:
        tasks = list_tasks(config)
        assert_legacy_scheduler_allowed(config, (task.id for task in tasks))
        queued = _daemon_select_runnable(config, tasks, limit=max(1, args.parallel))
        if queued:
            with ThreadPoolExecutor(max_workers=max(1, args.parallel), thread_name_prefix="dyro-dispatch") as pool:
                futures = {
                    pool.submit(run_task, config, task, dry_run=args.dry_run, legacy_scheduler=True): task
                    for task in queued
                }
                for future in as_completed(futures):
                    task = futures[future]
                    try:
                        print(f"dispatch {task.id} -> {future.result()}")
                    except DyroError as exc:
                        print(f"skip {task.id}: {exc}")
        review_queue = list(plan_tasks(config).review)
        if review_queue:
            with ThreadPoolExecutor(max_workers=max(1, args.parallel), thread_name_prefix="dyro-review") as pool:
                futures = {pool.submit(review_task, config, task, dry_run=args.dry_run): task for task in review_queue}
                for future in as_completed(futures):
                    task = futures[future]
                    try:
                        print(f"review {task.id} -> {future.result()}")
                    except DyroError as exc:
                        print(f"keep review {task.id}: {exc}")
        if args.once or args.dry_run:
            return
        time.sleep(max(10, args.interval))


def _add_common(parser: argparse.ArgumentParser) -> None:
    location = parser.add_mutually_exclusive_group()
    location.add_argument("--root", help="工作区根目录；默认从当前目录向上查找 dyro.toml")
    location.add_argument(
        "--workspace",
        dest="workspace_alias",
        help="全局登记的工作区别名；可从任意目录使用",
    )
    parser.add_argument("--dry-run", action="store_true", help="仅输出计划，不写文件、不调用 Agent 或 Git 写操作")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dyro", description="DyroEngineeringFlow：本地优先的多仓工程自动化与交付控制平台")
    parser.add_argument("--version", action="version", version=f"dyro {__version__}")
    _add_common(parser)
    sub = parser.add_subparsers(dest="command")

    # Real handling is early-exit in main(); these document commands in --help.
    sub.add_parser(
        "dispatch",
        help="可选本地多 Agent 派发（L0–L4；不替代 gates/merge）。用法：dyro dispatch <subcommand> …",
        add_help=False,
    )
    init = sub.add_parser("init", help="初始化工作区配置")
    init.add_argument("path", nargs="?", default=".")
    init.add_argument("--name", default="my-workspace")
    init.add_argument("--base", default="main", help="--discover 时写入的默认基线分支")
    init_mode = init.add_mutually_exclusive_group()
    init_mode.add_argument("--wizard", action="store_true", help="交互式登记真实仓库与可选 remote")
    init_mode.add_argument("--discover", action="store_true", help="自动发现当前目录下的 Git 仓库并登记 origin")
    init.set_defaults(func=cmd_init)

    setup = sub.add_parser("setup", help="首次引导：预览并安全创建 Profile、仓库与首条开发线")
    setup.add_argument("path", nargs="?", default=".")
    setup.add_argument("--name", help="新 Profile 的工作区名称；默认由目录名推断")
    setup.add_argument("--base", help="首条开发线与新 Profile 的默认基线；默认 main")
    setup.add_argument("--line", default="dev", help="首条功能开发线 ID；默认 dev")
    setup.add_argument("--branch", help="首条开发线分支；默认 feat/<line>")
    setup.add_argument("--no-line", action="store_true", help="仅建立 Profile，不创建 Git worktree 开发线")
    setup.add_argument("--yes", action="store_true", help="确认创建首条 Git worktree 开发线")
    setup_mode = setup.add_mutually_exclusive_group()
    setup_mode.add_argument("--interactive", action="store_true", help="强制运行交互式首次设置")
    setup_mode.add_argument("--non-interactive", action="store_true", help="禁用交互提示；适合脚本与 CI")
    setup.add_argument("--quick", action="store_true", help="只检查现有 Profile 并给出下一步")
    setup.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=argparse.SUPPRESS,
        help="仅预览设置计划；也兼容全局 --dry-run 放在命令前",
    )
    setup.set_defaults(func=cmd_setup)

    sub.add_parser("home", help="打开当前或默认项目首页").set_defaults(func=cmd_home)
    workspace = sub.add_parser("workspace", help="管理可从任意目录进入的全局工作区")
    workspace_sub = workspace.add_subparsers(dest="workspace_command", required=True)
    workspace_add = workspace_sub.add_parser("add", help="登记已有 Dyro 工作区")
    workspace_add.add_argument("path", nargs="?", default=".")
    workspace_add.add_argument("--name", help="便于记忆的工作区别名；默认读取 Profile 名称")
    workspace_add.add_argument("--default", action="store_true", help="设为裸 dyro 的默认项目")
    workspace_add.set_defaults(func=cmd_workspace_add)
    workspace_sub.add_parser("list", help="显示已登记工作区及可用状态").set_defaults(
        func=cmd_workspace_list
    )
    workspace_default = workspace_sub.add_parser("default", help="设置裸 dyro 的默认项目")
    workspace_default.add_argument("name")
    workspace_default.set_defaults(func=cmd_workspace_default)
    workspace_remove = workspace_sub.add_parser("remove", help="移除全局入口，不删除项目文件")
    workspace_remove.add_argument("name")
    workspace_remove.add_argument("--yes", action="store_true")
    workspace_remove.set_defaults(func=cmd_workspace_remove)

    blueprint = sub.add_parser("blueprint", help="验证可复用的团队工作区蓝图")
    blueprint_sub = blueprint.add_subparsers(dest="blueprint_command", required=True)
    blueprint_validate = blueprint_sub.add_parser("validate", help="只读验证蓝图结构与固定基线")
    blueprint_validate.add_argument("source", help="本地 TOML/目录、HTTPS 文件或 Git 仓库")
    blueprint_validate.add_argument("--ref", dest="blueprint_ref", help="Git 蓝图仓库的分支或 tag")
    blueprint_validate.add_argument(
        "--file",
        dest="blueprint_file",
        default=BLUEPRINT_FILENAME,
        help=f"Git 蓝图仓库内的文件；默认 {BLUEPRINT_FILENAME}",
    )
    blueprint_validate.set_defaults(func=cmd_blueprint_validate)

    join = sub.add_parser("join", help="从团队蓝图安全创建独立的多仓工作区")
    join.add_argument("source", help="本地 TOML/目录、HTTPS 文件或 Git 仓库")
    join.add_argument("--path", help="目标目录；默认 ~/DyroProjects/<suggested_directory>")
    join.add_argument("--line", help="要创建的开发线；默认交互选择或使用蓝图默认值")
    join.add_argument("--ref", dest="blueprint_ref", help="Git 蓝图仓库的分支或 tag")
    join.add_argument(
        "--file",
        dest="blueprint_file",
        default=BLUEPRINT_FILENAME,
        help=f"Git 蓝图仓库内的文件；默认 {BLUEPRINT_FILENAME}",
    )
    join.add_argument("--yes", action="store_true", help="确认执行已展示的加入计划")
    registration = join.add_mutually_exclusive_group()
    registration.add_argument("--no-register", action="store_true", help="不登记到裸 dyro 的全局首页")
    registration.add_argument(
        "--default",
        dest="make_default",
        action="store_true",
        help="将新工作区设为裸 dyro 的默认项目",
    )
    join.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=argparse.SUPPRESS,
        help="仅预览，不写文件或执行 Git 写操作",
    )
    join.set_defaults(func=cmd_join)

    sub.add_parser("doctor", help="验证动态工作区结构").set_defaults(func=cmd_doctor)
    terminology = sub.add_parser("terminology", help="使用仓库外策略扫描候选术语")
    terminology_sub = terminology.add_subparsers(dest="terminology_command", required=True)
    terminology_check = terminology_sub.add_parser(
        "check",
        help="扫描工作区、分支、diff 与提交候选；策略不写入仓库",
    )
    terminology_check.add_argument(
        "--policy-file",
        help="仓库外的 UTF-8 策略文件；也可使用外部环境输入",
    )
    terminology_check.add_argument(
        "--base-ref",
        default="origin/main",
        help="候选分支和提交的比较基线；默认 origin/main",
    )
    terminology_check.add_argument(
        "--message",
        action="append",
        default=[],
        help="额外扫描的提交候选说明；可重复指定",
    )
    terminology_check.set_defaults(func=cmd_terminology_check)
    status_parser = sub.add_parser("status", help="显示 anchors 与开发线 Git 状态")
    status_parser.add_argument("--all", action="store_true", help="汇总所有全局登记工作区")
    status_parser.set_defaults(func=cmd_status)
    bootstrap_parser = sub.add_parser("bootstrap", help="clone 配置了 remote 的缺失仓库 anchor")
    bootstrap_parser.add_argument("--yes", action="store_true")
    bootstrap_parser.set_defaults(func=cmd_bootstrap)
    repo = sub.add_parser("repo", help="免手改 TOML 的仓库配置管理")
    repo_sub = repo.add_subparsers(dest="repo_command", required=True)
    repo_sub.add_parser("list", help="显示已登记仓库").set_defaults(func=cmd_repo_list)
    repo_add = repo_sub.add_parser("add", help="登记一个本地 Git 仓库；自动读取 origin")
    repo_add.add_argument("path", help="工作区内的仓库路径")
    repo_add.add_argument("--id", help="仓库标识；默认使用目录名")
    repo_add.add_argument("--mount", help="开发线内挂载路径；默认智能推断")
    repo_add.add_argument("--remote", help="缺失路径的 clone remote，或覆盖自动发现的 origin")
    repo_add.set_defaults(func=cmd_repo_add)
    agent = sub.add_parser("agent", help="Agent adapters")
    agent_sub = agent.add_subparsers(dest="agent_command", required=True)
    agent_sub.add_parser("list", help="显示已登记的 Agent adapter").set_defaults(func=cmd_agent_list)
    agent_sub.add_parser("discover", help="检测本机 Agent，并区分已配置与尚未集成").set_defaults(
        func=cmd_agent_discover
    )
    agent_add = agent_sub.add_parser("add", help="通过预设或命令登记 Agent，无需编辑 TOML")
    agent_add.add_argument("id")
    agent_source = agent_add.add_mutually_exclusive_group(required=True)
    agent_source.add_argument("--preset", choices=("codex", "noop"))
    agent_source.add_argument("--command", help="作为 launch/read/write 的 argv 命令行；不会经 shell 执行")
    agent_add.set_defaults(func=cmd_agent_add)
    agent_test = agent_sub.add_parser("test", help="仅检查 adapter 可执行文件是否可用，不启动 Agent")
    agent_test.add_argument("id")
    agent_test.set_defaults(func=cmd_agent_test)
    tool = sub.add_parser("tool", help="发现、排序和安全安装本地编码工具")
    tool_sub = tool.add_subparsers(dest="tool_command", required=True)
    tool_sub.add_parser("list", help="按首页实际顺序显示工具状态").set_defaults(
        func=cmd_tool_list
    )
    tool_install = tool_sub.add_parser("install", help="显示并执行内置的官方安装方案")
    tool_install.add_argument("id")
    tool_install.add_argument("--yes", action="store_true", help="确认执行已展示的安装命令或打开官方页面")
    tool_install.set_defaults(func=cmd_tool_install)
    tool_default = tool_sub.add_parser("default", help="设置个人默认工具")
    tool_default.add_argument("id", nargs="?")
    tool_default.add_argument("--clear", action="store_true")
    tool_default.set_defaults(func=cmd_tool_default)
    tool_pin = tool_sub.add_parser("pin", help="设置个人工具置顶顺序")
    tool_pin.add_argument("ids", nargs="*")
    tool_pin.add_argument("--clear", action="store_true")
    tool_pin.set_defaults(func=cmd_tool_pin)
    update = sub.add_parser("update", help="检测并安全更新 Dyro")
    update_sub = update.add_subparsers(dest="update_command", required=True)
    update_sub.add_parser("check", help="立即检查官方 PyPI 的最新稳定版本").set_defaults(
        func=cmd_update_check
    )
    update_now = update_sub.add_parser("now", help="显示计划并更新到最新稳定版本")
    update_now.add_argument("--yes", action="store_true", help="确认执行已展示的更新命令")
    update_now.set_defaults(func=cmd_update_now)
    update_auto = update_sub.add_parser("auto", help="管理补丁版本自动更新")
    update_auto.add_argument("mode", choices=("on", "off", "status"), nargs="?", default="status")
    update_auto.set_defaults(func=cmd_update_auto)
    update_sub.add_parser("enable", help="开启每日首次交互运行更新检测").set_defaults(
        func=cmd_update_enabled
    )
    update_sub.add_parser("disable", help="关闭每日更新检测与自动更新").set_defaults(
        func=cmd_update_enabled
    )
    config_command = sub.add_parser("config", help="安全地读取或修改常用 Profile 策略")
    config_sub = config_command.add_subparsers(dest="config_command", required=True)
    config_get = config_sub.add_parser("get")
    config_get.add_argument("key")
    config_get.set_defaults(func=cmd_config_get)
    config_set = config_sub.add_parser("set")
    config_set.add_argument("key")
    config_set.add_argument("value")
    config_set.set_defaults(func=cmd_config_set)
    key = sub.add_parser("key", help="Ed25519 签名密钥与工作区信任根")
    key_sub = key.add_subparsers(dest="key_command", required=True)
    key_generate = key_sub.add_parser("generate", help="生成 runner 或 approver Ed25519 密钥对")
    key_generate.add_argument("id")
    key_generate.add_argument("--private-key", required=True)
    key_generate.add_argument("--public-key", required=True)
    key_generate.set_defaults(func=cmd_key_generate)
    key_trust = key_sub.add_parser("trust", help="将公钥安装到用途隔离的工作区 trust store")
    key_trust.add_argument("id")
    key_trust.add_argument(
        "--purpose",
        choices=TRUST_PURPOSES,
        required=True,
    )
    key_trust.add_argument("--public-key", required=True)
    key_trust.add_argument("--principal", help="不可变签名主体；省略时使用 key ID")
    key_trust.add_argument("--not-before", help="ISO-8601 生效时间；必须包含时区")
    key_trust.add_argument("--not-after", help="ISO-8601 失效时间；必须包含时区")
    key_trust.set_defaults(func=cmd_key_trust)
    key_revoke = key_sub.add_parser("revoke", help="撤销指定用途的 trusted key ID；保留公钥与审计记录")
    key_revoke.add_argument("id")
    key_revoke.add_argument(
        "--purpose",
        choices=TRUST_PURPOSES,
        required=True,
    )
    key_revoke.add_argument("--reason", required=True)
    key_revoke.set_defaults(func=cmd_key_revoke)
    key_list = key_sub.add_parser("list", help="列出指定用途的 trusted key IDs")
    key_list.add_argument(
        "--purpose",
        choices=TRUST_PURPOSES,
        required=True,
    )
    key_list.add_argument("--show-status", action="store_true", help="同时显示 pending、expired 与 revoked key")
    key_list.set_defaults(func=cmd_key_list)
    key_sub.add_parser("audit", help="输出 trust/revoke JSONL 审计记录").set_defaults(func=cmd_key_audit)
    key_audit_sync = key_sub.add_parser(
        "audit-sync",
        help="将本地 trust 审计链同步到远程 Witness",
    )
    key_audit_sync.add_argument("--witness", required=True)
    key_audit_sync.add_argument("--endpoint", required=True)
    key_audit_sync.add_argument("--signing-key", required=True)
    key_audit_sync.add_argument("--key-id", required=True)
    key_audit_sync.add_argument("--witness-key-id", required=True)
    key_audit_sync.add_argument("--workspace-id")
    key_audit_sync.add_argument("--token-env", default="DYRO_AUDIT_TOKEN")
    key_audit_sync.add_argument("--timeout-seconds", type=float, default=15.0)
    key_audit_sync.add_argument("--witness-recovery-key-id")
    key_audit_sync.add_argument(
        "--allow-insecure-http",
        action="store_true",
        help="仅为本地测试允许 HTTP",
    )
    key_audit_sync.set_defaults(func=cmd_key_audit_sync)
    witness = sub.add_parser("witness", help="运行独立的远程 audit Witness 服务")
    witness_sub = witness.add_subparsers(dest="witness_command", required=True)
    witness_serve = witness_sub.add_parser("serve", help="验证批次、签发回执并持久化 Witness ledger")
    witness_serve.add_argument("--storage-root", required=True)
    witness_serve.add_argument("--client-trust-root", required=True)
    witness_serve.add_argument("--witness-id", required=True)
    witness_serve.add_argument("--receipt-key-id", required=True)
    witness_serve.add_argument("--receipt-signing-key", required=True)
    witness_serve.add_argument("--record-archive-root")
    witness_serve.add_argument("--workspace-id")
    witness_serve.add_argument(
        "--client-workspace-binding",
        action="append",
        default=[],
        metavar="KEY_ID=WORKSPACE_ID",
        help="多工作区模式中将 client audit-export key 绑定到唯一 workspace，可重复指定",
    )
    witness_serve.add_argument("--expected-endpoint")
    witness_serve.add_argument("--auth-token-env", default="DYRO_WITNESS_TOKEN")
    witness_serve.add_argument("--allow-unauthenticated", action="store_true")
    witness_serve.add_argument("--host", default="127.0.0.1")
    witness_serve.add_argument("--port", type=int, default=8443)
    witness_serve.add_argument("--max-concurrent-requests", type=int, default=32)
    witness_serve.add_argument("--read-timeout-seconds", type=float, default=15.0)
    witness_serve.add_argument("--tls-cert")
    witness_serve.add_argument("--tls-key")
    witness_serve.add_argument("--allow-http", action="store_true")
    witness_serve.add_argument("--transition-key-id")
    witness_serve.add_argument("--transition-signing-key")
    witness_serve.add_argument(
        "--transition-purpose",
        choices=("audit-receipt", "audit-recovery"),
    )
    witness_serve.set_defaults(func=cmd_witness_serve)
    open_cmd = sub.add_parser("open", help="在指定开发线启动 Agent")
    open_cmd.add_argument("line")
    open_cmd.add_argument("--kind", choices=("line", "hotfix"))
    open_cmd.add_argument("--agent", default="codex")
    open_cmd.add_argument("--prompt", default="")
    open_cmd.set_defaults(func=cmd_open)
    start = sub.add_parser("start", help="新人入口：检查工作区、选择开发线和 Agent")
    start.add_argument("--line")
    start.add_argument("--kind", choices=("line", "hotfix"))
    start.add_argument("--agent")
    start.add_argument("--prompt", default="")
    start.set_defaults(func=cmd_start)
    sub.add_parser("next", help="根据当前状态给出新手的唯一安全下一步").set_defaults(func=cmd_next)

    line = sub.add_parser("line", help="功能开发线")
    line_sub = line.add_subparsers(dest="line_command", required=True)
    line_list = line_sub.add_parser("list")
    line_list.add_argument("--kind", choices=("line", "hotfix"))
    line_list.set_defaults(func=cmd_line_list)
    line_create = line_sub.add_parser("create")
    line_create.add_argument("id")
    line_create.add_argument("--branch")
    line_create.add_argument("--base")
    line_create.add_argument("--repos", help="逗号分隔；默认全部 configured repositories")
    line_create.add_argument("--repo-base", action="append", metavar="REPOSITORY=REF", help="为一个仓库覆盖默认基线；可重复")
    line_create.add_argument("--storage", action="append", metavar="REPOSITORY=MODE", help="仓库存储方式：linked-worktree 或 anchor-reference；可重复")
    line_create.add_argument("--yes", action="store_true")
    line_create.set_defaults(func=cmd_line_create)

    hotfix = sub.add_parser("hotfix", help="生产 Hotfix 开发线")
    hotfix_sub = hotfix.add_subparsers(dest="hotfix_command", required=True)
    hotfix_create = hotfix_sub.add_parser("create")
    hotfix_create.add_argument("id")
    hotfix_create.add_argument("--branch")
    hotfix_create.add_argument("--base", required=True)
    hotfix_create.add_argument("--repos")
    hotfix_create.add_argument("--repo-base", action="append", metavar="REPOSITORY=REF", help="为一个仓库覆盖 --base；可重复")
    hotfix_create.add_argument("--storage", action="append", metavar="REPOSITORY=MODE", help="仓库存储方式：linked-worktree 或 anchor-reference；可重复")
    hotfix_create.add_argument("--yes", action="store_true")
    hotfix_create.set_defaults(func=cmd_hotfix_create)

    changeset = sub.add_parser("changeset", help="记录与核验跨仓交付提交组合")
    changeset_sub = changeset.add_subparsers(dest="changeset_command", required=True)
    changeset_create = changeset_sub.add_parser("create")
    changeset_create.add_argument("id")
    changeset_create.add_argument("--line", required=True)
    changeset_create.add_argument("--repos", help="逗号分隔；默认该开发线全部仓库")
    changeset_create.set_defaults(func=cmd_changeset_create)
    changeset_sub.add_parser("list").set_defaults(func=cmd_changeset_list)
    changeset_verify = changeset_sub.add_parser("verify")
    changeset_verify.add_argument("id")
    changeset_verify.set_defaults(func=cmd_changeset_verify)

    objective = sub.add_parser("objective", help="持久化并观察一个跨任务 Objective；此阶段不执行 Task")
    objective_sub = objective.add_subparsers(dest="objective_command", required=True)
    objective_start = objective_sub.add_parser("start", help="固定 Objective 合约、目标和依赖闭包")
    objective_start.add_argument("--file", help="完整 Objective v1 TOML 合约")
    objective_start.add_argument("--id")
    objective_start.add_argument("--title")
    objective_start.add_argument("--line")
    objective_start.add_argument("--targets", help="逗号分隔的 Task ID；非文件模式必填")
    objective_start.add_argument("--mode", choices=tuple(item.value for item in RequestedMode))
    objective_start.add_argument("--operation", action="append", choices=tuple(item.value for item in Operation))
    objective_start.add_argument("--yes", action="store_true")
    objective_start.set_defaults(func=cmd_objective_start)
    objective_sub.add_parser("list", help="列出已接受的 Objective").set_defaults(func=cmd_objective_list)
    objective_status = objective_sub.add_parser("status", help="显示 Objective 状态和派生结果")
    objective_status.add_argument("id")
    objective_status.set_defaults(func=cmd_objective_status)
    objective_plan = objective_sub.add_parser("plan", help="只读生成确定性 Objective action plan，不执行任务")
    objective_plan.add_argument("id")
    objective_plan.add_argument("--format", choices=("text", "json"), default="text")
    objective_plan.set_defaults(func=cmd_objective_plan)
    objective_explain = objective_sub.add_parser("explain", help="解释 Objective 当前的可推进项与阻塞原因")
    objective_explain.add_argument("id")
    objective_explain.add_argument("--format", choices=("text", "json"), default="text")
    objective_explain.set_defaults(func=cmd_objective_explain)
    objective_graph = objective_sub.add_parser("graph", help="渲染 Objective、Task、Decision 与 Action 的只读组合图")
    objective_graph.add_argument("id")
    objective_graph.add_argument("--format", choices=("mermaid", "json"), default="mermaid")
    objective_graph.set_defaults(func=cmd_objective_graph)
    for command, function, help_text in (
        ("pause", cmd_objective_pause, "暂停后续推进，不写完成状态"),
        ("resume", cmd_objective_resume, "恢复 paused Objective 的 ownership"),
        ("stop", cmd_objective_stop, "终止 Objective；不能再恢复"),
        ("reconcile", cmd_objective_reconcile, "重新固定 TaskGraph scope 与 contract 哈希"),
    ):
        parser_item = objective_sub.add_parser(command, help=help_text)
        parser_item.add_argument("id")
        parser_item.add_argument("--yes", action="store_true")
        parser_item.set_defaults(func=function)
    objective_scope = objective_sub.add_parser("scope", help="显式调整 Objective targets 并建立新 revision")
    objective_scope_sub = objective_scope.add_subparsers(dest="objective_scope_command", required=True)
    for command, function, help_text in (
        ("add", cmd_objective_scope_add, "将同一开发线的 Task 加入 targets"),
        ("remove", cmd_objective_scope_remove, "从 targets 移除一个 Task"),
    ):
        parser_item = objective_scope_sub.add_parser(command, help=help_text)
        parser_item.add_argument("id")
        parser_item.add_argument("task")
        parser_item.add_argument("--yes", action="store_true")
        parser_item.set_defaults(func=function)

    task = sub.add_parser("task", help="任务编排")
    task_sub = task.add_subparsers(dest="task_command", required=True)
    task_graph = task_sub.add_parser("graph", help="编译、校验或渲染任务图")
    task_graph.add_argument("action", nargs="?", choices=("show", "check"), default="show")
    task_graph.add_argument("--line", help="只显示或校验指定开发线")
    task_graph.add_argument("--format", choices=("mermaid", "json"), default="mermaid")
    task_graph.set_defaults(func=cmd_task_graph)
    task_explain = task_sub.add_parser("explain", help="解释任务当前为什么可调度或被阻塞")
    task_explain.add_argument("id")
    task_explain.set_defaults(func=cmd_task_explain)
    task_attempts = task_sub.add_parser("attempts", help="显示任务的本地执行 provenance")
    task_attempts.add_argument("id")
    task_attempts.set_defaults(func=cmd_task_attempts)
    task_binding = task_sub.add_parser("binding", help="输出 review 所需的完整 attempt 与 plan binding")
    task_binding.add_argument("id")
    task_binding.set_defaults(func=cmd_task_binding)
    task_create = task_sub.add_parser("create")
    task_create.add_argument("id")
    task_create.add_argument("--title", required=True)
    task_create.add_argument("--line", required=True)
    task_create.add_argument("--repository", required=True)
    task_create.set_defaults(func=cmd_task_create)
    task_sub.add_parser("list").set_defaults(func=cmd_task_list)
    task_sub.add_parser("board").set_defaults(func=cmd_task_board)
    task_status_parser = task_sub.add_parser("status")
    task_status_parser.add_argument("id")
    task_status_parser.add_argument("value", nargs="?", choices=STATUSES)
    task_status_parser.add_argument("--force", action="store_true")
    task_status_parser.set_defaults(func=cmd_task_status)
    task_run = task_sub.add_parser("run")
    task_run.add_argument("id")
    task_run.set_defaults(func=cmd_task_run)
    task_open = task_sub.add_parser("open", help="进入已存在的任务工作树，不改变任务状态")
    task_open.add_argument("id")
    task_open.add_argument("--agent", default="codex")
    task_open.add_argument("--prompt", default="")
    task_open.set_defaults(func=cmd_task_open)
    task_claim = task_sub.add_parser("claim", help="由隔离执行器一次性领取任务")
    task_claim.add_argument("id")
    task_claim.add_argument("--by", required=True, help="执行器实例或受信任身份")
    task_claim.add_argument("--key-id", help="与 claim 绑定的 trusted execution key ID")
    task_claim.add_argument("--lease-seconds", type=int, default=3600, help="claim 租约秒数；默认 3600")
    task_claim.add_argument(
        "--output",
        help="把新 claim 以 0600 权限导出到 runner 交接路径；拒绝覆盖",
    )
    task_claim.set_defaults(func=cmd_task_claim)
    task_claim_renew = task_sub.add_parser("claim-renew", help="由当前 runner 续租未过期 claim")
    task_claim_renew.add_argument("id")
    task_claim_renew.add_argument("--by", required=True, help="当前执行器实例或受信任身份")
    task_claim_renew.add_argument("--lease-seconds", type=int, default=3600, help="续租秒数；默认 3600")
    task_claim_renew.set_defaults(func=cmd_task_claim_renew)
    task_claim_release = task_sub.add_parser("claim-release", help="释放当前 runner 的 claim")
    task_claim_release.add_argument("id")
    task_claim_release.add_argument("--by", required=True, help="当前执行器实例或受信任身份")
    task_claim_release.set_defaults(func=cmd_task_claim_release)
    task_next = task_sub.add_parser("next", help="显示或启动下一个满足依赖的任务")
    task_next.add_argument("--id")
    task_next.add_argument("--run", action="store_true")
    task_next.add_argument("--yes", action="store_true")
    task_next.set_defaults(func=cmd_task_next)
    task_answer = task_sub.add_parser("answer")
    task_answer.add_argument("id")
    group = task_answer.add_mutually_exclusive_group(required=True)
    group.add_argument("--text")
    group.add_argument("--file")
    task_answer.set_defaults(func=cmd_task_answer)
    task_gates = task_sub.add_parser("gates")
    task_gates.add_argument("id")
    task_gates.set_defaults(func=cmd_task_gates)
    task_review = task_sub.add_parser("review")
    task_review.add_argument("id")
    task_review.set_defaults(func=cmd_task_review)
    task_signoff = task_sub.add_parser("signoff", help="记录 receipt-bound 外部签收")
    task_signoff.add_argument("id")
    task_signoff.add_argument("--by", required=True, help="签收人或外部审批标识")
    task_signoff.add_argument("--signing-key", help="Ed25519 approver 私钥 PEM")
    task_signoff.add_argument("--key-id", help="已安装到 signoff trust store 的 key ID")
    task_signoff.set_defaults(func=cmd_task_signoff)
    evidence = task_sub.add_parser("evidence", help="构建或导入隔离执行器证据")
    evidence_sub = evidence.add_subparsers(dest="evidence_command", required=True)
    evidence_execution = evidence_sub.add_parser("execution", help="导入执行回执与门禁结果")
    evidence_execution.add_argument("id")
    evidence_input = evidence_execution.add_mutually_exclusive_group(required=True)
    evidence_input.add_argument("--receipt")
    evidence_input.add_argument("--bundle", help="由 task evidence build 生成的可移植 ZIP 证据包")
    evidence_execution.add_argument("--gates", help="外部门禁 JSON；任务含 gates 时必填")
    evidence_execution.add_argument("--heads", help="执行后逐仓 Git HEAD JSON；DONE 回执时必填")
    evidence_execution.add_argument("--provenance", help="可选的外部 execution provenance JSON")
    evidence_execution.add_argument("--allow-legacy", action="store_true", help="显式允许导入缺少 provenance 的旧证据")
    evidence_execution.set_defaults(func=cmd_task_evidence_execution)
    evidence_build = evidence_sub.add_parser("build", help="在隔离 runner 中运行门禁并构建可导入 ZIP 证据包")
    evidence_build.add_argument("id")
    evidence_build.add_argument("--workspace", required=True, help="隔离 runner 中任务分支的多仓工作区")
    evidence_build.add_argument("--receipt", required=True, help="执行器写出的 receipt.md")
    evidence_build.add_argument("--output", required=True, help="新 ZIP 证据包的输出路径；拒绝覆盖已有文件")
    evidence_build.add_argument("--signing-key", help="Ed25519 runner 私钥 PEM")
    evidence_build.add_argument("--key-id", help="已安装到 execution trust store 的 key ID")
    evidence_build.add_argument("--claim", help="控制面导出的 claim.json；默认读取任务目录")
    evidence_build.set_defaults(func=cmd_task_evidence_build)
    evidence_review = evidence_sub.add_parser("review", help="导入 receipt-bound 复核结果")
    evidence_review.add_argument("id")
    evidence_review.add_argument("--file", required=True)
    evidence_review.set_defaults(func=cmd_task_evidence_review)
    evidence_review_build = evidence_sub.add_parser("review-build", help="构建独立 reviewer 签名的 review JSON")
    evidence_review_build.add_argument("id")
    evidence_review_build.add_argument("--file", required=True, help="包含 verdict 与绑定字段的 review.md")
    evidence_review_build.add_argument("--reviewer", required=True)
    evidence_review_build.add_argument("--output", required=True)
    evidence_review_build.add_argument("--signing-key", required=True)
    evidence_review_build.add_argument("--key-id", required=True)
    evidence_review_build.set_defaults(func=cmd_task_evidence_review_build)
    evidence_generations = evidence_sub.add_parser(
        "generations",
        help="列出证据世代，或按年龄与保留数量安全清理非当前世代",
    )
    evidence_generations.add_argument("id")
    evidence_generations.add_argument("--prune", action="store_true", help="执行或预演清理计划")
    evidence_generations.add_argument("--older-than-days", type=int, default=30)
    evidence_generations.add_argument("--keep", type=int, default=10)
    evidence_generations.add_argument("--yes", action="store_true")
    evidence_generations.set_defaults(func=cmd_task_evidence_generations)
    task_merge = task_sub.add_parser("merge")
    task_merge.add_argument("id")
    task_merge.add_argument("--yes", action="store_true")
    task_merge.add_argument("--push", action="store_true")
    task_merge.set_defaults(func=cmd_task_merge)
    task_sub.add_parser("decisions").set_defaults(func=cmd_task_decisions)
    task_sub.add_parser("stats").set_defaults(func=cmd_task_stats)
    task_sub.add_parser("loop").set_defaults(func=cmd_task_loop)
    daemon = task_sub.add_parser("daemon")
    daemon.add_argument("--parallel", type=int, default=2)
    daemon.add_argument("--interval", type=int, default=30)
    daemon.add_argument("--once", action="store_true")
    daemon.set_defaults(func=cmd_task_daemon)
    return parser


def _route_experiment_surface(raw: list[str]) -> tuple[str, list[str]] | None:
    command_index = 0
    while command_index < len(raw):
        token = raw[command_index]
        if token in {"--root", "--workspace"}:
            command_index += 2
            continue
        if token.startswith("--root=") or token.startswith("--workspace="):
            command_index += 1
            continue
        if token == "--dry-run":
            command_index += 1
            continue
        break
    if command_index >= len(raw) or raw[command_index] != "dispatch":
        return None

    common = argparse.ArgumentParser(add_help=False)
    location = common.add_mutually_exclusive_group()
    location.add_argument("--root")
    location.add_argument("--workspace", dest="workspace_alias")
    common.add_argument("--dry-run", action="store_true")
    global_args = common.parse_args(raw[:command_index])
    surface = raw[command_index]
    forwarded = list(raw[command_index + 1 :])
    if surface == "dispatch":
        dispatch_common = argparse.ArgumentParser(add_help=False)
        dispatch_common.add_argument("--home")
        dispatch_common.add_argument("--dry-run", action="store_true")
        _, dispatch_remaining = dispatch_common.parse_known_args(forwarded)
        dispatch_command = (
            dispatch_remaining[0] if dispatch_remaining else ""
        )
        selected_root = global_args.root
        if global_args.workspace_alias:
            selected_root = str(get_workspace(global_args.workspace_alias).root)
        if (
            selected_root
            and dispatch_command in {"run", "panel"}
            and not any(
                token == "--project" or token.startswith("--project=")
                for token in forwarded
            )
        ):
            forwarded.extend(["--project", selected_root])
        if global_args.dry_run:
            forwarded.insert(0, "--dry-run")
    return surface, forwarded


def _should_run_daily_update(
    args: argparse.Namespace, *, interactive: bool | None = None
) -> bool:
    opt_out = os.environ.get("DYRO_NO_UPDATE_CHECK", "").strip().lower()
    if opt_out in {"1", "true", "yes", "on"}:
        return False
    if getattr(args, "dry_run", False):
        return False
    if getattr(args, "command", None) not in {None, "home", "start"}:
        return False
    if interactive is None:
        interactive = sys.stdin.isatty() and sys.stdout.isatty()
    return interactive


def _maybe_run_daily_update(*, install=perform_update) -> None:
    try:
        result = check_for_update(__version__)
        if not result.checked or result.error or result.kind == UpdateKind.NONE:
            return
        state = load_update_state()
    except (DyroError, OSError):
        return
    print(
        f"\n发现 Dyro {result.latest_version}（当前 {result.current_version}）。"
    )
    if state.auto_patch and result.kind == UpdateKind.PATCH:
        print("已开启补丁版本自动更新，正在安全更新……")
        try:
            updated = install(
                result.latest_version,
                yes=True,
                dry_run=False,
            )
        except (DyroError, OSError) as exc:
            print(f"自动更新失败：{exc}")
            print("本次启动继续使用当前版本；稍后可运行 dyro update now 重试。")
            return
        if updated:
            print("自动更新完成；本次启动继续运行，下次将使用新版本。")
        return
    print("运行 dyro update now 可一键确认更新；今天不再重复提示。")


def main(argv: list[str] | None = None) -> None:
    import sys

    # The optional local dispatch surface ships in the dyro wheel.
    raw = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    try:
        experiment = _route_experiment_surface(raw)
        if experiment is not None and experiment[0] == "dispatch":
            from experiments.local_agent_dispatch.cli import main as dispatch_main

            raise SystemExit(dispatch_main(experiment[1]))
        args = parser.parse_args(argv)
        if _should_run_daily_update(args):
            _maybe_run_daily_update()
        if hasattr(args, "func"):
            args.func(args)
        else:
            cmd_home(args)
    except DyroError as exc:
        parser.exit(2, f"错误：{exc}\n")


if __name__ == "__main__":
    main()

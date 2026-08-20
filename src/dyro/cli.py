from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import subprocess
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
from .changesets import (
    create_changeset,
    get_changeset,
    list_changesets,
    verify_changeset,
)
from .config import CONFIG_NAME, Config, load, load_profile_exact, push_disclosure, push_policy_fields, validate_id
from .console.launcher import launch_console, render_console_plan
from .continuation.attention import (
    build_attention_projection,
    render_attention_json,
    render_attention_text,
)
from .continuation.briefing import (
    ATTENTION_CLOSER,
    TICK_CLOSER,
    arrival_lines,
    briefing_payload,
    follow_up_argv,
    render_briefing_text,
    render_human_attention,
    render_human_wave,
)
from .continuation.ready_briefing import briefing_command, build_ready_briefing
from .continuation.engine import (
    build_scheduler_tick,
    render_scheduler_tick_text,
    scheduler_tick_payload,
)
from .continuation.models import Operation, RequestedMode
from .continuation.planner import (
    build_continuation_plan,
    build_scheduler_projection,
    continuation_plan_payload,
    render_plan_text,
    render_projection_json,
    render_projection_mermaid,
)
from .continuation.resolution import (
    ResolvedWorkspace,
    WorkspaceResolutionError,
    WorkspaceResolutionFailure,
    WorkspaceResolutionSource,
    resolve_line,
    resolve_workspace,
    resolve_workspace_readonly,
)
from .continuation.snapshot import (
    build_scheduler_snapshot,
    build_scheduler_snapshot_bounded,
)
from .continuation.store import (
    add_objective_target,
    create_objective,
    derive_objective_result,
    get_objective,
    list_objectives,
    pause_objective,
    preview_objective_wave_budgets,
    reconcile_objective,
    remove_objective_target,
    render_budget_preview_text,
    resume_objective,
    stop_objective,
)
from .continuation.supervision import (
    apply_supervised_wave,
    build_supervised_wave,
    render_supervised_outcomes,
    render_supervised_wave_text,
    supervised_outcomes_payload,
    supervised_wave_payload,
)
from .continuation.triggers import (
    TriggerConfig,
    TriggerKind,
    TriggerProbeInput,
    probe_builtin,
)
from .evidence import build_execution_bundle, unpack_execution_bundle
from .errors import DyroError, ValidationError
from .home import (
    HomeTool,
    _record_for_root,
    existing_line_workspace,
    home_tools,
    launch_start_tool,
    launcher_tools,
    open_line,
    open_task,
    print_agent_discovery,
    print_all_status,
    print_status,
    ready_home_tools,
    resolve_home_config,
    resolve_start_tool,
    run_home,
    sort_home_tools,
)
from .hub import (
    WorkspaceRecord,
    WorkspaceRegistrationPlan,
    add_workspace,
    get_workspace,
    load_registry,
    load_registry_bounded,
    preview_workspace_registration,
    remove_workspace,
    set_default_workspace,
)
from .image_sidecar import (
    ABSENT_INFO_LINE,
    SOURCE_URL,
    discover_sidecar,
    install_image_sidecar,
    probe_sidecar,
    require_interactive_install,
)
from .integrations import (
    INTEGRATION_CHOICES,
    IntegrationState,
    install_integration,
    integration_status,
    sync_managed_skill,
    uninstall_integration,
)
from .integrations.seats import COMPANION_IDS, managed_skill_bundle
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
    validate_bootstrap_destination,
)
from .read_limits import ObservationLimits, ReadBudget, ReadLimitCode, ReadLimitError
from .profile import (
    append_adapter,
    command_adapter,
    config_value,
    installed_launchable_presets,
    launchable_preset_ids,
    preset_adapter,
    set_config_value,
    test_adapter,
)
from .state import atomic_write_text, exclusive_lock
from .terminal import danger, muted, success, title, value as terminal_value, warning
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


CONFIG_TEMPLATE = """schema_version = 1

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
"""


_MANAGED_SKILL_BUNDLE: tuple[tuple[str, str], ...] = managed_skill_bundle()


def _config(args: argparse.Namespace) -> Config:
    root_arg = getattr(args, "root", None)
    workspace_arg = getattr(args, "workspace_alias", None)
    if getattr(args, "format", None) == "json":
        budget = _control_plane_budget(args)
        if root_arg:
            root = Path(root_arg).expanduser()
            if not root.is_absolute():
                root = Path.cwd() / root
            try:
                profile = load_profile_exact(root, budget)
            except ReadLimitError as exc:
                if exc.code is not ReadLimitCode.UNSAFE_FILE:
                    raise
                raise WorkspaceResolutionError(
                    WorkspaceResolutionFailure.LOCAL_PROFILE_INVALID
                ) from exc
            except PermissionError as exc:
                raise WorkspaceResolutionError(
                    WorkspaceResolutionFailure.HOST_READ_PERMISSION_REQUIRED
                ) from exc
            except (OSError, ValidationError) as exc:
                raise WorkspaceResolutionError(
                    WorkspaceResolutionFailure.LOCAL_PROFILE_INVALID
                ) from exc
            resolved = ResolvedWorkspace(
                profile,
                WorkspaceResolutionSource.EXPLICIT,
                None,
            )
        else:
            resolved = resolve_workspace_readonly(
                start=None,
                workspace=workspace_arg,
                cwd=Path.cwd().absolute(),
                budget=budget,
            )
        setattr(args, "_control_plane_resolution", resolved)
        return resolved.profile.config
    if root_arg:
        root = Path(root_arg).expanduser()
    elif workspace_arg:
        root = get_workspace(workspace_arg).root
    else:
        interactive = sys.stdin.isatty() and sys.stdout.isatty()
        return resolve_workspace(
            start=Path.cwd(),
            interactive=interactive,
            chooser=(lambda label, values: _choose(label, list(values)))
            if interactive
            else None,
        )
    return load(root)


def _control_plane_budget(args: argparse.Namespace) -> ReadBudget:
    existing = getattr(args, "_control_plane_read_budget", None)
    if isinstance(existing, ReadBudget):
        return existing
    budget = ReadBudget(ObservationLimits())
    setattr(args, "_control_plane_read_budget", budget)
    return budget


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
        raise DyroError(
            f"{label} 会创建或修改 Git worktree；请先使用 --dry-run 检查，再加 --yes 执行"
        )


def _require_objective_yes(args: argparse.Namespace, label: str) -> None:
    if not args.yes and not args.dry_run:
        raise DyroError(
            f"{label} 会修改 Objective 状态；请先使用 --dry-run 检查，再加 --yes 执行"
        )


def _objective_contract_from_args(args: argparse.Namespace, config: Config) -> str:
    if args.file:
        path = Path(args.file).expanduser()
        if not path.is_file() or path.is_symlink():
            raise DyroError(f"Objective 合约文件必须是安全的普通文件：{path}")
        return path.read_text(encoding="utf-8")
    if not args.id or not args.title:
        raise DyroError("非文件模式必须提供 --id 与 --title")
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    selected_line = (
        args.line
        or resolve_line(
            config,
            interactive=interactive,
            chooser=(lambda label, values: _choose(label, list(values)))
            if interactive
            else None,
        ).id
    )
    targets = tuple(
        item.strip() for item in (args.targets or "").split(",") if item.strip()
    )
    if not targets:
        raise DyroError(
            "非文件模式必须提供 --targets TASK_ID[,TASK_ID...]；交互选择将在后续版本提供"
        )
    mode = args.mode or RequestedMode.SUPERVISED.value
    operations = tuple(
        args.operation or (Operation.EXECUTE.value, Operation.REVIEW.value)
    )
    return "\n".join(
        (
            "schema_version = 1",
            f"id = {json.dumps(args.id, ensure_ascii=False)}",
            f"title = {json.dumps(args.title, ensure_ascii=False)}",
            f"line = {json.dumps(selected_line, ensure_ascii=False)}",
            "targets = ["
            + ", ".join(json.dumps(item, ensure_ascii=False) for item in targets)
            + "]",
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


def _print_control_plane_json(
    kind: str, *, stream=None, **payload: object
) -> None:
    print(
        json.dumps(
            {"schema_version": 1, "kind": kind, **payload},
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ),
        file=stream,
    )


def _finding_payload(finding: str) -> dict[str, str]:
    status, separator, message = finding.partition(" ")
    return {
        "status": status if separator else "UNKNOWN",
        "message": message if separator else finding,
    }


def _doctor_finding_payload(
    finding: str, *, include_paths: bool
) -> dict[str, str]:
    payload = _finding_payload(finding)
    if include_paths:
        return payload
    message = payload["message"]
    if not message.startswith("repository "):
        return payload
    identity, separator, detail = message.partition(": ")
    if not separator:
        payload["message"] = "repository: unavailable"
    elif payload["status"] == "PASS":
        payload["message"] = f"{identity}: ready"
    elif detail.startswith("missing or not Git:"):
        payload["message"] = f"{identity}: missing or not Git"
    else:
        payload["message"] = f"{identity}: unavailable"
    return payload


def _status_payload(
    config: Config, *, read_budget: ReadBudget | None = None
) -> dict[str, object]:
    return {
        "workspace": config.name,
        **push_policy_fields(config.policy),
        "rows": [
            {
                "scope": scope,
                "repository": repository,
                "branch": branch,
                "head": head,
                "upstream": upstream,
                "dirty_count": dirty,
            }
            for scope, repository, branch, head, upstream, dirty in status_rows(
                config, read_budget=read_budget
            )
        ],
    }


def _control_plane_command(args: argparse.Namespace) -> str:
    parts: list[str] = []
    for attribute in (
        "command",
        "workspace_command",
        "integration_command",
        "image_command",
        "line_command",
        "changeset_command",
        "objective_command",
        "objective_scope_command",
    ):
        value = getattr(args, attribute, None)
        if isinstance(value, str) and value and value not in parts:
            parts.append(value)
    return " ".join(parts) or "dyro"


def _control_plane_error_code(
    args: argparse.Namespace, exc: BaseException
) -> str:
    code = getattr(exc, "code", None)
    if hasattr(code, "value"):
        return str(code.value)
    if isinstance(code, str) and code:
        return code
    if isinstance(exc, ReadLimitError):
        return exc.code.value
    if isinstance(exc, ValidationError):
        return "VALIDATION_ERROR"
    if isinstance(exc, OSError):
        return "IO_ERROR"
    command = getattr(args, "command", "")
    return {
        "changeset": "CHANGESET_UNAVAILABLE",
        "doctor": "WORKSPACE_UNHEALTHY",
        "image": "SIDECAR_UNREADABLE",
        "integration": "INTEGRATION_UNAVAILABLE",
        "line": "LINE_UNAVAILABLE",
        "next": "NEXT_STEP_UNAVAILABLE",
        "objective": "OBJECTIVE_UNAVAILABLE",
        "status": "WORKSPACE_OBSERVATION_FAILED",
        "workspace": "WORKSPACE_REGISTRY_UNAVAILABLE",
    }.get(command, "DYRO_ERROR")


def _print_control_plane_error(
    args: argparse.Namespace, exc: BaseException
) -> None:
    _print_control_plane_json(
        "error",
        stream=sys.stderr,
        code=_control_plane_error_code(args, exc),
        command=_control_plane_command(args),
        retryable=False,
    )


def _workspace_selector_argv(
    args: argparse.Namespace, config: Config
) -> tuple[str, ...]:
    alias = getattr(args, "workspace_alias", None)
    if alias:
        return ("dyro", "--workspace", alias)
    return ("dyro", "--root", str(config.root))


def _scoped_command(
    args: argparse.Namespace, config: Config, *command: str
) -> str:
    return shlex.join((*_workspace_selector_argv(args, config), *command))


def _briefing_command(
    args: argparse.Namespace, config: Config, *command: str
) -> str:
    """Scope a read-only briefing command without embedding --root paths."""
    alias = getattr(args, "workspace_alias", None) or config.name
    return briefing_command(str(alias), *command)


def _workspace_ready_briefing(
    args: argparse.Namespace,
    config: Config,
    read_budget: ReadBudget | None,
) -> tuple[dict[str, object] | None, list[str]]:
    """Attach a switch-tool opening when live Objectives exist.

    `next.commands` stays empty. The briefing command is a read, not a mutation.
    """
    alias = getattr(args, "workspace_alias", None) or config.name
    return build_ready_briefing(config, alias=str(alias), read_budget=read_budget)


def _ready_next_summary(briefing: dict[str, object] | None) -> str:
    if briefing is None:
        return "工作区已就绪。"
    if briefing.get("available") is not True:
        return "工作区已就绪，但目标简报未读到。"
    objective_id = briefing.get("objective_id")
    if isinstance(objective_id, str) and objective_id:
        lines = briefing.get("lines")
        if isinstance(lines, list) and lines and isinstance(lines[0], str):
            return lines[0]
    matter = briefing.get("matter")
    if isinstance(matter, str) and matter:
        return matter
    return "工作区已就绪。"


def _objective_payload(
    config: Config,
    record,
    *,
    detailed: bool,
    read_budget: ReadBudget | None = None,
) -> dict[str, object]:
    if read_budget is None:
        derived_result = derive_objective_result(config, record)
    else:
        snapshot = build_scheduler_snapshot_bounded(
            config, objective=record, budget=read_budget
        )
        derived_result = build_continuation_plan(snapshot).completion.value
    payload: dict[str, object] = {
        "id": record.objective.id,
        "operator_state": record.operator_state,
        "derived_result": derived_result,
        "revision": record.revision,
        "line": record.objective.line,
        "targets": list(record.objective.targets),
    }
    if detailed:
        payload.update(
            {
                "scope": list(record.scope),
                "contract_sha256": record.contract_sha256,
            }
        )
    return payload


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
            raise DyroError(
                "未发现 Git 仓库；可先 clone 仓库，或使用 dyro init --wizard"
            )
        content = render_config(args.name, repositories, args.base)
    else:
        content = CONFIG_TEMPLATE.format(name=args.name)
    atomic_write_text(config_file, content)
    for relative in (".dyro/tasks", ".dyro/lines", ".dyro/hotfixes", ".dyro/changes"):
        (root / relative).mkdir(parents=True, exist_ok=True)
    print(f"已初始化 {root}")
    if args.discover:
        print(
            f"已自动登记 {len(repositories)} 个本地 Git 仓库；下一步：运行 dyro doctor"
        )
    else:
        print("下一步：登记 repositories，随后运行 dyro doctor")


def _default_workspace_name(root: Path) -> str:
    candidate = "".join(
        character if character.isascii() and character.isalnum() else "-"
        for character in root.name
    ).strip("-._")
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


def _ask_setup_choice(
    heading: str,
    choices: tuple[tuple[str, str], ...],
    *,
    default: str,
    aliases: dict[str, str] | None = None,
    allow_alias_target: bool = False,
) -> str:
    """Ask one small, recommendation-first onboarding question."""

    values = {value for value, _ in choices}
    if default not in values:
        raise ValueError("首次设置默认选项必须在候选项中")
    print("\n" + heading)
    for value, label in choices:
        print(f"  {value}) {label}")
    raw = input(f"请选择（直接回车默认 {default}）：").strip().lower()
    if not raw:
        return default
    selected = (aliases or {}).get(raw, raw)
    if selected not in values and not (allow_alias_target and raw in (aliases or {})):
        raise DyroError("请输入列表中的编号或工具 ID")
    return selected


@dataclass(frozen=True)
class SetupPersonalPreferences:
    """Global, reversible preferences collected before setup applies its plan."""

    check_enabled: bool
    auto_patch: bool
    default_tool: str | None
    make_default_workspace: bool
    install_skill: bool


def _setup_update_preferences() -> tuple[bool, bool]:
    current = load_update_state()
    check_choice = _ask_setup_choice(
        "更新检测（仅每日首次交互启动；失败不会阻塞进入项目）",
        (
            ("1", "开启每日更新检测（推荐）"),
            ("2", "关闭更新检测"),
        ),
        default="1" if current.check_enabled else "2",
    )
    if check_choice == "2":
        return False, False
    auto_choice = _ask_setup_choice(
        "补丁版本自动更新（只允许 X.Y.Z → X.Y.Z，同一主次版本）",
        (
            ("1", "保持手动更新（推荐）"),
            ("2", "自动安装补丁更新"),
        ),
        default="2" if current.auto_patch else "1",
    )
    return True, auto_choice == "2"


def _setup_default_tool(root: Path, provider_preset: str | None) -> str | None:
    available = [tool for tool in launcher_tools(root) if tool.available]
    if not available:
        print(muted("未检测到可直接打开的编码工具；稍后可用 dyro tool default 设置。"))
        return None
    current = load_tool_preferences().default_tool
    recommended = next(
        (tool.id for tool in available if tool.id == provider_preset),
        "",
    )
    if not recommended and current in {tool.id for tool in available}:
        recommended = current
    ordered = sorted(
        available,
        key=lambda tool: (tool.id != recommended, tool.label.casefold(), tool.id),
    )
    visible = ordered[:3]

    def choose(candidates: list[HomeTool], *, show_all: bool) -> str:
        choices = [("0", "暂不设置个人默认工具（每次自行选择）")]
        aliases = {
            "none": "0",
            "skip": "0",
            **{tool.id: f"tool:{tool.id}" for tool in ordered},
        }
        for index, tool in enumerate(candidates, start=1):
            marker = "（推荐）" if tool.id == recommended else ""
            choices.append((str(index), f"{tool.label}{marker}"))
        if not show_all and len(ordered) > len(visible):
            choices.append(("m", "查看全部已检测工具"))
            aliases["more"] = "m"
        default = next(
            (
                str(index)
                for index, tool in enumerate(candidates, start=1)
                if tool.id == recommended
            ),
            "0",
        )
        return _ask_setup_choice(
            "常用编码工具（只影响首页排序，不会自动启动或安装工具）"
            if not show_all
            else "全部已检测编码工具",
            tuple(choices),
            default=default,
            aliases=aliases,
            allow_alias_target=True,
        )

    selected = choose(visible, show_all=False)
    if selected.startswith("tool:"):
        return selected.removeprefix("tool:")
    if selected == "m":
        selected = choose(ordered, show_all=True)
        selected_tools = ordered
    else:
        selected_tools = visible
    if selected.startswith("tool:"):
        return selected.removeprefix("tool:")
    if selected == "0":
        return ""
    return selected_tools[int(selected) - 1].id


def _setup_default_workspace(
    registration: WorkspaceRegistrationPlan | None,
    args: argparse.Namespace,
) -> bool:
    if registration is None or args.make_default or registration.becomes_default:
        return bool(args.make_default)
    registry = load_registry()
    if registry.default == registration.name:
        return False
    selected = _ask_setup_choice(
        "Console 默认项目（裸 dyro 会直接进入该项目）",
        (
            ("1", f"保持 {registry.default} 为默认项目（推荐）"),
            ("2", f"将 {registration.name} 设为默认项目"),
        ),
        default="1",
    )
    return selected == "2"


def _setup_skill_preference() -> bool:
    """Ask whether to install/sync the first-party Skill bundle during setup."""
    statuses = {
        integration: integration_status(integration)
        for integration, _label in _MANAGED_SKILL_BUNDLE
    }
    if all(
        status.state is IntegrationState.CURRENT for status in statuses.values()
    ):
        print(muted("Dyro Skills 已是当前版本；无需在 setup 中重复安装。"))
        return False
    blocked = {
        integration: status
        for integration, status in statuses.items()
        if status.state
        in {
            IntegrationState.DRIFTED,
            IntegrationState.UNOWNED_CONFLICT,
            IntegrationState.STALE_MANIFEST,
            IntegrationState.RECOVERY_REQUIRED,
        }
    }
    if blocked:
        detail = "；".join(
            f"{integration}={status.state.value}（{status.detail}）"
            for integration, status in blocked.items()
        )
        print(
            warning(
                f"Dyro Skills 状态需要人工处理：{detail}；"
                "setup 不会自动改写，请先运行对应的 "
                "dyro integration status <id>。"
            )
        )
        return False
    hosts = sorted(
        {
            row.host
            for status in statuses.values()
            for row in status.avatars
        }
    )
    if hosts:
        host_text = "、".join(hosts)
        prompt = f"Dyro Skills（控制面 + 执行 + 评审板 + Dispatch；挂接到：{host_text}）"
        option_one = "安装 / 同步到已检测宿主（推荐）"
        default = "1"
    else:
        prompt = "Dyro Skills（当前未检测到 Agent 宿主目录）"
        option_one = "仍要尝试安装（当前无宿主，预期失败）"
        default = "2"
    selected = _ask_setup_choice(
        prompt,
        (
            ("1", option_one),
            ("2", "稍后手动安装（dyro integration install skill / executor / board / dispatch）"),
        ),
        default=default,
    )
    return selected == "1"


def _skill_status_blocks_automatic_change(status: IntegrationState) -> bool:
    return status in {
        IntegrationState.DRIFTED,
        IntegrationState.UNOWNED_CONFLICT,
        IntegrationState.STALE_MANIFEST,
        IntegrationState.RECOVERY_REQUIRED,
    }


def _setup_personal_preferences(
    *,
    root: Path,
    provider_preset: str | None,
    registration: WorkspaceRegistrationPlan | None,
    args: argparse.Namespace,
) -> SetupPersonalPreferences:
    check_enabled, auto_patch = _setup_update_preferences()
    default_tool = _setup_default_tool(root, provider_preset)
    make_default_workspace = _setup_default_workspace(registration, args)
    install_skill = _setup_skill_preference()
    return SetupPersonalPreferences(
        check_enabled=check_enabled,
        auto_patch=auto_patch,
        default_tool=default_tool,
        make_default_workspace=make_default_workspace,
        install_skill=install_skill,
    )


def _render_setup_personal_preferences(
    preferences: SetupPersonalPreferences,
) -> None:
    update_summary = (
        (
            "每日检测；补丁自动更新"
            if preferences.auto_patch
            else "每日检测；补丁保持手动更新"
        )
        if preferences.check_enabled
        else "关闭每日检测与补丁自动更新"
    )
    print("  - 更新：" + update_summary)
    if preferences.default_tool is None:
        print("  - 编码工具：" + muted("保持当前个人偏好"))
    elif preferences.default_tool:
        print("  - 编码工具：个人默认 " + terminal_value(preferences.default_tool))
    else:
        print("  - 编码工具：" + muted("不设置个人默认"))
    if preferences.install_skill:
        statuses = {
            integration: integration_status(integration)
            for integration, _label in _MANAGED_SKILL_BUNDLE
        }
        if all(
            status.state is IntegrationState.ABSENT and not status.avatars
            for status in statuses.values()
        ):
            print("  - Skills：无法安装：未检测到 Agent 宿主目录")
            print(
                "      · 确认后会 soft-fail；请安装宿主后运行 "
                "dyro integration install skill / executor / board / dispatch"
            )
            return
        plans = []
        for integration, label in _MANAGED_SKILL_BUNDLE:
            plan = sync_managed_skill(
                integration,
                yes=False,
                dry_run=True,
                allow_first_install=True,
            )
            if plan is not None:
                plans.append((label, plan))
        if not plans:
            print("  - Skills：" + muted("已是当前版本"))
        else:
            print("  - Skills：安装 / 同步控制面、执行、评审板与 Dispatch（镜像 + 宿主分身）")
            for label, plan in plans:
                print(f"      · {label}")
                for change in plan.changes:
                    print("        · " + change)
    else:
        print("  - Skills：" + muted("稍后手动安装"))


def _apply_setup_personal_preferences(preferences: SetupPersonalPreferences) -> str:
    """Apply personal preferences; return Skill-bundle setup outcome."""
    if preferences.check_enabled:
        set_update_enabled(True)
        set_auto_patch(preferences.auto_patch)
    else:
        set_update_enabled(False)
    if preferences.default_tool is not None:
        set_default_tool(preferences.default_tool)
    if not preferences.install_skill:
        return "skipped"
    plans = []
    failures = []
    for integration, label in _MANAGED_SKILL_BUNDLE:
        try:
            plan = sync_managed_skill(
                integration,
                yes=True,
                allow_first_install=True,
            )
        except DyroError as exc:
            failures.append((integration, label, exc))
            continue
        if plan is not None:
            plans.append((label, plan))
    for integration, label, exc in failures:
        print(
            warning(
                f"{label} Skill 未安装成功：{exc}；可稍后运行 "
                f"dyro integration install {integration} --dry-run 排查。"
            )
        )
    if failures:
        return "failed"
    if not plans:
        print(muted("Dyro Skills 已是当前版本。"))
        return "current"
    print(success("已安装 / 同步 Dyro Skills。"))
    for label, plan in plans:
        print(f"  - {label}")
        for change in plan.changes:
            print("    - " + change)
    return "success"


def _normalize_provider_presets(value: object) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(value)


def _setup_provider_preset() -> tuple[str, ...]:
    """Register every installed launchable Agent the user confirms."""

    discovered = installed_launchable_presets()
    if not discovered:
        print(
            "未发现本机 Agent；可稍后运行 dyro agent add <id> --command '…'，"
            "或直接 dyro start 使用已安装工具。"
        )
        return ()
    labels = "、".join(discovered)
    print("检测到本机编码工具：" + labels)
    question = (
        "将它加入 Dyro Profile 吗"
        if len(discovered) == 1
        else "将它们加入 Dyro Profile 吗"
    )
    if _ask_yes_no(question, default=True):
        return discovered
    return ()


def _setup_registration_plan(
    root: Path,
    name: str,
    args: argparse.Namespace,
    *,
    make_default: bool | None = None,
) -> WorkspaceRegistrationPlan | None:
    if args.no_register:
        return None
    return preview_workspace_registration(
        root,
        name=name,
        make_default=args.make_default if make_default is None else make_default,
    )


def _render_setup_registration_plan(
    registration: WorkspaceRegistrationPlan | None,
) -> None:
    if registration is None:
        print("  - Console：" + muted("不登记全局入口（--no-register）"))
        return
    name = registration.name
    root = registration.root
    action = (
        "已登记，保持现有入口" if registration.already_registered else "将登记全局入口"
    )
    default = "；设为默认项目" if registration.becomes_default else "；不改变默认项目"
    print(
        f"  - Console：{action} {terminal_value(name)} → {terminal_value(root)}{default}"
    )


def _register_setup_workspace(
    config: Config, *, register: bool, make_default: bool
) -> WorkspaceRecord | None:
    if not register:
        return None
    record = add_workspace(config.root, name=config.name, make_default=make_default)
    default = "（默认项目）" if load_registry().default == record.name else ""
    print(success(f"已登记全局入口：{record.name}{default}"))
    return record


def _print_setup_completion(
    config: Config,
    registration: WorkspaceRecord | None,
    preferences: SetupPersonalPreferences | None = None,
    *,
    skill_outcome: str = "skipped",
) -> None:
    print("\n" + success("━━ 设置完成 ━━"))
    print(f"  - Profile：{terminal_value(config.name)}")
    if registration is None:
        print("  - Console：" + muted("未登记（--no-register）"))
    else:
        print(
            f"  - Console：{terminal_value(registration.name)} 已可在 dyro console 中查看"
        )
    if preferences is not None:
        update = "每日检测已开启" if preferences.check_enabled else "每日检测已关闭"
        patch = (
            "补丁自动更新已开启"
            if preferences.auto_patch
            else "补丁保持手动更新"
            if preferences.check_enabled
            else "补丁自动更新已关闭"
        )
        print(f"  - 更新：{update}；{patch}")
        if preferences.default_tool is not None:
            tool = preferences.default_tool or "未设置"
            print(f"  - 编码工具：个人默认 {terminal_value(tool)}")
        if skill_outcome == "success":
            print("  - Skills：已安装 / 同步")
        elif skill_outcome == "current":
            print("  - Skills：" + muted("已是当前版本"))
        elif skill_outcome == "failed":
            print(
                "  - Skills："
                + warning(
                    "安装未成功；可运行 dyro integration status skill / executor / board / dispatch 排查"
                )
            )
        else:
            print("  - Skills：" + muted("未在 setup 中安装"))
    print("下一步：" + terminal_value("dyro start"))


def _render_interactive_setup_plan(
    plan: SetupPlan,
    registration: WorkspaceRegistrationPlan | None,
    preferences: SetupPersonalPreferences,
) -> None:
    print("\n" + title("━━ 设置计划 ━━"))
    print(muted("尚未修改任何文件。"))
    for item in render_setup_plan(plan):
        print("  - " + item)
    _render_setup_registration_plan(registration)
    _render_setup_personal_preferences(preferences)
    if plan.needs_bootstrap:
        print("  - " + muted("将仅 clone 缺失且已明确提供 remote 的仓库"))
    print("  - " + muted("不会移动、覆盖或清理现有 Git 仓库"))


def _print_doctor_finding(finding: str) -> None:
    if finding.startswith("PASS"):
        print(success(finding))
    elif finding.startswith("WARN"):
        print(warning(finding))
    elif finding.startswith("FAIL"):
        print(danger(finding))
    else:
        print(finding)


def _apply_setup_plan(
    plan: SetupPlan,
    *,
    dry_run: bool,
    register: bool,
    make_default: bool,
    preferences: SetupPersonalPreferences,
) -> None:
    if dry_run:
        print("DRY RUN: 上述计划不会写入文件、执行 clone 或创建 Git worktree")
        return
    config_file = plan.root / CONFIG_NAME
    if config_file.exists():
        raise DyroError(f"配置已存在：{config_file}")
    plan.root.mkdir(parents=True, exist_ok=True)
    adapter_presets = plan.provider_presets or (
        (plan.provider_preset,) if plan.provider_preset else ()
    )
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
    registration = _register_setup_workspace(
        config, register=register, make_default=make_default
    )
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
        print(
            success("已创建开发线 ")
            + terminal_value(line.id)
            + f"（{terminal_value(line.branch)}）"
        )
    findings = doctor(config)
    for finding in findings:
        _print_doctor_finding(finding)
    if any(finding.startswith("FAIL") for finding in findings):
        raise DyroError("设置已保存，但 doctor 发现问题；请修复后运行 dyro next")
    skill_outcome = _apply_setup_personal_preferences(preferences)
    _print_setup_completion(
        config, registration, preferences, skill_outcome=skill_outcome
    )


def _interactive_setup(args: argparse.Namespace) -> None:
    root = Path(args.path).expanduser().resolve()
    config_file = root / CONFIG_NAME
    suggested_base = args.base or "main"
    print("\n" + title("━━ Dyro 首次设置 ━━"))
    print(muted("先检查环境，确认前不会修改文件。"))
    if config_file.exists():
        config = load(root)
        registration = _setup_registration_plan(config.root, config.name, args)
        print(
            f"已发现 Profile：{terminal_value(config_file)}（{len(config.repositories)} 个仓库）"
        )
        for finding in doctor(config):
            _print_doctor_finding(finding)
        preferences = _setup_personal_preferences(
            root=config.root,
            provider_preset="codex" if "codex" in config.adapters else None,
            registration=registration,
            args=args,
        )
        registration = _setup_registration_plan(
            config.root,
            config.name,
            args,
            make_default=preferences.make_default_workspace,
        )
        print("\n" + title("━━ 设置计划 ━━"))
        print(muted("尚未修改任何文件。"))
        _render_setup_registration_plan(registration)
        _render_setup_personal_preferences(preferences)
        if args.dry_run:
            print("DRY RUN: 上述个人偏好和全局入口不会写入。")
            return
        if not args.yes and not _ask_yes_no("应用这些个人偏好", default=False):
            print("已取消；没有修改任何偏好或全局入口。")
            return
        record = _register_setup_workspace(
            config,
            register=registration is not None,
            make_default=preferences.make_default_workspace,
        )
        skill_outcome = _apply_setup_personal_preferences(preferences)
        _print_setup_completion(
            config, record, preferences, skill_outcome=skill_outcome
        )
        return

    repositories = (
        discover_repositories(root)
        if root.exists() and not is_git_repository(root)
        else []
    )
    if is_git_repository(root):
        remote = origin_url(root)
        if not remote:
            print(
                warning(
                    "当前目录是 Git 仓库，但没有 origin。Dyro 不会把控制状态写进该仓库。"
                )
            )
            print(
                muted(
                    "请先为它配置 origin，或在包含多个仓库的独立目录中运行 dyro setup。"
                )
            )
            return
        source_branch = current_branch(root)
        if source_branch and not args.base:
            suggested_base = source_branch
        suggested_root = sibling_workspace_for(root)
        raw_root = _ask_value(
            "为这个项目创建独立 Dyro 工作区", default=str(suggested_root)
        )
        root = Path(raw_root).expanduser().resolve()
        if root.exists() and any(root.iterdir()):
            raise DyroError(f"建议工作区必须为空或不存在：{root}")
        repository = repository_from_remote(remote)
        repositories = [repository]
        print(muted("将从当前仓库的 origin clone 新 anchor；当前仓库保持不变。"))
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
    presets = _normalize_provider_presets(_setup_provider_preset())
    plan = SetupPlan(
        root=root,
        name=name,
        repositories=tuple(repositories),
        default_base=base,
        line_id=line_id or None,
        branch=f"feat/{line_id}" if line_id else None,
        provider_preset=presets[0] if presets else None,
        provider_presets=presets,
    )
    registration = _setup_registration_plan(plan.root, plan.name, args)
    preferences = _setup_personal_preferences(
        root=plan.root,
        provider_preset=plan.provider_preset,
        registration=registration,
        args=args,
    )
    registration = _setup_registration_plan(
        plan.root,
        plan.name,
        args,
        make_default=preferences.make_default_workspace,
    )
    _render_interactive_setup_plan(plan, registration, preferences)
    if args.dry_run:
        _apply_setup_plan(
            plan,
            dry_run=True,
            register=not args.no_register,
            make_default=preferences.make_default_workspace,
            preferences=preferences,
        )
        return
    if not args.yes and not _ask_yes_no("应用此设置与个人偏好", default=False):
        print("已取消；没有修改任何文件。")
        return
    _apply_setup_plan(
        plan,
        dry_run=False,
        register=not args.no_register,
        make_default=preferences.make_default_workspace,
        preferences=preferences,
    )


def _setup_quick(args: argparse.Namespace) -> None:
    root = Path(args.path).expanduser().resolve()
    config = load(root)
    print("\n" + title("━━ 快速检查 ━━"))
    print(f"检查现有 Profile：{terminal_value(config.root)}")
    for finding in doctor(config):
        _print_doctor_finding(finding)
    print("下一步：" + terminal_value("dyro next"))


def _non_interactive_setup(args: argparse.Namespace) -> None:
    """Create a usable Profile and, optionally, its first safe development line."""
    root = Path(args.path).expanduser().resolve()
    config_file = root / CONFIG_NAME
    created = False
    if config_file.exists():
        config = load(root)
        print(f"复用已有 Profile：{terminal_value(config_file)}")
        registration = _setup_registration_plan(config.root, config.name, args)
    else:
        repositories = discover_repositories(root)
        if not repositories:
            raise DyroError(
                "未发现 Git 仓库；请先 clone 仓库到工作区，或使用 dyro init --wizard"
            )
        name = args.name or _default_workspace_name(root)
        validate_id(name, "workspace 名称")
        registration = _setup_registration_plan(root, name, args)
        if args.dry_run:
            print(
                f"DRY RUN: 将创建 {config_file}，自动登记 {len(repositories)} 个 Git 仓库"
            )
            _render_setup_registration_plan(registration)
            if not args.no_line:
                print(
                    f"DRY RUN: 将创建开发线 {args.line}（分支 {args.branch or f'feat/{args.line}'}）"
                )
            return
        root.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            config_file, render_config(name, repositories, args.base or "main")
        )
        config = load(root)
        created = True
        print(success(f"已创建 Profile，并自动登记 {len(repositories)} 个 Git 仓库"))
    if config_file.exists() or not args.dry_run:
        _render_setup_registration_plan(registration)
    if not args.dry_run:
        _ensure_state_directories(config.root)
        registered = _register_setup_workspace(
            config,
            register=not args.no_register,
            make_default=args.make_default,
        )
    else:
        registered = None
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
            prefix = "DRY RUN: " if args.dry_run else ""
            print(
                prefix
                + success("已创建开发线 ")
                + terminal_value(line.id)
                + f"（{terminal_value(line.branch)}）"
            )
        else:
            print(f"开发线已存在：{existing.id}（{existing.branch}）")
    findings = doctor(config)
    for finding in findings:
        _print_doctor_finding(finding)
    if any(finding.startswith("FAIL") for finding in findings):
        raise DyroError("setup 已完成基础配置，但 doctor 仍发现结构错误")
    if created or registered is not None:
        _print_setup_completion(config, registered)


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
    print("\n" + title("━━ 已登记仓库 ━━"))
    print(muted(f"Profile：{config.name} · {len(config.repositories)} 个仓库"))
    print(muted(f"{'ID':18} {'ANCHOR':36} {'MOUNT':28} REMOTE"))
    for repository_id, repository in sorted(config.repositories.items()):
        remote = success("configured") if repository.remote else muted("-")
        print(
            f"{terminal_value(f'{repository_id:18}')} "
            f"{repository.path:36} {repository.mount:28} {remote}"
        )


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
    print("\n" + title("━━ 初始化仓库 ━━"))
    print(muted("只处理当前 Profile 中缺失的 anchor 仓库。"))
    for message in bootstrap(config, dry_run=args.dry_run):
        print(message)
    if not args.dry_run:
        print("\n" + title("━━ 初始化后检查 ━━"))
        for finding in doctor(config):
            _print_doctor_finding(finding)


def cmd_doctor(args: argparse.Namespace) -> None:
    config = _config(args)
    budget = _control_plane_budget(args) if args.format == "json" else None
    findings = doctor(config, read_budget=budget)
    failures = [item for item in findings if item.startswith("FAIL")]
    sidecar = discover_sidecar()
    if args.format == "json":
        _print_control_plane_json(
            "doctor",
            workspace=config.name,
            passed=not failures,
            findings=[
                _doctor_finding_payload(item, include_paths=args.include_paths)
                for item in findings
            ],
            sidecars={"local_image_gen": sidecar.as_dict()},
        )
        if failures:
            raise SystemExit(2)
        return
    print("\n" + title("━━ Dyro 健康检查 ━━"))
    print(muted(f"Profile：{config.name} · 检查仓库、基线与隔离工作区。"))
    for finding in findings:
        _print_doctor_finding(finding)
    if sidecar.state == "absent":
        print(ABSENT_INFO_LINE)
    if failures:
        raise DyroError("doctor 发现结构错误")
    print("\n" + success("检查通过。") + " 下一步：" + terminal_value("dyro"))


def cmd_image_doctor(args: argparse.Namespace) -> None:
    if args.dry_run:
        presence = discover_sidecar()
        if args.format == "json":
            _print_control_plane_json("image_doctor", **presence.as_dict())
            return
        print("DRY RUN: 未探测 local-image-gen")
        if presence.state == "absent":
            print(ABSENT_INFO_LINE)
        else:
            print("PATH 上已有 local-image-gen；未查询后端。")
        return
    probe = probe_sidecar()
    if args.format == "json":
        _print_control_plane_json(
            "image_doctor",
            **probe.as_dict(include_paths=args.include_paths),
        )
        if probe.state == "unavailable":
            raise SystemExit(2)
        return
    print("\n" + title("━━ local-image-gen ━━"))
    if probe.state == "absent":
        print(ABSENT_INFO_LINE)
        print("下一步：" + terminal_value("dyro image install"))
        return
    if probe.state == "ready":
        backends = "、".join(probe.usable_providers) or "-"
        print(success("状态：ready") + (f" · {probe.version}" if probe.version else ""))
        print(f"可用后端：{backends}")
        print("下一步：直接运行 " + terminal_value("local-image-gen") + "。Dyro 不代跑出图。")
        return
    if probe.state == "needs_setup":
        print(muted("状态：needs_setup"))
        print(probe.message or "已安装 local-image-gen，但没有可用订阅或 API key。")
        print(f"来源：{SOURCE_URL}")
        print("下一步：按上游文档登录或配置密钥后，再运行 " + terminal_value("dyro image doctor"))
        return
    print(danger(probe.message or "sidecar 不可读"))
    raise SystemExit(2)


def cmd_image_install(args: argparse.Namespace) -> None:
    require_interactive_install(
        yes=args.yes,
        dry_run=args.dry_run,
        tty=sys.stdin.isatty() and sys.stdout.isatty(),
    )
    install_image_sidecar(yes=args.yes, dry_run=args.dry_run)


def cmd_terminology_check(args: argparse.Namespace) -> None:
    root = (
        _config(args).root
        if args.workspace_alias
        else Path(args.root or ".").expanduser().resolve()
    )
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


def cmd_console(args: argparse.Namespace) -> None:
    initial_workspace = getattr(args, "workspace_alias", None)
    root_arg = getattr(args, "root", None)
    target_root: Path | None = None
    if args.dry_run:
        target_root = Path(root_arg).expanduser().absolute() if root_arg else None
        render_console_plan(
            port=args.port,
            no_open=args.no_open,
            initial_workspace=initial_workspace,
            target_root=target_root,
        )
        return
    if root_arg:
        config = load(Path(root_arg).expanduser())
        target_root = config.root
        initial_workspace = config.name
    elif initial_workspace:
        get_workspace(initial_workspace)
    launch_console(
        port=args.port,
        no_open=args.no_open,
        initial_workspace=initial_workspace,
        target_root=target_root,
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
    print(
        success("已登记工作区：")
        + terminal_value(record.name)
        + " → "
        + terminal_value(record.root)
    )


def cmd_workspace_list(args: argparse.Namespace) -> None:
    budget = _control_plane_budget(args) if args.format == "json" else None
    registry = load_registry_bounded(budget) if budget is not None else load_registry()
    rows: list[dict[str, object]] = []
    for record in registry.workspaces:
        try:
            if budget is None:
                load(record.root)
            else:
                load_profile_exact(record.root, budget)
        except (DyroError, OSError, ValidationError):
            available = False
        else:
            available = True
        row: dict[str, object] = {
            "name": record.name,
            "default": record.name == registry.default,
            "available": available,
        }
        if args.include_paths:
            row["root"] = str(record.root)
        rows.append(row)
    if args.format == "json":
        _print_control_plane_json(
            "workspace_list",
            default=registry.default or None,
            workspaces=rows,
        )
        return
    if not registry.workspaces:
        print("还没有登记全局工作区。下一步：dyro workspace add <路径>")
        return
    print("\n" + title("━━ 全局工作区 ━━"))
    print(muted("这里只管理首页入口，不会移动或删除项目文件。"))
    print(muted(f"{'默认':4} {'名称':20} {'状态':8} 路径"))
    for record, row in zip(registry.workspaces, rows, strict=True):
        marker = (
            success(f"{'●':4}")
            if record.name == registry.default
            else muted(f"{'·':4}")
        )
        if not row["available"]:
            state = danger(f"{'不可用':8}")
        else:
            state = success(f"{'可用':8}")
        print(
            f"{marker}{terminal_value(f'{record.name:20}')} {state} "
            f"{terminal_value(record.root)}"
        )


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
        raise DyroError("移除只会删除全局首页入口，不会删除项目文件；确认后请加 --yes")
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
            raise DyroError(
                "join 会创建工作区和 Git worktree；请先使用 --dry-run，再加 --yes 执行"
            )
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
    clean_count = sum(scopes == selected_scopes for scopes in clean_scopes.values())
    print(f"仓库：{clean_count}/{len(config.repositories)} clean")
    print("下一步：dyro")


def cmd_status(args: argparse.Namespace) -> None:
    if args.format == "json":
        budget = _control_plane_budget(args)
        if not args.all:
            _print_control_plane_json(
                "workspace_status",
                **_status_payload(_config(args), read_budget=budget),
            )
            return
        registry = load_registry_bounded(budget)
        workspaces: list[dict[str, object]] = []
        for record in registry.workspaces:
            try:
                config = load_profile_exact(record.root, budget).config
            except (DyroError, OSError, ValidationError) as exc:
                workspaces.append(
                    {
                        "workspace": record.name,
                        "available": False,
                        "error_code": _control_plane_error_code(args, exc),
                        "rows": [],
                    }
                )
            else:
                workspaces.append(
                    {
                        "available": True,
                        **_status_payload(config, read_budget=budget),
                    }
                )
        _print_control_plane_json("workspace_status_all", workspaces=workspaces)
        return
    if args.all:
        print_all_status()
        return
    print_status(_config(args))


def cmd_agent_list(args: argparse.Namespace) -> None:
    config = _config(args)
    print("\n" + title("━━ 已登记 Agent ━━"))
    for adapter_id, adapter in sorted(config.adapters.items()):
        print(
            f"{terminal_value(f'{adapter_id:16}')} "
            f"{muted('launch=')}{shlex.join(adapter.launch)}"
        )


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
    print(
        f"{'DRY RUN: 将添加' if args.dry_run else '已添加'} Agent adapter：{adapter.id}"
    )


def cmd_agent_test(args: argparse.Namespace) -> None:
    checks = test_adapter(_config(args), args.id)
    failures = []
    for mode, available, executable in checks:
        rendered = f"{'PASS' if available else 'FAIL'} {args.id}.{mode}: {executable}"
        print(success(rendered) if available else danger(rendered))
        if not available:
            failures.append(mode)
    if failures:
        raise DyroError(f"Agent adapter 不可用：{args.id}（{', '.join(failures)}）")


def cmd_agent_discover(args: argparse.Namespace) -> None:
    print_agent_discovery(_config(args))


def cmd_capability_list(args: argparse.Namespace) -> None:
    from .capability import card_payload, discover_unintegrated, runtime_cards

    config = _config(args)
    cards = []
    for card in runtime_cards(config).values():
        payload = card_payload(card)
        payload["hook_surface_declared"] = payload.get("hook_surface", "")
        payload["hook_proven"] = False
        cards.append(payload)
    discovered = [
        {"id": item.id, "command": item.command, "state": item.state}
        for item in discover_unintegrated(config)
    ]
    if args.format == "json":
        print(
            json.dumps(
                {"schema_version": 1, "cards": cards, "discovered_unintegrated": discovered},
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
        )
        return
    if not cards and not discovered:
        print("暂无 Capability Card")
        return
    for card in cards:
        print(
            f"{card['id']:16} {card['kind']:10} {card['source']:14} "
            f"isolation={card['attested_isolation']} cannot_prove={','.join(card['cannot_prove'])}"
        )
    for item in discovered:
        print(f"{item['id']:16} discovered  {item['state']}  command={item['command']}")


def cmd_capability_add(args: argparse.Namespace) -> None:
    from .capability import append_capability, card_from_command, card_from_preset

    config = _config(args)
    if args.preset:
        card = card_from_preset(args.id, args.preset)
    else:
        try:
            command = shlex.split(args.command)
        except ValueError as exc:
            raise DyroError(f"Capability command 解析失败：{exc}") from exc
        card = card_from_command(args.id, command)
    append_capability(config, card, dry_run=args.dry_run)
    print(f"{'DRY RUN: 将添加' if args.dry_run else '已添加'} Capability Card：{card.id}")


def cmd_capability_test(args: argparse.Namespace) -> None:
    from .capability import test_capability

    report = test_capability(_config(args), args.id)
    if args.format == "json":
        print(
            json.dumps(
                {
                    "id": report.id,
                    "source": report.source,
                    "executable": report.executable,
                    "logged_in": report.logged_in,
                    "hook_surface": report.hook_surface,
                    "attested_isolation": report.attested_isolation,
                    "cannot_prove": list(report.cannot_prove),
                    "checks": [
                        {"mode": mode, "available": available, "executable": executable}
                        for mode, available, executable in report.checks
                    ],
                },
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
        )
    else:
        print(f"{report.id} source={report.source} executable={report.executable}")
        print(f"isolation={report.attested_isolation} hook_surface={report.hook_surface or '-'}")
        for mode, available, executable in report.checks:
            print(f"{'PASS' if available else 'FAIL'} {report.id}.{mode}: {executable}")
    if not report.executable:
        raise DyroError(f"Capability 不可用：{report.id}")


def _host_projection_payload(item) -> dict[str, object]:
    return {
        "authority_projection": item.authority_projection,
        "hook_installed_on_surface": False,
        "hook_note": "deny hook 写在投影树 SKILL.md 旁，未安装到 hook_surface，不是宿主拦截",
        "hook_relpath": item.hook_relpath,
        "hook_sha256": item.hook_sha256,
        "host": item.host,
        "input_sha256": item.input_sha256,
        "manifest_relpath": item.manifest_relpath,
        "scope": item.scope,
        "skill_relpath": item.skill_relpath,
        "skill_sha256": item.skill_sha256,
    }


def cmd_host_compile(args: argparse.Namespace) -> None:
    from .host import compile_hosts

    projections = compile_hosts(_config(args), user=args.user, dry_run=args.dry_run)
    prefix = "DRY RUN: " if args.dry_run else ""
    if args.format == "json":
        print(
            json.dumps(
                {
                    "dry_run": args.dry_run,
                    "projections": [_host_projection_payload(item) for item in projections],
                    "schema_version": 1,
                    "scope": "user" if args.user else "workspace",
                },
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
        )
        return
    print(f"{prefix}已编译宿主投影 scope={'user' if args.user else 'workspace'}")
    for item in projections:
        print(f"{item.host} {item.authority_projection}")


def cmd_host_status(args: argparse.Namespace) -> None:
    from .host import doctor_payload, inspect_projections, render_doctor_text

    report = inspect_projections(_config(args), user=args.user)
    if args.format == "json":
        print(json.dumps(doctor_payload(report), ensure_ascii=False, sort_keys=True, indent=2))
        return
    print(render_doctor_text(report), end="")


def cmd_host_doctor(args: argparse.Namespace) -> None:
    from .host import doctor_payload, inspect_projections, render_doctor_text

    report = inspect_projections(_config(args), user=args.user)
    if args.format == "json":
        print(json.dumps(doctor_payload(report), ensure_ascii=False, sort_keys=True, indent=2))
    else:
        print(render_doctor_text(report), end="")
    if not report.ok:
        raise DyroError("宿主投影过期或被手改")


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
    print("\n" + title("━━ 编码工具 ━━"))
    print(muted("顺序已综合上次使用、项目推荐与个人偏好。"))
    print(muted(f"{'ID':20} {'状态':12} {'类型':10} 名称"))
    state_styles = {
        ToolState.READY: success,
        ToolState.NEEDS_SETUP: warning,
        ToolState.INSTALLABLE: warning,
        ToolState.UNAVAILABLE: danger,
    }
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
            f"{terminal_value(f'{tool.id:20}')} "
            f"{state_styles[tool.state](f'{labels[tool.state]:12}')} "
            f"{muted(f'{tool.kind:10}')} {terminal_value(tool.label)}{muted(suffix)}"
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
        "已清除工具置顶顺序" if not tool_ids else "工具置顶顺序：" + ", ".join(tool_ids)
    )


def cmd_integration_status(args: argparse.Namespace) -> None:
    status = integration_status(
        args.id,
        read_budget=_control_plane_budget(args) if args.format == "json" else None,
    )
    if args.format == "json":
        avatars: list[dict[str, object]] = []
        for avatar in status.avatars:
            row: dict[str, object] = {
                "host": avatar.host,
                "state": avatar.state,
            }
            if args.include_paths:
                row.update(path=str(avatar.path), detail=avatar.detail)
            avatars.append(row)
        payload: dict[str, object] = {
            "integration": status.integration,
            "state": status.state.value,
            "avatars": avatars,
        }
        if args.include_paths:
            payload.update(target=str(status.target), detail=status.detail)
        _print_control_plane_json("integration_status", **payload)
        return
    print(f"{status.integration}\t{status.state.value}\t{status.target}")
    print(status.detail)
    for avatar in status.avatars:
        print(
            f"avatar\t{avatar.host}\t{avatar.state}\t{avatar.path}\t{avatar.detail}"
        )


def _print_integration_plan(plan, *, dry_run: bool) -> None:
    prefix = "DRY RUN: " if dry_run else ""
    print(f"{prefix}{plan.action} {plan.status.integration}: {plan.status.state.value}")
    for change in plan.changes:
        print(f"  - {change}")


def cmd_integration_install(args: argparse.Namespace) -> None:
    preview = args.dry_run or not args.yes
    plan = install_integration(args.id, yes=args.yes, dry_run=preview)
    _print_integration_plan(plan, dry_run=preview)
    if not args.yes and not args.dry_run:
        print("确认计划后，重新运行并添加 --yes 执行。")


def cmd_integration_sync(args: argparse.Namespace) -> None:
    """Upgrade a managed Skill install; never performs a first-time install."""
    preview = args.dry_run or not args.yes
    plan = sync_managed_skill(
        args.id,
        yes=args.yes,
        dry_run=preview,
        allow_first_install=False,
    )
    if plan is None:
        print("无需同步；Skill 未安装或已是当前版本。")
        return
    _print_integration_plan(plan, dry_run=preview)
    if not args.yes and not args.dry_run:
        print("确认计划后，重新运行并添加 --yes 执行。")


def cmd_integration_uninstall(args: argparse.Namespace) -> None:
    preview = args.dry_run or not args.yes
    plan = uninstall_integration(args.id, yes=args.yes, dry_run=preview)
    _print_integration_plan(plan, dry_run=preview)
    if not args.yes and not args.dry_run:
        print("确认计划后，重新运行并添加 --yes 执行。")


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
        print("运行 dyro update 可确认并完成更新。")


def cmd_update_now(args: argparse.Namespace) -> None:
    result = _explicit_update_check(persist=not args.dry_run)
    _print_update_result(result)
    if result.kind == UpdateKind.NONE:
        return
    yes = bool(getattr(args, "yes", False))
    if (
        not yes
        and not args.dry_run
        and not (sys.stdin.isatty() and sys.stdout.isatty())
    ):
        raise DyroError(
            "非交互环境不会更新 Dyro；请在终端中运行，或审阅计划后显式添加 --yes"
        )
    updated = perform_update(
        result.latest_version,
        yes=yes,
        dry_run=args.dry_run,
    )
    if updated:
        _refresh_skill_via_new_cli()


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
    print(
        f"{'DRY RUN: 将设置' if args.dry_run else '已设置'} {args.key} = {json.dumps(value, ensure_ascii=False)}"
    )


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
        raise DyroError(
            "工作区尚未就绪；先修复 doctor 失败项，或运行 dyro bootstrap --yes"
        )
    alias = getattr(args, "workspace_alias", None) or config.name
    briefing, _ = build_ready_briefing(config, alias=str(alias))
    text = render_briefing_text(briefing) if briefing else ""
    if text:
        print(text)
        print()
    line_id = args.line or _choose("开发线", [line.id for line in list_lines(config)])
    line, workspace = existing_line_workspace(config, line_id, args.kind)
    record = _record_for_root(config.root)
    last_tool = record.last_agent if record else ""
    requested = args.agent or None
    tool = resolve_start_tool(
        config,
        requested=requested,
        workspace=workspace,
        last_tool=last_tool,
    )
    if tool is None:
        ready = ready_home_tools(config, workspace=workspace)
        chosen = _choose("Agent", [item.id for item in ready])
        tool = next(item for item in ready if item.id == chosen)
    launch_start_tool(
        config,
        workspace=workspace,
        tool=tool,
        line=line.id,
        prompt=args.prompt or "",
        dry_run=args.dry_run,
    )


def _bootstrap_destination_safe(config: Config, relative: str) -> bool:
    try:
        validate_bootstrap_destination(config, relative)
    except (DyroError, OSError):
        return False
    return True


def _next_push_fields(config: Config) -> dict[str, object]:
    return push_policy_fields(config.policy)


def _print_push_disclosure(config: Config) -> None:
    note = push_disclosure(config.policy)
    if note:
        print(note)


def cmd_next(args: argparse.Namespace) -> None:
    """Give newcomers one safe, concrete next step without making changes."""

    try:
        config = _config(args)
    except WorkspaceResolutionError as exc:
        if (
            getattr(args, "workspace_alias", None)
            or getattr(args, "root", None)
            or exc.code is not WorkspaceResolutionFailure.WORKSPACE_NOT_FOUND
        ):
            raise
        if args.format == "json":
            _print_control_plane_json(
                "next_step",
                state="workspace_missing",
                summary="尚未发现 Dyro 工作区。",
                commands=[],
                mutation_available=False,
                required_choice="join_existing_or_setup_new",
            )
            return
        print("尚未发现 Dyro 工作区。")
        print("加入团队项目：dyro join <蓝图地址>")
        print("设置一个新项目：dyro setup")
        return
    budget = _control_plane_budget(args) if args.format == "json" else None
    findings = doctor(config, read_budget=budget)
    failures = [finding for finding in findings if finding.startswith("FAIL")]
    if failures:
        absent_bootstrap_ids = {
            repo_id
            for repo_id, repository in config.repositories.items()
            if repository.remote
            and not (config.root / repository.path).exists()
            and not (config.root / repository.path).is_symlink()
            and _bootstrap_destination_safe(config, repository.path)
        }
        expected_bootstrap_failures = {
            f"FAIL repository {repo_id}: missing or not Git: "
            f"{config.root / config.repositories[repo_id].path}"
            for repo_id in absent_bootstrap_ids
        }
        bootstrap_applicable = (
            bool(absent_bootstrap_ids) and set(failures) == expected_bootstrap_failures
        )
        repair_commands = (
            [_briefing_command(args, config, "bootstrap", "--yes")]
            if bootstrap_applicable
            else []
        )
        findings = [
            _doctor_finding_payload(item, include_paths=False) for item in failures
        ]
        if args.format == "json":
            _print_control_plane_json(
                "next_step",
                state="needs_repair",
                summary="工作区还不能开始任务。",
                commands=repair_commands,
                diagnostic_commands=[_briefing_command(args, config, "doctor")],
                mutation_available=bootstrap_applicable,
                findings=findings,
                **_next_push_fields(config),
            )
            return
        print("工作区还不能开始任务：")
        for finding in findings:
            print(f"  {finding['status']} {finding['message']}")
        print(f"修复后运行：{_briefing_command(args, config, 'doctor')}")
        if bootstrap_applicable:
            print(
                "缺失仓库均已配置 remote，可运行："
                + _briefing_command(args, config, "bootstrap", "--yes")
            )
        _print_push_disclosure(config)
        return
    lines = list_lines(config, read_budget=budget)
    if not lines:
        command = _scoped_command(args, config, "line", "create", "dev", "--yes")
        if args.format == "json":
            _print_control_plane_json(
                "next_step",
                state="needs_line",
                summary="Profile 已就绪，但还没有开发线。",
                commands=[command],
                mutation_available=True,
                **_next_push_fields(config),
            )
            return
        print(f"Profile 已就绪，但还没有开发线。下一步：{command}")
        _print_push_disclosure(config)
        return
    if not config.adapters and not installed_launchable_presets():
        if args.format == "json":
            _print_control_plane_json(
                "next_step",
                state="needs_agent",
                summary="工作区已就绪，但尚未发现可启动的编码工具。",
                commands=[],
                mutation_available=False,
                required_inputs=["agent_id", "agent_command"],
                **_next_push_fields(config),
            )
            return
        print(
            "工作区已就绪，但尚未发现可启动的编码工具。"
            "安装本机 Agent 后运行 dyro start，或 "
            + _scoped_command(args, config, "agent", "add", "<id>", "--command", "…")
        )
        _print_push_disclosure(config)
        return
    briefing, diagnostic_commands = _workspace_ready_briefing(
        args, config, budget
    )
    if args.format == "json":
        payload: dict[str, object] = {
            "state": "ready",
            "summary": _ready_next_summary(briefing),
            "commands": [],
            "mutation_available": False,
        }
        if briefing is not None:
            payload["briefing"] = briefing
            payload["diagnostic_commands"] = diagnostic_commands
        payload.update(_next_push_fields(config))
        _print_control_plane_json("next_step", **payload)
        return
    if briefing is None:
        print("工作区已就绪。可用 dyro start 打开本机已安装的编码工具。")
        _print_push_disclosure(config)
        return
    print(render_briefing_text(briefing))
    _print_push_disclosure(config)


def cmd_line_list(args: argparse.Namespace) -> None:
    config = _config(args)
    budget = _control_plane_budget(args) if args.format == "json" else None
    lines = list_lines(config, args.kind, read_budget=budget)
    if args.format == "json":
        _print_control_plane_json(
            "line_list",
            workspace=config.name,
            lines=[
                {
                    "kind": line.kind,
                    "id": line.id,
                    "branch": line.branch,
                    "base": line.base,
                    "repositories": [
                        {
                            "id": repository,
                            "base": line.base_for(repository),
                            "storage": line.storage_for(repository),
                        }
                        for repository in line.repositories
                    ],
                }
                for line in lines
            ],
        )
        return
    if not lines:
        print("暂无已登记开发线")
        return
    print(f"{'KIND':8} {'ID':28} {'BRANCH':30} {'BASE':24} REPOSITORIES")
    for line in lines:
        repositories = ", ".join(
            f"{repo_id}@{line.base_for(repo_id)}[{line.storage_for(repo_id)}]"
            for repo_id in line.repositories
        )
        print(
            f"{line.kind:8} {line.id:28} {line.branch:30} {line.base:24} {repositories}"
        )


def _create_line(args: argparse.Namespace, kind: str) -> None:
    config = _config(args)
    _require_yes(args, "创建开发线")
    branch = args.branch or (
        f"hotfix/{args.id}" if kind == "hotfix" else f"feat/{args.id}"
    )
    if kind == "hotfix" and not args.base:
        raise DyroError(
            "Hotfix 必须显式提供 --base（已核实的 release/tag/deployed SHA）"
        )
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
    bases = ", ".join(
        f"{repo_id}={line.base_for(repo_id)}" for repo_id in line.repositories
    )
    print(
        f"{'DRY RUN: ' if args.dry_run else ''}已创建 {line.kind} {line.id}，分支 {line.branch}，仓库基线：{bases}"
    )


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
    heads = ", ".join(
        f"{repository}={changeset.heads[repository][:12]}"
        for repository in changeset.repositories
    )
    print(
        f"{'DRY RUN: ' if args.dry_run else ''}已创建 Change Set {changeset.id}：{heads}"
    )


def cmd_changeset_list(args: argparse.Namespace) -> None:
    config = _config(args)
    budget = _control_plane_budget(args) if args.format == "json" else None
    changesets = list_changesets(config, read_budget=budget)
    if args.format == "json":
        _print_control_plane_json(
            "changeset_list",
            changesets=[
                {
                    "id": changeset.id,
                    "line": changeset.line,
                    "branch": changeset.branch,
                    "repositories": list(changeset.repositories),
                    "heads": changeset.heads,
                    "created_at": changeset.created_at,
                }
                for changeset in changesets
            ],
        )
        return
    if not changesets:
        print("暂无 Change Set")
        return
    print(f"{'ID':28} {'LINE':24} {'BRANCH':28} REPOSITORIES")
    for changeset in changesets:
        print(
            f"{changeset.id:28} {changeset.line:24} {changeset.branch:28} {', '.join(changeset.repositories)}"
        )


def cmd_changeset_verify(args: argparse.Namespace) -> None:
    config = _config(args)
    budget = _control_plane_budget(args) if args.format == "json" else None
    findings = verify_changeset(
        config,
        get_changeset(config, args.id, read_budget=budget),
        read_budget=budget,
    )
    failures = [finding for finding in findings if finding.startswith("FAIL")]
    if args.format == "json":
        _print_control_plane_json(
            "changeset_verification",
            changeset=args.id,
            passed=not failures,
            findings=[_finding_payload(item) for item in findings],
        )
        if failures:
            raise SystemExit(2)
        return
    for finding in findings:
        print(finding)
    if failures:
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
        atomic_write_text(
            path / "task.toml",
            task_template(args.id, args.title, args.line, args.repository, mount),
        )
        atomic_write_text(
            path / "handoff.md", f"# {args.title}\n\n- 目标：\n- 范围：\n- 验收：\n"
        )
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
        print(
            f"{task.id:30} {task_status(config, task):16} {task.line:20} {task.title}"
        )


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
    requested_output = Path(args.output).expanduser() if args.output else None
    if requested_output is not None and (
        requested_output.exists() or requested_output.is_symlink()
    ):
        raise DyroError(f"拒绝覆盖已有 claim 导出文件：{requested_output}")
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
            raise DyroError(f"无法创建 claim 导出目录：{output.parent}") from exc
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        initial_mode = 0o600 if os.name == "nt" else 0o000
        try:
            descriptor = os.open(output, flags, initial_mode)
        except FileExistsError as exc:
            raise DyroError(f"拒绝覆盖已有 claim 导出文件：{output}") from exc
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
    print(
        f"{task.id} -> {release_task_claim(config, task, runner=args.by, dry_run=args.dry_run)}"
    )


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
        print(
            "运行：dyro task next --run --yes"
            + (" --id <任务ID>" if len(candidates) > 1 else "")
        )
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
                provenance=evidence["provenance"]
                if evidence["provenance"].is_file()
                else None,
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
    if args.allow_unauthenticated and args.host not in {
        "127.0.0.1",
        "::1",
        "localhost",
    }:
        raise ValidationError("--allow-unauthenticated 只能绑定 loopback host")
    if (args.tls_cert is None) != (args.tls_key is None):
        raise ValidationError("Witness TLS cert 与 key 必须同时设置")
    if args.tls_cert is None:
        if not args.allow_http:
            raise ValidationError(
                "Witness 必须设置 TLS，或显式使用仅本地的 --allow-http"
            )
        if args.host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValidationError("--allow-http 只能绑定 loopback host")
    bindings: dict[str, str] = {}
    for value in args.client_workspace_binding:
        key_id, separator, workspace_id = value.partition("=")
        if not separator or not key_id or not workspace_id:
            raise ValidationError(
                "--client-workspace-binding 必须为 KEY_ID=WORKSPACE_ID"
            )
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
    print(
        f"{task.id} -> {import_review_evidence(config, task, review=Path(args.file), dry_run=args.dry_run)}"
    )


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
        json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        + b"\n",
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
        state = (
            "current"
            if record.current
            else "temporary"
            if record.temporary
            else "history"
        )
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
    print(
        f"{'DRY RUN: ' if args.dry_run else ''}已合并 {task.id}"
        + (" 并推送" if args.push else "")
    )


def _proofs_from_args(args: argparse.Namespace, *, proof_id: str | None = None):
    from .proof import list_proofs

    config = _config(args)
    proofs = list_proofs(
        config,
        task_id=getattr(args, "task", None),
        objective_id=getattr(args, "objective", None),
        line_id=getattr(args, "line", None),
    )
    if proof_id:
        matched = tuple(proof for proof in proofs if proof.id == proof_id)
        if not matched:
            raise DyroError(f"Proof 不存在：{proof_id}")
        return matched
    return proofs


def _print_proofs(args: argparse.Namespace, proofs, *, mode: str = "") -> None:
    from .proof import render_proofs_json, render_proofs_text

    if getattr(args, "format", "text") == "json":
        print(render_proofs_json(proofs, mode=mode or "rebind"))
        return
    print(render_proofs_text(proofs, mode=mode), end="")


def cmd_proof_list(args: argparse.Namespace) -> None:
    _print_proofs(args, _proofs_from_args(args))


def cmd_proof_show(args: argparse.Namespace) -> None:
    _print_proofs(args, _proofs_from_args(args, proof_id=args.proof_id))


def cmd_proof_verify(args: argparse.Namespace) -> None:
    from .proof import verify_exit_code

    if getattr(args, "rerun_procedure", False):
        raise DyroError(
            "--rerun-procedure 必须在隔离 runner 中重放；0.7 未提供隔离重跑，"
            "未 replay 不得声称 procedure_reproduced"
        )
    proofs = _proofs_from_args(args, proof_id=getattr(args, "proof_id", None))
    _print_proofs(args, proofs, mode="rebind")
    code = verify_exit_code(proofs)
    if code:
        raise SystemExit(code)


def cmd_proof_export(args: argparse.Namespace) -> None:
    from pathlib import Path

    from .proof import export_bundle

    if bool(args.proof_id) == bool(args.task):
        raise ValidationError("proof export 的位置参数 proof-id 与 --task 互斥，且必须提供其一")
    proofs = _proofs_from_args(args, proof_id=args.proof_id)
    if not proofs:
        raise DyroError("没有可导出的 Proof")
    path = export_bundle(proofs, Path(args.bundle))
    print(f"已导出 {len(proofs)} 条 Proof 到 {path}")


def cmd_proof_verify_bundle(args: argparse.Namespace) -> None:
    from pathlib import Path

    from .proof import load_current_heads, verify_bundle, verify_exit_code
    from .proof.bundle import INTEGRITY_MODE

    if not args.bundle:
        raise DyroError("verify-bundle 需要 Proof Bundle 路径")
    heads = load_current_heads(Path(args.current_heads)) if args.current_heads else None
    git_dirs = tuple(Path(item) for item in (args.git_dir or ()))
    proofs = verify_bundle(Path(args.bundle), git_dirs=git_dirs, current_heads=heads)
    _print_proofs(args, proofs, mode=INTEGRITY_MODE)
    code = verify_exit_code(proofs)
    if code:
        raise SystemExit(code)


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
        print(
            f"{agent:18} {counters['executor']:>5} {counters['executor_ok']:>8} {counters['review']:>7} {counters['review_ok']:>10}"
        )


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
    budget = _control_plane_budget(args) if args.format == "json" else None
    records = list_objectives(config, recover=False, read_budget=budget)
    if args.format == "json":
        _print_control_plane_json(
            "objective_list",
            workspace=config.name,
            objectives=[
                _objective_payload(
                    config, record, detailed=False, read_budget=budget
                )
                for record in records
            ],
        )
        return
    if not records:
        print(
            "暂无 Objective。下一步：dyro objective start --file <objective.toml> --yes"
        )
        return
    print(f"{'OBJECTIVE':28} {'STATE':8} {'RESULT':16} {'REV':4} {'LINE':20} TARGETS")
    for record in records:
        _print_objective(config, record)


def cmd_objective_status(args: argparse.Namespace) -> None:
    config = _config(args)
    budget = _control_plane_budget(args) if args.format == "json" else None
    record = get_objective(
        config, args.id, recover=False, read_budget=budget
    )
    if args.format == "json":
        _print_control_plane_json(
            "objective_status",
            workspace=config.name,
            objective=_objective_payload(
                config, record, detailed=True, read_budget=budget
            ),
        )
        return
    print(f"Objective: {record.objective.id}")
    print(f"Operator state: {record.operator_state}")
    print(f"Derived result: {derive_objective_result(config, record)}")
    print(f"Revision: {record.revision}")
    print(f"Line: {record.objective.line}")
    print(f"Targets: {', '.join(record.objective.targets)}")
    print(f"Scope: {', '.join(record.scope)}")
    print(f"Contract SHA-256: {record.contract_sha256}")


def _read_objective_plan(
    config: Config,
    objective_id: str,
    *,
    read_budget: ReadBudget | None = None,
):
    """Build an Objective plan without recovery, mutation, dispatch, or agents."""
    record = get_objective(
        config, objective_id, recover=False, read_budget=read_budget
    )
    snapshot = (
        build_scheduler_snapshot(config, objective=record)
        if read_budget is None
        else build_scheduler_snapshot_bounded(
            config, objective=record, budget=read_budget
        )
    )
    return record, snapshot, build_continuation_plan(snapshot)


def cmd_objective_plan(args: argparse.Namespace) -> None:
    config = _config(args)
    record, _, plan = _read_objective_plan(
        config,
        args.id,
        read_budget=_control_plane_budget(args) if args.format == "json" else None,
    )
    preview = preview_objective_wave_budgets(
        config,
        objective=record.objective,
        actions=plan.selected_actions,
        now=datetime.now(timezone.utc),
    )
    if args.format == "json":
        payload = continuation_plan_payload(plan)
        payload["budget_preview"] = preview
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
        return
    print(render_plan_text(plan))
    for note in render_budget_preview_text(preview):
        print(note)


def cmd_objective_explain(args: argparse.Namespace) -> None:
    config = _config(args)
    record, _, plan = _read_objective_plan(
        config,
        args.id,
        read_budget=_control_plane_budget(args) if args.format == "json" else None,
    )
    briefing = briefing_payload(
        plan,
        command=_briefing_command(args, config, *follow_up_argv(plan)),
        title=record.objective.title,
    )
    if args.format == "json":
        payload = continuation_plan_payload(plan)
        payload["briefing"] = briefing
        print(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
        )
        return
    print(render_briefing_text(briefing))
    print()
    print(render_plan_text(plan))


def cmd_objective_graph(args: argparse.Namespace) -> None:
    _, snapshot, plan = _read_objective_plan(
        _config(args),
        args.id,
        read_budget=_control_plane_budget(args) if args.format == "json" else None,
    )
    projection = build_scheduler_projection(snapshot, plan)
    if args.format == "json":
        print(render_projection_json(projection))
        return
    print(render_projection_mermaid(projection))


def cmd_objective_tick(args: argparse.Namespace) -> None:
    """Preview the next bounded Objective mutation wave without applying it."""
    config = _config(args)
    record = get_objective(
        config,
        args.id,
        recover=False,
        read_budget=_control_plane_budget(args) if args.format == "json" else None,
    )
    snapshot = (
        build_scheduler_snapshot(config, objective=record)
        if args.format != "json"
        else build_scheduler_snapshot_bounded(
            config, objective=record, budget=_control_plane_budget(args)
        )
    )
    plan = build_continuation_plan(snapshot)
    from .peer_wave import (
        annotate_objective_tick,
        discover_available_write_providers,
        recommended_max_parallel,
    )

    available_write = discover_available_write_providers()
    tick = build_scheduler_tick(
        snapshot,
        plan,
        max_parallel=recommended_max_parallel(
            record.objective.budget.max_parallel, len(available_write)
        ),
    )
    overlay = annotate_objective_tick(
        snapshot,
        plan,
        tick,
        available_write,
        capabilities=getattr(config, "capabilities", None),
    )
    overlay["budget_preview"] = preview_objective_wave_budgets(
        config,
        objective=record.objective,
        actions=tick.wave,
        now=datetime.now(timezone.utc),
    )
    if args.format == "json":
        payload = scheduler_tick_payload(tick)
        payload.update(overlay)
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
        return
    print(
        render_briefing_text(
            {
                "lines": arrival_lines(
                    plan, record.objective.title, TICK_CLOSER
                )
            }
        )
    )
    print()
    print("\n".join(render_human_wave(tick.wave)))
    print()
    print(render_scheduler_tick_text(tick))
    for note in overlay.get("peer_wave", {}).get("warnings", []):
        print(f"Warning: {note}")
    for binding in overlay.get("peer_wave", {}).get("executor_bindings", []):
        print(
            f"Harness: {binding['task_id']} -> {binding['executor']} "
            f"({binding['source']})"
        )
    for note in render_budget_preview_text(overlay["budget_preview"]):
        print(note)


def cmd_objective_attention(args: argparse.Namespace) -> None:
    """Render the safe, deterministic attention view without mutating state."""
    config = _config(args)
    record = get_objective(
        config,
        args.id,
        recover=False,
        read_budget=_control_plane_budget(args) if args.format == "json" else None,
    )
    # Attention must rebind merge proofs.  Bounded JSON snapshots stay
    # Git-free for plan/tick/graph; this command is the decay surface.
    snapshot = build_scheduler_snapshot(config, objective=record)
    plan = build_continuation_plan(snapshot)
    scheduler = build_scheduler_projection(snapshot, plan)
    projection = build_attention_projection(
        snapshot,
        plan,
        scheduler,
        budget=record.objective.budget,
    )
    if args.format == "json":
        print(render_attention_json(projection))
        return
    print(
        render_briefing_text(
            {
                "lines": arrival_lines(
                    plan, record.objective.title, ATTENTION_CLOSER
                )
            }
        )
    )
    print()
    print(
        "\n".join(
            render_human_attention(
                tuple((item.reason, item.subject_id) for item in projection.items)
            )
        )
    )
    print()
    print(render_attention_text(projection))


def cmd_objective_apply(args: argparse.Namespace) -> None:
    """Display an exact supervised wave, then apply it only after confirmation."""
    config = _config(args)
    wave = build_supervised_wave(config, args.id)
    if args.format == "text":
        print(render_supervised_wave_text(wave))
    if args.dry_run:
        if args.format == "json":
            print(
                json.dumps(
                    {"wave": supervised_wave_payload(wave), "dry_run": True},
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                )
            )
        else:
            print(
                "DRY RUN: 未创建 owner lease、Action intent、Action-start 或 Task 执行。"
            )
        return
    if args.yes and args.confirm_sha != wave.confirmation_sha256:
        raise DyroError(
            "--yes 必须同时提供当前 Confirmation SHA-256；请先运行 objective apply --dry-run 后复制摘要"
        )
    if not args.yes:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            raise DyroError(
                "非交互执行必须提供 --yes 与 --confirm-sha；先使用 --dry-run 查看精确 wave"
            )
        if not _ask_yes_no("确认按上述顺序执行该受监督 Action wave", default=False):
            print("已取消；未写入 Objective 或 Task 状态。")
            return
    outcomes = apply_supervised_wave(config, wave)
    if args.format == "json":
        print(
            json.dumps(
                {
                    "wave": supervised_wave_payload(wave),
                    "dry_run": False,
                    "outcomes": supervised_outcomes_payload(outcomes),
                },
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
        )
    elif outcomes:
        print(render_supervised_outcomes(outcomes))


def _trigger_cli_time(value: str | None, label: str) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DyroError(f"{label} 必须是带时区的 ISO-8601 时间") from exc
    if parsed.tzinfo is None:
        raise DyroError(f"{label} 必须是带时区的 ISO-8601 时间")
    return parsed.astimezone(timezone.utc)


def _trigger_cli_facts(
    values: list[str] | None, label: str
) -> tuple[tuple[str, str], ...]:
    facts: list[tuple[str, str]] = []
    seen: set[str] = set()
    for value in values or []:
        name, separator, state = value.partition("=")
        name, state = name.strip(), state.strip()
        if not separator or not name or not state:
            raise DyroError(f"{label} 必须使用 KEY=VALUE；可重复指定")
        if name in seen:
            raise DyroError(f"{label} 不能重复指定同一个 KEY：{name}")
        seen.add(name)
        facts.append((name, state))
    return tuple(facts)


def _trigger_payload(observation, *, delivery: str | None = None) -> dict[str, object]:
    payload: dict[str, object] = {
        "trigger_id": observation.trigger_id,
        "state": observation.state.value,
        "summary": observation.summary,
        "evidence_ref": observation.evidence_ref,
        "observed_at": observation.observed_at.isoformat()
        if observation.observed_at
        else None,
        "next_probe_at": observation.next_probe_at.isoformat()
        if observation.next_probe_at
        else None,
    }
    if delivery is not None:
        payload["delivery"] = delivery
    return payload


def _print_trigger_observation(
    args: argparse.Namespace, observation, *, delivery: str | None = None
) -> None:
    payload = _trigger_payload(observation, delivery=delivery)
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return
    print(
        f"{payload['trigger_id']}: {payload['state']} ({payload['summary']}) "
        f"@ {payload['observed_at']}"
    )
    if delivery:
        print("只读观测已输出；尚未写入任务、证据或执行台账。")


def cmd_trigger_list(args: argparse.Namespace) -> None:
    rows = [
        {
            "kind": kind.value,
            "execution": "bounded_adapter_required"
            if kind is TriggerKind.PROVIDER
            else "builtin_read_only",
        }
        for kind in TriggerKind
    ]
    if args.format == "json":
        print(json.dumps(rows, ensure_ascii=False, sort_keys=True))
        return
    for row in rows:
        print(f"{row['kind']:16} {row['execution']}")


def cmd_trigger_probe(args: argparse.Namespace) -> None:
    kind = TriggerKind(args.kind)
    if kind is TriggerKind.PROVIDER:
        raise DyroError(
            "provider Trigger 只能由已登记的有界 adapter 调用；CLI 不接受命令、URL 或脚本"
        )
    now = _trigger_cli_time(args.at, "--at") or datetime.now(timezone.utc)
    not_before = _trigger_cli_time(args.not_before, "--not-before")
    if args.signal and kind is not TriggerKind.MANUAL_SIGNAL:
        raise DyroError("--signal 只能与 manual_signal Trigger 一起使用")
    observation = probe_builtin(
        TriggerProbeInput(
            config=TriggerConfig(args.id, kind, not_before=not_before),
            now=now,
            current_facts=_trigger_cli_facts(args.current, "--current"),
            previous_facts=_trigger_cli_facts(args.previous, "--previous"),
            manual_signal=args.signal or "",
        )
    )
    _print_trigger_observation(args, observation, delivery="ephemeral")


def cmd_trigger_signal(args: argparse.Namespace) -> None:
    now = _trigger_cli_time(args.at, "--at") or datetime.now(timezone.utc)
    observation = probe_builtin(
        TriggerProbeInput(
            config=TriggerConfig(args.id, TriggerKind.MANUAL_SIGNAL),
            now=now,
            manual_signal=args.signal,
        )
    )
    _print_trigger_observation(args, observation, delivery="ephemeral")


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
    print(
        f"{'DRY RUN: ' if args.dry_run else ''}{record.objective.id} -> {record.operator_state} r{record.revision}"
    )


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
    print(
        f"{'DRY RUN: ' if args.dry_run else ''}{record.objective.id} r{record.revision} targets={', '.join(record.objective.targets)}"
    )


def cmd_objective_scope_remove(args: argparse.Namespace) -> None:
    config = _config(args)
    _require_objective_yes(args, "缩减 Objective scope")
    record = remove_objective_target(config, args.id, args.task, dry_run=args.dry_run)
    print(
        f"{'DRY RUN: ' if args.dry_run else ''}{record.objective.id} r{record.revision} targets={', '.join(record.objective.targets)}"
    )


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
        from .peer_wave import apply_harness_bindings, discover_available_write_providers
        from .tasks import ScheduleWave

        available_write = discover_available_write_providers()
        bound, decision = apply_harness_bindings(
            ScheduleWave(tasks=tuple(queued), deferred=()),
            available_write,
            capabilities=getattr(config, "capabilities", None),
        )
        for note in decision.warnings:
            print(f"warning: {note}")
        for item in decision.deferred:
            print(f"defer {item.task.id}: {item.reason}")
        if bound:
            overrides = {item.task_id: item.executor for item in decision.bindings}
            with ThreadPoolExecutor(
                max_workers=max(1, args.parallel), thread_name_prefix="dyro-dispatch"
            ) as pool:
                futures = {
                    pool.submit(
                        run_task,
                        config,
                        task,
                        dry_run=args.dry_run,
                        legacy_scheduler=True,
                        executor_override=overrides.get(task.id),
                    ): task
                    for task in bound
                }
                for future in as_completed(futures):
                    task = futures[future]
                    try:
                        print(f"dispatch {task.id} -> {future.result()}")
                    except DyroError as exc:
                        print(f"skip {task.id}: {exc}")
        review_queue = list(plan_tasks(config).review)
        if review_queue:
            with ThreadPoolExecutor(
                max_workers=max(1, args.parallel), thread_name_prefix="dyro-review"
            ) as pool:
                futures = {
                    pool.submit(review_task, config, task, dry_run=args.dry_run): task
                    for task in review_queue
                }
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
    location.add_argument(
        "--root", help="工作区根目录；默认从当前目录向上查找 dyro.toml"
    )
    location.add_argument(
        "--workspace",
        dest="workspace_alias",
        help="全局登记的工作区别名；可从任意目录使用",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅输出计划，不写文件、不调用 Agent 或 Git 写操作",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dyro",
        description="DyroEngineeringFlow：本地优先的多仓工程自动化与交付控制平台",
    )
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
    init_mode.add_argument(
        "--wizard", action="store_true", help="交互式登记真实仓库与可选 remote"
    )
    init_mode.add_argument(
        "--discover",
        action="store_true",
        help="自动发现当前目录下的 Git 仓库并登记 origin",
    )
    init.set_defaults(func=cmd_init)

    setup = sub.add_parser(
        "setup", help="首次引导：预览并安全创建 Profile、仓库与首条开发线"
    )
    setup.add_argument("path", nargs="?", default=".")
    setup.add_argument("--name", help="新 Profile 的工作区名称；默认由目录名推断")
    setup.add_argument("--base", help="首条开发线与新 Profile 的默认基线；默认 main")
    setup.add_argument("--line", default="dev", help="首条功能开发线 ID；默认 dev")
    setup.add_argument("--branch", help="首条开发线分支；默认 feat/<line>")
    setup.add_argument(
        "--no-line",
        action="store_true",
        help="仅建立 Profile，不创建 Git worktree 开发线",
    )
    setup.add_argument(
        "--yes",
        action="store_true",
        help="确认需要确认的 setup 计划步骤，包括首条 Git worktree 与全局入口登记",
    )
    setup_registration = setup.add_mutually_exclusive_group()
    setup_registration.add_argument(
        "--no-register",
        action="store_true",
        help="不加入 Console 的全局工作区列表；适合 CI 与临时环境",
    )
    setup_registration.add_argument(
        "--default",
        dest="make_default",
        action="store_true",
        help="登记后设为裸 dyro 的默认项目",
    )
    setup_mode = setup.add_mutually_exclusive_group()
    setup_mode.add_argument(
        "--interactive", action="store_true", help="强制运行交互式首次设置"
    )
    setup_mode.add_argument(
        "--non-interactive", action="store_true", help="禁用交互提示；适合脚本与 CI"
    )
    setup.add_argument(
        "--quick", action="store_true", help="只检查现有 Profile 并给出下一步"
    )
    setup.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=argparse.SUPPRESS,
        help="仅预览设置计划；也兼容全局 --dry-run 放在命令前",
    )
    setup.set_defaults(func=cmd_setup)

    sub.add_parser("home", help="打开当前或默认项目首页").set_defaults(func=cmd_home)
    console = sub.add_parser("console", help="启动只读本地项目控制台")
    console.add_argument(
        "--no-open", action="store_true", help="不自动打开浏览器，打印一次性本地 URL"
    )
    console.add_argument(
        "--port", type=int, default=0, help="loopback 端口；默认 0 由系统分配"
    )
    console.set_defaults(func=cmd_console)
    workspace = sub.add_parser("workspace", help="管理可从任意目录进入的全局工作区")
    workspace_sub = workspace.add_subparsers(dest="workspace_command", required=True)
    workspace_add = workspace_sub.add_parser("add", help="登记已有 Dyro 工作区")
    workspace_add.add_argument("path", nargs="?", default=".")
    workspace_add.add_argument(
        "--name", help="便于记忆的工作区别名；默认读取 Profile 名称"
    )
    workspace_add.add_argument(
        "--default", action="store_true", help="设为裸 dyro 的默认项目"
    )
    workspace_add.set_defaults(func=cmd_workspace_add)
    workspace_list = workspace_sub.add_parser(
        "list", help="显示已登记工作区及可用状态"
    )
    workspace_list.add_argument(
        "--format", choices=("text", "json"), default="text"
    )
    workspace_list.add_argument(
        "--include-paths",
        action="store_true",
        help="在 JSON 中显式包含本机工作区绝对路径",
    )
    workspace_list.set_defaults(func=cmd_workspace_list)
    workspace_default = workspace_sub.add_parser(
        "default", help="设置裸 dyro 的默认项目"
    )
    workspace_default.add_argument("name")
    workspace_default.set_defaults(func=cmd_workspace_default)
    workspace_remove = workspace_sub.add_parser(
        "remove", help="移除全局入口，不删除项目文件"
    )
    workspace_remove.add_argument("name")
    workspace_remove.add_argument("--yes", action="store_true")
    workspace_remove.set_defaults(func=cmd_workspace_remove)

    blueprint = sub.add_parser("blueprint", help="验证可复用的团队工作区蓝图")
    blueprint_sub = blueprint.add_subparsers(dest="blueprint_command", required=True)
    blueprint_validate = blueprint_sub.add_parser(
        "validate", help="只读验证蓝图结构与固定基线"
    )
    blueprint_validate.add_argument(
        "source", help="本地 TOML/目录、HTTPS 文件或 Git 仓库"
    )
    blueprint_validate.add_argument(
        "--ref", dest="blueprint_ref", help="Git 蓝图仓库的分支或 tag"
    )
    blueprint_validate.add_argument(
        "--file",
        dest="blueprint_file",
        default=BLUEPRINT_FILENAME,
        help=f"Git 蓝图仓库内的文件；默认 {BLUEPRINT_FILENAME}",
    )
    blueprint_validate.set_defaults(func=cmd_blueprint_validate)

    join = sub.add_parser("join", help="从团队蓝图安全创建独立的多仓工作区")
    join.add_argument("source", help="本地 TOML/目录、HTTPS 文件或 Git 仓库")
    join.add_argument(
        "--path", help="目标目录；默认 ~/DyroProjects/<suggested_directory>"
    )
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
    registration.add_argument(
        "--no-register", action="store_true", help="不登记到裸 dyro 的全局首页"
    )
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

    doctor_parser = sub.add_parser("doctor", help="验证动态工作区结构")
    doctor_parser.add_argument(
        "--format", choices=("text", "json"), default="text"
    )
    doctor_parser.add_argument(
        "--include-paths",
        action="store_true",
        help="在 JSON 中显式包含本机诊断路径",
    )
    doctor_parser.set_defaults(func=cmd_doctor)
    image = sub.add_parser(
        "image",
        help="发现并引导安装可选的 local-image-gen sidecar；不代跑计费出图",
    )
    image_sub = image.add_subparsers(dest="image_command", required=True)
    image_doctor = image_sub.add_parser(
        "doctor",
        help="探测 local-image-gen 是否在 PATH，以及是否有可用后端",
    )
    image_doctor.add_argument(
        "--format", choices=("text", "json"), default="text"
    )
    image_doctor.add_argument(
        "--include-paths",
        action="store_true",
        help="在 JSON 中显式包含本机产出目录与工作区路径",
    )
    image_doctor.set_defaults(func=cmd_image_doctor)
    image_install = image_sub.add_parser(
        "install",
        help="展示官方安装来源；不会执行远程安装脚本",
    )
    image_install.add_argument(
        "--yes", action="store_true", help="确认后打开官方仓库页面"
    )
    image_install.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=argparse.SUPPRESS,
        help="仅展示安装来源，不打开浏览器；也兼容全局 --dry-run",
    )
    image_install.set_defaults(func=cmd_image_install)
    terminology = sub.add_parser("terminology", help="使用仓库外策略扫描候选术语")
    terminology_sub = terminology.add_subparsers(
        dest="terminology_command", required=True
    )
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
    status_parser.add_argument(
        "--all", action="store_true", help="汇总所有全局登记工作区"
    )
    status_parser.add_argument(
        "--format", choices=("text", "json"), default="text"
    )
    status_parser.set_defaults(func=cmd_status)
    bootstrap_parser = sub.add_parser(
        "bootstrap", help="clone 配置了 remote 的缺失仓库 anchor"
    )
    bootstrap_parser.add_argument("--yes", action="store_true")
    bootstrap_parser.set_defaults(func=cmd_bootstrap)
    repo = sub.add_parser("repo", help="免手改 TOML 的仓库配置管理")
    repo_sub = repo.add_subparsers(dest="repo_command", required=True)
    repo_sub.add_parser("list", help="显示已登记仓库").set_defaults(func=cmd_repo_list)
    repo_add = repo_sub.add_parser("add", help="登记一个本地 Git 仓库；自动读取 origin")
    repo_add.add_argument("path", help="工作区内的仓库路径")
    repo_add.add_argument("--id", help="仓库标识；默认使用目录名")
    repo_add.add_argument("--mount", help="开发线内挂载路径；默认智能推断")
    repo_add.add_argument(
        "--remote", help="缺失路径的 clone remote，或覆盖自动发现的 origin"
    )
    repo_add.set_defaults(func=cmd_repo_add)
    agent = sub.add_parser("agent", help="Agent adapters")
    agent_sub = agent.add_subparsers(dest="agent_command", required=True)
    agent_sub.add_parser("list", help="显示已登记的 Agent adapter").set_defaults(
        func=cmd_agent_list
    )
    agent_sub.add_parser(
        "discover", help="检测本机 Agent，并区分已配置与尚未集成"
    ).set_defaults(func=cmd_agent_discover)
    agent_add = agent_sub.add_parser(
        "add",
        help="写入 [adapters.*]；运行时升级为 Card，不写 [[capabilities]]",
    )
    agent_add.add_argument("id")
    agent_source = agent_add.add_mutually_exclusive_group(required=True)
    agent_source.add_argument("--preset", choices=launchable_preset_ids())
    agent_source.add_argument(
        "--command", help="作为 launch/read/write 的 argv 命令行；不会经 shell 执行"
    )
    agent_add.set_defaults(func=cmd_agent_add)
    capability = sub.add_parser(
        "capability",
        help="审计后的 Capability Card；PATH 发现不是 Card；无 Card 的 dispatch 就绪是第二扇门；有 Card 无 execute 一律拒绝",
    )
    capability_sub = capability.add_subparsers(dest="capability_command", required=True)
    capability_list = capability_sub.add_parser("list", help="列出已审计 Card 与 discovered_unintegrated")
    capability_list.add_argument("--format", choices=("text", "json"), default="text")
    capability_list.set_defaults(func=cmd_capability_list)
    capability_add = capability_sub.add_parser("add", help="写入 [[capabilities]]，不写 PATH 发现")
    capability_add.add_argument("id")
    capability_source = capability_add.add_mutually_exclusive_group(required=True)
    capability_source.add_argument("--preset", choices=("codex", "noop"))
    capability_source.add_argument("--command", help="作为 launch/read/write 的 argv；不会经 shell 执行")
    capability_add.set_defaults(func=cmd_capability_add)
    capability_test = capability_sub.add_parser("test", help="探测可执行/登录，不启动交付")
    capability_test.add_argument("id")
    capability_test.add_argument("--format", choices=("text", "json"), default="text")
    capability_test.set_defaults(func=cmd_capability_test)

    host = sub.add_parser(
        "host",
        help="编译并核验宿主投影（skill；deny hook 不是沙箱，只挡受监督 apply）",
    )
    host_sub = host.add_subparsers(dest="host_command", required=True)
    host_compile = host_sub.add_parser(
        "compile",
        help="把定律与已审计 Card 编译为工作区 skill；deny hook 不是沙箱。--user 才写用户级",
    )
    host_compile.add_argument(
        "--user",
        action="store_true",
        help="写入用户级 host-projections；默认只写当前工作区",
    )
    host_compile.add_argument("--dry-run", action="store_true")
    host_compile.add_argument("--format", choices=("text", "json"), default="text")
    host_compile.set_defaults(func=cmd_host_compile)
    host_status = host_sub.add_parser("status", help="查看已编译投影是否仍与当前 Card 一致")
    host_status.add_argument(
        "--user",
        action="store_true",
        help="核验用户级投影",
    )
    host_status.add_argument("--format", choices=("text", "json"), default="text")
    host_status.set_defaults(func=cmd_host_status)
    host_doctor = host_sub.add_parser(
        "doctor",
        help="重算投影哈希；手改或过期则失败。只挡受监督 apply，不管 task run / merge。deny hook 不是隔离边界",
    )
    host_doctor.add_argument(
        "--user",
        action="store_true",
        help="核验用户级投影",
    )
    host_doctor.add_argument("--format", choices=("text", "json"), default="text")
    host_doctor.set_defaults(func=cmd_host_doctor)

    agent_test = agent_sub.add_parser(
        "test", help="仅检查 adapter 可执行文件是否可用，不启动 Agent"
    )
    agent_test.add_argument("id")
    agent_test.set_defaults(func=cmd_agent_test)
    tool = sub.add_parser("tool", help="发现、排序和安全安装本地编码工具")
    tool_sub = tool.add_subparsers(dest="tool_command", required=True)
    tool_sub.add_parser("list", help="按首页实际顺序显示工具状态").set_defaults(
        func=cmd_tool_list
    )
    tool_install = tool_sub.add_parser("install", help="显示并执行内置的官方安装方案")
    tool_install.add_argument("id")
    tool_install.add_argument(
        "--yes", action="store_true", help="确认执行已展示的安装命令或打开官方页面"
    )
    tool_install.set_defaults(func=cmd_tool_install)
    tool_default = tool_sub.add_parser("default", help="设置个人默认工具")
    tool_default.add_argument("id", nargs="?")
    tool_default.add_argument("--clear", action="store_true")
    tool_default.set_defaults(func=cmd_tool_default)
    tool_pin = tool_sub.add_parser("pin", help="设置个人工具置顶顺序")
    tool_pin.add_argument("ids", nargs="*")
    tool_pin.add_argument("--clear", action="store_true")
    tool_pin.set_defaults(func=cmd_tool_pin)
    integration = sub.add_parser(
        "integration", help="管理 Dyro 拥有的可选编码智能体集成"
    )
    integration_sub = integration.add_subparsers(
        dest="integration_command", required=True
    )
    integration_status_parser = integration_sub.add_parser(
        "status", help="只读检查集成状态"
    )
    integration_status_parser.add_argument("id", choices=INTEGRATION_CHOICES)
    integration_status_parser.add_argument(
        "--format", choices=("text", "json"), default="text"
    )
    integration_status_parser.add_argument(
        "--include-paths",
        action="store_true",
        help="在 JSON 中显式包含本机集成路径与路径相关细节",
    )
    integration_status_parser.set_defaults(func=cmd_integration_status)
    integration_install_parser = integration_sub.add_parser(
        "install", help="预览或安装 Dyro 自有集成资产（镜像+分身）"
    )
    integration_install_parser.add_argument(
        "id",
        choices=INTEGRATION_CHOICES,
        help="skill 为控制面；executor 为执行座位；board 为评审板；dispatch 为派发；codex 为 skill 别名",
    )
    integration_install_parser.add_argument(
        "--yes", action="store_true", help="确认执行已预览的安装或升级"
    )
    integration_install_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=argparse.SUPPRESS,
        help="仅预览安装计划；也兼容全局 --dry-run 放在命令前",
    )
    integration_install_parser.set_defaults(func=cmd_integration_install)
    integration_sync_parser = integration_sub.add_parser(
        "sync",
        help="仅升级已托管的 Skill（不会首次安装）",
    )
    integration_sync_parser.add_argument(
        "id",
        choices=INTEGRATION_CHOICES,
        help="skill 为控制面；executor 为执行座位；board 为评审板；dispatch 为派发；codex 为 skill 别名",
    )
    integration_sync_parser.add_argument(
        "--yes", action="store_true", help="确认执行已预览的同步或升级"
    )
    integration_sync_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=argparse.SUPPRESS,
        help="仅预览同步计划；也兼容全局 --dry-run 放在命令前",
    )
    integration_sync_parser.set_defaults(func=cmd_integration_sync)
    integration_uninstall_parser = integration_sub.add_parser(
        "uninstall", help="仅卸载仍匹配 ownership manifest 的资产"
    )
    integration_uninstall_parser.add_argument("id", choices=INTEGRATION_CHOICES)
    integration_uninstall_parser.add_argument(
        "--yes", action="store_true", help="确认卸载仍完整的自有资产"
    )
    integration_uninstall_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=argparse.SUPPRESS,
        help="仅预览卸载计划；也兼容全局 --dry-run 放在命令前",
    )
    integration_uninstall_parser.set_defaults(func=cmd_integration_uninstall)
    update = sub.add_parser(
        "update",
        help="检测并安全更新 Dyro（无子命令时确认后安装）",
    )
    update.add_argument(
        "--yes", action="store_true", help="确认执行已展示的更新命令"
    )
    update_sub = update.add_subparsers(dest="update_command", required=False)
    update.set_defaults(func=cmd_update_now)
    update_sub.add_parser(
        "check", help="仅检查官方 PyPI 的最新稳定版本，不安装"
    ).set_defaults(func=cmd_update_check)
    update_now = update_sub.add_parser(
        "now", help="显示计划并更新到最新稳定版本（等价于 dyro update）"
    )
    update_now.add_argument(
        "--yes", action="store_true", help="确认执行已展示的更新命令"
    )
    update_now.set_defaults(func=cmd_update_now)
    update_auto = update_sub.add_parser("auto", help="管理补丁版本自动更新")
    update_auto.add_argument(
        "mode", choices=("on", "off", "status"), nargs="?", default="status"
    )
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
    key_generate = key_sub.add_parser(
        "generate", help="生成 runner 或 approver Ed25519 密钥对"
    )
    key_generate.add_argument("id")
    key_generate.add_argument("--private-key", required=True)
    key_generate.add_argument("--public-key", required=True)
    key_generate.set_defaults(func=cmd_key_generate)
    key_trust = key_sub.add_parser(
        "trust", help="将公钥安装到用途隔离的工作区 trust store"
    )
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
    key_revoke = key_sub.add_parser(
        "revoke", help="撤销指定用途的 trusted key ID；保留公钥与审计记录"
    )
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
    key_list.add_argument(
        "--show-status",
        action="store_true",
        help="同时显示 pending、expired 与 revoked key",
    )
    key_list.set_defaults(func=cmd_key_list)
    key_sub.add_parser("audit", help="输出 trust/revoke JSONL 审计记录").set_defaults(
        func=cmd_key_audit
    )
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
    witness_serve = witness_sub.add_parser(
        "serve", help="验证批次、签发回执并持久化 Witness ledger"
    )
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
    open_cmd.add_argument("--agent")
    open_cmd.add_argument("--prompt", default="")
    open_cmd.set_defaults(func=cmd_open)
    start = sub.add_parser("start", help="新人入口：检查工作区、选择开发线和 Agent")
    start.add_argument("--line")
    start.add_argument("--kind", choices=("line", "hotfix"))
    start.add_argument("--agent")
    start.add_argument("--prompt", default="")
    start.set_defaults(func=cmd_start)
    next_parser = sub.add_parser(
        "next",
        help="给出唯一安全下一步；已有目标时打印换工具开场白，不续另一家会话",
    )
    next_parser.add_argument(
        "--format", choices=("text", "json"), default="text"
    )
    next_parser.set_defaults(func=cmd_next)

    line = sub.add_parser("line", help="功能开发线")
    line_sub = line.add_subparsers(dest="line_command", required=True)
    line_list = line_sub.add_parser("list")
    line_list.add_argument("--kind", choices=("line", "hotfix"))
    line_list.add_argument("--format", choices=("text", "json"), default="text")
    line_list.set_defaults(func=cmd_line_list)
    line_create = line_sub.add_parser("create")
    line_create.add_argument("id")
    line_create.add_argument("--branch")
    line_create.add_argument("--base")
    line_create.add_argument(
        "--repos", help="逗号分隔；默认全部 configured repositories"
    )
    line_create.add_argument(
        "--repo-base",
        action="append",
        metavar="REPOSITORY=REF",
        help="为一个仓库覆盖默认基线；可重复",
    )
    line_create.add_argument(
        "--storage",
        action="append",
        metavar="REPOSITORY=MODE",
        help="仓库存储方式：linked-worktree 或 anchor-reference；可重复",
    )
    line_create.add_argument("--yes", action="store_true")
    line_create.set_defaults(func=cmd_line_create)

    hotfix = sub.add_parser("hotfix", help="生产 Hotfix 开发线")
    hotfix_sub = hotfix.add_subparsers(dest="hotfix_command", required=True)
    hotfix_create = hotfix_sub.add_parser("create")
    hotfix_create.add_argument("id")
    hotfix_create.add_argument("--branch")
    hotfix_create.add_argument("--base", required=True)
    hotfix_create.add_argument("--repos")
    hotfix_create.add_argument(
        "--repo-base",
        action="append",
        metavar="REPOSITORY=REF",
        help="为一个仓库覆盖 --base；可重复",
    )
    hotfix_create.add_argument(
        "--storage",
        action="append",
        metavar="REPOSITORY=MODE",
        help="仓库存储方式：linked-worktree 或 anchor-reference；可重复",
    )
    hotfix_create.add_argument("--yes", action="store_true")
    hotfix_create.set_defaults(func=cmd_hotfix_create)

    changeset = sub.add_parser("changeset", help="记录与核验跨仓交付提交组合")
    changeset_sub = changeset.add_subparsers(dest="changeset_command", required=True)
    changeset_create = changeset_sub.add_parser("create")
    changeset_create.add_argument("id")
    changeset_create.add_argument("--line", required=True)
    changeset_create.add_argument("--repos", help="逗号分隔；默认该开发线全部仓库")
    changeset_create.set_defaults(func=cmd_changeset_create)
    changeset_list = changeset_sub.add_parser("list")
    changeset_list.add_argument(
        "--format", choices=("text", "json"), default="text"
    )
    changeset_list.set_defaults(func=cmd_changeset_list)
    changeset_verify = changeset_sub.add_parser("verify")
    changeset_verify.add_argument("id")
    changeset_verify.add_argument(
        "--format", choices=("text", "json"), default="text"
    )
    changeset_verify.set_defaults(func=cmd_changeset_verify)

    objective = sub.add_parser(
        "objective", help="持久化并观察一个跨任务 Objective；此阶段不执行 Task"
    )
    objective_sub = objective.add_subparsers(dest="objective_command", required=True)
    objective_start = objective_sub.add_parser(
        "start", help="固定 Objective 合约、目标和依赖闭包"
    )
    objective_start.add_argument("--file", help="完整 Objective v1 TOML 合约")
    objective_start.add_argument("--id")
    objective_start.add_argument("--title")
    objective_start.add_argument("--line")
    objective_start.add_argument("--targets", help="逗号分隔的 Task ID；非文件模式必填")
    objective_start.add_argument(
        "--mode", choices=tuple(item.value for item in RequestedMode)
    )
    objective_start.add_argument(
        "--operation", action="append", choices=tuple(item.value for item in Operation)
    )
    objective_start.add_argument("--yes", action="store_true")
    objective_start.set_defaults(func=cmd_objective_start)
    objective_list = objective_sub.add_parser("list", help="列出已接受的 Objective")
    objective_list.add_argument(
        "--format", choices=("text", "json"), default="text"
    )
    objective_list.set_defaults(func=cmd_objective_list)
    objective_status = objective_sub.add_parser(
        "status", help="显示 Objective 状态和派生结果"
    )
    objective_status.add_argument("id")
    objective_status.add_argument(
        "--format", choices=("text", "json"), default="text"
    )
    objective_status.set_defaults(func=cmd_objective_status)
    objective_plan = objective_sub.add_parser(
        "plan", help="只读生成确定性 Objective action plan，不执行任务"
    )
    objective_plan.add_argument("id")
    objective_plan.add_argument("--format", choices=("text", "json"), default="text")
    objective_plan.set_defaults(func=cmd_objective_plan)
    objective_explain = objective_sub.add_parser(
        "explain",
        help="用人话解释当前事项，并给出一条只读下一步；不续另一家会话",
    )
    objective_explain.add_argument("id")
    objective_explain.add_argument("--format", choices=("text", "json"), default="text")
    objective_explain.set_defaults(func=cmd_objective_explain)
    objective_graph = objective_sub.add_parser(
        "graph", help="渲染 Objective、Task、Decision 与 Action 的只读组合图"
    )
    objective_graph.add_argument("id")
    objective_graph.add_argument(
        "--format", choices=("mermaid", "json"), default="mermaid"
    )
    objective_graph.set_defaults(func=cmd_objective_graph)
    objective_tick = objective_sub.add_parser(
        "tick",
        help="预览下一组有界 Objective Action 与预算；不创建 intent 或执行任务",
    )
    objective_tick.add_argument("id")
    objective_tick.add_argument("--format", choices=("text", "json"), default="text")
    objective_tick.set_defaults(func=cmd_objective_tick)
    objective_attention = objective_sub.add_parser(
        "attention", help="显示安全且只读的 Objective Attention 投影"
    )
    objective_attention.add_argument("id")
    objective_attention.add_argument(
        "--format", choices=("text", "json"), default="text"
    )
    objective_attention.set_defaults(func=cmd_objective_attention)
    objective_apply = objective_sub.add_parser(
        "apply",
        help="显示精确 Action wave；确认后仅受监督地执行 execute/review，不 merge 或 push",
    )
    objective_apply.add_argument("id")
    objective_apply.add_argument(
        "--confirm-sha", help="非交互 --yes 必填；必须等于当前 Confirmation SHA-256"
    )
    objective_apply.add_argument(
        "--yes", action="store_true", help="确认当前显示的精确 wave 后执行"
    )
    objective_apply.add_argument("--format", choices=("text", "json"), default="text")
    objective_apply.set_defaults(func=cmd_objective_apply)
    for command, function, help_text in (
        ("pause", cmd_objective_pause, "暂停后续推进，不写完成状态"),
        ("resume", cmd_objective_resume, "恢复 paused Objective 的 ownership"),
        ("stop", cmd_objective_stop, "终止 Objective；不能再恢复"),
        (
            "reconcile",
            cmd_objective_reconcile,
            "重新固定 TaskGraph scope 与 contract 哈希",
        ),
    ):
        parser_item = objective_sub.add_parser(command, help=help_text)
        parser_item.add_argument("id")
        parser_item.add_argument("--yes", action="store_true")
        parser_item.set_defaults(func=function)
    objective_scope = objective_sub.add_parser(
        "scope", help="显式调整 Objective targets 并建立新 revision"
    )
    objective_scope_sub = objective_scope.add_subparsers(
        dest="objective_scope_command", required=True
    )
    for command, function, help_text in (
        ("add", cmd_objective_scope_add, "将同一开发线的 Task 加入 targets"),
        ("remove", cmd_objective_scope_remove, "从 targets 移除一个 Task"),
    ):
        parser_item = objective_scope_sub.add_parser(command, help=help_text)
        parser_item.add_argument("id")
        parser_item.add_argument("task")
        parser_item.add_argument("--yes", action="store_true")
        parser_item.set_defaults(func=function)

    trigger = sub.add_parser(
        "trigger", help="只读 Trigger 观测与有界 provider 协议入口"
    )
    trigger_sub = trigger.add_subparsers(dest="trigger_command", required=True)
    trigger_list = trigger_sub.add_parser(
        "list", help="列出内置 Trigger 类型与 provider 边界"
    )
    trigger_list.add_argument("--format", choices=("text", "json"), default="text")
    trigger_list.set_defaults(func=cmd_trigger_list)
    trigger_probe = trigger_sub.add_parser(
        "probe", help="用显式事实执行一次只读内置 Trigger 观测"
    )
    trigger_probe.add_argument(
        "kind", choices=tuple(item.value for item in TriggerKind)
    )
    trigger_probe.add_argument("--id", default="manual-probe")
    trigger_probe.add_argument(
        "--at", help="观测时间（带时区 ISO-8601）；默认当前 UTC 时间"
    )
    trigger_probe.add_argument(
        "--not-before", help="time_due 的最早触发时间（带时区 ISO-8601）"
    )
    trigger_probe.add_argument(
        "--current", action="append", metavar="KEY=VALUE", help="当前事实；可重复指定"
    )
    trigger_probe.add_argument(
        "--previous", action="append", metavar="KEY=VALUE", help="上次事实；可重复指定"
    )
    trigger_probe.add_argument("--signal", help="manual_signal 的非空人工信号")
    trigger_probe.add_argument("--format", choices=("text", "json"), default="text")
    trigger_probe.set_defaults(func=cmd_trigger_probe)
    trigger_signal = trigger_sub.add_parser(
        "signal", help="输出一次临时人工信号观测，不写入任务或控制面"
    )
    trigger_signal.add_argument("signal")
    trigger_signal.add_argument("--id", default="manual-signal")
    trigger_signal.add_argument(
        "--at", help="观测时间（带时区 ISO-8601）；默认当前 UTC 时间"
    )
    trigger_signal.add_argument("--format", choices=("text", "json"), default="text")
    trigger_signal.set_defaults(func=cmd_trigger_signal)

    proof = sub.add_parser("proof", help="只读派生并核验交付 Proof（rebind，不是 replay）")
    proof_sub = proof.add_subparsers(dest="proof_command", required=True)
    proof_list = proof_sub.add_parser(
        "list",
        help="从当前工作区全量重派生 Proof（含 trigger_observation；--task 不含；--line 只含该线 Objective 的 trigger）",
    )
    proof_list.add_argument("--task")
    proof_list.add_argument("--objective")
    proof_list.add_argument("--line")
    proof_list.add_argument("--format", choices=("text", "json"), default="text")
    proof_list.set_defaults(func=cmd_proof_list)
    proof_show = proof_sub.add_parser("show", help="显示一条重派生的 Proof")
    proof_show.add_argument("proof_id")
    proof_show.add_argument("--format", choices=("text", "json"), default="text")
    proof_show.set_defaults(func=cmd_proof_show)
    proof_verify = proof_sub.add_parser("verify", help="对当前工作区做衰减与绑定重算，不重跑 gate")
    proof_verify.add_argument("proof_id", nargs="?")
    proof_verify.add_argument("--task")
    proof_verify.add_argument("--objective")
    proof_verify.add_argument("--line")
    proof_verify.add_argument("--format", choices=("text", "json"), default="text")
    proof_verify.add_argument(
        "--rerun-procedure",
        action="store_true",
        help="0.7 拒绝：隔离 replay 尚未提供",
    )
    proof_verify.set_defaults(func=cmd_proof_verify)
    proof_export = proof_sub.add_parser("export", help="导出 Proof Bundle（schema_version=1，不含 git 对象）")
    proof_export.add_argument("proof_id", nargs="?")
    proof_export.add_argument("--task")
    proof_export.add_argument("--bundle", required=True, help="输出 .zip 路径")
    proof_export.set_defaults(func=cmd_proof_export)
    proof_verify_bundle = proof_sub.add_parser(
        "verify-bundle",
        help="核验 bundle 完整性；需要调用方 --git-dir，不是当前工作区 verify，也不是身份证明，不是 merge",
    )
    proof_verify_bundle.add_argument("bundle")
    proof_verify_bundle.add_argument(
        "--git-dir",
        action="append",
        default=[],
        help="调用方必须传入包含已钉 SHA 的对象库；可重复，按并集查找，不是 repo_id 映射。缺省或无 pin 不得报 live",
    )
    proof_verify_bundle.add_argument(
        "--current-heads",
        help="可选 JSON：提供后才允许衰减结论，不得与 merge 混称",
    )
    proof_verify_bundle.add_argument("--format", choices=("text", "json"), default="text")
    proof_verify_bundle.set_defaults(func=cmd_proof_verify_bundle)

    task = sub.add_parser("task", help="任务编排")
    task_sub = task.add_subparsers(dest="task_command", required=True)
    task_graph = task_sub.add_parser("graph", help="编译、校验或渲染任务图")
    task_graph.add_argument(
        "action", nargs="?", choices=("show", "check"), default="show"
    )
    task_graph.add_argument("--line", help="只显示或校验指定开发线")
    task_graph.add_argument("--format", choices=("mermaid", "json"), default="mermaid")
    task_graph.set_defaults(func=cmd_task_graph)
    task_explain = task_sub.add_parser(
        "explain", help="解释任务当前为什么可调度或被阻塞"
    )
    task_explain.add_argument("id")
    task_explain.set_defaults(func=cmd_task_explain)
    task_attempts = task_sub.add_parser(
        "attempts", help="显示任务的本地执行 provenance"
    )
    task_attempts.add_argument("id")
    task_attempts.set_defaults(func=cmd_task_attempts)
    task_binding = task_sub.add_parser(
        "binding", help="输出 review 所需的完整 attempt 与 plan binding"
    )
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
    task_open = task_sub.add_parser(
        "open", help="进入已存在的任务工作树，不改变任务状态"
    )
    task_open.add_argument("id")
    task_open.add_argument("--agent")
    task_open.add_argument("--prompt", default="")
    task_open.set_defaults(func=cmd_task_open)
    task_claim = task_sub.add_parser("claim", help="由隔离执行器一次性领取任务")
    task_claim.add_argument("id")
    task_claim.add_argument("--by", required=True, help="执行器实例或受信任身份")
    task_claim.add_argument("--key-id", help="与 claim 绑定的 trusted execution key ID")
    task_claim.add_argument(
        "--lease-seconds", type=int, default=3600, help="claim 租约秒数；默认 3600"
    )
    task_claim.add_argument(
        "--output",
        help="把新 claim 以 0600 权限导出到 runner 交接路径；拒绝覆盖",
    )
    task_claim.set_defaults(func=cmd_task_claim)
    task_claim_renew = task_sub.add_parser(
        "claim-renew", help="由当前 runner 续租未过期 claim"
    )
    task_claim_renew.add_argument("id")
    task_claim_renew.add_argument(
        "--by", required=True, help="当前执行器实例或受信任身份"
    )
    task_claim_renew.add_argument(
        "--lease-seconds", type=int, default=3600, help="续租秒数；默认 3600"
    )
    task_claim_renew.set_defaults(func=cmd_task_claim_renew)
    task_claim_release = task_sub.add_parser(
        "claim-release", help="释放当前 runner 的 claim"
    )
    task_claim_release.add_argument("id")
    task_claim_release.add_argument(
        "--by", required=True, help="当前执行器实例或受信任身份"
    )
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
    evidence_execution = evidence_sub.add_parser(
        "execution", help="导入执行回执与门禁结果"
    )
    evidence_execution.add_argument("id")
    evidence_input = evidence_execution.add_mutually_exclusive_group(required=True)
    evidence_input.add_argument("--receipt")
    evidence_input.add_argument(
        "--bundle", help="由 task evidence build 生成的可移植 ZIP 证据包"
    )
    evidence_execution.add_argument(
        "--gates", help="外部门禁 JSON；任务含 gates 时必填"
    )
    evidence_execution.add_argument(
        "--heads", help="执行后逐仓 Git HEAD JSON；DONE 回执时必填"
    )
    evidence_execution.add_argument(
        "--provenance", help="可选的外部 execution provenance JSON"
    )
    evidence_execution.add_argument(
        "--allow-legacy",
        action="store_true",
        help="显式允许导入缺少 provenance 的旧证据",
    )
    evidence_execution.set_defaults(func=cmd_task_evidence_execution)
    evidence_build = evidence_sub.add_parser(
        "build", help="在隔离 runner 中运行门禁并构建可导入 ZIP 证据包"
    )
    evidence_build.add_argument("id")
    evidence_build.add_argument(
        "--workspace", required=True, help="隔离 runner 中任务分支的多仓工作区"
    )
    evidence_build.add_argument(
        "--receipt", required=True, help="执行器写出的 receipt.md"
    )
    evidence_build.add_argument(
        "--output", required=True, help="新 ZIP 证据包的输出路径；拒绝覆盖已有文件"
    )
    evidence_build.add_argument("--signing-key", help="Ed25519 runner 私钥 PEM")
    evidence_build.add_argument(
        "--key-id", help="已安装到 execution trust store 的 key ID"
    )
    evidence_build.add_argument(
        "--claim", help="控制面导出的 claim.json；默认读取任务目录"
    )
    evidence_build.set_defaults(func=cmd_task_evidence_build)
    evidence_review = evidence_sub.add_parser(
        "review", help="导入 receipt-bound 复核结果"
    )
    evidence_review.add_argument("id")
    evidence_review.add_argument("--file", required=True)
    evidence_review.set_defaults(func=cmd_task_evidence_review)
    evidence_review_build = evidence_sub.add_parser(
        "review-build", help="构建独立 reviewer 签名的 review JSON"
    )
    evidence_review_build.add_argument("id")
    evidence_review_build.add_argument(
        "--file", required=True, help="包含 verdict 与绑定字段的 review.md"
    )
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
    evidence_generations.add_argument(
        "--prune", action="store_true", help="执行或预演清理计划"
    )
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
        dispatch_command = dispatch_remaining[0] if dispatch_remaining else ""
        selected_root = global_args.root
        if global_args.workspace_alias:
            selected_root = str(get_workspace(global_args.workspace_alias).root)
        if (
            selected_root
            and dispatch_command in {"run", "panel", "batch-plan", "batch-start"}
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


def _maybe_run_daily_update(*, install=None) -> bool:
    """Run the daily update path.

    Returns ``True`` when a successful package update already triggered a
    best-effort Skill refresh in this process. Callers must then skip
    in-process Skill sync so stale in-memory assets cannot overwrite the
    fresh subprocess write.
    """
    if install is None:
        install = perform_update
    try:
        result = check_for_update(__version__)
        if not result.checked or result.error or result.kind == UpdateKind.NONE:
            return False
        state = load_update_state()
    except (DyroError, OSError):
        return False
    print(f"\n发现 Dyro {result.latest_version}（当前 {result.current_version}）。")
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
            print("本次启动继续使用当前版本；稍后可运行 dyro update 重试。")
            return False
        if updated:
            print("自动更新完成；本次启动继续运行，下次将使用新版本。")
            _refresh_skill_via_new_cli()
            return True
        return False
    print("运行 dyro update 可确认并完成更新；今天不再重复提示。")
    return False


def _fresh_dyro_argv(*cli_args: str) -> list[str]:
    """Build argv for a new Dyro process bound to this interpreter install."""
    executable = Path(sys.executable)
    for candidate in (
        executable.with_name("dyro"),
        executable.with_name("dyro.exe"),
    ):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return [str(candidate), *cli_args]
    return [sys.executable, "-m", "dyro", *cli_args]


def _refresh_skill_via_new_cli() -> None:
    """Best-effort Skill-bundle sync through the freshly installed CLI."""
    requests = [("sync", "skill")]
    statuses = {}
    for integration, _label in _MANAGED_SKILL_BUNDLE:
        try:
            statuses[integration] = integration_status(integration)
        except (DyroError, OSError, ValidationError):
            continue
    control_status = statuses.get("skill")
    control_opted_in = (
        control_status is not None
        and control_status.state
        in {IntegrationState.CURRENT, IntegrationState.OUTDATED}
    )
    if control_opted_in:
        # Existing control-plane ownership is the user's one-time opt-in to the
        # first-party Skill bundle. Install newly shipped companions safely.
        for integration in COMPANION_IDS:
            status = statuses.get(integration)
            if status is not None and (
                status.state is IntegrationState.CURRENT
                or _skill_status_blocks_automatic_change(status.state)
            ):
                continue
            requests.append(("install", integration))
    else:
        for integration in COMPANION_IDS:
            status = statuses.get(integration)
            if status is not None and status.state is IntegrationState.OUTDATED:
                requests.append(("sync", integration))

    print("正在同步已托管的 Dyro Skills……")
    for action, integration in requests:
        argv = _fresh_dyro_argv(
            "integration",
            action,
            integration,
            "--yes",
        )
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(
                warning(
                    f"{integration} Skill 同步未完成：{exc}；下次启动将重试。"
                )
            )
            continue
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            message = f"{integration} Skill 同步未完成"
            if detail:
                message += f"：{detail}"
            print(warning(message + "；下次启动将重试。"))
            continue
        output = (completed.stdout or "").strip()
        if output:
            print(output)


def _maybe_sync_managed_skill() -> None:
    """Auto-repair the opted-in Skill bundle on interactive launch."""
    statuses = {}
    for integration, _label in _MANAGED_SKILL_BUNDLE:
        try:
            statuses[integration] = integration_status(integration)
        except (DyroError, OSError, ValidationError):
            continue
    control_status = statuses.get("skill")
    control_opted_in = (
        control_status is not None
        and control_status.state
        in {IntegrationState.CURRENT, IntegrationState.OUTDATED}
    )
    candidates = []
    for integration, label in _MANAGED_SKILL_BUNDLE:
        status = statuses.get(integration)
        if status is None or _skill_status_blocks_automatic_change(status.state):
            continue
        allow_first_install = (
            integration in COMPANION_IDS
            and status.state is IntegrationState.ABSENT
            and control_opted_in
        )
        if status.state is IntegrationState.OUTDATED or allow_first_install:
            candidates.append((integration, label, allow_first_install))
    if not candidates:
        return

    print("\n检测到 Dyro Skills 可自动安装 / 同步，正在处理……")
    changed = []
    for integration, label, allow_first_install in candidates:
        try:
            plan = sync_managed_skill(
                integration,
                yes=True,
                allow_first_install=allow_first_install,
            )
        except DyroError as exc:
            print(
                warning(
                    f"{label} Skill 自动同步未完成：{exc}；可运行 "
                    f"dyro integration sync {integration} --dry-run 查看详情。"
                )
            )
            continue
        if plan is not None:
            changed.append((label, plan))
    if not changed:
        return
    print(success("Dyro Skills 已同步到当前 Dyro 包。"))
    for label, plan in changed:
        print(f"  - {label}")
        for change in plan.changes:
            print("    - " + change)


def main(argv: list[str] | None = None) -> None:
    import sys

    # The optional local dispatch surface ships in the dyro wheel.
    raw = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args: argparse.Namespace | None = None
    try:
        experiment = _route_experiment_surface(raw)
        if experiment is not None and experiment[0] == "dispatch":
            from experiments.local_agent_dispatch.cli import main as dispatch_main

            raise SystemExit(dispatch_main(experiment[1]))
        args = parser.parse_args(argv)
        if _should_run_daily_update(args):
            # After a same-turn package update + Skill refresh, skip in-process
            # sync so stale in-memory assets cannot overwrite the fresh write.
            if not _maybe_run_daily_update():
                _maybe_sync_managed_skill()
        if hasattr(args, "func"):
            args.func(args)
        else:
            cmd_home(args)
    except DyroError as exc:
        if args is not None and getattr(args, "format", None) == "json":
            _print_control_plane_error(args, exc)
            raise SystemExit(2) from None
        parser.exit(2, danger(f"错误：{exc}\n", stream=sys.stderr))
    except OSError as exc:
        if args is not None and getattr(args, "format", None) == "json":
            _print_control_plane_error(args, exc)
            raise SystemExit(2) from None
        raise
    except (KeyboardInterrupt, EOFError):
        if args is not None and getattr(args, "format", None) == "json":
            _print_control_plane_json(
                "error",
                stream=sys.stderr,
                code="INTERRUPTED",
                command=_control_plane_command(args),
                retryable=False,
            )
            raise SystemExit(130) from None
        parser.exit(
            130,
            muted(
                "\n已停止当前操作；未完成的步骤不会继续。"
                "若刚开始执行写入、clone 或创建 worktree，请运行 dyro doctor 确认状态。\n",
                stream=sys.stderr,
            ),
        )


if __name__ == "__main__":
    main()

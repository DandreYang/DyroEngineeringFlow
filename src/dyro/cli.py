from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import shlex
import time

from . import __version__
from .changesets import create_changeset, get_changeset, list_changesets, verify_changeset
from .config import CONFIG_NAME, Config, expand_argv, load, validate_id
from .evidence import build_execution_bundle, unpack_execution_bundle
from .errors import DyroError
from .onboarding import (
    append_repository,
    ask_for_workspace,
    bootstrap,
    discover_repositories,
    render_config,
    repository_input_from_path,
)
from .profile import append_adapter, command_adapter, config_value, preset_adapter, set_config_value, test_adapter
from .state import atomic_write_text, exclusive_lock
from .tasks import (
    STATUSES,
    answer_task,
    board,
    check_dispatchable,
    claim_task,
    decisions,
    import_execution_evidence,
    import_review_evidence,
    list_tasks,
    load_task,
    loop_tasks,
    merge_task,
    review_task,
    run_gates,
    run_task,
    set_status,
    signoff_task,
    stats,
    status as task_status,
    task_template,
)
from .workspace import create_line, doctor, get_line, line_root, list_lines, status_rows


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

# Commands are argv arrays, not shell strings.  DyroEngineeringFlow expands only
# {{workspace}}, {{root}}, {{task}}, {{line}} and {{prompt}}.
[adapters.codex]
launch = ["codex", "-C", "{{workspace}}"]
read = ["codex", "exec", "--skip-git-repo-check", "--sandbox", "workspace-write", "{{prompt}}"]
write = ["codex", "exec", "--skip-git-repo-check", "--sandbox", "workspace-write", "{{prompt}}"]

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
    root = Path(args.root).expanduser() if getattr(args, "root", None) else Path.cwd()
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


def cmd_setup(args: argparse.Namespace) -> None:
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
        print("下一步：dyro start --line " + args.line if not args.no_line else "下一步：dyro line create <id> --yes")


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


def cmd_status(args: argparse.Namespace) -> None:
    rows = status_rows(_config(args))
    print(f"{'SCOPE':24} {'REPOSITORY':14} {'BRANCH':24} {'HEAD':12} {'DIRTY':>5} UPSTREAM")
    for scope, repo_id, branch, head, upstream, dirty in rows:
        dirty_text = "-" if dirty < 0 else str(dirty)
        print(f"{scope:24} {repo_id:14} {branch:24} {head:12} {dirty_text:>5} {upstream}")


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
    config = _config(args)
    line = get_line(config, args.line, args.kind)
    try:
        adapter = config.adapters[args.agent]
    except KeyError as exc:
        raise DyroError(f"未配置 Agent adapter：{args.agent}") from exc
    workspace = line_root(config, line)
    if not workspace.is_dir():
        raise DyroError(f"开发线工作区不存在：{workspace}")
    argv = expand_argv(adapter.launch, workspace=workspace, root=config.root, task="", line=line.id, prompt=args.prompt or "")
    _print_command(argv)
    if args.dry_run:
        return
    os.chdir(workspace)
    os.execvp(argv[0], list(argv))


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
    line_id = args.line or _choose("开发线", [line.id for line in list_lines(config)])
    line = get_line(config, line_id, args.kind)
    agent = args.agent or _choose("Agent", sorted(config.adapters))
    open_args = argparse.Namespace(root=str(config.root), line=line.id, kind=line.kind, agent=agent, prompt=args.prompt or "", dry_run=args.dry_run)
    cmd_open(open_args)


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


def cmd_task_claim(args: argparse.Namespace) -> None:
    config = _config(args)
    task = load_task(config, args.id)
    print(f"{task.id} -> {claim_task(config, task, runner=args.by, dry_run=args.dry_run)}")


def cmd_task_next(args: argparse.Namespace) -> None:
    config = _config(args)
    candidates = []
    for task in list_tasks(config):
        if task_status(config, task) not in ("backlog", "assigned"):
            continue
        try:
            check_dispatchable(config, task)
            candidates.append(task)
        except DyroError:
            continue
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
    print(f"{task.id} -> {signoff_task(config, task, approver=args.by, dry_run=args.dry_run)}")


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
                dry_run=args.dry_run,
            )
    else:
        result = import_execution_evidence(
            config,
            task,
            receipt=Path(args.receipt),
            gates=Path(args.gates) if args.gates else None,
            heads=Path(args.heads) if args.heads else None,
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
        dry_run=args.dry_run,
    )
    print(f"{task.id} -> {bundle.result}: {bundle.output}")
    if not bundle.gates_passed:
        raise DyroError("外部门禁失败；已输出证据包供排查，不能导入为 DONE")


def cmd_task_evidence_review(args: argparse.Namespace) -> None:
    config = _config(args)
    task = load_task(config, args.id)
    print(f"{task.id} -> {import_review_evidence(config, task, review=Path(args.file), dry_run=args.dry_run)}")


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


def cmd_task_daemon(args: argparse.Namespace) -> None:
    config = _config(args)
    while True:
        tasks = list_tasks(config)
        active_groups = {task.conflict_group for task in tasks if task.conflict_group and task_status(config, task) == "in_progress"}
        queued = []
        for task in tasks:
            if task_status(config, task) != "assigned" or len(queued) >= max(1, args.parallel):
                continue
            if task.conflict_group and task.conflict_group in active_groups:
                continue
            queued.append(task)
            if task.conflict_group:
                active_groups.add(task.conflict_group)
        if queued:
            with ThreadPoolExecutor(max_workers=max(1, args.parallel), thread_name_prefix="dyro-dispatch") as pool:
                futures = {pool.submit(run_task, config, task, dry_run=args.dry_run): task for task in queued}
                for future in as_completed(futures):
                    task = futures[future]
                    try:
                        print(f"dispatch {task.id} -> {future.result()}")
                    except DyroError as exc:
                        print(f"skip {task.id}: {exc}")
        review_queue = [task for task in list_tasks(config) if task_status(config, task) == "review"]
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
    parser.add_argument("--root", help="工作区根目录；默认从当前目录向上查找 dyro.toml")
    parser.add_argument("--dry-run", action="store_true", help="仅输出计划，不写文件、不调用 Agent 或 Git 写操作")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dyro", description="DyroEngineeringFlow：本地优先的多仓工程自动化与交付控制平台")
    parser.add_argument("--version", action="version", version=f"dyro {__version__}")
    _add_common(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="初始化工作区配置")
    init.add_argument("path", nargs="?", default=".")
    init.add_argument("--name", default="my-workspace")
    init.add_argument("--base", default="main", help="--discover 时写入的默认基线分支")
    init_mode = init.add_mutually_exclusive_group()
    init_mode.add_argument("--wizard", action="store_true", help="交互式登记真实仓库与可选 remote")
    init_mode.add_argument("--discover", action="store_true", help="自动发现当前目录下的 Git 仓库并登记 origin")
    init.set_defaults(func=cmd_init)

    setup = sub.add_parser("setup", help="新人一键创建 Profile、目录与首条开发线")
    setup.add_argument("path", nargs="?", default=".")
    setup.add_argument("--name", help="新 Profile 的工作区名称；默认由目录名推断")
    setup.add_argument("--base", help="首条开发线与新 Profile 的默认基线；默认 main")
    setup.add_argument("--line", default="dev", help="首条功能开发线 ID；默认 dev")
    setup.add_argument("--branch", help="首条开发线分支；默认 feat/<line>")
    setup.add_argument("--no-line", action="store_true", help="仅建立 Profile，不创建 Git worktree 开发线")
    setup.add_argument("--yes", action="store_true", help="确认创建首条 Git worktree 开发线")
    setup.set_defaults(func=cmd_setup)

    sub.add_parser("doctor", help="验证动态工作区结构").set_defaults(func=cmd_doctor)
    sub.add_parser("status", help="显示 anchors 与开发线 Git 状态").set_defaults(func=cmd_status)
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
    agent_add = agent_sub.add_parser("add", help="通过预设或命令登记 Agent，无需编辑 TOML")
    agent_add.add_argument("id")
    agent_source = agent_add.add_mutually_exclusive_group(required=True)
    agent_source.add_argument("--preset", choices=("codex", "noop"))
    agent_source.add_argument("--command", help="作为 launch/read/write 的 argv 命令行；不会经 shell 执行")
    agent_add.set_defaults(func=cmd_agent_add)
    agent_test = agent_sub.add_parser("test", help="仅检查 adapter 可执行文件是否可用，不启动 Agent")
    agent_test.add_argument("id")
    agent_test.set_defaults(func=cmd_agent_test)
    config_command = sub.add_parser("config", help="安全地读取或修改常用 Profile 策略")
    config_sub = config_command.add_subparsers(dest="config_command", required=True)
    config_get = config_sub.add_parser("get")
    config_get.add_argument("key")
    config_get.set_defaults(func=cmd_config_get)
    config_set = config_sub.add_parser("set")
    config_set.add_argument("key")
    config_set.add_argument("value")
    config_set.set_defaults(func=cmd_config_set)
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

    task = sub.add_parser("task", help="任务编排")
    task_sub = task.add_subparsers(dest="task_command", required=True)
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
    task_claim = task_sub.add_parser("claim", help="由隔离执行器一次性领取任务")
    task_claim.add_argument("id")
    task_claim.add_argument("--by", required=True, help="执行器实例或受信任身份")
    task_claim.set_defaults(func=cmd_task_claim)
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
    evidence_execution.set_defaults(func=cmd_task_evidence_execution)
    evidence_build = evidence_sub.add_parser("build", help="在隔离 runner 中运行门禁并构建可导入 ZIP 证据包")
    evidence_build.add_argument("id")
    evidence_build.add_argument("--workspace", required=True, help="隔离 runner 中任务分支的多仓工作区")
    evidence_build.add_argument("--receipt", required=True, help="执行器写出的 receipt.md")
    evidence_build.add_argument("--output", required=True, help="新 ZIP 证据包的输出路径；拒绝覆盖已有文件")
    evidence_build.set_defaults(func=cmd_task_evidence_build)
    evidence_review = evidence_sub.add_parser("review", help="导入 receipt-bound 复核结果")
    evidence_review.add_argument("id")
    evidence_review.add_argument("--file", required=True)
    evidence_review.set_defaults(func=cmd_task_evidence_review)
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


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except DyroError as exc:
        parser.exit(2, f"错误：{exc}\n")


if __name__ == "__main__":
    main()

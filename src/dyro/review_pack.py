from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import fnmatch
from pathlib import Path
import time

from .config import Config
from .process import git, run
from .workspace import get_line, line_repository_path, Line

IGNORE_DIFF_PATTERNS = (
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "uv.lock",
    "poetry.lock",
    "*.min.js",
    "*.min.css",
    "*.map",
    "*.svg",
    "*.png",
    "*.jpg",
    "*.jpeg",
    "*.ico",
    "*.pdf",
    "*.log",
    "dist/*",
    "build/*",
    "target/*",
    "unpackage/*",
    ".pytest_cache/*",
    ".idea/*",
    ".vscode/*",
)

CONTRACT_PATTERNS = (
    "*dto*",
    "*schema*",
    "*schemas*",
    "*model*",
    "*models*",
    "*entity*",
    "*entities*",
    "*api*",
    "*controller*",
    "*protocol*",
    "*contract*",
    "*constants*",
    "*.sql",
    "*ddl*",
)


@dataclass(frozen=True)
class GateResult:
    repo_id: str
    argv: tuple[str, ...]
    code: int
    passed: bool
    stdout: str
    elapsed_seconds: float


@dataclass(frozen=True)
class RepoDiffSummary:
    repo_id: str
    branch: str
    base: str
    commits_count: int
    commits_log: str
    stat_summary: str
    contract_files: tuple[str, ...]
    other_files: tuple[str, ...]
    file_diffs: dict[str, str]


def is_ignored(filename: str) -> bool:
    norm = filename.replace("\\", "/")
    for pattern in IGNORE_DIFF_PATTERNS:
        if fnmatch.fnmatch(norm, pattern) or fnmatch.fnmatch(Path(norm).name, pattern):
            return True
    return False


def is_contract(filename: str) -> bool:
    norm = filename.replace("\\", "/").lower()
    for pattern in CONTRACT_PATTERNS:
        if fnmatch.fnmatch(norm, pattern) or fnmatch.fnmatch(Path(norm).name, pattern):
            return True
    return False


def run_line_verify(
    config: Config,
    line: Line,
    *,
    dry_run: bool = False,
) -> list[GateResult]:
    results: list[GateResult] = []
    for repo_id in line.repositories:
        repo_cfg = config.repositories.get(repo_id)
        if not repo_cfg or not repo_cfg.verify:
            continue
        repo_path = line_repository_path(config, line, repo_id)
        if not repo_path.is_dir():
            continue
        for cmd in repo_cfg.verify:
            start_time = time.perf_counter()
            exec_res = run(cmd, cwd=repo_path, dry_run=dry_run)
            elapsed = time.perf_counter() - start_time
            passed = exec_res.code == 0
            results.append(
                GateResult(
                    repo_id=repo_id,
                    argv=tuple(cmd),
                    code=exec_res.code,
                    passed=passed,
                    stdout=exec_res.stdout.strip(),
                    elapsed_seconds=elapsed,
                )
            )
    return results


def analyze_repo_diff(
    config: Config,
    line: Line,
    repo_id: str,
    *,
    base: str | None = None,
    scope: str = "contracts",
    max_lines_per_file: int = 250,
) -> RepoDiffSummary | None:
    repo_path = line_repository_path(config, line, repo_id)
    if not repo_path.is_dir():
        return None

    actual_base = base or line.base_for(repo_id)

    verify_base = git(repo_path, "rev-parse", "--verify", "-q", actual_base)
    if verify_base.code != 0:
        alt_base = f"origin/{actual_base}"
        if git(repo_path, "rev-parse", "--verify", "-q", alt_base).code == 0:
            actual_base = alt_base

    branch_res = git(repo_path, "branch", "--show-current")
    branch = branch_res.stdout.strip() or line.branch

    log_res = git(repo_path, "log", "--oneline", f"{actual_base}...HEAD")
    commits = [line for line in log_res.stdout.strip().splitlines() if line.strip()]
    commits_count = len(commits)
    commits_log = "\n".join(commits[:15])
    if commits_count > 15:
        commits_log += f"\n... (共 {commits_count} 个提交，省略剩余 {commits_count - 15} 个)"

    stat_res = git(repo_path, "diff", f"{actual_base}...HEAD", "--stat")
    stat_summary = stat_res.stdout.strip()

    names_res = git(repo_path, "diff", f"{actual_base}...HEAD", "--name-status")
    changed_names = [line.strip() for line in names_res.stdout.strip().splitlines() if line.strip()]

    contract_files: list[str] = []
    other_files: list[str] = []
    file_diffs: dict[str, str] = {}

    for line_item in changed_names:
        parts = line_item.split(maxsplit=1)
        if len(parts) < 2:
            continue
        _status, file_path = parts[0], parts[1]
        if " -> " in file_path:
            file_path = file_path.split(" -> ")[1].strip()

        if is_ignored(file_path):
            continue

        if is_contract(file_path):
            contract_files.append(file_path)
        else:
            other_files.append(file_path)

    # Determine which files need full diff based on scope
    files_to_diff: list[str] = []
    if scope == "contracts":
        files_to_diff = list(contract_files)
    elif scope == "all":
        files_to_diff = list(contract_files) + list(other_files)

    for file_path in files_to_diff:
        diff_res = git(repo_path, "diff", f"{actual_base}...HEAD", "--", file_path)
        raw_diff = diff_res.stdout
        diff_lines = raw_diff.splitlines()

        if len(diff_lines) > max_lines_per_file:
            truncated_diff = "\n".join(diff_lines[:max_lines_per_file])
            truncated_diff += f"\n... [Dyro 提示：该文件改动超过 {max_lines_per_file} 行，已智能截断以降低 Token 消耗。原文件共 {len(diff_lines)} 行]"
            file_diffs[file_path] = truncated_diff
        else:
            file_diffs[file_path] = raw_diff

    return RepoDiffSummary(
        repo_id=repo_id,
        branch=branch,
        base=actual_base,
        commits_count=commits_count,
        commits_log=commits_log,
        stat_summary=stat_summary,
        contract_files=tuple(contract_files),
        other_files=tuple(other_files),
        file_diffs=file_diffs,
    )


def build_review_pack(
    config: Config,
    line_id: str,
    *,
    base: str | None = None,
    scope: str = "contracts",
    run_verify: bool = True,
    max_lines_per_file: int = 250,
    dry_run: bool = False,
) -> str:
    line = get_line(config, line_id)
    now_str = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")

    sections: list[str] = [
        "# Dyro 开发线复核审查靶区包（Review Pack）",
        f"- **开发线 ID**：`{line.id}`",
        f"- **分支**：`{line.branch}`",
        f"- **对比基线（Base）**：`{base or line.base}`",
        f"- **审查模式（Scope）**：`{scope}`（{'契约与协议靶区高亮·Token优化' if scope == 'contracts' else '全量改动Diff' if scope == 'all' else '改动概览与索引'}）",
        f"- **生成时间**：{now_str}",
        "",
    ]

    # 1. Verification Gates
    gates_failed = False
    if run_verify:
        gate_results = run_line_verify(config, line, dry_run=dry_run)
        sections.append("## 一、 本地静态门禁验证（Verify Gates）")
        if not gate_results:
            sections.append("> ℹ️ 当前开发线仓库未配置或未执行本地 verify 门禁。\n")
        else:
            sections.append("| 仓库 | 门禁指令 | 耗时 | 状态 |")
            sections.append("|:---|:---|:---|:---|")
            for gr in gate_results:
                status_label = "✅ PASS" if gr.passed else "❌ FAIL"
                cmd_str = " ".join(gr.argv)
                sections.append(f"| `{gr.repo_id}` | `{cmd_str}` | {gr.elapsed_seconds:.2f}s | {status_label} |")
                if not gr.passed:
                    gates_failed = True

            sections.append("")
            if gates_failed:
                sections.append("> 🚨 **门禁阻断告警**：本地静态门禁未全部通过！请优先解决机器检查报错，避免消耗 AI Token 进行无意义的肉眼语法排查。\n")
                sections.append("### 门禁失败日志摘录")
                for gr in gate_results:
                    if not gr.passed:
                        snippet = "\n".join(gr.stdout.splitlines()[-25:])
                        sections.append(f"#### `{gr.repo_id}`: `{' '.join(gr.argv)}`")
                        sections.append(f"```text\n{snippet}\n```\n")

    # 2. Repo Diff Analysis
    repo_summaries: list[RepoDiffSummary] = []
    for repo_id in line.repositories:
        summary = analyze_repo_diff(
            config,
            line,
            repo_id,
            base=base,
            scope=scope,
            max_lines_per_file=max_lines_per_file,
        )
        if summary is not None:
            repo_summaries.append(summary)

    sections.append("## 二、 跨仓改动总览（Cross-Repo Diff Overview）")
    sections.append("| 仓库 | 分支 | 基线 | 提交数 | 契约文件数 | 业务文件数 |")
    sections.append("|:---|:---|:---|:---|:---|:---|")
    total_commits = 0
    total_contracts = 0
    total_others = 0

    for rs in repo_summaries:
        c_count = len(rs.contract_files)
        o_count = len(rs.other_files)
        total_commits += rs.commits_count
        total_contracts += c_count
        total_others += o_count
        sections.append(f"| `{rs.repo_id}` | `{rs.branch}` | `{rs.base}` | {rs.commits_count} | {c_count} | {o_count} |")

    sections.append("")
    sections.append(f"**合计**：共 {len(repo_summaries)} 个仓库，{total_commits} 次提交，{total_contracts} 个核心契约变更，{total_others} 个业务实现变更。\n")

    # 3. High-Priority Contract Index
    sections.append("## 三、 核心协议与契约变更索引（高危审查靶区）")
    has_any_contract = False
    for rs in repo_summaries:
        if rs.contract_files:
            has_any_contract = True
            sections.append(f"### 仓 `{rs.repo_id}` 契约文件（{len(rs.contract_files)} 个）：")
            for cf in rs.contract_files:
                sections.append(f"- `{cf}`")
            sections.append("")

    if not has_any_contract:
        sections.append("> 无显式 DTO / Schema / API 契约文件名变更。\n")

    has_other_files = any(bool(rs.other_files) for rs in repo_summaries)
    if has_other_files:
        sections.append("### 其他业务实现文件变更清单（不展开 Diff 以免消耗大量 Token）：")
        for rs in repo_summaries:
            if rs.other_files:
                sections.append(f"**仓 `{rs.repo_id}`（{len(rs.other_files)} 个文件）**：")
                for of in rs.other_files[:60]:
                    sections.append(f"- `{of}`")
                if len(rs.other_files) > 60:
                    sections.append(f"- ... (省略剩余 {len(rs.other_files) - 60} 个文件)")
                sections.append("")

    # 4. Recommended Review Seats
    sections.append("## 四、 分席位独立审查建议（阻断上下文二次方膨胀）")
    sections.append("> 💡 **审查指南**：不要把整个大包塞进同一个 AI 会话。推荐使用独立对话分别委派以下三个席位，各席位完成审查后向主会话仅汇总结论：\n")
    sections.append(
        "1. **`Reviewer-Java` 席位**：专注于 `common-msv` 仓的 DDL、业务事务回滚、鉴权注解与对外接口边界。\n"
        "2. **`Reviewer-Python` 席位**：专注于 `ai-agent` 仓的模型路由、Prompt 构造、Pydantic Schema 校验与供应商容灾。\n"
        "3. **`Reviewer-Contract` 席位**：专注于 Java DTO ↔ Python Schema ↔ 前端（小程序/PC）实时态与历史态消费者对齐。\n"
    )

    # 5. Compact Code Diffs
    if scope != "summary":
        sections.append("## 五、 增量代码 Diff（Compact Diff）")
        for rs in repo_summaries:
            if not rs.file_diffs:
                continue
            sections.append(f"### 仓库 `{rs.repo_id}`")
            if rs.commits_log:
                sections.append("<details><summary>最近提交记录（点击展开）</summary>\n")
                sections.append(f"```text\n{rs.commits_log}\n```\n</details>\n")

            # Contract diffs first
            if rs.contract_files:
                sections.append(f"#### 1. 契约与协议代码 Diff（{len(rs.contract_files)} 个）")
                for cf in rs.contract_files:
                    diff_text = rs.file_diffs.get(cf, "")
                    if diff_text.strip():
                        sections.append(f"**文件：`{cf}`**")
                        sections.append(f"```diff\n{diff_text.strip()}\n```\n")

            # Other diffs
            if scope == "all" and rs.other_files:
                sections.append(f"#### 2. 业务实现代码 Diff（{len(rs.other_files)} 个）")
                for of in rs.other_files:
                    diff_text = rs.file_diffs.get(of, "")
                    if diff_text.strip():
                        sections.append(f"<details><summary><code>{of}</code>（点击展开 Diff）</summary>\n")
                        sections.append(f"```diff\n{diff_text.strip()}\n```\n</details>\n")

    return "\n".join(sections)

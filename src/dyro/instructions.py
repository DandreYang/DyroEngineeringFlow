"""Thin overlay instruction files for coding harnesses.

These markdown files are pointers, not policy. Host-compiled SKILL.md and
``dyro next`` remain the gate. They live on the Dyro overlay (next to
``dyro.toml``, or at ``versions/<line>/``), never inside product Git checkouts.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .errors import DyroError
from .process import git_read
from .state import atomic_write_text


INSTRUCTION_NAMES = ("AGENTS.md", "CLAUDE.md")
SEED_COMMAND = "dyro host seed"


def render_workspace_overlay() -> str:
    return """# Dyro overlay

这是 Dyro 工作区 overlay（与 `dyro.toml` 同级），不是产品 Git 仓库。

本文件不是权威。权威入口是：
- 宿主编译的 SKILL.md（`dyro host compile`）
- `dyro next`

不要绕过门禁做 `git merge` / `git push`。合入与推送只走 `dyro`（例如 `dyro task merge`、`dyro line merge`；push 还需 `--push` 且 `policy.allow_push`）。

下一步：运行 `dyro next`。
"""


def render_line_persona(*, line_id: str, branch: str) -> str:
    remote = f"origin/{branch}"
    return f"""# Dyro 开发线 {line_id}

继承工作区 overlay 约束：这里仍不是产品仓库。权威是 SKILL.md 与 `dyro next`。不要绕过门禁 merge/push。

当前线：
- id：{line_id}
- 分支：{branch}
- 必须跟踪 `{remote}`

下一步：运行 `dyro next`。
"""


@dataclass(frozen=True)
class SeedOutcome:
    written: tuple[str, ...]
    skipped: tuple[str, ...]


def missing_workspace_overlay_files(root: Path) -> tuple[str, ...]:
    return tuple(name for name in INSTRUCTION_NAMES if not (root / name).is_file())


def overlay_instruction_warning(root: Path) -> str | None:
    missing = missing_workspace_overlay_files(root)
    if not missing:
        return None
    joined = " 与 ".join(missing)
    return f"WARN overlay 缺少 {joined}；运行 {SEED_COMMAND}"


def _inside_git_checkout(path: Path) -> bool:
    result = git_read(path, "rev-parse", "--is-inside-work-tree")
    return result.code == 0 and result.stdout.strip() == "true"


def seed_overlay_files(
    directory: Path,
    body: str,
    *,
    force: bool = False,
    dry_run: bool = False,
    refuse_git: bool = False,
) -> SeedOutcome:
    """Write AGENTS.md and CLAUDE.md with the same body. Never overwrite unless force."""
    if _inside_git_checkout(directory):
        if refuse_git:
            raise DyroError(
                "目标目录是 Git 工作区，拒绝写入 AGENTS.md/CLAUDE.md"
            )
        return SeedOutcome((), INSTRUCTION_NAMES)
    written: list[str] = []
    skipped: list[str] = []
    for name in INSTRUCTION_NAMES:
        path = directory / name
        if path.exists() and not force:
            skipped.append(name)
            continue
        if path.exists() and not path.is_file():
            raise DyroError(f"无法写入 overlay 说明：{name} 已存在且不是普通文件")
        if not dry_run:
            directory.mkdir(parents=True, exist_ok=True)
            atomic_write_text(path, body if body.endswith("\n") else body + "\n")
        written.append(name)
    return SeedOutcome(tuple(written), tuple(skipped))


def seed_workspace_overlay(
    root: Path,
    *,
    force: bool = False,
    dry_run: bool = False,
    refuse_git: bool = False,
) -> SeedOutcome:
    return seed_overlay_files(
        root,
        render_workspace_overlay(),
        force=force,
        dry_run=dry_run,
        refuse_git=refuse_git,
    )


def seed_line_overlay(
    overlay: Path,
    *,
    line_id: str,
    branch: str,
    force: bool = False,
    dry_run: bool = False,
) -> SeedOutcome:
    return seed_overlay_files(
        overlay,
        render_line_persona(line_id=line_id, branch=branch),
        force=force,
        dry_run=dry_run,
        refuse_git=False,
    )


def seed_configured_overlays(
    config,
    *,
    force: bool = False,
    dry_run: bool = False,
) -> tuple[SeedOutcome, tuple[tuple[str, SeedOutcome], ...]]:
    """Seed workspace overlay files and missing line personas. Existing files stay."""
    from .workspace import line_root, list_lines

    workspace = seed_workspace_overlay(
        config.root, force=force, dry_run=dry_run, refuse_git=True
    )
    lines: list[tuple[str, SeedOutcome]] = []
    for line in list_lines(config):
        outcome = seed_line_overlay(
            line_root(config, line),
            line_id=line.id,
            branch=line.branch,
            force=force,
            dry_run=dry_run,
        )
        lines.append((line.id, outcome))
    return workspace, tuple(lines)

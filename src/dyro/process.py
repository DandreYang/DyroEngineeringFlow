from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
from typing import Iterable

from .errors import DyroError


@dataclass(frozen=True)
class Result:
    argv: tuple[str, ...]
    code: int
    stdout: str


def run(
    argv: Iterable[str],
    *,
    cwd: Path | None = None,
    timeout: int | None = None,
    dry_run: bool = False,
) -> Result:
    """Run an argument vector without a shell.

    DyroEngineeringFlow deliberately stores executable commands as string arrays.  This
    avoids treating a project manifest as shell source and makes the exact
    executed command auditable.
    """
    args = tuple(str(item) for item in argv)
    if not args:
        raise DyroError("拒绝执行空命令")
    if dry_run:
        return Result(args, 0, "")
    try:
        completed = subprocess.run(
            args,
            cwd=cwd,
            timeout=timeout,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
    except FileNotFoundError as exc:
        raise DyroError(f"找不到可执行命令：{args[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise DyroError(f"命令超时（{timeout}s）：{' '.join(args)}") from exc
    return Result(args, completed.returncode, completed.stdout or "")


def require_ok(result: Result, context: str) -> Result:
    if result.code != 0:
        output = result.stdout.strip()
        detail = f"\n{output}" if output else ""
        raise DyroError(f"{context} 失败（退出码 {result.code}）：{' '.join(result.argv)}{detail}")
    return result


def git(repo: Path, *args: str, dry_run: bool = False, timeout: int = 180) -> Result:
    return run(("git", "-C", str(repo), *args), timeout=timeout, dry_run=dry_run)

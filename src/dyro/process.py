from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
import threading
from typing import Iterable

from .errors import DyroError
from .read_limits import ReadBudget, ReadLimitCode, ReadLimitError


@dataclass(frozen=True)
class Result:
    argv: tuple[str, ...]
    code: int
    stdout: str
    output_bytes: int = 0


def _run_with_bounded_output(
    args: tuple[str, ...],
    *,
    cwd: Path | None,
    timeout: float | None,
    maximum_output_bytes: int,
) -> Result:
    if maximum_output_bytes < 1:
        raise ReadLimitError(
            ReadLimitCode.AGGREGATE_BYTES_EXCEEDED,
            "No observation output budget remains",
        )
    try:
        process = subprocess.Popen(
            args,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except FileNotFoundError as exc:
        raise DyroError(f"找不到可执行命令：{args[0]}") from exc

    assert process.stdout is not None
    captured = bytearray()
    output_exceeded = threading.Event()

    def drain() -> None:
        while True:
            chunk = process.stdout.read(64 * 1024)
            if not chunk:
                return
            remaining = maximum_output_bytes - len(captured)
            if len(chunk) > remaining:
                if remaining > 0:
                    captured.extend(chunk[:remaining])
                output_exceeded.set()
                process.kill()
                return
            captured.extend(chunk)

    reader = threading.Thread(target=drain, name="dyro-output-reader", daemon=True)
    reader.start()
    try:
        code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.wait()
        process.stdout.close()
        reader.join(timeout=1)
        raise ReadLimitError(
            ReadLimitCode.DEADLINE_EXCEEDED,
            "Observation subprocess deadline exceeded",
        ) from exc
    reader.join(timeout=1)
    if reader.is_alive():
        process.stdout.close()
        reader.join(timeout=1)
    if reader.is_alive():
        raise DyroError(f"无法完成命令输出读取：{' '.join(args)}")
    process.stdout.close()
    if output_exceeded.is_set():
        raise ReadLimitError(
            ReadLimitCode.AGGREGATE_BYTES_EXCEEDED,
            "Observation subprocess output exceeds the remaining byte budget",
        )
    content = bytes(captured)
    return Result(
        args,
        code,
        content.decode("utf-8", errors="replace"),
        len(content),
    )


def run(
    argv: Iterable[str],
    *,
    cwd: Path | None = None,
    timeout: float | None = None,
    dry_run: bool = False,
    maximum_output_bytes: int | None = None,
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
    if maximum_output_bytes is not None:
        return _run_with_bounded_output(
            args,
            cwd=cwd,
            timeout=timeout,
            maximum_output_bytes=maximum_output_bytes,
        )
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


def git_read(
    repo: Path,
    *args: str,
    dry_run: bool = False,
    timeout: float = 180,
    read_budget: ReadBudget | None = None,
) -> Result:
    """Run a Git observation without optional locks or index refresh writes."""
    bounded_timeout = timeout
    maximum_output_bytes = None
    if read_budget is not None:
        bounded_timeout = min(timeout, read_budget.remaining_seconds())
        maximum_output_bytes = read_budget.remaining_bytes
    result = run(
        ("git", "--no-optional-locks", "-C", str(repo), *args),
        timeout=bounded_timeout,
        dry_run=dry_run,
        maximum_output_bytes=maximum_output_bytes,
    )
    if read_budget is not None:
        read_budget.charge_bytes(result.output_bytes)
    return result

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tempfile
from typing import Iterator, TYPE_CHECKING
import zipfile

from .config import Config, expand_argv
from .errors import DyroError, ValidationError
from .process import git, require_ok, run
from .state import atomic_write_bytes

if TYPE_CHECKING:
    from .tasks import Task


MAX_BUNDLE_MEMBERS = 128
MAX_BUNDLE_BYTES = 50 * 1024 * 1024


@dataclass(frozen=True)
class ExecutionBundle:
    result: str
    output: Path
    gates_passed: bool


def _receipt_result(receipt: Path) -> tuple[str, bytes]:
    if not receipt.is_file():
        raise DyroError(f"外部回执文件不存在：{receipt}")
    content = receipt.read_bytes()
    try:
        first_line = content.decode("utf-8").splitlines()[0]
    except (UnicodeDecodeError, IndexError) as exc:
        raise ValidationError("外部回执首行必须是 result: DONE、BLOCKED 或 QUESTION") from exc
    normalized = first_line.strip().upper()
    if normalized not in ("RESULT: DONE", "RESULT: BLOCKED", "RESULT: QUESTION"):
        raise ValidationError("外部回执首行必须是 result: DONE、BLOCKED 或 QUESTION")
    return normalized.partition(": ")[2], content


def _external_repository(config: Config, task: Task, workspace: Path, repository_id: str) -> Path:
    path = (workspace / config.repositories[repository_id].mount).resolve()
    try:
        path.relative_to(workspace)
    except ValueError as exc:
        raise ValidationError(f"外部执行工作区仓库越界：{path}") from exc
    if git(path, "rev-parse", "--is-inside-work-tree").stdout.strip() != "true":
        raise DyroError(f"外部执行仓库不是 Git 工作树：{path}")
    top_level = require_ok(git(path, "rev-parse", "--show-toplevel"), f"读取外部仓库根目录 {repository_id}").stdout.strip()
    if Path(top_level).resolve() != path:
        raise DyroError(f"外部执行仓库根目录错误：{path} 实际为 {top_level}")
    expected_branch = f"{config.policy.task_branch_prefix}{task.id}"
    branch = require_ok(git(path, "branch", "--show-current"), f"读取外部仓库分支 {repository_id}").stdout.strip()
    if branch != expected_branch:
        raise DyroError(f"外部执行仓库分支错误：{path} 当前 {branch or 'DETACHED'}，期望 {expected_branch}")
    return path


def _collect_external_heads(config: Config, task: Task, workspace: Path) -> dict[str, str]:
    heads: dict[str, str] = {}
    for repository_id in task.repositories:
        path = _external_repository(config, task, workspace, repository_id)
        dirty = require_ok(git(path, "status", "--porcelain=v1", "-uall"), f"读取外部仓库状态 {repository_id}").stdout.strip()
        if dirty:
            raise DyroError(f"外部执行仓库不干净，必须先提交全部改动：{path}")
        heads[repository_id] = require_ok(git(path, "rev-parse", "HEAD"), f"读取外部仓库 HEAD {repository_id}").stdout.strip()
    return heads


def _task_heads_bytes(config: Config, task: Task, heads: dict[str, str]) -> bytes:
    payload = {
        "schema_version": 1,
        "task_id": task.id,
        "line": task.line,
        "branch": f"{config.policy.task_branch_prefix}{task.id}",
        "repositories": heads,
    }
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _gate_artifacts(config: Config, task: Task, workspace: Path, output_dir: Path, *, dry_run: bool) -> tuple[bytes, bool]:
    receipt_path = output_dir / "receipt.md"
    receipt_hash = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
    entries: list[dict[str, object]] = []
    passed = True
    logs_dir = output_dir / "gates"
    for index, gate in enumerate(task.gates, start=1):
        cwd = (workspace / gate.cwd).resolve()
        try:
            cwd.relative_to(workspace)
        except ValueError as exc:
            raise ValidationError(f"外部门禁工作目录越界：{gate.cwd}") from exc
        argv = expand_argv(gate.argv, workspace=workspace, root=config.root, task=task.id, line=task.line)
        result = run(argv, cwd=cwd, timeout=gate.timeout_seconds, dry_run=dry_run)
        log_name = f"gate-{index}.log"
        log_path = logs_dir / log_name
        if not dry_run:
            atomic_write_bytes(log_path, result.stdout.encode("utf-8"))
        log_bytes = result.stdout.encode("utf-8")
        entries.append(
            {
                "name": gate.name,
                "exit_code": result.code,
                "log": f"gates/{log_name}",
                "log_sha256": hashlib.sha256(log_bytes).hexdigest(),
            }
        )
        passed = passed and result.code == 0
    payload = {
        "schema_version": 1,
        "task_id": task.id,
        "receipt_sha256": receipt_hash,
        "gates": entries,
    }
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"), passed


def _write_bundle(source: Path, output: Path) -> None:
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DyroError(
            f"无法创建证据包输出目录：{output.parent}"
        ) from exc
    temporary = ""
    try:
        with tempfile.NamedTemporaryFile(prefix=f".{output.name}.", suffix=".zip", dir=output.parent, delete=False) as handle:
            temporary = handle.name
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for path in sorted(source.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(source).as_posix())
        with Path(temporary).open("rb") as handle:
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        try:
            os.link(temporary, output, follow_symlinks=False)
        except FileExistsError as exc:
            raise DyroError(f"拒绝覆盖已有证据包：{output}") from exc
        except OSError as exc:
            raise DyroError(
                "无法原子发布证据包；输出目录必须支持同文件系统硬链接："
                f"{output.parent}"
            ) from exc
        try:
            directory_flags = os.O_RDONLY | getattr(
                os,
                "O_DIRECTORY",
                0,
            )
            directory_descriptor = os.open(output.parent, directory_flags)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except OSError:
            # The file itself is already durable. Some platforms/filesystems
            # do not support directory fsync.
            pass
    except DyroError:
        raise
    except (OSError, zipfile.BadZipFile) as exc:
        raise DyroError(f"构建证据包失败：{output}") from exc
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)


def build_execution_bundle(
    config: Config,
    task: Task,
    *,
    workspace: Path,
    receipt: Path,
    output: Path,
    signing_key: Path | None = None,
    key_id: str | None = None,
    claim: Path | None = None,
    dry_run: bool = False,
) -> ExecutionBundle:
    """Package the receipt, gate logs, and exact HEADs generated in an isolated runner."""
    if config.policy.execution_mode != "external":
        raise DyroError("证据包仅用于 execution_mode = external 的 Profile")
    workspace = workspace.expanduser().resolve()
    if not workspace.is_dir():
        raise DyroError(f"外部执行工作区不存在：{workspace}")
    requested_output = output.expanduser()
    if requested_output.exists() or requested_output.is_symlink():
        raise DyroError(f"拒绝覆盖已有证据包：{requested_output}")
    output = requested_output.parent.resolve() / requested_output.name
    if output.exists() or output.is_symlink():
        raise DyroError(f"拒绝覆盖已有证据包：{output}")
    if (signing_key is None) != (key_id is None):
        raise ValidationError("--signing-key 与 --key-id 必须同时提供")
    claim_binding: dict[str, object] | None = None
    if getattr(config.policy, "require_signed_execution", False):
        if signing_key is None or key_id is None:
            raise ValidationError("策略要求 evidence build 提供 --signing-key 与 --key-id")
        from .tasks import execution_claim_binding

        claim_binding = execution_claim_binding(
            task,
            claim_file=claim.expanduser().resolve() if claim is not None else None,
        )
        if claim_binding["execution_key_id"] != key_id:
            raise ValidationError("evidence signing key ID 与 claim execution_key_id 不匹配")
    result, receipt_bytes = _receipt_result(receipt.expanduser().resolve())
    if dry_run:
        return ExecutionBundle("dry-run", output, True)

    with tempfile.TemporaryDirectory(prefix="dyro-evidence-") as temporary:
        staging = Path(temporary)
        atomic_write_bytes(staging / "receipt.md", receipt_bytes)
        gates_passed = True
        gates_bytes = b""
        task_heads_bytes = b""
        if result == "DONE":
            gates_bytes, gates_passed = _gate_artifacts(config, task, workspace, staging, dry_run=False)
            atomic_write_bytes(staging / "gates.json", gates_bytes)
            task_heads_bytes = _task_heads_bytes(config, task, _collect_external_heads(config, task, workspace))
            atomic_write_bytes(staging / "task-heads.json", task_heads_bytes)
        from .provenance import build_external_attempt_record, external_execution_plan

        provenance = build_external_attempt_record(
            task.directory,
            task.id,
            external_execution_plan(
                task,
                config.policy.execution_mode,
                claim_binding=claim_binding,
            ),
            result=result,
            receipt_sha256=hashlib.sha256(receipt_bytes).hexdigest(),
            gates_sha256=hashlib.sha256(gates_bytes).hexdigest() if gates_bytes else "",
            task_heads_sha256=hashlib.sha256(task_heads_bytes).hexdigest() if task_heads_bytes else "",
        )
        if signing_key is not None and key_id is not None:
            from .signing import sign_record

            provenance = sign_record(
                provenance,
                purpose="execution",
                key_id=key_id,
                private_key=signing_key.expanduser().resolve(),
            )
        atomic_write_bytes(
            staging / "provenance.json",
            json.dumps(provenance, ensure_ascii=False, indent=2).encode("utf-8") + b"\n",
        )
        _write_bundle(staging, output)
    return ExecutionBundle(result, output, gates_passed)


def _safe_bundle_member(info: zipfile.ZipInfo) -> PurePosixPath:
    if "\\" in info.filename:
        raise ValidationError(f"证据包路径必须使用 POSIX 分隔符：{info.filename!r}")
    path = PurePosixPath(info.filename)
    if not info.filename or path.is_absolute() or ".." in path.parts or path.parts[0] in ("", "."):
        raise ValidationError(f"证据包包含不安全路径：{info.filename!r}")
    mode = info.external_attr >> 16
    if stat.S_ISLNK(mode):
        raise ValidationError(f"证据包不允许符号链接：{info.filename!r}")
    allowed = (
        path == PurePosixPath("receipt.md")
        or path == PurePosixPath("gates.json")
        or path == PurePosixPath("task-heads.json")
        or path == PurePosixPath("provenance.json")
    )
    allowed = allowed or (len(path.parts) == 2 and path.parts[0] == "gates" and path.suffix == ".log")
    if not allowed:
        raise ValidationError(f"证据包包含未声明文件：{info.filename!r}")
    return path


@contextmanager
def unpack_execution_bundle(bundle: Path) -> Iterator[dict[str, Path]]:
    """Safely materialize a portable evidence ZIP for the existing importer."""
    bundle = bundle.expanduser().resolve()
    if not bundle.is_file():
        raise DyroError(f"证据包不存在：{bundle}")
    try:
        archive = zipfile.ZipFile(bundle)
    except zipfile.BadZipFile as exc:
        raise ValidationError(f"证据包不是有效 ZIP：{bundle}") from exc
    with archive:
        infos = archive.infolist()
        if not infos or len(infos) > MAX_BUNDLE_MEMBERS:
            raise ValidationError("证据包文件数量非法或超过限制")
        if sum(info.file_size for info in infos) > MAX_BUNDLE_BYTES:
            raise ValidationError("证据包解压后超过大小限制")
        names: set[str] = set()
        safe_infos: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
        for info in infos:
            path = _safe_bundle_member(info)
            if path.as_posix() in names:
                raise ValidationError(f"证据包包含重复文件：{path}")
            names.add(path.as_posix())
            safe_infos.append((info, path))
        if "receipt.md" not in names:
            raise ValidationError("证据包缺少 receipt.md")
        with tempfile.TemporaryDirectory(prefix="dyro-evidence-import-") as temporary:
            destination = Path(temporary)
            destination_resolved = destination.resolve()
            for info, path in safe_infos:
                target = (destination / Path(path.as_posix())).resolve()
                try:
                    target.relative_to(destination_resolved)
                except ValueError as exc:
                    raise ValidationError(f"证据包路径越出解压目录：{info.filename!r}") from exc
                target.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_bytes(target, archive.read(info))
            yield {
                "receipt": destination / "receipt.md",
                "gates": destination / "gates.json",
                "heads": destination / "task-heads.json",
                "provenance": destination / "provenance.json",
            }

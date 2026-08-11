"""Objective event ledger, projections, and crash recovery.

This module owns only durable storage mechanics.  Scope derivation, ownership
rules, and lifecycle decisions remain in :mod:`dyro.continuation.store`.
"""

from __future__ import annotations

import hashlib
import io
import json
from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import stat
from typing import Iterator

from ..canonical import canonical_json_bytes
from ..config import Config, validate_id
from ..errors import DyroError, ValidationError
from ..read_limits import ReadBudget, ReadLimitCode, ReadLimitError
from ..state import open_safe_child_directory, open_safe_directory
from .models import Objective, RequestedMode


OBJECTIVE_STORE_SCHEMA_VERSION = 1
EVENT_SCHEMA_VERSION = 1
OPERATOR_STATES = frozenset({"active", "paused", "stopped"})
_PENDING_FILE = "pending.json"


@dataclass(frozen=True)
class StoredObjective:
    objective: Objective
    revision: int
    operator_state: str
    scope: tuple[str, ...]
    task_contract_sha256: tuple[tuple[str, str], ...]
    scope_sha256: str
    contract_sha256: str
    event_seq: int
    event_sha256: str

    @property
    def owns_mutation_scope(self) -> bool:
        return (
            self.operator_state == "active"
            and self.objective.requested_mode != RequestedMode.OBSERVE
        )


@dataclass(frozen=True)
class ObjectiveDirectory:
    """A stable Objective directory identity held by an open descriptor."""

    path: Path
    fd: int
    parent_fd: int
    name: str


@contextmanager
def open_objective_directory(
    config: Config,
    objective_id: str,
    *,
    create: bool = False,
    budget: ReadBudget | None = None,
) -> Iterator[ObjectiveDirectory]:
    """Open one Objective directory without re-resolving mutable state paths.

    Windows cannot provide an equivalent no-reparse-point ``openat`` primitive
    in this implementation, so Objective persistence fails closed there until
    a platform-native safe traversal is available.
    """
    if os.name == "nt":
        raise DyroError(
            "Windows 暂不支持安全的 Objective 持久化；拒绝写入以避免 reparse-point 路径逃逸"
        )
    if not hasattr(os, "O_NOFOLLOW"):
        raise DyroError(
            "当前平台缺少安全的 Objective 持久化能力；拒绝访问以避免路径逃逸"
        )
    validate_id(objective_id, "Objective ID")
    if budget is not None:
        if create:
            raise ValidationError("bounded Objective read 不允许创建状态目录")
        with budget.open_safe_directory_chain(
            config.root, config.objectives_dir
        ) as parent_fd:
            assert parent_fd is not None
            with budget.open_safe_directory_chain(
                config.root, config.objectives_dir / objective_id
            ) as objective_fd:
                assert objective_fd is not None
                yield ObjectiveDirectory(
                    config.objectives_dir / objective_id,
                    objective_fd,
                    parent_fd,
                    objective_id,
                )
        return
    workspace_fd = open_safe_directory(config.root)
    dyro_fd: int | None = None
    objectives_fd: int | None = None
    objective_fd: int | None = None
    try:
        dyro_fd = open_safe_child_directory(workspace_fd, ".dyro", create=create)
        objectives_fd = open_safe_child_directory(dyro_fd, "objectives", create=create)
        if create:
            try:
                os.mkdir(objective_id, mode=0o700, dir_fd=objectives_fd)
            except FileExistsError as exc:
                raise DyroError(f"Objective 已存在：{objective_id}") from exc
            os.fsync(objectives_fd)
        objective_fd = open_safe_child_directory(objectives_fd, objective_id)
        yield ObjectiveDirectory(
            config.objectives_dir / objective_id,
            objective_fd,
            objectives_fd,
            objective_id,
        )
    finally:
        if objective_fd is not None:
            os.close(objective_fd)
        if objectives_fd is not None:
            os.close(objectives_fd)
        if dyro_fd is not None:
            os.close(dyro_fd)
        os.close(workspace_fd)


def list_objective_ids(config: Config) -> tuple[str, ...]:
    """Return only verified Objective directory names from a stable root FD."""
    if os.name == "nt":
        raise DyroError(
            "Windows 暂不支持安全的 Objective 持久化；拒绝访问以避免 reparse-point 路径逃逸"
        )
    if not hasattr(os, "O_NOFOLLOW"):
        raise DyroError(
            "当前平台缺少安全的 Objective 持久化能力；拒绝访问以避免路径逃逸"
        )
    workspace_fd = open_safe_directory(config.root)
    dyro_fd: int | None = None
    objectives_fd: int | None = None
    try:
        try:
            dyro_fd = open_safe_child_directory(workspace_fd, ".dyro")
        except DyroError:
            parent = config.root / ".dyro"
            if not parent.exists() and not parent.is_symlink():
                return ()
            raise
        try:
            objectives_fd = open_safe_child_directory(dyro_fd, "objectives")
        except DyroError:
            root = config.objectives_dir
            if not root.exists() and not root.is_symlink():
                return ()
            raise
        result: list[str] = []
        for name in os.listdir(objectives_fd):
            if name == "objectives.lock":
                continue
            if name.startswith("."):
                raise ValidationError(
                    f"Objective 根目录包含未知状态文件：{config.objectives_dir / name}"
                )
            try:
                validate_id(name, "Objective ID")
                info = os.stat(name, dir_fd=objectives_fd, follow_symlinks=False)
            except (OSError, ValidationError) as exc:
                raise ValidationError(
                    f"Objective 根目录包含不安全条目：{config.objectives_dir / name}"
                ) from exc
            if stat.S_ISLNK(info.st_mode):
                raise ValidationError(
                    f"Objective 根目录包含符号链接：{config.objectives_dir / name}"
                )
            if not stat.S_ISDIR(info.st_mode):
                raise ValidationError(
                    f"Objective 根目录包含不安全条目：{config.objectives_dir / name}"
                )
            result.append(name)
        return tuple(sorted(result))
    finally:
        if objectives_fd is not None:
            os.close(objectives_fd)
        if dyro_fd is not None:
            os.close(dyro_fd)
        os.close(workspace_fd)


def _json_bytes(value: object) -> bytes:
    return canonical_json_bytes(value)


def _sha256(value: object) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _directory_path(directory: ObjectiveDirectory) -> Path:
    return directory.path


def _checked_name(name: str) -> str:
    if not name or Path(name).name != name:
        raise ValidationError(f"Objective 状态文件名非法：{name!r}")
    return name


def _fd_flags(flags: int) -> int:
    return flags | (os.O_NOFOLLOW if hasattr(os, "O_NOFOLLOW") else 0)


def _read_all(descriptor: int, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = os.read(descriptor, remaining)
        if not chunk:
            raise ValidationError("Objective 状态文件读取中断")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise DyroError("Objective 状态文件写入中断")
        view = view[written:]


def _read_file(
    directory: ObjectiveDirectory,
    name: str,
    label: str,
    *,
    budget: ReadBudget | None = None,
    maximum_bytes: int | None = None,
) -> bytes:
    name = _checked_name(name)
    try:
        descriptor = os.open(
            name,
            _fd_flags(os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)),
            dir_fd=directory.fd,
        )
    except PermissionError:
        raise
    except OSError as exc:
        raise ValidationError(f"无法安全读取 {label}：{directory.path / name}") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValidationError(
                f"{label} 必须是安全的普通文件：{directory.path / name}"
            )
        if budget is not None:
            if maximum_bytes is None:
                raise ValidationError("bounded Objective read 缺少 maximum_bytes")
            return budget.read_descriptor_bytes(
                descriptor,
                size=info.st_size,
                maximum_bytes=maximum_bytes,
                label=label,
            )
        return _read_all(descriptor, info.st_size)
    finally:
        os.close(descriptor)


def _file_exists(directory: ObjectiveDirectory, name: str) -> bool:
    try:
        info = os.stat(_checked_name(name), dir_fd=directory.fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except PermissionError:
        raise
    except OSError as exc:
        raise DyroError(
            f"无法读取 Objective 状态文件：{directory.path / name}"
        ) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValidationError(
            f"Objective 状态文件必须是安全的普通文件：{directory.path / name}"
        )
    return True


def _create_file(directory: ObjectiveDirectory, name: str, content: bytes) -> None:
    name = _checked_name(name)
    try:
        descriptor = os.open(
            _checked_name(name),
            _fd_flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL),
            0o600,
            dir_fd=directory.fd,
        )
    except FileExistsError as exc:
        raise DyroError(f"拒绝覆盖已存在的状态文件：{directory.path / name}") from exc
    except OSError as exc:
        raise DyroError(f"无法安全创建状态文件：{directory.path / name}") from exc
    try:
        _write_all(descriptor, content)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.fsync(directory.fd)


def _append_file(directory: ObjectiveDirectory, name: str, content: bytes) -> None:
    name = _checked_name(name)
    try:
        descriptor = os.open(
            name, _fd_flags(os.O_WRONLY | os.O_APPEND), dir_fd=directory.fd
        )
    except OSError as exc:
        raise DyroError(
            f"无法安全追加 Objective 事件日志：{directory.path / name}"
        ) from exc
    try:
        _write_all(descriptor, content)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_replace_file(
    directory: ObjectiveDirectory, name: str, content: bytes
) -> None:
    name = _checked_name(name)
    temporary = f".{name}.{os.getpid()}.{os.urandom(8).hex()}"
    try:
        descriptor = os.open(
            temporary,
            _fd_flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL),
            0o600,
            dir_fd=directory.fd,
        )
        try:
            _write_all(descriptor, content)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, name, src_dir_fd=directory.fd, dst_dir_fd=directory.fd)
        os.fsync(directory.fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory.fd)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise DyroError(
                f"无法清理 Objective 临时状态文件：{directory.path / temporary}"
            ) from exc


def _remove_file(directory: ObjectiveDirectory, name: str, label: str) -> None:
    if not _file_exists(directory, name):
        return
    try:
        os.unlink(_checked_name(name), dir_fd=directory.fd)
    except OSError as exc:
        raise DyroError(f"无法清理 {label}：{directory.path / name}") from exc
    os.fsync(directory.fd)


def event_hash(event: dict[str, object]) -> str:
    payload = dict(event)
    payload.pop("sha256", None)
    return _sha256(payload)


def _validate_event(
    event: object, *, expected_seq: int, previous: str, path: Path
) -> dict[str, object]:
    if not isinstance(event, dict) or set(event) != {
        "schema_version",
        "seq",
        "event",
        "previous_sha256",
        "record",
        "sha256",
    }:
        raise ValidationError(f"Objective 事件结构无效：{path}")
    if (
        type(event.get("schema_version")) is not int
        or event.get("schema_version") != EVENT_SCHEMA_VERSION
        or type(event.get("seq")) is not int
        or event.get("seq") != expected_seq
    ):
        raise ValidationError(f"Objective 事件 seq 无效：{path}")
    if (
        not isinstance(event.get("event"), str)
        or event.get("previous_sha256") != previous
    ):
        raise ValidationError(f"Objective 事件链无效：{path}")
    digest = event.get("sha256")
    if not isinstance(digest, str) or digest != event_hash(event):
        raise ValidationError(f"Objective 事件哈希分叉：{path}")
    return event


def _validate_event_bounded(
    event: object, *, expected_seq: int, previous: str, path: Path
) -> dict[str, object]:
    try:
        return _validate_event(
            event,
            expected_seq=expected_seq,
            previous=previous,
            path=path,
        )
    except RecursionError as exc:
        raise ValidationError(f"Objective 事件结构过深：{path}") from exc


def read_events(
    directory: ObjectiveDirectory,
    *,
    allow_empty: bool = False,
    budget: ReadBudget | None = None,
) -> tuple[dict[str, object], ...]:
    event_path = _directory_path(directory) / "events.jsonl"
    if not _file_exists(directory, "events.jsonl") and allow_empty:
        return ()
    try:
        raw = _read_file(
            directory,
            "events.jsonl",
            "Objective 事件日志",
            budget=budget,
            maximum_bytes=(budget.limits.objective_events_bytes if budget else None),
        ).decode("utf-8")
    except UnicodeError as exc:
        raise ValidationError(f"无法读取 Objective 事件日志：{event_path}") from exc
    if not raw and allow_empty:
        return ()
    if not raw.endswith("\n"):
        raise ValidationError(f"Objective 事件日志断尾：{event_path}")
    events: list[dict[str, object]] = []
    previous = ""
    for expected_seq, line in enumerate(io.StringIO(raw), start=1):
        if budget is not None:
            budget.check_deadline()
            if expected_seq > budget.limits.objective_event_records:
                raise ReadLimitError(
                    ReadLimitCode.RECORD_LIMIT_EXCEEDED,
                    "Objective event record limit exceeded",
                )
        line = line.removesuffix("\n")
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise ValidationError(
                f"Objective 事件日志 JSON 无效：{event_path}"
            ) from exc
        verified = _validate_event_bounded(
            event,
            expected_seq=expected_seq,
            previous=previous,
            path=event_path,
        )
        previous = str(verified["sha256"])
        events.append(verified)
        if budget is not None:
            budget.check_deadline()
    if not events:
        if allow_empty:
            return ()
        raise ValidationError(f"Objective 事件日志不能为空：{event_path}")
    return tuple(events)


def _event_for(record: StoredObjective, event_name: str) -> dict[str, object]:
    from .store import _record_payload

    event = {
        "schema_version": EVENT_SCHEMA_VERSION,
        "seq": record.event_seq + 1,
        "event": event_name,
        "previous_sha256": record.event_sha256,
        "record": _record_payload(record),
    }
    event["sha256"] = event_hash(event)
    return event


def _pending_payload(
    event: dict[str, object],
    contract_content: bytes | None,
    action_cancellation: dict[str, object] | None,
) -> dict[str, object]:
    return {
        "schema_version": OBJECTIVE_STORE_SCHEMA_VERSION,
        "event": event,
        "contract_revision": int(event["record"]["revision"]),
        "contract_sha256": (
            hashlib.sha256(contract_content).hexdigest()
            if contract_content is not None
            else ""
        ),
        "action_cancellation": action_cancellation,
    }


def read_pending(
    directory: ObjectiveDirectory, *, budget: ReadBudget | None = None
) -> dict[str, object] | None:
    path = _directory_path(directory) / _PENDING_FILE
    if not _file_exists(directory, _PENDING_FILE):
        return None
    try:
        payload = json.loads(
            _read_file(
                directory,
                _PENDING_FILE,
                "Objective pending transaction",
                budget=budget,
                maximum_bytes=(
                    budget.limits.objective_metadata_bytes if budget else None
                ),
            ).decode("utf-8")
        )
    except PermissionError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValidationError(
            f"Objective pending transaction JSON 无效：{path}"
        ) from exc
    pending_fields = (
        {"schema_version", "event", "contract_revision", "contract_sha256"},
        {
            "schema_version",
            "event",
            "contract_revision",
            "contract_sha256",
            "action_cancellation",
        },
    )
    if not isinstance(payload, dict) or set(payload) not in pending_fields:
        raise ValidationError(f"Objective pending transaction 结构无效：{path}")
    if payload.get("schema_version") != OBJECTIVE_STORE_SCHEMA_VERSION:
        raise ValidationError(f"Objective pending transaction 版本无效：{path}")
    raw_event = payload.get("event")
    if (
        not isinstance(raw_event, dict)
        or type(raw_event.get("seq")) is not int
        or raw_event["seq"] < 1
    ):
        raise ValidationError(f"Objective pending transaction event 无效：{path}")
    previous = raw_event.get("previous_sha256")
    if not isinstance(previous, str):
        raise ValidationError(
            f"Objective pending transaction previous hash 无效：{path}"
        )
    event = _validate_event_bounded(
        raw_event,
        expected_seq=raw_event["seq"],
        previous=previous,
        path=path,
    )
    event_record = event.get("record")
    if not isinstance(event_record, dict):
        raise ValidationError(f"Objective pending transaction record 无效：{path}")
    if type(payload.get("contract_revision")) is not int or payload[
        "contract_revision"
    ] != event_record.get("revision"):
        raise ValidationError(f"Objective pending transaction revision 无效：{path}")
    contract_digest = payload.get("contract_sha256")
    if not isinstance(contract_digest, str) or (
        contract_digest
        and (
            len(contract_digest) != 64
            or any(char not in "0123456789abcdef" for char in contract_digest)
        )
    ):
        raise ValidationError(
            f"Objective pending transaction contract 哈希无效：{path}"
        )
    action_cancellation = payload.get("action_cancellation")
    if action_cancellation is not None and not isinstance(action_cancellation, dict):
        raise ValidationError(
            f"Objective pending transaction Action cancellation 无效：{path}"
        )
    return payload


def _apply_pending_action_cancellation(
    directory: ObjectiveDirectory, pending: dict[str, object]
) -> None:
    action_cancellation = pending.get("action_cancellation")
    if action_cancellation is None:
        return
    from .actions import apply_action_cancellation

    apply_action_cancellation(directory, action_cancellation)


def _contract_name(revision: int) -> str:
    if type(revision) is not int or revision < 1:
        raise ValidationError("Objective revision 必须是正整数")
    return f"contract-{revision}.toml"


def recover_pending(directory: ObjectiveDirectory) -> bool:
    """Finish or roll back only the transaction explicitly recorded as pending."""
    from .store import _record_from_payload

    path = _directory_path(directory)
    pending = read_pending(directory)
    if pending is None:
        return False
    event = pending["event"]
    assert isinstance(event, dict)
    events = read_events(directory, allow_empty=True)
    previous = str(event["previous_sha256"])
    expected_sha = str(event["sha256"])
    last_sha = str(events[-1]["sha256"]) if events else ""
    if last_sha == expected_sha:
        revision = int(pending["contract_revision"])
        record = _record_from_payload(
            path,
            event["record"],
            event_seq=int(event["seq"]),
            event_sha256=expected_sha,
            contract_content=_read_file(
                directory, _contract_name(revision), "Objective contract"
            ),
        )
        _apply_pending_action_cancellation(directory, pending)
        write_projection(directory, record)
        _remove_file(directory, _PENDING_FILE, "Objective pending transaction")
        return False
    if last_sha != previous:
        raise ValidationError(f"Objective pending transaction 与事件链分叉：{path}")

    revision = int(pending["contract_revision"])
    contract_digest = str(pending["contract_sha256"])
    if contract_digest:
        name = _contract_name(revision)
        if _file_exists(directory, name):
            if (
                hashlib.sha256(
                    _read_file(directory, name, "未提交的 Objective contract")
                ).hexdigest()
                != contract_digest
            ):
                raise ValidationError(
                    f"未提交的 Objective contract 哈希不匹配：{path / name}"
                )
            _remove_file(directory, name, "未提交的 Objective contract")
    _remove_file(directory, _PENDING_FILE, "Objective pending transaction")
    if events:
        return False

    _remove_file(directory, "events.jsonl", "未提交的 Objective 事件日志")
    try:
        os.rmdir(directory.name, dir_fd=directory.parent_fd)
        os.fsync(directory.parent_fd)
    except OSError as exc:
        raise DyroError(f"无法回滚未提交的 Objective 创建：{path}") from exc
    return True


def read_stored(
    config: Config,
    objective_id: str,
    *,
    recover: bool = True,
    directory: ObjectiveDirectory | None = None,
    budget: ReadBudget | None = None,
) -> StoredObjective:
    from .store import _record_from_payload, _record_payload

    if directory is None:
        with open_objective_directory(config, objective_id, budget=budget) as opened:
            return read_stored(
                config, objective_id, recover=recover, directory=opened, budget=budget
            )
    path = directory.path
    if read_pending(directory, budget=budget) is not None:
        if not recover:
            raise DyroError(
                f"Objective 存在未完成事务：{objective_id}；dry-run 不会写入恢复状态"
            )
        if recover_pending(directory):
            raise DyroError(
                f"Objective 创建在提交前中断，已安全回滚：{objective_id}；请重试"
            )
    events = read_events(directory, budget=budget)
    final_event = events[-1]
    payload = final_event["record"]
    if not isinstance(payload, dict) or type(payload.get("revision")) is not int:
        raise ValidationError(f"Objective 事件 record 无效：{path}")
    revision = payload["revision"]
    record = _record_from_payload(
        path,
        payload,
        event_seq=int(final_event["seq"]),
        event_sha256=str(final_event["sha256"]),
        contract_content=_read_file(
            directory,
            _contract_name(revision),
            "Objective contract",
            budget=budget,
            maximum_bytes=(budget.limits.objective_metadata_bytes if budget else None),
        ),
    )
    try:
        state = json.loads(
            _read_file(
                directory,
                "state.json",
                "Objective 投影",
                budget=budget,
                maximum_bytes=(
                    budget.limits.objective_metadata_bytes if budget else None
                ),
            ).decode("utf-8")
        )
        checkpoint = json.loads(
            _read_file(
                directory,
                "checkpoint.json",
                "Objective checkpoint",
                budget=budget,
                maximum_bytes=(
                    budget.limits.objective_metadata_bytes if budget else None
                ),
            ).decode("utf-8")
        )
    except PermissionError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValidationError(f"Objective 投影或 checkpoint JSON 无效：{path}") from exc
    try:
        expected_state = _record_payload(record)
        if _json_bytes(state) != _json_bytes(expected_state):
            raise ValidationError(f"Objective 投影与事件重放不一致：{path}")
        expected_checkpoint = {
            "schema_version": OBJECTIVE_STORE_SCHEMA_VERSION,
            "event_seq": record.event_seq,
            "event_sha256": record.event_sha256,
            "state_sha256": _sha256(expected_state),
        }
        if _json_bytes(checkpoint) != _json_bytes(expected_checkpoint):
            raise ValidationError(f"Objective checkpoint 回滚或损坏：{path}")
    except RecursionError as exc:
        raise ValidationError(f"Objective 投影或 checkpoint 结构过深：{path}") from exc
    return record


def write_projection(directory: ObjectiveDirectory, record: StoredObjective) -> None:
    from .store import _record_payload

    state = _record_payload(record)
    state_bytes = (
        json.dumps(
            state, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        + b"\n"
    )
    checkpoint = {
        "schema_version": OBJECTIVE_STORE_SCHEMA_VERSION,
        "event_seq": record.event_seq,
        "event_sha256": record.event_sha256,
        "state_sha256": _sha256(state),
    }
    checkpoint_bytes = (
        json.dumps(
            checkpoint, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        + b"\n"
    )
    _atomic_replace_file(directory, "state.json", state_bytes)
    _atomic_replace_file(directory, "checkpoint.json", checkpoint_bytes)


def _create_or_validate_contract(
    directory: ObjectiveDirectory, revision: int, content: bytes
) -> None:
    name = _contract_name(revision)
    path = _directory_path(directory) / name
    if _file_exists(directory, name):
        if (
            hashlib.sha256(
                _read_file(directory, name, "Objective contract")
            ).hexdigest()
            != hashlib.sha256(content).hexdigest()
        ):
            raise ValidationError(f"Objective contract 已存在但内容哈希不同：{path}")
        return
    _create_file(directory, name, content)


def commit_event(
    directory: ObjectiveDirectory,
    record: StoredObjective,
    event_name: str,
    *,
    contract_content: bytes | None = None,
    action_cancellation: dict[str, object] | None = None,
) -> StoredObjective:
    """Commit an event with a durable intent record before any new contract."""
    path = _directory_path(directory)
    event = _event_for(record, event_name)
    pending = _pending_payload(event, contract_content, action_cancellation)
    if _file_exists(directory, _PENDING_FILE):
        existing = read_pending(directory)
        if existing is None or _json_bytes(existing) != _json_bytes(pending):
            raise DyroError(f"Objective 存在另一笔未完成事务：{path}")
    else:
        _create_file(directory, _PENDING_FILE, _json_bytes(pending) + b"\n")

    if not _file_exists(directory, "events.jsonl"):
        _create_file(directory, "events.jsonl", b"")
    else:
        _read_file(directory, "events.jsonl", "Objective 事件日志")
    if contract_content is not None:
        _create_or_validate_contract(directory, record.revision, contract_content)

    events = read_events(directory, allow_empty=True)
    previous = str(event["previous_sha256"])
    last_sha = str(events[-1]["sha256"]) if events else ""
    if last_sha == str(event["sha256"]):
        if _json_bytes(events[-1]) != _json_bytes(event):
            raise ValidationError(f"Objective 事件哈希重复但内容不一致：{path}")
    elif last_sha == previous:
        _append_file(
            directory,
            "events.jsonl",
            (
                json.dumps(
                    event, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
                + "\n"
            ).encode("utf-8"),
        )
    else:
        raise ValidationError(f"Objective 事件链无法继续：{path}")
    persisted = StoredObjective(
        objective=record.objective,
        revision=record.revision,
        operator_state=record.operator_state,
        scope=record.scope,
        task_contract_sha256=record.task_contract_sha256,
        scope_sha256=record.scope_sha256,
        contract_sha256=record.contract_sha256,
        event_seq=int(event["seq"]),
        event_sha256=str(event["sha256"]),
    )
    _apply_pending_action_cancellation(directory, pending)
    write_projection(directory, persisted)
    _remove_file(directory, _PENDING_FILE, "Objective pending transaction")
    return persisted

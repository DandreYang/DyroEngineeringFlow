"""Secure persistent Action Journal storage and cancellation plans."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import json
import os
import stat
from typing import Iterator

from ..canonical import canonical_json_bytes
from ..config import validate_id
from ..errors import DyroError, ValidationError
from ..state import open_safe_child_directory
from .action_models import (
    ACTION_SCHEMA_VERSION, ActionIntent, ActionReceipt, ActionRecord, ActionStart, ActionStatus,
    parse_timestamp, reservation_from_payload, reservation_payload, safe_summary, timestamp, utc,
)
from .models import ActionKind
from .objective_storage import ObjectiveDirectory


MAX_ACTION_FILE_BYTES = 65_536
MAX_CANCELLATION_RECEIPTS = 128
_ACTION_DIRECTORIES = {"intent": "actions", "start": "action-starts", "receipt": "action-receipts"}


def _filename(action_id: str) -> str:
    return f"{validate_id(action_id, 'Action ID')}.json"


@contextmanager
def _child_directory(directory: ObjectiveDirectory, kind: str, *, create: bool) -> Iterator[int]:
    descriptor = open_safe_child_directory(directory.fd, _ACTION_DIRECTORIES[kind], create=create)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def _child_exists(directory: ObjectiveDirectory, kind: str) -> bool:
    try:
        info = os.stat(_ACTION_DIRECTORIES[kind], dir_fd=directory.fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise DyroError(f"无法读取 Action {kind} 目录") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ValidationError(f"Action {kind} 目录必须是安全的普通目录")
    return True


def _read_exact(descriptor: int, size: int) -> bytes:
    chunks: list[bytes] = []
    while size:
        chunk = os.read(descriptor, size)
        if not chunk:
            raise ValidationError("Action 状态文件读取中断")
        chunks.append(chunk)
        size -= len(chunk)
    return b"".join(chunks)


def _read_json(parent_fd: int, name: str, label: str) -> object:
    flags = os.O_RDONLY | (os.O_NOFOLLOW if hasattr(os, "O_NOFOLLOW") else 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise ValidationError(f"无法安全读取 {label}") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ACTION_FILE_BYTES:
            raise ValidationError(f"{label} 必须是受限普通文件")
        raw = _read_exact(descriptor, info.st_size)
    finally:
        os.close(descriptor)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValidationError(f"{label} 不是有效 JSON") from exc


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise DyroError("Action 状态文件写入中断")
        view = view[written:]


def _canonical_bytes(value: object) -> bytes:
    content = canonical_json_bytes(value)
    if len(content) > MAX_ACTION_FILE_BYTES:
        raise ValidationError("Action 状态文件超过允许大小")
    return content


def _create_only_json(parent_fd: int, name: str, value: object, label: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | (os.O_NOFOLLOW if hasattr(os, "O_NOFOLLOW") else 0)
    try:
        descriptor = os.open(name, flags, 0o600, dir_fd=parent_fd)
    except FileExistsError:
        raise
    except OSError as exc:
        raise DyroError(f"无法安全创建 {label}") from exc
    try:
        _write_all(descriptor, _canonical_bytes(value))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.fsync(parent_fd)


def _replace_json(parent_fd: int, name: str, value: object, label: str) -> None:
    temporary = f".{name}.{os.getpid()}.{os.urandom(8).hex()}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | (os.O_NOFOLLOW if hasattr(os, "O_NOFOLLOW") else 0)
    try:
        descriptor = os.open(temporary, flags, 0o600, dir_fd=parent_fd)
        try:
            _write_all(descriptor, _canonical_bytes(value))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
    except OSError as exc:
        raise DyroError(f"无法安全更新 {label}") from exc
    finally:
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise DyroError(f"无法清理 {label} 临时文件") from exc


def _remove_json(parent_fd: int, name: str, label: str) -> None:
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise DyroError(f"无法读取 {label}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValidationError(f"{label} 必须是安全的普通文件")
    try:
        os.unlink(name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except OSError as exc:
        raise DyroError(f"无法清理 {label}") from exc


def _intent_payload(intent: ActionIntent) -> dict[str, object]:
    return {
        "schema_version": ACTION_SCHEMA_VERSION, "action_id": intent.action_id,
        "idempotency_key": intent.idempotency_key, "objective_id": intent.objective_id,
        "objective_revision": intent.objective_revision, "objective_event_seq": intent.objective_event_seq,
        "objective_event_sha256": intent.objective_event_sha256, "scope_sha256": intent.scope_sha256,
        "snapshot_sha256": intent.snapshot_sha256, "plan_sha256": intent.plan_sha256,
        "operation": intent.operation.value, "subject_id": intent.subject_id, "owner_generation": intent.owner_generation,
        "expected_operation_generation": intent.expected_operation_generation, "authority_sha256": intent.authority_sha256,
        "budget_reservation": reservation_payload(intent.budget_reservation), "created_at": timestamp(intent.created_at),
    }


def _intent_from_payload(value: object) -> ActionIntent:
    fields = {
        "schema_version", "action_id", "idempotency_key", "objective_id", "objective_revision", "objective_event_seq",
        "objective_event_sha256", "scope_sha256", "snapshot_sha256", "plan_sha256", "operation", "subject_id",
        "owner_generation", "expected_operation_generation", "authority_sha256", "budget_reservation", "created_at",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ValidationError("Action intent 结构无效")
    if type(value.get("schema_version")) is not int or value["schema_version"] != ACTION_SCHEMA_VERSION:
        raise ValidationError("Action intent schema_version 无效")
    try:
        return ActionIntent(
            action_id=value["action_id"], idempotency_key=value["idempotency_key"], objective_id=value["objective_id"],
            objective_revision=value["objective_revision"], objective_event_seq=value["objective_event_seq"],
            objective_event_sha256=value["objective_event_sha256"], scope_sha256=value["scope_sha256"],
            snapshot_sha256=value["snapshot_sha256"], plan_sha256=value["plan_sha256"], operation=ActionKind(value["operation"]),
            subject_id=value["subject_id"], owner_generation=value["owner_generation"],
            expected_operation_generation=value["expected_operation_generation"], authority_sha256=value["authority_sha256"],
            budget_reservation=reservation_from_payload(value["budget_reservation"]),
            created_at=parse_timestamp(value["created_at"], "Action intent created_at"),
        )
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise ValidationError("Action intent 内容无效") from exc


def _start_payload(start: ActionStart) -> dict[str, object]:
    return {
        "schema_version": ACTION_SCHEMA_VERSION, "action_id": start.action_id, "idempotency_key": start.idempotency_key,
        "owner_generation": start.owner_generation, "owner_token_sha256": start.owner_token_sha256,
        "started_at": timestamp(start.started_at),
    }


def _start_from_payload(value: object) -> ActionStart:
    fields = {"schema_version", "action_id", "idempotency_key", "owner_generation", "owner_token_sha256", "started_at"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValidationError("Action start 结构无效")
    if type(value.get("schema_version")) is not int or value["schema_version"] != ACTION_SCHEMA_VERSION:
        raise ValidationError("Action start schema_version 无效")
    try:
        return ActionStart(
            action_id=value["action_id"], idempotency_key=value["idempotency_key"], owner_generation=value["owner_generation"],
            owner_token_sha256=value["owner_token_sha256"], started_at=parse_timestamp(value["started_at"], "Action start started_at"),
        )
    except (KeyError, TypeError, ValidationError) as exc:
        raise ValidationError("Action start 内容无效") from exc


def _receipt_payload(receipt: ActionReceipt) -> dict[str, object]:
    return {
        "schema_version": ACTION_SCHEMA_VERSION, "action_id": receipt.action_id,
        "idempotency_key": receipt.idempotency_key, "owner_generation": receipt.owner_generation,
        "status": receipt.status.value, "summary": receipt.summary, "recorded_at": timestamp(receipt.recorded_at),
    }


def _receipt_from_payload(value: object) -> ActionReceipt:
    fields = {"schema_version", "action_id", "idempotency_key", "owner_generation", "status", "summary", "recorded_at"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValidationError("Action receipt 结构无效")
    if type(value.get("schema_version")) is not int or value["schema_version"] != ACTION_SCHEMA_VERSION:
        raise ValidationError("Action receipt schema_version 无效")
    try:
        return ActionReceipt(
            action_id=value["action_id"], idempotency_key=value["idempotency_key"], owner_generation=value["owner_generation"],
            status=ActionStatus(value["status"]), summary=value["summary"],
            recorded_at=parse_timestamp(value["recorded_at"], "Action receipt recorded_at"),
        )
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise ValidationError("Action receipt 内容无效") from exc


def _read_intent(directory: ObjectiveDirectory, action_id: str) -> ActionIntent:
    with _child_directory(directory, "intent", create=False) as parent_fd:
        return _intent_from_payload(_read_json(parent_fd, _filename(action_id), "Action intent"))


def _read_optional_component(directory: ObjectiveDirectory, kind: str, action_id: str) -> ActionStart | ActionReceipt | None:
    if not _child_exists(directory, kind):
        return None
    try:
        with _child_directory(directory, kind, create=False) as parent_fd:
            payload = _read_json(parent_fd, _filename(action_id), f"Action {kind}")
    except FileNotFoundError:
        return None
    return _start_from_payload(payload) if kind == "start" else _receipt_from_payload(payload)


def read_action(directory: ObjectiveDirectory, action_id: str) -> ActionRecord:
    intent = _read_intent(directory, action_id)
    start = _read_optional_component(directory, "start", action_id)
    receipt = _read_optional_component(directory, "receipt", action_id)
    if start is not None and (not isinstance(start, ActionStart) or start.action_id != intent.action_id or start.idempotency_key != intent.idempotency_key or start.owner_generation != intent.owner_generation):
        raise ValidationError("Action start 与 intent binding 不匹配")
    if receipt is not None:
        if not isinstance(receipt, ActionReceipt) or receipt.action_id != intent.action_id or receipt.idempotency_key != intent.idempotency_key or receipt.owner_generation != intent.owner_generation:
            raise ValidationError("Action receipt 与 intent binding 不匹配")
        if start is None and receipt.status is not ActionStatus.CANCELLED:
            raise ValidationError("未 start 的 Action 只能写 cancelled receipt")
    return ActionRecord(intent=intent, start=start, receipt=receipt)


def _list_component_ids(directory: ObjectiveDirectory, kind: str) -> tuple[str, ...]:
    if not _child_exists(directory, kind):
        return ()
    with _child_directory(directory, kind, create=False) as parent_fd:
        ids: list[str] = []
        for name in os.listdir(parent_fd):
            if not name.endswith(".json") or name.startswith("."):
                raise ValidationError(f"Action {kind} 目录包含未知条目")
            action_id = validate_id(name.removesuffix(".json"), "Action ID")
            info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise ValidationError(f"Action {kind} 目录包含不安全条目")
            ids.append(action_id)
    return tuple(sorted(ids))


def list_actions(directory: ObjectiveDirectory) -> tuple[ActionRecord, ...]:
    intent_ids = _list_component_ids(directory, "intent")
    for kind in ("start", "receipt"):
        orphaned = sorted(set(_list_component_ids(directory, kind)) - set(intent_ids))
        if orphaned:
            raise ValidationError(f"Action {kind} 包含没有 intent 的记录：{', '.join(orphaned)}")
    return tuple(read_action(directory, action_id) for action_id in intent_ids)


def _same_intent_binding(existing: ActionIntent, candidate: ActionIntent) -> bool:
    return (
        existing.idempotency_key == candidate.idempotency_key and existing.objective_id == candidate.objective_id
        and existing.objective_revision == candidate.objective_revision and existing.objective_event_seq == candidate.objective_event_seq
        and existing.objective_event_sha256 == candidate.objective_event_sha256 and existing.scope_sha256 == candidate.scope_sha256
        and existing.snapshot_sha256 == candidate.snapshot_sha256 and existing.plan_sha256 == candidate.plan_sha256
        and existing.operation is candidate.operation and existing.subject_id == candidate.subject_id
        and existing.owner_generation == candidate.owner_generation and existing.expected_operation_generation == candidate.expected_operation_generation
        and existing.authority_sha256 == candidate.authority_sha256 and existing.budget_reservation == candidate.budget_reservation
    )


def reserve_action(directory: ObjectiveDirectory, intent: ActionIntent) -> ActionRecord:
    from .owner_lease import recover_owner_takeover

    recover_owner_takeover(directory)
    for existing in list_actions(directory):
        if existing.intent.idempotency_key == intent.idempotency_key:
            if not _same_intent_binding(existing.intent, intent):
                raise DyroError("Action idempotency_key 已绑定不同的 intent")
            return existing
    with _child_directory(directory, "intent", create=True) as parent_fd:
        try:
            _create_only_json(parent_fd, _filename(intent.action_id), _intent_payload(intent), "Action intent")
        except FileExistsError:
            existing = read_action(directory, intent.action_id)
            if not _same_intent_binding(existing.intent, intent):
                raise DyroError("Action ID 已绑定不同的 intent")
            return existing
    return ActionRecord(intent=intent, start=None, receipt=None)


def start_action(directory: ObjectiveDirectory, *, action_id: str, grant: object, now: datetime) -> ActionRecord:
    from .owner_lease import OwnerLeaseGrant, assert_owner, read_optional_lease, recover_owner_takeover

    if not isinstance(grant, OwnerLeaseGrant):
        raise TypeError("grant 必须是 OwnerLeaseGrant")
    recover_owner_takeover(directory)
    record = read_action(directory, action_id)
    if record.receipt is not None:
        raise DyroError("Action 已有终态 receipt；不能再次 start")
    lease = read_optional_lease(directory)
    if lease is None:
        raise DyroError("Scheduler owner lease 不存在")
    now_utc = assert_owner(lease, grant, now)
    if lease.objective_id != record.intent.objective_id or lease.generation != record.intent.owner_generation:
        raise DyroError("Action intent 已被新的 Scheduler generation 围栏")
    expected = ActionStart(record.intent.action_id, record.intent.idempotency_key, record.intent.owner_generation, lease.owner_token_sha256, now_utc)
    if record.start is not None:
        if record.start != expected and (
            record.start.action_id != expected.action_id or record.start.idempotency_key != expected.idempotency_key
            or record.start.owner_generation != expected.owner_generation or record.start.owner_token_sha256 != expected.owner_token_sha256
        ):
            raise DyroError("Action start 已存在且与当前 owner binding 不匹配")
        return record
    with _child_directory(directory, "start", create=True) as parent_fd:
        try:
            _create_only_json(parent_fd, _filename(action_id), _start_payload(expected), "Action start")
        except FileExistsError:
            current = read_action(directory, action_id)
            if current.start is None or current.start.action_id != expected.action_id or current.start.idempotency_key != expected.idempotency_key or current.start.owner_generation != expected.owner_generation or current.start.owner_token_sha256 != expected.owner_token_sha256:
                raise DyroError("Action start 已存在且与当前 owner binding 不匹配")
            return current
    return ActionRecord(intent=record.intent, start=expected, receipt=None)


def _same_receipt(existing: ActionReceipt, candidate: ActionReceipt) -> bool:
    return existing == candidate


def _publish_receipt(directory: ObjectiveDirectory, receipt: ActionReceipt) -> ActionRecord:
    record = read_action(directory, receipt.action_id)
    if receipt.idempotency_key != record.intent.idempotency_key or receipt.owner_generation != record.intent.owner_generation:
        raise ValidationError("Action receipt 与 intent binding 不匹配")
    if record.receipt is not None:
        if not _same_receipt(record.receipt, receipt):
            raise DyroError("Action receipt 已存在且不匹配")
        return record
    with _child_directory(directory, "receipt", create=True) as parent_fd:
        try:
            _create_only_json(parent_fd, _filename(receipt.action_id), _receipt_payload(receipt), "Action receipt")
        except FileExistsError:
            current = read_action(directory, receipt.action_id)
            if current.receipt is None or not _same_receipt(current.receipt, receipt):
                raise DyroError("Action receipt 已存在且不匹配")
            return current
    return ActionRecord(intent=record.intent, start=record.start, receipt=receipt)


def _cancellation_plan_payload(receipts: tuple[ActionReceipt, ...]) -> dict[str, object]:
    return {"schema_version": ACTION_SCHEMA_VERSION, "receipts": [_receipt_payload(receipt) for receipt in receipts]}


def cancellation_plan_from_payload(value: object) -> tuple[ActionReceipt, ...]:
    if not isinstance(value, dict) or set(value) != {"schema_version", "receipts"}:
        raise ValidationError("Action cancellation plan 结构无效")
    if value.get("schema_version") != ACTION_SCHEMA_VERSION:
        raise ValidationError("Action cancellation plan 版本无效")
    raw = value.get("receipts")
    if not isinstance(raw, list) or not raw or len(raw) > MAX_CANCELLATION_RECEIPTS:
        raise ValidationError("Action cancellation plan receipts 无效")
    try:
        receipts = tuple(_receipt_from_payload(item) for item in raw)
    except (TypeError, ValidationError) as exc:
        raise ValidationError("Action cancellation plan receipts 无效") from exc
    ids = tuple(receipt.action_id for receipt in receipts)
    if ids != tuple(sorted(ids)) or len(set(ids)) != len(ids) or any(receipt.status is not ActionStatus.CANCELLED for receipt in receipts):
        raise ValidationError("Action cancellation plan 无效")
    return receipts


def prepare_action_cancellation(directory: ObjectiveDirectory, *, summary: str, now: datetime) -> dict[str, object] | None:
    recorded_at = utc(now, "now")
    reserved = tuple(record for record in list_actions(directory) if record.start is None and record.receipt is None)
    if not reserved:
        return None
    if len(reserved) > MAX_CANCELLATION_RECEIPTS:
        raise DyroError("待取消 Action 数量超过单次安全上限")
    safe = safe_summary(summary)
    return _cancellation_plan_payload(tuple(
        ActionReceipt(record.intent.action_id, record.intent.idempotency_key, record.intent.owner_generation, ActionStatus.CANCELLED, safe, recorded_at)
        for record in reserved
    ))


def apply_action_cancellation(directory: ObjectiveDirectory, plan: object) -> tuple[ActionRecord, ...]:
    receipts = cancellation_plan_from_payload(plan)
    for receipt in receipts:
        record = read_action(directory, receipt.action_id)
        if record.receipt is not None and not _same_receipt(record.receipt, receipt):
            raise DyroError("Action cancellation plan 与既有 receipt 不匹配")
        if record.receipt is None and record.start is not None:
            raise DyroError("Action 已 start；拒绝应用 cancellation plan")
    return tuple(_publish_receipt(directory, receipt) for receipt in receipts)


def cancel_unstarted_actions(directory: ObjectiveDirectory, *, summary: str, now: datetime) -> tuple[ActionRecord, ...]:
    plan = prepare_action_cancellation(directory, summary=summary, now=now)
    return () if plan is None else apply_action_cancellation(directory, plan)


def record_action_receipt(directory: ObjectiveDirectory, receipt: ActionReceipt, *, grant: object = None, now: datetime | None = None) -> ActionRecord:
    from .owner_lease import OwnerLeaseGrant, recover_owner_takeover, verify_owner_lease

    recover_owner_takeover(directory)
    record = read_action(directory, receipt.action_id)
    if receipt.idempotency_key != record.intent.idempotency_key or receipt.owner_generation != record.intent.owner_generation:
        raise ValidationError("Action receipt 与 intent binding 不匹配")
    if record.receipt is not None:
        return _publish_receipt(directory, receipt)
    if record.start is None:
        if receipt.status is not ActionStatus.CANCELLED:
            raise DyroError("未 start 的 Action 只能写 cancelled receipt")
        if not isinstance(grant, OwnerLeaseGrant) or now is None:
            raise DyroError("取消未 start 的 Action 必须提供当前 Scheduler owner grant")
        lease = verify_owner_lease(directory, grant=grant, now=now)
        if lease.objective_id != record.intent.objective_id:
            raise DyroError("Scheduler owner lease 与 Action Objective 不匹配")
    return _publish_receipt(directory, receipt)

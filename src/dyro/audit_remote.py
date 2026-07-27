from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import secrets
import stat
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .canonical import canonical_json_bytes
from .errors import DyroError, ValidationError
from .signing import (
    read_trust_audit,
    sign_record,
    signature_key_id,
    trusted_keys_directory,
    validate_key_id,
    verify_record,
)
from .state import atomic_write_text, exclusive_lock


AUDIT_BATCH_TYPE = "dyro.audit.batch"
AUDIT_RECEIPT_TYPE = "dyro.audit.receipt"
AUDIT_KEY_TRANSITION_TYPE = "dyro.audit.key-transition"
AUDIT_EXPORT_PURPOSE = "audit-export"
AUDIT_RECEIPT_PURPOSE = "audit-receipt"
AUDIT_RECOVERY_PURPOSE = "audit-recovery"
GENESIS_HEAD = "0" * 64
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_CLOCK_SKEW = timedelta(minutes=5)
WORKSPACE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


@dataclass(frozen=True)
class AuditSyncResult:
    synced: bool
    sequence: int
    head_sha256: str
    batch: dict[str, object] | None
    receipt: dict[str, object] | None
    state_path: Path


def _validate_workspace_id(workspace_id: str) -> str:
    value = workspace_id.strip()
    if not WORKSPACE_ID_PATTERN.fullmatch(value):
        raise ValidationError(
            "audit workspace ID 只能包含字母、数字、点、下划线、冒号和连字符，最长 128 字符"
        )
    return value


def default_audit_workspace_id(name: str) -> str:
    value = name.strip()
    if WORKSPACE_ID_PATTERN.fullmatch(value):
        return value
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:24]
    return f"workspace-{digest}"


def _state_path(root: Path, witness: str) -> Path:
    return root / ".dyro" / "audit-witnesses" / f"{validate_key_id(witness)}.json"


def _load_state(path: Path) -> dict[str, object] | None:
    if not path.exists() and not path.is_symlink():
        return None
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        raise ValidationError(f"audit witness 状态发生并发变化：{path}") from None
    if not stat.S_ISREG(mode):
        raise ValidationError(f"audit witness 状态必须是普通文件且不能是符号链接：{path}")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"audit witness 状态损坏：{path}") from exc
    if (
        not isinstance(state, dict)
        or state.get("schema_version") != 2
        or not isinstance(state.get("workspace_id"), str)
        or not isinstance(state.get("witness"), str)
        or not isinstance(state.get("witness_key_id"), str)
        or not isinstance(state.get("receipt_key_epoch"), int)
        or isinstance(state.get("receipt_key_epoch"), bool)
        or int(state.get("receipt_key_epoch", 0)) < 1
        or not isinstance(state.get("endpoint"), str)
        or not isinstance(state.get("sequence"), int)
        or isinstance(state.get("sequence"), bool)
        or not isinstance(state.get("head_sha256"), str)
        or (
            state.get("receipt") is not None
            and not isinstance(state.get("receipt"), dict)
        )
        or (
            state.get("confirmed_batch") is not None
            and not isinstance(state.get("confirmed_batch"), dict)
        )
        or (
            state.get("pending") is not None
            and not isinstance(state.get("pending"), dict)
        )
        or (
            state.get("recovery_key_id") is not None
            and not isinstance(state.get("recovery_key_id"), str)
        )
        or (
            state.get("verified_at") is not None
            and not isinstance(state.get("verified_at"), str)
        )
    ):
        raise ValidationError(f"audit witness 状态格式无效：{path}")
    return state


def _validate_sha256(value: object, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValidationError(f"audit {field} 必须是小写 SHA-256")
    return value


def _advance_head(
    head: str,
    *,
    sequence: int,
    event: dict[str, object],
) -> str:
    envelope = {"sequence": sequence, "event": event}
    return hashlib.sha256(
        bytes.fromhex(head) + canonical_json_bytes(envelope)
    ).hexdigest()


def validate_audit_batch(
    batch: dict[str, object],
    *,
    workspace_id: str,
    witness: str,
    previous_sequence: int,
    previous_head: str,
) -> tuple[int, str]:
    """Validate and independently replay one signed batch at a Witness."""
    workspace_id = _validate_workspace_id(workspace_id)
    witness = validate_key_id(witness)
    previous_head = _validate_sha256(previous_head, field="previous head")
    if previous_sequence < 0:
        raise ValidationError("audit previous sequence 不能为负数")
    if (
        batch.get("schema_version") != 1
        or batch.get("type") != AUDIT_BATCH_TYPE
        or batch.get("workspace_id") != workspace_id
        or batch.get("witness") != witness
        or not isinstance(batch.get("endpoint"), str)
    ):
        raise ValidationError("audit batch 身份或 schema 不匹配")
    request_id = batch.get("request_id")
    if (
        not isinstance(request_id, str)
        or len(request_id) != 32
        or any(character not in "0123456789abcdef" for character in request_id)
    ):
        raise ValidationError("audit batch request_id 必须是 128-bit 小写十六进制值")
    requested_at = batch.get("requested_at")
    if not isinstance(requested_at, str):
        raise ValidationError("audit batch requested_at 必须是带时区 ISO-8601")
    try:
        requested_time = datetime.fromisoformat(requested_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("audit batch requested_at 不是有效 ISO-8601") from exc
    if requested_time.tzinfo is None:
        raise ValidationError("audit batch requested_at 必须包含时区")
    validate_key_id(str(batch.get("requested_witness_key_id", "")))
    recovery_key_id = batch.get("recovery_key_id")
    if recovery_key_id is not None:
        validate_key_id(str(recovery_key_id))
    receipt_key_epoch = batch.get("receipt_key_epoch")
    if (
        not isinstance(receipt_key_epoch, int)
        or isinstance(receipt_key_epoch, bool)
        or receipt_key_epoch < 1
    ):
        raise ValidationError("audit batch receipt_key_epoch 必须是正整数")
    from_sequence = batch.get("from_sequence")
    to_sequence = batch.get("to_sequence")
    events = batch.get("events")
    if (
        not isinstance(from_sequence, int)
        or isinstance(from_sequence, bool)
        or not isinstance(to_sequence, int)
        or isinstance(to_sequence, bool)
        or not isinstance(events, list)
    ):
        raise ValidationError("audit batch sequence 或 events 格式无效")
    if batch.get("previous_head_sha256") != previous_head:
        raise ValidationError("audit batch previous head 与 Witness 当前 head 不匹配")
    if from_sequence != previous_sequence + 1:
        raise ValidationError("audit batch 必须从 Witness 下一 sequence 开始")
    if to_sequence != previous_sequence + len(events):
        raise ValidationError("audit batch sequence 范围与 events 数量不一致")
    head = previous_head
    for offset, item in enumerate(events, start=1):
        expected_sequence = previous_sequence + offset
        if (
            not isinstance(item, dict)
            or item.get("sequence") != expected_sequence
            or not isinstance(item.get("event"), dict)
        ):
            raise ValidationError("audit batch events 必须连续且包含 JSON event 对象")
        head = _advance_head(
            head,
            sequence=expected_sequence,
            event=dict(item["event"]),
        )
    if batch.get("head_sha256") != head:
        raise ValidationError("audit batch head 与 Witness 重算结果不匹配")
    return to_sequence, head


def _chain_head(
    events: tuple[dict[str, object], ...],
    *,
    limit: int | None = None,
) -> str:
    head = GENESIS_HEAD
    selected = events if limit is None else events[:limit]
    for sequence, event in enumerate(selected, start=1):
        head = _advance_head(head, sequence=sequence, event=event)
    return head


def _validate_local_history(
    events: tuple[dict[str, object], ...],
    *,
    sequence: int,
    head: str,
) -> None:
    if sequence > len(events):
        raise ValidationError("本地 audit 日志短于已确认的远端 sequence，检测到回滚")
    if _chain_head(events, limit=sequence) != head:
        raise ValidationError("本地 audit 历史与远端已确认 head 不匹配，检测到篡改或分叉")


def _build_batch(
    root: Path,
    *,
    workspace_id: str,
    witness: str,
    endpoint: str,
    signing_key: Path,
    key_id: str,
    witness_key_id: str,
    recovery_key_id: str | None,
    current_witness_key_id: str | None,
    current_receipt_key_epoch: int,
    events: tuple[dict[str, object], ...],
    previous_sequence: int,
    previous_head: str,
) -> tuple[dict[str, object], int, str]:
    _validate_local_history(
        events,
        sequence=previous_sequence,
        head=previous_head,
    )
    head = _chain_head(events)
    receipt_key_epoch = (
        current_receipt_key_epoch + 1
        if current_witness_key_id is not None
        and current_witness_key_id != witness_key_id
        else current_receipt_key_epoch
    )
    unsigned: dict[str, object] = {
        "schema_version": 1,
        "type": AUDIT_BATCH_TYPE,
        "workspace_id": workspace_id,
        "witness": witness,
        "endpoint": endpoint,
        "request_id": secrets.token_hex(16),
        "requested_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        "requested_witness_key_id": validate_key_id(witness_key_id),
        "recovery_key_id": recovery_key_id,
        "receipt_key_epoch": receipt_key_epoch,
        "from_sequence": previous_sequence + 1,
        "to_sequence": len(events),
        "previous_head_sha256": previous_head,
        "head_sha256": head,
        "events": [
            {
                "sequence": sequence,
                "event": events[sequence - 1],
            }
            for sequence in range(previous_sequence + 1, len(events) + 1)
        ],
    }
    return (
        sign_record(
            unsigned,
            purpose=AUDIT_EXPORT_PURPOSE,
            key_id=key_id,
            private_key=signing_key,
        ),
        len(events),
        head,
    )


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        return None


def _validate_endpoint(endpoint: str, *, allow_insecure_http: bool) -> str:
    parsed = urlparse(endpoint)
    allowed_schemes = {"https"} | ({"http"} if allow_insecure_http else set())
    if (
        parsed.scheme not in allowed_schemes
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValidationError("audit endpoint 必须是无内嵌凭据和 fragment 的 HTTPS URL")
    return endpoint


def _post_batch(
    endpoint: str,
    batch: dict[str, object],
    *,
    token: str | None,
    timeout_seconds: float,
) -> dict[str, object]:
    body = canonical_json_bytes(batch)
    batch_sha256 = hashlib.sha256(body).hexdigest()
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Idempotency-Key": batch_sha256,
        "User-Agent": "dyro-audit-sync/1",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(endpoint, data=body, headers=headers, method="POST")
    opener = build_opener(_NoRedirectHandler())
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            content = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        try:
            error_content = exc.read(MAX_RESPONSE_BYTES + 1)
        except OSError:
            error_content = b""
        exc.close()
        detail = ""
        if len(error_content) <= MAX_RESPONSE_BYTES:
            try:
                payload = json.loads(error_content)
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, dict):
                parts = [
                    str(payload[field]).strip()
                    for field in ("code", "error")
                    if isinstance(payload.get(field), str)
                    and str(payload[field]).strip()
                ]
                safe_parts = [
                    "".join(
                        character
                        for character in part
                        if character.isprintable() and character not in "\r\n"
                    )[:200]
                    for part in parts
                ]
                if safe_parts:
                    detail = f": {' - '.join(safe_parts)}"
        raise DyroError(f"audit witness HTTP {exc.code}{detail}") from exc
    except (URLError, OSError) as exc:
        reason = getattr(exc, "reason", str(exc))
        raise DyroError(f"audit witness 连接失败：{reason}") from exc
    if len(content) > MAX_RESPONSE_BYTES:
        raise ValidationError("audit witness 回执超过 1 MiB 限制")
    try:
        receipt = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValidationError("audit witness 回执不是有效 JSON") from exc
    if not isinstance(receipt, dict):
        raise ValidationError("audit witness 回执必须是 JSON 对象")
    return receipt


def _validate_key_transition(
    root: Path,
    *,
    transition: dict[str, object],
    batch_sha256: str,
    batch: dict[str, object],
    previous_witness_key_id: str,
    witness_key_id: str,
    recovery_key_id: str | None,
) -> None:
    signature = transition.get("signature")
    purpose = signature.get("purpose") if isinstance(signature, dict) else None
    if purpose == AUDIT_RECEIPT_PURPOSE:
        expected_authorizer = previous_witness_key_id
    elif purpose == AUDIT_RECOVERY_PURPOSE and recovery_key_id is not None:
        expected_authorizer = recovery_key_id
    else:
        raise ValidationError("audit witness key transition 缺少有效旧 key 或 recovery 授权")
    verify_record(
        transition,
        purpose=purpose,
        trust_directory=trusted_keys_directory(root, purpose),
        required=True,
    )
    if signature_key_id(transition) != validate_key_id(expected_authorizer):
        raise ValidationError("audit witness key transition 授权 key ID 不匹配")
    expected = {
        "schema_version": 1,
        "type": AUDIT_KEY_TRANSITION_TYPE,
        "witness": batch["witness"],
        "workspace_id": batch["workspace_id"],
        "sequence": batch["to_sequence"],
        "head_sha256": batch["head_sha256"],
        "batch_sha256": batch_sha256,
        "previous_key_id": previous_witness_key_id,
        "next_key_id": witness_key_id,
        "previous_receipt_key_epoch": int(batch["receipt_key_epoch"]) - 1,
        "next_receipt_key_epoch": batch["receipt_key_epoch"],
    }
    for field, value in expected.items():
        if transition.get(field) != value:
            raise ValidationError(f"audit witness key transition 字段不匹配：{field}")


def _validate_receipt(
    root: Path,
    *,
    receipt: dict[str, object],
    batch: dict[str, object],
    witness: str,
    witness_key_id: str,
    previous_witness_key_id: str | None,
    recovery_key_id: str | None,
    historical: bool = False,
    historical_verified_at: datetime | None = None,
    fresh_verified_at: datetime | None = None,
) -> None:
    accepted_at = receipt.get("accepted_at")
    if not isinstance(accepted_at, str):
        raise ValidationError("audit witness 回执缺少 accepted_at")
    try:
        verification_time = datetime.fromisoformat(
            accepted_at.replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise ValidationError("audit witness accepted_at 不是有效 ISO-8601") from exc
    if verification_time.tzinfo is None:
        raise ValidationError("audit witness accepted_at 必须包含时区")
    requested_at = batch.get("requested_at")
    if not isinstance(requested_at, str):
        raise ValidationError("audit batch 缺少 requested_at")
    try:
        requested_time = datetime.fromisoformat(requested_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("audit batch requested_at 不是有效 ISO-8601") from exc
    if requested_time.tzinfo is None:
        raise ValidationError("audit batch requested_at 必须包含时区")
    if verification_time < requested_time - MAX_CLOCK_SKEW:
        raise ValidationError("audit witness accepted_at 早于允许的请求时钟偏差")
    local_verification_time = (
        historical_verified_at
        if historical
        else (fresh_verified_at or datetime.now(timezone.utc))
    )
    if local_verification_time is None:
        raise ValidationError("audit witness 历史状态缺少本地 verified_at")
    if verification_time > local_verification_time + MAX_CLOCK_SKEW:
        raise ValidationError("audit witness accepted_at 晚于允许的接收时钟偏差")
    verify_record(
        receipt,
        purpose=AUDIT_RECEIPT_PURPOSE,
        trust_directory=trusted_keys_directory(root, AUDIT_RECEIPT_PURPOSE),
        required=True,
        at=local_verification_time,
    )
    verify_record(
        receipt,
        purpose=AUDIT_RECEIPT_PURPOSE,
        trust_directory=trusted_keys_directory(root, AUDIT_RECEIPT_PURPOSE),
        required=True,
        at=verification_time,
        ignore_revocation=True,
    )
    if signature_key_id(receipt) != validate_key_id(witness_key_id):
        raise ValidationError("audit receipt signature key ID 与配置的 witness key 不匹配")
    batch_sha256 = hashlib.sha256(canonical_json_bytes(batch)).hexdigest()
    expected = {
        "schema_version": 1,
        "type": AUDIT_RECEIPT_TYPE,
        "witness": witness,
        "workspace_id": batch["workspace_id"],
        "from_sequence": batch["from_sequence"],
        "to_sequence": batch["to_sequence"],
        "head_sha256": batch["head_sha256"],
        "batch_sha256": batch_sha256,
        "witness_key_id": witness_key_id,
        "recovery_key_id": batch.get("recovery_key_id"),
        "receipt_key_epoch": batch.get("receipt_key_epoch"),
    }
    for field, value in expected.items():
        if receipt.get(field) != value:
            raise ValidationError(f"audit witness 回执字段不匹配：{field}")
    if (
        previous_witness_key_id is not None
        and previous_witness_key_id != witness_key_id
    ):
        transition = receipt.get("key_transition")
        if not isinstance(transition, dict):
            raise ValidationError("audit witness key 轮换缺少签名 transition")
        _validate_key_transition(
            root,
            transition=transition,
            batch_sha256=batch_sha256,
            batch=batch,
            previous_witness_key_id=previous_witness_key_id,
            witness_key_id=witness_key_id,
            recovery_key_id=recovery_key_id,
        )


def _validate_confirmed_state(root: Path, state: dict[str, object]) -> None:
    sequence = int(state["sequence"])
    head = _validate_sha256(state["head_sha256"], field="state head")
    receipt = state.get("receipt")
    batch = state.get("confirmed_batch")
    if receipt is None and batch is None:
        if sequence != 0 or head != GENESIS_HEAD:
            raise ValidationError("audit witness 未确认状态必须位于 genesis")
        return
    if not isinstance(receipt, dict) or not isinstance(batch, dict):
        raise ValidationError("audit witness receipt 与 confirmed batch 必须成对存在")
    witness_key_id = validate_key_id(str(state["witness_key_id"]))
    recovery_key_id = state.get("recovery_key_id")
    verified_at = state.get("verified_at")
    if not isinstance(verified_at, str):
        raise ValidationError("audit witness confirmed state 缺少 verified_at")
    try:
        historical_verified_at = datetime.fromisoformat(
            verified_at.replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise ValidationError("audit witness verified_at 不是有效 ISO-8601") from exc
    if historical_verified_at.tzinfo is None:
        raise ValidationError("audit witness verified_at 必须包含时区")
    _validate_receipt(
        root,
        receipt=receipt,
        batch=batch,
        witness=str(state["witness"]),
        witness_key_id=witness_key_id,
        previous_witness_key_id=witness_key_id,
        recovery_key_id=(
            validate_key_id(recovery_key_id)
            if isinstance(recovery_key_id, str)
            else None
        ),
        historical=True,
        historical_verified_at=historical_verified_at,
    )
    if (
        batch.get("to_sequence") != sequence
        or batch.get("head_sha256") != head
        or batch.get("recovery_key_id") != state.get("recovery_key_id")
        or batch.get("receipt_key_epoch") != state.get("receipt_key_epoch")
    ):
        raise ValidationError("audit witness state 与已验证回执 checkpoint 不匹配")


def _validate_pending_batch(
    batch: dict[str, object],
    *,
    workspace_id: str,
    witness: str,
    endpoint: str,
    signing_key: Path,
    key_id: str,
    witness_key_id: str,
    recovery_key_id: str | None,
    current_witness_key_id: str | None,
    current_receipt_key_epoch: int,
    previous_sequence: int,
    previous_head: str,
    events: tuple[dict[str, object], ...],
) -> tuple[int, str]:
    if batch.get("endpoint") != endpoint:
        raise ValidationError("audit pending batch endpoint 不匹配")
    if batch.get("requested_witness_key_id") != witness_key_id:
        raise ValidationError("audit pending batch witness key ID 不匹配")
    if batch.get("recovery_key_id") != recovery_key_id:
        raise ValidationError("audit pending batch recovery key ID 不匹配")
    expected_epoch = (
        current_receipt_key_epoch + 1
        if current_witness_key_id is not None
        and current_witness_key_id != witness_key_id
        else current_receipt_key_epoch
    )
    if batch.get("receipt_key_epoch") != expected_epoch:
        raise ValidationError("audit pending batch receipt key epoch 不匹配")
    sequence, head = validate_audit_batch(
        batch,
        workspace_id=workspace_id,
        witness=witness,
        previous_sequence=previous_sequence,
        previous_head=previous_head,
    )
    if signature_key_id(batch) != validate_key_id(key_id):
        raise ValidationError("audit pending batch export key ID 不匹配")
    unsigned = dict(batch)
    unsigned.pop("signature", None)
    expected = sign_record(
        unsigned,
        purpose=AUDIT_EXPORT_PURPOSE,
        key_id=key_id,
        private_key=signing_key,
    )
    if canonical_json_bytes(expected) != canonical_json_bytes(batch):
        raise ValidationError("audit pending batch 签名与当前 export key 不匹配")
    _validate_local_history(events, sequence=sequence, head=head)
    return sequence, head


def sync_trust_audit(
    root: Path,
    *,
    workspace_id: str,
    witness: str,
    endpoint: str,
    signing_key: Path,
    key_id: str,
    witness_key_id: str,
    recovery_key_id: str | None = None,
    token: str | None = None,
    allow_insecure_http: bool = False,
    timeout_seconds: float = 15.0,
    dry_run: bool = False,
) -> AuditSyncResult:
    root = root.resolve()
    workspace_id = _validate_workspace_id(workspace_id)
    witness = validate_key_id(witness)
    witness_key_id = validate_key_id(witness_key_id)
    recovery_key_id = (
        validate_key_id(recovery_key_id) if recovery_key_id is not None else None
    )
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValidationError("audit timeout seconds 必须是有限正数")
    endpoint = _validate_endpoint(endpoint, allow_insecure_http=allow_insecure_http)
    signing_key = signing_key.expanduser().resolve()
    state_path = _state_path(root, witness)
    lock_path = root / ".dyro" / "locks" / f"audit-{witness}.lock"
    with exclusive_lock(lock_path):
        previous = _load_state(state_path)
        current_witness_key_id: str | None = None
        current_receipt_key_epoch = 1
        if previous is not None:
            if previous.get("endpoint") != endpoint:
                raise ValidationError("audit witness endpoint 与已有回执状态不一致")
            if previous.get("workspace_id") != workspace_id:
                raise ValidationError("audit workspace ID 与已有回执状态不一致")
            if previous.get("witness") != witness:
                raise ValidationError("audit witness ID 与已有回执状态不一致")
            stored_recovery_key_id = previous.get("recovery_key_id")
            if recovery_key_id is None and isinstance(stored_recovery_key_id, str):
                recovery_key_id = stored_recovery_key_id
            elif stored_recovery_key_id != recovery_key_id:
                raise ValidationError("audit recovery key ID 与已有回执状态不一致")
            _validate_confirmed_state(root, previous)
            current_witness_key_id = validate_key_id(
                str(previous["witness_key_id"])
            )
            current_receipt_key_epoch = int(previous["receipt_key_epoch"])
        previous_sequence = int(previous["sequence"]) if previous is not None else 0
        previous_head = (
            str(previous["head_sha256"]) if previous is not None else GENESIS_HEAD
        )
        events = tuple(dict(event) for event in read_trust_audit(root))
        _validate_local_history(
            events,
            sequence=previous_sequence,
            head=previous_head,
        )
        pending = previous.get("pending") if previous is not None else None
        if isinstance(pending, dict):
            batch = dict(pending)
            sequence, head = _validate_pending_batch(
                batch,
                workspace_id=workspace_id,
                witness=witness,
                endpoint=endpoint,
                signing_key=signing_key,
                key_id=key_id,
                witness_key_id=witness_key_id,
                recovery_key_id=recovery_key_id,
                current_witness_key_id=current_witness_key_id,
                current_receipt_key_epoch=current_receipt_key_epoch,
                previous_sequence=previous_sequence,
                previous_head=previous_head,
                events=events,
            )
        else:
            batch, sequence, head = _build_batch(
                root,
                workspace_id=workspace_id,
                witness=witness,
                endpoint=endpoint,
                signing_key=signing_key,
                key_id=key_id,
                witness_key_id=witness_key_id,
                recovery_key_id=recovery_key_id,
                current_witness_key_id=current_witness_key_id,
                current_receipt_key_epoch=current_receipt_key_epoch,
                events=events,
                previous_sequence=previous_sequence,
                previous_head=previous_head,
            )
        if dry_run:
            return AuditSyncResult(
                synced=False,
                sequence=sequence,
                head_sha256=head,
                batch=batch,
                receipt=None,
                state_path=state_path,
            )
        if not isinstance(pending, dict):
            pending_state = {
                "schema_version": 2,
                "workspace_id": workspace_id,
                "witness": witness,
                "witness_key_id": current_witness_key_id or witness_key_id,
                "receipt_key_epoch": current_receipt_key_epoch,
                "recovery_key_id": recovery_key_id,
                "endpoint": endpoint,
                "sequence": previous_sequence,
                "head_sha256": previous_head,
                "confirmed_batch": (
                    previous.get("confirmed_batch") if previous is not None else None
                ),
                "receipt": previous.get("receipt") if previous is not None else None,
                "pending": batch,
                "verified_at": (
                    previous.get("verified_at") if previous is not None else None
                ),
                "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            atomic_write_text(
                state_path,
                json.dumps(
                    pending_state,
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                )
                + "\n",
            )
        receipt = _post_batch(
            endpoint,
            batch,
            token=token,
            timeout_seconds=timeout_seconds,
        )
        receipt_verified_at = datetime.now(timezone.utc)
        _validate_receipt(
            root,
            receipt=receipt,
            batch=batch,
            witness=witness,
            witness_key_id=witness_key_id,
            previous_witness_key_id=current_witness_key_id,
            recovery_key_id=recovery_key_id,
            fresh_verified_at=receipt_verified_at,
        )
        state = {
            "schema_version": 2,
            "workspace_id": workspace_id,
            "witness": witness,
            "witness_key_id": witness_key_id,
            "receipt_key_epoch": batch["receipt_key_epoch"],
            "recovery_key_id": recovery_key_id,
            "endpoint": endpoint,
            "sequence": sequence,
            "head_sha256": head,
            "confirmed_batch": batch,
            "receipt": receipt,
            "pending": None,
            "verified_at": receipt_verified_at.isoformat(
                timespec="microseconds"
            ),
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        atomic_write_text(
            state_path,
            json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        )
        return AuditSyncResult(
            synced=True,
            sequence=sequence,
            head_sha256=head,
            batch=batch,
            receipt=receipt,
            state_path=state_path,
        )

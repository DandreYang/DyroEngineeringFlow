from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import socket
import ssl
import stat
import tempfile
import threading
from typing import Any, Callable

from .audit_remote import (
    AUDIT_EXPORT_PURPOSE,
    AUDIT_KEY_TRANSITION_TYPE,
    AUDIT_RECEIPT_PURPOSE,
    AUDIT_RECEIPT_TYPE,
    AUDIT_RECOVERY_PURPOSE,
    GENESIS_HEAD,
    MAX_RESPONSE_BYTES,
    WORKSPACE_ID_PATTERN,
    validate_audit_batch,
)
from .canonical import canonical_json_bytes
from .errors import DyroError, ValidationError
from .signing import (
    sign_record,
    signature_key_id,
    trusted_keys_directory,
    validate_key_id,
    verify_record,
)
from .state import exclusive_lock


WITNESS_STATE_SCHEMA = 1
WITNESS_RECORD_SCHEMA = 1
WITNESS_PATH = "/v1/dyro/batches"
DEFAULT_MAX_CONCURRENT_REQUESTS = 32
DEFAULT_READ_TIMEOUT_SECONDS = 15.0


class WitnessRequestError(DyroError):
    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class WitnessConfig:
    storage_root: Path
    client_trust_root: Path
    witness_id: str
    receipt_key_id: str
    receipt_signing_key: Path
    record_archive_root: Path | None = None
    auth_token: str | None = None
    workspace_id: str | None = None
    client_workspace_bindings: dict[str, str] | None = None
    expected_endpoint: str | None = None
    transition_key_id: str | None = None
    transition_signing_key: Path | None = None
    transition_purpose: str | None = None


@dataclass(frozen=True)
class WitnessAcceptance:
    receipt: dict[str, object]
    created: bool


def _validate_workspace_id(value: object) -> str:
    if not isinstance(value, str):
        raise WitnessRequestError("invalid_workspace", "workspace_id 必须是字符串")
    normalized = value.strip()
    if not WORKSPACE_ID_PATTERN.fullmatch(normalized):
        raise WitnessRequestError("invalid_workspace", "workspace_id 格式无效")
    return normalized


def _validate_config(config: WitnessConfig) -> WitnessConfig:
    witness_id = validate_key_id(config.witness_id)
    receipt_key_id = validate_key_id(config.receipt_key_id)
    workspace_id = (
        _validate_workspace_id(config.workspace_id)
        if config.workspace_id is not None
        else None
    )
    bindings: dict[str, str] | None = None
    if config.client_workspace_bindings is not None:
        if not isinstance(config.client_workspace_bindings, dict):
            raise ValidationError("Witness client workspace bindings 必须是映射")
        bindings = {}
        for key_id, bound_workspace_id in config.client_workspace_bindings.items():
            if not isinstance(key_id, str):
                raise ValidationError("Witness client workspace binding key 必须是字符串")
            normalized_key_id = validate_key_id(key_id)
            if normalized_key_id in bindings:
                raise ValidationError(f"Witness client workspace binding 重复：{normalized_key_id}")
            bindings[normalized_key_id] = _validate_workspace_id(bound_workspace_id)
    if workspace_id is None and not bindings:
        raise ValidationError("Witness 必须设置 --workspace-id 或 client workspace bindings")
    transition_values = (
        config.transition_key_id,
        config.transition_signing_key,
        config.transition_purpose,
    )
    if any(value is not None for value in transition_values) and not all(
        value is not None for value in transition_values
    ):
        raise ValidationError("Witness transition key、私钥和 purpose 必须同时设置")
    transition_key_id = (
        validate_key_id(config.transition_key_id)
        if config.transition_key_id is not None
        else None
    )
    if config.transition_purpose not in (None, AUDIT_RECEIPT_PURPOSE, AUDIT_RECOVERY_PURPOSE):
        raise ValidationError("Witness transition purpose 必须是 audit-receipt 或 audit-recovery")
    if config.expected_endpoint is not None and not config.expected_endpoint:
        raise ValidationError("Witness expected endpoint 不能为空")
    return WitnessConfig(
        storage_root=config.storage_root.expanduser().resolve(),
        client_trust_root=config.client_trust_root.expanduser().resolve(),
        witness_id=witness_id,
        receipt_key_id=receipt_key_id,
        receipt_signing_key=config.receipt_signing_key.expanduser().resolve(),
        record_archive_root=(
            config.record_archive_root.expanduser().resolve()
            if config.record_archive_root is not None
            else None
        ),
        auth_token=config.auth_token,
        workspace_id=workspace_id,
        client_workspace_bindings=bindings,
        expected_endpoint=config.expected_endpoint,
        transition_key_id=transition_key_id,
        transition_signing_key=(
            config.transition_signing_key.expanduser().resolve()
            if config.transition_signing_key is not None
            else None
        ),
        transition_purpose=config.transition_purpose,
    )


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_directory(path: Path, *, root: Path | None = None) -> None:
    """Create a Witness directory tree without following untrusted descendants."""
    if root is None:
        missing_directories: list[Path] = []
        current = path
        while not current.exists():
            missing_directories.append(current)
            current = current.parent
        for missing_directory in reversed(missing_directories):
            missing_directory.mkdir()
            _fsync_directory(missing_directory.parent)
        root = path
    try:
        relative_path = path.relative_to(root)
    except ValueError as exc:
        raise ValidationError(f"Witness 存储路径越出根目录：{path}") from exc
    current = root
    for component in (".", *relative_path.parts):
        if component != ".":
            current = current / component
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            try:
                current.mkdir()
            except FileExistsError:
                pass
            else:
                _fsync_directory(current.parent)
            try:
                mode = current.lstat().st_mode
            except FileNotFoundError:
                raise ValidationError(f"Witness 存储目录发生并发变化：{current}") from None
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise ValidationError(f"Witness 存储路径必须是不含符号链接的普通目录：{current}")
        _fsync_directory(current.parent)


def _read_regular_json(path: Path, label: str) -> dict[str, object]:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        raise ValidationError(f"{label}不存在：{path}") from None
    if not stat.S_ISREG(mode):
        raise ValidationError(f"{label}必须是普通文件：{path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"{label}不是有效 JSON：{path}") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"{label}必须是 JSON 对象：{path}")
    return value


def _write_create_only(
    path: Path,
    payload: dict[str, object],
    *,
    root: Path,
) -> bool:
    _ensure_directory(path.parent, root=root)
    encoded = canonical_json_bytes(payload) + b"\n"
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{path.name}.",
            dir=path.parent,
            delete=False,
        ) as output:
            temporary_name = output.name
            os.fchmod(output.fileno(), 0o600)
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary_name, path, follow_symlinks=False)
        except FileExistsError:
            return False
        _fsync_directory(path.parent)
        return True
    finally:
        try:
            if temporary_name:
                Path(temporary_name).unlink(missing_ok=True)
        except OSError:
            pass


def _write_checkpoint(path: Path, state: dict[str, object]) -> None:
    encoded = json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{path.name}.",
            dir=path.parent,
            delete=False,
        ) as output:
            temporary_name = output.name
            os.fchmod(output.fileno(), 0o600)
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_name, path)
        _fsync_directory(path.parent)
    finally:
        try:
            if temporary_name:
                Path(temporary_name).unlink(missing_ok=True)
        except OSError:
            pass


class WitnessStore:
    """Durable, single-host Witness ledger; mount its root on immutable storage."""

    def __init__(self, config: WitnessConfig) -> None:
        self.config = _validate_config(config)
        _ensure_directory(self.config.storage_root)
        if self.config.record_archive_root is not None:
            _ensure_directory(self.config.record_archive_root)

    def _workspace_directory(self, workspace_id: str) -> Path:
        return self.config.storage_root / "workspaces" / workspace_id / self.config.witness_id

    def _ensure_workspace_directory(self, workspace_id: str) -> Path:
        path = self._workspace_directory(workspace_id)
        _ensure_directory(path, root=self.config.storage_root)
        return path

    def _state_path(self, workspace_id: str) -> Path:
        return self._workspace_directory(workspace_id) / "checkpoint.json"

    def _record_path(self, workspace_id: str, batch_sha256: str) -> Path:
        archive_root = self.config.record_archive_root or self.config.storage_root
        return (
            archive_root
            / "workspaces"
            / workspace_id
            / self.config.witness_id
            / "records"
            / f"{batch_sha256}.json"
        )

    def _ensure_record_directory(self, workspace_id: str) -> Path:
        archive_root = self.config.record_archive_root or self.config.storage_root
        path = self._record_path(workspace_id, "placeholder").parent
        _ensure_directory(path, root=archive_root)
        return path

    def _load_state(self, workspace_id: str) -> dict[str, object]:
        path = self._state_path(workspace_id)
        if not path.exists() and not path.is_symlink():
            return {
                "schema_version": WITNESS_STATE_SCHEMA,
                "workspace_id": workspace_id,
                "witness": self.config.witness_id,
                "sequence": 0,
                "head_sha256": GENESIS_HEAD,
                "receipt_key_id": self.config.receipt_key_id,
                "receipt_key_epoch": 1,
                "recovery_key_id": None,
                "batch_sha256": None,
            }
        state = _read_regular_json(path, "Witness checkpoint")
        if (
            state.get("schema_version") != WITNESS_STATE_SCHEMA
            or state.get("workspace_id") != workspace_id
            or state.get("witness") != self.config.witness_id
            or not isinstance(state.get("sequence"), int)
            or isinstance(state.get("sequence"), bool)
            or int(state["sequence"]) < 0
            or not isinstance(state.get("head_sha256"), str)
            or not isinstance(state.get("receipt_key_id"), str)
            or not isinstance(state.get("receipt_key_epoch"), int)
            or isinstance(state.get("receipt_key_epoch"), bool)
            or int(state["receipt_key_epoch"]) < 1
            or (
                state.get("recovery_key_id") is not None
                and not isinstance(state.get("recovery_key_id"), str)
            )
            or (
                state.get("batch_sha256") is not None
                and not isinstance(state.get("batch_sha256"), str)
            )
        ):
            raise ValidationError(f"Witness checkpoint 格式无效：{path}")
        validate_key_id(str(state["receipt_key_id"]))
        if state.get("recovery_key_id") is not None:
            validate_key_id(str(state["recovery_key_id"]))
        return state

    def _write_state(self, workspace_id: str, state: dict[str, object]) -> None:
        path = self._state_path(workspace_id)
        _ensure_directory(path.parent, root=self.config.storage_root)
        _write_checkpoint(path, state)

    @staticmethod
    def _record_matches_batch(record: dict[str, object], batch: dict[str, object]) -> bool:
        stored = record.get("batch")
        return isinstance(stored, dict) and canonical_json_bytes(stored) == canonical_json_bytes(batch)

    @staticmethod
    def _receipt_matches_state(receipt: dict[str, object], state: dict[str, object]) -> bool:
        return (
            receipt.get("to_sequence") == state["sequence"]
            and receipt.get("head_sha256") == state["head_sha256"]
            and receipt.get("witness_key_id") == state["receipt_key_id"]
            and receipt.get("receipt_key_epoch") == state["receipt_key_epoch"]
            and receipt.get("recovery_key_id") == state["recovery_key_id"]
        )

    def _validate_batch_scope(
        self,
        batch: dict[str, object],
        *,
        workspace_id: str,
    ) -> None:
        if batch.get("witness") != self.config.witness_id:
            raise WitnessRequestError("wrong_witness", "batch witness 不属于当前服务", status_code=409)
        if self.config.workspace_id is not None and workspace_id != self.config.workspace_id:
            raise WitnessRequestError("wrong_workspace", "workspace 未获当前 Witness 授权", status_code=403)
        if (
            self.config.expected_endpoint is not None
            and batch.get("endpoint") != self.config.expected_endpoint
        ):
            raise WitnessRequestError("wrong_endpoint", "batch endpoint 与 Witness 配置不一致", status_code=409)
        verify_record(
            batch,
            purpose=AUDIT_EXPORT_PURPOSE,
            trust_directory=trusted_keys_directory(
                self.config.client_trust_root,
                AUDIT_EXPORT_PURPOSE,
            ),
            required=True,
        )
        if self.config.client_workspace_bindings is not None:
            signer = signature_key_id(batch)
            if self.config.client_workspace_bindings.get(signer) != workspace_id:
                raise WitnessRequestError(
                    "unauthorized_client_workspace",
                    "客户端签名密钥未获当前 workspace 授权",
                    status_code=403,
                )

    def _validate_batch_identity(
        self,
        batch: dict[str, object],
        *,
        workspace_id: str,
        state: dict[str, object],
    ) -> tuple[int, str]:
        try:
            return validate_audit_batch(
                batch,
                workspace_id=workspace_id,
                witness=self.config.witness_id,
                previous_sequence=int(state["sequence"]),
                previous_head=str(state["head_sha256"]),
            )
        except ValidationError as exc:
            raise WitnessRequestError("audit_fork", str(exc), status_code=409) from exc

    def _next_state(
        self,
        batch: dict[str, object],
        *,
        state: dict[str, object],
        sequence: int,
        head: str,
        batch_sha256: str,
    ) -> tuple[dict[str, object], dict[str, object]]:
        requested_key_id = validate_key_id(str(batch.get("requested_witness_key_id", "")))
        if requested_key_id != self.config.receipt_key_id:
            raise WitnessRequestError("unknown_receipt_key", "请求的 receipt key 不属于当前 Witness", status_code=409)
        requested_epoch = batch.get("receipt_key_epoch")
        if not isinstance(requested_epoch, int) or isinstance(requested_epoch, bool):
            raise WitnessRequestError("invalid_key_epoch", "receipt key epoch 无效")
        current_key_id = str(state["receipt_key_id"])
        current_epoch = int(state["receipt_key_epoch"])
        rotating = requested_key_id != current_key_id
        expected_epoch = current_epoch + 1 if rotating else current_epoch
        if requested_epoch != expected_epoch:
            raise WitnessRequestError("key_epoch_mismatch", "receipt key epoch 与当前 Witness 不一致", status_code=409)
        recovery_key_id = batch.get("recovery_key_id")
        if recovery_key_id is not None:
            recovery_key_id = validate_key_id(str(recovery_key_id))
        if state.get("recovery_key_id") is not None and recovery_key_id != state.get("recovery_key_id"):
            raise WitnessRequestError("recovery_key_mismatch", "recovery key 与当前 Witness 不一致", status_code=409)
        unsigned: dict[str, object] = {
            "schema_version": 1,
            "type": AUDIT_RECEIPT_TYPE,
            "witness": self.config.witness_id,
            "workspace_id": batch["workspace_id"],
            "from_sequence": batch["from_sequence"],
            "to_sequence": sequence,
            "head_sha256": head,
            "batch_sha256": batch_sha256,
            "witness_key_id": requested_key_id,
            "recovery_key_id": recovery_key_id,
            "receipt_key_epoch": requested_epoch,
            "accepted_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        }
        if rotating:
            if (
                self.config.transition_key_id is None
                or self.config.transition_signing_key is None
                or self.config.transition_purpose is None
            ):
                raise WitnessRequestError("transition_unavailable", "Witness 未配置 receipt key transition", status_code=409)
            allowed_transition_key = (
                current_key_id
                if self.config.transition_purpose == AUDIT_RECEIPT_PURPOSE
                else state.get("recovery_key_id")
            )
            if self.config.transition_key_id != allowed_transition_key:
                raise WitnessRequestError("transition_unauthorized", "Witness transition signer 不匹配", status_code=409)
            unsigned["key_transition"] = sign_record(
                {
                    "schema_version": 1,
                    "type": AUDIT_KEY_TRANSITION_TYPE,
                    "witness": self.config.witness_id,
                    "workspace_id": batch["workspace_id"],
                    "sequence": sequence,
                    "head_sha256": head,
                    "batch_sha256": batch_sha256,
                    "previous_key_id": current_key_id,
                    "next_key_id": requested_key_id,
                    "previous_receipt_key_epoch": current_epoch,
                    "next_receipt_key_epoch": requested_epoch,
                },
                purpose=self.config.transition_purpose,
                key_id=self.config.transition_key_id,
                private_key=self.config.transition_signing_key,
            )
        receipt = sign_record(
            unsigned,
            purpose=AUDIT_RECEIPT_PURPOSE,
            key_id=requested_key_id,
            private_key=self.config.receipt_signing_key,
        )
        next_state = {
            "schema_version": WITNESS_STATE_SCHEMA,
            "workspace_id": batch["workspace_id"],
            "witness": self.config.witness_id,
            "sequence": sequence,
            "head_sha256": head,
            "receipt_key_id": requested_key_id,
            "receipt_key_epoch": requested_epoch,
            "recovery_key_id": recovery_key_id,
            "batch_sha256": batch_sha256,
        }
        return receipt, next_state

    def _stored_receipt_matches(
        self,
        receipt: dict[str, object],
        batch: dict[str, object],
        *,
        next_state: dict[str, object],
        batch_sha256: str,
        rotating: bool,
    ) -> bool:
        expected = {
            "schema_version": 1,
            "type": AUDIT_RECEIPT_TYPE,
            "witness": self.config.witness_id,
            "workspace_id": batch["workspace_id"],
            "from_sequence": batch["from_sequence"],
            "to_sequence": next_state["sequence"],
            "head_sha256": next_state["head_sha256"],
            "batch_sha256": batch_sha256,
            "witness_key_id": next_state["receipt_key_id"],
            "recovery_key_id": next_state["recovery_key_id"],
            "receipt_key_epoch": next_state["receipt_key_epoch"],
        }
        if any(receipt.get(field) != value for field, value in expected.items()):
            return False
        if not isinstance(receipt.get("accepted_at"), str):
            return False
        if signature_key_id(receipt) != self.config.receipt_key_id:
            return False
        if rotating:
            transition = receipt.get("key_transition")
            if (
                not isinstance(transition, dict)
                or self.config.transition_key_id is None
                or self.config.transition_signing_key is None
                or self.config.transition_purpose is None
            ):
                return False
            transition_unsigned = dict(transition)
            transition_unsigned.pop("signature", None)
            expected_transition = sign_record(
                transition_unsigned,
                purpose=self.config.transition_purpose,
                key_id=self.config.transition_key_id,
                private_key=self.config.transition_signing_key,
            )
            if canonical_json_bytes(expected_transition) != canonical_json_bytes(transition):
                return False
        receipt_unsigned = dict(receipt)
        receipt_unsigned.pop("signature", None)
        expected_receipt = sign_record(
            receipt_unsigned,
            purpose=AUDIT_RECEIPT_PURPOSE,
            key_id=self.config.receipt_key_id,
            private_key=self.config.receipt_signing_key,
        )
        return canonical_json_bytes(expected_receipt) == canonical_json_bytes(receipt)

    def accept(
        self,
        batch: dict[str, object],
        *,
        idempotency_key: str,
    ) -> WitnessAcceptance:
        if not isinstance(batch, dict):
            raise WitnessRequestError("invalid_batch", "batch 必须是 JSON 对象")
        body = canonical_json_bytes(batch)
        batch_sha256 = hashlib.sha256(body).hexdigest()
        if not hmac.compare_digest(idempotency_key, batch_sha256):
            raise WitnessRequestError("idempotency_mismatch", "Idempotency-Key 与 batch 不匹配")
        workspace_id = _validate_workspace_id(batch.get("workspace_id"))
        self._validate_batch_scope(batch, workspace_id=workspace_id)
        workspace_directory = self._ensure_workspace_directory(workspace_id)
        lock_path = workspace_directory / ".lock"
        if lock_path.is_symlink():
            raise ValidationError(f"Witness 状态锁不能是符号链接：{lock_path}")
        with exclusive_lock(lock_path):
            state = self._load_state(workspace_id)
            self._ensure_record_directory(workspace_id)
            record_path = self._record_path(workspace_id, batch_sha256)
            record = (
                _read_regular_json(record_path, "Witness batch record")
                if record_path.exists() or record_path.is_symlink()
                else None
            )
            if record is not None and not self._record_matches_batch(record, batch):
                raise WitnessRequestError("record_collision", "Witness batch record 与请求不一致", status_code=409)
            if record is not None:
                _fsync_directory(record_path.parent)
                receipt = record.get("receipt")
                if not isinstance(receipt, dict):
                    raise ValidationError(f"Witness batch record 格式无效：{record_path}")
                if (
                    self._receipt_matches_state(receipt, state)
                ):
                    _fsync_directory(self._state_path(workspace_id).parent)
                    return WitnessAcceptance(receipt=receipt, created=False)
            sequence, head = self._validate_batch_identity(
                batch,
                workspace_id=workspace_id,
                state=state,
            )
            if record is not None:
                _, recovered_state = self._next_state(
                    batch,
                    state=state,
                    sequence=sequence,
                    head=head,
                    batch_sha256=batch_sha256,
                )
                if not self._stored_receipt_matches(
                    receipt,
                    batch,
                    next_state=recovered_state,
                    batch_sha256=batch_sha256,
                    rotating=(
                        batch.get("requested_witness_key_id")
                        != state["receipt_key_id"]
                    ),
                ):
                    raise WitnessRequestError("record_recovery_mismatch", "Witness 未完成记录无法安全恢复", status_code=409)
                self._write_state(workspace_id, recovered_state)
                return WitnessAcceptance(receipt=receipt, created=False)
            receipt, next_state = self._next_state(
                batch,
                state=state,
                sequence=sequence,
                head=head,
                batch_sha256=batch_sha256,
            )
            record_created = _write_create_only(
                record_path,
                {
                    "schema_version": WITNESS_RECORD_SCHEMA,
                    "batch_sha256": batch_sha256,
                    "batch": batch,
                    "receipt": receipt,
                },
                root=self.config.record_archive_root or self.config.storage_root,
            )
            if not record_created:
                raise WitnessRequestError("record_race", "Witness batch record 已被其他写入者创建", status_code=409)
            self._write_state(workspace_id, next_state)
            return WitnessAcceptance(receipt=receipt, created=True)


def _handler(store: WitnessStore) -> type[BaseHTTPRequestHandler]:
    class WitnessHandler(BaseHTTPRequestHandler):
        server_version = "DyroWitness/1"

        def _send(self, status: int, payload: dict[str, object]) -> None:
            body = canonical_json_bytes(payload)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            expected = store.config.auth_token
            if expected is None:
                return True
            supplied = self.headers.get("Authorization", "")
            return hmac.compare_digest(supplied, f"Bearer {expected}")

        def do_GET(self) -> None:
            if self.path != "/healthz":
                self._send(404, {"code": "not_found", "error": "not found"})
                return
            self._send(200, {"status": "ok", "witness": store.config.witness_id})

        def do_POST(self) -> None:
            if self.path != WITNESS_PATH:
                self._send(404, {"code": "not_found", "error": "not found"})
                return
            if not self._authorized():
                self._send(401, {"code": "unauthorized", "error": "unauthorized"})
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self._send(411, {"code": "length_required", "error": "Content-Length required"})
                return
            if length < 1 or length > MAX_RESPONSE_BYTES:
                self._send(413, {"code": "payload_too_large", "error": "batch exceeds limit"})
                return
            try:
                body = self.rfile.read(length)
            except TimeoutError:
                self._send(408, {"code": "read_timeout", "error": "request read timed out"})
                return
            if len(body) != length:
                self._send(400, {"code": "truncated_body", "error": "incomplete request body"})
                return
            try:
                batch: Any = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send(400, {"code": "invalid_json", "error": "batch must be JSON"})
                return
            if not isinstance(batch, dict) or body != canonical_json_bytes(batch):
                self._send(400, {"code": "non_canonical_batch", "error": "batch must use canonical JSON"})
                return
            try:
                accepted = store.accept(
                    batch,
                    idempotency_key=self.headers.get("Idempotency-Key", ""),
                )
            except WitnessRequestError as exc:
                self._send(exc.status_code, {"code": exc.code, "error": str(exc)})
                return
            except ValidationError as exc:
                self._send(400, {"code": "invalid_batch", "error": str(exc)})
                return
            self._send(201 if accepted.created else 200, accepted.receipt)

        def log_message(self, format: str, *args: object) -> None:
            return

    return WitnessHandler


class _BoundedWitnessHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        *,
        max_concurrent_requests: int,
        read_timeout_seconds: float,
        ssl_context: ssl.SSLContext | None,
    ) -> None:
        self._request_slots = threading.BoundedSemaphore(max_concurrent_requests)
        self._read_timeout_seconds = read_timeout_seconds
        self._ssl_context = ssl_context
        super().__init__(address, handler)

    def get_request(self) -> tuple[Any, tuple[str, int]]:
        request, client_address = super().get_request()
        request.settimeout(self._read_timeout_seconds)
        return request, client_address

    def process_request(self, request: Any, client_address: tuple[str, int]) -> None:
        if not self._request_slots.acquire(blocking=False):
            request.close()
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request: Any, client_address: tuple[str, int]) -> None:
        active_request = request
        handed_off = False
        deadline_timer: threading.Timer | None = None
        try:
            if self._ssl_context is not None:
                active_request = self._ssl_context.wrap_socket(
                    request,
                    server_side=True,
                    do_handshake_on_connect=False,
                )
            active_request.settimeout(self._read_timeout_seconds)
            deadline_timer = threading.Timer(
                self._read_timeout_seconds,
                self._terminate_request,
                args=(active_request,),
            )
            deadline_timer.daemon = True
            deadline_timer.start()
            try:
                if self._ssl_context is not None:
                    active_request.do_handshake()
                super().process_request_thread(active_request, client_address)
                handed_off = True
            finally:
                deadline_timer.cancel()
        except (OSError, ssl.SSLError, TimeoutError):
            if not handed_off:
                try:
                    active_request.close()
                except OSError:
                    pass
        finally:
            if deadline_timer is not None:
                deadline_timer.cancel()
            self._request_slots.release()

    @staticmethod
    def _terminate_request(request: Any) -> None:
        try:
            request.shutdown(socket.SHUT_RDWR)
        except (AttributeError, OSError):
            pass
        try:
            request.close()
        except (AttributeError, OSError):
            pass


def create_witness_http_server(
    config: WitnessConfig,
    *,
    host: str,
    port: int,
    max_concurrent_requests: int = DEFAULT_MAX_CONCURRENT_REQUESTS,
    read_timeout_seconds: float = DEFAULT_READ_TIMEOUT_SECONDS,
    ssl_context: ssl.SSLContext | None = None,
) -> ThreadingHTTPServer:
    if max_concurrent_requests < 1:
        raise ValidationError("Witness max concurrent requests 必须大于零")
    if not math.isfinite(read_timeout_seconds) or read_timeout_seconds <= 0:
        raise ValidationError("Witness read timeout 必须大于零")
    return _BoundedWitnessHTTPServer(
        (host, port),
        _handler(WitnessStore(config)),
        max_concurrent_requests=max_concurrent_requests,
        read_timeout_seconds=read_timeout_seconds,
        ssl_context=ssl_context,
    )


def serve_witness(
    config: WitnessConfig,
    *,
    host: str,
    port: int,
    tls_cert: Path | None = None,
    tls_key: Path | None = None,
    max_concurrent_requests: int = DEFAULT_MAX_CONCURRENT_REQUESTS,
    read_timeout_seconds: float = DEFAULT_READ_TIMEOUT_SECONDS,
    on_listening: Callable[[], None] | None = None,
) -> None:
    if (tls_cert is None) != (tls_key is None):
        raise ValidationError("Witness TLS cert 与 key 必须同时设置")
    public_listener = host not in {"127.0.0.1", "::1", "localhost"}
    if tls_cert is None and public_listener:
        raise ValidationError("未设置 TLS 的 Witness 只能绑定 loopback host")
    if config.auth_token is None and public_listener:
        raise ValidationError("未认证 Witness 只能绑定 loopback host")
    ssl_context: ssl.SSLContext | None = None
    if tls_cert is not None and tls_key is not None:
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_context.load_cert_chain(tls_cert, tls_key)
    server = create_witness_http_server(
        config,
        host=host,
        port=port,
        max_concurrent_requests=max_concurrent_requests,
        read_timeout_seconds=read_timeout_seconds,
        ssl_context=ssl_context,
    )
    try:
        if on_listening is not None:
            on_listening()
        server.serve_forever()
    finally:
        server.server_close()

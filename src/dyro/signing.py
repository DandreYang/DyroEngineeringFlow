from __future__ import annotations

import base64
import binascii
from datetime import datetime, timezone
import hashlib
import json
import os
import re
from pathlib import Path
import stat
import tempfile

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .canonical import canonical_json_bytes
from .errors import ValidationError

SIGNATURE_ALGORITHM = "ed25519"
TRUST_PURPOSES = ("execution", "review", "signoff")
KEY_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
KEY_METADATA_SUFFIX = ".metadata.json"
KEY_REVOCATION_SUFFIX = ".revoked.json"
TRUST_AUDIT_FILE = "audit.jsonl"


def validate_key_id(key_id: str) -> str:
    key_id = key_id.strip()
    if not KEY_ID_PATTERN.fullmatch(key_id):
        raise ValidationError("key ID 只能包含字母、数字、点、下划线和连字符，最长 64 字符")
    return key_id


def validate_purpose(purpose: str) -> str:
    if purpose not in TRUST_PURPOSES:
        raise ValidationError(f"签名用途必须是：{', '.join(TRUST_PURPOSES)}")
    return purpose


def trusted_keys_directory(root: Path, purpose: str) -> Path:
    return root / ".dyro" / "trust" / "ed25519" / validate_purpose(purpose)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_effective_at(value: str | None, label: str) -> datetime | None:
    if value is None:
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValidationError(f"{label} 必须是带时区的 ISO-8601 时间") from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{label} 必须包含时区")
    return parsed.astimezone(timezone.utc)


def _format_effective_at(value: datetime | None) -> str:
    return value.isoformat(timespec="seconds") if value is not None else ""


def _ensure_real_directory(path: Path) -> None:
    resolved_root = path.anchor
    current = Path(resolved_root) if resolved_root else Path()
    parts = path.parts[1:] if resolved_root else path.parts
    for part in parts:
        current /= part
        if current.exists() or current.is_symlink():
            try:
                mode = current.lstat().st_mode
            except FileNotFoundError:
                raise ValidationError(f"信任库目录状态发生并发变化：{current}") from None
            if not stat.S_ISDIR(mode):
                raise ValidationError(f"信任库路径必须是普通目录且不能是符号链接：{current}")
            continue
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            mode = current.lstat().st_mode
            if not stat.S_ISDIR(mode):
                raise ValidationError(f"信任库路径必须是普通目录且不能是符号链接：{current}") from None


def _read_regular_file(path: Path, label: str) -> bytes:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        raise ValidationError(f"{label}不存在：{path}") from None
    if not stat.S_ISREG(mode):
        raise ValidationError(f"{label}必须是普通文件且不能是符号链接：{path}")
    return path.read_bytes()


def _validate_trust_directory(directory: Path) -> None:
    try:
        mode = directory.lstat().st_mode
    except FileNotFoundError:
        raise ValidationError(f"信任库目录不存在：{directory}") from None
    if not stat.S_ISDIR(mode):
        raise ValidationError(f"信任库路径必须是普通目录且不能是符号链接：{directory}")


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_install(path: Path, content: bytes, mode: int = 0o600) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, path, follow_symlinks=False)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _metadata_path(directory: Path, key_id: str) -> Path:
    return directory / f"{key_id}{KEY_METADATA_SUFFIX}"


def _revocation_path(directory: Path, key_id: str) -> Path:
    return directory / f"{key_id}{KEY_REVOCATION_SUFFIX}"


def _read_json_file(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(_read_regular_file(path, label))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"{label}不是有效 JSON：{path}") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"{label}必须是 JSON 对象：{path}")
    return value


def _key_status(
    directory: Path,
    key_id: str,
    *,
    at: datetime | None = None,
) -> tuple[str, dict[str, object] | None]:
    revoked = _revocation_path(directory, key_id)
    if revoked.exists() or revoked.is_symlink():
        record = _read_json_file(revoked, "密钥撤销记录")
        if record.get("schema_version") != 1 or record.get("key_id") != key_id:
            raise ValidationError(f"密钥撤销记录格式无效：{revoked}")
        return "revoked", record
    metadata_path = _metadata_path(directory, key_id)
    if not metadata_path.exists() and not metadata_path.is_symlink():
        return "active", None
    metadata = _read_json_file(metadata_path, "密钥元数据")
    if (
        metadata.get("schema_version") != 1
        or metadata.get("key_id") != key_id
        or metadata.get("purpose") != directory.name
        or not isinstance(metadata.get("fingerprint_sha256"), str)
    ):
        raise ValidationError(f"密钥元数据格式无效：{metadata_path}")
    now = (at or _utc_now()).astimezone(timezone.utc)
    not_before = _parse_effective_at(str(metadata.get("not_before") or "") or None, "not_before")
    not_after = _parse_effective_at(str(metadata.get("not_after") or "") or None, "not_after")
    if not_before is not None and now < not_before:
        return "pending", metadata
    if not_after is not None and now > not_after:
        return "expired", metadata
    return "active", metadata


def trusted_key_records(root: Path, purpose: str) -> tuple[dict[str, object], ...]:
    directory = trusted_keys_directory(root, purpose)
    if not directory.exists() and not directory.is_symlink():
        return ()
    try:
        mode = directory.lstat().st_mode
    except FileNotFoundError:
        return ()
    if not stat.S_ISDIR(mode):
        raise ValidationError(f"信任库路径必须是普通目录且不能是符号链接：{directory}")
    records: list[dict[str, object]] = []
    for path in sorted(directory.glob("*.pem")):
        key_id = validate_key_id(path.stem)
        _read_regular_file(path, "信任公钥")
        status, metadata = _key_status(directory, key_id)
        records.append(
            {
                "key_id": key_id,
                "status": status,
                "not_before": str((metadata or {}).get("not_before", "")),
                "not_after": str((metadata or {}).get("not_after", "")),
            }
        )
    return tuple(records)


def trusted_key_ids(root: Path, purpose: str) -> tuple[str, ...]:
    return tuple(
        str(record["key_id"])
        for record in trusted_key_records(root, purpose)
        if record["status"] == "active"
    )


def _canonical_record(record: dict[str, object]) -> bytes:
    unsigned = dict(record)
    unsigned.pop("signature", None)
    return canonical_json_bytes(unsigned)


def _signature_message(record: dict[str, object], purpose: str) -> bytes:
    return f"dyro/{validate_purpose(purpose)}/v1\0".encode("ascii") + _canonical_record(record)


def _load_private_key(path: Path) -> Ed25519PrivateKey:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        raise ValidationError(f"Ed25519 私钥不存在：{path}") from None
    if not stat.S_ISREG(mode):
        raise ValidationError(f"Ed25519 私钥必须是普通文件且不能是符号链接：{path}")
    if os.name != "nt" and stat.S_IMODE(mode) & 0o077:
        raise ValidationError(f"Ed25519 私钥权限过宽，必须限制为 0600：{path}")
    try:
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    except (OSError, TypeError, ValueError) as exc:
        raise ValidationError(f"Ed25519 私钥无法加载：{path}") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise ValidationError(f"签名密钥不是 Ed25519 私钥：{path}")
    return key


def _load_public_key_bytes(data: bytes, source: Path) -> Ed25519PublicKey:
    try:
        key = serialization.load_pem_public_key(data)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"Ed25519 公钥无法加载：{source}") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise ValidationError(f"信任密钥不是 Ed25519 公钥：{source}")
    return key


def sign_record(
    record: dict[str, object],
    *,
    purpose: str,
    key_id: str,
    private_key: Path,
) -> dict[str, object]:
    if "signature" in record:
        raise ValidationError("记录已经包含 signature，拒绝重复签名")
    key_id = validate_key_id(key_id)
    signature = _load_private_key(private_key).sign(_signature_message(record, purpose))
    signed = dict(record)
    signed["signature"] = {
        "schema_version": 1,
        "algorithm": SIGNATURE_ALGORITHM,
        "purpose": validate_purpose(purpose),
        "key_id": key_id,
        "value": base64.b64encode(signature).decode("ascii"),
    }
    return signed


def verify_record(
    record: dict[str, object],
    *,
    purpose: str,
    trust_directory: Path,
    required: bool = False,
) -> bool:
    purpose = validate_purpose(purpose)
    signature = record.get("signature")
    if signature is None:
        if required:
            raise ValidationError(f"策略要求 {purpose} 记录包含 Ed25519 signature")
        return False
    if not isinstance(signature, dict):
        raise ValidationError("signature 必须是对象")
    if (
        signature.get("schema_version") != 1
        or signature.get("algorithm") != SIGNATURE_ALGORITHM
        or signature.get("purpose") != purpose
        or not isinstance(signature.get("key_id"), str)
        or not isinstance(signature.get("value"), str)
    ):
        raise ValidationError("Ed25519 signature envelope 无效")
    key_id = validate_key_id(str(signature["key_id"]))
    _validate_trust_directory(trust_directory)
    public_key_path = trust_directory / f"{key_id}.pem"
    try:
        public_key_bytes = _read_regular_file(public_key_path, "信任公钥")
    except ValidationError as exc:
        if not public_key_path.exists() and not public_key_path.is_symlink():
            raise ValidationError(f"签名 key ID 未受信任：{key_id}") from exc
        raise
    status, metadata = _key_status(trust_directory, key_id)
    if status != "active":
        raise ValidationError(f"签名 key ID 当前不可用：{key_id} ({status})")
    public_key = _load_public_key_bytes(public_key_bytes, public_key_path)
    normalized = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    if metadata is not None and hashlib.sha256(normalized).hexdigest() != metadata["fingerprint_sha256"]:
        raise ValidationError(f"信任公钥与元数据指纹不匹配：{key_id}")
    try:
        signature_bytes = base64.b64decode(str(signature["value"]), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValidationError("Ed25519 signature 不是有效 Base64") from exc
    try:
        public_key.verify(signature_bytes, _signature_message(record, purpose))
    except InvalidSignature as exc:
        raise ValidationError("Ed25519 signature 验证失败") from exc
    return True


def signature_key_id(record: dict[str, object]) -> str | None:
    signature = record.get("signature")
    if not isinstance(signature, dict) or not isinstance(signature.get("key_id"), str):
        return None
    return validate_key_id(str(signature["key_id"]))


def generate_keypair(key_id: str, *, private_key: Path, public_key: Path) -> None:
    validate_key_id(key_id)
    private_key = private_key.expanduser().resolve()
    public_key = public_key.expanduser().resolve()
    if private_key == public_key:
        raise ValidationError("私钥与公钥输出路径不能相同")
    if private_key.exists() or public_key.exists():
        raise ValidationError("拒绝覆盖已有密钥文件")
    key = Ed25519PrivateKey.generate()
    private_bytes = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_bytes = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    private_key.parent.mkdir(parents=True, exist_ok=True)
    public_key.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(private_key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(private_bytes)
    except Exception:
        private_key.unlink(missing_ok=True)
        raise
    try:
        public_key.write_bytes(public_bytes)
    except Exception:
        private_key.unlink(missing_ok=True)
        public_key.unlink(missing_ok=True)
        raise


def _append_trust_audit(root: Path, record: dict[str, object]) -> None:
    base = root.resolve() / ".dyro" / "trust" / "ed25519"
    _ensure_real_directory(base)
    audit_path = base / TRUST_AUDIT_FILE
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(audit_path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValidationError(f"信任审计日志必须是普通文件：{audit_path}")
        line = (_canonical_record(record) + b"\n")
        os.write(descriptor, line)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(base)


def read_trust_audit(root: Path) -> tuple[dict[str, object], ...]:
    path = root.resolve() / ".dyro" / "trust" / "ed25519" / TRUST_AUDIT_FILE
    if not path.exists() and not path.is_symlink():
        return ()
    records: list[dict[str, object]] = []
    for line_number, line in enumerate(_read_regular_file(path, "信任审计日志").splitlines(), start=1):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"信任审计日志第 {line_number} 行损坏") from exc
        if not isinstance(record, dict):
            raise ValidationError(f"信任审计日志第 {line_number} 行格式无效")
        records.append(record)
    return tuple(records)


def trust_public_key(
    root: Path,
    key_id: str,
    *,
    purpose: str,
    source: Path,
    not_before: str | None = None,
    not_after: str | None = None,
) -> Path:
    key_id = validate_key_id(key_id)
    purpose = validate_purpose(purpose)
    source = source.expanduser().resolve()
    if not source.is_file():
        raise ValidationError(f"公钥文件不存在：{source}")
    key = _load_public_key_bytes(source.read_bytes(), source)
    normalized = key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    effective_from = _parse_effective_at(not_before, "not_before")
    effective_until = _parse_effective_at(not_after, "not_after")
    if effective_from is not None and effective_until is not None and effective_from >= effective_until:
        raise ValidationError("not_before 必须早于 not_after")
    directory = trusted_keys_directory(root.resolve(), purpose)
    _ensure_real_directory(directory)
    target = directory / f"{key_id}.pem"
    if _revocation_path(directory, key_id).exists() or _revocation_path(directory, key_id).is_symlink():
        raise ValidationError(f"key ID 已撤销，不能重新绑定：{key_id}")
    if target.exists() or target.is_symlink():
        if _read_regular_file(target, "信任公钥") == normalized:
            return target
        raise ValidationError(f"key ID 已绑定其他公钥：{key_id}")
    trusted_at = _utc_now().isoformat(timespec="seconds")
    metadata = {
        "schema_version": 1,
        "key_id": key_id,
        "purpose": purpose,
        "fingerprint_sha256": hashlib.sha256(normalized).hexdigest(),
        "not_before": _format_effective_at(effective_from),
        "not_after": _format_effective_at(effective_until),
        "trusted_at": trusted_at,
    }
    metadata_path = _metadata_path(directory, key_id)
    metadata_bytes = (_canonical_record(metadata) + b"\n")
    if metadata_path.exists() or metadata_path.is_symlink():
        existing = _read_json_file(metadata_path, "密钥元数据")
        comparable = dict(existing)
        comparable.pop("trusted_at", None)
        expected = dict(metadata)
        expected.pop("trusted_at", None)
        if comparable != expected:
            raise ValidationError(f"key ID 存在冲突的信任元数据：{key_id}")
        metadata = existing
    else:
        _atomic_install(metadata_path, metadata_bytes)
    try:
        _atomic_install(target, normalized)
    except FileExistsError:
        if _read_regular_file(target, "信任公钥") != normalized:
            raise ValidationError(f"key ID 已绑定其他公钥：{key_id}") from None
    _append_trust_audit(
        root,
        {
            "schema_version": 1,
            "event": "trust",
            "key_id": key_id,
            "purpose": purpose,
            "fingerprint_sha256": hashlib.sha256(normalized).hexdigest(),
            "not_before": metadata.get("not_before", ""),
            "not_after": metadata.get("not_after", ""),
            "occurred_at": trusted_at,
        },
    )
    return target


def revoke_public_key(
    root: Path,
    key_id: str,
    *,
    purpose: str,
    reason: str,
) -> Path:
    key_id = validate_key_id(key_id)
    purpose = validate_purpose(purpose)
    reason = reason.strip()
    if not reason:
        raise ValidationError("撤销原因不能为空")
    directory = trusted_keys_directory(root.resolve(), purpose)
    _ensure_real_directory(directory)
    public_key_path = directory / f"{key_id}.pem"
    public_key_bytes = _read_regular_file(public_key_path, "待撤销信任公钥")
    public_key = _load_public_key_bytes(public_key_bytes, public_key_path)
    normalized = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    revoked_at = _utc_now().isoformat(timespec="seconds")
    record = {
        "schema_version": 1,
        "key_id": key_id,
        "purpose": purpose,
        "fingerprint_sha256": hashlib.sha256(normalized).hexdigest(),
        "reason": reason,
        "revoked_at": revoked_at,
    }
    target = _revocation_path(directory, key_id)
    if target.exists() or target.is_symlink():
        _read_json_file(target, "密钥撤销记录")
        return target
    try:
        _atomic_install(target, _canonical_record(record) + b"\n")
    except FileExistsError:
        _read_json_file(target, "密钥撤销记录")
        return target
    _append_trust_audit(
        root,
        {
            "schema_version": 1,
            "event": "revoke",
            "key_id": key_id,
            "purpose": purpose,
            "fingerprint_sha256": record["fingerprint_sha256"],
            "reason": reason,
            "occurred_at": revoked_at,
        },
    )
    return target

from __future__ import annotations

import base64
import binascii
import json
import os
import re
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .errors import ValidationError

SIGNATURE_ALGORITHM = "ed25519"
TRUST_PURPOSES = ("execution", "review", "signoff")
KEY_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


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


def trusted_key_ids(root: Path, purpose: str) -> tuple[str, ...]:
    directory = trusted_keys_directory(root, purpose)
    if not directory.is_dir():
        return ()
    return tuple(sorted(path.stem for path in directory.glob("*.pem") if path.is_file()))


def _canonical_record(record: dict[str, object]) -> bytes:
    unsigned = dict(record)
    unsigned.pop("signature", None)
    return json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _signature_message(record: dict[str, object], purpose: str) -> bytes:
    return f"dyro/{validate_purpose(purpose)}/v1\0".encode("ascii") + _canonical_record(record)


def _load_private_key(path: Path) -> Ed25519PrivateKey:
    if not path.is_file():
        raise ValidationError(f"Ed25519 私钥不存在：{path}")
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
    public_key_path = trust_directory / f"{key_id}.pem"
    if not public_key_path.is_file():
        raise ValidationError(f"签名 key ID 未受信任：{key_id}")
    public_key = _load_public_key_bytes(public_key_path.read_bytes(), public_key_path)
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


def trust_public_key(root: Path, key_id: str, *, purpose: str, source: Path) -> Path:
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
    target = trusted_keys_directory(root, purpose) / f"{key_id}.pem"
    if target.is_file():
        if target.read_bytes() == normalized:
            return target
        raise ValidationError(f"key ID 已绑定其他公钥：{key_id}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(normalized)
    return target

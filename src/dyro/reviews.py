from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .errors import ValidationError
from .signing import (
    sign_record,
    signature_key_id,
    trusted_key_principal_from_directory,
    verify_record,
)


@dataclass(frozen=True)
class ReviewEvidence:
    content: bytes
    reviewer: str
    key_id: str | None
    principal_id: str | None
    signed: bool


def build_signed_review_record(
    task_id: str,
    *,
    reviewer: str,
    review_content: bytes,
    signing_key: Path,
    key_id: str,
) -> dict[str, object]:
    reviewer = reviewer.strip()
    if not reviewer:
        raise ValidationError("reviewer 不能为空")
    try:
        review_text = review_content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError("review 文件必须是 UTF-8") from exc
    record: dict[str, object] = {
        "schema_version": 1,
        "type": "dyro.review",
        "task_id": task_id,
        "reviewer": reviewer,
        "actor": reviewer,
        "review_sha256": hashlib.sha256(review_content).hexdigest(),
        "review_text": review_text,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    return sign_record(
        record,
        purpose="review",
        key_id=key_id,
        private_key=signing_key,
    )


def load_review_evidence(
    path: Path,
    *,
    task_id: str,
    trust_directory: Path,
    require_signature: bool,
) -> ReviewEvidence:
    if not path.is_file():
        raise ValidationError(f"review 证据文件不存在：{path}")
    raw = path.read_bytes()
    try:
        decoded = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        decoded = None
    if not isinstance(decoded, dict) or decoded.get("type") != "dyro.review":
        if require_signature:
            raise ValidationError("策略要求导入 signed review JSON")
        return ReviewEvidence(raw, "", None, None, False)
    verify_record(
        decoded,
        purpose="review",
        trust_directory=trust_directory,
        required=require_signature,
    )
    if (
        decoded.get("schema_version") != 1
        or decoded.get("task_id") != task_id
        or not isinstance(decoded.get("reviewer"), str)
        or not str(decoded["reviewer"]).strip()
        or not isinstance(decoded.get("review_text"), str)
        or not isinstance(decoded.get("review_sha256"), str)
    ):
        raise ValidationError("signed review 身份或内容字段无效")
    key_id = signature_key_id(decoded)
    if key_id is None:
        raise ValidationError("signed review 缺少 signature key ID")
    principal_id = trusted_key_principal_from_directory(trust_directory, key_id)
    if decoded.get("actor") != decoded["reviewer"] or str(decoded["reviewer"]).strip() != principal_id:
        raise ValidationError("signed review actor 必须等于 review key 的 principal")
    content = str(decoded["review_text"]).encode("utf-8")
    if hashlib.sha256(content).hexdigest() != decoded["review_sha256"]:
        raise ValidationError("signed review 的 review_sha256 不匹配")
    return ReviewEvidence(
        content=content,
        reviewer=str(decoded["reviewer"]).strip(),
        key_id=key_id,
        principal_id=principal_id,
        signed="signature" in decoded,
    )

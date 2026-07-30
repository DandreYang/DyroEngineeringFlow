"""Signed, release-bound acceptance evidence for the production gate.

This module does not manufacture production readiness. It verifies evidence
created outside the runtime by four independently trusted roles and returns a
typed result that the read-only production gate can consume.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from types import MappingProxyType
from typing import Mapping, Sequence
from urllib.parse import urlsplit

from dyro import __version__
from dyro.canonical import canonical_json_bytes
from dyro.errors import ValidationError
from dyro.signing import (
    signature_key_id,
    trusted_key_fingerprint,
    trusted_keys_directory,
    verify_record,
)

from ..sandbox import BUN_IMAGE


RELEASE_PURPOSE = "production-release"
ATTESTATION_PURPOSES = MappingProxyType(
    {
        "PROD-01": "production-security",
        "PROD-02": "production-provider",
        "PROD-09": "production-quota",
    }
)

_MAX_DOCUMENT_BYTES = 256 * 1024
_MAX_ATTESTATION_AGE = timedelta(days=31)
_CLOCK_SKEW = timedelta(minutes=5)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_PROVIDER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_DURABLE_EVIDENCE_SCHEMES = frozenset({"https", "s3", "gs", "az", "urn"})

_MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "release_id",
        "environment_id",
        "dyro_version",
        "source_commit",
        "runtime_image",
        "artifacts",
        "providers",
        "operations",
        "created_at",
        "signature",
    }
)
_ARTIFACT_KEYS = frozenset(
    {
        "wheel_sha256",
        "sdist_sha256",
        "sbom_sha256",
        "provenance_sha256",
    }
)
_OPERATIONS_KEYS = frozenset(
    {
        "deployment_sha256",
        "canary_plan_sha256",
        "rollback_plan_sha256",
        "observability_plan_sha256",
        "runbook_sha256",
    }
)
_ATTESTATION_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "check_id",
        "release_manifest_sha256",
        "environment_id",
        "verdict",
        "issued_at",
        "expires_at",
        "evidence",
        "assertions",
        "signature",
    }
)
_SIGNATURE_KEYS = frozenset(
    {"schema_version", "algorithm", "purpose", "key_id", "value"}
)
_EVIDENCE_KEYS = frozenset({"uri", "sha256", "summary"})
_ASSERTION_KEYS = MappingProxyType(
    {
        "PROD-01": frozenset(
            {
                "multi_host_escape_tested",
                "tenant_boundary_tested",
                "orchestrator_policy_verified",
                "kernel_hardening_verified",
                "storage_isolation_verified",
                "network_isolation_verified",
                "high_findings_open",
                "critical_findings_open",
            }
        ),
        "PROD-02": frozenset(
            {
                "provider_binary_pins_verified",
                "broker_only_credentials_verified",
                "credential_rotation_tested",
                "credential_revocation_tested",
                "failure_recovery_tested",
                "canary_runs",
                "high_findings_open",
                "critical_findings_open",
            }
        ),
        "PROD-09": frozenset(
            {
                "all_writable_mounts_declared",
                "byte_limits_enforced",
                "inode_limits_enforced",
                "file_count_limits_enforced",
                "exhaustion_tested",
                "concurrent_tenant_tested",
                "writable_mount_count",
                "high_findings_open",
                "critical_findings_open",
            }
        ),
    }
)
_PASS_BOOLEAN_ASSERTIONS = MappingProxyType(
    {
        "PROD-01": (
            "multi_host_escape_tested",
            "tenant_boundary_tested",
            "orchestrator_policy_verified",
            "kernel_hardening_verified",
            "storage_isolation_verified",
            "network_isolation_verified",
        ),
        "PROD-02": (
            "provider_binary_pins_verified",
            "broker_only_credentials_verified",
            "credential_rotation_tested",
            "credential_revocation_tested",
            "failure_recovery_tested",
        ),
        "PROD-09": (
            "all_writable_mounts_declared",
            "byte_limits_enforced",
            "inode_limits_enforced",
            "file_count_limits_enforced",
            "exhaustion_tested",
            "concurrent_tenant_tested",
        ),
    }
)
_COUNT_ASSERTIONS = MappingProxyType(
    {
        "PROD-01": ("high_findings_open", "critical_findings_open"),
        "PROD-02": (
            "canary_runs",
            "high_findings_open",
            "critical_findings_open",
        ),
        "PROD-09": (
            "writable_mount_count",
            "high_findings_open",
            "critical_findings_open",
        ),
    }
)


@dataclass(frozen=True)
class VerifiedProductionAttestation:
    check_id: str
    verdict: str
    signer_key_id: str
    signer_fingerprint: str
    issued_at: str
    expires_at: str
    evidence_count: int

    def to_mapping(self) -> dict[str, object]:
        return {
            "check_id": self.check_id,
            "verdict": self.verdict,
            "signer_key_id": self.signer_key_id,
            "signer_fingerprint_sha256": self.signer_fingerprint,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "evidence_count": self.evidence_count,
        }


@dataclass(frozen=True)
class VerifiedProductionAcceptance:
    release_id: str
    environment_id: str
    source_commit: str
    release_manifest_sha256: str
    release_signer_key_id: str
    release_signer_fingerprint: str
    attestations: Mapping[str, VerifiedProductionAttestation]
    signer_fingerprints: Mapping[str, str]

    @property
    def missing_checks(self) -> tuple[str, ...]:
        return tuple(
            check_id
            for check_id in ATTESTATION_PURPOSES
            if check_id not in self.attestations
        )

    def to_mapping(self) -> dict[str, object]:
        signers: dict[str, dict[str, str]] = {
            RELEASE_PURPOSE: {
                "key_id": self.release_signer_key_id,
                "fingerprint_sha256": self.release_signer_fingerprint,
            }
        }
        for check_id, attestation in self.attestations.items():
            signers[ATTESTATION_PURPOSES[check_id]] = {
                "key_id": attestation.signer_key_id,
                "fingerprint_sha256": attestation.signer_fingerprint,
            }
        return {
            "provided": True,
            "release_manifest_verified": True,
            "release_id": self.release_id,
            "environment_id": self.environment_id,
            "source_commit": self.source_commit,
            "release_manifest_sha256": self.release_manifest_sha256,
            "signers": signers,
            "attestations": {
                check_id: attestation.to_mapping()
                for check_id, attestation in self.attestations.items()
            },
            "missing_checks": list(self.missing_checks),
            "required_signing_purposes": {
                "release": RELEASE_PURPOSE,
                **dict(ATTESTATION_PURPOSES),
            },
            "schemas": {
                "release_manifest": (
                    "schemas/production-deployment-manifest.schema.json"
                ),
                "attestation": ("schemas/production-attestation.schema.json"),
            },
        }


def _reject_json_constant(value: str) -> object:
    raise ValidationError(f"生产验收 JSON 不允许非有限数值：{value}")


def _reject_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError(f"生产验收 JSON 包含重复字段：{key}")
        result[key] = value
    return result


def _read_bounded_json(path: Path, label: str) -> dict[str, object]:
    path = path.expanduser()
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        raise ValidationError(f"{label}不存在：{path}") from None
    except OSError as exc:
        raise ValidationError(f"{label}无法安全检查：{path}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise ValidationError(f"{label}必须是普通文件且不能是符号链接：{path}")
    if metadata.st_size > _MAX_DOCUMENT_BYTES:
        raise ValidationError(f"{label}超过 {_MAX_DOCUMENT_BYTES} 字节上限：{path}")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValidationError(f"{label}无法安全打开：{path}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValidationError(f"{label}必须是普通文件且不能是符号链接：{path}")
        if before.st_dev != metadata.st_dev or before.st_ino != metadata.st_ino:
            raise ValidationError(f"{label}在打开前被替换：{path}")
        if before.st_size > _MAX_DOCUMENT_BYTES:
            raise ValidationError(f"{label}超过 {_MAX_DOCUMENT_BYTES} 字节上限：{path}")
        chunks: list[bytes] = []
        remaining = _MAX_DOCUMENT_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > _MAX_DOCUMENT_BYTES:
            raise ValidationError(f"{label}超过 {_MAX_DOCUMENT_BYTES} 字节上限：{path}")
        after = os.fstat(descriptor)
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or after.st_size != len(data)
            or before.st_mtime_ns != after.st_mtime_ns
        ):
            raise ValidationError(f"{label}读取期间发生变化：{path}")
    finally:
        os.close(descriptor)
    try:
        value = json.loads(
            data,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValidationError(f"{label}不是有效且有界的 UTF-8 JSON：{path}") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"{label}必须是 JSON 对象：{path}")
    return value


def _require_exact_keys(
    value: Mapping[str, object],
    expected: frozenset[str],
    label: str,
) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValidationError(
            f"{label}字段不完整或包含未知字段；missing={missing}, extra={extra}"
        )


def _require_string(
    value: object,
    label: str,
    *,
    maximum: int = 4096,
) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{label}必须是字符串")
    normalized = value.strip()
    if not normalized or normalized != value or len(value) > maximum:
        raise ValidationError(f"{label}不能为空、不能有首尾空白且长度必须受限")
    return value


def _require_sha256(value: object, label: str) -> str:
    text = _require_string(value, label, maximum=64)
    if not _SHA256.fullmatch(text):
        raise ValidationError(f"{label}必须是小写 SHA-256")
    return text


def _parse_timestamp(value: object, label: str) -> datetime:
    text = _require_string(value, label, maximum=64)
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValidationError(f"{label}必须是带时区的 ISO-8601 时间") from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{label}必须包含时区")
    return parsed.astimezone(timezone.utc)


def _validate_signature_shape(
    record: Mapping[str, object],
    *,
    purpose: str,
    label: str,
) -> None:
    signature = record.get("signature")
    if not isinstance(signature, dict):
        raise ValidationError(f"{label}.signature 必须是对象")
    _require_exact_keys(signature, _SIGNATURE_KEYS, f"{label}.signature")
    if signature.get("purpose") != purpose:
        raise ValidationError(f"{label}.signature purpose 必须是 {purpose}")


def _validate_hash_mapping(
    value: object,
    *,
    expected_keys: frozenset[str] | None,
    label: str,
) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValidationError(f"{label}必须是对象")
    if expected_keys is not None:
        _require_exact_keys(value, expected_keys, label)
    elif not value or len(value) > 16:
        raise ValidationError(f"{label}必须包含 1 至 16 个条目")
    result: dict[str, str] = {}
    for key, digest in value.items():
        if not isinstance(key, str) or (
            expected_keys is None and not _PROVIDER_ID.fullmatch(key)
        ):
            raise ValidationError(f"{label}包含无效条目 ID")
        result[key] = _require_sha256(digest, f"{label}.{key}")
    return result


def _validate_release_manifest(
    manifest: dict[str, object],
    *,
    now: datetime,
) -> None:
    _require_exact_keys(manifest, _MANIFEST_KEYS, "发布清单")
    if (
        type(manifest.get("schema_version")) is not int
        or manifest.get("schema_version") != 1
        or manifest.get("kind") != "dyro-production-deployment-manifest"
    ):
        raise ValidationError("发布清单 kind/schema_version 不受支持")
    for field in ("release_id", "environment_id"):
        value = _require_string(manifest.get(field), f"发布清单.{field}", maximum=128)
        if not _IDENTIFIER.fullmatch(value):
            raise ValidationError(f"发布清单.{field} 格式无效")
    version = _require_string(
        manifest.get("dyro_version"),
        "发布清单.dyro_version",
        maximum=64,
    )
    if version != __version__:
        raise ValidationError(
            f"发布清单 dyro_version={version} 与当前版本 {__version__} 不一致"
        )
    source_commit = _require_string(
        manifest.get("source_commit"),
        "发布清单.source_commit",
        maximum=64,
    )
    if not _SOURCE_COMMIT.fullmatch(source_commit):
        raise ValidationError("发布清单.source_commit 必须是完整 Git SHA")
    if manifest.get("runtime_image") != BUN_IMAGE:
        raise ValidationError("发布清单.runtime_image 与获批 digest 不一致")
    _validate_hash_mapping(
        manifest.get("artifacts"),
        expected_keys=_ARTIFACT_KEYS,
        label="发布清单.artifacts",
    )
    _validate_hash_mapping(
        manifest.get("providers"),
        expected_keys=None,
        label="发布清单.providers",
    )
    _validate_hash_mapping(
        manifest.get("operations"),
        expected_keys=_OPERATIONS_KEYS,
        label="发布清单.operations",
    )
    created_at = _parse_timestamp(manifest.get("created_at"), "发布清单.created_at")
    if created_at > now + _CLOCK_SKEW:
        raise ValidationError("发布清单.created_at 位于未来")
    _validate_signature_shape(
        manifest,
        purpose=RELEASE_PURPOSE,
        label="发布清单",
    )


def _validate_evidence_entry(
    value: object,
    *,
    check_id: str,
    index: int,
) -> None:
    label = f"{check_id}.evidence[{index}]"
    if not isinstance(value, dict):
        raise ValidationError(f"{label}必须是对象")
    _require_exact_keys(value, _EVIDENCE_KEYS, label)
    uri = _require_string(value.get("uri"), f"{label}.uri", maximum=2048)
    if any(character.isspace() for character in uri):
        raise ValidationError(f"{label}.uri 不能包含空白")
    parsed = urlsplit(uri)
    if (
        parsed.scheme not in _DURABLE_EVIDENCE_SCHEMES
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise ValidationError(
            f"{label}.uri 必须是无凭据、query 与 fragment 的持久证据 URI"
        )
    if parsed.scheme == "https" and not parsed.hostname:
        raise ValidationError(f"{label}.uri 缺少 HTTPS host")
    if parsed.scheme in {"s3", "gs", "az"} and not parsed.netloc:
        raise ValidationError(f"{label}.uri 缺少存储容器")
    if parsed.scheme == "urn" and not parsed.path:
        raise ValidationError(f"{label}.uri 缺少 URN 标识")
    try:
        parsed.port
    except ValueError as exc:
        raise ValidationError(f"{label}.uri port 无效") from exc
    _require_sha256(value.get("sha256"), f"{label}.sha256")
    _require_string(value.get("summary"), f"{label}.summary", maximum=512)


def _validate_assertions(
    value: object,
    *,
    check_id: str,
    verdict: str,
) -> None:
    if not isinstance(value, dict):
        raise ValidationError(f"{check_id}.assertions 必须是对象")
    _require_exact_keys(
        value,
        _ASSERTION_KEYS[check_id],
        f"{check_id}.assertions",
    )
    for field in _PASS_BOOLEAN_ASSERTIONS[check_id]:
        if type(value.get(field)) is not bool:
            raise ValidationError(f"{check_id}.{field} 必须是布尔值")
        if verdict == "pass" and value[field] is not True:
            raise ValidationError(f"{check_id} pass 验收要求 {field}=true")
    for field in _COUNT_ASSERTIONS[check_id]:
        count = value.get(field)
        if type(count) is not int or count < 0:
            raise ValidationError(f"{check_id}.{field} 必须是非负整数")
    if verdict == "pass":
        for field in ("high_findings_open", "critical_findings_open"):
            if value[field] != 0:
                raise ValidationError(f"{check_id} pass 验收要求 {field}=0")
        minimum_field = {
            "PROD-02": "canary_runs",
            "PROD-09": "writable_mount_count",
        }.get(check_id)
        if minimum_field is not None and value[minimum_field] < 1:
            raise ValidationError(f"{check_id} pass 验收要求 {minimum_field}>=1")


def _verify_attestation(
    attestation: dict[str, object],
    *,
    root: Path,
    manifest: Mapping[str, object],
    manifest_sha256: str,
    now: datetime,
) -> VerifiedProductionAttestation:
    _require_exact_keys(attestation, _ATTESTATION_KEYS, "生产验收证明")
    if (
        type(attestation.get("schema_version")) is not int
        or attestation.get("schema_version") != 1
        or attestation.get("kind") != "dyro-production-attestation"
    ):
        raise ValidationError("生产验收证明 kind/schema_version 不受支持")
    check_id = _require_string(
        attestation.get("check_id"),
        "生产验收证明.check_id",
        maximum=7,
    )
    if check_id not in ATTESTATION_PURPOSES:
        raise ValidationError(f"生产验收证明.check_id 不受支持：{check_id}")
    if attestation.get("release_manifest_sha256") != manifest_sha256:
        raise ValidationError(f"{check_id} 未绑定当前发布清单")
    if attestation.get("environment_id") != manifest["environment_id"]:
        raise ValidationError(f"{check_id} environment_id 与发布清单不一致")
    verdict = _require_string(
        attestation.get("verdict"),
        f"{check_id}.verdict",
        maximum=4,
    )
    if verdict not in {"pass", "fail"}:
        raise ValidationError(f"{check_id}.verdict 必须是 pass 或 fail")
    issued_at = _parse_timestamp(attestation.get("issued_at"), f"{check_id}.issued_at")
    expires_at = _parse_timestamp(
        attestation.get("expires_at"),
        f"{check_id}.expires_at",
    )
    if issued_at > now + _CLOCK_SKEW:
        raise ValidationError(f"{check_id} issued_at 位于未来")
    if expires_at <= issued_at:
        raise ValidationError(f"{check_id} expires_at 必须晚于 issued_at")
    if expires_at - issued_at > _MAX_ATTESTATION_AGE:
        raise ValidationError(f"{check_id} 有效期不能超过 31 天")
    if expires_at <= now:
        raise ValidationError(f"{check_id} 生产验收证明已过期")
    evidence = attestation.get("evidence")
    if not isinstance(evidence, list) or not 1 <= len(evidence) <= 32:
        raise ValidationError(f"{check_id}.evidence 必须包含 1 至 32 项")
    evidence_identities: set[tuple[object, object]] = set()
    for index, entry in enumerate(evidence):
        _validate_evidence_entry(entry, check_id=check_id, index=index)
        identity = (entry["uri"], entry["sha256"])
        if identity in evidence_identities:
            raise ValidationError(f"{check_id}.evidence 不能包含重复证据")
        evidence_identities.add(identity)
    _validate_assertions(
        attestation.get("assertions"),
        check_id=check_id,
        verdict=verdict,
    )
    purpose = ATTESTATION_PURPOSES[check_id]
    _validate_signature_shape(
        attestation,
        purpose=purpose,
        label=check_id,
    )
    verify_record(
        attestation,
        purpose=purpose,
        trust_directory=trusted_keys_directory(root, purpose),
        required=True,
        at=now,
    )
    key_id = signature_key_id(attestation)
    if key_id is None:
        raise ValidationError(f"{check_id} 缺少签名 key ID")
    return VerifiedProductionAttestation(
        check_id=check_id,
        verdict=verdict,
        signer_key_id=key_id,
        signer_fingerprint=trusted_key_fingerprint(root, purpose, key_id),
        issued_at=issued_at.isoformat(),
        expires_at=expires_at.isoformat(),
        evidence_count=len(evidence),
    )


def verify_production_acceptance(
    *,
    root: Path,
    release_manifest_path: Path,
    attestation_paths: Mapping[str, Path] | Sequence[Path],
) -> VerifiedProductionAcceptance:
    """Verify one deployment manifest and zero or more role attestations."""
    if len(attestation_paths) > len(ATTESTATION_PURPOSES):
        raise ValidationError("生产验收证明最多只能包含 PROD-01、PROD-02、PROD-09")
    if isinstance(attestation_paths, Mapping):
        unknown_checks = set(attestation_paths) - set(ATTESTATION_PURPOSES)
        if unknown_checks:
            raise ValidationError(f"生产验收证明包含未知检查：{sorted(unknown_checks)}")
        attestation_entries: Sequence[tuple[str | None, Path]] = tuple(
            attestation_paths.items()
        )
    else:
        attestation_entries = tuple((None, path) for path in attestation_paths)
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise ValidationError(f"生产验收 trust root 不存在：{root}")
    checked_at = datetime.now(timezone.utc)
    manifest = _read_bounded_json(release_manifest_path, "生产发布清单")
    _validate_release_manifest(manifest, now=checked_at)
    verify_record(
        manifest,
        purpose=RELEASE_PURPOSE,
        trust_directory=trusted_keys_directory(root, RELEASE_PURPOSE),
        required=True,
        at=checked_at,
    )
    release_key_id = signature_key_id(manifest)
    if release_key_id is None:
        raise ValidationError("生产发布清单缺少签名 key ID")
    release_fingerprint = trusted_key_fingerprint(
        root,
        RELEASE_PURPOSE,
        release_key_id,
    )
    manifest_sha256 = hashlib.sha256(canonical_json_bytes(manifest)).hexdigest()

    attestations: dict[str, VerifiedProductionAttestation] = {}
    fingerprints: dict[str, str] = {
        RELEASE_PURPOSE: release_fingerprint,
    }
    seen_fingerprints = {release_fingerprint}
    for expected_check_id, path in attestation_entries:
        attestation = _read_bounded_json(path, "生产验收证明")
        verified = _verify_attestation(
            attestation,
            root=root,
            manifest=manifest,
            manifest_sha256=manifest_sha256,
            now=checked_at,
        )
        if expected_check_id is not None and verified.check_id != expected_check_id:
            raise ValidationError(
                f"{expected_check_id} 参数不能接受 {verified.check_id} 验收证明"
            )
        if verified.check_id in attestations:
            raise ValidationError(f"重复的生产验收证明：{verified.check_id}")
        if verified.signer_fingerprint in seen_fingerprints:
            raise ValidationError(
                "发布与各验收角色必须使用四把独立公钥，不能跨角色自我批准"
            )
        attestations[verified.check_id] = verified
        purpose = ATTESTATION_PURPOSES[verified.check_id]
        fingerprints[purpose] = verified.signer_fingerprint
        seen_fingerprints.add(verified.signer_fingerprint)

    return VerifiedProductionAcceptance(
        release_id=str(manifest["release_id"]),
        environment_id=str(manifest["environment_id"]),
        source_commit=str(manifest["source_commit"]),
        release_manifest_sha256=manifest_sha256,
        release_signer_key_id=release_key_id,
        release_signer_fingerprint=release_fingerprint,
        attestations=MappingProxyType(attestations),
        signer_fingerprints=MappingProxyType(fingerprints),
    )

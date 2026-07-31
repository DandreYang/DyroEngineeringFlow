"""Create-only operator tooling for signed production-acceptance records.

The helpers in this module hash real release inputs, prepare unsigned records,
export the exact bytes an external Ed25519 signer must sign, and verify a
returned signature before attaching it.  They never load a production private
key, create acceptance facts, approve a release, or deploy anything.
"""

from __future__ import annotations

import base64
import binascii
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import tempfile
from typing import Mapping, Sequence

from dyro import __version__
from dyro.canonical import canonical_json_bytes
from dyro.errors import ValidationError
from dyro.signing import (
    SIGNATURE_ALGORITHM,
    signature_message,
    trusted_key_fingerprint,
    trusted_keys_directory,
    validate_key_id,
    verify_record,
)

from ..sandbox import BUN_IMAGE
from .production_acceptance import (
    RELEASE_PURPOSE,
    VerifiedProductionAcceptance,
    production_schema_paths as _contract_schema_paths,
    read_production_json,
    validate_unsigned_production_attestation,
    validate_unsigned_release_manifest,
    verify_production_acceptance,
)


_ARTIFACT_INPUTS = {
    "wheel_sha256": "wheel",
    "sdist_sha256": "sdist",
    "sbom_sha256": "SBOM",
    "provenance_sha256": "provenance",
}
_OPERATION_INPUTS = {
    "deployment_sha256": "deployment",
    "canary_plan_sha256": "canary plan",
    "rollback_plan_sha256": "rollback plan",
    "observability_plan_sha256": "observability plan",
    "runbook_sha256": "runbook",
}
_PROVIDER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MAX_RELEASE_INPUT_BYTES = 8 * 1024 * 1024 * 1024
_MAX_EVIDENCE_BYTES = 2 * 1024 * 1024 * 1024
_MAX_SIGNATURE_FILE_BYTES = 4096


def _checked_at() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _safety_boundary() -> dict[str, bool]:
    return {
        "private_key_loaded": False,
        "release_approval_granted": False,
        "deployment_attempted": False,
    }


def _shell_argument(value: object) -> str:
    return shlex.quote(str(value))


def _stable_path(path: Path, label: str) -> Path:
    requested = Path(path).expanduser()
    if not requested.name:
        raise ValidationError(f"{label}路径缺少文件名")
    try:
        parent = requested.parent.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise ValidationError(f"{label}父目录不存在或无法解析：{requested.parent}") from exc
    try:
        mode = parent.lstat().st_mode
    except OSError as exc:
        raise ValidationError(f"{label}父目录无法检查：{parent}") from exc
    if not stat.S_ISDIR(mode):
        raise ValidationError(f"{label}父路径必须是目录：{parent}")
    return parent / requested.name


def _preflight_new_file(path: Path, label: str) -> Path:
    output = _stable_path(path, label)
    try:
        output.lstat()
    except FileNotFoundError:
        return output
    except OSError as exc:
        raise ValidationError(f"{label}无法安全检查：{output}") from exc
    raise ValidationError(f"拒绝覆盖已有{label}：{output}")


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_create_only(path: Path, content: bytes, label: str) -> Path:
    output = _preflight_new_file(path, label)
    descriptor: int | None = None
    temporary: Path | None = None
    linked = False
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{output.name}.",
            dir=output.parent,
        )
        temporary = Path(temporary_name)
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("create-only write made no progress")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        try:
            os.link(temporary, output, follow_symlinks=False)
            linked = True
        except FileExistsError:
            raise ValidationError(f"拒绝覆盖已有{label}：{output}") from None
        except OSError as exc:
            raise ValidationError(f"{label}无法原子创建：{output}") from exc
        _fsync_directory(output.parent)
        return output
    except Exception:
        if linked and temporary is not None:
            try:
                temporary_metadata = temporary.lstat()
                output_metadata = output.lstat()
                if (
                    temporary_metadata.st_dev == output_metadata.st_dev
                    and temporary_metadata.st_ino == output_metadata.st_ino
                ):
                    output.unlink()
            except OSError:
                pass
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _json_bytes(record: Mapping[str, object]) -> bytes:
    return (
        json.dumps(
            record,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _hash_stable_file(
    path: Path,
    label: str,
    *,
    max_bytes: int,
) -> dict[str, object]:
    source = _stable_path(path, label)
    try:
        path_metadata = source.lstat()
    except FileNotFoundError:
        raise ValidationError(f"{label}不存在：{source}") from None
    except OSError as exc:
        raise ValidationError(f"{label}无法安全检查：{source}") from exc
    if not stat.S_ISREG(path_metadata.st_mode):
        raise ValidationError(f"{label}必须是普通文件且不能是符号链接：{source}")
    if path_metadata.st_size < 1:
        raise ValidationError(f"{label}不能为空：{source}")
    if path_metadata.st_size > max_bytes:
        raise ValidationError(f"{label}超过 {max_bytes} 字节上限：{source}")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(source, flags)
    except OSError as exc:
        raise ValidationError(f"{label}无法安全打开：{source}") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_dev != path_metadata.st_dev
            or before.st_ino != path_metadata.st_ino
        ):
            raise ValidationError(f"{label}在打开前被替换：{source}")
        digest = hashlib.sha256()
        bytes_read = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            bytes_read += len(chunk)
            if bytes_read > max_bytes:
                raise ValidationError(f"{label}读取时超过字节上限：{source}")
            digest.update(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        final_path_metadata = source.lstat()
    except OSError as exc:
        raise ValidationError(f"{label}读取后路径发生变化：{source}") from exc
    stable_identity = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    if (
        stable_identity
        != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        )
        or stable_identity
        != (
            final_path_metadata.st_dev,
            final_path_metadata.st_ino,
            final_path_metadata.st_size,
            final_path_metadata.st_mtime_ns,
        )
        or bytes_read != after.st_size
    ):
        raise ValidationError(f"{label}在哈希期间发生变化：{source}")
    return {
        "path": str(source),
        "size_bytes": bytes_read,
        "sha256": digest.hexdigest(),
    }


def _read_stable_bytes(
    path: Path,
    label: str,
    *,
    max_bytes: int,
) -> bytes:
    source = _stable_path(path, label)
    try:
        path_metadata = source.lstat()
    except FileNotFoundError:
        raise ValidationError(f"{label}不存在：{source}") from None
    except OSError as exc:
        raise ValidationError(f"{label}无法安全检查：{source}") from exc
    if not stat.S_ISREG(path_metadata.st_mode):
        raise ValidationError(f"{label}必须是普通文件且不能是符号链接：{source}")
    if not 1 <= path_metadata.st_size <= max_bytes:
        raise ValidationError(f"{label}必须为 1 至 {max_bytes} 字节：{source}")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(source, flags)
    except OSError as exc:
        raise ValidationError(f"{label}无法安全打开：{source}") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_dev != path_metadata.st_dev
            or before.st_ino != path_metadata.st_ino
        ):
            raise ValidationError(f"{label}在打开前被替换：{source}")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        final_path_metadata = source.lstat()
    except OSError as exc:
        raise ValidationError(f"{label}读取后路径发生变化：{source}") from exc
    stable_identity = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    if (
        not content
        or len(content) > max_bytes
        or len(content) != after.st_size
        or stable_identity
        != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        )
        or stable_identity
        != (
            final_path_metadata.st_dev,
            final_path_metadata.st_ino,
            final_path_metadata.st_size,
            final_path_metadata.st_mtime_ns,
        )
    ):
        raise ValidationError(f"{label}在读取期间发生变化：{source}")
    return content


def production_schema_paths() -> dict[str, Path]:
    paths = _contract_schema_paths()
    for name, path in paths.items():
        _hash_stable_file(
            path,
            f"已安装 {name} schema",
            max_bytes=1024 * 1024,
        )
    return paths


def describe_production_schemas(
    *,
    output_dir: Path | None = None,
    dry_run: bool = False,
) -> dict[str, object]:
    checked = _checked_at()
    paths = production_schema_paths()
    schema_records = {
        name: _hash_stable_file(
            path,
            f"已安装 {name} schema",
            max_bytes=1024 * 1024,
        )
        for name, path in paths.items()
    }
    exported_directory: Path | None = None
    if output_dir is not None:
        exported_directory = _preflight_new_file(output_dir, "schema 导出目录")
        if not dry_run:
            created_files: list[Path] = []
            try:
                try:
                    os.mkdir(exported_directory, 0o700)
                except FileExistsError:
                    raise ValidationError(
                        f"拒绝覆盖已有 schema 导出目录：{exported_directory}"
                    ) from None
                for name, source in paths.items():
                    destination = exported_directory / source.name
                    _write_create_only(
                        destination,
                        _read_stable_bytes(
                            source,
                            f"已安装 {name} schema",
                            max_bytes=1024 * 1024,
                        ),
                        "schema 文件",
                    )
                    created_files.append(destination)
                    schema_records[name]["exported_path"] = str(destination)
                _fsync_directory(exported_directory)
                _fsync_directory(exported_directory.parent)
            except Exception:
                for created in created_files:
                    try:
                        created.unlink(missing_ok=True)
                    except OSError:
                        pass
                try:
                    exported_directory.rmdir()
                except OSError:
                    pass
                raise
    verdict = "DRY_RUN" if dry_run and output_dir is not None else (
        "EXPORTED" if output_dir is not None else "LOCATED"
    )
    return {
        "schema_version": 1,
        "kind": "dyro-production-schema-contract",
        "verdict": verdict,
        "checked_at": _timestamp(checked),
        "schemas": schema_records,
        "output_directory": (
            str(exported_directory) if exported_directory is not None else None
        ),
        "written": output_dir is not None and not dry_run,
        **_safety_boundary(),
        "next_command": (
            "dyro runtime production-acceptance release-prepare --help"
        ),
    }


def _hash_fixed_inputs(
    paths: Mapping[str, Path],
    labels: Mapping[str, str],
) -> tuple[dict[str, str], dict[str, dict[str, object]]]:
    if set(paths) != set(labels):
        raise ValidationError(
            f"生产发布输入不完整；missing={sorted(set(labels) - set(paths))}, "
            f"extra={sorted(set(paths) - set(labels))}"
        )
    hashes: dict[str, str] = {}
    details: dict[str, dict[str, object]] = {}
    for field, label in labels.items():
        detail = _hash_stable_file(
            paths[field],
            f"生产发布 {label}",
            max_bytes=_MAX_RELEASE_INPUT_BYTES,
        )
        hashes[field] = str(detail["sha256"])
        details[field] = detail
    return hashes, details


def _hash_providers(
    providers: Sequence[tuple[str, Path]],
) -> tuple[dict[str, str], dict[str, dict[str, object]]]:
    if not 1 <= len(providers) <= 16:
        raise ValidationError("生产发布 provider 必须包含 1 至 16 项")
    hashes: dict[str, str] = {}
    details: dict[str, dict[str, object]] = {}
    for provider_id, path in providers:
        if not _PROVIDER_ID.fullmatch(provider_id):
            raise ValidationError(f"provider ID 格式无效：{provider_id}")
        if provider_id in hashes:
            raise ValidationError(f"provider ID 重复：{provider_id}")
        detail = _hash_stable_file(
            path,
            f"provider {provider_id}",
            max_bytes=_MAX_RELEASE_INPUT_BYTES,
        )
        hashes[provider_id] = str(detail["sha256"])
        details[provider_id] = detail
    return hashes, details


def prepare_release_manifest(
    *,
    release_id: str,
    environment_id: str,
    source_commit: str,
    artifacts: Mapping[str, Path],
    providers: Sequence[tuple[str, Path]],
    operations: Mapping[str, Path],
    output: Path,
    created_at: str | None = None,
    dry_run: bool = False,
) -> dict[str, object]:
    destination = _preflight_new_file(output, "未签名发布清单")
    checked = _checked_at()
    artifact_hashes, artifact_details = _hash_fixed_inputs(
        artifacts,
        _ARTIFACT_INPUTS,
    )
    provider_hashes, provider_details = _hash_providers(providers)
    operation_hashes, operation_details = _hash_fixed_inputs(
        operations,
        _OPERATION_INPUTS,
    )
    record: dict[str, object] = {
        "schema_version": 1,
        "kind": "dyro-production-deployment-manifest",
        "release_id": release_id,
        "environment_id": environment_id,
        "dyro_version": __version__,
        "source_commit": source_commit,
        "runtime_image": BUN_IMAGE,
        "artifacts": artifact_hashes,
        "providers": provider_hashes,
        "operations": operation_hashes,
        "created_at": created_at or _timestamp(checked),
    }
    validate_unsigned_release_manifest(record, checked_at=checked)
    if not dry_run:
        _write_create_only(
            destination,
            _json_bytes(record),
            "未签名发布清单",
        )
    return {
        "schema_version": 1,
        "kind": "dyro-production-release-preparation",
        "verdict": "DRY_RUN" if dry_run else "PREPARED",
        "checked_at": _timestamp(checked),
        "record_kind": record["kind"],
        "record_sha256": hashlib.sha256(canonical_json_bytes(record)).hexdigest(),
        "signing_purpose": RELEASE_PURPOSE,
        "output": str(destination),
        "written": not dry_run,
        "signed": False,
        **_safety_boundary(),
        "record": record,
        "inputs": {
            "artifacts": artifact_details,
            "providers": provider_details,
            "operations": operation_details,
        },
        "next_command": (
            "重复执行且移除 --dry-run"
            if dry_run
            else (
                "dyro runtime production-acceptance signing-payload "
                f"--record {_shell_argument(destination)} "
                "--output <production-release.payload>"
            )
        ),
    }


def _verified_release(
    *,
    root: Path | None,
    release_manifest: Path | None,
    checked_at: datetime,
) -> VerifiedProductionAcceptance:
    if root is None or release_manifest is None:
        raise ValidationError(
            "验收证明必须提供 --root 与已签名 --release-manifest"
        )
    return verify_production_acceptance(
        root=root,
        release_manifest_path=release_manifest,
        attestation_paths=(),
        checked_at=checked_at,
    )


def prepare_production_attestation(
    *,
    root: Path,
    release_manifest: Path,
    check_id: str,
    verdict: str,
    assertions_path: Path,
    evidence: Sequence[tuple[str, Path, str]],
    expires_at: str,
    output: Path,
    issued_at: str | None = None,
    dry_run: bool = False,
) -> dict[str, object]:
    destination = _preflight_new_file(output, "未签名生产验收证明")
    checked = _checked_at()
    release = _verified_release(
        root=root,
        release_manifest=release_manifest,
        checked_at=checked,
    )
    assertions = read_production_json(assertions_path, "生产验收 assertions")
    if not 1 <= len(evidence) <= 32:
        raise ValidationError("生产验收 evidence 必须包含 1 至 32 项")
    evidence_records: list[dict[str, object]] = []
    evidence_inputs: list[dict[str, object]] = []
    for index, (uri, path, summary) in enumerate(evidence):
        detail = _hash_stable_file(
            path,
            f"{check_id} evidence[{index}]",
            max_bytes=_MAX_EVIDENCE_BYTES,
        )
        evidence_records.append(
            {
                "uri": uri,
                "sha256": detail["sha256"],
                "summary": summary,
            }
        )
        evidence_inputs.append(detail)
    record: dict[str, object] = {
        "schema_version": 1,
        "kind": "dyro-production-attestation",
        "check_id": check_id,
        "release_manifest_sha256": release.release_manifest_sha256,
        "environment_id": release.environment_id,
        "verdict": verdict,
        "issued_at": issued_at or _timestamp(checked),
        "expires_at": expires_at,
        "evidence": evidence_records,
        "assertions": assertions,
    }
    purpose = validate_unsigned_production_attestation(
        record,
        release=release,
        checked_at=checked,
    )
    if not dry_run:
        _write_create_only(
            destination,
            _json_bytes(record),
            "未签名生产验收证明",
        )
    return {
        "schema_version": 1,
        "kind": "dyro-production-attestation-preparation",
        "verdict": "DRY_RUN" if dry_run else "PREPARED",
        "checked_at": _timestamp(checked),
        "check_id": check_id,
        "requested_verdict": verdict,
        "release_id": release.release_id,
        "environment_id": release.environment_id,
        "release_manifest_sha256": release.release_manifest_sha256,
        "record_sha256": hashlib.sha256(canonical_json_bytes(record)).hexdigest(),
        "signing_purpose": purpose,
        "output": str(destination),
        "written": not dry_run,
        "signed": False,
        **_safety_boundary(),
        "record": record,
        "inputs": {
            "assertions": {
                "path": str(_stable_path(assertions_path, "assertions")),
                "canonical_sha256": hashlib.sha256(
                    canonical_json_bytes(assertions)
                ).hexdigest(),
            },
            "evidence": evidence_inputs,
        },
        "next_command": (
            "重复执行且移除 --dry-run"
            if dry_run
            else (
                "dyro runtime production-acceptance signing-payload "
                f"--record {_shell_argument(destination)} "
                f"--root {_shell_argument(Path(root))} "
                "--release-manifest "
                f"{_shell_argument(Path(release_manifest))} "
                f"--output <{purpose}.payload>"
            )
        ),
    }


def _unsigned_record_purpose(
    record: dict[str, object],
    *,
    root: Path | None,
    release_manifest: Path | None,
    checked_at: datetime,
) -> tuple[str, VerifiedProductionAcceptance | None]:
    if "signature" in record:
        raise ValidationError("记录已经包含 signature，拒绝重复生成签名载荷")
    kind = record.get("kind")
    if kind == "dyro-production-deployment-manifest":
        validate_unsigned_release_manifest(record, checked_at=checked_at)
        return RELEASE_PURPOSE, None
    if kind == "dyro-production-attestation":
        release = _verified_release(
            root=root,
            release_manifest=release_manifest,
            checked_at=checked_at,
        )
        purpose = validate_unsigned_production_attestation(
            record,
            release=release,
            checked_at=checked_at,
        )
        return purpose, release
    raise ValidationError("只接受未签名生产发布清单或生产验收证明")


def build_production_signing_payload(
    *,
    record_path: Path,
    output: Path | None = None,
    root: Path | None = None,
    release_manifest: Path | None = None,
    dry_run: bool = False,
) -> dict[str, object]:
    destination = (
        _preflight_new_file(output, "签名载荷") if output is not None else None
    )
    checked = _checked_at()
    record = read_production_json(record_path, "未签名生产记录")
    purpose, _ = _unsigned_record_purpose(
        record,
        root=root,
        release_manifest=release_manifest,
        checked_at=checked,
    )
    message = signature_message(record, purpose)
    if destination is not None and not dry_run:
        _write_create_only(destination, message, "签名载荷")
    return {
        "schema_version": 1,
        "kind": "dyro-production-signing-payload",
        "verdict": "DRY_RUN" if dry_run else "GENERATED",
        "checked_at": _timestamp(checked),
        "record": str(_stable_path(record_path, "未签名生产记录")),
        "record_sha256": hashlib.sha256(canonical_json_bytes(record)).hexdigest(),
        "purpose": purpose,
        "algorithm": SIGNATURE_ALGORITHM,
        "prehashed": False,
        "domain": f"dyro/{purpose}/v1",
        "domain_terminator": "NUL",
        "payload_size_bytes": len(message),
        "payload_sha256": hashlib.sha256(message).hexdigest(),
        "payload_base64": base64.b64encode(message).decode("ascii"),
        "output": str(destination) if destination is not None else None,
        "written": destination is not None and not dry_run,
        **_safety_boundary(),
        "next_command": (
            "由外部 Ed25519 signer/HSM 签署精确 payload bytes，然后运行 "
            "dyro runtime production-acceptance signature-attach --help"
        ),
    }


def read_external_signature(path: Path) -> str:
    content = _read_stable_bytes(
        path,
        "外部 Base64 signature",
        max_bytes=_MAX_SIGNATURE_FILE_BYTES,
    )
    try:
        lines = content.decode("ascii").splitlines()
    except UnicodeDecodeError as exc:
        raise ValidationError("外部 signature 文件必须是 ASCII Base64") from exc
    if len(lines) != 1 or lines[0] != lines[0].strip():
        raise ValidationError("外部 signature 文件必须只包含一行规范 Base64")
    return lines[0]


def _canonical_signature(value: str) -> str:
    if value != value.strip():
        raise ValidationError("外部 signature 不能包含首尾空白")
    normalized = value
    if not normalized or any(character.isspace() for character in normalized):
        raise ValidationError("外部 signature 必须是单行 Base64")
    try:
        decoded = base64.b64decode(normalized, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValidationError("外部 signature 不是有效 Base64") from exc
    if len(decoded) != 64:
        raise ValidationError("Ed25519 signature 必须恰好为 64 字节")
    canonical = base64.b64encode(decoded).decode("ascii")
    if normalized != canonical:
        raise ValidationError("外部 signature 必须使用规范 Base64 编码")
    return canonical


def attach_production_signature(
    *,
    root: Path,
    record_path: Path,
    key_id: str,
    signature_base64: str,
    output: Path,
    release_manifest: Path | None = None,
    dry_run: bool = False,
) -> dict[str, object]:
    destination = _preflight_new_file(output, "已签名生产记录")
    checked = _checked_at()
    record = read_production_json(record_path, "未签名生产记录")
    purpose, release = _unsigned_record_purpose(
        record,
        root=root,
        release_manifest=release_manifest,
        checked_at=checked,
    )
    key_id = validate_key_id(key_id)
    signed = dict(record)
    signed["signature"] = {
        "schema_version": 1,
        "algorithm": SIGNATURE_ALGORITHM,
        "purpose": purpose,
        "key_id": key_id,
        "value": _canonical_signature(signature_base64),
    }
    verify_record(
        signed,
        purpose=purpose,
        trust_directory=trusted_keys_directory(Path(root).resolve(), purpose),
        required=True,
        at=checked,
    )
    signer_fingerprint = trusted_key_fingerprint(root, purpose, key_id)
    if (
        release is not None
        and signer_fingerprint == release.release_signer_fingerprint
    ):
        raise ValidationError("验收证明签名公钥不能与发布签名公钥相同")
    if not dry_run:
        _write_create_only(
            destination,
            _json_bytes(signed),
            "已签名生产记录",
        )
    return {
        "schema_version": 1,
        "kind": "dyro-production-signature-attachment",
        "verdict": "DRY_RUN" if dry_run else "ATTACHED",
        "checked_at": _timestamp(checked),
        "record_kind": record["kind"],
        "purpose": purpose,
        "key_id": key_id,
        "signer_fingerprint_sha256": signer_fingerprint,
        "signed_record_sha256": hashlib.sha256(
            canonical_json_bytes(signed)
        ).hexdigest(),
        "output": str(destination),
        "written": not dry_run,
        "signature_verified": True,
        **_safety_boundary(),
        "next_command": (
            "重复执行且移除 --dry-run"
            if dry_run
            else (
                "dyro runtime production-acceptance attestation-prepare --help"
                if purpose == RELEASE_PURPOSE
                else "dyro runtime production-gate --help"
            )
        ),
    }

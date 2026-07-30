"""Build an experiment evidence pack only after dual cleanup verification.

This pack is NOT a production Dyro evidence package: it does not call signoff,
merge, push, or import into a live control plane. It only seals local run
artifacts for later review once Sandbox and Broker cleanup are both proven.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
from typing import Mapping, Sequence
import zipfile

from ..errors import Stage0ValidationError


PACK_SCHEMA_VERSION = 1
PACK_KIND = "external-workflow-runner-stage4-evidence-pack"
MAX_EVIDENCE_ARTIFACTS = 256
MAX_EVIDENCE_ARTIFACT_BYTES = 32 * 1024 * 1024
MAX_EVIDENCE_TOTAL_BYTES = 64 * 1024 * 1024
MAX_TELEMETRY_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class CleanupProof:
    sandbox_cleanup_verified: bool
    broker_cleanup_verified: bool
    broker_containers_absent: bool
    raw_marker_leaked: bool
    provider_token_leaked: bool

    def assert_packable(self) -> None:
        if not self.sandbox_cleanup_verified:
            raise Stage0ValidationError(
                "evidence packing refused: sandbox cleanup not verified"
            )
        if not self.broker_cleanup_verified:
            raise Stage0ValidationError(
                "evidence packing refused: broker cleanup not verified"
            )
        if not self.broker_containers_absent:
            raise Stage0ValidationError(
                "evidence packing refused: broker containers still present"
            )
        if self.raw_marker_leaked or self.provider_token_leaked:
            raise Stage0ValidationError(
                "evidence packing refused: secret material leaked"
            )


@dataclass(frozen=True)
class EvidencePackResult:
    pack_dir: Path
    manifest_path: Path
    zip_path: Path
    pack_sha256: str
    manifest: dict[str, object]


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: Mapping[str, object]) -> str:
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    path.write_text(text, encoding="utf-8")
    return _sha256_bytes(text.encode("utf-8"))


def envelope_artifact_hashes(
    envelope: Mapping[str, object],
) -> dict[tuple[str, str], str]:
    raw_artifacts = envelope.get("artifacts")
    if not isinstance(raw_artifacts, list) or not raw_artifacts:
        raise Stage0ValidationError(
            "evidence packing refused: envelope has no validated artifacts"
        )
    expected: dict[tuple[str, str], str] = {}
    for index, item in enumerate(raw_artifacts):
        if not isinstance(item, Mapping):
            raise Stage0ValidationError(
                f"evidence packing refused: envelope artifact {index} is invalid"
            )
        repository = item.get("repository")
        name = item.get("path")
        digest = item.get("sha256")
        if (
            type(repository) is not str
            or not repository
            or type(name) is not str
            or not name
            or type(digest) is not str
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise Stage0ValidationError(
                f"evidence packing refused: envelope artifact {index} is incomplete"
            )
        key = (repository, name)
        if key in expected:
            raise Stage0ValidationError(
                f"evidence packing refused: duplicate envelope artifact {repository}/{name}"
            )
        expected[key] = digest
    return expected


def _copy_stable_regular_file(
    source: Path,
    destination: Path,
    *,
    max_bytes: int,
) -> tuple[str, int]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        source_descriptor = os.open(Path(source), flags)
    except OSError as exc:
        raise Stage0ValidationError(
            f"evidence artifact cannot be opened: {source}"
        ) from exc
    destination_descriptor: int | None = None
    try:
        before = os.fstat(source_descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise Stage0ValidationError(
                f"evidence artifact is not regular: {source}"
            )
        if before.st_size > max_bytes:
            raise Stage0ValidationError(
                f"evidence artifact exceeds byte limit: {source}"
            )
        destination_descriptor = os.open(
            destination,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        digest = hashlib.sha256()
        bytes_read = 0
        while True:
            chunk = os.read(source_descriptor, 1024 * 1024)
            if not chunk:
                break
            bytes_read += len(chunk)
            if bytes_read > max_bytes:
                raise Stage0ValidationError(
                    f"evidence artifact grew beyond byte limit: {source}"
                )
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(destination_descriptor, view)
                if written <= 0:
                    raise OSError("evidence artifact copy made no progress")
                view = view[written:]
        os.fsync(destination_descriptor)
        after = os.fstat(source_descriptor)
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or bytes_read != after.st_size
        ):
            raise Stage0ValidationError(
                f"evidence artifact changed while being sealed: {source}"
            )
        return digest.hexdigest(), bytes_read
    except OSError as exc:
        destination.unlink(missing_ok=True)
        raise Stage0ValidationError(
            f"evidence artifact cannot be copied safely: {source}"
        ) from exc
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    finally:
        if destination_descriptor is not None:
            os.close(destination_descriptor)
        os.close(source_descriptor)


def pack_run_evidence(
    *,
    pack_root: Path,
    workflow_run_id: str,
    claim: Mapping[str, object],
    canonical_input_sha256: str,
    envelope: Mapping[str, object],
    artifact_paths: Sequence[tuple[str, str, Path]],
    provider_pin: Mapping[str, object],
    claim_matrix: Mapping[str, object],
    cleanup: CleanupProof,
    mid_run_renewals: int,
    provider_mode: str,
    telemetry_text: str = "",
) -> EvidencePackResult:
    """
    Seal a Stage 4 evidence pack.

    Fail-closed: dual cleanup must already be verified. Never merges or pushes.
    """
    cleanup.assert_packable()
    if envelope.get("status") != "DONE":
        raise Stage0ValidationError(
            "evidence packing refused: envelope status is not DONE"
        )
    if envelope.get("workflow_run_id") != workflow_run_id:
        raise Stage0ValidationError(
            "evidence packing refused: envelope workflow_run_id mismatch"
        )
    expected_artifacts = envelope_artifact_hashes(envelope)

    pack_root = Path(pack_root)
    if pack_root.exists() or pack_root.is_symlink():
        raise Stage0ValidationError("evidence pack root already exists")

    artifact_inputs = list(artifact_paths)
    if len(artifact_inputs) > MAX_EVIDENCE_ARTIFACTS:
        raise Stage0ValidationError(
            f"evidence artifact count exceeds limit: {MAX_EVIDENCE_ARTIFACTS}"
        )
    packed_ids: set[tuple[str, str]] = set()
    for repository, name, _ in artifact_inputs:
        artifact_id = (repository, name)
        if artifact_id in packed_ids:
            raise Stage0ValidationError(
                f"duplicate evidence artifact: {repository}/{name}"
            )
        if artifact_id not in expected_artifacts:
            raise Stage0ValidationError(
                f"evidence artifact is not declared by envelope: {repository}/{name}"
            )
        packed_ids.add(artifact_id)
    if packed_ids != set(expected_artifacts):
        missing = sorted(set(expected_artifacts) - packed_ids)
        raise Stage0ValidationError(
            f"evidence pack omitted envelope artifacts: {missing}"
        )

    telemetry_bytes = telemetry_text.encode("utf-8")
    if len(telemetry_bytes) > MAX_TELEMETRY_BYTES:
        raise Stage0ValidationError(
            f"evidence telemetry exceeds byte limit: {MAX_TELEMETRY_BYTES}"
        )

    pack_root.parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(
        tempfile.mkdtemp(
            prefix=f".{pack_root.name}.",
            dir=str(pack_root.parent),
        )
    )
    try:
        files_dir = staging_root / "files"
        files_dir.mkdir()
        artifact_records: list[dict[str, object]] = []
        total_artifact_bytes = 0
        for index, (repository, name, source_path) in enumerate(artifact_inputs):
            remaining = MAX_EVIDENCE_TOTAL_BYTES - total_artifact_bytes
            if remaining <= 0:
                raise Stage0ValidationError(
                    f"evidence artifacts exceed total byte limit: "
                    f"{MAX_EVIDENCE_TOTAL_BYTES}"
                )
            temporary_name = f"{index:04d}.artifact"
            temporary_path = files_dir / temporary_name
            actual_sha, byte_count = _copy_stable_regular_file(
                Path(source_path),
                temporary_path,
                max_bytes=min(MAX_EVIDENCE_ARTIFACT_BYTES, remaining),
            )
            expected_sha = expected_artifacts[(repository, name)]
            if not hmac.compare_digest(actual_sha, expected_sha):
                raise Stage0ValidationError(
                    f"evidence artifact mismatch vs envelope: {repository}/{name}"
                )
            stored_name = f"{index:04d}-{actual_sha}.artifact"
            temporary_path.rename(files_dir / stored_name)
            total_artifact_bytes += byte_count
            artifact_records.append(
                {
                    "repository": repository,
                    "name": name,
                    "stored_name": stored_name,
                    "sha256": actual_sha,
                    "bytes": byte_count,
                }
            )

        envelope_path = staging_root / "result-envelope.json"
        envelope_sha = _write_json(envelope_path, envelope)

        claim_path = staging_root / "claim.json"
        claim_sha = _write_json(claim_path, dict(claim))

        if telemetry_bytes:
            telemetry_path = staging_root / "broker-telemetry.jsonl"
            telemetry_path.write_bytes(telemetry_bytes)
            telemetry_sha = _sha256_file(telemetry_path)
        else:
            telemetry_sha = ""

        manifest: dict[str, object] = {
            "schema_version": PACK_SCHEMA_VERSION,
            "kind": PACK_KIND,
            "workflow_run_id": workflow_run_id,
            "canonical_input_sha256": canonical_input_sha256,
            "result_envelope_sha256": envelope_sha,
            "claim_sha256": claim_sha,
            "telemetry_sha256": telemetry_sha,
            "artifacts": artifact_records,
            "provider_mode": provider_mode,
            "provider_cli": dict(provider_pin),
            "claim_matrix": dict(claim_matrix),
            "mid_run_renewals": mid_run_renewals,
            "cleanup": {
                "sandbox_cleanup_verified": cleanup.sandbox_cleanup_verified,
                "broker_cleanup_verified": cleanup.broker_cleanup_verified,
                "broker_containers_absent": cleanup.broker_containers_absent,
                "raw_marker_leaked": cleanup.raw_marker_leaked,
                "provider_token_leaked": cleanup.provider_token_leaked,
            },
            "non_goals": [
                "no_signoff",
                "no_merge",
                "no_push",
                "no_dyro_core_import",
            ],
        }
        manifest_path = staging_root / "pack-manifest.json"
        manifest_sha = _write_json(manifest_path, manifest)

        zip_path = staging_root / "evidence-pack.zip"
        with zipfile.ZipFile(
            zip_path,
            "w",
            compression=zipfile.ZIP_DEFLATED,
        ) as archive:
            for path in sorted(staging_root.rglob("*")):
                if path == zip_path or not path.is_file():
                    continue
                archive.write(
                    path,
                    arcname=path.relative_to(staging_root).as_posix(),
                )
        pack_sha = _sha256_file(zip_path)

        seal = {
            "schema_version": 1,
            "kind": "stage4-evidence-seal",
            "pack_sha256": pack_sha,
            "pack_manifest_sha256": manifest_sha,
            "workflow_run_id": workflow_run_id,
            "actions_forbidden": ["signoff", "merge", "push"],
        }
        _write_json(staging_root / "seal.json", seal)

        if pack_root.exists() or pack_root.is_symlink():
            raise Stage0ValidationError("evidence pack root already exists")
        staging_root.rename(pack_root)
    except Exception:
        shutil.rmtree(staging_root, ignore_errors=True)
        raise

    return EvidencePackResult(
        pack_dir=pack_root,
        manifest_path=pack_root / "pack-manifest.json",
        zip_path=pack_root / "evidence-pack.zip",
        pack_sha256=pack_sha,
        manifest=manifest,
    )


def refuse_if_merge_requested(flags: Mapping[str, object] | None) -> None:
    if not flags:
        return
    for key in ("merge", "push", "signoff", "import_evidence"):
        if flags.get(key):
            raise Stage0ValidationError(
                f"Stage 4 Supervisor forbids action: {key}"
            )

"""Non-production dry-run validator for Stage 4/5 local evidence packs.

Validates sealed pack integrity and emits a candidate record for human review.
Never calls Dyro Core import, signoff, merge, or push.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from copy import copy
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import stat
import struct
import tempfile
from typing import Mapping
import zipfile

from ..errors import Stage0ValidationError
from ..stage1.claim import ClaimRecord
from ..stage4.evidence_pack import envelope_artifact_hashes
from ..stage4.evidence_pack import PACK_KIND, PACK_SCHEMA_VERSION


DRY_RUN_KIND = "external-workflow-runner-evidence-dry-run"
MAX_PACK_FILES = 256
MAX_PACK_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_PACK_JSON_BYTES = 4 * 1024 * 1024
MAX_ZIP_CENTRAL_DIRECTORY_BYTES = 1024 * 1024
ZIP_READ_CHUNK_BYTES = 1024 * 1024
ALLOWED_ZIP_COMPRESSION = frozenset(
    {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
)
ZIP_EOCD_SIGNATURE = b"PK\x05\x06"
ZIP_CENTRAL_ENTRY_SIGNATURE = b"PK\x01\x02"
ZIP64_EOCD_LOCATOR_SIGNATURE = b"PK\x06\x07"
ZIP_EOCD_BYTES = 22
ZIP_MAX_COMMENT_BYTES = 65535
FORBIDDEN_ACTIONS = frozenset(
    {"signoff", "merge", "push", "import_evidence", "import", "review_bind"}
)


@dataclass(frozen=True)
class EvidenceDryRunResult:
    pack_dir: Path
    report_path: Path
    report: dict[str, object]
    pack_sha256_verified: bool
    candidate_record: dict[str, object]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _assert_regular_file(path: Path, *, max_bytes: int | None = None) -> int:
    try:
        metadata = Path(path).lstat()
    except OSError as exc:
        raise Stage0ValidationError(f"evidence pack missing: {path.name}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise Stage0ValidationError(
            f"evidence pack member is not a regular file: {path.name}"
        )
    if max_bytes is not None and metadata.st_size > max_bytes:
        raise Stage0ValidationError(f"evidence pack member is too large: {path.name}")
    return metadata.st_size


@contextmanager
def _snapshot_zip(path: Path) -> Iterator[tuple[Path, str]]:
    """Copy one no-follow source descriptor into a bounded private snapshot."""
    path = Path(path)
    try:
        path_metadata = path.lstat()
    except OSError as exc:
        raise Stage0ValidationError(
            f"evidence pack missing: {path.name}"
        ) from exc
    if not stat.S_ISREG(path_metadata.st_mode):
        raise Stage0ValidationError(
            f"evidence pack member is not a regular file: {path.name}"
        )
    if path_metadata.st_size > MAX_PACK_UNCOMPRESSED_BYTES:
        raise Stage0ValidationError(
            f"evidence pack member is too large: {path.name}"
        )

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        opened_metadata = os.fstat(descriptor)
        if not stat.S_ISREG(opened_metadata.st_mode):
            raise Stage0ValidationError(
                f"evidence pack member is not a regular file: {path.name}"
            )
        if (
            opened_metadata.st_dev != path_metadata.st_dev
            or opened_metadata.st_ino != path_metadata.st_ino
        ):
            raise Stage0ValidationError(
                "evidence pack zip changed while it was opened"
            )
        if opened_metadata.st_size > MAX_PACK_UNCOMPRESSED_BYTES:
            raise Stage0ValidationError(
                f"evidence pack member is too large: {path.name}"
            )

        with tempfile.TemporaryDirectory(prefix="dyro-evidence-zip-") as tmp:
            snapshot_path = Path(tmp) / "evidence-pack.zip"
            digest = hashlib.sha256()
            byte_count = 0
            source = os.fdopen(descriptor, "rb", closefd=True)
            descriptor = None
            with (
                source,
                snapshot_path.open("xb") as destination,
            ):
                while True:
                    chunk = source.read(ZIP_READ_CHUNK_BYTES)
                    if not chunk:
                        break
                    byte_count += len(chunk)
                    if byte_count > MAX_PACK_UNCOMPRESSED_BYTES:
                        raise Stage0ValidationError(
                            "evidence pack member is too large: "
                            f"{path.name}"
                        )
                    destination.write(chunk)
                    digest.update(chunk)
            if byte_count != opened_metadata.st_size:
                raise Stage0ValidationError(
                    "evidence pack zip changed while it was snapshotted"
                )
            yield snapshot_path, digest.hexdigest()
    except Stage0ValidationError:
        raise
    except OSError as exc:
        raise Stage0ValidationError("evidence zip is unreadable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _load_json(path: Path) -> dict[str, object]:
    _assert_regular_file(path, max_bytes=MAX_PACK_JSON_BYTES)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Stage0ValidationError(f"unreadable pack json: {path.name}") from exc
    if not isinstance(payload, dict):
        raise Stage0ValidationError(f"pack json must be object: {path.name}")
    return payload


def _preflight_zip_directory(path: Path) -> int:
    """Bound central-directory parsing before ``ZipFile`` allocates entries."""
    file_size = _assert_regular_file(
        path,
        max_bytes=MAX_PACK_UNCOMPRESSED_BYTES,
    )
    if file_size < ZIP_EOCD_BYTES:
        raise Stage0ValidationError("evidence zip end record is missing")

    tail_size = min(file_size, ZIP_EOCD_BYTES + ZIP_MAX_COMMENT_BYTES)
    try:
        with Path(path).open("rb") as stream:
            stream.seek(file_size - tail_size)
            tail = stream.read(tail_size)
            if len(tail) != tail_size:
                raise Stage0ValidationError(
                    "evidence zip end record is truncated"
                )

            tail_offset = tail.rfind(ZIP_EOCD_SIGNATURE)
            if tail_offset < 0 or tail_offset + ZIP_EOCD_BYTES > len(tail):
                raise Stage0ValidationError(
                    "evidence zip end record is missing or malformed"
                )
            fields = struct.unpack_from("<4s4H2IH", tail, tail_offset)
            if tail_offset + ZIP_EOCD_BYTES + fields[-1] != len(tail):
                # ZipFile also selects the last EOCD signature. Refuse comments
                # containing a later fake signature rather than preflighting a
                # different record from the one ZipFile would materialize.
                raise Stage0ValidationError(
                    "evidence zip end record comment is malformed"
                )
            (
                _signature,
                disk_number,
                directory_disk,
                disk_entries,
                total_entries,
                directory_size,
                directory_offset,
                _comment_size,
            ) = fields
            eocd_offset = file_size - tail_size + tail_offset

            if (
                disk_entries == 0xFFFF
                or total_entries == 0xFFFF
                or directory_size == 0xFFFFFFFF
                or directory_offset == 0xFFFFFFFF
            ):
                raise Stage0ValidationError("evidence zip does not allow ZIP64")
            if eocd_offset >= 20:
                stream.seek(eocd_offset - 20)
                if stream.read(4) == ZIP64_EOCD_LOCATOR_SIGNATURE:
                    raise Stage0ValidationError(
                        "evidence zip does not allow ZIP64"
                    )
            if (
                disk_number != 0
                or directory_disk != 0
                or disk_entries != total_entries
            ):
                raise Stage0ValidationError(
                    "evidence zip must use one complete disk"
                )
            if total_entries > MAX_PACK_FILES:
                raise Stage0ValidationError("evidence zip has too many members")
            if directory_size > MAX_ZIP_CENTRAL_DIRECTORY_BYTES:
                raise Stage0ValidationError(
                    "evidence zip central directory exceeds byte limit"
                )
            if directory_offset + directory_size != eocd_offset:
                raise Stage0ValidationError(
                    "evidence zip central directory bounds are invalid"
                )

            stream.seek(directory_offset)
            directory = stream.read(directory_size)
            if len(directory) != directory_size:
                raise Stage0ValidationError(
                    "evidence zip central directory is truncated"
                )
    except Stage0ValidationError:
        raise
    except OSError as exc:
        raise Stage0ValidationError("evidence zip is unreadable") from exc

    actual_entries = 0
    offset = 0
    while offset < len(directory):
        if (
            offset + 46 > len(directory)
            or directory[offset : offset + 4] != ZIP_CENTRAL_ENTRY_SIGNATURE
        ):
            raise Stage0ValidationError(
                "evidence zip central directory is malformed"
            )
        (
            name_size,
            extra_size,
            comment_size,
        ) = struct.unpack_from("<3H", directory, offset + 28)
        disk_start = struct.unpack_from("<H", directory, offset + 34)[0]
        compressed_size = struct.unpack_from("<I", directory, offset + 20)[0]
        uncompressed_size = struct.unpack_from("<I", directory, offset + 24)[0]
        local_header_offset = struct.unpack_from("<I", directory, offset + 42)[0]
        if (
            disk_start == 0xFFFF
            or compressed_size == 0xFFFFFFFF
            or uncompressed_size == 0xFFFFFFFF
            or local_header_offset == 0xFFFFFFFF
        ):
            raise Stage0ValidationError("evidence zip does not allow ZIP64")
        if disk_start != 0:
            raise Stage0ValidationError(
                "evidence zip must use one complete disk"
            )

        record_end = (
            offset + 46 + name_size + extra_size + comment_size
        )
        if record_end > len(directory):
            raise Stage0ValidationError(
                "evidence zip central directory entry is truncated"
            )
        extra_start = offset + 46 + name_size
        extra_end = extra_start + extra_size
        extra_offset = extra_start
        while extra_offset < extra_end:
            if extra_offset + 4 > extra_end:
                raise Stage0ValidationError(
                    "evidence zip central directory extra field is malformed"
                )
            header_id, value_size = struct.unpack_from(
                "<2H",
                directory,
                extra_offset,
            )
            extra_offset += 4
            if extra_offset + value_size > extra_end:
                raise Stage0ValidationError(
                    "evidence zip central directory extra field is truncated"
                )
            if header_id == 0x0001:
                raise Stage0ValidationError("evidence zip does not allow ZIP64")
            extra_offset += value_size

        actual_entries += 1
        if actual_entries > MAX_PACK_FILES:
            raise Stage0ValidationError("evidence zip has too many members")
        offset = record_end
    if actual_entries != total_entries:
        raise Stage0ValidationError(
            "evidence zip member count differs from end record"
        )
    return actual_entries


def _hash_zip_members(
    archive: zipfile.ZipFile,
    infos: list[zipfile.ZipInfo],
) -> tuple[dict[str, str], dict[str, int]]:
    """Hash actual member output with a bounded streaming decompression probe."""
    hashes: dict[str, str] = {}
    sizes: dict[str, int] = {}
    total_uncompressed = 0
    for info in infos:
        if info.is_dir():
            continue
        if info.flag_bits & 0x1:
            raise Stage0ValidationError(
                f"evidence zip has encrypted member: {info.filename}"
            )
        if info.compress_type not in ALLOWED_ZIP_COMPRESSION:
            raise Stage0ValidationError(
                f"evidence zip has unsupported compression: {info.filename}"
            )

        # ZipExtFile normally trusts the central-directory file_size and stops
        # there. Probe beyond the remaining application budget so a forged,
        # under-reported size cannot hide a larger deflate stream.
        probe_info = copy(info)
        remaining = MAX_PACK_UNCOMPRESSED_BYTES - total_uncompressed
        probe_info.file_size = remaining + ZIP_READ_CHUNK_BYTES + 1
        digest = hashlib.sha256()
        member_size = 0
        try:
            with archive.open(probe_info, "r") as member_stream:
                while True:
                    read_size = min(
                        ZIP_READ_CHUNK_BYTES,
                        MAX_PACK_UNCOMPRESSED_BYTES
                        - total_uncompressed
                        + 1,
                    )
                    chunk = member_stream.read(read_size)
                    if not chunk:
                        break
                    member_size += len(chunk)
                    total_uncompressed += len(chunk)
                    if total_uncompressed > MAX_PACK_UNCOMPRESSED_BYTES:
                        raise Stage0ValidationError(
                            "evidence zip exceeds uncompressed byte limit"
                        )
                    digest.update(chunk)
        except Stage0ValidationError:
            raise
        except (
            EOFError,
            NotImplementedError,
            OSError,
            RuntimeError,
            zipfile.BadZipFile,
        ) as exc:
            raise Stage0ValidationError(
                f"evidence zip member is unreadable: {info.filename}"
            ) from exc
        hashes[info.filename] = digest.hexdigest()
        sizes[info.filename] = member_size
    return hashes, sizes


def refuse_production_actions(flags: Mapping[str, object] | None) -> None:
    if not flags:
        return
    for key in FORBIDDEN_ACTIONS:
        if flags.get(key):
            raise Stage0ValidationError(
                f"evidence dry-run forbids production action: {key}"
            )


def dry_run_validate_pack(
    pack_dir: Path,
    *,
    output_path: Path | None = None,
    production_actions: Mapping[str, object] | None = None,
) -> EvidenceDryRunResult:
    refuse_production_actions(production_actions)
    pack_dir = Path(pack_dir)
    if not pack_dir.is_dir():
        raise Stage0ValidationError("evidence pack directory is missing")

    manifest_path = pack_dir / "pack-manifest.json"
    seal_path = pack_dir / "seal.json"
    zip_path = pack_dir / "evidence-pack.zip"
    envelope_path = pack_dir / "result-envelope.json"
    claim_path = pack_dir / "claim.json"

    for required in (manifest_path, seal_path, envelope_path, claim_path):
        _assert_regular_file(required, max_bytes=MAX_PACK_JSON_BYTES)
    manifest = _load_json(manifest_path)
    seal = _load_json(seal_path)
    envelope = _load_json(envelope_path)
    claim = _load_json(claim_path)
    verified_claim = ClaimRecord.from_mapping(claim)

    if (
        manifest.get("schema_version") != PACK_SCHEMA_VERSION
        or manifest.get("kind") != PACK_KIND
    ):
        raise Stage0ValidationError("evidence pack kind/schema is unsupported")
    if (
        seal.get("schema_version") != 1
        or seal.get("kind") != "stage4-evidence-seal"
    ):
        raise Stage0ValidationError("evidence seal kind/schema is unsupported")
    workflow_run_id = manifest.get("workflow_run_id")
    if (
        not isinstance(workflow_run_id, str)
        or not workflow_run_id
        or len(workflow_run_id) > 256
        or envelope.get("workflow_run_id") != workflow_run_id
        or seal.get("workflow_run_id") != workflow_run_id
    ):
        raise Stage0ValidationError(
            "workflow_run_id is not bound across manifest, envelope, and seal"
        )
    canonical_input_sha256 = manifest.get(
        "canonical_input_sha256"
    )
    if (
        not isinstance(canonical_input_sha256, str)
        or len(canonical_input_sha256) != 64
        or any(
            character not in "0123456789abcdef"
            for character in canonical_input_sha256
        )
    ):
        raise Stage0ValidationError(
            "canonical_input_sha256 is invalid"
        )
    if envelope.get("status") != "DONE":
        raise Stage0ValidationError("dry-run refused: envelope status is not DONE")
    if not hmac.compare_digest(
        str(seal.get("pack_manifest_sha256", "")),
        _sha256_file(manifest_path),
    ):
        raise Stage0ValidationError("pack manifest sha256 does not match seal")

    cleanup = manifest.get("cleanup")
    if not isinstance(cleanup, Mapping):
        raise Stage0ValidationError("evidence pack missing cleanup proof")
    if (
        cleanup.get("sandbox_cleanup_verified") is not True
        or cleanup.get("broker_cleanup_verified") is not True
        or cleanup.get("broker_containers_absent") is not True
        or cleanup.get("raw_marker_leaked") is True
        or cleanup.get("provider_token_leaked") is True
    ):
        raise Stage0ValidationError(
            "dry-run refused: dual cleanup proof incomplete or secrets leaked"
        )

    non_goals = manifest.get("non_goals")
    required_non_goals = {
        "no_signoff",
        "no_merge",
        "no_push",
        "no_dyro_core_import",
    }
    if (
        not isinstance(non_goals, list)
        or any(not isinstance(item, str) for item in non_goals)
        or not required_non_goals.issubset(set(non_goals))
    ):
        raise Stage0ValidationError(
            "evidence pack must declare every production-action non-goal"
        )
    actions_forbidden = seal.get("actions_forbidden")
    if (
        not isinstance(actions_forbidden, list)
        or len(actions_forbidden) != 3
        or any(not isinstance(item, str) for item in actions_forbidden)
        or set(actions_forbidden) != {"signoff", "merge", "push"}
    ):
        raise Stage0ValidationError(
            "evidence seal must forbid signoff, merge, and push"
        )

    with _snapshot_zip(zip_path) as (zip_snapshot, zip_sha):
        if seal.get("pack_sha256") != zip_sha:
            raise Stage0ValidationError(
                "evidence pack zip sha256 does not match seal"
            )

        # Seal, preflight, and ZipFile all bind to the same private snapshot.
        expected_zip_entries = _preflight_zip_directory(zip_snapshot)
        with zipfile.ZipFile(zip_snapshot, "r") as zf:
            infos = zf.infolist()
            if len(infos) != expected_zip_entries:
                raise Stage0ValidationError(
                    "evidence zip changed after central directory preflight"
                )
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                raise Stage0ValidationError("evidence zip has duplicate members")
            for info in infos:
                member = PurePosixPath(info.filename)
                if (
                    member.is_absolute()
                    or ".." in member.parts
                    or "\\" in info.filename
                    or stat.S_ISLNK(info.external_attr >> 16)
                ):
                    raise Stage0ValidationError(
                        f"evidence zip has unsafe member: {info.filename}"
                    )
            zip_hashes, zip_sizes = _hash_zip_members(zf, infos)
            for member in (
                "pack-manifest.json",
                "result-envelope.json",
                "claim.json",
            ):
                if member not in names:
                    raise Stage0ValidationError(
                        f"evidence zip missing member: {member}"
                    )
            for member, disk_path in (
                ("pack-manifest.json", manifest_path),
                ("result-envelope.json", envelope_path),
                ("claim.json", claim_path),
            ):
                if not hmac.compare_digest(
                    zip_hashes[member],
                    _sha256_file(disk_path),
                ):
                    raise Stage0ValidationError(
                        "evidence zip member differs from pack directory: "
                        f"{member}"
                    )

    envelope_sha = _sha256_file(envelope_path)
    claim_sha = _sha256_file(claim_path)
    if manifest.get("result_envelope_sha256") != envelope_sha:
        raise Stage0ValidationError("result envelope sha256 mismatch vs pack manifest")
    if manifest.get("claim_sha256") != claim_sha:
        raise Stage0ValidationError("claim sha256 mismatch vs pack manifest")

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise Stage0ValidationError("evidence pack has no artifacts")
    files_dir = pack_dir / "files"
    expected_artifacts = envelope_artifact_hashes(envelope)
    observed_artifacts: set[tuple[str, str]] = set()
    for item in artifacts:
        if not isinstance(item, Mapping):
            raise Stage0ValidationError("artifact record invalid")
        repository = item.get("repository")
        name = item.get("name")
        stored_name = item.get("stored_name")
        sha = item.get("sha256")
        byte_count = item.get("bytes")
        if (
            type(repository) is not str
            or type(name) is not str
            or type(stored_name) is not str
            or type(sha) is not str
            or type(byte_count) is not int
            or byte_count < 0
        ):
            raise Stage0ValidationError("artifact record fields invalid")
        artifact_id = (repository, name)
        if artifact_id in observed_artifacts:
            raise Stage0ValidationError(
                f"duplicate pack artifact record: {repository}/{name}"
            )
        expected_sha = expected_artifacts.get(artifact_id)
        if expected_sha != sha:
            raise Stage0ValidationError(
                f"pack artifact sha256 mismatch vs envelope: {repository}/{name}"
            )
        if (
            stored_name in {"", ".", ".."}
            or "/" in stored_name
            or "\\" in stored_name
            or Path(stored_name).name != stored_name
        ):
            raise Stage0ValidationError("artifact stored_name is unsafe")
        artifact_path = files_dir / stored_name
        disk_size = _assert_regular_file(
            artifact_path,
            max_bytes=MAX_PACK_UNCOMPRESSED_BYTES,
        )
        if disk_size != byte_count:
            raise Stage0ValidationError(f"pack artifact byte count mismatch: {name}")
        if _sha256_file(artifact_path) != sha:
            raise Stage0ValidationError(f"pack artifact sha256 mismatch: {name}")
        zip_member = f"files/{stored_name}"
        if zip_member not in names:
            raise Stage0ValidationError(f"evidence zip missing artifact: {name}")
        if zip_sizes[zip_member] != byte_count:
            raise Stage0ValidationError(
                f"evidence zip artifact byte count mismatch: {name}"
            )
        if zip_hashes[zip_member] != sha:
            raise Stage0ValidationError(
                f"evidence zip artifact sha256 mismatch: {name}"
            )
        observed_artifacts.add(artifact_id)
    if observed_artifacts != set(expected_artifacts):
        raise Stage0ValidationError("pack artifacts do not match envelope artifacts")

    candidate = {
        "schema_version": 1,
        "kind": "experiment-evidence-candidate",
        "workflow_run_id": manifest.get("workflow_run_id"),
        "canonical_input_sha256": manifest.get("canonical_input_sha256"),
        "claim_generation": verified_claim.generation,
        "task_id": verified_claim.task_id,
        "runner_id": verified_claim.runner_id,
        "execution_key_id": verified_claim.execution_key_id,
        "control_claim_id": verified_claim.control_claim_id,
        "control_generation": verified_claim.control_generation,
        "authority_expires_at": verified_claim.authority_expires_at,
        "envelope_status": envelope.get("status"),
        "artifact_count": len(artifacts),
        "artifacts": [
            {
                "repository": item["repository"],
                "path": item["name"],
                "sha256": item["sha256"],
                "bytes": item["bytes"],
            }
            for item in artifacts
        ],
        "provider_mode": manifest.get("provider_mode"),
        "pack_sha256": zip_sha,
        "production_import": False,
        "production_signoff": False,
        "production_merge": False,
        "production_push": False,
        "notes": [
            "Local experiment pack only.",
            "Not a Dyro Core evidence package.",
            "Must not bind review/signoff/merge/push.",
        ],
    }

    report = {
        "schema_version": 1,
        "kind": DRY_RUN_KIND,
        "verdict": "ACCEPT_FOR_HUMAN_REVIEW_ONLY",
        "pack_dir": str(pack_dir),
        "pack_sha256_verified": True,
        "seal_actions_forbidden": seal.get("actions_forbidden"),
        "cleanup": dict(cleanup),
        "candidate_record": candidate,
        "forbidden_actions_honored": sorted(FORBIDDEN_ACTIONS),
        "dyro_core_import_attempted": False,
        "production_ready": False,
    }

    if output_path is None:
        output_path = pack_dir / "evidence-dry-run.json"
    else:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    return EvidenceDryRunResult(
        pack_dir=pack_dir,
        report_path=output_path,
        report=report,
        pack_sha256_verified=True,
        candidate_record=candidate,
    )

"""Build an experiment evidence pack only after dual cleanup verification.

This pack is NOT a production Dyro evidence package: it does not call signoff,
merge, push, or import into a live control plane. It only seals local run
artifacts for later review once Sandbox and Broker cleanup are both proven.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Mapping, Sequence
import zipfile

from ..errors import Stage0ValidationError


PACK_SCHEMA_VERSION = 1
PACK_KIND = "external-workflow-runner-stage4-evidence-pack"


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
    return _sha256_bytes(Path(path).read_bytes())


def _write_json(path: Path, payload: Mapping[str, object]) -> str:
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    path.write_text(text, encoding="utf-8")
    return _sha256_bytes(text.encode("utf-8"))


def pack_run_evidence(
    *,
    pack_root: Path,
    workflow_run_id: str,
    claim: Mapping[str, object],
    canonical_input_sha256: str,
    envelope: Mapping[str, object],
    artifact_paths: Sequence[tuple[str, Path]],
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

    pack_root = Path(pack_root)
    if pack_root.exists():
        raise Stage0ValidationError("evidence pack root already exists")
    pack_root.mkdir(parents=True)
    files_dir = pack_root / "files"
    files_dir.mkdir()

    artifact_records: list[dict[str, object]] = []
    for name, path in artifact_paths:
        path = Path(path)
        if not path.is_file():
            raise Stage0ValidationError(f"evidence artifact missing: {name}")
        target = files_dir / name.replace("/", "_")
        data = path.read_bytes()
        target.write_bytes(data)
        artifact_records.append(
            {
                "name": name,
                "sha256": _sha256_bytes(data),
                "bytes": len(data),
            }
        )

    envelope_path = pack_root / "result-envelope.json"
    envelope_sha = _write_json(envelope_path, envelope)

    claim_path = pack_root / "claim.json"
    claim_sha = _write_json(claim_path, dict(claim))

    if telemetry_text:
        telemetry_path = pack_root / "broker-telemetry.jsonl"
        telemetry_path.write_text(telemetry_text, encoding="utf-8")
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
    manifest_path = pack_root / "pack-manifest.json"
    manifest_sha = _write_json(manifest_path, manifest)
    manifest["pack_manifest_sha256"] = manifest_sha
    # Rewrite with self-hash for sealed identity.
    manifest_sha = _write_json(manifest_path, manifest)

    zip_path = pack_root / "evidence-pack.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(pack_root.rglob("*")):
            if path == zip_path or not path.is_file():
                continue
            zf.write(path, arcname=path.relative_to(pack_root).as_posix())
    pack_sha = _sha256_file(zip_path)

    seal = {
        "schema_version": 1,
        "kind": "stage4-evidence-seal",
        "pack_sha256": pack_sha,
        "pack_manifest_sha256": manifest_sha,
        "workflow_run_id": workflow_run_id,
        "actions_forbidden": ["signoff", "merge", "push"],
    }
    _write_json(pack_root / "seal.json", seal)

    return EvidencePackResult(
        pack_dir=pack_root,
        manifest_path=manifest_path,
        zip_path=zip_path,
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

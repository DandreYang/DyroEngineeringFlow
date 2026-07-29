"""Non-production dry-run validator for Stage 4/5 local evidence packs.

Validates sealed pack integrity and emits a candidate record for human review.
Never calls Dyro Core import, signoff, merge, or push.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Mapping
import zipfile

from ..errors import Stage0ValidationError
from ..stage4.evidence_pack import PACK_KIND, PACK_SCHEMA_VERSION


DRY_RUN_KIND = "external-workflow-runner-evidence-dry-run"
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
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _load_json(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Stage0ValidationError(f"unreadable pack json: {path.name}") from exc
    if not isinstance(payload, dict):
        raise Stage0ValidationError(f"pack json must be object: {path.name}")
    return payload


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

    for required in (manifest_path, seal_path, zip_path, envelope_path, claim_path):
        if not required.is_file():
            raise Stage0ValidationError(f"evidence pack missing: {required.name}")

    manifest = _load_json(manifest_path)
    seal = _load_json(seal_path)
    envelope = _load_json(envelope_path)
    claim = _load_json(claim_path)

    if (
        manifest.get("schema_version") != PACK_SCHEMA_VERSION
        or manifest.get("kind") != PACK_KIND
    ):
        raise Stage0ValidationError("evidence pack kind/schema is unsupported")
    if envelope.get("status") != "DONE":
        raise Stage0ValidationError("dry-run refused: envelope status is not DONE")

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
    if not isinstance(non_goals, list) or "no_merge" not in non_goals:
        raise Stage0ValidationError("evidence pack must declare no_merge non-goal")

    zip_sha = _sha256_file(zip_path)
    if seal.get("pack_sha256") != zip_sha:
        raise Stage0ValidationError("evidence pack zip sha256 does not match seal")

    # Seal is written after zip in the packer; require core members inside zip.
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = set(zf.namelist())
        for member in (
            "pack-manifest.json",
            "result-envelope.json",
            "claim.json",
        ):
            if member not in names:
                raise Stage0ValidationError(f"evidence zip missing member: {member}")

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
    for item in artifacts:
        if not isinstance(item, Mapping):
            raise Stage0ValidationError("artifact record invalid")
        name = item.get("name")
        sha = item.get("sha256")
        if type(name) is not str or type(sha) is not str:
            raise Stage0ValidationError("artifact record fields invalid")
        artifact_path = files_dir / name.replace("/", "_")
        if not artifact_path.is_file():
            raise Stage0ValidationError(f"pack artifact file missing: {name}")
        if _sha256_file(artifact_path) != sha:
            raise Stage0ValidationError(f"pack artifact sha256 mismatch: {name}")

    candidate = {
        "schema_version": 1,
        "kind": "experiment-evidence-candidate",
        "workflow_run_id": manifest.get("workflow_run_id"),
        "canonical_input_sha256": manifest.get("canonical_input_sha256"),
        "claim_generation": claim.get("generation"),
        "task_id": claim.get("task_id"),
        "runner_id": claim.get("runner_id"),
        "envelope_status": envelope.get("status"),
        "artifact_count": len(artifacts),
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

"""Optional L4 bridge: validate an external-workflow Stage4/5 evidence pack.

Uses the Stage5 dry-run validator when available. Never imports into Dyro Core.
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

from .errors import DispatchValidationError


def dry_run_stage5_pack(
    pack_dir: Path,
    *,
    production_actions: Mapping[str, object] | None = None,
) -> dict[str, object]:
    if production_actions:
        for key in ("merge", "push", "signoff", "import_evidence"):
            if production_actions.get(key):
                raise DispatchValidationError(
                    f"stage5 bridge forbids production action: {key}"
                )
    pack_dir = Path(pack_dir)
    if not pack_dir.is_dir():
        raise DispatchValidationError(f"pack_dir missing: {pack_dir}")

    try:
        from experiments.external_workflow_runner.stage5.evidence_dry_run import (
            dry_run_validate_pack,
        )
    except ImportError as exc:
        raise DispatchValidationError(
            "Stage5 dry-run module is not available in this checkout"
        ) from exc

    result = dry_run_validate_pack(
        pack_dir, production_actions=production_actions
    )
    return {
        "schema_version": 1,
        "kind": "local-dispatch-stage5-bridge",
        "pack_dir": str(result.pack_dir),
        "report_path": str(result.report_path),
        "pack_sha256_verified": result.pack_sha256_verified,
        "verdict": result.report.get("verdict"),
        "candidate_record": result.candidate_record,
        "dyro_core_import_attempted": False,
        "production_ready": False,
    }

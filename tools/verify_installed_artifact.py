"""Verify runtime resources from an installed dyro distribution, outside checkout."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile

import experiments.external_workflow_runner as runtime_package
from experiments.external_workflow_runner.doctor import (
    collect_runtime_diagnostics,
)
from experiments.external_workflow_runner.stage1.bundle import (
    BUNDLE_SRC as STAGE1_BUNDLE_SRC,
    assemble_stage1_bundle,
)
from experiments.external_workflow_runner.stage1.package_runtime import RUNTIME_SOURCE
from experiments.external_workflow_runner.stage2.bundle import (
    BUNDLE_SRC as STAGE2_BUNDLE_SRC,
    assemble_stage2_bundle,
)
from experiments.external_workflow_runner.stage3.bundle import (
    BUNDLE_SRC as STAGE3_BUNDLE_SRC,
    assemble_stage3_bundle,
)
from experiments.external_workflow_runner.stage4.bundle import (
    BUNDLE_SRC as STAGE4_BUNDLE_SRC,
    assemble_stage4_bundle,
)
from experiments.external_workflow_runner.stage5.bundle import (
    BUNDLE_SRC as STAGE5_BUNDLE_SRC,
    assemble_stage5_bundle,
)
from experiments.external_workflow_runner.stage5.host_provider import (
    pin_host_provider,
    write_host_fixture_cli,
)
from experiments.external_workflow_runner.stage5.core_handoff import (
    build_core_evidence_handoff,
)
from experiments.external_workflow_runner.stage5.production_operator import (
    describe_production_schemas,
    prepare_release_manifest,
)
from experiments.external_workflow_runner.manifest import verify_bundle_manifest


def _verify_bundle(result: dict[str, object], *, expected_stage: int) -> None:
    manifest = result.get("manifest")
    identity = result.get("identity")
    bundle_root = result.get("bundle_root")
    if (
        not isinstance(manifest, dict)
        or not manifest.get("files")
        or not isinstance(identity, dict)
        or identity.get("stage") != expected_stage
        or not isinstance(bundle_root, Path)
    ):
        raise SystemExit(
            f"installed Stage {expected_stage} bundle assembly is incomplete"
        )
    verify_bundle_manifest(
        bundle_root,
        manifest,
        expected_identity=identity,
    )


def main() -> None:
    if not callable(collect_runtime_diagnostics) or not callable(
        build_core_evidence_handoff
    ) or not callable(prepare_release_manifest):
        raise SystemExit("installed runtime operator modules are unavailable")
    package_root = Path(runtime_package.__file__).resolve().parent
    resources = {
        "runtime": RUNTIME_SOURCE,
        "stage1": STAGE1_BUNDLE_SRC,
        "stage2": STAGE2_BUNDLE_SRC,
        "stage3": STAGE3_BUNDLE_SRC,
        "stage4": STAGE4_BUNDLE_SRC,
        "stage5": STAGE5_BUNDLE_SRC,
    }
    missing = [name for name, path in resources.items() if not path.is_dir()]
    if missing:
        raise SystemExit(f"installed runtime resources missing: {missing}")
    schema_paths = {
        "result-envelope": package_root / "schemas/result-envelope.schema.json",
        "production-deployment-manifest": (
            package_root
            / "schemas/production-deployment-manifest.schema.json"
        ),
        "production-attestation": (
            package_root / "schemas/production-attestation.schema.json"
        ),
    }
    missing_schemas = [
        name for name, path in schema_paths.items() if not path.is_file()
    ]
    if missing_schemas:
        raise SystemExit(
            f"installed runtime schemas missing: {missing_schemas}"
        )
    for name, path in schema_paths.items():
        try:
            schema = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SystemExit(
                f"installed runtime schema is unreadable: {name}"
            ) from exc
        if (
            not isinstance(schema, dict)
            or schema.get("$schema")
            != "https://json-schema.org/draft/2020-12/schema"
        ):
            raise SystemExit(f"installed runtime schema is invalid: {name}")
    with tempfile.TemporaryDirectory() as tmp:
        temporary_root = Path(tmp)
        schema_contract = describe_production_schemas()
        if (
            schema_contract.get("verdict") != "LOCATED"
            or schema_contract.get("written") is not False
        ):
            raise SystemExit("installed production schema contract is unavailable")
        release_inputs = {
            name: temporary_root / f"{name}.input"
            for name in (
                "wheel_sha256",
                "sdist_sha256",
                "sbom_sha256",
                "provenance_sha256",
                "deployment_sha256",
                "canary_plan_sha256",
                "rollback_plan_sha256",
                "observability_plan_sha256",
                "runbook_sha256",
            )
        }
        for name, path in release_inputs.items():
            path.write_bytes(name.encode("ascii"))
        provider = temporary_root / "provider.bin"
        provider.write_bytes(b"installed-provider")
        dry_release = prepare_release_manifest(
            release_id="installed-smoke",
            environment_id="prod/smoke",
            source_commit="1" * 40,
            artifacts={
                name: release_inputs[name]
                for name in (
                    "wheel_sha256",
                    "sdist_sha256",
                    "sbom_sha256",
                    "provenance_sha256",
                )
            },
            providers=(("smoke", provider),),
            operations={
                name: release_inputs[name]
                for name in (
                    "deployment_sha256",
                    "canary_plan_sha256",
                    "rollback_plan_sha256",
                    "observability_plan_sha256",
                    "runbook_sha256",
                )
            },
            output=temporary_root / "release.unsigned.json",
            dry_run=True,
        )
        if (
            dry_release.get("verdict") != "DRY_RUN"
            or dry_release.get("written") is not False
            or (temporary_root / "release.unsigned.json").exists()
        ):
            raise SystemExit(
                "installed production release preparation is unsafe"
            )
        runtime_lock_path = package_root / "runtime-lock.json"
        assemblers = (
            (1, assemble_stage1_bundle),
            (2, assemble_stage2_bundle),
            (3, assemble_stage3_bundle),
            (4, assemble_stage4_bundle),
        )
        for stage, assembler in assemblers:
            result = assembler(
                temporary_root / f"stage{stage}-bundle",
                runtime_lock_path=runtime_lock_path,
            )
            _verify_bundle(result, expected_stage=stage)
        provider = write_host_fixture_cli(temporary_root / "host-provider.ts")
        provider_pin = pin_host_provider(
            provider,
            allowed_roots=(temporary_root,),
        )
        stage5 = assemble_stage5_bundle(
            temporary_root / "stage5-bundle",
            runtime_lock_path=runtime_lock_path,
            host_provider=provider_pin,
        )
        _verify_bundle(stage5, expected_stage=5)
    print(f"installed artifact verified from {package_root}")


if __name__ == "__main__":
    main()

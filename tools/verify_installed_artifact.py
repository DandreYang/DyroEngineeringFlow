"""Verify runtime resources from an installed dyro distribution, outside checkout."""

from __future__ import annotations

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
    ):
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
    with tempfile.TemporaryDirectory() as tmp:
        temporary_root = Path(tmp)
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

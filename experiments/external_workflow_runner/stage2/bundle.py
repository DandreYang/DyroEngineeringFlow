"""Assemble Stage 2 bundle: first-party semantic-flow + Stage2 sources."""

from __future__ import annotations

from pathlib import Path
import shutil

from ..errors import Stage0ValidationError
from ..manifest import build_bundle_manifest
from ..stage1.bundle import stage1_identity
from ..stage1.package_runtime import (
    VENDOR_DIR_NAME,
    package_semantic_flow_runtime,
)

STAGE2_DIR = Path(__file__).resolve().parent
BUNDLE_SRC = STAGE2_DIR / "bundle_src"


def assemble_stage2_bundle(
    destination: Path,
    *,
    runtime_lock_path: Path,
    tarball_source: Path | None = None,
) -> dict[str, object]:
    del tarball_source
    destination = Path(destination)
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    if not BUNDLE_SRC.is_dir():
        raise Stage0ValidationError("Stage 2 bundle_src is missing")
    for name in ("workflow.ts", "broker_server.ts"):
        source = BUNDLE_SRC / name
        if not source.is_file():
            raise Stage0ValidationError(f"Stage 2 bundle source missing: {name}")
        shutil.copy2(source, destination / name)

    packaged = package_semantic_flow_runtime(
        destination / "_pkg",
        runtime_lock_path=runtime_lock_path,
    )
    vendor_target = destination / "vendor" / VENDOR_DIR_NAME
    vendor_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(packaged.package_root, vendor_target)
    shutil.copy2(packaged.vendor_root / "package.json", destination / "package.json")
    shutil.copy2(
        packaged.vendor_root / "runtime-package-lock.json",
        destination / "runtime-package-lock.json",
    )
    shutil.copy2(
        packaged.vendor_root / "install-receipt.json",
        destination / "install-receipt.json",
    )
    shutil.rmtree(destination / "_pkg")

    identity = {
        **stage1_identity(),
        "stage": 2,
        "provider_modes": ["fake", "simulated-cli"],
        "ipc_protocol_versions": [1, 2],
    }
    manifest = build_bundle_manifest(destination, identity=identity)
    return {
        "identity": identity,
        "manifest": manifest,
        "install_receipt": packaged.lock_record,
        "bundle_root": destination,
    }

"""Assemble a fixed, reviewed Stage 1 workflow bundle with vendored runtime."""

from __future__ import annotations

from pathlib import Path
import shutil

from ..errors import Stage0ValidationError
from ..manifest import build_bundle_manifest
from .install import (
    EXPECTED_INTEGRITY,
    IMPLEMENTATION_NAME,
    PACKAGE_NAME,
    PACKAGE_VERSION,
    SOURCE_COMMIT,
    SOURCE_TAG,
    install_verified_runtime,
)
from ..sandbox import BUN_IMAGE, BUN_USER, BUN_VERSION

STAGE1_DIR = Path(__file__).resolve().parent
BUNDLE_SRC = STAGE1_DIR / "bundle_src"


def stage1_identity() -> dict[str, object]:
    return {
        "schema_version": 1,
        "workflow_runtime": {
            "implementation": IMPLEMENTATION_NAME,
            "package": PACKAGE_NAME,
            "version": PACKAGE_VERSION,
            "npm_dist_integrity": EXPECTED_INTEGRITY,
            "same_version_source_tag": SOURCE_TAG,
            "source_tag_peeled_commit": SOURCE_COMMIT,
        },
        "runtime": {
            "bun_version": BUN_VERSION,
            "container_image": BUN_IMAGE,
            "container_user": BUN_USER,
        },
        "stage": 1,
    }


def assemble_stage1_bundle(
    destination: Path,
    *,
    runtime_lock_path: Path,
    tarball_source: Path | None = None,
) -> dict[str, object]:
    """
    Build a readonly-ready bundle directory.

    Layout:
      workflow.ts, broker_agent.ts
      vendor/evaluated-typescript-runtime/...
      package.json, runtime-package-lock.json, install-receipt.json
    """
    destination = Path(destination)
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    if not BUNDLE_SRC.is_dir():
        raise Stage0ValidationError("Stage 1 bundle_src is missing")
    for name in ("workflow.ts", "broker_agent.ts", "broker_server.ts"):
        source = BUNDLE_SRC / name
        if not source.is_file():
            raise Stage0ValidationError(f"Stage 1 bundle source missing: {name}")
        shutil.copy2(source, destination / name)
    install = install_verified_runtime(
        destination / "_install",
        runtime_lock_path=runtime_lock_path,
        tarball_source=tarball_source,
    )
    vendor_target = destination / "vendor" / "evaluated-typescript-runtime"
    vendor_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(install.package_root, vendor_target)
    shutil.copy2(install.vendor_root / "package.json", destination / "package.json")
    shutil.copy2(
        install.vendor_root / "runtime-package-lock.json",
        destination / "runtime-package-lock.json",
    )
    shutil.copy2(
        install.vendor_root / "install-receipt.json",
        destination / "install-receipt.json",
    )
    # Drop the intermediate install tree (tarball kept only if needed by tests).
    shutil.rmtree(destination / "_install")
    identity = stage1_identity()
    # Manifest is returned separately; writing it into the bundle would create a
    # circular hash dependency with build_bundle_manifest().
    manifest = build_bundle_manifest(destination, identity=identity)
    return {
        "identity": identity,
        "manifest": manifest,
        "install_receipt": install.lock_record,
        "bundle_root": destination,
    }

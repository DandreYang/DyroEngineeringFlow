"""Assemble Stage 1 bundle with first-party semantic-flow runtime."""

from __future__ import annotations

from pathlib import Path
import shutil

from ..errors import Stage0ValidationError
from ..manifest import build_bundle_manifest
from ..sandbox import BUN_IMAGE, BUN_USER, BUN_VERSION
from .package_runtime import (
    IMPLEMENTATION_NAME,
    RUNTIME_VERSION,
    VENDOR_DIR_NAME,
    hash_runtime_tree,
    package_semantic_flow_runtime,
    RUNTIME_SOURCE,
)

STAGE1_DIR = Path(__file__).resolve().parent
BUNDLE_SRC = STAGE1_DIR / "bundle_src"


def stage1_identity() -> dict[str, object]:
    content_sha256 = hash_runtime_tree(RUNTIME_SOURCE)
    return {
        "schema_version": 1,
        "workflow_runtime": {
            "implementation": IMPLEMENTATION_NAME,
            "version": RUNTIME_VERSION,
            "content_sha256": content_sha256,
            "origin": "first-party",
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
      workflow.ts, broker_agent.ts, broker_server.ts
      vendor/dyro-semantic-flow/...
      package.json, runtime-package-lock.json, install-receipt.json

    tarball_source is ignored (kept for call-site compatibility).
    """
    del tarball_source
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

    identity = stage1_identity()
    manifest = build_bundle_manifest(destination, identity=identity)
    return {
        "identity": identity,
        "manifest": manifest,
        "install_receipt": packaged.lock_record,
        "bundle_root": destination,
    }

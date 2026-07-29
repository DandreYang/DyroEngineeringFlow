"""Assemble Stage 3 bundle with argv-cli provider fixtures."""

from __future__ import annotations

from pathlib import Path
import shutil

from ..errors import Stage0ValidationError
from ..manifest import build_bundle_manifest
from ..stage1.bundle import stage1_identity
from ..stage1.install import install_verified_runtime

STAGE3_DIR = Path(__file__).resolve().parent
BUNDLE_SRC = STAGE3_DIR / "bundle_src"


def assemble_stage3_bundle(
    destination: Path,
    *,
    runtime_lock_path: Path,
    tarball_source: Path | None = None,
) -> dict[str, object]:
    destination = Path(destination)
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    for name in ("workflow.ts", "broker_server.ts", "fake_provider_cli.ts"):
        source = BUNDLE_SRC / name
        if not source.is_file():
            raise Stage0ValidationError(f"Stage 3 bundle source missing: {name}")
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
    shutil.rmtree(destination / "_install")

    identity = {
        **stage1_identity(),
        "stage": 3,
        "provider_modes": ["fake", "simulated-cli", "argv-cli"],
        "ipc_protocol_versions": [1, 2],
        "provider_cli": {
            "invocation": "argv-only",
            "fixture": "fake_provider_cli.ts",
            "credentials_in_sandbox": False,
        },
    }
    manifest = build_bundle_manifest(destination, identity=identity)
    return {
        "identity": identity,
        "manifest": manifest,
        "install_receipt": install.lock_record,
        "bundle_root": destination,
    }

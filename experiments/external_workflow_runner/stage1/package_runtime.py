"""Package the first-party Dyro semantic-flow runtime into a sandbox vendor tree."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import shutil

from ..errors import Stage0ValidationError
from ..sandbox import BUN_IMAGE, BUN_USER, BUN_VERSION


IMPLEMENTATION_NAME = "dyro-semantic-flow"
RUNTIME_VERSION = "1.0.0"
VENDOR_DIR_NAME = "dyro-semantic-flow"
RUNTIME_SOURCE = Path(__file__).resolve().parents[1] / "ts_runtime"


@dataclass(frozen=True)
class RuntimePackageResult:
    vendor_root: Path
    package_root: Path
    content_sha256: str
    lock_record: dict[str, object]


def load_runtime_lock(lock_path: Path) -> dict[str, object]:
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Stage0ValidationError("runtime-lock.json is unreadable") from exc
    if not isinstance(payload, dict):
        raise Stage0ValidationError("runtime-lock.json must be an object")
    return payload


def hash_runtime_tree(root: Path) -> str:
    if not root.is_dir():
        raise Stage0ValidationError("semantic-flow runtime source is missing")
    digest = hashlib.sha256()
    files = sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and not path.name.startswith(".")
    )
    if not files:
        raise Stage0ValidationError("semantic-flow runtime source is empty")
    for path in files:
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def verify_runtime_lock(lock: dict[str, object], *, content_sha256: str) -> None:
    workflow = lock.get("workflow_runtime")
    runtime = lock.get("runtime")
    if not isinstance(workflow, dict) or not isinstance(runtime, dict):
        raise Stage0ValidationError("runtime-lock.json is missing required sections")
    if (
        lock.get("schema_version") != 1
        or workflow.get("implementation") != IMPLEMENTATION_NAME
        or workflow.get("version") != RUNTIME_VERSION
        or workflow.get("content_sha256") != content_sha256
        or runtime.get("bun_version") != BUN_VERSION
        or runtime.get("container_image") != BUN_IMAGE
        or runtime.get("container_user") != BUN_USER
    ):
        raise Stage0ValidationError(
            "runtime-lock.json does not match the first-party semantic-flow identity"
        )


def package_semantic_flow_runtime(
    destination: Path,
    *,
    runtime_lock_path: Path,
) -> RuntimePackageResult:
    """
    Copy first-party ts_runtime into destination and verify content hash lock.
    """
    destination = Path(destination)
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=False)

    content_sha256 = hash_runtime_tree(RUNTIME_SOURCE)
    lock = load_runtime_lock(runtime_lock_path)
    verify_runtime_lock(lock, content_sha256=content_sha256)

    package_root = destination / "vendor" / VENDOR_DIR_NAME
    package_root.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(RUNTIME_SOURCE, package_root)

    lock_record = {
        "implementation": IMPLEMENTATION_NAME,
        "version": RUNTIME_VERSION,
        "content_sha256": content_sha256,
        "vendor_root": f"vendor/{VENDOR_DIR_NAME}",
        "transitive_count": 0,
        "origin": "first-party",
        "notes": [
            "Dyro first-party semantic-flow runtime; no third-party workflow package.",
            "Identity is the SHA-256 of the checked-in ts_runtime tree.",
        ],
    }
    package_manifest = {
        "name": "@dyro/semantic-flow",
        "private": True,
        "type": "module",
        "version": RUNTIME_VERSION,
        "dyroRuntimePin": lock_record,
    }
    (destination / "package.json").write_text(
        json.dumps(package_manifest, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    (destination / "runtime-package-lock.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "lockfile_kind": "dyro-first-party-semantic-flow",
                "packages": {
                    "@dyro/semantic-flow": {
                        "version": RUNTIME_VERSION,
                        "content_sha256": content_sha256,
                        "dependencies": {},
                    }
                },
                "transitive_count": 0,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (destination / "install-receipt.json").write_text(
        json.dumps(lock_record, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return RuntimePackageResult(
        vendor_root=destination,
        package_root=package_root,
        content_sha256=content_sha256,
        lock_record=lock_record,
    )


# Back-compat aliases used by older stage imports during transition.
RuntimeInstallResult = RuntimePackageResult
install_verified_runtime = package_semantic_flow_runtime
EXPECTED_CONTENT_SHA256 = None  # resolved dynamically from tree + lock
PACKAGE_VERSION = RUNTIME_VERSION

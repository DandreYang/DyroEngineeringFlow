"""Install and verify the frozen TypeScript workflow runtime identity."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import shutil
import tarfile
import urllib.request

from ..errors import Stage0ValidationError
from ..sandbox import BUN_IMAGE, BUN_USER, BUN_VERSION


PACKAGE_NAME = "@dyro/semantic-flow"
PACKAGE_VERSION = "0.2.0"
PACKAGE_TARBALL_URL = (
    "https://registry.npmjs.org/@dyro/semantic-flow/-/semantic-flow-0.2.0.tgz"
)
EXPECTED_INTEGRITY = "sha512-sJgf79AHIwx67b570lMOuQjpouXepXSlfTeLXNobEubYzcViQZslnqRw2XEvYjF9+N3VUlpy6ID5qziSS1ICBw=="
IMPLEMENTATION_NAME = "evaluated-typescript-runtime"
SOURCE_TAG = "v0.2.0"
SOURCE_COMMIT = "73c61156197445be4a0fad390e3a1d802f2cda4a"
MAX_TARBALL_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class RuntimeInstallResult:
    vendor_root: Path
    package_root: Path
    tarball_path: Path
    integrity: str
    package_json_sha256: str
    lock_record: dict[str, object]


def load_runtime_lock(lock_path: Path) -> dict[str, object]:
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Stage0ValidationError("runtime-lock.json is unreadable") from exc
    if not isinstance(payload, dict):
        raise Stage0ValidationError("runtime-lock.json must be an object")
    return payload


def verify_runtime_lock(lock: dict[str, object]) -> None:
    workflow = lock.get("workflow_runtime")
    runtime = lock.get("runtime")
    if not isinstance(workflow, dict) or not isinstance(runtime, dict):
        raise Stage0ValidationError("runtime-lock.json is missing required sections")
    if (
        lock.get("schema_version") != 1
        or workflow.get("implementation") != IMPLEMENTATION_NAME
        or workflow.get("version") != PACKAGE_VERSION
        or workflow.get("npm_dist_integrity") != EXPECTED_INTEGRITY
        or workflow.get("same_version_source_tag") != SOURCE_TAG
        or workflow.get("source_tag_peeled_commit") != SOURCE_COMMIT
        or runtime.get("bun_version") != BUN_VERSION
        or runtime.get("container_image") != BUN_IMAGE
        or runtime.get("container_user") != BUN_USER
    ):
        raise Stage0ValidationError(
            "runtime-lock.json does not match the approved Stage 1 identity"
        )


def integrity_of(data: bytes) -> str:
    return "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode("ascii")


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _download_tarball(destination: Path) -> bytes:
    request = urllib.request.Request(
        PACKAGE_TARBALL_URL,
        headers={"User-Agent": "dyro-external-workflow-runner-stage1"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_TARBALL_BYTES:
                raise Stage0ValidationError("runtime tarball exceeds size limit")
            chunks.append(chunk)
    payload = b"".join(chunks)
    destination.write_bytes(payload)
    return payload


def _safe_extract(tarball: Path, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tarball, "r:gz") as archive:
        members = archive.getmembers()
        if len(members) > 512:
            raise Stage0ValidationError("runtime tarball contains too many entries")
        for member in members:
            name = member.name
            if name.startswith("/") or ".." in Path(name).parts:
                raise Stage0ValidationError(f"runtime tarball path is unsafe: {name}")
            if member.issym() or member.islnk():
                raise Stage0ValidationError(f"runtime tarball contains a link: {name}")
            if member.size > MAX_TARBALL_BYTES:
                raise Stage0ValidationError(
                    f"runtime tarball member is too large: {name}"
                )
        archive.extractall(destination, filter="data")
    package_root = destination / "package"
    if not package_root.is_dir():
        raise Stage0ValidationError("runtime tarball missing package/ directory")
    package_json = package_root / "package.json"
    if not package_json.is_file():
        raise Stage0ValidationError("runtime package.json is missing")
    try:
        metadata = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Stage0ValidationError("runtime package.json is invalid") from exc
    if (
        not isinstance(metadata, dict)
        or metadata.get("name") != PACKAGE_NAME
        or metadata.get("version") != PACKAGE_VERSION
    ):
        raise Stage0ValidationError(
            "runtime package.json identity does not match the frozen release"
        )
    return package_root


def write_frozen_package_manifest(path: Path, *, integrity: str) -> None:
    """Write a project-local package manifest that pins the exact npm release."""
    payload = {
        "name": "external-workflow-runner-stage1-bundle",
        "private": True,
        "type": "module",
        "dependencies": {
            PACKAGE_NAME: PACKAGE_VERSION,
        },
        "overrides": {
            PACKAGE_NAME: {
                "version": PACKAGE_VERSION,
            }
        },
        "dyroRuntimePin": {
            "implementation": IMPLEMENTATION_NAME,
            "package": PACKAGE_NAME,
            "version": PACKAGE_VERSION,
            "npm_dist_integrity": integrity,
            "same_version_source_tag": SOURCE_TAG,
            "source_tag_peeled_commit": SOURCE_COMMIT,
            "install_policy": "verified-tarball-only-no-runtime-install",
        },
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def write_frozen_lockfile(path: Path, *, integrity: str, tarball_sha256: str) -> None:
    """Write a deterministic lock record used instead of a floating resolver."""
    payload = {
        "schema_version": 1,
        "lockfile_kind": "dyro-stage1-runtime-lock",
        "packages": {
            PACKAGE_NAME: {
                "version": PACKAGE_VERSION,
                "integrity": integrity,
                "resolved": PACKAGE_TARBALL_URL,
                "tarball_sha256": tarball_sha256,
                "dependencies": {},
            }
        },
        "transitive_count": 0,
        "notes": [
            "The evaluated TypeScript runtime 0.2.0 publishes no production dependencies.",
            "Runtime installation is performed only by install_verified_runtime().",
            "Sandbox execution must never run bun install or npm install.",
        ],
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def install_verified_runtime(
    destination: Path,
    *,
    runtime_lock_path: Path,
    tarball_source: Path | None = None,
) -> RuntimeInstallResult:
    """
    Install the frozen runtime into destination after integrity verification.

    When tarball_source is provided, network is not used. The bytes must still
    match the integrity recorded in runtime-lock.json.
    """
    lock = load_runtime_lock(runtime_lock_path)
    verify_runtime_lock(lock)
    destination = Path(destination)
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=False)
    tarball_path = destination / "semantic-flow-0.2.0.tgz"
    if tarball_source is not None:
        payload = Path(tarball_source).read_bytes()
        if len(payload) > MAX_TARBALL_BYTES:
            raise Stage0ValidationError("runtime tarball exceeds size limit")
        tarball_path.write_bytes(payload)
    else:
        payload = _download_tarball(tarball_path)
    integrity = integrity_of(payload)
    if integrity != EXPECTED_INTEGRITY:
        raise Stage0ValidationError(
            "runtime tarball integrity does not match runtime-lock.json"
        )
    package_root = _safe_extract(tarball_path, destination / "extract")
    vendor_root = destination / "vendor" / "evaluated-typescript-runtime"
    if vendor_root.exists():
        shutil.rmtree(vendor_root)
    shutil.copytree(package_root, vendor_root)
    package_json = vendor_root / "package.json"
    package_json_sha256 = sha256_of(package_json)
    write_frozen_package_manifest(
        destination / "package.json",
        integrity=integrity,
    )
    write_frozen_lockfile(
        destination / "runtime-package-lock.json",
        integrity=integrity,
        tarball_sha256=sha256_of(tarball_path),
    )
    lock_record = {
        "package": PACKAGE_NAME,
        "version": PACKAGE_VERSION,
        "integrity": integrity,
        "tarball_sha256": sha256_of(tarball_path),
        "package_json_sha256": package_json_sha256,
        "vendor_root": "vendor/evaluated-typescript-runtime",
        "implementation": IMPLEMENTATION_NAME,
    }
    (destination / "install-receipt.json").write_text(
        json.dumps(lock_record, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return RuntimeInstallResult(
        vendor_root=destination,
        package_root=vendor_root,
        tarball_path=tarball_path,
        integrity=integrity,
        package_json_sha256=package_json_sha256,
        lock_record=lock_record,
    )

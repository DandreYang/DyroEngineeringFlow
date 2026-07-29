"""Minimal Stage 0 supervisor that never receives a signing key."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
from typing import Mapping, Sequence

from .artifacts import ArtifactPolicy, ValidatedArtifact, validate_artifacts
from .errors import Stage0ValidationError
from .manifest import verify_bundle_manifest
from .result import validate_result_envelope
from .sandbox import (
    BUN_IMAGE,
    BUN_USER,
    BUN_VERSION,
    DockerSandboxConfig,
    DockerSandboxResult,
    DockerSandboxRunner,
)


@dataclass(frozen=True)
class SupervisorConfig:
    sandbox: DockerSandboxConfig
    bundle_manifest: Mapping[str, object]
    bundle_identity: Mapping[str, object]
    workflow_run_id: str
    expected_branches: Mapping[str, bool]
    artifact_policy: ArtifactPolicy
    result_filename: str
    max_result_bytes: int

    def __post_init__(self) -> None:
        if not isinstance(self.bundle_manifest, Mapping):
            raise Stage0ValidationError("bundle_manifest must be an object")
        if not isinstance(self.bundle_identity, Mapping):
            raise Stage0ValidationError("bundle_identity must be an object")
        runtime_identity = self.bundle_identity.get("runtime")
        if (
            type(self.bundle_identity.get("schema_version")) is not int
            or self.bundle_identity.get("schema_version") != 1
            or not isinstance(runtime_identity, Mapping)
            or runtime_identity.get("bun_version") != BUN_VERSION
            or runtime_identity.get("container_image") != BUN_IMAGE
            or runtime_identity.get("container_user") != BUN_USER
            or self.sandbox.image != runtime_identity.get("container_image")
        ):
            raise Stage0ValidationError(
                "bundle runtime identity does not match the approved sandbox runtime"
            )
        if not self.workflow_run_id or len(self.workflow_run_id) > 128:
            raise Stage0ValidationError(
                "workflow_run_id must contain 1 to 128 characters"
            )
        if not self.expected_branches:
            raise Stage0ValidationError("expected_branches must not be empty")
        if any(
            not isinstance(branch_id, str)
            or not branch_id
            or len(branch_id) > 128
            or not isinstance(critical, bool)
            for branch_id, critical in self.expected_branches.items()
        ):
            raise Stage0ValidationError(
                "expected_branches contains an invalid declaration"
            )
        if (
            not self.result_filename
            or self.result_filename in (".", "..")
            or "/" in self.result_filename
            or "\\" in self.result_filename
            or "\x00" in self.result_filename
        ):
            raise Stage0ValidationError(
                "result_filename must be one safe path component"
            )
        if type(self.max_result_bytes) is not int or self.max_result_bytes <= 0:
            raise Stage0ValidationError("max_result_bytes must be positive")
        if self.sandbox.environment.get("DYRO_WORKFLOW_RUN_ID") != self.workflow_run_id:
            raise Stage0ValidationError(
                "sandbox workflow run ID must match the supervisor value"
            )
        expected_result_path = f"/run/dyro/{self.result_filename}"
        if self.sandbox.environment.get("DYRO_RESULT_PATH") != expected_result_path:
            raise Stage0ValidationError(
                "sandbox result path must match the supervisor result filename"
            )


@dataclass(frozen=True)
class SupervisedResult:
    process: DockerSandboxResult
    envelope: dict[str, object]
    artifacts: tuple[ValidatedArtifact, ...]


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    decoded: dict[str, object] = {}
    for name, value in pairs:
        if name in decoded:
            raise Stage0ValidationError(f"result JSON contains a duplicate key: {name}")
        decoded[name] = value
    return decoded


def _reject_non_finite_number(value: str) -> object:
    raise Stage0ValidationError(f"result JSON contains a non-finite number: {value}")


def _read_result(root: Path, filename: str, max_bytes: int) -> dict[str, object]:
    if root.is_symlink() or not root.is_dir():
        raise Stage0ValidationError("result root must be a non-symlink directory")
    root_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    file_flags = (
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    root_fd = os.open(root, root_flags)
    file_fd: int | None = None
    try:
        try:
            metadata = os.stat(filename, dir_fd=root_fd, follow_symlinks=False)
        except OSError as exc:
            raise Stage0ValidationError("result envelope is missing") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise Stage0ValidationError("result envelope must not be a symbolic link")
        if not stat.S_ISREG(metadata.st_mode):
            raise Stage0ValidationError("result envelope must be a regular file")
        if metadata.st_size > max_bytes:
            raise Stage0ValidationError(
                f"result envelope exceeds byte limit: {metadata.st_size} > {max_bytes}"
            )
        file_fd = os.open(filename, file_flags, dir_fd=root_fd)
        opened = os.fstat(file_fd)
        if opened.st_dev != metadata.st_dev or opened.st_ino != metadata.st_ino:
            raise Stage0ValidationError("result envelope changed before it was opened")
        payload = bytearray()
        while len(payload) <= max_bytes:
            chunk = os.read(file_fd, min(64 * 1024, max_bytes + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) > max_bytes:
            raise Stage0ValidationError("result envelope grew beyond its byte limit")
        final = os.fstat(file_fd)
        if (
            final.st_size != opened.st_size
            or final.st_mtime_ns != opened.st_mtime_ns
            or len(payload) != final.st_size
        ):
            raise Stage0ValidationError("result envelope changed while it was read")
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(root_fd)
    try:
        decoded = json.loads(
            bytes(payload).decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite_number,
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise Stage0ValidationError("result envelope is not strict UTF-8 JSON") from exc
    if not isinstance(decoded, dict):
        raise Stage0ValidationError("result envelope JSON must be an object")
    return decoded


class Stage0Supervisor:
    """Verify, execute, re-verify, and return only validated data."""

    def __init__(self, config: SupervisorConfig) -> None:
        self.config = config
        self._bundle_manifest = deepcopy(dict(config.bundle_manifest))
        self._bundle_identity = deepcopy(dict(config.bundle_identity))
        self._expected_branches = dict(config.expected_branches)

    def execute(
        self,
        command: Sequence[str],
        *,
        timeout_seconds: float,
    ) -> SupervisedResult:
        result_path = self.config.sandbox.run_root / self.config.result_filename
        if result_path.exists() or result_path.is_symlink():
            raise Stage0ValidationError("result path must not exist before execution")
        verify_bundle_manifest(
            self.config.sandbox.bundle_root,
            self._bundle_manifest,
            expected_identity=self._bundle_identity,
        )
        try:
            process = DockerSandboxRunner(self.config.sandbox).run(
                command,
                timeout_seconds=timeout_seconds,
            )
            if process.returncode != 0:
                detail = (process.stderr or process.stdout or "").strip()
                if len(detail) > 1200:
                    detail = detail[-1200:]
                suffix = f": {detail}" if detail else ""
                raise Stage0ValidationError(
                    f"Workflow sandbox exited non-zero: {process.returncode}{suffix}"
                )
            envelope = validate_result_envelope(
                _read_result(
                    self.config.sandbox.run_root,
                    self.config.result_filename,
                    self.config.max_result_bytes,
                ),
                workflow_run_id=self.config.workflow_run_id,
                expected_branches=self._expected_branches,
            )
            artifacts = validate_artifacts(
                envelope["artifacts"],
                self.config.artifact_policy,
            )
            return SupervisedResult(
                process=process,
                envelope=envelope,
                artifacts=artifacts,
            )
        finally:
            verify_bundle_manifest(
                self.config.sandbox.bundle_root,
                self._bundle_manifest,
                expected_identity=self._bundle_identity,
            )

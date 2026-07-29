"""Docker-backed Stage 0 Workflow Sandbox."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import os
from pathlib import Path
import re
import secrets
import subprocess
import time
from types import MappingProxyType
from typing import Mapping, Sequence

from .errors import Stage0ValidationError
from .process import ProcessLimits, run_bounded_process


BUN_IMAGE = (
    "oven/bun@sha256:478281fdd196871c7e51ba6a820b7803a8ae97042ec86cdbc2e1c6b6626442d9"
)
BUN_VERSION = "1.3.11"
BUN_USER = "1000:1000"
_IMAGE = re.compile(r"^[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}$")
_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
_WORKTREE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MEMORY = re.compile(r"^[1-9][0-9]*(?:[kKmMgG])$")
_CLEANUP_TOKEN = re.compile(r"^[0-9a-f]{32}$")
_CLEANUP_LABEL = "com.dyro.external-workflow-runner.cleanup-token"
_ALLOWED_ENVIRONMENT = {
    "DYRO_WORKFLOW_RUN_ID",
    "DYRO_RESULT_PATH",
    "DYRO_BROKER_HOST",
    "DYRO_BROKER_PORT",
    "DYRO_CANONICAL_INPUT_PATH",
    "DYRO_IPC_PROTOCOL_VERSION",
    "DYRO_PROVIDER_MODE",
    "DYRO_STAGE2_HOLD_MS",
    "HOME",
    "TMPDIR",
    "BUN_INSTALL_CACHE_DIR",
    "XDG_CACHE_HOME",
    "LANG",
    "LC_ALL",
    "TZ",
}
_NETWORK_MODE = re.compile(r"^(?:none|container:[A-Za-z0-9][A-Za-z0-9_.-]{0,127})$")


def _docker_environment() -> dict[str, str]:
    return {
        name: os.environ[name]
        for name in (
            "DOCKER_CONFIG",
            "DOCKER_CONTEXT",
            "DOCKER_HOST",
            "HOME",
            "PATH",
        )
        if name in os.environ
    }


def _mount_source(path: Path, label: str) -> Path:
    path = Path(path)
    if not path.is_absolute() or not path.is_dir():
        raise Stage0ValidationError(f"{label} must be an existing absolute directory")
    if path.is_symlink():
        raise Stage0ValidationError(f"{label} must not be a symbolic link")
    resolved = path.resolve(strict=True)
    if "," in os.fspath(resolved):
        raise Stage0ValidationError(
            f"{label} contains a comma unsupported by Docker --mount"
        )
    return resolved


@dataclass(frozen=True)
class DockerSandboxConfig:
    name: str
    image: str
    bundle_root: Path
    run_root: Path
    worktrees: Mapping[str, Path]
    environment: Mapping[str, str]
    memory: str = "512m"
    cpus: float = 1.0
    pids_limit: int = 64
    tmpfs_size: str = "64m"
    max_stdout_bytes: int = 1024 * 1024
    max_stderr_bytes: int = 1024 * 1024
    ipc_root: Path | None = None
    network_mode: str = "none"
    cleanup_token: str = field(
        default_factory=lambda: secrets.token_hex(16),
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if not _NAME.fullmatch(self.name):
            raise Stage0ValidationError("Docker sandbox name is invalid")
        if not _IMAGE.fullmatch(self.image):
            raise Stage0ValidationError("Docker image must use an exact sha256 digest")
        if self.image != BUN_IMAGE:
            raise Stage0ValidationError(
                "Docker image is not the approved Stage 0 runtime"
            )
        if not _NETWORK_MODE.fullmatch(self.network_mode):
            raise Stage0ValidationError(
                "network_mode must be 'none' or container:<name>"
            )
        object.__setattr__(
            self, "bundle_root", _mount_source(self.bundle_root, "bundle_root")
        )
        object.__setattr__(self, "run_root", _mount_source(self.run_root, "run_root"))
        if self.ipc_root is not None:
            object.__setattr__(
                self, "ipc_root", _mount_source(self.ipc_root, "ipc_root")
            )
        if not self.worktrees:
            raise Stage0ValidationError("at least one task worktree is required")
        normalized_worktrees: dict[str, Path] = {}
        for repository, root in self.worktrees.items():
            if not _WORKTREE_ID.fullmatch(repository):
                raise Stage0ValidationError(
                    f"worktree repository ID is invalid: {repository}"
                )
            normalized_worktrees[repository] = _mount_source(
                root, f"worktree {repository}"
            )
        object.__setattr__(self, "worktrees", MappingProxyType(normalized_worktrees))
        normalized_environment = dict(self.environment)
        disallowed = set(normalized_environment) - _ALLOWED_ENVIRONMENT
        if disallowed:
            raise Stage0ValidationError(
                f"environment contains non-allowlisted names: {sorted(disallowed)}"
            )
        for name, value in normalized_environment.items():
            if not isinstance(value, str) or "\x00" in value:
                raise Stage0ValidationError(f"environment value is invalid: {name}")
        object.__setattr__(
            self, "environment", MappingProxyType(normalized_environment)
        )
        if not _MEMORY.fullmatch(self.memory) or not _MEMORY.fullmatch(self.tmpfs_size):
            raise Stage0ValidationError(
                "memory limits must use an integer k/m/g suffix"
            )
        if (
            isinstance(self.cpus, bool)
            or not isinstance(self.cpus, (int, float))
            or not math.isfinite(self.cpus)
            or self.cpus <= 0
            or self.cpus > 8
        ):
            raise Stage0ValidationError(
                "cpus must be greater than zero and no more than 8"
            )
        if type(self.pids_limit) is not int or not 2 <= self.pids_limit <= 512:
            raise Stage0ValidationError("pids_limit must be between 2 and 512")
        if (
            type(self.max_stdout_bytes) is not int
            or type(self.max_stderr_bytes) is not int
            or self.max_stdout_bytes <= 0
            or self.max_stderr_bytes <= 0
        ):
            raise Stage0ValidationError("Docker output limits must be positive")
        if not isinstance(self.cleanup_token, str) or not _CLEANUP_TOKEN.fullmatch(
            self.cleanup_token
        ):
            raise Stage0ValidationError("Docker cleanup token is invalid")

    def argv(self, command: Sequence[str]) -> list[str]:
        if not command or any(
            not isinstance(item, str) or not item or "\x00" in item for item in command
        ):
            raise Stage0ValidationError("sandbox command must be a non-empty argv")
        argv = [
            "docker",
            "run",
            "--rm",
            "--pull",
            "never",
            "--init",
            "--name",
            self.name,
            "--label",
            f"{_CLEANUP_LABEL}={self.cleanup_token}",
            "--network",
            self.network_mode,
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            str(self.pids_limit),
            "--memory",
            self.memory,
            "--cpus",
            str(self.cpus),
            "--user",
            BUN_USER,
            "--tmpfs",
            f"/tmp:rw,noexec,nosuid,nodev,size={self.tmpfs_size}",
            "--mount",
            f"type=bind,src={self.bundle_root},dst=/opt/workflow,readonly",
            "--mount",
            f"type=bind,src={self.run_root},dst=/run/dyro",
        ]
        if self.ipc_root is not None:
            argv.extend(
                [
                    "--mount",
                    f"type=bind,src={self.ipc_root},dst=/run/broker",
                ]
            )
        for repository, root in sorted(self.worktrees.items()):
            argv.extend(
                [
                    "--mount",
                    f"type=bind,src={root},dst=/worktrees/{repository}",
                ]
            )
        for name, value in sorted(self.environment.items()):
            argv.extend(["--env", f"{name}={value}"])
        argv.append(self.image)
        argv.extend(command)
        return argv


@dataclass(frozen=True)
class DockerSandboxResult:
    returncode: int
    stdout: str
    stderr: str
    cleanup_verified: bool


class DockerSandboxRunner:
    def __init__(self, config: DockerSandboxConfig) -> None:
        self.config = config

    def _container_owner(self) -> str | None:
        try:
            inspection = subprocess.run(
                [
                    "docker",
                    "container",
                    "inspect",
                    "--format",
                    f'{{{{ index .Config.Labels "{_CLEANUP_LABEL}" }}}}',
                    self.config.name,
                ],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5,
                env=_docker_environment(),
            )
            if inspection.returncode == 0:
                return inspection.stdout.strip()
            listing = subprocess.run(
                [
                    "docker",
                    "container",
                    "ls",
                    "--all",
                    "--filter",
                    f"name=^/{self.config.name}$",
                    "--format",
                    "{{.Names}}",
                ],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5,
                env=_docker_environment(),
            )
        except subprocess.TimeoutExpired as exc:
            raise Stage0ValidationError(
                f"Workflow sandbox ownership check timed out: {self.config.name}"
            ) from exc
        if listing.returncode != 0:
            raise Stage0ValidationError(
                f"Workflow sandbox ownership could not be inspected: {self.config.name}"
            )
        if self.config.name in listing.stdout.splitlines():
            raise Stage0ValidationError(
                f"Workflow sandbox exists but its owner cannot be read: {self.config.name}"
            )
        return None

    def _assert_name_available(self) -> None:
        if self._container_owner() is not None:
            raise Stage0ValidationError(
                f"Workflow sandbox container already exists: {self.config.name}"
            )

    def _force_remove(self, *, settle_seconds: float = 0.5) -> None:
        deadline = time.monotonic() + settle_seconds
        owner = self._container_owner()
        while owner is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.05, remaining))
            owner = self._container_owner()
        if owner != self.config.cleanup_token:
            raise Stage0ValidationError(
                f"refusing to remove a container owned by another run: {self.config.name}"
            )
        try:
            removal = subprocess.run(
                ["docker", "rm", "--force", self.config.name],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5,
                env=_docker_environment(),
            )
        except subprocess.TimeoutExpired as exc:
            raise Stage0ValidationError(
                f"Workflow sandbox cleanup command timed out: {self.config.name}"
            ) from exc
        if removal.returncode != 0 or self._container_owner() is not None:
            raise Stage0ValidationError(
                f"Workflow sandbox cleanup could not be verified: {self.config.name}"
            )

    def run(
        self,
        command: Sequence[str],
        *,
        timeout_seconds: float,
    ) -> DockerSandboxResult:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise Stage0ValidationError("timeout_seconds must be positive")
        self._assert_name_available()
        try:
            process = run_bounded_process(
                self.config.argv(command),
                cwd=Path.cwd(),
                environment=_docker_environment(),
                limits=ProcessLimits(
                    timeout_seconds=timeout_seconds,
                    max_stdout_bytes=self.config.max_stdout_bytes,
                    max_stderr_bytes=self.config.max_stderr_bytes,
                    terminate_grace_seconds=0.25,
                ),
            )
        finally:
            self._force_remove()
        if process.timed_out:
            raise TimeoutError(
                f"Workflow sandbox exceeded deadline: {self.config.name}"
            )
        if process.output_limited or process.descendant_pipe_lingered:
            raise Stage0ValidationError(
                f"Workflow sandbox output or descendant pipe exceeded limits: {self.config.name}"
            )
        return DockerSandboxResult(
            returncode=process.returncode,
            stdout=process.stdout,
            stderr=process.stderr,
            cleanup_verified=True,
        )

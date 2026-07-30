"""Shared fail-closed Docker resource cleanup proof for experiment stages."""

from __future__ import annotations

from dataclasses import dataclass
import hmac
import math
import subprocess
import time
from typing import Callable

from .errors import Stage0ValidationError


DockerRunner = Callable[..., subprocess.CompletedProcess[str]]
CLEANUP_LABEL = "com.dyro.external-workflow-runner.cleanup-token"
PARTIAL_START_SETTLE_SECONDS = 1.0


@dataclass(frozen=True)
class DockerCleanupProof:
    containers_absent: bool
    network_absent: bool


def container_absent(
    *,
    run_docker: DockerRunner,
    name: str,
) -> bool:
    inspect = run_docker(
        [
            "docker",
            "inspect",
            "--type",
            "container",
            "--format",
            "{{.Id}}",
            name,
        ],
        timeout=5,
    )
    if inspect.returncode == 0:
        return False
    combined = f"{inspect.stdout}\n{inspect.stderr}".lower()
    if inspect.returncode == 1 and (
        "no such object" in combined or "no such container" in combined
    ):
        return True
    raise Stage0ValidationError(
        f"docker inspect failed while proving container absence: {combined[-500:]}"
    )


def network_absent(
    *,
    run_docker: DockerRunner,
    name: str,
) -> bool:
    inspect = run_docker(["docker", "network", "inspect", name], timeout=5)
    if inspect.returncode == 0:
        return False
    combined = f"{inspect.stdout}\n{inspect.stderr}".lower()
    if inspect.returncode == 1:
        exact_missing = f"network {name.lower()} not found"
        if "no such network" in combined or exact_missing in combined:
            return True
    raise Stage0ValidationError(
        f"docker inspect failed while proving network absence: {combined[-500:]}"
    )


def _parse_owned_resource(
    inspection: subprocess.CompletedProcess[str],
    *,
    name: str,
    resource_kind: str,
    missing_markers: tuple[str, ...],
) -> tuple[str, str] | None:
    if inspection.returncode == 0:
        resource_id, separator, owner_token = inspection.stdout.strip().partition("|")
        if (
            not separator
            or not resource_id
            or not owner_token
            or owner_token == "<no value>"
        ):
            raise Stage0ValidationError(
                f"Docker {resource_kind} exists but ownership is unreadable: {name}"
            )
        return resource_id, owner_token
    combined = f"{inspection.stdout}\n{inspection.stderr}".lower()
    if inspection.returncode == 1 and any(
        marker in combined for marker in missing_markers
    ):
        return None
    raise Stage0ValidationError(
        f"Docker {resource_kind} ownership inspect failed: {combined[-500:]}"
    )


def _container_identity(
    *,
    run_docker: DockerRunner,
    name: str,
) -> tuple[str, str] | None:
    inspection = run_docker(
        [
            "docker",
            "inspect",
            "--type",
            "container",
            "--format",
            f'{{{{.Id}}}}|{{{{ index .Config.Labels "{CLEANUP_LABEL}" }}}}',
            name,
        ],
        timeout=5,
    )
    return _parse_owned_resource(
        inspection,
        name=name,
        resource_kind="container",
        missing_markers=("no such object", "no such container"),
    )


def _network_identity(
    *,
    run_docker: DockerRunner,
    name: str,
) -> tuple[str, str] | None:
    inspection = run_docker(
        [
            "docker",
            "network",
            "inspect",
            "--format",
            f'{{{{.Id}}}}|{{{{ index .Labels "{CLEANUP_LABEL}" }}}}',
            name,
        ],
        timeout=5,
    )
    return _parse_owned_resource(
        inspection,
        name=name,
        resource_kind="network",
        missing_markers=("no such network", f"network {name.lower()} not found"),
    )


def _remove_and_verify_once(
    *,
    run_docker: DockerRunner,
    container_names: tuple[str, ...],
    network_name: str,
    owner_token: str,
) -> DockerCleanupProof:
    """Attempt every removal once, then prove every named resource is absent."""
    attempt_errors: list[str] = []
    owned_containers: list[tuple[str, str]] = []
    for name in container_names:
        try:
            identity = _container_identity(
                run_docker=run_docker,
                name=name,
            )
            if identity is None:
                continue
            resource_id, observed_owner = identity
            if not hmac.compare_digest(observed_owner, owner_token):
                attempt_errors.append(
                    f"refusing to remove container owned by another run: {name}"
                )
                continue
            owned_containers.append((name, resource_id))
        except Exception as exc:  # noqa: BLE001 - inspect every resource
            attempt_errors.append(f"inspect container ownership {name}: {exc}")

    owned_network: tuple[str, str] | None = None
    try:
        identity = _network_identity(
            run_docker=run_docker,
            name=network_name,
        )
        if identity is not None:
            resource_id, observed_owner = identity
            if not hmac.compare_digest(observed_owner, owner_token):
                attempt_errors.append(
                    "refusing to remove network owned by another run: "
                    f"{network_name}"
                )
            else:
                owned_network = (network_name, resource_id)
    except Exception as exc:  # noqa: BLE001 - continue to absence proof
        attempt_errors.append(
            f"inspect network ownership {network_name}: {exc}"
        )

    for name, resource_id in owned_containers:
        try:
            run_docker(["docker", "rm", "--force", resource_id], timeout=15)
        except Exception as exc:  # noqa: BLE001 - continue cleanup attempts
            attempt_errors.append(f"remove container {name}: {exc}")
    if owned_network is not None:
        try:
            run_docker(
                ["docker", "network", "rm", owned_network[1]],
                timeout=15,
            )
        except Exception as exc:  # noqa: BLE001 - continue to absence proof
            attempt_errors.append(f"remove network {network_name}: {exc}")

    proof_errors: list[str] = []
    container_results: list[bool] = []
    # Keep the name proofs to catch resources that materialize late, and add
    # exact-ID proofs so a resource renamed during cleanup cannot escape.
    for name in container_names:
        try:
            container_results.append(
                container_absent(run_docker=run_docker, name=name)
            )
        except Exception as exc:  # noqa: BLE001 - inspect all resources
            container_results.append(False)
            proof_errors.append(f"container {name}: {exc}")
    for name, resource_id in owned_containers:
        try:
            container_results.append(
                container_absent(
                    run_docker=run_docker,
                    name=resource_id,
                )
            )
        except Exception as exc:  # noqa: BLE001 - inspect all resources
            container_results.append(False)
            proof_errors.append(f"container {name} ({resource_id}): {exc}")
    try:
        network_name_result = network_absent(
            run_docker=run_docker,
            name=network_name,
        )
    except Exception as exc:  # noqa: BLE001 - aggregate proof failure
        network_name_result = False
        proof_errors.append(f"network {network_name}: {exc}")
    network_id_result = True
    if owned_network is not None:
        try:
            network_id_result = network_absent(
                run_docker=run_docker,
                name=owned_network[1],
            )
        except Exception as exc:  # noqa: BLE001 - aggregate proof failure
            network_id_result = False
            proof_errors.append(
                f"network {network_name} ({owned_network[1]}): {exc}"
            )

    containers_gone = all(container_results)
    network_result = network_name_result and network_id_result
    if not containers_gone:
        proof_errors.append("one or more Docker containers remain")
    if not network_result:
        proof_errors.append("Docker network remains")
    if proof_errors:
        details = "; ".join([*proof_errors, *attempt_errors])
        raise Stage0ValidationError(
            f"Docker cleanup could not be proven: {details[-2000:]}"
        )
    return DockerCleanupProof(
        containers_absent=True,
        network_absent=True,
    )


def remove_and_verify(
    *,
    run_docker: DockerRunner,
    container_names: tuple[str, ...],
    network_name: str,
    owner_token: str,
    settle_seconds: float = 0.0,
    retry_interval_seconds: float = 0.1,
) -> DockerCleanupProof:
    """Remove resources and, when requested, recheck through a settle window."""
    if (
        type(owner_token) is not str
        or len(owner_token) != 32
        or any(character not in "0123456789abcdef" for character in owner_token)
    ):
        raise Stage0ValidationError("Docker cleanup owner token is invalid")
    if (
        isinstance(settle_seconds, bool)
        or not isinstance(settle_seconds, (int, float))
        or not math.isfinite(settle_seconds)
        or settle_seconds < 0
    ):
        raise Stage0ValidationError(
            "cleanup settle_seconds must be finite and non-negative"
        )
    if (
        isinstance(retry_interval_seconds, bool)
        or not isinstance(retry_interval_seconds, (int, float))
        or not math.isfinite(retry_interval_seconds)
        or retry_interval_seconds <= 0
    ):
        raise Stage0ValidationError(
            "cleanup retry_interval_seconds must be finite and positive"
        )

    deadline = time.monotonic() + float(settle_seconds)
    while True:
        try:
            proof = _remove_and_verify_once(
                run_docker=run_docker,
                container_names=container_names,
                network_name=network_name,
                owner_token=owner_token,
            )
            cleanup_error: Stage0ValidationError | None = None
        except Stage0ValidationError as exc:
            proof = None
            cleanup_error = exc
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if cleanup_error is not None:
                raise cleanup_error
            if proof is None:  # pragma: no cover - narrowed above
                raise Stage0ValidationError("Docker cleanup proof is missing")
            return proof
        time.sleep(min(float(retry_interval_seconds), remaining))

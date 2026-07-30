"""Stage 5 Docker broker: host-mounted pinned provider + dual-cleanup proof."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import secrets
import subprocess
import time
from typing import NoReturn

from ..docker_cleanup import container_absent as prove_container_absent
from ..docker_cleanup import network_absent as prove_network_absent
from ..docker_cleanup import (
    CLEANUP_LABEL,
    PARTIAL_START_SETTLE_SECONDS,
    remove_and_verify,
)
from ..errors import Stage0ValidationError
from ..sandbox import BUN_IMAGE, BUN_USER, _docker_environment
from .host_provider import HostProviderPin


def _run_docker(
    argv: list[str], *, timeout: float = 30.0
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            env=_docker_environment(),
        )
    except subprocess.TimeoutExpired as exc:
        raise Stage0ValidationError(f"docker command timed out: {argv[:4]}") from exc


def _container_absent(name: str) -> bool:
    return prove_container_absent(run_docker=_run_docker, name=name)


def _network_absent(name: str) -> bool:
    return prove_network_absent(run_docker=_run_docker, name=name)


def _fail_start_with_cleanup(
    message: str,
    *,
    network_name: str,
    netns_name: str,
    broker_name: str,
    cleanup_token: str,
) -> NoReturn:
    try:
        remove_and_verify(
            run_docker=_run_docker,
            container_names=(broker_name, netns_name),
            network_name=network_name,
            owner_token=cleanup_token,
            settle_seconds=PARTIAL_START_SETTLE_SECONDS,
        )
    except Exception as cleanup_error:
        raise Stage0ValidationError(
            f"{message}; partial-start cleanup could not be proven: {cleanup_error}"
        ) from cleanup_error
    raise Stage0ValidationError(message)


def _run_start_step(
    argv: list[str],
    *,
    network_name: str,
    netns_name: str,
    broker_name: str,
    cleanup_token: str,
) -> subprocess.CompletedProcess[str]:
    try:
        return _run_docker(argv)
    except Exception as start_error:
        _fail_start_with_cleanup(
            f"Docker start step failed: {start_error}",
            network_name=network_name,
            netns_name=netns_name,
            broker_name=broker_name,
            cleanup_token=cleanup_token,
        )


@dataclass
class Stage5DockerBrokerStack:
    network_name: str
    netns_name: str
    broker_name: str
    port: int
    telemetry_host_path: Path
    bundle_root: Path
    provider_mode: str
    max_concurrency: int
    host_provider: HostProviderPin
    cleanup_token: str
    cleanup_verified: bool = False
    containers_absent: bool = False
    network_absent: bool = False

    @classmethod
    def start(
        cls,
        *,
        bundle_root: Path,
        telemetry_host_path: Path,
        model: str,
        host_provider: HostProviderPin,
        provider_mode: str = "argv-cli",
        max_concurrency: int = 2,
        port: int = 7421,
        provider_fake_token: str = "stage5-broker-only-token",
    ) -> Stage5DockerBrokerStack:
        if provider_mode not in {"fake", "simulated-cli", "argv-cli"}:
            raise Stage0ValidationError(f"unknown provider mode: {provider_mode}")
        if type(max_concurrency) is not int or not 1 <= max_concurrency <= 8:
            raise Stage0ValidationError("max_concurrency must be 1..8")

        host_path = host_provider.verify()

        token = secrets.token_hex(4)
        network_name = f"dyro-s5-net-{token}"
        netns_name = f"dyro-s5-ns-{token}"
        broker_name = f"dyro-s5-broker-{token}"
        cleanup_token = secrets.token_hex(16)
        telemetry_host_path = Path(telemetry_host_path)
        telemetry_host_path.parent.mkdir(parents=True, exist_ok=True)
        if telemetry_host_path.exists():
            telemetry_host_path.unlink()
        telemetry_host_path.write_text("", encoding="utf-8")
        os.chmod(telemetry_host_path, 0o666)

        create_net = _run_start_step(
            [
                "docker",
                "network",
                "create",
                "--label",
                f"{CLEANUP_LABEL}={cleanup_token}",
                "--internal",
                network_name,
            ],
            network_name=network_name,
            netns_name=netns_name,
            broker_name=broker_name,
            cleanup_token=cleanup_token,
        )
        if create_net.returncode != 0:
            _fail_start_with_cleanup(
                f"failed to create internal network: {create_net.stderr.strip()}",
                network_name=network_name,
                netns_name=netns_name,
                broker_name=broker_name,
                cleanup_token=cleanup_token,
            )

        create_ns = _run_start_step(
            [
                "docker",
                "run",
                "-d",
                "--rm",
                "--name",
                netns_name,
                "--label",
                f"{CLEANUP_LABEL}={cleanup_token}",
                "--network",
                network_name,
                "--user",
                BUN_USER,
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                BUN_IMAGE,
                "sleep",
                "600",
            ],
            network_name=network_name,
            netns_name=netns_name,
            broker_name=broker_name,
            cleanup_token=cleanup_token,
        )
        if create_ns.returncode != 0:
            _fail_start_with_cleanup(
                "failed to create broker network namespace: "
                f"{create_ns.stderr.strip()}",
                network_name=network_name,
                netns_name=netns_name,
                broker_name=broker_name,
                cleanup_token=cleanup_token,
            )

        # Host provider is mounted read-only into the Broker only — never the Sandbox.
        start_broker = _run_start_step(
            [
                "docker",
                "run",
                "-d",
                "--rm",
                "--name",
                broker_name,
                "--label",
                f"{CLEANUP_LABEL}={cleanup_token}",
                "--network",
                f"container:{netns_name}",
                "--user",
                BUN_USER,
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,nodev,size=32m",
                "--mount",
                f"type=bind,src={Path(bundle_root).resolve()},dst=/opt/workflow,readonly",
                "--mount",
                f"type=bind,src={host_path},dst={host_provider.container_path},readonly",
                "--mount",
                f"type=bind,src={telemetry_host_path.resolve()},dst=/run/dyro/broker-telemetry.jsonl",
                "--env",
                "DYRO_BROKER_HOST=127.0.0.1",
                "--env",
                f"DYRO_BROKER_PORT={port}",
                "--env",
                f"DYRO_BROKER_MODEL={model}",
                "--env",
                "DYRO_BROKER_TELEMETRY_PATH=/run/dyro/broker-telemetry.jsonl",
                "--env",
                f"DYRO_PROVIDER_MODE={provider_mode}",
                "--env",
                f"DYRO_BROKER_MAX_CONCURRENCY={max_concurrency}",
                "--env",
                "DYRO_PROVIDER_RAW_ROOT=/tmp/provider-raw",
                "--env",
                f"DYRO_PROVIDER_ARGV={host_provider.argv_csv()}",
                "--env",
                f"DYRO_PROVIDER_ARGV_SHA256={host_provider.content_sha256}",
                "--env",
                f"DYRO_PROVIDER_PIN_PATH={host_provider.container_path}",
                "--env",
                f"DYRO_PROVIDER_FAKE_TOKEN={provider_fake_token}",
                "--env",
                "HOME=/tmp",
                "--env",
                "TMPDIR=/tmp",
                "--env",
                "BUN_INSTALL_CACHE_DIR=/tmp/bun-cache",
                "--env",
                "XDG_CACHE_HOME=/tmp/xdg-cache",
                BUN_IMAGE,
                "bun",
                "/opt/workflow/broker_server.ts",
            ],
            network_name=network_name,
            netns_name=netns_name,
            broker_name=broker_name,
            cleanup_token=cleanup_token,
        )
        if start_broker.returncode != 0:
            _fail_start_with_cleanup(
                f"failed to start broker container: {start_broker.stderr.strip()}",
                network_name=network_name,
                netns_name=netns_name,
                broker_name=broker_name,
                cleanup_token=cleanup_token,
            )

        stack = cls(
            network_name=network_name,
            netns_name=netns_name,
            broker_name=broker_name,
            port=port,
            telemetry_host_path=telemetry_host_path,
            bundle_root=Path(bundle_root),
            provider_mode=provider_mode,
            max_concurrency=max_concurrency,
            host_provider=host_provider,
            cleanup_token=cleanup_token,
        )
        try:
            stack._wait_until_ready()
            return stack
        except Exception as readiness_error:
            try:
                stack.stop()
            except Exception as cleanup_error:
                raise Stage0ValidationError(
                    "broker readiness failed and cleanup could not be proven: "
                    f"{readiness_error}; cleanup: {cleanup_error}"
                ) from cleanup_error
            raise

    def _wait_until_ready(self, *, timeout_seconds: float = 20.0) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            logs = _run_docker(["docker", "logs", self.broker_name], timeout=5)
            combined = logs.stdout + logs.stderr
            if "broker-refuses-execution-key" in combined:
                raise Stage0ValidationError("broker refused execution key env")
            if "broker-pin-mismatch" in combined:
                raise Stage0ValidationError("broker rejected host provider integrity pin")
            if "broker-ready" in combined:
                return
            inspect = _run_docker(
                [
                    "docker",
                    "inspect",
                    "--format",
                    "{{.State.Running}}",
                    self.broker_name,
                ],
                timeout=5,
            )
            if inspect.stdout.strip() != "true":
                raise Stage0ValidationError(
                    f"broker container exited early: {logs.stderr or logs.stdout}"
                )
            time.sleep(0.1)
        raise Stage0ValidationError("broker container did not become ready")

    def stop(self) -> None:
        errors: list[str] = []
        try:
            logs = _run_docker(["docker", "logs", self.broker_name], timeout=5)
            combined = logs.stdout + logs.stderr
            if logs.returncode != 0:
                errors.append("final broker logs were unreadable")
        except Exception as exc:  # noqa: BLE001 - cleanup must still run
            combined = ""
            errors.append(f"final broker logs failed: {exc}")
        try:
            proof = remove_and_verify(
                run_docker=_run_docker,
                container_names=(self.broker_name, self.netns_name),
                network_name=self.network_name,
                owner_token=self.cleanup_token,
            )
            self.containers_absent = proof.containers_absent
            self.network_absent = proof.network_absent
        except Exception as exc:  # noqa: BLE001 - aggregate proof failures
            self.containers_absent = False
            self.network_absent = False
            errors.append(str(exc))
        if "broker-shutdown-error" in combined:
            errors.append(f"broker shutdown reported raw residue: {combined[-500:]}")
        self.cleanup_verified = (
            not errors
            and self.containers_absent
            and self.network_absent
        )
        if errors:
            raise Stage0ValidationError(
                f"broker cleanup verification failed: {'; '.join(errors)}"
            )

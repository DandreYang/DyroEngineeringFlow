"""Docker-isolated Agent Broker on an internal network (no external egress)."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import secrets
import subprocess
import time
from typing import NoReturn

from ..docker_cleanup import (
    CLEANUP_LABEL,
    PARTIAL_START_SETTLE_SECONDS,
    remove_and_verify,
)
from ..errors import Stage0ValidationError
from ..sandbox import BUN_IMAGE, BUN_USER, _docker_environment


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
class DockerBrokerStack:
    network_name: str
    netns_name: str
    broker_name: str
    port: int
    telemetry_host_path: Path
    bundle_root: Path
    cleanup_token: str

    @classmethod
    def start(
        cls,
        *,
        bundle_root: Path,
        telemetry_host_path: Path,
        model: str,
        port: int = 7421,
    ) -> DockerBrokerStack:
        token = secrets.token_hex(4)
        network_name = f"dyro-s1-net-{token}"
        netns_name = f"dyro-s1-ns-{token}"
        broker_name = f"dyro-s1-broker-{token}"
        cleanup_token = secrets.token_hex(16)
        telemetry_host_path = Path(telemetry_host_path)
        telemetry_host_path.parent.mkdir(parents=True, exist_ok=True)
        if telemetry_host_path.exists():
            telemetry_host_path.unlink()
        telemetry_host_path.write_text("", encoding="utf-8")
        # Container uid 1000 must be able to append telemetry.
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

        # Network namespace holder with no workflow code.
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
                "/tmp:rw,noexec,nosuid,nodev,size=16m",
                "--mount",
                f"type=bind,src={Path(bundle_root).resolve()},dst=/opt/workflow,readonly",
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
                    "broker readiness failed and cleanup could not be attempted: "
                    f"{readiness_error}; cleanup: {cleanup_error}"
                ) from cleanup_error
            raise

    def _wait_until_ready(self, *, timeout_seconds: float = 15.0) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            logs = _run_docker(
                ["docker", "logs", self.broker_name],
                timeout=5,
            )
            if "broker-ready" in (logs.stdout + logs.stderr):
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

    def sandbox_network_args(self) -> list[str]:
        # Share the broker network namespace: loopback TCP only, no external egress.
        return ["--network", f"container:{self.netns_name}"]

    def stop(self) -> None:
        remove_and_verify(
            run_docker=_run_docker,
            container_names=(self.broker_name, self.netns_name),
            network_name=self.network_name,
            owner_token=self.cleanup_token,
        )

"""Stage 3 Docker broker stack: argv-cli provider + internal network."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import secrets
import subprocess
import time

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


@dataclass
class Stage3DockerBrokerStack:
    network_name: str
    netns_name: str
    broker_name: str
    port: int
    telemetry_host_path: Path
    bundle_root: Path
    provider_mode: str
    max_concurrency: int

    @classmethod
    def start(
        cls,
        *,
        bundle_root: Path,
        telemetry_host_path: Path,
        model: str,
        provider_mode: str = "argv-cli",
        max_concurrency: int = 2,
        port: int = 7421,
        provider_fake_token: str = "stage3-broker-only-token",
        provider_argv: str = "bun,/opt/workflow/fake_provider_cli.ts",
    ) -> Stage3DockerBrokerStack:
        if provider_mode not in {"fake", "simulated-cli", "argv-cli"}:
            raise Stage0ValidationError(f"unknown provider mode: {provider_mode}")
        if type(max_concurrency) is not int or not 1 <= max_concurrency <= 8:
            raise Stage0ValidationError("max_concurrency must be 1..8")

        token = secrets.token_hex(4)
        network_name = f"dyro-s3-net-{token}"
        netns_name = f"dyro-s3-ns-{token}"
        broker_name = f"dyro-s3-broker-{token}"
        telemetry_host_path = Path(telemetry_host_path)
        telemetry_host_path.parent.mkdir(parents=True, exist_ok=True)
        if telemetry_host_path.exists():
            telemetry_host_path.unlink()
        telemetry_host_path.write_text("", encoding="utf-8")
        os.chmod(telemetry_host_path, 0o666)

        create_net = _run_docker(
            ["docker", "network", "create", "--internal", network_name]
        )
        if create_net.returncode != 0:
            raise Stage0ValidationError(
                f"failed to create internal network: {create_net.stderr.strip()}"
            )

        create_ns = _run_docker(
            [
                "docker",
                "run",
                "-d",
                "--rm",
                "--name",
                netns_name,
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
            ]
        )
        if create_ns.returncode != 0:
            _run_docker(["docker", "network", "rm", network_name], timeout=15)
            raise Stage0ValidationError(
                f"failed to create broker network namespace: {create_ns.stderr.strip()}"
            )

        start_broker = _run_docker(
            [
                "docker",
                "run",
                "-d",
                "--rm",
                "--name",
                broker_name,
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
                f"DYRO_PROVIDER_ARGV={provider_argv}",
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
            ]
        )
        if start_broker.returncode != 0:
            _run_docker(["docker", "rm", "--force", netns_name], timeout=15)
            _run_docker(["docker", "network", "rm", network_name], timeout=15)
            raise Stage0ValidationError(
                f"failed to start broker container: {start_broker.stderr.strip()}"
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
        )
        stack._wait_until_ready()
        return stack

    def _wait_until_ready(self, *, timeout_seconds: float = 20.0) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            logs = _run_docker(["docker", "logs", self.broker_name], timeout=5)
            if "broker-ready" in (logs.stdout + logs.stderr):
                if "broker-refuses-execution-key" in (logs.stdout + logs.stderr):
                    raise Stage0ValidationError("broker refused execution key env")
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
        logs = _run_docker(["docker", "logs", self.broker_name], timeout=5)
        combined = logs.stdout + logs.stderr
        for name in (self.broker_name, self.netns_name):
            _run_docker(["docker", "rm", "--force", name], timeout=15)
        _run_docker(["docker", "network", "rm", self.network_name], timeout=15)
        if "broker-shutdown-error" in combined:
            raise Stage0ValidationError(
                f"broker shutdown reported raw residue: {combined[-500:]}"
            )

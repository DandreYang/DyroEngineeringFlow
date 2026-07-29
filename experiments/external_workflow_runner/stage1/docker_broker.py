"""Docker-isolated Agent Broker on an internal network (no external egress)."""

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
class DockerBrokerStack:
    network_name: str
    netns_name: str
    broker_name: str
    port: int
    telemetry_host_path: Path
    bundle_root: Path

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
        telemetry_host_path = Path(telemetry_host_path)
        telemetry_host_path.parent.mkdir(parents=True, exist_ok=True)
        if telemetry_host_path.exists():
            telemetry_host_path.unlink()
        telemetry_host_path.write_text("", encoding="utf-8")
        # Container uid 1000 must be able to append telemetry.
        os.chmod(telemetry_host_path, 0o666)

        create_net = _run_docker(
            ["docker", "network", "create", "--internal", network_name]
        )
        if create_net.returncode != 0:
            raise Stage0ValidationError(
                f"failed to create internal network: {create_net.stderr.strip()}"
            )

        # Network namespace holder with no workflow code.
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
        )
        stack._wait_until_ready()
        return stack

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
        for name in (self.broker_name, self.netns_name):
            _run_docker(["docker", "rm", "--force", name], timeout=15)
        _run_docker(["docker", "network", "rm", self.network_name], timeout=15)

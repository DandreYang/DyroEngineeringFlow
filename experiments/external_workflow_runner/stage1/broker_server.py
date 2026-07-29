"""Isolated Agent Broker process: Unix-socket IPC and fake provider."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import socketserver
import threading
import time
from types import MappingProxyType
from typing import Callable, Mapping

from ..broker import BrokerLimiter
from ..errors import Stage0ValidationError
from .protocol import (
    AgentCallRequest,
    AgentCallResponse,
    dumps_strict,
    loads_strict,
    sanitize_text,
)


FakeProvider = Callable[[AgentCallRequest], AgentCallResponse]


def default_fake_provider(request: AgentCallRequest) -> AgentCallResponse:
    """Deterministic provider with no credentials and no network."""
    summary = sanitize_text(
        f"fake-provider:{request.model}:{request.prompt.strip()[:120]}",
        max_chars=512,
    )
    return AgentCallResponse(
        call_id=request.call_id,
        status="ok",
        text=summary,
        error_code="",
    )


@dataclass
class BrokerTelemetry:
    path: Path
    max_bytes: int = 256 * 1024
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    bytes_written: int = 0
    truncated: bool = False

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self.path.unlink()
        self.path.touch()
        os.chmod(self.path, 0o600)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass

    def append(self, event: Mapping[str, object]) -> None:
        line = dumps_strict(dict(event)) + "\n"
        encoded = line.encode("utf-8")
        with self._lock:
            if self.bytes_written + len(encoded) > self.max_bytes:
                self.truncated = True
                return
            with self.path.open("ab") as handle:
                handle.write(encoded)
            self.bytes_written += len(encoded)


class _BrokerRequestHandler(socketserver.StreamRequestHandler):
    server: "BrokerUnixServer"

    def handle(self) -> None:
        raw = self.rfile.readline(256 * 1024)
        if not raw:
            return
        if len(raw) >= 256 * 1024:
            self._write_error("request_too_large", "IPC request line is too large")
            return
        try:
            payload = loads_strict(raw.decode("utf-8", errors="strict").strip())
            request = AgentCallRequest.from_mapping(payload)
        except (UnicodeDecodeError, Stage0ValidationError) as exc:
            self._write_error("invalid_request", sanitize_text(str(exc), max_chars=200))
            return
        started = time.monotonic()
        try:
            response = self.server.dispatch(request)
        except TimeoutError:
            response = AgentCallResponse(
                call_id=request.call_id,
                status="timeout",
                text="",
                error_code="deadline_exceeded",
            )
        except Exception as exc:  # noqa: BLE001 - fail-closed broker boundary
            response = AgentCallResponse(
                call_id=request.call_id,
                status="error",
                text="",
                error_code=sanitize_text(type(exc).__name__, max_chars=64),
            )
        duration_ms = int((time.monotonic() - started) * 1000)
        self.server.telemetry.append(
            {
                "schema_version": 1,
                "kind": "agent_call",
                "call_id": request.call_id,
                "model": request.model,
                "status": response.status,
                "error_code": response.error_code,
                "duration_ms": duration_ms,
                "prompt_chars": len(request.prompt),
                "response_chars": len(response.text),
                "sanitizer": "stage1-v1",
            }
        )
        self.wfile.write((dumps_strict(response.to_mapping()) + "\n").encode("utf-8"))

    def _write_error(self, code: str, message: str) -> None:
        payload = {
            "protocol_version": 1,
            "type": "agent.result",
            "call_id": "invalid",
            "status": "error",
            "text": "",
            "error_code": code,
            "detail": message,
        }
        # Keep response on the narrow schema without detail when possible.
        safe = {
            "protocol_version": 1,
            "type": "agent.result",
            "call_id": "invalid",
            "status": "error",
            "text": "",
            "error_code": code,
        }
        del payload
        self.wfile.write((dumps_strict(safe) + "\n").encode("utf-8"))


class BrokerUnixServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        socket_path: Path,
        *,
        provider: FakeProvider,
        limiter: BrokerLimiter,
        telemetry: BrokerTelemetry,
        allowed_models: Mapping[str, bool],
    ) -> None:
        self.socket_path = Path(socket_path)
        if self.socket_path.exists() or self.socket_path.is_symlink():
            self.socket_path.unlink()
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self.provider = provider
        self.limiter = limiter
        self.telemetry = telemetry
        self.allowed_models = MappingProxyType(dict(allowed_models))
        super().__init__(os.fspath(self.socket_path), _BrokerRequestHandler)
        # Docker Stage1 runs as container uid 1000; allow socket connect without
        # widening network exposure (Unix socket only, still mode-restricted dir).
        os.chmod(self.socket_path, 0o666)

    def dispatch(self, request: AgentCallRequest) -> AgentCallResponse:
        if request.model not in self.allowed_models:
            return AgentCallResponse(
                call_id=request.call_id,
                status="error",
                text="",
                error_code="model_not_allowed",
            )
        timeout_seconds = max(request.deadline_ms / 1000.0, 0.001)

        async def _run() -> AgentCallResponse:
            return self.provider(request)

        import asyncio

        return asyncio.run(
            self.limiter.call(
                request.call_id,
                lambda _call_id: _run(),
                timeout_seconds=timeout_seconds,
            )
        )


@dataclass
class BrokerProcess:
    socket_path: Path
    telemetry_path: Path
    server: BrokerUnixServer
    thread: threading.Thread

    @classmethod
    def start(
        cls,
        ipc_root: Path,
        *,
        max_concurrency: int = 2,
        default_timeout_seconds: float = 5.0,
        allowed_models: Mapping[str, bool] | None = None,
        provider: FakeProvider | None = None,
        socket_path: Path | None = None,
    ) -> BrokerProcess:
        ipc_root = Path(ipc_root)
        ipc_root.mkdir(parents=True, exist_ok=True)
        # Directory must be traversable by the container non-root user.
        os.chmod(ipc_root, 0o755)
        # AF_UNIX paths are capped (~104 bytes on macOS). Callers with deep
        # project worktrees should pass an explicit short socket_path.
        resolved_socket = (
            Path(socket_path) if socket_path is not None else ipc_root / "s.sock"
        )
        if len(os.fspath(resolved_socket)) > 100:
            raise Stage0ValidationError(
                "broker socket path exceeds AF_UNIX length limit; "
                "pass a shorter socket_path"
            )
        telemetry_path = ipc_root / "broker-telemetry.jsonl"
        telemetry = BrokerTelemetry(telemetry_path)
        limiter = BrokerLimiter(
            max_concurrency=max_concurrency,
            default_timeout_seconds=default_timeout_seconds,
        )
        server = BrokerUnixServer(
            resolved_socket,
            provider=provider or default_fake_provider,
            limiter=limiter,
            telemetry=telemetry,
            allowed_models=allowed_models or {"fake-model": True},
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return cls(
            socket_path=resolved_socket,
            telemetry_path=telemetry_path,
            server=server,
            thread=thread,
        )

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        if self.socket_path.exists() or self.socket_path.is_symlink():
            self.socket_path.unlink()
        self.thread.join(timeout=2.0)
        if self.thread.is_alive():
            raise Stage0ValidationError("Agent Broker thread did not stop")

    @property
    def max_observed_concurrency(self) -> int:
        return self.server.limiter.max_observed_concurrency

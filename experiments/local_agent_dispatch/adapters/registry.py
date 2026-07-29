"""Backend registry and probing."""

from __future__ import annotations

from .base import BackendAdapter
from .echo import EchoAdapter
from .subprocess_cli import claude_adapter, codex_adapter


def _all() -> dict[str, BackendAdapter]:
    adapters: list[BackendAdapter] = [
        EchoAdapter(),
        codex_adapter(),
        claude_adapter(),
    ]
    return {a.id: a for a in adapters}


def list_adapters() -> list[str]:
    return sorted(_all().keys())


def get_adapter(backend_id: str) -> BackendAdapter:
    adapters = _all()
    if backend_id == "auto":
        for preferred in ("codex", "claude", "echo"):
            adapter = adapters.get(preferred)
            if adapter is not None and adapter.available():
                return adapter
        return EchoAdapter()
    adapter = adapters.get(backend_id)
    if adapter is None:
        from ..errors import DispatchValidationError

        raise DispatchValidationError(f"unknown backend: {backend_id}")
    return adapter


def probe_backends() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for adapter_id, adapter in sorted(_all().items()):
        rows.append(
            {
                "id": adapter_id,
                "command": adapter.command,
                "available": adapter.available(),
            }
        )
    return rows

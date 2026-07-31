"""Backend registry and probing."""

from __future__ import annotations

import os
import shutil

from ..errors import DispatchValidationError
from .base import BackendAdapter
from .echo import EchoAdapter
from .subprocess_cli import claude_adapter, codex_adapter


REAL_PROVIDER_IDS = ("codex", "claude")
DISCOVER_ONLY_PROVIDERS = {
    "cursor-agent": "cursor-agent",
    "opencode": "opencode",
    "grok": "grok",
    "hermes": "hermes",
    "kimi": "kimi",
}


def _all() -> dict[str, BackendAdapter]:
    adapters: list[BackendAdapter] = [
        EchoAdapter(),
        codex_adapter(),
        claude_adapter(),
    ]
    return {a.id: a for a in adapters}


def list_adapters() -> list[str]:
    return sorted(_all().keys())


def list_real_provider_ids() -> tuple[str, ...]:
    return REAL_PROVIDER_IDS


def adapter_is_authenticated(adapter: BackendAdapter) -> bool:
    """Apply one platform capability policy to selection and reporting."""
    if os.name != "posix":
        return adapter.id == "echo"
    authenticated = getattr(adapter, "authenticated", None)
    if callable(authenticated):
        return bool(authenticated())
    # Preserve the original adapter-test/extension compatibility contract:
    # adapters predating the explicit auth probe use availability as the probe.
    return adapter.available()


def get_adapter(
    backend_id: str,
    *,
    require_strict: bool = False,
) -> BackendAdapter:
    adapters = _all()
    if backend_id == "auto":
        for preferred in REAL_PROVIDER_IDS:
            adapter = adapters.get(preferred)
            if (
                adapter is not None
                and adapter.available()
                and adapter_is_authenticated(adapter)
                and (not require_strict or adapter.strict_isolation)
            ):
                return adapter
        raise DispatchValidationError(
            "no available authenticated backend satisfies the isolation policy"
        )
    adapter = adapters.get(backend_id)
    if adapter is None:
        raise DispatchValidationError(f"unknown backend: {backend_id}")
    if require_strict and not adapter.strict_isolation:
        raise DispatchValidationError(
            f"backend does not provide strict isolation: {backend_id}"
        )
    return adapter


def probe_backends() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for adapter_id, adapter in sorted(_all().items()):
        available = adapter.available()
        rows.append(
            {
                "id": adapter_id,
                "command": adapter.command,
                "available": available,
                "authenticated": adapter_is_authenticated(adapter),
                "strict_isolation": adapter.strict_isolation,
                "supported": adapter.id != "echo",
                "execution_kind": (
                    "offline-simulation" if adapter.id == "echo" else "provider"
                ),
            }
        )
    for provider_id, command in sorted(DISCOVER_ONLY_PROVIDERS.items()):
        rows.append(
            {
                "id": provider_id,
                "command": command,
                "available": shutil.which(command) is not None,
                "authenticated": False,
                "strict_isolation": False,
                "supported": False,
                "execution_kind": "unintegrated",
                "reason": "command discovery only; no audited non-interactive adapter",
            }
        )
    return rows

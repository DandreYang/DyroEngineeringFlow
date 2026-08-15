"""Backend registry and probing."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
from typing import Mapping

from dyro.canonical import canonical_json_bytes
from ..errors import DispatchValidationError
from ..context_guard import assert_content_allowed
from .base import BackendAdapter
from .echo import EchoAdapter
from .subprocess_cli import (
    claude_adapter,
    codex_adapter,
    cursor_adapter,
    dsh_adapter,
    grok_adapter,
    hermes_adapter,
    kimi_adapter,
    opencode_adapter,
    pi_adapter,
)


REAL_PROVIDER_IDS = (
    "codex",
    "claude",
    "cursor-agent",
    "opencode",
    "grok",
    "hermes",
    "kimi",
    "dsh",
    "pi",
)


def _all() -> dict[str, BackendAdapter]:
    adapters: list[BackendAdapter] = [
        EchoAdapter(),
        codex_adapter(),
        claude_adapter(),
        cursor_adapter(),
        opencode_adapter(),
        grok_adapter(),
        hermes_adapter(),
        kimi_adapter(),
        dsh_adapter(),
        pi_adapter(),
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
        try:
            return bool(authenticated())
        except DispatchValidationError:
            return False
    # Preserve the original adapter-test/extension compatibility contract:
    # adapters predating the explicit auth probe use availability as the probe.
    return adapter.available()


def adapter_execution_profile(adapter: BackendAdapter) -> dict[str, str]:
    """Return a bounded, non-secret identity for the selected execution route."""
    provider = getattr(adapter, "execution_profile", None)
    raw: Mapping[str, str]
    if callable(provider):
        raw = provider()
    else:
        command = getattr(adapter, "command", "")
        resolved = shutil.which(command) if command else None
        raw = {
            "backend": str(getattr(adapter, "id", "")),
            "command_path": str(Path(resolved).resolve()) if resolved else command,
        }
    return normalize_execution_profile(
        raw,
        backend=str(getattr(adapter, "id", "")),
    )


def normalize_execution_profile(
    raw: Mapping[str, str],
    *,
    backend: str,
) -> dict[str, str]:
    if not isinstance(raw, Mapping) or not raw:
        raise DispatchValidationError("backend execution profile must be an object")
    normalized: dict[str, str] = {}
    for name, value in raw.items():
        if (
            type(name) is not str
            or not name
            or len(name) > 64
            or type(value) is not str
            or len(value) > 4096
        ):
            raise DispatchValidationError("backend execution profile is invalid")
        normalized[name] = value
        assert_content_allowed(value, label=f"backend execution profile {name}")
    if normalized.get("backend") != backend:
        raise DispatchValidationError("backend execution profile identity mismatch")
    return dict(sorted(normalized.items()))


def adapter_execution_profile_sha256(adapter: BackendAdapter) -> str:
    return execution_profile_sha256(adapter_execution_profile(adapter))


def execution_profile_sha256(profile: Mapping[str, str]) -> str:
    return hashlib.sha256(canonical_json_bytes(dict(profile))).hexdigest()


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


def probe_backends(*, passive: bool = False) -> list[dict[str, object]]:
    """Describe adapters; passive mode never starts third-party auth CLIs."""
    rows: list[dict[str, object]] = []
    for adapter_id, adapter in sorted(_all().items()):
        available = adapter.available()
        authenticated = False if passive else adapter_is_authenticated(adapter)
        row: dict[str, object] = {
            "id": adapter_id,
            "command": adapter.command,
            "available": available,
            "authenticated": authenticated,
            "strict_isolation": adapter.strict_isolation,
            "supported": adapter.id != "echo",
            "execution_kind": (
                "offline-simulation" if adapter.id == "echo" else "provider"
            ),
            "authentication_probe": "not_run" if passive else "completed",
        }
        if passive and available and adapter.id != "echo":
            row["reason"] = "authentication probe not run in dry-run mode"
        elif available and adapter.id != "echo" and not authenticated:
            reason = getattr(adapter, "readiness_reason", None)
            row["reason"] = (
                reason() if callable(reason) else "authentication probe failed"
            )
        rows.append(row)
    return rows

"""Parallel multi-backend panel runs with a simple board artifact."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import secrets
import time
from typing import Mapping, Sequence

from .adapters.registry import (
    REAL_PROVIDER_IDS,
    adapter_execution_profile,
    adapter_is_authenticated,
    get_adapter,
    probe_backends,
)
from .context_guard import safe_error_text
from .errors import DispatchValidationError
from .json_store import atomic_write_json
from .paths import panels_dir
from .supervisor import DispatchSupervisor
from .task_contract import parse_task_contract


MAX_PANEL_MEMBERS = len(REAL_PROVIDER_IDS)
MAX_PANEL_PARALLEL = 4
_DEFAULT_PROVIDER_ORDER = (
    "codex",
    "claude",
    "grok",
    "opencode",
    "hermes",
    "pi",
    "kimi",
    "dsh",
    "cursor-agent",
)


def ready_provider_ids() -> list[str]:
    """Return ready real Providers in the stable dispatch preference order."""
    ready = {
        str(row["id"])
        for row in probe_backends()
        if (
            row["available"]
            and row["authenticated"]
            and row.get("supported")
            and row.get("execution_kind") == "provider"
        )
    }
    return [backend for backend in _DEFAULT_PROVIDER_ORDER if backend in ready]


def candidate_provider_ids() -> list[str]:
    """Return installed Providers without starting their authentication CLIs."""
    candidates: list[str] = []
    for backend in _DEFAULT_PROVIDER_ORDER:
        adapter = get_adapter(backend)
        if not adapter.available():
            continue
        try:
            adapter_execution_profile(adapter)
        except DispatchValidationError:
            continue
        candidates.append(backend)
    return candidates


def resolve_panel_members(requested: Sequence[str] | None) -> list[str]:
    if requested:
        requested = [name.strip() for name in requested if name.strip()]
        if "all" in requested:
            if requested != ["all"]:
                raise DispatchValidationError(
                    "panel member 'all' cannot be combined with backend IDs"
                )
            preferred = ready_provider_ids()
            if not preferred:
                raise DispatchValidationError(
                    "no authenticated integrated provider is available"
                )
            return preferred
        members: list[str] = []
        for name in requested:
            adapter = get_adapter(name)
            if (
                adapter.available()
                and adapter_is_authenticated(adapter)
                and adapter.id not in members
            ):
                members.append(adapter.id)
        if not members:
            raise DispatchValidationError("no panel members available")
        if len(members) > MAX_PANEL_MEMBERS:
            raise DispatchValidationError(
                f"panel exceeds the {MAX_PANEL_MEMBERS}-member limit"
            )
        return members
    # Default panels must contain only ready, integrated Providers.  An
    # explicitly requested echo member remains possible for deterministic test
    # simulations, but must be separately acknowledged by the task contract.
    preferred = ready_provider_ids()
    if not preferred:
        raise DispatchValidationError(
            "no authenticated integrated provider is available; "
            "configure a Provider route or explicitly request an acknowledged "
            "offline simulation"
        )
    return preferred[:3]


def run_panel(
    payload: Mapping[str, object],
    *,
    project_root: Path,
    members: Sequence[str] | None = None,
    home: Path | None = None,
    timeout_seconds: float = 120.0,
) -> dict[str, object]:
    """
    Fan out the same task concurrently under the Supervisor's dual slot limits.
    Writes a panel board JSON under dispatch home.
    """
    base = dict(payload)
    # Panel members must not inherit a single forced backend.
    backends = resolve_panel_members(members)
    panel_id = f"panel-{secrets.token_hex(6)}"
    supervisor = DispatchSupervisor(home=home)
    runs: list[dict[str, object]] = []

    def dispatch_member(backend: str) -> dict[str, object]:
        run_id = ""
        try:
            contract_payload = dict(base)
            contract_payload["backend"] = backend
            parse_task_contract(contract_payload)
            record = supervisor.accept(
                contract_payload, project_root=project_root, panel_id=panel_id
            )
            run_id = record.run_id
            finished = supervisor.execute(
                record.run_id, timeout_seconds=timeout_seconds, sync=True
            )
            result = finished.result or {}
            return {
                "backend": backend,
                "run_id": finished.run_id,
                "status": finished.status,
                "summary": result.get("summary"),
                "confidence": result.get("confidence"),
                "verified_ratio": result.get("verified_ratio"),
                "evidence": result.get("evidence"),
                "error": finished.error,
            }
        except Exception as exc:  # noqa: BLE001 - isolate panel members
            error = safe_error_text(
                exc, fallback="panel member execution failed"
            )
            persisted_status = "failed"
            if run_id:
                try:
                    persisted = supervisor.store.fail_if_accepted(
                        run_id,
                        error=error,
                    )
                    persisted_status = persisted.status
                except Exception:  # noqa: BLE001 - preserve the board
                    persisted_status = "unknown"
            return {
                "backend": backend,
                "run_id": run_id,
                "status": persisted_status,
                "summary": None,
                "confidence": None,
                "verified_ratio": None,
                "evidence": None,
                "error": error,
            }

    with ThreadPoolExecutor(
        max_workers=min(len(backends), MAX_PANEL_PARALLEL),
        thread_name_prefix="dyro-panel",
    ) as executor:
        runs.extend(executor.map(dispatch_member, backends))

    board = {
        "schema_version": 1,
        "kind": "local-agent-dispatch-panel",
        "panel_id": panel_id,
        "created_at": time.time(),
        "project_root": str(Path(project_root).resolve()),
        "members": backends,
        "runs": runs,
        "arbitration": {
            "status": "pending_human_or_host",
            "notes": [
                "Host agent must synthesize consensus vs disagreement.",
                "Do not treat majority vote as a Dyro gate.",
            ],
        },
    }
    path = panels_dir(home) / f"{panel_id}.json"
    atomic_write_json(path, board)
    board["board_path"] = str(path)
    return board

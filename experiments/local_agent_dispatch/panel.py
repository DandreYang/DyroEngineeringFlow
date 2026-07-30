"""Parallel multi-backend panel runs with a simple board artifact."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import secrets
import time
from typing import Mapping, Sequence

from .adapters.registry import (
    adapter_is_authenticated,
    get_adapter,
    probe_backends,
)
from .errors import DispatchValidationError
from .json_store import atomic_write_json
from .paths import panels_dir
from .supervisor import DispatchSupervisor
from .task_contract import parse_task_contract


def resolve_panel_members(requested: Sequence[str] | None) -> list[str]:
    if requested:
        members = []
        for name in requested:
            adapter = get_adapter(name)
            if adapter.available() and adapter_is_authenticated(adapter):
                members.append(adapter.id)
        if not members:
            raise DispatchValidationError("no panel members available")
        return members
    # Default: available real backends, always allow echo as last resort
    available = [
        row["id"]
        for row in probe_backends()
        if row["available"] and row["authenticated"]
    ]
    preferred = [b for b in ("codex", "claude", "echo") if b in available]
    if not preferred:
        preferred = ["echo"]
    # panel wants diversity: at least echo if only one real
    if len(preferred) == 1 and preferred[0] != "echo":
        preferred.append("echo")
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
        contract_payload = dict(base)
        contract_payload["backend"] = backend
        parse_task_contract(contract_payload)
        record = supervisor.accept(
            contract_payload, project_root=project_root, panel_id=panel_id
        )
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

    with ThreadPoolExecutor(
        max_workers=min(len(backends), 4),
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

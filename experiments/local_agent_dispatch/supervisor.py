"""Dispatch supervisor: accept runs, execute workers, never merge/push."""

from __future__ import annotations

from pathlib import Path
import time
from typing import Mapping

from .adapters.registry import get_adapter
from .context_guard import materialize_strict_shadow
from .errors import DispatchValidationError
from .fileset import collect_guarded_files
from .lease import SlotManager
from .paths import patches_dir, shadow_dir
from .result_envelope import build_result
from .run_store import RunRecord, RunStore
from .task_contract import parse_task_contract


FORBIDDEN = frozenset({"merge", "push", "signoff", "import_evidence"})


def refuse_production_actions(flags: Mapping[str, object] | None) -> None:
    if not flags:
        return
    for key in FORBIDDEN:
        if flags.get(key):
            raise DispatchValidationError(
                f"local agent dispatch forbids production action: {key}"
            )


class DispatchSupervisor:
    def __init__(
        self,
        *,
        home: Path | None = None,
        max_per_backend: int = 2,
        max_global: int = 4,
    ) -> None:
        self.home = home
        self.store = RunStore(home)
        self.slots = SlotManager(
            home, max_per_backend=max_per_backend, max_global=max_global
        )

    def accept(
        self,
        payload: Mapping[str, object],
        *,
        project_root: Path,
        panel_id: str = "",
    ) -> RunRecord:
        refuse_production_actions(
            payload.get("production_actions")  # type: ignore[arg-type]
            if isinstance(payload.get("production_actions"), dict)
            else None
        )
        contract = parse_task_contract(payload)
        # Fail-closed: expand + guard before accepting.
        collect_guarded_files(contract.files, project_root)
        adapter = get_adapter(contract.backend)
        backend = adapter.id
        if not adapter.available() and backend != "echo":
            raise DispatchValidationError(
                f"backend not available: {contract.backend} (resolved={backend})"
            )
        return self.store.create(
            contract=contract,
            project_root=project_root,
            backend=backend,
            panel_id=panel_id,
        )

    def execute(
        self,
        run_id: str,
        *,
        timeout_seconds: float = 120.0,
        sync: bool = True,
    ) -> RunRecord:
        if not sync:
            # Async: spawn is left to CLI; API still supports sync default.
            raise DispatchValidationError(
                "async execute is only available via CLI worker spawn; use sync=True"
            )
        record = self.store.load(run_id)
        if record.status not in {"accepted", "running"}:
            return record

        leases = self.slots.acquire(record.backend)
        lease_paths = [str(lease.slot_dir) for lease in leases]
        try:
            self.store.update_status(
                run_id, "running", lease_slots=lease_paths
            )
            return self._run_worker(run_id, timeout_seconds=timeout_seconds)
        finally:
            self.slots.release_all(leases)

    def _run_worker(self, run_id: str, *, timeout_seconds: float) -> RunRecord:
        record = self.store.load(run_id)
        contract = parse_task_contract(record.contract)
        project_root = Path(record.project_root)
        started = time.time()

        try:
            files = collect_guarded_files(contract.files, project_root)
            context: dict[str, str] = {}
            for path in files:
                rel = path.resolve().relative_to(project_root.resolve()).as_posix()
                context[rel] = path.read_text(encoding="utf-8")

            work_cwd = project_root
            shadow_path = ""
            if contract.strict:
                shadow_root = shadow_dir(self.home) / run_id
                if shadow_root.exists():
                    import shutil

                    shutil.rmtree(shadow_root)
                materialize_strict_shadow(
                    shadow_root=shadow_root,
                    root_dir=project_root,
                    relative_files=context,
                )
                work_cwd = shadow_root
                shadow_path = str(shadow_root)

            if contract.mode == "edit":
                # Isolation: only allow patch path under dispatch home, never auto-apply.
                patch_dir = patches_dir(self.home) / run_id
                patch_dir.mkdir(parents=True, exist_ok=True)

            adapter = get_adapter(record.backend)
            adapter_result = adapter.run(
                contract=contract,
                cwd=work_cwd,
                context_files=context,
                timeout_seconds=timeout_seconds,
            )

            # Locator verification uses project_root for non-strict; shadow for strict.
            verify_cwd = work_cwd if contract.strict else project_root
            envelope = build_result(
                run_id=run_id,
                status=adapter_result.status if adapter_result.status != "ok" else "ok",
                summary=adapter_result.summary,
                cwd=verify_cwd,
                evidence=adapter_result.evidence,
                confidence=adapter_result.confidence,
                patch_ref=adapter_result.patch_ref,
                takeover=adapter_result.takeover,
                usage={
                    **adapter_result.usage,
                    "duration_ms": int((time.time() - started) * 1000),
                },
                warnings=adapter_result.warnings,
                backend=record.backend,
                error_code=adapter_result.error_code,
            )
            if adapter_result.status == "timeout":
                status = "timeout"
            elif adapter_result.status == "ok":
                status = "completed"
            else:
                status = "failed"

            return self.store.update_status(
                run_id,
                status,
                result=envelope.to_mapping(),
                error=adapter_result.error_code,
                shadow_path=shadow_path,
            )
        except Exception as exc:  # noqa: BLE001 - persist failure onto run
            return self.store.update_status(
                run_id,
                "failed",
                error=str(exc),
                result=build_result(
                    run_id=run_id,
                    status="error",
                    summary="",
                    cwd=project_root,
                    evidence=[],
                    backend=record.backend,
                    error_code="worker_exception",
                    warnings=[str(exc)],
                ).to_mapping(),
            )

    def result(self, run_id: str) -> RunRecord:
        return self.store.load(run_id)

    def wait(
        self,
        run_ids: list[str],
        *,
        timeout_seconds: float = 300.0,
        poll_seconds: float = 0.2,
    ) -> list[RunRecord]:
        deadline = time.time() + timeout_seconds
        terminal = {"completed", "failed", "timeout", "cancelled"}
        while time.time() < deadline:
            records = [self.store.load(rid) for rid in run_ids]
            if all(r.status in terminal for r in records):
                return records
            time.sleep(poll_seconds)
        return [self.store.load(rid) for rid in run_ids]

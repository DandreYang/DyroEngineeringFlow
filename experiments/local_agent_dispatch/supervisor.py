"""Dispatch supervisor: accept runs, execute workers, never merge/push."""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
import secrets
import signal
import shutil
import subprocess
import sys
import threading
import time
from typing import Callable, Mapping

from .adapters.registry import (
    adapter_execution_profile,
    adapter_execution_profile_sha256,
    adapter_is_authenticated,
    execution_profile_sha256,
    get_adapter,
    list_real_provider_ids,
)
from .context_guard import materialize_strict_shadow, safe_error_text
from .edit_workspace import EditWorkspace
from .edit_workspace import review_edit_snapshot
from .errors import DispatchValidationError
from .fileset import collect_guarded_context, guarded_context_sha256
from .lease import LeaseHeartbeat, SlotManager
from .paths import dispatch_home, runs_dir, shadow_dir
from .process_identity import current_identity
from .result_envelope import build_result
from .run_store import RunRecord, RunStore
from .skill_render import selected_route
from .task_contract import parse_task_contract


FORBIDDEN = frozenset({"merge", "push", "signoff", "import_evidence"})
_ASYNC_WORKERS: set[subprocess.Popen[bytes]] = set()
_ASYNC_WORKERS_LOCK = threading.Lock()
ASYNC_STARTUP_TIMEOUT_SECONDS = 5.0
_WORKER_COMMON_ENV = frozenset(
    {
        "PATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "TMPDIR",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
    }
)
def _require_posix_supervision() -> None:
    if os.name != "posix":
        raise DispatchValidationError(
            "local dispatch run/panel/worker supervision requires a POSIX "
            "host (Linux or macOS)"
        )


@dataclass(frozen=True)
class _WorkerOutcome:
    status: str
    result: dict[str, object]
    error: str = ""
    shadow_path: str = ""


class _CooperativeCancellation(Exception):
    """Internal control flow after the exact worker observes cancellation."""


def _validate_timeout(timeout_seconds: float) -> None:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(float(timeout_seconds))
        or timeout_seconds <= 0
    ):
        raise DispatchValidationError("timeout_seconds must be finite and positive")


def _worker_environment(
    *,
    backend: str,
    home: Path,
    run_id: str,
    expected_execution_profile_sha256: str,
    expected_execution_profile: Mapping[str, str],
) -> dict[str, str]:
    if backend in list_real_provider_ids():
        adapter = get_adapter(backend)
        current_profile = adapter_execution_profile(adapter)
        if (
            not expected_execution_profile_sha256
            or current_profile != dict(expected_execution_profile)
            or execution_profile_sha256(current_profile)
            != expected_execution_profile_sha256
        ):
            raise DispatchValidationError(
                "backend execution profile changed before async worker start"
            )
        configure_profile = getattr(adapter, "configure_execution_profile", None)
        if callable(configure_profile):
            configure_profile(expected_execution_profile)
        build_environment = getattr(adapter, "worker_environment", None)
        if not callable(build_environment):
            raise DispatchValidationError(
                f"backend cannot build a safe async worker environment: {backend}"
            )
        isolated_home = _async_worker_profile_home(
            home=home,
            run_id=run_id,
            backend=backend,
        )
        try:
            environment = build_environment(isolated_home=isolated_home)
        except Exception:
            if isolated_home is not None:
                _cleanup_async_worker_profile_home(isolated_home, home=home)
            raise
        if not isinstance(environment, dict) or any(
            type(name) is not str or type(value) is not str
            for name, value in environment.items()
        ):
            if isolated_home is not None:
                _cleanup_async_worker_profile_home(isolated_home, home=home)
            raise DispatchValidationError(
                f"backend returned an invalid async worker environment: {backend}"
            )
        environment["DYRO_DISPATCH_PROFILE_BACKEND"] = backend
        environment["DYRO_DISPATCH_PROFILE_PROVIDER"] = (
            expected_execution_profile["provider"]
        )
        environment["DYRO_DISPATCH_PROFILE_MODEL"] = (
            expected_execution_profile["model"]
        )
    else:
        if backend == "echo" and (
            not expected_execution_profile_sha256
            or adapter_execution_profile_sha256(get_adapter(backend))
            != expected_execution_profile_sha256
        ):
            raise DispatchValidationError(
                "backend execution profile changed before async worker start"
            )
        environment = {
            name: value
            for name, value in os.environ.items()
            if name in _WORKER_COMMON_ENV and value
        }
    environment["DYRO_LOCAL_AGENT_DISPATCH_HOME"] = str(home)
    return environment


def _async_worker_profile_home(
    *, home: Path, run_id: str, backend: str
) -> Path | None:
    if backend not in list_real_provider_ids():
        return None
    if not run_id.startswith("run-") or "/" in run_id or ".." in run_id:
        raise DispatchValidationError("invalid run_id for async worker profile")
    return runs_dir(home) / f".{run_id}.{backend}.home"


def _cleanup_async_worker_profile_home(path: Path, *, home: Path) -> None:
    parent = runs_dir(home).resolve(strict=True)
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink() or not path.is_dir():
        raise DispatchValidationError("async worker profile home is unsafe")
    if path.resolve(strict=True).parent != parent:
        raise DispatchValidationError("async worker profile home escapes runs directory")
    shutil.rmtree(path)


def _validate_orchestration_binding(
    record: RunRecord,
    *,
    home: Path | None,
) -> None:
    """Fail closed if mutable run state diverges from its signed plan manifest."""
    if not record.orchestration_id:
        return
    from .orchestration_store import OrchestrationStore

    manifest = OrchestrationStore(home, create=False).load(
        record.orchestration_id
    )
    for member, planned in zip(
        manifest.members,
        manifest.plan.members,
        strict=True,
    ):
        if member.run_id != record.run_id:
            continue
        expected_base_head = planned.base_head or ""
        if (
            record.project_root != str(manifest.plan.project_root)
            or record.backend != member.backend
            or record.thread_id != member.role_id
            or record.contract != planned.normalized_contract
            or record.planned_context_sha256 != planned.context_sha256
            or record.planned_base_head != expected_base_head
            or record.planned_execution_profile_sha256
            != execution_profile_sha256(planned.execution_profile)
            or dict(record.planned_execution_profile or {})
            != dict(planned.execution_profile)
        ):
            raise DispatchValidationError(
                "batch run state does not match its orchestration plan"
            )
        return
    raise DispatchValidationError(
        "batch run is not a member of its orchestration plan"
    )


def _reap_async_worker(
    process: subprocess.Popen[bytes],
    *,
    store: RunStore,
    run_id: str,
    worker_token: str,
    worker_profile_home: Path | None = None,
    home: Path | None = None,
) -> None:
    try:
        return_code = process.wait()
        current = store.fail_if_reserved_worker(
            run_id,
            worker_token=worker_token,
            error=(
                "async worker exited before publishing a terminal result "
                f"(exit_code={return_code})"
            ),
        )
        if current.status == "running":
            if not store.cleanup_backend_if_owned(
                run_id,
                worker_token=worker_token,
            ):
                return
            store.fail_if_active_worker(
                run_id,
                worker_token=worker_token,
                error=(
                    "async worker exited before publishing a terminal result "
                    f"(exit_code={return_code})"
                ),
            )
    finally:
        try:
            if worker_profile_home is not None and home is not None:
                _cleanup_async_worker_profile_home(worker_profile_home, home=home)
        finally:
            with _ASYNC_WORKERS_LOCK:
                _ASYNC_WORKERS.discard(process)


def _start_async_reaper(
    process: subprocess.Popen[bytes],
    *,
    store: RunStore,
    run_id: str,
    worker_token: str,
    worker_profile_home: Path | None = None,
    home: Path | None = None,
) -> None:
    """Start the waiter only after the startup handshake is resolved."""
    threading.Thread(
        target=_reap_async_worker,
        args=(process,),
        kwargs={
            "store": store,
            "run_id": run_id,
            "worker_token": worker_token,
            "worker_profile_home": worker_profile_home,
            "home": home,
        },
        daemon=True,
    ).start()


def _terminate_async_worker(process: subprocess.Popen[bytes]) -> None:
    """Bound a detached worker that never completes its startup handshake."""
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait(timeout=1.0)
        return
    except OSError as exc:
        raise DispatchValidationError(
            "async worker process group could not be signalled"
        ) from exc
    # Do not reap the group leader before escalation: while it is alive or a
    # zombie its pid cannot be reused, so the dedicated pgid still identifies
    # the worker generation and any surviving descendants.
    time.sleep(1.0)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError as exc:
        raise DispatchValidationError(
            "async worker process group could not be killed"
        ) from exc
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired as exc:
        raise DispatchValidationError(
            "async worker process group could not be terminated"
        ) from exc


def _failure_outcome(
    *,
    record: RunRecord,
    error: str,
    warning: str | None = None,
) -> _WorkerOutcome:
    safe_error = safe_error_text(error)
    warnings = [safe_error_text(warning or error)]
    return _WorkerOutcome(
        status="failed",
        error=safe_error,
        result=build_result(
            run_id=record.run_id,
            status="error",
            summary="",
            cwd=Path(record.project_root),
            evidence=[],
            backend=record.backend,
            error_code="worker_exception",
            warnings=warnings,
        ).to_mapping(),
    )


def _cancelled_outcome(
    *,
    record: RunRecord,
    cwd: Path | None = None,
    shadow_path: str = "",
    duration_ms: int = 0,
) -> _WorkerOutcome:
    return _WorkerOutcome(
        status="cancelled",
        error="cancelled",
        shadow_path=shadow_path,
        result=build_result(
            run_id=record.run_id,
            status="cancelled",
            summary="",
            cwd=cwd or Path(record.project_root),
            evidence=[],
            backend=record.backend,
            error_code="cancelled",
            warnings=["run cancellation was requested"],
            usage={"duration_ms": duration_ms},
        ).to_mapping(),
    )


def refuse_production_actions(flags: Mapping[str, object] | None) -> None:
    if not flags:
        return
    for key in FORBIDDEN:
        if flags.get(key):
            raise DispatchValidationError(
                f"local agent dispatch forbids production action: {key}"
            )


def _configured_backend_id(backend: str, *, home: Path | None) -> str | None:
    if backend != "auto":
        return backend
    return selected_route(home=home)


def preflight_dispatch(
    payload: Mapping[str, object],
    *,
    project_root: Path,
    home: Path | None = None,
) -> dict[str, object]:
    """Validate a dispatch request without creating state or probing a CLI."""
    refuse_production_actions(
        payload.get("production_actions")
        if isinstance(payload.get("production_actions"), dict)
        else None
    )
    contract = parse_task_contract(payload)
    context = collect_guarded_context(contract.files, project_root)
    configured = _configured_backend_id(contract.backend, home=home)
    resolved_backend: str | None = None
    if configured is not None:
        adapter = get_adapter(configured, require_strict=contract.strict)
        resolved_backend = adapter.id
    return {
        "contract": contract,
        "context_files": len(context),
        "resolved_backend": resolved_backend,
        "requires_provider_selection": contract.backend == "auto" and configured is None,
        "requires_allow_unconfined_provider": bool(
            resolved_backend in list_real_provider_ids()
            and not contract.strict
            and not contract.allow_unconfined_provider
        ),
        "requires_allow_offline_simulation": bool(
            resolved_backend == "echo" and not contract.allow_offline_simulation
        ),
    }


def _resolve_execution_backend(contract, *, home: Path | None):
    configured = _configured_backend_id(contract.backend, home=home)
    if configured is not None:
        adapter = get_adapter(configured, require_strict=contract.strict)
        if not adapter.available():
            raise DispatchValidationError(f"backend not available: {configured}")
        if not adapter_is_authenticated(adapter):
            raise DispatchValidationError(f"backend is not authenticated: {configured}")
        return adapter

    candidates = []
    for backend_id in list_real_provider_ids():
        adapter = get_adapter(backend_id, require_strict=contract.strict)
        if adapter.available() and adapter_is_authenticated(adapter):
            candidates.append(adapter)
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise DispatchValidationError(
            "no authenticated real provider found; install/login Codex or Claude, "
            "then run `dyro dispatch route add default <backend>`"
        )
    raise DispatchValidationError(
        "multiple real providers are ready; choose one with "
        "`dyro dispatch route add default <backend>` or pass --backend"
    )


class DispatchSupervisor:
    def __init__(
        self,
        *,
        home: Path | None = None,
        max_per_backend: int = 2,
        max_global: int = 4,
    ) -> None:
        _require_posix_supervision()
        self.home = home
        self.store = RunStore(home)
        self.store.reconcile_orphaned_workers()
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
        context = collect_guarded_context(contract.files, project_root)
        adapter = _resolve_execution_backend(contract, home=self.home)
        backend = adapter.id
        if contract.strict and not getattr(adapter, "strict_isolation", False):
            raise DispatchValidationError(
                f"backend does not provide strict isolation: {backend}"
            )
        if backend == "echo" and not contract.allow_offline_simulation:
            raise DispatchValidationError(
                "offline echo simulation requires allow_offline_simulation=true"
            )
        if (
            backend in list_real_provider_ids()
            and not contract.strict
            and not contract.allow_unconfined_provider
        ):
            raise DispatchValidationError(
                "real provider access requires allow_unconfined_provider=true; "
                "files are projected for read-only work but this is not OS-level isolation"
            )
        planned_base_head = (
            review_edit_snapshot(project_root, tuple(context))
            if contract.mode == "edit"
            else ""
        )
        execution_profile = adapter_execution_profile(adapter)
        return self.store.create(
            contract=contract,
            project_root=project_root,
            backend=backend,
            panel_id=panel_id,
            planned_context_sha256=guarded_context_sha256(context),
            planned_base_head=planned_base_head,
            planned_execution_profile_sha256=execution_profile_sha256(
                execution_profile
            ),
            planned_execution_profile=execution_profile,
        )

    def execute(
        self,
        run_id: str,
        *,
        timeout_seconds: float = 120.0,
        sync: bool = True,
        worker_token: str | None = None,
    ) -> RunRecord:
        _validate_timeout(timeout_seconds)
        if not sync:
            if worker_token is not None:
                raise DispatchValidationError(
                    "worker_token is only supported for synchronous execution"
                )
            return self.spawn_worker(run_id, timeout_seconds=timeout_seconds)
        record = self.store.load(run_id)
        if record.status != "accepted":
            if record.status == "running":
                raise DispatchValidationError(f"run is already claimed: {run_id}")
            return record

        leases = self.slots.acquire(record.backend)
        lease_paths = [str(lease.slot_dir) for lease in leases]
        worker_token = worker_token or secrets.token_hex(16)

        def abort_on_lease_failure(_failure: Exception) -> None:
            if not self.store.cleanup_backend_if_owned(
                run_id,
                worker_token=worker_token,
            ):
                raise DispatchValidationError(
                    "backend cleanup could not be proven after lease "
                    "heartbeat failure"
                )

        heartbeat = LeaseHeartbeat(
            leases,
            on_failure=abort_on_lease_failure,
        )
        heartbeat_started = False
        claimed = False
        outcome: _WorkerOutcome | None = None
        operation_error: BaseException | None = None
        try:
            worker_identity = current_identity()
            self.store.claim_for_execution(
                run_id,
                worker_token=worker_token,
                lease_slots=lease_paths,
                worker_pid=worker_identity.pid,
                worker_started_at=worker_identity.started_at,
            )
            claimed = True
            for lease in leases:
                lease.bind_run(run_id)
            heartbeat_started = True
            heartbeat.start()
            outcome = self._run_worker(
                run_id,
                timeout_seconds=timeout_seconds,
                worker_token=worker_token,
                lease_check=heartbeat.check,
            )
        except BaseException as exc:  # preserve cleanup on interrupts/exits
            operation_error = exc

        backend_cleanup_error: Exception | None = None
        if claimed:
            try:
                if not self.store.cleanup_backend_if_owned(
                    run_id,
                    worker_token=worker_token,
                ):
                    backend_cleanup_error = DispatchValidationError(
                        "backend process-group cleanup could not be proven"
                    )
            except Exception as exc:  # noqa: BLE001 - keep run nonterminal
                backend_cleanup_error = exc

        cleanup_errors: list[Exception] = []
        if heartbeat_started:
            try:
                heartbeat.stop()
            except Exception as exc:  # noqa: BLE001 - aggregate lifecycle failures
                cleanup_errors.append(exc)
        if backend_cleanup_error is None:
            try:
                self.slots.release_all(leases)
            except Exception as exc:  # noqa: BLE001 - aggregate failures
                cleanup_errors.append(exc)
        cleanup_detail = "; ".join(safe_error_text(exc) for exc in cleanup_errors)
        preserved_outcome_error = False

        if not claimed:
            if operation_error is not None:
                raise operation_error
            if cleanup_errors:
                raise DispatchValidationError(
                    f"unclaimed worker cleanup failed: {cleanup_detail}"
                )
            raise DispatchValidationError("worker exited without claiming run")

        if backend_cleanup_error is not None:
            if operation_error is not None:
                raise DispatchValidationError(
                    "worker failed and backend cleanup could not be proven: "
                    f"{backend_cleanup_error}"
                ) from operation_error
            raise backend_cleanup_error

        cancellation_requested = self.store.cancel_requested(
            run_id,
            worker_token=worker_token,
        )
        if cancellation_requested:
            outcome = _cancelled_outcome(record=record)
            # Backend termination may surface as an adapter or parser error.
            # Once cleanup is proven, the persisted cancellation is the cause.
            operation_error = None
        elif operation_error is not None:
            error = "worker failed after claim: " + safe_error_text(operation_error)
            warning = error
            if cleanup_errors:
                warning += (
                    "; lifecycle cleanup also failed: "
                    + cleanup_detail
                )
            outcome = _failure_outcome(
                record=record,
                error=error,
                warning=warning,
            )
        if cleanup_errors:
            if outcome is not None and outcome.status in {
                "failed",
                "timeout",
                "cancelled",
            }:
                preserved_outcome_error = True
                result = dict(outcome.result)
                warnings = list(result.get("warnings") or [])
                warnings.append(
                    "worker lifecycle cleanup also failed: "
                    + cleanup_detail
                )
                result["warnings"] = warnings
                outcome = _WorkerOutcome(
                    status=outcome.status,
                    result=result,
                    error=outcome.error,
                    shadow_path=outcome.shadow_path,
                )
            else:
                error = (
                    f"worker lifecycle cleanup failed: {cleanup_detail}"
                )
                outcome = _failure_outcome(
                    record=record,
                    error=error,
                )

        if outcome is None:
            raise DispatchValidationError("worker produced no terminal outcome")

        try:
            final_record = self.store.update_status(
                run_id,
                outcome.status,
                result=outcome.result,
                error=outcome.error,
                shadow_path=outcome.shadow_path,
                expected_worker_token=worker_token,
            )
        except Exception as state_exc:
            if self.store.cancel_requested(
                run_id,
                worker_token=worker_token,
            ):
                # Close the check/update race: update_status rejects any
                # non-cancelled terminal transition once the request wins its
                # run lock, then this exact worker retries as cancelled.
                outcome = _cancelled_outcome(record=record)
                final_record = self.store.update_status(
                    run_id,
                    outcome.status,
                    result=outcome.result,
                    error=outcome.error,
                    shadow_path=outcome.shadow_path,
                    expected_worker_token=worker_token,
                )
                operation_error = None
            elif operation_error is not None:
                raise DispatchValidationError(
                    "worker failed and running state could not be "
                    f"terminalized: {state_exc}"
                ) from operation_error
            elif cleanup_errors:
                raise DispatchValidationError(
                    "worker lifecycle cleanup failed and run state could "
                    f"not be terminalized: {state_exc}"
                ) from cleanup_errors[0]
            else:
                raise

        if operation_error is not None:
            raise operation_error
        if cleanup_errors:
            if preserved_outcome_error and outcome.error:
                raise DispatchValidationError(
                    f"worker {outcome.status}: {outcome.error}; "
                    f"lifecycle cleanup also failed: {cleanup_detail}"
                ) from cleanup_errors[0]
            raise cleanup_errors[0]
        return final_record

    def _fail_async_supervision(
        self,
        *,
        process: subprocess.Popen[bytes] | None,
        run_id: str,
        worker_token: str,
        error: Exception,
        worker_profile_home: Path | None = None,
    ) -> RunRecord:
        """Fail a spawn only after its worker and backend are proven stopped."""
        cleanup_errors: list[Exception] = []
        if process is not None:
            try:
                _terminate_async_worker(process)
            except Exception as cleanup_exc:  # noqa: BLE001
                cleanup_errors.append(cleanup_exc)
            finally:
                with _ASYNC_WORKERS_LOCK:
                    _ASYNC_WORKERS.discard(process)
        current = self.store.load(run_id)
        if current.status == "running":
            try:
                if not self.store.cleanup_backend_if_owned(
                    run_id,
                    worker_token=worker_token,
                ):
                    cleanup_errors.append(
                        DispatchValidationError(
                            "backend cleanup could not be proven"
                        )
                    )
            except Exception as cleanup_exc:  # noqa: BLE001
                cleanup_errors.append(cleanup_exc)
        if worker_profile_home is not None:
            try:
                _cleanup_async_worker_profile_home(
                    worker_profile_home,
                    home=dispatch_home(self.home),
                )
            except Exception as cleanup_exc:  # noqa: BLE001
                cleanup_errors.append(cleanup_exc)
        if cleanup_errors:
            detail = "; ".join(str(item) for item in cleanup_errors)
            raise DispatchValidationError(
                "worker spawn supervision failed and cleanup could not "
                f"be proven: {detail}"
            ) from error
        return self.store.fail_if_active_worker(
            run_id,
            worker_token=worker_token,
            error="worker spawn failed: " + safe_error_text(error),
        )

    def spawn_worker(
        self,
        run_id: str,
        *,
        timeout_seconds: float = 120.0,
    ) -> RunRecord:
        _validate_timeout(timeout_seconds)
        record = self.store.load(run_id)
        if record.status != "accepted":
            raise DispatchValidationError(
                f"run is not available for async spawn: {run_id} status={record.status}"
            )
        home = dispatch_home(self.home)
        log_path = runs_dir(home) / f"{run_id}.worker.log"
        worker_profile_home = _async_worker_profile_home(
            home=home,
            run_id=run_id,
            backend=record.backend,
        )
        worker_token = secrets.token_hex(16)
        try:
            record = self.store.reserve_async_worker(
                run_id,
                worker_token=worker_token,
            )
        except Exception:
            raise
        try:
            environment = _worker_environment(
                backend=record.backend,
                home=home,
                run_id=run_id,
                expected_execution_profile_sha256=(
                    record.planned_execution_profile_sha256
                ),
                expected_execution_profile=dict(
                    record.planned_execution_profile or {}
                ),
            )
        except Exception as exc:  # noqa: BLE001 - exact reservation owns cleanup
            if worker_profile_home is not None:
                try:
                    _cleanup_async_worker_profile_home(
                        worker_profile_home,
                        home=home,
                    )
                except Exception:
                    pass
            return self._fail_async_supervision(
                process=None,
                run_id=run_id,
                worker_token=worker_token,
                error=exc,
                worker_profile_home=worker_profile_home,
            )
        package_root = Path(__file__).resolve().parents[2]
        bootstrap = (
            "import runpy,sys;"
            f"sys.path.insert(0,{str(package_root)!r});"
            "runpy.run_module("
            "'experiments.local_agent_dispatch',run_name='__main__')"
        )
        process: subprocess.Popen[bytes] | None = None
        try:
            log_descriptor = os.open(
                log_path,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_APPEND
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                os.fchmod(log_descriptor, 0o600)
            except Exception:
                os.close(log_descriptor)
                raise
            with os.fdopen(log_descriptor, "ab") as log:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-I",
                        "-c",
                        bootstrap,
                        "--home",
                        str(home),
                        "worker",
                        run_id,
                        "--worker-token",
                        worker_token,
                        "--timeout",
                        str(timeout_seconds),
                    ],
                    cwd=home,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    close_fds=True,
                    env=environment,
                )
            with _ASYNC_WORKERS_LOCK:
                _ASYNC_WORKERS.add(process)
        except Exception as exc:  # noqa: BLE001 - never orphan a live worker
            return self._fail_async_supervision(
                process=process,
                run_id=run_id,
                worker_token=worker_token,
                error=exc,
                worker_profile_home=worker_profile_home,
            )
        assert process is not None
        startup_deadline = time.monotonic() + ASYNC_STARTUP_TIMEOUT_SECONDS
        while time.monotonic() < startup_deadline:
            current = self.store.load(run_id)
            if current.status != "accepted":
                try:
                    _start_async_reaper(
                        process,
                        store=self.store,
                        run_id=run_id,
                        worker_token=worker_token,
                        worker_profile_home=worker_profile_home,
                        home=home,
                    )
                except Exception as exc:  # noqa: BLE001
                    return self._fail_async_supervision(
                        process=process,
                        run_id=run_id,
                        worker_token=worker_token,
                        error=exc,
                        worker_profile_home=worker_profile_home,
                    )
                return current
            if process.poll() is not None:
                _reap_async_worker(
                    process,
                    store=self.store,
                    run_id=run_id,
                    worker_token=worker_token,
                    worker_profile_home=worker_profile_home,
                    home=home,
                )
                return self.store.load(run_id)
            time.sleep(0.02)
        current = self.store.fail_if_reserved_worker(
            run_id,
            worker_token=worker_token,
            error="async worker did not claim run before startup deadline",
        )
        if current.status != "failed":
            try:
                _start_async_reaper(
                    process,
                    store=self.store,
                    run_id=run_id,
                    worker_token=worker_token,
                    worker_profile_home=worker_profile_home,
                    home=home,
                )
            except Exception as exc:  # noqa: BLE001
                return self._fail_async_supervision(
                    process=process,
                    run_id=run_id,
                    worker_token=worker_token,
                    error=exc,
                    worker_profile_home=worker_profile_home,
                )
            return current
        _terminate_async_worker(process)
        with _ASYNC_WORKERS_LOCK:
            _ASYNC_WORKERS.discard(process)
        if worker_profile_home is not None:
            _cleanup_async_worker_profile_home(worker_profile_home, home=home)
        return current

    def _run_worker(
        self,
        run_id: str,
        *,
        timeout_seconds: float,
        worker_token: str,
        lease_check: Callable[[], None],
    ) -> _WorkerOutcome:
        record = self.store.load(run_id)
        _validate_orchestration_binding(record, home=self.home)
        contract = parse_task_contract(record.contract)
        project_root = Path(record.project_root)
        started = time.time()
        cleanup_warning = ""
        shadow_path = ""

        def cancellation_requested() -> bool:
            return self.store.cancel_requested(
                run_id,
                worker_token=worker_token,
            )

        def check_cancellation() -> None:
            if cancellation_requested():
                raise _CooperativeCancellation

        try:
            lease_check()
            check_cancellation()
            adapter = get_adapter(
                record.backend,
                require_strict=contract.strict,
            )
            context = collect_guarded_context(contract.files, project_root)
            if not record.planned_context_sha256:
                raise DispatchValidationError(
                    "run is missing its reviewed context snapshot"
                )
            if not record.planned_execution_profile_sha256:
                raise DispatchValidationError(
                    "run is missing its reviewed backend execution profile"
                )
            current_execution_profile = adapter_execution_profile(adapter)
            if current_execution_profile != dict(
                record.planned_execution_profile or {}
            ) or execution_profile_sha256(
                current_execution_profile
            ) != record.planned_execution_profile_sha256:
                raise DispatchValidationError(
                    "backend execution profile changed after run acceptance"
                )
            configure_profile = getattr(
                adapter,
                "configure_execution_profile",
                None,
            )
            if callable(configure_profile):
                configure_profile(record.planned_execution_profile or {})
            if guarded_context_sha256(context) != record.planned_context_sha256:
                raise DispatchValidationError(
                    "guarded context changed after the reviewed batch plan"
                )
            lease_check()
            check_cancellation()

            work_cwd = project_root
            edit_workspace: EditWorkspace | None = None
            use_context_projection = (
                contract.strict
                or (
                    contract.mode == "read-only"
                    and record.backend in list_real_provider_ids()
                )
            )
            if use_context_projection:
                shadow_root = shadow_dir(self.home) / run_id
                if shadow_root.exists():
                    raise DispatchValidationError("context shadow already exists for run")
                materialize_strict_shadow(
                    shadow_root=shadow_root,
                    root_dir=project_root,
                    relative_files=context,
                )
                work_cwd = shadow_root
                shadow_path = str(shadow_root)

            if contract.mode == "edit":
                if not record.planned_base_head:
                    raise DispatchValidationError(
                        "edit run is missing its reviewed Git HEAD"
                    )
                edit_workspace = EditWorkspace.create(
                    project_root=project_root,
                    home=self.home,
                    run_id=run_id,
                    base_head=record.planned_base_head,
                )
                work_cwd = edit_workspace.worktree_root
                try:
                    lease_check()
                    check_cancellation()
                except BaseException:
                    try:
                        edit_workspace.cleanup()
                    except Exception as exc:  # noqa: BLE001 - preserve cause
                        cleanup_warning = (
                            "edit workspace cleanup could not be completed: "
                            + safe_error_text(exc)
                        )
                    raise
            else:
                lease_check()
                check_cancellation()

            envelope_mapping: dict[str, object]
            try:
                check_cancellation()
                if contract.strict and not getattr(
                    adapter,
                    "strict_isolation",
                    False,
                ):
                    raise DispatchValidationError(
                        f"backend does not provide strict isolation: {record.backend}"
                    )
                configure_tracking = getattr(
                    adapter,
                    "configure_process_tracking",
                    None,
                )
                if callable(configure_tracking):
                    def bind_backend_process(
                        pid: int,
                        pgid: int,
                        started_at: str,
                    ) -> None:
                        lease_check()
                        check_cancellation()
                        self.store.bind_backend_process(
                            run_id,
                            worker_token=worker_token,
                            backend_pid=pid,
                            backend_pgid=pgid,
                            backend_started_at=started_at,
                        )
                        lease_check()
                        check_cancellation()

                    configure_tracking(
                        observer=bind_backend_process,
                        lifetime_lock_path=(
                            runs_dir(self.home)
                            / f"{run_id}.backend.lifetime"
                        ),
                    )
                configure_cancellation = getattr(
                    adapter,
                    "configure_cancellation",
                    None,
                )
                if callable(configure_cancellation):
                    configure_cancellation(
                        cancel_check=cancellation_requested,
                    )
                lease_check()
                check_cancellation()
                adapter_result = adapter.run(
                    contract=contract,
                    cwd=work_cwd,
                    context_files=context,
                    timeout_seconds=timeout_seconds,
                )
                lease_check()
                check_cancellation()
                patch_ref = (
                    edit_workspace.seal_patch()
                    if edit_workspace is not None
                    else None
                )

                # Verify edit evidence before removing its isolated worktree.
                verify_cwd = (
                    work_cwd
                    if use_context_projection or edit_workspace is not None
                    else project_root
                )
                isolation = (
                    "strict"
                    if contract.strict or record.backend == "echo"
                    else (
                        "context-projection"
                        if use_context_projection
                        else "best-effort-unconfined"
                    )
                )
                warnings = list(adapter_result.warnings)
                if isolation == "context-projection":
                    warnings.append(
                        "read-only provider ran in a guarded context projection; "
                        "this is not a claim of OS-level strict isolation"
                    )
                elif isolation == "best-effort-unconfined":
                    warnings.append(
                        "unconfined provider access was explicitly acknowledged; "
                        "files limits injected context, not all provider tool reads"
                    )
                envelope = build_result(
                    run_id=run_id,
                    status=(
                        adapter_result.status
                        if adapter_result.status != "ok"
                        else "ok"
                    ),
                    summary=adapter_result.summary,
                    cwd=verify_cwd,
                    evidence=adapter_result.evidence,
                    confidence=adapter_result.confidence,
                    patch_ref=patch_ref,
                    takeover=adapter_result.takeover,
                    usage={
                        **adapter_result.usage,
                        "duration_ms": int((time.time() - started) * 1000),
                    },
                    warnings=warnings,
                    backend=record.backend,
                    error_code=adapter_result.error_code,
                    execution_kind=adapter_result.execution_kind,
                    isolation=isolation,
                )
            finally:
                if edit_workspace is not None:
                    try:
                        edit_workspace.cleanup()
                    except Exception as exc:  # noqa: BLE001 - preserve result
                        cleanup_warning = (
                            "edit workspace cleanup could not be completed: "
                            + safe_error_text(exc)
                        )
            envelope_mapping = envelope.to_mapping()
            if cleanup_warning:
                warnings = list(envelope_mapping.get("warnings", []))
                warnings.append(cleanup_warning)
                envelope_mapping["warnings"] = warnings
            if adapter_result.status == "timeout":
                status = "timeout"
            elif adapter_result.status == "cancelled":
                status = "cancelled"
            elif adapter_result.status == "ok":
                status = "completed"
            else:
                status = "failed"

            return _WorkerOutcome(
                status,
                result=envelope_mapping,
                error=adapter_result.error_code,
                shadow_path=shadow_path,
            )
        except _CooperativeCancellation:
            outcome = _cancelled_outcome(
                record=record,
                cwd=project_root,
                shadow_path=shadow_path,
                duration_ms=int((time.time() - started) * 1000),
            )
            if cleanup_warning:
                result = dict(outcome.result)
                warnings = list(result.get("warnings") or [])
                warnings.append(cleanup_warning)
                result["warnings"] = warnings
                outcome = _WorkerOutcome(
                    status=outcome.status,
                    result=result,
                    error=outcome.error,
                    shadow_path=outcome.shadow_path,
                )
            return outcome
        except Exception as exc:  # noqa: BLE001 - persist failure onto run
            safe_error = safe_error_text(exc)
            warnings = [safe_error]
            if cleanup_warning:
                warnings.append(cleanup_warning)
            return _WorkerOutcome(
                status="failed",
                error=safe_error,
                result=build_result(
                    run_id=run_id,
                    status="error",
                    summary="",
                    cwd=project_root,
                    evidence=[],
                    backend=record.backend,
                    error_code="worker_exception",
                    warnings=warnings,
                ).to_mapping(),
            )

    def result(self, run_id: str) -> RunRecord:
        self.store.reconcile_orphaned_workers(run_ids={run_id})
        return self.store.load(run_id)

    def cancel(
        self,
        run_id: str,
        *,
        reason: str = "cancel requested",
    ) -> RunRecord:
        """Request cooperative cancellation without signalling an external PID."""
        return self.store.request_cancel(run_id, reason=reason)

    def wait(
        self,
        run_ids: list[str],
        *,
        timeout_seconds: float = 300.0,
        poll_seconds: float = 0.2,
    ) -> list[RunRecord]:
        _validate_timeout(timeout_seconds)
        if (
            isinstance(poll_seconds, bool)
            or not isinstance(poll_seconds, (int, float))
            or not math.isfinite(float(poll_seconds))
            or poll_seconds <= 0
        ):
            raise DispatchValidationError(
                "poll_seconds must be finite and positive"
            )
        deadline = time.time() + timeout_seconds
        terminal = {"completed", "failed", "timeout", "cancelled"}
        next_reconcile = 0.0
        while time.time() < deadline:
            now = time.monotonic()
            if now >= next_reconcile:
                self.store.reconcile_orphaned_workers(run_ids=set(run_ids))
                next_reconcile = now + 1.0
            records = [self.store.load(rid) for rid in run_ids]
            if all(r.status in terminal for r in records):
                return records
            time.sleep(poll_seconds)
        return [self.store.load(rid) for rid in run_ids]

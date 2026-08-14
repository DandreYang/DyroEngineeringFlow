from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from experiments.local_agent_dispatch.cli import main as dispatch_cli_main
from experiments.local_agent_dispatch.adapters.registry import (
    adapter_execution_profile_sha256,
    get_adapter,
)
from experiments.local_agent_dispatch.errors import DispatchValidationError
from experiments.local_agent_dispatch.fileset import (
    collect_guarded_context,
    guarded_context_sha256,
)
from experiments.local_agent_dispatch.gc import gc
from experiments.local_agent_dispatch.orchestration import (
    cancel_batch,
    get_batch_result,
    get_batch_status,
    plan_batch,
    start_batch,
)
import experiments.local_agent_dispatch.orchestration_store as orchestration_store_module
from experiments.local_agent_dispatch.orchestration_store import OrchestrationStore
from experiments.local_agent_dispatch.run_store import RunStore
from experiments.local_agent_dispatch.supervisor import DispatchSupervisor
from experiments.local_agent_dispatch.task_contract import parse_task_contract


def _project(root: Path) -> Path:
    project = root / "project"
    (project / "src").mkdir(parents=True)
    (project / "src" / "app.py").write_text(
        "def hello():\n    return 'hello'\n",
        encoding="utf-8",
    )
    return project


def _contract(backend: str, *, objective: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "backend": backend,
        "mode": "read-only",
        "strict": False,
        "allow_unconfined_provider": True,
        "allow_offline_simulation": False,
        "files": ["src/app.py"],
        "task": {
            "briefing": "Inspect the supplied implementation.",
            "locations": "src/app.py",
            "objective": objective,
            "constraints": "Do not modify source or perform production actions.",
            "output_contract": "Return bounded JSON summary and evidence.",
        },
    }


def _batch_payload(*, request_id: str = "request-001") -> dict[str, object]:
    return {
        "schema_version": 1,
        "request_id": request_id,
        "strategy": "independent",
        "members": [
            {
                "role_id": "finder",
                "timeout_seconds": 30,
                "contract": _contract(
                    "codex", objective="Find one correctness risk."
                ),
            },
            {
                "role_id": "verifier",
                "timeout_seconds": 30,
                "contract": _contract(
                    "claude", objective="Independently verify the implementation."
                ),
            },
        ],
    }


class _ReadyAdapter:
    strict_isolation = False
    supported_modes = frozenset({"read-only", "edit"})

    def __init__(self, backend: str) -> None:
        self.id = backend
        self.command = backend

    def available(self) -> bool:
        return True

    def authenticated(self) -> bool:
        return True


class BatchPlanningTests(unittest.TestCase):
    def test_plan_is_side_effect_free_and_context_bound(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "dispatch-home"
            project = _project(root)
            payload = _batch_payload()

            with (
                patch(
                    "experiments.local_agent_dispatch.orchestration."
                    "candidate_provider_ids",
                    return_value=["codex", "claude"],
                ),
                patch(
                    "experiments.local_agent_dispatch.orchestration.get_adapter",
                    side_effect=lambda backend: _ReadyAdapter(backend),
                ),
            ):
                first = plan_batch(payload, project_root=project, home=home)
                self.assertFalse(home.exists())
                (project / "src" / "app.py").write_text(
                    "def hello():\n    return 'changed'\n",
                    encoding="utf-8",
                )
                second = plan_batch(payload, project_root=project, home=home)

            self.assertNotEqual(first.plan_sha256, second.plan_sha256)
            self.assertEqual(first.effects["starts_provider_processes"], 2)
            self.assertTrue(first.effects["may_use_network_or_bill"])

    def test_plan_never_calls_active_authentication_probe(self) -> None:
        class _PassiveOnlyAdapter(_ReadyAdapter):
            def authenticated(self) -> bool:
                raise AssertionError("planning must not start an auth CLI")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = _project(root)
            with (
                patch(
                    "experiments.local_agent_dispatch.orchestration."
                    "candidate_provider_ids",
                    return_value=["codex", "claude"],
                ),
                patch(
                    "experiments.local_agent_dispatch.orchestration.get_adapter",
                    side_effect=lambda backend: _PassiveOnlyAdapter(backend),
                ),
            ):
                plan = plan_batch(_batch_payload(), project_root=project)
            self.assertEqual(len(plan.members), 2)

    def test_plan_digest_binds_inner_provider_and_model_profile(self) -> None:
        selected_model = {"value": "model-a"}

        class _ProfileAdapter(_ReadyAdapter):
            def execution_profile(self) -> dict[str, str]:
                return {
                    "backend": self.id,
                    "command_path": self.command,
                    "provider": "provider-a",
                    "model": selected_model["value"],
                }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "dispatch-home"
            project = _project(root)
            with (
                patch(
                    "experiments.local_agent_dispatch.orchestration."
                    "candidate_provider_ids",
                    return_value=["codex", "claude"],
                ),
                patch(
                    "experiments.local_agent_dispatch.orchestration.get_adapter",
                    side_effect=lambda backend: _ProfileAdapter(backend),
                ),
            ):
                first = plan_batch(_batch_payload(), project_root=project)
                selected_model["value"] = "model-b"
                second = plan_batch(_batch_payload(), project_root=project)
                with self.assertRaisesRegex(
                    DispatchValidationError, "plan digest changed"
                ):
                    start_batch(
                        _batch_payload(),
                        expected_plan_sha256=first.plan_sha256,
                        project_root=project,
                        home=home,
                    )

            self.assertNotEqual(first.plan_sha256, second.plan_sha256)
            self.assertFalse(home.exists())

    def test_plan_distributes_auto_members_across_ready_providers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = _project(root)
            payload = _batch_payload()
            for member in payload["members"]:  # type: ignore[index]
                member["contract"]["backend"] = "auto"  # type: ignore[index]

            with (
                patch(
                    "experiments.local_agent_dispatch.orchestration."
                    "candidate_provider_ids",
                    return_value=["codex", "claude"],
                ),
                patch(
                    "experiments.local_agent_dispatch.orchestration.get_adapter",
                    side_effect=lambda backend: _ReadyAdapter(backend),
                ),
            ):
                plan = plan_batch(payload, project_root=project)

            self.assertEqual(
                [member.resolved_backend for member in plan.members],
                ["codex", "claude"],
            )

    def test_plan_rejects_unsupported_mode_without_creating_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "dispatch-home"
            project = _project(root)
            payload = _batch_payload()
            payload["members"][0]["contract"]["mode"] = "edit"  # type: ignore[index]

            class _ReadOnlyAdapter(_ReadyAdapter):
                supported_modes = frozenset({"read-only"})

            with (
                patch(
                    "experiments.local_agent_dispatch.orchestration."
                    "candidate_provider_ids",
                    return_value=["codex", "claude"],
                ),
                patch(
                    "experiments.local_agent_dispatch.orchestration.get_adapter",
                    side_effect=lambda backend: _ReadOnlyAdapter(backend),
                ),
                self.assertRaisesRegex(DispatchValidationError, "does not support"),
            ):
                plan_batch(payload, project_root=project, home=home)
            self.assertFalse(home.exists())

    def test_edit_plan_binds_clean_head_and_rejects_selected_file_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = _project(root)
            for arguments in (
                ["git", "init", "-q", str(project)],
                ["git", "-C", str(project), "config", "user.name", "Dyro Test"],
                [
                    "git",
                    "-C",
                    str(project),
                    "config",
                    "user.email",
                    "dyro@example.invalid",
                ],
                ["git", "-C", str(project), "add", "src/app.py"],
                ["git", "-C", str(project), "commit", "-q", "-m", "base"],
            ):
                subprocess.run(
                    arguments,
                    check=True,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            payload = _batch_payload()
            payload["members"][0]["contract"]["mode"] = "edit"  # type: ignore[index]

            with (
                patch(
                    "experiments.local_agent_dispatch.orchestration."
                    "candidate_provider_ids",
                    return_value=["codex", "claude"],
                ),
                patch(
                    "experiments.local_agent_dispatch.orchestration.get_adapter",
                    side_effect=lambda backend: _ReadyAdapter(backend),
                ),
            ):
                plan = plan_batch(payload, project_root=project)
                self.assertRegex(plan.members[0].base_head or "", r"^[0-9a-f]{40}$")
                (project / "src" / "app.py").write_text(
                    "def hello():\n    return 'dirty'\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    DispatchValidationError, "differs from.*HEAD"
                ):
                    plan_batch(payload, project_root=project)


class BatchLifecycleTests(unittest.TestCase):
    def _patch_ready(self):
        return (
            patch(
                "experiments.local_agent_dispatch.orchestration."
                "candidate_provider_ids",
                return_value=["codex", "claude"],
            ),
            patch(
                "experiments.local_agent_dispatch.orchestration.get_adapter",
                side_effect=lambda backend: _ReadyAdapter(backend),
            ),
        )

    def test_start_is_idempotent_and_status_groups_member_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "dispatch-home"
            project = _project(root)
            payload = _batch_payload()
            ready, adapters = self._patch_ready()

            def reserve_only(supervisor, run_id, **_kwargs):
                record = supervisor.store.load(run_id)
                if not record.worker_token:
                    record = supervisor.store.reserve_async_worker(
                        run_id,
                        worker_token=f"worker-{run_id}",
                    )
                return record

            with (
                ready,
                adapters,
                patch(
                    "experiments.local_agent_dispatch.orchestration."
                    "DispatchSupervisor.execute",
                    autospec=True,
                    side_effect=reserve_only,
                ) as execute,
            ):
                plan = plan_batch(payload, project_root=project, home=home)
                first = start_batch(
                    payload,
                    expected_plan_sha256=plan.plan_sha256,
                    project_root=project,
                    home=home,
                )
                second = start_batch(
                    payload,
                    expected_plan_sha256=plan.plan_sha256,
                    project_root=project,
                    home=home,
                )

            self.assertEqual(first["orchestration_id"], second["orchestration_id"])
            self.assertEqual(execute.call_count, 2)
            self.assertEqual(first["status"], "running")
            self.assertEqual(len(first["members"]), 2)
            self.assertEqual(
                get_batch_status(first["orchestration_id"], home=home)["status"],
                "running",
            )

    def test_start_rejects_plan_drift_before_creating_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "dispatch-home"
            project = _project(root)
            payload = _batch_payload()
            ready, adapters = self._patch_ready()
            with ready, adapters:
                plan = plan_batch(payload, project_root=project, home=home)
                payload["members"][0]["contract"]["task"]["objective"] = (  # type: ignore[index]
                    "A changed objective."
                )
                with self.assertRaisesRegex(
                    DispatchValidationError, "plan.*changed|digest"
                ):
                    start_batch(
                        payload,
                        expected_plan_sha256=plan.plan_sha256,
                        project_root=project,
                        home=home,
                    )
            self.assertFalse(home.exists())

    def test_cancel_is_batch_scoped_and_result_preserves_healthy_member(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "dispatch-home"
            project = _project(root)
            payload = _batch_payload()
            ready, adapters = self._patch_ready()
            with (
                ready,
                adapters,
                patch(
                    "experiments.local_agent_dispatch.orchestration."
                    "DispatchSupervisor.execute",
                    autospec=True,
                    side_effect=lambda supervisor, run_id, **_kwargs: (
                        supervisor.store.load(run_id)
                    ),
                ),
            ):
                plan = plan_batch(payload, project_root=project, home=home)
                started = start_batch(
                    payload,
                    expected_plan_sha256=plan.plan_sha256,
                    project_root=project,
                    home=home,
                )

            store = RunStore(home)
            first_id = started["members"][0]["run_id"]
            store.update_status(
                first_id,
                "completed",
                result={
                    "summary": "healthy",
                    "confidence": "high",
                    "evidence": [],
                    "warnings": [],
                    "patch_ref": None,
                },
            )
            cancelled = cancel_batch(started["orchestration_id"], home=home)
            self.assertEqual(cancelled["status"], "partial")
            result = get_batch_result(started["orchestration_id"], home=home)
            self.assertTrue(result["ready"])
            self.assertEqual(result["members"][0]["summary"], "healthy")
            self.assertEqual(result["members"][1]["status"], "cancelled")

    def test_gc_protects_active_batch_then_removes_terminal_batch_together(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "dispatch-home"
            project = _project(root)
            payload = _batch_payload()
            ready, adapters = self._patch_ready()
            with (
                ready,
                adapters,
                patch(
                    "experiments.local_agent_dispatch.orchestration."
                    "DispatchSupervisor.execute",
                    autospec=True,
                    side_effect=lambda supervisor, run_id, **_kwargs: (
                        supervisor.store.load(run_id)
                    ),
                ),
            ):
                plan = plan_batch(payload, project_root=project, home=home)
                started = start_batch(
                    payload,
                    expected_plan_sha256=plan.plan_sha256,
                    project_root=project,
                    home=home,
                )

            active = gc(home=home, max_age_seconds=0, dry_run=False)
            self.assertEqual(active["removed_runs"], [])
            self.assertEqual(active["removed_orchestrations"], [])

            store = RunStore(home)
            for member in started["members"]:
                store.update_status(member["run_id"], "completed", result={})
                patch_root = home / "patches" / member["run_id"]
                patch_root.mkdir(parents=True)
                (patch_root / "changes.patch").write_text(
                    "patch evidence",
                    encoding="utf-8",
                )
                (home / "runs" / f"{member['run_id']}.worker.log").write_text(
                    "bounded log",
                    encoding="utf-8",
                )
                (home / "runs" / f"{member['run_id']}.backend.lifetime").touch()
            terminal = gc(home=home, max_age_seconds=0, dry_run=False)
            self.assertCountEqual(
                terminal["removed_runs"],
                [member["run_id"] for member in started["members"]],
            )
            self.assertEqual(len(terminal["removed_orchestrations"]), 1)
            tombstones = list(
                (home / "orchestrations").glob("request-*.json")
            )
            self.assertEqual(len(tombstones), 1)
            with self.assertRaisesRegex(
                DispatchValidationError,
                "already executed and garbage-collected",
            ):
                OrchestrationStore(home).create_or_load(plan)
            for member in started["members"]:
                self.assertFalse((home / "patches" / member["run_id"]).exists())
                self.assertFalse(
                    (home / "runs" / f"{member['run_id']}.worker.log").exists()
                )
                self.assertFalse(
                    (
                        home
                        / "runs"
                        / f"{member['run_id']}.backend.lifetime"
                    ).exists()
                )

    def test_gc_heals_missing_request_tombstone_before_manifest_removal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "dispatch-home"
            project = _project(root)
            payload = _batch_payload(request_id="request-heal-tombstone")
            ready, adapters = self._patch_ready()
            with (
                ready,
                adapters,
                patch(
                    "experiments.local_agent_dispatch.orchestration."
                    "DispatchSupervisor.execute",
                    autospec=True,
                    side_effect=lambda supervisor, run_id, **_kwargs: (
                        supervisor.store.load(run_id)
                    ),
                ),
            ):
                plan = plan_batch(payload, project_root=project, home=home)
                started = start_batch(
                    payload,
                    expected_plan_sha256=plan.plan_sha256,
                    project_root=project,
                    home=home,
                )
            store = RunStore(home)
            for member in started["members"]:
                store.update_status(member["run_id"], "completed", result={})
            tombstone = next((home / "orchestrations").glob("request-*.json"))
            tombstone.unlink()

            report = gc(home=home, max_age_seconds=0, dry_run=False)

            self.assertEqual(len(report["removed_orchestrations"]), 1)
            self.assertEqual(
                len(list((home / "orchestrations").glob("request-*.json"))),
                1,
            )
            with self.assertRaisesRegex(
                DispatchValidationError,
                "already executed and garbage-collected",
            ):
                OrchestrationStore(home).create_or_load(plan)

    def test_gc_cannot_delete_batch_while_member_states_initialize(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "dispatch-home"
            project = _project(root)
            payload = _batch_payload(request_id="request-init-gc-race")
            ready, adapters = self._patch_ready()
            with ready, adapters:
                plan = plan_batch(payload, project_root=project, home=home)

            entered = threading.Event()
            release = threading.Event()
            start_errors: list[Exception] = []
            start_results: list[dict[str, object]] = []
            gc_results: list[dict[str, object]] = []
            original_ensure = RunStore.ensure_created
            first = True

            def paused_ensure(store, **kwargs):
                nonlocal first
                if first:
                    first = False
                    entered.set()
                    if not release.wait(timeout=2.0):
                        raise RuntimeError("test did not release batch initialization")
                return original_ensure(store, **kwargs)

            def launch() -> None:
                try:
                    start_results.append(
                        start_batch(
                            payload,
                            expected_plan_sha256=plan.plan_sha256,
                            project_root=project,
                            home=home,
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - asserted below
                    start_errors.append(exc)

            def collect() -> None:
                gc_results.append(
                    gc(home=home, max_age_seconds=0, dry_run=False)
                )

            ready, adapters = self._patch_ready()
            with (
                ready,
                adapters,
                patch.object(RunStore, "ensure_created", new=paused_ensure),
                patch(
                    "experiments.local_agent_dispatch.orchestration."
                    "DispatchSupervisor.execute",
                    autospec=True,
                    side_effect=lambda supervisor, run_id, **_kwargs: (
                        supervisor.store.load(run_id)
                    ),
                ),
            ):
                start_thread = threading.Thread(target=launch)
                start_thread.start()
                self.assertTrue(entered.wait(timeout=1.0))
                gc_thread = threading.Thread(target=collect)
                gc_thread.start()
                time.sleep(0.05)
                self.assertTrue(gc_thread.is_alive())
                release.set()
                start_thread.join(timeout=2.0)
                gc_thread.join(timeout=2.0)

            self.assertFalse(start_thread.is_alive())
            self.assertFalse(gc_thread.is_alive())
            self.assertEqual(start_errors, [])
            self.assertEqual(len(start_results), 1)
            self.assertEqual(gc_results[0]["removed_orchestrations"], [])
            self.assertEqual(gc_results[0]["removed_runs"], [])
            manifest = OrchestrationStore(home).load(
                start_results[0]["orchestration_id"]
            )
            self.assertEqual(len(manifest.members), 2)
            run_store = RunStore(home)
            for member in manifest.members:
                self.assertEqual(run_store.load(member.run_id).status, "accepted")

    def test_gc_serializes_with_cancellation_and_cannot_resurrect_manifest(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "dispatch-home"
            project = _project(root)
            payload = _batch_payload(request_id="request-cancel-gc-race")
            ready, adapters = self._patch_ready()
            with (
                ready,
                adapters,
                patch(
                    "experiments.local_agent_dispatch.orchestration."
                    "DispatchSupervisor.execute",
                    autospec=True,
                    side_effect=lambda supervisor, run_id, **_kwargs: (
                        supervisor.store.load(run_id)
                    ),
                ),
            ):
                plan = plan_batch(payload, project_root=project, home=home)
                started = start_batch(
                    payload,
                    expected_plan_sha256=plan.plan_sha256,
                    project_root=project,
                    home=home,
                )
            run_store = RunStore(home)
            for member in started["members"]:
                run_store.update_status(member["run_id"], "completed", result={})
            time.sleep(0.6)

            entered_write = threading.Event()
            release_write = threading.Event()
            errors: list[Exception] = []
            reports: list[dict[str, object]] = []
            original_write = orchestration_store_module.atomic_write_json

            def paused_write(path, mapping) -> None:
                if (
                    Path(path).name
                    == f"{started['orchestration_id']}.json"
                    and mapping.get("cancel_requested") is True
                ):
                    entered_write.set()
                    if not release_write.wait(timeout=2.0):
                        raise RuntimeError("test did not release cancellation write")
                original_write(path, mapping)

            def request_cancel() -> None:
                try:
                    OrchestrationStore(home).request_cancel(
                        started["orchestration_id"]
                    )
                except Exception as exc:  # noqa: BLE001 - asserted below
                    errors.append(exc)

            def collect() -> None:
                try:
                    reports.append(
                        gc(home=home, max_age_seconds=0.5, dry_run=False)
                    )
                except Exception as exc:  # noqa: BLE001 - asserted below
                    errors.append(exc)

            with patch.object(
                orchestration_store_module,
                "atomic_write_json",
                side_effect=paused_write,
            ):
                cancel_thread = threading.Thread(target=request_cancel)
                cancel_thread.start()
                self.assertTrue(entered_write.wait(timeout=1.0))
                gc_thread = threading.Thread(target=collect)
                gc_thread.start()
                time.sleep(0.05)
                self.assertTrue(gc_thread.is_alive())
                release_write.set()
                cancel_thread.join(timeout=2.0)
                gc_thread.join(timeout=2.0)

            self.assertFalse(cancel_thread.is_alive())
            self.assertFalse(gc_thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(len(reports), 1)
            self.assertEqual(reports[0]["removed_orchestrations"], [])
            self.assertEqual(reports[0]["removed_runs"], [])
            manifest = OrchestrationStore(home).load(
                started["orchestration_id"]
            )
            self.assertTrue(manifest.cancel_requested)
            for member in started["members"]:
                self.assertEqual(
                    run_store.load(member["run_id"]).status,
                    "completed",
                )

    def test_gc_recovers_when_one_terminal_member_record_is_already_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "dispatch-home"
            project = _project(root)
            payload = _batch_payload(request_id="request-gc-recovery")
            ready, adapters = self._patch_ready()
            with (
                ready,
                adapters,
                patch(
                    "experiments.local_agent_dispatch.orchestration."
                    "DispatchSupervisor.execute",
                    autospec=True,
                    side_effect=lambda supervisor, run_id, **_kwargs: (
                        supervisor.store.load(run_id)
                    ),
                ),
            ):
                plan = plan_batch(payload, project_root=project, home=home)
                started = start_batch(
                    payload,
                    expected_plan_sha256=plan.plan_sha256,
                    project_root=project,
                    home=home,
                )
            store = RunStore(home)
            for member in started["members"]:
                store.update_status(member["run_id"], "completed", result={})
            store.delete(started["members"][0]["run_id"])

            report = gc(home=home, max_age_seconds=0, dry_run=False)
            self.assertEqual(
                report["removed_runs"],
                [started["members"][1]["run_id"]],
            )
            self.assertEqual(len(report["removed_orchestrations"]), 1)

    def test_mutated_batch_run_binding_is_attention_and_cannot_execute(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "dispatch-home"
            project = _project(root)
            payload = _batch_payload(request_id="request-binding")
            ready, adapters = self._patch_ready()
            with (
                ready,
                adapters,
                patch(
                    "experiments.local_agent_dispatch.orchestration."
                    "DispatchSupervisor.execute",
                    autospec=True,
                    side_effect=lambda supervisor, run_id, **_kwargs: (
                        supervisor.store.load(run_id)
                    ),
                ),
            ):
                plan = plan_batch(payload, project_root=project, home=home)
                started = start_batch(
                    payload,
                    expected_plan_sha256=plan.plan_sha256,
                    project_root=project,
                    home=home,
                )
            run_id = started["members"][0]["run_id"]
            state_path = home / "runs" / f"{run_id}.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["planned_context_sha256"] = ""
            state_path.write_text(json.dumps(state), encoding="utf-8")

            status = get_batch_status(
                started["orchestration_id"],
                home=home,
                reconcile=False,
            )
            self.assertEqual(status["status"], "attention_required")
            with self.assertRaisesRegex(
                DispatchValidationError, "does not match"
            ):
                DispatchSupervisor(home=home).execute(
                    run_id,
                    timeout_seconds=5,
                    sync=True,
                )
            finished = RunStore(home).load(run_id)
            self.assertEqual(finished.status, "failed")
            self.assertIn("does not match", finished.error)

    def test_non_reconciling_status_does_not_create_missing_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "missing-home"
            with self.assertRaisesRegex(
                DispatchValidationError, "orchestration not found"
            ):
                get_batch_status(
                    "orch-0000000000000000",
                    home=home,
                    reconcile=False,
                )
            self.assertFalse(home.exists())

    def test_worker_rejects_context_drift_after_batch_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "dispatch-home"
            project = _project(root)
            contract = parse_task_contract(
                _contract("codex", objective="Review the stable snapshot.")
            )
            digest = guarded_context_sha256(
                collect_guarded_context(contract.files, project)
            )
            store = RunStore(home)
            execution_profile = get_adapter("codex").execution_profile()
            record = store.create(
                contract=contract,
                project_root=project,
                backend="codex",
                thread_id="finder",
                planned_context_sha256=digest,
                planned_execution_profile_sha256=(
                    adapter_execution_profile_sha256(get_adapter("codex"))
                ),
                planned_execution_profile=execution_profile,
            )
            (project / "src" / "app.py").write_text(
                "def hello():\n    return 'drifted'\n",
                encoding="utf-8",
            )

            finished = DispatchSupervisor(home=home).execute(
                record.run_id,
                timeout_seconds=5,
                sync=True,
            )
            self.assertEqual(finished.status, "failed")
            self.assertIn("context changed", finished.error)


class BatchCliTests(unittest.TestCase):
    def test_batch_plan_cli_emits_plan_without_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "dispatch-home"
            project = _project(root)
            stream = io.StringIO()
            with (
                patch(
                    "experiments.local_agent_dispatch.orchestration."
                    "candidate_provider_ids",
                    return_value=["codex", "claude"],
                ),
                patch(
                    "experiments.local_agent_dispatch.orchestration.get_adapter",
                    side_effect=lambda backend: _ReadyAdapter(backend),
                ),
                patch("sys.stdin", io.StringIO(json.dumps(_batch_payload()))),
                redirect_stdout(stream),
            ):
                code = dispatch_cli_main(
                    [
                        "--home",
                        str(home),
                        "batch-plan",
                        "--project",
                        str(project),
                        "--stdin",
                    ]
                )

            self.assertEqual(code, 0)
            output = json.loads(stream.getvalue())
            self.assertEqual(output["kind"], "local-agent-dispatch-batch-plan")
            self.assertFalse(home.exists())


if __name__ == "__main__":
    unittest.main()

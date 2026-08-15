from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from experiments.local_agent_dispatch.adapters.base import AdapterResult
from experiments.local_agent_dispatch.adapters.registry import (
    adapter_execution_profile_sha256,
)
from experiments.local_agent_dispatch.adapters.subprocess_cli import (
    SubprocessCliAdapter,
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
from experiments.local_agent_dispatch.bounded_process import run_bounded
from experiments.local_agent_dispatch.errors import DispatchValidationError
from experiments.local_agent_dispatch.run_store import (
    MAX_CANCEL_REASON_CHARS,
    MAX_RUN_STATE_BYTES,
    RunRecord,
    RunStore,
)
from experiments.local_agent_dispatch.supervisor import (
    DispatchSupervisor,
    _worker_environment,
)
from experiments.local_agent_dispatch.task_contract import parse_task_contract


def _payload(*, backend: str = "fake") -> dict[str, object]:
    return {
        "schema_version": 1,
        "backend": backend,
        "mode": "read-only",
        "strict": False,
        "files": ["app.py"],
        "task": {
            "briefing": "Inspect the supplied file.",
            "locations": "app.py",
            "objective": "Return a bounded result.",
            "constraints": "Do not touch production actions.",
            "output_contract": "JSON summary and evidence.",
        },
    }


class _SlowSubprocessAdapter(SubprocessCliAdapter):
    def __init__(self) -> None:
        super().__init__(backend_id="fake", command=sys.executable)
        self.started = threading.Event()

    def run(self, *, contract, cwd, context_files, timeout_seconds):
        del contract, context_files
        self.started.set()
        return self._run(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=cwd,
            prompt="",
            timeout_seconds=timeout_seconds,
        )


class _CooperativeAdapter:
    id = "fake"
    command = "fake"
    strict_isolation = False
    supported_modes = frozenset({"read-only", "edit"})

    def __init__(self) -> None:
        self.started = threading.Event()
        self._cancel_check = lambda: False

    def available(self) -> bool:
        return True

    def authenticated(self) -> bool:
        return True

    def configure_cancellation(self, *, cancel_check) -> None:
        self._cancel_check = cancel_check

    def run(self, *, contract, cwd, context_files, timeout_seconds):
        del contract, cwd, context_files, timeout_seconds
        self.started.set()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if self._cancel_check():
                return AdapterResult(
                    status="cancelled",
                    summary="",
                    error_code="cancelled",
                )
            time.sleep(0.01)
        raise AssertionError("cancellation was not observed")


class RunStoreCancellationTests(unittest.TestCase):
    def _store_and_contract(self, root: Path):
        project = root / "project"
        project.mkdir()
        (project / "app.py").write_text("safe = True\n", encoding="utf-8")
        return RunStore(root / "home"), parse_task_contract(_payload()), project

    def test_accepted_running_and_terminal_cancellation_are_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, contract, project = self._store_and_contract(Path(tmp))

            accepted = store.create(
                contract=contract,
                project_root=project,
                backend="fake",
            )
            cancelled = store.request_cancel(accepted.run_id, reason="operator stop")
            repeated = store.request_cancel(accepted.run_id, reason="later reason")
            self.assertEqual(cancelled.status, "cancelled")
            self.assertGreater(cancelled.cancel_requested_at, 0)
            self.assertEqual(cancelled.cancel_reason, "operator stop")
            self.assertEqual(repeated.revision, cancelled.revision)
            self.assertEqual(repeated.cancel_reason, "operator stop")

            running = store.create(
                contract=contract,
                project_root=project,
                backend="fake",
            )
            running = store.claim_for_execution(
                running.run_id,
                worker_token="right-generation",
                lease_slots=[],
            )
            requested = store.request_cancel(running.run_id, reason="stop running")
            self.assertEqual(requested.status, "running")
            self.assertFalse(
                store.cancel_requested(
                    running.run_id,
                    worker_token="wrong-generation",
                )
            )
            self.assertTrue(
                store.cancel_requested(
                    running.run_id,
                    worker_token="right-generation",
                )
            )

            terminal = store.create(
                contract=contract,
                project_root=project,
                backend="fake",
            )
            terminal = store.claim_for_execution(
                terminal.run_id,
                worker_token="terminal-generation",
                lease_slots=[],
            )
            terminal = store.update_status(
                terminal.run_id,
                "completed",
                result={},
                expected_worker_token="terminal-generation",
            )
            unchanged = store.request_cancel(terminal.run_id, reason="too late")
            self.assertEqual(unchanged.status, "completed")
            self.assertEqual(unchanged.revision, terminal.revision)
            self.assertEqual(unchanged.cancel_requested_at, 0)

    def test_cancel_fields_are_backward_compatible_and_bounded(self) -> None:
        payload = RunRecord(
            run_id="run-old",
            status="accepted",
            contract={},
            project_root="/tmp/project",
            backend="echo",
            created_at=1.0,
            updated_at=1.0,
        ).to_mapping()
        payload.pop("cancel_requested_at")
        payload.pop("cancel_reason")
        payload.pop("orchestration_id")
        payload.pop("planned_context_sha256")
        payload.pop("planned_base_head")
        payload.pop("planned_execution_profile_sha256")
        payload.pop("planned_execution_profile")
        restored = RunRecord.from_mapping(payload)
        self.assertEqual(restored.cancel_requested_at, 0)
        self.assertEqual(restored.cancel_reason, "")
        self.assertEqual(restored.orchestration_id, "")

        payload["cancel_requested_at"] = 1.0
        payload["cancel_reason"] = "x" * (MAX_CANCEL_REASON_CHARS + 1)
        with self.assertRaisesRegex(DispatchValidationError, "character limit"):
            RunRecord.from_mapping(payload)

    def test_run_state_reads_are_nofollow_bounded_and_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, contract, project = self._store_and_contract(Path(tmp))
            record = store.create(
                contract=contract,
                project_root=project,
                backend="fake",
            )
            path = store.root / f"{record.run_id}.json"
            good = path.read_bytes()
            path.unlink()
            target = Path(tmp) / "outside.json"
            target.write_bytes(good)
            os.symlink(target, path)
            with self.assertRaisesRegex(DispatchValidationError, "symbolic link"):
                store.load(record.run_id)

            path.unlink()
            path.write_bytes(b"{" + (b" " * MAX_RUN_STATE_BYTES) + b"}")
            with self.assertRaisesRegex(DispatchValidationError, "exceeds"):
                store.load(record.run_id)

            if os.name == "posix" and hasattr(os, "mkfifo"):
                path.unlink()
                os.mkfifo(path)
                errors: list[Exception] = []

                def load_fifo() -> None:
                    try:
                        store.load(record.run_id)
                    except Exception as exc:  # noqa: BLE001 - asserted below
                        errors.append(exc)

                reader = threading.Thread(target=load_fifo)
                reader.start()
                reader.join(timeout=0.5)
                was_blocked = reader.is_alive()
                if was_blocked:
                    descriptor = os.open(
                        path,
                        os.O_RDWR | getattr(os, "O_NONBLOCK", 0),
                    )
                    os.close(descriptor)
                    reader.join(timeout=1.0)
                self.assertFalse(was_blocked, "run state reader blocked on FIFO")
                self.assertFalse(reader.is_alive())
                self.assertEqual(len(errors), 1)
                self.assertIn("regular file", str(errors[0]))

    def test_deterministic_create_is_idempotent_and_conflicts_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, contract, project = self._store_and_contract(Path(tmp))
            first = store.ensure_created(
                run_id="run-batch-fixed",
                contract=contract,
                project_root=project,
                backend="fake",
                orchestration_id="batch-1",
                thread_id="reviewer",
            )
            repeated = store.ensure_created(
                run_id="run-batch-fixed",
                contract=contract,
                project_root=project,
                backend="fake",
                orchestration_id="batch-1",
                thread_id="reviewer",
            )
            self.assertEqual(repeated.to_mapping(), first.to_mapping())

            with self.assertRaisesRegex(
                DispatchValidationError,
                "conflicts with deterministic create",
            ):
                store.ensure_created(
                    run_id="run-batch-fixed",
                    contract=contract,
                    project_root=project,
                    backend="echo",
                    orchestration_id="batch-1",
                    thread_id="reviewer",
                )


class CooperativeProcessCancellationTests(unittest.TestCase):
    def test_run_bounded_cancels_a_real_long_running_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cancelled = threading.Event()
            timer = threading.Timer(0.15, cancelled.set)
            started = time.monotonic()
            timer.start()
            try:
                completed = run_bounded(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    cwd=Path(tmp),
                    timeout_seconds=10.0,
                    cancel_check=cancelled.is_set,
                )
            finally:
                timer.cancel()

            self.assertTrue(completed.cancelled)
            self.assertFalse(completed.timed_out)
            self.assertLess(time.monotonic() - started, 3.0)

    def test_supervisor_cancels_real_backend_after_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            (project / "app.py").write_text("safe = True\n", encoding="utf-8")
            supervisor = DispatchSupervisor(home=root / "home")
            adapter = _SlowSubprocessAdapter()
            outcomes: list[RunRecord] = []
            errors: list[BaseException] = []

            with patch(
                "experiments.local_agent_dispatch.supervisor.get_adapter",
                return_value=adapter,
            ):
                record = supervisor.accept(_payload(), project_root=project)

                def execute() -> None:
                    try:
                        outcomes.append(
                            supervisor.execute(record.run_id, timeout_seconds=10.0)
                        )
                    except BaseException as exc:  # pragma: no cover - asserted below
                        errors.append(exc)

                thread = threading.Thread(target=execute)
                thread.start()
                self.assertTrue(adapter.started.wait(2.0))
                supervisor.cancel(record.run_id, reason="batch stop")
                thread.join(timeout=8.0)

            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(len(outcomes), 1)
            self.assertEqual(outcomes[0].status, "cancelled")
            self.assertEqual(outcomes[0].backend_pid, 0)
            self.assertEqual((outcomes[0].result or {}).get("status"), "cancelled")

    def test_unproven_backend_cleanup_keeps_cancelled_run_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            (project / "app.py").write_text("safe = True\n", encoding="utf-8")
            supervisor = DispatchSupervisor(home=root / "home")
            adapter = _CooperativeAdapter()
            errors: list[BaseException] = []

            with patch(
                "experiments.local_agent_dispatch.supervisor.get_adapter",
                return_value=adapter,
            ):
                record = supervisor.accept(_payload(), project_root=project)

                def execute() -> None:
                    try:
                        supervisor.execute(record.run_id)
                    except BaseException as exc:  # pragma: no cover - asserted below
                        errors.append(exc)

                with patch.object(
                    supervisor.store,
                    "cleanup_backend_if_owned",
                    return_value=False,
                ):
                    thread = threading.Thread(target=execute)
                    thread.start()
                    self.assertTrue(adapter.started.wait(2.0))
                    supervisor.cancel(record.run_id)
                    thread.join(timeout=5.0)

            self.assertFalse(thread.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIn("cleanup could not be proven", str(errors[0]))
            self.assertEqual(supervisor.store.load(record.run_id).status, "running")


class AsyncWorkerEnvironmentTests(unittest.TestCase):
    def test_all_real_adapters_build_backend_scoped_worker_environments(self) -> None:
        factories = (
            codex_adapter,
            claude_adapter,
            cursor_adapter,
            opencode_adapter,
            grok_adapter,
            hermes_adapter,
            kimi_adapter,
            dsh_adapter,
            pi_adapter,
        )
        with patch.dict(
            os.environ,
            {"AWS_SECRET_ACCESS_KEY": "must-not-pass"},
            clear=True,
        ):
            for factory in factories:
                with self.subTest(adapter=factory.__name__):
                    environment = factory().worker_environment()
                    self.assertIsInstance(environment, dict)
                    self.assertNotIn("AWS_SECRET_ACCESS_KEY", environment)

        self.assertEqual(cursor_adapter().supported_modes, frozenset({"read-only"}))
        self.assertEqual(
            codex_adapter().supported_modes,
            frozenset({"read-only", "edit"}),
        )

    def test_pi_worker_only_inherits_current_default_provider_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pi_home = Path(tmp) / "pi"
            pi_home.mkdir()
            (pi_home / "settings.json").write_text(
                json.dumps(
                    {"defaultProvider": "openai", "defaultModel": "gpt-test"}
                ),
                encoding="utf-8",
            )
            (pi_home / "auth.json").write_text(
                json.dumps(
                    {
                        "openai": {"token": "selected-oauth"},
                        "anthropic": {"token": "lateral-oauth"},
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {
                    "PI_CODING_AGENT_DIR": str(pi_home),
                    "OPENAI_API_KEY": "selected-key",
                    "ANTHROPIC_API_KEY": "lateral-key",
                    "XAI_API_KEY": "lateral-xai-key",
                    "AWS_SECRET_ACCESS_KEY": "unrelated-key",
                },
                clear=True,
            ):
                adapter = pi_adapter()
                profile = adapter.execution_profile()
                environment = _worker_environment(
                    backend="pi",
                    home=Path(tmp) / "dispatch-home",
                    run_id="run-1111111111111111",
                    expected_execution_profile_sha256=(
                        adapter_execution_profile_sha256(adapter)
                    ),
                    expected_execution_profile=profile,
                )

            self.assertEqual(environment.get("OPENAI_API_KEY"), "selected-key")
            self.assertNotIn("ANTHROPIC_API_KEY", environment)
            self.assertNotIn("XAI_API_KEY", environment)
            self.assertNotIn("AWS_SECRET_ACCESS_KEY", environment)
            isolated = Path(str(environment.get("PI_CODING_AGENT_DIR")))
            self.assertNotEqual(isolated, pi_home)
            self.assertEqual(isolated.parent.name, "runs")
            self.assertEqual(
                json.loads((isolated / "auth.json").read_text()),
                {"openai": {"token": "selected-oauth"}},
            )
            self.assertEqual(
                environment.get("DYRO_LOCAL_AGENT_DISPATCH_HOME"),
                str(Path(tmp) / "dispatch-home"),
            )

    def test_hermes_worker_home_contains_only_selected_provider_auth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "hermes-source"
            source.mkdir()
            (source / "config.yaml").write_text(
                "model:\n  default: grok-test\n  provider: xai-oauth\n",
                encoding="utf-8",
            )
            (source / "auth.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "active_provider": "other-provider",
                        "providers": {
                            "xai-oauth": {"token": "selected"},
                            "other-provider": {"token": "lateral"},
                        },
                        "credential_pool": {
                            "xai-oauth": [{"token": "selected-pool"}],
                            "other-provider": [{"token": "lateral-pool"}],
                        },
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"HERMES_HOME": str(source)}, clear=True):
                adapter = hermes_adapter()
                environment = _worker_environment(
                    backend="hermes",
                    home=Path(tmp) / "dispatch-home",
                    run_id="run-2222222222222222",
                    expected_execution_profile_sha256=(
                        adapter_execution_profile_sha256(adapter)
                    ),
                    expected_execution_profile=adapter.execution_profile(),
                )

            isolated = Path(environment["HERMES_HOME"])
            self.assertNotEqual(isolated, source)
            copied = json.loads((isolated / "auth.json").read_text())
            self.assertEqual(set(copied["providers"]), {"xai-oauth"})
            self.assertEqual(set(copied["credential_pool"]), {"xai-oauth"})
            self.assertNotIn("other-provider", json.dumps(copied))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from experiments.local_agent_dispatch.adapters.base import AdapterResult
from experiments.local_agent_dispatch.adapters.subprocess_cli import (
    _completed_to_result,
    _parse_model_json,
    claude_adapter,
    codex_adapter,
)
from experiments.local_agent_dispatch.adapters.registry import get_adapter
from experiments.local_agent_dispatch.adapters.registry import probe_backends
from experiments.local_agent_dispatch.bounded_process import (
    BoundedCompletedProcess,
    _terminate_process_group,
    run_bounded,
)
from experiments.local_agent_dispatch.cli import main as dispatch_cli_main
from experiments.local_agent_dispatch.context_guard import (
    assert_files_allowed,
    guard_file,
    read_guarded_file,
)
from experiments.local_agent_dispatch.edit_workspace import EditWorkspace
from experiments.local_agent_dispatch.errors import DispatchValidationError
from experiments.local_agent_dispatch.file_lock import exclusive_file_lock
from experiments.local_agent_dispatch.fileset import collect_guarded_context
from experiments.local_agent_dispatch.gc import gc
from experiments.local_agent_dispatch.json_store import atomic_write_json, read_json
from experiments.local_agent_dispatch.lease import SlotManager
from experiments.local_agent_dispatch.process_identity import (
    process_identity_is_dead,
    process_is_alive,
    process_started_at,
)
from experiments.local_agent_dispatch.result_envelope import build_result
from experiments.local_agent_dispatch.run_store import (
    ASYNC_RESERVATION_GRACE_SECONDS,
    RunRecord,
)
import experiments.local_agent_dispatch.lease as lease_module
import experiments.local_agent_dispatch.supervisor as supervisor_module
from experiments.local_agent_dispatch.supervisor import DispatchSupervisor
from experiments.local_agent_dispatch.task_contract import parse_task_contract
from dyro.cli import _route_experiment_surface, main as dyro_main


def _payload(
    *,
    mode: str = "read-only",
    strict: bool = False,
    files: list[str] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "backend": "fake",
        "mode": mode,
        "strict": strict,
        "files": files or ["app.py"],
        "task": {
            "briefing": "Inspect the supplied file.",
            "locations": "app.py",
            "objective": "Return a bounded result.",
            "constraints": "Do not touch production actions.",
            "output_contract": "JSON summary and evidence.",
        },
    }


class _WritingAdapter:
    id = "fake"
    command = "fake"
    strict_isolation = False

    def __init__(self) -> None:
        self.calls = 0
        self.started = threading.Event()

    def available(self) -> bool:
        return True

    def run(self, *, contract, cwd, context_files, timeout_seconds):
        del contract, context_files, timeout_seconds
        self.calls += 1
        self.started.set()
        (Path(cwd) / "app.py").write_text("changed = True\n", encoding="utf-8")
        time.sleep(0.05)
        return AdapterResult(
            status="ok",
            summary="changed isolated copy",
            evidence=[
                {
                    "file": "app.py",
                    "lines": "1-1",
                    "claim": "changed the isolated copy",
                }
            ],
            confidence="high",
        )


def _init_git_project(root: Path) -> None:
    (root / "app.py").write_text("changed = False\n", encoding="utf-8")
    for argv in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "tests@example.invalid"],
        ["git", "config", "user.name", "Dyro Tests"],
        ["git", "add", "app.py"],
        ["git", "commit", "-qm", "initial"],
    ):
        subprocess.run(argv, cwd=root, check=True, capture_output=True)


class DispatchIsolationTests(unittest.TestCase):
    def test_strict_auto_backend_fails_closed_without_eligible_candidate(
        self,
    ) -> None:
        candidate = Mock(
            id="echo",
            strict_isolation=False,
        )
        candidate.available.return_value = True
        candidate.authenticated.return_value = True
        with (
            patch(
                "experiments.local_agent_dispatch.adapters.registry._all",
                return_value={"echo": candidate},
            ),
            self.assertRaisesRegex(
                DispatchValidationError,
                "isolation policy",
            ),
        ):
            get_adapter("auto", require_strict=True)

    def test_real_cli_adapters_do_not_overclaim_strict_isolation(self) -> None:
        for backend in ("codex", "claude"):
            with self.subTest(backend=backend):
                with self.assertRaisesRegex(
                    DispatchValidationError,
                    "strict isolation",
                ):
                    get_adapter(backend, require_strict=True)

    def test_edit_mode_changes_only_isolated_worktree_and_returns_patch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            _init_git_project(project)
            home = root / "dispatch-home"
            adapter = _WritingAdapter()
            supervisor = DispatchSupervisor(home=home)

            with patch(
                "experiments.local_agent_dispatch.supervisor.get_adapter",
                return_value=adapter,
            ):
                record = supervisor.accept(
                    _payload(mode="edit"),
                    project_root=project,
                )
                finished = supervisor.execute(record.run_id)

            self.assertEqual(
                (project / "app.py").read_text(encoding="utf-8"),
                "changed = False\n",
            )
            self.assertEqual(finished.status, "completed")
            patch_ref = str((finished.result or {}).get("patch_ref") or "")
            self.assertIn("#sha256=", patch_ref)
            patch_path = Path(patch_ref.split("#", 1)[0])
            self.assertTrue(patch_path.is_file())
            self.assertIn("changed = True", patch_path.read_text(encoding="utf-8"))

    def test_edit_cleanup_failure_is_reported_without_discarding_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            _init_git_project(project)
            supervisor = DispatchSupervisor(home=root / "dispatch-home")
            adapter = _WritingAdapter()
            with patch(
                "experiments.local_agent_dispatch.supervisor.get_adapter",
                return_value=adapter,
            ):
                record = supervisor.accept(
                    _payload(mode="edit"),
                    project_root=project,
                )
                with patch.object(
                    EditWorkspace,
                    "cleanup",
                    side_effect=DispatchValidationError("cleanup blocked"),
                ):
                    finished = supervisor.execute(record.run_id)

            self.assertEqual(finished.status, "completed")
            self.assertEqual(
                (project / "app.py").read_text(encoding="utf-8"),
                "changed = False\n",
            )
            warnings = list((finished.result or {}).get("warnings") or [])
            self.assertTrue(
                any("cleanup could not be completed" in item for item in warnings)
            )

    def test_edit_patch_preserves_raw_git_diff_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            patch_path = root / "patches" / "changes.patch"
            patch_path.parent.mkdir()
            workspace = EditWorkspace(
                project_root=root,
                worktree_root=root,
                patch_path=patch_path,
                _created=True,
            )
            raw_diff = b"diff --git a/data b/data\n@@ -0,0 +1 @@\n+\xff\n"
            added = BoundedCompletedProcess(
                args=("git",),
                returncode=0,
                stdout="",
                stderr="",
            )
            diffed = BoundedCompletedProcess(
                args=("git",),
                returncode=0,
                stdout=raw_diff.decode("utf-8", errors="replace"),
                stderr="",
                stdout_bytes=raw_diff,
            )
            with patch(
                "experiments.local_agent_dispatch.edit_workspace._git",
                side_effect=[added, diffed],
            ):
                patch_ref = workspace.seal_patch()

            self.assertIsNotNone(patch_ref)
            self.assertEqual(patch_path.read_bytes(), raw_diff)

    def test_failed_edit_creation_removes_new_patch_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            _init_git_project(project)
            home = root / "dispatch-home"
            from experiments.local_agent_dispatch import edit_workspace as edit_module

            real_git = edit_module._git

            def fail_worktree_add(project_root, arguments, **kwargs):
                if arguments[:2] == ["worktree", "add"]:
                    raise DispatchValidationError("simulated add failure")
                return real_git(project_root, arguments, **kwargs)

            with (
                patch.object(edit_module, "_git", side_effect=fail_worktree_add),
                self.assertRaisesRegex(DispatchValidationError, "add failure"),
            ):
                EditWorkspace.create(
                    project_root=project,
                    home=home,
                    run_id="retryable",
                )
            self.assertFalse((home / "patches" / "retryable").exists())

            workspace = EditWorkspace.create(
                project_root=project,
                home=home,
                run_id="retryable",
            )
            workspace.cleanup()

    def test_strict_mode_rejects_backend_without_physical_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            (project / "app.py").write_text("safe = True\n", encoding="utf-8")
            supervisor = DispatchSupervisor(home=root / "home")
            with patch(
                "experiments.local_agent_dispatch.supervisor.get_adapter",
                return_value=_WritingAdapter(),
            ):
                with self.assertRaisesRegex(
                    DispatchValidationError,
                    "strict isolation",
                ):
                    supervisor.accept(
                        _payload(strict=True),
                        project_root=project,
                    )


class DispatchOwnershipTests(unittest.TestCase):
    def test_equal_coarse_start_token_does_not_prove_process_dead(
        self,
    ) -> None:
        with (
            patch(
                "experiments.local_agent_dispatch.process_identity."
                "process_is_alive",
                return_value=True,
            ),
            patch(
                "experiments.local_agent_dispatch.process_identity."
                "process_state",
                return_value="S",
            ),
            patch(
                "experiments.local_agent_dispatch.process_identity."
                "process_started_at",
                return_value="Wed Jul 30 12:00:00 2026",
            ),
        ):
            self.assertFalse(
                process_identity_is_dead(
                    pid=12345,
                    started_at="Wed Jul 30 12:00:00 2026",
                )
            )

    def test_run_record_rejects_incomplete_worker_process_identity(self) -> None:
        mapping = RunRecord(
            run_id="run-1",
            status="running",
            contract={},
            project_root="/project",
            backend="echo",
            created_at=1.0,
            updated_at=1.0,
        ).to_mapping()
        for worker_pid, worker_started_at in (
            (7, ""),
            (0, "process-generation"),
        ):
            with self.subTest(
                worker_pid=worker_pid,
                worker_started_at=worker_started_at,
            ):
                malformed = dict(mapping)
                malformed["worker_pid"] = worker_pid
                malformed["worker_started_at"] = worker_started_at
                with self.assertRaisesRegex(
                    DispatchValidationError,
                    "worker process identity must be complete",
                ):
                    RunRecord.from_mapping(malformed)

    def test_only_one_worker_can_claim_a_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            (project / "app.py").write_text("safe = True\n", encoding="utf-8")
            adapter = _WritingAdapter()
            supervisor = DispatchSupervisor(home=root / "home")
            with patch(
                "experiments.local_agent_dispatch.supervisor.get_adapter",
                return_value=adapter,
            ):
                record = supervisor.accept(_payload(), project_root=project)
                outcomes: list[str] = []

                def execute() -> None:
                    try:
                        outcomes.append(supervisor.execute(record.run_id).status)
                    except DispatchValidationError:
                        outcomes.append("rejected")

                threads = [threading.Thread(target=execute) for _ in range(2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()

            self.assertEqual(adapter.calls, 1)
            self.assertCountEqual(outcomes, ["completed", "rejected"])

    def test_stale_lease_release_does_not_delete_new_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = SlotManager(home=Path(tmp), max_per_backend=1, max_global=1)
            lease = manager.acquire("fake")[0]
            lease_path = lease.slot_dir / "lease.json"
            replacement = dict(read_json(lease_path) or {})
            replacement["owner_token"] = "replacement-owner"
            atomic_write_json(lease_path, replacement)

            lease.release()

            self.assertTrue(lease.slot_dir.is_dir())
            self.assertEqual(
                (read_json(lease_path) or {}).get("owner_token"),
                "replacement-owner",
            )

    def test_release_compare_and_delete_are_serialized_by_slot_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = SlotManager(home=Path(tmp), max_per_backend=1, max_global=1)
            leases = manager.acquire("fake")
            lease = leases[0]
            lease_path = lease.slot_dir / "lease.json"
            released: list[bool] = []
            started = threading.Event()

            def release_old_owner() -> None:
                started.set()
                released.append(lease.release())

            with exclusive_file_lock(lease.lock_path):
                thread = threading.Thread(target=release_old_owner)
                thread.start()
                self.assertTrue(started.wait(1.0))
                time.sleep(0.05)
                self.assertTrue(thread.is_alive())
                replacement = dict(read_json(lease_path) or {})
                replacement["owner_token"] = "replacement-owner"
                atomic_write_json(lease_path, replacement)
            thread.join(timeout=1.0)

            self.assertEqual(released, [False])
            self.assertEqual(
                (read_json(lease_path) or {}).get("owner_token"),
                "replacement-owner",
            )
            leases[1].release()

    def test_nonfinite_lease_timestamp_cannot_block_reclaim(self) -> None:
        for renewed_at in (float("nan"), float("inf")):
            with self.subTest(renewed_at=renewed_at):
                with tempfile.TemporaryDirectory() as tmp:
                    home = Path(tmp)
                    manager = SlotManager(
                        home=home,
                        max_per_backend=1,
                        max_global=1,
                    )
                    slot_dir = home / "locks" / "backend-fake" / "slot-0"
                    slot_dir.mkdir(parents=True)
                    atomic_write_json(
                        slot_dir / "lease.json",
                        {
                            "pid": 999_999_999,
                            "started_at": "not-live",
                            "renewed_at": renewed_at,
                            "owner_token": "stale-owner",
                        },
                    )

                    leases = manager.acquire("fake")

                    self.assertNotEqual(
                        (read_json(slot_dir / "lease.json") or {}).get(
                            "owner_token"
                        ),
                        "stale-owner",
                    )
                    manager.release_all(leases)

    def test_fresh_lease_with_proven_dead_owner_is_reclaimed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            manager = SlotManager(
                home=home,
                max_per_backend=1,
                max_global=1,
            )
            slot_dir = home / "locks" / "backend-fake" / "slot-0"
            slot_dir.mkdir(parents=True)
            atomic_write_json(
                slot_dir / "lease.json",
                {
                    "pid": 999_999_999,
                    "started_at": "not-live",
                    "renewed_at": time.time(),
                    "owner_token": "dead-owner",
                },
            )

            leases = manager.acquire("fake")

            self.assertNotEqual(
                (read_json(slot_dir / "lease.json") or {}).get("owner_token"),
                "dead-owner",
            )
            manager.release_all(leases)

    def test_symlinked_lease_scope_cannot_redirect_reclaim(self) -> None:
        if os.name != "posix":
            self.skipTest("descriptor-relative symlink semantics require POSIX")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            outside = root / "outside"
            slot_dir = outside / "slot-0"
            slot_dir.mkdir(parents=True)
            victim = slot_dir / "victim.txt"
            victim.write_text("must remain\n", encoding="utf-8")
            atomic_write_json(
                slot_dir / "lease.json",
                {
                    "pid": 999_999_999,
                    "started_at": "not-live",
                    "renewed_at": time.time(),
                    "owner_token": "outside-owner",
                },
            )
            manager = SlotManager(
                home=home,
                max_per_backend=1,
                max_global=1,
            )
            scope = home / "locks" / "backend-fake"
            try:
                scope.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symbolic links unavailable: {exc}")

            with self.assertRaisesRegex(
                DispatchValidationError,
                "safely",
            ):
                manager.acquire("fake")

            self.assertEqual(victim.read_text(encoding="utf-8"), "must remain\n")
            self.assertEqual(
                (read_json(slot_dir / "lease.json") or {}).get("owner_token"),
                "outside-owner",
            )

    def test_global_scope_failure_releases_partial_backend_slot(self) -> None:
        if os.name != "posix":
            self.skipTest("descriptor-relative symlink semantics require POSIX")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            outside = root / "outside"
            outside.mkdir()
            manager = SlotManager(
                home=home,
                max_per_backend=1,
                max_global=1,
            )
            try:
                (home / "locks" / "global").symlink_to(
                    outside,
                    target_is_directory=True,
                )
            except OSError as exc:
                self.skipTest(f"symbolic links unavailable: {exc}")

            with self.assertRaisesRegex(
                DispatchValidationError,
                "safely",
            ):
                manager.acquire("fake")

            self.assertFalse(
                (home / "locks" / "backend-fake" / "slot-0").exists()
            )

    def test_non_scalar_run_id_is_quarantined_without_partial_lease(
        self,
    ) -> None:
        for malformed_run_id in ([], {}):
            with self.subTest(run_id=malformed_run_id):
                with tempfile.TemporaryDirectory() as tmp:
                    home = Path(tmp)
                    manager = SlotManager(
                        home=home,
                        max_per_backend=1,
                        max_global=1,
                    )
                    slot_dir = home / "locks" / "global" / "slot-0"
                    slot_dir.mkdir(parents=True)
                    atomic_write_json(
                        slot_dir / "lease.json",
                        {
                            "pid": 999_999_999,
                            "started_at": "not-live",
                            "renewed_at": time.time(),
                            "owner_token": "malformed-owner",
                            "run_id": malformed_run_id,
                        },
                    )

                    with self.assertRaisesRegex(
                        DispatchValidationError,
                        "no free global",
                    ):
                        manager.acquire("fake")

                    self.assertFalse(
                        (
                            home
                            / "locks"
                            / "backend-fake"
                            / "slot-0"
                        ).exists()
                    )

    def test_interrupt_after_global_lease_write_rolls_back_both_slots(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            manager = SlotManager(
                home=home,
                max_per_backend=1,
                max_global=1,
            )
            original_write = lease_module._atomic_write_json_at

            def interrupt_after_global_write(
                directory_descriptor: int,
                name: str,
                payload: dict[str, object],
            ) -> None:
                original_write(
                    directory_descriptor,
                    name,
                    payload,
                )
                if payload.get("scope") == "global":
                    raise KeyboardInterrupt

            with (
                patch.object(
                    lease_module,
                    "_atomic_write_json_at",
                    side_effect=interrupt_after_global_write,
                ),
                self.assertRaises(KeyboardInterrupt),
            ):
                manager.acquire("fake")

            self.assertFalse(
                (home / "locks" / "backend-fake" / "slot-0").exists()
            )
            self.assertFalse(
                (home / "locks" / "global" / "slot-0").exists()
            )


class ContextAndResultContractTests(unittest.TestCase):
    def test_unqualified_star_is_rejected(self) -> None:
        with self.assertRaisesRegex(DispatchValidationError, "unrestricted"):
            parse_task_contract(_payload(files=["*"]))

    def test_context_reader_rejects_unscanned_tail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            large = root / "large.txt"
            large.write_bytes(b"a" * (512 * 1024) + b"\nSECRET=tail\n")
            with self.assertRaisesRegex(DispatchValidationError, "byte limit"):
                collect_guarded_context(["large.txt"], root)

    def test_context_reader_rejects_workspace_root_as_a_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(DispatchValidationError, "regular file"):
                read_guarded_file(root, root)

    def test_context_reader_rejects_fifo_without_blocking(self) -> None:
        if os.name != "posix" or not hasattr(os, "mkfifo"):
            self.skipTest("FIFO semantics require POSIX")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fifo = root / "context.fifo"
            os.mkfifo(fifo)
            errors: list[Exception] = []

            def read_fifo() -> None:
                try:
                    read_guarded_file(fifo, root)
                except Exception as exc:  # noqa: BLE001 - asserted below
                    errors.append(exc)

            reader = threading.Thread(target=read_fifo)
            reader.start()
            reader.join(timeout=0.5)
            was_blocked = reader.is_alive()
            if was_blocked:
                unblock_descriptor = os.open(
                    fifo,
                    os.O_RDWR | getattr(os, "O_NONBLOCK", 0),
                )
                os.close(unblock_descriptor)
                reader.join(timeout=1.0)

            self.assertFalse(
                was_blocked,
                "context reader blocked while opening a FIFO",
            )
            self.assertFalse(reader.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], DispatchValidationError)
            self.assertIn("regular file", str(errors[0]))

    def test_context_reader_falls_back_without_dir_fd_support(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nested = root / "src"
            nested.mkdir()
            source = nested / "app.py"
            source.write_text("safe = True\n", encoding="utf-8")

            with patch(
                "experiments.local_agent_dispatch.context_guard.os.supports_dir_fd",
                frozenset(),
            ):
                relative, text = read_guarded_file(source, root)

            self.assertEqual(relative, "src/app.py")
            self.assertEqual(text, "safe = True\n")

    def test_context_reader_fallback_rejects_path_replacement(self) -> None:
        if os.name == "nt":
            self.skipTest("Windows does not permit replacing this open test file")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "app.py"
            source.write_text("safe = True\n", encoding="utf-8")
            replacement = root / "replacement.py"
            replacement.write_text("safe = False\n", encoding="utf-8")
            original_read = os.read
            replaced = False

            def replace_after_read(
                file_descriptor: int,
                size: int,
            ) -> bytes:
                nonlocal replaced
                chunk = original_read(file_descriptor, size)
                if not replaced:
                    replaced = True
                    replacement.replace(source)
                return chunk

            with (
                patch(
                    "experiments.local_agent_dispatch.context_guard.os.supports_dir_fd",
                    frozenset(),
                ),
                patch(
                    "experiments.local_agent_dispatch.context_guard.os.read",
                    side_effect=replace_after_read,
                ),
                self.assertRaisesRegex(DispatchValidationError, "path changed"),
            ):
                read_guarded_file(source, root)

    def test_context_reader_fallback_rejects_fifo_replacement_without_blocking(
        self,
    ) -> None:
        if os.name != "posix" or not hasattr(os, "mkfifo"):
            self.skipTest("FIFO semantics require POSIX")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "app.py"
            source.write_text("safe = True\n", encoding="utf-8")
            original_open = os.open
            replaced = False
            errors: list[Exception] = []

            def replace_before_open(path, flags, *args, **kwargs):
                nonlocal replaced
                if Path(path).name == source.name and not replaced:
                    replaced = True
                    source.unlink()
                    os.mkfifo(source)
                return original_open(path, flags, *args, **kwargs)

            def read_replaced_file() -> None:
                try:
                    read_guarded_file(source, root)
                except Exception as exc:  # noqa: BLE001 - asserted below
                    errors.append(exc)

            with (
                patch(
                    "experiments.local_agent_dispatch.context_guard."
                    "os.supports_dir_fd",
                    frozenset(),
                ),
                patch(
                    "experiments.local_agent_dispatch.context_guard.os.open",
                    side_effect=replace_before_open,
                ),
            ):
                reader = threading.Thread(target=read_replaced_file)
                reader.start()
                reader.join(timeout=0.5)
                was_blocked = reader.is_alive()
                if was_blocked:
                    unblock_descriptor = original_open(
                        source,
                        os.O_RDWR | getattr(os, "O_NONBLOCK", 0),
                    )
                    os.close(unblock_descriptor)
                    reader.join(timeout=1.0)

            self.assertFalse(
                was_blocked,
                "fallback context reader blocked on a replacement FIFO",
            )
            self.assertFalse(reader.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], DispatchValidationError)
            self.assertRegex(str(errors[0]), "changed|regular file")

    def test_context_guard_helpers_propagate_custom_byte_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "app.py"
            source.write_text("safe = True\n", encoding="utf-8")

            verdict = guard_file(source, root, max_bytes=4)
            self.assertFalse(verdict.allowed)
            self.assertIn("byte limit", verdict.reason)
            with self.assertRaisesRegex(DispatchValidationError, "byte limit"):
                assert_files_allowed([source], root, max_bytes=4)

    def test_non_json_backend_output_is_rejected(self) -> None:
        with self.assertRaisesRegex(DispatchValidationError, "JSON"):
            _parse_model_json("plain text is not the result contract")

    def test_empty_evidence_is_not_fully_verified(self) -> None:
        result = build_result(
            run_id="run-1",
            status="ok",
            summary="no evidence",
            cwd=Path.cwd(),
            evidence=[],
            confidence="low",
        )
        self.assertEqual(result.to_mapping()["verified_ratio"], 0.0)


class ProcessAndLifecycleTests(unittest.TestCase):
    def test_non_posix_supervision_fails_closed(self) -> None:
        home = Path("/unused")
        with (
            patch.object(supervisor_module.os, "name", "nt"),
            self.assertRaisesRegex(
                DispatchValidationError,
                "requires a POSIX host",
            ),
        ):
            DispatchSupervisor(home=home)

    def test_non_posix_backend_probe_does_not_spawn_cli(self) -> None:
        with (
            patch(
                "experiments.local_agent_dispatch.adapters.registry.os.name",
                "nt",
            ),
            patch(
                "experiments.local_agent_dispatch.adapters.subprocess_cli.run_bounded"
            ) as bounded,
        ):
            rows = probe_backends()
            selected = get_adapter("auto")

        bounded.assert_not_called()
        self.assertEqual(selected.id, "echo")
        by_id = {str(row["id"]): row for row in rows}
        self.assertFalse(by_id["codex"]["authenticated"])
        self.assertFalse(by_id["claude"]["authenticated"])
        self.assertTrue(by_id["echo"]["authenticated"])

    def test_codex_adapter_pins_sandbox_and_strips_unrelated_secrets(self) -> None:
        contract = parse_task_contract(
            {
                **_payload(),
                "backend": "codex",
            }
        )
        completed = BoundedCompletedProcess(
            args=("codex",),
            returncode=0,
            stdout=json.dumps(
                {
                    "summary": "bounded",
                    "confidence": "high",
                    "evidence": [],
                }
            ),
            stderr="",
        )
        with (
            patch.dict(
                "os.environ",
                {
                    "AWS_SECRET_ACCESS_KEY": "must-not-pass",
                    "ANTHROPIC_API_KEY": "claude-only",
                    "CODEX_HOME": "/tmp/codex-home",
                },
                clear=False,
            ),
            patch(
                "experiments.local_agent_dispatch.adapters.subprocess_cli.shutil.which",
                return_value="/usr/local/bin/codex",
            ),
            patch(
                "experiments.local_agent_dispatch.adapters.subprocess_cli.run_bounded",
                return_value=completed,
            ) as bounded,
        ):
            result = codex_adapter().run(
                contract=contract,
                cwd=Path.cwd(),
                context_files={"app.py": "safe = True\n"},
                timeout_seconds=10.0,
            )
        self.assertEqual(result.status, "ok")
        argv = bounded.call_args.args[0]
        self.assertIn("read-only", argv)
        self.assertIn("--ignore-user-config", argv)
        self.assertIn("--ignore-rules", argv)
        self.assertNotIn("must-not-pass", bounded.call_args.kwargs["env"].values())
        self.assertNotIn("claude-only", bounded.call_args.kwargs["env"].values())
        self.assertEqual(
            bounded.call_args.kwargs["env"].get("CODEX_HOME"),
            "/tmp/codex-home",
        )
        self.assertNotIn("safe = True", " ".join(argv))

    def test_claude_adapter_does_not_receive_codex_credentials(self) -> None:
        contract = parse_task_contract(
            {
                **_payload(),
                "backend": "claude",
            }
        )
        completed = BoundedCompletedProcess(
            args=("claude",),
            returncode=0,
            stdout=json.dumps(
                {
                    "summary": "bounded",
                    "confidence": "high",
                    "evidence": [],
                }
            ),
            stderr="",
        )
        with (
            patch.dict(
                "os.environ",
                {
                    "CODEX_HOME": "/tmp/codex-only",
                    "ANTHROPIC_API_KEY": "claude-only",
                },
                clear=False,
            ),
            patch(
                "experiments.local_agent_dispatch.adapters.subprocess_cli.shutil.which",
                return_value="/usr/local/bin/claude",
            ),
            patch(
                "experiments.local_agent_dispatch.adapters.subprocess_cli.run_bounded",
                return_value=completed,
            ) as bounded,
        ):
            result = claude_adapter().run(
                contract=contract,
                cwd=Path.cwd(),
                context_files={"app.py": "safe = True\n"},
                timeout_seconds=10.0,
            )
        self.assertEqual(result.status, "ok")
        environment = bounded.call_args.kwargs["env"]
        self.assertNotIn("CODEX_HOME", environment)
        self.assertEqual(environment.get("ANTHROPIC_API_KEY"), "claude-only")

    def test_backend_failure_does_not_persist_raw_stderr(self) -> None:
        result = _completed_to_result(
            BoundedCompletedProcess(
                args=("codex",),
                returncode=7,
                stdout="untrusted output",
                stderr="secret-token-value",
            ),
            backend="codex",
        )
        self.assertEqual(result.summary, "")
        self.assertEqual(result.raw_preview, "")
        self.assertNotIn("secret-token-value", " ".join(result.warnings))

    def test_setup_failure_terminates_spawned_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "setup-failure-survivor"
            child = (
                "import pathlib,time;"
                "time.sleep(0.5);"
                f"pathlib.Path({str(marker)!r}).write_text('bad')"
            )
            with (
                patch(
                    "experiments.local_agent_dispatch.bounded_process.os.set_blocking",
                    side_effect=OSError("simulated setup failure"),
                ),
                self.assertRaisesRegex(OSError, "setup failure"),
            ):
                run_bounded(
                    [sys.executable, "-c", child],
                    cwd=Path(tmp),
                    timeout_seconds=2.0,
                )
            time.sleep(0.8)
            self.assertFalse(marker.exists())

    def test_non_finite_timeout_is_rejected_before_spawn(self) -> None:
        for timeout in (float("nan"), float("inf"), float("-inf")):
            with (
                self.subTest(timeout=timeout),
                patch(
                    "experiments.local_agent_dispatch.bounded_process.subprocess.Popen"
                ) as popen,
                self.assertRaisesRegex(ValueError, "timeout_seconds"),
            ):
                run_bounded(
                    [sys.executable, "-c", "pass"],
                    cwd=Path.cwd(),
                    timeout_seconds=timeout,
                )
            popen.assert_not_called()

    def test_group_leader_is_not_reaped_before_kill_escalation(self) -> None:
        process = Mock()
        process.pid = 12345
        process.returncode = None
        events: list[str] = []
        process.wait.side_effect = lambda **_kwargs: events.append("wait") or 0

        def signal_group(_pid: int, sig: signal.Signals) -> None:
            events.append("term" if sig == signal.SIGTERM else "kill")

        with (
            patch(
                "experiments.local_agent_dispatch.bounded_process.os.killpg",
                side_effect=signal_group,
            ),
            patch(
                "experiments.local_agent_dispatch.bounded_process.time.sleep",
                side_effect=lambda _seconds: events.append("grace"),
            ),
        ):
            _terminate_process_group(process)

        self.assertEqual(events, ["term", "grace", "kill", "wait"])

    def test_timeout_terminates_descendant_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "descendant-survived"
            child = (
                "import subprocess,sys,time;"
                f"subprocess.Popen([sys.executable,'-c',"
                f"\"import time,pathlib;time.sleep(1);"
                f"pathlib.Path({str(marker)!r}).write_text('bad')\"]);"
                "time.sleep(10)"
            )
            completed = run_bounded(
                [sys.executable, "-c", child],
                cwd=Path(tmp),
                timeout_seconds=0.2,
            )
            self.assertTrue(completed.timed_out)
            time.sleep(1.2)
            self.assertFalse(marker.exists())

    def test_timeout_terminates_descendant_after_group_leader_exits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "orphan-survived"
            child = (
                "import subprocess,sys;"
                f"subprocess.Popen([sys.executable,'-c',"
                f"\"import time,pathlib;time.sleep(1);"
                f"pathlib.Path({str(marker)!r}).write_text('bad')\"])"
            )
            completed = run_bounded(
                [sys.executable, "-c", child],
                cwd=Path(tmp),
                timeout_seconds=0.2,
            )
            self.assertTrue(completed.timed_out)
            time.sleep(1.2)
            self.assertFalse(marker.exists())

    def test_output_limit_terminates_backend(self) -> None:
        completed = run_bounded(
            [
                sys.executable,
                "-c",
                "import os,time; os.write(1, b'x' * 1000000); time.sleep(10)",
            ],
            cwd=Path.cwd(),
            timeout_seconds=3.0,
            max_output_bytes=4096,
        )
        self.assertTrue(completed.output_limited)
        self.assertLessEqual(len(completed.stdout.encode("utf-8")), 4096)

    def test_default_cli_run_starts_async_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            (project / "app.py").write_text("safe = True\n", encoding="utf-8")
            payload = _payload()
            payload["backend"] = "echo"
            task_file = root / "task.json"
            task_file.write_text(json.dumps(payload), encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = dispatch_cli_main(
                    [
                        "--home",
                        str(root / "home"),
                        "run",
                        "--project",
                        str(project),
                        "--file",
                        str(task_file),
                    ]
                )
            self.assertEqual(exit_code, 0)
            run_id = str(json.loads(output.getvalue())["run_id"])
            finished = DispatchSupervisor(home=root / "home").wait(
                [run_id],
                timeout_seconds=10.0,
                poll_seconds=0.05,
            )[0]
            self.assertEqual(finished.status, "completed")
            log_path = root / "home" / "runs" / f"{run_id}.worker.log"
            self.assertEqual(stat.S_IMODE(log_path.stat().st_mode), 0o600)
            deadline = time.time() + 2.0
            while time.time() < deadline:
                with supervisor_module._ASYNC_WORKERS_LOCK:
                    if not supervisor_module._ASYNC_WORKERS:
                        break
                time.sleep(0.02)
            with supervisor_module._ASYNC_WORKERS_LOCK:
                self.assertFalse(supervisor_module._ASYNC_WORKERS)

    def test_async_worker_startup_cannot_be_hijacked_by_project_python(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            (project / "app.py").write_text("safe = True\n", encoding="utf-8")
            site_marker = root / "sitecustomize-ran"
            package_marker = root / "project-package-ran"
            (project / "sitecustomize.py").write_text(
                "from pathlib import Path\n"
                f"Path({str(site_marker)!r}).write_text('bad')\n",
                encoding="utf-8",
            )
            shadow_package = project / "experiments" / "local_agent_dispatch"
            shadow_package.mkdir(parents=True)
            (project / "experiments" / "__init__.py").write_text(
                "",
                encoding="utf-8",
            )
            (shadow_package / "__init__.py").write_text("", encoding="utf-8")
            (shadow_package / "__main__.py").write_text(
                "from pathlib import Path\n"
                f"Path({str(package_marker)!r}).write_text('bad')\n",
                encoding="utf-8",
            )
            payload = _payload()
            payload["backend"] = "echo"
            task_file = root / "task.json"
            task_file.write_text(json.dumps(payload), encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = dispatch_cli_main(
                    [
                        "--home",
                        str(root / "home"),
                        "run",
                        "--project",
                        str(project),
                        "--file",
                        str(task_file),
                    ]
                )
            self.assertEqual(exit_code, 0)
            run_id = str(json.loads(output.getvalue())["run_id"])
            finished = DispatchSupervisor(home=root / "home").wait(
                [run_id],
                timeout_seconds=10.0,
                poll_seconds=0.05,
            )[0]
            self.assertEqual(finished.status, "completed")
            self.assertFalse(site_marker.exists())
            self.assertFalse(package_marker.exists())
            deadline = time.time() + 2.0
            while time.time() < deadline:
                with supervisor_module._ASYNC_WORKERS_LOCK:
                    if not supervisor_module._ASYNC_WORKERS:
                        break
                time.sleep(0.02)
            with supervisor_module._ASYNC_WORKERS_LOCK:
                self.assertFalse(supervisor_module._ASYNC_WORKERS)

    def test_worker_failure_before_claim_terminalizes_accepted_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            (project / "app.py").write_text("safe = True\n", encoding="utf-8")
            home = root / "home"
            supervisor = DispatchSupervisor(home=home)
            with patch(
                "experiments.local_agent_dispatch.supervisor.get_adapter",
                return_value=_WritingAdapter(),
            ):
                record = supervisor.accept(_payload(), project_root=project)

            errors = io.StringIO()
            with (
                patch.object(
                    DispatchSupervisor,
                    "execute",
                    side_effect=DispatchValidationError("no free slot"),
                ),
                redirect_stderr(errors),
            ):
                exit_code = dispatch_cli_main(
                    [
                        "--home",
                        str(home),
                        "worker",
                        record.run_id,
                    ]
                )

            self.assertEqual(exit_code, 2)
            failed = supervisor.store.load(record.run_id)
            self.assertEqual(failed.status, "failed")
            self.assertIn("no free slot", failed.error)

    def test_worker_cli_preserves_running_when_backend_cleanup_is_unproven(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            (project / "app.py").write_text("safe = True\n", encoding="utf-8")
            home = root / "home"
            supervisor = DispatchSupervisor(home=home)
            with patch(
                "experiments.local_agent_dispatch.supervisor.get_adapter",
                return_value=_WritingAdapter(),
            ):
                record = supervisor.accept(_payload(), project_root=project)
            supervisor.store.reserve_async_worker(
                record.run_id,
                worker_token="tracked-generation",
            )
            supervisor.store.claim_for_execution(
                record.run_id,
                worker_token="tracked-generation",
                lease_slots=[],
            )

            with (
                patch.object(
                    DispatchSupervisor,
                    "execute",
                    side_effect=DispatchValidationError(
                        "backend cleanup could not be proven"
                    ),
                ),
                redirect_stderr(io.StringIO()),
            ):
                exit_code = dispatch_cli_main(
                    [
                        "--home",
                        str(home),
                        "worker",
                        record.run_id,
                        "--worker-token",
                        "tracked-generation",
                    ]
                )

            self.assertEqual(exit_code, 2)
            self.assertEqual(
                supervisor.store.load(record.run_id).status,
                "running",
            )

    def test_async_reaper_terminalizes_exact_running_worker_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            (project / "app.py").write_text("safe = True\n", encoding="utf-8")
            supervisor = DispatchSupervisor(home=root / "home")
            with patch(
                "experiments.local_agent_dispatch.supervisor.get_adapter",
                return_value=_WritingAdapter(),
            ):
                record = supervisor.accept(_payload(), project_root=project)
            supervisor.store.reserve_async_worker(
                record.run_id,
                worker_token="spawn-generation",
            )
            supervisor.store.claim_for_execution(
                record.run_id,
                worker_token="spawn-generation",
                lease_slots=[],
            )
            process = Mock()
            process.wait.return_value = 137

            supervisor_module._reap_async_worker(
                process,
                store=supervisor.store,
                run_id=record.run_id,
                worker_token="spawn-generation",
            )

            failed = supervisor.store.load(record.run_id)
            self.assertEqual(failed.status, "failed")
            self.assertIn("exit_code=137", failed.error)

    def test_async_execute_rejects_caller_supplied_worker_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            (project / "app.py").write_text("safe = True\n", encoding="utf-8")
            supervisor = DispatchSupervisor(home=root / "home")
            with patch(
                "experiments.local_agent_dispatch.supervisor.get_adapter",
                return_value=_WritingAdapter(),
            ):
                record = supervisor.accept(_payload(), project_root=project)

            with self.assertRaisesRegex(
                DispatchValidationError,
                "only supported for synchronous",
            ):
                supervisor.execute(
                    record.run_id,
                    sync=False,
                    worker_token="caller-generation",
                )

            current = supervisor.store.load(record.run_id)
            self.assertEqual(current.status, "accepted")
            self.assertEqual(current.worker_token, "")

    def test_backend_record_rejects_pid_pgid_identity_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            (project / "app.py").write_text("safe = True\n", encoding="utf-8")
            supervisor = DispatchSupervisor(home=root / "home")
            with patch(
                "experiments.local_agent_dispatch.supervisor.get_adapter",
                return_value=_WritingAdapter(),
            ):
                record = supervisor.accept(_payload(), project_root=project)
            payload = record.to_mapping()
            payload.update(
                {
                    "backend_pid": 12345,
                    "backend_pgid": 54321,
                    "backend_started_at": "stable-generation",
                    "backend_lock_path": str(
                        root / "home" / "runs" / "backend.lifetime"
                    ),
                }
            )

            with self.assertRaisesRegex(
                DispatchValidationError,
                "dedicated process group",
            ):
                RunRecord.from_mapping(payload)

    def test_backend_binding_validates_run_id_before_lifetime_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = supervisor_module.RunStore(Path(tmp) / "home")

            with self.assertRaisesRegex(
                DispatchValidationError,
                "invalid run_id",
            ):
                store.bind_backend_process(
                    "../outside",
                    worker_token="generation",
                    backend_pid=12345,
                    backend_pgid=12345,
                    backend_started_at="stable-generation",
                )

    def test_backend_cleanup_fails_closed_without_posix_process_groups(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = supervisor_module.RunStore(Path(tmp) / "home")

            with (
                patch(
                    "experiments.local_agent_dispatch.run_store."
                    "_POSIX_PROCESS_GROUPS",
                    False,
                ),
                self.assertRaisesRegex(
                    DispatchValidationError,
                    "requires POSIX process groups",
                ),
            ):
                store.cleanup_backend_if_owned(
                    "run-any",
                    worker_token="generation",
                )

    def test_reaper_cannot_terminalize_unproven_backend_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            (project / "app.py").write_text("safe = True\n", encoding="utf-8")
            supervisor = DispatchSupervisor(home=root / "home")
            with patch(
                "experiments.local_agent_dispatch.supervisor.get_adapter",
                return_value=_WritingAdapter(),
            ):
                record = supervisor.accept(_payload(), project_root=project)
            supervisor.store.reserve_async_worker(
                record.run_id,
                worker_token="spawn-generation",
            )
            supervisor.store.claim_for_execution(
                record.run_id,
                worker_token="spawn-generation",
                lease_slots=[],
            )
            process = Mock()
            process.wait.return_value = 137

            with patch.object(
                supervisor.store,
                "cleanup_backend_if_owned",
                return_value=False,
            ):
                supervisor_module._reap_async_worker(
                    process,
                    store=supervisor.store,
                    run_id=record.run_id,
                    worker_token="spawn-generation",
                )

            self.assertEqual(
                supervisor.store.load(record.run_id).status,
                "running",
            )

    def test_stale_async_reaper_cannot_fail_new_worker_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            (project / "app.py").write_text("safe = True\n", encoding="utf-8")
            supervisor = DispatchSupervisor(home=root / "home")
            with patch(
                "experiments.local_agent_dispatch.supervisor.get_adapter",
                return_value=_WritingAdapter(),
            ):
                record = supervisor.accept(_payload(), project_root=project)
            supervisor.store.reserve_async_worker(
                record.run_id,
                worker_token="new-generation",
            )
            process = Mock()
            process.wait.return_value = 137

            supervisor_module._reap_async_worker(
                process,
                store=supervisor.store,
                run_id=record.run_id,
                worker_token="stale-generation",
            )

            current = supervisor.store.load(record.run_id)
            self.assertEqual(current.status, "accepted")
            self.assertEqual(current.worker_token, "new-generation")

    def test_stale_async_reservation_is_durably_reconciled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            (project / "app.py").write_text("safe = True\n", encoding="utf-8")
            supervisor = DispatchSupervisor(home=root / "home")
            with patch(
                "experiments.local_agent_dispatch.supervisor.get_adapter",
                return_value=_WritingAdapter(),
            ):
                record = supervisor.accept(_payload(), project_root=project)
            reserved = supervisor.store.reserve_async_worker(
                record.run_id,
                worker_token="launcher-generation",
            )

            with patch(
                "experiments.local_agent_dispatch.run_store.time.time",
                return_value=(
                    reserved.updated_at
                    + ASYNC_RESERVATION_GRACE_SECONDS
                    + 1
                ),
            ):
                reconciled = supervisor.store.reconcile_orphaned_workers(
                    run_ids={record.run_id}
                )

            failed = supervisor.store.load(record.run_id)
            self.assertEqual(reconciled, [record.run_id])
            self.assertEqual(failed.status, "failed")
            self.assertIn("reservation expired", failed.error)

    def test_startup_timeout_terminates_detached_process_group(self) -> None:
        process = Mock()
        process.pid = 12345
        process.returncode = None
        process.wait.return_value = 0
        with (
            patch(
                "experiments.local_agent_dispatch.supervisor.os.killpg"
            ) as killpg,
            patch("experiments.local_agent_dispatch.supervisor.time.sleep"),
        ):
            supervisor_module._terminate_async_worker(process)

        self.assertEqual(
            killpg.call_args_list,
            [
                unittest.mock.call(12345, signal.SIGTERM),
                unittest.mock.call(12345, signal.SIGKILL),
            ],
        )

    def test_startup_termination_kills_group_after_leader_exits(self) -> None:
        process = Mock()
        process.pid = 12345
        process.returncode = None
        process.wait.return_value = 0

        def leader_exits_after_term(_seconds: float) -> None:
            process.returncode = 0

        with (
            patch(
                "experiments.local_agent_dispatch.supervisor.os.killpg"
            ) as killpg,
            patch(
                "experiments.local_agent_dispatch.supervisor.time.sleep",
                side_effect=leader_exits_after_term,
            ),
        ):
            supervisor_module._terminate_async_worker(process)

        self.assertEqual(process.returncode, 0)
        self.assertEqual(
            killpg.call_args_list,
            [
                unittest.mock.call(12345, signal.SIGTERM),
                unittest.mock.call(12345, signal.SIGKILL),
            ],
        )

    def test_startup_termination_never_signals_an_already_reaped_pid(self) -> None:
        process = Mock()
        process.pid = 12345
        process.returncode = 0
        with patch(
            "experiments.local_agent_dispatch.supervisor.os.killpg"
        ) as killpg:
            supervisor_module._terminate_async_worker(process)

        killpg.assert_not_called()

    def test_reaper_start_failure_terminates_worker_before_terminal_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            (project / "app.py").write_text("safe = True\n", encoding="utf-8")
            supervisor = DispatchSupervisor(home=root / "home")
            with patch(
                "experiments.local_agent_dispatch.supervisor.get_adapter",
                return_value=_WritingAdapter(),
            ):
                record = supervisor.accept(_payload(), project_root=project)
            process = Mock()
            process.pid = 12345
            process.poll.return_value = None

            def launch_process(*_args, **_kwargs):
                reserved = supervisor.store.load(record.run_id)
                supervisor.store.claim_for_execution(
                    record.run_id,
                    worker_token=reserved.worker_token,
                    lease_slots=[],
                )
                return process

            with (
                patch(
                    "experiments.local_agent_dispatch.supervisor.subprocess.Popen",
                    side_effect=launch_process,
                ),
                patch(
                    "experiments.local_agent_dispatch.supervisor.threading.Thread.start",
                    side_effect=RuntimeError("thread unavailable"),
                ),
                patch(
                    "experiments.local_agent_dispatch.supervisor."
                    "_terminate_async_worker"
                ) as terminate,
            ):
                failed = supervisor.spawn_worker(record.run_id)

            terminate.assert_called_once_with(process)
            self.assertEqual(failed.status, "failed")
            self.assertIn("thread unavailable", failed.error)
            self.assertNotIn(process, supervisor_module._ASYNC_WORKERS)

    def test_startup_timeout_has_no_concurrent_reaper_waiter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            (project / "app.py").write_text("safe = True\n", encoding="utf-8")
            supervisor = DispatchSupervisor(home=root / "home")
            with patch(
                "experiments.local_agent_dispatch.supervisor.get_adapter",
                return_value=_WritingAdapter(),
            ):
                record = supervisor.accept(_payload(), project_root=project)
            process = Mock()
            process.pid = 12345

            with (
                patch(
                    "experiments.local_agent_dispatch.supervisor.subprocess.Popen",
                    return_value=process,
                ),
                patch(
                    "experiments.local_agent_dispatch.supervisor."
                    "ASYNC_STARTUP_TIMEOUT_SECONDS",
                    0.0,
                ),
                patch(
                    "experiments.local_agent_dispatch.supervisor."
                    "_start_async_reaper"
                ) as start_reaper,
                patch(
                    "experiments.local_agent_dispatch.supervisor."
                    "_terminate_async_worker"
                ) as terminate,
            ):
                failed = supervisor.spawn_worker(record.run_id)

            self.assertEqual(failed.status, "failed")
            start_reaper.assert_not_called()
            terminate.assert_called_once_with(process)

    def test_startup_poll_exit_reaps_worker_that_claimed_after_load(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            (project / "app.py").write_text("safe = True\n", encoding="utf-8")
            supervisor = DispatchSupervisor(home=root / "home")
            with patch(
                "experiments.local_agent_dispatch.supervisor.get_adapter",
                return_value=_WritingAdapter(),
            ):
                record = supervisor.accept(_payload(), project_root=project)
            process = Mock()
            process.pid = 12345
            process.returncode = 137
            process.wait.return_value = 137

            def claim_bind_and_exit():
                reserved = supervisor.store.load(record.run_id)
                supervisor.store.claim_for_execution(
                    record.run_id,
                    worker_token=reserved.worker_token,
                    lease_slots=[],
                )
                running = supervisor.store.load(record.run_id)
                running.backend_pid = 12345
                running.backend_pgid = 12345
                running.backend_started_at = "stable-generation"
                running.backend_lock_path = str(
                    root / "home" / "runs" / "backend.lifetime"
                )
                supervisor.store.save(running)
                return 137

            def prove_cleanup(*_args, **_kwargs) -> bool:
                running = supervisor.store.load(record.run_id)
                running.backend_pid = 0
                running.backend_pgid = 0
                running.backend_started_at = ""
                running.backend_lock_path = ""
                supervisor.store.save(running)
                return True

            process.poll.side_effect = claim_bind_and_exit
            with (
                patch(
                    "experiments.local_agent_dispatch.supervisor.subprocess.Popen",
                    return_value=process,
                ),
                patch.object(
                    supervisor.store,
                    "cleanup_backend_if_owned",
                    side_effect=prove_cleanup,
                ) as cleanup,
                patch(
                    "experiments.local_agent_dispatch.supervisor."
                    "_start_async_reaper"
                ) as start_reaper,
            ):
                failed = supervisor.spawn_worker(record.run_id)

            cleanup.assert_called_once()
            start_reaper.assert_not_called()
            self.assertEqual(failed.status, "failed")
            self.assertEqual(failed.backend_pid, 0)
            self.assertNotIn(process, supervisor_module._ASYNC_WORKERS)

    def test_new_supervisor_reconciles_worker_killed_after_parent_cli_exit(
        self,
    ) -> None:
        if (
            os.name != "posix"
            or not hasattr(os, "getpgid")
            or not hasattr(signal, "SIGKILL")
        ):
            self.skipTest("process-group reconciliation requires POSIX")
        if shutil.which("python3") is None:
            self.skipTest("python3 shebang interpreter is unavailable")
        if process_started_at() is None:
            self.skipTest("stable process-generation identity is unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            (project / "app.py").write_text("safe = True\n", encoding="utf-8")
            home = root / "home"
            fake_bin = root / "bin"
            fake_bin.mkdir()
            backend_pid_path = root / "backend.pid"
            fake_codex = fake_bin / "codex"
            fake_codex.write_text(
                "#!/usr/bin/env python3\n"
                "import os\n"
                "from pathlib import Path\n"
                "import sys\n"
                "import time\n"
                "if sys.argv[1:2] == ['login']:\n"
                "    raise SystemExit(0)\n"
                "for descriptor in range(3, 256):\n"
                "    try:\n"
                "        os.close(descriptor)\n"
                "    except OSError:\n"
                "        pass\n"
                f"Path({str(backend_pid_path)!r}).write_text(str(os.getpid()))\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )
            fake_codex.chmod(0o755)
            payload = _payload()
            payload["backend"] = "codex"
            task_file = root / "task.json"
            task_file.write_text(json.dumps(payload), encoding="utf-8")
            environment = dict(os.environ)
            environment["PATH"] = (
                f"{fake_bin}{os.pathsep}{environment.get('PATH', '')}"
            )
            worker_pid = 0
            backend_pid = 0
            try:
                launched = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "experiments.local_agent_dispatch",
                        "--home",
                        str(home),
                        "run",
                        "--project",
                        str(project),
                        "--file",
                        str(task_file),
                    ],
                    cwd=Path(__file__).resolve().parents[1],
                    env=environment,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(
                    launched.returncode,
                    0,
                    f"stdout={launched.stdout!r} stderr={launched.stderr!r}",
                )
                launch_result = json.loads(launched.stdout)
                self.assertEqual(launch_result["status"], "running")
                run_id = str(launch_result["run_id"])
                store = supervisor_module.RunStore(home)
                running = store.load(run_id)
                worker_pid = running.worker_pid
                self.assertGreater(worker_pid, 0)
                backend_deadline = time.time() + 2.0
                while time.time() < backend_deadline:
                    running = store.load(run_id)
                    if backend_pid_path.is_file() and running.backend_pid > 0:
                        break
                    time.sleep(0.02)
                worker_log = (
                    home / "runs" / f"{run_id}.worker.log"
                ).read_text(encoding="utf-8")
                self.assertTrue(
                    backend_pid_path.is_file(),
                    f"record={running.to_mapping()!r} log={worker_log!r}",
                )
                backend_pid = int(
                    backend_pid_path.read_text(encoding="utf-8").strip()
                )
                self.assertNotEqual(running.backend_pid, backend_pid)
                self.assertEqual(
                    running.backend_pgid,
                    running.backend_pid,
                )
                self.assertEqual(
                    os.getpgid(backend_pid),
                    running.backend_pgid,
                )
                self.assertTrue(process_is_alive(backend_pid))

                os.kill(worker_pid, signal.SIGKILL)
                deadline = time.time() + 3.0
                while time.time() < deadline:
                    if process_identity_is_dead(
                        pid=worker_pid,
                        started_at=running.worker_started_at,
                    ):
                        break
                    time.sleep(0.02)
                self.assertTrue(
                    process_identity_is_dead(
                        pid=worker_pid,
                        started_at=running.worker_started_at,
                    )
                )

                DispatchSupervisor(home=home)

                failed = store.load(run_id)
                self.assertEqual(failed.status, "failed")
                self.assertIn("worker process exited", failed.error)
                self.assertEqual(failed.backend_pid, 0)
                self.assertEqual(failed.backend_pgid, 0)
                self.assertFalse(
                    (
                        home
                        / "runs"
                        / f"{run_id}.backend.lifetime"
                    ).exists()
                )
                backend_deadline = time.time() + 2.0
                while time.time() < backend_deadline:
                    if not process_is_alive(backend_pid):
                        break
                    time.sleep(0.02)
                self.assertFalse(
                    process_is_alive(backend_pid),
                    "reconciliation left the backend process alive",
                )
            finally:
                if worker_pid:
                    try:
                        os.kill(worker_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if backend_pid:
                    try:
                        os.kill(backend_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_heartbeat_failure_does_not_mask_worker_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            (project / "app.py").write_text("safe = True\n", encoding="utf-8")
            supervisor = DispatchSupervisor(home=root / "home")
            with patch(
                "experiments.local_agent_dispatch.supervisor.get_adapter",
                return_value=_WritingAdapter(),
            ):
                record = supervisor.accept(_payload(), project_root=project)
            with (
                patch.object(
                    supervisor,
                    "_run_worker",
                    side_effect=RuntimeError("primary worker failure"),
                ),
                patch(
                    "experiments.local_agent_dispatch.supervisor.LeaseHeartbeat.start"
                ),
                patch(
                    "experiments.local_agent_dispatch.supervisor.LeaseHeartbeat.stop",
                    side_effect=RuntimeError("heartbeat failure"),
                ),
                self.assertRaisesRegex(RuntimeError, "primary worker failure"),
            ):
                supervisor.execute(record.run_id)
            failed = supervisor.store.load(record.run_id)
            self.assertEqual(failed.status, "failed")
            self.assertIn("primary worker failure", failed.error)

    def test_adapter_failure_is_preserved_when_heartbeat_cleanup_fails(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            (project / "app.py").write_text("safe = True\n", encoding="utf-8")
            supervisor = DispatchSupervisor(home=root / "home")
            adapter = _WritingAdapter()
            original_stop = supervisor_module.LeaseHeartbeat.stop

            def failing_stop(heartbeat) -> None:
                original_stop(heartbeat)
                raise RuntimeError("heartbeat cleanup failure")

            with patch(
                "experiments.local_agent_dispatch.supervisor.get_adapter",
                return_value=adapter,
            ):
                record = supervisor.accept(_payload(), project_root=project)
                with (
                    patch.object(
                        adapter,
                        "run",
                        side_effect=RuntimeError("primary adapter failure"),
                    ),
                    patch.object(
                        supervisor_module.LeaseHeartbeat,
                        "stop",
                        failing_stop,
                    ),
                    self.assertRaisesRegex(
                        DispatchValidationError,
                        "primary adapter failure.*heartbeat cleanup failure",
                    ),
                ):
                    supervisor.execute(record.run_id)

            failed = supervisor.store.load(record.run_id)
            self.assertEqual(failed.status, "failed")
            self.assertIn("primary adapter failure", failed.error)
            self.assertNotIn("heartbeat cleanup failure", failed.error)
            warnings = list((failed.result or {}).get("warnings") or [])
            self.assertTrue(
                any(
                    "heartbeat cleanup failure" in warning
                    for warning in warnings
                )
            )

    def test_heartbeat_failure_interrupts_active_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            (project / "app.py").write_text("safe = True\n", encoding="utf-8")
            supervisor = DispatchSupervisor(home=root / "home")
            original_init = supervisor_module.LeaseHeartbeat.__init__
            cleanup_called = threading.Event()
            callback_observed: list[bool] = []

            def quick_heartbeat_init(
                heartbeat,
                leases,
                *,
                on_failure=None,
            ) -> None:
                original_init(
                    heartbeat,
                    leases,
                    interval_seconds=0.02,
                    on_failure=on_failure,
                )

            def cleanup_backend(*_args, **_kwargs) -> bool:
                cleanup_called.set()
                return True

            def wait_for_heartbeat(*_args, **_kwargs):
                callback_observed.append(cleanup_called.wait(timeout=0.5))
                return supervisor_module._WorkerOutcome(
                    status="completed",
                    result={"status": "ok"},
                )

            with (
                patch(
                    "experiments.local_agent_dispatch.supervisor.get_adapter",
                    return_value=_WritingAdapter(),
                ),
            ):
                record = supervisor.accept(_payload(), project_root=project)
            with (
                patch.object(
                    supervisor_module.LeaseHeartbeat,
                    "__init__",
                    quick_heartbeat_init,
                ),
                patch(
                    "experiments.local_agent_dispatch.lease.SlotLease.renew",
                    side_effect=RuntimeError("renewal unavailable"),
                ),
                patch.object(
                    supervisor.store,
                    "cleanup_backend_if_owned",
                    side_effect=cleanup_backend,
                ),
                patch.object(
                    supervisor,
                    "_run_worker",
                    side_effect=wait_for_heartbeat,
                ),
            ):
                with self.assertRaisesRegex(
                    DispatchValidationError,
                    "lease heartbeat failed",
                ):
                    supervisor.execute(record.run_id, timeout_seconds=10.0)

            self.assertEqual(callback_observed, [True])
            failed = supervisor.store.load(record.run_id)
            self.assertEqual(failed.status, "failed")
            self.assertEqual(failed.backend_pid, 0)

    def test_unproven_backend_cleanup_retains_capacity_leases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            (project / "app.py").write_text("safe = True\n", encoding="utf-8")
            supervisor = DispatchSupervisor(
                home=root / "home",
                max_per_backend=1,
                max_global=1,
            )
            with patch(
                "experiments.local_agent_dispatch.supervisor.get_adapter",
                return_value=_WritingAdapter(),
            ):
                record = supervisor.accept(_payload(), project_root=project)
                with (
                    patch.object(
                        supervisor.store,
                        "cleanup_backend_if_owned",
                        return_value=False,
                    ),
                    self.assertRaisesRegex(
                        DispatchValidationError,
                        "cleanup could not be proven",
                    ),
                ):
                    supervisor.execute(record.run_id)

            running = supervisor.store.load(record.run_id)
            self.assertEqual(running.status, "running")
            self.assertEqual(len(running.lease_slots or []), 2)
            self.assertTrue(
                all(Path(path).is_dir() for path in running.lease_slots or [])
            )
            with self.assertRaisesRegex(
                DispatchValidationError,
                "no free slot",
            ):
                supervisor.slots.acquire("fake")
            for path in running.lease_slots or []:
                lease_path = Path(path) / "lease.json"
                payload = dict(read_json(lease_path) or {})
                self.assertEqual(payload.get("run_id"), running.run_id)
                payload["pid"] = 999_999_999
                payload["started_at"] = "exited-generation"
                payload["renewed_at"] = time.time()
                atomic_write_json(lease_path, payload)
            run_path = (
                root
                / "home"
                / "runs"
                / f"{running.run_id}.json"
            )
            run_payload = dict(read_json(run_path) or {})
            run_payload["worker_pid"] = 999_999_999
            run_payload["worker_started_at"] = "exited-generation"
            atomic_write_json(run_path, run_payload)
            with self.assertRaisesRegex(
                DispatchValidationError,
                "no free slot",
            ):
                supervisor.slots.acquire("fake")
            for path in running.lease_slots or []:
                lease_path = Path(path) / "lease.json"
                payload = dict(read_json(lease_path) or {})
                payload.pop("run_id", None)
                atomic_write_json(lease_path, payload)
            with self.assertRaisesRegex(
                DispatchValidationError,
                "no free slot",
            ):
                supervisor.slots.acquire("fake")

    def test_lifecycle_cleanup_failure_revokes_completed_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            (project / "app.py").write_text("safe = True\n", encoding="utf-8")
            supervisor = DispatchSupervisor(home=root / "home")
            with patch(
                "experiments.local_agent_dispatch.supervisor.get_adapter",
                return_value=_WritingAdapter(),
            ):
                record = supervisor.accept(_payload(), project_root=project)
                cleanup_started = threading.Event()
                allow_cleanup = threading.Event()
                errors: list[Exception] = []

                def failing_stop(_heartbeat) -> None:
                    cleanup_started.set()
                    self.assertTrue(allow_cleanup.wait(2.0))
                    raise RuntimeError("heartbeat cleanup failure")

                def execute() -> None:
                    try:
                        supervisor.execute(record.run_id)
                    except Exception as exc:  # noqa: BLE001 - asserted below
                        errors.append(exc)

                with (
                    patch(
                        "experiments.local_agent_dispatch.supervisor.LeaseHeartbeat.stop",
                        failing_stop,
                    ),
                ):
                    thread = threading.Thread(target=execute)
                    thread.start()
                    self.assertTrue(cleanup_started.wait(2.0))
                    self.assertEqual(
                        supervisor.store.load(record.run_id).status,
                        "running",
                    )
                    allow_cleanup.set()
                    thread.join(timeout=3.0)

            self.assertFalse(thread.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], RuntimeError)
            self.assertIn("heartbeat cleanup failure", str(errors[0]))
            failed = supervisor.store.load(record.run_id)
            self.assertEqual(failed.status, "failed")
            self.assertIn("lifecycle cleanup failed", failed.error)

    def test_terminal_status_is_not_visible_until_lifecycle_cleanup_finishes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            (project / "app.py").write_text("safe = True\n", encoding="utf-8")
            supervisor = DispatchSupervisor(home=root / "home")
            adapter = _WritingAdapter()
            with patch(
                "experiments.local_agent_dispatch.supervisor.get_adapter",
                return_value=adapter,
            ):
                record = supervisor.accept(_payload(), project_root=project)
                cleanup_started = threading.Event()
                allow_cleanup = threading.Event()
                original_stop = supervisor_module.LeaseHeartbeat.stop

                def blocked_stop(heartbeat) -> None:
                    cleanup_started.set()
                    self.assertTrue(allow_cleanup.wait(2.0))
                    original_stop(heartbeat)

                outcomes: list[object] = []
                with patch.object(
                    supervisor_module.LeaseHeartbeat,
                    "stop",
                    blocked_stop,
                ):
                    thread = threading.Thread(
                        target=lambda: outcomes.append(
                            supervisor.execute(record.run_id)
                        )
                    )
                    thread.start()
                    self.assertTrue(cleanup_started.wait(2.0))
                    self.assertEqual(
                        supervisor.store.load(record.run_id).status,
                        "running",
                    )
                    allow_cleanup.set()
                    thread.join(timeout=3.0)

            self.assertFalse(thread.is_alive())
            self.assertEqual(len(outcomes), 1)
            final_record = supervisor.store.load(record.run_id)
            self.assertEqual(
                final_record.status,
                "completed",
                final_record.error,
            )

    def test_terminal_status_cannot_be_reversed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            (project / "app.py").write_text("safe = True\n", encoding="utf-8")
            supervisor = DispatchSupervisor(home=root / "home")
            with patch(
                "experiments.local_agent_dispatch.supervisor.get_adapter",
                return_value=_WritingAdapter(),
            ):
                record = supervisor.accept(_payload(), project_root=project)
                finished = supervisor.execute(record.run_id)

            with self.assertRaisesRegex(
                DispatchValidationError,
                "terminal run status cannot be changed",
            ):
                supervisor.store.update_status(
                    finished.run_id,
                    "failed",
                )

    def test_top_level_dry_run_routes_to_dispatch_without_state_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            (project / "app.py").write_text("safe = True\n", encoding="utf-8")
            payload = _payload()
            payload["backend"] = "echo"
            task_file = root / "task.json"
            task_file.write_text(json.dumps(payload), encoding="utf-8")
            state_home = root / "state"
            output = io.StringIO()
            with (
                patch.dict(
                    "os.environ",
                    {"DYRO_LOCAL_AGENT_DISPATCH_HOME": str(state_home)},
                    clear=False,
                ),
                redirect_stdout(output),
                self.assertRaises(SystemExit) as raised,
            ):
                dyro_main(
                    [
                        "--dry-run",
                        "--root",
                        str(project),
                        "dispatch",
                        "run",
                        "--file",
                        str(task_file),
                    ]
                )
            self.assertEqual(raised.exception.code, 0)
            self.assertTrue(json.loads(output.getvalue())["dry_run"])
            self.assertFalse(state_home.exists())

    def test_top_level_root_does_not_override_equals_project_argument(self) -> None:
        surface, forwarded = _route_experiment_surface(
            [
                "--root",
                "/global-project",
                "dispatch",
                "--home",
                "/dispatch-state",
                "run",
                "--project=/explicit-project",
                "--file",
                "task.json",
            ]
        ) or ("", [])
        self.assertEqual(surface, "dispatch")
        self.assertIn("--project=/explicit-project", forwarded)
        self.assertNotIn("/global-project", forwarded)

    def test_top_level_root_is_injected_after_dispatch_home_option(self) -> None:
        for command in ("run", "panel"):
            with self.subTest(command=command):
                surface, forwarded = _route_experiment_surface(
                    [
                        "--dry-run",
                        "--root",
                        "/global-project",
                        "dispatch",
                        "--home",
                        "/dispatch-state",
                        command,
                        "--stdin",
                    ]
                ) or ("", [])
                self.assertEqual(surface, "dispatch")
                self.assertIn("--home", forwarded)
                self.assertIn(command, forwarded)
                project_index = forwarded.index("--project")
                self.assertEqual(
                    forwarded[project_index + 1],
                    "/global-project",
                )

    def test_dry_run_read_surfaces_do_not_create_state_or_run_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_home = Path(tmp) / "absent-state"
            commands = (
                ["doctor"],
                ["result", "missing-run"],
                ["route", "list"],
                ["skill-render"],
                ["gc"],
                ["worker", "missing-run"],
            )
            with (
                patch(
                    "experiments.local_agent_dispatch.cli.probe_backends",
                    return_value=[],
                ),
                patch(
                    "experiments.local_agent_dispatch.skill_render.probe_backends",
                    return_value=[],
                ),
                patch.object(
                    DispatchSupervisor,
                    "execute",
                ) as execute,
            ):
                for command in commands:
                    with (
                        self.subTest(command=command),
                        redirect_stdout(io.StringIO()),
                        redirect_stderr(io.StringIO()),
                    ):
                        dispatch_cli_main(
                            [
                                "--home",
                                str(state_home),
                                "--dry-run",
                                *command,
                            ]
                        )
                        self.assertFalse(state_home.exists())
            execute.assert_not_called()

    def test_top_level_dry_run_routes_to_read_only_runtime_status(self) -> None:
        output = io.StringIO()
        with (
            redirect_stdout(output),
            self.assertRaises(SystemExit) as raised,
        ):
            dyro_main(["--dry-run", "runtime", "status"])
        self.assertEqual(raised.exception.code, 0)
        report = json.loads(output.getvalue())
        self.assertFalse(report["production"]["production_ready"])
        self.assertEqual(report["production"]["verdict"], "NOT_READY")


class GarbageCollectionSafetyTests(unittest.TestCase):
    def test_tampered_shadow_path_cannot_delete_outside_shadow_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            project = root / "project"
            project.mkdir()
            (project / "app.py").write_text("safe = True\n", encoding="utf-8")
            victim = home / "victim"
            victim.mkdir(parents=True)
            (victim / "keep.txt").write_text("keep\n", encoding="utf-8")
            supervisor = DispatchSupervisor(home=home)
            with patch(
                "experiments.local_agent_dispatch.supervisor.get_adapter",
                return_value=_WritingAdapter(),
            ):
                record = supervisor.accept(_payload(), project_root=project)
            record.status = "completed"
            record.shadow_path = str(home / "shadow" / ".." / "victim")
            supervisor.store.save(record)

            gc(home=home, max_age_seconds=0, dry_run=False)

            self.assertTrue((victim / "keep.txt").is_file())

    def test_same_run_symlink_cannot_authorize_external_shadow_deletion(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            project = root / "project"
            project.mkdir()
            (project / "app.py").write_text("safe = True\n", encoding="utf-8")
            victim = root / "outside-victim"
            victim.mkdir()
            marker = victim / "keep.txt"
            marker.write_text("keep\n", encoding="utf-8")
            supervisor = DispatchSupervisor(home=home)
            with patch(
                "experiments.local_agent_dispatch.supervisor.get_adapter",
                return_value=_WritingAdapter(),
            ):
                record = supervisor.accept(_payload(), project_root=project)
            expected_shadow = home / "shadow" / record.run_id
            expected_shadow.symlink_to(victim, target_is_directory=True)
            record.status = "completed"
            record.shadow_path = str(victim)
            supervisor.store.save(record)

            gc(home=home, max_age_seconds=0, dry_run=False)

            self.assertEqual(marker.read_text(encoding="utf-8"), "keep\n")
            self.assertTrue(expected_shadow.is_symlink())

    def test_gc_rejects_nan_without_deleting_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            victim = home / "shadow" / "keep"
            victim.mkdir(parents=True)
            (victim / "keep.txt").write_text("keep\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "finite"):
                gc(
                    home=home,
                    max_age_seconds=float("nan"),
                    dry_run=False,
                )

            self.assertTrue((victim / "keep.txt").is_file())

    def test_gc_preserves_active_shadow_even_when_old(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            project = root / "project"
            project.mkdir()
            (project / "app.py").write_text("safe = True\n", encoding="utf-8")
            supervisor = DispatchSupervisor(home=home)
            with patch(
                "experiments.local_agent_dispatch.supervisor.get_adapter",
                return_value=_WritingAdapter(),
            ):
                record = supervisor.accept(_payload(), project_root=project)
            active_shadow = home / "shadow" / record.run_id
            active_shadow.mkdir(parents=True)
            (active_shadow / "keep.txt").write_text("keep\n", encoding="utf-8")
            os.utime(active_shadow, (1, 1))
            record.status = "running"
            record.worker_token = "active-owner"
            record.shadow_path = str(active_shadow)
            supervisor.store.save(record)

            gc(home=home, max_age_seconds=0, dry_run=False)

            self.assertTrue((active_shadow / "keep.txt").is_file())

    def test_gc_never_removes_shadow_root_or_symlink_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            project = root / "project"
            project.mkdir()
            (project / "app.py").write_text("safe = True\n", encoding="utf-8")
            supervisor = DispatchSupervisor(home=home)
            with patch(
                "experiments.local_agent_dispatch.supervisor.get_adapter",
                return_value=_WritingAdapter(),
            ):
                root_record = supervisor.accept(_payload(), project_root=project)
                link_record = supervisor.accept(_payload(), project_root=project)
                active_record = supervisor.accept(_payload(), project_root=project)

            shadow_root = home / "shadow"
            target = shadow_root / "target"
            target.mkdir(parents=True)
            (target / "keep.txt").write_text("keep\n", encoding="utf-8")
            link = shadow_root / "link"
            link.symlink_to(target, target_is_directory=True)

            root_record.status = "completed"
            root_record.shadow_path = str(shadow_root)
            supervisor.store.save(root_record)
            link_record.status = "completed"
            link_record.shadow_path = str(link)
            supervisor.store.save(link_record)
            active_record.status = "running"
            active_record.worker_token = "active-owner"
            active_record.shadow_path = str(target)
            supervisor.store.save(active_record)

            gc(home=home, max_age_seconds=0, dry_run=False)

            self.assertTrue(shadow_root.is_dir())
            self.assertTrue((target / "keep.txt").is_file())

    def test_terminal_run_cannot_point_gc_at_active_run_shadow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            project = root / "project"
            project.mkdir()
            (project / "app.py").write_text("safe = True\n", encoding="utf-8")
            supervisor = DispatchSupervisor(home=home)
            with patch(
                "experiments.local_agent_dispatch.supervisor.get_adapter",
                return_value=_WritingAdapter(),
            ):
                terminal_record = supervisor.accept(
                    _payload(),
                    project_root=project,
                )
                active_record = supervisor.accept(
                    _payload(),
                    project_root=project,
                )
            active_shadow = home / "shadow" / active_record.run_id
            active_shadow.mkdir(parents=True)
            (active_shadow / "keep.txt").write_text(
                "keep\n",
                encoding="utf-8",
            )
            os.utime(active_shadow, (1, 1))

            terminal_record.status = "completed"
            terminal_record.shadow_path = str(active_shadow)
            terminal_record.updated_at = 0
            supervisor.store.save(terminal_record)
            active_record.status = "running"
            active_record.worker_token = "active-owner"
            active_record.shadow_path = str(active_shadow)
            supervisor.store.save(active_record)

            gc(home=home, max_age_seconds=0, dry_run=False)

            self.assertTrue((active_shadow / "keep.txt").is_file())

    def test_active_run_can_protect_terminal_run_shadow_from_gc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            project = root / "project"
            project.mkdir()
            (project / "app.py").write_text("safe = True\n", encoding="utf-8")
            supervisor = DispatchSupervisor(home=home)
            with patch(
                "experiments.local_agent_dispatch.supervisor.get_adapter",
                return_value=_WritingAdapter(),
            ):
                terminal_record = supervisor.accept(
                    _payload(),
                    project_root=project,
                )
                active_record = supervisor.accept(
                    _payload(),
                    project_root=project,
                )
            terminal_shadow = home / "shadow" / terminal_record.run_id
            terminal_shadow.mkdir(parents=True)
            (terminal_shadow / "keep.txt").write_text(
                "keep\n",
                encoding="utf-8",
            )
            os.utime(terminal_shadow, (1, 1))

            terminal_record.status = "completed"
            terminal_record.shadow_path = str(terminal_shadow)
            supervisor.store.save(terminal_record)
            active_record.status = "running"
            active_record.worker_token = "active-owner"
            active_record.shadow_path = str(terminal_shadow)
            supervisor.store.save(active_record)

            gc(home=home, max_age_seconds=0, dry_run=False)

            self.assertTrue((terminal_shadow / "keep.txt").is_file())

    def test_orphan_sweep_rechecks_run_created_after_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            project = root / "project"
            project.mkdir()
            (project / "app.py").write_text("safe = True\n", encoding="utf-8")
            supervisor = DispatchSupervisor(home=home)
            with patch(
                "experiments.local_agent_dispatch.supervisor.get_adapter",
                return_value=_WritingAdapter(),
            ):
                active_record = supervisor.accept(
                    _payload(),
                    project_root=project,
                )
            active_shadow = home / "shadow" / active_record.run_id
            active_shadow.mkdir(parents=True)
            (active_shadow / "keep.txt").write_text(
                "keep\n",
                encoding="utf-8",
            )
            os.utime(active_shadow, (1, 1))

            with patch.object(
                type(supervisor.store),
                "list_runs",
                return_value=[],
            ):
                gc(home=home, max_age_seconds=0, dry_run=False)

            self.assertTrue((active_shadow / "keep.txt").is_file())

    def test_gc_rejects_symlinked_managed_roots_without_touching_targets(
        self,
    ) -> None:
        for managed_name in ("shadow", "panels"):
            for dry_run in (False, True):
                with self.subTest(
                    managed_name=managed_name,
                    dry_run=dry_run,
                ):
                    with tempfile.TemporaryDirectory() as tmp:
                        root = Path(tmp)
                        home = root / "home"
                        home.mkdir()
                        victim = root / f"{managed_name}-victim"
                        victim.mkdir()
                        marker = victim / "keep.txt"
                        marker.write_text("keep\n", encoding="utf-8")
                        (home / managed_name).symlink_to(
                            victim,
                            target_is_directory=True,
                        )

                        with self.assertRaisesRegex(
                            DispatchValidationError,
                            "symbolic link",
                        ):
                            gc(
                                home=home,
                                max_age_seconds=0,
                                dry_run=dry_run,
                            )

                        self.assertEqual(
                            marker.read_text(encoding="utf-8"),
                            "keep\n",
                        )

    def test_gc_skips_symlinked_panel_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            supervisor = DispatchSupervisor(home=home)
            del supervisor
            target = root / "outside-panel.json"
            target.write_text(
                json.dumps({"created_at": 0}),
                encoding="utf-8",
            )
            (home / "panels" / "panel-escape.json").symlink_to(target)

            report = gc(home=home, max_age_seconds=0, dry_run=False)

            self.assertTrue(target.is_file())
            self.assertEqual(report["removed_panels"], [])


if __name__ == "__main__":
    unittest.main()

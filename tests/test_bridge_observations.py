from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import shutil
from unittest.mock import patch

from jsonschema import Draft202012Validator

from dyro import read_limits as read_limits_module
from dyro.bridge import observations as bridge_observations_module
from dyro.bridge.observations import (
    BridgeObservationError,
    GateDefinitionsObservation,
    ObservationFailure,
    WorkspaceListObservation,
    _scan_records,
    get_gate_definitions_observation,
    list_workspace_observations,
    observe_workspace,
)
from dyro.bridge.schemas import get_operation_schema
from dyro.config import load
from dyro.continuation import objective_storage as objective_storage_module
from dyro.continuation.objective_storage import _event_for, _pending_payload
from dyro.continuation.store import (
    create_objective,
    get_objective,
    pause_objective,
    resume_objective,
)
from dyro.errors import DyroError, ValidationError
from dyro.read_limits import ObservationLimits
from dyro.read_limits import (
    ReadBudget,
    ReadLimitCode,
    ReadLimitError,
    open_safe_directory_chain,
)
from dyro.tasks import task_template
from dyro.workspace import create_line

from .support import WorkspaceCase


class BridgeObservationTests(WorkspaceCase):
    def setUp(self) -> None:
        super().setUp()
        self.config = load(self.root)
        create_line(self.config, line_id="alpha", branch="feat/alpha", base="main")
        self._write_task("TASK-A")
        self.home = self.root / "dyro-home"
        self.home.mkdir()
        self.home.joinpath("workspaces.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "default": "sample",
                    "workspaces": [
                        {
                            "name": "sample",
                            "root": str(self.root.resolve()),
                            "last_kind": "",
                            "last_target": "",
                            "last_agent": "",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def _write_task(self, task_id: str, *, content: str | None = None) -> Path:
        directory = self.config.task_specs_dir / task_id
        directory.mkdir(parents=True)
        directory.joinpath("task.toml").write_text(
            content or task_template(task_id, task_id, "alpha", "api", "services/api"),
            encoding="utf-8",
        )
        directory.joinpath("status").write_text("backlog\n", encoding="utf-8")
        return directory

    def _observe(
        self,
        *,
        limits: ObservationLimits | None = None,
        monotonic=None,
    ):
        with patch.dict(os.environ, {"DYRO_HOME": str(self.home)}, clear=False):
            arguments = {
                "workspace": "sample",
                "start": None,
                "cwd": self.root,
                "limits": limits,
                "clock": lambda: datetime(2026, 8, 6, tzinfo=timezone.utc),
            }
            if monotonic is not None:
                arguments["monotonic"] = monotonic
            return observe_workspace(
                **arguments,
            )

    def test_workspace_observation_is_schema_valid_path_free_and_not_inspected(
        self,
    ) -> None:
        result = self._observe()
        payload = result.as_dict()
        Draft202012Validator(
            get_operation_schema("workspace.observe").output_schema()
        ).validate(payload)
        rendered = json.dumps(payload, sort_keys=True)
        self.assertNotIn(str(self.root), rendered)
        self.assertNotIn("argv", rendered)
        self.assertEqual(payload["integration_inspection"], "not_inspected")
        self.assertEqual(payload["tasks"][0]["status"], "backlog")
        self.assertNotIn("ready", rendered.lower())
        self.assertNotIn("dispatchable", rendered.lower())

    def test_one_invalid_task_does_not_erase_healthy_sibling(self) -> None:
        self._write_task("TASK-B", content="not valid = [")
        result = self._observe()
        self.assertEqual([item.id for item in result.tasks], ["TASK-A"])
        self.assertIn(("task:TASK-B", "RECORD_INVALID"), result.failure_pairs())
        self.assertTrue(result.partial)

    def test_wrong_task_executor_type_isolated_as_record_invalid(self) -> None:
        malformed = task_template(
            "TASK-B", "TASK-B", "alpha", "api", "services/api"
        ).replace(
            '[executor]\nagent = "codex"',
            'executor = "wrong-table-type"',
        )
        self._write_task("TASK-B", content=malformed)
        result = self._observe()
        self.assertEqual([item.id for item in result.tasks], ["TASK-A"])
        self.assertIn(("task:TASK-B", "RECORD_INVALID"), result.failure_pairs())

    def test_wrong_task_gates_type_is_a_typed_gate_definition_failure(self) -> None:
        malformed = task_template("TASK-A", "TASK-A", "alpha", "api", "services/api")
        gate_block = """[[gates]]
name = "diff-check"
argv = ["git", "diff", "--check"]
cwd = "services/api"
timeout_seconds = 120

"""
        malformed = malformed.replace(
            'conflict_group = ""', 'conflict_group = ""\ngates = "wrong-array-type"'
        ).replace(gate_block, "")
        self.config.task_specs_dir.joinpath("TASK-A", "task.toml").write_text(
            malformed,
            encoding="utf-8",
        )
        with patch.dict(os.environ, {"DYRO_HOME": str(self.home)}, clear=False):
            with self.assertRaises(BridgeObservationError) as raised:
                get_gate_definitions_observation(
                    task_id="TASK-A", workspace="sample", start=None, cwd=self.root
                )
        self.assertEqual(raised.exception.error.code.value, "RECORD_INVALID")

    def test_task_parent_replacement_after_scan_is_rejected(self) -> None:
        parent = self.config.task_specs_dir
        displaced = parent.with_name("tasks-original")
        replacement = self.root / "replacement-tasks"
        replacement_task = replacement / "TASK-A"
        replacement_task.mkdir(parents=True)
        replacement_task.joinpath("task.toml").write_text(
            task_template("TASK-A", "Replacement", "alpha", "api", "services/api"),
            encoding="utf-8",
        )
        replacement_task.joinpath("status").write_text("done\n", encoding="utf-8")
        real_scan = bridge_observations_module._scan_records
        swapped = False

        def scan_then_replace(path, **kwargs):
            nonlocal swapped
            scan = real_scan(path, **kwargs)
            if not swapped and path == parent:
                parent.rename(displaced)
                replacement.rename(parent)
                swapped = True
            return scan

        with patch(
            "dyro.bridge.observations._scan_records", side_effect=scan_then_replace
        ):
            result = self._observe()
        self.assertTrue(swapped)
        self.assertEqual(result.tasks, ())
        self.assertIn(("task:TASK-A", "RECORD_INVALID"), result.failure_pairs())

    def test_task_record_replacement_after_scan_is_rejected(self) -> None:
        original = self.config.task_specs_dir / "TASK-A"
        displaced = self.config.task_specs_dir / "TASK-A-original"
        replacement = self.root / "replacement-task-record"
        replacement.mkdir()
        replacement.joinpath("task.toml").write_text(
            task_template("TASK-A", "Replacement", "alpha", "api", "services/api"),
            encoding="utf-8",
        )
        replacement.joinpath("status").write_text("done\n", encoding="utf-8")
        real_scan = bridge_observations_module._scan_records
        swapped = False

        def scan_then_replace(path, **kwargs):
            nonlocal swapped
            scan = real_scan(path, **kwargs)
            if not swapped and path == self.config.task_specs_dir:
                original.rename(displaced)
                replacement.rename(original)
                swapped = True
            return scan

        with patch(
            "dyro.bridge.observations._scan_records", side_effect=scan_then_replace
        ):
            result = self._observe()
        self.assertTrue(swapped)
        self.assertEqual(result.tasks, ())
        self.assertIn(("task:TASK-A", "RECORD_INVALID"), result.failure_pairs())

    def test_line_parent_replacement_after_scan_is_rejected(self) -> None:
        parent = self.config.lines_state_dir
        displaced = parent.with_name("lines-original")
        replacement = self.root / "replacement-lines"
        shutil.copytree(parent, replacement)
        real_scan = bridge_observations_module._scan_records
        swapped = False

        def scan_then_replace(path, **kwargs):
            nonlocal swapped
            scan = real_scan(path, **kwargs)
            if not swapped and path == parent:
                parent.rename(displaced)
                replacement.rename(parent)
                swapped = True
            return scan

        with patch(
            "dyro.bridge.observations._scan_records", side_effect=scan_then_replace
        ):
            result = self._observe()
        self.assertTrue(swapped)
        self.assertEqual(result.lines, ())
        self.assertIn(("line:alpha", "RECORD_INVALID"), result.failure_pairs())

    def test_line_record_replacement_after_scan_is_rejected(self) -> None:
        original = self.config.lines_state_dir / "alpha.toml"
        displaced = original.with_name("alpha-original.toml")
        replacement = self.root / "replacement-alpha.toml"
        replacement.write_bytes(original.read_bytes() + b"\n# replacement\n")
        real_scan = bridge_observations_module._scan_records
        swapped = False

        def scan_then_replace(path, **kwargs):
            nonlocal swapped
            scan = real_scan(path, **kwargs)
            if not swapped and path == self.config.lines_state_dir:
                original.rename(displaced)
                replacement.rename(original)
                swapped = True
            return scan

        with patch(
            "dyro.bridge.observations._scan_records", side_effect=scan_then_replace
        ):
            result = self._observe()
        self.assertTrue(swapped)
        self.assertEqual(result.lines, ())
        self.assertIn(("line:alpha", "RECORD_INVALID"), result.failure_pairs())

    def test_objective_parent_replacement_after_scan_is_rejected(self) -> None:
        create_objective(
            self.config,
            """schema_version = 1
id = "swap-objective"
title = "Swap objective"
line = "alpha"
targets = ["TASK-A"]

[continuation]
requested_mode = "observe"
operations = ["execute"]
""",
        )
        pause_objective(self.config, "swap-objective")
        parent = self.config.objectives_dir
        displaced = parent.with_name("objectives-original")
        replacement = self.root / "replacement-objectives"
        shutil.copytree(parent, replacement)
        resume_objective(self.config, "swap-objective")
        real_scan = bridge_observations_module._scan_records
        swapped = False

        def scan_then_replace(path, **kwargs):
            nonlocal swapped
            scan = real_scan(path, **kwargs)
            if not swapped and path == parent:
                parent.rename(displaced)
                replacement.rename(parent)
                swapped = True
            return scan

        with patch(
            "dyro.bridge.observations._scan_records", side_effect=scan_then_replace
        ):
            result = self._observe()
        self.assertTrue(swapped)
        self.assertEqual(result.objectives, ())
        self.assertIn(
            ("objective:swap-objective", "RECORD_INVALID"),
            result.failure_pairs(),
        )

    def test_objective_record_replacement_after_scan_is_rejected(self) -> None:
        create_objective(
            self.config,
            """schema_version = 1
id = "record-swap"
title = "Record swap"
line = "alpha"
targets = ["TASK-A"]

[continuation]
requested_mode = "observe"
operations = ["execute"]
""",
        )
        pause_objective(self.config, "record-swap")
        original = self.config.objectives_dir / "record-swap"
        displaced = self.config.objectives_dir / "record-swap-original"
        replacement = self.root / "replacement-objective-record"
        shutil.copytree(original, replacement)
        resume_objective(self.config, "record-swap")
        real_scan = bridge_observations_module._scan_records
        swapped = False

        def scan_then_replace(path, **kwargs):
            nonlocal swapped
            scan = real_scan(path, **kwargs)
            if not swapped and path == self.config.objectives_dir:
                original.rename(displaced)
                replacement.rename(original)
                swapped = True
            return scan

        with patch(
            "dyro.bridge.observations._scan_records", side_effect=scan_then_replace
        ):
            result = self._observe()
        self.assertTrue(swapped)
        self.assertEqual(result.objectives, ())
        self.assertIn(
            ("objective:record-swap", "RECORD_INVALID"), result.failure_pairs()
        )

    def test_missing_intermediate_directory_cannot_join_a_later_generation(
        self,
    ) -> None:
        create_objective(
            self.config,
            """schema_version = 1
id = "later-objective"
title = "Later objective"
line = "alpha"
targets = ["TASK-A"]

[continuation]
requested_mode = "observe"
operations = ["execute"]
""",
        )
        state_root = self.root / ".dyro"
        displaced = self.root / ".dyro-original"
        state_root.rename(displaced)
        real_read_objectives = bridge_observations_module._read_objectives

        def restore_then_read(*args, **kwargs):
            displaced.rename(state_root)
            return real_read_objectives(*args, **kwargs)

        with patch(
            "dyro.bridge.observations._read_objectives",
            side_effect=restore_then_read,
        ):
            result = self._observe()
        self.assertEqual(result.lines, ())
        self.assertEqual(result.tasks, ())
        self.assertEqual(result.objectives, ())
        self.assertIn(("objectives", "RECORD_INVALID"), result.failure_pairs())
        self.assertTrue(result.partial)

    def test_objective_event_validation_stops_at_deadline(self) -> None:
        create_objective(
            self.config,
            """schema_version = 1
id = "deadline-events"
title = "Deadline events"
line = "alpha"
targets = ["TASK-A"]

[continuation]
requested_mode = "observe"
operations = ["execute"]
""",
        )
        for _ in range(10):
            pause_objective(self.config, "deadline-events")
            resume_objective(self.config, "deadline-events")
        elapsed = 0.0
        validated = 0
        real_validate = objective_storage_module._validate_event_bounded

        def monotonic() -> float:
            return elapsed

        def validate_then_expire(*args, **kwargs):
            nonlocal elapsed, validated
            event = real_validate(*args, **kwargs)
            validated += 1
            if validated == 1:
                elapsed = 2.0
            return event

        with patch(
            "dyro.continuation.objective_storage._validate_event_bounded",
            side_effect=validate_then_expire,
        ):
            result = self._observe(
                limits=ObservationLimits(deadline_seconds=1.0),
                monotonic=monotonic,
            )
        self.assertEqual(validated, 1)
        self.assertIn(
            ("objective:deadline-events", "OBSERVATION_DEADLINE_EXCEEDED"),
            result.failure_pairs(),
        )

    def test_objective_event_count_is_bounded(self) -> None:
        create_objective(
            self.config,
            """schema_version = 1
id = "bounded-events"
title = "Bounded events"
line = "alpha"
targets = ["TASK-A"]

[continuation]
requested_mode = "observe"
operations = ["execute"]
""",
        )
        pause_objective(self.config, "bounded-events")
        result = self._observe(limits=ObservationLimits(objective_event_records=1))
        self.assertIn(
            ("objective:bounded-events", "RESOURCE_LIMIT_EXCEEDED"),
            result.failure_pairs(),
        )

    def test_objective_permission_error_preserves_host_permission_code(self) -> None:
        create_objective(
            self.config,
            """schema_version = 1
id = "permission-objective"
title = "Permission objective"
line = "alpha"
targets = ["TASK-A"]

[continuation]
requested_mode = "observe"
operations = ["execute"]
""",
        )
        real_open = os.open

        def deny_state(path, *args, **kwargs):
            if path == "state.json":
                raise PermissionError("denied")
            return real_open(path, *args, **kwargs)

        with patch(
            "dyro.continuation.objective_storage.os.open", side_effect=deny_state
        ):
            result = self._observe()
        self.assertIn(
            ("objective:permission-objective", "HOST_READ_PERMISSION_REQUIRED"),
            result.failure_pairs(),
        )

    def test_one_invalid_objective_does_not_erase_healthy_sibling_or_recover(
        self,
    ) -> None:
        create_objective(
            self.config,
            """schema_version = 1
id = "observe-a"
title = "Observe A"
line = "alpha"
targets = ["TASK-A"]

[continuation]
requested_mode = "observe"
operations = ["execute"]
""",
        )
        bad = self.config.objectives_dir / "broken"
        bad.mkdir()
        bad.joinpath("events.jsonl").write_text("not-json\n", encoding="utf-8")
        pending = bad / "pending.json"
        pending.write_text("{}\n", encoding="utf-8")
        with patch(
            "dyro.continuation.objective_storage.recover_pending",
            side_effect=AssertionError("recovery attempted"),
        ):
            result = self._observe()
        self.assertTrue(pending.exists())
        self.assertEqual([item.id for item in result.objectives], ["observe-a"])
        self.assertIn(("objective:broken", "RECORD_INVALID"), result.failure_pairs())

    def test_deep_objective_event_isolated_as_record_invalid(self) -> None:
        for objective_id in ("deep-bad", "healthy"):
            create_objective(
                self.config,
                f"""schema_version = 1
id = "{objective_id}"
title = "{objective_id}"
line = "alpha"
targets = ["TASK-A"]

[continuation]
requested_mode = "observe"
operations = ["execute"]
""",
            )
        original_validate = objective_storage_module._validate_event

        def validate_with_deep_failure(*args, **kwargs):
            if "deep-bad" in str(kwargs["path"]):
                raise RecursionError("nested record")
            return original_validate(*args, **kwargs)

        with patch(
            "dyro.continuation.objective_storage._validate_event",
            side_effect=validate_with_deep_failure,
        ):
            result = self._observe()
        self.assertEqual([item.id for item in result.objectives], ["healthy"])
        self.assertIn(("objective:deep-bad", "RECORD_INVALID"), result.failure_pairs())

    def test_real_deep_objective_state_isolated_as_record_invalid(self) -> None:
        for objective_id in ("deep-state", "healthy-state"):
            create_objective(
                self.config,
                f"""schema_version = 1
id = "{objective_id}"
title = "{objective_id}"
line = "alpha"
targets = ["TASK-A"]

[continuation]
requested_mode = "observe"
operations = ["execute"]
""",
            )
        nested = '{"x":' + "[" * 1000 + "0" + "]" * 1000 + "}\n"
        self.config.objectives_dir.joinpath("deep-state", "state.json").write_text(
            nested, encoding="utf-8"
        )
        result = self._observe()
        self.assertEqual([item.id for item in result.objectives], ["healthy-state"])
        self.assertIn(
            ("objective:deep-state", "RECORD_INVALID"), result.failure_pairs()
        )

    def test_oversized_record_and_count_cap_are_explicit_partial_results(self) -> None:
        self._write_task("TASK-B")
        self._write_task("TASK-C")
        self.config.task_specs_dir.joinpath("TASK-B", "task.toml").write_text(
            "x" * 2048, encoding="utf-8"
        )
        result = self._observe(
            limits=ObservationLimits(task_manifest_bytes=1024, task_records=2)
        )
        # Once the directory exceeds its computation cap, no filesystem-order
        # subset is projected; that keeps the partial result deterministic.
        self.assertEqual(result.tasks, ())
        self.assertIn(("tasks", "RESOURCE_LIMIT_EXCEEDED"), result.failure_pairs())
        self.assertTrue(result.truncated)

    def test_protocol_limits_can_only_be_tightened(self) -> None:
        for values in (
            {"response_records": 101},
            {"aggregate_bytes": 64 * 1024 * 1024 + 1},
            {"objective_event_records": 10_001},
            {"deadline_seconds": 5.1},
            {"deadline_seconds": math.inf},
            {"deadline_seconds": math.nan},
        ):
            with self.subTest(values=values):
                with self.assertRaises(ValidationError):
                    ObservationLimits(**values)

    def test_observation_dtos_reject_mutable_collections(self) -> None:
        with self.assertRaises(ValidationError):
            WorkspaceListObservation([])  # type: ignore[arg-type]
        with self.assertRaises(ValidationError):
            GateDefinitionsObservation("TASK-A", [])  # type: ignore[arg-type]
        with self.assertRaises(ValidationError):
            GateDefinitionsObservation(7, ())  # type: ignore[arg-type]
        with self.assertRaises(ValidationError):
            ObservationFailure("x" * 129, "RECORD_INVALID")
        with self.assertRaises(ValidationError):
            replace(self._observe(), workspace_revision="not-a-digest")

    def test_safe_directory_chain_rejects_lexical_parent_escape(self) -> None:
        budget = ReadBudget(ObservationLimits())
        with self.assertRaises(ValidationError):
            with budget.open_safe_directory_chain(
                self.config.root,
                self.config.root / ".." / "outside",
            ):
                self.fail("parent escape unexpectedly opened")

    def test_bounded_file_open_rejects_fifo_without_blocking(self) -> None:
        if not hasattr(os, "mkfifo"):
            self.skipTest("FIFO support is required")
        fifo = self.root / "probe.fifo"
        os.mkfifo(fifo)
        budget = ReadBudget(ObservationLimits(deadline_seconds=0.1))
        with self.assertRaises(ReadLimitError) as raised:
            budget.read_regular_bytes_at(
                root=self.config.root,
                directory=self.config.root,
                name=fifo.name,
                maximum_bytes=1024,
                label="FIFO probe",
            )
        self.assertEqual(raised.exception.code, ReadLimitCode.UNSAFE_FILE)

    def test_objective_fifo_isolated_without_blocking(self) -> None:
        if not hasattr(os, "mkfifo"):
            self.skipTest("FIFO support is required")
        for objective_id in ("fifo-state", "healthy-fifo-sibling"):
            create_objective(
                self.config,
                f"""schema_version = 1
id = "{objective_id}"
title = "{objective_id}"
line = "alpha"
targets = ["TASK-A"]

[continuation]
requested_mode = "observe"
operations = ["execute"]
""",
            )
        state = self.config.objectives_dir / "fifo-state" / "state.json"
        state.unlink()
        os.mkfifo(state)
        result = self._observe()
        self.assertIn("healthy-fifo-sibling", {item.id for item in result.objectives})
        self.assertIn(
            ("objective:fifo-state", "RECORD_INVALID"), result.failure_pairs()
        )

    def test_record_scan_stops_after_one_over_limit_sentinel(self) -> None:
        for index in range(10):
            self._write_task(f"TASK-{index:02d}")
        real_scandir = os.scandir
        inspected = 0

        class CountingScandir:
            def __init__(self, directory_fd):
                self._inner = real_scandir(directory_fd)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self._inner.close()

            def __iter__(self):
                return self

            def __next__(self):
                nonlocal inspected
                item = next(self._inner)
                inspected += 1
                return item

        with patch(
            "dyro.bridge.observations.os.scandir",
            side_effect=CountingScandir,
        ):
            scan = _scan_records(
                self.config.task_specs_dir,
                workspace_root=self.config.root,
                maximum=2,
                directories=True,
                budget=ReadBudget(ObservationLimits()),
            )
        self.assertEqual(inspected, 3)
        self.assertEqual(scan.names, ())
        self.assertTrue(scan.truncated)

    def test_record_scan_counts_hidden_and_wrong_suffix_entries(self) -> None:
        parent = self.config.lines_state_dir
        for name in (".hidden-a", ".hidden-b", "wrong.txt", "more.txt"):
            parent.joinpath(name).write_text("ignored\n", encoding="utf-8")
        real_scandir = os.scandir
        inspected = 0

        class CountingScandir:
            def __init__(self, directory_fd):
                self._inner = real_scandir(directory_fd)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self._inner.close()

            def __iter__(self):
                return self

            def __next__(self):
                nonlocal inspected
                item = next(self._inner)
                inspected += 1
                return item

        with patch(
            "dyro.bridge.observations.os.scandir",
            side_effect=CountingScandir,
        ):
            scan = _scan_records(
                parent,
                workspace_root=self.config.root,
                maximum=2,
                directories=False,
                suffix=".toml",
                budget=ReadBudget(ObservationLimits()),
            )
        self.assertEqual(inspected, 3)
        self.assertEqual(scan.names, ())
        self.assertEqual(scan.invalid_entries, 0)
        self.assertTrue(scan.truncated)

    def test_failure_projection_is_capped_without_erasing_a_later_healthy_task(
        self,
    ) -> None:
        for index in range(101):
            directory = self.config.task_specs_dir / f"BAD-{index:03d}"
            directory.mkdir()
            directory.joinpath("task.toml").write_text(
                "not valid = [", encoding="utf-8"
            )
        result = self._observe()
        payload = result.as_dict()
        Draft202012Validator(
            get_operation_schema("workspace.observe").output_schema()
        ).validate(payload)
        self.assertIn("TASK-A", {item.id for item in result.tasks})
        self.assertLessEqual(len(result.failures), 100)
        self.assertTrue(result.truncated)

    def test_deadline_is_bounded_and_reported_without_emptying_prior_components(
        self,
    ) -> None:
        elapsed = 0.0
        original_load_line = bridge_observations_module.load_line_bounded

        def monotonic() -> float:
            return elapsed

        def load_line_then_expire(*args, **kwargs):
            nonlocal elapsed
            result = original_load_line(*args, **kwargs)
            elapsed = 10.0
            return result

        with patch(
            "dyro.bridge.observations.load_line_bounded",
            side_effect=load_line_then_expire,
        ):
            result = self._observe(
                limits=ObservationLimits(deadline_seconds=1.0),
                monotonic=monotonic,
            )
        self.assertTrue(result.partial)
        self.assertTrue(result.lines)
        self.assertIn(
            "OBSERVATION_DEADLINE_EXCEEDED",
            {code for _, code in result.failure_pairs()},
        )

    def test_workspace_list_preserves_a_deadline_item_as_partial(self) -> None:
        with patch.dict(os.environ, {"DYRO_HOME": str(self.home)}, clear=False):
            with patch(
                "dyro.bridge.observations._load_registered_for_list",
                return_value=bridge_observations_module.ErrorCode.OBSERVATION_DEADLINE_EXCEEDED,
            ):
                result = list_workspace_observations()
        self.assertTrue(result.partial)
        self.assertEqual(
            result.workspaces[0].failure_code,
            "OBSERVATION_DEADLINE_EXCEEDED",
        )

    def test_deadline_crossed_during_revision_hash_cannot_return_complete(self) -> None:
        elapsed = 0.0
        real_sha256 = __import__("hashlib").sha256

        def monotonic() -> float:
            return elapsed

        def delayed_sha256(*args, **kwargs):
            nonlocal elapsed
            elapsed = 2.0
            return real_sha256(*args, **kwargs)

        with patch("dyro.bridge.observations.hashlib.sha256", delayed_sha256):
            with self.assertRaises(BridgeObservationError) as raised:
                self._observe(
                    limits=ObservationLimits(deadline_seconds=1.0),
                    monotonic=monotonic,
                )
        self.assertEqual(
            raised.exception.error.code.value,
            "OBSERVATION_DEADLINE_EXCEEDED",
        )

    def test_gate_definitions_are_allowlisted_and_never_execute(self) -> None:
        with patch.dict(os.environ, {"DYRO_HOME": str(self.home)}, clear=False):
            with (
                patch(
                    "dyro.tasks.run_gates", side_effect=AssertionError("gate execution")
                ),
                patch("dyro.process.run", side_effect=AssertionError("subprocess")),
            ):
                result = get_gate_definitions_observation(
                    task_id="TASK-A", workspace="sample", start=None, cwd=self.root
                )
        payload = result.as_dict()
        Draft202012Validator(
            get_operation_schema("task.gate_definitions.get").output_schema()
        ).validate(payload)
        self.assertEqual(payload["task_id"], "TASK-A")
        self.assertEqual(
            payload["gates"],
            [{"name": "diff-check", "required": True, "description": None}],
        )
        self.assertNotIn("argv", json.dumps(payload))

    def test_gate_definition_invalid_task_id_is_schema_validation_failure(
        self,
    ) -> None:
        with patch.dict(os.environ, {"DYRO_HOME": str(self.home)}, clear=False):
            for task_id in ("bad id", 7):
                with self.subTest(task_id=task_id):
                    with self.assertRaises(BridgeObservationError) as raised:
                        get_gate_definitions_observation(
                            task_id=task_id,
                            workspace="sample",
                            start=None,
                            cwd=self.root,
                        )
                    self.assertEqual(
                        raised.exception.error.code.value,
                        "SCHEMA_VALIDATION_FAILED",
                    )

    def test_gate_definition_rejects_a_symlinked_intermediate_task_directory(
        self,
    ) -> None:
        outside = self.root / "outside-task"
        outside.mkdir()
        outside.joinpath("task.toml").write_text(
            task_template("TASK-LINK", "Task Link", "alpha", "api", "services/api"),
            encoding="utf-8",
        )
        self.config.task_specs_dir.joinpath("TASK-LINK").symlink_to(
            outside, target_is_directory=True
        )
        with patch.dict(os.environ, {"DYRO_HOME": str(self.home)}, clear=False):
            with self.assertRaises(BridgeObservationError) as raised:
                get_gate_definitions_observation(
                    task_id="TASK-LINK",
                    workspace="sample",
                    start=None,
                    cwd=self.root,
                )
        self.assertEqual(raised.exception.error.code.value, "RECORD_INVALID")

    def test_gate_definition_rejects_parent_swap_after_directory_scan(self) -> None:
        outside = self.root / "outside-race"
        outside.mkdir()
        outside.joinpath("task.toml").write_text(
            task_template(
                "TASK-A",
                "Outside",
                "alpha",
                "api",
                "services/api",
            ).replace('name = "diff-check"', 'name = "external-gate"'),
            encoding="utf-8",
        )
        original = self.config.task_specs_dir / "TASK-A"
        displaced = self.config.task_specs_dir / "TASK-A-original"
        real_open = os.open
        swapped = False

        def racing_open(path, flags, *args, **kwargs):
            nonlocal swapped
            if (
                not swapped
                and path == "TASK-A"
                and kwargs.get("dir_fd") is not None
                and flags & getattr(os, "O_DIRECTORY", 0)
            ):
                original.rename(displaced)
                original.symlink_to(outside, target_is_directory=True)
                swapped = True
            return real_open(path, flags, *args, **kwargs)

        with patch.dict(os.environ, {"DYRO_HOME": str(self.home)}, clear=False):
            with patch("dyro.read_limits.os.open", side_effect=racing_open):
                with self.assertRaises(BridgeObservationError) as raised:
                    get_gate_definitions_observation(
                        task_id="TASK-A",
                        workspace="sample",
                        start=None,
                        cwd=self.root,
                    )
        self.assertTrue(swapped)
        self.assertEqual(raised.exception.error.code.value, "RECORD_INVALID")

    def test_observation_keeps_manifest_and_status_on_one_task_directory_fd(
        self,
    ) -> None:
        original = self.config.task_specs_dir / "TASK-A"
        displaced = self.config.task_specs_dir / "TASK-A-original"
        replacement = self.root / "replacement-task"
        replacement.mkdir()
        replacement.joinpath("task.toml").write_text(
            task_template("TASK-A", "Replacement", "alpha", "api", "services/api"),
            encoding="utf-8",
        )
        replacement.joinpath("status").write_text("done\n", encoding="utf-8")
        real_read = ReadBudget.read_regular_bytes_from_directory_fd
        swapped = False

        def read_then_swap(budget, directory_fd, **kwargs):
            nonlocal swapped
            content = real_read(budget, directory_fd, **kwargs)
            if not swapped and kwargs["name"] == "task.toml":
                original.rename(displaced)
                original.symlink_to(replacement, target_is_directory=True)
                swapped = True
            return content

        with patch.object(
            ReadBudget,
            "read_regular_bytes_from_directory_fd",
            new=read_then_swap,
        ):
            result = self._observe()
        self.assertTrue(swapped)
        self.assertEqual(result.tasks[0].id, "TASK-A")
        self.assertEqual(result.tasks[0].status, "backlog")

    def test_symlinked_state_ancestor_is_rejected_even_when_children_are_missing(
        self,
    ) -> None:
        root = self.root / "unsafe-workspace"
        root.mkdir()
        root.joinpath("dyro.toml").write_text(
            self.root.joinpath("dyro.toml")
            .read_text(encoding="utf-8")
            .replace('name = "test-workspace"', 'name = "unsafe-workspace"'),
            encoding="utf-8",
        )
        empty = self.root / "empty-state"
        empty.mkdir()
        root.joinpath(".dyro").symlink_to(empty, target_is_directory=True)
        registry = json.loads(
            self.home.joinpath("workspaces.json").read_text(encoding="utf-8")
        )
        registry["workspaces"].append(
            {
                "name": "unsafe",
                "root": str(root),
                "last_kind": "",
                "last_target": "",
                "last_agent": "",
            }
        )
        self.home.joinpath("workspaces.json").write_text(
            json.dumps(registry), encoding="utf-8"
        )
        with patch.dict(os.environ, {"DYRO_HOME": str(self.home)}, clear=False):
            result = observe_workspace(workspace="unsafe", start=None, cwd=self.root)
        self.assertTrue(result.partial)
        self.assertIn("RECORD_INVALID", {item.code for item in result.failures})

    def test_symlinked_task_manifest_is_record_invalid_not_resource_exhaustion(
        self,
    ) -> None:
        task_file = self.config.task_specs_dir / "TASK-A" / "task.toml"
        target = task_file.with_name("task-target.toml")
        task_file.rename(target)
        task_file.symlink_to(target)
        result = self._observe()
        self.assertIn(("task:TASK-A", "RECORD_INVALID"), result.failure_pairs())
        self.assertNotIn(
            ("task:TASK-A", "RESOURCE_LIMIT_EXCEEDED"), result.failure_pairs()
        )

    def test_valid_pending_transaction_is_never_recovered_or_changed(self) -> None:
        create_objective(
            self.config,
            """schema_version = 1
id = "pending-a"
title = "Pending A"
line = "alpha"
targets = ["TASK-A"]

[continuation]
requested_mode = "observe"
operations = ["execute"]
""",
        )
        record = get_objective(self.config, "pending-a")
        pending_payload = _pending_payload(
            _event_for(record, "objective_updated"), None, None
        )
        pending = self.config.objectives_dir / "pending-a" / "pending.json"
        pending.write_text(
            json.dumps(pending_payload, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        before = pending.read_bytes(), pending.stat().st_mtime_ns
        with patch(
            "dyro.continuation.objective_storage.recover_pending",
            side_effect=AssertionError("recovery attempted"),
        ):
            result = self._observe()
        after = pending.read_bytes(), pending.stat().st_mtime_ns
        self.assertEqual(before, after)
        self.assertIn(("objective:pending-a", "RECORD_INVALID"), result.failure_pairs())

    def test_aggregate_budget_fails_before_consuming_the_descriptor(self) -> None:
        source = self.root / "aggregate.bin"
        source.write_bytes(b"12345678")
        descriptor = os.open(source, os.O_RDONLY)
        try:
            budget = ReadBudget(ObservationLimits(aggregate_bytes=4))
            with self.assertRaises(ReadLimitError) as raised:
                budget.read_descriptor_bytes(
                    descriptor,
                    size=8,
                    maximum_bytes=16,
                    label="aggregate probe",
                )
            self.assertEqual(
                raised.exception.code, ReadLimitCode.AGGREGATE_BYTES_EXCEEDED
            )
            self.assertEqual(os.lseek(descriptor, 0, os.SEEK_CUR), 0)
            self.assertEqual(budget.bytes_read, 0)
        finally:
            os.close(descriptor)

    def test_safe_directory_root_fstat_failure_closes_descriptor(self) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(self.root, flags)
        with (
            patch(
                "dyro.read_limits._open_absolute_directory",
                return_value=descriptor,
            ),
            patch("dyro.read_limits.os.fstat", side_effect=OSError("fstat failed")),
        ):
            with self.assertRaises(ReadLimitError):
                with open_safe_directory_chain(self.root, self.root):
                    pass
        with self.assertRaises(OSError):
            os.fstat(descriptor)

    def test_directory_parent_close_failure_still_closes_opened_child(self) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        parent_fd = os.open(self.root, flags)
        child_fd = os.dup(parent_fd)
        real_close = os.close
        parent_failed = False

        def fail_first_parent_close(descriptor):
            nonlocal parent_failed
            if descriptor == parent_fd and not parent_failed:
                parent_failed = True
                raise OSError("parent close failed")
            return real_close(descriptor)

        with (
            patch(
                "dyro.read_limits.os.open",
                side_effect=(parent_fd, child_fd),
            ),
            patch(
                "dyro.read_limits.os.close",
                side_effect=fail_first_parent_close,
            ),
        ):
            with self.assertRaises(OSError):
                read_limits_module._open_absolute_directory(Path("/child"))
        self.assertTrue(parent_failed)
        with self.assertRaises(OSError):
            os.fstat(child_fd)
        try:
            os.fstat(parent_fd)
        except OSError:
            pass
        else:
            os.close(parent_fd)

    def test_directory_parent_close_error_never_retries_a_reused_fd(self) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        parent_fd = os.open(self.root, flags)
        child_fd = os.dup(parent_fd)
        real_open = os.open
        real_close = os.close
        victim_fd: int | None = None
        parent_failed = False

        def close_then_report_error(descriptor):
            nonlocal parent_failed, victim_fd
            if descriptor == parent_fd and not parent_failed:
                parent_failed = True
                real_close(descriptor)
                victim_fd = real_open("/dev/null", os.O_RDONLY)
                self.assertEqual(victim_fd, parent_fd)
                raise OSError("delayed parent close error")
            return real_close(descriptor)

        try:
            with (
                patch(
                    "dyro.read_limits.os.open",
                    side_effect=(parent_fd, child_fd),
                ),
                patch(
                    "dyro.read_limits.os.close",
                    side_effect=close_then_report_error,
                ),
            ):
                with self.assertRaises(OSError):
                    read_limits_module._open_absolute_directory(Path("/child"))
            self.assertTrue(parent_failed)
            assert victim_fd is not None
            os.fstat(victim_fd)
            with self.assertRaises(OSError):
                os.fstat(child_fd)
        finally:
            if victim_fd is not None:
                real_close(victim_fd)

    def test_allow_missing_never_treats_a_missing_root_as_optional(self) -> None:
        missing_root = self.root / "missing-root"
        with self.assertRaises(ReadLimitError) as raised:
            with open_safe_directory_chain(
                missing_root,
                missing_root / "state",
                allow_missing=True,
            ):
                pass
        self.assertEqual(raised.exception.code, ReadLimitCode.UNSAFE_FILE)

    def test_budget_maps_a_missing_root_to_an_unsafe_file_error(self) -> None:
        missing_root = self.root / "missing-budget-root"
        budget = ReadBudget(ObservationLimits())
        with self.assertRaises(ReadLimitError) as raised:
            with budget.open_safe_directory_chain(
                missing_root,
                missing_root / "state",
                allow_missing=True,
            ):
                pass
        self.assertEqual(raised.exception.code, ReadLimitCode.UNSAFE_FILE)

    def test_directory_traversal_and_objective_storage_fail_closed_on_windows(
        self,
    ) -> None:
        with patch("dyro.read_limits.os.name", "nt"):
            with self.assertRaises(ReadLimitError) as traversal:
                with ReadBudget(ObservationLimits()).open_safe_directory_chain(
                    self.root,
                    self.root,
                ):
                    pass
        self.assertEqual(traversal.exception.code, ReadLimitCode.UNSAFE_FILE)
        with patch("dyro.continuation.objective_storage.os.name", "nt"):
            with self.assertRaises(DyroError):
                with objective_storage_module.open_objective_directory(
                    self.config, "objective"
                ):
                    pass

    def test_registry_listing_preserves_stale_records_without_exposing_paths(
        self,
    ) -> None:
        registry = json.loads(
            self.home.joinpath("workspaces.json").read_text(encoding="utf-8")
        )
        registry["workspaces"].append(
            {
                "name": "stale",
                "root": str(self.root / "missing"),
                "last_kind": "",
                "last_target": "",
                "last_agent": "",
            }
        )
        self.home.joinpath("workspaces.json").write_text(
            json.dumps(registry), encoding="utf-8"
        )
        with patch.dict(os.environ, {"DYRO_HOME": str(self.home)}, clear=False):
            result = list_workspace_observations()
        payload = result.as_dict()
        Draft202012Validator(
            get_operation_schema("workspace.list").output_schema()
        ).validate(payload)
        self.assertEqual(
            [item.registry_alias for item in result.workspaces], ["sample", "stale"]
        )
        self.assertEqual(result.workspaces[1].failure_code, "REGISTERED_ROOT_STALE")
        self.assertNotIn(str(self.root), json.dumps(payload))

    def test_registry_listing_isolates_an_invalid_profile_name(self) -> None:
        invalid = self.root / "invalid-profile"
        invalid.mkdir()
        invalid.joinpath("dyro.toml").write_text(
            self.root.joinpath("dyro.toml")
            .read_text(encoding="utf-8")
            .replace('name = "test-workspace"', 'name = "invalid name"'),
            encoding="utf-8",
        )
        registry = json.loads(
            self.home.joinpath("workspaces.json").read_text(encoding="utf-8")
        )
        registry["workspaces"].append(
            {
                "name": "invalid",
                "root": str(invalid),
                "last_kind": "",
                "last_target": "",
                "last_agent": "",
            }
        )
        self.home.joinpath("workspaces.json").write_text(
            json.dumps(registry), encoding="utf-8"
        )
        with patch.dict(os.environ, {"DYRO_HOME": str(self.home)}, clear=False):
            result = list_workspace_observations()
        self.assertEqual(result.workspaces[0].health, "available")
        self.assertEqual(result.workspaces[1].health, "unavailable")
        self.assertEqual(result.workspaces[1].failure_code, "REGISTERED_ROOT_STALE")

    def test_observation_does_not_call_git_network_writers_or_registry_mutations(
        self,
    ) -> None:
        with patch.dict(os.environ, {"DYRO_HOME": str(self.home)}, clear=False):
            with (
                patch("dyro.process.run", side_effect=AssertionError("process")),
                patch(
                    "dyro.hub._update_registry",
                    side_effect=AssertionError("registry write"),
                ),
                patch(
                    "dyro.state.atomic_write_text", side_effect=AssertionError("write")
                ),
            ):
                result = observe_workspace(
                    workspace="sample", start=None, cwd=self.root
                )
        self.assertEqual(result.integration_inspection, "not_inspected")

    def test_workspace_root_replacement_cannot_return_complete(self) -> None:
        original_load_line = bridge_observations_module.load_line_bounded
        displaced = self.root.with_name(self.root.name + "-displaced")
        replaced = False

        def load_line_then_replace(*args, **kwargs):
            nonlocal replaced
            result = original_load_line(*args, **kwargs)
            self.root.rename(displaced)
            self.root.mkdir()
            replaced = True
            return result

        with patch(
            "dyro.bridge.observations.load_line_bounded",
            side_effect=load_line_then_replace,
        ):
            with self.assertRaises(BridgeObservationError) as raised:
                self._observe()
        self.assertTrue(replaced)
        self.assertEqual(raised.exception.error.code.value, "RECORD_INVALID")


if __name__ == "__main__":
    import unittest

    unittest.main()

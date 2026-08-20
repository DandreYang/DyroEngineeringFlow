from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
import unittest
from unittest.mock import patch

from dyro.config import load
from dyro.console.inspection import IsolatedOverviewService
from dyro.console.overview import ConsoleOverviewService
from dyro.console.twin import empty_operator_twin, project_operator_twin
from dyro.continuation.store import create_objective
from dyro.events import append_event
from dyro.hub import add_workspace
from dyro.tasks import ledger, set_status, task_template
from dyro.workspace import create_line

from .support import WorkspaceCase


OBJECTIVE = '''schema_version = 1
id = "release"
title = "Release readiness"
line = "core"
targets = ["TASK-A"]

[continuation]
requested_mode = "supervised"
operations = ["execute", "review"]

[budget]
max_actions = 5
max_attempts_per_task = 2
max_failures = 2
max_no_progress_cycles = 2
max_parallel = 1
'''


class OperatorTwinProjectionTests(WorkspaceCase):
    def setUp(self) -> None:
        super().setUp()
        self.config = load(self.root)
        create_line(self.config, line_id="core", branch="feat/core", base="main")
        task = self.config.task_specs_dir / "TASK-A"
        task.mkdir(parents=True)
        task.joinpath("task.toml").write_text(
            task_template("TASK-A", "Pay path", "core", "api", "services/api"),
            encoding="utf-8",
        )
        task.joinpath("handoff.md").write_text("# handoff\n", encoding="utf-8")
        create_objective(self.config, OBJECTIVE)
        self.clock = lambda: datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)

    def _service(self) -> ConsoleOverviewService:
        home = self.root / "console-state"
        with patch.dict(os.environ, {"DYRO_HOME": str(home)}):
            add_workspace(self.root, name=self.config.name, make_default=True)
        from dyro.hub import load_registry_from_home

        return ConsoleOverviewService(
            registry_loader=lambda: load_registry_from_home(home),
            cursor_secret=b"k" * 32,
        )

    def test_missing_wave_dispatch_board_and_ledger_fail_closed(self) -> None:
        service = self._service()
        payload = service.workspace(self.config.name)
        twin = payload["data"]["operator_twin"]
        self.assertEqual([row["id"] for row in twin["plan"]], ["release"])
        self.assertFalse(twin["plan"][0]["wave_present"])
        self.assertEqual(twin["plan"][0]["milestone"], "incomplete")
        self.assertEqual(twin["running"], [])
        self.assertFalse(twin["latest_ledger"]["present"])
        self.assertEqual(twin["latest_ledger"]["facts"], {})
        self.assertFalse((self.root / ".dyro" / "events.lock").exists())

    def test_swimlanes_do_not_invent_objectives_from_wave_events(self) -> None:
        append_event(
            self.config,
            kind="objective_wave",
            actor="ghost",
            subject="ghost",
            family="core",
            facts={"mode": "apply", "count": 2},
            clock=self.clock,
        )
        twin = project_operator_twin(
            self.config,
            {
                "tasks": [{"id": "TASK-A", "title": "Pay path", "line": "core", "status": "backlog", "executor": "noop"}],
                "objectives": [
                    {
                        "id": "release",
                        "title": "Release readiness",
                        "line": "core",
                        "derived_result": "incomplete",
                        "selected_actions": [{"kind": "execute", "subject_id": "TASK-A", "reason": "TASK_READY"}],
                    }
                ],
            },
        )
        self.assertEqual([row["id"] for row in twin["plan"]], ["release"])
        self.assertFalse(twin["plan"][0]["wave_present"])
        self.assertEqual(twin["plan"][0]["task_ids"], ["TASK-A"])

    def test_wave_and_dispatch_and_board_project_only_from_existing_rows(self) -> None:
        item = __import__("dyro.tasks", fromlist=["load_task"]).load_task(self.config, "TASK-A")
        set_status(self.config, item, "assigned")
        set_status(self.config, item, "in_progress")
        append_event(
            self.config,
            kind="objective_wave",
            actor="release",
            subject="release",
            family="core",
            facts={"mode": "apply", "count": 1},
            clock=self.clock,
        )
        append_event(
            self.config,
            kind="dispatch",
            actor="core",
            subject="TASK-A",
            family="core",
            facts={"executor": "noop", "phase": "start"},
            clock=self.clock,
        )
        append_event(
            self.config,
            kind="board",
            actor="core",
            subject="TASK-A",
            family="core",
            facts={"result": "recorded"},
            clock=self.clock,
        )
        service = self._service()
        twin = service.workspace(self.config.name)["data"]["operator_twin"]
        self.assertTrue(twin["plan"][0]["wave_present"])
        self.assertEqual(twin["plan"][0]["wave_mode"], "apply")
        self.assertEqual(len(twin["running"]), 1)
        self.assertEqual(twin["running"][0]["id"], "TASK-A")
        self.assertEqual(twin["running"][0]["executor"], "codex")
        self.assertTrue(twin["running"][0]["dispatch_present"])
        self.assertEqual(twin["running"][0]["dispatch_state"], "running")
        self.assertTrue(twin["running"][0]["board_landed"])
        in_progress = next(column for column in twin["phases"] if column["status"] == "in_progress")
        self.assertEqual([task["id"] for task in in_progress["tasks"]], ["TASK-A"])

    def test_who_is_running_does_not_claim_board_without_board_event(self) -> None:
        item = __import__("dyro.tasks", fromlist=["load_task"]).load_task(self.config, "TASK-A")
        set_status(self.config, item, "assigned")
        set_status(self.config, item, "in_progress")
        append_event(
            self.config,
            kind="dispatch",
            actor="core",
            subject="TASK-A",
            family="core",
            facts={"executor": "noop", "phase": "end", "status": "idle"},
            clock=self.clock,
        )
        twin = project_operator_twin(
            self.config,
            {
                "tasks": [
                    {
                        "id": "TASK-A",
                        "title": "Pay path",
                        "line": "core",
                        "status": "in_progress",
                        "executor": "noop",
                    }
                ],
                "objectives": [],
            },
        )
        self.assertEqual(twin["running"][0]["dispatch_state"], "idle")
        self.assertFalse(twin["running"][0]["board_landed"])

    def test_who_is_running_does_not_claim_a_board_for_another_task(self) -> None:
        item = __import__("dyro.tasks", fromlist=["load_task"]).load_task(self.config, "TASK-A")
        set_status(self.config, item, "assigned")
        set_status(self.config, item, "in_progress")
        other = self.config.task_specs_dir / "TASK-B"
        other.mkdir(parents=True)
        other.joinpath("task.toml").write_text(
            task_template("TASK-B", "Other path", "core", "api", "services/api"),
            encoding="utf-8",
        )
        other.joinpath("handoff.md").write_text("# handoff\n", encoding="utf-8")
        inventory = {
            "tasks": [
                {
                    "id": "TASK-A",
                    "title": "Pay path",
                    "line": "core",
                    "status": "in_progress",
                    "executor": "noop",
                },
                {
                    "id": "TASK-B",
                    "title": "Other path",
                    "line": "core",
                    "status": "backlog",
                    "executor": "noop",
                },
            ],
            "objectives": [],
        }
        for subject, facts in (
            ("ghost-task", {"result": "recorded"}),
            ("TASK-B", {"task_id": "TASK-B", "result": "recorded"}),
        ):
            with self.subTest(subject=subject):
                append_event(
                    self.config,
                    kind="board",
                    actor="core",
                    subject=subject,
                    family="core",
                    facts=facts,
                    clock=self.clock,
                )
                twin = project_operator_twin(self.config, inventory)
                self.assertEqual(twin["running"][0]["id"], "TASK-A")
                self.assertFalse(twin["running"][0]["board_landed"])

    def test_truncated_events_and_ledger_fail_closed(self) -> None:
        append_event(
            self.config,
            kind="objective_wave",
            actor="release",
            subject="release",
            family="core",
            facts={"mode": "apply", "count": 1},
            clock=self.clock,
        )
        events = self.root / ".dyro" / "events.jsonl"
        events.write_text(events.read_text(encoding="utf-8").rstrip("\n"), encoding="utf-8")
        ledger(
            self.config,
            "TASK-A",
            "execution_heads",
            task_heads_sha256="a" * 64,
            prompt="do not leak",
            argv="/usr/bin/true",
        )
        ledger_path = self.config.ledger_file
        ledger_path.write_text(ledger_path.read_text(encoding="utf-8") + '{"ts":', encoding="utf-8")
        inventory = {
            "tasks": [
                {
                    "id": "TASK-A",
                    "title": "Pay path",
                    "line": "core",
                    "status": "in_progress",
                    "executor": "noop",
                }
            ],
            "objectives": [
                {
                    "id": "release",
                    "title": "Release readiness",
                    "line": "core",
                    "derived_result": "complete",
                    "selected_actions": [],
                }
            ],
        }
        twin = project_operator_twin(self.config, inventory)
        self.assertEqual(twin["plan"][0]["milestone"], "complete")
        self.assertFalse(twin["plan"][0]["wave_present"])
        self.assertFalse(twin["running"][0]["dispatch_present"])
        self.assertFalse(twin["running"][0]["board_landed"])
        self.assertFalse(twin["latest_ledger"]["present"])
        self.assertFalse(twin["overlay_complete"])
        got = self._service().workspace(self.config.name)["data"]["operator_twin"]
        self.assertFalse(got["overlay_complete"])

    def test_latest_ledger_line_is_redacted(self) -> None:
        ledger(
            self.config,
            "TASK-A",
            "execution_heads",
            task_heads_sha256="ab" * 32,
            prompt="secret prompt",
            argv="/tmp/bin --yes",
            error=str(self.root / "private.log"),
            parent="core",
            child="core_pay",
        )
        twin = project_operator_twin(self.config, {"tasks": [], "objectives": []})
        row = twin["latest_ledger"]
        self.assertTrue(row["present"])
        self.assertEqual(row["task_id"], "TASK-A")
        self.assertEqual(row["phase"], "execution_heads")
        self.assertEqual(row["facts"]["parent"], "core")
        self.assertEqual(row["facts"]["child"], "core_pay")
        rendered = json.dumps(row)
        self.assertNotIn("secret prompt", rendered)
        self.assertNotIn("/tmp/bin", rendered)
        self.assertNotIn("private.log", rendered)
        self.assertNotIn("--yes", rendered)
        self.assertNotIn(str(self.root), rendered)

    def test_latest_ledger_drops_blocked_keys_even_when_values_are_safe_tokens(self) -> None:
        ledger(
            self.config,
            "TASK-A",
            "execution_heads",
            prompt="apply",
            argv="codex",
            parent="core",
        )
        twin = project_operator_twin(self.config, {"tasks": [], "objectives": []})
        facts = twin["latest_ledger"]["facts"]
        self.assertNotIn("prompt", facts)
        self.assertNotIn("argv", facts)
        self.assertEqual(facts.get("parent"), "core")

    def test_milestone_does_not_invent_a_fourth_state(self) -> None:
        twin = project_operator_twin(
            self.config,
            {
                "tasks": [],
                "objectives": [
                    {
                        "id": "release",
                        "title": "Release readiness",
                        "line": "core",
                        "derived_result": "shipped",
                        "selected_actions": [],
                    }
                ],
            },
        )
        self.assertEqual(twin["plan"][0]["milestone"], "")

    def test_workspace_read_does_not_create_overlay_lock_or_ledger(self) -> None:
        service = self._service()
        before = {path.relative_to(self.root) for path in self.root.rglob("*") if path.is_file()}
        service.workspace(self.config.name)
        after = {path.relative_to(self.root) for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_workspace_get_does_not_append_events_or_ledger_in_place(self) -> None:
        append_event(
            self.config,
            kind="spawn",
            actor="core",
            subject="core_pay",
            family="core",
            facts={"parent": "core", "child": "core_pay"},
            clock=self.clock,
        )
        ledger(
            self.config,
            "TASK-A",
            "execution_heads",
            task_heads_sha256="ab" * 32,
            parent="core",
        )
        events = self.root / ".dyro" / "events.jsonl"
        ledger_path = self.config.ledger_file
        before_events = hashlib.sha256(events.read_bytes()).hexdigest()
        before_ledger = hashlib.sha256(ledger_path.read_bytes()).hexdigest()
        self._service().workspace(self.config.name)
        self.assertEqual(hashlib.sha256(events.read_bytes()).hexdigest(), before_events)
        self.assertEqual(hashlib.sha256(ledger_path.read_bytes()).hexdigest(), before_ledger)

    def test_task_status_events_do_not_invent_phase_cards(self) -> None:
        append_event(
            self.config,
            kind="task_status",
            actor="core",
            subject="ghost-task",
            family="core",
            facts={"from_status": "backlog", "to_status": "in_progress"},
            clock=self.clock,
        )
        twin = project_operator_twin(
            self.config,
            {
                "tasks": [
                    {
                        "id": "TASK-A",
                        "title": "Pay path",
                        "line": "core",
                        "status": "backlog",
                        "executor": "noop",
                    }
                ],
                "objectives": [],
            },
        )
        backlog = next(column for column in twin["phases"] if column["status"] == "backlog")
        in_progress = next(column for column in twin["phases"] if column["status"] == "in_progress")
        self.assertEqual([task["id"] for task in backlog["tasks"]], ["TASK-A"])
        self.assertEqual(in_progress["tasks"], [])
        self.assertEqual(twin["running"], [])

    def test_empty_twin_shape_is_stable(self) -> None:
        twin = empty_operator_twin()
        self.assertEqual(
            set(twin),
            {"plan", "phases", "running", "latest_ledger", "projected_seq", "overlay_complete"},
        )
        self.assertEqual(twin["projected_seq"], 0)
        self.assertFalse(twin["overlay_complete"])
        self.assertEqual([column["status"] for column in twin["phases"]], [
            "backlog",
            "assigned",
            "in_progress",
            "waiting_answer",
            "review",
            "review_pending_signoff",
            "done",
            "failed",
        ])


class IsolatedOperatorTwinTests(WorkspaceCase):
    def setUp(self) -> None:
        super().setUp()
        self.home = self.root / "console-state"
        self.environment = patch.dict(os.environ, {"DYRO_HOME": str(self.home)})
        self.environment.start()
        add_workspace(self.root, name="demo", make_default=True)

    def tearDown(self) -> None:
        self.environment.stop()
        super().tearDown()

    def test_isolated_workspace_accepts_operator_twin(self) -> None:
        service = IsolatedOverviewService(
            registry_state_home=self.home,
            timeout_seconds=5,
            cursor_secret=b"q" * 32,
        )
        payload = service.workspace("demo")
        twin = payload["data"]["operator_twin"]
        self.assertEqual(
            set(twin),
            {"plan", "phases", "running", "latest_ledger", "projected_seq", "overlay_complete"},
        )
        self.assertTrue(twin["overlay_complete"])
        self.assertEqual(twin["projected_seq"], 0)
        self.assertFalse(twin["latest_ledger"]["present"])
        self.assertNotIn(str(self.root), repr(payload))


if __name__ == "__main__":
    unittest.main()

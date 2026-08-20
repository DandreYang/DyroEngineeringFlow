from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from dyro.config import load
from dyro.console.overview import ConsoleOverviewService
from dyro.console.twin import TASK_STATUSES
from dyro.continuation.store import create_objective
from dyro.events import append_event, read_overlay_events
from dyro.hub import add_workspace
from dyro.tasks import load_task, set_status, task_template
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


HARNESS = Path(__file__).resolve().parent / "support" / "console_twin_live.mjs"
APP_JS = Path(__file__).resolve().parents[1] / "src" / "dyro" / "console" / "assets" / "app.js"


def _task_card(task_id: str, status: str) -> dict[str, str]:
    return {
        "id": task_id,
        "title": "Pay path",
        "line": "core",
        "executor": "noop",
        "status": status,
    }


def _empty_phases(done_id: str = "") -> list[dict[str, object]]:
    phases = [{"status": status, "tasks": []} for status in TASK_STATUSES]
    if done_id:
        done = next(column for column in phases if column["status"] == "done")
        done["tasks"] = [_task_card(done_id, "done")]
    return phases


def _snapshot(*, projected_seq: int, overlay_complete: bool = True) -> dict[str, object]:
    return {
        "tasks": [
            {
                "id": "TASK-A",
                "title": "Pay path",
                "line": "core",
                "status": "done",
                "executor": "noop",
            }
        ],
        "operator_twin": {
            "plan": [
                {
                    "id": "release",
                    "title": "Release readiness",
                    "line": "core",
                    "milestone": "complete",
                    "wave_present": False,
                    "wave_id": "",
                    "wave_at": "",
                    "wave_mode": "",
                    "wave_count": 0,
                    "task_ids": ["TASK-A"],
                }
            ],
            "phases": _empty_phases("TASK-A"),
            "running": [],
            "latest_ledger": {
                "present": False,
                "at": "",
                "task_id": "",
                "phase": "",
                "facts": {},
            },
            "projected_seq": projected_seq,
            "overlay_complete": overlay_complete,
        },
    }


def _event(seq: int, kind: str, subject: str, facts: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "seq": seq,
        "id": f"evt_{seq}",
        "kind": kind,
        "at": "2026-08-20T12:00:00Z",
        "actor": "core",
        "subject": subject,
        "family": "core",
        "facts": facts or {},
    }


def _prefix_page() -> list[dict[str, object]]:
    """Oldest 50 rows: early in_progress + board, then fillers."""
    events = [
        _event(1, "task_status", "TASK-A", {"from_status": "assigned", "to_status": "in_progress"}),
        _event(2, "board", "TASK-A", {"result": "recorded"}),
    ]
    for seq in range(3, 51):
        events.append(_event(seq, "spawn", "core_pay", {"parent": "core", "child": "core_pay"}))
    return events


def _run_live(payload: dict[str, object]) -> dict[str, object]:
    node = shutil.which("node")
    if not node:
        raise AssertionError("node is required to exercise mergeTwinFromEvents")
    completed = subprocess.run(
        [node, str(HARNESS)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout or "twin live harness failed")
    return json.loads(completed.stdout)


class MergeTwinFromEventsTests(unittest.TestCase):
    def test_ghost_wave_does_not_push_a_lane(self) -> None:
        snapshot = _snapshot(projected_seq=0, overlay_complete=True)
        result = _run_live(
            {
                "snapshot": snapshot,
                "events": [
                    _event(1, "objective_wave", "ghost", {"mode": "apply", "count": 2}),
                ],
            }
        )
        self.assertEqual(result["plan_ids"], ["release"])
        self.assertEqual(result["wave_ids"], [])

    def test_board_for_other_or_non_running_task_does_not_land(self) -> None:
        snapshot = _snapshot(projected_seq=0, overlay_complete=True)
        in_progress = next(
            column
            for column in snapshot["operator_twin"]["phases"]
            if column["status"] == "in_progress"
        )
        done = next(
            column
            for column in snapshot["operator_twin"]["phases"]
            if column["status"] == "done"
        )
        done["tasks"] = []
        in_progress["tasks"] = [_task_card("TASK-A", "in_progress")]
        snapshot["tasks"][0]["status"] = "in_progress"
        snapshot["operator_twin"]["running"] = [
            {
                "id": "TASK-A",
                "title": "Pay path",
                "line": "core",
                "executor": "noop",
                "dispatch_present": False,
                "dispatch_id": "",
                "dispatch_at": "",
                "dispatch_state": "unknown",
                "dispatch_facts": {},
                "board_landed": False,
            }
        ]
        for subject in ("ghost-task", "TASK-B"):
            with self.subTest(subject=subject):
                result = _run_live(
                    {
                        "snapshot": snapshot,
                        "events": [_event(1, "board", subject, {"task_id": subject})],
                    }
                )
                self.assertEqual(len(result["running"]), 1)
                self.assertFalse(result["board_landed"])
                self.assertNotIn("会审已落下", result["rendered"])

    def test_harness_loads_the_page_merge_functions(self) -> None:
        script = APP_JS.read_text(encoding="utf-8")
        self.assertIn("function mergeTwinFromEvents", script)
        self.assertIn("function twinFromData", script)
        self.assertIn("function renderOperatorTwin", script)
        self.assertIn("function applyLiveTwinEvents", script)
        self.assertIn("source.overlay_complete === true", script)
        self.assertIn("state.twinAfterSeq = state.operatorTwin.projected_seq", script)
        self.assertIn("overlayComplete !== true", script)
        self.assertIn("seq <= floor", script)


class GetTwinFloorBindTests(WorkspaceCase):
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

    def _early_in_progress_and_board(self) -> None:
        item = load_task(self.config, "TASK-A")
        set_status(self.config, item, "assigned")
        set_status(self.config, item, "in_progress")
        append_event(
            self.config,
            kind="board",
            actor="core",
            subject="TASK-A",
            family="core",
            facts={"result": "recorded"},
            clock=self.clock,
        )

    def _force_inventory_done(self) -> None:
        item = load_task(self.config, "TASK-A")
        (item.directory / "status").write_text("done\n", encoding="utf-8")

    def _assert_prefix_does_not_invent_running(self, result: dict[str, object]) -> None:
        self.assertEqual(result["running"], [])
        self.assertEqual(result["done_ids"], ["TASK-A"])
        self.assertFalse(result["board_landed"])
        self.assertNotIn("会审已落下", result["rendered"])

    def test_workspace_projected_seq_is_the_live_floor_for_the_first_page(self) -> None:
        self._early_in_progress_and_board()
        for _ in range(52):
            append_event(
                self.config,
                kind="spawn",
                actor="core",
                subject="core_pay",
                family="core",
                facts={"parent": "core", "child": "core_pay"},
                clock=self.clock,
            )
        self._force_inventory_done()
        records, complete = read_overlay_events(self.config)
        self.assertTrue(complete)
        self.assertGreater(len(records), 50)
        last_seq = records[-1]["seq"]
        self.assertEqual(last_seq, len(records))
        self.assertGreater(last_seq, 50)

        service = self._service()
        payload = service.workspace(self.config.name)
        data = payload["data"]
        twin = data["operator_twin"]
        self.assertTrue(twin["overlay_complete"])
        self.assertEqual(twin["projected_seq"], last_seq)
        self.assertEqual(twin["running"], [])
        done = next(column for column in twin["phases"] if column["status"] == "done")
        self.assertEqual([task["id"] for task in done["tasks"]], ["TASK-A"])

        first_page = service.events(self.config.name)["data"]["events"]
        self.assertEqual(len(first_page), 50)
        self.assertTrue(all(event["seq"] <= 50 for event in first_page))
        self.assertTrue(
            any(
                event["kind"] == "task_status" and event["facts"].get("to_status") == "in_progress"
                for event in first_page
            )
        )
        self.assertTrue(any(event["kind"] == "board" and event["subject"] == "TASK-A" for event in first_page))

        result = _run_live({"snapshot": data, "events": first_page})
        self._assert_prefix_does_not_invent_running(result)
        self.assertEqual(result["after_seq"], last_seq)
        self.assertEqual(result["projected_seq"], last_seq)

    def test_truncated_workspace_get_fail_closes_before_prefix_merge(self) -> None:
        self._early_in_progress_and_board()
        events = self.root / ".dyro" / "events.jsonl"
        events.write_text(events.read_text(encoding="utf-8").rstrip("\n"), encoding="utf-8")
        self._force_inventory_done()
        service = self._service()
        payload = service.workspace(self.config.name)
        data = payload["data"]
        twin = data["operator_twin"]
        self.assertFalse(twin["overlay_complete"])
        self.assertEqual(twin["running"], [])
        done = next(column for column in twin["phases"] if column["status"] == "done")
        self.assertEqual([task["id"] for task in done["tasks"]], ["TASK-A"])

        first_page = _prefix_page()
        self.assertEqual(len(first_page), 50)
        result = _run_live({"snapshot": data, "events": first_page})
        self._assert_prefix_does_not_invent_running(result)
        self.assertFalse(result["overlay_complete"])


if __name__ == "__main__":
    unittest.main()

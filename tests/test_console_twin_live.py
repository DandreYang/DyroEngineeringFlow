from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

from dyro.console.twin import TASK_STATUSES


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
    """Oldest 50 rows: early in_progress + board, then fillers. Later done is seq 54."""
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
    def test_prefix_page_does_not_undo_snapshot_done_or_claim_board(self) -> None:
        first_page = _prefix_page()
        self.assertEqual(len(first_page), 50)
        self.assertEqual(first_page[0]["facts"]["to_status"], "in_progress")
        self.assertEqual(first_page[1]["kind"], "board")
        self.assertTrue(all(event["seq"] <= 50 for event in first_page))
        later_done_seq = 54
        self.assertGreater(later_done_seq, 50)
        self.assertGreaterEqual(later_done_seq - 2, 51)

        result = _run_live(
            {
                "snapshot": _snapshot(projected_seq=later_done_seq, overlay_complete=True),
                "events": first_page,
            }
        )
        self.assertEqual(result["running"], [])
        self.assertEqual(result["done_ids"], ["TASK-A"])
        self.assertFalse(result["board_landed"])
        self.assertNotIn("会审已落下", result["rendered"])

    def test_truncated_overlay_fail_closes_to_snapshot_not_prefix(self) -> None:
        result = _run_live(
            {
                "snapshot": _snapshot(projected_seq=0, overlay_complete=False),
                "events": _prefix_page(),
                "overlay_complete": False,
                "after_seq": 0,
            }
        )
        self.assertEqual(result["running"], [])
        self.assertEqual(result["done_ids"], ["TASK-A"])
        self.assertNotIn("会审已落下", result["rendered"])

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
        self.assertIn("function applyLiveTwinEvents", script)
        self.assertIn("overlayComplete !== true", script)
        self.assertIn("seq <= floor", script)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import unittest
from unittest.mock import patch

from dyro.config import load
from dyro.continuation.actions import ActionStatus
from dyro.continuation.store import (
    create_objective,
    get_objective_action,
    list_objective_actions,
    pause_objective,
    resume_objective,
    start_objective_action as store_start_objective_action,
)
from dyro.continuation.supervision import apply_supervised_wave, build_supervised_wave
from dyro.errors import DyroError
from dyro.tasks import load_task, set_status, status, task_template
from dyro.workspace import create_line

from .support import WorkspaceCase


def _contract(*, max_actions: int = 20) -> str:
    return f'''schema_version = 1
id = "release"
title = "Release"
line = "alpha"
targets = ["TASK-A"]

[continuation]
requested_mode = "supervised"
operations = ["execute", "review"]

[budget]
max_actions = {max_actions}
max_attempts_per_task = 2
max_failures = 3
max_no_progress_cycles = 2
max_parallel = 1
'''


class SupervisedContinuationTests(WorkspaceCase):
    def setUp(self) -> None:
        super().setUp()
        self.config = load(self.root)
        create_line(self.config, line_id="alpha", branch="feat/alpha", base="main")
        self.task_directory = self._write_task("TASK-A")
        self.now = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)

    def _write_task(self, task_id: str) -> Path:
        directory = self.config.task_specs_dir / task_id
        directory.mkdir(parents=True)
        directory.joinpath("task.toml").write_text(
            task_template(task_id, "Task A", "alpha", "api", "services/api").replace(
                'agent = "codex"', 'agent = "noop"'
            ),
            encoding="utf-8",
        )
        directory.joinpath("handoff.md").write_text("# handoff\n", encoding="utf-8")
        return directory

    def _wave(self, *, max_actions: int = 20):
        create_objective(self.config, _contract(max_actions=max_actions))
        return build_supervised_wave(self.config, "release", clock=lambda: self.now)

    def test_action_start_precedes_task_api_and_success_receipt_is_bound(self) -> None:
        wave = self._wave()

        def invoke(config, task, *, expected_contract_sha256, **_unused):
            records = list_objective_actions(config, "release")
            self.assertEqual(len(records), 1)
            self.assertIsNotNone(records[0].start)
            self.assertEqual(len(expected_contract_sha256), 64)
            return "review"

        with patch("dyro.continuation.supervision.run_task", side_effect=invoke):
            outcomes = apply_supervised_wave(self.config, wave, clock=lambda: self.now)

        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0].status, ActionStatus.SUCCEEDED)
        record = get_objective_action(self.config, "release", outcomes[0].action_id)
        self.assertIsNotNone(record.start)
        self.assertEqual(record.receipt.status, ActionStatus.SUCCEEDED)
        self.assertEqual(record.intent.budget_reservation.attempts, 1)

    def test_supervised_execute_uses_the_real_local_task_path_after_action_start(self) -> None:
        self.task_directory.joinpath("receipt.md").write_text("result: DONE\n", encoding="utf-8")
        wave = self._wave()

        outcomes = apply_supervised_wave(self.config, wave, clock=lambda: self.now)

        self.assertEqual([(item.status, item.result) for item in outcomes], [(ActionStatus.SUCCEEDED, "review")])
        self.assertEqual(status(self.config, load_task(self.config, "TASK-A")), "review")
        record = get_objective_action(self.config, "release", outcomes[0].action_id)
        self.assertIsNotNone(record.start)
        self.assertEqual(record.receipt.status, ActionStatus.SUCCEEDED)

    def test_contract_drift_before_apply_creates_no_action_or_owner_lease(self) -> None:
        wave = self._wave()
        self.task_directory.joinpath("task.toml").write_text(
            self.task_directory.joinpath("task.toml").read_text(encoding="utf-8") + "\n# drift\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(DyroError, "没有可由受监督阶段执行"):
            apply_supervised_wave(self.config, wave, clock=lambda: self.now)

        objective_dir = self.config.objectives_dir / "release"
        self.assertEqual(list_objective_actions(self.config, "release"), ())
        self.assertFalse(objective_dir.joinpath("scheduler-owner.json").exists())

    def test_objective_event_change_before_apply_creates_no_action_or_owner_lease(self) -> None:
        wave = self._wave()
        pause_objective(self.config, "release")
        resume_objective(self.config, "release")

        with self.assertRaisesRegex(DyroError, "语义变化"):
            apply_supervised_wave(self.config, wave, clock=lambda: self.now)

        objective_dir = self.config.objectives_dir / "release"
        self.assertEqual(list_objective_actions(self.config, "release"), ())
        self.assertFalse(objective_dir.joinpath("scheduler-owner.json").exists())

    def test_task_exception_after_action_start_is_uncertain_and_blocks_replay(self) -> None:
        wave = self._wave()
        with patch("dyro.continuation.supervision.run_task", side_effect=RuntimeError("runner interrupted")):
            outcomes = apply_supervised_wave(self.config, wave, clock=lambda: self.now)

        self.assertEqual([(item.status, item.result) for item in outcomes], [(ActionStatus.UNCERTAIN, "uncertain")])
        record = get_objective_action(self.config, "release", outcomes[0].action_id)
        self.assertEqual(record.receipt.status, ActionStatus.UNCERTAIN)
        with self.assertRaisesRegex(DyroError, "uncertain"):
            apply_supervised_wave(self.config, wave, clock=lambda: self.now)

    def test_keyboard_interrupt_after_action_start_writes_uncertain_receipt_then_reraises(self) -> None:
        wave = self._wave()
        with patch("dyro.continuation.supervision.run_task", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                apply_supervised_wave(self.config, wave, clock=lambda: self.now)

        records = list_objective_actions(self.config, "release")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].receipt.status, ActionStatus.UNCERTAIN)

    def test_keyboard_interrupt_after_start_write_recovers_uncertain_then_reraises(self) -> None:
        wave = self._wave()

        def interrupt_after_start(*args, **kwargs):
            store_start_objective_action(*args, **kwargs)
            raise KeyboardInterrupt

        with patch("dyro.continuation.supervision.start_objective_action", side_effect=interrupt_after_start):
            with self.assertRaises(KeyboardInterrupt):
                apply_supervised_wave(self.config, wave, clock=lambda: self.now)

        records = list_objective_actions(self.config, "release")
        self.assertEqual(len(records), 1)
        self.assertIsNotNone(records[0].start)
        self.assertEqual(records[0].receipt.status, ActionStatus.UNCERTAIN)

    def test_confirmation_digest_is_stable_across_sampling_time_but_changes_with_task_state(self) -> None:
        create_objective(self.config, _contract())
        first = build_supervised_wave(self.config, "release", clock=lambda: self.now)
        later = build_supervised_wave(
            self.config,
            "release",
            clock=lambda: datetime(2026, 8, 4, 12, 1, tzinfo=timezone.utc),
        )
        self.assertNotEqual(first.tick_sha256, later.tick_sha256)
        self.assertEqual(first.confirmation_sha256, later.confirmation_sha256)

        set_status(self.config, load_task(self.config, "TASK-A"), "assigned")
        changed = build_supervised_wave(self.config, "release", clock=lambda: self.now)
        self.assertNotEqual(first.confirmation_sha256, changed.confirmation_sha256)

    def test_started_action_consumes_budget_before_any_replay(self) -> None:
        wave = self._wave(max_actions=1)
        with patch("dyro.continuation.supervision.run_task", return_value="review") as runner:
            first = apply_supervised_wave(self.config, wave, clock=lambda: self.now)
            replay = build_supervised_wave(self.config, "release", clock=lambda: self.now)
            with self.assertRaisesRegex(DyroError, "ACTION_LIMIT"):
                apply_supervised_wave(self.config, replay, clock=lambda: self.now)

        self.assertEqual(first[0].status, ActionStatus.SUCCEEDED)
        self.assertEqual(runner.call_count, 1)

    def test_supervised_apply_ignores_untrusted_usage_even_with_provider_cap(self) -> None:
        path = self.root / "dyro.toml"
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                'name = "test-workspace"',
                'name = "test-workspace"\nmax_provider_usage = 100',
            ),
            encoding="utf-8",
        )
        self.config = load(self.root)
        self.task_directory.joinpath("receipt.md").write_text(
            "result: DONE\n", encoding="utf-8"
        )
        wave = self._wave()
        outcomes = apply_supervised_wave(self.config, wave, clock=lambda: self.now)
        self.assertEqual(
            [(item.status, item.result) for item in outcomes],
            [(ActionStatus.SUCCEEDED, "review")],
        )


if __name__ == "__main__":
    unittest.main()

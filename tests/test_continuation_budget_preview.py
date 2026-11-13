from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from io import StringIO
import inspect
import json
from pathlib import Path

from dyro.cli import main
from dyro.config import load
from dyro.continuation.models import ActionKind, PlannedAction, ReasonCode
from dyro.continuation.store import (
    create_objective,
    list_objective_actions,
    preview_objective_wave_budgets,
    render_budget_preview_text,
    reserve_supervised_objective_action,
)
from dyro.tasks import task_template
from dyro.workspace import create_line

from .support import WorkspaceCase


def _contract(*, mode: str) -> str:
    return f'''schema_version = 1
id = "release"
title = "Release"
line = "alpha"
targets = ["TASK-A"]

[continuation]
requested_mode = "{mode}"
operations = ["execute", "review"]

[budget]
max_actions = 20
max_attempts_per_task = 2
max_failures = 3
max_no_progress_cycles = 2
max_parallel = 1
'''


class AutomaticBudgetPreviewTests(WorkspaceCase):
    def setUp(self) -> None:
        super().setUp()
        self.now = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)

    def _write_task(self, config, task_id: str = "TASK-A") -> Path:
        directory = config.task_specs_dir / task_id
        directory.mkdir(parents=True)
        directory.joinpath("task.toml").write_text(
            task_template(task_id, "Task A", "alpha", "api", "services/api").replace(
                'agent = "codex"', 'agent = "noop"'
            ),
            encoding="utf-8",
        )
        directory.joinpath("handoff.md").write_text("# handoff\n", encoding="utf-8")
        return directory

    def _prepare(self, *, mode: str, provider_cap: int | None = None):
        if provider_cap is not None:
            path = self.root / "dyro.toml"
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    'name = "test-workspace"',
                    f'name = "test-workspace"\nmax_provider_usage = {provider_cap}',
                ),
                encoding="utf-8",
            )
        config = load(self.root)
        create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        self._write_task(config)
        record = create_objective(config, _contract(mode=mode))
        return config, record

    def _execute_action(self) -> PlannedAction:
        return PlannedAction(
            kind=ActionKind.EXECUTE_TASK,
            subject_id="TASK-A",
            reason=ReasonCode.TASK_READY,
        )

    def _run_cli(self, *argv: str) -> tuple[int, str, str]:
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                main(["--root", str(self.root), *argv])
                code = 0
            except SystemExit as exc:
                code = 0 if exc.code is None else int(exc.code)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_automatic_untrusted_usage_hard_stops_only_when_cap_exists(self) -> None:
        config, record = self._prepare(mode="automatic", provider_cap=100)
        preview = preview_objective_wave_budgets(
            config,
            objective=record.objective,
            actions=(self._execute_action(),),
            now=self.now,
        )
        self.assertTrue(preview["automatic"])
        self.assertEqual(preview["provider_cap"], 100)
        self.assertFalse(preview["reserved"])
        self.assertEqual(preview["actions"][0]["reasons"], ["PROVIDER_USAGE_UNTRUSTED"])
        self.assertFalse(preview["actions"][0]["allowed"])
        self.assertEqual(list_objective_actions(config, "release"), ())

    def test_supervised_preview_does_not_invent_untrusted_hard_stop(self) -> None:
        config, record = self._prepare(mode="supervised", provider_cap=100)
        preview = preview_objective_wave_budgets(
            config,
            objective=record.objective,
            actions=(self._execute_action(),),
            now=self.now,
        )
        self.assertFalse(preview["automatic"])
        self.assertTrue(preview["actions"][0]["allowed"])
        self.assertEqual(preview["actions"][0]["reasons"], [])

    def test_automatic_without_cap_does_not_hard_stop(self) -> None:
        config, record = self._prepare(mode="automatic")
        preview = preview_objective_wave_budgets(
            config,
            objective=record.objective,
            actions=(self._execute_action(),),
            now=self.now,
        )
        self.assertTrue(preview["automatic"])
        self.assertIsNone(preview["provider_cap"])
        self.assertTrue(preview["actions"][0]["allowed"])
        self.assertIn("没有工作区 provider cap", "\n".join(render_budget_preview_text(preview)))

    def test_trusted_card_allows_automatic_preview_when_cap_exists(self) -> None:
        path = self.root / "dyro.toml"
        path.write_text(
            path.read_text(encoding="utf-8")
            + """

[[capabilities]]
id = "metered"
kind = "agent"
launch = ["/usr/bin/true"]
read = ["/usr/bin/true"]
write = ["/usr/bin/true"]
trusted_usage = true
""",
            encoding="utf-8",
        )
        config, record = self._prepare(mode="automatic", provider_cap=100)
        task_path = config.task_specs_dir / "TASK-A"
        spec = task_path.joinpath("task.toml").read_text(encoding="utf-8")
        task_path.joinpath("task.toml").write_text(
            spec.replace('agent = "noop"', 'agent = "metered"'),
            encoding="utf-8",
        )
        preview = preview_objective_wave_budgets(
            config,
            objective=record.objective,
            actions=(self._execute_action(),),
            now=self.now,
        )
        self.assertTrue(preview["actions"][0]["allowed"])
        self.assertEqual(preview["actions"][0]["reasons"], [])

    def test_tick_json_exposes_preview_without_reserving(self) -> None:
        self._prepare(mode="automatic", provider_cap=50)
        code, stdout, _stderr = self._run_cli(
            "objective", "tick", "release", "--format", "json"
        )
        self.assertEqual(code, 0)
        payload = json.loads(stdout)
        preview = payload["budget_preview"]
        self.assertTrue(preview["automatic"])
        self.assertEqual(preview["provider_cap"], 50)
        self.assertFalse(preview["reserved"])
        self.assertTrue(
            any(
                "PROVIDER_USAGE_UNTRUSTED" in item.get("reasons", [])
                for item in preview["actions"]
            )
        )
        self.assertEqual(list_objective_actions(load(self.root), "release"), ())

    def test_plan_json_exposes_the_same_preview_without_reserving(self) -> None:
        self._prepare(mode="automatic", provider_cap=50)
        code, stdout, _stderr = self._run_cli(
            "objective", "plan", "release", "--format", "json"
        )
        self.assertEqual(code, 0)
        payload = json.loads(stdout)
        preview = payload["budget_preview"]
        self.assertTrue(preview["automatic"])
        self.assertEqual(preview["provider_cap"], 50)
        self.assertFalse(preview["reserved"])
        self.assertEqual(list_objective_actions(load(self.root), "release"), ())

    def test_reserve_stays_manual_by_default(self) -> None:
        self.assertFalse(
            inspect.signature(reserve_supervised_objective_action)
            .parameters["automatic"]
            .default
        )

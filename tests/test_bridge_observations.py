from __future__ import annotations

import ast
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from unittest.mock import patch

from dyro.bridge.catalog import IMPLEMENTED_TESTABLE_IDS
from dyro.bridge.observations import (
    BridgeObservationError,
    explain_task,
    gate_definitions,
    list_lines_observation,
    list_objectives_observation,
    list_tasks_observation,
    objective_status_observation,
    observe_workspace,
    task_graph,
)
from dyro.config import load
from dyro.continuation.store import create_objective, list_objectives
from dyro.tasks import task_template
from dyro.workspace import create_line

from .support import WorkspaceCase

ROOT = Path(__file__).resolve().parents[1]
OBSERVATIONS = ROOT / "src" / "dyro" / "bridge" / "observations.py"

_CONTRACT = '''schema_version = 1
id = "observe"
title = "Observe"
line = "alpha"
targets = ["TASK-A"]

[continuation]
requested_mode = "observe"
operations = ["execute"]
'''


def _imported_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module)
            names.update(alias.name for alias in node.names)
    return names


class BridgeObservationTests(WorkspaceCase):
    def setUp(self) -> None:
        super().setUp()
        self.home = self.root / "dyro-home"
        self.home.mkdir()
        self.env = patch.dict(os.environ, {"DYRO_HOME": str(self.home)}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.config = load(self.root)
        create_line(self.config, line_id="alpha", branch="feat/alpha", base="main")
        directory = self.config.task_specs_dir / "TASK-A"
        directory.mkdir(parents=True)
        directory.joinpath("task.toml").write_text(
            task_template("TASK-A", "Task A", "alpha", "api", "services/api").replace(
                'agent = "codex"', 'agent = "noop"'
            ),
            encoding="utf-8",
        )
        directory.joinpath("handoff.md").write_text("# handoff\n", encoding="utf-8")
        create_objective(self.config, _CONTRACT)
        self.clock = lambda: datetime(2026, 8, 16, 14, 0, tzinfo=timezone.utc)

    def _blob(self, payload: object) -> str:
        return json.dumps(payload, ensure_ascii=False, default=str)

    def test_observe_workspace_redacts_paths_and_git_facts(self) -> None:
        payload = observe_workspace(
            start=self.root, workspace=None, cwd=self.root, clock=self.clock
        )
        self.assertEqual(payload["workspace"]["name"], "test-workspace")
        self.assertEqual(payload["integration_inspection"], "not_inspected")
        self.assertEqual(payload["proof_inspection"], "not_inspected")
        self.assertTrue(payload["tasks"])
        self.assertTrue(
            all(task["integration_state"] == "not_inspected" for task in payload["tasks"])
        )
        self.assertIn("observe", {item["id"] for item in payload["objectives"]})
        blob = self._blob(payload)
        self.assertNotIn(str(self.root.resolve()), blob)
        self.assertNotIn("argv", blob)
        self.assertNotIn("/usr/bin/true", blob)

    def test_lists_and_status_keep_ready_unknown(self) -> None:
        lines = list_lines_observation(self.config)
        tasks = list_tasks_observation(self.config)
        objectives = list_objectives_observation(self.config)
        status = objective_status_observation(self.config, "observe")
        self.assertEqual(lines["lines"][0]["id"], "alpha")
        self.assertEqual(tasks["integration_inspection"], "not_inspected")
        self.assertEqual(objectives["objectives"][0]["id"], "observe")
        self.assertIsNone(status["ready"])
        self.assertIsNone(status["blocked"])
        self.assertEqual(status["integration_inspection"], "not_inspected")
        self.assertNotIn(str(self.root.resolve()), self._blob((lines, tasks, objectives, status)))

    def test_objective_list_disables_recovery(self) -> None:
        with patch(
            "dyro.bridge.observations.list_objectives", wraps=list_objectives
        ) as mocked:
            list_objectives_observation(self.config)
        mocked.assert_called_once()
        self.assertFalse(mocked.call_args.kwargs["recover"])

    def test_gate_definitions_omit_argv_and_cannot_import_run_gates(self) -> None:
        payload = gate_definitions(self.config, "TASK-A")
        self.assertEqual(payload["task_id"], "TASK-A")
        self.assertEqual(payload["gates"], [{"name": "diff-check", "timeout_seconds": 120}])
        source = OBSERVATIONS.read_text(encoding="utf-8")
        self.assertNotIn("run_gates", source)
        imported = _imported_names(OBSERVATIONS)
        self.assertNotIn("subprocess", imported)
        self.assertNotIn("run_gates", imported)
        with self.assertRaises(BridgeObservationError) as ctx:
            gate_definitions(self.config, "missing")
        self.assertEqual(ctx.exception.code, "TASK_NOT_FOUND")

    def test_authoritative_git_observations_stay_unavailable(self) -> None:
        for operation in (explain_task, task_graph):
            with self.assertRaises(BridgeObservationError) as ctx:
                operation(self.config, "TASK-A")
            self.assertEqual(ctx.exception.code, "OPERATION_UNAVAILABLE")
        self.assertNotIn("task.explain", IMPLEMENTED_TESTABLE_IDS)
        self.assertNotIn("task.graph", IMPLEMENTED_TESTABLE_IDS)

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dyro.errors import ValidationError
from dyro.graph import TaskGraph, render_task_graph, validate_task_graph
from dyro.tasks import Task, plan_tasks


class TaskGraphTests(unittest.TestCase):
    @staticmethod
    def _task(
        root: Path,
        task_id: str,
        *,
        line: str = "alpha",
        depends_on: tuple[str, ...] = (),
        blocked_on: tuple[str, ...] = (),
    ) -> Task:
        directory = root / task_id
        directory.mkdir(parents=True, exist_ok=True)
        return Task(
            id=task_id,
            title=f"Task {task_id}",
            line=line,
            risk="write",
            executor="noop",
            reviewer="noop",
            repositories=("api",),
            depends_on=depends_on,
            blocked_on=blocked_on,
            directory=directory,
        )

    def test_validation_reports_structural_graph_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task_a = self._task(root, "A", depends_on=("B",))
            task_b = self._task(root, "B", depends_on=("A",))
            task_c = self._task(root, "C", depends_on=("MISSING",), blocked_on=("D-1",))
            task_d = self._task(root, "D", line="beta")
            task_e = self._task(root, "E", depends_on=("D", "D"))
            tasks = (task_a, task_b, task_c, task_d, task_e)
            graph = TaskGraph(
                line=None,
                tasks=tasks,
                known_tasks=tasks,
                decisions={},
                execution_mode="local",
            )

            codes = {issue.code for issue in validate_task_graph(graph)}
            self.assertTrue(
                {
                    "dependency_cycle",
                    "missing_dependency",
                    "missing_decision",
                    "cross_line_dependency",
                    "duplicate_dependency",
                }.issubset(codes)
            )

    def test_json_and_mermaid_are_rendered_from_the_same_graph(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task_a = self._task(root, "A")
            task_b = self._task(root, "B", depends_on=("A",))
            graph = TaskGraph(
                line="alpha",
                tasks=(task_a, task_b),
                known_tasks=(task_a, task_b),
                decisions={},
                execution_mode="local",
            )
            config = SimpleNamespace(policy=SimpleNamespace(execution_mode="local"))

            payload = json.loads(render_task_graph(config, graph, output_format="json"))
            mermaid = render_task_graph(config, graph, output_format="mermaid")

            self.assertEqual([node["id"] for node in payload["nodes"]], ["A", "B"])
            self.assertIn("flowchart LR", mermaid)
            self.assertIn('T0["A<br/>Task A<br/>[backlog]"]', mermaid)
            self.assertIn('T1["B<br/>Task B<br/>[backlog]"]', mermaid)
            self.assertIn("T0 -->|requires| T1", mermaid)

    def test_scheduler_fails_closed_when_graph_is_invalid(self) -> None:
        with (
            patch("dyro.graph.build_task_graph", return_value=SimpleNamespace()),
            patch(
                "dyro.graph.validate_task_graph",
                side_effect=ValidationError("invalid task graph"),
            ),
        ):
            with self.assertRaisesRegex(ValidationError, "invalid task graph"):
                plan_tasks(SimpleNamespace(), candidates=[])


if __name__ == "__main__":
    unittest.main()

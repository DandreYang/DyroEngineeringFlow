from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dyro.graph import TaskGraph
from dyro.tasks import Task, plan_tasks


class SchedulerSnapshotTests(unittest.TestCase):
    def test_plan_reads_each_known_task_status_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task_a = Task(
                id="A",
                title="A",
                line="alpha",
                risk="write",
                executor="noop",
                reviewer="noop",
                repositories=("api",),
                directory=root / "A",
            )
            task_b = Task(
                id="B",
                title="B",
                line="alpha",
                risk="write",
                executor="noop",
                reviewer="noop",
                repositories=("api",),
                directory=root / "B",
            )
            graph = TaskGraph(
                line=None,
                tasks=(task_a, task_b),
                known_tasks=(task_a, task_b),
                decisions={},
                execution_mode="local",
            )
            config = SimpleNamespace(policy=SimpleNamespace(execution_mode="local"))

            with (
                patch("dyro.graph.build_task_graph", return_value=graph),
                patch("dyro.graph.validate_task_graph", return_value=()),
                patch("dyro.tasks.status", return_value="backlog") as read_status,
            ):
                plan = plan_tasks(config)

            self.assertEqual([task.id for task in plan.ready], ["A", "B"])
            self.assertEqual(read_status.call_count, 2)


if __name__ == "__main__":
    unittest.main()

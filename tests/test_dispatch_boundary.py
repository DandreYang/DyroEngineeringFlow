"""Boundary tests: local agent dispatch must never act as delivery control."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from experiments.local_agent_dispatch.errors import DispatchValidationError
from experiments.local_agent_dispatch.supervisor import (
    FORBIDDEN,
    DispatchSupervisor,
    refuse_production_actions,
)


ROOT = Path(__file__).resolve().parents[1]
DISPATCH_DIR = ROOT / "experiments" / "local_agent_dispatch"


class DispatchBoundaryTests(unittest.TestCase):
    def test_forbidden_flags_are_documented(self) -> None:
        self.assertEqual(
            FORBIDDEN,
            frozenset({"merge", "push", "signoff", "import_evidence"}),
        )

    def test_refuse_production_actions_fail_closed(self) -> None:
        for key in sorted(FORBIDDEN):
            with self.subTest(key=key):
                with self.assertRaises(DispatchValidationError) as ctx:
                    refuse_production_actions({key: True})
                self.assertIn(key, str(ctx.exception))

    def test_accept_rejects_production_actions_in_payload(self) -> None:
        supervisor = DispatchSupervisor(home=Path("/tmp/dyro-dispatch-boundary-test"))
        payload = {
            "schema_version": 1,
            "mode": "read-only",
            "objective": "x",
            "files": ["README.md"],
            "backend": "echo",
            "production_actions": {"merge": True},
        }
        with self.assertRaises(DispatchValidationError) as ctx:
            supervisor.accept(payload, project_root=ROOT)
        self.assertIn("merge", str(ctx.exception))

    def test_dispatch_tree_does_not_import_dyro_merge_paths(self) -> None:
        """Static: dispatch sources must not call Core merge/signoff modules."""
        banned_modules = {
            "dyro.tasks",
            "dyro.changesets",
            "dyro.workspace",
        }
        banned_names = {"merge_task", "signoff_task", "create_changeset"}
        offenders: list[str] = []
        for path in DISPATCH_DIR.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name in banned_modules or alias.name.startswith(
                            "dyro.tasks"
                        ):
                            offenders.append(f"{path}:{alias.name}")
                elif isinstance(node, ast.ImportFrom):
                    mod = node.module or ""
                    if mod in banned_modules or mod.startswith("dyro.tasks"):
                        offenders.append(f"{path}:from {mod}")
                    for alias in node.names:
                        if alias.name in banned_names:
                            offenders.append(f"{path}:name {alias.name}")
                elif isinstance(node, ast.Attribute):
                    if isinstance(node.value, ast.Name) and node.attr in banned_names:
                        offenders.append(f"{path}:attr {node.attr}")
        self.assertEqual(offenders, [], msg=f"dispatch must not touch Core delivery: {offenders}")


if __name__ == "__main__":
    unittest.main()

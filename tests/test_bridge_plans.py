from __future__ import annotations

import ast
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from dyro.bridge.constants import PLANNER_REVISIONS
from dyro.bridge.observations import BridgeObservationError
from dyro.bridge.plans import (
    objective_attention,
    objective_explain,
    objective_graph,
    objective_plan,
    objective_tick,
)
from dyro.canonical import canonical_json_bytes
from dyro.config import load
from dyro.continuation.store import create_objective, get_objective
from dyro.tasks import task_template
from dyro.workspace import create_line

from .support import WorkspaceCase

ROOT = Path(__file__).resolve().parents[1]
PLANS = ROOT / "src" / "dyro" / "bridge" / "plans.py"

_CONTRACT = '''schema_version = 1
id = "release"
title = "Release"
line = "alpha"
targets = ["TASK-A"]

[continuation]
requested_mode = "supervised"
operations = ["execute", "review"]

[budget]
max_actions = 20
max_attempts_per_task = 2
max_failures = 3
max_no_progress_cycles = 2
max_parallel = 3
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


class BridgePlanTests(WorkspaceCase):
    def setUp(self) -> None:
        super().setUp()
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
        self.profile_bytes = (self.root / "dyro.toml").read_bytes()
        self.clock = lambda: datetime(2026, 8, 16, 15, 0, tzinfo=timezone.utc)

    def _blob(self, payload: object) -> str:
        return json.dumps(payload, ensure_ascii=False, default=str)

    def _assert_envelope(self, payload: dict[str, object], operation: str) -> None:
        self.assertIs(payload["executable"], False)
        self.assertEqual(payload["authorization"], "none")
        self.assertEqual(payload["operation"], operation)
        self.assertEqual(payload["planner_revision"], PLANNER_REVISIONS[operation])
        self.assertEqual(payload["effective_risk"], "PLAN")
        self.assertEqual(payload["read_set"]["integration_inspection"], "not_inspected")
        clone = {key: value for key, value in payload.items() if key != "plan_sha256"}
        digest = hashlib.sha256(canonical_json_bytes(clone)).hexdigest()
        self.assertEqual(payload["plan_sha256"], f"sha256:{digest}")
        self.assertNotIn(str(self.root.resolve()), self._blob(payload))

    def test_plan_digest_is_stable_and_excludes_itself(self) -> None:
        first = objective_plan(
            self.config, "release", profile_bytes=self.profile_bytes, clock=self.clock
        )
        second = objective_plan(
            self.config, "release", profile_bytes=self.profile_bytes, clock=self.clock
        )
        self._assert_envelope(first, "objective.plan")
        self.assertEqual(first["plan_sha256"], second["plan_sha256"])
        mutated = dict(first)
        mutated["planner_revision"] = "objective-plan/2"
        mutated.pop("plan_sha256")
        changed = hashlib.sha256(canonical_json_bytes(mutated)).hexdigest()
        self.assertNotEqual(first["plan_sha256"], f"sha256:{changed}")

    def test_all_plan_operations_are_non_executable(self) -> None:
        builders = {
            "objective.plan": objective_plan,
            "objective.explain": objective_explain,
            "objective.graph": objective_graph,
            "objective.tick": objective_tick,
            "objective.attention": objective_attention,
        }
        for operation, builder in builders.items():
            payload = builder(
                self.config, "release", profile_bytes=self.profile_bytes, clock=self.clock
            )
            self._assert_envelope(payload, operation)
        tick = objective_tick(
            self.config, "release", profile_bytes=self.profile_bytes, clock=self.clock
        )
        self.assertEqual(tick["projection"]["max_parallel"], 3)

    def test_missing_objective_is_not_found(self) -> None:
        with self.assertRaises(BridgeObservationError) as ctx:
            objective_plan(
                self.config, "missing", profile_bytes=self.profile_bytes, clock=self.clock
            )
        self.assertEqual(ctx.exception.code, "OBJECTIVE_NOT_FOUND")

    def test_plans_disable_recovery_and_do_not_probe_path(self) -> None:
        record = get_objective(self.config, "release", recover=False)
        self.assertEqual(record.objective.id, "release")
        source = PLANS.read_text(encoding="utf-8")
        self.assertNotIn("discover_available_write_providers", source)
        self.assertIn("inspect_integration=False", source)
        imported = _imported_names(PLANS)
        self.assertNotIn("subprocess", imported)
        self.assertNotIn("discover_available_write_providers", imported)

    def test_mutation_modules_do_not_import_bridge_plans(self) -> None:
        for relative in (
            "src/dyro/cli.py",
            "src/dyro/continuation/store.py",
            "src/dyro/tasks.py",
        ):
            imported = _imported_names(ROOT / relative)
            self.assertFalse(
                any(name == "dyro.bridge" or name.startswith("dyro.bridge") for name in imported),
                relative,
            )

from __future__ import annotations

import os
from pathlib import Path
import tempfile
from unittest.mock import patch

from dyro.config import load
from dyro.continuation.resolution import resolve_line, resolve_objective, resolve_workspace
from dyro.continuation.store import create_objective
from dyro.errors import DyroError, ValidationError
from dyro.hub import add_workspace
from dyro.tasks import task_template
from dyro.workspace import create_line, line_root

from .support import CONFIG, WorkspaceCase


class ContinuationResolutionTests(WorkspaceCase):
    def setUp(self) -> None:
        super().setUp()
        self.config = load(self.root)
        create_line(self.config, line_id="alpha", branch="feat/alpha", base="main")
        self._write_task("TASK-A")

    def _write_task(self, task_id: str) -> None:
        directory = self.config.task_specs_dir / task_id
        directory.mkdir(parents=True)
        directory.joinpath("task.toml").write_text(
            task_template(task_id, task_id, "alpha", "api", "services/api").replace('agent = "codex"', 'agent = "noop"'),
            encoding="utf-8",
        )
        directory.joinpath("handoff.md").write_text("# handoff\n", encoding="utf-8")

    def test_resolves_current_line_then_requires_explicit_non_interactive_ambiguity(self) -> None:
        inside_line = line_root(self.config, create_line(self.config, line_id="beta", branch="feat/beta", base="main"))
        self.assertEqual(resolve_line(self.config, start=inside_line, interactive=False).id, "beta")
        with self.assertRaisesRegex(DyroError, "非交互模式必须显式指定"):
            resolve_line(self.config, start=self.root / "unrelated", interactive=False)

    def test_resolves_registered_default_workspace_from_unrelated_directory(self) -> None:
        home = self.root / "dyro-home"
        with patch.dict(os.environ, {"DYRO_HOME": str(home)}, clear=False):
            add_workspace(self.root, name="sample", make_default=True)
            self.assertEqual(
                resolve_workspace(start=self.root / "unrelated", interactive=False).root,
                self.root.resolve(),
            )
            self.assertEqual(
                resolve_workspace(workspace="sample", interactive=False).name,
                "test-workspace",
            )

    def test_malformed_local_profile_never_falls_back_to_registry_default(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dyro-resolution-") as tmp:
            fallback = Path(tmp) / "fallback"
            fallback.mkdir()
            (fallback / "dyro.toml").write_text(CONFIG, encoding="utf-8")
            home = self.root / "dyro-home"
            with patch.dict(os.environ, {"DYRO_HOME": str(home)}, clear=False):
                add_workspace(fallback, name="fallback", make_default=True)
                (self.root / "dyro.toml").write_text("not valid = [", encoding="utf-8")
                with self.assertRaises(ValidationError):
                    resolve_workspace(start=self.root, interactive=False)

    def test_dangling_local_profile_symlink_never_falls_back_to_registry_default(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dyro-resolution-") as tmp:
            fallback = Path(tmp) / "fallback"
            fallback.mkdir()
            (fallback / "dyro.toml").write_text(CONFIG, encoding="utf-8")
            home = self.root / "dyro-home"
            with patch.dict(os.environ, {"DYRO_HOME": str(home)}, clear=False):
                add_workspace(fallback, name="fallback", make_default=True)
                profile = self.root / "dyro.toml"
                profile.unlink()
                profile.symlink_to(self.root / "missing-profile.toml")
                with self.assertRaisesRegex(ValidationError, "安全的普通文件"):
                    resolve_workspace(start=self.root, interactive=False)

    def test_local_profile_directory_never_falls_back_to_registry_default(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dyro-resolution-") as tmp:
            fallback = Path(tmp) / "fallback"
            fallback.mkdir()
            (fallback / "dyro.toml").write_text(CONFIG, encoding="utf-8")
            home = self.root / "dyro-home"
            with patch.dict(os.environ, {"DYRO_HOME": str(home)}, clear=False):
                add_workspace(fallback, name="fallback", make_default=True)
                profile = self.root / "dyro.toml"
                profile.unlink()
                profile.mkdir()
                with self.assertRaisesRegex(ValidationError, "安全的普通文件"):
                    resolve_workspace(start=self.root, interactive=False)

    def test_multiple_active_objectives_require_selector_in_non_interactive_mode(self) -> None:
        create_objective(
            self.config,
            '''schema_version = 1
id = "observe-a"
title = "Observe A"
line = "alpha"
targets = ["TASK-A"]

[continuation]
requested_mode = "observe"
operations = ["execute"]
''',
        )
        create_objective(
            self.config,
            '''schema_version = 1
id = "observe-b"
title = "Observe B"
line = "alpha"
targets = ["TASK-A"]

[continuation]
requested_mode = "observe"
operations = ["execute"]
''',
        )
        with self.assertRaisesRegex(DyroError, "非交互模式必须显式指定"):
            resolve_objective(self.config, interactive=False)
        self.assertEqual(resolve_objective(self.config, objective_id="observe-a", interactive=False).objective.id, "observe-a")

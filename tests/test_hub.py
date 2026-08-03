from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dyro.cli import _route_experiment_surface, build_parser, main
from dyro.config import load
from dyro.hub import (
    add_workspace,
    get_workspace,
    load_registry,
    mark_workspace_used,
    registry_home,
    remove_workspace,
    set_default_workspace,
)
from dyro.tasks import task_template
from dyro.workspace import create_line

from .support import WorkspaceCase, shell


class RegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="dyro-hub-")
        self.base = Path(self.tmp.name)
        self.state = self.base / "state"
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.workspace.joinpath("dyro.toml").write_text(
            """schema_version = 1

[workspace]
name = "demo"

[repositories.api]
path = "repositories/api"
mount = "api"
""",
            encoding="utf-8",
        )
        self.environment = patch.dict(os.environ, {"DYRO_HOME": str(self.state)})
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.tmp.cleanup()

    def test_add_default_recent_and_remove_are_persisted(self) -> None:
        record = add_workspace(self.workspace, name="demo", make_default=True)
        self.assertEqual(record.name, "demo")
        self.assertEqual(get_workspace("demo").root, self.workspace.resolve())

        mark_workspace_used(
            "demo", target_kind="line", target_id="alpha", agent="codex"
        )
        recent = load_registry().workspaces[0]
        self.assertEqual(
            (recent.last_kind, recent.last_target, recent.last_agent),
            ("line", "alpha", "codex"),
        )

        set_default_workspace("demo")
        self.assertEqual(load_registry().default, "demo")
        remove_workspace("demo")
        self.assertEqual(load_registry().workspaces, ())

    def test_duplicate_workspace_path_is_rejected(self) -> None:
        add_workspace(self.workspace, name="demo")
        with self.assertRaisesRegex(Exception, "已经登记"):
            add_workspace(self.workspace, name="other")

    def test_malformed_registry_fails_closed(self) -> None:
        self.state.mkdir(parents=True)
        registry_home().joinpath("workspaces.json").write_text(
            '{"schema_version": 9}', encoding="utf-8"
        )
        with self.assertRaisesRegex(Exception, "工作区记录"):
            load_registry()

    def test_malformed_registry_rejects_non_string_alias(self) -> None:
        self.state.mkdir(parents=True)
        registry_home().joinpath("workspaces.json").write_text(
            """{
  "schema_version": 1,
  "default": "",
  "workspaces": [
    {
      "name": 123,
      "root": "/tmp/demo",
      "last_kind": "",
      "last_target": "",
      "last_agent": ""
    }
  ]
}
""",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(Exception, "别名"):
            load_registry()


class HubCliTests(WorkspaceCase):
    def setUp(self) -> None:
        super().setUp()
        self.hub_home = self.root / "global-state"
        self.environment = patch.dict(os.environ, {"DYRO_HOME": str(self.hub_home)})
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        super().tearDown()

    def _create_line(self) -> None:
        create_line(load(self.root), line_id="alpha", branch="feat/alpha", base="main")

    def _create_task_worktree(self, task_id: str = "TASK-OPEN") -> None:
        self._create_line()
        config = load(self.root)
        task_dir = config.task_specs_dir / task_id
        task_dir.mkdir(parents=True)
        task_dir.joinpath("task.toml").write_text(
            task_template(
                task_id, "Open existing work", "alpha", "api", "services/api"
            ).replace('agent = "codex"', 'agent = "noop"'),
            encoding="utf-8",
        )
        task_dir.joinpath("handoff.md").write_text("# handoff\n", encoding="utf-8")
        destination = self.root / "worktrees" / "alpha" / task_id / "services/api"
        destination.parent.mkdir(parents=True)
        shell(
            "git",
            "worktree",
            "add",
            "-b",
            f"task/{task_id}",
            str(destination),
            "feat/alpha",
            cwd=self.anchor,
        )

    def test_workspace_commands_do_not_require_manual_config_editing(self) -> None:
        output = StringIO()
        with redirect_stdout(output):
            main(["workspace", "add", str(self.root), "--name", "demo", "--default"])
            main(["workspace", "list"])
        rendered = output.getvalue()
        self.assertIn("demo", rendered)
        self.assertIn(str(self.root), rendered)

        main(["workspace", "remove", "demo", "--yes"])
        self.assertEqual(load_registry().workspaces, ())

    def test_no_argument_home_opens_registered_line_from_any_directory(self) -> None:
        self._create_line()
        add_workspace(self.root, name="demo", make_default=True)
        output = StringIO()
        with (
            patch("dyro.home.interactive_terminal", return_value=True),
            patch("builtins.input", return_value=""),
            redirect_stdout(output),
        ):
            main(["--dry-run"])

        rendered = output.getvalue()
        self.assertIn("test-workspace", rendered)
        self.assertIn("alpha", rendered)
        self.assertIn("/usr/bin/true", rendered)
        self.assertEqual(load_registry().workspaces[0].last_target, "")

    def test_unhealthy_line_does_not_block_opening_a_healthy_line(self) -> None:
        self._create_line()
        create_line(load(self.root), line_id="beta", branch="feat/beta", base="main")
        shell(
            "git",
            "branch",
            "-m",
            "wrong-beta",
            cwd=self.root / "versions/beta/services/api",
        )
        add_workspace(self.root, name="demo", make_default=True)

        output = StringIO()
        with (
            patch("dyro.home.interactive_terminal", return_value=True),
            patch("builtins.input", return_value=""),
            redirect_stdout(output),
        ):
            main(["--dry-run"])

        rendered = output.getvalue()
        self.assertIn("检测到 1 个结构问题", rendered)
        self.assertIn(str(self.root / "versions/alpha"), rendered)
        self.assertIn("/usr/bin/true", rendered)

    def test_task_open_uses_existing_worktree_without_changing_status(self) -> None:
        self._create_task_worktree()
        task_dir = load(self.root).task_specs_dir / "TASK-OPEN"
        self.assertFalse(task_dir.joinpath("status").exists())
        output = StringIO()
        with redirect_stdout(output):
            main(
                [
                    "--root",
                    str(self.root),
                    "--dry-run",
                    "task",
                    "open",
                    "TASK-OPEN",
                    "--agent",
                    "noop",
                ]
            )
        self.assertIn(str(self.root / "worktrees/alpha/TASK-OPEN"), output.getvalue())
        self.assertFalse(task_dir.joinpath("status").exists())

    def test_task_open_rejects_a_worktree_on_the_wrong_branch(self) -> None:
        self._create_task_worktree("TASK-WRONG-BRANCH")
        destination = self.root / "worktrees/alpha/TASK-WRONG-BRANCH/services/api"
        shell("git", "branch", "-m", "wrong-branch", cwd=destination)
        stderr = StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            main(
                [
                    "--root",
                    str(self.root),
                    "task",
                    "open",
                    "TASK-WRONG-BRANCH",
                    "--agent",
                    "noop",
                ]
            )
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("期望 task/TASK-WRONG-BRANCH", stderr.getvalue())

    def test_task_open_missing_worktree_gives_one_recovery_command(self) -> None:
        self._create_line()
        config = load(self.root)
        task_dir = config.task_specs_dir / "TASK-MISSING"
        task_dir.mkdir(parents=True)
        task_dir.joinpath("task.toml").write_text(
            task_template(
                "TASK-MISSING", "Missing worktree", "alpha", "api", "services/api"
            ).replace('agent = "codex"', 'agent = "noop"'),
            encoding="utf-8",
        )
        task_dir.joinpath("handoff.md").write_text("# handoff\n", encoding="utf-8")
        stderr = StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            main(
                [
                    "--root",
                    str(self.root),
                    "task",
                    "open",
                    "TASK-MISSING",
                    "--agent",
                    "noop",
                ]
            )
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("dyro task run TASK-MISSING", stderr.getvalue())

    def test_status_all_reports_registered_workspaces(self) -> None:
        self._create_line()
        add_workspace(self.root, name="demo", make_default=True)
        output = StringIO()
        with redirect_stdout(output):
            main(["status", "--all"])
        rendered = output.getvalue()
        self.assertIn("demo", rendered)
        self.assertIn("line:alpha", rendered)

    def test_status_all_keeps_reporting_when_one_registered_path_is_stale(self) -> None:
        self._create_line()
        add_workspace(self.root, name="demo", make_default=True)
        stale = self.root.parent / f"{self.root.name}-stale"
        stale.mkdir()
        stale.joinpath("dyro.toml").write_text(
            self.root.joinpath("dyro.toml").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        add_workspace(stale, name="stale")
        stale.joinpath("dyro.toml").unlink()

        output = StringIO()
        with redirect_stdout(output):
            main(["status", "--all"])
        rendered = output.getvalue()
        self.assertIn("工作区：demo", rendered)
        self.assertIn("工作区：stale", rendered)
        self.assertIn("不可用", rendered)

    def test_explicit_workspace_alias_works_from_any_directory(self) -> None:
        self._create_line()
        add_workspace(self.root, name="demo", make_default=True)
        output = StringIO()
        with redirect_stdout(output):
            main(
                ["--workspace", "demo", "--dry-run", "open", "alpha", "--agent", "noop"]
            )
        self.assertIn(str(self.root / "versions/alpha"), output.getvalue())

    def test_runner_workspace_option_does_not_collide_with_global_alias(self) -> None:
        arguments = [
            "--root",
            str(self.root),
            "task",
            "evidence",
            "build",
            "TASK-EVIDENCE",
            "--workspace",
            "/runner/workspace",
            "--receipt",
            "/runner/receipt.md",
            "--output",
            "/runner/evidence.zip",
        ]
        self.assertIsNone(_route_experiment_surface(arguments))
        parsed = build_parser().parse_args(arguments)
        self.assertIsNone(parsed.workspace_alias)
        self.assertEqual(parsed.workspace, "/runner/workspace")

    def test_home_registers_an_explicit_profile_without_editing_it(self) -> None:
        self._create_line()
        before = self.root.joinpath("dyro.toml").read_bytes()
        with patch("dyro.home.interactive_terminal", return_value=False):
            main(["--root", str(self.root)])
        self.assertEqual(get_workspace("test-workspace").root, self.root.resolve())
        self.assertEqual(self.root.joinpath("dyro.toml").read_bytes(), before)

    def test_agent_discovery_separates_configured_and_unintegrated_commands(
        self,
    ) -> None:
        output = StringIO()
        discovered = {"codex", "claude", "cursor-agent"}
        with (
            patch(
                "dyro.home.shutil.which",
                side_effect=lambda name: (
                    f"/fake/{name}" if name in discovered else None
                ),
            ),
            redirect_stdout(output),
        ):
            main(["--root", str(self.root), "agent", "discover"])
        rendered = output.getvalue()
        self.assertIn("codex", rendered)
        self.assertIn("claude", rendered)
        self.assertIn("尚未配置", rendered)
        self.assertIn("尚未集成", rendered)

    def test_agent_discovery_reports_configured_missing_command(self) -> None:
        with self.root.joinpath("dyro.toml").open("a", encoding="utf-8") as handle:
            handle.write(
                """
[adapters.codex]
launch = ["codex"]
read = ["codex"]
write = ["codex"]
"""
            )
        output = StringIO()
        with (
            patch("dyro.home.shutil.which", return_value=None),
            redirect_stdout(output),
        ):
            main(["--root", str(self.root), "agent", "discover"])
        rendered = output.getvalue()
        self.assertIn("已配置但不可用:codex", rendered)
        self.assertIn("命令不可用", rendered)

    def test_first_use_without_registry_is_actionable_and_non_mutating(self) -> None:
        output = StringIO()
        with (
            patch("dyro.home.interactive_terminal", return_value=False),
            redirect_stdout(output),
        ):
            main(["--dry-run"])
        rendered = output.getvalue()
        self.assertIn("欢迎使用 Dyro", rendered)
        self.assertIn("dyro setup", rendered)
        self.assertIn("dyro workspace add", rendered)
        self.assertFalse(self.hub_home.exists())

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from dyro.cli import _route_experiment_surface, build_parser, main
from dyro.config import load
from dyro.home import (
    HomeTool,
    _choose_action,
    _choose_tool,
    _macos_app_name,
    _openclaw_needs_setup,
    _parse_repository_selection,
    home_tools,
    sort_home_tools,
)
from dyro.hub import (
    add_workspace,
    get_workspace,
    load_registry,
    mark_workspace_used,
    registry_home,
    remove_workspace,
    set_default_workspace,
)
from dyro.tooling import (
    ToolPreferences,
    ToolState,
    load_tool_preferences,
    save_tool_preferences,
)
from dyro.tasks import task_template
from dyro.workspace import create_line, get_line

from .support import WorkspaceCase, shell


class RepositorySelectionParsingTests(unittest.TestCase):
    def test_accepts_indices_ids_and_mixed_tokens(self) -> None:
        repositories = ("miniapp", "pc-web", "common-msv", "ai-agent", "video-engine")
        selected, error = _parse_repository_selection("1,3", repositories)
        self.assertIsNone(error)
        self.assertEqual(selected, ("miniapp", "common-msv"))

        selected, error = _parse_repository_selection("pc-web，video-engine", repositories)
        self.assertIsNone(error)
        self.assertEqual(selected, ("pc-web", "video-engine"))

        selected, error = _parse_repository_selection("2, ai-agent, 2", repositories)
        self.assertIsNone(error)
        self.assertEqual(selected, ("pc-web", "ai-agent"))

    def test_rejects_out_of_range_and_unknown_tokens(self) -> None:
        repositories = ("miniapp", "pc-web")
        selected, error = _parse_repository_selection("3", repositories)
        self.assertIsNone(selected)
        self.assertIn("序号超出范围", error or "")

        selected, error = _parse_repository_selection("missing", repositories)
        self.assertIsNone(selected)
        self.assertIn("未配置的仓库", error or "")

    def test_numeric_repository_id_wins_over_index(self) -> None:
        repositories = ("api", "1", "svc")
        selected, error = _parse_repository_selection("1", repositories)
        self.assertIsNone(error)
        self.assertEqual(selected, ("1",))

        selected, error = _parse_repository_selection("3", repositories)
        self.assertIsNone(error)
        self.assertEqual(selected, ("svc",))


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
            patch("dyro.home.Path.cwd", return_value=self.root.parent),
            patch("dyro.home.interactive_terminal", return_value=True),
            patch("builtins.input", return_value=""),
            redirect_stdout(output),
        ):
            main(["--dry-run"])

        rendered = output.getvalue()
        self.assertIn("test-workspace", rendered)
        self.assertIn("alpha", rendered)
        self.assertIn("常用编码工具", rendered)
        self.assertIn("noop", rendered)
        self.assertIn("/usr/bin/true", rendered)
        self.assertEqual(load_registry().workspaces[0].last_target, "")

    def test_home_console_choice_honors_dry_run(self) -> None:
        self._create_line()
        add_workspace(self.root, name="demo", make_default=True)
        output = StringIO()
        with (
            patch("dyro.home.Path.cwd", return_value=self.root.parent),
            patch("dyro.home.interactive_terminal", return_value=True),
            patch("builtins.input", return_value="3"),
            patch("dyro.console.launcher.create_console_http_server") as server_factory,
            redirect_stdout(output),
        ):
            main(["--dry-run"])

        self.assertIn("DRY RUN: 将启动只读本地 Console", output.getvalue())
        server_factory.assert_not_called()

    def test_home_lists_safe_new_work_entrypoints(self) -> None:
        output = StringIO()
        with (
            patch("dyro.home.interactive_terminal", return_value=True),
            patch("builtins.input", return_value="3"),
            redirect_stdout(output),
        ):
            action = _choose_action([], can_switch=False)

        self.assertEqual(action, ("new-line", ""))
        self.assertIn("开启新的功能开发线", output.getvalue())
        self.assertIn("处理新的线上问题 / Hotfix", output.getvalue())

    def test_home_uses_semantic_color_when_explicitly_enabled(self) -> None:
        output = StringIO()
        with (
            patch.dict(os.environ, {"DYRO_COLOR": "always"}, clear=True),
            patch("dyro.home.interactive_terminal", return_value=True),
            patch("builtins.input", return_value="q"),
            redirect_stdout(output),
        ):
            self.assertIsNone(_choose_action([], can_switch=False))

        rendered = output.getvalue()
        self.assertIn("\033[1;35m━━ 今天做什么 ━━\033[0m", rendered)
        self.assertIn("\033[2;37m查看与管理\033[0m", rendered)

    def test_home_action_retries_invalid_input_and_recommends_new_feature_when_empty(
        self,
    ) -> None:
        answers = iter(["not-a-number", "99", ""])
        prompts: list[str] = []
        output = StringIO()
        with (
            patch("dyro.home.interactive_terminal", return_value=True),
            patch(
                "builtins.input",
                side_effect=lambda prompt: prompts.append(prompt) or next(answers),
            ),
            redirect_stdout(output),
        ):
            action = _choose_action([], can_switch=False)

        self.assertEqual(action, ("new-line", ""))
        rendered = output.getvalue()
        self.assertIn("请输入菜单编号", rendered)
        self.assertIn("该编号不在当前菜单中", rendered)
        self.assertTrue(any("回车=3" in prompt for prompt in prompts))

    def test_home_exits_cleanly_when_terminal_input_is_interrupted(self) -> None:
        output = StringIO()
        with (
            patch("dyro.home.interactive_terminal", return_value=True),
            patch("builtins.input", side_effect=KeyboardInterrupt),
            redirect_stdout(output),
        ):
            main(["--root", str(self.root)])

        self.assertIn("已中断当前引导；没有执行后续步骤", output.getvalue())

    def test_first_use_registers_an_existing_workspace_then_enters_home(self) -> None:
        answers = iter(["3", str(self.root), "q"])
        output = StringIO()
        with (
            patch("dyro.home.Path.cwd", return_value=self.root.parent),
            patch("dyro.home.interactive_terminal", return_value=True),
            patch("builtins.input", side_effect=lambda _: next(answers)),
            redirect_stdout(output),
        ):
            main([])

        rendered = output.getvalue()
        self.assertIn("已登记工作区：test-workspace。接下来选择要做什么", rendered)
        self.assertIn("━━ 今天做什么 ━━", rendered)
        self.assertEqual(get_workspace("test-workspace").root, self.root.resolve())

    def test_first_use_retries_an_unreadable_workspace_path(self) -> None:
        answers = iter(["3", str(self.root.parent / "missing"), "q"])
        output = StringIO()
        with (
            patch("dyro.home.Path.cwd", return_value=self.root.parent),
            patch("dyro.home.interactive_terminal", return_value=True),
            patch("builtins.input", side_effect=lambda _: next(answers)),
            redirect_stdout(output),
        ):
            main([])

        self.assertIn("无法登记该路径", output.getvalue())
        self.assertFalse(self.hub_home.exists())

    def test_home_new_feature_entrypoint_creates_confirmed_isolated_worktree(
        self,
    ) -> None:
        add_workspace(self.root, name="demo", make_default=True)
        answers = iter(["3", "FEATURE-20260804", "", "yes"])
        output = StringIO()
        with (
            patch("dyro.home.interactive_terminal", return_value=True),
            patch("builtins.input", side_effect=lambda _: next(answers)),
            patch("dyro.home._choose_tool", return_value=None),
            redirect_stdout(output),
        ):
            main(["--root", str(self.root)])

        rendered = output.getvalue()
        self.assertIn("新功能开发线会创建隔离 Git worktree", rendered)
        self.assertIn("━━ 开启功能开发线 ━━", rendered)
        self.assertIn("步骤：功能 ID → 参与仓库 → 开发基线 → 创建确认", rendered)
        self.assertIn("[2/3] 参与仓库", rendered)
        self.assertIn("main（工作区默认基线）（推荐）", rendered)
        self.assertIn("━━ 创建前确认 ━━", rendered)
        self.assertIn("已创建功能开发线：FEATURE-20260804", rendered)
        line = get_line(load(self.root), "FEATURE-20260804", "line")
        self.assertEqual(line.base, "main")
        self.assertTrue(self.root.joinpath("versions", "FEATURE-20260804").is_dir())

    def test_home_new_feature_entrypoint_cancels_without_creating_worktree(
        self,
    ) -> None:
        add_workspace(self.root, name="demo", make_default=True)
        before_registry = load_registry()
        answers = iter(["3", "FEATURE-20260804", "q"])
        output = StringIO()
        with (
            patch("dyro.home.interactive_terminal", return_value=True),
            patch("builtins.input", side_effect=lambda _: next(answers)),
            redirect_stdout(output),
        ):
            main(["--root", str(self.root)])

        self.assertIn("已取消；没有修改任何 Git 工作区", output.getvalue())
        self.assertFalse(
            self.root.joinpath(".dyro", "lines", "FEATURE-20260804.toml").exists()
        )
        self.assertEqual(load_registry(), before_registry)

    def test_home_new_feature_can_return_to_edit_a_previous_step(self) -> None:
        add_workspace(self.root, name="demo", make_default=True)
        answers = iter(
            [
                "3",
                "FEATURE-BACK",
                "b",
                "FEATURE-BACK",
                "",
                "b",
                "",
                "yes",
            ]
        )
        output = StringIO()
        with (
            patch("dyro.home.interactive_terminal", return_value=True),
            patch("builtins.input", side_effect=lambda _: next(answers)),
            patch("dyro.home._choose_tool", return_value=None),
            redirect_stdout(output),
        ):
            main(["--root", str(self.root)])

        rendered = output.getvalue()
        self.assertIn("b) 返回上一步", rendered)
        self.assertEqual(rendered.count("━━ 创建前确认 ━━"), 2)
        self.assertTrue(self.root.joinpath("versions", "FEATURE-BACK").is_dir())

    def test_home_new_feature_rechecks_manual_base_before_confirmation(self) -> None:
        add_workspace(self.root, name="demo", make_default=True)
        answers = iter(["3", "FEATURE-BAD-BASE", "2", "missing-release", "q"])
        output = StringIO()
        with (
            patch("dyro.home.interactive_terminal", return_value=True),
            patch("builtins.input", side_effect=lambda _: next(answers)),
            redirect_stdout(output),
        ):
            main(["--root", str(self.root)])

        rendered = output.getvalue()
        self.assertIn("api 无法解析 missing-release", rendered)
        self.assertIn("请为这个仓库单独选择已核实的基线", rendered)
        self.assertNotIn("将创建隔离功能开发线", rendered)
        self.assertFalse(
            self.root.joinpath(".dyro", "lines", "FEATURE-BAD-BASE.toml").exists()
        )

    def test_home_new_feature_preflights_dirty_repository_before_confirmation(
        self,
    ) -> None:
        add_workspace(self.root, name="demo", make_default=True)
        self.anchor.joinpath("dirty.txt").write_text(
            "not committed\n", encoding="utf-8"
        )
        answers = iter(["3", "FEATURE-DIRTY", "", "", "q"])
        output = StringIO()
        with (
            patch("dyro.home.interactive_terminal", return_value=True),
            patch("builtins.input", side_effect=lambda _: next(answers)),
            redirect_stdout(output),
        ):
            main(["--root", str(self.root)])

        rendered = output.getvalue()
        self.assertIn("创建功能开发线前检查未通过", rendered)
        self.assertIn("仓库不干净", rendered)
        self.assertNotIn("确认以上范围与基线", rendered)
        self.assertFalse(
            self.root.joinpath(".dyro", "lines", "FEATURE-DIRTY.toml").exists()
        )

    def test_home_new_feature_normalizes_release_per_repository(self) -> None:
        config_path = self.root / "dyro.toml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8")
            + """
[repositories.web]
path = "repositories/web"
mount = "web"
""",
            encoding="utf-8",
        )
        web = self.root / "repositories/web"
        web.mkdir(parents=True)
        shell("git", "init", "-b", "main", cwd=web)
        shell("git", "config", "user.name", "Test User", cwd=web)
        shell("git", "config", "user.email", "test@example.com", cwd=web)
        (web / "README.md").write_text("web\n", encoding="utf-8")
        shell("git", "add", "README.md", cwd=web)
        shell("git", "commit", "-m", "chore: initial", cwd=web)
        shell(
            "git", "update-ref", "refs/remotes/origin/release", "HEAD", cwd=self.anchor
        )

        add_workspace(self.root, name="demo", make_default=True)
        answers = iter(["3", "FEATURE-RELEASE", "", "2", "release", "", "", "yes"])
        output = StringIO()
        with (
            patch("dyro.home.interactive_terminal", return_value=True),
            patch("builtins.input", side_effect=lambda _: next(answers)),
            patch("dyro.home._choose_tool", return_value=None),
            redirect_stdout(output),
        ):
            main(["--root", str(self.root)])

        rendered = output.getvalue()
        self.assertIn("当前仓库基线：", rendered)
        self.assertIn("api  origin/release  [默认]", rendered)
        self.assertIn("web  main  [覆盖]", rendered)
        line = get_line(load(self.root), "FEATURE-RELEASE", "line")
        self.assertEqual(line.base, "origin/release")
        self.assertEqual(line.repository_bases, {"web": "main"})

    def test_home_new_feature_limits_base_adjustment_to_selected_repositories(
        self,
    ) -> None:
        config_path = self.root / "dyro.toml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8")
            + """
[repositories.web]
path = "repositories/web"
mount = "web"
""",
            encoding="utf-8",
        )
        web = self.root / "repositories/web"
        web.mkdir(parents=True)
        shell("git", "init", "-b", "main", cwd=web)
        shell("git", "config", "user.name", "Test User", cwd=web)
        shell("git", "config", "user.email", "test@example.com", cwd=web)
        web.joinpath("README.md").write_text("web\n", encoding="utf-8")
        shell("git", "add", "README.md", cwd=web)
        shell("git", "commit", "-m", "chore: initial", cwd=web)
        shell("git", "checkout", "-b", "release", cwd=web)

        add_workspace(self.root, name="demo", make_default=True)
        answers = iter(["3", "FEATURE-WEB", "2", "2", "2", "release", "yes"])
        output = StringIO()
        with (
            patch("dyro.home.interactive_terminal", return_value=True),
            patch("builtins.input", side_effect=lambda _: next(answers)),
            patch("dyro.home._choose_tool", return_value=None),
            redirect_stdout(output),
        ):
            main(["--root", str(self.root)])

        rendered = output.getvalue()
        self.assertIn("可选仓库：", rendered)
        self.assertIn("  1) api", rendered)
        self.assertIn("  2) web", rendered)
        self.assertNotIn("当前仓库基线：", rendered)
        line = get_line(load(self.root), "FEATURE-WEB", "line")
        self.assertEqual(line.repositories, ("web",))
        self.assertEqual(line.base, "release")

    def test_home_hotfix_limits_baseline_scope_to_selected_repositories(self) -> None:
        config_path = self.root / "dyro.toml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8")
            + """
[repositories.web]
path = "repositories/web"
mount = "web"
""",
            encoding="utf-8",
        )
        web = self.root / "repositories/web"
        web.mkdir(parents=True)
        shell("git", "init", "-b", "main", cwd=web)
        shell("git", "config", "user.name", "Test User", cwd=web)
        shell("git", "config", "user.email", "test@example.com", cwd=web)
        web.joinpath("README.md").write_text("web\n", encoding="utf-8")
        shell("git", "add", "README.md", cwd=web)
        shell("git", "commit", "-m", "chore: initial", cwd=web)
        shell("git", "checkout", "-b", "release", cwd=web)

        add_workspace(self.root, name="demo", make_default=True)
        # Custom repo pick: "2" is the index of web (api=1, web=2).
        answers = iter(["4", "INC-WEB", "2", "2", "", "yes"])
        output = StringIO()
        with (
            patch("dyro.home.interactive_terminal", return_value=True),
            patch("builtins.input", side_effect=lambda _: next(answers)),
            patch("dyro.home._choose_tool", return_value=None),
            redirect_stdout(output),
        ):
            main(["--root", str(self.root)])

        rendered = output.getvalue()
        self.assertIn("步骤：问题 ID → 参与仓库 → 生产基线 → 创建确认", rendered)
        self.assertIn("  1) api", rendered)
        self.assertIn("  2) web", rendered)
        self.assertIn("发布分支 release", rendered)
        line = get_line(load(self.root), "INC-WEB", "hotfix")
        self.assertEqual(line.repositories, ("web",))
        self.assertEqual(line.base, "release")

    def test_home_hotfix_entrypoint_creates_confirmed_isolated_worktree(self) -> None:
        add_workspace(self.root, name="demo", make_default=True)
        shell("git", "tag", "v2026.08.04", cwd=self.anchor)
        answers = iter(["4", "INC-20260804", "", "yes"])
        output = StringIO()
        with (
            patch("dyro.home.interactive_terminal", return_value=True),
            patch("builtins.input", side_effect=lambda _: next(answers)),
            patch("dyro.home._choose_tool", return_value=None),
            redirect_stdout(output),
        ):
            main(["--root", str(self.root)])

        rendered = output.getvalue()
        self.assertIn("Hotfix 需要已核实的生产 release 分支、tag 或部署 SHA", rendered)
        self.assertIn("共同 Git 基线", rendered)
        self.assertIn(
            "发布 tag v2026.08.04（所有已配置仓库均可解析）（推荐）", rendered
        )
        self.assertIn("━━ 创建前确认 ━━", rendered)
        self.assertIn("已创建 Hotfix：INC-20260804", rendered)
        line = get_line(load(self.root), "INC-20260804", "hotfix")
        self.assertEqual(line.base, "v2026.08.04")
        self.assertTrue(self.root.joinpath("hotfixes", "INC-20260804").is_dir())

    def test_home_hotfix_entrypoint_cancels_without_creating_worktree(self) -> None:
        add_workspace(self.root, name="demo", make_default=True)
        before_registry = load_registry()
        answers = iter(["4", "INC-20260804", "1", "main", "no"])
        output = StringIO()
        with (
            patch("dyro.home.interactive_terminal", return_value=True),
            patch("builtins.input", side_effect=lambda _: next(answers)),
            redirect_stdout(output),
        ):
            main(["--root", str(self.root)])

        self.assertIn("已取消；没有修改任何 Git 工作区", output.getvalue())
        self.assertFalse(
            self.root.joinpath(".dyro", "hotfixes", "INC-20260804.toml").exists()
        )
        self.assertEqual(load_registry(), before_registry)

    def test_home_hotfix_can_return_to_edit_the_verified_baseline(self) -> None:
        add_workspace(self.root, name="demo", make_default=True)
        answers = iter(["4", "INC-BACK", "1", "main", "b", "1", "main", "yes"])
        output = StringIO()
        with (
            patch("dyro.home.interactive_terminal", return_value=True),
            patch("builtins.input", side_effect=lambda _: next(answers)),
            patch("dyro.home._choose_tool", return_value=None),
            redirect_stdout(output),
        ):
            main(["--root", str(self.root)])

        rendered = output.getvalue()
        self.assertIn("b) 返回上一步", rendered)
        self.assertEqual(rendered.count("━━ 创建前确认 ━━"), 2)
        self.assertTrue(self.root.joinpath("hotfixes", "INC-BACK").is_dir())

    def test_home_hotfix_recommends_per_repository_release_refs(self) -> None:
        config_path = self.root / "dyro.toml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8")
            + """
[repositories.web]
path = "repositories/web"
mount = "web"
""",
            encoding="utf-8",
        )
        web = self.root / "repositories/web"
        web.mkdir(parents=True)
        shell("git", "init", "-b", "main", cwd=web)
        shell("git", "config", "user.name", "Test User", cwd=web)
        shell("git", "config", "user.email", "test@example.com", cwd=web)
        (web / "README.md").write_text("web\n", encoding="utf-8")
        shell("git", "add", "README.md", cwd=web)
        shell("git", "commit", "-m", "chore: initial", cwd=web)
        shell("git", "checkout", "-b", "release", cwd=web)
        shell(
            "git", "update-ref", "refs/remotes/origin/release", "HEAD", cwd=self.anchor
        )

        add_workspace(self.root, name="demo", make_default=True)
        answers = iter(["4", "INC-RELEASE", "", "", "", "yes"])
        output = StringIO()
        with (
            patch("dyro.home.interactive_terminal", return_value=True),
            patch("builtins.input", side_effect=lambda _: next(answers)),
            patch("dyro.home._choose_tool", return_value=None),
            redirect_stdout(output),
        ):
            main(["--root", str(self.root)])

        rendered = output.getvalue()
        self.assertIn("发布分支 release（所有已配置仓库均可解析）（推荐）", rendered)
        self.assertIn("api  origin/release  [默认]", rendered)
        self.assertIn("web  release  [覆盖]", rendered)
        line = get_line(load(self.root), "INC-RELEASE", "hotfix")
        self.assertEqual(line.base, "origin/release")
        self.assertEqual(line.repository_bases, {"web": "release"})

    def test_home_hotfix_can_override_one_repository_base(self) -> None:
        config_path = self.root / "dyro.toml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8")
            + """
[repositories.web]
path = "repositories/web"
mount = "web"
""",
            encoding="utf-8",
        )
        web = self.root / "repositories/web"
        web.mkdir(parents=True)
        shell("git", "init", "-b", "main", cwd=web)
        shell("git", "config", "user.name", "Test User", cwd=web)
        shell("git", "config", "user.email", "test@example.com", cwd=web)
        (web / "README.md").write_text("web\n", encoding="utf-8")
        shell("git", "add", "README.md", cwd=web)
        shell("git", "commit", "-m", "chore: initial", cwd=web)
        shell("git", "checkout", "-b", "release", cwd=web)
        shell(
            "git", "update-ref", "refs/remotes/origin/release", "HEAD", cwd=self.anchor
        )

        add_workspace(self.root, name="demo", make_default=True)
        answers = iter(["4", "INC-OVERRIDE", "", "", "2", "2", "2", "", "yes"])
        output = StringIO()
        with (
            patch("dyro.home.interactive_terminal", return_value=True),
            patch("builtins.input", side_effect=lambda _: next(answers)),
            patch("dyro.home._choose_tool", return_value=None),
            redirect_stdout(output),
        ):
            main(["--root", str(self.root)])

        rendered = output.getvalue()
        self.assertIn("按仓库单独调整基线", rendered)
        self.assertIn("api  origin/release  [默认]", rendered)
        self.assertIn("web  main  [覆盖]", rendered)
        line = get_line(load(self.root), "INC-OVERRIDE", "hotfix")
        self.assertEqual(line.base, "origin/release")
        self.assertEqual(line.repository_bases, {"web": "main"})

    def test_home_hotfix_rechecks_manual_base_before_confirmation(self) -> None:
        add_workspace(self.root, name="demo", make_default=True)
        answers = iter(["4", "INC-BAD-BASE", "1", "missing-release", "", "q"])
        output = StringIO()
        with (
            patch("dyro.home.interactive_terminal", return_value=True),
            patch("builtins.input", side_effect=lambda _: next(answers)),
            redirect_stdout(output),
        ):
            main(["--root", str(self.root)])

        rendered = output.getvalue()
        self.assertIn("api 无法解析 missing-release", rendered)
        self.assertIn("请为这个仓库单独选择已核实的基线", rendered)
        self.assertNotIn("将创建隔离 Hotfix worktree", rendered)
        self.assertFalse(
            self.root.joinpath(".dyro", "hotfixes", "INC-BAD-BASE.toml").exists()
        )

    def test_home_hotfix_preflights_dirty_repository_before_confirmation(self) -> None:
        add_workspace(self.root, name="demo", make_default=True)
        self.anchor.joinpath("dirty.txt").write_text(
            "not committed\n", encoding="utf-8"
        )
        answers = iter(["4", "INC-DIRTY", "1", "main", "", "q"])
        output = StringIO()
        with (
            patch("dyro.home.interactive_terminal", return_value=True),
            patch("builtins.input", side_effect=lambda _: next(answers)),
            redirect_stdout(output),
        ):
            main(["--root", str(self.root)])

        rendered = output.getvalue()
        self.assertIn("创建Hotfix前检查未通过", rendered)
        self.assertIn("仓库不干净", rendered)
        self.assertNotIn("确认该基线已在生产核实", rendered)
        self.assertFalse(
            self.root.joinpath(".dyro", "hotfixes", "INC-DIRTY.toml").exists()
        )

    def test_home_can_open_a_detected_tool_without_granting_adapter_capabilities(
        self,
    ) -> None:
        self._create_line()
        add_workspace(self.root, name="demo", make_default=True)
        before = self.root.joinpath("dyro.toml").read_bytes()
        answers = iter(["", "claude"])
        output = StringIO()
        with (
            patch("dyro.home.Path.cwd", return_value=self.root.parent),
            patch("dyro.home.interactive_terminal", return_value=True),
            patch("builtins.input", side_effect=lambda _: next(answers)),
            patch(
                "dyro.home.shutil.which",
                side_effect=lambda name: "/fake/claude" if name == "claude" else None,
            ),
            redirect_stdout(output),
        ):
            main(["--dry-run"])

        rendered = output.getvalue()
        self.assertIn("更多工具", rendered)
        self.assertIn("$ claude", rendered)
        self.assertNotIn("claude", load(self.root).adapters)
        self.assertEqual(self.root.joinpath("dyro.toml").read_bytes(), before)

    def test_home_exposes_detected_codex_without_configuring_an_adapter(
        self,
    ) -> None:
        with patch(
            "dyro.home.shutil.which",
            side_effect=lambda name: "/fake/codex" if name == "codex" else None,
        ):
            tools = home_tools(load(self.root), workspace=self.root)

        codex = next(tool for tool in tools if tool.id == "codex")
        self.assertEqual(codex.kind, "launcher")
        self.assertEqual(codex.argv, ("codex", "-C", str(self.root)))
        self.assertTrue(codex.available)
        self.assertNotIn("codex", load(self.root).adapters)

    def test_home_detects_antigravity_qoder_and_zcode_as_launch_only_tools(
        self,
    ) -> None:
        discovered = {"agy", "qodercli", "zcode"}
        with patch(
            "dyro.home.shutil.which",
            side_effect=lambda name: f"/fake/{name}" if name in discovered else None,
        ):
            tools = home_tools(load(self.root), workspace=self.root)

        by_id = {tool.id: tool for tool in tools}
        self.assertEqual(by_id["antigravity"].argv, ("agy",))
        self.assertEqual(by_id["qoder"].argv, ("qodercli",))
        self.assertEqual(by_id["zcode"].argv, ("zcode", str(self.root)))
        for tool_id in ("antigravity", "qoder", "zcode"):
            self.assertEqual(by_id[tool_id].kind, "launcher")
            self.assertEqual(by_id[tool_id].state, ToolState.READY)
            self.assertNotIn(tool_id, load(self.root).adapters)

    def test_home_detects_codex_and_claude_desktops_as_launch_only_tools(
        self,
    ) -> None:
        def detected(name: str) -> str | None:
            return "/fake/" + name if name in {"codex", "open"} else None

        with (
            patch("dyro.home.sys.platform", "darwin"),
            patch("dyro.home.Path.is_dir", return_value=True),
            patch("dyro.home.shutil.which", side_effect=detected),
        ):
            tools = home_tools(load(self.root), workspace=self.root)

        by_id = {tool.id: tool for tool in tools}
        self.assertEqual(
            by_id["codex-desktop"].argv,
            ("codex", "app", str(self.root)),
        )
        claude_argv = by_id["claude-desktop"].argv
        self.assertEqual(claude_argv[0], "open")
        self.assertTrue(claude_argv[1].startswith("claude://code/new?folder="))
        for tool_id in ("codex-desktop", "claude-desktop"):
            self.assertEqual(by_id[tool_id].kind, "launcher")
            self.assertEqual(by_id[tool_id].state, ToolState.READY)
            self.assertNotIn(tool_id, load(self.root).adapters)

    def test_macos_desktop_detection_accepts_codex_and_claude_app_variants(
        self,
    ) -> None:
        with (
            patch("dyro.home.sys.platform", "darwin"),
            patch(
                "dyro.home.Path.is_dir",
                side_effect=(False, False, True, False, False, True),
            ),
        ):
            self.assertEqual(_macos_app_name("Codex", "ChatGPT"), "ChatGPT")
            self.assertEqual(
                _macos_app_name("Claude", "Claude Code URL Handler"),
                "Claude Code URL Handler",
            )

    def test_agent_discovery_lists_detected_codex_and_claude_desktops(self) -> None:
        def detected(name: str) -> str | None:
            return "/fake/" + name if name in {"codex", "open"} else None

        output = StringIO()
        with (
            patch("dyro.home.sys.platform", "darwin"),
            patch("dyro.home.Path.is_dir", return_value=True),
            patch("dyro.home.shutil.which", side_effect=detected),
            redirect_stdout(output),
        ):
            main(["--root", str(self.root), "agent", "discover"])

        rendered = output.getvalue()
        self.assertIn("codex-desktop", rendered)
        self.assertIn("claude-desktop", rendered)
        self.assertIn("尚未集成", rendered)

    def test_home_tool_picker_shows_common_choices_before_full_catalog(self) -> None:
        discovered = {"agy", "claude", "kimi", "qodercli", "zcode"}
        answers = iter(["m", "antigravity"])
        output = StringIO()
        with (
            patch(
                "dyro.home.shutil.which",
                side_effect=lambda name: (
                    f"/fake/{name}" if name in discovered else None
                ),
            ),
            patch("builtins.input", side_effect=lambda _: next(answers)),
            redirect_stdout(output),
        ):
            tool = _choose_tool(
                load(self.root), None, workspace=self.root, dry_run=True
            )

        self.assertIsNotNone(tool)
        self.assertEqual(tool.id if tool else "", "antigravity")
        rendered = output.getvalue()
        self.assertIn("更多工具", rendered)
        self.assertIn("Antigravity CLI", rendered)

    def test_home_tool_picker_offers_installable_default_when_nothing_is_ready(
        self,
    ) -> None:
        installable = HomeTool(
            "qoder", "Qoder CLI", "launcher", ("qodercli",), (), ToolState.INSTALLABLE
        )
        output = StringIO()
        with (
            patch("dyro.home.home_tools", return_value=[installable]),
            patch("builtins.input", return_value=""),
            redirect_stdout(output),
        ):
            tool = _choose_tool(
                load(self.root), None, workspace=self.root, dry_run=True
            )

        self.assertEqual(tool, installable)
        self.assertIn("Qoder CLI", output.getvalue())
        self.assertIn("未安装，可引导安装", output.getvalue())

    def test_home_tool_picker_retries_invalid_choice(self) -> None:
        ready = HomeTool("codex", "Codex", "launcher", ("codex",), (), ToolState.READY)
        answers = iter(["missing-tool", "1"])
        output = StringIO()
        with (
            patch("dyro.home.home_tools", return_value=[ready]),
            patch("builtins.input", side_effect=lambda _: next(answers)),
            redirect_stdout(output),
        ):
            tool = _choose_tool(
                load(self.root), None, workspace=self.root, dry_run=True
            )

        self.assertEqual(tool, ready)
        self.assertIn("未找到该编码工具", output.getvalue())

    def test_home_tool_picker_keeps_user_in_the_menu_for_unavailable_tool(self) -> None:
        unavailable = HomeTool(
            "grok", "Grok", "launcher", ("grok",), (), ToolState.UNAVAILABLE
        )
        ready = HomeTool("codex", "Codex", "launcher", ("codex",), (), ToolState.READY)
        answers = iter(["grok", "1"])
        output = StringIO()
        with (
            patch("dyro.home.home_tools", return_value=[unavailable, ready]),
            patch("builtins.input", side_effect=lambda _: next(answers)),
            redirect_stdout(output),
        ):
            tool = _choose_tool(
                load(self.root), None, workspace=self.root, dry_run=True
            )

        self.assertEqual(tool, ready)
        self.assertIn("Grok 当前不可用", output.getvalue())

    def test_home_detects_cursor_desktop_separately_from_cursor_cli(self) -> None:
        def detected(name: str) -> str | None:
            return "/fake/cursor" if name == "cursor" else None

        with patch("dyro.home.shutil.which", side_effect=detected):
            tools = home_tools(load(self.root), workspace=self.root)

        desktop = next(tool for tool in tools if tool.id == "cursor-desktop")
        cli = next(tool for tool in tools if tool.id == "cursor-agent")
        self.assertEqual(desktop.argv, ("cursor", str(self.root)))
        self.assertEqual(desktop.state, ToolState.READY)
        self.assertEqual(cli.state, ToolState.INSTALLABLE)

    def test_openclaw_uses_selected_workspace_without_becoming_an_adapter(self) -> None:
        with (
            patch(
                "dyro.home.shutil.which",
                side_effect=lambda name: (
                    "/fake/openclaw" if name == "openclaw" else None
                ),
            ),
            patch("dyro.home._openclaw_needs_setup", return_value=False),
        ):
            tools = home_tools(load(self.root), workspace=self.root)

        openclaw = next(tool for tool in tools if tool.id == "openclaw")
        self.assertEqual(openclaw.state, ToolState.READY)
        self.assertEqual(
            openclaw.environment,
            (("OPENCLAW_WORKSPACE_DIR", str(self.root)),),
        )
        self.assertNotIn("openclaw", load(self.root).adapters)

    def test_openclaw_setup_detection_honors_home_and_named_profile(self) -> None:
        openclaw_home = self.root / "openclaw-home"
        config = openclaw_home / ".openclaw-work" / "openclaw.json"
        config.parent.mkdir(parents=True)
        config.write_text("{}\n", encoding="utf-8")

        with patch.dict(
            os.environ,
            {
                "OPENCLAW_HOME": str(openclaw_home),
                "OPENCLAW_PROFILE": "work",
            },
            clear=True,
        ):
            self.assertFalse(_openclaw_needs_setup())

    def test_openclaw_explicit_config_path_takes_precedence(self) -> None:
        config = self.root / "explicit-openclaw.json"
        config.write_text("{}\n", encoding="utf-8")

        with patch.dict(
            os.environ,
            {
                "OPENCLAW_CONFIG_PATH": str(config),
                "OPENCLAW_STATE_DIR": str(self.root / "missing-state"),
                "OPENCLAW_HOME": str(self.root / "missing-home"),
                "OPENCLAW_PROFILE": "work",
            },
            clear=True,
        ):
            self.assertFalse(_openclaw_needs_setup())

    def test_tool_sorting_prefers_history_then_recommendation_and_availability(
        self,
    ) -> None:
        def tool(tool_id: str, state: ToolState) -> HomeTool:
            return HomeTool(tool_id, tool_id, "launcher", (), (), state)

        tools = [
            tool("shell", ToolState.READY),
            tool("openclaw", ToolState.NEEDS_SETUP),
            tool("kimi", ToolState.INSTALLABLE),
            tool("grok", ToolState.UNAVAILABLE),
            tool("claude", ToolState.READY),
            tool("cursor-desktop", ToolState.READY),
            tool("codex", ToolState.READY),
        ]
        ordered = sort_home_tools(
            tools,
            last_tool="codex",
            recommended_tool="cursor-desktop",
            preferences=ToolPreferences(
                default_tool="claude", pinned_tools=("claude", "codex")
            ),
        )

        self.assertEqual(
            [item.id for item in ordered],
            [
                "codex",
                "cursor-desktop",
                "claude",
                "openclaw",
                "kimi",
                "grok",
                "shell",
            ],
        )

    def test_tool_preferences_are_local_and_do_not_modify_profile(self) -> None:
        before = self.root.joinpath("dyro.toml").read_bytes()
        save_tool_preferences(
            ToolPreferences(
                default_tool="cursor-desktop",
                pinned_tools=("cursor-desktop", "codex"),
            )
        )

        self.assertEqual(self.root.joinpath("dyro.toml").read_bytes(), before)

    def test_tool_preference_commands_drive_list_markers(self) -> None:
        main(["tool", "default", "cursor-desktop"])
        main(["tool", "pin", "cursor-desktop", "codex", "openclaw"])

        output = StringIO()
        with redirect_stdout(output):
            main(["--root", str(self.root), "tool", "list"])

        rendered = output.getvalue()
        self.assertIn("cursor-desktop", rendered)
        self.assertIn("个人默认", rendered)
        preferences = load_tool_preferences()
        self.assertEqual(preferences.default_tool, "cursor-desktop")
        self.assertEqual(
            preferences.pinned_tools,
            ("cursor-desktop", "codex", "openclaw"),
        )

    def test_tool_install_dry_run_is_non_mutating_and_noninteractive(self) -> None:
        output = StringIO()
        with redirect_stdout(output):
            main(["--dry-run", "tool", "install", "openclaw"])

        rendered = output.getvalue()
        self.assertIn("npm install -g openclaw@latest", rendered)
        self.assertIn("DRY RUN", rendered)

    def test_home_guides_install_then_rechecks_without_granting_adapter(self) -> None:
        self._create_line()
        add_workspace(self.root, name="demo", make_default=True)
        installed = False
        calls: list[tuple[str, ...]] = []

        def detected(name: str) -> str | None:
            if name == "npm":
                return "/fake/npm"
            if name == "openclaw" and installed:
                return "/fake/openclaw"
            return None

        def run(argv: tuple[str, ...], **_: object) -> subprocess.CompletedProcess[str]:
            nonlocal installed
            calls.append(argv)
            installed = True
            return subprocess.CompletedProcess(argv, 0)

        answers = iter(["", "openclaw", "y", "n"])
        output = StringIO()
        before = self.root.joinpath("dyro.toml").read_bytes()
        with (
            patch("dyro.home.Path.cwd", return_value=self.root.parent),
            patch("dyro.home.interactive_terminal", return_value=True),
            patch("dyro.home.shutil.which", side_effect=detected),
            patch("dyro.home._openclaw_needs_setup", return_value=True),
            patch("dyro.tooling._run_install", side_effect=run),
            patch("builtins.input", side_effect=lambda _: next(answers)),
            redirect_stdout(output),
        ):
            main([])

        self.assertEqual(
            calls,
            [
                ("/fake/npm", "install", "-g", "openclaw@latest"),
                ("/fake/openclaw", "--version"),
            ],
        )
        self.assertIn("正在重新检测", output.getvalue())
        self.assertIn("官方初始化", output.getvalue())
        self.assertIn("不是系统沙箱", output.getvalue())
        self.assertNotIn("openclaw", load(self.root).adapters)
        self.assertEqual(self.root.joinpath("dyro.toml").read_bytes(), before)

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
            patch("dyro.home.Path.cwd", return_value=self.root.parent),
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
        self.assertIn("首页可仅打开工作区", rendered)
        self.assertIn("不获得执行、门禁或复核权限", rendered)

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
            patch("dyro.home.Path.cwd", return_value=self.root.parent),
            patch("dyro.home.interactive_terminal", return_value=False),
            redirect_stdout(output),
        ):
            main(["--dry-run"])
        rendered = output.getvalue()
        self.assertIn("欢迎使用 Dyro", rendered)
        self.assertIn("dyro join", rendered)
        self.assertIn("dyro setup", rendered)
        self.assertIn("dyro workspace add", rendered)
        self.assertFalse(self.hub_home.exists())

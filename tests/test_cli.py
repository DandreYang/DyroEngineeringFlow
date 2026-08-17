from pathlib import Path
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import Mock, call, patch

from dyro.cli import (
    _print_doctor_finding,
    _print_setup_completion,
    _route_experiment_surface,
    _setup_default_tool,
    _setup_provider_preset,
    main,
)
from dyro.changesets import get_changeset
from dyro.config import load
from dyro.continuation.store import pause_objective
from dyro.evidence_store import publish_evidence_generation
from dyro.home import HomeTool
from dyro.hub import load_registry
from dyro.tasks import load_task, status, task_template
from dyro.tooling import ToolState, load_tool_preferences
from dyro.updates import load_update_state
from dyro.workspace import create_line, get_line, line_repository_path

from .support import WorkspaceCase


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry_tmp = tempfile.TemporaryDirectory(prefix="dyro-registry-")
        self.registry_environment = patch.dict(
            os.environ, {"DYRO_HOME": self.registry_tmp.name}, clear=False
        )
        self.registry_environment.start()

    def tearDown(self) -> None:
        self.registry_environment.stop()
        self.registry_tmp.cleanup()

    def test_runtime_is_not_an_experiment_surface(self) -> None:
        routed = _route_experiment_surface(
            [
                "--root",
                "/workspace",
                "--dry-run",
                "runtime",
                "handoff",
                "--task",
                "TASK-1",
            ]
        )
        self.assertIsNone(routed)

    def test_runtime_command_is_rejected(self) -> None:
        stderr = StringIO()
        with (
            redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            main(["runtime", "status"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("invalid choice", stderr.getvalue())

    def test_interrupted_setup_exits_without_a_traceback(self) -> None:
        stderr = StringIO()
        with (
            patch("dyro.cli._interactive_setup", side_effect=KeyboardInterrupt),
            redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            main(["setup", "--interactive"])

        self.assertEqual(raised.exception.code, 130)
        self.assertIn("已停止当前操作", stderr.getvalue())
        self.assertIn("dyro doctor", stderr.getvalue())

    def test_management_views_have_clear_plain_text_headings(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dyro-cli-") as tmp:
            root = Path(tmp) / "workspace"
            repository = root / "repositories/api"
            repository.mkdir(parents=True)
            from .support import shell

            shell("git", "init", "-b", "main", cwd=repository)
            shell("git", "config", "user.name", "Test User", cwd=repository)
            shell("git", "config", "user.email", "test@example.com", cwd=repository)
            repository.joinpath("README.md").write_text("anchor\n", encoding="utf-8")
            shell("git", "add", "README.md", cwd=repository)
            shell("git", "commit", "-m", "chore: initial", cwd=repository)
            main(["setup", str(root), "--name", "demo", "--no-line"])
            output = StringIO()
            with redirect_stdout(output):
                main(["workspace", "list"])
                main(["--root", str(root), "doctor"])

            rendered = output.getvalue()
            self.assertIn("━━ 全局工作区 ━━", rendered)
            self.assertIn("●", rendered)
            self.assertIn("━━ Dyro 健康检查 ━━", rendered)
            self.assertIn("检查通过。", rendered)

    def test_init_creates_workspace_contract(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dyro-cli-") as tmp:
            root = Path(tmp) / "workspace"
            main(["init", str(root), "--name", "demo"])
            self.assertTrue((root / "dyro.toml").exists())
            self.assertTrue((root / ".dyro/tasks").is_dir())
            self.assertEqual(load(root).name, "demo")

    def test_workspace_list_json_is_structured_and_identifies_the_default(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dyro-cli-") as tmp:
            root = Path(tmp) / "workspace"
            main(["init", str(root), "--name", "demo"])
            main(["workspace", "add", str(root), "--default"])

            output = StringIO()
            with redirect_stdout(output):
                main(["workspace", "list", "--format", "json"])

            payload = json.loads(output.getvalue())
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["kind"], "workspace_list")
            self.assertEqual(payload["default"], "demo")
            self.assertEqual(payload["workspaces"][0]["name"], "demo")
            self.assertTrue(payload["workspaces"][0]["available"])
            self.assertNotIn("root", payload["workspaces"][0])

            output = StringIO()
            with redirect_stdout(output):
                main(
                    [
                        "workspace",
                        "list",
                        "--format",
                        "json",
                        "--include-paths",
                    ]
                )
            with_paths = json.loads(output.getvalue())
            self.assertEqual(with_paths["workspaces"][0]["root"], str(root.resolve()))

    def test_setup_presentation_uses_semantic_color_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dyro-cli-") as tmp:
            root = Path(tmp) / "workspace"
            main(["init", str(root), "--name", "demo"])
            output = StringIO()
            with patch.dict(os.environ, {"DYRO_COLOR": "always"}):
                os.environ.pop("NO_COLOR", None)
                with redirect_stdout(output):
                    _print_setup_completion(load(root), None)
                    _print_doctor_finding("PASS repository api: ready")

            rendered = output.getvalue()
            self.assertIn("\033[1;32m━━ 设置完成 ━━\033[0m", rendered)
            self.assertIn("\033[1;36mdemo\033[0m", rendered)
            self.assertIn("\033[1;32mPASS repository api: ready\033[0m", rendered)

    def test_init_discover_creates_config_from_local_git_repositories(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dyro-cli-") as tmp:
            root = Path(tmp) / "workspace"
            repository = root / "repositories/api"
            repository.mkdir(parents=True)
            from .support import shell

            shell("git", "init", "-b", "main", cwd=repository)
            main(["init", str(root), "--name", "demo", "--discover"])

            config = load(root)
            self.assertEqual(config.repositories["api"].path, "repositories/api")
            self.assertEqual(config.repositories["api"].mount, "api")

    def test_init_discover_skips_delivery_line_worktrees(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dyro-cli-") as tmp:
            root = Path(tmp) / "workspace"
            repository = root / "repositories/api"
            nested_worktree = root / "versions/release-1/services/api"
            repository.mkdir(parents=True)
            nested_worktree.mkdir(parents=True)
            from .support import shell

            shell("git", "init", "-b", "main", cwd=repository)
            shell("git", "init", "-b", "main", cwd=nested_worktree)
            main(["init", str(root), "--name", "demo", "--discover"])

            self.assertEqual(sorted(load(root).repositories), ["api"])

    def test_setup_discovers_repositories_and_creates_a_first_line(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dyro-cli-") as tmp:
            root = Path(tmp) / "workspace"
            repository = root / "repositories/api"
            repository.mkdir(parents=True)
            from .support import shell

            shell("git", "init", "-b", "main", cwd=repository)
            shell("git", "config", "user.name", "Test User", cwd=repository)
            shell("git", "config", "user.email", "test@example.com", cwd=repository)
            repository.joinpath("README.md").write_text("anchor\n", encoding="utf-8")
            shell("git", "add", "README.md", cwd=repository)
            shell("git", "commit", "-m", "chore: initial", cwd=repository)

            main(["setup", str(root), "--name", "demo", "--line", "dev", "--yes"])

            config = load(root)
            self.assertEqual(config.name, "demo")
            self.assertTrue((root / ".dyro/tasks").is_dir())
            self.assertEqual(get_line(config, "dev").branch, "feat/dev")
            self.assertTrue((root / "versions/dev/api").is_dir())

            registry = load_registry()
            self.assertEqual(registry.default, "demo")
            self.assertEqual(
                [(record.name, record.root) for record in registry.workspaces],
                [("demo", root.resolve())],
            )

    def test_setup_keeps_an_existing_default_workspace(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dyro-cli-") as tmp:
            root = Path(tmp) / "workspace"
            repository = root / "repositories/api"
            repository.mkdir(parents=True)
            from .support import shell

            shell("git", "init", "-b", "main", cwd=repository)
            shell("git", "config", "user.name", "Test User", cwd=repository)
            shell("git", "config", "user.email", "test@example.com", cwd=repository)
            repository.joinpath("README.md").write_text("anchor\n", encoding="utf-8")
            shell("git", "add", "README.md", cwd=repository)
            shell("git", "commit", "-m", "chore: initial", cwd=repository)

            existing = Path(tmp) / "existing"
            main(["init", str(existing), "--name", "existing"])
            main(["workspace", "add", str(existing), "--default"])

            main(["setup", str(root), "--name", "demo", "--no-line"])

            registry = load_registry()
            self.assertEqual(registry.default, "existing")
            self.assertEqual(
                {record.name for record in registry.workspaces}, {"demo", "existing"}
            )

    def test_setup_can_explicitly_set_the_default_workspace(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dyro-cli-") as tmp:
            root = Path(tmp) / "workspace"
            repository = root / "repositories/api"
            repository.mkdir(parents=True)
            from .support import shell

            shell("git", "init", "-b", "main", cwd=repository)

            existing = Path(tmp) / "existing"
            main(["init", str(existing), "--name", "existing"])
            main(["workspace", "add", str(existing), "--default"])

            main(
                [
                    "setup",
                    str(root),
                    "--name",
                    "demo",
                    "--no-line",
                    "--default",
                ]
            )

            self.assertEqual(load_registry().default, "demo")

    def test_setup_can_skip_global_workspace_registration(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dyro-cli-") as tmp:
            root = Path(tmp) / "workspace"
            repository = root / "repositories/api"
            repository.mkdir(parents=True)
            from .support import shell

            shell("git", "init", "-b", "main", cwd=repository)

            main(
                [
                    "setup",
                    str(root),
                    "--name",
                    "demo",
                    "--no-line",
                    "--no-register",
                ]
            )

            self.assertEqual(load_registry().workspaces, ())

    def test_setup_rejects_an_alias_conflict_before_writing_a_profile(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dyro-cli-") as tmp:
            existing = Path(tmp) / "existing"
            main(["init", str(existing), "--name", "demo"])
            main(["workspace", "add", str(existing)])

            root = Path(tmp) / "workspace"
            repository = root / "repositories/api"
            repository.mkdir(parents=True)
            from .support import shell

            shell("git", "init", "-b", "main", cwd=repository)

            stderr = StringIO()
            with (
                redirect_stderr(stderr),
                self.assertRaises(SystemExit) as raised,
            ):
                main(["setup", str(root), "--name", "demo", "--no-line"])

            self.assertEqual(raised.exception.code, 2)
            self.assertIn("工作区别名 demo 已指向", stderr.getvalue())
            self.assertFalse((root / "dyro.toml").exists())

    def test_setup_accepts_dry_run_after_the_command_without_writing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dyro-cli-") as tmp:
            root = Path(tmp) / "workspace"
            repository = root / "repositories/api"
            repository.mkdir(parents=True)
            from .support import shell

            shell("git", "init", "-b", "main", cwd=repository)

            main(["setup", str(root), "--name", "demo", "--no-line", "--dry-run"])

            self.assertFalse((root / "dyro.toml").exists())

    def test_setup_rejects_conflicting_interaction_modes(self) -> None:
        stderr = StringIO()
        with (
            redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            main(["setup", "--interactive", "--non-interactive"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("not allowed with argument", stderr.getvalue())

    def test_setup_offers_every_detected_launchable_provider(self) -> None:
        discovered = {
            "agy",
            "claude",
            "cursor-agent",
            "grok",
            "opencode",
            "hermes",
            "kimi",
            "dsh",
            "pi",
            "qodercli",
        }
        output = StringIO()
        with (
            patch(
                "dyro.profile.shutil.which",
                side_effect=lambda command: (
                    f"/fake/{command}" if command in discovered else None
                ),
            ),
            patch("dyro.cli._ask_yes_no", return_value=True),
            redirect_stdout(output),
        ):
            presets = _setup_provider_preset()

        self.assertEqual(
            presets,
            (
                "antigravity",
                "claude",
                "cursor-agent",
                "grok",
                "opencode",
                "hermes",
                "kimi",
                "qoder",
            ),
        )
        rendered = output.getvalue()
        self.assertIn("antigravity", rendered)
        self.assertIn("grok", rendered)

    def test_interactive_setup_can_be_cancelled_without_writing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dyro-cli-") as tmp:
            root = Path(tmp) / "workspace"
            repository = root / "repositories/api"
            repository.mkdir(parents=True)
            from .support import shell

            shell("git", "init", "-b", "main", cwd=repository)
            answers = iter(["", "", "", "", "", "n"])
            with (
                patch("dyro.cli._setup_provider_preset", return_value=None),
                patch("dyro.cli.launcher_tools", return_value=[]),
                patch("dyro.cli._setup_skill_preference", return_value=False),
                patch("builtins.input", side_effect=lambda _: next(answers)),
            ):
                main(["setup", str(root), "--interactive"])

            self.assertFalse((root / "dyro.toml").exists())
            self.assertFalse((root / ".dyro").exists())
            self.assertFalse((Path(self.registry_tmp.name) / "updates.json").exists())

    def test_interactive_setup_applies_confirmed_plan(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dyro-cli-") as tmp:
            root = Path(tmp) / "workspace"
            repository = root / "repositories/api"
            repository.mkdir(parents=True)
            from .support import shell

            shell("git", "init", "-b", "main", cwd=repository)
            shell("git", "config", "user.name", "Test User", cwd=repository)
            shell("git", "config", "user.email", "test@example.com", cwd=repository)
            repository.joinpath("README.md").write_text("anchor\n", encoding="utf-8")
            shell("git", "add", "README.md", cwd=repository)
            shell("git", "commit", "-m", "chore: initial", cwd=repository)
            answers = iter(["", "", "", "", "", "y"])
            with (
                patch("dyro.cli._setup_provider_preset", return_value=None),
                patch("dyro.cli.launcher_tools", return_value=[]),
                patch("dyro.cli._setup_skill_preference", return_value=False),
                patch("builtins.input", side_effect=lambda _: next(answers)),
            ):
                main(["setup", str(root), "--interactive"])

            config = load(root)
            self.assertNotIn("codex", config.adapters)
            self.assertEqual(get_line(config, "dev").branch, "feat/dev")

    def test_interactive_setup_never_writes_into_a_git_repository_root(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dyro-cli-") as tmp:
            repository = Path(tmp) / "api"
            repository.mkdir()
            from .support import shell

            shell("git", "init", "-b", "main", cwd=repository)
            answers = iter([])
            output = StringIO()
            with (
                patch("builtins.input", side_effect=lambda _: next(answers)),
                redirect_stdout(output),
            ):
                main(["setup", str(repository), "--interactive"])

            self.assertFalse((repository / "dyro.toml").exists())
            self.assertIn("没有 origin", output.getvalue())

    def test_interactive_setup_clones_a_sibling_workspace_for_a_git_root(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dyro-cli-") as tmp:
            parent = Path(tmp)
            remote = parent / "api-origin.git"
            repository = parent / "api"
            sibling = parent / "api-dyro"
            from .support import shell

            shell("git", "init", "--bare", str(remote), cwd=parent)
            repository.mkdir()
            shell("git", "init", "-b", "main", cwd=repository)
            shell("git", "config", "user.name", "Test User", cwd=repository)
            shell("git", "config", "user.email", "test@example.com", cwd=repository)
            repository.joinpath("README.md").write_text("anchor\n", encoding="utf-8")
            shell("git", "add", "README.md", cwd=repository)
            shell("git", "commit", "-m", "chore: initial", cwd=repository)
            shell("git", "remote", "add", "origin", str(remote), cwd=repository)
            shell("git", "push", "-u", "origin", "main", cwd=repository)
            answers = iter(["", "", "", "", "", "", "y"])
            with (
                patch("dyro.cli._setup_provider_preset", return_value=None),
                patch("dyro.cli.launcher_tools", return_value=[]),
                patch("dyro.cli._setup_skill_preference", return_value=False),
                patch("builtins.input", side_effect=lambda _: next(answers)),
            ):
                main(["setup", str(repository), "--interactive"])

            self.assertFalse((repository / "dyro.toml").exists())
            self.assertTrue((sibling / "dyro.toml").is_file())
            config = load(sibling)
            self.assertEqual(len(config.repositories), 1)
            self.assertTrue((sibling / "versions/dev").is_dir())

    def test_interactive_setup_uses_the_source_branch_as_the_suggested_base(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="dyro-cli-") as tmp:
            parent = Path(tmp)
            remote = parent / "api-origin.git"
            repository = parent / "api"
            sibling = parent / "api-dyro"
            from .support import shell

            shell("git", "init", "--bare", str(remote), cwd=parent)
            repository.mkdir()
            shell("git", "init", "-b", "trunk", cwd=repository)
            shell("git", "config", "user.name", "Test User", cwd=repository)
            shell("git", "config", "user.email", "test@example.com", cwd=repository)
            repository.joinpath("README.md").write_text("anchor\n", encoding="utf-8")
            shell("git", "add", "README.md", cwd=repository)
            shell("git", "commit", "-m", "chore: initial", cwd=repository)
            shell("git", "remote", "add", "origin", str(remote), cwd=repository)
            shell("git", "push", "-u", "origin", "trunk", cwd=repository)
            shell("git", "symbolic-ref", "HEAD", "refs/heads/trunk", cwd=remote)
            answers = iter(["", "", "", "", "", "", "y"])
            with (
                patch("dyro.cli._setup_provider_preset", return_value=None),
                patch("dyro.cli.launcher_tools", return_value=[]),
                patch("dyro.cli._setup_skill_preference", return_value=False),
                patch("builtins.input", side_effect=lambda _: next(answers)),
            ):
                main(["setup", str(repository), "--interactive"])

            config = load(sibling)
            self.assertEqual(config.policy.default_base, "trunk")
            self.assertEqual(get_line(config, "dev").base, "trunk")

    def test_interactive_setup_saves_confirmed_personal_preferences(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dyro-cli-") as tmp:
            root = Path(tmp) / "workspace"
            repository = root / "repositories/api"
            repository.mkdir(parents=True)
            from .support import shell

            shell("git", "init", "-b", "main", cwd=repository)
            shell("git", "config", "user.name", "Test User", cwd=repository)
            shell("git", "config", "user.email", "test@example.com", cwd=repository)
            repository.joinpath("README.md").write_text("anchor\n", encoding="utf-8")
            shell("git", "add", "README.md", cwd=repository)
            shell("git", "commit", "-m", "chore: initial", cwd=repository)
            tools = [
                HomeTool("codex", "Codex", "launcher", (), (), ToolState.READY)
            ]
            answers = iter(["", "", "", "", "", "codex", "y"])
            with (
                patch("dyro.cli._setup_provider_preset", return_value="codex"),
                patch("dyro.cli.launcher_tools", return_value=tools),
                patch("dyro.cli._setup_skill_preference", return_value=False),
                patch("builtins.input", side_effect=lambda _: next(answers)),
            ):
                main(["setup", str(root), "--interactive"])

            updates = load_update_state()
            self.assertTrue(updates.check_enabled)
            self.assertFalse(updates.auto_patch)
            self.assertEqual(load_tool_preferences().default_tool, "codex")
            self.assertIn("codex", load(root).adapters)

    def test_setup_default_tool_keeps_the_first_screen_short_and_accepts_tool_id(self) -> None:
        tools = [
            HomeTool("antigravity", "Antigravity CLI", "launcher", (), (), ToolState.READY),
            HomeTool("claude", "Claude Code", "launcher", (), (), ToolState.READY),
            HomeTool("codex", "Codex", "launcher", (), (), ToolState.READY),
            HomeTool("qoder", "Qoder CLI", "launcher", (), (), ToolState.READY),
        ]
        output = StringIO()
        with (
            patch("dyro.cli.launcher_tools", return_value=tools),
            patch("builtins.input", return_value="qoder"),
            redirect_stdout(output),
        ):
            selected = _setup_default_tool(Path("/workspace"), None)

        self.assertEqual(selected, "qoder")
        rendered = output.getvalue()
        self.assertIn("查看全部已检测工具", rendered)
        self.assertNotIn("Qoder CLI", rendered)

    def test_interactive_setup_can_switch_console_default_project(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dyro-cli-") as tmp:
            existing = Path(tmp) / "existing"
            main(["init", str(existing), "--name", "existing"])
            main(["workspace", "add", str(existing), "--default"])

            root = Path(tmp) / "workspace"
            repository = root / "repositories/api"
            repository.mkdir(parents=True)
            from .support import shell

            shell("git", "init", "-b", "main", cwd=repository)
            shell("git", "config", "user.name", "Test User", cwd=repository)
            shell("git", "config", "user.email", "test@example.com", cwd=repository)
            repository.joinpath("README.md").write_text("anchor\n", encoding="utf-8")
            shell("git", "add", "README.md", cwd=repository)
            shell("git", "commit", "-m", "chore: initial", cwd=repository)
            answers = iter(["", "", "", "", "", "2", "y"])
            with (
                patch("dyro.cli._setup_provider_preset", return_value=None),
                patch("dyro.cli.launcher_tools", return_value=[]),
                patch("dyro.cli._setup_skill_preference", return_value=False),
                patch("builtins.input", side_effect=lambda _: next(answers)),
            ):
                main(["setup", str(root), "--interactive"])

            self.assertEqual(load_registry().default, "workspace")

    def test_existing_profile_interactive_setup_dry_run_never_writes_preferences(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="dyro-cli-") as tmp:
            root = Path(tmp) / "workspace"
            repository = root / "repositories/api"
            repository.mkdir(parents=True)
            from .support import shell

            shell("git", "init", "-b", "main", cwd=repository)
            main(["setup", str(root), "--name", "workspace", "--no-line"])
            output = StringIO()
            answers = iter(["", ""])
            with (
                patch("dyro.cli.launcher_tools", return_value=[]),
                patch("dyro.cli._setup_skill_preference", return_value=False),
                patch("builtins.input", side_effect=lambda _: next(answers)),
                redirect_stdout(output),
            ):
                main(["setup", str(root), "--interactive", "--dry-run"])

            self.assertFalse((Path(self.registry_tmp.name) / "updates.json").exists())
            self.assertIn("DRY RUN: 上述个人偏好和全局入口不会写入。", output.getvalue())

    def test_setup_skill_preference_defaults_to_install_when_hosts_exist(
        self,
    ) -> None:
        from dyro.cli import _setup_skill_preference
        from dyro.integrations import AvatarStatus, IntegrationState, IntegrationStatus

        status = IntegrationStatus(
            "skill",
            IntegrationState.ABSENT,
            Path("/tmp/mirror"),
            Path("/tmp/manifest"),
            "未安装",
            avatars=(
                AvatarStatus("codex", Path("/tmp/codex"), "missing", "missing"),
            ),
        )
        with (
            patch("dyro.cli.integration_status", return_value=status),
            patch("builtins.input", return_value=""),
        ):
            self.assertTrue(_setup_skill_preference())

    def test_setup_skill_preference_defaults_to_defer_without_hosts(self) -> None:
        from dyro.cli import _setup_skill_preference
        from dyro.integrations import IntegrationState, IntegrationStatus

        status = IntegrationStatus(
            "skill",
            IntegrationState.ABSENT,
            Path("/tmp/mirror"),
            Path("/tmp/manifest"),
            "未安装",
        )
        with (
            patch("dyro.cli.integration_status", return_value=status),
            patch("builtins.input", return_value=""),
        ):
            self.assertFalse(_setup_skill_preference())

    def test_apply_setup_personal_preferences_installs_skill_when_requested(
        self,
    ) -> None:
        from dyro.cli import SetupPersonalPreferences, _apply_setup_personal_preferences

        preferences = SetupPersonalPreferences(
            check_enabled=True,
            auto_patch=False,
            default_tool=None,
            make_default_workspace=False,
            install_skill=True,
        )
        plan = Mock()
        plan.changes = ("创建镜像",)
        with (
            patch("dyro.cli.set_update_enabled") as set_enabled,
            patch("dyro.cli.set_auto_patch") as set_auto,
            patch("dyro.cli.sync_managed_skill", return_value=plan) as sync,
        ):
            outcome = _apply_setup_personal_preferences(preferences)
        set_enabled.assert_called_once_with(True)
        set_auto.assert_called_once_with(False)
        self.assertEqual(
            sync.call_args_list,
            [
                call("skill", yes=True, allow_first_install=True),
                call("dispatch", yes=True, allow_first_install=True),
            ],
        )
        self.assertEqual(outcome, "success")

    def test_setup_skill_preference_offers_missing_dispatch_companion(self) -> None:
        from dyro.cli import _setup_skill_preference
        from dyro.integrations import AvatarStatus, IntegrationState, IntegrationStatus

        statuses = {
            "skill": IntegrationStatus(
                "skill",
                IntegrationState.CURRENT,
                Path("/tmp/control"),
                Path("/tmp/control.json"),
                "current",
                avatars=(
                    AvatarStatus("codex", Path("/tmp/codex"), "current", "current"),
                ),
            ),
            "dispatch": IntegrationStatus(
                "dispatch",
                IntegrationState.ABSENT,
                Path("/tmp/dispatch"),
                Path("/tmp/dispatch.json"),
                "absent",
                avatars=(
                    AvatarStatus("codex", Path("/tmp/codex"), "missing", "missing"),
                ),
            ),
        }
        with (
            patch("dyro.cli.integration_status", side_effect=statuses.__getitem__),
            patch("builtins.input", return_value=""),
        ):
            self.assertTrue(_setup_skill_preference())

    def test_print_setup_completion_reflects_skill_failure(self) -> None:
        from dyro.cli import SetupPersonalPreferences, _print_setup_completion

        preferences = SetupPersonalPreferences(
            check_enabled=True,
            auto_patch=False,
            default_tool=None,
            make_default_workspace=False,
            install_skill=True,
        )
        with tempfile.TemporaryDirectory(prefix="dyro-cli-") as tmp:
            root = Path(tmp) / "workspace"
            main(["init", str(root), "--name", "demo"])
            output = StringIO()
            with redirect_stdout(output):
                _print_setup_completion(
                    load(root),
                    None,
                    preferences,
                    skill_outcome="failed",
                )
            self.assertIn("安装未成功", output.getvalue())
            self.assertNotIn("已请求安装", output.getvalue())


class StartTests(WorkspaceCase):
    def test_start_dry_run_uses_selected_line_and_adapter(self) -> None:
        config = load(self.root)
        create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        main(
            [
                "--root",
                str(self.root),
                "--dry-run",
                "start",
                "--line",
                "alpha",
                "--agent",
                "noop",
            ]
        )

    def test_next_without_a_profile_explains_how_to_begin(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dyro-cli-") as tmp:
            output = StringIO()
            with redirect_stdout(output):
                main(["--root", str(Path(tmp) / "empty"), "next"])

            self.assertIn("dyro join", output.getvalue())
            self.assertIn("dyro setup", output.getvalue())


class LineCommandsTests(WorkspaceCase):
    def test_line_create_records_per_repository_base_and_storage_without_toml_edits(
        self,
    ) -> None:
        main(
            [
                "--root",
                str(self.root),
                "line",
                "create",
                "alpha",
                "--repo-base",
                "api=main",
                "--storage",
                "api=linked-worktree",
                "--yes",
            ]
        )

        line = get_line(load(self.root), "alpha")
        self.assertEqual(line.base_for("api"), "main")
        self.assertEqual(line.storage_for("api"), "linked-worktree")

    def test_changeset_create_records_a_delivery_line_without_manual_toml_edit(
        self,
    ) -> None:
        config = load(self.root)
        create_line(config, line_id="alpha", branch="feat/alpha", base="main")

        main(
            [
                "--root",
                str(self.root),
                "changeset",
                "create",
                "alpha-ready",
                "--line",
                "alpha",
            ]
        )

        self.assertEqual(get_changeset(load(self.root), "alpha-ready").line, "alpha")


class RepositoryCommandsTests(WorkspaceCase):
    def test_repo_add_registers_an_existing_git_repository_without_manual_toml_edit(
        self,
    ) -> None:
        web = self.root / "repositories/web"
        web.mkdir(parents=True)
        from .support import shell

        shell("git", "init", "-b", "main", cwd=web)
        shell(
            "git",
            "remote",
            "add",
            "origin",
            "https://example.test/acme/web.git",
            cwd=web,
        )
        main(["--root", str(self.root), "repo", "add", "repositories/web"])

        config = load(self.root)
        self.assertEqual(config.repositories["web"].path, "repositories/web")
        self.assertEqual(config.repositories["web"].mount, "web")
        self.assertEqual(
            config.repositories["web"].remote, "https://example.test/acme/web.git"
        )


class ProfileCommandsTests(WorkspaceCase):
    def test_config_and_agent_management_do_not_require_manual_toml_edits(self) -> None:
        main(
            [
                "--root",
                str(self.root),
                "config",
                "set",
                "policy.execution_mode",
                "external",
            ]
        )
        self.assertEqual(load(self.root).policy.execution_mode, "external")

        main(["--root", str(self.root), "agent", "add", "isolated", "--preset", "noop"])
        self.assertIn("isolated", load(self.root).adapters)
        main(["--root", str(self.root), "agent", "test", "isolated"])

        main(["--root", str(self.root), "agent", "add", "grok", "--preset", "grok"])
        self.assertEqual(
            load(self.root).adapters["grok"].launch,
            ("grok", "--cwd", "{workspace}"),
        )

    def test_start_can_launch_an_installed_tool_without_a_profile_adapter(self) -> None:
        create_line(load(self.root), line_id="alpha", branch="feat/alpha", base="main")
        launched: list[object] = []

        def fake_available(executable: str, cwd=None) -> bool:
            return Path(executable).name == "grok"

        with (
            patch("dyro.home.shutil.which", side_effect=lambda name: "/fake/grok" if name == "grok" else None),
            patch("dyro.home.executable_available", side_effect=fake_available),
            patch(
                "dyro.cli.launch_start_tool",
                side_effect=lambda *args, **kwargs: launched.append(
                    kwargs["tool"].id
                ),
            ),
        ):
            main(
                [
                    "--root",
                    str(self.root),
                    "--dry-run",
                    "start",
                    "--line",
                    "alpha",
                    "--agent",
                    "grok",
                ]
            )

        self.assertEqual(launched, ["grok"])


class ExternalClaimCommandsTests(WorkspaceCase):
    def test_claim_output_preflight_preserves_task_and_existing_file(
        self,
    ) -> None:
        config_path = self.root / "dyro.toml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "require_clean_merge = true",
                'require_clean_merge = true\nexecution_mode = "external"',
            ),
            encoding="utf-8",
        )
        config = load(self.root)
        create_line(
            config,
            line_id="alpha",
            branch="feat/alpha",
            base="main",
        )
        task_id = "TASK-CLAIM-OUTPUT"
        task_path = config.task_specs_dir / task_id
        task_path.mkdir(parents=True)
        task_path.joinpath("task.toml").write_text(
            task_template(
                task_id,
                "claim output preflight",
                "alpha",
                "api",
                "services/api",
            ).replace('agent = "codex"', 'agent = "noop"'),
            encoding="utf-8",
        )
        task_path.joinpath("handoff.md").write_text(
            "# handoff\n",
            encoding="utf-8",
        )
        output = self.root / "runner-inbox" / "claim.json"
        output.parent.mkdir(parents=True)
        output.write_text("user-owned\n", encoding="utf-8")

        with self.assertRaises(SystemExit) as raised:
            main(
                [
                    "--root",
                    str(self.root),
                    "task",
                    "claim",
                    task_id,
                    "--by",
                    "runner-1",
                    "--output",
                    str(output),
                ]
            )

        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(output.read_text(encoding="utf-8"), "user-owned\n")
        task = load_task(load(self.root), task_id)
        self.assertEqual(status(load(self.root), task), "backlog")
        self.assertFalse(task_path.joinpath("claim.json").exists())


class ObjectiveCliTests(WorkspaceCase):
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

    def _read_json(self, *argv: str) -> dict[str, object]:
        output = StringIO()
        with redirect_stdout(output):
            main(["--root", str(self.root), *argv, "--format", "json"])
        return json.loads(output.getvalue())

    def _start_release_objective(self) -> None:
        main(
            [
                "--root",
                str(self.root),
                "objective",
                "start",
                "--id",
                "release",
                "--title",
                "Release",
                "--line",
                "alpha",
                "--targets",
                "TASK-A",
                "--yes",
            ]
        )

    def test_control_plane_workspace_views_have_stable_json_shapes(self) -> None:
        doctor_payload = self._read_json("doctor")
        self.assertEqual(doctor_payload["kind"], "doctor")
        self.assertTrue(doctor_payload["passed"])
        self.assertTrue(doctor_payload["findings"])
        rendered_doctor = json.dumps(doctor_payload)
        self.assertNotIn(str(self.root.resolve()), rendered_doctor)
        self.assertNotIn(str(self.anchor.resolve()), rendered_doctor)
        self.assertTrue(
            any(
                finding["message"] == "repository api: ready"
                for finding in doctor_payload["findings"]
            )
        )

        doctor_with_paths = self._read_json("doctor", "--include-paths")
        self.assertIn(str(self.anchor.resolve()), json.dumps(doctor_with_paths))

        status_payload = self._read_json("status")
        self.assertEqual(status_payload["kind"], "workspace_status")
        self.assertEqual(status_payload["workspace"], "test-workspace")
        self.assertEqual(status_payload["rows"][0]["scope"], "anchor")

        next_payload = self._read_json("next")
        self.assertEqual(next_payload["kind"], "next_step")
        self.assertEqual(next_payload["state"], "ready")
        self.assertEqual(next_payload["commands"], [])
        self.assertFalse(next_payload["mutation_available"])
        self.assertNotIn("briefing", next_payload)

        lines_payload = self._read_json("line", "list")
        self.assertEqual(lines_payload["kind"], "line_list")
        self.assertEqual(lines_payload["lines"][0]["id"], "alpha")
        self.assertEqual(
            lines_payload["lines"][0]["repositories"][0]["storage"],
            "linked-worktree",
        )

    def test_control_plane_next_preserves_an_explicit_workspace_selector(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dyro-registry-") as registry_home:
            with patch.dict(os.environ, {"DYRO_HOME": registry_home}, clear=False):
                main(
                    [
                        "workspace",
                        "add",
                        str(self.root),
                        "--name",
                        "selected",
                        "--default",
                    ]
                )
                output = StringIO()
                with redirect_stdout(output):
                    main(
                        [
                            "--workspace",
                            "selected",
                            "next",
                            "--format",
                            "json",
                        ]
                    )

        payload = json.loads(output.getvalue())
        self.assertEqual(payload["state"], "ready")
        self.assertEqual(payload["commands"], [])
        self.assertFalse(payload["mutation_available"])

    def test_control_plane_json_runtime_errors_use_one_stable_envelope(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dyro-registry-") as registry_home:
            stdout = StringIO()
            stderr = StringIO()
            with (
                patch.dict(os.environ, {"DYRO_HOME": registry_home}, clear=False),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
                self.assertRaises(SystemExit) as raised,
            ):
                main(
                    [
                        "--workspace",
                        "missing",
                        "status",
                        "--format",
                        "json",
                    ]
                )

        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(
            json.loads(stderr.getvalue()),
            {
                "schema_version": 1,
                "kind": "error",
                "code": "WORKSPACE_NOT_REGISTERED",
                "command": "status",
                "retryable": False,
            },
        )

        stdout = StringIO()
        stderr = StringIO()
        with tempfile.TemporaryDirectory(prefix="dyro-missing-") as missing_home:
            missing_root = Path(missing_home) / "missing-workspace"
            with (
                patch.dict(os.environ, {"DYRO_HOME": missing_home}, clear=False),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
                self.assertRaises(SystemExit) as raised,
            ):
                main(
                    [
                        "--root",
                        str(missing_root),
                        "next",
                        "--format",
                        "json",
                    ]
                )
        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(
            json.loads(stderr.getvalue())["code"], "LOCAL_PROFILE_INVALID"
        )

    def test_control_plane_next_rejects_a_broken_local_profile(self) -> None:
        (self.root / "dyro.toml").write_text("not valid toml = [", encoding="utf-8")
        stdout = StringIO()
        stderr = StringIO()
        with (
            patch("dyro.cli.Path.cwd", return_value=self.root),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            main(["next", "--format", "json"])

        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(
            json.loads(stderr.getvalue())["code"], "LOCAL_PROFILE_INVALID"
        )

    def test_control_plane_doctor_failure_is_one_json_result(self) -> None:
        self.anchor.rename(self.root / "api-missing")
        stdout = StringIO()
        stderr = StringIO()
        with (
            redirect_stdout(stdout),
            redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            main(
                [
                    "--root",
                    str(self.root),
                    "doctor",
                    "--format",
                    "json",
                ]
            )

        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(stderr.getvalue(), "")
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["kind"], "doctor")
        self.assertFalse(payload["passed"])

    def test_control_plane_json_interrupt_is_one_stable_envelope(self) -> None:
        stdout = StringIO()
        stderr = StringIO()
        with (
            patch("dyro.cli.doctor", side_effect=KeyboardInterrupt),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            main(
                [
                    "--root",
                    str(self.root),
                    "doctor",
                    "--format",
                    "json",
                ]
            )

        self.assertEqual(raised.exception.code, 130)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(json.loads(stderr.getvalue())["code"], "INTERRUPTED")

    def test_control_plane_next_only_offers_applicable_bootstrap(self) -> None:
        (self.config.lines_state_dir / "alpha.toml").unlink()
        self.anchor.rename(self.root / "api-missing")
        unavailable = self._read_json("next")
        self.assertFalse(unavailable["mutation_available"])
        self.assertEqual(unavailable["commands"], [])
        self.assertEqual(
            unavailable["diagnostic_commands"],
            [f"dyro --root {self.root.resolve()} doctor"],
        )

        config_path = self.root / "dyro.toml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                'mount = "services/api"',
                'mount = "services/api"\nremote = "https://example.invalid/api.git"',
            ),
            encoding="utf-8",
        )
        applicable = self._read_json("next")
        self.assertTrue(applicable["mutation_available"])
        self.assertEqual(
            applicable["commands"],
            [f"dyro --root {self.root.resolve()} bootstrap --yes"],
        )

    def test_control_plane_next_never_offers_bootstrap_through_symlink_parent(
        self,
    ) -> None:
        (self.config.lines_state_dir / "alpha.toml").unlink()
        self.anchor.rename(self.root / "api-missing")
        outside = self.root.parent / f"{self.root.name}-outside"
        outside.mkdir()
        escape = self.root / "escape"
        escape.symlink_to(outside, target_is_directory=True)
        config_path = self.root / "dyro.toml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8")
            .replace('path = "repositories/api"', 'path = "escape/api"')
            .replace(
                'mount = "services/api"',
                'mount = "services/api"\nremote = "https://example.invalid/api.git"',
            ),
            encoding="utf-8",
        )

        payload = self._read_json("next")

        self.assertFalse(payload["mutation_available"])
        self.assertEqual(payload["commands"], [])
        self.assertFalse((outside / "api").exists())

    def test_control_plane_rejects_symlinked_line_and_changeset_manifests(self) -> None:
        line_path = self.config.lines_state_dir / "alpha.toml"
        line_target = self.root / "outside-line.toml"
        line_target.write_bytes(line_path.read_bytes())
        line_path.unlink()
        line_path.symlink_to(line_target)

        stderr = StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            main(
                [
                    "--root",
                    str(self.root),
                    "line",
                    "list",
                    "--format",
                    "json",
                ]
            )
        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(json.loads(stderr.getvalue())["code"], "UNSAFE_FILE")

        line_path.unlink()
        line_path.write_bytes(line_target.read_bytes())
        main(
            [
                "--root",
                str(self.root),
                "changeset",
                "create",
                "release-candidate",
                "--line",
                "alpha",
            ]
        )
        changeset_path = self.config.changesets_dir / "release-candidate.toml"
        changeset_target = self.root / "outside-changeset.toml"
        changeset_target.write_bytes(changeset_path.read_bytes())
        changeset_path.unlink()
        changeset_path.symlink_to(changeset_target)

        stderr = StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            main(
                [
                    "--root",
                    str(self.root),
                    "changeset",
                    "list",
                    "--format",
                    "json",
                ]
            )
        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(json.loads(stderr.getvalue())["code"], "UNSAFE_FILE")

    def test_control_plane_rejects_a_symlinked_profile(self) -> None:
        profile = self.root / "dyro.toml"
        target = self.root / "outside-profile.toml"
        target.write_bytes(profile.read_bytes())
        profile.unlink()
        profile.symlink_to(target)

        stdout = StringIO()
        stderr = StringIO()
        with (
            redirect_stdout(stdout),
            redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            main(
                [
                    "--root",
                    str(self.root),
                    "status",
                    "--format",
                    "json",
                ]
            )

        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(
            json.loads(stderr.getvalue())["code"], "LOCAL_PROFILE_INVALID"
        )

    def test_control_plane_objective_plan_rejects_a_symlinked_task(self) -> None:
        self._start_release_objective()
        task_directory = self.config.task_specs_dir / "TASK-A"
        outside = self.root / "outside-task"
        task_directory.rename(outside)
        task_directory.symlink_to(outside, target_is_directory=True)

        stdout = StringIO()
        stderr = StringIO()
        with (
            redirect_stdout(stdout),
            redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            main(
                [
                    "--root",
                    str(self.root),
                    "objective",
                    "plan",
                    "release",
                    "--format",
                    "json",
                ]
            )

        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(json.loads(stderr.getvalue())["code"], "UNSAFE_FILE")

    def test_control_plane_changeset_views_have_stable_json_shapes(self) -> None:
        main(
            [
                "--root",
                str(self.root),
                "changeset",
                "create",
                "release-candidate",
                "--line",
                "alpha",
            ]
        )

        listed = self._read_json("changeset", "list")
        self.assertEqual(listed["kind"], "changeset_list")
        self.assertEqual(listed["changesets"][0]["id"], "release-candidate")
        self.assertEqual(set(listed["changesets"][0]["heads"]), {"api"})

        verified = self._read_json(
            "changeset", "verify", "release-candidate"
        )
        self.assertEqual(verified["kind"], "changeset_verification")
        self.assertTrue(verified["passed"])
        self.assertEqual(verified["findings"][0]["status"], "PASS")

    def test_objective_list_and_status_json_are_strictly_non_recovering(self) -> None:
        self._start_release_objective()

        listed = self._read_json("objective", "list")
        self.assertEqual(listed["kind"], "objective_list")
        self.assertEqual(listed["objectives"][0]["id"], "release")
        detailed = self._read_json("objective", "status", "release")
        self.assertEqual(detailed["kind"], "objective_status")
        self.assertEqual(detailed["objective"]["derived_result"], "incomplete")

        with patch(
            "dyro.continuation.objective_storage.write_projection",
            side_effect=OSError("simulated crash"),
        ):
            with self.assertRaisesRegex(OSError, "simulated crash"):
                pause_objective(self.config, "release")

        objective_dir = self.config.objectives_dir / "release"
        before = {
            path.relative_to(objective_dir): path.read_bytes()
            for path in objective_dir.rglob("*")
            if path.is_file()
        }
        self.assertIn(Path("pending.json"), before)

        for argv in (
            ("objective", "list", "--format", "json"),
            ("objective", "status", "release", "--format", "json"),
        ):
            stdout = StringIO()
            stderr = StringIO()
            with (
                redirect_stdout(stdout),
                redirect_stderr(stderr),
                self.assertRaises(SystemExit) as raised,
            ):
                main(["--root", str(self.root), *argv])
            self.assertEqual(raised.exception.code, 2)
            self.assertEqual(stdout.getvalue(), "")
            error = json.loads(stderr.getvalue())
            self.assertEqual(error["kind"], "error")
            self.assertEqual(error["code"], "OBJECTIVE_UNAVAILABLE")
            after = {
                path.relative_to(objective_dir): path.read_bytes()
                for path in objective_dir.rglob("*")
                if path.is_file()
            }
            self.assertEqual(after, before)

    def test_objective_json_reports_completed_integrated_target(self) -> None:
        self._start_release_objective()
        task_directory = self.config.task_specs_dir / "TASK-A"
        task_directory.joinpath("status").write_text("done\n", encoding="utf-8")
        line = get_line(self.config, "alpha")
        target = line_repository_path(self.config, line, "api")
        head = subprocess.run(
            ["git", "-C", str(target), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        task_directory.joinpath("task-heads.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "task_id": "TASK-A",
                    "line": "alpha",
                    "branch": "task/TASK-A",
                    "repositories": {"api": head},
                }
            ),
            encoding="utf-8",
        )

        detailed = self._read_json("objective", "status", "release")

        self.assertEqual(detailed["objective"]["derived_result"], "complete")

    def test_objective_json_rejects_unmanifested_imported_task_heads(self) -> None:
        self._start_release_objective()
        task_directory = self.config.task_specs_dir / "TASK-A"
        task_directory.joinpath("status").write_text("done\n", encoding="utf-8")
        line = get_line(self.config, "alpha")
        target = line_repository_path(self.config, line, "api")
        head = subprocess.run(
            ["git", "-C", str(target), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        generation = publish_evidence_generation(
            task_directory,
            "attempt-1",
            {"receipt.md": b"result: DONE\n"},
        )
        generation.chmod(0o700)
        generation.joinpath("task-heads.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "task_id": "TASK-A",
                    "line": "alpha",
                    "branch": "task/TASK-A",
                    "repositories": {"api": head},
                }
            ),
            encoding="utf-8",
        )

        detailed = self._read_json("objective", "status", "release")

        self.assertEqual(detailed["objective"]["derived_result"], "incomplete")

    def test_objective_start_dry_run_has_zero_writes_and_lifecycle_commands_work(
        self,
    ) -> None:
        root = str(self.root)
        main(
            [
                "--root",
                root,
                "--dry-run",
                "objective",
                "start",
                "--id",
                "release",
                "--title",
                "Release",
                "--line",
                "alpha",
                "--targets",
                "TASK-A",
            ]
        )
        self.assertFalse(self.config.objectives_dir.exists())
        main(
            [
                "--root",
                root,
                "objective",
                "start",
                "--id",
                "release",
                "--title",
                "Release",
                "--line",
                "alpha",
                "--targets",
                "TASK-A",
                "--yes",
            ]
        )
        stderr = StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as rejected:
            main(["--root", root, "task", "loop"])
        self.assertEqual(rejected.exception.code, 2)
        self.assertIn("不能绕过 ownership", stderr.getvalue())
        main(["--root", root, "objective", "pause", "release", "--yes"])
        main(["--root", root, "objective", "resume", "release", "--yes"])
        output = StringIO()
        with redirect_stdout(output):
            main(["--root", root, "objective", "status", "release"])
        self.assertIn("Derived result: incomplete", output.getvalue())

    def test_objective_read_only_plan_explain_graph_tick_and_attention_do_not_mutate_state(
        self,
    ) -> None:
        root = str(self.root)
        main(
            [
                "--root",
                root,
                "objective",
                "start",
                "--id",
                "release",
                "--title",
                "Release",
                "--line",
                "alpha",
                "--targets",
                "TASK-A",
                "--yes",
            ]
        )
        objective_dir = self.config.objectives_dir / "release"
        before = {
            path.relative_to(objective_dir): path.read_bytes()
            for path in objective_dir.rglob("*")
            if path.is_file()
        }
        plan_output = StringIO()
        with redirect_stdout(plan_output):
            main(["--root", root, "objective", "plan", "release", "--format", "json"])
        self.assertIn('"kind": "execute_task"', plan_output.getvalue())
        explain_output = StringIO()
        with redirect_stdout(explain_output):
            main(["--root", root, "objective", "explain", "release"])
        explain_text = explain_output.getvalue()
        self.assertIn("Objective: release", explain_text)
        self.assertIn("下一步：", explain_text)
        self.assertNotIn(str(self.root.resolve()), explain_text)
        graph_output = StringIO()
        with redirect_stdout(graph_output):
            main(
                ["--root", root, "objective", "graph", "release", "--format", "mermaid"]
            )
        self.assertIn("flowchart LR", graph_output.getvalue())
        tick_output = StringIO()
        with redirect_stdout(tick_output):
            main(["--root", root, "objective", "tick", "release", "--format", "json"])
        self.assertIn('"tick_sha256"', tick_output.getvalue())
        self.assertIn('"wave"', tick_output.getvalue())
        attention_output = StringIO()
        with redirect_stdout(attention_output):
            main(
                [
                    "--root",
                    root,
                    "objective",
                    "attention",
                    "release",
                    "--format",
                    "json",
                ]
            )
        self.assertIn('"attention_sha256"', attention_output.getvalue())
        self.assertIn('"items"', attention_output.getvalue())
        after = {
            path.relative_to(objective_dir): path.read_bytes()
            for path in objective_dir.rglob("*")
            if path.is_file()
        }
        self.assertEqual(before, after)

    def test_objective_explain_json_includes_path_free_briefing(self) -> None:
        self._start_release_objective()
        payload = self._read_json("objective", "explain", "release")
        briefing = payload["briefing"]
        blob = json.dumps(payload)
        self.assertTrue(briefing["available"])
        self.assertEqual(briefing["objective_id"], "release")
        self.assertIn("--workspace test-workspace", briefing["command"])
        self.assertNotIn("--root", briefing["command"])
        self.assertNotIn(str(self.root.resolve()), blob)
        self.assertNotIn("session", blob.lower())
        self.assertIn("plan_sha256", payload)
        self.assertIn("下一步：", "\n".join(briefing["lines"]))

    def test_next_with_one_live_objective_points_to_follow_up(self) -> None:
        self._start_release_objective()
        explain = self._read_json("objective", "explain", "release")
        payload = self._read_json("next")
        briefing = payload["briefing"]
        self.assertEqual(payload["kind"], "next_step")
        self.assertEqual(payload["state"], "ready")
        self.assertEqual(payload["commands"], [])
        self.assertFalse(payload["mutation_available"])
        self.assertEqual(
            briefing["command"],
            "dyro --workspace test-workspace objective tick release",
        )
        self.assertEqual(briefing["command"], explain["briefing"]["command"])
        self.assertEqual(payload["diagnostic_commands"], [briefing["command"]])
        self.assertNotIn("objective apply", json.dumps(payload))
        self.assertNotIn(str(self.root.resolve()), json.dumps(payload))

    def test_bare_dyro_prints_the_same_follow_up_before_the_home_menu(self) -> None:
        self._start_release_objective()
        output = StringIO()
        with (
            patch("dyro.home.interactive_terminal", return_value=False),
            redirect_stdout(output),
        ):
            main(["--root", str(self.root)])
        text = output.getvalue()
        self.assertRegex(text, r"dyro --workspace \S+ objective tick release")
        self.assertIn("今天做什么", text)
        self.assertIn("做下一步，不打开编码工具", text)
        self.assertNotIn("objective apply", text)

    def test_bare_dyro_without_objectives_does_not_invent_a_briefing(self) -> None:
        output = StringIO()
        with (
            patch("dyro.home.interactive_terminal", return_value=False),
            redirect_stdout(output),
        ):
            main(["--root", str(self.root)])
        text = output.getvalue()
        self.assertIn("今天做什么", text)
        self.assertNotIn("objective tick", text)
        self.assertNotIn("objective attention", text)

    def test_next_with_two_live_objectives_does_not_pick_one(self) -> None:
        self._start_release_objective()
        directory = self.config.task_specs_dir / "TASK-B"
        directory.mkdir(parents=True)
        directory.joinpath("task.toml").write_text(
            task_template("TASK-B", "Task B", "alpha", "api", "services/api").replace(
                'agent = "codex"', 'agent = "noop"'
            ),
            encoding="utf-8",
        )
        directory.joinpath("handoff.md").write_text("# handoff\n", encoding="utf-8")
        main(
            [
                "--root",
                str(self.root),
                "objective",
                "start",
                "--id",
                "hotfix",
                "--title",
                "Hotfix",
                "--line",
                "alpha",
                "--targets",
                "TASK-B",
                "--yes",
            ]
        )
        payload = self._read_json("next")
        self.assertEqual(payload["commands"], [])
        self.assertFalse(payload["mutation_available"])
        self.assertEqual(payload["briefing"]["objective_id"], "")
        self.assertIn("objective list", payload["briefing"]["command"])
        self.assertIn("多个未停止的目标", payload["briefing"]["matter"])

    def test_objective_tick_text_leads_with_human_arrival(self) -> None:
        self._start_release_objective()
        output = StringIO()
        with redirect_stdout(output):
            main(["--root", str(self.root), "objective", "tick", "release"])
        text = output.getvalue()
        self.assertIn("Release · 未完成", text)
        self.assertIn("这是预览，还没有执行。当前窗口可以接着做。", text)
        self.assertIn("本轮可以推进：", text)
        self.assertIn("执行 · 有任务可以继续做（TASK-A）", text)
        self.assertIn("Tick SHA-256", text)
        self.assertNotIn("objective apply", text)
        payload = self._read_json("objective", "tick", "release")
        self.assertNotIn("briefing", payload)
        self.assertIn("tick_sha256", payload)

    def test_objective_attention_text_leads_with_human_arrival(self) -> None:
        self._start_release_objective()
        output = StringIO()
        with redirect_stdout(output):
            main(["--root", str(self.root), "objective", "attention", "release"])
        text = output.getvalue()
        self.assertIn("Release · 未完成", text)
        self.assertIn("这些事项需要你处理。当前窗口可以接着做。", text)
        self.assertIn("Attention SHA-256", text)
        payload = self._read_json("objective", "attention", "release")
        self.assertNotIn("briefing", payload)
        self.assertIn("attention_sha256", payload)

    def test_objective_apply_dry_run_shows_the_exact_wave_without_writing(self) -> None:
        root = str(self.root)
        main(
            [
                "--root",
                root,
                "objective",
                "start",
                "--id",
                "release",
                "--title",
                "Release",
                "--line",
                "alpha",
                "--targets",
                "TASK-A",
                "--yes",
            ]
        )
        objective_dir = self.config.objectives_dir / "release"
        before = {
            path.relative_to(objective_dir): path.read_bytes()
            for path in objective_dir.rglob("*")
            if path.is_file()
        }
        output = StringIO()
        with redirect_stdout(output):
            main(["--root", root, "--dry-run", "objective", "apply", "release"])
        after = {
            path.relative_to(objective_dir): path.read_bytes()
            for path in objective_dir.rglob("*")
            if path.is_file()
        }
        self.assertIn("Tick SHA-256", output.getvalue())
        self.assertIn("DRY RUN", output.getvalue())
        self.assertEqual(before, after)

    def test_objective_apply_noninteractive_uses_stable_confirmation_and_json_envelope(
        self,
    ) -> None:
        from dyro.continuation.supervision import build_supervised_wave

        root = str(self.root)
        main(
            [
                "--root",
                root,
                "objective",
                "start",
                "--id",
                "release",
                "--title",
                "Release",
                "--line",
                "alpha",
                "--targets",
                "TASK-A",
                "--yes",
            ]
        )
        confirmation = build_supervised_wave(self.config, "release").confirmation_sha256
        output = StringIO()
        with (
            patch("dyro.cli.apply_supervised_wave", return_value=()) as apply,
            redirect_stdout(output),
        ):
            main(
                [
                    "--root",
                    root,
                    "objective",
                    "apply",
                    "release",
                    "--yes",
                    "--confirm-sha",
                    confirmation,
                    "--format",
                    "json",
                ]
            )
        payload = json.loads(output.getvalue())
        self.assertFalse(payload["dry_run"])
        self.assertEqual(payload["outcomes"], [])
        self.assertIn("confirmation_sha256", payload["wave"])
        apply.assert_called_once()

    def test_objective_apply_rejects_a_semantically_stale_confirmation(self) -> None:
        from dyro.continuation.supervision import build_supervised_wave
        from dyro.tasks import set_status

        root = str(self.root)
        main(
            [
                "--root",
                root,
                "objective",
                "start",
                "--id",
                "release",
                "--title",
                "Release",
                "--line",
                "alpha",
                "--targets",
                "TASK-A",
                "--yes",
            ]
        )
        confirmation = build_supervised_wave(self.config, "release").confirmation_sha256
        set_status(self.config, load_task(self.config, "TASK-A"), "assigned")
        stderr = StringIO()
        with (
            patch("dyro.cli.apply_supervised_wave") as apply,
            redirect_stderr(stderr),
            self.assertRaises(SystemExit) as rejected,
        ):
            main(
                [
                    "--root",
                    root,
                    "objective",
                    "apply",
                    "release",
                    "--yes",
                    "--confirm-sha",
                    confirmation,
                ]
            )
        self.assertEqual(rejected.exception.code, 2)
        self.assertIn("Confirmation SHA-256", stderr.getvalue())
        apply.assert_not_called()


class DaemonSelectionTests(WorkspaceCase):
    def test_daemon_selects_backlog_tasks_like_loop(self) -> None:
        from dyro.cli import _daemon_select_runnable
        from dyro.tasks import list_tasks, status, task_template
        from dyro.workspace import create_line

        config = load(self.root)
        create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        task_path = config.task_specs_dir / "TASK-DAEMON"
        task_path.mkdir(parents=True)
        task_path.joinpath("task.toml").write_text(
            task_template(
                "TASK-DAEMON", "daemon backlog", "alpha", "api", "services/api"
            ).replace('agent = "codex"', 'agent = "noop"'),
            encoding="utf-8",
        )
        task_path.joinpath("handoff.md").write_text("# handoff\n", encoding="utf-8")
        tasks = list_tasks(config)
        self.assertEqual(status(config, tasks[0]), "backlog")

        selected = _daemon_select_runnable(config, tasks, limit=2)
        self.assertEqual([task.id for task in selected], ["TASK-DAEMON"])

    def test_daemon_once_dispatches_backlog_task(self) -> None:
        from dyro.tasks import load_task, status, task_template
        from dyro.workspace import create_line

        config = load(self.root)
        create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        task_path = config.task_specs_dir / "TASK-ONCE"
        task_path.mkdir(parents=True)
        task_path.joinpath("task.toml").write_text(
            task_template(
                "TASK-ONCE", "daemon once", "alpha", "api", "services/api"
            ).replace('agent = "codex"', 'agent = "noop"'),
            encoding="utf-8",
        )
        task_path.joinpath("handoff.md").write_text("# handoff\n", encoding="utf-8")
        task_path.joinpath("receipt.md").write_text("result: DONE\n", encoding="utf-8")

        main(["--root", str(self.root), "task", "daemon", "--once", "--parallel", "1"])
        self.assertEqual(status(config, load_task(config, "TASK-ONCE")), "review")


class VersionTests(unittest.TestCase):
    def test_package_version_matches_pyproject(self) -> None:
        import tomllib
        from pathlib import Path

        from dyro import __version__

        metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(__version__, metadata["project"]["version"])

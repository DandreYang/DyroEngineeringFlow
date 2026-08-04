from pathlib import Path
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import patch

from dyro.cli import _route_experiment_surface, _setup_provider_preset, main
from dyro.changesets import get_changeset
from dyro.config import load
from dyro.hub import load_registry
from dyro.tasks import load_task, status, task_template
from dyro.workspace import create_line, get_line

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

    def test_init_creates_workspace_contract(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dyro-cli-") as tmp:
            root = Path(tmp) / "workspace"
            main(["init", str(root), "--name", "demo"])
            self.assertTrue((root / "dyro.toml").exists())
            self.assertTrue((root / ".dyro/tasks").is_dir())
            self.assertEqual(load(root).name, "demo")

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

    def test_setup_reports_detected_but_unintegrated_providers_without_registering_them(self) -> None:
        discovered = {
            "agy",
            "claude",
            "cursor-agent",
            "grok",
            "opencode",
            "hermes",
            "kimi",
            "qodercli",
        }
        output = StringIO()
        with (
            patch("dyro.cli.shutil.which", side_effect=lambda command: f"/fake/{command}" if command in discovered else None),
            redirect_stdout(output),
        ):
            self.assertIsNone(_setup_provider_preset())

        rendered = output.getvalue()
        for command in discovered:
            self.assertIn(command, rendered)
        self.assertIn("不会写入配置", rendered)

    def test_interactive_setup_can_be_cancelled_without_writing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dyro-cli-") as tmp:
            root = Path(tmp) / "workspace"
            repository = root / "repositories/api"
            repository.mkdir(parents=True)
            from .support import shell

            shell("git", "init", "-b", "main", cwd=repository)
            answers = iter(["", "", "", "n"])
            with (
                patch("dyro.cli._setup_provider_preset", return_value=None),
                patch("builtins.input", side_effect=lambda _: next(answers)),
            ):
                main(["setup", str(root), "--interactive"])

            self.assertFalse((root / "dyro.toml").exists())
            self.assertFalse((root / ".dyro").exists())

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
            answers = iter(["", "", "", "y"])
            with (
                patch("dyro.cli._setup_provider_preset", return_value=None),
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
            answers = iter(["", "", "", "", "y"])
            with (
                patch("dyro.cli._setup_provider_preset", return_value=None),
                patch("builtins.input", side_effect=lambda _: next(answers)),
            ):
                main(["setup", str(repository), "--interactive"])

            self.assertFalse((repository / "dyro.toml").exists())
            self.assertTrue((sibling / "dyro.toml").is_file())
            config = load(sibling)
            self.assertEqual(len(config.repositories), 1)
            self.assertTrue((sibling / "versions/dev").is_dir())

    def test_interactive_setup_uses_the_source_branch_as_the_suggested_base(self) -> None:
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
            answers = iter(["", "", "", "", "y"])
            with (
                patch("dyro.cli._setup_provider_preset", return_value=None),
                patch("builtins.input", side_effect=lambda _: next(answers)),
            ):
                main(["setup", str(repository), "--interactive"])

            config = load(sibling)
            self.assertEqual(config.policy.default_base, "trunk")
            self.assertEqual(get_line(config, "dev").base, "trunk")


class StartTests(WorkspaceCase):
    def test_start_dry_run_uses_selected_line_and_adapter(self) -> None:
        config = load(self.root)
        create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        main(["--root", str(self.root), "--dry-run", "start", "--line", "alpha", "--agent", "noop"])

    def test_next_without_a_profile_explains_how_to_begin(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dyro-cli-") as tmp:
            output = StringIO()
            with redirect_stdout(output):
                main(["--root", str(Path(tmp) / "empty"), "next"])

            self.assertIn("dyro join", output.getvalue())
            self.assertIn("dyro setup", output.getvalue())


class LineCommandsTests(WorkspaceCase):
    def test_line_create_records_per_repository_base_and_storage_without_toml_edits(self) -> None:
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

    def test_changeset_create_records_a_delivery_line_without_manual_toml_edit(self) -> None:
        config = load(self.root)
        create_line(config, line_id="alpha", branch="feat/alpha", base="main")

        main(["--root", str(self.root), "changeset", "create", "alpha-ready", "--line", "alpha"])

        self.assertEqual(get_changeset(load(self.root), "alpha-ready").line, "alpha")


class RepositoryCommandsTests(WorkspaceCase):
    def test_repo_add_registers_an_existing_git_repository_without_manual_toml_edit(self) -> None:
        web = self.root / "repositories/web"
        web.mkdir(parents=True)
        from .support import shell

        shell("git", "init", "-b", "main", cwd=web)
        shell("git", "remote", "add", "origin", "https://example.test/acme/web.git", cwd=web)
        main(["--root", str(self.root), "repo", "add", "repositories/web"])

        config = load(self.root)
        self.assertEqual(config.repositories["web"].path, "repositories/web")
        self.assertEqual(config.repositories["web"].mount, "web")
        self.assertEqual(config.repositories["web"].remote, "https://example.test/acme/web.git")


class ProfileCommandsTests(WorkspaceCase):
    def test_config_and_agent_management_do_not_require_manual_toml_edits(self) -> None:
        main(["--root", str(self.root), "config", "set", "policy.execution_mode", "external"])
        self.assertEqual(load(self.root).policy.execution_mode, "external")

        main(["--root", str(self.root), "agent", "add", "isolated", "--preset", "noop"])
        self.assertIn("isolated", load(self.root).adapters)
        main(["--root", str(self.root), "agent", "test", "isolated"])


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
            task_template("TASK-A", "Task A", "alpha", "api", "services/api").replace('agent = "codex"', 'agent = "noop"'),
            encoding="utf-8",
        )
        directory.joinpath("handoff.md").write_text("# handoff\n", encoding="utf-8")

    def test_objective_start_dry_run_has_zero_writes_and_lifecycle_commands_work(self) -> None:
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

    def test_objective_read_only_plan_explain_graph_tick_and_attention_do_not_mutate_state(self) -> None:
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
        before = {path.relative_to(objective_dir): path.read_bytes() for path in objective_dir.rglob("*") if path.is_file()}
        plan_output = StringIO()
        with redirect_stdout(plan_output):
            main(["--root", root, "objective", "plan", "release", "--format", "json"])
        self.assertIn('"kind": "execute_task"', plan_output.getvalue())
        explain_output = StringIO()
        with redirect_stdout(explain_output):
            main(["--root", root, "objective", "explain", "release"])
        self.assertIn("Objective: release", explain_output.getvalue())
        graph_output = StringIO()
        with redirect_stdout(graph_output):
            main(["--root", root, "objective", "graph", "release", "--format", "mermaid"])
        self.assertIn("flowchart LR", graph_output.getvalue())
        tick_output = StringIO()
        with redirect_stdout(tick_output):
            main(["--root", root, "objective", "tick", "release", "--format", "json"])
        self.assertIn('"tick_sha256"', tick_output.getvalue())
        self.assertIn('"wave"', tick_output.getvalue())
        attention_output = StringIO()
        with redirect_stdout(attention_output):
            main(["--root", root, "objective", "attention", "release", "--format", "json"])
        self.assertIn('"attention_sha256"', attention_output.getvalue())
        self.assertIn('"items"', attention_output.getvalue())
        after = {path.relative_to(objective_dir): path.read_bytes() for path in objective_dir.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_objective_apply_dry_run_shows_the_exact_wave_without_writing(self) -> None:
        root = str(self.root)
        main(
            [
                "--root", root, "objective", "start", "--id", "release", "--title", "Release",
                "--line", "alpha", "--targets", "TASK-A", "--yes",
            ]
        )
        objective_dir = self.config.objectives_dir / "release"
        before = {path.relative_to(objective_dir): path.read_bytes() for path in objective_dir.rglob("*") if path.is_file()}
        output = StringIO()
        with redirect_stdout(output):
            main(["--root", root, "--dry-run", "objective", "apply", "release"])
        after = {path.relative_to(objective_dir): path.read_bytes() for path in objective_dir.rglob("*") if path.is_file()}
        self.assertIn("Tick SHA-256", output.getvalue())
        self.assertIn("DRY RUN", output.getvalue())
        self.assertEqual(before, after)

    def test_objective_apply_noninteractive_uses_stable_confirmation_and_json_envelope(self) -> None:
        from dyro.continuation.supervision import build_supervised_wave

        root = str(self.root)
        main(
            [
                "--root", root, "objective", "start", "--id", "release", "--title", "Release",
                "--line", "alpha", "--targets", "TASK-A", "--yes",
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
                    "--root", root, "objective", "apply", "release", "--yes",
                    "--confirm-sha", confirmation, "--format", "json",
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
                "--root", root, "objective", "start", "--id", "release", "--title", "Release",
                "--line", "alpha", "--targets", "TASK-A", "--yes",
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
                    "--root", root, "objective", "apply", "release", "--yes",
                    "--confirm-sha", confirmation,
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
            task_template("TASK-DAEMON", "daemon backlog", "alpha", "api", "services/api").replace(
                'agent = "codex"', 'agent = "noop"'
            ),
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
            task_template("TASK-ONCE", "daemon once", "alpha", "api", "services/api").replace(
                'agent = "codex"', 'agent = "noop"'
            ),
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

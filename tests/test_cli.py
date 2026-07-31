from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO

from dyro.cli import _route_experiment_surface, main
from dyro.changesets import get_changeset
from dyro.config import load
from dyro.tasks import load_task, status, task_template
from dyro.workspace import create_line, get_line

from .support import WorkspaceCase


class CliTests(unittest.TestCase):
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


class StartTests(WorkspaceCase):
    def test_start_dry_run_uses_selected_line_and_adapter(self) -> None:
        config = load(self.root)
        create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        main(["--root", str(self.root), "--dry-run", "start", "--line", "alpha", "--agent", "noop"])


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

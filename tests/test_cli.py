from pathlib import Path
import tempfile
import unittest

from dyro.cli import main
from dyro.changesets import get_changeset
from dyro.config import load
from dyro.workspace import create_line, get_line

from .support import WorkspaceCase


class CliTests(unittest.TestCase):
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

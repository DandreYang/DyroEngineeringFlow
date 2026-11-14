from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

from dyro import line_ops, workspace
from dyro.cli import main
from dyro.config import load
from dyro.errors import DyroError, ValidationError
from dyro.workspace import (
    _write_line,
    add_line_repository,
    create_line,
    get_line,
    line_repository_path,
    line_root,
    spawn_line,
)

from .support import WorkspaceCase, shell


def git_stdout(*args: str, cwd: Path) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.stdout.strip()


def branch_names(repo: Path) -> set[str]:
    text = git_stdout("branch", "--list", "--format=%(refname:short)", cwd=repo)
    return set(filter(None, text.splitlines()))


class LineAddRepoTests(WorkspaceCase):
    def _register_web(self) -> Path:
        web = self.root / "repositories/web"
        web.mkdir(parents=True)
        shell("git", "init", "-b", "main", cwd=web)
        shell("git", "config", "user.name", "Test User", cwd=web)
        shell("git", "config", "user.email", "test@example.com", cwd=web)
        shell("git", "config", "commit.gpgsign", "false", cwd=web)
        (web / "README.md").write_text("web\n", encoding="utf-8")
        shell("git", "add", "README.md", cwd=web)
        shell("git", "commit", "-m", "chore: initial", cwd=web)
        config_path = self.root / "dyro.toml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8")
            + '\n[repositories.web]\npath = "repositories/web"\nmount = "clients/web"\n',
            encoding="utf-8",
        )
        return web

    def _line_with_api(self, line_id: str = "alpha"):
        config = load(self.root)
        line = create_line(
            config,
            line_id=line_id,
            branch=f"feat/{line_id}",
            base="main",
            repositories=["api"],
        )
        return config, line

    def _manifest(self, config, line_id: str) -> Path:
        return config.lines_state_dir / f"{line_id}.toml"

    def test_top_level_line_adds_registered_repo_from_base(self) -> None:
        config, line = self._line_with_api()
        api_worktree = line_repository_path(config, line, "api")
        marker = api_worktree / "keep.txt"
        marker.write_text("untouched\n", encoding="utf-8")
        api_head = git_stdout("rev-parse", "HEAD", cwd=api_worktree)
        web = self._register_web()
        config = load(self.root)
        base_sha = git_stdout("rev-parse", "main^{commit}", cwd=web)

        updated = add_line_repository(config, "alpha", "web")

        destination = line_repository_path(config, updated, "web")
        self.assertTrue((destination / ".git").exists())
        self.assertFalse(destination.is_symlink())
        self.assertEqual(git_stdout("branch", "--show-current", cwd=destination), line.branch)
        self.assertEqual(git_stdout("rev-parse", "HEAD", cwd=destination), base_sha)
        self.assertEqual(get_line(config, "alpha").repositories, ("api", "web"))
        self.assertEqual(marker.read_text(encoding="utf-8"), "untouched\n")
        self.assertEqual(git_stdout("rev-parse", "HEAD", cwd=api_worktree), api_head)
        self.assertTrue(api_worktree.is_dir())

    def test_custom_base_is_persisted_and_default_base_is_not(self) -> None:
        config, _line = self._line_with_api()
        web = self._register_web()
        shell("git", "checkout", "-b", "release", cwd=web)
        (web / "release.txt").write_text("rel\n", encoding="utf-8")
        shell("git", "add", "release.txt", cwd=web)
        shell("git", "commit", "-m", "release", cwd=web)
        release_sha = git_stdout("rev-parse", "HEAD", cwd=web)
        shell("git", "checkout", "main", cwd=web)
        config = load(self.root)

        updated = add_line_repository(config, "alpha", "web", base="release")

        destination = line_repository_path(config, updated, "web")
        manifest = self._manifest(config, "alpha").read_text(encoding="utf-8")
        self.assertEqual(updated.repository_bases["web"], "release")
        self.assertIn("[repository_bases]", manifest)
        self.assertIn('"web" = "release"', manifest)
        self.assertEqual(git_stdout("rev-parse", "HEAD", cwd=destination), release_sha)
        self.assertTrue((destination / "release.txt").is_file())

        create_line(
            config,
            line_id="beta",
            branch="feat/beta",
            base="main",
            repositories=["api"],
        )
        defaulted = add_line_repository(config, "beta", "web")
        beta_manifest = self._manifest(config, "beta").read_text(encoding="utf-8")
        self.assertNotIn("web", defaulted.repository_bases)
        self.assertNotIn("[repository_bases]", beta_manifest)
        self.assertEqual(defaulted.base_for("web"), "main")

    def test_existing_local_branch_is_reused(self) -> None:
        config, line = self._line_with_api()
        web = self._register_web()
        shell("git", "checkout", "-b", line.branch, cwd=web)
        (web / "extra.txt").write_text("extra\n", encoding="utf-8")
        shell("git", "add", "extra.txt", cwd=web)
        shell("git", "commit", "-m", "extra", cwd=web)
        tip = git_stdout("rev-parse", "HEAD", cwd=web)
        main_sha = git_stdout("rev-parse", "main", cwd=web)
        shell("git", "checkout", "main", cwd=web)
        before = branch_names(web)
        config = load(self.root)

        updated = add_line_repository(config, "alpha", "web")

        destination = line_repository_path(config, updated, "web")
        self.assertEqual(git_stdout("rev-parse", "HEAD", cwd=destination), tip)
        self.assertNotEqual(tip, main_sha)
        self.assertEqual(branch_names(web), before)
        self.assertTrue((destination / "extra.txt").is_file())

    def test_already_included_and_unregistered_repo_write_nothing(self) -> None:
        config, line = self._line_with_api()
        manifest = self._manifest(config, "alpha")
        before = manifest.read_bytes()
        root = line_root(config, line)
        before_tree = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))

        with self.assertRaisesRegex(DyroError, "已包含"):
            add_line_repository(config, "alpha", "api")
        with self.assertRaisesRegex(ValidationError, "未配置的仓库：missing"):
            add_line_repository(config, "alpha", "missing")

        self.assertEqual(manifest.read_bytes(), before)
        self.assertEqual(
            sorted(path.relative_to(root).as_posix() for path in root.rglob("*")),
            before_tree,
        )

    def test_child_derives_base_and_storage_and_rejects_overrides(self) -> None:
        config, parent = self._line_with_api("onboard")
        child = spawn_line(config, parent.id, "tryon")
        web = self._register_web()
        config = load(self.root)
        child_manifest = self._manifest(config, child.id)
        before = child_manifest.read_bytes()
        child_destination = self.root / "versions" / child.id / "clients" / "web"

        with self.assertRaisesRegex(ValidationError, parent.id):
            add_line_repository(config, child.id, "web")
        self.assertEqual(child_manifest.read_bytes(), before)
        self.assertFalse(child_destination.exists())

        shell("git", "checkout", "-b", parent.branch, cwd=web)
        add_line_repository(config, parent.id, "web", storage="anchor-reference")
        config = load(self.root)
        parent = get_line(config, parent.id)
        self.assertEqual(child_manifest.read_bytes(), before)

        with self.assertRaisesRegex(ValidationError, "--base"):
            add_line_repository(config, child.id, "web", base="main")
        with self.assertRaisesRegex(ValidationError, "--storage"):
            add_line_repository(config, child.id, "web", storage="linked-worktree")
        self.assertEqual(child_manifest.read_bytes(), before)
        self.assertFalse(child_destination.exists())

        shell("git", "checkout", "-b", child.branch, cwd=web)
        updated = add_line_repository(config, child.id, "web")
        self.assertEqual(updated.base_for("web"), parent.branch)
        self.assertEqual(updated.storage_for("web"), parent.storage_for("web"))
        self.assertEqual(updated.storage_for("web"), "anchor-reference")
        self.assertTrue(child_destination.is_symlink())
        reloaded = get_line(config, child.id)
        self.assertEqual(reloaded.repositories, ("api", "web"))
        self.assertEqual(reloaded.base_for("web"), parent.branch)

    def test_adding_to_parent_leaves_child_manifest_unchanged(self) -> None:
        config, parent = self._line_with_api("onboard")
        child = spawn_line(config, parent.id, "tryon")
        self._register_web()
        config = load(self.root)
        child_manifest = self._manifest(config, child.id)
        before = child_manifest.read_bytes()
        child_web = self.root / "versions" / child.id / "clients" / "web"

        add_line_repository(config, parent.id, "web")

        self.assertEqual(child_manifest.read_bytes(), before)
        self.assertFalse(child_web.exists())
        self.assertEqual(get_line(config, child.id).repositories, ("api",))
        self.assertIn("web", get_line(config, parent.id).repositories)

    def test_unfinished_op_refuses_without_creating_a_worktree(self) -> None:
        config, _line = self._line_with_api()
        self._register_web()
        config = load(self.root)
        line_ops.create_op(
            config,
            kind="merge",
            subject_line_ids=["alpha"],
            manifest_snapshot={"id": "alpha", "repositories": ["api"]},
            repos_ordered=["api"],
            effective_options={},
        )
        manifest = self._manifest(config, "alpha")
        before = manifest.read_bytes()
        destination = self.root / "versions/alpha/clients/web"

        with self.assertRaisesRegex(DyroError, "未完成操作"):
            add_line_repository(config, "alpha", "web")

        self.assertEqual(manifest.read_bytes(), before)
        self.assertFalse(destination.exists())

    def test_write_failure_removes_only_the_new_worktree(self) -> None:
        config, line = self._line_with_api()
        empty_keep = line_repository_path(config, line, "api") / "empty-keep"
        empty_keep.mkdir()
        web = self._register_web()
        shell("git", "checkout", "-b", "release", cwd=web)
        (web / "release.txt").write_text("rel\n", encoding="utf-8")
        shell("git", "add", "release.txt", cwd=web)
        shell("git", "commit", "-m", "release", cwd=web)
        shell("git", "checkout", "main", cwd=web)
        config = load(self.root)
        manifest = self._manifest(config, "alpha")
        before = manifest.read_bytes()
        destination = self.root / "versions/alpha/clients/web"

        with patch("dyro.workspace._write_line", side_effect=RuntimeError("write failed")):
            with self.assertRaisesRegex(RuntimeError, "write failed"):
                add_line_repository(config, "alpha", "web", base="release")

        self.assertFalse(destination.exists())
        self.assertFalse(destination.parent.exists())
        self.assertTrue(empty_keep.is_dir())
        self.assertEqual(manifest.read_bytes(), before)
        self.assertTrue(line_repository_path(config, line, "api").is_dir())
        self.assertNotIn(line.branch, branch_names(web))

        # Retry with the default base must start from main, not the failed release tip.
        updated = add_line_repository(config, "alpha", "web")
        self.assertEqual(
            git_stdout("rev-parse", "HEAD", cwd=line_repository_path(config, updated, "web")),
            git_stdout("rev-parse", "main", cwd=web),
        )

    def test_write_failure_keeps_a_preexisting_branch(self) -> None:
        config, line = self._line_with_api()
        web = self._register_web()
        shell("git", "branch", line.branch, cwd=web)
        config = load(self.root)

        with patch("dyro.workspace._write_line", side_effect=RuntimeError("write failed")):
            with self.assertRaisesRegex(RuntimeError, "write failed"):
                add_line_repository(config, "alpha", "web")

        self.assertIn(line.branch, branch_names(web))
        self.assertFalse((self.root / "versions/alpha/clients/web").exists())

    def test_anchor_reference_write_failure_removes_symlink(self) -> None:
        config, line = self._line_with_api()
        web = self._register_web()
        shell("git", "checkout", "-b", line.branch, cwd=web)
        config = load(self.root)
        destination = self.root / "versions/alpha/clients/web"

        with patch("dyro.workspace._write_line", side_effect=RuntimeError("write failed")):
            with self.assertRaisesRegex(RuntimeError, "write failed"):
                add_line_repository(config, "alpha", "web", storage="anchor-reference")

        self.assertFalse(destination.is_symlink() or destination.exists())
        self.assertFalse(destination.parent.exists())
        self.assertTrue(web.is_dir())
        self.assertIn(line.branch, branch_names(web))

    def test_path_cleanup_failure_never_silently_keeps_self_created_branch(self) -> None:
        config, line = self._line_with_api()
        web = self._register_web()
        config = load(self.root)
        real_git = workspace.git

        def flaky_git(path, *args, **kwargs):
            if args[:2] in (("worktree", "remove"), ("worktree", "prune")):
                return SimpleNamespace(code=1, stdout="boom")
            return real_git(path, *args, **kwargs)

        with patch("dyro.workspace._write_line", side_effect=RuntimeError("write failed")):
            with patch("dyro.workspace.git", side_effect=flaky_git):
                with self.assertRaisesRegex(DyroError, "worktree remove failed") as caught:
                    add_line_repository(config, "alpha", "web")

        # Either the branch is gone, or the error names it so it is never left silently.
        self.assertTrue(
            line.branch not in branch_names(web) or line.branch in str(caught.exception)
        )

    def test_create_line_rollback_deletes_only_branches_it_created(self) -> None:
        web = self._register_web()
        shell("git", "branch", "feat/gamma", cwd=web)
        config = load(self.root)

        with patch("dyro.workspace._write_line", side_effect=RuntimeError("write failed")):
            with self.assertRaisesRegex(RuntimeError, "write failed"):
                create_line(
                    config,
                    line_id="gamma",
                    branch="feat/gamma",
                    base="main",
                    repositories=["api", "web"],
                )

        self.assertNotIn("feat/gamma", branch_names(self.anchor))
        self.assertIn("feat/gamma", branch_names(web))
        self.assertFalse((self.root / "versions/gamma").exists())

    def test_dry_run_plans_without_writing(self) -> None:
        config, _line = self._line_with_api()
        self._register_web()
        config = load(self.root)
        manifest = self._manifest(config, "alpha")
        before = manifest.read_bytes()
        destination = self.root / "versions/alpha/clients/web"

        updated = add_line_repository(config, "alpha", "web", dry_run=True)

        self.assertEqual(updated.repositories, ("api", "web"))
        self.assertFalse(destination.exists())
        self.assertEqual(manifest.read_bytes(), before)
        self.assertEqual(get_line(config, "alpha").repositories, ("api",))

    def test_remote_anchors_survive_add_repo(self) -> None:
        config, line = self._line_with_api()
        anchor_sha = "a" * 40
        _write_line(config, replace(get_line(config, line.id), remote_anchors={"api": anchor_sha}))
        self._register_web()
        config = load(self.root)

        add_line_repository(config, "alpha", "web")

        text = self._manifest(config, "alpha").read_text(encoding="utf-8")
        reloaded = get_line(config, "alpha")
        self.assertIn("schema_version = 4", text)
        self.assertIn("[remote_anchors]", text)
        self.assertIn(f'"api" = "{anchor_sha}"', text)
        self.assertEqual(reloaded.remote_anchors["api"], anchor_sha)
        self.assertEqual(reloaded.repositories, ("api", "web"))
        self.assertNotIn("web", reloaded.remote_anchors)

    def test_cli_refuses_without_yes_and_adds_with_yes(self) -> None:
        self._line_with_api()
        self._register_web()
        destination = self.root / "versions/alpha/clients/web"
        stderr = StringIO()
        with redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                main(["--root", str(self.root), "line", "add-repo", "alpha", "web"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--yes", stderr.getvalue())
        self.assertIn("--dry-run", stderr.getvalue())
        self.assertFalse(destination.exists())

        stdout = StringIO()
        with redirect_stdout(stdout):
            main(["--root", str(self.root), "line", "add-repo", "alpha", "web", "--yes"])
        self.assertIn(
            "已为 line alpha 加入仓库 web，分支 feat/alpha，基线 main，存储 linked-worktree",
            stdout.getvalue(),
        )
        self.assertTrue((destination / ".git").exists())
        self.assertEqual(get_line(load(self.root), "alpha").repositories, ("api", "web"))

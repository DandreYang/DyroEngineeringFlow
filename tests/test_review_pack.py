from __future__ import annotations

from dataclasses import replace
from unittest.mock import patch

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from dyro.errors import DyroError
from dyro.config import load
from dyro.review_pack import build_review_pack, run_line_verify
from dyro.workspace import create_line, line_repository_path

from tests.support import WorkspaceCase, shell


def commit_file(worktree: Path, name: str, message: str, content: str = "") -> None:
    target = worktree / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content or f"{message}\n", encoding="utf-8")
    shell("git", "add", str(target), cwd=worktree)
    shell("git", "commit", "-m", message, cwd=worktree)


class ReviewPackTests(WorkspaceCase):
    def test_run_line_verify_pass_and_fail(self) -> None:
        config_path = self.root / "dyro.toml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                'verify = [["git", "diff", "--check"]]',
                'verify = [["git", "status"], ["sh", "-c", "exit 0"]]',
            ),
            encoding="utf-8",
        )
        config = load(self.root)
        create_line(config, line_id="feat-1", branch="feat/1", base="main")
        from dyro.workspace import get_line

        line = get_line(config, "feat-1")
        results = run_line_verify(config, line, all_repos=True)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r.passed for r in results))

    def test_build_review_pack_generates_markdown_with_contracts(self) -> None:
        config_path = self.root / "dyro.toml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                'verify = [["git", "diff", "--check"]]',
                'verify = [["sh", "-c", "echo all-good"]]',
            ),
            encoding="utf-8",
        )
        config = load(self.root)
        create_line(config, line_id="feat-review", branch="feat/review", base="main")

        from dyro.workspace import get_line

        line = get_line(config, "feat-review")
        api_path = line_repository_path(config, line, "api")

        # Commit a contract change and an implementation change
        commit_file(
            api_path,
            "src/dto/UserDTO.java",
            "feat(api): add UserDTO",
            "public class UserDTO { String id; }",
        )
        commit_file(
            api_path,
            "src/service/UserService.java",
            "feat(api): update service",
            "class UserService {}",
        )

        pack = build_review_pack(config, "feat-review", run_verify=True)
        self.assertIn("Dyro 开发线复核审查靶区包", pack)
        self.assertIn("UserDTO.java", pack)
        self.assertIn("UserService.java", pack)
        self.assertIn("✅ PASS", pack)
        self.assertIn("Reviewer-Java", pack)
        self.assertIn("Reviewer-Contract", pack)

    def test_cli_review_pack_and_verify(self) -> None:
        from dyro.cli import main

        config_path = self.root / "dyro.toml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                'verify = [["git", "diff", "--check"]]',
                'verify = [["sh", "-c", "echo verify-ok"]]',
            ),
            encoding="utf-8",
        )
        config = load(self.root)
        create_line(config, line_id="feat-cli", branch="feat/cli", base="main")

        buf = StringIO()
        with redirect_stdout(buf):
            main(["--root", str(self.root), "review", "verify", "--line", "feat-cli"])
        self.assertIn("[SKIP] api: 相对基线无改动", buf.getvalue())
        self.assertIn("通过 0 项", buf.getvalue())

        buf_pack = StringIO()
        with redirect_stdout(buf_pack):
            main(["--root", str(self.root), "review", "pack", "--line", "feat-cli"])
        self.assertIn("Dyro 开发线复核审查靶区包", buf_pack.getvalue())

    def test_external_verify_refuses_before_running_commands(self) -> None:
        config = load(self.root)
        line = create_line(config, line_id="external", branch="feat/external", base="main")
        config = replace(config, policy=replace(config.policy, execution_mode="external"))
        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run), patch("dyro.review_pack.run") as run:
                with self.assertRaisesRegex(DyroError, "当前 Profile 要求外部隔离执行器"):
                    run_line_verify(config, line, dry_run=dry_run, all_repos=True)
                run.assert_not_called()

    def test_external_pack_skips_verify(self) -> None:
        config = load(self.root)
        create_line(config, line_id="external", branch="feat/external", base="main")
        config = replace(config, policy=replace(config.policy, execution_mode="external"))
        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run), patch("dyro.review_pack.run_line_verify") as verify:
                pack = build_review_pack(config, "external", dry_run=dry_run, all_repos=True)
                verify.assert_not_called()
                self.assertIn("SKIP：当前 Profile 要求外部隔离执行器", pack)
                self.assertNotIn("✅ PASS", pack)

    def test_unchanged_repo_skipped_and_all_repos_runs_it(self) -> None:
        from dyro.cli import main

        config = load(self.root)
        line = create_line(config, line_id="unchanged", branch="feat/unchanged", base="main")
        with patch("dyro.review_pack.run") as run:
            results = run_line_verify(config, line)
            run.assert_not_called()
        self.assertEqual(results[0].skipped_reason, "相对基线无改动")
        pack = build_review_pack(config, "unchanged")
        self.assertIn("SKIP：相对基线无改动", pack)
        self.assertNotIn("❌ FAIL", pack)
        for command in ("verify", "pack"):
            buf = StringIO()
            with redirect_stdout(buf):
                main(["--root", str(self.root), "review", command,
                      "--line", "unchanged", "--all-repos"])
            self.assertIn("PASS", buf.getvalue())
            self.assertNotIn("SKIP", buf.getvalue())

    def test_ignored_file_change_still_runs_verify(self) -> None:
        config = load(self.root)
        line = create_line(config, line_id="ignored", branch="feat/ignored", base="main")
        commit_file(line_repository_path(config, line, "api"), "uv.lock", "update lock")
        results = run_line_verify(config, line)
        self.assertTrue(results[0].passed)
        self.assertFalse(results[0].skipped_reason)

    def test_pack_base_override_controls_verify_selection(self) -> None:
        config = load(self.root)
        line = create_line(config, line_id="base", branch="feat/base", base="main")
        commit_file(line_repository_path(config, line, "api"), "api.py", "add api")
        pack = build_review_pack(config, "base", base="HEAD")
        self.assertIn("SKIP：相对基线无改动", pack)

    def test_uncommitted_change_still_runs_verify(self) -> None:
        config = load(self.root)
        line = create_line(config, line_id="dirty", branch="feat/dirty", base="main")
        (line_repository_path(config, line, "api") / "wip.py").write_text("x = 1\n", encoding="utf-8")
        results = run_line_verify(config, line)
        self.assertTrue(results[0].passed)
        self.assertFalse(results[0].skipped_reason)

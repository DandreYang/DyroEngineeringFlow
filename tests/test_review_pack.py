from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

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
        results = run_line_verify(config, line)
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
        self.assertIn("全部门禁通过", buf.getvalue())

        buf_pack = StringIO()
        with redirect_stdout(buf_pack):
            main(["--root", str(self.root), "review", "pack", "--line", "feat-cli"])
        self.assertIn("Dyro 开发线复核审查靶区包", buf_pack.getvalue())

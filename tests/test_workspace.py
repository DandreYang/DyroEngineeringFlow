from pathlib import Path
import unittest
import os
import subprocess

from dyro.config import load
from dyro.errors import DyroError
from dyro.process import Result
from dyro.workspace import (
    create_line,
    doctor,
    is_missing_origin_finding,
    line_repository_path,
    list_lines,
    preflight_line,
    status_rows,
)

from .support import WorkspaceCase, publish_origin_branch, shell


def shell_stdout(*args: str, cwd, check: bool = True) -> str:
    completed = subprocess.run(
        args,
        cwd=cwd,
        check=check,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.stdout.strip()


class WorkspaceTests(WorkspaceCase):
    def test_control_plane_git_observations_do_not_refresh_the_index(self) -> None:
        config = load(self.root)
        tracked = self.anchor / "README.md"
        tracked_stat = tracked.stat()
        os.utime(
            tracked,
            ns=(tracked_stat.st_atime_ns, tracked_stat.st_mtime_ns + 2_000_000_000),
        )
        index = self.anchor / ".git/index"
        before_bytes = index.read_bytes()
        before_mtime = index.stat().st_mtime_ns

        status_rows(config)
        doctor(config)

        self.assertEqual(index.read_bytes(), before_bytes)
        self.assertEqual(index.stat().st_mtime_ns, before_mtime)
        self.assertFalse(index.with_name("index.lock").exists())

    def test_create_line_and_dynamic_doctor(self) -> None:
        publish_origin_branch(self.anchor, "feat/alpha")
        config = load(self.root)
        line = create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        self.assertEqual(line.id, "alpha")
        self.assertTrue((line_repository_path(config, line, "api") / ".git").exists())
        self.assertEqual([item.id for item in list_lines(config)], ["alpha"])
        findings = doctor(config)
        self.assertFalse(any(item.startswith("FAIL") for item in findings), findings)

    def test_create_line_tracks_origin_feat_when_remote_exists(self) -> None:
        publish_origin_branch(self.anchor, "feat/remote-ready")
        config = load(self.root)
        line = create_line(
            config, line_id="remote-ready", branch="feat/remote-ready", base="main"
        )
        worktree = line_repository_path(config, line, "api")
        upstream = shell_stdout(
            "git",
            "rev-parse",
            "--abbrev-ref",
            "--symbolic-full-name",
            "@{upstream}",
            cwd=worktree,
        )
        self.assertEqual(upstream, "origin/feat/remote-ready")
        findings = doctor(config)
        self.assertFalse(any(item.startswith("FAIL") for item in findings), findings)

    def test_local_only_line_creates_but_doctor_and_next_are_not_ready(self) -> None:
        from contextlib import redirect_stdout
        from io import StringIO
        import json

        from dyro.cli import main

        config = load(self.root)
        line = create_line(
            config, line_id="local-only", branch="feat/local-only", base="main"
        )
        worktree = line_repository_path(config, line, "api")
        upstream = shell_stdout(
            "git",
            "rev-parse",
            "--abbrev-ref",
            "--symbolic-full-name",
            "@{upstream}",
            cwd=worktree,
            check=False,
        )
        self.assertIn(upstream, ("", "-"))
        findings = doctor(config)
        self.assertTrue(
            any("missing origin/feat/local-only" in item for item in findings),
            findings,
        )
        output = StringIO()
        with redirect_stdout(output):
            main(["--root", str(self.root), "next"])
        rendered = output.getvalue()
        # doctor still FAILs missing origin; next stays ready when that is
        # the only FAIL and discloses it.
        self.assertIn("missing origin/feat/local-only", rendered)
        self.assertNotIn("还不能开始任务", rendered)
        self.assertIn("工作区已就绪", rendered)
        json_out = StringIO()
        with redirect_stdout(json_out):
            main(["--root", str(self.root), "next", "--format", "json"])
        payload = json.loads(json_out.getvalue())
        self.assertEqual(payload["state"], "ready")
        self.assertTrue(
            any(
                "missing origin/feat/local-only" in item.get("message", "")
                for item in payload.get("findings", [])
            ),
            payload,
        )

    def test_doctor_fails_when_one_repo_missing_origin_feat(self) -> None:
        web = self.root / "repositories/web"
        web.mkdir(parents=True)
        shell("git", "init", "-b", "main", cwd=web)
        shell("git", "config", "user.name", "Test User", cwd=web)
        shell("git", "config", "user.email", "test@example.com", cwd=web)
        (web / "README.md").write_text("web\n", encoding="utf-8")
        shell("git", "add", "README.md", cwd=web)
        shell("git", "commit", "-m", "chore: initial", cwd=web)
        config_path = self.root / "dyro.toml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8")
            + '\n[repositories.web]\npath = "repositories/web"\nmount = "clients/web"\n',
            encoding="utf-8",
        )
        publish_origin_branch(self.anchor, "feat/partial-remote")
        config = load(self.root)
        create_line(
            config,
            line_id="partial-remote",
            branch="feat/partial-remote",
            base="main",
        )
        findings = doctor(config)
        self.assertTrue(
            any(
                item.startswith("FAIL")
                and "web" in item
                and "missing origin/feat/partial-remote" in item
                for item in findings
            ),
            findings,
        )

    def test_doctor_fails_when_named_child_sits_on_parent(self) -> None:
        publish_origin_branch(self.anchor, "main")
        shell("git", "checkout", "-b", "feat/child", cwd=self.anchor)
        (self.anchor / "child.txt").write_text("child\n", encoding="utf-8")
        shell("git", "add", "child.txt", cwd=self.anchor)
        shell("git", "commit", "-m", "feat: child", cwd=self.anchor)
        publish_origin_branch(self.anchor, "feat/child")
        shell("git", "checkout", "main", cwd=self.anchor)
        shell("git", "branch", "-D", "feat/child", cwd=self.anchor)
        config = load(self.root)
        line = create_line(config, line_id="child", branch="feat/child", base="main")
        worktree = line_repository_path(config, line, "api")
        shell("git", "reset", "--hard", "main", cwd=worktree)
        shell("git", "branch", "--set-upstream-to=origin/main", cwd=worktree)
        findings = doctor(config)
        self.assertTrue(
            any(
                item.startswith("FAIL")
                and "child" in item
                and "expected upstream origin/feat/child" in item
                for item in findings
            ),
            findings,
        )

    def test_doctor_fails_when_published_child_still_tracks_parent(self) -> None:
        from contextlib import redirect_stdout
        from io import StringIO
        import json

        from dyro.cli import main

        publish_origin_branch(self.anchor, "main")
        publish_origin_branch(self.anchor, "feat/same-sha-child")
        config = load(self.root)
        line = create_line(
            config, line_id="same-sha-child", branch="feat/same-sha-child", base="main"
        )
        worktree = line_repository_path(config, line, "api")
        shell("git", "branch", "--set-upstream-to=origin/main", cwd=worktree)
        head = shell_stdout("git", "rev-parse", "HEAD", cwd=worktree)
        remote_feat = shell_stdout(
            "git", "rev-parse", "origin/feat/same-sha-child", cwd=worktree
        )
        parent = shell_stdout("git", "rev-parse", "origin/main", cwd=worktree)
        self.assertEqual(head, remote_feat)
        self.assertEqual(head, parent)
        findings = doctor(config)
        self.assertTrue(
            any(
                item.startswith("FAIL")
                and "same-sha-child" in item
                and "expected upstream origin/feat/same-sha-child" in item
                for item in findings
            ),
            findings,
        )
        output = StringIO()
        with redirect_stdout(output):
            main(["--root", str(self.root), "next"])
        rendered = output.getvalue()
        self.assertIn("还不能开始任务", rendered)
        self.assertNotIn("工作区已就绪", rendered)
        json_out = StringIO()
        with redirect_stdout(json_out):
            main(["--root", str(self.root), "next", "--format", "json"])
        payload = json.loads(json_out.getvalue())
        self.assertEqual(payload["state"], "needs_repair")

    def test_plan_rejects_local_branch_tracking_parent_feat(self) -> None:
        shell("git", "checkout", "-b", "feat/from-parent", cwd=self.anchor)
        shell("git", "branch", "--set-upstream-to=main", cwd=self.anchor)
        shell("git", "checkout", "main", cwd=self.anchor)
        config = load(self.root)
        with self.assertRaisesRegex(DyroError, "上游"):
            create_line(
                config, line_id="from-parent", branch="feat/from-parent", base="main"
            )

    def test_create_line_preflight_rejects_bad_base_without_worktrees(self) -> None:
        web = self.root / "repositories/web"
        web.mkdir(parents=True)
        shell("git", "init", "-b", "main", cwd=web)
        shell("git", "config", "user.name", "Test User", cwd=web)
        shell("git", "config", "user.email", "test@example.com", cwd=web)
        (web / "README.md").write_text("web\n", encoding="utf-8")
        shell("git", "add", "README.md", cwd=web)
        shell("git", "commit", "-m", "chore: initial", cwd=web)
        config_path = self.root / "dyro.toml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8")
            + '\n[repositories.web]\npath = "repositories/web"\nmount = "clients/web"\n',
            encoding="utf-8",
        )
        config = load(self.root)

        with self.assertRaisesRegex(DyroError, "校验 web 基线"):
            create_line(
                config,
                line_id="partial-preflight",
                branch="feat/partial-preflight",
                base="main",
                repository_bases={"web": "missing-ref"},
            )
        self.assertFalse((self.root / "versions/partial-preflight").exists())
        self.assertEqual(list_lines(config), [])

    def test_public_preflight_detects_a_dirty_anchor_without_mutating(self) -> None:
        config = load(self.root)
        self.anchor.joinpath("dirty.txt").write_text("pending\n", encoding="utf-8")

        with self.assertRaisesRegex(DyroError, "仓库不干净"):
            preflight_line(
                config,
                line_id="dirty-anchor",
                branch="feat/dirty-anchor",
                base="main",
            )

        self.assertFalse((self.root / "versions/dirty-anchor").exists())
        self.assertEqual(list_lines(config), [])

    def test_create_line_rolls_back_when_a_later_repository_fails(self) -> None:
        web = self.root / "repositories/web"
        web.mkdir(parents=True)
        shell("git", "init", "-b", "main", cwd=web)
        shell("git", "config", "user.name", "Test User", cwd=web)
        shell("git", "config", "user.email", "test@example.com", cwd=web)
        (web / "README.md").write_text("web\n", encoding="utf-8")
        shell("git", "add", "README.md", cwd=web)
        shell("git", "commit", "-m", "chore: initial", cwd=web)
        config_path = self.root / "dyro.toml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8")
            + '\n[repositories.web]\npath = "repositories/web"\nmount = "clients/web"\n',
            encoding="utf-8",
        )
        config = load(self.root)

        from dyro import workspace as workspace_mod

        original_git = workspace_mod.git

        def flaky_git(repo: Path, *args: str, dry_run: bool = False, timeout: int = 180):
            if not dry_run and args[:2] == ("worktree", "add"):
                if any("clients/web" in str(item) for item in args):
                    return Result(("git", "-C", str(repo), *args), 1, "simulated worktree failure")
            return original_git(repo, *args, dry_run=dry_run, timeout=timeout)

        workspace_mod.git = flaky_git  # type: ignore[assignment]
        try:
            with self.assertRaisesRegex(DyroError, "创建 web worktree"):
                create_line(config, line_id="partial", branch="feat/partial", base="main")
        finally:
            workspace_mod.git = original_git  # type: ignore[assignment]

        self.assertEqual(list_lines(load(self.root)), [])
        self.assertFalse((self.root / "versions/partial").exists())
        self.assertFalse((self.root / "versions/partial/services/api").exists())
        self.assertFalse((self.root / "versions/partial/clients/web").exists())

    def test_line_persists_a_base_per_repository(self) -> None:
        web = self.root / "repositories/web"
        web.mkdir(parents=True)
        shell("git", "init", "-b", "main", cwd=web)
        shell("git", "config", "user.name", "Test User", cwd=web)
        shell("git", "config", "user.email", "test@example.com", cwd=web)
        (web / "README.md").write_text("web\n", encoding="utf-8")
        shell("git", "add", "README.md", cwd=web)
        shell("git", "commit", "-m", "chore: initial", cwd=web)
        shell("git", "branch", "release", cwd=web)
        config_path = self.root / "dyro.toml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8")
            + '''\n[repositories.web]\npath = "repositories/web"\nmount = "clients/web"\n''',
            encoding="utf-8",
        )

        from dyro.config import load

        line = create_line(
            load(self.root),
            line_id="mixed-baselines",
            branch="feat/mixed-baselines",
            base="main",
            repository_bases={"api": "main", "web": "release"},
        )

        self.assertEqual(line.base_for("api"), "main")
        self.assertEqual(line.base_for("web"), "release")
        persisted = list_lines(load(self.root))[0]
        self.assertEqual(persisted.base_for("web"), "release")

    def test_anchor_reference_storage_is_explicit_and_doctor_validates_it(self) -> None:
        shell("git", "checkout", "-b", "feat/reuse-anchor", cwd=self.anchor)
        config = load(self.root)

        line = create_line(
            config,
            line_id="reuse-anchor",
            branch="feat/reuse-anchor",
            base="main",
            storage_modes={"api": "anchor-reference"},
        )

        path = line_repository_path(config, line, "api")
        self.assertTrue(path.is_symlink())
        self.assertEqual(line.storage_for("api"), "anchor-reference")
        findings = doctor(config)
        self.assertFalse(any(item.startswith("FAIL") for item in findings), findings)

        shell("git", "checkout", "main", cwd=self.anchor)
        findings = doctor(config)
        self.assertTrue(any("expected feat/reuse-anchor" in item for item in findings), findings)

class MissingOriginFindingTests(unittest.TestCase):
    def test_recognizes_only_missing_origin_doctor_fails(self) -> None:
        self.assertTrue(
            is_missing_origin_finding("FAIL line:alpha/api: missing origin/feat/alpha")
        )
        self.assertTrue(
            is_missing_origin_finding(
                "FAIL hotfix:cut/api: missing origin/hotfix/cut"
            )
        )
        self.assertFalse(
            is_missing_origin_finding(
                "FAIL line:alpha/api: expected upstream origin/feat/alpha, found origin/main"
            )
        )
        self.assertFalse(
            is_missing_origin_finding("FAIL line:alpha/api: missing worktree")
        )
        self.assertFalse(
            is_missing_origin_finding(
                "FAIL repository api: missing or not Git: /tmp/api"
            )
        )
        self.assertFalse(
            is_missing_origin_finding(
                "PASS line:alpha/api: missing origin/feat/alpha"
            )
        )

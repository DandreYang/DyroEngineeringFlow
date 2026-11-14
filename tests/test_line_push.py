"""dyro line push publishes a line branch and records linked-worktree anchors."""
from __future__ import annotations

import json
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
import subprocess
from unittest.mock import patch

from dyro import line_ops, workspace
from dyro.cli import main
from dyro.config import load
from dyro.errors import DyroError
from dyro.events import read_events_fail_closed
from dyro.process import Result
from dyro.workspace import (
    create_line,
    get_line,
    line_repository_path,
    push_line,
    spawn_line,
)

from .support import WorkspaceCase, publish_origin_branch, shell


def _stdout(*args: str, cwd: Path) -> str:
    completed = subprocess.run(
        args,
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.stdout.strip()


def _commit_text(worktree: Path, filename: str, content: str, message: str) -> str:
    (worktree / filename).write_text(content, encoding="utf-8")
    shell("git", "add", filename, cwd=worktree)
    shell("git", "commit", "-m", message, cwd=worktree)
    return _stdout("git", "rev-parse", "HEAD", cwd=worktree)


def _ledger(config) -> list[dict]:
    if not config.ledger_file.exists():
        return []
    return [
        json.loads(row)
        for row in config.ledger_file.read_text(encoding="utf-8").splitlines()
        if row.strip()
    ]


class LinePushTests(WorkspaceCase):
    def _enable_web(self, repo_id="web"):
        web = self.root / f"repositories/{repo_id}"
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
            + f'\n[repositories.{repo_id}]\npath = "repositories/{repo_id}"\nmount = "clients/{repo_id}"\n',
            encoding="utf-8",
        )
        return load(self.root), web

    def _remote(self, worktree: Path, branch: str) -> str:
        return _stdout("git", "rev-parse", f"refs/remotes/origin/{branch}", cwd=worktree)

    def _bare(self, anchor: Path, branch: str) -> str:
        bare = anchor.parent / f"{anchor.name}.origin.git"
        return _stdout("git", "rev-parse", f"refs/heads/{branch}", cwd=bare)

    def _two_linked(self, *, allow_push: bool = False, commit: bool = True):
        config, _web = self._enable_web()
        publish_origin_branch(self.anchor, "feat/alpha")
        publish_origin_branch(self.root / "repositories/web", "feat/alpha")
        line = create_line(
            config,
            line_id="alpha",
            branch="feat/alpha",
            base="main",
            repositories=("api", "web"),
        )
        config = load(self.root)
        paths = {
            repo_id: line_repository_path(config, line, repo_id)
            for repo_id in line.repositories
        }
        if commit:
            for repo_id, path in paths.items():
                _commit_text(path, f"{repo_id}.txt", f"{repo_id}\n", f"feat: {repo_id}")
        if allow_push:
            config = replace(config, policy=replace(config.policy, allow_push=True))
        line = get_line(config, line.id)
        return config, line, paths

    def _ops(self, config) -> list[Path]:
        directory = config.root / ".dyro" / "ops"
        if not directory.exists():
            return []
        return sorted(directory.glob("*.json"))

    def test_two_linked_repos_are_pushed_and_anchored(self) -> None:
        config, line, paths = self._two_linked(allow_push=True)
        for path in paths.values():
            shell("git", "branch", "--unset-upstream", cwd=path)
        events = read_events_fail_closed(config)

        updated, pushed, skipped = push_line(config, line.id)

        self.assertEqual(pushed, ["api", "web"])
        self.assertEqual(skipped, [])
        heads = {}
        for repo_id, path in paths.items():
            head = _stdout("git", "rev-parse", "HEAD", cwd=path)
            heads[repo_id] = head
            self.assertEqual(
                _stdout("git", "rev-parse", "--abbrev-ref", "@{u}", cwd=path),
                f"origin/{line.branch}",
            )
            self.assertEqual(self._remote(path, line.branch), head)
            anchor = self.anchor if repo_id == "api" else self.root / "repositories/web"
            self.assertEqual(self._bare(anchor, line.branch), head)
        self.assertEqual(dict(updated.remote_anchors), heads)
        reloaded = get_line(config, line.id)
        self.assertEqual(dict(reloaded.remote_anchors), heads)
        manifest = (config.lines_state_dir / "alpha.toml").read_text(encoding="utf-8")
        self.assertIn("schema_version = 4", manifest)
        rows = [row for row in _ledger(config) if row["phase"] == "line_push"]
        self.assertEqual([row["repository"] for row in rows], ["api", "web"])
        for row in rows:
            self.assertEqual(row["task_id"], line.id)
            self.assertEqual(row["branch"], line.branch)
            self.assertEqual(row["head"], heads[row["repository"]])
        self.assertEqual(read_events_fail_closed(config), events)
        self.assertEqual(self._ops(config), [])

    def test_allow_push_false_leaves_remote_refs(self) -> None:
        config, line, paths = self._two_linked(allow_push=False)
        before = {repo_id: self._remote(path, line.branch) for repo_id, path in paths.items()}

        with self.assertRaises(DyroError) as raised:
            push_line(config, line.id)

        self.assertIn("禁止 push", str(raised.exception))
        for repo_id, path in paths.items():
            self.assertEqual(self._remote(path, line.branch), before[repo_id])
            self.assertNotEqual(before[repo_id], _stdout("git", "rev-parse", "HEAD", cwd=path))
        self.assertEqual(get_line(config, line.id).remote_anchors, {})
        self.assertEqual(_ledger(config), [])

    def test_anchor_reference_repo_is_skipped(self) -> None:
        config, web = self._enable_web()
        publish_origin_branch(self.anchor, "feat/alpha")
        shell("git", "checkout", "-b", "feat/alpha", cwd=web)
        line = create_line(
            config,
            line_id="alpha",
            branch="feat/alpha",
            base="main",
            repositories=("api", "web"),
            storage_modes={"web": "anchor-reference"},
        )
        config = replace(load(self.root), policy=replace(load(self.root).policy, allow_push=True))
        api = line_repository_path(config, line, "api")
        shell("git", "branch", "--unset-upstream", cwd=api)
        head = _commit_text(api, "api.txt", "api\n", "feat: api")

        updated, pushed, skipped = push_line(config, line.id)

        self.assertEqual(pushed, ["api"])
        self.assertEqual(skipped, ["web"])
        self.assertEqual(updated.remote_anchors, {"api": head})
        self.assertNotIn("web", get_line(config, line.id).remote_anchors)
        self.assertEqual(self._remote(api, line.branch), head)
        self.assertEqual(
            _stdout("git", "rev-parse", "--abbrev-ref", "@{u}", cwd=api),
            f"origin/{line.branch}",
        )
        web_remotes = _stdout("git", "remote", cwd=web)
        self.assertNotIn("origin", web_remotes.split())

    def test_preflight_failure_pushes_nothing(self) -> None:
        config, line, paths = self._two_linked(allow_push=True)
        before = {repo_id: self._remote(path, line.branch) for repo_id, path in paths.items()}
        shell(
            "git",
            "remote",
            "set-url",
            "origin",
            str(self.root / "missing-web.git"),
            cwd=paths["web"],
        )

        with self.assertRaises(DyroError) as raised:
            push_line(config, line.id)

        self.assertIn("预检推送 web", str(raised.exception))
        for repo_id, path in paths.items():
            self.assertEqual(self._remote(path, line.branch), before[repo_id])
        self.assertEqual(get_line(config, line.id).remote_anchors, {})
        manifest = (config.lines_state_dir / "alpha.toml").read_text(encoding="utf-8")
        self.assertIn("schema_version = 2", manifest)
        self.assertEqual(_ledger(config), [])

    def test_second_repo_push_failure_keeps_first_anchor(self) -> None:
        config, line, paths = self._two_linked(allow_push=True)
        web_before = self._remote(paths["web"], line.branch)
        api_head = _stdout("git", "rev-parse", "HEAD", cwd=paths["api"])
        real_git = workspace.git

        def fake_git(repo, *args, **kwargs):
            if Path(repo) == paths["web"] and args == ("push", "-u", "origin", f"refs/heads/{line.branch}:refs/heads/{line.branch}"):
                return Result(("git", "-C", str(repo), *args), 1, "rejected")
            return real_git(repo, *args, **kwargs)

        with patch.object(workspace, "git", side_effect=fake_git):
            with self.assertRaises(DyroError) as raised:
                push_line(config, line.id)

        message = str(raised.exception)
        self.assertIn("web", message)
        self.assertIn("api", message)
        self.assertIn("rejected", message)
        self.assertNotIn("本地合并已保留", message)
        reloaded = get_line(config, line.id)
        self.assertEqual(reloaded.remote_anchors, {"api": api_head})
        self.assertEqual(self._remote(paths["api"], line.branch), api_head)
        self.assertEqual(self._remote(paths["web"], line.branch), web_before)
        rows = _ledger(config)
        self.assertEqual([row["phase"] for row in rows], ["line_push", "line_push_failed"])
        self.assertEqual(rows[0]["repository"], "api")
        self.assertEqual(rows[0]["head"], api_head)
        self.assertEqual(rows[1]["repository"], "web")
        self.assertEqual(rows[1]["pushed_repositories"], ["api"])
        self.assertEqual(rows[1]["error"], "rejected")

    def test_dry_run_does_not_push_or_write(self) -> None:
        config, line, paths = self._two_linked(allow_push=True)
        before = {repo_id: self._remote(path, line.branch) for repo_id, path in paths.items()}
        events = read_events_fail_closed(config)

        updated, pushed, skipped = push_line(config, line.id, dry_run=True)

        self.assertEqual(pushed, [])
        self.assertEqual(skipped, [])
        self.assertEqual(updated.remote_anchors, {})
        self.assertEqual(get_line(config, line.id).remote_anchors, {})
        for repo_id, path in paths.items():
            self.assertEqual(self._remote(path, line.branch), before[repo_id])
            self.assertNotEqual(before[repo_id], _stdout("git", "rev-parse", "HEAD", cwd=path))
        self.assertEqual(_ledger(config), [])
        self.assertEqual(read_events_fail_closed(config), events)
        self.assertEqual(self._ops(config), [])

    def test_dry_run_runs_preflight_for_every_repo(self) -> None:
        config, line, paths = self._two_linked(allow_push=True)
        shell("git", "remote", "set-url", "origin", str(self.root / "missing.git"), cwd=paths["web"])

        with self.assertRaises(DyroError) as raised:
            push_line(config, line.id, dry_run=True)

        self.assertIn("预检推送 web", str(raised.exception))

    def test_dirty_worktree_still_pushes_committed_head(self) -> None:
        config, line, paths = self._two_linked(allow_push=True)
        heads = {r: _stdout("git", "rev-parse", "HEAD", cwd=p) for r, p in paths.items()}
        (paths["api"] / "untracked.txt").write_text("wip", encoding="utf-8")
        (paths["web"] / "web.txt").write_text("edited", encoding="utf-8")

        updated, pushed, _ = push_line(config, line.id)

        self.assertEqual(pushed, ["api", "web"])
        self.assertEqual(dict(updated.remote_anchors), heads)

    def test_anchor_is_pushed_sha_even_if_worktree_moves(self) -> None:
        config, line, paths = self._two_linked(allow_push=True)
        pushed_head = _stdout("git", "rev-parse", "HEAD", cwd=paths["api"])
        real_git = workspace.git

        def move_after_push(repo, *args, **kwargs):
            result = real_git(repo, *args, **kwargs)
            if args == ("push", "-u", "origin", f"refs/heads/{line.branch}:refs/heads/{line.branch}") and Path(repo) == paths["api"]:
                shell("git", "checkout", "-q", "-b", "elsewhere", cwd=paths["api"])
                _commit_text(paths["api"], "moved.txt", "moved", "moved")
            return result

        with patch.object(workspace, "git", side_effect=move_after_push):
            updated, _, _ = push_line(config, line.id)

        self.assertNotEqual(_stdout("git", "rev-parse", "HEAD", cwd=paths["api"]), pushed_head)
        self.assertEqual(updated.remote_anchors["api"], pushed_head)
        self.assertEqual(self._bare(self.root / "repositories/api", line.branch), pushed_head)

    def test_branch_moving_during_push_refuses_anchor(self) -> None:
        config, line, paths = self._two_linked(allow_push=True)
        real_git = workspace.git

        def commit_after_push(repo, *args, **kwargs):
            result = real_git(repo, *args, **kwargs)
            if args == ("push", "-u", "origin", f"refs/heads/{line.branch}:refs/heads/{line.branch}") and Path(repo) == paths["api"]:
                _commit_text(paths["api"], "late.txt", "late", "late")
            return result

        with patch.object(workspace, "git", side_effect=commit_after_push):
            with self.assertRaises(DyroError) as raised:
                push_line(config, line.id)

        self.assertIn("推送期间", str(raised.exception))
        self.assertIn("已推送仓库：api", str(raised.exception))
        self.assertEqual(get_line(config, line.id).remote_anchors, {})

    def test_narrow_fetch_refspec_still_anchors_pushed_tip(self) -> None:
        config, line, paths = self._two_linked(allow_push=True)
        api = paths["api"]
        stale = _stdout("git", "rev-parse", f"refs/remotes/origin/{line.branch}", cwd=api)
        shell("git", "config", "remote.origin.fetch", "+refs/heads/main:refs/remotes/origin/main", cwd=api)
        head = _stdout("git", "rev-parse", "HEAD", cwd=api)
        self.assertNotEqual(stale, head)

        updated, _, _ = push_line(config, line.id)

        self.assertEqual(updated.remote_anchors["api"], head)
        self.assertEqual(self._bare(self.root / "repositories/api", line.branch), head)

    def test_push_default_upstream_cannot_redirect_destination(self) -> None:
        config, line, paths = self._two_linked(allow_push=True)
        api = paths["api"]
        bare = self.root / "repositories/api"
        publish_origin_branch(bare, "main")
        main_before = self._bare(bare, "main")
        shell("git", "config", "push.default", "upstream", cwd=api)
        shell("git", "branch", "--set-upstream-to=origin/main", line.branch, cwd=api)
        head = _stdout("git", "rev-parse", "HEAD", cwd=api)

        updated, _, _ = push_line(config, line.id)

        self.assertEqual(self._bare(bare, "main"), main_before)
        self.assertEqual(self._bare(bare, line.branch), head)
        self.assertEqual(updated.remote_anchors["api"], head)
        self.assertEqual(
            _stdout("git", "rev-parse", "--abbrev-ref", "@{u}", cwd=api), f"origin/{line.branch}"
        )

    def test_unfinished_merge_op_refuses_before_push(self) -> None:
        config, line, paths = self._two_linked(allow_push=True)
        before = {repo_id: self._remote(path, line.branch) for repo_id, path in paths.items()}
        op = line_ops.create_op(
            config,
            kind="merge",
            subject_line_ids=(line.id,),
            manifest_snapshot={"target": line.id},
            repos_ordered=line.repositories,
            effective_options={"integration": "into"},
        )

        with self.assertRaises(DyroError) as raised:
            push_line(config, line.id)

        self.assertIn("未完成操作", str(raised.exception))
        self.assertIn(op["op_id"], str(raised.exception))
        for repo_id, path in paths.items():
            self.assertEqual(self._remote(path, line.branch), before[repo_id])
        self.assertEqual(get_line(config, line.id).remote_anchors, {})
        self.assertEqual(_ledger(config), [])

    def test_unfinished_retire_op_refuses_before_push(self) -> None:
        config, line, paths = self._two_linked(allow_push=True)
        op = line_ops.create_op(
            config,
            kind="retire",
            subject_line_ids=(line.id,),
            manifest_snapshot={"line": line.id},
            repos_ordered=line.repositories,
            effective_options={},
        )

        with self.assertRaises(DyroError) as raised:
            push_line(config, line.id)

        self.assertIn(op["op_id"], str(raised.exception))
        self.assertEqual(get_line(config, line.id).remote_anchors, {})
        self.assertEqual(_ledger(config), [])

    def test_wrong_branch_refuses_before_push(self) -> None:
        config, line, paths = self._two_linked(allow_push=True)
        before = {repo_id: self._remote(path, line.branch) for repo_id, path in paths.items()}
        shell("git", "checkout", "-b", "wrong", cwd=paths["web"])

        with self.assertRaises(DyroError) as raised:
            push_line(config, line.id)

        self.assertIn("仓库分支错误", str(raised.exception))
        self.assertIn(line.branch, str(raised.exception))
        for repo_id, path in paths.items():
            self.assertEqual(self._remote(path, line.branch), before[repo_id])
        self.assertEqual(get_line(config, line.id).remote_anchors, {})
        self.assertEqual(_ledger(config), [])

    def test_anchor_record_failure_reports_push_already_happened(self) -> None:
        config, line, paths = self._two_linked(allow_push=True)
        api_head = _stdout("git", "rev-parse", "HEAD", cwd=paths["api"])
        web_head = _stdout("git", "rev-parse", "HEAD", cwd=paths["web"])
        real_record = workspace.record_remote_anchor

        def boom(config, line, repo_id, path, **kwargs):
            if repo_id == "web":
                raise RuntimeError("disk full")
            return real_record(config, line, repo_id, path, **kwargs)

        with patch.object(workspace, "record_remote_anchor", side_effect=boom):
            with self.assertRaises(DyroError) as raised:
                push_line(config, line.id)

        self.assertIn("已发生", str(raised.exception))
        self.assertIn("web", str(raised.exception))
        self.assertIn("远端锚点或 ledger 未记录完整", str(raised.exception))
        self.assertIn("已推送仓库：api, web", str(raised.exception))
        self.assertIsInstance(raised.exception.__cause__, RuntimeError)
        reloaded = get_line(config, line.id)
        self.assertEqual(reloaded.remote_anchors, {"api": api_head})
        self.assertEqual(self._remote(paths["web"], line.branch), web_head)
        self.assertEqual(self._bare(self.root / "repositories/web", line.branch), web_head)
        rows = [row for row in _ledger(config) if row["phase"] == "line_push"]
        self.assertEqual([row["repository"] for row in rows], ["api"])

    def test_cli_requires_yes(self) -> None:
        _, line, paths = self._two_linked(allow_push=False)
        before = {repo_id: self._remote(path, line.branch) for repo_id, path in paths.items()}
        toml = self.root / "dyro.toml"
        toml.write_text(
            toml.read_text(encoding="utf-8").replace(
                "allow_push = false", "allow_push = true", 1
            ),
            encoding="utf-8",
        )
        stderr = StringIO()
        with redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                main(["--root", str(self.root), "line", "push", line.id])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--yes", stderr.getvalue())
        for repo_id, path in paths.items():
            self.assertEqual(self._remote(path, line.branch), before[repo_id])
        self.assertEqual(get_line(load(self.root), line.id).remote_anchors, {})

        for path in paths.values():
            shell("git", "branch", "--unset-upstream", cwd=path)
        stdout = StringIO()
        with redirect_stdout(stdout):
            main(["--root", str(self.root), "line", "push", line.id, "--yes"])
        text = stdout.getvalue()
        reloaded = get_line(load(self.root), line.id)
        for repo_id, path in paths.items():
            head = _stdout("git", "rev-parse", "HEAD", cwd=path)
            self.assertIn(
                f"已推送 line {line.id} 仓库 {repo_id}：{head[:12]}（{line.branch}）",
                text,
            )
            self.assertEqual(
                _stdout("git", "rev-parse", "--abbrev-ref", "@{u}", cwd=path),
                f"origin/{line.branch}",
            )
            self.assertEqual(self._remote(path, line.branch), head)
            self.assertEqual(reloaded.remote_anchors[repo_id], head)

    def test_child_line_can_be_pushed(self) -> None:
        publish_origin_branch(self.anchor, "feat/alpha")
        config = load(self.root)
        parent = create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        child = spawn_line(config, parent.id, "child")
        self.assertEqual(child.id, "alpha_child")
        self.assertEqual(child.branch, "feat/alpha_child")
        path = line_repository_path(config, child, "api")
        head = _commit_text(path, "child.txt", "child\n", "feat: child")
        config = replace(load(self.root), policy=replace(load(self.root).policy, allow_push=True))
        events = read_events_fail_closed(config)

        updated, pushed, skipped = push_line(config, child.id)

        self.assertEqual(pushed, ["api"])
        self.assertEqual(skipped, [])
        self.assertEqual(updated.remote_anchors, {"api": head})
        self.assertEqual(
            _stdout("git", "rev-parse", "--abbrev-ref", "@{u}", cwd=path),
            "origin/feat/alpha_child",
        )
        self.assertEqual(self._remote(path, child.branch), head)
        self.assertEqual(self._bare(self.anchor, child.branch), head)
        manifest = (config.lines_state_dir / "alpha_child.toml").read_text(encoding="utf-8")
        self.assertIn("schema_version = 4", manifest)
        parent_manifest = (config.lines_state_dir / "alpha.toml").read_text(encoding="utf-8")
        self.assertIn("schema_version = 2", parent_manifest)
        self.assertEqual(get_line(config, parent.id).remote_anchors, {})
        self.assertEqual([event["kind"] for event in read_events_fail_closed(config)], ["spawn"])
        self.assertEqual(read_events_fail_closed(config), events)
        rows = [row for row in _ledger(config) if row["phase"] == "line_push"]
        self.assertEqual(rows[0]["task_id"], child.id)
        self.assertEqual(rows[0]["head"], head)
        self.assertEqual(self._ops(config), [])

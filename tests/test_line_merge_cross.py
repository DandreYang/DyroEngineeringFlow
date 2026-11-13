"""Cross-line merge, merge-into-base, and report-only --recover (§3.5)."""
from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
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
    line_repository_path,
    merge_line_from,
    merge_line_into_base,
    recover_line_merge,
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


class CrossLineMergeTests(WorkspaceCase):
    def _enable_web(self):
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
        return load(self.root), web

    def _sibling_lines(self):
        config, _web = self._enable_web()
        publish_origin_branch(self.anchor, "feat/alpha")
        publish_origin_branch(self.root / "repositories/web", "feat/alpha")
        alpha = create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        beta = create_line(config, line_id="beta", branch="feat/beta", base="main")
        return load(self.root), alpha, beta

    def test_from_merge_writes_ledger_event_and_keeps_parent(self) -> None:
        config, alpha, beta = self._sibling_lines()
        source_head = _commit_text(
            line_repository_path(config, beta, "api"),
            "from-beta.txt",
            "beta\n",
            "feat: beta work",
        )
        result = merge_line_from(config, alpha.id, beta.id)
        self.assertFalse(result.partial)
        self.assertEqual(result.outcomes["api"], "committed")
        self.assertEqual(workspace.get_line(config, beta.id).parent, "")
        self.assertEqual(workspace.get_line(config, alpha.id).parent, "")
        contained = subprocess.run(
            ["git", "merge-base", "--is-ancestor", source_head, "HEAD"],
            cwd=line_repository_path(config, alpha, "api"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(contained.returncode, 0)
        ledger = config.ledger_file.read_text(encoding="utf-8")
        self.assertIn('"phase": "line_merge"', ledger)
        self.assertIn(f'"op_id": "{result.op_id}"', ledger)
        events = read_events_fail_closed(config)
        self.assertTrue(any(event.get("kind") == "merge_cross" for event in events))
        self.assertTrue(
            any(
                isinstance(event.get("facts"), dict)
                and event["facts"].get("op_id") == result.op_id
                for event in events
            )
        )

    def test_merge_into_base_direction_and_refuses_wrong_branch(self) -> None:
        config = load(self.root)
        publish_origin_branch(self.anchor, "feat/ship")
        line = create_line(config, line_id="ship", branch="feat/ship", base="main")
        source_head = _commit_text(
            line_repository_path(config, line, "api"),
            "ship.txt",
            "ship\n",
            "feat: ship work",
        )
        shell("git", "checkout", "-B", "develop", cwd=self.anchor)
        with self.assertRaisesRegex(DyroError, "不会自动 checkout"):
            merge_line_into_base(config, line.id)
        shell("git", "checkout", "main", cwd=self.anchor)
        result = merge_line_into_base(config, line.id)
        self.assertEqual(result.source_id, line.id)
        self.assertEqual(result.target_id, "base")
        contained = subprocess.run(
            ["git", "merge-base", "--is-ancestor", source_head, "HEAD"],
            cwd=self.anchor,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(contained.returncode, 0)
        self.assertEqual(_stdout("git", "branch", "--show-current", cwd=self.anchor), "main")

    def test_partial_superset_and_allow_partial_exit(self) -> None:
        config, _web = self._enable_web()
        publish_origin_branch(self.anchor, "feat/alpha")
        publish_origin_branch(self.root / "repositories/web", "feat/alpha")
        alpha = create_line(
            config, line_id="alpha", branch="feat/alpha", base="main", repositories=["api"]
        )
        beta = create_line(config, line_id="beta", branch="feat/beta", base="main")
        _commit_text(
            line_repository_path(config, beta, "api"),
            "only-api.txt",
            "x\n",
            "feat: api",
        )
        result = merge_line_from(config, alpha.id, beta.id, allow_partial=True)
        self.assertTrue(result.partial)
        self.assertEqual(result.excluded_repositories, ("web",))
        self.assertIn("api", result.repositories)

        gamma = create_line(config, line_id="gamma", branch="feat/gamma", base="main")
        _commit_text(
            line_repository_path(config, gamma, "api"),
            "g.txt",
            "g\n",
            "feat: g",
        )
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            with self.assertRaises(SystemExit) as caught:
                main(
                    [
                        "--root",
                        str(self.root),
                        "line",
                        "merge",
                        alpha.id,
                        "--from",
                        gamma.id,
                        "--yes",
                    ]
                )
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("partial", buffer.getvalue())

        with self.assertRaisesRegex(DyroError, "没有可合入的仓库交集"):
            other = create_line(
                config,
                line_id="webonly",
                branch="feat/webonly",
                base="main",
                repositories=["web"],
            )
            merge_line_from(config, alpha.id, other.id)

    def test_already_included_still_ledgered_with_committed_sibling(self) -> None:
        config, _web = self._enable_web()
        publish_origin_branch(self.anchor, "feat/alpha")
        publish_origin_branch(self.root / "repositories/web", "feat/alpha")
        alpha = create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        beta = create_line(config, line_id="beta", branch="feat/beta", base="main")
        # Make api already included: commit on beta, merge once into alpha, then
        # reset beta tip equivalence by merging beta→alpha so api matches; add web-only change.
        api_head = _commit_text(
            line_repository_path(config, beta, "api"),
            "shared.txt",
            "shared\n",
            "feat: shared",
        )
        # Put the same commit into alpha/api by merging once.
        merge_line_from(config, alpha.id, beta.id)
        web_head = _commit_text(
            line_repository_path(config, beta, "web"),
            "web-only.txt",
            "web\n",
            "feat: web only",
        )
        result = merge_line_from(config, alpha.id, beta.id)
        self.assertEqual(result.outcomes["api"], "already_included")
        self.assertEqual(result.outcomes["web"], "committed")
        op = line_ops.load_op(config, result.op_id)
        self.assertEqual(op["repositories"]["api"]["outcome"], "already_included")
        ledger_rows = [
            json.loads(line)
            for line in config.ledger_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        op_rows = [row for row in ledger_rows if row.get("op_id") == result.op_id]
        repos = {row["repository"] for row in op_rows}
        self.assertEqual(repos, {"api", "web"})
        self.assertTrue(
            any(row.get("outcome") == "already_included" for row in op_rows)
        )
        contained = subprocess.run(
            ["git", "merge-base", "--is-ancestor", web_head, "HEAD"],
            cwd=line_repository_path(config, alpha, "web"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(contained.returncode, 0)
        self.assertTrue(
            workspace._is_ancestor(
                line_repository_path(config, alpha, "api"), api_head
            )
        )

    def test_rollback_persisted_recover_does_not_auto_commit(self) -> None:
        config, _web = self._enable_web()
        publish_origin_branch(self.anchor, "feat/alpha")
        publish_origin_branch(self.root / "repositories/web", "feat/alpha")
        alpha = create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        beta = create_line(config, line_id="beta", branch="feat/beta", base="main")
        _commit_text(
            line_repository_path(config, beta, "api"),
            "a.txt",
            "a\n",
            "feat: a",
        )
        _commit_text(
            line_repository_path(config, beta, "web"),
            "w.txt",
            "w\n",
            "feat: w",
        )
        alpha_api = line_repository_path(config, alpha, "api")
        alpha_web = line_repository_path(config, alpha, "web")
        before_api = _stdout("git", "rev-parse", "HEAD", cwd=alpha_api)
        before_web = _stdout("git", "rev-parse", "HEAD", cwd=alpha_web)

        original_git = workspace.git

        def flaky(repo: Path, *args: str, dry_run: bool = False, timeout: int = 180):
            if not dry_run and args[:1] == ("commit",) and "clients/web" in str(repo):
                return Result(("git", "-C", str(repo), *args), 1, "simulated commit failure")
            return original_git(repo, *args, dry_run=dry_run, timeout=timeout)

        with patch.object(workspace, "git", side_effect=flaky):
            with self.assertRaisesRegex(DyroError, "提交 web 合并"):
                merge_line_from(config, alpha.id, beta.id)

        ops = list((config.root / ".dyro/ops").glob("*.json"))
        self.assertEqual(len(ops), 1)
        op = json.loads(ops[0].read_text(encoding="utf-8"))
        self.assertEqual(op["repositories"]["api"]["outcome"], "rolled_back")
        self.assertEqual(
            _stdout("git", "rev-parse", "HEAD", cwd=alpha_api), before_api
        )
        self.assertEqual(
            _stdout("git", "rev-parse", "HEAD", cwd=alpha_web), before_web
        )

        # Simulate crash after rollback journal: reopen op if finish aborted it.
        if op["phase"] in line_ops.TERMINAL_PHASES:
            op["phase"] = "pending"
            ops[0].write_text(
                json.dumps(op, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            recovered = recover_line_merge(config, alpha.id, op_id=op["op_id"])
        self.assertEqual(recovered.outcomes["api"], "rolled_back")
        self.assertEqual(recovered.outcomes.get("web"), "not_started")
        self.assertNotIn("committed", recovered.outcomes.values())
        text = buffer.getvalue()
        self.assertIn("aborted", text)
        self.assertNotIn("自动", text)
        # No success ledger for rolled_back / not_started.
        if config.ledger_file.exists():
            for line in config.ledger_file.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                if row.get("op_id") == op["op_id"] and row.get("phase") == "line_merge":
                    self.fail(f"unexpected success ledger: {row}")
        # Re-open blocking is cleared after abort; a new merge may start.
        self.assertIsNone(
            line_ops.find_unfinished_op(config, alpha.id, kinds=("merge",))
        )

    def test_unknown_head_drift_blocks_until_manual_recover(self) -> None:
        config, alpha, beta = self._sibling_lines()
        _commit_text(
            line_repository_path(config, beta, "api"),
            "b.txt",
            "b\n",
            "feat: b",
        )
        # Create an unfinished op with M0 snapshots, then drift HEAD without result.
        target = line_repository_path(config, alpha, "api")
        original = _stdout("git", "rev-parse", "HEAD", cwd=target)
        source_head = _stdout(
            "git", "rev-parse", "HEAD", cwd=line_repository_path(config, beta, "api")
        )
        op = line_ops.create_op(
            config,
            kind="merge",
            subject_line_ids=[alpha.id],
            manifest_snapshot={"target": alpha.id, "source": beta.id, "repositories": ["api"]},
            repos_ordered=["api"],
            effective_options={
                "allow_partial": False,
                "integration": "from",
                "source": beta.id,
                "target": alpha.id,
            },
            repository_snapshots={
                "api": {
                    "path": str(target),
                    "storage_mode": "linked-worktree",
                    "original_head": original,
                    "source_head": source_head,
                }
            },
            event_facts={"op_id": "pending", "repo_count": 1, "partial": False},
            op_id="driftop01" + "0" * 24,
        )
        line_ops.update_repo_phase(
            config, op["op_id"], "api", phase="pending", outcome="not_started"
        )
        _commit_text(target, "drift.txt", "drift\n", "feat: drift")

        with self.assertRaisesRegex(DyroError, "未完成操作"):
            merge_line_from(config, alpha.id, beta.id)

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            recovered = recover_line_merge(config, alpha.id, op_id=op["op_id"])
        self.assertEqual(recovered.outcomes["api"], "unknown")
        self.assertIn("unknown", buffer.getvalue())
        self.assertIn("reset --hard", buffer.getvalue())
        with self.assertRaisesRegex(DyroError, "未完成操作"):
            merge_line_from(config, alpha.id, beta.id)

        shell("git", "reset", "--hard", original, cwd=target)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            recovered = recover_line_merge(config, alpha.id, op_id=op["op_id"])
        self.assertEqual(recovered.outcomes["api"], "not_started")
        self.assertEqual(line_ops.load_op(config, op["op_id"])["phase"], "aborted")
        # New op can proceed.
        merge_line_from(config, alpha.id, beta.id)

    def test_frozen_allow_partial_conflict_and_no_mark_unknown(self) -> None:
        config, alpha, beta = self._sibling_lines()
        op = line_ops.create_op(
            config,
            kind="merge",
            subject_line_ids=[alpha.id],
            manifest_snapshot={"target": alpha.id, "source": beta.id, "repositories": ["api"]},
            repos_ordered=["api"],
            effective_options={
                "allow_partial": False,
                "integration": "from",
                "source": beta.id,
                "target": alpha.id,
            },
            repository_snapshots={
                "api": {
                    "path": str(line_repository_path(config, alpha, "api")),
                    "storage_mode": "linked-worktree",
                    "original_head": _stdout(
                        "git",
                        "rev-parse",
                        "HEAD",
                        cwd=line_repository_path(config, alpha, "api"),
                    ),
                    "source_head": _stdout(
                        "git",
                        "rev-parse",
                        "HEAD",
                        cwd=line_repository_path(config, beta, "api"),
                    ),
                }
            },
            event_facts={"op_id": "x" * 32, "repo_count": 1, "partial": False},
            op_id="freeze001" + "0" * 24,
        )
        with self.assertRaisesRegex(DyroError, "恢复选项冲突"):
            recover_line_merge(config, alpha.id, op_id=op["op_id"], allow_partial=True)
        from dyro.cli import build_parser

        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(
                ["line", "merge", "alpha", "--from", "beta", "--mark-unknown"]
            )

    def test_merge_head_recover_then_abort_to_not_started(self) -> None:
        config, alpha, beta = self._sibling_lines()
        _commit_text(
            line_repository_path(config, beta, "api"),
            "m.txt",
            "m\n",
            "feat: m",
        )
        target = line_repository_path(config, alpha, "api")
        original = _stdout("git", "rev-parse", "HEAD", cwd=target)
        source_head = _stdout(
            "git", "rev-parse", "HEAD", cwd=line_repository_path(config, beta, "api")
        )
        op = line_ops.create_op(
            config,
            kind="merge",
            subject_line_ids=[alpha.id],
            manifest_snapshot={"target": alpha.id, "source": beta.id, "repositories": ["api"]},
            repos_ordered=["api"],
            effective_options={
                "allow_partial": False,
                "integration": "from",
                "source": beta.id,
                "target": alpha.id,
            },
            repository_snapshots={
                "api": {
                    "path": str(target),
                    "storage_mode": "linked-worktree",
                    "original_head": original,
                    "source_head": source_head,
                }
            },
            event_facts={"op_id": "y" * 32, "repo_count": 1, "partial": False},
            op_id="mergehd01" + "0" * 24,
        )
        line_ops.update_repo_phase(
            config, op["op_id"], "api", phase="pending", outcome="not_started"
        )
        shell("git", "merge", "--no-ff", "--no-commit", source_head, cwd=target)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            first = recover_line_merge(config, alpha.id, op_id=op["op_id"])
        self.assertEqual(first.outcomes["api"], "unknown")
        self.assertIn("merge --abort", buffer.getvalue())
        shell("git", "merge", "--abort", cwd=target)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            second = recover_line_merge(config, alpha.id, op_id=op["op_id"])
        self.assertEqual(second.outcomes["api"], "not_started")
        self.assertEqual(line_ops.load_op(config, op["op_id"])["phase"], "aborted")
        merge_line_from(config, alpha.id, beta.id)

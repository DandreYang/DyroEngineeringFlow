from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import hashlib
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

from dyro.config import load
from dyro.errors import DyroError
from dyro.workspace import (
    create_line,
    doctor,
    get_line,
    is_missing_origin_finding,
    line_repository_path,
    merge_line,
    repository_path,
    spawn_line,
)

from tests.support import WorkspaceCase, executor_writes_receipt, publish_origin_branch, shell


def shell_stdout(*args: str, cwd: Path, check: bool = True) -> str:
    return subprocess.run(
        args, cwd=cwd, check=check, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    ).stdout.strip()


def commit_file(worktree: Path, name: str, message: str) -> str:
    (worktree / name).write_text(f"{message}\n", encoding="utf-8")
    shell("git", "add", name, cwd=worktree)
    shell("git", "commit", "-m", message, cwd=worktree)
    return shell_stdout("git", "rev-parse", "HEAD", cwd=worktree)


class MissingOriginIsAdvisoryTests(WorkspaceCase):
    """Dyro never pushes, so an unpublished line branch is a WARN, not a FAIL."""

    def test_doctor_reports_missing_origin_as_warn(self) -> None:
        config = load(self.root)
        create_line(config, line_id="local-only", branch="feat/local-only", base="main")
        findings = doctor(config)
        origin = [item for item in findings if "missing origin/feat/local-only" in item]
        self.assertEqual(len(origin), 1, findings)
        self.assertTrue(origin[0].startswith("WARN "), origin[0])
        self.assertFalse(any(item.startswith("FAIL") for item in findings), findings)

    def test_next_is_ready_when_only_origin_is_missing(self) -> None:
        from dyro.cli import main

        config = load(self.root)
        create_line(config, line_id="local-only", branch="feat/local-only", base="main")
        output = StringIO()
        with redirect_stdout(output):
            main(["--root", str(self.root), "next"])
        rendered = output.getvalue()
        self.assertIn("工作区已就绪", rendered)
        self.assertNotIn("还不能开始任务", rendered)
        json_out = StringIO()
        with redirect_stdout(json_out):
            main(["--root", str(self.root), "next", "--format", "json"])
        self.assertEqual(json.loads(json_out.getvalue())["state"], "ready")

    def test_missing_origin_matcher_accepts_warn_shape(self) -> None:
        self.assertTrue(
            is_missing_origin_finding("WARN line:alpha/api: missing origin/feat/alpha")
        )
        self.assertFalse(
            is_missing_origin_finding("WARN overlay 缺少 AGENTS.md: missing origin/x")
        )

    def test_line_create_prints_publish_hint(self) -> None:
        from dyro.cli import main

        output = StringIO()
        with redirect_stdout(output):
            main(["--root", str(self.root), "line", "create", "local-only", "--yes"])
        self.assertIn("git push -u origin feat/local-only", output.getvalue())


class SpawnBaseTests(WorkspaceCase):
    def test_spawn_starts_from_parent_local_head_not_stale_origin(self) -> None:
        publish_origin_branch(self.anchor, "feat/onboard")
        config = load(self.root)
        parent = create_line(config, line_id="onboard", branch="feat/onboard", base="main")
        parent_wt = line_repository_path(config, parent, "api")
        local_head = commit_file(parent_wt, "local.txt", "feat: parent local work")
        child = spawn_line(config, "onboard", "tryon")
        self.assertEqual(child.base, "feat/onboard")
        child_wt = line_repository_path(load(self.root), child, "api")
        self.assertEqual(shell_stdout("git", "rev-parse", "HEAD", cwd=child_wt), local_head)


class TaskCreateTests(WorkspaceCase):
    def _create(self, *extra: str) -> Path:
        from dyro.cli import main

        config = load(self.root)
        create_line(config, line_id="dev", branch="feat/dev", base="main")
        with redirect_stdout(StringIO()):
            main(
                [
                    "--root",
                    str(self.root),
                    "task",
                    "create",
                    "T1",
                    "--title",
                    "trial",
                    "--line",
                    "dev",
                    *extra,
                ]
            )
        return self.root / ".dyro/tasks/T1/task.toml"

    def test_profile_verify_commands_become_task_gates(self) -> None:
        manifest = self._create("--repository", "api").read_text(encoding="utf-8")
        self.assertIn('name = "verify-api-1"', manifest)
        self.assertIn('argv = ["git", "diff", "--check"]', manifest)
        self.assertIn('cwd = "services/api"', manifest)

    def test_executor_defaults_to_a_configured_adapter(self) -> None:
        manifest = self._create("--repository", "api").read_text(encoding="utf-8")
        self.assertIn('agent = "noop"', manifest)
        self.assertNotIn('agent = "codex"', manifest)

    def test_task_may_span_multiple_repositories(self) -> None:
        (self.root / "dyro.toml").write_text(
            (self.root / "dyro.toml").read_text(encoding="utf-8")
            + '\n[repositories.web]\npath = "repositories/web"\nmount = "services/web"\nverify = []\n',
            encoding="utf-8",
        )
        web = self.root / "repositories/web"
        web.mkdir(parents=True)
        shell("git", "init", "-b", "main", cwd=web)
        shell("git", "config", "user.name", "Test User", cwd=web)
        shell("git", "config", "user.email", "test@example.com", cwd=web)
        shell("git", "config", "commit.gpgsign", "false", cwd=web)
        (web / "README.md").write_text("web\n", encoding="utf-8")
        shell("git", "add", "README.md", cwd=web)
        shell("git", "commit", "-m", "chore: initial", cwd=web)
        manifest = self._create("--repository", "api", "--repository", "web").read_text(
            encoding="utf-8"
        )
        self.assertEqual(manifest.count("[[repositories]]"), 2)
        self.assertIn('id = "web"', manifest)

    def _init_second_repo(self, repo_id: str, *, verify: str) -> None:
        (self.root / "dyro.toml").write_text(
            (self.root / "dyro.toml").read_text(encoding="utf-8")
            + f'\n[repositories.{repo_id}]\npath = "repositories/{repo_id}"\n'
            f'mount = "services/{repo_id}"\nverify = {verify}\n',
            encoding="utf-8",
        )
        repo = self.root / f"repositories/{repo_id}"
        repo.mkdir(parents=True)
        shell("git", "init", "-b", "main", cwd=repo)
        shell("git", "config", "user.name", "Test User", cwd=repo)
        shell("git", "config", "user.email", "test@example.com", cwd=repo)
        shell("git", "config", "commit.gpgsign", "false", cwd=repo)
        (repo / "README.md").write_text(f"{repo_id}\n", encoding="utf-8")
        shell("git", "add", "README.md", cwd=repo)
        shell("git", "commit", "-m", "chore: initial", cwd=repo)

    def test_empty_verify_gates_are_unique_per_repository(self) -> None:
        from dyro.tasks import load_task

        (self.root / "dyro.toml").write_text(
            (self.root / "dyro.toml").read_text(encoding="utf-8").replace(
                'verify = [["git", "diff", "--check"]]',
                "verify = []",
            ),
            encoding="utf-8",
        )
        self._init_second_repo("web", verify="[]")
        manifest = self._create("--repository", "api", "--repository", "web").read_text(
            encoding="utf-8"
        )
        self.assertIn('name = "diff-check-api"', manifest)
        self.assertIn('name = "diff-check-web"', manifest)
        self.assertNotIn('name = "diff-check"\n', manifest)
        load_task(load(self.root), "T1")


class DependencyReleaseTests(WorkspaceCase):
    def test_dependency_requires_line_worktree_on_line_branch(self) -> None:
        from dyro.tasks import _assert_line_worktree_on_branch, load_task

        config = load(self.root)
        line = create_line(config, line_id="dev", branch="feat/dev", base="main")
        line_wt = line_repository_path(config, line, "api")
        head = shell_stdout("git", "rev-parse", "HEAD", cwd=line_wt)
        task_dir = self.root / ".dyro/tasks/T1"
        task_dir.mkdir(parents=True)
        (task_dir / "task.toml").write_text(
            'schema_version = 1\nid = "T1"\ntitle = "t"\nline = "dev"\n'
            '[executor]\nagent = "noop"\n[reviewer]\nagent = "noop"\n'
            '[[repositories]]\nid = "api"\n',
            encoding="utf-8",
        )
        (task_dir / "task-heads.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "task_id": "T1",
                    "line": "dev",
                    "branch": "task/T1",
                    "repositories": {"api": head},
                }
            ),
            encoding="utf-8",
        )
        task = load_task(config, "T1")
        _assert_line_worktree_on_branch(config, task)
        shell("git", "checkout", "--detach", head, cwd=line_wt)
        with self.assertRaisesRegex(DyroError, "feat/dev"):
            _assert_line_worktree_on_branch(config, task)

    def test_line_branch_check_does_not_walk_to_overlay_git(self) -> None:
        from dyro.tasks import _assert_line_worktree_on_branch, load_task

        config = load(self.root)
        line = create_line(config, line_id="dev", branch="feat/dev", base="main")
        dest = line_repository_path(config, line, "api")
        shell("git", "worktree", "remove", str(dest), cwd=repository_path(config, "api"))
        dest.mkdir(parents=True)
        shell("git", "init", "-b", "feat/dev", cwd=self.root)
        shell("git", "config", "user.name", "Test User", cwd=self.root)
        shell("git", "config", "user.email", "test@example.com", cwd=self.root)
        shell("git", "config", "commit.gpgsign", "false", cwd=self.root)
        (self.root / "overlay.txt").write_text("overlay\n", encoding="utf-8")
        shell("git", "add", "overlay.txt", cwd=self.root)
        shell("git", "commit", "-m", "overlay", cwd=self.root)
        task_dir = self.root / ".dyro/tasks/T1"
        task_dir.mkdir(parents=True)
        (task_dir / "task.toml").write_text(
            'schema_version = 1\nid = "T1"\ntitle = "t"\nline = "dev"\n'
            '[executor]\nagent = "noop"\n[reviewer]\nagent = "noop"\n'
            '[[repositories]]\nid = "api"\n',
            encoding="utf-8",
        )
        task = load_task(config, "T1")
        with self.assertRaisesRegex(DyroError, "根目录错误"):
            _assert_line_worktree_on_branch(config, task)


class EventLogRotationTests(WorkspaceCase):
    def test_append_rotates_instead_of_failing_at_size_cap(self) -> None:
        from dyro import events

        config = load(self.root)
        with patch.object(events, "MAX_EVENT_LOG_BYTES", 600):
            for index in range(12):
                events.append_event(
                    config, kind="sync", actor="dev", subject=f"child{index}", family="dev"
                )
            path = events.events_path(config)
            self.assertLessEqual(path.stat().st_size, 600)
            archives = sorted(path.parent.glob("events.jsonl.*"))
            self.assertTrue(archives, "expected a rotated archive")
            records, last_seq = events.read_events(config, after_seq=0)
            self.assertEqual(last_seq, 12)
            self.assertEqual(records[-1]["seq"], 12)
            self.assertEqual(records[0]["seq"], 1)
            newer, _ = events.read_events(config, after_seq=last_seq - 1)
            self.assertEqual([item["seq"] for item in newer], [12])

    def test_status_transition_survives_event_log_failure(self) -> None:
        from dyro import tasks
        from dyro.events import EventLogError

        config = load(self.root)
        create_line(config, line_id="dev", branch="feat/dev", base="main")
        task_dir = self.root / ".dyro/tasks/T1"
        task_dir.mkdir(parents=True)
        (task_dir / "task.toml").write_text(
            'schema_version = 1\nid = "T1"\ntitle = "t"\nline = "dev"\n'
            '[executor]\nagent = "noop"\n[reviewer]\nagent = "noop"\n'
            '[[repositories]]\nid = "api"\n',
            encoding="utf-8",
        )
        task = tasks.load_task(config, "T1")
        with patch("dyro.events.append_event", side_effect=EventLogError("EVENT_LOG_INVALID")):
            tasks.set_status(config, task, "assigned")
        self.assertEqual(tasks.status(config, task), "assigned")
        ledger = (self.root / ".dyro/ledger.jsonl").read_text(encoding="utf-8")
        self.assertIn("event_append_failed", ledger)
        self.assertIn('"error_code": "EVENT_LOG_INVALID"', ledger)
        from dyro.events import read_overlay_events

        _records, complete = read_overlay_events(config)
        self.assertFalse(complete)
        self.assertTrue((self.root / ".dyro" / "events.gap").is_file())

    def test_read_overlay_events_stitches_archives_when_current_missing(self) -> None:
        from dyro import events

        config = load(self.root)
        with patch.object(events, "MAX_EVENT_LOG_BYTES", 600):
            for index in range(12):
                events.append_event(
                    config, kind="sync", actor="dev", subject=f"child{index}", family="dev"
                )
            path = events.events_path(config)
            self.assertTrue(list(path.parent.glob("events.jsonl.*")))
            path.unlink()
            records, complete = events.read_overlay_events(config)
        self.assertTrue(complete)
        self.assertGreaterEqual(len(records), 1)
        self.assertEqual(records[0]["seq"], 1)

    def test_non_seq_archive_suffix_does_not_fail_closed(self) -> None:
        from dyro import events

        config = load(self.root)
        events.append_event(
            config, kind="sync", actor="dev", subject="child", family="dev"
        )
        path = events.events_path(config)
        (path.parent / "events.jsonl.bak").write_text("not-json\n", encoding="utf-8")
        records, last_seq = events.read_events(config)
        self.assertEqual(last_seq, 1)
        overlay, complete = events.read_overlay_events(config)
        self.assertTrue(complete)
        self.assertEqual(len(overlay), 1)


class StaleReceiptTests(WorkspaceCase):
    def test_previous_receipt_is_not_reused_by_a_new_attempt(self) -> None:
        from dyro.cli import main
        from dyro.tasks import load_task, run_task

        config = load(self.root)
        create_line(config, line_id="dev", branch="feat/dev", base="main")
        with redirect_stdout(StringIO()):
            main(
                [
                    "--root",
                    str(self.root),
                    "task",
                    "create",
                    "T1",
                    "--title",
                    "trial",
                    "--line",
                    "dev",
                    "--repository",
                    "api",
                ]
            )
        task_dir = self.root / ".dyro/tasks/T1"
        (task_dir / "receipt.md").write_text("result: DONE\n", encoding="utf-8")
        task = load_task(config, "T1")
        self.assertEqual(run_task(config, task), "failed")
        self.assertFalse((task_dir / "receipt.md").exists())


class HonestyRepairTests(WorkspaceCase):
    def test_global_dry_run_host_compile_does_not_write(self) -> None:
        from dyro.cli import main
        from dyro.host import projection_root

        create_line(load(self.root), line_id="dev", branch="feat/dev", base="main")
        output = StringIO()
        with redirect_stdout(output):
            main(["--root", str(self.root), "--dry-run", "host", "compile"])
        self.assertIn("DRY RUN", output.getvalue())
        self.assertFalse(
            (projection_root(load(self.root), user=False) / "cli").exists()
        )
        self.assertFalse((self.root / ".dyro" / "host.lock").exists())

    def test_proof_export_dry_run_does_not_write_zip(self) -> None:
        from dyro.cli import main

        bundle = self.root / "proofs.zip"
        with redirect_stdout(StringIO()):
            main(
                [
                    "--root",
                    str(self.root),
                    "--dry-run",
                    "proof",
                    "export",
                    "--task",
                    "T1",
                    "--bundle",
                    str(bundle),
                ]
            )
        self.assertFalse(bundle.exists())

    def test_task_close_removes_worktree_after_failure(self) -> None:
        from dyro.cli import main
        from dyro.tasks import close_task, load_task, run_task, status, worktree_root

        config = load(self.root)
        create_line(config, line_id="dev", branch="feat/dev", base="main")
        with redirect_stdout(StringIO()):
            main(
                [
                    "--root",
                    str(self.root),
                    "task",
                    "create",
                    "T1",
                    "--title",
                    "trial",
                    "--line",
                    "dev",
                    "--repository",
                    "api",
                ]
            )
        task = load_task(config, "T1")
        self.assertEqual(run_task(config, task), "failed")
        self.assertEqual(status(config, task), "failed")
        root = worktree_root(config, task)
        self.assertTrue(root.exists())
        close_task(config, task)
        self.assertFalse((root / "services/api").exists())

    def test_review_fail_verdict_exits_nonzero(self) -> None:
        from dyro.cli import main
        from dyro.tasks import load_task, run_task

        config = load(self.root)
        create_line(config, line_id="dev", branch="feat/dev", base="main")
        with redirect_stdout(StringIO()):
            main(
                [
                    "--root",
                    str(self.root),
                    "task",
                    "create",
                    "T1",
                    "--title",
                    "trial",
                    "--line",
                    "dev",
                    "--repository",
                    "api",
                ]
            )
        task_dir = self.root / ".dyro/tasks/T1"
        task = load_task(config, "T1")
        with executor_writes_receipt(task_dir):
            self.assertEqual(run_task(config, task), "review")
        task_dir.joinpath("review.md").write_text("verdict: FAIL\n", encoding="utf-8")
        with redirect_stdout(StringIO()), self.assertRaises(SystemExit) as raised:
            main(["--root", str(self.root), "task", "review", "T1"])
        self.assertEqual(raised.exception.code, 2)
        from dyro.tasks import status

        self.assertEqual(status(config, task), "review")

    def _create_named_task(self, task_id: str = "T1") -> None:
        from dyro.cli import main

        create_line(load(self.root), line_id="dev", branch="feat/dev", base="main")
        with redirect_stdout(StringIO()):
            main(
                [
                    "--root",
                    str(self.root),
                    "task",
                    "create",
                    task_id,
                    "--title",
                    "trial",
                    "--line",
                    "dev",
                    "--repository",
                    "api",
                ]
            )

    def test_task_close_refuses_symlink_mount_and_keeps_line_worktree(self) -> None:
        from dyro.tasks import close_task, load_task, run_task, worktree_root

        self._create_named_task()
        config = load(self.root)
        task = load_task(config, "T1")
        self.assertEqual(run_task(config, task), "failed")
        mount = worktree_root(config, task) / "services/api"
        line_wt = line_repository_path(config, get_line(config, "dev"), "api")
        shell("git", "worktree", "remove", str(mount), cwd=repository_path(config, "api"))
        mount.symlink_to(line_wt)
        with self.assertRaisesRegex(DyroError, "符号链接"):
            close_task(config, task)
        self.assertTrue(line_wt.is_dir())
        self.assertEqual(
            shell_stdout("git", "rev-parse", "--is-inside-work-tree", cwd=line_wt),
            "true",
        )

    def test_task_close_refuses_symlinked_task_root(self) -> None:
        import shutil

        from dyro.tasks import close_task, load_task, run_task, worktree_root
        from dyro.workspace import line_root

        self._create_named_task()
        config = load(self.root)
        task = load_task(config, "T1")
        self.assertEqual(run_task(config, task), "failed")
        root = worktree_root(config, task)
        mount = root / "services/api"
        line_dir = line_root(config, get_line(config, "dev"))
        line_wt = line_repository_path(config, get_line(config, "dev"), "api")
        shell("git", "worktree", "remove", str(mount), cwd=repository_path(config, "api"))
        shutil.rmtree(root)
        root.symlink_to(line_dir)
        with self.assertRaisesRegex(DyroError, "符号链接"):
            close_task(config, task)
        self.assertTrue(line_wt.is_dir())
        self.assertEqual(
            shell_stdout("git", "rev-parse", "--is-inside-work-tree", cwd=line_wt),
            "true",
        )

    def test_merge_dry_run_timeout_still_aborts_merge_head(self) -> None:
        from dyro import tasks as tasks_mod
        from dyro.provenance import review_binding
        from dyro.tasks import (
            answer_task,
            load_task,
            merge_task,
            review_task,
            run_task,
            worktree_root,
        )

        self._create_named_task()
        config = load(self.root)
        task = load_task(config, "T1")
        task_dir = task.directory
        with executor_writes_receipt(task_dir, "result: QUESTION\n"):
            self.assertEqual(run_task(config, task), "waiting_answer")
        wt = worktree_root(config, task) / "services/api"
        (wt / "change.txt").write_text("change\n", encoding="utf-8")
        shell("git", "add", "change.txt", cwd=wt)
        shell("git", "commit", "-m", "feat: change", cwd=wt)
        task_dir.joinpath("receipt.md").write_text("result: DONE\n", encoding="utf-8")
        self.assertEqual(answer_task(config, task, "continue"), "review")
        receipt_hash = hashlib.sha256(task_dir.joinpath("receipt.md").read_bytes()).hexdigest()
        heads_hash = hashlib.sha256(task_dir.joinpath("task-heads.json").read_bytes()).hexdigest()
        binding = review_binding(task_dir)
        task_dir.joinpath("review.md").write_text(
            "verdict: PASS\n"
            f"receipt_sha256: {receipt_hash}\n"
            f"task_heads_sha256: {heads_hash}\n"
            f"attempt_id: {binding[0]}\n"
            f"plan_sha256: {binding[1]}\n",
            encoding="utf-8",
        )
        self.assertEqual(review_task(config, task), "done")
        original = tasks_mod.git

        def flaky_git(repo, *args, dry_run=False, timeout=180):
            result = original(repo, *args, dry_run=dry_run, timeout=timeout)
            if args[:1] == ("merge",):
                raise DyroError("命令超时（300s）：git merge")
            return result

        line_wt = line_repository_path(config, get_line(config, "dev"), "api")
        with patch.object(tasks_mod, "git", side_effect=flaky_git):
            with self.assertRaisesRegex(DyroError, "超时"):
                merge_task(config, task, dry_run=True)
        merge_head = subprocess.run(
            ["git", "-C", str(line_wt), "rev-parse", "-q", "--verify", "MERGE_HEAD"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertNotEqual(merge_head.returncode, 0)

    def test_external_profile_dry_run_gates_do_not_execute_argv(self) -> None:
        from dyro.tasks import load_task, run_gates

        (self.root / "dyro.toml").write_text(
            (self.root / "dyro.toml").read_text(encoding="utf-8").replace(
                "require_clean_merge = true",
                'require_clean_merge = true\nexecution_mode = "external"\n',
            ),
            encoding="utf-8",
        )
        self._create_named_task()
        task = load_task(load(self.root), "T1")
        with patch("dyro.tasks.run", side_effect=AssertionError("argv executed")):
            with self.assertRaisesRegex(DyroError, "不会在本机执行门禁"):
                run_gates(load(self.root), task, dry_run=True)

    def test_task_create_refuses_empty_adapters(self) -> None:
        from dyro.cli import main

        (self.root / "dyro.toml").write_text(
            (self.root / "dyro.toml").read_text(encoding="utf-8").replace(
                '[adapters.noop]\nlaunch = ["/usr/bin/true"]\n'
                'read = ["/usr/bin/true"]\nwrite = ["/usr/bin/true"]\n\n',
                "",
            ),
            encoding="utf-8",
        )
        create_line(load(self.root), line_id="dev", branch="feat/dev", base="main")
        with redirect_stdout(StringIO()), self.assertRaises(SystemExit) as raised:
            main(
                [
                    "--root",
                    str(self.root),
                    "task",
                    "create",
                    "T1",
                    "--title",
                    "trial",
                    "--line",
                    "dev",
                    "--repository",
                    "api",
                ]
            )
        self.assertEqual(raised.exception.code, 2)
        with redirect_stdout(StringIO()), self.assertRaises(SystemExit) as dry:
            main(
                [
                    "--root",
                    str(self.root),
                    "--dry-run",
                    "task",
                    "create",
                    "T1",
                    "--title",
                    "trial",
                    "--line",
                    "dev",
                    "--repository",
                    "api",
                ]
            )
        self.assertEqual(dry.exception.code, 2)

    def _reach_done_with_commit(self, config, task):
        from dyro.provenance import review_binding
        from dyro.tasks import answer_task, review_task, run_task, worktree_root

        task_dir = task.directory
        with executor_writes_receipt(task_dir, "result: QUESTION\n"):
            self.assertEqual(run_task(config, task), "waiting_answer")
        wt = worktree_root(config, task) / "services/api"
        (wt / "change.txt").write_text("change\n", encoding="utf-8")
        shell("git", "add", "change.txt", cwd=wt)
        shell("git", "commit", "-m", "feat: change", cwd=wt)
        task_dir.joinpath("receipt.md").write_text("result: DONE\n", encoding="utf-8")
        self.assertEqual(answer_task(config, task, "continue"), "review")
        receipt_hash = hashlib.sha256(task_dir.joinpath("receipt.md").read_bytes()).hexdigest()
        heads_hash = hashlib.sha256(
            task_dir.joinpath("task-heads.json").read_bytes()
        ).hexdigest()
        binding = review_binding(task_dir)
        task_dir.joinpath("review.md").write_text(
            "verdict: PASS\n"
            f"receipt_sha256: {receipt_hash}\n"
            f"task_heads_sha256: {heads_hash}\n"
            f"attempt_id: {binding[0]}\n"
            f"plan_sha256: {binding[1]}\n",
            encoding="utf-8",
        )
        self.assertEqual(review_task(config, task), "done")
        return wt

    def test_task_close_refuses_dirty_done_worktree(self) -> None:
        from dyro.tasks import close_task, load_task

        self._create_named_task()
        config = load(self.root)
        task = load_task(config, "T1")
        wt = self._reach_done_with_commit(config, task)
        (wt / "dirty.txt").write_text("dirty\n", encoding="utf-8")
        with self.assertRaisesRegex(DyroError, "不干净"):
            close_task(config, task)

    def test_task_close_refuses_unmerged_done_branch(self) -> None:
        from dyro.tasks import close_task, load_task

        self._create_named_task()
        config = load(self.root)
        task = load_task(config, "T1")
        self._reach_done_with_commit(config, task)
        with self.assertRaisesRegex(DyroError, "尚未合入"):
            close_task(config, task)

    def test_task_loop_exits_nonzero_on_review_failed(self) -> None:
        from dyro.cli import main
        from dyro.tasks import load_task, run_task

        self._create_named_task()
        config = load(self.root)
        task = load_task(config, "T1")
        with executor_writes_receipt(task.directory):
            self.assertEqual(run_task(config, task), "review")
        task.directory.joinpath("review.md").write_text("verdict: FAIL\n", encoding="utf-8")
        with redirect_stdout(StringIO()), self.assertRaises(SystemExit) as raised:
            main(["--root", str(self.root), "task", "loop"])
        self.assertEqual(raised.exception.code, 2)

    def test_task_daemon_once_exits_nonzero_on_review_failed(self) -> None:
        from dyro.cli import main
        from dyro.tasks import load_task, run_task

        self._create_named_task()
        config = load(self.root)
        task = load_task(config, "T1")
        with executor_writes_receipt(task.directory):
            self.assertEqual(run_task(config, task), "review")
        task.directory.joinpath("review.md").write_text("verdict: FAIL\n", encoding="utf-8")
        with redirect_stdout(StringIO()), self.assertRaises(SystemExit) as raised:
            main(["--root", str(self.root), "task", "daemon", "--once"])
        self.assertEqual(raised.exception.code, 2)


class LineMergeProbeTests(WorkspaceCase):
    def test_line_merge_timeout_still_aborts_merge_head(self) -> None:
        from dyro import workspace as workspace_mod

        publish_origin_branch(self.anchor, "feat/onboard")
        config = load(self.root)
        parent = create_line(config, line_id="onboard", branch="feat/onboard", base="main")
        child = spawn_line(config, "onboard", "tryon")
        child_wt = line_repository_path(config, child, "api")
        commit_file(child_wt, "child.txt", "feat: child work")
        parent_wt = line_repository_path(config, parent, "api")
        original = workspace_mod.git

        def flaky_git(repo, *args, dry_run=False, timeout=180):
            result = original(repo, *args, dry_run=dry_run, timeout=timeout)
            if args[:1] == ("merge",):
                raise DyroError("命令超时（300s）：git merge")
            return result

        with patch.object(workspace_mod, "git", side_effect=flaky_git):
            with self.assertRaisesRegex(DyroError, "超时"):
                merge_line(config, child.id, parent.id, dry_run=True)
        merge_head = subprocess.run(
            ["git", "-C", str(parent_wt), "rev-parse", "-q", "--verify", "MERGE_HEAD"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertNotEqual(merge_head.returncode, 0)

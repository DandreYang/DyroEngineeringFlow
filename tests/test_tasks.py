import hashlib
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import zipfile

from dyro.config import ValidationError, load
from dyro.evidence import build_execution_bundle, unpack_execution_bundle
from dyro.errors import DyroError
from dyro.tasks import (
    answer_task,
    claim_task,
    import_execution_evidence,
    import_review_evidence,
    load_task,
    merge_task,
    review_task,
    run_task,
    set_status,
    signoff_task,
    status,
    task_template,
)
from dyro.workspace import create_line

from .support import WorkspaceCase, shell


class TaskTests(WorkspaceCase):
    @staticmethod
    def _write_bound_review(task_path: Path) -> None:
        receipt_hash = hashlib.sha256(task_path.joinpath("receipt.md").read_bytes()).hexdigest()
        heads_hash = hashlib.sha256(task_path.joinpath("task-heads.json").read_bytes()).hexdigest()
        task_path.joinpath("review.md").write_text(
            f"verdict: PASS\nreceipt_sha256: {receipt_hash}\ntask_heads_sha256: {heads_hash}\n",
            encoding="utf-8",
        )

    def test_dry_run_does_not_change_task_state(self) -> None:
        config = load(self.root)
        create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        task_path = config.task_specs_dir / "TASK-DRY"
        task_path.mkdir(parents=True)
        task_path.joinpath("task.toml").write_text(
            task_template("TASK-DRY", "dry run", "alpha", "api", "services/api").replace('agent = "codex"', 'agent = "noop"'),
            encoding="utf-8",
        )
        task_path.joinpath("handoff.md").write_text("# handoff\n", encoding="utf-8")
        task = load_task(config, "TASK-DRY")
        self.assertEqual(run_task(config, task, dry_run=True), "dry-run")
        self.assertEqual(status(config, task), "backlog")

    def test_run_review_and_merge_task(self) -> None:
        config = load(self.root)
        create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        task_path = config.task_specs_dir / "TASK-1"
        task_path.mkdir(parents=True)
        task_path.joinpath("task.toml").write_text(
            task_template("TASK-1", "verify task lifecycle", "alpha", "api", "services/api").replace('agent = "codex"', 'agent = "noop"'),
            encoding="utf-8",
        )
        task_path.joinpath("handoff.md").write_text("# handoff\n", encoding="utf-8")
        task_path.joinpath("receipt.md").write_text("result: DONE\n", encoding="utf-8")
        task = load_task(config, "TASK-1")
        self.assertEqual(run_task(config, task), "review")
        self.assertEqual(status(config, task), "review")
        self._write_bound_review(task_path)
        self.assertEqual(review_task(config, task), "done")
        self.assertEqual(status(config, task), "done")
        merge_task(config, task)

    def test_external_signoff_is_required_after_receipt_bound_review(self) -> None:
        config_path = self.root / "dyro.toml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "require_clean_merge = true", "require_clean_merge = true\nrequire_external_signoff = true"
            ),
            encoding="utf-8",
        )
        config = load(self.root)
        create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        task_path = config.task_specs_dir / "TASK-SIGNOFF"
        task_path.mkdir(parents=True)
        task_path.joinpath("task.toml").write_text(
            task_template("TASK-SIGNOFF", "requires signoff", "alpha", "api", "services/api").replace('agent = "codex"', 'agent = "noop"'),
            encoding="utf-8",
        )
        task_path.joinpath("handoff.md").write_text("# handoff\n", encoding="utf-8")
        task_path.joinpath("receipt.md").write_text("result: DONE\n", encoding="utf-8")
        task = load_task(config, "TASK-SIGNOFF")
        self.assertEqual(run_task(config, task), "review")
        self._write_bound_review(task_path)

        self.assertEqual(review_task(config, task), "review_pending_signoff")
        self.assertEqual(status(config, task), "review_pending_signoff")
        with self.assertRaisesRegex(DyroError, "要求外部签收"):
            set_status(config, task, "done", force=True)
        self.assertEqual(signoff_task(config, task, approver="release-manager"), "done")
        self.assertEqual(status(config, task), "done")
        signoff = task_path.joinpath("signoff.json").read_text(encoding="utf-8")
        self.assertIn('"approver": "release-manager"', signoff)

    def test_external_execution_mode_blocks_local_agent_execution_but_allows_dry_run(self) -> None:
        config_path = self.root / "dyro.toml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "require_clean_merge = true", "require_clean_merge = true\nexecution_mode = \"external\""
            ),
            encoding="utf-8",
        )
        config = load(self.root)
        create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        task_path = config.task_specs_dir / "TASK-EXTERNAL"
        task_path.mkdir(parents=True)
        task_path.joinpath("task.toml").write_text(
            task_template("TASK-EXTERNAL", "external only", "alpha", "api", "services/api").replace('agent = "codex"', 'agent = "noop"'),
            encoding="utf-8",
        )
        task_path.joinpath("handoff.md").write_text("# handoff\n", encoding="utf-8")
        task = load_task(config, "TASK-EXTERNAL")

        self.assertEqual(run_task(config, task, dry_run=True), "dry-run")
        with self.assertRaisesRegex(DyroError, "外部隔离执行器"):
            run_task(config, task)

    def test_external_runner_claims_and_imports_receipt_gate_and_review_evidence(self) -> None:
        config_path = self.root / "dyro.toml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "require_clean_merge = true", "require_clean_merge = true\nexecution_mode = \"external\""
            ),
            encoding="utf-8",
        )
        config = load(self.root)
        create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        task_path = config.task_specs_dir / "TASK-IMPORT"
        task_path.mkdir(parents=True)
        task_path.joinpath("task.toml").write_text(
            task_template("TASK-IMPORT", "external evidence", "alpha", "api", "services/api").replace('agent = "codex"', 'agent = "noop"'),
            encoding="utf-8",
        )
        task_path.joinpath("handoff.md").write_text("# handoff\n", encoding="utf-8")
        task = load_task(config, "TASK-IMPORT")
        runner_dir = self.root / "external-runner"
        runner_dir.mkdir()
        receipt = runner_dir / "receipt.md"
        receipt.write_text("result: DONE\n", encoding="utf-8")
        receipt_hash = hashlib.sha256(receipt.read_bytes()).hexdigest()
        gate_log = runner_dir / "diff-check.log"
        gate_log.write_text("diff check passed\n", encoding="utf-8")
        gate_log_hash = hashlib.sha256(gate_log.read_bytes()).hexdigest()
        gates = runner_dir / "gates.json"
        gates.write_text(
            '{"schema_version": 1, "task_id": "TASK-IMPORT", "receipt_sha256": "'
            + receipt_hash
            + '", "gates": [{"name": "diff-check", "exit_code": 0, "log": "diff-check.log", "log_sha256": "'
            + gate_log_hash
            + '"}]}\n',
            encoding="utf-8",
        )
        heads = runner_dir / "task-heads.json"
        heads.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "task_id": "TASK-IMPORT",
                    "line": "alpha",
                    "branch": "task/TASK-IMPORT",
                    "repositories": {"api": "a" * 40},
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        self.assertEqual(claim_task(config, task, runner="isolated-runner-1"), "assigned")
        self.assertEqual(import_execution_evidence(config, task, receipt=receipt, gates=gates, heads=heads), "review")
        self.assertEqual(status(config, task), "review")
        self.assertEqual((task_path / "evidence/gates/gate-1.log").read_text(encoding="utf-8"), "diff check passed\n")
        review = runner_dir / "review.md"
        heads_hash = hashlib.sha256(heads.read_bytes()).hexdigest()
        review.write_text(
            f"verdict: PASS\nreceipt_sha256: {receipt_hash}\ntask_heads_sha256: {heads_hash}\n",
            encoding="utf-8",
        )
        self.assertEqual(import_review_evidence(config, task, review=review), "done")
        self.assertEqual(status(config, task), "done")

    def test_external_claim_is_serialized_for_two_local_process_threads(self) -> None:
        config_path = self.root / "dyro.toml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "require_clean_merge = true", "require_clean_merge = true\nexecution_mode = \"external\""
            ),
            encoding="utf-8",
        )
        config = load(self.root)
        create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        task_path = config.task_specs_dir / "TASK-CLAIM-LOCK"
        task_path.mkdir(parents=True)
        task_path.joinpath("task.toml").write_text(
            task_template("TASK-CLAIM-LOCK", "serialized external claim", "alpha", "api", "services/api").replace(
                'agent = "codex"', 'agent = "noop"'
            ),
            encoding="utf-8",
        )
        task_path.joinpath("handoff.md").write_text("# handoff\n", encoding="utf-8")
        task = load_task(config, "TASK-CLAIM-LOCK")

        def claim(runner: str) -> str:
            try:
                return claim_task(config, task, runner=runner)
            except DyroError:
                return "rejected"

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(claim, ("runner-a", "runner-b")))
        self.assertEqual(sorted(outcomes), ["assigned", "rejected"])
        self.assertEqual(status(config, task), "assigned")

    def test_external_claim_blocks_another_task_in_the_same_conflict_group(self) -> None:
        config_path = self.root / "dyro.toml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "require_clean_merge = true", "require_clean_merge = true\nexecution_mode = \"external\""
            ),
            encoding="utf-8",
        )
        config = load(self.root)
        create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        for task_id in ("TASK-GROUP-A", "TASK-GROUP-B"):
            task_path = config.task_specs_dir / task_id
            task_path.mkdir(parents=True)
            task_path.joinpath("task.toml").write_text(
                task_template(task_id, "exclusive external claim", "alpha", "api", "services/api")
                .replace('agent = "codex"', 'agent = "noop"')
                .replace('conflict_group = ""', 'conflict_group = "shared-resource"'),
                encoding="utf-8",
            )
            task_path.joinpath("handoff.md").write_text("# handoff\n", encoding="utf-8")

        self.assertEqual(claim_task(config, load_task(config, "TASK-GROUP-A"), runner="runner-a"), "assigned")
        with self.assertRaisesRegex(DyroError, "活跃任务"):
            claim_task(config, load_task(config, "TASK-GROUP-B"), runner="runner-b")

    def test_external_runner_builds_and_imports_a_portable_evidence_bundle(self) -> None:
        config_path = self.root / "dyro.toml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "require_clean_merge = true", "require_clean_merge = true\nexecution_mode = \"external\""
            ),
            encoding="utf-8",
        )
        config = load(self.root)
        create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        task_path = config.task_specs_dir / "TASK-BUNDLE"
        task_path.mkdir(parents=True)
        task_path.joinpath("task.toml").write_text(
            task_template("TASK-BUNDLE", "portable external evidence", "alpha", "api", "services/api").replace(
                'agent = "codex"', 'agent = "noop"'
            ),
            encoding="utf-8",
        )
        task_path.joinpath("handoff.md").write_text("# handoff\n", encoding="utf-8")
        task = load_task(config, "TASK-BUNDLE")

        workspace = self.root / "isolated-runner"
        repository = workspace / "services/api"
        repository.parent.mkdir(parents=True)
        shell("git", "clone", str(self.anchor), str(repository), cwd=self.root)
        shell("git", "checkout", "-b", "task/TASK-BUNDLE", cwd=repository)
        receipt = workspace / "receipt.md"
        receipt.write_text("result: DONE\n", encoding="utf-8")
        bundle = self.root / "execution.zip"

        result = build_execution_bundle(config, task, workspace=workspace, receipt=receipt, output=bundle)
        self.assertEqual(result.result, "DONE")
        self.assertTrue(result.gates_passed)
        self.assertTrue(bundle.is_file())

        self.assertEqual(claim_task(config, task, runner="isolated-runner-1"), "assigned")
        with unpack_execution_bundle(bundle) as evidence:
            self.assertEqual(
                import_execution_evidence(
                    config,
                    task,
                    receipt=evidence["receipt"],
                    gates=evidence["gates"],
                    heads=evidence["heads"],
                ),
                "review",
            )
        self.assertEqual(status(config, task), "review")

    def test_external_evidence_bundle_rejects_path_traversal(self) -> None:
        bundle = self.root / "unsafe-evidence.zip"
        with zipfile.ZipFile(bundle, "w") as archive:
            archive.writestr("receipt.md", "result: DONE\n")
            archive.writestr("../escape.txt", "nope")
        with self.assertRaisesRegex(ValidationError, "不安全路径"):
            with unpack_execution_bundle(bundle):
                pass

    def test_external_evidence_bundle_rejects_windows_style_path_traversal(self) -> None:
        bundle = self.root / "unsafe-windows-evidence.zip"
        with zipfile.ZipFile(bundle, "w") as archive:
            archive.writestr("receipt.md", "result: DONE\n")
            archive.writestr("gates/..\\..\\escape.log", "nope")
        with self.assertRaisesRegex(ValidationError, "POSIX 分隔符"):
            with unpack_execution_bundle(bundle):
                pass

    def test_allows_a_human_gate_name_without_using_it_as_a_log_path(self) -> None:
        config = load(self.root)
        create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        task_path = config.task_specs_dir / "TASK-GATE-NAME"
        task_path.mkdir(parents=True)
        task_path.joinpath("task.toml").write_text(
            task_template("TASK-GATE-NAME", "safe gate names", "alpha", "api", "services/api")
            .replace('agent = "codex"', 'agent = "noop"')
            .replace('name = "diff-check"', 'name = "unit tests / edge cases"'),
            encoding="utf-8",
        )
        task_path.joinpath("handoff.md").write_text("# handoff\n", encoding="utf-8")
        task_path.joinpath("receipt.md").write_text("result: DONE\n", encoding="utf-8")
        task = load_task(config, "TASK-GATE-NAME")
        self.assertEqual(run_task(config, task), "review")
        self.assertTrue((task_path / "logs/gate-1.log").is_file())
        self.assertFalse((task_path / "logs/unit tests / edge cases.log").exists())

    def test_rejects_string_merge_booleans(self) -> None:
        config = load(self.root)
        create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        task_path = config.task_specs_dir / "TASK-BOOL"
        task_path.mkdir(parents=True)
        task_path.joinpath("task.toml").write_text(
            task_template("TASK-BOOL", "strict booleans", "alpha", "api", "services/api")
            .replace('agent = "codex"', 'agent = "noop"')
            .replace("auto = false", 'auto = "false"'),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValidationError, "merge.auto 必须是布尔值"):
            load_task(config, "TASK-BOOL")

    def test_review_rejects_task_head_drift(self) -> None:
        config = load(self.root)
        create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        task_path = config.task_specs_dir / "TASK-DRIFT"
        task_path.mkdir(parents=True)
        task_path.joinpath("task.toml").write_text(
            task_template("TASK-DRIFT", "bind reviewed heads", "alpha", "api", "services/api").replace(
                'agent = "codex"', 'agent = "noop"'
            ),
            encoding="utf-8",
        )
        task_path.joinpath("handoff.md").write_text("# handoff\n", encoding="utf-8")
        task_path.joinpath("receipt.md").write_text("result: DONE\n", encoding="utf-8")
        task = load_task(config, "TASK-DRIFT")
        self.assertEqual(run_task(config, task), "review")
        self._write_bound_review(task_path)

        task_repository = self.root / "worktrees/alpha/TASK-DRIFT/services/api"
        task_repository.joinpath("AFTER_REVIEW.txt").write_text("drift\n", encoding="utf-8")
        shell("git", "add", "AFTER_REVIEW.txt", cwd=task_repository)
        shell("git", "commit", "-m", "test: drift after execution", cwd=task_repository)

        with self.assertRaisesRegex(DyroError, "偏离已记录 HEAD"):
            review_task(config, task)
        self.assertEqual(status(config, task), "review")

    def test_review_detects_reviewer_source_mutation(self) -> None:
        config_path = self.root / "dyro.toml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                'read = ["/usr/bin/true"]',
                'read = ["/usr/bin/touch", "services/api/REVIEW_MUTATION"]',
            ),
            encoding="utf-8",
        )
        config = load(self.root)
        create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        task_path = config.task_specs_dir / "TASK-REVIEW-GUARD"
        task_path.mkdir(parents=True)
        task_path.joinpath("task.toml").write_text(
            task_template("TASK-REVIEW-GUARD", "guard review source", "alpha", "api", "services/api").replace(
                'agent = "codex"', 'agent = "noop"'
            ),
            encoding="utf-8",
        )
        task_path.joinpath("handoff.md").write_text("# handoff\n", encoding="utf-8")
        task_path.joinpath("receipt.md").write_text("result: DONE\n", encoding="utf-8")
        task = load_task(config, "TASK-REVIEW-GUARD")
        self.assertEqual(run_task(config, task), "review")
        self._write_bound_review(task_path)

        with self.assertRaisesRegex(DyroError, "复核期间任务源码发生变化"):
            review_task(config, task)
        self.assertEqual(status(config, task), "review")

    def test_existing_non_git_task_destination_is_rejected(self) -> None:
        config = load(self.root)
        create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        task_path = config.task_specs_dir / "TASK-STALE"
        task_path.mkdir(parents=True)
        task_path.joinpath("task.toml").write_text(
            task_template("TASK-STALE", "reject stale destination", "alpha", "api", "services/api").replace(
                'agent = "codex"', 'agent = "noop"'
            ),
            encoding="utf-8",
        )
        task_path.joinpath("handoff.md").write_text("# handoff\n", encoding="utf-8")
        stale = self.root / "worktrees/alpha/TASK-STALE/services/api"
        stale.mkdir(parents=True)

        with self.assertRaisesRegex(DyroError, "不是有效的任务 Git worktree"):
            run_task(config, load_task(config, "TASK-STALE"))

    def test_run_task_rejects_uncommitted_source_changes(self) -> None:
        config_path = self.root / "dyro.toml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                'write = ["/usr/bin/true"]',
                'write = ["/usr/bin/touch", "services/api/UNCOMMITTED"]',
            ),
            encoding="utf-8",
        )
        config = load(self.root)
        create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        task_path = config.task_specs_dir / "TASK-DIRTY"
        task_path.mkdir(parents=True)
        task_path.joinpath("task.toml").write_text(
            task_template("TASK-DIRTY", "reject uncommitted source", "alpha", "api", "services/api").replace(
                'agent = "codex"', 'agent = "noop"'
            ),
            encoding="utf-8",
        )
        task_path.joinpath("handoff.md").write_text("# handoff\n", encoding="utf-8")
        task_path.joinpath("receipt.md").write_text("result: DONE\n", encoding="utf-8")
        task = load_task(config, "TASK-DIRTY")

        with self.assertRaisesRegex(DyroError, "必须先提交全部改动"):
            run_task(config, task)
        self.assertEqual(status(config, task), "failed")

    def test_auto_merge_failure_does_not_mark_task_done(self) -> None:
        config = load(self.root)
        create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        task_path = config.task_specs_dir / "TASK-AUTO"
        task_path.mkdir(parents=True)
        task_path.joinpath("task.toml").write_text(
            task_template("TASK-AUTO", "safe auto merge", "alpha", "api", "services/api")
            .replace('agent = "codex"', 'agent = "noop"')
            .replace("auto = false", "auto = true"),
            encoding="utf-8",
        )
        task_path.joinpath("handoff.md").write_text("# handoff\n", encoding="utf-8")
        task_path.joinpath("receipt.md").write_text("result: DONE\n", encoding="utf-8")
        task = load_task(config, "TASK-AUTO")
        self.assertEqual(run_task(config, task), "review")
        self._write_bound_review(task_path)
        line_repository = self.root / "versions/alpha/services/api"
        line_repository.joinpath("DIRTY.txt").write_text("dirty\n", encoding="utf-8")

        with self.assertRaisesRegex(DyroError, "开发线仓库不干净"):
            review_task(config, task)
        self.assertEqual(status(config, task), "review")

    def test_cross_repository_merge_rolls_back_when_later_repository_conflicts(self) -> None:
        web_anchor = self.root / "repositories/web"
        web_anchor.mkdir(parents=True)
        shell("git", "init", "-b", "main", cwd=web_anchor)
        shell("git", "config", "user.name", "Test User", cwd=web_anchor)
        shell("git", "config", "user.email", "test@example.com", cwd=web_anchor)
        web_anchor.joinpath("README.md").write_text("anchor\n", encoding="utf-8")
        shell("git", "add", "README.md", cwd=web_anchor)
        shell("git", "commit", "-m", "chore: initial", cwd=web_anchor)
        config_path = self.root / "dyro.toml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8")
            + '\n[repositories.web]\npath = "repositories/web"\nmount = "services/web"\nverify = [["git", "diff", "--check"]]\n',
            encoding="utf-8",
        )
        config = load(self.root)
        create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        task_path = config.task_specs_dir / "TASK-TXN"
        task_path.mkdir(parents=True)
        manifest = task_template("TASK-TXN", "transactional merge", "alpha", "api", "services/api").replace(
            'agent = "codex"', 'agent = "noop"'
        )
        manifest = manifest.replace('[[gates]]', '[[repositories]]\nid = "web"\n\n[[gates]]', 1)
        task_path.joinpath("task.toml").write_text(manifest, encoding="utf-8")
        task_path.joinpath("handoff.md").write_text("# handoff\n", encoding="utf-8")
        task_path.joinpath("receipt.md").write_text("result: QUESTION\n", encoding="utf-8")
        task = load_task(config, "TASK-TXN")
        self.assertEqual(run_task(config, task), "waiting_answer")

        for repository, content in (("api", "task api\n"), ("web", "task web\n")):
            task_repository = self.root / f"worktrees/alpha/TASK-TXN/services/{repository}"
            task_repository.joinpath("README.md").write_text(content, encoding="utf-8")
            shell("git", "add", "README.md", cwd=task_repository)
            shell("git", "commit", "-m", f"feat: update {repository}", cwd=task_repository)
        task_path.joinpath("receipt.md").write_text("result: DONE\n", encoding="utf-8")
        self.assertEqual(answer_task(config, task, "continue"), "review")
        self._write_bound_review(task_path)
        self.assertEqual(review_task(config, task), "done")

        line_api = self.root / "versions/alpha/services/api"
        line_web = self.root / "versions/alpha/services/web"
        original_api_head = subprocess_output("git", "rev-parse", "HEAD", cwd=line_api)
        line_web.joinpath("README.md").write_text("line web\n", encoding="utf-8")
        shell("git", "add", "README.md", cwd=line_web)
        shell("git", "commit", "-m", "feat: conflicting line update", cwd=line_web)

        with self.assertRaisesRegex(DyroError, "合并 web 失败"):
            merge_task(config, task)
        self.assertEqual(subprocess_output("git", "rev-parse", "HEAD", cwd=line_api), original_api_head)
        self.assertEqual(subprocess_output("git", "status", "--porcelain=v1", "-uall", cwd=line_api), "")
        self.assertEqual(subprocess_output("git", "status", "--porcelain=v1", "-uall", cwd=line_web), "")


def subprocess_output(*args: str, cwd: Path) -> str:
    import subprocess

    return subprocess.run(args, cwd=cwd, check=True, text=True, stdout=subprocess.PIPE).stdout.strip()

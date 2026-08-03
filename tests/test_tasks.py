import hashlib
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import zipfile

from dyro.config import ValidationError, load
from dyro.evidence import build_execution_bundle, unpack_execution_bundle
from dyro.errors import DyroError
from dyro.reviews import build_signed_review_record
from dyro.signing import generate_keypair, sign_record, trust_public_key
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
    def _external_config(self):
        config_path = self.root / "dyro.toml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "require_clean_merge = true",
                "\n".join(
                    (
                        "require_clean_merge = true",
                        'execution_mode = "external"',
                        "require_signed_execution = true",
                        "require_signed_review = true",
                    )
                ),
            ),
            encoding="utf-8",
        )
        return load(self.root)

    def _trusted_key(self, config, key_id: str, purpose: str, principal: str) -> Path:
        secure = self.root / "secure"
        private_key = secure / f"{key_id}.private.pem"
        public_key = secure / f"{key_id}.public.pem"
        generate_keypair(key_id, private_key=private_key, public_key=public_key)
        trust_public_key(
            config.root,
            key_id,
            purpose=purpose,
            source=public_key,
            principal_id=principal,
        )
        return private_key

    @staticmethod
    def _write_bound_review(task_path: Path) -> None:
        from dyro.provenance import review_binding

        receipt_hash = hashlib.sha256(task_path.joinpath("receipt.md").read_bytes()).hexdigest()
        heads_hash = hashlib.sha256(task_path.joinpath("task-heads.json").read_bytes()).hexdigest()
        binding = review_binding(task_path)
        provenance = (
            f"attempt_id: {binding[0]}\nplan_sha256: {binding[1]}\n"
            if binding is not None
            else ""
        )
        task_path.joinpath("review.md").write_text(
            f"verdict: PASS\nreceipt_sha256: {receipt_hash}\ntask_heads_sha256: {heads_hash}\n{provenance}",
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
        task_path.joinpath("receipt.md").write_text("result: QUESTION\n", encoding="utf-8")
        task = load_task(config, "TASK-1")
        self.assertEqual(run_task(config, task), "waiting_answer")
        task_repository = self.root / "worktrees/alpha/TASK-1/services/api"
        task_repository.joinpath("README.md").write_text("task change\n", encoding="utf-8")
        shell("git", "add", "README.md", cwd=task_repository)
        shell("git", "commit", "-m", "feat: task change", cwd=task_repository)
        task_path.joinpath("receipt.md").write_text("result: DONE\n", encoding="utf-8")
        self.assertEqual(answer_task(config, task, "continue"), "review")
        self.assertEqual(status(config, task), "review")
        self._write_bound_review(task_path)
        self.assertEqual(review_task(config, task), "done")
        self.assertEqual(status(config, task), "done")
        merge_task(config, task)
        line_repository = self.root / "versions/alpha/services/api"
        first_merge_head = subprocess_output("git", "rev-parse", "HEAD", cwd=line_repository)
        self.assertNotEqual(first_merge_head, subprocess_output("git", "rev-parse", "HEAD", cwd=task_repository))

        # A CI retry or a second explicit request must not create another merge.
        merge_task(config, task)
        self.assertEqual(
            subprocess_output("git", "rev-parse", "HEAD", cwd=line_repository),
            first_merge_head,
        )

    def test_public_status_cannot_bypass_review_or_merge(self) -> None:
        config = load(self.root)
        create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        task_path = config.task_specs_dir / "TASK-REVIEW-GATE"
        task_path.mkdir(parents=True)
        task_path.joinpath("task.toml").write_text(
            task_template("TASK-REVIEW-GATE", "review gate", "alpha", "api", "services/api").replace('agent = "codex"', 'agent = "noop"'),
            encoding="utf-8",
        )
        task_path.joinpath("handoff.md").write_text("# handoff\n", encoding="utf-8")
        task_path.joinpath("receipt.md").write_text("result: DONE\n", encoding="utf-8")
        task = load_task(config, "TASK-REVIEW-GATE")
        self.assertEqual(run_task(config, task), "review")

        with self.assertRaisesRegex(DyroError, "质量门"):
            set_status(config, task, "done")
        with self.assertRaisesRegex(DyroError, "仅 done"):
            merge_task(config, task)
        self.assertEqual(status(config, task), "review")

    def test_merge_revalidates_accepted_review_binding(self) -> None:
        config = load(self.root)
        create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        task_path = config.task_specs_dir / "TASK-MERGE-RECHECK"
        task_path.mkdir(parents=True)
        task_path.joinpath("task.toml").write_text(
            task_template("TASK-MERGE-RECHECK", "merge recheck", "alpha", "api", "services/api").replace('agent = "codex"', 'agent = "noop"'),
            encoding="utf-8",
        )
        task_path.joinpath("handoff.md").write_text("# handoff\n", encoding="utf-8")
        task_path.joinpath("receipt.md").write_text("result: DONE\n", encoding="utf-8")
        task = load_task(config, "TASK-MERGE-RECHECK")
        self.assertEqual(run_task(config, task), "review")
        self._write_bound_review(task_path)
        self.assertEqual(review_task(config, task), "done")
        task_path.joinpath("review.md").write_text("verdict: PASS\n", encoding="utf-8")

        with self.assertRaisesRegex(DyroError, "有效的独立复核"):
            merge_task(config, task)

    def test_execution_exception_marks_current_task_failed_for_retry(self) -> None:
        config_path = self.root / "dyro.toml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                'write = ["/usr/bin/true"]', 'write = ["definitely-missing-dyro-agent"]'
            ),
            encoding="utf-8",
        )
        config = load(self.root)
        create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        task_path = config.task_specs_dir / "TASK-RETRY"
        task_path.mkdir(parents=True)
        task_path.joinpath("task.toml").write_text(
            task_template("TASK-RETRY", "retryable failure", "alpha", "api", "services/api").replace('agent = "codex"', 'agent = "noop"'),
            encoding="utf-8",
        )
        task_path.joinpath("handoff.md").write_text("# handoff\n", encoding="utf-8")
        task = load_task(config, "TASK-RETRY")

        with self.assertRaisesRegex(DyroError, "找不到可执行命令"):
            run_task(config, task)
        self.assertEqual(status(config, task), "failed")
        attempt = next(task_path.joinpath("attempts").glob("*.json"))
        self.assertEqual(json.loads(attempt.read_text(encoding="utf-8"))["status"], "failed")

    def test_task_manifest_rejects_non_positive_or_non_integer_timeouts(self) -> None:
        config = load(self.root)
        create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        cases = {
            "timeout_minutes = true": "timeout_minutes",
            'review_timeout_minutes = "0"': "review_timeout_minutes",
            "timeout_seconds = -1": "timeout_seconds",
        }
        for replacement, expected in cases.items():
            with self.subTest(replacement=replacement):
                task_id = f"TASK-TIMEOUT-{len(replacement)}"
                task_path = config.task_specs_dir / task_id
                task_path.mkdir(parents=True, exist_ok=True)
                manifest = task_template(task_id, "timeout validation", "alpha", "api", "services/api")
                field = replacement.split(" =", 1)[0]
                if field == "timeout_seconds":
                    manifest = manifest.replace("timeout_seconds = 120", replacement)
                else:
                    manifest = manifest.replace(f"{field} = 60" if field == "timeout_minutes" else "review_timeout_minutes = 45", replacement)
                task_path.joinpath("task.toml").write_text(
                    manifest.replace('agent = "codex"', 'agent = "noop"'),
                    encoding="utf-8",
                )
                task_path.joinpath("handoff.md").write_text("# handoff\n", encoding="utf-8")
                with self.assertRaisesRegex(ValidationError, expected):
                    load_task(config, task_id)

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
        with self.assertRaisesRegex(DyroError, "质量门"):
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

    def test_legacy_external_profile_cannot_claim_until_signed_identity_is_enabled(self) -> None:
        config_path = self.root / "dyro.toml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "require_clean_merge = true", "require_clean_merge = true\nexecution_mode = \"external\""
            ),
            encoding="utf-8",
        )
        config = load(self.root)
        create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        task_path = config.task_specs_dir / "TASK-LEGACY-EXTERNAL"
        task_path.mkdir(parents=True)
        task_path.joinpath("task.toml").write_text(
            task_template("TASK-LEGACY-EXTERNAL", "requires signed identity", "alpha", "api", "services/api").replace(
                'agent = "codex"', 'agent = "noop"'
            ),
            encoding="utf-8",
        )
        task_path.joinpath("handoff.md").write_text("# handoff\n", encoding="utf-8")

        with self.assertRaisesRegex(DyroError, "身份边界尚未迁移"):
            claim_task(config, load_task(config, "TASK-LEGACY-EXTERNAL"), runner="runner")

    def test_external_runner_rejects_unsigned_execution_evidence(self) -> None:
        config = self._external_config()
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
        receipt.write_text("result: QUESTION\n", encoding="utf-8")
        self._trusted_key(config, "runner-import", "execution", "isolated-runner-1")

        self.assertEqual(
            claim_task(
                config,
                task,
                runner="isolated-runner-1",
                key_id="runner-import",
            ),
            "assigned",
        )
        with self.assertRaisesRegex(ValidationError, "策略要求"):
            import_execution_evidence(
                config,
                task,
                receipt=receipt,
                allow_legacy_provenance=True,
            )

    def test_external_review_rejects_execution_principal_with_a_different_key(self) -> None:
        config = self._external_config()
        create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        task_path = config.task_specs_dir / "TASK-SELF-REVIEW"
        task_path.mkdir(parents=True)
        task_path.joinpath("task.toml").write_text(
            task_template("TASK-SELF-REVIEW", "reject self review", "alpha", "api", "services/api").replace(
                'agent = "codex"', 'agent = "noop"'
            ),
            encoding="utf-8",
        )
        task_path.joinpath("handoff.md").write_text("# handoff\n", encoding="utf-8")
        task = load_task(config, "TASK-SELF-REVIEW")
        self._trusted_key(config, "runner-self-review", "execution", "shared-principal")
        reviewer_private = self._trusted_key(
            config,
            "reviewer-self-review",
            "review",
            "shared-principal",
        )
        self.assertEqual(
            claim_task(
                config,
                task,
                runner="shared-principal",
                key_id="runner-self-review",
            ),
            "assigned",
        )
        task_path.joinpath("status").write_text("review\n", encoding="utf-8")
        record = build_signed_review_record(
            task.id,
            reviewer="shared-principal",
            review_content=b"verdict: FAIL\n",
            signing_key=reviewer_private,
            key_id="reviewer-self-review",
        )
        review_path = self.root / "self-review.json"
        review_path.write_text(json.dumps(record), encoding="utf-8")

        with self.assertRaisesRegex(ValidationError, "不得复核自己的结果"):
            import_review_evidence(config, task, review=review_path)

    def test_external_claim_is_serialized_for_two_local_process_threads(self) -> None:
        config = self._external_config()
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
        for runner in ("runner-a", "runner-b"):
            self._trusted_key(config, f"{runner}-key", "execution", runner)

        def claim(runner: str) -> str:
            try:
                return claim_task(config, task, runner=runner, key_id=f"{runner}-key")
            except DyroError:
                return "rejected"

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(claim, ("runner-a", "runner-b")))
        self.assertEqual(sorted(outcomes), ["assigned", "rejected"])
        self.assertEqual(status(config, task), "assigned")

    def test_external_claim_blocks_another_task_in_the_same_conflict_group(self) -> None:
        config = self._external_config()
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

        self._trusted_key(config, "runner-a-key", "execution", "runner-a")
        self._trusted_key(config, "runner-b-key", "execution", "runner-b")
        self.assertEqual(
            claim_task(
                config,
                load_task(config, "TASK-GROUP-A"),
                runner="runner-a",
                key_id="runner-a-key",
            ),
            "assigned",
        )
        with self.assertRaisesRegex(DyroError, "活跃任务"):
            claim_task(
                config,
                load_task(config, "TASK-GROUP-B"),
                runner="runner-b",
                key_id="runner-b-key",
            )

    def test_external_runner_builds_and_imports_a_portable_evidence_bundle(self) -> None:
        config = self._external_config()
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
        runner_private = self._trusted_key(
            config,
            "runner-bundle",
            "execution",
            "isolated-runner-1",
        )
        self.assertEqual(
            claim_task(
                config,
                task,
                runner="isolated-runner-1",
                key_id="runner-bundle",
            ),
            "assigned",
        )

        result = build_execution_bundle(
            config,
            task,
            workspace=workspace,
            receipt=receipt,
            output=bundle,
            signing_key=runner_private,
            key_id="runner-bundle",
            claim=task_path / "claim.json",
        )
        self.assertEqual(result.result, "DONE")
        self.assertTrue(result.gates_passed)
        self.assertTrue(bundle.is_file())

        escaped_bundle = self.root / "escaped-execution.zip"
        symlink_bundle = self.root / "symlink-execution.zip"
        symlink_bundle.symlink_to(escaped_bundle)
        with self.assertRaisesRegex(DyroError, "拒绝覆盖"):
            build_execution_bundle(
                config,
                task,
                workspace=workspace,
                receipt=receipt,
                output=symlink_bundle,
            )
        self.assertTrue(symlink_bundle.is_symlink())
        self.assertFalse(escaped_bundle.exists())

        with unpack_execution_bundle(bundle) as evidence:
            self.assertEqual(
                import_execution_evidence(
                    config,
                    task,
                    receipt=evidence["receipt"],
                    gates=evidence["gates"],
                    heads=evidence["heads"],
                    provenance=evidence["provenance"],
                ),
                "review",
            )
        self.assertEqual(status(config, task), "review")

    def test_external_question_can_be_answered_by_the_claimed_runner(self) -> None:
        config = self._external_config()
        create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        task_path = config.task_specs_dir / "TASK-QUESTION"
        task_path.mkdir(parents=True)
        task_path.joinpath("task.toml").write_text(
            task_template("TASK-QUESTION", "external question", "alpha", "api", "services/api").replace(
                'agent = "codex"', 'agent = "noop"'
            ),
            encoding="utf-8",
        )
        task_path.joinpath("handoff.md").write_text("# handoff\n", encoding="utf-8")
        task = load_task(config, "TASK-QUESTION")
        receipt = self.root / "question-receipt.md"
        receipt.write_text("result: QUESTION\n", encoding="utf-8")
        runner_private = self._trusted_key(
            config,
            "runner-question",
            "execution",
            "isolated-runner-1",
        )

        self.assertEqual(
            claim_task(
                config,
                task,
                runner="isolated-runner-1",
                key_id="runner-question",
            ),
            "assigned",
        )
        from dyro.provenance import build_external_attempt_record, external_execution_plan
        from dyro.tasks import execution_claim_binding

        provenance = build_external_attempt_record(
            task_path,
            task.id,
            external_execution_plan(
                task,
                config.policy.execution_mode,
                claim_binding=execution_claim_binding(task),
            ),
            result="QUESTION",
            receipt_sha256=hashlib.sha256(receipt.read_bytes()).hexdigest(),
        )
        provenance["actor"] = "isolated-runner-1"
        signed_provenance = sign_record(
            provenance,
            purpose="execution",
            key_id="runner-question",
            private_key=runner_private,
        )
        provenance_path = self.root / "question-provenance.json"
        provenance_path.write_text(json.dumps(signed_provenance), encoding="utf-8")
        self.assertEqual(
            import_execution_evidence(
                config,
                task,
                receipt=receipt,
                provenance=provenance_path,
            ),
            "waiting_answer",
        )
        self.assertEqual(answer_task(config, task, "Use the stable API contract."), "assigned")
        self.assertEqual(status(config, task), "assigned")
        self.assertEqual(
            task_path.joinpath("answers.md").read_text(encoding="utf-8"),
            "Use the stable API contract.\n",
        )

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

    def test_pass_review_never_runs_implicit_merge_or_push(self) -> None:
        config = load(self.root)
        create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        task_path = config.task_specs_dir / "TASK-AUTO"
        task_path.mkdir(parents=True)
        task_path.joinpath("task.toml").write_text(
            task_template("TASK-AUTO", "safe auto merge", "alpha", "api", "services/api")
            .replace('agent = "codex"', 'agent = "noop"')
            .replace("auto = false", "auto = true")
            .replace("push = false", "push = true"),
            encoding="utf-8",
        )
        task_path.joinpath("handoff.md").write_text("# handoff\n", encoding="utf-8")
        task_path.joinpath("receipt.md").write_text("result: DONE\n", encoding="utf-8")
        task = load_task(config, "TASK-AUTO")
        self.assertEqual(run_task(config, task), "review")
        self._write_bound_review(task_path)

        with patch("dyro.tasks._merge_task_repositories") as merge_repositories:
            self.assertEqual(review_task(config, task), "done")

        merge_repositories.assert_not_called()
        self.assertEqual(status(config, task), "done")

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

    def test_merge_serializes_on_delivery_line_lock(self) -> None:
        import dyro.tasks as tasks_mod
        from dyro.state import exclusive_lock
        from dyro.tasks import MERGE_LOCK_TIMEOUT_SECONDS, _merge_lock_path

        config = load(self.root)
        create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        task_path = config.task_specs_dir / "TASK-LOCK"
        task_path.mkdir(parents=True)
        task_path.joinpath("task.toml").write_text(
            task_template("TASK-LOCK", "merge lock", "alpha", "api", "services/api").replace(
                'agent = "codex"', 'agent = "noop"'
            ),
            encoding="utf-8",
        )
        task_path.joinpath("handoff.md").write_text("# handoff\n", encoding="utf-8")
        task_path.joinpath("receipt.md").write_text("result: DONE\n", encoding="utf-8")
        task = load_task(config, "TASK-LOCK")
        self.assertEqual(run_task(config, task), "review")
        self._write_bound_review(task_path)
        self.assertEqual(review_task(config, task), "done")

        lock_path = _merge_lock_path(config, task.line)
        errors: list[str] = []
        previous_timeout = tasks_mod.MERGE_LOCK_TIMEOUT_SECONDS
        tasks_mod.MERGE_LOCK_TIMEOUT_SECONDS = 0.4

        def blocked_merge() -> None:
            try:
                merge_task(config, task)
            except DyroError as exc:
                errors.append(str(exc))

        try:
            with exclusive_lock(lock_path, timeout_seconds=5.0):
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(blocked_merge)
                    future.result(timeout=5)
        finally:
            tasks_mod.MERGE_LOCK_TIMEOUT_SECONDS = previous_timeout

        self.assertTrue(errors)
        self.assertTrue(any("等待状态锁超时" in item for item in errors), errors)
        self.assertEqual(MERGE_LOCK_TIMEOUT_SECONDS, 1800.0)


def subprocess_output(*args: str, cwd: Path) -> str:
    import subprocess

    return subprocess.run(args, cwd=cwd, check=True, text=True, stdout=subprocess.PIPE).stdout.strip()

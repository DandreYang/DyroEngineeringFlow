"""End-to-end Stage5 pack -> signed Core evidence -> review binding."""

from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import io
from pathlib import Path
import tempfile
import unittest

from dyro.cli import main
from dyro.config import load
from dyro.evidence_store import resolve_evidence_path
from dyro.provenance import review_binding
from dyro.tasks import load_task, status, task_template
from dyro.workspace import create_line
from experiments.external_workflow_runner.stage4.evidence_pack import (
    CleanupProof,
    pack_run_evidence,
)
from experiments.external_workflow_runner.stage5.core_handoff import (
    build_core_evidence_handoff,
    stage5_claim_from_core,
)

from .support import WorkspaceCase, shell


class RuntimeCoreHandoffIntegrationTests(WorkspaceCase):
    def _cli(self, *arguments: str) -> None:
        with redirect_stdout(io.StringIO()):
            main(["--root", str(self.root), *arguments])

    def test_signed_handoff_reaches_independent_review_binding(self) -> None:
        config_path = self.root / "dyro.toml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "require_clean_merge = true",
                "\n".join(
                    (
                        "require_clean_merge = true",
                        'execution_mode = "external"',
                        "require_signed_execution = true",
                    )
                ),
            ),
            encoding="utf-8",
        )
        config = load(self.root)
        create_line(
            config,
            line_id="alpha",
            branch="feat/alpha",
            base="main",
        )
        task_id = "TASK-RUNTIME-HANDOFF"
        task_path = config.task_specs_dir / task_id
        task_path.mkdir(parents=True)
        task_path.joinpath("task.toml").write_text(
            task_template(
                task_id,
                "runtime handoff",
                "alpha",
                "api",
                "services/api",
            ).replace('agent = "codex"', 'agent = "noop"'),
            encoding="utf-8",
        )
        task_path.joinpath("handoff.md").write_text(
            "# runtime handoff\n",
            encoding="utf-8",
        )

        secure_directory = tempfile.TemporaryDirectory(
            prefix="dyro-runtime-keys-"
        )
        self.addCleanup(secure_directory.cleanup)
        secure = Path(secure_directory.name)
        runner_private = secure / "runner.private.pem"
        runner_public = secure / "runner.public.pem"
        self._cli(
            "key",
            "generate",
            "runner-runtime",
            "--private-key",
            str(runner_private),
            "--public-key",
            str(runner_public),
        )
        self._cli(
            "key",
            "trust",
            "runner-runtime",
            "--purpose",
            "execution",
            "--public-key",
            str(runner_public),
            "--not-after",
            "2999-01-01T00:00:00+00:00",
        )
        core_claim = self.root / "runner-inbox" / "core-claim.json"
        self._cli(
            "task",
            "claim",
            task_id,
            "--by",
            "stage5-runner",
            "--key-id",
            "runner-runtime",
            "--output",
            str(core_claim),
        )
        self.assertTrue(core_claim.is_file())
        self.assertEqual(core_claim.stat().st_mode & 0o777, 0o600)
        stage5_claim = stage5_claim_from_core(core_claim)

        runner_workspace = self.root / "isolated-runner"
        repository = runner_workspace / "services/api"
        repository.parent.mkdir(parents=True)
        shell(
            "git",
            "clone",
            str(self.anchor),
            str(repository),
            cwd=self.root,
        )
        shell("git", "config", "user.name", "Test User", cwd=repository)
        shell("git", "config", "user.email", "test@example.com", cwd=repository)
        shell(
            "git",
            "checkout",
            "-b",
            f"task/{task_id}",
            cwd=repository,
        )
        artifact = repository / "runtime-report.md"
        artifact.write_text(
            "# Stage5 runtime report\n\nfixed workflow completed\n",
            encoding="utf-8",
        )
        shell("git", "add", "runtime-report.md", cwd=repository)
        shell(
            "git",
            "commit",
            "-m",
            "test: add runtime artifact",
            cwd=repository,
        )
        artifact_sha = hashlib.sha256(artifact.read_bytes()).hexdigest()
        workflow_run_id = "runtime-handoff-run-1"
        pack = pack_run_evidence(
            pack_root=self.root / "stage5-pack",
            workflow_run_id=workflow_run_id,
            claim=stage5_claim.to_mapping(),
            canonical_input_sha256="c" * 64,
            envelope={
                "status": "DONE",
                "workflow_run_id": workflow_run_id,
                "artifacts": [
                    {
                        "repository": "api",
                        "path": "runtime-report.md",
                        "sha256": artifact_sha,
                    }
                ],
            },
            artifact_paths=(
                ("api", "runtime-report.md", artifact),
            ),
            provider_pin={
                "source": "host",
                "content_sha256": "d" * 64,
            },
            claim_matrix={"total_ms": 1},
            cleanup=CleanupProof(
                sandbox_cleanup_verified=True,
                broker_cleanup_verified=True,
                broker_containers_absent=True,
                raw_marker_leaked=False,
                provider_token_leaked=False,
            ),
            mid_run_renewals=0,
            provider_mode="argv-cli",
        )

        core_bundle = self.root / "runtime-core-evidence.zip"
        dry_run_bundle = self.root / "runtime-core-evidence-dry-run.zip"
        dry_run_handoff = build_core_evidence_handoff(
            root=self.root,
            task_id=task_id,
            pack_dir=pack.pack_dir,
            workspace=runner_workspace,
            core_claim=core_claim,
            output=dry_run_bundle,
            signing_key=runner_private,
            key_id="runner-runtime",
            dry_run=True,
        )
        self.assertEqual(dry_run_handoff["verdict"], "DRY_RUN")
        self.assertFalse(dry_run_bundle.exists())
        self.assertFalse(dry_run_handoff["gates_executed"])
        self.assertIsNone(dry_run_handoff["gates_passed"])
        self.assertFalse(dry_run_handoff["workspace_heads_verified"])
        self.assertFalse(dry_run_handoff["signature_created"])
        self.assertFalse(dry_run_handoff["ready_for_core_import"])
        self.assertEqual(
            dry_run_handoff["next_command"],
            "dyro runtime handoff --help",
        )

        handoff = build_core_evidence_handoff(
            root=self.root,
            task_id=task_id,
            pack_dir=pack.pack_dir,
            workspace=runner_workspace,
            core_claim=core_claim,
            output=core_bundle,
            signing_key=runner_private,
            key_id="runner-runtime",
        )
        self.assertEqual(handoff["verdict"], "BUILT")
        self.assertTrue(core_bundle.is_file())
        self.assertTrue(handoff["gates_executed"])
        self.assertTrue(handoff["gates_passed"])
        self.assertTrue(handoff["workspace_heads_verified"])
        self.assertTrue(handoff["signature_created"])
        self.assertTrue(handoff["ready_for_core_import"])
        self.assertFalse(handoff["core_import_attempted"])
        self.assertFalse(handoff["review_attempted"])
        self.assertFalse(handoff["merge_attempted"])
        self._cli(
            "task",
            "evidence",
            "execution",
            task_id,
            "--bundle",
            str(core_bundle),
        )

        config = load(self.root)
        task = load_task(config, task_id)
        self.assertEqual(status(config, task), "review")
        receipt = resolve_evidence_path(
            task_path,
            "receipt.md",
        ).read_text(encoding="utf-8")
        self.assertIn(
            f"runtime_pack_sha256: {pack.pack_sha256}",
            receipt,
        )
        receipt_hash = hashlib.sha256(
            resolve_evidence_path(task_path, "receipt.md").read_bytes()
        ).hexdigest()
        heads_hash = hashlib.sha256(
            resolve_evidence_path(
                task_path,
                "task-heads.json",
            ).read_bytes()
        ).hexdigest()
        binding = review_binding(task_path)
        self.assertIsNotNone(binding)
        review = self.root / "review.md"
        review.write_text(
            "\n".join(
                (
                    "verdict: PASS",
                    f"receipt_sha256: {receipt_hash}",
                    f"task_heads_sha256: {heads_hash}",
                    f"attempt_id: {binding[0]}",
                    f"plan_sha256: {binding[1]}",
                    "",
                )
            ),
            encoding="utf-8",
        )
        self._cli(
            "task",
            "evidence",
            "review",
            task_id,
            "--file",
            str(review),
        )
        self.assertEqual(
            status(load(self.root), load_task(load(self.root), task_id)),
            "done",
        )


if __name__ == "__main__":
    unittest.main()

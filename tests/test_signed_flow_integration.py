from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import unittest

from dyro.cli import main
from dyro.config import load
from dyro.evidence_store import resolve_evidence_path
from dyro.provenance import review_binding
from dyro.tasks import load_task, status, task_template
from dyro.workspace import create_line

from .support import WorkspaceCase, shell


class SignedExternalCliFlowTests(WorkspaceCase):
    def _cli(self, *arguments: str) -> None:
        with redirect_stdout(io.StringIO()):
            main(["--root", str(self.root), *arguments])

    def _generate_and_trust(self, key_id: str, purpose: str, secure: Path) -> tuple[Path, Path]:
        private_key = secure / f"{key_id}.private.pem"
        public_key = secure / f"{key_id}.public.pem"
        self._cli(
            "key",
            "generate",
            key_id,
            "--private-key",
            str(private_key),
            "--public-key",
            str(public_key),
        )
        self._cli(
            "key",
            "trust",
            key_id,
            "--purpose",
            purpose,
            "--public-key",
            str(public_key),
            "--not-after",
            "2999-01-01T00:00:00+00:00",
        )
        return private_key, public_key

    def test_signed_execution_review_and_signoff_complete_through_cli(self) -> None:
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
                        "require_external_signoff = true",
                        "require_signed_signoff = true",
                    )
                ),
            ),
            encoding="utf-8",
        )
        config = load(self.root)
        create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        task_path = config.task_specs_dir / "TASK-SIGNED-E2E"
        task_path.mkdir(parents=True)
        task_path.joinpath("task.toml").write_text(
            task_template(
                "TASK-SIGNED-E2E",
                "signed external CLI flow",
                "alpha",
                "api",
                "services/api",
            ).replace('agent = "codex"', 'agent = "noop"'),
            encoding="utf-8",
        )
        task_path.joinpath("handoff.md").write_text("# signed flow\n", encoding="utf-8")

        secure = self.root / "secure"
        runner_private, _ = self._generate_and_trust("runner-e2e", "execution", secure)
        reviewer_private, _ = self._generate_and_trust("reviewer-e2e", "review", secure)
        approver_private, _ = self._generate_and_trust("approver-e2e", "signoff", secure)

        self._cli(
            "task",
            "claim",
            "TASK-SIGNED-E2E",
            "--by",
            "isolated-runner",
            "--key-id",
            "runner-e2e",
        )

        runner_workspace = self.root / "isolated-runner"
        repository = runner_workspace / "services/api"
        repository.parent.mkdir(parents=True)
        shell("git", "clone", str(self.anchor), str(repository), cwd=self.root)
        shell("git", "checkout", "-b", "task/TASK-SIGNED-E2E", cwd=repository)
        receipt = runner_workspace / "receipt.md"
        receipt.write_text("result: DONE\n", encoding="utf-8")
        bundle = self.root / "signed-execution.zip"
        self._cli(
            "task",
            "evidence",
            "build",
            "TASK-SIGNED-E2E",
            "--workspace",
            str(runner_workspace),
            "--receipt",
            str(receipt),
            "--output",
            str(bundle),
            "--claim",
            str(task_path / "claim.json"),
            "--signing-key",
            str(runner_private),
            "--key-id",
            "runner-e2e",
        )
        self._cli(
            "task",
            "evidence",
            "execution",
            "TASK-SIGNED-E2E",
            "--bundle",
            str(bundle),
        )

        receipt_hash = hashlib.sha256(
            resolve_evidence_path(task_path, "receipt.md").read_bytes()
        ).hexdigest()
        heads_hash = hashlib.sha256(
            resolve_evidence_path(task_path, "task-heads.json").read_bytes()
        ).hexdigest()
        binding = review_binding(task_path)
        self.assertIsNotNone(binding)
        review_text = self.root / "review.md"
        review_text.write_text(
            f"verdict: PASS\nreceipt_sha256: {receipt_hash}\n"
            f"task_heads_sha256: {heads_hash}\nattempt_id: {binding[0]}\n"
            f"plan_sha256: {binding[1]}\n",
            encoding="utf-8",
        )
        signed_review = self.root / "signed-review.json"
        self._cli(
            "task",
            "evidence",
            "review-build",
            "TASK-SIGNED-E2E",
            "--file",
            str(review_text),
            "--reviewer",
            "independent-reviewer",
            "--output",
            str(signed_review),
            "--signing-key",
            str(reviewer_private),
            "--key-id",
            "reviewer-e2e",
        )
        self._cli(
            "task",
            "evidence",
            "review",
            "TASK-SIGNED-E2E",
            "--file",
            str(signed_review),
        )
        self._cli(
            "task",
            "signoff",
            "TASK-SIGNED-E2E",
            "--by",
            "release-manager",
            "--signing-key",
            str(approver_private),
            "--key-id",
            "approver-e2e",
        )

        final_config = load(self.root)
        task = load_task(final_config, "TASK-SIGNED-E2E")
        self.assertEqual(status(final_config, task), "done")
        signoff = json.loads((task_path / "signoff.json").read_text(encoding="utf-8"))
        self.assertEqual(signoff["signature"]["key_id"], "approver-e2e")


if __name__ == "__main__":
    unittest.main()

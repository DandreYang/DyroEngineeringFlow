from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dyro.config import load
from dyro.continuation.actions import (
    ActionIntent,
    ActionReceipt,
    ActionStatus,
    acquire_owner_lease,
    record_action_receipt,
    reserve_action,
    start_action,
)
from dyro.continuation.budgets import BudgetReservation
from dyro.continuation.models import ActionKind
from dyro.continuation.objective_storage import open_objective_directory
from dyro.continuation.store import create_objective
from dyro.evidence_store import publish_evidence_generation
from dyro.proof.decay import NEXT_PROBE_AT
from dyro.proof.derive import (
    derive_objective_proofs,
    derive_task_proofs,
    derive_trigger_proofs,
    list_proofs,
)
from dyro.proof.evaluate import evaluate_proofs
from dyro.proof.models import ProofKind, ProofStatus
from dyro.provenance import review_binding
from dyro.tasks import answer_task, load_task, review_task, run_task, task_template
from dyro.workspace import create_line

from .support import WorkspaceCase, shell


def _write_bound_review(task_path: Path) -> None:
    receipt_hash = hashlib.sha256(task_path.joinpath("receipt.md").read_bytes()).hexdigest()
    heads_hash = hashlib.sha256(task_path.joinpath("task-heads.json").read_bytes()).hexdigest()
    binding = review_binding(task_path)
    provenance = (
        f"attempt_id: {binding[0]}\nplan_sha256: {binding[1]}\n" if binding is not None else ""
    )
    task_path.joinpath("review.md").write_text(
        f"verdict: PASS\nreceipt_sha256: {receipt_hash}\ntask_heads_sha256: {heads_hash}\n{provenance}",
        encoding="utf-8",
    )


def _objective_contract(objective_id: str, target: str) -> str:
    return f'''schema_version = 1
id = "{objective_id}"
title = "Objective {objective_id}"
line = "alpha"
targets = ["{target}"]

[continuation]
requested_mode = "supervised"
operations = ["execute", "review"]
'''


class ProofDeriveTests(WorkspaceCase):
    def _reviewed_task(self, task_id: str = "TASK-PROOF"):
        config = load(self.root)
        create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        task_path = config.task_specs_dir / task_id
        task_path.mkdir(parents=True)
        task_path.joinpath("task.toml").write_text(
            task_template(task_id, "proof derive", "alpha", "api", "services/api").replace(
                'agent = "codex"', 'agent = "noop"'
            ),
            encoding="utf-8",
        )
        task_path.joinpath("handoff.md").write_text("# handoff\n", encoding="utf-8")
        task_path.joinpath("receipt.md").write_text("result: DONE\n", encoding="utf-8")
        task = load_task(config, task_id)
        self.assertEqual(run_task(config, task), "review")
        _write_bound_review(task_path)
        self.assertEqual(review_task(config, task), "done")
        return config, load_task(config, task_id)

    def test_bound_review_fixture_derives_review_gate_and_integration(self) -> None:
        config, task = self._reviewed_task()
        proofs = derive_task_proofs(config, task)
        kinds = {proof.kind for proof in proofs}
        self.assertIn(ProofKind.GATE_LOG, kinds)
        self.assertIn(ProofKind.REVIEW_VERDICT, kinds)
        self.assertIn(ProofKind.INTEGRATION_HEADS, kinds)
        self.assertNotIn(ProofKind.ACTION_RECEIPT, kinds)
        review = next(proof for proof in proofs if proof.kind is ProofKind.REVIEW_VERDICT)
        self.assertEqual(review.subject, task.id)
        self.assertTrue(review.substrate.attempt_id)
        self.assertTrue(review.substrate.contract_hash)
        self.assertEqual(review.produced_at, "")
        self.assertEqual(review.status, ProofStatus.INCONCLUSIVE)
        gate = next(proof for proof in proofs if proof.kind is ProofKind.GATE_LOG)
        self.assertTrue(dict(gate.substrate.extra).get("argv_sha256"))
        integration = next(proof for proof in proofs if proof.kind is ProofKind.INTEGRATION_HEADS)
        # No extra task commit: recorded heads already sit on the line, so ancestor holds.
        self.assertEqual(dict(integration.substrate.extra)["integration_state"], "integrated")

    def test_unmerged_task_commit_marks_integration_pending(self) -> None:
        config = load(self.root)
        create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        task_path = config.task_specs_dir / "TASK-PENDING"
        task_path.mkdir(parents=True)
        task_path.joinpath("task.toml").write_text(
            task_template("TASK-PENDING", "unmerged heads", "alpha", "api", "services/api").replace(
                'agent = "codex"', 'agent = "noop"'
            ),
            encoding="utf-8",
        )
        task_path.joinpath("handoff.md").write_text("# handoff\n", encoding="utf-8")
        task_path.joinpath("receipt.md").write_text("result: QUESTION\n", encoding="utf-8")
        task = load_task(config, "TASK-PENDING")
        self.assertEqual(run_task(config, task), "waiting_answer")
        repository = self.root / "worktrees/alpha/TASK-PENDING/services/api"
        repository.joinpath("PROOF.md").write_text("drift\n", encoding="utf-8")
        shell("git", "add", "PROOF.md", cwd=repository)
        shell("git", "commit", "-m", "feat: unmerged", cwd=repository)
        task_path.joinpath("receipt.md").write_text("result: DONE\n", encoding="utf-8")
        self.assertEqual(answer_task(config, task, "continue"), "review")
        _write_bound_review(task_path)
        self.assertEqual(review_task(config, task), "done")
        proofs = derive_task_proofs(config, load_task(config, "TASK-PENDING"))
        integration = next(proof for proof in proofs if proof.kind is ProofKind.INTEGRATION_HEADS)
        self.assertEqual(dict(integration.substrate.extra)["integration_state"], "pending")

    def test_p1_never_forges_live(self) -> None:
        config, task = self._reviewed_task("TASK-NO-LIVE")
        for proof in derive_task_proofs(config, task):
            self.assertIsNot(proof.status, ProofStatus.LIVE)

    def test_missing_review_binding_is_inconclusive(self) -> None:
        config, task = self._reviewed_task("TASK-MISSING")
        task.directory.joinpath("review.md").write_text("verdict: PASS\n", encoding="utf-8")
        proofs = derive_task_proofs(config, task)
        review = next(proof for proof in proofs if proof.kind is ProofKind.REVIEW_VERDICT)
        self.assertEqual(review.status, ProofStatus.INCONCLUSIVE)
        self.assertIsNot(review.status, ProofStatus.LIVE)

    def test_identity_stable_across_mtime(self) -> None:
        config, task = self._reviewed_task("TASK-MTIME")
        first = {proof.kind: proof.id for proof in derive_task_proofs(config, task)}
        review = task.directory / "review.md"
        review.touch()
        (task.directory / "receipt.md").touch()
        second = {proof.kind: proof.id for proof in derive_task_proofs(config, task)}
        self.assertEqual(first, second)

    def test_signoff_uses_signed_at_not_mtime(self) -> None:
        config, task = self._reviewed_task("TASK-SIGNOFF")
        signed_at = "2026-08-15T00:00:00+00:00"
        task.directory.joinpath("signoff.json").write_text(
            json.dumps(
                {
                    "task_id": task.id,
                    "approver": "owner",
                    "receipt_sha256": "a" * 64,
                    "task_heads_sha256": "b" * 64,
                    "review_sha256": "c" * 64,
                    "attempt_id": "attempt-1",
                    "plan_sha256": "d" * 64,
                    "signed_at": signed_at,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        proofs = derive_task_proofs(config, task)
        signoff = next(proof for proof in proofs if proof.kind is ProofKind.SIGNOFF)
        self.assertEqual(signoff.produced_at, signed_at)
        before = signoff.id
        task.directory.joinpath("signoff.json").touch()
        again = next(proof for proof in derive_task_proofs(config, task) if proof.kind is ProofKind.SIGNOFF)
        self.assertEqual(again.id, before)

    def test_external_generation_derives_one_gate_log(self) -> None:
        config, task = self._reviewed_task("TASK-EXT-GATE")
        for leftover in task.directory.glob("gate-*.log"):
            leftover.unlink()
        publish_evidence_generation(
            task.directory,
            "attempt-ext",
            {
                "receipt.md": b"result: DONE\n",
                "task-heads.json": task.directory.joinpath("task-heads.json").read_bytes(),
                "gates.json": b'{"schema_version":1,"gates":[]}\n',
                "gates/gate-1.log": b"ok\n",
            },
        )
        proofs = [proof for proof in derive_task_proofs(config, task) if proof.kind is ProofKind.GATE_LOG]
        self.assertEqual(len(proofs), 1)
        self.assertEqual(proofs[0].generation, "attempt-ext")

    def test_task_filter_excludes_action_receipt(self) -> None:
        config, task = self._reviewed_task("TASK-A")
        create_objective(config, _objective_contract("release", "TASK-A"))
        listed = list_proofs(config, task_id="TASK-A")
        self.assertFalse(any(proof.kind is ProofKind.ACTION_RECEIPT for proof in listed))

    def test_objective_path_derives_action_receipt(self) -> None:
        config, _task = self._reviewed_task("TASK-A")
        record = create_objective(config, _objective_contract("release", "TASK-A"))
        now = datetime(2026, 8, 15, 4, 0, tzinfo=timezone.utc)
        with open_objective_directory(config, "release") as directory:
            grant = acquire_owner_lease(
                directory,
                objective_id="release",
                now=now,
                ttl_seconds=30,
                pid=123,
                process_start="boot-1",
                owner_token="1" * 64,
            )
            intent = ActionIntent(
                action_id="action-1",
                objective_id="release",
                objective_revision=record.revision,
                objective_event_seq=record.event_seq,
                objective_event_sha256=record.event_sha256,
                scope_sha256=record.scope_sha256,
                snapshot_sha256="a" * 64,
                plan_sha256="b" * 64,
                operation=ActionKind.EXECUTE_TASK,
                subject_id="TASK-A",
                owner_generation=grant.lease.generation,
                expected_operation_generation=0,
                authority_sha256="c" * 64,
                budget_reservation=BudgetReservation("release", "TASK-A"),
                created_at=now,
            )
            reserve_action(directory, intent)
            start_action(directory, action_id="action-1", grant=grant, now=now + timedelta(seconds=1))
            record_action_receipt(
                directory,
                ActionReceipt(
                    action_id="action-1",
                    idempotency_key=intent.idempotency_key,
                    owner_generation=grant.lease.generation,
                    status=ActionStatus.SUCCEEDED,
                    summary="gate-passed",
                    recorded_at=now + timedelta(seconds=3),
                ),
            )
        proofs = derive_objective_proofs(config, "release")
        self.assertTrue(proofs)
        receipt = proofs[0]
        self.assertEqual(receipt.kind, ProofKind.ACTION_RECEIPT)
        self.assertEqual(receipt.subject, "release")
        self.assertEqual(receipt.substrate.contract_hash, record.contract_sha256)
        self.assertTrue(receipt.produced_at)
        self.assertNotIn(receipt, list_proofs(config, task_id="TASK-A"))

    def test_trigger_file_derives_stable_identity_and_is_excluded_from_task_filter(self) -> None:
        config, _task = self._reviewed_task("TASK-A")
        create_objective(config, _objective_contract("release", "TASK-A"))
        trigger_dir = config.objectives_dir / "release" / "triggers"
        trigger_dir.mkdir(parents=True)
        payload = {
            "schema_version": 1,
            "state": "waiting",
            "summary": "probe later",
            "evidence_ref": "obs-1",
            "next_probe_at": "2026-08-15T00:00:00Z",
            "observed_at": "2026-08-14T00:00:00Z",
        }
        trigger_dir.joinpath("ci-watch.json").write_text(json.dumps(payload), encoding="utf-8")
        first = derive_trigger_proofs(config, objective_id="release")
        second = derive_trigger_proofs(config, objective_id="release")
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].id, second[0].id)
        self.assertEqual(first[0].kind, ProofKind.TRIGGER_OBSERVATION)
        self.assertEqual(first[0].subject, "ci-watch")
        self.assertEqual(dict(first[0].substrate.extra)["next_probe_at"], "2026-08-15T00:00:00Z")
        self.assertFalse(
            any(proof.kind is ProofKind.TRIGGER_OBSERVATION for proof in list_proofs(config, task_id="TASK-A"))
        )
        self.assertTrue(
            any(proof.kind is ProofKind.TRIGGER_OBSERVATION for proof in list_proofs(config, objective_id="release"))
        )
        self.assertTrue(any(proof.kind is ProofKind.TRIGGER_OBSERVATION for proof in list_proofs(config)))
        due = evaluate_proofs(
            config,
            first,
            clock=lambda: datetime(2026, 8, 16, tzinfo=timezone.utc),
        )
        early = evaluate_proofs(
            config,
            first,
            clock=lambda: datetime(2026, 8, 14, tzinfo=timezone.utc),
        )
        self.assertEqual(due[0].status, ProofStatus.DECAYED)
        self.assertEqual(due[0].decay_reason, NEXT_PROBE_AT)
        self.assertEqual(early[0].status, ProofStatus.LIVE)

    def test_polyrepo_example_lists_empty_without_crash(self) -> None:
        root = Path(__file__).resolve().parents[1] / "examples" / "polyrepo"
        config = load(root)
        self.assertEqual(list_proofs(config), ())

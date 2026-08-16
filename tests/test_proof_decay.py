from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
import hashlib
import unittest

from dyro.config import load
from dyro.continuation.budgets import ProgressFacts, progress_fingerprint
from dyro.continuation.models import ActionKind, AttentionKind, PlanCompletion, ReasonCode
from dyro.continuation.planner import build_continuation_plan, build_task_readiness
from dyro.continuation.snapshot import (
    SchedulerSnapshot,
    SchedulerTaskSnapshot,
    build_scheduler_snapshot_bounded,
    build_scheduler_snapshot_from_facts,
)
from dyro.continuation.store import create_objective
from dyro.read_limits import ObservationLimits, ReadBudget
from dyro.errors import DyroError
from dyro.graph import explain_task
from dyro.proof.decay import (
    ACTION_RECEIPT_BYTES,
    CURRENT_SUBSTRATE_MISSING,
    DEPENDENCY_INTEGRATED,
    EXTERNAL_SIGNOFF,
    GATE_ARGV,
    GATE_BYTES,
    LINE_PREPARE_NOT_DECAY,
    PREDICATE_INCONCLUSIVE,
    REVIEW_ACCEPTANCE,
    STILL_BOUND,
    decay,
)
from dyro.proof.derive import derive_task_proofs, list_proofs
from dyro.proof.evaluate import evaluate_proofs, live_merge_evidence
from dyro.proof.models import ObservedSubstrate, Proof, ProofKind, ProofStatus, ProofSubstrate
from dyro.provenance import review_binding
from dyro.tasks import (
    _valid_review_acceptance,
    check_dispatchable,
    load_task,
    merge_task,
    review_task,
    run_task,
    task_template,
)
from dyro.workspace import create_line

from .support import WorkspaceCase, shell

CLOCK = datetime(2026, 8, 15, 5, 20, tzinfo=timezone.utc)


def _proof(kind: ProofKind, *, bytes_sha256: str = "aa", extra: tuple[tuple[str, str], ...] = ()) -> Proof:
    return Proof(
        id="proof-" + kind.value,
        kind=kind,
        subject="TASK-A",
        substrate=ProofSubstrate(extra=extra),
        procedure="test",
        bytes_sha256=bytes_sha256,
        generation="g1",
        status=ProofStatus.INCONCLUSIVE,
    )


class ProofDecayPureTests(unittest.TestCase):
    def test_review_verdict_projects_valid_review_acceptance(self) -> None:
        proof = _proof(ProofKind.REVIEW_VERDICT)
        live = decay(proof, None, clock=CLOCK, review_ok=True)
        dead = decay(proof, None, clock=CLOCK, review_ok=False)
        unknown = decay(proof, None, clock=CLOCK, review_ok=None)
        self.assertEqual(live.status, ProofStatus.LIVE)
        self.assertEqual(live.reason, STILL_BOUND)
        self.assertEqual(dead.status, ProofStatus.DECAYED)
        self.assertEqual(dead.reason, REVIEW_ACCEPTANCE)
        self.assertEqual(unknown.status, ProofStatus.INCONCLUSIVE)
        self.assertEqual(unknown.reason, PREDICATE_INCONCLUSIVE)

    def test_signoff_projects_valid_external_signoff(self) -> None:
        proof = _proof(ProofKind.SIGNOFF)
        self.assertEqual(decay(proof, None, clock=CLOCK, signoff_ok=True).status, ProofStatus.LIVE)
        decision = decay(proof, None, clock=CLOCK, signoff_ok=False)
        self.assertEqual(decision.status, ProofStatus.DECAYED)
        self.assertEqual(decision.reason, EXTERNAL_SIGNOFF)

    def test_integration_projects_assert_dependency_integrated(self) -> None:
        proof = _proof(ProofKind.INTEGRATION_HEADS)
        self.assertEqual(decay(proof, None, clock=CLOCK, integration_ok=True).status, ProofStatus.LIVE)
        decision = decay(proof, None, clock=CLOCK, integration_ok=False)
        self.assertEqual(decision.status, ProofStatus.DECAYED)
        self.assertEqual(decision.reason, DEPENDENCY_INTEGRATED)

    def test_line_prepare_merge_is_not_proof_decayed(self) -> None:
        proof = _proof(ProofKind.INTEGRATION_HEADS)
        decision = decay(
            proof,
            None,
            clock=CLOCK,
            integration_ok=True,
            line_prepare_ok=False,
        )
        self.assertEqual(decision.status, ProofStatus.LIVE)
        self.assertNotEqual(decision.reason, LINE_PREPARE_NOT_DECAY)
        self.assertNotEqual(decision.status, ProofStatus.DECAYED)

    def test_git_revert_is_not_ancestor_break(self) -> None:
        proof = _proof(ProofKind.INTEGRATION_HEADS)
        decision = decay(proof, None, clock=CLOCK, integration_ok=True)
        self.assertEqual(decision.status, ProofStatus.LIVE)

    def test_gate_log_hash_change_is_display_decayed(self) -> None:
        proof = _proof(ProofKind.GATE_LOG, bytes_sha256="aa", extra=(("argv_sha256", "argv1"),))
        same = decay(proof, ObservedSubstrate("aa", "argv1"), clock=CLOCK)
        changed = decay(proof, ObservedSubstrate("bb", "argv1"), clock=CLOCK)
        argv = decay(proof, ObservedSubstrate("aa", "argv2"), clock=CLOCK)
        missing = decay(proof, None, clock=CLOCK)
        self.assertEqual(same.status, ProofStatus.LIVE)
        self.assertEqual(changed.status, ProofStatus.DECAYED)
        self.assertEqual(changed.reason, GATE_BYTES)
        self.assertEqual(argv.status, ProofStatus.DECAYED)
        self.assertEqual(argv.reason, GATE_ARGV)
        self.assertEqual(missing.status, ProofStatus.INCONCLUSIVE)
        self.assertEqual(missing.reason, CURRENT_SUBSTRATE_MISSING)

    def test_action_receipt_byte_change_is_decayed(self) -> None:
        proof = _proof(ProofKind.ACTION_RECEIPT, bytes_sha256="old")
        decision = decay(proof, ObservedSubstrate("new"), clock=CLOCK)
        self.assertEqual(decision.status, ProofStatus.DECAYED)
        self.assertEqual(decision.reason, ACTION_RECEIPT_BYTES)

    def test_clock_is_injected_and_identity_untouched(self) -> None:
        proof = _proof(ProofKind.REVIEW_VERDICT)
        decision = decay(proof, None, clock=CLOCK, review_ok=True)
        self.assertEqual(decision.observed_at, "2026-08-15T05:20:00Z")
        self.assertEqual(proof.status, ProofStatus.INCONCLUSIVE)

    def test_live_merge_evidence_changes_fingerprint_without_new_field(self) -> None:
        base = ProgressFacts(task_states=(("TASK-A", "done"),), effective_evidence=(("TASK-A", "receipt-1"),))
        live = Proof(
            id="b" * 64,
            kind=ProofKind.REVIEW_VERDICT,
            subject="TASK-A",
            substrate=ProofSubstrate(),
            procedure="review",
            bytes_sha256="aa",
            generation="g1",
            status=ProofStatus.LIVE,
        )
        decayed = Proof(
            id="c" * 64,
            kind=ProofKind.REVIEW_VERDICT,
            subject="TASK-A",
            substrate=ProofSubstrate(),
            procedure="review",
            bytes_sha256="aa",
            generation="g1",
            status=ProofStatus.DECAYED,
        )
        triggerish = Proof(
            id="d" * 64,
            kind=ProofKind.ACTION_RECEIPT,
            subject="release",
            substrate=ProofSubstrate(),
            procedure="receipt",
            bytes_sha256="aa",
            generation="g1",
            status=ProofStatus.LIVE,
        )
        with_live = ProgressFacts(
            task_states=base.task_states,
            effective_evidence=base.effective_evidence + live_merge_evidence((live, triggerish)),
        )
        with_decayed = ProgressFacts(
            task_states=base.task_states,
            effective_evidence=base.effective_evidence + live_merge_evidence((decayed, triggerish)),
        )
        self.assertNotEqual(progress_fingerprint(base), progress_fingerprint(with_live))
        self.assertEqual(progress_fingerprint(base), progress_fingerprint(with_decayed))

    def test_planner_emits_proof_decayed_attention_without_blocking_downstream(self) -> None:
        with self._temp_snapshot() as snapshot:
            plan = build_continuation_plan(snapshot)
            readiness = build_task_readiness(snapshot)
            self.assertEqual(plan.completion, PlanCompletion.INCOMPLETE)
            self.assertTrue(any(item.reason is ReasonCode.PROOF_DECAYED for item in plan.attention))
            self.assertTrue(any(item.kind is AttentionKind.NEEDS_USER for item in plan.attention))
            self.assertFalse(any(action.reason is ReasonCode.PROOF_DECAYED for action in plan.blocked))
            self.assertEqual([task.id for task in readiness.ready], ["TASK-B"])
            self.assertFalse(any(action.kind is ActionKind.EXECUTE_TASK and action.reason is ReasonCode.PROOF_DECAYED for action in readiness.blocked))

    def _temp_snapshot(self):
        from tempfile import TemporaryDirectory

        class _Guard:
            def __enter__(self_inner):
                self_inner.tmp = TemporaryDirectory()
                root = Path(self_inner.tmp.name)
                done = Taskish(root, "TASK-A")
                ready = Taskish(root, "TASK-B", depends_on=("TASK-A",))
                snapshot = SchedulerSnapshot(
                    observed_at=CLOCK,
                    tasks=(
                        SchedulerTaskSnapshot(done, "done", False, "integrated"),
                        SchedulerTaskSnapshot(ready, "backlog", False, "not_required"),
                    ),
                    decisions=(),
                    execution_mode="local",
                    candidate_ids=("TASK-A", "TASK-B"),
                    snapshot_sha256="a" * 64,
                    objective_id="release",
                    objective_revision=1,
                    objective_state="active",
                    objective_scope=("TASK-A", "TASK-B"),
                    objective_targets=("TASK-B",),
                    objective_requested_mode="supervised",
                    objective_operations=("execute", "review"),
                    decayed_merge_subjects=("TASK-A",),
                )
                self_inner.snapshot = snapshot
                return snapshot

            def __exit__(self_inner, *args):
                self_inner.tmp.cleanup()

        return _Guard()


def Taskish(root: Path, task_id: str, *, depends_on: tuple[str, ...] = ()):
    from dyro.tasks import Task

    return Task(
        id=task_id,
        title=task_id,
        line="alpha",
        risk="write",
        executor="noop",
        reviewer="noop",
        repositories=("api",),
        depends_on=depends_on,
        directory=root / task_id,
    )


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


class ProofDecayWorkspaceTests(WorkspaceCase):
    def _reviewed_task(self, task_id: str):
        config = load(self.root)
        create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        task_path = config.task_specs_dir / task_id
        task_path.mkdir(parents=True)
        task_path.joinpath("task.toml").write_text(
            task_template(task_id, "decay", "alpha", "api", "services/api").replace(
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

    def test_evaluate_review_matches_valid_review_acceptance(self) -> None:
        config, task = self._reviewed_task("TASK-LIVE")
        self.assertTrue(_valid_review_acceptance(config, task))
        proofs = evaluate_proofs(config, derive_task_proofs(config, task))
        review = next(proof for proof in proofs if proof.kind is ProofKind.REVIEW_VERDICT)
        self.assertEqual(review.status, ProofStatus.LIVE)
        integration = next(proof for proof in proofs if proof.kind is ProofKind.INTEGRATION_HEADS)
        self.assertEqual(integration.status, ProofStatus.LIVE)

    def test_torn_review_is_decayed_and_merge_still_refuses(self) -> None:
        config, task = self._reviewed_task("TASK-TORN")
        task.directory.joinpath("review.md").write_text("verdict: PASS\n", encoding="utf-8")
        self.assertFalse(_valid_review_acceptance(config, task))
        proofs = evaluate_proofs(config, derive_task_proofs(config, task))
        review = next(proof for proof in proofs if proof.kind is ProofKind.REVIEW_VERDICT)
        self.assertEqual(review.status, ProofStatus.DECAYED)
        self.assertEqual(review.decay_reason, REVIEW_ACCEPTANCE)
        with self.assertRaisesRegex(DyroError, r"有效的独立复核.*PROOF_DECAYED"):
            merge_task(config, task)

    def test_missing_review_file_is_inconclusive_after_list(self) -> None:
        config, task = self._reviewed_task("TASK-ABSENT-REVIEW")
        task.directory.joinpath("review.md").unlink()
        self.assertFalse(_valid_review_acceptance(config, task))
        listed = list_proofs(config, task_id=task.id)
        review = next(proof for proof in listed if proof.kind is ProofKind.REVIEW_VERDICT)
        self.assertEqual(review.status, ProofStatus.INCONCLUSIVE)
        self.assertIsNot(review.status, ProofStatus.DECAYED)
        with self.assertRaises(DyroError) as raised:
            merge_task(config, task)
        self.assertIn("有效的独立复核", str(raised.exception))
        self.assertNotIn("PROOF_DECAYED", str(raised.exception))

    def test_dirty_task_worktree_without_head_change_still_refuses_merge(self) -> None:
        config, task = self._reviewed_task("TASK-DIRTY-HEAD")
        worktree = self.root / "worktrees/alpha/TASK-DIRTY-HEAD/services/api"
        worktree.joinpath("DIRTY.txt").write_text("stay dirty\n", encoding="utf-8")
        self.assertFalse(_valid_review_acceptance(config, task))
        proofs = evaluate_proofs(config, derive_task_proofs(config, task))
        review = next(proof for proof in proofs if proof.kind is ProofKind.REVIEW_VERDICT)
        self.assertEqual(review.status, ProofStatus.DECAYED)
        with self.assertRaisesRegex(DyroError, "有效的独立复核"):
            merge_task(config, task)

    def test_line_dirty_is_prepare_merge_not_proof_decayed(self) -> None:
        config, task = self._reviewed_task("TASK-LINE-DIRTY")
        line_repo = self.root / "versions/alpha/services/api"
        line_repo.joinpath("LINE-DIRTY.txt").write_text("dirty line\n", encoding="utf-8")
        proofs = evaluate_proofs(config, derive_task_proofs(config, task))
        review = next(proof for proof in proofs if proof.kind is ProofKind.REVIEW_VERDICT)
        integration = next(proof for proof in proofs if proof.kind is ProofKind.INTEGRATION_HEADS)
        self.assertEqual(review.status, ProofStatus.LIVE)
        self.assertEqual(integration.status, ProofStatus.LIVE)
        with self.assertRaisesRegex(DyroError, "开发线仓库不干净"):
            merge_task(config, task)

    def _downstream(self, config, task_id: str, dependency: str):
        path = config.task_specs_dir / task_id
        path.mkdir(parents=True)
        spec = task_template(task_id, "downstream", "alpha", "api", "services/api").replace(
            'agent = "codex"', 'agent = "noop"'
        )
        spec = spec.replace("depends_on = []", f'depends_on = ["{dependency}"]')
        path.joinpath("task.toml").write_text(spec, encoding="utf-8")
        path.joinpath("handoff.md").write_text("# handoff\n", encoding="utf-8")
        return load_task(config, task_id)

    def test_explain_blocks_on_unintegrated_done_dependency(self) -> None:
        config = load(self.root)
        create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        task_path = config.task_specs_dir / "TASK-UP"
        task_path.mkdir(parents=True)
        task_path.joinpath("task.toml").write_text(
            task_template("TASK-UP", "upstream", "alpha", "api", "services/api").replace(
                'agent = "codex"', 'agent = "noop"'
            ),
            encoding="utf-8",
        )
        task_path.joinpath("handoff.md").write_text("# handoff\n", encoding="utf-8")
        task_path.joinpath("receipt.md").write_text("result: QUESTION\n", encoding="utf-8")
        task = load_task(config, "TASK-UP")
        from dyro.tasks import answer_task

        self.assertEqual(run_task(config, task), "waiting_answer")
        repository = self.root / "worktrees/alpha/TASK-UP/services/api"
        repository.joinpath("UNMERGED.md").write_text("pending\n", encoding="utf-8")
        shell("git", "add", "UNMERGED.md", cwd=repository)
        shell("git", "commit", "-m", "feat: unmerged", cwd=repository)
        task_path.joinpath("receipt.md").write_text("result: DONE\n", encoding="utf-8")
        self.assertEqual(answer_task(config, task, "continue"), "review")
        _write_bound_review(task_path)
        self.assertEqual(review_task(config, load_task(config, "TASK-UP")), "done")
        downstream = self._downstream(config, "TASK-DOWN", "TASK-UP")
        report = explain_task(config, "TASK-DOWN")
        self.assertFalse(report["dispatchable"])
        self.assertTrue(any("尚未集成" in reason for reason in report["reasons"]))
        with self.assertRaisesRegex(DyroError, "尚未集成"):
            check_dispatchable(config, downstream)

    def test_torn_review_does_not_block_downstream_when_ancestor_holds(self) -> None:
        config, integrated = self._reviewed_task("TASK-INT")
        self._downstream(config, "TASK-DOWN2", "TASK-INT")
        integrated.directory.joinpath("review.md").write_text("verdict: PASS\n", encoding="utf-8")
        check_dispatchable(config, load_task(config, "TASK-DOWN2"))
        torn = explain_task(config, "TASK-DOWN2")
        self.assertTrue(torn["dispatchable"])
        with self.assertRaisesRegex(DyroError, "PROOF_DECAYED"):
            merge_task(config, load_task(config, "TASK-INT"))

    def test_from_facts_does_not_inspect_proofs(self) -> None:
        with patch("dyro.proof.evaluate.decayed_merge_subjects") as inspect:
            snapshot = build_scheduler_snapshot_from_facts(
                tasks=(),
                decisions=(),
                execution_mode="local",
                candidate_ids=(),
                observed_at=CLOCK,
            )
        inspect.assert_not_called()
        self.assertEqual(snapshot.decayed_merge_subjects, ())

    def test_bounded_snapshot_does_not_inspect_proofs(self) -> None:
        config = load(self.root)
        create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        task_path = config.task_specs_dir / "TASK-BOUND"
        task_path.mkdir(parents=True)
        task_path.joinpath("task.toml").write_text(
            task_template("TASK-BOUND", "bounded", "alpha", "api", "services/api").replace(
                'agent = "codex"', 'agent = "noop"'
            ),
            encoding="utf-8",
        )
        task_path.joinpath("handoff.md").write_text("# handoff\n", encoding="utf-8")
        record = create_objective(
            config,
            '''schema_version = 1
id = "bounded"
title = "Bounded snapshot"
line = "alpha"
targets = ["TASK-BOUND"]

[continuation]
requested_mode = "supervised"
operations = ["execute", "review"]
''',
        )
        with patch("dyro.proof.evaluate.decayed_merge_subjects") as inspect:
            snapshot = build_scheduler_snapshot_bounded(
                config,
                objective=record,
                budget=ReadBudget(ObservationLimits()),
            )
        inspect.assert_not_called()
        self.assertEqual(snapshot.decayed_merge_subjects, ())


if __name__ == "__main__":
    unittest.main()

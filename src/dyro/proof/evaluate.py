"""I/O layer: run existing 0.6 predicates, then call pure decay()."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Callable, Iterable

from ..config import Config
from ..errors import DyroError, ValidationError
from ..evidence_store import resolve_evidence_path
from ..tasks import (
    TASK_HEADS_FILE,
    Task,
    _assert_dependency_integrated,
    _valid_external_signoff,
    _valid_review_acceptance,
    load_task,
)
from .decay import decay
from .derive import derive_objective_proofs, derive_task_proofs
from .models import ObservedSubstrate, Proof, ProofKind, ProofStatus

_MERGE_KINDS = frozenset({ProofKind.REVIEW_VERDICT, ProofKind.SIGNOFF})
_Clock = Callable[[], datetime]


def evaluate_proofs(
    config: Config,
    proofs: Iterable[Proof],
    *,
    clock: _Clock | None = None,
) -> tuple[Proof, ...]:
    now = clock or (lambda: datetime.now(timezone.utc))
    observed = now()
    return tuple(evaluate_proof(config, proof, clock=lambda: observed) for proof in proofs)


def evaluate_proof(
    config: Config,
    proof: Proof,
    *,
    clock: _Clock | None = None,
) -> Proof:
    observed_at = (clock or (lambda: datetime.now(timezone.utc)))()
    review_ok: bool | None = None
    signoff_ok: bool | None = None
    integration_ok: bool | None = None
    probe_due: bool | None = None
    current: ObservedSubstrate | None = None

    if proof.kind is ProofKind.REVIEW_VERDICT:
        review_ok = _review_ok(config, proof)
    elif proof.kind is ProofKind.SIGNOFF:
        signoff_ok = _signoff_ok(config, proof)
    elif proof.kind is ProofKind.INTEGRATION_HEADS:
        integration_ok = _integration_ok(config, proof)
    elif proof.kind in {ProofKind.GATE_LOG, ProofKind.ACTION_RECEIPT}:
        current = _current_bytes(config, proof)
    elif proof.kind is ProofKind.TRIGGER_OBSERVATION:
        probe_due = _probe_due(proof, observed_at)

    decision = decay(
        proof,
        current,
        clock=observed_at,
        review_ok=review_ok,
        signoff_ok=signoff_ok,
        integration_ok=integration_ok,
        probe_due=probe_due,
    )
    updated = replace(proof, status=decision.status, decay_reason=decision.reason, observed_at=decision.observed_at)
    return _refresh_integration_state(updated)


def decayed_merge_subjects(config: Config, tasks: Iterable[Task]) -> tuple[str, ...]:
    """Task ids whose merge-relevant Proofs are decayed. Does not block downstream."""
    found: list[str] = []
    for task in tasks:
        try:
            proofs = evaluate_proofs(config, derive_task_proofs(config, task))
        except (DyroError, ValidationError, OSError):
            continue
        if any(proof.kind in _MERGE_KINDS and proof.status is ProofStatus.DECAYED for proof in proofs):
            found.append(task.id)
    return tuple(sorted(found))


def live_merge_evidence(proofs: Iterable[Proof]) -> tuple[tuple[str, str], ...]:
    """Pairs for ProgressFacts.effective_evidence. Trigger-class kinds stay out."""
    return tuple(
        sorted(
            (proof.subject, proof.id)
            for proof in proofs
            if proof.status is ProofStatus.LIVE
            and proof.kind in {ProofKind.REVIEW_VERDICT, ProofKind.SIGNOFF, ProofKind.INTEGRATION_HEADS}
        )
    )


def _subject_task(config: Config, proof: Proof) -> Task:
    return load_task(config, proof.subject)


def _incomplete_evidence(proof: Proof) -> bool:
    return any(key in {"missing", "unparseable"} for key, _value in proof.substrate.extra)


def _review_ok(config: Config, proof: Proof) -> bool | None:
    if _incomplete_evidence(proof) or not _review_files_present(config, proof):
        return None
    return _predicate(lambda: _valid_review_acceptance(config, _subject_task(config, proof)))


def _signoff_ok(config: Config, proof: Proof) -> bool | None:
    if _incomplete_evidence(proof) or not _signoff_file_present(config, proof):
        return None
    return _predicate(lambda: _valid_external_signoff(config, _subject_task(config, proof)))


def _review_files_present(config: Config, proof: Proof) -> bool:
    try:
        task = _subject_task(config, proof)
    except (DyroError, ValidationError):
        return False
    if not (task.directory / "review.md").is_file():
        return False
    try:
        resolve_evidence_path(task.directory, "receipt.md")
        resolve_evidence_path(task.directory, TASK_HEADS_FILE)
    except DyroError:
        return False
    return True


def _signoff_file_present(config: Config, proof: Proof) -> bool:
    try:
        task = _subject_task(config, proof)
    except (DyroError, ValidationError):
        return False
    return (task.directory / "signoff.json").is_file()


def _refresh_integration_state(proof: Proof) -> Proof:
    if proof.kind is not ProofKind.INTEGRATION_HEADS:
        return proof
    mapped = {
        ProofStatus.LIVE: "integrated",
        ProofStatus.DECAYED: "pending",
        ProofStatus.INCONCLUSIVE: "inconclusive",
    }.get(proof.status)
    if mapped is None:
        return proof
    extra = tuple(
        (key, mapped if key == "integration_state" else value)
        for key, value in proof.substrate.extra
    )
    if not any(key == "integration_state" for key, _value in extra):
        extra = extra + (("integration_state", mapped),)
    return replace(proof, substrate=replace(proof.substrate, extra=extra))


def _predicate(probe: Callable[[], bool]) -> bool | None:
    try:
        return bool(probe())
    except (DyroError, ValidationError, OSError, KeyError):
        return None


def _integration_ok(config: Config, proof: Proof) -> bool | None:
    try:
        task = _subject_task(config, proof)
    except (DyroError, ValidationError):
        return None
    try:
        _assert_dependency_integrated(config, task)
        return True
    except DyroError as exc:
        if "尚未集成" in str(exc):
            return False
        return None
    except (ValidationError, OSError):
        return None


def _probe_due(proof: Proof, observed_at: datetime) -> bool | None:
    raw = dict(proof.substrate.extra).get("next_probe_at", "")
    if not raw:
        return None
    try:
        due = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if due.tzinfo is None:
        return None
    return observed_at >= due.astimezone(timezone.utc)


def _current_bytes(config: Config, proof: Proof) -> ObservedSubstrate | None:
    try:
        if proof.kind is ProofKind.ACTION_RECEIPT:
            fresh = derive_objective_proofs(config, proof.subject)
        else:
            fresh = derive_task_proofs(config, _subject_task(config, proof))
    except (DyroError, ValidationError, OSError):
        return None
    match = next((item for item in fresh if item.id == proof.id), None)
    if match is None:
        return None
    return ObservedSubstrate(
        bytes_sha256=match.bytes_sha256,
        argv_sha256=dict(match.substrate.extra).get("argv_sha256", ""),
        present=True,
    )

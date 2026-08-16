"""Pure Proof decay. Callers inject substrate, predicates, and clock."""

from __future__ import annotations

from datetime import datetime, timezone

from .models import DecayDecision, ObservedSubstrate, Proof, ProofKind, ProofStatus

# Reason tokens name the 0.6 predicate they project. Tests match these strings.
REVIEW_ACCEPTANCE = "review_acceptance"
EXTERNAL_SIGNOFF = "external_signoff"
DEPENDENCY_INTEGRATED = "dependency_integrated"
GATE_BYTES = "gate_bytes"
GATE_ARGV = "gate_argv"
ACTION_RECEIPT_BYTES = "action_receipt_bytes"
NEXT_PROBE_AT = "next_probe_at"
CURRENT_SUBSTRATE_MISSING = "current_substrate_missing"
PREDICATE_INCONCLUSIVE = "predicate_inconclusive"
STILL_BOUND = "still_bound"
LINE_PREPARE_NOT_DECAY = "line_prepare_not_decay"


def _observed_at(clock: datetime) -> str:
    if clock.tzinfo is None:
        raise TypeError("decay clock 必须带时区")
    return clock.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _decision(status: ProofStatus, reason: str, clock: datetime) -> DecayDecision:
    return DecayDecision(status=status, reason=reason, observed_at=_observed_at(clock))


def _from_predicate(ok: bool | None, *, live_reason: str, decay_reason: str, clock: datetime) -> DecayDecision:
    if ok is None:
        return _decision(ProofStatus.INCONCLUSIVE, PREDICATE_INCONCLUSIVE, clock)
    if ok:
        return _decision(ProofStatus.LIVE, live_reason, clock)
    return _decision(ProofStatus.DECAYED, decay_reason, clock)


def decay(
    proof: Proof,
    current: ObservedSubstrate | None,
    *,
    clock: datetime,
    review_ok: bool | None = None,
    signoff_ok: bool | None = None,
    integration_ok: bool | None = None,
    line_prepare_ok: bool | None = None,
    probe_due: bool | None = None,
) -> DecayDecision:
    """Project live/decayed/inconclusive from injected facts.

    ``line_prepare_ok`` is accepted so callers can record a ``_prepare_merge``
    failure. It never produces ``PROOF_DECAYED``; dirty or wrong-branch lines
    stay merge errors.
    """
    if not isinstance(proof, Proof):
        raise TypeError("proof 必须是 Proof")
    if current is not None and not isinstance(current, ObservedSubstrate):
        raise TypeError("current 必须是 ObservedSubstrate 或 None")
    if line_prepare_ok is False and proof.kind is ProofKind.INTEGRATION_HEADS:
        # Explicitly ignore prepare-merge failures for this kind.
        pass

    if proof.kind is ProofKind.REVIEW_VERDICT:
        return _from_predicate(
            review_ok,
            live_reason=STILL_BOUND,
            decay_reason=REVIEW_ACCEPTANCE,
            clock=clock,
        )
    if proof.kind is ProofKind.SIGNOFF:
        return _from_predicate(
            signoff_ok,
            live_reason=STILL_BOUND,
            decay_reason=EXTERNAL_SIGNOFF,
            clock=clock,
        )
    if proof.kind is ProofKind.INTEGRATION_HEADS:
        return _from_predicate(
            integration_ok,
            live_reason=STILL_BOUND,
            decay_reason=DEPENDENCY_INTEGRATED,
            clock=clock,
        )
    if proof.kind is ProofKind.GATE_LOG:
        return _bytes_kind(
            proof,
            current,
            clock=clock,
            mismatch_reason=GATE_BYTES,
            argv_reason=GATE_ARGV,
        )
    if proof.kind is ProofKind.ACTION_RECEIPT:
        return _bytes_kind(proof, current, clock=clock, mismatch_reason=ACTION_RECEIPT_BYTES)
    if proof.kind is ProofKind.TRIGGER_OBSERVATION:
        return _from_predicate(
            None if probe_due is None else (not probe_due),
            live_reason=STILL_BOUND,
            decay_reason=NEXT_PROBE_AT,
            clock=clock,
        )
    return _decision(ProofStatus.INCONCLUSIVE, PREDICATE_INCONCLUSIVE, clock)


def _bytes_kind(
    proof: Proof,
    current: ObservedSubstrate | None,
    *,
    clock: datetime,
    mismatch_reason: str,
    argv_reason: str = "",
) -> DecayDecision:
    if current is None or not current.present:
        return _decision(ProofStatus.INCONCLUSIVE, CURRENT_SUBSTRATE_MISSING, clock)
    recorded_argv = dict(proof.substrate.extra).get("argv_sha256", "")
    if argv_reason and current.argv_sha256 and recorded_argv and current.argv_sha256 != recorded_argv:
        return _decision(ProofStatus.DECAYED, argv_reason, clock)
    if current.bytes_sha256 != proof.bytes_sha256:
        return _decision(ProofStatus.DECAYED, mismatch_reason, clock)
    return _decision(ProofStatus.LIVE, STILL_BOUND, clock)

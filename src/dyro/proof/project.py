"""Shared Proof projection for text and JSON. Never claims procedure replay."""

from __future__ import annotations

import json

from .models import Proof, ProofStatus

VERIFY_EXIT_OK = 0
VERIFY_EXIT_DECAYED = 1
VERIFY_EXIT_ERROR = 2
VERIFY_EXIT_INCONCLUSIVE = 3


def proof_payload(proof: Proof, *, procedure_reproduced: bool = False) -> dict[str, object]:
    if procedure_reproduced:
        raise ValueError("未 replay 不得声称 procedure_reproduced")
    return {
        "id": proof.id,
        "kind": proof.kind.value,
        "subject": proof.subject,
        "status": proof.status.value,
        "generation": proof.generation,
        "procedure": proof.procedure,
        "bytes_sha256": proof.bytes_sha256,
        "produced_at": proof.produced_at,
        "decay_reason": proof.decay_reason,
        "observed_at": proof.observed_at,
        "declared_key_ids": list(proof.declared_key_ids),
        "policy_require_signed": dict(proof.policy_require_signed),
        "substrate": {
            "repo_heads": dict(proof.substrate.repo_heads),
            "plan_sha256": proof.substrate.plan_sha256,
            "attempt_id": proof.substrate.attempt_id,
            "contract_hash": proof.substrate.contract_hash,
            "extra": dict(proof.substrate.extra),
        },
        "procedure_reproduced": False,
    }


def proofs_payload(proofs: tuple[Proof, ...], *, mode: str = "rebind") -> dict[str, object]:
    counts = {status.value: 0 for status in ProofStatus}
    for proof in proofs:
        counts[proof.status.value] += 1
    payload: dict[str, object] = {
        "schema_version": 1,
        "mode": mode,
        "proofs": [proof_payload(proof) for proof in proofs],
        "summary": counts,
    }
    if mode == "integrity":
        payload["conclusion"] = "integrity"
        payload["merge_equivalent"] = False
    return payload


def render_proofs_json(proofs: tuple[Proof, ...], *, mode: str = "rebind") -> str:
    return json.dumps(proofs_payload(proofs, mode=mode), ensure_ascii=False, sort_keys=True, indent=2)


def render_proofs_text(proofs: tuple[Proof, ...], *, mode: str = "") -> str:
    lines: list[str] = []
    if mode:
        lines.append(f"mode: {mode}")
        lines.append("procedure_reproduced: false")
    if not proofs:
        lines.append("暂无 Proof")
        return "\n".join(lines) + "\n"
    for proof in proofs:
        lines.append(f"{proof.kind.value:20} {proof.status.value:13} {proof.subject:24} {proof.id}")
        if proof.decay_reason:
            lines.append(f"  {proof.decay_reason}")
    return "\n".join(lines) + "\n"


def verify_exit_code(proofs: tuple[Proof, ...]) -> int:
    statuses = {proof.status for proof in proofs}
    if ProofStatus.DECAYED in statuses:
        return VERIFY_EXIT_DECAYED
    if ProofStatus.INCONCLUSIVE in statuses or ProofStatus.REVOKED in statuses:
        return VERIFY_EXIT_INCONCLUSIVE
    return VERIFY_EXIT_OK

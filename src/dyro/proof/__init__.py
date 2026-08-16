"""Read-only Proof projection. Not a second PASS, merge, or gate."""

from .bundle import export_bundle, load_current_heads, verify_bundle
from .decay import decay
from .derive import derive_objective_proofs, derive_task_proofs, list_proofs
from .evaluate import decayed_merge_subjects, evaluate_proof, evaluate_proofs, live_merge_evidence
from .models import (
    DecayDecision,
    ObservedSubstrate,
    Proof,
    ProofKind,
    ProofStatus,
    ProofSubstrate,
    proof_identity_sha256,
)
from .project import (
    proof_payload,
    proofs_payload,
    render_proofs_json,
    render_proofs_text,
    verify_exit_code,
)

__all__ = (
    "DecayDecision",
    "ObservedSubstrate",
    "Proof",
    "ProofKind",
    "ProofStatus",
    "ProofSubstrate",
    "decay",
    "decayed_merge_subjects",
    "derive_objective_proofs",
    "derive_task_proofs",
    "evaluate_proof",
    "evaluate_proofs",
    "export_bundle",
    "list_proofs",
    "live_merge_evidence",
    "load_current_heads",
    "proof_identity_sha256",
    "proof_payload",
    "proofs_payload",
    "render_proofs_json",
    "render_proofs_text",
    "verify_bundle",
    "verify_exit_code",
)

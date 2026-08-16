"""Immutable Proof records. Identity hashes contain no clock or mtime."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
from typing import Any, Mapping

from ..canonical import canonical_json_bytes
from ..errors import ValidationError


class ProofKind(str, Enum):
    GATE_LOG = "gate_log"
    REVIEW_VERDICT = "review_verdict"
    SIGNOFF = "signoff"
    INTEGRATION_HEADS = "integration_heads"
    ACTION_RECEIPT = "action_receipt"
    BUNDLE_FAILURE = "bundle_failure"


class ProofStatus(str, Enum):
    LIVE = "live"
    DECAYED = "decayed"
    INCONCLUSIVE = "inconclusive"
    REVOKED = "revoked"


_KIND_VALUES = {item.value for item in ProofKind}


def _frozen_pairs(value: Any, label: str) -> tuple[tuple[str, str], ...]:
    if isinstance(value, (str, bytes)):
        raise TypeError(f"{label} 必须是集合，不能是字符串")
    pairs: list[tuple[str, str]] = []
    for item in value:
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            raise TypeError(f"{label} 必须只包含两个字符串组成的键值对")
        key, mapped = item
        if not isinstance(key, str) or not isinstance(mapped, str):
            raise TypeError(f"{label} 必须只包含两个字符串组成的键值对")
        pairs.append((key, mapped))
    return tuple(pairs)


def _frozen_strings(value: Any, label: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        raise TypeError(f"{label} 必须是集合，不能是字符串")
    items = tuple(value)
    if not all(isinstance(item, str) for item in items):
        raise TypeError(f"{label} 必须只包含字符串")
    return items


def proof_identity_sha256(
    *,
    kind: ProofKind | str,
    subject: str,
    generation: str,
    identity_payload: Mapping[str, object],
) -> str:
    """Return the stable Proof id. Payload must omit produced_at, mtime, and now."""
    kind_value = kind.value if isinstance(kind, ProofKind) else kind
    if kind_value not in _KIND_VALUES:
        raise ValidationError(f"Proof kind 无效：{kind_value}")
    if not subject:
        raise ValidationError("Proof subject 不能为空")
    forbidden = {"produced_at", "mtime", "now", "observed_at"}
    if forbidden.intersection(identity_payload):
        raise ValidationError("Proof 身份载荷不能包含时钟或 mtime 字段")
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "kind": kind_value,
                "subject": subject,
                "generation": generation,
                "identity": dict(identity_payload),
            }
        )
    ).hexdigest()


@dataclass(frozen=True)
class ProofSubstrate:
    repo_heads: tuple[tuple[str, str], ...] = ()
    plan_sha256: str = ""
    attempt_id: str = ""
    contract_hash: str = ""
    extra: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "repo_heads", _frozen_pairs(self.repo_heads, "ProofSubstrate.repo_heads"))
        object.__setattr__(self, "extra", _frozen_pairs(self.extra, "ProofSubstrate.extra"))
        if not isinstance(self.plan_sha256, str) or not isinstance(self.attempt_id, str):
            raise TypeError("ProofSubstrate.plan_sha256 与 attempt_id 必须是字符串")
        if not isinstance(self.contract_hash, str):
            raise TypeError("ProofSubstrate.contract_hash 必须是字符串")


@dataclass(frozen=True)
class Proof:
    id: str
    kind: ProofKind
    subject: str
    substrate: ProofSubstrate
    procedure: str
    bytes_sha256: str
    generation: str
    status: ProofStatus
    produced_at: str = ""
    declared_key_ids: tuple[str, ...] = ()
    policy_require_signed: tuple[tuple[str, str], ...] = ()
    decay_reason: str = ""
    observed_at: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ProofKind):
            raise TypeError("Proof.kind 必须是 ProofKind")
        if not isinstance(self.status, ProofStatus):
            raise TypeError("Proof.status 必须是 ProofStatus")
        if not isinstance(self.substrate, ProofSubstrate):
            raise TypeError("Proof.substrate 必须是 ProofSubstrate")
        if not self.id or not self.subject:
            raise ValidationError("Proof id 与 subject 不能为空")
        object.__setattr__(self, "declared_key_ids", _frozen_strings(self.declared_key_ids, "Proof.declared_key_ids"))
        object.__setattr__(
            self,
            "policy_require_signed",
            _frozen_pairs(self.policy_require_signed, "Proof.policy_require_signed"),
        )
        for value, label in (
            (self.procedure, "Proof.procedure"),
            (self.bytes_sha256, "Proof.bytes_sha256"),
            (self.generation, "Proof.generation"),
            (self.produced_at, "Proof.produced_at"),
            (self.decay_reason, "Proof.decay_reason"),
            (self.observed_at, "Proof.observed_at"),
        ):
            if not isinstance(value, str):
                raise TypeError(f"{label} 必须是字符串")


@dataclass(frozen=True)
class ObservedSubstrate:
    """Caller-injected current bytes. decay() never reads the workspace."""

    bytes_sha256: str = ""
    argv_sha256: str = ""
    present: bool = True


@dataclass(frozen=True)
class DecayDecision:
    status: ProofStatus
    reason: str
    observed_at: str

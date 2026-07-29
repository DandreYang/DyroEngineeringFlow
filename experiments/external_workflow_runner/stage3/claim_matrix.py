"""Multi-phase claim deadline matrix for Stage 3."""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import Stage0ValidationError


@dataclass(frozen=True)
class ClaimDeadlineMatrix:
    """
    Lease must cover:

      phase holds + agent budget + sandbox/broker cleanup + safety margin
    """

    phase1_hold_ms: int = 1200
    phase2_agent_budget_ms: int = 4000
    phase3_hold_ms: int = 1200
    cleanup_ms: int = 2000
    safety_ms: int = 2000

    def __post_init__(self) -> None:
        for name in (
            "phase1_hold_ms",
            "phase2_agent_budget_ms",
            "phase3_hold_ms",
            "cleanup_ms",
            "safety_ms",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0 or value > 600_000:
                raise Stage0ValidationError(f"claim matrix field invalid: {name}")

    @property
    def workflow_hold_ms(self) -> int:
        return self.phase1_hold_ms + self.phase3_hold_ms

    @property
    def total_ms(self) -> int:
        return (
            self.phase1_hold_ms
            + self.phase2_agent_budget_ms
            + self.phase3_hold_ms
            + self.cleanup_ms
            + self.safety_ms
        )

    def total_seconds(self) -> float:
        return self.total_ms / 1000.0

    def recommend_initial_lease_seconds(self) -> float:
        # Short initial lease forces mid-run renewal during multi-phase hold.
        return max(3.0, (self.phase1_hold_ms + self.safety_ms) / 1000.0)

    def recommend_extend_seconds(self) -> float:
        return max(15.0, self.total_seconds())

    def recommend_workflow_timeout_seconds(self) -> float:
        return max(30.0, self.total_seconds() + 10.0)

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "phase1_hold_ms": self.phase1_hold_ms,
            "phase2_agent_budget_ms": self.phase2_agent_budget_ms,
            "phase3_hold_ms": self.phase3_hold_ms,
            "cleanup_ms": self.cleanup_ms,
            "safety_ms": self.safety_ms,
            "total_ms": self.total_ms,
        }

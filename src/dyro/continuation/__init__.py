"""Immutable contracts for Dyro's native continuation engine.

The package intentionally contains no filesystem, clock, process, or network
access at this boundary. State persistence and execution arrive in later
stages through explicit Core-owned modules.
"""

from .contracts import canonical_contract, contract_sha256, parse_contract, validate_objective_scope
from .models import (
    ActionKind,
    AttentionItem,
    AttentionKind,
    BudgetLimit,
    CompletionRule,
    ContinuationPlan,
    ContinuationSnapshot,
    Objective,
    Operation,
    PlanCompletion,
    PlannedAction,
    ReasonCode,
    RequestedMode,
    TriggerObservation,
    TriggerState,
)

__all__ = (
    "AttentionItem",
    "AttentionKind",
    "ActionKind",
    "BudgetLimit",
    "CompletionRule",
    "ContinuationPlan",
    "ContinuationSnapshot",
    "Objective",
    "Operation",
    "PlanCompletion",
    "PlannedAction",
    "ReasonCode",
    "RequestedMode",
    "TriggerObservation",
    "TriggerState",
    "canonical_contract",
    "contract_sha256",
    "parse_contract",
    "validate_objective_scope",
)

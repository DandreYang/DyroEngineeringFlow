"""Immutable contracts for Dyro's native continuation engine.

The package intentionally contains no filesystem, clock, process, or network
access at this boundary. State persistence and execution arrive in later
stages through explicit Core-owned modules.
"""

from .contracts import canonical_contract, contract_sha256, parse_contract, validate_objective_scope
from .attention import AttentionReadItem, AttentionReadProjection, attention_projection_payload, build_attention_projection
from .engine import SchedulerTick, WaveDeferral, WaveDeferralReason, build_scheduler_tick, scheduler_tick_payload
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
    SchedulerEdge,
    SchedulerNode,
    SchedulerReadProjection,
    TriggerObservation,
    TriggerState,
)

__all__ = (
    "AttentionItem",
    "AttentionKind",
    "AttentionReadItem",
    "AttentionReadProjection",
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
    "SchedulerEdge",
    "SchedulerNode",
    "SchedulerReadProjection",
    "SchedulerTick",
    "TriggerObservation",
    "TriggerState",
    "WaveDeferral",
    "WaveDeferralReason",
    "build_scheduler_tick",
    "attention_projection_payload",
    "build_attention_projection",
    "canonical_contract",
    "contract_sha256",
    "parse_contract",
    "scheduler_tick_payload",
    "validate_objective_scope",
)

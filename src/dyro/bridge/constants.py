"""Single-source protocol and planner revision constants."""

from __future__ import annotations

from types import MappingProxyType


BRIDGE_VERSION = "1.0"
PROTOCOL_MAJOR = 1
PROTOCOL_MINOR = 0
PLAN_TTL_SECONDS = 300

PLAN_OPERATION_REVISIONS = MappingProxyType(
    {
        "objective.attention": "objective-attention/1",
        "objective.explain": "objective-explain/1",
        "objective.graph": "objective-graph/1",
        "objective.plan": "objective-plan/1",
        "objective.tick": "objective-tick/1",
    }
)

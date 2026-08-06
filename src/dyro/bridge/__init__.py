"""Machine-readable, inspect-and-plan-only Dyro Bridge contracts.

Phase 0 intentionally contains no transport and no mutation entry point.
"""

from .catalog import (
    capabilities_digest,
    compact_capabilities,
    get_operation,
    list_operations,
)

__all__ = [
    "capabilities_digest",
    "compact_capabilities",
    "get_operation",
    "list_operations",
]

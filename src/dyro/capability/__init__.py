"""Capability plane: audited Cards only.

PATH discovery is not a Card. A dispatch-ready provider without a Card is the
second write door (explicitly allowed). A Card without execute is always refused.
"""

from .cards import (
    assert_capability_allows_write,
    card_forbids_execute,
    card_from_adapter,
    card_trusted_usage,
    merge_capability_plane,
    parse_capability_tables,
    write_capability_denied,
)
from .models import (
    CapabilityCard,
    CapabilityKind,
    CapabilityTestReport,
    DiscoveredTool,
    Isolation,
)
from .probe import (
    card_payload,
    discover_unintegrated,
    runtime_cards,
    test_capability,
)
from .store import append_capability, card_from_command, card_from_preset

__all__ = (
    "CapabilityCard",
    "CapabilityKind",
    "CapabilityTestReport",
    "DiscoveredTool",
    "Isolation",
    "append_capability",
    "assert_capability_allows_write",
    "card_forbids_execute",
    "card_from_adapter",
    "card_from_command",
    "card_trusted_usage",
    "card_from_preset",
    "card_payload",
    "discover_unintegrated",
    "merge_capability_plane",
    "parse_capability_tables",
    "runtime_cards",
    "test_capability",
    "write_capability_denied",
)

"""Probe Cards and list PATH discoveries. Discovery never becomes execute."""

from __future__ import annotations

from pathlib import Path
import shutil

from ..config import Config
from ..errors import DyroError
from ..profile import test_adapter
from ..tooling import TOOL_DEFINITIONS
from .models import CapabilityCard, CapabilityTestReport, DiscoveredTool


def runtime_cards(config: Config) -> dict[str, CapabilityCard]:
    return dict(config.capabilities)


def discover_unintegrated(config: Config) -> tuple[DiscoveredTool, ...]:
    """PATH / catalog hits that are not audited Cards. No execute intent."""
    cards = runtime_cards(config)
    configured_commands = {
        Path(card.launch[0]).name
        for card in cards.values()
        if card.launch
    }
    found: list[DiscoveredTool] = []
    for definition in TOOL_DEFINITIONS:
        if definition.interface == "desktop":
            continue
        if definition.id in cards or definition.command in configured_commands:
            continue
        if shutil.which(definition.command) is None:
            continue
        found.append(DiscoveredTool(id=definition.id, command=definition.command))
    return tuple(found)


def test_capability(config: Config, card_id: str) -> CapabilityTestReport:
    """Login / executable probe only. Does not start delivery or write Cards."""
    cards = runtime_cards(config)
    try:
        card = cards[card_id]
    except KeyError as exc:
        raise DyroError(f"未配置 Capability：{card_id}") from exc
    if card_id in config.adapters:
        checks = test_adapter(config, card_id)
    else:
        checks = ()
    executable = bool(checks) and all(available for _mode, available, _exe in checks)
    hook_surface = _proven_hook_surface(config, card)
    return CapabilityTestReport(
        id=card.id,
        source=card.source,
        executable=executable,
        logged_in=None,
        hook_surface=hook_surface,
        attested_isolation=card.attested_isolation.value,
        cannot_prove=card.cannot_prove,
        checks=checks,
    )


def _proven_hook_surface(config: Config, card: CapabilityCard) -> str:
    declared = card.hook_surface.strip()
    if not declared:
        return ""
    surface = Path(declared)
    if surface.is_absolute() or ".." in surface.parts or surface.parts == (".",):
        return ""
    if declared in {".", "dyro.toml"} or declared.startswith(".dyro"):
        return ""
    path = (config.root / surface).resolve()
    try:
        path.relative_to(config.root.resolve())
    except ValueError:
        return ""
    if not path.is_dir() or path == config.root.resolve():
        return ""
    return declared


def card_payload(card: CapabilityCard) -> dict[str, object]:
    return {
        "id": card.id,
        "kind": card.kind.value,
        "source": card.source,
        "preset": card.preset,
        "attested_isolation": card.attested_isolation.value,
        "trusted_usage": card.trusted_usage,
        "can_prove": list(card.can_prove),
        "cannot_prove": list(card.cannot_prove),
        "intents": list(card.intents),
        "hosts": list(card.hosts),
        "hook_surface": card.hook_surface,
    }

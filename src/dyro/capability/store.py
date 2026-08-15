"""Append Capability Cards to dyro.toml without rewriting the rest."""

from __future__ import annotations

import json

from ..config import CONFIG_NAME, Config, load, validate_id
from ..errors import DyroError, ValidationError
from ..profile import command_adapter, preset_adapter
from ..state import atomic_write_text, exclusive_lock
from .cards import card_from_adapter
from .models import CapabilityCard, CapabilityKind, Isolation


def card_from_preset(card_id: str, preset: str) -> CapabilityCard:
    adapter = preset_adapter(card_id, preset)
    card = card_from_adapter(adapter)
    return CapabilityCard(
        id=card.id,
        kind=CapabilityKind.AGENT,
        launch=card.launch,
        read=card.read,
        write=card.write,
        preset=preset,
        attested_isolation=Isolation.CWD,
        trusted_usage=False,
        can_prove=(),
        cannot_prove=card.cannot_prove,
        intents=card.intents,
        hosts=card.hosts,
        source="capabilities",
    )


def card_from_command(card_id: str, argv: list[str] | tuple[str, ...]) -> CapabilityCard:
    adapter = command_adapter(card_id, argv)
    card = card_from_adapter(adapter)
    return CapabilityCard(
        id=card.id,
        kind=card.kind,
        launch=card.launch,
        read=card.read,
        write=card.write,
        attested_isolation=card.attested_isolation,
        cannot_prove=card.cannot_prove,
        intents=card.intents,
        hosts=card.hosts,
        source="capabilities",
    )


def append_capability(config: Config, card: CapabilityCard, *, dry_run: bool = False) -> None:
    validate_id(card.id, "capability id")
    if dry_run:
        return
    with exclusive_lock(config.root / ".dyro" / "profile.lock"):
        current = load(config.root)
        if card.id in current.adapters or card.id in current.capabilities:
            raise DyroError(f"Capability 或 adapter 已配置：{card.id}")
        if card.kind is CapabilityKind.AGENT and not (card.launch and card.read and card.write):
            raise ValidationError(f"capabilities.{card.id} 的 agent 必须提供 launch/read/write")
        lines = [
            "[[capabilities]]",
            f"id = {json.dumps(card.id, ensure_ascii=False)}",
            f"kind = {json.dumps(card.kind.value, ensure_ascii=False)}",
        ]
        if card.preset:
            lines.append(f"preset = {json.dumps(card.preset, ensure_ascii=False)}")
        lines.extend(
            [
                f"launch = {json.dumps(list(card.launch), ensure_ascii=False)}",
                f"read = {json.dumps(list(card.read), ensure_ascii=False)}",
                f"write = {json.dumps(list(card.write), ensure_ascii=False)}",
                f"attested_isolation = {json.dumps(card.attested_isolation.value, ensure_ascii=False)}",
                f"trusted_usage = {'true' if card.trusted_usage else 'false'}",
                f"can_prove = {json.dumps(list(card.can_prove), ensure_ascii=False)}",
                f"cannot_prove = {json.dumps(list(card.cannot_prove), ensure_ascii=False)}",
                f"intents = {json.dumps(list(card.intents), ensure_ascii=False)}",
                f"hosts = {json.dumps(list(card.hosts), ensure_ascii=False)}",
            ]
        )
        config_file = current.root / CONFIG_NAME
        content = config_file.read_text(encoding="utf-8").rstrip() + "\n\n" + "\n".join(lines) + "\n"
        atomic_write_text(config_file, content)

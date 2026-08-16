"""Parse [[capabilities]], upgrade [adapters.*], and refuse ID collisions."""

from __future__ import annotations

from typing import Any, Mapping

from ..config import Adapter, validate_id
from ..errors import DyroError, ValidationError
from .models import (
    DEFAULT_CANNOT_PROVE,
    CapabilityCard,
    CapabilityKind,
    Isolation,
)


def card_forbids_execute(card: object | None) -> bool:
    """True only when a Card exists and does not grant execute."""
    return card is not None and "execute" not in getattr(card, "intents", ())


def write_capability_denied(
    capabilities: Mapping[str, object] | None, executor: str
) -> bool:
    if not capabilities:
        return False
    return card_forbids_execute(capabilities.get(executor))


def assert_capability_allows_write(config: object, executor: str) -> None:
    cards = getattr(config, "capabilities", None)
    if write_capability_denied(cards, executor):
        raise DyroError(f"Capability {executor} 未授予 execute，不能作为任务执行器")


def card_from_adapter(adapter: Adapter) -> CapabilityCard:
    """Runtime upgrade. Missing Card fields stay fail-closed."""
    return CapabilityCard(
        id=adapter.id,
        kind=CapabilityKind.AGENT,
        launch=adapter.launch,
        read=adapter.read,
        write=adapter.write,
        attested_isolation=Isolation.CWD,
        trusted_usage=False,
        can_prove=(),
        cannot_prove=DEFAULT_CANNOT_PROVE,
        intents=("observe", "execute"),
        hosts=("cli",),
        source="adapter",
    )


def parse_capability_tables(raw: Any) -> dict[str, CapabilityCard]:
    if raw in (None, {}):
        return {}
    if not isinstance(raw, list):
        raise ValidationError("capabilities 必须是 [[capabilities]] 数组，不能是 [capabilities] 表")
    cards: dict[str, CapabilityCard] = {}
    for index, entry in enumerate(raw):
        card = parse_capability_entry(entry, label=f"capabilities[{index}]")
        if card.id in cards:
            raise ValidationError(f"capabilities 重复 ID：{card.id}")
        cards[card.id] = card
    return cards


def parse_capability_entry(entry: Any, *, label: str) -> CapabilityCard:
    if not isinstance(entry, dict):
        raise ValidationError(f"{label} 必须是表")
    if "env" in entry:
        raise ValidationError(f"{label} 不得声明环境变量；认证使用本机已登录会话")
    card_id = validate_id(str(entry.get("id", "") or ""), f"{label}.id")
    kind_raw = str(entry.get("kind", "agent") or "agent")
    try:
        kind = CapabilityKind(kind_raw)
    except ValueError as exc:
        raise ValidationError(f"{label}.kind 无效：{kind_raw}") from exc
    isolation_raw = str(entry.get("attested_isolation", "cwd") or "cwd")
    try:
        isolation = Isolation(isolation_raw)
    except ValueError as exc:
        raise ValidationError(f"{label}.attested_isolation 无效：{isolation_raw}") from exc
    launch = _argv(entry.get("launch"), f"{label}.launch", required=kind is CapabilityKind.AGENT)
    read = _argv(entry.get("read", entry.get("launch")), f"{label}.read", required=kind is CapabilityKind.AGENT)
    write = _argv(entry.get("write", entry.get("launch")), f"{label}.write", required=kind is CapabilityKind.AGENT)
    can_prove = _string_list(entry.get("can_prove", []), f"{label}.can_prove")
    cannot_prove = _string_list(entry.get("cannot_prove", list(DEFAULT_CANNOT_PROVE)), f"{label}.cannot_prove")
    intents = _string_list(entry.get("intents", ["observe", "execute"]), f"{label}.intents")
    hosts = _string_list(entry.get("hosts", ["cli"]), f"{label}.hosts")
    preset = entry.get("preset", "")
    if preset is None:
        preset = ""
    if not isinstance(preset, str):
        raise ValidationError(f"{label}.preset 必须是字符串")
    hook_surface = entry.get("hook_surface", "")
    if hook_surface is None:
        hook_surface = ""
    if not isinstance(hook_surface, str):
        raise ValidationError(f"{label}.hook_surface 必须是字符串")
    trusted = entry.get("trusted_usage", False)
    if not isinstance(trusted, bool):
        raise ValidationError(f"{label}.trusted_usage 必须是布尔值")
    return CapabilityCard(
        id=card_id,
        kind=kind,
        launch=launch,
        read=read,
        write=write,
        preset=preset,
        attested_isolation=isolation,
        trusted_usage=trusted,
        can_prove=can_prove,
        cannot_prove=cannot_prove,
        intents=intents,
        hosts=hosts,
        source="capabilities",
        hook_surface=hook_surface,
    )


def merge_capability_plane(
    adapters: Mapping[str, Adapter],
    cards: Mapping[str, CapabilityCard],
) -> tuple[dict[str, Adapter], dict[str, CapabilityCard]]:
    overlap = sorted(set(adapters) & set(cards))
    if overlap:
        raise ValidationError(f"adapters 与 capabilities ID 冲突：{', '.join(overlap)}")
    merged_adapters = dict(adapters)
    for card in cards.values():
        if card.launch and card.read and card.write:
            merged_adapters[card.id] = Adapter(card.id, card.launch, card.read, card.write)
    merged_cards = {adapter_id: card_from_adapter(adapter) for adapter_id, adapter in adapters.items()}
    merged_cards.update(cards)
    return merged_adapters, merged_cards


def _argv(value: Any, label: str, *, required: bool) -> tuple[str, ...]:
    if value is None:
        if required:
            raise ValidationError(f"{label} 必须是非空 argv 数组")
        return ()
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise ValidationError(f"{label} 必须是非空 argv 数组")
    return tuple(value)


def _string_list(value: Any, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ValidationError(f"{label} 必须是字符串数组")
    return tuple(value)

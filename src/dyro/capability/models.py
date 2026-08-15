"""Capability Card types. Adapters upgrade into Cards; discovery is not a Card."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from ..errors import ValidationError


class CapabilityKind(str, Enum):
    AGENT = "agent"
    GATE = "gate"
    REVIEWER = "reviewer"
    TRIGGER = "trigger"
    TOOL = "tool"


class Isolation(str, Enum):
    NONE = "none"
    CWD = "cwd"
    WORKTREE = "worktree"
    OS_SANDBOX = "os_sandbox"
    EXTERNAL_RUNNER = "external_runner"


class Intent(str, Enum):
    OBSERVE = "observe"
    EXECUTE = "execute"
    REVIEW = "review"
    SIGN = "sign"
    INTEGRATE = "integrate"
    PUBLISH = "publish"


DEFAULT_CANNOT_PROVE = ("done", "merge")
PROOF_KINDS = frozenset(
    {"gate_log", "review_verdict", "signoff", "integration_heads", "action_receipt"}
)
CARD_SOURCES = frozenset({"adapter", "capabilities"})


def _frozen_strings(value: Any, label: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        raise TypeError(f"{label} 必须是集合，不能是字符串")
    items = tuple(value)
    if not all(isinstance(item, str) for item in items):
        raise TypeError(f"{label} 必须只包含字符串")
    return items


def _frozen_argv(value: Any, label: str) -> tuple[str, ...]:
    items = _frozen_strings(value, label)
    if items and not all(item for item in items):
        raise ValidationError(f"{label} 不能包含空参数")
    return items


@dataclass(frozen=True)
class CapabilityCard:
    id: str
    kind: CapabilityKind
    launch: tuple[str, ...]
    read: tuple[str, ...]
    write: tuple[str, ...]
    preset: str = ""
    attested_isolation: Isolation = Isolation.CWD
    trusted_usage: bool = False
    can_prove: tuple[str, ...] = ()
    cannot_prove: tuple[str, ...] = DEFAULT_CANNOT_PROVE
    intents: tuple[str, ...] = (Intent.OBSERVE.value, Intent.EXECUTE.value)
    hosts: tuple[str, ...] = ("cli",)
    source: str = "capabilities"
    hook_surface: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.kind, CapabilityKind):
            raise TypeError("CapabilityCard.kind 必须是 CapabilityKind")
        if not isinstance(self.attested_isolation, Isolation):
            raise TypeError("CapabilityCard.attested_isolation 必须是 Isolation")
        if self.source not in CARD_SOURCES:
            raise ValidationError(f"CapabilityCard.source 无效：{self.source}")
        object.__setattr__(self, "launch", _frozen_argv(self.launch, "launch"))
        object.__setattr__(self, "read", _frozen_argv(self.read, "read"))
        object.__setattr__(self, "write", _frozen_argv(self.write, "write"))
        object.__setattr__(self, "can_prove", _frozen_strings(self.can_prove, "can_prove"))
        cannot_prove = _frozen_strings(self.cannot_prove, "cannot_prove")
        missing = [item for item in DEFAULT_CANNOT_PROVE if item not in cannot_prove]
        object.__setattr__(self, "cannot_prove", cannot_prove + tuple(missing))
        object.__setattr__(self, "intents", _frozen_strings(self.intents, "intents"))
        object.__setattr__(self, "hosts", _frozen_strings(self.hosts, "hosts"))
        unknown_prove = [item for item in self.can_prove if item not in PROOF_KINDS]
        if unknown_prove:
            raise ValidationError(f"can_prove 只能填写 Proof kind：{', '.join(unknown_prove)}")
        unknown_intents = [item for item in self.intents if item not in {intent.value for intent in Intent}]
        if unknown_intents:
            raise ValidationError(f"intents 无效：{', '.join(unknown_intents)}")
        if not self.hosts:
            raise ValidationError("hosts 不能为空")
        if not isinstance(self.trusted_usage, bool):
            raise TypeError("trusted_usage 必须是布尔值")
        if not isinstance(self.hook_surface, str) or not isinstance(self.preset, str):
            raise TypeError("hook_surface 与 preset 必须是字符串")


@dataclass(frozen=True)
class DiscoveredTool:
    id: str
    command: str
    state: str = "discovered_unintegrated"


@dataclass(frozen=True)
class CapabilityTestReport:
    id: str
    source: str
    executable: bool
    logged_in: bool | None
    hook_surface: str
    attested_isolation: str
    cannot_prove: tuple[str, ...]
    checks: tuple[tuple[str, bool, str], ...]

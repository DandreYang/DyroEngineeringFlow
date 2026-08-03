"""Bounded Trigger observations and deterministic retry scheduling.

Triggers only state when a planner should reconsider immutable facts.  This
module contains no Task mutation, evidence import, review, merge, push, or
network client. An extension process must be described by an allowlisted
``ProviderDescriptor`` and feed its bounded JSON bytes through
:func:`parse_provider_observation`; it cannot receive control-plane authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import json
import re
from typing import Any

from ..config import validate_id
from .models import TriggerObservation, TriggerState


MAX_PROVIDER_BYTES = 65_536
MAX_SUMMARY_LENGTH = 512
_SECRET_MARKER = re.compile(r"(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,})")


class TriggerKind(str, Enum):
    TIME_DUE = "time_due"
    TASK_STATE = "task_state"
    DECISION_STATE = "decision_state"
    MANUAL_SIGNAL = "manual_signal"
    LOCAL_REF = "local_ref"
    PROVIDER = "provider"


class TriggerErrorKind(str, Enum):
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    AUTH_MISSING = "auth_missing"
    INVALID_OUTPUT = "invalid_output"
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class ProviderDescriptor:
    """A declarative boundary for an externally run observation provider.

    The core process never imports provider code and this type intentionally
    has no command or URL field. A later adapter may map a trusted,
    allowlisted descriptor ID to a bounded subprocess invocation, then pass
    its stdout to this module. This keeps arbitrary shell and HTTP triggers
    out of the continuation core.
    """

    id: str
    trigger_ids: tuple[str, ...]
    timeout_seconds: int = 30
    maximum_bytes: int = MAX_PROVIDER_BYTES

    def __post_init__(self) -> None:
        validate_id(self.id, "provider ID")
        try:
            trigger_ids = tuple(self.trigger_ids)
        except TypeError as exc:
            raise TypeError("ProviderDescriptor.trigger_ids 必须是 Trigger ID 集合") from exc
        if not trigger_ids:
            raise ValueError("ProviderDescriptor.trigger_ids 不能为空")
        for trigger_id in trigger_ids:
            if not isinstance(trigger_id, str):
                raise TypeError("ProviderDescriptor.trigger_ids 必须只包含字符串")
            validate_id(trigger_id, "Trigger ID")
        if len(set(trigger_ids)) != len(trigger_ids):
            raise ValueError("ProviderDescriptor.trigger_ids 不能重复")
        object.__setattr__(self, "trigger_ids", trigger_ids)
        if type(self.timeout_seconds) is not int or not 1 <= self.timeout_seconds <= 300:
            raise ValueError("ProviderDescriptor.timeout_seconds 必须在 1 到 300 秒之间")
        if type(self.maximum_bytes) is not int or not 1 <= self.maximum_bytes <= MAX_PROVIDER_BYTES:
            raise ValueError("ProviderDescriptor.maximum_bytes 超出允许范围")


def _utc(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise TypeError(f"{label} 必须是带时区的 datetime")
    return value.astimezone(timezone.utc)


def _facts(value: Any, label: str) -> tuple[tuple[str, str], ...]:
    if isinstance(value, (str, bytes)):
        raise TypeError(f"{label} 必须是键值对集合")
    try:
        raw = tuple(value)
    except TypeError as exc:
        raise TypeError(f"{label} 必须是键值对集合") from exc
    facts: list[tuple[str, str]] = []
    for item in raw:
        try:
            pair = tuple(item)
        except TypeError as exc:
            raise TypeError(f"{label} 包含无效键值对") from exc
        if len(pair) != 2 or not all(isinstance(part, str) and part for part in pair):
            raise TypeError(f"{label} 必须包含非空字符串键值对")
        facts.append((pair[0], pair[1]))
    return tuple(sorted(facts))


@dataclass(frozen=True)
class TriggerConfig:
    id: str
    kind: TriggerKind
    not_before: datetime | None = None
    min_interval_seconds: int = 30
    max_interval_seconds: int = 900

    def __post_init__(self) -> None:
        validate_id(self.id, "Trigger ID")
        if not isinstance(self.kind, TriggerKind):
            raise TypeError("TriggerConfig.kind 必须是 TriggerKind")
        for field in ("min_interval_seconds", "max_interval_seconds"):
            value = getattr(self, field)
            if type(value) is not int or value < 1:
                raise TypeError(f"TriggerConfig.{field} 必须是正整数")
        if self.min_interval_seconds > self.max_interval_seconds:
            raise TypeError("TriggerConfig.min_interval_seconds 不能大于 max_interval_seconds")
        if self.not_before is not None:
            object.__setattr__(self, "not_before", _utc(self.not_before, "TriggerConfig.not_before"))


@dataclass(frozen=True)
class TriggerProbeInput:
    config: TriggerConfig
    now: datetime
    current_facts: tuple[tuple[str, str], ...] = ()
    previous_facts: tuple[tuple[str, str], ...] = ()
    manual_signal: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.config, TriggerConfig):
            raise TypeError("TriggerProbeInput.config 必须是 TriggerConfig")
        object.__setattr__(self, "now", _utc(self.now, "TriggerProbeInput.now"))
        object.__setattr__(self, "current_facts", _facts(self.current_facts, "TriggerProbeInput.current_facts"))
        object.__setattr__(self, "previous_facts", _facts(self.previous_facts, "TriggerProbeInput.previous_facts"))
        if not isinstance(self.manual_signal, str) or len(self.manual_signal) > MAX_SUMMARY_LENGTH:
            raise TypeError("TriggerProbeInput.manual_signal 无效")


@dataclass(frozen=True)
class TriggerProbeSchedule:
    next_probe_at: datetime | None
    unchanged_cycles: int
    disabled: bool = False


def _observation(config: TriggerConfig, state: TriggerState, summary: str, now: datetime) -> TriggerObservation:
    return TriggerObservation(
        trigger_id=config.id,
        state=state,
        summary=summary,
        observed_at=now,
    )


def probe_builtin(input: TriggerProbeInput) -> TriggerObservation:
    """Observe one built-in Trigger using only caller-supplied facts."""
    config = input.config
    if config.kind is TriggerKind.TIME_DUE:
        due = config.not_before is not None and input.now >= config.not_before
        return _observation(config, TriggerState.SATISFIED if due else TriggerState.PENDING, "time_due" if due else "time_waiting", input.now)
    if config.kind in {TriggerKind.TASK_STATE, TriggerKind.LOCAL_REF}:
        changed = input.current_facts != input.previous_facts and bool(input.current_facts or input.previous_facts)
        return _observation(config, TriggerState.SATISFIED if changed else TriggerState.PENDING, "state_changed" if changed else "state_unchanged", input.now)
    if config.kind is TriggerKind.DECISION_STATE:
        resolved = any(value == "resolved" for _, value in input.current_facts)
        return _observation(config, TriggerState.SATISFIED if resolved else TriggerState.PENDING, "decision_resolved" if resolved else "decision_open", input.now)
    if config.kind is TriggerKind.MANUAL_SIGNAL:
        signalled = bool(input.manual_signal)
        return _observation(config, TriggerState.SATISFIED if signalled else TriggerState.PENDING, "manual_signal" if signalled else "signal_absent", input.now)
    return _observation(config, TriggerState.DISABLED, "provider_requires_bounded_adapter", input.now)


def _jitter_seconds(config: TriggerConfig, cycles: int, interval: int) -> int:
    window = max(1, interval // 10)
    digest = hashlib.sha256(f"{config.id}:{cycles}:{interval}".encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % (window + 1)


def next_probe_schedule(
    config: TriggerConfig,
    observation: TriggerObservation,
    *,
    unchanged_cycles: int,
    now: datetime,
    changed: bool = False,
    error_kind: TriggerErrorKind | None = None,
) -> TriggerProbeSchedule:
    """Compute bounded exponential backoff without sampling a new clock."""
    if not isinstance(config, TriggerConfig) or observation.trigger_id != config.id:
        raise ValueError("Trigger 配置与观测不匹配")
    if type(unchanged_cycles) is not int or unchanged_cycles < 0:
        raise TypeError("unchanged_cycles 必须是非负整数")
    if error_kind is not None and not isinstance(error_kind, TriggerErrorKind):
        raise TypeError("error_kind 必须是 TriggerErrorKind 或 None")
    now_utc = _utc(now, "now")
    if error_kind in {TriggerErrorKind.PERMANENT, TriggerErrorKind.AUTH_MISSING, TriggerErrorKind.INVALID_OUTPUT}:
        return TriggerProbeSchedule(next_probe_at=None, unchanged_cycles=unchanged_cycles, disabled=True)
    if observation.state is TriggerState.DISABLED:
        return TriggerProbeSchedule(next_probe_at=None, unchanged_cycles=unchanged_cycles, disabled=True)
    # A planner must explicitly establish an edge before waking immediately.
    # Treating a level-triggered condition (for example, time_due after its
    # deadline) as a change would otherwise spin with a zero delay forever.
    if changed:
        return TriggerProbeSchedule(next_probe_at=now_utc, unchanged_cycles=0)
    cycles = unchanged_cycles + 1
    interval = min(config.max_interval_seconds, config.min_interval_seconds * (2 ** min(cycles - 1, 30)))
    interval = min(config.max_interval_seconds, interval + _jitter_seconds(config, cycles, interval))
    return TriggerProbeSchedule(next_probe_at=now_utc + timedelta(seconds=interval), unchanged_cycles=cycles)


def _parse_time(value: object, label: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} 必须是带时区的 ISO-8601 时间")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} 必须是带时区的 ISO-8601 时间") from exc
    return _utc(parsed, label)


def parse_provider_observation(
    payload: bytes,
    *,
    trigger_id: str,
    observed_at: datetime,
    maximum_bytes: int = MAX_PROVIDER_BYTES,
) -> TriggerObservation:
    """Validate an extension's bounded JSON output without trusting its content.

    This parser accepts no delivery-state fields.  A validated observation can
    request a replan only; it never represents Task, review, evidence, merge,
    or push authority.
    """
    validate_id(trigger_id, "Trigger ID")
    if type(maximum_bytes) is not int or maximum_bytes < 1 or maximum_bytes > MAX_PROVIDER_BYTES:
        raise ValueError("maximum_bytes 超出允许范围")
    if not isinstance(payload, bytes) or len(payload) > maximum_bytes:
        raise ValueError("provider 输出超过允许大小")
    try:
        raw = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("provider 输出不是有效 JSON") from exc
    if not isinstance(raw, dict):
        raise ValueError("provider 输出必须是对象")
    allowed = {"schema_version", "state", "summary", "evidence_ref", "next_probe_at"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"provider 输出包含未知字段：{', '.join(unknown)}")
    if type(raw.get("schema_version")) is not int or raw["schema_version"] != 1:
        raise ValueError("provider 输出必须使用 schema_version = 1")
    try:
        state = TriggerState(raw.get("state"))
    except (TypeError, ValueError) as exc:
        raise ValueError("provider 输出 state 无效") from exc
    summary = raw.get("summary")
    if not isinstance(summary, str) or not summary or len(summary) > MAX_SUMMARY_LENGTH:
        raise ValueError("provider 输出 summary 无效")
    if _SECRET_MARKER.search(summary) or any(ord(character) < 32 for character in summary):
        raise ValueError("provider 输出 summary 疑似包含机密")
    evidence_ref = raw.get("evidence_ref", "")
    if not isinstance(evidence_ref, str) or len(evidence_ref) > MAX_SUMMARY_LENGTH or ".." in evidence_ref or evidence_ref.startswith("/"):
        raise ValueError("provider 输出 evidence_ref 无效")
    if _SECRET_MARKER.search(evidence_ref) or any(ord(character) < 32 for character in evidence_ref):
        raise ValueError("provider 输出 evidence_ref 疑似包含机密")
    observed_at_utc = _utc(observed_at, "observed_at")
    next_probe_at = _parse_time(raw.get("next_probe_at"), "next_probe_at")
    if next_probe_at is not None and next_probe_at < observed_at_utc:
        raise ValueError("provider 输出 next_probe_at 不能早于 observed_at")
    return TriggerObservation(
        trigger_id=trigger_id,
        state=state,
        summary=summary,
        evidence_ref=evidence_ref,
        observed_at=observed_at_utc,
        next_probe_at=next_probe_at,
    )

"""Workspace overlay live-event log: ``.dyro/events.jsonl``.

This is not Objective ``events.jsonl`` and not the delivery ledger.  Rows are
append-only, one ``kind`` each.  Truncation, replacement, or a partial last
line fail closed: readers refuse the log and writers refuse to invent the
missing rows.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from .config import Config
from .errors import DyroError, ValidationError
from .state import append_text, exclusive_lock


EVENTS_FILE = ".dyro/events.jsonl"
EVENTS_LOCK = ".dyro/events.lock"
MAX_EVENT_LOG_BYTES = 2 * 1024 * 1024
EVENT_KINDS = frozenset(
    {
        "spawn",
        "merge",
        "sync",
        "task_status",
        "objective_wave",
        "dispatch",
        "board",
        "signal",
        "host_seed",
    }
)
DEFAULT_EVENT_LIMIT = 50
MAX_EVENT_LIMIT = 100
_MAX_FACTS = 16
_MAX_FACT_KEY = 40
_MAX_FACT_STRING = 80
_SAFE_FACT = frozenset("abcdefghijklmnopqrstuvwxyz0123456789._-")
_SAFE_ID = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-")


class EventLogError(DyroError):
    """Stable, path-free failure while reading or appending the event log."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def events_path(config: Config) -> Path:
    return config.root / EVENTS_FILE


def _utc(clock: Callable[[], datetime] | None) -> datetime:
    value = clock() if clock is not None else datetime.now(timezone.utc)
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValidationError("事件时钟必须提供带时区的 datetime")
    return value.astimezone(timezone.utc)


def _safe_token(value: object, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise EventLogError("EVENT_WRITE_INVALID")
    if value == "":
        if allow_empty:
            return ""
        raise EventLogError("EVENT_WRITE_INVALID")
    if len(value) > 80 or value[0] in "._-" or any(char not in _SAFE_ID for char in value):
        raise EventLogError("EVENT_WRITE_INVALID")
    return value


def _clean_fact_key(key: object) -> str:
    if not isinstance(key, str) or not key or len(key) > _MAX_FACT_KEY:
        raise EventLogError("EVENT_WRITE_INVALID")
    if any(char not in _SAFE_FACT for char in key):
        raise EventLogError("EVENT_WRITE_INVALID")
    return key


def _clean_fact_value(value: object) -> str | int | bool:
    if type(value) is bool:
        return value
    if type(value) is int and not isinstance(value, bool) and 0 <= value <= 1_000_000:
        return value
    if isinstance(value, str):
        if len(value) > _MAX_FACT_STRING:
            raise EventLogError("EVENT_WRITE_INVALID")
        if value == "":
            return ""
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise EventLogError("EVENT_WRITE_INVALID")
        lowered = value.lower()
        if any(token in lowered for token in ("/", "\\", "http:", "https:", "token=", "secret")):
            raise EventLogError("EVENT_WRITE_INVALID")
        return value
    raise EventLogError("EVENT_WRITE_INVALID")


def _clean_facts(facts: Mapping[str, object] | None) -> dict[str, str | int | bool]:
    raw = dict(facts or {})
    if len(raw) > _MAX_FACTS:
        raise EventLogError("EVENT_WRITE_INVALID")
    cleaned: dict[str, str | int | bool] = {}
    for key, value in raw.items():
        cleaned[_clean_fact_key(key)] = _clean_fact_value(value)
    return cleaned


def _decode_event(raw: str) -> dict[str, object]:
    try:
        decoded: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise EventLogError("EVENT_LOG_INVALID") from exc
    if not isinstance(decoded, dict):
        raise EventLogError("EVENT_LOG_INVALID")
    seq = decoded.get("seq")
    event_id = decoded.get("id")
    kind = decoded.get("kind")
    at = decoded.get("at")
    actor = decoded.get("actor")
    subject = decoded.get("subject")
    family = decoded.get("family")
    facts = decoded.get("facts")
    if (
        type(seq) is not int
        or seq < 1
        or not isinstance(event_id, str)
        or event_id != f"evt_{seq}"
        or not isinstance(kind, str)
        or not isinstance(at, str)
        or not isinstance(actor, str)
        or not isinstance(subject, str)
        or not isinstance(family, str)
        or not isinstance(facts, dict)
    ):
        raise EventLogError("EVENT_LOG_INVALID")
    return {
        "seq": seq,
        "id": event_id,
        "kind": kind,
        "at": at,
        "actor": actor,
        "subject": subject,
        "family": family,
        "facts": dict(facts),
    }


def _read_locked_records(path: Path) -> list[dict[str, object]]:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise EventLogError("EVENT_LOG_INVALID")
    if not path.exists():
        return []
    try:
        size = path.stat().st_size
        if size > MAX_EVENT_LOG_BYTES:
            raise EventLogError("EVENT_LOG_INVALID")
        text = path.read_text(encoding="utf-8")
    except EventLogError:
        raise
    except OSError as exc:
        raise EventLogError("EVENT_LOG_INVALID") from exc
    if not text:
        return []
    if not text.endswith("\n"):
        raise EventLogError("EVENT_LOG_INVALID")
    records: list[dict[str, object]] = []
    expected = 1
    for line in text.splitlines():
        if not line:
            raise EventLogError("EVENT_LOG_INVALID")
        record = _decode_event(line)
        if record["seq"] != expected:
            raise EventLogError("EVENT_LOG_INVALID")
        records.append(record)
        expected += 1
    return records


def read_overlay_events(config: object) -> tuple[tuple[dict[str, object], ...], bool]:
    """Read overlay events without creating a lock or inventing rows.

    A missing file is a complete empty log.  Truncation, replacement, or
    an unreadable path is incomplete: callers must fail closed to the
    snapshot and must not replay a readable prefix.
    """
    try:
        path = events_path(config)  # type: ignore[arg-type]
        if path.is_symlink():
            return (), False
        if not path.exists():
            return (), True
        return tuple(_read_locked_records(path)), True
    except (EventLogError, OSError, TypeError, AttributeError):
        return (), False


def read_events_fail_closed(config: object) -> tuple[dict[str, object], ...]:
    """Read overlay events without creating a lock or inventing rows.

    A missing file is empty.  Truncation, replacement, or an unreadable
    path returns no rows.  Callers must not treat emptiness as proof that
    no work happened.
    """
    records, complete = read_overlay_events(config)
    return records if complete else ()


def read_events(
    config: Config,
    *,
    after_seq: int = 0,
    limit: int = DEFAULT_EVENT_LIMIT,
) -> tuple[tuple[dict[str, object], ...], int]:
    """Return events with ``seq > after_seq``.

    ``after_seq`` is a bare integer used by the HMAC cursor layer.  A missing
    file is an empty log.  A truncated or replaced sequence is not repaired.
    """
    if type(after_seq) is not int or after_seq < 0:
        raise EventLogError("EVENT_CURSOR_INVALID")
    if type(limit) is not int or not 1 <= limit <= MAX_EVENT_LIMIT:
        raise EventLogError("EVENT_LIMIT_INVALID")
    path = events_path(config)
    with exclusive_lock(config.root / EVENTS_LOCK):
        records = _read_locked_records(path)
    if after_seq > len(records):
        raise EventLogError("EVENT_CURSOR_INVALID")
    if after_seq:
        if records[after_seq - 1]["seq"] != after_seq:
            raise EventLogError("EVENT_CURSOR_INVALID")
    selected = records[after_seq : after_seq + limit]
    last_seq = records[-1]["seq"] if records else 0
    return tuple(selected), last_seq


def event_at(config: Config, seq: int) -> dict[str, object] | None:
    if type(seq) is not int or seq < 1:
        return None
    path = events_path(config)
    with exclusive_lock(config.root / EVENTS_LOCK):
        records = _read_locked_records(path)
    if seq > len(records):
        return None
    return records[seq - 1]


def overlay_lock(config: Config):
    """Shared overlay lock for ``events.jsonl`` and family channel writes."""
    return exclusive_lock(config.root / EVENTS_LOCK)


def read_event_records_locked(config: Config) -> list[dict[str, object]]:
    """Read the event log.  Caller must already hold ``overlay_lock``."""
    return _read_locked_records(events_path(config))


def append_event_locked(
    config: Config,
    *,
    kind: str,
    actor: str,
    subject: str,
    family: str = "",
    facts: Mapping[str, object] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, object]:
    """Append one event.  Caller must already hold ``overlay_lock``."""
    if kind not in EVENT_KINDS:
        raise EventLogError("EVENT_WRITE_INVALID")
    actor = _safe_token(actor)
    subject = _safe_token(subject)
    family = _safe_token(family, allow_empty=True)
    cleaned = _clean_facts(facts)
    stamp = _utc(clock).strftime("%Y-%m-%dT%H:%M:%SZ")
    path = events_path(config)
    try:
        records = _read_locked_records(path)
        seq = (records[-1]["seq"] + 1) if records else 1
        record = {
            "seq": seq,
            "id": f"evt_{seq}",
            "kind": kind,
            "at": stamp,
            "actor": actor,
            "subject": subject,
            "family": family,
            "facts": cleaned,
        }
        append_text(
            path,
            json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n",
        )
    except EventLogError:
        raise
    except OSError as exc:
        raise EventLogError("EVENT_WRITE_FAILED") from exc
    return record


def append_event(
    config: Config,
    *,
    kind: str,
    actor: str,
    subject: str,
    family: str = "",
    facts: Mapping[str, object] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, object]:
    """Append one typed event.  Dry-run callers must not invoke this."""
    try:
        with overlay_lock(config):
            return append_event_locked(
                config,
                kind=kind,
                actor=actor,
                subject=subject,
                family=family,
                facts=facts,
                clock=clock,
            )
    except EventLogError:
        raise
    except OSError as exc:
        raise EventLogError("EVENT_WRITE_FAILED") from exc

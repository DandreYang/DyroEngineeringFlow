"""Console DTO and HMAC cursor for workspace ``.dyro/events.jsonl``."""

from __future__ import annotations

import base64
from collections.abc import Mapping
import hashlib
import hmac
import json
from typing import Any

from ..canonical import canonical_json_bytes
from ..config import Config
from ..events import (
    DEFAULT_EVENT_LIMIT,
    EVENT_KINDS,
    MAX_EVENT_LIMIT,
    EventLogError,
    event_at,
    read_events,
)
from .overview import ConsoleOverviewError
from .redaction import REDACTED, safe_id


_CURSOR_SCHEMA = 1
_CURSOR_MAX_LENGTH = 512


def _stable_json(value: object) -> bytes:
    return canonical_json_bytes(value)


def encode_event_cursor(
    secret: bytes,
    *,
    after_seq: int,
    event_id: str,
) -> str:
    body = _stable_json(
        {
            "schema_version": _CURSOR_SCHEMA,
            "after": after_seq,
            "event_id": event_id,
        }
    )
    signature = hmac.new(secret, body, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(body + signature).rstrip(b"=").decode("ascii")


def decode_event_cursor(secret: bytes, value: str) -> tuple[int, str]:
    if not isinstance(value, str) or not value or len(value) > _CURSOR_MAX_LENGTH:
        raise ConsoleOverviewError("EVENT_CURSOR_INVALID")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, UnicodeError) as exc:
        raise ConsoleOverviewError("EVENT_CURSOR_INVALID") from exc
    if len(raw) <= hashlib.sha256().digest_size:
        raise ConsoleOverviewError("EVENT_CURSOR_INVALID")
    body, signature = raw[:-32], raw[-32:]
    expected = hmac.new(secret, body, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, signature):
        raise ConsoleOverviewError("EVENT_CURSOR_INVALID")
    try:
        decoded: Any = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConsoleOverviewError("EVENT_CURSOR_INVALID") from exc
    if (
        not isinstance(decoded, dict)
        or set(decoded) != {"schema_version", "after", "event_id"}
        or decoded["schema_version"] != _CURSOR_SCHEMA
        or type(decoded["after"]) is not int
        or decoded["after"] < 1
        or not isinstance(decoded["event_id"], str)
    ):
        raise ConsoleOverviewError("EVENT_CURSOR_INVALID")
    return decoded["after"], decoded["event_id"]


def _safe_fact_value(value: object) -> str | int | bool | None:
    if type(value) is bool:
        return value
    if type(value) is int and not isinstance(value, bool) and 0 <= value <= 1_000_000:
        return value
    if isinstance(value, str) and len(value) <= 80:
        token = safe_id(value)
        if value == "" or token != REDACTED:
            return value if value == "" else token
        if all(ord(char) >= 32 and ord(char) != 127 for char in value):
            lowered = value.lower()
            if not any(item in lowered for item in ("/", "\\", "http:", "https:", "token", "secret")):
                return value
    return None


def project_event(record: Mapping[str, object]) -> dict[str, object]:
    kind = record.get("kind")
    seq = record.get("seq")
    event_id = record.get("id")
    at = record.get("at")
    if type(seq) is not int or not isinstance(event_id, str) or not isinstance(at, str):
        return {
            "seq": 0,
            "id": "evt_0",
            "kind": "EVENT_REDACTED",
            "at": "",
            "actor": "",
            "subject": "",
            "family": "",
            "facts": {},
        }
    if kind not in EVENT_KINDS:
        return {
            "seq": seq,
            "id": event_id,
            "kind": "EVENT_REDACTED",
            "at": at,
            "actor": "",
            "subject": "",
            "family": "",
            "facts": {},
        }
    facts_in = record.get("facts")
    facts: dict[str, str | int | bool] = {}
    if isinstance(facts_in, dict):
        for key, value in facts_in.items():
            if not isinstance(key, str) or safe_id(key) == REDACTED:
                continue
            cleaned = _safe_fact_value(value)
            if cleaned is not None:
                facts[key] = cleaned
    actor = safe_id(record.get("actor"))
    subject = safe_id(record.get("subject"))
    family_raw = record.get("family")
    family = "" if family_raw == "" else safe_id(family_raw)
    return {
        "seq": seq,
        "id": event_id,
        "kind": kind,
        "at": at,
        "actor": "" if actor == REDACTED else actor,
        "subject": "" if subject == REDACTED else subject,
        "family": "" if family == REDACTED else family,
        "facts": facts,
    }


def event_page(
    config: Config,
    *,
    secret: bytes,
    after: str | None,
    limit: int = DEFAULT_EVENT_LIMIT,
) -> dict[str, object]:
    if type(limit) is not int or not 1 <= limit <= MAX_EVENT_LIMIT:
        raise ConsoleOverviewError("EVENT_LIMIT_INVALID")
    after_seq = 0
    if after:
        after_seq, event_id = decode_event_cursor(secret, after)
        current = event_at(config, after_seq)
        if current is None or current.get("id") != event_id:
            raise ConsoleOverviewError("EVENT_CURSOR_INVALID")
    try:
        records, _last_seq = read_events(config, after_seq=after_seq, limit=limit)
    except EventLogError as exc:
        raise ConsoleOverviewError(exc.code) from exc
    events = [project_event(item) for item in records]
    if events:
        next_cursor = encode_event_cursor(
            secret,
            after_seq=int(events[-1]["seq"]),
            event_id=str(events[-1]["id"]),
        )
    elif after:
        next_cursor = after
    else:
        next_cursor = None
    return {
        "events": events,
        "next_cursor": next_cursor,
    }

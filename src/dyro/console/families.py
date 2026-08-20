"""Console DTO for one-level family graphs and the family channel."""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
import hashlib
import hmac
import json
from typing import Any

from ..canonical import canonical_json_bytes
from ..config import Config
from ..families import (
    CHANNEL_KINDS,
    DEFAULT_CHANNEL_LIMIT,
    HUMAN_POST_KINDS,
    IMAGE_MEDIA_TYPES,
    MAX_CHANNEL_LIMIT,
    OPERATOR_ID,
    OPERATOR_POST_KINDS,
    FamilyArtifactError,
    FamilyChannelError,
    ack_channel_message,
    channel_at,
    family_children,
    family_graph,
    family_ids,
    family_members,
    find_channel_message,
    infer_post_family,
    line_records,
    list_family_artifacts,
    post_channel_message,
    read_acks,
    read_family_artifact,
    read_family_artifact_bytes,
    read_visible_channel,
    retracted_message_ids,
    unread_by_member,
)
from .overview import ConsoleOverviewError
from .redaction import REDACTED, safe_id


_CURSOR_SCHEMA = 2
_CURSOR_MAX_LENGTH = 512
_DIGEST_HEX = frozenset("0123456789abcdef")
_FILTER_KEYS = frozenset({"unacked", "kind", "from"})


def _line_id(value: object) -> str:
    token = safe_id(value)
    return "" if token == REDACTED else token


def _member_token(value: object) -> str:
    if value == OPERATOR_ID:
        return OPERATOR_ID
    return _line_id(value)


def family_badges(
    lines: Sequence[Mapping[str, object]],
    tasks: Sequence[Mapping[str, object]],
    *,
    unread: Mapping[str, int] | None = None,
) -> dict[str, dict[str, object]]:
    """P1 git badges stay uninspected.  Unread comes from the overlay channel."""
    in_progress: set[str] = set()
    for task in tasks:
        if task.get("status") == "in_progress":
            line_id = _line_id(task.get("line"))
            if line_id:
                in_progress.add(line_id)
    marks = dict(unread or {})
    badges: dict[str, dict[str, object]] = {}
    for line in lines:
        line_id = _line_id(line.get("id"))
        if not line_id:
            continue
        badges[line_id] = {
            "dirty": False,
            "missing_origin": False,
            "in_progress": line_id in in_progress,
            "unread": marks.get(line_id, 0) if type(marks.get(line_id, 0)) is int else 0,
        }
    badges[OPERATOR_ID] = {
        "dirty": False,
        "missing_origin": False,
        "in_progress": False,
        "unread": marks.get(OPERATOR_ID, 0) if type(marks.get(OPERATOR_ID, 0)) is int else 0,
    }
    return badges


def family_cards(
    lines: Sequence[Mapping[str, object]],
    tasks: Sequence[Mapping[str, object]],
    *,
    unread: Mapping[str, int] | None = None,
) -> list[dict[str, object]]:
    badges = family_badges(lines, tasks, unread=unread)
    cards: list[dict[str, object]] = []
    counts = dict(unread or {})
    for parent_id in family_ids(lines):
        safe_parent = _line_id(parent_id)
        if not safe_parent:
            continue
        children = [_line_id(item) for item in family_children(lines, safe_parent)]
        children = [item for item in children if item]
        marks = [badges.get(safe_parent, {}), *(badges.get(child, {}) for child in children)]
        unread_count = counts.get(safe_parent, 0)
        if type(unread_count) is not int or unread_count < 0:
            unread_count = 0
        cards.append(
            {
                "parent": safe_parent,
                "children": children,
                "unread": unread_count,
                "dirty": sum(1 for item in marks if item.get("dirty")),
                "missing_origin": sum(1 for item in marks if item.get("missing_origin")),
                "in_progress": sum(1 for item in marks if item.get("in_progress")),
            }
        )
    return cards


def family_payload(
    lines: Sequence[Mapping[str, object]],
    parent_id: str,
    tasks: Sequence[Mapping[str, object]],
    *,
    unread: Mapping[str, int] | None = None,
) -> dict[str, object]:
    graph = family_graph(lines, parent_id, badges=family_badges(lines, tasks, unread=unread))
    return dict(graph)


def family_unread_maps(
    config: Config,
    lines: Sequence[Mapping[str, object]],
) -> tuple[dict[str, int], dict[str, dict[str, int]]]:
    """Return card unread (operator) and per-family member unread."""
    cards: dict[str, int] = {}
    members: dict[str, dict[str, int]] = {}
    for parent_id in family_ids(lines):
        safe_parent = _line_id(parent_id)
        if not safe_parent:
            continue
        try:
            counts = unread_by_member(config, safe_parent, lines)
        except FamilyChannelError as exc:
            raise ConsoleOverviewError(exc.code) from exc
        members[safe_parent] = counts
        cards[safe_parent] = counts.get(OPERATOR_ID, 0)
    return cards, members


def channel_cursor_digest(record: Mapping[str, object]) -> str:
    payload = {
        "kind": record.get("kind", ""),
        "at": record.get("at", ""),
        "from": record.get("from", ""),
        "to": record.get("to", ""),
        "family": record.get("family", ""),
        "body": record.get("body", ""),
        "retracts": record.get("retracts", ""),
        "facts": record.get("facts") if isinstance(record.get("facts"), dict) else {},
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def encode_channel_cursor(
    secret: bytes,
    *,
    after_seq: int,
    message_id: str,
    digest: str,
) -> str:
    body = canonical_json_bytes(
        {
            "schema_version": _CURSOR_SCHEMA,
            "after": after_seq,
            "event_id": message_id,
            "digest": digest,
        }
    )
    signature = hmac.new(secret, body, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(body + signature).rstrip(b"=").decode("ascii")


def decode_channel_cursor(secret: bytes, value: str) -> tuple[int, str, str]:
    if not isinstance(value, str) or not value or len(value) > _CURSOR_MAX_LENGTH:
        raise ConsoleOverviewError("CHANNEL_CURSOR_INVALID")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, UnicodeError) as exc:
        raise ConsoleOverviewError("CHANNEL_CURSOR_INVALID") from exc
    if len(raw) <= hashlib.sha256().digest_size:
        raise ConsoleOverviewError("CHANNEL_CURSOR_INVALID")
    body, signature = raw[:-32], raw[-32:]
    expected = hmac.new(secret, body, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, signature):
        raise ConsoleOverviewError("CHANNEL_CURSOR_INVALID")
    try:
        decoded: Any = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConsoleOverviewError("CHANNEL_CURSOR_INVALID") from exc
    digest = decoded.get("digest") if isinstance(decoded, dict) else None
    if (
        not isinstance(decoded, dict)
        or set(decoded) != {"schema_version", "after", "event_id", "digest"}
        or decoded["schema_version"] != _CURSOR_SCHEMA
        or type(decoded["after"]) is not int
        or decoded["after"] < 1
        or not isinstance(decoded["event_id"], str)
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(char not in _DIGEST_HEX for char in digest)
    ):
        raise ConsoleOverviewError("CHANNEL_CURSOR_INVALID")
    return decoded["after"], decoded["event_id"], digest


def parse_channel_filter(value: str | None) -> dict[str, str | bool]:
    if not value:
        return {}
    parsed: dict[str, str | bool] = {}
    for token in value.split(","):
        item = token.strip()
        if not item:
            raise ConsoleOverviewError("CHANNEL_FILTER_INVALID")
        if item == "unacked":
            parsed["unacked"] = True
            continue
        key, separator, raw = item.partition(":")
        if not separator or key not in {"kind", "from"} or key in parsed or not raw:
            raise ConsoleOverviewError("CHANNEL_FILTER_INVALID")
        if key == "kind":
            if raw not in CHANNEL_KINDS:
                raise ConsoleOverviewError("CHANNEL_FILTER_INVALID")
            parsed["kind"] = raw
            continue
        member = _member_token(raw)
        if not member:
            raise ConsoleOverviewError("CHANNEL_FILTER_INVALID")
        parsed["from"] = member
    if set(parsed) - _FILTER_KEYS:
        raise ConsoleOverviewError("CHANNEL_FILTER_INVALID")
    return parsed


def project_channel_message(
    record: Mapping[str, object],
    *,
    acked: frozenset[str],
    retracted_ids: frozenset[str],
) -> dict[str, object]:
    message_id = str(record.get("id") or "")
    kind = record.get("kind")
    seq = record.get("seq")
    at = record.get("at")
    if type(seq) is not int or not isinstance(at, str) or kind not in CHANNEL_KINDS:
        return {
            "id": "msg_0",
            "seq": 0,
            "at": "",
            "family": "",
            "from": "",
            "to": "",
            "kind": "EVENT_REDACTED",
            "body": "",
            "retracts": "",
            "retracted": False,
            "acked": False,
            "artifact_id": "",
        }
    sender = _member_token(record.get("from"))
    recipient = record.get("to")
    recipient_token = "" if recipient == "" else _member_token(recipient)
    family = _line_id(record.get("family"))
    body = record.get("body") if isinstance(record.get("body"), str) else ""
    if len(body) > 2048:
        body = ""
    retracts = record.get("retracts") if isinstance(record.get("retracts"), str) else ""
    facts = record.get("facts") if isinstance(record.get("facts"), dict) else {}
    artifact_raw = facts.get("artifact_id")
    artifact_id = _line_id(artifact_raw) if isinstance(artifact_raw, str) and artifact_raw else ""
    return {
        "id": message_id,
        "seq": seq,
        "at": at,
        "family": family,
        "from": sender,
        "to": recipient_token,
        "kind": kind,
        "body": body,
        "retracts": retracts,
        "retracted": message_id in retracted_ids,
        "acked": message_id in acked,
        "artifact_id": artifact_id,
    }


def channel_page(
    config: Config,
    parent_id: str,
    *,
    secret: bytes,
    after: str | None,
    filter_text: str | None = None,
    limit: int = DEFAULT_CHANNEL_LIMIT,
) -> dict[str, object]:
    if type(limit) is not int or not 1 <= limit <= MAX_CHANNEL_LIMIT:
        raise ConsoleOverviewError("CHANNEL_LIMIT_INVALID")
    lines = line_records(config)
    if parent_id not in family_ids(lines):
        raise ConsoleOverviewError("FAMILY_NOT_FOUND")
    after_seq = 0
    filters = parse_channel_filter(filter_text)
    try:
        if after:
            after_seq, message_id, digest = decode_channel_cursor(secret, after)
            current = channel_at(config, parent_id, after_seq)
            if (
                current is None
                or current.get("id") != message_id
                or channel_cursor_digest(current) != digest
            ):
                raise ConsoleOverviewError("CHANNEL_CURSOR_INVALID")
        records = [
            item
            for item in read_visible_channel(config, parent_id, viewer=OPERATOR_ID)
            if int(item["seq"]) > after_seq
        ]
        acked = read_acks(config, parent_id)
        retracted_ids = retracted_message_ids(config, parent_id)
    except FamilyChannelError as exc:
        raise ConsoleOverviewError(exc.code) from exc
    messages = [
        project_channel_message(item, acked=acked, retracted_ids=retracted_ids)
        for item in records
    ]
    if filters.get("unacked"):
        messages = [item for item in messages if not item["acked"]]
    kind_filter = filters.get("kind")
    if isinstance(kind_filter, str):
        messages = [item for item in messages if item["kind"] == kind_filter]
    from_filter = filters.get("from")
    if isinstance(from_filter, str):
        messages = [item for item in messages if item["from"] == from_filter]
    messages = messages[:limit]
    if messages:
        last = next(item for item in records if item.get("id") == messages[-1]["id"])
        next_cursor = encode_channel_cursor(
            secret,
            after_seq=int(last["seq"]),
            message_id=str(last["id"]),
            digest=channel_cursor_digest(last),
        )
    elif after:
        next_cursor = after
    else:
        next_cursor = None
    return {
        "family": parent_id,
        "members": list(family_members(lines, parent_id)),
        "messages": messages,
        "next_cursor": next_cursor,
    }


def apply_human_channel_post(
    config: Config,
    parent_id: str,
    payload: Mapping[str, object],
    *,
    clock=None,
) -> dict[str, object]:
    """Listener-side overlay write.  ``from`` is always ``operator``."""
    if set(payload) - {"kind", "to", "body", "ack_id"}:
        raise ConsoleOverviewError("FAMILY_POST_INVALID")
    kind = payload.get("kind")
    if kind not in HUMAN_POST_KINDS:
        raise ConsoleOverviewError("FAMILY_POST_FORBIDDEN")
    to_raw = payload.get("to", "")
    body_raw = payload.get("body", "")
    ack_id = payload.get("ack_id", "")
    if to_raw is None:
        to_raw = ""
    if body_raw is None:
        body_raw = ""
    if ack_id is None:
        ack_id = ""
    if not isinstance(to_raw, str) or not isinstance(body_raw, str) or not isinstance(ack_id, str):
        raise ConsoleOverviewError("FAMILY_POST_INVALID")
    lines = line_records(config)
    if parent_id not in family_ids(lines):
        raise ConsoleOverviewError("FAMILY_NOT_FOUND")
    members = set(family_members(lines, parent_id))
    try:
        if kind == "ack":
            if body_raw or to_raw:
                raise ConsoleOverviewError("FAMILY_POST_INVALID")
            located = find_channel_message(config, ack_id, family=parent_id)
            if located is None or located[0] != parent_id:
                raise ConsoleOverviewError("CHANNEL_MESSAGE_NOT_FOUND")
            result = ack_channel_message(
                config, ack_id, family=parent_id, clock=clock
            )
            return {"id": result["id"], "seq": result["seq"]}
        if ack_id:
            raise ConsoleOverviewError("FAMILY_POST_INVALID")
        if kind not in OPERATOR_POST_KINDS:
            raise ConsoleOverviewError("FAMILY_POST_FORBIDDEN")
        recipient = to_raw
        if recipient and recipient not in members:
            raise ConsoleOverviewError("FAMILY_TO_INVALID")
        infer_post_family(lines, OPERATOR_ID, recipient, parent_id)
        result = post_channel_message(
            config,
            sender=OPERATOR_ID,
            kind=kind,
            body=body_raw,
            recipient=recipient,
            family=parent_id,
            clock=clock,
        )
    except FamilyChannelError as exc:
        raise ConsoleOverviewError(exc.code) from exc
    return {"id": result["id"], "seq": result["seq"]}


def project_artifact(
    record: Mapping[str, object],
    *,
    alias: str,
    parent_id: str,
) -> dict[str, object]:
    artifact_id = _line_id(record.get("id"))
    artifact_type = record.get("type")
    title = record.get("title") if isinstance(record.get("title"), str) else ""
    conclusion = record.get("conclusion") if isinstance(record.get("conclusion"), str) else ""
    bound_hash = record.get("bound_hash") if isinstance(record.get("bound_hash"), str) else ""
    media_type = record.get("media_type") if isinstance(record.get("media_type"), str) else ""
    size = record.get("size")
    duration = record.get("duration") if isinstance(record.get("duration"), str) else ""
    points_raw = record.get("points")
    points: list[dict[str, object]] = []
    if isinstance(points_raw, list):
        for item in points_raw[:256]:
            if not isinstance(item, Mapping):
                continue
            x_value = item.get("x")
            y_value = item.get("y")
            if isinstance(x_value, (int, float)) and isinstance(y_value, (int, float)):
                if isinstance(x_value, bool) or isinstance(y_value, bool):
                    continue
                points.append({"x": x_value, "y": y_value})
    open_command = ""
    if artifact_type == "video" and alias and parent_id:
        open_command = f"dyro --workspace {alias} --dry-run line inbox --family {parent_id}"
    return {
        "id": artifact_id,
        "type": artifact_type if artifact_type in {"review", "image", "chart", "video"} else "",
        "title": title[:80],
        "conclusion": conclusion if conclusion in {"pass", "fail", "inconclusive"} else "",
        "bound_hash": bound_hash[:12],
        "media_type": media_type if media_type in IMAGE_MEDIA_TYPES or media_type == "application/json" else "",
        "size": size if type(size) is int and size >= 0 else 0,
        "duration": duration[:16],
        "points": points,
        "open_command": open_command,
    }


def artifacts_payload(config: Config, parent_id: str, *, alias: str) -> dict[str, object]:
    lines = line_records(config)
    if parent_id not in family_ids(lines):
        raise ConsoleOverviewError("FAMILY_NOT_FOUND")
    try:
        records = list_family_artifacts(config, parent_id)
    except FamilyArtifactError as exc:
        raise ConsoleOverviewError(exc.code) from exc
    return {
        "family": parent_id,
        "artifacts": [
            project_artifact(item, alias=alias, parent_id=parent_id) for item in records
        ],
    }


def artifact_payload(
    config: Config, parent_id: str, artifact_id: str, *, alias: str
) -> dict[str, object]:
    lines = line_records(config)
    if parent_id not in family_ids(lines):
        raise ConsoleOverviewError("FAMILY_NOT_FOUND")
    try:
        record = read_family_artifact(config, parent_id, artifact_id)
    except FamilyArtifactError as exc:
        raise ConsoleOverviewError(exc.code) from exc
    return project_artifact(record, alias=alias, parent_id=parent_id)


def artifact_bytes_payload(
    config: Config, parent_id: str, artifact_id: str
) -> tuple[str, bytes]:
    lines = line_records(config)
    if parent_id not in family_ids(lines):
        raise ConsoleOverviewError("FAMILY_NOT_FOUND")
    try:
        return read_family_artifact_bytes(config, parent_id, artifact_id)
    except FamilyArtifactError as exc:
        raise ConsoleOverviewError(exc.code) from exc

"""One-level line families: ``F(P) = {P} ∪ children(P) ∪ {operator}``.

P2 adds the overlay channel and operator ack index.  A channel write and its
matching ``signal`` event share the overlay lock; one side only is fail-closed.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
import json
import re
import unicodedata
from pathlib import Path
from typing import Any

from .config import Config, validate_id
from .errors import DyroError, ValidationError
from .events import (
    EventLogError,
    append_event_locked,
    overlay_lock,
    read_event_records_locked,
)
from .state import append_text, atomic_write_text


OPERATOR_ID = "operator"
CHANNEL_KINDS = frozenset(
    {
        "contract",
        "blocked",
        "shipped",
        "ask_sync",
        "decision",
        "artifact",
        "retract",
    }
)
OPERATOR_POST_KINDS = frozenset({"decision", "contract"})
HUMAN_POST_KINDS = frozenset({"decision", "contract", "ack"})
UNACKED_KIND_PRIORITY = (
    "blocked",
    "ask_sync",
    "contract",
    "decision",
    "shipped",
    "artifact",
    "retract",
)
DEFAULT_CHANNEL_LIMIT = 50
MAX_CHANNEL_LIMIT = 100
MAX_CHANNEL_BODY = 2048
CHANNEL_FILE = "channel.jsonl"
ACKS_FILE = "acks.json"
MAX_CHANNEL_LOG_BYTES = 2 * 1024 * 1024
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_CREDENTIAL = re.compile(
    r"(?i)(?:"
    r"(?:token|secret|password|api[_-]?key|authorization)\s*(?:=|:)"
    r"|(?:token|secret|password|api[_-]?key|authorization)\s+[A-Za-z0-9._-]{8,}"
    r"|(?:token|secret|password|api[_-]?key|authorization)[._-][A-Za-z0-9._-]{6,}"
    r"|bearer\s+[A-Za-z0-9._-]{8,}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}"
    r"|(?:sk|rk|pk|ghp|gho|ghs|ghu|github_pat|glpat|npm|pypi|AIza)[_-][A-Za-z0-9._-]{6,}"
    r"|AKIA[A-Z0-9]{16}"
    r"|ya29\.[A-Za-z0-9._-]{8,}"
    r")"
)
_REMOTE = re.compile(r"(?i)(?:[a-z][a-z0-9+.-]*://|git@[^\s:]+:)")
_ABSOLUTE_PATH = re.compile(r"(?:^|[^A-Za-z0-9._-])(?:~|/|[A-Za-z]:[\\/])")


class FamilyChannelError(DyroError):
    """Stable, path-free failure while reading or writing a family channel."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def family_children(lines: Iterable[Mapping[str, object]], parent_id: str) -> tuple[str, ...]:
    """Direct children of ``parent_id``.  Grandchildren are excluded."""
    children: list[str] = []
    for line in lines:
        if str(line.get("parent") or "") == parent_id:
            child_id = str(line.get("id") or "")
            if child_id and child_id != parent_id:
                children.append(child_id)
    return tuple(children)


def family_members(lines: Iterable[Mapping[str, object]], parent_id: str) -> tuple[str, ...]:
    return (parent_id, *family_children(lines, parent_id), OPERATOR_ID)


def family_ids(lines: Iterable[Mapping[str, object]]) -> tuple[str, ...]:
    """Every line can be opened as a one-level family parent."""
    return tuple(str(line.get("id") or "") for line in lines if line.get("id"))


def family_graph(
    lines: Iterable[Mapping[str, object]],
    parent_id: str,
    *,
    badges: Mapping[str, Mapping[str, object]] | None = None,
) -> dict[str, object]:
    """Return the one-level graph for ``F(parent_id)``.

    ``badges`` maps line id → ``dirty`` / ``missing_origin`` / ``in_progress`` /
    ``unread``.  Unknown ids default to false / zero.  ``operator`` has no git
    badges.
    """
    items = [dict(line) for line in lines]
    ids = {str(line.get("id") or "") for line in items}
    if parent_id not in ids:
        return {}
    children = family_children(items, parent_id)
    marks = {key: dict(value) for key, value in dict(badges or {}).items()}
    nodes = [
        _node(parent_id, "parent", marks.get(parent_id, {})),
        *(_node(child_id, "child", marks.get(child_id, {})) for child_id in children),
        _node(OPERATOR_ID, "operator", marks.get(OPERATOR_ID, {}), git=False),
    ]
    edges = [{"from": parent_id, "to": child_id, "kind": "parent"} for child_id in children]
    return {
        "parent": parent_id,
        "members": list(family_members(items, parent_id)),
        "nodes": nodes,
        "edges": edges,
    }


def line_records(config: Config) -> list[dict[str, str]]:
    from .workspace import list_lines

    return [{"id": line.id, "parent": line.parent} for line in list_lines(config)]


def line_parent_map(lines: Iterable[Mapping[str, object]]) -> dict[str, str]:
    return {
        str(line.get("id") or ""): str(line.get("parent") or "")
        for line in lines
        if line.get("id")
    }


def family_dir(config: Config, parent_id: str) -> Path:
    validate_id(parent_id, "父开发线 ID")
    return config.root / ".dyro" / "families" / parent_id


def channel_path(config: Config, parent_id: str) -> Path:
    return family_dir(config, parent_id) / CHANNEL_FILE


def acks_path(config: Config, parent_id: str) -> Path:
    return family_dir(config, parent_id) / ACKS_FILE


def sanitize_channel_body(value: object, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise FamilyChannelError("CHANNEL_BODY_INVALID")
    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized:
        if allow_empty:
            return ""
        raise FamilyChannelError("CHANNEL_BODY_INVALID")
    if (
        len(normalized) > MAX_CHANNEL_BODY
        or _CONTROL.search(normalized)
        or _CREDENTIAL.search(normalized)
        or _REMOTE.search(normalized)
        or _ABSOLUTE_PATH.search(normalized)
    ):
        raise FamilyChannelError("CHANNEL_BODY_INVALID")
    return normalized


def visible_to(viewer: str, post: Mapping[str, object], family_parent: str) -> bool:
    """Whether ``viewer`` may see ``post`` inside ``F(family_parent)``.

    Broadcasts are visible to the whole family.  Directed posts are visible to
    the sender, receiver, parent, and operator.  Cousins do not see others' DMs.
    """
    recipient = str(post.get("to") or "")
    if not recipient:
        return True
    sender = str(post.get("from") or "")
    return viewer in {sender, recipient, family_parent, OPERATOR_ID}


def infer_post_family(
    lines: Iterable[Mapping[str, object]],
    sender: str,
    recipient: str = "",
    family: str = "",
) -> str:
    items = [dict(line) for line in lines]
    ids = family_ids(items)
    if family:
        if family not in ids:
            raise FamilyChannelError("FAMILY_NOT_FOUND")
        members = set(family_members(items, family))
        if sender not in members:
            raise FamilyChannelError("FAMILY_MEMBER_INVALID")
        if recipient and recipient not in members:
            raise FamilyChannelError("FAMILY_TO_INVALID")
        return family
    if sender == OPERATOR_ID:
        if recipient and recipient != OPERATOR_ID:
            parent = line_parent_map(items).get(recipient, "")
            chosen = parent or recipient
            if chosen not in ids:
                raise FamilyChannelError("FAMILY_NOT_FOUND")
            members = set(family_members(items, chosen))
            if recipient not in members:
                raise FamilyChannelError("FAMILY_TO_INVALID")
            return chosen
        raise FamilyChannelError("FAMILY_REQUIRED")
    if sender not in ids:
        raise FamilyChannelError("FAMILY_MEMBER_INVALID")
    parent = line_parent_map(items).get(sender, "")
    # operator sits in every F(P). Inferring from membership would pick
    # F(sender) for `--to operator`, hiding the post from the parent inbox.
    if recipient == OPERATOR_ID or not recipient:
        return parent or sender
    own_lines = {sender, *family_children(items, sender)}
    if recipient in own_lines:
        return sender
    if parent and recipient in set(family_members(items, parent)):
        return parent
    raise FamilyChannelError("FAMILY_TO_INVALID")


def family_unacked(
    config: Config,
    *,
    lines: Iterable[Mapping[str, object]] | None = None,
) -> dict[str, object]:
    """Operator-unacked overlay summary.  Never a repair or spawn blocker."""
    items = list(lines) if lines is not None else line_records(config)
    best: dict[str, object] | None = None
    count = 0
    for parent_id in family_ids(items):
        try:
            posts = read_visible_channel(config, parent_id, viewer=OPERATOR_ID)
        except FamilyChannelError:
            continue
        acked = read_acks(config, parent_id)
        for post in posts:
            if post["id"] in acked:
                continue
            count += 1
            if best is None or _kind_rank(str(post["kind"])) < _kind_rank(str(best["kind"])):
                best = post
    if best is None:
        return {"count": 0, "kind": "", "family": "", "summary": ""}
    return {
        "count": count,
        "kind": best["kind"],
        "family": best["family"],
        "summary": _safe_summary(str(best.get("body") or best["kind"])),
    }


def unread_by_member(
    config: Config,
    parent_id: str,
    lines: Iterable[Mapping[str, object]],
) -> dict[str, int]:
    members = family_members(lines, parent_id)
    try:
        posts = read_visible_channel(config, parent_id, viewer=OPERATOR_ID)
        acked = read_acks(config, parent_id)
    except FamilyChannelError:
        return {member: 0 for member in members}
    unacked = [post for post in posts if post["id"] not in acked]
    counts = {member: 0 for member in members}
    for member in members:
        counts[member] = sum(1 for post in unacked if visible_to(member, post, parent_id))
    return counts


def post_channel_message(
    config: Config,
    *,
    sender: str,
    kind: str,
    body: str = "",
    recipient: str = "",
    family: str = "",
    clock: Callable[[], datetime] | None = None,
    dry_run: bool = False,
) -> dict[str, object]:
    """Append one channel row and its ``signal`` event under the overlay lock."""
    sender = _member_id(sender, "发送者")
    recipient = _member_id(recipient, "接收者", allow_empty=True)
    if kind not in CHANNEL_KINDS:
        raise FamilyChannelError("CHANNEL_KIND_INVALID")
    if sender == OPERATOR_ID and kind not in OPERATOR_POST_KINDS:
        raise FamilyChannelError("FAMILY_POST_FORBIDDEN")
    lines = line_records(config)
    parent_id = infer_post_family(lines, sender, recipient, family)
    members = set(family_members(lines, parent_id))
    if sender not in members or (recipient and recipient not in members):
        raise FamilyChannelError("FAMILY_TO_INVALID")
    retracts = ""
    if kind == "retract":
        retracts = _message_id(body)
        body = ""
    else:
        body = sanitize_channel_body(body, allow_empty=kind == "artifact")
    record = {
        "from": sender,
        "to": recipient,
        "kind": kind,
        "body": body,
        "retracts": retracts,
        "family": parent_id,
    }
    if dry_run:
        return {**record, "id": "msg_0", "seq": 0, "at": "", "dry_run": True}
    return _commit_channel_row(config, parent_id, record, clock=clock)


def ack_channel_message(
    config: Config,
    message_id: str,
    *,
    clock: Callable[[], datetime] | None = None,
    dry_run: bool = False,
) -> dict[str, object]:
    """Mark one row operator-read.  Ack is inbox state, not a channel kind."""
    message_id = _message_id(message_id)
    located = find_channel_message(config, message_id)
    if located is None:
        raise FamilyChannelError("CHANNEL_MESSAGE_NOT_FOUND")
    parent_id, row = located
    if dry_run:
        return {
            "id": message_id,
            "seq": row["seq"],
            "family": parent_id,
            "acked": True,
            "dry_run": True,
        }
    try:
        with overlay_lock(config):
            _assert_channel_paired(config, parent_id)
            acked = set(_read_ack_ids_locked(config, parent_id))
            acked.add(message_id)
            append_event_locked(
                config,
                kind="signal",
                actor=OPERATOR_ID,
                subject=parent_id,
                family=parent_id,
                facts={"channel_id": message_id, "ack": True},
                clock=clock,
            )
            _write_acks_locked(config, parent_id, acked)
            _assert_channel_paired(config, parent_id)
    except EventLogError as exc:
        raise FamilyChannelError(exc.code) from exc
    except OSError as exc:
        raise FamilyChannelError("CHANNEL_WRITE_FAILED") from exc
    return {"id": message_id, "seq": row["seq"], "family": parent_id, "acked": True}


def list_inbox(
    config: Config,
    *,
    family: str = "",
    viewer: str = "",
    unacked: bool = False,
) -> dict[str, object]:
    lines = line_records(config)
    parent_id, resolved_viewer = _inbox_scope(lines, family=family, viewer=viewer)
    posts = read_visible_channel(config, parent_id, viewer=resolved_viewer)
    acked = read_acks(config, parent_id)
    if unacked:
        posts = [post for post in posts if post["id"] not in acked]
    messages = [_decorate(post, acked) for post in posts]
    return {
        "family": parent_id,
        "viewer": resolved_viewer,
        "messages": messages,
        "unacked": sum(1 for item in messages if not item["acked"]),
    }


def find_channel_message(
    config: Config, message_id: str
) -> tuple[str, dict[str, object]] | None:
    message_id = _message_id(message_id)
    for parent_id in family_ids(line_records(config)):
        try:
            for post in read_visible_channel(config, parent_id, viewer=OPERATOR_ID):
                if post["id"] == message_id:
                    return parent_id, post
        except FamilyChannelError:
            continue
    return None


def read_visible_channel(
    config: Config,
    parent_id: str,
    *,
    viewer: str = OPERATOR_ID,
) -> list[dict[str, object]]:
    """Return every visible row.  Unpaired channel/event writes fail closed."""
    validate_id(parent_id, "父开发线 ID")
    with overlay_lock(config):
        _assert_channel_paired(config, parent_id)
        records = _read_channel_locked(config, parent_id)
    return [row for row in records if visible_to(viewer, row, parent_id)]


def retracted_message_ids(config: Config, parent_id: str) -> frozenset[str]:
    validate_id(parent_id, "父开发线 ID")
    with overlay_lock(config):
        records = _read_channel_locked(config, parent_id)
    return frozenset(
        str(item["retracts"])
        for item in records
        if item["kind"] == "retract" and item["retracts"]
    )


def read_channel(
    config: Config,
    parent_id: str,
    *,
    viewer: str = OPERATOR_ID,
    after_seq: int = 0,
    limit: int = DEFAULT_CHANNEL_LIMIT,
) -> list[dict[str, object]]:
    if type(after_seq) is not int or after_seq < 0:
        raise FamilyChannelError("CHANNEL_CURSOR_INVALID")
    if type(limit) is not int or not 1 <= limit <= MAX_CHANNEL_LIMIT:
        raise FamilyChannelError("CHANNEL_LIMIT_INVALID")
    selected = [
        row
        for row in read_visible_channel(config, parent_id, viewer=viewer)
        if int(row["seq"]) > after_seq
    ]
    return selected[:limit]


def read_acks(config: Config, parent_id: str) -> frozenset[str]:
    validate_id(parent_id, "父开发线 ID")
    with overlay_lock(config):
        return _read_ack_ids_locked(config, parent_id)


def channel_at(config: Config, parent_id: str, seq: int) -> dict[str, object] | None:
    if type(seq) is not int or seq < 1:
        return None
    with overlay_lock(config):
        records = _read_channel_locked(config, parent_id)
    if seq > len(records):
        return None
    return records[seq - 1]


def _commit_channel_row(
    config: Config,
    parent_id: str,
    record: Mapping[str, object],
    *,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, object]:
    stamp = _utc(clock).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with overlay_lock(config):
            _replay_unpaired_signals(config, parent_id, clock=clock)
            _assert_channel_paired(config, parent_id)
            records = _read_channel_locked(config, parent_id)
            if record["kind"] == "retract":
                target = str(record["retracts"])
                if not any(item["id"] == target for item in records):
                    raise FamilyChannelError("CHANNEL_MESSAGE_NOT_FOUND")
            seq = (int(records[-1]["seq"]) + 1) if records else 1
            row = {
                "id": f"msg_{seq}",
                "seq": seq,
                "at": stamp,
                "family": parent_id,
                "from": record["from"],
                "to": record["to"],
                "kind": record["kind"],
                "body": record["body"],
                "retracts": record["retracts"],
            }
            append_text(
                channel_path(config, parent_id),
                json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n",
            )
            append_event_locked(
                config,
                kind="signal",
                actor=str(row["from"]),
                subject=str(row["to"] or parent_id),
                family=parent_id,
                facts={"channel_id": row["id"]},
                clock=clock,
            )
            _assert_channel_paired(config, parent_id)
    except FamilyChannelError:
        raise
    except EventLogError as exc:
        raise FamilyChannelError(exc.code) from exc
    except OSError as exc:
        raise FamilyChannelError("CHANNEL_WRITE_FAILED") from exc
    return row


def _replay_unpaired_signals(
    config: Config,
    parent_id: str,
    *,
    clock: Callable[[], datetime] | None = None,
) -> None:
    records = _read_channel_locked(config, parent_id)
    paired = _signal_channel_ids(config)
    for row in records:
        if row["id"] in paired:
            continue
        append_event_locked(
            config,
            kind="signal",
            actor=str(row["from"]),
            subject=str(row["to"] or parent_id),
            family=parent_id,
            facts={"channel_id": str(row["id"])},
            clock=clock,
        )


def _assert_channel_paired(config: Config, parent_id: str) -> None:
    records = _read_channel_locked(config, parent_id)
    paired = _signal_channel_ids(config)
    if any(row["id"] not in paired for row in records):
        raise FamilyChannelError("CHANNEL_LOG_INCONSISTENT")


def _signal_channel_ids(config: Config) -> set[str]:
    ids: set[str] = set()
    for record in read_event_records_locked(config):
        if record.get("kind") != "signal":
            continue
        facts = record.get("facts")
        if not isinstance(facts, dict):
            continue
        channel_id = facts.get("channel_id")
        if isinstance(channel_id, str) and channel_id:
            ids.add(channel_id)
    return ids


def _read_channel_locked(config: Config, parent_id: str) -> list[dict[str, object]]:
    path = channel_path(config, parent_id)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise FamilyChannelError("CHANNEL_LOG_INVALID")
    if not path.exists():
        return []
    try:
        if path.stat().st_size > MAX_CHANNEL_LOG_BYTES:
            raise FamilyChannelError("CHANNEL_LOG_INVALID")
        text = path.read_text(encoding="utf-8")
    except FamilyChannelError:
        raise
    except OSError as exc:
        raise FamilyChannelError("CHANNEL_LOG_INVALID") from exc
    if not text:
        return []
    if not text.endswith("\n"):
        raise FamilyChannelError("CHANNEL_LOG_INVALID")
    records: list[dict[str, object]] = []
    expected = 1
    for line in text.splitlines():
        if not line:
            raise FamilyChannelError("CHANNEL_LOG_INVALID")
        record = _decode_channel(line)
        if record["seq"] != expected or record["id"] != f"msg_{expected}":
            raise FamilyChannelError("CHANNEL_LOG_INVALID")
        records.append(record)
        expected += 1
    return records


def _decode_channel(raw: str) -> dict[str, object]:
    try:
        decoded: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise FamilyChannelError("CHANNEL_LOG_INVALID") from exc
    if not isinstance(decoded, dict):
        raise FamilyChannelError("CHANNEL_LOG_INVALID")
    required = ("id", "seq", "at", "family", "from", "to", "kind", "body", "retracts")
    if any(key not in decoded for key in required):
        raise FamilyChannelError("CHANNEL_LOG_INVALID")
    seq = decoded.get("seq")
    kind = decoded.get("kind")
    if (
        type(seq) is not int
        or seq < 1
        or not isinstance(decoded.get("id"), str)
        or not isinstance(decoded.get("at"), str)
        or not isinstance(decoded.get("family"), str)
        or not isinstance(decoded.get("from"), str)
        or not isinstance(decoded.get("to"), str)
        or not isinstance(kind, str)
        or kind not in CHANNEL_KINDS
        or not isinstance(decoded.get("body"), str)
        or not isinstance(decoded.get("retracts"), str)
    ):
        raise FamilyChannelError("CHANNEL_LOG_INVALID")
    return {
        "id": decoded["id"],
        "seq": seq,
        "at": decoded["at"],
        "family": decoded["family"],
        "from": decoded["from"],
        "to": decoded["to"],
        "kind": kind,
        "body": decoded["body"],
        "retracts": decoded["retracts"],
    }


def _read_ack_ids_locked(config: Config, parent_id: str) -> frozenset[str]:
    path = acks_path(config, parent_id)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise FamilyChannelError("CHANNEL_ACKS_INVALID")
    if not path.exists():
        return frozenset()
    try:
        decoded: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FamilyChannelError("CHANNEL_ACKS_INVALID") from exc
    if not isinstance(decoded, dict) or decoded.get("schema_version") != 1:
        raise FamilyChannelError("CHANNEL_ACKS_INVALID")
    ids = decoded.get("ids")
    if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
        raise FamilyChannelError("CHANNEL_ACKS_INVALID")
    return frozenset(ids)


def _write_acks_locked(config: Config, parent_id: str, ids: Iterable[str]) -> None:
    payload = {
        "schema_version": 1,
        "ids": sorted(set(ids), key=_ack_sort_key),
    }
    atomic_write_text(
        acks_path(config, parent_id),
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
    )


def _inbox_scope(
    lines: Iterable[Mapping[str, object]],
    *,
    family: str,
    viewer: str,
) -> tuple[str, str]:
    items = [dict(line) for line in lines]
    ids = family_ids(items)
    if family:
        if family not in ids:
            raise FamilyChannelError("FAMILY_NOT_FOUND")
        resolved = viewer or OPERATOR_ID
        if resolved != OPERATOR_ID and resolved not in set(family_members(items, family)):
            raise FamilyChannelError("FAMILY_MEMBER_INVALID")
        return family, resolved
    if viewer and viewer != OPERATOR_ID:
        if viewer not in ids:
            raise FamilyChannelError("FAMILY_MEMBER_INVALID")
        parent = line_parent_map(items).get(viewer, "")
        return parent or viewer, viewer
    if len(ids) == 1:
        return ids[0], OPERATOR_ID
    raise FamilyChannelError("FAMILY_REQUIRED")


def _decorate(post: Mapping[str, object], acked: frozenset[str]) -> dict[str, object]:
    row = dict(post)
    row["acked"] = row.get("id") in acked
    return row


def _member_id(value: str, label: str, *, allow_empty: bool = False) -> str:
    if value == "":
        if allow_empty:
            return ""
        raise FamilyChannelError("FAMILY_MEMBER_INVALID")
    if value == OPERATOR_ID:
        return OPERATOR_ID
    try:
        return validate_id(value, label)
    except ValidationError as exc:
        raise FamilyChannelError("FAMILY_MEMBER_INVALID") from exc


def _message_id(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"msg_[1-9][0-9]{0,7}", value):
        raise FamilyChannelError("CHANNEL_MESSAGE_NOT_FOUND")
    return value


def _kind_rank(kind: str) -> int:
    try:
        return UNACKED_KIND_PRIORITY.index(kind)
    except ValueError:
        return len(UNACKED_KIND_PRIORITY)


def _safe_summary(value: str) -> str:
    text = value.strip()
    if len(text) > 80:
        text = text[:80]
    return text


def _ack_sort_key(value: str) -> tuple[int, str]:
    if value.startswith("msg_"):
        try:
            return (int(value[4:]), value)
        except ValueError:
            return (10**9, value)
    return (10**9, value)


def _utc(clock: Callable[[], datetime] | None) -> datetime:
    value = clock() if clock is not None else datetime.now(timezone.utc)
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValidationError("家族频道时钟必须提供带时区的 datetime")
    return value.astimezone(timezone.utc)


def _node(
    node_id: str,
    role: str,
    badge: Mapping[str, object],
    *,
    git: bool = True,
) -> dict[str, object]:
    unread = badge.get("unread", 0)
    unread_count = unread if type(unread) is int and unread >= 0 else 0
    if not git:
        return {
            "id": node_id,
            "role": role,
            "dirty": False,
            "missing_origin": False,
            "in_progress": False,
            "unread": unread_count,
        }
    return {
        "id": node_id,
        "role": role,
        "dirty": bool(badge.get("dirty")),
        "missing_origin": bool(badge.get("missing_origin")),
        "in_progress": bool(badge.get("in_progress")),
        "unread": unread_count,
    }

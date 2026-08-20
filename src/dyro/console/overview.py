"""Path-free, paginated global Console overview projection.

The HTTP listener supplies authentication and transport validation only.  This
module owns no listener and performs no mutation: it reads the existing
registry, captures the Core-owned workspace snapshot once per workspace, and
projects an explicitly whitelisted summary for the Console.
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
from typing import Any

from .. import __version__
from ..canonical import canonical_json_bytes
from ..config import Config, load, validate_id
from ..errors import DyroError, ValidationError
from ..hub import WorkspaceRegistry, load_registry
from ..continuation.briefing import follow_up_from_kind
from ..updates import UpdateState, classify_update, load_update_state
from ..observations import (
    WorkspaceReadSnapshot,
    capture_workspace_read_snapshot,
    inspect_workspace_read_snapshot,
)
from .models import ConsoleEnvelope
from .read_model import proof_inspect_data, workspace_envelope
from .redaction import REDACTED, safe_id, safe_title


_PAGE_SCHEMA_VERSION = 1
_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100
_CURSOR_MAX_LENGTH = 512
_ATTENTION_PRIORITY = {
    "repair_required": 0,
    "needs_user": 1,
    "ready": 2,
    "paused": 3,
    "waiting": 4,
}


class ConsoleOverviewError(Exception):
    """A stable, path-free error that can cross the local HTTP boundary."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _stable_json(value: object) -> bytes:
    return canonical_json_bytes(value)


def _sha256(value: object) -> str:
    return hashlib.sha256(_stable_json(value)).hexdigest()


def _safe_code(value: object) -> str:
    code = safe_id(value)
    return code if code != REDACTED else "REDACTED"


def _safe_mapping(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, dict) else {}


def _safe_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _empty_inventory() -> dict[str, list[dict[str, object]]]:
    return {"lines": [], "tasks": [], "objectives": []}


def _without_proof_decay(items: object) -> list[dict[str, object]]:
    return [
        dict(item)
        for item in _safe_list(items)
        if _safe_code(item.get("reason")) != "PROOF_DECAYED"
    ]


def _inventory_from_envelope(data: dict[str, object]) -> dict[str, list[dict[str, object]]]:
    """Project already-captured lists. Never add Proofs or rebind integration."""
    tasks: list[dict[str, object]] = []
    for item in _safe_list(data.get("tasks")):
        task = dict(item)
        task["integration_state"] = "not_inspected"
        tasks.append(task)
    objectives: list[dict[str, object]] = []
    for item in _safe_list(data.get("objectives")):
        objective = dict(item)
        objective["attention"] = _without_proof_decay(item.get("attention"))
        objective["selected_actions"] = _without_proof_decay(item.get("selected_actions"))
        objective["blocked_actions"] = _without_proof_decay(item.get("blocked_actions"))
        objectives.append(objective)
    return {
        "lines": [dict(item) for item in _safe_list(data.get("lines"))],
        "tasks": tasks,
        "objectives": objectives,
    }


def _empty_update() -> dict[str, object]:
    return {
        "check_enabled": False,
        "last_checked_on": "",
        "latest_version": "",
        "kind": "none",
    }


def _update_payload(state: UpdateState, *, current: str) -> dict[str, object]:
    latest = state.latest_version if isinstance(state.latest_version, str) else ""
    checked = state.last_checked_on if isinstance(state.last_checked_on, str) else ""
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", latest):
        latest = ""
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", checked):
        checked = ""
    try:
        kind = classify_update(current, latest).value if latest else "none"
    except ValidationError:
        kind = "none"
    return {
        "check_enabled": bool(state.check_enabled),
        "last_checked_on": checked,
        "latest_version": latest,
        "kind": kind,
    }


class ConsoleOverviewService:
    """Read and project registered workspaces without changing their state."""

    def __init__(
        self,
        *,
        registry_loader: Callable[[], WorkspaceRegistry] = load_registry,
        config_loader: Callable[[Path], Config] = load,
        snapshot_loader: Callable[[Config], WorkspaceReadSnapshot] = capture_workspace_read_snapshot,
        inspect_loader: Callable[[Config], WorkspaceReadSnapshot] = inspect_workspace_read_snapshot,
        clock: Callable[[], datetime] = _utc_now,
        cursor_secret: bytes | None = None,
        summary_loader: Callable[
            [WorkspaceRegistry], tuple[list[dict[str, object]], set[str]]
        ]
        | None = None,
        update_loader: Callable[[], UpdateState] = load_update_state,
        version_loader: Callable[[], str] = lambda: __version__,
    ) -> None:
        if cursor_secret is not None and (
            not isinstance(cursor_secret, bytes) or len(cursor_secret) < 32
        ):
            raise ValueError("Console overview cursor secret 必须至少为 256 bit")
        self._registry_loader = registry_loader
        self._config_loader = config_loader
        self._snapshot_loader = snapshot_loader
        self._inspect_loader = inspect_loader
        self._clock = clock
        self._cursor_secret = cursor_secret or secrets.token_bytes(32)
        self._summary_loader = summary_loader
        self._update_loader = update_loader
        self._version_loader = version_loader

    def page(
        self,
        *,
        cursor: str | None = None,
        limit: int = _DEFAULT_LIMIT,
    ) -> dict[str, object]:
        """Return a deterministic, paginated overview envelope.

        A cursor is authenticated against the complete current summary.  A
        changed registry or workspace snapshot therefore cannot make an old
        offset silently point at a different project.
        """
        if type(limit) is not int or not 1 <= limit <= _MAX_LIMIT:
            raise ConsoleOverviewError("OVERVIEW_LIMIT_INVALID")
        registry = self._load_registry()
        summaries, warning_codes = self._load_summaries(registry)
        aggregate = {
            "schema_version": _PAGE_SCHEMA_VERSION,
            "default_workspace": _safe_code(registry.default) if registry.default else "",
            "workspaces": summaries,
            "warnings": sorted(warning_codes),
        }
        aggregate_sha256 = _sha256(aggregate)
        offset = self._decode_cursor(cursor, aggregate_sha256) if cursor else 0
        if offset > len(summaries):
            raise ConsoleOverviewError("OVERVIEW_CURSOR_INVALID")
        items = summaries[offset : offset + limit]
        next_offset = offset + len(items)
        next_cursor = (
            self._encode_cursor(next_offset, aggregate_sha256)
            if next_offset < len(summaries)
            else None
        )
        attention_counts = self._attention_counts(summaries)
        data = {
            "default_workspace": _safe_code(registry.default) if registry.default else "",
            "total_workspaces": len(summaries),
            "workspaces": items,
            "next_cursor": next_cursor,
            "attention_counts": attention_counts,
            "task_status_counts": self._task_status_counts(summaries),
            "highest_priority": self._highest_priority(summaries),
        }
        return self._envelope(data, warning_codes)

    def workspace(self, alias: str) -> dict[str, object]:
        """Return one summary card plus already-captured inventory.

        Inventory comes from the same summary snapshot. It is not an inspect,
        so Proofs stay out and task integration stays unread.
        """
        try:
            alias = validate_id(alias, "工作区别名")
        except ValidationError:
            raise ConsoleOverviewError("WORKSPACE_ALIAS_INVALID") from None
        registry = self._load_registry()
        try:
            record = next(item for item in registry.workspaces if item.name == alias)
        except StopIteration:
            raise ConsoleOverviewError("WORKSPACE_NOT_FOUND") from None
        summary, warning_codes, inventory = self._capture(
            record.name, record.root, record.name == registry.default
        )
        return self._envelope({"workspace": summary, **inventory}, warning_codes)

    def events(
        self,
        alias: str,
        *,
        after: str | None = None,
        limit: int = 50,
    ) -> dict[str, object]:
        """Return a cursor page of overlay live events. Overview polling never calls this."""
        from .events import event_page

        config, _warning_codes = self._workspace_config(alias)
        try:
            data = event_page(
                config, secret=self._cursor_secret, after=after, limit=limit
            )
        except ConsoleOverviewError:
            raise
        return self._envelope(data, set())

    def families(self, alias: str) -> dict[str, object]:
        """Return one-level family cards. Unread is operator-unacked overlay."""
        from .families import family_cards, family_unread_maps

        config, warning_from_config = self._workspace_config(alias)
        _summary, warning_codes, inventory = self._workspace_inventory(alias)
        warning_codes = set(warning_codes) | warning_from_config
        card_unread, _members = family_unread_maps(config, inventory["lines"])
        data = {
            "families": family_cards(
                inventory["lines"], inventory["tasks"], unread=card_unread
            )
        }
        return self._envelope(data, warning_codes)

    def family(self, alias: str, parent: str) -> dict[str, object]:
        """Return ``F(parent)``. Grandchildren are excluded."""
        from .families import family_payload, family_unread_maps

        try:
            parent_id = validate_id(parent, "父开发线 ID")
        except ValidationError:
            raise ConsoleOverviewError("FAMILY_PARENT_INVALID") from None
        config, warning_from_config = self._workspace_config(alias)
        _summary, warning_codes, inventory = self._workspace_inventory(alias)
        warning_codes = set(warning_codes) | warning_from_config
        _cards, members = family_unread_maps(config, inventory["lines"])
        payload = family_payload(
            inventory["lines"],
            parent_id,
            inventory["tasks"],
            unread=members.get(parent_id, {}),
        )
        if not payload:
            raise ConsoleOverviewError("FAMILY_NOT_FOUND")
        return self._envelope(payload, warning_codes)

    def channel(
        self,
        alias: str,
        parent: str,
        *,
        after: str | None = None,
        filter: str | None = None,
        limit: int = 50,
    ) -> dict[str, object]:
        """Return a cursor page of the family channel. Overview polling never calls this."""
        from .families import channel_page

        try:
            parent_id = validate_id(parent, "父开发线 ID")
        except ValidationError:
            raise ConsoleOverviewError("FAMILY_PARENT_INVALID") from None
        config, warning_codes = self._workspace_config(alias)
        try:
            data = channel_page(
                config,
                parent_id,
                secret=self._cursor_secret,
                after=after,
                filter_text=filter,
                limit=limit,
            )
        except ConsoleOverviewError:
            raise
        return self._envelope(data, warning_codes)

    def post_channel(
        self,
        alias: str,
        parent: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        """Write one operator overlay signal in the listener process. No git."""
        from .families import apply_human_channel_post

        try:
            parent_id = validate_id(parent, "父开发线 ID")
        except ValidationError:
            raise ConsoleOverviewError("FAMILY_PARENT_INVALID") from None
        config, warning_codes = self._workspace_config(alias)
        data = apply_human_channel_post(config, parent_id, payload)
        return self._envelope(data, warning_codes)

    def system(self) -> dict[str, object]:
        """Return cached update facts. Do not probe PATH or start a network check."""
        warnings: set[str] = set()
        try:
            update = _update_payload(self._update_loader(), current=self._version_loader())
        except (DyroError, ValidationError, OSError, UnicodeError):
            update = _empty_update()
            warnings.add("UPDATE_STATE_UNAVAILABLE")
        return self._envelope(
            {
                "tool_inspection": "not_inspected",
                "tools": [],
                "update": update,
            },
            warnings,
        )

    def inspect_proofs(self, alias: str) -> dict[str, object]:
        """Independent Proof inspect. Must not use the summary snapshot_loader."""
        try:
            alias = validate_id(alias, "工作区别名")
        except ValidationError:
            raise ConsoleOverviewError("WORKSPACE_ALIAS_INVALID") from None
        registry = self._load_registry()
        try:
            record = next(item for item in registry.workspaces if item.name == alias)
        except StopIteration:
            raise ConsoleOverviewError("WORKSPACE_NOT_FOUND") from None
        try:
            config = self._config_loader(record.root)
            snapshot = self._inspect_loader(config)
            warning_codes = {_safe_code(failure.code) for failure in snapshot.failures}
            warning_codes.discard("REDACTED")
            return self._envelope(proof_inspect_data(snapshot), warning_codes)
        except (DyroError, ValidationError, OSError, UnicodeError):
            return self._envelope(
                {
                    "proof_inspection": "not_inspected",
                    "proofs": [],
                    "objectives": [],
                },
                {"WORKSPACE_UNAVAILABLE"},
            )

    def _envelope(
        self, data: dict[str, object], warning_codes: set[str]
    ) -> dict[str, object]:
        partial = bool(warning_codes)
        freshness_state = "partial" if partial else "fresh"
        warnings = tuple(sorted(warning_codes))
        # ``captured_at`` is intentionally excluded: re-sampling identical
        # facts may update its wall-clock value, while a warning-only change
        # must still invalidate the HTTP ETag.
        digest = _sha256(
            {
                "schema_version": _PAGE_SCHEMA_VERSION,
                "freshness": {
                    "state": freshness_state,
                    "partial": partial,
                    "warnings": list(warnings),
                },
                "data": data,
            }
        )
        return ConsoleEnvelope(
            captured_at=self._capture_time(),
            snapshot_sha256=digest,
            freshness_state=freshness_state,
            partial=partial,
            warnings=warnings,
            data=data,
        ).to_payload()

    def _workspace_config(self, alias: str) -> tuple[Config, set[str]]:
        try:
            alias = validate_id(alias, "工作区别名")
        except ValidationError:
            raise ConsoleOverviewError("WORKSPACE_ALIAS_INVALID") from None
        registry = self._load_registry()
        try:
            record = next(item for item in registry.workspaces if item.name == alias)
        except StopIteration:
            raise ConsoleOverviewError("WORKSPACE_NOT_FOUND") from None
        try:
            return self._config_loader(record.root), set()
        except (DyroError, ValidationError, OSError, UnicodeError):
            raise ConsoleOverviewError("WORKSPACE_UNAVAILABLE") from None

    def _workspace_inventory(
        self, alias: str
    ) -> tuple[dict[str, object], set[str], dict[str, list[dict[str, object]]]]:
        try:
            alias = validate_id(alias, "工作区别名")
        except ValidationError:
            raise ConsoleOverviewError("WORKSPACE_ALIAS_INVALID") from None
        registry = self._load_registry()
        try:
            record = next(item for item in registry.workspaces if item.name == alias)
        except StopIteration:
            raise ConsoleOverviewError("WORKSPACE_NOT_FOUND") from None
        return self._capture(record.name, record.root, record.name == registry.default)

    def _load_registry(self) -> WorkspaceRegistry:
        try:
            registry = self._registry_loader()
        except (DyroError, ValidationError, OSError, UnicodeError):
            raise ConsoleOverviewError("REGISTRY_UNAVAILABLE") from None
        if not isinstance(registry, WorkspaceRegistry):
            raise ConsoleOverviewError("REGISTRY_UNAVAILABLE")
        return registry

    def _capture_time(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE")
        return value.astimezone(timezone.utc)

    def _summaries(
        self, registry: WorkspaceRegistry
    ) -> tuple[list[dict[str, object]], set[str]]:
        summaries: list[dict[str, object]] = []
        warnings: set[str] = set()
        for record in registry.workspaces:
            summary, codes = self._summary(
                record.name, record.root, record.name == registry.default
            )
            summaries.append(summary)
            warnings.update(codes)
        summaries.sort(key=self._summary_sort_key)
        return summaries, warnings

    def _load_summaries(
        self, registry: WorkspaceRegistry
    ) -> tuple[list[dict[str, object]], set[str]]:
        if self._summary_loader is None:
            return self._summaries(registry)
        summaries, warnings = self._summary_loader(registry)
        if not isinstance(summaries, list) or not isinstance(warnings, set):
            raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE")
        copied = [dict(item) for item in summaries if isinstance(item, dict)]
        if len(copied) != len(summaries) or not all(isinstance(item, str) for item in warnings):
            raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE")
        copied.sort(key=self._summary_sort_key)
        return copied, set(warnings)

    def _summary(
        self, alias: str, root: Path, is_default: bool
    ) -> tuple[dict[str, object], set[str]]:
        summary, warnings, _inventory = self._capture(alias, root, is_default)
        return summary, warnings

    def _capture(
        self, alias: str, root: Path, is_default: bool
    ) -> tuple[dict[str, object], set[str], dict[str, list[dict[str, object]]]]:
        safe_alias = _safe_code(alias)
        try:
            config = self._config_loader(root)
            snapshot = self._snapshot_loader(config)
            envelope = workspace_envelope(snapshot)
        except (DyroError, ValidationError, OSError, UnicodeError):
            return (
                {
                    "alias": safe_alias,
                    "display_name": safe_alias,
                    "is_default": is_default,
                    "availability": "unavailable",
                    "health": "unavailable",
                    "freshness": "partial",
                    "repository_count": 0,
                    "line_count": 0,
                    "objective_count": 0,
                    "active_objective_count": 0,
                    "task_count": 0,
                    "task_status_counts": {},
                    "attention_counts": self._empty_attention_counts(),
                    "recommendation": {
                        "reason": "WORKSPACE_UNAVAILABLE",
                        "command": f"dyro --workspace {safe_alias} doctor",
                    },
                    "snapshot_sha256": "",
                    "proof_inspection": "not_inspected",
                },
                {"WORKSPACE_UNAVAILABLE"},
                _empty_inventory(),
            )

        data = _safe_mapping(envelope.get("data"))
        workspace = _safe_mapping(data.get("workspace"))
        tasks = _safe_list(data.get("tasks"))
        objectives = _safe_list(data.get("objectives"))
        lines = _safe_list(data.get("lines"))
        freshness = _safe_mapping(envelope.get("freshness"))
        warning_codes = {
            _safe_code(item.get("code"))
            for item in _safe_list(freshness.get("warnings"))
        }
        warning_codes.discard("REDACTED")
        task_status_counts: dict[str, int] = {}
        for task in tasks:
            status = _safe_code(task.get("status"))
            task_status_counts[status] = task_status_counts.get(status, 0) + 1
        attention = self._workspace_attention(objectives)
        partial = freshness.get("state") != "fresh"
        health = "degraded" if partial else "healthy"
        summary = {
            "alias": safe_alias,
            "display_name": safe_title(getattr(config, "name", workspace.get("name"))),
            "is_default": is_default,
            "availability": "available",
            "health": health,
            "freshness": "partial" if partial else "fresh",
            "repository_count": len(getattr(config, "repositories", {})),
            "line_count": len(lines),
            "objective_count": len(objectives),
            "active_objective_count": sum(
                1 for item in objectives if item.get("operator_state") == "active"
            ),
            "task_count": len(tasks),
            "task_status_counts": dict(sorted(task_status_counts.items())),
            "attention_counts": attention["counts"],
            "recommendation": self._recommendation(safe_alias, attention["items"]),
            "snapshot_sha256": str(envelope.get("snapshot_sha256", "")),
            "proof_inspection": "not_inspected",
        }
        return summary, warning_codes, _inventory_from_envelope(data)

    @staticmethod
    def _empty_attention_counts() -> dict[str, int]:
        return {kind: 0 for kind in _ATTENTION_PRIORITY}

    def _workspace_attention(
        self, objectives: list[dict[str, object]]
    ) -> dict[str, object]:
        counts = self._empty_attention_counts()
        items: list[dict[str, str]] = []
        for objective in objectives:
            objective_id = _safe_code(objective.get("id"))
            for raw in _safe_list(objective.get("attention")):
                kind = _safe_code(raw.get("kind"))
                reason = _safe_code(raw.get("reason"))
                if kind not in _ATTENTION_PRIORITY or reason == "PROOF_DECAYED":
                    continue
                counts[kind] += 1
                items.append(
                    {
                        "objective_id": objective_id,
                        "kind": kind,
                        "subject_id": _safe_code(raw.get("subject_id")),
                        "reason": reason,
                    }
                )
        items.sort(
            key=lambda item: (
                _ATTENTION_PRIORITY[item["kind"]],
                item["objective_id"],
                item["subject_id"],
                item["reason"],
            )
        )
        return {"counts": counts, "items": items}

    def _recommendation(
        self, alias: str, attention: object
    ) -> dict[str, str] | None:
        if not isinstance(attention, list) or not attention:
            return {
                "reason": "HOME_GUIDANCE",
                "command": f"dyro --workspace {alias}",
            }
        item = attention[0]
        if not isinstance(item, dict):
            return None
        objective_id = _safe_code(item.get("objective_id"))
        return {
            "reason": _safe_code(item.get("reason")),
            "command": " ".join(
                (
                    "dyro",
                    "--workspace",
                    alias,
                    *follow_up_from_kind(_safe_code(item.get("kind")), objective_id),
                )
            ),
        }

    def _attention_counts(self, summaries: list[dict[str, object]]) -> dict[str, int]:
        counts = self._empty_attention_counts()
        for summary in summaries:
            for kind, value in _safe_mapping(summary.get("attention_counts")).items():
                if kind in counts and type(value) is int and value >= 0:
                    counts[kind] += value
        return counts

    def _task_status_counts(self, summaries: list[dict[str, object]]) -> dict[str, int]:
        """Sum task statuses from readable workspaces only.

        Unavailable cards keep empty maps. Counting them as 0 would pretend
        unread workspaces have no work.
        """
        counts: dict[str, int] = {}
        for summary in summaries:
            if summary.get("availability") != "available":
                continue
            for status, value in _safe_mapping(summary.get("task_status_counts")).items():
                code = _safe_code(status)
                if code == "REDACTED" or type(value) is not int or value < 0:
                    continue
                counts[code] = counts.get(code, 0) + value
        return dict(sorted(counts.items()))

    def _highest_priority(self, summaries: list[dict[str, object]]) -> dict[str, str] | None:
        candidates: list[tuple[int, str, dict[str, str]]] = []
        for summary in summaries:
            recommendation = summary.get("recommendation")
            counts = _safe_mapping(summary.get("attention_counts"))
            if not isinstance(recommendation, dict):
                continue
            for kind, priority in _ATTENTION_PRIORITY.items():
                if counts.get(kind, 0):
                    candidates.append(
                        (
                            priority,
                            str(summary.get("alias", "")),
                            {
                                "alias": _safe_code(summary.get("alias")),
                                "kind": kind,
                                "reason": _safe_code(recommendation.get("reason")),
                            },
                        )
                    )
                    break
        if not candidates:
            return None
        return min(candidates, key=lambda item: (item[0], item[1]))[2]

    @staticmethod
    def _summary_sort_key(summary: dict[str, object]) -> tuple[int, str]:
        counts = _safe_mapping(summary.get("attention_counts"))
        if summary.get("availability") == "unavailable" or counts.get("repair_required", 0):
            priority = 0
        elif counts.get("needs_user", 0):
            priority = 1
        elif summary.get("active_objective_count", 0) or summary.get("task_status_counts", {}).get("in_progress", 0):
            priority = 2
        elif counts.get("paused", 0):
            priority = 3
        elif counts.get("waiting", 0):
            priority = 4
        else:
            priority = 5
        return priority, str(summary.get("alias", ""))

    def _encode_cursor(self, offset: int, aggregate_sha256: str) -> str:
        body = _stable_json(
            {
                "schema_version": _PAGE_SCHEMA_VERSION,
                "offset": offset,
                "aggregate_sha256": aggregate_sha256,
            }
        )
        signature = hmac.new(self._cursor_secret, body, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(body + signature).rstrip(b"=").decode("ascii")

    def _decode_cursor(self, value: str, aggregate_sha256: str) -> int:
        if not isinstance(value, str) or not value or len(value) > _CURSOR_MAX_LENGTH:
            raise ConsoleOverviewError("OVERVIEW_CURSOR_INVALID")
        try:
            raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        except (ValueError, UnicodeError):
            raise ConsoleOverviewError("OVERVIEW_CURSOR_INVALID") from None
        if len(raw) <= hashlib.sha256().digest_size:
            raise ConsoleOverviewError("OVERVIEW_CURSOR_INVALID")
        body, signature = raw[:-32], raw[-32:]
        expected = hmac.new(self._cursor_secret, body, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, signature):
            raise ConsoleOverviewError("OVERVIEW_CURSOR_INVALID")
        try:
            decoded: Any = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ConsoleOverviewError("OVERVIEW_CURSOR_INVALID") from None
        if (
            not isinstance(decoded, dict)
            or set(decoded) != {"schema_version", "offset", "aggregate_sha256"}
            or decoded["schema_version"] != _PAGE_SCHEMA_VERSION
            or type(decoded["offset"]) is not int
            or decoded["offset"] < 0
            or decoded["aggregate_sha256"] != aggregate_sha256
        ):
            raise ConsoleOverviewError("OVERVIEW_CURSOR_INVALID")
        return decoded["offset"]

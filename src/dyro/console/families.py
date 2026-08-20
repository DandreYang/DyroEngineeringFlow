"""Console DTO for one-level family graphs. Channel POST is P2 and absent."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from ..families import OPERATOR_ID, family_children, family_graph, family_ids
from .redaction import REDACTED, safe_id


def _line_id(value: object) -> str:
    token = safe_id(value)
    return "" if token == REDACTED else token


def family_badges(
    lines: Sequence[Mapping[str, object]],
    tasks: Sequence[Mapping[str, object]],
) -> dict[str, dict[str, object]]:
    """P1 badges: in-progress from tasks; dirty / missing-origin are uninspected."""
    in_progress: set[str] = set()
    for task in tasks:
        if task.get("status") == "in_progress":
            line_id = _line_id(task.get("line"))
            if line_id:
                in_progress.add(line_id)
    badges: dict[str, dict[str, object]] = {}
    for line in lines:
        line_id = _line_id(line.get("id"))
        if not line_id:
            continue
        badges[line_id] = {
            "dirty": False,
            "missing_origin": False,
            "in_progress": line_id in in_progress,
            "unread": 0,
        }
    badges[OPERATOR_ID] = {
        "dirty": False,
        "missing_origin": False,
        "in_progress": False,
        "unread": 0,
    }
    return badges


def family_cards(
    lines: Sequence[Mapping[str, object]],
    tasks: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    badges = family_badges(lines, tasks)
    cards: list[dict[str, object]] = []
    for parent_id in family_ids(lines):
        safe_parent = _line_id(parent_id)
        if not safe_parent:
            continue
        children = [_line_id(item) for item in family_children(lines, safe_parent)]
        children = [item for item in children if item]
        marks = [badges.get(safe_parent, {}), *(badges.get(child, {}) for child in children)]
        cards.append(
            {
                "parent": safe_parent,
                "children": children,
                "unread": 0,
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
) -> dict[str, object]:
    graph = family_graph(lines, parent_id, badges=family_badges(lines, tasks))
    return dict(graph)

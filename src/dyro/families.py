"""One-level line families: ``F(P) = {P} ∪ children(P) ∪ {operator}``.

P1 uses this only to project a graph.  Channel, ack, and artifact stores are
P2 / P3 and are not created here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping


OPERATOR_ID = "operator"


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
    """Return the P1 graph for ``F(parent_id)``.

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

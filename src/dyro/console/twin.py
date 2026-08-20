"""Read-only operator twin for workspace detail.

The twin composes already-captured Objective and Task DTOs with overlay
``events.jsonl`` and one redacted ledger line.  It does not invent
Objectives, claim a board landed without a ``board`` event, or write
``.dyro`` / git.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
import re

from ..events import read_overlay_events
from .redaction import REDACTED, safe_id, safe_title


TASK_STATUSES = (
    "backlog",
    "assigned",
    "in_progress",
    "waiting_answer",
    "review",
    "review_pending_signoff",
    "done",
    "failed",
)
MILESTONES = frozenset({"incomplete", "complete", "repair_required"})
DISPATCH_STATES = frozenset({"running", "idle", "unknown"})
MAX_LEDGER_BYTES = 2 * 1024 * 1024
_LEDGER_TS = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:Z|[+-][0-9]{2}:[0-9]{2})?$"
)
_BLOCKED_LEDGER_KEYS = frozenset(
    {
        "prompt",
        "answer",
        "handoff",
        "argv",
        "env",
        "stdout",
        "stderr",
        "error",
        "command",
        "path",
        "root",
        "cwd",
        "url",
        "remote",
    }
)


def empty_operator_twin() -> dict[str, object]:
    return {
        "plan": [],
        "phases": [{"status": status, "tasks": []} for status in TASK_STATUSES],
        "running": [],
        "latest_ledger": {
            "present": False,
            "at": "",
            "task_id": "",
            "phase": "",
            "facts": {},
        },
        "projected_seq": 0,
        "overlay_complete": True,
    }


def _safe_token(value: object) -> str:
    token = safe_id(value)
    return "" if token == REDACTED else token


def _safe_label(value: object) -> str:
    title = safe_title(value)
    return title if title != REDACTED else REDACTED if isinstance(value, str) and value else ""


def _task_card(task: Mapping[str, object]) -> dict[str, str]:
    status = _safe_token(task.get("status"))
    return {
        "id": _safe_token(task.get("id")),
        "title": _safe_label(task.get("title")),
        "line": _safe_token(task.get("line")),
        "executor": _safe_token(task.get("executor")),
        "status": status if status in TASK_STATUSES else "",
    }


def _dispatch_state(facts: Mapping[str, object]) -> str:
    phase = facts.get("phase")
    status = facts.get("status")
    if phase == "start":
        return "running"
    if phase == "end" and status == "idle":
        return "idle"
    return "unknown"


def _event_task_id(event: Mapping[str, object], task_ids: set[str]) -> str:
    subject = event.get("subject")
    if isinstance(subject, str) and subject in task_ids:
        return subject
    facts = event.get("facts")
    if isinstance(facts, dict):
        for key in ("task_id", "task"):
            value = facts.get(key)
            if isinstance(value, str) and value in task_ids:
                return value
    return ""


def _objective_id(event: Mapping[str, object], objective_ids: set[str]) -> str:
    for key in ("subject", "actor"):
        value = event.get(key)
        if isinstance(value, str) and value in objective_ids:
            return value
    return ""


def _project_latest_ledger(config: object) -> dict[str, object]:
    empty = {
        "present": False,
        "at": "",
        "task_id": "",
        "phase": "",
        "facts": {},
    }
    path = getattr(config, "ledger_file", None)
    if not isinstance(path, Path):
        return empty
    try:
        if path.is_symlink() or not path.is_file():
            return empty
        size = path.stat().st_size
        if size <= 0 or size > MAX_LEDGER_BYTES:
            return empty
        text = path.read_text(encoding="utf-8")
    except OSError:
        return empty
    if not text.endswith("\n"):
        return empty
    lines = [line for line in text.splitlines() if line]
    if not lines:
        return empty
    try:
        decoded = json.loads(lines[-1])
    except json.JSONDecodeError:
        return empty
    if not isinstance(decoded, dict):
        return empty
    raw_at = decoded.get("ts")
    at = raw_at if isinstance(raw_at, str) and len(raw_at) <= 40 and _LEDGER_TS.fullmatch(raw_at) else ""
    task_id = _safe_token(decoded.get("task_id"))
    phase = _safe_token(decoded.get("phase"))
    facts: dict[str, str | int | bool] = {}
    for key, value in decoded.items():
        if key in {"ts", "task_id", "phase"} or key in _BLOCKED_LEDGER_KEYS:
            continue
        from .events import is_safe_event_fact

        if not is_safe_event_fact(key, value):
            continue
        token_key = _safe_token(key)
        if not token_key:
            continue
        if type(value) is bool or (type(value) is int and not isinstance(value, bool)):
            facts[token_key] = value
        else:
            cleaned = _safe_token(value)
            if cleaned:
                facts[token_key] = cleaned
    return {
        "present": True,
        "at": at,
        "task_id": task_id,
        "phase": phase,
        "facts": facts,
    }


def project_operator_twin(
    config: object,
    inventory: Mapping[str, object] | None,
) -> dict[str, object]:
    """Compose a fail-closed twin from inventory plus overlay reads."""
    twin = empty_operator_twin()
    source = inventory if isinstance(inventory, Mapping) else {}
    raw_tasks = source.get("tasks")
    raw_objectives = source.get("objectives")
    tasks = [item for item in raw_tasks if isinstance(item, dict)] if isinstance(raw_tasks, list) else []
    objectives = (
        [item for item in raw_objectives if isinstance(item, dict)]
        if isinstance(raw_objectives, list)
        else []
    )
    task_by_id: dict[str, dict[str, object]] = {}
    for task in tasks:
        task_id = _safe_token(task.get("id"))
        if task_id:
            task_by_id[task_id] = task
    objective_ids = {token for token in (_safe_token(item.get("id")) for item in objectives) if token}

    latest_wave: dict[str, Mapping[str, object]] = {}
    latest_dispatch: dict[str, Mapping[str, object]] = {}
    board_tasks: set[str] = set()
    records, overlay_complete = read_overlay_events(config)
    projected_seq = 0
    if overlay_complete and records:
        last_seq = records[-1].get("seq")
        projected_seq = last_seq if type(last_seq) is int and last_seq >= 0 else 0
    for event in records if overlay_complete else ():
        kind = event.get("kind")
        if kind == "objective_wave":
            objective_id = _objective_id(event, objective_ids)
            if objective_id:
                latest_wave[objective_id] = event
        elif kind == "dispatch":
            task_id = _event_task_id(event, set(task_by_id))
            if task_id:
                latest_dispatch[task_id] = event
        elif kind == "board":
            task_id = _event_task_id(event, set(task_by_id))
            if task_id:
                board_tasks.add(task_id)

    plan: list[dict[str, object]] = []
    for objective in sorted(objectives, key=lambda item: _safe_token(item.get("id"))):
        objective_id = _safe_token(objective.get("id"))
        if not objective_id:
            continue
        derived = _safe_token(objective.get("derived_result"))
        selected = objective.get("selected_actions")
        task_ids: list[str] = []
        if isinstance(selected, list):
            for action in selected:
                if not isinstance(action, dict):
                    continue
                subject = _safe_token(action.get("subject_id"))
                if subject and subject in task_by_id and subject not in task_ids:
                    task_ids.append(subject)
        from .events import project_event

        wave = latest_wave.get(objective_id)
        projected = project_event(wave) if wave is not None else None
        facts = projected.get("facts") if projected else {}
        mode = facts.get("mode") if isinstance(facts, dict) else ""
        count = facts.get("count") if isinstance(facts, dict) else 0
        plan.append(
            {
                "id": objective_id,
                "title": _safe_label(objective.get("title")),
                "line": _safe_token(objective.get("line")),
                "milestone": derived if derived in MILESTONES else "",
                "wave_present": wave is not None,
                "wave_id": _safe_token(projected.get("id")) if projected else "",
                "wave_at": str(projected.get("at") or "") if projected and isinstance(projected.get("at"), str) and len(str(projected.get("at"))) <= 40 else "",
                "wave_mode": mode if isinstance(mode, str) and _safe_token(mode) == mode else "",
                "wave_count": count if type(count) is int and not isinstance(count, bool) and 0 <= count <= 1_000_000 else 0,
                "task_ids": task_ids,
            }
        )
    twin["plan"] = plan

    phases = {status: [] for status in TASK_STATUSES}
    for task in tasks:
        card = _task_card(task)
        if not card["id"] or card["status"] not in TASK_STATUSES:
            continue
        phases[card["status"]].append(card)
    twin["phases"] = [
        {
            "status": status,
            "tasks": sorted(phases[status], key=lambda item: item["id"]),
        }
        for status in TASK_STATUSES
    ]

    running: list[dict[str, object]] = []
    for task_id, task in sorted(task_by_id.items()):
        if _safe_token(task.get("status")) != "in_progress":
            continue
        card = _task_card(task)
        from .events import project_event

        event = latest_dispatch.get(task_id)
        projected = project_event(event) if event is not None else None
        facts = dict(projected.get("facts") or {}) if projected else {}
        running.append(
            {
                "id": card["id"],
                "title": card["title"],
                "line": card["line"],
                "executor": card["executor"],
                "dispatch_present": event is not None,
                "dispatch_id": _safe_token(projected.get("id")) if projected else "",
                "dispatch_at": str(projected.get("at") or "") if projected and isinstance(projected.get("at"), str) and len(str(projected.get("at"))) <= 40 else "",
                "dispatch_state": _dispatch_state(facts) if event is not None else "unknown",
                "dispatch_facts": facts,
                "board_landed": task_id in board_tasks,
            }
        )
    twin["running"] = running
    twin["latest_ledger"] = _project_latest_ledger(config)
    twin["projected_seq"] = projected_seq
    twin["overlay_complete"] = overlay_complete
    return twin

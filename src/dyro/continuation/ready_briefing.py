"""Workspace-scoped switch-tool briefing. Read-only; no session resume."""

from __future__ import annotations

import shlex

from ..config import Config
from ..errors import DyroError, ValidationError
from ..read_limits import ReadBudget, ReadLimitError
from .briefing import (
    briefing_payload,
    follow_up_argv,
    inventory_briefing,
    unread_briefing,
)
from .planner import build_continuation_plan
from .snapshot import build_scheduler_snapshot, build_scheduler_snapshot_bounded
from .store import get_objective, list_objectives


def briefing_command(alias: str, *command: str) -> str:
    """Scope a read-only command without embedding --root paths."""
    return shlex.join(("dyro", "--workspace", alias, *command))


def _read_plan(
    config: Config,
    objective_id: str,
    read_budget: ReadBudget | None,
):
    record = get_objective(
        config, objective_id, recover=False, read_budget=read_budget
    )
    snapshot = (
        build_scheduler_snapshot(config, objective=record)
        if read_budget is None
        else build_scheduler_snapshot_bounded(
            config, objective=record, budget=read_budget
        )
    )
    return record, build_continuation_plan(snapshot)


def build_ready_briefing(
    config: Config,
    *,
    alias: str,
    read_budget: ReadBudget | None = None,
) -> tuple[dict[str, object] | None, list[str]]:
    """Return a path-free opening when live Objectives exist.

    The command is a read (`tick`, `attention`, `explain`, or `list`),
    never apply or a cross-harness chat resume.
    """
    try:
        records = [
            record
            for record in list_objectives(
                config, recover=False, read_budget=read_budget
            )
            if record.operator_state != "stopped"
        ]
    except (DyroError, ValidationError, OSError, ReadLimitError):
        command = briefing_command(alias, "objective", "list")
        return unread_briefing(command), [command]
    if not records:
        return None, []
    if len(records) > 1:
        command = briefing_command(alias, "objective", "list")
        return inventory_briefing(command, len(records)), [command]
    record = records[0]
    explain = briefing_command(alias, "objective", "explain", record.objective.id)
    try:
        stored, plan = _read_plan(config, record.objective.id, read_budget)
    except (DyroError, ValidationError, OSError, ReadLimitError):
        return unread_briefing(explain), [explain]
    command = briefing_command(alias, *follow_up_argv(plan))
    return (
        briefing_payload(plan, command=command, title=stored.objective.title),
        [command],
    )

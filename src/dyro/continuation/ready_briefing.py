"""Workspace-scoped switch-tool briefing. Read-only; no session resume."""

from __future__ import annotations

import shlex

from ..config import Config
from ..errors import DyroError, ValidationError
from ..hub import (
    alias_fold_collides,
    load_registry,
    unique_registered_alias,
    workspace_alias_retargets_root,
)
from ..read_limits import ReadBudget, ReadLimitCode, ReadLimitError
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


def _root_scoped_command(config: Config, *command: str) -> str:
    return shlex.join(("dyro", "--root", str(config.root), *command))


def scoped_briefing_command(
    config: Config,
    alias: str,
    *command: str,
    names: tuple[str, ...] | None = None,
) -> str:
    """Advertise ``--workspace`` only when that selector stays on this root.

    A unique fold uses the canonical registered spelling when that record is
    the current workspace. An unregistered profile name keeps ``--workspace``
    so path-free next ads stay path-free. A fold collision, a unique fold
    that would resolve to a different root, or a registry read that cannot
    prove the selector stays here, switches the ad to ``--root``.
    """
    try:
        records = tuple(load_registry().workspaces)
    except (DyroError, ValidationError, OSError, TypeError, AttributeError):
        if getattr(config, "root", None) is not None:
            return _root_scoped_command(config, *command)
        records = ()
    registered = names if names is not None else tuple(item.name for item in records)
    if alias_fold_collides(alias, registered):
        return _root_scoped_command(config, *command)
    root = getattr(config, "root", None)
    if root is not None and workspace_alias_retargets_root(
        alias, root, workspaces=records or None
    ):
        return _root_scoped_command(config, *command)
    canonical = unique_registered_alias(alias, registered) or alias
    return briefing_command(canonical, *command)


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
    except ReadLimitError as exc:
        if exc.code == ReadLimitCode.DEADLINE_EXCEEDED:
            raise
        command = scoped_briefing_command(config, alias, "objective", "list")
        return unread_briefing(command), [command]
    except (DyroError, ValidationError, OSError):
        command = scoped_briefing_command(config, alias, "objective", "list")
        return unread_briefing(command), [command]
    if not records:
        return None, []
    if len(records) > 1:
        command = scoped_briefing_command(config, alias, "objective", "list")
        return inventory_briefing(command, len(records)), [command]
    record = records[0]
    explain = scoped_briefing_command(
        config, alias, "objective", "explain", record.objective.id
    )
    try:
        stored, plan = _read_plan(config, record.objective.id, read_budget)
    except ReadLimitError as exc:
        if exc.code == ReadLimitCode.DEADLINE_EXCEEDED:
            raise
        return unread_briefing(explain), [explain]
    except (DyroError, ValidationError, OSError):
        return unread_briefing(explain), [explain]
    command = scoped_briefing_command(config, alias, *follow_up_argv(plan))
    return (
        briefing_payload(plan, command=command, title=stored.objective.title),
        [command],
    )

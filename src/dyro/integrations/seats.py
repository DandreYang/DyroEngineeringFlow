"""First-party Skill seats: auto-trigger load, never auto-mutate."""

from __future__ import annotations

from dataclasses import dataclass


CONTROL_PLANE_ID = "skill"
DISPATCH_ID = "dispatch"
EXECUTOR_ID = "executor"
BOARD_ID = "board"

CONTROL_PLANE_SKILL = "dyro-control-plane"
DISPATCH_SKILL = "dyro-dispatch"
EXECUTOR_SKILL = "dyro-executor"
BOARD_SKILL = "dyro-board"


@dataclass(frozen=True)
class SeatSpec:
    integration_id: str
    skill_name: str
    label: str
    intent: str
    companion: bool
    trigger_terms: tuple[str, ...]


SEATS: tuple[SeatSpec, ...] = (
    SeatSpec(
        CONTROL_PLANE_ID,
        CONTROL_PLANE_SKILL,
        "控制面",
        "observe",
        False,
        (
            "what is next",
            "下一步",
            "堵住了",
            "switching coding tools",
            "Auto-load this navigator seat",
        ),
    ),
    SeatSpec(
        DISPATCH_ID,
        DISPATCH_SKILL,
        "Dispatch",
        "dispatch",
        True,
        (
            "parallelize",
            "并行",
            "对比",
            "几个 agent",
            "Auto-load this seat on those words",
        ),
    ),
    SeatSpec(
        EXECUTOR_ID,
        EXECUTOR_SKILL,
        "执行",
        "execute",
        True,
        (
            "task worktree",
            "修这个",
            "做完",
            "implement",
            "Auto-load this executor seat",
        ),
    ),
    SeatSpec(
        BOARD_ID,
        BOARD_SKILL,
        "会审",
        "board",
        True,
        (
            "会审",
            "对抗",
            "能不能发",
            "Go/No-Go",
            "Auto-load this board seat",
        ),
    ),
)

FIRST_BATCH_IDS: tuple[str, ...] = tuple(seat.integration_id for seat in SEATS)
COMPANION_IDS: tuple[str, ...] = tuple(
    seat.integration_id for seat in SEATS if seat.companion
)


def seat_by_id(integration_id: str) -> SeatSpec:
    for seat in SEATS:
        if seat.integration_id == integration_id:
            return seat
    raise KeyError(integration_id)


def managed_skill_bundle() -> tuple[tuple[str, str], ...]:
    return tuple((seat.integration_id, seat.label) for seat in SEATS)


def select_launch_seat(*, task: str = "", line: str = "") -> str:
    """Pick the seat that must auto-load when Dyro opens a tool."""
    _ = line
    if str(task).strip():
        return EXECUTOR_ID
    return CONTROL_PLANE_ID


def render_seat_notice(seat_id: str) -> str:
    seat = seat_by_id(seat_id)
    if seat.integration_id == EXECUTOR_ID:
        closer = "只在本任务 worktree 写；不要 merge / push / 恢复另一家会话。"
    elif seat.integration_id == CONTROL_PLANE_ID:
        closer = "先观察 next / attention；不要自己 apply 或 merge。"
    elif seat.integration_id == DISPATCH_ID:
        closer = "先 dry-run 拆法；本轮没有并行意图就不要启动 Provider。"
    else:
        closer = "意见写入共享审查文件；不要当成 Proof 或 task review PASS。"
    return f"座位  {seat.label} · {seat.skill_name}\n{closer}"

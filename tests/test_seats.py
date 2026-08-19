from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import re
import unittest
from unittest.mock import Mock, patch

from dyro.home import HomeTool, launch_start_tool
from dyro.integrations import manager
from dyro.integrations.seats import (
    BOARD_ID,
    COMPANION_IDS,
    CONTROL_PLANE_ID,
    EXECUTOR_ID,
    FIRST_BATCH_IDS,
    SEATS,
    managed_skill_bundle,
    render_seat_notice,
    select_launch_seat,
)
from dyro.tooling import ToolState


class FirstBatchSeatTests(unittest.TestCase):
    def test_first_batch_is_four_seats_and_omits_reviewer(self) -> None:
        self.assertEqual(
            FIRST_BATCH_IDS,
            ("skill", "dispatch", "executor", "board"),
        )
        self.assertEqual(COMPANION_IDS, ("dispatch", "executor", "board"))
        self.assertEqual(
            managed_skill_bundle(),
            (
                ("skill", "控制面"),
                ("dispatch", "Dispatch"),
                ("executor", "执行"),
                ("board", "会审"),
            ),
        )
        names = {seat.skill_name for seat in SEATS}
        self.assertNotIn("dyro-reviewer", names)

    def test_launch_seat_follows_task_then_navigator(self) -> None:
        self.assertEqual(select_launch_seat(task="API-101", line="dev"), EXECUTOR_ID)
        self.assertEqual(select_launch_seat(task="", line="dev"), CONTROL_PLANE_ID)
        notice = render_seat_notice(EXECUTOR_ID)
        self.assertIn("dyro-executor", notice)
        self.assertIn("不要 merge", notice)
        self.assertNotIn(BOARD_ID, notice)

    def test_launch_start_tool_prints_seat_for_desktop_launcher(self) -> None:
        tool = HomeTool(
            "cursor",
            "Cursor Desktop",
            "launcher",
            ("open", "-a", "Cursor"),
            (),
            ToolState.READY,
        )
        output = StringIO()
        with (
            patch("dyro.home.launch_home_tool") as launch,
            redirect_stdout(output),
        ):
            launch_start_tool(
                Mock(),
                workspace=Path("/tmp/ws"),
                tool=tool,
                line="dev",
                task="API-101",
                dry_run=True,
            )
        launch.assert_called_once()
        self.assertIn("座位  执行 · dyro-executor", output.getvalue())

    def test_packaged_first_batch_skills_auto_trigger_and_stay_bounded(self) -> None:
        for seat in SEATS:
            root = manager._asset_root(seat.integration_id)
            skill = root / "SKILL.md"
            metadata = root / "agents" / "openai.yaml"
            content = skill.read_text(encoding="utf-8")
            self.assertLessEqual(
                len(content.encode("utf-8")), 8 * 1024, msg=seat.skill_name
            )
            self.assertNotIn("TODO", content)
            frontmatter = content.split("---", 2)[1]
            keys = {
                line.split(":", 1)[0]
                for line in frontmatter.splitlines()
                if line.strip()
            }
            self.assertEqual(keys, {"name", "description"}, msg=seat.skill_name)
            self.assertIn(f"name: {seat.skill_name}", frontmatter)
            lowered = content.lower()
            for term in seat.trigger_terms:
                self.assertIn(term.lower(), lowered, msg=f"{seat.skill_name}: {term}")
            self.assertIn(f"${seat.skill_name}", metadata.read_text(encoding="utf-8"))
            description = frontmatter.split("description:", 1)[1].strip()
            self.assertLessEqual(len(description), 1024, msg=seat.skill_name)
            self.assertNotIn("~/", content)
            for pattern in (
                r"/Users/[^<\s]",
                r"/home/[^<\s]",
                r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
            ):
                self.assertIsNone(
                    re.search(pattern, content),
                    msg=f"{seat.skill_name}: {pattern}",
                )

    def test_slash_skills_are_packaged_but_not_auto_load_seats(self) -> None:
        names = {seat.skill_name for seat in SEATS}
        self.assertNotIn("dyro-review-board", names)
        self.assertNotIn("dyro-task-merge", names)
        metadata = Path(__file__).resolve().parents[1].joinpath("pyproject.toml")
        text = metadata.read_text(encoding="utf-8")
        for skill_name in ("dyro-review-board", "dyro-task-merge"):
            self.assertIn(f"assets/{skill_name}/SKILL.md", text)
            content = (
                Path(__file__).resolve().parents[1]
                / "src"
                / "dyro"
                / "integrations"
                / "assets"
                / skill_name
                / "SKILL.md"
            ).read_text(encoding="utf-8")
            self.assertIn("disable-model-invocation: true", content)
            self.assertNotIn("TODO", content)

    def test_wheel_package_data_lists_every_first_batch_skill(self) -> None:
        metadata = Path(__file__).resolve().parents[1].joinpath("pyproject.toml")
        text = metadata.read_text(encoding="utf-8")
        for seat in SEATS:
            self.assertIn(f"assets/{seat.skill_name}/SKILL.md", text)
            self.assertIn(f"assets/{seat.skill_name}/agents/openai.yaml", text)

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path


HARNESS = Path(__file__).resolve().parent / "support" / "console_operator.mjs"


def _run(action: str) -> dict[str, object]:
    node = shutil.which("node")
    if not node:
        raise AssertionError("node is required to exercise the console operator surface")
    completed = subprocess.run(
        [node, str(HARNESS)],
        input=json.dumps({"action": action}),
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout or "operator harness failed")
    return json.loads(completed.stdout)


class ConsoleOperatorSurfaceTests(unittest.TestCase):
    def test_fail_findings_and_empty_commands_are_not_unknown_or_bare(self) -> None:
        result = _run("fail_overview")

        self.assertNotEqual(result["heading"], "关注项未知")
        self.assertEqual(result["heading"], "需要修复")
        self.assertNotEqual(result["command"], "dyro --workspace core")
        self.assertEqual(result["command"], "dyro --workspace core doctor")
        self.assertNotIn("摘要未列出关注项", result["needsYou"])
        self.assertIn("core", result["needsYou"])
        self.assertIn("release_a", result["needsYou"])

    def test_tablist_switch_changes_visible_section_ids(self) -> None:
        result = _run("tabs")

        self.assertEqual(result["tabs"], ["family", "events", "channel"])
        visible = {row["tab"]: row["visible"] for row in result["switches"]}
        self.assertEqual(visible["family"], ["family-pane"])
        self.assertEqual(visible["events"], ["event-pane"])
        self.assertEqual(visible["channel"], ["channel-pane"])
        self.assertEqual(
            [row["hashTab"] for row in result["switches"]],
            ["family", "events", "channel"],
        )

    def test_spawn_copy_does_not_invent_a_child_name(self) -> None:
        result = _run("spawn")

        self.assertIn("先在终端想好子线名", result["empty"])
        self.assertNotIn("core_new", result["empty"])
        self.assertNotIn("line spawn core core_new", result["empty"])
        self.assertIn("line spawn core core_pay", result["withChild"])
        self.assertIn("--dry-run", result["withChild"])
        self.assertNotIn("--yes", result["withChild"])

    def test_line_fail_badge_does_not_paint_unknown_pair(self) -> None:
        result = _run("badges")

        self.assertIn("远端跟踪分支不存在", result["fail"])
        self.assertNotIn("未检查", result["fail"])
        self.assertIn("未检查", result["unknown"])

    def test_forced_refresh_omits_etag_and_moves_captured_at(self) -> None:
        result = _run("refresh")

        self.assertTrue(result["cachedHasMatch"])
        self.assertFalse(result["forcedHasMatch"])
        self.assertNotEqual(result["before"], result["after"])
        self.assertIn("读取于", result["after"])

    def test_missing_bootstrap_explains_how_to_open_again(self) -> None:
        result = _run("session")

        self.assertIn("尚未建立", result["missing"])
        self.assertIn("dyro console", result["missing"])
        self.assertIn("dyro console", result["expired"])
        self.assertNotIn("正在建立安全本地会话", result["missing"])
        self.assertNotIn("正在建立安全本地会话", result["expired"])


if __name__ == "__main__":
    unittest.main()

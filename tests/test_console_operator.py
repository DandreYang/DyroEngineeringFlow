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
        self.assertFalse(result["refreshHasMatch"])
        self.assertFalse(result["clickHasMatch"])
        self.assertNotEqual(result["before"], result["afterRefresh"])
        self.assertNotEqual(result["before"], result["afterClick"])
        self.assertIn("读取于", result["afterRefresh"])
        self.assertIn("读取于", result["afterClick"])
        self.assertIn("读取于", result["after"])

    def test_missing_bootstrap_explains_how_to_open_again(self) -> None:
        result = _run("session")

        for door in (result["missing"], result["expired"]):
            self.assertNotEqual(door["heading"], "正在读取工程状态")
            self.assertNotEqual(door["primary"], "正在准备推荐命令…")
            self.assertEqual(door["heading"], door["helper"])
            self.assertEqual(door["primary"], "")
            self.assertTrue(door["primaryHidden"])
            self.assertNotIn("正在读取", door["center"])
            self.assertNotIn("正在准备推荐命令", door["center"])
            self.assertIn("dyro console", door["heading"])
        self.assertIn("尚未建立", result["missing"]["heading"])
        self.assertIn("尚未建立", result["missing"]["helper"])
        self.assertIn("dyro console", result["missing"]["helper"])
        self.assertIn("dyro console", result["expired"]["helper"])
        self.assertNotIn("正在建立安全本地会话", result["missing"]["status"])
        self.assertNotIn("正在建立安全本地会话", result["expired"]["status"])

    def test_ghost_test_workspace_does_not_win_command_center(self) -> None:
        result = _run("ghost_overview")

        self.assertNotIn("test-workspace", result["needsYou"])
        self.assertNotIn("test-workspace", result["needsYouAliases"])
        self.assertEqual(result["ghostCommand"], "")
        self.assertNotEqual(result["priorityAlias"], "test-workspace")
        self.assertNotEqual(result["command"], "dyro --workspace test-workspace doctor")
        self.assertNotIn("dyro --workspace test-workspace", result["primary"])
        self.assertIn("core", result["list"])
        self.assertIn("读不到", result["list"])
        self.assertIn("登记还在，工作区目录已经不在了", result["list"])
        self.assertNotEqual(result["heading"], "需要修复")

    def test_timeout_copy_is_not_missing_root(self) -> None:
        result = _run("timeout_overview")

        self.assertEqual(result["timeoutReason"], "read_timeout")
        self.assertEqual(result["missingReason"], "missing_root")
        self.assertIn("读取超时", result["timeoutMatter"])
        self.assertIn("项目还在", result["timeoutMatter"])
        self.assertIn("目录已经不在了", result["missingMatter"])
        self.assertNotEqual(result["timeoutMatter"], result["missingMatter"])
        self.assertNotIn("slow", result["needsYou"])
        self.assertNotIn("test-workspace", result["needsYou"])
        self.assertNotEqual(result["command"], "dyro --workspace slow doctor")
        self.assertNotEqual(result["command"], "dyro --workspace test-workspace doctor")
        self.assertIn("读取未完成", result["list"])
        self.assertIn("目录不在了", result["list"])

    def test_family_picker_defaults_to_roots_plus_focused_parent(self) -> None:
        result = _run("family_picker")

        self.assertEqual(result["roots"], ["core", "release_a"])
        self.assertNotIn("core_pay", result["roots"])
        self.assertNotIn("core_pay_fix", result["roots"])
        self.assertEqual(result["focused"], ["core", "release_a", "core_pay"])
        self.assertEqual(result["grandchild"], ["core", "release_a", "core_pay_fix"])
        self.assertNotIn("core_pay_fix", result["focused"])
        self.assertEqual(result["rootButtons"], ["core", "release_a"])
        self.assertEqual(result["focusedButtons"], ["core", "release_a", "core_pay"])
        self.assertEqual(result["grandchildButtons"], ["core", "release_a", "core_pay_fix"])
        self.assertNotIn("core_pay", result["rootButtons"])
        self.assertNotIn("core_pay_fix", result["rootButtons"])
        self.assertNotIn("core_pay_fix", result["focusedButtons"])

    def test_fail_outRanks_ready_in_overview_heading(self) -> None:
        result = _run("fail_over_ready")

        self.assertEqual(result["heading"], "需要修复")
        self.assertEqual(result["state"], "需要修复")
        self.assertNotEqual(result["heading"], "有工作可推进")
        self.assertNotEqual(result["state"], "有工作可推进")

    def test_hash_tab_shows_only_that_pane_without_a_click(self) -> None:
        result = _run("hash_tab")

        self.assertEqual(result["visible"], ["event-pane"])
        self.assertEqual(result["detailTab"], "events")
        self.assertNotIn("family-pane", result["visible"])
        self.assertNotIn("channel-pane", result["visible"])

    def test_empty_twin_explains_lines_without_tasks_or_objectives(self) -> None:
        result = _run("empty_twin")

        self.assertIn("2 条线", result["text"])
        self.assertIn("还没有 Task / Objective", result["text"])
        self.assertIn("谁在跑是预期的", result["text"])
        self.assertNotEqual(result["text"].strip(), "没有目标。")
        self.assertNotIn("没有目标。", result["text"])


if __name__ == "__main__":
    unittest.main()

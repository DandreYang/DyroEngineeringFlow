from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from dyro.errors import DyroError, ValidationError
from dyro.tooling import (
    ToolPreferences,
    install_tool,
    load_tool_preferences,
    save_tool_preferences,
    tool_definition,
)


class ToolPreferenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="dyro-tools-")
        self.environment = patch.dict(os.environ, {"DYRO_HOME": self.tmp.name})
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.tmp.cleanup()

    def test_preferences_round_trip_without_touching_workspace_registry(self) -> None:
        preferences = ToolPreferences(
            default_tool="cursor-desktop",
            pinned_tools=("cursor-desktop", "codex", "openclaw"),
        )

        save_tool_preferences(preferences)

        self.assertEqual(load_tool_preferences(), preferences)
        self.assertFalse(Path(self.tmp.name, "workspaces.json").exists())

    def test_preferences_reject_unknown_fields_and_duplicate_pins(self) -> None:
        state = Path(self.tmp.name)
        state.mkdir(parents=True, exist_ok=True)
        state.joinpath("tools.json").write_text(
            '{"schema_version": 1, "default_tool": "", "pinned_tools": [], "command": "bad"}',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValidationError, "未知字段"):
            load_tool_preferences()

        with self.assertRaisesRegex(ValidationError, "重复"):
            save_tool_preferences(ToolPreferences(pinned_tools=("codex", "codex")))


class GuidedInstallerTests(unittest.TestCase):
    def test_catalog_includes_antigravity_qoder_and_zcode(self) -> None:
        antigravity = tool_definition("antigravity")
        qoder = tool_definition("qoder")
        zcode = tool_definition("zcode")

        self.assertEqual(antigravity.command if antigravity else "", "agy")
        self.assertEqual(qoder.command if qoder else "", "qodercli")
        self.assertEqual(zcode.interface if zcode else "", "desktop")
        self.assertEqual(
            qoder.install.argv if qoder and qoder.install else (),
            ("npm", "install", "-g", "@qoder-ai/qodercli"),
        )

    def test_command_recipe_is_explicit_argv_and_requires_confirmation(self) -> None:
        calls: list[tuple[str, ...]] = []

        def run(argv: tuple[str, ...], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0)

        output = StringIO()
        with (
            patch(
                "dyro.tooling.shutil.which",
                side_effect=lambda name: f"/fake/{name}",
            ),
            redirect_stdout(output),
        ):
            installed = install_tool(
                "openclaw",
                yes=False,
                dry_run=False,
                ask=lambda _: "y",
                run=run,
            )

        self.assertTrue(installed)
        self.assertEqual(
            calls,
            [
                ("/fake/npm", "install", "-g", "openclaw@latest"),
                ("/fake/openclaw", "--version"),
            ],
        )
        self.assertIn("官方来源", output.getvalue())
        self.assertIn("当前 npm 全局环境", output.getvalue())
        self.assertIn("安装脚本", output.getvalue())

    def test_command_recipe_can_be_cancelled_and_dry_run_never_executes(self) -> None:
        calls: list[tuple[str, ...]] = []

        def run(argv: tuple[str, ...], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0)

        with patch(
            "dyro.tooling.shutil.which", side_effect=lambda name: f"/fake/{name}"
        ):
            self.assertFalse(
                install_tool(
                    "openclaw",
                    yes=False,
                    dry_run=False,
                    ask=lambda _: "n",
                    run=run,
                )
            )
            self.assertFalse(
                install_tool(
                    "openclaw",
                    yes=True,
                    dry_run=True,
                    run=run,
                )
            )
        self.assertEqual(calls, [])

    def test_script_only_recipe_opens_official_page_instead_of_running_shell(self) -> None:
        opened: list[str] = []
        output = StringIO()
        with redirect_stdout(output):
            installed = install_tool(
                "cursor-agent",
                yes=True,
                dry_run=False,
                open_url=lambda url: opened.append(url) or True,
            )

        self.assertFalse(installed)
        self.assertEqual(opened, ["https://docs.cursor.com/en/cli/installation"])
        self.assertIn("不会代为执行远程安装脚本", output.getvalue())

    def test_missing_package_manager_falls_back_to_actionable_error(self) -> None:
        with (
            patch("dyro.tooling.shutil.which", return_value=None),
            self.assertRaisesRegex(DyroError, "需要 npm"),
        ):
            install_tool("openclaw", yes=True, dry_run=False)

    def test_installation_fails_closed_when_version_probe_fails(self) -> None:
        results = iter((0, 2))

        def run(argv: tuple[str, ...], **_: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, next(results))

        with (
            patch(
                "dyro.tooling.shutil.which",
                side_effect=lambda name: f"/fake/{name}",
            ),
            self.assertRaisesRegex(DyroError, "版本验证失败"),
        ):
            install_tool("openclaw", yes=True, dry_run=False, run=run)

    def test_unknown_install_recipe_fails_closed(self) -> None:
        with self.assertRaisesRegex(DyroError, "没有内置安装方案"):
            install_tool("grok", yes=True, dry_run=False)


if __name__ == "__main__":
    unittest.main()

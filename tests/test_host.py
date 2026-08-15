from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dyro.cli import main
from dyro.config import load
from dyro.continuation.store import create_objective
from dyro.continuation.supervision import apply_supervised_wave, build_supervised_wave
from dyro.errors import DyroError
from dyro.host import (
    AUTHORITY_SKILL_AND_HOOK,
    AUTHORITY_SKILL_ONLY,
    assert_projections_allow_mutation,
    compile_hosts,
    inspect_projections,
    projection_root,
)
from dyro.host.compile import HOOK_NAME, SKILL_NAME
from dyro.tasks import task_template
from dyro.workspace import create_line

from .support import WorkspaceCase


def _append_toml(root: Path, fragment: str) -> None:
    path = root / "dyro.toml"
    path.write_text(path.read_text(encoding="utf-8") + fragment, encoding="utf-8")


def _strip_noop_adapter(root: Path) -> None:
    path = root / "dyro.toml"
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace(
            """
[adapters.noop]
launch = ["/usr/bin/true"]
read = ["/usr/bin/true"]
write = ["/usr/bin/true"]
""",
            "\n",
        ),
        encoding="utf-8",
    )


def _objective_contract() -> str:
    return """schema_version = 1
id = "release"
title = "Release"
line = "alpha"
targets = ["TASK-A"]

[continuation]
requested_mode = "supervised"
operations = ["execute", "review"]

[budget]
max_actions = 20
max_attempts_per_task = 2
max_failures = 3
max_no_progress_cycles = 2
max_parallel = 1
"""


class HostCompilerTests(WorkspaceCase):
    def _skill(self, host: str = "cli", *, user: bool = False) -> str:
        root = projection_root(load(self.root), user=user)
        return (root / host / SKILL_NAME).read_text(encoding="utf-8")

    def test_compile_writes_workspace_skill_without_execute_commands(self) -> None:
        stdout = StringIO()
        with redirect_stdout(stdout):
            main(["--root", str(self.root), "host", "compile", "--format", "json"])
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["scope"], "workspace")
        self.assertEqual(payload["projections"][0]["authority_projection"], AUTHORITY_SKILL_ONLY)
        self.assertNotIn(str(self.root), stdout.getvalue())
        skill = self._skill()
        self.assertIn("不要用 git merge 结束任务", skill)
        self.assertIn("不要把测试通过写成 done", skill)
        self.assertIn("`dyro next`", skill)
        self.assertIn("| noop |", skill)
        self.assertNotIn("dyro task", skill)
        self.assertNotIn("execute_task", skill)
        self.assertNotIn("/usr/bin", skill)
        self.assertNotIn(str(self.root), skill)
        self.assertFalse((projection_root(load(self.root), user=False) / "cli" / HOOK_NAME).exists())
        self.assertTrue((self.root / ".dyro" / "host-projections" / "cli.toml").is_file())

    def test_no_available_card_has_no_execute_implication(self) -> None:
        _strip_noop_adapter(self.root)
        compile_hosts(load(self.root))
        skill = self._skill()
        self.assertIn("不要执行", skill)
        self.assertNotIn("| noop |", skill)
        self.assertNotIn("dyro task", skill.lower())
        self.assertNotIn("execute_task", skill)
        self.assertNotIn("task run", skill)

    def test_removed_card_disappears_on_recompile(self) -> None:
        _append_toml(
            self.root,
            """

[adapters.extra]
launch = ["/usr/bin/true"]
read = ["/usr/bin/true"]
write = ["/usr/bin/true"]
""",
        )
        compile_hosts(load(self.root))
        self.assertIn("| extra |", self._skill())
        path = self.root / "dyro.toml"
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                """
[adapters.extra]
launch = ["/usr/bin/true"]
read = ["/usr/bin/true"]
write = ["/usr/bin/true"]
""",
                "\n",
            ),
            encoding="utf-8",
        )
        compile_hosts(load(self.root))
        self.assertNotIn("| extra |", self._skill())
        self.assertIn("| noop |", self._skill())

    def test_opencode_fixture_without_hook_compiles_skill_only(self) -> None:
        _append_toml(
            self.root,
            """

[[capabilities]]
id = "opencode-host"
kind = "tool"
hosts = ["opencode"]
intents = ["observe"]
""",
        )
        projections = {item.host: item for item in compile_hosts(load(self.root))}
        self.assertEqual(projections["opencode"].authority_projection, AUTHORITY_SKILL_ONLY)
        self.assertEqual(projections["opencode"].hook_relpath, "")
        root = projection_root(load(self.root), user=False)
        self.assertFalse((root / "opencode" / HOOK_NAME).exists())
        self.assertTrue((root / "opencode" / SKILL_NAME).is_file())
        _strip_host_card(self.root)
        compile_hosts(load(self.root))
        self.assertFalse((root / "opencode.toml").exists())
        self.assertFalse((root / "opencode" / SKILL_NAME).exists())

    def test_fake_hook_surface_does_not_write_hook(self) -> None:
        _append_toml(
            self.root,
            """

[[capabilities]]
id = "opencode-host"
kind = "tool"
hosts = ["opencode"]
intents = ["observe"]
hook_surface = "missing-hooks/opencode"
""",
        )
        compile_hosts(load(self.root))
        root = projection_root(load(self.root), user=False)
        self.assertFalse((root / "opencode" / HOOK_NAME).exists())
        manifest = (root / "opencode.toml").read_text(encoding="utf-8")
        self.assertIn(AUTHORITY_SKILL_ONLY, manifest)

    def test_dot_hook_surface_does_not_write_hook(self) -> None:
        _append_toml(
            self.root,
            """

[[capabilities]]
id = "opencode-host"
kind = "tool"
hosts = ["opencode"]
intents = ["observe"]
hook_surface = "."
""",
        )
        compile_hosts(load(self.root))
        root = projection_root(load(self.root), user=False)
        self.assertFalse((root / "opencode" / HOOK_NAME).exists())
        self.assertIn(AUTHORITY_SKILL_ONLY, (root / "opencode.toml").read_text(encoding="utf-8"))

    def test_toml_hook_surface_does_not_write_hook(self) -> None:
        _append_toml(
            self.root,
            """

[[capabilities]]
id = "opencode-host"
kind = "tool"
hosts = ["opencode"]
intents = ["observe"]
hook_surface = "dyro.toml"
""",
        )
        compile_hosts(load(self.root))
        root = projection_root(load(self.root), user=False)
        self.assertFalse((root / "opencode" / HOOK_NAME).exists())
        self.assertIn(AUTHORITY_SKILL_ONLY, (root / "opencode.toml").read_text(encoding="utf-8"))

    def test_absolute_hook_surface_does_not_write_hook(self) -> None:
        _append_toml(
            self.root,
            """

[[capabilities]]
id = "opencode-host"
kind = "tool"
hosts = ["opencode"]
intents = ["observe"]
hook_surface = "/tmp/hooks"
""",
        )
        compile_hosts(load(self.root))
        root = projection_root(load(self.root), user=False)
        self.assertFalse((root / "opencode" / HOOK_NAME).exists())

    def test_proven_hook_surface_writes_deny_hook_from_intent_lattice(self) -> None:
        (self.root / "hooks" / "surface").mkdir(parents=True)
        _append_toml(
            self.root,
            """

[[capabilities]]
id = "opencode-host"
kind = "tool"
hosts = ["opencode"]
intents = ["observe"]
hook_surface = "hooks/surface"
""",
        )
        projections = {item.host: item for item in compile_hosts(load(self.root))}
        self.assertEqual(projections["opencode"].authority_projection, AUTHORITY_SKILL_AND_HOOK)
        hook_path = projection_root(load(self.root), user=False) / "opencode" / HOOK_NAME
        hook = json.loads(hook_path.read_text(encoding="utf-8"))
        self.assertEqual(hook["denied_intents"], ["integrate", "publish"])
        self.assertEqual(hook["denied_paths"], [".dyro/"])
        self.assertNotIn("sandbox", hook_path.read_text(encoding="utf-8").lower())
        report = inspect_projections(load(self.root))
        self.assertTrue(report.ok)
        hook_path.unlink()
        stale = inspect_projections(load(self.root))
        self.assertFalse(stale.ok)
        self.assertTrue(any(item.code == "MISSING_HOOK" for item in stale.findings))

    def test_card_change_expires_projection_until_recompile(self) -> None:
        compile_hosts(load(self.root))
        self.assertTrue(inspect_projections(load(self.root)).ok)
        _append_toml(
            self.root,
            """

[adapters.extra]
launch = ["/usr/bin/true"]
read = ["/usr/bin/true"]
write = ["/usr/bin/true"]
""",
        )
        expired = inspect_projections(load(self.root))
        self.assertFalse(expired.ok)
        self.assertTrue(any(item.code == "EXPIRED" for item in expired.findings))
        compile_hosts(load(self.root))
        self.assertTrue(inspect_projections(load(self.root)).ok)

    def test_doctor_fails_on_one_byte_tamper_and_compile_repairs(self) -> None:
        compile_hosts(load(self.root))
        skill_path = projection_root(load(self.root), user=False) / "cli" / SKILL_NAME
        skill_path.write_text(skill_path.read_text(encoding="utf-8") + " ", encoding="utf-8")
        stderr = StringIO()
        stdout = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            main(["--root", str(self.root), "host", "doctor", "--format", "json"])
        self.assertEqual(raised.exception.code, 2)
        payload = json.loads(stdout.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["scope"], "workspace")
        self.assertTrue(any(item["code"] == "TAMPERED" for item in payload["findings"]))
        compile_hosts(load(self.root))
        repaired = inspect_projections(load(self.root))
        self.assertTrue(repaired.ok)
        self.assertTrue(repaired.compiled)

    def test_user_scope_writes_registry_home_and_doctor_reports_user(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dyro-home-") as home:
            previous = os.environ.get("DYRO_HOME")
            os.environ["DYRO_HOME"] = home
            try:
                main(["--root", str(self.root), "host", "compile", "--user"])
                expected = Path(home) / "host-projections" / "test-workspace" / "cli" / SKILL_NAME
                self.assertTrue(expected.is_file())
                self.assertFalse((self.root / ".dyro" / "host-projections" / "cli" / SKILL_NAME).exists())
                stdout = StringIO()
                with redirect_stdout(stdout):
                    main(["--root", str(self.root), "host", "doctor", "--user", "--format", "json"])
                payload = json.loads(stdout.getvalue())
                self.assertEqual(payload["scope"], "user")
                self.assertTrue(payload["ok"])
                workspace = inspect_projections(load(self.root), user=False)
                self.assertFalse(workspace.compiled)
            finally:
                if previous is None:
                    os.environ.pop("DYRO_HOME", None)
                else:
                    os.environ["DYRO_HOME"] = previous

    def test_orphan_skill_without_manifest_blocks_apply(self) -> None:
        compile_hosts(load(self.root))
        root = projection_root(load(self.root), user=False)
        (root / "cli.toml").unlink()
        report = inspect_projections(load(self.root))
        self.assertTrue(report.compiled)
        self.assertFalse(report.ok)
        self.assertTrue(any(item.code == "TAMPERED" for item in report.findings))
        with self.assertRaisesRegex(DyroError, "plan-only"):
            assert_projections_allow_mutation(load(self.root))

    def test_never_compiled_does_not_block_apply(self) -> None:
        assert_projections_allow_mutation(load(self.root))
        config = load(self.root)
        create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        self._write_task(config)
        create_objective(config, _objective_contract())
        now = datetime(2026, 8, 15, 4, 0, tzinfo=timezone.utc)
        wave = build_supervised_wave(config, "release", clock=lambda: now)
        with patch("dyro.continuation.supervision.run_task", return_value="review"):
            outcomes = apply_supervised_wave(config, wave, clock=lambda: now)
        self.assertEqual(len(outcomes), 1)

    def test_stale_projection_fail_closes_apply_to_plan_only(self) -> None:
        compile_hosts(load(self.root))
        skill_path = projection_root(load(self.root), user=False) / "cli" / SKILL_NAME
        skill_path.write_text(skill_path.read_text(encoding="utf-8") + "x", encoding="utf-8")
        config = load(self.root)
        create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        self._write_task(config)
        create_objective(config, _objective_contract())
        now = datetime(2026, 8, 15, 4, 0, tzinfo=timezone.utc)
        wave = build_supervised_wave(config, "release", clock=lambda: now)
        with self.assertRaisesRegex(DyroError, "plan-only"):
            apply_supervised_wave(config, wave, clock=lambda: now)

    def test_host_help_does_not_call_hook_a_sandbox(self) -> None:
        stdout = StringIO()
        with redirect_stdout(stdout), self.assertRaises(SystemExit) as raised:
            main(["host", "--help"])
        self.assertEqual(raised.exception.code, 0)
        help_text = stdout.getvalue()
        self.assertIn("不是沙箱", help_text)
        self.assertIn("不是隔离", help_text)

    def _write_task(self, config) -> None:
        directory = config.task_specs_dir / "TASK-A"
        directory.mkdir(parents=True)
        directory.joinpath("task.toml").write_text(
            task_template("TASK-A", "Task A", "alpha", "api", "services/api").replace(
                'agent = "codex"', 'agent = "noop"'
            ),
            encoding="utf-8",
        )
        directory.joinpath("handoff.md").write_text("# handoff\n", encoding="utf-8")


def _strip_host_card(root: Path) -> None:
    path = root / "dyro.toml"
    text = path.read_text(encoding="utf-8")
    marker = "\n[[capabilities]]\nid = \"opencode-host\""
    index = text.find(marker)
    if index < 0:
        raise AssertionError("opencode-host card missing")
    path.write_text(text[:index] + "\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()

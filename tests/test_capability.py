from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch
import json
import os
import stat
import tempfile
import unittest

from dyro.capability import discover_unintegrated, runtime_cards
from dyro.cli import main
from dyro.config import load
from dyro.errors import DyroError, ValidationError
from dyro.process import Result
from dyro.tasks import load_task, run_task, task_template
from dyro.workspace import create_line, doctor

from .support import WorkspaceCase


class CapabilityCardTests(WorkspaceCase):
    def test_adapters_upgrade_to_cards_with_fail_closed_defaults(self) -> None:
        config = load(self.root)
        cards = runtime_cards(config)
        self.assertIn("noop", cards)
        card = cards["noop"]
        self.assertEqual(card.source, "adapter")
        self.assertEqual(card.attested_isolation.value, "cwd")
        self.assertEqual(card.cannot_prove, ("done", "merge"))
        self.assertIn("execute", card.intents)

    def test_capabilities_table_parses_and_synthesizes_adapter(self) -> None:
        path = self.root / "dyro.toml"
        path.write_text(
            path.read_text(encoding="utf-8")
            + """

[[capabilities]]
id = "reviewer"
kind = "agent"
launch = ["/usr/bin/true"]
read = ["/usr/bin/true"]
write = ["/usr/bin/true"]
can_prove = ["review_verdict"]
""",
            encoding="utf-8",
        )
        config = load(self.root)
        self.assertIn("reviewer", config.adapters)
        card = config.capabilities["reviewer"]
        self.assertEqual(card.source, "capabilities")
        self.assertIn("done", card.cannot_prove)
        self.assertIn("merge", card.cannot_prove)
        self.assertEqual(card.can_prove, ("review_verdict",))

    def test_adapter_and_capability_id_conflict_is_fail_closed(self) -> None:
        path = self.root / "dyro.toml"
        path.write_text(
            path.read_text(encoding="utf-8")
            + """

[[capabilities]]
id = "noop"
kind = "agent"
launch = ["/usr/bin/true"]
read = ["/usr/bin/true"]
write = ["/usr/bin/true"]
""",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValidationError, "ID 冲突"):
            load(self.root)

    def test_rejects_env_and_invalid_can_prove(self) -> None:
        path = self.root / "dyro.toml"
        original = path.read_text(encoding="utf-8")
        path.write_text(
            original
            + """

[[capabilities]]
id = "secretive"
kind = "agent"
launch = ["/usr/bin/true"]
read = ["/usr/bin/true"]
write = ["/usr/bin/true"]
env = { API_TOKEN = "nope" }
""",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValidationError, "环境变量"):
            load(self.root)
        path.write_text(
            original
            + """

[[capabilities]]
id = "badprove"
kind = "agent"
launch = ["/usr/bin/true"]
read = ["/usr/bin/true"]
write = ["/usr/bin/true"]
can_prove = ["dispatch"]
""",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValidationError, "Proof kind"):
            load(self.root)

    def test_polyrepo_example_still_doctors_without_toml_changes(self) -> None:
        root = Path(__file__).resolve().parents[1] / "examples" / "polyrepo"
        config = load(root)
        self.assertIn("codex", config.adapters)
        self.assertEqual(config.capabilities["codex"].source, "adapter")
        findings = doctor(config)
        self.assertTrue(findings)
        self.assertTrue(any("workspace root" in item for item in findings))

    def test_capability_cli_add_and_test_noop(self) -> None:
        stdout = StringIO()
        with redirect_stdout(stdout):
            main(["--root", str(self.root), "capability", "add", "local-true", "--preset", "noop"])
        self.assertIn("Capability Card", stdout.getvalue())
        config = load(self.root)
        self.assertEqual(config.capabilities["local-true"].source, "capabilities")
        self.assertIn("local-true", config.adapters)
        code_out = StringIO()
        with redirect_stdout(code_out):
            main(["--root", str(self.root), "capability", "test", "local-true", "--format", "json"])
        report = json.loads(code_out.getvalue())
        self.assertTrue(report["executable"])
        self.assertEqual(report["hook_surface"], "")
        self.assertIn("done", report["cannot_prove"])

    def test_capability_add_conflicts_with_existing_adapter(self) -> None:
        stderr = StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            main(["--root", str(self.root), "capability", "add", "noop", "--preset", "noop"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("已配置", stderr.getvalue())

    def test_discovered_unintegrated_cannot_execute(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_bin = Path(tmp)
            fake = fake_bin / "opencode"
            fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
            previous = os.environ.get("PATH")
            os.environ["PATH"] = env["PATH"]
            try:
                config = load(self.root)
                discovered = discover_unintegrated(config)
                self.assertTrue(any(item.id == "opencode" for item in discovered))
                self.assertNotIn("opencode", runtime_cards(config))
                create_line(config, line_id="alpha", branch="feat/alpha", base="main")
                task_path = config.task_specs_dir / "TASK-OPEN"
                task_path.mkdir(parents=True)
                spec = task_template("TASK-OPEN", "unintegrated", "alpha", "api", "services/api")
                spec = spec.replace('agent = "codex"', 'agent = "opencode"')
                task_path.joinpath("task.toml").write_text(spec, encoding="utf-8")
                task_path.joinpath("handoff.md").write_text("# handoff\n", encoding="utf-8")
                task_path.joinpath("receipt.md").write_text("result: DONE\n", encoding="utf-8")
                with patch(
                    "dyro.task_dispatch.is_dispatch_write_ready",
                    return_value=False,
                ):
                    with self.assertRaisesRegex(ValidationError, "未配置"):
                        run_task(config, load_task(config, "TASK-OPEN"))
            finally:
                if previous is None:
                    os.environ.pop("PATH", None)
                else:
                    os.environ["PATH"] = previous

    def test_observe_only_card_cannot_be_task_executor(self) -> None:
        path = self.root / "dyro.toml"
        path.write_text(
            path.read_text(encoding="utf-8")
            + """

[[capabilities]]
id = "watcher"
kind = "agent"
launch = ["/usr/bin/true"]
read = ["/usr/bin/true"]
write = ["/usr/bin/true"]
intents = ["observe"]
""",
            encoding="utf-8",
        )
        config = load(self.root)
        create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        task_path = config.task_specs_dir / "TASK-WATCH"
        task_path.mkdir(parents=True)
        spec = task_template("TASK-WATCH", "observe only", "alpha", "api", "services/api")
        spec = spec.replace('agent = "codex"', 'agent = "watcher"')
        task_path.joinpath("task.toml").write_text(spec, encoding="utf-8")
        task_path.joinpath("handoff.md").write_text("# handoff\n", encoding="utf-8")
        task_path.joinpath("receipt.md").write_text("result: DONE\n", encoding="utf-8")
        with self.assertRaisesRegex(DyroError, "未授予 execute"):
            run_task(config, load_task(config, "TASK-WATCH"))

    def test_observe_only_dispatch_ready_same_id_cannot_execute(self) -> None:
        path = self.root / "dyro.toml"
        path.write_text(
            path.read_text(encoding="utf-8")
            + """

[[capabilities]]
id = "codex"
kind = "agent"
launch = ["/usr/bin/true"]
read = ["/usr/bin/true"]
write = ["/usr/bin/true"]
intents = ["observe"]
""",
            encoding="utf-8",
        )
        config = load(self.root)
        create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        task_path = config.task_specs_dir / "TASK-CODEX"
        task_path.mkdir(parents=True)
        spec = task_template("TASK-CODEX", "observe only dispatch", "alpha", "api", "services/api")
        task_path.joinpath("task.toml").write_text(spec, encoding="utf-8")
        task_path.joinpath("handoff.md").write_text("# handoff\n", encoding="utf-8")
        task_path.joinpath("receipt.md").write_text("result: DONE\n", encoding="utf-8")
        with (
            patch("dyro.task_dispatch.is_dispatch_write_ready", return_value=True),
            patch("dyro.task_dispatch.run_task_bound_dispatch") as dispatch,
        ):
            with self.assertRaisesRegex(DyroError, "未授予 execute"):
                run_task(config, load_task(config, "TASK-CODEX"))
            dispatch.assert_not_called()

    def test_dispatch_ready_without_card_is_explicit_second_door(self) -> None:
        config = load(self.root)
        self.assertNotIn("codex", getattr(config, "capabilities", {}))
        create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        task_path = config.task_specs_dir / "TASK-DOOR"
        task_path.mkdir(parents=True)
        spec = task_template("TASK-DOOR", "second door", "alpha", "api", "services/api")
        task_path.joinpath("task.toml").write_text(spec, encoding="utf-8")
        task_path.joinpath("handoff.md").write_text("# handoff\n", encoding="utf-8")
        task_path.joinpath("receipt.md").write_text("result: DONE\n", encoding="utf-8")
        result = Result(("dyro", "task-dispatch", "codex", "TASK-DOOR"), 0, "")
        with (
            patch("dyro.task_dispatch.is_dispatch_write_ready", return_value=True),
            patch(
                "dyro.task_dispatch.run_task_bound_dispatch",
                return_value=result,
            ) as dispatch,
        ):
            self.assertEqual(
                run_task(config, load_task(config, "TASK-DOOR"), dry_run=True),
                "dry-run",
            )
            dispatch.assert_called_once()


if __name__ == "__main__":
    unittest.main()

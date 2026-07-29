from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from experiments.local_agent_dispatch.adapters.registry import get_adapter, probe_backends
from experiments.local_agent_dispatch.cli import main as cli_main
from experiments.local_agent_dispatch.errors import DispatchValidationError
from experiments.local_agent_dispatch.gc import gc
from experiments.local_agent_dispatch.lease import SlotManager
from experiments.local_agent_dispatch.panel import run_panel
from experiments.local_agent_dispatch.skill_render import render_skill_markdown, write_skill
from experiments.local_agent_dispatch.supervisor import DispatchSupervisor
from experiments.local_agent_dispatch.task_contract import parse_task_contract


def _home(tmp: str) -> Path:
    return Path(tmp) / "dispatch-home"


def _project(tmp: str) -> Path:
    root = Path(tmp) / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text(
        "def hello():\n    return 1\n",
        encoding="utf-8",
    )
    return root


def _task(backend: str = "echo", *, strict: bool = False) -> dict:
    return {
        "schema_version": 1,
        "backend": backend,
        "mode": "read-only",
        "strict": strict,
        "files": ["src/*.py"],
        "task": {
            "briefing": "tiny project",
            "locations": "src/",
            "objective": "summarize hello()",
            "constraints": "read-only",
            "output_contract": "summary + evidence",
        },
    }


class LeaseTests(unittest.TestCase):
    def test_acquire_and_release(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = SlotManager(_home(tmp), max_per_backend=1, max_global=1)
            leases = mgr.acquire("echo")
            self.assertEqual(len(leases), 2)
            with self.assertRaises(DispatchValidationError):
                mgr.acquire("echo")
            mgr.release_all(leases)
            leases2 = mgr.acquire("echo")
            mgr.release_all(leases2)


class SupervisorEchoTests(unittest.TestCase):
    def test_sync_run_and_strict_shadow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = _home(tmp)
            project = _project(tmp)
            sup = DispatchSupervisor(home=home, max_per_backend=2, max_global=4)
            record = sup.accept(_task(strict=True), project_root=project)
            self.assertEqual(record.status, "accepted")
            done = sup.execute(record.run_id, timeout_seconds=30)
            self.assertEqual(done.status, "completed")
            self.assertIsNotNone(done.result)
            assert done.result is not None
            self.assertIn("echo-adapter", str(done.result.get("summary")))
            self.assertTrue(done.shadow_path)
            self.assertTrue(Path(done.shadow_path).is_dir())
            # host .env must not be required; secret file should fail accept
            (project / ".env").write_text("SECRET=1\n", encoding="utf-8")
            bad = _task()
            bad["files"] = [".env", "src/*.py"]
            with self.assertRaises(DispatchValidationError):
                sup.accept(bad, project_root=project)


class PanelTests(unittest.TestCase):
    def test_panel_echo_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = _home(tmp)
            project = _project(tmp)
            board = run_panel(
                _task(),
                project_root=project,
                members=["echo"],
                home=home,
                timeout_seconds=30,
            )
            self.assertEqual(board["members"], ["echo"])
            self.assertEqual(len(board["runs"]), 1)
            self.assertTrue(Path(str(board["board_path"])).is_file())


class SkillAndGcTests(unittest.TestCase):
    def test_skill_render_and_gc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = _home(tmp)
            text = render_skill_markdown(home=home)
            self.assertIn("Available backends", text)
            self.assertIn("never `**/*`", text)
            path = write_skill(home=home)
            self.assertTrue(path.is_file())

            project = _project(tmp)
            sup = DispatchSupervisor(home=home)
            rec = sup.accept(_task(), project_root=project)
            done = sup.execute(rec.run_id)
            # Force age
            payload_path = home / "runs" / f"{done.run_id}.json"
            data = json.loads(payload_path.read_text(encoding="utf-8"))
            data["updated_at"] = 1.0
            payload_path.write_text(json.dumps(data), encoding="utf-8")
            report = gc(home=home, max_age_seconds=10, dry_run=False)
            self.assertIn(done.run_id, report["removed_runs"])


class CliTests(unittest.TestCase):
    def test_cli_backends_and_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = _home(tmp)
            project = _project(tmp)
            task_path = Path(tmp) / "task.json"
            task_path.write_text(json.dumps(_task()), encoding="utf-8")
            code = cli_main(
                [
                    "--home",
                    str(home),
                    "backends",
                ]
            )
            self.assertEqual(code, 0)
            code = cli_main(
                [
                    "--home",
                    str(home),
                    "run",
                    "--project",
                    str(project),
                    "--file",
                    str(task_path),
                    "--wait",
                    "--backend",
                    "echo",
                ]
            )
            self.assertEqual(code, 0)


class RegistryTests(unittest.TestCase):
    def test_auto_and_echo(self) -> None:
        echo = get_adapter("echo")
        self.assertTrue(echo.available())
        auto = get_adapter("auto")
        self.assertTrue(auto.id)
        rows = probe_backends()
        self.assertTrue(any(r["id"] == "echo" for r in rows))


class ContractRejectTests(unittest.TestCase):
    def test_forbidden_star(self) -> None:
        with self.assertRaises(DispatchValidationError):
            parse_task_contract(
                {
                    "schema_version": 1,
                    "files": ["**/*"],
                    "task": {
                        "briefing": "a",
                        "locations": "b",
                        "objective": "c",
                        "constraints": "d",
                        "output_contract": "e",
                    },
                }
            )


if __name__ == "__main__":
    unittest.main()

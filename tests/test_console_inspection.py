from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import time
import unittest
from unittest.mock import Mock, patch

from dyro.canonical import canonical_json_bytes
from dyro.console import _inspect_worker
from dyro.console.inspection import IsolatedOverviewService
from dyro.console.overview import ConsoleOverviewError
from dyro.hub import WorkspaceRecord, WorkspaceRegistry, add_workspace

from .support import WorkspaceCase


class IsolatedOverviewServiceTests(WorkspaceCase):
    def setUp(self) -> None:
        super().setUp()
        self.home = self.root / "console-state"
        self.environment = patch.dict(os.environ, {"DYRO_HOME": str(self.home)})
        self.environment.start()
        add_workspace(self.root, name="demo", make_default=True)

    def tearDown(self) -> None:
        self.environment.stop()
        super().tearDown()

    def test_exec_worker_returns_overview_and_single_workspace_without_root_disclosure(self) -> None:
        service = IsolatedOverviewService(
            registry_state_home=self.home,
            timeout_seconds=5,
            cursor_secret=b"q" * 32,
        )

        overview = service.page(limit=1)
        workspace = service.workspace("demo")
        inspect = service.inspect_proofs("demo")

        self.assertEqual(overview["data"]["workspaces"][0]["alias"], "demo")
        self.assertEqual(workspace["data"]["workspace"]["alias"], "demo")
        self.assertEqual(overview["data"]["workspaces"][0]["availability"], "available")
        self.assertEqual(workspace["data"]["workspace"]["availability"], "available")
        self.assertEqual(inspect["data"]["proof_inspection"], "inspected")
        self.assertEqual(overview["data"]["workspaces"][0]["proof_inspection"], "not_inspected")
        self.assertEqual(workspace["data"]["workspace"]["proof_inspection"], "not_inspected")
        self.assertEqual(set(workspace["data"]), {"workspace", "lines", "tasks", "objectives"})
        self.assertNotIn("proofs", workspace["data"])
        self.assertTrue(
            all(
                item.get("integration_state") == "not_inspected"
                for item in workspace["data"]["tasks"]
            )
        )
        self.assertNotIn("procedure", repr(inspect))
        self.assertNotIn(str(self.root), repr(overview))
        self.assertNotIn(str(self.root), repr(workspace))
        self.assertNotIn(str(self.root), repr(inspect))
        system = service.system()
        self.assertEqual(system["data"]["tool_inspection"], "not_inspected")
        self.assertEqual(system["data"]["tools"], [])
        self.assertIn(system["data"]["update"]["kind"], {"none", "patch", "minor", "major"})
        self.assertNotIn(str(self.root), repr(system))
        self.assertNotIn("/usr/", repr(system))

    def test_default_workspace_budget_tolerates_process_startup_overhead(self) -> None:
        clock = [0.0]
        record = WorkspaceRecord(name="demo", root=self.root)
        registry = WorkspaceRegistry(default="demo", workspaces=(record,))
        available = _inspect_worker._unavailable_summary("demo", "IGNORED")
        available.update(
            {
                "availability": "available",
                "health": "healthy",
                "freshness": "fresh",
                "recommendation": None,
            }
        )

        class DelayedQueue:
            def get_nowait(self) -> object:
                if clock[0] < 1.0:
                    raise queue.Empty
                return {"summary": available, "warnings": []}

            def get(self, *, timeout: float) -> object:
                del timeout
                return self.get_nowait()

            def close(self) -> None:
                return None

        class DelayedProcess:
            def start(self) -> None:
                return None

            def is_alive(self) -> bool:
                return clock[0] < 1.0

            def terminate(self) -> None:
                return None

            def join(self, *, timeout: float) -> None:
                del timeout

        context = Mock()
        context.Queue.return_value = DelayedQueue()
        context.Process.return_value = DelayedProcess()

        with (
            patch("dyro.console._inspect_worker.get_context", return_value=context),
            patch(
                "dyro.console._inspect_worker.time.monotonic",
                side_effect=lambda: clock[0],
            ),
            patch(
                "dyro.console._inspect_worker.time.sleep",
                side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
            ),
        ):
            summaries, warnings = _inspect_worker._isolated_summaries(registry)

        self.assertEqual(warnings, set())
        self.assertEqual(summaries[0]["availability"], "available")

    def test_temporary_root_is_read_without_registering_it_globally(self) -> None:
        service = IsolatedOverviewService(
            registry_state_home=self.root / "unrelated-state",
            timeout_seconds=5,
            cursor_secret=b"q" * 32,
            target_root=self.root,
        )

        overview = service.page(limit=20)

        self.assertEqual(overview["data"]["default_workspace"], "test-workspace")
        self.assertEqual(overview["data"]["total_workspaces"], 1)
        self.assertEqual(overview["data"]["workspaces"][0]["alias"], "test-workspace")
        self.assertNotIn(str(self.root), repr(overview))

    def test_worker_timeout_kills_its_process_group_and_returns_a_stable_code(self) -> None:
        process = Mock(spec=subprocess.Popen)
        process.pid = 12345
        process.communicate.side_effect = [
            subprocess.TimeoutExpired(["worker"], 0.1),
            (b"", b""),
        ]
        service = IsolatedOverviewService(registry_state_home=Path("/tmp"))

        with (
            patch("dyro.console.inspection.subprocess.Popen", return_value=process),
            patch("dyro.console.inspection.os.killpg") as killpg,
            self.assertRaisesRegex(ConsoleOverviewError, "OVERVIEW_TIMEOUT"),
        ):
            service.page()

        killpg.assert_called_once()

    def test_windows_inspection_fails_closed_without_starting_a_worker(self) -> None:
        service = IsolatedOverviewService(registry_state_home=Path("/tmp"))
        with (
            patch("dyro.console.inspection.os.name", "nt"),
            patch("dyro.console.inspection.subprocess.Popen") as popen,
            self.assertRaisesRegex(ConsoleOverviewError, "OVERVIEW_UNAVAILABLE"),
        ):
            service.page()
        popen.assert_not_called()
        with (
            patch("dyro.console.inspection.os.name", "nt"),
            patch("dyro.console.inspection.subprocess.Popen") as popen,
            self.assertRaisesRegex(ConsoleOverviewError, "OVERVIEW_UNAVAILABLE"),
        ):
            service.inspect_proofs("demo")
        popen.assert_not_called()

    def test_inspect_timeout_kills_its_process_group_and_returns_a_stable_code(self) -> None:
        process = Mock(spec=subprocess.Popen)
        process.pid = 12345
        process.communicate.side_effect = [
            subprocess.TimeoutExpired(["worker"], 0.1),
            (b"", b""),
        ]
        service = IsolatedOverviewService(registry_state_home=Path("/tmp"))

        with (
            patch("dyro.console.inspection.subprocess.Popen", return_value=process),
            patch("dyro.console.inspection.os.killpg") as killpg,
            self.assertRaisesRegex(ConsoleOverviewError, "OVERVIEW_TIMEOUT"),
        ):
            service.inspect_proofs("demo")

        killpg.assert_called_once()

    def test_inspect_timeout_reaps_hung_descendants(self) -> None:
        if os.name == "nt" or not hasattr(os, "killpg"):
            self.skipTest("inspect process-group kill is POSIX-only")
        marker = self.root / "hung-inspect-descendant.pid"
        wrapper = self.root / "hang-inspect-python"
        wrapper.write_text(
            "\n".join(
                (
                    "#!/usr/bin/env python3",
                    "import subprocess",
                    "import sys",
                    "import time",
                    "from pathlib import Path",
                    f"marker = Path({str(marker)!r})",
                    "child = subprocess.Popen(",
                    "    [sys.executable, '-c', 'import time; time.sleep(60)']",
                    ")",
                    "marker.write_text(str(child.pid), encoding='utf-8')",
                    "time.sleep(60)",
                    "",
                )
            ),
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
        service = IsolatedOverviewService(
            registry_state_home=self.home,
            timeout_seconds=0.6,
            cursor_secret=b"q" * 32,
            python_executable=str(wrapper),
        )

        with self.assertRaisesRegex(ConsoleOverviewError, "OVERVIEW_TIMEOUT"):
            service.inspect_proofs("demo")

        deadline = time.monotonic() + 2.0
        pid = 0
        while time.monotonic() < deadline:
            if marker.exists():
                pid = int(marker.read_text(encoding="utf-8"))
                break
            time.sleep(0.05)
        self.assertGreater(pid, 1, "hung inspect descendant did not start")
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                return
            time.sleep(0.05)
        self.fail("hung inspect descendant survived process-group kill")

    def test_invalid_worker_output_fails_closed_without_echoing_it(self) -> None:
        service = IsolatedOverviewService(registry_state_home=Path("/tmp"))
        with self.assertRaisesRegex(ConsoleOverviewError, "OVERVIEW_UNAVAILABLE") as raised:
            service._parse_worker_output(b'{"ok":false,"error":{"code":"/private/secret"}}')
        self.assertNotIn("private", str(raised.exception))
        with self.assertRaisesRegex(ConsoleOverviewError, "OVERVIEW_UNAVAILABLE"):
            service._parse_worker_output(b'{"ok":1,"payload":{}}')

    def test_parent_rejects_a_digest_consistent_but_unwhitelisted_ipc_payload(self) -> None:
        service = IsolatedOverviewService(
            registry_state_home=self.home,
            cursor_secret=b"q" * 32,
        )
        valid = service.page(limit=1)
        for mutation in (
            lambda payload: payload["data"]["workspaces"][0].update(
                {"alias": "../escaped"}
            ),
            lambda payload: payload["freshness"].update({"raw_path": "/private/secret"}),
        ):
            with self.subTest(mutation=mutation):
                payload = deepcopy(valid)
                mutation(payload)
                payload["snapshot_sha256"] = hashlib.sha256(
                    canonical_json_bytes(
                        {
                            "schema_version": 1,
                            "freshness": payload["freshness"],
                            "data": payload["data"],
                        }
                    )
                ).hexdigest()
                raw = json.dumps({"ok": True, "payload": payload}).encode("utf-8")
                with self.assertRaisesRegex(ConsoleOverviewError, "OVERVIEW_UNAVAILABLE"):
                    service._parse_worker_output(raw, expected_operation="overview")

    def test_parent_rejects_inspect_payload_with_procedure_or_paths(self) -> None:
        service = IsolatedOverviewService(
            registry_state_home=self.home,
            timeout_seconds=5,
            cursor_secret=b"q" * 32,
        )
        valid = service.inspect_proofs("demo")
        payload = deepcopy(valid)
        payload["data"]["procedure"] = "git merge-base --is-ancestor"
        payload["snapshot_sha256"] = hashlib.sha256(
            canonical_json_bytes(
                {
                    "schema_version": 1,
                    "freshness": payload["freshness"],
                    "data": payload["data"],
                }
            )
        ).hexdigest()
        raw = json.dumps({"ok": True, "payload": payload}).encode("utf-8")
        with self.assertRaisesRegex(ConsoleOverviewError, "OVERVIEW_UNAVAILABLE"):
            service._parse_worker_output(raw, expected_operation="inspect_proofs")

    def test_parent_rejects_an_inspected_summary_card(self) -> None:
        service = IsolatedOverviewService(
            registry_state_home=self.home,
            timeout_seconds=5,
            cursor_secret=b"q" * 32,
        )
        valid = service.workspace("demo")
        payload = deepcopy(valid)
        payload["data"]["workspace"]["proof_inspection"] = "inspected"
        payload["snapshot_sha256"] = hashlib.sha256(
            canonical_json_bytes(
                {
                    "schema_version": 1,
                    "freshness": payload["freshness"],
                    "data": payload["data"],
                }
            )
        ).hexdigest()
        raw = json.dumps({"ok": True, "payload": payload}).encode("utf-8")
        with self.assertRaisesRegex(ConsoleOverviewError, "OVERVIEW_UNAVAILABLE"):
            service._parse_worker_output(raw, expected_operation="workspace")

    def test_parent_rejects_workspace_inventory_that_leaks_inspect(self) -> None:
        service = IsolatedOverviewService(
            registry_state_home=self.home,
            timeout_seconds=5,
            cursor_secret=b"q" * 32,
        )
        valid = service.workspace("demo")

        integrated = deepcopy(valid)
        integrated["data"]["tasks"] = [
            {
                "id": "TASK-A",
                "title": "Safe task",
                "line": "alpha",
                "status": "done",
                "risk": "write",
                "depends_on": [],
                "blocked_on": [],
                "conflict_group": "",
                "executor": "codex",
                "reviewer": "codex",
                "integration_state": "integrated",
                "external_claim_active": False,
            }
        ]
        decayed = deepcopy(valid)
        decayed["data"]["objectives"] = [
            {
                "id": "release",
                "title": "Safe release",
                "line": "alpha",
                "revision": 1,
                "operator_state": "active",
                "derived_result": "incomplete",
                "requested_mode": "supervised",
                "operations": ["execute"],
                "scope_count": 1,
                "budget": {"max_actions": 2},
                "selected_actions": [],
                "blocked_actions": [],
                "attention": [
                    {
                        "kind": "needs_user",
                        "subject_id": "TASK-A",
                        "reason": "PROOF_DECAYED",
                    }
                ],
                "contract_sha256": "c" * 64,
                "scope_sha256": "d" * 64,
                "event_sha256": "e" * 64,
            }
        ]
        proofs = deepcopy(valid)
        proofs["data"]["proofs"] = []
        missing = deepcopy(valid)
        del missing["data"]["lines"]

        for payload in (integrated, decayed, proofs, missing):
            payload["snapshot_sha256"] = hashlib.sha256(
                canonical_json_bytes(
                    {
                        "schema_version": 1,
                        "freshness": payload["freshness"],
                        "data": payload["data"],
                    }
                )
            ).hexdigest()
            raw = json.dumps({"ok": True, "payload": payload}).encode("utf-8")
            with self.subTest(keys=sorted(payload["data"])):
                with self.assertRaisesRegex(ConsoleOverviewError, "OVERVIEW_UNAVAILABLE"):
                    service._parse_worker_output(raw, expected_operation="workspace")

    def test_parent_rejects_system_tool_probe_leak(self) -> None:
        service = IsolatedOverviewService(
            registry_state_home=self.home,
            timeout_seconds=5,
            cursor_secret=b"q" * 32,
        )
        valid = service.system()
        inspected = deepcopy(valid)
        inspected["data"]["tool_inspection"] = "inspected"
        probed = deepcopy(valid)
        probed["data"]["tools"] = [{"name": "git", "argv": ["git"]}]
        for payload in (inspected, probed):
            payload["snapshot_sha256"] = hashlib.sha256(
                canonical_json_bytes(
                    {
                        "schema_version": 1,
                        "freshness": payload["freshness"],
                        "data": payload["data"],
                    }
                )
            ).hexdigest()
            raw = json.dumps({"ok": True, "payload": payload}).encode("utf-8")
            with self.subTest(keys=sorted(payload["data"])):
                with self.assertRaisesRegex(ConsoleOverviewError, "OVERVIEW_UNAVAILABLE") as error:
                    service._parse_worker_output(raw, expected_operation="system")
                self.assertNotIn("git", str(error.exception))
                self.assertNotIn("argv", str(error.exception))

    def test_isolated_command_allowlist_rejects_task_next(self) -> None:
        self.assertTrue(
            IsolatedOverviewService._safe_command(
                "dyro --workspace demo objective tick release", "demo"
            )
        )
        self.assertTrue(
            IsolatedOverviewService._safe_command("dyro --workspace demo doctor", "demo")
        )
        self.assertFalse(
            IsolatedOverviewService._safe_command(
                "dyro --workspace demo task next", "demo"
            )
        )


if __name__ == "__main__":
    unittest.main()

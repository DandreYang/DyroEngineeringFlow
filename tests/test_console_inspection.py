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
from dyro.console.overview import (
    ConsoleOverviewError,
    WORKSPACE_MISSING_ROOT,
    WORKSPACE_TIMEOUT,
)
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
        self.assertEqual(
            set(workspace["data"]),
            {"workspace", "lines", "tasks", "objectives", "operator_twin"},
        )
        twin = workspace["data"]["operator_twin"]
        self.assertEqual(
            set(twin),
            {"plan", "phases", "running", "latest_ledger", "projected_seq", "overlay_complete"},
        )
        self.assertFalse(twin["latest_ledger"]["present"])
        self.assertEqual(twin["running"], [])
        self.assertTrue(
            all("parent" in item for item in workspace["data"]["lines"])
        )
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
        events = service.events("demo")
        families = service.families("demo")
        self.assertEqual(events["data"]["events"], [])
        self.assertIn("next_cursor", events["data"])
        self.assertIsInstance(families["data"]["families"], list)
        self.assertNotIn(str(self.root), repr(events))
        self.assertNotIn(str(self.root), repr(families))
        system = service.system()
        self.assertEqual(system["data"]["tool_inspection"], "not_inspected")
        self.assertEqual(system["data"]["tools"], [])
        self.assertIn(system["data"]["update"]["kind"], {"none", "patch", "minor", "major"})
        self.assertNotIn(str(self.root), repr(system))
        self.assertNotIn("/usr/", repr(system))

    def test_events_after_cursor_and_one_level_family_survive_a_new_worker(self) -> None:
        from dyro.config import load
        from dyro.workspace import create_line, spawn_line

        config = load(self.root)
        create_line(config, line_id="core", branch="feat/core", base="main")
        spawn_line(config, "core", "pay")
        spawn_line(config, "core_pay", "fix")
        service = IsolatedOverviewService(
            registry_state_home=self.home,
            timeout_seconds=5,
            cursor_secret=b"q" * 32,
        )

        first = service.events("demo")
        events = first["data"]["events"]
        self.assertEqual([item["kind"] for item in events], ["spawn", "spawn"])
        self.assertEqual(events[0]["facts"]["child"], "core_pay")
        cursor = first["data"]["next_cursor"]
        self.assertTrue(cursor)
        resumed = service.events("demo", after=cursor)
        self.assertEqual(resumed["data"]["events"], [])

        workspace = service.workspace("demo")
        parents = {item["id"]: item["parent"] for item in workspace["data"]["lines"]}
        self.assertEqual(parents["core"], "")
        self.assertEqual(parents["core_pay"], "core")
        self.assertEqual(parents["core_pay_fix"], "core_pay")

        core = service.family("demo", "core")
        self.assertEqual(core["data"]["members"], ["core", "core_pay", "operator"])
        self.assertFalse(any(node["id"] == "core_pay_fix" for node in core["data"]["nodes"]))
        pay = service.family("demo", "core_pay")
        self.assertIn("core_pay_fix", pay["data"]["members"])
        self.assertNotIn("core", pay["data"]["members"])

        empty = service.channel("demo", "core")
        self.assertEqual(empty["data"]["family"], "core")
        self.assertEqual(empty["data"]["messages"], [])
        with patch.object(service, "_run_worker", side_effect=AssertionError("worker")):
            posted = service.post_channel(
                "demo",
                "core",
                {"kind": "decision", "body": "先同步 core_pay"},
            )
        self.assertEqual(posted["data"]["id"], "msg_1")
        with self.assertRaises(ConsoleOverviewError) as raised:
            service.post_channel("demo", "core", {"kind": "blocked", "body": "禁止"})
        self.assertEqual(raised.exception.code, "FAMILY_POST_FORBIDDEN")
        reader = IsolatedOverviewService(
            registry_state_home=self.home,
            timeout_seconds=5,
            cursor_secret=b"q" * 32,
        )
        page = reader.channel("demo", "core")
        self.assertEqual(page["data"]["messages"][0]["from"], "operator")
        self.assertEqual(page["data"]["messages"][0]["kind"], "decision")
        events = reader.events("demo")
        self.assertTrue(
            any(
                item["kind"] == "signal" and item["facts"].get("channel_id") == "msg_1"
                for item in events["data"]["events"]
            )
        )

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

    def test_root_scope_hides_global_ghost_registry_rows(self) -> None:
        ghost = Path("/tmp/dyro-test-xyz")
        self.assertFalse(ghost.exists())
        home = self.root / "scoped-state"
        home.mkdir()
        home.joinpath("workspaces.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "default": "ghost",
                    "workspaces": [
                        {
                            "name": "ghost",
                            "root": str(ghost),
                            "last_kind": "",
                            "last_target": "",
                            "last_agent": "",
                        }
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        service = IsolatedOverviewService(
            registry_state_home=home,
            timeout_seconds=5,
            cursor_secret=b"q" * 32,
            target_root=self.root,
        )

        overview = service.page(limit=20)
        aliases = [item["alias"] for item in overview["data"]["workspaces"]]

        self.assertEqual(overview["data"]["total_workspaces"], 1)
        self.assertEqual(aliases, ["test-workspace"])
        self.assertEqual(overview["data"]["workspaces"][0]["availability"], "available")
        self.assertNotIn("ghost", aliases)
        self.assertNotIn("/tmp", repr(overview))

    def test_vanished_test_workspace_does_not_win_unscoped_overview(self) -> None:
        ghost = Path("/tmp/dyro-test-xyz")
        self.assertFalse(ghost.exists())
        self.home.joinpath("workspaces.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "default": "core",
                    "workspaces": [
                        {
                            "name": "core",
                            "root": str(self.root),
                            "last_kind": "",
                            "last_target": "",
                            "last_agent": "",
                        },
                        {
                            "name": "test-workspace",
                            "root": str(ghost),
                            "last_kind": "",
                            "last_target": "",
                            "last_agent": "",
                        },
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        service = IsolatedOverviewService(
            registry_state_home=self.home,
            timeout_seconds=5,
            cursor_secret=b"q" * 32,
        )

        overview = service.page(limit=20)
        cards = overview["data"]["workspaces"]
        aliases = [item["alias"] for item in cards]

        self.assertEqual(aliases[0], "core")
        self.assertNotEqual(aliases[0], "test-workspace")
        ghost_card = next(item for item in cards if item["alias"] == "test-workspace")
        self.assertEqual(ghost_card["availability"], "unavailable")
        self.assertEqual(ghost_card["unavailable_reason"], "missing_root")
        self.assertEqual(ghost_card["recommendation"]["reason"], WORKSPACE_MISSING_ROOT)
        highest = overview["data"]["highest_priority"]
        if highest is not None:
            self.assertNotEqual(highest["alias"], "test-workspace")
        self.assertNotEqual(
            cards[0]["recommendation"]["command"],
            "dyro --workspace test-workspace doctor",
        )
        self.assertNotIn("/tmp", repr(overview))
        self.assertNotIn("dyro-test-xyz", repr(overview))

    def test_timeout_card_is_not_a_missing_root(self) -> None:
        timeout = _inspect_worker._unavailable_summary("core", WORKSPACE_TIMEOUT)
        missing = _inspect_worker._unavailable_summary(
            "test-workspace", WORKSPACE_MISSING_ROOT
        )

        IsolatedOverviewService._validate_summary(timeout)
        IsolatedOverviewService._validate_summary(missing)
        self.assertEqual(timeout["unavailable_reason"], "read_timeout")
        self.assertEqual(missing["unavailable_reason"], "missing_root")
        self.assertEqual(timeout["recommendation"]["reason"], WORKSPACE_TIMEOUT)
        self.assertEqual(missing["recommendation"]["reason"], WORKSPACE_MISSING_ROOT)
        self.assertNotEqual(
            timeout["recommendation"]["reason"],
            missing["recommendation"]["reason"],
        )

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

    def test_parent_rejects_twin_plan_or_running_unbound_from_inventory(self) -> None:
        service = IsolatedOverviewService(
            registry_state_home=self.home,
            timeout_seconds=5,
            cursor_secret=b"q" * 32,
        )
        valid = service.workspace("demo")
        digest = "a" * 64
        objective = {
            "id": "release",
            "title": "Release readiness",
            "line": "core",
            "revision": 1,
            "operator_state": "active",
            "derived_result": "incomplete",
            "requested_mode": "supervised",
            "operations": ["execute"],
            "scope_count": 1,
            "budget": {"max_actions": 1},
            "selected_actions": [],
            "blocked_actions": [],
            "attention": [],
            "contract_sha256": digest,
            "scope_sha256": digest,
            "event_sha256": digest,
        }
        ghost_plan = {
            "id": "ghost",
            "title": "Ghost",
            "line": "core",
            "milestone": "incomplete",
            "wave_present": False,
            "wave_id": "",
            "wave_at": "",
            "wave_mode": "",
            "wave_count": 0,
            "task_ids": [],
        }
        running = {
            "id": "TASK-A",
            "title": "Pay path",
            "line": "core",
            "executor": "noop",
            "dispatch_present": False,
            "dispatch_id": "",
            "dispatch_at": "",
            "dispatch_state": "unknown",
            "dispatch_facts": {},
            "board_landed": True,
        }

        def resign(payload: dict[str, object]) -> bytes:
            payload["snapshot_sha256"] = hashlib.sha256(
                canonical_json_bytes(
                    {
                        "schema_version": 1,
                        "freshness": payload["freshness"],
                        "data": payload["data"],
                    }
                )
            ).hexdigest()
            return json.dumps({"ok": True, "payload": payload}).encode("utf-8")

        ghost = deepcopy(valid)
        ghost["data"]["objectives"] = [objective]
        ghost["data"]["operator_twin"]["plan"] = [ghost_plan]
        with self.assertRaisesRegex(ConsoleOverviewError, "OVERVIEW_UNAVAILABLE"):
            service._parse_worker_output(resign(ghost), expected_operation="workspace")

        unbound_running = deepcopy(valid)
        unbound_running["data"]["operator_twin"]["running"] = [running]
        in_progress = next(
            column
            for column in unbound_running["data"]["operator_twin"]["phases"]
            if column["status"] == "in_progress"
        )
        in_progress["tasks"] = []
        with self.assertRaisesRegex(ConsoleOverviewError, "OVERVIEW_UNAVAILABLE"):
            service._parse_worker_output(
                resign(unbound_running), expected_operation="workspace"
            )

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

    def test_parent_rejects_digest_consistent_events_with_path_in_facts(self) -> None:
        service = IsolatedOverviewService(
            registry_state_home=self.home,
            timeout_seconds=5,
            cursor_secret=b"q" * 32,
        )
        valid = service.events("demo")
        payload = deepcopy(valid)
        payload["data"]["events"] = [
            {
                "seq": 1,
                "id": "evt_1",
                "kind": "spawn",
                "at": "2026-08-20T12:00:00Z",
                "actor": "core",
                "subject": "core_pay",
                "family": "core",
                "facts": {"parent": "core", "path": "/tmp/secret"},
            }
        ]
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
            service._parse_worker_output(raw, expected_operation="events")

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
        self.assertFalse(
            IsolatedOverviewService._safe_command("dyro --workspace demo", "demo")
        )

    def test_missing_origin_fail_is_not_ready_or_a_bare_workspace_command(self) -> None:
        from dyro.config import load
        from dyro.workspace import create_line, spawn_line

        config = load(self.root)
        create_line(config, line_id="core", branch="feat/core", base="main")
        spawn_line(config, "core", "pay")
        create_line(
            config,
            line_id="release_a",
            branch="hotfix/release_a",
            base="main",
            kind="hotfix",
        )
        service = IsolatedOverviewService(
            registry_state_home=self.home,
            timeout_seconds=5,
            cursor_secret=b"q" * 32,
        )

        overview = service.page(limit=1)
        card = overview["data"]["workspaces"][0]
        reasons = {(item["reason"], item["line"]) for item in card["findings"]}

        self.assertIn(("MISSING_ORIGIN", "core"), reasons)
        self.assertIn(("MISSING_ORIGIN", "core_pay"), reasons)
        self.assertIn(("MISSING_ORIGIN", "release_a"), reasons)
        self.assertEqual(card["recommendation"]["command"], "dyro --workspace demo doctor")
        self.assertNotEqual(card["recommendation"]["command"], "dyro --workspace demo")
        self.assertEqual(card["health"], "degraded")
        self.assertNotEqual(card["recommendation"]["reason"], "HOME_GUIDANCE")
        self.assertNotIn(str(self.root), repr(overview))

    def test_worker_cannot_serve_or_write_artifacts_via_a_mutation_op(self) -> None:
        from dyro.config import load
        from dyro.families import plant_family_artifact
        from dyro.workspace import create_line

        config = load(self.root)
        create_line(config, line_id="core", branch="feat/core", base="main")
        plant_family_artifact(
            config,
            "core",
            artifact_id="img_1",
            artifact_type="image",
            title="复核图",
            body=(
                b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
                b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
                b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
            ),
        )
        service = IsolatedOverviewService(
            registry_state_home=self.home,
            timeout_seconds=5,
            cursor_secret=b"q" * 32,
        )
        with patch.object(service, "_run_worker", side_effect=AssertionError("worker")):
            listed = service.artifacts("demo", "core")
        self.assertEqual(listed["data"]["artifacts"][0]["id"], "img_1")
        with patch.object(service, "_run_worker", side_effect=AssertionError("worker")):
            media = service.artifact_bytes("demo", "core", "img_1")
        self.assertIsNotNone(media)
        self.assertEqual(media[0], "image/png")
        for operation in ("post_channel", "artifact_write", "artifacts", "put_artifact"):
            with self.subTest(operation=operation):
                with self.assertRaises(ConsoleOverviewError) as raised:
                    service._request(
                        {"op": operation, "alias": "demo", "parent": "core"}
                    )
                self.assertEqual(raised.exception.code, "OVERVIEW_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()

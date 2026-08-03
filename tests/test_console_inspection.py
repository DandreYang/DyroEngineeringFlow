from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import Mock, patch

from dyro.canonical import canonical_json_bytes
from dyro.console.inspection import IsolatedOverviewService
from dyro.console.overview import ConsoleOverviewError
from dyro.hub import add_workspace

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

        self.assertEqual(overview["data"]["workspaces"][0]["alias"], "demo")
        self.assertEqual(workspace["data"]["workspace"]["alias"], "demo")
        self.assertNotIn(str(self.root), repr(overview))
        self.assertNotIn(str(self.root), repr(workspace))

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


if __name__ == "__main__":
    unittest.main()

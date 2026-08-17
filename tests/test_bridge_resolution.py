from __future__ import annotations

import os
from pathlib import Path
import tempfile
from unittest.mock import patch

from dyro.bridge.identity import workspace_identity_v1
from dyro.bridge.observations import (
    BridgeObservationError,
    list_workspaces_observation,
    resolve_workspace_observation,
)
from dyro.hub import add_workspace

from .support import CONFIG, WorkspaceCase


class BridgeResolutionTests(WorkspaceCase):
    def setUp(self) -> None:
        super().setUp()
        self.home = self.root / "dyro-home"
        self.home.mkdir()
        self.env = patch.dict(os.environ, {"DYRO_HOME": str(self.home)}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_local_resolution_returns_identity_without_paths(self) -> None:
        payload = resolve_workspace_observation(
            start=self.root, workspace=None, cwd=self.root
        )
        expected = workspace_identity_v1(
            canonical_root=self.root.resolve(), profile_name="test-workspace"
        )
        self.assertEqual(payload["workspace"]["id"], expected)
        self.assertEqual(payload["workspace"]["name"], "test-workspace")
        self.assertEqual(payload["resolution_source"], "local")
        self.assertNotIn(str(self.root.resolve()), str(payload))

    def test_malformed_local_profile_never_falls_back_to_registry_default(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dyro-bridge-fallback-") as tmp:
            fallback = Path(tmp) / "fallback"
            fallback.mkdir()
            (fallback / "dyro.toml").write_text(CONFIG, encoding="utf-8")
            add_workspace(fallback, name="fallback", make_default=True)
            (self.root / "dyro.toml").write_text("not valid = [", encoding="utf-8")
            with self.assertRaises(BridgeObservationError) as ctx:
                resolve_workspace_observation(
                    start=self.root, workspace=None, cwd=self.root
                )
            self.assertEqual(ctx.exception.code, "LOCAL_PROFILE_INVALID")
            explicit = resolve_workspace_observation(
                start=self.root, workspace="fallback", cwd=self.root
            )
            self.assertEqual(explicit["resolution_source"], "explicit")
            self.assertEqual(explicit["workspace"]["name"], "test-workspace")

    def test_list_marks_stale_registered_root_without_paths(self) -> None:
        add_workspace(self.root, name="sample", make_default=True)
        stale = self.root / "stale-ws"
        stale.mkdir()
        (stale / "dyro.toml").write_text(
            CONFIG.replace('name = "test-workspace"', 'name = "stale-ws"'),
            encoding="utf-8",
        )
        add_workspace(stale, name="stale")
        (stale / "dyro.toml").unlink()
        payload = list_workspaces_observation()
        by_alias = {item["alias"]: item for item in payload["workspaces"]}
        self.assertEqual(by_alias["sample"]["status"], "ok")
        self.assertEqual(by_alias["stale"]["status"], "stale")
        self.assertTrue(payload["partial"])
        self.assertNotIn(str(self.root.resolve()), str(payload))
        self.assertNotIn(str(stale.resolve()), str(payload))

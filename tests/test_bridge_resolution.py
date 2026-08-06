from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dyro.bridge.models import ErrorCode
from dyro.bridge.observations import (
    BridgeObservationError,
    list_workspace_observations,
    resolve_workspace_observation,
)
from dyro.config import LoadedProfile, load_profile_exact
from dyro.continuation.resolution import (
    WorkspaceResolutionError,
    WorkspaceResolutionFailure,
    WorkspaceResolutionSource,
    resolve_workspace_readonly,
)
from dyro.read_limits import ObservationLimits, ReadBudget

from .support import CONFIG


class BridgeResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="dyro-bridge-resolution-")
        self.base = Path(self.tmp.name)
        self.home = self.base / "dyro-home"
        self.home.mkdir()
        self.root = self._workspace("workspace")
        self.unrelated = self.base / "unrelated"
        self.unrelated.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _workspace(self, name: str) -> Path:
        root = self.base / name
        root.mkdir()
        root.joinpath("dyro.toml").write_text(
            CONFIG.replace('name = "test-workspace"', f'name = "{name}"'),
            encoding="utf-8",
        )
        return root.resolve()

    def _registry(
        self,
        records: list[tuple[str, Path]],
        *,
        default: str = "",
    ) -> Path:
        path = self.home / "workspaces.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "default": default,
                    "workspaces": [
                        {
                            "name": name,
                            "root": str(root),
                            "last_kind": "",
                            "last_target": "",
                            "last_agent": "",
                        }
                        for name, root in records
                    ],
                }
            ),
            encoding="utf-8",
        )
        return path

    @staticmethod
    def _budget() -> ReadBudget:
        return ReadBudget(ObservationLimits())

    def test_exact_profile_loader_returns_the_same_bounded_bytes_used_for_parsing(
        self,
    ) -> None:
        profile = load_profile_exact(self.root, self._budget())
        self.assertIsInstance(profile, LoadedProfile)
        self.assertEqual(profile.root, self.root)
        self.assertEqual(
            profile.profile_bytes, self.root.joinpath("dyro.toml").read_bytes()
        )
        self.assertEqual(profile.config.name, "workspace")

    def test_exact_profile_loader_preserves_permission_error(self) -> None:
        with patch.object(Path, "resolve", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                load_profile_exact(self.root, self._budget())

    def test_resolution_preserves_explicit_local_default_and_unique_sources(
        self,
    ) -> None:
        second = self._workspace("second")
        with patch.dict(os.environ, {"DYRO_HOME": str(self.home)}, clear=False):
            self._registry([("primary", self.root)], default="primary")
            explicit = resolve_workspace_readonly(
                workspace="primary",
                start=None,
                cwd=self.unrelated,
                budget=self._budget(),
            )
            self.assertEqual(explicit.source, WorkspaceResolutionSource.EXPLICIT)
            local = resolve_workspace_readonly(
                workspace=None,
                start=self.root,
                cwd=self.unrelated,
                budget=self._budget(),
            )
            self.assertEqual(local.source, WorkspaceResolutionSource.LOCAL)
            default = resolve_workspace_readonly(
                workspace=None,
                start=self.unrelated,
                cwd=self.unrelated,
                budget=self._budget(),
            )
            self.assertEqual(default.source, WorkspaceResolutionSource.DEFAULT)

            self._registry([("second", second)])
            unique = resolve_workspace_readonly(
                workspace=None,
                start=self.unrelated,
                cwd=self.unrelated,
                budget=self._budget(),
            )
            self.assertEqual(unique.source, WorkspaceResolutionSource.UNIQUE)

    def test_registered_child_cannot_borrow_a_parent_profile(self) -> None:
        stale_child = self.root / "removed-child"
        with patch.dict(os.environ, {"DYRO_HOME": str(self.home)}, clear=False):
            self._registry([("stale", stale_child)], default="stale")
            with self.assertRaises(BridgeObservationError) as raised:
                resolve_workspace_observation(
                    workspace="stale", start=None, cwd=self.unrelated
                )
        self.assertEqual(raised.exception.error.code, ErrorCode.REGISTERED_ROOT_STALE)

    def test_malformed_local_profile_never_falls_back_to_default(self) -> None:
        fallback = self._workspace("fallback")
        self.root.joinpath("dyro.toml").write_text("not valid = [", encoding="utf-8")
        with patch.dict(os.environ, {"DYRO_HOME": str(self.home)}, clear=False):
            self._registry([("fallback", fallback)], default="fallback")
            with self.assertRaises(BridgeObservationError) as raised:
                resolve_workspace_observation(
                    workspace=None, start=self.root, cwd=self.unrelated
                )
        self.assertEqual(raised.exception.error.code, ErrorCode.LOCAL_PROFILE_INVALID)

    def test_invalid_profile_name_is_a_typed_resolution_failure(self) -> None:
        self.root.joinpath("dyro.toml").write_text(
            CONFIG.replace('name = "test-workspace"', 'name = "invalid name"'),
            encoding="utf-8",
        )
        with patch.dict(os.environ, {"DYRO_HOME": str(self.home)}, clear=False):
            self._registry([("primary", self.root)], default="primary")
            with self.assertRaises(BridgeObservationError) as local:
                resolve_workspace_observation(
                    workspace=None, start=self.root, cwd=self.unrelated
                )
            with self.assertRaises(BridgeObservationError) as registered:
                resolve_workspace_observation(
                    workspace="primary", start=None, cwd=self.unrelated
                )
        self.assertEqual(local.exception.error.code, ErrorCode.LOCAL_PROFILE_INVALID)
        self.assertEqual(
            registered.exception.error.code, ErrorCode.REGISTERED_ROOT_STALE
        )

    def test_wrong_profile_table_type_is_a_typed_local_failure(self) -> None:
        self.root.joinpath("dyro.toml").write_text(
            'schema_version = 1\nworkspace = "wrong-type"\n',
            encoding="utf-8",
        )
        with patch.dict(os.environ, {"DYRO_HOME": str(self.home)}, clear=False):
            with self.assertRaises(BridgeObservationError) as raised:
                resolve_workspace_observation(
                    workspace=None, start=self.root, cwd=self.unrelated
                )
        self.assertEqual(raised.exception.error.code, ErrorCode.LOCAL_PROFILE_INVALID)

    def test_wrong_profile_verify_type_is_a_typed_resolution_failure(self) -> None:
        malformed = CONFIG.replace(
            'verify = [["git", "diff", "--check"]]',
            "verify = 7",
        )
        self.root.joinpath("dyro.toml").write_text(malformed, encoding="utf-8")
        with patch.dict(os.environ, {"DYRO_HOME": str(self.home)}, clear=False):
            self._registry([("primary", self.root)], default="primary")
            with self.assertRaises(BridgeObservationError) as local:
                resolve_workspace_observation(
                    workspace=None, start=self.root, cwd=self.unrelated
                )
            with self.assertRaises(BridgeObservationError) as registered:
                resolve_workspace_observation(
                    workspace="primary", start=None, cwd=self.unrelated
                )
        self.assertEqual(local.exception.error.code, ErrorCode.LOCAL_PROFILE_INVALID)
        self.assertEqual(
            registered.exception.error.code, ErrorCode.REGISTERED_ROOT_STALE
        )

    def test_wrong_profile_layout_scalar_type_is_rejected(self) -> None:
        malformed = CONFIG.replace(
            'anchors = "repositories"',
            'anchors = ["repositories"]',
        )
        self.root.joinpath("dyro.toml").write_text(malformed, encoding="utf-8")
        with patch.dict(os.environ, {"DYRO_HOME": str(self.home)}, clear=False):
            with self.assertRaises(BridgeObservationError) as raised:
                resolve_workspace_observation(
                    workspace=None, start=self.root, cwd=self.unrelated
                )
        self.assertEqual(raised.exception.error.code, ErrorCode.LOCAL_PROFILE_INVALID)

    def test_starting_from_a_workspace_file_resolves_its_parent_profile(self) -> None:
        source = self.root / "README.md"
        source.write_text("workspace file\n", encoding="utf-8")
        with patch.dict(os.environ, {"DYRO_HOME": str(self.home)}, clear=False):
            result = resolve_workspace_observation(
                workspace=None, start=source, cwd=self.unrelated
            )
        self.assertEqual(result.resolution_source, "local")

    def test_tilde_input_is_rejected_without_home_expansion(self) -> None:
        with patch.dict(os.environ, {"DYRO_HOME": str(self.home)}, clear=False):
            with self.assertRaises(BridgeObservationError) as raised:
                resolve_workspace_observation(
                    workspace=None, start="~/workspace", cwd=self.base
                )
        self.assertEqual(
            raised.exception.error.code, ErrorCode.SCHEMA_VALIDATION_FAILED
        )

    def test_resolution_is_noninteractive_and_does_not_update_registry_state(
        self,
    ) -> None:
        with patch.dict(os.environ, {"DYRO_HOME": str(self.home)}, clear=False):
            registry = self._registry([("primary", self.root)], default="primary")
            before = registry.read_bytes(), registry.stat().st_mtime_ns
            with patch(
                "dyro.hub._update_registry", side_effect=AssertionError("write")
            ):
                result = resolve_workspace_observation(
                    workspace=None, start=self.unrelated, cwd=self.unrelated
                )
            after = registry.read_bytes(), registry.stat().st_mtime_ns
        self.assertEqual(before, after)
        self.assertEqual(result.resolution_source, "default")

    def test_multiple_usable_registered_workspaces_fail_with_typed_ambiguity(
        self,
    ) -> None:
        second = self._workspace("second")
        with patch.dict(os.environ, {"DYRO_HOME": str(self.home)}, clear=False):
            self._registry([("primary", self.root), ("second", second)])
            with self.assertRaises(WorkspaceResolutionError) as raised:
                resolve_workspace_readonly(
                    workspace=None,
                    start=self.unrelated,
                    cwd=self.unrelated,
                    budget=self._budget(),
                )
        self.assertEqual(
            raised.exception.code, WorkspaceResolutionFailure.AMBIGUOUS_WORKSPACE
        )

    def test_permission_error_is_not_downgraded_to_stale_root(self) -> None:
        with patch.dict(os.environ, {"DYRO_HOME": str(self.home)}, clear=False):
            self._registry([("primary", self.root)], default="primary")
            with patch(
                "dyro.continuation.resolution.load_profile_exact",
                side_effect=PermissionError("denied"),
            ):
                with self.assertRaises(BridgeObservationError) as raised:
                    resolve_workspace_observation(
                        workspace="primary", start=None, cwd=self.unrelated
                    )
        self.assertEqual(
            raised.exception.error.code,
            ErrorCode.HOST_READ_PERMISSION_REQUIRED,
        )

    def test_registry_permission_error_is_not_reported_as_invalid_content(self) -> None:
        with patch.dict(os.environ, {"DYRO_HOME": str(self.home)}, clear=False):
            self._registry([("primary", self.root)], default="primary")
            with patch(
                "dyro.hub.Path.resolve",
                side_effect=PermissionError("denied"),
            ):
                with self.assertRaises(BridgeObservationError) as raised:
                    resolve_workspace_observation(
                        workspace="primary", start=None, cwd=self.unrelated
                    )
        self.assertEqual(
            raised.exception.error.code,
            ErrorCode.HOST_READ_PERMISSION_REQUIRED,
        )

    def test_final_deadline_gates_are_mapped_to_bridge_errors(self) -> None:
        elapsed = 0.0

        def monotonic() -> float:
            return elapsed

        with patch.dict(os.environ, {"DYRO_HOME": str(self.home)}, clear=False):
            self._registry([("primary", self.root)], default="primary")
            from dyro.bridge import observations as observations_module

            original_workspace_ref = observations_module._workspace_ref

            def workspace_ref_then_expire(*args, **kwargs):
                nonlocal elapsed
                result = original_workspace_ref(*args, **kwargs)
                elapsed = 10.0
                return result

            with patch(
                "dyro.bridge.observations._workspace_ref",
                side_effect=workspace_ref_then_expire,
            ):
                with self.assertRaises(BridgeObservationError) as resolve_error:
                    resolve_workspace_observation(
                        workspace="primary",
                        start=None,
                        cwd=self.unrelated,
                        limits=ObservationLimits(deadline_seconds=1.0),
                        monotonic=monotonic,
                    )
        self.assertEqual(
            resolve_error.exception.error.code,
            ErrorCode.OBSERVATION_DEADLINE_EXCEEDED,
        )

        elapsed = 0.0
        with patch.dict(os.environ, {"DYRO_HOME": str(self.home)}, clear=False):
            with patch(
                "dyro.bridge.observations._workspace_ref",
                side_effect=workspace_ref_then_expire,
            ):
                with self.assertRaises(BridgeObservationError) as list_error:
                    list_workspace_observations(
                        limits=ObservationLimits(deadline_seconds=1.0),
                        monotonic=monotonic,
                    )
        self.assertEqual(
            list_error.exception.error.code,
            ErrorCode.OBSERVATION_DEADLINE_EXCEEDED,
        )

    def test_root_replacement_after_resolution_is_a_typed_local_failure(self) -> None:
        from dyro.bridge import observations as observations_module

        real_resolve = observations_module.resolve_workspace_readonly
        displaced = self.root.with_name(self.root.name + "-resolved")
        replaced = False

        def resolve_then_replace(*args, **kwargs):
            nonlocal replaced
            resolved = real_resolve(*args, **kwargs)
            self.root.rename(displaced)
            self.root.symlink_to(displaced, target_is_directory=True)
            replaced = True
            return resolved

        with (
            patch.dict(os.environ, {"DYRO_HOME": str(self.home)}, clear=False),
            patch(
                "dyro.bridge.observations.resolve_workspace_readonly",
                side_effect=resolve_then_replace,
            ),
        ):
            with self.assertRaises(BridgeObservationError) as raised:
                resolve_workspace_observation(
                    workspace=None,
                    start=self.root,
                    cwd=self.unrelated,
                )
        self.assertTrue(replaced)
        self.assertEqual(raised.exception.error.code, ErrorCode.LOCAL_PROFILE_INVALID)

    def test_registry_and_selection_failures_have_stable_codes(self) -> None:
        with patch.dict(os.environ, {"DYRO_HOME": str(self.home)}, clear=False):
            self.home.joinpath("workspaces.json").write_text(
                "not-json", encoding="utf-8"
            )
            with self.assertRaises(BridgeObservationError) as corrupt:
                resolve_workspace_observation(
                    workspace=None, start=self.unrelated, cwd=self.unrelated
                )
            self.assertEqual(corrupt.exception.error.code, ErrorCode.REGISTRY_INVALID)

            self._registry([])
            with self.assertRaises(BridgeObservationError) as missing:
                resolve_workspace_observation(
                    workspace=None, start=self.unrelated, cwd=self.unrelated
                )
            self.assertEqual(
                missing.exception.error.code, ErrorCode.WORKSPACE_NOT_FOUND
            )

            with self.assertRaises(BridgeObservationError) as unknown:
                resolve_workspace_observation(
                    workspace="unknown", start=None, cwd=self.unrelated
                )
            self.assertEqual(
                unknown.exception.error.code, ErrorCode.WORKSPACE_NOT_REGISTERED
            )

    def test_registry_symlink_is_invalid_not_a_resource_limit(self) -> None:
        target = self.home / "registry-target.json"
        target.write_text("{}", encoding="utf-8")
        self.home.joinpath("workspaces.json").symlink_to(target)
        with patch.dict(os.environ, {"DYRO_HOME": str(self.home)}, clear=False):
            with self.assertRaises(BridgeObservationError) as raised:
                resolve_workspace_observation(
                    workspace=None, start=self.unrelated, cwd=self.unrelated
                )
        self.assertEqual(raised.exception.error.code, ErrorCode.REGISTRY_INVALID)

    def test_deep_registry_json_is_redacted_as_registry_invalid(self) -> None:
        self.home.joinpath("workspaces.json").write_text(
            "[" * 2000 + "0" + "]" * 2000,
            encoding="utf-8",
        )
        with patch.dict(os.environ, {"DYRO_HOME": str(self.home)}, clear=False):
            with self.assertRaises(BridgeObservationError) as raised:
                resolve_workspace_observation(
                    workspace=None, start=self.unrelated, cwd=self.unrelated
                )
        self.assertEqual(raised.exception.error.code, ErrorCode.REGISTRY_INVALID)


if __name__ == "__main__":
    unittest.main()

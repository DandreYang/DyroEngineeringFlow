from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from dyro.cli import main
from dyro.errors import DyroError
from dyro.integrations import (
    IntegrationState,
    install_integration,
    integration_status,
    uninstall_integration,
)
from dyro.integrations import manager


class IntegrationManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="dyro-integrations-")
        self.root = Path(self.tmp.name)
        self.codex_home = self.root / "codex"
        self.dyro_home = self.root / "dyro"
        self.environment = patch.dict(
            os.environ,
            {
                "CODEX_HOME": str(self.codex_home),
                "DYRO_HOME": str(self.dyro_home),
                "DYRO_NO_UPDATE_CHECK": "1",
            },
            clear=False,
        )
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.tmp.cleanup()

    @property
    def target(self) -> Path:
        return self.codex_home / "skills" / "dyro-control-plane"

    @property
    def manifest(self) -> Path:
        return self.dyro_home / "integrations" / "codex.json"

    def _tree_snapshot(self) -> tuple[tuple[str, str, int], ...]:
        rows: list[tuple[str, str, int]] = []
        for path in sorted(self.root.rglob("*")):
            relative = path.relative_to(self.root).as_posix()
            kind = (
                "symlink" if path.is_symlink() else "dir" if path.is_dir() else "file"
            )
            size = path.lstat().st_size
            rows.append((relative, kind, size))
        return tuple(rows)

    def test_packaged_skill_is_concise_and_has_required_metadata(self) -> None:
        skill = manager._asset_root() / "SKILL.md"
        metadata = manager._asset_root() / "agents" / "openai.yaml"

        content = skill.read_text(encoding="utf-8")
        self.assertLessEqual(len(content.encode("utf-8")), 8 * 1024)
        self.assertNotIn("TODO", content)
        frontmatter = content.split("---", 2)[1]
        keys = {
            line.split(":", 1)[0] for line in frontmatter.splitlines() if line.strip()
        }
        self.assertEqual(keys, {"name", "description"})
        self.assertIn("name: dyro-control-plane", frontmatter)
        self.assertIn("$dyro-control-plane", metadata.read_text(encoding="utf-8"))
        for line in metadata.read_text(encoding="utf-8").splitlines():
            if ": " in line:
                self.assertTrue(line.split(": ", 1)[1].startswith('"'))

    def test_status_and_dry_run_are_strictly_zero_write(self) -> None:
        before = self._tree_snapshot()
        status = integration_status("codex")
        plan = install_integration("codex", yes=False, dry_run=True)
        uninstall_plan = uninstall_integration("codex", yes=False, dry_run=True)

        self.assertEqual(status.state, IntegrationState.ABSENT)
        self.assertEqual(plan.status.state, IntegrationState.ABSENT)
        self.assertEqual(uninstall_plan.status.state, IntegrationState.ABSENT)
        self.assertEqual(self._tree_snapshot(), before)

    def test_install_is_owned_idempotent_and_uninstall_preserves_parent(self) -> None:
        with self.assertRaisesRegex(DyroError, "--yes"):
            install_integration("codex", yes=False)

        installed = install_integration("codex", yes=True)
        self.assertEqual(installed.status.state, IntegrationState.CURRENT)
        self.assertTrue(self.target.joinpath("SKILL.md").is_file())
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertEqual(manifest["target"], str(self.target))
        self.assertEqual(set(manifest["files"]), {"SKILL.md", "agents/openai.yaml"})

        before = self._tree_snapshot()
        again = install_integration("codex", yes=True)
        self.assertEqual(again.status.state, IntegrationState.CURRENT)
        self.assertEqual(self._tree_snapshot(), before)

        sibling = self.codex_home / "skills" / "user-skill.txt"
        sibling.write_text("keep\n", encoding="utf-8")
        removed = uninstall_integration("codex", yes=True)
        self.assertEqual(removed.status.state, IntegrationState.ABSENT)
        self.assertFalse(self.target.exists())
        self.assertTrue(sibling.is_file())

    def test_unowned_conflict_is_never_overwritten_or_removed(self) -> None:
        self.target.mkdir(parents=True)
        foreign = self.target / "SKILL.md"
        foreign.write_text("foreign\n", encoding="utf-8")

        status = integration_status("codex")
        self.assertEqual(status.state, IntegrationState.UNOWNED_CONFLICT)
        with self.assertRaisesRegex(DyroError, "拒绝覆盖"):
            install_integration("codex", yes=True)
        with self.assertRaisesRegex(DyroError, "拒绝删除"):
            uninstall_integration("codex", yes=True)
        self.assertEqual(foreign.read_text(encoding="utf-8"), "foreign\n")

    def test_owned_drift_blocks_upgrade_and_uninstall(self) -> None:
        install_integration("codex", yes=True)
        self.target.joinpath("SKILL.md").write_text("drift\n", encoding="utf-8")

        self.assertEqual(integration_status("codex").state, IntegrationState.DRIFTED)
        with self.assertRaisesRegex(DyroError, "drifted"):
            install_integration("codex", yes=True)
        with self.assertRaisesRegex(DyroError, "drifted"):
            uninstall_integration("codex", yes=True)
        self.assertEqual(self.target.joinpath("SKILL.md").read_text(), "drift\n")

    def test_outdated_owned_asset_can_upgrade(self) -> None:
        install_integration("codex", yes=True)
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest["asset_version"] = manifest["asset_version"] + 1
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")

        self.assertEqual(integration_status("codex").state, IntegrationState.OUTDATED)
        result = install_integration("codex", yes=True)
        self.assertEqual(result.status.state, IntegrationState.CURRENT)

    def test_stale_manifest_and_recovery_marker_fail_closed(self) -> None:
        install_integration("codex", yes=True)
        shutil.rmtree(self.target)
        self.assertEqual(
            integration_status("codex").state, IntegrationState.STALE_MANIFEST
        )

        self.manifest.unlink()
        transaction = self.dyro_home / "integrations" / "codex.transaction.json"
        transaction.write_text("{}\n", encoding="utf-8")
        self.assertEqual(
            integration_status("codex").state, IntegrationState.RECOVERY_REQUIRED
        )
        with self.assertRaisesRegex(DyroError, "recovery_required"):
            install_integration("codex", yes=True)

    def test_symlink_target_and_state_paths_fail_closed(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        outside_file = outside / "sentinel"
        outside_file.write_text("keep\n", encoding="utf-8")
        self.target.parent.mkdir(parents=True)
        self.target.symlink_to(outside, target_is_directory=True)

        self.assertEqual(
            integration_status("codex").state,
            IntegrationState.UNOWNED_CONFLICT,
        )
        with self.assertRaises(DyroError):
            install_integration("codex", yes=True)
        self.assertEqual(outside_file.read_text(encoding="utf-8"), "keep\n")

        self.target.unlink()
        state_outside = self.root / "state-outside"
        state_outside.mkdir()
        self.dyro_home.symlink_to(state_outside, target_is_directory=True)
        self.assertEqual(
            integration_status("codex").state,
            IntegrationState.RECOVERY_REQUIRED,
        )

    def test_install_failure_rolls_back_target_and_manifest(self) -> None:
        real_atomic_write = manager.atomic_write_text

        def fail_manifest(path: Path, content: str) -> None:
            if path == self.manifest:
                raise OSError("injected manifest failure")
            real_atomic_write(path, content)

        with (
            patch.object(manager, "atomic_write_text", side_effect=fail_manifest),
            self.assertRaisesRegex(OSError, "injected"),
        ):
            install_integration("codex", yes=True)

        self.assertFalse(self.target.exists())
        self.assertFalse(self.manifest.exists())
        self.assertEqual(integration_status("codex").state, IntegrationState.ABSENT)

    def test_absent_rollback_dangling_target_keeps_recovery_marker(self) -> None:
        real_atomic_write = manager.atomic_write_text
        real_remove_tree = manager._remove_tree
        missing = self.root / "missing-target"

        def fail_manifest(path: Path, content: str) -> None:
            if path == self.manifest:
                raise OSError("injected manifest failure")
            real_atomic_write(path, content)

        def replace_target_with_symlink(path: Path) -> None:
            real_remove_tree(path)
            if path == self.target:
                path.symlink_to(missing, target_is_directory=True)

        with (
            patch.object(manager, "atomic_write_text", side_effect=fail_manifest),
            patch.object(
                manager, "_remove_tree", side_effect=replace_target_with_symlink
            ),
            self.assertRaisesRegex(OSError, "injected"),
        ):
            install_integration("codex", yes=True)

        self.assertTrue(self.target.is_symlink())
        self.assertEqual(
            integration_status("codex").state, IntegrationState.RECOVERY_REQUIRED
        )

    def test_absent_rollback_dangling_manifest_keeps_recovery_marker(self) -> None:
        real_atomic_write = manager.atomic_write_text
        missing = self.root / "missing-manifest"

        def replace_manifest_with_symlink(path: Path, content: str) -> None:
            if path == self.manifest:
                path.symlink_to(missing)
                raise OSError("injected manifest failure")
            real_atomic_write(path, content)

        with (
            patch.object(
                manager,
                "atomic_write_text",
                side_effect=replace_manifest_with_symlink,
            ),
            self.assertRaisesRegex(OSError, "injected"),
        ):
            install_integration("codex", yes=True)

        self.assertTrue(self.manifest.is_symlink())
        self.assertEqual(
            integration_status("codex").state, IntegrationState.RECOVERY_REQUIRED
        )

    def test_upgrade_cleanup_failure_keeps_committed_state_recoverable(self) -> None:
        install_integration("codex", yes=True)
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest["asset_version"] = manifest["asset_version"] + 1
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        real_remove_tree = manager._remove_tree
        injected = False

        def fail_first_backup(path: Path) -> None:
            nonlocal injected
            if not injected and ".backup-" in path.name:
                injected = True
                raise OSError("injected backup cleanup failure")
            real_remove_tree(path)

        with (
            patch.object(manager, "_remove_tree", side_effect=fail_first_backup),
            self.assertRaisesRegex(OSError, "injected"),
        ):
            install_integration("codex", yes=True)

        self.assertEqual(
            integration_status("codex").state, IntegrationState.RECOVERY_REQUIRED
        )
        self.assertTrue(self.target.joinpath("SKILL.md").is_file())
        self.assertTrue(
            (self.dyro_home / "integrations" / "codex.transaction.json").exists()
        )

    def test_committed_upgrade_unlink_failure_keeps_recovery_marker(self) -> None:
        install_integration("codex", yes=True)
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest["asset_version"] = manifest["asset_version"] + 1
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")

        with (
            patch.object(
                manager,
                "_unlink_transaction",
                side_effect=OSError("injected unlink failure"),
            ),
            self.assertRaisesRegex(OSError, "injected"),
        ):
            install_integration("codex", yes=True)

        self.assertEqual(
            integration_status("codex").state, IntegrationState.RECOVERY_REQUIRED
        )
        self.assertTrue(self.target.joinpath("SKILL.md").is_file())

    def test_committed_upgrade_fsync_failure_recreates_recovery_marker(self) -> None:
        install_integration("codex", yes=True)
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest["asset_version"] = manifest["asset_version"] + 1
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        real_fsync_directory = manager.fsync_directory
        transaction = self.dyro_home / "integrations" / "codex.transaction.json"

        def fail_after_unlink(path: Path) -> None:
            if path == transaction.parent and not transaction.exists():
                raise OSError("injected directory fsync failure")
            real_fsync_directory(path)

        with (
            patch.object(manager, "fsync_directory", side_effect=fail_after_unlink),
            self.assertRaisesRegex(OSError, "injected"),
        ):
            install_integration("codex", yes=True)

        self.assertEqual(
            integration_status("codex").state, IntegrationState.RECOVERY_REQUIRED
        )
        self.assertTrue(transaction.exists())

    def test_committed_uninstall_cleanup_failure_keeps_recovery_marker(self) -> None:
        install_integration("codex", yes=True)
        with (
            patch.object(
                manager, "_remove_tree", side_effect=OSError("injected delete")
            ),
            self.assertRaisesRegex(OSError, "injected"),
        ):
            uninstall_integration("codex", yes=True)

        self.assertEqual(
            integration_status("codex").state, IntegrationState.RECOVERY_REQUIRED
        )
        self.assertFalse(self.target.exists())
        self.assertFalse(self.manifest.exists())

    def test_uninstall_precommit_failure_restores_owned_installation(self) -> None:
        install_integration("codex", yes=True)
        real_atomic_write = manager.atomic_write_text
        transaction = self.dyro_home / "integrations" / "codex.transaction.json"

        def fail_committed_marker(path: Path, content: str) -> None:
            if path == transaction and '"phase":"committed"' in content:
                raise OSError("injected committed marker failure")
            real_atomic_write(path, content)

        with (
            patch.object(
                manager, "atomic_write_text", side_effect=fail_committed_marker
            ),
            self.assertRaisesRegex(OSError, "injected"),
        ):
            uninstall_integration("codex", yes=True)

        self.assertEqual(integration_status("codex").state, IntegrationState.CURRENT)
        self.assertTrue(self.target.joinpath("SKILL.md").is_file())

    def test_uninstall_rollback_manifest_race_keeps_recovery_marker(self) -> None:
        install_integration("codex", yes=True)
        real_atomic_write = manager.atomic_write_text
        real_inventory = manager._inventory
        transaction = self.dyro_home / "integrations" / "codex.transaction.json"
        alternate_target = self.root / "different-target"

        def fail_committed_marker(path: Path, content: str) -> None:
            if path == transaction and '"phase":"committed"' in content:
                raise OSError("injected committed marker failure")
            real_atomic_write(path, content)

        def mutate_during_verification(path: Path) -> dict[str, str]:
            result = real_inventory(path)
            if path == self.target and transaction.exists() and self.manifest.exists():
                payload = json.loads(self.manifest.read_text(encoding="utf-8"))
                payload["target"] = str(alternate_target)
                self.manifest.write_text(json.dumps(payload), encoding="utf-8")
            return result

        with (
            patch.object(
                manager, "atomic_write_text", side_effect=fail_committed_marker
            ),
            patch.object(manager, "_inventory", side_effect=mutate_during_verification),
            self.assertRaisesRegex(OSError, "injected"),
        ):
            uninstall_integration("codex", yes=True)

        self.assertEqual(
            integration_status("codex").state, IntegrationState.RECOVERY_REQUIRED
        )
        self.assertTrue(transaction.exists())

    def test_nested_symlink_in_codex_home_path_is_rejected(self) -> None:
        actual = self.root / "actual"
        actual.joinpath("codex").mkdir(parents=True)
        alias = self.root / "alias"
        alias.symlink_to(actual, target_is_directory=True)
        escaped_home = alias / "codex"

        with self.assertRaisesRegex(DyroError, "不安全"):
            install_integration("codex", yes=True, codex_home=escaped_home)
        self.assertFalse(actual.joinpath("codex", "skills").exists())

    def test_missing_home_below_symlink_is_rejected_before_any_write(self) -> None:
        actual = self.root / "actual-missing"
        actual.mkdir()
        alias = self.root / "alias-missing"
        alias.symlink_to(actual, target_is_directory=True)
        escaped_home = alias / "new-codex-home"

        with self.assertRaisesRegex(DyroError, "不安全"):
            install_integration("codex", yes=True, codex_home=escaped_home)
        self.assertFalse(actual.joinpath("new-codex-home").exists())

    def test_status_reports_unsafe_missing_home_before_absent(self) -> None:
        actual = self.root / "status-actual"
        actual.mkdir()
        alias = self.root / "status-alias"
        alias.symlink_to(actual, target_is_directory=True)
        escaped_home = alias / "new-codex-home"

        status = integration_status("codex", codex_home=escaped_home)

        self.assertEqual(status.state, IntegrationState.UNOWNED_CONFLICT)
        self.assertIn("不安全", status.detail)
        self.assertFalse(actual.joinpath("new-codex-home").exists())

    def test_cli_status_dry_run_install_and_confirmation_gate(self) -> None:
        output = StringIO()
        before = self._tree_snapshot()
        with redirect_stdout(output):
            main(["integration", "status", "codex"])
            main(["--dry-run", "integration", "install", "codex"])
        self.assertIn("codex\tabsent", output.getvalue())
        self.assertIn("DRY RUN: install codex", output.getvalue())
        self.assertEqual(self._tree_snapshot(), before)

        preview = StringIO()
        with redirect_stdout(preview):
            main(["integration", "install", "codex"])
            main(["integration", "install", "codex", "--dry-run"])
        self.assertIn("DRY RUN: install codex", preview.getvalue())
        self.assertIn("重新运行并添加 --yes", preview.getvalue())
        self.assertEqual(self._tree_snapshot(), before)

        receipt = StringIO()
        with redirect_stdout(receipt):
            main(["integration", "install", "codex", "--yes"])
            main(["integration", "uninstall", "codex", "--yes"])
        self.assertIn(f"创建 {self.target}", receipt.getvalue())
        self.assertIn(f"移除自有目录 {self.target}", receipt.getvalue())
        self.assertEqual(integration_status("codex").state, IntegrationState.ABSENT)


if __name__ == "__main__":
    unittest.main()

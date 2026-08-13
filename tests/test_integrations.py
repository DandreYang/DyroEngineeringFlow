from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import re
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
    sync_managed_skill,
    uninstall_integration,
)
from dyro.integrations import manager


class IntegrationManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="dyro-integrations-")
        self.root = Path(self.tmp.name)
        self.codex_home = self.root / "codex"
        self.claude_home = self.root / "claude"
        self.dyro_home = self.root / "dyro"
        self.fake_home = self.root / "home"
        self.fake_home.mkdir()
        self.environment = patch.dict(
            os.environ,
            {
                "CODEX_HOME": str(self.codex_home),
                "DYRO_HOME": str(self.dyro_home),
                "HOME": str(self.fake_home),
                "DYRO_NO_UPDATE_CHECK": "1",
            },
            clear=False,
        )
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.tmp.cleanup()

    @property
    def mirror(self) -> Path:
        return self.dyro_home / "skills" / "dyro-control-plane"

    @property
    def avatar(self) -> Path:
        return self.codex_home / "skills" / "dyro-control-plane"

    @property
    def claude_avatar(self) -> Path:
        return self.claude_home / "skills" / "dyro-control-plane"

    @property
    def manifest(self) -> Path:
        return self.dyro_home / "integrations" / "skill.json"

    @property
    def legacy_manifest(self) -> Path:
        return self.dyro_home / "integrations" / "codex.json"

    def _host_homes(self, *, claude: bool = False) -> dict[str, Path]:
        homes = {"codex": self.codex_home}
        if claude:
            homes["claude"] = self.claude_home
        return homes

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
        self.assertIn("coding agent", frontmatter)
        self.assertNotIn("from Codex", frontmatter)
        for command in (
            "workspace list --format json",
            "status --format json",
            "doctor --format json",
            "objective attention <id> --format json",
            "objective explain <id> --format json",
            "objective plan <id> --format json",
        ):
            self.assertIn(command, content)
        for forbidden_action in ("`console`", "`dispatch`", "`task gates`"):
            self.assertIn(forbidden_action, content)
        self.assertIn("skip global discovery", content)
        self.assertIn("Never add `--include-paths`", content)
        for private_pattern in (
            r"/Users/[^<\s]",
            r"/home/[^<\s]",
            r"[A-Za-z]:[\\\\/]+Users[\\\\/]",
            r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}",
            r"session[_ -]?id",
            r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
        ):
            self.assertIsNone(
                re.search(private_pattern, content, flags=re.IGNORECASE),
                msg=private_pattern,
            )
        self.assertIn("$dyro-control-plane", metadata.read_text(encoding="utf-8"))
        for line in metadata.read_text(encoding="utf-8").splitlines():
            if ": " in line:
                self.assertTrue(line.split(": ", 1)[1].startswith('"'))

    def test_integration_status_json_is_structured(self) -> None:
        output = StringIO()
        with redirect_stdout(output):
            main(["integration", "status", "skill", "--format", "json"])

        payload = json.loads(output.getvalue())
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["kind"], "integration_status")
        self.assertEqual(payload["integration"], "skill")
        self.assertEqual(payload["state"], "absent")
        self.assertEqual(payload["avatars"][0]["host"], "codex")
        self.assertEqual(payload["avatars"][0]["state"], "missing")
        self.assertNotIn("target", payload)
        self.assertNotIn("detail", payload)
        self.assertNotIn("path", payload["avatars"][0])
        self.assertNotIn("detail", payload["avatars"][0])

        output = StringIO()
        with redirect_stdout(output):
            main(
                [
                    "integration",
                    "status",
                    "skill",
                    "--format",
                    "json",
                    "--include-paths",
                ]
            )
        with_paths = json.loads(output.getvalue())
        self.assertEqual(with_paths["target"], str(self.mirror))
        self.assertEqual(with_paths["avatars"][0]["path"], str(self.avatar))

    def test_integration_status_json_rejects_an_oversized_manifest(self) -> None:
        install_integration("skill", yes=True)
        self.manifest.write_bytes(b"{" + b"x" * (1024 * 1024))
        stdout = StringIO()
        stderr = StringIO()

        with (
            redirect_stdout(stdout),
            patch("sys.stderr", stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            main(["integration", "status", "skill", "--format", "json"])

        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(json.loads(stderr.getvalue())["code"], "FILE_TOO_LARGE")

    def test_status_and_dry_run_are_strictly_zero_write(self) -> None:
        before = self._tree_snapshot()
        status = integration_status("skill")
        plan = install_integration("skill", yes=False, dry_run=True)
        uninstall_plan = uninstall_integration("codex", yes=False, dry_run=True)

        self.assertEqual(status.state, IntegrationState.ABSENT)
        self.assertEqual(plan.status.state, IntegrationState.ABSENT)
        self.assertEqual(uninstall_plan.status.state, IntegrationState.ABSENT)
        self.assertEqual(self._tree_snapshot(), before)

    def test_install_creates_mirror_and_avatar_symlink(self) -> None:
        with self.assertRaisesRegex(DyroError, "--yes"):
            install_integration("skill", yes=False)

        installed = install_integration("skill", yes=True)
        self.assertEqual(installed.status.state, IntegrationState.CURRENT)
        self.assertTrue(self.mirror.joinpath("SKILL.md").is_file())
        self.assertFalse(self.mirror.is_symlink())
        self.assertTrue(self.avatar.is_symlink())
        self.assertEqual(self.avatar.resolve(), self.mirror.resolve())
        self.assertTrue(self.avatar.joinpath("SKILL.md").is_file())
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertEqual(manifest["integration"], "skill")
        self.assertEqual(manifest["schema_version"], 2)
        self.assertEqual(manifest["mirror"], str(self.mirror))
        self.assertEqual(set(manifest["files"]), {"SKILL.md", "agents/openai.yaml"})
        self.assertIn("codex", manifest["avatars"])

        before = self._tree_snapshot()
        again = install_integration("codex", yes=True)
        self.assertEqual(again.status.state, IntegrationState.CURRENT)
        self.assertEqual(self._tree_snapshot(), before)

        sibling = self.codex_home / "skills" / "user-skill.txt"
        sibling.write_text("keep\n", encoding="utf-8")
        removed = uninstall_integration("skill", yes=True)
        self.assertEqual(removed.status.state, IntegrationState.ABSENT)
        self.assertFalse(self.mirror.exists())
        self.assertFalse(self.avatar.exists() or self.avatar.is_symlink())
        self.assertTrue(sibling.is_file())

    def test_install_attaches_avatars_for_multiple_detected_hosts(self) -> None:
        self.claude_home.mkdir()
        result = install_integration(
            "skill",
            yes=True,
            host_homes=self._host_homes(claude=True),
        )
        self.assertEqual(result.status.state, IntegrationState.CURRENT)
        self.assertTrue(self.avatar.is_symlink())
        self.assertTrue(self.claude_avatar.is_symlink())
        self.assertEqual(self.claude_avatar.resolve(), self.mirror.resolve())
        hosts = {row.host for row in result.status.avatars}
        self.assertEqual(hosts, {"codex", "claude"})

    def test_unowned_conflict_is_never_overwritten_or_removed(self) -> None:
        self.avatar.mkdir(parents=True)
        foreign = self.avatar / "SKILL.md"
        foreign.write_text("foreign\n", encoding="utf-8")

        status = integration_status("skill")
        self.assertEqual(status.state, IntegrationState.UNOWNED_CONFLICT)
        with self.assertRaisesRegex(DyroError, "拒绝覆盖"):
            install_integration("skill", yes=True)
        # Unowned paths are not Dyro-owned, so uninstall also refuses.
        with self.assertRaisesRegex(DyroError, "拒绝删除|unowned_conflict"):
            uninstall_integration("skill", yes=True)
        self.assertEqual(foreign.read_text(encoding="utf-8"), "foreign\n")

    def test_owned_drift_via_avatar_blocks_upgrade_and_uninstall(self) -> None:
        install_integration("skill", yes=True)
        self.avatar.joinpath("SKILL.md").write_text("drift\n", encoding="utf-8")

        self.assertEqual(integration_status("skill").state, IntegrationState.DRIFTED)
        with self.assertRaisesRegex(DyroError, "drifted"):
            install_integration("skill", yes=True)
        with self.assertRaisesRegex(DyroError, "drifted"):
            uninstall_integration("skill", yes=True)
        self.assertEqual(self.mirror.joinpath("SKILL.md").read_text(), "drift\n")

    def test_outdated_owned_asset_can_upgrade(self) -> None:
        install_integration("skill", yes=True)
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest["asset_version"] = manifest["asset_version"] + 1
        # Keep digest consistent with files map for parser validity.
        self.manifest.write_text(
            json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        self.assertEqual(integration_status("skill").state, IntegrationState.OUTDATED)
        result = install_integration("skill", yes=True)
        self.assertEqual(result.status.state, IntegrationState.CURRENT)

    def test_missing_avatar_is_outdated_and_repairable(self) -> None:
        install_integration("skill", yes=True)
        self.avatar.unlink()
        self.assertEqual(integration_status("skill").state, IntegrationState.OUTDATED)
        repaired = install_integration("skill", yes=True)
        self.assertEqual(repaired.status.state, IntegrationState.CURRENT)
        self.assertTrue(self.avatar.is_symlink())

    def test_legacy_codex_copy_migrates_to_mirror_and_avatar(self) -> None:
        self.avatar.mkdir(parents=True)
        files = manager._asset_inventory()
        for relative, _digest in files.items():
            destination = self.avatar / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes((manager._asset_root() / relative).read_bytes())
        legacy = {
            "schema_version": 1,
            "integration": "codex",
            "asset_version": manager.ASSET_VERSION,
            "asset_digest": manager._asset_digest(files),
            "target": str(self.avatar),
            "files": files,
        }
        self.legacy_manifest.parent.mkdir(parents=True, exist_ok=True)
        self.legacy_manifest.write_text(
            json.dumps(legacy, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        status = integration_status("codex")
        self.assertEqual(status.state, IntegrationState.OUTDATED)
        migrated = install_integration("skill", yes=True)
        self.assertEqual(migrated.status.state, IntegrationState.CURRENT)
        self.assertTrue(self.mirror.joinpath("SKILL.md").is_file())
        self.assertTrue(self.avatar.is_symlink())
        self.assertEqual(self.avatar.resolve(), self.mirror.resolve())
        self.assertFalse(self.legacy_manifest.exists())
        self.assertTrue(self.manifest.is_file())

    def test_stale_manifest_and_recovery_marker_fail_closed(self) -> None:
        install_integration("skill", yes=True)
        shutil.rmtree(self.mirror)
        self.assertEqual(
            integration_status("skill").state, IntegrationState.STALE_MANIFEST
        )

        self.manifest.unlink()
        transaction = self.dyro_home / "integrations" / "skill.transaction.json"
        transaction.write_text("{}\n", encoding="utf-8")
        self.assertEqual(
            integration_status("skill").state, IntegrationState.RECOVERY_REQUIRED
        )
        with self.assertRaisesRegex(DyroError, "recovery_required"):
            install_integration("skill", yes=True)

    def test_symlink_avatar_to_foreign_path_is_conflict(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        outside_file = outside / "sentinel"
        outside_file.write_text("keep\n", encoding="utf-8")
        self.avatar.parent.mkdir(parents=True)
        self.avatar.symlink_to(outside, target_is_directory=True)

        self.assertEqual(
            integration_status("skill").state,
            IntegrationState.UNOWNED_CONFLICT,
        )
        with self.assertRaises(DyroError):
            install_integration("skill", yes=True)
        self.assertEqual(outside_file.read_text(encoding="utf-8"), "keep\n")

        self.avatar.unlink()
        state_outside = self.root / "state-outside"
        state_outside.mkdir()
        self.dyro_home.symlink_to(state_outside, target_is_directory=True)
        self.assertEqual(
            integration_status("skill").state,
            IntegrationState.RECOVERY_REQUIRED,
        )

    def test_install_failure_rolls_back_mirror_and_manifest(self) -> None:
        real_atomic_write = manager.atomic_write_text

        def fail_manifest(path: Path, content: str) -> None:
            if path == self.manifest:
                raise OSError("injected manifest failure")
            real_atomic_write(path, content)

        with (
            patch.object(manager, "atomic_write_text", side_effect=fail_manifest),
            self.assertRaisesRegex(OSError, "injected"),
        ):
            install_integration("skill", yes=True)

        self.assertFalse(self.mirror.exists())
        self.assertFalse(self.manifest.exists())
        self.assertFalse(self.avatar.exists() or self.avatar.is_symlink())
        self.assertEqual(integration_status("skill").state, IntegrationState.ABSENT)

    def test_absent_rollback_dangling_mirror_keeps_recovery_marker(self) -> None:
        real_atomic_write = manager.atomic_write_text
        real_remove_tree = manager._remove_tree
        missing = self.root / "missing-target"

        def fail_manifest(path: Path, content: str) -> None:
            if path == self.manifest:
                raise OSError("injected manifest failure")
            real_atomic_write(path, content)

        def replace_mirror_with_symlink(path: Path) -> None:
            real_remove_tree(path)
            if path == self.mirror:
                path.symlink_to(missing, target_is_directory=True)

        with (
            patch.object(manager, "atomic_write_text", side_effect=fail_manifest),
            patch.object(
                manager, "_remove_tree", side_effect=replace_mirror_with_symlink
            ),
            self.assertRaisesRegex(OSError, "injected"),
        ):
            install_integration("skill", yes=True)

        self.assertTrue(self.mirror.is_symlink())
        self.assertEqual(
            integration_status("skill").state, IntegrationState.RECOVERY_REQUIRED
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
            install_integration("skill", yes=True)

        self.assertTrue(self.manifest.is_symlink())
        self.assertEqual(
            integration_status("skill").state, IntegrationState.RECOVERY_REQUIRED
        )

    def test_upgrade_cleanup_failure_keeps_committed_state_recoverable(self) -> None:
        install_integration("skill", yes=True)
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest["asset_version"] = manifest["asset_version"] + 1
        self.manifest.write_text(
            json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
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
            install_integration("skill", yes=True)

        self.assertEqual(
            integration_status("skill").state, IntegrationState.RECOVERY_REQUIRED
        )
        self.assertTrue(self.mirror.joinpath("SKILL.md").is_file())
        self.assertTrue(
            (self.dyro_home / "integrations" / "skill.transaction.json").exists()
        )

    def test_committed_upgrade_unlink_failure_keeps_recovery_marker(self) -> None:
        install_integration("skill", yes=True)
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest["asset_version"] = manifest["asset_version"] + 1
        self.manifest.write_text(
            json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        with (
            patch.object(
                manager,
                "_unlink_transaction",
                side_effect=OSError("injected unlink failure"),
            ),
            self.assertRaisesRegex(OSError, "injected"),
        ):
            install_integration("skill", yes=True)

        self.assertEqual(
            integration_status("skill").state, IntegrationState.RECOVERY_REQUIRED
        )
        self.assertTrue(self.mirror.joinpath("SKILL.md").is_file())

    def test_committed_upgrade_fsync_failure_recreates_recovery_marker(self) -> None:
        install_integration("skill", yes=True)
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest["asset_version"] = manifest["asset_version"] + 1
        self.manifest.write_text(
            json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        real_fsync_directory = manager.fsync_directory
        transaction = self.dyro_home / "integrations" / "skill.transaction.json"

        def fail_after_unlink(path: Path) -> None:
            if path == transaction.parent and not transaction.exists():
                raise OSError("injected directory fsync failure")
            real_fsync_directory(path)

        with (
            patch.object(manager, "fsync_directory", side_effect=fail_after_unlink),
            self.assertRaisesRegex(OSError, "injected"),
        ):
            install_integration("skill", yes=True)

        self.assertEqual(
            integration_status("skill").state, IntegrationState.RECOVERY_REQUIRED
        )
        self.assertTrue(transaction.exists())

    def test_committed_uninstall_cleanup_failure_keeps_recovery_marker(self) -> None:
        install_integration("skill", yes=True)
        with (
            patch.object(
                manager, "_remove_tree", side_effect=OSError("injected delete")
            ),
            self.assertRaisesRegex(OSError, "injected"),
        ):
            uninstall_integration("skill", yes=True)

        self.assertEqual(
            integration_status("skill").state, IntegrationState.RECOVERY_REQUIRED
        )
        self.assertFalse(self.mirror.exists())
        self.assertFalse(self.manifest.exists())

    def test_uninstall_precommit_failure_restores_owned_installation(self) -> None:
        install_integration("skill", yes=True)
        real_atomic_write = manager.atomic_write_text
        transaction = self.dyro_home / "integrations" / "skill.transaction.json"

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
            uninstall_integration("skill", yes=True)

        self.assertEqual(integration_status("skill").state, IntegrationState.CURRENT)
        self.assertTrue(self.mirror.joinpath("SKILL.md").is_file())
        self.assertTrue(self.avatar.is_symlink())

    def test_uninstall_rollback_manifest_race_keeps_recovery_marker(self) -> None:
        install_integration("skill", yes=True)
        real_atomic_write = manager.atomic_write_text
        real_inventory = manager._inventory
        transaction = self.dyro_home / "integrations" / "skill.transaction.json"
        alternate_mirror = self.root / "different-mirror"

        def fail_committed_marker(path: Path, content: str) -> None:
            if path == transaction and '"phase":"committed"' in content:
                raise OSError("injected committed marker failure")
            real_atomic_write(path, content)

        def mutate_during_verification(path: Path) -> dict[str, str]:
            result = real_inventory(path)
            if path == self.mirror and transaction.exists() and self.manifest.exists():
                payload = json.loads(self.manifest.read_text(encoding="utf-8"))
                payload["mirror"] = str(alternate_mirror)
                self.manifest.write_text(json.dumps(payload), encoding="utf-8")
            return result

        with (
            patch.object(
                manager, "atomic_write_text", side_effect=fail_committed_marker
            ),
            patch.object(manager, "_inventory", side_effect=mutate_during_verification),
            self.assertRaisesRegex(OSError, "injected"),
        ):
            uninstall_integration("skill", yes=True)

        self.assertEqual(
            integration_status("skill").state, IntegrationState.RECOVERY_REQUIRED
        )
        self.assertTrue(transaction.exists())

    def test_nested_symlink_in_codex_home_path_is_rejected(self) -> None:
        actual = self.root / "actual"
        actual.joinpath("codex").mkdir(parents=True)
        alias = self.root / "alias"
        alias.symlink_to(actual, target_is_directory=True)
        escaped_home = alias / "codex"

        with self.assertRaisesRegex(DyroError, "不安全|unowned_conflict|拒绝覆盖"):
            install_integration("skill", yes=True, codex_home=escaped_home)
        self.assertFalse(actual.joinpath("codex", "skills").exists())

    def test_missing_home_below_symlink_is_rejected_before_any_write(self) -> None:
        actual = self.root / "actual-missing"
        actual.mkdir()
        alias = self.root / "alias-missing"
        alias.symlink_to(actual, target_is_directory=True)
        escaped_home = alias / "new-codex-home"

        with self.assertRaisesRegex(DyroError, "不安全|unowned_conflict|拒绝覆盖"):
            install_integration("skill", yes=True, codex_home=escaped_home)
        self.assertFalse(actual.joinpath("new-codex-home").exists())

    def test_status_reports_unsafe_missing_home_before_absent(self) -> None:
        actual = self.root / "status-actual"
        actual.mkdir()
        alias = self.root / "status-alias"
        alias.symlink_to(actual, target_is_directory=True)
        escaped_home = alias / "new-codex-home"

        status = integration_status("skill", codex_home=escaped_home)

        self.assertEqual(status.state, IntegrationState.UNOWNED_CONFLICT)
        self.assertTrue(
            "不安全" in status.detail
            or any("不安全" in row.detail for row in status.avatars),
            msg=f"detail={status.detail!r} avatars={status.avatars!r}",
        )
        self.assertFalse(actual.joinpath("new-codex-home").exists())

    def _write_legacy_manifest(self, target: Path, files: dict[str, str]) -> None:
        self.legacy_manifest.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "integration": "codex",
            "asset_version": manager.ASSET_VERSION,
            "asset_digest": manager._asset_digest(files),
            "target": str(target),
            "files": files,
        }
        self.legacy_manifest.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def test_unbound_legacy_target_is_not_deleted_on_uninstall(self) -> None:
        victim = self.root / "victim_dir"
        victim.mkdir()
        files = manager._asset_inventory()
        for relative in files:
            destination = victim / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes((manager._asset_root() / relative).read_bytes())
        self._write_legacy_manifest(victim, files)

        status = integration_status("skill")
        self.assertEqual(status.state, IntegrationState.ABSENT)
        plan = uninstall_integration("skill", yes=True)
        self.assertEqual(plan.status.state, IntegrationState.ABSENT)
        self.assertTrue(victim.exists())
        self.assertTrue((victim / "SKILL.md").is_file())

    def test_forged_legacy_over_foreign_avatar_is_refused(self) -> None:
        self.avatar.mkdir(parents=True)
        (self.avatar / "SKILL.md").write_text("FOREIGN_SKILL_CONTENT\n", encoding="utf-8")
        foreign_files = manager._inventory(self.avatar)
        self._write_legacy_manifest(self.avatar, foreign_files)

        status = integration_status("skill")
        self.assertEqual(status.state, IntegrationState.UNOWNED_CONFLICT)
        with self.assertRaisesRegex(DyroError, "拒绝覆盖|unowned_conflict"):
            install_integration("skill", yes=True)
        self.assertEqual(
            (self.avatar / "SKILL.md").read_text(encoding="utf-8"),
            "FOREIGN_SKILL_CONTENT\n",
        )
        self.assertFalse(self.avatar.is_symlink())

    def test_sync_managed_skill_skips_absent_without_first_install(self) -> None:
        self.assertIsNone(sync_managed_skill(yes=True, allow_first_install=False))
        self.assertEqual(integration_status("skill").state, IntegrationState.ABSENT)

    def test_sync_managed_skill_upgrades_outdated(self) -> None:
        install_integration("skill", yes=True)
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest["asset_version"] = manifest["asset_version"] + 1
        self.manifest.write_text(
            json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.assertEqual(integration_status("skill").state, IntegrationState.OUTDATED)
        plan = sync_managed_skill(yes=True, allow_first_install=False)
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.status.state, IntegrationState.CURRENT)

    def test_plan_surfaces_missing_host_blocker(self) -> None:
        with patch.dict(os.environ):
            for key in ("CODEX_HOME", "CLAUDE_HOME", "AGENTS_HOME", "CURSOR_HOME"):
                os.environ.pop(key, None)
            plan = install_integration("skill", yes=False, dry_run=True)
        self.assertEqual(plan.status.state, IntegrationState.ABSENT)
        self.assertFalse(plan.status.avatars)
        self.assertTrue(
            any("未检测到宿主目录" in change for change in plan.changes),
            msg=plan.changes,
        )
        self.assertTrue(
            any("不会创建孤立镜像" in change for change in plan.changes),
            msg=plan.changes,
        )

    def test_cli_sync_is_upgrade_only_and_skips_absent(self) -> None:
        output = StringIO()
        before = self._tree_snapshot()
        with redirect_stdout(output):
            main(["integration", "sync", "skill"])
            main(["integration", "sync", "skill", "--yes"])
        self.assertIn("无需同步", output.getvalue())
        self.assertEqual(self._tree_snapshot(), before)

        install_integration("skill", yes=True)
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest["asset_version"] = manifest["asset_version"] + 1
        self.manifest.write_text(
            json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        synced = StringIO()
        with redirect_stdout(synced):
            main(["integration", "sync", "skill", "--yes"])
        text = synced.getvalue()
        self.assertIn("install skill", text)
        self.assertIn("current", text)
        self.assertEqual(integration_status("skill").state, IntegrationState.CURRENT)

    def test_cli_status_dry_run_install_and_confirmation_gate(self) -> None:
        output = StringIO()
        before = self._tree_snapshot()
        with redirect_stdout(output):
            main(["integration", "status", "skill"])
            main(["--dry-run", "integration", "install", "skill"])
        self.assertIn("skill\tabsent", output.getvalue())
        self.assertIn("DRY RUN: install skill", output.getvalue())
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
            main(["integration", "install", "skill", "--yes"])
            main(["integration", "status", "skill"])
            main(["integration", "uninstall", "skill", "--yes"])
        text = receipt.getvalue()
        self.assertIn(f"创建镜像 {self.mirror}", text)
        self.assertIn(f"创建分身 {self.avatar}", text)
        self.assertIn("avatar\tcodex\tcurrent", text)
        self.assertIn(f"移除镜像 {self.mirror}", text)
        self.assertEqual(integration_status("skill").state, IntegrationState.ABSENT)


if __name__ == "__main__":
    unittest.main()

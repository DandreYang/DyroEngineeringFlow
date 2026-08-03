from pathlib import Path

from dyro.config import ValidationError, expand_argv, external_security_errors, load
from dyro.profile import config_value, set_config_value

from .support import WorkspaceCase


class ConfigTests(WorkspaceCase):
    def test_loads_workspace_and_safe_template(self) -> None:
        config = load(self.root)
        self.assertEqual(config.name, "test-workspace")
        self.assertEqual(config.recommended_tool, "")
        self.assertEqual(config.repositories["api"].mount, "services/api")
        self.assertEqual(expand_argv(("echo", "{workspace}"), workspace=Path("/tmp/work")), ("echo", "/tmp/work"))

    def test_loads_and_validates_project_recommended_tool(self) -> None:
        config_path = self.root / "dyro.toml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                'name = "test-workspace"',
                'name = "test-workspace"\nrecommended_tool = "cursor-desktop"',
            ),
            encoding="utf-8",
        )
        self.assertEqual(load(self.root).recommended_tool, "cursor-desktop")

        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                'recommended_tool = "cursor-desktop"',
                'recommended_tool = "bad tool"',
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValidationError, "workspace.recommended_tool"):
            load(self.root)

    def test_recommended_tool_can_be_managed_without_manual_toml_editing(self) -> None:
        config = load(self.root)
        set_config_value(
            config,
            "workspace.recommended_tool",
            "openclaw",
        )

        updated = load(self.root)
        self.assertEqual(
            config_value(updated, "workspace.recommended_tool"), "openclaw"
        )

    def test_rejects_parent_traversal(self) -> None:
        config = (self.root / "dyro.toml").read_text(encoding="utf-8")
        (self.root / "dyro.toml").write_text(config.replace('path = "repositories/api"', 'path = "../escape"'), encoding="utf-8")
        with self.assertRaises(ValidationError):
            load(self.root)

    def test_rejects_string_policy_booleans(self) -> None:
        config_path = self.root / "dyro.toml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace("allow_push = false", 'allow_push = "false"'),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValidationError, "policy.allow_push 必须是布尔值"):
            load(self.root)

    def test_rejects_disabled_clean_merge_policy(self) -> None:
        config_path = self.root / "dyro.toml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "require_clean_merge = true",
                "require_clean_merge = false",
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValidationError, "policy.require_clean_merge 必须为 true"):
            load(self.root)

    def test_external_profile_reports_required_signed_identity_migration(self) -> None:
        config_path = self.root / "dyro.toml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "require_clean_merge = true",
                'require_clean_merge = true\nexecution_mode = "external"',
            ),
            encoding="utf-8",
        )

        self.assertEqual(
            external_security_errors(load(self.root).policy),
            ("policy.require_signed_execution = true", "policy.require_signed_review = true"),
        )

    def test_loads_disabled_unattended_ceiling_and_objectives_location(self) -> None:
        config = load(self.root)

        self.assertEqual(config.objectives_dir, config.root / ".dyro/objectives")
        self.assertFalse(config.policy.allow_unattended_execute)
        self.assertFalse(config.policy.allow_unattended_review)
        self.assertFalse(config.policy.allow_unattended_merge)

        config_path = self.root / "dyro.toml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "require_clean_merge = true",
                "\n".join(
                    (
                        "require_clean_merge = true",
                        "allow_unattended_execute = true",
                        "allow_unattended_review = true",
                        "allow_unattended_merge = true",
                    )
                ),
            ),
            encoding="utf-8",
        )
        updated = load(self.root)
        self.assertTrue(updated.policy.allow_unattended_execute)
        self.assertTrue(updated.policy.allow_unattended_review)
        self.assertTrue(updated.policy.allow_unattended_merge)

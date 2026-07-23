from pathlib import Path

from dyro.config import ValidationError, expand_argv, load

from .support import WorkspaceCase


class ConfigTests(WorkspaceCase):
    def test_loads_workspace_and_safe_template(self) -> None:
        config = load(self.root)
        self.assertEqual(config.name, "test-workspace")
        self.assertEqual(config.repositories["api"].mount, "services/api")
        self.assertEqual(expand_argv(("echo", "{workspace}"), workspace=Path("/tmp/work")), ("echo", "/tmp/work"))

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

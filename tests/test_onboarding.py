from dyro.config import load
from dyro.errors import ValidationError
from dyro.onboarding import RepositoryInput, ask_for_workspace, bootstrap, discover_repositories, render_config, repository_input_from_path

from .support import WorkspaceCase


class OnboardingTests(WorkspaceCase):
    def test_wizard_collects_real_repository_inputs(self) -> None:
        responses = iter(["team-space", "release", "api", "repositories/api", "services/api", "", ""])
        name, repositories, base = ask_for_workspace("default", ask=lambda _: next(responses))
        self.assertEqual((name, base), ("team-space", "release"))
        self.assertEqual(repositories[0].id, "api")
        self.assertIn('[repositories.api]', render_config(name, repositories, base))

    def test_bootstrap_clones_only_missing_anchor(self) -> None:
        source = self.anchor
        original = (self.root / "dyro.toml").read_text(encoding="utf-8")
        updated = original.replace('path = "repositories/api"', 'path = "repositories/cloned-api"')
        updated = updated.replace('mount = "services/api"', f'mount = "services/api"\nremote = "{source}"')
        (self.root / "dyro.toml").write_text(updated, encoding="utf-8")
        config = load(self.root)
        messages = bootstrap(config)
        self.assertTrue(any(message.startswith("CLONE api") for message in messages))
        self.assertTrue((self.root / "repositories/cloned-api/.git").exists())

    def test_discover_repositories_uses_workspace_relative_paths(self) -> None:
        from .support import shell

        shell("git", "remote", "add", "origin", "https://example.test/acme/api.git", cwd=self.anchor)
        web = self.root / "repositories/web"
        web.mkdir(parents=True)

        shell("git", "init", "-b", "main", cwd=web)
        discovered = discover_repositories(self.root)

        self.assertEqual([(repo.id, repo.path, repo.mount) for repo in discovered], [
            ("api", "repositories/api", "api"),
            ("web", "repositories/web", "web"),
        ])
        self.assertEqual(discovered[0].remote, "https://example.test/acme/api.git")

    def test_repository_input_rejects_mount_outside_workspace(self) -> None:
        with self.assertRaises(ValidationError):
            repository_input_from_path(self.root, "repositories/api", mount="../outside")

    def test_render_config_supports_repository_id_with_dot(self) -> None:
        config_file = self.root / "dyro.toml"
        config_file.write_text(
            render_config("workspace", [
                RepositoryInput("web.app", "repositories/web", "web"),
            ]),
            encoding="utf-8",
        )

        self.assertIn("web.app", load(self.root).repositories)

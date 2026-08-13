from dyro.config import load
from dyro.errors import DyroError, ValidationError
from dyro.onboarding import (
    RepositoryInput,
    SetupPlan,
    ask_for_workspace,
    bootstrap,
    discover_repositories,
    render_config,
    render_setup_plan,
    repository_from_remote,
    repository_input_from_path,
)

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

    def test_bootstrap_rejects_a_symlinked_destination_parent(self) -> None:
        outside = self.root.parent / f"{self.root.name}-outside"
        outside.mkdir()
        (self.root / "escape").symlink_to(outside, target_is_directory=True)
        profile = (self.root / "dyro.toml").read_text(encoding="utf-8")
        profile = profile.replace(
            'path = "repositories/api"', 'path = "escape/api"'
        ).replace(
            'mount = "services/api"',
            f'mount = "services/api"\nremote = "{self.anchor}"',
        )
        (self.root / "dyro.toml").write_text(profile, encoding="utf-8")

        with self.assertRaisesRegex(DyroError, "符号链接"):
            bootstrap(load(self.root))

        self.assertFalse((outside / "api").exists())

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

    def test_render_config_requires_an_explicit_provider_choice(self) -> None:
        content = render_config("workspace", [RepositoryInput("api", "repositories/api", "api")])

        self.assertNotIn("[adapters.codex]", content)

    def test_render_config_writes_launchable_non_codex_presets(self) -> None:
        content = render_config(
            "workspace",
            [RepositoryInput("api", "repositories/api", "api")],
            adapter_presets=("grok", "claude"),
        )

        self.assertIn("[adapters.grok]", content)
        self.assertIn('launch = ["grok", "--cwd", "{workspace}"]', content)
        self.assertIn("[adapters.claude]", content)
        self.assertIn('launch = ["claude"]', content)

    def test_remote_repository_and_setup_plan_are_safe_to_preview(self) -> None:
        repository = repository_from_remote("git@github.com:acme/payments.git")
        plan = SetupPlan(
            root=self.root,
            name="workspace",
            repositories=(repository,),
            default_base="main",
            line_id="dev",
            branch="feat/dev",
        )

        self.assertEqual((repository.id, repository.path), ("payments", "repositories/payments"))
        self.assertTrue(plan.needs_bootstrap)
        self.assertIn("开发线：dev（feat/dev）", render_setup_plan(plan))

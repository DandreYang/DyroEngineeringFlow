from dyro.config import load
from dyro.workspace import create_line, doctor, line_repository_path, list_lines

from .support import WorkspaceCase, shell


class WorkspaceTests(WorkspaceCase):
    def test_create_line_and_dynamic_doctor(self) -> None:
        config = load(self.root)
        line = create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        self.assertEqual(line.id, "alpha")
        self.assertTrue((line_repository_path(config, line, "api") / ".git").exists())
        self.assertEqual([item.id for item in list_lines(config)], ["alpha"])
        findings = doctor(config)
        self.assertFalse(any(item.startswith("FAIL") for item in findings), findings)

    def test_line_persists_a_base_per_repository(self) -> None:
        web = self.root / "repositories/web"
        web.mkdir(parents=True)
        shell("git", "init", "-b", "main", cwd=web)
        shell("git", "config", "user.name", "Test User", cwd=web)
        shell("git", "config", "user.email", "test@example.com", cwd=web)
        (web / "README.md").write_text("web\n", encoding="utf-8")
        shell("git", "add", "README.md", cwd=web)
        shell("git", "commit", "-m", "chore: initial", cwd=web)
        shell("git", "branch", "release", cwd=web)
        config_path = self.root / "dyro.toml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8")
            + '''\n[repositories.web]\npath = "repositories/web"\nmount = "clients/web"\n''',
            encoding="utf-8",
        )

        from dyro.config import load

        line = create_line(
            load(self.root),
            line_id="mixed-baselines",
            branch="feat/mixed-baselines",
            base="main",
            repository_bases={"api": "main", "web": "release"},
        )

        self.assertEqual(line.base_for("api"), "main")
        self.assertEqual(line.base_for("web"), "release")
        persisted = list_lines(load(self.root))[0]
        self.assertEqual(persisted.base_for("web"), "release")

    def test_anchor_reference_storage_is_explicit_and_doctor_validates_it(self) -> None:
        shell("git", "checkout", "-b", "feat/reuse-anchor", cwd=self.anchor)
        config = load(self.root)

        line = create_line(
            config,
            line_id="reuse-anchor",
            branch="feat/reuse-anchor",
            base="main",
            storage_modes={"api": "anchor-reference"},
        )

        path = line_repository_path(config, line, "api")
        self.assertTrue(path.is_symlink())
        self.assertEqual(line.storage_for("api"), "anchor-reference")
        findings = doctor(config)
        self.assertFalse(any(item.startswith("FAIL") for item in findings), findings)

        shell("git", "checkout", "main", cwd=self.anchor)
        findings = doctor(config)
        self.assertTrue(any("expected feat/reuse-anchor" in item for item in findings), findings)

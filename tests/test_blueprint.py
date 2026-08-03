from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from dyro.blueprint import (
    apply_join_plan,
    build_join_plan,
    load_blueprint_source,
    parse_blueprint,
)
from dyro.cli import main
from dyro.config import load
from dyro.errors import DyroError, ValidationError
from dyro.workspace import get_line

from .support import shell


def _create_remote(parent: Path, name: str) -> tuple[Path, str]:
    source = parent / f"{name}-source"
    remote = parent / f"{name}.git"
    source.mkdir()
    shell("git", "init", "-b", "main", cwd=source)
    shell("git", "config", "user.name", "Test User", cwd=source)
    shell("git", "config", "user.email", "test@example.com", cwd=source)
    source.joinpath("README.md").write_text(f"{name}\n", encoding="utf-8")
    shell("git", "add", "README.md", cwd=source)
    shell("git", "commit", "-m", "chore: initial", cwd=source)
    shell("git", "clone", "--bare", str(source), str(remote), cwd=parent)
    head = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=source,
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout.strip()
    return remote, head


def _blueprint_text(api_remote: Path, api_head: str, web_remote: Path, web_head: str) -> str:
    return f'''schema_version = 1

[workspace]
name = "acme-platform"
suggested_directory = "acme-platform"
default_line = "feature-a"
default_base = "main"

[repositories.api]
remote = "{api_remote}"
path = "repositories/services/api"
mount = "services/api"
verify = [["git", "diff", "--check"]]

[repositories.web]
remote = "{web_remote}"
path = "repositories/clients/web"
mount = "clients/web"
verify = []

[lines.feature-a]
branch = "feat/feature-a"

[lines.feature-a.bases]
api = "{api_head}"
web = "{web_head}"
'''


class BlueprintParsingTests(unittest.TestCase):
    def test_public_example_is_a_valid_generic_blueprint(self) -> None:
        example = Path(__file__).parents[1] / "examples/blueprints/acme-platform.toml"

        blueprint = parse_blueprint(example.read_bytes())

        self.assertEqual(blueprint.name, "acme-platform")
        self.assertEqual(set(blueprint.repositories), {"api", "web"})

    def test_parses_generic_multi_repository_blueprint_with_pinned_bases(self) -> None:
        sha_a = "a" * 40
        sha_b = "b" * 40
        blueprint = parse_blueprint(
            _blueprint_text(
                Path("/tmp/api.git"), sha_a, Path("/tmp/web.git"), sha_b
            ).encode("utf-8")
        )

        self.assertEqual(blueprint.name, "acme-platform")
        self.assertEqual(blueprint.default_line, "feature-a")
        self.assertEqual(tuple(blueprint.repositories), ("api", "web"))
        self.assertEqual(blueprint.lines["feature-a"].base_for("web"), sha_b)
        self.assertEqual(
            blueprint.repositories["api"].verify,
            (("git", "diff", "--check"),),
        )

    def test_rejects_unknown_fields_and_moving_branch_bases(self) -> None:
        text = _blueprint_text(
            Path("/tmp/api.git"), "a" * 40, Path("/tmp/web.git"), "b" * 40
        )
        with self.assertRaisesRegex(ValidationError, "未知字段"):
            parse_blueprint(
                text.replace(
                    "schema_version = 1",
                    "schema_version = 1\ninternal_project = true",
                ).encode("utf-8")
            )

        with self.assertRaisesRegex(ValidationError, "完整提交 SHA"):
            parse_blueprint(text.replace('api = "' + "a" * 40 + '"', 'api = "origin/main"').encode("utf-8"))

    def test_rejects_inline_http_credentials(self) -> None:
        text = _blueprint_text(
            Path("/tmp/api.git"), "a" * 40, Path("/tmp/web.git"), "b" * 40
        ).replace(
            'remote = "/tmp/api.git"',
            'remote = "https://token@example.com/acme/api.git"',
        )

        with self.assertRaisesRegex(ValidationError, "凭据"):
            parse_blueprint(text.encode("utf-8"))

    def test_rejects_anchor_reference_storage(self) -> None:
        text = _blueprint_text(
            Path("/tmp/api.git"), "a" * 40, Path("/tmp/web.git"), "b" * 40
        ).replace(
            '[lines.feature-a.bases]',
            '[lines.feature-a.storage_modes]\napi = "anchor-reference"\n\n[lines.feature-a.bases]',
        )

        with self.assertRaisesRegex(ValidationError, "不会让开发线共享 anchor"):
            parse_blueprint(text.encode("utf-8"))


class BlueprintJoinTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="dyro-blueprint-")
        self.parent = Path(self.tmp.name)
        self.api_remote, self.api_head = _create_remote(self.parent, "api")
        self.web_remote, self.web_head = _create_remote(self.parent, "web")
        self.blueprint_file = self.parent / "dyro-blueprint.toml"
        self.blueprint_file.write_text(
            _blueprint_text(
                self.api_remote,
                self.api_head,
                self.web_remote,
                self.web_head,
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_join_creates_detached_anchors_and_selected_development_line(self) -> None:
        target = self.parent / "workspace"
        document = load_blueprint_source(str(self.blueprint_file))
        plan = build_join_plan(document, target=target, line_id=None)

        apply_join_plan(plan)
        apply_join_plan(plan)  # completed plans are safe and resumable

        config = load(target)
        line = get_line(config, "feature-a")
        self.assertEqual(line.branch, "feat/feature-a")
        self.assertEqual(line.base_for("api"), self.api_head)
        self.assertEqual(line.base_for("web"), self.web_head)
        self.assertEqual(
            target.joinpath(".dyro/join.json").read_text(encoding="utf-8").count('"status": "complete"'),
            1,
        )
        for repo_id, expected_head in (("api", self.api_head), ("web", self.web_head)):
            anchor = target / config.repositories[repo_id].path
            branch = subprocess.run(
                ("git", "branch", "--show-current"),
                cwd=anchor,
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout.strip()
            head = subprocess.run(
                ("git", "rev-parse", "HEAD"),
                cwd=anchor,
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout.strip()
            self.assertEqual(branch, "")
            self.assertEqual(head, expected_head)
            self.assertTrue((target / "versions/feature-a" / config.repositories[repo_id].mount).is_dir())

    def test_join_dry_run_and_validation_do_not_create_target(self) -> None:
        target = self.parent / "preview"
        output = StringIO()
        with redirect_stdout(output):
            main(
                [
                    "join",
                    str(self.blueprint_file),
                    "--path",
                    str(target),
                    "--dry-run",
                ]
            )
            main(["blueprint", "validate", str(self.blueprint_file)])

        self.assertFalse(target.exists())
        self.assertIn("DRY RUN", output.getvalue())
        self.assertIn("仓库：2 个", output.getvalue())

    def test_join_refuses_an_unrelated_non_empty_target(self) -> None:
        target = self.parent / "occupied"
        target.mkdir()
        target.joinpath("notes.txt").write_text("user-owned\n", encoding="utf-8")
        plan = build_join_plan(
            load_blueprint_source(str(self.blueprint_file)),
            target=target,
            line_id="feature-a",
        )

        with self.assertRaisesRegex(DyroError, "非空"):
            apply_join_plan(plan)

        self.assertEqual(
            target.joinpath("notes.txt").read_text(encoding="utf-8"),
            "user-owned\n",
        )

    def test_join_dry_run_rejects_non_empty_target_without_writing(self) -> None:
        target = self.parent / "occupied-preview"
        target.mkdir()
        target.joinpath("notes.txt").write_text("user-owned\n", encoding="utf-8")
        stderr = StringIO()

        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            main(
                [
                    "join",
                    str(self.blueprint_file),
                    "--path",
                    str(target),
                    "--dry-run",
                ]
            )

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("非空", stderr.getvalue())
        self.assertEqual(sorted(path.name for path in target.iterdir()), ["notes.txt"])

    def test_join_cli_registers_the_workspace_in_an_isolated_home(self) -> None:
        target = self.parent / "registered-workspace"
        registry_home = self.parent / "registry"
        output = StringIO()

        with (
            patch.dict("os.environ", {"DYRO_HOME": str(registry_home)}),
            redirect_stdout(output),
        ):
            main(
                [
                    "join",
                    str(self.blueprint_file),
                    "--path",
                    str(target),
                    "--yes",
                    "--default",
                ]
            )

        registry = registry_home.joinpath("workspaces.json").read_text(encoding="utf-8")
        self.assertIn('"default": "acme-platform"', registry)
        self.assertIn(str(target), registry)
        self.assertIn("仓库：2/2 clean", output.getvalue())
        self.assertIn("下一步：dyro", output.getvalue())

    def test_join_cli_reports_actual_clean_repository_count(self) -> None:
        target = self.parent / "workspace-with-wip"
        plan = build_join_plan(
            load_blueprint_source(str(self.blueprint_file)),
            target=target,
            line_id="feature-a",
        )
        config = apply_join_plan(plan)
        target.joinpath(
            "versions/feature-a", config.repositories["api"].mount, "README.md"
        ).write_text("local work\n", encoding="utf-8")
        output = StringIO()

        with redirect_stdout(output):
            main(
                [
                    "join",
                    str(self.blueprint_file),
                    "--path",
                    str(target),
                    "--yes",
                    "--no-register",
                ]
            )

        self.assertIn("仓库：1/2 clean", output.getvalue())

    def test_join_refuses_a_changed_blueprint_during_resume(self) -> None:
        target = self.parent / "workspace"
        first = build_join_plan(
            load_blueprint_source(str(self.blueprint_file)),
            target=target,
            line_id="feature-a",
        )
        apply_join_plan(first)
        self.blueprint_file.write_text(
            self.blueprint_file.read_text(encoding="utf-8").replace(
                'suggested_directory = "acme-platform"',
                'suggested_directory = "acme-platform-v2"',
            ),
            encoding="utf-8",
        )
        changed = build_join_plan(
            load_blueprint_source(str(self.blueprint_file)),
            target=target,
            line_id="feature-a",
        )

        with self.assertRaisesRegex(DyroError, "蓝图.*不一致"):
            apply_join_plan(changed)

    def test_join_refuses_a_symlinked_state_directory(self) -> None:
        target = self.parent / "workspace"
        victim = self.parent / "victim"
        target.mkdir()
        victim.mkdir()
        target.joinpath(".dyro").symlink_to(victim, target_is_directory=True)
        plan = build_join_plan(
            load_blueprint_source(str(self.blueprint_file)),
            target=target,
            line_id="feature-a",
        )

        with self.assertRaisesRegex(DyroError, "状态目录不能是符号链接"):
            apply_join_plan(plan)

        self.assertEqual(list(victim.iterdir()), [])

    def test_loads_blueprint_from_a_git_source(self) -> None:
        source = self.parent / "blueprints"
        source.mkdir()
        shell("git", "init", "-b", "main", cwd=source)
        shell("git", "config", "user.name", "Test User", cwd=source)
        shell("git", "config", "user.email", "test@example.com", cwd=source)
        source.joinpath("dyro-blueprint.toml").write_bytes(
            self.blueprint_file.read_bytes()
        )
        shell("git", "add", "dyro-blueprint.toml", cwd=source)
        shell("git", "commit", "-m", "chore: add blueprint", cwd=source)

        document = load_blueprint_source("git+file://" + str(source))

        self.assertEqual(document.blueprint.name, "acme-platform")
        self.assertEqual(document.source, "file://" + str(source))

    def test_local_directory_source_rejects_symlinked_path_components(self) -> None:
        source = self.parent / "blueprint-directory"
        external = self.parent / "external"
        source.mkdir()
        external.mkdir()
        external.joinpath("team.toml").write_bytes(self.blueprint_file.read_bytes())
        source.joinpath("config").symlink_to(external, target_is_directory=True)

        with self.assertRaisesRegex(DyroError, "不能经过符号链接"):
            load_blueprint_source(str(source), blueprint_file="config/team.toml")


if __name__ == "__main__":
    unittest.main()

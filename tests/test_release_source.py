from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "verify_release_source",
    ROOT / "tools" / "verify_release_source.py",
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _git(repository: Path, *args: str) -> None:
    subprocess.run(
        ("git", *args),
        cwd=repository,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


class ReleaseSourceVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = Path(self.temporary.name)
        _git(self.repository, "init", "-q", "-b", "main")
        _git(self.repository, "config", "user.name", "Dyro Test")
        _git(self.repository, "config", "user.email", "tests@example.invalid")
        (self.repository / "README.md").write_text("main\n", encoding="utf-8")
        _git(self.repository, "add", "README.md")
        _git(self.repository, "commit", "-qm", "initial")
        _git(self.repository, "tag", "v1.2.3")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_accepts_tag_checked_out_from_trusted_main(self) -> None:
        verified = MODULE.verify_release_source(
            repository=self.repository,
            release_tag="v1.2.3",
            trusted_ref="main",
        )
        self.assertEqual(verified["release_tag"], "v1.2.3")
        self.assertEqual(len(verified["tag_commit"]), 40)

    def test_rejects_tag_that_is_not_on_trusted_main(self) -> None:
        _git(self.repository, "switch", "-qc", "release-only")
        (self.repository / "README.md").write_text("release only\n", encoding="utf-8")
        _git(self.repository, "add", "README.md")
        _git(self.repository, "commit", "-qm", "release only")
        _git(self.repository, "tag", "v1.2.4")
        with self.assertRaisesRegex(MODULE.ReleaseSourceError, "ancestor"):
            MODULE.verify_release_source(
                repository=self.repository,
                release_tag="v1.2.4",
                trusted_ref="main",
            )

    def test_rejects_checkout_that_does_not_match_the_tag(self) -> None:
        (self.repository / "README.md").write_text("new main\n", encoding="utf-8")
        _git(self.repository, "add", "README.md")
        _git(self.repository, "commit", "-qm", "new main")
        with self.assertRaisesRegex(MODULE.ReleaseSourceError, "does not equal"):
            MODULE.verify_release_source(
                repository=self.repository,
                release_tag="v1.2.3",
                trusted_ref="main",
            )

    def test_publish_workflow_requires_successful_exact_sha_ci_gate(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "pypi-publish.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("actions: read", workflow)
        self.assertIn("actions/workflows/ci.yml/runs", workflow)
        self.assertIn('-f "head_sha=${release_sha}"', workflow)
        self.assertIn("-f event=push", workflow)
        self.assertIn('"${status}" == "completed"', workflow)
        self.assertIn('"${conclusion}" != "success"', workflow)
        self.assertIn("ci-gate-run.tsv", workflow)
        self.assertNotIn("dyro-bridge-zero-effect-evidence", workflow)
        self.assertNotIn(
            "Agent Bridge source/wheel/sdist gate (Ubuntu 24.04)", workflow
        )
        self.assertNotIn("bridge-gate-run.tsv", workflow)
        self.assertIn("dyro-dispatch','SKILL.md", workflow)
        self.assertIn("dyro-dispatch','agents','openai.yaml", workflow)
        self.assertIn("dyro-executor','SKILL.md", workflow)
        self.assertIn("dyro-board','SKILL.md", workflow)

    def test_ci_no_longer_ships_agent_bridge_zero_effect_gate(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("bridge-zero-effects", workflow)
        self.assertNotIn("dyro-bridge-zero-effect-evidence", workflow)
        self.assertNotIn("verify_bridge_zero_effects.py", workflow)
        self.assertNotIn("Agent Bridge source/wheel/sdist gate", workflow)
        self.assertIn("dyro-dispatch','SKILL.md", workflow)
        self.assertIn("dyro-dispatch','agents','openai.yaml", workflow)
        self.assertIn("dyro-executor','SKILL.md", workflow)
        self.assertIn("dyro-board','SKILL.md", workflow)
        self.assertGreaterEqual(workflow.count("verify_bundle_stranger.py"), 2)
        self.assertGreaterEqual(
            workflow.count("find_spec('dyro.bridge') is None"), 2
        )

    def test_default_wheel_stays_bridge_free(self) -> None:
        metadata = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertNotIn("dyro-bridge", metadata)
        self.assertNotIn("dyro-mcp", metadata)
        self.assertNotIn('"dyro.bridge"', metadata)
        self.assertNotIn("[project.optional-dependencies.mcp]", metadata)
        self.assertNotIn("[mcp]", metadata)
        self.assertIn('dyro = "dyro.cli:main"', metadata)

from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dyro.cli import main
from dyro.errors import ValidationError
from dyro.terminology import load_terminology_policy, scan_terminology

from .support import shell


class TerminologyPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="dyro-terminology-")
        self.base = Path(self.temporary.name)
        self.root = self.base / "workspace"
        self.root.mkdir()
        shell("git", "init", "-b", "main", cwd=self.root)
        shell("git", "config", "user.name", "Test User", cwd=self.root)
        shell("git", "config", "user.email", "test@example.com", cwd=self.root)
        self.root.joinpath("README.md").write_text("safe text\n", encoding="utf-8")
        shell("git", "add", "README.md", cwd=self.root)
        shell("git", "commit", "-m", "chore: initial", cwd=self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_policy_must_stay_outside_the_repository_and_has_stable_hash(self) -> None:
        external_policy = self.base / "policy.txt"
        external_policy.write_text("marker-two\nmarker-one\n", encoding="utf-8")
        policy = load_terminology_policy(self.root, policy_file=external_policy)
        reordered = load_terminology_policy(
            self.root,
            environ={"DYRO_TERMINOLOGY_DENYLIST": "marker-one\nmarker-two\n"},
        )
        self.assertEqual(policy.input_hash, reordered.input_hash)
        self.assertEqual(policy.terms, ("marker-two", "marker-one"))

        with self.assertRaisesRegex(ValidationError, "仓库外"):
            load_terminology_policy(
                self.root,
                policy_file=self.root / "policy.txt",
            )

    def test_scan_covers_workspace_branch_diff_and_commit_candidates_without_echoing_terms(self) -> None:
        external_policy = self.base / "policy.txt"
        external_policy.write_text("marker-blocked\n", encoding="utf-8")
        policy = load_terminology_policy(self.root, policy_file=external_policy)
        self.root.joinpath("marker-blocked-notes.md").write_text(
            "marker-blocked\n",
            encoding="utf-8",
        )
        shell("git", "checkout", "-b", "feature/marker-blocked", cwd=self.root)

        result = scan_terminology(
            self.root,
            policy,
            base_ref="HEAD",
            candidate_messages=("candidate marker-blocked",),
        )

        self.assertGreaterEqual(result.scanned_sources, 5)
        self.assertEqual(result.policy_hash, policy.input_hash)
        self.assertTrue(any(item.startswith("workspace-file:") for item in result.violations))
        self.assertTrue(any(item.startswith("branch") for item in result.violations))
        self.assertTrue(any(item.startswith("candidate-message:1") for item in result.violations))
        self.assertNotIn("marker-blocked", str(result.as_dict()))

    def test_base_ref_cannot_be_parsed_as_a_git_write_option(self) -> None:
        external_policy = self.base / "policy.txt"
        external_policy.write_text("marker\n", encoding="utf-8")
        policy = load_terminology_policy(self.root, policy_file=external_policy)
        unexpected_output = self.base / "unexpected-output"

        with self.assertRaisesRegex(ValidationError, "基线"):
            scan_terminology(
                self.root,
                policy,
                base_ref=f"--output={unexpected_output}",
            )

        self.assertFalse(unexpected_output.exists())

    def test_policy_requires_one_external_input(self) -> None:
        with self.assertRaisesRegex(ValidationError, "未配置"):
            load_terminology_policy(self.root, environ={})
        with self.assertRaisesRegex(ValidationError, "只能指定一个"):
            load_terminology_policy(
                self.root,
                policy_file=self.base / "missing.txt",
                environ={"DYRO_TERMINOLOGY_DENYLIST": "marker"},
            )

    def test_cli_scans_a_git_repository_without_requiring_a_dyro_profile(self) -> None:
        output = io.StringIO()
        with patch.dict(os.environ, {"DYRO_TERMINOLOGY_DENYLIST": "marker-clear"}):
            with redirect_stdout(output):
                main(
                    [
                        "--root",
                        str(self.root),
                        "terminology",
                        "check",
                        "--base-ref",
                        "HEAD",
                    ]
                )
        result = json.loads(output.getvalue())
        self.assertEqual(result["policy_term_count"], 1)
        self.assertEqual(result["violations"], [])


if __name__ == "__main__":
    unittest.main()

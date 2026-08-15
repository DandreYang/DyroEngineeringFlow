from __future__ import annotations

import os
from pathlib import Path
import re
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]


def _changelog_status(version: str, changelog: str) -> str | None:
    heading = re.search(
        rf"(?m)^## {re.escape(version)} - (?P<status>Unreleased|\d{{4}}-\d{{2}}-\d{{2}})(?: |$)",
        changelog,
    )
    return heading.group("status") if heading is not None else None


def _assert_release_changelog(version: str, status: str | None, release_tag: str | None) -> None:
    if status is None:
        raise AssertionError("the package version must have a valid changelog entry")
    if release_tag is None:
        return
    if release_tag != f"v{version}":
        raise AssertionError("the release tag must equal the package version")
    if status == "Unreleased":
        raise AssertionError("a release must have a dated changelog entry")


class ReleaseMetadataTests(unittest.TestCase):
    def test_current_package_version_has_a_valid_changelog_entry(self) -> None:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        version = project["project"]["version"]
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

        _assert_release_changelog(version, _changelog_status(version, changelog), os.environ.get("DYRO_RELEASE_TAG"))

    def test_release_tag_rejects_an_unreleased_changelog_entry(self) -> None:
        with self.assertRaisesRegex(AssertionError, "dated changelog"):
            _assert_release_changelog("0.5.3", "Unreleased", "v0.5.3")

    def test_source_distribution_excludes_generated_python_bytecode(self) -> None:
        manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")

        self.assertIn("global-exclude *.py[cod]", manifest.splitlines())

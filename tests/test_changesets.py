from pathlib import Path
import os

from dyro.changesets import create_changeset, get_changeset, verify_changeset
from dyro.config import load
from dyro.process import git, require_ok
from dyro.workspace import create_line

from .support import WorkspaceCase


class ChangeSetTests(WorkspaceCase):
    def test_changeset_verification_does_not_refresh_the_index(self) -> None:
        config = load(self.root)
        create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        changeset = create_changeset(
            config, changeset_id="alpha-ready", line_id="alpha"
        )
        delivery = self.root / "versions/alpha/services/api"
        index = Path(
            require_ok(
                git(
                    delivery,
                    "rev-parse",
                    "--path-format=absolute",
                    "--git-path",
                    "index",
                ),
                "读取 worktree index",
            ).stdout.strip()
        )
        tracked = delivery / "README.md"
        tracked_stat = tracked.stat()
        os.utime(
            tracked,
            ns=(tracked_stat.st_atime_ns, tracked_stat.st_mtime_ns + 2_000_000_000),
        )
        before_bytes = index.read_bytes()
        before_mtime = index.stat().st_mtime_ns

        findings = verify_changeset(config, changeset)

        self.assertFalse(any(item.startswith("FAIL") for item in findings), findings)
        self.assertEqual(index.read_bytes(), before_bytes)
        self.assertEqual(index.stat().st_mtime_ns, before_mtime)
        self.assertFalse(index.with_name("index.lock").exists())

    def test_changeset_pins_and_verifies_delivery_line_heads(self) -> None:
        config = load(self.root)
        create_line(config, line_id="alpha", branch="feat/alpha", base="main")

        changeset = create_changeset(config, changeset_id="alpha-ready", line_id="alpha")

        self.assertEqual(changeset.id, "alpha-ready")
        self.assertEqual(get_changeset(config, "alpha-ready").heads, changeset.heads)
        findings = verify_changeset(config, changeset)
        self.assertFalse(any(finding.startswith("FAIL") for finding in findings), findings)

    def test_changeset_verify_rejects_dirty_delivery_line(self) -> None:
        config = load(self.root)
        create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        changeset = create_changeset(config, changeset_id="alpha-ready", line_id="alpha")
        delivery_repository = self.root / "versions/alpha/services/api"
        delivery_repository.joinpath("UNCOMMITTED.txt").write_text("dirty\n", encoding="utf-8")

        findings = verify_changeset(config, changeset)

        self.assertIn("FAIL api: delivery-line repository is dirty", findings)

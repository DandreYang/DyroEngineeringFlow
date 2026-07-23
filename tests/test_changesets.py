from dyro.changesets import create_changeset, get_changeset, verify_changeset
from dyro.config import load
from dyro.workspace import create_line

from .support import WorkspaceCase


class ChangeSetTests(WorkspaceCase):
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

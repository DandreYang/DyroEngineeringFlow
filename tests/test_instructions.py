from dyro.config import load
from dyro.instructions import seed_configured_overlays, seed_workspace_overlay
from dyro.workspace import (
    create_line,
    doctor,
    is_missing_origin_finding,
    line_repository_path,
    line_root,
)

from .support import WorkspaceCase


class OverlayInstructionTests(WorkspaceCase):
    def test_old_workspace_doctors_with_warn_not_fail_when_overlay_files_missing(
        self,
    ) -> None:
        config = load(self.root)
        findings = doctor(config)
        self.assertTrue(
            any(
                item.startswith("WARN overlay 缺少") and "AGENTS.md" in item
                for item in findings
            ),
            findings,
        )
        self.assertTrue(any("dyro host seed" in item for item in findings), findings)
        self.assertFalse(any(item.startswith("FAIL") for item in findings), findings)
        self.assertFalse((self.root / "AGENTS.md").exists())
        self.assertFalse((self.root / "CLAUDE.md").exists())

    def test_seed_is_idempotent_and_does_not_overwrite_hand_edited_agents(
        self,
    ) -> None:
        first = seed_workspace_overlay(self.root)
        self.assertEqual(first.written, ("AGENTS.md", "CLAUDE.md"))
        self.assertEqual(first.skipped, ())
        edited = "hand-edited overlay\n"
        (self.root / "AGENTS.md").write_text(edited, encoding="utf-8")
        second = seed_workspace_overlay(self.root)
        self.assertEqual(second.written, ())
        self.assertEqual(second.skipped, ("AGENTS.md", "CLAUDE.md"))
        self.assertEqual((self.root / "AGENTS.md").read_text(encoding="utf-8"), edited)
        findings = doctor(load(self.root))
        self.assertFalse(
            any(item.startswith("WARN overlay 缺少") for item in findings), findings
        )
        self.assertFalse(any(item.startswith("FAIL") for item in findings), findings)

        force = seed_workspace_overlay(self.root, force=True)
        self.assertEqual(force.written, ("AGENTS.md", "CLAUDE.md"))
        self.assertIn(
            "Dyro overlay", (self.root / "AGENTS.md").read_text(encoding="utf-8")
        )
        self.assertEqual(
            (self.root / "AGENTS.md").read_text(encoding="utf-8"),
            (self.root / "CLAUDE.md").read_text(encoding="utf-8"),
        )

    def test_host_seed_writes_line_personas_outside_git_worktrees(self) -> None:
        config = load(self.root)
        line = create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        overlay = line_root(config, line)
        worktree = line_repository_path(config, line, "api")
        (overlay / "AGENTS.md").write_text("line-hand-edit\n", encoding="utf-8")
        workspace, lines = seed_configured_overlays(config)
        self.assertEqual(workspace.written, ("AGENTS.md", "CLAUDE.md"))
        self.assertEqual(dict(lines)["alpha"].skipped, ("AGENTS.md", "CLAUDE.md"))
        self.assertEqual(
            (overlay / "AGENTS.md").read_text(encoding="utf-8"), "line-hand-edit\n"
        )
        self.assertFalse((worktree / "AGENTS.md").exists())
        self.assertFalse((worktree / "CLAUDE.md").exists())
        self.assertFalse((self.anchor / "AGENTS.md").exists())
        findings = doctor(load(self.root))
        failures = [item for item in findings if item.startswith("FAIL")]
        self.assertTrue(
            all(is_missing_origin_finding(item) for item in failures), findings
        )
        self.assertFalse(
            any(item.startswith("WARN overlay 缺少") for item in findings), findings
        )

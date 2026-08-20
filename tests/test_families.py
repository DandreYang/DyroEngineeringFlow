from __future__ import annotations

import unittest

from dyro.config import load
from dyro.console.families import family_cards, family_payload
from dyro.console.read_model import workspace_envelope
from dyro.families import family_graph, family_members
from dyro.observations import capture_workspace_read_snapshot
from dyro.workspace import create_line, spawn_line

from .support import WorkspaceCase


class OneLevelFamilyTests(WorkspaceCase):
    def setUp(self) -> None:
        super().setUp()
        self.config = load(self.root)
        create_line(self.config, line_id="core", branch="feat/core", base="main")
        spawn_line(self.config, "core", "pay")
        spawn_line(self.config, "core_pay", "fix")

    def test_parent_is_projected_on_the_line_dto(self) -> None:
        snapshot = capture_workspace_read_snapshot(self.config)
        by_id = {item.id: item.parent for item in snapshot.lines}
        self.assertEqual(by_id["core"], "")
        self.assertEqual(by_id["core_pay"], "core")
        self.assertEqual(by_id["core_pay_fix"], "core_pay")
        envelope = workspace_envelope(snapshot)
        projected = {item["id"]: item["parent"] for item in envelope["data"]["lines"]}
        self.assertEqual(projected["core"], "")
        self.assertEqual(projected["core_pay"], "core")
        self.assertEqual(projected["core_pay_fix"], "core_pay")

    def test_family_is_one_level_and_includes_operator(self) -> None:
        lines = [
            {"id": "core", "parent": ""},
            {"id": "core_pay", "parent": "core"},
            {"id": "core_pay_fix", "parent": "core_pay"},
        ]
        self.assertEqual(family_members(lines, "core"), ("core", "core_pay", "operator"))
        self.assertEqual(
            family_members(lines, "core_pay"),
            ("core_pay", "core_pay_fix", "operator"),
        )
        core = family_graph(lines, "core")
        self.assertEqual(core["members"], ["core", "core_pay", "operator"])
        self.assertEqual(core["edges"], [{"from": "core", "to": "core_pay", "kind": "parent"}])
        self.assertNotIn("core_pay_fix", core["members"])
        pay = family_graph(lines, "core_pay")
        self.assertIn("core_pay_fix", pay["members"])
        self.assertNotIn("core", pay["members"])

    def test_family_cards_and_payload_use_direct_children_only(self) -> None:
        snapshot = capture_workspace_read_snapshot(self.config)
        envelope = workspace_envelope(snapshot)
        lines = envelope["data"]["lines"]
        tasks = envelope["data"]["tasks"]
        cards = {item["parent"]: item for item in family_cards(lines, tasks)}
        self.assertEqual(cards["core"]["children"], ["core_pay"])
        self.assertEqual(cards["core_pay"]["children"], ["core_pay_fix"])
        payload = family_payload(lines, "core", tasks)
        self.assertEqual(payload["parent"], "core")
        self.assertEqual(payload["members"], ["core", "core_pay", "operator"])
        self.assertFalse(any(node["id"] == "core_pay_fix" for node in payload["nodes"]))


if __name__ == "__main__":
    unittest.main()

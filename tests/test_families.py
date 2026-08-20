from __future__ import annotations

import json
import unittest

from dyro.config import load
from dyro.console.families import apply_human_channel_post, family_cards, family_payload
from dyro.console.overview import ConsoleOverviewError
from dyro.console.read_model import workspace_envelope
from dyro.events import read_events
from dyro.families import (
    FamilyChannelError,
    ack_channel_message,
    channel_path,
    family_graph,
    family_members,
    family_unacked,
    infer_post_family,
    line_records,
    post_channel_message,
    read_acks,
    read_visible_channel,
)
from dyro.observations import capture_workspace_read_snapshot
from dyro.state import append_text
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


class FamilyChannelTests(WorkspaceCase):
    def setUp(self) -> None:
        super().setUp()
        self.config = load(self.root)
        create_line(self.config, line_id="core", branch="feat/core", base="main")
        spawn_line(self.config, "core", "pay")
        spawn_line(self.config, "core", "shop")
        spawn_line(self.config, "core_pay", "fix")

    def test_cousins_see_broadcast_but_not_each_others_directed_posts(self) -> None:
        broadcast = post_channel_message(
            self.config,
            sender="core_pay",
            kind="ask_sync",
            body="请同步父线",
        )
        directed = post_channel_message(
            self.config,
            sender="core_pay",
            kind="blocked",
            body="只要父线看见",
            recipient="core",
        )
        self.assertEqual(broadcast["family"], "core")
        self.assertEqual(directed["family"], "core")
        shop = {item["id"] for item in read_visible_channel(self.config, "core", viewer="core_shop")}
        pay = {item["id"] for item in read_visible_channel(self.config, "core", viewer="core_pay")}
        parent = {item["id"] for item in read_visible_channel(self.config, "core", viewer="core")}
        operator = {
            item["id"] for item in read_visible_channel(self.config, "core", viewer="operator")
        }
        self.assertIn(broadcast["id"], shop)
        self.assertNotIn(directed["id"], shop)
        self.assertIn(directed["id"], pay)
        self.assertIn(directed["id"], parent)
        self.assertIn(directed["id"], operator)
        events, _last = read_events(self.config)
        signals = [item for item in events if item["kind"] == "signal"]
        self.assertEqual(
            [item["facts"]["channel_id"] for item in signals],
            [broadcast["id"], directed["id"]],
        )

    def test_to_operator_uses_broadcast_default_family(self) -> None:
        lines = line_records(self.config)
        broadcast_family = infer_post_family(lines, "core_pay", "")
        self.assertEqual(infer_post_family(lines, "core_pay", "operator"), broadcast_family)
        self.assertEqual(broadcast_family, "core")
        self.assertEqual(infer_post_family(lines, "core_pay_fix", "operator"), "core_pay")
        self.assertEqual(
            infer_post_family(lines, "core_pay_fix", "operator"),
            infer_post_family(lines, "core_pay_fix", ""),
        )

        broadcast = post_channel_message(
            self.config,
            sender="core_pay",
            kind="blocked",
            body="默认家族",
        )
        to_operator = post_channel_message(
            self.config,
            sender="core_pay",
            kind="blocked",
            body="发给人类",
            recipient="operator",
        )
        self.assertEqual(to_operator["family"], broadcast["family"])
        self.assertEqual(to_operator["family"], "core")
        parent = {item["id"] for item in read_visible_channel(self.config, "core", viewer="core")}
        cousin = {
            item["id"] for item in read_visible_channel(self.config, "core", viewer="core_shop")
        }
        self.assertIn(to_operator["id"], parent)
        self.assertNotIn(to_operator["id"], cousin)

        grandchild = post_channel_message(
            self.config,
            sender="core_pay_fix",
            kind="blocked",
            body="发给人类",
            recipient="operator",
        )
        self.assertEqual(grandchild["family"], "core_pay")
        self.assertEqual(broadcast["id"], "msg_1")
        self.assertEqual(grandchild["id"], "msg_1")
        core_msg1 = next(
            item
            for item in read_visible_channel(self.config, "core")
            if item["id"] == "msg_1"
        )
        pay_msg1 = next(
            item
            for item in read_visible_channel(self.config, "core_pay")
            if item["id"] == "msg_1"
        )
        self.assertEqual(core_msg1["family"], "core")
        self.assertEqual(pay_msg1["family"], "core_pay")
        self.assertEqual(core_msg1["from"], "core_pay")
        self.assertEqual(pay_msg1["from"], "core_pay_fix")
        self.assertEqual(pay_msg1["id"], core_msg1["id"])

    def test_to_outside_family_is_rejected(self) -> None:
        with self.assertRaises(FamilyChannelError) as raised:
            post_channel_message(
                self.config,
                sender="core",
                kind="blocked",
                body="孙线不在本家族",
                recipient="core_pay_fix",
            )
        self.assertEqual(raised.exception.code, "FAMILY_TO_INVALID")

    def test_grandchild_channel_stays_off_the_grandparent_family(self) -> None:
        child = post_channel_message(
            self.config,
            sender="core_pay",
            kind="ask_sync",
            body="请看修复线",
            recipient="core_pay_fix",
        )
        self.assertEqual(child["family"], "core_pay")
        core_ids = {item["id"] for item in read_visible_channel(self.config, "core")}
        pay_ids = {item["id"] for item in read_visible_channel(self.config, "core_pay")}
        self.assertNotIn(child["id"], core_ids)
        self.assertIn(child["id"], pay_ids)

    def test_colliding_msg_ids_are_family_scoped(self) -> None:
        core_row = post_channel_message(
            self.config,
            sender="core_pay",
            kind="blocked",
            body="父族广播",
        )
        pay_row = post_channel_message(
            self.config,
            sender="core_pay_fix",
            kind="blocked",
            body="发给人类",
            recipient="operator",
        )
        self.assertEqual(core_row["id"], "msg_1")
        self.assertEqual(pay_row["id"], "msg_1")
        self.assertEqual(core_row["family"], "core")
        self.assertEqual(pay_row["family"], "core_pay")

        with self.assertRaises(FamilyChannelError) as ambiguous:
            ack_channel_message(self.config, "msg_1")
        self.assertEqual(ambiguous.exception.code, "CHANNEL_MESSAGE_AMBIGUOUS")

        http_ack = apply_human_channel_post(
            self.config, "core_pay", {"kind": "ack", "ack_id": "msg_1"}
        )
        self.assertEqual(http_ack["id"], "msg_1")
        self.assertEqual(read_acks(self.config, "core_pay"), frozenset({"msg_1"}))
        self.assertEqual(read_acks(self.config, "core"), frozenset())

        scoped = ack_channel_message(self.config, "msg_1", family="core")
        self.assertEqual(scoped["family"], "core")
        self.assertEqual(read_acks(self.config, "core"), frozenset({"msg_1"}))
        self.assertEqual(read_acks(self.config, "core_pay"), frozenset({"msg_1"}))

        with self.assertRaises(ConsoleOverviewError) as wrong_family:
            apply_human_channel_post(
                self.config, "core_shop", {"kind": "ack", "ack_id": "msg_1"}
            )
        self.assertEqual(wrong_family.exception.code, "CHANNEL_MESSAGE_NOT_FOUND")

        append_text(
            channel_path(self.config, "core_shop"),
            json.dumps(
                {
                    "id": "msg_1",
                    "seq": 1,
                    "at": "2026-08-20T12:00:00Z",
                    "family": "core_shop",
                    "from": "core_shop",
                    "to": "",
                    "kind": "ask_sync",
                    "body": "半写入",
                    "retracts": "",
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n",
        )
        with self.assertRaises(FamilyChannelError) as unpaired:
            read_visible_channel(self.config, "core_shop", viewer="core_shop")
        self.assertEqual(unpaired.exception.code, "CHANNEL_LOG_INCONSISTENT")
        still_core = {
            item["id"] for item in read_visible_channel(self.config, "core")
        }
        self.assertIn("msg_1", still_core)

    def test_unpaired_channel_row_fails_closed_and_is_not_broadcast(self) -> None:
        append_text(
            channel_path(self.config, "core"),
            json.dumps(
                {
                    "id": "msg_1",
                    "seq": 1,
                    "at": "2026-08-20T12:00:00Z",
                    "family": "core",
                    "from": "core_pay",
                    "to": "",
                    "kind": "ask_sync",
                    "body": "半写入",
                    "retracts": "",
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n",
        )
        with self.assertRaises(FamilyChannelError) as raised:
            read_visible_channel(self.config, "core", viewer="core_shop")
        self.assertEqual(raised.exception.code, "CHANNEL_LOG_INCONSISTENT")
        events, _last = read_events(self.config)
        self.assertFalse(any(item["kind"] == "signal" for item in events))

    def test_operator_post_rejects_non_human_kinds(self) -> None:
        with self.assertRaises(FamilyChannelError) as raised:
            post_channel_message(
                self.config,
                sender="operator",
                kind="blocked",
                body="人类不能发阻塞",
                family="core",
            )
        self.assertEqual(raised.exception.code, "FAMILY_POST_FORBIDDEN")
        with self.assertRaises(ConsoleOverviewError) as http:
            apply_human_channel_post(
                self.config, "core", {"kind": "shipped", "body": "不能发"}
            )
        self.assertEqual(http.exception.code, "FAMILY_POST_FORBIDDEN")

    def test_dry_run_post_and_ack_write_nothing(self) -> None:
        planned = post_channel_message(
            self.config,
            sender="core",
            kind="decision",
            body="先看一眼",
            dry_run=True,
        )
        self.assertTrue(planned["dry_run"])
        self.assertFalse(channel_path(self.config, "core").exists())
        written = post_channel_message(
            self.config,
            sender="core",
            kind="decision",
            body="先看一眼",
        )
        ack = ack_channel_message(self.config, written["id"], dry_run=True)
        self.assertTrue(ack["dry_run"])
        unread = family_unacked(self.config)
        self.assertEqual(unread["count"], 1)
        self.assertEqual(unread["kind"], "decision")
        self.assertEqual(unread["family"], "core")


if __name__ == "__main__":
    unittest.main()

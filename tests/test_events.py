from __future__ import annotations

from datetime import datetime, timezone
import json
import unittest

from dyro.config import load
from dyro.console.events import decode_event_cursor, event_page
from dyro.console.overview import ConsoleOverviewError
from dyro.events import EventLogError, append_event, read_events
from dyro.tasks import set_status, task_template
from dyro.workspace import create_line, spawn_line

from .support import WorkspaceCase


class WorkspaceEventLogTests(WorkspaceCase):
    def setUp(self) -> None:
        super().setUp()
        self.config = load(self.root)
        self.clock = lambda: datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)

    def test_append_and_read_are_ordered_and_typed(self) -> None:
        first = append_event(
            self.config,
            kind="spawn",
            actor="core",
            subject="core_pay",
            family="core",
            facts={"parent": "core", "child": "core_pay"},
            clock=self.clock,
        )
        second = append_event(
            self.config,
            kind="merge",
            actor="core_pay",
            subject="core",
            family="core",
            facts={"parent": "core", "child": "core_pay"},
            clock=self.clock,
        )

        page, last = read_events(self.config, after_seq=0, limit=50)
        self.assertEqual(last, 2)
        self.assertEqual([item["id"] for item in page], ["evt_1", "evt_2"])
        self.assertEqual(first["kind"], "spawn")
        self.assertEqual(second["kind"], "merge")
        self.assertEqual(page[0]["facts"]["child"], "core_pay")
        after_first, _last = read_events(self.config, after_seq=1, limit=50)
        self.assertEqual([item["id"] for item in after_first], ["evt_2"])

    def test_truncated_log_fails_closed_on_read_and_write(self) -> None:
        append_event(
            self.config,
            kind="sync",
            actor="core",
            subject="core_pay",
            family="core",
            facts={"parent": "core", "child": "core_pay"},
            clock=self.clock,
        )
        path = self.root / ".dyro" / "events.jsonl"
        path.write_text(path.read_text(encoding="utf-8").rstrip("\n"), encoding="utf-8")

        with self.assertRaises(EventLogError) as raised:
            read_events(self.config)
        self.assertEqual(raised.exception.code, "EVENT_LOG_INVALID")
        with self.assertRaises(EventLogError) as write_error:
            append_event(
                self.config,
                kind="spawn",
                actor="core",
                subject="core_pay_fix",
                family="core",
                facts={"parent": "core", "child": "core_pay_fix"},
                clock=self.clock,
            )
        self.assertEqual(write_error.exception.code, "EVENT_LOG_INVALID")

    def test_write_refuses_a_non_file_log(self) -> None:
        path = self.root / ".dyro" / "events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.mkdir()
        with self.assertRaises(EventLogError) as raised:
            append_event(
                self.config,
                kind="host_seed",
                actor="operator",
                subject="overlay",
                family="",
                facts={"written": 1},
                clock=self.clock,
            )
        self.assertEqual(raised.exception.code, "EVENT_LOG_INVALID")

    def test_spawn_writes_an_event_and_dry_run_does_not(self) -> None:
        create_line(self.config, line_id="core", branch="feat/core", base="main")
        spawn_line(self.config, "core", "pay", dry_run=True)
        self.assertFalse((self.root / ".dyro" / "events.jsonl").exists())

        spawn_line(self.config, "core", "pay")
        page, last = read_events(self.config)
        self.assertEqual(last, 1)
        self.assertEqual(page[0]["kind"], "spawn")
        self.assertEqual(page[0]["facts"]["parent"], "core")
        self.assertEqual(page[0]["facts"]["child"], "core_pay")

    def test_task_status_writes_an_event(self) -> None:
        create_line(self.config, line_id="core", branch="feat/core", base="main")
        task = self.config.task_specs_dir / "TASK-A"
        task.mkdir(parents=True)
        task.joinpath("task.toml").write_text(
            task_template("TASK-A", "Prepare release", "core", "api", "services/api"),
            encoding="utf-8",
        )
        task.joinpath("handoff.md").write_text("# handoff\n", encoding="utf-8")
        from dyro.tasks import load_task

        item = load_task(self.config, "TASK-A")
        set_status(self.config, item, "assigned")
        page, _last = read_events(self.config)
        self.assertEqual(page[0]["kind"], "task_status")
        self.assertEqual(page[0]["facts"]["to_status"], "assigned")
        self.assertNotIn("prompt", json.dumps(page[0]))

    def test_write_refuses_path_or_unknown_kind(self) -> None:
        with self.assertRaises(EventLogError) as path_error:
            append_event(
                self.config,
                kind="spawn",
                actor="core",
                subject="core_pay",
                family="core",
                facts={"parent": "/tmp/core"},
                clock=self.clock,
            )
        self.assertEqual(path_error.exception.code, "EVENT_WRITE_INVALID")
        with self.assertRaises(EventLogError) as kind_error:
            append_event(
                self.config,
                kind="merge_main",
                actor="core",
                subject="core_pay",
                family="core",
                facts={"parent": "core", "child": "core_pay"},
                clock=self.clock,
            )
        self.assertEqual(kind_error.exception.code, "EVENT_WRITE_INVALID")
        self.assertFalse((self.root / ".dyro" / "events.jsonl").exists())

    def test_hmac_after_cursor_rejects_tampering(self) -> None:
        append_event(
            self.config,
            kind="spawn",
            actor="core",
            subject="core_pay",
            family="core",
            facts={"parent": "core", "child": "core_pay"},
            clock=self.clock,
        )
        secret = b"k" * 32
        page = event_page(self.config, secret=secret, after=None, limit=50)
        cursor = page["next_cursor"]
        self.assertTrue(cursor)
        after_seq, event_id = decode_event_cursor(secret, cursor)
        self.assertEqual(after_seq, 1)
        self.assertEqual(event_id, "evt_1")
        resumed = event_page(self.config, secret=secret, after=cursor, limit=50)
        self.assertEqual(resumed["events"], [])
        with self.assertRaisesRegex(ConsoleOverviewError, "EVENT_CURSOR_INVALID"):
            event_page(self.config, secret=secret, after=cursor[:-1] + "x", limit=50)
        path = self.root / ".dyro" / "events.jsonl"
        path.write_text("", encoding="utf-8")
        with self.assertRaisesRegex(ConsoleOverviewError, "EVENT_CURSOR_INVALID"):
            event_page(self.config, secret=secret, after=cursor, limit=50)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from datetime import datetime, timezone
import json
import unittest

from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from unittest.mock import patch

from dyro.cli import build_parser, cmd_host_seed
from dyro.config import load
from dyro.console.events import decode_event_cursor, event_page, project_event
from dyro.console.overview import ConsoleOverviewError
from dyro.events import EventLogError, append_event, read_events
from dyro.process import Result
from dyro.tasks import _execute_task_agent, load_task, set_status, task_template
from dyro.workspace import create_line, line_repository_path, merge_line, spawn_line, sync_line

from .support import WorkspaceCase, publish_origin_branch, shell


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
        after_seq, event_id, digest = decode_event_cursor(secret, cursor)
        self.assertEqual(after_seq, 1)
        self.assertEqual(event_id, "evt_1")
        self.assertEqual(len(digest), 64)
        resumed = event_page(self.config, secret=secret, after=cursor, limit=50)
        self.assertEqual(resumed["events"], [])
        # Unpadded base64 can treat the final character as unused bits; flip
        # a body character so the HMAC input actually changes.
        with self.assertRaisesRegex(ConsoleOverviewError, "EVENT_CURSOR_INVALID"):
            event_page(self.config, secret=secret, after="x" + cursor[1:], limit=50)
        path = self.root / ".dyro" / "events.jsonl"
        path.write_text("", encoding="utf-8")
        with self.assertRaisesRegex(ConsoleOverviewError, "EVENT_CURSOR_INVALID"):
            event_page(self.config, secret=secret, after=cursor, limit=50)

    def test_hmac_after_cursor_rejects_replaced_same_seq_row(self) -> None:
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
        cursor = event_page(self.config, secret=secret, after=None, limit=50)["next_cursor"]
        self.assertTrue(cursor)
        replacement = {
            "actor": "core",
            "at": "2026-08-20T12:00:01Z",
            "family": "core",
            "facts": {"parent": "core", "child": "core_pay"},
            "id": "evt_1",
            "kind": "sync",
            "seq": 1,
            "subject": "core_pay",
        }
        path = self.root / ".dyro" / "events.jsonl"
        path.write_text(
            json.dumps(replacement, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ConsoleOverviewError, "EVENT_CURSOR_INVALID"):
            event_page(self.config, secret=secret, after=cursor, limit=50)

    def test_project_event_drops_path_prompt_and_nested_facts(self) -> None:
        projected = project_event(
            {
                "seq": 1,
                "id": "evt_1",
                "kind": "spawn",
                "at": "2026-08-20T12:00:00Z",
                "actor": "core",
                "subject": "core_pay",
                "family": "core",
                "facts": {
                    "parent": "core",
                    "child": "core_pay",
                    "path": "/tmp/secret",
                    "prompt": "please prepare notes without any slash token",
                    "nested": {"argv": ["dyro", "next"]},
                },
            }
        )
        self.assertEqual(projected["facts"], {"parent": "core", "child": "core_pay"})

    def _event_kinds(self) -> list[str]:
        path = self.root / ".dyro" / "events.jsonl"
        if not path.is_file():
            return []
        return [
            json.loads(line)["kind"]
            for line in path.read_text(encoding="utf-8").splitlines()
            if line
        ]

    def _parent_and_child(self):
        publish_origin_branch(self.anchor, "feat/core")
        parent = create_line(self.config, line_id="core", branch="feat/core", base="main")
        child = spawn_line(self.config, "core", "pay")
        return parent, child

    def test_merge_and_sync_write_events_dry_run_and_failed_preflight_do_not(
        self,
    ) -> None:
        from dyro.errors import DyroError

        parent, child = self._parent_and_child()
        self.assertEqual(self._event_kinds(), ["spawn"])
        child_wt = line_repository_path(self.config, child, "api")
        (child_wt / "child.txt").write_text("from child\n", encoding="utf-8")
        shell("git", "add", "child.txt", cwd=child_wt)
        shell("git", "commit", "-m", "feat: child work", cwd=child_wt)

        merge_line(self.config, child.id, parent.id, dry_run=True)
        self.assertEqual(self._event_kinds(), ["spawn"])

        parent_wt = line_repository_path(self.config, parent, "api")
        (parent_wt / "dirty.txt").write_text("pending\n", encoding="utf-8")
        with self.assertRaisesRegex(DyroError, "不干净"):
            merge_line(self.config, child.id, parent.id)
        self.assertEqual(self._event_kinds(), ["spawn"])
        (parent_wt / "dirty.txt").unlink()

        merge_line(self.config, child.id, parent.id)
        self.assertEqual(self._event_kinds(), ["spawn", "merge"])

        sync_line(self.config, child.id, dry_run=True)
        self.assertEqual(self._event_kinds(), ["spawn", "merge"])
        sync_line(self.config, child.id)
        self.assertEqual(self._event_kinds(), ["spawn", "merge", "sync"])

    def test_dispatch_start_and_end_write_events_dry_run_does_not(self) -> None:
        create_line(self.config, line_id="core", branch="feat/core", base="main")
        task_dir = self.config.task_specs_dir / "TASK-A"
        task_dir.mkdir(parents=True)
        task_dir.joinpath("task.toml").write_text(
            task_template("TASK-A", "Prepare release", "core", "api", "services/api"),
            encoding="utf-8",
        )
        task_dir.joinpath("handoff.md").write_text("# handoff\n", encoding="utf-8")
        task = load_task(self.config, "TASK-A")
        result = Result(("dyro", "task-dispatch", "codex", "TASK-A"), 0, "")
        with (
            patch("dyro.task_dispatch.is_dispatch_write_ready", return_value=True),
            patch(
                "dyro.task_dispatch.run_task_bound_dispatch",
                return_value=result,
            ),
        ):
            _execute_task_agent(
                self.config,
                task,
                workspace=self.root,
                prompt="do-work",
                log_name="executor.log",
                dry_run=True,
            )
            self.assertEqual(self._event_kinds(), [])
            _execute_task_agent(
                self.config,
                task,
                workspace=self.root,
                prompt="do-work",
                log_name="executor.log",
                dry_run=False,
            )
        page, _last = read_events(self.config)
        self.assertEqual([item["kind"] for item in page], ["dispatch", "dispatch"])
        self.assertEqual(page[0]["facts"]["phase"], "start")
        self.assertEqual(page[1]["facts"]["phase"], "end")
        self.assertEqual(page[1]["facts"]["status"], "idle")

    def test_host_seed_writes_an_event_and_dry_run_does_not(self) -> None:
        parser = build_parser()
        dry = parser.parse_args(["--root", str(self.root), "host", "seed", "--dry-run"])
        with redirect_stdout(StringIO()):
            cmd_host_seed(dry)
        self.assertEqual(self._event_kinds(), [])
        seeded = parser.parse_args(["--root", str(self.root), "host", "seed"])
        with redirect_stdout(StringIO()):
            cmd_host_seed(seeded)
        page, _last = read_events(self.config)
        self.assertEqual(page[0]["kind"], "host_seed")
        self.assertGreater(page[0]["facts"]["written"], 0)

    def test_apply_supervised_wave_writes_an_event(self) -> None:
        from dyro.continuation.store import create_objective
        from dyro.continuation.supervision import apply_supervised_wave, build_supervised_wave
        from dyro.host import compile_hosts

        compile_hosts(self.config)
        create_line(self.config, line_id="alpha", branch="feat/alpha", base="main")
        task_dir = self.config.task_specs_dir / "TASK-A"
        task_dir.mkdir(parents=True)
        task_dir.joinpath("task.toml").write_text(
            task_template("TASK-A", "Task A", "alpha", "api", "services/api").replace(
                'agent = "codex"', 'agent = "noop"'
            ),
            encoding="utf-8",
        )
        task_dir.joinpath("handoff.md").write_text("# handoff\n", encoding="utf-8")
        task_dir.joinpath("receipt.md").write_text("result: DONE\n", encoding="utf-8")
        create_objective(
            self.config,
            '''schema_version = 1
id = "release"
title = "Release"
line = "alpha"
targets = ["TASK-A"]

[continuation]
requested_mode = "supervised"
operations = ["execute", "review"]

[budget]
max_actions = 20
max_attempts_per_task = 2
max_failures = 3
max_no_progress_cycles = 2
max_parallel = 1
''',
        )
        now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
        wave = build_supervised_wave(self.config, "release", clock=lambda: now)
        apply_supervised_wave(self.config, wave, clock=lambda: now)
        kinds = self._event_kinds()
        self.assertIn("objective_wave", kinds)
        page, _last = read_events(self.config)
        wave_events = [item for item in page if item["kind"] == "objective_wave"]
        self.assertEqual(wave_events[0]["facts"]["mode"], "apply")


if __name__ == "__main__":
    unittest.main()

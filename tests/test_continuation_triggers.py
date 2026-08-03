from __future__ import annotations

from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
import json
import unittest

from dyro.cli import build_parser
from dyro.errors import DyroError
from dyro.continuation.models import TriggerState
from dyro.continuation.triggers import (
    ProviderDescriptor,
    TriggerConfig,
    TriggerErrorKind,
    TriggerKind,
    TriggerProbeInput,
    next_probe_schedule,
    parse_provider_observation,
    probe_builtin,
)


class ContinuationTriggerTests(unittest.TestCase):
    def test_builtin_time_task_decision_and_manual_signals_only_observe(self) -> None:
        now = datetime(2026, 8, 4, 10, 0, tzinfo=timezone.utc)
        time_due = probe_builtin(
            TriggerProbeInput(
                config=TriggerConfig("wake", TriggerKind.TIME_DUE, not_before=now),
                now=now,
            )
        )
        task_change = probe_builtin(
            TriggerProbeInput(
                config=TriggerConfig("task", TriggerKind.TASK_STATE),
                now=now,
                current_facts=(("TASK-A", "review"),),
                previous_facts=(("TASK-A", "backlog"),),
            )
        )
        unresolved = probe_builtin(
            TriggerProbeInput(
                config=TriggerConfig("decision", TriggerKind.DECISION_STATE),
                now=now,
                current_facts=(("D-1", "open"),),
                previous_facts=(("D-1", "open"),),
            )
        )
        signal = probe_builtin(
            TriggerProbeInput(
                config=TriggerConfig("signal", TriggerKind.MANUAL_SIGNAL),
                now=now,
                manual_signal="operator-ready",
            )
        )

        self.assertEqual(time_due.state, TriggerState.SATISFIED)
        self.assertEqual(task_change.state, TriggerState.SATISFIED)
        self.assertEqual(unresolved.state, TriggerState.PENDING)
        self.assertEqual(signal.state, TriggerState.SATISFIED)
        self.assertFalse(any(hasattr(item, "mutation") for item in (time_due, task_change, unresolved, signal)))

    def test_backoff_is_deterministic_and_changes_wake_immediately(self) -> None:
        now = datetime(2026, 8, 4, 10, 0, tzinfo=timezone.utc)
        config = TriggerConfig("poll", TriggerKind.TIME_DUE, min_interval_seconds=10, max_interval_seconds=80)
        pending = probe_builtin(TriggerProbeInput(config=config, now=now))
        first = next_probe_schedule(config, pending, unchanged_cycles=1, now=now)
        repeat = next_probe_schedule(config, pending, unchanged_cycles=1, now=now)
        changed = next_probe_schedule(
            config,
            probe_builtin(TriggerProbeInput(config=config, now=now, manual_signal="n/a")),
            unchanged_cycles=4,
            now=now,
            changed=True,
        )

        self.assertEqual(first, repeat)
        self.assertGreater(first.next_probe_at, now)
        self.assertLessEqual((first.next_probe_at - now).total_seconds(), 80)
        self.assertEqual(changed.next_probe_at, now)

    def test_level_trigger_does_not_spin_and_deleted_fact_is_a_change(self) -> None:
        now = datetime(2026, 8, 4, 10, 0, tzinfo=timezone.utc)
        time_config = TriggerConfig("due", TriggerKind.TIME_DUE, not_before=now, min_interval_seconds=10)
        due = probe_builtin(TriggerProbeInput(config=time_config, now=now))
        schedule = next_probe_schedule(time_config, due, unchanged_cycles=7, now=now)
        self.assertGreater(schedule.next_probe_at, now)
        self.assertEqual(schedule.unchanged_cycles, 8)

        deleted = probe_builtin(
            TriggerProbeInput(
                config=TriggerConfig("ref", TriggerKind.LOCAL_REF),
                now=now,
                current_facts=(),
                previous_facts=(("refs/main", "abc123"),),
            )
        )
        self.assertEqual(deleted.state, TriggerState.SATISFIED)

    def test_provider_protocol_is_bounded_and_fail_closed(self) -> None:
        now = datetime(2026, 8, 4, 10, 0, tzinfo=timezone.utc)
        payload = json.dumps(
            {
                "schema_version": 1,
                "state": "satisfied",
                "summary": "green",
                "evidence_ref": "ci:123",
            }
        ).encode()
        observation = parse_provider_observation(
            payload,
            trigger_id="provider-ci",
            observed_at=now,
            maximum_bytes=256,
        )

        self.assertEqual(observation.state, TriggerState.SATISFIED)
        self.assertEqual(observation.trigger_id, "provider-ci")
        with self.assertRaisesRegex(ValueError, "超过"):
            parse_provider_observation(b"x" * 257, trigger_id="provider-ci", observed_at=now, maximum_bytes=256)
        with self.assertRaisesRegex(ValueError, "未知字段"):
            parse_provider_observation(
                b'{"schema_version":1,"state":"satisfied","summary":"ok","mutation":"task merge"}',
                trigger_id="provider-ci",
                observed_at=now,
                maximum_bytes=256,
            )
        with self.assertRaisesRegex(ValueError, "schema_version"):
            parse_provider_observation(
                b'{"schema_version":true,"state":"pending","summary":"wait"}',
                trigger_id="provider-ci",
                observed_at=now,
            )
        with self.assertRaisesRegex(ValueError, "有效 JSON"):
            parse_provider_observation(
                (b"[" * 10_000) + (b"]" * 10_000),
                trigger_id="provider-ci",
                observed_at=now,
            )
        self.assertEqual(TriggerErrorKind.AUTH_MISSING.value, "auth_missing")

    def test_provider_descriptor_and_output_protocol_fail_closed(self) -> None:
        now = datetime(2026, 8, 4, 10, 0, tzinfo=timezone.utc)
        descriptor = ProviderDescriptor("ci-provider", ("provider-ci",), timeout_seconds=20, maximum_bytes=1024)

        self.assertEqual(descriptor.trigger_ids, ("provider-ci",))
        self.assertFalse(hasattr(descriptor, "command"))
        with self.assertRaisesRegex(ValueError, "next_probe_at"):
            parse_provider_observation(
                b'{"schema_version":1,"state":"pending","summary":"wait","next_probe_at":"2026-08-04T09:59:59Z"}',
                trigger_id="provider-ci",
                observed_at=now,
            )
        with self.assertRaisesRegex(ValueError, "summary"):
            parse_provider_observation(
                b'{"schema_version":1,"state":"pending","summary":"line\\nbreak"}',
                trigger_id="provider-ci",
                observed_at=now,
            )

    def test_cli_probe_and_signal_are_ephemeral_and_provider_cannot_execute(self) -> None:
        parser = build_parser()
        probe = parser.parse_args(
            [
                "trigger",
                "probe",
                "task_state",
                "--id",
                "task-change",
                "--current",
                "T-1=review",
                "--previous",
                "T-1=backlog",
                "--at",
                "2026-08-04T10:00:00Z",
                "--format",
                "json",
            ]
        )
        output = StringIO()
        with redirect_stdout(output):
            probe.func(probe)
        rendered = json.loads(output.getvalue())
        self.assertEqual(rendered["state"], "satisfied")
        self.assertEqual(rendered["delivery"], "ephemeral")

        signal = parser.parse_args(
            ["trigger", "signal", "operator-ready", "--at", "2026-08-04T10:00:00Z", "--format", "json"]
        )
        output = StringIO()
        with redirect_stdout(output):
            signal.func(signal)
        self.assertEqual(json.loads(output.getvalue())["summary"], "manual_signal")

        listing = parser.parse_args(["trigger", "list", "--format", "json"])
        output = StringIO()
        with redirect_stdout(output):
            listing.func(listing)
        self.assertIn(
            {"kind": "provider", "execution": "bounded_adapter_required"},
            json.loads(output.getvalue()),
        )

        provider = parser.parse_args(["trigger", "probe", "provider"])
        with self.assertRaisesRegex(DyroError, "不接受命令、URL 或脚本"):
            provider.func(provider)


if __name__ == "__main__":
    unittest.main()

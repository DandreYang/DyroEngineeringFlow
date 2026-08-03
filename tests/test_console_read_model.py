from __future__ import annotations

from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from dyro.config import load
from dyro.console.read_model import workspace_envelope
from dyro.console.models import ConsoleEnvelope
from dyro.console.redaction import safe_branch, safe_id, safe_title
from dyro.continuation.store import create_objective
from dyro.observations import capture_workspace_read_snapshot
from dyro.tasks import task_template
from dyro.workspace import create_line

from .support import WorkspaceCase


class ConsoleReadModelTests(WorkspaceCase):
    def setUp(self) -> None:
        super().setUp()
        self.config = load(self.root)
        create_line(self.config, line_id="alpha", branch="feat/alpha", base="main")
        task = self.config.task_specs_dir / "TASK-A"
        task.mkdir(parents=True)
        task.joinpath("task.toml").write_text(
            task_template("TASK-A", "Prepare release", "alpha", "api", "services/api"),
            encoding="utf-8",
        )
        task.joinpath("handoff.md").write_text("# handoff\n", encoding="utf-8")

    def _objective(self, *, target: str = "TASK-A") -> None:
        create_objective(
            self.config,
            f'''schema_version = 1
id = "release"
title = "Release readiness"
line = "alpha"
targets = ["{target}"]

[continuation]
requested_mode = "supervised"
operations = ["execute", "review"]

[budget]
max_actions = 5
max_attempts_per_task = 2
max_failures = 2
max_no_progress_cycles = 2
max_parallel = 1
''',
        )

    def test_capture_is_read_only_and_preserves_authoritative_core_facts(self) -> None:
        self._objective()
        before = {
            path.relative_to(self.root): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file()
        }

        snapshot = capture_workspace_read_snapshot(
            self.config,
            clock=lambda: datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc),
        )

        after = {
            path.relative_to(self.root): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file()
        }
        self.assertEqual(before, after)
        self.assertEqual(snapshot.workspace_name, "test-workspace")
        self.assertEqual([(item.id, item.status) for item in snapshot.tasks], [("TASK-A", "backlog")])
        self.assertEqual([(item.id, item.derived_result) for item in snapshot.objectives], [("release", "incomplete")])
        self.assertEqual(snapshot.objectives[0].attention[0].reason, "TASK_READY")

    def test_envelope_is_path_free_redacted_and_capture_time_does_not_change_digest(self) -> None:
        task_manifest = self.config.task_specs_dir / "TASK-A" / "task.toml"
        task_manifest.write_text(
            task_manifest.read_text(encoding="utf-8").replace(
                'title = "Prepare release"',
                'title = "Copy /Users/alice/private token=top-secret"',
            ),
            encoding="utf-8",
        )
        first = workspace_envelope(
            capture_workspace_read_snapshot(
                self.config,
                clock=lambda: datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc),
            )
        )
        second = workspace_envelope(
            capture_workspace_read_snapshot(
                self.config,
                clock=lambda: datetime(2026, 8, 4, 12, 1, tzinfo=timezone.utc),
            )
        )

        rendered = repr(first)
        self.assertNotIn(str(self.root), rendered)
        self.assertNotIn("/Users/alice/private", rendered)
        self.assertNotIn("top-secret", rendered)
        self.assertEqual(first["snapshot_sha256"], second["snapshot_sha256"])
        self.assertNotEqual(first["captured_at"], second["captured_at"])
        task = first["data"]["tasks"][0]
        self.assertEqual(task["title"], "REDACTED")

        task_manifest.write_text(
            task_manifest.read_text(encoding="utf-8").replace(
                'title = "Copy /Users/alice/private token=top-secret"',
                'title = "Inspect(path=/private/var/db)"',
            ),
            encoding="utf-8",
        )
        nested_path = workspace_envelope(
            capture_workspace_read_snapshot(
                self.config,
                clock=lambda: datetime(2026, 8, 4, 12, 2, tzinfo=timezone.utc),
            )
        )
        self.assertEqual(nested_path["data"]["tasks"][0]["title"], "REDACTED")
        self.assertEqual(safe_title("Rotate token sk-proj-THIS_IS_A_SECRET"), "REDACTED")
        self.assertEqual(safe_id("secret_token_ABC123"), "REDACTED")
        self.assertEqual(safe_branch("feat/secret-token-ABC123"), "REDACTED")
        slack_like = "xoxb-" + "123456789012-1234567890123-abcdefabcdefabcdefabcdef"
        self.assertEqual(safe_title(f"Rotate {slack_like}"), "REDACTED")

    def test_summary_capture_does_not_start_an_integration_probe(self) -> None:
        dependent = self.config.task_specs_dir / "TASK-B"
        dependent.mkdir(parents=True)
        dependent.joinpath("task.toml").write_text(
            task_template("TASK-B", "Dependent task", "alpha", "api", "services/api").replace(
                "depends_on = []", 'depends_on = ["TASK-A"]'
            ),
            encoding="utf-8",
        )
        dependent.joinpath("handoff.md").write_text("# handoff\n", encoding="utf-8")
        self.config.task_specs_dir.joinpath("TASK-A", "status").write_text(
            "done\n", encoding="utf-8"
        )
        self._objective(target="TASK-B")

        with patch(
            "dyro.continuation.snapshot.task_module._assert_dependency_integrated",
            side_effect=AssertionError("Console C01 must not probe Git"),
        ) as probe:
            snapshot = capture_workspace_read_snapshot(
                self.config,
                clock=lambda: datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc),
            )

        self.assertFalse(probe.called)
        task_a = next(item for item in snapshot.tasks if item.id == "TASK-A")
        self.assertEqual(task_a.integration_state, "not_inspected")
        self.assertEqual(
            snapshot.objectives[0].blocked_actions[0].reason,
            "TASK_INTEGRATION_PENDING",
        )

    def test_envelope_returns_a_deeply_fresh_json_value(self) -> None:
        envelope = ConsoleEnvelope(
            captured_at=datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc),
            snapshot_sha256="a" * 64,
            freshness_state="fresh",
            partial=False,
            warnings=(),
            data={"nested": {"items": ["safe"]}},
        )
        first = envelope.to_payload()
        first["data"]["nested"]["items"].append("changed")

        second = envelope.to_payload()
        self.assertEqual(second["data"]["nested"]["items"], ["safe"])

    def test_objective_failure_is_component_scoped_without_leaking_exception_text(self) -> None:
        self._objective()
        events = self.config.objectives_dir / "release" / "events.jsonl"
        events.write_text("not-json\n", encoding="utf-8")

        envelope = workspace_envelope(
            capture_workspace_read_snapshot(
                self.config,
                clock=lambda: datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc),
            )
        )

        self.assertEqual(envelope["freshness"]["state"], "partial")
        self.assertEqual(envelope["freshness"]["warnings"], [{"code": "OBJECTIVES_UNAVAILABLE"}])
        self.assertNotIn("events.jsonl", repr(envelope))


if __name__ == "__main__":
    unittest.main()

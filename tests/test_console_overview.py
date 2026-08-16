from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest

from dyro.console.overview import ConsoleOverviewError, ConsoleOverviewService
from dyro.errors import ValidationError
from dyro.hub import WorkspaceRecord, WorkspaceRegistry
from dyro.observations import (
    ObjectiveAttentionObservation,
    WorkspaceLineObservation,
    WorkspaceObjectiveObservation,
    WorkspaceReadSnapshot,
    WorkspaceTaskObservation,
)


def _snapshot(
    *,
    name: str,
    task_status: str = "backlog",
    attention_kind: str = "ready",
    reason: str = "TASK_READY",
    partial: bool = False,
    failure_code: str = "OBJECTIVES_UNAVAILABLE",
) -> WorkspaceReadSnapshot:
    observed_at = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    failures = ()
    if partial:
        from dyro.observations import ReadFailure

        failures = (ReadFailure("objectives", failure_code),)
    return WorkspaceReadSnapshot(
        schema_version=1,
        workspace_name=name,
        observed_at=observed_at,
        capture_id="capture-" + "a" * 24,
        workspace_revision="a" * 64,
        source_digests=(("tasks", "b" * 64),),
        completeness="partial" if partial else "complete",
        proof_inspection="not_inspected",
        lines=(
            WorkspaceLineObservation(
                id="alpha",
                kind="line",
                branch="feat/alpha",
                base="main",
                repository_count=1,
            ),
        ),
        tasks=(
            WorkspaceTaskObservation(
                id="TASK-A",
                title="Safe task",
                line="alpha",
                status=task_status,
                risk="write",
                depends_on=(),
                blocked_on=(),
                conflict_group="",
                executor="codex",
                reviewer="codex",
                integration_state="not_inspected",
                external_claim_active=False,
            ),
        ),
        objectives=(
            WorkspaceObjectiveObservation(
                id="release",
                title="Safe release",
                line="alpha",
                revision=1,
                operator_state="active",
                derived_result="incomplete",
                requested_mode="supervised",
                operations=("execute",),
                scope_count=1,
                budget=(("max_actions", 2),),
                selected_actions=(),
                blocked_actions=(),
                attention=(
                    ObjectiveAttentionObservation(
                        kind=attention_kind,
                        subject_id="TASK-A",
                        reason=reason,
                    ),
                ),
                contract_sha256="c" * 64,
                scope_sha256="d" * 64,
                event_sha256="e" * 64,
            ),
        ),
        failures=failures,
    )


class ConsoleOverviewServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.alpha_root = Path("/private/alpha-workspace")
        self.beta_root = Path("/private/beta-workspace")
        self.broken_root = Path("/private/broken-workspace")
        self.registry = WorkspaceRegistry(
            default="alpha",
            workspaces=(
                WorkspaceRecord("alpha", self.alpha_root),
                WorkspaceRecord("beta", self.beta_root),
                WorkspaceRecord("broken", self.broken_root),
            ),
        )
        configurations = {
            self.alpha_root: SimpleNamespace(name="Alpha Project", repositories={"api": object()}),
            self.beta_root: SimpleNamespace(name="Beta Project", repositories={"web": object()}),
        }
        self.snapshots = {
            "Alpha Project": _snapshot(
                name="Alpha Project",
                attention_kind="needs_user",
                reason="ANSWER_REQUIRED",
            ),
            "Beta Project": _snapshot(
                name="Beta Project",
                attention_kind="repair_required",
                reason="ACTION_UNCERTAIN",
                partial=True,
            ),
        }

        def config_loader(root: Path) -> SimpleNamespace:
            if root == self.broken_root:
                raise ValidationError("/private/broken-workspace/dyro.toml is malformed")
            return configurations[root]

        self.service = ConsoleOverviewService(
            registry_loader=lambda: self.registry,
            config_loader=config_loader,
            snapshot_loader=lambda config: self.snapshots[config.name],
            clock=lambda: datetime(2026, 8, 4, 12, 5, tzinfo=timezone.utc),
            cursor_secret=b"k" * 32,
        )

    def test_paginates_stably_prioritizes_attention_and_never_exposes_roots(self) -> None:
        first = self.service.page(limit=1)

        self.assertEqual(first["freshness"]["state"], "partial")
        self.assertEqual(first["data"]["total_workspaces"], 3)
        self.assertEqual(first["data"]["workspaces"][0]["alias"], "beta")
        self.assertEqual(first["data"]["highest_priority"]["alias"], "beta")
        self.assertEqual(first["data"]["highest_priority"]["kind"], "repair_required")
        self.assertEqual(first["data"]["attention_counts"]["needs_user"], 1)
        self.assertEqual(first["data"]["attention_counts"]["repair_required"], 1)
        self.assertIn("WORKSPACE_UNAVAILABLE", first["freshness"]["warnings"][1]["code"])
        self.assertNotIn("/private", repr(first))
        self.assertNotIn("dyro.toml", repr(first))

        second = self.service.page(cursor=first["data"]["next_cursor"], limit=1)
        self.assertEqual(second["data"]["workspaces"][0]["alias"], "broken")
        self.assertEqual(second["data"]["workspaces"][0]["availability"], "unavailable")
        self.assertEqual(
            second["data"]["workspaces"][0]["recommendation"]["command"],
            "dyro --workspace broken doctor",
        )
        self.assertNotEqual(first["snapshot_sha256"], second["snapshot_sha256"])

    def test_rejects_tampered_or_stale_cursor_without_falling_back_to_an_offset(self) -> None:
        first = self.service.page(limit=1)
        cursor = first["data"]["next_cursor"]
        with self.assertRaisesRegex(ConsoleOverviewError, "OVERVIEW_CURSOR_INVALID"):
            self.service.page(cursor=cursor[:-1] + "A", limit=1)

        self.registry = WorkspaceRegistry(
            default="alpha",
            workspaces=(WorkspaceRecord("alpha", self.alpha_root),),
        )
        with self.assertRaisesRegex(ConsoleOverviewError, "OVERVIEW_CURSOR_INVALID"):
            self.service.page(cursor=cursor, limit=1)

    def test_empty_attention_recommends_the_guided_home_not_task_next(self) -> None:
        recommendation = self.service._recommendation("alpha", [])

        self.assertEqual(
            recommendation,
            {"reason": "HOME_GUIDANCE", "command": "dyro --workspace alpha"},
        )

    def test_registry_failure_is_stable_and_path_free(self) -> None:
        service = ConsoleOverviewService(
            registry_loader=lambda: (_ for _ in ()).throw(
                ValidationError("/private/state/workspaces.json malformed")
            ),
            cursor_secret=b"z" * 32,
        )

        with self.assertRaisesRegex(ConsoleOverviewError, "REGISTRY_UNAVAILABLE") as raised:
            service.page()
        self.assertNotIn("/private", str(raised.exception))

    def test_warning_only_change_invalidates_the_page_etag(self) -> None:
        first = self.service.page(limit=1)
        self.snapshots["Beta Project"] = _snapshot(
            name="Beta Project",
            attention_kind="repair_required",
            reason="ACTION_UNCERTAIN",
            partial=True,
            failure_code="TASKS_UNAVAILABLE",
        )

        second = self.service.page(limit=1)

        first_data = dict(first["data"])
        second_data = dict(second["data"])
        first_cursor = first_data.pop("next_cursor")
        second_cursor = second_data.pop("next_cursor")
        self.assertEqual(first_data, second_data)
        self.assertNotEqual(first_cursor, second_cursor)
        self.assertNotEqual(first["snapshot_sha256"], second["snapshot_sha256"])
        self.assertNotEqual(first["freshness"]["warnings"], second["freshness"]["warnings"])
        with self.assertRaisesRegex(ConsoleOverviewError, "OVERVIEW_CURSOR_INVALID"):
            self.service.page(cursor=first["data"]["next_cursor"], limit=1)

    def test_single_workspace_reuses_the_same_summary_and_rejects_unsafe_aliases(self) -> None:
        payload = self.service.workspace("alpha")

        self.assertEqual(payload["data"]["workspace"]["alias"], "alpha")
        self.assertNotIn("/private", repr(payload))
        with self.assertRaisesRegex(ConsoleOverviewError, "WORKSPACE_ALIAS_INVALID"):
            self.service.workspace("%2fprivate")
        with self.assertRaisesRegex(ConsoleOverviewError, "WORKSPACE_NOT_FOUND"):
            self.service.workspace("missing")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest

from dyro.console.overview import ConsoleOverviewError, ConsoleOverviewService
from dyro.errors import ValidationError
from dyro.updates import UpdateState
from dyro.hub import WorkspaceRecord, WorkspaceRegistry
from dyro.observations import (
    ObjectiveAttentionObservation,
    WorkspaceLineObservation,
    WorkspaceObjectiveObservation,
    WorkspaceProofObservation,
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
    proof_inspection: str = "not_inspected",
    proofs: tuple[WorkspaceProofObservation, ...] = (),
    attention: tuple[ObjectiveAttentionObservation, ...] | None = None,
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
        proof_inspection=proof_inspection,
        proofs=proofs,
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
                    (
                        ObjectiveAttentionObservation(
                            kind=attention_kind,
                            subject_id="TASK-A",
                            reason=reason,
                        ),
                    )
                    if attention is None
                    else attention
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
            doctor_loader=lambda config: [],
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
        self.assertEqual(first["data"]["task_status_counts"], {"backlog": 2})
        self.assertIn("WORKSPACE_UNAVAILABLE", first["freshness"]["warnings"][1]["code"])
        self.assertNotIn("/private", repr(first))
        self.assertNotIn("dyro.toml", repr(first))

        second = self.service.page(cursor=first["data"]["next_cursor"], limit=1)
        self.assertEqual(second["data"]["workspaces"][0]["alias"], "broken")
        self.assertEqual(second["data"]["workspaces"][0]["availability"], "unavailable")
        self.assertEqual(
            first["data"]["workspaces"][0]["recommendation"]["command"],
            "dyro --workspace beta objective attention release",
        )
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

    def test_empty_attention_recommends_doctor_not_a_bare_workspace_invocation(self) -> None:
        recommendation = self.service._recommendation("core", [])

        self.assertEqual(
            recommendation,
            {"reason": "HOME_GUIDANCE", "command": "dyro --workspace core doctor"},
        )
        self.assertNotEqual(recommendation["command"], "dyro --workspace core")

    def test_fail_findings_and_empty_commands_recommend_doctor_not_bare_home(self) -> None:
        recommendation = self.service._recommendation(
            "core",
            [],
            findings=[
                {"status": "FAIL", "reason": "MISSING_ORIGIN", "line": "core"},
                {"status": "FAIL", "reason": "MISSING_ORIGIN", "line": "release_a"},
            ],
            commands=[],
        )

        self.assertEqual(recommendation["command"], "dyro --workspace core doctor")
        self.assertNotEqual(recommendation["command"], "dyro --workspace core")
        self.assertEqual(recommendation["reason"], "MISSING_ORIGIN")
        self.assertNotEqual(recommendation["reason"], "HOME_GUIDANCE")

    def test_fail_findings_project_path_free_and_degrade_health(self) -> None:
        self.registry = WorkspaceRegistry(
            default="core",
            workspaces=(WorkspaceRecord("core", self.alpha_root),),
        )
        self.snapshots["Alpha Project"] = _snapshot(
            name="Alpha Project",
            attention=(),
        )
        service = ConsoleOverviewService(
            registry_loader=lambda: self.registry,
            config_loader=self.service._config_loader,
            snapshot_loader=lambda config: self.snapshots[config.name],
            clock=self.service._clock,
            cursor_secret=b"k" * 32,
            doctor_loader=lambda config: [
                "FAIL line:core/api: missing origin/feat/core",
                "FAIL line:core_pay/api: missing origin/feat/core_pay",
                "FAIL hotfix:release_a/api: missing origin/hotfix/release_a",
                "FAIL repository api: missing or not Git: /private/secret",
            ],
        )

        page = service.page()
        card = page["data"]["workspaces"][0]

        self.assertEqual(card["alias"], "core")
        self.assertEqual(card["health"], "degraded")
        self.assertEqual(
            {(item["reason"], item["line"]) for item in card["findings"]},
            {
                ("MISSING_ORIGIN", "core"),
                ("MISSING_ORIGIN", "core_pay"),
                ("MISSING_ORIGIN", "release_a"),
                ("REPOSITORY_UNAVAILABLE", ""),
            },
        )
        self.assertEqual(card["recommendation"]["command"], "dyro --workspace core doctor")
        self.assertNotEqual(card["recommendation"]["command"], "dyro --workspace core")
        self.assertEqual(page["data"]["highest_priority"]["kind"], "repair_required")
        self.assertEqual(page["data"]["highest_priority"]["reason"], "MISSING_ORIGIN")
        self.assertNotIn("/private", repr(page))
        self.assertNotIn("secret", repr(card["findings"]))

    def test_attention_recommends_the_same_follow_up_as_next(self) -> None:
        self.assertEqual(
            self.service._recommendation(
                "alpha",
                [
                    {
                        "objective_id": "release",
                        "kind": "ready",
                        "subject_id": "TASK-A",
                        "reason": "TASK_READY",
                    }
                ],
            ),
            {
                "reason": "TASK_READY",
                "command": "dyro --workspace alpha objective tick release",
            },
        )
        self.assertEqual(
            self.service._recommendation(
                "alpha",
                [
                    {
                        "objective_id": "release",
                        "kind": "needs_user",
                        "subject_id": "TASK-A",
                        "reason": "ANSWER_REQUIRED",
                    }
                ],
            ),
            {
                "reason": "ANSWER_REQUIRED",
                "command": "dyro --workspace alpha objective attention release",
            },
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
        page = self.service.page(limit=3)

        self.assertEqual(payload["data"]["workspace"]["alias"], "alpha")
        self.assertEqual(payload["data"]["workspace"]["findings"], [])
        self.assertEqual(payload["data"]["workspace"]["proof_inspection"], "not_inspected")
        self.assertEqual(payload["data"]["lines"][0]["id"], "alpha")
        self.assertEqual(payload["data"]["lines"][0]["parent"], "")
        self.assertEqual(payload["data"]["tasks"][0]["id"], "TASK-A")
        self.assertEqual(payload["data"]["tasks"][0]["integration_state"], "not_inspected")
        self.assertEqual(payload["data"]["objectives"][0]["id"], "release")
        self.assertEqual(payload["data"]["operator_twin"]["plan"][0]["id"], "release")
        self.assertFalse(payload["data"]["operator_twin"]["plan"][0]["wave_present"])
        self.assertFalse(payload["data"]["operator_twin"]["latest_ledger"]["present"])
        self.assertNotIn("proofs", payload["data"])
        self.assertNotIn("operator_twin", page["data"])
        self.assertNotIn("lines", page["data"])
        self.assertNotIn("tasks", page["data"])
        self.assertNotIn("objectives", page["data"])
        self.assertNotIn("/private", repr(payload))
        with self.assertRaisesRegex(ConsoleOverviewError, "WORKSPACE_ALIAS_INVALID"):
            self.service.workspace("%2fprivate")
        with self.assertRaisesRegex(ConsoleOverviewError, "WORKSPACE_NOT_FOUND"):
            self.service.workspace("missing")

    def test_overview_task_status_counts_ignore_unavailable_workspaces(self) -> None:
        self.registry = WorkspaceRegistry(
            default="broken",
            workspaces=(WorkspaceRecord("broken", self.broken_root),),
        )
        service = ConsoleOverviewService(
            registry_loader=lambda: self.registry,
            config_loader=self.service._config_loader,
            snapshot_loader=self.service._snapshot_loader,
            clock=self.service._clock,
            cursor_secret=b"k" * 32,
            doctor_loader=lambda config: [],
        )

        payload = service.page()

        self.assertEqual(payload["data"]["workspaces"][0]["availability"], "unavailable")
        self.assertEqual(payload["data"]["task_status_counts"], {})

    def test_unavailable_workspace_keeps_empty_inventory_keys(self) -> None:
        payload = self.service.workspace("broken")

        self.assertEqual(payload["data"]["workspace"]["availability"], "unavailable")
        self.assertEqual(payload["data"]["workspace"]["findings"], [])
        self.assertEqual(payload["data"]["workspace"]["proof_inspection"], "not_inspected")
        self.assertEqual(payload["data"]["lines"], [])
        self.assertEqual(payload["data"]["tasks"], [])
        self.assertEqual(payload["data"]["objectives"], [])
        self.assertEqual(payload["data"]["operator_twin"]["plan"], [])
        self.assertFalse(payload["data"]["operator_twin"]["latest_ledger"]["present"])
        self.assertNotIn("proofs", payload["data"])

    def test_inspect_proofs_does_not_use_summary_loader_and_can_show_decay(self) -> None:
        inspected = _snapshot(
            name="Alpha Project",
            attention_kind="needs_user",
            reason="PROOF_DECAYED",
            proof_inspection="inspected",
            proofs=(
                WorkspaceProofObservation(
                    id="a" * 64,
                    kind="review_verdict",
                    subject="TASK-A",
                    status="decayed",
                    decay_reason="review_acceptance",
                ),
            ),
        )

        def summary_loader(config: object) -> WorkspaceReadSnapshot:
            raise AssertionError("summary snapshot_loader must not run during inspect")

        service = ConsoleOverviewService(
            registry_loader=lambda: self.registry,
            config_loader=self.service._config_loader,
            snapshot_loader=summary_loader,
            inspect_loader=lambda config: inspected,
            clock=lambda: datetime(2026, 8, 4, 12, 5, tzinfo=timezone.utc),
            cursor_secret=b"k" * 32,
            doctor_loader=lambda config: [],
        )
        leaked = ConsoleOverviewService(
            registry_loader=lambda: self.registry,
            config_loader=self.service._config_loader,
            snapshot_loader=lambda config: inspected,
            inspect_loader=lambda config: inspected,
            clock=lambda: datetime(2026, 8, 4, 12, 5, tzinfo=timezone.utc),
            cursor_secret=b"k" * 32,
            doctor_loader=lambda config: [],
        )
        summary = leaked.workspace("alpha")
        self.assertEqual(summary["data"]["workspace"]["proof_inspection"], "not_inspected")
        self.assertEqual(summary["data"]["tasks"][0]["integration_state"], "not_inspected")
        self.assertNotIn("proofs", summary["data"])
        self.assertNotIn("PROOF_DECAYED", repr(summary["data"]["objectives"]))
        payload = service.inspect_proofs("alpha")
        self.assertEqual(payload["data"]["proof_inspection"], "inspected")
        self.assertEqual(payload["data"]["proofs"][0]["status"], "decayed")
        self.assertEqual(payload["data"]["objectives"][0]["attention"][0]["reason"], "PROOF_DECAYED")
        self.assertNotIn("procedure", repr(payload))
        with self.assertRaisesRegex(ConsoleOverviewError, "WORKSPACE_ALIAS_INVALID"):
            service.inspect_proofs("%2fprivate")

    def test_system_reads_cached_update_without_probing_tools(self) -> None:
        service = ConsoleOverviewService(
            registry_loader=lambda: self.registry,
            update_loader=lambda: UpdateState(
                check_enabled=True,
                last_checked_on="2026-08-16",
                latest_version="0.7.2",
            ),
            version_loader=lambda: "0.7.1",
            clock=lambda: datetime(2026, 8, 17, 3, 0, tzinfo=timezone.utc),
            cursor_secret=b"k" * 32,
        )

        payload = service.system()

        self.assertEqual(payload["data"]["tool_inspection"], "not_inspected")
        self.assertEqual(payload["data"]["tools"], [])
        self.assertEqual(payload["data"]["update"]["kind"], "patch")
        self.assertEqual(payload["data"]["update"]["latest_version"], "0.7.2")
        self.assertNotIn("/private", repr(payload))

    def test_invalid_update_state_is_unread_and_path_free(self) -> None:
        service = ConsoleOverviewService(
            registry_loader=lambda: self.registry,
            update_loader=lambda: (_ for _ in ()).throw(
                ValidationError("/private/state/updates.json malformed")
            ),
            cursor_secret=b"k" * 32,
        )

        payload = service.system()

        self.assertEqual(payload["data"]["tool_inspection"], "not_inspected")
        self.assertEqual(payload["data"]["tools"], [])
        self.assertEqual(payload["data"]["update"]["kind"], "none")
        self.assertIn("UPDATE_STATE_UNAVAILABLE", payload["freshness"]["warnings"][0]["code"])
        self.assertNotIn("/private", repr(payload))

    def test_system_sanitizes_unreadable_update_fields(self) -> None:
        service = ConsoleOverviewService(
            registry_loader=lambda: self.registry,
            update_loader=lambda: UpdateState(
                check_enabled=True,
                last_checked_on="Tuesday",
                latest_version="not-a-version",
            ),
            version_loader=lambda: "0.7.1",
            clock=lambda: datetime(2026, 8, 17, 3, 0, tzinfo=timezone.utc),
            cursor_secret=b"k" * 32,
        )

        payload = service.system()

        self.assertEqual(payload["data"]["update"]["last_checked_on"], "")
        self.assertEqual(payload["data"]["update"]["latest_version"], "")
        self.assertEqual(payload["data"]["update"]["kind"], "none")
        self.assertEqual(payload["data"]["tools"], [])


if __name__ == "__main__":
    unittest.main()

"""Production readiness gate for the external semantic runtime experiment.

Stage 5 closeout: local isolation may be proven while production remains
explicitly Not ready. This module encodes the ADR-0001 stop conditions and the
operator checklist so CI/tests can assert the gate stays fail-closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence


@dataclass(frozen=True)
class ChecklistItem:
    id: str
    requirement: str
    status: str  # pass | partial | fail | out_of_scope
    evidence: str
    blocks_production: bool


def production_not_ready_checklist() -> tuple[ChecklistItem, ...]:
    return (
        ChecklistItem(
            id="PROD-01",
            requirement="Multi-host / production container escape review",
            status="out_of_scope",
            evidence="Stage0–5 only prove local Docker/Colima isolation",
            blocks_production=True,
        ),
        ChecklistItem(
            id="PROD-02",
            requirement="Real Codex/Claude binary + credential mounts in operator fleet",
            status="partial",
            evidence="Stage5 host provider pin path exists; suite uses fixture CLI",
            blocks_production=True,
        ),
        ChecklistItem(
            id="PROD-03",
            requirement="Dyro Core evidence import + review binding",
            status="fail",
            evidence="Stage5 dry-run only; no import/signoff/merge/push",
            blocks_production=True,
        ),
        ChecklistItem(
            id="PROD-04",
            requirement="No third-party workflow package / brand in tree or history",
            status="pass",
            evidence="first-party @dyro/semantic-flow; history purge completed",
            blocks_production=False,
        ),
        ChecklistItem(
            id="PROD-05",
            requirement="Sandbox never holds provider tokens or execution keys",
            status="pass",
            evidence="Stage1–5 tests deny token/key in sandbox surfaces",
            blocks_production=False,
        ),
        ChecklistItem(
            id="PROD-06",
            requirement="Fail-closed critical branches; no silent null parallel",
            status="pass",
            evidence="@dyro/semantic-flow parallelSettled + envelope validation",
            blocks_production=False,
        ),
        ChecklistItem(
            id="PROD-07",
            requirement="Dual cleanup before any post-run privileged action",
            status="pass",
            evidence="Stage4/5 CleanupProof before pack and dry-run",
            blocks_production=False,
        ),
        ChecklistItem(
            id="PROD-08",
            requirement="Supervisor never merges or pushes",
            status="pass",
            evidence="refuse_if_merge_requested / dry-run forbidden actions",
            blocks_production=False,
        ),
        ChecklistItem(
            id="PROD-09",
            requirement="Worktree storage quotas in all writable mounts",
            status="partial",
            evidence="host worktree quota only; Docker volume driver quotas unproven",
            blocks_production=True,
        ),
        ChecklistItem(
            id="PROD-10",
            requirement="Replace Dyro TaskGraph with external semantic runtime",
            status="out_of_scope",
            evidence="ADR-0001 veto; never attempted",
            blocks_production=False,
        ),
    )


def evaluate_production_readiness(
    checklist: Sequence[ChecklistItem] | None = None,
) -> dict[str, object]:
    items = list(checklist or production_not_ready_checklist())
    blockers = [item for item in items if item.blocks_production]
    open_blockers = [
        item
        for item in blockers
        if item.status in {"fail", "partial", "out_of_scope"}
    ]
    production_ready = len(open_blockers) == 0
    return {
        "schema_version": 1,
        "kind": "external-semantic-runtime-production-gate",
        "production_ready": production_ready,
        "verdict": "READY" if production_ready else "NOT_READY",
        "blocker_count": len(open_blockers),
        "blockers": [
            {
                "id": item.id,
                "requirement": item.requirement,
                "status": item.status,
                "evidence": item.evidence,
            }
            for item in open_blockers
        ],
        "checklist": [
            {
                "id": item.id,
                "requirement": item.requirement,
                "status": item.status,
                "evidence": item.evidence,
                "blocks_production": item.blocks_production,
            }
            for item in items
        ],
        "adr_stop_conditions": [
            "must_not_modify_dyro_scheduler_for_internal_phases",
            "must_isolate_credentials_and_execution_keys",
            "must_fail_closed_on_critical_failures",
            "must_not_reintroduce_third_party_workflow_deps",
        ],
    }


def assert_not_production_ready(report: Mapping[str, object] | None = None) -> None:
    payload = report or evaluate_production_readiness()
    if payload.get("production_ready") is True:
        raise AssertionError(
            "production gate unexpectedly READY; experiment closeout expects NOT_READY"
        )
    if payload.get("verdict") != "NOT_READY":
        raise AssertionError(f"unexpected production verdict: {payload.get('verdict')}")

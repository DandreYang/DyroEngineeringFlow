"""Deny-by-default Exposure Catalog. Metadata only; no CLI routing."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import sys

from ..canonical import canonical_json_bytes
from ..errors import ValidationError
from .models import Availability, CatalogRecord, Risk

IMPLEMENTED_TESTABLE_IDS = frozenset(
    {
        "bridge.hello",
        "bridge.capabilities.compact",
        "bridge.operation.schema",
        "workspace.resolve",
        "workspace.list",
        "workspace.observe",
        "line.list",
        "task.list",
        "task.gate_definitions.get",
        "objective.list",
        "objective.status",
        "objective.plan",
        "objective.explain",
        "objective.graph",
        "objective.tick",
        "objective.attention",
    }
)

MANDATORY_OPERATION_IDS = frozenset(
    {
        "bridge.hello",
        "bridge.capabilities.compact",
        "bridge.operation.schema",
        "workspace.resolve",
        "workspace.list",
        "workspace.observe",
        "objective.plan",
    }
)

EXCLUDED_OPERATION_IDS = frozenset(
    {
        "task.gates",
        "task.gates.run",
        "task.run",
        "task.next",
        "task.loop",
        "task.daemon",
        "task.answer",
        "objective.apply",
        "objective.create",
        "task.merge",
        "task.push",
        "line.merge",
        "release.publish",
        "workspace.update",
    }
)

_PHASE0_DECLARED: tuple[tuple[str, Risk, str], ...] = (
    ("bridge.hello", Risk.R0, "dyro.bridge.transport.hello"),
    ("bridge.capabilities.compact", Risk.R0, "dyro.bridge.catalog.compact_catalog"),
    ("bridge.operation.schema", Risk.R0, "dyro.bridge.schemas.operation_schema"),
    ("workspace.resolve", Risk.R0, "dyro.bridge.observations.resolve_workspace"),
    ("workspace.list", Risk.R0, "dyro.bridge.observations.list_workspaces"),
    ("workspace.observe", Risk.R0, "dyro.bridge.observations.observe_workspace"),
    ("line.list", Risk.R0, "dyro.bridge.observations.list_lines"),
    ("task.list", Risk.R0, "dyro.bridge.observations.list_tasks"),
    ("task.explain", Risk.R0, "dyro.bridge.observations.explain_task"),
    ("task.graph", Risk.R0, "dyro.bridge.observations.task_graph"),
    ("task.gate_definitions.get", Risk.R0, "dyro.bridge.observations.gate_definitions"),
    ("objective.list", Risk.R0, "dyro.bridge.observations.list_objectives"),
    ("objective.status", Risk.R0, "dyro.bridge.observations.objective_status"),
    ("objective.plan", Risk.PLAN, "dyro.bridge.plans.objective_plan"),
    ("objective.explain", Risk.PLAN, "dyro.bridge.plans.objective_explain"),
    ("objective.graph", Risk.PLAN, "dyro.bridge.plans.objective_graph"),
    ("objective.tick", Risk.PLAN, "dyro.bridge.plans.objective_tick"),
    ("objective.attention", Risk.PLAN, "dyro.bridge.plans.objective_attention"),
)


@dataclass(frozen=True)
class ExposureCatalog:
    operations: tuple[CatalogRecord, ...]
    digest: str

    def record(self, operation_id: str) -> CatalogRecord | None:
        for item in self.operations:
            if item.id == operation_id:
                return item
        return None


def compact_catalog(catalog: ExposureCatalog) -> dict[str, object]:
    return {
        "schema_version": 1,
        "operations": [
            {
                "id": item.id,
                "risk": item.risk.value,
                "availability": item.availability.value,
                "operation_schema_version": item.schema_version,
                "must_be_available": item.must_be_available,
            }
            for item in catalog.operations
        ],
    }


def catalog_digest(operations: tuple[CatalogRecord, ...]) -> str:
    payload = compact_catalog(ExposureCatalog(operations=operations, digest=""))
    digest = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    return f"sha256:{digest}"


def validate_catalog(catalog: ExposureCatalog, *, release: bool = False) -> None:
    ids = [item.id for item in catalog.operations]
    if len(ids) != len(set(ids)):
        raise ValidationError("Exposure Catalog 不能重复 operation")
    excluded = sorted(set(ids) & EXCLUDED_OPERATION_IDS)
    if excluded:
        raise ValidationError(f"Phase 0 禁止这些 operation：{', '.join(excluded)}")
    missing = sorted(MANDATORY_OPERATION_IDS - set(ids))
    if missing:
        raise ValidationError(f"Exposure Catalog 缺少强制 operation：{', '.join(missing)}")
    if release:
        public = tuple(
            item
            for item in catalog.operations
            if item.availability is Availability.PUBLIC_AVAILABLE
        )
        if not public:
            raise ValidationError("release catalog 不能是空的 public surface")
        unpaid = sorted(
            item.id
            for item in catalog.operations
            if item.must_be_available
            and item.availability is not Availability.PUBLIC_AVAILABLE
        )
        if unpaid:
            raise ValidationError(
                f"release catalog 强制 operation 尚未 public：{', '.join(unpaid)}"
            )


def catalog_platform(value: str | None = None) -> str:
    raw = (value or sys.platform).lower()
    if raw.startswith("linux"):
        return "linux"
    if raw.startswith("darwin"):
        return "darwin"
    if raw.startswith("win"):
        return "win32"
    return "unsupported"


def operation_availability(operation_id: str, *, platform: str | None = None) -> Availability:
    host = catalog_platform(platform)
    if host == "linux" and operation_id in MANDATORY_OPERATION_IDS:
        return Availability.PUBLIC_AVAILABLE
    if operation_id in IMPLEMENTED_TESTABLE_IDS:
        return Availability.IMPLEMENTED_TESTABLE
    return Availability.DECLARED


def build_default_catalog(*, platform: str | None = None) -> ExposureCatalog:
    operations = tuple(
        CatalogRecord(
            id=operation_id,
            risk=risk,
            availability=operation_availability(operation_id, platform=platform),
            schema_version=1,
            must_be_available=operation_id in MANDATORY_OPERATION_IDS,
            core_service=service,
        )
        for operation_id, risk, service in _PHASE0_DECLARED
    )
    catalog = ExposureCatalog(operations=operations, digest=catalog_digest(operations))
    validate_catalog(catalog, release=False)
    return catalog

"""Deny-by-default exposure metadata for Agent Bridge Phase 0."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib

from ..canonical import canonical_json_bytes
from ..errors import ValidationError
from .models import (
    AvailabilityState,
    OperationKind,
    OperationSpec,
    PlatformAvailability,
    PlatformState,
    RiskClass,
)
from .constants import PLAN_OPERATION_REVISIONS
from .schemas import get_operation_schema


MANDATORY_OPERATION_IDS = (
    "bridge.capabilities.compact",
    "bridge.hello",
    "bridge.operation.schema",
    "objective.plan",
    "workspace.list",
    "workspace.observe",
    "workspace.resolve",
)

PHASE0_DECLARED_OPERATION_IDS = (
    "bridge.capabilities.compact",
    "bridge.hello",
    "bridge.operation.schema",
    "line.list",
    "objective.attention",
    "objective.explain",
    "objective.graph",
    "objective.list",
    "objective.plan",
    "objective.status",
    "objective.tick",
    "task.explain",
    "task.gate_definitions.get",
    "task.graph",
    "task.list",
    "workspace.list",
    "workspace.observe",
    "workspace.resolve",
)

_DECLARED_PLATFORMS = (
    PlatformAvailability("linux-ubuntu-24.04", PlatformState.DECLARED),
    PlatformAvailability("macos-15", PlatformState.DECLARED),
    PlatformAvailability("windows", PlatformState.UNAVAILABLE),
)

_IMPLEMENTED_TESTABLE_SERVICES = {
    "objective.attention": "dyro.bridge.plans.attention_objective",
    "objective.explain": "dyro.bridge.plans.explain_objective",
    "objective.graph": "dyro.bridge.plans.graph_objective",
    "objective.plan": "dyro.bridge.plans.plan_objective",
    "objective.tick": "dyro.bridge.plans.tick_objective",
    "task.gate_definitions.get": "dyro.bridge.observations.get_gate_definitions_observation",
    "workspace.list": "dyro.bridge.observations.list_workspace_observations",
    "workspace.observe": "dyro.bridge.observations.observe_workspace",
    "workspace.resolve": "dyro.bridge.observations.resolve_workspace_observation",
}


def _declared(
    operation_id: str,
    *,
    kind: OperationKind,
    planner_revision: str | None = None,
) -> OperationSpec:
    schema = get_operation_schema(operation_id)
    service_id = _IMPLEMENTED_TESTABLE_SERVICES.get(operation_id)
    return OperationSpec(
        operation_id=operation_id,
        kind=kind,
        maximum_risk=RiskClass.R0 if kind is OperationKind.INSPECT else RiskClass.PLAN,
        schema_version=schema.schema_version,
        planner_revision=planner_revision,
        input_schema_id=schema.input_schema_id,
        output_schema_id=schema.output_schema_id,
        must_be_available=operation_id in MANDATORY_OPERATION_IDS,
        availability_state=(
            AvailabilityState.IMPLEMENTED_TESTABLE
            if service_id is not None
            else AvailabilityState.DECLARED
        ),
        service_id=service_id,
        platforms=_DECLARED_PLATFORMS,
    )


@dataclass(frozen=True)
class ExposureCatalog:
    operations: tuple[OperationSpec, ...]
    mandatory_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.operations, tuple) or not isinstance(
            self.mandatory_ids, tuple
        ):
            raise ValidationError("Exposure Catalog 必须使用不可变 tuple")
        if not all(isinstance(item, OperationSpec) for item in self.operations):
            raise ValidationError("Exposure Catalog 必须只包含 OperationSpec")
        if not all(isinstance(item, str) for item in self.mandatory_ids):
            raise ValidationError("mandatory operation ID 必须是字符串")
        operation_ids = tuple(item.operation_id for item in self.operations)
        if not operation_ids:
            raise ValidationError("Exposure Catalog 不能为空")
        if operation_ids != tuple(sorted(operation_ids)):
            raise ValidationError("Exposure Catalog operation 必须按 ID 稳定排序")
        if len(set(operation_ids)) != len(operation_ids):
            raise ValidationError("Exposure Catalog 包含重复 operation")
        if self.mandatory_ids != tuple(sorted(self.mandatory_ids)):
            raise ValidationError("mandatory operation ID 必须稳定排序")
        if len(set(self.mandatory_ids)) != len(self.mandatory_ids):
            raise ValidationError("mandatory operation ID 不能重复")
        if not self.mandatory_ids:
            raise ValidationError("Mandatory Core Surface 不能为空")
        missing = tuple(
            item for item in self.mandatory_ids if item not in operation_ids
        )
        if missing:
            raise ValidationError(
                f"Exposure Catalog 缺少 mandatory operation：{', '.join(missing)}"
            )
        for operation in self.operations:
            if operation.maximum_risk not in {RiskClass.R0, RiskClass.PLAN}:
                raise ValidationError(
                    f"Phase 0 Exposure Catalog 禁止 {operation.maximum_risk.value}："
                    f"{operation.operation_id}"
                )
            schema = get_operation_schema(operation.operation_id)
            if schema.schema_version != operation.schema_version:
                raise ValidationError(
                    f"operation schema version 不一致：{operation.operation_id}"
                )
            if schema.input_schema_id != operation.input_schema_id:
                raise ValidationError(
                    f"input schema ID 不一致：{operation.operation_id}"
                )
            if schema.output_schema_id != operation.output_schema_id:
                raise ValidationError(
                    f"output schema ID 不一致：{operation.operation_id}"
                )
            if (
                operation.operation_id in self.mandatory_ids
                and not operation.must_be_available
            ):
                raise ValidationError(
                    f"mandatory operation 未标记 must_be_available：{operation.operation_id}"
                )

    def get(self, operation_id: str) -> OperationSpec:
        if not isinstance(operation_id, str):
            raise ValidationError("Bridge operation ID 必须是字符串")
        for operation in self.operations:
            if operation.operation_id == operation_id:
                return operation
        raise ValidationError(f"Bridge operation 未声明：{operation_id!r}")

    def compact_capabilities(self, platform: str) -> tuple[dict[str, object], ...]:
        return tuple(operation.capability(platform) for operation in self.operations)

    def capabilities_digest(self, platform: str) -> str:
        payload = {"operations": list(self.compact_capabilities(platform))}
        return "sha256:" + hashlib.sha256(canonical_json_bytes(payload)).hexdigest()

    def validate_release(self, supported_platforms: tuple[str, ...]) -> None:
        if (
            not isinstance(supported_platforms, tuple)
            or not supported_platforms
            or not all(
                isinstance(platform, str) and platform
                for platform in supported_platforms
            )
            or len(set(supported_platforms)) != len(supported_platforms)
        ):
            raise ValidationError("release platform 必须是非空且不重复的 tuple")
        for operation_id in self.mandatory_ids:
            operation = self.get(operation_id)
            if not operation.public_available:
                raise ValidationError(
                    f"mandatory operation 尚未 public available：{operation_id}"
                )
            platform_states = {
                item.platform: item.state for item in operation.platforms
            }
            missing = tuple(
                platform
                for platform in supported_platforms
                if platform_states.get(platform) is not PlatformState.AVAILABLE
            )
            if missing:
                raise ValidationError(
                    f"mandatory operation {operation_id} 未支持平台：{', '.join(missing)}"
                )


_OPERATIONS = tuple(
    sorted(
        (
            _declared("bridge.hello", kind=OperationKind.INSPECT),
            _declared("bridge.capabilities.compact", kind=OperationKind.INSPECT),
            _declared("bridge.operation.schema", kind=OperationKind.INSPECT),
            _declared("line.list", kind=OperationKind.INSPECT),
            _declared(
                "objective.attention",
                kind=OperationKind.PLAN,
                planner_revision=PLAN_OPERATION_REVISIONS["objective.attention"],
            ),
            _declared(
                "objective.explain",
                kind=OperationKind.PLAN,
                planner_revision=PLAN_OPERATION_REVISIONS["objective.explain"],
            ),
            _declared(
                "objective.graph",
                kind=OperationKind.PLAN,
                planner_revision=PLAN_OPERATION_REVISIONS["objective.graph"],
            ),
            _declared("objective.list", kind=OperationKind.INSPECT),
            _declared(
                "objective.plan",
                kind=OperationKind.PLAN,
                planner_revision=PLAN_OPERATION_REVISIONS["objective.plan"],
            ),
            _declared("objective.status", kind=OperationKind.INSPECT),
            _declared(
                "objective.tick",
                kind=OperationKind.PLAN,
                planner_revision=PLAN_OPERATION_REVISIONS["objective.tick"],
            ),
            _declared("task.explain", kind=OperationKind.INSPECT),
            _declared("task.gate_definitions.get", kind=OperationKind.INSPECT),
            _declared("task.graph", kind=OperationKind.INSPECT),
            _declared("task.list", kind=OperationKind.INSPECT),
            _declared("workspace.list", kind=OperationKind.INSPECT),
            _declared("workspace.observe", kind=OperationKind.INSPECT),
            _declared("workspace.resolve", kind=OperationKind.INSPECT),
        ),
        key=lambda item: item.operation_id,
    )
)

CATALOG = ExposureCatalog(_OPERATIONS, mandatory_ids=MANDATORY_OPERATION_IDS)


def list_operations() -> tuple[OperationSpec, ...]:
    return CATALOG.operations


def get_operation(operation_id: str) -> OperationSpec:
    return CATALOG.get(operation_id)


def compact_capabilities(platform: str) -> tuple[dict[str, object], ...]:
    return CATALOG.compact_capabilities(platform)


def capabilities_digest(platform: str) -> str:
    return CATALOG.capabilities_digest(platform)

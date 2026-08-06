"""Immutable JSON Schema documents for the declared Phase 0 surface."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from types import MappingProxyType

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from ..canonical import canonical_json_bytes, canonical_json_text
from ..errors import ValidationError


JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
SAFE_ID_PATTERN = r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}$"
OPERATION_ID_PATTERN = r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$"


@dataclass(frozen=True)
class OperationSchema:
    operation_id: str
    schema_version: int
    input_schema_id: str
    output_schema_id: str
    _input_json: str
    _output_json: str

    def __post_init__(self) -> None:
        if not isinstance(self.operation_id, str) or not re.fullmatch(
            OPERATION_ID_PATTERN, self.operation_id
        ):
            raise ValidationError(f"Bridge operation ID 不合法：{self.operation_id!r}")
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version < 1
        ):
            raise ValidationError("operation schema version 必须是正整数")
        expected_input = f"{self.operation_id}.input.v{self.schema_version}"
        expected_output = f"{self.operation_id}.output.v{self.schema_version}"
        if (
            self.input_schema_id != expected_input
            or self.output_schema_id != expected_output
        ):
            raise ValidationError(f"operation schema ID 不一致：{self.operation_id}")
        for label, value, expected_id in (
            ("input", self._input_json, expected_input),
            ("output", self._output_json, expected_output),
        ):
            if not isinstance(value, str):
                raise ValidationError(f"{label} schema 必须是 canonical JSON 字符串")
            try:
                document = json.loads(value)
            except (TypeError, ValueError) as exc:
                raise ValidationError(f"{label} schema 不是合法 JSON") from exc
            if not isinstance(document, dict):
                raise ValidationError(f"{label} schema 顶层必须是 object")
            if document.get("$schema") != JSON_SCHEMA_DIALECT:
                raise ValidationError(f"{label} schema dialect 不一致")
            if document.get("$id") != f"urn:dyro:bridge:{expected_id}":
                raise ValidationError(f"{label} schema $id 不一致")
            if (
                document.get("type") != "object"
                or document.get("additionalProperties") is not False
            ):
                raise ValidationError(f"{label} schema 顶层必须是 strict object")
            try:
                Draft202012Validator.check_schema(document)
            except SchemaError as exc:
                raise ValidationError(f"{label} schema 不符合 Draft 2020-12") from exc
            if canonical_json_text(document) != value:
                raise ValidationError(f"{label} schema 必须使用 canonical JSON")

    def input_schema(self) -> dict[str, object]:
        return json.loads(self._input_json)

    def output_schema(self) -> dict[str, object]:
        return json.loads(self._output_json)

    def public_dict(self) -> dict[str, object]:
        return {
            "operation": self.operation_id,
            "operation_schema_version": self.schema_version,
            "input_schema": self.input_schema(),
            "output_schema": self.output_schema(),
            "schema_digest": _operation_schema_digest(self),
        }


def _operation_schema_digest(bundle: OperationSchema) -> str:
    payload = {
        "operation": bundle.operation_id,
        "operation_schema_version": bundle.schema_version,
        "input_schema": bundle.input_schema(),
        "output_schema": bundle.output_schema(),
    }
    return "sha256:" + hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _string(*, maximum: int = 128, pattern: str | None = None) -> dict[str, object]:
    result: dict[str, object] = {"type": "string", "minLength": 1, "maxLength": maximum}
    if pattern is not None:
        result["pattern"] = pattern
    return result


def _nullable_string(
    *, maximum: int = 128, minimum: int = 0, pattern: str | None = None
) -> dict[str, object]:
    result: dict[str, object] = {
        "type": ["string", "null"],
        "minLength": minimum,
        "maxLength": maximum,
    }
    if pattern is not None:
        result["pattern"] = pattern
    return result


def _safe_id() -> dict[str, object]:
    return _string(maximum=80, pattern=SAFE_ID_PATTERN)


def _object(
    schema_id: str,
    properties: dict[str, object],
    *,
    required: tuple[str, ...] = (),
) -> dict[str, object]:
    result: dict[str, object] = {
        "$schema": JSON_SCHEMA_DIALECT,
        "$id": f"urn:dyro:bridge:{schema_id}",
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
    }
    if required:
        result["required"] = list(required)
    return result


def _strict_object(
    properties: dict[str, object], *, required: tuple[str, ...] = ()
) -> dict[str, object]:
    result: dict[str, object] = {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
    }
    if required:
        result["required"] = list(required)
    return result


def _workspace_selector() -> dict[str, object]:
    return {
        "workspace": _nullable_string(maximum=80, minimum=1, pattern=SAFE_ID_PATTERN),
        "start": _nullable_string(maximum=4096, minimum=1, pattern=r"^(?!~).+$"),
    }


def _workspace_ref() -> dict[str, object]:
    return _strict_object(
        {
            "id": _string(pattern=r"^workspace:[0-9a-f]{64}$"),
            "name": _string(maximum=80),
            "profile_schema_version": {"type": "integer", "minimum": 1},
        },
        required=("id", "name", "profile_schema_version"),
    )


def _operation_capability() -> dict[str, object]:
    return _strict_object(
        {
            "operation": _string(maximum=128),
            "kind": {"enum": ["inspect", "plan"]},
            "maximum_risk": {"enum": ["R0", "PLAN"]},
            "available": {"type": "boolean"},
            "operation_schema_version": {"type": "integer", "minimum": 1},
            "planner_revision": _nullable_string(maximum=128),
        },
        required=(
            "operation",
            "kind",
            "maximum_risk",
            "available",
            "operation_schema_version",
            "planner_revision",
        ),
    )


def _operation_schema(
    operation_id: str,
    schema_version: int,
    input_schema: dict[str, object],
    output_schema: dict[str, object],
) -> OperationSchema:
    return OperationSchema(
        operation_id=operation_id,
        schema_version=schema_version,
        input_schema_id=f"{operation_id}.input.v{schema_version}",
        output_schema_id=f"{operation_id}.output.v{schema_version}",
        _input_json=canonical_json_text(input_schema),
        _output_json=canonical_json_text(output_schema),
    )


def _empty_input(operation: str) -> dict[str, object]:
    return _object(f"{operation}.input.v1", {})


_WORKSPACE_RESOLVE_INPUT = _object(
    "workspace.resolve.input.v1",
    _workspace_selector(),
)

_WORKSPACE_RESOLVE_OUTPUT = _object(
    "workspace.resolve.output.v1",
    {
        "workspace": _workspace_ref(),
        "resolution_source": {"enum": ["explicit", "local", "default", "unique"]},
        "health": {"enum": ["available", "degraded"]},
    },
    required=("workspace", "resolution_source", "health"),
)

_AVAILABLE_WORKSPACE_RECORD = _strict_object(
    {
        "registry_alias": _safe_id(),
        "workspace": _workspace_ref(),
        "health": {"enum": ["available", "degraded"]},
        "default": {"type": "boolean"},
        "failure_code": {"type": "null"},
    },
    required=("registry_alias", "workspace", "health", "default", "failure_code"),
)

_UNAVAILABLE_WORKSPACE_RECORD = _strict_object(
    {
        "registry_alias": _safe_id(),
        "health": {"const": "unavailable"},
        "default": {"type": "boolean"},
        "failure_code": _string(maximum=128),
    },
    required=("registry_alias", "health", "default", "failure_code"),
)

_WORKSPACE_LIST_OUTPUT = _object(
    "workspace.list.output.v1",
    {
        "workspaces": {
            "type": "array",
            "maxItems": 100,
            "items": {
                "oneOf": [
                    _AVAILABLE_WORKSPACE_RECORD,
                    _UNAVAILABLE_WORKSPACE_RECORD,
                ]
            },
        }
    },
    required=("workspaces",),
)

_OBSERVED_ITEM = _strict_object(
    {
        "id": _string(maximum=128),
        "status": _nullable_string(maximum=128),
        "integration_inspection": {"enum": ["complete", "not_inspected", "partial"]},
    },
    required=("id", "status", "integration_inspection"),
)

_WORKSPACE_OBSERVE_OUTPUT = _object(
    "workspace.observe.output.v1",
    {
        "workspace": _workspace_ref(),
        "observed_at": _string(maximum=64),
        "capture_id": _string(maximum=128),
        "workspace_revision": _string(pattern=r"^sha256:[0-9a-f]{64}$"),
        "completeness": {"enum": ["complete", "partial"]},
        "integration_inspection": {"enum": ["complete", "not_inspected", "partial"]},
        "lines": {"type": "array", "maxItems": 100, "items": _OBSERVED_ITEM},
        "tasks": {"type": "array", "maxItems": 100, "items": _OBSERVED_ITEM},
        "objectives": {"type": "array", "maxItems": 100, "items": _OBSERVED_ITEM},
        "failures": {
            "type": "array",
            "maxItems": 100,
            "items": _strict_object(
                {
                    "component": _string(maximum=128),
                    "code": _string(maximum=128),
                },
                required=("component", "code"),
            ),
        },
    },
    required=(
        "workspace",
        "observed_at",
        "capture_id",
        "workspace_revision",
        "completeness",
        "integration_inspection",
        "lines",
        "tasks",
        "objectives",
        "failures",
    ),
)

_PLAN_ACTION = _strict_object(
    {
        "kind": _string(maximum=128),
        "subject_id": _string(maximum=128),
        "reason": _string(maximum=128),
    },
    required=("kind", "subject_id", "reason"),
)

_PLAN_EFFECT = _strict_object(
    {
        "kind": _string(maximum=128),
        "subject_id": _string(maximum=128),
    },
    required=("kind", "subject_id"),
)

_WARNING = _strict_object(
    {
        "code": _string(maximum=128),
        "message": _string(maximum=4096),
    },
    required=("code", "message"),
)

_OBJECTIVE_PLAN_INPUT = _object(
    "objective.plan.input.v1",
    {
        **_workspace_selector(),
        "objective_id": _safe_id(),
    },
    required=("objective_id",),
)

_OBJECTIVE_PLAN_OUTPUT = _object(
    "objective.plan.output.v1",
    {
        "executable": {"const": False},
        "authorization": {"const": "none"},
        "operation": {"const": "objective.plan"},
        "operation_schema_version": {"const": 1},
        "planner_revision": {"const": "objective-plan/1"},
        "workspace": _strict_object(
            {
                "id": _string(pattern=r"^workspace:[0-9a-f]{64}$"),
                "config_sha256": _string(pattern=r"^sha256:[0-9a-f]{64}$"),
            },
            required=("id", "config_sha256"),
        ),
        "normalized_input": _strict_object(
            {"objective_id": _safe_id()}, required=("objective_id",)
        ),
        "read_set": _strict_object(
            {
                "objective_revision": {"type": "integer", "minimum": 1},
                "contract_sha256": _string(pattern=r"^sha256:[0-9a-f]{64}$"),
                "scope_sha256": _string(pattern=r"^sha256:[0-9a-f]{64}$"),
                "event_sha256": _string(pattern=r"^sha256:[0-9a-f]{64}$"),
                "integration_inspection": {"const": "complete"},
            },
            required=(
                "objective_revision",
                "contract_sha256",
                "scope_sha256",
                "event_sha256",
                "integration_inspection",
            ),
        ),
        "projection": _strict_object(
            {
                "selected_actions": {
                    "type": "array",
                    "maxItems": 100,
                    "items": _PLAN_ACTION,
                },
                "blocked": {"type": "array", "maxItems": 100, "items": _PLAN_ACTION},
                "attention": {"type": "array", "maxItems": 100, "items": _PLAN_ACTION},
            },
            required=("selected_actions", "blocked", "attention"),
        ),
        "effects": {"type": "array", "maxItems": 100, "items": _PLAN_EFFECT},
        "warnings": {"type": "array", "maxItems": 64, "items": _WARNING},
        "maximum_risk": {"const": "PLAN"},
        "effective_risk": {"const": "PLAN"},
        "expires_at": _string(maximum=64),
        "plan_sha256": _string(pattern=r"^sha256:[0-9a-f]{64}$"),
    },
    required=(
        "executable",
        "authorization",
        "operation",
        "operation_schema_version",
        "planner_revision",
        "workspace",
        "normalized_input",
        "read_set",
        "projection",
        "effects",
        "warnings",
        "maximum_risk",
        "effective_risk",
        "expires_at",
        "plan_sha256",
    ),
)


def _identified_input(operation: str, field: str) -> dict[str, object]:
    return _object(
        f"{operation}.input.v1",
        {**_workspace_selector(), field: _safe_id()},
        required=(field,),
    )


_SUMMARY_ITEM = _strict_object(
    {
        "id": _safe_id(),
        "status": _string(maximum=128),
        "integration_inspection": {"enum": ["complete", "not_inspected", "partial"]},
    },
    required=("id", "status", "integration_inspection"),
)


def _summary_collection_output(operation: str, field: str) -> dict[str, object]:
    return _object(
        f"{operation}.output.v1",
        {
            field: {
                "type": "array",
                "maxItems": 100,
                "items": _SUMMARY_ITEM,
            }
        },
        required=(field,),
    )


def _explanation_output(operation: str, subject_field: str) -> dict[str, object]:
    return _object(
        f"{operation}.output.v1",
        {
            subject_field: _safe_id(),
            "summary": _string(maximum=4096),
            "reasons": {
                "type": "array",
                "maxItems": 100,
                "items": _string(maximum=512),
            },
            "integration_inspection": {
                "enum": ["complete", "not_inspected", "partial"]
            },
        },
        required=(subject_field, "summary", "reasons", "integration_inspection"),
    )


_GRAPH_NODE = _strict_object(
    {"id": _safe_id(), "status": _nullable_string(maximum=128)},
    required=("id", "status"),
)

_GRAPH_EDGE = _strict_object(
    {
        "source": _safe_id(),
        "target": _safe_id(),
        "kind": _string(maximum=128),
    },
    required=("source", "target", "kind"),
)

_GRAPH_ISSUE = _strict_object(
    {"code": _string(maximum=128), "subject_id": _safe_id()},
    required=("code", "subject_id"),
)


def _graph_output(operation: str) -> dict[str, object]:
    return _object(
        f"{operation}.output.v1",
        {
            "nodes": {"type": "array", "maxItems": 100, "items": _GRAPH_NODE},
            "edges": {"type": "array", "maxItems": 100, "items": _GRAPH_EDGE},
            "issues": {"type": "array", "maxItems": 100, "items": _GRAPH_ISSUE},
            "integration_inspection": {
                "enum": ["complete", "not_inspected", "partial"]
            },
        },
        required=("nodes", "edges", "issues", "integration_inspection"),
    )


def _objective_read_set() -> dict[str, object]:
    return _strict_object(
        {
            "objective_revision": {"type": "integer", "minimum": 1},
            "contract_sha256": _string(pattern=r"^sha256:[0-9a-f]{64}$"),
            "scope_sha256": _string(pattern=r"^sha256:[0-9a-f]{64}$"),
            "event_sha256": _string(pattern=r"^sha256:[0-9a-f]{64}$"),
            "integration_inspection": {"const": "complete"},
        },
        required=(
            "objective_revision",
            "contract_sha256",
            "scope_sha256",
            "event_sha256",
            "integration_inspection",
        ),
    )


_OBJECTIVE_EXPLAIN_PROJECTION = _strict_object(
    {
        "summary": _string(maximum=4096),
        "reasons": {
            "type": "array",
            "maxItems": 100,
            "items": _string(maximum=512),
        },
        "selected_actions": {"type": "array", "maxItems": 100, "items": _PLAN_ACTION},
        "blocked": {"type": "array", "maxItems": 100, "items": _PLAN_ACTION},
        "attention": {"type": "array", "maxItems": 100, "items": _PLAN_ACTION},
    },
    required=("summary", "reasons", "selected_actions", "blocked", "attention"),
)

_OBJECTIVE_GRAPH_PROJECTION = _strict_object(
    {
        "nodes": {"type": "array", "maxItems": 100, "items": _GRAPH_NODE},
        "edges": {"type": "array", "maxItems": 100, "items": _GRAPH_EDGE},
        "issues": {"type": "array", "maxItems": 100, "items": _GRAPH_ISSUE},
    },
    required=("nodes", "edges", "issues"),
)

_OBJECTIVE_TICK_PROJECTION = _strict_object(
    {
        "selected_actions": {"type": "array", "maxItems": 100, "items": _PLAN_ACTION},
        "blocked": {"type": "array", "maxItems": 100, "items": _PLAN_ACTION},
        "attention": {"type": "array", "maxItems": 100, "items": _PLAN_ACTION},
        "tick_wave": {"type": "array", "maxItems": 100, "items": _PLAN_ACTION},
    },
    required=("selected_actions", "blocked", "attention", "tick_wave"),
)

_OBJECTIVE_ATTENTION_PROJECTION = _strict_object(
    {
        "attention": {"type": "array", "maxItems": 100, "items": _PLAN_ACTION},
    },
    required=("attention",),
)


def _plan_envelope_output(
    operation: str,
    planner_revision: str,
    projection: dict[str, object],
) -> dict[str, object]:
    return _object(
        f"{operation}.output.v1",
        {
            "executable": {"const": False},
            "authorization": {"const": "none"},
            "operation": {"const": operation},
            "operation_schema_version": {"const": 1},
            "planner_revision": {"const": planner_revision},
            "workspace": _strict_object(
                {
                    "id": _string(pattern=r"^workspace:[0-9a-f]{64}$"),
                    "config_sha256": _string(pattern=r"^sha256:[0-9a-f]{64}$"),
                },
                required=("id", "config_sha256"),
            ),
            "normalized_input": _strict_object(
                {"objective_id": _safe_id()}, required=("objective_id",)
            ),
            "read_set": _objective_read_set(),
            "projection": projection,
            "effects": {"type": "array", "maxItems": 100, "items": _PLAN_EFFECT},
            "warnings": {"type": "array", "maxItems": 64, "items": _WARNING},
            "maximum_risk": {"const": "PLAN"},
            "effective_risk": {"const": "PLAN"},
            "expires_at": _string(maximum=64),
            "plan_sha256": _string(pattern=r"^sha256:[0-9a-f]{64}$"),
        },
        required=(
            "executable",
            "authorization",
            "operation",
            "operation_schema_version",
            "planner_revision",
            "workspace",
            "normalized_input",
            "read_set",
            "projection",
            "effects",
            "warnings",
            "maximum_risk",
            "effective_risk",
            "expires_at",
            "plan_sha256",
        ),
    )


_SCHEMAS = tuple(
    sorted(
        (
            _operation_schema(
                "bridge.hello",
                1,
                _empty_input("bridge.hello"),
                _object(
                    "bridge.hello.output.v1",
                    {
                        "dyro_version": _string(maximum=128),
                        "bridge_version": {"const": "1.0"},
                        "server_protocol": _strict_object(
                            {
                                "major": {"const": 1},
                                "minor": {"type": "integer", "minimum": 0},
                            },
                            required=("major", "minor"),
                        ),
                    },
                    required=("dyro_version", "bridge_version", "server_protocol"),
                ),
            ),
            _operation_schema(
                "bridge.capabilities.compact",
                1,
                _empty_input("bridge.capabilities.compact"),
                _object(
                    "bridge.capabilities.compact.output.v1",
                    {
                        "operations": {
                            "type": "array",
                            "maxItems": 64,
                            "items": _operation_capability(),
                        },
                        "capabilities_digest": _string(
                            pattern=r"^sha256:[0-9a-f]{64}$"
                        ),
                    },
                    required=("operations", "capabilities_digest"),
                ),
            ),
            _operation_schema(
                "bridge.operation.schema",
                1,
                _object(
                    "bridge.operation.schema.input.v1",
                    {"operation": _string(maximum=128, pattern=OPERATION_ID_PATTERN)},
                    required=("operation",),
                ),
                _object(
                    "bridge.operation.schema.output.v1",
                    {
                        "operation": _string(maximum=128, pattern=OPERATION_ID_PATTERN),
                        "operation_schema_version": {"type": "integer", "minimum": 1},
                        "input_schema": {"type": "object"},
                        "output_schema": {"type": "object"},
                        "schema_digest": _string(pattern=r"^sha256:[0-9a-f]{64}$"),
                    },
                    required=(
                        "operation",
                        "operation_schema_version",
                        "input_schema",
                        "output_schema",
                        "schema_digest",
                    ),
                ),
            ),
            _operation_schema(
                "line.list",
                1,
                _object("line.list.input.v1", _workspace_selector()),
                _summary_collection_output("line.list", "lines"),
            ),
            _operation_schema(
                "task.list",
                1,
                _object(
                    "task.list.input.v1",
                    {
                        **_workspace_selector(),
                        "line_id": _nullable_string(
                            maximum=80,
                            minimum=1,
                            pattern=SAFE_ID_PATTERN,
                        ),
                    },
                ),
                _summary_collection_output("task.list", "tasks"),
            ),
            _operation_schema(
                "task.explain",
                1,
                _identified_input("task.explain", "task_id"),
                _explanation_output("task.explain", "task_id"),
            ),
            _operation_schema(
                "task.graph",
                1,
                _object(
                    "task.graph.input.v1",
                    {
                        **_workspace_selector(),
                        "task_id": _nullable_string(
                            maximum=80,
                            minimum=1,
                            pattern=SAFE_ID_PATTERN,
                        ),
                    },
                ),
                _graph_output("task.graph"),
            ),
            _operation_schema(
                "task.gate_definitions.get",
                1,
                _identified_input("task.gate_definitions.get", "task_id"),
                _object(
                    "task.gate_definitions.get.output.v1",
                    {
                        "task_id": _safe_id(),
                        "gates": {
                            "type": "array",
                            "maxItems": 64,
                            "items": _strict_object(
                                {
                                    "name": _safe_id(),
                                    "required": {"type": "boolean"},
                                    "description": _nullable_string(maximum=512),
                                },
                                required=("name", "required", "description"),
                            ),
                        },
                    },
                    required=("task_id", "gates"),
                ),
            ),
            _operation_schema(
                "workspace.resolve",
                1,
                _WORKSPACE_RESOLVE_INPUT,
                _WORKSPACE_RESOLVE_OUTPUT,
            ),
            _operation_schema(
                "workspace.list",
                1,
                _empty_input("workspace.list"),
                _WORKSPACE_LIST_OUTPUT,
            ),
            _operation_schema(
                "workspace.observe",
                1,
                _object("workspace.observe.input.v1", _workspace_selector()),
                _WORKSPACE_OBSERVE_OUTPUT,
            ),
            _operation_schema(
                "objective.list",
                1,
                _object("objective.list.input.v1", _workspace_selector()),
                _summary_collection_output("objective.list", "objectives"),
            ),
            _operation_schema(
                "objective.status",
                1,
                _identified_input("objective.status", "objective_id"),
                _explanation_output("objective.status", "objective_id"),
            ),
            _operation_schema(
                "objective.plan", 1, _OBJECTIVE_PLAN_INPUT, _OBJECTIVE_PLAN_OUTPUT
            ),
            _operation_schema(
                "objective.explain",
                1,
                _identified_input("objective.explain", "objective_id"),
                _plan_envelope_output(
                    "objective.explain",
                    "objective-explain/1",
                    _OBJECTIVE_EXPLAIN_PROJECTION,
                ),
            ),
            _operation_schema(
                "objective.graph",
                1,
                _identified_input("objective.graph", "objective_id"),
                _plan_envelope_output(
                    "objective.graph",
                    "objective-graph/1",
                    _OBJECTIVE_GRAPH_PROJECTION,
                ),
            ),
            _operation_schema(
                "objective.tick",
                1,
                _identified_input("objective.tick", "objective_id"),
                _plan_envelope_output(
                    "objective.tick",
                    "objective-tick/1",
                    _OBJECTIVE_TICK_PROJECTION,
                ),
            ),
            _operation_schema(
                "objective.attention",
                1,
                _identified_input("objective.attention", "objective_id"),
                _plan_envelope_output(
                    "objective.attention",
                    "objective-attention/1",
                    _OBJECTIVE_ATTENTION_PROJECTION,
                ),
            ),
        ),
        key=lambda item: item.operation_id,
    )
)

_BY_OPERATION = MappingProxyType({item.operation_id: item for item in _SCHEMAS})


def list_operation_schemas() -> tuple[OperationSchema, ...]:
    return _SCHEMAS


def get_operation_schema(operation_id: str) -> OperationSchema:
    try:
        return _BY_OPERATION[operation_id]
    except (KeyError, TypeError) as exc:
        raise ValidationError(
            f"Bridge operation schema 不存在：{operation_id!r}"
        ) from exc


def operation_schema_digest(operation_id: str) -> str:
    return _operation_schema_digest(get_operation_schema(operation_id))

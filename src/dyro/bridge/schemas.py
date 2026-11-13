"""Per-operation JSON Schema. Fetch one allowlisted ID; reject unknown."""

from __future__ import annotations

from ..errors import ValidationError
from .catalog import build_default_catalog

_OBJECT = {"type": "object", "additionalProperties": False}
_WORKSPACE_SELECTOR = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "workspace": {"type": ["string", "null"]},
        "start": {"type": "string"},
    },
}
_OBJECTIVE_SELECTOR = {
    "type": "object",
    "additionalProperties": False,
    "required": ["objective_id"],
    "properties": {
        "workspace": {"type": ["string", "null"]},
        "start": {"type": "string"},
        "objective_id": {"type": "string"},
    },
}
_TASK_SELECTOR = {
    "type": "object",
    "additionalProperties": False,
    "required": ["task_id"],
    "properties": {
        "workspace": {"type": ["string", "null"]},
        "start": {"type": "string"},
        "task_id": {"type": "string"},
    },
}

_SCHEMAS: dict[str, dict[str, object]] = {
    "bridge.hello": {
        "input": {**_OBJECT, "properties": {}},
        "output": {
            "type": "object",
            "additionalProperties": False,
            "required": ["protocol", "dyro_version", "bridge_version"],
            "properties": {
                "protocol": {"type": "object"},
                "dyro_version": {"type": "string"},
                "bridge_version": {"type": "string"},
            },
        },
    },
    "bridge.capabilities.compact": {
        "input": {**_OBJECT, "properties": {}},
        "output": {
            "type": "object",
            "additionalProperties": False,
            "required": ["schema_version", "operations"],
            "properties": {
                "schema_version": {"type": "integer"},
                "operations": {"type": "array"},
            },
        },
    },
    "bridge.operation.schema": {
        "input": {
            "type": "object",
            "additionalProperties": False,
            "required": ["operation"],
            "properties": {"operation": {"type": "string"}},
        },
        "output": {
            "type": "object",
            "additionalProperties": False,
            "required": ["operation", "schema_version", "input", "output"],
            "properties": {
                "operation": {"type": "string"},
                "schema_version": {"type": "integer"},
                "input": {"type": "object"},
                "output": {"type": "object"},
            },
        },
    },
    "workspace.resolve": {"input": _WORKSPACE_SELECTOR, "output": {**_OBJECT, "properties": {}}},
    "workspace.list": {"input": {**_OBJECT, "properties": {}}, "output": {**_OBJECT, "properties": {}}},
    "workspace.observe": {"input": _WORKSPACE_SELECTOR, "output": {**_OBJECT, "properties": {}}},
    "line.list": {"input": _WORKSPACE_SELECTOR, "output": {**_OBJECT, "properties": {}}},
    "task.list": {"input": _WORKSPACE_SELECTOR, "output": {**_OBJECT, "properties": {}}},
    "task.gate_definitions.get": {"input": _TASK_SELECTOR, "output": {**_OBJECT, "properties": {}}},
    "objective.list": {"input": _WORKSPACE_SELECTOR, "output": {**_OBJECT, "properties": {}}},
    "objective.status": {"input": _OBJECTIVE_SELECTOR, "output": {**_OBJECT, "properties": {}}},
    "objective.plan": {"input": _OBJECTIVE_SELECTOR, "output": {**_OBJECT, "properties": {}}},
    "objective.explain": {"input": _OBJECTIVE_SELECTOR, "output": {**_OBJECT, "properties": {}}},
    "objective.graph": {"input": _OBJECTIVE_SELECTOR, "output": {**_OBJECT, "properties": {}}},
    "objective.tick": {"input": _OBJECTIVE_SELECTOR, "output": {**_OBJECT, "properties": {}}},
    "objective.attention": {"input": _OBJECTIVE_SELECTOR, "output": {**_OBJECT, "properties": {}}},
}


def validate_input(schema: dict[str, object], value: object, *, label: str = "input") -> None:
    expected = schema.get("type", "object")
    allowed = expected if isinstance(expected, list) else [expected]
    if not any(_type_matches(value, item) for item in allowed):
        raise ValidationError(f"{label} 类型无效")
    if "object" not in allowed or not isinstance(value, dict):
        return
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        properties = {}
    if schema.get("additionalProperties") is False:
        unknown = sorted(set(value) - set(properties))
        if unknown:
            raise ValidationError(f"{label} 包含未知字段")
    required = schema.get("required", [])
    if isinstance(required, list):
        missing = [name for name in required if name not in value]
        if missing:
            raise ValidationError(f"{label} 缺少字段")
    for name, item in value.items():
        child = properties.get(name)
        if isinstance(child, dict):
            validate_input(child, item, label=f"{label}.{name}")


def _type_matches(value: object, expected: object) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return False


def operation_schema(
    operation_id: str, *, catalog=None, platform: str | None = None
) -> dict[str, object]:
    catalog = catalog if catalog is not None else build_default_catalog(platform=platform)
    record = catalog.record(operation_id)
    if record is None:
        raise ValidationError(f"未知 operation：{operation_id}")
    schema = _SCHEMAS.get(operation_id)
    if schema is None:
        schema = {
            "input": {**_OBJECT, "properties": {}},
            "output": {**_OBJECT, "properties": {}},
        }
    return {
        "operation": operation_id,
        "schema_version": record.schema_version,
        "availability": record.availability.value,
        "input": schema["input"],
        "output": schema["output"],
    }

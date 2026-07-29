"""Stage 2 IPC protocol versioning (compatible with Stage 1 v1)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..errors import Stage0ValidationError
from ..stage1.protocol import (
    MAX_CWD_CHARS,
    MAX_PROMPT_CHARS,
    MAX_RESPONSE_CHARS,
    AgentCallResponse,
    dumps_strict,
    loads_strict,
    sanitize_text,
)

SUPPORTED_PROTOCOL_VERSIONS = frozenset({1, 2})
MAX_SCHEMA_HINT_CHARS = 64


@dataclass(frozen=True)
class AgentCallRequestV2:
    """Request accepted for protocol versions 1 and 2."""

    protocol_version: int
    call_id: str
    prompt: str
    model: str
    cwd: str
    deadline_ms: int
    schema_hint: str = ""

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> AgentCallRequestV2:
        version = payload.get("protocol_version")
        if type(version) is not int or version not in SUPPORTED_PROTOCOL_VERSIONS:
            raise Stage0ValidationError(
                f"unsupported IPC protocol_version: {version!r}; "
                f"supported={sorted(SUPPORTED_PROTOCOL_VERSIONS)}"
            )
        if payload.get("type") != "agent.call":
            raise Stage0ValidationError("IPC request type must be agent.call")
        call_id = payload.get("call_id")
        prompt = payload.get("prompt")
        model = payload.get("model")
        cwd = payload.get("cwd")
        deadline_ms = payload.get("deadline_ms")
        schema_hint = payload.get("schema_hint", "")
        if not isinstance(call_id, str) or not call_id or len(call_id) > 128:
            raise Stage0ValidationError("call_id is invalid")
        if not isinstance(prompt, str) or not prompt or len(prompt) > MAX_PROMPT_CHARS:
            raise Stage0ValidationError("prompt is invalid")
        if not isinstance(model, str) or not model or len(model) > 64:
            raise Stage0ValidationError("model is invalid")
        if not isinstance(cwd, str) or not cwd or len(cwd) > MAX_CWD_CHARS:
            raise Stage0ValidationError("cwd is invalid")
        if type(deadline_ms) is not int or deadline_ms <= 0 or deadline_ms > 600_000:
            raise Stage0ValidationError("deadline_ms is invalid")
        if version == 1 and "schema_hint" in payload:
            raise Stage0ValidationError("protocol v1 rejects schema_hint")
        if not isinstance(schema_hint, str) or len(schema_hint) > MAX_SCHEMA_HINT_CHARS:
            raise Stage0ValidationError("schema_hint is invalid")
        allowed = {
            "protocol_version",
            "type",
            "call_id",
            "prompt",
            "model",
            "cwd",
            "deadline_ms",
        }
        if version >= 2:
            allowed.add("schema_hint")
        unexpected = set(payload) - allowed
        if unexpected:
            raise Stage0ValidationError(
                f"IPC request contains unknown fields: {sorted(unexpected)}"
            )
        return cls(
            protocol_version=version,
            call_id=call_id,
            prompt=prompt,
            model=model,
            cwd=cwd,
            deadline_ms=deadline_ms,
            schema_hint=schema_hint if version >= 2 else "",
        )

    def to_mapping(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "protocol_version": self.protocol_version,
            "type": "agent.call",
            "call_id": self.call_id,
            "prompt": self.prompt,
            "model": self.model,
            "cwd": self.cwd,
            "deadline_ms": self.deadline_ms,
        }
        if self.protocol_version >= 2:
            payload["schema_hint"] = self.schema_hint
        return payload


__all__ = [
    "SUPPORTED_PROTOCOL_VERSIONS",
    "AgentCallRequestV2",
    "AgentCallResponse",
    "dumps_strict",
    "loads_strict",
    "sanitize_text",
    "MAX_PROMPT_CHARS",
    "MAX_RESPONSE_CHARS",
]

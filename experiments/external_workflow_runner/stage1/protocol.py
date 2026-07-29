"""Narrow Agent Broker IPC protocol (schema-validated JSON lines)."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import re
from typing import Any, Mapping

from ..errors import Stage0ValidationError


PROTOCOL_VERSION = 1
MAX_PROMPT_CHARS = 16_384
MAX_RESPONSE_CHARS = 16_384
MAX_MODEL_CHARS = 64
MAX_CALL_ID_CHARS = 128
MAX_CWD_CHARS = 512
_CALL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,63}$")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    decoded: dict[str, object] = {}
    for name, value in pairs:
        if name in decoded:
            raise Stage0ValidationError(f"IPC JSON contains a duplicate key: {name}")
        decoded[name] = value
    return decoded


def _reject_non_finite_number(value: str) -> object:
    raise Stage0ValidationError(f"IPC JSON contains a non-finite number: {value}")


def loads_strict(text: str) -> dict[str, object]:
    try:
        decoded = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite_number,
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise Stage0ValidationError("IPC payload is not strict UTF-8 JSON") from exc
    if not isinstance(decoded, dict):
        raise Stage0ValidationError("IPC payload must be a JSON object")
    return decoded


def dumps_strict(payload: Mapping[str, object]) -> str:
    try:
        return json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise Stage0ValidationError("IPC payload is not JSON-serializable") from exc


@dataclass(frozen=True)
class AgentCallRequest:
    call_id: str
    prompt: str
    model: str
    cwd: str
    deadline_ms: int

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> AgentCallRequest:
        if payload.get("protocol_version") != PROTOCOL_VERSION:
            raise Stage0ValidationError("unsupported IPC protocol_version")
        if payload.get("type") != "agent.call":
            raise Stage0ValidationError("IPC request type must be agent.call")
        call_id = payload.get("call_id")
        prompt = payload.get("prompt")
        model = payload.get("model")
        cwd = payload.get("cwd")
        deadline_ms = payload.get("deadline_ms")
        if not isinstance(call_id, str) or not _CALL_ID.fullmatch(call_id):
            raise Stage0ValidationError("call_id is invalid")
        if not isinstance(prompt, str) or not prompt or len(prompt) > MAX_PROMPT_CHARS:
            raise Stage0ValidationError("prompt is invalid")
        if "\x00" in prompt:
            raise Stage0ValidationError("prompt contains NUL")
        if not isinstance(model, str) or not _MODEL.fullmatch(model):
            raise Stage0ValidationError("model is invalid")
        if not isinstance(cwd, str) or not cwd or len(cwd) > MAX_CWD_CHARS:
            raise Stage0ValidationError("cwd is invalid")
        if "\x00" in cwd or ".." in cwd.split("/"):
            raise Stage0ValidationError("cwd is not a safe reference")
        if type(deadline_ms) is not int or deadline_ms <= 0 or deadline_ms > 600_000:
            raise Stage0ValidationError("deadline_ms is invalid")
        unexpected = set(payload) - {
            "protocol_version",
            "type",
            "call_id",
            "prompt",
            "model",
            "cwd",
            "deadline_ms",
        }
        if unexpected:
            raise Stage0ValidationError(
                f"IPC request contains unknown fields: {sorted(unexpected)}"
            )
        return cls(
            call_id=call_id,
            prompt=prompt,
            model=model,
            cwd=cwd,
            deadline_ms=deadline_ms,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "type": "agent.call",
            "call_id": self.call_id,
            "prompt": self.prompt,
            "model": self.model,
            "cwd": self.cwd,
            "deadline_ms": self.deadline_ms,
        }


@dataclass(frozen=True)
class AgentCallResponse:
    call_id: str
    status: str
    text: str
    error_code: str = ""

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> AgentCallResponse:
        if payload.get("protocol_version") != PROTOCOL_VERSION:
            raise Stage0ValidationError("unsupported IPC protocol_version")
        if payload.get("type") != "agent.result":
            raise Stage0ValidationError("IPC response type must be agent.result")
        call_id = payload.get("call_id")
        status = payload.get("status")
        text = payload.get("text")
        error_code = payload.get("error_code", "")
        if not isinstance(call_id, str) or not _CALL_ID.fullmatch(call_id):
            raise Stage0ValidationError("response call_id is invalid")
        if status not in {"ok", "error", "timeout"}:
            raise Stage0ValidationError("response status is invalid")
        if not isinstance(text, str) or len(text) > MAX_RESPONSE_CHARS:
            raise Stage0ValidationError("response text is invalid")
        if "\x00" in text:
            raise Stage0ValidationError("response text contains NUL")
        if not isinstance(error_code, str) or len(error_code) > 64:
            raise Stage0ValidationError("response error_code is invalid")
        unexpected = set(payload) - {
            "protocol_version",
            "type",
            "call_id",
            "status",
            "text",
            "error_code",
        }
        if unexpected:
            raise Stage0ValidationError(
                f"IPC response contains unknown fields: {sorted(unexpected)}"
            )
        return cls(
            call_id=call_id,
            status=status,
            text=text,
            error_code=error_code,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "type": "agent.result",
            "call_id": self.call_id,
            "status": self.status,
            "text": self.text,
            "error_code": self.error_code,
        }


def sanitize_text(value: str, *, max_chars: int = MAX_RESPONSE_CHARS) -> str:
    """Remove control characters and truncate for telemetry/response surfaces."""
    cleaned = "".join(
        ch if (ch == "\n" or ch == "\t" or ord(ch) >= 32) and ch != "\x00" else " "
        for ch in value
    )
    # Redact common secret-shaped tokens before persistence.
    for needle in (
        "BEGIN PRIVATE KEY",
        "execution-key",
        "DYRO_EXECUTION_KEY",
        "sk-",
        "AKIA",
    ):
        cleaned = cleaned.replace(needle, "[REDACTED]")
    if len(cleaned) > max_chars:
        cleaned = cleaned[: max_chars - 16] + "…[truncated]"
    return cleaned


def positive_finite(value: object, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise Stage0ValidationError(f"{label} must be a positive finite number")
    return float(value)

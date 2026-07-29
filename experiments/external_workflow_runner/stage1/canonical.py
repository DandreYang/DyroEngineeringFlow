"""Canonical input binding for Stage 1 workflow runs."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Mapping, Sequence

from ..errors import Stage0ValidationError
from .protocol import dumps_strict, loads_strict


def canonical_sha256(payload: Mapping[str, object]) -> str:
    encoded = dumps_strict(payload).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class CanonicalInput:
    workflow_run_id: str
    task_id: str
    runner_id: str
    claim_generation: int
    branches: tuple[str, ...]
    artifact_repository: str
    artifact_path: str
    model: str
    max_agent_calls: int

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "workflow_run_id": self.workflow_run_id,
            "task_id": self.task_id,
            "runner_id": self.runner_id,
            "claim_generation": self.claim_generation,
            "branches": list(self.branches),
            "artifact_repository": self.artifact_repository,
            "artifact_path": self.artifact_path,
            "model": self.model,
            "max_agent_calls": self.max_agent_calls,
        }

    def digest(self) -> str:
        return canonical_sha256(self.to_mapping())

    def write(self, path: Path) -> str:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        text = dumps_strict(self.to_mapping()) + "\n"
        path.write_text(text, encoding="utf-8")
        path.chmod(0o600)
        return self.digest()

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> CanonicalInput:
        if payload.get("schema_version") != 1:
            raise Stage0ValidationError("canonical input schema_version is unsupported")
        branches = payload.get("branches")
        if not isinstance(branches, list) or not branches:
            raise Stage0ValidationError("canonical branches must be a non-empty list")
        if any(
            not isinstance(item, str) or not item or len(item) > 128
            for item in branches
        ):
            raise Stage0ValidationError("canonical branches contain an invalid id")
        for field in (
            "workflow_run_id",
            "task_id",
            "runner_id",
            "artifact_repository",
            "artifact_path",
            "model",
        ):
            value = payload.get(field)
            if not isinstance(value, str) or not value or len(value) > 256:
                raise Stage0ValidationError(f"canonical field is invalid: {field}")
        generation = payload.get("claim_generation")
        max_agent_calls = payload.get("max_agent_calls")
        if type(generation) is not int or generation < 1:
            raise Stage0ValidationError("claim_generation is invalid")
        if type(max_agent_calls) is not int or not 1 <= max_agent_calls <= 16:
            raise Stage0ValidationError("max_agent_calls is invalid")
        return cls(
            workflow_run_id=str(payload["workflow_run_id"]),
            task_id=str(payload["task_id"]),
            runner_id=str(payload["runner_id"]),
            claim_generation=generation,
            branches=tuple(str(item) for item in branches),
            artifact_repository=str(payload["artifact_repository"]),
            artifact_path=str(payload["artifact_path"]),
            model=str(payload["model"]),
            max_agent_calls=max_agent_calls,
        )

    @classmethod
    def read(cls, path: Path) -> CanonicalInput:
        try:
            payload = loads_strict(Path(path).read_text(encoding="utf-8").strip())
        except (OSError, UnicodeDecodeError) as exc:
            raise Stage0ValidationError("canonical input is unreadable") from exc
        return cls.from_mapping(payload)


def expected_branches_map(branch_ids: Sequence[str]) -> dict[str, bool]:
    if not branch_ids:
        raise Stage0ValidationError("branch list must not be empty")
    return {branch_id: True for branch_id in branch_ids}

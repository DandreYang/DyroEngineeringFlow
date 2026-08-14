"""Strict Batch V1 request and normalized execution-plan contracts."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from dyro.canonical import canonical_json_bytes

from .context_guard import assert_content_allowed
from .errors import DispatchValidationError
from .task_contract import TASK_FIELDS, TaskContract, parse_task_contract


BATCH_SCHEMA_VERSION = 1
BATCH_PLAN_KIND = "local-agent-dispatch-batch-plan"
MIN_BATCH_MEMBERS = 2
MAX_BATCH_MEMBERS = 4
MAX_TIMEOUT_SECONDS = 3600.0
MAX_BATCH_REQUEST_BYTES = 1024 * 1024
MAX_REQUEST_ID_LENGTH = 128
MAX_ROLE_ID_LENGTH = 64

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_HEAD = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_BATCH_FIELDS = frozenset(
    {"schema_version", "request_id", "strategy", "members"}
)
_MEMBER_FIELDS = frozenset({"role_id", "timeout_seconds", "contract"})
_CONTRACT_FIELDS = frozenset(
    {
        "schema_version",
        "backend",
        "mode",
        "strict",
        "allow_unconfined_provider",
        "allow_offline_simulation",
        "files",
        "task",
    }
)
_TASK_FIELDS = frozenset(TASK_FIELDS)
_CANONICAL_PLAN_FIELDS = frozenset(
    {
        "schema_version",
        "kind",
        "project_root",
        "request_id",
        "strategy",
        "effects",
        "members",
    }
)
_DISPLAY_PLAN_FIELDS = _CANONICAL_PLAN_FIELDS | {"plan_sha256"}
_PLAN_MEMBER_FIELDS = frozenset(
    {
        "role_id",
        "resolved_backend",
        "context_file_count",
        "context_sha256",
        "base_head",
        "execution_profile",
        "timeout_seconds",
        "normalized_contract",
    }
)
_EFFECT_FIELDS = frozenset(
    {
        "creates_local_state",
        "starts_provider_processes",
        "may_use_network_or_bill",
        "writes_source_worktree",
        "returns_patch_only",
    }
)


def _require_exact_fields(
    payload: Mapping[str, Any],
    expected: frozenset[str],
    *,
    label: str,
) -> None:
    actual = set(payload)
    unknown = sorted(actual - expected)
    if unknown:
        raise DispatchValidationError(
            f"{label} contains unknown fields: {', '.join(unknown)}"
        )
    missing = sorted(expected - actual)
    if missing:
        raise DispatchValidationError(
            f"{label} is missing required fields: {', '.join(missing)}"
        )


def _require_safe_id(value: object, *, label: str, maximum: int) -> str:
    if type(value) is not str or not value or len(value) > maximum:
        raise DispatchValidationError(
            f"{label} must be a non-empty string of at most {maximum} characters"
        )
    if _SAFE_ID.fullmatch(value) is None:
        raise DispatchValidationError(
            f"{label} may contain only ASCII letters, digits, '.', '_', and '-' "
            "and must start with a letter or digit"
        )
    return value


def _require_timeout(value: object, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DispatchValidationError(f"{label} must be a finite positive number")
    timeout = float(value)
    if not math.isfinite(timeout) or timeout <= 0 or timeout > MAX_TIMEOUT_SECONDS:
        raise DispatchValidationError(
            f"{label} must be finite, positive, and at most {MAX_TIMEOUT_SECONDS:g}"
        )
    return timeout


def _require_sha256(value: object, *, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise DispatchValidationError(f"{label} must be a lowercase SHA-256 hex digest")
    return value


def _strict_task_contract(payload: object, *, label: str) -> TaskContract:
    if not isinstance(payload, Mapping):
        raise DispatchValidationError(f"{label} must be an object")
    _require_exact_fields(payload, _CONTRACT_FIELDS, label=label)
    if (
        type(payload.get("schema_version")) is not int
        or payload.get("schema_version") != BATCH_SCHEMA_VERSION
    ):
        raise DispatchValidationError(f"{label}.schema_version must be 1")
    task = payload.get("task")
    if not isinstance(task, Mapping):
        raise DispatchValidationError(f"{label}.task must be an object")
    _require_exact_fields(task, _TASK_FIELDS, label=f"{label}.task")
    contract = parse_task_contract(payload)
    if contract.backend == "echo":
        raise DispatchValidationError("batch members cannot use the echo backend")
    if contract.allow_offline_simulation:
        raise DispatchValidationError(
            "batch members cannot allow offline simulation"
        )
    return contract


@dataclass(frozen=True)
class BatchMemberRequest:
    role_id: str
    timeout_seconds: float
    contract: TaskContract

    def to_mapping(self) -> dict[str, object]:
        return {
            "role_id": self.role_id,
            "timeout_seconds": self.timeout_seconds,
            "contract": self.contract.to_mapping(),
        }


@dataclass(frozen=True)
class BatchRequest:
    schema_version: int
    request_id: str
    strategy: str
    members: tuple[BatchMemberRequest, ...]

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "strategy": self.strategy,
            "members": [member.to_mapping() for member in self.members],
        }


def parse_batch_request(payload: Mapping[str, Any]) -> BatchRequest:
    """Parse a complete, fail-closed Batch V1 request."""
    if not isinstance(payload, Mapping):
        raise DispatchValidationError("batch request must be an object")
    _require_exact_fields(payload, _BATCH_FIELDS, label="batch request")
    if (
        type(payload.get("schema_version")) is not int
        or payload.get("schema_version") != BATCH_SCHEMA_VERSION
    ):
        raise DispatchValidationError("batch schema_version must be 1")
    request_id = _require_safe_id(
        payload.get("request_id"),
        label="batch request_id",
        maximum=MAX_REQUEST_ID_LENGTH,
    )
    strategy = payload.get("strategy")
    if strategy != "independent":
        raise DispatchValidationError("batch strategy must be independent")
    raw_members = payload.get("members")
    if not isinstance(raw_members, Sequence) or isinstance(
        raw_members, (str, bytes)
    ):
        raise DispatchValidationError("batch members must be a list")
    if not MIN_BATCH_MEMBERS <= len(raw_members) <= MAX_BATCH_MEMBERS:
        raise DispatchValidationError("batch must contain between 2 and 4 members")

    members: list[BatchMemberRequest] = []
    roles: set[str] = set()
    edit_count = 0
    for index, raw_member in enumerate(raw_members):
        label = f"batch members[{index}]"
        if not isinstance(raw_member, Mapping):
            raise DispatchValidationError(f"{label} must be an object")
        _require_exact_fields(raw_member, _MEMBER_FIELDS, label=label)
        role_id = _require_safe_id(
            raw_member.get("role_id"),
            label=f"{label}.role_id",
            maximum=MAX_ROLE_ID_LENGTH,
        )
        if role_id in roles:
            raise DispatchValidationError(f"batch role_id must be unique: {role_id}")
        roles.add(role_id)
        timeout = _require_timeout(
            raw_member.get("timeout_seconds"),
            label=f"{label}.timeout_seconds",
        )
        contract = _strict_task_contract(
            raw_member.get("contract"), label=f"{label}.contract"
        )
        if contract.mode == "edit":
            edit_count += 1
            if edit_count > 1:
                raise DispatchValidationError(
                    "batch may contain at most one edit-mode member"
                )
        members.append(
            BatchMemberRequest(
                role_id=role_id,
                timeout_seconds=timeout,
                contract=contract,
            )
        )
    request = BatchRequest(
        schema_version=BATCH_SCHEMA_VERSION,
        request_id=request_id,
        strategy="independent",
        members=tuple(members),
    )
    if len(canonical_json_bytes(request.to_mapping())) > MAX_BATCH_REQUEST_BYTES:
        raise DispatchValidationError(
            f"batch request exceeds {MAX_BATCH_REQUEST_BYTES} bytes"
        )
    return request


def effects_for_members(
    members: Sequence[BatchMemberRequest | BatchMemberPlan],
) -> dict[str, object]:
    edit = any(
        (
            member.contract.mode
            if isinstance(member, BatchMemberRequest)
            else str(member.normalized_contract.get("mode") or "")
        )
        == "edit"
        for member in members
    )
    return {
        "creates_local_state": True,
        "starts_provider_processes": len(members),
        "may_use_network_or_bill": True,
        "writes_source_worktree": False,
        "returns_patch_only": edit,
    }


def canonical_project_root(project_root: str | Path) -> str:
    text = str(project_root)
    if not text.strip():
        raise DispatchValidationError("batch project_root must be non-empty")
    return str(Path(project_root).expanduser().resolve())


@dataclass(frozen=True)
class BatchMemberPlan:
    role_id: str
    resolved_backend: str
    context_file_count: int
    context_sha256: str
    base_head: str | None
    execution_profile: Mapping[str, str]
    timeout_seconds: float
    normalized_contract: Mapping[str, object]

    def __post_init__(self) -> None:
        role_id = _require_safe_id(
            self.role_id,
            label="batch plan member role_id",
            maximum=MAX_ROLE_ID_LENGTH,
        )
        backend = _require_safe_id(
            self.resolved_backend,
            label="batch plan member resolved_backend",
            maximum=64,
        )
        if backend in {"auto", "echo"}:
            raise DispatchValidationError(
                "batch plan requires a resolved real provider backend"
            )
        if type(self.context_file_count) is not int or self.context_file_count < 0:
            raise DispatchValidationError(
                "batch plan context_file_count must be a non-negative integer"
            )
        context_sha256 = _require_sha256(
            self.context_sha256, label="batch plan context_sha256"
        )
        if not isinstance(self.execution_profile, Mapping) or not self.execution_profile:
            raise DispatchValidationError(
                "batch plan execution_profile must be a non-empty object"
            )
        execution_profile: dict[str, str] = {}
        if len(self.execution_profile) > 16:
            raise DispatchValidationError(
                "batch plan execution_profile contains too many fields"
            )
        for name, value in self.execution_profile.items():
            if (
                type(name) is not str
                or not name
                or len(name) > 64
                or type(value) is not str
                or len(value) > 4096
            ):
                raise DispatchValidationError(
                    "batch plan execution_profile is invalid"
                )
            execution_profile[name] = value
            assert_content_allowed(
                value,
                label=f"batch plan execution_profile.{name}",
            )
        if execution_profile.get("backend") != backend:
            raise DispatchValidationError(
                "batch plan execution_profile backend mismatch"
            )
        timeout = _require_timeout(
            self.timeout_seconds, label="batch plan timeout_seconds"
        )
        contract = _strict_task_contract(
            self.normalized_contract,
            label="batch plan normalized_contract",
        )
        if contract.mode == "read-only":
            if self.base_head is not None:
                raise DispatchValidationError(
                    "read-only batch plan member base_head must be null"
                )
        elif (
            type(self.base_head) is not str
            or _GIT_HEAD.fullmatch(self.base_head) is None
        ):
            raise DispatchValidationError(
                "edit batch plan member base_head must be a 40- or "
                "64-character lowercase Git hash"
            )
        object.__setattr__(self, "role_id", role_id)
        object.__setattr__(self, "resolved_backend", backend)
        object.__setattr__(self, "context_sha256", context_sha256)
        object.__setattr__(self, "execution_profile", dict(sorted(execution_profile.items())))
        object.__setattr__(self, "timeout_seconds", timeout)
        object.__setattr__(self, "normalized_contract", contract.to_mapping())

    def to_mapping(self) -> dict[str, object]:
        return {
            "role_id": self.role_id,
            "resolved_backend": self.resolved_backend,
            "context_file_count": self.context_file_count,
            "context_sha256": self.context_sha256,
            "base_head": self.base_head,
            "execution_profile": dict(self.execution_profile),
            "timeout_seconds": self.timeout_seconds,
            "normalized_contract": dict(self.normalized_contract),
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> BatchMemberPlan:
        _require_exact_fields(payload, _PLAN_MEMBER_FIELDS, label="batch plan member")
        return cls(
            role_id=payload.get("role_id"),  # type: ignore[arg-type]
            resolved_backend=payload.get("resolved_backend"),  # type: ignore[arg-type]
            context_file_count=payload.get("context_file_count"),  # type: ignore[arg-type]
            context_sha256=payload.get("context_sha256"),  # type: ignore[arg-type]
            base_head=payload.get("base_head"),  # type: ignore[arg-type]
            execution_profile=payload.get("execution_profile"),  # type: ignore[arg-type]
            timeout_seconds=payload.get("timeout_seconds"),  # type: ignore[arg-type]
            normalized_contract=payload.get("normalized_contract"),  # type: ignore[arg-type]
        )


# Keep both natural name orders available to callers during Batch V1 integration.
BatchPlanMember = BatchMemberPlan


def _validate_effects(
    effects: Mapping[str, object], members: Sequence[BatchMemberPlan]
) -> dict[str, object]:
    _require_exact_fields(effects, _EFFECT_FIELDS, label="batch plan effects")
    expected = effects_for_members(members)
    normalized = dict(effects)
    booleans = (
        "creates_local_state",
        "may_use_network_or_bill",
        "writes_source_worktree",
        "returns_patch_only",
    )
    if any(type(normalized.get(field)) is not bool for field in booleans):
        raise DispatchValidationError(
            "batch plan effect flags must be booleans"
        )
    if type(normalized.get("starts_provider_processes")) is not int:
        raise DispatchValidationError(
            "batch plan starts_provider_processes must be an integer"
        )
    if normalized != expected:
        raise DispatchValidationError(
            "batch plan effects do not match the normalized member contracts"
        )
    return expected


@dataclass(frozen=True)
class BatchPlan:
    project_root: str | Path
    request_id: str
    strategy: str
    effects: Mapping[str, object]
    members: tuple[BatchMemberPlan, ...]
    schema_version: int = BATCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != BATCH_SCHEMA_VERSION
        ):
            raise DispatchValidationError("batch plan schema_version must be 1")
        root = canonical_project_root(self.project_root)
        request_id = _require_safe_id(
            self.request_id,
            label="batch plan request_id",
            maximum=MAX_REQUEST_ID_LENGTH,
        )
        if self.strategy != "independent":
            raise DispatchValidationError("batch plan strategy must be independent")
        members = tuple(self.members)
        if not MIN_BATCH_MEMBERS <= len(members) <= MAX_BATCH_MEMBERS:
            raise DispatchValidationError(
                "batch plan must contain between 2 and 4 members"
            )
        if not all(isinstance(member, BatchMemberPlan) for member in members):
            raise DispatchValidationError("batch plan members must be normalized")
        roles = [member.role_id for member in members]
        if len(set(roles)) != len(roles):
            raise DispatchValidationError("batch plan role_id values must be unique")
        if sum(
            member.normalized_contract.get("mode") == "edit" for member in members
        ) > 1:
            raise DispatchValidationError(
                "batch plan may contain at most one edit-mode member"
            )
        if not isinstance(self.effects, Mapping):
            raise DispatchValidationError("batch plan effects must be an object")
        effects = _validate_effects(self.effects, members)
        object.__setattr__(self, "project_root", root)
        object.__setattr__(self, "request_id", request_id)
        object.__setattr__(self, "members", members)
        object.__setattr__(self, "effects", effects)

    @property
    def kind(self) -> str:
        return BATCH_PLAN_KIND

    @property
    def plan_sha256(self) -> str:
        return plan_sha256(self)

    def to_canonical_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "project_root": str(self.project_root),
            "request_id": self.request_id,
            "strategy": self.strategy,
            "effects": dict(self.effects),
            "members": [member.to_mapping() for member in self.members],
        }

    def to_mapping(self) -> dict[str, object]:
        return {
            **self.to_canonical_mapping(),
            "plan_sha256": self.plan_sha256,
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> BatchPlan:
        _require_exact_fields(payload, _DISPLAY_PLAN_FIELDS, label="batch plan")
        if payload.get("kind") != BATCH_PLAN_KIND:
            raise DispatchValidationError(
                f"batch plan kind must be {BATCH_PLAN_KIND}"
            )
        raw_members = payload.get("members")
        if not isinstance(raw_members, list):
            raise DispatchValidationError("batch plan members must be a list")
        members: list[BatchMemberPlan] = []
        for index, raw_member in enumerate(raw_members):
            if not isinstance(raw_member, Mapping):
                raise DispatchValidationError(
                    f"batch plan members[{index}] must be an object"
                )
            members.append(BatchMemberPlan.from_mapping(raw_member))
        effects = payload.get("effects")
        if not isinstance(effects, Mapping):
            raise DispatchValidationError("batch plan effects must be an object")
        plan = cls(
            schema_version=payload.get("schema_version"),  # type: ignore[arg-type]
            project_root=payload.get("project_root"),  # type: ignore[arg-type]
            request_id=payload.get("request_id"),  # type: ignore[arg-type]
            strategy=payload.get("strategy"),  # type: ignore[arg-type]
            effects=effects,
            members=tuple(members),
        )
        supplied_digest = _require_sha256(
            payload.get("plan_sha256"), label="batch plan plan_sha256"
        )
        if plan.plan_sha256 != supplied_digest:
            raise DispatchValidationError(
                "batch plan plan_sha256 does not match its canonical fields"
            )
        return plan


def canonical_plan_bytes(plan: BatchPlan) -> bytes:
    """Return the JCS bytes bound by a Batch V1 plan digest.

    Display timestamps live on the orchestration manifest, not in this mapping.
    """
    if not isinstance(plan, BatchPlan):
        raise DispatchValidationError("plan must be a BatchPlan")
    return canonical_json_bytes(plan.to_canonical_mapping())


def plan_sha256(plan: BatchPlan) -> str:
    return hashlib.sha256(canonical_plan_bytes(plan)).hexdigest()


canonical_batch_plan_bytes = canonical_plan_bytes
batch_plan_sha256 = plan_sha256

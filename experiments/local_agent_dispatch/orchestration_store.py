"""Fail-closed persisted manifest store for Batch V1 orchestrations."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import time
from typing import Any, Callable, Iterator, Mapping

from dyro.canonical import canonical_json_bytes

from .batch_contract import (
    BATCH_SCHEMA_VERSION,
    BatchPlan,
    MAX_ROLE_ID_LENGTH,
    batch_plan_sha256,
)
from .errors import DispatchValidationError
from .file_lock import exclusive_file_lock
from .json_store import atomic_write_json
from .paths import dispatch_home, dispatch_home_path


MAX_MANIFEST_BYTES = 2 * 1024 * 1024
_ORCHESTRATION_ID = re.compile(r"^orch-[0-9a-f]{16}$")
_RUN_ID = re.compile(r"^run-[0-9a-f]{16}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "orchestration_id",
        "request_id",
        "plan_sha256",
        "created_at",
        "updated_at",
        "revision",
        "cancel_requested",
        "members",
        "plan",
    }
)
_MEMBER_FIELDS = frozenset(
    {"role_id", "backend", "run_id", "timeout_seconds"}
)
_SAFE_ROLE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_REQUEST_TOMBSTONE_FIELDS = frozenset(
    {
        "schema_version",
        "request_id",
        "plan_sha256",
        "orchestration_id",
        "bound_at",
    }
)


def _require_exact_fields(
    payload: Mapping[str, Any], expected: frozenset[str], *, label: str
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


def _require_digest(value: object, *, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise DispatchValidationError(f"{label} must be a lowercase SHA-256 digest")
    return value


def orchestration_id_for(request_id: str, plan_sha256: str) -> str:
    _require_digest(plan_sha256, label="plan_sha256")
    if type(request_id) is not str or not request_id:
        raise DispatchValidationError("request_id must be a non-empty string")
    digest = hashlib.sha256(f"{request_id}{plan_sha256}".encode("utf-8")).hexdigest()
    return f"orch-{digest[:16]}"


def run_id_for(orchestration_id: str, member_index: int) -> str:
    if type(orchestration_id) is not str or _ORCHESTRATION_ID.fullmatch(
        orchestration_id
    ) is None:
        raise DispatchValidationError("invalid orchestration_id")
    if type(member_index) is not int or member_index < 0:
        raise DispatchValidationError("member_index must be a non-negative integer")
    digest = hashlib.sha256(
        f"{orchestration_id}{member_index}".encode("utf-8")
    ).hexdigest()
    return f"run-{digest[:16]}"


@dataclass(frozen=True)
class OrchestrationMember:
    role_id: str
    backend: str
    run_id: str
    timeout_seconds: float

    def to_mapping(self) -> dict[str, object]:
        return {
            "role_id": self.role_id,
            "backend": self.backend,
            "run_id": self.run_id,
            "timeout_seconds": self.timeout_seconds,
        }


@dataclass(frozen=True)
class OrchestrationManifest:
    orchestration_id: str
    request_id: str
    plan_sha256: str
    created_at: float
    updated_at: float
    revision: int
    cancel_requested: bool
    members: tuple[OrchestrationMember, ...]
    plan: BatchPlan
    schema_version: int = BATCH_SCHEMA_VERSION

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "orchestration_id": self.orchestration_id,
            "request_id": self.request_id,
            "plan_sha256": self.plan_sha256,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "revision": self.revision,
            "cancel_requested": self.cancel_requested,
            "members": [member.to_mapping() for member in self.members],
            "plan": self.plan.to_mapping(),
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> OrchestrationManifest:
        _require_exact_fields(payload, _MANIFEST_FIELDS, label="orchestration manifest")
        if (
            type(payload.get("schema_version")) is not int
            or payload.get("schema_version") != BATCH_SCHEMA_VERSION
        ):
            raise DispatchValidationError("orchestration schema_version must be 1")
        orchestration_id = payload.get("orchestration_id")
        if type(orchestration_id) is not str or _ORCHESTRATION_ID.fullmatch(
            orchestration_id
        ) is None:
            raise DispatchValidationError("invalid orchestration_id")
        plan_digest = _require_digest(
            payload.get("plan_sha256"), label="orchestration plan_sha256"
        )
        created_at = payload.get("created_at")
        updated_at = payload.get("updated_at")
        if (
            isinstance(created_at, bool)
            or not isinstance(created_at, (int, float))
            or not math.isfinite(float(created_at))
            or float(created_at) < 0
        ):
            raise DispatchValidationError("orchestration created_at must be finite")
        if (
            isinstance(updated_at, bool)
            or not isinstance(updated_at, (int, float))
            or not math.isfinite(float(updated_at))
            or float(updated_at) < float(created_at)
        ):
            raise DispatchValidationError(
                "orchestration updated_at must be finite and not precede created_at"
            )
        revision = payload.get("revision")
        if type(revision) is not int or revision < 0:
            raise DispatchValidationError(
                "orchestration revision must be a non-negative integer"
            )
        cancel_requested = payload.get("cancel_requested")
        if type(cancel_requested) is not bool:
            raise DispatchValidationError(
                "orchestration cancel_requested must be a boolean"
            )
        raw_plan = payload.get("plan")
        if not isinstance(raw_plan, Mapping):
            raise DispatchValidationError("orchestration plan must be an object")
        plan = BatchPlan.from_mapping(raw_plan)
        if payload.get("request_id") != plan.request_id:
            raise DispatchValidationError(
                "orchestration request_id does not match its plan"
            )
        if batch_plan_sha256(plan) != plan_digest:
            raise DispatchValidationError(
                "orchestration plan_sha256 does not match its plan"
            )
        if orchestration_id_for(plan.request_id, plan_digest) != orchestration_id:
            raise DispatchValidationError(
                "orchestration_id does not match request_id and plan_sha256"
            )

        raw_members = payload.get("members")
        if not isinstance(raw_members, list) or len(raw_members) != len(plan.members):
            raise DispatchValidationError(
                "orchestration members must match the plan member count"
            )
        members: list[OrchestrationMember] = []
        for index, (raw_member, planned) in enumerate(
            zip(raw_members, plan.members, strict=True)
        ):
            label = f"orchestration members[{index}]"
            if not isinstance(raw_member, Mapping):
                raise DispatchValidationError(f"{label} must be an object")
            _require_exact_fields(raw_member, _MEMBER_FIELDS, label=label)
            role_id = raw_member.get("role_id")
            if (
                type(role_id) is not str
                or len(role_id) > MAX_ROLE_ID_LENGTH
                or _SAFE_ROLE.fullmatch(role_id) is None
                or role_id != planned.role_id
            ):
                raise DispatchValidationError(f"{label}.role_id does not match the plan")
            backend = raw_member.get("backend")
            if type(backend) is not str or backend != planned.resolved_backend:
                raise DispatchValidationError(f"{label}.backend does not match the plan")
            run_id = raw_member.get("run_id")
            if (
                type(run_id) is not str
                or _RUN_ID.fullmatch(run_id) is None
                or run_id != run_id_for(orchestration_id, index)
            ):
                raise DispatchValidationError(f"{label}.run_id is invalid")
            timeout = raw_member.get("timeout_seconds")
            if (
                isinstance(timeout, bool)
                or not isinstance(timeout, (int, float))
                or not math.isfinite(float(timeout))
                or float(timeout) != planned.timeout_seconds
            ):
                raise DispatchValidationError(
                    f"{label}.timeout_seconds does not match the plan"
                )
            members.append(
                OrchestrationMember(
                    role_id=role_id,
                    backend=backend,
                    run_id=run_id,
                    timeout_seconds=float(timeout),
                )
            )
        return cls(
            schema_version=BATCH_SCHEMA_VERSION,
            orchestration_id=orchestration_id,
            request_id=plan.request_id,
            plan_sha256=plan_digest,
            created_at=float(created_at),
            updated_at=float(updated_at),
            revision=revision,
            cancel_requested=cancel_requested,
            members=tuple(members),
            plan=plan,
        )


class OrchestrationStore:
    def __init__(
        self,
        home: Path | None = None,
        *,
        create: bool = True,
    ) -> None:
        self._create = create
        self.home = dispatch_home(home) if create else dispatch_home_path(home)
        self.root = self.home / "orchestrations"
        self._ensure_root()

    def _ensure_root(self) -> None:
        if self.root.is_symlink():
            raise DispatchValidationError(
                f"orchestration directory is a symbolic link: {self.root}"
            )
        if self._create:
            try:
                self.root.mkdir(mode=0o700, exist_ok=True)
            except OSError as exc:
                raise DispatchValidationError(
                    f"orchestration directory cannot be created: {self.root}"
                ) from exc
        elif not self.root.exists():
            return
        if (
            self.root.is_symlink()
            or not self.root.is_dir()
            or self.root.resolve(strict=True).parent != self.home
        ):
            raise DispatchValidationError(
                f"orchestration directory escapes dispatch home: {self.root}"
            )

    def _path(self, orchestration_id: str) -> Path:
        self._ensure_root()
        if type(orchestration_id) is not str or _ORCHESTRATION_ID.fullmatch(
            orchestration_id
        ) is None:
            raise DispatchValidationError("invalid orchestration_id")
        return self.root / f"{orchestration_id}.json"

    def _manifest_lock_path(self, orchestration_id: str) -> Path:
        self._path(orchestration_id)
        return self.root / f".{orchestration_id}.lock"

    def _request_lock_path(self, request_id: str) -> Path:
        digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:24]
        return self.root / f".request-{digest}.lock"

    def _request_tombstone_path(self, request_id: str) -> Path:
        digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
        return self.root / f"request-{digest}.json"

    @contextmanager
    def mutation_locks(
        self,
        *,
        orchestration_id: str,
        request_id: str,
    ) -> Iterator[None]:
        """Serialize GC with create/idempotency and cancellation mutation."""
        with exclusive_file_lock(self._request_lock_path(request_id)):
            with exclusive_file_lock(
                self._manifest_lock_path(orchestration_id)
            ):
                yield

    def _read_payload(self, path: Path) -> dict[str, Any]:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError as exc:
            raise DispatchValidationError(
                f"orchestration not found: {path.stem}"
            ) from exc
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.EMLINK} or path.is_symlink():
                raise DispatchValidationError(
                    f"orchestration manifest is a symbolic link: {path.stem}"
                ) from exc
            raise DispatchValidationError(
                f"orchestration manifest cannot be opened safely: {path.stem}"
            ) from exc
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise DispatchValidationError(
                    f"orchestration manifest is not a regular file: {path.stem}"
                )
            if opened.st_size > MAX_MANIFEST_BYTES:
                raise DispatchValidationError(
                    f"orchestration manifest exceeds {MAX_MANIFEST_BYTES} bytes"
                )
            linked = os.stat(path, follow_symlinks=False)
            if stat.S_ISLNK(linked.st_mode) or not os.path.samestat(opened, linked):
                raise DispatchValidationError(
                    f"orchestration manifest path changed while opening: {path.stem}"
                )
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                raw = handle.read(MAX_MANIFEST_BYTES + 1)
            if len(raw) > MAX_MANIFEST_BYTES:
                raise DispatchValidationError(
                    f"orchestration manifest exceeds {MAX_MANIFEST_BYTES} bytes"
                )
        finally:
            os.close(descriptor)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DispatchValidationError(
                f"orchestration manifest is corrupt: {path.stem}"
            ) from exc
        if not isinstance(payload, dict):
            raise DispatchValidationError(
                f"orchestration manifest must be an object: {path.stem}"
            )
        return payload

    def _create_only(self, path: Path, payload: dict[str, object]) -> None:
        text = (
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        ).encode("utf-8")
        if len(text) > MAX_MANIFEST_BYTES:
            raise DispatchValidationError(
                f"orchestration manifest exceeds {MAX_MANIFEST_BYTES} bytes"
            )
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(self.root)
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, path, follow_symlinks=False)
            except FileExistsError:
                raise
            directory_fd = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _find_request(self, request_id: str) -> OrchestrationManifest | None:
        for path in sorted(self.root.glob("orch-*.json")):
            manifest = OrchestrationManifest.from_mapping(self._read_payload(path))
            if manifest.request_id == request_id:
                return manifest
        return None

    def _load_request_tombstone(
        self,
        request_id: str,
    ) -> dict[str, object] | None:
        path = self._request_tombstone_path(request_id)
        if not path.exists() and not path.is_symlink():
            return None
        payload = self._read_payload(path)
        _require_exact_fields(
            payload,
            _REQUEST_TOMBSTONE_FIELDS,
            label="orchestration request tombstone",
        )
        if payload.get("schema_version") != BATCH_SCHEMA_VERSION:
            raise DispatchValidationError(
                "orchestration request tombstone schema_version must be 1"
            )
        if payload.get("request_id") != request_id:
            raise DispatchValidationError(
                "orchestration request tombstone does not match request_id"
            )
        _require_digest(
            payload.get("plan_sha256"),
            label="orchestration request tombstone plan_sha256",
        )
        orchestration_id = payload.get("orchestration_id")
        if (
            type(orchestration_id) is not str
            or _ORCHESTRATION_ID.fullmatch(orchestration_id) is None
        ):
            raise DispatchValidationError(
                "orchestration request tombstone has invalid orchestration_id"
            )
        bound_at = payload.get("bound_at")
        if (
            isinstance(bound_at, bool)
            or not isinstance(bound_at, (int, float))
            or not math.isfinite(float(bound_at))
            or float(bound_at) <= 0
        ):
            raise DispatchValidationError(
                "orchestration request tombstone bound_at must be finite"
            )
        return dict(payload)

    def _bind_request_tombstone(
        self,
        *,
        request_id: str,
        plan_sha256: str,
        orchestration_id: str,
        bound_at: float,
    ) -> None:
        expected: dict[str, object] = {
            "schema_version": BATCH_SCHEMA_VERSION,
            "request_id": request_id,
            "plan_sha256": plan_sha256,
            "orchestration_id": orchestration_id,
            "bound_at": bound_at,
        }
        existing = self._load_request_tombstone(request_id)
        if existing is not None:
            if (
                existing["request_id"] != request_id
                or existing["plan_sha256"] != plan_sha256
                or existing["orchestration_id"] != orchestration_id
            ):
                raise DispatchValidationError(
                    "batch request_id is already bound to a different plan"
                )
            return
        try:
            self._create_only(
                self._request_tombstone_path(request_id),
                expected,
            )
        except FileExistsError:
            persisted = self._load_request_tombstone(request_id)
            if persisted is None or (
                persisted["request_id"] != request_id
                or persisted["plan_sha256"] != plan_sha256
                or persisted["orchestration_id"] != orchestration_id
            ):
                raise DispatchValidationError(
                    "batch request_id is already bound to a different plan"
                )

    @staticmethod
    def orchestration_id_for(request_id: str, plan_sha256: str) -> str:
        return orchestration_id_for(request_id, plan_sha256)

    @staticmethod
    def run_id_for(orchestration_id: str, member_index: int) -> str:
        return run_id_for(orchestration_id, member_index)

    def _create_or_load_locked(self, plan: BatchPlan) -> OrchestrationManifest:
        """Create or load while the caller owns the request mutation lock."""
        if not self._create:
            raise DispatchValidationError(
                "read-only orchestration store cannot create manifests"
            )
        if not isinstance(plan, BatchPlan):
            raise DispatchValidationError("plan must be a BatchPlan")
        digest = batch_plan_sha256(plan)
        orchestration_id = orchestration_id_for(plan.request_id, digest)
        plan_mapping = plan.to_mapping()
        tombstone = self._load_request_tombstone(plan.request_id)
        existing_for_request = self._find_request(plan.request_id)
        if existing_for_request is not None:
            if (
                existing_for_request.orchestration_id != orchestration_id
                or existing_for_request.plan_sha256 != digest
                or canonical_json_bytes(existing_for_request.plan.to_mapping())
                != canonical_json_bytes(plan_mapping)
            ):
                raise DispatchValidationError(
                    "batch request_id is already bound to a different plan"
                )
            self._bind_request_tombstone(
                request_id=plan.request_id,
                plan_sha256=digest,
                orchestration_id=orchestration_id,
                bound_at=existing_for_request.created_at,
            )
            return existing_for_request

        if tombstone is not None:
            if (
                tombstone["plan_sha256"] != digest
                or tombstone["orchestration_id"] != orchestration_id
            ):
                raise DispatchValidationError(
                    "batch request_id is already bound to a different plan"
                )
            raise DispatchValidationError(
                "batch request_id was already executed and garbage-collected; "
                "use a new request_id"
            )

        now = time.time()
        members = tuple(
            OrchestrationMember(
                role_id=member.role_id,
                backend=member.resolved_backend,
                run_id=run_id_for(orchestration_id, index),
                timeout_seconds=member.timeout_seconds,
            )
            for index, member in enumerate(plan.members)
        )
        manifest = OrchestrationManifest(
            orchestration_id=orchestration_id,
            request_id=plan.request_id,
            plan_sha256=digest,
            created_at=now,
            updated_at=now,
            revision=0,
            cancel_requested=False,
            members=members,
            plan=plan,
        )
        path = self._path(orchestration_id)
        try:
            self._create_only(path, manifest.to_mapping())
        except FileExistsError:
            persisted = self.load(orchestration_id)
            if (
                persisted.request_id == plan.request_id
                and persisted.plan_sha256 == digest
                and canonical_json_bytes(persisted.plan.to_mapping())
                == canonical_json_bytes(plan_mapping)
            ):
                return persisted
            raise DispatchValidationError(
                "orchestration manifest already exists with different content"
            )
        self._bind_request_tombstone(
            request_id=plan.request_id,
            plan_sha256=digest,
            orchestration_id=orchestration_id,
            bound_at=manifest.created_at,
        )
        return manifest

    def create_or_load(self, plan: BatchPlan) -> OrchestrationManifest:
        if not isinstance(plan, BatchPlan):
            raise DispatchValidationError("plan must be a BatchPlan")
        with exclusive_file_lock(self._request_lock_path(plan.request_id)):
            return self._create_or_load_locked(plan)

    def create_or_load_initialized(
        self,
        plan: BatchPlan,
        initializer: Callable[[OrchestrationManifest], None],
    ) -> OrchestrationManifest:
        """Create the manifest and all member state under GC's lock order."""
        if not isinstance(plan, BatchPlan):
            raise DispatchValidationError("plan must be a BatchPlan")
        if not callable(initializer):
            raise DispatchValidationError("initializer must be callable")
        with exclusive_file_lock(self._request_lock_path(plan.request_id)):
            manifest = self._create_or_load_locked(plan)
            with exclusive_file_lock(
                self._manifest_lock_path(manifest.orchestration_id)
            ):
                fresh = self.load(manifest.orchestration_id)
                initializer(fresh)
                return fresh

    def bind_manifest_tombstone(
        self,
        manifest: OrchestrationManifest,
    ) -> None:
        """Create or validate a manifest's permanent idempotency binding."""
        if not self._create:
            raise DispatchValidationError(
                "read-only orchestration store cannot bind request tombstones"
            )
        if not isinstance(manifest, OrchestrationManifest):
            raise DispatchValidationError("manifest must be an orchestration")
        self._bind_request_tombstone(
            request_id=manifest.request_id,
            plan_sha256=manifest.plan_sha256,
            orchestration_id=manifest.orchestration_id,
            bound_at=manifest.created_at,
        )

    def load(self, orchestration_id: str) -> OrchestrationManifest:
        path = self._path(orchestration_id)
        return OrchestrationManifest.from_mapping(self._read_payload(path))

    def request_cancel(
        self,
        orchestration_id: str,
        *,
        expected_revision: int | None = None,
    ) -> OrchestrationManifest:
        if not self._create:
            raise DispatchValidationError(
                "read-only orchestration store cannot request cancellation"
            )
        if expected_revision is not None and (
            type(expected_revision) is not int or expected_revision < 0
        ):
            raise DispatchValidationError(
                "expected_revision must be a non-negative integer"
            )
        with exclusive_file_lock(self._manifest_lock_path(orchestration_id)):
            manifest = self.load(orchestration_id)
            if manifest.cancel_requested:
                return manifest
            if (
                expected_revision is not None
                and manifest.revision != expected_revision
            ):
                raise DispatchValidationError(
                    "orchestration revision conflict while requesting cancellation"
                )
            updated = OrchestrationManifest(
                orchestration_id=manifest.orchestration_id,
                request_id=manifest.request_id,
                plan_sha256=manifest.plan_sha256,
                created_at=manifest.created_at,
                updated_at=time.time(),
                revision=manifest.revision + 1,
                cancel_requested=True,
                members=manifest.members,
                plan=manifest.plan,
            )
            payload = updated.to_mapping()
            encoded = (
                json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
                + "\n"
            ).encode("utf-8")
            if len(encoded) > MAX_MANIFEST_BYTES:
                raise DispatchValidationError(
                    f"orchestration manifest exceeds {MAX_MANIFEST_BYTES} bytes"
                )
            atomic_write_json(self._path(orchestration_id), payload)
            return self.load(orchestration_id)

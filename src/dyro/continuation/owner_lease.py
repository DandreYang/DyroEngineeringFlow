"""Fenced owner leases and their crash-recoverable takeover protocol."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import secrets

from ..config import validate_id
from ..errors import DyroError, ValidationError
from .action_journal import (
    _create_only_json, _read_json, _remove_json, _replace_json, apply_action_cancellation,
    cancellation_plan_from_payload, prepare_action_cancellation,
)
from .action_models import parse_timestamp, require_digest, timestamp, utc
from .objective_storage import ObjectiveDirectory


OWNER_LEASE_SCHEMA_VERSION = 1
OWNER_TAKEOVER_SCHEMA_VERSION = 1
_OWNER_TAKEOVER_PENDING_FILE = "scheduler-owner-pending.json"


@dataclass(frozen=True)
class OwnerLease:
    objective_id: str
    generation: int
    owner_token_sha256: str
    pid: int
    process_start: str
    issued_at: datetime
    heartbeat_at: datetime
    expires_at: datetime
    released_at: datetime | None = None

    def __post_init__(self) -> None:
        validate_id(self.objective_id, "Objective ID")
        require_digest(self.owner_token_sha256, "OwnerLease.owner_token_sha256")
        if type(self.generation) is not int or self.generation < 1:
            raise TypeError("OwnerLease.generation 必须是正整数")
        if type(self.pid) is not int or self.pid < 1:
            raise TypeError("OwnerLease.pid 必须是正整数")
        if not isinstance(self.process_start, str) or not self.process_start or len(self.process_start) > 256:
            raise TypeError("OwnerLease.process_start 无效")
        for field in ("issued_at", "heartbeat_at", "expires_at"):
            object.__setattr__(self, field, utc(getattr(self, field), f"OwnerLease.{field}"))
        if self.heartbeat_at < self.issued_at or self.expires_at <= self.heartbeat_at:
            raise ValidationError("OwnerLease 时间顺序无效")
        if self.released_at is not None:
            released_at = utc(self.released_at, "OwnerLease.released_at")
            if released_at < self.heartbeat_at or released_at > self.expires_at:
                raise ValidationError("OwnerLease.released_at 超出有效 lease 时间")
            object.__setattr__(self, "released_at", released_at)

    @property
    def active(self) -> bool:
        return self.released_at is None


def _owner_token_digest(owner_token: object) -> str:
    if not isinstance(owner_token, str) or len(owner_token) != 64 or any(char not in "0123456789abcdef" for char in owner_token):
        raise ValidationError("owner token 必须是 32 字节十六进制随机值")
    return hashlib.sha256(owner_token.encode("ascii")).hexdigest()


@dataclass(frozen=True)
class OwnerLeaseGrant:
    lease: OwnerLease
    owner_token: str

    def __post_init__(self) -> None:
        if not isinstance(self.lease, OwnerLease):
            raise TypeError("OwnerLeaseGrant.lease 必须是 OwnerLease")
        if _owner_token_digest(self.owner_token) != self.lease.owner_token_sha256:
            raise ValidationError("OwnerLeaseGrant token 与 lease 不匹配")


def _lease_payload(lease: OwnerLease) -> dict[str, object]:
    return {
        "schema_version": OWNER_LEASE_SCHEMA_VERSION, "objective_id": lease.objective_id,
        "generation": lease.generation, "owner_token_sha256": lease.owner_token_sha256, "pid": lease.pid,
        "process_start": lease.process_start, "issued_at": timestamp(lease.issued_at),
        "heartbeat_at": timestamp(lease.heartbeat_at), "expires_at": timestamp(lease.expires_at),
        "released_at": timestamp(lease.released_at) if lease.released_at else None,
    }


def _lease_from_payload(value: object) -> OwnerLease:
    fields = {
        "schema_version", "objective_id", "generation", "owner_token_sha256", "pid", "process_start",
        "issued_at", "heartbeat_at", "expires_at", "released_at",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ValidationError("Scheduler owner lease 结构无效")
    if type(value.get("schema_version")) is not int or value["schema_version"] != OWNER_LEASE_SCHEMA_VERSION:
        raise ValidationError("Scheduler owner lease schema_version 无效")
    try:
        released = value["released_at"]
        return OwnerLease(
            objective_id=value["objective_id"], generation=value["generation"], owner_token_sha256=value["owner_token_sha256"],
            pid=value["pid"], process_start=value["process_start"],
            issued_at=parse_timestamp(value["issued_at"], "Scheduler owner issued_at"),
            heartbeat_at=parse_timestamp(value["heartbeat_at"], "Scheduler owner heartbeat_at"),
            expires_at=parse_timestamp(value["expires_at"], "Scheduler owner expires_at"),
            released_at=parse_timestamp(released, "Scheduler owner released_at") if released is not None else None,
        )
    except (KeyError, TypeError, ValidationError) as exc:
        raise ValidationError("Scheduler owner lease 内容无效") from exc


def read_optional_lease(directory: ObjectiveDirectory) -> OwnerLease | None:
    try:
        payload = _read_json(directory.fd, "scheduler-owner.json", "Scheduler owner lease")
    except FileNotFoundError:
        return None
    return _lease_from_payload(payload)


def _takeover_pending_payload(lease: OwnerLease, previous: OwnerLease | None, cancellation: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": OWNER_TAKEOVER_SCHEMA_VERSION, "lease": _lease_payload(lease),
        "previous_lease": _lease_payload(previous) if previous is not None else None,
        "action_cancellation": cancellation,
    }


def _read_takeover_pending(directory: ObjectiveDirectory) -> tuple[OwnerLease, OwnerLease | None, dict[str, object]] | None:
    try:
        payload = _read_json(directory.fd, _OWNER_TAKEOVER_PENDING_FILE, "Scheduler owner takeover pending")
    except FileNotFoundError:
        return None
    fields = {"schema_version", "lease", "previous_lease", "action_cancellation"}
    if not isinstance(payload, dict) or set(payload) != fields or payload.get("schema_version") != OWNER_TAKEOVER_SCHEMA_VERSION:
        raise ValidationError("Scheduler owner takeover pending 结构或版本无效")
    try:
        expected = _lease_from_payload(payload["lease"])
    except (KeyError, TypeError, ValidationError) as exc:
        raise ValidationError("Scheduler owner takeover pending lease 无效") from exc
    raw_previous = payload["previous_lease"]
    try:
        previous = None if raw_previous is None else _lease_from_payload(raw_previous)
    except (TypeError, ValidationError) as exc:
        raise ValidationError("Scheduler owner takeover pending previous lease 无效") from exc
    if previous is None and expected.generation != 1:
        raise ValidationError("Scheduler owner takeover pending previous lease 无效")
    if previous is not None and (
        previous.objective_id != expected.objective_id or previous.generation + 1 != expected.generation
    ):
        raise ValidationError("Scheduler owner takeover pending previous lease 无效")
    cancellation = payload["action_cancellation"]
    if not isinstance(cancellation, dict):
        raise ValidationError("Scheduler owner takeover pending cancellation 无效")
    cancellation_plan_from_payload(cancellation)
    return expected, previous, cancellation


def recover_owner_takeover(directory: ObjectiveDirectory) -> None:
    pending = _read_takeover_pending(directory)
    if pending is None:
        return
    expected, previous, cancellation = pending
    current = read_optional_lease(directory)
    if current == expected:
        apply_action_cancellation(directory, cancellation)
        _remove_json(directory.fd, _OWNER_TAKEOVER_PENDING_FILE, "Scheduler owner takeover pending")
        return
    if current == previous:
        _remove_json(directory.fd, _OWNER_TAKEOVER_PENDING_FILE, "Scheduler owner takeover pending")
        return
    raise ValidationError("Scheduler owner takeover pending 与当前 lease 不一致")


def read_owner_lease(directory: ObjectiveDirectory) -> OwnerLease | None:
    recover_owner_takeover(directory)
    return read_optional_lease(directory)


def acquire_owner_lease(
    directory: ObjectiveDirectory, *, objective_id: str, now: datetime, ttl_seconds: int, pid: int,
    process_start: str, owner_token: str | None = None,
) -> OwnerLeaseGrant:
    recover_owner_takeover(directory)
    validate_id(objective_id, "Objective ID")
    now_utc = utc(now, "now")
    if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 3600:
        raise TypeError("ttl_seconds 必须在 1 到 3600 秒之间")
    if type(pid) is not int or pid < 1:
        raise TypeError("pid 必须是正整数")
    if not isinstance(process_start, str) or not process_start or len(process_start) > 256:
        raise TypeError("process_start 无效")
    previous = read_optional_lease(directory)
    if previous is not None:
        if previous.objective_id != objective_id:
            raise ValidationError("Scheduler owner lease Objective 不匹配")
        if now_utc < previous.heartbeat_at:
            raise DyroError("检测到 Scheduler 时钟回拨；拒绝接管 owner lease")
        if previous.active and now_utc < previous.expires_at:
            raise DyroError("Scheduler owner lease 仍有效；拒绝并发接管")
    token = owner_token if owner_token is not None else secrets.token_hex(32)
    lease = OwnerLease(
        objective_id=objective_id, generation=1 if previous is None else previous.generation + 1,
        owner_token_sha256=_owner_token_digest(token), pid=pid, process_start=process_start,
        issued_at=now_utc, heartbeat_at=now_utc, expires_at=now_utc + timedelta(seconds=ttl_seconds),
    )
    cancellation = prepare_action_cancellation(directory, summary="owner lease 已接管；未 start Action 已取消", now=now_utc)
    if cancellation is not None:
        _create_only_json(directory.fd, _OWNER_TAKEOVER_PENDING_FILE, _takeover_pending_payload(lease, previous, cancellation), "Scheduler owner takeover pending")
    _replace_json(directory.fd, "scheduler-owner.json", _lease_payload(lease), "Scheduler owner lease")
    recover_owner_takeover(directory)
    return OwnerLeaseGrant(lease, token)


def assert_owner(lease: OwnerLease, grant: OwnerLeaseGrant, now: datetime) -> datetime:
    now_utc = utc(now, "now")
    if not lease.active or now_utc >= lease.expires_at:
        raise DyroError("Scheduler owner lease 已失效")
    if now_utc < lease.heartbeat_at:
        raise DyroError("检测到 Scheduler 时钟回拨；拒绝继续 owner lease")
    if grant.lease.generation != lease.generation or grant.lease.owner_token_sha256 != lease.owner_token_sha256:
        raise DyroError("Scheduler owner generation 已被围栏；拒绝旧 owner")
    if _owner_token_digest(grant.owner_token) != lease.owner_token_sha256:
        raise DyroError("Scheduler owner token 不匹配")
    return now_utc


def verify_owner_lease(directory: ObjectiveDirectory, *, grant: OwnerLeaseGrant, now: datetime) -> OwnerLease:
    recover_owner_takeover(directory)
    lease = read_optional_lease(directory)
    if lease is None:
        raise DyroError("Scheduler owner lease 不存在")
    assert_owner(lease, grant, now)
    return lease


def renew_owner_lease(directory: ObjectiveDirectory, *, grant: OwnerLeaseGrant, now: datetime, ttl_seconds: int) -> OwnerLeaseGrant:
    recover_owner_takeover(directory)
    if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 3600:
        raise TypeError("ttl_seconds 必须在 1 到 3600 秒之间")
    lease = read_optional_lease(directory)
    if lease is None:
        raise DyroError("Scheduler owner lease 不存在")
    now_utc = assert_owner(lease, grant, now)
    renewed = OwnerLease(lease.objective_id, lease.generation, lease.owner_token_sha256, lease.pid, lease.process_start, lease.issued_at, now_utc, now_utc + timedelta(seconds=ttl_seconds))
    _replace_json(directory.fd, "scheduler-owner.json", _lease_payload(renewed), "Scheduler owner lease")
    return OwnerLeaseGrant(renewed, grant.owner_token)


def release_owner_lease(directory: ObjectiveDirectory, *, grant: OwnerLeaseGrant, now: datetime) -> OwnerLease:
    recover_owner_takeover(directory)
    lease = read_optional_lease(directory)
    if lease is None:
        raise DyroError("Scheduler owner lease 不存在")
    now_utc = assert_owner(lease, grant, now)
    released = OwnerLease(lease.objective_id, lease.generation, lease.owner_token_sha256, lease.pid, lease.process_start, lease.issued_at, now_utc, lease.expires_at, now_utc)
    _replace_json(directory.fd, "scheduler-owner.json", _lease_payload(released), "Scheduler owner lease")
    return released

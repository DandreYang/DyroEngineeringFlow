"""Host projection records. A compiled skill is authority, not a sandbox."""

from __future__ import annotations

from dataclasses import dataclass


SCHEMA_VERSION = 1
AUTHORITY_SKILL_ONLY = "skill_only"
AUTHORITY_SKILL_AND_HOOK = "skill_and_hook"
SCOPE_WORKSPACE = "workspace"
SCOPE_USER = "user"
SKILL_NAME = "SKILL.md"
HOOK_NAME = "deny-hook.json"
DEFAULT_HOST = "cli"
HOOK_SIDECAR_NOTE = "deny hook 写在投影树 SKILL.md 旁，未安装到 hook_surface，不是宿主拦截"

FINDING_FRESH = "FRESH"
FINDING_TAMPERED = "TAMPERED"
FINDING_EXPIRED = "EXPIRED"
FINDING_MISSING_HOOK = "MISSING_HOOK"
FINDING_UNEXPECTED_HOOK = "UNEXPECTED_HOOK"
FINDING_INVALID = "INVALID"


@dataclass(frozen=True)
class HostManifest:
    schema_version: int
    host: str
    scope: str
    authority_projection: str
    skill_sha256: str
    hook_sha256: str
    input_sha256: str


@dataclass(frozen=True)
class HostProjection:
    host: str
    scope: str
    authority_projection: str
    skill_text: str
    hook_text: str
    skill_sha256: str
    hook_sha256: str
    input_sha256: str
    skill_relpath: str
    hook_relpath: str
    manifest_relpath: str


@dataclass(frozen=True)
class HostFinding:
    host: str
    scope: str
    ok: bool
    code: str
    authority_projection: str
    message: str


@dataclass(frozen=True)
class HostDoctorReport:
    schema_version: int
    scope: str
    compiled: bool
    ok: bool
    input_sha256: str
    findings: tuple[HostFinding, ...]

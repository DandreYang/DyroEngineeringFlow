"""Recompute host projection hashes. Stale compiled output is fail-closed."""

from __future__ import annotations

from pathlib import Path

from ..config import Config
from ..errors import DyroError, ValidationError
from .compile import (
    collect_compiler_input,
    hosts_to_compile,
    parse_manifest,
    projection_root,
    projection_scope,
    sha256_text,
)
from .models import (
    AUTHORITY_SKILL_AND_HOOK,
    FINDING_EXPIRED,
    FINDING_FRESH,
    FINDING_INVALID,
    FINDING_MISSING_HOOK,
    FINDING_TAMPERED,
    FINDING_UNEXPECTED_HOOK,
    HOOK_NAME,
    HOOK_SIDECAR_NOTE,
    SCHEMA_VERSION,
    SKILL_NAME,
    HostDoctorReport,
    HostFinding,
    HostManifest,
)


def inspect_projections(config: Config, *, user: bool = False) -> HostDoctorReport:
    """Hash current files and Cards. Missing manifests do not fail."""
    scope = projection_scope(user=user)
    root = projection_root(config, user=user)
    source = collect_compiler_input(config)
    live_hosts = set(hosts_to_compile(source))
    manifests = _load_manifests(root)
    findings: list[HostFinding] = []
    if manifests:
        extra = sorted(set(manifests) - live_hosts)
        missing = sorted(live_hosts - set(manifests))
        if extra or missing:
            findings.append(
                HostFinding(
                    host="*",
                    scope=scope,
                    ok=False,
                    code=FINDING_EXPIRED,
                    authority_projection="",
                    message="宿主集合与当前 Card 不一致；请重新 host compile",
                )
            )
    for host, loaded in manifests.items():
        findings.append(_check_manifest(root, loaded, source.digest, scope=scope))
    orphans = _orphan_hosts(root, set(manifests))
    for host in orphans:
        findings.append(
            HostFinding(
                host=host,
                scope=scope,
                ok=False,
                code=FINDING_TAMPERED,
                authority_projection="",
                message="投影产物缺少有效 manifest；请重新 host compile",
            )
        )
    compiled = bool(manifests) or bool(orphans)
    ok = all(item.ok for item in findings)
    return HostDoctorReport(
        schema_version=SCHEMA_VERSION,
        scope=scope,
        compiled=compiled,
        ok=ok,
        input_sha256=source.digest,
        findings=tuple(findings),
    )


def assert_projections_allow_mutation(config: Config) -> None:
    """Block the next mutation tick only when a compiled projection is stale."""
    report = inspect_projections(config, user=False)
    if not report.compiled:
        return
    if not report.ok:
        raise DyroError(
            "宿主投影过期或被手改；本次 mutation 已降为 plan-only。请运行 dyro host compile"
        )


def doctor_payload(report: HostDoctorReport) -> dict[str, object]:
    return {
        "compiled": report.compiled,
        "hook_enforcement": "projection_sidecar",
        "hook_note": HOOK_SIDECAR_NOTE,
        "findings": [
            {
                "authority_projection": item.authority_projection,
                "code": item.code,
                "host": item.host,
                "message": item.message,
                "ok": item.ok,
                "scope": item.scope,
            }
            for item in report.findings
        ],
        "input_sha256": report.input_sha256,
        "ok": report.ok,
        "schema_version": report.schema_version,
        "scope": report.scope,
    }


def render_doctor_text(report: HostDoctorReport) -> str:
    if not report.compiled:
        return f"未编译宿主投影 scope={report.scope}\n"
    lines = [f"{'PASS' if report.ok else 'FAIL'} scope={report.scope}"]
    for item in report.findings:
        mark = "PASS" if item.ok else "FAIL"
        lines.append(
            f"{mark} {item.host} {item.code} {item.authority_projection or '-'} {item.message}"
        )
    return "\n".join(lines) + "\n"


def _orphan_hosts(root: Path, known: set[str]) -> tuple[str, ...]:
    if not root.is_dir():
        return ()
    found: set[str] = set()
    for path in root.iterdir():
        if not path.is_dir() or path.name in known:
            continue
        if (path / SKILL_NAME).is_file() or (path / HOOK_NAME).is_file():
            found.add(path.name)
    return tuple(sorted(found))


def _load_manifests(root: Path) -> dict[str, HostManifest | ValidationError]:
    if not root.is_dir():
        return {}
    loaded: dict[str, HostManifest | ValidationError] = {}
    for path in sorted(root.glob("*.toml")):
        try:
            loaded[path.stem] = parse_manifest(path)
        except ValidationError as exc:
            loaded[path.stem] = exc
    return loaded


def _check_manifest(
    root: Path,
    loaded: HostManifest | ValidationError,
    input_digest: str,
    *,
    scope: str,
) -> HostFinding:
    if isinstance(loaded, ValidationError):
        return HostFinding(
            host="?",
            scope=scope,
            ok=False,
            code=FINDING_INVALID,
            authority_projection="",
            message=str(loaded),
        )
    skill_path = root / loaded.host / SKILL_NAME
    hook_path = root / loaded.host / HOOK_NAME
    if not skill_path.is_file():
        return _finding(loaded, scope, FINDING_TAMPERED, "SKILL.md 缺失")
    try:
        skill_text = skill_path.read_text(encoding="utf-8")
    except OSError:
        return _finding(loaded, scope, FINDING_TAMPERED, "SKILL.md 无法读取")
    if sha256_text(skill_text) != loaded.skill_sha256:
        return _finding(loaded, scope, FINDING_TAMPERED, "SKILL.md 哈希漂移")
    if loaded.input_sha256 != input_digest:
        return _finding(loaded, scope, FINDING_EXPIRED, "投影输入已过期")
    if loaded.authority_projection == AUTHORITY_SKILL_AND_HOOK:
        if not hook_path.is_file():
            return _finding(loaded, scope, FINDING_MISSING_HOOK, "已投影 deny hook 缺失")
        try:
            hook_text = hook_path.read_text(encoding="utf-8")
        except OSError:
            return _finding(loaded, scope, FINDING_MISSING_HOOK, "deny hook 无法读取")
        if sha256_text(hook_text) != loaded.hook_sha256:
            return _finding(loaded, scope, FINDING_TAMPERED, "deny hook 哈希漂移")
    elif hook_path.is_file():
        return _finding(loaded, scope, FINDING_UNEXPECTED_HOOK, "skill_only 不应存在 deny hook")
    return _finding(loaded, scope, FINDING_FRESH, "投影与当前 Card 一致")


def _finding(manifest: HostManifest, scope: str, code: str, message: str) -> HostFinding:
    return HostFinding(
        host=manifest.host,
        scope=scope,
        ok=code == FINDING_FRESH,
        code=code,
        authority_projection=manifest.authority_projection,
        message=message,
    )

"""Compile audited Cards into a host skill. Paths come from probe, not vendors."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from ..canonical import canonical_json_bytes, canonical_json_text
from ..capability.models import CapabilityCard, CapabilityTestReport, DiscoveredTool, Intent
from ..capability.probe import (
    card_payload,
    discover_unintegrated,
    runtime_cards,
    test_capability,
)
from ..config import Config, validate_id
from ..errors import DyroError, ValidationError
from ..hub import registry_home
from ..state import atomic_write_text, exclusive_lock
from .models import (
    AUTHORITY_SKILL_AND_HOOK,
    AUTHORITY_SKILL_ONLY,
    DEFAULT_HOST,
    HOOK_NAME,
    HOOK_SIDECAR_NOTE,
    SCHEMA_VERSION,
    SCOPE_USER,
    SCOPE_WORKSPACE,
    SKILL_NAME,
    HostManifest,
    HostProjection,
)


WORKSPACE_PROJECTIONS = Path(".dyro") / "host-projections"
DENIED_INTENTS = (Intent.INTEGRATE.value, Intent.PUBLISH.value)
DENIED_PATHS = (".dyro/",)
DESCRIPTION_NEGATIVES = (
    "不要用 git merge 结束任务。",
    "不要把测试通过写成 done。",
)
_FORBIDDEN_SKILL_MARKERS = (
    "dyro task",
    "execute_task",
    "task run",
    "/usr/",
    "/users/",
    "/home/",
    "/tmp/",
    "~/",
    "https://",
    "http://",
    "git@",
)


@dataclass(frozen=True)
class CompilerInput:
    cards: tuple[CapabilityCard, ...]
    reports: tuple[CapabilityTestReport, ...]
    discovered: tuple[DiscoveredTool, ...]
    payload: dict[str, object]
    digest: str


def projection_root(config: Config, *, user: bool) -> Path:
    if user:
        return registry_home() / "host-projections" / validate_id(config.name, "workspace name")
    return config.root / WORKSPACE_PROJECTIONS


def projection_scope(*, user: bool) -> str:
    return SCOPE_USER if user else SCOPE_WORKSPACE


def sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def collect_compiler_input(config: Config) -> CompilerInput:
    cards = tuple(sorted(runtime_cards(config).values(), key=lambda card: card.id))
    reports = tuple(test_capability(config, card.id) for card in cards)
    discovered = discover_unintegrated(config)
    payload = {
        "cards": [card_payload(card) for card in cards],
        "discovered": [item.id for item in discovered],
        "tests": [
            {
                "executable": report.executable,
                "hook_surface": report.hook_surface,
                "id": report.id,
            }
            for report in reports
        ],
    }
    return CompilerInput(
        cards=cards,
        reports=reports,
        discovered=discovered,
        payload=payload,
        digest=hashlib.sha256(canonical_json_bytes(payload)).hexdigest(),
    )


def hosts_to_compile(source: CompilerInput | Config) -> tuple[str, ...]:
    cards = source.cards if isinstance(source, CompilerInput) else runtime_cards(source).values()
    hosts = {validate_id(host, "host") for card in cards for host in card.hosts}
    return tuple(sorted(hosts)) or (DEFAULT_HOST,)


def compile_hosts(
    config: Config,
    *,
    user: bool = False,
    dry_run: bool = False,
) -> tuple[HostProjection, ...]:
    """Render one skill per host. Deny hooks are optional and never a sandbox."""
    source = collect_compiler_input(config)
    scope = projection_scope(user=user)
    root = projection_root(config, user=user)
    planned = tuple(
        _plan_host(host, source, scope=scope)
        for host in hosts_to_compile(source)
    )
    if dry_run:
        return planned
    lock = (registry_home() / "host.lock") if user else (config.root / ".dyro" / "host.lock")
    with exclusive_lock(lock):
        written = tuple(
            _write_host(root, projection) for projection in planned
        )
        _remove_stale_hosts(root, {item.host for item in written})
    return written


def render_manifest(manifest: HostManifest) -> str:
    return (
        f"schema_version = {manifest.schema_version}\n"
        f"host = {json.dumps(manifest.host, ensure_ascii=False)}\n"
        f"scope = {json.dumps(manifest.scope, ensure_ascii=False)}\n"
        f"authority_projection = {json.dumps(manifest.authority_projection, ensure_ascii=False)}\n"
        f"skill_sha256 = {json.dumps(manifest.skill_sha256, ensure_ascii=False)}\n"
        f"hook_sha256 = {json.dumps(manifest.hook_sha256, ensure_ascii=False)}\n"
        f"input_sha256 = {json.dumps(manifest.input_sha256, ensure_ascii=False)}\n"
    )


def parse_manifest(path: Path) -> HostManifest:
    import tomllib

    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValidationError(f"宿主投影清单无法读取：{path.name}") from exc
    if not isinstance(raw, dict):
        raise ValidationError(f"宿主投影清单必须是表：{path.name}")
    try:
        schema_version = int(raw["schema_version"])
        host = validate_id(str(raw["host"]), "host")
        scope = str(raw["scope"])
        authority = str(raw["authority_projection"])
        skill_sha256 = str(raw["skill_sha256"])
        hook_sha256 = str(raw.get("hook_sha256", "") or "")
        input_sha256 = str(raw["input_sha256"])
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise ValidationError(f"宿主投影清单字段无效：{path.name}") from exc
    if schema_version != SCHEMA_VERSION:
        raise ValidationError(f"宿主投影清单 schema_version 不受支持：{path.name}")
    if scope not in {SCOPE_WORKSPACE, SCOPE_USER}:
        raise ValidationError(f"宿主投影清单 scope 无效：{path.name}")
    if authority not in {AUTHORITY_SKILL_ONLY, AUTHORITY_SKILL_AND_HOOK}:
        raise ValidationError(f"宿主投影清单 authority_projection 无效：{path.name}")
    return HostManifest(
        schema_version=schema_version,
        host=host,
        scope=scope,
        authority_projection=authority,
        skill_sha256=skill_sha256,
        hook_sha256=hook_sha256,
        input_sha256=input_sha256,
    )


def _plan_host(host: str, source: CompilerInput, *, scope: str) -> HostProjection:
    reports = {report.id: report for report in source.reports}
    available = tuple(
        card
        for card in source.cards
        if host in card.hosts
        and Intent.EXECUTE.value in card.intents
        and reports[card.id].executable
    )
    hook_surface = next(
        (
            reports[card.id].hook_surface
            for card in source.cards
            if host in card.hosts and reports[card.id].hook_surface
        ),
        "",
    )
    skill_text = render_skill(
        host,
        available=available,
        discovered=source.discovered,
    )
    _assert_skill_contracts(skill_text, available=available)
    hook_text = render_deny_hook(source.cards, host=host) if hook_surface else ""
    authority = AUTHORITY_SKILL_AND_HOOK if hook_text else AUTHORITY_SKILL_ONLY
    skill_sha256 = sha256_text(skill_text)
    hook_sha256 = sha256_text(hook_text) if hook_text else ""
    return HostProjection(
        host=host,
        scope=scope,
        authority_projection=authority,
        skill_text=skill_text,
        hook_text=hook_text,
        skill_sha256=skill_sha256,
        hook_sha256=hook_sha256,
        input_sha256=source.digest,
        skill_relpath=f"{host}/{SKILL_NAME}",
        hook_relpath=f"{host}/{HOOK_NAME}" if hook_text else "",
        manifest_relpath=f"{host}.toml",
    )


def render_skill(
    host: str,
    *,
    available: tuple[CapabilityCard, ...],
    discovered: tuple[DiscoveredTool, ...],
) -> str:
    description = (
        "只观察 Dyro 交付状态并打印已批准的下一步。"
        + "".join(DESCRIPTION_NEGATIVES)
    )
    lines = [
        "---",
        "name: dyro-delivery",
        "description: >",
        f"  {description}",
        "---",
        "",
        "# Dyro 宿主投影",
        "",
        f"投影面：{host}",
        "",
        "## 定律",
        "",
        "1. 外部真源：git 对象与 Objective 状态是交付真源；宿主对话不是。",
        "2. 衰减：合并与下游绑定以当前对象库为准；过期证据不能当活证据。",
        "3. 单写者：同一 Objective 同时只有一个 mutation 写者。",
        "4. 编译权威：宿主只能观察，并打印已批准的 `dyro next`。",
        "",
        "## 本机可用能力",
        "",
    ]
    if available:
        lines.extend(
            [
                "| id | attested | cannot_prove |",
                "| --- | --- | --- |",
            ]
        )
        for card in available:
            lines.append(
                f"| {_skill_cell(card.id)} | {_skill_cell(card.attested_isolation.value)} | {_skill_cell(', '.join(card.cannot_prove))} |"
            )
    else:
        lines.append("本机没有已审计且可探测的 Capability Card。不要执行任务。")
    lines.extend(["", "## 已发现未集成", ""])
    if discovered:
        lines.append("这些不是已审计 Card，不能当作执行器。")
        lines.append("")
        for item in discovered:
            lines.append(f"- {_skill_cell(item.id)}")
    else:
        lines.append("无。")
    lines.extend(
        [
            "",
            "## 允许的命令",
            "",
            "只打印：",
            "",
            "`dyro next`",
            "",
            "## 禁止",
            "",
            f"- {DESCRIPTION_NEGATIVES[0]}",
            f"- {DESCRIPTION_NEGATIVES[1]}",
            "- 不要写入 `.dyro/`。",
            "- 不要 integrate 或 publish。",
            f"- {HOOK_SIDECAR_NOTE}。",
            "",
        ]
    )
    return "\n".join(lines)


def render_deny_hook(cards: tuple[CapabilityCard, ...], *, host: str) -> str:
    authorized = {
        intent
        for card in cards
        if host in card.hosts
        for intent in card.intents
    }
    denied = [intent for intent in DENIED_INTENTS if intent not in authorized]
    payload = {
        "denied_intents": denied,
        "denied_paths": list(DENIED_PATHS),
        "kind": "dyro.host.deny_hook",
        "schema_version": SCHEMA_VERSION,
    }
    return canonical_json_text(payload) + "\n"


def _skill_cell(value: str) -> str:
    cleaned = " ".join(str(value).split()).replace("|", "/").replace("`", "'")
    lowered = cleaned.lower()
    for marker in _FORBIDDEN_SKILL_MARKERS:
        if marker in lowered:
            raise DyroError("Capability Card 字段不能写入未批准命令")
    return cleaned


def _assert_skill_contracts(
    skill_text: str,
    *,
    available: tuple[CapabilityCard, ...],
) -> None:
    lowered = skill_text.lower()
    for marker in DESCRIPTION_NEGATIVES:
        if marker not in skill_text:
            raise DyroError("宿主 skill 的 description 必须包含负例")
    if "`dyro next`" not in skill_text:
        raise DyroError("宿主 skill 只能批准 dyro next")
    for marker in _FORBIDDEN_SKILL_MARKERS:
        if marker in lowered:
            raise DyroError("宿主投影不能包含绝对路径、remote 或未批准命令")
    if not available and "不要执行" not in skill_text:
        raise DyroError("无可用 Card 时宿主 skill 不能暗示执行")


def _write_host(root: Path, projection: HostProjection) -> HostProjection:
    skill_path = root / projection.skill_relpath
    atomic_write_text(skill_path, projection.skill_text)
    hook_path = root / projection.host / HOOK_NAME
    if projection.hook_text:
        atomic_write_text(hook_path, projection.hook_text)
    elif hook_path.is_file():
        hook_path.unlink()
    manifest = HostManifest(
        schema_version=SCHEMA_VERSION,
        host=projection.host,
        scope=projection.scope,
        authority_projection=projection.authority_projection,
        skill_sha256=projection.skill_sha256,
        hook_sha256=projection.hook_sha256,
        input_sha256=projection.input_sha256,
    )
    atomic_write_text(root / projection.manifest_relpath, render_manifest(manifest))
    return projection


def _remove_stale_hosts(root: Path, keep: set[str]) -> None:
    if not root.is_dir():
        return
    for manifest_path in root.glob("*.toml"):
        host = manifest_path.stem
        if host in keep:
            continue
        manifest_path.unlink(missing_ok=True)
        host_dir = root / host
        for name in (SKILL_NAME, HOOK_NAME):
            (host_dir / name).unlink(missing_ok=True)
        if host_dir.is_dir():
            try:
                host_dir.rmdir()
            except OSError:
                pass

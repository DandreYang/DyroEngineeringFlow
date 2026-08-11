"""Safe mirror + avatar installation of the Dyro control-plane Skill."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Mapping

from ..errors import DyroError, ValidationError
from ..hub import registry_home
from ..state import atomic_write_text, exclusive_lock, fsync_directory


CANONICAL_INTEGRATION_ID = "skill"
LEGACY_INTEGRATION_ID = "codex"
SKILL_NAME = "dyro-control-plane"
ASSET_VERSION = 1
MANIFEST_SCHEMA_VERSION = 2
LEGACY_MANIFEST_SCHEMA_VERSION = 1
_SHA256_PREFIX = "sha256:"


class IntegrationState(str, Enum):
    ABSENT = "absent"
    CURRENT = "current"
    OUTDATED = "outdated"
    DRIFTED = "drifted"
    UNOWNED_CONFLICT = "unowned_conflict"
    STALE_MANIFEST = "stale_manifest"
    RECOVERY_REQUIRED = "recovery_required"


@dataclass(frozen=True)
class HostSpec:
    host_id: str
    env_var: str | None
    default_dirname: str


HOSTS: tuple[HostSpec, ...] = (
    HostSpec("codex", "CODEX_HOME", ".codex"),
    HostSpec("claude", "CLAUDE_HOME", ".claude"),
    HostSpec("agents", "AGENTS_HOME", ".agents"),
    HostSpec("cursor", "CURSOR_HOME", ".cursor"),
)


@dataclass(frozen=True)
class AvatarStatus:
    host: str
    path: Path
    state: str
    detail: str


@dataclass(frozen=True)
class IntegrationStatus:
    integration: str
    state: IntegrationState
    target: Path
    manifest: Path
    detail: str
    avatars: tuple[AvatarStatus, ...] = ()


@dataclass(frozen=True)
class IntegrationPlan:
    action: str
    status: IntegrationStatus
    changes: tuple[str, ...]


def _asset_root() -> Path:
    return Path(__file__).parent / "assets" / SKILL_NAME


def _absolute_path(value: Path, label: str) -> Path:
    expanded = value.expanduser()
    if not expanded.is_absolute():
        raise ValidationError(f"{label} 必须是绝对路径：{value}")
    return Path(os.path.normpath(expanded))


def _dyro_home(override: Path | None) -> Path:
    if override is not None:
        return _absolute_path(override, "Dyro home")
    return registry_home()


def _user_home() -> Path:
    raw = os.environ.get("HOME", "").strip()
    if raw:
        return _absolute_path(Path(raw), "HOME")
    return Path.home()


def _normalize_integration(integration: str) -> str:
    if integration in {CANONICAL_INTEGRATION_ID, LEGACY_INTEGRATION_ID}:
        return CANONICAL_INTEGRATION_ID
    raise ValidationError(f"未知 Integration：{integration}")


def _avatar_kind() -> str:
    return "junction" if os.name == "nt" else "symlink"


def _mirror_path(dyro_home: Path | None) -> Path:
    home = _dyro_home(dyro_home)
    mirror = home / "skills" / SKILL_NAME
    if mirror.parent.parent != home or mirror.name != SKILL_NAME:
        raise ValidationError("Skill 镜像路径越界")
    return mirror


def _state_paths(dyro_home: Path | None) -> tuple[Path, Path, Path, Path]:
    home = _dyro_home(dyro_home)
    state_dir = home / "integrations"
    return (
        state_dir / f"{CANONICAL_INTEGRATION_ID}.json",
        state_dir / f"{CANONICAL_INTEGRATION_ID}.transaction.json",
        state_dir / f"{CANONICAL_INTEGRATION_ID}.lock",
        state_dir / f"{LEGACY_INTEGRATION_ID}.json",
    )


def _host_home(
    spec: HostSpec, overrides: Mapping[str, Path] | None
) -> Path | None:
    if overrides is not None and spec.host_id in overrides:
        return _absolute_path(overrides[spec.host_id], f"{spec.host_id} home")
    if spec.env_var:
        raw = os.environ.get(spec.env_var, "").strip()
        if raw:
            return _absolute_path(Path(raw), spec.env_var)
    candidate = _user_home() / spec.default_dirname
    if candidate.exists() and candidate.is_dir() and not candidate.is_symlink():
        unsafe = _symlink_component(candidate)
        if unsafe is None:
            return candidate
    return None


def _avatar_path(host_home: Path) -> Path:
    target = host_home / "skills" / SKILL_NAME
    if target.parent.parent != host_home or target.name != SKILL_NAME:
        raise ValidationError("Skill 分身路径越界")
    return target


def _sha256(content: bytes) -> str:
    return _SHA256_PREFIX + hashlib.sha256(content).hexdigest()


def _inventory(root: Path) -> dict[str, str]:
    if root.is_symlink() or not root.is_dir():
        raise ValidationError(f"Skill 镜像必须是普通目录：{root}")
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValidationError(f"Skill 镜像禁止 symlink：{path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValidationError(f"Skill 资产必须是普通文件：{path}")
        relative = path.relative_to(root).as_posix()
        files[relative] = _sha256(path.read_bytes())
    if not files:
        raise ValidationError("Skill 资产不能为空")
    return files


def _asset_inventory() -> dict[str, str]:
    return _inventory(_asset_root())


def _asset_digest(files: Mapping[str, str]) -> str:
    payload = json.dumps(
        files, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )
    return _sha256(payload.encode("utf-8"))


def _trusted_system_symlink(path: Path) -> bool:
    """Allow only the conventional macOS /tmp and /var compatibility aliases."""
    if os.name == "nt" or path.as_posix() not in {"/tmp", "/var"}:
        return False
    try:
        return path.resolve(strict=True).as_posix() in {"/private/tmp", "/private/var"}
    except OSError:
        return False


def _symlink_component(path: Path, *, boundary: Path | None = None) -> Path | None:
    if boundary is not None and path != boundary and boundary not in path.parents:
        raise ValidationError(f"安全路径检查越界：{path}")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink() and not _trusted_system_symlink(current):
            return current
        if current.exists() and not current.is_dir():
            return current
    return None


def _safe_existing_directory(
    path: Path, label: str, *, boundary: Path | None = None
) -> None:
    unsafe = _symlink_component(path, boundary=boundary)
    if unsafe is not None:
        raise DyroError(f"{label} 包含 symlink 或非目录路径组件：{unsafe}")
    if path.exists() and not path.is_dir():
        raise DyroError(f"{label} 必须是目录：{path}")


def _ensure_safe_directory(path: Path, label: str, *, boundary: Path) -> None:
    if path != boundary and boundary not in path.parents:
        raise ValidationError(f"{label} 路径越界：{path}")
    unsafe_boundary = _symlink_component(boundary)
    if unsafe_boundary is not None:
        raise DyroError(f"{label} 包含 symlink 或非目录路径组件：{unsafe_boundary}")
    if not boundary.exists():
        boundary.mkdir(mode=0o700, parents=True)
    _safe_existing_directory(boundary, label, boundary=boundary)
    current = boundary
    for part in path.relative_to(boundary).parts:
        current /= part
        if current.is_symlink():
            raise DyroError(f"{label} 禁止 symlink：{current}")
        if current.exists():
            _safe_existing_directory(current, label, boundary=boundary)
            continue
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            pass
        _safe_existing_directory(current, label, boundary=boundary)
    _safe_existing_directory(path, label, boundary=boundary)


def _is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    if os.name != "nt" or not path.exists():
        return False
    try:
        return path.resolve() != path
    except OSError:
        return False


def _resolves_to(path: Path, expected: Path) -> bool:
    try:
        return path.resolve() == expected.resolve()
    except OSError:
        return False


def _create_avatar_link(avatar: Path, mirror: Path) -> str:
    kind = _avatar_kind()
    if avatar.exists() or avatar.is_symlink():
        raise DyroError(f"Skill 分身路径已存在：{avatar}")
    if os.name == "nt":
        import subprocess

        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(avatar), str(mirror)],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise DyroError(
                "创建 Skill 分身 junction 失败："
                f"{completed.stderr.strip() or completed.stdout.strip() or completed.returncode}"
            )
    else:
        avatar.symlink_to(mirror, target_is_directory=True)
    if not _resolves_to(avatar, mirror):
        raise DyroError(f"Skill 分身未指向镜像：{avatar}")
    return kind


def _remove_avatar_link(avatar: Path) -> None:
    if avatar.is_symlink() or (os.name == "nt" and _is_link(avatar)):
        avatar.unlink()
        return
    if avatar.exists():
        raise DyroError(f"拒绝删除非 Dyro 分身路径：{avatar}")


def _manifest_payload(
    mirror: Path,
    files: Mapping[str, str],
    avatars: Mapping[str, Mapping[str, str]],
) -> dict[str, object]:
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "integration": CANONICAL_INTEGRATION_ID,
        "asset_version": ASSET_VERSION,
        "asset_digest": _asset_digest(files),
        "mirror": str(mirror),
        "files": dict(sorted(files.items())),
        "avatars": {
            host: {"path": meta["path"], "kind": meta["kind"]}
            for host, meta in sorted(avatars.items())
        },
    }


def _validate_file_map(files: object) -> dict[str, str]:
    if not isinstance(files, dict) or not files:
        raise ValidationError("Integration ownership manifest 文件清单无效")
    validated: dict[str, str] = {}
    for name, digest in files.items():
        candidate = Path(name) if isinstance(name, str) else Path("/")
        if (
            not isinstance(name, str)
            or candidate.is_absolute()
            or ".." in candidate.parts
            or candidate.as_posix() != name
            or not isinstance(digest, str)
            or len(digest) != 71
            or not digest.startswith(_SHA256_PREFIX)
        ):
            raise ValidationError("Integration ownership manifest 文件记录无效")
        validated[name] = digest
    return validated


def _parse_manifest(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError("Integration ownership manifest 无法读取") from exc
    if not isinstance(raw, dict):
        raise ValidationError("Integration ownership manifest 必须是 JSON object")
    if raw.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ValidationError("Integration ownership manifest schema 不受支持")
    expected = {
        "schema_version",
        "integration",
        "asset_version",
        "asset_digest",
        "mirror",
        "files",
        "avatars",
    }
    if set(raw) != expected:
        raise ValidationError("Integration ownership manifest 字段不匹配")
    if raw["integration"] != CANONICAL_INTEGRATION_ID:
        raise ValidationError("Integration ownership manifest 主体不匹配")
    if not isinstance(raw["asset_version"], int) or raw["asset_version"] < 1:
        raise ValidationError("Integration ownership manifest asset version 无效")
    if not isinstance(raw["mirror"], str) or not Path(raw["mirror"]).is_absolute():
        raise ValidationError("Integration ownership manifest mirror 无效")
    files = _validate_file_map(raw["files"])
    digest = raw["asset_digest"]
    if not isinstance(digest, str) or digest != _asset_digest(files):
        raise ValidationError("Integration ownership manifest digest 不匹配")
    avatars = raw["avatars"]
    if not isinstance(avatars, dict):
        raise ValidationError("Integration ownership manifest avatars 无效")
    for host, meta in avatars.items():
        if (
            not isinstance(host, str)
            or not host
            or not isinstance(meta, dict)
            or set(meta) != {"path", "kind"}
            or not isinstance(meta["path"], str)
            or not Path(meta["path"]).is_absolute()
            or meta["kind"] not in {"symlink", "junction"}
        ):
            raise ValidationError("Integration ownership manifest avatar 记录无效")
    return raw


def _parse_legacy_manifest(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError("Legacy Integration ownership manifest 无法读取") from exc
    if not isinstance(raw, dict):
        raise ValidationError("Legacy Integration ownership manifest 必须是 JSON object")
    expected = {
        "schema_version",
        "integration",
        "asset_version",
        "asset_digest",
        "target",
        "files",
    }
    if set(raw) != expected:
        raise ValidationError("Legacy Integration ownership manifest 字段不匹配")
    if raw["schema_version"] != LEGACY_MANIFEST_SCHEMA_VERSION:
        raise ValidationError("Legacy Integration ownership manifest schema 不受支持")
    if raw["integration"] != LEGACY_INTEGRATION_ID:
        raise ValidationError("Legacy Integration ownership manifest 主体不匹配")
    if not isinstance(raw["asset_version"], int) or raw["asset_version"] < 1:
        raise ValidationError("Legacy Integration ownership manifest asset version 无效")
    if not isinstance(raw["target"], str) or not Path(raw["target"]).is_absolute():
        raise ValidationError("Legacy Integration ownership manifest target 无效")
    files = _validate_file_map(raw["files"])
    digest = raw["asset_digest"]
    if not isinstance(digest, str) or digest != _asset_digest(files):
        raise ValidationError("Legacy Integration ownership manifest digest 不匹配")
    return raw


def _inspect_avatar(
    *,
    host: str,
    avatar: Path,
    mirror: Path,
    host_home: Path,
    recorded: Mapping[str, object] | None,
) -> AvatarStatus:
    unsafe = _symlink_component(avatar.parent, boundary=host_home)
    if unsafe is not None:
        return AvatarStatus(
            host, avatar, "unowned", f"分身父路径不安全：{unsafe}"
        )
    exists = avatar.exists() or avatar.is_symlink()
    if not exists:
        return AvatarStatus(host, avatar, "missing", "分身未安装")
    if _is_link(avatar):
        if _resolves_to(avatar, mirror):
            return AvatarStatus(host, avatar, "current", "分身指向镜像")
        return AvatarStatus(host, avatar, "unowned", "分身指向非 Dyro 镜像")
    if avatar.is_dir() and not avatar.is_symlink():
        if recorded is not None and str(recorded.get("path")) == str(avatar):
            return AvatarStatus(host, avatar, "drifted", "分身被替换为普通目录")
        # Owned legacy copy may still sit here before migration.
        return AvatarStatus(
            host, avatar, "legacy_copy", "存在整目录副本，等待迁移为分身"
        )
    return AvatarStatus(host, avatar, "unowned", "分身路径被非目录占用")


def _legacy_owned_copy(
    legacy_manifest_path: Path, *, expected_target: Path | None = None
) -> tuple[dict[str, object], Path] | None:
    if not legacy_manifest_path.is_file() or legacy_manifest_path.is_symlink():
        return None
    try:
        manifest = _parse_legacy_manifest(legacy_manifest_path)
    except ValidationError:
        return None
    target = Path(str(manifest["target"]))
    if expected_target is not None and target != expected_target:
        return None
    if target.is_symlink() or not target.is_dir():
        return None
    try:
        if _inventory(target) != manifest["files"]:
            return None
    except ValidationError:
        return None
    return manifest, target


def integration_status(
    integration: str,
    *,
    dyro_home: Path | None = None,
    host_homes: Mapping[str, Path] | None = None,
    # Backward-compatible test/API alias for Codex home override.
    codex_home: Path | None = None,
) -> IntegrationStatus:
    """Inspect Skill mirror/avatar ownership without creating files."""
    requested = integration
    _normalize_integration(integration)
    overrides: dict[str, Path] = dict(host_homes or {})
    if codex_home is not None:
        overrides["codex"] = codex_home

    mirror = _mirror_path(dyro_home)
    manifest_path, transaction_path, _, legacy_manifest_path = _state_paths(dyro_home)
    state_root = manifest_path.parent
    unsafe_state = _symlink_component(state_root, boundary=_dyro_home(dyro_home))
    if unsafe_state is not None:
        return IntegrationStatus(
            requested,
            IntegrationState.RECOVERY_REQUIRED,
            mirror,
            manifest_path,
            f"Dyro Integration 状态路径不安全：{unsafe_state}",
        )
    if transaction_path.exists() or transaction_path.is_symlink():
        return IntegrationStatus(
            requested,
            IntegrationState.RECOVERY_REQUIRED,
            mirror,
            manifest_path,
            "检测到未完成事务；需要人工恢复后再操作",
        )

    detected: list[tuple[HostSpec, Path]] = []
    for spec in HOSTS:
        home = _host_home(spec, overrides)
        if home is not None:
            detected.append((spec, home))

    avatar_rows: list[AvatarStatus] = []
    host_by_id = {spec.host_id: spec for spec, _home in detected}
    for spec, home in detected:
        avatar = _avatar_path(home)
        row = _inspect_avatar(
            host=spec.host_id,
            avatar=avatar,
            mirror=mirror,
            host_home=home,
            recorded=None,
        )
        avatar_rows.append(row)

    manifest_exists = manifest_path.exists() or manifest_path.is_symlink()
    mirror_exists = mirror.exists() or mirror.is_symlink()
    legacy = _legacy_owned_copy(legacy_manifest_path)
    blocking_avatars: list[AvatarStatus] = []
    for row in avatar_rows:
        if row.state in {"missing", "current"}:
            continue
        if row.state == "unowned":
            spec = host_by_id.get(row.host)
            if spec is None or not _host_is_explicit(spec, overrides):
                continue
        blocking_avatars.append(row)

    if manifest_path.is_symlink():
        return IntegrationStatus(
            requested,
            IntegrationState.STALE_MANIFEST,
            mirror,
            manifest_path,
            "ownership manifest 不能是 symlink",
            tuple(avatar_rows),
        )

    if not manifest_exists:
        if legacy is not None:
            return IntegrationStatus(
                requested,
                IntegrationState.OUTDATED,
                mirror,
                manifest_path,
                "检测到旧版 Codex 整目录安装，可迁移为镜像+分身",
                tuple(avatar_rows),
            )
        if mirror_exists:
            return IntegrationStatus(
                requested,
                IntegrationState.UNOWNED_CONFLICT,
                mirror,
                manifest_path,
                "镜像目录已存在，但不属于 Dyro Integration Manager",
                tuple(avatar_rows),
            )
        if blocking_avatars:
            return IntegrationStatus(
                requested,
                IntegrationState.UNOWNED_CONFLICT,
                mirror,
                manifest_path,
                "分身路径已存在，但不属于 Dyro Integration Manager",
                tuple(avatar_rows),
            )
        return IntegrationStatus(
            requested,
            IntegrationState.ABSENT,
            mirror,
            manifest_path,
            "未安装",
            tuple(avatar_rows),
        )

    try:
        manifest = _parse_manifest(manifest_path)
    except ValidationError as exc:
        return IntegrationStatus(
            requested,
            IntegrationState.STALE_MANIFEST,
            mirror,
            manifest_path,
            str(exc),
            tuple(avatar_rows),
        )

    if manifest["mirror"] != str(mirror):
        return IntegrationStatus(
            requested,
            IntegrationState.STALE_MANIFEST,
            mirror,
            manifest_path,
            "ownership manifest 绑定了不同镜像路径",
            tuple(avatar_rows),
        )
    if not mirror_exists:
        return IntegrationStatus(
            requested,
            IntegrationState.STALE_MANIFEST,
            mirror,
            manifest_path,
            "ownership manifest 存在，但镜像目录缺失",
            tuple(avatar_rows),
        )
    if mirror.is_symlink() or not mirror.is_dir():
        return IntegrationStatus(
            requested,
            IntegrationState.DRIFTED,
            mirror,
            manifest_path,
            "镜像被替换为 symlink 或非目录",
            tuple(avatar_rows),
        )
    try:
        installed = _inventory(mirror)
    except ValidationError as exc:
        return IntegrationStatus(
            requested,
            IntegrationState.DRIFTED,
            mirror,
            manifest_path,
            str(exc),
            tuple(avatar_rows),
        )
    if installed != manifest["files"]:
        return IntegrationStatus(
            requested,
            IntegrationState.DRIFTED,
            mirror,
            manifest_path,
            "镜像文件与 ownership manifest 不匹配",
            tuple(avatar_rows),
        )

    recorded_avatars = manifest["avatars"]
    assert isinstance(recorded_avatars, dict)
    refreshed: list[AvatarStatus] = []
    for spec, home in detected:
        avatar = _avatar_path(home)
        recorded = recorded_avatars.get(spec.host_id)
        row = _inspect_avatar(
            host=spec.host_id,
            avatar=avatar,
            mirror=mirror,
            host_home=home,
            recorded=recorded if isinstance(recorded, dict) else None,
        )
        refreshed.append(row)

    if any(row.state in {"drifted", "legacy_copy"} for row in refreshed):
        return IntegrationStatus(
            requested,
            IntegrationState.DRIFTED,
            mirror,
            manifest_path,
            "已拥有分身状态异常",
            tuple(refreshed),
        )
    # Unowned host paths are skipped; only managed/missing hosts affect freshness.
    managed_or_new = [row for row in refreshed if row.state != "unowned"]
    if any(row.state == "missing" for row in managed_or_new):
        return IntegrationStatus(
            requested,
            IntegrationState.OUTDATED,
            mirror,
            manifest_path,
            "镜像完整，但缺少一个或多个宿主分身",
            tuple(refreshed),
        )

    desired = _asset_inventory()
    if (
        manifest["asset_version"] == ASSET_VERSION
        and manifest["asset_digest"] == _asset_digest(desired)
        and installed == desired
        and managed_or_new
        and all(row.state == "current" for row in managed_or_new)
    ):
        return IntegrationStatus(
            requested,
            IntegrationState.CURRENT,
            mirror,
            manifest_path,
            "镜像与可用分身均与当前 Dyro 包一致",
            tuple(refreshed),
        )
    if not managed_or_new:
        return IntegrationStatus(
            requested,
            IntegrationState.OUTDATED,
            mirror,
            manifest_path,
            "镜像已安装，但没有可挂接的宿主分身",
            tuple(refreshed),
        )
    return IntegrationStatus(
        requested,
        IntegrationState.OUTDATED,
        mirror,
        manifest_path,
        "已安装资产完整，但不是当前 Dyro 包版本",
        tuple(refreshed),
    )


def plan_integration(
    action: str,
    integration: str,
    *,
    dyro_home: Path | None = None,
    host_homes: Mapping[str, Path] | None = None,
    codex_home: Path | None = None,
) -> IntegrationPlan:
    if action not in {"install", "uninstall"}:
        raise ValidationError(f"未知 Integration action：{action}")
    status = integration_status(
        integration,
        dyro_home=dyro_home,
        host_homes=host_homes,
        codex_home=codex_home,
    )
    if action == "install":
        if status.state is IntegrationState.CURRENT:
            changes = ("无需写入；Integration 已是当前版本",)
        elif status.state is IntegrationState.ABSENT:
            changes = (
                f"创建镜像 {status.target}",
                f"写入 {status.manifest}",
                *(f"创建分身 {row.path}" for row in status.avatars if row.state == "missing"),
            )
        elif status.state is IntegrationState.OUTDATED:
            changes = (
                f"原子升级镜像 {status.target}",
                f"更新 {status.manifest}",
                *(
                    f"修复分身 {row.path}"
                    for row in status.avatars
                    if row.state in {"missing", "legacy_copy", "current"}
                ),
            )
        else:
            changes = (f"拒绝写入：{status.detail}",)
    elif status.state is IntegrationState.ABSENT:
        changes = ("无需写入；Integration 尚未安装",)
    elif status.state in {IntegrationState.CURRENT, IntegrationState.OUTDATED}:
        changes = (
            *(
                f"移除分身 {row.path}"
                for row in status.avatars
                if row.state in {"current", "legacy_copy", "missing"}
                and (row.path.exists() or row.path.is_symlink())
            ),
            f"移除镜像 {status.target}",
            f"移除 {status.manifest}",
        )
    else:
        changes = (f"拒绝删除：{status.detail}",)
    return IntegrationPlan(action, status, changes)


def _write_stage(stage: Path) -> None:
    source = _asset_root()
    for relative in _asset_inventory():
        source_file = source / relative
        destination = stage / relative
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with destination.open("xb") as handle:
            handle.write(source_file.read_bytes())
            handle.flush()
            os.fsync(handle.fileno())
        destination.chmod(0o644)
    fsync_directory(stage)
    if _inventory(stage) != _asset_inventory():
        raise DyroError("Integration staging 校验失败")


def _remove_tree(path: Path) -> None:
    shutil.rmtree(path)


def _transaction_payload(
    action: str, mirror: Path, backup: Path | None, *, phase: str
) -> str:
    if phase not in {"prepared", "committed"}:
        raise ValidationError(f"未知 Integration transaction phase：{phase}")
    return (
        json.dumps(
            {
                "schema_version": 1,
                "integration": CANONICAL_INTEGRATION_ID,
                "action": action,
                "phase": phase,
                "mirror": str(mirror),
                "backup": str(backup) if backup is not None else None,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )


def _unlink_transaction(path: Path) -> None:
    path.unlink()


def _preserve_recovery_marker(path: Path, payload: str) -> None:
    if path.exists() or path.is_symlink():
        return
    try:
        atomic_write_text(path, payload)
    except Exception:
        pass


def _complete_transaction(path: Path, payload: str) -> None:
    try:
        _unlink_transaction(path)
        fsync_directory(path.parent)
    except Exception:
        _preserve_recovery_marker(path, payload)
        raise


def _execution_result(
    plan: IntegrationPlan, final_status: IntegrationStatus
) -> IntegrationPlan:
    return IntegrationPlan(plan.action, final_status, plan.changes)


def _restored_owned_installation(
    mirror: Path, manifest_path: Path, original_manifest_text: str
) -> bool:
    if (
        mirror.is_symlink()
        or not mirror.is_dir()
        or manifest_path.is_symlink()
        or not manifest_path.is_file()
    ):
        return False
    try:
        if manifest_path.read_text(encoding="utf-8") != original_manifest_text:
            return False
        manifest = _parse_manifest(manifest_path)
        if manifest["mirror"] != str(mirror):
            return False
        if _inventory(mirror) != manifest["files"]:
            return False
        return (
            not manifest_path.is_symlink()
            and manifest_path.read_text(encoding="utf-8") == original_manifest_text
        )
    except (OSError, UnicodeError, ValidationError):
        return False


def _require_mutable_state(status: IntegrationStatus, action: str) -> None:
    if action == "install" and status.state in {
        IntegrationState.ABSENT,
        IntegrationState.OUTDATED,
        IntegrationState.CURRENT,
    }:
        return
    if action == "uninstall" and status.state in {
        IntegrationState.ABSENT,
        IntegrationState.OUTDATED,
        IntegrationState.CURRENT,
    }:
        return
    verb = "覆盖" if action == "install" else "删除"
    raise DyroError(
        f"Integration 状态为 {status.state.value}；拒绝{verb}：{status.detail}"
    )


def _detected_hosts(
    overrides: Mapping[str, Path] | None,
) -> list[tuple[HostSpec, Path]]:
    detected: list[tuple[HostSpec, Path]] = []
    for spec in HOSTS:
        home = _host_home(spec, overrides)
        if home is not None:
            detected.append((spec, home))
    return detected


def _host_is_explicit(
    spec: HostSpec, overrides: Mapping[str, Path] | None
) -> bool:
    if overrides is not None and spec.host_id in overrides:
        return True
    if spec.env_var and os.environ.get(spec.env_var, "").strip():
        return True
    return False


def _install_avatars(
    *,
    mirror: Path,
    detected: list[tuple[HostSpec, Path]],
    legacy_target: Path | None,
    overrides: Mapping[str, Path] | None,
) -> dict[str, dict[str, str]]:
    avatars: dict[str, dict[str, str]] = {}
    created: list[Path] = []
    try:
        for spec, home in detected:
            avatar = _avatar_path(home)
            unsafe = _symlink_component(avatar.parent, boundary=home)
            if unsafe is not None:
                if _host_is_explicit(spec, overrides):
                    raise DyroError(f"{spec.host_id} skills 目录不安全：{unsafe}")
                continue
            if _is_link(avatar) and _resolves_to(avatar, mirror):
                avatars[spec.host_id] = {
                    "path": str(avatar),
                    "kind": _avatar_kind(),
                }
                continue
            if avatar.exists() or avatar.is_symlink():
                if (
                    legacy_target is not None
                    and avatar == legacy_target
                    and avatar.is_dir()
                    and not avatar.is_symlink()
                ):
                    _remove_tree(avatar)
                elif _host_is_explicit(spec, overrides):
                    raise DyroError(f"拒绝覆盖非 Dyro 分身路径：{avatar}")
                else:
                    # Auto-detected host with a foreign skill: leave it alone.
                    continue
            _ensure_safe_directory(
                avatar.parent, f"{spec.host_id} skills 目录", boundary=home
            )
            kind = _create_avatar_link(avatar, mirror)
            created.append(avatar)
            avatars[spec.host_id] = {"path": str(avatar), "kind": kind}
        if not avatars:
            raise DyroError("没有可挂接的宿主分身；拒绝只安装孤立镜像")
        return avatars
    except Exception:
        for avatar in created:
            if _is_link(avatar) and _resolves_to(avatar, mirror):
                _remove_avatar_link(avatar)
        raise


def install_integration(
    integration: str,
    *,
    yes: bool,
    dry_run: bool = False,
    dyro_home: Path | None = None,
    host_homes: Mapping[str, Path] | None = None,
    codex_home: Path | None = None,
) -> IntegrationPlan:
    overrides: dict[str, Path] = dict(host_homes or {})
    if codex_home is not None:
        overrides["codex"] = codex_home
    plan = plan_integration(
        "install",
        integration,
        dyro_home=dyro_home,
        host_homes=overrides,
    )
    if dry_run:
        return plan
    if not yes:
        raise DyroError("安装 Integration 需要先预览，再显式添加 --yes")
    _require_mutable_state(plan.status, "install")
    if plan.status.state is IntegrationState.CURRENT:
        return plan

    manifest_path, transaction_path, lock_path, legacy_manifest_path = _state_paths(
        dyro_home
    )
    mirror = plan.status.target
    detected = _detected_hosts(overrides)
    _ensure_safe_directory(
        manifest_path.parent,
        "Dyro Integration 状态目录",
        boundary=_dyro_home(dyro_home),
    )
    _ensure_safe_directory(
        mirror.parent, "Dyro skills 镜像目录", boundary=_dyro_home(dyro_home)
    )
    with exclusive_lock(lock_path):
        current = integration_status(
            integration, dyro_home=dyro_home, host_homes=overrides
        )
        _require_mutable_state(current, "install")
        if current.state is IntegrationState.CURRENT:
            return plan_integration(
                "install",
                integration,
                dyro_home=dyro_home,
                host_homes=overrides,
            )

        legacy = _legacy_owned_copy(legacy_manifest_path)
        legacy_target = legacy[1] if legacy is not None else None

        stage = Path(
            tempfile.mkdtemp(prefix=f".{SKILL_NAME}.stage-", dir=mirror.parent)
        )
        backup: Path | None = None
        activated = False
        committed = False
        restored = False
        created_avatars: list[Path] = []
        old_manifest_text = (
            manifest_path.read_text(encoding="utf-8")
            if manifest_path.exists()
            else None
        )
        manifest_replaced = False
        transaction_payload = ""
        try:
            _write_stage(stage)
            if mirror.exists():
                backup = Path(
                    tempfile.mkdtemp(
                        prefix=f".{SKILL_NAME}.backup-", dir=mirror.parent
                    )
                )
                backup.rmdir()
            transaction_payload = _transaction_payload(
                "install", mirror, backup, phase="prepared"
            )
            atomic_write_text(transaction_path, transaction_payload)
            if backup is not None:
                os.replace(mirror, backup)
            if mirror.exists() or mirror.is_symlink():
                raise DyroError("Skill 镜像在事务期间被其他进程创建；已中止")
            os.replace(stage, mirror)
            activated = True
            desired = _asset_inventory()
            avatars = _install_avatars(
                mirror=mirror,
                detected=detected,
                legacy_target=legacy_target,
                overrides=overrides,
            )
            created_avatars = [Path(meta["path"]) for meta in avatars.values()]
            if old_manifest_text is None:
                if manifest_path.exists() or manifest_path.is_symlink():
                    raise DyroError(
                        "Integration ownership manifest 在事务期间被其他进程创建；已中止"
                    )
            elif manifest_path.read_text(encoding="utf-8") != old_manifest_text:
                raise DyroError(
                    "Integration ownership manifest 在事务期间发生变化；已中止"
                )
            atomic_write_text(
                manifest_path,
                json.dumps(
                    _manifest_payload(mirror, desired, avatars),
                    ensure_ascii=True,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
            )
            manifest_replaced = True
            if legacy_manifest_path.exists() or legacy_manifest_path.is_symlink():
                legacy_manifest_path.unlink()
            transaction_payload = _transaction_payload(
                "install", mirror, backup, phase="committed"
            )
            atomic_write_text(transaction_path, transaction_payload)
            committed = True
            if backup is not None:
                _remove_tree(backup)
            _complete_transaction(transaction_path, transaction_payload)
        except Exception:
            if committed:
                _preserve_recovery_marker(transaction_path, transaction_payload)
                if stage.exists():
                    _remove_tree(stage)
                raise
            try:
                for avatar in created_avatars:
                    if _is_link(avatar) and _resolves_to(avatar, mirror):
                        _remove_avatar_link(avatar)
                if activated and mirror.exists():
                    _remove_tree(mirror)
                if backup is not None and backup.exists():
                    os.replace(backup, mirror)
                if manifest_replaced:
                    if old_manifest_text is None:
                        manifest_path.unlink(missing_ok=True)
                    else:
                        atomic_write_text(manifest_path, old_manifest_text)
                if old_manifest_text is None:
                    restored = (
                        not mirror.exists()
                        and not mirror.is_symlink()
                        and not manifest_path.exists()
                        and not manifest_path.is_symlink()
                    )
                elif mirror.exists() and manifest_path.exists():
                    restored = _restored_owned_installation(
                        mirror, manifest_path, old_manifest_text
                    )
            finally:
                if stage.exists():
                    _remove_tree(stage)
                if restored and transaction_path.exists():
                    _complete_transaction(transaction_path, transaction_payload)
            raise
    final_status = integration_status(
        integration, dyro_home=dyro_home, host_homes=overrides
    )
    return _execution_result(plan, final_status)


def uninstall_integration(
    integration: str,
    *,
    yes: bool,
    dry_run: bool = False,
    dyro_home: Path | None = None,
    host_homes: Mapping[str, Path] | None = None,
    codex_home: Path | None = None,
) -> IntegrationPlan:
    overrides: dict[str, Path] = dict(host_homes or {})
    if codex_home is not None:
        overrides["codex"] = codex_home
    plan = plan_integration(
        "uninstall",
        integration,
        dyro_home=dyro_home,
        host_homes=overrides,
    )
    if dry_run:
        return plan
    if not yes:
        raise DyroError("卸载 Integration 需要先预览，再显式添加 --yes")
    _require_mutable_state(plan.status, "uninstall")
    if plan.status.state is IntegrationState.ABSENT:
        return plan

    manifest_path, transaction_path, lock_path, legacy_manifest_path = _state_paths(
        dyro_home
    )
    mirror = plan.status.target
    _safe_existing_directory(
        manifest_path.parent,
        "Dyro Integration 状态目录",
        boundary=_dyro_home(dyro_home),
    )
    with exclusive_lock(lock_path):
        current = integration_status(
            integration, dyro_home=dyro_home, host_homes=overrides
        )
        _require_mutable_state(current, "uninstall")
        if current.state is IntegrationState.ABSENT:
            return plan_integration(
                "uninstall",
                integration,
                dyro_home=dyro_home,
                host_homes=overrides,
            )

        legacy = _legacy_owned_copy(legacy_manifest_path)
        manifest_text = (
            manifest_path.read_text(encoding="utf-8")
            if manifest_path.exists()
            else None
        )
        if mirror.exists() and not mirror.is_symlink():
            backup_dir = mirror.parent
            boundary = _dyro_home(dyro_home)
        elif legacy is not None:
            backup_dir = legacy[1].parent
            boundary = legacy[1].parent.parent
        else:
            backup_dir = mirror.parent
            boundary = _dyro_home(dyro_home)
        _ensure_safe_directory(backup_dir, "Skill 卸载备份目录", boundary=boundary)
        backup = Path(
            tempfile.mkdtemp(prefix=f".{SKILL_NAME}.backup-", dir=backup_dir)
        )
        backup.rmdir()
        removed_manifest = False
        removed_avatars: list[Path] = []
        committed = False
        restored = False
        transaction_payload = _transaction_payload(
            "uninstall", mirror, backup, phase="prepared"
        )
        atomic_write_text(transaction_path, transaction_payload)
        try:
            for row in current.avatars:
                avatar = row.path
                if _is_link(avatar) and _resolves_to(avatar, mirror):
                    _remove_avatar_link(avatar)
                    removed_avatars.append(avatar)
            moved_tree = False
            if mirror.exists() and not mirror.is_symlink():
                os.replace(mirror, backup)
                moved_tree = True
            elif legacy is not None and legacy[1].exists() and not legacy[1].is_symlink():
                os.replace(legacy[1], backup)
                moved_tree = True
            elif mirror.is_symlink():
                mirror.unlink()
            if manifest_path.exists():
                manifest_path.unlink()
                removed_manifest = True
            if legacy_manifest_path.exists() or legacy_manifest_path.is_symlink():
                legacy_manifest_path.unlink()
            transaction_payload = _transaction_payload(
                "uninstall", mirror, backup, phase="committed"
            )
            atomic_write_text(transaction_path, transaction_payload)
            committed = True
            if moved_tree and backup.exists():
                _remove_tree(backup)
            _complete_transaction(transaction_path, transaction_payload)
        except Exception:
            if committed:
                _preserve_recovery_marker(transaction_path, transaction_payload)
                raise
            try:
                if backup.exists() and not mirror.exists():
                    os.replace(backup, mirror)
                for avatar in removed_avatars:
                    if (
                        not avatar.exists()
                        and not avatar.is_symlink()
                        and mirror.exists()
                    ):
                        _create_avatar_link(avatar, mirror)
                if removed_manifest and manifest_text is not None and mirror.exists():
                    atomic_write_text(manifest_path, manifest_text)
                if (
                    manifest_text is not None
                    and mirror.exists()
                    and manifest_path.exists()
                ):
                    restored = _restored_owned_installation(
                        mirror, manifest_path, manifest_text
                    )
            finally:
                if restored and transaction_path.exists():
                    _complete_transaction(transaction_path, transaction_payload)
            raise
    final_status = integration_status(
        integration, dyro_home=dyro_home, host_homes=overrides
    )
    return _execution_result(plan, final_status)

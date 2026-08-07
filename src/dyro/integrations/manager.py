"""Safe, independently-owned installation of optional host integrations."""

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


INTEGRATION_ID = "codex"
SKILL_NAME = "dyro-control-plane"
ASSET_VERSION = 1
MANIFEST_SCHEMA_VERSION = 1
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
class IntegrationStatus:
    integration: str
    state: IntegrationState
    target: Path
    manifest: Path
    detail: str


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


def _codex_home(override: Path | None) -> Path:
    if override is not None:
        return _absolute_path(override, "Codex home")
    raw = os.environ.get("CODEX_HOME", "").strip()
    if raw:
        return _absolute_path(Path(raw), "CODEX_HOME")
    return Path.home() / ".codex"


def _dyro_home(override: Path | None) -> Path:
    if override is not None:
        return _absolute_path(override, "Dyro home")
    return registry_home()


def _target_path(codex_home: Path | None) -> Path:
    home = _codex_home(codex_home)
    target = home / "skills" / SKILL_NAME
    if target.parent.parent != home or target.name != SKILL_NAME:
        raise ValidationError("Codex skill 目标路径越界")
    return target


def _state_paths(dyro_home: Path | None) -> tuple[Path, Path, Path]:
    home = _dyro_home(dyro_home)
    state_dir = home / "integrations"
    return (
        state_dir / f"{INTEGRATION_ID}.json",
        state_dir / f"{INTEGRATION_ID}.transaction.json",
        state_dir / f"{INTEGRATION_ID}.lock",
    )


def _validate_integration(integration: str) -> None:
    if integration != INTEGRATION_ID:
        raise ValidationError(f"未知 Integration：{integration}")


def _sha256(content: bytes) -> str:
    return _SHA256_PREFIX + hashlib.sha256(content).hexdigest()


def _inventory(root: Path) -> dict[str, str]:
    if root.is_symlink() or not root.is_dir():
        raise ValidationError(f"Integration 目录必须是普通目录：{root}")
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValidationError(f"Integration 目录禁止 symlink：{path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValidationError(f"Integration 资产必须是普通文件：{path}")
        relative = path.relative_to(root).as_posix()
        files[relative] = _sha256(path.read_bytes())
    if not files:
        raise ValidationError("Integration 资产不能为空")
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


def _manifest_payload(target: Path, files: Mapping[str, str]) -> dict[str, object]:
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "integration": INTEGRATION_ID,
        "asset_version": ASSET_VERSION,
        "asset_digest": _asset_digest(files),
        "target": str(target),
        "files": dict(sorted(files.items())),
    }


def _parse_manifest(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError("Integration ownership manifest 无法读取") from exc
    if not isinstance(raw, dict):
        raise ValidationError("Integration ownership manifest 必须是 JSON object")
    expected = {
        "schema_version",
        "integration",
        "asset_version",
        "asset_digest",
        "target",
        "files",
    }
    if set(raw) != expected:
        raise ValidationError("Integration ownership manifest 字段不匹配")
    if raw["schema_version"] != MANIFEST_SCHEMA_VERSION:
        raise ValidationError("Integration ownership manifest schema 不受支持")
    if raw["integration"] != INTEGRATION_ID:
        raise ValidationError("Integration ownership manifest 主体不匹配")
    if not isinstance(raw["asset_version"], int) or raw["asset_version"] < 1:
        raise ValidationError("Integration ownership manifest asset version 无效")
    if not isinstance(raw["target"], str) or not Path(raw["target"]).is_absolute():
        raise ValidationError("Integration ownership manifest target 无效")
    files = raw["files"]
    if not isinstance(files, dict) or not files:
        raise ValidationError("Integration ownership manifest 文件清单无效")
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
    digest = raw["asset_digest"]
    if not isinstance(digest, str) or digest != _asset_digest(files):
        raise ValidationError("Integration ownership manifest digest 不匹配")
    return raw


def integration_status(
    integration: str,
    *,
    codex_home: Path | None = None,
    dyro_home: Path | None = None,
) -> IntegrationStatus:
    """Inspect integration ownership without creating files or directories."""
    _validate_integration(integration)
    target = _target_path(codex_home)
    manifest_path, transaction_path, _ = _state_paths(dyro_home)
    state_root = manifest_path.parent
    unsafe_state = _symlink_component(state_root, boundary=_dyro_home(dyro_home))
    if unsafe_state is not None:
        return IntegrationStatus(
            integration,
            IntegrationState.RECOVERY_REQUIRED,
            target,
            manifest_path,
            f"Dyro Integration 状态路径不安全：{unsafe_state}",
        )
    if transaction_path.exists() or transaction_path.is_symlink():
        return IntegrationStatus(
            integration,
            IntegrationState.RECOVERY_REQUIRED,
            target,
            manifest_path,
            "检测到未完成事务；需要人工恢复后再操作",
        )
    manifest_exists = manifest_path.exists() or manifest_path.is_symlink()
    target_exists = target.exists() or target.is_symlink()
    unsafe_target = _symlink_component(target, boundary=_codex_home(codex_home))
    if unsafe_target is not None:
        state = (
            IntegrationState.DRIFTED
            if manifest_exists
            else IntegrationState.UNOWNED_CONFLICT
        )
        return IntegrationStatus(
            integration,
            state,
            target,
            manifest_path,
            f"Codex Integration 目标路径不安全：{unsafe_target}",
        )
    if not manifest_exists and not target_exists:
        return IntegrationStatus(
            integration,
            IntegrationState.ABSENT,
            target,
            manifest_path,
            "未安装",
        )
    if manifest_path.is_symlink():
        return IntegrationStatus(
            integration,
            IntegrationState.STALE_MANIFEST,
            target,
            manifest_path,
            "ownership manifest 不能是 symlink",
        )
    if not manifest_exists:
        return IntegrationStatus(
            integration,
            IntegrationState.UNOWNED_CONFLICT,
            target,
            manifest_path,
            "目标已存在，但不属于 Dyro Integration Manager",
        )
    try:
        manifest = _parse_manifest(manifest_path)
    except ValidationError as exc:
        return IntegrationStatus(
            integration,
            IntegrationState.STALE_MANIFEST,
            target,
            manifest_path,
            str(exc),
        )
    if manifest["target"] != str(target):
        return IntegrationStatus(
            integration,
            IntegrationState.STALE_MANIFEST,
            target,
            manifest_path,
            "ownership manifest 绑定了不同目标路径",
        )
    if not target_exists:
        return IntegrationStatus(
            integration,
            IntegrationState.STALE_MANIFEST,
            target,
            manifest_path,
            "ownership manifest 存在，但已安装目录缺失",
        )
    if target.is_symlink() or not target.is_dir():
        return IntegrationStatus(
            integration,
            IntegrationState.DRIFTED,
            target,
            manifest_path,
            "已拥有目标被替换为 symlink 或非目录",
        )
    try:
        installed = _inventory(target)
    except ValidationError as exc:
        return IntegrationStatus(
            integration,
            IntegrationState.DRIFTED,
            target,
            manifest_path,
            str(exc),
        )
    if installed != manifest["files"]:
        return IntegrationStatus(
            integration,
            IntegrationState.DRIFTED,
            target,
            manifest_path,
            "已安装文件与 ownership manifest 不匹配",
        )
    desired = _asset_inventory()
    if (
        manifest["asset_version"] == ASSET_VERSION
        and manifest["asset_digest"] == _asset_digest(desired)
        and installed == desired
    ):
        state = IntegrationState.CURRENT
        detail = "已安装资产与当前 Dyro 包一致"
    else:
        state = IntegrationState.OUTDATED
        detail = "已安装资产完整，但不是当前 Dyro 包版本"
    return IntegrationStatus(integration, state, target, manifest_path, detail)


def plan_integration(
    action: str,
    integration: str,
    *,
    codex_home: Path | None = None,
    dyro_home: Path | None = None,
) -> IntegrationPlan:
    if action not in {"install", "uninstall"}:
        raise ValidationError(f"未知 Integration action：{action}")
    status = integration_status(integration, codex_home=codex_home, dyro_home=dyro_home)
    if action == "install":
        if status.state is IntegrationState.CURRENT:
            changes = ("无需写入；Integration 已是当前版本",)
        elif status.state is IntegrationState.ABSENT:
            changes = (f"创建 {status.target}", f"写入 {status.manifest}")
        elif status.state is IntegrationState.OUTDATED:
            changes = (f"原子升级 {status.target}", f"更新 {status.manifest}")
        else:
            changes = (f"拒绝写入：{status.detail}",)
    elif status.state is IntegrationState.ABSENT:
        changes = ("无需写入；Integration 尚未安装",)
    elif status.state in {IntegrationState.CURRENT, IntegrationState.OUTDATED}:
        changes = (f"移除自有目录 {status.target}", f"移除 {status.manifest}")
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
    action: str, target: Path, backup: Path | None, *, phase: str
) -> str:
    if phase not in {"prepared", "committed"}:
        raise ValidationError(f"未知 Integration transaction phase：{phase}")
    return (
        json.dumps(
            {
                "schema_version": 1,
                "integration": INTEGRATION_ID,
                "action": action,
                "phase": phase,
                "target": str(target),
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
    """Best-effort restore of a marker removed before a failed directory sync."""
    if path.exists() or path.is_symlink():
        return
    try:
        atomic_write_text(path, payload)
    except Exception:
        # The original exception remains authoritative.  atomic_write_text replaces
        # the destination before its final directory sync, so the marker will still
        # exist for the ordinary fsync-failure case.
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
    """Keep the applied change list while reporting the resulting state."""
    return IntegrationPlan(plan.action, final_status, plan.changes)


def _restored_owned_installation(
    target: Path, manifest_path: Path, original_manifest_text: str
) -> bool:
    """Verify rollback restored the exact pre-transaction ownership pair."""
    if (
        target.is_symlink()
        or not target.is_dir()
        or manifest_path.is_symlink()
        or not manifest_path.is_file()
    ):
        return False
    try:
        if manifest_path.read_text(encoding="utf-8") != original_manifest_text:
            return False
        manifest = _parse_manifest(manifest_path)
        if manifest["target"] != str(target):
            return False
        if _inventory(target) != manifest["files"]:
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


def install_integration(
    integration: str,
    *,
    yes: bool,
    dry_run: bool = False,
    codex_home: Path | None = None,
    dyro_home: Path | None = None,
) -> IntegrationPlan:
    plan = plan_integration(
        "install", integration, codex_home=codex_home, dyro_home=dyro_home
    )
    if dry_run:
        return plan
    if not yes:
        raise DyroError("安装 Integration 需要先预览，再显式添加 --yes")
    _require_mutable_state(plan.status, "install")
    if plan.status.state is IntegrationState.CURRENT:
        return plan

    manifest_path, transaction_path, lock_path = _state_paths(dyro_home)
    target = plan.status.target
    _ensure_safe_directory(
        manifest_path.parent,
        "Dyro Integration 状态目录",
        boundary=_dyro_home(dyro_home),
    )
    _ensure_safe_directory(
        target.parent, "Codex skills 目录", boundary=_codex_home(codex_home)
    )
    with exclusive_lock(lock_path):
        current = integration_status(
            integration, codex_home=codex_home, dyro_home=dyro_home
        )
        _require_mutable_state(current, "install")
        if current.state is IntegrationState.CURRENT:
            return plan_integration(
                "install", integration, codex_home=codex_home, dyro_home=dyro_home
            )

        stage = Path(
            tempfile.mkdtemp(prefix=f".{SKILL_NAME}.stage-", dir=target.parent)
        )
        backup: Path | None = None
        activated = False
        committed = False
        restored = False
        old_manifest_text = (
            manifest_path.read_text(encoding="utf-8")
            if manifest_path.exists()
            else None
        )
        manifest_replaced = False
        transaction_payload = ""
        try:
            _write_stage(stage)
            if target.exists():
                backup = Path(
                    tempfile.mkdtemp(prefix=f".{SKILL_NAME}.backup-", dir=target.parent)
                )
                backup.rmdir()
            transaction_payload = _transaction_payload(
                "install", target, backup, phase="prepared"
            )
            atomic_write_text(transaction_path, transaction_payload)
            if backup is not None:
                os.replace(target, backup)
            if target.exists() or target.is_symlink():
                raise DyroError("Integration 目标在事务期间被其他进程创建；已中止")
            os.replace(stage, target)
            activated = True
            desired = _asset_inventory()
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
                    _manifest_payload(target, desired),
                    ensure_ascii=True,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
            )
            manifest_replaced = True
            transaction_payload = _transaction_payload(
                "install", target, backup, phase="committed"
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
                if activated and target.exists():
                    _remove_tree(target)
                if backup is not None and backup.exists():
                    os.replace(backup, target)
                if manifest_replaced:
                    if old_manifest_text is None:
                        manifest_path.unlink(missing_ok=True)
                    else:
                        atomic_write_text(manifest_path, old_manifest_text)
                if old_manifest_text is None:
                    restored = (
                        not target.exists()
                        and not target.is_symlink()
                        and not manifest_path.exists()
                        and not manifest_path.is_symlink()
                    )
                elif target.exists() and manifest_path.exists():
                    restored = _restored_owned_installation(
                        target, manifest_path, old_manifest_text
                    )
            finally:
                if stage.exists():
                    _remove_tree(stage)
                if restored and transaction_path.exists():
                    _complete_transaction(transaction_path, transaction_payload)
            raise
    final_status = integration_status(
        integration, codex_home=codex_home, dyro_home=dyro_home
    )
    return _execution_result(plan, final_status)


def uninstall_integration(
    integration: str,
    *,
    yes: bool,
    dry_run: bool = False,
    codex_home: Path | None = None,
    dyro_home: Path | None = None,
) -> IntegrationPlan:
    plan = plan_integration(
        "uninstall", integration, codex_home=codex_home, dyro_home=dyro_home
    )
    if dry_run:
        return plan
    if not yes:
        raise DyroError("卸载 Integration 需要先预览，再显式添加 --yes")
    _require_mutable_state(plan.status, "uninstall")
    if plan.status.state is IntegrationState.ABSENT:
        return plan

    manifest_path, transaction_path, lock_path = _state_paths(dyro_home)
    target = plan.status.target
    _safe_existing_directory(
        manifest_path.parent,
        "Dyro Integration 状态目录",
        boundary=_dyro_home(dyro_home),
    )
    _safe_existing_directory(
        target.parent, "Codex skills 目录", boundary=_codex_home(codex_home)
    )
    with exclusive_lock(lock_path):
        current = integration_status(
            integration, codex_home=codex_home, dyro_home=dyro_home
        )
        _require_mutable_state(current, "uninstall")
        if current.state is IntegrationState.ABSENT:
            return plan_integration(
                "uninstall", integration, codex_home=codex_home, dyro_home=dyro_home
            )
        manifest_text = manifest_path.read_text(encoding="utf-8")
        backup = Path(
            tempfile.mkdtemp(prefix=f".{SKILL_NAME}.backup-", dir=target.parent)
        )
        backup.rmdir()
        removed_manifest = False
        committed = False
        restored = False
        transaction_payload = _transaction_payload(
            "uninstall", target, backup, phase="prepared"
        )
        atomic_write_text(transaction_path, transaction_payload)
        try:
            os.replace(target, backup)
            manifest_path.unlink()
            removed_manifest = True
            transaction_payload = _transaction_payload(
                "uninstall", target, backup, phase="committed"
            )
            atomic_write_text(transaction_path, transaction_payload)
            committed = True
            _remove_tree(backup)
            _complete_transaction(transaction_path, transaction_payload)
        except Exception:
            if committed:
                _preserve_recovery_marker(transaction_path, transaction_payload)
                raise
            try:
                if backup.exists() and not target.exists():
                    os.replace(backup, target)
                if removed_manifest and target.exists():
                    atomic_write_text(manifest_path, manifest_text)
                if target.exists() and manifest_path.exists():
                    restored = _restored_owned_installation(
                        target, manifest_path, manifest_text
                    )
            finally:
                if restored and transaction_path.exists():
                    _complete_transaction(transaction_path, transaction_payload)
            raise
    final_status = integration_status(
        integration, codex_home=codex_home, dyro_home=dyro_home
    )
    return _execution_result(plan, final_status)

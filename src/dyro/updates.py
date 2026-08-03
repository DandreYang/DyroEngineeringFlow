from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from enum import Enum
from importlib.metadata import (
    PackageNotFoundError,
    distribution,
    version as distribution_version,
)
from importlib.util import find_spec
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import site
import subprocess
import sys
import tempfile
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .errors import DyroError, ValidationError
from .hub import registry_home
from .state import atomic_write_text, exclusive_lock


UPDATE_SCHEMA_VERSION = 1
UPDATE_FILE = "updates.json"
UPDATE_LOCK = "updates.lock"
PYPI_JSON_URL = "https://pypi.org/pypi/dyro/json"
PYPI_SIMPLE_URL = "https://pypi.org/simple"
PYPI_RESPONSE_LIMIT = 256 * 1024
DEFAULT_CHECK_TIMEOUT = 1.5
EXPLICIT_CHECK_TIMEOUT = 5.0
DAILY_LOCK_TIMEOUT = 0.25
_STABLE_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


class UpdateKind(str, Enum):
    NONE = "none"
    PATCH = "patch"
    MINOR = "minor"
    MAJOR = "major"


@dataclass(frozen=True)
class UpdateState:
    check_enabled: bool = True
    auto_patch: bool = False
    last_checked_on: str = ""
    latest_version: str = ""


@dataclass(frozen=True)
class UpdateResult:
    checked: bool
    current_version: str
    latest_version: str = ""
    kind: UpdateKind = UpdateKind.NONE
    error: str = ""


@dataclass(frozen=True)
class UpdatePlan:
    manager: str
    argv: tuple[str, ...]
    scope: str
    constraint: str = ""


def _state_path() -> Path:
    return registry_home() / UPDATE_FILE


def _state_lock_path() -> Path:
    return registry_home() / UPDATE_LOCK


def _parse_stable_version(value: str) -> tuple[int, int, int]:
    match = _STABLE_VERSION.fullmatch(value)
    if match is None:
        raise ValidationError(f"Dyro 版本号不是稳定的 X.Y.Z 格式：{value}")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def classify_update(current_version: str, latest_version: str) -> UpdateKind:
    current = _parse_stable_version(current_version)
    latest = _parse_stable_version(latest_version)
    if latest <= current:
        return UpdateKind.NONE
    if latest[0] > current[0]:
        return UpdateKind.MAJOR
    if latest[1] > current[1]:
        return UpdateKind.MINOR
    return UpdateKind.PATCH


def _validate_state(state: UpdateState) -> UpdateState:
    if not isinstance(state.check_enabled, bool) or not isinstance(state.auto_patch, bool):
        raise ValidationError("更新偏好必须使用布尔值")
    if state.auto_patch and not state.check_enabled:
        raise ValidationError("关闭更新检测时不能启用补丁自动更新")
    if state.last_checked_on:
        try:
            date.fromisoformat(state.last_checked_on)
        except ValueError as exc:
            raise ValidationError("更新状态的检查日期无效") from exc
    if state.latest_version:
        _parse_stable_version(state.latest_version)
    return state


def load_update_state() -> UpdateState:
    path = _state_path()
    if not path.exists() and not path.is_symlink():
        return UpdateState()
    if path.is_symlink() or not path.is_file():
        raise ValidationError(f"更新偏好不是安全的普通文件：{path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"无法读取更新偏好：{path}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != UPDATE_SCHEMA_VERSION:
        raise ValidationError(f"更新偏好版本无效：{path}")
    expected = {
        "schema_version",
        "check_enabled",
        "auto_patch",
        "last_checked_on",
        "latest_version",
    }
    unknown = set(raw) - expected
    if unknown:
        raise ValidationError("更新偏好包含未知字段：" + ", ".join(sorted(unknown)))
    state = UpdateState(
        check_enabled=raw.get("check_enabled", True),
        auto_patch=raw.get("auto_patch", False),
        last_checked_on=raw.get("last_checked_on", ""),
        latest_version=raw.get("latest_version", ""),
    )
    if not isinstance(state.last_checked_on, str) or not isinstance(
        state.latest_version, str
    ):
        raise ValidationError("更新状态字段必须是字符串")
    return _validate_state(state)


def _write_update_state(state: UpdateState) -> None:
    state = _validate_state(state)
    payload = {
        "schema_version": UPDATE_SCHEMA_VERSION,
        "check_enabled": state.check_enabled,
        "auto_patch": state.auto_patch,
        "last_checked_on": state.last_checked_on,
        "latest_version": state.latest_version,
    }
    path = _state_path()
    try:
        atomic_write_text(
            path,
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        )
    except OSError as exc:
        raise DyroError(f"无法保存更新偏好：{path}") from exc


def set_update_enabled(enabled: bool) -> UpdateState:
    with exclusive_lock(_state_lock_path()):
        current = load_update_state()
        updated = replace(
            current,
            check_enabled=enabled,
            auto_patch=current.auto_patch if enabled else False,
        )
        _write_update_state(updated)
        return updated


def set_auto_patch(enabled: bool) -> UpdateState:
    with exclusive_lock(_state_lock_path()):
        current = load_update_state()
        updated = replace(
            current,
            check_enabled=True if enabled else current.check_enabled,
            auto_patch=enabled,
        )
        _write_update_state(updated)
        return updated


def fetch_latest_version(
    current_version: str,
    *,
    open_url: Callable[..., object] = urlopen,
    timeout: float = DEFAULT_CHECK_TIMEOUT,
) -> str:
    _parse_stable_version(current_version)
    request = Request(
        PYPI_JSON_URL,
        headers={
            "Accept": "application/json",
            "User-Agent": f"dyro/{current_version} update-check",
        },
        method="GET",
    )
    try:
        with open_url(request, timeout=timeout) as response:  # type: ignore[attr-defined]
            payload = response.read(PYPI_RESPONSE_LIMIT + 1)  # type: ignore[attr-defined]
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise DyroError("暂时无法连接官方 PyPI") from exc
    if len(payload) > PYPI_RESPONSE_LIMIT:
        raise DyroError("PyPI 更新响应过大，已拒绝处理")
    try:
        raw = json.loads(payload.decode("utf-8"))
        latest = raw["info"]["version"]
    except (UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise DyroError("PyPI 更新响应无效") from exc
    if not isinstance(latest, str):
        raise DyroError("PyPI 更新响应无效")
    try:
        _parse_stable_version(latest)
    except ValidationError as exc:
        raise DyroError("PyPI 返回的 Dyro 版本号无效") from exc
    return latest


def check_for_update(
    current_version: str,
    *,
    force: bool = False,
    persist: bool = True,
    today: date | None = None,
    fetch: Callable[[str], str] | None = None,
) -> UpdateResult:
    _parse_stable_version(current_version)
    checked_on = (today or date.today()).isoformat()
    fetch = fetch or fetch_latest_version
    if not persist:
        try:
            latest_version = fetch(current_version)
            kind = classify_update(current_version, latest_version)
        except DyroError as exc:
            return UpdateResult(
                checked=True,
                current_version=current_version,
                error=str(exc),
            )
        return UpdateResult(
            checked=True,
            current_version=current_version,
            latest_version=latest_version,
            kind=kind,
        )
    with exclusive_lock(
        _state_lock_path(), timeout_seconds=DAILY_LOCK_TIMEOUT
    ):
        state = load_update_state()
        cached_kind = (
            classify_update(current_version, state.latest_version)
            if state.latest_version
            else UpdateKind.NONE
        )
        if not force and (
            not state.check_enabled or state.last_checked_on == checked_on
        ):
            return UpdateResult(
                checked=False,
                current_version=current_version,
                latest_version=state.latest_version,
                kind=cached_kind,
            )
        _write_update_state(replace(state, last_checked_on=checked_on))
    try:
        latest_version = fetch(current_version)
        kind = classify_update(current_version, latest_version)
    except DyroError as exc:
        return UpdateResult(
            checked=True,
            current_version=current_version,
            latest_version=state.latest_version,
            kind=cached_kind,
            error=str(exc),
        )
    with exclusive_lock(
        _state_lock_path(), timeout_seconds=DAILY_LOCK_TIMEOUT
    ):
        current_state = load_update_state()
        _write_update_state(
            replace(
                current_state,
                latest_version=latest_version,
            )
        )
    return UpdateResult(
        checked=True,
        current_version=current_version,
        latest_version=latest_version,
        kind=kind,
    )


def build_update_plan(
    target_version: str,
    *,
    prefix: str | None = None,
    base_prefix: str | None = None,
    executable: str | None = None,
    which: Callable[[str], str | None] | None = None,
    editable: bool | None = None,
    user_install: bool | None = None,
    pip_available: bool | None = None,
) -> UpdatePlan:
    _parse_stable_version(target_version)
    if editable is None:
        editable = _is_editable_install()
    if editable:
        raise DyroError(
            "当前 Dyro 是 editable 源码安装；为避免覆盖开发环境，请通过 Git 更新源码"
        )
    prefix = prefix or sys.prefix
    base_prefix = base_prefix or sys.base_prefix
    executable = executable or sys.executable
    which = which or shutil.which
    normalized = prefix.replace("\\", "/").rstrip("/").lower()
    requirement = f"dyro=={target_version}"
    uv_managed = _managed_prefix(normalized, "/uv/tools/dyro")
    if uv_managed:
        uv = which("uv")
        if not uv:
            raise DyroError("当前 Dyro 由 uv tool 管理，但找不到 uv 命令")
        return UpdatePlan(
            "uv tool",
            (
                uv,
                "tool",
                "upgrade",
                "--default-index",
                PYPI_SIMPLE_URL,
                "--no-config",
                requirement,
            ),
            "当前 uv tool 隔离环境",
        )
    pipx_managed = _managed_prefix(normalized, "/pipx/venvs/dyro")
    if pipx_managed:
        pipx = which("pipx")
        if not pipx:
            raise DyroError("当前 Dyro 由 pipx 管理，但找不到 pipx 命令")
        return UpdatePlan(
            "pipx",
            (pipx, "upgrade", "--index-url", PYPI_SIMPLE_URL, "dyro"),
            "当前 pipx 隔离环境",
            constraint=requirement,
        )
    if user_install is None:
        user_install = _is_user_install()
    if pip_available is None:
        pip_available = _has_pip()
    if not pip_available:
        uv = which("uv")
        if uv and Path(prefix) != Path(base_prefix) and not user_install:
            return UpdatePlan(
                "uv pip",
                (
                    uv,
                    "pip",
                    "install",
                    "--python",
                    executable,
                    "--upgrade",
                    "--default-index",
                    PYPI_SIMPLE_URL,
                    "--no-config",
                    requirement,
                ),
                "当前 Python 虚拟环境",
            )
        raise DyroError(
            "当前 Python 环境没有 pip，也找不到可用的 uv；请先恢复原安装工具"
        )
    pip_argv = [
        executable,
        "-m",
        "pip",
        "--isolated",
        "install",
    ]
    if user_install:
        pip_argv.append("--user")
    pip_argv.extend(
        (
            "--upgrade",
            "--index-url",
            PYPI_SIMPLE_URL,
            requirement,
        )
    )
    return UpdatePlan(
        "pip",
        tuple(pip_argv),
        "当前 Python 用户环境" if user_install else "当前 Python 环境",
    )


def _managed_prefix(normalized_prefix: str, marker: str) -> bool:
    position = normalized_prefix.find(marker)
    if position < 0:
        return False
    end = position + len(marker)
    return end == len(normalized_prefix) or normalized_prefix[end] == "/"


def _is_editable_install() -> bool:
    try:
        direct_url = distribution("dyro").read_text("direct_url.json")
    except (PackageNotFoundError, OSError):
        return False
    if not direct_url:
        return False
    try:
        payload = json.loads(direct_url)
        directory = payload.get("dir_info", {})
    except (json.JSONDecodeError, AttributeError):
        return True
    return isinstance(directory, dict) and directory.get("editable") is True


def _is_user_install() -> bool:
    try:
        installed_root = Path(distribution("dyro").locate_file("")).resolve()
        user_root = Path(site.getusersitepackages()).resolve()
    except (PackageNotFoundError, OSError, TypeError):
        return False
    return installed_root == user_root or user_root in installed_root.parents


def _has_pip() -> bool:
    try:
        return find_spec("pip") is not None
    except (ImportError, ValueError):
        return False


def _run_update(
    argv: tuple[str, ...], *, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=600,
        env=env,
    )


def _update_environment() -> dict[str, str]:
    environment = dict(os.environ)
    untrusted_index_settings = {
        "PIP_CONFIG_FILE",
        "PIP_CONSTRAINT",
        "PIP_EXTRA_INDEX_URL",
        "PIP_FIND_LINKS",
        "PIP_INDEX_URL",
        "PIP_NO_INDEX",
        "PIP_REQUIREMENT",
        "PIP_TRUSTED_HOST",
        "UV_CONFIG_FILE",
        "UV_DEFAULT_INDEX",
        "UV_EXTRA_INDEX_URL",
        "UV_FIND_LINKS",
        "UV_INDEX",
        "UV_INDEX_URL",
        "UV_INSECURE_HOST",
        "UV_NO_INDEX",
    }
    for key in untrusted_index_settings:
        environment.pop(key, None)
    environment["PIP_CONFIG_FILE"] = os.devnull
    return environment


def _execute_update_plan(
    plan: UpdatePlan,
    run: Callable[..., subprocess.CompletedProcess[str]],
) -> subprocess.CompletedProcess[str]:
    environment = _update_environment()
    constraint_path: Path | None = None
    try:
        if plan.constraint:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix="dyro-update-", suffix=".txt", text=True
            )
            constraint_path = Path(temporary_name)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(plan.constraint + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            environment["PIP_CONSTRAINT"] = str(constraint_path)
        return run(plan.argv, env=environment)
    finally:
        if constraint_path is not None:
            constraint_path.unlink(missing_ok=True)


def perform_update(
    target_version: str,
    *,
    yes: bool,
    dry_run: bool,
    ask: Callable[[str], str] | None = None,
    run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    installed_version: Callable[[], str] | None = None,
) -> bool:
    plan = build_update_plan(target_version)
    ask = ask or input
    run = run or _run_update
    installed_version = installed_version or (lambda: distribution_version("dyro"))
    print(f"更新目标：Dyro {target_version}")
    print(f"安装方式：{plan.manager}（{plan.scope}）")
    print("执行命令：" + shlex.join(plan.argv))
    if plan.constraint:
        print("版本约束：" + plan.constraint)
    print("安全来源：官方 PyPI；版本已固定，命令不会由远程响应提供")
    if dry_run:
        print("DRY RUN: 未执行更新")
        return False
    if not yes:
        answer = ask("是否现在更新？[y/N]：").strip().lower()
        if answer not in {"y", "yes"}:
            print("已取消；Dyro 未更新。")
            return False
    try:
        completed = _execute_update_plan(plan, run)
    except subprocess.TimeoutExpired as exc:
        raise DyroError("Dyro 更新超时；请运行 dyro --version 检查当前状态后重试") from exc
    except OSError as exc:
        raise DyroError(f"无法启动 {plan.manager} 更新命令") from exc
    if completed.returncode != 0:
        output = (completed.stdout or "").strip()
        detail = f"\n{output}" if output else ""
        raise DyroError(f"Dyro 更新失败（退出码 {completed.returncode}）{detail}")
    try:
        actual = installed_version()
    except (PackageNotFoundError, OSError) as exc:
        raise DyroError("Dyro 更新后无法读取已安装版本") from exc
    if actual != target_version:
        raise DyroError(
            f"Dyro 更新后的版本验证失败：期望 {target_version}，实际 {actual}"
        )
    print(f"Dyro 已更新到 {target_version}；下次运行将使用新版本。")
    return True

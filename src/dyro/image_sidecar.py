"""Optional local-image-gen discovery. Not a coding tool and not a Skill seat.

``dyro doctor`` may only ask whether the PATH wrapper exists. The only process
that may spawn ``local-image-gen --doctor`` is ``dyro image doctor``. Dyro never
wraps billed image generation.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import shutil
import subprocess
from typing import Callable
import webbrowser

from .errors import DyroError


SIDECAR_ID = "local-image-gen"
WRAPPER_NAME = "local-image-gen"
SOURCE_URL = "https://github.com/DandreYang/local-image-gen"
INSTALL_SCRIPT_URL = (
    "https://raw.githubusercontent.com/DandreYang/local-image-gen/main/install.sh"
)
DOCTOR_TIMEOUT_SECONDS = 5
ABSENT_INFO_LINE = (
    f"未安装 {SIDECAR_ID}（可选）。来源：{SOURCE_URL}"
)

PresenceState = str
ProbeState = str


@dataclass(frozen=True)
class SidecarPresence:
    """Cheap PATH discovery used by workspace ``dyro doctor``."""

    id: str = SIDECAR_ID
    optional: bool = True
    state: PresenceState = "absent"

    def as_dict(self) -> dict[str, object]:
        return {"id": self.id, "optional": self.optional, "state": self.state}


@dataclass(frozen=True)
class SidecarProbe:
    """Normalized ``local-image-gen --doctor`` result. Never a raw passthrough."""

    id: str = SIDECAR_ID
    optional: bool = True
    state: ProbeState = "absent"
    version: str | None = None
    usable_providers: tuple[str, ...] = ()
    output_dir: str | None = None
    workspace: str | None = None
    message: str = ""

    def as_dict(self, *, include_paths: bool = False) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": self.id,
            "optional": self.optional,
            "state": self.state,
        }
        if self.version is not None:
            payload["version"] = self.version
        if self.state != "absent":
            payload["usable_providers"] = list(self.usable_providers)
        if include_paths:
            if self.output_dir:
                payload["output_dir"] = self.output_dir
            if self.workspace:
                payload["workspace"] = self.workspace
        return payload


def which_wrapper(name: str = WRAPPER_NAME) -> str | None:
    return shutil.which(name)


def run_sidecar_doctor(
    executable: str, *, timeout: float = DOCTOR_TIMEOUT_SECONDS
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (executable, "--doctor"),
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        text=True,
        timeout=timeout,
    )


def open_source_url(url: str) -> bool:
    return webbrowser.open(url)


def discover_sidecar(
    *, which: Callable[[str], str | None] | None = None
) -> SidecarPresence:
    """Return absent/present from PATH. Never spawn the sidecar."""

    lookup = which or which_wrapper
    try:
        found = lookup(WRAPPER_NAME)
    except OSError:
        found = None
    return SidecarPresence(state="present" if found else "absent")


def probe_sidecar(
    *,
    which: Callable[[str], str | None] | None = None,
    run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    timeout: float = DOCTOR_TIMEOUT_SECONDS,
) -> SidecarProbe:
    """Spawn ``local-image-gen --doctor`` once and normalize the envelope."""

    lookup = which or which_wrapper
    try:
        executable = lookup(WRAPPER_NAME)
    except OSError:
        executable = None
    if not executable:
        return SidecarProbe(state="absent", message=ABSENT_INFO_LINE)
    runner = run or run_sidecar_doctor
    try:
        completed = runner(executable, timeout=timeout)
    except subprocess.TimeoutExpired:
        return SidecarProbe(state="unavailable", message="sidecar 不可读：探测超时")
    except (OSError, subprocess.SubprocessError, TypeError):
        return SidecarProbe(state="unavailable", message="sidecar 不可读")
    if completed.returncode != 0:
        return SidecarProbe(state="unavailable", message="sidecar 不可读")
    return normalize_doctor_json(completed.stdout)


def normalize_doctor_json(raw: object) -> SidecarProbe:
    """Accept exactly one JSON object. Missing fields are unknown, not fatal."""

    if not isinstance(raw, str):
        return SidecarProbe(state="unavailable", message="sidecar 不可读")
    text = raw.strip()
    if not text:
        return SidecarProbe(state="unavailable", message="sidecar 不可读")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return SidecarProbe(state="unavailable", message="sidecar 不可读")
    if not isinstance(payload, dict):
        return SidecarProbe(state="unavailable", message="sidecar 不可读")

    version = payload.get("version")
    version_text = version if isinstance(version, str) and version else None
    usable = _usable_providers(payload.get("providers"))
    output_dir, workspace = _optional_paths(payload.get("dyro"))
    success = payload.get("success")
    if success is True and usable:
        state: ProbeState = "ready"
        message = ""
    elif success is False:
        state = "unavailable"
        message = "sidecar 不可读"
    else:
        state = "needs_setup"
        message = "已安装 local-image-gen，但没有可用订阅或 API key。"
    return SidecarProbe(
        state=state,
        version=version_text,
        usable_providers=usable,
        output_dir=output_dir,
        workspace=workspace,
        message=message,
    )


def render_install_guide() -> str:
    return "\n".join(
        (
            f"未检测到 {SIDECAR_ID}",
            f"官方来源：{SOURCE_URL}",
            f"安装脚本：{INSTALL_SCRIPT_URL}",
            "安装范围：用户 PATH 上的包装命令（不是编码工具，也不是托管座位）",
            "安装方式：打开官方安装页面",
            "安全说明：Dyro 不会代为执行远程安装脚本",
            "安装之后：确认 PATH 上有 local-image-gen 后运行 dyro image doctor",
        )
    )


def install_image_sidecar(
    *,
    yes: bool,
    dry_run: bool,
    ask: Callable[[str], str] | None = None,
    open_url: Callable[[str], bool] | None = None,
) -> bool:
    """Print the official source. Never download or execute install.sh."""

    ask = ask or input
    opener = open_url or open_source_url
    print(render_install_guide())
    if dry_run:
        print("DRY RUN: 未安装、未打开浏览器")
        return False
    if not yes:
        confirmed = ask("是否继续？[y/N]：").strip().lower()
        if confirmed not in {"y", "yes"}:
            print("已取消；没有安装任何工具。")
            return False
    if not opener(SOURCE_URL):
        print(f"未能自动打开浏览器，请手动访问：{SOURCE_URL}")
    else:
        print("已打开官方安装页面；安装完成后重新运行 dyro image doctor。")
    return False


def require_interactive_install(*, yes: bool, dry_run: bool, tty: bool) -> None:
    if not yes and not dry_run and not tty:
        raise DyroError(
            "非交互环境不会打开安装页面；请在终端中运行，或审阅计划后显式添加 --yes"
        )


def _usable_providers(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    names: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = item.get("provider")
        if not isinstance(name, str) or not name or name in seen:
            continue
        if item.get("subscription") is True or item.get("api_key") is True:
            seen.add(name)
            names.append(name)
    return tuple(names)


def _optional_paths(raw: object) -> tuple[str | None, str | None]:
    if not isinstance(raw, dict):
        return None, None
    output_dir = raw.get("output_dir")
    workspace = raw.get("workspace")
    return (
        output_dir if isinstance(output_dir, str) and output_dir else None,
        workspace if isinstance(workspace, str) and workspace else None,
    )

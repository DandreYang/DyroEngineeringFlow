"""Read-only operator diagnostics for the optional external runtime.

The doctor proves only whether this host can exercise the local Stage5
substrate.  It deliberately does not turn environment observations into a
production approval; the production gate remains a separate fail-closed
decision.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import shutil
import subprocess
from typing import Sequence

from .sandbox import BUN_IMAGE
from .stage1.package_runtime import (
    RUNTIME_SOURCE,
    hash_runtime_tree,
    load_runtime_lock,
    verify_runtime_lock,
)
from .stage5.host_provider import pin_host_provider


PROBE_TIMEOUT_SECONDS = 5
RUNTIME_LOCK_PATH = Path(__file__).resolve().parent / "runtime-lock.json"


@dataclass(frozen=True)
class DiagnosticCheck:
    id: str
    label: str
    status: str  # pass | fail | blocked | not_configured
    detail: str
    remediation: str
    blocks_local: bool


def _check(
    id: str,
    label: str,
    status: str,
    detail: str,
    remediation: str = "",
    *,
    blocks_local: bool,
) -> DiagnosticCheck:
    return DiagnosticCheck(
        id=id,
        label=label,
        status=status,
        detail=detail,
        remediation=remediation,
        blocks_local=blocks_local,
    )


def _probe(argv: Sequence[str]) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            tuple(argv),
            timeout=PROBE_TIMEOUT_SECONDS,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    output = (completed.stdout or "").strip()
    if completed.returncode != 0:
        return False, output or f"exit code {completed.returncode}"
    return True, output


def _runtime_identity_check() -> DiagnosticCheck:
    try:
        content_sha256 = hash_runtime_tree(RUNTIME_SOURCE)
        runtime_lock = load_runtime_lock(RUNTIME_LOCK_PATH)
        verify_runtime_lock(runtime_lock, content_sha256=content_sha256)
    except (OSError, ValueError, RuntimeError) as exc:
        return _check(
            "LOCAL-01",
            "Runtime 身份",
            "fail",
            f"Runtime lock 校验失败：{exc}",
            "恢复已评审的 runtime 源码树并重新生成获批 lock。",
            blocks_local=True,
        )
    return _check(
        "LOCAL-01",
        "Runtime 身份",
        "pass",
        f"已评审 runtime 源码树匹配 lock {content_sha256[:12]}…",
        blocks_local=True,
    )


def _provider_check(
    provider_path: Path | None,
    provider_roots: Sequence[Path],
) -> DiagnosticCheck:
    if provider_path is None:
        return _check(
            "LOCAL-05",
            "Host provider 钉扎",
            "not_configured",
            "未提供真实 provider 路径；仍可使用确定性 fixture 模式。",
            (
                "执行舰队 canary 时，请提供 --provider-path 与至少一个 "
                "--provider-root 白名单。"
            ),
            blocks_local=False,
        )
    if not provider_roots:
        return _check(
            "LOCAL-05",
            "Host provider 钉扎",
            "fail",
            "已提供 provider 路径，但缺少显式允许根目录。",
            "为 operator 管理的二进制目录添加 --provider-root。",
            blocks_local=True,
        )
    try:
        pin = pin_host_provider(
            Path(provider_path),
            allowed_roots=tuple(Path(root) for root in provider_roots),
        )
        verified = pin.verify()
    except (OSError, ValueError, RuntimeError) as exc:
        return _check(
            "LOCAL-05",
            "Host provider 钉扎",
            "fail",
            f"Provider 内容钉扎校验失败：{exc}",
            (
                "使用显式 --provider-root 下的绝对路径普通文件；文件不能是"
                "符号链接或全局可写。"
            ),
            blocks_local=True,
        )
    return _check(
        "LOCAL-05",
        "Host provider 钉扎",
        "pass",
        f"已将 {verified.name} 钉扎为 {pin.content_sha256[:12]}…",
        blocks_local=True,
    )


def collect_runtime_diagnostics(
    *,
    provider_path: Path | None = None,
    provider_roots: Sequence[Path] = (),
) -> dict[str, object]:
    """Inspect the local substrate without writing state or pulling images."""
    checks: list[DiagnosticCheck] = [_runtime_identity_check()]
    docker = shutil.which("docker")
    if docker is None:
        checks.extend(
            (
                _check(
                    "LOCAL-02",
                    "Docker CLI",
                    "fail",
                    "PATH 中找不到 Docker CLI。",
                    "安装 Docker 或 operator 批准的兼容 CLI。",
                    blocks_local=True,
                ),
                _check(
                    "LOCAL-03",
                    "Docker daemon",
                    "blocked",
                    "Docker CLI 不可用，已跳过 daemon 探测。",
                    blocks_local=False,
                ),
                _check(
                    "LOCAL-04",
                    "钉扎 runtime 镜像",
                    "blocked",
                    "Docker CLI 不可用，已跳过镜像探测。",
                    blocks_local=False,
                ),
            )
        )
    else:
        checks.append(
            _check(
                "LOCAL-02",
                "Docker CLI",
                "pass",
                f"通过绝对 PATH 解析使用 {Path(docker).name}。",
                blocks_local=True,
            )
        )
        daemon_ok, daemon_detail = _probe(
            (docker, "version", "--format", "{{.Server.Version}}")
        )
        checks.append(
            _check(
                "LOCAL-03",
                "Docker daemon",
                "pass" if daemon_ok else "fail",
                (
                    f"Docker server 版本 {daemon_detail}"
                    if daemon_ok
                    else f"无法连接 Docker daemon：{daemon_detail}"
                ),
                "" if daemon_ok else "启动获批 Docker daemon 后重试。",
                blocks_local=True,
            )
        )
        if daemon_ok:
            image_ok, image_detail = _probe(
                (docker, "image", "inspect", BUN_IMAGE, "--format", "{{.Id}}")
            )
            checks.append(
                _check(
                    "LOCAL-04",
                    "钉扎 runtime 镜像",
                    "pass" if image_ok else "fail",
                    (
                        f"钉扎镜像已存在：{image_detail}"
                        if image_ok
                        else f"钉扎镜像不可用：{image_detail}"
                    ),
                    "" if image_ok else f"拉取精确获批镜像：docker pull {BUN_IMAGE}",
                    blocks_local=True,
                )
            )
        else:
            checks.append(
                _check(
                    "LOCAL-04",
                    "钉扎 runtime 镜像",
                    "blocked",
                    "Docker daemon 不可用，已跳过镜像探测。",
                    blocks_local=False,
                )
            )
    checks.append(_provider_check(provider_path, provider_roots))

    blocking = [
        item
        for item in checks
        if item.blocks_local and item.status != "pass"
    ]
    ready_for_local_poc = not blocking
    next_steps: list[str] = []
    for item in checks:
        if item.status != "pass" and item.remediation:
            if item.remediation not in next_steps:
                next_steps.append(item.remediation)
    next_steps.append(
        "使用 dyro runtime plan 查看剩余生产阻断项。"
    )
    return {
        "schema_version": 1,
        "kind": "external-semantic-runtime-doctor",
        "mode": "local",
        "verdict": "PASS" if ready_for_local_poc else "BLOCKED",
        "ready_for_local_poc": ready_for_local_poc,
        "production_ready": False,
        "blocking_count": len(blocking),
        "checks": [asdict(item) for item in checks],
        "next_steps": next_steps,
        "notes": [
            "该命令只读且不会拉取镜像。",
            "本地 doctor 通过不能清除生产阻断项。",
            "Runtime 永不拥有 review、signoff、merge 或 push 权限。",
        ],
    }

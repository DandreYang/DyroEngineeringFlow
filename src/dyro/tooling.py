from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
from pathlib import Path
import shlex
import shutil
import subprocess
from typing import Callable, Sequence
import webbrowser

from .config import validate_id
from .errors import DyroError, ValidationError
from .hub import registry_home
from .state import atomic_write_text, exclusive_lock


TOOLS_SCHEMA_VERSION = 1
TOOLS_FILE = "tools.json"
TOOLS_LOCK = "tools.lock"


class ToolState(str, Enum):
    READY = "ready"
    NEEDS_SETUP = "needs_setup"
    INSTALLABLE = "installable"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class InstallGuide:
    source_url: str
    scope: str
    argv: tuple[str, ...] = ()
    prerequisite: str = ""
    remote_script_only: bool = False
    post_install: str = ""
    risk: str = ""


@dataclass(frozen=True)
class ToolDefinition:
    id: str
    label: str
    command: str
    interface: str
    launch: tuple[str, ...]
    environment: tuple[tuple[str, str], ...] = ()
    install: InstallGuide | None = None


@dataclass(frozen=True)
class ToolPreferences:
    default_tool: str = ""
    pinned_tools: tuple[str, ...] = ()


_NPM_SCOPE = "当前 npm 全局环境（不会使用 sudo；权限不足时会失败）"
_NPM_RISK = "将联网下载软件包，并可能执行软件包提供的安装脚本"


TOOL_DEFINITIONS = (
    ToolDefinition(
        "antigravity",
        "Antigravity CLI",
        "agy",
        "terminal",
        ("agy",),
        install=InstallGuide(
            "https://antigravity.google/download",
            "Antigravity 官方用户级安装目录",
            remote_script_only=True,
        ),
    ),
    ToolDefinition(
        "codex",
        "Codex",
        "codex",
        "terminal",
        ("codex", "-C", "{workspace}"),
        install=InstallGuide(
            "https://developers.openai.com/codex/cli/",
            _NPM_SCOPE,
            ("npm", "install", "-g", "@openai/codex@latest"),
            prerequisite="npm",
            risk=_NPM_RISK,
        ),
    ),
    ToolDefinition(
        "claude",
        "Claude Code",
        "claude",
        "terminal",
        ("claude",),
        install=InstallGuide(
            "https://docs.anthropic.com/en/docs/claude-code/getting-started",
            _NPM_SCOPE,
            ("npm", "install", "-g", "@anthropic-ai/claude-code@latest"),
            prerequisite="npm",
            risk=_NPM_RISK,
        ),
    ),
    ToolDefinition(
        "cursor-desktop",
        "Cursor Desktop",
        "cursor",
        "desktop",
        (),
        install=InstallGuide(
            "https://cursor.com/download",
            "当前操作系统的桌面应用",
            remote_script_only=True,
        ),
    ),
    ToolDefinition(
        "cursor-agent",
        "Cursor CLI",
        "cursor-agent",
        "terminal",
        ("cursor-agent", "--workspace", "{workspace}"),
        install=InstallGuide(
            "https://docs.cursor.com/en/cli/installation",
            "Cursor 官方用户级安装目录",
            remote_script_only=True,
        ),
    ),
    ToolDefinition("grok", "Grok", "grok", "terminal", ("grok", "--cwd", "{workspace}")),
    ToolDefinition(
        "opencode",
        "OpenCode",
        "opencode",
        "terminal",
        ("opencode", "{workspace}"),
        install=InstallGuide(
            "https://opencode.ai/docs",
            _NPM_SCOPE,
            ("npm", "install", "-g", "opencode-ai@latest"),
            prerequisite="npm",
            risk=_NPM_RISK,
        ),
    ),
    ToolDefinition(
        "openclaw",
        "OpenClaw",
        "openclaw",
        "runtime",
        ("openclaw",),
        environment=(("OPENCLAW_WORKSPACE_DIR", "{workspace}"),),
        install=InstallGuide(
            "https://docs.openclaw.ai/install",
            _NPM_SCOPE,
            ("npm", "install", "-g", "openclaw@latest"),
            prerequisite="npm",
            post_install="首次启动将进入 OpenClaw 官方 onboarding",
            risk=_NPM_RISK + "；OpenClaw 可在用户授权范围内访问文件、Shell 和网络",
        ),
    ),
    ToolDefinition(
        "hermes",
        "Hermes",
        "hermes",
        "terminal",
        ("hermes",),
        install=InstallGuide(
            "https://github.com/NousResearch/hermes-agent/blob/main/website/docs/getting-started/quickstart.md",
            "Hermes 官方用户级安装目录",
            remote_script_only=True,
        ),
    ),
    ToolDefinition(
        "kimi",
        "Kimi Code",
        "kimi",
        "terminal",
        ("kimi",),
        install=InstallGuide(
            "https://www.kimi.com/code/docs/kimi-code-cli/guides/getting-started.html",
            _NPM_SCOPE,
            ("npm", "install", "-g", "@moonshot-ai/kimi-code@latest"),
            prerequisite="npm",
            risk=_NPM_RISK,
        ),
    ),
    ToolDefinition(
        "qoder",
        "Qoder CLI",
        "qodercli",
        "terminal",
        ("qodercli",),
        install=InstallGuide(
            "https://docs.qoder.com/en/cli/quick-start",
            _NPM_SCOPE,
            ("npm", "install", "-g", "@qoder-ai/qodercli"),
            prerequisite="npm",
            risk=_NPM_RISK,
        ),
    ),
    ToolDefinition(
        "zcode",
        "ZCode",
        "zcode",
        "desktop",
        ("zcode", "{workspace}"),
        install=InstallGuide(
            "https://zcode.z.ai/en/docs/install",
            "当前操作系统的桌面应用",
            remote_script_only=True,
        ),
    ),
)


_TOOL_BY_ID = {definition.id: definition for definition in TOOL_DEFINITIONS}
_TOOL_BY_COMMAND = {
    definition.command: definition for definition in TOOL_DEFINITIONS
}


def tool_definition(tool_id: str) -> ToolDefinition | None:
    return _TOOL_BY_ID.get(tool_id)


def tool_definition_for_command(command: str) -> ToolDefinition | None:
    return _TOOL_BY_COMMAND.get(command)


def _preferences_path() -> Path:
    return registry_home() / TOOLS_FILE


def _preferences_lock_path() -> Path:
    return registry_home() / TOOLS_LOCK


def _validate_preferences(preferences: ToolPreferences) -> ToolPreferences:
    if preferences.default_tool:
        validate_id(preferences.default_tool, "default_tool")
    for tool_id in preferences.pinned_tools:
        validate_id(tool_id, "pinned_tools")
    if len(set(preferences.pinned_tools)) != len(preferences.pinned_tools):
        raise ValidationError("pinned_tools 不能包含重复工具")
    return preferences


def load_tool_preferences() -> ToolPreferences:
    path = _preferences_path()
    if not path.exists() and not path.is_symlink():
        return ToolPreferences()
    if path.is_symlink() or not path.is_file():
        raise ValidationError(f"编码工具偏好不是安全的普通文件：{path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"无法读取编码工具偏好：{path}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != TOOLS_SCHEMA_VERSION:
        raise ValidationError(f"编码工具偏好版本无效：{path}")
    unknown = set(raw) - {"schema_version", "default_tool", "pinned_tools"}
    if unknown:
        raise ValidationError(
            "编码工具偏好包含未知字段：" + ", ".join(sorted(unknown))
        )
    default_tool = raw.get("default_tool", "")
    pinned_tools = raw.get("pinned_tools", [])
    if not isinstance(default_tool, str) or not isinstance(pinned_tools, list):
        raise ValidationError(f"编码工具偏好结构无效：{path}")
    if not all(isinstance(tool_id, str) for tool_id in pinned_tools):
        raise ValidationError("pinned_tools 必须是工具 ID 字符串数组")
    return _validate_preferences(
        ToolPreferences(default_tool, tuple(pinned_tools))
    )


def save_tool_preferences(preferences: ToolPreferences) -> None:
    preferences = _validate_preferences(preferences)
    with exclusive_lock(_preferences_lock_path()):
        _write_tool_preferences(preferences)


def _write_tool_preferences(preferences: ToolPreferences) -> None:
    payload = {
        "schema_version": TOOLS_SCHEMA_VERSION,
        "default_tool": preferences.default_tool,
        "pinned_tools": list(preferences.pinned_tools),
    }
    atomic_write_text(
        _preferences_path(),
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    )


def set_default_tool(tool_id: str) -> ToolPreferences:
    if tool_id:
        validate_id(tool_id, "编码工具 ID")
    with exclusive_lock(_preferences_lock_path()):
        current = load_tool_preferences()
        updated = _validate_preferences(
            ToolPreferences(tool_id, current.pinned_tools)
        )
        _write_tool_preferences(updated)
        return updated


def set_pinned_tools(tool_ids: Sequence[str]) -> ToolPreferences:
    with exclusive_lock(_preferences_lock_path()):
        current = load_tool_preferences()
        updated = _validate_preferences(
            ToolPreferences(current.default_tool, tuple(tool_ids))
        )
        _write_tool_preferences(updated)
        return updated


def _run_install(
    argv: tuple[str, ...], *, check: bool
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, check=check)


def install_tool(
    tool_id: str,
    *,
    yes: bool,
    dry_run: bool,
    ask: Callable[[str], str] | None = None,
    run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    open_url: Callable[[str], bool] | None = None,
) -> bool:
    ask = ask or input
    run = run or _run_install
    open_url = open_url or webbrowser.open
    definition = tool_definition(tool_id)
    if definition is None or definition.install is None:
        raise DyroError(f"{tool_id} 没有内置安装方案；请按该工具官方文档安装")
    guide = definition.install
    print(f"\n未检测到 {definition.label}")
    print(f"官方来源：{guide.source_url}")
    print(f"安装范围：{guide.scope}")
    if guide.argv:
        print("安装命令：" + shlex.join(guide.argv))
    else:
        print("安装方式：打开官方安装页面")
        if guide.remote_script_only:
            print("安全说明：Dyro 不会代为执行远程安装脚本")
    if guide.post_install:
        print("安装之后：" + guide.post_install)
    if guide.risk:
        print("权限提示：" + guide.risk)
    if dry_run:
        print("DRY RUN: 未安装、未打开浏览器")
        return False
    prerequisite_path = ""
    if guide.prerequisite:
        prerequisite_path = shutil.which(guide.prerequisite) or ""
        if not prerequisite_path:
            raise DyroError(
                f"安装 {definition.label} 需要 {guide.prerequisite}；"
                f"请先准备该工具，或查看 {guide.source_url}"
            )
    if not yes:
        confirmed = ask("是否继续？[y/N]：").strip().lower()
        if confirmed not in {"y", "yes"}:
            print("已取消；没有安装任何工具。")
            return False
    if not guide.argv:
        if not open_url(guide.source_url):
            print(f"未能自动打开浏览器，请手动访问：{guide.source_url}")
        else:
            print("已打开官方安装页面；安装完成后重新运行 dyro。")
        return False
    argv = (
        (prerequisite_path, *guide.argv[1:])
        if prerequisite_path and guide.argv[0] == guide.prerequisite
        else guide.argv
    )
    completed = run(tuple(argv), check=False)
    if completed.returncode != 0:
        raise DyroError(
            f"{definition.label} 安装命令失败（exit {completed.returncode}）；"
            f"请查看 {guide.source_url}"
        )
    executable = shutil.which(definition.command)
    if executable is None:
        print(
            f"{definition.label} 安装命令已完成，但当前 PATH 尚未发现 "
            f"{definition.command}；请重新打开终端后再运行 dyro。"
        )
        return True
    verification = run((executable, "--version"), check=False)
    if verification.returncode != 0:
        raise DyroError(
            f"{definition.label} 已安装但版本验证失败"
            f"（exit {verification.returncode}）；请查看 {guide.source_url}"
        )
    print(f"{definition.label} 已通过 --version 验证，正在重新检测。")
    return True

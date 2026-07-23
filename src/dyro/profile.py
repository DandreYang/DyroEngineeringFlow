from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
from typing import Iterable

from .config import Adapter, CONFIG_NAME, Config, expand_argv, load, validate_id
from .errors import DyroError, ValidationError
from .state import atomic_write_text, exclusive_lock


_BARE_TOML_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
_MANAGED_VALUES = frozenset(
    {
        "workspace.name",
        "policy.default_base",
        "policy.task_branch_prefix",
        "policy.allow_push",
        "policy.require_external_signoff",
        "policy.execution_mode",
        "policy.require_clean_merge",
    }
)


def _toml_table_key(value: str) -> str:
    return value if _BARE_TOML_KEY.fullmatch(value) else json.dumps(value, ensure_ascii=False)


def _validate_argv(argv: Iterable[str], label: str) -> tuple[str, ...]:
    value = tuple(argv)
    if not value or not all(isinstance(item, str) and item for item in value):
        raise ValidationError(f"{label} 必须是非空 argv 数组")
    return value


def preset_adapter(adapter_id: str, preset: str) -> Adapter:
    validate_id(adapter_id, "adapter id")
    if preset == "codex":
        return Adapter(
            adapter_id,
            ("codex", "-C", "{workspace}"),
            ("codex", "exec", "--skip-git-repo-check", "--sandbox", "workspace-write", "{prompt}"),
            ("codex", "exec", "--skip-git-repo-check", "--sandbox", "workspace-write", "{prompt}"),
        )
    if preset == "noop":
        return Adapter(adapter_id, ("/usr/bin/true",), ("/usr/bin/true",), ("/usr/bin/true",))
    raise ValidationError(f"未知 Agent preset：{preset}")


def command_adapter(adapter_id: str, argv: Iterable[str]) -> Adapter:
    validate_id(adapter_id, "adapter id")
    command = _validate_argv(argv, "Agent command")
    return Adapter(adapter_id, command, command, command)


def append_adapter(config: Config, adapter: Adapter, *, dry_run: bool = False) -> None:
    """Register an adapter without reformatting the rest of a Profile."""
    validate_id(adapter.id, "adapter id")
    launch = _validate_argv(adapter.launch, "launch")
    read = _validate_argv(adapter.read, "read")
    write = _validate_argv(adapter.write, "write")
    if dry_run:
        return
    with exclusive_lock(config.root / ".dyro" / "profile.lock"):
        current = load(config.root)
        if adapter.id in current.adapters:
            raise DyroError(f"Agent adapter 已配置：{adapter.id}")
        lines = [
            f"[adapters.{_toml_table_key(adapter.id)}]",
            f"launch = {json.dumps(list(launch), ensure_ascii=False)}",
            f"read = {json.dumps(list(read), ensure_ascii=False)}",
            f"write = {json.dumps(list(write), ensure_ascii=False)}",
        ]
        config_file = current.root / CONFIG_NAME
        content = config_file.read_text(encoding="utf-8").rstrip() + "\n\n" + "\n".join(lines) + "\n"
        atomic_write_text(config_file, content)


def config_value(config: Config, key: str) -> str | bool:
    if key not in _MANAGED_VALUES:
        raise ValidationError(f"不支持的配置键：{key}")
    if key == "workspace.name":
        return config.name
    attribute = key.partition(".")[2]
    return getattr(config.policy, attribute)


def _parse_value(key: str, raw: str) -> str | bool:
    if key not in _MANAGED_VALUES:
        raise ValidationError(f"不支持的配置键：{key}")
    if key in ("policy.allow_push", "policy.require_external_signoff", "policy.require_clean_merge"):
        normalized = raw.strip().lower()
        if normalized not in ("true", "false"):
            raise ValidationError(f"{key} 只能是 true 或 false")
        value = normalized == "true"
        if key == "policy.require_clean_merge" and not value:
            raise ValidationError("policy.require_clean_merge 必须保持 true")
        return value
    value = raw.strip()
    if not value or "\n" in value or "\r" in value:
        raise ValidationError(f"{key} 必须是单行非空字符串")
    if key == "workspace.name":
        validate_id(value, "workspace.name")
    if key == "policy.execution_mode" and value not in ("local", "external"):
        raise ValidationError("policy.execution_mode 只能是 local 或 external")
    return value


def _replace_toml_value(content: str, section: str, key: str, rendered: str) -> str:
    header = re.compile(rf"(?m)^\[{re.escape(section)}\]\s*$")
    match = header.search(content)
    if match is None:
        raise ValidationError(f"配置缺少 [{section}] 段")
    body_start = match.end()
    next_header = re.compile(r"(?m)^\[").search(content, body_start)
    body_end = next_header.start() if next_header else len(content)
    body = content[body_start:body_end]
    assignment = re.compile(rf"(?m)^{re.escape(key)}\s*=.*$")
    replacement = f"{key} = {rendered}"
    if assignment.search(body):
        updated_body = assignment.sub(replacement, body, count=1)
    else:
        updated_body = body.rstrip() + "\n" + replacement + "\n"
    return content[:body_start] + updated_body + content[body_end:]


def set_config_value(config: Config, key: str, raw: str, *, dry_run: bool = False) -> str | bool:
    value = _parse_value(key, raw)
    section, _, field = key.partition(".")
    rendered = "true" if value is True else "false" if value is False else json.dumps(value, ensure_ascii=False)
    if not dry_run:
        with exclusive_lock(config.root / ".dyro" / "profile.lock"):
            current = load(config.root)
            config_file = current.root / CONFIG_NAME
            updated = _replace_toml_value(config_file.read_text(encoding="utf-8"), section, field, rendered)
            atomic_write_text(config_file, updated)
    return value


def test_adapter(config: Config, adapter_id: str) -> tuple[tuple[str, bool, str], ...]:
    try:
        adapter = config.adapters[adapter_id]
    except KeyError as exc:
        raise DyroError(f"未配置 Agent adapter：{adapter_id}") from exc
    values = {"workspace": config.root, "root": config.root, "task": "DYRO-PROBE", "line": "dyro-probe", "prompt": "adapter probe"}
    checks: list[tuple[str, bool, str]] = []
    for mode, template in (("launch", adapter.launch), ("read", adapter.read), ("write", adapter.write)):
        argv = expand_argv(template, **values)
        executable = argv[0]
        if "/" in executable or "\\" in executable:
            available = Path(executable).is_file() and os_access_executable(Path(executable))
        else:
            available = shutil.which(executable) is not None
        checks.append((mode, available, executable))
    return tuple(checks)


def os_access_executable(path: Path) -> bool:
    import os

    return os.access(path, os.X_OK)

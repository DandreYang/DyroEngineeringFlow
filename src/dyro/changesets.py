from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import tomllib
from typing import Iterable

from .config import Config, validate_id
from .errors import DyroError, ValidationError
from .process import git, require_ok
from .state import atomic_write_text
from .workspace import get_line, line_repository_path


@dataclass(frozen=True)
class ChangeSet:
    id: str
    line: str
    branch: str
    repositories: tuple[str, ...]
    heads: dict[str, str]
    created_at: str


def _path(config: Config, changeset_id: str) -> Path:
    validate_id(changeset_id, "Change Set ID")
    return config.changesets_dir / f"{changeset_id}.toml"


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _write(config: Config, changeset: ChangeSet) -> None:
    path = _path(config, changeset.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    repository_items = ", ".join(_toml_string(repository) for repository in changeset.repositories)
    chunks = [
        "schema_version = 1",
        f"id = {_toml_string(changeset.id)}",
        f"line = {_toml_string(changeset.line)}",
        f"branch = {_toml_string(changeset.branch)}",
        f"created_at = {_toml_string(changeset.created_at)}",
        f"repositories = [{repository_items}]",
        "",
        "[heads]",
    ]
    chunks.extend(f"{_toml_string(repository)} = {_toml_string(changeset.heads[repository])}" for repository in changeset.repositories)
    atomic_write_text(path, "\n".join((*chunks, "")))


def _parse(path: Path) -> ChangeSet:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ValidationError(f"Change Set 格式错误：{path}: {exc}") from exc
    if raw.get("schema_version") != 1:
        raise ValidationError(f"不支持的 Change Set 版本：{path}")
    changeset_id = validate_id(str(raw.get("id", "")), "Change Set ID")
    line = validate_id(str(raw.get("line", "")), "开发线 ID")
    branch = str(raw.get("branch", "")).strip()
    created_at = str(raw.get("created_at", "")).strip()
    repositories_raw = raw.get("repositories")
    heads_raw = raw.get("heads")
    if not branch or not created_at or not isinstance(repositories_raw, list) or not repositories_raw or not isinstance(heads_raw, dict):
        raise ValidationError(f"Change Set 缺少 branch/created_at/repositories/heads：{path}")
    repositories = tuple(validate_id(str(item), "Change Set repository") for item in repositories_raw)
    if len(set(repositories)) != len(repositories):
        raise ValidationError(f"Change Set 仓库不能重复：{path}")
    if set(heads_raw) != set(repositories) or not all(isinstance(value, str) and value for value in heads_raw.values()):
        raise ValidationError(f"Change Set heads 必须与 repositories 一一对应：{path}")
    return ChangeSet(changeset_id, line, branch, repositories, {repository: str(heads_raw[repository]) for repository in repositories}, created_at)


def get_changeset(config: Config, changeset_id: str) -> ChangeSet:
    path = _path(config, changeset_id)
    if not path.is_file():
        raise DyroError(f"未找到 Change Set：{changeset_id}")
    return _parse(path)


def list_changesets(config: Config) -> list[ChangeSet]:
    if not config.changesets_dir.exists():
        return []
    return [_parse(path) for path in sorted(config.changesets_dir.glob("*.toml"))]


def create_changeset(
    config: Config,
    *,
    changeset_id: str,
    line_id: str,
    repositories: Iterable[str] | None = None,
    dry_run: bool = False,
) -> ChangeSet:
    if _path(config, changeset_id).exists():
        raise DyroError(f"Change Set 已存在：{changeset_id}")
    line = get_line(config, line_id)
    selected = tuple(repositories or line.repositories)
    if not selected:
        raise ValidationError("Change Set 至少包含一个仓库")
    if len(set(selected)) != len(selected):
        raise ValidationError("Change Set 仓库不能重复")
    missing = [repository for repository in selected if repository not in line.repositories]
    if missing:
        raise ValidationError(f"Change Set 仓库不属于开发线 {line.id}：{', '.join(missing)}")
    heads: dict[str, str] = {}
    for repository in selected:
        target = line_repository_path(config, line, repository)
        if git(target, "rev-parse", "--git-dir").code != 0:
            raise DyroError(f"Change Set 开发线仓库不存在或不是 Git：{target}")
        branch = require_ok(git(target, "branch", "--show-current"), f"读取 {repository} 分支").stdout.strip()
        if branch != line.branch:
            raise DyroError(f"Change Set 要求 {repository} 位于 {line.branch}，当前为 {branch or 'DETACHED'}")
        dirty = require_ok(git(target, "status", "--porcelain=v1", "-uall"), f"读取 {repository} 状态").stdout.strip()
        if dirty:
            raise DyroError(f"Change Set 拒绝记录未提交改动：{target}")
        heads[repository] = require_ok(git(target, "rev-parse", "HEAD"), f"读取 {repository} HEAD").stdout.strip()
    changeset = ChangeSet(
        id=changeset_id,
        line=line.id,
        branch=line.branch,
        repositories=selected,
        heads=heads,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    if not dry_run:
        _write(config, changeset)
    return changeset


def verify_changeset(config: Config, changeset: ChangeSet) -> list[str]:
    findings: list[str] = []
    line = get_line(config, changeset.line)
    if line.branch != changeset.branch:
        findings.append(f"FAIL changeset {changeset.id}: registered line branch changed from {changeset.branch} to {line.branch}")
        return findings
    for repository in changeset.repositories:
        target = line_repository_path(config, line, repository)
        if git(target, "rev-parse", "--git-dir").code != 0:
            findings.append(f"FAIL {repository}: missing delivery-line repository")
            continue
        branch = git(target, "branch", "--show-current")
        if branch.code != 0 or branch.stdout.strip() != changeset.branch:
            findings.append(f"FAIL {repository}: expected branch {changeset.branch}, found {branch.stdout.strip() or 'DETACHED'}")
            continue
        dirty = git(target, "status", "--porcelain=v1", "-uall")
        if dirty.code != 0 or dirty.stdout.strip():
            findings.append(f"FAIL {repository}: delivery-line repository is dirty")
            continue
        head = git(target, "rev-parse", "HEAD")
        if head.code != 0 or head.stdout.strip() != changeset.heads[repository]:
            findings.append(f"FAIL {repository}: HEAD differs from pinned Change Set")
            continue
        findings.append(f"PASS {repository}: {changeset.heads[repository][:12]}")
    return findings

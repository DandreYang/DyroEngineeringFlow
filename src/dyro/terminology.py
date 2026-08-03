from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import stat
from typing import Mapping, Sequence

from .errors import DyroError, ValidationError
from .process import git, require_ok


DENYLIST_ENV = "DYRO_TERMINOLOGY_DENYLIST"
DENYLIST_FILE_ENV = "DYRO_TERMINOLOGY_DENYLIST_FILE"
MAX_POLICY_BYTES = 128 * 1024
MAX_TEXT_FILE_BYTES = 2 * 1024 * 1024
SKIPPED_DIRECTORIES = frozenset({".git", ".venv", "__pycache__"})


@dataclass(frozen=True)
class TerminologyPolicy:
    terms: tuple[str, ...]
    input_hash: str


@dataclass(frozen=True)
class TerminologyScan:
    policy_hash: str
    policy_term_count: int
    scanned_sources: int
    violations: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "policy_hash": self.policy_hash,
            "policy_term_count": self.policy_term_count,
            "scanned_sources": self.scanned_sources,
            "violations": list(self.violations),
        }


def _policy_terms(raw: str) -> tuple[str, ...]:
    terms: list[str] = []
    seen: set[str] = set()
    for line in raw.splitlines():
        term = line.strip()
        if not term or term.startswith("#"):
            continue
        normalized = term.casefold()
        if normalized in seen:
            continue
        if len(term) > 256:
            raise ValidationError("术语策略项不能超过 256 个字符")
        seen.add(normalized)
        terms.append(term)
    if not terms:
        raise ValidationError("术语策略不能为空")
    if len(terms) > 512:
        raise ValidationError("术语策略项不能超过 512 条")
    return tuple(terms)


def _policy_hash(terms: Sequence[str]) -> str:
    canonical = "\n".join(sorted((term.casefold() for term in terms)))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _read_external_policy(path: Path, *, root: Path) -> str:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        pass
    else:
        raise ValidationError("术语策略文件必须位于仓库外")
    try:
        metadata = resolved.lstat()
    except FileNotFoundError as exc:
        raise ValidationError("术语策略文件不存在") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise ValidationError("术语策略文件必须是普通文件")
    if metadata.st_size > MAX_POLICY_BYTES:
        raise ValidationError("术语策略文件过大")
    try:
        return resolved.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError("术语策略文件必须是 UTF-8") from exc


def load_terminology_policy(
    root: Path,
    *,
    policy_file: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> TerminologyPolicy:
    """Load a deny list only from an external path or process environment."""
    root = root.expanduser().resolve()
    environment = os.environ if environ is None else environ
    inline = environment.get(DENYLIST_ENV, "").strip()
    configured_path = environment.get(DENYLIST_FILE_ENV, "").strip()
    if policy_file is not None and (inline or configured_path):
        raise ValidationError("术语策略只能指定一个外部输入")
    if inline and configured_path:
        raise ValidationError("术语策略环境输入不能同时使用文本和文件")
    if policy_file is not None:
        raw = _read_external_policy(policy_file, root=root)
    elif configured_path:
        raw = _read_external_policy(Path(configured_path), root=root)
    elif inline:
        raw = inline
    else:
        raise ValidationError("术语策略未配置；请从外部环境提供策略输入")
    terms = _policy_terms(raw)
    return TerminologyPolicy(terms=terms, input_hash=_policy_hash(terms))


def _git_output(root: Path, *arguments: str, label: str) -> str:
    return require_ok(git(root, *arguments), label).stdout


def _added_lines(diff: str) -> str:
    return "\n".join(
        line[1:]
        for line in diff.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )


def _workspace_texts(root: Path) -> tuple[tuple[str, str], ...]:
    sources: list[tuple[str, str]] = []
    for current, directories, files in os.walk(root, followlinks=False):
        directories[:] = sorted(
            directory
            for directory in directories
            if directory not in SKIPPED_DIRECTORIES
        )
        for filename in sorted(files):
            if filename == ".git" or filename.endswith(".pyc"):
                continue
            path = Path(current) / filename
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_TEXT_FILE_BYTES:
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            except FileNotFoundError:
                continue
            relative_path = str(path.relative_to(root))
            source_id = hashlib.sha256(relative_path.encode("utf-8")).hexdigest()[:16]
            sources.append((f"workspace-file:{source_id}", content))
    return tuple(sources)


def _violations(policy: TerminologyPolicy, sources: Sequence[tuple[str, str]]) -> tuple[str, ...]:
    violations: list[str] = []
    normalized_terms = tuple(term.casefold() for term in policy.terms)
    for label, content in sources:
        folded = content.casefold()
        matches = sum(folded.count(term) for term in normalized_terms)
        if matches:
            violations.append(f"{label} ({matches})")
    return tuple(violations)


def _base_commit(root: Path, base_ref: str) -> str:
    normalized = base_ref.strip()
    if not normalized or normalized.startswith("-"):
        raise ValidationError("术语扫描基线必须是一个 commit 引用")
    result = git(
        root,
        "rev-parse",
        "--verify",
        "--end-of-options",
        f"{normalized}^{{commit}}",
    )
    if result.code != 0:
        raise ValidationError("术语扫描基线必须解析为一个 commit")
    commit = result.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", commit):
        raise ValidationError("术语扫描基线解析结果无效")
    return commit


def scan_terminology(
    root: Path,
    policy: TerminologyPolicy,
    *,
    base_ref: str,
    candidate_messages: Sequence[str] = (),
) -> TerminologyScan:
    """Scan workspace text plus branch, diff, and commit-message candidates."""
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise DyroError(f"术语扫描根目录不存在：{root}")
    base_commit = _base_commit(root, base_ref)
    branch = _git_output(root, "branch", "--show-current", label="读取当前分支").strip()
    committed_diff = _git_output(
        root,
        "diff",
        "--no-ext-diff",
        "--unified=0",
        f"{base_commit}...HEAD",
        label="读取候选提交 diff",
    )
    working_diff = _git_output(
        root,
        "diff",
        "--no-ext-diff",
        "--unified=0",
        label="读取工作区 diff",
    )
    staged_diff = _git_output(
        root,
        "diff",
        "--cached",
        "--no-ext-diff",
        "--unified=0",
        label="读取暂存 diff",
    )
    commit_messages = _git_output(
        root,
        "log",
        "--format=%B",
        f"{base_commit}..HEAD",
        label="读取候选提交说明",
    )
    sources = list(_workspace_texts(root))
    sources.extend(
        (
            ("branch", branch),
            ("diff:committed", _added_lines(committed_diff)),
            ("diff:working", _added_lines(working_diff)),
            ("diff:staged", _added_lines(staged_diff)),
            ("commits", commit_messages),
        )
    )
    sources.extend(
        (f"candidate-message:{index}", message)
        for index, message in enumerate(candidate_messages, start=1)
    )
    frozen_sources = tuple(sources)
    return TerminologyScan(
        policy_hash=policy.input_hash,
        policy_term_count=len(policy.terms),
        scanned_sources=len(frozen_sources),
        violations=_violations(policy, frozen_sources),
    )

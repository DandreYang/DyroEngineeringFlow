"""Path and content secret guard before context injection (ADR-0002)."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
from typing import Iterable, Mapping

from .errors import DispatchValidationError


CREDENTIAL_BASENAMES = frozenset(
    {
        ".env",
        ".npmrc",
        ".netrc",
        ".pgpass",
        "credentials",
        "credentials.json",
        "service-account.json",
        "id_rsa",
        "id_ed25519",
        "id_ecdsa",
    }
)
CREDENTIAL_PREFIXES = (".env.", "id_rsa.", "id_ed25519.")
CREDENTIAL_EXTENSIONS = frozenset({".pem", ".key", ".p12", ".pfx", ".keystore"})
CREDENTIAL_DIRS = frozenset({".ssh", ".aws", ".gnupg", ".kube"})

CONTENT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{36,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)


@dataclass(frozen=True)
class GuardVerdict:
    allowed: bool
    reason: str = ""

    def to_mapping(self) -> dict[str, object]:
        return {"allowed": self.allowed, "reason": self.reason}


def _resolve_existing(path: Path) -> Path:
    if path.exists():
        return path.resolve()
    return path.expanduser().absolute()


def check_path(abs_file: Path, root_dir: Path) -> GuardVerdict:
    root = root_dir.resolve()
    candidate = _resolve_existing(Path(abs_file))
    try:
        candidate.relative_to(root)
    except ValueError:
        return GuardVerdict(False, f"path escapes workspace: {abs_file}")

    base = candidate.name
    if base in CREDENTIAL_BASENAMES:
        return GuardVerdict(False, f"credential basename: {base}")
    if any(base.startswith(prefix) for prefix in CREDENTIAL_PREFIXES):
        return GuardVerdict(False, f"credential prefix: {base}")
    if candidate.suffix in CREDENTIAL_EXTENSIONS:
        return GuardVerdict(False, f"credential extension: {candidate.suffix}")
    for segment in candidate.parts:
        if segment in CREDENTIAL_DIRS:
            return GuardVerdict(False, f"sensitive directory segment: {segment}")
    return GuardVerdict(True)


def check_content(content: str, *, file_label: str = "") -> GuardVerdict:
    for pattern in CONTENT_PATTERNS:
        if pattern.search(content):
            label = file_label or "<buffer>"
            return GuardVerdict(
                False,
                f"secret-like content matched /{pattern.pattern[:40]}/ in {label}",
            )
    return GuardVerdict(True)


def guard_file(path: Path, root_dir: Path, *, read_content: bool = True) -> GuardVerdict:
    path = Path(path)
    path_verdict = check_path(path, root_dir)
    if not path_verdict.allowed:
        return path_verdict
    if not read_content:
        return path_verdict
    if not path.is_file():
        return GuardVerdict(False, f"not a regular file: {path}")
    try:
        with path.open("rb") as handle:
            raw = handle.read(512 * 1024)
    except OSError as exc:
        return GuardVerdict(False, f"unreadable: {path}: {exc}")
    if b"\0" in raw:
        return GuardVerdict(False, f"binary file rejected for context injection: {path}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return GuardVerdict(False, f"non-utf8 file rejected: {path}")
    return check_content(text, file_label=str(path))


def assert_files_allowed(
    paths: Iterable[Path], root_dir: Path, *, read_content: bool = True
) -> list[dict[str, object]]:
    """Return per-file verdicts; raise if any file is denied."""
    reports: list[dict[str, object]] = []
    denied: list[str] = []
    for path in paths:
        verdict = guard_file(path, root_dir, read_content=read_content)
        entry = {"path": str(path), **verdict.to_mapping()}
        reports.append(entry)
        if not verdict.allowed:
            denied.append(f"{path}: {verdict.reason}")
    if denied:
        raise DispatchValidationError(
            "context guard denied file(s):\n" + "\n".join(denied)
        )
    return reports


def materialize_strict_shadow(
    *,
    shadow_root: Path,
    root_dir: Path,
    relative_files: Mapping[str, str] | None = None,
    file_paths: Iterable[Path] | None = None,
) -> Path:
    """
    Materialize guarded file contents under shadow_root preserving relative paths.

    Provide either relative_files (rel_posix -> content) or file_paths under root_dir.
    """
    shadow_root = Path(shadow_root)
    if shadow_root.exists():
        raise DispatchValidationError("shadow_root already exists")
    shadow_root.mkdir(parents=True)
    shadow_resolved = shadow_root.resolve()

    pairs: list[tuple[str, str]] = []
    if relative_files is not None:
        for rel, content in relative_files.items():
            if ".." in Path(rel).parts or Path(rel).is_absolute():
                raise DispatchValidationError(f"invalid relative path: {rel}")
            content_verdict = check_content(content, file_label=rel)
            if not content_verdict.allowed:
                raise DispatchValidationError(content_verdict.reason)
            pairs.append((rel, content))
    elif file_paths is not None:
        root = Path(root_dir).resolve()
        for path in file_paths:
            path = Path(path)
            verdict = guard_file(path, root)
            if not verdict.allowed:
                raise DispatchValidationError(verdict.reason)
            rel = path.resolve().relative_to(root).as_posix()
            pairs.append((rel, path.read_text(encoding="utf-8")))
    else:
        raise DispatchValidationError("materialize_strict_shadow requires inputs")

    for rel, content in pairs:
        dest = (shadow_root / rel).resolve()
        try:
            dest.relative_to(shadow_resolved)
        except ValueError as exc:
            raise DispatchValidationError(f"shadow path escapes root: {rel}") from exc
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        os.chmod(dest, 0o600)
    return shadow_root

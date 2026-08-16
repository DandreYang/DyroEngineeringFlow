"""Proof Bundle export and integrity verify. Not identity, not workspace decay."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path, PurePosixPath
import hashlib
import json
import re
import zipfile

from ..errors import DyroError, ValidationError
from ..process import run
from .models import Proof, ProofKind, ProofStatus, ProofSubstrate
from .project import proof_payload


BUNDLE_KIND = "dyro.proof.bundle"
BUNDLE_SCHEMA_VERSION = 1
INTEGRITY_MODE = "integrity"
EVIDENCE_MARKERS = frozenset({"receipt.md", "provenance.json", "gates.json", "task-heads.json"})
_SHA_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_HEX_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_MEMBER_BYTES = 8 * 1024 * 1024
MAX_PROOF_IDS = 256

MISSING_GIT = "missing_git_objects"
MISSING_PROCEDURE = "missing_procedure"
MISSING_SUBSTRATE = "missing_substrate"
MISSING_DECLARED_KEYS = "missing_declared_keys"
NOT_PROOF_BUNDLE = "not_proof_bundle"
OBJECT_UNRESOLVED = "object_unresolved"
BUNDLE_BYTES_MISMATCH = "bundle_bytes_mismatch"
CURRENT_HEADS = "current_heads"
STILL_BOUND = "still_bound"
INVALID_POLICY = "invalid_policy"

_SIGNED_POLICY_BY_KIND = {
    ProofKind.REVIEW_VERDICT: "require_signed_review",
    ProofKind.SIGNOFF: "require_signed_signoff",
}


def export_bundle(proofs: tuple[Proof, ...], destination: Path) -> Path:
    """Write a schema_version=1 ZIP. No git object database, paths, or credentials."""
    if destination.suffix != ".zip":
        raise DyroError("proof export --bundle 必须是 .zip 路径")
    destination.parent.mkdir(parents=True, exist_ok=True)
    files: dict[str, str] = {}
    digest: dict[str, str] = {}
    for proof in proofs:
        body = json.dumps(_portable_payload(proof), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        name = f"proofs/{proof.id}.json"
        files[name] = body
        digest[proof.id] = hashlib.sha256(body.encode("utf-8")).hexdigest()
    manifest = {
        "kind": BUNDLE_KIND,
        "proof_ids": [proof.id for proof in proofs],
        "proof_sha256": digest,
        "schema_version": BUNDLE_SCHEMA_VERSION,
    }
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "manifest.json",
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        )
        for name, body in files.items():
            archive.writestr(name, body)
    return destination


def verify_bundle(
    bundle: Path,
    *,
    git_dirs: tuple[Path, ...] = (),
    current_heads: dict[str, str] | None = None,
) -> tuple[Proof, ...]:
    """Integrity of a portable bundle plus caller git objects.

    Without ``current_heads`` the result is only ``live`` or ``inconclusive``.
    That conclusion is not workspace ``proof verify`` and not merge.
    Empty ``git_dirs`` or a proof without a resolvable pin cannot be ``live``.
    """
    try:
        archive = zipfile.ZipFile(bundle)
    except (OSError, zipfile.BadZipFile) as exc:
        raise DyroError(f"无法打开 Proof Bundle：{bundle}") from exc
    with archive:
        names = set(archive.namelist())
        if _looks_like_evidence_zip(names):
            return (_bundle_inconclusive(NOT_PROOF_BUNDLE, "不是 schema_version=1 的 Proof Bundle"),)
        if "manifest.json" not in names:
            return (_bundle_inconclusive(NOT_PROOF_BUNDLE, "不是 schema_version=1 的 Proof Bundle"),)
        try:
            manifest = json.loads(_read_member(archive, "manifest.json"))
        except (UnicodeDecodeError, json.JSONDecodeError, DyroError):
            return (_bundle_inconclusive(NOT_PROOF_BUNDLE, "manifest.json 无法解析"),)
        if not _valid_manifest(manifest):
            return (_bundle_inconclusive(NOT_PROOF_BUNDLE, "Proof Bundle schema_version 必须为 1"),)
        if any(_git_layout_member(name) for name in names):
            return (_bundle_inconclusive(NOT_PROOF_BUNDLE, "捆内不得包含 git 对象库"),)
        resolved = tuple(_resolve_git_dir(path) for path in git_dirs)
        proofs: list[Proof] = []
        digests = manifest.get("proof_sha256")
        for proof_id in manifest["proof_ids"]:
            member = f"proofs/{proof_id}.json"
            try:
                raw = _read_member(archive, member)
            except DyroError:
                proofs.append(_bundle_inconclusive(BUNDLE_BYTES_MISMATCH, f"缺少 {member}"))
                continue
            if not isinstance(digests, dict) or proof_id not in digests:
                proofs.append(_bundle_inconclusive(BUNDLE_BYTES_MISMATCH, f"缺少 {proof_id} 哈希"))
                continue
            expected = digests[proof_id]
            if not isinstance(expected, str) or not expected:
                proofs.append(_bundle_inconclusive(BUNDLE_BYTES_MISMATCH, f"缺少 {proof_id} 哈希"))
                continue
            if hashlib.sha256(raw.encode("utf-8")).hexdigest() != expected:
                proofs.append(_bundle_inconclusive(BUNDLE_BYTES_MISMATCH, f"{proof_id} 字节哈希漂移"))
                continue
            try:
                payload = json.loads(raw)
                proof = proof_from_payload(payload)
            except (json.JSONDecodeError, KeyError, TypeError, ValueError, ValidationError):
                proofs.append(_bundle_inconclusive(NOT_PROOF_BUNDLE, f"{proof_id} 无法重建"))
                continue
            proofs.append(
                _integrity_of(
                    proof,
                    git_dirs=resolved,
                    current_heads=current_heads,
                )
            )
        if not proofs:
            return (_bundle_inconclusive(MISSING_SUBSTRATE, "Proof Bundle 为空"),)
        return tuple(proofs)


def proof_from_payload(raw: object) -> Proof:
    if not isinstance(raw, dict):
        raise ValidationError("Proof 载荷必须是对象")
    substrate_raw = raw.get("substrate")
    if not isinstance(substrate_raw, dict):
        raise ValidationError("Proof substrate 必须是对象")
    repo_heads = substrate_raw.get("repo_heads") or {}
    extra = substrate_raw.get("extra") or {}
    if not isinstance(repo_heads, dict) or not isinstance(extra, dict):
        raise ValidationError("Proof substrate.repo_heads 与 extra 必须是对象")
    policy = raw.get("policy_require_signed") or {}
    if not isinstance(policy, dict):
        raise ValidationError("policy_require_signed 必须是对象")
    keys = raw.get("declared_key_ids") or ()
    return Proof(
        id=str(raw["id"]),
        kind=ProofKind(str(raw["kind"])),
        subject=str(raw["subject"]),
        substrate=ProofSubstrate(
            repo_heads=tuple((str(key), str(value)) for key, value in repo_heads.items()),
            plan_sha256=str(substrate_raw.get("plan_sha256") or ""),
            attempt_id=str(substrate_raw.get("attempt_id") or ""),
            contract_hash=str(substrate_raw.get("contract_hash") or ""),
            extra=tuple((str(key), str(value)) for key, value in extra.items()),
        ),
        procedure=str(raw.get("procedure") or ""),
        bytes_sha256=str(raw.get("bytes_sha256") or ""),
        generation=str(raw.get("generation") or ""),
        status=ProofStatus.INCONCLUSIVE,
        declared_key_ids=tuple(str(item) for item in keys),
        policy_require_signed=tuple((str(key), _policy_flag(value)) for key, value in policy.items()),
    )


def load_current_heads(path: Path) -> dict[str, str]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DyroError(f"无法读取 --current-heads：{path}") from exc
    if not isinstance(raw, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in raw.items()):
        raise ValidationError("--current-heads 必须是仓库 ID 到 git SHA 的 JSON 对象")
    heads = {key: value.strip() for key, value in raw.items()}
    for sha in heads.values():
        if sha and not _SHA_RE.fullmatch(sha):
            raise ValidationError("--current-heads 的 SHA 必须是 40 或 64 位小写十六进制")
    return heads


def _portable_payload(proof: Proof) -> dict[str, object]:
    payload = proof_payload(proof)
    payload["status"] = ProofStatus.INCONCLUSIVE.value
    payload["decay_reason"] = ""
    payload["observed_at"] = ""
    return payload


def _policy_flag(value: object) -> str:
    if value is True or value == "true":
        return "true"
    if value is False or value == "false":
        return "false"
    raise ValidationError("policy_require_signed 只能是 true 或 false")


def _integrity_of(
    proof: Proof,
    *,
    git_dirs: tuple[Path, ...],
    current_heads: dict[str, str] | None,
) -> Proof:
    if not proof.procedure.strip():
        return replace(proof, status=ProofStatus.INCONCLUSIVE, decay_reason=MISSING_PROCEDURE)
    if not _has_substrate(proof):
        return replace(proof, status=ProofStatus.INCONCLUSIVE, decay_reason=MISSING_SUBSTRATE)
    if _requires_declared_keys(proof) and not proof.declared_key_ids:
        return replace(proof, status=ProofStatus.INCONCLUSIVE, decay_reason=MISSING_DECLARED_KEYS)
    shas = [sha for _repo, sha in proof.substrate.repo_heads if sha]
    if not git_dirs:
        return replace(proof, status=ProofStatus.INCONCLUSIVE, decay_reason=MISSING_GIT)
    if not shas:
        return replace(proof, status=ProofStatus.INCONCLUSIVE, decay_reason=MISSING_GIT)
    for sha in shas:
        if not _SHA_RE.fullmatch(sha) or not _object_exists(git_dirs, sha):
            return replace(proof, status=ProofStatus.INCONCLUSIVE, decay_reason=OBJECT_UNRESOLVED)
    if current_heads is None:
        return replace(proof, status=ProofStatus.LIVE, decay_reason=STILL_BOUND)
    return _decay_against_current_heads(proof, git_dirs=git_dirs, current_heads=current_heads)


def _decay_against_current_heads(
    proof: Proof,
    *,
    git_dirs: tuple[Path, ...],
    current_heads: dict[str, str],
) -> Proof:
    if not proof.substrate.repo_heads:
        return replace(proof, status=ProofStatus.INCONCLUSIVE, decay_reason=MISSING_GIT)
    for repo, pinned in proof.substrate.repo_heads:
        current = current_heads.get(repo, "").strip()
        if not current:
            return replace(proof, status=ProofStatus.INCONCLUSIVE, decay_reason=CURRENT_HEADS)
        if not _SHA_RE.fullmatch(current) or not _object_exists(git_dirs, current):
            return replace(proof, status=ProofStatus.INCONCLUSIVE, decay_reason=OBJECT_UNRESOLVED)
        if proof.kind is ProofKind.INTEGRATION_HEADS:
            if not _is_ancestor(git_dirs, pinned, current):
                return replace(proof, status=ProofStatus.DECAYED, decay_reason=CURRENT_HEADS)
        elif pinned != current:
            return replace(proof, status=ProofStatus.DECAYED, decay_reason=CURRENT_HEADS)
    return replace(proof, status=ProofStatus.LIVE, decay_reason=STILL_BOUND)


def _has_substrate(proof: Proof) -> bool:
    if proof.substrate.repo_heads:
        return True
    if proof.bytes_sha256:
        return True
    if proof.substrate.plan_sha256 or proof.substrate.attempt_id or proof.substrate.contract_hash:
        return True
    return False


def _requires_declared_keys(proof: Proof) -> bool:
    wanted = _SIGNED_POLICY_BY_KIND.get(proof.kind)
    if wanted is None:
        return False
    return any(key == wanted and value == "true" for key, value in proof.policy_require_signed)


def _valid_manifest(raw: object) -> bool:
    if not isinstance(raw, dict):
        return False
    if raw.get("kind") != BUNDLE_KIND or raw.get("schema_version") != BUNDLE_SCHEMA_VERSION:
        return False
    ids = raw.get("proof_ids")
    digests = raw.get("proof_sha256")
    if not isinstance(ids, list) or not ids or len(ids) > MAX_PROOF_IDS:
        return False
    if len(set(ids)) != len(ids):
        return False
    if not all(isinstance(item, str) and item for item in ids):
        return False
    if not isinstance(digests, dict):
        return False
    for proof_id in ids:
        digest = digests.get(proof_id)
        if not isinstance(digest, str) or not _HEX_DIGEST_RE.fullmatch(digest):
            return False
    return True


def _looks_like_evidence_zip(names: set[str]) -> bool:
    top = {PurePosixPath(name).parts[0] for name in names if name and not name.endswith("/")}
    return bool(top & EVIDENCE_MARKERS)


def _git_layout_member(name: str) -> bool:
    parts = PurePosixPath(name).parts
    return "objects" in parts or ".git" in parts or name.startswith("pack/")


def _read_member(archive: zipfile.ZipFile, name: str) -> str:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or not name:
        raise DyroError("Proof Bundle 包含不安全路径")
    try:
        info = archive.getinfo(name)
    except KeyError as exc:
        raise DyroError(f"Proof Bundle 缺少 {name}") from exc
    if info.is_dir():
        raise DyroError(f"Proof Bundle 成员必须是文件：{name}")
    if info.file_size > MAX_MEMBER_BYTES:
        raise DyroError(f"Proof Bundle 成员过大：{name}")
    return archive.read(name).decode("utf-8")


def _resolve_git_dir(path: Path) -> Path:
    candidate = path.expanduser()
    if (candidate / "objects").is_dir() and (candidate / "HEAD").exists():
        return candidate
    result = run(("git", "-C", str(candidate), "rev-parse", "--absolute-git-dir"), timeout=30)
    if result.code != 0 or not result.stdout.strip():
        raise DyroError(f"verify-bundle 的 --git-dir 必须指向调用方 git 对象库：{path}")
    return Path(result.stdout.strip())


def _object_exists(git_dirs: tuple[Path, ...], sha: str) -> bool:
    return any(_git_dir_ok(git_dir, "cat-file", "-e", sha) for git_dir in git_dirs)


def _is_ancestor(git_dirs: tuple[Path, ...], pinned: str, current: str) -> bool:
    return any(_git_dir_ok(git_dir, "merge-base", "--is-ancestor", pinned, current) for git_dir in git_dirs)


def _git_dir_ok(git_dir: Path, *args: str) -> bool:
    result = run(("git", f"--git-dir={git_dir}", *args), timeout=30)
    return result.code == 0


def _bundle_inconclusive(reason: str, subject: str) -> Proof:
    return Proof(
        id=hashlib.sha256(f"{reason}:{subject}".encode("utf-8")).hexdigest(),
        kind=ProofKind.BUNDLE_FAILURE,
        subject=subject,
        substrate=ProofSubstrate(),
        procedure="",
        bytes_sha256="",
        generation="",
        status=ProofStatus.INCONCLUSIVE,
        decay_reason=reason,
    )

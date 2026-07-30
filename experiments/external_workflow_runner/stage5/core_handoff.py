"""Fail-closed Stage5-to-Dyro-Core execution-evidence handoff.

This adapter runs on the trusted runner side after Stage5 cleanup.  It verifies
the sealed Stage5 pack, binds it to an exported current Dyro claim, then calls
the existing Core evidence builder.  It never imports evidence and never owns
review, signoff, merge, or push.
"""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import stat
import tempfile
import time
from typing import Mapping

from ..artifacts import ArtifactPolicy, validate_artifacts
from ..errors import Stage0ValidationError
from ..stage1.claim import ClaimRecord
from .evidence_dry_run import dry_run_validate_pack


MAX_CORE_CLAIM_BYTES = 64 * 1024


def _read_core_claim(path: Path) -> dict[str, object]:
    path = Path(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise Stage0ValidationError("Core claim is unreadable") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size < 2
            or before.st_size > MAX_CORE_CLAIM_BYTES
        ):
            raise Stage0ValidationError(
                "Core claim must be a bounded regular file"
            )
        if os.name != "nt" and stat.S_IMODE(before.st_mode) & 0o077:
            raise Stage0ValidationError(
                "Core claim permissions are too broad; require 0600"
            )
        chunks: list[bytes] = []
        remaining = MAX_CORE_CLAIM_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(16 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
        ):
            raise Stage0ValidationError(
                "Core claim changed while it was read"
            )
        try:
            payload = json.loads(b"".join(chunks).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise Stage0ValidationError(
                "Core claim is not valid UTF-8 JSON"
            ) from exc
    finally:
        os.close(descriptor)
    if not isinstance(payload, dict):
        raise Stage0ValidationError("Core claim must be a JSON object")
    return payload


def _parse_core_expiry(value: object) -> float:
    if not isinstance(value, str):
        raise Stage0ValidationError(
            "Core claim lease_expires_at must be an ISO-8601 timestamp"
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise Stage0ValidationError(
            "Core claim lease_expires_at is invalid"
        ) from exc
    if parsed.tzinfo is None:
        raise Stage0ValidationError(
            "Core claim lease_expires_at must include a timezone"
        )
    return parsed.timestamp()


def _validate_signing_key_path(
    path: Path,
    *,
    forbidden_roots: tuple[Path, ...],
) -> Path:
    requested = Path(path).expanduser()
    try:
        mode = requested.lstat().st_mode
    except FileNotFoundError:
        raise Stage0ValidationError(
            f"execution signing key does not exist: {requested}"
        ) from None
    if not stat.S_ISREG(mode):
        raise Stage0ValidationError(
            "execution signing key must be a regular non-symlink file"
        )
    if os.name != "nt" and stat.S_IMODE(mode) & 0o077:
        raise Stage0ValidationError(
            "execution signing key permissions are too broad; require 0600"
        )
    resolved = requested.resolve(strict=True)
    for root in forbidden_roots:
        normalized_root = Path(root).expanduser().resolve()
        try:
            resolved.relative_to(normalized_root)
        except ValueError:
            continue
        raise Stage0ValidationError(
            "execution signing key must be outside the Dyro Profile, "
            "runner workspace, and Stage5 pack"
        )
    return resolved


def _core_claim_binding(
    payload: Mapping[str, object],
    *,
    now: float | None = None,
) -> dict[str, object]:
    current = time.time() if now is None else float(now)
    task_id = payload.get("task_id")
    claim_id = payload.get("claim_id")
    runner = payload.get("runner")
    execution_key_id = payload.get("execution_key_id")
    generation = payload.get("generation")
    if any(
        not isinstance(value, str) or not value or len(value) > 128
        for value in (task_id, claim_id, runner, execution_key_id)
    ):
        raise Stage0ValidationError(
            "Core claim is missing task, owner, claim ID, or execution key ID"
        )
    if type(generation) is not int or generation < 1:
        raise Stage0ValidationError("Core claim generation is invalid")
    authority_expires_at = _parse_core_expiry(
        payload.get("lease_expires_at")
    )
    if authority_expires_at <= current:
        raise Stage0ValidationError("Core claim has expired")
    return {
        "task_id": str(task_id),
        "claim_id": str(claim_id),
        "runner": str(runner),
        "execution_key_id": str(execution_key_id),
        "generation": generation,
        "authority_expires_at": authority_expires_at,
    }


def stage5_claim_from_core(
    core_claim: Path,
    *,
    now: float | None = None,
) -> ClaimRecord:
    """Translate one exported Core claim without expanding its authority."""
    current = time.time() if now is None else float(now)
    binding = _core_claim_binding(_read_core_claim(core_claim), now=current)
    return ClaimRecord(
        task_id=str(binding["task_id"]),
        runner_id=str(binding["runner"]),
        generation=1,
        execution_key_id=str(binding["execution_key_id"]),
        issued_at=current,
        expires_at=float(binding["authority_expires_at"]),
        control_claim_id=str(binding["claim_id"]),
        control_generation=int(binding["generation"]),
        authority_expires_at=float(binding["authority_expires_at"]),
    )


def _write_new_claim(path: Path, record: ClaimRecord) -> None:
    path = Path(path)
    ClaimRecord.from_mapping(record.to_mapping())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            dir=path.parent,
        )
    except OSError as exc:
        raise Stage0ValidationError(
            "Stage5 claim output directory cannot be prepared"
        ) from exc
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        content = (
            json.dumps(
                record.to_mapping(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError as exc:
            raise Stage0ValidationError(
                f"refusing to overwrite Stage5 claim: {path}"
            ) from exc
        try:
            directory_flags = os.O_RDONLY | getattr(
                os,
                "O_DIRECTORY",
                0,
            )
            directory_descriptor = os.open(path.parent, directory_flags)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except OSError:
            pass
    except Stage0ValidationError:
        raise
    except OSError as exc:
        raise Stage0ValidationError(
            "Stage5 claim output cannot be created"
        ) from exc
    finally:
        temporary.unlink(missing_ok=True)


def prepare_stage5_claim(
    *,
    core_claim: Path,
    output: Path,
    dry_run: bool = False,
) -> dict[str, object]:
    record = stage5_claim_from_core(core_claim)
    requested_output = Path(output).expanduser()
    if requested_output.exists() or requested_output.is_symlink():
        raise Stage0ValidationError(
            f"refusing to overwrite Stage5 claim: {requested_output}"
        )
    output = requested_output.parent.resolve() / requested_output.name
    if output.exists() or output.is_symlink():
        raise Stage0ValidationError(
            f"refusing to overwrite Stage5 claim: {output}"
        )
    if not dry_run:
        _write_new_claim(output, record)
    return {
        "schema_version": 1,
        "kind": "external-runtime-claim-preparation",
        "verdict": "DRY_RUN" if dry_run else "PREPARED",
        "task_id": record.task_id,
        "runner_id": record.runner_id,
        "control_claim_id": record.control_claim_id,
        "control_generation": record.control_generation,
        "execution_key_id": record.execution_key_id,
        "authority_expires_at": record.authority_expires_at,
        "output": str(output),
        "written": not dry_run,
        "notes": [
            "Runtime renewals cannot extend beyond the Core claim authority.",
            "This claim grants no review, signoff, merge, or push authority.",
        ],
    }


def _assert_pack_matches_core_claim(
    candidate: Mapping[str, object],
    binding: Mapping[str, object],
) -> None:
    expected = {
        "task_id": binding["task_id"],
        "runner_id": binding["runner"],
        "execution_key_id": binding["execution_key_id"],
        "control_claim_id": binding["claim_id"],
        "control_generation": binding["generation"],
    }
    for field, value in expected.items():
        if candidate.get(field) != value:
            raise Stage0ValidationError(
                f"Stage5 pack does not match Core claim field: {field}"
            )
    authority = candidate.get("authority_expires_at")
    if (
        type(authority) not in (int, float)
        or abs(
            float(authority)
            - float(binding["authority_expires_at"])
        )
        > 0.001
    ):
        raise Stage0ValidationError(
            "Stage5 pack does not match Core claim expiry"
        )


def _receipt_text(
    candidate: Mapping[str, object],
    binding: Mapping[str, object],
) -> str:
    return "\n".join(
        (
            "result: DONE",
            "runtime_kind: dyro-external-semantic-runtime",
            "runtime_stage: 5",
            f"runtime_workflow_run_id: {candidate['workflow_run_id']}",
            f"runtime_pack_sha256: {candidate['pack_sha256']}",
            (
                "runtime_canonical_input_sha256: "
                f"{candidate['canonical_input_sha256']}"
            ),
            f"runtime_control_claim_id: {binding['claim_id']}",
            (
                "runtime_control_generation: "
                f"{binding['generation']}"
            ),
            "",
        )
    )


def _validate_workspace_artifacts(
    *,
    config: object,
    task: object,
    workspace: Path,
    candidate: Mapping[str, object],
) -> None:
    raw_artifacts = candidate.get("artifacts")
    if not isinstance(raw_artifacts, list) or not raw_artifacts:
        raise Stage0ValidationError(
            "verified Stage5 candidate has no workspace artifacts"
        )
    workspace = Path(workspace).expanduser().resolve()
    repositories = getattr(config, "repositories")
    task_repositories = set(getattr(task, "repositories"))
    roots: dict[str, Path] = {}
    allowed: set[tuple[str, str]] = set()
    artifacts: list[dict[str, object]] = []
    max_artifact_bytes = 1
    max_total_bytes = 0
    for item in raw_artifacts:
        if not isinstance(item, Mapping):
            raise Stage0ValidationError(
                "Stage5 candidate artifact is invalid"
            )
        repository = item.get("repository")
        path = item.get("path")
        sha256 = item.get("sha256")
        byte_count = item.get("bytes")
        if (
            not isinstance(repository, str)
            or repository not in task_repositories
            or repository not in repositories
            or not isinstance(path, str)
            or not isinstance(sha256, str)
            or type(byte_count) is not int
            or byte_count < 0
        ):
            raise Stage0ValidationError(
                "Stage5 artifact is not bound to the Dyro task repositories"
            )
        repository_path = (
            workspace / repositories[repository].mount
        )
        if repository_path.is_symlink() or not repository_path.is_dir():
            raise Stage0ValidationError(
                "task repository root must be an existing non-symlink directory"
            )
        repository_root = repository_path.resolve()
        try:
            repository_root.relative_to(workspace)
        except ValueError as exc:
            raise Stage0ValidationError(
                "task repository root escapes the runner workspace"
            ) from exc
        roots[repository] = repository_root
        allowed.add((repository, path))
        artifacts.append(
            {
                "repository": repository,
                "path": path,
                "sha256": sha256,
            }
        )
        max_artifact_bytes = max(max_artifact_bytes, byte_count)
        max_total_bytes += byte_count
    validate_artifacts(
        artifacts,
        ArtifactPolicy(
            repository_roots=roots,
            allowed_paths=allowed,
            max_artifacts=len(artifacts),
            max_artifact_bytes=max_artifact_bytes,
            max_total_bytes=max(1, max_total_bytes),
        ),
    )


def build_core_evidence_handoff(
    *,
    root: Path,
    task_id: str,
    pack_dir: Path,
    workspace: Path,
    core_claim: Path,
    output: Path,
    signing_key: Path,
    key_id: str,
    dry_run: bool = False,
) -> dict[str, object]:
    """Build, but never import, a Core execution-evidence ZIP."""
    from dyro.config import load
    from dyro.evidence import build_execution_bundle
    from dyro.tasks import execution_claim_binding, load_task

    config = load(Path(root))
    if config.policy.execution_mode != "external":
        raise Stage0ValidationError(
            "Core handoff requires execution_mode = external"
        )
    if not config.policy.require_signed_execution:
        raise Stage0ValidationError(
            "Production runtime handoff requires require_signed_execution = true"
        )
    signing_key = _validate_signing_key_path(
        Path(signing_key),
        forbidden_roots=(
            Path(config.root),
            Path(workspace),
            Path(pack_dir),
        ),
    )
    task = load_task(config, task_id)
    core_claim_payload = _read_core_claim(Path(core_claim))
    with tempfile.TemporaryDirectory(
        prefix="dyro-runtime-handoff-"
    ) as temporary:
        temporary_root = Path(temporary)
        claim_snapshot = temporary_root / "core-claim.json"
        claim_snapshot.write_text(
            json.dumps(
                core_claim_payload,
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        claim_snapshot.chmod(0o600)
        verified_binding = execution_claim_binding(
            task,
            claim_file=claim_snapshot,
        )
        binding = _core_claim_binding(core_claim_payload)
        for field in (
            "claim_id",
            "generation",
            "runner",
            "execution_key_id",
        ):
            if binding[field] != verified_binding[field]:
                raise Stage0ValidationError(
                    f"Core claim binding is inconsistent: {field}"
                )
        if binding["task_id"] != task.id:
            raise Stage0ValidationError(
                "Core claim task does not match the requested handoff task"
            )
        if binding["execution_key_id"] != key_id:
            raise Stage0ValidationError(
                "handoff key ID does not match the Core claim"
            )
        verified = dry_run_validate_pack(
            Path(pack_dir),
            output_path=temporary_root / "stage5-verification.json",
        )
        candidate = verified.candidate_record
        _assert_pack_matches_core_claim(candidate, binding)
        _validate_workspace_artifacts(
            config=config,
            task=task,
            workspace=Path(workspace),
            candidate=candidate,
        )
        receipt = temporary_root / "receipt.md"
        receipt.write_text(
            _receipt_text(candidate, binding),
            encoding="utf-8",
        )
        bundle = build_execution_bundle(
            config,
            task,
            workspace=Path(workspace),
            receipt=receipt,
            output=Path(output),
            signing_key=signing_key,
            key_id=key_id,
            claim=claim_snapshot,
            dry_run=dry_run,
        )
    gates_executed = not dry_run
    gates_passed = bundle.gates_passed if gates_executed else None
    ready_for_core_import = gates_passed is True
    if dry_run:
        verdict = "DRY_RUN"
        next_command: str | None = "dyro runtime handoff --help"
        remediation: str | None = (
            "Review the validated plan, then repeat handoff without --dry-run."
        )
    elif gates_passed is False:
        verdict = "BLOCKED"
        next_command = None
        remediation = (
            "Inspect the signed diagnostic bundle gate logs, fix the task "
            "branch, renew the Core claim if needed, and build to a new output."
        )
    else:
        verdict = "BUILT"
        next_command = (
            f"dyro task evidence execution {task.id} "
            f"--bundle {Path(output).expanduser().resolve()}"
        )
        remediation = None
    return {
        "schema_version": 1,
        "kind": "external-runtime-core-evidence-handoff",
        "verdict": verdict,
        "task_id": task.id,
        "workflow_run_id": candidate["workflow_run_id"],
        "runtime_pack_sha256": candidate["pack_sha256"],
        "canonical_input_sha256": candidate[
            "canonical_input_sha256"
        ],
        "control_claim_id": binding["claim_id"],
        "control_generation": binding["generation"],
        "core_bundle": str(Path(output).expanduser().resolve()),
        "core_bundle_written": not dry_run,
        "gates_executed": gates_executed,
        "gates_passed": gates_passed,
        "workspace_heads_verified": gates_executed,
        "signature_created": gates_executed,
        "ready_for_core_import": ready_for_core_import,
        "core_import_attempted": False,
        "review_attempted": False,
        "signoff_attempted": False,
        "merge_attempted": False,
        "push_attempted": False,
        "next_command": next_command,
        "remediation": remediation,
    }

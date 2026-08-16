"""Derive 0.7 Proofs from existing task and Objective files. Never writes sources."""

from __future__ import annotations

from pathlib import Path
import hashlib
import json
import re
from typing import Iterable

from ..canonical import canonical_json_bytes
from ..config import Config, validate_id
from ..errors import DyroError, ValidationError
from ..evidence_store import current_evidence_directory, resolve_evidence_path
from ..process import run
from ..provenance import latest_execution_attempt, review_binding
from ..signing import signature_key_id
from ..tasks import TASK_HEADS_FILE, Task, list_tasks, load_task
from ..workspace import get_line, line_repository_path
from .models import Proof, ProofKind, ProofStatus, ProofSubstrate, proof_identity_sha256

_GATE_LOG_RE = re.compile(r"^gate-(\d+)\.log$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def list_proofs(
    config: Config,
    *,
    task_id: str | None = None,
    objective_id: str | None = None,
    line_id: str | None = None,
    evaluate: bool = True,
) -> tuple[Proof, ...]:
    """Rebuild Proofs from disk. `--task` never includes action_receipt."""
    if task_id and objective_id:
        raise ValidationError("proof list 的 --task 与 --objective 互斥")
    if objective_id:
        derived = derive_objective_proofs(config, objective_id)
    elif task_id:
        derived = derive_task_proofs(config, load_task(config, task_id))
    else:
        tasks = list_tasks(config)
        if line_id:
            tasks = [task for task in tasks if task.line == line_id]
        proofs: list[Proof] = []
        for task in tasks:
            proofs.extend(derive_task_proofs(config, task))
        if not line_id:
            proofs.extend(derive_trigger_proofs(config))
        derived = tuple(_dedupe(proofs))
    if not evaluate:
        return derived
    from .evaluate import evaluate_proofs

    return evaluate_proofs(config, derived)


def derive_task_proofs(config: Config, task: Task) -> tuple[Proof, ...]:
    """Task-scoped 0.7 kinds. Does not scan Objective action-receipts/."""
    proofs: list[Proof] = []
    proofs.extend(_derive_gate_logs(config, task))
    review = _derive_review_verdict(config, task)
    if review is not None:
        proofs.append(review)
    signoff = _derive_signoff(config, task)
    if signoff is not None:
        proofs.append(signoff)
    subjects = [task]
    for dependency in task.depends_on:
        try:
            subjects.append(load_task(config, dependency))
        except (DyroError, ValidationError):
            proofs.append(
                _inconclusive(
                    config,
                    kind=ProofKind.INTEGRATION_HEADS,
                    subject=dependency,
                    generation="",
                    identity={"line": task.line, "missing_dependency": "true"},
                    procedure="git merge-base --is-ancestor",
                    extra=(("integration_state", "inconclusive"),),
                )
            )
    for subject in subjects:
        integration = _derive_integration_heads(config, subject)
        if integration is not None:
            proofs.append(integration)
    return tuple(_dedupe(proofs))


def derive_objective_proofs(config: Config, objective_id: str) -> tuple[Proof, ...]:
    from ..continuation.store import get_objective, list_objective_actions

    record = get_objective(config, objective_id)
    proofs: list[Proof] = []
    for action in list_objective_actions(config, objective_id):
        receipt = action.receipt
        if receipt is None:
            continue
        payload = {
            "action_id": receipt.action_id,
            "authority_sha256": action.intent.authority_sha256,
            "idempotency_key": receipt.idempotency_key,
            "owner_generation": receipt.owner_generation,
            "status": receipt.status.value,
            "summary": receipt.summary,
        }
        generation = str(receipt.owner_generation)
        produced_at = receipt.recorded_at.isoformat().replace("+00:00", "Z")
        proofs.append(
            _build(
                config,
                kind=ProofKind.ACTION_RECEIPT,
                subject=objective_id,
                generation=generation,
                identity={"action_id": receipt.action_id},
                procedure="action journal receipt bytes",
                bytes_sha256=_sha256_json(payload),
                substrate=ProofSubstrate(
                    plan_sha256=action.intent.plan_sha256,
                    attempt_id=receipt.action_id,
                    contract_hash=record.contract_sha256,
                    extra=(
                        ("authority_sha256", action.intent.authority_sha256),
                        ("owner_generation", generation),
                    ),
                ),
                produced_at=produced_at,
            )
        )
    proofs.extend(derive_trigger_proofs(config, objective_id=objective_id))
    return tuple(proofs)


def _derive_gate_logs(config: Config, task: Task) -> tuple[Proof, ...]:
    generation = _task_generation(task)
    argv_hash = _gate_argv_hash(task)
    contract_hash = _task_contract_hash(task)
    attempt_id, plan_sha256 = _attempt_binding(task)
    heads = _recorded_heads(task)
    policy_extra = (("argv_sha256", argv_hash),)

    evidence = _safe_current_evidence(task.directory)
    if evidence is not None:
        gates_json = evidence / "gates.json"
        if gates_json.is_file():
            logs = sorted((evidence / "gates").glob("gate-*.log")) if (evidence / "gates").is_dir() else []
            attested = _attested_bytes([gates_json, *logs])
            produced_at = _record_timestamp(gates_json)
            return (
                _build(
                    config,
                    kind=ProofKind.GATE_LOG,
                    subject=task.id,
                    generation=generation or evidence.name,
                    identity={"mode": "external"},
                    procedure="gates.json + gates/gate-n.log; argv from task.toml",
                    bytes_sha256=_sha256_bytes(attested),
                    substrate=ProofSubstrate(
                        repo_heads=heads,
                        plan_sha256=plan_sha256,
                        attempt_id=attempt_id,
                        contract_hash=contract_hash,
                        extra=policy_extra,
                    ),
                    produced_at=produced_at,
                ),
            )

    local_logs = _local_gate_logs(task.directory)
    if not local_logs:
        return ()
    attested = _attested_bytes(local_logs)
    return (
        _build(
            config,
            kind=ProofKind.GATE_LOG,
            subject=task.id,
            generation=generation,
            identity={"mode": "local"},
            procedure="logs/gate-n.log; argv from task.toml; not ledger",
            bytes_sha256=_sha256_bytes(attested),
            substrate=ProofSubstrate(
                repo_heads=heads,
                plan_sha256=plan_sha256,
                attempt_id=attempt_id,
                contract_hash=contract_hash,
                extra=policy_extra,
            ),
        ),
    )


def _derive_review_verdict(config: Config, task: Task) -> Proof | None:
    review_path = task.directory / "review.md"
    receipt_path = _safe_evidence_path(task.directory, "receipt.md")
    heads_path = _safe_evidence_path(task.directory, TASK_HEADS_FILE)
    if review_path.is_file() or (receipt_path is not None and receipt_path.is_file()) or (
        heads_path is not None and heads_path.is_file()
    ):
        pass
    else:
        return None

    attempt_id, plan_sha256 = _attempt_binding(task)
    generation = attempt_id or _task_generation(task)
    extras: list[tuple[str, str]] = []
    review_bytes = b""
    if not review_path.is_file():
        extras.append(("missing", "review.md"))
    else:
        review_bytes = review_path.read_bytes()
        extras.append(("review_sha256", hashlib.sha256(review_bytes).hexdigest()))
    receipt_hash = _file_digest(receipt_path)
    heads_hash = _file_digest(heads_path)
    if receipt_hash:
        extras.append(("receipt_sha256", receipt_hash))
    else:
        extras.append(("missing", "receipt.md"))
    if heads_hash:
        extras.append(("task_heads_sha256", heads_hash))
    else:
        extras.append(("missing", TASK_HEADS_FILE))
    if attempt_id:
        extras.append(("attempt_id", attempt_id))
    else:
        extras.append(("missing", "attempt_id"))

    produced_at = ""
    key_ids: tuple[str, ...] = ()
    identity_path = task.directory / "review-identity.json"
    if identity_path.is_file():
        produced_at, key_ids = _signed_review_fields(identity_path)

    attested = review_bytes or (receipt_path.read_bytes() if receipt_path and receipt_path.is_file() else b"")
    return _build(
        config,
        kind=ProofKind.REVIEW_VERDICT,
        subject=task.id,
        generation=generation,
        identity={},
        procedure="review.md + receipt + task-heads + attempt/plan binding",
        bytes_sha256=_sha256_bytes(attested),
        substrate=ProofSubstrate(
            repo_heads=_recorded_heads(task),
            plan_sha256=plan_sha256,
            attempt_id=attempt_id,
            contract_hash=_task_contract_hash(task),
            extra=tuple(extras),
        ),
        produced_at=produced_at,
        declared_key_ids=key_ids,
    )


def _derive_signoff(config: Config, task: Task) -> Proof | None:
    path = task.directory / "signoff.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _inconclusive(
            config,
            kind=ProofKind.SIGNOFF,
            subject=task.id,
            generation=_task_generation(task),
            identity={},
            procedure="signoff.json rebind",
            extra=(("unparseable", "signoff.json"),),
        )
    if not isinstance(payload, dict):
        return _inconclusive(
            config,
            kind=ProofKind.SIGNOFF,
            subject=task.id,
            generation=_task_generation(task),
            identity={},
            procedure="signoff.json rebind",
            extra=(("unparseable", "signoff.json"),),
        )
    attempt_id = str(payload.get("attempt_id", "") or "")
    plan_sha256 = str(payload.get("plan_sha256", "") or "")
    produced_at = str(payload.get("signed_at", "") or "")
    key_id = signature_key_id(payload)
    extras = [
        ("review_sha256", str(payload.get("review_sha256", "") or "")),
        ("receipt_sha256", str(payload.get("receipt_sha256", "") or "")),
        ("task_heads_sha256", str(payload.get("task_heads_sha256", "") or "")),
    ]
    return _build(
        config,
        kind=ProofKind.SIGNOFF,
        subject=task.id,
        generation=attempt_id or _task_generation(task),
        identity={},
        procedure="signoff.json rebind",
        bytes_sha256=_sha256_bytes(path.read_bytes()),
        substrate=ProofSubstrate(
            repo_heads=_recorded_heads(task),
            plan_sha256=plan_sha256,
            attempt_id=attempt_id,
            contract_hash=_task_contract_hash(task),
            extra=tuple(extras),
        ),
        produced_at=produced_at,
        declared_key_ids=(key_id,) if key_id else (),
    )


def derive_trigger_proofs(
    config: Config,
    *,
    objective_id: str | None = None,
) -> tuple[Proof, ...]:
    """Read ``objectives/<id>/triggers/<trigger-id>.json``. Uses ``next_probe_at`` only."""
    root = getattr(config, "objectives_dir", None)
    if root is None or not Path(root).is_dir():
        return ()
    proofs: list[Proof] = []
    names: Iterable[str]
    if objective_id:
        try:
            validate_id(objective_id, "Objective ID")
        except ValidationError:
            return ()
        names = (objective_id,)
    else:
        names = tuple(path.name for path in Path(root).iterdir() if path.is_dir())
    for name in names:
        try:
            validate_id(name, "Objective ID")
        except ValidationError:
            continue
        directory = Path(root) / name / "triggers"
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json")):
            proof = _derive_trigger_observation(config, name, path)
            if proof is not None:
                proofs.append(proof)
    return tuple(_dedupe(proofs))


def _derive_trigger_observation(config: Config, objective_id: str, path: Path) -> Proof | None:
    trigger_id = path.stem
    try:
        validate_id(trigger_id, "Trigger ID")
    except ValidationError:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return _inconclusive(
            config,
            kind=ProofKind.TRIGGER_OBSERVATION,
            subject=trigger_id,
            generation="",
            identity={"objective_id": objective_id},
            procedure="objective triggers/<id>.json; next_probe_at",
            extra=(("unparseable", "trigger.json"),),
        )
    if not isinstance(payload, dict):
        return _inconclusive(
            config,
            kind=ProofKind.TRIGGER_OBSERVATION,
            subject=trigger_id,
            generation="",
            identity={"objective_id": objective_id},
            procedure="objective triggers/<id>.json; next_probe_at",
            extra=(("unparseable", "trigger.json"),),
        )
    state = str(payload.get("state", "") or "")
    summary = str(payload.get("summary", "") or "")
    evidence_ref = str(payload.get("evidence_ref", "") or "")
    next_probe_at = str(payload.get("next_probe_at", "") or "")
    produced_at = str(payload.get("observed_at", "") or "")
    extras: list[tuple[str, str]] = [("state", state)]
    if next_probe_at:
        extras.append(("next_probe_at", next_probe_at))
    if evidence_ref and "/" not in evidence_ref and "\\" not in evidence_ref and not evidence_ref.startswith("~"):
        extras.append(("evidence_ref", evidence_ref))
    generation = _sha256_json({"state": state, "summary": summary, "evidence_ref": evidence_ref})
    return _build(
        config,
        kind=ProofKind.TRIGGER_OBSERVATION,
        subject=trigger_id,
        generation=generation,
        identity={"objective_id": objective_id},
        procedure="objective triggers/<id>.json; next_probe_at",
        bytes_sha256=_sha256_bytes(path.read_bytes()),
        substrate=ProofSubstrate(
            contract_hash="",
            extra=tuple(extras),
        ),
        produced_at=produced_at,
    )


def _derive_integration_heads(config: Config, task: Task) -> Proof | None:
    heads_path = _safe_evidence_path(task.directory, TASK_HEADS_FILE)
    if heads_path is None or not heads_path.is_file():
        return None
    heads = _recorded_heads(task)
    generation = hashlib.sha256(heads_path.read_bytes()).hexdigest() if heads else ""
    state = _observe_integration(config, task, heads)
    return _build(
        config,
        kind=ProofKind.INTEGRATION_HEADS,
        subject=task.id,
        generation=generation,
        identity={"line": task.line},
        procedure="git merge-base --is-ancestor",
        bytes_sha256=_sha256_bytes(heads_path.read_bytes()),
        substrate=ProofSubstrate(
            repo_heads=heads,
            plan_sha256="",
            attempt_id="",
            contract_hash=_task_contract_hash(task),
            extra=(("integration_state", state),),
        ),
    )


def _observe_integration(config: Config, task: Task, heads: tuple[tuple[str, str], ...]) -> str:
    if not heads:
        return "inconclusive"
    try:
        line = get_line(config, task.line)
    except (DyroError, ValidationError):
        return "inconclusive"
    for repo_id, task_head in heads:
        destination = line_repository_path(config, line, repo_id)
        result = run(("git", "merge-base", "--is-ancestor", task_head, "HEAD"), cwd=destination)
        if result.code != 0:
            return "pending"
    return "integrated"


def _build(
    config: Config,
    *,
    kind: ProofKind,
    subject: str,
    generation: str,
    identity: dict[str, object],
    procedure: str,
    bytes_sha256: str,
    substrate: ProofSubstrate,
    produced_at: str = "",
    declared_key_ids: tuple[str, ...] = (),
) -> Proof:
    proof_id = proof_identity_sha256(
        kind=kind,
        subject=subject,
        generation=generation,
        identity_payload=identity,
    )
    return Proof(
        id=proof_id,
        kind=kind,
        subject=subject,
        substrate=substrate,
        procedure=procedure,
        bytes_sha256=bytes_sha256,
        generation=generation,
        status=ProofStatus.INCONCLUSIVE,
        produced_at=produced_at,
        declared_key_ids=declared_key_ids,
        policy_require_signed=_policy_snapshot(config, kind),
    )


def _inconclusive(
    config: Config,
    *,
    kind: ProofKind,
    subject: str,
    generation: str,
    identity: dict[str, object],
    procedure: str,
    extra: tuple[tuple[str, str], ...] = (),
) -> Proof:
    return _build(
        config,
        kind=kind,
        subject=subject,
        generation=generation,
        identity=identity,
        procedure=procedure,
        bytes_sha256="",
        substrate=ProofSubstrate(extra=extra),
    )


def _policy_snapshot(config: Config, kind: ProofKind) -> tuple[tuple[str, str], ...]:
    policy = config.policy
    if kind is ProofKind.REVIEW_VERDICT:
        return (
            ("require_signed_review", "true" if getattr(policy, "require_signed_review", False) else "false"),
        )
    if kind is ProofKind.SIGNOFF:
        return (
            ("require_signed_signoff", "true" if getattr(policy, "require_signed_signoff", False) else "false"),
        )
    return ()


def _task_generation(task: Task) -> str:
    evidence = _safe_current_evidence(task.directory)
    if evidence is not None:
        return evidence.name
    attempt_id, _ = _attempt_binding(task)
    if attempt_id:
        return attempt_id
    latest = _safe_latest_attempt(task.directory)
    if latest and isinstance(latest.get("attempt_id"), str):
        return str(latest["attempt_id"])
    return ""


def _attempt_binding(task: Task) -> tuple[str, str]:
    try:
        binding = review_binding(task.directory)
    except (ValidationError, OSError):
        binding = None
    if binding is not None:
        return binding
    latest = _safe_latest_attempt(task.directory)
    if latest is None:
        return "", ""
    attempt_id = str(latest.get("attempt_id", "") or "")
    plan_sha256 = str(latest.get("plan_sha256", "") or "")
    return attempt_id, plan_sha256 if _SHA256_RE.fullmatch(plan_sha256) else ""


def _task_contract_hash(task: Task) -> str:
    latest = _safe_latest_attempt(task.directory)
    if latest is None:
        return ""
    digest = str(latest.get("task_contract_sha256", "") or "")
    return digest if _SHA256_RE.fullmatch(digest) else ""


def _safe_latest_attempt(task_directory: Path) -> dict[str, object] | None:
    try:
        return latest_execution_attempt(task_directory)
    except (ValidationError, OSError):
        return None


def _recorded_heads(task: Task) -> tuple[tuple[str, str], ...]:
    path = _safe_evidence_path(task.directory, TASK_HEADS_FILE)
    if path is None or not path.is_file():
        return ()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ()
    repositories = payload.get("repositories") if isinstance(payload, dict) else None
    if not isinstance(repositories, dict):
        return ()
    heads: list[tuple[str, str]] = []
    for repo_id, head in sorted(repositories.items()):
        if isinstance(repo_id, str) and isinstance(head, str):
            heads.append((repo_id, head.lower()))
    return tuple(heads)


def _gate_argv_hash(task: Task) -> str:
    payload = [{"name": gate.name, "argv": list(gate.argv), "cwd": gate.cwd} for gate in task.gates]
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _local_gate_logs(task_directory: Path) -> list[Path]:
    candidates: list[Path] = []
    for directory in (task_directory / "logs", task_directory):
        if not directory.is_dir():
            continue
        candidates.extend(
            path for path in directory.iterdir() if path.is_file() and _GATE_LOG_RE.fullmatch(path.name)
        )
    return sorted(candidates, key=lambda path: (path.parent.name, path.name))


def _safe_current_evidence(task_directory: Path) -> Path | None:
    try:
        return current_evidence_directory(task_directory)
    except (ValidationError, OSError):
        return None


def _safe_evidence_path(task_directory: Path, relative: str) -> Path | None:
    try:
        return resolve_evidence_path(task_directory, relative)
    except (ValidationError, OSError):
        candidate = task_directory / relative
        return candidate if candidate.is_file() else None


def _file_digest(path: Path | None) -> str:
    if path is None or not path.is_file():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _attested_bytes(paths: Iterable[Path]) -> bytes:
    chunks: list[bytes] = []
    for path in paths:
        chunks.append(path.name.encode("utf-8"))
        chunks.append(b"\0")
        chunks.append(path.read_bytes())
        chunks.append(b"\n")
    return b"".join(chunks)


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _sha256_json(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _record_timestamp(path: Path) -> str:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    for key in ("produced_at", "created_at", "signed_at", "recorded_at"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _signed_review_fields(path: Path) -> tuple[str, tuple[str, ...]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "", ()
    if not isinstance(payload, dict):
        return "", ()
    created_at = str(payload.get("created_at", "") or "")
    key_id = payload.get("key_id")
    keys = (str(key_id),) if isinstance(key_id, str) and key_id else ()
    return created_at, keys


def _dedupe(proofs: Iterable[Proof]) -> list[Proof]:
    seen: set[str] = set()
    unique: list[Proof] = []
    for proof in proofs:
        if proof.id in seen:
            continue
        seen.add(proof.id)
        unique.append(proof)
    return unique

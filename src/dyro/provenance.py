from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .canonical import canonical_json_text
from .errors import ValidationError
from .evidence_store import iter_generation_artifacts, resolve_evidence_path
from .state import atomic_write_text


RUN_STATE_FILE = "execution-run.json"
ATTEMPTS_DIR = "attempts"
ATTEMPT_ID_RE = re.compile(r"(?m)^attempt_id:\s*([A-Za-z0-9._:-]+)\s*$")
PLAN_SHA_RE = re.compile(r"(?mi)^plan_sha256:\s*([0-9a-f]{64})\s*$")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: object) -> str:
    return canonical_json_text(value)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _task_contract_sha256(task_directory: Path) -> str:
    contract_path = task_directory / "task.toml"
    try:
        return _sha256(contract_path.read_bytes())
    except OSError as exc:
        raise ValidationError(f"无法读取任务契约：{contract_path}: {exc}") from exc


@dataclass(frozen=True)
class ExecutionAttempt:
    task_id: str
    run_id: str
    attempt_id: str
    attempt_number: int
    task_contract_sha256: str
    plan_sha256: str
    path: Path
    record: dict[str, object]


def _load_run_state(path: Path, task_id: str) -> tuple[str, int]:
    if not path.exists():
        return uuid.uuid4().hex, 0
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"执行 run 状态损坏：{path}: {exc}") from exc
    if (
        not isinstance(raw, dict)
        or raw.get("schema_version") != 1
        or raw.get("task_id") != task_id
        or not isinstance(raw.get("run_id"), str)
        or not isinstance(raw.get("last_attempt"), int)
    ):
        raise ValidationError(f"执行 run 状态格式无效：{path}")
    return str(raw["run_id"]), int(raw["last_attempt"])


def begin_execution_attempt(
    task_directory: Path,
    task_id: str,
    plan: dict[str, object],
    *,
    parent_attempt_id: str | None = None,
) -> ExecutionAttempt:
    run_state_path = task_directory / RUN_STATE_FILE
    run_id, previous_attempt = _load_run_state(run_state_path, task_id)
    attempt_number = previous_attempt + 1
    attempt_id = f"{task_id}-a{attempt_number:04d}-{uuid.uuid4().hex[:8]}"
    started_at = _utc_now()
    plan_json = _canonical_json(plan)
    task_contract_sha256 = _task_contract_sha256(task_directory)
    plan_sha256 = _sha256(plan_json.encode("utf-8"))
    attempt_path = task_directory / ATTEMPTS_DIR / f"{attempt_id}.json"
    attempt_path.parent.mkdir(parents=True, exist_ok=True)

    run_state = {
        "schema_version": 1,
        "task_id": task_id,
        "run_id": run_id,
        "last_attempt": attempt_number,
        "current_attempt_id": attempt_id,
        "updated_at": started_at,
    }
    record: dict[str, object] = {
        "schema_version": 1,
        "task_id": task_id,
        "run_id": run_id,
        "attempt_id": attempt_id,
        "attempt_number": attempt_number,
        "status": "in_progress",
        "started_at": started_at,
        "task_contract_sha256": task_contract_sha256,
        "plan_sha256": plan_sha256,
        "plan": plan,
    }
    if parent_attempt_id:
        record["parent_attempt_id"] = parent_attempt_id
    atomic_write_text(run_state_path, json.dumps(run_state, ensure_ascii=False, indent=2) + "\n")
    atomic_write_text(attempt_path, json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    return ExecutionAttempt(
        task_id=task_id,
        run_id=run_id,
        attempt_id=attempt_id,
        attempt_number=attempt_number,
        task_contract_sha256=task_contract_sha256,
        plan_sha256=plan_sha256,
        path=attempt_path,
        record=record,
    )


def finish_execution_attempt(
    attempt: ExecutionAttempt,
    *,
    result: str | None = None,
    error: Exception | None = None,
) -> None:
    record = dict(attempt.record)
    record["completed_at"] = _utc_now()
    if error is None:
        record["status"] = "completed"
        record["result"] = result
    else:
        record["status"] = "failed"
        record["error"] = {
            "type": type(error).__name__,
            "message": str(error),
        }
    atomic_write_text(
        attempt.path,
        json.dumps(record, ensure_ascii=False, indent=2) + "\n",
    )
    if error is None and result == "review":
        task_directory = attempt.path.parent.parent
        run_state_path = task_directory / RUN_STATE_FILE
        try:
            run_state = json.loads(run_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationError(f"执行 run 状态损坏：{run_state_path}: {exc}") from exc
        if not isinstance(run_state, dict) or run_state.get("run_id") != attempt.run_id:
            raise ValidationError(f"执行 run 状态与 attempt 不匹配：{run_state_path}")
        run_state["review_attempt_id"] = attempt.attempt_id
        run_state["updated_at"] = record["completed_at"]
        atomic_write_text(
            run_state_path,
            json.dumps(run_state, ensure_ascii=False, indent=2) + "\n",
        )


def external_execution_plan(
    task: object,
    execution_mode: str,
    *,
    claim_binding: dict[str, object] | None = None,
) -> dict[str, object]:
    plan: dict[str, object] = {
        "schema_version": 1,
        "source": "external_evidence_bundle",
        "task": {
            "id": getattr(task, "id"),
            "line": getattr(task, "line"),
            "risk": getattr(task, "risk"),
            "executor": getattr(task, "executor"),
            "reviewer": getattr(task, "reviewer"),
            "repositories": list(getattr(task, "repositories")),
            "depends_on": list(getattr(task, "depends_on")),
            "blocked_on": list(getattr(task, "blocked_on")),
            "conflict_group": getattr(task, "conflict_group"),
            "gates": [
                {
                    "name": gate.name,
                    "argv": list(gate.argv),
                    "cwd": gate.cwd,
                    "timeout_seconds": gate.timeout_seconds,
                }
                for gate in getattr(task, "gates")
            ],
        },
        "execution_mode": execution_mode,
    }
    if claim_binding is not None:
        plan["claim"] = dict(claim_binding)
    return plan


def build_external_attempt_record(
    task_directory: Path,
    task_id: str,
    plan: dict[str, object],
    *,
    result: str,
    receipt_sha256: str,
    gates_sha256: str = "",
    task_heads_sha256: str = "",
) -> dict[str, object]:
    now = _utc_now()
    plan_sha256 = _sha256(_canonical_json(plan).encode("utf-8"))
    run_id = uuid.uuid4().hex
    return {
        "schema_version": 1,
        "task_id": task_id,
        "run_id": run_id,
        "attempt_id": f"{task_id}-external-{uuid.uuid4().hex[:12]}",
        "attempt_number": 1,
        "status": "completed",
        "started_at": now,
        "completed_at": now,
        "result": result,
        "receipt_sha256": receipt_sha256,
        "gates_sha256": gates_sha256,
        "task_heads_sha256": task_heads_sha256,
        "task_contract_sha256": _task_contract_sha256(task_directory),
        "plan_sha256": plan_sha256,
        "plan": plan,
    }


def _validate_external_attempt_record(
    task_directory: Path,
    task_id: str,
    record: object,
    *,
    receipt_sha256: str,
    result: str,
    expected_plan: dict[str, object],
    gates_sha256: str,
    task_heads_sha256: str,
    trusted_keys_dir: Path | None = None,
    require_signature: bool = False,
) -> dict[str, object]:
    if not isinstance(record, dict) or record.get("schema_version") != 1:
        raise ValidationError("外部 provenance 格式无效")
    if trusted_keys_dir is not None:
        from .signing import verify_record

        verify_record(
            record,
            purpose="execution",
            trust_directory=trusted_keys_dir,
            required=require_signature,
        )
    required_strings = (
        "run_id",
        "attempt_id",
        "task_contract_sha256",
        "plan_sha256",
    )
    if record.get("task_id") != task_id or any(
        not isinstance(record.get(field), str) or not record.get(field)
        for field in required_strings
    ):
        raise ValidationError("外部 provenance 身份字段无效")
    attempt_id = str(record["attempt_id"])
    if not re.fullmatch(r"[A-Za-z0-9._:-]+", attempt_id):
        raise ValidationError(f"外部 provenance attempt_id 无效：{attempt_id!r}")
    if record.get("receipt_sha256") != receipt_sha256:
        raise ValidationError("外部 provenance receipt_sha256 不匹配")
    if record.get("gates_sha256", "") != gates_sha256:
        raise ValidationError("外部 provenance gates_sha256 不匹配")
    if record.get("task_heads_sha256", "") != task_heads_sha256:
        raise ValidationError("外部 provenance task_heads_sha256 不匹配")
    if str(record.get("result", "")).upper() != result.upper():
        raise ValidationError("外部 provenance result 不匹配")
    if record.get("status") != "completed":
        raise ValidationError("外部 provenance status 必须为 completed")
    if not isinstance(record.get("attempt_number"), int) or int(record["attempt_number"]) < 1:
        raise ValidationError("外部 provenance attempt_number 无效")
    if record.get("task_contract_sha256") != _task_contract_sha256(task_directory):
        raise ValidationError("外部 provenance task_contract_sha256 不匹配")
    plan = record.get("plan")
    if not isinstance(plan, dict):
        raise ValidationError("外部 provenance 缺少 plan")
    if _canonical_json(plan) != _canonical_json(expected_plan):
        raise ValidationError("外部 provenance plan 与控制面权威计划不匹配")
    expected_plan_sha256 = _sha256(_canonical_json(plan).encode("utf-8"))
    if record.get("plan_sha256") != expected_plan_sha256:
        raise ValidationError("外部 provenance plan_sha256 不匹配")
    return dict(record)


def import_external_execution_attempt(
    task_directory: Path,
    task_id: str,
    *,
    provenance: Path | None,
    receipt_sha256: str,
    result: str,
    expected_plan: dict[str, object],
    gates_sha256: str = "",
    task_heads_sha256: str = "",
    trusted_keys_dir: Path | None = None,
    require_signature: bool = False,
    dry_run: bool = False,
) -> dict[str, object]:
    if provenance is None:
        record = build_external_attempt_record(
            task_directory,
            task_id,
            expected_plan,
            result=result,
            receipt_sha256=receipt_sha256,
            gates_sha256=gates_sha256,
            task_heads_sha256=task_heads_sha256,
        )
        record["legacy_provenance"] = True
        if trusted_keys_dir is not None:
            from .signing import verify_record

            verify_record(
                record,
                purpose="execution",
                trust_directory=trusted_keys_dir,
                required=require_signature,
            )
    else:
        if not provenance.is_file():
            raise ValidationError(f"外部 provenance 文件不存在：{provenance}")
        try:
            raw = json.loads(provenance.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationError(f"外部 provenance 文件损坏：{provenance}: {exc}") from exc
        record = _validate_external_attempt_record(
            task_directory,
            task_id,
            raw,
            receipt_sha256=receipt_sha256,
            result=result,
            expected_plan=expected_plan,
            gates_sha256=gates_sha256,
            task_heads_sha256=task_heads_sha256,
            trusted_keys_dir=trusted_keys_dir,
            require_signature=require_signature,
        )
    if dry_run:
        return record

    return persist_external_execution_attempt(
        task_directory,
        task_id,
        record,
        result=result,
    )


def persist_external_execution_attempt(
    task_directory: Path,
    task_id: str,
    record: dict[str, object],
    *,
    result: str,
    writer: object | None = None,
) -> dict[str, object]:
    """Persist one already validated external attempt, optionally into a staging writer."""
    attempt_id = str(record["attempt_id"])
    attempt_path = task_directory / ATTEMPTS_DIR / f"{attempt_id}.json"
    existing_attempt = next(
        (item for item in list_execution_attempts(task_directory) if item.get("attempt_id") == attempt_id),
        None,
    )
    if existing_attempt is not None:
        if _canonical_json(existing_attempt) != _canonical_json(record):
            raise ValidationError(f"外部 provenance attempt 已存在但内容不同：{attempt_id}")
    else:
        content = (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        if writer is None:
            attempt_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(attempt_path, content.decode("utf-8"))
        else:
            writer(attempt_path, content)  # type: ignore[operator]
    current_run_state_path = resolve_evidence_path(task_directory, RUN_STATE_FILE)
    run_state_path = task_directory / RUN_STATE_FILE
    if current_run_state_path.is_file():
        try:
            previous_run_state = json.loads(current_run_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationError(f"已有 execution run state 损坏：{task_id}: {exc}") from exc
        previous_number = int(previous_run_state.get("last_attempt", 0))
        attempt_number = int(record.get("attempt_number", 1))
        previous_attempt_id = str(previous_run_state.get("current_attempt_id", ""))
        if attempt_number < previous_number:
            raise ValidationError(f"拒绝回放过期 external attempt：{attempt_id}")
        if attempt_number == previous_number and previous_attempt_id not in ("", attempt_id):
            raise ValidationError(f"external attempt 序号冲突：{attempt_id}")
    run_state = {
        "schema_version": 1,
        "task_id": task_id,
        "run_id": record["run_id"],
        "last_attempt": int(record.get("attempt_number", 1)),
        "current_attempt_id": attempt_id,
        "updated_at": record.get("completed_at", _utc_now()),
    }
    if result.upper() == "DONE":
        run_state["review_attempt_id"] = attempt_id
    run_state_content = (json.dumps(run_state, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if writer is None:
        atomic_write_text(run_state_path, run_state_content.decode("utf-8"))
    else:
        writer(run_state_path, run_state_content)  # type: ignore[operator]
    return record


def list_execution_attempts(task_directory: Path) -> list[dict[str, object]]:
    attempts_directory = task_directory / ATTEMPTS_DIR
    attempts: list[dict[str, object]] = []
    paths = list(sorted(attempts_directory.glob("*.json"))) if attempts_directory.exists() else []
    paths.extend(iter_generation_artifacts(task_directory, ATTEMPTS_DIR, suffix=".json"))
    seen: set[str] = set()
    for path in paths:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationError(f"执行 attempt 记录损坏：{path}: {exc}") from exc
        if not isinstance(raw, dict) or raw.get("schema_version") != 1:
            raise ValidationError(f"执行 attempt 记录格式无效：{path}")
        attempt_id = str(raw.get("attempt_id", ""))
        if attempt_id in seen:
            continue
        seen.add(attempt_id)
        attempts.append(raw)
    attempts.sort(
        key=lambda item: (
            str(item.get("started_at", "")),
            int(item.get("attempt_number", 0)),
        )
    )
    return attempts


def latest_execution_attempt(task_directory: Path) -> dict[str, object] | None:
    attempts = list_execution_attempts(task_directory)
    return attempts[-1] if attempts else None


def current_execution_attempt_id(task_directory: Path) -> str | None:
    run_state_path = resolve_evidence_path(task_directory, RUN_STATE_FILE)
    if not run_state_path.is_file():
        return None
    try:
        run_state = json.loads(run_state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"执行 run 状态损坏：{run_state_path}: {exc}") from exc
    if not isinstance(run_state, dict):
        raise ValidationError(f"执行 run 状态格式无效：{run_state_path}")
    attempt_id = run_state.get("current_attempt_id")
    return str(attempt_id) if isinstance(attempt_id, str) and attempt_id else None


def review_binding(task_directory: Path) -> tuple[str, str] | None:
    attempts = list_execution_attempts(task_directory)
    if not attempts:
        return None
    run_state_path = resolve_evidence_path(task_directory, RUN_STATE_FILE)
    review_attempt_id = ""
    if run_state_path.is_file():
        try:
            run_state = json.loads(run_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationError(f"执行 run 状态损坏：{run_state_path}: {exc}") from exc
        if isinstance(run_state, dict) and isinstance(run_state.get("review_attempt_id"), str):
            review_attempt_id = str(run_state["review_attempt_id"])
    attempt = next(
        (candidate for candidate in attempts if candidate.get("attempt_id") == review_attempt_id),
        None,
    )
    if attempt is None:
        eligible = [
            candidate
            for candidate in attempts
            if candidate.get("status") == "completed"
            and str(candidate.get("result", "")).upper() in ("REVIEW", "DONE")
        ]
        attempt = eligible[-1] if eligible else None
    if attempt is None:
        return None
    attempt_id = str(attempt.get("attempt_id", ""))
    plan_sha256 = str(attempt.get("plan_sha256", ""))
    if not attempt_id or not re.fullmatch(r"[0-9a-f]{64}", plan_sha256):
        raise ValidationError("最新 execution attempt 缺少有效 review binding")
    return attempt_id, plan_sha256


def validate_review_binding(
    task_directory: Path,
    review_content: str,
) -> tuple[bool, tuple[str, str] | None, tuple[str, str]]:
    expected = review_binding(task_directory)
    attempt_match = ATTEMPT_ID_RE.search(review_content)
    plan_match = PLAN_SHA_RE.search(review_content)
    reviewed = (
        attempt_match.group(1) if attempt_match else "",
        plan_match.group(1).lower() if plan_match else "",
    )
    if expected is None:
        return True, None, reviewed
    return reviewed == expected, expected, reviewed


def render_review_binding(
    task_id: str,
    binding: tuple[str, str] | None,
) -> str:
    if binding is None:
        raise ValidationError(f"任务 {task_id} 暂无 execution attempt，不能生成 review binding")
    return f"attempt_id: {binding[0]}\nplan_sha256: {binding[1]}\n"


def render_execution_attempts(task_id: str, attempts: list[dict[str, object]]) -> str:
    if not attempts:
        return f"任务 {task_id} 暂无本地执行 attempt\n"
    lines = [
        f"{'ATTEMPT':34} {'STATUS':12} {'RESULT':18} {'CONTRACT':12} {'PLAN':12}",
    ]
    for attempt in attempts:
        lines.append(
            f"{str(attempt.get('attempt_id', '-')):34} "
            f"{str(attempt.get('status', '-')):12} "
            f"{str(attempt.get('result', '-')):18} "
            f"{str(attempt.get('task_contract_sha256', '-'))[:12]:12} "
            f"{str(attempt.get('plan_sha256', '-'))[:12]:12}"
        )
    return "\n".join(lines) + "\n"

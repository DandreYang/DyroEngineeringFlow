"""Strict validation for the Stage 0 result envelope."""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Mapping

from .errors import Stage0ValidationError


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TOP_LEVEL_KEYS = {
    "schema_version",
    "status",
    "workflow_run_id",
    "branches",
    "artifacts",
    "question",
}
_BRANCH_KEYS = {"id", "critical", "status", "error_code"}
_ARTIFACT_KEYS = {"repository", "path", "sha256"}
_RESULTS = {"DONE", "BLOCKED", "QUESTION"}
_BRANCH_RESULTS = {"success", "failed", "question"}


def _require_exact_keys(
    value: Mapping[str, object], expected: set[str], label: str
) -> None:
    actual = set(value)
    if actual != expected:
        raise Stage0ValidationError(
            f"{label} keys mismatch: missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
        )


def validate_result_envelope(
    envelope: Mapping[str, object],
    *,
    workflow_run_id: str,
    expected_branches: Mapping[str, bool],
) -> dict[str, object]:
    """Validate the only machine-readable result allowed to drive a receipt."""
    if not isinstance(envelope, Mapping):
        raise Stage0ValidationError("result envelope must be an object")
    if not isinstance(workflow_run_id, str) or not workflow_run_id:
        raise Stage0ValidationError(
            "expected workflow_run_id must be a non-empty string"
        )
    if not isinstance(expected_branches, Mapping) or not expected_branches:
        raise Stage0ValidationError("expected_branches must be a non-empty object")
    if any(
        not isinstance(branch_id, str)
        or not branch_id
        or len(branch_id) > 128
        or not isinstance(critical, bool)
        for branch_id, critical in expected_branches.items()
    ):
        raise Stage0ValidationError("expected_branches contains an invalid declaration")
    _require_exact_keys(envelope, _TOP_LEVEL_KEYS, "result envelope")
    if (
        type(envelope.get("schema_version")) is not int
        or envelope.get("schema_version") != 1
    ):
        raise Stage0ValidationError("result envelope schema version is unsupported")
    if envelope.get("workflow_run_id") != workflow_run_id:
        raise Stage0ValidationError(
            "workflow_run_id does not match the supervisor value"
        )
    status = envelope.get("status")
    if not isinstance(status, str) or status not in _RESULTS:
        raise Stage0ValidationError("result status is invalid")

    branches = envelope.get("branches")
    if not isinstance(branches, list):
        raise Stage0ValidationError("branches must be an array")
    seen: set[str] = set()
    normalized: list[Mapping[str, object]] = []
    for index, branch in enumerate(branches):
        if not isinstance(branch, Mapping):
            raise Stage0ValidationError(f"branch at index {index} must be an object")
        _require_exact_keys(branch, _BRANCH_KEYS, f"branch at index {index}")
        branch_id = branch.get("id")
        if not isinstance(branch_id, str) or not branch_id:
            raise Stage0ValidationError(f"branch at index {index} has an invalid ID")
        if branch_id in seen:
            raise Stage0ValidationError(f"duplicate branch ID: {branch_id}")
        seen.add(branch_id)
        if branch_id not in expected_branches:
            raise Stage0ValidationError(f"undeclared branch ID: {branch_id}")
        critical = branch.get("critical")
        if (
            not isinstance(critical, bool)
            or critical is not expected_branches[branch_id]
        ):
            raise Stage0ValidationError(f"branch critical flag mismatch: {branch_id}")
        branch_status = branch.get("status")
        if not isinstance(branch_status, str) or branch_status not in _BRANCH_RESULTS:
            raise Stage0ValidationError(f"branch status is invalid: {branch_id}")
        error_code = branch.get("error_code")
        if not isinstance(error_code, str) or len(error_code) > 128:
            raise Stage0ValidationError(f"branch error_code is invalid: {branch_id}")
        if branch_status == "failed" and not error_code:
            raise Stage0ValidationError(
                f"failed branch requires error_code: {branch_id}"
            )
        if branch_status != "failed" and error_code:
            raise Stage0ValidationError(
                f"non-failed branch must not have error_code: {branch_id}"
            )
        normalized.append(branch)

    if seen != set(expected_branches):
        raise Stage0ValidationError(
            f"branch set mismatch: missing={sorted(set(expected_branches) - seen)}"
        )
    if status == "DONE" and any(
        branch["critical"] and branch["status"] != "success" for branch in normalized
    ):
        raise Stage0ValidationError("DONE requires every critical branch to succeed")
    if status == "BLOCKED" and not any(
        branch["status"] == "failed" for branch in normalized
    ):
        raise Stage0ValidationError("BLOCKED requires a failed branch with error_code")
    if status == "QUESTION" and not any(
        branch["status"] == "question" for branch in normalized
    ):
        raise Stage0ValidationError("QUESTION requires a branch in question state")

    question = envelope.get("question")
    if not isinstance(question, str) or len(question) > 4000:
        raise Stage0ValidationError(
            "question must be a string no longer than 4000 characters"
        )
    if status == "QUESTION" and not question.strip():
        raise Stage0ValidationError("QUESTION requires a non-empty question")
    if status != "QUESTION" and question:
        raise Stage0ValidationError("question must be empty unless status is QUESTION")

    artifacts = envelope.get("artifacts")
    if not isinstance(artifacts, list):
        raise Stage0ValidationError("artifacts must be an array")
    artifact_ids: set[tuple[str, str]] = set()
    for index, artifact in enumerate(artifacts):
        if not isinstance(artifact, Mapping):
            raise Stage0ValidationError(f"artifact at index {index} must be an object")
        _require_exact_keys(artifact, _ARTIFACT_KEYS, f"artifact at index {index}")
        repository = artifact.get("repository")
        path = artifact.get("path")
        digest = artifact.get("sha256")
        if not isinstance(repository, str) or not repository:
            raise Stage0ValidationError(
                f"artifact repository is invalid at index {index}"
            )
        if not isinstance(path, str) or not path:
            raise Stage0ValidationError(f"artifact path is invalid at index {index}")
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise Stage0ValidationError(f"artifact SHA-256 is invalid at index {index}")
        artifact_id = (repository, path)
        if artifact_id in artifact_ids:
            raise Stage0ValidationError(f"duplicate artifact: {repository}/{path}")
        artifact_ids.add(artifact_id)

    return deepcopy(dict(envelope))

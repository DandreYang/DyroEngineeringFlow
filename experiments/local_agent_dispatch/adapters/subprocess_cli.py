"""Fail-closed adapters for local Codex and Claude command-line backends."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
from typing import Callable, Mapping, Sequence

from ..bounded_process import BoundedCompletedProcess, run_bounded
from ..context_guard import assert_content_allowed, safe_error_text
from ..errors import DispatchValidationError
from ..process_identity import identity_for_pid
from ..task_contract import TaskContract
from .base import AdapterResult


_COMMON_ENV_ALLOWLIST = frozenset(
    {
        "PATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "TMPDIR",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
    }
)
_BACKEND_ENV_ALLOWLIST = {
    "codex": frozenset(
        {
            "CODEX_HOME",
        }
    ),
    "claude": frozenset(
        {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CONFIG_DIR",
        "XDG_CONFIG_HOME",
        }
    ),
}


def _backend_environment(backend: str) -> dict[str, str]:
    """Pass only backend login/runtime variables, never the full host environment."""
    allowed = _COMMON_ENV_ALLOWLIST | _BACKEND_ENV_ALLOWLIST.get(
        backend,
        frozenset(),
    )
    return {
        name: value
        for name, value in os.environ.items()
        if name in allowed and value
    }


def _build_prompt(contract: TaskContract, context_files: Mapping[str, str]) -> str:
    parts = [
        "# Task (self-contained; no prior conversation)",
        "",
        f"Execution mode: {contract.mode}",
        f"Strict context-only mode: {contract.strict}",
        f"## Briefing\n{contract.task.briefing}",
        f"## Locations\n{contract.task.locations}",
        f"## Objective\n{contract.task.objective}",
        f"## Constraints\n{contract.task.constraints}",
        f"## Output contract\n{contract.task.output_contract}",
        "",
        "Use only the context supplied below unless edit mode explicitly provides an",
        "isolated worktree. Never invoke Git network operations or production actions.",
        "Respond with one JSON object containing summary (string),",
        "confidence (high|medium|low), and evidence",
        "(array of {file, lines?, claim}). Do not include secrets or Markdown fences.",
        "",
        "## Context files",
    ]
    for relative, content in sorted(context_files.items()):
        parts.append(f"\n### {relative}\n```\n{content}\n```")
    return "\n".join(parts)


def _parse_model_json(text: str) -> dict[str, object]:
    raw = text.strip()
    if not raw:
        raise DispatchValidationError("backend returned an empty JSON result")
    candidates = [raw]
    if "```" in raw:
        for chunk in raw.split("```"):
            candidate = chunk.strip()
            if candidate.startswith("json"):
                candidate = candidate[4:].strip()
            if candidate:
                candidates.append(candidate)
    payload: object | None = None
    for candidate in candidates:
        try:
            decoded = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, dict):
            payload = decoded
            break
    if not isinstance(payload, dict):
        raise DispatchValidationError("backend result is not a JSON object")

    summary = payload.get("summary")
    confidence = payload.get("confidence")
    evidence = payload.get("evidence")
    if type(summary) is not str or not summary.strip() or len(summary) > 4000:
        raise DispatchValidationError("backend JSON summary is invalid")
    if confidence not in {"high", "medium", "low"}:
        raise DispatchValidationError("backend JSON confidence is invalid")
    if not isinstance(evidence, list) or len(evidence) > 100:
        raise DispatchValidationError("backend JSON evidence is invalid")
    if any(not isinstance(item, dict) for item in evidence):
        raise DispatchValidationError("backend JSON evidence entries must be objects")
    assert_content_allowed(summary, label="provider.summary")
    for index, item in enumerate(evidence):
        for name in ("file", "claim", "lines"):
            value = item.get(name)
            if isinstance(value, str):
                assert_content_allowed(value, label=f"provider.evidence[{index}].{name}")
    return {
        "summary": summary.strip(),
        "confidence": confidence,
        "evidence": evidence,
    }


def _completed_to_result(
    completed: BoundedCompletedProcess,
    *,
    backend: str,
) -> AdapterResult:
    if completed.timed_out:
        return AdapterResult(
            status="timeout",
            summary="",
            error_code="timeout",
            warnings=["backend process group exceeded its deadline and was terminated"],
        )
    if completed.output_limited:
        return AdapterResult(
            status="error",
            summary="",
            error_code="output_limit",
            warnings=["backend output exceeded the byte limit and was terminated"],
        )
    if completed.returncode != 0:
        return AdapterResult(
            status="error",
            summary="",
            error_code=f"exit_{completed.returncode}",
            warnings=[f"{backend} process exited with code {completed.returncode}"],
        )
    try:
        parsed = _parse_model_json(completed.stdout)
    except DispatchValidationError as exc:
        return AdapterResult(
            status="error",
            summary="",
            error_code="protocol_error",
            warnings=[safe_error_text(exc, fallback="backend result failed validation")],
        )
    return AdapterResult(
        status="ok",
        summary=str(parsed["summary"]),
        evidence=list(parsed["evidence"]),  # type: ignore[arg-type]
        confidence=str(parsed["confidence"]),
        usage={"exit_code": completed.returncode, "backend": backend},
    )


class SubprocessCliAdapter:
    strict_isolation = False

    def __init__(self, *, backend_id: str, command: str) -> None:
        self.id = backend_id
        self.command = command
        self._process_observer: Callable[[int, int, str], None] | None = None
        self._lifetime_lock_path: Path | None = None

    def configure_process_tracking(
        self,
        *,
        observer: Callable[[int, int, str], None],
        lifetime_lock_path: Path,
    ) -> None:
        self._process_observer = observer
        self._lifetime_lock_path = Path(lifetime_lock_path)

    def available(self) -> bool:
        return shutil.which(self.command) is not None

    def authenticated(self) -> bool:
        return self.available()

    def _run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        prompt: str,
        timeout_seconds: float,
    ) -> AdapterResult:
        on_spawn: Callable[[int], None] | None = None
        if self._process_observer is not None:
            observer = self._process_observer

            def observe(pid: int) -> None:
                identity = identity_for_pid(pid)
                if os.name != "posix":
                    raise DispatchValidationError(
                        "tracked subprocess execution requires POSIX"
                    )
                process_group_id = os.getpgid(identity.pid)
                if process_group_id != identity.pid:
                    raise DispatchValidationError(
                        "tracked backend must lead a dedicated process group"
                    )
                observer(
                    identity.pid,
                    process_group_id,
                    identity.started_at,
                )

            on_spawn = observe
        try:
            completed = run_bounded(
                argv,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
                env=_backend_environment(self.id),
                input_text=prompt,
                on_spawn=on_spawn,
                lifetime_lock_path=self._lifetime_lock_path,
            )
        except OSError as exc:
            return AdapterResult(
                status="error",
                summary="",
                error_code="spawn_failed",
                warnings=[safe_error_text(exc, fallback="backend process could not start")],
            )
        return _completed_to_result(completed, backend=self.id)


class CodexAdapter(SubprocessCliAdapter):
    strict_isolation = False

    def authenticated(self) -> bool:
        if not self.available():
            return False
        try:
            completed = run_bounded(
                ["codex", "login", "status"],
                cwd=Path.cwd(),
                timeout_seconds=3.0,
                env=_backend_environment("codex"),
                max_output_bytes=32 * 1024,
            )
        except OSError:
            return False
        return completed.returncode == 0 and not completed.timed_out

    def run(
        self,
        *,
        contract: TaskContract,
        cwd: Path,
        context_files: Mapping[str, str],
        timeout_seconds: float,
    ) -> AdapterResult:
        if not self.available():
            return AdapterResult(
                status="error",
                summary="",
                error_code="backend_not_installed",
                warnings=["command not found: codex"],
            )
        sandbox = "workspace-write" if contract.mode == "edit" else "read-only"
        argv = [
            "codex",
            "exec",
            "--sandbox",
            sandbox,
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "-c",
            'approval_policy="never"',
            "-c",
            "sandbox_workspace_write.network_access=false",
            "-",
        ]
        return self._run(
            argv,
            cwd=cwd,
            prompt=_build_prompt(contract, context_files),
            timeout_seconds=timeout_seconds,
        )


class ClaudeAdapter(SubprocessCliAdapter):
    # Tool-less read-only mode reduces capability, but it is not an OS sandbox.
    strict_isolation = False

    def authenticated(self) -> bool:
        if not self.available():
            return False
        try:
            completed = run_bounded(
                ["claude", "auth", "status", "--json"],
                cwd=Path.cwd(),
                timeout_seconds=3.0,
                env=_backend_environment("claude"),
                max_output_bytes=32 * 1024,
            )
        except OSError:
            return False
        if completed.returncode != 0 or completed.timed_out:
            return False
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return False
        return isinstance(payload, dict) and payload.get("loggedIn") is True

    def run(
        self,
        *,
        contract: TaskContract,
        cwd: Path,
        context_files: Mapping[str, str],
        timeout_seconds: float,
    ) -> AdapterResult:
        if not self.available():
            return AdapterResult(
                status="error",
                summary="",
                error_code="backend_not_installed",
                warnings=["command not found: claude"],
            )
        edit_mode = contract.mode == "edit"
        argv = [
            "claude",
            "-p",
            "--output-format",
            "text",
            "--permission-mode",
            "acceptEdits" if edit_mode else "plan",
            "--safe-mode",
            "--disable-slash-commands",
            "--strict-mcp-config",
            "--mcp-config",
            "{}",
            "--no-session-persistence",
            "--tools",
            "Read,Edit" if edit_mode else "",
        ]
        return self._run(
            argv,
            cwd=cwd,
            prompt=_build_prompt(contract, context_files),
            timeout_seconds=timeout_seconds,
        )


def codex_adapter() -> SubprocessCliAdapter:
    return CodexAdapter(backend_id="codex", command="codex")


def claude_adapter() -> SubprocessCliAdapter:
    return ClaudeAdapter(backend_id="claude", command="claude")

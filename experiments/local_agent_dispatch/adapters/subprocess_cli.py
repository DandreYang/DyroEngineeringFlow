"""Generic headless CLI adapter (codex/claude-style print modes)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Mapping, Sequence

from ..task_contract import TaskContract
from .base import AdapterResult


def _build_prompt(contract: TaskContract, context_files: Mapping[str, str]) -> str:
    parts = [
        "# Task (self-contained; no prior conversation)",
        "",
        f"## Briefing\n{contract.task.briefing}",
        f"## Locations\n{contract.task.locations}",
        f"## Objective\n{contract.task.objective}",
        f"## Constraints\n{contract.task.constraints}",
        f"## Output contract\n{contract.task.output_contract}",
        "",
        "Respond as JSON with keys: summary (string), confidence (high|medium|low),",
        "evidence (array of {file, lines?, claim}). Do not include secrets.",
        "",
        "## Context files",
    ]
    for rel, content in sorted(context_files.items()):
        clipped = content if len(content) <= 40_000 else content[:40_000] + "\n…[truncated]\n"
        parts.append(f"\n### {rel}\n```\n{clipped}\n```")
    return "\n".join(parts)


def _parse_model_json(text: str) -> dict[str, object]:
    text = text.strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
        if isinstance(payload, dict):
            return payload
    except json.JSONDecodeError:
        pass
    # Try fenced block
    if "```" in text:
        chunks = text.split("```")
        for chunk in chunks:
            chunk = chunk.strip()
            if chunk.startswith("json"):
                chunk = chunk[4:].strip()
            try:
                payload = json.loads(chunk)
                if isinstance(payload, dict):
                    return payload
            except json.JSONDecodeError:
                continue
    # Fallback: whole text as summary
    return {"summary": text[:4000], "confidence": "low", "evidence": []}


class SubprocessCliAdapter:
    def __init__(
        self,
        *,
        backend_id: str,
        command: str,
        argv_builder,
    ) -> None:
        self.id = backend_id
        self.command = command
        self._argv_builder = argv_builder

    def available(self) -> bool:
        return shutil.which(self.command) is not None

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
                warnings=[f"command not found: {self.command}"],
            )
        prompt = _build_prompt(contract, context_files)
        argv: Sequence[str] = self._argv_builder(prompt)
        env = os.environ.copy()
        # Do not strip all env (CLI login may need it); never inject dyro secrets.
        try:
            completed = subprocess.run(
                list(argv),
                cwd=str(cwd),
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return AdapterResult(
                status="timeout",
                summary="",
                error_code="timeout",
                warnings=[f"backend timed out after {timeout_seconds}s"],
            )
        except OSError as exc:
            return AdapterResult(
                status="error",
                summary="",
                error_code="spawn_failed",
                warnings=[str(exc)],
            )

        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        if completed.returncode != 0:
            return AdapterResult(
                status="error",
                summary=(stdout or stderr)[:2000],
                error_code=f"exit_{completed.returncode}",
                warnings=[stderr[:500]] if stderr else [],
                raw_preview=stdout[:1000],
            )

        parsed = _parse_model_json(stdout)
        summary = str(parsed.get("summary") or stdout[:2000])
        confidence = str(parsed.get("confidence") or "medium")
        if confidence not in {"high", "medium", "low"}:
            confidence = "medium"
        evidence = parsed.get("evidence") if isinstance(parsed.get("evidence"), list) else []
        return AdapterResult(
            status="ok",
            summary=summary,
            evidence=[e for e in evidence if isinstance(e, dict)],
            confidence=confidence,
            warnings=[],
            usage={"exit_code": completed.returncode},
            raw_preview=stdout[:500],
            takeover=None,
        )


def codex_adapter() -> SubprocessCliAdapter:
    def argv_builder(prompt: str) -> list[str]:
        # Prefer non-interactive exec if available; fall back to print-style.
        return ["codex", "exec", "--json", "-"]

    # Use stdin prompt via a thin wrapper: write temp file for broader compat
    class CodexAdapter(SubprocessCliAdapter):
        def run(self, *, contract, cwd, context_files, timeout_seconds):
            if not self.available():
                return AdapterResult(
                    status="error",
                    summary="",
                    error_code="backend_not_installed",
                    warnings=["command not found: codex"],
                )
            prompt = _build_prompt(contract, context_files)
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", suffix=".md", delete=False
            ) as handle:
                handle.write(prompt)
                prompt_path = handle.name
            try:
                # codex exec variants differ; try common patterns
                attempts = [
                    ["codex", "exec", "--skip-git-repo-check", prompt],
                    ["codex", "exec", prompt],
                ]
                last_err = ""
                for argv in attempts:
                    try:
                        completed = subprocess.run(
                            argv,
                            cwd=str(cwd),
                            check=False,
                            capture_output=True,
                            text=True,
                            timeout=timeout_seconds,
                        )
                    except (OSError, subprocess.TimeoutExpired) as exc:
                        last_err = str(exc)
                        continue
                    if completed.returncode == 0 and (completed.stdout or "").strip():
                        parsed = _parse_model_json(completed.stdout)
                        return AdapterResult(
                            status="ok",
                            summary=str(parsed.get("summary") or completed.stdout[:2000]),
                            evidence=[
                                e
                                for e in (parsed.get("evidence") or [])
                                if isinstance(e, dict)
                            ],
                            confidence=str(parsed.get("confidence") or "medium"),
                            usage={"exit_code": 0},
                        )
                    last_err = (completed.stderr or completed.stdout or "")[:500]
                return AdapterResult(
                    status="error",
                    summary="",
                    error_code="codex_failed",
                    warnings=[last_err or "codex exec failed"],
                )
            finally:
                try:
                    os.unlink(prompt_path)
                except OSError:
                    pass

    return CodexAdapter(backend_id="codex", command="codex", argv_builder=lambda p: ["codex", "exec", p])


def claude_adapter() -> SubprocessCliAdapter:
    class ClaudeAdapter(SubprocessCliAdapter):
        def run(self, *, contract, cwd, context_files, timeout_seconds):
            if not self.available():
                return AdapterResult(
                    status="error",
                    summary="",
                    error_code="backend_not_installed",
                    warnings=["command not found: claude"],
                )
            prompt = _build_prompt(contract, context_files)
            argv = [
                "claude",
                "-p",
                prompt,
                "--output-format",
                "text",
                "--permission-mode",
                "plan",
            ]
            try:
                completed = subprocess.run(
                    argv,
                    cwd=str(cwd),
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=timeout_seconds,
                )
            except subprocess.TimeoutExpired:
                return AdapterResult(
                    status="timeout",
                    summary="",
                    error_code="timeout",
                )
            except OSError as exc:
                return AdapterResult(
                    status="error",
                    summary="",
                    error_code="spawn_failed",
                    warnings=[str(exc)],
                )
            if completed.returncode != 0:
                return AdapterResult(
                    status="error",
                    summary=(completed.stdout or completed.stderr or "")[:2000],
                    error_code=f"exit_{completed.returncode}",
                )
            parsed = _parse_model_json(completed.stdout or "")
            return AdapterResult(
                status="ok",
                summary=str(parsed.get("summary") or (completed.stdout or "")[:2000]),
                evidence=[
                    e for e in (parsed.get("evidence") or []) if isinstance(e, dict)
                ],
                confidence=str(parsed.get("confidence") or "medium"),
            )

    return ClaudeAdapter(
        backend_id="claude",
        command="claude",
        argv_builder=lambda p: ["claude", "-p", p],
    )

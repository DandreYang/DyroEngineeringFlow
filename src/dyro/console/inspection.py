"""Parent-side, bounded process boundary for Console inspection."""

from __future__ import annotations

import base64
from collections.abc import Mapping
from datetime import datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
from typing import Any

from ..canonical import canonical_json_bytes
from ..hub import registry_home
from .overview import ConsoleOverviewError
from .redaction import REDACTED, safe_id, safe_sha256, safe_title


_CURSOR_SECRET_ENV = "DYRO_CONSOLE_CURSOR_SECRET"
_WORKER_OUTPUT_LIMIT = 2 * 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CURSOR = re.compile(r"^[A-Za-z0-9_-]{1,512}$")
_WARNING_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,79}$")
_ATTENTION_KINDS = frozenset(
    {"repair_required", "needs_user", "ready", "paused", "waiting"}
)
_SUMMARY_KEYS = frozenset(
    {
        "alias",
        "display_name",
        "is_default",
        "availability",
        "health",
        "freshness",
        "repository_count",
        "line_count",
        "objective_count",
        "active_objective_count",
        "task_count",
        "task_status_counts",
        "attention_counts",
        "recommendation",
        "snapshot_sha256",
    }
)


class IsolatedOverviewService:
    """Request a whitelisted overview from a hard-terminable worker process.

    The listener only retains this IPC client.  It never receives a registry
    record or Config object, and the child receives a minimal environment with
    no launcher, provider, or session credentials.
    """

    def __init__(
        self,
        *,
        registry_state_home: Path | None = None,
        timeout_seconds: float = 5.0,
        cursor_secret: bytes | None = None,
        python_executable: str | None = None,
        target_root: Path | None = None,
    ) -> None:
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not 0.1 <= float(timeout_seconds) <= 10.0
        ):
            raise ValueError("Console inspection timeout 必须为 0.1 到 10 秒")
        if cursor_secret is not None and (
            not isinstance(cursor_secret, bytes) or len(cursor_secret) < 32
        ):
            raise ValueError("Console inspection cursor secret 必须至少为 256 bit")
        self._registry_state_home = (registry_state_home or registry_home()).absolute()
        self._timeout_seconds = float(timeout_seconds)
        self._cursor_secret = cursor_secret or os.urandom(32)
        self._python_executable = python_executable or sys.executable
        if target_root is not None and not isinstance(target_root, Path):
            raise ValueError("Console target_root 必须是 Path")
        self._target_root = target_root.absolute() if target_root is not None else None

    @property
    def target_root(self) -> Path | None:
        """The transient, read-only Profile root selected by ``--root``."""
        return self._target_root

    def page(self, *, cursor: str | None = None, limit: int = 20) -> dict[str, object]:
        return self._request({"op": "overview", "cursor": cursor, "limit": limit})

    def workspace(self, alias: str) -> dict[str, object]:
        return self._request({"op": "workspace", "alias": alias})

    def _request(self, request: Mapping[str, object]) -> dict[str, object]:
        worker_request = dict(request)
        if self._target_root is not None:
            worker_request["target_root"] = str(self._target_root)
        encoded_request = base64.urlsafe_b64encode(
            json.dumps(worker_request, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).rstrip(b"=").decode("ascii")
        if len(encoded_request) > 2048:
            raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE")
        output = self._run_worker(encoded_request)
        operation = request.get("op")
        return self._parse_worker_output(
            output, expected_operation=operation if isinstance(operation, str) else None
        )

    def _run_worker(self, encoded_request: str) -> bytes:
        # Console must not claim a bounded inspection lifetime on Windows until
        # a tested Job Object implementation can terminate the entire worker
        # tree.  Killing only the outer process may orphan a child reader.
        if os.name == "nt":
            raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE")
        environment = {
            "PATH": os.defpath,
            "PYTHONUTF8": "1",
            "DYRO_HOME": str(self._registry_state_home),
            _CURSOR_SECRET_ENV: base64.urlsafe_b64encode(self._cursor_secret)
            .rstrip(b"=")
            .decode("ascii"),
        }
        try:
            process = subprocess.Popen(
                (
                    self._python_executable,
                    "-m",
                    "dyro.console._inspect_worker",
                    "--request",
                    encoded_request,
                ),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                cwd=tempfile.gettempdir(),
                env=environment,
                close_fds=True,
                start_new_session=os.name != "nt",
            )
            output, _ = process.communicate(timeout=self._timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            self._terminate(process)
            try:
                output, _ = process.communicate(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                output = b""
            del exc
            raise ConsoleOverviewError("OVERVIEW_TIMEOUT") from None
        except (OSError, ValueError):
            raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE") from None
        if process.returncode != 0 or len(output) > _WORKER_OUTPUT_LIMIT:
            raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE")
        return output

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes]) -> None:
        if os.name != "nt" and process.pid is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
                return
            except OSError:
                pass
        try:
            process.kill()
        except OSError:
            pass

    @classmethod
    def _parse_worker_output(
        cls, output: bytes, *, expected_operation: str | None = None
    ) -> dict[str, object]:
        try:
            decoded: Any = json.loads(output.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE") from None
        if not isinstance(decoded, dict):
            raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE")
        if decoded.get("ok") is False:
            if set(decoded) != {"ok", "error"}:
                raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE")
            error = decoded.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{2,79}", code):
                raise ConsoleOverviewError(code)
            raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE")
        if decoded.get("ok") is not True:
            raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE")
        if set(decoded) != {"ok", "payload"}:
            raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE")
        payload = decoded.get("payload")
        if not isinstance(payload, dict) or set(payload) - {
            "schema_version",
            "captured_at",
            "snapshot_sha256",
            "freshness",
            "data",
        }:
            raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE")
        if payload.get("schema_version") != 1 or not _SHA256.fullmatch(str(payload.get("snapshot_sha256", ""))):
            raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE")
        captured_at = payload.get("captured_at")
        if not isinstance(captured_at, str):
            raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE")
        try:
            parsed_time = datetime.fromisoformat(captured_at)
        except ValueError:
            raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE") from None
        if parsed_time.tzinfo is None:
            raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE")
        freshness = payload.get("freshness")
        if (
            not isinstance(freshness, dict)
            or set(freshness) != {"state", "partial", "warnings"}
            or freshness.get("state") not in {"fresh", "partial"}
            or type(freshness.get("partial")) is not bool
            or not isinstance(freshness.get("warnings"), list)
            or not isinstance(payload.get("data"), dict)
        ):
            raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE")
        state = freshness["state"]
        partial = freshness["partial"]
        warnings = freshness["warnings"]
        if partial != (state == "partial"):
            raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE")
        warning_codes = cls._warning_codes(warnings)
        data = dict(payload["data"])
        cls._validate_data(data, expected_operation=expected_operation)
        expected = hashlib.sha256(
            canonical_json_bytes(
                {
                    "schema_version": 1,
                    "freshness": {
                        "state": state,
                        "partial": partial,
                        "warnings": warning_codes,
                    },
                    "data": data,
                }
            )
        ).hexdigest()
        if not hmac.compare_digest(expected, payload["snapshot_sha256"]):
            raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE")
        return dict(payload)

    @staticmethod
    def _warning_codes(value: list[object]) -> list[str]:
        codes: list[str] = []
        for item in value:
            if not isinstance(item, dict) or set(item) != {"code"}:
                raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE")
            code = item.get("code")
            if not isinstance(code, str) or not _WARNING_CODE.fullmatch(code):
                raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE")
            codes.append(code)
        if codes != sorted(set(codes)):
            raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE")
        return codes

    @classmethod
    def _validate_data(
        cls, data: dict[str, object], *, expected_operation: str | None
    ) -> None:
        if set(data) == {"workspace"}:
            if expected_operation == "overview":
                raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE")
            cls._validate_summary(data["workspace"])
            return
        if expected_operation == "workspace":
            raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE")
        expected = {
            "default_workspace",
            "total_workspaces",
            "workspaces",
            "next_cursor",
            "attention_counts",
            "highest_priority",
        }
        if set(data) != expected:
            raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE")
        default_workspace = data["default_workspace"]
        if default_workspace != "" and not cls._safe_alias(default_workspace):
            raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE")
        if not cls._count(data["total_workspaces"]):
            raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE")
        workspaces = data["workspaces"]
        if not isinstance(workspaces, list) or len(workspaces) > 100:
            raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE")
        for item in workspaces:
            cls._validate_summary(item)
        next_cursor = data["next_cursor"]
        if next_cursor is not None and (
            not isinstance(next_cursor, str) or not _CURSOR.fullmatch(next_cursor)
        ):
            raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE")
        cls._validate_attention_counts(data["attention_counts"])
        highest = data["highest_priority"]
        if highest is not None:
            if not isinstance(highest, dict) or set(highest) != {"alias", "kind", "reason"}:
                raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE")
            if (
                not cls._safe_alias(highest.get("alias"))
                or highest.get("kind") not in _ATTENTION_KINDS
                or not cls._safe_code(highest.get("reason"))
            ):
                raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE")

    @classmethod
    def _validate_summary(cls, value: object) -> None:
        if not isinstance(value, dict) or set(value) != _SUMMARY_KEYS:
            raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE")
        alias = value.get("alias")
        if not cls._safe_alias(alias):
            raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE")
        display_name = value.get("display_name")
        if display_name != REDACTED and safe_title(display_name) != display_name:
            raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE")
        if type(value.get("is_default")) is not bool:
            raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE")
        if value.get("availability") not in {"available", "unavailable"}:
            raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE")
        if value.get("health") not in {"healthy", "degraded", "unavailable"}:
            raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE")
        if value.get("freshness") not in {"fresh", "partial"}:
            raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE")
        for key in (
            "repository_count",
            "line_count",
            "objective_count",
            "active_objective_count",
            "task_count",
        ):
            if not cls._count(value.get(key)):
                raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE")
        statuses = value.get("task_status_counts")
        if not isinstance(statuses, dict):
            raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE")
        for status, count in statuses.items():
            if not cls._safe_code(status) or not cls._count(count):
                raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE")
        cls._validate_attention_counts(value.get("attention_counts"))
        recommendation = value.get("recommendation")
        if not isinstance(recommendation, dict) or set(recommendation) != {"reason", "command"}:
            raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE")
        reason = recommendation.get("reason")
        command = recommendation.get("command")
        if not cls._safe_code(reason) or not cls._safe_command(command, alias):
            raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE")
        digest = value.get("snapshot_sha256")
        if digest != "" and safe_sha256(digest) != digest:
            raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE")

    @staticmethod
    def _count(value: object) -> bool:
        return type(value) is int and 0 <= value <= 1_000_000

    @staticmethod
    def _safe_alias(value: object) -> bool:
        return isinstance(value, str) and safe_id(value) == value and value != REDACTED

    @staticmethod
    def _safe_code(value: object) -> bool:
        return isinstance(value, str) and safe_id(value) == value

    @staticmethod
    def _safe_command(command: object, alias: object) -> bool:
        if not isinstance(command, str) or not isinstance(alias, str):
            return False
        escaped_alias = re.escape(alias)
        return bool(
            re.fullmatch(
                rf"dyro --workspace {escaped_alias} (?:doctor|task next|objective explain [A-Za-z0-9][A-Za-z0-9._-]{{0,79}})",
                command,
            )
        )

    @staticmethod
    def _validate_attention_counts(value: object) -> None:
        if not isinstance(value, dict) or set(value) != _ATTENTION_KINDS:
            raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE")
        if not all(IsolatedOverviewService._count(item) for item in value.values()):
            raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE")

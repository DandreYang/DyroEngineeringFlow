"""Exec-only Console inspection worker.

This module is never reached from an HTTP request handler by import.  The
parent starts it with a minimal environment and a fixed module argv, so a
blocked workspace read can be terminated as a complete process group.
"""

from __future__ import annotations

import argparse
import base64
import json
from multiprocessing import get_context
import os
from pathlib import Path
import queue
import sys
import time
from typing import Any

from ..config import load
from ..hub import WorkspaceRecord, WorkspaceRegistry
from .overview import (
    ConsoleOverviewError,
    ConsoleOverviewService,
    WORKSPACE_MISSING_ROOT,
    WORKSPACE_TIMEOUT,
    WORKSPACE_UNAVAILABLE,
    unavailable_workspace_summary,
    workspace_root_missing,
)


_CURSOR_SECRET_ENV = "DYRO_CONSOLE_CURSOR_SECRET"
_MAX_WORKERS = 4
_WORKSPACE_TIMEOUT_SECONDS = 3.0
_OVERVIEW_TIMEOUT_SECONDS = 6.0
_WORKER_RESPONSE_LIMIT = 2 * 1024 * 1024


def _unavailable_summary(alias: str, code: str) -> dict[str, object]:
    return unavailable_workspace_summary(alias, False, reason=code)


def _capture_workspace_summary(
    result_queue: Any,
    record: WorkspaceRecord,
    is_default: bool,
) -> None:
    """Capture one workspace only, returning a JSON-safe value through IPC."""
    try:
        if workspace_root_missing(record.root):
            result_queue.put(
                {
                    "summary": _unavailable_summary(record.name, WORKSPACE_MISSING_ROOT),
                    "warnings": [WORKSPACE_MISSING_ROOT],
                }
            )
            return
        registry = WorkspaceRegistry(
            default=record.name if is_default else "",
            workspaces=(record,),
        )
        service = ConsoleOverviewService(registry_loader=lambda: registry)
        payload = service.workspace(record.name)
        summary = payload["data"]["workspace"]
        warnings = [item["code"] for item in payload["freshness"]["warnings"]]
        result_queue.put({"summary": summary, "warnings": warnings})
    except Exception:
        result_queue.put(
            {
                "summary": _unavailable_summary(record.name, WORKSPACE_UNAVAILABLE),
                "warnings": [WORKSPACE_UNAVAILABLE],
            }
        )


def _parse_child_result(
    value: object, record: WorkspaceRecord, *, is_default: bool
) -> tuple[dict[str, object], set[str]]:
    if not isinstance(value, dict):
        return _unavailable_summary(record.name, WORKSPACE_UNAVAILABLE), {
            WORKSPACE_UNAVAILABLE
        }
    summary = value.get("summary")
    warnings = value.get("warnings")
    if (
        not isinstance(summary, dict)
        or not isinstance(warnings, list)
        or not all(isinstance(item, str) for item in warnings)
    ):
        return _unavailable_summary(record.name, WORKSPACE_UNAVAILABLE), {
            WORKSPACE_UNAVAILABLE
        }
    copied = dict(summary)
    copied["alias"] = record.name
    copied["is_default"] = is_default
    return copied, set(warnings)


def _isolated_summaries(
    registry: WorkspaceRegistry,
    *,
    workspace_timeout: float = _WORKSPACE_TIMEOUT_SECONDS,
    total_timeout: float = _OVERVIEW_TIMEOUT_SECONDS,
) -> tuple[list[dict[str, object]], set[str]]:
    """Sample at most four workspaces concurrently in killable child processes."""
    context = get_context("spawn")
    pending = list(registry.workspaces)
    running: dict[str, tuple[Any, Any, float, WorkspaceRecord]] = {}
    summaries: list[dict[str, object]] = []
    warnings: set[str] = set()
    deadline = time.monotonic() + total_timeout

    def finish(record: WorkspaceRecord, value: object, *, default: bool) -> None:
        summary, codes = _parse_child_result(value, record, is_default=default)
        summaries.append(summary)
        warnings.update(codes)

    while pending or running:
        now = time.monotonic()
        if now >= deadline:
            for _, process, _, record in running.values():
                if process.is_alive():
                    process.terminate()
                process.join(timeout=0.1)
                finish(
                    record,
                    {
                        "summary": _unavailable_summary(record.name, WORKSPACE_TIMEOUT),
                        "warnings": [WORKSPACE_TIMEOUT],
                    },
                    default=record.name == registry.default,
                )
            running.clear()
            for record in pending:
                finish(
                    record,
                    {
                        "summary": _unavailable_summary(record.name, WORKSPACE_TIMEOUT),
                        "warnings": [WORKSPACE_TIMEOUT],
                    },
                    default=record.name == registry.default,
                )
            pending.clear()
            break
        while pending and len(running) < _MAX_WORKERS:
            record = pending.pop(0)
            result_queue = context.Queue(maxsize=1)
            process = context.Process(
                target=_capture_workspace_summary,
                args=(result_queue, record, record.name == registry.default),
                daemon=True,
            )
            process.start()
            running[record.name] = (result_queue, process, now, record)

        made_progress = False
        for name, (result_queue, process, started_at, record) in tuple(running.items()):
            value: object | None = None
            has_value = False
            try:
                value = result_queue.get_nowait()
                has_value = True
            except queue.Empty:
                pass
            timed_out = time.monotonic() - started_at >= workspace_timeout
            if has_value or not process.is_alive() or timed_out:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=0.1)
                if not has_value:
                    try:
                        value = result_queue.get(timeout=0.05)
                        has_value = True
                    except queue.Empty:
                        pass
                if has_value:
                    finish(record, value, default=record.name == registry.default)
                else:
                    code = WORKSPACE_TIMEOUT if timed_out else WORKSPACE_UNAVAILABLE
                    finish(
                        record,
                        {
                            "summary": _unavailable_summary(record.name, code),
                            "warnings": [code],
                        },
                        default=record.name == registry.default,
                    )
                result_queue.close()
                running.pop(name)
                made_progress = True
        if not made_progress:
            time.sleep(0.01)
    return summaries, warnings


def _isolated_workspace(
    service: ConsoleOverviewService, alias: str
) -> dict[str, object]:
    # Run in this exec worker so hung git stays in the parent's killpg
    # group. Nested summary spawn only returns the count card.
    return service.workspace(alias)


def _secret_from_environment() -> bytes:
    raw = os.environ.get(_CURSOR_SECRET_ENV)
    if not isinstance(raw, str):
        raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE")
    try:
        decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except (ValueError, UnicodeError):
        raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE") from None
    if len(decoded) < 32:
        raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE")
    return decoded


def _decode_request(value: str) -> dict[str, object]:
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        decoded = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, json.JSONDecodeError):
        raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE") from None
    if not isinstance(decoded, dict) or set(decoded) - {
        "op",
        "cursor",
        "limit",
        "alias",
        "after",
        "parent",
        "filter",
        "target_root",
    }:
        raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE")
    return decoded


def _target_registry(request: dict[str, object]) -> WorkspaceRegistry | None:
    raw_root = request.get("target_root")
    if raw_root is None:
        return None
    if not isinstance(raw_root, str) or not raw_root or len(raw_root) > 4096:
        raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE")
    root = Path(raw_root)
    if not root.is_absolute():
        raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE")
    try:
        config = load(root)
    except Exception:
        raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE") from None
    return WorkspaceRegistry(
        default=config.name,
        workspaces=(WorkspaceRecord(name=config.name, root=config.root),),
    )


def _response(payload: dict[str, object] | None = None, *, code: str = "") -> int:
    body: dict[str, object] = {"ok": not code}
    if code:
        body["error"] = {"code": code}
    else:
        body["payload"] = payload
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    if len(encoded) > _WORKER_RESPONSE_LIMIT:
        encoded = b'{"error":{"code":"OVERVIEW_UNAVAILABLE"},"ok":false}'
    sys.stdout.buffer.write(encoded)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--request")
    args = parser.parse_args(argv)
    try:
        request = _decode_request(args.request)
        target_registry = _target_registry(request)
        service_arguments: dict[str, object] = {
            "cursor_secret": _secret_from_environment(),
            "summary_loader": _isolated_summaries,
        }
        if target_registry is not None:
            service_arguments["registry_loader"] = lambda: target_registry
        service = ConsoleOverviewService(**service_arguments)
        operation = request.get("op")
        if operation in {
            "post_channel",
            "artifact_write",
            "artifacts_write",
            "put_artifact",
        }:
            raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE")
        if operation == "overview":
            payload = service.page(
                cursor=request.get("cursor"),
                limit=request.get("limit", 20),
            )
        elif operation == "workspace":
            alias = request.get("alias")
            if not isinstance(alias, str):
                raise ConsoleOverviewError("WORKSPACE_ALIAS_INVALID")
            payload = _isolated_workspace(service, alias)
        elif operation == "inspect_proofs":
            alias = request.get("alias")
            if not isinstance(alias, str):
                raise ConsoleOverviewError("WORKSPACE_ALIAS_INVALID")
            # Inspect is its own exec-worker request. Keep git descendants in
            # this process group so the parent's 8s killpg can reap them.
            payload = service.inspect_proofs(alias)
        elif operation == "system":
            payload = service.system()
        elif operation == "events":
            alias = request.get("alias")
            if not isinstance(alias, str):
                raise ConsoleOverviewError("WORKSPACE_ALIAS_INVALID")
            after = request.get("after")
            if after is not None and not isinstance(after, str):
                raise ConsoleOverviewError("EVENT_CURSOR_INVALID")
            limit = request.get("limit", 50)
            if type(limit) is not int:
                raise ConsoleOverviewError("EVENT_LIMIT_INVALID")
            payload = service.events(alias, after=after, limit=limit)
        elif operation == "families":
            alias = request.get("alias")
            if not isinstance(alias, str):
                raise ConsoleOverviewError("WORKSPACE_ALIAS_INVALID")
            payload = service.families(alias)
        elif operation == "family":
            alias = request.get("alias")
            parent = request.get("parent")
            if not isinstance(alias, str):
                raise ConsoleOverviewError("WORKSPACE_ALIAS_INVALID")
            if not isinstance(parent, str):
                raise ConsoleOverviewError("FAMILY_PARENT_INVALID")
            payload = service.family(alias, parent)
        elif operation == "channel":
            alias = request.get("alias")
            parent = request.get("parent")
            if not isinstance(alias, str):
                raise ConsoleOverviewError("WORKSPACE_ALIAS_INVALID")
            if not isinstance(parent, str):
                raise ConsoleOverviewError("FAMILY_PARENT_INVALID")
            after = request.get("after")
            if after is not None and not isinstance(after, str):
                raise ConsoleOverviewError("CHANNEL_CURSOR_INVALID")
            filter_text = request.get("filter")
            if filter_text is not None and not isinstance(filter_text, str):
                raise ConsoleOverviewError("CHANNEL_FILTER_INVALID")
            limit = request.get("limit", 50)
            if type(limit) is not int:
                raise ConsoleOverviewError("CHANNEL_LIMIT_INVALID")
            payload = service.channel(
                alias, parent, after=after, filter=filter_text, limit=limit
            )
        else:
            raise ConsoleOverviewError("OVERVIEW_UNAVAILABLE")
        return _response(payload)
    except ConsoleOverviewError as exc:
        return _response(code=exc.code)
    except Exception:
        return _response(code="OVERVIEW_UNAVAILABLE")


if __name__ == "__main__":
    raise SystemExit(main())

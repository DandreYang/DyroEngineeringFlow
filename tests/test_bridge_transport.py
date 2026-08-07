from __future__ import annotations

import ast
from dataclasses import replace
from io import BytesIO
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import unittest
from unittest.mock import patch

from dyro.bridge import transport as transport_module
from dyro.bridge.catalog import CATALOG
from dyro.bridge.models import ErrorCode
from dyro.bridge.observations import bridge_error
from dyro.bridge.transport import (
    MAX_JSON_DEPTH,
    MAX_REQUEST_BYTES,
    TransportContext,
    handle_request_bytes,
    run,
)
from dyro.bridge.plans import plan_objective
from dyro.config import load
from dyro.continuation.store import create_objective
from dyro.tasks import task_template
from dyro.workspace import create_line

from .support import WorkspaceCase


def _request(
    operation: str = "bridge.hello",
    operation_input: dict[str, object] | None = None,
    **overrides: object,
) -> bytes:
    document: dict[str, object] = {
        "protocol": {"major": 1, "minor": 0},
        "request_id": "req-1",
        "client": {"name": "test-client", "version": "1.0"},
        "operation": operation,
        "input": operation_input or {},
    }
    document.update(overrides)
    return json.dumps(document, separators=(",", ":")).encode()


def _context(**kwargs: object) -> TransportContext:
    values: dict[str, object] = {
        "platform": "linux-ubuntu-24.04",
        "cwd": Path("."),
        "allow_test_services": True,
        "event_id_factory": lambda: "evt_test",
    }
    values.update(kwargs)
    return TransportContext(**values)


class _BrokenWriter:
    calls = 0

    def write(self, _: object) -> int:
        self.calls += 1
        raise BrokenPipeError

    def flush(self) -> None:
        raise AssertionError("flush must not run after broken write")


class _PartialThenClosedWriter:
    def __init__(self) -> None:
        self.calls = 0

    def write(self, payload: object) -> int:
        self.calls += 1
        if self.calls == 1:
            return min(8, len(payload))  # type: ignore[arg-type]
        raise ValueError("closed stream")

    def flush(self) -> None:
        raise AssertionError("flush must not run after closed write")


class _CloseFailsReader(BytesIO):
    def close(self) -> None:
        raise OSError("close failed")

    def __del__(self) -> None:
        pass


class BridgeTransportTests(unittest.TestCase):
    def assert_error(
        self,
        raw: bytes,
        expected: ErrorCode,
        expected_exit: int = 2,
        *,
        context: TransportContext | None = None,
    ) -> dict[str, object]:
        response, exit_code = handle_request_bytes(raw, context or _context())
        self.assertEqual(exit_code, expected_exit)
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], expected.value)
        return response

    def test_production_catalog_exposes_linux_mandatory_and_denies_other_hosts(
        self,
    ) -> None:
        linux = replace(
            _context(), platform="linux-ubuntu-24.04", allow_test_services=False
        )
        response, exit_code = handle_request_bytes(_request(), linux)
        self.assertEqual(exit_code, 0)
        self.assertTrue(response["ok"])
        self.assert_error(
            _request("objective.explain", {"objective_id": "OBJ-1"}),
            ErrorCode.OPERATION_UNAVAILABLE,
            4,
            context=linux,
        )

        context = replace(_context(), platform="macos-15", allow_test_services=False)
        self.assert_error(
            _request(), ErrorCode.OPERATION_UNAVAILABLE, 4, context=context
        )
        context = replace(_context(), platform="windows", allow_test_services=False)
        self.assert_error(
            _request(), ErrorCode.OPERATION_UNAVAILABLE, 4, context=context
        )

    def test_runtime_platform_detection_fails_closed_on_unverified_versions(
        self,
    ) -> None:
        with (
            patch.object(transport_module.sys, "platform", "darwin"),
            patch.object(
                transport_module.runtime_platform,
                "mac_ver",
                return_value=("14.7", (), ""),
            ),
        ):
            self.assertEqual(transport_module._platform_name(), "unsupported")
        with (
            patch.object(transport_module.sys, "platform", "darwin"),
            patch.object(
                transport_module.runtime_platform,
                "mac_ver",
                side_effect=PermissionError,
            ),
        ):
            self.assertEqual(transport_module._platform_name(), "unsupported")
        with (
            patch.object(transport_module.sys, "platform", "darwin"),
            patch.object(
                transport_module.runtime_platform,
                "mac_ver",
                return_value=("15.4", (), ""),
            ),
        ):
            self.assertEqual(transport_module._platform_name(), "macos-15")
        with (
            patch.object(transport_module.sys, "platform", "linux"),
            patch.object(
                transport_module.runtime_platform,
                "freedesktop_os_release",
                return_value={"ID": "debian", "VERSION_ID": "12"},
            ),
        ):
            self.assertEqual(transport_module._platform_name(), "unsupported")
        with (
            patch.object(transport_module.sys, "platform", "linux"),
            patch.object(
                transport_module.runtime_platform,
                "freedesktop_os_release",
                return_value={"ID": "ubuntu", "VERSION_ID": "24.04"},
            ),
        ):
            self.assertEqual(transport_module._platform_name(), "linux-ubuntu-24.04")

    def test_internal_hello_and_capabilities_use_static_routes(self) -> None:
        response, exit_code = handle_request_bytes(_request(), _context())
        self.assertEqual(exit_code, 0)
        self.assertTrue(response["ok"])
        self.assertEqual(response["data"]["bridge_version"], "1.0")
        self.assertEqual(response["meta"]["event_id"], "evt_test")

        response, exit_code = handle_request_bytes(
            _request("bridge.capabilities.compact"), _context()
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(response["data"]["operations"]), len(CATALOG.operations))

    def test_schema_discovery_rejects_non_callable_target(self) -> None:
        self.assert_error(
            _request("bridge.operation.schema", {"operation": "line.list"}),
            ErrorCode.OPERATION_UNAVAILABLE,
            4,
        )
        response, exit_code = handle_request_bytes(
            _request("bridge.operation.schema", {"operation": "workspace.resolve"}),
            _context(),
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(response["data"]["operation"], "workspace.resolve")

    def test_parser_rejects_invalid_and_ambiguous_documents(self) -> None:
        invalid_documents = (
            b"\xff",
            b"[]",
            b'{"x":1,"x":2}',
            b'{"x":{"y":1,"y":2}}',
            b"{} {}",
            b'{"x":NaN}',
            b'{"x":Infinity}',
            ("\u00a0" + _request().decode()).encode(),
            (_request().decode() + "\u2003").encode(),
            _request(request_id="\ud800"),
            _request(client={"name": "\ud800", "version": "1"}),
            ("[" * (MAX_JSON_DEPTH + 1) + "]" * (MAX_JSON_DEPTH + 1)).encode(),
            ('{"x":' + "1" * 129 + "}").encode(),
        )
        for raw in invalid_documents:
            with self.subTest(raw=raw[:30]):
                self.assert_error(raw, ErrorCode.INVALID_JSON)

    def test_request_limit_is_checked_before_parsing(self) -> None:
        response = self.assert_error(
            b" " * (MAX_REQUEST_BYTES + 1), ErrorCode.REQUEST_TOO_LARGE
        )
        self.assertIsNone(response["meta"]["requested_protocol"])
        self.assertIsNone(response["meta"]["operation"])

    def test_node_limit_is_checked_before_json_decoding(self) -> None:
        raw = ("[" + ",".join("null" for _ in range(10_001)) + "]").encode()
        with patch("dyro.bridge.transport.json.JSONDecoder") as decoder:
            self.assert_error(raw, ErrorCode.INVALID_JSON)
        decoder.assert_not_called()

    def test_envelope_protocol_operation_and_input_errors_are_distinct(self) -> None:
        self.assert_error(
            _request(extra="forbidden"), ErrorCode.SCHEMA_VALIDATION_FAILED
        )
        self.assert_error(
            _request(protocol={"major": 2, "minor": 0}),
            ErrorCode.PROTOCOL_MAJOR_UNSUPPORTED,
        )
        self.assert_error(
            _request(protocol={"major": 1, "minor": 1}),
            ErrorCode.PROTOCOL_MINOR_UNSUPPORTED,
        )
        self.assert_error(
            _request(protocol={"major": 1, "minor": 65_536}),
            ErrorCode.PROTOCOL_MINOR_UNSUPPORTED,
        )
        oversized = self.assert_error(
            _request(protocol={"major": 9_007_199_254_740_992, "minor": 0}),
            ErrorCode.SCHEMA_VALIDATION_FAILED,
        )
        self.assertIsNone(oversized["meta"]["requested_protocol"])
        self.assert_error(_request("unknown.operation"), ErrorCode.OPERATION_UNKNOWN)
        self.assert_error(
            _request("workspace.resolve", {"unexpected": True}),
            ErrorCode.SCHEMA_VALIDATION_FAILED,
        )

    def test_operation_input_schema_precedes_handler(self) -> None:
        called = False

        def handler(_: dict[str, object], __: TransportContext) -> object:
            nonlocal called
            called = True
            return {}

        service_id = CATALOG.get("workspace.resolve").service_id
        context = _context(handlers={service_id: handler})
        self.assert_error(
            _request("workspace.resolve", {"unexpected": True}),
            ErrorCode.SCHEMA_VALIDATION_FAILED,
            context=context,
        )
        self.assertFalse(called)

    def test_core_error_and_unexpected_exception_are_redacted(self) -> None:
        service_id = CATALOG.get("workspace.resolve").service_id

        def expected(_: dict[str, object], __: TransportContext) -> object:
            raise bridge_error(ErrorCode.WORKSPACE_NOT_FOUND)

        context = _context(handlers={service_id: expected})
        self.assert_error(
            _request("workspace.resolve"),
            ErrorCode.WORKSPACE_NOT_FOUND,
            3,
            context=context,
        )

        secret = "sk-abcdefghijklmnop /Users/private"

        def unexpected(_: dict[str, object], __: TransportContext) -> object:
            raise RuntimeError(secret)

        context = _context(handlers={service_id: unexpected})
        response = self.assert_error(
            _request("workspace.resolve"),
            ErrorCode.INTERNAL_ERROR,
            3,
            context=context,
        )
        self.assertNotIn(secret, json.dumps(response))

    def test_core_python_and_fd_output_cannot_pollute_protocol(self) -> None:
        service_id = CATALOG.get("bridge.hello").service_id
        secret = "sk-abcdefghijklmnop"

        def noisy(_: dict[str, object], __: TransportContext) -> object:
            print(secret)
            os.write(1, secret.encode())
            os.write(2, secret.encode())
            return {
                "dyro_version": "test",
                "bridge_version": "1.0",
                "server_protocol": {"major": 1, "minor": 0},
            }

        output = BytesIO()
        exit_code = run(
            BytesIO(_request()),
            output,
            _context(handlers={service_id: noisy}),
        )
        self.assertEqual(exit_code, 0)
        self.assertNotIn(secret.encode(), output.getvalue())
        self.assertTrue(json.loads(output.getvalue())["ok"])

    def test_stdin_is_closed_before_core_handler(self) -> None:
        service_id = CATALOG.get("bridge.hello").service_id
        request_stream = BytesIO(_request())

        def handler(_: dict[str, object], __: TransportContext) -> object:
            self.assertTrue(request_stream.closed)
            return {
                "dyro_version": "test",
                "bridge_version": "1.0",
                "server_protocol": {"major": 1, "minor": 0},
            }

        self.assertEqual(
            run(
                request_stream,
                BytesIO(),
                _context(handlers={service_id: handler}),
            ),
            0,
        )

    def test_stdin_close_failure_never_reaches_core(self) -> None:
        service_id = CATALOG.get("bridge.hello").service_id
        called = False

        def handler(_: dict[str, object], __: TransportContext) -> object:
            nonlocal called
            called = True
            return {}

        output = BytesIO()
        exit_code = run(
            _CloseFailsReader(_request()),
            output,
            _context(handlers={service_id: handler}),
        )
        self.assertEqual(exit_code, 3)
        self.assertFalse(called)
        self.assertEqual(
            json.loads(output.getvalue())["error"]["code"],
            ErrorCode.INTERNAL_ERROR.value,
        )

    def test_subprocess_core_output_is_silenced_on_real_fds(self) -> None:
        program = r"""
import os
from pathlib import Path
import sys
from dyro.bridge.catalog import CATALOG
from dyro.bridge.transport import TransportContext, run

secret = "sk-abcdefghijklmnop"
service_id = CATALOG.get("bridge.hello").service_id
class NoisyResult:
    @property
    def partial(self):
        os.write(1, b"DTO_PROPERTY_LEAK")
        return False
    truncated = False
    def as_dict(self):
        print("DTO_PRINT_LEAK", flush=True)
        os.write(1, b"DTO_FD_LEAK")
        return {
            "dyro_version": "test",
            "bridge_version": "1.0",
            "server_protocol": {"major": 1, "minor": 0},
        }
def noisy(_request, _context):
    print(secret, flush=True)
    os.write(1, secret.encode())
    os.write(2, secret.encode())
    return NoisyResult()
context = TransportContext(
    platform="linux-ubuntu-24.04",
    cwd=Path("."),
    allow_test_services=True,
    handlers={service_id: noisy},
    event_id_factory=lambda: "evt_test",
)
raise SystemExit(run(sys.stdin.buffer, sys.stdout.buffer, context))
"""
        completed = subprocess.run(
            (sys.executable, "-c", program),
            input=_request(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stderr, b"")
        self.assertNotIn(b"sk-", completed.stdout)
        self.assertEqual(completed.stdout.count(b"\n"), 1)
        self.assertTrue(json.loads(completed.stdout)["ok"])

    def test_subprocess_uses_eof_as_the_request_frame(self) -> None:
        process = subprocess.Popen(
            (sys.executable, "-m", "dyro.bridge.transport"),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        process.stdin.write(_request())
        process.stdin.flush()
        readable, _, _ = select.select((process.stdout,), (), (), 0.1)
        self.assertEqual(readable, [])
        process.stdin.close()
        stdout = process.stdout.read()
        stderr = process.stderr.read()
        exit_code = process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()
        self.assertIn(exit_code, {0, 4})
        self.assertEqual(stderr, b"")
        self.assertEqual(stdout.count(b"\n"), 1)
        self.assertIn("ok", json.loads(stdout))

    def test_missing_current_directory_returns_one_internal_error(self) -> None:
        program = r"""
from io import BytesIO
import json
import os
import sys
import tempfile
from dyro.bridge.transport import run

directory = tempfile.mkdtemp()
os.chdir(directory)
os.rmdir(directory)
request = json.dumps({
    "protocol": {"major": 1, "minor": 0},
    "client": {"name": "test", "version": "1"},
    "operation": "bridge.hello",
    "input": {},
}).encode()
raise SystemExit(run(BytesIO(request), sys.stdout.buffer))
"""
        completed = subprocess.run(
            (sys.executable, "-c", program),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(completed.returncode, 3)
        self.assertEqual(completed.stderr, b"")
        self.assertEqual(completed.stdout.count(b"\n"), 1)
        response = json.loads(completed.stdout)
        self.assertEqual(response["error"]["code"], ErrorCode.INTERNAL_ERROR.value)

    def test_request_id_is_echoed_only_when_safe(self) -> None:
        response, _ = handle_request_bytes(_request(request_id="safe:1"), _context())
        self.assertEqual(response["meta"]["request_id"], "safe:1")
        response, _ = handle_request_bytes(
            _request(request_id="Bearer abcdefghijklmnop"), _context()
        )
        self.assertIsNone(response["meta"]["request_id"])
        self.assertEqual(response["warnings"][0]["code"], "REQUEST_ID_REDACTED")

    def test_invalid_core_output_fails_closed(self) -> None:
        service_id = CATALOG.get("bridge.hello").service_id
        context = _context(handlers={service_id: lambda *_: {"path": "/tmp/leak"}})
        self.assert_error(_request(), ErrorCode.INTERNAL_ERROR, 3, context=context)

    def test_response_limit_replaces_success_without_cutting_json(self) -> None:
        with patch("dyro.bridge.transport.MAX_RESPONSE_BYTES", 64):
            response, exit_code = handle_request_bytes(_request(), _context())
        self.assertEqual(exit_code, 3)
        self.assertEqual(
            response["error"]["code"], ErrorCode.RESOURCE_LIMIT_EXCEEDED.value
        )
        self.assertTrue(response["meta"]["truncated"])

    def test_run_emits_exactly_one_compact_json_line(self) -> None:
        request_stream = BytesIO(_request())
        output = BytesIO()
        exit_code = run(request_stream, output, _context())
        self.assertEqual(exit_code, 0)
        self.assertTrue(request_stream.closed)
        payload = output.getvalue()
        self.assertEqual(payload.count(b"\n"), 1)
        self.assertTrue(payload.endswith(b"\n"))
        self.assertTrue(json.loads(payload)["ok"])

    def test_broken_stdout_exits_five_without_retry(self) -> None:
        output = _BrokenWriter()
        self.assertEqual(run(BytesIO(_request()), output, _context()), 5)
        self.assertEqual(output.calls, 1)

        closed = BytesIO()
        closed.close()
        self.assertEqual(run(BytesIO(_request()), closed, _context()), 5)

        partial = _PartialThenClosedWriter()
        self.assertEqual(run(BytesIO(_request()), partial, _context()), 5)
        self.assertEqual(partial.calls, 2)

    def test_transport_has_no_dynamic_import_cli_or_shell_surface(self) -> None:
        path = Path(__file__).parents[1] / "src" / "dyro" / "bridge" / "transport.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imports.update(
            name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for name in ((node.module or ""), *(alias.name for alias in node.names))
        )
        forbidden = {"argparse", "importlib", "subprocess", "dyro.cli", "cli"}
        self.assertFalse(
            any(
                name == blocked or name.startswith(blocked + ".")
                for name in imports
                for blocked in forbidden
            )
        )
        self.assertFalse(
            any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "__import__"
                for node in ast.walk(tree)
            )
        )


class BridgeTransportPlanTests(WorkspaceCase):
    def setUp(self) -> None:
        super().setUp()
        config = load(self.root)
        create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        task_dir = config.task_specs_dir / "TASK-A"
        task_dir.mkdir(parents=True)
        task_dir.joinpath("task.toml").write_text(
            task_template("TASK-A", "Release", "alpha", "api", "services/api"),
            encoding="utf-8",
        )
        create_objective(
            config,
            """schema_version = 1
id = "release"
title = "Release"
line = "alpha"
targets = ["TASK-A"]

[continuation]
requested_mode = "supervised"
operations = ["execute", "review"]

[budget]
max_actions = 10
max_attempts_per_task = 2
max_failures = 2
max_no_progress_cycles = 2
max_parallel = 1
""",
        )

    def test_plan_crosses_transport_without_digest_rewrite(self) -> None:
        response, exit_code = handle_request_bytes(
            _request(
                "objective.plan",
                {"objective_id": "release", "workspace": None, "start": str(self.root)},
            ),
            _context(cwd=self.root),
        )
        self.assertEqual(exit_code, 0)
        self.assertTrue(response["ok"])
        self.assertEqual(response["data"]["authorization"], "none")
        self.assertRegex(response["data"]["plan_sha256"], r"^sha256:[0-9a-f]{64}$")

    def test_transport_recomputes_plan_digest_before_success(self) -> None:
        plan = plan_objective(
            objective_id="release",
            workspace=None,
            start=self.root,
            cwd=self.root,
        ).as_dict()
        plan["plan_sha256"] = "sha256:" + "0" * 64
        service_id = CATALOG.get("objective.plan").service_id
        response, exit_code = handle_request_bytes(
            _request(
                "objective.plan",
                {"objective_id": "release", "workspace": None, "start": str(self.root)},
            ),
            _context(cwd=self.root, handlers={service_id: lambda *_: plan}),
        )
        self.assertEqual(exit_code, 3)
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], ErrorCode.INTERNAL_ERROR.value)


if __name__ == "__main__":
    unittest.main()

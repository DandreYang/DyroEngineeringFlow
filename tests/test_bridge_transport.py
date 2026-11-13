from __future__ import annotations

from io import BytesIO
import json
from unittest.mock import patch

from dyro.bridge.catalog import EXCLUDED_OPERATION_IDS, build_default_catalog
from dyro.bridge.models import Availability
from dyro.bridge.parse import MAX_NODES, MAX_REQUEST_BYTES, load_bounded_json
from dyro.bridge.transport import handle_request, serve_once
from dyro.config import load
from dyro.continuation.store import create_objective
from dyro.tasks import task_template
from dyro.workspace import create_line

from .support import WorkspaceCase


def _request(
    operation: str,
    payload: dict[str, object] | None = None,
    *,
    major: int = 1,
    minor: int = 0,
    request_id: str | None = "client-1",
    extra: dict[str, object] | None = None,
) -> bytes:
    body: dict[str, object] = {
        "protocol": {"major": major, "minor": minor},
        "client": {"name": "test", "version": "0.0.1"},
        "operation": operation,
        "input": {} if payload is None else payload,
    }
    if request_id is not None:
        body["request_id"] = request_id
    if extra:
        body.update(extra)
    return json.dumps(body, separators=(",", ":")).encode("utf-8")


class _BrokenStdout(BytesIO):
    def write(self, data: bytes) -> int:  # type: ignore[override]
        raise BrokenPipeError()


class BridgeTransportTests(WorkspaceCase):
    def setUp(self) -> None:
        super().setUp()
        self.cwd = self.root

    def _handle(self, raw: bytes, *, exposure: str = "testable"):
        return handle_request(raw, cwd=self.cwd, exposure=exposure)

    def test_public_exposure_stays_unavailable_off_linux(self) -> None:
        code, payload = handle_request(
            _request("bridge.hello"),
            cwd=self.cwd,
            exposure="public",
            platform="darwin",
        )
        self.assertEqual(code, 4)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "OPERATION_UNAVAILABLE")
        self.assertEqual(payload["meta"]["requested_protocol"], {"major": 1, "minor": 0})

    def test_linux_public_exposes_mandatory_but_not_line_list(self) -> None:
        hello = handle_request(
            _request("bridge.hello"),
            cwd=self.cwd,
            exposure="public",
            platform="linux",
        )
        self.assertEqual(hello[0], 0)
        self.assertTrue(hello[1]["ok"])
        hidden = handle_request(
            _request("line.list", {"start": "."}),
            cwd=self.cwd,
            exposure="public",
            platform="linux",
        )
        self.assertEqual(hidden[0], 4)
        self.assertEqual(hidden[1]["error"]["code"], "OPERATION_UNAVAILABLE")

    def test_testable_hello_is_one_json_object(self) -> None:
        stdout = BytesIO()
        stdin = BytesIO(_request("bridge.hello"))
        exit_code = serve_once(stdin, stdout, cwd=self.cwd, exposure="testable")
        raw = stdout.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertTrue(raw.endswith(b"\n"))
        self.assertEqual(raw.count(b"\n"), 1)
        payload = json.loads(raw.decode("utf-8"))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["bridge_version"], "1.0")
        self.assertEqual(payload["data"]["protocol"], {"major": 1, "minor": 0})
        self.assertNotIn("\x1b", raw.decode("utf-8"))

    def test_parse_failures_happen_before_core_access(self) -> None:
        with patch(
            "dyro.bridge.transport.resolve_workspace_readonly",
            side_effect=AssertionError("core"),
        ), patch(
            "dyro.bridge.transport.resolve_workspace_observation",
            side_effect=AssertionError("core"),
        ):
            oversize = self._handle(b"{" + (b"x" * (MAX_REQUEST_BYTES + 8)))
            self.assertEqual(oversize[1]["error"]["code"], "REQUEST_TOO_LARGE")
            duplicate = self._handle(b'{"a":1,"a":2}')
            self.assertEqual(duplicate[1]["error"]["code"], "INVALID_JSON")
            trailing = self._handle(b'{"a":1}{"b":2}')
            self.assertEqual(trailing[1]["error"]["code"], "INVALID_JSON")
            invalid_utf8 = self._handle(b"\xff\xfe")
            self.assertEqual(invalid_utf8[1]["error"]["code"], "INVALID_JSON")
            self.assertIsNone(oversize[1]["meta"]["operation"])
            self.assertIsNone(oversize[1]["meta"]["requested_protocol"])

    def test_deep_and_numerous_structures_fail_closed(self) -> None:
        deep = "[" * 65 + "]" * 65
        code, payload = self._handle(deep.encode("utf-8"))
        self.assertEqual(payload["error"]["code"], "INVALID_JSON")
        self.assertEqual(code, 2)
        many = "[" + ",".join("1" for _ in range(MAX_NODES)) + "]"
        self.assertEqual(self._handle(many.encode("utf-8"))[1]["error"]["code"], "INVALID_JSON")
        long_number = b"1" * 129
        self.assertEqual(self._handle(long_number)[1]["error"]["code"], "INVALID_JSON")
        surrogate = b'{"x":"\\uD800"}'
        self.assertEqual(self._handle(surrogate)[1]["error"]["code"], "INVALID_JSON")

    def test_schema_and_protocol_fail_closed(self) -> None:
        unknown = self._handle(_request("objective.apply"))
        self.assertEqual(unknown[1]["error"]["code"], "OPERATION_UNKNOWN")
        self.assertIn("objective.apply", EXCLUDED_OPERATION_IDS)
        extra_field = self._handle(_request("bridge.hello", extra={"apply": True}))
        self.assertEqual(extra_field[1]["error"]["code"], "SCHEMA_VALIDATION_FAILED")
        mutation_input = self._handle(_request("bridge.hello", {"dry_run": True}))
        self.assertEqual(mutation_input[1]["error"]["code"], "SCHEMA_VALIDATION_FAILED")
        major = self._handle(_request("bridge.hello", major=2))
        self.assertEqual(major[1]["error"]["code"], "PROTOCOL_MAJOR_UNSUPPORTED")
        minor = self._handle(_request("bridge.hello", minor=1))
        self.assertEqual(minor[1]["error"]["code"], "PROTOCOL_MINOR_UNSUPPORTED")
        tilde = self._handle(_request("workspace.resolve", {"start": "~/project"}))
        self.assertEqual(tilde[1]["error"]["code"], "SCHEMA_VALIDATION_FAILED")

    def test_request_id_redaction_and_broken_pipe(self) -> None:
        code, payload = self._handle(_request("bridge.hello", request_id="/tmp/secret"))
        self.assertEqual(code, 0)
        self.assertIsNone(payload["meta"]["request_id"])
        self.assertEqual(payload["warnings"][0]["code"], "REQUEST_ID_REDACTED")
        exit_code = serve_once(
            BytesIO(_request("bridge.hello")),
            _BrokenStdout(),
            cwd=self.cwd,
            exposure="testable",
        )
        self.assertEqual(exit_code, 5)

    def test_malformed_local_profile_does_not_fall_back(self) -> None:
        (self.root / "dyro.toml").write_text("not valid = [", encoding="utf-8")
        code, payload = self._handle(_request("workspace.resolve", {"start": "."}))
        self.assertEqual(code, 3)
        self.assertEqual(payload["error"]["code"], "LOCAL_PROFILE_INVALID")
        self.assertNotIn(str(self.root.resolve()), json.dumps(payload))

    def test_testable_plan_stays_non_executable(self) -> None:
        config = load(self.root)
        create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        directory = config.task_specs_dir / "TASK-A"
        directory.mkdir(parents=True)
        directory.joinpath("task.toml").write_text(
            task_template("TASK-A", "Task A", "alpha", "api", "services/api").replace(
                'agent = "codex"', 'agent = "noop"'
            ),
            encoding="utf-8",
        )
        directory.joinpath("handoff.md").write_text("# handoff\n", encoding="utf-8")
        create_objective(
            config,
            '''schema_version = 1
id = "release"
title = "Release"
line = "alpha"
targets = ["TASK-A"]

[continuation]
requested_mode = "supervised"
operations = ["execute"]
''',
        )
        code, payload = self._handle(
            _request("objective.plan", {"start": ".", "objective_id": "release"})
        )
        self.assertEqual(code, 0)
        self.assertIs(payload["data"]["executable"], False)
        self.assertEqual(payload["data"]["authorization"], "none")
        self.assertEqual(payload["meta"]["planner_revision"], "objective-plan/1")
        self.assertNotIn(str(self.root.resolve()), json.dumps(payload))

    def test_catalog_public_surface_is_linux_only(self) -> None:
        darwin = build_default_catalog(platform="darwin")
        linux = build_default_catalog(platform="linux")
        self.assertFalse(
            any(item.availability is Availability.PUBLIC_AVAILABLE for item in darwin.operations)
        )
        self.assertEqual(
            {
                item.id
                for item in linux.operations
                if item.availability is Availability.PUBLIC_AVAILABLE
            },
            {
                "bridge.hello",
                "bridge.capabilities.compact",
                "bridge.operation.schema",
                "workspace.resolve",
                "workspace.list",
                "workspace.observe",
                "objective.plan",
            },
        )

    def test_parser_accepts_empty_object(self) -> None:
        self.assertEqual(load_bounded_json(b"{}"), {})

from __future__ import annotations

from contextlib import ExitStack
from io import BytesIO
import json
from unittest.mock import patch

from dyro.bridge.transport import handle_request, serve_once

from .support import WorkspaceCase


class _Effect(AssertionError):
    pass


def _request(operation: str, payload: dict[str, object] | None = None) -> bytes:
    return json.dumps(
        {
            "protocol": {"major": 1, "minor": 0},
            "client": {"name": "zero-effect", "version": "0.0.1"},
            "operation": operation,
            "input": {} if payload is None else payload,
        },
        separators=(",", ":"),
    ).encode("utf-8")


class BridgeZeroEffectTests(WorkspaceCase):
    def _traps(self):
        return (
            patch("subprocess.Popen", side_effect=_Effect("popen")),
            patch("subprocess.run", side_effect=_Effect("run")),
            patch("socket.socket", side_effect=_Effect("socket")),
            patch("os.replace", side_effect=_Effect("replace")),
            patch("os.rename", side_effect=_Effect("rename")),
            patch("os.remove", side_effect=_Effect("remove")),
        )

    def test_linux_public_hello_does_not_write_or_spawn(self) -> None:
        with ExitStack() as stack:
            for trap in self._traps():
                stack.enter_context(trap)
            code, payload = handle_request(
                _request("bridge.hello"),
                cwd=self.root,
                exposure="public",
                platform="linux",
            )
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["bridge_version"], "1.0")

    def test_linux_public_capabilities_do_not_claim_line_list(self) -> None:
        code, payload = handle_request(
            _request("bridge.capabilities.compact"),
            cwd=self.root,
            exposure="public",
            platform="linux",
        )
        self.assertEqual(code, 0)
        available = {
            item["id"]: item["availability"] for item in payload["data"]["operations"]
        }
        self.assertEqual(available["bridge.hello"], "public_available")
        self.assertEqual(available["line.list"], "implemented_testable")
        self.assertNotIn("objective.apply", available)

    def test_serve_once_writes_only_stdout(self) -> None:
        stdout = BytesIO()
        before = {path.relative_to(self.root) for path in self.root.rglob("*")}
        exit_code = serve_once(
            BytesIO(_request("bridge.hello")),
            stdout,
            cwd=self.root,
            exposure="public",
            platform="linux",
        )
        after = {path.relative_to(self.root) for path in self.root.rglob("*")}
        self.assertEqual(exit_code, 0)
        self.assertEqual(before, after)
        self.assertTrue(stdout.getvalue().endswith(b"\n"))

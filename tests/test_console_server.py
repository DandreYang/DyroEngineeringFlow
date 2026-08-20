from __future__ import annotations

from http.client import HTTPConnection
import json
import socket
from threading import Thread
import unittest
from unittest.mock import Mock

from dyro.console.inspection import IsolatedOverviewService
from dyro.console.server import create_console_http_server


class ConsoleServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = create_console_http_server(port=0, bootstrap_secret="a" * 43)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port
        self.origin = f"http://127.0.0.1:{self.port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        connection = HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = response.read()
        result = response.status, dict(response.getheaders()), payload
        connection.close()
        return result

    def _exchange(self, secret: str = "a" * 43) -> str:
        status, headers, body = self._request(
            "POST",
            "/api/v1/session",
            body=json.dumps({"bootstrap": secret}).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Origin": self.origin,
                "Sec-Fetch-Site": "same-origin",
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(headers.get("Set-Cookie"), None)
        return json.loads(body)["bearer"]

    def test_static_shell_has_no_project_data_or_bootstrap_secret(self) -> None:
        status, headers, body = self._request("GET", "/")

        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Security-Policy"], "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; worker-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        self.assertEqual(headers["Cross-Origin-Opener-Policy"], "same-origin")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("a" * 43, body.decode("utf-8"))
        self.assertNotIn("dyro.toml", body.decode("utf-8"))

    def test_bootstrap_is_same_origin_single_use_and_issues_independent_bearer(self) -> None:
        rejected, _, _ = self._request(
            "POST",
            "/api/v1/session",
            body=json.dumps({"bootstrap": "a" * 43}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Origin": "http://example.test"},
        )
        self.assertEqual(rejected, 403)

        bearer = self._exchange()
        self.assertNotEqual(bearer, "a" * 43)
        replayed, _, _ = self._request(
            "POST",
            "/api/v1/session",
            body=json.dumps({"bootstrap": "a" * 43}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Origin": self.origin},
        )
        self.assertEqual(replayed, 401)

    def test_api_requires_exact_host_authorization_and_origin(self) -> None:
        unauthorized, _, body = self._request("GET", "/api/v1/meta")
        self.assertEqual(unauthorized, 401)
        self.assertEqual(json.loads(body)["error"]["code"], "UNAUTHORIZED")

        bearer = self._exchange()
        spoofed, _, _ = self._request(
            "GET",
            "/api/v1/meta",
            headers={"Host": "localhost", "Authorization": f"Bearer {bearer}"},
        )
        self.assertEqual(spoofed, 400)
        cross_origin, _, _ = self._request(
            "GET",
            "/api/v1/meta",
            headers={"Authorization": f"Bearer {bearer}", "Origin": "http://example.test"},
        )
        self.assertEqual(cross_origin, 403)

        status, headers, body = self._request(
            "GET",
            "/api/v1/meta",
            headers={"Authorization": f"Bearer {bearer}", "Origin": self.origin},
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        payload = json.loads(body)
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["data"]["surfaces"], ["overview", "proofs", "system", "events"])
        self.assertEqual(payload["data"]["capabilities"], ["overview", "proofs", "system", "events"])
        self.assertEqual(payload["data"]["initial_workspace"], "")
        self.assertIn("session_expires_at", payload["data"])

    def test_api_refuses_cors_preflight_mutations_and_invalid_request_framing(self) -> None:
        for method in ("OPTIONS", "PUT", "DELETE", "PATCH"):
            status, headers, _ = self._request(method, "/api/v1/meta")
            self.assertEqual(status, 405)
            self.assertNotIn("Access-Control-Allow-Origin", headers)

        spoofed, _, _ = self._request(
            "OPTIONS", "/api/v1/meta", headers={"Host": "localhost"}
        )
        self.assertEqual(spoofed, 400)

        body = json.dumps({"bootstrap": "a" * 43}).encode("utf-8")
        status, _, _ = self._request(
            "POST",
            "/api/v1/session",
            body=body,
            headers={
                "Content-Type": "application/json",
                "Origin": self.origin,
                "Transfer-Encoding": "chunked",
            },
        )
        self.assertEqual(status, 400)

    def test_server_uses_loopback_only_and_rejects_invalid_ports(self) -> None:
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        self.assertIsInstance(self.server.overview_service, IsolatedOverviewService)
        with self.assertRaises(ValueError):
            create_console_http_server(port=-1)
        with self.assertRaises(ValueError):
            create_console_http_server(port=65536)

    def test_request_line_limit_fails_closed_before_stdlib_parsing(self) -> None:
        client = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            client.sendall(b"GET /" + b"a" * 5000 + b" HTTP/1.1\r\nHost: ignored\r\n\r\n")
            chunks: list[bytes] = []
            while True:
                chunk = client.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            client.close()
        response = b"".join(chunks)

        self.assertIn(b" 400 ", response)
        self.assertIn(b"Connection: close", response)

    def test_stdlib_parse_errors_are_sanitized(self) -> None:
        client = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            client.sendall(b"GET / HTTP/1.1\r\nBroken-Header\r\n\r\n")
            chunks: list[bytes] = []
            while True:
                chunk = client.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            client.close()
        response = b"".join(chunks)

        self.assertIn(b" 400 ", response)
        self.assertIn(b'"code":"BAD_REQUEST"', response)
        self.assertNotIn(b"Broken-Header", response)

    def test_header_limits_and_obs_fold_fail_before_application_routing(self) -> None:
        cases = (
            b"GET / HTTP/1.1\r\nHost: ignored\r\n X-Folded: value\r\n\r\n",
            b"GET / HTTP/1.1\r\nHost: ignored\r\nX-Large: " + b"a" * 5000 + b"\r\n\r\n",
        )
        for request in cases:
            with self.subTest(request=request[:24]):
                client = socket.create_connection(("127.0.0.1", self.port), timeout=5)
                try:
                    client.sendall(request)
                    chunks: list[bytes] = []
                    while True:
                        chunk = client.recv(4096)
                        if not chunk:
                            break
                        chunks.append(chunk)
                finally:
                    client.close()
                response = b"".join(chunks)
                self.assertIn(b"BAD_REQUEST", response)

    def test_each_response_closes_a_pipelined_http11_connection(self) -> None:
        client = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        request = (
            f"GET /api/v1/meta HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n\r\n"
            f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n\r\n"
        ).encode("ascii")
        try:
            client.sendall(request)
            chunks: list[bytes] = []
            while True:
                chunk = client.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            client.close()
        response = b"".join(chunks)

        self.assertIn(b" 401 ", response)
        self.assertIn(b"Connection: close", response)
        self.assertNotIn(b"<h1>Dyro Console</h1>", response)


class ConsoleOverviewServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.overview = Mock()
        self.overview.page.return_value = {
            "schema_version": 1,
            "captured_at": "2026-08-04T12:00:00+00:00",
            "snapshot_sha256": "f" * 64,
            "freshness": {"state": "fresh", "partial": False, "warnings": []},
            "data": {"workspaces": [], "next_cursor": None},
        }
        self.overview.workspace.return_value = {
            "schema_version": 1,
            "captured_at": "2026-08-04T12:00:00+00:00",
            "snapshot_sha256": "e" * 64,
            "freshness": {"state": "partial", "partial": True, "warnings": []},
            "data": {"workspace": {"alias": "alpha", "availability": "available"}},
        }
        self.overview.system.return_value = {
            "schema_version": 1,
            "captured_at": "2026-08-04T12:00:00+00:00",
            "snapshot_sha256": "c" * 64,
            "freshness": {"state": "fresh", "partial": False, "warnings": []},
            "data": {
                "tool_inspection": "not_inspected",
                "tools": [],
                "update": {
                    "check_enabled": True,
                    "last_checked_on": "2026-08-16",
                    "latest_version": "",
                    "kind": "none",
                },
            },
        }
        self.overview.inspect_proofs.return_value = {
            "schema_version": 1,
            "captured_at": "2026-08-04T12:00:00+00:00",
            "snapshot_sha256": "d" * 64,
            "freshness": {"state": "fresh", "partial": False, "warnings": []},
            "data": {"proof_inspection": "inspected", "proofs": [], "objectives": []},
        }
        self.overview.events.return_value = {
            "schema_version": 1,
            "captured_at": "2026-08-04T12:00:00+00:00",
            "snapshot_sha256": "b" * 64,
            "freshness": {"state": "fresh", "partial": False, "warnings": []},
            "data": {
                "events": [
                    {
                        "seq": 1,
                        "id": "evt_1",
                        "kind": "spawn",
                        "at": "2026-08-20T12:00:00Z",
                        "actor": "core",
                        "subject": "core_pay",
                        "family": "core",
                        "facts": {"parent": "core", "child": "core_pay"},
                    }
                ],
                "next_cursor": "after_evt_1",
            },
        }
        self.overview.families.return_value = {
            "schema_version": 1,
            "captured_at": "2026-08-04T12:00:00+00:00",
            "snapshot_sha256": "a" * 64,
            "freshness": {"state": "fresh", "partial": False, "warnings": []},
            "data": {"families": []},
        }
        self.overview.family.return_value = {
            "schema_version": 1,
            "captured_at": "2026-08-04T12:00:00+00:00",
            "snapshot_sha256": "9" * 64,
            "freshness": {"state": "fresh", "partial": False, "warnings": []},
            "data": {
                "parent": "core",
                "members": ["core", "core_pay", "operator"],
                "nodes": [],
                "edges": [],
            },
        }
        self.server = create_console_http_server(
            port=0,
            bootstrap_secret="a" * 43,
            overview_service=self.overview,
        )
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port
        self.origin = f"http://127.0.0.1:{self.port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _request(
        self, method: str, path: str, *, body: bytes | None = None, headers: dict[str, str] | None = None
    ) -> tuple[int, dict[str, str], bytes]:
        connection = HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def _bearer(self) -> str:
        status, _, body = self._request(
            "POST",
            "/api/v1/session",
            body=json.dumps({"bootstrap": "a" * 43}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Origin": self.origin},
        )
        self.assertEqual(status, 201)
        return json.loads(body)["bearer"]

    def test_authenticated_overview_uses_strict_query_and_conditional_etag(self) -> None:
        bearer = self._bearer()
        headers = {"Authorization": f"Bearer {bearer}", "Origin": self.origin}

        status, response_headers, body = self._request(
            "GET", "/api/v1/overview?limit=1", headers=headers
        )
        self.assertEqual(status, 200)
        self.assertEqual(response_headers["ETag"], '"' + "f" * 64 + '"')
        self.assertEqual(json.loads(body)["data"]["workspaces"], [])
        self.overview.page.assert_called_once_with(cursor=None, limit=1)

        cached, cached_headers, cached_body = self._request(
            "GET",
            "/api/v1/overview?limit=1",
            headers={**headers, "If-None-Match": response_headers["ETag"]},
        )
        self.assertEqual(cached, 304)
        self.assertEqual(cached_headers["ETag"], response_headers["ETag"])
        self.assertEqual(cached_body, b"")

        invalid, _, body = self._request(
            "GET", "/api/v1/overview?limit=1&limit=2", headers=headers
        )
        self.assertEqual(invalid, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "OVERVIEW_QUERY_INVALID")

    def test_workspace_summary_is_authenticated_alias_only_and_cacheable(self) -> None:
        bearer = self._bearer()
        headers = {"Authorization": f"Bearer {bearer}", "Origin": self.origin}

        status, response_headers, body = self._request(
            "GET", "/api/v1/workspaces/alpha", headers=headers
        )
        self.assertEqual(status, 200)
        self.assertEqual(response_headers["ETag"], '"' + "e" * 64 + '"')
        self.assertEqual(json.loads(body)["data"]["workspace"]["alias"], "alpha")
        self.overview.workspace.assert_called_once_with("alpha")

        unsafe, _, unsafe_body = self._request(
            "GET", "/api/v1/workspaces/%2fprivate", headers=headers
        )
        self.assertEqual(unsafe, 400)
        self.assertEqual(
            json.loads(unsafe_body)["error"]["code"], "WORKSPACE_ALIAS_INVALID"
        )

    def test_proof_inspect_is_authenticated_get_only(self) -> None:
        unauthorized, _, body = self._request("GET", "/api/v1/workspaces/alpha/proofs")
        self.assertEqual(unauthorized, 401)
        self.assertEqual(json.loads(body)["error"]["code"], "UNAUTHORIZED")

        rejected, _, rejected_body = self._request("POST", "/api/v1/workspaces/alpha/proofs")
        self.assertEqual(rejected, 405)
        self.assertEqual(json.loads(rejected_body)["error"]["code"], "METHOD_NOT_ALLOWED")
        self.overview.inspect_proofs.assert_not_called()

        bearer = self._bearer()
        headers = {"Authorization": f"Bearer {bearer}", "Origin": self.origin}
        status, response_headers, body = self._request(
            "GET", "/api/v1/workspaces/alpha/proofs", headers=headers
        )
        self.assertEqual(status, 200)
        self.assertEqual(response_headers["ETag"], '"' + "d" * 64 + '"')
        self.assertEqual(json.loads(body)["data"]["proof_inspection"], "inspected")
        self.overview.inspect_proofs.assert_called_once_with("alpha")
        self.overview.workspace.assert_not_called()

    def test_system_is_authenticated_get_only(self) -> None:
        unauthorized, _, body = self._request("GET", "/api/v1/system")
        self.assertEqual(unauthorized, 401)
        self.assertEqual(json.loads(body)["error"]["code"], "UNAUTHORIZED")

        rejected, _, rejected_body = self._request("POST", "/api/v1/system")
        self.assertEqual(rejected, 405)
        self.assertEqual(json.loads(rejected_body)["error"]["code"], "METHOD_NOT_ALLOWED")
        self.overview.system.assert_not_called()

        bearer = self._bearer()
        headers = {"Authorization": f"Bearer {bearer}", "Origin": self.origin}
        status, response_headers, body = self._request("GET", "/api/v1/system", headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(response_headers["ETag"], '"' + "c" * 64 + '"')
        self.assertEqual(json.loads(body)["data"]["tool_inspection"], "not_inspected")
        self.assertEqual(json.loads(body)["data"]["tools"], [])
        self.overview.system.assert_called_once_with()

    def test_events_after_query_and_sse_resume_are_authenticated(self) -> None:
        unauthorized, _, body = self._request("GET", "/api/v1/workspaces/alpha/events")
        self.assertEqual(unauthorized, 401)
        self.assertEqual(json.loads(body)["error"]["code"], "UNAUTHORIZED")

        rejected, _, rejected_body = self._request("POST", "/api/v1/workspaces/alpha/events")
        self.assertEqual(rejected, 405)
        self.assertEqual(json.loads(rejected_body)["error"]["code"], "METHOD_NOT_ALLOWED")

        bearer = self._bearer()
        headers = {"Authorization": f"Bearer {bearer}", "Origin": self.origin}
        status, _, body = self._request(
            "GET", "/api/v1/workspaces/alpha/events?after=after_evt_1&limit=50", headers=headers
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["data"]["events"][0]["kind"], "spawn")
        self.overview.events.assert_called_with("alpha", after="after_evt_1", limit=50)

        stream, stream_headers, stream_body = self._request(
            "GET", "/api/v1/workspaces/alpha/events/stream?after=after_evt_1", headers=headers
        )
        self.assertEqual(stream, 200)
        self.assertEqual(stream_headers["Content-Type"], "text/event-stream; charset=utf-8")
        self.assertIn(b"data: ", stream_body)
        self.assertIn(b'"kind":"spawn"', stream_body)
        self.assertIn(b"id: after_evt_1", stream_body)
        self.assertIn(b": keepalive", stream_body)

        localhost, _, _ = self._request(
            "GET",
            "/api/v1/workspaces/alpha/events",
            headers={"Host": "localhost", "Authorization": f"Bearer {bearer}"},
        )
        self.assertEqual(localhost, 400)

        channel, _, channel_body = self._request(
            "POST", "/api/v1/workspaces/alpha/families/core/channel", headers=headers
        )
        self.assertEqual(channel, 405)
        self.assertEqual(json.loads(channel_body)["error"]["code"], "METHOD_NOT_ALLOWED")

    def test_family_graph_is_authenticated_get_only(self) -> None:
        bearer = self._bearer()
        headers = {"Authorization": f"Bearer {bearer}", "Origin": self.origin}
        status, _, body = self._request(
            "GET", "/api/v1/workspaces/alpha/families/core", headers=headers
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["data"]["parent"], "core")
        self.overview.family.assert_called_once_with("alpha", "core")


if __name__ == "__main__":
    unittest.main()

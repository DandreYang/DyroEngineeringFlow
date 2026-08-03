from __future__ import annotations

from http.client import HTTPConnection
import json
import socket
from threading import Thread
import unittest

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
        self.assertEqual(payload["data"]["capabilities"], [])
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


if __name__ == "__main__":
    unittest.main()

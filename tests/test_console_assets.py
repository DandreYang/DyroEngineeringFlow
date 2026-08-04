from __future__ import annotations

from http.client import HTTPConnection
from threading import Thread
import unittest

from dyro.console.assets import load_asset, validate_assets
from dyro.console.server import create_console_http_server


class ConsoleAssetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = create_console_http_server(port=0, bootstrap_secret="a" * 43)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _request(self, path: str) -> tuple[int, dict[str, str], bytes]:
        connection = HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request("GET", path)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def test_manifest_assets_are_packaged_and_safe_for_static_serving(self) -> None:
        validate_assets()
        shell = load_asset("index.html")
        script = load_asset("app.js")

        self.assertEqual(shell.content_type, "text/html; charset=utf-8")
        self.assertEqual(script.content_type, "text/javascript; charset=utf-8")
        self.assertIn(b'type="module"', shell.body)
        self.assertNotIn(b"innerHTML", script.body)
        self.assertNotIn(b"http://", script.body)
        self.assertNotIn(b"https://", script.body)

    def test_public_assets_are_fixed_manifest_entries_and_reject_paths(self) -> None:
        status, headers, body = self._request("/assets/app.js")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/javascript; charset=utf-8")
        self.assertIn(b"sessionStorage", body)
        self.assertEqual(headers["Cache-Control"], "no-store")

        for path in ("/assets/unknown.js", "/assets/../app.js", "/assets/%2e%2e/app.js"):
            with self.subTest(path=path):
                rejected, _, _ = self._request(path)
                self.assertEqual(rejected, 404)

    def test_static_shell_has_no_bootstrap_secret_or_workspace_data(self) -> None:
        status, _, body = self._request("/")

        self.assertEqual(status, 200)
        self.assertNotIn(b"a" * 43, body)
        self.assertNotIn(b"dyro.toml", body)
        self.assertNotIn(b"bootstrap=", body)


if __name__ == "__main__":
    unittest.main()

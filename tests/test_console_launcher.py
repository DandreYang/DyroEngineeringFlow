from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from dyro.cli import main
from dyro.console.launcher import launch_console
from dyro.errors import DyroError


class _SessionStore:
    bootstrap_secret = "a" * 43


class _Server:
    origin = "http://127.0.0.1:43123"

    def __init__(self) -> None:
        self.sessions = _SessionStore()
        self.closed = False

    def shutdown(self) -> None:
        return

    def server_close(self) -> None:
        self.closed = True


class ConsoleLauncherTests(unittest.TestCase):
    def _launch(
        self,
        *,
        no_open: bool = False,
        browser_result: bool = True,
        initial_workspace: str | None = None,
        target_root: Path | None = None,
    ) -> tuple[_Server, str, Mock, Mock]:
        server = _Server()
        factory = Mock(return_value=server)
        browser = Mock(return_value=browser_result)
        output = StringIO()
        with redirect_stdout(output):
            launch_console(
                port=0,
                no_open=no_open,
                initial_workspace=initial_workspace,
                target_root=target_root,
                browser_open=browser,
                server_factory=factory,
                serve=lambda _: None,
            )
        return server, output.getvalue(), factory, browser

    def test_auto_open_keeps_bootstrap_secret_out_of_terminal_output(self) -> None:
        server, output, factory, browser = self._launch(initial_workspace="demo")

        self.assertTrue(server.closed)
        self.assertEqual(factory.call_args.kwargs["port"], 0)
        self.assertEqual(factory.call_args.kwargs["initial_workspace"], "demo")
        self.assertEqual(
            browser.call_args.args[0],
            "http://127.0.0.1:43123/#bootstrap=" + "a" * 43 + "&workspace=demo",
        )
        self.assertNotIn("a" * 43, output)
        self.assertIn("127.0.0.1:43123", output)

    def test_no_open_prints_the_one_time_fragment_url(self) -> None:
        _, output, factory, browser = self._launch(no_open=True)

        self.assertIn("#bootstrap=" + "a" * 43, output)
        self.assertIn("单次", output)
        self.assertFalse(factory.call_args is None)
        browser.assert_not_called()

    def test_no_open_flushes_the_one_time_url_so_a_pipe_still_sees_it(self) -> None:
        printed: list[tuple[tuple[object, ...], dict[str, object]]] = []

        def capture(*args: object, **kwargs: object) -> None:
            printed.append((args, kwargs))

        server = _Server()
        with patch("builtins.print", side_effect=capture):
            launch_console(
                port=0,
                no_open=True,
                browser_open=Mock(return_value=True),
                server_factory=Mock(return_value=server),
                serve=lambda _: None,
            )

        url_calls = [
            kwargs
            for args, kwargs in printed
            if args and isinstance(args[0], str) and "#bootstrap=" in args[0]
        ]
        self.assertTrue(url_calls)
        self.assertTrue(all(item.get("flush") is True for item in url_calls))

    def test_failed_browser_open_prints_manual_recovery_url(self) -> None:
        _, output, _, _ = self._launch(browser_result=False)

        self.assertIn("无法自动打开浏览器", output)
        self.assertIn("#bootstrap=" + "a" * 43, output)

    def test_temporary_root_is_composed_into_the_read_only_service(self) -> None:
        root = Path("/tmp/console-target")
        _, _, factory, _ = self._launch(target_root=root)

        service = factory.call_args.kwargs["overview_service"]
        self.assertEqual(service.target_root, root.absolute())

    def test_listener_start_failure_is_sanitized(self) -> None:
        with self.assertRaisesRegex(DyroError, "无法启动本地 loopback listener") as raised:
            launch_console(
                server_factory=Mock(side_effect=OSError("/private/secret")),
                serve=lambda _: None,
            )
        self.assertNotIn("private", str(raised.exception))

    def test_dry_run_neither_binds_nor_opens_a_browser(self) -> None:
        output = StringIO()
        with (
            patch("dyro.console.launcher.create_console_http_server") as server_factory,
            patch("dyro.console.launcher.webbrowser.open") as browser_open,
            redirect_stdout(output),
        ):
            main(["--dry-run", "console", "--no-open"])

        self.assertIn("DRY RUN", output.getvalue())
        self.assertIn("127.0.0.1:0", output.getvalue())
        server_factory.assert_not_called()
        browser_open.assert_not_called()


if __name__ == "__main__":
    unittest.main()

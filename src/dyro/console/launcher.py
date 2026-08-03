"""Foreground lifecycle for the local, read-only Console."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import webbrowser

from ..config import validate_id
from ..errors import DyroError, ValidationError
from .inspection import IsolatedOverviewService
from .server import ConsoleHTTPServer, create_console_http_server


BrowserOpen = Callable[[str], bool | None]
ServerFactory = Callable[..., ConsoleHTTPServer]
Serve = Callable[[ConsoleHTTPServer], None]


def _validate_port(port: int) -> None:
    if type(port) is not int or not 0 <= port <= 65535:
        raise DyroError("Console port 必须是 0 到 65535 之间的整数")


def _validate_workspace(alias: str | None) -> str | None:
    if alias is None:
        return None
    try:
        return validate_id(alias, "工作区别名")
    except ValidationError:
        raise DyroError("Console 工作区别名无效") from None


def _bootstrap_url(server: ConsoleHTTPServer, initial_workspace: str | None) -> str:
    # Both values are URL-safe tokens.  The secret remains in the fragment so
    # it is never sent in an HTTP request, Referer header, or server log.
    fragment = f"bootstrap={server.sessions.bootstrap_secret}"
    if initial_workspace:
        fragment += f"&workspace={initial_workspace}"
    return f"{server.origin}/#{fragment}"


def render_console_plan(
    *,
    port: int,
    no_open: bool,
    initial_workspace: str | None,
    target_root: Path | None,
) -> None:
    """Print the all-read-only plan used by ``dyro --dry-run console``."""
    _validate_port(port)
    initial_workspace = _validate_workspace(initial_workspace)
    print("DRY RUN: 将启动只读本地 Console；未 bind、未生成 secret、未打开浏览器。")
    print(f"监听：127.0.0.1:{port}")
    if target_root is not None:
        print(f"临时只读工作区：{target_root.absolute()}")
    elif initial_workspace:
        print(f"初始焦点：{initial_workspace}")
    else:
        print("初始焦点：当前或默认工作区")
    print("浏览器：不自动打开" if no_open else "浏览器：就绪后自动打开一次性 fragment URL")


def _serve_forever(server: ConsoleHTTPServer) -> None:
    server.serve_forever(poll_interval=0.2)


def launch_console(
    *,
    port: int = 0,
    no_open: bool = False,
    initial_workspace: str | None = None,
    target_root: Path | None = None,
    browser_open: BrowserOpen = webbrowser.open,
    server_factory: ServerFactory = create_console_http_server,
    serve: Serve = _serve_forever,
) -> None:
    """Run the bounded Console server in the foreground until Ctrl-C.

    This is deliberately the only Console component allowed to open a browser.
    It passes a one-time bootstrap secret through a URL fragment, never through
    a query string or terminal output after automatic opening succeeds.
    """
    _validate_port(port)
    initial_workspace = _validate_workspace(initial_workspace)
    if not isinstance(no_open, bool):
        raise DyroError("Console --no-open 参数无效")
    if target_root is not None and not isinstance(target_root, Path):
        raise DyroError("Console 临时工作区路径无效")
    service = (
        IsolatedOverviewService(target_root=target_root.absolute())
        if target_root is not None
        else None
    )
    try:
        server = server_factory(
            port=port,
            overview_service=service,
            initial_workspace=initial_workspace,
        )
    except (OSError, ValueError):
        raise DyroError("Console 无法启动本地 loopback listener") from None
    try:
        url = _bootstrap_url(server, initial_workspace)
        if no_open:
            print("Console 已就绪。以下 URL 单次可用，并在 60 秒后失效：")
            print(url)
        else:
            try:
                opened = browser_open(url)
            except Exception:
                opened = False
            if opened is False:
                print("无法自动打开浏览器。以下 URL 单次可用，并在 60 秒后失效：")
                print(url)
            else:
                print(f"Console 已在 {server.origin} 打开；按 Ctrl-C 停止。")
        try:
            serve(server)
        except KeyboardInterrupt:
            print("Console 已停止。")
    finally:
        try:
            server.shutdown()
        finally:
            server.server_close()

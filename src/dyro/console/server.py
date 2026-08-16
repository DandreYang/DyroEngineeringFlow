"""Loopback-only HTTP boundary for the read-only local Console."""

from __future__ import annotations

from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import socket
import threading
from typing import Any
from urllib.parse import urlsplit

from .. import __version__
from .assets import ConsoleAssetError, load_asset, validate_assets
from .inspection import IsolatedOverviewService
from .overview import ConsoleOverviewError
from .session import ConsoleSessionStore, SessionRejected


HOST = "127.0.0.1"
REQUEST_LINE_LIMIT = 4 * 1024
HEADER_LIMIT = 16 * 1024
HEADER_LINE_LIMIT = 4 * 1024
SESSION_BODY_LIMIT = 512
READ_TIMEOUT_SECONDS = 5.0
REQUEST_DEADLINE_SECONDS = 10.0
MAX_CONCURRENT_REQUESTS = 8
_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "connect-src 'self'; worker-src 'none'; object-src 'none'; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'"
)
_TOKEN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


class ConsoleHTTPServer(ThreadingHTTPServer):
    """A bounded IPv4 loopback server with no request logging."""

    address_family = socket.AF_INET
    daemon_threads = True
    request_queue_size = MAX_CONCURRENT_REQUESTS

    def __init__(
        self,
        *,
        port: int,
        bootstrap_secret: str | None,
        session_store: ConsoleSessionStore | None,
        overview_service: IsolatedOverviewService | None,
        initial_workspace: str | None,
        max_concurrent_requests: int,
    ) -> None:
        try:
            validate_assets()
        except ConsoleAssetError:
            raise ValueError("Console static assets unavailable") from None
        self._request_slots = threading.BoundedSemaphore(max_concurrent_requests)
        self.sessions = session_store
        self.overview_service = overview_service or IsolatedOverviewService()
        if initial_workspace is not None and not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", initial_workspace
        ):
            raise ValueError("Console initial_workspace 无效")
        self.initial_workspace = initial_workspace or ""
        super().__init__((HOST, port), ConsoleRequestHandler)
        if self.sessions is None:
            self.sessions = ConsoleSessionStore(bootstrap_secret=bootstrap_secret)
        self.origin = f"http://{HOST}:{self.server_port}"

    def get_request(self) -> tuple[Any, tuple[str, int]]:
        request, client_address = super().get_request()
        request.settimeout(READ_TIMEOUT_SECONDS)
        return request, client_address

    def process_request(self, request: Any, client_address: tuple[str, int]) -> None:
        if not self._request_slots.acquire(blocking=False):
            request.close()
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request: Any, client_address: tuple[str, int]) -> None:
        timer = threading.Timer(REQUEST_DEADLINE_SECONDS, self._close_request, args=(request,))
        timer.daemon = True
        timer.start()
        try:
            super().process_request_thread(request, client_address)
        finally:
            timer.cancel()
            self._request_slots.release()

    @staticmethod
    def _close_request(request: Any) -> None:
        try:
            request.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            request.close()
        except OSError:
            pass

    def server_close(self) -> None:
        if self.sessions is not None:
            self.sessions.clear()
        super().server_close()

    def handle_error(self, request: Any, client_address: tuple[str, int]) -> None:
        """Never write a traceback, request text, or local paths to stderr."""
        del request, client_address
        return


class ConsoleRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "DyroConsole"
    sys_version = ""

    @property
    def console(self) -> ConsoleHTTPServer:
        return self.server  # type: ignore[return-value]

    def log_message(self, format: str, *args: object) -> None:
        return

    def send_error(
        self,
        code: int,
        message: str | None = None,
        explain: str | None = None,
    ) -> None:
        """Do not let stdlib parser messages reflect raw request material."""
        del message, explain
        self._error(400 if 400 <= code < 500 else 500, "BAD_REQUEST")

    def handle_one_request(self) -> None:
        """Apply the Console request-line ceiling before stdlib parsing.

        ``BaseHTTPRequestHandler`` otherwise accepts a much larger line before
        application code can enforce the documented 4 KiB protocol boundary.
        """
        try:
            self.raw_requestline = self.rfile.readline(REQUEST_LINE_LIMIT + 1)
            if len(self.raw_requestline) > REQUEST_LINE_LIMIT:
                self.requestline = ""
                self.request_version = "HTTP/1.1"
                self.command = ""
                self.close_connection = True
                self._error(400, "BAD_REQUEST")
                return
            if not self.raw_requestline:
                self.close_connection = True
                return
            if not self._parse_console_request():
                return
            method = getattr(self, f"do_{self.command}", None)
            if method is None:
                self._reject_method()
            else:
                method()
            self.wfile.flush()
        except OSError:
            self.close_connection = True

    def _parse_console_request(self) -> bool:
        """Parse only strict HTTP/1.1 origin-form requests within fixed bounds."""
        if not self.raw_requestline.endswith(b"\r\n"):
            self._bad_parse_request()
            return False
        try:
            requestline = self.raw_requestline[:-2].decode("ascii")
        except UnicodeDecodeError:
            self._bad_parse_request()
            return False
        fields = requestline.split(" ")
        if len(fields) != 3 or not all(fields) or not _TOKEN.fullmatch(fields[0]):
            self._bad_parse_request()
            return False
        method, target, version = fields
        if version != "HTTP/1.1" or not target.startswith("/") or target.startswith("//"):
            self._bad_parse_request()
            return False
        self.requestline = requestline
        self.command = method
        self.path = target
        self.request_version = version
        headers = Message()
        total = 0
        while True:
            line = self.rfile.readline(HEADER_LINE_LIMIT + 1)
            if not line or len(line) > HEADER_LINE_LIMIT:
                self._bad_parse_request()
                return False
            total += len(line)
            if total > HEADER_LIMIT:
                self._bad_parse_request()
                return False
            if line == b"\r\n":
                self.headers = headers
                return True
            if not line.endswith(b"\r\n") or line[:1] in {b" ", b"\t"}:
                self._bad_parse_request()
                return False
            raw = line[:-2]
            if any(byte < 0x20 or byte == 0x7F for byte in raw):
                self._bad_parse_request()
                return False
            name, separator, value = raw.partition(b":")
            if not separator or not name:
                self._bad_parse_request()
                return False
            try:
                decoded_name = name.decode("ascii")
                decoded_value = value.decode("latin-1").strip()
            except UnicodeDecodeError:
                self._bad_parse_request()
                return False
            if not _TOKEN.fullmatch(decoded_name):
                self._bad_parse_request()
                return False
            headers.add_header(decoded_name, decoded_value)

    def _bad_parse_request(self) -> None:
        self.requestline = ""
        self.request_version = "HTTP/1.1"
        self.command = ""
        self.close_connection = True
        self._error(400, "BAD_REQUEST")

    def do_GET(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def do_OPTIONS(self) -> None:
        self._reject_method()

    def do_PUT(self) -> None:
        self._reject_method()

    def do_PATCH(self) -> None:
        self._reject_method()

    def do_DELETE(self) -> None:
        self._reject_method()

    def do_HEAD(self) -> None:
        self._reject_method()

    def do_TRACE(self) -> None:
        self._reject_method()

    def do_CONNECT(self) -> None:
        self._reject_method()

    def _dispatch(self) -> None:
        if not self._validate_request_envelope():
            return
        parsed = urlsplit(self.path)
        if parsed.fragment or not parsed.path or parsed.path.startswith("//"):
            self._error(400, "BAD_REQUEST")
            return
        if parsed.query and parsed.path != "/api/v1/overview":
            self._error(400, "BAD_REQUEST")
            return
        if self.command == "GET" and parsed.path == "/":
            if self._has_body():
                self._error(400, "BAD_REQUEST")
                return
            self._asset("index.html")
            return
        if self.command == "GET" and parsed.path.startswith("/assets/"):
            if self._has_body():
                self._error(400, "BAD_REQUEST")
                return
            self._asset(parsed.path.removeprefix("/assets/"))
            return
        if parsed.path == "/api/v1/session":
            if self.command != "POST":
                self._method_not_allowed()
                return
            self._exchange_session()
            return
        if parsed.path == "/api/v1/meta":
            if self.command != "GET":
                self._method_not_allowed()
                return
            if self._has_body():
                self._error(400, "BAD_REQUEST")
                return
            session = self._authorized_session()
            if session is None:
                return
            self._json(
                200,
                {
                    "schema_version": 1,
                    "data": {
                        "version": __version__,
                        "capabilities": ["overview"] if self.console.overview_service else [],
                        "initial_workspace": self.console.initial_workspace,
                        "session_expires_at": session.expires_at.isoformat(),
                    },
                },
            )
            return
        if parsed.path == "/api/v1/overview":
            if self.command != "GET":
                self._method_not_allowed()
                return
            if self._has_body():
                self._error(400, "BAD_REQUEST")
                return
            if self._authorized_session() is None:
                return
            self._overview(parsed.query)
            return
        if parsed.path.startswith("/api/v1/workspaces/"):
            if self.command != "GET":
                self._method_not_allowed()
                return
            if self._has_body():
                self._error(400, "BAD_REQUEST")
                return
            if self._authorized_session() is None:
                return
            self._workspace_resource(parsed.path)
            return
        if parsed.path.startswith("/api/"):
            self._error(401, "UNAUTHORIZED")
            return
        self._error(404, "NOT_FOUND")

    def _asset(self, name: str) -> None:
        if not name or "/" in name or "\\" in name:
            self._error(404, "NOT_FOUND")
            return
        try:
            asset = load_asset(name)
        except ConsoleAssetError:
            self._error(404, "NOT_FOUND")
            return
        self._send(200, asset.body, asset.content_type)

    def _overview(self, query: str) -> None:
        service = self.console.overview_service
        if service is None:
            self._error(404, "NOT_FOUND")
            return
        try:
            cursor, limit = self._overview_parameters(query)
            payload = service.page(cursor=cursor, limit=limit)
        except ConsoleOverviewError as exc:
            self._error(self._overview_error_status(exc.code), exc.code)
            return
        self._json(200, payload, etag=str(payload.get("snapshot_sha256", "")))

    def _workspace_resource(self, path: str) -> None:
        service = self.console.overview_service
        if service is None:
            self._error(404, "NOT_FOUND")
            return
        remainder = path.removeprefix("/api/v1/workspaces/")
        inspect = remainder.endswith("/proofs")
        alias = remainder[: -len("/proofs")] if inspect else remainder
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", alias):
            self._error(400, "WORKSPACE_ALIAS_INVALID")
            return
        try:
            payload = service.inspect_proofs(alias) if inspect else service.workspace(alias)
        except ConsoleOverviewError as exc:
            self._error(self._overview_error_status(exc.code), exc.code)
            return
        self._json(200, payload, etag=str(payload.get("snapshot_sha256", "")))

    @staticmethod
    def _overview_error_status(code: str) -> int:
        if code in {
            "OVERVIEW_CURSOR_INVALID",
            "OVERVIEW_LIMIT_INVALID",
            "OVERVIEW_QUERY_INVALID",
            "WORKSPACE_ALIAS_INVALID",
        }:
            return 400
        if code == "WORKSPACE_NOT_FOUND":
            return 404
        return 503

    @staticmethod
    def _overview_parameters(query: str) -> tuple[str | None, int]:
        if not query:
            return None, 20
        values: dict[str, str] = {}
        for segment in query.split("&"):
            key, separator, value = segment.partition("=")
            if (
                not separator
                or key not in {"cursor", "limit"}
                or key in values
                or not value
            ):
                raise ConsoleOverviewError("OVERVIEW_QUERY_INVALID")
            values[key] = value
        cursor = values.get("cursor")
        if cursor is not None and (
            len(cursor) > 512 or not re.fullmatch(r"[A-Za-z0-9_-]+", cursor)
        ):
            raise ConsoleOverviewError("OVERVIEW_QUERY_INVALID")
        raw_limit = values.get("limit", "20")
        if len(raw_limit) > 3 or not raw_limit.isdecimal():
            raise ConsoleOverviewError("OVERVIEW_QUERY_INVALID")
        return cursor, int(raw_limit)

    def _validate_request_envelope(self) -> bool:
        if len(self.raw_requestline) > REQUEST_LINE_LIMIT:
            self._error(400, "BAD_REQUEST")
            return False
        if not self.path.startswith("/") or self.path.startswith("//"):
            self._error(400, "BAD_REQUEST")
            return False
        try:
            header_items = list(self.headers.items())
        except (TypeError, ValueError):
            self._error(400, "BAD_REQUEST")
            return False
        if sum(len(name) + len(value) + 4 for name, value in header_items) > HEADER_LIMIT:
            self._error(400, "BAD_REQUEST")
            return False
        hosts = self.headers.get_all("Host") or []
        if len(hosts) != 1 or hosts[0] != f"{HOST}:{self.console.server_port}":
            self._error(400, "BAD_REQUEST")
            return False
        if self.headers.get_all("Transfer-Encoding"):
            self._error(400, "BAD_REQUEST")
            return False
        if any(name.lower() == "forwarded" or name.lower().startswith("x-forwarded-") for name, _ in header_items):
            self._error(400, "BAD_REQUEST")
            return False
        return True

    def _has_body(self) -> bool:
        return bool(self.headers.get_all("Content-Length"))

    def _content_length(self) -> int | None:
        values = self.headers.get_all("Content-Length") or []
        if len(values) != 1 or not values[0].isdigit():
            return None
        value = int(values[0])
        return value if value <= SESSION_BODY_LIMIT else None

    def _valid_origin(self, *, required: bool) -> bool:
        origins = self.headers.get_all("Origin") or []
        if required and len(origins) != 1:
            return False
        if origins and (len(origins) != 1 or origins[0] != self.console.origin):
            return False
        sites = self.headers.get_all("Sec-Fetch-Site") or []
        return not sites or (len(sites) == 1 and sites[0] == "same-origin")

    def _exchange_session(self) -> None:
        if not self._valid_origin(required=True):
            self._error(403, "ORIGIN_REJECTED")
            return
        content_types = self.headers.get_all("Content-Type") or []
        length = self._content_length()
        if len(content_types) != 1 or content_types[0] != "application/json" or length is None:
            self._error(400, "BAD_REQUEST")
            return
        try:
            raw = self.rfile.read(length)
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, OSError):
            self._error(400, "BAD_REQUEST")
            return
        if not isinstance(decoded, dict) or set(decoded) != {"bootstrap"}:
            self._error(400, "BAD_REQUEST")
            return
        try:
            session = self.console.sessions.exchange(decoded["bootstrap"])
        except SessionRejected:
            self._error(401, "UNAUTHORIZED")
            return
        self._json(
            201,
            {
                "schema_version": 1,
                "bearer": session.token,
                "session_expires_at": session.expires_at.isoformat(),
            },
        )

    def _authorized_session(self) -> object | None:
        if not self._valid_origin(required=False):
            self._error(403, "ORIGIN_REJECTED")
            return None
        values = self.headers.get_all("Authorization") or []
        if len(values) != 1 or not values[0].startswith("Bearer "):
            self._error(401, "UNAUTHORIZED")
            return None
        try:
            return self.console.sessions.authorize(values[0][len("Bearer ") :])
        except SessionRejected:
            self._error(401, "UNAUTHORIZED")
            return None

    def _method_not_allowed(self) -> None:
        self._error(405, "METHOD_NOT_ALLOWED")

    def _reject_method(self) -> None:
        if self._validate_request_envelope():
            self._method_not_allowed()

    def _json(self, status: int, payload: object, *, etag: str = "") -> None:
        if status == 200 and self._if_none_match(etag):
            self._send(304, b"", "application/json; charset=utf-8", etag=etag)
            return
        self._send(
            status,
            _json_bytes(payload),
            "application/json; charset=utf-8",
            etag=etag,
        )

    def _error(self, status: int, code: str) -> None:
        self._json(status, {"schema_version": 1, "error": {"code": code}})

    def _if_none_match(self, etag: str) -> bool:
        if not re.fullmatch(r"[0-9a-f]{64}", etag):
            return False
        values = self.headers.get_all("If-None-Match") or []
        return len(values) == 1 and values[0] == f'"{etag}"'

    def _send(
        self, status: int, body: bytes, content_type: str, *, etag: str = ""
    ) -> None:
        # Every response terminates this HTTP/1.1 connection.  In particular,
        # rejected GET or unknown API requests must never leave an unread body
        # that a later handler iteration could interpret as a request line.
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Content-Security-Policy", _CSP)
        if re.fullmatch(r"[0-9a-f]{64}", etag):
            self.send_header("ETag", f'"{etag}"')
        self.end_headers()
        self.wfile.write(body)


def create_console_http_server(
    *,
    port: int = 0,
    bootstrap_secret: str | None = None,
    session_store: ConsoleSessionStore | None = None,
    overview_service: IsolatedOverviewService | None = None,
    initial_workspace: str | None = None,
    max_concurrent_requests: int = MAX_CONCURRENT_REQUESTS,
) -> ConsoleHTTPServer:
    """Bind a Console server to IPv4 loopback only; no host override exists."""
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("Console port 必须是 0 到 65535 之间的整数")
    if type(max_concurrent_requests) is not int or max_concurrent_requests < 1:
        raise ValueError("Console max_concurrent_requests 必须是正整数")
    if session_store is not None and bootstrap_secret is not None:
        raise ValueError("Console session_store 与 bootstrap_secret 不能同时指定")
    return ConsoleHTTPServer(
        port=port,
        bootstrap_secret=bootstrap_secret,
        session_store=session_store,
        overview_service=overview_service,
        initial_workspace=initial_workspace,
        max_concurrent_requests=max_concurrent_requests,
    )

"""A small stdlib-only reverse proxy.

Local clients talk plain HTTP to ``127.0.0.1:<port>`` and every request is
forwarded to a fixed upstream origin.  Redirects and cookies are rewritten so
that the browser stays on the local origin.
"""

from __future__ import annotations

import errno
import gzip
import http.client
import os
import re
import socket
import ssl
import threading
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, urlunsplit

from .js_rules import (
    HTML_CONTENT_TYPES,
    JS_CONTENT_TYPES,
    JsRewriteConfig,
    charset_from_content_type,
    rewrite_inline_scripts,
    rewrite_js,
)

try:  # Python 3.14+
    from compression import zstd
except ImportError:  # pragma: no cover - optional dependency on older versions
    zstd = None  # type: ignore[assignment]

HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

REWRITABLE_TYPES = frozenset(
    {
        "text/html",
        "application/xhtml+xml",
        "text/css",
        "image/svg+xml",
    }
) | JS_CONTENT_TYPES


_CHUNK_SIZE = 64 * 1024
_REFRESH_URL_RE = re.compile(r"(url\s*=\s*)(['\"]?)([^'\"\s;]+)", re.IGNORECASE)


@dataclass(frozen=True)
class Target:
    """Parsed upstream origin."""

    url: str
    scheme: str
    host: str
    port: int
    netloc: str
    base_path: str

    @classmethod
    def parse(cls, url: str) -> "Target":
        if "://" not in url:
            url = "https://" + url
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        if scheme not in ("http", "https"):
            raise ValueError(f"unsupported scheme: {scheme!r} (expected http or https)")
        if not parts.hostname:
            raise ValueError(f"missing host in target URL: {url!r}")
        try:
            port = parts.port or (443 if scheme == "https" else 80)
        except ValueError as exc:
            raise ValueError(f"invalid port in target URL: {url!r}") from exc
        netloc = parts.netloc.rsplit("@", 1)[-1]  # drop userinfo
        return cls(
            url=url,
            scheme=scheme,
            host=parts.hostname,
            port=port,
            netloc=netloc,
            base_path=parts.path.rstrip("/"),
        )

    @property
    def origin(self) -> str:
        return f"{self.scheme}://{self.netloc}"


@dataclass(frozen=True)
class RequestRecord:
    """One completed proxied request, used by observers such as the GUI."""

    time: float
    client: str
    method: str
    path: str
    status: int
    duration: float
    js_edits: int = 0


def is_address_in_use(exc: OSError) -> bool:
    """Return True when a bind failed because the port is already taken."""
    return exc.errno == errno.EADDRINUSE or getattr(exc, "winerror", None) == 10048


def _join_path(base_path: str, path: str) -> str:
    if not path.startswith("/"):
        path = "/" + path
    return base_path + path if base_path else path


def _rewrite_url(value: str, target: Target, local_origin: str) -> str:
    """Point same-origin upstream URLs back at the local proxy."""
    try:
        parts = urlsplit(value)
    except ValueError:
        return value
    if not parts.netloc or parts.hostname != target.host:
        return value
    try:
        port = parts.port
    except ValueError:
        return value
    if port not in (None, target.port, 80, 443):
        return value
    local = urlsplit(local_origin)
    path = parts.path or "/"
    if target.base_path:
        if path == target.base_path:
            path = "/"
        elif path.startswith(target.base_path + "/"):
            path = path[len(target.base_path) :]
    return urlunsplit((local.scheme, local.netloc, path, parts.query, parts.fragment))


def _rewrite_refresh(value: str, target: Target, local_origin: str) -> str:
    def replace(match: re.Match[str]) -> str:
        return match.group(1) + match.group(2) + _rewrite_url(match.group(3), target, local_origin)

    return _REFRESH_URL_RE.sub(replace, value)


def _sanitize_cookie(value: str, local_is_secure: bool) -> str:
    """Drop attributes that would make the browser reject a cookie for localhost."""
    parts = value.split(";")
    cleaned = [parts[0].strip()]
    for attribute in parts[1:]:
        name, _, attr_value = attribute.partition("=")
        name = name.strip().lower()
        if name == "domain":
            continue
        if name == "secure" and not local_is_secure:
            continue
        if name == "samesite" and attr_value.strip().lower() == "none" and not local_is_secure:
            cleaned.append("SameSite=Lax")
            continue
        cleaned.append(attribute.strip())
    return "; ".join(part for part in cleaned if part)


def _decode_body(data: bytes, encoding: str | None) -> bytes | None:
    """Decode a response body, or return None when the encoding is unsupported."""
    if not encoding:
        return data
    try:
        for token in reversed([token.strip().lower() for token in encoding.split(",") if token.strip()]):
            if token == "identity":
                continue
            if token in ("gzip", "x-gzip"):
                data = gzip.decompress(data)
            elif token == "deflate":
                try:
                    data = zlib.decompress(data)
                except zlib.error:
                    data = zlib.decompress(data, -zlib.MAX_WBITS)
            elif token == "zstd":
                if zstd is None:
                    return None
                data = zstd.decompress(data)
            else:
                return None  # e.g. brotli, which the stdlib cannot decode
    except Exception:  # noqa: BLE001 - fall back to passing the body through untouched
        return None
    return data


def _rewrite_body(data: bytes, target: Target, local_origin: str) -> bytes:
    """Rewrite absolute links to the upstream origin so they stay on the local proxy."""
    local_netloc = urlsplit(local_origin).netloc
    local = local_origin.encode()
    replacements = (
        (f"https://{target.netloc}", local),
        (f"http://{target.netloc}", local),
        (f"//{target.netloc}", f"//{local_netloc}".encode()),
    )
    # Replace origin + base path first so the local path does not gain the prefix twice.
    if target.base_path:
        boundary = rb"(?=/|[?#\"'\s]|$)"
        base = re.escape(target.base_path.encode())
        for needle, replacement in replacements:
            pattern = re.compile(re.escape(needle.encode()) + base + boundary)
            data = pattern.sub(lambda _match, value=replacement: value, data)
    for needle, replacement in replacements:
        data = data.replace(needle.encode(), replacement)
    return data


def _connection_tokens(headers) -> set[str]:
    tokens: set[str] = set()
    for value in headers.get_all("Connection") or ():
        tokens.update(token.strip().lower() for token in value.split(",") if token.strip())
    return tokens


class ProxyRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "isproxy/0.1"

    server: "ProxyServer"  # type: ignore[assignment]

    # -- connection tracking ----------------------------------------------
    def setup(self) -> None:
        super().setup()
        self.server.register_connection(self.connection)

    def finish(self) -> None:
        try:
            super().finish()
        finally:
            self.server.unregister_connection(self.connection)

    # -- HTTP methods -----------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        self._proxy()

    def do_HEAD(self) -> None:  # noqa: N802
        self._proxy()

    def do_POST(self) -> None:  # noqa: N802
        self._proxy()

    def do_PUT(self) -> None:  # noqa: N802
        self._proxy()

    def do_PATCH(self) -> None:  # noqa: N802
        self._proxy()

    def do_DELETE(self) -> None:  # noqa: N802
        self._proxy()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._proxy()

    # -- proxy logic ------------------------------------------------------
    def _proxy(self) -> None:
        started = time.monotonic()
        status = 502
        js_edits = 0
        try:
            target = self.server.target
            try:
                body = self._read_request_body()
            except ValueError as exc:
                status = 400
                self.log_error("bad request body: %s", exc)
                self._send_error(400, "Bad request")
                return

            upstream_path = _join_path(target.base_path, self._upstream_path())
            headers = self._upstream_headers()
            local_origin = self._local_origin()

            conn_cls = http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
            conn_kwargs = {"timeout": self.server.upstream_timeout}
            if target.scheme == "https":
                conn_kwargs["context"] = self.server.ssl_context
            conn = conn_cls(target.host, target.port, **conn_kwargs)

            try:
                try:
                    conn.request(self.command, upstream_path, body=body, headers=headers)
                    response = conn.getresponse()
                except (OSError, http.client.HTTPException) as exc:
                    self.log_error("upstream request failed: %s", exc)
                    self._send_error(502, "Upstream error")
                    return
                status = response.status
                try:
                    js_edits = self._relay_response(response, target, local_origin, upstream_path)
                except (BrokenPipeError, ConnectionResetError):
                    self.close_connection = True
                except (OSError, http.client.HTTPException) as exc:
                    self.log_error("upstream response failed: %s", exc)
                    self.close_connection = True
            finally:
                conn.close()
        finally:
            self.server.notify_request(
                RequestRecord(
                    time=time.time(),
                    client=self.client_address[0],
                    method=self.command,
                    path=self.path,
                    status=status,
                    duration=time.monotonic() - started,
                    js_edits=js_edits,
                )
            )

    def _upstream_path(self) -> str:
        path = self.path
        if path == "*":
            return "/"
        if path.startswith(("http://", "https://")):
            parts = urlsplit(path)
            path = urlunsplit(("", "", parts.path or "/", parts.query, ""))
        return path

    def _read_request_body(self) -> bytes | None:
        transfer_encoding = self.headers.get("Transfer-Encoding", "")
        if "chunked" in transfer_encoding.lower():
            chunks: list[bytes] = []
            while True:
                line = self.rfile.readline(65537)
                if not line:
                    raise ValueError("unexpected EOF while reading chunk size")
                size = int(line.split(b";", 1)[0].strip() or b"0", 16)
                if size == 0:
                    while True:
                        trailer = self.rfile.readline(65537)
                        if trailer in (b"\r\n", b"\n", b""):
                            break
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.read(2)  # trailing CRLF
            return b"".join(chunks)

        length = self.headers.get("Content-Length")
        if length is None:
            return None
        size = int(length)
        if size < 0:
            raise ValueError("negative Content-Length")
        return self.rfile.read(size) if size else b""

    def _upstream_headers(self) -> dict[str, str]:
        target = self.server.target
        connection_tokens = _connection_tokens(self.headers)
        skip = HOP_BY_HOP | connection_tokens | {"host", "content-length", "expect"}

        headers: dict[str, str] = {}
        original_names: dict[str, str] = {}
        for name, value in self.headers.items():
            key = name.lower()
            if key in skip:
                continue
            if key in headers:
                headers[key] = f"{headers[key]}, {value}"
            else:
                headers[key] = value
                original_names[key] = name
        result = {original_names[key]: value for key, value in headers.items()}
        for key in ("host", "x-forwarded-for", "x-forwarded-proto", "x-forwarded-host"):
            result.pop(original_names.get(key), None)

        result["Host"] = target.netloc
        client = self.client_address[0]
        forwarded_for = self.headers.get("X-Forwarded-For")
        result["X-Forwarded-For"] = f"{forwarded_for}, {client}" if forwarded_for else client
        result["X-Forwarded-Proto"] = "http"
        result["X-Forwarded-Host"] = self.headers.get("Host", target.netloc)

        # Ask only for encodings we can decode before rewriting HTML/CSS/JS links.
        unsupported = {"br"} if zstd is not None else {"br", "zstd"}
        for name in list(result):
            if name.lower() != "accept-encoding":
                continue
            tokens = [token.strip() for token in result[name].split(",") if token.strip()]
            kept = [token for token in tokens if token.lower() not in unsupported]
            if kept:
                result[name] = ", ".join(kept)
            else:
                del result[name]
            break
        return result

    def _local_origin(self) -> str:
        host = self.headers.get("Host") or self.server.default_local_host
        return f"http://{host}"

    def _send_error(self, status: int, message: str) -> None:
        try:
            self.send_error(status, message)
        except OSError:
            self.close_connection = True

    def _relay_response(
        self,
        response: http.client.HTTPResponse,
        target: Target,
        local_origin: str,
        scope_path: str,
    ) -> int:
        body_allowed = (
            self.command != "HEAD"
            and response.status not in (204, 304)
            and not 100 <= response.status < 200
        )

        raw_content_type = response.getheader("Content-Type") or ""
        content_type = raw_content_type.split(";", 1)[0].strip().lower()
        rewritable = body_allowed and content_type in REWRITABLE_TYPES

        buffered: bytes | None = None
        length: int | None = None
        drop_content_encoding = False
        js_edits = 0
        if self.command == "HEAD":
            raw_length = response.getheader("Content-Length")
            if raw_length and raw_length.strip().isdigit():
                length = int(raw_length)
        elif body_allowed:
            if rewritable:
                raw = response.read()
                decoded = _decode_body(raw, response.getheader("Content-Encoding"))
                if decoded is None:
                    buffered = raw
                else:
                    config = self.server.js_config
                    charset = charset_from_content_type(raw_content_type)
                    path = scope_path.partition("?")[0]
                    if content_type in HTML_CONTENT_TYPES:
                        decoded, js_edits = rewrite_inline_scripts(decoded, config, charset, path)
                    elif content_type in JS_CONTENT_TYPES:
                        decoded, js_edits = rewrite_js(decoded, config, charset, path)
                    buffered = _rewrite_body(decoded, target, local_origin)
                    drop_content_encoding = True
                length = len(buffered)
            elif response.length is not None:
                length = response.length
            else:
                buffered = response.read()
                length = len(buffered)

        self.send_response_only(response.status, response.reason)
        skip = HOP_BY_HOP | _connection_tokens(response.msg) | {"content-length"}
        for name, value in response.getheaders():
            if name.lower() in skip:
                continue
            if drop_content_encoding and name.lower() == "content-encoding":
                continue
            if name.lower() == "location":
                value = _rewrite_url(value, target, local_origin)
            elif name.lower() == "set-cookie":
                value = _sanitize_cookie(value, local_origin.startswith("https://"))
            elif name.lower() == "refresh":
                value = _rewrite_refresh(value, target, local_origin)
            self.send_header(name, value)

        if length is not None and (body_allowed or self.command == "HEAD"):
            self.send_header("Content-Length", str(length))
        elif body_allowed:
            self.close_connection = True
            self.send_header("Connection", "close")
        self.end_headers()

        if not body_allowed:
            return js_edits
        if buffered is not None:
            self.wfile.write(buffered)
            return js_edits
        while True:
            chunk = response.read(_CHUNK_SIZE)
            if not chunk:
                break
            self.wfile.write(chunk)
        return js_edits


class ProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR allows a second proxy to silently bind the same
    # port, so a stale runoob proxy can keep serving while a new target starts.
    # Disable it there to fail fast with "address already in use" instead.
    allow_reuse_address = os.name != "nt"

    def __init__(
        self,
        server_address: tuple[str, int],
        target: Target,
        *,
        timeout: float = 30.0,
        verify_tls: bool = True,
        on_request: Callable[[RequestRecord], None] | None = None,
        js_config: JsRewriteConfig | None = None,
    ) -> None:
        self.target = target
        self.upstream_timeout = timeout
        self.on_request = on_request
        self.js_config = js_config
        self.default_local_host = f"{server_address[0]}:{server_address[1]}"
        self._connections: set[socket.socket] = set()
        self._connections_lock = threading.Lock()
        self._closing = False
        if target.scheme == "https":
            context = ssl.create_default_context()
            if not verify_tls:
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            self.ssl_context = context
        else:
            self.ssl_context = None
        super().__init__(server_address, ProxyRequestHandler)

    def notify_request(self, record: RequestRecord) -> None:
        callback = self.on_request
        if callback is None:
            return
        try:
            callback(record)
        except Exception:  # noqa: BLE001 - observers must never break request handling
            pass

    def register_connection(self, connection: socket.socket) -> None:
        with self._connections_lock:
            if not self._closing:
                self._connections.add(connection)
                return
        self._close_socket(connection)

    def unregister_connection(self, connection: socket.socket) -> None:
        with self._connections_lock:
            self._connections.discard(connection)

    @staticmethod
    def _close_socket(connection: socket.socket) -> None:
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            connection.close()
        except OSError:
            pass

    def close_active_connections(self) -> None:
        """Close keep-alive client sockets so a stopped server cannot serve stale rules."""
        with self._connections_lock:
            self._closing = True
            connections = list(self._connections)
            self._connections.clear()
        for connection in connections:
            self._close_socket(connection)

    def server_close(self) -> None:
        try:
            super().server_close()
        finally:
            self.close_active_connections()

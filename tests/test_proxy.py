from __future__ import annotations

import gzip
import http.client
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from isproxy.js_rules import JsRewriteConfig, JsRule
from isproxy.proxy import ProxyServer, RequestRecord, Target, is_address_in_use


class UpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass

    def _respond(self, status: int, body: bytes = b"", **headers: str) -> None:
        content_type = headers.pop("Content-Type", "text/plain; charset=utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        port = self.server.server_address[1]
        if self.path.endswith("/redirect"):
            prefix = self.path[: -len("/redirect")]
            self._respond(302, Location=f"http://{self.headers['Host']}{prefix}/landed")
        elif self.path == "/cookie":
            self._respond(200, b"cookie", **{"Set-Cookie": "session=abc; Domain=127.0.0.1; Path=/; Secure"})
        elif self.path == "/headers":
            body = "|".join(
                self.headers.get(name, "")
                for name in ("Host", "X-Forwarded-For", "X-Forwarded-Proto", "Accept-Encoding")
            ).encode()
            self._respond(200, body)
        elif self.path.endswith("/page"):
            prefix = self.path[: -len("/page")]
            html = (
                f'<a href="https://127.0.0.1:{port}{prefix}/a">a</a>'
                f'<a href="//127.0.0.1:{port}{prefix}/b">b</a>'
            ).encode()
            self._respond(200, html, **{"Content-Type": "text/html; charset=utf-8"})
        elif self.path.endswith("/js-page"):
            html = b'<p>OLD</p><script>var url = "MARKER"; var x = "OLD";</script>'
            self._respond(200, html, **{"Content-Type": "text/html; charset=utf-8"})
        elif self.path.endswith("/app.js"):
            self._respond(200, b'var x = "OLD";', **{"Content-Type": "application/javascript"})
        elif self.path == "/gzip":
            html = f'<a href="https://127.0.0.1:{port}/z">z</a>'.encode()
            self._respond(
                200,
                gzip.compress(html),
                **{"Content-Type": "text/html", "Content-Encoding": "gzip"},
            )
        else:
            self._respond(200, f"{self.command} {self.path}".encode())

    def do_HEAD(self) -> None:  # noqa: N802
        self._respond(200, f"{self.command} {self.path}".encode())

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self._respond(200, f"{self.command} {self.path} ".encode() + body)


def _start(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


class ProxyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        _start(cls.upstream)
        upstream_port = cls.upstream.server_address[1]

        cls.records: list = []
        cls.proxy = ProxyServer(
            ("127.0.0.1", 0),
            Target.parse(f"http://127.0.0.1:{upstream_port}"),
            on_request=cls.records.append,
        )
        _start(cls.proxy)
        cls.proxy_port = cls.proxy.server_address[1]

        cls.base_proxy = ProxyServer(("127.0.0.1", 0), Target.parse(f"http://127.0.0.1:{upstream_port}/base"))
        _start(cls.base_proxy)
        cls.base_proxy_port = cls.base_proxy.server_address[1]

    @classmethod
    def tearDownClass(cls) -> None:
        for server in (cls.upstream, cls.proxy, cls.base_proxy):
            server.shutdown()
            server.server_close()

    def request(self, method: str, path: str, *, server=None, body=None, headers=None, **kwargs):
        server = server or self.proxy
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {}, **kwargs)
            response = conn.getresponse()
            return response.status, response.getheaders(), response.read()
        finally:
            conn.close()

    def start_proxy(self, js_config: JsRewriteConfig | None = None) -> ProxyServer:
        proxy = ProxyServer(
            ("127.0.0.1", 0),
            Target.parse(f"http://127.0.0.1:{self.upstream.server_address[1]}"),
            js_config=js_config,
            on_request=self.records.append,
        )
        _start(proxy)
        self.addCleanup(proxy.server_close)
        self.addCleanup(proxy.shutdown)
        return proxy

    def wait_for_record(self) -> RequestRecord:
        deadline = time.time() + 2
        while not self.records and time.time() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.records)
        return self.records[-1]

    def test_get_is_forwarded(self) -> None:
        status, headers, body = self.request("GET", "/hello")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"GET /hello")
        self.assertEqual(dict(headers)["Content-Type"], "text/plain; charset=utf-8")

    def test_head_has_no_body_but_keeps_length(self) -> None:
        status, headers, body = self.request("HEAD", "/hello")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertEqual(dict(headers)["Content-Length"], str(len("HEAD /hello")))

    def test_post_body_is_forwarded(self) -> None:
        status, _, body = self.request("POST", "/echo", body=b"abc")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"POST /echo abc")

    def test_chunked_request_body_is_forwarded(self) -> None:
        status, _, body = self.request(
            "POST",
            "/echo",
            body=[b"a", b"b"],
            headers={"Transfer-Encoding": "chunked"},
            encode_chunked=True,
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, b"POST /echo ab")

    def test_absolute_form_path(self) -> None:
        status, _, body = self.request("GET", "http://example.invalid/hello?x=1")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"GET /hello?x=1")

    def test_redirect_location_is_rewritten(self) -> None:
        status, headers, _ = self.request("GET", "/redirect")
        self.assertEqual(status, 302)
        self.assertEqual(
            dict(headers)["Location"],
            f"http://127.0.0.1:{self.proxy_port}/landed",
        )

    def test_cookie_attributes_are_relaxed(self) -> None:
        _, headers, _ = self.request("GET", "/cookie")
        cookie = dict(headers)["Set-Cookie"].lower()
        self.assertIn("session=abc", cookie)
        self.assertNotIn("domain=", cookie)
        self.assertNotIn("secure", cookie)

    def test_forwarded_headers(self) -> None:
        _, _, body = self.request("GET", "/headers")
        host, forwarded_for, proto, _ = body.decode().split("|")
        self.assertEqual(host, f"127.0.0.1:{self.upstream.server_address[1]}")
        self.assertEqual(forwarded_for, "127.0.0.1")
        self.assertEqual(proto, "http")

    def test_request_callback_records_request(self) -> None:
        self.records.clear()
        self.request("GET", "/hello")
        deadline = time.time() + 2
        while not self.records and time.time() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.records)
        record = self.records[-1]
        self.assertEqual((record.method, record.path, record.status), ("GET", "/hello", 200))
        self.assertEqual(record.client, "127.0.0.1")
        self.assertGreaterEqual(record.duration, 0)

    def test_html_links_are_rewritten(self) -> None:
        _, headers, body = self.request("GET", "/page")
        text = body.decode()
        self.assertIn(f"http://127.0.0.1:{self.proxy_port}/a", text)
        self.assertIn(f"//127.0.0.1:{self.proxy_port}/b", text)
        self.assertNotIn(f"127.0.0.1:{self.upstream.server_address[1]}/a", text)
        self.assertNotIn("content-encoding", {name.lower() for name, _ in headers})

    def test_gzip_html_links_are_rewritten(self) -> None:
        _, headers, body = self.request("GET", "/gzip")
        self.assertNotIn("content-encoding", {name.lower() for name, _ in headers})
        self.assertIn(f"http://127.0.0.1:{self.proxy_port}/z", body.decode())

    def test_brotli_is_not_requested_upstream(self) -> None:
        _, _, body = self.request("GET", "/headers", headers={"Accept-Encoding": "gzip, br"})
        self.assertEqual(body.decode().split("|")[3], "gzip")

    def test_base_path_is_prepended(self) -> None:
        status, _, body = self.request("GET", "/hello", server=self.base_proxy)
        self.assertEqual(status, 200)
        self.assertEqual(body, b"GET /base/hello")

    def test_redirect_location_strips_base_path(self) -> None:
        status, headers, _ = self.request("GET", "/redirect", server=self.base_proxy)
        self.assertEqual(status, 302)
        self.assertEqual(
            dict(headers)["Location"],
            f"http://127.0.0.1:{self.base_proxy_port}/landed",
        )

    def test_html_links_strip_base_path(self) -> None:
        _, _, body = self.request("GET", "/page", server=self.base_proxy)
        text = body.decode()
        self.assertIn(f"http://127.0.0.1:{self.base_proxy_port}/a", text)
        self.assertIn(f"//127.0.0.1:{self.base_proxy_port}/b", text)
        self.assertNotIn("/base/a", text)

    def test_port_conflict_is_rejected(self) -> None:
        with self.assertRaises(OSError) as caught:
            ProxyServer(("127.0.0.1", self.proxy_port), Target.parse("https://example.com"))
        self.assertTrue(is_address_in_use(caught.exception))

    def test_js_rules_rewrite_inline_script_only(self) -> None:
        self.records.clear()
        config = JsRewriteConfig(rules=(JsRule(find="OLD", replace="NEW"),))
        proxy = self.start_proxy(config)
        status, _, body = self.request("GET", "/js-page", server=proxy)
        self.assertEqual(status, 200)
        self.assertIn(b"<p>OLD</p>", body)
        self.assertIn(b'var x = "NEW";', body)
        self.assertEqual(self.wait_for_record().js_edits, 1)

    def test_js_rules_leave_external_js_alone_by_default(self) -> None:
        self.records.clear()
        config = JsRewriteConfig(rules=(JsRule(find="OLD", replace="NEW"),))
        proxy = self.start_proxy(config)
        _, _, body = self.request("GET", "/app.js", server=proxy)
        self.assertEqual(body, b'var x = "OLD";')
        self.assertEqual(self.wait_for_record().js_edits, 0)

    def test_js_rules_rewrite_external_js_when_enabled(self) -> None:
        self.records.clear()
        config = JsRewriteConfig(
            external_scripts=True,
            rules=(JsRule(find="OLD", replace="NEW"),),
        )
        proxy = self.start_proxy(config)
        _, _, body = self.request("GET", "/app.js", server=proxy)
        self.assertEqual(body, b'var x = "NEW";')
        self.assertEqual(self.wait_for_record().js_edits, 1)

    def test_js_rule_inserted_url_is_localized(self) -> None:
        self.records.clear()
        upstream_url = f"http://127.0.0.1:{self.upstream.server_address[1]}/asset"
        config = JsRewriteConfig(rules=(JsRule(find="MARKER", replace=upstream_url),))
        proxy = self.start_proxy(config)
        _, _, body = self.request("GET", "/js-page", server=proxy)
        self.assertIn(f"http://127.0.0.1:{proxy.server_address[1]}/asset".encode(), body)
        self.assertNotIn(upstream_url.encode(), body)

    def test_disabled_js_rule_is_not_applied(self) -> None:
        self.records.clear()
        config = JsRewriteConfig(rules=(JsRule(find="OLD", replace="NEW", enabled=False),))
        proxy = self.start_proxy(config)
        _, _, body = self.request("GET", "/js-page", server=proxy)
        self.assertIn(b'var x = "OLD";', body)
        self.assertEqual(self.wait_for_record().js_edits, 0)

    def test_stop_closes_keep_alive_connections(self) -> None:
        proxy = self.start_proxy()
        connection = http.client.HTTPConnection("127.0.0.1", proxy.server_address[1], timeout=5)
        try:
            connection.request("GET", "/hello")
            response = connection.getresponse()
            self.assertEqual(response.read(), b"GET /hello")

            proxy.shutdown()
            proxy.server_close()

            with self.assertRaises((OSError, http.client.HTTPException)):
                connection.request("GET", "/hello")
                connection.getresponse().read()
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()

"""Loopback server routing, headers, and realpath containment."""

import html
import contextlib
import http.client
import http.server
import json
import io
import os
import socket
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import helpers

sys.path.insert(0, str(helpers.REPO / "paseo-explain" / "scripts"))

from explainlib import serve

PAGE_CSP = (
    "default-src 'none'; script-src 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'unsafe-inline'; img-src data:; font-src 'none'; "
    "connect-src 'none'; base-uri 'none'; form-action 'none'"
)
INDEX_CSP = "default-src 'none'; style-src 'unsafe-inline'"
NONE_CSP = "default-src 'none'"

RAW_TITLE = '<b>x</b>&"'
HTML_BYTES = b'<meta name="paseo-explain" content="0.2.0">\n'
MD_BYTES = b"# explain\n"
SENTINEL_HTML = b"SENTINEL-HTML-OUTSIDE"
SENTINEL_DIR = b"SENTINEL-DIR-OUTSIDE"
SENTINEL_MD = b"SENTINEL-MD-OUTSIDE"


def _write_session(directory: Path, created_at: str, title: str, html: bytes, md: bytes | None) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "session.json").write_text(
        json.dumps({"created_at": created_at}),
        encoding="utf-8",
    )
    (directory / "explain.json").write_text(
        json.dumps({"title": title}),
        encoding="utf-8",
    )
    (directory / "explain.html").write_bytes(html)
    if md is not None:
        (directory / "explain.md").write_bytes(md)


def _fetch(port: int, method: str, path: str, host: str = "127.0.0.1"):
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        conn.request(method, path)
        response = conn.getresponse()
        body = response.read()
        headers = {key.lower(): value for key, value in response.getheaders()}
        return response.status, headers, body
    finally:
        conn.close()


class InsideTests(unittest.TestCase):
    def test_root_slash_contains_tmp(self):
        self.assertTrue(serve._inside("/", "/tmp/x"))

    def test_sibling_prefix_is_outside(self):
        self.assertFalse(serve._inside("/srv/a", "/srv/ab/x"))

    def test_child_of_root_is_inside(self):
        self.assertTrue(serve._inside("/srv/a", "/srv/a/x"))

    def test_symlink_outside_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="pe-test-") as temporary:
            base = Path(temporary)
            root = base / "root"
            root.mkdir()
            outside = base / "outside"
            outside.mkdir()
            secret = outside / "secret"
            secret.write_text("nope", encoding="utf-8")
            link = root / "link"
            link.symlink_to(secret)
            inside = root / "inside.txt"
            inside.write_text("yes", encoding="utf-8")
            root_real = os.path.realpath(root)
            self.assertFalse(serve._inside(root_real, str(link)))
            self.assertTrue(serve._inside(root_real, str(inside)))


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="pe-test-")
        base = Path(cls._tmp.name)
        cls.root = base / "sessions"
        cls.root.mkdir()
        outside = base / "outside"
        outside.mkdir()
        (outside / "secret.html").write_bytes(SENTINEL_HTML)
        (outside / "secret.md").write_bytes(SENTINEL_MD)
        (outside / "session.json").write_text(
            json.dumps({"created_at": "2099-01-01T00:00:00Z"}),
            encoding="utf-8",
        )
        (outside / "explain.json").write_text(
            json.dumps({"title": "EXPLAIN-OUTSIDE-TITLE"}),
            encoding="utf-8",
        )
        linked = outside / "linked-dir"
        _write_session(linked, "2099-01-01T00:00:00Z", "OUTSIDE-DIR", SENTINEL_DIR, b"outside-md")

        _write_session(cls.root / "good-1", "2026-09-30T12:00:00Z", RAW_TITLE, HTML_BYTES, MD_BYTES)
        _write_session(cls.root / "older-1", "2020-01-01T00:00:00Z", "Older", b"<p>older</p>", b"older-md")
        _write_session(cls.root / "Bad_Slug", "2026-09-30T12:00:00Z", "Bad", b"<p>bad</p>", b"bad")

        html_out = cls.root / "html-out"
        _write_session(html_out, "2026-09-30T12:00:00Z", "Html Out", b"placeholder", b"md-inside")
        (html_out / "explain.html").unlink()
        (html_out / "explain.html").symlink_to(outside / "secret.html")

        json_out = cls.root / "json-out"
        _write_session(json_out, "2026-09-30T12:00:00Z", "Should Skip", b"<p>skip</p>", b"skip-md")
        (json_out / "session.json").unlink()
        (json_out / "session.json").symlink_to(outside / "session.json")

        explain_out = cls.root / "explain-out"
        _write_session(explain_out, "2026-09-30T12:00:00Z", "Local Title", b"<p>local</p>", b"local-md")
        (explain_out / "explain.json").unlink()
        (explain_out / "explain.json").symlink_to(outside / "explain.json")

        md_out = cls.root / "md-out"
        _write_session(md_out, "2026-09-30T12:00:00Z", "Md Out", b"<p>md</p>", b"placeholder-md")
        (md_out / "explain.md").unlink()
        (md_out / "explain.md").symlink_to(outside / "secret.md")

        (cls.root / "linked-1").symlink_to(linked)

        real_root = base / "real-root"
        _write_session(real_root / "good-1", "2026-09-30T12:00:00Z", RAW_TITLE, HTML_BYTES, MD_BYTES)
        cls.linked_root = base / "linked-root"
        cls.linked_root.symlink_to(real_root)

        cls.server = serve.make_server(cls.root, "127.0.0.1", 0)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls._tmp.cleanup()

    def fetch(self, method, path, host="127.0.0.1", port=None):
        return _fetch(self.port if port is None else port, method, path, host)

    def assert_security(self, headers, csp):
        self.assertEqual(headers.get("x-content-type-options"), "nosniff")
        self.assertEqual(headers.get("referrer-policy"), "no-referrer")
        self.assertEqual(headers.get("cache-control"), "no-store")
        self.assertEqual(headers.get("cross-origin-resource-policy"), "same-origin")
        self.assertEqual(headers.get("content-security-policy"), csp)

    def test_index_escapes_title_and_orders_newest_first(self):
        status, headers, body = self.fetch("GET", "/")
        text = body.decode("utf-8")
        escaped = html.escape(RAW_TITLE, quote=True)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("content-type"), "text/html; charset=utf-8")
        self.assert_security(headers, INDEX_CSP)
        self.assertIn(escaped, text)
        self.assertNotIn("<b>x</b>", text)
        self.assertIn('href="/good-1/"', text)
        self.assertLess(text.find('href="/good-1/"'), text.find('href="/older-1/"'))
        self.assertNotIn("/Bad_Slug/", text)
        self.assertNotIn("/html-out/", text)
        self.assertNotIn("/linked-1/", text)
        self.assertNotIn("/json-out/", text)
        self.assertNotIn("/explain-out/", text)
        self.assertNotIn("Should Skip", text)
        self.assertNotIn("EXPLAIN-OUTSIDE-TITLE", text)
        self.assertNotIn("OUTSIDE-DIR", text)
        self.assertNotIn("Html Out", text)

    def test_page_and_markdown_and_head(self):
        status, headers, body = self.fetch("GET", "/good-1/")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("content-type"), "text/html; charset=utf-8")
        self.assert_security(headers, PAGE_CSP)
        self.assertEqual(body, HTML_BYTES)

        status, headers, body = self.fetch("GET", "/good-1/explain.md")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("content-type"), "text/markdown; charset=utf-8")
        self.assert_security(headers, NONE_CSP)
        self.assertEqual(body, MD_BYTES)

        status, headers, body = self.fetch("HEAD", "/good-1/")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertEqual(headers.get("content-type"), "text/html; charset=utf-8")
        self.assertEqual(headers.get("content-length"), str(len(HTML_BYTES)))
        self.assert_security(headers, PAGE_CSP)

    def test_query_string_ignored_and_single_decode(self):
        status, _headers, body = self.fetch("GET", "/good-1/?q=1")
        self.assertEqual(status, 200)
        self.assertEqual(body, HTML_BYTES)
        status, _headers, body = self.fetch("GET", "/good%2d1/")
        self.assertEqual(status, 200)
        self.assertEqual(body, HTML_BYTES)

    def test_rejected_paths_are_404(self):
        for path in ("/../", "/%2e%2e/", "/%2F/", "/good-1/../x", "/Bad_Slug/", "/nope/", "/good-1"):
            status, headers, body = self.fetch("GET", path)
            self.assertEqual(status, 404, path)
            self.assert_security(headers, NONE_CSP)
            self.assertNotIn(SENTINEL_HTML, body)
            self.assertNotIn(SENTINEL_DIR, body)

    def test_post_and_put_are_405(self):
        for method in ("POST", "PUT"):
            status, headers, _body = self.fetch(method, "/")
            self.assertEqual(status, 405, method)
            self.assert_security(headers, NONE_CSP)

    def test_symlinked_html_outside_is_404(self):
        status, headers, body = self.fetch("GET", "/html-out/")
        self.assertEqual(status, 404)
        self.assert_security(headers, NONE_CSP)
        self.assertNotIn(SENTINEL_HTML, body)

    def test_symlinked_session_dir_outside_is_404(self):
        status, headers, body = self.fetch("GET", "/linked-1/")
        self.assertEqual(status, 404)
        self.assert_security(headers, NONE_CSP)
        self.assertNotIn(SENTINEL_DIR, body)
        index_status, _headers, index = self.fetch("GET", "/")
        self.assertEqual(index_status, 200)
        self.assertNotIn("/linked-1/", index.decode("utf-8"))

    def test_symlinked_session_json_outside_skipped(self):
        status, _headers, body = self.fetch("GET", "/")
        text = body.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertNotIn("/json-out/", text)
        self.assertNotIn("Should Skip", text)

    def test_symlinked_explain_json_outside_skipped(self):
        status, _headers, body = self.fetch("GET", "/")
        text = body.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertNotIn("/explain-out/", text)
        self.assertNotIn("EXPLAIN-OUTSIDE-TITLE", text)
        self.assertNotIn("Local Title", text)

    def test_symlinked_markdown_outside_is_404(self):
        status, headers, body = self.fetch("GET", "/md-out/explain.md")
        self.assertEqual(status, 404)
        self.assert_security(headers, NONE_CSP)
        self.assertNotIn(SENTINEL_MD, body)

    def test_make_server_accepts_all_interfaces(self):
        server = serve.make_server(self.root, "0.0.0.0", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            status, _headers, body = _fetch(server.server_address[1], "GET", "/good-1/")
            self.assertEqual(status, 200)
            self.assertEqual(body, HTML_BYTES)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_make_server_rejects_hostname(self):
        with self.assertRaises(ValueError):
            serve.make_server(self.root, "example.com", 0)

    def test_non_loopback_serve_warns(self):
        warning = io.StringIO()
        with mock.patch.object(serve.ThreadingHTTPServer, "serve_forever"), contextlib.redirect_stderr(warning):
            serve.serve(self.root, "0.0.0.0", 0)
        self.assertRegex(warning.getvalue(), r"^serving on 0\.0\.0\.0:\d+, reachable from the network\n$")

    def test_make_handler_type(self):
        handler = serve.make_handler(self.root)
        self.assertIsInstance(handler, type)
        self.assertTrue(issubclass(handler, http.server.BaseHTTPRequestHandler))

    def test_ipv6_subclass(self):
        server = serve.make_server(self.root, "::1", 0)
        thread = None
        try:
            self.assertIsNot(type(server), ThreadingHTTPServer)
            self.assertTrue(issubclass(type(server), ThreadingHTTPServer))
            self.assertEqual(server.address_family, socket.AF_INET6)
            server.daemon_threads = True
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_address[1]
            status, headers, body = _fetch(port, "GET", "/good-1/", host="::1")
            self.assertEqual(status, 200)
            self.assertEqual(body, HTML_BYTES)
            self.assert_security(headers, PAGE_CSP)
        finally:
            if thread is not None:
                server.shutdown()
                thread.join(timeout=5)
            server.server_close()

    def test_symlinked_root_is_served(self):
        server = serve.make_server(self.linked_root, "127.0.0.1", 0)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            status, _headers, body = _fetch(server.server_address[1], "GET", "/good-1/")
            self.assertEqual(status, 200)
            self.assertEqual(body, HTML_BYTES)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_serve_invokes_serve_forever(self):
        called = {}

        def fake_forever(server_self):
            called["host"] = server_self.server_address[0]
            called["family"] = server_self.address_family

        with mock.patch.object(serve.ThreadingHTTPServer, "serve_forever", fake_forever):
            serve.serve(self.root, "127.0.0.1", 0)
        self.assertEqual(called["host"], "127.0.0.1")
        self.assertEqual(called["family"], socket.AF_INET)

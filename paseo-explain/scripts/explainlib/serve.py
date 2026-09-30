"""Loopback HTTP server for explain sessions."""

import html
import json
import os
import re
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from explainlib.common import SLUG_RE

HTML_TYPE = "text/html; charset=utf-8"
MARKDOWN_TYPE = "text/markdown; charset=utf-8"
CSP_PAGE = (
    "default-src 'none'; script-src 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'unsafe-inline'; img-src data:; font-src 'none'; "
    "connect-src 'none'; base-uri 'none'; form-action 'none'"
)
CSP_INDEX = "default-src 'none'; style-src 'unsafe-inline'"
CSP_NONE = "default-src 'none'"

_NOT_FOUND = b"<!DOCTYPE html><html><head><title>404</title></head><body>Not found</body></html>"
_NOT_ALLOWED = (
    b"<!DOCTYPE html><html><head><title>405</title></head><body>Method not allowed</body></html>"
)
_URI_TOO_LONG = b"<!DOCTYPE html><html><head><title>414</title></head><body>URI too long</body></html>"

_INDEX_RE = re.compile(r"^/$")
_PAGE_RE = re.compile(r"^/([^/]+)/$")
_MD_RE = re.compile(r"^/([^/]+)/explain\.md$")

_ALLOWED_HOSTS = ("127.0.0.1", "::1")


def _inside(root_real: str, path: str) -> bool:
    real = os.path.realpath(path)
    try:
        return os.path.commonpath([root_real, real]) == root_real
    except ValueError:
        return False


def _load_json_inside(root_real: str, path: str):
    if not _inside(root_real, path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _readable_file(root_real: str, path: str) -> bool:
    if not _inside(root_real, path):
        return False
    try:
        with open(path, "rb") as handle:
            handle.read(1)
    except OSError:
        return False
    return True


def _index_entries(root_str: str, root_real: str) -> list:
    try:
        names = os.listdir(root_str)
    except OSError:
        return []
    entries = []
    for name in names:
        if SLUG_RE.fullmatch(name) is None:
            continue
        session_dir = os.path.join(root_str, name)
        if not _inside(root_real, session_dir) or not os.path.isdir(session_dir):
            continue
        session = _load_json_inside(root_real, os.path.join(session_dir, "session.json"))
        if not isinstance(session, dict):
            continue
        html_path = os.path.join(session_dir, "explain.html")
        if not _readable_file(root_real, html_path):
            continue
        explain_path = os.path.join(session_dir, "explain.json")
        if os.path.lexists(explain_path):
            explain = _load_json_inside(root_real, explain_path)
            if not isinstance(explain, dict):
                continue
            title = explain.get("title", "")
            if not isinstance(title, str):
                continue
        else:
            title = ""
        created = session.get("created_at", "")
        if not isinstance(created, str):
            created = ""
        entries.append((created, name, title))
    entries.sort(key=lambda item: item[1])
    entries.sort(key=lambda item: item[0], reverse=True)
    return entries


def _index_page(root_str: str, root_real: str) -> bytes:
    lines = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        "<title>paseo-explain</title>",
        "<style>body{font-family:system-ui,sans-serif;margin:2rem}a{color:inherit}</style>",
        "</head>",
        "<body>",
        "<h1>paseo-explain</h1>",
        "<ul>",
    ]
    for _created, slug, title in _index_entries(root_str, root_real):
        href = html.escape(f"/{slug}/", quote=True)
        text = html.escape(title, quote=True)
        lines.append(f'<li><a href="{href}">{text}</a></li>')
    lines.extend(["</ul>", "</body>", "</html>", ""])
    return "\n".join(lines).encode("utf-8")


class _IPv6Server(ThreadingHTTPServer):
    address_family = socket.AF_INET6


def make_handler(root: Path):
    root_path = Path(root)
    root_real = os.path.realpath(root_path)
    root_str = str(root_path)

    class ExplainHandler(BaseHTTPRequestHandler):
        def handle_one_request(self):
            try:
                self.raw_requestline = self.rfile.readline(65537)
                if len(self.raw_requestline) > 65536:
                    self.requestline = ""
                    self.request_version = ""
                    self.command = ""
                    self._send(414, _URI_TOO_LONG, HTML_TYPE, CSP_NONE, True)
                    return
                if not self.raw_requestline:
                    self.close_connection = True
                    return
                if not self.parse_request():
                    return
                if self.command == "GET":
                    self.do_GET()
                elif self.command == "HEAD":
                    self.do_HEAD()
                else:
                    self.do_POST()
                self.wfile.flush()
            except TimeoutError as exc:
                self.log_error("Request timed out: %r", exc)
                self.close_connection = True

        def do_GET(self):
            self._respond(unquote(urlsplit(self.path).path), True)

        def do_HEAD(self):
            self._respond(unquote(urlsplit(self.path).path), False)

        def do_POST(self):
            self._send(405, _NOT_ALLOWED, HTML_TYPE, CSP_NONE, self.command != "HEAD")

        def send_error(self, code, message=None, explain=None):
            try:
                shortmsg, longmsg = self.responses[code]
            except KeyError:
                shortmsg, longmsg = "???", "???"
            if message is None:
                message = shortmsg
            if explain is None:
                explain = longmsg
            self.log_error("code %d, message %s", code, message)
            content = (self.error_message_format % {
                "code": code,
                "message": html.escape(message, quote=False),
                "explain": html.escape(explain, quote=False),
            }).encode("UTF-8", "replace")
            self._send(int(code), content, HTML_TYPE, CSP_NONE, self.command != "HEAD")

        def _respond(self, path: str, send_body: bool) -> None:
            if _INDEX_RE.match(path):
                self._send(200, _index_page(root_str, root_real), HTML_TYPE, CSP_INDEX, send_body)
                return
            page = _PAGE_RE.match(path)
            if page:
                self._serve_file(page.group(1), "explain.html", HTML_TYPE, CSP_PAGE, send_body)
                return
            markdown = _MD_RE.match(path)
            if markdown:
                self._serve_file(markdown.group(1), "explain.md", MARKDOWN_TYPE, CSP_NONE, send_body)
                return
            self._send(404, _NOT_FOUND, HTML_TYPE, CSP_NONE, send_body)

        def _serve_file(self, slug: str, name: str, content_type: str, csp: str, send_body: bool) -> None:
            if SLUG_RE.fullmatch(slug) is None:
                self._send(404, _NOT_FOUND, HTML_TYPE, CSP_NONE, send_body)
                return
            session_dir = os.path.join(root_str, slug)
            target = os.path.join(session_dir, name)
            if not _inside(root_real, session_dir) or not os.path.isdir(session_dir):
                self._send(404, _NOT_FOUND, HTML_TYPE, CSP_NONE, send_body)
                return
            if not _inside(root_real, target):
                self._send(404, _NOT_FOUND, HTML_TYPE, CSP_NONE, send_body)
                return
            try:
                with open(target, "rb") as handle:
                    data = handle.read()
            except OSError:
                self._send(404, _NOT_FOUND, HTML_TYPE, CSP_NONE, send_body)
                return
            self._send(200, data, content_type, csp, send_body)

        def _send(self, code: int, body: bytes, content_type: str, csp: str, send_body: bool) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("Content-Security-Policy", csp)
            if code == 405:
                self.send_header("Allow", "GET, HEAD")
            self.end_headers()
            if send_body and body:
                self.wfile.write(body)

    return ExplainHandler


def make_server(root, host, port):
    if host not in _ALLOWED_HOSTS:
        raise ValueError("host must be 127.0.0.1 or ::1")
    handler = make_handler(Path(root))
    server_cls = _IPv6Server if host == "::1" else ThreadingHTTPServer
    server = server_cls((host, int(port)), handler)
    server.daemon_threads = True
    return server


def serve(root: str, host: str, port: int) -> None:
    server = make_server(root, host, port)
    try:
        server.serve_forever()
    finally:
        server.server_close()

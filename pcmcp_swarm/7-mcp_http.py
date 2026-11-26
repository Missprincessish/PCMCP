"""Streamable HTTP transport for the PCMCP MCP server (standard library only).

Same tools and same owner gates as the stdio server; only the transport differs.
Safe defaults:
  * binds to 127.0.0.1 only (use --host to change it on purpose)
  * every request needs `Authorization: Bearer <token>` (PCMCP_HTTP_TOKEN, or a
    random token printed once at start)
  * Host and Origin headers are checked (DNS-rebinding protection)
  * stateless: no sessions, no server-initiated streams (GET answers 405)
Nothing here widens what a client may do: approve, real execution and screenshots
stay refused unless the owner turned them on in the config.
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import mcp_server

MAX_BODY = 1_000_000
REQUEST_TIMEOUT = 15          # seconds a client may stall on one connection
MIN_TOKEN_CHARS = 32
_CALL_LOCK = threading.Lock()  # tool calls touch shared files: run one at a time
PATH = "/mcp"
LOOPBACK_NAMES = {"127.0.0.1", "localhost", "[::1]", "::1"}


def _hostname(value: str) -> str:
    value = (value or "").strip().lower()
    if value.startswith("["):
        return value.split("]")[0] + "]"
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def make_handler(token: str, allowed_hosts: set, allowed_origins: set):
    class Handler(BaseHTTPRequestHandler):
        server_version = "pcmcp-mcp"
        protocol_version = "HTTP/1.1"
        timeout = REQUEST_TIMEOUT

        def log_message(self, fmt, *args):  # keep stdout clean; stderr only
            sys.stderr.write("pcmcp-http: " + (fmt % args) + "\n")

        def _send(self, code, body=None, headers=None):
            data = b"" if body is None else json.dumps(body).encode("utf-8")
            self.send_response(code)
            if data:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            if code >= 400:   # a rejected request may leave an unread body: never reuse the connection
                self.send_header("Connection", "close")
                self.close_connection = True
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if data:
                self.wfile.write(data)

        def _guard(self) -> bool:
            host = _hostname(self.headers.get("Host", ""))
            if host not in allowed_hosts:
                self._send(403, {"error": "host not allowed"})
                return False
            origin = self.headers.get("Origin")
            if origin is not None and origin.strip().lower() not in allowed_origins:
                self._send(403, {"error": "origin not allowed"})
                return False
            auth = self.headers.get("Authorization", "")
            given = auth[7:] if auth.lower().startswith("bearer ") else ""
            if not hmac.compare_digest(given.encode(), token.encode()):
                self._send(401, {"error": "missing or wrong bearer token"},
                           {"WWW-Authenticate": 'Bearer realm="pcmcp"'})
                return False
            if self.path.split("?")[0] != PATH:
                self._send(404, {"error": "not found"})
                return False
            return True

        def do_GET(self):
            if self._guard():
                self._send(405, {"error": "no server-initiated stream; POST JSON-RPC to /mcp"}, {"Allow": "POST"})

        do_DELETE = do_GET

        def do_POST(self):
            if not self._guard():
                return
            ver = self.headers.get("MCP-Protocol-Version")
            if ver is not None and ver not in mcp_server.SUPPORTED_VERSIONS:
                return self._send(400, {"error": f"unsupported MCP-Protocol-Version {ver}"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY:
                return self._send(413, {"error": "body too large or missing length"})
            raw = self.rfile.read(length)
            try:
                req = json.loads(raw)
            except json.JSONDecodeError as e:
                return self._send(400, {"jsonrpc": "2.0", "id": None,
                                        "error": {"code": -32700, "message": f"parse error: {e}"}})
            if not isinstance(req, dict):
                return self._send(400, {"jsonrpc": "2.0", "id": None,
                                        "error": {"code": -32600, "message": "send one JSON-RPC object per POST"}})
            if "method" not in req:               # a client response or junk: accept, no reply
                return self._send(202)
            try:
                with _CALL_LOCK:
                    resp = mcp_server.handle(req)
            except Exception as e:  # never leak a traceback to the client; details go to stderr only
                sys.stderr.write(f"pcmcp-http: internal error: {type(e).__name__}\n")
                rid = req.get("id") if isinstance(req.get("id"), (str, int)) else None
                if "id" not in req:
                    return self._send(202)
                return self._send(500, {"jsonrpc": "2.0", "id": rid,
                                        "error": {"code": -32603, "message": "internal error"}})
            if resp is None:                      # notification
                return self._send(202)
            self._send(200, resp)

    return Handler


def serve_http(host: str = "127.0.0.1", port: int = 8765, extra_hosts=(), extra_origins=()) -> int:
    token = os.environ.get("PCMCP_HTTP_TOKEN") or ""
    generated = False
    if not token:
        token, generated = secrets.token_urlsafe(32), True
    if len(token) < MIN_TOKEN_CHARS:
        sys.exit(f"error: PCMCP_HTTP_TOKEN must be at least {MIN_TOKEN_CHARS} characters")
    allowed_hosts = set(LOOPBACK_NAMES) | {h.lower() for h in extra_hosts}
    if host not in LOOPBACK_NAMES and host not in ("0.0.0.0", "::"):
        allowed_hosts.add(host.lower())
    allowed_origins = {f"http://{h}:{port}" for h in ("127.0.0.1", "localhost")}
    allowed_origins |= {o.lower() for o in extra_origins}
    srv = ThreadingHTTPServer((host, port), make_handler(token, allowed_hosts, allowed_origins))
    sys.stderr.write(f"pcmcp MCP (Streamable HTTP) on http://{host}:{srv.server_address[1]}{PATH}\n")
    if generated:
        sys.stderr.write(f"bearer token (shown once): {token}\n")
    if host not in LOOPBACK_NAMES:
        sys.stderr.write("WARNING: not bound to loopback. Anyone who can reach this port and has the token "
                         "can call the tools. Put it behind TLS.\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0

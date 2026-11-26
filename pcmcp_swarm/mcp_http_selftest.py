"""Selftest for the Streamable HTTP MCP transport: python3 -m pcmcp_swarm.mcp_http_selftest"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import urllib.error
import urllib.request

TOKEN = "t" * 32


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="pcmcp_http_selftest_")
    os.environ["HOME"] = tmp
    os.environ["PCMCP_CONFIG"] = os.path.join(tmp, "config.json")
    from http.server import ThreadingHTTPServer
    from . import mcp_http
    srv = ThreadingHTTPServer(("127.0.0.1", 0), mcp_http.make_handler(
        TOKEN, set(mcp_http.LOOPBACK_NAMES), set()))
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/mcp"
    results = []

    def call(body=None, headers=None, method="POST", auth=True, raw=None):
        h = {"Content-Type": "application/json"}
        if auth:
            h["Authorization"] = f"Bearer {TOKEN}"
        h.update(headers or {})
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(url, data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                txt = r.read().decode()
                return r.status, (json.loads(txt) if txt else None)
        except urllib.error.HTTPError as e:
            txt = e.read().decode()
            try:
                return e.code, json.loads(txt) if txt else None
            except json.JSONDecodeError:
                return e.code, txt

    def check(name, ok):
        results.append(ok)
        print(("[PASS] " if ok else "[FAIL] ") + name)

    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
    st, r = call(init)
    check("initialize returns serverInfo pcmcp and requested protocol", st == 200 and r["result"]["serverInfo"]["name"] == "pcmcp" and r["result"]["protocolVersion"] == "2025-06-18")
    st, r = call({"jsonrpc": "2.0", "method": "notifications/initialized"})
    check("notification gets 202 with no body", st == 202 and r is None)
    st, r = call({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, {"MCP-Protocol-Version": "2025-06-18"})
    names = {t["name"] for t in r["result"]["tools"]} if st == 200 else set()
    check("tools/list lists the swarm tools", {"swarm_status", "swarm_meter", "swarm_approve"} <= names)
    st, r = call({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "swarm_approve", "arguments": {"ref": "latest"}}})
    check("owner gate still refuses approve over HTTP", st == 200 and r["result"].get("isError") is True)
    st, r = call({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "swarm_screenshot", "arguments": {}}})
    check("screenshot gate still refuses over HTTP", st == 200 and r["result"].get("isError") is True)
    st, _ = call(init, auth=False)
    check("no token -> 401", st == 401)
    st, _ = call(init, headers={"Authorization": "Bearer wrong"})
    check("wrong token -> 401", st == 401)
    st, _ = call(init, headers={"Origin": "https://evil.example"})
    check("foreign Origin -> 403", st == 403)
    st, _ = call(init, headers={"Host": "evil.example"})
    check("foreign Host -> 403", st == 403)
    st, _ = call(init, headers={"MCP-Protocol-Version": "1999-01-01"})
    check("unsupported MCP-Protocol-Version -> 400", st == 400)
    st, _ = call(method="GET")
    check("GET -> 405", st == 405)
    st, r = call(raw=b"{not json")
    check("bad JSON -> 400 parse error", st == 400 and r["error"]["code"] == -32700)
    st, _ = call(init, headers={"Content-Length": "5000000"}, raw=b"{}")
    check("oversize body -> rejected", st in (400, 413))
    import http.client
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("POST", "/mcp", body=json.dumps(init), headers={"Content-Type": "application/json"})
    r = c.getresponse(); r.read()
    check("rejected request with a body closes the connection (no keep-alive desync)",
          r.status == 401 and (r.getheader("Connection") or "").lower() == "close")
    c.close()
    from unittest import mock
    with mock.patch.object(mcp_http.mcp_server, "handle", side_effect=RuntimeError("secret detail /etc/x")):
        st, r = call({"jsonrpc": "2.0", "id": 9, "method": "tools/list"})
    check("internal error -> JSON-RPC -32603, no detail leaked", st == 500 and r["error"]["code"] == -32603 and "secret" not in json.dumps(r))
    srv.shutdown()
    print(f"\n{sum(results)}/{len(results)} checks passed")
    print("OVERALL: " + ("PASS" if all(results) else "FAIL"))
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Local doorbell listeners (cross-OS).

local_spool: any local program rings by writing a small file into the spool folder
             (`python3 -m pcmcp_swarm ring "<trigger>"`, a macOS Shortcut, a cron job...).
local_http:  POST the trigger text to http://127.0.0.1:<port>/ring. Bound to loopback only.
"""
from __future__ import annotations

import queue
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import DoorbellChannel

MAX_RING_BYTES = 512


def spool_ring(cfg: dict, text: str) -> Path:
    d = Path(cfg["doorbell"]["local_spool"]["dir"]).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"ring_{uuid.uuid4().hex}.txt"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(text[:MAX_RING_BYTES], encoding="utf-8")
    tmp.replace(p)
    return p


class LocalSpool(DoorbellChannel):
    name = "local_spool"

    def __init__(self, cfg):
        super().__init__(cfg)
        self.dir = Path(cfg["doorbell"]["local_spool"]["dir"]).expanduser()
        self.dir.mkdir(parents=True, exist_ok=True)

    def poll(self):
        rings = []
        for p in sorted(self.dir.glob("ring_*.txt")):
            try:
                with open(p, "rb") as f:
                    text = f.read(MAX_RING_BYTES).decode("utf-8", "replace")
                p.unlink()
            except OSError:
                continue
            rings.append({"channel": self.name, "sender": "local", "text": text, "ref": p.name})
        return rings


class LocalHttp(DoorbellChannel):
    name = "local_http"

    def __init__(self, cfg):
        super().__init__(cfg)
        self.q = queue.Queue(maxsize=1000)
        q = self.q

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                if self.path != "/ring":
                    self.send_response(404)
                    self.end_headers()
                    return
                n = min(int(self.headers.get("Content-Length") or 0), MAX_RING_BYTES)
                text = self.rfile.read(n).decode("utf-8", "replace")
                try:
                    q.put_nowait({"channel": "local_http", "sender": self.client_address[0], "text": text, "ref": "http"})
                except queue.Full:
                    pass
                self.send_response(202)  # same answer for valid and junk: reveals nothing
                self.end_headers()

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", int(cfg["doorbell"]["local_http"]["port"])), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def poll(self):
        rings = []
        while not self.q.empty():
            rings.append(self.q.get_nowait())
        return rings

    def close(self):
        self.server.shutdown()

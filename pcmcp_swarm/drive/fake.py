"""A local, in-memory fake of the parts of Google Drive API v3 the swarm uses. For offline proof only.

Implements: files.list (q subset + paging), files.get, files.create (folders only), files.update
(addParents/removeParents), files.copy, and the /batch/drive/v3 multipart endpoint.
Refuses (and counts) DELETE, trash and content downloads, so tests can prove none were attempted.
Can inject rate-limit answers (429 / 403 userRateLimitExceeded) every Nth write, and add latency.

Run standalone for rehearsal:  python3 -m pcmcp_swarm drive fake-server --port 8799 --seed demo
"""
from __future__ import annotations

import hashlib
import http.server
import json
import re
import threading
import time
import urllib.parse
from socketserver import ThreadingMixIn

from . import FOLDER_MIME
from ..util import canonical_json, sha256_bytes

_TS = "2026-01-01T00:00:00.000Z"


class _Srv(ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class FakeDrive:
    def __init__(self, rate_limit_every: int = 0, latency_ms: int = 0, fail_first_batches: int = 0):
        self.fail_first_batches = fail_first_batches   # whole-batch 503s, to exercise chunk reassignment
        self.fail_list_folders = set()                 # folder ids whose listing always answers 500 (incomplete-inventory test)
        self.max_concurrent = 0
        self._concurrent = 0
        self.lock = threading.RLock()
        self.files = {}
        self.n = 0
        self.root = "ROOTFOLDER"
        self.files[self.root] = {"id": self.root, "name": "My Drive", "mimeType": FOLDER_MIME, "parents": [],
                                 "createdTime": _TS, "modifiedTime": _TS, "trashed": False, "ownedByMe": True}
        self.rate_limit_every = rate_limit_every
        self.latency = latency_ms / 1000.0
        self.c = {"writes_applied": 0, "write_attempts": 0, "rate_limited": 0, "reads": 0, "batch_requests": 0,
                  "max_batch_calls": 0, "forbidden_attempts": 0, "single_requests": 0}
        self.srv = None

    # ---- seeding
    def _id(self, prefix="f"):
        self.n += 1
        return f"{prefix}{self.n:06d}x" + hashlib.sha1(str(self.n).encode()).hexdigest()[:10]

    def add_folder(self, name, parent=None):
        with self.lock:
            fid = self._id("d")
            self.files[fid] = {"id": fid, "name": name, "mimeType": FOLDER_MIME, "parents": [parent or self.root],
                               "createdTime": _TS, "modifiedTime": _TS, "trashed": False, "ownedByMe": True}
            return fid

    def add_file(self, name, parent=None, content=b"", mime="application/octet-stream", **extra):
        with self.lock:
            fid = self._id("f")
            self.files[fid] = {"id": fid, "name": name, "mimeType": mime, "parents": [parent or self.root],
                               "size": str(len(content)), "md5Checksum": hashlib.md5(content).hexdigest(),
                               "createdTime": _TS, "modifiedTime": _TS, "trashed": False, "ownedByMe": True, **extra}
            return fid

    # ---- state proof
    def listing(self, under=None) -> dict:
        with self.lock:
            ids = self.descendants(under) if under else set(self.files)
            return {i: {k: self.files[i].get(k) for k in ("name", "mimeType", "parents", "md5Checksum", "size")}
                    for i in sorted(ids)}

    def state_hash(self) -> str:
        return sha256_bytes(canonical_json(self.listing()))

    def descendants(self, root):
        with self.lock:
            kids = {}
            for f in self.files.values():
                for p in f.get("parents") or []:
                    kids.setdefault(p, []).append(f["id"])
            out, stack = set(), [root]
            while stack:
                for k in kids.get(stack.pop(), []):
                    if k not in out:
                        out.add(k)
                        stack.append(k)
            return out

    # ---- REST dispatch: returns (status, body_dict)
    def _caps(self, f):
        caps = {"canEdit": True, "canMoveItemWithinDrive": True, "canAddChildren": f["mimeType"] == FOLDER_MIME}
        caps.update(f.get("capabilities") or {})
        return caps

    def _out(self, f):
        o = {k: v for k, v in f.items() if k != "capabilities"}
        o["capabilities"] = self._caps(f)
        return o

    def _rate_limited(self):
        self.c["write_attempts"] += 1
        if self.rate_limit_every and self.c["write_attempts"] % self.rate_limit_every == 0:
            self.c["rate_limited"] += 1
            if (self.c["rate_limited"] % 2) == 0:
                return (403, {"error": {"code": 403, "message": "User Rate Limit Exceeded",
                                        "errors": [{"reason": "userRateLimitExceeded"}]}})
            return (429, {"error": {"code": 429, "message": "Rate Limit Exceeded", "errors": [{"reason": "rateLimitExceeded"}]}})
        return None

    def dispatch(self, method, path, params, body):
        with self.lock:
            p = urllib.parse.urlparse(path).path
            m = method.upper()
            if m == "DELETE" or "/trash" in p or params.get("alt") == "media" or "/export" in p \
                    or (isinstance(body, dict) and "trashed" in body):
                self.c["forbidden_attempts"] += 1
                return (405, {"error": {"code": 405, "message": "refused by fake: delete/trash/download"}})
            mt = re.fullmatch(r"/drive/v3/files/([^/]+)/copy", p)
            if mt and m == "POST":
                rl = self._rate_limited()
                if rl:
                    return rl
                src = self.files.get(self._alias(mt.group(1)))
                if not src:
                    return (404, {"error": {"code": 404, "message": "File not found"}})
                nid = self._id("c")
                new = dict(src, id=nid, name=(body or {}).get("name", src["name"]),
                           parents=list((body or {}).get("parents") or src["parents"]))
                self.files[nid] = new
                self.c["writes_applied"] += 1
                return (200, self._out(new))
            mt = re.fullmatch(r"/drive/v3/files/([^/]+)", p)
            if mt and m == "GET":
                self.c["reads"] += 1
                f = self.files.get(self._alias(mt.group(1)))
                return (200, self._out(f)) if f and not f["trashed"] else (404, {"error": {"code": 404, "message": "File not found"}})
            if mt and m == "PATCH":
                rl = self._rate_limited()
                if rl:
                    return rl
                f = self.files.get(self._alias(mt.group(1)))
                if not f:
                    return (404, {"error": {"code": 404, "message": "File not found"}})
                if not self._caps(f)["canMoveItemWithinDrive"]:
                    return (403, {"error": {"code": 403, "message": "Insufficient permissions", "errors": [{"reason": "insufficientFilePermissions"}]}})
                add = [self._alias(x) for x in str(params.get("addParents", "")).split(",") if x]
                rem = [self._alias(x) for x in str(params.get("removeParents", "")).split(",") if x]
                for a in add:
                    if a not in self.files:
                        return (404, {"error": {"code": 404, "message": f"Parent not found: {a}"}})
                parents = [x for x in f["parents"] if x not in rem]
                for a in add:
                    if a not in parents:
                        parents.append(a)
                f["parents"] = parents
                if isinstance(body, dict) and "name" in body:
                    f["name"] = body["name"]
                self.c["writes_applied"] += 1
                return (200, self._out(f))
            if p == "/drive/v3/files" and m == "POST":
                rl = self._rate_limited()
                if rl:
                    return rl
                if (body or {}).get("mimeType") != FOLDER_MIME:
                    return (400, {"error": {"code": 400, "message": "fake only creates folders"}})
                parent = self._alias(((body or {}).get("parents") or [self.root])[0])
                if parent not in self.files:
                    return (404, {"error": {"code": 404, "message": "Parent not found"}})
                fid = self.add_folder(body["name"], parent)
                self.c["writes_applied"] += 1
                return (200, self._out(self.files[fid]))
            if p == "/drive/v3/files" and m == "GET":
                self.c["reads"] += 1
                mm = re.search(r"'([^']+)' in parents", params.get("q", ""))
                if mm and self._alias(mm.group(1)) in self.fail_list_folders:
                    return (500, {"error": {"code": 500, "message": "backend error (injected, permanent)"}})
                return (200, self._list(params))
            return (404, {"error": {"code": 404, "message": f"no route {m} {p}"}})

    def _alias(self, fid):
        return self.root if fid == "root" else fid

    def _list(self, params):
        q = params.get("q", "")
        clauses = [c.strip() for c in re.split(r"\s+and\s+", q) if c.strip()]
        def ok(f):
            for c in clauses:
                if c == "trashed=false" or c == "trashed = false":
                    if f["trashed"]:
                        return False
                elif (mm := re.fullmatch(r"'(.+)' in parents", c)):
                    if self._alias(mm.group(1)) not in (f.get("parents") or []):
                        return False
                elif (mm := re.fullmatch(r"name\s*=\s*'((?:[^'\\]|\\.)*)'", c)):
                    if f["name"] != mm.group(1).replace("\\'", "'").replace("\\\\", "\\"):
                        return False
                elif (mm := re.fullmatch(r"mimeType\s*=\s*'(.+)'", c)):
                    if f["mimeType"] != mm.group(1):
                        return False
                else:
                    raise ValueError(f"fake: unsupported q clause {c!r}")
            return True
        items = [self._out(f) for i, f in sorted(self.files.items()) if i != self.root and ok(f)]
        start = int(params.get("pageToken") or 0)
        size = int(params.get("pageSize") or 100)
        page = items[start:start + size]
        out = {"files": page}
        if start + size < len(items):
            out["nextPageToken"] = str(start + size)
        return out

    # ---- HTTP server
    def start(self, port: int = 0) -> str:
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _send(self, status, body: bytes, ctype="application/json"):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _handle(self):
                u = urllib.parse.urlparse(self.path)
                params = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                with fake.lock:
                    fake._concurrent += 1
                    fake.max_concurrent = max(fake.max_concurrent, fake._concurrent)
                try:
                    if fake.latency:
                        time.sleep(fake.latency)
                finally:
                    with fake.lock:
                        fake._concurrent -= 1
                if u.path == "/batch/drive/v3" and self.command == "POST":
                    return self._batch(raw)
                fake.c["single_requests"] += 1
                try:
                    body = json.loads(raw) if raw else None
                except json.JSONDecodeError:
                    body = None
                status, out = fake.dispatch(self.command, u.path, params, body)
                self._send(status, json.dumps(out).encode())

            def _batch(self, raw):
                ctype = self.headers.get("Content-Type", "")
                b = ctype.split("boundary=", 1)[-1].strip('"')
                parts = [x for x in raw.decode().split("--" + b) if x.strip() and x.strip() != "--"]
                fake.c["batch_requests"] += 1
                fake.c["max_batch_calls"] = max(fake.c["max_batch_calls"], len(parts))
                with fake.lock:
                    is_write = any(re.search(r"^(PATCH|POST) ", p.strip("\r\n").partition("\r\n\r\n")[2], re.M) for p in parts)
                    if is_write and fake.fail_first_batches > 0:
                        fake.fail_first_batches -= 1
                        fake.c["failed_batches"] = fake.c.get("failed_batches", 0) + 1
                        return self._send(503, b'{"error":{"code":503,"message":"backend error (injected)"}}')
                if len(parts) > 100:
                    return self._send(400, b'{"error":{"code":400,"message":"too many calls in batch"}}')
                rb = "batch_fake_reply"
                out = []
                for part in parts:
                    head, _, rest = part.strip("\r\n").partition("\r\n\r\n")
                    cid = re.search(r"Content-ID:\s*<([^>]+)>", head, re.I)
                    cid = cid.group(1) if cid else "item-x"
                    req_line, _, after = rest.partition("\r\n")
                    method, target = req_line.split()[0], req_line.split()[1]
                    _, _, body_txt = after.partition("\r\n\r\n")
                    body_txt = body_txt.strip()
                    tu = urllib.parse.urlparse(target)
                    params = {k: v[0] for k, v in urllib.parse.parse_qs(tu.query).items()}
                    try:
                        body = json.loads(body_txt) if body_txt else None
                    except json.JSONDecodeError:
                        body = None
                    status, res = fake.dispatch(method, tu.path, params, body)
                    j = json.dumps(res)
                    out.append(f"--{rb}\r\nContent-Type: application/http\r\nContent-ID: <response-{cid}>\r\n\r\n"
                               f"HTTP/1.1 {status} X\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{j}\r\n")
                payload = ("".join(out) + f"--{rb}--\r\n").encode()
                self._send(200, payload, f"multipart/mixed; boundary={rb}")

            do_GET = do_POST = do_PATCH = do_DELETE = _handle

            def log_message(self, *a):
                pass

        self.srv = _Srv(("127.0.0.1", port), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.srv.server_port}"

    def stop(self):
        if self.srv:
            self.srv.shutdown()
            self.srv.server_close()


def seed_demo(fake: FakeDrive, n_files: int = 600) -> str:
    """A messy test folder for rehearsal and tests. Returns the test folder id.
    Also puts a few files OUTSIDE the test folder so tests can prove the swarm never touches them."""
    test = fake.add_folder("PCMCP_DRIVE_TEST")
    olds = [fake.add_folder(n, test) for n in ("College", "Old Downloads", "misc", "Stuff 2019")]
    deep = fake.add_folder("nested", olds[2])
    kinds = [("photo_{i}.jpg", "image/jpeg"), ("lecture_{i}.pdf", "application/pdf"), ("scan_{i}.png", "image/png"),
             ("notes_{i}.txt", "text/plain"), ("model_{i}.glb", "model/gltf-binary"), ("budget_{i}.xlsx", "application/vnd.ms-excel")]
    places = [test] + olds + [deep]
    for i in range(n_files):
        name, mime = kinds[i % len(kinds)]
        fake.add_file(name.format(i=i), places[i % len(places)], f"content-{i}".encode(), mime)
    # rule-bait files used by validator tests
    fake.add_file("synomize_world_v3.json", olds[1], b"synomize", "application/json")
    fake.add_file("ASH notes.txt", olds[1], b"ash", "text/plain")
    syn = fake.add_folder("Synomize old export", olds[3])
    fake.add_file("tree_asset.glb", syn, b"glb", "model/gltf-binary")
    legal = fake.add_folder("LEGAL PROOF - DO NOT TOUCH", test)
    fake.add_file("Halo_2026-07-22.zip", legal, b"proof", "application/zip")
    # outside the test folder: must never be touched
    for i in range(5):
        fake.add_file(f"outside_{i}.doc", None, f"outside-{i}".encode())
    return test

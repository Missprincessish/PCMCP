"""Google Drive API v3 client. Python stdlib only.

SAFETY rules enforced here, for every request:
  - no DELETE method, no trash, no `trashed` field: nothing is ever deleted
  - no `alt=media` and no export: file bytes are never downloaded (contents are never opened)
  - moves are metadata-only PATCH calls (addParents / removeParents)
Rate limits (429, or 403 with a rate-limit reason) are retried with exponential backoff and counted
as retries, never as errors.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import random
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from pathlib import Path

from ..util import read_json

RATE_REASONS = {"rateLimitExceeded", "userRateLimitExceeded"}
TRANSIENT = {500, 502, 503, 504}
FILE_FIELDS = "id,name,mimeType,size,parents,createdTime,modifiedTime,md5Checksum,trashed,ownedByMe,capabilities(canEdit,canMoveItemWithinDrive,canAddChildren)"


class DriveError(Exception):
    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {str(body)[:300]}")
        self.status, self.body = status, body


class ForbiddenOperation(Exception):
    """Raised before any network call when a request would delete, trash, or download."""


def _reason(body) -> str:
    try:
        errs = body.get("error", {}).get("errors") or []
        return errs[0].get("reason", "") if errs else body.get("error", {}).get("status", "")
    except AttributeError:
        return ""


def is_rate_limited(status: int, body) -> bool:
    return status == 429 or (status == 403 and _reason(body) in RATE_REASONS)


def guard(method: str, path: str, params: dict | None, body) -> None:
    """SAFETY: the one gate every request passes. Refuses delete, trash and download."""
    m = method.upper()
    p = path.lower()
    q = {k.lower(): str(v).lower() for k, v in (params or {}).items()}
    if m == "DELETE":
        raise ForbiddenOperation("DELETE is not allowed: nothing is ever deleted")
    if "/trash" in p or "emptytrash" in p:
        raise ForbiddenOperation("trash endpoints are not allowed")
    if q.get("alt") == "media" or "/export" in p or "alt=media" in p:
        raise ForbiddenOperation("downloading file contents is not allowed")
    if isinstance(body, dict) and "trashed" in body:
        raise ForbiddenOperation("setting 'trashed' is not allowed")
    if m not in ("GET", "POST", "PATCH"):
        raise ForbiddenOperation(f"method {m} is not allowed")


class Throttle:
    """Shared token bucket: at most `per_minute` edits across every sub-bot, plus a global cooldown
    after a rate-limit answer so all bots back off together."""

    def __init__(self, per_minute: int):
        self.rate = max(1.0, per_minute / 60.0)
        self.capacity = max(1.0, min(per_minute / 60.0 * 2, 200.0))
        self.tokens = self.capacity
        self.last = time.monotonic()
        self.cool_until = 0.0
        self.lock = threading.Lock()

    def take(self, n: int = 1) -> None:
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
                self.last = now
                wait = max(0.0, self.cool_until - now)
                if wait == 0 and self.tokens >= min(n, self.capacity):
                    self.tokens -= min(n, self.capacity)
                    return
                if wait == 0:
                    wait = (min(n, self.capacity) - self.tokens) / self.rate
            time.sleep(min(wait, 1.0))

    def cooldown(self, seconds: float) -> None:
        with self.lock:
            self.cool_until = max(self.cool_until, time.monotonic() + seconds)


# ---------------------------------------------------------------- OAuth (Angie signs in once)

def _client_info(dcfg: dict) -> dict:
    data = read_json(Path(dcfg["oauth_client_file"]).expanduser())
    if not data:
        raise FileNotFoundError(f"OAuth client file missing: {dcfg['oauth_client_file']} (see CONTROL.md, 'One-time setup')")
    info = data.get("installed") or data.get("web")
    if not info:
        raise ValueError("OAuth client file must be a Desktop-app client (has an 'installed' section)")
    return info


def authorize(dcfg: dict, open_browser: bool = True, timeout: int = 300) -> Path:
    """Loopback + PKCE sign-in. Angie signs in in her browser; the token is saved with mode 0600."""
    info = _client_info(dcfg)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    got = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if qs.get("state", [""])[0] == state:
                got.update({k: v[0] for k, v in qs.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"PCMCP: sign-in received. You can close this tab.")

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    redirect = f"http://127.0.0.1:{srv.server_port}"
    url = info.get("auth_uri", "https://accounts.google.com/o/oauth2/v2/auth") + "?" + urllib.parse.urlencode({
        "client_id": info["client_id"], "redirect_uri": redirect, "response_type": "code",
        "scope": dcfg["scope"], "code_challenge": challenge, "code_challenge_method": "S256",
        "access_type": "offline", "prompt": "consent", "state": state})
    print("Sign in to Google in your browser. If it does not open, paste this URL:\n" + url)
    if open_browser:
        webbrowser.open(url)
    srv.timeout = 2
    end = time.time() + timeout
    while "code" not in got and "error" not in got and time.time() < end:
        srv.handle_request()
    srv.server_close()
    if "code" not in got:
        raise RuntimeError(f"sign-in did not complete: {got.get('error', 'timed out')}")
    tok = _token_post(info, {"code": got["code"], "code_verifier": verifier, "grant_type": "authorization_code",
                             "redirect_uri": redirect})
    return _save_token(dcfg, tok)


def _token_post(info: dict, form: dict) -> dict:
    form = dict(form, client_id=info["client_id"])
    if info.get("client_secret"):
        form["client_secret"] = info["client_secret"]
    req = urllib.request.Request(info.get("token_uri", "https://oauth2.googleapis.com/token"),
                                 data=urllib.parse.urlencode(form).encode(), method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def _save_token(dcfg: dict, tok: dict, keep_refresh: str | None = None) -> Path:
    p = Path(dcfg["token_file"]).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    rec = {"access_token": tok["access_token"], "refresh_token": tok.get("refresh_token") or keep_refresh,
           "expires_at": time.time() + int(tok.get("expires_in", 3600)) - 60, "scope": tok.get("scope")}
    fd = os.open(str(p) + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(rec, f)
    os.replace(str(p) + ".tmp", p)
    return p


# ---------------------------------------------------------------- client

class DriveClient:
    def __init__(self, dcfg: dict, on_retry=None, read_only: bool = False):
        self.dcfg = dcfg
        self.read_only = read_only      # SAFETY: inventory and dry run use a client that cannot write
        self.base = dcfg["api_base"].rstrip("/")
        self.throttle = Throttle(int(dcfg["edits_per_minute"]))
        self.read_throttle = Throttle(int(dcfg.get("reads_per_minute", 12000)))
        self.max_retries = int(dcfg["max_retries"])
        self.b0 = float(dcfg["backoff_base_seconds"])
        self.bmax = float(dcfg["backoff_max_seconds"])
        self.on_retry = on_retry or (lambda kind, n=1: None)
        self._tok_lock = threading.Lock()
        self._tok = None
        self.requests_sent = 0

    # -- auth
    def _auth_header(self) -> dict:
        if self.dcfg.get("auth") == "none":
            return {}
        with self._tok_lock:
            if self._tok is None:
                self._tok = read_json(Path(self.dcfg["token_file"]).expanduser())
                if not self._tok:
                    raise FileNotFoundError("not signed in: run `python3 -m pcmcp_swarm drive auth` (Angie, once)")
            if time.time() >= self._tok["expires_at"]:
                info = _client_info(self.dcfg)
                new = _token_post(info, {"refresh_token": self._tok["refresh_token"], "grant_type": "refresh_token"})
                _save_token(self.dcfg, new, keep_refresh=self._tok["refresh_token"])
                self._tok = read_json(Path(self.dcfg["token_file"]).expanduser())
            return {"Authorization": f"Bearer {self._tok['access_token']}"}

    def backoff(self, attempt: int) -> float:
        return min(self.bmax, self.b0 * (2 ** attempt)) * (0.5 + random.random() / 2)

    # -- single request, with retry
    def _ro(self, method: str):
        if self.read_only and method.upper() != "GET":
            raise ForbiddenOperation(f"read-only client refused {method} (inventory / dry run never change Drive)")

    def request(self, method: str, path: str, params: dict | None = None, body=None, write: bool = False):
        guard(method, path, params, body)
        self._ro(method)
        url = self.base + path + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(self.max_retries + 1):
            (self.throttle if write else self.read_throttle).take(1)
            headers = {"Content-Type": "application/json", **self._auth_header()}
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                self.requests_sent += 1
                with urllib.request.urlopen(req, timeout=120) as r:
                    raw = r.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                raw = e.read()
                try:
                    b = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    b = {"raw": raw[:300].decode("utf-8", "replace")}
                if attempt < self.max_retries and (is_rate_limited(e.code, b) or e.code in TRANSIENT):
                    kind = "rate_limit" if is_rate_limited(e.code, b) else "transient"
                    self.on_retry(kind)
                    d = self.backoff(attempt)
                    if kind == "rate_limit":
                        self.throttle.cooldown(d)
                    time.sleep(d)
                    continue
                raise DriveError(e.code, b)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if attempt < self.max_retries:
                    self.on_retry("transient")
                    time.sleep(self.backoff(attempt))
                    continue
                raise DriveError(0, {"network": str(e)})
        raise DriveError(0, "retries exhausted")

    # -- reads (metadata only)
    def get(self, file_id: str, fields: str = FILE_FIELDS) -> dict:
        return self.request("GET", f"/drive/v3/files/{urllib.parse.quote(file_id)}", {"fields": fields, "supportsAllDrives": "false"})

    def list_query(self, q: str, fields: str = FILE_FIELDS) -> list:
        out, token = [], None
        while True:
            params = {"q": q, "pageSize": 1000, "fields": f"nextPageToken,files({fields})", "spaces": "drive"}
            if token:
                params["pageToken"] = token
            r = self.request("GET", "/drive/v3/files", params)
            out.extend(r.get("files", []))
            token = r.get("nextPageToken")
            if not token:
                return out

    # -- batch (<= 100 calls per HTTP request). Returns [(status, body)] in input order.
    def batch_once(self, calls: list) -> list:
        if not calls:
            return []
        if len(calls) > 100:
            raise ValueError("Google allows at most 100 calls per batch")
        for c in calls:
            guard(c["method"], c["path"], c.get("params"), c.get("body"))
            self._ro(c["method"])
        writes = sum(1 for c in calls if c["method"] != "GET")
        if writes:
            self.throttle.take(writes)
        boundary = "pcmcp_" + uuid.uuid4().hex
        parts = []
        for i, c in enumerate(calls):
            qs = ("?" + urllib.parse.urlencode(c["params"], doseq=True)) if c.get("params") else ""
            b = json.dumps(c.get("body") or {}) if c["method"] != "GET" else ""
            parts.append(f"--{boundary}\r\nContent-Type: application/http\r\nContent-ID: <item-{i}>\r\n\r\n"
                         f"{c['method']} {c['path']}{qs} HTTP/1.1\r\n"
                         + ("Content-Type: application/json; charset=UTF-8\r\n" if b else "")
                         + f"\r\n{b}\r\n")
        payload = ("".join(parts) + f"--{boundary}--\r\n").encode()
        req = urllib.request.Request(self.base + "/batch/drive/v3", data=payload, method="POST",
                                     headers={"Content-Type": f"multipart/mixed; boundary={boundary}", **self._auth_header()})
        self.requests_sent += 1
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                ctype = r.headers.get("Content-Type", "")
                return parse_batch_response(r.read(), ctype, len(calls))
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                b = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                b = {"raw": raw[:300].decode("utf-8", "replace")}
            return [(e.code, b)] * len(calls)          # whole batch refused: every item gets that answer
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            return [(0, {"network": str(e)})] * len(calls)

    def batch(self, calls: list) -> tuple:
        """Send a batch; re-send only the items that were rate-limited or hit a transient error, with
        backoff. Returns (results, retry_counts)."""
        results = [None] * len(calls)
        todo = list(range(len(calls)))
        retries = {"rate_limit": 0, "transient": 0}
        for attempt in range(self.max_retries + 1):
            answers = self.batch_once([calls[i] for i in todo])
            again = []
            for i, (status, body) in zip(todo, answers):
                rate = is_rate_limited(status, body)
                if attempt < self.max_retries and (rate or status in TRANSIENT or status == 0):
                    kind = "rate_limit" if rate else "transient"
                    retries[kind] += 1
                    self.on_retry(kind)
                    again.append(i)
                results[i] = (status, body)
            if not again:
                break
            d = self.backoff(attempt)
            if retries["rate_limit"]:
                self.throttle.cooldown(d)
            time.sleep(d)
            todo = again
        return results, retries


def parse_batch_response(raw: bytes, content_type: str, n: int) -> list:
    """Parse a multipart/mixed batch reply into [(status, json_body)] ordered by Content-ID."""
    boundary = None
    for piece in content_type.split(";"):
        piece = piece.strip()
        if piece.startswith("boundary="):
            boundary = piece[len("boundary="):].strip('"')
    if not boundary:
        raise DriveError(0, f"batch reply without boundary: {content_type}")
    text = raw.decode("utf-8", "replace")
    out = {}
    order = []
    for part in text.split("--" + boundary):
        part = part.strip("\r\n")
        if not part or part == "--":
            continue
        head, _, rest = part.partition("\r\n\r\n")
        cid = None
        for line in head.split("\r\n"):
            if line.lower().startswith("content-id:"):
                v = line.split(":", 1)[1].strip().strip("<>")
                cid = int(v.rsplit("-", 1)[-1]) if v.rsplit("-", 1)[-1].isdigit() else None
        status_line, _, after = rest.partition("\r\n")
        try:
            status = int(status_line.split()[1])
        except (IndexError, ValueError):
            status = 0
        _, _, body_txt = after.partition("\r\n\r\n")
        body_txt = body_txt.strip()
        try:
            body = json.loads(body_txt) if body_txt else {}
        except json.JSONDecodeError:
            body = {"raw": body_txt[:300]}
        if cid is None:
            cid = len(order)
        order.append(cid)
        out[cid] = (status, body)
    return [out.get(i, (0, {"missing": f"no reply for item {i}"})) for i in range(n)]

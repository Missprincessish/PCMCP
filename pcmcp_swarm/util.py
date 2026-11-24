"""Shared, dependency-free helpers: hashing, canonical JSON, atomic writes, path safety."""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import threading
from pathlib import Path

_append_lock = threading.Lock()


def now_iso() -> str:
    """UTC timestamp, ISO-8601 with 'Z'. Timestamps are recorded, never used to decide anything."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_iso(s: str) -> _dt.datetime:
    return _dt.datetime.strptime(s.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S.%f%z") if "." in s \
        else _dt.datetime.strptime(s.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z")


def canonical_json(obj) -> bytes:
    """Canonical encoding used for every hash and every signature (see SPEC.md §2).

    sort_keys + compact separators + UTF-8. Any language can reproduce it byte-for-byte.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(path) -> str | None:
    """SAFETY: the before/after proof for every touched file. None means 'did not exist'."""
    p = Path(path)
    if not p.is_file():
        return None
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_json(path, default=None):
    p = Path(path)
    if not p.exists():
        return default
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def atomic_write_json(path, obj) -> None:
    """Write-temp, fsync, rename. A crash leaves either the old file or the new one, never half."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, sort_keys=True, ensure_ascii=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)


def append_jsonl(path, obj) -> None:
    """Append one JSON line and fsync, so a logged event survives power loss."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(obj, sort_keys=True, ensure_ascii=False) + "\n"
    with _append_lock:
        with open(p, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())


def read_jsonl(path) -> list:
    p = Path(path)
    if not p.exists():
        return []
    out = []
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                # A torn final line from a crash is ignored, never guessed at.
                continue
    return out


def is_within(path, root) -> bool:
    """SAFETY: true only if `path` resolves inside `root`. Blocks ../ escapes and symlink escapes."""
    try:
        rp = Path(os.path.realpath(path))
        rr = Path(os.path.realpath(root))
        return rp == rr or rr in rp.parents
    except (OSError, ValueError):
        return False


SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def safe_id(s) -> bool:
    """SAFETY: IDs become file names (receipts, quarantine dirs, screenshots). Only plain names allowed."""
    return isinstance(s, str) and bool(SAFE_ID.fullmatch(s)) and ".." not in s


def to_posix_rel(path, root) -> str:
    return Path(os.path.relpath(path, root)).as_posix()

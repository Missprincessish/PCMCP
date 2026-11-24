"""Read-only inventory export. Metadata only: never opens, downloads or changes any file.

Inventory lead I commands a pool of sub-bots (I1..In). Each sub-bot takes a folder, lists its children
(paged), adds every item, and hands each sub-folder back to the pool, so many folders are read at once.
All sub-bots share one read throttle and the same 403/429 backoff as the rest of the swarm.
Whole Drive: scope "root" (every item under My Drive). Test folder: scope = that folder's id.
If any folder still cannot be listed after retries, the inventory is marked complete=false and plans
built on it are refused (an incomplete inventory would make the coverage check meaningless).
"""
from __future__ import annotations

import collections
import threading
import time

from . import FOLDER_MIME, INVENTORY_SCHEMA, sub_bot_names
from ..util import canonical_json, now_iso, sha256_bytes

# Lighter than the full field list: permissions (capabilities) are checked later, in the dry run,
# only for the files a plan actually moves.
INV_FIELDS = "id,name,mimeType,size,parents,createdTime,modifiedTime,md5Checksum,ownedByMe"


class Crawl:
    """Shared state of one parallel crawl. Thread-safe."""

    def __init__(self, pool: int):
        self.lock = threading.Condition()
        self.queue = collections.deque()
        self.pending = 0                 # folders queued or being listed
        self.items, self.seen = [], set()
        self.folders_done = 0
        self.errors = []
        self.active = set()
        self.used = set()
        self.recent = collections.deque()
        self.started = time.time()
        self.names = sub_bot_names("I", pool)

    def add_folder(self, fid):
        with self.lock:
            self.queue.append(fid)
            self.pending += 1
            self.lock.notify()

    def snapshot(self) -> dict:
        with self.lock:
            cut = time.time() - 60
            while self.recent and self.recent[0] < cut:
                self.recent.popleft()
            el = time.time() - self.started
            return {"items_listed": len(self.items), "folders_listed": self.folders_done,
                    "folders_waiting": len(self.queue), "bots_active": len(self.active),
                    "bots_active_names": sorted(self.active), "sub_bots_used": sorted(self.used),
                    "errors": len(self.errors), "per_minute_last_60s": len(self.recent),
                    "avg_per_minute": round(len(self.items) / el * 60, 1) if el else 0.0, "elapsed_seconds": round(el, 1)}


def _crawl(client, root_id: str, pool: int, progress=None, should_stop=None) -> Crawl:
    c = Crawl(pool)
    c.add_folder(root_id)
    stop = {"at": 0.0, "flag": False}

    def stopped():
        if should_stop and time.time() - stop["at"] > 0.5:
            stop["at"] = time.time()
            stop["flag"] = bool(should_stop())
        return stop["flag"]

    def sub_bot(name):
        while True:
            if stopped():
                with c.lock:
                    if not any(e.get("stopped") for e in c.errors):
                        c.errors.append({"stopped": True, "error": "stopped by `drive stop` before the crawl finished"})
                    c.lock.notify_all()
                return
            with c.lock:
                while not c.queue and c.pending > 0 and not stop["flag"]:
                    c.lock.wait(0.5)
                if stop["flag"]:
                    continue
                if not c.queue:
                    return                      # nothing waiting and nothing in flight: done
                fid = c.queue.popleft()
                c.active.add(name)
            try:
                kids = client.list_query(f"'{fid}' in parents and trashed=false", INV_FIELDS)
                err = None
            except Exception as e:      # after the client's own retries; recorded, never guessed around
                kids, err = [], f"{type(e).__name__}: {e}"
            with c.lock:
                c.active.discard(name)
                c.used.add(name)
                if err:
                    c.errors.append({"folder_id": fid, "bot": name, "error": err[:300]})
                for f in kids:
                    if f["id"] in c.seen:
                        continue
                    c.seen.add(f["id"])
                    c.items.append(f)
                    c.recent.append(time.time())
                    if f["mimeType"] == FOLDER_MIME:
                        c.queue.append(f["id"])
                        c.pending += 1
                c.folders_done += 1
                c.pending -= 1
                c.lock.notify_all()

    threads = [threading.Thread(target=sub_bot, args=(n,), name=n, daemon=True) for n in c.names]
    for t in threads:
        t.start()
    while any(t.is_alive() for t in threads):
        if progress:
            progress(c.snapshot())
        for t in threads:
            t.join(timeout=1.0 / max(1, len(threads)))
    if progress:
        progress(c.snapshot())
    return c


def _walk_scope(client, scope: str, pool: int = 30, progress=None, should_stop=None) -> tuple:
    root = client.get(scope, "id,name,mimeType,parents")
    c = _crawl(client, root["id"], pool, progress, should_stop)
    return root, c


def build_paths(root_id: str, items: list) -> dict:
    """Folder path (relative to the scope root) of each item's first parent. None = not under the scope."""
    by_id = {f["id"]: f for f in items}
    cache = {root_id: ""}

    def folder_path(fid, depth=0):
        if fid in cache:
            return cache[fid]
        f = by_id.get(fid)
        if f is None or depth > 200 or not f.get("parents"):
            cache[fid] = None
            return None
        parent = folder_path(f["parents"][0], depth + 1)
        cache[fid] = None if parent is None else (f["name"] if parent == "" else f"{parent}/{f['name']}")
        return cache[fid]

    out = {}
    for f in items:
        par = (f.get("parents") or [None])[0]
        out[f["id"]] = folder_path(par) if par else None
    return out


def export(client, scope: str = "root", pool: int = 30, progress=None, should_stop=None) -> dict:
    root, crawl = _walk_scope(client, scope, pool, progress, should_stop)
    raw = crawl.items
    paths = build_paths(root["id"], raw)
    items = []
    for f in sorted(raw, key=lambda x: x["id"]):
        folder = paths[f["id"]]
        caps = f.get("capabilities") or {}
        items.append({
            "id": f["id"], "name": f["name"], "mimeType": f["mimeType"], "is_folder": f["mimeType"] == FOLDER_MIME,
            "size": int(f["size"]) if f.get("size") else None, "parents": f.get("parents") or [],
            "folder_path": folder, "path": (None if folder is None else (f["name"] if folder == "" else f"{folder}/{f['name']}")),
            "in_scope": folder is not None, "createdTime": f.get("createdTime"), "modifiedTime": f.get("modifiedTime"),
            "md5Checksum": f.get("md5Checksum"), "ownedByMe": f.get("ownedByMe"),
            "canMove": caps.get("canMoveItemWithinDrive", True), "canAddChildren": caps.get("canAddChildren"),
        })
    files = [i for i in items if not i["is_folder"]]
    inv = {"schema": INVENTORY_SCHEMA, "schema_version": 1, "generated_at": now_iso(),
           "scope_root_id": root["id"], "scope_root_name": root.get("name"), "scope_arg": scope,
           "contents_opened": False,
           "complete": not crawl.errors, "errors": crawl.errors,
           "crawl": dict(crawl.snapshot(), lead="I", pool=pool),
           "counts": {"items": len(items), "files": len(files), "folders": len(items) - len(files),
                      "files_in_scope": sum(1 for i in files if i["in_scope"]),
                      "total_bytes": sum(i["size"] or 0 for i in files)},
           "items": items}
    inv["inventory_hash"] = sha256_bytes(canonical_json(items))
    return inv


def listing_of(inv: dict, exclude_ids: set | None = None) -> dict:
    """Canonical before/after listing: id -> name, mimeType, parents, md5Checksum, size, path."""
    ex = exclude_ids or set()
    rows = {i["id"]: {"name": i["name"], "mimeType": i["mimeType"], "parents": sorted(i["parents"]),
                      "md5Checksum": i["md5Checksum"], "size": i["size"], "path": i["path"]}
            for i in inv["items"] if i["id"] not in ex}
    return {"scope_root_id": inv["scope_root_id"], "generated_at": inv["generated_at"], "count": len(rows),
            "listing_hash": sha256_bytes(canonical_json(rows)), "rows": rows}


def compare(before: dict, after: dict) -> dict:
    b, a = before["rows"], after["rows"]
    moved = sorted(i for i in b if i in a and b[i]["parents"] != a[i]["parents"])
    changed_bytes = sorted(i for i in b if i in a and (b[i]["md5Checksum"], b[i]["size"]) != (a[i]["md5Checksum"], a[i]["size"]))
    return {"identical": before["listing_hash"] == after["listing_hash"],
            "before_hash": before["listing_hash"], "after_hash": after["listing_hash"],
            "missing_after": sorted(set(b) - set(a)), "new_after": sorted(set(a) - set(b)),
            "moved": len(moved), "bytes_changed": changed_bytes}

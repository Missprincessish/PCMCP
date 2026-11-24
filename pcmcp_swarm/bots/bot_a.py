"""Letter A — create a new file or folder. Deterministic: same order, same result."""
from __future__ import annotations

import base64
import os

from .common import Refuse, check_path, file_rec, load_intent, qdir, run, save_intent, stage, unstage
from ..util import sha256_bytes, sha256_file

LETTER = "A"


def _content(params) -> bytes:
    if "content_b64" in params:
        return base64.b64decode(params["content_b64"])
    return str(params.get("content", "")).encode("utf-8")


def create_folder(order, mode):
    p = check_path(order, order["params"]["path"])
    intent = load_intent(order)
    if p.exists() and not p.is_dir():
        raise Refuse(f"a file already exists where the folder should be: {p}")
    if mode == "dry_run":
        return {"status": "would_do" if not p.exists() else "already_done", "dst": str(p),
                "note": "folder exists" if p.exists() else "will create folder"}
    if intent is None:
        intent = {"existed_before": p.exists()}
        save_intent(order, intent)
    p.mkdir(parents=True, exist_ok=True)
    return {"status": "done", "dst": str(p),
            "undo": {"kind": "remove_folder_if_created_and_empty", "path": str(p), "created": not intent["existed_before"]}}


def create_file(order, mode):
    params = order["params"]
    p = check_path(order, params["path"])
    data = _content(params)
    want = sha256_bytes(data)
    cur = sha256_file(p)
    intent = load_intent(order)
    if cur is not None and cur != want and intent is None and not params.get("overwrite"):
        raise Refuse(f"file exists and overwrite was not approved: {p}")
    if mode == "dry_run":
        return {"status": "already_done" if cur == want else "would_do", "dst": str(p),
                "files": [file_rec("dst", p, cur, want)]}
    if intent is None:
        intent = {"sha256_before": cur}
        save_intent(order, intent)
    staged_old = None
    if intent["sha256_before"] is not None and intent["sha256_before"] != want:
        staged_old = qdir(order, "overwritten", p.name)
        if p.exists() and sha256_file(p) == intent["sha256_before"]:
            stage(p, staged_old)  # overwrite: old content goes to quarantine first
    if sha256_file(p) != want:
        tmp = p.with_name(p.name + ".pcmcp-tmp")
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    return {"status": "done", "dst": str(p), "files": [file_rec("dst", p, intent["sha256_before"], sha256_file(p))],
            "undo": {"kind": "unstage_created_file", "path": str(p), "sha256": want,
                     "restore_overwritten": str(staged_old) if staged_old else None,
                     "overwritten_sha256": intent["sha256_before"] if staged_old else None}}


def undo_create_folder(order, mode):
    u = order["undo"]
    p = check_path(order, u["path"])
    if not u.get("created"):
        return {"status": "done", "note": "folder pre-existed; left in place"}
    if not p.exists():
        return {"status": "already_done"}
    if any(p.iterdir()):
        raise Refuse(f"folder not empty, will not remove: {p}")
    if mode != "dry_run":
        p.rmdir()  # empty folder only: nothing can be lost
    return {"status": "done" if mode != "dry_run" else "would_do", "dst": str(p)}


def undo_create_file(order, mode):
    u = order["undo"]
    p = check_path(order, u["path"])
    dest = qdir(order, "undo", p.name)
    files = []
    if p.exists():
        if sha256_file(p) != u["sha256"]:
            raise Refuse(f"file changed since creation, will not undo: {p}")
        if mode != "dry_run":
            stage(p, dest)
        files.append(file_rec("created_file_staged", p, u["sha256"], None))
    if u.get("restore_overwritten") and mode != "dry_run":
        unstage(qdir(order, "overwritten", p.name), p, u["overwritten_sha256"])
        files.append(file_rec("restored_original", p, None, u["overwritten_sha256"]))
    return {"status": "done" if mode != "dry_run" else "would_do", "files": files}


if __name__ == "__main__":
    run(LETTER, {"create_folder": create_folder, "create_file": create_file},
        {"create_folder": undo_create_folder, "create_file": undo_create_file})

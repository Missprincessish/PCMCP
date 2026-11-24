"""Letter B — copy or move one file.

SAFETY: a move/copy never silently overwrites. If the destination exists with different content,
the order must carry overwrite=true (only possible inside an owner-approved plan) and the old
destination is staged into quarantine first.
"""
from __future__ import annotations

import os
import shutil

from .common import Refuse, check_path, file_rec, load_intent, qdir, run, save_intent, stage, unstage
from ..util import sha256_file

LETTER = "B"


def _copy_atomic(src, dst):
    tmp = dst.with_name(dst.name + ".pcmcp-tmp")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(src), str(tmp))
    os.replace(tmp, dst)


def _transfer(order, mode, move: bool):
    params = order["params"]
    src, dst = check_path(order, params["src"]), check_path(order, params["dst"])
    expect = params.get("expect_sha256")
    intent = load_intent(order)

    # Resume path: a previous run already finished this op before the crash.
    if intent is not None:
        if move and not src.exists() and sha256_file(dst) == intent["src_sha256"]:
            return _done(order, src, dst, intent, move, status="already_done")
        if not move and sha256_file(dst) == intent["src_sha256"]:
            return _done(order, src, dst, intent, move, status="already_done")

    src_sha = sha256_file(src)
    if src_sha is None:
        raise Refuse(f"source missing: {src}")
    if expect and src_sha != expect:
        raise Refuse(f"source changed since the plan was approved: {src}")
    dst_sha = sha256_file(dst)
    if dst.exists() and dst.is_dir():
        raise Refuse(f"destination is a folder: {dst}")
    if dst_sha is not None and dst_sha != src_sha and not params.get("overwrite"):
        raise Refuse(f"destination exists and overwrite was not approved: {dst}")

    if mode == "dry_run":
        return {"status": "would_do", "src": str(src), "dst": str(dst),
                "files": [file_rec("src", src, src_sha, None if move else src_sha), file_rec("dst", dst, dst_sha, src_sha)]}

    if intent is None:
        intent = {"src_sha256": src_sha, "dst_sha256_before": dst_sha}
        save_intent(order, intent)
    if dst_sha is not None and dst_sha != src_sha:
        stage(dst, qdir(order, "overwritten", dst.name))
    if move:
        if dst.exists():  # identical content already at destination: stage the redundant source, never delete
            stage(src, qdir(order, "identical_src", src.name))
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
    else:
        if sha256_file(dst) != src_sha:
            _copy_atomic(src, dst)
    if sha256_file(dst) != src_sha:
        raise RuntimeError(f"post-transfer hash mismatch at {dst}")
    return _done(order, src, dst, intent, move)


def _done(order, src, dst, intent, move, status="done"):
    overwritten = intent["dst_sha256_before"] not in (None, intent["src_sha256"])
    identical_staged = move and intent["dst_sha256_before"] == intent["src_sha256"]
    return {"status": status, "src": str(src), "dst": str(dst),
            "files": [file_rec("src", src, intent["src_sha256"], None if move else intent["src_sha256"]),
                      file_rec("dst", dst, intent["dst_sha256_before"], intent["src_sha256"])],
            "undo": {"kind": "move_back" if move else "unstage_copy", "src": str(src), "dst": str(dst),
                     "sha256": intent["src_sha256"], "overwritten": overwritten,
                     "overwritten_sha256": intent["dst_sha256_before"] if overwritten else None,
                     "identical_src_staged": identical_staged}}


def copy(order, mode):
    return _transfer(order, mode, move=False)


def move(order, mode):
    return _transfer(order, mode, move=True)


def _restore_overwritten(order, u, dst):
    if u.get("overwritten"):
        unstage(qdir(order, "overwritten", dst.name), dst, u["overwritten_sha256"])


def undo_move(order, mode):
    u = order["undo"]
    src, dst = check_path(order, u["src"]), check_path(order, u["dst"])
    if mode == "dry_run":
        return {"status": "would_do", "src": str(dst), "dst": str(src)}
    if u.get("identical_src_staged"):
        unstage(qdir(order, "identical_src", src.name), src, u["sha256"])
    elif not (src.exists() and sha256_file(src) == u["sha256"] and not dst.exists()):
        if src.exists():
            raise Refuse(f"original location is occupied, will not overwrite: {src}")
        if sha256_file(dst) != u["sha256"]:
            raise Refuse(f"moved file changed since the move: {dst}")
        src.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(dst), str(src))
    _restore_overwritten(order, u, dst)
    return {"status": "done", "src": str(dst), "dst": str(src),
            "files": [file_rec("restored", src, None, sha256_file(src))]}


def undo_copy(order, mode):
    u = order["undo"]
    dst = check_path(order, u["dst"])
    if mode == "dry_run":
        return {"status": "would_do", "dst": str(dst)}
    if dst.exists():
        if sha256_file(dst) != u["sha256"]:
            raise Refuse(f"copy changed since it was made: {dst}")
        stage(dst, qdir(order, "undo", dst.name))
    _restore_overwritten(order, u, dst)
    return {"status": "done", "files": [file_rec("copy_staged", dst, u["sha256"], None)]}


if __name__ == "__main__":
    run(LETTER, {"copy": copy, "move": move}, {"copy": undo_copy, "move": undo_move})

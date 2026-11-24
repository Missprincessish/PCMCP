"""Letter C — delete, which in this system ALWAYS means "stage into quarantine".

SAFETY: there is no hard-delete code path anywhere in the swarm. A C bot moves the file into
<quarantine>/<plan>/<op>/deleted/ and records its hash. Undo puts it back byte-identical.
Runs only inside an owner-approved plan, and refuses if the file changed since the plan was approved.
"""
from __future__ import annotations

from .common import Refuse, check_path, file_rec, load_intent, qdir, run, save_intent, stage, unstage
from ..util import sha256_file

LETTER = "C"


def delete(order, mode):
    params = order["params"]
    p = check_path(order, params["path"])
    expect = params.get("expect_sha256")
    if not expect:
        raise Refuse("delete orders must carry expect_sha256 (the hash the owner approved)")
    staged = qdir(order, "deleted", p.name)
    intent = load_intent(order)
    if intent is not None and not p.exists() and sha256_file(staged) == intent["sha256"]:
        return _done(p, staged, intent["sha256"], "already_done")
    cur = sha256_file(p)
    if cur is None:
        raise Refuse(f"file to delete is missing: {p}")
    if cur != expect:
        raise Refuse(f"file changed since the plan was approved, will not delete: {p}")
    if mode == "dry_run":
        return {"status": "would_do", "src": str(p), "files": [file_rec("deleted", p, cur, None)],
                "note": "will be staged to quarantine, not destroyed"}
    if intent is None:
        save_intent(order, {"sha256": cur})
    stage(p, staged)
    return _done(p, staged, cur, "done")


def _done(p, staged, sha, status):
    return {"status": status, "src": str(p), "dst": str(staged),
            "files": [file_rec("deleted", p, sha, None), file_rec("quarantine", staged, None, sha)],
            "undo": {"kind": "restore_from_quarantine", "path": str(p), "staged": str(staged), "sha256": sha}}


def undo_delete(order, mode):
    u = order["undo"]
    p = check_path(order, u["path"])
    if mode == "dry_run":
        return {"status": "would_do", "dst": str(p)}
    from pathlib import Path
    unstage(Path(u["staged"]), p, u["sha256"])
    return {"status": "done", "dst": str(p), "files": [file_rec("restored", p, None, sha256_file(p))]}


if __name__ == "__main__":
    run(LETTER, {"delete": delete}, {"delete": undo_delete})

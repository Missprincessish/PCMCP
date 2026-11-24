"""Letter E — archive: zip a fixed list of files, then stage the originals into quarantine.

Deterministic zip: members sorted, fixed timestamps, fixed permissions, so the same inputs always
produce a byte-identical archive with the same SHA-256.
"""
from __future__ import annotations

import os
import zipfile
from pathlib import Path

from .common import Refuse, check_path, file_rec, load_intent, qdir, run, save_intent, stage, unstage
from ..util import sha256_file

LETTER = "E"
FIXED_TIME = (1980, 1, 1, 0, 0, 0)


def _write_zip(dst: Path, members: list):
    tmp = dst.with_name(dst.name + ".pcmcp-tmp")
    dst.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for arcname, src in sorted(members):
            info = zipfile.ZipInfo(arcname, date_time=FIXED_TIME)
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            with open(src, "rb") as f:
                z.writestr(info, f.read())
    with open(tmp, "rb") as f:
        os.fsync(f.fileno())
    os.replace(tmp, dst)


def archive(order, mode):
    params = order["params"]
    dst = check_path(order, params["dst_zip"])
    sources = params["sources"]  # [{"path", "arcname", "expect_sha256"}]
    if not sources:
        raise Refuse("archive with no sources")
    intent = load_intent(order)
    if intent is None:
        if dst.exists():
            raise Refuse(f"archive destination already exists: {dst}")
        recs = []
        for s in sources:
            p = check_path(order, s["path"])
            cur = sha256_file(p)
            if cur is None:
                raise Refuse(f"archive source missing: {p}")
            if s.get("expect_sha256") and cur != s["expect_sha256"]:
                raise Refuse(f"archive source changed since the plan was approved: {p}")
            recs.append({"path": str(p), "arcname": s["arcname"], "sha256": cur})
        if mode == "dry_run":
            return {"status": "would_do", "dst": str(dst),
                    "files": [file_rec("archived_original", r["path"], r["sha256"], None) for r in recs]}
        intent = {"sources": recs}
        save_intent(order, intent)
    elif mode == "dry_run":
        return {"status": "would_do", "dst": str(dst), "note": "resuming interrupted archive"}

    recs = intent["sources"]
    if "zip_sha256" not in intent:
        _write_zip(dst, [(r["arcname"], r["path"]) for r in recs])
        # Verify every member round-trips before any original is staged.
        with zipfile.ZipFile(dst) as z:
            import hashlib
            for r in recs:
                if hashlib.sha256(z.read(r["arcname"])).hexdigest() != r["sha256"]:
                    raise RuntimeError(f"archive verification failed for {r['arcname']}")
        intent["zip_sha256"] = sha256_file(dst)
        save_intent(order, intent)
    elif sha256_file(dst) != intent["zip_sha256"]:
        raise Refuse(f"archive changed during interrupted run: {dst}")

    files = []
    if params.get("remove_originals", True):
        for i, r in enumerate(recs):
            p = Path(r["path"])
            slot = qdir(order, "originals", f"{i:05d}_{p.name}")
            if p.exists():
                stage(p, slot)
            files.append(file_rec("archived_original", p, r["sha256"], None))
    files.append(file_rec("archive", dst, None, intent["zip_sha256"]))
    return {"status": "done", "dst": str(dst), "files": files,
            "undo": {"kind": "restore_originals_stage_zip", "dst_zip": str(dst), "zip_sha256": intent["zip_sha256"],
                     "removed_originals": bool(params.get("remove_originals", True)),
                     "originals": [{"path": r["path"], "sha256": r["sha256"],
                                    "staged": str(qdir(order, "originals", f"{i:05d}_{Path(r['path']).name}"))}
                                   for i, r in enumerate(recs)]}}


def undo_archive(order, mode):
    u = order["undo"]
    dst = check_path(order, u["dst_zip"])
    if mode == "dry_run":
        return {"status": "would_do", "dst": str(dst)}
    files = []
    if u["removed_originals"]:
        for o in u["originals"]:
            target = check_path(order, o["path"])
            unstage(Path(o["staged"]), target, o["sha256"])
            files.append(file_rec("restored", target, None, o["sha256"]))
    if dst.exists():
        if sha256_file(dst) != u["zip_sha256"]:
            raise Refuse(f"archive changed since creation: {dst}")
        stage(dst, qdir(order, "undo", dst.name))
        files.append(file_rec("archive_staged", dst, u["zip_sha256"], None))
    return {"status": "done", "files": files}


if __name__ == "__main__":
    run(LETTER, {"archive": archive}, {"archive": undo_archive})

"""Dry-run scan: discover + classify files and build an exact organization plan. Changes nothing.

Every rule is a fixed function of (file path, extension, size, sha256, mtime, config):
  1. exact duplicates (same sha256): keep the first path in sorted order; stage the rest (letter C)
  2. last modified before scan.archive_before: zip per category into _Archive/ (letter E)
  3. everything else: move into _Organized/<Category>/ (letter B), folders created first (letter A)
Ops are dealt to bots round-robin in sorted order, so the same folder always yields the same plan.
"""
from __future__ import annotations

import datetime as _dt
import os
from pathlib import Path

from . import plans as plans_mod
from .util import sha256_file


def _category(ext: str, cfg: dict) -> str:
    for cat, exts in sorted(cfg["categories"].items()):
        if ext in exts:
            return cat
    return cfg["uncategorized"]


def discover(root: Path, scfg: dict) -> list:
    skip_top = {scfg["organized_dir"], scfg["archive_dir"]}
    ignore = set(scfg["ignore_names"])
    out = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = Path(dirpath).relative_to(root)
        if rel_dir == Path("."):
            dirnames[:] = [d for d in dirnames if d not in skip_top]
        dirnames.sort()
        for name in sorted(filenames):
            p = Path(dirpath) / name
            if name in ignore or p.is_symlink() or name.endswith(".pcmcp-tmp"):
                continue
            st = p.stat()
            out.append({"path": str(p), "rel": p.relative_to(root).as_posix(), "name": name,
                        "ext": p.suffix.lower(), "size": st.st_size, "mtime": int(st.st_mtime),
                        "sha256": sha256_file(p)})
    out.sort(key=lambda f: f["rel"])
    return out


def _unique(dst_dir: Path, name: str, taken: set) -> Path:
    stem, suf = os.path.splitext(name)
    cand, n = dst_dir / name, 2
    while str(cand) in taken or cand.exists():
        cand = dst_dir / f"{stem} ({n}){suf}"
        n += 1
    taken.add(str(cand))
    return cand


def build_plan(root: str, cfg: dict, registry: dict, source: dict) -> tuple:
    scfg = cfg["scan"]
    root_p = Path(os.path.realpath(root))
    files = discover(root_p, scfg)
    cutoff = None
    if scfg.get("archive_before"):
        cutoff = int(_dt.datetime.strptime(scfg["archive_before"], "%Y-%m-%d").replace(tzinfo=_dt.timezone.utc).timestamp())

    seen_hash = {}
    deletes, archive_groups, moves = [], {}, []
    for f in files:
        f["category"] = _category(f["ext"], scfg)
        if scfg.get("delete_exact_duplicates") and f["sha256"] in seen_hash:
            f["decision"] = f"duplicate of {seen_hash[f['sha256']]}"
            deletes.append(f)
            continue
        seen_hash.setdefault(f["sha256"], f["rel"])
        if cutoff is not None and f["mtime"] < cutoff:
            f["decision"] = f"archive (modified before {scfg['archive_before']})"
            archive_groups.setdefault(f["category"], []).append(f)
            continue
        f["decision"] = f"organize into {f['category']}"
        moves.append(f)

    org = root_p / scfg["organized_dir"]
    arc = root_p / scfg["archive_dir"]
    folders = set()
    taken = set()
    move_ops = []
    for f in moves:
        dst_dir = org / f["category"]
        if Path(f["path"]).parent == dst_dir:
            continue
        folders.update([str(org), str(dst_dir)])
        move_ops.append({"action": "move", "letter": "B",
                         "params": {"src": f["path"], "dst": str(_unique(dst_dir, f["name"], taken)),
                                    "expect_sha256": f["sha256"]}})
    arch_ops = []
    for cat in sorted(archive_groups):
        folders.add(str(arc))
        dst_zip = _unique(arc, f"{cat}_before_{scfg['archive_before']}.zip", taken)
        arch_ops.append({"action": "archive", "letter": "E",
                         "params": {"dst_zip": str(dst_zip), "remove_originals": True,
                                    "sources": [{"path": f["path"], "arcname": f["rel"], "expect_sha256": f["sha256"]}
                                                for f in archive_groups[cat]]}})
    del_ops = [{"action": "delete", "letter": "C", "params": {"path": f["path"], "expect_sha256": f["sha256"]}}
               for f in deletes]
    folder_ops = [{"action": "create_folder", "letter": "A", "params": {"path": p}}
                  for p in sorted(folders, key=lambda s: (s.count(os.sep), s))]

    ops = []
    for letter_ops in (folder_ops, move_ops, del_ops, arch_ops):
        ops.extend(deal(letter_ops, registry))
    ops = plans_mod.assign_ids(ops, {"root": str(root_p), "files": [(f["rel"], f["sha256"]) for f in files]})

    summary = plans_mod.summarize(ops)
    summary.update({"files_scanned": len(files), "bytes_scanned": sum(f["size"] for f in files),
                    "duplicates": len(deletes), "to_archive": sum(len(v) for v in archive_groups.values()),
                    "to_organize": len(move_ops),
                    "by_category": _count(f["category"] for f in files)})
    plan = plans_mod.make(str(root_p), ops, source, summary)
    classification = [{k: f[k] for k in ("rel", "category", "size", "sha256", "decision")} for f in files]
    return plan, classification


def deal(ops: list, registry: dict) -> list:
    """Round-robin assignment to the bots holding each letter (sorted, deterministic)."""
    out, counters = [], {}
    for op in ops:
        bots = registry["letters"][op["letter"]]["bots"]
        if not bots:
            raise ValueError(f"no bots hold letter {op['letter']}; raise it in config.fleet and re-run init")
        i = counters.get(op["letter"], 0)
        counters[op["letter"]] = i + 1
        out.append(dict(op, bot=bots[i % len(bots)]))
    return out


def _count(items) -> dict:
    d = {}
    for i in items:
        d[i] = d.get(i, 0) + 1
    return dict(sorted(d.items()))

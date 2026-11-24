"""Plan-file format (see DRIVE_PLAN_SPEC.md), validator, and compiler to bot operations.

This module does NOT decide where anything goes. Instinct writes the plan. This code only checks the
plan against the hard rules and turns it, unchanged, into a list of operations.
"""
from __future__ import annotations

import re

from . import PLAN_SCHEMA
from ..util import canonical_json, read_json, sha256_bytes

TOP_KEYS = {"schema", "schema_version", "mode", "author", "written_at", "inventory_hash", "scope_root_id",
            "new_folders_declared", "create_folders", "placements", "archives", "copies", "stay", "tags", "notes"}
REQUIRED = {"schema", "schema_version", "author", "inventory_hash", "scope_root_id", "create_folders", "placements"}
DELETE_WORDS = re.compile(r"(?i)(delete|trash|purge|erase|destroy|wipe|unlink|rmdir)")


def plan_hash(plan: dict) -> str:
    return sha256_bytes(canonical_json(plan))


def load_plan(path) -> dict:
    p = read_json(path)
    if not isinstance(p, dict):
        raise ValueError(f"plan file {path} is not a JSON object")
    return p


def norm(path: str) -> str:
    return "" if path in ("", "/") else path.strip("/")


def bad_path(path: str) -> str | None:
    if not isinstance(path, str):
        return "path is not text"
    if path in ("", "/"):
        return None
    segs = path.strip("/").split("/")
    for s in segs:
        if s == "" or s in (".", "..") or s != s.strip():
            return f"bad folder name segment {s!r}"
    return None


def parent_of(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def ext_of(name: str) -> str:
    return ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""


def under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def _scan_for_delete(obj, where="plan") -> list:
    """SAFETY: a delete must be impossible to express. Any key, op or action naming delete/trash fails."""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if DELETE_WORDS.search(str(k)) or str(k).lower() == "trashed":
                out.append(("DELETE_FORBIDDEN", f"{where}: key {k!r} names a delete/trash operation; nothing is ever deleted"))
            if str(k).lower() in ("op", "action", "operation", "method") and DELETE_WORDS.search(str(v)):
                out.append(("DELETE_FORBIDDEN", f"{where}.{k} = {v!r}: delete operations do not exist"))
            out += _scan_for_delete(v, f"{where}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out += _scan_for_delete(v, f"{where}[{i}]")
    return out


def never_move_violation(item: dict, dest_folder: str, tags: list, rules: list) -> str | None:
    """SAFETY (hard rules, checked twice): returns the broken rule id, or None.
    Guards only refuse; they never pick a destination."""
    tags = {t.lower() for t in (tags or [])}
    hay = [item.get("name") or "", item.get("path") or ""]
    for r in rules:
        if "forbidden_destination_prefix" in r:
            if not under(dest_folder, r["forbidden_destination_prefix"]):
                continue
            hit = bool(tags & {t.lower() for t in r.get("tags_any", [])}) or any(
                re.search(rx, h) for rx in r.get("name_or_path_regex_any", []) for h in hay)
            if hit:
                return r["id"]
        if "locked_folder" in r:
            if under(item.get("folder_path") or "", r["locked_folder"]) and not under(dest_folder, r["locked_folder"]):
                return r["id"]
            continue
        if "must_stay_under" in r:
            if dest_folder == item.get("folder_path"):      # not moving: nothing leaves anywhere
                continue
            if ext_of(item.get("name") or "") not in {e.lower() for e in r.get("three_d_extensions", [])}:
                continue
            applies = bool(tags & {t.lower() for t in r.get("applies_when_tags_any", [])}) or any(
                re.search(rx, item.get("path") or "") for rx in r.get("applies_when_current_path_regex_any", []))
            if applies and not under(dest_folder, r["must_stay_under"]):
                return r["id"]
    return None


def skip_rules(fmap: dict) -> list:
    return [r for r in (fmap or {}).get("never_move_rules", []) if r.get("on_violation") == "skip"]


def skipped_moves(plan: dict, inv: dict, fmap: dict) -> dict:
    """Direct moves/copies the plan asks for that a "skip" rule forbids (3D assets attached to Synomize).
    They are NOT executed: the file stays where it is, and the list is shown to Angie. {file_id: rule_id}"""
    rules = skip_rules(fmap)
    if not rules:
        return {}
    items = {i["id"]: i for i in inv["items"]}
    tags = plan.get("tags", {}) if isinstance(plan.get("tags"), dict) else {}
    out = {}
    for key in ("placements", "archives", "copies"):
        groups = plan.get(key) or {}
        if not isinstance(groups, dict):
            continue
        for dest, ids in groups.items():
            for fid in ids if isinstance(ids, list) else []:
                it = items.get(fid) if isinstance(fid, str) else None
                if it and not it["is_folder"]:
                    hit = never_move_violation(it, norm(dest), tags.get(fid, []), rules)
                    if hit:
                        out[fid] = hit
    return out


def validate(plan: dict, inv: dict, fmap: dict) -> dict:
    """Returns {"ok": bool, "errors": [(code, msg)], "warnings": [...], "stats": {...}}."""
    E, W = [], []
    E += _scan_for_delete(plan)
    if plan.get("schema") != PLAN_SCHEMA or plan.get("schema_version") != 1:
        E.append(("SCHEMA", f"schema must be {PLAN_SCHEMA!r} version 1"))
    for k in plan:
        if k not in TOP_KEYS and not DELETE_WORDS.search(k):
            E.append(("SCHEMA", f"unknown top-level key {k!r}"))
    for k in REQUIRED - set(plan):
        E.append(("SCHEMA", f"missing required key {k!r}"))
    mode = plan.get("mode", "full")
    if mode not in ("full", "folders_only"):
        E.append(("SCHEMA", "mode must be 'full' or 'folders_only'"))
    if mode == "folders_only" and any(plan.get(k) for k in ("placements", "archives", "copies", "stay")):
        E.append(("SCHEMA", "a folders_only plan may only create folders (placements, archives, copies and stay must be empty)"))
    if inv.get("complete") is False:
        E.append(("INVENTORY_INCOMPLETE", f"{len(inv.get('errors', []))} folder(s) could not be listed; re-run the inventory before planning"))
    if plan.get("inventory_hash") != inv["inventory_hash"]:
        E.append(("INVENTORY_MISMATCH", "plan.inventory_hash does not match this run's inventory (plan written from a different export)"))
    if plan.get("scope_root_id") != inv["scope_root_id"]:
        E.append(("INVENTORY_MISMATCH", "plan.scope_root_id does not match the inventory scope"))

    def dict_of_lists(key):
        v = plan.get(key, {})
        if not isinstance(v, dict) or not all(isinstance(x, list) for x in v.values()):
            E.append(("SCHEMA", f"{key} must be an object of destination path -> list of file ids"))
            return {}
        for dest, ids in v.items():
            for x in ids:
                if not isinstance(x, str):
                    E.append(("SCHEMA", f"{key}[{dest!r}] entries must be file id strings, got {type(x).__name__}"))
        return {norm(d): [x for x in ids if isinstance(x, str)] for d, ids in v.items()}

    placements, archives, copies = dict_of_lists("placements"), dict_of_lists("archives"), dict_of_lists("copies")
    stay = plan.get("stay", []) if isinstance(plan.get("stay", []), list) else []
    tags = plan.get("tags", {}) if isinstance(plan.get("tags", {}), dict) else {}
    creates = [norm(p) for p in plan.get("create_folders", []) if isinstance(p, str)]
    declared = [norm(p) for p in plan.get("new_folders_declared", []) if isinstance(p, str)]
    rules = fmap.get("never_move_rules", [])
    map_paths = {norm(f["path"]) for f in fmap.get("folders", [])}

    items = {i["id"]: i for i in inv["items"]}
    in_scope_files = {i["id"] for i in inv["items"] if i["in_scope"] and not i["is_folder"]}
    existing_folders = {}
    for i in inv["items"]:
        if i["is_folder"] and i["in_scope"]:
            existing_folders.setdefault(i["path"], []).append(i["id"])

    # --- folders
    for p in creates + declared:
        b = bad_path(p)
        if b:
            E.append(("BAD_PATH", f"{p!r}: {b}"))
    if len(set(creates)) != len(creates):
        E.append(("SCHEMA", "create_folders lists a folder twice"))
    for p in creates:
        if p not in map_paths and p not in declared:
            E.append(("UNDECLARED_FOLDER", f"new folder {p!r} is not in folder-map.json and not declared in new_folders_declared"))
        par = parent_of(p)
        if par and par not in creates and par not in existing_folders:
            E.append(("FOLDER_NOT_LISTED", f"{p!r}: parent folder {par!r} neither exists nor is in create_folders"))
    for p in declared:
        if p not in creates:
            E.append(("SCHEMA", f"declared new folder {p!r} is not in create_folders"))
        if p in map_paths:
            W.append(("DECLARED_IN_MAP", f"{p!r} is already in the folder map; declaring it is harmless"))

    # --- coverage: every in-scope file has exactly one destination (Review counts; 'stay' counts)
    seen = {}
    for where, groups in (("placements", placements), ("archives", archives)):
        for dest, ids in groups.items():
            for fid in ids:
                seen.setdefault(fid, []).append(f"{where}[{dest!r}]")
    for fid in stay:
        if isinstance(fid, str):
            seen.setdefault(fid, []).append("stay")
    for fid, where in seen.items():
        if fid not in items:
            E.append(("UNKNOWN_FILE", f"{fid}: not in the inventory"))
        elif not items[fid]["in_scope"]:
            E.append(("UNKNOWN_FILE", f"{fid}: outside the inventory scope"))
        if len(where) > 1:
            E.append(("DUPLICATE_FILE", f"{fid} has more than one destination: {', '.join(where)}"))
    for fid in sorted(in_scope_files - set(seen)) if mode != "folders_only" else []:
        E.append(("MISSING_DESTINATION", f"{fid} ({items[fid]['path']}) has no destination (use 'Review' when unsure, or 'stay')"))
    for dest, ids in copies.items():
        for fid in ids:
            if fid not in items or items[fid]["is_folder"]:
                E.append(("UNKNOWN_FILE", f"copy {fid}: not a file in the inventory"))

    # --- destinations
    moved_folders = {fid for g in (placements, archives) for ids in g.values() for fid in ids if fid in items and items[fid]["is_folder"]}
    # A destination at or under a folder this plan moves would end up somewhere else than its path says,
    # which would let a file slip past the never-move rules. Refuse it, so destination path == final path.
    moved_paths = sorted({items[f]["path"] for f in moved_folders})
    for p in creates:
        hit = next((m for m in moved_paths if under(p, m)), None)
        if hit:
            E.append(("FOLDER_BEING_MOVED", f"new folder {p!r} is inside {hit!r}, which this plan moves"))
    for where, groups in (("placements", placements), ("archives", archives), ("copies", copies)):
        for dest in groups:
            hit = next((m for m in moved_paths if dest and under(dest, m)), None)
            if hit:
                E.append(("FOLDER_BEING_MOVED", f"{where} destination {dest!r} is at or inside {hit!r}, which this plan moves"))
                continue
            b = bad_path(dest)
            if b:
                E.append(("BAD_PATH", f"{where} destination {dest!r}: {b}"))
                continue
            if dest and dest not in map_paths and dest not in declared:
                E.append(("DEST_NOT_IN_MAP", f"{where} destination {dest!r} is not in folder-map.json and not declared"))
            if dest and dest not in creates and dest not in existing_folders:
                E.append(("FOLDER_NOT_LISTED", f"{where} destination {dest!r} does not exist and is not in create_folders"))
            if len(existing_folders.get(dest, [])) > 1:
                E.append(("AMBIGUOUS_FOLDER", f"destination {dest!r} matches {len(existing_folders[dest])} existing folders"))

    # --- effective final folder of every item, then the never-move rules (check #1)
    skipped = skipped_moves(plan, inv, fmap)
    for fid, rid in sorted(skipped.items(), key=lambda x: (items[x[0]]["path"] or "")):
        W.append(("SKIPPED_KEPT_IN_SYNOMIZE", f"{items[fid]['path']!r} stays where it is ({rid}); the plan's move is skipped"))
    dest_of = {fid: d for g in (placements, archives) for d, ids in g.items() for fid in ids if fid not in skipped}
    eff_cache = {}

    def eff_full(fid, depth=0):
        """Final full path of a FOLDER after the plan runs ('' for the scope root)."""
        if fid == inv["scope_root_id"]:
            return ""
        if fid in eff_cache:
            return eff_cache[fid]
        it = items.get(fid)
        if it is None or depth > 200:
            return None
        if fid in dest_of:
            base = dest_of[fid]
        else:
            par = (it["parents"] or [None])[0]
            base = eff_full(par, depth + 1) if par else None
        eff_cache[fid] = None if base is None else (it["name"] if base == "" else f"{base}/{it['name']}")
        return eff_cache[fid]

    for fid in moved_folders:
        d = dest_of[fid]
        own = items[fid]["path"]
        if under(d, own):
            E.append(("CYCLE", f"folder {own!r} would be moved inside itself ({d!r})"))
    final = {}
    for fid in in_scope_files:
        it = items[fid]
        if fid in dest_of:
            final[fid] = dest_of[fid]
        else:
            par = (it["parents"] or [None])[0]
            final[fid] = eff_full(par) if par else None
        if final[fid] is None:
            continue
        broken = never_move_violation(it, final[fid], tags.get(fid, []), rules)
        if broken:
            E.append((f"NEVER_MOVE:{broken}", f"{it['path']!r} -> {final[fid]!r} breaks never-move rule {broken!r}"))
    for dest, ids in copies.items():
        for fid in ids:
            if fid in items and fid not in skipped:
                broken = never_move_violation(items[fid], dest, tags.get(fid, []), rules)
                if broken:
                    E.append((f"NEVER_MOVE:{broken}", f"copy of {items[fid]['path']!r} -> {dest!r} breaks rule {broken!r}"))
    for fid in dest_of:
        if fid in items and not items[fid]["canMove"]:
            W.append(("CANNOT_MOVE", f"{items[fid]['path']!r}: Drive says this item cannot be moved by you"))

    stats = {"files_in_scope": len(in_scope_files), "covered": len(set(seen) & in_scope_files),
             "to_review": len(placements.get(fmap.get("review_folder", "Review - Not Sure"), [])),
             "folders_to_create": len(creates), "new_folders_declared": len(declared), "mode": mode,
             "skipped_kept_in_synomize": len(skipped)}
    return {"ok": not E, "errors": E, "warnings": W, "stats": stats, "plan_hash": plan_hash(plan),
            "skipped": [{"file_id": f, "path": items[f]["path"], "rule": r} for f, r in sorted(skipped.items())]}


def compile_ops(plan: dict, inv: dict, fmap: dict | None = None) -> dict:
    """Turn a VALID plan into operations, unchanged, except moves a "skip" rule forbids (listed in `skipped`).
    Letters: A create, B move/copy, E archive (move)."""
    ph = plan_hash(plan)[:12]
    skipped = skipped_moves(plan, inv, fmap) if fmap else {}
    items = {i["id"]: i for i in inv["items"]}
    existing = {}
    for i in inv["items"]:
        if i["is_folder"] and i["in_scope"]:
            existing.setdefault(i["path"], []).append(i["id"])
    ops = {"A": [], "B": [], "E": []}
    reuse = []
    for p in sorted({norm(x) for x in plan["create_folders"]}, key=lambda s: (s.count("/"), s)):
        if p in existing:
            reuse.append(p)
            continue
        ops["A"].append({"action": "create_folder", "path": p, "parent_path": parent_of(p),
                         "name": p.rsplit("/", 1)[-1], "depth": p.count("/")})

    def mv(action, fid, dest):
        it = items[fid]
        return {"action": action, "file_id": fid, "name": it["name"], "from_path": it["folder_path"],
                "from_parents": list(it["parents"]), "to_path": dest, "is_folder": it["is_folder"]}

    unchanged = 0
    for dest, ids in sorted((norm(d), v) for d, v in plan["placements"].items()):
        for fid in ids:
            if fid in skipped:
                continue
            if items[fid]["folder_path"] == dest and len(items[fid]["parents"]) == 1:
                unchanged += 1
                continue
            ops["B"].append(mv("move", fid, dest))
    for dest, ids in sorted((norm(d), v) for d, v in (plan.get("copies") or {}).items()):
        for fid in ids:
            if fid not in skipped:
                ops["B"].append(mv("copy", fid, dest))
    for dest, ids in sorted((norm(d), v) for d, v in (plan.get("archives") or {}).items()):
        for fid in ids:
            if fid in skipped:
                continue
            if items[fid]["folder_path"] == dest:
                unchanged += 1
                continue
            ops["E"].append(mv("archive_move", fid, dest))
    n = 0
    for letter in ("A", "B", "E"):
        for op in ops[letter]:
            n += 1
            op["op_id"] = f"{ph}-{letter}-{n:06d}"
            op["letter"] = letter
    return {"plan_hash": plan_hash(plan), "ops": ops, "reused_folders": reuse, "unchanged": unchanged,
            "tags": plan.get("tags", {}),
            "skipped": [{"file_id": f, "path": items[f]["path"], "rule": r} for f, r in sorted(skipped.items())],
            "totals": {k: len(v) for k, v in ops.items()}}


def render(plan: dict, inv: dict | None = None) -> str:
    L = [f"PLAN {plan_hash(plan)}", f"author: {plan.get('author')}   written: {plan.get('written_at')}",
         f"scope: {plan.get('scope_root_id')}   inventory: {str(plan.get('inventory_hash'))[:16]}…", ""]
    L.append(f"NEW FOLDERS DECLARED ({len(plan.get('new_folders_declared', []))}) - approve these first:")
    L += [f"  + {p}" for p in plan.get("new_folders_declared", [])] or ["  (none)"]
    L.append(f"\nFOLDERS TO CREATE ({len(plan.get('create_folders', []))}):")
    L += [f"  {p}" for p in plan.get("create_folders", [])]
    for key in ("placements", "archives", "copies"):
        groups = plan.get(key) or {}
        if groups:
            L.append(f"\n{key.upper()} (destination: file count):")
            L += [f"  {d or '(root)'}: {len(ids)}" for d, ids in sorted(groups.items())]
    L.append(f"\nSTAY IN PLACE: {len(plan.get('stay', []))}")
    if plan.get("mode") == "folders_only":
        L.insert(3, "MODE: folders_only (creates folders; moves nothing)")
    return "\n".join(L)

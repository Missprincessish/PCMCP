"""Plans and owner approvals.

A plan is an exact, ordered list of operations. Its identity is plan_hash = sha256(canonical(body)).
SAFETY: an approval names one plan_hash. Change one byte of the plan and the hash changes, so the
old approval no longer applies and the runner refuses to execute.
"""
from __future__ import annotations

import getpass
import socket

from . import registry as reg_mod
from .util import append_jsonl, atomic_write_json, canonical_json, now_iso, read_json, read_jsonl, sha256_bytes

PHASES = ["A", "B", "C", "E", "S"]  # execution order by letter: create, then move, then delete, then archive


def assign_ids(ops: list, prefix_source) -> list:
    """Deterministic op IDs derived from plan content (so the same scan gives the same IDs)."""
    pid = sha256_bytes(canonical_json(prefix_source))[:12]
    out = []
    for i, op in enumerate(ops):
        op = dict(op)
        op["op_id"] = f"{pid}-{i:05d}"
        out.append(op)
    return out


def make(root: str, operations: list, source: dict, summary: dict | None = None) -> dict:
    body = {"schema": "pcmcp.plan", "schema_version": 1, "root": root, "operations": operations,
            "source": source, "summary": summary or summarize(operations)}
    return {"plan_hash": sha256_bytes(canonical_json(body)), "body": body, "meta": {"created_at": now_iso()}}


def summarize(ops: list) -> dict:
    by_letter, by_bot = {}, {}
    for op in ops:
        by_letter[op["letter"]] = by_letter.get(op["letter"], 0) + 1
        by_bot[op["bot"]] = by_bot.get(op["bot"], 0) + 1
    return {"total_operations": len(ops), "by_letter": dict(sorted(by_letter.items())), "by_bot": dict(sorted(by_bot.items()))}


def validate(plan: dict, registry: dict, allowed_roots: list) -> list:
    """Every op must be lawful for its bot and every path inside an allowed root."""
    from .util import is_within, safe_id
    problems = []
    if sha256_bytes(canonical_json(plan["body"])) != plan["plan_hash"]:
        problems.append("plan_hash does not match plan body (tampered or corrupted)")
    if not any(is_within(plan["body"]["root"], r) for r in allowed_roots):
        problems.append(f"plan root {plan['body']['root']} is not inside allowed_roots")
    ids = set()
    for op in plan["body"]["operations"]:
        if not safe_id(op["op_id"]):
            problems.append(f"unsafe op_id {op['op_id']!r}")
        if op["op_id"] in ids:
            problems.append(f"duplicate op_id {op['op_id']}")
        ids.add(op["op_id"])
        if reg_mod.letter_for_action(op["action"]) != op.get("letter"):
            problems.append(f"{op['op_id']}: letter/action mismatch")
        err = reg_mod.check_order(registry, op["bot"], op["action"])
        if err:
            problems.append(f"{op['op_id']}: {err}")
    return problems


def save(paths: dict, plan: dict) -> str:
    atomic_write_json(paths["plans"] / f"{plan['plan_hash']}.json", plan)
    return plan["plan_hash"]


def load(paths: dict, ref: str) -> dict:
    """Load by full hash, unique prefix, or 'latest'."""
    files = sorted((p for p in paths["plans"].glob("*.json") if not p.name.endswith(".classification.json")),
                   key=lambda p: p.stat().st_mtime)
    if ref == "latest":
        if not files:
            raise FileNotFoundError("no plans yet")
        return read_json(files[-1])
    matches = [f for f in files if f.stem.startswith(ref)]
    if len(matches) != 1:
        raise FileNotFoundError(f"plan ref {ref!r} matched {len(matches)} plans")
    return read_json(matches[0])


def record_approval(paths: dict, plan_hash: str) -> dict:
    """SAFETY: only callable from the local CLI by the owner. No channel can reach this function."""
    rec = {"plan_hash": plan_hash, "approved_at": now_iso(), "approved_by": "owner-local",
           "os_user": getpass.getuser(), "host": socket.gethostname()}
    append_jsonl(paths["approvals"], rec)
    return rec


def is_approved(paths: dict, plan_hash: str) -> bool:
    return any(r.get("plan_hash") == plan_hash for r in read_jsonl(paths["approvals"]))


def render(plan: dict) -> str:
    b = plan["body"]
    lines = [f"PLAN {plan['plan_hash']}", f"root: {b['root']}", f"source: {b['source']}",
             f"summary: {b['summary']}", ""]
    for op in b["operations"]:
        p = op["params"]
        if op["action"] in ("move", "copy"):
            detail = f"{p['src']}  ->  {p['dst']}"
        elif op["action"] == "archive":
            detail = f"{len(p['sources'])} files -> {p['dst_zip']} (originals staged to quarantine)"
        elif op["action"] == "delete":
            detail = f"{p['path']}  (staged to quarantine, sha256 {p['expect_sha256'][:12]}…)"
        else:
            detail = p.get("path") or p.get("out_path") or str(p)
        lines.append(f"[{op['op_id']}] {op['bot']:<5} {op['action']:<13} {detail}")
    return "\n".join(lines)
